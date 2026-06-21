"""Audit log helpers."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops_agent.redaction import redact


def append_approval_record(
    path: Path,
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    approved: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
