"""Flink 写操作工具(危险)—— cancel job / trigger savepoint。

与只读 query_flink_rest 严格分开。两道闸:
1. **审批闸(HITL)**:登记 DANGEROUS,执行前过 agent 审批闸 —— webhook 路径注入 deny-all,
   **从网络侧完全不可达**;CLI/chat 才能 y/N 批准(见 agent.py / ADR-0001)。
2. **执行侧默认安全**:复用 exec_tools.exec_gate —— 默认 dry-run(只报"将做什么"),真执行需
   OPS_ALLOW_EXEC=true,prod 还需 OPS_PROD_ALLOW_EXEC=true。

只有"被批准 + 显式开 exec"才真 PATCH/POST 到 Flink REST。base 来自 resolve_target.flink_url。
"""

import json
import re
import urllib.error
import urllib.request

from ops_agent.config import get_settings
from ops_agent.exec_tools import exec_gate
from ops_agent.http_limits import read_limited_text
from ops_agent.targets import resolve_target
from ops_agent.tool_result import ToolResult

_JOBID_RE = re.compile(r"^[0-9a-f]{32}$")  # Flink jobid = 32 位 hex
_RESPONSE_CAP_BYTES = 50_000
_TIMEOUT_S = 30


def _flink_write(
    jobid: str, *, tool: str, method: str, path_suffix: str, dry_run_desc: str, body: dict | None
) -> ToolResult:
    if not isinstance(jobid, str) or not _JOBID_RE.match(jobid):
        return ToolResult.failure(f"[{tool}] jobid 非法(应为 32 位 hex)", jobid=jobid)
    settings = get_settings()
    gate = exec_gate(settings, dry_run_desc=dry_run_desc, tool=tool, jobid=jobid)
    if gate is not None:  # dry-run 或 prod 硬拒 → 不出网
        return gate
    try:
        base = resolve_target(None).flink_url
    except ValueError as e:
        return ToolResult.failure(f"[{tool}] {e}", jobid=jobid)
    if not base or not base.startswith(("http://", "https://")):
        return ToolResult.failure(f"[{tool}] 未配置或非 http(s) 的 flink_url", jobid=jobid)
    url = f"{base.rstrip('/')}/jobs/{jobid}{path_suffix}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(  # noqa: S310  # nosec B310
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310  # nosec B310
            out, truncated, _observed = read_limited_text(resp, max_bytes=_RESPONSE_CAP_BYTES)
    except (urllib.error.URLError, TimeoutError) as e:
        return ToolResult.failure(f"[{tool}] 执行失败:{e}", jobid=jobid)
    return ToolResult.success(
        f"[{tool}] 已执行 {method} {url}\n{out[:500]}",
        jobid=jobid,
        executed=True,
        truncated=truncated,
    )


def flink_cancel_job_result(jobid: str) -> ToolResult:
    """取消一个 Flink 作业(危险/写)。Flink: PATCH /jobs/:jobid?mode=cancel。"""
    return _flink_write(
        jobid,
        tool="flink_cancel_job",
        method="PATCH",
        path_suffix="?mode=cancel",
        dry_run_desc=f"将取消 Flink 作业 {jobid}",
        body=None,
    )


def flink_trigger_savepoint_result(jobid: str) -> ToolResult:
    """对一个 Flink 作业触发 savepoint(危险/写)。Flink: POST /jobs/:jobid/savepoints。"""
    return _flink_write(
        jobid,
        tool="flink_trigger_savepoint",
        method="POST",
        path_suffix="/savepoints",
        dry_run_desc=f"将对 Flink 作业 {jobid} 触发 savepoint",
        body={"cancel-job": False},
    )


FLINK_CANCEL_TOOL = {
    "name": "flink_cancel_job",
    "description": "取消指定 Flink 作业(危险/写,需人工审批)。仅在确认该作业需停止时使用。",
    "input_schema": {
        "type": "object",
        "properties": {"jobid": {"type": "string", "description": "Flink jobid(32 位 hex)"}},
        "required": ["jobid"],
    },
}

FLINK_SAVEPOINT_TOOL = {
    "name": "flink_trigger_savepoint",
    "description": "对指定 Flink 作业触发 savepoint(危险/写,需人工审批)。用于安全升级/迁移前留点。",
    "input_schema": {
        "type": "object",
        "properties": {"jobid": {"type": "string", "description": "Flink jobid(32 位 hex)"}},
        "required": ["jobid"],
    },
}

FLINK_EXEC_TOOL_RESULT_IMPLS = {
    "flink_cancel_job": flink_cancel_job_result,
    "flink_trigger_savepoint": flink_trigger_savepoint_result,
}
