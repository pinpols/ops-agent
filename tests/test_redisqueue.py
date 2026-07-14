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
        job = rq.submit(
            "why slow",
            target="fbs",
            trace_id="trace-redis",
            actor="alice",
            event_id="alert/redis-1",
        )
        self.assertIsNotNone(job)
        self.assertEqual(rq.qsize(), 1)
        loaded = rq.get(job.id)
        self.assertEqual(loaded.question, "why slow")
        self.assertEqual(loaded.trace_id, "trace-redis")
        self.assertEqual(loaded.target, "fbs")
        self.assertEqual(loaded.actor, "alice")
        self.assertEqual(loaded.event_id, "alert/redis-1")
        self.assertEqual(loaded.status, QUEUED)

    def test_submit_deduplicates_event_id(self):
        rq = _rq()
        first = rq.submit("why slow", event_id="alert/redis-dup")
        second = rq.submit("why slow again", event_id="alert/redis-dup")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first.id, second.id)
        self.assertEqual(rq.qsize(), 1)

    def test_backpressure_counts_retry_backlog(self):
        rq = _rq(max_queue=1, max_retries=1)
        job = rq.submit("q")
        self.assertIsNotNone(job)
        rq.consume(timeout=1)
        self.assertEqual(rq.fail_or_retry(job.id, "boom"), "retried")
        self.assertEqual(rq.qsize(), 0)
        self.assertEqual(rq.retry_size(), 1)
        self.assertIsNone(rq.submit("new"))  # retry backlog 已占满全局容量

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
        self.assertIn("# TYPE ops_agent_queue_backlog_total gauge", text)
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


class CrashSafeConsumeTest(unittest.TestCase):
    """P1-1:consume 不再 BRPOP 破坏性出队,而是 LMOVE 到 per-worker processing list;
    worker 硬崩(SIGKILL/OOM)后 reaper 从 processing 残留 + RUNNING 残留两处回收,不丢任务。"""

    def _pair(self, **kw):
        client = fakeredis.FakeRedis(decode_responses=True)
        return client, RedisQueue(client, worker_id="w1", **kw)

    def test_consume_moves_to_processing_list(self):
        client, rq = self._pair()
        job = rq.submit("q")
        got = rq.consume(timeout=1)
        self.assertEqual(got, job.id)
        self.assertEqual(rq.qsize(), 0)
        # 出队即挂到本 worker 的 processing list(在途登记,崩溃可追溯)
        self.assertEqual(client.lrange("ops:queue:processing:w1", 0, -1), [job.id])

    def test_complete_removes_from_processing_list(self):
        client, rq = self._pair()
        job = rq.submit("q")
        rq.consume(timeout=1)
        rq.mark_running(job.id)
        rq.complete(job.id, {"ok": True})
        self.assertEqual(client.llen("ops:queue:processing:w1"), 0)

    def test_fail_or_retry_removes_from_processing_list(self):
        client, rq = self._pair(max_retries=1)
        job = rq.submit("q")
        rq.consume(timeout=1)
        rq.mark_running(job.id)
        self.assertEqual(rq.fail_or_retry(job.id, "boom"), "retried")
        self.assertEqual(client.llen("ops:queue:processing:w1"), 0)

    def test_mark_running_records_running_since_and_worker(self):
        client, rq = self._pair()
        job = rq.submit("q")
        rq.consume(timeout=1)
        self.assertTrue(rq.mark_running(job.id))
        h = client.hgetall(rq._job_key(job.id))
        self.assertEqual(h["status"], RUNNING)
        self.assertEqual(h["worker"], "w1")
        self.assertGreater(float(h["running_since"]), 0)

    def test_reaper_requeues_inflight_jobs_of_dead_worker(self):
        client = fakeredis.FakeRedis(decode_responses=True)
        w1 = RedisQueue(client, worker_id="w1", max_retries=2)
        job = w1.submit("q")
        w1.consume(timeout=1)
        w1.mark_running(job.id)
        # w1 硬崩:没有 complete/fail,processing/RUNNING 双残留;w2(新 worker)启动后 reap
        w2 = RedisQueue(client, worker_id="w2", max_retries=2)
        stats = w2.reap(now=time.time() + 3600)  # 远超 worker_dead_after → w1 判死
        self.assertEqual(stats["requeued"], 1)
        self.assertEqual(client.llen("ops:queue:processing:w1"), 0)
        j = w2.get(job.id)
        self.assertEqual(j.status, QUEUED)  # 回灌走 fail_or_retry:计入 attempts
        self.assertEqual(j.attempts, 1)
        self.assertEqual(w2.retry_size() + w2.qsize(), 1)  # 任务守恒:在 retry zset 或主队列

    def test_reaper_spares_alive_worker(self):
        client = fakeredis.FakeRedis(decode_responses=True)
        w1 = RedisQueue(client, worker_id="w1")
        job = w1.submit("q")
        w1.consume(timeout=1)  # consume 自带心跳
        w1.mark_running(job.id)
        w2 = RedisQueue(client, worker_id="w2")
        stats = w2.reap(now=time.time())  # w1 心跳新鲜 → 不动它的在途任务
        self.assertEqual(stats["requeued"], 0)
        self.assertEqual(client.lrange("ops:queue:processing:w1", 0, -1), [job.id])
        self.assertEqual(w2.get(job.id).status, RUNNING)

    def test_reaper_recovers_stale_running_job_even_if_worker_alive(self):
        # 心跳还在(进程活着)但单个任务卡死超 stale_running_seconds → 走 fail_or_retry 兜底
        client = fakeredis.FakeRedis(decode_responses=True)
        rq = RedisQueue(client, worker_id="w1", max_retries=2, stale_running_seconds=10)
        job = rq.submit("q")
        rq.consume(timeout=1)
        rq.mark_running(job.id)
        client.hset(rq._job_key(job.id), "running_since", str(time.time() - 3600))
        stats = rq.reap(now=time.time())
        self.assertEqual(stats["requeued"], 1)
        self.assertEqual(rq.get(job.id).status, QUEUED)
        self.assertEqual(client.llen("ops:queue:processing:w1"), 0)  # 残留同步清掉

    def test_reaper_counts_lost_for_ghost_id_in_dead_processing_list(self):
        from ops_agent.metrics import METRICS

        client = fakeredis.FakeRedis(decode_responses=True)
        client.lpush("ops:queue:processing:dead-worker", "ghost")
        rq = RedisQueue(client, worker_id="w2")
        before = METRICS.snapshot().get(("jobs_lost_total", ()), 0)
        stats = rq.reap(now=time.time() + 3600)
        self.assertEqual(stats["lost"], 1)
        after = METRICS.snapshot().get(("jobs_lost_total", ()), 0)
        self.assertEqual(after, before + 1)
        self.assertEqual(client.llen("ops:queue:processing:dead-worker"), 0)

    def test_discard_removes_ghost_from_processing_list(self):
        client, rq = self._pair()
        client.lpush(rq._queue_key, "ghost")
        rq.consume(timeout=1)
        self.assertEqual(client.llen("ops:queue:processing:w1"), 1)
        rq.discard("ghost")
        self.assertEqual(client.llen("ops:queue:processing:w1"), 0)


