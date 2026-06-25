"""T1 触发层:轻量 HTTP 服务(stdlib,零框架)——把 CLI 变成能接告警/被调度的服务。

端点:
  GET  /healthz   存活探针 → {"status":"ok", version, prompt_version}
  GET  /metrics   Prometheus 文本(agent 自身指标)
  POST /diagnose  告警 webhook:{"question": "...", "target": "..."} → 只读诊断 → Diagnosis JSON

安全:
  - /diagnose 需 Bearer token(OPS_WEBHOOK_TOKEN),常量时间比较;
    未配 token 即 fail-closed(401),不开裸端点。
  - 只读:webhook 注入"全拒"审批闸 —— 即便模型想 restart 也被拒,
    触发层永不执行写操作(T1 边界)。
"""

import hmac
import json
import logging
import signal
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ops_agent import __version__
from ops_agent.budget import BudgetExceeded
from ops_agent.config import get_settings
from ops_agent.metrics import METRICS
from ops_agent.prompts import PROMPT_VERSION

logger = logging.getLogger("ops_agent.server")

_MAX_BODY_BYTES = 64 * 1024  # webhook body 上限,防超大 payload


def _deny_all_approver(tool_name: str, tool_input: dict) -> bool:
    """触发层只读闸:任何危险工具一律拒绝执行。"""
    return False


def authorize(auth_header: str | None, expected_token: str | None) -> bool:
    """Bearer 鉴权:未配 token → False(fail-closed);配了则常量时间比较。"""
    if not expected_token:
        return False
    if not auth_header or not auth_header.startswith("Bearer "):
        return False
    presented = auth_header[len("Bearer ") :].strip()
    return hmac.compare_digest(presented, expected_token)


def _payload_trace_id(payload: dict[str, Any]) -> str:
    value = payload.get("trace_id")
    return value.strip() if isinstance(value, str) and value.strip() else uuid.uuid4().hex


def handle_diagnose(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """处理 /diagnose 业务(鉴权之后调用)。返回 (http_status, json_body)。只读、注入全拒审批闸。"""
    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        return 400, {"error": "缺 question(非空字符串)"}
    target = payload.get("target")
    trace_id = _payload_trace_id(payload)
    # 延迟 import:避免 server 模块 import 期就拉起 LLM 依赖链(健康探针应轻)。
    from ops_agent.agent import run_agent

    q = f"[target={target}] {question}" if target else question
    try:
        # include_trace 默认 False → 返回 2-元组;取 [0] 兼容两种返回签名(避免 mypy 解包歧义)。
        diagnosis = run_agent(q, approver=_deny_all_approver, trace_id=trace_id)[0]
    except BudgetExceeded as e:
        return 503, {"error": "budget_exceeded", "detail": str(e)}
    except Exception:  # noqa: BLE001 - webhook 边界,任何异常转 500 而非崩进程
        METRICS.inc("webhook_error_total")
        # 完整异常(可能含路径/DSN 等内部细节)只进服务端日志;响应仅给类别,不回泄内部信息。
        logger.exception("diagnose 失败 trace_id=%s", trace_id)
        return 500, {"error": "internal_error", "trace_id": trace_id}
    return 200, {"trace_id": trace_id, "diagnosis": diagnosis.model_dump(mode="json")}


def diagnosis_job_handler(job: Any) -> dict[str, Any]:
    """worker 端任务处理:跑只读诊断 → 结果 dict。异常抛出由 JobQueue 标记 FAILED。

    与同步 `handle_diagnose` 共用同一只读内核(注入全拒审批闸);成功后可选回调。
    """
    from ops_agent.agent import run_agent

    q = f"[target={job.target}] {job.question}" if job.target else job.question
    logger.info("开始处理诊断任务 job_id=%s trace_id=%s", job.id, job.trace_id)
    diagnosis = run_agent(q, approver=_deny_all_approver, trace_id=job.trace_id)[0]
    result = {"trace_id": job.trace_id, "diagnosis": diagnosis.model_dump(mode="json")}
    _post_callback(job, result)
    return result


def _post_callback(job: Any, result: dict[str, Any]) -> None:
    """配了 OPS_CALLBACK_URL 就把结果 POST 过去(best-effort,失败只 warn,不影响任务成败)。"""
    url = get_settings().ops_callback_url
    if not url or not url.startswith(("http://", "https://")):
        return
    import urllib.request

    body = json.dumps(
        {"job_id": job.id, "trace_id": job.trace_id, "status": "succeeded", "result": result}
    ).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=10).close()  # noqa: S310
    except Exception as exc:  # noqa: BLE001 - 回调是 best-effort,失败不该影响诊断结果
        logger.warning(
            "回调投递失败 job_id=%s trace_id=%s url=%s: %s", job.id, job.trace_id, url, exc
        )


