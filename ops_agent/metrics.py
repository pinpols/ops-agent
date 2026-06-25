"""Agent 自身的可观测指标(零依赖)——把计数/耗时落成 Prometheus textfile,供抓取或 /metrics 暴露。

不引 prometheus_client:进程级累计计数 + 原子写 textfile,够 T1 用。指标含:
诊断次数/失败次数、累计 token、累计耗时、各工具调用次数。`render()` 出 Prometheus 文本格式。
"""

import os
import tempfile
import threading
from pathlib import Path

_PREFIX = "ops_agent"

# 处理耗时默认桶(秒):覆盖快速读日志(亚秒)到多步 LLM 诊断(数十秒)。
DEFAULT_DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0)

_LabelKey = tuple[str, tuple[tuple[str, str], ...]]


def _fmt_labels(labels: tuple[tuple[str, str], ...], extra: tuple[str, str] | None = None) -> str:
    items = list(labels)
    if extra is not None:
        items = sorted([*items, extra])
    if not items:
        return ""
    return "{" + ",".join(f'{k}="{v}"' for k, v in items) + "}"


class Metrics:
    """进程内累计指标(counter / gauge / histogram)。线程安全(简单锁)。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[_LabelKey, float] = {}
        self._types: dict[str, str] = {}
        # 直方图:key → {"buckets": (le...), "counts": [cumulative...], "sum": s, "count": n}
        self._histograms: dict[_LabelKey, dict[str, object]] = {}

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._types.setdefault(name, "counter")
            self._counters[key] = self._counters.get(key, 0.0) + value

    def set(self, name: str, value: float, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._types[name] = "gauge"
            self._counters[key] = float(value)

    def add(self, name: str, delta: float = 1.0, **labels: str) -> None:
        """gauge 的相对增减(inc/dec),用于 workers_busy 这类在途计数。"""
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._types[name] = "gauge"
            self._counters[key] = self._counters.get(key, 0.0) + delta

    def observe(
        self, name: str, value: float, buckets: tuple[float, ...] | None = None, **labels: str
    ) -> None:
        """记录一次直方图观测(累计桶 + sum + count)。"""
        bkts = tuple(buckets) if buckets else DEFAULT_DURATION_BUCKETS
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._types[name] = "histogram"
            h = self._histograms.get(key)
            if h is None or h["buckets"] != bkts:
                h = {"buckets": bkts, "counts": [0.0] * len(bkts), "sum": 0.0, "count": 0.0}
                self._histograms[key] = h
            counts = h["counts"]
            assert isinstance(counts, list)
            for i, b in enumerate(bkts):
                if value <= b:
                    counts[i] += 1.0
            h["sum"] = float(h["sum"]) + value  # type: ignore[arg-type]
            h["count"] = float(h["count"]) + 1.0  # type: ignore[arg-type]

    def snapshot(self) -> dict[_LabelKey, float]:
        with self._lock:
            return dict(self._counters)

    def render(self) -> str:
        """Prometheus exposition 文本:counter/gauge + histogram(_bucket/_sum/_count)。"""
        with self._lock:
            counters = dict(self._counters)
            types = dict(self._types)
            histograms = {k: dict(v) for k, v in self._histograms.items()}
        lines: list[str] = []
        seen: set[str] = set()
        for (name, labels), val in sorted(counters.items()):
            metric = f"{_PREFIX}_{name}"
            if metric not in seen:
                lines.append(f"# TYPE {metric} {types.get(name, 'counter')}")
                seen.add(metric)
            lines.append(f"{metric}{_fmt_labels(labels)} {val}")
        for (name, labels), h in sorted(histograms.items()):
            metric = f"{_PREFIX}_{name}"
            if metric not in seen:
                lines.append(f"# TYPE {metric} histogram")
                seen.add(metric)
            buckets = h["buckets"]
            counts = h["counts"]
            assert isinstance(buckets, tuple) and isinstance(counts, list)
            for le, c in zip(buckets, counts, strict=True):
                lines.append(f"{metric}_bucket{_fmt_labels(labels, ('le', str(le)))} {c}")
            lines.append(f"{metric}_bucket{_fmt_labels(labels, ('le', '+Inf'))} {h['count']}")
            lines.append(f"{metric}_sum{_fmt_labels(labels)} {h['sum']}")
            lines.append(f"{metric}_count{_fmt_labels(labels)} {h['count']}")
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
