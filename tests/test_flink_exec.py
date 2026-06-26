"""Flink 写工具(危险):jobid 校验 + 默认 dry-run + prod 双开关 + 审批闸(HITL)拦在执行前。"""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ops_agent import flink_exec_tools as fx
from ops_agent.flink_exec_tools import flink_cancel_job_result, flink_trigger_savepoint_result

_JID = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"


class GuardTest(unittest.TestCase):
    def setUp(self):
        for k in ("OPS_ALLOW_EXEC", "OPS_PROD_ALLOW_EXEC", "OPS_PROFILE", "OPS_FLINK_URL"):
            os.environ.pop(k, None)

    def test_rejects_bad_jobid(self):
        r = flink_cancel_job_result("../etc")
        self.assertFalse(r.ok)
        self.assertIn("jobid 非法", r.to_text())

    def test_dry_run_by_default_no_network(self):
        with (
            patch.dict(os.environ, {"OPS_FLINK_URL": "http://flink:8081"}),
            patch("ops_agent.flink_exec_tools.urllib.request.urlopen") as urlopen,
        ):
            r = flink_cancel_job_result(_JID)
        urlopen.assert_not_called()  # dry-run 不出网
        self.assertIn("DRY-RUN", r.to_text())
        self.assertIn("未真执行", r.to_text())
        self.assertTrue(r.metadata["dry_run"])

    def test_prod_needs_double_gate(self):
        with patch.dict(
            os.environ,
            {"OPS_PROFILE": "prod", "OPS_ALLOW_EXEC": "true", "OPS_FLINK_URL": "https://flink"},
        ):
            r = flink_trigger_savepoint_result(_JID)
        self.assertFalse(r.ok)
        self.assertIn("OPS_PROD_ALLOW_EXEC", r.to_text())

    def test_approved_and_allowed_does_real_patch(self):
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = b'{"status":"ok"}'
        with (
            patch.dict(
                os.environ, {"OPS_ALLOW_EXEC": "true", "OPS_FLINK_URL": "http://flink:8081"}
            ),
            patch("ops_agent.flink_exec_tools.urllib.request.urlopen", return_value=cm),
            patch("ops_agent.flink_exec_tools.urllib.request.Request") as Request,
        ):
            r = flink_cancel_job_result(_JID)
        self.assertTrue(r.ok)
        self.assertTrue(r.metadata["executed"])
        self.assertEqual(
            Request.call_args.kwargs.get("method"), "PATCH"
        )  # cancel = PATCH ?mode=cancel
        self.assertIn(f"/jobs/{_JID}?mode=cancel", Request.call_args.args[0])


class HitlGateTest(unittest.TestCase):
    """写工具登记为 DANGEROUS → webhook 的 deny-all 审批闸在执行前拦住(结构性,不依赖模型)。"""

    def test_webhook_deny_all_blocks_flink_writes(self):
        from ops_agent import agent, server
        from ops_agent.tool_registry import DANGEROUS

        self.assertIn("flink_cancel_job", DANGEROUS)
        self.assertIn("flink_trigger_savepoint", DANGEROUS)
        for name in ("flink_cancel_job", "flink_trigger_savepoint"):
            self.assertFalse(server._deny_all_approver(name, {"jobid": _JID}))

        # 模型被劫持去调 flink_cancel_job + deny-all 审批 → impl 永不运行
        def _resp(*b):
            return SimpleNamespace(content=list(b), stop_reason="tool_use")

        def _tu(tid, nm, inp):
            return SimpleNamespace(type="tool_use", id=tid, name=nm, input=inp)

        report = {
            "severity": "WARNING",
            "summary": "s",
            "root_cause": "r",
            "evidence": [],
            "suggested_action": "a",
            "confidence": 0.5,
        }
        spy = MagicMock(return_value=fx.ToolResult.success("should-not-run"))
        with (
            patch("ops_agent.agent.make_client") as mc,
            patch.dict(agent._ALL_IMPLS, {"flink_cancel_job": spy}),
        ):
            mc.return_value.messages.create.side_effect = [
                _resp(_tu("evil", "flink_cancel_job", {"jobid": _JID})),
                _resp(_tu("rep", agent.REPORT_TOOL_NAME, report)),
            ]
            agent.run_agent("诊断", approver=server._deny_all_approver)
        spy.assert_not_called()  # 审批闸拦在执行前


if __name__ == "__main__":
    unittest.main()
