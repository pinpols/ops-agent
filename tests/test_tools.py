"""read_logs 工具单测(真读文件,不打 API):功能 + 安全护栏。"""

import os
import unittest
from pathlib import Path

from ops_agent import tools


class ReadLogsTest(unittest.TestCase):
    def setUp(self):
        # 指向项目 data/(内有 sample-console.log)
        os.environ["OPS_LOG_DIR"] = str(Path(__file__).resolve().parent.parent / "data")

    def test_reads_matching_service(self):
        out = tools.read_logs("console")
        self.assertIn("read_logs", out)
        self.assertIn("BatchConsoleApiApplication", out)  # 样本里确有

    def test_pattern_filters_lines(self):
        out = tools.read_logs("console", pattern="WARN|ERROR")
        self.assertIn("WARN", out)
        self.assertNotIn("Started BatchConsoleApiApplication", out)  # INFO 行被过滤掉

    def test_max_lines_truncates(self):
        out = tools.read_logs("console", max_lines=2)
        # header + 至多 2 行
        body = out.split("\n", 1)[1] if "\n" in out else ""
        self.assertLessEqual(len([l for l in body.splitlines() if l]), 2)

    def test_rejects_path_traversal_service(self):
        self.assertIn("非法", tools.read_logs("../etc"))
        self.assertIn("非法", tools.read_logs("a/b"))

    def test_no_match_service(self):
        self.assertIn("未找到", tools.read_logs("nonexistent"))


if __name__ == "__main__":
    unittest.main()
