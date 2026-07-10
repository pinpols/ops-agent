"""Redis 队列后端(架构演进 Step 2:真队列 + 独立 worker 进程 + DLQ/重试)。

与进程内 `JobQueue` 对外接口对齐(`submit`/`get`),但队列与任务状态都落 Redis,
因此 **ingress 进程**(serve)与 **worker 进程**(serve-worker)跨进程共享同一份状态,
worker 崩溃/重启可恢复,可水平扩展。

数据布局:
- 队列:Redis LIST `<queue_key>`(ingress LPUSH,worker BLMOVE 到 processing list)。
- 在途:Redis LIST `<queue_key>:processing:<worker_id>`(per-worker 在途登记,崩溃可回收)。
- 心跳:Redis ZSET `<queue_key>:workers`(worker_id → 最近心跳时间,reaper 判活)。
- 任务:Redis HASH `<ns>:job:<id>`(question/target/status/result/error/created_at/attempts/
  running_since/worker),带 TTL。
- 死信:Redis LIST `<dlq_key>`(超重试上限的 job_id)。

**at-least-once 语义**(ADR-0003):出队用 LMOVE 而非破坏性 BRPOP —— worker 硬崩
(SIGKILL/OOM/滚动发布超 grace period)后,在途任务仍留在它的 processing list;
`reap()`(worker 启动时 + 周期性)把死 worker 的在途任务经 `fail_or_retry` 回灌主队列或 DLQ,
另兜底回收 RUNNING 超 `stale_running_seconds` 的卡死任务。任务不再无声蒸发到 24h TTL。

`redis` 是**可选依赖**(`pip install ops-agent[redis]`),仅本模块延迟 import;memory 后端不需要它。
"""

import json
import logging
import os
import socket
import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from ops_agent.config import Settings

from ops_agent.jobqueue import FAILED, QUEUED, RUNNING, SUCCEEDED, DiagnosisJob
from ops_agent.metrics import METRICS

logger = logging.getLogger("ops_agent.redisqueue")

_JOB_PREFIX = "ops:job:"


