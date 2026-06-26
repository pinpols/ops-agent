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
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import ssl
import uuid
from base64 import b64encode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse, urlunparse

from ops_agent import __version__
from ops_agent.audit import audit_actor
from ops_agent.budget import BudgetExceeded
from ops_agent.config import Settings, get_settings
from ops_agent.metrics import METRICS
from ops_agent.prompts import PROMPT_VERSION

logger = logging.getLogger("ops_agent.server")

_MAX_BODY_BYTES = 64 * 1024  # webhook body 上限,防超大 payload
_MAX_QUESTION_CHARS = 8192
_TARGET_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ACTOR_RE = re.compile(r"^[A-Za-z0-9_.@:-]{1,128}$")


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


_TRACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _payload_trace_id(payload: dict[str, Any]) -> str:
    # 用户可控:限长 + 字符白名单,防超长串撑 Redis/SQLite + 日志注入;不合规则自生成。
    value = payload.get("trace_id")
    if isinstance(value, str) and _TRACE_ID_RE.match(value.strip()):
        return value.strip()
    return uuid.uuid4().hex


def _request_actor(value: str | None) -> str:
    if value and _ACTOR_RE.match(value.strip()):
        return value.strip()
    return "webhook"


def _validate_question_target(
    payload: dict[str, Any],
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        return None, None, {"error": "缺 question(非空字符串)"}
    question = question.strip()
    if len(question) > _MAX_QUESTION_CHARS:
        return None, None, {"error": "question_too_long", "max_chars": _MAX_QUESTION_CHARS}
    target = payload.get("target")
    if target is None or target == "":
        return question, None, None
    if not isinstance(target, str) or not _TARGET_RE.match(target.strip()):
        return (
            None,
            None,
            {"error": "invalid_target", "detail": "target 只允许 1-64 位字母/数字/_/-"},
        )
    return question, target.strip(), None


def handle_diagnose(
    payload: dict[str, Any], *, actor: str | None = None
) -> tuple[int, dict[str, Any]]:
    """处理 /diagnose 业务(鉴权之后调用)。返回 (http_status, json_body)。只读、注入全拒审批闸。"""
    question, target, error = _validate_question_target(payload)
    if error:
        return 400, error
    if question is None:
        return 400, {"error": "缺 question(非空字符串)"}
    trace_id = _payload_trace_id(payload)
    # 延迟 import:避免 server 模块 import 期就拉起 LLM 依赖链(健康探针应轻)。
    from ops_agent.agent import run_agent

    q = f"[target={target}] {question}" if target else question
    try:
        # include_trace 默认 False → 返回 2-元组;取 [0] 兼容两种返回签名(避免 mypy 解包歧义)。
        with audit_actor(actor):
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
    try:
        with audit_actor(getattr(job, "actor", None)):
            diagnosis = run_agent(q, approver=_deny_all_approver, trace_id=job.trace_id)[0]
    except Exception as exc:
        # 失败也回调:否则配了 OPS_CALLBACK_URL 的下游永远等不到结果、不知任务已失败/进 DLQ。
        _post_callback(job, status="failed", error=_callback_error(exc))
        raise
    result = {"trace_id": job.trace_id, "diagnosis": diagnosis.model_dump(mode="json")}
    _post_callback(job, status="succeeded", result=result)
    return result


def _post_callback(
    job: Any,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """配了 OPS_CALLBACK_URL 就把结果 POST 过去(成功/失败均回调,best-effort,失败只 warn)。"""
    settings = get_settings()
    url = settings.ops_callback_url
    allowed, reason = _callback_url_allowed(url, settings)
    if not allowed:
        if url:
            logger.warning(
                "回调 URL 被安全策略拒绝 job_id=%s trace_id=%s reason=%s",
                job.id,
                job.trace_id,
                reason,
            )
        return
    if url is None:
        return
    import urllib.request

    body = json.dumps(
        {
            "job_id": job.id,
            "trace_id": job.trace_id,
            "status": status,
            "result": result,
            "error": error,
        }
    ).encode("utf-8")
    try:
        if settings.production:
            _post_https_callback_pinned(url, body)
        else:
            req = urllib.request.Request(  # noqa: S310
                url, data=body, headers={"Content-Type": "application/json"}, method="POST"
            )
            urllib.request.urlopen(req, timeout=10).close()  # noqa: S310  # nosec B310
    except Exception as exc:  # noqa: BLE001 - 回调是 best-effort,失败不该影响诊断结果
        logger.warning(
            "回调投递失败 job_id=%s trace_id=%s url=%s: %s", job.id, job.trace_id, url, exc
        )


def _callback_error(exc: Exception) -> str:
    settings = get_settings()
    if settings.production:
        return f"{type(exc).__name__}: callback_error"
    return f"{type(exc).__name__}: {exc}"


def _callback_url_allowed(url: str | None, settings: Settings) -> tuple[bool, str]:
    if not url:
        return False, "not_configured"
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False, "callback_url_must_be_http_or_https"
    host = parsed.hostname.lower()
    if settings.ops_callback_allow_hosts and host not in settings.ops_callback_allow_hosts:
        return False, "callback_host_not_in_allowlist"
    if settings.production:
        if parsed.scheme != "https":
            return False, "prod_callback_requires_https"
        if not settings.ops_callback_allow_hosts:
            return False, "prod_callback_requires_allowlist"
        ok, reason = _callback_host_is_public(host, parsed.port)
        if not ok:
            return False, reason
    return True, "ok"


def _resolve_callback_public_addresses(
    host: str, port: int | None = None
) -> tuple[list[ipaddress.IPv4Address | ipaddress.IPv6Address], str]:
    try:
        addresses = [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return [], "callback_host_dns_failed"
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                return [], "callback_host_unparseable_address"
    if not addresses:
        return [], "callback_host_no_addresses"
    for address in addresses:
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            return [], "callback_host_resolves_to_non_public_ip"
    return sorted(set(addresses), key=str), "ok"


def _callback_host_is_public(host: str, port: int | None = None) -> tuple[bool, str]:
    addresses, reason = _resolve_callback_public_addresses(host, port)
    return bool(addresses), reason


def _post_https_callback_pinned(url: str, body: bytes, *, timeout: float = 10) -> None:
    """POST HTTPS callback after resolving once, then connecting to that vetted IP.

    urllib validates the URL and then resolves again inside urlopen. In production callbacks this
    keeps DNS rebinding from swapping a previously public host to an internal address between
    policy check and connect. TLS still validates against the original hostname via SNI.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("prod callback must be https with hostname")
    host = parsed.hostname.lower()
    port = parsed.port or 443
    addresses, reason = _resolve_callback_public_addresses(host, port)
    if not addresses:
        raise ValueError(reason)

    request = _build_callback_http_request(parsed, body)

    context = ssl.create_default_context()
    last_error: Exception | None = None
    for address in addresses:
        try:
            with (
                _open_callback_tcp_stream(address, host, port, timeout=timeout) as raw,
                context.wrap_socket(raw, server_hostname=host) as sock,
            ):
                sock.settimeout(timeout)
                sock.sendall(request)
                with sock.makefile("rb") as response:
                    status_line = response.readline(512).decode("iso-8859-1", errors="replace")
                parts = status_line.split()
                if len(parts) < 2 or not parts[1].isdigit():
                    raise RuntimeError("invalid callback response")
                status = int(parts[1])
                if status < 200 or status >= 300:
                    raise RuntimeError(f"callback http status {status}")
                return
        except Exception as exc:  # noqa: BLE001 - try next resolved public address
            last_error = exc
    if last_error:
        raise last_error


def _build_callback_http_request(parsed: Any, body: bytes) -> bytes:
    host = parsed.hostname.lower()
    port = parsed.port or 443
    path = urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {_host_header(host, port)}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + body
    return request


def _host_header(host: str, port: int) -> str:
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        base = host
    else:
        base = f"[{address}]" if address.version == 6 else str(address)
    return base if port == 443 else f"{base}:{port}"


def _proxy_from_env() -> tuple[str, int, str | None] | None:
    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("HTTPS_PROXY 仅支持 http://host:port CONNECT 代理")
    auth = None
    if parsed.username:
        username = unquote(parsed.username)
        password = unquote(parsed.password or "")
        token = b64encode(f"{username}:{password}".encode()).decode("ascii")
        auth = f"Proxy-Authorization: Basic {token}\r\n"
    return parsed.hostname, parsed.port or 8080, auth


def _open_callback_tcp_stream(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    host: str,
    port: int,
    *,
    timeout: float,
):
    proxy = _proxy_from_env()
    if proxy is None:
        return socket.create_connection((str(address), port), timeout=timeout)

    proxy_host, proxy_port, proxy_auth = proxy
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    sock.settimeout(timeout)
    target = _host_header(str(address), port)
    auth_header = proxy_auth or ""
    connect = (
        f"CONNECT {target} HTTP/1.1\r\n"
        f"Host: {target}\r\n"
        f"{auth_header}"
        "Proxy-Connection: Keep-Alive\r\n"
        "\r\n"
    ).encode("ascii")
    try:
        sock.sendall(connect)
        with sock.makefile("rb") as response:
            status_line = response.readline(512).decode("iso-8859-1", errors="replace")
            while response.readline(512).strip():
                pass
        parts = status_line.split()
        if len(parts) < 2 or parts[1] != "200":
            raise RuntimeError("callback proxy CONNECT failed")
        return sock
    except Exception:
        sock.close()
        raise


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
