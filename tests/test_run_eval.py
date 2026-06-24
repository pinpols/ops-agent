"""run_eval 单测:聚合纯函数 + run/main 编排(mock 掉 LLM 边界,离线零成本)。"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evals import run_eval
from evals.cases import CASES
from evals.scorers import JudgeVerdict
from ops_agent.models import Diagnosis, Severity


def _fake_diagnosis() -> Diagnosis:
    """固定的诊断对象,供 mock diagnose_log 返回(内容真假不重要,只验编排)。"""
    return Diagnosis(
        severity=Severity.WARNING,
        summary="redis 连接被拒",
        root_cause="本地 16379 上 redis 未启动",
        evidence=["Connection refused: localhost/127.0.0.1:16379"],
        suggested_action="确认 valkey 容器在跑",
        confidence=0.8,
    )


class AggregateTest(unittest.TestCase):
    def test_aggregate_pass_rate_and_judge_avg(self):
        results = {
            "a": {"passed": True, "judge_score": 0.8},
            "b": {"passed": False, "judge_score": 0.4},
            "c": {"passed": True},  # 无 judge
        }
        agg = run_eval._aggregate(results)
        self.assertEqual(agg["passed"], 2)
        self.assertEqual(agg["total"], 3)
        self.assertAlmostEqual(agg["pass_rate"], 0.667, places=2)
        self.assertAlmostEqual(agg["judge_avg"], 0.6, places=2)

    def test_judge_avg_none_when_no_judge(self):
        agg = run_eval._aggregate({"a": {"passed": True}})
        self.assertIsNone(agg["judge_avg"])

    def test_aggregate_empty_results_no_zero_division(self):
        # 空集合不能 ZeroDivisionError(pass_rate 退化为 0.0)
        agg = run_eval._aggregate({})
        self.assertEqual(agg, {"pass_rate": 0.0, "passed": 0, "total": 0, "judge_avg": None})

    def test_aggregate_accepts_saved_envelope(self):
        payload = {
            "metadata": {"prompt_version": "test"},
            "results": {
                "a": {"passed": True, "judge_score": 1.0},
                "b": {"passed": False, "judge_score": 0.5},
            },
        }
        agg = run_eval._aggregate(payload)
        self.assertEqual(agg["passed"], 1)
        self.assertEqual(agg["total"], 2)
        self.assertAlmostEqual(agg["judge_avg"], 0.75, places=2)


class RunTest(unittest.TestCase):
    """run() 编排:对每条 case 调 diagnose_log + 确定性打分,judge 开关控制是否加评委字段。"""

    @patch("evals.run_eval.diagnose_log")
    def test_run_without_judge_builds_entry_per_case(self, mock_diag):
        mock_diag.return_value = _fake_diagnosis()

        results = run_eval.run(use_judge=False)

        self.assertEqual(len(results), len(CASES))
        self.assertEqual(mock_diag.call_count, len(CASES))
        for entry in results.values():
            self.assertIn("passed", entry)
            self.assertIn("severity", entry)
            self.assertIn("keyword_recall", entry)
            self.assertIn("missed_keywords", entry)
            self.assertIn("normal_ok", entry)
            self.assertNotIn("judge_score", entry)  # judge=off 不加评委字段
        # diagnose_log 返回 WARNING:redis_down 期望 WARNING + 关键词命中 → 该条应 PASS
        self.assertTrue(results["redis_down"]["passed"])

    @patch("evals.run_eval.llm_judge")
    @patch("evals.run_eval.diagnose_log")
    def test_run_with_judge_adds_verdict_fields(self, mock_diag, mock_judge):
        mock_diag.return_value = _fake_diagnosis()
        mock_judge.return_value = JudgeVerdict(correct=True, score=0.9, reasoning="根因对")

        results = run_eval.run(use_judge=True)

        self.assertEqual(mock_judge.call_count, len(CASES))
        for entry in results.values():
            self.assertEqual(entry["judge_score"], 0.9)
            self.assertTrue(entry["judge_correct"])
            self.assertEqual(entry["judge_reasoning"], "根因对")


class MainTest(unittest.TestCase):
    """main() CLI 编排:--save 落盘 + --baseline 回归对比。run() 整段 mock,不打 LLM。"""

    _STUB_RESULTS = {
        "a": {
            "passed": True,
            "severity": "WARNING",
            "keyword_recall": 1.0,
            "missed_keywords": [],
            "normal_ok": True,
        },
        "b": {
            "passed": False,
            "severity": "INFO",
            "keyword_recall": 0.0,
            "missed_keywords": ["x"],
            "normal_ok": True,
        },
    }

    @patch("evals.run_eval._git_sha")
    @patch("evals.run_eval.run")
    def test_main_save_writes_baseline_json(self, mock_run, mock_git_sha):
        mock_run.return_value = dict(self._STUB_RESULTS)
        mock_git_sha.return_value = "abc1234"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "base.json"
            argv = ["run_eval", "--save", str(out)]
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                run_eval.main()
            saved = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(saved["results"], self._STUB_RESULTS)
        self.assertEqual(saved["metadata"]["git_sha"], "abc1234")
        self.assertEqual(saved["metadata"]["case_count"], 2)
        self.assertFalse(saved["metadata"]["judge_enabled"])
        mock_run.assert_called_once_with(False)  # 未传 --judge → run(False)

    @patch("evals.run_eval.run")
    def test_main_baseline_flags_regression(self, mock_run):
        # 基线里 a=PASS;本次 a 变 FAIL → 应打印回归告警
        baseline = {"a": {"passed": True}, "b": {"passed": False}}
        regressed = dict(self._STUB_RESULTS)
        regressed["a"] = {**regressed["a"], "passed": False}
        mock_run.return_value = regressed
        with tempfile.TemporaryDirectory() as tmp:
            base_file = Path(tmp) / "base.json"
            base_file.write_text(json.dumps(baseline), encoding="utf-8")
            argv = ["run_eval", "--baseline", str(base_file)]
            buf = io.StringIO()
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(buf):
                run_eval.main()
            output = buf.getvalue()
        self.assertIn("a", output)
        self.assertIn("PASS→FAIL", output)

    @patch("evals.run_eval.run")
    def test_main_baseline_accepts_saved_envelope(self, mock_run):
        baseline = {"metadata": {"prompt_version": "x"}, "results": {"a": {"passed": True}}}
        regressed = {"a": {**self._STUB_RESULTS["a"], "passed": False}}
        mock_run.return_value = regressed
        with tempfile.TemporaryDirectory() as tmp:
            base_file = Path(tmp) / "base.json"
            base_file.write_text(json.dumps(baseline), encoding="utf-8")
            argv = ["run_eval", "--baseline", str(base_file)]
            buf = io.StringIO()
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(buf):
                run_eval.main()
            output = buf.getvalue()
        self.assertIn("PASS→FAIL", output)


if __name__ == "__main__":
    unittest.main()
