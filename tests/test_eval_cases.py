"""Golden eval case hygiene tests."""

import unittest

from evals.cases import CASES
from ops_agent.models import Severity


class EvalCasesTest(unittest.TestCase):
    def test_case_ids_are_unique(self):
        ids = [case.id for case in CASES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_cases_cover_normal_warning_and_critical(self):
        severities = {case.expected_severity for case in CASES}
        self.assertIn(Severity.INFO, severities)
        self.assertIn(Severity.WARNING, severities)
        self.assertIn(Severity.CRITICAL, severities)
        self.assertGreaterEqual(len(CASES), 50)  # golden set 已扩到 50+

    def test_enough_normal_cases_to_guard_false_alarms(self):
        # 反例(正常日志)要够多,才能真正检验"不草木皆兵"维度
        normals = [c for c in CASES if c.is_normal]
        self.assertGreaterEqual(len(normals), 8)
        # 正常样本不应配 CRITICAL 期望(自相矛盾)
        for c in normals:
            self.assertNotEqual(c.expected_severity, Severity.CRITICAL, c.id)

    def test_non_normal_cases_have_keywords(self):
        # 异常样本必须有期望关键词,否则确定性召回形同虚设
        for c in CASES:
            if not c.is_normal:
                self.assertTrue(c.expected_keywords, c.id)


if __name__ == "__main__":
    unittest.main()
