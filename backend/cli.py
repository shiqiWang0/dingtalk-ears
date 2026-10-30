"""Explicit, scriptable commands for setup, diagnostics, running and sharing."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .config import initialize, load_config, migrate_legacy_config
from .diagnostics import doctor, resolve_commands
from .distribution import create_archive, launch_agent, save_launch_agent


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="钉钉消息助手：仅审查确认后发送")
    parser.add_argument(
        "--config", default="config.local.json", help="配置文件；相对路径以此文件为基准"
    )
    commands = parser.add_subparsers(dest="command")
    init = commands.add_parser("init", help="创建占位配置，不覆盖已有文件")
    init.add_argument("--output", default="config.local.json")
    commands.add_parser("doctor", help="检查依赖、登录、身份、路径和端口，不创建订阅")
    run = commands.add_parser("run", help="启动三个监听、Codex App Server 和本地面板")
    run.add_argument(
        "--adopt-state", action="store_true", help="核对备份后，一次性认领无身份标记的旧数据库"
    )
    share = commands.add_parser("share", help="仅按白名单打包可分发源码")
    share.add_argument("--output", required=True)
    service = commands.add_parser("service", help="生成 macOS 登录启动项；默认只生成文件")
    service.add_argument("--output", default="launchagent.local.plist")
    service.add_argument("--python", default=sys.executable)
    service.add_argument(
        "--install", action="store_true", help="安装到本人 LaunchAgents 并加载；已有服务需先停止"
    )
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        if args.command == "init":
            initialize(args.output)
            print("已创建配置，请填写本人身份和群 ID：" + str(Path(args.output).resolve()))
        elif args.command == "share":
            print(create_archive(Path(__file__).resolve().parent.parent, args.output))
        else:
            migrate_legacy_config(args.config)
            config = load_config(args.config)
            if args.command == "doctor":
                checks = doctor(config)
                print(
                    json.dumps(
                        {
                            "ok": all(
                                item["ok"] or item.get("level") == "warning" for item in checks
                            ),
                            "checks": checks,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return (
                    0 if all(item["ok"] or item.get("level") == "warning" for item in checks) else 1
                )
            if args.command == "service":
                if sys.platform != "darwin":
                    raise ValueError("service 命令仅适用于 macOS；Linux 请前台运行或自建用户服务")
                resolve_commands(config)
                data = launch_agent(args.config, args.python)
                output = Path(args.output).expanduser().resolve()
                save_launch_agent(output, data)
                print("已生成：" + str(output))
                if args.install:
                    domain = "gui/" + str(os.getuid())
                    running = subprocess.run(
                        ["launchctl", "print", domain + "/com.codex.dingtalk-ears"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    if running.returncode == 0:
                        raise ValueError(
                            "服务已加载；请先 launchctl bootout "
                            + domain
                            + "/com.codex.dingtalk-ears 再安装"
                        )
                    config.state_dir.mkdir(parents=True, exist_ok=True)
                    target = Path.home() / "Library/LaunchAgents/com.codex.dingtalk-ears.plist"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(output.read_bytes())
                    subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
                    print("已安装并加载：" + str(target))
            elif args.command == "run":
                from .host import Host

                Host(resolve_commands(config), adopt_state=args.adopt_state).run()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print("错误：" + str(error), file=sys.stderr)
        return 1
    return 0
