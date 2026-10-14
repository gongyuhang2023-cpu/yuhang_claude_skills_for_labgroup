#!/usr/bin/env python3
"""服务器上的项目：代码推了没、推到 Gitea。

advise.py project … 经 ssh 用 `python3 -I - <参数>` 把本文件送到服务器上执行（纯标准库）。
只以本人身份动本人的仓库；绝不 --force；令牌不经过这里（缺凭据时 git 直接失败，不卡住等输入）。
写操作（ignore / push / init）不带 --yes 只预演；push 预演给出 plan_id，带 --yes 时核对 plan_id 没变才动手
（预演到确认之间，作业可能又往仓库里写了东西）。
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

BIG = 50 << 20                     # 单个文件超过它就拦：数据放 NAS，不进 git
WARN_TOTAL = 90 << 20              # 一次推送接近 100 MB 时提醒（站点前面的代理可能限制单次上传）
# 必须写进 .gitignore 的目录 -> 拦下时的理由。results/ 不在里面：小的结果表可以进 git，大文件由 BIG 拦
DATA_DIRS = {"data": "数据放 NAS，目录写进 .gitignore", "_data": "数据放 NAS，目录写进 .gitignore",
             "logs": "作业日志不进 git，目录写进 .gitignore（日志照样留在服务器上）"}
SECRET_RE = re.compile(r"(^|/)(\.env(\.[^/]*)?|id_(rsa|ed25519|ecdsa|dsa)|[^/]*\.pem|[^/]*\.key|\.netrc|"
                       r"\.git-credentials|credentials(\.json)?|[^/]*token[^/]*\.txt)$", re.I)
INVENTORY = "/opt/bio/var/inventory.json"
DB_ROOT = "/opt/bio/db"
DB_RE = re.compile(re.escape(DB_ROOT) + r"/([A-Za-z0-9_.+-]+)/([A-Za-z0-9_.+-]+)")
SCRIPT_EXT = {".sh", ".bash", ".py", ".r", ".rmd", ".qmd", ".smk", ".nf", ".sbatch", ".slurm", ".yaml",
              ".yml", ".toml", ".cfg", ".config", ".ipynb", ".pl", ".mk"}
SCRIPT_NAMES = {"Snakefile", "Makefile", "nextflow.config"}
PRUNE = {".git", ".snakemake", "work", "node_modules", "__pycache__", ".pixi", "envs", "_data", "data",
         "results", "logs", ".ipynb_checkpoints"}
ENV_SPECS = ("environment.yml", "environment.yaml", "pixi.toml", "pixi.lock", "requirements.txt",
             "renv.lock", "conda-lock.yml", "pyproject.toml", "DESCRIPTION")


def out(obj, rc=0):
    print(json.dumps(obj, ensure_ascii=False, indent=1))
    sys.exit(rc)


def git(repo, *a, timeout=60):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C", GIT_OPTIONAL_LOCKS="0")   # 只看不写：status 不回写 .git/index
    try:
        p = subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, "", "git %s 超过 %d 秒" % (a[0], timeout)
    return p.returncode, p.stdout, p.stderr


def redact(url):
    return re.sub(r"://[^/@\s]+@", "://<凭据已隐去>@", url)


def project_dir(p):
    p = os.path.realpath(os.path.expanduser(p))
    if not os.path.isdir(p):
        out({"error": "目录不存在：" + p}, 2)
    return p


def tilde(p):
    h = os.path.expanduser("~")
    return "~" + p[len(h):] if p == h or p.startswith(h + "/") else p


def size_of(top, rel):
    try:
        return os.lstat(os.path.join(top, rel)).st_size
    except OSError:
        return 0


def status_files(top):
    """-> [(状态, 相对路径)]：git add -A 会带上的全部（含已暂存、改了没暂存、没被忽略的新文件）。"""
    rc, so, se = git(top, "status", "--porcelain=v1", "-uall", "-z")
    if rc != 0:
        return None
    items, parts, i = [], so.split("\0"), 0
    while i < len(parts):
        e = parts[i]
        i += 1
        if len(e) < 4:
            continue
        st, path = e[:2], e[3:]
        if st[0] in "RC":           # 改名：下一段是旧名
            i += 1
        items.append((st, path))
    return items


def remote_state(top, branch):
    """远端是否有本地没有的提交：用 ls-remote（只读，不改本地引用）。"""
    rc, so, se = git(top, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    up = so.strip() if rc == 0 else None
    res = {"upstream": up}
    if not up or "/" not in up:
        return res
    remote, rbranch = up.split("/", 1)
    rc, so, se = git(top, "ls-remote", remote, "refs/heads/" + rbranch, timeout=25)
    if rc != 0:
        res["remote_error"] = "查不到远端（凭据或网络）：" + redact(se.strip())[-200:]
        return res
    sha = so.split()[0] if so.strip() else None
    res["remote_head"] = sha
    if sha:
        known = git(top, "cat-file", "-e", sha + "^{commit}")[0] == 0
        res["remote_ahead"] = not known or git(top, "merge-base", "--is-ancestor", sha, "HEAD")[0] != 0
    return res


def other_branches(top, current):
    """当前分支以外、有没推的提交或从没推过的本地分支（比的是本地记着的远端分支，不 fetch）。"""
    rc, so, se = git(top, "for-each-ref", "--format=%(refname:short)\t%(upstream:short)\t%(upstream:track)",
                     "refs/heads")
    res = []
    for ln in so.splitlines():
        name, up, track = (ln.split("\t") + ["", ""])[:3]
        if name == current:
            continue
        if not up:
            res.append({"branch": name, "upstream": None, "note": "从没推过"})
        elif "ahead" in track:
            res.append({"branch": name, "upstream": up, "note": track.strip("[]")})
    return res


def unpushed_blobs(top, upstream):
    """没推的提交里，大文件和总量（按对象大小算）。"""
    rng = [upstream + "..HEAD"] if upstream else ["HEAD"]
    rc, so, se = git(top, "rev-list", "--objects", *rng, timeout=120)
    if rc != 0:
        return [], 0, []
    lines = [x for x in so.splitlines() if x.strip()]
    commits_rc, commits_out, _ = git(top, "log", "--format=%h %s", *rng, "-n", "50")
    p = subprocess.run(["git", "-C", top, "cat-file", "--batch-check=%(objecttype) %(objectsize) %(rest)"],
                       input="\n".join(x.split(" ", 1)[0] + (" " + x.split(" ", 1)[1] if " " in x else "")
                                       for x in lines) + "\n",
                       capture_output=True, text=True, timeout=120)
    big, total = [], 0
    for ln in p.stdout.splitlines():
        f = ln.split(" ", 2)
        if len(f) >= 2 and f[0] == "blob":
            n = int(f[1])
            total += n
            if n > BIG:
                big.append({"file": f[2] if len(f) > 2 else "?", "bytes": n})
    return big, total, commits_out.splitlines() if commits_rc == 0 else []


def scan_db_refs(top):
    """脚本里用到的公共数据库：直接写的 /opt/bio/db/<库>/<版本|current>，和登录环境里指向它的变量。"""
    env_vars = {}
    try:
        p = subprocess.run(["bash", "-lc", "env"], capture_output=True, text=True, timeout=20)
        for ln in p.stdout.splitlines():
            k, _, v = ln.partition("=")
            if v.startswith(DB_ROOT + "/") and re.fullmatch(r"[A-Z_][A-Z0-9_]*", k):
                env_vars[k] = v
    except Exception:
        pass
    var_re = re.compile(r"\$\{?(%s)\b" % "|".join(map(re.escape, env_vars))) if env_vars else None
    hits = {}
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = [d for d in dirnames if d not in PRUNE and not os.path.islink(os.path.join(dirpath, d))]
        for fn in filenames:
            if fn not in SCRIPT_NAMES and os.path.splitext(fn)[1].lower() not in SCRIPT_EXT:
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) > 1 << 20:
                    continue
                with open(full, encoding="utf-8", errors="replace") as f:
                    for no, line in enumerate(f, 1):
                        refs = [m.group(0) for m in DB_RE.finditer(line)]
                        if var_re:
                            refs += ["$" + m.group(1) for m in var_re.finditer(line)]
                        for r in refs:
                            h = hits.setdefault(r, [])
                            if len(h) < 5:
                                h.append("%s:%d" % (os.path.relpath(full, top), no))
            except OSError:
                continue
    try:
        with open(INVENTORY, encoding="utf-8") as f:
            dbs = json.load(f).get("dbs", [])
    except Exception:
        dbs = []
    res = []
    for ref, where in sorted(hits.items()):
        path = env_vars.get(ref[1:], "") if ref.startswith("$") else ref
        real = os.path.realpath(path) if path else ""
        rec = next((d for d in dbs if real == d.get("path") or real.startswith(d.get("path", "\0") + "/")), None)
        res.append({"ref": ref, "points_to": path or None, "resolved": real or None,
                    "db": rec.get("name") if rec else None, "version": rec.get("version") if rec else None,
                    "is_current_link": "/current" in path, "where": where})
    note = ("current 和变量解析出来的是今天的版本，不一定是当初出结果时用的版本；"
            "写论文时以出结果那天的作业日志为准") if any(r["is_current_link"] or r["ref"].startswith("$") for r in res) else None
    return res, note


def find_nested_and_root(top, limit=40000):
    nested, root_owned, seen = [], [], 0
    for dirpath, dirnames, filenames in os.walk(top):
        if dirpath != top and ".git" in dirnames + filenames:
            nested.append(os.path.relpath(dirpath, top))
        dirnames[:] = [d for d in dirnames if d != ".git" and not os.path.islink(os.path.join(dirpath, d))]
        for n in filenames + dirnames:
            seen += 1
            try:
                if os.lstat(os.path.join(dirpath, n)).st_uid == 0 and len(root_owned) < 5:
                    root_owned.append(os.path.relpath(os.path.join(dirpath, n), top))
            except OSError:
                pass
        if seen > limit:
            break
    return nested[:10], root_owned


def identity(top):
    name = git(top, "config", "user.name")[1].strip()
    email = git(top, "config", "user.email")[1].strip()
    return {"name": name or None, "email": email or None, "ok": bool(name and email)}


def problems_for(top, files):
    """要提交的文件里不该进 git 的：大文件、数据目录、像密钥的。"""
    bad = []
    for st, rel in files:
        if SECRET_RE.search(rel):
            bad.append({"file": rel, "why": "像密钥 / 凭据文件"})
        elif rel.split("/", 1)[0] in DATA_DIRS:
            d = rel.split("/", 1)[0]
            bad.append({"file": rel, "why": "在 %s/ 里：%s" % (d, DATA_DIRS[d])})
        elif size_of(top, rel) > BIG:
            bad.append({"file": rel, "why": "单个文件 %.0f MB，超过 50 MB" % (size_of(top, rel) / 2**20)})
    return bad


def cmd_check(p):
    top_in = project_dir(p)
    rc, so, se = git(top_in, "rev-parse", "--show-toplevel")
    res = {"path": tilde(top_in), "is_repo": rc == 0}
    if rc != 0:
        if "dubious ownership" in se:
            res["error"] = "git 拒绝了这个目录（属主不是你）：" + se.strip()[-200:]
        res["env_specs"] = [s for s in ENV_SPECS if os.path.exists(os.path.join(top_in, s))]
        res["data_dirs"] = [d for d in DATA_DIRS if os.path.isdir(os.path.join(top_in, d))]
        res["db_refs"], res["db_note"] = scan_db_refs(top_in)
        res["next"] = "还不是 git 仓库：先在 Gitea 网页上建一个私有库，再 project init <路径> --remote <网址>"
        out(res)
    top = so.strip()
    branch = git(top, "symbolic-ref", "--short", "-q", "HEAD")[1].strip() or None
    remotes = [{"name": ln.split()[0], "url": redact(ln.split()[1])}
               for ln in git(top, "remote", "-v")[1].splitlines() if ln.strip().endswith("(push)")]
    rs = remote_state(top, branch)
    ahead_behind = None
    if rs.get("upstream"):
        r = git(top, "rev-list", "--left-right", "--count", "@{u}...HEAD")
        if r[0] == 0 and r[1].split():
            behind, ahead = map(int, r[1].split())
            ahead_behind = {"local_ahead": ahead, "local_behind_known": behind}
    files = status_files(top) or []
    by_dir = {}                     # 按顶层目录计数：样本只有前 15 个，logs/ 一多就把脚本挤出去了
    for _, f in files:
        k = f.split("/", 1)[0] + ("/" if "/" in f else "")
        by_dir[k] = by_dir.get(k, 0) + 1
    others = other_branches(top, branch)
    stash = len(git(top, "stash", "list")[1].splitlines())
    big_blobs, total, commits = unpushed_blobs(top, rs.get("upstream"))
    pending = sum(size_of(top, f) for _, f in files)
    data_dirs = []
    for d in DATA_DIRS:
        if os.path.isdir(os.path.join(top, d)):
            ignored = git(top, "check-ignore", "-q", d + "/")[0] == 0
            data_dirs.append({"dir": d + "/", "ignored": ignored})
    nested, root_owned = find_nested_and_root(top)
    db_refs, db_note = scan_db_refs(top)
    res.update({
        "path": tilde(top), "branch": branch, "detached": branch is None, "remotes": remotes, **rs,
        "upstream_remote": rs["upstream"].split("/", 1)[0] if rs.get("upstream") else None,
        "ahead_behind": ahead_behind, "uncommitted": len(files), "untracked": sum(st == "??" for st, _ in files),
        "uncommitted_sample": [f for _, f in files[:15]],
        "uncommitted_by_dir": dict(sorted(by_dir.items(), key=lambda kv: -kv[1])),
        "unpushed_commits": commits[:15], "unpushed_count": len(commits),
        "other_branches": others, "stash": stash,
        "freshness": "unpushed_* 和 ahead_behind 比的是本地记着的远端分支（没有 fetch）；remote_ahead 是刚实时查的",
        "blocked_files": problems_for(top, files), "big_in_unpushed_commits": big_blobs,
        "push_size_estimate_mb": round((pending + total) / 2**20, 1),
        "data_dirs": data_dirs, "nested_repos": nested, "root_owned": root_owned,
        "identity": identity(top), "env_specs": [s for s in ENV_SPECS if os.path.exists(os.path.join(top, s))],
        "db_refs": db_refs, "db_note": db_note,
    })
    todo = []
    if not remotes:
        todo.append("没有远端：先在 Gitea 网页上建私有库，再 git remote add（project init 不适用于已有仓库）")
    if res["detached"]:
        todo.append("HEAD 不在任何分支上：先切回分支")
    if rs.get("remote_ahead"):
        todo.append("远端有你本地没有的提交：先 git pull")
    if files:
        n_new = sum(st == "??" for st, _ in files)
        todo.append("%d 处改动没提交" % len(files) + ("（其中 %d 个是没跟踪的新文件）" % n_new if n_new else ""))
    if commits:
        todo.append("%d 个提交没推" % len(commits))
    if others:
        todo.append("别的分支也有没推的：" + "、".join("%s（%s）" % (b["branch"], b["note"]) for b in others[:5]))
    if stash:
        todo.append("有 %d 个 stash，只存在服务器上" % stash)
    if res["blocked_files"] or big_blobs:
        todo.append("有不该进 git 的文件（大文件 / 数据 / 日志 / 密钥），推之前先处理")
    if any(not d["ignored"] for d in data_dirs):
        todo.append("数据 / 日志目录没写进 .gitignore：" + "、".join(d["dir"] for d in data_dirs if not d["ignored"]))
    if not res["identity"]["ok"]:
        todo.append("git 身份没配（user.name / user.email），提交会失败")
    if not res["env_specs"]:
        todo.append("没有环境说明文件（environment.yml / pixi.lock / requirements.txt）")
    if res["push_size_estimate_mb"] * 2**20 > WARN_TOTAL:
        todo.append("这次要推约 %.0f MB，接近单次上传上限，考虑分批" % res["push_size_estimate_mb"])
    res["todo"] = todo
    res["clean"] = not todo
    # 项目能不能离开服务器只看代码是否都在远端；clean 还要求 .gitignore、环境说明等配好，那是为以后的推送
    res["code_safe"] = bool(remotes) and not res["detached"] and not files and not commits and not others \
        and not stash and not rs.get("remote_error")
    out(res)


def cmd_ignore(p, patterns, yes):
    top = project_dir(p)
    gi = os.path.join(top, ".gitignore")
    have = []
    if os.path.isfile(gi):
        with open(gi, encoding="utf-8", errors="replace") as f:
            have = [x.strip() for x in f.read().splitlines()]
    add = [x for x in patterns if x.strip() and x.strip() not in have]
    res = {"path": tilde(gi), "add": add, "already": [x for x in patterns if x.strip() in have], "written": False}
    if not add or not yes:
        out(res)
    with open(gi, "a", encoding="utf-8", newline="\n") as f:
        if have and have[-1] != "":
            f.write("\n")
        f.write("\n".join(add) + "\n")
    res["written"] = True
    out(res)


def push_plan(top, set_upstream, message=""):
    branch = git(top, "symbolic-ref", "--short", "-q", "HEAD")[1].strip() or None
    rs = remote_state(top, branch)
    files = status_files(top)
    if files is None:
        out({"error": "git status 失败（目录属主不是你？）"}, 1)
    big_blobs, total, commits = unpushed_blobs(top, rs.get("upstream"))
    refuse = []
    if branch is None:
        refuse.append("HEAD 不在任何分支上")
    if not rs.get("upstream") and not set_upstream:
        refuse.append("当前分支没有上游：第一次推送加 --set-upstream（推到 origin）")
    if not rs.get("upstream") and set_upstream and git(top, "remote", "get-url", "origin")[0] != 0:
        refuse.append("没有名为 origin 的远端")
    if rs.get("remote_ahead"):
        refuse.append("远端有你本地没有的提交：先 git pull，再推")
    if rs.get("remote_error"):
        refuse.append(rs["remote_error"])
    blocked = problems_for(top, files)
    if blocked:
        refuse.append("有不该进 git 的文件（共 %d 个，下面是前 5 个）：" % len(blocked) +
                      "；".join("%s（%s）" % (b["file"], b["why"]) for b in blocked[:5]))
    if big_blobs:
        refuse.append("没推的提交里有大文件：" + "、".join(b["file"] for b in big_blobs[:5]))
    nested, _ = find_nested_and_root(top, limit=5000)
    if nested:
        refuse.append("里面嵌着别的 git 仓库（%s），会被当成子模块链接提交" % "、".join(nested[:3]))
    who = identity(top)
    if files and not who["ok"]:
        refuse.append("git 身份没配（user.name / user.email）：请你自己设好再推")
    sized = [{"file": f, "status": st.strip() or "?", "bytes": size_of(top, f)} for st, f in files]
    head = git(top, "rev-parse", "-q", "--verify", "HEAD")[1].strip() or "无提交"
    basis = json.dumps({"head": head, "branch": branch, "files": [(x["file"], x["bytes"]) for x in sized],
                        "commits": commits, "message": message}, ensure_ascii=False)   # 说明改了也要重新确认
    total_mb = round((sum(x["bytes"] for x in sized) + total) / 2**20, 1)
    return {"path": tilde(top), "branch": branch, "upstream": rs.get("upstream"), "files": sized[:200],
            "file_count": len(sized), "unpushed_commits": commits, "push_size_estimate_mb": total_mb,
            "warning": ("这次要推约 %.0f MB，接近单次上传上限" % total_mb) if total_mb * 2**20 > WARN_TOTAL else None,
            "refuse": refuse,       # 被拒的计划不给 plan_id：拿它确认不了，别让人误以为能
            "plan_id": None if refuse else hashlib.sha256(basis.encode()).hexdigest()[:12]}


def cmd_push(p, message, set_upstream, yes, plan_id):
    top0 = project_dir(p)
    rc, so, se = git(top0, "rev-parse", "--show-toplevel")
    if rc != 0:
        out({"error": "不是 git 仓库" + ("（属主不是你）" if "dubious ownership" in se else "") +
                      "：先 project init", "path": tilde(top0)}, 1)
    top = so.strip()
    plan = push_plan(top, set_upstream, message)
    if plan["refuse"] or not yes:
        plan["executed"] = False
        if not plan["refuse"]:
            plan["next"] = "确认后带 --yes --plan-id %s 再跑一次" % plan["plan_id"]
        out(plan, 1 if plan["refuse"] else 0)
    if plan_id != plan["plan_id"]:
        out({"error": "仓库在预演之后变了（plan_id 对不上），重新预演给用户看", "plan_id": plan["plan_id"]}, 1)
    done = []
    if plan["file_count"]:
        for step in (["add", "-A"], ["commit", "-q", "-m", message]):
            r = git(top, *step, timeout=300)
            done.append({"step": "git " + " ".join(step[:2]), "ok": r[0] == 0, "output": (r[1] + r[2]).strip()[-300:]})
            if r[0] != 0:
                out({"error": "git %s 失败，已停下" % step[0], "done": done}, 1)
    push = ["push", "--porcelain"] + (["-u", "origin", plan["branch"]] if not plan["upstream"] else [])
    r = git(top, *push, timeout=600)
    done.append({"step": "git push", "ok": r[0] == 0, "output": redact((r[1] + r[2]).strip())[-400:]})
    head = git(top, "rev-parse", "--short", "HEAD")[1].strip()
    out({"executed": True, "error": None if r[0] == 0 else "推送失败（凭据？网络？远端拒绝？）", "head": head,
         "done": done}, 0 if r[0] == 0 else 1)


def cmd_init(p, remote, yes):
    top = project_dir(p)
    if git(top, "rev-parse", "--show-toplevel")[0] == 0:
        out({"error": "已经是 git 仓库了：用 project push", "path": tilde(top)}, 1)
    if not re.match(r"^(https://[^\s]+|ssh://[^\s]+|[\w.-]+@[\w.-]+:[^\s]+)$", remote):
        out({"error": "远端网址看着不对（要 https://… 或 git@…:…）"}, 2)
    data = [d + "/" for d in DATA_DIRS if os.path.isdir(os.path.join(top, d))]
    res = {"path": tilde(top), "remote": redact(remote), "steps": ["git init -b main", "git remote add origin " + redact(remote)],
           "suggest_gitignore": data, "executed": False,
           "next": "先用 project ignore 把数据目录写进 .gitignore，再 project push --set-upstream 预演第一次推送"}
    if not yes:
        out(res)
    for step in (["init", "-q", "-b", "main"], ["remote", "add", "origin", remote]):
        r = git(top, *step)
        if r[0] != 0:
            out({"error": "git %s 失败：%s" % (step[0], redact(r[2].strip())[-200:])}, 1)
    res["executed"] = True
    out(res)


def main():
    ap = argparse.ArgumentParser(prog="remote_project")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("path")
    ig = sub.add_parser("ignore")
    ig.add_argument("path")
    ig.add_argument("--add", nargs="+", required=True)
    ig.add_argument("--yes", action="store_true")
    pu = sub.add_parser("push")
    pu.add_argument("path")
    pu.add_argument("-m", "--message", required=True)
    pu.add_argument("--set-upstream", action="store_true")
    pu.add_argument("--yes", action="store_true")
    pu.add_argument("--plan-id")
    it = sub.add_parser("init")
    it.add_argument("path")
    it.add_argument("--remote", required=True)
    it.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    if os.geteuid() == 0:
        out({"error": "不要用 root 跑"}, 2)
    if a.cmd == "check":
        cmd_check(a.path)
    elif a.cmd == "ignore":
        cmd_ignore(a.path, a.add, a.yes)
    elif a.cmd == "push":
        cmd_push(a.path, a.message, a.set_upstream, a.yes, a.plan_id)
    else:
        cmd_init(a.path, a.remote, a.yes)


if __name__ == "__main__":
    main()
