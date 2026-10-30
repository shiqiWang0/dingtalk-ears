"""Configuration failures must happen before any external service is started."""

import importlib
import json
import tempfile
import unittest
from pathlib import Path


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        try:
            self.module = importlib.import_module("backend.config")
        except ImportError:
            self.module = None
        self.assertIsNotNone(self.module, "configuration module is not implemented")
        self.path = self.root / "config.local.json"
        self.value = {
            "profile": "corp:employee",
            "self_id": "open-person",
            "display_name": "同事",
            "groups": [{"id": "cid-example", "name": "设计群"}],
        }

    def load(self):
        self.path.write_text(json.dumps(self.value), encoding="utf-8")
        return self.module.load_config(self.path)

    def test_relative_paths_follow_config_location_not_working_directory(self):
        config = self.load()
        self.assertEqual(config.state_dir, self.root / "data")
        self.assertEqual(config.output_dir, self.root / "data/output")
        self.assertEqual(config.port, 8766)

    def test_missing_identity_placeholder_and_unknown_fields_are_rejected(self):
        for change in [
            {"profile": ""},
            {"self_id": "填写本人ID"},
            {"token": "secret"},
            {"server": {"port": 8766, "host": "0.0.0.0"}},
        ]:
            with self.subTest(change=change):
                self.value.update(change)
                with self.assertRaises(ValueError):
                    self.load()
                self.value = {
                    "profile": "corp:employee",
                    "self_id": "open-person",
                    "display_name": "同事",
                    "groups": [{"id": "cid-example", "name": "设计群"}],
                }

    def test_invalid_numbers_and_repeated_json_keys_are_rejected(self):
        for port in [True, 0, 65536, "8766"]:
            self.value["server"] = {"port": port}
            with self.assertRaises(ValueError):
                self.load()
        self.value.pop("server")
        self.value["batch"] = {"trigger_count": 20, "max_messages": 10}
        with self.assertRaises(ValueError):
            self.load()
        self.path.write_text('{"profile":"a:b","profile":"x:y"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.module.load_config(self.path)

    def test_command_paths_follow_config_but_bare_commands_use_path(self):
        self.value["commands"] = {"dws": "./bin/dws", "codex": "codex", "agy": "./bin/agy"}
        config = self.load()
        self.assertEqual(config.dws, str(self.root / "bin/dws"))
        self.assertEqual(config.codex, "codex")
        self.assertEqual(config.agy, str(self.root / "bin/agy"))

    def test_ai_provider_and_model_configuration(self):
        # 默认 provider 和 model
        config = self.load()
        self.assertEqual(config.ai_provider, "codex")
        self.assertEqual(config.ai_model, "gemini-3.8-flash-medium")

        # 配置为 agy
        self.value["ai_provider"] = "agy"
        self.value["ai_model"] = "gemini-3.8-flash-medium"
        config_agy = self.load()
        self.assertEqual(config_agy.ai_provider, "agy")
        self.assertEqual(config_agy.ai_model, "gemini-3.8-flash-medium")

        # 非法 provider 抛出异常
        self.value["ai_provider"] = "unsupported"
        with self.assertRaises(ValueError):
            self.load()
        self.value["ai_provider"] = "codex"

    def test_extra_groups_are_validated_and_persisted(self):
        self.value["groups"] = [{"id": "cid-extra", "name": "研发群"}]
        config = self.load()
        self.assertEqual(config.watched_groups, (("cid-extra", "研发群"),))
        groups = self.module.add_group(self.path, "cid-new", "产品群")
        self.assertEqual([item["name"] for item in groups], ["研发群", "产品群"])
        self.module.remove_group(self.path, "cid-extra")
        self.assertEqual(self.module.configured_groups(self.path)[0]["id"], "cid-new")
        self.module.remove_group(self.path, "cid-new")
        self.assertEqual(self.module.configured_groups(self.path), [])

    def test_state_cannot_be_reused_by_another_account(self):
        first = self.load()
        self.module.bind_state(first)
        self.value["profile"] = "another:employee"
        with self.assertRaises(ValueError):
            self.module.bind_state(self.load())

    def test_legacy_databases_require_explicit_adoption(self):
        config = self.load()
        config.state_dir.mkdir()
        (config.state_dir / "queue.db").touch()
        with self.assertRaises(ValueError):
            self.module.bind_state(config)
        self.module.bind_state(config, adopt=True)
        self.module.bind_state(config)

    def test_config_example_is_created_once_without_overwriting(self):
        self.module.initialize(self.path)
        with self.assertRaises(FileExistsError):
            self.module.initialize(self.path)
        with self.assertRaises(ValueError):
            self.module.load_config(self.path)


if __name__ == "__main__":
    unittest.main()
