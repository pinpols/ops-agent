"""诊断历史持久层单测:落库/查询/过滤/留存双闸/导出/脱敏入库。"""

import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from ops_agent.history import DiagnosisRun, DiagnosisStore


def _run(**kw) -> DiagnosisRun:
    base = dict(severity="WARNING", summary="s", root_cause="r", confidence=0.5)
    base.update(kw)
    return DiagnosisRun(**base)


class HistoryStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.store = DiagnosisStore(Path(self._tmp.name) / "sub" / "hist.db")  # 自动建父目录

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_record_and_recent(self):
        rid = self.store.record(_run(target="fbs", model="claude", input_tokens=10))
        self.assertGreater(rid, 0)
        rows = self.store.recent()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target"], "fbs")
        self.assertEqual(rows[0]["input_tokens"], 10)
        self.assertEqual(self.store.count(), 1)

    def test_recent_is_reverse_chronological(self):
        for i in range(3):
            self.store.record(_run(summary=f"s{i}"))
        rows = self.store.recent()
        self.assertEqual([r["summary"] for r in rows], ["s2", "s1", "s0"])  # 倒序

    def test_query_filters_target_and_severity(self):
        self.store.record(_run(target="a", severity="CRITICAL"))
        self.store.record(_run(target="b", severity="WARNING"))
        self.assertEqual(len(self.store.query(target="a")), 1)
        self.assertEqual(self.store.query(target="a")[0]["severity"], "CRITICAL")
        self.assertEqual(len(self.store.query(severity="WARNING")), 1)
        self.assertEqual(len(self.store.query(severity="INFO")), 0)

    def test_query_since(self):
        self.store.record(_run())
        future = datetime.now(UTC) + timedelta(hours=1)
        self.assertEqual(len(self.store.query(since=future)), 0)  # 未来下界 → 无
        past = datetime.now(UTC) - timedelta(hours=1)
        self.assertEqual(len(self.store.query(since=past)), 1)

    def test_redacts_secrets_before_storing(self):
        # DSN 口令必须脱敏后入库,DB 里不留明文
        self.store.record(_run(question="dsn=postgresql://u:supersecret@h/db"))
        stored = self.store.recent()[0]["question"]
        self.assertNotIn("supersecret", stored)

    def test_prune_by_max_rows_deletes_oldest(self):
        for i in range(5):
            self.store.record(_run(summary=f"s{i}"))
        deleted = self.store.prune(retention_days=0, max_rows=3)  # retention 关,只压行数
        self.assertEqual(deleted, 2)
        rows = self.store.recent()
        self.assertEqual(self.store.count(), 3)
        self.assertEqual({r["summary"] for r in rows}, {"s2", "s3", "s4"})  # 最旧 s0/s1 被删

    def test_prune_by_retention_days(self):
        self.store.record(_run(summary="fresh"))
        # 手动塞一条 100 天前的记录
        old_ts = (datetime.now(UTC) - timedelta(days=100)).isoformat()
        self.store._conn.execute(
            "INSERT INTO diagnosis_run (ts, severity, summary, root_cause, confidence) "
            "VALUES (?,?,?,?,?)",
            (old_ts, "INFO", "old", "r", 0.1),
        )
        self.store._conn.commit()
        deleted = self.store.prune(retention_days=90, max_rows=0)  # 行数闸关
        self.assertEqual(deleted, 1)
        self.assertEqual([r["summary"] for r in self.store.recent()], ["fresh"])

    def test_export_writes_json_array(self):
        self.store.record(_run(summary="a"))
        self.store.record(_run(summary="b"))
        out = Path(self._tmp.name) / "export.json"
        n = self.store.export(out)
        self.assertEqual(n, 2)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual([d["summary"] for d in data], ["a", "b"])  # 正序导出


if __name__ == "__main__":
    unittest.main()
