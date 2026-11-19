#!/usr/bin/env python3
"""项目用到的公共数据库：扫描脚本、项目数据库清单 databases.lock.json、个人副本指纹。

服务器和本机共用，只用标准库：
- 服务器上：advise.py 经 ssh 用 `python3 -I - <子命令> …` 把本文件送过去执行；remote_project.py 也带着它。
- 本机 / ws：项目目录在本机时，advise.py、ws_forward.py 直接载入本文件，在本机扫描、写清单和脚本；
  库的信息（公共库清单、current 指向、登录环境里的数据库变量、公共工具版本、指纹）只在服务器上有，
  用 facts 子命令取回来。
current 只 readlink，不 realpath 进 NAS。会写文件的 lock / pin 默认只预演，带 --yes --plan-id 才写。
"""
import argparse
import csv
import datetime
import difflib
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time

DB_ROOT = "/opt/bio/db"
INVENTORY = "/opt/bio/var/inventory.json"
DB_CHECK = "/opt/bio/var/db-check.json"
CATALOG_TOOL = "/opt/bio/sbin/bio_db_catalog.py"
LOCK_FILE = "databases.lock.json"
LOCK_SCHEMA = 1
LOW_PRIORITY_NICE = 100000          # 同 advise.py：排在所有人的普通作业之后
COMPARE_DIR = "~/.spark-advisor/db-compare"
SMALL_BYTES = 2 * 10 ** 9           # 个人副本不超过 2 GB、文件不超过 SMALL_FILES 个：当场算指纹
SMALL_FILES = 20000
DB_INSTALL = "/usr/local/bin/db-install"
ADOPT_DIR = "~/.spark-advisor/db-adopt"   # 收编预演的作业结果和保存的预演（plan-<plan_sha256>.json），只有本人能读
ADOPT_DIRECT_BYTES = 10 ** 9        # 收编预演要逐个文件算 sha256：不超过 1 GB 当场做，更大的放进低优先级作业
ADOPT_JOB_TIME = "1-00:00:00"
JOB_WAITING = ("PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "SUSPENDED", "REQUEUED", "RESIZING")

# 绑定工具：工具名 -> (它默认读的环境变量, 库名, 显式指定库的参数)。调用了工具却没设变量，就读服务器的 current
BOUND_TOOLS = {"gtdbtk": ("GTDBTK_DATA_PATH", "gtdbtk", ()),
               "bakta": ("BAKTA_DB", "bakta", ("--db", "-d")),
               "checkv": ("CHECKVDB", "checkv", ("-d",))}
# 服务端兼容检查（compat）：库名 -> 检查所用工具名
BOUND_DBS = {"gtdbtk": "gtdbtk", "bakta": "bakta", "checkv": "checkv", "genomad": "genomad"}

SCRIPT_EXT = {".sh", ".bash", ".zsh", ".py", ".r", ".rmd", ".qmd", ".smk", ".nf", ".sbatch", ".slurm", ".yaml",
              ".yml", ".toml", ".cfg", ".config", ".ipynb", ".pl", ".mk"}
SCRIPT_NAMES = {"Snakefile", "Makefile", "nextflow.config"}
PRUNE = {".git", ".snakemake", "work", "node_modules", "__pycache__", ".pixi", "envs", "_data", "data",
         "results", "logs", ".ipynb_checkpoints"}
MAX_FILE_BYTES = 1 << 20
MAX_FILES = 20000

NAME = r"[A-Za-z0-9][A-Za-z0-9_.+-]*"
COMPONENT_RE = re.compile(NAME)
VAR_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
LIT_RE = re.compile(r"(?<![\w.+/-])" + re.escape(DB_ROOT) + r"/(" + NAME + r")/(" + NAME + r")"
                    r"((?:/[^\s\"'`;|&<>(){}\[\],$]*)?)")
VAR_RE = re.compile(r"\$(?:\{(" + VAR_NAME + r")[^}]*\}|(" + VAR_NAME + r"))")
R_GET_RE = re.compile(r"\bSys\.getenv\(\s*[\"'](" + VAR_NAME + r")[\"']")
PY_GET_RE = re.compile(r"\bos\.(?:environ\.get|getenv)\(\s*[\"'](" + VAR_NAME + r")[\"']"
                       r"|\bos\.environ\s*\[\s*[\"'](" + VAR_NAME + r")[\"']\s*\](?!\s*=(?!=))")
PY_SET_RE = re.compile(r"\bos\.environ\s*\[\s*[\"'](" + VAR_NAME + r")[\"']\s*\]\s*=(?!=)\s*(.*)"
                       r"|\bos\.environ\.setdefault\(\s*[\"'](" + VAR_NAME + r")[\"']\s*,\s*(.*)"
                       r"|[\"'](" + VAR_NAME + r")[\"']\s*:\s*(.*)")
R_SET_RE = re.compile(r"\bSys\.setenv\((.*)\)")
R_PAIR_RE = re.compile(r"(" + VAR_NAME + r")\s*=\s*(\"[^\"]*\"|'[^']*'|[^,)]+)")
SH_SET_RE = re.compile(r"(?:^|(?<=[\s;&|(]))(?:export\s+|declare\s+-x\s+|readonly\s+)?(" + VAR_NAME + r")="
                       r"(\"[^\"]*\"|'[^']*'|[^\s;|&)]*)")
SH_SOURCE_RE = re.compile(r"(?:^|[;&|]\s*)(?:source|\.)\s+(\"[^\"]+\"|'[^']+'|[^\s;|&]+)")
DBCACHE_RE = re.compile(r"\bdb-cache\s+use\s+(?:--fetch-if-missing\s+)?(\"[^\"]*\"|'[^']*'|[^\s;|&)]+)")
TOOLS = "|".join(sorted(BOUND_TOOLS))
# 命令位置上的调用（行首、; | && 之后、$( 里，可以带 srun / time / env 和 VAR=值 前缀）；--version / -h 不读库
TOOL_SH_RE = re.compile(r"(?:^|[;&|(`]|\$\()\s*(?:(?:sudo|srun|time|nohup|exec|env|xargs)\s+(?:-\S+\s+(?:[^-\s]\S*\s+)?)*)*"
                        r"(?:" + VAR_NAME + r"=\S*\s+)*(?:\S*/)?(" + TOOLS + r")(?=\s|$|[;&|)])"
                        r"(?!\s+(?:--version|-v|-h|--help|version)\b)")
# Python / R / Snakemake 字符串里的命令：gtdbtk、checkv 要带子命令，bakta 要带参数，免得把 print("gtdbtk done") 当成调用
TOOL_STR_RE = re.compile(r"[\"'\[]\s*[\"']?(?:[^\s\"']*/)?(?:(gtdbtk)(?:\s+|[\"']\s*,\s*[\"'])(?=(?:classify_wf|de_novo_wf|"
                         r"identify|align|classify|infer|ani_rep|ani_screen|check_install|decorate|infer_ranks|root)\b)"
                         r"|(checkv)(?:\s+|[\"']\s*,\s*[\"'])(?=(?:end_to_end|contamination|completeness|complete_genomes)\b)"
                         r"|(bakta)(?:\s+|[\"']\s*,\s*[\"'])(?=-))")
HOME_RE = re.compile(r"(?<![\w.+/-])(?:/home/[^/\s\"'`]+|~|\$HOME|\$\{HOME\})/[^\s\"'`;|&<>(){}\[\],]+")


# ----------------------------- 小工具 -----------------------------

def whoami():
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, AttributeError):
        import getpass
        return getpass.getuser()


