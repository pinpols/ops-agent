"""只读 Flink REST 工具:路径白名单(防写/防奇怪路径)+ GET-only 结构性只读 + 配置/解析。"""

import os
import unittest
from unittest.mock import MagicMock, patch

from ops_agent import flink_tools
from ops_agent.flink_tools import _path_allowed, query_flink_rest_result


class PathAllowlistTest(unittest.TestCase):
    def test_read_only_paths_allowed(self):
        for p in (
            "/overview",
            "/jobs",
            "/jobs/0a1b2c3d4e5f",
            "/jobs/0a1b2c3d/exceptions",
            "/jobs/0a1b2c3d/checkpoints",
            "/jobs/0a1b2c3d/vertices/abc123/backpressure",
            "/taskmanagers",
            "/taskmanagers/host:45123-abc/metrics",
            "/jobmanager/metrics",
        ):
            self.assertTrue(_path_allowed(p), p)

    def test_write_and_weird_paths_rejected(self):
        for p in (
            "/jobs/0a1b2c3d/savepoints",  # 触发 savepoint(写)
            "/jobs/0a1b2c3d/stop",  # 停作业(写)
            "/jobs/0a1b2c3d/rescaling",  # 改并行度(写)
            "/jars/upload",  # 上传 jar(写)
            "/jobs/../../etc/passwd",  # 路径穿越
            "/jobs/0a1b2c3d/exceptions?x=1",  # 查询串(白名单不含)
            "/overview ",  # 含空白
            "http://evil/jobs",  # 完整 URL 注入
        ):
            self.assertFalse(_path_allowed(p), p)


class QueryFlinkRestTest(unittest.TestCase):
    def setUp(self):
        for k in ("OPS_FLINK_URL", "OPS_TARGETS_FILE"):
            os.environ.pop(k, None)

    def test_not_configured_fails_clearly(self):
        r = query_flink_rest_result("/jobs")
        self.assertFalse(r.ok)
        self.assertIn("未配置 flink_url", r.to_text())

    def test_disallowed_path_never_hits_network(self):
        with (
            patch.dict(os.environ, {"OPS_FLINK_URL": "http://flink:8081"}),
            patch("ops_agent.flink_tools.urllib.request.urlopen") as urlopen,
        ):
            r = query_flink_rest_result("/jobs/abc/stop")  # 写操作路径
        urlopen.assert_not_called()  # 白名单先拦,不出网
        self.assertFalse(r.ok)
        self.assertIn("白名单", r.to_text())

    def test_happy_path_is_get_only(self):
        body = b'{"jobs":[{"id":"abc","status":"RUNNING"}]}'
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = body
        with (
            patch.dict(os.environ, {"OPS_FLINK_URL": "http://flink:8081/"}),
            patch("ops_agent.flink_tools.urllib.request.urlopen", return_value=cm),
            patch("ops_agent.flink_tools.urllib.request.Request") as Request,
        ):
            r = query_flink_rest_result("/jobs")
        self.assertTrue(r.ok)
        self.assertIn("RUNNING", r.to_text())
        # 结构性只读:Request 必须以 method=GET 构造
        self.assertEqual(Request.call_args.kwargs.get("method"), "GET")
        self.assertEqual(Request.call_args.args[0], "http://flink:8081/jobs")

    def test_caps_large_response(self):
        body = ("x" * (flink_tools._RESPONSE_CAP + 500)).encode()
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = body
        with (
            patch.dict(os.environ, {"OPS_FLINK_URL": "http://flink:8081"}),
            patch("ops_agent.flink_tools.urllib.request.urlopen", return_value=cm),
        ):
            r = query_flink_rest_result("/jobs")
        self.assertTrue(r.ok)
        self.assertTrue(r.metadata["truncated"])
        self.assertEqual(len(r.to_text().rstrip("\n").splitlines()[-1]), flink_tools._RESPONSE_CAP)

    def test_non_http_scheme_rejected(self):
        with patch.dict(os.environ, {"OPS_FLINK_URL": "file:///etc/passwd"}):
            r = query_flink_rest_result("/jobs")
        self.assertFalse(r.ok)
        self.assertIn("http(s)", r.to_text())


if __name__ == "__main__":
    unittest.main()
