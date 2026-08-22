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
import re
import shlex
import socket
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


_RESOLVED_HOST = None


def resolve_host(cfg):
    """Pick the network path to the server.

    If config has an optional `host_lan` (a direct/LAN address) and its SSH port
    answers, use it -- a wired LAN is typically an order of magnitude faster than
    a VPN/WAN path. Otherwise fall back to `host`, which is the address that works
    from anywhere. Both point at the same machine, so the choice is transparent to
    callers.

    Probed once per process: a single command usually issues several ssh calls and
    there is no reason to re-probe for each.
    """
    global _RESOLVED_HOST
    if _RESOLVED_HOST is not None:
        return _RESOLVED_HOST
    lan = cfg.get("host_lan")
    if lan:
        try:
            with socket.create_connection((lan, int(cfg.get("ssh_port", 22))), timeout=1.0):
                _RESOLVED_HOST = lan
                return _RESOLVED_HOST
        except OSError:
            pass  # LAN not reachable right now (different site, cable unplugged)
    _RESOLVED_HOST = cfg["host"]
    return _RESOLVED_HOST


def ssh_run(cfg, remote_cmd, timeout=30, stdin_data=None):
    """Run a command on the server over SSH. Returns stdout (str)."""
    target = f'{cfg["user"]}@{resolve_host(cfg)}'
    cmd = ["ssh"] + list(cfg.get("ssh_opts", ["-o", "ConnectTimeout=12", "-o", "BatchMode=yes"]))
    if cfg.get("ssh_key"):
        cmd += ["-i", cfg["ssh_key"]]
    cmd += [target, remote_cmd]
    # 走字节 I/O，不用 text=True：Windows 文本模式会把 stdin 里的 \n 翻成 \r\n，
    # sbatch 见到 CRLF 脚本会拒收（"Batch script contains DOS line breaks"）。
    payload = stdin_data.encode("utf-8") if stdin_data is not None else None
    try:
        r = subprocess.run(cmd, capture_output=True,
                           timeout=timeout, input=payload)
    except FileNotFoundError:
        die("`ssh` not found on PATH. Install an OpenSSH client.")
    except subprocess.TimeoutExpired:
        die(f"SSH timed out talking to {target}.")
    stdout = r.stdout.decode("utf-8", "replace")
    stderr = r.stderr.decode("utf-8", "replace")
    if r.returncode != 0 and not stdout.strip():
        die(f"SSH command failed ({r.returncode}) on {target}: {stderr.strip()[:300]}")
    return stdout


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
    # MemSpecLimit 是给系统守护进程预留的，不参与用户分配。
    # 用户能申请的天花板 = RealMemory - MemSpecLimit，不减会多报、按其推荐会被 Slurm 拒。
    spec = int0(d.get("MemSpecLimit"))
    allocatable = max(0, real - spec)
    gpu_total = gres_count(d.get("Gres", ""))
    gpu_used = gres_count(d.get("GresUsed", ""))
    # CPU 核：CPUTot 总数，CPUAlloc 已分配，CPUEfctv = 扣掉 CoreSpec 后的可分配有效核
    # （没设 CoreSpec 时 == CPUTot）。可调度核 = 有效核 − 已分配，与 mem_schedulable 同理。
    cpu_total = int0(d.get("CPUTot"))
    cpu_eff = int0(d.get("CPUEfctv")) or cpu_total
    cpu_alloc = int0(d.get("CPUAlloc"))
    cpu_free = max(0, cpu_eff - cpu_alloc)

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
        "cpu_total": cpu_total,                        # 节点总核
        "cpu_alloc": cpu_alloc,                        # 已被作业预留的核
        "cpu_free": cpu_free,                          # 现在还能调度的核（有效核 − 已分配）
        "mem_total_gb": mb_to_gb(real),
        "mem_spec_reserved_gb": mb_to_gb(spec),        # 系统守护进程占用，用户碰不到
        "mem_allocatable_gb": mb_to_gb(allocatable),   # 用户能申请的天花板
        "mem_reserved_gb": mb_to_gb(alloc),
        "mem_schedulable_gb": round((allocatable - alloc) / 1024, 1),
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
        "note": "MaxRSS = HOST (CPU-side) memory. Reliable for CPU jobs; DISTORTED for GPU "
                "jobs (cudaMalloc bypasses cgroup accounting -> under-reports by ~100x). "
                "Real per-job VRAM peaks ARE sampled server-side since 2026-07-20 "
                "(gpu_proc_samples), but nothing exposes them per job -- only the "
                "fleet-wide aggregate via `spark-usage --all`. This engine does not fetch "
                "VRAM at all: a known boundary, not a missing feature. Do not expect "
                "host_mem_peaks_gb to say anything about GPU memory.",
        "jobs": rows[:40],
    }


