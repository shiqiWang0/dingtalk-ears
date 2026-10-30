"""Unwrap IM events, filter identity/scope and derive stable deduplication keys."""

import hashlib
import json

EVENTS = {
    "direct": "user_im_message_receive_o2o_all",
    "at": "user_im_message_receive_at",
    "ued": "user_im_message_receive_group",
}


def normalize(raw, source, self_id, group_ids=()):
    group_ids = set(group_ids)
    group_source = source == "ued" or source.startswith("group-")
    if (source not in EVENTS and not group_source) or not isinstance(raw, dict):
        return None
    value = raw
    for unused in range(5):
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            return None
        kind = str(value.get("type", ""))
        if kind.startswith("user_") and kind not in EVENTS.values():
            return None
        if "data" in value:
            value = value["data"]
        elif isinstance(value.get("payload"), dict):
            value = value["payload"]
        elif isinstance(value.get("body"), dict):
            value = value["body"]
        else:
            break
    if not isinstance(value, dict):
        return None
    cid = str(value.get("conversation_id") or value.get("conversationId") or "")
    sid = str(value.get("sender_open_dingtalk_id") or value.get("senderOpenDingtalkId") or "")
    if sid == self_id or (group_source and cid not in group_ids):
        return None
    text = value.get("content")
    if text is None:
        text = value.get("text", "")
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    mid = str(value.get("message_id") or value.get("messageId") or "")
    timestamp = (
        value.get("create_time")
        or value.get("createTime")
        or value.get("event_time")
        or raw.get("timestamp")
        or ""
    )
    if not cid or not (text or mid):
        return None
    key = (
        cid + ":" + mid
        if mid
        else hashlib.sha256(
            json.dumps([cid, sid, timestamp, text], ensure_ascii=False).encode()
        ).hexdigest()
    )
    return {
        "key": key,
        "message_id": mid,
        "conversation_id": cid,
        "sender": value.get("sender") or sid or "发送人未提供",
        "sender_id": sid,
        "time": timestamp,
        "text": text or "[非文本消息，需另行查看]",
        "sources": [source],
        "quoted_message": value.get("quoted_message") or value.get("quotedMessage"),
    }
