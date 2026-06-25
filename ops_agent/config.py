"""Centralized runtime configuration for ops-agent."""

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
# 合法 profile 闭集。profile 决定 prod fail-closed 闸 + 自由 SQL 默认值,
# 非法值(如把 "prod" 误写成 "production")绝不能静默当 dev —— 那是 fail-open。
_ALLOWED_PROFILES = ("dev", "staging", "prod")
DEFAULT_TRACE_DIR = ".ops-agent/traces"
DEFAULT_BUNDLE_DIR = ".ops-agent/bundles"
DEFAULT_APPROVAL_LOG = ".ops-agent/approvals.jsonl"


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_target_root() -> Path | None:
    candidate = _project_root().parent / "file-batch-system"
    return candidate.resolve() if candidate.exists() else None


def _env_bool(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_csv(name: str) -> tuple[str, ...]:
    value = os.environ.get(name, "")
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _env_or_file(name: str) -> str | None:
    """读敏感值:优先 env `NAME`,否则读 `NAME_FILE` 指向的文件内容(去尾换行)。

    `*_FILE` 是 docker/k8s secret 注入的标准做法 —— 密钥落文件挂进容器,不进环境变量/进程表/日志。
    """
    direct = os.environ.get(name)
    if direct:
        return direct
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        content = Path(file_path).read_text(encoding="utf-8").strip()
        return content or None
    return None


@dataclass(frozen=True)
class Settings:
    ops_profile: str
    anthropic_api_key: str | None = field(repr=False)  # 防 key 误入 repr/异常栈/日志
    anthropic_model: str
    anthropic_judge_model: str
    anthropic_max_retries: int
    ops_target_root: Path | None
    ops_log_dir: Path
    ops_trace_dir: Path | None
    ops_bundle_dir: Path
    ops_pg_dsn: str | None
    ops_sql_allow_free: bool
    ops_allow_exec: bool
    ops_prod_allow_exec: bool
    ops_restart_cmd: str | None
    ops_exec_allowlist: tuple[str, ...]
    ops_approval_log: Path
    ops_redact_artifacts: bool
    ops_redaction_rules_file: Path | None
    langfuse_public_key: str | None
    langfuse_secret_key: str | None
    # T1 生产化:webhook 触发鉴权 + run 预算闸 + 自身指标 textfile
    ops_webhook_token: str | None = field(default=None, repr=False)
    ops_max_run_seconds: float = 120.0
    ops_max_run_tokens: int = 200_000
    ops_metrics_file: Path | None = None
    # 诊断历史持久层(可查询 + 留存);未配 db 路径=关闭,不落库(零回归)。
    ops_history_db: Path | None = None
    ops_history_retention_days: int = 90
    ops_history_max_rows: int = 100_000
    # 事件驱动 Step 1:异步 webhook(入队 + 202 + worker 池)。默认关=同步内联(零回归)。
    ops_async_diagnose: bool = False
    ops_worker_count: int = 2
    ops_queue_max: int = 100
    ops_callback_url: str | None = None
    # 事件驱动 Step 2:队列后端。memory=进程内(默认);redis=真队列 + 独立 worker 进程 + DLQ。
    ops_queue_backend: str = "memory"
    ops_redis_url: str | None = None
    ops_queue_key: str = "ops:queue"
    ops_dlq_key: str = "ops:dlq"
    ops_job_ttl_seconds: int = 86_400
    ops_max_retries: int = 2
    ops_retry_base_seconds: float = 1.0
    ops_retry_max_seconds: float = 60.0
    ops_queue_depth_alert_threshold: int = 0

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def has_pg(self) -> bool:
        return bool(self.ops_pg_dsn)

    @property
    def has_target_root(self) -> bool:
        return bool(self.ops_target_root and self.ops_target_root.exists())

    @property
    def production(self) -> bool:
        return self.ops_profile == "prod"

    @classmethod
    def from_env(cls) -> "Settings":
        profile = os.environ.get("OPS_PROFILE", "dev").strip().lower()
        if profile not in _ALLOWED_PROFILES:
            raise ValueError(
                f"OPS_PROFILE={profile!r} 非法,必须是 {list(_ALLOWED_PROFILES)} 之一"
                "(常见笔误:用了 'production' 而非 'prod')。"
                "拒绝静默降级为 dev,以免 prod 安全闸被绕过。"
            )
        model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)
        target_root = (
            Path(os.environ["OPS_TARGET_ROOT"]).resolve()
            if os.environ.get("OPS_TARGET_ROOT")
            else _default_target_root()
        )
        log_dir = os.environ.get("OPS_LOG_DIR")
        resolved_log_dir = Path(log_dir).resolve() if log_dir else _default_log_dir(target_root)
        trace_dir = os.environ.get("OPS_TRACE_DIR")
        return cls(
            ops_profile=profile,
            anthropic_api_key=_env_or_file("ANTHROPIC_API_KEY"),
            anthropic_model=model,
            anthropic_judge_model=os.environ.get("ANTHROPIC_JUDGE_MODEL", model),
            anthropic_max_retries=int(os.environ.get("OPS_LLM_MAX_RETRIES", "4")),
            ops_target_root=target_root,
            ops_log_dir=resolved_log_dir,
            ops_trace_dir=Path(trace_dir).resolve() if trace_dir else None,
            ops_bundle_dir=Path(os.environ.get("OPS_BUNDLE_DIR", DEFAULT_BUNDLE_DIR)).resolve(),
            ops_pg_dsn=_env_or_file("OPS_PG_DSN"),
            ops_sql_allow_free=_env_bool("OPS_SQL_ALLOW_FREE", default=profile != "prod"),
            ops_allow_exec=_env_bool("OPS_ALLOW_EXEC"),
            ops_prod_allow_exec=_env_bool("OPS_PROD_ALLOW_EXEC"),
            ops_restart_cmd=os.environ.get("OPS_RESTART_CMD"),
            ops_exec_allowlist=_env_csv("OPS_EXEC_ALLOWLIST"),
            ops_approval_log=Path(
                os.environ.get("OPS_APPROVAL_LOG", DEFAULT_APPROVAL_LOG)
            ).resolve(),
            ops_redact_artifacts=_env_bool("OPS_REDACT_ARTIFACTS", default=True),
            ops_redaction_rules_file=(
                Path(os.environ["OPS_REDACTION_RULES_FILE"]).resolve()
                if os.environ.get("OPS_REDACTION_RULES_FILE")
                else None
            ),
            langfuse_public_key=os.environ.get("LANGFUSE_PUBLIC_KEY"),
            langfuse_secret_key=os.environ.get("LANGFUSE_SECRET_KEY"),
            ops_webhook_token=_env_or_file("OPS_WEBHOOK_TOKEN"),
            ops_max_run_seconds=float(os.environ.get("OPS_MAX_RUN_SECONDS", "120")),
            ops_max_run_tokens=int(os.environ.get("OPS_MAX_RUN_TOKENS", "200000")),
            ops_metrics_file=(
                Path(os.environ["OPS_METRICS_FILE"]).resolve()
                if os.environ.get("OPS_METRICS_FILE")
                else None
            ),
            ops_history_db=(
                Path(os.environ["OPS_HISTORY_DB"]).resolve()
                if os.environ.get("OPS_HISTORY_DB")
                else None
            ),
            ops_history_retention_days=int(os.environ.get("OPS_HISTORY_RETENTION_DAYS", "90")),
            ops_history_max_rows=int(os.environ.get("OPS_HISTORY_MAX_ROWS", "100000")),
            ops_async_diagnose=_env_bool("OPS_ASYNC_DIAGNOSE"),
            ops_worker_count=int(os.environ.get("OPS_WORKER_COUNT", "2")),
            ops_queue_max=int(os.environ.get("OPS_QUEUE_MAX", "100")),
            ops_callback_url=os.environ.get("OPS_CALLBACK_URL"),
            ops_queue_backend=os.environ.get("OPS_QUEUE_BACKEND", "memory").strip().lower(),
            ops_redis_url=os.environ.get("OPS_REDIS_URL"),
            ops_queue_key=os.environ.get("OPS_QUEUE_KEY", "ops:queue"),
            ops_dlq_key=os.environ.get("OPS_DLQ_KEY", "ops:dlq"),
            ops_job_ttl_seconds=int(os.environ.get("OPS_JOB_TTL_SECONDS", "86400")),
            ops_max_retries=int(os.environ.get("OPS_MAX_RETRIES", "2")),
            ops_retry_base_seconds=float(os.environ.get("OPS_RETRY_BASE_SECONDS", "1")),
            ops_retry_max_seconds=float(os.environ.get("OPS_RETRY_MAX_SECONDS", "60")),
            ops_queue_depth_alert_threshold=int(
                os.environ.get("OPS_QUEUE_DEPTH_ALERT_THRESHOLD", "0")
            ),
        )


def get_settings() -> Settings:
    """Read current environment into an immutable settings object."""
    return Settings.from_env()


def _default_log_dir(target_root: Path | None) -> Path:
    if target_root:
        candidate = target_root / "logs"
        if candidate.exists():
            return candidate.resolve()
    return (_project_root() / "data").resolve()
