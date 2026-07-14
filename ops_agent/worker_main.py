"""独立 worker 进程(架构演进 Step 2):消费 Redis 队列 → 跑诊断 → 写结果 / 重试 / DLQ。

与 ingress(`serve`)解耦的独立进程,可起多份做水平扩展;崩溃/重启可恢复(状态在 Redis)。
进程内再起 `OPS_WORKER_COUNT` 个消费线程提升单进程并发,外加一个 reaper 线程
(P1-1:回收硬崩 worker 的在途任务)。SIGINT/SIGTERM 优雅停机,排空窗口覆盖单次 run 预算。
"""

import logging
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ops_agent.budget import is_non_retryable
from ops_agent.config import Settings, get_settings
from ops_agent.metrics import METRICS
from ops_agent.queue_backend import WorkerQueue
from ops_agent.redisqueue import RedisQueue

logger = logging.getLogger("ops_agent.worker")

_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_MAX_SECONDS = 30.0


class _WorkerMetricsHandler(BaseHTTPRequestHandler):
    server_version = "ops-agent-worker"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, "ok\n", "text/plain")
            return
        if self.path == "/metrics":
            self._send(200, METRICS.render(), "text/plain; version=0.0.4")
            return
        self._send(404, "not_found\n", "text/plain")

    def _send(self, status: int, body: str, content_type: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _start_worker_metrics_server(port: int) -> ThreadingHTTPServer | None:
    if port <= 0:
        return None
    # Container scrape endpoint; NetworkPolicy/Service controls exposure.
    httpd = ThreadingHTTPServer(("0.0.0.0", port), _WorkerMetricsHandler)  # nosec B104
    thread = threading.Thread(target=httpd.serve_forever, name="worker-metrics", daemon=True)
    thread.start()
    logger.info("worker metrics on :%d", port)
    return httpd


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
        # 必须留痕 + 计指标 + 清 processing 登记(否则幽灵 id 永挂在途),便于告警/排查。
        logger.warning("job_id=%s 出队但 hash 缺失(TTL过期/驱逐),丢弃", job_id)
        METRICS.inc("jobs_lost_total")
        discard = getattr(rq, "discard", None)
        if discard is not None:
            discard(job_id)
        return None
    if not rq.mark_running(job_id):
        return None  # hash 在 get 与 mark 之间蒸发(P2-2 守卫已计 lost + 清登记)
    METRICS.add("workers_busy", 1, backend="redis")  # 在途 worker 数(利用率分子)
    started = time.monotonic()
    try:
        logger.info("处理 job_id=%s trace_id=%s", job_id, job.trace_id)
        result = handler(job)
        if not rq.complete(job_id, result):
            # 任务已被另一方(reaper/重复副本)写成终态 → 本方结果作废,不发 succeeded(P1-3)
            logger.warning("job %s trace_id=%s 完成时已被判终态,结果丢弃", job_id, job.trace_id)
            return "superseded"
        if not hasattr(rq, "deliver_due_callbacks"):
            _notify_succeeded(job, result)
        return "succeeded"
    except Exception as exc:  # noqa: BLE001 - worker 边界:失败转重试/DLQ,不崩线程
        # P2-4:确定性失败(预算耗尽/max_steps 绕圈)重试注定同样结局,直接判 dead 进 DLQ
        retryable = not is_non_retryable(exc)
        outcome = rq.fail_or_retry(job_id, f"{type(exc).__name__}: {exc}", retryable=retryable)
        logger.warning(
            "job %s trace_id=%s 失败(retryable=%s)→ %s: %s",
            job_id,
            job.trace_id,
            retryable,
            outcome,
            exc,
        )
        return outcome
    finally:
        METRICS.observe("job_duration_seconds", time.monotonic() - started, backend="redis")
        METRICS.add("workers_busy", -1, backend="redis")


def _flush_metrics_file(path: Path | None) -> None:
    """把累计指标原子落成 textfile(P1-4①,best-effort)。

    complete 的 jobs_succeeded_total、finally 的 workers_busy-1、reaper 的
    jobs_reaped/jobs_lost 都发生在 run_agent 内部 flush **之后** —— 只靠 run_agent
    落盘,这些指标永远停在上一轮快照。消费循环/reaper 每轮结束补一次 flush,
    空闲轮也 flush(否则 worker 闲下来后 workers_busy 等 gauge 冻结在旧值)。
    """
    if path is None:
        return
    try:
        METRICS.write_textfile(path)
    except OSError as exc:
        logger.warning("指标 textfile 写入失败 %s: %s", path, exc)


def _notify_succeeded(job: Any, result: dict) -> None:
    """succeeded 回调(P1-3,best-effort):只在 complete 确认本方是第一个终态写入者后投递。"""
    try:
        from ops_agent.callback import _post_callback

        _post_callback(job, status="succeeded", result=result)
    except Exception as exc:  # noqa: BLE001 - 回调失败不影响队列状态机
        logger.warning("succeeded 回调投递异常 job_id=%s: %s", job.id, exc)


def _touch_heartbeat(path: Path | None) -> None:
    """更新心跳文件 mtime;失败不致命(只影响 liveness 信号,不该崩 worker)。"""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError as exc:  # 心跳尽力而为
        logger.warning("心跳文件写入失败 %s: %s", path, exc)


def _worker_loop(
    rq: WorkerQueue,
    handler: Any,
    stop: threading.Event,
    metrics_file: Path | None = None,
) -> None:
    """消费循环。循环自身异常(如 Redis 断连)不终结 worker,但要**指数退避 + 日志限频**
    (P2-5):否则断连期间每秒热旋重连打满 CPU、异常栈刷爆日志。退避用 stop.wait 实现,
    停机信号能立刻打断等待。每轮结束 flush 指标 textfile(P1-4①,含空闲轮)。"""
    failures = 0
    while not stop.is_set():
        try:
            process_once(rq, handler, timeout=1)
            failures = 0
        except Exception:  # noqa: BLE001 - 消费循环自身异常(如 Redis 抖动)不该终结 worker
            failures += 1
            delay = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** (failures - 1)))
            # 日志限频:前 3 次全记(保留现场),之后每 10 次记 1 条(断连风暴不刷日志)
            if failures <= 3 or failures % 10 == 0:
                logger.exception("worker 循环异常(连续 %d 次),%.1fs 后重试", failures, delay)
            stop.wait(delay)
        finally:
            _flush_metrics_file(metrics_file)


