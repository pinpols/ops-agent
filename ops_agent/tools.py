"""Agent 可调用的工具。每个工具 = agent 的"手",从第一个就立"白名单 + 只读 + 限量"规矩。

阶段 2 只有 read_logs(读日志)。query_pg(只读 SQL)等留到后面。
"""

import re
from pathlib import Path

from ops_agent.config import get_settings
from ops_agent.tool_result import ToolResult

# 服务名白名单:只允许小写字母/数字/连字符。挡掉路径穿越(.. / /),
# 否则模型(或被注入的日志内容)可诱导读任意文件。
_SERVICE_RE = re.compile(r"^[a-z0-9-]+$")
_LOG_LINE_CAP = 1000
_QUERY_ROW_CAP = 200


def _coerce_positive_limit(
    value: object, *, default: int, cap: int, name: str
) -> tuple[int | None, str | None]:
    """Normalize model-supplied row/line limits before using them for slicing/fetching."""
    if value is None:
        return default, None
    if isinstance(value, bool):
        return None, f"[{name}] 必须是正整数,收到 {value!r}"
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return None, f"[{name}] 必须是正整数,收到 {value!r}"
    if limit <= 0:
        return None, f"[{name}] 必须大于 0,收到 {limit}"
    return min(limit, cap), None


def _log_dir() -> Path:
    # 默认指向项目 data/(样本日志在此);真用时设 OPS_LOG_DIR=../file-batch-system/logs/app
    return get_settings().ops_log_dir


def read_logs_result(service: str, pattern: str | None = None, max_lines: int = 200) -> ToolResult:
    """读取某服务的日志(只读),返回结构化工具结果。"""
    if not isinstance(service, str) or not _SERVICE_RE.match(service):
        return ToolResult.failure(
            f"[read_logs] 非法 service 名:{service!r}(只允许小写字母/数字/连字符)",
            service=service,
        )
    if pattern is not None and not isinstance(pattern, str):
        return ToolResult.failure(
            f"[read_logs] pattern 必须是字符串或空,收到 {type(pattern).__name__}",
            service=service,
        )
    limit, err = _coerce_positive_limit(
        max_lines, default=200, cap=_LOG_LINE_CAP, name="read_logs.max_lines"
    )
    if err:
        return ToolResult.failure(err, service=service, pattern=pattern)

    base = _log_dir()
    # 只在日志目录内 glob *<service>*.log;resolve 后再确认仍在 base 下(双保险防穿越)
    matches = sorted(p for p in base.glob(f"*{service}*.log") if p.resolve().is_relative_to(base))
    if not matches:
        return ToolResult.failure(
            f"[read_logs] 未找到 service={service} 的日志(目录 {base})",
            service=service,
            log_dir=str(base),
        )

    out_lines: list[str] = []
    try:
        rx = re.compile(pattern) if pattern else None
    except re.error as e:
        return ToolResult.failure(f"[read_logs] pattern 正则非法:{e}", service=service)
    for path in matches:
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    if rx is None or rx.search(line):
                        out_lines.append(line.rstrip("\n"))
        except OSError as e:
            out_lines.append(f"[read_logs] 读取 {path.name} 失败:{e}")

    if not out_lines:
        return ToolResult.failure(
            f"[read_logs] service={service} 命中 0 行(pattern={pattern!r})",
            service=service,
            pattern=pattern,
            matched_files=len(matches),
        )

    # 尾部 max_lines(最新),并标注截断
    truncated = len(out_lines) > limit
    tail = out_lines[-limit:]
    header = f"[read_logs] service={service} pattern={pattern!r} 返回 {len(tail)} 行" + (
        f"(共 {len(out_lines)} 行,已截断尾部)" if truncated else ""
    )
    return ToolResult.success(
        header + "\n" + "\n".join(tail),
        service=service,
        pattern=pattern,
        returned_lines=len(tail),
        matched_lines=len(out_lines),
        matched_files=len(matches),
        truncated=truncated,
    )


def read_logs(service: str, pattern: str | None = None, max_lines: int = 200) -> str:
    """读取某服务的日志(只读),可选正则过滤,尾部截断 max_lines 行。

    :param service: 服务名(白名单 ^[a-z0-9-]+$),匹配日志目录下 *<service>*.log
    :param pattern: 可选正则,只保留匹配的行(如 'WARN|ERROR|Exception')
    :param max_lines: 最多返回多少行(从尾部取,最新优先);防止把整个大日志喂进上下文
    """
    return read_logs_result(service, pattern, max_lines).to_text()


