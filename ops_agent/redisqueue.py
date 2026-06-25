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
import uuid
from typing import Any

from ops_agent.jobqueue import FAILED, QUEUED, RUNNING, SUCCEEDED, DiagnosisJob

_JOB_PREFIX = "ops:job:"


class RedisQueue:
    """Redis 后端队列。client 可注入(测试用 fakeredis);生产走 from_url。"""

    def __init__(
        self,
        client: Any,
        *,
        queue_key: str = "ops:queue",
        dlq_key: str = "ops:dlq",
        max_queue: int = 1000,
        job_ttl: int = 86400,
        max_retries: int = 2,
    ) -> None:
        self._r = client
        self._queue_key = queue_key
        self._dlq_key = dlq_key
        self._max_queue = max(1, max_queue)
        self._job_ttl = job_ttl
        self._max_retries = max_retries

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> "RedisQueue":
        import redis  # 延迟 import:memory 后端无需 redis 依赖

        client = redis.from_url(url, decode_responses=True)
        return cls(client, **kwargs)

    # ── 序列化 ────────────────────────────────────────────────
    def _job_key(self, job_id: str) -> str:
        return _JOB_PREFIX + job_id

    def _store(self, job: DiagnosisJob) -> None:
        mapping = {
            "question": job.question,
            "target": job.target or "",
            "status": job.status,
            "result": json.dumps(job.result) if job.result is not None else "",
            "error": job.error or "",
            "created_at": str(job.created_at),
            "attempts": str(job.attempts),
        }
        key = self._job_key(job.id)
        self._r.hset(key, mapping=mapping)
        self._r.expire(key, self._job_ttl)

    def _load(self, job_id: str) -> DiagnosisJob | None:
        h = self._r.hgetall(self._job_key(job_id))
        if not h:
            return None
        return DiagnosisJob(
            id=job_id,
            question=h.get("question", ""),
            target=h.get("target") or None,
            status=h.get("status", QUEUED),
            result=json.loads(h["result"]) if h.get("result") else None,
            error=h.get("error") or None,
            created_at=float(h.get("created_at", "0") or 0),
            attempts=int(h.get("attempts", "0") or 0),
        )

    # ── ingress(serve)────────────────────────────────────────
    def submit(self, question: str, target: str | None = None) -> DiagnosisJob | None:
        """入队。队列长度达上限 → 返回 None(背压)。"""
        if self._r.llen(self._queue_key) >= self._max_queue:
            return None
        job = DiagnosisJob(id=uuid.uuid4().hex, question=question, target=target)
        self._store(job)
        self._r.lpush(self._queue_key, job.id)
        return job

    def get(self, job_id: str) -> DiagnosisJob | None:
        return self._load(job_id)

    def qsize(self) -> int:
        return int(self._r.llen(self._queue_key))

    # ── worker(serve-worker)──────────────────────────────────
    def consume(self, timeout: int = 1) -> str | None:
        """阻塞取一个 job_id(BRPOP);超时返回 None。"""
        item = self._r.brpop([self._queue_key], timeout=timeout)
        if item is None:
            return None
        return item[1]  # (key, value)

    def mark_running(self, job_id: str) -> None:
        self._r.hset(self._job_key(job_id), "status", RUNNING)

    def complete(self, job_id: str, result: dict) -> None:
        key = self._job_key(job_id)
        self._r.hset(key, mapping={"status": SUCCEEDED, "result": json.dumps(result), "error": ""})

    def fail_or_retry(self, job_id: str, error: str) -> str:
        """失败处理:未超重试上限 → 重新入队(返回 'retried');否则 → DLQ(返回 'dead')。"""
        key = self._job_key(job_id)
        attempts = int(self._r.hincrby(key, "attempts", 1))
        if attempts <= self._max_retries:
            self._r.hset(key, mapping={"status": QUEUED, "error": error})
            self._r.lpush(self._queue_key, job_id)
            return "retried"
        self._r.hset(key, mapping={"status": FAILED, "error": error})
        self._r.lpush(self._dlq_key, job_id)
        return "dead"

    # ── DLQ 运维 ──────────────────────────────────────────────
    def dlq_list(self, limit: int = 100) -> list[str]:
        return list(self._r.lrange(self._dlq_key, 0, max(0, limit - 1)))

    def dlq_size(self) -> int:
        return int(self._r.llen(self._dlq_key))

    def dlq_requeue(self, job_id: str) -> bool:
        """把一个死信任务移回主队列(重置 attempts)。"""
        removed = self._r.lrem(self._dlq_key, 1, job_id)
        if not removed:
            return False
        reset = {"status": QUEUED, "attempts": "0", "error": ""}
        self._r.hset(self._job_key(job_id), mapping=reset)
        self._r.lpush(self._queue_key, job_id)
        return True

    def close(self) -> None:
        import contextlib

        with contextlib.suppress(Exception):  # 关闭尽力而为
            self._r.close()
