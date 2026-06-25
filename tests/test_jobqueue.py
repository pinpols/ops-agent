"""进程内任务队列单测:异步处理 / 失败标记 / 背压 / 查询。"""

import threading
import time
import unittest

from ops_agent.jobqueue import FAILED, QUEUED, SUCCEEDED, JobQueue


def _wait(jq: JobQueue, job_id: str, timeout: float = 3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = jq.get(job_id)
        if job is not None and job.status in (SUCCEEDED, FAILED):
            return job
        time.sleep(0.01)
    return jq.get(job_id)


class JobQueueTest(unittest.TestCase):
    def test_processes_job_to_succeeded(self):
        jq = JobQueue(lambda job: {"echo": job.question}, workers=1)
        try:
            job = jq.submit("hello", target="fbs", trace_id="trace-1")
            self.assertIsNotNone(job)
            done = _wait(jq, job.id)
            self.assertEqual(done.status, SUCCEEDED)
            self.assertEqual(done.result, {"echo": "hello"})
            self.assertEqual(done.target, "fbs")
            self.assertEqual(done.trace_id, "trace-1")
        finally:
            jq.shutdown()

    def test_handler_exception_marks_failed(self):
        def boom(job):
            raise RuntimeError("nope")

        jq = JobQueue(boom, workers=1)
        try:
            job = jq.submit("x")
            done = _wait(jq, job.id)
            self.assertEqual(done.status, FAILED)
            self.assertIn("nope", done.error)
        finally:
            jq.shutdown()

    def test_backpressure_returns_none_when_full(self):
        # 阻塞 handler 占住唯一 worker;队列(max=1)塞满后 submit 返回 None
        gate = threading.Event()

        def slow(job):
            gate.wait(5)
            return {}

        jq = JobQueue(slow, workers=1, max_queue=1)
        try:
            jq.submit("1")  # worker 取走开始跑(阻塞在 gate)
            time.sleep(0.15)
            j2 = jq.submit("2")  # 入队占满 max_queue=1
            j3 = jq.submit("3")  # 队列满 → 背压 → None
            self.assertIsNotNone(j2)
            self.assertIsNone(j3)
        finally:
            gate.set()
            jq.shutdown()

    def test_get_unknown_returns_none(self):
        jq = JobQueue(lambda job: {}, workers=1)
        try:
            self.assertIsNone(jq.get("does-not-exist"))
        finally:
            jq.shutdown()

    def test_to_public_shape(self):
        jq = JobQueue(lambda job: {"ok": True}, workers=1)
        try:
            job = jq.submit("q", target="t")
            done = _wait(jq, job.id)
            pub = done.to_public()
            self.assertEqual(pub["job_id"], job.id)
            self.assertEqual(pub["trace_id"], job.trace_id)
            self.assertEqual(pub["status"], SUCCEEDED)
            self.assertEqual(pub["result"], {"ok": True})
            self.assertIn("created_at", pub)
        finally:
            jq.shutdown()

    def test_retry_uses_backoff_then_succeeds(self):
        seen = 0

        def flaky(job):
            nonlocal seen
            seen += 1
            if seen == 1:
                raise RuntimeError("try again")
            return {"ok": True}

        jq = JobQueue(flaky, workers=1, max_retries=1, retry_base_seconds=0.01)
        try:
            job = jq.submit("q", trace_id="trace-retry")
            done = _wait(jq, job.id)
            self.assertEqual(done.status, SUCCEEDED)
            self.assertEqual(done.attempts, 1)
            self.assertEqual(done.result, {"ok": True})
            self.assertEqual(done.trace_id, "trace-retry")
        finally:
            jq.shutdown()

    def test_shutdown_during_retry_delay_marks_failed_not_stuck_queued(self):
        # 任务失败进入重试延迟(Timer 未触发)时停机:必须取消 Timer 并把任务标终态 FAILED,
        # 否则任务永久卡在 QUEUED(查询端点看不到终态)且 Timer 作为 daemon 静默丢失。
        def always_fail(job):
            raise RuntimeError("boom")

        # retry_base_seconds 很大 → 第一次失败后 Timer 排期但远不触发,停机时它一定还挂着
        jq = JobQueue(always_fail, workers=1, max_retries=3, retry_base_seconds=30)
        job = jq.submit("q")

        # 等到 handler 跑过一次、任务落入重试态(QUEUED + attempts≥1 + Timer pending)
        deadline = time.time() + 3
        while time.time() < deadline:
            j = jq.get(job.id)
            if j is not None and j.attempts >= 1 and j.status == QUEUED:
                break
            time.sleep(0.01)
        self.assertEqual(jq.get(job.id).status, QUEUED)  # 确认处于重试延迟中

        jq.shutdown(timeout=2)

        final = jq.get(job.id)
        self.assertEqual(final.status, FAILED)  # 不再卡 QUEUED
        self.assertEqual(final.error, "shutdown_retry_cancelled")
        # 已取消所有挂起 Timer,无泄漏
        self.assertEqual(len(jq._timers), 0)


if __name__ == "__main__":
    unittest.main()
