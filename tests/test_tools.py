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


class QueryPgSafetyTest(unittest.TestCase):
    """query_pg 护栏:字符串闸在连库前就拦下危险 SQL(不需真 DB)。"""

    def setUp(self):
        os.environ.pop("OPS_PG_DSN", None)  # 确保停在"未配 DSN"而非真连库

    def test_rejects_non_select(self):
        self.assertIn("只允许", tools.query_pg("update t set x=1"))

    def test_rejects_multi_statement(self):
        # 含 ; 先被多语句闸拦(在关键词闸之前)
        self.assertIn("多语句", tools.query_pg("select 1; drop table t"))
        self.assertIn("多语句", tools.query_pg("select 1; select 2"))

    def test_rejects_forbidden_in_select(self):
        # 以 with 开头但夹带 delete
        self.assertIn("被禁", tools.query_pg("with x as (delete from t returning *) select * from x"))

    def test_select_passes_guard_then_needs_dsn(self):
        # 合法 SELECT 过了字符串闸 → 因没配 DSN 停下(证明 guard 放行了合法查询)
        self.assertIn("OPS_PG_DSN", tools.query_pg("select count(*) from batch.job_instance"))


if __name__ == "__main__":
    unittest.main()
