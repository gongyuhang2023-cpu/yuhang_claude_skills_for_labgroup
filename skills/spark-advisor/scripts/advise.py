#!/usr/bin/env python3
"""
spark-advisor engine -- recommend Slurm job parameters for a shared GPU server.

Pure standard library. All the fragile bits (SSH, scontrol/squeue/sacct parsing)
live here and return CLEAN JSON, so the calling AI never freehands SSH and never
gets a wall of raw terminal output dumped into its context.

Subcommands:
  status            server state: Slurm accounting AND the physical truth from
                    nvidia-smi (they disagree when someone bypasses the queue)
  history           this user's past jobs from sacct (host-mem peak, elapsed)
  recommend         baseline --gres/--mem/--time + confidence, combining the above
  gen-sbatch        emit an sbatch script from chosen params (does NOT submit)
  submit            submit a local sbatch script via the server (gated by SKILL.md)
  wait              watch a job; returns the moment it finishes / fails / is
                    CANCELLED / gets FROZEN. Run it in the background: a frozen
                    job looks exactly like a running one in squeue, so without
                    this an agent just waits forever. Fixed exit codes, see below.
  cancel-frozen     cleanly cancel a frozen job: unfreeze -> scancel -> verify
                    four conditions. Plain scancel on a frozen job wedges it in
                    COMPLETING and never returns the memory.
  gpu-peak          a job's observed LOWER BOUND on GPU memory (5s sampling can
                    miss spikes), plus host memory pressure during the run, which
                    decides whether "it finished, unfrozen" counts as an upper bound
  debug-start/run/  interactive salloc session: hold resources ONCE, then push many
  list/end          short runs into it via srun (33-61ms each, measured 2026-09-06).
                    For the debug loop "run 2min -> think 20min -> run again", where
                    re-queueing every round costs hours (measured: 2h51m wait for a
                    51s job). Default 6h. --time cannot be raised later (permission
                    denied) and expiry SIGTERMs whatever is running, so it defaults
                    generous; idleguard exempts interactive jobs, so --time is the
                    only safety net there is.

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
import time

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


def ssh_try(cfg, remote_cmd, timeout=30):
    """ssh_run 的不致命版本：返回 (rc, stdout, stderr)，绝不 die()。

    ssh_run 在命令失败时直接退出整个进程 —— 对 status/recommend 这种一次性查询没问题，
    但 wait/cancel-frozen 必须自己区分「命令失败」与「查到的结果就是空」，
    并把失败映射成一个明确的错误码返回给调用者（含 AI agent）。
    把两者混成一个结果，正是这次事故里看门狗犯的同一个错误。
    """
    target = f'{cfg["user"]}@{resolve_host(cfg)}'
    cmd = ["ssh"] + list(cfg.get("ssh_opts", ["-o", "ConnectTimeout=12", "-o", "BatchMode=yes"]))
    if cfg.get("ssh_key"):
        cmd += ["-i", cfg["ssh_key"]]
    cmd += [target, remote_cmd]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return 255, "", "ssh not found on PATH"
    except subprocess.TimeoutExpired:
        return 255, "", f"ssh timed out after {timeout}s"
    return (r.returncode,
            r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"))


# GPU 上每个计算进程的身份。一次 ssh 拿全，避免 N 次往返。
_GPU_PROBE = r"""
if ! command -v nvidia-smi >/dev/null 2>&1; then echo "NVSMI_MISSING"; exit 0; fi
if ! nvidia-smi --query-compute-apps=pid --format=csv,noheader >/dev/null 2>&1; then
  echo "NVSMI_FAIL"; exit 0
fi
echo "NVSMI_OK"
nvidia-smi --query-compute-apps=pid,used_gpu_memory --format=csv,noheader,nounits |
while IFS=, read -r pid mb; do
  pid=$(echo "$pid" | tr -d ' '); mb=$(echo "$mb" | tr -d ' ')
  [ -n "$pid" ] || continue
  printf 'P|%s|%s|%s|%s|%s\n' "$pid" "$mb" \
    "$(cat /proc/$pid/comm 2>/dev/null)" \
    "$(stat -c %U /proc/$pid 2>/dev/null)" \
    "$(head -1 /proc/$pid/cgroup 2>/dev/null)"
