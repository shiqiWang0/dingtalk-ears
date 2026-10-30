"""Loopback HTTP panel with strict Host, Origin and action-token validation."""

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .dingtalk import create_todo, deliver


def start_panel(store, status, regenerate, port=None, groups=None, add_group=None, remove_group=None, available_groups=None):
    port = store.config.port if port is None else port
    static = Path(__file__).with_name("static")
    token = secrets.token_urlsafe(32)
    page = (static / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", token)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, data, content_type="application/json; charset=utf-8"):
            body = (
                data
                if isinstance(data, (bytes, bytearray))
                else data.encode("utf-8")
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
            allowed_hosts = {"127.0.0.1:" + str(port), "localhost:" + str(port)}
            allowed_origins = {"http://127.0.0.1:" + str(port), "http://localhost:" + str(port)}
            # 校验请求 Host 是否来自允许的本地回环地址
            if self.headers.get("Host") not in allowed_hosts:
                return False
            supplied = self.headers.get("Origin")
            # 存在 Origin 时校验是否在允许的本地源列表中
            if supplied and supplied not in allowed_origins:
                return False
            # 写请求需来自允许的 Origin 且携带有效的面板令牌
            return not write or (
                supplied in allowed_origins
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
            clean_path = self.path.split("?")[0]
            if clean_path == "/panel.css":
                return self.reply(
                    200,
                    (static / "panel.css").read_text(encoding="utf-8"),
                    "text/css; charset=utf-8",
                )
            if clean_path == "/favicon.ico":
                ico = static / "favicon.ico"
                # 存在本地图标文件时返回钉钉图标
                if ico.exists():
                    return self.reply(200, ico.read_bytes(), "image/x-icon")
            if self.path == "/api/state":
                return self.reply(
                    200,
                    {
                        "messages": store.snapshot(),
                        "status": status(),
                        "overview": store.get_overview(),
                        "groups": groups() if groups else [],
                    },
                )
            if self.path == "/api/groups/available" and available_groups:
                return self.reply(200, {"groups": available_groups()})
            self.reply(404, {"error": "不存在"})

        def do_POST(self):
            if not self.allowed(True):
                return self.reply(403, {"error": "请求校验失败，请从本地面板操作"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("请求内容大小不正确")
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if self.path == "/api/send":
                    draft, message = store.claim_send(body["id"], body["version"], body["text"])
                    threading.Thread(
                        target=deliver, args=(store, draft, message, regenerate), daemon=True
                    ).start()
                elif self.path == "/api/ignore":
                    store.ignore(body["id"], body["version"])
                    regenerate("__overview__")
                elif self.path == "/api/todo-ignore":
                    store.ignore_task(body["id"])
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
                elif self.path == "/api/groups":
                    if not add_group:
                        raise ValueError("关注群管理未启用")
                    add_group(body["id"], body["name"])
                elif self.path == "/api/groups/remove":
                    if not remove_group:
                        raise ValueError("关注群管理未启用")
                    remove_group(body["id"])
                else:
                    return self.reply(404, {"error": "不存在"})
                self.reply(202, {"ok": True})
            except (ValueError, KeyError, TypeError) as error:
                self.reply(409, {"error": str(error)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
