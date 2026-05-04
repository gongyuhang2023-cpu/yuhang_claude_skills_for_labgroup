#!/usr/bin/env python3
"""
Generate a self-contained project viewer HTML and a .url shortcut.

Usage:
  python generate_launcher.py --root <project_dir>

Produces:
  <project_dir>/.project-meta/launch_viewer.html  (viewer + embedded data)
  <project_dir>/文件画像.url                       (shortcut with icon)
"""

import argparse
import json
import os
import re
import sys

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEWER_PATH = os.path.join(SKILL_DIR, 'viewer.html')


def generate(project_dir):
    desc_path = os.path.join(project_dir, '.project-meta', 'descriptions.json')
    if not os.path.exists(desc_path):
        print(json.dumps({"error": f"descriptions.json not found at {desc_path}"}))
        sys.exit(1)

    if not os.path.exists(VIEWER_PATH):
        print(json.dumps({"error": f"viewer.html not found at {VIEWER_PATH}"}))
        sys.exit(1)

    with open(VIEWER_PATH, 'r', encoding='utf-8') as f:
        viewer_html = f.read()

    with open(desc_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    data_json = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    embed_script = f'<script>window.EMBEDDED_DATA={data_json};</script>\n'

    launcher_html = viewer_html.replace(
        '<script>\n/* ═══ CONSTANTS',
        embed_script + '<script>\n/* ═══ CONSTANTS'
    )

    project_name = data.get('projectName', '项目')
    safe_title = re.sub(r'[<>:"/\\|?*]', '', project_name)[:40]
    launcher_html = launcher_html.replace(
        '<title>项目文件画像</title>',
        f'<title>{safe_title} - 文件画像</title>'
    )

    launcher_path = os.path.join(project_dir, '.project-meta', 'launch_viewer.html')
    with open(launcher_path, 'w', encoding='utf-8') as f:
        f.write(launcher_html)

    abs_html = os.path.abspath(launcher_path).replace('\\', '/')
    url_content = (
        '[InternetShortcut]\n'
        f'URL=file:///{abs_html}\n'
        f'IconFile=C:\\Windows\\System32\\shell32.dll\n'
        f'IconIndex=44\n'
    )
    url_path = os.path.join(project_dir, '文件画像.url')
    with open(url_path, 'w', encoding='utf-8') as f:
        f.write(url_content)

    result = {
        "launcher_html": launcher_path,
        "shortcut": url_path,
        "project_name": project_name,
        "icon": "shell32.dll,44 (starred folder)"
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Generate project viewer launcher')
    parser.add_argument('--root', required=True, help='Project root directory')
    args = parser.parse_args()
    generate(os.path.abspath(args.root))
