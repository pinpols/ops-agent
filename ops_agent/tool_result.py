"""Structured tool execution results."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: str = ""
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_text(self) -> str:
        return self.content if self.ok else self.error or self.content

    @classmethod
    def success(cls, content: str, **metadata: Any) -> "ToolResult":
        return cls(ok=True, content=content, metadata=metadata)

    @classmethod
    def failure(cls, error: str, **metadata: Any) -> "ToolResult":
        return cls(ok=False, error=error, metadata=metadata)
