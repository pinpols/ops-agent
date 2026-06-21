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
    # 应出现在 root_cause/summary/evidence
    expected_keywords: list[str] = field(default_factory=list)
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
    Case(
        id="db_pool_exhausted",
        log_text=(
            "2026-06-15T09:21:14.002+08:00 ERROR [worker-import-17] "
            "com.zaxxer.hikari.pool.HikariPool - HikariPool-1 - Connection is not available, "
            "request timed out after 30000ms.\n"
            "2026-06-15T09:21:14.004+08:00 WARN [worker-import-17] "
            "o.s.jdbc.CannotGetJdbcConnectionException - Failed to obtain JDBC Connection"
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["hikari", "connection", "timeout"],
    ),
    Case(
        id="pg_lock_wait",
        log_text=(
            "2026-06-15T10:03:44.120+08:00 WARN [orchestrator-3] "
            "job_instance update blocked for 62000ms waiting for ShareLock on relation "
            "batch.job_instance\n"
            "2026-06-15T10:03:44.122+08:00 WARN [orchestrator-3] "
            "pg_stat_activity wait_event_type=Lock wait_event=transactionid"
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["lock", "job_instance"],
    ),
    Case(
        id="worker_queue_backlog",
        log_text=(
            "2026-06-15T11:40:01.550+08:00 WARN [scheduler] "
            "worker-import backlog=1842 oldest_task_age=47m active_workers=2\n"
            "2026-06-15T11:40:02.007+08:00 WARN [scheduler] "
            "dispatch lag above threshold for queue worker-import"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["backlog", "worker-import"],
    ),
    Case(
        id="external_api_timeout",
        log_text=(
            "2026-06-15T12:18:31.902+08:00 ERROR [worker-export-4] "
            "PaymentGatewayClient - POST https://payments.example.test/export timed out "
            "after 10000ms\n"
            "2026-06-15T12:18:31.904+08:00 WARN [worker-export-4] "
            "retrying payment export request attempt=3"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["payment", "timeout"],
    ),
    Case(
        id="disk_full",
        log_text=(
            "2026-06-15T13:02:09.711+08:00 ERROR [worker-process-9] "
            "java.io.IOException: No space left on device while writing "
            "/var/lib/batch/tmp/chunk-00042\n"
            "2026-06-15T13:02:09.713+08:00 ERROR [worker-process-9] "
            "failed to persist batch artifact, free_disk_bytes=0"
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["disk", "no space"],
    ),
    Case(
        id="healthy_heartbeat",
        log_text=(
            "2026-06-15T14:00:00.000+08:00 INFO [healthcheck] "
            "orchestrator heartbeat ok active_jobs=3 queued_jobs=0\n"
            "2026-06-15T14:00:05.000+08:00 INFO [healthcheck] "
            "worker-import heartbeat ok processed_last_minute=42"
        ),
        expected_severity=Severity.INFO,
        is_normal=True,
    ),
    Case(
        id="real_import_json_shape_mismatch",
        log_text=(
            "2026-06-17T18:41:36.335+08:00 WARN  [worker-task-exec-1] "
            "c.e.b.w.i.i.q.ValidationConfigSupport - catch:Exception: "
            "MismatchedInputException: Cannot deserialize value of type "
            "`java.util.LinkedHashMap<java.lang.String,java.lang.Object>` from Array value "
            "(token `JsonToken.START_ARRAY`)\n"
            "2026-06-17T18:41:36.572+08:00 WARN  [worker-task-exec-3] "
            "ValidationConfigSupport - catch:Exception: MismatchedInputException"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["mismatchedinputexception", "array"],
    ),
    Case(
        id="real_import_missing_table",
        log_text=(
            "2026-06-17T18:47:22.560+08:00 ERROR [worker-task-exec-1] "
            "c.e.batch.worker.imports.stage.LoadStep - load stage (streaming) failed: "
            "tenantId=ta, fileId=5400, message=PreparedStatementCallback; bad SQL grammar\n"
            "Caused by: org.postgresql.util.PSQLException: ERROR: relation "
            '"biz.missing_customer_account" does not exist'
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["missing_customer_account", "relation"],
    ),
    Case(
        id="real_partition_replace_copy_config",
        log_text=(
            "2026-06-17T18:47:28.385+08:00 ERROR [worker-task-exec-3] "
            "c.e.batch.worker.imports.stage.LoadStep - load stage (streaming) failed: "
            "message=PARTITION_REPLACE_COPY cannot run with partitionCount=2: each worker "
            "partition would clear the same target partition before COPY, which can leave "
            "partial data. Use shard_strategy=NONE for this template."
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["partition_replace_copy", "partitioncount"],
    ),
]
