"""Diagnosis schema 单测:needs_human_review 派生信号 + 关键不变量(不进 LLM 工具 schema)。"""

import unittest

from ops_agent.models import (
    CRITICAL_REVIEW_FLOOR,
    REVIEW_CONFIDENCE_FLOOR,
    Diagnosis,
    Severity,
)


def _diag(severity: Severity, confidence: float) -> Diagnosis:
    return Diagnosis(
        severity=severity,
        summary="s",
        root_cause="r",
        evidence=["e"],
        suggested_action="a",
        confidence=confidence,
    )


class NeedsHumanReviewTest(unittest.TestCase):
    def test_low_confidence_always_needs_review(self):
        self.assertTrue(_diag(Severity.INFO, REVIEW_CONFIDENCE_FLOOR - 0.01).needs_human_review)
        self.assertTrue(_diag(Severity.WARNING, 0.3).needs_human_review)

    def test_critical_needs_higher_confidence(self):
        # CRITICAL 误报代价高:把握不足(< CRITICAL_REVIEW_FLOOR)即便过了通用地板也要复核
        self.assertTrue(_diag(Severity.CRITICAL, CRITICAL_REVIEW_FLOOR - 0.01).needs_human_review)
        self.assertFalse(_diag(Severity.CRITICAL, CRITICAL_REVIEW_FLOOR).needs_human_review)

    def test_confident_non_critical_does_not_need_review(self):
        self.assertFalse(_diag(Severity.INFO, 0.95).needs_human_review)
        self.assertFalse(_diag(Severity.WARNING, 0.9).needs_human_review)

    def test_surfaced_in_model_dump(self):
        # 关键:派生字段进 model_dump → webhook(model_dump(mode=json))/bundle/history 自动带上
        dump = _diag(Severity.CRITICAL, 0.7).model_dump(mode="json")
        self.assertIn("needs_human_review", dump)
        self.assertTrue(dump["needs_human_review"])

    def test_not_in_validation_schema_so_model_cannot_fill_it(self):
        # 关键不变量:computed_field 不进 report_diagnosis 的 input_schema(validation mode),
        # 否则会让模型自评"要不要人看"(双重乐观)且可被注入篡改。防回归。
        props = Diagnosis.model_json_schema().get("properties", {})
        self.assertNotIn("needs_human_review", props)
        self.assertIn("confidence", props)


if __name__ == "__main__":
    unittest.main()
