#!/usr/bin/env python3
"""元数据更新工具 - 增量写入 descriptions.json"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone


def load_descriptions(root: str) -> dict:
    desc_file = os.path.join(root, ".project-meta", "descriptions.json")
    if not os.path.exists(desc_file):
        return {
            "projectName": os.path.basename(os.path.abspath(root)),
            "projectDescription": "",
            "lastScan": "",
            "version": "1.0",
            "files": {},
            "folders": {},
        }
    with open(desc_file, "r", encoding="utf-8") as f:
        return json.load(f)


def save_descriptions(root: str, data: dict):
    desc_file = os.path.join(root, ".project-meta", "descriptions.json")
    os.makedirs(os.path.dirname(desc_file), exist_ok=True)
    with open(desc_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _rebuild_launcher(root)


def _rebuild_launcher(root: str):
    """JSON 写入后自动重新生成快捷查看器。"""
    try:
        from generate_launcher import generate
        generate(root)
    except Exception:
        try:
            script = os.path.join(os.path.dirname(__file__), "generate_launcher.py")
            import subprocess
            subprocess.run(
                [sys.executable, script, "--root", root],
                capture_output=True, timeout=15,
            )
        except Exception:
            pass


def update_descriptions(root: str, new_data: dict) -> dict:
    existing = load_descriptions(root)

    if "files" in new_data:
        for path, info in new_data["files"].items():
            existing["files"][path] = {
                **existing["files"].get(path, {}),
                **info,
            }

    if "folders" in new_data:
        for path, info in new_data["folders"].items():
            existing["folders"][path] = {
                **existing["folders"].get(path, {}),
                **info,
            }

    if "deleted" in new_data:
        for path in new_data["deleted"]:
            existing["files"].pop(path, None)
            existing["folders"].pop(path, None)

    if "projectName" in new_data:
        existing["projectName"] = new_data["projectName"]
    if "projectDescription" in new_data:
        existing["projectDescription"] = new_data["projectDescription"]

    existing["lastScan"] = datetime.now(timezone.utc).isoformat()

    save_descriptions(root, existing)
    return existing


def batch_tag(root: str, folder: str, tags: list[str], description: str) -> dict:
    existing = load_descriptions(root)

    existing["folders"][folder] = {
        **existing["folders"].get(folder, {}),
        "description": description,
        "tags": tags,
    }

    for file_path in list(existing["files"].keys()):
        normalized = file_path.replace("\\", "/")
        if normalized.startswith(folder.rstrip("/") + "/"):
            file_info = existing["files"][file_path]
            existing_tags = set(file_info.get("tags", []))
            existing_tags.update(tags)
            file_info["tags"] = sorted(existing_tags)
            existing["files"][file_path] = file_info

    existing["lastScan"] = datetime.now(timezone.utc).isoformat()
    save_descriptions(root, existing)
    return existing


def get_stats(root: str) -> dict:
    existing = load_descriptions(root)
    all_tags = set()
    for info in existing["files"].values():
        all_tags.update(info.get("tags", []))
    for info in existing["folders"].values():
        all_tags.update(info.get("tags", []))

    described = sum(1 for f in existing["files"].values() if f.get("description"))
    total = len(existing["files"])

    return {
        "projectName": existing.get("projectName", ""),
        "lastScan": existing.get("lastScan", ""),
        "total_files": total,
        "described_files": described,
        "undescribed_files": total - described,
        "total_folders": len(existing["folders"]),
        "all_tags": sorted(all_tags),
    }


def main():
    parser = argparse.ArgumentParser(description="项目元数据更新工具")
    parser.add_argument(
        "--action",
        choices=["update", "batch-tag", "stats"],
        required=True,
    )
    parser.add_argument("--root", default=".", help="项目根目录")
    parser.add_argument("--data", default=None, help="JSON 数据字符串")
    parser.add_argument("--file", default=None, help="JSON 数据文件路径（优先于 --data）")
    parser.add_argument("--folder", default=None, help="批量归类的目标文件夹")
    parser.add_argument("--tags", default=None, help="逗号分隔的标签列表")
    parser.add_argument("--description", default=None, help="文件夹描述")
    args = parser.parse_args()

    root = os.path.abspath(args.root)

    if args.action == "update":
        if args.file:
            with open(args.file, "r", encoding="utf-8") as f:
                new_data = json.load(f)
        elif args.data:
            new_data = json.loads(args.data)
        else:
            print(json.dumps({"error": "需要 --file 或 --data 参数"}, ensure_ascii=False))
            sys.exit(1)
        result = update_descriptions(root, new_data)
        file_count = len(result["files"])
        folder_count = len(result["folders"])
        print(json.dumps({
            "status": "updated",
            "total_files": file_count,
            "total_folders": folder_count,
            "lastScan": result["lastScan"],
        }, ensure_ascii=False, indent=2))

    elif args.action == "batch-tag":
        if not args.folder or not args.tags:
            print(json.dumps({
                "error": "批量归类需要 --folder 和 --tags 参数"
            }, ensure_ascii=False))
            sys.exit(1)
        tags = [t.strip() for t in args.tags.split(",")]
        desc = args.description or ""
        result = batch_tag(root, args.folder, tags, desc)
        print(json.dumps({
            "status": "batch_tagged",
            "folder": args.folder,
            "tags": tags,
        }, ensure_ascii=False, indent=2))

    elif args.action == "stats":
        stats = get_stats(root)
        print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
