"""单测:共享 LLM 客户端工厂(集中重试配置,替代各模块裸 Anthropic())。"""

import os
import unittest
from unittest.mock import patch

from ops_agent import llm


class MakeClientTest(unittest.TestCase):
    @patch("ops_agent.llm.Anthropic")
    def test_uses_configured_max_retries(self, anthropic):
        with patch.dict(os.environ, {"OPS_LLM_MAX_RETRIES": "5", "OPS_PROFILE": "dev"}, clear=True):
            llm.make_client()
        anthropic.assert_called_once()
        self.assertEqual(anthropic.call_args.kwargs.get("max_retries"), 5)

    @patch("ops_agent.llm.Anthropic")
    def test_default_max_retries_is_resilient(self, anthropic):
        # 默认就该比 SDK 默认(2)高:多步诊断单次 429/超时不应中断整条链路
        with patch.dict(os.environ, {}, clear=True):
            llm.make_client()
        self.assertGreaterEqual(anthropic.call_args.kwargs.get("max_retries"), 4)


class BlockDualAccessTest(unittest.TestCase):
    def test_block_supports_attr_and_dict_access(self):
        b = llm._Block({"type": "tool_use", "input": {"x": 1}})
        self.assertEqual(b.type, "tool_use")  # 属性访问(ops-agent)
        self.assertEqual(b.get("type"), "tool_use")  # dict 访问(tooltrans)
        self.assertEqual(b.input, {"x": 1})

    def test_missing_attr_raises_not_silent_none(self):
        # 回归:缺失属性必须抛 AttributeError(对齐原生 SDK),不能像 dict.get 静默返 None,
        # 否则 getattr(block, 'thinking', default)/hasattr 分支只在 gateway 路径静默误判。
        b = llm._Block({"type": "text"})
        with self.assertRaises(AttributeError):
            _ = b.thinking
        self.assertFalse(hasattr(b, "nonexistent"))
        self.assertEqual(getattr(b, "missing", "dflt"), "dflt")  # 默认值机制恢复正常


if __name__ == "__main__":
    unittest.main()