class MarkRunningTerminalGuardTest(unittest.TestCase):
    """P1-2:job 被 reaper 抢走重入队、原 worker 已写 SUCCEEDED 后,第二个 worker 取到
    同 id 时 mark_running 不得把终态改回 RUNNING 重跑。"""

    def test_mark_running_refuses_succeeded_job(self):
        client = fakeredis.FakeRedis(decode_responses=True)
        rq = RedisQueue(client, worker_id="w1")
        job = rq.submit("q")
        rq.consume(timeout=1)
        rq.mark_running(job.id)
        rq.complete(job.id, {"ok": True})
        # 重复投递:同 id 再次被消费(reaper 抢走后重入队的迟到副本)
        client.lpush(rq._queue_key, job.id)
        got = rq.consume(timeout=1)
        self.assertEqual(got, job.id)
        self.assertFalse(rq.mark_running(job.id))  # 终态不回翻
        self.assertEqual(rq.get(job.id).status, SUCCEEDED)
        self.assertEqual(client.llen("ops:queue:processing:w1"), 0)  # 在途登记同步摘除

    def test_mark_running_refuses_failed_job(self):
        client = fakeredis.FakeRedis(decode_responses=True)
        rq = RedisQueue(client, worker_id="w1", max_retries=0)
        job = rq.submit("q")
        rq.consume(timeout=1)
        rq.fail_or_retry(job.id, "boom")  # 直接终局 FAILED
        client.lpush(rq._queue_key, job.id)
        rq.consume(timeout=1)
        self.assertFalse(rq.mark_running(job.id))
        self.assertEqual(rq.get(job.id).status, FAILED)
        self.assertEqual(client.llen("ops:queue:processing:w1"), 0)

    def test_process_once_skips_terminal_job_without_running_handler(self):
        client = fakeredis.FakeRedis(decode_responses=True)
        rq = RedisQueue(client, worker_id="w1")
        job = rq.submit("q")
        rq.consume(timeout=1)
        rq.complete(job.id, {"ok": True})
        client.lpush(rq._queue_key, job.id)
        calls = []
        self.assertIsNone(process_once(rq, lambda j: calls.append(j) or {}, timeout=1))
        self.assertEqual(calls, [])  # handler 没被重跑
        self.assertEqual(rq.get(job.id).result, {"ok": True})  # 结果没被覆写


