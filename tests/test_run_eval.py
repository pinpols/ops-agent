"""run_eval 聚合逻辑单测(纯函数,无 LLM)。"""

import unittest

from evals import run_eval


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


if __name__ == "__main__":
    unittest.main()
