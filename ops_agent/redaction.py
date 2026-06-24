"""Redaction helpers for persisted artifacts."""

import json
import os
import re
import threading
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), "sk-ant-***"),
    (re.compile(r"gh[opsu]_[A-Za-z0-9_]+"), "gh***"),
    # AWS Access Key ID:裸出现(无 key= 关键词)也要遮,AKIA + 16 位大写/数字。
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***"),
    # JWT:三段 base64url(header.payload.signature),裸出现于日志/头部时遮蔽。
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "***JWT***"),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._-]+"), r"\1***"),
    # 私钥块整段(PEM,含 RSA/EC/OPENSSH 等变体),DOTALL 跨行
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "-----BEGIN PRIVATE KEY-----***-----END PRIVATE KEY-----",
    ),
    # key=value 与 key: value(YAML/properties 都覆盖);关键词扩展到常见凭据字段。
    # read_app_config 读的 Spring 配置正是 `password: xxx` 冒号写法,旧正则只匹配 `=` 会漏。
    (
        re.compile(
            r"(?im)([\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
            r"secret[_-]?key|private[_-]?key|client[_-]?secret|credential)[\w.-]*\s*[:=]\s*)\S+"
        ),
        r"\1***",
    ),
    # 通用 URL 凭据 user:pass@host(redis / mysql / mongodb / amqp / postgres…),不止 postgres。
    (re.compile(r"([a-z][a-z0-9+.-]*://[^:\s/@]*:)[^@\s]+@"), r"\1***@"),
    (re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"), "***@***"),
    (re.compile(r"\b1[3-9]\d{9}\b"), "1**********"),
]

_FLAG_MAP = {
    "ignorecase": re.IGNORECASE,
    "multiline": re.MULTILINE,
    "dotall": re.DOTALL,
}
_EXTERNAL_CACHE_LOCK = threading.Lock()
_EXTERNAL_CACHE: tuple[str, int, int, list[tuple[re.Pattern[str], str]]] | None = None


class RedactionRulesError(ValueError):
    """External redaction rules are configured but cannot be loaded safely."""


def _rules_path_from_env() -> Path | None:
    rules_file = os.environ.get("OPS_REDACTION_RULES_FILE")
    return Path(rules_file).expanduser() if rules_file else None


def _compile_external_rules(raw: Any) -> list[tuple[re.Pattern[str], str]]:
    if not isinstance(raw, list):
        raise RedactionRulesError("redaction rules file must contain a JSON list")

    patterns: list[tuple[re.Pattern[str], str]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise RedactionRulesError(f"redaction rule #{index} must be an object")
        pattern = item.get("pattern")
        replacement = item.get("replacement", "***")
        if not isinstance(pattern, str) or not pattern:
            raise RedactionRulesError(f"redaction rule #{index} requires a non-empty pattern")
        if not isinstance(replacement, str):
            raise RedactionRulesError(f"redaction rule #{index} replacement must be a string")

        raw_flags = item.get("flags", [])
        if not isinstance(raw_flags, list) or not all(isinstance(flag, str) for flag in raw_flags):
            raise RedactionRulesError(f"redaction rule #{index} flags must be a list of strings")
        flags = 0
        for flag in raw_flags:
            if flag not in _FLAG_MAP:
                raise RedactionRulesError(f"redaction rule #{index} has unknown flag {flag!r}")
            flags |= _FLAG_MAP[flag]

        try:
            patterns.append((re.compile(pattern, flags), replacement))
        except re.error as exc:
            raise RedactionRulesError(f"redaction rule #{index} has invalid regex: {exc}") from exc
    return patterns


def _load_external_patterns(path: Path) -> list[tuple[re.Pattern[str], str]]:
    global _EXTERNAL_CACHE
    try:
        stat = path.stat()
    except OSError as exc:
        raise RedactionRulesError(f"cannot read redaction rules file {path}: {exc}") from exc

    cache_key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    with _EXTERNAL_CACHE_LOCK:
        if _EXTERNAL_CACHE and _EXTERNAL_CACHE[:3] == cache_key:
            return _EXTERNAL_CACHE[3]

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RedactionRulesError(f"invalid redaction rules JSON in {path}: {exc}") from exc

    patterns = _compile_external_rules(raw)
    with _EXTERNAL_CACHE_LOCK:
        _EXTERNAL_CACHE = (*cache_key, patterns)
    return patterns


def _external_patterns() -> list[tuple[re.Pattern[str], str]]:
    path = _rules_path_from_env()
    return _load_external_patterns(path) if path else []


def validate_redaction_rules(path: Path | None = None) -> None:
    target = path or _rules_path_from_env()
    if target:
        _load_external_patterns(target)


def redact_text(text: str) -> str:
    redacted = text
    for pattern, replacement in [*_PATTERNS, *_external_patterns()]:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if is_dataclass(value) and not isinstance(value, type):
        return redact(asdict(value))
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value
