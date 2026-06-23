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


if __name__ == "__main__":
    unittest.main()
