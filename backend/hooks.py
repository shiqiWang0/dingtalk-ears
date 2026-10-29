"""Optional signed webhook for triaged messages. Delivery never blocks ingestion."""

import hashlib
import hmac
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .utils import now_text

SOURCES = ("direct", "at", "ued")
DEFAULTS = {
    "enabled": False,
    "url": "",
    "secret": "",
    "filter": {"sources": ["direct", "at"], "needs_reply_only": False},
    "timeout_seconds": 5,
    "retries": 2,
}
SIGNATURE_HEADER = "X-Ears-Signature"
TIMESTAMP_HEADER = "X-Ears-Timestamp"
TOLERANCE_SECONDS = 300


def _strict_int(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("{} 必须是 {}–{} 的整数".format(label, low, high))
    return value


def validate_hooks(value):
    """Return a complete hooks section; url and secret are only required when enabled."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("hooks 必须是对象")
    unknown = set(value) - set(DEFAULTS)
    if unknown:
        raise ValueError("hooks 含未知字段: " + ", ".join(sorted(unknown)))
    raw_filter = value.get("filter", DEFAULTS["filter"])
    if not isinstance(raw_filter, dict) or set(raw_filter) - set(DEFAULTS["filter"]):
        raise ValueError("hooks.filter 只支持 sources 和 needs_reply_only")
    sources = raw_filter.get("sources", DEFAULTS["filter"]["sources"])
    if not isinstance(sources, list) or any(item not in SOURCES for item in sources):
        raise ValueError("hooks.filter.sources 只能包含 direct、at、ued")
    needs_reply_only = raw_filter.get("needs_reply_only", False)
    enabled = value.get("enabled", False)
    if type(enabled) is not bool or type(needs_reply_only) is not bool:
        raise ValueError("hooks.enabled 和 needs_reply_only 必须是 true 或 false")
    url = value.get("url", "")
    secret = value.get("secret", "")
    if not isinstance(url, str) or not isinstance(secret, str):
        raise ValueError("hooks.url 和 hooks.secret 必须是字符串")
    url, secret = url.strip(), secret.strip()
    if url:
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or any(char.isspace() for char in url)
        ):
            raise ValueError("hooks.url 需要 http:// 或 https:// 地址")
    if secret and (len(secret) < 16 or len(secret) > 256):
        raise ValueError("hooks.secret 需要 16–256 个字符")
    if enabled and not (url and secret and sources):
        raise ValueError("启用 hooks 需要填写地址、签名密钥，并至少选择一种消息")
    return {
        "enabled": enabled,
        "url": url,
        "secret": secret,
        "filter": {
            "sources": [s for s in SOURCES if s in sources],
            "needs_reply_only": needs_reply_only,
        },
        "timeout_seconds": _strict_int(
            value.get("timeout_seconds", 5), "hooks.timeout_seconds", 1, 30
        ),
        "retries": _strict_int(value.get("retries", 2), "hooks.retries", 0, 5),
    }


def sign(secret, timestamp, body):
    """HMAC-SHA256 over "<timestamp>.<raw body>", hex encoded with a sha256= prefix."""
    digest = hmac.new(
        secret.encode("utf-8"), str(timestamp).encode("ascii") + b"." + body, hashlib.sha256
    )
    return "sha256=" + digest.hexdigest()


def verify(secret, timestamp, body, signature, now=None):
    if not secret or not timestamp or not signature:
        return False
    try:
        age = abs((time.time() if now is None else now) - int(timestamp))
    except ValueError:
        return False
    return age <= TOLERANCE_SECONDS and hmac.compare_digest(
        sign(secret, timestamp, body), signature
    )


def _message(item):
    return {
        "id": item["key"],
        "message_id": item.get("message_id") or "",
        "sender": item.get("sender") or "",
        "sender_id": item.get("sender_id") or "",
        "conversation_id": item.get("conversation_id") or "",
        "sources": sorted(item.get("sources") or []),
        "text": item.get("text") or "",
        "time": item.get("time") or "",
    }


def build_payload(messages, draft, needs_reply, event="message.triaged"):
    """Describe the latest message of a conversation batch together with its AI triage."""
    latest = messages[-1]
    sources = sorted({source for item in messages for source in item.get("sources") or []})
    return {
        "event": event,
        "delivery_id": uuid.uuid4().hex,
        "timestamp": now_text(),
        "message": _message(latest),
        "batch": [_message(item) for item in messages],
        "classification": {
            "sources": sources,
            "kind": "direct" if "direct" in sources else "at" if "at" in sources else "group",
            "needs_reply": bool(needs_reply),
        },
        "summary": draft.get("summary") or "",
        "suggested_reply": draft.get("text") or "",
        "draft": {"id": draft.get("id"), "version": draft.get("version")},
        "actions": {
            "reply": "/messages/" + quote(latest["key"], safe="") + "/reply",
            "dismiss": "/messages/" + quote(latest["key"], safe="") + "/dismiss",
        },
    }


def matches(settings, payload):
    rule = settings["filter"]
    if rule["needs_reply_only"] and not payload["classification"]["needs_reply"]:
        return False
    return bool(set(payload["classification"]["sources"]) & set(rule["sources"]))


def log(text):
    print("[hooks] {} {}".format(now_text(), text), file=sys.stderr, flush=True)


class HookSender:
    """Holds the single target, persists edits back to the config file and posts in threads."""

    def __init__(self, settings=None, config_path=None, post=None, sleep=time.sleep):
        self.lock = threading.Lock()
        self.settings = validate_hooks(settings)
        self.config_path = Path(config_path) if config_path else None
        self.post = post or self._post
        self.sleep = sleep
        self.last = None

    def public(self):
        with self.lock:
            value = dict(self.settings, filter=dict(self.settings["filter"]))
            last = dict(self.last) if self.last else None
        secret = value.pop("secret")
        value.update(secret_set=bool(secret), last=last, persisted=bool(self.config_path))
        return value

    def merged(self, changes):
        """Merge panel input; a blank secret keeps the saved one so it is never echoed back."""
        if not isinstance(changes, dict):
            raise ValueError("hooks 设置格式不正确")
        with self.lock:
            current = dict(self.settings)
        value = dict(current)
        for key in ["enabled", "url", "filter", "timeout_seconds", "retries"]:
            if key in changes:
                value[key] = changes[key]
        if isinstance(changes.get("secret"), str) and changes["secret"].strip():
            value["secret"] = changes["secret"]
        return validate_hooks(value)

    def update(self, changes):
        value = self.merged(changes)
        if self.config_path:
            persist(self.config_path, value)
        with self.lock:
            self.settings = value
        log("设置已更新：enabled={} url={}".format(value["enabled"], value["url"] or "-"))
        return self.public()

    def notify(self, messages, draft, needs_reply):
        """Queue delivery for a triaged batch when enabled and matching. Never raises."""
        try:
            with self.lock:
                settings = dict(self.settings)
            if not settings["enabled"]:
                return None
            payload = build_payload(messages, draft, needs_reply)
            if not matches(settings, payload):
                return None
            thread = threading.Thread(target=self.deliver, args=(payload, settings), daemon=True)
            thread.start()
            return thread
        except Exception as error:
            log("准备投递失败：" + str(error)[:200])
            return None

    def send_test(self, changes=None):
        """Post a synthetic message once, using unsaved panel input when provided."""
        settings = self.merged(changes or {})
        if not settings["url"] or not settings["secret"]:
            raise ValueError("请先填写地址和签名密钥")
        fake = {
            "key": "test:" + uuid.uuid4().hex[:12],
            "message_id": "test-message",
            "conversation_id": "test-conversation",
            "sender": "测试消息",
            "sender_id": "test-sender",
            "time": now_text(),
            "text": "这是 dingtalk-ears 发出的测试消息，不是真实钉钉消息。",
            "sources": ["direct"],
        }
        draft = {"id": None, "version": None, "summary": "测试投递", "text": "收到，测试成功。"}
        payload = build_payload([fake], draft, True, event="hook.test")
        return self.deliver(payload, dict(settings, retries=0))

    def deliver(self, payload, settings):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        attempts = settings["retries"] + 1
        error = None
        status = None
        for attempt in range(1, attempts + 1):
            timestamp = str(int(time.time()))
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "dingtalk-ears-hooks/1",
                "X-Ears-Event": payload["event"],
                "X-Ears-Delivery": payload["delivery_id"],
                TIMESTAMP_HEADER: timestamp,
                SIGNATURE_HEADER: sign(settings["secret"], timestamp, body),
            }
            try:
                status = self.post(settings["url"], body, headers, settings["timeout_seconds"])
                if 200 <= status < 300:
                    error = None
                    break
                error = "HTTP {}".format(status)
            except Exception as failure:
                error = type(failure).__name__ + ": " + str(failure)[:200]
            log(
                "投递失败 {}/{} delivery={} event={}：{}".format(
                    attempt, attempts, payload["delivery_id"], payload["event"], error
                )
            )
            if attempt < attempts:
                self.sleep(min(2 ** (attempt - 1), 10))
        result = {
            "ok": error is None,
            "status": status,
            "attempts": attempt,
            "error": error,
            "event": payload["event"],
            "delivery_id": payload["delivery_id"],
            "message_id": payload["message"]["id"],
            "time": now_text(),
        }
        if error is None:
            log(
                "已投递 delivery={} event={} status={}".format(
                    payload["delivery_id"], payload["event"], status
                )
            )
        with self.lock:
            self.last = result
        return result

    @staticmethod
    def _post(url, body, headers, timeout):
        try:
            with urlopen(
                Request(url, data=body, headers=headers, method="POST"), timeout=timeout
            ) as response:
                response.read(65536)
                return response.status
        except HTTPError as error:
            return error.code


def persist(path, hooks):
    """Replace only the hooks section of the owner-readable config file."""
    from .config import _unique_object

    path = Path(path)
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    value["hooks"] = hooks
    tmp = path.with_name(path.name + ".hooks.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")
    os.replace(str(tmp), str(path))
