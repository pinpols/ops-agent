"""Create local diagnosis bundles."""

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ops_agent.agent import AgentStepTrace, run_agent
from ops_agent.config import get_settings
from ops_agent.models import Diagnosis
from ops_agent.redaction import redact, redact_text


def _safe_slug(text: str, *, max_len: int = 48) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in text).strip("-")
    slug = "-".join(part for part in slug.split("-") if part)
    return (slug or "diagnosis")[:max_len]


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _maybe_redact(value, *, enabled: bool):
    return redact(value) if enabled else value


def create_bundle(question: str) -> Path:
    settings = get_settings()
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    bundle_dir = settings.ops_bundle_dir / f"{ts}-{uuid4().hex[:8]}-{_safe_slug(question)}"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    result = run_agent(question, include_trace=True)
    if len(result) != 3:
        raise RuntimeError("run_agent(include_trace=True) 未返回 trace")
    diagnosis, _, trace = result
    if not isinstance(diagnosis, Diagnosis):
        raise TypeError(f"诊断结果类型异常:{type(diagnosis).__name__}")
    bad_steps = [step for step in trace if not isinstance(step, AgentStepTrace)]
    if bad_steps:
        raise TypeError(f"trace step 类型异常:{type(bad_steps[0]).__name__}")
    redact_enabled = settings.ops_redact_artifacts

    (bundle_dir / "diagnosis.json").write_text(
        json.dumps(
            _maybe_redact(diagnosis.model_dump(mode="json"), enabled=redact_enabled),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _write_jsonl(
        bundle_dir / "trace.jsonl",
        _maybe_redact(
            [
                {"type": "question", "question": question, "timestamp": ts},
                *(dict(type="tool_step", **asdict(step)) for step in trace),
                {"type": "diagnosis", "diagnosis": diagnosis.model_dump(mode="json")},
            ],
            enabled=redact_enabled,
        ),
    )
    evidence = "\n\n".join(step.output for step in trace if step.output)
    if redact_enabled:
        evidence = redact_text(evidence)
    (bundle_dir / "evidence.log").write_text(
        evidence,
        encoding="utf-8",
    )
    summary = "\n".join(
        [
            f"# {diagnosis.summary}",
            "",
            f"- Severity: {diagnosis.severity.value}",
            f"- Confidence: {diagnosis.confidence}",
            f"- Root cause: {diagnosis.root_cause}",
            f"- Suggested action: {diagnosis.suggested_action}",
            "",
            "## Evidence",
            *(f"- {item}" for item in diagnosis.evidence),
            "",
            "## Tool Steps",
            *(f"- step {step.step}: {step.tool_name} ok={step.ok}" for step in trace),
        ]
    )
    if redact_enabled:
        summary = redact_text(summary)
    (bundle_dir / "summary.md").write_text(summary, encoding="utf-8")
    return bundle_dir
