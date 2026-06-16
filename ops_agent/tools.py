"""Agent 可调用的工具。每个工具 = agent 的"手",从第一个就立"白名单 + 只读 + 限量"规矩。

阶段 2 只有 read_logs(读日志)。query_pg(只读 SQL)等留到后面。
"""

import os
import re
from pathlib import Path

# 服务名白名单:只允许小写字母/数字/连字符。挡掉路径穿越(.. / /),
# 否则模型(或被注入的日志内容)可诱导读任意文件。
_SERVICE_RE = re.compile(r"^[a-z0-9-]+$")


def _log_dir() -> Path:
    # 默认指向项目 data/(样本日志在此);真用时设 OPS_LOG_DIR=../file-batch-system/logs/app
    return Path(os.environ.get("OPS_LOG_DIR", "data")).resolve()


def read_logs(service: str, pattern: str | None = None, max_lines: int = 200) -> str:
    """读取某服务的日志(只读),可选正则过滤,尾部截断 max_lines 行。

    :param service: 服务名(白名单 ^[a-z0-9-]+$),匹配日志目录下 *<service>*.log
    :param pattern: 可选正则,只保留匹配的行(如 'WARN|ERROR|Exception')
    :param max_lines: 最多返回多少行(从尾部取,最新优先);防止把整个大日志喂进上下文
    """
    if not _SERVICE_RE.match(service or ""):
        return f"[read_logs] 非法 service 名:{service!r}(只允许小写字母/数字/连字符)"

    base = _log_dir()
    # 只在日志目录内 glob *<service>*.log;resolve 后再确认仍在 base 下(双保险防穿越)
    matches = sorted(p for p in base.glob(f"*{service}*.log") if p.resolve().is_relative_to(base))
    if not matches:
        return f"[read_logs] 未找到 service={service} 的日志(目录 {base})"

    out_lines: list[str] = []
    rx = re.compile(pattern) if pattern else None
    for path in matches:
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    if rx is None or rx.search(line):
                        out_lines.append(line.rstrip("\n"))
        except OSError as e:
            out_lines.append(f"[read_logs] 读取 {path.name} 失败:{e}")

    if not out_lines:
        return f"[read_logs] service={service} 命中 0 行(pattern={pattern!r})"

    # 尾部 max_lines(最新),并标注截断
    truncated = len(out_lines) > max_lines
    tail = out_lines[-max_lines:]
    header = f"[read_logs] service={service} pattern={pattern!r} 返回 {len(tail)} 行" + (
        f"(共 {len(out_lines)} 行,已截断尾部)" if truncated else ""
    )
    return header + "\n" + "\n".join(tail)


# 工具的 JSON Schema(给模型看):描述 + 参数。description 直接影响模型调得准不准。
READ_LOGS_TOOL = {
    "name": "read_logs",
    "description": "读取指定服务的日志做排查。先用它取数据,再下结论。可用 pattern 正则过滤关键行。",
    "input_schema": {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "description": "服务名,如 console / orchestrator / worker-import(小写字母/数字/连字符)",
            },
            "pattern": {
                "type": "string",
                "description": "可选正则,只保留匹配行,如 'WARN|ERROR|Exception|timeout'",
            },
            "max_lines": {
                "type": "integer",
                "description": "最多返回行数(尾部最新优先),默认 200",
            },
        },
        "required": ["service"],
    },
}

# 工具名 → 实现 的派发表(执行 tool_use 时用)
TOOL_IMPLS = {"read_logs": read_logs}
