#!/usr/bin/env python3
"""Relay Lab Frontier commands using spark-advisor's SSH configuration."""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import socket
import subprocess
import sys


REMOTE = "/usr/local/bin/spark-frontier"


class ClientError(Exception):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def load_config():
    home = Path.home()
    skill_roots = (Path(__file__).absolute().parents[2], home / ".agents/skills",
                   home / ".claude/skills")
    for root in skill_roots:
        path = root / "spark-advisor" / "config.json"
        try:
            with path.open(encoding="utf-8") as stream:
                config = json.load(stream)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            raise ClientError("ssh_config_invalid", "无法读取 spark-advisor 的 config.json") from None
        if (not isinstance(config, dict) or
                any(not isinstance(config.get(key), str) or not config[key].strip()
                    for key in ("user", "host"))):
            raise ClientError("ssh_config_invalid", "spark-advisor 的 config.json 需要有效的 user 和 host")
        options = config.get("ssh_opts", [])
        if (not isinstance(options, list) or any(not isinstance(value, str) for value in options)
                or any(config.get(key) is not None and not isinstance(config[key], str)
                       for key in ("ssh_exe", "ssh_key", "host_lan"))):
            raise ClientError("ssh_config_invalid", "spark-advisor 的 SSH 选项格式有误")
        try:
            port = int(config.get("ssh_port", 22))
            if not 1 <= port <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            raise ClientError("ssh_config_invalid", "spark-advisor 的 ssh_port 无效") from None
        return config
    raise ClientError("ssh_config_missing", "请先按 spark-advisor 的「首次配置」完成连接设置")


def resolve_host(config):
    if lan := config.get("host_lan"):
        try:
            with socket.create_connection((lan, int(config.get("ssh_port", 22))), timeout=1):
                return lan
        except OSError:
            pass
    return config["host"]


def ssh_args(config, remote_args, *, interactive=False):
    # OpenSSH uses the first BatchMode setting it reads.
    batch_mode = "no" if interactive else "yes"
    args = [config.get("ssh_exe") or "ssh", "-o", f"BatchMode={batch_mode}"]
    args.extend(config.get("ssh_opts", []))
    args.extend(("-o", "ConnectTimeout=12"))
    if "ssh_port" in config:
        args.extend(("-p", str(config["ssh_port"])))
    if key := config.get("ssh_key"):
        args.extend(("-i", str(Path(key).expanduser())))
    args.extend(("-t",) if interactive else ("-T", "-n"))
    host = config["host"] if interactive else resolve_host(config)
    args.extend(("--", f"{config['user']}@{host}", shlex.join(remote_args)))
    return args


def terminal_command(args):
    if os.name == "nt":
        return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in args)
    return shlex.join(args)


def relay(config, remote_args, timeout):
    try:
        result = subprocess.run(ssh_args(config, [REMOTE, *remote_args]), stdin=subprocess.DEVNULL,
                                capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ClientError("ssh_timeout", "等待服务器结果超时，请先查状态") from None
    except OSError:
        raise ClientError("ssh_unavailable", "无法启动 SSH 客户端，请检查 spark-advisor 的连接设置") from None

    lines = result.stdout.rstrip().rsplit("\n", 1)
    line = lines[-1].strip()
    try:
        if isinstance(json.loads(line), dict):
            print(line)
            return result.returncode
    except (ValueError, RecursionError):
        pass
    if result.returncode == 255:
        raise ClientError("ssh_failed", "SSH 连接失败，请用 spark-advisor 核对连接设置")
    if result.returncode == 127:
        raise ClientError("frontier_unavailable", "服务器上找不到 spark-frontier，请联系维护者")
    raise ClientError("invalid_response", "服务器未在最后一行返回 JSON 对象")


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    commands.add_parser("setup").add_argument("--preset")
    commands.add_parser("config").add_argument("--preset", required=True)
    commands.add_parser("uninstall").add_argument("--purge", action="store_true")
    for name in ("status", "pause", "resume", "gitea-login"):
        commands.add_parser(name)
    commands.add_parser("codex-login").add_argument("--binary", default="codex")

    brief = commands.add_parser("brief").add_subparsers(dest="action", required=True)
    show = brief.add_parser("show")
    show.add_argument("--date", metavar="YYYY-MM-DD")
    show.add_argument("--json", action="store_true", help="JSON is always emitted")

    follow = commands.add_parser("follow").add_subparsers(dest="action", required=True)
    follow.add_parser("list")
    add = follow.add_parser("add")
    add.add_argument("owner")
    add.add_argument("date", metavar="YYYY-MM-DD")
    add.add_argument("item_id")
    set_item = follow.add_parser("set")
    set_item.add_argument("id")
    set_item.add_argument("active", choices=("true", "false"))

    profile = commands.add_parser("profile").add_subparsers(dest="action", required=True)
    profile.add_parser("show")
    profile.add_parser("note").add_argument("--person", required=True)
    for action in ("stop", "resume"):
        profile.add_parser(action).add_argument("project_id", metavar="OWNER/NAME")
    return cli


def main(argv=None):
    cli = parser()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    args = cli.parse_args(raw_args)
    if args.command == "codex-login" and args.binary != "codex":
        binary = PurePosixPath(args.binary)
        if not binary.is_absolute() or binary.name != "codex" or ".." in binary.parts:
            cli.error("--binary must be codex or an absolute path to codex")
    try:
        config = load_config()
        if args.command in {"gitea-login", "codex-login"}:
            remote = ([REMOTE, "gitea-login"] if args.command == "gitea-login"
                      else [args.binary, "login", "--device-auth"])
            print(terminal_command(ssh_args(config, remote, interactive=True)))
            return 0
        if args.command == "brief" and args.json:
            raw_args = ["brief", "show"]
            if args.date is not None:
                raw_args.extend(("--date", args.date))
        timeout = {"setup": 4 * 3600, "config": 3600}.get(args.command, 60)
        return relay(config, raw_args, timeout)
    except ClientError as error:
        print(json.dumps({"ok": False, "reason": error.reason, "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