done
echo "FROZEN_BEGIN"
cat /run/spark-usage/frozen-jobs.json 2>/dev/null || echo "{}"; echo
"""


def gpu_physical(cfg):
    """GPU 的**物理**占用真相，而不是 Slurm 的记账。

    ★ 为什么不能只信 Slurm：它是记账系统，只知道自己派出去的作业。有人不经队列
    直接跑，它一无所知，照旧报告「GPU 空闲」—— 2026-08-27 当天实测就是如此。
    照着这个提交作业，两个程序就会抢同一张卡。

    ★ 为什么不做成 busy/free 两档：本机的桌面远程服务常驻占着 176 MiB，按「有进程
    就算占用」的话 GPU 会永远显示占用，等于没有信息。所以分开呈现：系统服务 /
    Slurm 作业 / 未归因的用户进程，三者含义完全不同。

    ★ 查询失败一律报 unknown，**绝不降级成 free** —— 那正是把"我没查到"说成
    "确实没有"，是这次事故的根错误。
    """
    rc, out, err = ssh_try(cfg, _GPU_PROBE, timeout=30)
    if rc != 0:
        return {"state": "unknown", "reason": f"ssh 失败: {err.strip()[:160]}",
                "procs": [], "frozen_jobs": []}
    lines = out.splitlines()
    head = lines[0].strip() if lines else ""
    if head == "NVSMI_MISSING":
        return {"state": "unknown", "reason": "nvidia-smi 不存在", "procs": [],
                "frozen_jobs": []}
    if head != "NVSMI_OK":
        return {"state": "unknown", "reason": "nvidia-smi 查询失败（不代表 GPU 空闲）",
                "procs": [], "frozen_jobs": []}

    procs, frozen_raw, in_frozen = [], [], False
    for ln in lines[1:]:
        if ln.strip() == "FROZEN_BEGIN":
            in_frozen = True
            continue
        if in_frozen:
            frozen_raw.append(ln)
            continue
        if not ln.startswith("P|"):
            continue
        f = ln.split("|", 5)
        if len(f) < 6:
            continue
        cg = f[5].strip()
        m = re.search(r"/job_(\d+)", cg)
        # 归属三分：Slurm 作业 / systemd 服务 / 没走队列的用户进程
        if m:
            kind, job_id = "slurm-job", m.group(1)
        elif cg.endswith(".service"):
            kind, job_id = "system-service", None
        else:
            kind, job_id = "unattributed", None
        try:
            mb = float(f[2])
        except ValueError:
            mb = 0.0
        procs.append({"pid": int0(f[1]), "gpu_mb": mb, "comm": f[3],
                      "user": f[4], "cgroup": cg, "kind": kind, "job_id": job_id})

    try:
        frozen = json.loads("\n".join(frozen_raw) or "{}")
    except ValueError:
        frozen = {}

    slurm = [p for p in procs if p["kind"] == "slurm-job"]
    system = [p for p in procs if p["kind"] == "system-service"]
    loose = [p for p in procs if p["kind"] == "unattributed"]
    if loose:
        state = "unattributed"       # 有人没走队列在用卡 —— 提交前必须知道
    elif slurm:
        state = "slurm-job"
    elif system:
        state = "system-use"         # 只有系统服务，用户计算意义上是空闲的
    else:
        state = "free"
    return {
        "state": state,
        "reason": "",
        "procs": procs,
        "slurm_job_procs": slurm,
        "system_procs": system,
        "unattributed_procs": loose,
        "unattributed_mb": round(sum(p["gpu_mb"] for p in loose), 1),
        "frozen_jobs": frozen.get("frozen", []),
        "frozen_list_complete": frozen.get("complete"),
    }


# ---- wait 的固定返回码。不透传 Slurm 的退出码，调用方只需认这几个数 ----
WAIT_OK          = 0    # 正常结束且 ExitCode 为 0
WAIT_NONZERO     = 10   # 跑完了但退出码非零
WAIT_FROZEN      = 11   # ★ 被 memguard 冻结 —— 不是卡死，别 scancel
WAIT_CANCELLED   = 12   # 被取消
WAIT_FAILED      = 13   # FAILED / TIMEOUT / OOM / NODE_FAIL / PREEMPTED
WAIT_TIMEOUT     = 20   # 监控自身超时（**不是**作业超时）
WAIT_QUERY_FAIL  = 21   # 查询失败 —— 不知道作业怎么样了，绝不当成完成
WAIT_SSH_FAIL    = 255  # 连不上服务器

_WAIT_PROBE = r"""
echo "SQ_BEGIN"
squeue -h -j {jid} -o '%T' 2>/dev/null
echo "FZ_BEGIN"
cat /run/spark-usage/frozen-jobs.json 2>/dev/null || echo '{{}}'; echo
echo "CGFZ_BEGIN"
CG=$(ls -d /sys/fs/cgroup/system.slice/*slurmstepd.scope/job_{jid} 2>/dev/null | head -1)
[ -n "$CG" ] && cat "$CG/cgroup.freeze" 2>/dev/null
echo "SA_BEGIN"
sacct -X -n -P -j {jid} -o State,ExitCode 2>/dev/null
"""


def cmd_wait(cfg, jid, interval, max_wait):
    """盯住一个作业，出事就返回 —— 给 AI agent 用的。

    ## 为什么需要它

    AI agent 提交完作业不会去轮询（费 token，而且正常运行时轮询没有意义），
    它等的是「命令返回」这个事件。**而冻结不产生任何事件**：
      · wall 广播到不了一个已经断开的 ssh 会话；
      · 被冻结的作业在 squeue 里状态**仍然显示 RUNNING** —— 冻结是 cgroup 层的事，
        Slurm 完全不知道。
    于是 agent 只能看到"在跑但日志不动"，然后误判成卡死去 scancel，
    正好触发把作业永久卡死、内存永不释放的那条路径。这就是 2026-08-27 事故的走法。

    本命令把轮询搬到**不花钱的地方**（一个本地循环 + 每轮一次短 ssh），
    出事就返回，于是冻结对 agent 而言变成一个和"程序挂了"同类的事件。

    ## 为什么在本地轮询而不是在服务器上跑循环

    远端循环会在客户端断线、休眠或被杀时继续存活好几天，谁也不知道它还在。
    本地循环随调用者一起消失，没有这个问题。

    ## 为什么作业从 squeue 消失不等于成功

    它可能是失败、被取消，也可能只是这一次查询失败了；而且 accounting 入库有延迟。
    所以终态一律以 sacct 为准，并给 accounting 一小段收敛时间。
    """
    t0 = time.time()
    gone_since = None       # 作业从 squeue 消失的时刻，用于给 sacct 留收敛时间
    probe = _WAIT_PROBE.format(jid=shlex.quote(str(jid)))

    while True:
        if max_wait and time.time() - t0 > max_wait:
            return WAIT_TIMEOUT, {"job": jid, "result": "wait-timeout",
                                  "waited_s": int(time.time() - t0),
                                  "note": "监控自身超时，作业状态未知 —— 这不代表作业失败"}

        rc, out, err = ssh_try(cfg, probe, timeout=30)
        if rc == 255 or "ssh not found" in err or "timed out" in err:
            return WAIT_SSH_FAIL, {"job": jid, "result": "ssh-failed",
                                   "error": err.strip()[:200]}
        if rc != 0:
            return WAIT_QUERY_FAIL, {"job": jid, "result": "query-failed",
                                     "error": err.strip()[:200],
                                     "note": "查询失败，作业状态未知 —— 不要当成已完成"}

        sq, fz, cgfz, sa = _split_sections(
            out, ["SQ_BEGIN", "FZ_BEGIN", "CGFZ_BEGIN", "SA_BEGIN"])
        state = sq.strip().splitlines()[0].strip() if sq.strip() else ""

        # ---- 冻结优先判断：它在 squeue 里长得跟正常运行一模一样 ----
        # 两个来源都认：memguard 维护的清单（带 user/name 等元信息），以及直接读
        # cgroup.freeze（后备）。只靠清单的话，memguard 没跑、刚重启、或文件还没
        # 生成时就会漏判 —— 而漏判的后果是 agent 以为作业在正常跑，继续等下去。
        try:
            frozen = json.loads(fz.strip() or "{}")
        except ValueError:
            frozen = {}
        frozen_ids = {str(j.get("job_id")) for j in frozen.get("frozen", [])}
        if str(jid) in frozen_ids or cgfz.strip() == "1":
            return WAIT_FROZEN, {
                "job": jid, "result": "frozen", "slurm_state": state,
                "waited_s": int(time.time() - t0),
                "note": ("作业已被 memguard 冻结：实际用量超出申报量。"
                         "它**没有死**，已算完的部分都在。"
                         "不要直接 scancel（信号送不进去，会永久卡住且不还内存）——"
                         "用 `advise.py cancel-frozen %s`。"
                         "也不要拿任何观测到的数字当它的真实需求：它是在越线那一刻"
                         "被打断的，真实峰值只会更高。" % jid),
            }

        if state:                    # 还在队列里跑着/排着
            gone_since = None
            time.sleep(interval)
            continue

        # ---- 不在 squeue 了 → 等 sacct 给终态，别急着说"完成了" ----
        if gone_since is None:
            gone_since = time.time()
        final = _parse_sacct(sa)
        if final is None:
            if time.time() - gone_since > 60:
                return WAIT_QUERY_FAIL, {
                    "job": jid, "result": "no-final-state",
                    "note": "作业已离开队列，但 60 秒内 sacct 仍给不出终态 —— 状态未知"}
            time.sleep(min(interval, 5))
            continue

        st, exit_code = final
        payload = {"job": jid, "result": st, "exit_code": exit_code,
                   "waited_s": int(time.time() - t0)}
        if st.startswith("COMPLETED"):
            return (WAIT_OK if exit_code in ("0:0", "0") else WAIT_NONZERO), payload
        if st.startswith("CANCELLED"):
            return WAIT_CANCELLED, payload
        return WAIT_FAILED, payload


def cmd_gpu_peak(cfg, jid):
    """一个作业的显存**观测下界** + 期间机器水位 + Slurm 终态，合成夹逼判断。

    ## 为什么不是"查峰值"

    采样每 5 秒一次，冲高又回落的尖峰拍不到（实测 Boltz 的读数会在 4.2~11.3 GiB
    之间来回跳，说明它会降，那就存在没被拍到的更高点）。所以这个数永远是**下界**。

    ## 夹逼：下界来自采样，上界来自"跑完了没被冻"

    但那个上界**有条件**：memguard 只在内存 ≥88% 时查账。机器空闲时跑完，
    只说明没人来查你的账，不说明你没超申报。所以必须同时看当时的机器水位 ——
    水位到过查账线、且作业正常完成，申报值才是有效上界。

    这套办法的好处是**不需要改被测程序**：任何工具都适用，包括源码你不想碰的。
    """
    rc, out, err = ssh_try(
        cfg, "echo PEAK_BEGIN; spark-usage --gpu-peak %s --json 2>/dev/null; echo; "
             "echo SACCT_BEGIN; sacct -X -n -P -j %s -o State,ReqMem,ExitCode 2>/dev/null"
             % (shlex.quote(str(jid)), shlex.quote(str(jid))), timeout=30)
    if rc != 0:
        return {"job": jid, "error": f"查询失败: {err.strip()[:200]}"}
    head, sa = _split_sections(out, ["PEAK_BEGIN", "SACCT_BEGIN"])
    try:
        prof = json.loads(head.strip().splitlines()[0]) if head.strip() else {}
    except (ValueError, IndexError):
        prof = {}
    if not prof.get("peak_mb"):
        return {"job": jid, "observed_lower_bound_gb": None,
                "note": "没有显存采样记录（不是 GPU 作业 / 跑得太短 / 记录已清理）"}

    state = req_mem = None
    for ln in (sa or "").strip().splitlines():
        f = ln.strip().split("|")
        if len(f) >= 2 and f[0].strip():
            state, req_mem = f[0].strip(), f[1].strip()
            break

    lower = prof["peak_mb"] / 1024.0
    hi_ratio = prof.get("host_mem_ratio_max")
    completed = bool(state and state.startswith("COMPLETED"))
    audited = bool(hi_ratio is not None and hi_ratio >= 0.88)
    upper = mb_to_gb(mem_to_mb_local(req_mem)) if (completed and audited and req_mem) else None

    res = {
        "job": jid,
        "observed_lower_bound_gb": round(lower, 1),
        "lower_bound_caveat": "采样每 5s 一次，尖峰可能被漏掉；真实峰值只会更大",
        "samples": prof.get("samples"),
        "host_mem_ratio_max": hi_ratio,
        "slurm_state": state,
        "requested_mem": req_mem,
        "upper_bound_valid": bool(upper),
        "effective_upper_bound_gb": upper,
    }
    if upper:
        res["conclusion"] = (
            "真实需求落在 [%.1f, %.1f] GiB：下界来自采样，上界成立是因为这次作业"
            "正常完成、且期间机器水位到过 %.0f%%（查账线之上，memguard 真的在查账）。"
            % (lower, upper, hi_ratio * 100))
    elif completed and not audited:
        res["conclusion"] = (
            "只有下界 %.1f GiB。这次虽然跑完了，但期间机器水位最高才 %.0f%%，"
            "没到查账线 —— memguard 当时根本没查账，所以「没被冻」不能当上界。"
            % (lower, (hi_ratio or 0) * 100))
    else:
        res["conclusion"] = (
            "只有下界 %.1f GiB（作业终态 %s，不是正常完成，拿不到上界）。"
            % (lower, state or "未知"))
    return res


def mem_to_mb_local(s):
    """'48G' / '96000M' -> MB。sacct 的 ReqMem 有时带 c/n 后缀，一并剥掉。"""
    m = re.match(r"^([\d.]+)\s*([KMGT]?)", str(s or "").strip(), re.I)
    if not m:
        return 0
    return float(m.group(1)) * {"K": 1 / 1024.0, "M": 1.0, "G": 1024.0,
                                "T": 1048576.0}.get((m.group(2) or "M").upper(), 1.0)


CF_OK            = 0    # 干净退出：终态已定、cgroup 空了、GPU 进程没了
CF_STUCK         = 30   # cleanup-stuck：取消请求发出去了，但资源没确认释放
CF_NOT_FROZEN    = 31   # 这个作业没被冻结 —— 不该用这条命令
CF_NEED_PRIV     = 32   # 解冻需要 root，而当前拿不到
CF_DRY_RUN       = 33   # 只做了检查（没带 --yes）

_CF_INSPECT = r"""
JID={jid}
CG=$(ls -d /sys/fs/cgroup/system.slice/*slurmstepd.scope/job_$JID 2>/dev/null | head -1)
echo "CG_PATH_BEGIN"; echo "$CG"
echo "FREEZE_BEGIN"; [ -n "$CG" ] && cat "$CG/cgroup.freeze" 2>/dev/null
echo "SQ_BEGIN"; squeue -h -j "$JID" -o '%T' 2>/dev/null
echo "OWNER_BEGIN"; squeue -h -j "$JID" -o '%u' 2>/dev/null
echo "AUDIT_BEGIN"; cat /run/spark-usage/frozen-jobs.json 2>/dev/null || echo '{{}}'; echo
echo "SUDO_BEGIN"; sudo -n true 2>/dev/null && echo YES || echo NO
"""

_CF_EXECUTE = r"""
JID={jid}
CG={cg}
sudo sh -c 'echo 0 > "'"$CG"'/cgroup.freeze"' 2>&1 || echo "UNFREEZE_FAILED"
sleep 1
sudo scancel "$JID" 2>&1 || echo "SCANCEL_FAILED"
sleep 6
echo "SACCT_BEGIN"; sacct -X -n -P -j "$JID" -o State,ExitCode 2>/dev/null
echo "CGSTATE_BEGIN"; if [ -d "$CG" ]; then cat "$CG/cgroup.events" 2>/dev/null; else echo "GONE"; fi
echo "GPUPROC_BEGIN"
for p in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null); do
  if head -1 /proc/$p/cgroup 2>/dev/null | grep -q "job_$JID"; then echo "STILL:$p"; fi
done
echo "MEM_BEGIN"; grep -E '^(MemTotal|MemAvailable):' /proc/meminfo
"""


def cmd_cancel_frozen(cfg, jid, confirm):
    """干净地取消一个被冻结的作业：解冻 → 取消 → 确认真的退干净了。

    ## 为什么必须是一条专门的命令

    对一个**已冻结**的作业直接 scancel，信号送不进去（冻结的进程处理不了信号），
    作业会永远卡在 COMPLETING、内存一直不还、squeue 里赖着不走。2026-08-27 就是
    这么把整台机器堵了三小时。而"记得先解冻"这种知识，靠文档和一次性提示是传不住的
    —— 事故当天的提示确实广播过，当事人没看到。

    所以把正确顺序封装进工具：调用者（人或 AI）不需要知道这个坑存在。

    ## 为什么"确认"要四个条件

    scancel 返回 0 只表示请求被受理，**不表示清理完成**。只看其中一条都会误判：
      · cgroup 目录消失得比进程晚，也可能相反；
      · Slurm 给出终态时资源未必已经释放。
    四条都满足才算干净；只满足一部分就报 cleanup-stuck，绝不报成功。
    """
    rc, out, err = ssh_try(cfg, _CF_INSPECT.format(jid=shlex.quote(str(jid))), timeout=30)
    if rc != 0:
        return WAIT_SSH_FAIL, {"job": jid, "result": "ssh-failed",
                               "error": err.strip()[:200]}
    cg, fz, sq, owner, aud, sudo_ok = _split_sections(
        out, ["CG_PATH_BEGIN", "FREEZE_BEGIN", "SQ_BEGIN", "OWNER_BEGIN",
              "AUDIT_BEGIN", "SUDO_BEGIN"])
    cg, fz = cg.strip(), fz.strip()
    state, owner = sq.strip(), owner.strip()

    if not cg:
        return CF_NOT_FROZEN, {
            "job": jid, "result": "no-cgroup",
            "note": "找不到该作业的 cgroup —— 它可能已经结束了。用普通 scancel 即可。"}
    if fz != "1":
        return CF_NOT_FROZEN, {
            "job": jid, "result": "not-frozen", "cgroup": cg, "slurm_state": state,
            "note": ("该作业**没有被冻结**（cgroup.freeze=%s），不需要这条命令。"
                     "直接 scancel 就行。" % (fz or "读不到"))}

    # 谁冻的：从 memguard 维护的冻结清单里取（该清单带 frozen_by_memguard 标志，
    # 是它从自己的审计记录里恢复出来的 —— 审计文件本身普通用户读不到）。
    # 人工冻结的作业不该由工具替人决定，所以这个标志要如实呈现，读不到就说读不到。
    entry, list_seen = None, False
    try:
        fl = json.loads(aud.strip() or "{}")
        list_seen = bool(fl)
        for it in fl.get("frozen", []):
            if str(it.get("job_id")) == str(jid):
                entry = it
                break
    except ValueError:
        pass
    plan = {
        "job": jid, "owner": owner, "cgroup": cg, "slurm_state": state,
        "frozen": True,
        "frozen_by_memguard": (entry or {}).get("frozen_by_memguard")
                              if entry else (False if list_seen else None),
        "frozen_at": (entry or {}).get("frozen_at"),
        "frozen_list_seen": list_seen,   # False = 清单读不到，来源无从判断
        "steps": ["解冻 cgroup.freeze -> 0", "scancel", "等待并确认四个条件"],
    }
    if plan["frozen_by_memguard"] is None:
        plan["source_note"] = ("读不到 memguard 的冻结清单，**无法确认是谁冻的**。"
                               "如果这是管理员手工冻的，取消前请先跟他确认。")
    elif plan["frozen_by_memguard"] is False:
        plan["source_note"] = ("该作业在冻结清单里，但**不是 memguard 冻的**"
                               "（可能是人工操作）。取消前请确认对方的意图。")
    if sudo_ok.strip() != "YES":
        plan.update({"result": "need-privilege",
                     "note": "解冻需要 root（写 cgroup.freeze），当前 sudo 不可用。"
                             "请管理员执行，或为该用户配置 sudoers。"})
        return CF_NEED_PRIV, plan
    if not confirm:
        plan.update({"result": "dry-run",
                     "note": "以上是将要执行的步骤。确认无误后加 --yes 真正执行。"})
        return CF_DRY_RUN, plan

    rc2, out2, err2 = ssh_try(
        cfg, _CF_EXECUTE.format(jid=shlex.quote(str(jid)), cg=shlex.quote(cg)),
        timeout=90)
    if rc2 != 0:
        plan.update({"result": "execute-failed", "error": err2.strip()[:200]})
        return CF_STUCK, plan

    sacct, cgstate, gpuproc, mem = _split_sections(
        out2, ["SACCT_BEGIN", "CGSTATE_BEGIN", "GPUPROC_BEGIN", "MEM_BEGIN"])
    final = _parse_sacct(sacct)
    still = [l.split(":", 1)[1] for l in gpuproc.splitlines() if l.startswith("STILL:")]
    cg_gone = "GONE" in cgstate
    cg_empty = "populated 0" in cgstate

    checks = {
        "sacct_terminal": bool(final) and not final[0].startswith("RUNNING"),
        "cgroup_released": cg_gone or cg_empty,
        "gpu_procs_gone": not still,
        "unfreeze_ok": "UNFREEZE_FAILED" not in out2,
    }
    plan.update({
        "final_state": final[0] if final else None,
        "exit_code": final[1] if final else None,
        "cgroup_state": "gone" if cg_gone else cgstate.strip()[:80],
        "gpu_procs_still_alive": still,
        "checks": checks,
        "mem_after": mem.strip().replace("\n", " "),
    })
    if all(checks.values()):
        plan.update({"result": "cancelled-clean",
                     "note": "四项确认全部通过：终态已定、cgroup 已释放、"
                             "该作业的 GPU 进程已消失、解冻成功。"})
        return CF_OK, plan
    plan.update({
        "result": "cleanup-stuck",
        "note": ("取消请求已发出，但**资源未确认释放**（见 checks）。"
                 "不要当成成功 —— 这正是 2026-08-27 那种状态。请人工检查，"
                 "必要时联系管理员。"),
    })
    return CF_STUCK, plan


def _split_sections(text, markers):
    """把探针输出按标记切成几段。

    ★ 标记必须是非空、且不会出现在数据里的字面行。用空串当标记会把第一个空行
    当成分隔点，前面那段直接丢掉 —— 2026-08-28 实测踩过一次（JSON 整段被吞，
    表现为"查不到数据"而不是报错）。这类分段解析出错都是**静默**的，所以宁可
    在这里硬拦。
    """
    bad = [m for m in markers if not (m or "").strip()]
    if bad:
        raise ValueError("_split_sections: 标记不能为空 —— %r" % (markers,))
    parts, cur, out = [], [], []
    idx = 0
    for ln in text.splitlines():
        if idx < len(markers) and ln.strip() == markers[idx]:
            if idx > 0:
                out.append("\n".join(cur))
            cur = []
            idx += 1
            continue
        cur.append(ln)
    out.append("\n".join(cur))
    while len(out) < len(markers):
        out.append("")
    return out[:len(markers)]


def _parse_sacct(sa):
    """sacct -X -n -P 的一行：State|ExitCode。拿不到返回 None（区别于"拿到了但是空"）。"""
    for ln in (sa or "").strip().splitlines():
        f = ln.strip().split("|")
        if len(f) >= 2 and f[0].strip():
            return f[0].strip(), f[1].strip()
    return None


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

    phys = gpu_physical(cfg)

    # Slurm 说空闲、但卡上真有用户进程 —— 交上去就撞。2026-08-27 实测出现过。
    gpu_conflict = (gpu_used < gpu_total) and phys["state"] == "unattributed"
    warns = []
    if gpu_conflict:
        warns.append(
            "★ Slurm 报告 GPU 空闲，但卡上有 %d 个**未走队列**的用户进程"
            "（合计 %.1f GB）。现在提交 GPU 作业会和它抢同一张卡。"
            % (len(phys["unattributed_procs"]), phys["unattributed_mb"] / 1024.0))
    if phys["state"] == "unknown":
        warns.append("★ 查不到 GPU 的物理占用（%s）—— 这**不等于**空闲，"
                     "提交前请人工确认。" % phys["reason"])
    if phys.get("frozen_jobs"):
        warns.append(
            "有 %d 个作业处于**冻结**状态（%s）。它们占着资源但不会前进，"
            "只有作业所有者能处置；查看用 `advise.py status`，取消用 `cancel-frozen`。"
            % (len(phys["frozen_jobs"]),
               "、".join(str(j.get("job_id")) for j in phys["frozen_jobs"][:5])))

    return {
        "node": node,
        "node_state": d.get("State", ""),
        "gpu_total": gpu_total,
        "gpu_used": gpu_used,
        # ↓ 这两个是 **Slurm 记账**，不是物理真相。没走队列的进程它看不见。
        "gpu_free": gpu_used < gpu_total,
        "gpu_job_running": any(j["gpu"] for j in running),
        # ↓ 这个才是卡上真实发生的事，来自 nvidia-smi
        "gpu_physical": phys,
        "gpu_conflict_risk": gpu_conflict,
        "warnings": warns,
        # 检查完到作业真正被调度之间仍有窗口：排队中的作业可能在检查后才启动，
        # 裸跑也可能在作业启动后才出现。这里只保证「如实展示」，不保证不撞。
        "collision_caveat": ("提交前检查挡不住所有撞卡：检查完到调度之间，"
                             "别人可能才开始裸跑，或排队作业才被派上。"),
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
echo "===CONDA==="; { command -v conda mamba micromamba ; } 2>/dev/null || for d in ~/miniforge3 ~/miniconda3 ~/anaconda3 ~/mambaforge; do [ -x "$d/bin/conda" ] && echo "$d/bin/conda (installed, absent from non-interactive PATH)"; done
echo "===RSCRIPT==="; command -v Rscript 2>/dev/null; for d in ~/miniforge3 ~/miniconda3 ~/anaconda3 ~/mambaforge; do ls "$d"/envs/*/bin/Rscript 2>/dev/null; done | head -20
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
            # R 单独探：非交互 ssh 不初始化 conda，`command -v Rscript` 必然 not found，
            # 而 R 恰恰只装在 conda env 里 —— 只看 PATH 会一路得出「这台机器没有 R」
            "r": many("RSCRIPT") or "none",
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
                "Re-probe if an env command fails or this looks stale. "
                "⚠ 非交互 ssh 不初始化 conda：PATH 里查不到 conda / Rscript "
                "不等于没装，以本文件的 conda / r 字段为准，调用走绝对路径。",
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


# ── 交互式调试会话（salloc）──────────────────────────────────────────────────
# 为什么要这一档：调试是「跑 2 分钟 → 想 20 分钟 → 再跑」的循环。每一轮都走 sbatch
# 就要重排一次队 —— 2026-09-02 实测同一个 s2-probe 排了 2h51m 才轮上，而它只跑 51 秒。
# salloc 把「壳」和「内容」拆开：壳（分配）按申请的时长活着，内容（每次 srun）想跑几次
# 跑几次。实测每次启动 33–61ms，且跨 ssh 连接照样进得去（新连接 36–51ms）——
# 所以调用方不必自己开终端、不必挂 tmux。
#
# ⚠️ --time 在这里是强制的，不是礼貌，是机制。三条实测支撑（2026-09-06）：
#   1) idleguard（/opt/spark-usage/idleguard.py:35）对 BatchFlag=0 的交互式作业
#      **豁免回收** —— 这正是它不打断你思考的原因，但也意味着忘了收工没人兜底。
#      sbatch 那条路可以不写 --time（有 idleguard 兜底，见 recommend-rules.md）；
#      交互式这条路没有第二道网，时限就是唯一那道。
#   2) 时限**只能往下调，不能往上调**：普通用户 scontrol update TimeLimit 调大
#      直接 "Access/permission denied"，调小成功。所以「先开短的、不够再延长」
#      这条路走不通，必须一开始就报够。
#   3) 到点**真掐**：实测 srun 收到 SIGTERM（退出码 143），报
#      "CANCELLED ... DUE TO TIME LIMIT"，Slurm 终态 TIMEOUT，不重排、不续跑。
# ⇒ 报少了修不了、报多了能随时 debug-end 释放 —— 风险不对称，所以默认往大了报。

DEBUG_DEFAULT_TIME = "6:00:00"   # 默认 6 小时。够长到不会在干活途中被掐，
                                 # 又短到不能拿调试会话当批处理使（正经长活该走 sbatch）。
DEBUG_WARN_FRACTION = 0.25       # 剩余不足 1/4 时开始催「去排下一个会话」。
                                 # 用比例不用固定值：时限不能延长，只能重开，
                                 # 而重开要重新排队 —— 会话越长，需要的提前量越大。


def _parse_alloc_id(text):
    """从 salloc 输出里取作业号。拿不到返回 None。"""
    m = re.search(r"Granted job allocation (\d+)", text or "")
    return m.group(1) if m else None


def _hms_to_seconds(s):
    """squeue 的 %L / %M / %l -> 秒。形如 '15:00' / '1:23:45' / '2-03:04:05'。
    拿不准返回 None（区别于 0）。"""
    if not s:
        return None
    s = s.strip()
    if s in ("UNLIMITED", "INVALID", "NOT_SET", "N/A", ""):
        return None
    days = 0
    if "-" in s:
        d, _, s = s.partition("-")
        try:
            days = int(d)
        except ValueError:
            return None
    try:
        nums = [int(x) for x in s.split(":")]
    except ValueError:
        return None
    while len(nums) < 3:
        nums.insert(0, 0)
    h, m, sec = nums[-3:]
    return days * 86400 + h * 3600 + m * 60 + sec


def _debug_session_info(cfg, jobid):
    """一个会话的现况：还活着吗、还剩多久。不在队列里返回 None。"""
    rc, out, _ = ssh_try(cfg, "squeue -h -j %s -o '%%T|%%L|%%M|%%C|%%m|%%j|%%l'"
                              % shlex.quote(str(jobid)))
    if rc != 0 or not out.strip():
        return None
    p = out.strip().splitlines()[0].split("|")
    if len(p) < 7:
        return None
    return {"state": p[0], "time_left": p[1], "elapsed": p[2],
            "cpus": p[3], "mem": p[4], "name": p[5], "time_limit": p[6],
            "time_left_seconds": _hms_to_seconds(p[1]),
            "time_limit_seconds": _hms_to_seconds(p[6])}


def _debug_time_warning(info):
    """剩余时间跌破 1/4 就催。拿不到时间返回 None（不瞎猜）。"""
    if not info:
        return None
    left = info.get("time_left_seconds")
    total = info.get("time_limit_seconds")
    if left is None or not total:
        return None
    if left > total * DEBUG_WARN_FRACTION:
        return None
    return ("⏰ 会话只剩 %s（时限 %s，已过 3/4）。时限**不能延长**，到点正在跑的活会被"
            "SIGTERM 掐死且不重排。要接着调就现在去排下一个会话 —— 机器忙时排队本身"
            "就要一阵子，这段提前量是留给排队的。"
            % (info.get("time_left"), info.get("time_limit")))


def _is_interactive(cfg, jobid):
    """真判据是 BatchFlag=0（Slurm 自己记的），不是作业名 —— 名字谁都能乱起。
    idleguard 判豁免用的也是这个字段。读不到返回 None（区别于 False）。"""
    rc, out, _ = ssh_try(cfg, "scontrol show job %s -o" % shlex.quote(str(jobid)))
    if rc != 0 or "BatchFlag=" not in out:
        return None
    return "BatchFlag=0" in out


def cmd_debug_start(cfg, time_str, cpus, mem_gb, gpu, name, wait_secs):
    time_str = time_str or DEBUG_DEFAULT_TIME
    name = name or ("debug-" + cfg["user"])
    parts = ["salloc", "--no-shell", "--immediate=%d" % int(wait_secs),
             "--job-name=%s" % shlex.quote(name),
             "--time=%s" % shlex.quote(time_str)]
    if cpus:
        parts.append("--cpus-per-task=%d" % int(cpus))
    if mem_gb:
        parts.append("--mem=%dG" % int(mem_gb))
    if gpu:
        parts.append("--gres=gpu:1")
    rc, out, err = ssh_try(cfg, " ".join(parts), timeout=int(wait_secs) + 25)
    blob = ((out or "") + "\n" + (err or "")).strip()
    jid = _parse_alloc_id(blob)
    if not jid:
        # 没拿到 = 机器现在腾不出这个规格。把现场一并返回，让调用方决定
        # 缩小 / 去 GPU / 等 —— 不在这里替用户选。
        return {"ok": False, "reason": "not_granted",
                "salloc_output": blob[:600],
                "server_now": cmd_status(cfg),
                "hint": "机器现在腾不出这个规格。可以：① 缩小 --cpus / --mem-gb "
                        "② 去掉 --gpu ③ 等正在跑的作业结束（server_now 里有还剩多久）"}
    info = _debug_session_info(cfg, jid) or {}
    return {"ok": True, "jobid": jid, "name": name,
            "requested": {"time": time_str, "cpus": cpus,
                          "mem_gb": mem_gb, "gpu": bool(gpu)},
            "session": info,
            "warn_at_seconds_left": int((info.get("time_limit_seconds") or 0)
                                        * DEBUG_WARN_FRACTION) or None,
            "run_with": "advise.py debug-run --jobid %s --command '...'" % jid,
            "end_with": "advise.py debug-end --jobid %s" % jid,
            "note": "会话到 --time 自动释放。别人在 squeue 里看到的是一个正常作业，"
                    "名字 '%s' —— 有名有姓有上限，不会被当成卡死的作业。" % name}


def cmd_debug_run(cfg, jobid, command, timeout):
    info = _debug_session_info(cfg, jobid)
    if not info:
        die("会话 %s 不在队列里 —— 已经到期或被取消了。"
            "用 `advise.py debug-list` 看还有哪些活着的会话。" % jobid)
    if info["state"] != "RUNNING":
        die("会话 %s 现在是 %s，还不能往里塞活。" % (jobid, info["state"]))
    # 用 login shell：非交互 shell 的 PATH 里没有 conda（probe-env 早就发现过这点），
    # 直接 srun 用户的命令会报 "conda: command not found"。
    remote = "srun --jobid=%s bash -lc %s" % (shlex.quote(str(jobid)),
                                              shlex.quote(command))
    t0 = time.time()
    rc, out, err = ssh_try(cfg, remote, timeout=timeout)
    elapsed = round(time.time() - t0, 2)
    after = _debug_session_info(cfg, jobid)
    result = {"jobid": str(jobid), "rc": rc, "wall_seconds": elapsed,
              "stdout": out, "stderr": err,
              "time_left": (after or {}).get("time_left"),
              "warn": _debug_time_warning(after)}
    if after is None:
        # 会话在这次运行期间没了 —— 最可能就是撞上了时限。说清楚，
        # 别让调用方把「被掐死」读成「程序自己失败了」。
        result["session_gone"] = True
        result["warn"] = ("⚠️ 会话在这次运行期间消失了。若 stderr 里有 "
                          "'DUE TO TIME LIMIT'，就是撞上时限被掐 —— 这次的活没跑完，"
                          "而且不会自动重跑。开个新会话重来。")
    return result


def cmd_debug_list(cfg):
    user = cfg["user"]
    rc, out, _ = ssh_try(cfg, "squeue -h -u %s -o '%%i|%%T|%%j|%%L|%%M|%%C|%%m|%%l'"
                              % shlex.quote(user))
    if rc != 0:
        die("读不到队列（ssh 或 squeue 失败）。")
    sessions, batch, unknown = [], [], []
    for ln in out.splitlines():
        p = ln.split("|")
        if len(p) < 8:
            continue
        rec = {"job": p[0], "state": p[1], "name": p[2], "time_left": p[3],
               "elapsed": p[4], "cpus": p[5], "mem": p[6], "time_limit": p[7]}
        flag = _is_interactive(cfg, p[0])
        if flag is True:
            rec["warn"] = _debug_time_warning({
                "time_left": p[3], "time_limit": p[7],
                "time_left_seconds": _hms_to_seconds(p[3]),
                "time_limit_seconds": _hms_to_seconds(p[7])})
            sessions.append(rec)
        elif flag is False:
            batch.append(rec)
        else:
            unknown.append(rec)   # 读不到 BatchFlag：不猜，单列一档
    return {"user": user, "debug_sessions": sessions, "batch_jobs": batch,
            "unknown": unknown,
            "note": "debug_sessions = 交互式分配（BatchFlag=0）。idleguard 豁免它们，"
                    "只有 --time 会收 —— 所以别忘了 debug-end。"}


def cmd_debug_end(cfg, jobid):
    before = _debug_session_info(cfg, jobid)
    if not before:
        return {"jobid": str(jobid), "released": True, "already_gone": True,
                "note": "它已经不在队列里了（到期或早就取消过）。"}
    ssh_try(cfg, "scancel %s" % shlex.quote(str(jobid)))
    # 确认真的退干净了 —— 只发一条 scancel 就报「释放了」是假报告。
    released = False
    for _ in range(8):
        time.sleep(1.0)
        if _debug_session_info(cfg, jobid) is None:
            released = True
            break
    return {"jobid": str(jobid), "released": released, "was": before,
            "note": None if released else
                    "scancel 已发出，但队列里还看得到 —— 再跑一次 debug-list 确认。"}


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

    # 盯住一个作业，出事就返回。给 AI agent 用：正常跑的时候一声不吭、不花 token，
    # 作业结束/失败/**被冻结**/被取消时立刻返回，冻结于是变成一个会主动送达的事件。
    w = sub.add_parser("wait")
    w.add_argument("jobid")
    w.add_argument("--interval", type=float, default=8.0,
                   help="轮询间隔秒数（默认 8）")
    w.add_argument("--max-wait", type=float, default=0,
                   help="监控自身的最长存活秒数，0 = 不限。超时返回 20，"
                        "含义是「监控放弃了」而不是「作业超时了」")

    # 干净地取消一个被冻结的作业：解冻 → 取消 → 确认真的退干净了。
    # 直接 scancel 一个冻结的作业会让它永久卡住且不还内存 —— 这条命令的全部意义
    # 就是让调用者不必知道有这个坑。
    gp = sub.add_parser("gpu-peak")
    gp.add_argument("jobid")

    cf = sub.add_parser("cancel-frozen")
    cf.add_argument("jobid")
    cf.add_argument("--yes", action="store_true",
                    help="确认取消。不带此参数只做检查并报告将要做什么")

    sub.add_parser("probe-env")

    # 交互式调试会话。默认 6 小时，理由见 cmd_debug_start 上面那段注释。
    ds = sub.add_parser("debug-start")
    ds.add_argument("--time", default=None,
                    help="会话时长，默认 6:00:00。时限不能事后延长，往大了报")
    ds.add_argument("--cpus", type=int, default=None)
    ds.add_argument("--mem-gb", type=int, default=None)
    ds.add_argument("--gpu", action="store_true", help="会话要占 GPU")
    ds.add_argument("--name", default=None,
                    help="squeue 里显示的名字，默认 debug-<user>")
    ds.add_argument("--wait", type=int, default=20,
                    help="等不到资源就放弃的秒数（默认 20），避免调用方被吊住")

    dr = sub.add_parser("debug-run")
    dr.add_argument("--jobid", required=True)
    dr.add_argument("--command", required=True)
    dr.add_argument("--timeout", type=int, default=900)

    sub.add_parser("debug-list")

    de = sub.add_parser("debug-end")
    de.add_argument("--jobid", required=True)

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
    elif args.cmd == "wait":
        # 退出码是这条命令的主要产物 —— 调用方（含 AI agent）靠它区分
        # 「跑完了」「挂了」「被冻了」，不必解析文本。
        rc, payload = cmd_wait(cfg, args.jobid, args.interval, args.max_wait)
        emit(payload)
        sys.exit(rc)
    elif args.cmd == "gpu-peak":
        emit(cmd_gpu_peak(cfg, args.jobid))
    elif args.cmd == "cancel-frozen":
        rc, payload = cmd_cancel_frozen(cfg, args.jobid, args.yes)
        emit(payload)
        sys.exit(rc)
    elif args.cmd == "probe-env":
        emit(cmd_probe_env(cfg))
    elif args.cmd == "debug-start":
        emit(cmd_debug_start(cfg, args.time, args.cpus, args.mem_gb,
                             args.gpu, args.name, args.wait))
    elif args.cmd == "debug-run":
        emit(cmd_debug_run(cfg, args.jobid, args.command, args.timeout))
    elif args.cmd == "debug-list":
        emit(cmd_debug_list(cfg))
    elif args.cmd == "debug-end":
        emit(cmd_debug_end(cfg, args.jobid))


if __name__ == "__main__":
    main()
