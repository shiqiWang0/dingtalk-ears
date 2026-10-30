import json
import tempfile
import unittest
from functools import partial
from pathlib import Path
from types import SimpleNamespace

from backend.events import normalize
from backend.queue_store import Store


class EarsTests(unittest.TestCase):
    def setUp(self):
        self.m = SimpleNamespace(
            GROUP="group-test",
            SELF="self-test",
            Store=Store,
            normalize=partial(normalize, self_id="self-test", group_ids=("group-test",)),
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.store = self.m.Store(Path(self.tmp.name) / "queue.db")

    def tearDown(self):
        if hasattr(self, "store"):
            self.store.close()
            self.tmp.cleanup()

    def event(self, source="direct", cid="direct-1", mid="m1"):
        return self.m.normalize(
            {
                "message_id": mid,
                "conversation_id": cid,
                "sender_open_dingtalk_id": "other",
                "sender": "同事",
                "content": "周五前确认方案",
            },
            source,
        )

    def test_nested_envelope_extracts_business_content(self):
        value = {
            "data": json.dumps(
                {
                    "payload": {
                        "body": {
                            "messageId": "m2",
                            "conversationId": self.m.GROUP,
                            "sender": "同事",
                            "content": "请确认",
                            "createTime": "123",
                        }
                    }
                }
            )
        }
        got = self.m.normalize(value, "ued")
        self.assertEqual(got["text"], "请确认")
        self.assertEqual(got["message_id"], "m2")

    def test_rejects_oa_other_group_and_self_messages(self):
        self.assertIsNone(self.m.normalize({"type": "user_oa_approval_task_created"}, "at"))
        self.assertIsNone(self.event("ued", "another-group"))
        self.assertIsNone(
            self.m.normalize({"sender_open_dingtalk_id": self.m.SELF, "content": "自己"}, "direct")
        )

    def test_accepts_each_configured_group(self):
        self.assertIsNotNone(
            self.m.normalize(
                {"conversation_id": "second-group", "content": "额外群消息"},
                "group-1",
                group_ids=(self.m.GROUP, "second-group"),
            )
        )

    def test_duplicate_channels_merge_and_promote_mention(self):
        self.store.add(self.event("ued", self.m.GROUP), 100)
        self.store.add(self.event("at", self.m.GROUP), 101)
        self.assertEqual(self.store.pending_count(), 1)
        self.assertEqual(self.store.due(129), [])
        rows = self.store.due(131)
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]["sources"]), {"ued", "at"})

    def test_queue_survives_restart_and_ack_only_consumes_batch(self):
        self.store.add(self.event(), 100)
        batch = self.store.due(131)
        self.store.add(self.event(mid="m2"), 132)
        self.store.close()
        self.store = self.m.Store(Path(self.tmp.name) / "queue.db")
        self.assertEqual(self.store.pending_count(), 2)
        self.store.ack(batch)
        self.assertEqual(self.store.pending_count(), 1)
        self.store.add(self.event(), 200)
        self.assertEqual(self.store.pending_count(), 1)
        self.store.requeue(batch[0]["key"])
        self.assertEqual(self.store.pending_count(), 2)
        self.assertEqual(self.store.due(132), batch)
        history = list(self.store.history())
        self.assertEqual([m["message_id"] for m in history], ["m1", "m2"])
        self.assertEqual(history[0], batch[0])

    def test_low_traffic_group_flushes_by_time_and_busy_group_by_count(self):
        self.store.add(self.event("ued", self.m.GROUP), 100)
        self.assertEqual(self.store.due(399), [])
        self.assertEqual(len(self.store.due(400)), 1)
        for n in range(1, 20):
            self.store.add(self.event("ued", self.m.GROUP, "m" + str(n + 1)), 200)
        self.assertEqual(len(self.store.due(201)), 20)


if __name__ == "__main__":
    unittest.main()
