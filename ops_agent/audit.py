"""Audit log helpers."""

import hashlib
import json
import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops_agent.redaction import redact

# 审计文件按大小滚动,防无限增长。生产应再把滚动后的归档投递到集中/不可篡改存储。
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_ROTATE_KEEP = 5
# 进程级锁 + 文件锁:ThreadingHTTPServer 并发请求和多进程 worker 都可能写同一审计文件;
# 滚动+追加须互斥,否则竞态丢记录。
_AUDIT_LOCK = threading.Lock()
_AUDIT_ACTOR: ContextVar[str | None] = ContextVar("ops_agent_audit_actor", default=None)
_ACTOR_RE = re.compile(r"^[A-Za-z0-9_.@:-]{1,128}$")


def _audit_max_bytes() -> int:
    try:
        return int(os.environ.get("OPS_AUDIT_MAX_BYTES", _DEFAULT_MAX_BYTES))
    except ValueError:
        return _DEFAULT_MAX_BYTES


def _audit_rotate_keep() -> int:
    try:
        return max(1, int(os.environ.get("OPS_AUDIT_ROTATE_KEEP", _DEFAULT_ROTATE_KEEP)))
    except ValueError:
        return _DEFAULT_ROTATE_KEEP


def _audit_actor() -> str:
    actor = (
        _AUDIT_ACTOR.get()
        or os.environ.get("OPS_ACTOR")
        or os.environ.get("USER")
        or os.environ.get("USERNAME")
        or "unknown"
    )
    return _normalize_actor(actor)


def _normalize_actor(actor: str | None) -> str:
    if actor and _ACTOR_RE.match(actor.strip()):
        return actor.strip()
    return "unknown"


@contextmanager
def audit_actor(actor: str | None) -> Iterator[None]:
    token = _AUDIT_ACTOR.set(actor or None)
    try:
        yield
    finally:
        _AUDIT_ACTOR.reset(token)


class _FileLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: Any | None = None

    def __enter__(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a", encoding="utf-8")
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            # Windows / unusual filesystems: process-local lock still protects threaded servers.
            pass
        return None

    def __exit__(self, *_exc: object) -> None:
        if self._fh is None:
            return
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        finally:
            self._fh.close()
            self._fh = None


def _rotate_if_large(path: Path) -> None:
    """文件超阈值则滚动为 <name>.1..<keep>,保留多档本地归档。"""
    if path.exists() and path.stat().st_size >= _audit_max_bytes():
        keep = _audit_rotate_keep()
        oldest = path.with_name(f"{path.name}.{keep}")
        if oldest.exists():
            oldest.unlink()
        for index in range(keep - 1, 0, -1):
            src = path.with_name(f"{path.name}.{index}")
            if src.exists():
                src.replace(path.with_name(f"{path.name}.{index + 1}"))
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
    with _AUDIT_LOCK, _FileLock(path.with_name(path.name + ".lock")):
        prev_hash = _last_hash(path)
        _rotate_if_large(path)
        actor = _normalize_actor(str(record.get("actor", "")))
        chained = redact(record)
        chained["actor"] = actor
        chained["prev_hash"] = prev_hash
        chained["hash"] = _record_hash(chained)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(chained, ensure_ascii=False) + "\n")


def append_approval_record(
    path: Path,
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    approved: bool,
    actor: str | None = None,
) -> None:
    _write_record(
        path,
        {
            "type": "approval",
            "timestamp": datetime.now(UTC).isoformat(),
            "actor": _normalize_actor(actor) if actor else _audit_actor(),
            "tool_name": tool_name,
            "tool_input": tool_input,
            "approved": approved,
        },
    )


def append_queue_record(
    path: Path,
    *,
    event: str,
    job_id: str,
    outcome: str,
    detail: str | None = None,
    actor: str | None = None,
) -> None:
    """队列运维动作留痕(P2-8):DLQ 回灌(QUEUE_REQUEUE)与 reaper 判死回收(QUEUE_REAP)
    改变任务命运却曾不入审计 —— 与审批/执行记录同一条本地 hash chain,无网络依赖。"""
    _write_record(
        path,
        {
            "type": "queue",
            "timestamp": datetime.now(UTC).isoformat(),
            "actor": _normalize_actor(actor) if actor else _audit_actor(),
            "event": event,
            "job_id": job_id,
            "outcome": outcome,
            "detail": detail,
        },
    )


def append_execution_record(
    path: Path,
    *,
    tool_name: str,
    ok: bool,
    dry_run: bool,
    detail: str | None = None,
    actor: str | None = None,
) -> None:
    """危险动作批准后真正执行的结果留痕(成败 / 是否 dry-run),补"只记批没批"的审计盲区。"""
    _write_record(
        path,
        {
            "type": "execution",
            "timestamp": datetime.now(UTC).isoformat(),
            "actor": _normalize_actor(actor) if actor else _audit_actor(),
            "tool_name": tool_name,
            "ok": ok,
            "dry_run": dry_run,
            "detail": detail,
        },
    )
