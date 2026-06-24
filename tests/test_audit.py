"""审计写入 + 按大小滚动留存单测。"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ops_agent.audit import _record_hash, append_approval_record, append_execution_record


class AuditTest(unittest.TestCase):
    def test_append_approval_and_execution(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            append_approval_record(
                path, tool_name="restart_service", tool_input={"service": "x"}, approved=True
            )
            append_execution_record(
                path, tool_name="restart_service", ok=True, dry_run=False, detail=None
            )
            lines = path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["type"], "approval")
        self.assertEqual(json.loads(lines[1])["type"], "execution")

    def test_records_include_verifiable_hash_chain(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            append_approval_record(
                path,
                tool_name="restart_service",
                tool_input={"service": "x", "token": "gho_SECRET123"},
                approved=True,
            )
            append_execution_record(
                path, tool_name="restart_service", ok=True, dry_run=False, detail=None
            )
            records = [
                json.loads(line) for line in path.read_text(encoding="utf-8").strip().splitlines()
            ]

        first, second = records
        self.assertIsNone(first["prev_hash"])
        self.assertEqual(second["prev_hash"], first["hash"])
        for record in records:
            persisted_hash = record.pop("hash")
            self.assertEqual(persisted_hash, _record_hash(record))
        self.assertNotIn("gho_SECRET123", repr(records))

    def test_rotation_when_over_size(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            path.write_text("x" * 100, encoding="utf-8")  # 预置超阈值内容
            # 阈值设 50 字节 → 下次 append 前先滚动为 audit.jsonl.1
            with patch.dict("os.environ", {"OPS_AUDIT_MAX_BYTES": "50"}, clear=False):
                append_approval_record(path, tool_name="t", tool_input={}, approved=False)
            rotated = path.with_name("audit.jsonl.1")
            self.assertTrue(rotated.exists())
            self.assertEqual(rotated.read_text(encoding="utf-8"), "x" * 100)
            # 新文件只含本次这一条
            records = [
                json.loads(line) for line in path.read_text(encoding="utf-8").strip().splitlines()
            ]
            self.assertEqual(len(records), 1)
            self.assertIsNone(records[0]["prev_hash"])


if __name__ == "__main__":
    unittest.main()