def cmd_recommend(cfg, tool, needs_gpu, mem_guess, cpus, time_guess):
    st = cmd_status(cfg)
    hist = cmd_history(cfg, cfg["user"], tool, 90)
    # 池子大小从服务器实时读，不用 config 里的写死值 —— 配置改过好几次，
    # 写死的数迟早过时（gpu_pool_gb 曾停留在 120，而实际早已是 113.9）。
    pool = st.get("mem_allocatable_gb") or cfg.get("gpu_pool_gb", 120)
    floor = cfg.get("gpu_floor_gb", 96)
    rec = {"gres": None, "cpus": cpus, "mem_gb": None, "time": None,
           "confidence": "low", "basis": [], "warnings": [], "run_now": None}

    if needs_gpu:
        rec["gres"] = "gpu:1"
        # ★ 2026-07-20 起规则变了：GPU 作业的 --mem 会被**尊重**，不再一律抬到 96G。
        #   没写才兜底给 96G。所以 --mem 从"写了也白写"变成了一份**申报**——
        #   spark-memguard 在内存吃紧时按"实际占用 vs 申报量"对账，冻结超得最多的那个。
        #   往小了申报不是占便宜，是把自己放到优先被冻结的位置。
        if mem_guess:
            rec["mem_gb"] = max(4, int(round(mem_guess * 1.3)))
            rec["confidence"] = "medium"
            rec["basis"].append(
                f"GPU job: your estimate {mem_guess}G +30% margin -> declare --mem={rec['mem_gb']}G. "
                f"Since 2026-07-20 an explicit --mem on a GPU job is RESPECTED (no longer floored "
                f"to {floor}G), so a small job no longer has to wait for {floor}G to free up.")
            rec["basis"].append(
                f"Declaring {rec['mem_gb']}G instead of the {floor}G default leaves "
                f"{round(pool - rec['mem_gb'], 1)}G for everyone else while you run "
                f"(the default would leave only {round(pool - floor, 1)}G).")
            rec["warnings"].append(
                "Declare honestly. spark-memguard reconciles actual usage against this number "
                "when memory gets tight (>=88%) and freezes whoever exceeds their own declaration "
                "by the most. Under-declaring puts you first in line to be frozen. "
                "Rounding UP is free; rounding down is not.")
        else:
            # 第一次跑某个工具/某个输入规模时，猜不如量。让 lua 兜底给 96G 跑一趟，
            # 采样器会记下真实峰值，下一次就能精确申报。
            rec["mem_gb"] = None
            rec["confidence"] = "low"
            rec["basis"].append(
                f"GPU job with no estimate: OMIT --mem for this first run -> lua gives the safe "
                f"{floor}G default. To get an exact number for NEXT time, make the job measure "
                f"itself (PyTorch: print torch.cuda.max_memory_allocated()/1024**3 at the end). "
                f"The server also samples per-job VRAM, but nothing exposes it per job -- do not "
                f"promise the user they can just look it up afterwards.")
            rec["basis"].append(
                f"Cost of that default: only {round(pool - floor, 1)}G left for everyone else "
                f"while this runs. Worth it once, to get a real measurement.")
            rec["warnings"].append(
                "If you already know roughly how much VRAM this needs, pass --mem-guess instead "
                "-- declaring a smaller number lets others use the machine alongside you.")
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

    # ── 时间（--time）—— ★2026-07-22 策略：默认不设限 ──────────────────────
    # 不再替用户估一个"紧"的 --time：谁也估不准运行时长，估短了作业撞墙钟被 TIMEOUT
    # 杀掉、整段白跑（TIMEOUT 是终态、不触发 --requeue、不自动续），比不设限更浪费。
    # 这台机 DefaultTime=MaxTime=14 天，不写 --time 就拿满 14 天（受支持的正规路径）。
    # 防"卡死作业空占卡"的是服务器端 idleguard 闲置回收（连续闲置 ~1h 自动收），不是墙钟。
    # → 默认 time=None（gen-sbatch 不写 --time）；仅当用户主动给上限才用。
    #   详见 references/recommend-rules.md §时间。
    if time_guess:
        rec["time"] = time_guess
        rec["basis"].append(
            f"--time = your explicit cap {time_guess} (optional). A hung/forgotten job then "
            f"auto-releases at this limit instead of squatting the single GPU. Overestimate freely "
            f"-- being killed at the cap is the only downside, so leave generous margin.")
    else:
        rec["time"] = None
        rec["basis"].append(
            "No --time on purpose -> partition default (14 days = the max). We do NOT guess a tight "
            "limit: underestimating kills the job (TIMEOUT, no auto-requeue) and wastes the whole "
            "run -- worse than not capping. Stuck jobs are reclaimed by the idle-watchdog (idleguard: "
            "GPU+CPU both quiet ~1h -> auto scancel), not by wall-clock. So pass NOTHING for --time to "
            "gen-sbatch. Only warn about time if this job plausibly needs MORE than 14 days to finish "
            "(then it needs checkpointing + splitting). Pass --time only if the user gives a safe "
            "upper bound and wants a forgotten job to release sooner.")

    # ── CPU 核数（--cpus-per-task）─────────────────────────────────
    # 类比内存"想要多少 vs 现在能给多少"，但核是**弹性**的（少给只是慢，不像内存少给会 OOM），
    # 所以默认**不为多凑几个核而让 GPU 作业排队**——GPU 才是串行稀缺资源。不写 --cpus 会落
    # Slurm 默认 1 核/task，把输入特征化/MSA/BLAS 那几步串行掉（静默降级、无告警）。
    cpu_free = st.get("cpu_free")
    cpu_total = st.get("cpu_total")
    if cpus is not None:
        rec["cpus"] = cpus                        # 用户显式指定 → 尊重
        rec["basis"].append(f"CPU 核：用你指定的 {cpus} 核。")
        if cpu_free is not None and cpus > cpu_free:
            rec["warnings"].append(
                f"要 {cpus} 核，但当前只 {cpu_free}/{cpu_total} 核空闲 -> 会排队等核空出。")
    elif needs_gpu:
        want = 4                                  # 喂特征化/MSA/BLAS 的多线程，够用又不霸核
        if cpu_free is not None:
            give = max(1, min(want, cpu_free))    # 按当前空闲收敛，绝不为凑核排队
            rec["cpus"] = give
            if give < want:
                rec["basis"].append(
                    f"CPU 核：想给 {want} 核（特征化/BLAS 多线程受益），但当前只 {cpu_free}/{cpu_total} "
                    f"核空闲，先给 {give} 核以免为凑核排队——核是弹性的、少几个只是预处理慢一点，"
                    f"GPU 那段不受影响。显式 --cpus 可覆盖。")
            else:
                rec["basis"].append(
                    f"CPU 核：默认 {give} 核（当前 {cpu_free}/{cpu_total} 空闲，够）。不写会落 Slurm "
                    f"默认 1 核、把特征化/MSA/BLAS 串行掉。显式 --cpus 可覆盖。")
        else:
            rec["cpus"] = want
            rec["basis"].append(
                f"CPU 核：默认 {want} 核（读不到当前核占用，按默认给；显式 --cpus 可覆盖）。")
    else:
        # 纯 CPU 作业：核=算力本身，最优值看工具并行度——单线程给 1，BLAS/sklearn/多进程按需。
        # 不硬塞默认（免得给单线程作业预留一堆核、白占共享池），只提示按工具定 + 报当前可用核。
        rec["cpus"] = cpus                        # 可能为 None -> gen-sbatch 不写 -> 默认 1 核（按需）
        avail = f"（当前 {cpu_free}/{cpu_total} 核空闲）" if cpu_free is not None else ""
        rec["basis"].append(
            f"CPU 核：按工具并行度定——单线程/串行脚本 1 核，BLAS/sklearn/多进程按需要给{avail}。"
            f"没传 --cpus 会落 Slurm 默认 1 核，别让并行工具白跑单核。")

    if needs_gpu:
        rec["run_now"] = not st["gpu_job_running"]
        if st["gpu_job_running"]:
            rec["warnings"].append(
                f"GPU is BUSY now ({st['pending_count']} already queued) -> your job will QUEUE.")
    else:
        mem_ok = (rec["mem_gb"] or 0) <= st["mem_schedulable_gb"]
        cpu_ok = rec["cpus"] is None or cpu_free is None or rec["cpus"] <= cpu_free
        rec["run_now"] = mem_ok and cpu_ok
        if not mem_ok:
            rec["warnings"].append(
                f"Needs {rec['mem_gb']}G but only {st['mem_schedulable_gb']}G schedulable now -> will QUEUE.")
        if not cpu_ok:
            rec["warnings"].append(
                f"Needs {rec['cpus']} cores but only {cpu_free}/{cpu_total} cores free now -> will QUEUE.")

    return {"recommendation": rec, "server_now": st,
            "history_summary": {"count": hist["count"], "host_mem_peaks_gb": hist["host_mem_peaks_gb"]}}


