"""Read-only dependency and identity checks. Never emit authentication payloads."""

import json
import os
import shutil
import socket
import subprocess
from dataclasses import replace


def resolve_commands(config):
    found = {}
    # 根据当前配置的 ai_provider 动态检查所需命令
    required = ["dws", config.ai_provider]
    for name in required:
        command = getattr(config, name)
        executable = shutil.which(command)
        if not executable:
            raise ValueError(
                "找不到 {}，请安装 CLI 或在 commands.{} 指定可执行文件路径".format(name, name)
            )
        found[name] = executable
    return replace(config, **found)


def _find(value, key):
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            result = _find(child, key)
            if result is not None:
                return result
    elif isinstance(value, list):
        for child in value:
            result = _find(child, key)
            if result is not None:
                return result
    return None


def doctor(config):
    """Check the selected account, CLI authentication and local prerequisites."""
    checks = []

    def record(name, ok, detail, level="error"):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "level": level})

    try:
        config = resolve_commands(config)
    except ValueError as error:
        record("CLI", False, str(error))
        return checks

    def run(args):
        return subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=45,
        )

    try:
        status = run(
            [config.dws, "auth", "status", "--profile", config.profile, "--format", "json"]
        )
        data = json.loads(status.stdout)
        authenticated = status.returncode == 0 and _find(data, "authenticated") is True
        record(
            "钉钉登录",
            authenticated,
            "目标 profile 已认证" if authenticated else "运行 dws auth login 后核对目标 profile",
        )
        if authenticated:
            user = run(
                [
                    config.dws,
                    "contact",
                    "user",
                    "get-self",
                    "--profile",
                    config.profile,
                    "--format",
                    "json",
                ]
            )
            identity = json.loads(user.stdout)
            user_id = _find(identity, "userId")
            matched = user.returncode == 0 and str(user_id) == config.profile.split(":")[1]
            record(
                "钉钉本人",
                matched,
                "本人 userId 与 profile 一致" if matched else "无法核验本人，请检查 profile 和授权",
            )
            open_id = _find(identity, "openDingTalkId") or _find(data, "openDingTalkId")
            if open_id:
                record("本人 OpenID", open_id == config.self_id, "核对配置 self_id 与当前身份")
            else:
                record(
                    "本人 OpenID",
                    False,
                    "接口未返回 openDingTalkId；self_id 需本人手动核验，不能用员工 userId 代替",
                    level="warning",
                )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        record(
            "钉钉登录", False, "无法完成认证检查；在终端运行 dws auth status 核对，不输出认证原文"
        )
    if config.ai_provider == "agy":
        try:
            result = run([config.agy, "models"])
            record(
                "agy 登录",
                result.returncode == 0,
                "已连接可用模型" if result.returncode == 0 else "运行 agy 登录并检查网络",
            )
        except (OSError, subprocess.TimeoutExpired):
            record("agy 登录", False, "无法运行 agy 命令")
    else:
        try:
            result = run([config.codex, "login", "status"])
            record(
                "Codex 登录",
                result.returncode == 0,
                "已登录" if result.returncode == 0 else "运行 codex login",
            )
        except (OSError, subprocess.TimeoutExpired):
            record("Codex 登录", False, "无法运行 codex login status")
    for name, directory in [("状态目录", config.state_dir), ("输出目录", config.output_dir)]:
        ancestor = directory
        while not ancestor.exists():
            ancestor = ancestor.parent
        record(name, ancestor.is_dir() and os.access(str(ancestor), os.W_OK), "检查配置路径可写性")
    marker = config.state_dir / "identity.json"
    if marker.exists():
        try:
            owner = json.loads(marker.read_text(encoding="utf-8"))
            same = owner.get("profile") == config.profile and owner.get("self_id") == config.self_id
            record(
                "状态归属", same, "身份匹配" if same else "此目录属于另一身份，请配置独立数据目录"
            )
        except (OSError, ValueError):
            record("状态归属", False, "identity.json 无法读取")
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", config.port))
        record("端口", True, "可用")
    except OSError:
        record("端口", False, "端口不可绑定：检查已有实例或本机权限")
    return checks
