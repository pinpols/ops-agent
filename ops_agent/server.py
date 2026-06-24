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


def handle_diagnose(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """处理 /diagnose 业务(鉴权之后调用)。返回 (http_status, json_body)。只读、注入全拒审批闸。"""
    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        return 400, {"error": "缺 question(非空字符串)"}
    target = payload.get("target")
    # 延迟 import:避免 server 模块 import 期就拉起 LLM 依赖链(健康探针应轻)。
    from ops_agent.agent import run_agent

    q = f"[target={target}] {question}" if target else question
    try:
        # include_trace 默认 False → 返回 2-元组;取 [0] 兼容两种返回签名(避免 mypy 解包歧义)。
        diagnosis = run_agent(q, approver=_deny_all_approver)[0]
    except BudgetExceeded as e:
        return 503, {"error": "budget_exceeded", "detail": str(e)}
    except Exception as e:  # noqa: BLE001 - webhook 边界,任何异常转 500 而非崩进程
        METRICS.inc("webhook_error_total")
        logger.exception("diagnose 失败")
        return 500, {"error": "internal_error", "detail": f"{type(e).__name__}: {e}"}
    return 200, {"diagnosis": diagnosis.model_dump(mode="json")}


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
            self._send(200, text=METRICS.render())
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/diagnose":
            self._send(404, {"error": "not_found"})
            return
        if not authorize(self.headers.get("Authorization"), get_settings().ops_webhook_token):
            METRICS.inc("webhook_unauthorized_total")
            self._send(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > _MAX_BODY_BYTES:
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
        status, body = handle_diagnose(payload)
        self._send(status, body)


def serve(host: str = "0.0.0.0", port: int = 8080) -> None:  # noqa: S104 - 容器内监听 0.0.0.0
    """启动阻塞式 HTTP 服务,Ctrl-C / SIGTERM 优雅退出。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    httpd = ThreadingHTTPServer((host, port), _Handler)
    logger.info("ops-agent serve on %s:%d (version=%s)", host, port, __version__)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到中断,优雅退出")
    finally:
        httpd.shutdown()
        httpd.server_close()
