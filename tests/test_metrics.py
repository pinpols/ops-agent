"""Metrics 自身指标单测:累加、Prometheus 渲染、原子 textfile。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ops_agent.metrics import Metrics


class MetricsTest(unittest.TestCase):
    def test_inc_accumulates(self):
        m = Metrics()
        m.inc("diagnose_started_total")
        m.inc("diagnose_started_total")
        m.inc("llm_input_tokens_total", 120)
        snap = m.snapshot()
        self.assertEqual(snap[("diagnose_started_total", ())], 2.0)
        self.assertEqual(snap[("llm_input_tokens_total", ())], 120.0)

    def test_labels_are_distinct_series(self):
        m = Metrics()
        m.inc("tool_calls_total", tool="read_logs", ok="true")
        m.inc("tool_calls_total", tool="read_logs", ok="true")
        m.inc("tool_calls_total", tool="query_pg", ok="false")
        text = m.render()
        self.assertIn('ops_agent_tool_calls_total{ok="true",tool="read_logs"} 2.0', text)
        self.assertIn('ops_agent_tool_calls_total{ok="false",tool="query_pg"} 1.0', text)
        # 每个 metric 名只出一次 TYPE 行
        self.assertEqual(text.count("# TYPE ops_agent_tool_calls_total counter"), 1)

    def test_render_empty(self):
        self.assertEqual(Metrics().render(), "")

    def test_write_textfile_atomic(self):
        m = Metrics()
        m.inc("diagnose_succeeded_total")
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "metrics.prom"
            m.write_textfile(path)  # 自动建父目录
            content = path.read_text(encoding="utf-8")
        self.assertIn("ops_agent_diagnose_succeeded_total 1.0", content)


if __name__ == "__main__":
    unittest.main()
