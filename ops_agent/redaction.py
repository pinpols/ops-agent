"""Redaction helpers for persisted artifacts."""

import re
from dataclasses import asdict, is_dataclass
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


def redact_text(text: str) -> str:
    redacted = text
    for pattern, replacement in _PATTERNS:
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
