"""阶段 1 单测:不打真 API,mock 掉 Anthropic client,验证

  tool_use.input(模型填的结构化参数) → Diagnosis 对象

这条解析链路是阶段 1 的核心,mock 后可离线、零成本反复验。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from ops_agent import diagnose
from ops_agent.models import Diagnosis, Severity


def _fake_response(tool_input: dict):
    """伪造一条 Anthropic 返回:一个 tool_use 内容块,input 为模型填好的诊断参数。"""
    block = SimpleNamespace(type="tool_use", name=diagnose._TOOL_NAME, input=tool_input)
    return SimpleNamespace(content=[block], stop_reason="tool_use")


class DiagnoseLogTest(unittest.TestCase):
    @patch("ops_agent.diagnose.Anthropic")
    def test_parses_tool_input_into_diagnosis(self, anthropic_cls):
        anthropic_cls.return_value.messages.create.return_value = _fake_response(
            {
                "severity": "WARNING",
                "summary": "Redis 连接被拒",
                "root_cause": "本地 16379 上 Redis 未启动",
                "evidence": ["Connection refused: localhost/127.0.0.1:16379"],
                "suggested_action": "确认 valkey/redis 容器在跑",
                "confidence": 0.8,
            }
        )

        result = diagnose.diagnose_log("...some log...")

        self.assertIsInstance(result, Diagnosis)
        self.assertEqual(result.severity, Severity.WARNING)
        self.assertEqual(result.confidence, 0.8)
        self.assertIn("16379", result.evidence[0])

    @patch("ops_agent.diagnose.Anthropic")
    def test_raises_when_no_tool_use_block(self, anthropic_cls):
        # 模型没调工具(只返回文本)→ 应明确报错,而不是静默返回 None
        text_block = SimpleNamespace(type="text", text="我觉得没问题")
        anthropic_cls.return_value.messages.create.return_value = SimpleNamespace(
            content=[text_block], stop_reason="end_turn"
        )
        with self.assertRaises(RuntimeError):
            diagnose.diagnose_log("...")

    @patch("ops_agent.diagnose.Anthropic")
    def test_invalid_input_fails_pydantic_validation(self, anthropic_cls):
        # confidence 超出 0~1 → Pydantic 校验应拦下(证明 schema 约束真生效)
        anthropic_cls.return_value.messages.create.return_value = _fake_response(
            {
                "severity": "INFO",
                "summary": "x",
                "root_cause": "x",
                "evidence": [],
                "suggested_action": "x",
                "confidence": 9.9,
            }
        )
        with self.assertRaises(ValidationError):
            diagnose.diagnose_log("...")


if __name__ == "__main__":
    unittest.main()
