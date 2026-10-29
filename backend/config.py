"""Validated settings, config-relative paths and per-account state ownership."""

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .hooks import DEFAULTS as HOOK_DEFAULTS
from .hooks import validate_hooks

EXAMPLE = {
    "profile": "填写corpId:userId",
    "self_id": "填写本人openDingTalkId",
    "display_name": "我的名字",
    "group": {"id": "填写群openConversationId", "name": "关注群"},
    "server": {"port": 8766},
    "paths": {"state_dir": "./data", "output_dir": "./data/output"},
    "commands": {"dws": "dws", "codex": "codex"},
    "batch": {"direct_seconds": 30, "group_seconds": 300, "trigger_count": 20, "max_messages": 50},
    "hooks": copy.deepcopy(HOOK_DEFAULTS),
}


@dataclass(frozen=True)
class Settings:
    profile: str
    self_id: str
    display_name: str
    group_id: str
    group_name: str
    state_dir: Path
    output_dir: Path
    port: int = 8766
    dws: str = "dws"
    codex: str = "codex"
    direct_seconds: int = 30
    group_seconds: int = 300
    trigger_count: int = 20
    max_messages: int = 50
    hooks: dict = None
    config_path: Path = None

    @property
    def panel_url(self):
        return "http://127.0.0.1:{}".format(self.port)


def _object(value, allowed, label):
    if not isinstance(value, dict):
        raise ValueError(label + " 必须是对象")
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError(label + " 含未知字段: " + ", ".join(sorted(unknown)))
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + " 不能为空")
    value = value.strip()
    if any(marker in value for marker in ["填写", "替换", "<", ">", "\n", "\r", "\x00"]):
        raise ValueError(label + " 需要真实值，不能包含占位符或换行")
    return value


def _integer(value, label, low=1, high=86400):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("{} 必须是 {}–{} 的整数".format(label, low, high))
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 字段重复: " + key)
        result[key] = value
    return result


def load_config(path):
    """Load settings without starting processes, making requests or writing state."""
    path = Path(path).expanduser().resolve()
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    _object(value, EXAMPLE, "配置")
    sections = {}
    for key in ["group", "server", "paths", "commands", "batch"]:
        sections[key] = _object(value.get(key, {}), EXAMPLE[key], key)
    profile = _text(value.get("profile"), "profile")
    if len(profile.split(":")) != 2 or not all(profile.split(":")):
        raise ValueError("profile 需要 corpId:userId")
    batch = {
        key: _integer(sections["batch"].get(key, default), "batch." + key)
        for key, default in EXAMPLE["batch"].items()
    }
    if batch["trigger_count"] > batch["max_messages"] or batch["max_messages"] > 500:
        raise ValueError("trigger_count 不能大于 max_messages，max_messages 不能超过 500")

    def config_path(key):
        raw = _text(sections["paths"].get(key, EXAMPLE["paths"][key]), "paths." + key)
        return (path.parent / Path(raw).expanduser()).resolve()

    def command_path(key):
        raw = _text(sections["commands"].get(key, key), "commands." + key)
        if "/" in raw or raw.startswith("~"):
            return str((path.parent / Path(raw).expanduser()).resolve())
        return raw

    return Settings(
        profile=profile,
        self_id=_text(value.get("self_id"), "self_id"),
        display_name=_text(value.get("display_name"), "display_name"),
        group_id=_text(sections["group"].get("id"), "group.id"),
        group_name=_text(sections["group"].get("name"), "group.name"),
        port=_integer(sections["server"].get("port", 8766), "server.port", high=65535),
        state_dir=config_path("state_dir"),
        output_dir=config_path("output_dir"),
        dws=command_path("dws"),
        codex=command_path("codex"),
        hooks=validate_hooks(value.get("hooks")),
        config_path=path,
        **batch,
    )


def initialize(path):
    """Create an owner-readable template; never overwrite existing settings."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(EXAMPLE, output, ensure_ascii=False, indent=2)
        output.write("\n")


def bind_state(config, adopt=False):
    """Prevent a different identity from silently inheriting replies or threads."""
    directory = config.state_dir
    directory.mkdir(parents=True, exist_ok=True)
    owner = {"profile": config.profile, "self_id": config.self_id, "group_id": config.group_id}
    marker = directory / "identity.json"
    if marker.exists():
        if json.loads(marker.read_text(encoding="utf-8")) != owner:
            raise ValueError("数据目录属于其他账号或群，请配置独立的 state_dir")
        return
    if not adopt and any((directory / name).exists() for name in ["queue.db", "panel.db"]):
        raise ValueError("旧数据库没有身份标记；备份核对后使用 run --adopt-state 认领一次")
    fd = os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(owner, output, ensure_ascii=False, indent=2)
        output.write("\n")
