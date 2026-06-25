"""System-aware tools tests."""

import os
import tempfile
import unittest
from pathlib import Path

from ops_agent import system_tools


class SystemToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["OPS_TARGET_ROOT"] = str(self.root)
        os.environ["OPS_LOG_DIR"] = str(self.root / "logs")
        (self.root / "logs").mkdir()
        (self.root / "batch-worker-import" / "src" / "main" / "resources").mkdir(parents=True)
        (self.root / "batch-worker-import" / "pom.xml").write_text("<project/>", encoding="utf-8")
        (self.root / "logs" / "worker-import.log").write_text(
            "INFO boot ok\nERROR import failed timeout after 1000ms\n", encoding="utf-8"
        )
        (self.root / "docker-compose.yml").write_text(
            "services:\n  postgres:\n    image: postgres:16\n    ports:\n      - '5432:5432'\n",
            encoding="utf-8",
        )
        (
            self.root / "batch-worker-import" / "src" / "main" / "resources" / "application.yml"
        ).write_text(
            "spring:\n  datasource:\n    url: jdbc:postgresql://localhost/batch\n",
            encoding="utf-8",
        )

    def tearDown(self):
        os.environ.pop("OPS_TARGET_ROOT", None)
        os.environ.pop("OPS_LOG_DIR", None)
        self.tmp.cleanup()

    def test_list_services_finds_modules_and_logs(self):
        result = system_tools.list_services_result()
        self.assertTrue(result.ok)
        self.assertIn("worker-import", result.to_text())
        self.assertIn("worker-import", result.metadata["services"])

    def test_tail_recent_errors_finds_error_lines(self):
        result = system_tools.tail_recent_errors_result()
        self.assertTrue(result.ok)
        self.assertIn("timeout", result.to_text())
        self.assertEqual(result.metadata["matched_lines"], 1)

    def test_tail_recent_errors_caps_scanned_files(self):
        for i in range(system_tools._LOG_FILE_CAP + 3):
            (self.root / "logs" / f"extra-{i}.log").write_text("ERROR capped\n", encoding="utf-8")
        result = system_tools.tail_recent_errors_result(max_lines=1000)
        self.assertTrue(result.ok)
        self.assertEqual(result.metadata["scanned_files"], system_tools._LOG_FILE_CAP)
        self.assertTrue(result.metadata["truncated"])

    def test_inspect_compose_summarizes_runtime_deps(self):
        result = system_tools.inspect_compose_result()
        self.assertTrue(result.ok)
        self.assertIn("postgres", result.to_text())
        self.assertEqual(len(result.metadata["files"]), 1)

    def test_read_app_config_for_service(self):
        result = system_tools.read_app_config_result("worker-import")
        self.assertTrue(result.ok)
        self.assertIn("datasource", result.to_text())
        self.assertEqual(result.metadata["service"], "worker-import")

    def test_read_app_config_rejects_path_traversal_service(self):
        # service 用户/模型可控:含 .. 或 / 直接拒,挡掉读 root 外任意文件(对抗审查 C2)
        for bad in ("../../etc", "..", "a/b", "foo bar", ""):
            result = system_tools.read_app_config_result(bad)
            self.assertFalse(result.ok, bad)
            self.assertIn("非法 service", result.to_text(), bad)

    def test_read_app_config_excludes_files_outside_root(self):
        # 纵深:即便 symlink 把配置指到 root 外,is_relative_to 守门也不收录
        outside = Path(self.tmp.name).parent / "outside-secret"
        outside.mkdir(exist_ok=True)
        try:
            (outside / "application.yml").write_text("secret: leaked\n", encoding="utf-8")
            link = self.root / "batch-worker-import" / "evil-link"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlink 不可用")
            result = system_tools.read_app_config_result("worker-import")
            self.assertNotIn("leaked", result.to_text())
        finally:
            import shutil

            shutil.rmtree(outside, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
