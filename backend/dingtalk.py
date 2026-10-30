"""The only DingTalk write boundary: reviewed replies and confirmed todos."""

import json
import subprocess


def list_joined_groups(config):
    result = subprocess.run(
        [config.dws, "chat", "+my-groups", "--page-all", "--profile", config.profile, "--format", "json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=30,
    )
    if result.returncode:
        raise ValueError("无法读取钉钉群列表，请检查登录状态")
    value = json.loads(result.stdout)
    if not value.get("complete"):
        raise ValueError("钉钉群列表未完整返回，请稍后重试")
    return [
        {"id": item["conversationId"], "name": item["name"]}
        for item in value.get("groups", [])
        if item.get("conversationId") and item.get("name")
    ]


def build_send_command(draft, message, text, config):
    cmd = [config.dws, "chat", "+messages-send", "--as", "user", "--profile", config.profile]
    if "direct" in message["sources"]:
        if not message.get("sender_id"):
            raise ValueError("原消息缺少发送人 ID，无法安全回复")
        cmd += ["--open-dingtalk-id", message["sender_id"]]
    else:
        if not message.get("conversation_id"):
            raise ValueError("原消息缺少会话 ID")
        cmd += ["--group", message["conversation_id"]]
    return cmd + [
        "--text",
        text,
        "--ai-tag=false",
        "--idempotency-key",
        draft["idem"],
        "--wait-delivery",
        "--yes",
        "--format",
        "json",
    ]


def deliver(store, draft, message, on_done=None):
    try:
        result = subprocess.run(
            build_send_command(draft, message, draft["text"], store.config),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=60,
        )
        if result.returncode:
            receipt = {
                "deliveryStatus": "unknown",
                "error": "发送未取得成功回执，请在钉钉核对；为避免重复，不会自动重发。",
            }
        else:
            receipt = json.loads(result.stdout)
    except Exception:
        receipt = {
            "deliveryStatus": "unknown",
            "error": "发送结果未确认，请在钉钉核对；不会自动重发。",
        }
    store.finish_send(draft["id"], receipt)
    if on_done:
        on_done("__overview__")


def create_todo(store, task, on_done=None):
    try:
        config = store.config
        cmd = [
            config.dws,
            "todo",
            "+remind",
            "--profile",
            config.profile,
            "--task",
            task["title"],
            "--yes",
            "--format",
            "json",
        ]
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60
        )
        receipt = (
            json.loads(result.stdout)
            if result.returncode == 0
            else {"error": "创建未核验，请在钉钉待办核对，不会自动重试"}
        )
    except Exception:
        receipt = {"error": "创建结果不明，请在钉钉待办核对，不会自动重试"}
    store.finish_task(task["id"], receipt)
    if on_done:
        on_done("__overview__")
