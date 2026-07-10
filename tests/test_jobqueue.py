"""进程内任务队列单测:异步处理 / 失败标记 / 背压 / 查询。"""

import threading
import time
import unittest
from unittest.mock import patch

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
            job = jq.submit("hello", target="fbs", trace_id="trace-1", actor="alice")
            self.assertIsNotNone(job)
            done = _wait(jq, job.id)
            self.assertEqual(done.status, SUCCEEDED)
            self.assertEqual(done.result, {"echo": "hello"})
            self.assertEqual(done.target, "fbs")
            self.assertEqual(done.trace_id, "trace-1")
            self.assertEqual(done.actor, "alice")
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

    def test_final_failure_posts_single_failed_callback(self):
        # P2-3:failed 回调只在终局投递一次(重试中不投,payload 可带最终 attempts)
        calls = []

        def boom(job):
            raise RuntimeError("nope")

        with patch(
            "ops_agent.callback._post_callback",
            side_effect=lambda job, **kw: calls.append((job, kw)),
        ):
            jq = JobQueue(boom, workers=1, max_retries=1, retry_base_seconds=0)
            try:
                job = jq.submit("x")
                done = _wait(jq, job.id)
                self.assertEqual(done.status, FAILED)
                deadline = time.time() + 2
                while time.time() < deadline and not calls:
                    time.sleep(0.01)
            finally:
                jq.shutdown()
        failed = [kw for _job, kw in calls if kw.get("status") == "failed"]
        self.assertEqual(len(failed), 1)  # 共 2 次尝试,只在终局回调 1 次
        self.assertEqual(calls[0][0].attempts, 2)

    def test_success_posts_succeeded_callback_after_terminal_write(self):
        # P1-3:succeeded 回调由队列层在状态写成 SUCCEEDED 之后投递(与 redis 后端一致),
        # 不再由 handler 提前发出。
        calls = []
        with patch(
            "ops_agent.callback._post_callback",
            side_effect=lambda job, **kw: calls.append((job, kw)),
        ):
            jq = JobQueue(lambda job: {"ok": True}, workers=1)
            try:
                job = jq.submit("x")
                done = _wait(jq, job.id)
                self.assertEqual(done.status, SUCCEEDED)
                deadline = time.time() + 2
                while time.time() < deadline and not calls:
                    time.sleep(0.01)
            finally:
                jq.shutdown()
        succeeded = [kw for _job, kw in calls if kw.get("status") == "succeeded"]
        self.assertEqual(len(succeeded), 1)
        self.assertEqual(succeeded[0]["result"], {"ok": True})

    def test_non_retryable_exception_fails_without_retries(self):
        # P2-4:BudgetExceeded 等确定性失败不重试,attempts 停在 1
        from ops_agent.budget import BudgetExceeded

        def boom(job):
            raise BudgetExceeded("token 预算耗尽")

        jq = JobQueue(boom, workers=1, max_retries=5, retry_base_seconds=0)
        try:
            job = jq.submit("x")
            done = _wait(jq, job.id)
            self.assertEqual(done.status, FAILED)
            self.assertEqual(done.attempts, 1)
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
            self.assertIn("actor", pub)
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


