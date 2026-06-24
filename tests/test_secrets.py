"""敏感值 *_FILE 解析 + config 集成单测(docker/k8s secret 注入模式)。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ops_agent.config import Settings, _env_or_file


class EnvOrFileTest(unittest.TestCase):
    def test_direct_env_wins(self):
        with patch.dict("os.environ", {"FOO": "direct"}, clear=False):
            self.assertEqual(_env_or_file("FOO"), "direct")

    def test_reads_from_file_when_env_absent(self):
        import os

        with TemporaryDirectory() as tmp:
            secret = Path(tmp) / "key"
            secret.write_text("file-secret\n", encoding="utf-8")  # 尾换行应被去掉
            os.environ.pop("FOO", None)  # 确保没有直连 FOO
            with patch.dict("os.environ", {"FOO_FILE": str(secret)}, clear=False):
                self.assertEqual(_env_or_file("FOO"), "file-secret")

    def test_none_when_neither(self):
        import os

        os.environ.pop("FOO", None)
        os.environ.pop("FOO_FILE", None)
        self.assertIsNone(_env_or_file("FOO"))

    def test_settings_reads_api_key_from_file(self):
        with TemporaryDirectory() as tmp:
            secret = Path(tmp) / "anthropic"
            secret.write_text("sk-from-file", encoding="utf-8")
            env = {"ANTHROPIC_API_KEY_FILE": str(secret), "OPS_WEBHOOK_TOKEN": "tok"}
            with patch.dict("os.environ", env, clear=False):
                import os

                os.environ.pop("ANTHROPIC_API_KEY", None)
                s = Settings.from_env()
            self.assertEqual(s.anthropic_api_key, "sk-from-file")
            self.assertEqual(s.ops_webhook_token, "tok")


if __name__ == "__main__":
    unittest.main()
