#!/usr/bin/env python3
"""
spark-advisor engine -- recommend Slurm job parameters for a shared GPU server.

Pure standard library. All the fragile bits (SSH, scontrol/squeue/sacct parsing)
live here and return CLEAN JSON, so the calling AI never freehands SSH and never
gets a wall of raw terminal output dumped into its context.

Subcommands:
  status            current server state (GPU free?, schedulable mem, queue)
  history           this user's past jobs from sacct (host-mem peak, elapsed)
  recommend         baseline --gres/--mem/--time + confidence, combining the above
  gen-sbatch        emit an sbatch script from chosen params (does NOT submit)
  submit            submit a local sbatch script via the server (gated by SKILL.md)

Facts vs judgment: this script produces FACTS + a rule-based baseline. The final
recommendation phrasing / confidence framing is the AI's job -- see
references/recommend-rules.md.

Config: reads config.json (next to the skill root). Copy config.example.json first.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_CANDIDATES = [
    os.path.join(HERE, "..", "config.json"),
    os.path.join(HERE, "config.json"),
]


# ----------------------------- infra -----------------------------

def die(msg):
    print(json.dumps({"error": msg}, ensure_ascii=False, indent=2))
    sys.exit(1)


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def load_config():
    for p in CONFIG_CANDIDATES:
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception as e:
                die(f"config.json is not valid JSON: {e}")
            for key in ("host", "user", "node"):
                if not cfg.get(key):
                    die(f"config.json is missing '{key}'. See config.example.json.")
            return cfg
    die("config.json not found. Copy config.example.json -> config.json and fill it in.")


def ssh_run(cfg, remote_cmd, timeout=30, stdin_data=None):
    """Run a command on the server over SSH. Returns stdout (str)."""
    target = f'{cfg["user"]}@{cfg["host"]}'
    cmd = ["ssh"] + list(cfg.get("ssh_opts", ["-o", "ConnectTimeout=12", "-o", "BatchMode=yes"]))
    if cfg.get("ssh_key"):
        cmd += ["-i", cfg["ssh_key"]]
    cmd += [target, remote_cmd]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, input=stdin_data)
    except FileNotFoundError:
        die("`ssh` not found on PATH. Install an OpenSSH client.")
    except subprocess.TimeoutExpired:
        die(f"SSH timed out talking to {target}.")
    if r.returncode != 0 and not r.stdout.strip():
        die(f"SSH command failed ({r.returncode}) on {target}: {r.stderr.strip()[:300]}")
    return r.stdout


# ----------------------------- parse helpers -----------------------------

def parse_kv_line(line):
    """scontrol -o output: space-separated key=value tokens."""
    d = {}
    for tok in line.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            d[k] = v
    return d


def int0(x):
    try:
        return int(str(x).strip().rstrip("M"))
    except Exception:
        return 0


def mb_to_gb(mb):
    return round(int0(mb) / 1024, 1)


def gres_count(s):
    """'gpu:1' -> 1 ; 'gpu:1(IDX:0)' -> 1 ; '(null)'/'' -> 0."""
    if not s or s in ("(null)", "N/A"):
        return 0
    total = 0
    for part in s.split(","):
        bits = part.split(":")
        if len(bits) >= 2 and bits[0].strip().endswith("gpu"):
            num = ""
            for ch in bits[1]:
                if ch.isdigit():
                    num += ch
                else:
                    break
            total += int(num) if num else 0
    return total


def rss_to_gb(s):
    """sacct MaxRSS like '508K', '62000000K', '2000M', '1.5G' -> GB float."""
    if not s:
        return None
    s = s.strip()
    if not s or s in ("0", "(null)"):
        return None
    unit = s[-1].upper()
    try:
        val = float(s[:-1]) if unit in "KMGT" else float(s)
    except ValueError:
        return None
    factor = {"K": 1 / 1024 / 1024, "M": 1 / 1024, "G": 1.0, "T": 1024.0}.get(unit, 1 / 1024 / 1024)
    return round(val * factor, 2)


# ----------------------------- subcommands -----------------------------

def cmd_status(cfg):
    node = cfg["node"]
    d = parse_kv_line(ssh_run(cfg, f"scontrol show node {node} -o").strip())
    real = int0(d.get("RealMemory"))
    alloc = int0(d.get("AllocMem"))
    gpu_total = gres_count(d.get("Gres", ""))
    gpu_used = gres_count(d.get("GresUsed", ""))

    fmt = "%i|%u|%T|%j|%m|%b|%M|%L|%R"
    running, pending = [], []
    for ln in ssh_run(cfg, f"squeue -h -o '{fmt}'").splitlines():
        p = ln.split("|")
        if len(p) < 9:
            continue
        job = {
            "job": p[0], "user": p[1], "name": p[3],
            "req_mem": p[4], "gpu": "gpu" in p[5].lower(),
            "elapsed": p[6], "time_left": p[7], "reason": "|".join(p[8:]),
        }
        if p[2] == "RUNNING":
            running.append(job)
        elif p[2] == "PENDING":
            pending.append(job)

    return {
        "node": node,
        "node_state": d.get("State", ""),
        "gpu_total": gpu_total,
        "gpu_used": gpu_used,
        "gpu_free": gpu_used < gpu_total,
        "gpu_job_running": any(j["gpu"] for j in running),
        "mem_total_gb": mb_to_gb(real),
        "mem_reserved_gb": mb_to_gb(alloc),
        "mem_schedulable_gb": round((real - alloc) / 1024, 1),
        "running_jobs": running,
        "pending_jobs": pending,
        "pending_count": len(pending),
    }


def cmd_history(cfg, user, tool, days):
    user = user or cfg["user"]
    fmt = "JobID,JobName,State,Elapsed,ReqMem,MaxRSS,AllocTRES"
    raw = ssh_run(cfg, f"sacct -u {user} --starttime now-{int(days)}days "
                       f"--parsable2 --noheader --format={fmt}")
    jobs = {}
    for ln in raw.splitlines():
        f = ln.split("|")
        if len(f) < 7:
            continue
        jid, name, state, elapsed, reqmem, maxrss, tres = f[:7]
        base = jid.split(".")[0]
        rec = jobs.setdefault(base, {"job": base})
        if "." not in jid:  # main job line
            rec.update({"name": name, "state": state, "elapsed": elapsed,
                        "req_mem": reqmem, "alloc_tres": tres})
        else:               # step line carries MaxRSS
            g = rss_to_gb(maxrss)
            if g is not None:
                rec["host_peak_gb"] = max(g, rec.get("host_peak_gb", 0))
    rows = [r for r in jobs.values() if r.get("name")]
    if tool:
        rows = [r for r in rows if tool.lower() in r.get("name", "").lower()]
    rows.sort(key=lambda r: r["job"], reverse=True)
    peaks = [r["host_peak_gb"] for r in rows if r.get("host_peak_gb")]
    return {
        "user": user, "tool_filter": tool, "days": days,
        "count": len(rows),
        "host_mem_peaks_gb": peaks,
        "note": "MaxRSS = HOST (CPU-side) memory only. GPU/VRAM peak is NOT in sacct "
                "and is not tracked yet (planned: profiled runs -> shared log).",
        "jobs": rows[:40],
    }


def cmd_recommend(cfg, tool, needs_gpu, mem_guess, cpus, time_guess):
    st = cmd_status(cfg)
    hist = cmd_history(cfg, cfg["user"], tool, 90)
    pool = cfg.get("gpu_pool_gb", 120)
    floor = cfg.get("gpu_floor_gb", 96)
    rec = {"gres": None, "cpus": cpus, "mem_gb": None, "time": None,
           "confidence": "low", "basis": [], "warnings": [], "run_now": None}

    if needs_gpu:
        rec["gres"] = "gpu:1"
        # MVP: no GPU-VRAM history yet -> lean generous (serialized => cheap to over-reserve)
        rec["mem_gb"] = min(int(round(floor * 1.15)), pool - 10)
        rec["confidence"] = "low"
        rec["basis"].append(
            f"GPU job, no VRAM history yet -> generous default ~{rec['mem_gb']}G "
            f"(lua auto-floors to {floor}G regardless). Over-reserving a GPU job is cheap "
            f"because GPU jobs are serialized.")
        rec["warnings"].append("Recommend a calibration/profiled run to pin real VRAM peak, "
                               "then this becomes exact.")
    else:
        peaks = hist["host_mem_peaks_gb"]
        if peaks:
            peak = max(peaks)
            rec["mem_gb"] = max(2, int(round(peak * 1.3)))
            rec["confidence"] = "high"
            rec["basis"].append(f"CPU job: past similar jobs peaked at {peak}G host memory "
                                f"(sacct MaxRSS) -> +30% margin.")
        elif mem_guess:
            rec["mem_gb"] = max(2, int(round(mem_guess * 1.5)))
            rec["confidence"] = "medium"
            rec["basis"].append(f"CPU job: your estimate {mem_guess}G +50% margin (no history).")
        else:
            rec["mem_gb"] = 16
            rec["confidence"] = "low"
            rec["basis"].append("CPU job: no history or estimate -> default 16G. "
                                "cgroup hard-caps this safely -- underestimate only kills your own job.")

    elapsed = [j["elapsed"] for j in hist["jobs"] if j.get("elapsed") and j["elapsed"] != "00:00:00"]
    if elapsed:
        rec["time"] = f"history longest ~{max(elapsed)}; suggest ~1.5x that"
    else:
        rec["time"] = time_guess or "6:00:00 (generous default -- set to your expected max; cap is 14 days)"

    if needs_gpu:
        rec["run_now"] = not st["gpu_job_running"]
        if st["gpu_job_running"]:
            rec["warnings"].append(
                f"GPU is BUSY now ({st['pending_count']} already queued) -> your job will QUEUE.")
    else:
        rec["run_now"] = (rec["mem_gb"] or 0) <= st["mem_schedulable_gb"]
        if not rec["run_now"]:
            rec["warnings"].append(
                f"Needs {rec['mem_gb']}G but only {st['mem_schedulable_gb']}G schedulable now -> will QUEUE.")

    return {"recommendation": rec, "server_now": st,
            "history_summary": {"count": hist["count"], "host_mem_peaks_gb": hist["host_mem_peaks_gb"]}}


def cmd_gen_sbatch(cfg, name, gres, mem_gb, time, cpus, command, out):
    L = ["#!/bin/bash",
         f"#SBATCH --job-name={name}",
         f"#SBATCH --partition={cfg.get('partition', 'main')}"]
    if gres:
        L.append(f"#SBATCH --gres={gres}")
    if cpus:
        L.append(f"#SBATCH --cpus-per-task={cpus}")
    if mem_gb:
        L.append(f"#SBATCH --mem={mem_gb}G")
    if time:
        L.append(f"#SBATCH --time={time}")
    L += ["#SBATCH --output=%x-%j.log", "",
          command or "# TODO: your command here", ""]
    script = "\n".join(L)
    result = {"sbatch_script": script}
    if out:
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)
        result["written_to"] = out
    result["next"] = "Review, get user OK, then: advise.py submit --script <file>"
    return result


def cmd_submit(cfg, script_path):
    if not os.path.exists(script_path):
        die(f"script not found: {script_path}")
    with open(script_path, encoding="utf-8") as f:
        script = f.read()
    out = ssh_run(cfg, "sbatch", stdin_data=script)
    return {"submitted": out.strip(), "as_user": cfg["user"]}


# remote probe script: one SSH pass, section markers, parsed locally.
_PROBE = r'''
echo "===ARCH==="; uname -m
echo "===OS==="; (. /etc/os-release 2>/dev/null; echo "$PRETTY_NAME")
echo "===KERNEL==="; uname -r
echo "===GPU==="; nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | head -1
echo "===CUDA==="; nvidia-smi 2>/dev/null | grep -oE "CUDA Version: [0-9.]+" | head -1
echo "===PYTHON==="; python3 --version 2>&1
echo "===PIP==="; python3 -m pip --version 2>&1 | head -1
echo "===GCC==="; gcc --version 2>/dev/null | head -1
echo "===CONDA==="; { command -v conda mamba micromamba ; } 2>/dev/null || echo none
echo "===CONTAINER==="; { command -v docker podman ; } 2>/dev/null || echo none
echo "===SLURM==="; sinfo --version 2>/dev/null
echo "===VENVS==="; find ~ -maxdepth 3 -name pyvenv.cfg 2>/dev/null | sed "s#/pyvenv.cfg##" | head -20
echo "===CONDAENVS==="; for d in ~/miniforge3 ~/miniconda3 ~/anaconda3 ~/mambaforge; do [ -d "$d/envs" ] && ls "$d/envs" 2>/dev/null | sed "s#^#$d/envs/#" ; done
echo "===PROJECTS==="; find ~ -maxdepth 3 -name .git -type d 2>/dev/null | sed "s#/.git##" | head -20
echo "===SHELL==="; grep -hE "^(export |module load |conda activate |source )" ~/.bashrc ~/.bash_profile ~/.profile 2>/dev/null | grep -vE "^[[:space:]]*#" | head -30
echo "===END==="
'''


def cmd_probe_env(cfg):
    """Probe the server's software env + this user's venvs; cache to a
    gitignored profile so the AI needn't re-investigate the box each time."""
    raw = ssh_run(cfg, _PROBE, timeout=45)
    sec, cur = {}, None
    for ln in raw.splitlines():
        m = ln.strip()
        if m.startswith("===") and m.endswith("===") and len(m) > 6:
            cur = m.strip("=").strip()
            sec[cur] = []
        elif cur is not None and ln.strip():
            sec[cur].append(ln.rstrip())

    def one(k):
        v = sec.get(k, [])
        return v[0] if v else None

    def many(k):
        return [x for x in sec.get(k, []) if x and x != "none"]

    profile = {
        "probed_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "server": f'{cfg["node"]} ({cfg["host"]})',
        "user": cfg["user"],
        "system": {
            "arch": one("ARCH"),
            "os": one("OS"),
            "kernel": one("KERNEL"),
            "gpu": one("GPU"),
            "cuda": (one("CUDA") or "").replace("CUDA Version: ", "") or None,
            "python3": one("PYTHON"),
            "pip": one("PIP"),
            "gcc": one("GCC"),
            "conda": many("CONDA") or "none",
            "container": many("CONTAINER") or "none",
            "slurm": one("SLURM"),
        },
        "user_env": {
            "venvs": many("VENVS"),
            "conda_envs": many("CONDAENVS"),
            "projects": many("PROJECTS"),
            "shell_setup": sec.get("SHELL", []),
        },
        "note": "Auto-probed cache. Refresh: advise.py probe-env. "
                "Re-probe if an env command fails or this looks stale.",
    }
    out = os.path.join(HERE, "..", "env-profile.local.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
    profile["_written_to"] = os.path.abspath(out)
    return profile


def cmd_detect_server(pattern):
    """Scan the LOCAL machine's tailnet for the server by name. Lets first-run
    auto-fill the host IP so the user never hand-types it. No config needed."""
    try:
        r = subprocess.run(["tailscale", "status"], capture_output=True,
                           text=True, timeout=15)
    except FileNotFoundError:
        return {"tailscale_cli": "not found",
                "hint": "Tailscale CLI not on PATH. Get the server IP from your lab "
                        "usage guide (or ask the admin) and pass it to `init --host`."}
    except subprocess.TimeoutExpired:
        return {"error": "`tailscale status` timed out"}
    if r.returncode != 0:
        return {"tailscale_cli": "error", "detail": r.stderr.strip()[:200],
                "hint": "Provide the server IP manually via `init --host`."}
    candidates = []
    for ln in r.stdout.splitlines():
        parts = ln.split()
        if len(parts) >= 2 and parts[0].count(".") == 3 and parts[0][0].isdigit():
            ip, name = parts[0], parts[1]
            if pattern.lower() in name.lower():
                candidates.append({"ip": ip, "name": name})
    return {"pattern": pattern, "candidates": candidates,
            "hint": "Empty? Try a different --pattern, or take the IP from the usage guide."}


def cmd_init(user, host, node, partition, gpu_pool_gb, gpu_floor_gb):
    """Write config.json (from the example's defaults) so first-run is one step.
    host from detect-server / the usage guide / the user; node = server's Slurm
    node name (usually == its tailnet device name)."""
    base = {}
    example = os.path.join(HERE, "..", "config.example.json")
    if os.path.exists(example):
        with open(example, encoding="utf-8") as f:
            base = json.load(f)
    base.pop("_comment", None)
    base["host"], base["user"], base["node"] = host, user, node
    if partition:
        base["partition"] = partition
    if gpu_pool_gb:
        base["gpu_pool_gb"] = gpu_pool_gb
    if gpu_floor_gb:
        base["gpu_floor_gb"] = gpu_floor_gb
    out = os.path.join(HERE, "..", "config.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False, indent=2)
    return {"written": os.path.abspath(out), "config": base,
            "next": "Verify: advise.py status. If it reports node-not-found, run "
                    "`ssh <user>@<host> sinfo -h -o %N` for the real node name and re-init."}


# ----------------------------- CLI -----------------------------

def main():
    ap = argparse.ArgumentParser(description="spark-advisor engine (facts + baseline; AI does final judgment)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")

    h = sub.add_parser("history")
    h.add_argument("--user", default=None)
    h.add_argument("--tool", default=None, help="filter by job-name substring, e.g. boltz")
    h.add_argument("--days", type=int, default=90)

    r = sub.add_parser("recommend")
    r.add_argument("--tool", default=None)
    r.add_argument("--gpu", action="store_true", help="job needs a GPU")
    r.add_argument("--mem-guess", type=float, default=None, help="your rough host-mem guess in GB")
    r.add_argument("--cpus", type=int, default=None)
    r.add_argument("--time", default=None)

    g = sub.add_parser("gen-sbatch")
    g.add_argument("--name", required=True)
    g.add_argument("--gres", default=None)
    g.add_argument("--mem-gb", type=int, default=None)
    g.add_argument("--time", default=None)
    g.add_argument("--cpus", type=int, default=None)
    g.add_argument("--command", default=None)
    g.add_argument("--out", default=None)

    s = sub.add_parser("submit")
    s.add_argument("--script", required=True)

    sub.add_parser("probe-env")

    d = sub.add_parser("detect-server")
    d.add_argument("--pattern", default="spark", help="match tailnet device name")

    i = sub.add_parser("init")
    i.add_argument("--user", required=True)
    i.add_argument("--host", required=True)
    i.add_argument("--node", required=True, help="Slurm node name (usually == tailnet device name)")
    i.add_argument("--partition", default=None)
    i.add_argument("--gpu-pool-gb", type=int, default=None)
    i.add_argument("--gpu-floor-gb", type=int, default=None)

    args = ap.parse_args()

    # these bootstrap first-run config, so they must NOT require an existing config
    if args.cmd == "detect-server":
        emit(cmd_detect_server(args.pattern))
        return
    if args.cmd == "init":
        emit(cmd_init(args.user, args.host, args.node, args.partition,
                      args.gpu_pool_gb, args.gpu_floor_gb))
        return

    cfg = load_config()
    if args.cmd == "status":
        emit(cmd_status(cfg))
    elif args.cmd == "history":
        emit(cmd_history(cfg, args.user, args.tool, args.days))
    elif args.cmd == "recommend":
        emit(cmd_recommend(cfg, args.tool, args.gpu, args.mem_guess, args.cpus, args.time))
    elif args.cmd == "gen-sbatch":
        emit(cmd_gen_sbatch(cfg, args.name, args.gres, args.mem_gb, args.time, args.cpus, args.command, args.out))
    elif args.cmd == "submit":
        emit(cmd_submit(cfg, args.script))
    elif args.cmd == "probe-env":
        emit(cmd_probe_env(cfg))


if __name__ == "__main__":
    main()
