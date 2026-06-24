"""Audit log helpers."""

import hashlib
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops_agent.redaction import redact

# 审计文件按大小滚动,防无限增长。生产应再把滚动后的归档投递到集中/不可篡改存储。
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
# 进程级锁:ThreadingHTTPServer 并发请求会并发写同一审计文件;滚动+追加须互斥,否则竞态丢记录。
# 注:仅护单进程内线程;多进程部署须改 fcntl.flock。
_AUDIT_LOCK = threading.Lock()


def _audit_max_bytes() -> int:
    try:
        return int(os.environ.get("OPS_AUDIT_MAX_BYTES", _DEFAULT_MAX_BYTES))
    except ValueError:
        return _DEFAULT_MAX_BYTES


def _rotate_if_large(path: Path) -> None:
    """文件超阈值则滚动为 <name>.1(覆盖旧归档)。轻量单档滚动,够审计留存基线用。"""
    if path.exists() and path.stat().st_size >= _audit_max_bytes():
        path.replace(path.with_name(path.name + ".1"))


def _canonical_record(record: dict[str, Any]) -> bytes:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _record_hash(record: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_record(record)).hexdigest()


def _last_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        for line in reversed(f.read().splitlines()):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                return None
            hash_value = value.get("hash")
            return str(hash_value) if hash_value else None
    return None


def _write_record(path: Path, record: dict[str, Any]) -> None:
    """滚动 + 追加一条记录,全程持锁(防并发竞态丢审计),并写入 hash chain。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT_LOCK:
        _rotate_if_large(path)
        chained = redact(record)
        chained["prev_hash"] = _last_hash(path)
        chained["hash"] = _record_hash(chained)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(chained, ensure_ascii=False) + "\n")


def append_approval_record(
    path: Path,
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    approved: bool,
) -> None:
    _write_record(
        path,
        {
            "type": "approval",
            "timestamp": datetime.now(UTC).isoformat(),
            "tool_name": tool_name,
            "tool_input": tool_input,
            "approved": approved,
        },
    )


def append_execution_record(
    path: Path,
    *,
    tool_name: str,
    ok: bool,
    dry_run: bool,
    detail: str | None = None,
) -> None:
    """危险动作批准后真正执行的结果留痕(成败 / 是否 dry-run),补"只记批没批"的审计盲区。"""
    _write_record(
        path,
        {
            "type": "execution",
            "timestamp": datetime.now(UTC).isoformat(),
            "tool_name": tool_name,
            "ok": ok,
            "dry_run": dry_run,
            "detail": detail,
        },
    )
