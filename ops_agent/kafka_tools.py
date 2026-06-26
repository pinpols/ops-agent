"""只读 Kafka 工具:查 Kafka REST(Confluent REST Proxy / kafka-rest v3 Admin API)。

补"看不见 broker/topic/消费组运行态"的盲区。只读保证是结构性的(GET-only,见 rest_tools)+
端点白名单。base URL 来自 resolve_target(target).kafka_rest_url 或 OPS_KAFKA_REST_URL。
写操作(建/删 topic、改配置、重置 offset)是 POST/DELETE/PATCH,本工具到不了;要做走 HITL/审批。

注:多数生产 Kafka 的可观测走 Prometheus(kafka_exporter / jmx),consumer lag、under-replicated、
offline partitions 等用 query_metrics 看更通用;本工具覆盖部署了 Kafka REST 的场景。
"""

import re

from ops_agent.rest_tools import path_allowed, readonly_rest_get
from ops_agent.targets import resolve_target
from ops_agent.tool_result import ToolResult

_ID = r"[\w.-]+"  # cluster id / topic / consumer-group 的安全字符集
_N = r"\d+"  # partition / broker id

# Kafka REST v3 Admin API 只读端点白名单(GET-only;写是 POST/DELETE,协议上到不了)。
_ALLOWED_PATHS = [
    re.compile(p)
    for p in (
        r"^/v3/clusters$",
        rf"^/v3/clusters/{_ID}$",
        rf"^/v3/clusters/{_ID}/topics$",
        rf"^/v3/clusters/{_ID}/topics/{_ID}$",
        rf"^/v3/clusters/{_ID}/topics/{_ID}/partitions$",
        rf"^/v3/clusters/{_ID}/topics/{_ID}/partitions/{_N}$",
        rf"^/v3/clusters/{_ID}/topics/{_ID}/partitions/{_N}/replicas$",
        rf"^/v3/clusters/{_ID}/brokers$",
        rf"^/v3/clusters/{_ID}/brokers/{_N}$",
        rf"^/v3/clusters/{_ID}/consumer-groups$",
        rf"^/v3/clusters/{_ID}/consumer-groups/{_ID}$",
        rf"^/v3/clusters/{_ID}/consumer-groups/{_ID}/consumers$",
        rf"^/v3/clusters/{_ID}/consumer-groups/{_ID}/lags$",
        rf"^/v3/clusters/{_ID}/consumer-groups/{_ID}/lag-summary$",
    )
]
_HINT = "仅 /v3/clusters[/:id[/(topics|brokers|consumer-groups)/...]] 下的只读 GET 端点"


def _path_allowed(path: str) -> bool:
    return path_allowed(path, _ALLOWED_PATHS)


def query_kafka_rest_result(path: str, target: str | None = None) -> ToolResult:
    """GET 一个白名单内的 Kafka REST v3 只读路径(broker/topic/分区/消费组 lag),返回 JSON(截断)。"""
    try:
        base = resolve_target(target).kafka_rest_url
    except ValueError as e:
        return ToolResult.failure(f"[query_kafka_rest] {e}")
    return readonly_rest_get(
        base,
        path,
        _ALLOWED_PATHS,
        tool="query_kafka_rest",
        whitelist_hint=_HINT,
        not_configured_msg=(
            "[query_kafka_rest] 该 target 未配置 kafka_rest_url(OPS_KAFKA_REST_URL 或 targets.toml)"
        ),
    )


QUERY_KAFKA_TOOL = {
    "name": "query_kafka_rest",
    "description": (
        "查 Kafka REST(v3 Admin API,GET-only,只读不写)。诊断 Kafka 用:"
        "/v3/clusters 拿 cluster id、/v3/clusters/:id/brokers 看 broker 存活、"
        "/v3/clusters/:id/topics/:t/partitions 看分区 leader/ISR/副本、"
        "/v3/clusters/:id/consumer-groups/:g/lags 看消费组堆积。"
        "注:Kafka lag/ISR/offline 也常用 query_metrics(kafka_exporter)看,更通用。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "白名单内的只读 REST 路径,如 /v3/clusters 或 消费组的 .../lags",
            },
            "target": {"type": "string", "description": "目标系统名(多目标时);留空用默认"},
        },
        "required": ["path"],
    },
}

QUERY_KAFKA_TOOL_RESULT_IMPLS = {"query_kafka_rest": query_kafka_rest_result}
