"""read_logs 工具单测(真读文件,不打 API):功能 + 安全护栏。"""

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ops_agent import tools


class ReadLogsTest(unittest.TestCase):
    def setUp(self):
        # 指向项目 data/(内有 sample-console.log)
        os.environ["OPS_LOG_DIR"] = str(Path(__file__).resolve().parent.parent / "data")
        os.environ.pop("OPS_PROFILE", None)

    def test_reads_matching_service(self):
        out = tools.read_logs("console")
        self.assertIn("read_logs", out)
        self.assertIn("BatchConsoleApiApplication", out)  # 样本里确有

    def test_read_logs_result_is_structured(self):
        result = tools.read_logs_result("console", max_lines=1)
        self.assertTrue(result.ok)
        self.assertIn("read_logs", result.to_text())
        self.assertEqual(result.metadata["service"], "console")
        self.assertEqual(result.metadata["returned_lines"], 1)

    def test_pattern_filters_lines(self):
        out = tools.read_logs("console", pattern="WARN|ERROR")
        self.assertIn("WARN", out)
        self.assertNotIn("Started BatchConsoleApiApplication", out)  # INFO 行被过滤掉

    def test_max_lines_truncates(self):
        out = tools.read_logs("console", max_lines=2)
        # header + 至多 2 行
        body = out.split("\n", 1)[1] if "\n" in out else ""
        self.assertLessEqual(len([line for line in body.splitlines() if line]), 2)

    def test_rejects_path_traversal_service(self):
        self.assertIn("非法", tools.read_logs("../etc"))
        self.assertIn("非法", tools.read_logs("a/b"))
        self.assertIn("非法", tools.read_logs(123))

    def test_rejects_invalid_pattern(self):
        self.assertIn("正则非法", tools.read_logs("console", pattern="["))
        self.assertIn("pattern 必须", tools.read_logs("console", pattern=["WARN"]))

    def test_rejects_invalid_max_lines(self):
        self.assertIn("必须大于 0", tools.read_logs("console", max_lines=0))
        self.assertIn("必须大于 0", tools.read_logs("console", max_lines=-1))
        self.assertIn("必须是正整数", tools.read_logs("console", max_lines=True))

    def test_accepts_string_max_lines_from_model(self):
        out = tools.read_logs("console", max_lines="2")
        body = out.split("\n", 1)[1] if "\n" in out else ""
        self.assertLessEqual(len([line for line in body.splitlines() if line]), 2)

    def test_no_match_service(self):
        self.assertIn("未找到", tools.read_logs("nonexistent"))

    def test_prod_rejects_writable_log_dir(self):
        os.environ["OPS_PROFILE"] = "prod"
        result = tools.read_logs_result("console")
        self.assertFalse(result.ok)
        self.assertIn("只读挂载", result.to_text())


