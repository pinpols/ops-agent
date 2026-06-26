"""只读 Flink 工具:查 Flink JobManager REST API。补"看不见流作业运行态"的盲区。

只读保证是结构性的(GET-only,见 rest_tools)+ 端点白名单。base URL 来自
resolve_target(target).flink_url 或 OPS_FLINK_URL;未配则明确报错不静默。
写操作(cancel/stop/savepoint/改并行度/删 state)要做,必须走另一条 HITL/审批路径(本工具永不涉及)。
"""

import re

from ops_agent.rest_tools import DEFAULT_CAP, path_allowed, readonly_rest_get
from ops_agent.targets import resolve_target
from ops_agent.tool_result import ToolResult

_RESPONSE_CAP = DEFAULT_CAP  # 保留供测试引用
_ID = r"[\w.:-]+"  # jobid(32-hex)/ vertex id / taskmanager id 的安全字符集

# 只读端点白名单(逐条精确匹配,不允许尾随其他路径 / 查询串 / `..`)。
_ALLOWED_PATHS = [
    re.compile(p)
    for p in (
        r"^/overview$",
        r"^/config$",
        r"^/jobs$",
        r"^/jobs/overview$",
        rf"^/jobs/{_ID}$",
        rf"^/jobs/{_ID}/exceptions$",
        rf"^/jobs/{_ID}/checkpoints$",
        rf"^/jobs/{_ID}/checkpoints/config$",
        rf"^/jobs/{_ID}/vertices/{_ID}$",
        rf"^/jobs/{_ID}/vertices/{_ID}/backpressure$",
        r"^/taskmanagers$",
        rf"^/taskmanagers/{_ID}$",
        rf"^/taskmanagers/{_ID}/metrics$",
        r"^/jobmanager/config$",
        r"^/jobmanager/metrics$",
    )
]
_HINT = (
    "仅 /overview、/jobs[/:id[/exceptions|/checkpoints|/vertices/:vid[/backpressure]]]、"
    "/taskmanagers[/:id[/metrics]]、/jobmanager/(config|metrics)"
)


def _path_allowed(path: str) -> bool:
    return path_allowed(path, _ALLOWED_PATHS)


def query_flink_rest_result(path: str, target: str | None = None) -> ToolResult:
    """GET 一个白名单内的 Flink JobManager 只读 REST 路径,返回响应 JSON(截断)。"""
    try:
        base = resolve_target(target).flink_url
    except ValueError as e:
        return ToolResult.failure(f"[query_flink_rest] {e}")
    return readonly_rest_get(
        base,
        path,
        _ALLOWED_PATHS,
        tool="query_flink_rest",
        whitelist_hint=_HINT,
        not_configured_msg=(
            "[query_flink_rest] 该 target 未配置 flink_url(OPS_FLINK_URL 或 targets.toml)"
        ),
    )


QUERY_FLINK_TOOL = {
    "name": "query_flink_rest",
    "description": (
        "查 Flink JobManager 的只读 REST(GET-only,只读不写)。诊断流作业用:"
        "/overview 看集群、/jobs 列作业、/jobs/:id 看状态与重启次数、"
        "/jobs/:id/exceptions 看异常根因、/jobs/:id/checkpoints 看 checkpoint 失败/超时、"
        "/jobs/:id/vertices/:vid/backpressure 看反压、/taskmanagers 看 TM 存活/资源。"
        "先 /jobs 拿 jobid,再按需下钻。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "白名单内的只读 REST 路径,如 /jobs 或 /jobs/<jobid>/checkpoints",
            },
            "target": {"type": "string", "description": "目标系统名(多目标时);留空用默认"},
        },
        "required": ["path"],
    },
}

QUERY_FLINK_TOOL_RESULT_IMPLS = {"query_flink_rest": query_flink_rest_result}
