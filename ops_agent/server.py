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

本模块只管 HTTP 传输 + 启停;诊断任务内核在 jobs.py,回调投递在 callback.py
(原 god-module 已拆)。为兼容 server.X 老调用点(cli/测试),此处 re-export 关键符号。
"""

import hmac
import json
import logging
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ops_agent import __version__

# re-export:保持 server.X 老调用点(cli / 测试)不破(import X as X = 显式 re-export,不报 F401)。
from ops_agent.callback import _callback_url_allowed as _callback_url_allowed
from ops_agent.callback import _post_callback as _post_callback
from ops_agent.config import get_settings
from ops_agent.jobs import _MAX_QUESTION_CHARS as _MAX_QUESTION_CHARS
from ops_agent.jobs import _deny_all_approver as _deny_all_approver
from ops_agent.jobs import (
    _payload_trace_id,
    _request_actor,
    _validate_question_target,
    diagnosis_job_handler,
    handle_diagnose,
)
from ops_agent.metrics import METRICS
from ops_agent.prompts import PROMPT_VERSION

logger = logging.getLogger("ops_agent.server")

_MAX_BODY_BYTES = 64 * 1024  # webhook body 上限,防超大 payload


def authorize(auth_header: str | None, expected_token: str | None) -> bool:
    """Bearer 鉴权:未配 token → False(fail-closed);配了则常量时间比较。"""
    if not expected_token:
        return False
    if not auth_header or not auth_header.startswith("Bearer "):
        return False
    presented = auth_header[len("Bearer ") :].strip()
    return hmac.compare_digest(presented, expected_token)


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

    def _authorized(self) -> bool:
        """Bearer 鉴权:token 启动时快照到 server(未配则 fail-closed)。/diagnose 与 /jobs 共用。"""
        expected = getattr(self.server, "expected_token", None)
        if authorize(self.headers.get("Authorization"), expected):
            return True
        METRICS.inc("webhook_unauthorized_total")
        self._send(401, {"error": "unauthorized"})
        return False

    def do_GET(self) -> None:
        if self.path == "/healthz":
            # liveness:进程能服务即存活。后端(Redis)抖动不该重启 pod,故不在此探后端。
            self._send(
                200, {"status": "ok", "version": __version__, "prompt_version": PROMPT_VERSION}
            )
        elif self.path == "/readyz":
            self._handle_readyz()
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

    def _handle_readyz(self) -> None:
        """readiness:能否接活。redis 后端探 Redis 可达;不可达→503 摘出 Service。

        与 liveness 区分:后端 down 时 readiness 失败(暂不收流量),但 liveness 仍 OK
        (不重启 pod),Redis 恢复后自动回到就绪。
        """
        jq = getattr(self.server, "job_queue", None)
        ping = getattr(jq, "ping", None) if jq is not None else None
        if ping is not None and not ping():
            self._send(503, {"status": "not_ready", "reason": "queue_backend_unreachable"})
            return
        self._send(200, {"status": "ready"})

    def _handle_job_status(self, job_id: str) -> None:
        """异步任务状态查询 GET /jobs/{id}。需鉴权(否则诊断结果裸泄露);无队列→404。"""
        if not self._authorized():  # 安全:/jobs 与 /diagnose 同等鉴权,防越权读他人诊断
            return
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
        if not self._authorized():
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
        actor = _request_actor(self.headers.get("X-Ops-Actor"))
        # 异步模式:入队 + 立即 202(消掉同步阻塞告警 webhook);队列满 → 429 背压。
        jq = getattr(self.server, "job_queue", None)
        if jq is not None:
            question, target, error = _validate_question_target(payload)
            if error:
                self._send(400, error)
                return
            trace_id = _payload_trace_id(payload)
            if question is None:
                self._send(400, {"error": "缺 question(非空字符串)"})
                return
            job = jq.submit(question, target, trace_id=trace_id, actor=actor)
            if job is None:
                METRICS.inc("webhook_queue_full_total")
                self._send(429, {"error": "queue_full", "detail": "稍后重试", "trace_id": trace_id})
                return
            self._send(202, {"job_id": job.id, "trace_id": job.trace_id, "status": job.status})
            return
        # 同步模式(默认):内联跑完返回(向后兼容)。
        status, body = handle_diagnose(payload, actor=actor)
        self._send(status, body)


def serve(
    host: str = "0.0.0.0",
    port: int = 8080,  # noqa: S104  # nosec B104
) -> None:
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

            job_queue = RedisQueue.from_settings(settings)
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
