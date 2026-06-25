"""故障注入 / 韧性测试(P4):四类生产故障下系统不丢、不崩、给确定信号。

覆盖:① 队列满(背压硬上限)② Redis 断连(readiness 失败 + worker 循环存活)
③ worker 崩溃(handler 抛错 → DLQ,线程不死)④ callback 超时(best-effort,不影响结果)。
用 fakeredis + 注入假故障,无需真 Redis / 真 LLM。
"""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import fakeredis

from ops_agent.jobqueue import FAILED, JobQueue
from ops_agent.redisqueue import RedisQueue
from ops_agent.worker_main import _worker_loop, process_once


def _rq(max_queue: int = 100, max_retries: int = 2) -> RedisQueue:
    return RedisQueue(
        fakeredis.FakeRedis(decode_responses=True), max_queue=max_queue, max_retries=max_retries
    )


class QueueFullBackpressureTest(unittest.TestCase):
    """① 队列满:硬上限,超出返回 None(背压),并发下也不破限。"""

    def test_redis_backpressure_is_hard_limit_under_concurrency(self):
        rq = _rq(max_queue=10)
        accepted = []
        lock = threading.Lock()

        def worker():
            job = rq.submit("q")
            with lock:
                accepted.append(job is not None)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 50 并发提交,恰好 10 入队(WATCH/MULTI 原子背压),其余被拒,绝不超限
        self.assertEqual(sum(accepted), 10)
        self.assertEqual(rq.qsize(), 10)

    def test_memory_backpressure_returns_none(self):
        import time

        # 用阻塞 handler 占住唯一 worker,再塞满 maxsize=1 队列,后续溢出应被背压
        gate = threading.Event()
        jq = JobQueue(lambda job: gate.wait(2) or {}, workers=1, max_queue=1)
        try:
            jq.submit("running")  # 占住 worker
            time.sleep(0.1)
            jq.submit("queued-1")  # 入队列(size 1)
            results = [jq.submit(f"overflow-{i}") for i in range(5)]
            self.assertTrue(any(r is None for r in results))  # 至少一个被背压
        finally:
            gate.set()
            jq.shutdown()


class RedisDisconnectTest(unittest.TestCase):
    """② Redis 断连:readiness 失败(摘流量),worker 循环吞掉异常继续(不崩)。"""

    def test_ping_false_when_redis_down(self):
        rq = _rq()

        class _Dead:
            def ping(self):
                raise ConnectionError("connection refused")

        rq._r = _Dead()
        self.assertFalse(rq.ping())

    def test_worker_loop_survives_consume_errors_then_stops(self):
        # consume 持续抛 ConnectionError(Redis 断)→ _worker_loop 不能崩,应继续循环到 stop。
        stop = threading.Event()
        calls = {"n": 0}

        class _FlakyRq:
            def consume(self, timeout=1):
                calls["n"] += 1
                if calls["n"] >= 3:
                    stop.set()  # 模拟"几次失败后人为停机"
                raise ConnectionError("redis down")

        # 不抛出到顶层即为通过(异常被 _worker_loop 捕获);stop 后正常返回
        _worker_loop(_FlakyRq(), lambda job: {}, stop)
        self.assertGreaterEqual(calls["n"], 3)


class WorkerCrashTest(unittest.TestCase):
    """③ worker 崩溃:handler 抛各类异常 → 进重试/DLQ,消费线程不死。"""

    def test_handler_exceptions_go_to_dlq_not_crash(self):
        for exc in (RuntimeError("boom"), ValueError("bad"), KeyError("missing")):
            rq = _rq(max_retries=0)  # 直接死信便于断言
            job = rq.submit("q")

            def boom(j, _e=exc):
                raise _e

            outcome = process_once(rq, boom, timeout=1)  # 不应抛出
            self.assertEqual(outcome, "dead")
            done = rq.get(job.id)
            self.assertEqual(done.status, FAILED)
            self.assertIn(type(exc).__name__, done.error)
            self.assertEqual(rq.dlq_size(), 1)

    def test_worker_loop_survives_handler_crash(self):
        rq = _rq(max_retries=0)
        rq.submit("q")
        stop = threading.Event()

        def boom(job):
            stop.set()  # 处理一个就停
            raise RuntimeError("worker crashed mid-flight")

        # handler 崩了 _worker_loop 也不该把异常抛到顶层
        _worker_loop(rq, boom, stop)
        self.assertEqual(rq.dlq_size(), 1)


class CallbackTimeoutTest(unittest.TestCase):
    """④ callback 超时:best-effort,不影响诊断结果、不崩 worker。"""

    def setUp(self):
        # get_settings() 每次读 env(无缓存),设了 OPS_CALLBACK_URL 即触发回调路径
        self._patcher = patch.dict(
            "os.environ", {"OPS_CALLBACK_URL": "http://127.0.0.1:9/cb"}, clear=False
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_post_callback_swallows_timeout(self):
        from ops_agent import server

        job = SimpleNamespace(id="j1", trace_id="t1")
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            # 不抛出即通过(回调失败只 warn)
            server._post_callback(job, status="succeeded", result={"ok": True})

    def test_handler_returns_result_even_if_callback_times_out(self):
        from ops_agent import server
        from ops_agent.models import Diagnosis, Severity

        diag = Diagnosis(
            severity=Severity.WARNING,
            summary="s",
            root_cause="r",
            evidence=["e"],
            suggested_action="a",
            confidence=0.5,
        )
        job = SimpleNamespace(id="j2", trace_id="t2", target=None, question="why")
        with (
            patch("ops_agent.agent.run_agent", return_value=(diag, [])),
            patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")),
        ):
            result = server.diagnosis_job_handler(job)
        # 回调超时被吞,诊断结果照常返回
        self.assertEqual(result["trace_id"], "t2")
        self.assertEqual(result["diagnosis"]["severity"], "WARNING")


if __name__ == "__main__":
    unittest.main()
