"""工具单一注册表 —— 一处声明(schema + impl + 危险标记),派生 agent 需要的三个视图。

此前这三者散在各叶子模块 + agent.py 手工拼装(schema 在 tools=[...]、impl 在 _ALL_IMPLS、
危险标记在 exec_tools.DANGEROUS_TOOLS),加个工具忘了任一处就静默不可用 / unknown_tool /
漏过审批闸。现在加/删一个工具 = 改 REGISTRY 一行,SCHEMAS / RESULT_IMPLS / DANGEROUS 自动一致。

顺序即发给模型的工具顺序 —— 保持稳定(prompt 前缀缓存命中依赖工具列表有序)。
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ops_agent.exec_tools import RESTART_TOOL, restart_service_result
from ops_agent.metrics_tools import QUERY_METRICS_TOOL, query_metrics_result
from ops_agent.system_tools import (
    INSPECT_COMPOSE_TOOL,
    LIST_SERVICES_TOOL,
    READ_APP_CONFIG_TOOL,
    TAIL_RECENT_ERRORS_TOOL,
    inspect_compose_result,
    list_services_result,
    read_app_config_result,
    tail_recent_errors_result,
)
from ops_agent.tool_result import ToolResult
from ops_agent.tools import (
    QUERY_PG_TEMPLATE_TOOL,
    QUERY_PG_TOOL,
    READ_LOGS_TOOL,
    query_pg_result,
    query_pg_template_result,
    read_logs_result,
)


@dataclass(frozen=True)
class Tool:
    """一个工具的单一声明:Anthropic schema + 返回 ToolResult 的 impl + 是否危险(过审批闸)。"""

    schema: dict[str, Any]
    impl: Callable[..., ToolResult]
    dangerous: bool = False

    @property
    def name(self) -> str:
        return self.schema["name"]


# 顺序 = 发给模型的工具顺序(系统取证类在前,restart 最后);report_diagnosis 由 agent 动态追加。
REGISTRY: list[Tool] = [
    Tool(LIST_SERVICES_TOOL, list_services_result),
    Tool(TAIL_RECENT_ERRORS_TOOL, tail_recent_errors_result),
    Tool(INSPECT_COMPOSE_TOOL, inspect_compose_result),
    Tool(READ_APP_CONFIG_TOOL, read_app_config_result),
    Tool(READ_LOGS_TOOL, read_logs_result),
    Tool(QUERY_METRICS_TOOL, query_metrics_result),
    Tool(QUERY_PG_TEMPLATE_TOOL, query_pg_template_result),
    Tool(QUERY_PG_TOOL, query_pg_result),
    Tool(RESTART_TOOL, restart_service_result, dangerous=True),
]

# 三个派生视图(agent 只依赖这三个,不再各处手工拼装):
SCHEMAS: list[dict[str, Any]] = [t.schema for t in REGISTRY]
RESULT_IMPLS: dict[str, Callable[..., ToolResult]] = {t.name: t.impl for t in REGISTRY}
DANGEROUS: frozenset[str] = frozenset(t.name for t in REGISTRY if t.dangerous)