def _heartbeat_loop(rq: Any, stop: threading.Event, interval: float) -> None:
    """独立心跳线程(P0-1①):旧实现只在 consume() 续心跳,handler 忙跑 max_run(可达
    120s+)期间整进程无心跳,dead_after 一过就被对面 reaper 判死、在途任务被抢走 →
    双执行 + 双回调。心跳独立于消费循环续,handler 再忙也不会"假死"。"""
    while not stop.is_set():
        try:
            rq.heartbeat()
        except Exception:  # noqa: BLE001 - Redis 抖动不终结心跳线程,下一轮重试
            logger.warning("worker 心跳续约失败,下一轮重试", exc_info=True)
        stop.wait(interval)


def _reaper_loop(
    rq: Any, stop: threading.Event, interval: float, metrics_file: Path | None = None
) -> None:
    """崩溃回收循环(P1-1):启动先 reap 一次(接管上任 worker 的遗留),之后周期扫。
    每轮结束 flush 指标 textfile(P1-4①:jobs_reaped/jobs_lost 更新后立即可抓)。"""
    failures = 0
    while not stop.is_set():
        try:
            stats = rq.reap()
            failures = 0
            if isinstance(stats, dict) and any(stats.values()):
                logger.warning(
                    "reaper 回收:requeued=%s dead=%s lost=%s",
                    stats.get("requeued"),
                    stats.get("dead"),
                    stats.get("lost"),
                )
        except Exception:  # noqa: BLE001 - reaper 异常不终结 worker,下一轮重试
            failures += 1
            if failures <= 3 or failures % 10 == 0:
                logger.exception("reaper 异常(连续 %d 次)", failures)
        _flush_metrics_file(metrics_file)
        stop.wait(interval)


def _callback_loop(rq: Any, stop: threading.Event, interval: float) -> None:
    """可靠回调 outbox 投递循环。RedisQueue 支持;其他后端无该方法则 no-op。"""
    deliver = getattr(rq, "deliver_due_callbacks", None)
    if deliver is None:
        return
    failures = 0
    while not stop.is_set():
        try:
            deliver(limit=25)
            failures = 0
        except Exception:  # noqa: BLE001 - 回调投递异常不终结 worker
            failures += 1
            if failures <= 3 or failures % 10 == 0:
                logger.exception("callback outbox 循环异常(连续 %d 次)", failures)
        stop.wait(interval)


def run(settings: Settings | None = None) -> None:
    """启动 worker:N 个消费线程 + reaper + 信号优雅停机(阻塞直到收到停止信号)。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = settings or get_settings()
    rq = build_redis_queue(settings)
    metrics_httpd = _start_worker_metrics_server(settings.ops_worker_metrics_port)
    # 复用诊断任务内核(注入全拒审批闸 + 回调)。直接 import jobs,解掉 worker→server 反向依赖。
    from ops_agent.jobs import diagnosis_job_handler

    worker_count = max(1, settings.ops_worker_count)
    METRICS.set("workers_total", worker_count, backend="redis")  # 利用率分母
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    metrics_file = settings.ops_metrics_file
    threads = [
        threading.Thread(
            target=_worker_loop,
            args=(rq, diagnosis_job_handler, stop, metrics_file),
            name=f"worker-{i}",
        )
        for i in range(worker_count)
    ]
    # reaper:启动即回收上任 worker 硬崩遗留的在途任务,并周期性兜底(P1-1)
    threads.append(
        threading.Thread(
            target=_reaper_loop,
            args=(rq, stop, max(1.0, settings.ops_reaper_interval_seconds), metrics_file),
            name="reaper",
        )
    )
    # 心跳线程(P0-1①):间隔取判死窗口的 1/6(夹在 1~10s),留足网络抖动余量
    hb_interval = max(1.0, min(10.0, settings.ops_worker_dead_after_seconds / 6))
    threads.append(
        threading.Thread(target=_heartbeat_loop, args=(rq, stop, hb_interval), name="heartbeat")
    )
    threads.append(
        threading.Thread(target=_callback_loop, args=(rq, stop, 1.0), name="callback-outbox")
    )
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
        # 优雅排空:窗口覆盖单次 run 预算(OPS_MAX_RUN_SECONDS)+ 收尾余量,配合
        # k8s terminationGracePeriodSeconds(deploy/k8s/worker.yaml)≥ 该窗口;
        # 旧值 join(10s) 会在在途诊断(可长至 120s)没跑完时就退出,任务卡 RUNNING。
        deadline = time.monotonic() + settings.ops_max_run_seconds + 10
        for t in threads:
            t.join(timeout=max(0.1, deadline - time.monotonic()))
        if metrics_httpd is not None:
            metrics_httpd.shutdown()
            metrics_httpd.server_close()
        rq.close()
        logger.info("worker 优雅退出")


def main() -> None:
    run()


if __name__ == "__main__":
    main()