# GPU 显存自限的已知写法。本机 cudaMalloc 绕过 cgroup（实测 8GB 只记 0.08GB），
# --mem 管不住显存，失控的 GPU 程序会吃满统一内存池把整机搞僵。所以生成 GPU 作业
# 脚本时机械检查一遍：命令里没有任何自限手段就明确警告，别让它悄悄溜过去。
_GPU_CAP_HINTS = (
    "gpu-memory-utilization",           # vLLM
    "gpu_memory_utilization",
    "set_per_process_memory_fraction",  # PyTorch
    "PYTORCH_CUDA_ALLOC_CONF",
    "memory_fraction",
    "CUDA_MPS_PINNED_DEVICE_MEM_LIMIT",
)


def gpu_cap_warning(gres, command):
    """GPU 作业但命令里看不到显存自限 -> 返回警告文本；否则 None。"""
    if not gres or "gpu" not in str(gres).lower():
        return None
    cmd = command or ""
    if any(h.lower() in cmd.lower() for h in _GPU_CAP_HINTS):
        return None
    return ("这是 GPU 作业，但命令里没有任何显存自限参数。本机 --mem 管不住显存"
            "（cudaMalloc 绕过 cgroup），只有程序自己能限制自己。"
            "支持自限的工具请带上（如 vLLM 的 --gpu-memory-utilization；"
            "自写 PyTorch 加 torch.cuda.set_per_process_memory_fraction）。"
            "工具确实不支持（如 Boltz）则靠控制输入规模，并按实测峰值申报 --mem。"
            "后果：程序涨过你申报的量、且机器内存吃紧时，spark-memguard 会冻结你这个作业"
            "（暂停不是杀掉，可解冻续跑，但会打断）。")


