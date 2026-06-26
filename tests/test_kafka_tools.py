"""只读 Kafka REST 工具:路径白名单(防写/防奇怪路径)+ GET-only 结构性只读 + 配置/解析。"""

import os
import unittest
from unittest.mock import MagicMock, patch

from ops_agent.kafka_tools import _path_allowed, query_kafka_rest_result


class PathAllowlistTest(unittest.TestCase):
    def test_read_only_paths_allowed(self):
        c = "/v3/clusters/lkc-abc123"
        for p in (
            "/v3/clusters",
            c,
            f"{c}/brokers",
            f"{c}/brokers/1",
            f"{c}/topics",
            f"{c}/topics/orders",
            f"{c}/topics/orders/partitions",
            f"{c}/topics/orders/partitions/3",
            f"{c}/topics/orders/partitions/3/replicas",
            f"{c}/consumer-groups",
            f"{c}/consumer-groups/flink-orders",
            f"{c}/consumer-groups/flink-orders/lags",
            f"{c}/consumer-groups/flink-orders/lag-summary",
        ):
            self.assertTrue(_path_allowed(p), p)

    def test_write_and_weird_paths_rejected(self):
        c = "/v3/clusters/lkc-abc123"
        for p in (
            f"{c}/topics/orders/configs:alter",  # 改配置(写)
            f"{c}/acls",  # ACL(非白名单)
            f"{c}/topics/../../etc/passwd",  # 路径穿越
            f"{c}/topics?x=1",  # 查询串
            "http://evil/v3/clusters",  # 完整 URL 注入
            "/v3/clusters/lkc/topics ",  # 含空白
        ):
            self.assertFalse(_path_allowed(p), p)


class QueryKafkaRestTest(unittest.TestCase):
    def setUp(self):
        for k in ("OPS_KAFKA_REST_URL", "OPS_TARGETS_FILE"):
            os.environ.pop(k, None)

    def test_not_configured_fails_clearly(self):
        r = query_kafka_rest_result("/v3/clusters")
        self.assertFalse(r.ok)
        self.assertIn("未配置 kafka_rest_url", r.to_text())

    def test_disallowed_path_never_hits_network(self):
        with (
            patch.dict(os.environ, {"OPS_KAFKA_REST_URL": "http://kafka-rest:8082"}),
            patch("ops_agent.rest_tools.urllib.request.urlopen") as urlopen,
        ):
            r = query_kafka_rest_result("/v3/clusters/lkc/acls")  # 非白名单
        urlopen.assert_not_called()
        self.assertFalse(r.ok)
        self.assertIn("白名单", r.to_text())

    def test_happy_path_is_get_only(self):
        body = b'{"data":[{"cluster_id":"lkc-abc","controller":{"broker_id":1}}]}'
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = body
        with (
            patch.dict(os.environ, {"OPS_KAFKA_REST_URL": "http://kafka-rest:8082/"}),
            patch("ops_agent.rest_tools.urllib.request.urlopen", return_value=cm),
            patch("ops_agent.rest_tools.urllib.request.Request") as Request,
        ):
            r = query_kafka_rest_result("/v3/clusters")
        self.assertTrue(r.ok)
        self.assertIn("lkc-abc", r.to_text())
        self.assertEqual(Request.call_args.kwargs.get("method"), "GET")  # 结构性只读
        self.assertEqual(Request.call_args.args[0], "http://kafka-rest:8082/v3/clusters")


if __name__ == "__main__":
    unittest.main()
