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

    @patch("ops_agent.agent.make_client")
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

    @patch("ops_agent.agent.make_client")
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

    @patch("ops_agent.agent.make_client")
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

    @patch("ops_agent.agent.make_client")
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

    @patch("ops_agent.agent.make_client")
    def test_trace_records_cumulative_token_usage(self, anthropic_cls):
        # 阶段4 承诺"trace/成本":trace 应记录跨步累计 token,便于"看哪步贵"。
        def _resp_usage(*blocks, in_tok, out_tok):
            return SimpleNamespace(
                content=list(blocks),
                stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
            )

        anthropic_cls.return_value.messages.create.side_effect = [
            _resp_usage(
                _tu("a", "read_logs", {"service": "console", "max_lines": 1}),
                in_tok=100,
                out_tok=20,
            ),
            _resp_usage(_tu("b", agent.REPORT_TOOL_NAME, _VALID_REPORT), in_tok=150, out_tok=30),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPS_TRACE_DIR"] = tmp
            agent.run_agent("看日志")
            content = next(Path(tmp).glob("agent-trace-*.jsonl")).read_text(encoding="utf-8")
        self.assertIn('"input_tokens": 250', content)  # 100 + 150
        self.assertIn('"output_tokens": 50', content)  # 20 + 30

    def test_extract_target_parses_server_prefix(self):
        self.assertEqual(agent._extract_target("[target=fbs] 为什么慢"), "fbs")
        self.assertEqual(agent._extract_target("[target=worker-import] x"), "worker-import")
        self.assertIsNone(agent._extract_target("没有前缀的问题"))

    @patch("ops_agent.agent.make_client")
    def test_tokens_recorded_even_when_run_fails_max_steps(self, anthropic_cls):
        # 回归:失控 run(绕圈撞 max_steps)的 token 也要计入累计指标,不是只成功路径计。
        from ops_agent.metrics import METRICS

        def _ru(*b, in_tok, out_tok):
            return SimpleNamespace(
                content=list(b),
                stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
            )

        anthropic_cls.return_value.messages.create.side_effect = [
            _ru(
                _tu(str(i), "read_logs", {"service": "console", "max_lines": 1}),
                in_tok=10,
                out_tok=5,
            )
            for i in range(3)
        ]
        before = METRICS.snapshot().get(("llm_input_tokens_total", ()), 0)
        with self.assertRaises(RuntimeError):
            agent.run_agent("绕圈不收口", max_steps=2)
        after = METRICS.snapshot().get(("llm_input_tokens_total", ()), 0)
        self.assertGreater(after, before)  # 失败 run 的 token 计入了


_VALID_REPORT = {
    "severity": "INFO",
    "summary": "ok",
    "root_cause": "无",
    "evidence": [],
    "suggested_action": "无",
    "confidence": 0.9,
}


def _tool_use_and_result_ids(messages):
    """提取所有 assistant tool_use 的 id 与所有 user tool_result 的 tool_use_id。"""
    use_ids, result_ids = set(), set()
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "tool_result":
                    result_ids.add(b["tool_use_id"])
            elif getattr(b, "type", None) == "tool_use":
                use_ids.add(b.id)
    return use_ids, result_ids


def _all_result_ids(messages):
    out = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                out.append(b["tool_use_id"])
    return out


class AgentToolResultProtocolTest(unittest.TestCase):
    """协议正确性:每个 tool_use 必须恰好对应一个 tool_result,否则下一轮 messages.create 会 400。"""

    def setUp(self):
        os.environ["OPS_LOG_DIR"] = str(Path(__file__).resolve().parent.parent / "data")
        os.environ.pop("OPS_TRACE_DIR", None)

    @patch("ops_agent.agent.make_client")
    def test_report_not_last_still_answers_every_tool_use(self, anthropic_cls):
        # 模型在同一轮把 report_diagnosis 放在非最后位置,另带一个工具块。
        # 旧实现处理完 report 直接 return,遗留兄弟 tool_use 无 tool_result(孤儿)。
        anthropic_cls.return_value.messages.create.side_effect = [
            _resp(
                _tu("r", agent.REPORT_TOOL_NAME, _VALID_REPORT),
                _tu("x", "read_logs", {"service": "console", "max_lines": 1}),
            )
        ]
        result, messages = agent.run_agent("q")
        self.assertIsInstance(result, Diagnosis)
        use_ids, result_ids = _tool_use_and_result_ids(messages)
        self.assertEqual(use_ids, result_ids)  # 无孤儿 tool_use

    @patch("ops_agent.agent.make_client")
    def test_invalid_then_valid_report_has_no_duplicate_results(self, anthropic_cls):
        # 不合规 report(confidence 越界)→ 回喂让模型修正;
        # 期间不能对同一 tool_use_id 出现两条 tool_result。
        bad = dict(_VALID_REPORT, confidence=9.9)
        anthropic_cls.return_value.messages.create.side_effect = [
            _resp(_tu("r1", agent.REPORT_TOOL_NAME, bad)),
            _resp(_tu("r2", agent.REPORT_TOOL_NAME, _VALID_REPORT)),
        ]
        result, messages = agent.run_agent("q")
        self.assertIsInstance(result, Diagnosis)  # 恢复成功
        all_ids = _all_result_ids(messages)
        self.assertEqual(len(all_ids), len(set(all_ids)))  # 无重复 tool_result
        use_ids, result_ids = _tool_use_and_result_ids(messages)
        self.assertEqual(use_ids, result_ids)

    @patch("ops_agent.agent.make_client")
    def test_tools_carry_ephemeral_cache_breakpoint(self, anthropic_cls):
        # 稳定的 tools→system 前缀在多步循环里跨轮重发,应打 ephemeral 缓存断点省 token。
        create = anthropic_cls.return_value.messages.create
        create.side_effect = [_resp(_tu("c", agent.REPORT_TOOL_NAME, _VALID_REPORT))]
        agent.run_agent("q")
        tools = create.call_args.kwargs["tools"]
        self.assertTrue(
            any(isinstance(t, dict) and t.get("cache_control") for t in tools),
            "工具列表应至少有一个 cache_control 断点",
        )

    @patch("ops_agent.agent.make_client")
    def test_raises_clear_error_on_max_tokens_truncation(self, anthropic_cls):
        # max_tokens 截断会产生不完整/部分 tool_use,旧实现要么静默绕圈要么 ValidationError 崩;
        # 应显式识别 stop_reason=="max_tokens" 并给可操作的报错。
        anthropic_cls.return_value.messages.create.side_effect = [
            SimpleNamespace(content=[], stop_reason="max_tokens")
        ]
        with self.assertRaises(RuntimeError) as ctx:
            agent.run_agent("q")
        self.assertIn("max_tokens", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
