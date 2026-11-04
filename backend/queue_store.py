"""Durable incoming message queue and batching policy."""

import json
import sqlite3


class Store:
    def __init__(
        self, path, direct_seconds=30, group_seconds=300, trigger_count=20, max_messages=50
    ):
        self.direct_seconds = direct_seconds
        self.group_seconds = group_seconds
        self.trigger_count = trigger_count
        self.max_messages = max_messages
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS messages (key TEXT PRIMARY KEY, body TEXT NOT NULL, first_seen REAL NOT NULL, done INTEGER NOT NULL DEFAULT 0)"
        )

    def add(self, message, received):
        old = self.db.execute("SELECT body FROM messages WHERE key=?", (message["key"],)).fetchone()
        if old:
            merged = json.loads(old[0])
            merged["sources"] = sorted(set(merged["sources"] + message["sources"]))
            self.db.execute(
                "UPDATE messages SET body=? WHERE key=?",
                (json.dumps(merged, ensure_ascii=False), message["key"]),
            )
        else:
            self.db.execute(
                "INSERT INTO messages (key,body,first_seen) VALUES (?,?,?)",
                (message["key"], json.dumps(message, ensure_ascii=False), received),
            )
        self.db.commit()

    def pending_count(self):
        return self.db.execute("SELECT COUNT(*) FROM messages WHERE done=0").fetchone()[0]

    def history(self):
        for row in self.db.execute("SELECT body FROM messages ORDER BY first_seen"):
            yield json.loads(row[0])

    def prune_before(self, cutoff):
        cursor = self.db.execute("DELETE FROM messages WHERE first_seen < ?", (cutoff,))
        self.db.commit()
        return cursor.rowcount

    def requeue(self, key):
        self.db.execute("UPDATE messages SET done=0,first_seen=0 WHERE key=?", (key,))
        self.db.commit()

    def due(self, now):
        rows = self.db.execute(
            "SELECT body,first_seen FROM messages WHERE done=0 ORDER BY first_seen LIMIT ?",
            (self.max_messages,),
        ).fetchall()
        items = [(json.loads(body), first) for body, first in rows]
        if len(items) >= self.trigger_count:
            return [m for m, unused in items]
        return [
            m
            for m, first in items
            if now - first
            >= (self.direct_seconds if set(m["sources"]) & {"direct", "at"} else self.group_seconds)
        ]

    def ack(self, batch):
        self.db.executemany("UPDATE messages SET done=1 WHERE key=?", [(m["key"],) for m in batch])
        self.db.commit()

    def close(self):
        self.db.close()
