"""工具注册表:派生的三视图与各叶子模块原始声明一致(防注册表与实现漂移)+ 单一危险源。"""

import unittest

from ops_agent import tool_registry as tr
from ops_agent.exec_tools import EXEC_TOOL_RESULT_IMPLS
from ops_agent.flink_tools import QUERY_FLINK_TOOL_RESULT_IMPLS
from ops_agent.metrics_tools import QUERY_METRICS_TOOL_RESULT_IMPLS
from ops_agent.system_tools import SYSTEM_TOOL_RESULT_IMPLS
from ops_agent.tools import TOOL_RESULT_IMPLS


class ToolRegistryTest(unittest.TestCase):
    def test_result_impls_equal_union_of_leaf_dicts(self):
        # 注册表派发表必须正好等于各叶子模块原始 impl 字典的并集(不漏不多)
        expected = {
            **SYSTEM_TOOL_RESULT_IMPLS,
            **TOOL_RESULT_IMPLS,
            **QUERY_METRICS_TOOL_RESULT_IMPLS,
            **QUERY_FLINK_TOOL_RESULT_IMPLS,
            **EXEC_TOOL_RESULT_IMPLS,
        }
        self.assertEqual(tr.RESULT_IMPLS, expected)

    def test_every_schema_has_impl_and_vice_versa(self):
        # 三视图自洽:每个 schema 都有 impl,每个 impl 都有 schema(消除"静默不可用/unknown_tool")
        schema_names = {s["name"] for s in tr.SCHEMAS}
        self.assertEqual(schema_names, set(tr.RESULT_IMPLS))

    def test_schema_order_is_stable(self):
        # 顺序稳定(缓存前缀依赖):系统取证类在前,restart 在最后
        names = [s["name"] for s in tr.SCHEMAS]
        self.assertEqual(
            names,
            [
                "list_services",
                "tail_recent_errors",
                "inspect_compose",
                "read_app_config",
                "read_logs",
                "query_metrics",
                "query_pg_template",
                "query_pg",
                "query_flink_rest",
                "restart_service",
            ],
        )

    def test_dangerous_is_single_source(self):
        self.assertEqual(tr.DANGEROUS, frozenset({"restart_service"}))
        # exec_tools 不再另立 DANGEROUS_TOOLS(防双源)
        import ops_agent.exec_tools as exec_tools

        self.assertFalse(hasattr(exec_tools, "DANGEROUS_TOOLS"))


if __name__ == "__main__":
    unittest.main()
