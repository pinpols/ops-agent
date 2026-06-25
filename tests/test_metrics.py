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

    def test_set_renders_gauge(self):
        m = Metrics()
        m.set("queue_depth", 7, backend="memory")
        text = m.render()
        self.assertIn("# TYPE ops_agent_queue_depth gauge", text)
        self.assertIn('ops_agent_queue_depth{backend="memory"} 7.0', text)

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

    def test_add_is_gauge_delta(self):
        # add 支持相对增减(inc/dec),类型是 gauge —— 用于 workers_busy 这类在途计数
        m = Metrics()
        m.add("workers_busy", 1, backend="redis")
        m.add("workers_busy", 1, backend="redis")
        m.add("workers_busy", -1, backend="redis")
        text = m.render()
        self.assertIn("# TYPE ops_agent_workers_busy gauge", text)
        self.assertIn('ops_agent_workers_busy{backend="redis"} 1.0', text)

    def test_observe_renders_histogram(self):
        m = Metrics()
        for v in (0.03, 0.4, 3.0):
            m.observe("job_duration_seconds", v, buckets=(0.1, 1.0, 5.0))
        text = m.render()
        self.assertIn("# TYPE ops_agent_job_duration_seconds histogram", text)
        # 累计桶:le=0.1 含 1 个(0.03),le=1.0 含 2 个(+0.4),le=5.0 与 +Inf 含全部 3 个
        self.assertIn('ops_agent_job_duration_seconds_bucket{le="0.1"} 1.0', text)
        self.assertIn('ops_agent_job_duration_seconds_bucket{le="1.0"} 2.0', text)
        self.assertIn('ops_agent_job_duration_seconds_bucket{le="5.0"} 3.0', text)
        self.assertIn('ops_agent_job_duration_seconds_bucket{le="+Inf"} 3.0', text)
        self.assertIn("ops_agent_job_duration_seconds_count 3.0", text)
        self.assertIn("ops_agent_job_duration_seconds_sum 3.43", text)

    def test_observe_with_labels(self):
        m = Metrics()
        m.observe("job_duration_seconds", 0.2, buckets=(0.1, 1.0), backend="memory")
        text = m.render()
        self.assertIn('ops_agent_job_duration_seconds_bucket{backend="memory",le="0.1"} 0.0', text)
        self.assertIn('ops_agent_job_duration_seconds_bucket{backend="memory",le="1.0"} 1.0', text)
        self.assertIn('ops_agent_job_duration_seconds_count{backend="memory"} 1.0', text)

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