class SucceededCallbackOrderingTest(unittest.TestCase):
    """P1-3:succeeded 回调必须在 complete 确认"本方是第一个终态写入者"之后才投递;
    complete 被终态守卫拒绝(如 reaper 已判 FAILED)时,绝不能给下游发 succeeded。"""

    def test_complete_returns_true_only_on_first_terminal_write(self):
        rq = _rq()
        job = rq.submit("q")
        rq.consume(timeout=1)
        rq.mark_running(job.id)
        self.assertTrue(rq.complete(job.id, {"ok": True}))
        self.assertFalse(rq.complete(job.id, {"ok": 2}))  # 已终态 → 拒绝

    def test_complete_returns_false_when_already_failed(self):
        rq = _rq(max_retries=0)
        job = rq.submit("q")
        rq.consume(timeout=1)
        rq.mark_running(job.id)
        rq.fail_or_retry(job.id, "reaper 判死")  # 另一方先写了 FAILED
        self.assertFalse(rq.complete(job.id, {"ok": True}))
        self.assertEqual(rq.get(job.id).status, FAILED)  # 不覆写

    def test_succeeded_callback_after_complete_wins(self):
        rq = _rq()
        job = rq.submit("q")
        with patch("ops_agent.callback._post_callback") as cb:
            outcome = process_once(rq, lambda j: {"echo": j.question}, timeout=1)
        self.assertEqual(outcome, "succeeded")
        succeeded = [c for c in cb.call_args_list if c.kwargs.get("status") == "succeeded"]
        self.assertEqual(len(succeeded), 1)
        self.assertEqual(succeeded[0].args[0].id, job.id)

    def test_no_succeeded_callback_when_complete_rejected(self):
        # handler 跑完前任务已被另一方判 FAILED(reaper 抢走后终局)→ 不发 succeeded
        rq = _rq(max_retries=0)
        job = rq.submit("q")

        def handler(j):
            # 模拟 handler 长跑期间另一方(reaper/另一 worker)把任务判成 FAILED
            rq.fail_or_retry(j.id, "stale_running_timeout")
            return {"ok": True}

        with patch("ops_agent.callback._post_callback") as cb:
            process_once(rq, handler, timeout=1)
        succeeded = [c for c in cb.call_args_list if c.kwargs.get("status") == "succeeded"]
        self.assertEqual(succeeded, [])  # 乱序终态信号被堵住
        self.assertEqual(rq.get(job.id).status, FAILED)


class ReaperHeartbeatRaceTest(unittest.TestCase):
    """P0-1:心跳模型 —— 忙 worker(心跳持续续、但不 consume)不得被判死;
    reaper 判死后回收前须二次确认心跳仍 stale,drain 中途心跳复活要立刻停手。"""

    def test_busy_worker_with_fresh_heartbeat_not_reaped(self):
        # 模拟忙 worker:handler 长跑期间不 consume,但独立心跳线程持续续心跳
        client = fakeredis.FakeRedis(decode_responses=True)
        w1 = RedisQueue(client, worker_id="w1", max_retries=2)
        job = w1.submit("q")
        w1.consume(timeout=1)
        w1.mark_running(job.id)
        w1.heartbeat()  # 心跳线程在续(即便 consume 不再被调)
        w2 = RedisQueue(client, worker_id="w2", max_retries=2)
        stats = w2.reap(now=time.time())
        self.assertEqual(stats["requeued"], 0)
        self.assertEqual(client.lrange("ops:queue:processing:w1", 0, -1), [job.id])
        self.assertIsNotNone(client.zscore("ops:queue:workers", "w1"))  # 心跳没被 zrem

    def test_truly_dead_worker_still_reaped(self):
        # 心跳停了(真死)→ 照常回收
        client = fakeredis.FakeRedis(decode_responses=True)
        w1 = RedisQueue(client, worker_id="w1", max_retries=2)
        job = w1.submit("q")
        w1.consume(timeout=1)
        w1.mark_running(job.id)
        client.zadd("ops:queue:workers", {"w1": time.time() - 3600})  # 心跳停更
        w2 = RedisQueue(client, worker_id="w2", max_retries=2)
        stats = w2.reap(now=time.time())
        self.assertEqual(stats["requeued"], 1)
        self.assertIsNone(client.zscore("ops:queue:workers", "w1"))

    def test_reaper_stops_draining_when_heartbeat_revives_mid_drain(self):
        # 判死后 drain 过程中 w1 心跳恢复 → 停止抢夺剩余在途任务,且不 zrem 其心跳
        client = fakeredis.FakeRedis(decode_responses=True)
        w1 = RedisQueue(client, worker_id="w1", max_retries=2)
        j1 = w1.submit("a")
        j2 = w1.submit("b")
        w1.consume(timeout=1)
        w1.consume(timeout=1)
        w1.mark_running(j1.id)
        w1.mark_running(j2.id)
        client.zadd("ops:queue:workers", {"w1": time.time() - 3600})  # 先呈 stale
        w2 = RedisQueue(client, worker_id="w2", max_retries=2)
        real_rpop = client.rpop

        def rpop_and_revive(key, *a, **kw):
            out = real_rpop(key, *a, **kw)
            client.zadd("ops:queue:workers", {"w1": time.time()})  # w1 恢复心跳
            return out

        client.rpop = rpop_and_revive
        try:
            stats = w2.reap(now=time.time())
        finally:
            client.rpop = real_rpop
        self.assertEqual(stats["requeued"], 1)  # 只抢走了复活前的那一个
        self.assertEqual(client.llen("ops:queue:processing:w1"), 1)  # 剩余在途没被抢
        self.assertIsNotNone(client.zscore("ops:queue:workers", "w1"))  # 心跳没被 zrem


