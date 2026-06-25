"""Trace persistence tests."""

import tempfile
import unittest
from pathlib import Path

from ops_agent.models import Diagnosis, Severity
from ops_agent.trace_io import write_agent_trace


class TraceIoTest(unittest.TestCase):
    def test_trace_filenames_do_not_collide_within_same_second(self):
        diagnosis = Diagnosis(
            severity=Severity.INFO,
            summary="ok",
            root_cause="无",
            evidence=[],
            suggested_action="无",
            confidence=0.9,
        )
        with tempfile.TemporaryDirectory() as tmp:
            first = write_agent_trace(
                Path(tmp), question="q", model="m", diagnosis=diagnosis, steps=[]
            )
            second = write_agent_trace(
                Path(tmp), question="q", model="m", diagnosis=diagnosis, steps=[]
            )

        self.assertNotEqual(first.name, second.name)

    def test_trace_redacts_secrets(self):
        diagnosis = Diagnosis(
            severity=Severity.WARNING,
            summary="token sk-ant-secret leaked",
            root_cause="dsn postgresql://user:pass@example/db",
            evidence=["email a@example.com phone 13900000000"],
            suggested_action="rotate token",
            confidence=0.7,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = write_agent_trace(
                Path(tmp), question="q", model="m", diagnosis=diagnosis, steps=[]
            )
            content = path.read_text(encoding="utf-8")

        self.assertNotIn("sk-ant-secret", content)
        self.assertNotIn("pass@example", content)
        self.assertNotIn("a@example.com", content)
        self.assertNotIn("13900000000", content)

    def test_trace_id_is_persisted(self):
        diagnosis = Diagnosis(
            severity=Severity.INFO,
            summary="ok",
            root_cause="无",
            evidence=[],
            suggested_action="无",
            confidence=0.9,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = write_agent_trace(
                Path(tmp),
                question="q",
                model="m",
                diagnosis=diagnosis,
                steps=[],
                trace_id="trace-file",
            )
            content = path.read_text(encoding="utf-8")

        self.assertIn('"trace_id": "trace-file"', content)


if __name__ == "__main__":
    unittest.main()
