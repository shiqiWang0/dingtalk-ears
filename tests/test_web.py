"""Exercise actual loopback requests with a non-default port and synthetic data."""

import http.client
import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.panel_store import PanelStore
from backend.web import start_panel


class WebTests(unittest.TestCase):
    def test_group_management_updates_panel_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PanelStore(Path(directory) / "panel.db", SimpleNamespace(port=8766))
            groups = [{"id": "primary", "name": "默认群", "primary": True}]

            def add(group_id, name):
                groups.append({"id": group_id, "name": name, "primary": False})

            def remove(group_id):
                groups[:] = [group for group in groups if group["id"] != group_id]

            server = start_panel(
                store, lambda: {}, lambda key: None, port=0, groups=lambda: groups, add_group=add, remove_group=remove
            )
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
            try:
                connection.request("GET", "/")
                token = re.search('nonce="([^"]+)"', connection.getresponse().read().decode()).group(1)
                headers = {"Origin": "http://127.0.0.1:" + str(server.server_address[1]), "X-Panel-Token": token, "Content-Type": "application/json"}
                connection.request("POST", "/api/groups", json.dumps({"id": "extra", "name": "研发群"}), headers)
                self.assertEqual(connection.getresponse().status, 202)
                connection.request("POST", "/api/groups/remove", json.dumps({"id": "extra"}), headers)
                self.assertEqual(connection.getresponse().status, 202)
                connection.request("GET", "/api/state")
                self.assertEqual(json.loads(connection.getresponse().read())["groups"], groups)
            finally:
                connection.close()
                server.shutdown()
                server.server_close()

    def test_candidate_and_manual_todos_share_confirmation_and_duplicate_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PanelStore(
                Path(directory) / "panel.db",
                SimpleNamespace(port=8766, dws="unused", profile="test"),
            )
            message = {
                "key": "test:m1",
                "conversation_id": "test",
                "text": "请确认方案",
                "sources": ["direct"],
            }
            store.receive(message)
            store.add_candidates(
                [{"message_key": message["key"], "title": "确认方案", "evidence": "确认方案"}],
                [message["key"]],
            )
            candidate = store.snapshot()[0]["todos"][0]
            server = start_panel(store, lambda: {}, lambda key: None, port=0)
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
            try:
                connection.request("GET", "/")
                page = connection.getresponse().read().decode()
                token = re.search('nonce="([^"]+)"', page).group(1)
                headers = {
                    "Origin": "http://127.0.0.1:" + str(server.server_address[1]),
                    "X-Panel-Token": token,
                    "Content-Type": "application/json",
                }
                receipt = SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {"ok": True, "data": {"verified": True, "taskId": "fixture"}}
                    ),
                )
                with patch("backend.dingtalk.subprocess.run", return_value=receipt):
                    for endpoint, body in [
                        ("todo", {"id": candidate["id"], "title": "确认方案"}),
                        ("todo-manual", {"key": message["key"], "title": "  手动跟进  "}),
                    ]:
                        for expected in [202, 409]:
                            connection.request(
                                "POST", "/api/" + endpoint, json.dumps(body), headers
                            )
                            response = connection.getresponse()
                            response.read()
                            self.assertEqual(response.status, expected)
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        tasks = store.snapshot()[0]["todos"]
                        if all(task["status"] == "created" for task in tasks):
                            break
                        time.sleep(0.01)
                    self.assertEqual({task["title"] for task in tasks}, {"确认方案", "手动跟进"})
                    self.assertEqual(len(tasks), 2)
                    self.assertTrue(all(task["status"] == "created" for task in tasks))
            finally:
                connection.close()
                server.shutdown()
                server.server_close()

    def test_external_css_and_script_work_with_origin_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PanelStore(Path(directory) / "panel.db", SimpleNamespace(port=8766))
            # Binding port zero is useful for isolated instances; start_panel must use
            # the actual assigned port in Host and Origin checks.
            server = start_panel(store, lambda: {"group_name": "设计组"}, lambda key: None, port=0)
            port = server.server_address[1]
            try:
                connection = http.client.HTTPConnection("127.0.0.1", port)
                connection.request("GET", "/")
                response = connection.getresponse()
                page = response.read().decode()
                self.assertEqual(response.status, 200)
                self.assertIn("/panel.css", page)
                token = re.search('nonce="([^"]+)"', page).group(1)
                for resource, kind in [
                    ("/panel.css", "text/css"),
                    ("/panel.js", "text/javascript"),
                ]:
                    connection.request("GET", resource)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertIn(kind, response.getheader("Content-Type"))
                    self.assertNotIn("__TOKEN__", response.read().decode())
                # 验证钉钉 favicon 静态资源返回
                connection.request("GET", "/favicon.ico")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual("image/x-icon", response.getheader("Content-Type"))
                self.assertTrue(len(response.read()) > 0)
                connection.request(
                    "POST",
                    "/api/overview",
                    body="{}",
                    headers={
                        "Origin": "http://127.0.0.1:" + str(port),
                        "X-Panel-Token": token,
                        "Content-Type": "application/json",
                    },
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 202)
                response.read()
                connection.close()

                # 验证 localhost 访问及 Origin 请求兼容性
                local_conn = http.client.HTTPConnection("127.0.0.1", port)
                local_conn.request("GET", "/", headers={"Host": "localhost:" + str(port)})
                local_resp = local_conn.getresponse()
                self.assertEqual(local_resp.status, 200)
                local_resp.read()

                local_conn.request(
                    "POST",
                    "/api/overview",
                    body="{}",
                    headers={
                        "Host": "localhost:" + str(port),
                        "Origin": "http://localhost:" + str(port),
                        "X-Panel-Token": token,
                        "Content-Type": "application/json",
                    },
                )
                local_post_resp = local_conn.getresponse()
                self.assertEqual(local_post_resp.status, 202)
                local_post_resp.read()
                local_conn.close()
            finally:
                server.shutdown()
                server.server_close()
