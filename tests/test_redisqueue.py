"""Redis 队列后端单测(用 fakeredis,无需真 Redis):入队/查询/背压/重试/DLQ + worker 处理。"""

import unittest

import fakeredis

from ops_agent.jobqueue import FAILED, QUEUED, RUNNING, SUCCEEDED
from ops_agent.redisqueue import RedisQueue
from ops_agent.worker_main import process_once


def _rq(max_queue: int = 100, max_retries: int = 2) -> RedisQueue:
    client = fakeredis.FakeRedis(decode_responses=True)
    return RedisQueue(client, max_queue=max_queue, max_retries=max_retries)


class RedisQueueTest(unittest.TestCase):
    def test_submit_stores_and_enqueues(self):
        rq = _rq()
        job = rq.submit("why slow", target="fbs", trace_id="trace-redis")
        self.assertIsNotNone(job)
        self.assertEqual(rq.qsize(), 1)
        loaded = rq.get(job.id)
        self.assertEqual(loaded.question, "why slow")
        self.assertEqual(loaded.trace_id, "trace-redis")
        self.assertEqual(loaded.target, "fbs")
        self.assertEqual(loaded.status, QUEUED)

    def test_backpressure_returns_none_when_full(self):
        rq = _rq(max_queue=1)
        self.assertIsNotNone(rq.submit("1"))
        self.assertIsNone(rq.submit("2"))  # 队列满 → 背压

    def test_consume_and_complete(self):
        rq = _rq()
        job = rq.submit("q")
        got = rq.consume(timeout=1)
        self.assertEqual(got, job.id)
        rq.mark_running(job.id)
        self.assertEqual(rq.get(job.id).status, RUNNING)
        rq.complete(job.id, {"severity": "WARNING"})
        done = rq.get(job.id)
        self.assertEqual(done.status, SUCCEEDED)
        self.assertEqual(done.result, {"severity": "WARNING"})

    def test_retry_then_dlq(self):
        rq = _rq(max_retries=1)
        job = rq.submit("q")
        rq.consume(timeout=1)
        # 第 1 次失败:attempts=1 ≤ 1 → 进入 delayed retry zset,未到期不进主队列
        self.assertEqual(rq.fail_or_retry(job.id, "boom"), "retried")
        self.assertEqual(rq.get(job.id).status, QUEUED)
        self.assertEqual(rq.retry_size(), 1)
        self.assertEqual(rq.qsize(), 0)
        self.assertEqual(rq.promote_due_retries(now=10**12), 1)
        self.assertEqual(rq.qsize(), 1)
        rq.consume(timeout=1)
        # 第 2 次失败:attempts=2 > 1 → 死信
        self.assertEqual(rq.fail_or_retry(job.id, "boom again"), "dead")
        self.assertEqual(rq.get(job.id).status, FAILED)
        self.assertEqual(rq.dlq_size(), 1)
        self.assertIn(job.id, rq.dlq_list())

    def test_dlq_requeue(self):
        rq = _rq(max_retries=0)
        job = rq.submit("q")
        rq.consume(timeout=1)
        self.assertEqual(rq.fail_or_retry(job.id, "x"), "dead")  # max_retries=0 → 直接死信
        self.assertEqual(rq.dlq_size(), 1)
        self.assertTrue(rq.dlq_requeue(job.id))
        self.assertEqual(rq.dlq_size(), 0)
        self.assertEqual(rq.qsize(), 1)
        self.assertEqual(rq.get(job.id).status, QUEUED)
        self.assertEqual(rq.get(job.id).attempts, 0)  # 重置
        self.assertFalse(rq.dlq_requeue("nope"))  # 不存在

    def test_consume_empty_returns_none(self):
        rq = _rq()
        self.assertIsNone(rq.consume(timeout=1))

    def test_update_queue_metrics_emits_backlog_and_dlq_gauges(self):
        from ops_agent.metrics import METRICS

        rq = _rq(max_retries=0)
        job = rq.submit("q")
        rq.consume(timeout=1)
        rq.fail_or_retry(job.id, "x")  # max_retries=0 → 直接死信
        text = METRICS.render()
        self.assertIn("# TYPE ops_agent_dlq_size gauge", text)
        self.assertIn("# TYPE ops_agent_retry_backlog gauge", text)
        self.assertIn('ops_agent_dlq_size{backend="redis"} 1.0', text)


class WorkerProcessOnceTest(unittest.TestCase):
    def test_process_once_succeeds(self):
        rq = _rq()
        job = rq.submit("hello")
        outcome = process_once(rq, lambda j: {"echo": j.question}, timeout=1)
        self.assertEqual(outcome, "succeeded")
        done = rq.get(job.id)
        self.assertEqual(done.status, SUCCEEDED)
        self.assertEqual(done.result, {"echo": "hello"})

    def test_process_once_failure_goes_to_retry_or_dlq(self):
        rq = _rq(max_retries=0)
        job = rq.submit("hello")

        def boom(j):
            raise RuntimeError("nope")

        outcome = process_once(rq, boom, timeout=1)
        self.assertEqual(outcome, "dead")  # max_retries=0 → 直接死信
        self.assertEqual(rq.get(job.id).status, FAILED)
        self.assertEqual(rq.dlq_size(), 1)

    def test_process_once_records_duration_histogram(self):
        from ops_agent.metrics import METRICS

        rq = _rq()
        rq.submit("hello")
        process_once(rq, lambda j: {"ok": True}, timeout=1)
        text = METRICS.render()
        self.assertIn("# TYPE ops_agent_job_duration_seconds histogram", text)
        self.assertIn('ops_agent_job_duration_seconds_count{backend="redis"}', text)

    def test_process_once_empty_returns_none(self):
        rq = _rq()
        self.assertIsNone(process_once(rq, lambda j: {}, timeout=1))

    def test_lost_job_when_hash_missing_is_logged_and_counted(self):
        # 队列里有 id 但 hash 不在(TTL过期/驱逐)→ 不静默丢,计 jobs_lost_total
        from ops_agent.metrics import METRICS

        rq = _rq()
        rq._r.lpush(rq._queue_key, "ghost-id")  # 只入队 id,不建 hash
        before = METRICS.snapshot().get(("jobs_lost_total", ()), 0)
        self.assertIsNone(process_once(rq, lambda j: {}, timeout=1))
        after = METRICS.snapshot().get(("jobs_lost_total", ()), 0)
        self.assertEqual(after, before + 1)


if __name__ == "__main__":
    unittest.main()
