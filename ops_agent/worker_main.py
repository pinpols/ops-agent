"""独立 worker 进程(架构演进 Step 2):消费 Redis 队列 → 跑诊断 → 写结果 / 重试 / DLQ。

与 ingress(`serve`)解耦的独立进程,可起多份做水平扩展;崩溃/重启可恢复(状态在 Redis)。
进程内再起 `OPS_WORKER_COUNT` 个消费线程提升单进程并发。SIGINT/SIGTERM 优雅停机。
"""

import logging
import signal
import threading
import time
from pathlib import Path
from typing import Any

from ops_agent.config import Settings, get_settings
from ops_agent.queue_backend import WorkerQueue
from ops_agent.redisqueue import RedisQueue

logger = logging.getLogger("ops_agent.worker")


def build_redis_queue(settings: Settings) -> RedisQueue:
    return RedisQueue.from_settings(settings)


def process_once(rq: WorkerQueue, handler: Any, timeout: int = 1) -> str | None:
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
    from ops_agent.metrics import METRICS

    METRICS.add("workers_busy", 1, backend="redis")  # 在途 worker 数(利用率分子)
    started = time.monotonic()
    try:
        logger.info("处理 job_id=%s trace_id=%s", job_id, job.trace_id)
        result = handler(job)
        rq.complete(job_id, result)
        return "succeeded"
    except Exception as exc:  # noqa: BLE001 - worker 边界:失败转重试/DLQ,不崩线程
        outcome = rq.fail_or_retry(job_id, f"{type(exc).__name__}: {exc}")
        logger.warning("job %s trace_id=%s 失败 → %s: %s", job_id, job.trace_id, outcome, exc)
        return outcome
    finally:
        METRICS.observe("job_duration_seconds", time.monotonic() - started, backend="redis")
        METRICS.add("workers_busy", -1, backend="redis")


def _touch_heartbeat(path: Path | None) -> None:
    """更新心跳文件 mtime;失败不致命(只影响 liveness 信号,不该崩 worker)。"""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError as exc:  # 心跳尽力而为
        logger.warning("心跳文件写入失败 %s: %s", path, exc)


def _worker_loop(rq: WorkerQueue, handler: Any, stop: threading.Event) -> None:
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
    # 复用诊断任务内核(注入全拒审批闸 + 回调)。直接 import jobs,解掉 worker→server 反向依赖。
    from ops_agent.jobs import diagnosis_job_handler
    from ops_agent.metrics import METRICS

    worker_count = max(1, settings.ops_worker_count)
    METRICS.set("workers_total", worker_count, backend="redis")  # 利用率分母
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    threads = [
        threading.Thread(
            target=_worker_loop, args=(rq, diagnosis_job_handler, stop), name=f"worker-{i}"
        )
        for i in range(worker_count)
    ]
    for t in threads:
        t.start()
    logger.info("worker 启动:threads=%d queue=%s", len(threads), settings.ops_queue_key)
    heartbeat = settings.ops_worker_heartbeat_file
    try:
        while not stop.is_set():
            _touch_heartbeat(heartbeat)  # k8s liveness 据此 mtime 判存活(检出僵死主循环)
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