# 工具的 JSON Schema(给模型看):描述 + 参数。description 直接影响模型调得准不准。
READ_LOGS_TOOL = {
    "name": "read_logs",
    "description": "读取指定服务的日志做排查。先用它取数据,再下结论。可用 pattern 正则过滤关键行。",
    "input_schema": {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "description": (
                    "服务名,如 console / orchestrator / worker-import(小写字母/数字/连字符)"
                ),
            },
            "pattern": {
                "type": "string",
                "description": "可选正则,只保留匹配行,如 'WARN|ERROR|Exception|timeout'",
            },
            "max_lines": {
                "type": "integer",
                "description": "最多返回行数(尾部最新优先),默认 200",
                "minimum": 1,
                "maximum": _LOG_LINE_CAP,
            },
        },
        "required": ["service"],
    },
}

# ── query_pg:只读 SQL 查询(强工具 → 多层护栏)─────────────────────────────────
# 黑名单(第一道、给清晰报错);真正承重的是连接级 default_transaction_read_only=on。
_SQL_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|"
    r"merge|call|do|vacuum|reindex|comment|lock)\b",
    re.IGNORECASE,
)


def query_pg_result(sql: str, max_rows: int = 50) -> ToolResult:
    """对平台库执行**只读** SQL,返回结构化工具结果。"""
    if not isinstance(sql, str):
        return ToolResult.failure(f"[query_pg] SQL 必须是字符串,收到 {type(sql).__name__}")
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        return ToolResult.failure("[query_pg] 空 SQL")
    low = s.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return ToolResult.failure("[query_pg] 只允许 SELECT / WITH 查询", sql=s)
    if ";" in s:
        return ToolResult.failure("[query_pg] 禁止多语句(含 ;)", sql=s)
    if _SQL_FORBIDDEN.search(s):
        return ToolResult.failure("[query_pg] 含被禁关键词(只读工具,不允许写/DDL)", sql=s)
    cap, err = _coerce_positive_limit(
        max_rows, default=50, cap=_QUERY_ROW_CAP, name="query_pg.max_rows"
    )
    if err:
        return ToolResult.failure(err, sql=s)

    dsn = get_settings().ops_pg_dsn
    if not dsn:
        return ToolResult.failure(
            "[query_pg] 未配 OPS_PG_DSN(如 postgresql://user:pass@localhost:5432/db),跳过",
            sql=s,
        )

    try:
        import psycopg
    except ImportError:
        return ToolResult.failure("[query_pg] 未装 psycopg:pip install 'psycopg[binary]'", sql=s)

    try:
        # 连接级只读 + 语句超时(最硬的护栏:就算字符串闸被绕,DB 也拒绝写/慢查询)
        with (
            psycopg.connect(
                dsn,
                autocommit=True,
                options="-c default_transaction_read_only=on -c statement_timeout=5000",
            ) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(s)
            cols = [d.name for d in cur.description] if cur.description else []
            rows = cur.fetchmany(cap)
    except Exception as e:  # noqa: BLE001 — 工具边界,任何 DB 错都转成给模型的文本
        return ToolResult.failure(f"[query_pg] 执行失败:{type(e).__name__}: {e}", sql=s)

    if not rows:
        return ToolResult.success(f"[query_pg] 0 行。列:{cols}", sql=s, columns=cols, row_count=0)
    lines = [" | ".join(cols), "-" * 40]
    lines += [" | ".join(str(v) for v in r) for r in rows]
    more = f"\n(已截断,最多 {cap} 行)" if len(rows) == cap else ""
    return ToolResult.success(
        "\n".join(lines) + more,
        sql=s,
        columns=cols,
        row_count=len(rows),
        truncated=len(rows) == cap,
    )


def query_pg(sql: str, max_rows: int = 50) -> str:
    """对平台库执行**只读** SQL,返回行(截断)。需配 OPS_PG_DSN。

    :param sql: 单条 SELECT/WITH 查询(禁多语句、禁任何写)
    :param max_rows: 最多返回行数(上限 200)
    """
    return query_pg_result(sql, max_rows).to_text()


QUERY_PG_TOOL = {
    "name": "query_pg",
    "description": (
        "对平台库执行只读 SQL 查那些日志看不到的运行态(如 pg_stat_activity 锁等待、"
        "job/任务状态计数)。只允许单条 SELECT/WITH。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "单条只读 SELECT/WITH 查询"},
            "max_rows": {
                "type": "integer",
                "description": "最多返回行数,默认 50,上限 200",
                "minimum": 1,
                "maximum": _QUERY_ROW_CAP,
            },
        },
        "required": ["sql"],
    },
}


# 工具名 → 实现 的派发表(执行 tool_use 时用)
TOOL_IMPLS = {"read_logs": read_logs, "query_pg": query_pg}
TOOL_RESULT_IMPLS = {"read_logs": read_logs_result, "query_pg": query_pg_result}
