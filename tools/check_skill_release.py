#!/usr/bin/env python3
"""Check the lab-frontier release content for private identifiers and wording."""

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SKILL = Path("skills/lab-frontier")
README = Path("README.md")
CATALOG = Path("catalog.json")

FORBIDDEN = (
    ("IPv4 地址", re.compile(r"(?<!\d)(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?!\d)")),
    ("服务器名", re.compile(r"\bspark-[0-9a-f]{4}\b", re.IGNORECASE)),
    ("tailnet 名", re.compile(r"\btail[a-z0-9]{5,}\b|\.ts\.net\b", re.IGNORECASE)),
    ("个人路径", re.compile(r"/home/[\w.-]+|[A-Z]:[\\/]+Users\b", re.IGNORECASE)),
)

# Fingerprints keep the literal private names and site identifier out of the public mirror.
PRIVATE_IDENTIFIERS = {
    2: {
        "03a9ed524add6e6a755b30d5ebce0dbfd02942631424da4fbe12be5921b7e2fd",
        "5bf028afbe260ecd46ef1d76be9876d200495f4209e2de988a90e47153e27bd9",
        "62994ced719f13157d01513670b792c4f208327c64841753d4c0e9c5c1cc236e",
    },
    3: {
        "1978f42d735b97b143537e870191c99c98774eafd55f1054ecbc95599f38b5cb",
        "79a98f8d48f5de10792cb00ff5bb06810f53c3bcf446ce27a921febce848824b",
        "ce6cc75996f1f8bbfcc8b0cd30f2797b3c27452d9a7054bdba2c558992a4ff39",
    },
    5: {
        "818d9fd79091d3dea65305be5e37a0a6d3c3511d9896cb02538e74b1186e6074",
        "8b7b4ba0883edabbd421a2c20f54abc48cb9db318fb516472473e10f0285e12e",
    },
    6: {"18a53242ad3e4c3676e294522f4116680b69ef6527995ee1b3576eb73e776bae"},
    7: {
        "57fb9cfe13f25e9cd7f3e46d39807c898927e0b9f8cfaeb257c00b7cd30e1a24",
        "6123d4683e203e7d5c38e63fe65d09e3e2cef49bb7445251ab6f3ed3fc9522ae",
    },
    8: {"79f2a61b86b34c60846635613a0b404e8e2b3d2a86ed5877664e41e1b0cb14bf"},
}
PRIVATE_WORD = "a7877a970f44a733e53af458a618824987373f0a48d0ef36539d7f6d0c6a504e"
ACCOUNT_WORDING = re.compile(
    r"账号(?:的?归属|所有权|所有者|是谁的|属于|是(?:自己|本人|别人|他人)的)|"
    r"(?:谁的|自己的|个人的?|他人的|别人的|本人|你的|我的)[ \tA-Za-z]*账号|"
    r"借用|借给|出借|租借|借号|借账号|"
    r"(?:共享|共用|公用)(?:(?:同)?一[份个]?)?[ \tA-Za-z]*(?:额度|配额|账号)|"
    r"(?:额度|配额|账号)[ \t，,]*(?:共享|共用|公用)|"
    r"account ownership|account (?:is owned by|belongs to|sharing|loan)|"
    r"(?:my|your|their|personal|own) (?:codex )?account\b|"
    r"borrow(?:ed|ing)? (?:an? |the )?account|shar(?:e|ed|ing) (?:an? |the )?(?:quota|account)|"
    r"(?:quota|account) sharing",
    re.IGNORECASE,
)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def has_private_identifier(line):
    line = line.casefold()
    for length, hashes in PRIVATE_IDENTIFIERS.items():
        if any(digest(line[start:start + length]) in hashes for start in range(len(line) - length + 1)):
            return True
    return any(digest(word) == PRIVATE_WORD for word in re.findall(r"\b[a-z]{3}\b", line))


def scan(path, lines, *, check_wording=False):
    for number, line in lines:
        for label, pattern in FORBIDDEN:
            if pattern.search(line):
                yield (path, number, label)
        if has_private_identifier(line):
            yield (path, number, "私有标识或人名")
        if check_wording and ACCOUNT_WORDING.search(line):
            yield (path, number, "账号或额度措辞")


def added_readme_lines(root):
    for ref in ("refs/heads/main", "refs/remotes/origin/main"):
        base = subprocess.run(["git", "merge-base", "HEAD", ref], cwd=root,
                              capture_output=True, text=True)
        if base.returncode == 0:
            break
    else:
        return set()

    diff = subprocess.run(["git", "diff", "--no-ext-diff", "--no-textconv", "--no-color",
                           "--unified=0", base.stdout.strip(), "--", str(README)],
                          cwd=root, capture_output=True, encoding="utf-8", check=True)
    numbers = set()
    current = None
    for line in diff.stdout.splitlines():
        if hunk := re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line):
            current = int(hunk[1])
        elif current is not None and line.startswith(("+", " ")):
            if line.startswith("+"):
                numbers.add(current)
            current += 1
    return numbers


def readme_lines(root):
    numbers = added_readme_lines(root)
    section = tree = found_section = found_tree = False
    for number, line in enumerate((root / README).read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("#"):
            section = line.startswith("### `lab-frontier`")
            found_section |= section
        if not line.startswith("│"):
            tree = line.startswith(("├── lab-frontier/", "└── lab-frontier/"))
            found_tree |= tree
        if section or tree or number in numbers:
            yield number, line
    if not (found_section and found_tree):
        raise ValueError("README.md 缺少 lab-frontier 的 Skills 条目或目录树条目")


def check(root):
    skill_root = root / SKILL
    if not (skill_root / "SKILL.md").is_file():
        raise ValueError("缺少 lab-frontier 的 SKILL.md")
    for file in sorted(skill_root.rglob("*")):
        if "__pycache__" in file.parts or file.suffix == ".pyc":
            continue
        path = file.relative_to(root)
        if file.is_symlink():
            raise ValueError(f"{path} 是符号链接，无法检查发布内容")
        if file.is_file():
            lines = enumerate(file.read_text(encoding="utf-8").splitlines(), 1)
            yield from scan(path, [(0, path.as_posix())])
            yield from scan(path, lines, check_wording=path == SKILL / "SKILL.md")

    catalog = json.loads((root / CATALOG).read_text(encoding="utf-8"))
    entries = [entry for entry in catalog["skills"] if entry.get("name") == SKILL.name]
    if len(entries) != 1:
        raise ValueError("catalog.json 需要恰好一项 lab-frontier")
    rendered = json.dumps(entries[0], ensure_ascii=False, indent=2)
    yield from scan("catalog.json[lab-frontier]", enumerate(rendered.splitlines(), 1))
    yield from scan(README, readme_lines(root))


def main():
    try:
        hits = list(check(ROOT))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as error:
        print(f"发布检查未完成：{error}", file=sys.stderr)
        return 2
    for path, number, label in hits:
        print(f"{path}:{number}: {label}", file=sys.stderr)
    if not hits:
        print("lab-frontier 发布检查通过")
    return int(bool(hits))


if __name__ == "__main__":
    sys.exit(main())