class MemoryDeadPathCallbackTest(unittest.TestCase):
    """P2-6:memory 后端三条 dead 路径(重试入队满 / 停机竞态 / shutdown 取消 Timer)
    此前不发终局 failed 回调 → 下游等不到失败通知。与 redis 后端对齐。"""

    def test_retry_queue_full_posts_failed_callback(self):
        calls = []
        gate = threading.Event()

        def slow(job):
            gate.wait(5)
            return {}

        with patch(
            "ops_agent.callback._post_callback",
            side_effect=lambda job, **kw: calls.append((job, kw)),
        ):
            # workers=1 被 gate 占住 → 队列(max=1)被 b 填满,确定性触发 queue.Full
            jq = JobQueue(slow, workers=1, max_queue=1)
            try:
                a = jq.submit("a")
                deadline = time.time() + 3
                while time.time() < deadline and jq.get(a.id).status != "running":
                    time.sleep(0.01)
                b = jq.submit("b")  # 占满队列
                self.assertEqual(jq.qsize(), 1)
                jq._requeue(b.id)  # 重试入队 → 队列已满 → 终局 FAILED + 回调
            finally:
                gate.set()
                jq.shutdown()
        self.assertEqual(jq.get(b.id).error, "retry_queue_full")
        failed = [kw for _job, kw in calls if kw.get("status") == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("retry_queue_full", failed[0].get("error") or "")

    def test_shutdown_cancelled_retry_posts_failed_callback(self):
        calls = []

        def boom(job):
            raise RuntimeError("nope")

        with patch(
            "ops_agent.callback._post_callback",
            side_effect=lambda job, **kw: calls.append((job, kw)),
        ):
            # 大 retry 延迟:Timer 挂起时 shutdown → 取消并标终态,必须发终局 failed
            jq = JobQueue(boom, workers=1, max_retries=1, retry_base_seconds=60)
            job = jq.submit("x")
            deadline = time.time() + 3
            while time.time() < deadline and not jq._timers:
                time.sleep(0.01)
            jq.shutdown()
        self.assertEqual(jq.get(job.id).status, FAILED)
        self.assertEqual(jq.get(job.id).error, "shutdown_retry_cancelled")
        failed = [kw for _job, kw in calls if kw.get("status") == "failed"]
        self.assertEqual(len(failed), 1)

    def test_stop_race_requeue_after_delay_posts_failed_callback(self):
        calls = []
        with patch(
            "ops_agent.callback._post_callback",
            side_effect=lambda job, **kw: calls.append((job, kw)),
        ):
            jq = JobQueue(lambda job: {}, workers=1)
            try:
                job = jq.submit("x")
                _wait(jq, job.id)
                jq.get(job.id).status = QUEUED  # 摆回非终态,模拟重试中
                jq._stop.set()  # 停机竞态窗:排 Timer 前 stop 已置位
                jq._requeue_after_delay(job.id, 60)
            finally:
                jq._stop.clear()
                jq.shutdown()
        self.assertEqual(jq.get(job.id).status, FAILED)
        failed = [kw for _job, kw in calls if kw.get("status") == "failed"]
        self.assertEqual(len(failed), 1)

    def test_mark_failed_locked_is_idempotent_no_double_callback(self):
        # 已终态的任务再走 dead 路径不得二次回调/二次计数
        calls = []
        with patch(
            "ops_agent.callback._post_callback",
            side_effect=lambda job, **kw: calls.append((job, kw)),
        ):
            jq = JobQueue(lambda job: {}, workers=1)
            try:
                job = jq.submit("x")
                _wait(jq, job.id)  # SUCCEEDED
                jq._stop.set()
                jq._requeue_after_delay(job.id, 60)  # 已终态 → no-op
            finally:
                jq._stop.clear()
                jq.shutdown()
        self.assertEqual(jq.get(job.id).status, SUCCEEDED)
        failed = [kw for _job, kw in calls if kw.get("status") == "failed"]
        self.assertEqual(failed, [])


class ServerShutdownWindowTest(unittest.TestCase):
    """P2-6:memory 后端 shutdown join 默认 10s 会截断在途诊断(可长至 max_run);
    server 停机须传 max_run+10 的排空窗口,与 redis worker 一致。"""

    def test_close_job_queue_passes_run_budget_window(self):
        from unittest.mock import MagicMock

        from ops_agent.config import Settings
        from ops_agent.server import _close_job_queue

        settings = MagicMock(spec=Settings)
        settings.ops_max_run_seconds = 120.0
        jq = MagicMock(spec=["shutdown"])
        _close_job_queue(jq, settings)
        jq.shutdown.assert_called_once_with(timeout=130.0)

    def test_close_job_queue_falls_back_to_close_for_redis(self):
        from unittest.mock import MagicMock

        from ops_agent.server import _close_job_queue

        rq = MagicMock(spec=["close"])
        _close_job_queue(rq, MagicMock())
        rq.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
