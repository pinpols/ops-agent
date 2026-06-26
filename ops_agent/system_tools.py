"""System-aware read-only tools for the target batch system."""

import fnmatch
import os
import re
from collections import deque
from pathlib import Path

from ops_agent.config import get_settings
from ops_agent.tool_result import ToolResult
from ops_agent.tools import (
    _LOG_SCAN_BYTES,
    _coerce_positive_limit,
    _iter_tail_lines,
    iter_log_files_bounded,
)

# 服务名白名单:只允许小写字母/数字/连字符,挡掉路径穿越(.. / / / 空字节)。
# 否则模型(或被注入的日志/配置内容)可诱导 read_app_config 读 OPS_TARGET_ROOT 外的任意文件。
_SERVICE_RE = re.compile(r"^[a-z0-9-]+$")

_ERROR_PATTERNS = ("ERROR", "WARN", "Exception", "timeout", "refused", "No space", "Lock")
_CONFIG_GLOBS = ("application*.yml", "application*.yaml", "application*.properties")
_COMPOSE_FILE_CAP = 10
_COMPOSE_SCAN_BYTES = 512 * 1024
_CONFIG_FILE_CAP = 50
_CONFIG_SCAN_BYTES = 256 * 1024
_CONFIG_DIR_CAP = 500
_CONFIG_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".venv",
    "venv",
    "node_modules",
    "target",
    "build",
    "dist",
    "__pycache__",
}

_MODULE_SERVICE_MAP = {
    "batch-console-api": "console",
    "batch-orchestrator": "orchestrator",
    "batch-trigger": "trigger",
    "batch-worker-import": "worker-import",
    "batch-worker-export": "worker-export",
    "batch-worker-process": "worker-process",
    "batch-worker-dispatch": "worker-dispatch",
    "batch-worker-atomic": "worker-atomic",
}


def _target_root() -> Path | None:
    return get_settings().ops_target_root


def _log_dir() -> Path:
    return get_settings().ops_log_dir


def _service_to_module(service: str) -> str:
    for module, mapped in _MODULE_SERVICE_MAP.items():
        if mapped == service:
            return module
    return f"batch-{service}"


def _read_bounded_lines(path: Path, *, max_bytes: int) -> tuple[list[str], bool]:
    with path.open("rb") as f:
        data = f.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    return data.decode("utf-8", errors="replace").splitlines(), truncated


def _find_config_files_bounded(search_roots: list[Path]) -> tuple[list[Path], bool, int]:
    files: list[Path] = []
    scanned_dirs = 0
    truncated = False
    for base in search_roots:
        if not base.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            scanned_dirs += 1
            if scanned_dirs > _CONFIG_DIR_CAP:
                truncated = True
                break
            dirnames[:] = sorted(d for d in dirnames if d not in _CONFIG_SKIP_DIRS)
            for filename in sorted(filenames):
                if any(fnmatch.fnmatch(filename, pattern) for pattern in _CONFIG_GLOBS):
                    files.append(Path(dirpath) / filename)
                    if len(files) >= _CONFIG_FILE_CAP:
                        truncated = True
                        break
            if truncated:
                break
        if truncated:
            break
    return files, truncated, scanned_dirs


def list_services_result() -> ToolResult:
    """List services inferred from target modules and log files."""
    settings = get_settings()
    services: dict[str, dict[str, str | None]] = {}
    extra_logs: list[str] = []

    if settings.ops_target_root and settings.ops_target_root.exists():
        for module, service in _MODULE_SERVICE_MAP.items():
            module_dir = settings.ops_target_root / module
            if module_dir.exists():
                services.setdefault(service, {})["module"] = str(module_dir)

    log_files_truncated = False
    if settings.ops_log_dir.exists():
        log_files, log_files_truncated = iter_log_files_bounded(settings.ops_log_dir)
        for path in log_files:
            if path.stat().st_size == 0:
                continue
            name = path.stem
            if name in _MODULE_SERVICE_MAP.values() or name.startswith(
                ("worker-", "orchestrator", "console", "trigger")
            ):
                services.setdefault(name, {})["log"] = str(path)
            else:
                extra_logs.append(str(path))

    if not services:
        return ToolResult.failure(
            "[list_services] 未发现服务模块或日志文件",
            target_root=str(settings.ops_target_root) if settings.ops_target_root else None,
            log_dir=str(settings.ops_log_dir),
        )

    lines = ["[list_services] 可诊断服务:"]
    for service, meta in sorted(services.items()):
        module = meta.get("module", "-")
        log = meta.get("log", "-")
        lines.append(f"- {service}: module={module} log={log}")
    if extra_logs:
        lines.append("额外日志:")
        lines.extend(f"- {path}" for path in extra_logs)
    return ToolResult.success(
        "\n".join(lines),
        services=sorted(services),
        extra_logs=extra_logs,
        count=len(services),
        log_dir=str(settings.ops_log_dir),
        log_files_truncated=log_files_truncated,
        target_root=str(settings.ops_target_root) if settings.ops_target_root else None,
    )


