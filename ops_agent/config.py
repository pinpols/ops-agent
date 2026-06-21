"""Centralized runtime configuration for ops-agent."""

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_TRACE_DIR = ".ops-agent/traces"
DEFAULT_BUNDLE_DIR = ".ops-agent/bundles"


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


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None
    anthropic_model: str
    anthropic_judge_model: str
    ops_target_root: Path | None
    ops_log_dir: Path
    ops_trace_dir: Path | None
    ops_bundle_dir: Path
    ops_pg_dsn: str | None
    ops_allow_exec: bool
    ops_restart_cmd: str | None
    langfuse_public_key: str | None
    langfuse_secret_key: str | None

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def has_pg(self) -> bool:
        return bool(self.ops_pg_dsn)

    @property
    def has_target_root(self) -> bool:
        return bool(self.ops_target_root and self.ops_target_root.exists())

    @classmethod
    def from_env(cls) -> "Settings":
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
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
            anthropic_model=model,
            anthropic_judge_model=os.environ.get("ANTHROPIC_JUDGE_MODEL", model),
            ops_target_root=target_root,
            ops_log_dir=resolved_log_dir,
            ops_trace_dir=Path(trace_dir).resolve() if trace_dir else None,
            ops_bundle_dir=Path(os.environ.get("OPS_BUNDLE_DIR", DEFAULT_BUNDLE_DIR)).resolve(),
            ops_pg_dsn=os.environ.get("OPS_PG_DSN"),
            ops_allow_exec=_env_bool("OPS_ALLOW_EXEC"),
            ops_restart_cmd=os.environ.get("OPS_RESTART_CMD"),
            langfuse_public_key=os.environ.get("LANGFUSE_PUBLIC_KEY"),
            langfuse_secret_key=os.environ.get("LANGFUSE_SECRET_KEY"),
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