def boltz_num_workers_warning(command):
    """Boltz 在本机 Slurm 上默认 num_workers=2 会静默死锁 -> 提醒加 --num_workers 0。"""
    cmd = (command or "").lower()
    if "boltz" not in cmd:
        return None
    if "num_workers" in cmd:            # 用户已写了（0 或别的值），不重复提醒
        return None
    return ("⚠️ Boltz 在本机 Slurm 上默认 num_workers=2 会**静默死锁**："
            "GPU 0%、worker 卡在 futex、不报错不退出、状态却显示『在跑』，"
            "无人值守会白挂几天。命令里务必加 `--num_workers 0`。（实测 2026-07-21）")


def cpus_warning(gres, cpus):
    """GPU 作业没设 --cpus-per-task -> 会落 Slurm 默认 1 核，特征化/BLAS 被串行。"""
    is_gpu = bool(gres and "gpu" in str(gres).lower())
    if not is_gpu or cpus:
        return None
    return ("这是 GPU 作业但没设 --cpus-per-task，会落 Slurm 默认 1 核/task："
            "输入特征化 / MSA 解析 / BLAS 那几步是多线程的，1 核会把它们串行掉"
            "（GPU 那段不受影响，但预处理更慢）。走 recommend 拿按当前空闲核算好的默认值，"
            "或显式 --cpus。")


