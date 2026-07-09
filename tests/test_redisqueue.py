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
        t = threading.Thread(
            target=worker_main._heartbeat_loop, args=(rq, stop, 0.01), daemon=True
        )
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
        t = threading.Thread(
            target=worker_main._heartbeat_loop, args=(rq, stop, 0.01), daemon=True
        )
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
