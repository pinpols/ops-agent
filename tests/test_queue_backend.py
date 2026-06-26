"""队列后端契约:两个具体后端满足 Protocol(防接口漂移)+ from_settings 工厂映射。"""

import os
import unittest
from unittest.mock import patch

import fakeredis

from ops_agent.config import Settings
from ops_agent.jobqueue import JobQueue
from ops_agent.queue_backend import IngressQueue, WorkerQueue
from ops_agent.redisqueue import RedisQueue


class QueueConformanceTest(unittest.TestCase):
    def test_jobqueue_satisfies_ingress(self):
        jq = JobQueue(lambda j: {}, workers=1)
        try:
            self.assertIsInstance(jq, IngressQueue)  # memory 后端 = 入口契约
        finally:
            jq.shutdown()

    def test_redisqueue_satisfies_worker_and_ingress(self):
        rq = RedisQueue(fakeredis.FakeRedis(decode_responses=True))
        self.assertIsInstance(rq, WorkerQueue)  # redis 后端 = 消费契约
        self.assertIsInstance(rq, IngressQueue)


class FromSettingsTest(unittest.TestCase):
    def test_rejects_non_redis_backend(self):
        with patch.dict(os.environ, {"OPS_QUEUE_BACKEND": "memory"}, clear=False):
            s = Settings.from_env()
        with self.assertRaises(ValueError):
            RedisQueue.from_settings(s)

    def test_maps_settings_to_queue(self):
        env = {
            "OPS_QUEUE_BACKEND": "redis",
            "OPS_REDIS_URL": "redis://localhost:6379/0",
            "OPS_QUEUE_MAX": "42",
            "OPS_MAX_RETRIES": "5",
        }
        with patch.dict(os.environ, env, clear=False):
            s = Settings.from_env()
            rq = RedisQueue.from_settings(s)  # redis.from_url 惰性,不真连
        self.assertEqual(rq._max_queue, 42)
        self.assertEqual(rq._max_retries, 5)


if __name__ == "__main__":
    unittest.main()
