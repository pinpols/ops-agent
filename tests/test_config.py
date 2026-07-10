"""单测:Settings 环境解析 + profile 校验。

profile 是所有安全闸(prod exec fail-closed、自由 SQL 禁用)的输入面:
旧实现把任何非 "prod" 字符串都当 dev,于是 OPS_PROFILE=production 这种常见笔误会
静默 fail-open(自由 SQL 开、prod exec 闸被跳过)。这里把它钉成 fail-fast。
"""

import os
import unittest
from unittest.mock import patch

from ops_agent.config import Settings, _env_bool


class EnvBoolTest(unittest.TestCase):
    def test_truthy_values(self):
        for v in ("1", "true", "TRUE", "yes", "y", "on"):
            with patch.dict(os.environ, {"X": v}, clear=True):
                self.assertTrue(_env_bool("X"), v)

    def test_falsy_values(self):
        # 关键:字符串 "false"/"0"/"no" 必须解析成 False(防 truthy-"false" 闸失效)
        for v in ("0", "false", "False", "no", "off", "", "nope"):
            with patch.dict(os.environ, {"X": v}, clear=True):
                self.assertFalse(_env_bool("X"), v)

    def test_unset_uses_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_env_bool("X"))
            self.assertTrue(_env_bool("X", default=True))


class ProfileValidationTest(unittest.TestCase):
    def test_prod_is_production_and_blocks_free_sql(self):
        with patch.dict(os.environ, {"OPS_PROFILE": "prod"}, clear=True):
            s = Settings.from_env()
            self.assertTrue(s.production)
            self.assertFalse(s.ops_sql_allow_free)

    def test_dev_allows_free_sql(self):
        with patch.dict(os.environ, {"OPS_PROFILE": "dev"}, clear=True):
            s = Settings.from_env()
            self.assertFalse(s.production)
            self.assertTrue(s.ops_sql_allow_free)

    def test_default_profile_is_dev(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(Settings.from_env().production)

    def test_staging_is_allowed_and_not_production(self):
        with patch.dict(os.environ, {"OPS_PROFILE": "staging"}, clear=True):
            self.assertFalse(Settings.from_env().production)

    def test_whitespace_and_case_normalized(self):
        # "  PROD " 归一化后是合法的 prod,不应被拒
        with patch.dict(os.environ, {"OPS_PROFILE": "  PROD "}, clear=True):
            self.assertTrue(Settings.from_env().production)

    def test_unknown_profile_rejected_fail_fast(self):
        # 'production' 是 'prod' 最常见的笔误 —— 旧实现静默当 dev,这里必须报错
        for bad in ("production", "prd", "qa", "develop"):
            with (
                patch.dict(os.environ, {"OPS_PROFILE": bad}, clear=True),
                self.assertRaises(ValueError, msg=bad),
            ):
                Settings.from_env()

    def test_redaction_rules_file_is_resolved(self):
        with patch.dict(os.environ, {"OPS_REDACTION_RULES_FILE": "rules.json"}, clear=True):
            self.assertEqual(Settings.from_env().ops_redaction_rules_file.name, "rules.json")

    def test_prod_forces_redact_even_if_disabled(self):
        # prod 下显式关脱敏也必须 fail-closed 为 True,防 artifact 明文外泄(对抗审查 M2)
        with patch.dict(
            os.environ,
            {"OPS_PROFILE": "prod", "OPS_REDACT_ARTIFACTS": "false"},
            clear=True,
        ):
            self.assertTrue(Settings.from_env().ops_redact_artifacts)

    def test_dev_respects_redact_disabled(self):
        # 非 prod 仍尊重显式关闭(只在 prod 强制)
        with patch.dict(
            os.environ,
            {"OPS_PROFILE": "dev", "OPS_REDACT_ARTIFACTS": "false"},
            clear=True,
        ):
            self.assertFalse(Settings.from_env().ops_redact_artifacts)


class WorkerDeadAfterDerivationTest(unittest.TestCase):
    """P0-1②:dead_after 默认必须覆盖"handler 忙跑 max_run + LLM 超时"的合法最坏窗口,
    否则忙 worker 会被对面 reaper 判死、在途任务被抢走造成双执行。"""

    def test_dead_after_default_derives_from_run_and_llm_budget(self):
        with patch.dict(
            os.environ,
            {"OPS_MAX_RUN_SECONDS": "120", "OPS_LLM_TIMEOUT_SECONDS": "90"},
            clear=True,
        ):
            s = Settings.from_env()
            self.assertEqual(s.ops_worker_dead_after_seconds, 120 + 90 + 60)

    def test_dead_after_default_without_llm_override(self):
        # 未配 LLM 超时 → llm_timeout 派生为 max_run,dead_after = 2×max_run + 60
        with patch.dict(os.environ, {"OPS_MAX_RUN_SECONDS": "100"}, clear=True):
            self.assertEqual(Settings.from_env().ops_worker_dead_after_seconds, 100 + 100 + 60)

    def test_dead_after_explicit_env_wins_but_warns_when_below_max_run(self):
        with (
            patch.dict(
                os.environ,
                {"OPS_MAX_RUN_SECONDS": "120", "OPS_WORKER_DEAD_AFTER_SECONDS": "60"},
                clear=True,
            ),
            self.assertLogs("ops_agent.config", level="WARNING") as logs,
        ):
            s = Settings.from_env()
        self.assertEqual(s.ops_worker_dead_after_seconds, 60)  # 显式值仍生效(只警告不拒绝)
        self.assertTrue(any("OPS_WORKER_DEAD_AFTER_SECONDS" in m for m in logs.output))

    def test_dead_after_explicit_env_above_max_run_no_warning(self):
        import logging

        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append  # type: ignore[method-assign]
        logging.getLogger("ops_agent.config").addHandler(handler)
        try:
            with patch.dict(
                os.environ,
                {"OPS_MAX_RUN_SECONDS": "120", "OPS_WORKER_DEAD_AFTER_SECONDS": "600"},
                clear=True,
            ):
                s = Settings.from_env()
        finally:
            logging.getLogger("ops_agent.config").removeHandler(handler)
        self.assertEqual(s.ops_worker_dead_after_seconds, 600)
        self.assertFalse(any("OPS_WORKER_DEAD_AFTER_SECONDS" in r.getMessage() for r in records))


class StaleRunningDerivationTest(unittest.TestCase):
    """P2-5②:stale_running 默认 2×max_run 可能小于合法最坏(max_run+llm_timeout+工具),
    默认派生改 max_run + llm_timeout + 120。"""

    def test_stale_running_default_derives_from_run_and_llm_budget(self):
        with patch.dict(
            os.environ,
            {"OPS_MAX_RUN_SECONDS": "120", "OPS_LLM_TIMEOUT_SECONDS": "300"},
            clear=True,
        ):
            self.assertEqual(Settings.from_env().ops_stale_running_seconds, 120 + 300 + 120)

    def test_stale_running_explicit_env_wins(self):
        with patch.dict(
            os.environ,
            {"OPS_MAX_RUN_SECONDS": "120", "OPS_STALE_RUNNING_SECONDS": "999"},
            clear=True,
        ):
            self.assertEqual(Settings.from_env().ops_stale_running_seconds, 999)


if __name__ == "__main__":
    unittest.main()
