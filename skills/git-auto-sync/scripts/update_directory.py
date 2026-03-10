#!/usr/bin/env python3
"""
项目目录自动更新脚本 (generate_directory.py)

在 git commit 前自动扫描项目结构，更新项目目录.md文件。
遵循 Agent Skills 开放标准：
- 独立模块，可被其他技能复用
- 容错设计：失败时不阻止后续操作（退出码始终为0）
"""

import io
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Windows GBK 终端兼容：强制 stdout 使用 utf-8
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')


# 需要忽略的目录和文件
IGNORE_DIRS = {
    '.git', '.Rproj.user', 'node_modules', '__pycache__',
    '.venv', 'venv', '.idea', '.vscode', 'downloads'
}

IGNORE_FILES = {
    '.DS_Store', 'Thumbs.db', '.gitignore', '.Rhistory',
    '.RData'
}


def get_git_changes() -> dict:
    """获取 git status 信息，识别变更的文件。"""
    try:
        result = subprocess.run(
            ['git', 'status', '--porcelain'],
            capture_output=True, text=True, check=True
        )
        changes = {'added': [], 'modified': [], 'deleted': []}
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            status = line[:2]
            filename = line[3:]
            if 'A' in status or '?' in status:
                changes['added'].append(filename)
            elif 'M' in status:
                changes['modified'].append(filename)
            elif 'D' in status:
                changes['deleted'].append(filename)
        return changes
    except Exception:
        return {'added': [], 'modified': [], 'deleted': []}


def generate_change_summary(changes: dict) -> str:
    """生成变更摘要文本。"""
    if not any(changes.values()):
        return "目录结构同步"

    parts = []
    if changes['added']:
        parts.append(f"新增 {len(changes['added'])} 个文件")
    if changes['modified']:
        parts.append(f"修改 {len(changes['modified'])} 个文件")
    if changes['deleted']:
        parts.append(f"删除 {len(changes['deleted'])} 个文件")
    return "、".join(parts)


def update_directory_md(project_path: Path) -> bool:
    """
    更新项目目录.md文件。

    只更新时间戳和更新日志，不重新生成整个目录结构
    （目录结构由用户手动维护，脚本只记录更新时间）
    """
    directory_file = project_path / "项目目录.md"

    if not directory_file.exists():
        print("  ℹ 项目目录.md 不存在，跳过")
        return False

    try:
        # 读取现有内容
        content = directory_file.read_text(encoding='utf-8')

        # 获取 git 变更信息
        git_changes = get_git_changes()
        change_summary = generate_change_summary(git_changes)

        # 当前时间
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        # 1. 更新顶部时间戳
        content = re.sub(
            r'> 自动生成于 \d{4}-\d{2}-\d{2}.*',
            f'> 自动生成于 {now}',
            content
        )

        # 2. 在更新日志表格中添加新条目
        if "## 更新日志" in content:
            # 找到表格头部结束位置
            table_header_pattern = r'\| 日期 \| 更新内容 \|\n\|[-]+\|[-]+\|\n'
            match = re.search(table_header_pattern, content)

            if match:
                insert_pos = match.end()
                new_entry = f"| {now} | {change_summary} |\n"
                content = content[:insert_pos] + new_entry + content[insert_pos:]

        # 写回文件
        directory_file.write_text(content, encoding='utf-8')
        return True

    except Exception as e:
        print(f"  ⚠ 更新失败: {e}")
        return False


def main():
    """
    主函数。

    重要：无论成功与否，始终返回退出码 0
    这确保目录更新失败不会阻止后续的 git 操作
    """
    print("=== 项目目录更新 ===")

    try:
        cwd = Path.cwd()

        if not (cwd / "项目目录.md").exists():
            print("  ℹ 当前目录没有项目目录.md，跳过")
            sys.exit(0)  # 成功退出，不阻止后续操作

        if update_directory_md(cwd):
            print("  ✓ 项目目录.md 已更新")
        else:
            print("  ℹ 目录更新跳过")

    except Exception as e:
        # 捕获所有异常，打印错误但不阻止后续操作
        print(f"  ⚠ 发生错误: {e}")

    # 始终返回成功，确保不阻止 git 操作
    sys.exit(0)


if __name__ == "__main__":
    main()