def list_services() -> str:
    return list_services_result().to_text()


def tail_recent_errors_result(max_lines: int = 200) -> ToolResult:
    """Scan target logs and return recent warning/error-like lines."""
    limit, err = _coerce_positive_limit(
        max_lines, default=200, cap=1000, name="tail_recent_errors.max_lines"
    )
    if err:
        return ToolResult.failure(err)

    log_dir = _log_dir()
    if not log_dir.exists():
        return ToolResult.failure(
            f"[tail_recent_errors] 日志目录不存在:{log_dir}", log_dir=str(log_dir)
        )

    log_files, truncated_files = iter_log_files_bounded(log_dir)
    hits: deque[str] = deque(maxlen=limit)
    matched_lines = 0
    scanned_files = 0
    scanned_bytes = 0
    for path in log_files:
        scanned_files += 1
        try:
            scanned_bytes += min(path.stat().st_size, _LOG_SCAN_BYTES)
            for line in _iter_tail_lines(path):
                if any(pattern in line for pattern in _ERROR_PATTERNS):
                    matched_lines += 1
                    hits.append(f"{path.name}: {line}")
        except OSError as e:
            hits.append(f"{path.name}: [读取失败] {e}")

    if not hits:
        return ToolResult.failure(
            f"[tail_recent_errors] {log_dir} 未命中 WARN/ERROR/Exception 等关键行",
            log_dir=str(log_dir),
            scanned_files=scanned_files,
            truncated_files=truncated_files,
        )

    tail = list(hits)
    truncated = matched_lines > len(tail) or truncated_files
    header = f"[tail_recent_errors] 返回 {len(tail)} 行" + (
        f"(命中 {matched_lines} 行,已按资源上限截断)" if truncated else ""
    )
    return ToolResult.success(
        header + "\n" + "\n".join(tail),
        log_dir=str(log_dir),
        matched_lines=matched_lines,
        returned_lines=len(tail),
        scanned_files=scanned_files,
        scanned_bytes=scanned_bytes,
        truncated=truncated,
    )


def tail_recent_errors(max_lines: int = 200) -> str:
    return tail_recent_errors_result(max_lines).to_text()


def inspect_compose_result(max_chars: int = 6000) -> ToolResult:
    """Summarize docker compose files relevant to runtime dependencies."""
    limit, err = _coerce_positive_limit(
        max_chars, default=6000, cap=20000, name="inspect_compose.max_chars"
    )
    if err:
        return ToolResult.failure(err)
    root = _target_root()
    if not root or not root.exists():
        return ToolResult.failure("[inspect_compose] 未配置或未找到 OPS_TARGET_ROOT")

    files = sorted(root.glob("docker-compose*.yml")) + sorted(root.glob("docker-compose*.yaml"))
    if not files:
        return ToolResult.failure(f"[inspect_compose] {root} 下未找到 docker-compose*.yml")
    truncated_files = len(files) > _COMPOSE_FILE_CAP
    files = files[:_COMPOSE_FILE_CAP]

    interesting = (
        "services:",
        "image:",
        "container_name:",
        "ports:",
        "postgres",
        "kafka",
        "redis",
        "valkey",
    )
    chunks: list[str] = []
    truncated_inputs: list[str] = []
    for path in files:
        lines = []
        raw_lines, input_truncated = _read_bounded_lines(path, max_bytes=_COMPOSE_SCAN_BYTES)
        if input_truncated:
            truncated_inputs.append(str(path))
        for idx, line in enumerate(raw_lines, 1):
            if any(token in line.lower() for token in interesting):
                lines.append(f"{idx}: {line}")
        if lines:
            chunks.append(f"## {path.name}\n" + "\n".join(lines))

    content = "[inspect_compose] compose 摘要\n" + "\n\n".join(chunks)
    truncated = len(content) > limit or truncated_files or bool(truncated_inputs)
    return ToolResult.success(
        content[:limit],
        target_root=str(root),
        files=[str(p) for p in files],
        scanned_bytes_cap=_COMPOSE_SCAN_BYTES,
        truncated_files=truncated_files,
        truncated_inputs=truncated_inputs,
        truncated=truncated,
    )


def inspect_compose(max_chars: int = 6000) -> str:
    return inspect_compose_result(max_chars).to_text()


