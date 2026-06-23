"""Diagnosis bundle tests."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ops_agent import agent
from ops_agent.bundle import create_bundle


def _tu(tid, name, inp):
    return SimpleNamespace(type="tool_use", id=tid, name=name, input=inp)


def _resp(*blocks):
    return SimpleNamespace(content=list(blocks), stop_reason="tool_use")


class BundleTest(unittest.TestCase):
    def setUp(self):
        os.environ["OPS_LOG_DIR"] = str(Path(__file__).resolve().parent.parent / "data")
        os.environ.pop("OPS_BUNDLE_DIR", None)

    def tearDown(self):
        os.environ.pop("OPS_BUNDLE_DIR", None)

    @patch("ops_agent.agent.make_client")
    def test_create_bundle_writes_expected_files(self, anthropic_cls):
        anthropic_cls.return_value.messages.create.side_effect = [
            _resp(_tu("a", "read_logs", {"service": "console", "max_lines": 1})),
            _resp(
                _tu(
                    "b",
                    agent.REPORT_TOOL_NAME,
                    {
                        "severity": "INFO",
                        "summary": "ok",
                        "root_cause": "无",
                        "evidence": [],
                        "suggested_action": "无",
                        "confidence": 0.9,
                    },
                )
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPS_BUNDLE_DIR"] = tmp
            bundle_dir = create_bundle("看日志")
            names = {path.name for path in bundle_dir.iterdir()}

        self.assertEqual(
            names,
            {"diagnosis.json", "trace.jsonl", "evidence.log", "summary.md"},
        )


if __name__ == "__main__":
    unittest.main()
