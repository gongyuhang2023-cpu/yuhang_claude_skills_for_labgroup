#!/usr/bin/env python3
"""文件快照工具 - 扫描项目文件夹并对比变更"""

import argparse
import json
import os
import sys
import fnmatch
from pathlib import Path
from datetime import datetime, timezone


def load_ignore_patterns(root: str) -> list[str]:
    ignore_file = os.path.join(root, ".project-meta", "ignore")
    patterns = []
    if os.path.exists(ignore_file):
        with open(ignore_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    return patterns


def should_ignore(rel_path: str, is_dir: bool, patterns: list[str]) -> bool:
    parts = rel_path.replace("\\", "/").split("/")
    for pattern in patterns:
        clean = pattern.rstrip("/")
        is_dir_pattern = pattern.endswith("/")
        for part in parts:
            if fnmatch.fnmatch(part, clean):
                if is_dir_pattern and not is_dir:
                    if part != parts[-1]:
                        return True
                else:
                    return True
        if fnmatch.fnmatch(rel_path.replace("\\", "/"), clean):
            return True
    return False


def scan_directory(root: str, patterns: list[str]) -> dict:
    snapshot = {}
    root_path = Path(root).resolve()
    for dirpath, dirnames, filenames in os.walk(root_path):
        rel_dir = os.path.relpath(dirpath, root_path).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""

        dirnames[:] = [
            d for d in dirnames
            if not should_ignore(
                f"{rel_dir}/{d}" if rel_dir else d, True, patterns
            )
        ]

        for fname in filenames:
            rel_path = f"{rel_dir}/{fname}" if rel_dir else fname
            if should_ignore(rel_path, False, patterns):
                continue
            full_path = os.path.join(dirpath, fname)
            try:
                stat = os.stat(full_path)
                snapshot[rel_path] = {
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                }
            except OSError:
                continue
    return snapshot


def load_snapshot(root: str) -> dict | None:
    snapshot_file = os.path.join(root, ".project-meta", "snapshot.json")
    if not os.path.exists(snapshot_file):
        return None
    with open(snapshot_file, "r", encoding="utf-8") as f:
        return json.load(f)


def save_snapshot(root: str, snapshot: dict):
    snapshot_file = os.path.join(root, ".project-meta", "snapshot.json")
    os.makedirs(os.path.dirname(snapshot_file), exist_ok=True)
    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "files": snapshot,
    }
    with open(snapshot_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def diff_snapshots(old: dict | None, new: dict) -> dict:
    if old is None:
        return {
            "added": sorted(new.keys()),
            "modified": [],
            "deleted": [],
            "unchanged_count": 0,
            "total_scanned": len(new),
        }

    old_files = old.get("files", old)
    added = []
    modified = []
    unchanged = 0

    for path, info in new.items():
        if path not in old_files:
            added.append(path)
        elif (info["size"] != old_files[path]["size"]
              or abs(info["mtime"] - old_files[path]["mtime"]) > 1.0):
            modified.append(path)
        else:
            unchanged += 1

    deleted = [p for p in old_files if p not in new]

    return {
        "added": sorted(added),
        "modified": sorted(modified),
        "deleted": sorted(deleted),
        "unchanged_count": unchanged,
        "total_scanned": len(new),
    }


def main():
    parser = argparse.ArgumentParser(description="项目文件快照工具")
    parser.add_argument("--action", choices=["scan", "save"], required=True)
    parser.add_argument("--root", default=".", help="项目根目录")
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    patterns = load_ignore_patterns(root)

    if args.action == "scan":
        current = scan_directory(root, patterns)
        old = load_snapshot(root)
        diff = diff_snapshots(old, current)
        print(json.dumps(diff, ensure_ascii=False, indent=2))

    elif args.action == "save":
        current = scan_directory(root, patterns)
        save_snapshot(root, current)
        print(json.dumps({
            "status": "saved",
            "file_count": len(current),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
