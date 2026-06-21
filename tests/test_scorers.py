"""eval 打分器单测:确定性打分(无 LLM)+ llm_judge(mock)。"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from evals import scorers
from evals.cases import Case
from ops_agent.models import Diagnosis, Severity


def _diag(severity, summary="", root_cause="", evidence=None):
    return Diagnosis(
        severity=severity,
        summary=summary,
        root_cause=root_cause,
        evidence=evidence or [],
        suggested_action="x",
        confidence=0.7,
    )


class DeterministicScoreTest(unittest.TestCase):
    def test_pass_when_severity_and_keywords_match(self):
        case = Case("redis", "log", Severity.WARNING, ["redis", "16379"])
        d = _diag(
            Severity.WARNING,
            root_cause="Redis 连接被拒",
            evidence=["refused localhost:16379"],
        )
        r = scorers.deterministic_score(d, case)
        self.assertTrue(r["passed"])
        self.assertEqual(r["keyword_recall"], 1.0)

    def test_fail_on_severity_mismatch(self):
        case = Case("redis", "log", Severity.WARNING, ["redis"])
        d = _diag(Severity.INFO, root_cause="redis 问题")
        r = scorers.deterministic_score(d, case)
        self.assertFalse(r["severity_ok"])
        self.assertFalse(r["passed"])

    def test_fail_on_missed_keyword(self):
        case = Case("x", "log", Severity.WARNING, ["redis", "16379"])
        d = _diag(Severity.WARNING, root_cause="redis 问题")  # 缺 16379
        r = scorers.deterministic_score(d, case)
        self.assertEqual(r["missed_keywords"], ["16379"])
        self.assertFalse(r["passed"])

    def test_normal_case_overalert_flagged(self):
        case = Case("ok", "log", Severity.INFO, [], is_normal=True)
        d = _diag(Severity.CRITICAL, summary="草木皆兵")
        r = scorers.deterministic_score(d, case)
        self.assertFalse(r["normal_ok"])  # 反例报 CRITICAL → 标红
        self.assertFalse(r["passed"])


class LlmJudgeTest(unittest.TestCase):
    @patch("anthropic.Anthropic")
    def test_parses_verdict(self, anthropic_cls):
        block = SimpleNamespace(
            type="tool_use",
            id="v",
            name="submit_verdict",
            input={"correct": True, "score": 0.85, "reasoning": "根因对"},
        )
        anthropic_cls.return_value.messages.create.return_value = SimpleNamespace(
            content=[block], stop_reason="tool_use"
        )
        case = Case("redis", "log...", Severity.WARNING, ["redis"])
        v = scorers.llm_judge(_diag(Severity.WARNING, root_cause="redis 挂了"), case)
        self.assertTrue(v.correct)
        self.assertEqual(v.score, 0.85)


if __name__ == "__main__":
    unittest.main()
