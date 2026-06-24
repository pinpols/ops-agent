"""Audit log helpers."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops_agent.redaction import redact

# 审计文件按大小滚动,防无限增长。生产应再把滚动后的归档投递到集中/不可篡改存储。
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024


def _audit_max_bytes() -> int:
    try:
        return int(os.environ.get("OPS_AUDIT_MAX_BYTES", _DEFAULT_MAX_BYTES))
    except ValueError:
        return _DEFAULT_MAX_BYTES


def _rotate_if_large(path: Path) -> None:
    """文件超阈值则滚动为 <name>.1(覆盖旧归档)。轻量单档滚动,够审计留存基线用。"""
    if path.exists() and path.stat().st_size >= _audit_max_bytes():
        path.replace(path.with_name(path.name + ".1"))


def append_approval_record(
    path: Path,
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    approved: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_large(path)
    record = redact(
        {
            "type": "approval",
            "timestamp": datetime.now(UTC).isoformat(),
            "tool_name": tool_name,
            "tool_input": tool_input,
            "approved": approved,
        }
    )
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_execution_record(
    path: Path,
    *,
    tool_name: str,
    ok: bool,
    dry_run: bool,
    detail: str | None = None,
) -> None:
    """危险动作批准后真正执行的结果留痕(成败 / 是否 dry-run),补"只记批没批"的审计盲区。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_large(path)
    record = redact(
        {
            "type": "execution",
            "timestamp": datetime.now(UTC).isoformat(),
            "tool_name": tool_name,
            "ok": ok,
            "dry_run": dry_run,
            "detail": detail,
        }
    )
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
