"""Own the DWS listener subprocesses; do not create unbounded retry loops."""

import json
import subprocess
import threading

from .events import normalize
from .utils import now_text


class ListenerManager:
    def __init__(self, config, status, inbox, stop):
        self.config = config
        self.status = status
        self.inbox = inbox
        self.stop = stop
        self.children = []
        self.subscriptions = {}

    def start(self, source, args, group_ids=None):
        process = subprocess.Popen(
            [self.config.dws, "event", "+listen-im"]
            + args
            + ["--profile", self.config.profile, "--duration", "0", "--format", "ndjson"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            bufsize=1,
        )
        self.children.append(process)
        stopped = threading.Event()
        entry = self.status["listeners"][source] = {
            "state": "starting",
            "pid": process.pid,
            "raw_received": 0,
            "accepted": 0,
            "filtered": 0,
        }

        def lifecycle():
            log_path = self.config.state_dir / (source + ".log")
            with log_path.open("w", encoding="utf-8") as log:
                for line in process.stderr:
                    log.write(line)
                    log.flush()
                    if "[event] ready" in line:
                        entry["state"] = "ready"
                    if "[event] transport " in line:
                        try:
                            entry["transport"] = json.loads(line.split("[event] transport ", 1)[1])
                        except ValueError:
                            pass
            code = process.wait()
            entry.update(state="stopped", exit_code=code)
            if not self.stop.is_set() and not stopped.is_set():
                self.status["error"] = source + " listener stopped; inspect " + str(log_path)

        def receive():
            for line in process.stdout:
                if stopped.is_set():
                    continue
                try:
                    raw = json.loads(line)
                    entry["raw_received"] += 1
                    entry["last_received"] = now_text()
                    value = normalize(
                        raw,
                        source,
                        self.config.self_id,
                        self.config.group_ids if group_ids is None else group_ids,
                    )
                    if value:
                        entry["accepted"] += 1
                        self.inbox.put(value)
                    else:
                        entry["filtered"] += 1
                except (ValueError, TypeError, KeyError) as error:
                    self.status["error"] = "Malformed event: " + str(error)

        lifecycle_thread = threading.Thread(target=lifecycle, daemon=True)
        receive_thread = threading.Thread(target=receive, daemon=True)
        self.subscriptions[source] = (process, stopped, lifecycle_thread, receive_thread)
        lifecycle_thread.start()
        receive_thread.start()

    def remove(self, source):
        """Stop only this subscription; planned removal is not a connection failure."""
        process, stopped, lifecycle_thread, receive_thread = self.subscriptions[source]
        stopped.set()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        lifecycle_thread.join(timeout=1)
        receive_thread.join(timeout=1)
        process.stdin.close()
        if not lifecycle_thread.is_alive():
            process.stderr.close()
        if not receive_thread.is_alive():
            process.stdout.close()
        self.subscriptions.pop(source)
        self.children.remove(process)
        self.status["listeners"].pop(source, None)
