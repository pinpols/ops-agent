"""阶段 3b 单测:LangGraph 版的"装配正确性"(不打真 API)。

完整跑一轮需要真模型(LangGraph 内部 tool-calling 难可靠 mock),所以这里验证:
工具 wrapper 透传 + 图能装配出预期结构(节点 = 手写循环的对应物)。
底层 read_logs/query_pg 的功能/安全已在 test_tools.py 覆盖。
"""

import os
import unittest
from pathlib import Path


class GraphAgentBuildTest(unittest.TestCase):
    def setUp(self):
        os.environ["OPS_LOG_DIR"] = str(Path(__file__).resolve().parent.parent / "data")
        os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy")  # 装配不打 API
        os.environ.pop("OPS_PG_DSN", None)

    def test_tool_wrappers_passthrough(self):
        from ops_agent import graph_agent

        out = graph_agent.read_logs.invoke({"service": "console", "pattern": "WARN"})
        self.assertIn("read_logs", out)
        self.assertEqual(graph_agent.read_logs.name, "read_logs")
        self.assertTrue(graph_agent.read_logs.description)  # docstring → 给模型的 description

        pg = graph_agent.query_pg.invoke({"sql": "update t set x=1"})
        self.assertIn("只允许", pg)  # 护栏在 wrapper 之下仍生效

    def test_graph_assembles_with_expected_nodes(self):
        from ops_agent import graph_agent

        agent = graph_agent.build_agent()
        nodes = set(agent.get_graph().nodes)
        # 这些节点 = 手写循环的对应物(见 docs/phase3b-concepts.md §1)
        self.assertIn("agent", nodes)
        self.assertIn("tools", nodes)
        self.assertIn("generate_structured_response", nodes)


if __name__ == "__main__":
    unittest.main()
