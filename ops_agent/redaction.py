"""Redaction helpers for persisted artifacts."""

import re
from dataclasses import asdict, is_dataclass
from typing import Any

_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), "sk-ant-***"),
    (re.compile(r"gh[opsu]_[A-Za-z0-9_]+"), "gh***"),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._-]+"), r"\1***"),
    (re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)=([^&\s]+)"), r"\1=***"),
    (re.compile(r"(postgres(?:ql)?://[^:\s/@]+):([^@\s]+)@"), r"\1:***@"),
    (re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"), "***@***"),
    (re.compile(r"\b1[3-9]\d{9}\b"), "1**********"),
]


def redact_text(text: str) -> str:
    redacted = text
    for pattern, replacement in _PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if is_dataclass(value):
        return redact(asdict(value))
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value
