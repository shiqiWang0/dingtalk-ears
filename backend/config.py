"""Validated settings, config-relative paths and per-account state ownership."""

import json
import os
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

EXAMPLE = {
    "profile": "填写corpId:userId",
    "self_id": "填写本人openDingTalkId",
    "display_name": "我的名字",
    "groups": [],
    "server": {"port": 8766},
    "retention": {"days": 7},
    "paths": {"state_dir": "./data", "output_dir": "./data/output"},
    "commands": {"dws": "dws", "codex": "codex", "agy": "agy"},
    "ai_provider": "codex",
    "ai_model": "gemini-3.8-flash-medium",
    "batch": {"direct_seconds": 30, "group_seconds": 300, "trigger_count": 20, "max_messages": 50},
}


@dataclass(frozen=True)
class Settings:
    profile: str
    self_id: str
    display_name: str
    config_path: Path
    state_dir: Path
    output_dir: Path
    port: int = 8766
    dws: str = "dws"
    codex: str = "codex"
    agy: str = "agy"
    ai_provider: str = "codex"
    ai_model: str = "gemini-3.8-flash-medium"
    direct_seconds: int = 30
    group_seconds: int = 300
    trigger_count: int = 20
    max_messages: int = 50
    groups: tuple = ()
    retention_days: int = 7

    @property
    def panel_url(self):
        return "http://127.0.0.1:{}".format(self.port)

    @property
    def watched_groups(self):
        return self.groups

    @property
    def group_name(self):
        return "、".join(name for unused, name in self.groups) or "关注群"

    @property
    def group_ids(self):
        return tuple(group_id for group_id, unused in self.watched_groups)


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


def _groups(value):
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("groups 必须是数组")
    groups, ids = [], set()
    for index, item in enumerate(value):
        item = _object(item, {"id", "name"}, "groups[{}]".format(index))
        group_id = _text(item.get("id"), "groups[{}].id".format(index))
        if group_id in ids:
            raise ValueError("groups 不能包含重复群 ID")
        ids.add(group_id)
        groups.append((group_id, _text(item.get("name"), "groups[{}].name".format(index))))
    return tuple(groups)


def load_config(path):
    """Load settings without starting processes, making requests or writing state."""
    path = Path(path).expanduser().resolve()
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    _object(value, EXAMPLE, "配置")
    sections = {}
    for key in ["server", "retention", "paths", "commands", "batch"]:
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

    ai_provider = _text(value.get("ai_provider", "codex"), "ai_provider").lower()
    if ai_provider not in ["codex", "agy"]:
        raise ValueError("ai_provider 必须是 codex 或 agy")
    ai_model = _text(value.get("ai_model", "gemini-3.8-flash-medium"), "ai_model")

    def config_path(key):
        raw = _text(sections["paths"].get(key, EXAMPLE["paths"][key]), "paths." + key)
        return (path.parent / Path(raw).expanduser()).resolve()

    def command_path(key, default=None):
        raw = _text(sections["commands"].get(key, default or key), "commands." + key)
        if "/" in raw or raw.startswith("~"):
            return str((path.parent / Path(raw).expanduser()).resolve())
        return raw

    return Settings(
        profile=profile,
        self_id=_text(value.get("self_id"), "self_id"),
        display_name=_text(value.get("display_name"), "display_name"),
        config_path=path,
        groups=_groups(value.get("groups", [])),
        retention_days=_integer(sections["retention"].get("days", 7), "retention.days", 1, 3650),
        port=_integer(sections["server"].get("port", 8766), "server.port", high=65535),
        state_dir=config_path("state_dir"),
        output_dir=config_path("output_dir"),
        dws=command_path("dws"),
        codex=command_path("codex"),
        agy=command_path("agy", "agy"),
        ai_provider=ai_provider,
        ai_model=ai_model,
        **batch,
    )


_CONFIG_LOCK = threading.Lock()


def configured_groups(path):
    """Return local-panel-safe group metadata from the current configuration."""
    config = load_config(path)
    return [
        {"id": group_id, "name": name}
        for group_id, name in config.watched_groups
    ]


def _write_config(path, value):
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except Exception:
        os.unlink(temporary)
        raise


def migrate_legacy_config(path):
    """Move a legacy single group into the unified groups list before startup."""
    path = Path(path).expanduser().resolve()
    with _CONFIG_LOCK:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        legacy = value.pop("group", None)
        if legacy is None:
            return False
        legacy = _object(legacy, {"id", "name"}, "group")
        group_id = _text(legacy.get("id"), "group.id")
        name = _text(legacy.get("name"), "group.name")
        groups = list(value.get("groups", []))
        if not any(isinstance(item, dict) and item.get("id") == group_id for item in groups):
            groups.insert(0, {"id": group_id, "name": name})
        value["groups"] = groups
        _write_config(path, value)
        return True


def add_group(path, group_id, name):
    with _CONFIG_LOCK:
        config = load_config(path)
        group_id = _text(group_id, "群 ID")
        name = _text(name, "群名称")
        if group_id in config.group_ids:
            raise ValueError("该群已在关注列表中")
        value = json.loads(config.config_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        groups = list(value.get("groups", []))
        groups.append({"id": group_id, "name": name})
        value["groups"] = groups
        _write_config(config.config_path, value)
        return configured_groups(config.config_path)


def remove_group(path, group_id):
    with _CONFIG_LOCK:
        config = load_config(path)
        group_id = _text(group_id, "群 ID")
        value = json.loads(config.config_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        groups = [item for item in value.get("groups", []) if item.get("id") != group_id]
        if len(groups) == len(value.get("groups", [])):
            raise ValueError("关注群不存在")
        value["groups"] = groups
        _write_config(config.config_path, value)
        return configured_groups(config.config_path)


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
    owner = {"profile": config.profile, "self_id": config.self_id}
    marker = directory / "identity.json"
    if marker.exists():
        existing = json.loads(marker.read_text(encoding="utf-8"))
        if existing.get("profile") != config.profile or existing.get("self_id") != config.self_id:
            raise ValueError("数据目录属于其他账号或群，请配置独立的 state_dir")
        if existing != owner:
            marker.write_text(json.dumps(owner, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return
    if not adopt and any((directory / name).exists() for name in ["queue.db", "panel.db"]):
        raise ValueError("旧数据库没有身份标记；备份核对后使用 run --adopt-state 认领一次")
    fd = os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(owner, output, ensure_ascii=False, indent=2)
        output.write("\n")
