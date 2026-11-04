"""Download image-message resources into the private local state directory."""

import hashlib
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

try:
    import imghdr

    def _detect_kind(path):
        return imghdr.what(str(path))

except ImportError:

    def _detect_kind(path):
        try:
            with open(path, "rb") as f:
                head = f.read(32)
            if head.startswith(b"\x89PNG\r\n\x1a\n"):
                return "png"
            if head.startswith(b"\xff\xd8\xff"):
                return "jpeg"
            if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
                return "gif"
            if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
                return "webp"
        except OSError:
            pass
        return None


MEDIA_ID = re.compile(r"\[图片消息\]\(mediaId=([^\s)]+)\)")
IMAGE_TYPES = {"jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}


def image_resource(text):
    if not isinstance(text, str):
        return None
    match = MEDIA_ID.search(text)
    return match.group(1) if match and len(match.group(1)) <= 1024 else None


def image_content_type(path):
    kind = _detect_kind(str(path))
    return IMAGE_TYPES.get(kind)


class MediaDownloader:
    def __init__(self, config, store):
        self.config = config
        self.store = store
        self.root = config.state_dir / "media"
        self.pending = set()
        self.lock = threading.Lock()

    def queue(self, message):
        if (message.get("image") or {}).get("state") == "ready":
            return
        if self.store and message.get("key"):
            try:
                stored = self.store.get_message(message["key"])
                if (stored.get("image") or {}).get("state") == "ready":
                    return
            except (KeyError, ValueError):
                pass
        resource_id = (message.get("image") or {}).get("resource_id") or image_resource(
            message.get("text")
        )
        if not resource_id or not message.get("message_id"):
            return
        with self.lock:
            if message["key"] in self.pending:
                return
            self.pending.add(message["key"])
        threading.Thread(
            target=self.download, args=(message["key"], resource_id), daemon=True
        ).start()

    def download(self, key, resource_id):
        try:
            message = self.store.get_message(key)
            if not message.get("message_id") or not message.get("conversation_id"):
                raise ValueError("图片消息缺少资源定位信息")
            if (message.get("image") or {}).get("state") == "ready":
                return
            directory = hashlib.sha256(key.encode("utf-8")).hexdigest()
            output = self.root / directory
            output.mkdir(parents=True, exist_ok=True)
            command = [
                self.config.dws,
                "chat",
                "+messages-resource-download",
                "--type",
                "mediaId",
                "--resource-id",
                resource_id,
                "--message-id",
                message["message_id"],
                "--open-conversation-id",
                message["conversation_id"],
                "--output",
                "media/" + directory,
                "--profile",
                self.config.profile,
                "--format",
                "json",
            ]
            result = subprocess.run(
                command,
                cwd=str(self.config.state_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=60,
            )
            image = next(
                (
                    path
                    for path in sorted(output.iterdir())
                    if path.is_file() and image_content_type(path)
                ),
                None,
            )
            if result.returncode != 0 or image is None:
                raise ValueError("图片下载失败")
            self.store.set_image(
                key,
                {
                    "state": "ready",
                    "resource_id": resource_id,
                    "path": str(image.relative_to(self.root)),
                },
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self.store.set_image(
                key,
                {
                    "state": "error",
                    "resource_id": resource_id,
                    "error": "图片下载失败，请在钉钉查看原图",
                },
            )
        finally:
            with self.lock:
                self.pending.discard(key)

    def prune_unreferenced(self, referenced):
        if not self.root.exists():
            return 0
        keep = {Path(path).parts[0] for path in referenced if Path(path).parts}
        removed = 0
        for path in self.root.iterdir():
            if path.name in keep:
                continue
            if path.is_dir():
                shutil.rmtree(str(path))
            else:
                path.unlink()
            removed += 1
        return removed


def safe_image_path(root, relative):
    if not isinstance(relative, str) or not relative:
        return None
    root = Path(root).resolve()
    candidate = (root / relative).resolve()
    if os.path.commonpath([str(root), str(candidate)]) != str(root):
        return None
    return candidate if candidate.is_file() and image_content_type(candidate) else None
