"""Run the distribution and startup tools against an isolated project fixture."""

import importlib
import json
import plistlib
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


class DistributionTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module("backend.distribution")
        except ImportError:
            self.module = None
        self.assertIsNotNone(self.module, "portable distribution tools not implemented")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_share_uses_allowlist_and_excludes_private_files(self):
        for name in [
            "README.md",
            "config.example.json",
            "backend/__init__.py",
            "config.local.json",
            "运行状态.json",
            "data/panel.db",
            "docs/截图.png",
            "backend/__pycache__/private.pyc",
        ]:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("private" if name.endswith(".db") else "sample")
        archive = self.root / "share.tar.gz"
        self.module.create_archive(self.root, archive)
        with tarfile.open(str(archive)) as bundle:
            names = set(bundle.getnames())
        self.assertIn("dingtalk-ears/README.md", names)
        self.assertIn("dingtalk-ears/backend/__init__.py", names)
        for suffix in ["config.local.json", "panel.db", "运行状态.json", ".png", ".pyc"]:
            self.assertFalse(any(name.endswith(suffix) for name in names), names)

    def test_share_never_follows_symlinks_to_private_content(self):
        (self.root / "README.md").symlink_to("/etc/hosts")
        with self.assertRaises(ValueError):
            self.module.create_archive(self.root, self.root / "share.tar.gz")

    def test_launch_agent_uses_passed_paths_and_preserves_runtime_environment(self):
        path = self.root / "config.local.json"
        path.write_text(
            json.dumps(
                {
                    "profile": "corp:user",
                    "self_id": "openid",
                    "display_name": "甲",
                    "groups": [{"id": "group", "name": "设计"}],
                }
            )
        )
        value = self.module.launch_agent(
            path,
            sys.executable,
            {
                "PATH": "/custom/bin:/usr/bin",
                "DWS_CONFIG_DIR": "/custom/dws",
                "TOKEN": "must-not-leak",
            },
        )
        decoded = plistlib.loads(plistlib.dumps(value))
        self.assertEqual(decoded["ProgramArguments"][0:2], ["/usr/bin/caffeinate", "-s"])
        self.assertIn(str(path.resolve()), decoded["ProgramArguments"])
        self.assertEqual(decoded["ProgramArguments"][3:5], ["-m", "backend"])
        self.assertEqual(decoded["EnvironmentVariables"]["DWS_CONFIG_DIR"], "/custom/dws")
        self.assertNotIn("TOKEN", decoded["EnvironmentVariables"])
        self.assertFalse(decoded["KeepAlive"])

    def test_cli_init_does_not_overwrite_and_bad_config_does_not_start(self):
        path = self.root / "config.local.json"
        command = [sys.executable, "-m", "backend"]
        first = subprocess.run(command + ["init", "--output", str(path)], stdout=subprocess.PIPE)
        self.assertEqual(first.returncode, 0)
        second = subprocess.run(command + ["init", "--output", str(path)], stderr=subprocess.PIPE)
        self.assertNotEqual(second.returncode, 0)
        result = subprocess.run(command + ["--config", str(path), "run"], stderr=subprocess.PIPE)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "data").exists())


class ServiceOutputTests(unittest.TestCase):
    def test_service_output_cannot_overwrite_existing_config_or_database(self):
        from backend.distribution import save_launch_agent

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ["config.local.json", "panel.db", "existing.plist"]:
                path = root / name
                path.write_text("keep this")
                with self.assertRaises((ValueError, FileExistsError)):
                    save_launch_agent(path, {"Label": "com.codex.dingtalk-ears"})
                self.assertEqual(path.read_text(), "keep this")
            fresh = root / "new.plist"
            save_launch_agent(fresh, {"Label": "com.codex.dingtalk-ears"})
            self.assertEqual(plistlib.loads(fresh.read_bytes())["Label"], "com.codex.dingtalk-ears")


if __name__ == "__main__":
    unittest.main()
