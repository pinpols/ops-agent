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
    # ── Kafka ────────────────────────────────────────────────
    Case(
        id="kafka_broker_down",
        log_text=(
            "2026-06-18T09:02:11.430+08:00 ERROR [kafka-producer-network-thread] "
            "o.a.k.c.NetworkClient - Connection to node 1 (kafka-0:9092) could not be "
            "established. Broker may not be available.\n"
            "2026-06-18T09:02:11.998+08:00 ERROR o.s.k.support.LoggingProducerListener - "
            "Exception thrown when sending: TimeoutException: Topic batch.launch not present"
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["kafka", "broker"],
    ),
    Case(
        id="kafka_consumer_lag",
        log_text=(
            "2026-06-18T09:30:44.120+08:00 WARN [consumer-launch-1] "
            "ConsumerLagMonitor - group=batch-orchestrator topic=batch.launch "
            "partition=3 lag=48211 and growing\n"
            "2026-06-18T09:30:49.005+08:00 WARN ConsumerLagMonitor - end-to-end "
            "processing delay 2m13s above SLO"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["lag", "batch.launch"],
    ),
    Case(
        id="kafka_rebalance_storm",
        log_text=(
            "2026-06-18T09:41:02.300+08:00 WARN [consumer-report-2] "
            "o.a.k.c.c.internals.ConsumerCoordinator - Group batch-report is rebalancing; "
            "member rejoined 7 times in 60s\n"
            "2026-06-18T09:41:08.110+08:00 WARN ConsumerCoordinator - revoking previously "
            "assigned partitions due to repeated heartbeat expiration"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["rebalanc", "heartbeat"],
    ),
    Case(
        id="kafka_offset_commit_failed",
        log_text=(
            "2026-06-18T10:05:51.700+08:00 WARN [consumer-claim-1] ConsumerCoordinator - "
            "Offset commit failed on partition batch.claim-0 at offset 99213: "
            "CommitFailedException - consumer poll timeout exceeded, group rebalanced"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["offset commit", "batch.claim"],
    ),
    Case(
        id="kafka_rebalance_completed_normal",
        log_text=(
            "2026-06-18T10:20:00.000+08:00 INFO [consumer-launch-1] ConsumerCoordinator - "
            "Successfully joined group batch-orchestrator with generation 12\n"
            "2026-06-18T10:20:00.140+08:00 INFO ConsumerCoordinator - Setting newly "
            "assigned partitions [batch.launch-0, batch.launch-1] for group"
        ),
        expected_severity=Severity.INFO,
        is_normal=True,
    ),
    # ── Postgres / Citus ─────────────────────────────────────
    Case(
        id="pg_deadlock_detected",
        log_text=(
            "2026-06-18T11:14:09.221+08:00 ERROR [orchestrator-5] "
            "o.h.engine.jdbc.spi.SqlExceptionHelper - ERROR: deadlock detected\n"
            "Detail: Process 2841 waits for ShareLock on transaction 99120; blocked by "
            "process 2790. Process 2790 waits for ShareLock on transaction 99118; "
            "blocked by process 2841.\n"
            "Where: while updating tuple in relation batch.job_instance"
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["deadlock", "job_instance"],
    ),
    Case(
        id="pg_too_many_connections",
        log_text=(
            "2026-06-18T11:31:55.880+08:00 ERROR [worker-import-2] o.p.util.PSQLException - "
            "FATAL: sorry, too many clients already\n"
            "2026-06-18T11:31:55.882+08:00 ERROR HikariPool - Exception during pool "
            "initialization: FATAL: remaining connection slots are reserved"
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["too many clients", "connection"],
    ),
    Case(
        id="pg_statement_timeout",
        log_text=(
            "2026-06-18T11:50:02.410+08:00 WARN [console-api-7] SqlExceptionHelper - "
            "ERROR: canceling statement due to statement timeout\n"
            "2026-06-18T11:50:02.412+08:00 WARN org.springframework.jdbc - query on "
            "batch.job_instance_archive exceeded statement_timeout=5000ms"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["statement timeout", "5000"],
    ),
    Case(
        id="pg_serialization_failure",
        log_text=(
            "2026-06-18T12:03:18.560+08:00 WARN [worker-atomic-3] SqlExceptionHelper - "
            "ERROR: could not serialize access due to concurrent update\n"
            "2026-06-18T12:03:18.999+08:00 WARN retrying transaction attempt=2 on "
            "batch.outbox_event (SQLSTATE 40001)"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["serialize", "40001"],
    ),
    Case(
        id="pg_unique_violation",
        log_text=(
            "2026-06-18T12:22:40.110+08:00 ERROR [worker-import-9] SqlExceptionHelper - "
            "ERROR: duplicate key value violates unique constraint "
            '"uq_job_instance_tenant_batch"\n'
            "Detail: Key (tenant_id, batch_no)=(ta, 20260618-001) already exists."
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["duplicate key", "unique constraint"],
    ),
    Case(
        id="pg_fk_violation",
        log_text=(
            "2026-06-18T12:41:07.330+08:00 ERROR [worker-process-4] SqlExceptionHelper - "
            'ERROR: insert or update on table "batch.task_instance" violates foreign key '
            'constraint "fk_task_job"\n'
            'Detail: Key (job_id)=(88213) is not present in table "job_instance".'
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["foreign key", "task_instance"],
    ),
    Case(
        id="citus_worker_node_down",
        log_text=(
            "2026-06-18T13:10:21.470+08:00 ERROR [orchestrator-1] SqlExceptionHelper - "
            "ERROR: connection to the remote node citus-worker-2:5432 failed with "
            '"could not connect to server: Connection refused"\n'
            "2026-06-18T13:10:21.900+08:00 ERROR distributed query on batch.job_instance "
            "aborted: placement on node citus-worker-2 unreachable"
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["citus-worker-2", "connection"],
    ),
    Case(
        id="pg_replication_lag",
        log_text=(
            "2026-06-18T13:33:50.020+08:00 WARN [replica-monitor] ReplicaHealth - "
            "read replica lag 38s exceeds threshold 10s; routing read-only queries to primary\n"
            "2026-06-18T13:33:55.001+08:00 WARN ReplicaHealth - replay_lsn behind by 412MB"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["replica", "lag"],
    ),
    Case(
        id="pg_wal_disk_pressure",
        log_text=(
            "2026-06-18T13:55:12.660+08:00 ERROR [postgres] checkpointer - "
            'could not write to file "pg_wal/000000010000002A0000003C": No space left on device\n'
            "2026-06-18T13:55:12.880+08:00 ERROR PANIC: could not write to log file, "
            "database is shutting down"
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["pg_wal", "no space"],
    ),
    Case(
        id="pg_vacuum_progress_normal",
        log_text=(
            "2026-06-18T14:00:00.000+08:00 INFO [autovacuum] automatic vacuum of table "
            '"batch.outbox_event": index scans: 1, tuples removed: 12044, elapsed 3.21s\n'
            "2026-06-18T14:00:03.000+08:00 INFO autovacuum completed, no bloat warning"
        ),
        expected_severity=Severity.INFO,
        is_normal=True,
    ),
    # ── Redis ────────────────────────────────────────────────
    Case(
        id="redis_oom_maxmemory",
        log_text=(
            "2026-06-18T14:21:33.140+08:00 ERROR [lettuce-nioEventLoop-4-1] "
            "RedisCommandExecutionException - OOM command not allowed when used memory > "
            "'maxmemory'.\n"
            "2026-06-18T14:21:33.560+08:00 ERROR cache write failed for key "
            "ops:dispatch:dedup, evicting disabled (noeviction)"
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["oom", "maxmemory"],
    ),
    Case(
        id="redis_master_failover",
        log_text=(
            "2026-06-18T14:40:09.700+08:00 WARN [lettuce-eventExecutorLoop-1-3] "
            "ConnectionWatchdog - Reconnecting, last destination was redis-master:6379\n"
            "2026-06-18T14:40:11.230+08:00 WARN Sentinel reported +switch-master "
            "mymaster redis-master 6379 redis-replica-1 6379"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["switch-master", "redis"],
    ),
    # ── S3 / MinIO ───────────────────────────────────────────
    Case(
        id="s3_access_denied",
        log_text=(
            "2026-06-18T15:02:44.010+08:00 ERROR [worker-export-2] "
            "software.amazon.awssdk.services.s3.model.S3Exception - Access Denied "
            "(Service: S3, Status Code: 403, Request ID: ABC123)\n"
            "2026-06-18T15:02:44.330+08:00 ERROR failed to put object "
            "exports/ta/20260618/report.xlsx to bucket batch-artifacts"
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["access denied", "403"],
    ),
    Case(
        id="s3_bucket_missing",
        log_text=(
            "2026-06-18T15:18:51.880+08:00 ERROR [worker-export-5] S3Exception - "
            "The specified bucket does not exist (Service: S3, Status Code: 404) "
            "bucket=batch-artifacts-staging"
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["bucket", "404"],
    ),
    Case(
        id="s3_slowdown_throttle",
        log_text=(
            "2026-06-18T15:39:12.450+08:00 WARN [worker-export-1] S3RetryHandler - "
            "SlowDown (503) Please reduce your request rate; retrying putObject "
            "attempt=4 backoff=800ms bucket=batch-artifacts"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["slowdown", "retry"],
    ),
    # ── JVM ──────────────────────────────────────────────────
    Case(
        id="jvm_heap_oom",
        log_text=(
            "2026-06-18T16:01:09.220+08:00 ERROR [worker-process-8] - "
            "java.lang.OutOfMemoryError: Java heap space\n"
            "\tat java.base/java.util.Arrays.copyOf(Arrays.java:3537)\n"
            "2026-06-18T16:01:09.900+08:00 ERROR Terminating due to "
            "java.lang.OutOfMemoryError: Java heap space"
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["outofmemoryerror", "heap"],
    ),
    Case(
        id="jvm_gc_overhead",
        log_text=(
            "2026-06-18T16:20:41.330+08:00 ERROR [console-api-3] - "
            "java.lang.OutOfMemoryError: GC overhead limit exceeded\n"
            "2026-06-18T16:20:41.660+08:00 WARN G1 Old Gen at 98%, last 5 full GCs "
            "recovered <2% heap"
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["gc overhead", "outofmemoryerror"],
    ),
    Case(
        id="jvm_metaspace_oom",
        log_text=(
            "2026-06-18T16:41:02.700+08:00 ERROR [main] - "
            "java.lang.OutOfMemoryError: Metaspace\n"
            "2026-06-18T16:41:02.900+08:00 ERROR class loading failed, Metaspace "
            "usage 256MB/256MB"
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["metaspace", "outofmemoryerror"],
    ),
    Case(
        id="jvm_thread_deadlock",
        log_text=(
            "2026-06-18T17:00:18.510+08:00 ERROR [jvm-monitor] ThreadMXBean - "
            "Deadlock detected between worker-import-3 and worker-import-7:\n"
            "worker-import-3 holds lock 0x42 waiting 0x43; worker-import-7 holds 0x43 "
            "waiting 0x42"
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["deadlock", "thread"],
    ),
    # ── Spring Boot / Flyway ─────────────────────────────────
    Case(
        id="flyway_checksum_mismatch",
        log_text=(
            "2026-06-18T17:22:40.110+08:00 ERROR [main] o.f.core.internal.command.DbValidate - "
            "Migration checksum mismatch for migration version 172\n"
            "-> Applied to database : 118273645\n"
            "-> Resolved locally    : 998812233. Either revert the changes or run repair."
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["checksum mismatch", "172"],
    ),
    Case(
        id="flyway_migrate_success_normal",
        log_text=(
            "2026-06-18T17:30:00.000+08:00 INFO [main] o.f.core.internal.command.DbMigrate - "
            'Successfully applied 2 migrations to schema "public" '
            "(execution time 00:01.204s), now at version v173\n"
            "2026-06-18T17:30:00.300+08:00 INFO Flyway Community Edition validation passed"
        ),
        expected_severity=Severity.INFO,
        is_normal=True,
    ),
    Case(
        id="spring_bean_creation_failure",
        log_text=(
            "2026-06-18T17:45:13.880+08:00 ERROR [main] o.s.boot.SpringApplication - "
            "Application run failed\n"
            "org.springframework.beans.factory.UnsatisfiedDependencyException: Error "
            "creating bean 'dispatchTopicRouter': No qualifying bean of type "
            "'KafkaTemplate' available"
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["bean", "kafkatemplate"],
    ),
    Case(
        id="spring_port_in_use",
        log_text=(
            "2026-06-18T17:58:02.140+08:00 ERROR [main] o.s.b.w.embedded.tomcat.TomcatStarter - "
            "Web server failed to start. Port 18080 was already in use.\n"
            "2026-06-18T17:58:02.150+08:00 ERROR Identify and stop the process listening "
            "on port 18080"
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["port", "18080"],
    ),
    Case(
        id="spring_context_started_normal",
        log_text=(
            "2026-06-18T18:00:00.000+08:00 INFO [main] o.s.b.w.embedded.tomcat.TomcatWebServer - "
            "Tomcat started on port(s): 18083 (http)\n"
            "2026-06-18T18:00:00.500+08:00 INFO c.e.b.worker.WorkerImportApplication - "
            "Started WorkerImportApplication in 22.41 seconds"
        ),
        expected_severity=Severity.INFO,
        is_normal=True,
    ),
    # ── Worker / orchestrator ────────────────────────────────
    Case(
        id="job_stuck_running",
        log_text=(
            "2026-06-18T18:21:44.300+08:00 WARN [orchestrator-watchdog] StuckJobDetector - "
            "job_instance id=88410 stuck in RUNNING for 95m, last heartbeat 92m ago, "
            "worker worker-process-2 unresponsive"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["stuck", "88410"],
    ),
    Case(
        id="task_retry_exhausted_dlq",
        log_text=(
            "2026-06-18T18:40:55.700+08:00 ERROR [worker-dispatch-1] RetryPolicy - "
            "task id=55120 exhausted max attempts=3, moving to dead letter\n"
            "2026-06-18T18:40:55.900+08:00 ERROR DLQ - task 55120 routed to "
            "batch.dispatch.dlq, last error=ConnectTimeoutException"
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["dead letter", "55120"],
    ),
    Case(
        id="dispatch_compensation_conflict",
        log_text=(
            "2026-06-18T19:02:11.120+08:00 WARN [worker-dispatch-3] CompensationHandler - "
            "ACK arrived after COMPENSATE for dispatch id=77310; state_conflict "
            "resolved to COMPLETE\n"
            "2026-06-18T19:02:11.330+08:00 WARN reversing compensation, downstream "
            "already acknowledged"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["compensat", "77310"],
    ),
    Case(
        id="worker_graceful_shutdown_normal",
        log_text=(
            "2026-06-18T19:15:00.000+08:00 INFO [SIGTERM-handler] WorkerLifecycle - "
            "received SIGTERM, draining in-flight tasks (2 active)\n"
            "2026-06-18T19:15:03.200+08:00 INFO WorkerLifecycle - all tasks drained, "
            "worker exited cleanly"
        ),
        expected_severity=Severity.INFO,
        is_normal=True,
    ),
    Case(
        id="job_completed_normal",
        log_text=(
            "2026-06-18T19:20:00.000+08:00 INFO [orchestrator-2] JobStateMachine - "
            "job_instance id=88500 transitioned RUNNING -> SUCCEEDED, "
            "12000 rows imported in 48.2s\n"
            "2026-06-18T19:20:00.100+08:00 INFO outbox_event emitted job.succeeded"
        ),
        expected_severity=Severity.INFO,
        is_normal=True,
    ),
    # ── Quartz ───────────────────────────────────────────────
    Case(
        id="quartz_misfire",
        log_text=(
            "2026-06-18T19:41:09.880+08:00 WARN [quartz-scheduler] QuartzScheduler - "
            "Handling 14 trigger(s) that missed their scheduled fire-time for job "
            "group=batch trigger=nightly-export (misfire threshold 60000ms exceeded)"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["misfire", "trigger"],
    ),
    Case(
        id="quartz_job_already_running",
        log_text=(
            "2026-06-18T20:00:02.140+08:00 WARN [quartz-worker-3] "
            "DisallowConcurrentExecution - skipping fire of job nightly-reconcile: "
            "previous execution still running (started 61m ago)"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["already running", "nightly-reconcile"],
    ),
    # ── 网络 / 安全 ──────────────────────────────────────────
    Case(
        id="dns_resolution_failure",
        log_text=(
            "2026-06-18T20:22:31.500+08:00 ERROR [worker-export-7] - "
            "java.net.UnknownHostException: payments.internal.svc: Name or service not known\n"
            "2026-06-18T20:22:31.700+08:00 ERROR failed to resolve downstream endpoint, "
            "all export dispatches failing"
        ),
        expected_severity=Severity.CRITICAL,
        expected_keywords=["unknownhostexception", "resolve"],
    ),
    Case(
        id="tls_handshake_failure",
        log_text=(
            "2026-06-18T20:41:18.220+08:00 WARN [worker-export-4] - "
            "javax.net.ssl.SSLHandshakeException: PKIX path building failed: unable to "
            "find valid certification path to requested target (host=api.partner.test)\n"
            "2026-06-18T20:41:18.500+08:00 WARN retrying after TLS failure attempt=2"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["sslhandshakeexception", "certification path"],
    ),
    Case(
        id="ssrf_blocked",
        log_text=(
            "2026-06-18T21:02:44.910+08:00 WARN [console-api-2] DnsResolveGuard - "
            "blocked outbound webhook to 169.254.169.254 (link-local/metadata), "
            "SSRF guard denied request for callback_url\n"
            "2026-06-18T21:02:44.920+08:00 WARN rejected user-supplied URL resolving "
            "to private range"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["ssrf", "169.254.169.254"],
    ),
    Case(
        id="webhook_rate_limited",
        log_text=(
            "2026-06-18T21:20:09.330+08:00 WARN [console-api-5] RateLimitFilter - "
            "client 10.2.3.4 exceeded 100 req/min on /api/triggers/launch, returning 429; "
            "rejected 37 requests in last window"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["rate", "429"],
    ),
    Case(
        id="auth_invalid_token",
        log_text=(
            "2026-06-18T21:35:51.770+08:00 WARN [console-api-1] ConsoleAuthFilter - "
            "rejected request to /api/console/jobs: invalid or expired bearer token "
            "(401), subject=unknown\n"
            "2026-06-18T21:35:52.010+08:00 WARN repeated 401 from 10.9.9.9, "
            "possible credential probing"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["401", "token"],
    ),
    # ── Hikari(补) ──────────────────────────────────────────
    Case(
        id="hikari_connection_leak",
        log_text=(
            "2026-06-18T21:55:02.440+08:00 WARN [HikariPool-1:housekeeper] ProxyLeakTask - "
            "Connection leak detection triggered for connection "
            "org.postgresql.jdbc.PgConnection@5f, stack trace follows (held 65000ms)"
        ),
        expected_severity=Severity.WARNING,
        expected_keywords=["leak", "connection"],
    ),
    # ── 更多正常样本(防草木皆兵)─────────────────────────────
    Case(
        id="scheduled_trigger_fired_normal",
        log_text=(
            "2026-06-18T22:00:00.000+08:00 INFO [quartz-scheduler] TriggerService - "
            "fired scheduled trigger nightly-export, created job_instance id=88600 "
            "for tenant ta\n"
            "2026-06-18T22:00:00.200+08:00 INFO enqueued to batch.launch partition 1"
        ),
        expected_severity=Severity.INFO,
        is_normal=True,
    ),
    Case(
        id="healthy_gc_normal",
        log_text=(
            "2026-06-18T22:10:00.000+08:00 INFO [gc] G1 Young Generation - "
            "Pause Young (Normal) 18ms, heap 412M->96M(1024M)\n"
            "2026-06-18T22:10:30.000+08:00 INFO no full GC in last hour, allocation "
            "rate nominal"
        ),
        expected_severity=Severity.INFO,
        is_normal=True,
    ),
    Case(
        id="dryrun_dispatch_normal",
        log_text=(
            "2026-06-18T22:20:00.000+08:00 INFO [worker-dispatch-2] DryRunGuard - "
            "dry-run mode: would POST to https://partner.test/webhook but suppressed "
            "real send (dry_run=true)\n"
            "2026-06-18T22:20:00.050+08:00 INFO dispatch id=77400 marked DRY_RUN_OK"
        ),
        expected_severity=Severity.INFO,
        is_normal=True,
    ),
    Case(
        id="backfill_completed_normal",
        log_text=(
            "2026-06-18T22:30:00.000+08:00 INFO [console-api-4] BackfillService - "
            "backfill window 2026-06-01..2026-06-07 completed: 7 partitions, "
            "84211 rows reprocessed, 0 errors"
        ),
        expected_severity=Severity.INFO,
        is_normal=True,
    ),
]
