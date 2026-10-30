"""Read real subprocess outputs from a local CLI fixture, never real tokens."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from backend.config import load_config
from backend.diagnostics import doctor


class DiagnosticsTests(unittest.TestCase):
    def test_user_id_verifies_profile_without_inventing_missing_open_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = root / "cli"
            cli.write_text(
                "#!"
                + sys.executable
                + """
import json,sys
if sys.argv[1:3] == ['auth','status']:
 print(json.dumps({'authenticated':True,'user_id':'person','token_valid':True}))
elif sys.argv[1:4] == ['contact','user','get-self']:
 print(json.dumps({'result':[{'orgEmployeeModel':{'userId':'person'}}]}))
elif sys.argv[1:3] == ['login','status']:
 print('Logged in using ChatGPT')
else: sys.exit(2)
"""
            )
            os.chmod(str(cli), 0o700)
            path = root / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "profile": "corp:person",
                        "self_id": "known-open-id",
                        "display_name": "我",
                        "groups": [{"id": "cid", "name": "设计"}],
                        "commands": {"dws": str(cli), "codex": str(cli)},
                    }
                )
            )
            results = doctor(load_config(path))
            open_id = next(item for item in results if item["name"] == "本人 OpenID")
            self.assertEqual(open_id.get("level"), "warning")
            self.assertFalse(open_id["ok"])
            self.assertTrue(next(item for item in results if item["name"] == "钉钉本人")["ok"])
            self.assertNotIn("token_valid", json.dumps(results))

    def test_agy_diagnostics_checks_models_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = root / "cli"
            cli.write_text(
                "#!"
                + sys.executable
                + """
import json,sys
if sys.argv[1:3] == ['auth','status']:
 print(json.dumps({'authenticated':True,'user_id':'person','openDingTalkId':'known-open-id'}))
elif sys.argv[1:4] == ['contact','user','get-self']:
 print(json.dumps({'userId':'person','openDingTalkId':'known-open-id'}))
elif sys.argv[1:] == ['models']:
 print('gemini-3.8-flash-medium')
else: sys.exit(2)
"""
            )
            os.chmod(str(cli), 0o700)
            path = root / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "profile": "corp:person",
                        "self_id": "known-open-id",
                        "display_name": "我",
                        "groups": [{"id": "cid", "name": "设计"}],
                        "ai_provider": "agy",
                        "commands": {"dws": str(cli), "agy": str(cli)},
                    }
                )
            )
            results = doctor(load_config(path))
            agy_check = next(item for item in results if item["name"] == "agy 登录")
            self.assertTrue(agy_check["ok"])
            self.assertEqual(agy_check["detail"], "已连接可用模型")
