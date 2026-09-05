#!/usr/bin/env python3
"""依赖关系扫描器 - 从 R/Quarto 文件中提取数据流依赖

扫描项目中的 .qmd/.Rmd/.R 文件，通过正则匹配提取文件读写调用，
构建文件间的数据依赖关系。若项目已有 qproj 输出则直接导入。

Usage:
  python dep_scanner.py --root <project_dir>
  python dep_scanner.py --root <project_dir> --output update
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path


# ── 读取函数模式 ──

READ_PATTERNS = [
    r'readRDS\s*\(',
    r'read_rds\s*\(',
    r'read_csv\s*\(',      r'read_csv2\s*\(',
    r'read_tsv\s*\(',      r'read_delim\s*\(',
    r'read\.csv\s*\(',     r'read\.csv2\s*\(',
    r'read\.table\s*\(',   r'read\.delim\s*\(',
    r'read_excel\s*\(',    r'read_xlsx\s*\(',
    r'fread\s*\(',         r'vroom\s*\(',
    r'read_parquet\s*\(',  r'read_feather\s*\(',
    r'load\s*\(',          r'source\s*\(',
    r'read_lines\s*\(',
]

# (pattern, arg_mode): 'first' = 第一个参数是路径, 'second' = 第二个, 'named' = file= 命名参数
WRITE_PATTERNS = [
    (r'saveRDS\s*\(', 'second'),
    (r'write_rds\s*\(', 'second'),
    (r'write_csv\s*\(', 'second'),    (r'write_csv2\s*\(', 'second'),
    (r'write_tsv\s*\(', 'second'),
    (r'write\.csv\s*\(', 'second'),   (r'write\.csv2\s*\(', 'second'),
    (r'write\.table\s*\(', 'second'),
    (r'write_xlsx\s*\(', 'second'),
    (r'fwrite\s*\(', 'second'),
    (r'write_parquet\s*\(', 'second'), (r'write_feather\s*\(', 'second'),
    (r'write_lines\s*\(', 'second'),
    (r'ggsave\s*\(', 'first'),
    (r'save\s*\(', 'named'),
]

SKIP_DIRS = {
    '.git', '.Rproj.user', 'renv', 'packrat', 'node_modules',
    '__pycache__', '.quarto', '.project-meta', '.qproj',
}


def extract_r_chunks(content: str) -> str:
    """从 .qmd/.Rmd 提取 R 代码块"""
    chunks = []
    in_chunk = False
    for line in content.split('\n'):
        if re.match(r'^```\{r', line):
            in_chunk = True
            continue
        if in_chunk and line.strip().startswith('```'):
            in_chunk = False
            continue
        if in_chunk:
            chunks.append(line)
    return '\n'.join(chunks)


def strip_comments(code: str) -> str:
    lines = []
    for line in code.split('\n'):
        in_str = None
        for i, ch in enumerate(line):
            if ch in ('"', "'") and (i == 0 or line[i - 1] != '\\'):
                in_str = None if in_str == ch else (ch if in_str is None else in_str)
            elif ch == '#' and in_str is None:
                line = line[:i]
                break
        lines.append(line)
    return '\n'.join(lines)


def resolve_here(args_str: str) -> str | None:
    """here::here("a", "b", "c.rds") → "a/b/c.rds" """
    parts = re.findall(r'["\']([^"\']+)["\']', args_str)
    return '/'.join(parts) if parts else None


# 路径构造函数：括号内的字符串字面量按顺序拼成路径。
# qproj 项目有两种写法，都要认：
#   here::here(path_target(), "x.csv")        ← path_target 无参，文件名在外层
#   path_target("x.csv")                      ← path_target 带参（proj_path_target 返回的闭包）
PATH_CALL = (
    r'(?:here::here|file\.path|path_target|path_source|path_raw'
    r'|path_resource|proj_path_\w+)'
)


def match_path_call(text: str) -> str | None:
    """匹配 text 开头的路径构造调用，返回拼接后的路径。

    参数用括号平衡提取，不能用 [^)]*：here::here(path_target(), "x.csv")
    这类嵌套调用里的内层 ')' 会让 [^)]* 提前收尾，导致整个路径解析失败。
    """
    m = re.match(r'\s*' + PATH_CALL + r'\s*\(', text)
    if not m:
        return None
    return resolve_here(extract_call_args(text, m.end() - 1))


def parse_path_expr(text: str) -> str | None:
    """解析一个路径表达式：路径构造调用，或裸字符串字面量"""
    hit = match_path_call(text)
    if hit is not None:
        return hit
    m = re.match(r'\s*["\']([^"\']+)["\']', text)
    return m.group(1) if m else None


def find_string_arg(text: str, pos: int) -> str | None:
    """从函数调用位置提取第一个字符串参数，支持 here::here() 嵌套"""
    paren = text.find('(', pos)
    if paren < 0:
        return None
    return parse_path_expr(text[paren + 1:])


def extract_call_args(text: str, paren_pos: int) -> str:
    """提取函数调用的参数文本（括号内内容），正确处理嵌套"""
    depth = 0
    for i in range(paren_pos, len(text)):
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
            if depth == 0:
                return text[paren_pos + 1:i]
    return text[paren_pos + 1:]


def find_write_arg(text: str, pos: int, mode: str) -> str | None:
    if mode == 'first':
        return find_string_arg(text, pos)

    paren = text.find('(', pos)
    if paren < 0:
        return None
    args = extract_call_args(text, paren)

    if mode == 'named':
        m = re.search(r'file\s*=\s*', args)
        return parse_path_expr(args[m.end():]) if m else None

    # mode == 'second': 先尝试命名参数，再尝试第二个位置参数
    m = re.search(r'(?:file|path)\s*=\s*', args)
    if m:
        return parse_path_expr(args[m.end():])

    depth = 0
    for i, ch in enumerate(args):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif ch == ',' and depth == 0:
            return parse_path_expr(args[i + 1:])
    return None


def scan_code(code: str) -> tuple[list[str], list[str]]:
    code = strip_comments(code)
    reads, writes = [], []

    for pat in READ_PATTERNS:
        for m in re.finditer(pat, code):
            p = find_string_arg(code, m.start())
            if p:
                reads.append(p)

    for pat, mode in WRITE_PATTERNS:
        for m in re.finditer(pat, code):
            p = find_write_arg(code, m.start(), mode)
            if p:
                writes.append(p)

    return reads, writes


def scan_file(filepath: str) -> tuple[list[str], list[str]]:
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return [], []

    ext = os.path.splitext(filepath)[1].lower()
    code = extract_r_chunks(content) if ext in ('.qmd', '.rmd') else content
    return scan_code(code)


def import_qproj(path: str) -> dict:
    """从 .qproj/graph/qproj-graph.json 导入依赖关系

    qproj 的 reads_from 边是数据流方向：source = 产出数据的上游 step，
    target = 读取它的下游 step（边的 evidence 记的是 target 文件里那句
    path_source(...) 调用）。所以 depends_on 要挂在 target 上，不是 source。
    """
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # step id → 相对文件路径：descriptions.json 以路径为 key，不能用 step id
    id_to_file = {}
    result = {}
    for node in data.get('nodes', []):
        if node.get('type') != 'step':
            continue
        nid = node.get('id', '')
        fpath = node.get('file') or nid
        id_to_file[nid] = fpath
        # 字段名是 outputs_detected；节点上没有 reads_from（只有计数 n_reads_from），
        # 读入关系一律从下面的边里取
        result[fpath] = {
            'depends_on': [],
            'produces': list(node.get('outputs_detected', [])),
        }

    for edge in data.get('edges', []):
        if edge.get('type') != 'reads_from':
            continue
        src = edge.get('source', '')   # 上游：产出这份数据的 step
        tgt = edge.get('target', '')   # 下游：读取它的 step
        refs = edge.get('files_referenced') or []
        if isinstance(refs, str):
            refs = [refs]
        entry = result.setdefault(
            id_to_file.get(tgt, tgt), {'depends_on': [], 'produces': []}
        )
        # 没记 files_referenced 的边，退化成记上游 step 本身，至少保住依赖关系
        for ref in refs or [id_to_file.get(src, src)]:
            if ref not in entry['depends_on']:
                entry['depends_on'].append(ref)

    return {k: v for k, v in result.items() if v['depends_on'] or v['produces']}


def scan_project(root: str) -> dict:
    root_path = Path(root).resolve()

    qproj = root_path / '.qproj' / 'graph' / 'qproj-graph.json'
    if qproj.exists():
        return import_qproj(str(qproj))

    result = {}
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith('.')
        ]

        for fname in filenames:
            if fname.startswith('_'):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in ('.qmd', '.rmd', '.r'):
                continue

            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, root_path).replace('\\', '/')
            reads, writes = scan_file(full)

            if reads or writes:
                result[rel] = {
                    'depends_on': sorted(set(reads)),
                    'produces': sorted(set(writes)),
                }

    return result


def main():
    parser = argparse.ArgumentParser(description='项目依赖关系扫描器')
    parser.add_argument('--root', default='.', help='项目根目录')
    parser.add_argument(
        '--output', choices=['json', 'update'], default='json',
        help='json=原始输出, update=meta_updater 兼容格式',
    )
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    deps = scan_project(root)

    if args.output == 'update':
        out = {'files': {}}
        for fp, info in deps.items():
            out['files'][fp] = {
                'produces': info.get('produces', []),
                'depends_on': info.get('depends_on', []),
            }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(deps, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
