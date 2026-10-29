"""Loopback HTTP panel with strict Host, Origin and action-token validation."""

import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from .dingtalk import create_todo, deliver
from .hooks import SIGNATURE_HEADER, TIMESTAMP_HEADER, HookSender, verify

MESSAGE_ACTION = re.compile(r"^/messages/([^/?#]+)/(reply|dismiss)$")


def start_panel(store, status, regenerate, port=None, hooks=None):
    port = store.config.port if port is None else port
    if hooks is None:
        hooks = HookSender(getattr(store.config, "hooks", None))
    static = Path(__file__).with_name("static")
    token = secrets.token_urlsafe(32)
    origin = "http://127.0.0.1:" + str(port)
    page = (static / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", token)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, data, content_type="application/json; charset=utf-8"):
            body = (
                data.encode("utf-8")
                if isinstance(data, str)
                else json.dumps(data, ensure_ascii=False).encode("utf-8")
            )
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'nonce-"
                + token
                + "'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def allowed(self, write=False):
            if self.headers.get("Host") != "127.0.0.1:" + str(port):
                return False
            supplied = self.headers.get("Origin")
            if supplied and supplied != origin:
                return False
            return not write or (
                supplied == origin
                and secrets.compare_digest(self.headers.get("X-Panel-Token", ""), token)
            )

        def do_GET(self):
            if not self.allowed():
                return self.reply(403, {"error": "访问来源不允许"})
            if self.path == "/":
                return self.reply(200, page, "text/html; charset=utf-8")
            if self.path == "/panel.js":
                script = (
                    (static / "panel.js").read_text(encoding="utf-8").replace("__TOKEN__", token)
                )
                return self.reply(200, script, "text/javascript; charset=utf-8")
            if self.path == "/panel.css":
                return self.reply(
                    200,
                    (static / "panel.css").read_text(encoding="utf-8"),
                    "text/css; charset=utf-8",
                )
            if self.path == "/api/hooks":
                return self.reply(200, hooks.public())
            if self.path == "/api/state":
                return self.reply(
                    200,
                    {
                        "messages": store.snapshot(),
                        "status": status(),
                        "overview": store.get_overview(),
                    },
                )
            self.reply(404, {"error": "不存在"})

        def signed(self, raw):
            """External callers sign the raw body with the hooks secret; no Origin allowed."""
            secret = hooks.settings["secret"] if hooks.settings["enabled"] else ""
            return not self.headers.get("Origin") and verify(
                secret,
                self.headers.get(TIMESTAMP_HEADER, ""),
                raw,
                self.headers.get(SIGNATURE_HEADER, ""),
            )

        def message_action(self, key, verb, raw):
            if not (self.allowed(True) or self.signed(raw)):
                return self.reply(403, {"error": "签名或面板校验失败"})
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(body, dict):
                    raise ValueError("请求内容需要 JSON 对象")
                key = unquote(key)
                if verb == "reply":
                    draft, message = store.claim_reply(key, body.get("text"))
                    threading.Thread(
                        target=deliver, args=(store, draft, message, regenerate), daemon=True
                    ).start()
                    return self.reply(202, {"ok": True, "id": key, "draft_id": draft["id"]})
                store.archive_message(key)
                regenerate("__overview__")
                return self.reply(202, {"ok": True, "id": key, "handled": True})
            except (ValueError, KeyError, TypeError) as error:
                return self.reply(409, {"error": str(error)})

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 <= length <= 65536:
                return self.reply(413, {"error": "请求内容大小不正确"})
            raw = self.rfile.read(length) if length else b""
            action = MESSAGE_ACTION.match(self.path)
            if action:
                return self.message_action(action.group(1), action.group(2), raw)
            if not self.allowed(True):
                return self.reply(403, {"error": "请求校验失败，请从本地面板操作"})
            try:
                if not raw:
                    raise ValueError("请求内容大小不正确")
                body = json.loads(raw.decode("utf-8"))
                if self.path == "/api/hooks":
                    return self.reply(200, hooks.update(body))
                if self.path == "/api/hooks/test":
                    return self.reply(200, hooks.send_test(body))
                if self.path == "/api/send":
                    draft, message = store.claim_send(body["id"], body["version"], body["text"])
                    threading.Thread(
                        target=deliver, args=(store, draft, message, regenerate), daemon=True
                    ).start()
                elif self.path == "/api/ignore":
                    store.ignore(body["id"], body["version"])
                    regenerate("__overview__")
                elif self.path == "/api/handled":
                    store.archive_message(body["key"])
                    regenerate("__overview__")
                elif self.path == "/api/overview":
                    regenerate("__overview__")
                elif self.path in {"/api/todo", "/api/todo-manual"}:
                    if self.path == "/api/todo-manual":
                        message = store.get_message(body["key"])
                        if not isinstance(body.get("title"), str):
                            raise ValueError("请填写待办标题")
                        title = body["title"].strip()
                        (ident,) = store.add_candidates(
                            [
                                {
                                    "message_key": message["key"],
                                    "title": title,
                                    "evidence": message["text"][:120],
                                }
                            ],
                            [message["key"]],
                        )
                        task = store.claim_task(ident, title)
                    else:
                        task = store.claim_task(body["id"], body["title"])
                    threading.Thread(
                        target=create_todo, args=(store, task, regenerate), daemon=True
                    ).start()
                elif self.path == "/api/regenerate":
                    message = store.get_message(body["key"])
                    regenerate(message["key"])
                elif self.path == "/api/manual":
                    store.create_draft(body["key"], "手动回复", "", None)
                else:
                    return self.reply(404, {"error": "不存在"})
                self.reply(202, {"ok": True})
            except (ValueError, KeyError, TypeError) as error:
                self.reply(409, {"error": str(error)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    port = server.server_address[1]
    origin = "http://127.0.0.1:" + str(port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