def vram_cap_block(mem_gb, pool_gb):
    """生成一段可直接粘进脚本的显存自限提示（按申报量算好比例）。

    刻意生成成**注释**而不是可执行代码：不同工具限制显存的方式完全不同，
    自动插一行 torch 调用可能直接改坏用户的程序。给出算好的数字让人自己选。
    """
    if not mem_gb or not pool_gb:
        return []
    frac = max(0.02, min(0.95, round(float(mem_gb) / float(pool_gb), 2)))
    return [
        "# ── 显存自限（强烈建议）──────────────────────────────",
        f"# 本机 --mem 管不住显存，上面那个 {mem_gb}G 是**申报**：机器内存吃紧时",
        f"# spark-memguard 按它对账，超出最多的作业会被冻结。请让程序自己也守住：",
        f"#   vLLM     : --gpu-memory-utilization {frac}",
        f"#   PyTorch  : torch.cuda.set_per_process_memory_fraction({frac})",
        "#   不支持的工具（如 Boltz）：控制输入规模；结尾自己打印",
        "#     torch.cuda.max_memory_allocated()/1024**3  拿到真值后修正申报",
        "# ─────────────────────────────────────────────────────",
    ]


def workdir_warning(workdir):
    """没给落点 → 日志会散在 SSH 登录目录（通常是 home 根）。"""
    if workdir:
        return None
    return ("没传 --workdir：Slurm 的 --output 是相对路径，作业经 SSH 提交时相对的是登录目录"
            "（通常就是 home 根）。跑几次之后 home 根会散一地 <作业名>-<ID>.log，"
            "看不出哪个对应哪次分析。把项目目录传给 --workdir，日志就进 <workdir>/logs/。")


