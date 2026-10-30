"""Coordinate queue batching, AI work and the local review panel."""

import fcntl
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time

from .appserver import AppServer
from .config import add_group, bind_state, configured_groups, remove_group
from .dingtalk import list_joined_groups
from .listeners import ListenerManager
from .panel_store import PanelStore
from .queue_store import Store
from .utils import atomic_write, now_text
from .web import start_panel


class Host:
    def __init__(self, config, adopt_state=False):
        self.adopt_state = adopt_state
        self.config = config
        self.stop = threading.Event()
        self.inbox = queue.Queue()
        self.results = queue.Queue()
        self.controls = queue.Queue()
        self.agent = None
        self.overview_requested = False

        self.status = {
            "started": now_text(),
            "pid": os.getpid(),
            "listeners": {},
            "summary_state": "waiting",
            "appserver": "starting",
            "ai_provider": config.ai_provider,
            "panel_url": config.panel_url,
            "group_name": config.group_name,
            "group_names": {},
            "listener_count": 2 + len(config.watched_groups),
            "error": None,
            "received": 0,
        }
        self.listeners = ListenerManager(config, self.status, self.inbox, self.stop)
        self.busy = False
        self.paused = False

    def create_agent(self):
        # 根据配置的 ai_provider 选用对应的生成器
        if self.config.ai_provider == "agy":
            from .agy_client import AgyClient

            return AgyClient(
                self.config.state_dir / "agent",
                executable=self.config.agy,
                display_name=self.config.display_name,
                group_name=self.config.group_name,
                model=self.config.ai_model,
            )
        return AppServer(
            self.config.state_dir / "agent",
            executable=self.config.codex,
            display_name=self.config.display_name,
            group_name="、".join(name for unused, name in self.config.watched_groups),
        )

    def generate(self, batch):
        try:
            if self.agent is None or not self.agent.is_alive():
                if self.agent:
                    self.agent.close()
                self.agent = self.create_agent()
                self.status["appserver"] = "ready"
            groups = {}
            for message in batch:
                if not self.panel.is_message_handled(message["key"]):
                    groups.setdefault(message["conversation_id"], []).append(message)
            texts = []
            for cid, messages in groups.items():
                thread_id = self.agent.thread(self.panel.get_thread(cid))
                self.panel.set_thread(cid, thread_id)
                result = self.agent.generate(
                    thread_id,
                    {
                        "new_messages": messages,
                        "confirmed_sent_replies": self.panel.confirmed_replies(cid),
                    },
                )
                if not self.panel.is_message_handled(messages[-1]["key"]):
                    try:
                        self.panel.create_draft(
                            messages[-1]["key"], result["summary"], result["draft"], thread_id
                        )
                    except ValueError:
                        if not self.panel.is_message_handled(messages[-1]["key"]):
                            raise
                    self.panel.add_candidates(
                        result.get("todos", []), [message["key"] for message in messages]
                    )
                texts.append(result["summary"])
            try:
                self.update_overview()
            except Exception as error:
                previous = self.panel.get_overview()
                self.panel.save_overview(
                    previous["text"],
                    previous["count"],
                    "整体总结更新失败，可重试：" + str(error)[:180],
                )
            self.results.put((batch, "\n\n".join(texts), None))
        except Exception as error:
            self.results.put((batch, None, str(error)))

    def update_overview(self):
        messages = [m for m in self.panel.snapshot() if not m["handled"]][:40]
        if not messages:
            self.panel.save_overview("当前没有未处理消息。", 0)
            return
        thread_id = self.agent.thread(self.panel.get_thread("__overview__"))
        self.panel.set_thread("__overview__", thread_id)
        items = [
            {
                "sender": m["sender"],
                "time": m["time"],
                "sources": m["sources"],
                "text": m["text"][:5000],
                "summary": m["draft"]["summary"] if m["draft"] else None,
                "todos": m["todos"],
            }
            for m in messages
        ]
        result = self.agent.generate(thread_id, {"task": "overview", "items": items})
        self.panel.save_overview(result["summary"], len(messages))

    def run(self):
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        lock = (self.config.state_dir / "host.lock").open("w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        bind_state(self.config, adopt=self.adopt_state)
        signal.signal(signal.SIGTERM, lambda unused, frame: self.stop.set())
        signal.signal(signal.SIGINT, lambda unused, frame: self.stop.set())
        store = Store(
            self.config.state_dir / "queue.db",
            self.config.direct_seconds,
            self.config.group_seconds,
            self.config.trigger_count,
            self.config.max_messages,
        )
        self.panel = PanelStore(self.config.state_dir / "panel.db", self.config)
        self.overview_requested = self.panel.get_overview()["updated"] is None
        for message in store.history():
            self.panel.receive(message)
        server = start_panel(
            self.panel,
            lambda: self.status,
            self.controls.put,
            groups=lambda: configured_groups(self.config.config_path),
            add_group=lambda group_id, name: add_group(self.config.config_path, group_id, name),
            remove_group=lambda group_id: remove_group(self.config.config_path, group_id),
            available_groups=lambda: list_joined_groups(self.config),
        )
        actual_port = server.server_address[1]
        url = f"http://127.0.0.1:{actual_port}/"
        # 换行打印面板访问地址
        print(
            f"\n本地面板访问地址：\n{url}\nhttp://localhost:{actual_port}/\n",
            flush=True,
        )
        # 前台交互式终端运行时主动唤起默认浏览器打开面板
        if sys.stdout.isatty():
            try:
                import webbrowser

                webbrowser.open(url)
            except Exception:
                pass
        try:
            self.agent = self.create_agent()
            self.status["appserver"] = "ready"
        except Exception as error:
            self.paused = True
            self.status.update(
                appserver="error", summary_state="paused_after_error", error=str(error)
            )
        digest = self.config.output_dir / "消息摘要.md"
        if not digest.exists():
            digest.write_text(
                "# 钉钉消息摘要\n\n等待接通后收到的新消息。只整理，不自动回复。\n", encoding="utf-8"
            )
        try:
            subscriptions = [
                ("direct", ["--kind", "all-direct"]),
                ("at", ["--kind", "at-me"]),
            ]
            for index, (group_id, group_name) in enumerate(self.config.watched_groups):
                source = "ued" if index == 0 else "group-{}".format(index)
                self.status["group_names"][source] = group_name
                subscriptions.append((source, ["--kind", "group", "--chat-id", group_id]))
            for source, args in subscriptions:
                if self.stop.is_set():
                    break
                self.listeners.start(source, args)
                # Serialize subscription setup to avoid racing creation of the shared bus.
                until = time.monotonic() + 45
                while (
                    self.status["listeners"][source]["state"] == "starting"
                    and time.monotonic() < until
                ):
                    if self.stop.wait(0.2):
                        break
            while not self.stop.is_set():
                if self.agent and self.agent.process.poll() is not None:
                    self.paused = True
                    self.status.update(
                        appserver="error",
                        summary_state="paused_after_error",
                        error="App Server 已退出，消息会继续保存；可在面板点击重新生成以恢复",
                    )
                try:
                    message = self.inbox.get(timeout=1)
                    store.add(message, time.time())
                    self.panel.receive(message)
                    self.status["received"] += 1
                except queue.Empty:
                    pass
                try:
                    key = self.controls.get_nowait()
                    if key == "__overview__":
                        self.overview_requested = True
                    else:
                        store.requeue(key)
                    self.paused = False
                    self.status["error"] = None
                except queue.Empty:
                    pass
                try:
                    batch, text, error = self.results.get_nowait()
                    self.busy = False
                    if error:
                        self.paused = True
                        self.status.update(summary_state="paused_after_error", error=error)
                    else:
                        self.paused = False
                        if batch:
                            heading = "\n\n## {} · {} 条消息\n\n".format(now_text(), len(batch))
                            with digest.open("a", encoding="utf-8") as output:
                                output.write(heading + text + "\n")
                                output.flush()
                                os.fsync(output.fileno())
                        store.ack(batch)
                        self.status.update(
                            summary_state="waiting", last_summary=now_text(), error=None
                        )
                except queue.Empty:
                    pass
                if not self.busy and not self.paused:
                    batch = store.due(time.time())
                    if batch or self.overview_requested:
                        self.overview_requested = False
                        self.busy = True
                        self.status["summary_state"] = "generating"
                        threading.Thread(target=self.generate, args=(batch,), daemon=True).start()
                self.status["pending"] = store.pending_count()
                self.status["updated"] = now_text()
                atomic_write(
                    self.config.output_dir / "运行状态.json",
                    json.dumps(self.status, ensure_ascii=False, indent=2),
                )
        finally:
            server.shutdown()
            server.server_close()
            if self.agent:
                self.agent.close()
            for process in self.listeners.children:
                if process.poll() is None:
                    process.terminate()
            for process in self.listeners.children:
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.status["error"] = "DWS shutdown pending for pid " + str(process.pid)
            store.close()
            self.status.update(state="stopped", updated=now_text())
            atomic_write(
                self.config.output_dir / "运行状态.json",
                json.dumps(self.status, ensure_ascii=False, indent=2),
            )
