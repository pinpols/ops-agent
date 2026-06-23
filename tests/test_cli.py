"""单测:CLI 退出码语义。

doctor 要能当部署前置闸(`ops-agent doctor && deploy`),prod 未就绪必须非零退出;
main 顶层把常见错误类映射到稳定退出码,方便脚本分支。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ops_agent.cli import main


class DoctorExitCodeTest(unittest.TestCase):
    def test_prod_not_ready_exits_nonzero(self):
        # prod + 日志目录可写(非只读)+ 无最小权限 DB 用户 → 未就绪
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"OPS_PROFILE": "prod", "OPS_LOG_DIR": tmp}, clear=True),
            self.assertRaises(SystemExit) as ctx,
        ):
            main(["doctor"])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_dev_profile_does_not_fail(self):
        # dev 不强制 prod_ready,doctor 正常返回(不抛 SystemExit)
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"OPS_PROFILE": "dev", "OPS_LOG_DIR": tmp}, clear=True),
        ):
            main(["doctor"])


class MainErrorTaxonomyTest(unittest.TestCase):
    def test_missing_log_file_exits_code_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"OPS_PROFILE": "dev", "OPS_LOG_DIR": tmp, "ANTHROPIC_API_KEY": "x"}
            missing = str(Path(tmp) / "nope.log")
            with (
                patch.dict(os.environ, env, clear=True),
                self.assertRaises(SystemExit) as ctx,
            ):
                main(["diagnose", missing])
        self.assertEqual(ctx.exception.code, 3)  # 缺输入文件 = 稳定退出码 3


class VerboseFlagTest(unittest.TestCase):
    def test_top_level_verbose_flag_accepted(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"OPS_PROFILE": "dev", "OPS_LOG_DIR": tmp}, clear=True),
        ):
            main(["-v", "doctor"])  # 顶层 -v 被接受且 doctor 正常跑


if __name__ == "__main__":
    unittest.main()
