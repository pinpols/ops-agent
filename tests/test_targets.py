"""多目标注册表单测:TOML 加载、按名解析、未知名报错、无注册表回退默认。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ops_agent import targets


class TargetsTest(unittest.TestCase):
    def _write_registry(self, tmp: str) -> str:
        path = Path(tmp) / "targets.toml"
        path.write_text(
            "[targets.fbs]\n"
            'root = "/srv/fbs"\n'
            'log_dir = "/var/log/fbs"\n'
            'pg_dsn = "postgresql://ro@h/db"\n'
            'metrics_url = "http://prom:9090"\n',
            encoding="utf-8",
        )
        return str(path)

    def test_load_and_resolve_named(self):
        with TemporaryDirectory() as tmp:
            reg = self._write_registry(tmp)
            with patch.dict("os.environ", {"OPS_TARGETS_FILE": reg}, clear=False):
                t = targets.resolve_target("fbs")
        self.assertEqual(t.name, "fbs")
        # 不比绝对路径(macOS /var→/private/var 符号链),只验末段 + 指标地址
        self.assertEqual(t.log_dir.name, "fbs")
        self.assertEqual(t.metrics_url, "http://prom:9090")
        self.assertEqual(t.pg_dsn, "postgresql://ro@h/db")

    def test_unknown_name_raises_when_registry_present(self):
        with TemporaryDirectory() as tmp:
            reg = self._write_registry(tmp)
            with (
                patch.dict("os.environ", {"OPS_TARGETS_FILE": reg}, clear=False),
                self.assertRaises(ValueError) as ctx,
            ):
                targets.resolve_target("nope")
        self.assertIn("nope", str(ctx.exception))

    def test_non_http_metrics_url_rejected_at_load(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.toml"
            path.write_text(
                '[targets.x]\nlog_dir = "/var/log/x"\nmetrics_url = "file:///etc/passwd"\n',
                encoding="utf-8",
            )
            with (
                patch.dict("os.environ", {"OPS_TARGETS_FILE": str(path)}, clear=False),
                self.assertRaises(ValueError) as ctx,
            ):
                targets.load_targets()
        self.assertIn("http", str(ctx.exception))

    def test_missing_log_dir_in_registry_raises(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.toml"
            path.write_text('[targets.bad]\nroot = "/x"\n', encoding="utf-8")
            with (
                patch.dict("os.environ", {"OPS_TARGETS_FILE": str(path)}, clear=False),
                self.assertRaises(ValueError),
            ):
                targets.load_targets()

    def test_no_registry_falls_back_to_default(self):
        # 指向不存在的注册表 → load_targets() 空 → resolve(None) 走 Settings 默认
        with patch.dict(
            "os.environ", {"OPS_TARGETS_FILE": "/nonexistent/targets.toml"}, clear=False
        ):
            self.assertEqual(targets.load_targets(), {})
            t = targets.resolve_target(None)
        self.assertEqual(t.name, "default")
        self.assertIsInstance(t.log_dir, Path)

    def test_named_with_empty_registry_falls_back(self):
        with patch.dict(
            "os.environ", {"OPS_TARGETS_FILE": "/nonexistent/targets.toml"}, clear=False
        ):
            t = targets.resolve_target("whatever")  # 空注册表容错回退默认
        self.assertEqual(t.name, "default")


if __name__ == "__main__":
    unittest.main()
