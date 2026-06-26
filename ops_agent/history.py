"""诊断历史持久层(SQLite,零依赖)——可查询的运维诊断运行记录 + 留存策略。

与 `audit.py` 边界清晰、互不重叠:
- `audit.py` = 哈希链防篡改的**安全审计**(审批/执行事件),合规取证用。
- 本模块 = 运维**诊断历史**(每次诊断的结论 + 元数据),供查询/趋势/导出/留存。

企业级要点:
- **可查询**:按 target / severity / 时间范围查近期诊断,不再只能翻 JSONL。
- **留存**:`prune(retention_days, max_rows)` 按时间 + 行数双闸清旧,防无限增长(可 cron)。
- **导出**:`export()` 一键导 JSON,供归档/外部分析。
- **脱敏入库**:question / summary / root_cause 等文本入库前过 `redact_text`,DB 里不留明文凭据。
- **线程安全**:ThreadingHTTPServer 并发写 → 单连接 + 锁。多进程部署各自一库或换中心库。
"""

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ops_agent.redaction import redact_text

_SCHEMA = """
CREATE TABLE IF NOT EXISTS diagnosis_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  trace_id TEXT,
  target TEXT,
  question TEXT,
  severity TEXT,
  summary TEXT,
  root_cause TEXT,
  confidence REAL,
  model TEXT,
  prompt_version TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS idx_diagnosis_run_ts ON diagnosis_run(ts);
CREATE INDEX IF NOT EXISTS idx_diagnosis_run_target ON diagnosis_run(target);
"""

# 入库前需脱敏的自由文本字段(可能含凭据/PII)。
_REDACT_FIELDS = ("question", "summary", "root_cause")


@dataclass
class DiagnosisRun:
    """一次诊断的可持久化运行记录。"""

    severity: str
    summary: str
    root_cause: str
    confidence: float
    target: str | None = None
    question: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    trace_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class DiagnosisStore:
    """SQLite 诊断历史存储。线程安全(单连接 + 锁)。"""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + 进程锁:允许 server 多线程共享一个连接。
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def _now_iso(self) -> str:
        return datetime.now(UTC).isoformat()

    def record(self, run: DiagnosisRun) -> int:
        """落一条诊断记录(自由文本入库前脱敏),返回行 id。"""
        data = asdict(run)
        for field in _REDACT_FIELDS:
            if data.get(field):
                data[field] = redact_text(str(data[field]))
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO diagnosis_run "
                "(ts, trace_id, target, question, severity, summary, root_cause, "
                " confidence, model, prompt_version, input_tokens, output_tokens) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self._now_iso(),
                    data["trace_id"],
                    data["target"],
                    data["question"],
                    data["severity"],
                    data["summary"],
                    data["root_cause"],
                    data["confidence"],
                    data["model"],
                    data["prompt_version"],
                    data["input_tokens"],
                    data["output_tokens"],
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)  # INSERT 后必有 rowid;or 0 仅为消 mypy 的 None 联合

    def recent(self, limit: int = 50) -> list[dict]:
        """最近 limit 条(按时间倒序)。"""
        return self.query(limit=limit)

    def query(
        self,
        *,
        target: str | None = None,
        severity: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """按 target / severity / 时间下界过滤,时间倒序返回。"""
        clauses: list[str] = []
        params: list[object] = []  # 混 str/int 参数,显式宽类型避免 mypy 推成 list[str]
        if target:
            clauses.append("target = ?")
            params.append(target)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        if since:
            clauses.append("ts >= ?")
            params.append(since.isoformat())
        params.append(max(1, limit))
        query = "SELECT * FROM diagnosis_run ORDER BY id DESC LIMIT ?"
        if clauses:
            query = (
                "SELECT * FROM diagnosis_run WHERE "  # nosec
                + " AND ".join(clauses)
                + " ORDER BY id DESC LIMIT ?"
            )
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM diagnosis_run").fetchone()[0])

    def prune(self, retention_days: int, max_rows: int) -> int:
        """留存双闸:删早于 retention_days 的;再把总量压到 max_rows(删最旧)。返回删除行数。"""
        deleted = 0
        with self._lock:
            if retention_days > 0:
                cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
                cur = self._conn.execute("DELETE FROM diagnosis_run WHERE ts < ?", (cutoff,))
                deleted += cur.rowcount
            if max_rows > 0:
                total = int(self._conn.execute("SELECT COUNT(*) FROM diagnosis_run").fetchone()[0])
                if total > max_rows:
                    cur = self._conn.execute(
                        "DELETE FROM diagnosis_run WHERE id IN "
                        "(SELECT id FROM diagnosis_run ORDER BY id ASC LIMIT ?)",
                        (total - max_rows,),
                    )
                    deleted += cur.rowcount
            self._conn.commit()
        return deleted

    def export(self, out_path: Path) -> int:
        """导出全部记录为 JSON 数组(时间正序),返回条数。"""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM diagnosis_run ORDER BY id ASC").fetchall()
        records = [dict(r) for r in rows]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        return len(records)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
