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


if __name__ == "__main__":
    unittest.main()