class WorkerHeartbeatThreadTest(unittest.TestCase):
    """P0-1①:独立心跳线程 —— handler 忙跑 120s 时 consume 不会被调,心跳必须独立续。"""

    def test_heartbeat_loop_renews_periodically_until_stop(self):
        rq = MagicMock()
        stop = threading.Event()
        t = threading.Thread(target=worker_main._heartbeat_loop, args=(rq, stop, 0.01), daemon=True)
        t.start()
        deadline = time.time() + 2
        while time.time() < deadline and rq.heartbeat.call_count < 3:
            time.sleep(0.01)
        stop.set()
        t.join(timeout=1)
        self.assertGreaterEqual(rq.heartbeat.call_count, 3)

    def test_heartbeat_loop_survives_redis_errors(self):
        rq = MagicMock()
        rq.heartbeat.side_effect = ConnectionError("redis down")
        stop = threading.Event()
        t = threading.Thread(target=worker_main._heartbeat_loop, args=(rq, stop, 0.01), daemon=True)
        t.start()
        deadline = time.time() + 2
        while time.time() < deadline and rq.heartbeat.call_count < 2:
            time.sleep(0.01)
        stop.set()
        t.join(timeout=1)
        self.assertGreaterEqual(rq.heartbeat.call_count, 2)  # 异常不终结心跳线程


class PromoteAtomicityTest(unittest.TestCase):
    """P1-2:promote_due_retries 的 zrem→lpush 必须原子;崩溃窗口不得丢任务。"""

    def test_promote_survives_direct_lpush_failure(self):
        # 旧实现:client.zrem 成功后 client.lpush 抛异常 → 任务从 zset 消失且没入队(永久丢失)。
        # 新实现走 WATCH+MULTI 事务,不再裸调 client.lpush;任务守恒(要么在 zset 要么在队列)。
        rq = _rq(max_retries=1)
        job = rq.submit("q")
        rq.consume(timeout=1)
        self.assertEqual(rq.fail_or_retry(job.id, "x"), "retried")
        self.assertEqual(rq.retry_size(), 1)
        with patch.object(rq._r, "lpush", side_effect=ConnectionError("redis down")):
            promoted = rq.promote_due_retries(now=10**12)
        self.assertEqual(promoted, 1)
        self.assertEqual(rq.qsize() + rq.retry_size(), 1, "任务守恒被破坏:重试任务丢失")
        self.assertEqual(rq.qsize(), 1)
        self.assertIn(job.id, rq._r.lrange(rq._queue_key, 0, -1))


class MarkRunningGuardTest(unittest.TestCase):
    """P2-2:mark_running 裸 hset 会把已 TTL 过期的 job 重建成无 question 无 TTL 的僵尸。"""

    def test_mark_running_missing_hash_counts_lost_and_creates_nothing(self):
        from ops_agent.metrics import METRICS

        rq = _rq()
        job = rq.submit("q")
        rq.consume(timeout=1)
        rq._r.delete(rq._job_key(job.id))  # 模拟 TTL 过期/驱逐
        before = METRICS.snapshot().get(("jobs_lost_total", ()), 0)
        self.assertFalse(rq.mark_running(job.id))
        after = METRICS.snapshot().get(("jobs_lost_total", ()), 0)
        self.assertEqual(after, before + 1)
        self.assertFalse(rq._r.exists(rq._job_key(job.id)))  # 不重建僵尸
        self.assertEqual(rq._r.llen(rq._processing_key), 0)  # 在途登记同步清掉


