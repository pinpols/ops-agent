"""负载脚本纯函数单测:百分位计算 + 退出码语义(scripts/ 不在覆盖门禁内,但逻辑要对)。"""

import importlib.util
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "loadtest", Path(__file__).resolve().parent.parent / "scripts" / "loadtest.py"
)
loadtest = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(loadtest)


class PctTest(unittest.TestCase):
    def test_empty_is_zero(self):
        self.assertEqual(loadtest._pct([], 95), 0.0)

    def test_p50_and_p95(self):
        vals = [float(i) for i in range(1, 101)]  # 1..100
        self.assertEqual(loadtest._pct(vals, 50), 51.0)
        self.assertEqual(loadtest._pct(vals, 95), 96.0)
        self.assertEqual(loadtest._pct(vals, 100), 100.0)  # 不越界

    def test_single_value(self):
        self.assertEqual(loadtest._pct([3.0], 95), 3.0)


if __name__ == "__main__":
    unittest.main()
