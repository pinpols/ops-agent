"""阶段5(②)单测:执行工具护栏 + HITL 审批闸。"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ops_agent import agent, exec_tools
from ops_agent.models import Diagnosis


def _tu(tid, name, inp):
    return SimpleNamespace(type="tool_use", id=tid, name=name, input=inp)


def _resp(*blocks):
    return SimpleNamespace(content=list(blocks), stop_reason="tool_use")


_REPORT = {
    "severity": "WARNING",
    "summary": "x",
    "root_cause": "x",
    "evidence": [],
    "suggested_action": "x",
    "confidence": 0.5,
}


class ExecToolGuardTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("OPS_ALLOW_EXEC", None)
        os.environ.pop("OPS_RESTART_CMD", None)
        os.environ.pop("OPS_EXEC_ALLOWLIST", None)

    def test_rejects_non_whitelisted_service(self):
        self.assertIn("不在白名单", exec_tools.restart_service("rm-rf"))

    def test_dry_run_by_default(self):
        out = exec_tools.restart_service("orchestrator")
        self.assertIn("DRY-RUN", out)
        self.assertIn("未真执行", out)

    def test_restart_service_result_is_structured(self):
        result = exec_tools.restart_service_result("orchestrator")
        self.assertTrue(result.ok)
        self.assertIn("DRY-RUN", result.to_text())
        self.assertTrue(result.metadata["dry_run"])

    @patch("ops_agent.exec_tools.subprocess.run")
    def test_real_exec_uses_argv_not_shell(self, run):
        os.environ["OPS_ALLOW_EXEC"] = "true"
        os.environ["OPS_RESTART_CMD"] = "echo {service}"
        os.environ["OPS_EXEC_ALLOWLIST"] = "echo"
        run.return_value = SimpleNamespace(returncode=0, stdout="ok", stderr="")

        result = exec_tools.restart_service_result("orchestrator")

        self.assertTrue(result.ok)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["echo", "orchestrator"])
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(result.metadata["command_args"], ["echo", "orchestrator"])

    @patch("ops_agent.exec_tools.subprocess.run")
    def test_real_exec_requires_allowlist(self, run):
        os.environ["OPS_ALLOW_EXEC"] = "true"
        os.environ["OPS_RESTART_CMD"] = "echo {service}"

        result = exec_tools.restart_service_result("orchestrator")

        self.assertFalse(result.ok)
        self.assertIn("OPS_EXEC_ALLOWLIST", result.to_text())
        run.assert_not_called()


class HitlApprovalTest(unittest.TestCase):
    def setUp(self):
        os.environ["OPS_LOG_DIR"] = str(Path(__file__).resolve().parent.parent / "data")
        os.environ.pop("OPS_ALLOW_EXEC", None)
        os.environ.pop("OPS_RESTART_CMD", None)
        os.environ.pop("OPS_EXEC_ALLOWLIST", None)
        os.environ.pop("OPS_APPROVAL_LOG", None)

    def _two_step(self):
        # 步1:模型要重启;步2:给结论
        return [
            _resp(_tu("a", "restart_service", {"service": "orchestrator"})),
            _resp(_tu("b", agent.REPORT_TOOL_NAME, _REPORT)),
        ]

    @patch("ops_agent.agent.Anthropic")
    def test_deny_blocks_execution(self, anthropic_cls):
        anthropic_cls.return_value.messages.create.side_effect = self._two_step()
        seen = {}

        def deny(name, inp):
            seen["asked"] = (name, inp)
            return False

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPS_APPROVAL_LOG"] = str(Path(tmp) / "approvals.jsonl")
            d, messages = agent.run_agent("重启 orchestrator", approver=deny)
            approval_lines = (
                Path(os.environ["OPS_APPROVAL_LOG"]).read_text(encoding="utf-8").splitlines()
            )
            records = [json.loads(line) for line in approval_lines]
        self.assertIsInstance(d, Diagnosis)
        self.assertEqual(seen["asked"][0], "restart_service")  # 审批闸被问到
        self.assertIn("拒绝", str(messages))  # 拒绝信息喂回模型
        self.assertNotIn("DRY-RUN", str(messages))  # 未执行(连 dry-run 都没跑)
        self.assertFalse(records[0]["approved"])
        self.assertEqual(records[0]["tool_name"], "restart_service")

    @patch("ops_agent.agent.Anthropic")
    def test_approve_runs_dry_run(self, anthropic_cls):
        anthropic_cls.return_value.messages.create.side_effect = self._two_step()
        d, messages = agent.run_agent("重启 orchestrator", approver=lambda *a: True)
        self.assertIsInstance(d, Diagnosis)
        self.assertIn("DRY-RUN", str(messages))  # 批准 → 执行(默认 dry-run)


if __name__ == "__main__":
    unittest.main()