def _default_worker_id() -> str:
    """worker 身份:主机名+pid+随机尾缀 —— 重启后是新身份,旧 processing list 由 reaper 回收。"""
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


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
        worker_id: str | None = None,
        worker_dead_after_seconds: float = 60.0,
        stale_running_seconds: float = 240.0,
        audit_log: "Path | None" = None,
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
        # P1-1 崩溃安全消费:per-worker processing list + 心跳 zset + reaper 参数
        self._worker_id = worker_id or _default_worker_id()
        self._processing_prefix = f"{queue_key}:processing:"
        self._processing_key = self._processing_prefix + self._worker_id
        self._workers_key = f"{queue_key}:workers"
        self._worker_dead_after_seconds = max(1.0, worker_dead_after_seconds)
        self._stale_running_seconds = max(1.0, stale_running_seconds)
        # P2-8:队列运维动作(DLQ 回灌/reaper 回收)审计链;未配则不留痕(测试/嵌入场景)
        self._audit_log = audit_log

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> "RedisQueue":
        import redis  # 延迟 import:memory 后端无需 redis 依赖

        client = redis.from_url(url, decode_responses=True)
        return cls(client, **kwargs)

    @classmethod
    def from_settings(cls, settings: "Settings") -> "RedisQueue":
        """从 Settings 一处映射构造 —— 收口 server/worker/cli 三处重复的参数拼装(DRY 工厂)。"""
        if settings.ops_queue_backend != "redis" or not settings.ops_redis_url:
            raise ValueError("Redis 队列需 OPS_QUEUE_BACKEND=redis + OPS_REDIS_URL")
        return cls.from_url(
            settings.ops_redis_url,
            queue_key=settings.ops_queue_key,
            dlq_key=settings.ops_dlq_key,
            max_queue=settings.ops_queue_max,
            job_ttl=settings.ops_job_ttl_seconds,
            max_retries=settings.ops_max_retries,
            retry_base_seconds=settings.ops_retry_base_seconds,
            retry_max_seconds=settings.ops_retry_max_seconds,
            queue_depth_alert_threshold=settings.ops_queue_depth_alert_threshold,
            worker_dead_after_seconds=settings.ops_worker_dead_after_seconds,
            stale_running_seconds=settings.ops_stale_running_seconds,
            audit_log=settings.ops_approval_log,
        )

    # ── 序列化 ────────────────────────────────────────────────
    def _job_key(self, job_id: str) -> str:
        return _JOB_PREFIX + job_id

    def _mapping(self, job: DiagnosisJob) -> dict[str, str]:
        return {
            "question": job.question,
            "trace_id": job.trace_id,
            "target": job.target or "",
            "actor": job.actor or "",
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
            actor=h.get("actor") or None,
            status=h.get("status", QUEUED),
            result=json.loads(h["result"]) if h.get("result") else None,
            error=h.get("error") or None,
            created_at=float(h.get("created_at", "0") or 0),
            attempts=int(h.get("attempts", "0") or 0),
        )

    # ── ingress(serve)────────────────────────────────────────
    def submit(
        self,
        question: str,
        target: str | None = None,
        trace_id: str | None = None,
        actor: str | None = None,
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
            actor=actor,
        )
        job_key = self._job_key(job.id)
        mapping = self._mapping(job)
        from redis.exceptions import WatchError  # 延迟 import:redis 是可选依赖

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
        """阻塞取一个 job_id;超时返回 None。

        **崩溃安全**(P1-1):BLMOVE(RIGHT→LEFT)把 job_id 原子挪进本 worker 的
        processing list,而非 BRPOP 破坏性出队 —— worker 硬崩后在途任务可被 reaper 回收。
        顺带续心跳(reaper 据此判本 worker 存活)。
        """
        self.promote_due_retries()
        self.heartbeat()
        job_id = self._r.blmove(self._queue_key, self._processing_key, timeout, "RIGHT", "LEFT")
        self.update_queue_metrics()
        return job_id

    def heartbeat(self, now: float | None = None) -> None:
        """续 worker 心跳(zset:worker_id → 时间戳)。consume 每次自动调用。"""
        self._r.zadd(self._workers_key, {self._worker_id: time.time() if now is None else now})

    def discard(self, job_id: str) -> None:
        """把一个 job_id 从本 worker 的 processing list 摘除(幽灵 id / 已判 lost 用)。"""
        self._r.lrem(self._processing_key, 1, job_id)

    def mark_running(self, job_id: str) -> bool:
        """标记 RUNNING + 记 running_since/worker(reaper 判卡死依据)。

        **存在守卫**(P2-2):hash 已 TTL 过期/驱逐时,裸 hset 会重建一个无 question、
        无 TTL 的僵尸 —— 这里判 lost(计 jobs_lost_total)并清 processing 登记,不执行。

        **终态守卫**(P1-2):job 被 reaper 抢走重入队、原 worker 已写 SUCCEEDED/FAILED 后,
        第二个 worker 取到同 id —— 旧实现只查存在性,会把终态改回 RUNNING 重跑;
        这里遇终态即清 processing 登记并返回 False(调用方跳过执行)。
        """
        key = self._job_key(job_id)
        from redis.exceptions import WatchError  # 延迟 import:redis 是可选依赖

        with self._r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(key)
                    status = pipe.hget(key, "status")
                    if status is None:
                        pipe.unwatch()
                        METRICS.inc("jobs_lost_total")
                        self.discard(job_id)
                        return False
                    if status in (SUCCEEDED, FAILED):
                        pipe.unwatch()
                        logger.warning(
                            "mark_running 拒绝已终态任务 job_id=%s status=%s(重复投递副本,跳过)",
                            job_id,
                            status,
                        )
                        self.discard(job_id)
                        return False
                    pipe.multi()
                    pipe.hset(
                        key,
                        mapping={
                            "status": RUNNING,
                            "running_since": str(time.time()),
                            "worker": self._worker_id,
                        },
                    )
                    pipe.execute()
                    return True
                except WatchError:
                    continue

    def complete(self, job_id: str, result: dict) -> bool:
        """标记成功。**原子 + 终态守卫**:WATCH 状态 → 仅当未终态/未丢失才写。

        避免迟到的 complete 覆写一个已被(重复投递的)另一 worker 写成 FAILED 的 job,
        也避免在 hash 已 TTL 过期后用 hset 重建一个无 question/无 TTL 的僵尸。

        返回是否**真正写入了 SUCCEEDED**(P1-3):调用方据此决定要不要投递 succeeded
        回调 —— 被守卫拒绝时本方不是第一个终态写入者,发 succeeded 会给下游乱序终态。
        """
        key = self._job_key(job_id)
        from redis.exceptions import WatchError  # 延迟 import:redis 是可选依赖

        with self._r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(key)
                    status = pipe.hget(key, "status")
                    if status is None or status in (SUCCEEDED, FAILED):
                        pipe.unwatch()
                        self.discard(job_id)  # 在途登记别悬空
                        return False  # 哈希已丢失 / 已终态 → 不覆写、不复活
                    pipe.multi()
                    pipe.hset(
                        key,
                        mapping={"status": SUCCEEDED, "result": json.dumps(result), "error": ""},
                    )
                    pipe.lrem(self._processing_key, 1, job_id)  # 在途登记随终态同事务摘除
                    pipe.execute()
                    break
                except WatchError:
                    continue
        METRICS.inc("jobs_succeeded_total")
        self.update_queue_metrics()
        return True

    def fail_or_retry(
        self,
        job_id: str,
        error: str,
        *,
        retryable: bool = True,
        expected_running_since: str | None = None,
    ) -> str:
        """失败处理:未超上限 → 重入队('retried');超限或不可重试 → DLQ('dead');哈希丢失 → 'lost'。

        **并发回收守卫**(P2-5①):传 `expected_running_since`(reaper 观测到的值)时,
        仅当任务仍 RUNNING 且 running_since 未变才执行,否则返回 'skipped' ——
        两个 reaper 同窗回收同一 stale job 时只有一个生效,attempts 不双增、不双入队;
        任务已被新 worker 领走重跑(running_since 刷新)时也不误伤。

        **原子 + 终态/存在守卫**(WATCH/MULTI):
        - 旧实现 `hincrby` 在 hash 已 TTL 过期时会**重建一个无 TTL、丢了 question 的僵尸 job**
          并被当空诊断处理 —— 这里先 WATCH+读状态,缺失则判 lost、不复活。
        - 旧实现无终态守卫:已 SUCCEEDED 的 job 被迟到 retry 覆写回 QUEUED → 重复诊断 + 状态翻转。
        - hincrby/hset/zadd 三步非原子 → 改为 MULTI 单事务;processing 登记随终态同事务摘除。

        `retryable=False`(P2-4):确定性失败(预算耗尽/max_steps)直接判 dead 进 DLQ,
        不浪费重试预算重复烧钱。终局失败(dead)才投递 failed 回调(P2-3)。
        """
        key = self._job_key(job_id)
        from redis.exceptions import WatchError  # 延迟 import:redis 是可选依赖

        with self._r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(key)
                    status = pipe.hget(key, "status")
                    if status is None:
                        pipe.unwatch()
                        METRICS.inc("jobs_lost_total")
                        self.discard(job_id)
                        return "lost"  # 哈希已丢失(TTL/驱逐)→ 不用 hincrby 复活僵尸
                    if status in (SUCCEEDED, FAILED):
                        pipe.unwatch()
                        self.discard(job_id)
                        return "dead" if status == FAILED else "succeeded"  # 已终态,不重处理
                    if expected_running_since is not None:
                        current = pipe.hget(key, "running_since")
                        if status != RUNNING or current != expected_running_since:
                            pipe.unwatch()
                            return "skipped"  # 已被别的 reaper 回收 / 已被新 worker 领走
                    attempts = int(pipe.hget(key, "attempts") or 0) + 1
                    if retryable and attempts <= self._max_retries:
                        retry_after = time.time() + self._retry_delay(attempts)
                        pipe.multi()
                        pipe.hset(
                            key,
                            mapping={
                                "attempts": str(attempts),
                                "status": QUEUED,
                                "error": error,
                                "retry_after": str(retry_after),
                            },
                        )
                        pipe.zadd(self._retry_key, {job_id: retry_after})
                        pipe.lrem(self._processing_key, 1, job_id)
                        pipe.execute()
                        outcome, metric = "retried", "jobs_retried_total"
                    else:
                        pipe.multi()
                        pipe.hset(
                            key,
                            mapping={"attempts": str(attempts), "status": FAILED, "error": error},
                        )
                        pipe.lpush(self._dlq_key, job_id)
                        pipe.lrem(self._processing_key, 1, job_id)
                        pipe.execute()
                        outcome, metric = "dead", "jobs_failed_total"
                    break
                except WatchError:
                    continue
        METRICS.inc(metric)
        self.update_queue_metrics()
        if outcome == "dead":
            self._notify_failed(job_id)  # P2-3:只在终局失败投递 failed 回调(带 attempts)
        return outcome

    def _notify_failed(self, job_id: str) -> None:
        """终局失败回调(best-effort):dead 才投递,避免"先 failed 后 succeeded"的乱序信号。"""
        try:
            from ops_agent.callback import _post_callback

            job = self._load(job_id)
            if job is not None:
                _post_callback(job, status="failed", error=job.error)
        except Exception as exc:  # noqa: BLE001 - 回调失败不影响队列状态机
            logger.warning("终局失败回调投递异常 job_id=%s: %s", job_id, exc)

    def _retry_delay(self, attempts: int) -> float:
        if self._retry_base_seconds <= 0:
            return 0.0
        return min(self._retry_max_seconds, self._retry_base_seconds * (2 ** max(0, attempts - 1)))

    def promote_due_retries(self, now: float | None = None) -> int:
        """把到期重试任务从 delayed zset 移回主队列。返回提升数量。

        **原子**(P1-2):旧实现 zrem→lpush 两步裸调,zrem 成功后崩溃/断连 → 任务从 zset
        消失且未入队(永久丢失)。改 WATCH retry_key + MULTI(zrem+lpush)单事务:
        要么整体提交(全部入队),要么整体不发生(全部留在 zset 等下一轮),崩溃窗口不丢任务;
        并发 worker 同扫时 WATCH 冲突方重试,不会双份入队。
        """
        now = time.time() if now is None else now
        from redis.exceptions import WatchError  # 延迟 import:redis 是可选依赖

        with self._r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(self._retry_key)
                    job_ids = list(pipe.zrangebyscore(self._retry_key, 0, now))
                    if not job_ids:
                        pipe.unwatch()
                        return 0
                    pipe.multi()
                    pipe.zrem(self._retry_key, *job_ids)
                    for job_id in job_ids:
                        pipe.lpush(self._queue_key, job_id)
                    pipe.execute()
                    promoted = len(job_ids)
                    break
                except WatchError:
                    continue  # 并发 worker 抢先提升 → 重读 zset(乐观锁,不双入队)
        self.update_queue_metrics()
        return promoted

    # ── reaper(P1-1:崩溃回收)────────────────────────────────
    def reap(self, now: float | None = None) -> dict[str, int]:
        """回收两类残留(worker 启动时 + 周期性调用):

        ① **死 worker 的 processing list**:心跳超 `worker_dead_after_seconds` 判死,
           在途任务逐个经 `fail_or_retry` 回灌主队列(计 attempts)或进 DLQ;hash 已蒸发计 lost。
        ② **RUNNING 卡死兜底**:worker 心跳还在但单任务 running_since 超
           `stale_running_seconds`(默认 2×OPS_MAX_RUN_SECONDS)→ 同样走 fail_or_retry。
        """
        now = time.time() if now is None else now
        stats = {"requeued": 0, "dead": 0, "lost": 0}
        # ① 死 worker 在途回收
        for key in list(self._r.scan_iter(match=self._processing_prefix + "*")):
            wid = key[len(self._processing_prefix) :]
            if wid == self._worker_id:
                continue  # 自己的在途由自己收口
            if self._worker_alive(wid, now):
                continue  # 心跳新鲜,worker 活着
            # P0-1③:判死后、每次回收前都再核对心跳 —— 缩小"worker 只是忙/刚复活"的竞态窗,
            # 也防 drain 循环把复活 worker 新领的任务一并抢走。
            revived = False
            while True:
                if self._worker_alive(wid, now):
                    revived = True
                    logger.warning("reaper 中止回收:worker=%s 心跳已恢复", wid)
                    break
                job_id = self._r.rpop(key)
                if job_id is None:
                    break
                logger.warning("reaper 回收死 worker=%s 在途任务 job_id=%s", wid, job_id)
                self._reap_one(job_id, stats, error=f"worker_crashed:{wid}")
            if not revived:
                self._r.zrem(self._workers_key, wid)
        # ② RUNNING 卡死兜底(含 worker 活着但任务卡死/心跳键丢失等)
        for jkey in list(self._r.scan_iter(match=_JOB_PREFIX + "*")):
            status, running_since, worker = self._r.hmget(jkey, "status", "running_since", "worker")
            if status != RUNNING or not running_since:
                continue
            if now - float(running_since) <= self._stale_running_seconds:
                continue
            job_id = jkey[len(_JOB_PREFIX) :]
            if worker:
                self._r.lrem(self._processing_prefix + worker, 1, job_id)
            logger.warning("reaper 回收 RUNNING 卡死任务 job_id=%s worker=%s", job_id, worker)
            # P2-5①:带上观测到的 running_since —— 并发 reaper 只有一个能真正回收
            self._reap_one(
                job_id, stats, error="stale_running_timeout", expected_running_since=running_since
            )
        if any(stats.values()):
            METRICS.inc("jobs_reaped_total", sum(stats.values()))
            self.update_queue_metrics()
        return stats

    def _worker_alive(self, wid: str, now: float) -> bool:
        score = self._r.zscore(self._workers_key, wid)
        return score is not None and now - float(score) < self._worker_dead_after_seconds

    def _reap_one(
        self,
        job_id: str,
        stats: dict[str, int],
        *,
        error: str,
        expected_running_since: str | None = None,
    ) -> None:
        if self._load(job_id) is None:
            METRICS.inc("jobs_lost_total")
            stats["lost"] += 1
            return
        outcome = self.fail_or_retry(
            job_id, error, expected_running_since=expected_running_since
        )
        if outcome == "retried":
            stats["requeued"] += 1
        elif outcome == "dead":
            stats["dead"] += 1
        elif outcome == "lost":
            # hash 在上面 _load 与 fail_or_retry 之间蒸发(P1-4③):jobs_lost_total 已由
            # fail_or_retry 计数,这里补 stats,否则 reaper 日志/汇总漏报丢单
            stats["lost"] += 1
        if outcome != "skipped":
            # P2-8:reaper 改变任务命运(回灌/判死/丢失)要留审计痕;skipped=没动它,不记
            self._audit_queue_event("QUEUE_REAP", job_id, outcome, detail=error)

    def retry_size(self) -> int:
        return int(self._r.zcard(self._retry_key))

    # ── DLQ 运维 ──────────────────────────────────────────────
    def dlq_list(self, limit: int = 100) -> list[str]:
        return list(self._r.lrange(self._dlq_key, 0, max(0, limit - 1)))

    def dlq_size(self) -> int:
        return int(self._r.llen(self._dlq_key))

    def dlq_requeue(self, job_id: str) -> bool:
        """把一个死信任务移回主队列(重置 attempts)。

        **原子 + 存在守卫**(P2-2):WATCH dlq+job hash → 确认 job 在 dlq **且 hash 仍含
        question** → MULTI(lrem+hset+expire+lpush)EXEC。守住三个坑:
        - "从 dlq 删除但未回主队列"的丢失;
        - lrem 删 0 却仍 hset/lpush(非死信任务误入队);
        - hash 已 TTL 蒸发时裸 hset 重建**无 question、无 TTL 的僵尸**并入队 ——
          此时清掉悬空死信条目、计 jobs_lost_total、返回 False;正常路径重入队必补 TTL。
        """
        job_key = self._job_key(job_id)
        reset = {"status": QUEUED, "attempts": "0", "error": ""}
        from redis.exceptions import WatchError  # 延迟 import:redis 是可选依赖

        with self._r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(self._dlq_key, job_key)
                    if pipe.lpos(self._dlq_key, job_id) is None:
                        pipe.reset()
                        return False  # 不在 dlq
                    if not pipe.hget(job_key, "question"):
                        # hash 已蒸发/缺 question:重建即僵尸 → 清悬空条目,判 lost
                        pipe.multi()
                        pipe.lrem(self._dlq_key, 1, job_id)
                        pipe.execute()
                        METRICS.inc("jobs_lost_total")
                        self.update_queue_metrics()
                        return False
                    pipe.multi()
                    pipe.lrem(self._dlq_key, 1, job_id)
                    pipe.hset(job_key, mapping=reset)
                    pipe.expire(job_key, self._job_ttl)  # 重入队续 TTL,防处理中蒸发
                    pipe.lpush(self._queue_key, job_id)
                    pipe.execute()
                    break
                except WatchError:
                    continue
        self.update_queue_metrics()
        self._audit_queue_event("QUEUE_REQUEUE", job_id, "requeued")  # P2-8:DLQ 回灌留痕
        return True

    def _audit_queue_event(
        self, event: str, job_id: str, outcome: str, detail: str | None = None
    ) -> None:
        """队列运维动作写审计 hash chain(P2-8,best-effort:审计失败不影响队列状态机)。"""
        if self._audit_log is None:
            return
        try:
            from ops_agent.audit import append_queue_record

            append_queue_record(
                self._audit_log, event=event, job_id=job_id, outcome=outcome, detail=detail
            )
        except Exception as exc:  # noqa: BLE001 - 审计尽力而为,不阻断回收/回灌
            logger.warning("队列审计写入失败 event=%s job_id=%s: %s", event, job_id, exc)

    def close(self) -> None:
        import contextlib

        with contextlib.suppress(Exception):  # P3:优雅退出摘除自己的心跳,别让 reaper
            self._r.zrem(self._workers_key, self._worker_id)  # 在 dead_after 窗口内误当活 worker
        with contextlib.suppress(Exception):  # 关闭尽力而为
            self._r.close()
