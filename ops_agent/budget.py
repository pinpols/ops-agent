"""Agent 运行预算:墙钟超时 + token 上限 —— 防模型抽风时无限调工具烧钱/挂死。

run_agent 每步检查 deadline 与累计 token;越界抛 BudgetExceeded(携带已用量),
上层据此给"预算耗尽,返回当前最佳证据"的可控降级,而不是无声转圈或天价账单。
"""

import time
from dataclasses import dataclass


class BudgetExceeded(RuntimeError):
    """墙钟或 token 预算耗尽。message 含已用量,供降级展示。"""


@dataclass
class RunBudget:
    """单次 run 的预算闸。time_fn 可注入便于测试(默认 time.monotonic)。"""

    max_seconds: float | None = 120.0
    max_total_tokens: int | None = 200_000
    _start: float = 0.0

    def __post_init__(self) -> None:
        self._start = self._now()

    def _now(self) -> float:
        return time.monotonic()

    def elapsed(self) -> float:
        return self._now() - self._start

    def check(self, total_tokens: int) -> None:
        """每步调用:超墙钟或 token 上限即抛 BudgetExceeded。None 表示该维度不限。"""
        if self.max_seconds is not None and self.elapsed() > self.max_seconds:
            raise BudgetExceeded(
                f"墙钟预算耗尽:已用 {self.elapsed():.1f}s > {self.max_seconds:.0f}s 上限"
            )
        if self.max_total_tokens is not None and total_tokens > self.max_total_tokens:
            raise BudgetExceeded(
                f"token 预算耗尽:已用 {total_tokens} > {self.max_total_tokens} 上限"
            )
