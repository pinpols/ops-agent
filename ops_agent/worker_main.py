"""独立 worker 进程(架构演进 Step 2):消费 Redis 队列 → 跑诊断 → 写结果 / 重试 / DLQ。

与 ingress(`serve`)解耦的独立进程,可起多份做水平扩展;崩溃/重启可恢复(状态在 Redis)。
进程内再起 `OPS_WORKER_COUNT` 个消费线程提升单进程并发。SIGINT/SIGTERM 优雅停机。
"""

import logging
import signal
import threading
from typing import Any

from ops_agent.config import Settings, get_settings
from ops_agent.redisqueue import RedisQueue

logger = logging.getLogger("ops_agent.worker")


def build_redis_queue(settings: Settings) -> RedisQueue:
    if settings.ops_queue_backend != "redis" or not settings.ops_redis_url:
        raise ValueError("worker 需 OPS_QUEUE_BACKEND=redis + OPS_REDIS_URL")
    return RedisQueue.from_url(
        settings.ops_redis_url,
        queue_key=settings.ops_queue_key,
        dlq_key=settings.ops_dlq_key,
        max_queue=settings.ops_queue_max,
        job_ttl=settings.ops_job_ttl_seconds,
        max_retries=settings.ops_max_retries,
        retry_base_seconds=settings.ops_retry_base_seconds,
        retry_max_seconds=settings.ops_retry_max_seconds,
        queue_depth_alert_threshold=settings.ops_queue_depth_alert_threshold,
    )


def process_once(rq: RedisQueue, handler: Any, timeout: int = 1) -> str | None:
    """取一个任务并处理。无任务返回 None;否则返回最终 outcome(succeeded/retried/dead)。"""
    job_id = rq.consume(timeout=timeout)
    if job_id is None:
        return None
    job = rq.get(job_id)
    if job is None:
        # 取到了 job_id 但 hash 已不在(TTL 过期/被驱逐)→ 任务丢失。出队即静默跳过会"无声丢单",
        # 必须留痕 + 计指标,便于告警/排查。
        from ops_agent.metrics import METRICS

        logger.warning("job_id=%s 出队但 hash 缺失(TTL过期/驱逐),丢弃", job_id)
        METRICS.inc("jobs_lost_total")
        return None
    rq.mark_running(job_id)
    try:
        logger.info("处理 job_id=%s trace_id=%s", job_id, job.trace_id)
        result = handler(job)
        rq.complete(job_id, result)
        return "succeeded"
    except Exception as exc:  # noqa: BLE001 - worker 边界:失败转重试/DLQ,不崩线程
        outcome = rq.fail_or_retry(job_id, f"{type(exc).__name__}: {exc}")
        logger.warning("job %s trace_id=%s 失败 → %s: %s", job_id, job.trace_id, outcome, exc)
        return outcome


def _worker_loop(rq: RedisQueue, handler: Any, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            process_once(rq, handler, timeout=1)
        except Exception:  # noqa: BLE001 - 消费循环自身异常(如 Redis 抖动)不该终结 worker
            logger.exception("worker 循环异常,继续")


def run(settings: Settings | None = None) -> None:
    """启动 worker:N 个消费线程 + 信号优雅停机(阻塞直到收到停止信号)。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = settings or get_settings()
    rq = build_redis_queue(settings)
    # 延迟 import:复用 ingress 端的只读诊断 handler(注入全拒审批闸 + 回调)。
    from ops_agent.server import diagnosis_job_handler

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    threads = [
        threading.Thread(
            target=_worker_loop, args=(rq, diagnosis_job_handler, stop), name=f"worker-{i}"
        )
        for i in range(max(1, settings.ops_worker_count))
    ]
    for t in threads:
        t.start()
    logger.info("worker 启动:threads=%d queue=%s", len(threads), settings.ops_queue_key)
    try:
        while not stop.is_set():
            stop.wait(1)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=10)
        rq.close()
        logger.info("worker 优雅退出")


def main() -> None:
    run()


if __name__ == "__main__":
    main()
