"""Integration with Antigravity CLI (agy) for draft generation and summaries."""

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .ai_contract import RESULT_SCHEMA, validate_result
from .prompts import SYSTEM_INSTRUCTIONS


class _DummyProcess:
    """提供与 AppServer.process 兼容的 poll 接口"""

    def __init__(self, is_closed_func):
        self._is_closed = is_closed_func

    def poll(self):
        return 0 if self._is_closed() else None


class AgyClient:
    """使用 agy CLI 运行基于 session/conversation 的生成器"""

    def __init__(
        self,
        state,
        command=None,
        display_name="本人",
        group_name="关注群",
        executable=None,
        model="gemini-3.8-flash-medium",
    ):
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True)
        self.context = {"current_user": display_name, "watched_group": group_name}
        self.executable = executable or shutil.which("agy") or "agy"
        self.custom_command = command
        self.model = model
        self.log = (self.state / "agy.log").open("a", encoding="utf-8")
        self.threads_file = self.state / "agy_threads.json"
        self.lock = threading.Lock()
        self.threads = self._load_threads()
        self.closed = False
        self.process = _DummyProcess(lambda: self.closed)

    def _load_threads(self):
        if self.threads_file.exists():
            try:
                return json.loads(self.threads_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
        return {}

    def _save_threads(self):
        try:
            temp_file = self.threads_file.with_suffix(".tmp")
            temp_file.write_text(json.dumps(self.threads, ensure_ascii=False), encoding="utf-8")
            temp_file.replace(self.threads_file)
        except OSError:
            pass

    def is_alive(self):
        return not self.closed

    def thread(self, existing):
        """返回已有 thread_id 或生成逻辑标识"""
        if existing:
            return existing
        import uuid

        return "agy-" + uuid.uuid4().hex[:12]

    def generate(self, thread_id, data, timeout=180):
        """调用 agy 生成结构化结果并校验契约"""
        if self.closed:
            raise RuntimeError("AgyClient 已关闭")

        conv_id = self.threads.get(thread_id)
        payload = dict(data, context=self.context)
        prompt = (
            f"{SYSTEM_INSTRUCTIONS}\n\n"
            f"输入数据：\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )

        if self.custom_command:
            # 允许测试注入自定义 mock 命令
            cmd = list(self.custom_command) + [prompt]
        else:
            cmd = [
                self.executable,
                "--print",
                prompt,
                "--model",
                self.model,
                "--output-format",
                "json",
                "--json-schema",
                json.dumps(RESULT_SCHEMA, ensure_ascii=False),
                "--disable-slash-commands",
            ]
            # 若已有关联的 conversation 则传入以保持会话上下文
            if conv_id:
                cmd.extend(["--conversation", conv_id])

        self.log.write(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] RUN: thread_id={thread_id}, conv_id={conv_id}\n"
        )
        self.log.flush()

        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("agy 生成超时，原消息已保留，可手动重试")

        if res.returncode != 0:
            err_msg = res.stderr.strip() or f"exit code {res.returncode}"
            self.log.write(f"ERROR: {err_msg}\n")
            self.log.flush()
            raise RuntimeError(f"agy 执行失败：{err_msg[:300]}")

        try:
            output = json.loads(res.stdout)
        except ValueError:
            raise ValueError("agy 未返回有效的 JSON 输出")

        # 记录并保存 agy 分配的 conversation_id
        new_conv_id = output.get("conversation_id")
        if new_conv_id and thread_id:
            with self.lock:
                if self.threads.get(thread_id) != new_conv_id:
                    self.threads[thread_id] = new_conv_id
                    self._save_threads()

        # 优先读取 structured_output，若无则尝试 response
        if "structured_output" in output and output["structured_output"] is not None:
            text = json.dumps(output["structured_output"], ensure_ascii=False)
        else:
            text = output.get("response", "")

        return validate_result(text)

    def close(self):
        """关闭客户端并释放日志文件句柄"""
        self.closed = True
        try:
            self.log.close()
        except Exception:
            pass
