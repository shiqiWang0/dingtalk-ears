import tempfile
import unittest
from pathlib import Path

try:
    from types import SimpleNamespace

    from backend.dingtalk import build_send_command
    from backend.panel_store import PanelStore

    CONFIG = SimpleNamespace(profile="test:person", dws="dws", port=8766)
except ImportError:
    PanelStore = None


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(PanelStore, "review and reply queue not implemented")
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "panel.db"
        self.store = PanelStore(self.path, CONFIG)
        self.msg = {
            "key": "c1:m1",
            "conversation_id": "c1",
            "message_id": "m1",
            "sender": "同事",
            "sender_id": "real-person",
            "time": "19:00",
            "text": "你好，请确认分享主题和确认方案",
            "sources": ["direct"],
        }
        self.store.receive(self.msg)

    def tearDown(self):
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def draft(self):
        return self.store.create_draft(self.msg["key"], "同事打招呼", "你好", "thread-1")

    def test_unapproved_draft_never_sends_and_target_comes_from_event(self):
        draft = self.draft()
        self.assertEqual(draft["status"], "pending")
        self.assertEqual(draft["receipt"], None)
        cmd = build_send_command(draft, self.msg, "你好呀", CONFIG)
        self.assertIn("--ai-tag=false", cmd)
        self.assertEqual(cmd[cmd.index("--open-dingtalk-id") + 1], "real-person")
        self.assertNotIn("--group", cmd)

    def test_confirm_edits_claims_once_and_blocks_duplicate_send(self):
        draft = self.draft()
        claimed, message = self.store.claim_send(draft["id"], draft["version"], "你好呀")
        self.assertEqual(claimed["text"], "你好呀")
        self.assertEqual(claimed["status"], "sending")
        self.assertEqual(message["sender_id"], "real-person")
        with self.assertRaises(ValueError):
            self.store.claim_send(draft["id"], draft["version"], "重复")
        self.store.finish_send(
            draft["id"], {"deliveryStatus": "delivered", "openMessageId": "sent1"}
        )
        self.assertEqual(self.store.get_draft(draft["id"])["status"], "delivered")

    def test_new_message_blocks_stale_draft_but_duplicate_does_not(self):
        draft = self.draft()
        self.store.receive(self.msg)
        self.assertEqual(self.store.get_draft(draft["id"])["status"], "pending")
        newer = dict(self.msg, key="c1:m2", message_id="m2", text="补充一下")
        self.store.receive(newer)
        with self.assertRaises(ValueError):
            self.store.claim_send(draft["id"], draft["version"], "旧回复")

    def test_unknown_receipt_and_restart_never_make_a_draft_sendable_again(self):
        draft = self.draft()
        self.store.claim_send(draft["id"], draft["version"], "你好")
        self.store = PanelStore(self.path, CONFIG)
        self.assertEqual(self.store.get_draft(draft["id"])["status"], "unknown")
        with self.assertRaises(ValueError):
            self.store.claim_send(draft["id"], draft["version"], "重发")

    def test_model_cannot_retarget_a_draft_and_late_result_is_stale(self):
        self.store.receive(dict(self.msg, key="c1:m2", message_id="m2", text="更新"))
        draft = self.draft()
        self.assertEqual(draft["status"], "stale")
        self.assertEqual(draft["message_key"], "c1:m1")
        self.store.set_thread("c1", "thread-1")
        self.assertEqual(self.store.get_thread("c1"), "thread-1")

    def test_group_reply_is_bound_to_original_conversation(self):
        group = dict(self.msg, sources=["ued"], conversation_id="real-group")
        cmd = build_send_command({"idem": "test"}, group, "原群回复", CONFIG)
        self.assertEqual(cmd[cmd.index("--group") + 1], "real-group")
        self.assertNotIn("--open-dingtalk-id", cmd)

    def test_handled_and_delivered_messages_are_archived_and_cannot_be_resent(self):
        draft = self.draft()
        self.store.archive_message(self.msg["key"])
        self.assertTrue(self.store.snapshot()[0]["handled"])
        with self.assertRaises(ValueError):
            self.store.claim_send(draft["id"], draft["version"], "旧回复")
        self.store.receive(dict(self.msg, key="c1:m2", message_id="m2"))
        sent = self.store.create_draft("c1:m2", "问候", "你好", "t")
        self.store.claim_send(sent["id"], sent["version"], "你好")
        self.store.finish_send(sent["id"], {"deliveryStatus": "delivered"})
        self.assertTrue(self.store.snapshot()[0]["handled"])
        with self.assertRaises(ValueError):
            self.store.create_draft("c1:m2", "再回一次", "你好", "t")

    def test_todo_candidates_are_local_deduplicated_and_claimed_only_once(self):
        candidate = {
            "title": "确认分享主题",
            "evidence": "请确认分享主题",
            "message_key": self.msg["key"],
        }
        self.store.add_candidates([candidate], [self.msg["key"]])
        self.store.add_candidates([candidate], [self.msg["key"]])
        tasks = self.store.snapshot()[0]["todos"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "candidate")
        task = self.store.claim_task(tasks[0]["id"], "准备分享主题")
        self.assertEqual(task["title"], "准备分享主题")
        self.assertEqual(task["status"], "creating")
        with self.assertRaises(ValueError):
            self.store.claim_task(task["id"], "重复添加")
        self.store = PanelStore(self.path, CONFIG)
        self.assertEqual(self.store.snapshot()[0]["todos"][0]["status"], "unknown")

    def test_todo_success_requires_verified_task_id_and_model_sources_stay_in_batch(self):
        candidate = {"title": "确认方案", "evidence": "确认方案", "message_key": self.msg["key"]}
        with self.assertRaises(ValueError):
            self.store.add_candidates([candidate], ["another-message"])
        self.store.add_candidates([candidate], [self.msg["key"]])
        task = self.store.snapshot()[0]["todos"][0]
        self.store.claim_task(task["id"], task["title"])
        self.store.finish_task(
            task["id"], {"ok": True, "data": {"taskId": "123", "verified": True}}
        )
        stored = self.store.snapshot()[0]["todos"][0]
        self.assertEqual(stored["status"], "created")
        self.assertEqual(stored["task_id"], "123")
        with self.assertRaises(ValueError):
            self.store.claim_task(task["id"], task["title"])

    def test_overview_is_persistent_and_does_not_imply_a_reply_was_sent(self):
        self.store.save_overview("有一条需要确认的消息", 1)
        self.store = PanelStore(self.path, CONFIG)
        self.assertEqual(self.store.get_overview()["text"], "有一条需要确认的消息")
        self.assertEqual(self.store.get_overview()["count"], 1)
        self.assertIsNone(self.store.snapshot()[0]["draft"])

    def test_retention_removes_expired_message_state_and_image_reference(self):
        self.store.set_image(self.msg["key"], {"state": "ready", "path": "old/image.png"})
        self.draft()
        with self.store.connect() as db:
            db.execute("UPDATE inbox SET received=0 WHERE key=?", (self.msg["key"],))
        self.assertEqual(self.store.prune_before(1), 1)
        with self.assertRaises(ValueError):
            self.store.get_message(self.msg["key"])
        self.assertEqual(self.store.image_paths(), set())

    def test_cross_origin_and_missing_token_cannot_trigger_send(self):
        import http.client
        import socket

        from backend.web import start_panel

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        server = start_panel(self.store, lambda: {}, lambda key: None, port=port)
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port)
            connection.request(
                "POST",
                "/api/send",
                body="{}",
                headers={"Origin": "https://untrusted.example", "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.close()
            connection = http.client.HTTPConnection("127.0.0.1", port)
            connection.request(
                "POST",
                "/api/send",
                body="{}",
                headers={
                    "Origin": "http://127.0.0.1:" + str(port),
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.close()
            self.assertIsNone(self.store.snapshot()[0]["draft"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