class QueryPgSafetyTest(unittest.TestCase):
    """query_pg 护栏:字符串闸在连库前就拦下危险 SQL(不需真 DB)。"""

    def setUp(self):
        os.environ.pop("OPS_PG_DSN", None)  # 确保停在"未配 DSN"而非真连库
        os.environ.pop("OPS_PROFILE", None)
        os.environ.pop("OPS_SQL_ALLOW_FREE", None)

    def test_rejects_non_select(self):
        self.assertIn("只允许", tools.query_pg("update t set x=1"))
        self.assertIn("SQL 必须", tools.query_pg(123))

    def test_rejects_multi_statement(self):
        # 含 ; 先被多语句闸拦(在关键词闸之前)
        self.assertIn("多语句", tools.query_pg("select 1; drop table t"))
        self.assertIn("多语句", tools.query_pg("select 1; select 2"))

    def test_rejects_forbidden_in_select(self):
        # 以 with 开头但夹带 delete
        self.assertIn(
            "被禁",
            tools.query_pg("with x as (delete from t returning *) select * from x"),
        )

    def test_rejects_sql_comments(self):
        # 注释用于拆词绕过关键词黑名单 / 行注释截句,一律拒绝(对抗审查 H3)
        for sql in (
            "select pg_/**/read_file('/etc/passwd')",
            "select 1 -- drop table t",
            "select 1 /* hidden */ from t",
            "select 1 # mysql-style",
        ):
            out = tools.query_pg(sql)
            self.assertIn("注释", out, sql)

    def test_select_passes_guard_then_needs_dsn(self):
        # 合法 SELECT 过了字符串闸 → 因没配 DSN 停下(证明 guard 放行了合法查询)
        self.assertIn("OPS_PG_DSN", tools.query_pg("select count(*) from batch.job_instance"))

    def test_query_pg_result_is_structured(self):
        result = tools.query_pg_result("select 1")
        self.assertFalse(result.ok)
        self.assertIn("OPS_PG_DSN", result.to_text())
        self.assertEqual(result.metadata["sql"], "select 1")

    def test_prod_profile_rejects_free_sql(self):
        os.environ["OPS_PROFILE"] = "prod"
        result = tools.query_pg_result("select 1")
        self.assertFalse(result.ok)
        self.assertIn("禁止自由 SQL", result.to_text())
        self.assertIn("pg_lock_waits", result.metadata["available_templates"])

    def test_query_pg_template_passes_template_guard_then_needs_dsn(self):
        os.environ["OPS_PROFILE"] = "prod"
        result = tools.query_pg_template_result("pg_lock_waits")
        self.assertFalse(result.ok)
        self.assertIn("OPS_PG_DSN", result.to_text())
        self.assertEqual(result.metadata["source"], "template:pg_lock_waits")

    def test_prod_rejects_privileged_db_user(self):
        os.environ["OPS_PROFILE"] = "prod"
        os.environ["OPS_PG_DSN"] = "postgresql://postgres:pass@localhost:5432/db"
        result = tools.query_pg_template_result("pg_lock_waits")
        self.assertFalse(result.ok)
        self.assertIn("最小权限", result.to_text())

    def test_rejects_invalid_max_rows_before_connecting(self):
        self.assertIn("必须大于 0", tools.query_pg("select 1", max_rows=0))
        self.assertIn("必须是正整数", tools.query_pg("select 1", max_rows=True))
        self.assertIn("OPS_PG_DSN", tools.query_pg("select 1", max_rows="2"))


class QueryPgConnectionGuardTest(unittest.TestCase):
    """连接级护栏 + 危险读函数黑名单(此前零直接覆盖)。"""

    def setUp(self):
        os.environ.pop("OPS_PROFILE", None)
        os.environ["OPS_SQL_ALLOW_FREE"] = "true"

    def tearDown(self):
        os.environ.pop("OPS_SQL_ALLOW_FREE", None)
        os.environ.pop("OPS_PG_DSN", None)

    def test_blacklist_rejects_dangerous_read_functions(self):
        # 字符串闸在连库前就拦下文件读 / 大对象 / 拖垮类函数
        for sql in (
            "select pg_read_file('/etc/passwd')",
            "select pg_ls_dir('/')",
            "select lo_import('/etc/passwd')",
            "select pg_sleep(100)",
        ):
            out = tools.query_pg(sql)
            self.assertIn("不允许", out, sql)

    @patch("psycopg.connect")
    def test_connection_uses_readonly_and_timeout(self, connect):
        os.environ["OPS_PG_DSN"] = "postgresql://reader:pw@localhost:5432/db"
        cur = MagicMock()
        cur.description = [SimpleNamespace(name="n")]
        cur.fetchmany.return_value = [(1,)]
        connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = cur

        result = tools.query_pg_result("select 1")

        self.assertTrue(result.ok)
        opts = connect.call_args.kwargs["options"]
        self.assertIn("default_transaction_read_only=on", opts)
        self.assertIn("statement_timeout=5000", opts)
        self.assertTrue(connect.call_args.kwargs["autocommit"])


if __name__ == "__main__":
    unittest.main()