def read_app_config_result(service: str | None = None, max_chars: int = 6000) -> ToolResult:
    """Read Spring application config for one service or summarize all configs."""
    limit, err = _coerce_positive_limit(
        max_chars, default=6000, cap=20000, name="read_app_config.max_chars"
    )
    if err:
        return ToolResult.failure(err)
    root = _target_root()
    if not root or not root.exists():
        return ToolResult.failure("[read_app_config] 未配置或未找到 OPS_TARGET_ROOT")

    if service is not None and not _SERVICE_RE.match(service):
        return ToolResult.failure(
            f"[read_app_config] 非法 service={service!r}(只允许小写字母/数字/连字符)"
        )

    search_roots = [root / _service_to_module(service)] if service else [root]
    files, truncated_file_search, scanned_dirs = _find_config_files_bounded(search_roots)

    # 纵深防御:即便服务名/glob 出岔,命中文件也必须落在 root 内(防 symlink/穿越外泄)。
    safe_files = []
    for f in files:
        try:
            if f.resolve().is_relative_to(root):
                safe_files.append(f)
        except OSError:
            continue
    files = sorted(set(safe_files))
    if not files:
        suffix = f" service={service}" if service else ""
        if truncated_file_search:
            return ToolResult.failure(
                f"[read_app_config] 搜索达到目录上限,未能确认 application 配置是否存在{suffix}",
                target_root=str(root),
                service=service,
                scanned_dirs=scanned_dirs,
                scanned_dirs_cap=_CONFIG_DIR_CAP,
                truncated_file_search=True,
            )
        return ToolResult.failure(f"[read_app_config] 未找到 application 配置{suffix}")

    key_tokens = (
        "spring:",
        "datasource",
        "kafka",
        "redis",
        "valkey",
        "server:",
        "port:",
        "profile",
    )
    chunks: list[str] = []
    truncated_inputs: list[str] = []
    for path in files:
        rel = path.relative_to(root)
        raw_lines, input_truncated = _read_bounded_lines(path, max_bytes=_CONFIG_SCAN_BYTES)
        if input_truncated:
            truncated_inputs.append(str(rel))
        picked = [line for line in raw_lines if any(token in line.lower() for token in key_tokens)]
        body = "\n".join(picked or raw_lines[:80])
        chunks.append(f"## {rel}\n{body}")

    content = "[read_app_config] 应用配置摘要\n" + "\n\n".join(chunks)
    truncated = len(content) > limit or truncated_file_search or bool(truncated_inputs)
    return ToolResult.success(
        content[:limit],
        target_root=str(root),
        service=service,
        files=[str(p) for p in files],
        scanned_bytes_cap=_CONFIG_SCAN_BYTES,
        scanned_dirs=scanned_dirs,
        scanned_dirs_cap=_CONFIG_DIR_CAP,
        truncated_file_search=truncated_file_search,
        truncated_inputs=truncated_inputs,
        truncated=truncated,
    )


def read_app_config(service: str | None = None, max_chars: int = 6000) -> str:
    return read_app_config_result(service, max_chars).to_text()


LIST_SERVICES_TOOL = {
    "name": "list_services",
    "description": "列出目标系统中可诊断的服务、模块和日志文件。用户不知道服务名时先调用它。",
    "input_schema": {"type": "object", "properties": {}},
}

TAIL_RECENT_ERRORS_TOOL = {
    "name": "tail_recent_errors",
    "description": "扫描目标日志目录最近 WARN/ERROR/Exception/timeout 等关键行,用于快速发现异常。",
    "input_schema": {
        "type": "object",
        "properties": {
            "max_lines": {
                "type": "integer",
                "description": "最多返回行数,默认 200,上限 1000",
                "minimum": 1,
                "maximum": 1000,
            }
        },
    },
}

INSPECT_COMPOSE_TOOL = {
    "name": "inspect_compose",
    "description": "读取目标系统 docker-compose 摘要,识别 PG/Kafka/Redis/Valkey 和服务端口。",
    "input_schema": {
        "type": "object",
        "properties": {
            "max_chars": {
                "type": "integer",
                "description": "最多返回字符数,默认 6000",
                "minimum": 1,
                "maximum": 20000,
            }
        },
    },
}

READ_APP_CONFIG_TOOL = {
    "name": "read_app_config",
    "description": "读取 Spring application 配置摘要,可指定服务名如 worker-import/orchestrator。",
    "input_schema": {
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "可选服务名"},
            "max_chars": {
                "type": "integer",
                "description": "最多返回字符数,默认 6000",
                "minimum": 1,
                "maximum": 20000,
            },
        },
    },
}

SYSTEM_TOOLS = [
    LIST_SERVICES_TOOL,
    TAIL_RECENT_ERRORS_TOOL,
    INSPECT_COMPOSE_TOOL,
    READ_APP_CONFIG_TOOL,
]

SYSTEM_TOOL_IMPLS = {
    "list_services": list_services,
    "tail_recent_errors": tail_recent_errors,
    "inspect_compose": inspect_compose,
    "read_app_config": read_app_config,
}

SYSTEM_TOOL_RESULT_IMPLS = {
    "list_services": list_services_result,
    "tail_recent_errors": tail_recent_errors_result,
    "inspect_compose": inspect_compose_result,
    "read_app_config": read_app_config_result,
}
