import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.media import MediaDownloader, image_resource, safe_image_path
from backend.panel_store import PanelStore

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da6360f8cfc0000003010180c5f7b20000000049454e44ae426082"
)


class MediaTests(unittest.TestCase):
    def test_image_resource_requires_a_media_id(self):
        self.assertEqual(image_resource("[图片消息](mediaId=abc)"), "abc")
        self.assertIsNone(image_resource("普通文本"))

    def test_download_stores_only_a_local_image_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(state_dir=root, dws="dws", profile="corp:user", port=8766)
            store = PanelStore(root / "panel.db", config)
            store.receive(
                {
                    "key": "c1:m1",
                    "message_id": "m1",
                    "conversation_id": "c1",
                    "text": "[图片消息](mediaId=p1)",
                    "sources": ["direct"],
                }
            )
            downloader = MediaDownloader(config, store)

            def download(command, **kwargs):
                output = command[command.index("--output") + 1]
                path = Path(kwargs["cwd"]) / output / "image.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(PNG)
                return SimpleNamespace(returncode=0, stdout="{}", stderr="")

            with patch("backend.media.subprocess.run", side_effect=download):
                downloader.download("c1:m1", "p1")
            image = store.get_message("c1:m1")["image"]
            self.assertEqual(image["state"], "ready")
            self.assertFalse(Path(image["path"]).is_absolute())
            self.assertIsNotNone(safe_image_path(root / "media", image["path"]))
            self.assertIsNone(safe_image_path(root / "media", "../outside.png"))

    def test_prune_removes_only_unreferenced_media_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(state_dir=root, dws="dws", profile="corp:user", port=8766)
            downloader = MediaDownloader(config, None)
            kept = root / "media" / "kept"
            removed = root / "media" / "removed"
            kept.mkdir(parents=True)
            removed.mkdir()
            self.assertEqual(downloader.prune_unreferenced({"kept/image.png"}), 1)
            self.assertTrue(kept.exists())
            self.assertFalse(removed.exists())

    def test_ready_image_skips_download_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(state_dir=root, dws="dws", profile="corp:user", port=8766)
            store = PanelStore(root / "panel.db", config)
            # Message originally enqueued with pending state
            queue_message = {
                "key": "c1:m1",
                "message_id": "m1",
                "conversation_id": "c1",
                "text": "[图片消息](mediaId=p1)",
                "sources": ["direct"],
                "image": {"state": "pending", "resource_id": "p1"},
            }
            store.receive(queue_message)
            store.set_image("c1:m1", {"state": "ready", "path": "c1_m1/image.png"})

            downloader = MediaDownloader(config, store)

            with patch("backend.media.subprocess.run") as mock_run, patch(
                "backend.media.threading.Thread"
            ) as mock_thread:
                # 1. Store already holds state=ready: queueing pending message from queue history must not download
                downloader.queue(queue_message)
                mock_run.assert_not_called()
                mock_thread.assert_not_called()
                self.assertNotIn("c1:m1", downloader.pending)

                # 2. Message object itself has state=ready: must also not download
                already_ready = {
                    "key": "c1:m2",
                    "message_id": "m2",
                    "conversation_id": "c1",
                    "text": "[图片消息](mediaId=p2)",
                    "sources": ["direct"],
                    "image": {"state": "ready", "path": "c1_m2/image.png"},
                }
                downloader.queue(already_ready)
                mock_run.assert_not_called()
                mock_thread.assert_not_called()
                self.assertNotIn("c1:m2", downloader.pending)

    def test_pending_image_proceeds_to_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(state_dir=root, dws="dws", profile="corp:user", port=8766)
            store = PanelStore(root / "panel.db", config)
            pending_msg = {
                "key": "c1:m3",
                "message_id": "m3",
                "conversation_id": "c1",
                "text": "[图片消息](mediaId=p3)",
                "sources": ["direct"],
                "image": {"state": "pending", "resource_id": "p3"},
            }
            store.receive(pending_msg)
            downloader = MediaDownloader(config, store)

            def download(command, **kwargs):
                output = command[command.index("--output") + 1]
                path = Path(kwargs["cwd"]) / output / "image.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(PNG)
                return SimpleNamespace(returncode=0, stdout="{}", stderr="")

            with patch("backend.media.subprocess.run", side_effect=download) as mock_run:
                downloader.queue(pending_msg)
                # Wait for daemon thread to complete download
                deadline = 50
                while deadline > 0 and "c1:m3" in downloader.pending:
                    import time

                    time.sleep(0.02)
                    deadline -= 1
                mock_run.assert_called_once()
                self.assertEqual(store.get_message("c1:m3")["image"]["state"], "ready")