def tilde(p):
    h = os.path.expanduser("~")
    return "~" + p[len(h):] if p == h or p.startswith(h + "/") else p


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def sha_text(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def valid_component(s):
    return bool(s) and COMPONENT_RE.fullmatch(s) is not None and s not in (".", "..")


def vkey(v):
    return [(1, int(x), "") if x.isdigit() else (0, 0, x) for x in re.split(r"[.\-_]", str(v or ""))]


def parse_db_path(value):
    """'/opt/bio/db/bakta/current/db' -> ('bakta', 'current', '/db')；不是公共库路径返回 None。只看字面，不碰文件系统。"""
    m = LIT_RE.search(value or "")
    if not m:
        return None
    version = m.group(2).rstrip(".")
    return m.group(1), version, m.group(3) or ""


def current_target(name, target, root=DB_ROOT):
    """readlink 得到的 current 指向 -> 版本号。指到库目录外（比如 NAS 上的绝对路径）就不往下解析，返回 None。"""
    t = (target or "").rstrip("/")
    if not t:
        return None
    if not t.startswith("/"):
        v = t.split("/")[0]
        return v if valid_component(v) else None
    prefix = root.rstrip("/") + "/" + name + "/"
    if t.startswith(prefix):
        v = t[len(prefix):].split("/")[0]
        return v if valid_component(v) else None
    return None


def resolve_current(name, facts):
    v = (facts.get("current") or {}).get(name)
    if v:
        return v
    for row in facts.get("catalog") or []:
        if row.get("name") == name and row.get("current") is True:
            return row.get("version")
    return None


def catalog_index(facts):
    return {(r.get("name"), r.get("version")): r for r in facts.get("catalog") or []}


def strip_comment(line):
    """去掉 # 注释（引号里的、${#x}、$# 不算）。"""
    quote, i = None, 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t;"):
            return line[:i]
        i += 1
    return line


def unquote(s):
    s = (s or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def lang_of(fn, first_line):
    """-> 语言集合：sh / py / r 决定认哪些写法和 pin 怎么插语句；doc 表示不自动插语句；data 只认字面路径和 $VAR。"""
    ext = os.path.splitext(fn)[1].lower()
    if fn == "Snakefile" or ext == ".smk":
        return {"py", "sh"}
    if ext in (".sh", ".bash", ".zsh", ".sbatch", ".slurm"):
        return {"sh"}
    if ext == ".py":
        return {"py"}
    if ext == ".r":
        return {"r"}
    if ext in (".rmd", ".qmd"):
        return {"r", "sh", "doc"}
    if ext == ".nf" or fn == "nextflow.config":
        return {"sh", "doc", "groovy"}
    if fn == "Makefile" or ext == ".mk":
        return {"sh", "doc"}
    if ext == "" and first_line.startswith("#!"):
        if "python" in first_line:
            return {"py"}
        if "Rscript" in first_line:
            return {"r"}
        if re.search(r"\b(ba|z|k|da)?sh\b", first_line):
            return {"sh"}
    return {"data"}


def logical_lines(text):
    """-> [(起始行号, 去掉注释的代码)]：行尾 \\ 续行合并成一行。"""
    out, buf, start = [], "", None
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\r")
        if start is None:
            start = no
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        out.append((start, buf + line))
        buf, start = "", None
    if buf:
        out.append((start, buf))
    return out


# ----------------------------- 服务器上的事实（facts） -----------------------------

CATALOG_KEYS = ("name", "version", "path", "current", "location", "nas", "size", "aliases", "via", "request_id",
                "installed_by", "verification", "compat", "compat_tool", "fingerprint", "layout", "recipe")


def login_env():
    """登录环境里指向公共库的变量（gen-sbatch 生成的作业是登录 shell，读到的就是这些）。"""
    try:
        p = subprocess.run(["bash", "-lc", "env -0"], capture_output=True, timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    env = {}
    for item in p.stdout.decode("utf-8", "replace").split("\0"):
        k, sep, v = item.partition("=")
        if sep and re.fullmatch(VAR_NAME, k) and (v == DB_ROOT or v.startswith(DB_ROOT + "/")):
            env[k] = v
    return env


def server_fingerprint(name, version):
    """服务器上的指纹（由文件清单现算）。命令约定：bio_db_catalog.py fingerprint <名> <版本> --json。"""
    try:
        p = subprocess.run(["python3", "-I", CATALOG_TOOL, "fingerprint", name, version, "--json"],
                           capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        if p.returncode == 0:
            d = json.loads(p.stdout)
            return {"fingerprint": d.get("fingerprint"), "partial": bool(d.get("partial")),
                    "meta_fingerprint": d.get("meta_fingerprint"), "match": d.get("match"), "source": "server"}
        err = (p.stderr or p.stdout).strip()[-300:]
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        err = str(e)[:300]
    return {"error": err or "指纹命令失败"}


def gather_facts(pairs=(), fingerprints=False, root=DB_ROOT, inventory=INVENTORY, db_check=DB_CHECK,
                 env_fn=login_env, fingerprint_fn=server_fingerprint):
    """在服务器上收集库的信息。读不到的写进 errors，不抛异常：调用方据此判「判断不了」。"""
    facts = {"schema": 1, "user": whoami(), "catalog": None, "current": {}, "env": {}, "tools": {},
             "fingerprints": {}, "db_check": None, "errors": []}
    try:
        with open(inventory, encoding="utf-8") as f:
            inv = json.load(f)
        facts["catalog"] = [{k: d[k] for k in CATALOG_KEYS if k in d} for d in inv.get("dbs") or []]
        for t in inv.get("tools") or []:
            if t.get("package") in BOUND_DBS.values():
                facts["tools"][t["package"]] = t.get("version")
    except (OSError, ValueError, AttributeError) as e:
        facts["errors"].append("公共库清单读不到：%s" % e)
    try:
        for name in sorted(os.listdir(root)):
            link = os.path.join(root, name, "current")
            if os.path.islink(link):
                facts["current"][name] = current_target(name, os.readlink(link), root)
    except OSError as e:
        facts["errors"].append("读不了 %s：%s" % (root, e.strerror or e))
    env = env_fn()
    if env is None:
        facts["errors"].append("读不到登录环境里的数据库变量")
    else:
        facts["env"] = env
    try:
        with open(db_check, encoding="utf-8") as f:
            chk = json.load(f)
        facts["db_check"] = {"status": chk.get("status"), "generated": chk.get("generated"),
                             "versions": {"%s/%s" % (d.get("name"), d.get("version")): d.get("status")
                                          for d in chk.get("databases") or [] if d.get("status") != "ok"}}
    except (OSError, ValueError, AttributeError):
        pass
    if fingerprints:
        for name, version in pairs:
            if valid_component(name) and valid_component(version):
                facts["fingerprints"][name + "/" + version] = fingerprint_fn(name, version)
    return facts


def fingerprint_for(name, version, facts):
    """-> (指纹, 说明)：优先用服务器现算的；指纹命令不可用时退回登记时写在 meta 里的。都没有返回 (None, 原因)。"""
    fp = (facts.get("fingerprints") or {}).get(name + "/" + version) or {}
    if fp.get("fingerprint"):
        return fp["fingerprint"], fp
    row = catalog_index(facts).get((name, version)) or {}
    if row.get("fingerprint"):
        return row["fingerprint"], {"source": "meta", "server_error": fp.get("error")}
    return None, {"error": fp.get("error") or "这个版本没有登记指纹"}


# ----------------------------- 扫描 -----------------------------

def iter_scripts(top, limit=MAX_FILES):
    """-> (相对路径, 文本) 和是否截断。跳过数据 / 结果 / 日志目录、链接、超过 1 MB 的文件。"""
    files, truncated = [], False
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = sorted(d for d in dirnames if d not in PRUNE and not os.path.islink(os.path.join(dirpath, d)))
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            ext = os.path.splitext(fn)[1].lower()
            if os.path.islink(full) or (fn not in SCRIPT_NAMES and ext not in SCRIPT_EXT and ext != ""):
                continue
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
                with open(full, encoding="utf-8", errors="replace", newline="") as f:
                    text = f.read()
            except OSError:
                continue
            if ext == "" and fn not in SCRIPT_NAMES and not text.startswith("#!"):
                continue
            if len(files) >= limit:
                return files, True
            files.append((os.path.relpath(full, top).replace(os.sep, "/"), text))
    return files, truncated


def scan_file(rel, text):
    """一个文件里的原始引用：字面路径、db-cache use、变量引用、设变量、调用绑定工具、source、个人副本路径。"""
    fn = rel.rsplit("/", 1)[-1]
    langs = lang_of(fn, text.split("\n", 1)[0])
    ipynb = fn.lower().endswith(".ipynb")
    occ, sets, sources = [], {}, []
    lines = logical_lines(text)
    for idx, (no, line) in enumerate(lines):
        if not ipynb:
            if line.lstrip().startswith("#!") or ("groovy" in langs and line.lstrip().startswith("//")):
                continue
            code = strip_comment(line)
        else:
            code = line
        if not code.strip():
            continue
        cache_spans = []
        for m in DBCACHE_RE.finditer(code):
            arg = unquote(m.group(1))
            cache_spans.append(m.span(1))
            if parse_db_path(arg):
                occ.append({"kind": "db-cache", "line": no, "value": arg})
        for m in LIT_RE.finditer(code):
            if any(a <= m.start() < b for a, b in cache_spans):
                continue
            occ.append({"kind": "literal", "line": no, "value": m.group(0)})
        for m in HOME_RE.finditer(code):
            occ.append({"kind": "personal", "line": no, "value": m.group(0)})
        if langs != {"r"}:          # R 里的 $ 是取列表元素，不是变量
            for m in VAR_RE.finditer(code):
                occ.append({"kind": "var", "line": no, "var": m.group(1) or m.group(2), "form": "$"})
        if "r" in langs:
            for m in R_GET_RE.finditer(code):
                occ.append({"kind": "var", "line": no, "var": m.group(1), "form": "Sys.getenv"})
            for m in R_SET_RE.finditer(code):
                for k, v in R_PAIR_RE.findall(m.group(1)):
                    sets.setdefault(k, []).append((no, unquote(v)))
        if "py" in langs:
            for m in PY_GET_RE.finditer(code):
                occ.append({"kind": "var", "line": no, "var": m.group(1) or m.group(2), "form": "os.environ"})
            for m in PY_SET_RE.finditer(code):
                k = m.group(1) or m.group(3) or m.group(5)
                v = m.group(2) if m.group(1) else m.group(4) if m.group(3) else m.group(6)
                if m.group(5) and not (k in {t[0] for t in BOUND_TOOLS.values()} or DB_ROOT in (v or "")):
                    continue        # 字典里的普通键，不是环境变量
                lit = re.match(r"\s*(\"[^\"]*\"|'[^']*')", v or "")
                sets.setdefault(k, []).append((no, unquote(lit.group(1)) if lit else (v or "").strip()))
        if "sh" in langs:
            for m in SH_SET_RE.finditer(code):
                sets.setdefault(m.group(1), []).append((no, unquote(m.group(2))))
            for m in SH_SOURCE_RE.finditer(code):
                sources.append(unquote(m.group(1)))
        tool_hits = []
        if "sh" in langs:
            tool_hits += [m.group(1) for m in TOOL_SH_RE.finditer(code)]
        if "py" in langs or "r" in langs:
            tool_hits += [m.group(1) or m.group(2) or m.group(3) for m in TOOL_STR_RE.finditer(code)]
        if tool_hits:
            window = " ".join(c for _, c in lines[idx:idx + 4])
            for tool in dict.fromkeys(tool_hits):
                opts = BOUND_TOOLS[tool][2]
                explicit = any(re.search(r"(?:^|[\s\"',\[])" + re.escape(o) + r"(?=[\s=\"',])", window) for o in opts)
                occ.append({"kind": "tool", "line": no, "tool": tool, "explicit_db": explicit})
    return {"langs": sorted(langs), "occ": occ, "sets": sets, "sources": sources}


def _sourced(rel, src, scanned):
    """source 的目标在项目里时返回它的相对路径（只认不带变量的路径）。"""
    if "$" in src or "`" in src:
        return None
    base = rel.rsplit("/", 1)[0] if "/" in rel else ""
    for cand in (os.path.normpath(os.path.join(base, src)).replace(os.sep, "/"), src.lstrip("./")):
        if cand in scanned:
            return cand
    return None


def _resolve_value(value, facts):
    """路径 -> (库名, 写的版本, 实际版本, 其余部分)；current 换成实际版本（readlink）。"""
    p = parse_db_path(value)
    if not p:
        return None
    name, version, rest = p
    resolved = resolve_current(name, facts) if version == "current" else version
    return name, version, resolved, rest


def scan(top, facts):
    """扫描项目，返回 {occ: [逐处引用], refs: [合并后的引用], files, truncated}。"""
    files, truncated = iter_scripts(top)
    per_file = {rel: scan_file(rel, text) for rel, text in files}
    env = facts.get("env") or {}
    bound_vars = {t[0]: t[1] for t in BOUND_TOOLS.values()}
    project_db_vars = {k for info in per_file.values() for k, vals in info["sets"].items()
                       if any(DB_ROOT in (v or "") for _, v in vals)}
    db_vars = set(env) | set(bound_vars) | project_db_vars
    cat = catalog_index(facts)
    names = sorted({r.get("name") for r in facts.get("catalog") or [] if len(r.get("name") or "") >= 4},
                   key=len, reverse=True)
    occ = []
    for rel, info in per_file.items():
        sets = {k: list(v) for k, v in info["sets"].items()}
        for src in info["sources"]:
            target = _sourced(rel, src, per_file)
            if target and target != rel:
                for k, v in per_file[target]["sets"].items():
                    sets.setdefault(k, []).extend(v)
        for o in info["occ"]:
            o = dict(o, file=rel, langs=info["langs"])
            kind = o["kind"]
            if kind == "var":
                var = o["var"]
                if var not in db_vars:
                    continue
                if var in sets:
                    lit = next((v for _, v in sets[var] if parse_db_path(v)), None)
                    o.update(source="project-set", value=lit)
                    if lit is None:
                        o["dynamic"] = True
                elif var in env:
                    o.update(source="server-env", value=env[var])
                else:
                    o.update(source="unknown", value=None, dynamic=True)
            elif kind == "tool":
                var, dbname, _ = BOUND_TOOLS[o["tool"]]
                if o.pop("explicit_db") or var in sets:
                    continue
                o.update(kind="tool-default", var=var, source="server-env", value=env.get(var))
                if o["value"] is None:
                    o["dynamic"] = True
            elif kind == "personal":
                # 目录名就是库名，或库名后面跟 db / 版本号（checkv-db-v1.5、gtdbtk_r226），或目录名是 db / database
                parts = [x for x in o["value"].lower().split("/") if x]
                hit = next((n for n in names if any(re.fullmatch(re.escape(n.lower()) + r"(?:[-_.]?(?:db|database|data|[vr]?\d).*)?", x)
                                                    for x in parts)), None)
                if not hit and not any(re.search(r"(^|[-_.])db($|[-_.])|database", x) for x in parts):
                    continue
                o["name"] = hit
                occ.append(o)
                continue
            else:
                o["source"] = "script"
            r = _resolve_value(o.get("value"), facts) if o.get("value") else None
            if r:
                name, version, resolved, rest = r
                row = cat.get((name, resolved)) if resolved else None
                o.update(name=name, version=version, resolved_version=resolved, rest=rest,
                         uses_current=version == "current", in_catalog=row is not None)
                if row:
                    o.update(verification=row.get("verification"), compat=row.get("compat"),
                             location=row.get("location") or "local")
            elif not o.get("dynamic"):
                continue
            occ.append(o)
    return {"occ": occ, "refs": group_refs(occ), "files": len(files), "truncated": truncated}


REF_KEYS = ("kind", "name", "version", "resolved_version", "uses_current", "var", "tool", "source", "value",
            "dynamic", "in_catalog", "verification", "compat", "location")


def group_refs(occ):
    groups = {}
    for o in occ:
        key = tuple(str(o.get(k)) for k in REF_KEYS)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {k: o[k] for k in REF_KEYS if o.get(k) is not None}
            g["where"], g["count"] = [], 0
        g["count"] += 1
        if len(g["where"]) < 5:
            g["where"].append("%s:%d" % (o["file"], o["line"]))
    return sorted(groups.values(), key=lambda g: (g.get("name") or "~", g["kind"], g["where"][0]))


def summarize(refs):
    dbs = {}
    for r in refs:
        if r["kind"] == "personal" or not r.get("name"):
            continue
        d = dbs.setdefault(r["name"], {"versions": [], "uses_current": False, "via_variable": []})
        v = r.get("resolved_version")
        if v and v not in d["versions"]:
            d["versions"].append(v)
        d["uses_current"] |= bool(r.get("uses_current"))
        if r.get("var") and r["var"] not in d["via_variable"]:
            d["via_variable"].append(r["var"])
    return dbs


# ----------------------------- 项目数据库清单 databases.lock.json -----------------------------

ENTRY_KEYS = ("name", "version", "path", "env", "fingerprint", "verification", "tool", "locked_at", "locked_by")


def read_lock(top):
    """-> (清单, 错误)。没有清单 -> (None, None)；读不懂 -> (None, 原因)。"""
    p = os.path.join(top, LOCK_FILE)
    if not os.path.lexists(p):
        return None, None
    try:
        with open(p, encoding="utf-8") as f:
            raw = f.read()
        d = json.loads(raw)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        return None, "读不了 %s：%s" % (LOCK_FILE, e)
    if not isinstance(d, dict) or d.get("schema") != LOCK_SCHEMA or not isinstance(d.get("databases"), list):
        return None, "%s 格式不对：要有 \"schema\": %d 和 databases 列表" % (LOCK_FILE, LOCK_SCHEMA)
    entries = {}
    for e in d["databases"]:
        if not isinstance(e, dict) or not valid_component(e.get("name")) or not valid_component(e.get("version")) \
                or e.get("version") == "current":
            return None, "%s 里有一项格式不对（name / version 缺失、写了 current 或带了不合法字符）：%s" % (
                LOCK_FILE, json.dumps(e, ensure_ascii=False)[:120])
        if e["name"] in entries:
            return None, "%s 里 %s 写了两次" % (LOCK_FILE, e["name"])
        entries[e["name"]] = e
    return {"raw": d, "text": raw, "entries": entries}, None


def render_lock(raw, entries):
    d = {k: v for k, v in (raw or {}).items() if k not in ("schema", "databases")}
    out = {"schema": LOCK_SCHEMA}
    out.update(d)
    out["databases"] = [entries[k] for k in sorted(entries)]
    return json.dumps(out, ensure_ascii=False, indent=2) + "\n"


def lock_entry(name, version, facts, env_var=None, locked_at=None):
    row = catalog_index(facts).get((name, version)) or {}
    fp, _ = fingerprint_for(name, version, facts)
    tool = BOUND_DBS.get(name) or BOUND_DBS.get(row.get("recipe") or "")
    return {"name": name, "version": version, "path": DB_ROOT + "/" + name + "/" + version, "env": env_var,
            "fingerprint": fp, "verification": row.get("verification") or None,
            "tool": {"name": tool, "version": (facts.get("tools") or {}).get(tool)} if tool else None,
            "locked_at": locked_at, "locked_by": facts.get("user") or None}


def parse_overrides(items):
    out = {}
    for it in items or []:
        name, sep, version = it.partition("=")
        if not sep or not valid_component(name) or not valid_component(version) or version == "current":
            raise ValueError("--db 要写成 名=版本（版本不能是 current）：" + it)
        out[name] = version
    return out


def resolve_targets(sc, lock, overrides, facts):
    """每个库最后要锁的版本。-> (targets, conflicts, unresolved, env_vars)。
    规则：--db 指定的为准；用 current 的跟清单走，清单里没有就取今天的 current；写死版本的照写的；
    同一个库落到两个版本（含清单）就是冲突，要用户用 --db 定。"""
    entries = (lock or {}).get("entries") or {}
    want, where, unresolved, env_vars = {}, {}, [], {}
    for o in sc["occ"]:
        if o["kind"] == "personal":
            continue
        name = o.get("name")
        if not name or o.get("dynamic"):
            unresolved.append(o)
            continue
        if o.get("var"):
            env_vars.setdefault(name, o["var"])
        if name in overrides:
            v = overrides[name]
        elif o.get("uses_current"):
            v = entries[name]["version"] if name in entries else o.get("resolved_version")
        else:
            v = o.get("resolved_version")
        if not v:
            unresolved.append(o)
            continue
        want.setdefault(name, set()).add(v)
        where.setdefault((name, v), []).append("%s:%d" % (o["file"], o["line"]))
    for name, v in overrides.items():
        want.setdefault(name, set()).add(v)
    for name, e in entries.items():
        if name in want and name not in overrides:
            want[name].add(e["version"])
            where.setdefault((name, e["version"]), []).append(LOCK_FILE)
    targets, conflicts = {}, []
    for name, vs in sorted(want.items()):
        if len(vs) > 1:
            conflicts.append({"name": name, "versions": {v: where.get((name, v), [])[:5] for v in sorted(vs, key=vkey)},
                              "fix": "用 --db %s=<版本> 指定统一到哪个版本（换版本会影响结果，先问用户）" % name})
        else:
            targets[name] = next(iter(vs))
    return targets, conflicts, unresolved, env_vars


def _unresolved_note(occ):
    where = "%s:%d" % (occ["file"], occ["line"])
    if occ.get("var"):
        return "%s 的 $%s 判断不了指向哪个版本（在脚本里设成了动态值，或服务器上没有这个变量）" % (where, occ["var"])
    return "%s 的 %s 判断不了版本（current 指向读不到）" % (where, occ.get("value"))


# ----------------------------- check -----------------------------

def check(top, sc, lock, lock_error, facts, display=None):
    """错 / 警告 / 判断不了，退出码 0 正常 / 1 警告 / 2 错 / 3 判断不了（错优先，其次判断不了）。"""
    shown = display or tilde(top)
    errors, warnings, unknown, notes = [], [], [], []
    if facts.get("catalog") is None:
        unknown.append({"type": "facts_unavailable", "message": "库的信息取不到：" + "；".join(facts.get("errors") or ["未知原因"]),
                        "fix": "确认能连上服务器，再跑一次"})
    if lock_error:
        unknown.append({"type": "lock_invalid", "message": lock_error, "fix": "修好 %s，或删掉后重新 database lock" % LOCK_FILE})
    if sc["truncated"]:
        unknown.append({"type": "scan_truncated", "message": "项目里的脚本超过 %d 个，只扫了一部分" % MAX_FILES,
                        "fix": "对子目录分别 check"})
    cat = catalog_index(facts)
    entries = (lock or {}).get("entries") or {}
    have_catalog = facts.get("catalog") is not None
    tools = facts.get("tools") or {}
    nas_bad = ((facts.get("db_check") or {}).get("versions") or {})
    for name, e in sorted(entries.items()):
        v = e["version"]
        row = cat.get((name, v))
        if have_catalog and row is None:
            errors.append({"type": "lock_version_missing", "name": name, "version": v,
                           "message": "清单里的 %s %s 在公共区已经不存在" % (name, v),
                           "fix": "database find %s 看现有版本，和用户商量换哪个（换版本会影响结果）" % name})
            continue
        fp, info = fingerprint_for(name, v, facts)
        if not e.get("fingerprint"):
            warnings.append({"type": "no_fingerprint", "name": name, "version": v,
                             "message": "清单里 %s %s 没有指纹，只核对了版本号" % (name, v),
                             "fix": "database lock %s --db %s=%s 重新锁一次补上指纹" % (shown, name, v)})
        elif fp is None:
            if have_catalog:
                unknown.append({"type": "fingerprint_unavailable", "name": name, "version": v,
                                "message": "%s %s 的指纹查不到：%s" % (name, v, info.get("error")),
                                "fix": "过一会儿再 check；一直查不到请管理员看这个版本的文件清单"})
        elif fp != e["fingerprint"]:
            errors.append({"type": "fingerprint_changed", "name": name, "version": v,
                           "message": "%s %s 的指纹变了：清单 %s…，现在 %s…（库的内容和锁定时不一样）" % (
                               name, v, str(e["fingerprint"])[:20], fp[:20]),
                           "fix": "告诉用户结果可能不可复现，请管理员说明这个版本改过什么"})
        elif fp.startswith("partial:"):
            warnings.append({"type": "fingerprint_partial", "name": name, "version": v,
                             "message": "%s %s 的指纹不完整（有文件缺校验值），只能部分核对" % (name, v)})
        if info.get("match") is False:
            warnings.append({"type": "manifest_changed", "name": name, "version": v,
                             "message": "服务器上 %s %s 的文件清单和登记时的指纹对不上" % (name, v),
                             "fix": "请管理员核对这个版本"})
        if row and row.get("compat") == "fail":
            errors.append({"type": "compat_fail", "name": name, "version": v,
                           "message": "%s %s 和公共区的工具对不上（compat=fail：%s）" % (name, v, row.get("compat_tool") or "?"),
                           "fix": "database find %s 换一个 compat=pass 的版本，和用户商量" % name})
        t = e.get("tool") or {}
        if t.get("name") and t.get("version") and tools.get(t["name"]) and tools[t["name"]] != t["version"]:
            warnings.append({"type": "tool_changed", "name": name, "version": v,
                             "message": "公共区的 %s 从 %s 变成了 %s：同一个库，新版工具的结果可能不一样" % (
                                 t["name"], t["version"], tools[t["name"]])})
        if (name + "/" + v) in nas_bad:
            warnings.append({"type": "nas_check", "name": name, "version": v,
                             "message": "NAS 每日核对报告 %s %s 有问题（%s）" % (name, v, nas_bad[name + "/" + v]),
                             "fix": "请管理员看每日核对结果"})
        if (row or {}).get("verification") == "local-copy" or e.get("verification") == "local-copy":
            warnings.append({"type": "local_copy", "name": name, "version": v,
                             "message": "%s %s 是收编的个人副本（未与官方核对）" % (name, v)})
    if lock is None and not lock_error and any(o["kind"] != "personal" for o in sc["occ"]):
        warnings.append({"type": "no_lock", "message": "项目里还没有 %s" % LOCK_FILE,
                         "fix": "database lock %s 预演，用户同意后写一份并提交进 git" % shown})
    used = set()
    for r in sc["refs"]:
        where = "、".join(r["where"]) + ("（共 %d 处）" % r["count"] if r["count"] > len(r["where"]) else "")
        name = r.get("name")
        if r["kind"] == "personal":
            warnings.append({"type": "personal_copy", "name": name, "where": r["where"],
                             "message": "引用了个人目录里的库 %s：%s" % (r["value"], where),
                             "fix": "database compare <这个目录> %s 看公共区有没有同一份；有就改用公共版本" % (name or "<库名>")})
            continue
        if r.get("dynamic"):
            unknown.append({"type": "unresolved", "where": r["where"],
                            "message": "%s：$%s 判断不了指向哪个版本（脚本里设成了动态值，或服务器上没有这个变量）" % (
                                where, r.get("var") or "?"), "fix": "改成写死的版本路径 %s/<库>/<版本>" % DB_ROOT})
            continue
        v = r.get("resolved_version")
        if name:
            used.add(name)
        locked = entries.get(name, {}).get("version")
        if r.get("uses_current"):
            target = locked or v
            path = DB_ROOT + "/" + name + "/" + (target or "<版本>") + (r.get("rest") or "")
            if r["kind"] == "tool-default":
                msg = "%s 调用了 %s，但没设 %s：它会读服务器的默认值 %s（current）" % (where, r["tool"], r["var"], r.get("value"))
                fix = "在脚本开头 export %s=%s，或 database pin %s 自动改" % (r["var"], path, shown)
            elif r["kind"] == "var" and r.get("source") == "server-env":
                msg = "%s 用了默认变量 $%s：服务器上它指向 %s（current）" % (where, r["var"], r.get("value"))
                fix = "在脚本开头 export %s=%s，或 database pin %s 自动改" % (r["var"], path, shown)
            else:
                msg = "%s 用了 current（%s）%s" % (where, r.get("value"), "，在 db-cache use 里" if r["kind"] == "db-cache" else "")
                fix = "改成 %s，或 database pin %s 自动改" % (path, shown)
            errors.append({"type": "uses_current", "name": name, "where": r["where"], "message": msg,
                           "fix": fix + ("（清单锁的是 %s）" % locked if locked else "（%s 是今天的 current）" % target if target else "")})
            if not v and not locked:
                unknown.append({"type": "unresolved", "where": r["where"], "message": "%s 的 current 指向读不到" % where})
            continue
        if have_catalog and not r.get("in_catalog"):
            errors.append({"type": "script_version_missing", "name": name, "version": v, "where": r["where"],
                           "message": "%s 用的 %s %s 在公共区不存在" % (where, name, v),
                           "fix": "database find %s 看现有版本" % name})
            continue
        if locked and locked != v:
            errors.append({"type": "version_mismatch", "name": name, "version": v, "where": r["where"],
                           "message": "%s 用的 %s %s 和清单的 %s 不一致" % (where, name, v, locked),
                           "fix": "database pin %s --db %s=%s 把脚本改成清单的版本；确实要换版本就 database lock %s --db %s=%s" % (
                               shown, name, locked, shown, name, v)})
            continue
        if lock is not None and not locked:
            warnings.append({"type": "not_in_lock", "name": name, "version": v, "where": r["where"],
                             "message": "%s 用的 %s %s 不在清单里" % (where, name, v),
                             "fix": "database lock %s 补进清单" % shown})
        if not locked and r.get("compat") == "fail":
            errors.append({"type": "compat_fail", "name": name, "version": v, "where": r["where"],
                           "message": "%s 用的 %s %s 和公共区的工具对不上（compat=fail）" % (where, name, v),
                           "fix": "database find %s 换一个 compat=pass 的版本" % name})
        if not locked and r.get("verification") == "local-copy":
            warnings.append({"type": "local_copy", "name": name, "version": v, "where": r["where"],
                             "message": "%s 用的 %s %s 是收编的个人副本（未与官方核对）" % (where, name, v)})
    for name in sorted(set(entries) - used):
        notes.append("清单里的 %s 在脚本里没找到引用（可能写在没扫到的地方，或者已经不用了）" % name)
    code = 2 if errors else 3 if unknown else 1 if warnings else 0
    summary = {0: "没有问题", 1: "%d 个警告" % len(warnings), 2: "%d 个错误" % len(errors),
               3: "有 %d 处判断不了" % len(unknown)}[code]
    return code, {"path": shown, "lock_file": LOCK_FILE if lock is not None or lock_error else None,
                  "exit_code": code, "summary": summary, "errors": errors, "warnings": warnings,
                  "undetermined": unknown, "notes": notes, "refs": sc["refs"], "files_scanned": sc["files"],
                  "codes": "0 正常 / 1 只有警告 / 2 有错 / 3 判断不了"}


# ----------------------------- lock / pin -----------------------------

def _set_statement(langs, var, value):
    """-> (语句, 插入方式) 或 None（这种文件不自动插，要人改）。"""
    if "doc" in langs or "data" in langs:
        return None
    if langs == ["sh"]:
        return "export %s=%s" % (var, shlex.quote(value)), "sh"
    if "py" in langs:
        return "os.environ[%s] = %s" % (json.dumps(var), json.dumps(value)), "py"
    if langs == ["r"]:
        return "Sys.setenv(%s = %s)" % (var, json.dumps(value)), "r"
    return None


def _insert(text, stmts, how):
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    block = "".join(s + nl for s in ["# 锁定数据库版本（database pin）"] + stmts)
    if how == "py":
        i = next((k + 1 for k, ln in enumerate(lines) if re.match(r"import\s+(?:[\w.]+\s*,\s*)*os\b(?!\.)", ln)), None)
        if i is None:
            return None
    else:
        i = 1 if lines and lines[0].startswith("#!") else 0
        while i < len(lines):
            s = lines[i].strip()
            if s == "" or s.startswith("#") or (how == "sh" and re.match(r"set\s+[-+]", s)):
                i += 1
                continue
            break
        while i > 0 and lines[i - 1].strip() == "":       # 插在空行前，紧挨着头部
            i -= 1
    if lines and i == len(lines) and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += nl
    return "".join(lines[:i] + [block] + lines[i:])


def _replace_versions(text, rel, targets, overrides):
    ipynb = rel.lower().endswith(".ipynb")
    out = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        end = line[len(body):]
        code = body if ipynb else strip_comment(body)
        rest = body[len(code):]

        def sub(m):
            name, version = m.group(1), m.group(2).rstrip(".")
            t = targets.get(name)
            if t and (version == "current" or (name in overrides and version != t)):
                s = m.group(0)
                a = m.start(2) - m.start(0)
                return s[:a] + t + s[a + len(version):]
            return m.group(0)
        out.append(LIT_RE.sub(sub, code) + rest + end)
    return "".join(out)


def plan_changes(top, sc, targets, overrides, facts):
    """-> (改动 {相对路径: (旧, 新)}, 要人改的 [说明])。"""
    env = facts.get("env") or {}
    by_file = {}
    for o in sc["occ"]:
        by_file.setdefault(o["file"], []).append(o)
    changes, manual = {}, []
    for rel, occs in sorted(by_file.items()):
        full = os.path.join(top, rel)
        try:
            with open(full, encoding="utf-8", newline="") as f:
                old = f.read()
        except (OSError, UnicodeDecodeError):
            manual.append("%s 不是 UTF-8 文本，没自动改" % rel)
            continue
        new = _replace_versions(old, rel, targets, overrides)
        need = {}
        for o in occs:
            name = o.get("name")
            if o["kind"] == "tool-default" and o.get("dynamic"):
                manual.append("%s:%d 调用了 %s，服务器上没有 %s 的默认值：在调用前 export %s=%s/%s/<版本>" % (
                    rel, o["line"], o["tool"], o["var"], o["var"], DB_ROOT, BOUND_TOOLS[o["tool"]][1]))
                continue
            if o["kind"] not in ("var", "tool-default") or o.get("source") != "server-env" or name not in targets:
                continue
            if not (o.get("uses_current") or name in overrides):
                continue
            p = parse_db_path(env.get(o["var"]) or "")
            if not p:
                continue
            need.setdefault(o["var"], DB_ROOT + "/" + p[0] + "/" + targets[name] + p[2])
        if need:
            spec = [_set_statement(occs[0]["langs"], var, value) for var, value in sorted(need.items())]
            if any(s is None for s in spec):
                for var, value in sorted(need.items()):
                    manual.append("%s：在用到 %s 之前把它设成 %s（这种文件不自动插语句）" % (rel, var, value))
            else:
                inserted = _insert(new, [s for s, _ in spec], spec[0][1])
                if inserted is None:
                    for var, value in sorted(need.items()):
                        manual.append("%s：没找到 import os，在用到 %s 之前写 os.environ[%s] = %s" % (
                            rel, var, json.dumps(var), json.dumps(value)))
                else:
                    new = inserted
        if new != old:
            changes[rel] = (old, new)
    return changes, manual


def _diff(rel, old, new):
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile="a/" + rel, tofile="b/" + rel))


def _write_atomic(path, text):
    mode = None
    try:
        mode = os.stat(path).st_mode & 0o7777
    except OSError:
        pass
    tmp = "%s.spark-advisor-%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def lock_or_pin(op, top, sc, lock, facts_fn, facts, overrides, yes=False, plan_id=None, jobs=None, display=None):
    """lock：只写 databases.lock.json；pin：先改脚本（版本路径 / export），再写清单。都先预演给 plan_id。"""
    shown = display or tilde(top)
    targets, conflicts, unresolved, env_vars = resolve_targets(sc, lock, overrides, facts)
    entries = dict((lock or {}).get("entries") or {})
    refuse = []
    if facts.get("catalog") is None:
        refuse.append("库的信息取不到：" + "；".join(facts.get("errors") or ["未知原因"]))
    cat = catalog_index(facts)
    for name, v in sorted(targets.items()):
        if facts.get("catalog") is not None and (name, v) not in cat:
            refuse.append("%s %s 在公共区不存在（database find %s 看现有版本）" % (name, v, name))
    if conflicts:
        refuse.append("有库在脚本 / 清单里对应了不止一个版本，见 conflicts")
    if op == "pin" and jobs is not None:
        if jobs.get("error"):
            refuse.append("查不了队列（%s），不确定有没有作业在用这些脚本" % jobs["error"])
        elif jobs.get("jobs"):
            refuse.append("项目下有运行或排队的作业（%s）：排队的作业启动时会读到改过的脚本，等它们结束再改" %
                          "、".join(j["job_id"] for j in jobs["jobs"]))
    new_pairs = sorted((n, v) for n, v in targets.items() if entries.get(n, {}).get("version") != v)
    if new_pairs and not refuse:
        facts = facts_fn(new_pairs, True)
    changes, manual = ({}, [])
    if op == "pin" and not refuse:
        changes, manual = plan_changes(top, sc, targets, overrides, facts)
    new_entries = dict(entries)
    warnings = []
    stamp = now_iso()           # 预演时先填上，真写时换成写的那一刻；plan_id 不含它
    for name, v in new_pairs:
        new_entries[name] = lock_entry(name, v, facts, env_vars.get(name), stamp)
        fp, info = fingerprint_for(name, v, facts)
        if fp is None:
            warnings.append("%s %s 查不到指纹（%s）：清单里先不写，check 只能核对版本号" % (name, v, info.get("error")))
        elif info.get("source") == "meta":
            warnings.append("%s %s 的指纹取自登记信息（服务器的指纹命令不可用：%s）" % (name, v, info.get("server_error")))
    old_text = (lock or {}).get("text")
    new_text = render_lock((lock or {}).get("raw"), new_entries) if (new_pairs or old_text is None) and new_entries else old_text
    basis = {"op": op, "files": {rel: [sha_text(o), sha_text(n)] for rel, (o, n) in sorted(changes.items())},
             "lock_old": sha_text(old_text) if old_text is not None else None,
             "lock_new": [{k: e.get(k) for k in ENTRY_KEYS if k != "locked_at"} for _, e in sorted(new_entries.items())],
             "manual": manual}
    pid = None if refuse else hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=True).encode()).hexdigest()[:12]
    res = {"path": shown, "op": op, "targets": targets, "conflicts": conflicts, "refuse": refuse,
           "unresolved": [_unresolved_note(o) for o in unresolved][:20], "warnings": warnings,
           "changes": [{"file": rel, "diff": _diff(rel, o, n)} for rel, (o, n) in sorted(changes.items())],
           "manual": manual, "lock_file": LOCK_FILE,
           "lock_diff": _diff(LOCK_FILE, old_text or "", new_text or "") if new_text != old_text else "",
           "plan_id": pid, "executed": False}
    if op == "lock" and any(o.get("uses_current") for o in sc["occ"]):
        res["pin_note"] = "脚本里还有 current 或默认变量：清单锁好后接着 database pin，把脚本改成版本路径"
    if refuse:
        return 1, res
    if not changes and new_text == old_text:
        res["note"] = ("项目里没找到公共数据库的引用，不用写清单" if not targets and not entries
                       else "不用改：脚本和清单已经一致" if op == "pin" else "清单已经是最新的")
        return 0, res
    if not yes:
        res["next"] = "把 changes / lock_diff 给用户看，用户同意后带 --yes --plan-id %s 再跑一次" % pid
        return 0, res
    if plan_id != pid:
        return 1, {"error": "预演之后项目或库的信息变了（plan_id 对不上），重新预演给用户看", "plan_id": pid, "path": shown}
    stamp = now_iso()
    for name, _ in new_pairs:
        new_entries[name]["locked_at"] = stamp
    written = []
    for rel, (_, n) in sorted(changes.items()):
        _write_atomic(os.path.join(top, rel), n)
        written.append(rel)
    if new_text != old_text:
        _write_atomic(os.path.join(top, LOCK_FILE), render_lock((lock or {}).get("raw"), new_entries))
        written.append(LOCK_FILE)
    res.update(executed=True, written=written,
               next="把改动提交进 git（%s 也要提交）；再跑一次 database check 确认" % LOCK_FILE)
    return 0, res


