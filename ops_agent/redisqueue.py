"""Redis 队列后端(架构演进 Step 2:真队列 + 独立 worker 进程 + DLQ/重试)。

与进程内 `JobQueue` 对外接口对齐(`submit`/`get`),但队列与任务状态都落 Redis,
因此 **ingress 进程**(serve)与 **worker 进程**(serve-worker)跨进程共享同一份状态,
worker 崩溃/重启可恢复,可水平扩展。

数据布局:
- 队列:Redis LIST `<queue_key>`(ingress LPUSH,worker BRPOP)。
- 任务:Redis HASH `<ns>:job:<id>`(question/target/status/result/error/created_at/attempts),带 TTL。
- 死信:Redis LIST `<dlq_key>`(超重试上限的 job_id)。

`redis` 是**可选依赖**(`pip install ops-agent[redis]`),仅本模块延迟 import;memory 后端不需要它。
"""

import json
import time
import uuid
from typing import Any

from redis.exceptions import WatchError

from ops_agent.jobqueue import FAILED, QUEUED, RUNNING, SUCCEEDED, DiagnosisJob
from ops_agent.metrics import METRICS

_JOB_PREFIX = "ops:job:"


class RedisQueue:
    """Redis 后端队列。client 可注入(测试用 fakeredis);生产走 from_url。"""

    def __init__(
        self,
        client: Any,
        *,
        queue_key: str = "ops:queue",
        dlq_key: str = "ops:dlq",
        retry_key: str = "ops:retry",
        max_queue: int = 1000,
        job_ttl: int = 86400,
        max_retries: int = 2,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 60.0,
        queue_depth_alert_threshold: int = 0,
    ) -> None:
        self._r = client
        self._queue_key = queue_key
        self._dlq_key = dlq_key
        self._retry_key = retry_key
        self._max_queue = max(1, max_queue)
        self._job_ttl = job_ttl
        self._max_retries = max(0, max_retries)
        self._retry_base_seconds = max(0.0, retry_base_seconds)
        self._retry_max_seconds = max(0.0, retry_max_seconds)
        self._queue_depth_alert_threshold = max(0, queue_depth_alert_threshold)

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> "RedisQueue":
        import redis  # 延迟 import:memory 后端无需 redis 依赖

        client = redis.from_url(url, decode_responses=True)
        return cls(client, **kwargs)

    # ── 序列化 ────────────────────────────────────────────────
    def _job_key(self, job_id: str) -> str:
        return _JOB_PREFIX + job_id

    def _mapping(self, job: DiagnosisJob) -> dict[str, str]:
        return {
            "question": job.question,
            "trace_id": job.trace_id,
            "target": job.target or "",
            "status": job.status,
            "result": json.dumps(job.result) if job.result is not None else "",
            "error": job.error or "",
            "created_at": str(job.created_at),
            "attempts": str(job.attempts),
        }

    def _store(self, job: DiagnosisJob) -> None:
        key = self._job_key(job.id)
        self._r.hset(key, mapping=self._mapping(job))
        self._r.expire(key, self._job_ttl)

    def _load(self, job_id: str) -> DiagnosisJob | None:
        h = self._r.hgetall(self._job_key(job_id))
        if not h:
            return None
        return DiagnosisJob(
            id=job_id,
            question=h.get("question", ""),
            trace_id=h.get("trace_id") or job_id,
            target=h.get("target") or None,
            status=h.get("status", QUEUED),
            result=json.loads(h["result"]) if h.get("result") else None,
            error=h.get("error") or None,
            created_at=float(h.get("created_at", "0") or 0),
            attempts=int(h.get("attempts", "0") or 0),
        )

    # ── ingress(serve)────────────────────────────────────────
    def submit(
        self, question: str, target: str | None = None, trace_id: str | None = None
    ) -> DiagnosisJob | None:
        """入队。队列长度达上限 → 返回 None(背压)。

        **原子**:WATCH 队列 → 校验长度 → MULTI(hset+expire+lpush)EXEC。多 ingress 并发时
        背压是硬上限(不会超),且 store 与 lpush 同事务提交,不会留"有 hash 无队列项"的孤儿。
        """
        job = DiagnosisJob(
            id=uuid.uuid4().hex,
            question=question,
            trace_id=trace_id or uuid.uuid4().hex,
            target=target,
        )
        job_key = self._job_key(job.id)
        mapping = self._mapping(job)
        with self._r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(self._queue_key)
                    if pipe.llen(self._queue_key) >= self._max_queue:
                        pipe.reset()
                        METRICS.inc("jobs_rejected_total")
                        self.update_queue_metrics()
                        return None
                    pipe.multi()
                    pipe.hset(job_key, mapping=mapping)
                    pipe.expire(job_key, self._job_ttl)
                    pipe.lpush(self._queue_key, job.id)
                    pipe.execute()
                    break
                except WatchError:
                    continue  # 队列被并发改 → 重试整段(乐观锁)
        METRICS.inc("jobs_submitted_total")
        self.update_queue_metrics()
        return job

    def get(self, job_id: str) -> DiagnosisJob | None:
        return self._load(job_id)

    def ping(self) -> bool:
        """后端连通性探测(k8s readiness 用):Redis 可达返回 True。"""
        try:
            return bool(self._r.ping())
        except Exception:  # noqa: BLE001 - 探活边界:任何连接异常都视为未就绪
            return False

    def qsize(self) -> int:
        return int(self._r.llen(self._queue_key))

    def update_queue_metrics(self) -> None:
        depth = self.qsize()
        METRICS.set("queue_depth", depth, backend="redis")
        # 重试积压(到期前挂在 delayed zset)+ 死信堆积:都是积压排查的关键水位
        METRICS.set("retry_backlog", self.retry_size(), backend="redis")
        METRICS.set("dlq_size", self.dlq_size(), backend="redis")
        if self._queue_depth_alert_threshold > 0:
            METRICS.set("queue_depth_alert_threshold", self._queue_depth_alert_threshold)
            METRICS.set(
                "queue_depth_over_threshold",
                1.0 if depth >= self._queue_depth_alert_threshold else 0.0,
                backend="redis",
            )

    # ── worker(serve-worker)──────────────────────────────────
    def consume(self, timeout: int = 1) -> str | None:
        """阻塞取一个 job_id(BRPOP);超时返回 None。"""
        self.promote_due_retries()
        item = self._r.brpop([self._queue_key], timeout=timeout)
        if item is None:
            self.update_queue_metrics()
            return None
        self.update_queue_metrics()
        return item[1]  # (key, value)

    def mark_running(self, job_id: str) -> None:
        self._r.hset(self._job_key(job_id), "status", RUNNING)

    def complete(self, job_id: str, result: dict) -> None:
        key = self._job_key(job_id)
        self._r.hset(key, mapping={"status": SUCCEEDED, "result": json.dumps(result), "error": ""})
        METRICS.inc("jobs_succeeded_total")
        self.update_queue_metrics()

    def fail_or_retry(self, job_id: str, error: str) -> str:
        """失败处理:未超重试上限 → 重新入队(返回 'retried');否则 → DLQ(返回 'dead')。"""
        key = self._job_key(job_id)
        attempts = int(self._r.hincrby(key, "attempts", 1))
        if attempts <= self._max_retries:
            retry_after = time.time() + self._retry_delay(attempts)
            self._r.hset(
                key,
                mapping={"status": QUEUED, "error": error, "retry_after": str(retry_after)},
            )
            self._r.zadd(self._retry_key, {job_id: retry_after})
            METRICS.inc("jobs_retried_total")
            self.update_queue_metrics()
            return "retried"
        self._r.hset(key, mapping={"status": FAILED, "error": error})
        self._r.lpush(self._dlq_key, job_id)
        METRICS.inc("jobs_failed_total")
        self.update_queue_metrics()
        return "dead"

    def _retry_delay(self, attempts: int) -> float:
        if self._retry_base_seconds <= 0:
            return 0.0
        return min(self._retry_max_seconds, self._retry_base_seconds * (2 ** max(0, attempts - 1)))

    def promote_due_retries(self, now: float | None = None) -> int:
        """把到期重试任务从 delayed zset 移回主队列。返回提升数量。"""
        now = time.time() if now is None else now
        job_ids = list(self._r.zrangebyscore(self._retry_key, 0, now))
        promoted = 0
        for job_id in job_ids:
            if self._r.zrem(self._retry_key, job_id):
                self._r.lpush(self._queue_key, job_id)
                promoted += 1
        if promoted:
            self.update_queue_metrics()
        return promoted

    def retry_size(self) -> int:
        return int(self._r.zcard(self._retry_key))

    # ── DLQ 运维 ──────────────────────────────────────────────
    def dlq_list(self, limit: int = 100) -> list[str]:
        return list(self._r.lrange(self._dlq_key, 0, max(0, limit - 1)))

    def dlq_size(self) -> int:
        return int(self._r.llen(self._dlq_key))

    def dlq_requeue(self, job_id: str) -> bool:
        """把一个死信任务移回主队列(重置 attempts)。

        **原子**:WATCH dlq → 确认 job 在 dlq → MULTI(lrem+hset+lpush)EXEC。避免"从 dlq 删除但
        未回主队列"的丢失,也避免 lrem 删 0 却仍 hset/lpush(把非死信任务误入队)。
        """
        job_key = self._job_key(job_id)
        reset = {"status": QUEUED, "attempts": "0", "error": ""}
        with self._r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(self._dlq_key)
                    if pipe.lpos(self._dlq_key, job_id) is None:
                        pipe.reset()
                        return False  # 不在 dlq
                    pipe.multi()
                    pipe.lrem(self._dlq_key, 1, job_id)
                    pipe.hset(job_key, mapping=reset)
                    pipe.lpush(self._queue_key, job_id)
                    pipe.execute()
                    break
                except WatchError:
                    continue
        self.update_queue_metrics()
        return True

    def close(self) -> None:
        import contextlib

        with contextlib.suppress(Exception):  # 关闭尽力而为
            self._r.close()
