"""诊断任务内核:webhook 同步路径(handle_diagnose)与 worker 异步路径(diagnosis_job_handler)
共用的只读诊断逻辑 + 请求校验。

从 server.py 拆出(原 god-module 的核心职责),让传输层(server)、任务内核(jobs)、回调
(callback)各司其职;worker_main 直接 import 本模块的 handler,解掉 worker→server 的反向依赖。
只读边界:两条路径都注入 `_deny_all_approver`(危险工具一律拒)。
"""

import logging
import re
import uuid
from typing import Any

from ops_agent.audit import audit_actor
from ops_agent.budget import BudgetExceeded
from ops_agent.callback import _post_callback
from ops_agent.metrics import METRICS

logger = logging.getLogger("ops_agent.jobs")

_MAX_QUESTION_CHARS = 8192
_TARGET_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ACTOR_RE = re.compile(r"^[A-Za-z0-9_.@:-]{1,128}$")
_TRACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _deny_all_approver(tool_name: str, tool_input: dict) -> bool:
    """触发层只读闸:任何危险工具一律拒绝执行。"""
    return False


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
    # 延迟 import:避免本模块 import 期就拉起 LLM 依赖链(健康探针应轻)。
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
    # 失败回调不在这里投递(P2-3):每次重试 attempt 都会发 failed、之后又可能发 succeeded,
    # 下游收到乱序终态信号。failed 只在**终局**(重试耗尽/不可重试判 dead)由队列层投递,
    # payload 带最终 attempts;下游"等不到失败通知"的诉求由终局回调满足。
    with audit_actor(getattr(job, "actor", None)):
        diagnosis = run_agent(q, approver=_deny_all_approver, trace_id=job.trace_id)[0]
    result = {"trace_id": job.trace_id, "diagnosis": diagnosis.model_dump(mode="json")}
    _post_callback(job, status="succeeded", result=result)
    return result