def project_root(path):
    """项目根目录：在 git 仓库里就用仓库根（不是 home 本身时）。"""
    top = os.path.realpath(os.path.expanduser(path))
    try:
        p = subprocess.run(["git", "-C", top, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                           timeout=20, env=dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0"),
                           stdin=subprocess.DEVNULL)
        root = os.path.realpath(p.stdout.strip()) if p.returncode == 0 and p.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        root = None
    if root and root != os.path.realpath(os.path.expanduser("~")) and (top == root or top.startswith(root + os.sep)):
        return root
    return top


def project_jobs(top):
    """服务器上：本人在这个项目下运行或排队的作业。"""
    try:
        p = subprocess.run(["squeue", "-h", "-u", whoami(), "-o", "%i %T %Z"], capture_output=True, text=True,
                           timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": str(e)[:200]}
    if p.returncode:
        return {"error": (p.stderr or "squeue 失败").strip()[:200]}
    jobs = []
    for line in p.stdout.splitlines():
        f = line.split(None, 2)
        if len(f) == 3:
            wd = os.path.normpath(f[2])
            if wd == top or wd.startswith(top + "/"):
                jobs.append({"job_id": f[0], "state": f[1], "workdir": f[2]})
    return {"jobs": jobs}


def project_op(op, path, facts_fn, dbs=(), yes=False, plan_id=None, jobs_fn=None, display=None):
    """scan / check / lock / pin 的总入口。facts_fn(pairs, fingerprints) 返回服务器上的库信息。-> (退出码, 结果)"""
    if not os.path.isdir(os.path.expanduser(path)):
        return 2, {"error": "目录不存在：" + path}
    top = project_root(path)
    shown = display(top) if display else tilde(top)
    try:
        overrides = parse_overrides(dbs)
    except ValueError as e:
        return 2, {"error": str(e)}
    lock, lock_error = read_lock(top)
    if op == "check":
        pairs = sorted((n, e["version"]) for n, e in ((lock or {}).get("entries") or {}).items())
        facts = facts_fn(pairs, True)
        return check(top, scan(top, facts), lock, lock_error, facts, shown)
    facts = facts_fn([], False)
    sc = scan(top, facts)
    if op == "scan":
        res = {"path": shown, "databases": summarize(sc["refs"]), "refs": sc["refs"], "files_scanned": sc["files"],
               "lock_file": LOCK_FILE if lock is not None else None, "facts_errors": facts.get("errors") or []}
        if sc["truncated"]:
            res["warning"] = "项目里的脚本超过 %d 个，只扫了一部分" % MAX_FILES
        if any(r.get("uses_current") for r in sc["refs"]):
            res["note"] = ("current 和默认变量指向今天的版本，以后会变；分析要锁版本：database lock 写清单，"
                           "database pin 把脚本改成版本路径")
        return (3 if facts.get("catalog") is None else 0), res
    if lock_error:
        return 1, {"path": shown, "refuse": [lock_error], "plan_id": None, "executed": False}
    jobs = jobs_fn(top) if (jobs_fn and op == "pin") else None
    return lock_or_pin(op, top, sc, lock, facts_fn, facts, overrides, yes, plan_id, jobs, shown)


# ----------------------------- 个人副本：指纹与比对 -----------------------------

# 这段代码既在这里用，也原样嵌进比对大目录的 Slurm 作业脚本里（作业里没有本文件）。
DIR_HASH_CODE = r'''
import hashlib, json, os, re, stat, sys, time

def sha256_file(path):
    """自己的文件用 O_NOATIME 读：比对不把文件记成「刚读过」（spark-cleanup dbscan 按最近被读提醒）。"""
    h = hashlib.sha256()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOATIME", 0))
    except PermissionError:
        fd = os.open(path, os.O_RDONLY)
    with os.fdopen(fd, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

def dir_rows(root, max_files=None, max_bytes=None, hash_files=True):
    """-> {rows, files, bytes, skipped, not_owned, truncated}：每个普通文件的（相对路径, 字节数, sha256），不跟随链接。"""
    rows, skipped, total, not_owned, stack = [], [], 0, 0, [""]
    uid = os.getuid() if hasattr(os, "getuid") else None
    while stack:
        rel = stack.pop()
        try:
            entries = sorted(os.scandir(os.path.join(root, rel) if rel else root), key=lambda e: e.name)
        except OSError as e:
            skipped.append({"path": rel or ".", "why": "读不了目录：%s" % (e.strerror or e)})
            continue
        for e in entries:
            r = rel + "/" + e.name if rel else e.name
            try:
                st = e.stat(follow_symlinks=False)
            except OSError as err:
                skipped.append({"path": r, "why": "stat 失败：%s" % (err.strerror or err)})
                continue
            if uid is not None and st.st_uid != uid:
                not_owned += 1
            if stat.S_ISDIR(st.st_mode):
                stack.append(r)
            elif stat.S_ISREG(st.st_mode):
                total += st.st_size
                if max_files is not None and (len(rows) >= max_files or total > max_bytes):
                    return {"rows": rows, "files": len(rows), "bytes": total, "skipped": skipped,
                            "not_owned": not_owned, "truncated": True}
                row = {"rel": r, "bytes": st.st_size}
                if hash_files:
                    try:
                        row["sha256"] = sha256_file(os.path.join(root, r))
                    except OSError as err:
                        row["sha256"] = ""
                        skipped.append({"path": r, "why": "读不了：%s" % (err.strerror or err)})
                rows.append(row)
            else:
                skipped.append({"path": r, "why": "链接" if stat.S_ISLNK(st.st_mode) else "特殊文件"})
    return {"rows": rows, "files": len(rows), "bytes": total, "skipped": skipped, "not_owned": not_owned,
            "truncated": False}

def rows_fingerprint(rows):
    """同服务器的指纹：排序后的（相对路径, 十进制字节数, 小写 sha256）逐行 UTF-8，整体 sha256；缺校验值加 partial:。"""
    partial, items = False, []
    for r in rows:
        sha = str(r.get("sha256") or "").lower()
        if not re.fullmatch("[0-9a-f]{64}", sha):
            sha, partial = "", True
        items.append((r["rel"], int(r["bytes"]), sha))
    if not items:
        partial = True
    text = "".join("%s\t%d\t%s\n" % x for x in sorted(items))
    return ("partial:" if partial else "") + hashlib.sha256(text.encode("utf-8")).hexdigest()

def hash_job(root, out):
    started = time.time()
    res = dir_rows(root)
    res.update(dir=os.path.realpath(root), fingerprint=rows_fingerprint(res["rows"]),
               seconds=round(time.time() - started, 1), finished=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    with open(out + ".part", "w", encoding="utf-8") as f:
        json.dump(res, f)
    os.replace(out + ".part", out)
'''
exec(DIR_HASH_CODE)


def read_meta(path):
    try:
        with open(path, encoding="utf-8") as f:
            return dict(ln.rstrip("\n").split("=", 1) for ln in f if "=" in ln and not ln.startswith("#"))
    except OSError:
        return {}


def read_tsv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def public_rows(name, version, root=DB_ROOT):
    """公共版本的文件（库内相对路径）和包成员。-> (meta, 文件行, 成员行, 有没有文件清单)"""
    base = os.path.join(root, name)
    meta = read_meta(os.path.join(base, version + ".meta"))
    try:
        files = read_tsv(os.path.join(base, version + ".files.tsv"))
    except OSError:
        files = None
    try:
        members = read_tsv(os.path.join(base, version + ".members.tsv"))
    except OSError:
        members = []
    prefixes = [r.rstrip("/") + "/" for r in meta.get("nas", "").split()] + [os.path.join(base, version) + "/"]
    link = os.path.join(base, version)
    if os.path.islink(link):       # 同服务器的指纹命令：只取链接目标的字符串，不进 NAS
        prefixes.append(os.path.normpath(os.path.join(base, os.readlink(link))).rstrip("/") + "/")
    prefixes.sort(key=len, reverse=True)
    archives = {m.get("archive") for m in members}
    rows = []
    for r in files or []:
        p = r.get("path") or ""
        rel = next((p[len(x):] for x in prefixes if p.startswith(x)), os.path.basename(p))
        if rel in archives:
            continue          # 包本身不比，比包里的成员
        rows.append({"rel": rel, "bytes": int(r.get("bytes") or 0), "sha256": (r.get("sha256") or "").lower()})
    mrows = [{"rel": m.get("rel") or "", "bytes": int(m.get("bytes") or 0), "sha256": (m.get("sha256") or "").lower()}
             for m in members]
    return meta, rows, mrows, files is not None


def match_rows(mine, public):
    idx = {r["rel"]: r for r in public}
    res = {"matched": 0, "different": [], "extra": [], "unknown": [], "missing": []}
    seen = set()
    for r in mine:
        p = idx.get(r["rel"])
        if p is None:
            res["extra"].append(r["rel"])
            continue
        seen.add(r["rel"])
        if p["bytes"] != int(r["bytes"]):
            res["different"].append(r["rel"])
        elif not re.fullmatch("[0-9a-f]{64}", p["sha256"] or ""):
            res["unknown"].append(r["rel"])
        elif p["sha256"] != r.get("sha256"):
            res["different"].append(r["rel"])
        else:
            res["matched"] += 1
    res["missing"] = sorted(rel for rel in idx if rel not in seen)
    return res


def compare_version(rows, fp, name, version, root=DB_ROOT):
    meta, frows, mrows, have_manifest = public_rows(name, version, root)
    tops = {m["rel"].split("/", 1)[0] for m in mrows if "/" in m["rel"]}
    top = next(iter(tops)) if len(tops) == 1 and all("/" in m["rel"] for m in mrows) else None
    # 两种层次都比，记下是哪一层对上的：收编时服务器只剥成员共有的「<版本>/」，
    # 上游包自带的顶层目录（如 checkv-db-v1.5/）不剥，--path 指错一层就会被判成不同
    variants = [("above_version" if top == version else "same", frows + mrows)]
    if top:
        variants.append(("same" if top == version else "inside_top",
                         frows + [dict(m, rel=m["rel"][len(top) + 1:]) for m in mrows]))
    best, level = max(((match_rows(rows, v), lv) for lv, v in variants), key=lambda x: x[0]["matched"])
    same = have_manifest and best["matched"] > 0 and not (best["different"] or best["extra"] or best["missing"]
                                                         or best["unknown"])
    out = {"version": version, "layout": meta.get("layout") or "files", "verification": meta.get("verification"),
           "public_fingerprint": meta.get("fingerprint"), "exact": bool(meta.get("fingerprint")) and meta.get("fingerprint") == fp,
           "same_content": same, "matched": best["matched"], "manifest": have_manifest, "level": level, "top_dir": top}
    for k in ("different", "extra", "missing", "unknown"):
        out[k + "_count"] = len(best[k])
        out[k] = best[k][:10]
    return out


def compare(path, name, version=None, yes=False, result=None, partition="main", root=DB_ROOT, submit_fn=None):
    """个人副本和公共版本比对。小目录当场算；大目录交低优先级作业（要 --yes），算完用 --result <作业号> 取。"""
    d = os.path.realpath(os.path.expanduser(path))
    if not valid_component(name) or (version and not valid_component(version)):
        return 2, {"error": "库名 / 版本不合法"}
    if not os.path.isdir(d):
        return 2, {"error": "目录不存在：" + path}
    versions = [version] if version else sorted(
        (f[:-5] for f in os.listdir(os.path.join(root, name)) if f.endswith(".meta")) if os.path.isdir(os.path.join(root, name)) else [],
        key=vkey)
    if version and not os.path.exists(os.path.join(root, name, version + ".meta")):
        return 2, {"error": "公共区没有 %s %s" % (name, version), "next": "database find %s 看现有版本" % name}
    if not versions:
        return 2, {"error": "公共区没有 %s" % name, "next": "database find %s；没有就可以 database adopt 收编" % name}
    outdir = os.path.expanduser(COMPARE_DIR)
    if result:
        if not re.fullmatch(r"[0-9]+", result):
            return 2, {"error": "--result 要写作业号"}
        rf = os.path.join(outdir, result + ".json")
        if not os.path.exists(rf):
            state = _job_state(result)
            return (0 if state in ("PENDING", "RUNNING", "CONFIGURING", "COMPLETING") else 1), {
                "job_id": result, "state": state or "查不到", "finished": False,
                "log": tilde(os.path.join(outdir, "db-compare-%s.log" % result)),
                "next": "还在算，过一会儿再取" if state in ("PENDING", "RUNNING", "CONFIGURING", "COMPLETING")
                else "作业结束了但没有结果：看日志"}
        with open(rf, encoding="utf-8") as f:
            got = json.load(f)
        if os.path.realpath(got.get("dir") or "") != d:
            return 2, {"error": "这个作业算的是 %s，不是 %s" % (tilde(got.get("dir") or "?"), tilde(d))}
        probe = got
    else:
        probe = dir_rows(d, SMALL_FILES, SMALL_BYTES, hash_files=False)
        if probe["truncated"]:
            script = compare_job_script(d, partition)
            plan = {"dir": tilde(d), "name": name, "large": True,
                    "size_note": "超过 %d 个文件或 %.0f GB（只数到这里），在登录节点上算太慢" % (SMALL_FILES, SMALL_BYTES / 1e9),
                    "job": {"cpus": 1, "mem": "2G", "nice": LOW_PRIORITY_NICE, "partition": partition},
                    "sbatch_script": script, "submitted": False}
            if not yes:
                plan["next"] = "交一个低优先级作业算指纹（1 核 2G，排在普通作业之后）：用户同意后加 --yes"
                return 0, plan
            os.makedirs(outdir, mode=0o700, exist_ok=True)
            job = (submit_fn or _sbatch)(script)
            plan.update(submitted=True, job_id=job, result_file=tilde(os.path.join(outdir, job + ".json")),
                        next="database compare %s %s%s --result %s 取结果" % (tilde(d), name, " " + version if version else "", job))
            return 0, plan
        probe = dir_rows(d)
        probe["fingerprint"] = rows_fingerprint(probe["rows"])
    rows, fp = probe["rows"], probe["fingerprint"]
    results = [compare_version(rows, fp, name, v, root) for v in versions]
    match = next(({"version": r["version"], "kind": "exact" if r["exact"] else "same_content", "level": r["level"]}
                  for r in results if r["exact"] or r["same_content"]), None)
    closest = next(r for r in results if r["version"] == match["version"]) if match else max(results, key=lambda r: r["matched"])
    out = {"dir": tilde(d), "name": name, "files": probe["files"], "bytes": probe["bytes"], "fingerprint": fp,
           "skipped": probe["skipped"][:10], "skipped_count": len(probe["skipped"]),
           "not_owned": probe.get("not_owned", 0), "versions": results, "match": match}
    adopt_path = d
    if closest["matched"]:
        adopt_path = {"inside_top": os.path.dirname(d), "above_version": os.path.join(d, closest["version"])}.get(
            closest["level"], d)
        out.update(level=closest["level"], level_note=_level_note(closest, d), adopt_path=tilde(adopt_path))
    if match:
        out["next"] = ("公共区的 %s %s 和这份内容一样：不用收编，脚本改用 %s/%s/%s（database pin），确认脚本已提交、"
                       "没有作业在用之后，可以用 spark-cleanup 删个人副本（先问用户）" % (
                           name, match["version"], DB_ROOT, name, match["version"]))
    else:
        out["next"] = ("和公共区的版本都对不上：要放进 NAS 公共库，就 database adopt %s <版本> --path %s"
                       "（和官方包同一层%s；对不上官方版本会登记成 <版本>-local-<用户名>）" % (
                           name, tilde(adopt_path), "：" + out["level_note"] if closest["matched"] else ""))
    if probe["skipped"] or probe.get("not_owned"):
        out["adopt_note"] = "目录里有链接、特殊文件、读不了的文件或不属于你的文件：adopt 会拒绝，先处理这些"
    return 0, out


def _level_note(r, d):
    if r["level"] == "inside_top":
        note = "官方包的文件都在顶层目录 %s/ 里，这份是这层里面的内容，--path 要指到包含 %s/ 的上一层" % (r["top_dir"], r["top_dir"])
        if os.path.basename(d) != r["top_dir"]:
            note += "（官方这层叫 %s，这份的目录叫 %s，名字要一样）" % (r["top_dir"], os.path.basename(d))
        return note + "，上一层里不能有别的文件"
    if r["level"] == "above_version":
        return "这份比官方包多一层 %s/，--path 要指到这一层里面" % r["version"]
    return "和官方包在同一层，--path 就指这个目录"


def compare_job_script(d, partition):
    return "\n".join([
        "#!/bin/bash -l", "#SBATCH --job-name=db-compare", "#SBATCH --partition=%s" % partition,
        "#SBATCH --cpus-per-task=1", "#SBATCH --mem=2G", "#SBATCH --nice=%d" % LOW_PRIORITY_NICE,
        "#SBATCH --chdir=%s" % os.path.expanduser(COMPARE_DIR), "#SBATCH --output=%x-%j.log",
        "set -eo pipefail",
        "python3 -I - %s \"$SLURM_JOB_ID.json\" <<'SPARK_DB_COMPARE_PY'" % shlex.quote(d),
        DIR_HASH_CODE.strip(), "hash_job(sys.argv[1], sys.argv[2])", "SPARK_DB_COMPARE_PY", ""])


def _sbatch(script):
    p = subprocess.run(["sbatch", "--parsable"], input=script, capture_output=True, text=True, timeout=60)
    job = p.stdout.strip().split(";")[0]
    if p.returncode or not re.fullmatch(r"[0-9]+", job):
        raise ValueError("sbatch 失败：" + (p.stderr or p.stdout).strip()[-300:])
    return job


def _job_state(job):
    for cmd in (["squeue", "-h", "-j", job, "-o", "%T"], ["sacct", "-n", "-X", "-j", job, "-o", "State"]):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
            s = p.stdout.split()
            if p.returncode == 0 and s:
                return s[0]
        except (OSError, subprocess.SubprocessError):
            continue
    return None


# ----------------------------- 收编个人副本：预演放进作业、提交用保存的预演 -----------------------------
# db-install adopt-plan 要给整个目录逐个文件算 sha256：大目录不在登录节点上做，交给低优先级作业。
# adopt-submit 只做轻量核对（目录还在、属主、文件数和总字节数和预演一致），逐个文件的校验在安装作业里由 worker 对指纹；
# 所以它要拿到预演的原文（stdin），预演结果按 plan_sha256 存在本人的 ADOPT_DIR 里。

def _err_line(text):
    """db-install 报错是 stderr 上的一行 JSON：取出 error。"""
    for line in reversed((text or "").strip().splitlines()):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("error"):
            return str(d["error"])
    return None


def _db_install_run(args, stdin=None, timeout=1800):
    """在服务器上以本人身份调 db-install（--json）。-> (退出码, 结果或 {"error": …})"""
    kw = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    try:
        p = subprocess.run([DB_INSTALL] + list(args) + ["--json"], capture_output=True, text=True, timeout=timeout, **kw)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, {"error": "调不了 db-install：%s" % str(e)[:300]}
    if p.returncode == 0:
        try:
            return 0, json.loads(p.stdout)
        except ValueError:
            return 1, {"error": "db-install 的输出不是 JSON：" + p.stdout.strip()[-200:]}
    return p.returncode, {"error": _err_line(p.stderr) or (p.stderr or p.stdout).strip()[-300:] or "db-install 失败"}


def _adopt_dir():
    d = os.path.expanduser(ADOPT_DIR)
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _save_plan(plan):
    sha = str(plan.get("plan_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        return None
    path = os.path.join(_adopt_dir(), "plan-%s.json" % sha)
    tmp = path + ".part"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def _plan_matches(plan, name, version, d):
    spec = plan.get("spec") or {}
    return (plan.get("action") == "adopt" and spec.get("name") == name and spec.get("version") == version
            and os.path.realpath(spec.get("path") or "/nonexistent") == d)


def adopt_job_script(d, name, version, partition):
    run = "%s adopt-plan %s %s --path %s --json" % (DB_INSTALL, shlex.quote(name), shlex.quote(version), shlex.quote(d))
    return "\n".join([
        "#!/bin/bash -l", "#SBATCH --job-name=db-adopt-plan", "#SBATCH --partition=%s" % partition,
        "#SBATCH --cpus-per-task=1", "#SBATCH --mem=2G", "#SBATCH --nice=%d" % LOW_PRIORITY_NICE,
        "#SBATCH --time=%s" % ADOPT_JOB_TIME, "#SBATCH --chdir=%s" % os.path.expanduser(ADOPT_DIR),
        "#SBATCH --output=%x-%j.log",
        run + ' > "$SLURM_JOB_ID.json.part" 2> "$SLURM_JOB_ID.err" && mv -- "$SLURM_JOB_ID.json.part" "$SLURM_JOB_ID.json"',
        ""])


def _adopt_result(job, name, version, d, state_fn=None):
    """预演作业的结果：算完了 -> 预演（同时按 plan_sha256 存好）；还在排队或在跑 -> finished=False；失败 -> 服务器的报错。"""
    if not re.fullmatch(r"[0-9]+", job or ""):
        return 2, {"error": "--result 要写作业号"}
    outdir = os.path.expanduser(ADOPT_DIR)
    rf, ef = os.path.join(outdir, job + ".json"), os.path.join(outdir, job + ".err")
    log = tilde(os.path.join(outdir, "db-adopt-plan-%s.log" % job))
    if os.path.exists(rf):
        with open(rf, encoding="utf-8") as f:
            plan = json.load(f)
        if not _plan_matches(plan, name, version, d):
            spec = plan.get("spec") or {}
            return 2, {"error": "作业 %s 预演的是 %s %s（%s），不是这次要收编的" % (
                job, spec.get("name"), spec.get("version"), tilde(spec.get("path") or "?"))}
        _save_plan(plan)
        return 0, {"mode": "job", "job_id": job, "finished": True, "plan": plan}
    state = (state_fn or _job_state)(job)
    if state is None or state in JOB_WAITING:
        return 0, {"mode": "job", "job_id": job, "finished": False, "state": state or "查不到", "log": log,
                   "next": "预演还在排队或在算：过一会儿用 --result %s 取" % job}
    err = None
    try:
        with open(ef, encoding="utf-8", errors="replace") as f:
            text = f.read()
        err = _err_line(text) or text.strip()[-300:] or None
    except OSError:
        pass
    return 1, {"mode": "job", "job_id": job, "finished": True, "state": state, "log": log,
               "error": err or "作业结束了但没有预演结果：看日志"}


def adopt_plan(path, name, version, plan_job=False, result=None, wait=0, partition="main",
               submit_fn=None, state_fn=None, sleep=time.sleep):
    """收编预演。目录不超过 1 GB：当场调 db-install adopt-plan；更大：给出低优先级作业的计划，--plan-job 才提交，
    提交后最多等 wait 秒，算完直接返回预演，没算完给作业号（之后 --result 取）。预演按 plan_sha256 存好，提交时用。"""
    if not valid_component(name) or not valid_component(version):
        return 2, {"error": "库名 / 版本不合法"}
    d = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(d):
        return 2, {"error": "目录不存在：" + path}
    if result:
        return _adopt_result(result, name, version, d, state_fn)
    probe = dir_rows(d, 10 ** 12, ADOPT_DIRECT_BYTES, hash_files=False)      # 只数大小，不读内容
    if not probe["truncated"]:
        rc, plan = _db_install_run(["adopt-plan", name, version, "--path", d])
        if rc:
            return rc, {"mode": "direct", "error": plan.get("error")}
        _save_plan(plan)
        return 0, {"mode": "direct", "plan": plan}
    res = {"mode": "job", "dir": tilde(d), "large": True, "submitted": False,
           "size_note": "目录超过 %.0f GB：收编预演要逐个文件算 sha256，不在登录节点上做" % (ADOPT_DIRECT_BYTES / 1e9),
           "job": {"cpus": 1, "mem": "2G", "nice": LOW_PRIORITY_NICE, "time": ADOPT_JOB_TIME, "partition": partition},
           "sbatch_script": adopt_job_script(d, name, version, partition)}
    if not plan_job:
        res["next"] = "交一个低优先级作业做收编预演（1 核 2G，排在所有人的普通作业之后）：用户同意后加 --plan-job"
        return 0, res
    _adopt_dir()
    job = (submit_fn or _sbatch)(res["sbatch_script"])
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        rc, got = _adopt_result(job, name, version, d, state_fn)
        if got.get("finished") or rc or time.monotonic() >= deadline:
            got.update(submitted=True, job_id=job, large=True)
            return rc, got
        sleep(min(5.0, max(0.1, deadline - time.monotonic())))


def adopt_submit(path, name, version, expected):
    """用保存的预演原文提交收编（db-install adopt-submit --expected-plan <sha>，预演从 stdin 进）。"""
    if not valid_component(name) or not valid_component(version):
        return 2, {"error": "库名 / 版本不合法"}
    if not re.fullmatch(r"[0-9a-f]{64}", expected or ""):
        return 2, {"error": "--expected-plan 要写预演给出的 plan_sha256"}
    d = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(d):
        return 2, {"error": "目录不存在：" + path}
    try:
        with open(os.path.join(os.path.expanduser(ADOPT_DIR), "plan-%s.json" % expected), encoding="utf-8") as f:
            text = f.read()
        plan = json.loads(text)
    except (OSError, ValueError):
        return 2, {"error": "找不到 plan_sha256 为 %s… 的收编预演：重新 database adopt 预演" % expected[:12]}
    if plan.get("plan_sha256") != expected or not _plan_matches(plan, name, version, d):
        return 2, {"error": "保存的预演和这次的库名、版本或目录对不上：重新 database adopt 预演"}
    return _db_install_run(["adopt-submit", "--expected-plan", expected], stdin=text, timeout=600)


# ----------------------------- 服务器端命令行 -----------------------------

def emit(obj, rc=0):
    print(json.dumps(obj, ensure_ascii=False, indent=1))
    sys.exit(rc)


def main(argv=None):
    rc, obj = cli(argv)
    emit(obj, rc)


def cli(argv=None):
    ap = argparse.ArgumentParser(prog="db_project")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("facts")
    f.add_argument("--pair", action="append", default=[])
    f.add_argument("--fingerprints", action="store_true")
    for name in ("scan", "check", "lock", "pin"):
        p = sub.add_parser(name)
        p.add_argument("path")
        if name in ("lock", "pin"):
            p.add_argument("--db", action="append", default=[])
            p.add_argument("--yes", action="store_true")
            p.add_argument("--plan-id")
    c = sub.add_parser("compare")
    c.add_argument("dir")
    c.add_argument("name")
    c.add_argument("version", nargs="?")
    c.add_argument("--yes", action="store_true")
    c.add_argument("--result")
    c.add_argument("--partition", default="main")
    ap_ = sub.add_parser("adopt-plan")
    ap_.add_argument("name")
    ap_.add_argument("version")
    ap_.add_argument("--path", required=True)
    ap_.add_argument("--plan-job", action="store_true")
    ap_.add_argument("--result")
    ap_.add_argument("--wait", type=float, default=0)
    ap_.add_argument("--partition", default="main")
    asub = sub.add_parser("adopt-submit")
    asub.add_argument("name")
    asub.add_argument("version")
    asub.add_argument("--path", required=True)
    asub.add_argument("--expected-plan", required=True)
    a = ap.parse_args(argv)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return 2, {"error": "不要用 root 跑"}
    if a.cmd == "facts":
        pairs = [tuple(x.split("/", 1)) for x in a.pair if "/" in x]
        return 0, gather_facts(pairs, a.fingerprints)
    try:
        if a.cmd == "compare":
            return compare(a.dir, a.name, a.version, a.yes, a.result, a.partition)
        if a.cmd == "adopt-plan":
            return adopt_plan(a.path, a.name, a.version, a.plan_job, a.result, a.wait, a.partition)
        if a.cmd == "adopt-submit":
            return adopt_submit(a.path, a.name, a.version, a.expected_plan)
    except (OSError, ValueError) as e:
        return 1, {"error": str(e)[:400]}
    return project_op(a.cmd, a.path, lambda pairs, fp: gather_facts(pairs, fp), getattr(a, "db", []),
                      getattr(a, "yes", False), getattr(a, "plan_id", None), jobs_fn=project_jobs)


if __name__ == "__main__":
    main()
