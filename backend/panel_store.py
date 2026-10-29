"""Transactional inbox, draft and todo state. No external writes happen here."""

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .dingtalk import build_send_command


class PanelStore:
    def __init__(self, path, config):
        self.config = config
        self.path = str(path)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
            db.execute(
                "UPDATE drafts SET status='unknown',version=version+1 WHERE status='sending'"
            )
            db.execute("UPDATE todos SET status='unknown' WHERE status='creating'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def receive(self, message):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT body FROM inbox WHERE key=?", (message["key"],)).fetchone()
            if old:
                body = json.loads(old["body"])
                body["sources"] = sorted(set(body["sources"] + message["sources"]))
                db.execute(
                    "UPDATE inbox SET body=? WHERE key=?",
                    (json.dumps(body, ensure_ascii=False), message["key"]),
                )
                return
            db.execute(
                "INSERT INTO inbox VALUES (?,?,?,?)",
                (
                    message["key"],
                    message["conversation_id"],
                    json.dumps(message, ensure_ascii=False),
                    time.time(),
                ),
            )
            db.execute(
                "UPDATE drafts SET status='stale',version=version+1 WHERE status='pending' AND message_key IN (SELECT key FROM inbox WHERE cid=?)",
                (message["conversation_id"],),
            )

    def get_message(self, key):
        with self.connect() as db:
            row = db.execute("SELECT body FROM inbox WHERE key=?", (key,)).fetchone()
            if not row:
                raise ValueError("原消息不存在")
            return json.loads(row["body"])

    def get_thread(self, cid):
        with self.connect() as db:
            row = db.execute("SELECT thread_id FROM threads WHERE cid=?", (cid,)).fetchone()
            return row["thread_id"] if row else None

    def set_thread(self, cid, thread_id):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO threads VALUES (?,?)", (cid, thread_id))

    def confirmed_replies(self, cid):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT d.text,d.created FROM drafts d JOIN inbox i ON d.message_key=i.key WHERE i.cid=? AND d.status='delivered' ORDER BY d.created DESC LIMIT 10",
                    (cid,),
                )
            ]

    def decode_draft(self, row):
        if not row:
            raise ValueError("草稿不存在")
        result = dict(row)
        result["receipt"] = json.loads(result["receipt"]) if result["receipt"] else None
        return result

    def get_draft(self, ident):
        with self.connect() as db:
            return self.decode_draft(
                db.execute("SELECT * FROM drafts WHERE id=?", (ident,)).fetchone()
            )

    def is_handled(self, db, key):
        return bool(
            db.execute("SELECT 1 FROM handled WHERE message_key=?", (key,)).fetchone()
            or db.execute(
                "SELECT 1 FROM drafts WHERE message_key=? AND status IN ('delivered','ignored') LIMIT 1",
                (key,),
            ).fetchone()
        )

    def archive_message(self, key):
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM inbox WHERE key=?", (key,)).fetchone():
                raise ValueError("原消息不存在")
            db.execute("INSERT OR REPLACE INTO handled VALUES (?,?)", (key, time.time()))

    def is_message_handled(self, key):
        with self.connect() as db:
            return self.is_handled(db, key)

    def save_overview(self, text, count, error=None):
        value = {"text": text, "count": count, "updated": time.time(), "error": error}
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES (?,?)",
                ("overview", json.dumps(value, ensure_ascii=False)),
            )

    def get_overview(self):
        with self.connect() as db:
            row = db.execute("SELECT body FROM metadata WHERE key='overview'").fetchone()
            return (
                json.loads(row["body"])
                if row
                else {"text": "尚未生成整体总结", "count": 0, "updated": None, "error": None}
            )

    def add_candidates(self, candidates, allowed_keys):
        """Validate and store candidates, returning stable IDs even for existing rows."""
        identifiers = []
        with self.connect() as db:
            for item in candidates:
                if not isinstance(item, dict) or any(
                    not isinstance(item.get(name), str)
                    for name in ["message_key", "title", "evidence"]
                ):
                    raise ValueError("待办格式不正确")
                key = item["message_key"]
                title = item["title"].strip()
                evidence = item["evidence"].strip()
                if key not in allowed_keys or not 0 < len(title) <= 200 or not evidence:
                    raise ValueError("待办缺少本批消息中的有效来源或标题")
                row = db.execute("SELECT body FROM inbox WHERE key=?", (key,)).fetchone()
                if not row or evidence not in json.loads(row["body"])["text"]:
                    raise ValueError("待办原文证据未在来源消息中找到")
                ident = hashlib.sha256((key + "\n" + title).encode("utf-8")).hexdigest()
                db.execute(
                    "INSERT OR IGNORE INTO todos VALUES (?,?,?,?,?,?,?,?)",
                    (ident, key, title, evidence, "candidate", None, None, time.time()),
                )
                identifiers.append(ident)
        return identifiers

    def claim_task(self, ident, title):
        if not isinstance(title, str) or not 0 < len(title.strip()) <= 200:
            raise ValueError("待办标题需要 1–200 字")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM todos WHERE id=?", (ident,)).fetchone()
            if not row or row["status"] != "candidate":
                raise ValueError("待办已添加、正在添加或结果不明，不能重复创建")
            task = dict(row)
            task.update(title=title.strip(), status="creating")
            db.execute(
                "UPDATE todos SET title=?,status='creating' WHERE id=?", (task["title"], ident)
            )
            return task

    def finish_task(self, ident, receipt):
        if not isinstance(receipt, dict):
            receipt = {"error": "创建回执格式不正确，请在钉钉核对"}
        data = receipt.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        verified = receipt.get("ok") is True and data.get("verified") is True and data.get("taskId")
        with self.connect() as db:
            db.execute(
                "UPDATE todos SET status=?,task_id=?,receipt=? WHERE id=?",
                (
                    "created" if verified else "unknown",
                    str(data["taskId"]) if data.get("taskId") else None,
                    json.dumps(receipt, ensure_ascii=False),
                    ident,
                ),
            )

    def create_draft(self, message_key, summary, text, thread_id):
        ident = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.is_handled(db, message_key):
                raise ValueError("这条消息已处理，不再生成重复回复")
            row = db.execute("SELECT cid FROM inbox WHERE key=?", (message_key,)).fetchone()
            if not row:
                raise ValueError("原消息不存在")
            latest = db.execute(
                "SELECT key FROM inbox WHERE cid=? ORDER BY rowid DESC LIMIT 1", (row["cid"],)
            ).fetchone()["key"]
            status = "pending" if latest == message_key else "stale"
            db.execute(
                "UPDATE drafts SET status='stale',version=version+1 WHERE message_key=? AND status='pending'",
                (message_key,),
            )
            db.execute(
                "INSERT INTO drafts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    message_key,
                    summary,
                    text,
                    thread_id,
                    status,
                    1,
                    "ears-" + ident,
                    None,
                    time.time(),
                ),
            )
        return self.get_draft(ident)

    def claim_send(self, ident, version, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 10000:
            raise ValueError("请填写 1–10000 字的回复")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            draft = self.decode_draft(
                db.execute("SELECT * FROM drafts WHERE id=?", (ident,)).fetchone()
            )
            if self.is_handled(db, draft["message_key"]):
                raise ValueError("这条消息已处理，不能再次发送")
            if draft["status"] != "pending" or draft["version"] != version:
                raise ValueError("草稿已更新、已发送或状态不可发送，请刷新查看")
            message = json.loads(
                db.execute(
                    "SELECT body FROM inbox WHERE key=?", (draft["message_key"],)
                ).fetchone()["body"]
            )
            latest = db.execute(
                "SELECT key FROM inbox WHERE cid=? ORDER BY rowid DESC LIMIT 1",
                (message["conversation_id"],),
            ).fetchone()["key"]
            if latest != draft["message_key"]:
                raise ValueError("对方已有新消息，请先查看最新内容并重新生成草稿")
            # Validate routing before recording the irreversible send attempt.
            build_send_command(draft, message, text, self.config)
            db.execute(
                "UPDATE drafts SET status='sending',text=?,version=version+1 WHERE id=?",
                (text, ident),
            )
            draft.update(status="sending", text=text, version=version + 1)
        return draft, message

    reply_lock = threading.Lock()

    def claim_reply(self, key, text):
        """Reuse the pending draft or open a manual one, then apply the normal send checks.

        A message that already has a sending, unknown or delivered reply is never sent again,
        so a retried or duplicated external call cannot produce a second DingTalk message.
        """
        with self.reply_lock:
            with self.connect() as db:
                if not db.execute("SELECT 1 FROM inbox WHERE key=?", (key,)).fetchone():
                    raise ValueError("原消息不存在")
                if db.execute(
                    "SELECT 1 FROM drafts WHERE message_key=? AND status IN ('sending','unknown','delivered')",
                    (key,),
                ).fetchone():
                    raise ValueError("这条消息已有发送记录，为避免重复不会再次发送")
                row = db.execute(
                    "SELECT id,version,status FROM drafts WHERE message_key=? ORDER BY created DESC LIMIT 1",
                    (key,),
                ).fetchone()
            if row and row["status"] == "pending":
                ident, version = row["id"], row["version"]
            else:
                draft = self.create_draft(key, "外部回复", "", None)
                ident, version = draft["id"], draft["version"]
            return self.claim_send(ident, version, text)

    def finish_send(self, ident, receipt):
        status = "delivered" if receipt.get("deliveryStatus") == "delivered" else "unknown"
        with self.connect() as db:
            db.execute(
                "UPDATE drafts SET status=?,receipt=?,version=version+1 WHERE id=?",
                (status, json.dumps(receipt, ensure_ascii=False), ident),
            )

    def ignore(self, ident, version):
        with self.connect() as db:
            db.execute(
                "UPDATE drafts SET status='ignored',version=version+1 WHERE id=? AND version=? AND status IN ('pending','stale')",
                (ident, version),
            )

    def snapshot(self):
        with self.connect() as db:
            messages = []
            for row in db.execute("SELECT key,body FROM inbox ORDER BY rowid DESC LIMIT 100"):
                item = json.loads(row["body"])
                draft = db.execute(
                    "SELECT * FROM drafts WHERE message_key=? ORDER BY created DESC LIMIT 1",
                    (row["key"],),
                ).fetchone()
                item["draft"] = self.decode_draft(draft) if draft else None
                item["handled"] = self.is_handled(db, row["key"])
                item["todos"] = [
                    dict(task)
                    for task in db.execute(
                        "SELECT id,title,evidence,status,task_id FROM todos WHERE message_key=? ORDER BY created",
                        (row["key"],),
                    )
                ]
                messages.append(item)
            return messages