def cmd_gen_sbatch(cfg, name, gres, mem_gb, time, cpus, command, out, workdir=None):
    # 纯 CPU 作业不写 --mem 会被 job_submit.lua 在提交阶段直接拒绝，
    # 生成这样的脚本只会让用户白跑一趟拿个报错。宁可在这里就拦住。
    if not mem_gb and not (gres and "gpu" in str(gres).lower()):
        die("CPU 作业必须指定 --mem-gb：服务器的 job_submit.lua 会拒绝没写 --mem 的 CPU 作业。"
            "先用 recommend 拿一个推荐值，或让用户给个估计。")
    L = ["#!/bin/bash",
         f"#SBATCH --job-name={name}",
         f"#SBATCH --partition={cfg.get('partition', 'main')}"]
    if gres:
        L.append(f"#SBATCH --gres={gres}")
    if cpus:
        L.append(f"#SBATCH --cpus-per-task={cpus}")
    is_gpu = bool(gres and "gpu" in str(gres).lower())
    if mem_gb:
        L.append(f"#SBATCH --mem={mem_gb}G")
    elif is_gpu:
        # GPU 作业不写 --mem 是合法的（lua 兜底给 96G），但要让人知道这是有意为之
        L.append("# 未写 --mem：job_submit.lua 会兜底预留 96000M（这一趟别人只剩 ~18G）。")
        L.append("# 想让下一趟能精确申报，让程序自己报峰值 —— PyTorch 在结尾加：")
        L.append("#   print('VRAM peak GB:', torch.cuda.max_memory_allocated()/1024**3)")
        L.append("# 拿到数后改成 #SBATCH --mem=<实测+30%>G，别人就能和你同时用机器。")
        L.append("# （服务器侧也在采显存，但按作业的值取不回来，别指望跑完去面板查。）")
    if time:
        L.append(f"#SBATCH --time={time}")
    if workdir:
        # workdir 是**服务器上**的绝对路径。在 Git Bash / MSYS 下调用本脚本时，
        # 形如 /home/... 的参数会被自动改写成 C:/Program Files/Git/home/...，
        # 生成的脚本照样能提交，但作业一 chdir 就失败、且失败信息没地方写 —— 在这里拦住。
        if not workdir.startswith("/"):
            die(f"--workdir 必须是服务器上的绝对路径（以 / 开头），实际收到：{workdir}\n"
                "看着像被 MSYS 路径转换改写过。在 Git Bash 里调用时前面加 MSYS_NO_PATHCONV=1，"
                "或改用 PowerShell 调用。")
        # 作业在项目目录里跑，日志进该项目的 logs/ —— 产物跟着项目走，不堆在 home 根。
        L.append(f"#SBATCH --chdir={workdir}")
        L.append("#SBATCH --output=logs/%x-%j.log")
    else:
        L.append("#SBATCH --output=%x-%j.log")
    if is_gpu:
        try:
            pool = cmd_status(cfg).get("mem_allocatable_gb")
        except Exception:
            pool = None       # 连不上服务器不该让生成脚本失败，只是少一段提示
        if pool:
            L += [""] + vram_cap_block(mem_gb, pool)
    L += ["", command or "# TODO: your command here", ""]
    script = "\n".join(L)
    result = {"sbatch_script": script}
    if out:
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)
        result["written_to"] = out
    warns = [w for w in (gpu_cap_warning(gres, command),
                         cpus_warning(gres, cpus),
                         workdir_warning(workdir),
                         boltz_num_workers_warning(command)) if w]
    if warns:
        result["warnings"] = warns
    result["next"] = "Review, get user OK, then: advise.py submit --script <file>"
    return result


def cmd_submit(cfg, script_path):
    if not os.path.exists(script_path):
        die(f"script not found: {script_path}")
    with open(script_path, encoding="utf-8") as f:
        script = f.read()
    # --chdir 的目标和它下面的 logs/ 必须在作业启动前就存在：Slurm 打不开 --output
    # 指定的文件时，作业直接失败，而且失败信息本身也没地方写。
    created = None
    m = re.search(r"^#SBATCH\s+--chdir=(\S+)", script, re.M)
    if m:
        wd = m.group(1)
        ssh_run(cfg, f"mkdir -p {shlex.quote(wd)}/logs")
        created = f"{wd}/logs"
    out = ssh_run(cfg, "sbatch", stdin_data=script)
    result = {"submitted": out.strip(), "as_user": cfg["user"]}
    if created:
        result["ensured_dir"] = created
    return result


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
        "server": f'{cfg["node"]} ({resolve_host(cfg)})',
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
    # Windows 控制台默认 cp936，emit()/die() 用 ensure_ascii=False 输出的中文（basis/warnings）
    # 会 mojibake。强制 stdout 走 UTF-8，让中文在任何调用方（Bash/PowerShell）都正确。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
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
    g.add_argument("--workdir", default=None,
                   help="absolute path of the project dir on the server; job runs there and "
                        "logs go to <workdir>/logs/ instead of piling up in the home dir")

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
        emit(cmd_gen_sbatch(cfg, args.name, args.gres, args.mem_gb, args.time, args.cpus, args.command,
                            args.out, args.workdir))
    elif args.cmd == "submit":
        emit(cmd_submit(cfg, args.script))
    elif args.cmd == "probe-env":
        emit(cmd_probe_env(cfg))


if __name__ == "__main__":
    main()
