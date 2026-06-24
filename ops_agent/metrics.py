"""Agent 自身的可观测指标(零依赖)——把计数/耗时落成 Prometheus textfile,供抓取或 /metrics 暴露。

不引 prometheus_client:进程级累计计数 + 原子写 textfile,够 T1 用。指标含:
诊断次数/失败次数、累计 token、累计耗时、各工具调用次数。`render()` 出 Prometheus 文本格式。
"""

import os
import tempfile
import threading
from pathlib import Path

_PREFIX = "ops_agent"


class Metrics:
    """进程内累计指标。线程安全(简单锁)。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def snapshot(self) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
        with self._lock:
            return dict(self._counters)

    def render(self) -> str:
        """Prometheus exposition 文本。每个 name 配 # TYPE counter。"""
        lines: list[str] = []
        seen: set[str] = set()
        for (name, labels), val in sorted(self.snapshot().items()):
            metric = f"{_PREFIX}_{name}"
            if metric not in seen:
                lines.append(f"# TYPE {metric} counter")
                seen.add(metric)
            label_str = "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}" if labels else ""
            lines.append(f"{metric}{label_str} {val}")
        return "\n".join(lines) + ("\n" if lines else "")

    def write_textfile(self, path: Path) -> None:
        """原子写 textfile(先写 tmp 再 rename),避免抓取读到半截。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.render())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


# 进程级单例:agent 各处 import 同一个累加器。
METRICS = Metrics()