class DlqRequeueGuardTest(unittest.TestCase):
    """P2-2:dlq_requeue 不校验 hash 存在/不补 TTL → 重建无 question 无 TTL 僵尸。"""

    def _dead_job(self):
        rq = _rq(max_retries=0)
        job = rq.submit("q")
        rq.consume(timeout=1)
        self.assertEqual(rq.fail_or_retry(job.id, "x"), "dead")
        return rq, job

    def test_dlq_requeue_missing_hash_returns_false_and_clears_entry(self):
        rq, job = self._dead_job()
        rq._r.delete(rq._job_key(job.id))  # hash 已 TTL 蒸发,只剩悬空死信条目
        self.assertFalse(rq.dlq_requeue(job.id))
        self.assertEqual(rq.qsize(), 0)  # 不把僵尸误入队
        self.assertFalse(rq._r.exists(rq._job_key(job.id)))  # 不重建
        self.assertEqual(rq.dlq_size(), 0)  # 悬空条目清理掉,不留永久假死信

    def test_dlq_requeue_refreshes_ttl(self):
        rq, job = self._dead_job()
        rq._r.persist(rq._job_key(job.id))  # 模拟 TTL 已被剥离(接近过期的极端情形)
        self.assertTrue(rq.dlq_requeue(job.id))
        self.assertGreater(rq._r.ttl(rq._job_key(job.id)), 0)  # 重入队必须补 TTL
        self.assertEqual(rq.get(job.id).question, "q")  # question 完整保留


class NonRetryableFailTest(unittest.TestCase):
    """P2-4(队列侧):retryable=False 直接判 dead 进 DLQ,不浪费重试预算。"""

    def test_fail_or_retry_non_retryable_goes_straight_to_dlq(self):
        rq = _rq(max_retries=5)
        job = rq.submit("q")
        rq.consume(timeout=1)
        outcome = rq.fail_or_retry(job.id, "BudgetExceeded: token 预算耗尽", retryable=False)
        self.assertEqual(outcome, "dead")
        self.assertEqual(rq.get(job.id).status, FAILED)
        self.assertEqual(rq.dlq_size(), 1)
        self.assertEqual(rq.retry_size(), 0)


class TerminalCallbackTest(unittest.TestCase):
    """P2-3:failed 回调只在终局(dead)投递 —— 否则下游先收 failed、之后又收 succeeded。"""

    def test_failed_callback_only_on_terminal_dead(self):
        rq = _rq(max_retries=1)
        job = rq.submit("q")

        def boom(j):
            raise RuntimeError("x")

        with patch("ops_agent.callback._post_callback") as cb:
            self.assertEqual(process_once(rq, boom, timeout=1), "retried")
            cb.assert_not_called()  # 中间重试不投递 failed
            rq.promote_due_retries(now=10**12)
            self.assertEqual(process_once(rq, boom, timeout=1), "dead")
            cb.assert_called_once()  # 终局才投递
            called_job = cb.call_args.args[0]
            self.assertEqual(called_job.id, job.id)
            self.assertEqual(called_job.attempts, 2)  # payload 带最终 attempts
            self.assertEqual(cb.call_args.kwargs["status"], "failed")

    def test_callback_outbox_retries_failed_delivery(self):
        rq = _rq()
        job = rq.submit("q")
        rq.consume(timeout=1)
        rq.mark_running(job.id)
        with patch("ops_agent.callback._post_callback", return_value=False) as cb:
            self.assertTrue(rq.complete(job.id, {"ok": True}))
        self.assertEqual(cb.call_count, 1)
        self.assertEqual(rq._r.llen(rq._callback_outbox_key), 0)
        self.assertEqual(rq._r.zcard(rq._callback_retry_key), 1)

        with patch("ops_agent.callback._post_callback", return_value=True) as cb:
            stats = rq.deliver_due_callbacks(limit=5, now=time.time() + 3600)
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(cb.call_args.kwargs["status"], "succeeded")

    def test_handler_no_longer_posts_failed_callback_midway(self):
        # jobs.diagnosis_job_handler 不再在每次异常时回调;终局投递收口在队列层
        from ops_agent.jobs import diagnosis_job_handler

        job = type("J", (), {"id": "j1", "trace_id": "t1", "target": None, "question": "q"})()
        with (
            patch("ops_agent.agent.run_agent", side_effect=RuntimeError("boom")),
            patch("ops_agent.callback._post_callback") as cb,
            self.assertRaises(RuntimeError),
        ):
            diagnosis_job_handler(job)
        cb.assert_not_called()


