import sys
import tempfile
import unittest
from pathlib import Path

from backend.agy_client import AgyClient


class AgyClientTests(unittest.TestCase):
    def test_personal_identity_is_context_data_not_system_instructions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "mock_agy.py"
            captured = root / "captured.json"
            # mock 脚本接收来自 AgyClient 的命令行调用并输出结构化 json
            fixture.write_text(
                """import json, sys
dest = sys.argv[1]
prompt = sys.argv[2]
with open(dest, 'w', encoding='utf-8') as f:
    f.write(prompt)

result = {
    "conversation_id": "mock-conv-123",
    "status": "SUCCESS",
    "structured_output": {
        "summary": "测试摘要",
        "needs_reply": False,
        "draft": "",
        "todos": []
    }
}
print(json.dumps(result, ensure_ascii=False))
""",
                encoding="utf-8",
            )
            client = AgyClient(
                root,
                command=[sys.executable, str(fixture), str(captured)],
                display_name="私有用户称呼",
                group_name="私有群名称",
            )
            try:
                thread = client.thread(None)
                client.generate(thread, {"task": "overview", "items": []})
                prompt_content = captured.read_text(encoding="utf-8")
                # 确认私有用户称呼存在于输入 context 数据中
                self.assertIn("私有用户称呼", prompt_content)
                self.assertIn("私有群名称", prompt_content)
                self.assertTrue(client.is_alive())
            finally:
                client.close()
                self.assertFalse(client.is_alive())

    def test_conversation_id_is_preserved_and_mapped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "mock_agy_conv.py"
            # 记录是否传递了 --conversation
            fixture.write_text(
                """import json, sys
result = {
    "conversation_id": "conv-persistent-999",
    "status": "SUCCESS",
    "structured_output": {
        "summary": "已收到",
        "needs_reply": False,
        "draft": "",
        "todos": []
    }
}
print(json.dumps(result, ensure_ascii=False))
""",
                encoding="utf-8",
            )
            client = AgyClient(root, command=[sys.executable, str(fixture)])
            try:
                thread = client.thread(None)
                # 首次生成
                res = client.generate(thread, {"new_messages": [], "confirmed_sent_replies": []})
                self.assertEqual(res["summary"], "已收到")
                # 验证内部映射成功记录了 conv-persistent-999
                self.assertEqual(client.threads.get(thread), "conv-persistent-999")
            finally:
                client.close()

    def test_cli_error_raises_runtime_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "mock_fail.py"
            fixture.write_text(
                """import sys
sys.stderr.write("model quota exceeded\\n")
sys.exit(1)
""",
                encoding="utf-8",
            )
            client = AgyClient(root, command=[sys.executable, str(fixture)])
            try:
                thread = client.thread(None)
                with self.assertRaises(RuntimeError) as ctx:
                    client.generate(thread, {"new_messages": []})
                self.assertIn("model quota exceeded", str(ctx.exception))
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
