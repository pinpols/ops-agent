"""Golden 测试集:真实日志片段(取自 file-batch-system)+ 期望诊断。

期望不逐字比对(LLM 不复现原文),而是:severity 强约束 + 关键词召回 + 反例不误报。
扩充:遇到判错的真实日志,加一条进来 —— 这就是回归基线在长大。
"""

from dataclasses import dataclass, field

from ops_agent.models import Severity


@dataclass
class Case:
    id: str
    log_text: str
    expected_severity: Severity
    expected_keywords: list[str] = field(default_factory=list)  # 应出现在 root_cause/summary/evidence
    is_normal: bool = False  # 反例:正常日志,不许报 CRITICAL


CASES: list[Case] = [
    Case(
        id="redis_down",
        log_text=(
            "2026-06-14T11:11:57.821+08:00 WARN [lettuce-eventExecutorLoop-1-2] "
            "i.l.core.protocol.ConnectionWatchdog - Cannot reconnect to "
            "[localhost/<unresolved>:16379]: finishConnect(..) failed with error(-61): "
            "Connection refused: localhost/127.0.0.1:16379\n"
            "2026-06-14T11:12:03.279+08:00 WARN [lettuce-kqueueEventLoop-4-5] "
            "ConnectionWatchdog - Cannot reconnect to [localhost:16379]: Connection refused"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["redis", "16379"],
    ),
    Case(
        id="hikari_starvation",
        log_text=(
            "2026-06-14T01:19:02.652+08:00 WARN [HikariPool-1:housekeeper] "
            "com.zaxxer.hikari.pool.HikariPool - HikariPool-1 - Thread starvation or "
            "clock leap detected (housekeeper delta=16m45s959ms)."
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["hikari", "starvation"],
    ),
    Case(
        id="healthy_startup",
        log_text=(
            "2026-06-13T20:57:24.149+08:00 INFO [main] "
            "c.e.b.console.BatchConsoleApiApplication - Started BatchConsoleApiApplication "
            "in 66.257 seconds\n"
            "2026-06-13T20:57:29.244+08:00 INFO o.s.web.servlet.DispatcherServlet - "
            "Initializing Servlet 'dispatcherServlet'"
        ),
        expected_severity=Severity.INFO,
        is_normal=True,
    ),
]
