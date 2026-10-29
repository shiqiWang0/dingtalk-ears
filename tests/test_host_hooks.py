"""Host lifecycle with hooks enabled: delivery happens after triage and never blocks it."""

import hashlib
import hmac
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from tests.test_hooks import SECRET, Receiver, closed_url

DWS = """
import sys,json,time
kind=sys.argv[sys.argv.index('--kind')+1]
group=kind=='group'
print('[event] ready',file=sys.stderr,flush=True)
print(json.dumps({'conversation_id':'fixture-group' if group else 'fixture-direct',
 'message_id':'group-message' if group else 'same-message',
 'sender_open_dingtalk_id':'other-person','sender':'测试同事','content':'合成消息，请查收'}),flush=True)
while True: time.sleep(1)
"""
CODEX = """
import sys,json
def send(value): print(json.dumps(value),flush=True)
for line in sys.stdin:
 x=json.loads(line); method=x.get('method')
 if method=='initialize': send({'id':x['id'],'result':{}})
 elif method in ['thread/start','thread/resume','thread/fork']:
  send({'id':x['id'],'result':{'thread':{'id':'fixture-thread'}}})
 elif method=='turn/start':
  tid=x['params']['threadId']
  send({'id':x['id'],'result':{'turn':{'id':'fixture-turn'}}})
  text=json.dumps({'summary':'收到合成消息','needs_reply':True,'draft':'收到','todos':[]})
  send({'method':'turn/completed','params':{'threadId':tid,'turn':{
   'id':'fixture-turn','status':'completed','items':[{'type':'agentMessage','text':text}]}}})
"""


class HostHookTests(unittest.TestCase):
    def run_host(self, url):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        commands = {}
        for name, source in [("dws", DWS), ("codex", CODEX)]:
            path = root / (name + "-fixture")
            path.write_text("#!" + sys.executable + "\n" + source)
            os.chmod(str(path), 0o700)
            commands[name] = str(path)
        config = root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "profile": "fixture:employee",
                    "self_id": "fixture-self",
                    "display_name": "测试用户",
                    "group": {"id": "fixture-group", "name": "设计组"},
                    "server": {"port": port},
                    "commands": commands,
                    "batch": {"direct_seconds": 1, "group_seconds": 1},
                    "hooks": {"enabled": True, "url": url, "secret": SECRET, "timeout_seconds": 1},
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
                    if len(result["messages"]) == 2 and all(m["draft"] for m in result["messages"]):
                        break
                except OSError:
                    pass
                time.sleep(0.2)
            time.sleep(4)  # let retries against an unreachable target finish
        finally:
            process.send_signal(signal.SIGTERM)
            unused, err = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, err.decode())
        return result, err.decode()

    def test_direct_message_is_delivered_and_group_message_is_filtered(self):
        receiver = Receiver()
        self.addCleanup(receiver.close)
        result, unused = self.run_host(receiver.url)
        self.assertTrue(all(item["draft"] for item in result["messages"]))
        headers, body = receiver.requests.get(timeout=2)
        headers = {key.lower(): value for key, value in headers.items()}
        mac = hmac.new(
            SECRET.encode(), headers["x-ears-timestamp"].encode() + b"." + body, hashlib.sha256
        )
        self.assertEqual(headers["x-ears-signature"], "sha256=" + mac.hexdigest())
        payload = json.loads(body)
        self.assertEqual(payload["message"]["conversation_id"], "fixture-direct")
        self.assertEqual(payload["suggested_reply"], "收到")
        self.assertTrue(receiver.requests.empty(), "group message must not match the filter")

    def test_unreachable_target_does_not_stop_triage(self):
        result, err = self.run_host(closed_url())
        self.assertEqual(len(result["messages"]), 2)
        self.assertTrue(all(item["draft"] for item in result["messages"]))
        self.assertIn("投递失败 3/3", err)
        self.assertNotIn(SECRET, err)


if __name__ == "__main__":
    unittest.main()