class NonRetryableWorkerTest(unittest.TestCase):
    """P2-4:确定性失败(预算耗尽/max_steps 绕圈)直接 dead 进 DLQ,不烧重试预算。"""

    def test_budget_exceeded_goes_straight_to_dlq(self):
        from ops_agent.budget import BudgetExceeded

        rq = _rq(max_retries=5)
        job = rq.submit("q")

        def boom(j):
            raise BudgetExceeded("token 预算耗尽")

        self.assertEqual(process_once(rq, boom, timeout=1), "dead")
        self.assertEqual(rq.get(job.id).status, FAILED)
        self.assertEqual(rq.dlq_size(), 1)
        self.assertEqual(rq.retry_size(), 0)

    def test_max_steps_exceeded_goes_straight_to_dlq(self):
        from ops_agent.budget import MaxStepsExceeded

        rq = _rq(max_retries=5)
        rq.submit("q")

        def boom(j):
            raise MaxStepsExceeded("达到 max_steps=8 仍未得出结论")

        self.assertEqual(process_once(rq, boom, timeout=1), "dead")
        self.assertEqual(rq.dlq_size(), 1)

    def test_plain_runtime_error_still_retries(self):
        rq = _rq(max_retries=5)
        rq.submit("q")

        def boom(j):
            raise RuntimeError("瞬时故障")

        self.assertEqual(process_once(rq, boom, timeout=1), "retried")
        self.assertEqual(rq.dlq_size(), 0)


class OptionalRedisDependencyTest(unittest.TestCase):
    """P3:模块 docstring 承诺 redis 是可选依赖、仅延迟 import —— 顶层 WatchError import
    会让未装 redis 的 memory 后端环境 import 本模块即炸。"""

    def test_module_import_does_not_require_redis(self):
        import importlib
        import sys

        saved = {
            name: sys.modules.pop(name)
            for name in list(sys.modules)
            if name == "redis" or name.startswith("redis.")
        }
        # sys.modules 里塞 None → import 该名字立刻 ImportError(标准阻断手法)
        sys.modules["redis"] = None  # type: ignore[assignment]
        sys.modules["redis.exceptions"] = None  # type: ignore[assignment]
        try:
            import ops_agent.redisqueue as rq_mod

            importlib.reload(rq_mod)  # redis 不可用时模块本身仍可 import
        finally:
            for name in ("redis", "redis.exceptions"):
                sys.modules.pop(name, None)
            sys.modules.update(saved)
            import ops_agent.redisqueue as rq_mod

            importlib.reload(rq_mod)  # 恢复真实模块状态,别污染后续测试


class CloseCleanupTest(unittest.TestCase):
    """P3:close() 摘除自己的心跳 —— 否则优雅退出的 worker 心跳残留到 dead_after 过期,
    期间 reaper 都把它当'活着',推迟对其残留的判定。"""

    def test_close_removes_own_heartbeat(self):
        client = fakeredis.FakeRedis(decode_responses=True)
        rq = RedisQueue(client, worker_id="w1")
        rq.heartbeat()
        self.assertIsNotNone(client.zscore("ops:queue:workers", "w1"))
        rq.close()
        self.assertIsNone(client.zscore("ops:queue:workers", "w1"))


