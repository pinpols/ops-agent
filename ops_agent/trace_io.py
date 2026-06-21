"""Trace persistence helpers."""

import json
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops_agent.models import Diagnosis


def _jsonable(value: Any) -> Any:
    if isinstance(value, Diagnosis):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    return value


def write_agent_trace(
    trace_dir: Path,
    *,
    question: str,
    model: str,
    diagnosis: Diagnosis,
    steps: list[Any],
) -> Path:
    trace_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = trace_dir / f"agent-trace-{ts}.jsonl"
    records = [
        {
            "type": "run",
            "timestamp": ts,
            "question": question,
            "model": model,
        },
        *({"type": "tool_step", **_jsonable(step)} for step in steps),
        {"type": "diagnosis", "diagnosis": _jsonable(diagnosis)},
    ]
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path
