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
