"""只读指标工具:查 Prometheus(instant query)。补"只有日志+PG、看不见指标"的盲区。

零依赖(stdlib urllib)。只打 GET /api/v1/query(只读 instant 查询),不碰 admin/写接口。
base URL 来自 resolve_target(target).metrics_url 或 OPS_METRICS_URL;未配则明确报错不静默。
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from ops_agent.targets import resolve_target
from ops_agent.tool_result import ToolResult

_QUERY_PATH = "/api/v1/query"
_RESULT_CAP = 50  # 防一条 PromQL 拉回上万 series 撑爆上下文
_TIMEOUT_S = 10


def query_metrics_result(promql: str, target: str | None = None) -> ToolResult:
    """对目标系统的 Prometheus 跑一条 instant PromQL,返回结构化结果(截断到 _RESULT_CAP series)。"""
    if not isinstance(promql, str) or not promql.strip():
        return ToolResult.failure("[query_metrics] promql 必须是非空字符串")
    try:
        base = resolve_target(target).metrics_url
    except ValueError as e:
        return ToolResult.failure(f"[query_metrics] {e}")
    if not base:
        return ToolResult.failure(
            "[query_metrics] 该 target 未配置 metrics_url(OPS_METRICS_URL 或 targets.toml)"
        )
    # scheme 校验在构建/打开请求**之前**:杜绝 file://、ftp:// 等非 http(s) 被 urlopen。
    if not base.startswith(("http://", "https://")):
        return ToolResult.failure("[query_metrics] metrics_url 必须是 http(s)")
    url = base.rstrip("/") + _QUERY_PATH + "?" + urllib.parse.urlencode({"query": promql})
    req = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310 - 已校验 http(s)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310 - 已校验 scheme
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError) as e:
        return ToolResult.failure(f"[query_metrics] 请求失败:{e}")
    except json.JSONDecodeError as e:
        return ToolResult.failure(f"[query_metrics] 响应非 JSON:{e}")

    if payload.get("status") != "success":
        return ToolResult.failure(f"[query_metrics] Prometheus 报错:{payload.get('error')}")
    results = (payload.get("data") or {}).get("result") or []
    truncated = len(results) > _RESULT_CAP
    rows = [{"metric": r.get("metric", {}), "value": r.get("value")} for r in results[:_RESULT_CAP]]
    return ToolResult.success(
        json.dumps(rows, ensure_ascii=False),
        promql=promql,
        series=len(results),
        truncated=truncated,
    )


QUERY_METRICS_TOOL = {
    "name": "query_metrics",
    "description": (
        "查目标系统 Prometheus 的只读 instant 指标(PromQL)。"
        "适合看 CPU/内存/QPS/延迟/队列深度等数值型信号,补日志看不到的趋势。"
        "示例:rate(http_requests_total[5m]) / up / pg_stat_activity_count。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "promql": {"type": "string", "description": "一条 instant PromQL 查询表达式"},
            "target": {"type": "string", "description": "目标系统名(多目标时);留空用默认"},
        },
        "required": ["promql"],
    },
}

QUERY_METRICS_TOOL_RESULT_IMPLS = {"query_metrics": query_metrics_result}
