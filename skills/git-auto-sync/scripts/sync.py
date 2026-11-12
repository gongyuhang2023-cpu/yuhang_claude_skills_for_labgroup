#!/usr/bin/env python3
"""Git Auto Sync: explicit GitHub/Gitea targets using the current user's Git."""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

GITEA_HOST = "git.denglab.xyz"


def git(*args, cwd=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never",
               GIT_OPTIONAL_LOCKS="0")
    return subprocess.run(["git", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def platform_for(url):
    if "://" in url:
        host = urlsplit(url).hostname
    else:
        match = re.match(r"(?:[^@/:]+@)?([^/:]+):", url)
        host = match.group(1) if match else None
    return {"github.com": "github", GITEA_HOST: "gitea"}.get(
        (host or "").lower(), "unknown")


def display_url(url):
    # A URL should not contain a token, but never echo one if it does.
    return re.sub(r"(https?://)[^/@]+@", r"\1***@", url)


def remotes_at(root):
    result = []
    for name in git("remote", cwd=root).stdout.splitlines():
        fetch = git("config", "--get-all", f"remote.{name}.url", cwd=root).stdout.splitlines()
        push = git("config", "--get-all", f"remote.{name}.pushurl", cwd=root).stdout.splitlines()
        urls = push or fetch
        result.append({"name": name, "url": fetch[0] if fetch else "",
                       "push_urls": urls,
                       "platform": platform_for(urls[0]) if len(urls) == 1 else "unknown"})
    return result


def choose_targets(root, remotes, to=None):
    """Resolve every destination before any staging/commit; ambiguity is an error."""
    configured = git("config", "--local", "--get", "sync.targets", cwd=root).stdout.strip()
    by_name = {remote["name"]: remote for remote in remotes}
    if to:
        wanted = "gitea" if to == "denglab" else to
        names = [remote["name"] for remote in remotes if remote["platform"] == wanted]
        if len(names) != 1:
            raise ValueError(f"找到 {len(names)} 个 {wanted} 远程，无法唯一选择。"
                             "请核对 --detect，并用 git config --local sync.targets 选择实际 remote 名。")
    elif configured:
        names = configured.split()
    elif len(remotes) == 1:
        names = [remotes[0]["name"]]
    elif not remotes:
        raise ValueError("没有远程仓库。请先按 references/gitea-first-use.md 配置远程。")
    else:
        raise ValueError("有多个 remote，但未设置 sync.targets。请先选择日常目标，"
                         "或用 --to gitea / --to github 只推一次。")
    names = list(dict.fromkeys(names))
    for name in names:
        if name not in by_name:
            raise ValueError(f"sync.targets 指定的 remote 不存在：{name}；尚未暂存或提交。")
        if len(by_name[name]["push_urls"]) != 1:
            raise ValueError(f"remote {name} 有零个或多个推送地址，请先整理为一个明确地址；尚未暂存或提交。")
    return names, configured


def detect(root, remotes):
    configured = git("config", "--local", "--get", "sync.targets", cwd=root).stdout.strip()
    try:
        targets, _ = choose_targets(root, remotes)
        error = None
    except ValueError as exc:
        targets, error = [], str(exc)
    safe = [{**remote, "url": display_url(remote["url"]),
             "push_urls": [display_url(url) for url in remote["push_urls"]]}
            for remote in remotes]
    print(json.dumps({
        "is_git_repo": True, "repo_path": str(root), "repo_name": root.name,
        "branch": git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=root).stdout.strip() or None,
        "has_changes": bool(git("status", "--porcelain", cwd=root).stdout),
        "remotes": safe, "platforms": sorted({r["platform"] for r in remotes} - {"unknown"}),
        "sync_targets": targets, "sync_targets_configured": bool(configured),
        "target_error": error,
    }, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="提交并推送到选定远程；安装技能不会自动执行此命令。",
        epilog="无选项时会 add、commit、push。持久目标用 git config --local sync.targets 'remote名'；"
               "本共享版不自动创建远程仓库。")
    parser.add_argument("message", nargs="?", help="提交说明（含空格时整体加引号）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--detect", action="store_true", help="只读查看分支、变更、远程及默认目标")
    mode.add_argument("--push-only", action="store_true", help="只推已有提交，不暂存、不提交")
    parser.add_argument("--to", choices=("gitea", "github", "denglab"),
                        help="只推指定平台；denglab 是 gitea 的兼容别名；不改日常目标")
    args = parser.parse_args(argv)
    if args.detect and (args.to or args.message is not None):
        parser.error("--detect 不能与 --to 或提交说明组合")
    if args.push_only and args.message is not None:
        parser.error("--push-only 不能带提交说明")

    found = git("rev-parse", "--show-toplevel")
    if found.returncode:
        if args.detect:
            print(json.dumps({"is_git_repo": False}))
            return 0
        print("✗ 当前目录不是 Git 仓库；请先确定项目根目录并初始化。", file=sys.stderr)
        return 1
    root = Path(found.stdout.strip())
    remotes = remotes_at(root)
    if args.detect:
        detect(root, remotes)
        return 0

    try:
        targets, _ = choose_targets(root, remotes, args.to)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=root).stdout.strip()
    if not branch:
        print("✗ 当前处于 detached HEAD，请先选择要推送的分支；尚未暂存或提交。", file=sys.stderr)
        return 1
    if git("diff", "--name-only", "--diff-filter=U", cwd=root).stdout.strip():
        print("✗ 有未解决的合并冲突；尚未暂存或提交。", file=sys.stderr)
        return 1
    print(f"仓库: {root}\n分支: {branch}\n目标: {' + '.join(targets)}")
    if not args.push_only and git("status", "--porcelain", cwd=root).stdout:
        added = git("add", "--all", cwd=root)
        if added.returncode:
            print(added.stderr.strip(), file=sys.stderr)
            return 1
        message = args.message or f"自动同步于 {datetime.now():%Y-%m-%d %H:%M:%S}"
        committed = git("commit", "-m", message, cwd=root)
        if committed.returncode:
            print((committed.stderr or committed.stdout).strip(), file=sys.stderr)
            return 1
        print(f"✓ 已提交: {message}")
    elif not args.push_only:
        print("无新变更，推送已有提交。")
    if git("rev-parse", "--verify", "HEAD", cwd=root).returncode:
        print("✗ 还没有可推送的提交。", file=sys.stderr)
        return 1
    success = True
    for remote in targets:
        pushed = git("push", "--", remote, f"HEAD:refs/heads/{branch}", cwd=root)
        if pushed.returncode:
            success = False
            print(f"✗ {remote}: 推送失败\n{pushed.stderr.strip()}", file=sys.stderr)
        else:
            print(f"✓ 已推送: {remote}/{branch}")
    print("同步完成。" if success else "同步未全部完成；已成功的远程保持不动，请处理失败项后重试。")
    return 0 if success else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except OSError as exc:
        print(f"✗ 无法运行 Git：{exc}", file=sys.stderr)
        sys.exit(1)
