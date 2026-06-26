"""只读 Flink 工具:查 Flink JobManager REST API。补"看不见流作业运行态"的盲区。

零依赖(stdlib urllib)。**只读保证是结构性的**:只发 GET —— Flink 的写操作(cancel/stop、
trigger savepoint、改并行度、删 state)全是 POST/PATCH/DELETE,GET-only 从协议上就到不了。
再叠一层**路径白名单**(纵深防御 + 防奇怪路径)。base URL 来自 resolve_target(target).flink_url
或 OPS_FLINK_URL;未配则明确报错不静默。

写操作要做,必须走另一条 HITL/审批路径(本工具永不涉及)。
"""

import re
import urllib.error
import urllib.request

from ops_agent.targets import resolve_target
from ops_agent.tool_result import ToolResult

_RESPONSE_CAP = 12000  # 单次响应字符上限,防 job 详情大 JSON 撑爆上下文
_TIMEOUT_S = 10
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


def _path_allowed(path: str) -> bool:
    if ".." in path or "://" in path or any(c.isspace() for c in path):
        return False
    return any(rx.match(path) for rx in _ALLOWED_PATHS)


def query_flink_rest_result(path: str, target: str | None = None) -> ToolResult:
    """GET 一个白名单内的 Flink JobManager 只读 REST 路径,返回响应 JSON(截断到 _RESPONSE_CAP)。"""
    if not isinstance(path, str) or not path.strip():
        return ToolResult.failure("[query_flink_rest] path 必须是非空字符串")
    path = path.strip()
    if not path.startswith("/"):
        return ToolResult.failure("[query_flink_rest] path 必须以 / 开头(REST 路径,不是完整 URL)")
    if not _path_allowed(path):
        return ToolResult.failure(
            "[query_flink_rest] 路径不在只读白名单内(仅 /overview、/jobs[/:id[/exceptions"
            "|/checkpoints|/vertices/:vid[/backpressure]]]、/taskmanagers[/:id[/metrics]]、"
            "/jobmanager/(config|metrics))",
            path=path,
        )
    try:
        base = resolve_target(target).flink_url
    except ValueError as e:
        return ToolResult.failure(f"[query_flink_rest] {e}")
    if not base:
        return ToolResult.failure(
            "[query_flink_rest] 该 target 未配置 flink_url(OPS_FLINK_URL 或 targets.toml)"
        )
    if not base.startswith(("http://", "https://")):
        return ToolResult.failure("[query_flink_rest] flink_url 必须是 http(s)")
    url = base.rstrip("/") + path
    # method 显式 GET:结构性只读(Flink 写操作均非 GET)。
    req = urllib.request.Request(  # noqa: S310  # nosec B310
        url, headers={"Accept": "application/json"}, method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310  # nosec B310
            raw = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as e:
        return ToolResult.failure(f"[query_flink_rest] 请求失败:{e}", path=path)
    truncated = len(raw) > _RESPONSE_CAP
    return ToolResult.success(
        raw[:_RESPONSE_CAP],
        path=path,
        bytes=len(raw),
        truncated=truncated,
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
