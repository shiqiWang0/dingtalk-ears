"""Full host lifecycle against local CLI fixtures, with no external service calls."""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener


class HostTests(unittest.TestCase):
    def test_configured_scope_batches_and_shutdown_preserve_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            dws = root / "dws-fixture"
            dws.write_text(
                "#!"
                + sys.executable
                + """
import sys,json,time
assert sys.argv[1:3]==['event','+listen-im']
assert sys.argv[sys.argv.index('--profile')+1]=='fixture:employee'
kind=sys.argv[sys.argv.index('--kind')+1]
group=kind=='group'
cid=sys.argv[sys.argv.index('--chat-id')+1] if group else 'fixture-direct'
print('[event] ready',file=sys.stderr,flush=True)
print(json.dumps({'conversation_id':cid,
 'message_id':'group-message' if group else 'same-message',
 'sender_open_dingtalk_id':'other-person','sender':'测试同事','content':'合成消息，请查收'}),flush=True)
while True: time.sleep(1)
"""
            )
            codex = root / "codex-fixture"
            codex.write_text(
                "#!"
                + sys.executable
                + """
import sys,json
assert sys.argv[1:3]==['app-server','--stdio']
def send(value): print(json.dumps(value),flush=True)
for line in sys.stdin:
 x=json.loads(line); method=x.get('method')
 if method=='initialize': send({'id':x['id'],'result':{}})
 elif method in ['thread/start','thread/resume','thread/fork']:
  send({'id':x['id'],'result':{'thread':{'id':'fixture-thread'}}})
 elif method=='turn/start':
  tid=x['params']['threadId']
  send({'id':x['id'],'result':{'turn':{'id':'fixture-turn'}}})
  text=json.dumps({'summary':'收到合成消息','needs_reply':False,'draft':'','todos':[]})
  send({'method':'turn/completed','params':{'threadId':tid,'turn':{
   'id':'fixture-turn','status':'completed','items':[{'type':'agentMessage','text':text}]}}})
"""
            )
            for command in [dws, codex]:
                os.chmod(str(command), 0o700)
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "profile": "fixture:employee",
                        "self_id": "fixture-self",
                        "display_name": "测试用户",
                        "group": {"id": "fixture-group", "name": "设计组"},
                        "server": {"port": port},
                        "commands": {"dws": str(dws), "codex": str(codex)},
                        "batch": {"direct_seconds": 1, "group_seconds": 1},
                    }
                )
            )
            process = subprocess.Popen(
                [sys.executable, "-m", "backend", "--config", str(config), "run"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            opener = build_opener(ProxyHandler({}))
            result = None
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    self.assertIsNone(process.poll(), "host unexpectedly exited")
                    try:
                        with opener.open(
                            "http://127.0.0.1:" + str(port) + "/api/state", timeout=1
                        ) as response:
                            result = json.load(response)
                        if (
                            len(result["messages"]) == 2
                            and result["overview"]["updated"]
                            and all(item["draft"] for item in result["messages"])
                        ):
                            break
                    except OSError:
                        pass
                    time.sleep(0.2)
                self.assertIsNotNone(result)
                self.assertEqual(len(result["messages"]), 2)
                self.assertEqual(result["status"]["group_name"], "设计组")
                self.assertTrue(result["overview"]["updated"])
                direct = next(
                    item
                    for item in result["messages"]
                    if item["conversation_id"] == "fixture-direct"
                )
                self.assertEqual(set(direct["sources"]), {"direct", "at"})
                self.assertEqual(direct["draft"]["text"], "")
                base = "http://127.0.0.1:" + str(port)
                with opener.open(base + "/") as response:
                    token = re.search('nonce="([^"]+)"', response.read().decode()).group(1)

                def change(path, value):
                    request = Request(
                        base + "/api/" + path,
                        data=json.dumps(value).encode(),
                        headers={
                            "Content-Type": "application/json",
                            "X-Panel-Token": token,
                            "Origin": base,
                        },
                    )
                    with opener.open(request) as response:
                        self.assertEqual(response.status, 202)

                def wait_groups(ids):
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        with opener.open(base + "/api/state") as response:
                            state = json.load(response)
                        mapping = state["status"].get("group_conversations", {})
                        if set(mapping.values()) == set(ids) and all(
                            state["status"]["listeners"].get(source, {}).get("state") == "ready"
                            for source in mapping
                        ):
                            return state
                        time.sleep(0.1)
                    self.fail("group subscriptions did not synchronize: " + str(state["status"]))

                original_pid = result["status"]["pid"]
                original_group_pid = result["status"]["listeners"]["ued"]["pid"]
                direct_pids = {
                    key: result["status"]["listeners"][key]["pid"] for key in ("direct", "at")
                }
                change("groups", {"id": "extra-group", "name": "新增群"})
                changed = wait_groups(["fixture-group", "extra-group"])
                self.assertEqual(changed["status"]["pid"], original_pid)
                self.assertEqual(changed["status"]["listener_count"], 4)
                change("groups/remove", {"id": "fixture-group"})
                changed = wait_groups(["extra-group"])
                with self.assertRaises(ProcessLookupError):
                    os.kill(original_group_pid, 0)
                self.assertEqual(changed["status"]["group_name"], "新增群")
                self.assertEqual(changed["status"]["listener_count"], 3)
                self.assertIsNone(changed["status"]["error"])
                self.assertTrue(
                    any(m["conversation_id"] == "extra-group" for m in changed["messages"])
                )
                change("groups/remove", {"id": "extra-group"})
                changed = wait_groups([])
                self.assertEqual(set(changed["status"]["listeners"]), {"direct", "at"})
                self.assertEqual(changed["status"]["listener_count"], 2)
                self.assertEqual(
                    {key: changed["status"]["listeners"][key]["pid"] for key in direct_pids},
                    direct_pids,
                )
                change("groups", {"id": "fixture-group", "name": "重新关注"})
                changed = wait_groups(["fixture-group"])
                self.assertEqual(changed["status"]["group_name"], "重新关注")
                self.assertEqual(changed["status"]["pid"], original_pid)
            finally:
                process.send_signal(signal.SIGTERM)
                out, err = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, err.decode())
            self.assertTrue((root / "data/panel.db").is_file())
            state = json.loads((root / "data/output/运行状态.json").read_text())
            self.assertEqual(state["state"], "stopped")
