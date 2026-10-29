"""Generate user-specific launch agents and clean source distributions."""

import os
import plistlib
import sys
import tarfile
from pathlib import Path

from .config import load_config

ROOT_FILES = {
    "AGENTS.md",
    "README.md",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "requirements-dev.txt",
    "config.example.json",
    "ears.py",
    ".gitignore",
    ".editorconfig",
    ".prettierignore",
    "prettier.config.cjs",
    "stylelint.config.cjs",
    "eslint.config.mjs",
}
DOC_FILES = {"安装配置与启动.md", "架构说明.md", "外部Hooks.md"}


def create_archive(root, output):
    """Ship source only. Reject links rather than following arbitrary targets."""
    root, output = Path(root).resolve(), Path(output).resolve()
    candidates = [root / name for name in sorted(ROOT_FILES) if (root / name).exists()]
    for directory, suffixes in [
        ("backend", {".py", ".html", ".js", ".css", ".sql"}),
        ("tests", {".py"}),
        ("scripts", {".py"}),
    ]:
        base = root / directory
        if base.is_symlink():
            raise ValueError("分享目录不能是符号链接: " + directory)
        if base.exists():
            candidates.extend(
                path
                for path in base.rglob("*")
                if path.suffix in suffixes and "__pycache__" not in path.parts
            )
    candidates.extend(
        root / "docs" / name for name in sorted(DOC_FILES) if (root / "docs" / name).exists()
    )
    for path in candidates:
        if path.is_symlink() or any(
            parent.is_symlink() for parent in path.parents if parent != root.parent
        ):
            raise ValueError("分享文件不能是符号链接: " + str(path))
        if output == path.resolve():
            raise ValueError("归档输出不能覆盖项目文件")
    with tarfile.open(str(output), "x:gz") as bundle:
        for path in sorted(candidates):
            if path.is_file():
                bundle.add(
                    str(path),
                    arcname="dingtalk-ears/" + path.relative_to(root).as_posix(),
                    recursive=False,
                )
    return output


def launch_agent(config_path, python=None, environment=None):
    """Return plist data only; installation requires an explicit CLI action."""
    config_path = Path(config_path).expanduser().resolve()
    config = load_config(config_path)
    environment = os.environ if environment is None else environment
    python_path = Path(python or sys.executable).expanduser().absolute()
    if not python_path.is_file() or not os.access(str(python_path), os.X_OK):
        raise ValueError("Python 路径不是可执行文件")
    python = str(python_path)
    project = Path(__file__).resolve().parent.parent
    env = {
        "PATH": environment.get("PATH", os.defpath),
        "LANG": environment.get("LANG", "en_US.UTF-8"),
        "PYTHONUNBUFFERED": "1",
    }
    for key in [
        "DWS_CONFIG_DIR",
        "CODEX_HOME",
        "CODEX_CA_CERTIFICATE",
        "SSL_CERT_FILE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    ]:
        if environment.get(key):
            env[key] = environment[key]
    return {
        "Label": "com.codex.dingtalk-ears",
        "ProgramArguments": [
            "/usr/bin/caffeinate",
            "-s",
            python,
            "-m",
            "backend",
            "--config",
            str(config_path),
            "run",
        ],
        "WorkingDirectory": str(project),
        "RunAtLoad": True,
        "KeepAlive": False,
        "ExitTimeOut": 45,
        "ProcessType": "Background",
        "EnvironmentVariables": env,
        "StandardOutPath": str(config.state_dir / "host.stdout.log"),
        "StandardErrorPath": str(config.state_dir / "host.stderr.log"),
    }


def save_launch_agent(path, data):
    """Write a new generated plist without ever overwriting an existing file."""
    path = Path(path).expanduser().absolute()
    if path.suffix != ".plist":
        raise ValueError("启动项输出必须是新的 .plist 文件")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(plistlib.dumps(data))
