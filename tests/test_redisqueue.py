"""Redis 队列后端单测(用 fakeredis,无需真 Redis):入队/查询/背压/重试/DLQ + worker 处理。"""

import os
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import fakeredis

from ops_agent import worker_main
from ops_agent.jobqueue import FAILED, QUEUED, RUNNING, SUCCEEDED
from ops_agent.redisqueue import RedisQueue
from ops_agent.worker_main import _touch_heartbeat, process_once


def _rq(max_queue: int = 100, max_retries: int = 2) -> RedisQueue:
    client = fakeredis.FakeRedis(decode_responses=True)
    return RedisQueue(client, max_queue=max_queue, max_retries=max_retries)


class RedisQueueTest(unittest.TestCase):
    def test_submit_stores_and_enqueues(self):
        rq = _rq()
        job = rq.submit("why slow", target="fbs", trace_id="trace-redis", actor="alice")
        self.assertIsNotNone(job)
        self.assertEqual(rq.qsize(), 1)
        loaded = rq.get(job.id)
        self.assertEqual(loaded.question, "why slow")
        self.assertEqual(loaded.trace_id, "trace-redis")
        self.assertEqual(loaded.target, "fbs")
        self.assertEqual(loaded.actor, "alice")
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

    def test_complete_does_not_overwrite_terminal_state(self):
        # 已 FAILED 的 job(被重复投递的另一 worker 写终态)不该被迟到的 complete 覆写回 SUCCEEDED
        rq = _rq(max_retries=0)
        job = rq.submit("q")
        rq.consume(timeout=1)
        self.assertEqual(rq.fail_or_retry(job.id, "boom"), "dead")
        self.assertEqual(rq.get(job.id).status, FAILED)
        rq.complete(job.id, {"late": True})  # 迟到的成功回写
        self.assertEqual(rq.get(job.id).status, FAILED)  # 终态守卫:不被覆写
        self.assertIsNone(rq.get(job.id).result)

    def test_fail_or_retry_does_not_resurrect_completed(self):
        # 已 SUCCEEDED 的 job 被迟到的 fail_or_retry 复活回 QUEUED → 重复诊断 + 状态翻转(回归)
        rq = _rq(max_retries=2)
        job = rq.submit("q")
        rq.consume(timeout=1)
        rq.complete(job.id, {"ok": True})
        self.assertEqual(rq.fail_or_retry(job.id, "late error"), "succeeded")
        self.assertEqual(rq.get(job.id).status, SUCCEEDED)  # 不复活
        self.assertEqual(rq.qsize(), 0)  # 没被重新入队
        self.assertEqual(rq.retry_size(), 0)

    def test_fail_or_retry_lost_when_hash_missing_no_zombie(self):
        # hash TTL 过期后 fail_or_retry:旧实现 hincrby 会重建无 TTL 僵尸;新实现判 lost、不复活
        from ops_agent.metrics import METRICS

        rq = _rq(max_retries=2)
        job = rq.submit("q")
        rq.consume(timeout=1)
        rq._r.delete(rq._job_key(job.id))  # 模拟 TTL 过期/驱逐
        before = METRICS.snapshot().get(("jobs_lost_total", ()), 0)
        self.assertEqual(rq.fail_or_retry(job.id, "boom"), "lost")
        after = METRICS.snapshot().get(("jobs_lost_total", ()), 0)
        self.assertEqual(after, before + 1)
        self.assertIsNone(rq.get(job.id))  # 没有被 hincrby 重建成僵尸
        self.assertEqual(rq.qsize(), 0)
        self.assertEqual(rq.retry_size(), 0)

    def test_consume_empty_returns_none(self):
        rq = _rq()
        self.assertIsNone(rq.consume(timeout=1))

    def test_ping_true_on_live_backend_false_on_error(self):
        rq = _rq()
        self.assertTrue(rq.ping())  # fakeredis 活着
        rq._r.close()

        class _Dead:
            def ping(self):
                raise ConnectionError("down")

        rq._r = _Dead()
        self.assertFalse(rq.ping())  # 连接异常视为未就绪

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

    def test_touch_heartbeat_creates_file_and_tolerates_none(self):
        _touch_heartbeat(None)  # 未配 → no-op,不报错
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / "sub" / "worker.hb"
            _touch_heartbeat(hb)  # 自动建父目录 + 落文件
            self.assertTrue(hb.exists())

    def test_lost_job_when_hash_missing_is_logged_and_counted(self):
        # 队列里有 id 但 hash 不在(TTL过期/驱逐)→ 不静默丢,计 jobs_lost_total
        from ops_agent.metrics import METRICS

        rq = _rq()
        rq._r.lpush(rq._queue_key, "ghost-id")  # 只入队 id,不建 hash
        before = METRICS.snapshot().get(("jobs_lost_total", ()), 0)
        self.assertIsNone(process_once(rq, lambda j: {}, timeout=1))
        after = METRICS.snapshot().get(("jobs_lost_total", ()), 0)
        self.assertEqual(after, before + 1)


class WorkerRunLifecycleTest(unittest.TestCase):
    """覆盖此前完全未测的运维契约:run() 信号注册 → 心跳 → 优雅停机 → 资源释放。"""

    def test_run_registers_signal_heartbeats_then_drains_and_closes(self):
        handlers: dict = {}
        fake_rq = MagicMock()
        fake_rq.consume.return_value = None  # 无任务,worker 空转

        with tempfile.TemporaryDirectory() as tmp:
            hb = os.path.join(tmp, "wk.hb")
            env = {
                "OPS_QUEUE_BACKEND": "redis",
                "OPS_REDIS_URL": "redis://x/0",
                "OPS_WORKER_COUNT": "1",
                "OPS_WORKER_HEARTBEAT_FILE": hb,
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(worker_main, "build_redis_queue", return_value=fake_rq),
                # signal.signal 只能在主线程注册;捕获而不真注册,以便在子线程跑 run()
                patch.object(worker_main.signal, "signal", lambda s, h: handlers.__setitem__(s, h)),
                patch("ops_agent.jobs.diagnosis_job_handler", lambda job: {}),
            ):
                t = threading.Thread(target=worker_main.run, daemon=True)
                t.start()
                deadline = time.time() + 3
                while time.time() < deadline and (
                    signal.SIGTERM not in handlers or not os.path.exists(hb)
                ):
                    time.sleep(0.02)
                self.assertIn(signal.SIGTERM, handlers)  # 注册了优雅停机
                self.assertTrue(os.path.exists(hb))  # 心跳被 touch(liveness 依赖)
                handlers[signal.SIGTERM](signal.SIGTERM, None)  # 触发 SIGTERM
                t.join(timeout=5)
                self.assertFalse(t.is_alive())  # run() 优雅退出
                fake_rq.close.assert_called_once()  # 资源释放


if __name__ == "__main__":
    unittest.main()