class _Handler(BaseHTTPRequestHandler):
    server_version = f"ops-agent/{__version__}"

    def _send(self, status: int, body: dict[str, Any] | None = None, *, text: str | None = None):
        self.send_response(status)
        if text is not None:
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            payload = text.encode("utf-8")
        else:
            self.send_header("Content-Type", "application/json")
            payload = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认 stderr 噪声,走 logger
        logger.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(
                200, {"status": "ok", "version": __version__, "prompt_version": PROMPT_VERSION}
            )
        elif self.path == "/metrics":
            jq = getattr(self.server, "job_queue", None)
            updater = getattr(jq, "update_queue_metrics", None) if jq is not None else None
            if updater is not None:
                updater()
            self._send(200, text=METRICS.render())
        elif self.path.startswith("/jobs/"):
            self._handle_job_status(self.path[len("/jobs/") :])
        else:
            self._send(404, {"error": "not_found"})

    def _handle_job_status(self, job_id: str) -> None:
        """异步任务状态查询 GET /jobs/{id}。无队列(同步模式)→ 404。"""
        jq = getattr(self.server, "job_queue", None)
        if jq is None:
            self._send(404, {"error": "async_mode_disabled"})
            return
        job = jq.get(job_id)
        if job is None:
            self._send(404, {"error": "job_not_found"})
            return
        self._send(200, job.to_public())

    def do_POST(self) -> None:
        if self.path != "/diagnose":
            self._send(404, {"error": "not_found"})
            return
        # token 在 serve() 启动时快照到 server 上(一致 + 避免每请求重读密钥文件);
        # 直接构造 server(测试)时无该属性 → None → fail-closed。
        expected = getattr(self.server, "expected_token", None)
        if not authorize(self.headers.get("Authorization"), expected):
            METRICS.inc("webhook_unauthorized_total")
            self._send(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self._send(400, {"error": "invalid_content_length"})
            return
        # 负数(伪造头)会让 rfile.read(-1) 读到 EOF/挂死;非法或超限一律拒。
        if length < 0 or length > _MAX_BODY_BYTES:
            self._send(413, {"error": "payload_too_large"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"error": "invalid_json"})
            return
        if not isinstance(payload, dict):
            self._send(400, {"error": "body must be a JSON object"})
            return
        METRICS.inc("webhook_diagnose_total")
        # 异步模式:入队 + 立即 202(消掉同步阻塞告警 webhook);队列满 → 429 背压。
        jq = getattr(self.server, "job_queue", None)
        if jq is not None:
            question = payload.get("question")
            if not isinstance(question, str) or not question.strip():
                self._send(400, {"error": "缺 question(非空字符串)"})
                return
            trace_id = _payload_trace_id(payload)
            job = jq.submit(question, payload.get("target"), trace_id=trace_id)
            if job is None:
                METRICS.inc("webhook_queue_full_total")
                self._send(429, {"error": "queue_full", "detail": "稍后重试", "trace_id": trace_id})
                return
            self._send(202, {"job_id": job.id, "trace_id": job.trace_id, "status": job.status})
            return
        # 同步模式(默认):内联跑完返回(向后兼容)。
        status, body = handle_diagnose(payload)
        self._send(status, body)


def serve(host: str = "0.0.0.0", port: int = 8080) -> None:  # noqa: S104 - 容器内监听 0.0.0.0
    """启动阻塞式 HTTP 服务,SIGINT / SIGTERM 均优雅退出(容器 docker stop / k8s 驱逐发 SIGTERM)。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    httpd = ThreadingHTTPServer((host, port), _Handler)
    # 鉴权 token 启动时快照一次:一致(密钥轮换不会让并发请求读到半新半旧)+ 避免每请求重读密钥文件。
    httpd.expected_token = settings.ops_webhook_token  # type: ignore[attr-defined]
    # 异步模式:/diagnose 转入队 + 202。后端二选一:
    #   memory(默认,Step 1):进程内队列 + 内置 worker 池。
    #   redis(Step 2):真队列(ingress 只入队/查询),worker 由独立进程 serve-worker 跑。
    job_queue: Any = None
    if settings.ops_async_diagnose:
        if settings.ops_queue_backend == "redis":
            from ops_agent.redisqueue import RedisQueue

            if not settings.ops_redis_url:
                raise ValueError("OPS_QUEUE_BACKEND=redis 需配 OPS_REDIS_URL")
            job_queue = RedisQueue.from_url(
                settings.ops_redis_url,
                queue_key=settings.ops_queue_key,
                dlq_key=settings.ops_dlq_key,
                max_queue=settings.ops_queue_max,
                job_ttl=settings.ops_job_ttl_seconds,
                max_retries=settings.ops_max_retries,
                retry_base_seconds=settings.ops_retry_base_seconds,
                retry_max_seconds=settings.ops_retry_max_seconds,
                queue_depth_alert_threshold=settings.ops_queue_depth_alert_threshold,
            )
            logger.info("async 模式:redis 后端(worker 由 serve-worker 独立进程跑)")
        else:
            from ops_agent.jobqueue import JobQueue

            job_queue = JobQueue(
                diagnosis_job_handler,
                workers=settings.ops_worker_count,
                max_queue=settings.ops_queue_max,
                max_retries=settings.ops_max_retries,
                retry_base_seconds=settings.ops_retry_base_seconds,
                retry_max_seconds=settings.ops_retry_max_seconds,
                queue_depth_alert_threshold=settings.ops_queue_depth_alert_threshold,
            )
            logger.info("async 模式:memory 后端 workers=%d", settings.ops_worker_count)
        httpd.job_queue = job_queue  # type: ignore[attr-defined]
    # SIGTERM(docker stop / k8s)默认直接终止、不跑 finally;转成 KeyboardInterrupt 走优雅退出。
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    logger.info("ops-agent serve on %s:%d (version=%s)", host, port, __version__)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到停止信号,优雅退出")
    finally:
        if job_queue is not None:
            # memory 后端有内置 worker → shutdown 排空;redis 后端 ingress 只需 close 连接。
            closer = getattr(job_queue, "shutdown", None) or getattr(job_queue, "close", None)
            if closer is not None:
                closer()
        httpd.shutdown()
        httpd.server_close()


def _raise_keyboard_interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt
