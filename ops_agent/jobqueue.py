"""进程内诊断任务队列 + 异步 worker(架构演进 Step 1:解耦同步阻塞 webhook)。

事件驱动第一步,零中间件:webhook 入队 → 立即 202 → worker 线程池异步跑 handler。
- **有界队列**:满则 `submit` 返回 None(背压信号),上游回 429,不堆积。
- **有界结果缓存**:`OrderedDict` 上限淘汰最旧,防内存无界增长。
- **优雅停机**:`shutdown` 置停 + join,排空在途任务。

Step 2 会把 `queue.Queue` 换成 Redis/SQS 并把 worker 拆成独立进程;本模块接口保持稳定。
"""

import logging
import queue
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ops_agent.budget import is_non_retryable
from ops_agent.metrics import METRICS

logger = logging.getLogger("ops_agent.jobqueue")

# 任务状态机:queued → running → succeeded / failed
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"


@dataclass
class DiagnosisJob:
    id: str
    question: str
    trace_id: str
    target: str | None = None
    actor: str | None = None
    status: str = QUEUED
    result: dict | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    attempts: int = 0  # 已尝试次数(重试/DLQ 判定;Redis 后端用)

    def to_public(self) -> dict[str, Any]:
        """对外 JSON(查询端点用)。"""
        return {
            "job_id": self.id,
            "trace_id": self.trace_id,
            "status": self.status,
            "target": self.target,
            "actor": self.actor,
            "created_at": self.created_at,
            "attempts": self.attempts,
            "result": self.result,
            "error": self.error,
        }


class JobQueue:
    """进程内有界任务队列 + worker 线程池。handler(job) 返回 dict 结果。"""

    def __init__(
        self,
        handler: Callable[[DiagnosisJob], dict],
        *,
        workers: int = 2,
        max_queue: int = 100,
        max_results: int = 1000,
        max_retries: int = 0,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 60.0,
        queue_depth_alert_threshold: int = 0,
    ) -> None:
        self._handler = handler
        self._max_queue = max(1, max_queue)
        self._q: queue.Queue[str] = queue.Queue(maxsize=self._max_queue)
        self._jobs: OrderedDict[str, DiagnosisJob] = OrderedDict()
        self._max_results = max(1, max_results)
        self._max_retries = max(0, max_retries)
        self._retry_base_seconds = max(0.0, retry_base_seconds)
        self._retry_max_seconds = max(0.0, retry_max_seconds)
        self._queue_depth_alert_threshold = max(0, queue_depth_alert_threshold)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._timers: dict[threading.Timer, str] = {}  # 挂起的重试 Timer → job_id(停机时取消)
        self._threads: list[threading.Thread] = []
        worker_n = max(1, workers)
        METRICS.set("workers_total", worker_n, backend="memory")  # 利用率分母
        for i in range(worker_n):
            t = threading.Thread(target=self._worker_loop, name=f"diag-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def submit(
        self,
        question: str,
        target: str | None = None,
        trace_id: str | None = None,
        actor: str | None = None,
    ) -> DiagnosisJob | None:
        """入队一个诊断任务。队列满 → 返回 None(背压,上游应回 429)。"""
        job = DiagnosisJob(
            id=uuid.uuid4().hex,
            question=question,
            target=target,
            trace_id=trace_id or uuid.uuid4().hex,
            actor=actor,
        )
        with self._lock:
            self._jobs[job.id] = job
            self._evict_locked()
        try:
            self._q.put_nowait(job.id)
        except queue.Full:
            with self._lock:
                self._jobs.pop(job.id, None)
            METRICS.inc("jobs_rejected_total")  # 背压:队列满拒收
            self.update_queue_metrics()
            return None
        METRICS.inc("jobs_submitted_total")
        self.update_queue_metrics()
        return job

    def get(self, job_id: str) -> DiagnosisJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def qsize(self) -> int:
        return self._q.qsize()

    def update_queue_metrics(self) -> None:
        depth = self.qsize()
        METRICS.set("queue_depth", depth, backend="memory")
        with self._lock:
            backlog = len(self._timers)  # 挂起的重试 Timer = 内存后端的重试积压
        METRICS.set("retry_backlog", backlog, backend="memory")
        if self._queue_depth_alert_threshold > 0:
            METRICS.set("queue_depth_alert_threshold", self._queue_depth_alert_threshold)
            METRICS.set(
                "queue_depth_over_threshold",
                1.0 if depth >= self._queue_depth_alert_threshold else 0.0,
                backend="memory",
            )

    def _retry_delay(self, attempts: int) -> float:
        if self._retry_base_seconds <= 0:
            return 0.0
        return min(self._retry_max_seconds, self._retry_base_seconds * (2 ** max(0, attempts - 1)))

    def _requeue_after_delay(self, job_id: str, delay: float) -> None:
        def fire() -> None:
            with self._lock:
                self._timers.pop(timer, None)
            self._requeue(job_id)

        timer = threading.Timer(delay, fire)
        timer.daemon = True
        with self._lock:
            if self._stop.is_set():
                # 停机竞态:不再排注定丢失的 Timer,直接标终态,避免任务永久卡 QUEUED
                cancelled = self._mark_failed_locked(job_id, "shutdown_retry_cancelled")
            else:
                self._timers[timer] = job_id
                cancelled = None
        if cancelled is not None:
            if cancelled:
                self._notify_failed(job_id)  # P2-6:dead 路径同样要发终局 failed(锁外)
            return
        timer.start()

    def _mark_failed_locked(self, job_id: str, error: str) -> bool:
        """持锁把非终态任务标记 FAILED(给查询端点一个确定终态)。返回是否真发生了状态翻转
        (已终态的任务返回 False,调用方据此避免重复回调/重复计数)。"""
        job = self._jobs.get(job_id)
        if job is not None and job.status not in (SUCCEEDED, FAILED):
            job.status = FAILED
            job.error = error
            METRICS.inc("jobs_failed_total")
            return True
        return False

    def _requeue(self, job_id: str) -> None:
        if self._stop.is_set():
            return
        try:
            self._q.put_nowait(job_id)
            self.update_queue_metrics()
        except queue.Full:
            logger.warning("重试入队失败:队列已满 job_id=%s", job_id)
            with self._lock:
                failed = self._mark_failed_locked(job_id, "retry_queue_full")
            self.update_queue_metrics()
            if failed:
                self._notify_failed(job_id)  # P2-6:终局 dead,必须给下游失败通知

    def _evict_locked(self) -> None:
        # 结果缓存上限:超出则从最旧开始淘汰(已 succeeded/failed 优先,但简化为 FIFO)。
        while len(self._jobs) > self._max_results:
            self._jobs.popitem(last=False)

    def _set_status(self, job_id: str, status: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.status = status

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                job = self.get(job_id)
                if job is None:
                    # 出队但 job entry 已被结果缓存淘汰(max_results < max_queue 误配时)→ 任务丢失。
                    # 静默 continue 会"无声丢单",留痕 + 计指标便于告警。
                    logger.warning("job_id=%s 出队但 entry 已淘汰,丢弃", job_id)
                    METRICS.inc("jobs_lost_total")
                    continue
                self._set_status(job_id, RUNNING)
                self.update_queue_metrics()
                METRICS.add("workers_busy", 1, backend="memory")  # 在途 worker 数(利用率分子)
                started = time.monotonic()
                try:
                    result = self._handler(job)
                    completed = False
                    with self._lock:
                        if job_id in self._jobs:
                            self._jobs[job_id].result = result
                            self._jobs[job_id].status = SUCCEEDED
                            completed = True
                    METRICS.inc("jobs_succeeded_total")
                    if completed:
                        # P1-3:succeeded 回调在终态写入后由队列层投递(与 redis 后端一致)
                        self._notify(job_id, status="succeeded", result=result)
                except Exception as exc:  # noqa: BLE001 - worker 边界:任务失败不拖垮 worker
                    logger.exception("诊断任务失败 job_id=%s trace_id=%s", job_id, job.trace_id)
                    retry_delay: float | None = None
                    final_failure = False
                    with self._lock:
                        if job_id in self._jobs:
                            stored = self._jobs[job_id]
                            stored.attempts += 1
                            stored.error = f"{type(exc).__name__}: {exc}"
                            # P2-4:确定性失败(预算耗尽/max_steps)不可重试,直接终局
                            if stored.attempts <= self._max_retries and not is_non_retryable(exc):
                                stored.status = QUEUED
                                retry_delay = self._retry_delay(stored.attempts)
                                METRICS.inc("jobs_retried_total")
                            else:
                                stored.status = FAILED
                                METRICS.inc("jobs_failed_total")
                                final_failure = True
                    # 在锁外排重试 Timer(Lock 不可重入,且 delay=0 时 Timer 会立刻回调取锁)
                    if retry_delay is not None:
                        self._requeue_after_delay(job_id, retry_delay)
                    if final_failure:
                        self._notify_failed(job_id)  # P2-3:只在终局投递 failed(锁外,网络 IO)
                finally:
                    elapsed = time.monotonic() - started
                    METRICS.observe("job_duration_seconds", elapsed, backend="memory")
                    METRICS.add("workers_busy", -1, backend="memory")
            finally:
                self._q.task_done()
                self.update_queue_metrics()

    def _notify_failed(self, job_id: str) -> None:
        """终局失败回调(P2-3,best-effort):重试耗尽/不可重试才投递,payload 带最终 attempts。"""
        job = self.get(job_id)
        if job is None:
            return
        self._notify(job_id, status="failed", error=job.error)

    def _notify(
        self, job_id: str, *, status: str, result: dict | None = None, error: str | None = None
    ) -> None:
        """终态回调统一投递口(best-effort,锁外调用,回调失败不影响任务状态机)。"""
        job = self.get(job_id)
        if job is None:
            return
        try:
            from ops_agent.callback import _post_callback

            _post_callback(job, status=status, result=result, error=error)
        except Exception as exc:  # noqa: BLE001 - 回调失败不影响任务状态机
            logger.warning("%s 回调投递异常 job_id=%s: %s", status, job_id, exc)

    def shutdown(self, timeout: float = 10.0) -> None:
        """置停并 join worker(排空在途)。挂起的重试 Timer 取消并把对应任务标终态。

        排空窗口(P2-6):server 停机传 `OPS_MAX_RUN_SECONDS+10`(与 redis worker 一致),
        默认 10s 仅供测试/嵌入场景 —— 在途诊断可长至 max_run,10s join 会截断它。
        """
        self._stop.set()
        with self._lock:
            pending = dict(self._timers)
            self._timers.clear()
        for timer in pending:
            timer.cancel()  # 取消还没触发的重试,避免 daemon Timer 静默丢单
        newly_failed: list[str] = []
        if pending:
            with self._lock:
                for job_id in pending.values():
                    if self._mark_failed_locked(job_id, "shutdown_retry_cancelled"):
                        newly_failed.append(job_id)
        for job_id in newly_failed:
            # P2-6:取消的重试是终局失败,发 failed 回调(best-effort;停机窗内网络 IO
            # 有 10s 超时上限,失败只 warn,不阻塞退出)
            self._notify_failed(job_id)
        for t in self._threads:
            t.join(timeout=timeout)
