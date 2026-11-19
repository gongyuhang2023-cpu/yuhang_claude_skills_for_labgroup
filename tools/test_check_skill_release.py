import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import check_skill_release as checker


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.skill = self.root / checker.SKILL / "SKILL.md"
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text("# lab-frontier\n往常查看简报。\n", encoding="utf-8")
        self.entry = {"name": "lab-frontier", "purpose": "查看简报"}
        self.catalog = self.root / checker.CATALOG
        self.write_catalog()
        self.readme = self.root / checker.README
        self.safe_readme = """\
# Skills
### `lab-frontier`
查看简报。
### `other`
其他技能。
```
├── lab-frontier/
│   └── SKILL.md
└── other/
```
"""
        self.readme.write_text(self.safe_readme, encoding="utf-8")

    def write_catalog(self, *other):
        self.catalog.write_text(json.dumps({"skills": [self.entry, *other]}), encoding="utf-8")

    def run_git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def test_detects_name_in_skill_and_exits_nonzero(self):
        name = "\u738b\u6167"
        self.skill.write_text(f"# lab-frontier\n审阅者为{name}。\n", encoding="utf-8")
        with patch.object(checker, "ROOT", self.root), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(checker.main(), 1)
        self.assertIn("SKILL.md:2: 私有标识或人名", err.getvalue())
        self.assertNotIn(name, err.getvalue())

    def test_scope_covers_catalog_entry_and_readme_blocks(self):
        private = "\u68ee\u6755"
        self.readme.write_text(self.safe_readme.replace("其他技能。", private), encoding="utf-8")
        self.write_catalog({"name": "other", "purpose": private})
        self.assertEqual(list(checker.check(self.root)), [])
        self.entry["purpose"] = private
        self.write_catalog()
        self.readme.write_text(self.safe_readme.replace("查看简报。", private)
                               .replace("└── SKILL.md", f"└── {private}"), encoding="utf-8")
        self.assertEqual([(str(path), number) for path, number, _ in checker.check(self.root)],
                         [("catalog.json[lab-frontier]", 3), ("README.md", 3), ("README.md", 8)])

    def test_scans_additions_outside_named_readme_blocks(self):
        self.run_git("init", "-b", "main")
        self.run_git("add", str(checker.README))
        self.run_git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                     "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                     "commit", "-m", "Fixture baseline")
        self.run_git("switch", "-c", "feature")
        self.readme.write_text(self.safe_readme + "\n示例 192.0.2.17\n", encoding="utf-8")
        self.assertEqual([(str(path), label) for path, _, label in checker.check(self.root)],
                         [("README.md", "IPv4 地址")])

    def test_location_patterns_and_name_word_boundary(self):
        values = ["192.0.2.17", "/home/member_name", r"C:\Users\member", "spark-ab12",
                  "tail123abc", "server.example.ts.net", "git." + "\x64\x65\x6e\x67\x6c\x61\x62" + ".xyz",
                  "\x59\x75\x68\x61\x6e\x67", "\u6c5d\u9526\u9f99", "owner (\x68\x75\x69)"]
        for value in values:
            with self.subTest(value=value):
                self.assertTrue(list(checker.scan("test", [(1, value)])))
        self.assertEqual(list(checker.scan("test", [(1, "shui hui2 huixin")])), [])

    def test_account_wording_is_checked_by_release_script(self):
        for wording in ("账号的归属", "借用 Codex 账号", "额度共享", "共用一份额度", "shared quota"):
            with self.subTest(wording=wording):
                self.skill.write_text(f"# lab-frontier\n{wording}\n", encoding="utf-8")
                self.assertEqual(list(checker.check(self.root)),
                                 [(checker.SKILL / "SKILL.md", 2, "账号或额度措辞")])


if __name__ == "__main__":
    unittest.main()
