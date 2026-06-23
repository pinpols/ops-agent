"""阶段 2 回合单测:mock Anthropic,验证

  模型请求 read_logs → 我们真执行并喂回 → 模型给 report_diagnosis → 解析成 Diagnosis

证明 tool_use → 执行 → tool_result 喂回 → 结论 这条回合链路通。
"""

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ops_agent import investigate
from ops_agent.models import Diagnosis, Severity


def _tool_use(tool_id, name, inp):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=inp)


class InvestigateTest(unittest.TestCase):
    def setUp(self):
        os.environ["OPS_LOG_DIR"] = str(Path(__file__).resolve().parent.parent / "data")

    @patch("ops_agent.investigate.make_client")
    def test_read_then_report_roundtrip(self, anthropic_cls):
        create = anthropic_cls.return_value.messages.create
        # 回合1:模型要读 console 日志(真会被执行)
        # 回合2:模型基于日志给结论
        create.side_effect = [
            SimpleNamespace(
                content=[_tool_use("t1", "read_logs", {"service": "console", "pattern": "WARN"})],
                stop_reason="tool_use",
            ),
            SimpleNamespace(
                content=[
                    _tool_use(
                        "t2",
                        investigate.REPORT_TOOL_NAME,
                        {
                            "severity": "WARNING",
                            "summary": "Redis 连接被拒",
                            "root_cause": "16379 上 Redis 未启",
                            "evidence": ["Connection refused"],
                            "suggested_action": "查 valkey 容器",
                            "confidence": 0.7,
                        },
                    )
                ],
                stop_reason="tool_use",
            ),
        ]

        result = investigate.investigate("console 最近有什么异常?")

        self.assertIsInstance(result, Diagnosis)
        self.assertEqual(result.severity, Severity.WARNING)
        # 第二次 create 的 messages 里应包含我们喂回的 read_logs 执行结果(tool_result)
        second_call_messages = create.call_args_list[1].kwargs["messages"]
        fed_back = any(
            isinstance(m.get("content"), list)
            and any(
                isinstance(b, dict)
                and b.get("type") == "tool_result"
                and "read_logs" in str(b.get("content", ""))
                for b in m["content"]
            )
            for m in second_call_messages
        )
        self.assertTrue(fed_back, "read_logs 执行结果应作为 tool_result 喂回模型")


if __name__ == "__main__":
    unittest.main()
