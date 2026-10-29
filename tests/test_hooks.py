"""Outbound hooks and signed write-back, against real loopback HTTP and mocked dws."""

import hashlib
import hmac
import http.client
import json
import os
import queue
import re
import stat
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.config import load_config
from backend.hooks import HookSender, build_payload, validate_hooks
from backend.panel_store import PanelStore
from backend.web import start_panel

SECRET = "fixture-secret-0123456789"


class Receiver:
    """Tiny webhook receiver that records requests and answers with a chosen status."""

    def __init__(self, status=204):
        self.requests = queue.Queue()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                owner.requests.put((dict(self.headers), body))
                self.send_response(status)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:{}/hook".format(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def closed_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    server.server_close()
    return "http://127.0.0.1:{}/hook".format(port)


def expected_signature(timestamp, body):
    mac = hmac.new(SECRET.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def message(key="cid-1:m1", sources=("direct",)):
    return {
        "key": key,
        "message_id": key.split(":")[1],
        "conversation_id": key.split(":")[0],
        "sender": "测试同事",
        "sender_id": "other-person",
        "time": "1730000000000",
        "text": "合成消息，请查收",
        "sources": list(sources),
    }


def enabled(url, **changes):
    value = {"enabled": True, "url": url, "secret": SECRET}
    value.update(changes)
    return value


class HookTests(unittest.TestCase):
    def test_hooks_default_off_and_enabled_hooks_need_target_and_secret(self):
        self.assertFalse(validate_hooks(None)["enabled"])
        self.assertEqual(validate_hooks({})["filter"]["sources"], ["direct", "at"])
        for value in [
            {"enabled": True},
            {"enabled": True, "url": "ftp://x", "secret": SECRET},
            {"enabled": True, "url": "http://x", "secret": "short"},
            {"enabled": "yes"},
            {"token": "x"},
            {"filter": {"sources": ["everything"]}},
            {"retries": 9},
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_hooks(value)

    def test_config_loads_hooks_section_and_example_stays_disabled(self):
        root = Path(__file__).resolve().parent.parent
        example = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
        self.assertFalse(example["hooks"]["enabled"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "profile": "corp:employee",
                        "self_id": "open-person",
                        "display_name": "同事",
                        "group": {"id": "cid-example", "name": "设计群"},
                        "hooks": enabled("https://example.com/hook"),
                    }
                )
            )
            config = load_config(path)
            self.assertTrue(config.hooks["enabled"])
            self.assertEqual(config.hooks["retries"], 2)
            self.assertEqual(config.config_path, path.resolve())

    def test_triaged_message_is_posted_with_valid_signature(self):
        receiver = Receiver()
        try:
            sender = HookSender(enabled(receiver.url))
            draft = {"id": "d1", "version": 1, "summary": "同事请你查收", "text": "收到"}
            sender.notify([message()], draft, True).join(5)
            headers, body = receiver.requests.get(timeout=2)
            headers = {key.lower(): value for key, value in headers.items()}
            self.assertEqual(
                headers["x-ears-signature"], expected_signature(headers["x-ears-timestamp"], body)
            )
            payload = json.loads(body)
            self.assertEqual(payload["event"], "message.triaged")
            self.assertEqual(headers["x-ears-delivery"], payload["delivery_id"])
            self.assertEqual(payload["message"]["id"], "cid-1:m1")
            self.assertEqual(payload["message"]["sender"], "测试同事")
            self.assertEqual(payload["message"]["conversation_id"], "cid-1")
            self.assertEqual(payload["message"]["text"], "合成消息，请查收")
            self.assertEqual(payload["classification"]["kind"], "direct")
            self.assertTrue(payload["classification"]["needs_reply"])
            self.assertEqual(payload["suggested_reply"], "收到")
            self.assertEqual(payload["actions"]["reply"], "/messages/cid-1%3Am1/reply")
            self.assertTrue(sender.public()["last"]["ok"])
            self.assertNotIn("secret", sender.public())
        finally:
            receiver.close()

    def test_filter_skips_unselected_sources_and_messages_without_reply(self):
        sender = HookSender(enabled("http://127.0.0.1:9/hook"))
        draft = {"id": "d1", "version": 1, "summary": "s", "text": ""}
        self.assertIsNone(sender.notify([message(sources=["ued"])], draft, True))
        sender = HookSender(
            enabled(
                "http://127.0.0.1:9/hook", filter={"sources": ["ued"], "needs_reply_only": True}
            )
        )
        self.assertIsNone(sender.notify([message(sources=["ued"])], draft, False))
        self.assertIsNone(sender.notify([message()], draft, True))
        self.assertIsNone(HookSender().notify([message()], draft, True))

    def test_unreachable_target_retries_logs_and_never_raises(self):
        sleeps = []
        sender = HookSender(enabled(closed_url(), timeout_seconds=1), sleep=sleeps.append)
        payload = build_payload([message()], {"summary": "", "text": ""}, False)
        with patch("sys.stderr") as stderr:
            result = sender.deliver(payload, sender.settings)
        self.assertFalse(result["ok"])
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(sleeps, [1, 2])
        logged = "".join(call[0][0] for call in stderr.write.call_args_list)
        self.assertIn("投递失败 3/3", logged)
        self.assertNotIn(SECRET, logged)

    def test_server_error_is_retried_then_succeeds_on_2xx_only(self):
        statuses = iter([500, 202])
        sender = HookSender(
            enabled("http://127.0.0.1:9/hook"),
            post=lambda *args: next(statuses),
            sleep=lambda seconds: None,
        )
        payload = build_payload([message()], {"summary": "", "text": ""}, False)
        with patch("sys.stderr"):
            result = sender.deliver(payload, sender.settings)
        self.assertEqual((result["ok"], result["attempts"], result["status"]), (True, 2, 202))


class HookWebTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.receiver = Receiver()
        self.addCleanup(self.receiver.close)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps({"profile": "corp:employee", "keep": 1}))
        os.chmod(str(self.config_path), 0o600)
        config = SimpleNamespace(port=8766, dws="dws-fixture", profile="corp:employee")
        self.store = PanelStore(self.root / "panel.db", config)
        self.store.receive(message())
        self.store.receive(message("cid-2:m2", ["at"]))
        self.store.create_draft("cid-1:m1", "同事请你查收", "收到", None)
        self.hooks = HookSender(None, self.config_path)
        self.regenerated = []
        self.server = start_panel(
            self.store, lambda: {}, self.regenerated.append, port=0, hooks=self.hooks
        )
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.connection = http.client.HTTPConnection("127.0.0.1", self.port)
        self.addCleanup(self.connection.close)
        self.connection.request("GET", "/")
        page = self.connection.getresponse().read().decode()
        self.token = re.search('nonce="([^"]+)"', page).group(1)

    def request(self, method, path, body=None, headers=None):
        raw = json.dumps(body).encode() if body is not None else None
        self.connection.request(method, path, raw, headers or {})
        response = self.connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")

    def panel(self, path, body):
        return self.request(
            "POST",
            path,
            body,
            {
                "Origin": "http://127.0.0.1:" + str(self.port),
                "X-Panel-Token": self.token,
                "Content-Type": "application/json",
            },
        )

    def signed(self, path, body, secret=SECRET):
        raw = json.dumps(body).encode()
        timestamp = str(int(time.time()))
        mac = hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256)
        headers = {
            "Content-Type": "application/json",
            "X-Ears-Timestamp": timestamp,
            "X-Ears-Signature": "sha256=" + mac.hexdigest(),
        }
        self.connection.request("POST", path, raw, headers)
        response = self.connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")

    def test_settings_save_to_config_hide_secret_and_send_test(self):
        status, value = self.panel("/api/hooks", enabled(self.receiver.url))
        self.assertEqual(status, 200)
        self.assertTrue(value["secret_set"])
        self.assertNotIn("secret", value)
        saved = json.loads(self.config_path.read_text())
        self.assertEqual((saved["keep"], saved["hooks"]["secret"]), (1, SECRET))
        self.assertEqual(stat.S_IMODE(os.stat(str(self.config_path)).st_mode), 0o600)
        status, value = self.request("GET", "/api/hooks")
        self.assertEqual(
            (status, value["url"], value["secret_set"]), (200, self.receiver.url, True)
        )
        status, result = self.panel("/api/hooks/test", {"secret": ""})
        self.assertEqual((status, result["ok"], result["event"]), (200, True, "hook.test"))
        headers, body = self.receiver.requests.get(timeout=2)
        headers = {key.lower(): value for key, value in headers.items()}
        self.assertEqual(
            headers["x-ears-signature"], expected_signature(headers["x-ears-timestamp"], body)
        )
        self.assertEqual(json.loads(body)["message"]["sender"], "测试消息")
        status, value = self.panel("/api/hooks", {"url": "not a url"})
        self.assertEqual(status, 409)

    def test_write_back_requires_panel_token_or_valid_signature(self):
        self.panel("/api/hooks", enabled(self.receiver.url))
        for headers in [
            {},
            {"X-Ears-Timestamp": str(int(time.time())), "X-Ears-Signature": "sha256=00"},
        ]:
            status, unused = self.request("POST", "/messages/cid-2%3Am2/dismiss", {}, headers)
            self.assertEqual(status, 403)
        status, unused = self.signed("/messages/cid-2%3Am2/dismiss", {}, "wrong-secret-0123456789")
        self.assertEqual(status, 403)
        self.assertFalse(self.store.is_message_handled("cid-2:m2"))

    def test_signed_dismiss_marks_handled_and_reply_uses_reviewed_send_path(self):
        self.panel("/api/hooks", enabled(self.receiver.url))
        status, value = self.signed("/messages/cid-2%3Am2/dismiss", {})
        self.assertEqual((status, value["handled"]), (202, True))
        self.assertTrue(self.store.is_message_handled("cid-2:m2"))
        self.assertIn("__overview__", self.regenerated)
        receipt = SimpleNamespace(returncode=0, stdout=json.dumps({"deliveryStatus": "delivered"}))
        with patch("backend.dingtalk.subprocess.run", return_value=receipt) as run:
            status, value = self.signed("/messages/cid-1%3Am1/reply", {"text": "好的，马上看"})
            self.assertEqual(status, 202)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not self.store.is_message_handled("cid-1:m1"):
                time.sleep(0.01)
        command = run.call_args[0][0]
        self.assertEqual(command[command.index("--text") + 1], "好的，马上看")
        self.assertEqual(command[command.index("--open-dingtalk-id") + 1], "other-person")
        self.assertEqual(self.store.get_draft(value["draft_id"])["status"], "delivered")
        status, value = self.signed("/messages/cid-1%3Am1/reply", {"text": "重复发送"})
        self.assertEqual(status, 409)
        self.assertEqual(run.call_count, 1)

    def test_unknown_send_result_blocks_a_second_external_reply(self):
        self.panel("/api/hooks", enabled(self.receiver.url))
        failed = SimpleNamespace(returncode=1, stdout="")
        with patch("backend.dingtalk.subprocess.run", return_value=failed) as run:
            status, value = self.signed("/messages/cid-1%3Am1/reply", {"text": "第一次"})
            self.assertEqual(status, 202)
            deadline = time.monotonic() + 3
            while (
                time.monotonic() < deadline
                and self.store.get_draft(value["draft_id"])["status"] == "sending"
            ):
                time.sleep(0.01)
            self.assertEqual(self.store.get_draft(value["draft_id"])["status"], "unknown")
            status, value = self.signed("/messages/cid-1%3Am1/reply", {"text": "再试一次"})
        self.assertEqual(status, 409)
        self.assertIn("发送记录", value["error"])
        self.assertEqual(run.call_count, 1)

    def test_signed_requests_are_rejected_while_hooks_are_disabled(self):
        status, unused = self.signed("/messages/cid-2%3Am2/dismiss", {})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
