"""Bounded HTTP response reads for stdlib urllib clients."""

from typing import Any


def read_limited_text(resp: Any, *, max_bytes: int) -> tuple[str, bool, int]:
    """Read at most max_bytes + 1 bytes, then decode.

    Returns (text, truncated, observed_bytes). The observed byte count is capped at max_bytes + 1;
    callers only use it for telemetry/truncation decisions, not exact Content-Length accounting.
    """
    limit = max(0, max_bytes)
    raw = resp.read(limit + 1)
    truncated = len(raw) > limit
    if truncated:
        raw = raw[:limit]
    return raw.decode("utf-8", errors="replace"), truncated, len(raw)
