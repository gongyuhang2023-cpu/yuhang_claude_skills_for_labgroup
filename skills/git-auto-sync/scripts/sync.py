#!/usr/bin/env python3
"""
Git Auto Sync

一键完成 Git 的 add、commit 和 push 操作。
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path


def run_command(command: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """执行命令并返回结果。"""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=check
        )
        return result
    except subprocess.CalledProcessError as e:
        return e


def is_git_repo() -> bool:
    """检查当前目录是否为 Git 仓库。"""
    result = run_command(["git", "rev-parse", "--is-inside-work-tree"], check=False)
    return isinstance(result, subprocess.CompletedProcess) and result.returncode == 0


def has_changes() -> bool:
    """检查是否有需要提交的变更。"""
    result = run_command(["git", "status", "--porcelain"], check=False)
    return bool(result.stdout.strip()) if isinstance(result, subprocess.CompletedProcess) else False


def get_current_branch() -> str:
    """获取当前分支名。"""
    result = run_command(["git", "branch", "--show-current"], check=False)
    if isinstance(result, subprocess.CompletedProcess) and result.returncode == 0:
        return result.stdout.strip()
    return "unknown"


def git_add() -> tuple[bool, str]:
    """执行 git add .。"""
    result = run_command(["git", "add", "."], check=False)
    if isinstance(result, subprocess.CompletedProcess) and result.returncode == 0:
        return True, "✓ 已添加所有变更"
    return False, f"✗ git add 失败: {result.stderr}"


def git_commit(message: str) -> tuple[bool, str]:
    """执行 git commit。"""
    result = run_command(["git", "commit", "-m", message], check=False)
    if isinstance(result, subprocess.CompletedProcess):
        if result.returncode == 0:
            return True, f"✓ 已提交: {message}"
        elif "nothing to commit" in result.stdout:
            return False, "! 没有需要提交的变更"
    return False, f"✗ git commit 失败: {result.stderr if hasattr(result, 'stderr') else str(result)}"


def git_push() -> tuple[bool, str]:
    """执行 git push。"""
    result = run_command(["git", "push"], check=False)
    if isinstance(result, subprocess.CompletedProcess):
        if result.returncode == 0:
            return True, "✓ 已推送到远程仓库"

        stderr = result.stderr.lower()

        # 检查常见错误
        if "rejected" in stderr or "conflict" in stderr:
            return False, (
                "✗ 推送被拒绝：远程有更新\n"
                "  请手动执行以下命令解决：\n"
                "  1. git pull --rebase\n"
                "  2. 解决冲突（如有）\n"
                "  3. git push"
            )
        elif "no upstream" in stderr or "no configured push destination" in stderr:
            branch = get_current_branch()
            return False, (
                f"✗ 未设置上游分支\n"
                f"  请执行: git push -u origin {branch}"
            )
        elif "could not resolve" in stderr or "unable to access" in stderr:
            return False, "✗ 网络错误：请检查网络连接和远程仓库地址"

        return False, f"✗ git push 失败: {result.stderr}"

    return False, f"✗ git push 失败: {str(result)}"


def sync(message: str = None):
    """执行完整的 Git 同步流程。"""
    print("=== Git Auto Sync ===\n")

    # 1. 检查是否为 Git 仓库
    if not is_git_repo():
        print("✗ 当前目录不是 Git 仓库")
        print("  请先执行: git init")
        return False

    print(f"仓库: {Path.cwd()}")
    print(f"分支: {get_current_branch()}\n")

    # 2. 检查是否有变更
    if not has_changes():
        print("! 没有需要同步的变更")
        return True

    # 3. git add
    success, msg = git_add()
    print(msg)
    if not success:
        return False

    # 4. 生成或使用提交信息
    if not message:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message = f"Claude 自动同步于 {timestamp}"

    # 5. git commit
    success, msg = git_commit(message)
    print(msg)
    if not success:
        return False

    # 6. git push
    success, msg = git_push()
    print(msg)

    if success:
        print("\n========== 同步完成 ==========")
    else:
        print("\n========== 同步失败 ==========")
        print("请根据上述提示手动处理")

    return success


if __name__ == "__main__":
    # 获取可选的提交信息参数
    message = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else None

    success = sync(message)
    sys.exit(0 if success else 1)
