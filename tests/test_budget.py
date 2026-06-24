"""RunBudget 预算闸单测:墙钟 / token 越界抛 BudgetExceeded。"""

import unittest

from ops_agent.budget import BudgetExceeded, RunBudget


class BudgetTest(unittest.TestCase):
    def test_within_budget_no_raise(self):
        b = RunBudget(max_seconds=100.0, max_total_tokens=1000)
        b.check(500)  # 不抛

    def test_token_over_budget_raises(self):
        b = RunBudget(max_seconds=100.0, max_total_tokens=1000)
        with self.assertRaises(BudgetExceeded) as ctx:
            b.check(1001)
        self.assertIn("token", str(ctx.exception))

    def test_wallclock_over_budget_raises(self):
        b = RunBudget(max_seconds=0.0, max_total_tokens=None)
        # max_seconds=0 → 任何 elapsed>0 即超;monotonic 单调,_start 之后 check 必 >0
        import time

        time.sleep(0.001)
        with self.assertRaises(BudgetExceeded) as ctx:
            b.check(0)
        self.assertIn("墙钟", str(ctx.exception))

    def test_none_limits_never_raise(self):
        b = RunBudget(max_seconds=None, max_total_tokens=None)
        b.check(10**9)  # 两维度都不限


if __name__ == "__main__":
    unittest.main()
