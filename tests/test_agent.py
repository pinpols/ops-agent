"""阶段 3 多步循环单测:mock Anthropic,验证

read_logs → query_pg → report_diagnosis 多步链路被逐个执行并喂回,且记忆(messages)累积。
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ops_agent import agent
from ops_agent.models import Diagnosis, Severity


def _tu(tid, name, inp):
    return SimpleNamespace(type="tool_use", id=tid, name=name, input=inp)


def _resp(*blocks):
    return SimpleNamespace(content=list(blocks), stop_reason="tool_use")


class AgentLoopTest(unittest.TestCase):
    def setUp(self):
        os.environ["OPS_LOG_DIR"] = str(Path(__file__).resolve().parent.parent / "data")
        os.environ.pop("OPS_PG_DSN", None)  # query_pg 停在"未配 DSN",不真连库
        os.environ.pop("OPS_TRACE_DIR", None)

    @patch("ops_agent.agent.Anthropic")
    def test_multistep_read_then_query_then_report(self, anthropic_cls):
        create = anthropic_cls.return_value.messages.create
        create.side_effect = [
            _resp(_tu("a", "read_logs", {"service": "console", "pattern": "WARN"})),  # 步1
            _resp(_tu("b", "query_pg", {"sql": "select count(*) from batch.job_instance"})),  # 步2
            _resp(
                _tu(
                    "c",
                    agent.REPORT_TOOL_NAME,
                    {  # 步3:下结论
                        "severity": "CRITICAL",
                        "summary": "Redis 挂导致限流失效",
                        "root_cause": "16379 Redis 拒连",
                        "evidence": ["Connection refused"],
                        "suggested_action": "拉起 valkey",
                        "confidence": 0.6,
                    },
                ),
            ),
        ]

        result, messages = agent.run_agent("出什么事了?")

        self.assertIsInstance(result, Diagnosis)
        self.assertEqual(result.severity, Severity.CRITICAL)
        self.assertEqual(create.call_count, 3)  # 真的走了 3 步
        # 记忆累积:messages 里应同时有 read_logs 与 query_pg 的 tool_result
        flat = str(messages)
        self.assertIn("read_logs", flat)
        self.assertIn("OPS_PG_DSN", flat)  # query_pg 的执行结果(未配 DSN 文案)被喂回

    @patch("ops_agent.agent.Anthropic")
    def test_memory_carries_history(self, anthropic_cls):
        create = anthropic_cls.return_value.messages.create
        create.side_effect = [
            _resp(
                _tu(
                    "c",
                    agent.REPORT_TOOL_NAME,
                    {
                        "severity": "INFO",
                        "summary": "ok",
                        "root_cause": "无",
                        "evidence": [],
                        "suggested_action": "无",
                        "confidence": 0.9,
                    },
                )
            ),
        ]
        prior = [
            {"role": "user", "content": "上一轮问题"},
            {"role": "assistant", "content": "上一轮回答"},
        ]
        _, messages = agent.run_agent("接着问", history=prior)
        # 新 messages 应保留历史前缀(多轮记忆)
        self.assertEqual(messages[0]["content"], "上一轮问题")
        self.assertIn("接着问", str(messages))

    @patch("ops_agent.agent.Anthropic")
    def test_include_trace_records_tool_results(self, anthropic_cls):
        create = anthropic_cls.return_value.messages.create
        create.side_effect = [
            _resp(_tu("a", "read_logs", {"service": "console", "max_lines": 1})),
            _resp(
                _tu(
                    "b",
                    agent.REPORT_TOOL_NAME,
                    {
                        "severity": "INFO",
                        "summary": "ok",
                        "root_cause": "无",
                        "evidence": [],
                        "suggested_action": "无",
                        "confidence": 0.9,
                    },
                )
            ),
        ]

        result, _, trace = agent.run_agent("看日志", include_trace=True)

        self.assertIsInstance(result, Diagnosis)
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0].tool_name, "read_logs")
        self.assertTrue(trace[0].ok)
        self.assertEqual(trace[0].metadata["service"], "console")

    @patch("ops_agent.agent.Anthropic")
    def test_trace_dir_persists_jsonl(self, anthropic_cls):
        create = anthropic_cls.return_value.messages.create
        create.side_effect = [
            _resp(_tu("a", "read_logs", {"service": "console", "max_lines": 1})),
            _resp(
                _tu(
                    "b",
                    agent.REPORT_TOOL_NAME,
                    {
                        "severity": "INFO",
                        "summary": "ok",
                        "root_cause": "无",
                        "evidence": [],
                        "suggested_action": "无",
                        "confidence": 0.9,
                    },
                )
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPS_TRACE_DIR"] = tmp
            agent.run_agent("看日志")
            files = list(Path(tmp).glob("agent-trace-*.jsonl"))
            content = files[0].read_text(encoding="utf-8")

        self.assertEqual(len(files), 1)
        self.assertIn('"type": "diagnosis"', content)


if __name__ == "__main__":
    unittest.main()
