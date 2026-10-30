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

    def start(self, source, args):
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
        self.status["listeners"][source] = {
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
                        self.status["listeners"][source]["state"] = "ready"
                    if "[event] transport " in line:
                        try:
                            self.status["listeners"][source]["transport"] = json.loads(
                                line.split("[event] transport ", 1)[1]
                            )
                        except ValueError:
                            pass
            code = process.wait()
            self.status["listeners"][source].update(state="stopped", exit_code=code)
            if not self.stop.is_set():
                self.status["error"] = source + " listener stopped; inspect " + str(log_path)

        def receive():
            for line in process.stdout:
                try:
                    raw = json.loads(line)
                    self.status["listeners"][source]["raw_received"] += 1
                    self.status["listeners"][source]["last_received"] = now_text()
                    value = normalize(
                        raw,
                        source,
                        self.config.self_id,
                        self.config.group_ids,
                    )
                    if value:
                        self.status["listeners"][source]["accepted"] += 1
                        self.inbox.put(value)
                    else:
                        self.status["listeners"][source]["filtered"] += 1
                except (ValueError, TypeError, KeyError) as error:
                    self.status["error"] = "Malformed event: " + str(error)

        threading.Thread(target=lifecycle, daemon=True).start()
        threading.Thread(target=receive, daemon=True).start()
