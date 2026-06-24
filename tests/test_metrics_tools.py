"""query_metrics 工具单测:mock urlopen,验证成功解析 / 无配置 / Prometheus 报错 / 截断。"""

import io
import json
import unittest
from unittest.mock import patch

from ops_agent.metrics_tools import query_metrics_result
from ops_agent.targets import Target


def _fake_http(payload: dict):
    """伪造 urlopen 上下文管理器,返回 JSON bytes。"""

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp(json.dumps(payload).encode("utf-8"))


_TARGET = Target(name="t", root=None, log_dir="/x", metrics_url="http://prom:9090")  # type: ignore[arg-type]


class QueryMetricsTest(unittest.TestCase):
    def test_empty_promql_fails(self):
        r = query_metrics_result("  ")
        self.assertFalse(r.ok)
        self.assertIn("promql", r.error)

    def test_no_metrics_url_fails(self):
        no_url = Target(name="t", root=None, log_dir="/x", metrics_url=None)  # type: ignore[arg-type]
        with patch("ops_agent.metrics_tools.resolve_target", return_value=no_url):
            r = query_metrics_result("up")
        self.assertFalse(r.ok)
        self.assertIn("metrics_url", r.error)

    def test_non_http_base_rejected_before_open(self):
        evil = Target(name="t", root=None, log_dir="/x", metrics_url="file:///etc/passwd")  # type: ignore[arg-type]
        with (
            patch("ops_agent.metrics_tools.resolve_target", return_value=evil),
            patch("ops_agent.metrics_tools.urllib.request.urlopen") as urlopen,
        ):
            r = query_metrics_result("up")
        self.assertFalse(r.ok)
        self.assertIn("http(s)", r.error)
        urlopen.assert_not_called()  # 校验在 open 之前,绝不发起请求

    def test_success_parses_series(self):
        payload = {
            "status": "success",
            "data": {"result": [{"metric": {"job": "api"}, "value": [123, "1"]}]},
        }
        with (
            patch("ops_agent.metrics_tools.resolve_target", return_value=_TARGET),
            patch(
                "ops_agent.metrics_tools.urllib.request.urlopen", return_value=_fake_http(payload)
            ),
        ):
            r = query_metrics_result("up")
        self.assertTrue(r.ok)
        rows = json.loads(r.content)
        self.assertEqual(rows[0]["metric"], {"job": "api"})
        self.assertEqual(r.metadata["series"], 1)

    def test_prometheus_error_status_fails(self):
        payload = {"status": "error", "error": "bad query"}
        with (
            patch("ops_agent.metrics_tools.resolve_target", return_value=_TARGET),
            patch(
                "ops_agent.metrics_tools.urllib.request.urlopen", return_value=_fake_http(payload)
            ),
        ):
            r = query_metrics_result("up(")
        self.assertFalse(r.ok)
        self.assertIn("bad query", r.error)

    def test_truncates_to_cap(self):
        many = [{"metric": {"i": str(i)}, "value": [0, "1"]} for i in range(80)]
        payload = {"status": "success", "data": {"result": many}}
        with (
            patch("ops_agent.metrics_tools.resolve_target", return_value=_TARGET),
            patch(
                "ops_agent.metrics_tools.urllib.request.urlopen", return_value=_fake_http(payload)
            ),
        ):
            r = query_metrics_result("up")
        self.assertTrue(r.metadata["truncated"])
        self.assertEqual(len(json.loads(r.content)), 50)


if __name__ == "__main__":
    unittest.main()
