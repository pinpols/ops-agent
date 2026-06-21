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
        self.assertGreaterEqual(len(CASES), 8)


if __name__ == "__main__":
    unittest.main()
