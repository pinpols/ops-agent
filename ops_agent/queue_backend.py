"""队列后端契约(Protocol)—— 把此前纯约定的鸭子类型钉成 mypy + 运行时可校验的接口,防漂移。

两层契约:
- ``IngressQueue``:serve 入口侧依赖的(入队/查询/水位)。memory(JobQueue)与 redis(RedisQueue)都满足。
- ``WorkerQueue``:serve-worker 跨进程消费侧依赖的(取/标记/完成/重试/探活/关闭)。目前仅 RedisQueue
  实现 —— 内存后端自带线程池、不需要外部消费循环,这也是两后端接口本就分叉的原因。
"""

from typing import Protocol, runtime_checkable

from ops_agent.jobqueue import DiagnosisJob


@runtime_checkable
class IngressQueue(Protocol):
    """入口契约:serve 对 job_queue 只依赖这几个方法。"""

    def submit(
        self, question: str, target: str | None = None, trace_id: str | None = None
    ) -> DiagnosisJob | None: ...

    def get(self, job_id: str) -> DiagnosisJob | None: ...

    def qsize(self) -> int: ...

    def update_queue_metrics(self) -> None: ...


@runtime_checkable
class WorkerQueue(IngressQueue, Protocol):
    """消费契约:在 IngressQueue 之上加 worker 侧取/标记/完成/重试/探活/关闭。"""

    def consume(self, timeout: int = 1) -> str | None: ...

    def mark_running(self, job_id: str) -> bool: ...

    def complete(self, job_id: str, result: dict) -> bool: ...

    def fail_or_retry(self, job_id: str, error: str, *, retryable: bool = True) -> str: ...

    def discard(self, job_id: str) -> None: ...

    def reap(self, now: float | None = None) -> dict[str, int]: ...

    def ping(self) -> bool: ...

    def close(self) -> None: ...