class QueueAuditTrailTest(unittest.TestCase):
    """P2-8:dlq_requeue 与 reaper 回灌/判死写入 audit hash chain(本地链,无网络依赖)。"""

    def _rq_with_audit(self, tmp, **kw):
        client = fakeredis.FakeRedis(decode_responses=True)
        return client, RedisQueue(
            client, worker_id="w2", audit_log=Path(tmp) / "approvals.jsonl", **kw
        )

    def _records(self, tmp):
        import json

        path = Path(tmp) / "approvals.jsonl"
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text().splitlines()]

    def test_dlq_requeue_writes_queue_requeue_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, rq = self._rq_with_audit(tmp, max_retries=0)
            job = rq.submit("q")
            rq.consume(timeout=1)
            rq.fail_or_retry(job.id, "boom")  # 直接进 DLQ
            self.assertTrue(rq.dlq_requeue(job.id))
            records = [r for r in self._records(tmp) if r.get("type") == "queue"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["event"], "QUEUE_REQUEUE")
        self.assertEqual(records[0]["job_id"], job.id)
        self.assertEqual(records[0]["outcome"], "requeued")
        self.assertIn("hash", records[0])  # 走同一条 hash chain

    def test_reaper_writes_queue_reap_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, rq = self._rq_with_audit(tmp, max_retries=2)
            w1 = RedisQueue(client, worker_id="w1", max_retries=2)
            job = w1.submit("q")
            w1.consume(timeout=1)
            w1.mark_running(job.id)
            client.zadd("ops:queue:workers", {"w1": time.time() - 3600})
            stats = rq.reap(now=time.time())
            self.assertEqual(stats["requeued"], 1)
            records = [r for r in self._records(tmp) if r.get("type") == "queue"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["event"], "QUEUE_REAP")
        self.assertEqual(records[0]["job_id"], job.id)
        self.assertEqual(records[0]["outcome"], "retried")
        self.assertIn("worker_crashed:w1", records[0]["detail"])

    def test_audit_failure_does_not_break_queue_ops(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, rq = self._rq_with_audit(tmp, max_retries=0)
            rq._audit_log = Path("/proc/definitely/not/writable/a.jsonl")
            job = rq.submit("q")
            rq.consume(timeout=1)
            rq.fail_or_retry(job.id, "boom")
            self.assertTrue(rq.dlq_requeue(job.id))  # 审计失败不影响队列状态机

    def test_from_settings_wires_audit_log(self):
        import os
        from unittest.mock import patch as _patch

        from ops_agent.config import Settings

        env = {
            "OPS_QUEUE_BACKEND": "redis",
            "OPS_REDIS_URL": "redis://localhost:6/0",
            "OPS_APPROVAL_LOG": "/tmp/x/approvals.jsonl",
        }
        with (
            _patch.dict(os.environ, env, clear=True),
            _patch("redis.from_url", return_value=fakeredis.FakeRedis(decode_responses=True)),
        ):
            rq = RedisQueue.from_settings(Settings.from_env())
        # macOS 下 /tmp 解析为 /private/tmp,故用后缀断言
        self.assertTrue(str(rq._audit_log).endswith("/tmp/x/approvals.jsonl"))


class ConcurrentReapGuardTest(unittest.TestCase):
    """P2-5①:两个 reaper 同窗回收同一 stale RUNNING job → fail_or_retry 双执行、
    attempts 双增。守卫:带 expected_running_since 的调用仅当仍 RUNNING 且
    running_since 未变才执行。"""

    def _stale_running_job(self, client):
        rq = RedisQueue(client, worker_id="w1", max_retries=5, stale_running_seconds=10)
        job = rq.submit("q")
        rq.consume(timeout=1)
        rq.mark_running(job.id)
        client.hset(rq._job_key(job.id), "running_since", str(time.time() - 3600))
        return rq, job

    def test_fail_or_retry_skips_when_running_since_changed(self):
        client = fakeredis.FakeRedis(decode_responses=True)
        rq, job = self._stale_running_job(client)
        observed = client.hget(rq._job_key(job.id), "running_since")
        # 第一个 reaper 先回收成功(同一观测值)
        self.assertEqual(
            rq.fail_or_retry(job.id, "stale", expected_running_since=observed), "retried"
        )
        self.assertEqual(rq.get(job.id).attempts, 1)
        # 第二个 reaper 拿着同一份旧观测值迟到 → 必须跳过,attempts 不双增
        rq2 = RedisQueue(client, worker_id="w2", max_retries=5, stale_running_seconds=10)
        self.assertEqual(
            rq2.fail_or_retry(job.id, "stale", expected_running_since=observed), "skipped"
        )
        self.assertEqual(rq2.get(job.id).attempts, 1)
        self.assertEqual(rq2.retry_size() + rq2.qsize(), 1)  # 没被双份入队

    def test_fail_or_retry_skips_when_job_remarked_running_by_new_worker(self):
        # 回收间隙任务已被新 worker 领走重跑(running_since 变新)→ 旧观测值失效,跳过
        client = fakeredis.FakeRedis(decode_responses=True)
        rq, job = self._stale_running_job(client)
        observed = client.hget(rq._job_key(job.id), "running_since")
        rq.mark_running(job.id)  # 新一轮执行:running_since 刷新
        self.assertEqual(
            rq.fail_or_retry(job.id, "stale", expected_running_since=observed), "skipped"
        )
        self.assertEqual(rq.get(job.id).status, RUNNING)  # 在跑的不被打断

    def test_reap_stale_running_still_works_end_to_end(self):
        client = fakeredis.FakeRedis(decode_responses=True)
        rq, job = self._stale_running_job(client)
        stats = rq.reap(now=time.time())
        self.assertEqual(stats["requeued"], 1)
        self.assertEqual(rq.get(job.id).status, QUEUED)


class MetricsFlushTest(unittest.TestCase):
    """P1-4①:complete/finally/reaper 里更新的指标发生在 run_agent 内部 flush 之后 ——
    消费循环与 reaper 每轮结束必须 flush textfile,空闲 worker 也要周期 flush。"""

    def test_worker_loop_flushes_metrics_even_when_idle(self):
        rq = MagicMock()
        rq.consume.return_value = None  # 空闲:无任务

        class _Stop:
            def __init__(self):
                self.n = 0

            def is_set(self):
                return self.n >= 3

            def wait(self, t):
                return False

        stop = _Stop()
        real = rq.consume

        def consume(timeout=1):
            stop.n += 1
            return real(timeout=timeout)

        rq.consume = consume
        with tempfile.TemporaryDirectory() as tmp:
            mf = Path(tmp) / "metrics.prom"
            worker_main._worker_loop(rq, lambda j: {}, stop, metrics_file=mf)
            self.assertTrue(mf.exists())  # 空闲轮也落盘
            self.assertIn("ops_agent", mf.read_text())

    def test_reaper_loop_flushes_metrics_each_round(self):
        rq = MagicMock()
        stop = threading.Event()
        rq.reap.side_effect = lambda: (stop.set(), {"requeued": 1, "dead": 0, "lost": 0})[1]
        with tempfile.TemporaryDirectory() as tmp:
            mf = Path(tmp) / "metrics.prom"
            worker_main._reaper_loop(rq, stop, 0.01, metrics_file=mf)
            self.assertTrue(mf.exists())

    def test_flush_metrics_file_tolerates_write_errors(self):
        # 只读路径 → 不抛,不崩循环
        worker_main._flush_metrics_file(Path("/proc/definitely/not/writable/x.prom"))

    def test_worker_metrics_http_server_exposes_metrics(self):
        import socket
        import urllib.request

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        httpd = worker_main._start_worker_metrics_server(port)
        self.assertIsNotNone(httpd)
        assert httpd is not None
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
                body = resp.read().decode()
            self.assertEqual(resp.status, 200)
            self.assertIn("ops_agent", body)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_reap_one_records_lost_outcome_from_fail_or_retry(self):
        # P1-4③:fail_or_retry 返回 lost(hash 在 _load 与 fail_or_retry 之间蒸发)时
        # stats 不得漏记
        rq = _rq()
        job = rq.submit("q")
        rq.consume(timeout=1)
        rq.mark_running(job.id)
        stats = {"requeued": 0, "dead": 0, "lost": 0}
        with patch.object(rq, "fail_or_retry", return_value="lost"):
            rq._reap_one(job.id, stats, error="worker_crashed:w1")
        self.assertEqual(stats["lost"], 1)


class WorkerLoopBackoffTest(unittest.TestCase):
    """P2-5:Redis 断连时消费循环指数退避(响应 stop)+ 日志限频,不热旋不刷屏。"""

    class _Stop:
        """记录 wait 时长的假 stop event;waits 达上限后自动置停,终止循环。"""

        def __init__(self, limit: int) -> None:
            self.waits: list[float] = []
            self._limit = limit
            self._set = False

        def is_set(self) -> bool:
            return self._set

        def wait(self, t: float) -> bool:
            self.waits.append(t)
            if len(self.waits) >= self._limit:
                self._set = True
            return self._set

    def test_backoff_grows_and_is_capped(self):
        rq = MagicMock()
        rq.consume.side_effect = ConnectionError("redis down")
        stop = self._Stop(limit=10)
        with patch.object(worker_main.logger, "exception") as log_exc:
            worker_main._worker_loop(rq, lambda j: {}, stop)
        self.assertEqual(len(stop.waits), 10)
        self.assertGreater(stop.waits[1], stop.waits[0])  # 指数增长
        self.assertGreater(stop.waits[3], stop.waits[2])
        self.assertLessEqual(max(stop.waits), worker_main._BACKOFF_MAX_SECONDS)  # 有上限
        self.assertLess(log_exc.call_count, len(stop.waits))  # 日志限频:不是每次失败都记

    def test_backoff_resets_after_success(self):
        rq = MagicMock()
        rq.consume.side_effect = [ConnectionError("down"), None, ConnectionError("down")]
        stop = self._Stop(limit=2)
        worker_main._worker_loop(rq, lambda j: {}, stop)
        # 两次失败之间隔了一次成功 → 退避回到基准值,不累积
        self.assertEqual(stop.waits[0], stop.waits[1])


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
                # P0-1:独立心跳线程在续 Redis 心跳(consume 被 mock,不会间接触发)
                deadline2 = time.time() + 3
                while time.time() < deadline2 and fake_rq.heartbeat.call_count < 1:
                    time.sleep(0.02)
                fake_rq.heartbeat.assert_called()
                handlers[signal.SIGTERM](signal.SIGTERM, None)  # 触发 SIGTERM
                t.join(timeout=5)
                self.assertFalse(t.is_alive())  # run() 优雅退出
                fake_rq.close.assert_called_once()  # 资源释放


if __name__ == "__main__":
    unittest.main()
