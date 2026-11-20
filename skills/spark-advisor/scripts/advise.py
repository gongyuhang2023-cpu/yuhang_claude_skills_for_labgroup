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
  rules             the server's own rules + shared software/databases + live free
                    resources (the panel's /ai.md) -- the single source of truth
  software find     look up a recipe or find ARM64 packages for a project environment
  software scan     find toolbox calls in scripts on Spark
  software recipes  export the recipe catalog as JSON
  env ...           project environments: init/build/adopt/status/check via env-store
  database ...      shared databases: find (every version, where it lives, how it was
                    installed, cache state); plan/install/status (self-install into the
                    NAS public library via the server's db-install, dry run first);
                    scan/check/lock/pin (the project's databases.lock.json, see
                    db_project.py); tidy (every database-like directory in the user's home,
                    via the server's spark-cleanup dbscan); adopt/compare (a personal copy).
  cleanup scan/     relay to the server command `spark-cleanup` (runs as the user, dry
  refs/apply        by default): personal envs / database copies that duplicate the
                    shared ones, with tier, references, in-use checks and real space

Facts vs judgment: this script produces FACTS + a rule-based baseline. The final
recommendation phrasing / confidence framing is the AI's job -- see
references/recommend-rules.md.

Config: reads config.json (next to the skill root). Don't hand-write it: on first run,
`advise.py detect-server`, then `advise.py init` (SKILL.md「首次配置」).
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
import urllib.error
import urllib.parse
import urllib.request
from http.client import IncompleteRead

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


def _soft(fn, *args):
    """调用一个可能 die() 的查询（如 cmd_status）：失败返回 None，而不是退出整个进程。

    die() 会先往 stdout 打一段错误 JSON 再 sys.exit —— 光接住 SystemExit 不够，
    那段 JSON 已经打出去了，调用方会收到两段。所以连 stdout 一起截住。
    """
    import contextlib
    import io
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args)
    except SystemExit:
        return None


def local_path(p):
    """本机文件路径（--out / --script）。

    Git Bash 里为了保住服务器路径要带 MSYS_NO_PATHCONV=1，可这样一来本机路径也不转换了：
    /c/Users/... 会被 Windows 版 Python 当成 C:\\c\\Users\\...，/tmp/x 当成 C:\\tmp\\x。
    """
    if os.name != "nt" or not p or not p.startswith("/"):
        return p
    m = re.match(r"^/([a-zA-Z])(/.*)?$", p)
    if m:
        return m.group(1).upper() + ":" + (m.group(2) or "/")
    try:
        r = subprocess.run(["cygpath", "-w", p], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return p


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
                    die(f"config.json is missing '{key}'. Re-create it: advise.py detect-server, "
                        "then advise.py init (SKILL.md「首次配置」).")
            return cfg
    die("config.json not found -- first-time setup: run `advise.py detect-server`, ask the user "
        "for their server username, then `advise.py init --user <name> --host <IP> --node <name>` "
        "(SKILL.md「首次配置」). Don't hand-write config.json.")


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
    cmd = [cfg.get("ssh_exe") or "ssh"] + list(cfg.get("ssh_opts", ["-o", "ConnectTimeout=12", "-o", "BatchMode=yes"]))
    if cfg.get("ssh_key"):
        cmd += ["-i", cfg["ssh_key"]]
    # Close unused stdin explicitly: nested Win32-OpenSSH can otherwise hang after the remote command exits.
    if stdin_data is None:
        cmd += ["-n"]
    cmd += [target, remote_cmd]
    # 走字节 I/O，不用 text=True：Windows 文本模式会把 stdin 里的 \n 翻成 \r\n，
    # sbatch 见到 CRLF 脚本会拒收（"Batch script contains DOS line breaks"）。
    payload = stdin_data.encode("utf-8") if stdin_data is not None else None
    try:
        r = subprocess.run(cmd, capture_output=True,
                           timeout=timeout, input=payload,
                           stdin=subprocess.DEVNULL if payload is None else None)
    except FileNotFoundError:
        die("`ssh` not found on PATH. Install an OpenSSH client.")
    except subprocess.TimeoutExpired:
        die(f"SSH timed out talking to {target}.")
    stdout = r.stdout.decode("utf-8", "replace")
    stderr = r.stderr.decode("utf-8", "replace")
    if r.returncode != 0 and not stdout.strip():
        die(f"SSH command failed ({r.returncode}) on {target}: {stderr.strip()[:300]}")
    return stdout


def ssh_try(cfg, remote_cmd, timeout=30, stdin_data=None):
    """ssh_run 的不致命版本：返回 (rc, stdout, stderr)，绝不 die()。

    ssh_run 在命令失败时直接退出整个进程 —— 对 status/recommend 这种一次性查询没问题，
    但 wait/cancel-frozen 必须自己区分「命令失败」与「查到的结果就是空」，
    并把失败映射成一个明确的错误码返回给调用者（含 AI agent）。
    把两者混成一个结果，正是这次事故里看门狗犯的同一个错误。
    """
    target = f'{cfg["user"]}@{resolve_host(cfg)}'
    cmd = [cfg.get("ssh_exe") or "ssh"] + list(cfg.get("ssh_opts", ["-o", "ConnectTimeout=12", "-o", "BatchMode=yes"]))
    if cfg.get("ssh_key"):
        cmd += ["-i", cfg["ssh_key"]]
    # Close unused stdin explicitly: nested Win32-OpenSSH can otherwise hang after the remote command exits.
    if stdin_data is None:
        cmd += ["-n"]
    cmd += [target, remote_cmd]
    # 走字节 I/O，理由同 ssh_run：Windows 文本模式会把 \n 翻成 \r\n。
    payload = stdin_data.encode("utf-8") if stdin_data is not None else None
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, input=payload,
                           stdin=subprocess.DEVNULL if payload is None else None)
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
WAIT_NO_SUCH_JOB = 22   # 没有这个作业号（squeue 说 Invalid job id，sacct 也没有记录）—— 多半是号写错了
WAIT_SSH_FAIL    = 255  # 连不上服务器

# squeue / sacct 的报错不再丢进 /dev/null：「没有这个作业」和「查询失败」要分得开。
# squeue 的状态行加 ST: 前缀，好和报错行区分；sacct 的退出码单独回传（SA_RC）。
_WAIT_PROBE = r"""
echo "SQ_BEGIN"
squeue -h -j {jid} -o 'ST:%T' 2>&1
echo "FZ_BEGIN"
cat /run/spark-usage/frozen-jobs.json 2>/dev/null || echo '{{}}'; echo
echo "CGFZ_BEGIN"
CG=$(ls -d /sys/fs/cgroup/system.slice/*slurmstepd.scope/job_{jid} 2>/dev/null | head -1)
[ -n "$CG" ] && cat "$CG/cgroup.freeze" 2>/dev/null
echo "SA_BEGIN"
sacct -X -n -P -j {jid} -o State,ExitCode 2>&1
echo "SA_RC=$?"
"""

# sacct 有记录但还不是终态（squeue 一时没查到、或作业正在收尾）—— 不能当终态返回
_NONTERMINAL = ("RUNNING", "PENDING", "REQUEUED", "RESIZING", "SUSPENDED",
                "COMPLETING", "CONFIGURING", "STAGE_OUT")


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
    first = True            # 只有第一次查询就查无此号，才判「没有这个作业」
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
        sq_lines = [ln.strip() for ln in sq.splitlines() if ln.strip()]
        state = next((ln[3:].strip() for ln in sq_lines if ln.startswith("ST:")), "")
        sq_err = " ".join(ln for ln in sq_lines if not ln.startswith("ST:"))
        no_such = "Invalid job id" in sq_err
        sa_lines = [ln.strip() for ln in sa.splitlines() if ln.strip()]
        sa_rc = next((ln[6:] for ln in sa_lines if ln.startswith("SA_RC=")), "")
        if sa_rc != "0":
            sa_err = " ".join(ln for ln in sa_lines if "|" not in ln and not ln.startswith("SA_RC="))
            return WAIT_QUERY_FAIL, {"job": jid, "result": "query-failed",
                                     "error": (sa_err or "sacct 退出码 %s" % (sa_rc or "未知"))[:200],
                                     "note": "sacct 查询失败，作业状态未知 —— 不要当成已完成"}
        if sq_err and not no_such:
            # squeue 自己出错（比如连不上 slurmctld）时 state 是空的，不处理就会被当成
            # 「作业已离开队列」，再拿 sacct 里的 RUNNING 当终态 —— 状态未知就说未知。
            return WAIT_QUERY_FAIL, {"job": jid, "result": "query-failed",
                                     "error": sq_err[:200],
                                     "note": "squeue 查询失败，作业状态未知 —— 不要当成已完成"}

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
            first = False
            gone_since = None
            time.sleep(interval)
            continue

        # ---- 不在 squeue 了 → 等 sacct 给终态，别急着说"完成了" ----
        if gone_since is None:
            gone_since = time.time()
        final = _parse_sacct(sa)
        if final is not None and final[0].split()[0] in _NONTERMINAL:
            final = None             # sacct 说还在跑/在收尾：不是终态，接着等
        if final is None:
            if first and no_such:
                # 第一次查就两边都没有：不用干等 60 秒再报「状态未知」
                return WAIT_NO_SUCH_JOB, {
                    "job": jid, "result": "no-such-job",
                    "note": "squeue 和 sacct 都查不到这个作业号 —— 多半是号写错了。"
                            "用 squeue -u $USER 或 advise.py history 核对作业号"}
            first = False
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
echo "SA_BEGIN"; sacct -X -n -P -j "$JID" -o State,User 2>/dev/null
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
    cg, fz, sq, owner, aud, sudo_ok, sa = _split_sections(
        out, ["CG_PATH_BEGIN", "FREEZE_BEGIN", "SQ_BEGIN", "OWNER_BEGIN",
              "AUDIT_BEGIN", "SUDO_BEGIN", "SA_BEGIN"])
    cg, fz = cg.strip(), fz.strip()
    state, owner = sq.strip(), owner.strip()
    sa_row = next((ln.split("|") for ln in sa.strip().splitlines() if "|" in ln), None)
    owner = owner or (sa_row[1].strip() if sa_row and len(sa_row) > 1 else "")
    # 不是自己的作业要说出来：取消别人的作业之前必须先和对方确认
    whose = ("" if not owner or owner == cfg.get("user")
             else "这是 %s 的作业，动它之前先和对方确认。" % owner)

    # 31 =「这条命令不适用」，按 note 做。别笼统地说「scancel 即可」：
    # 对没有这个作业、已经结束、还在排队、或找不到 cgroup 的作业，那句话都会误导。
    if not cg:
        if not state and not sa_row:
            return CF_NOT_FROZEN, {
                "job": jid, "result": "no-such-job",
                "note": "squeue 和 sacct 都查不到这个作业号 —— 多半是号写错了，不需要取消。"}
        if not state:
            return CF_NOT_FROZEN, {
                "job": jid, "result": "already-ended", "owner": owner,
                "slurm_state": sa_row[0].strip(),
                "note": "作业已经结束（sacct：%s），不需要取消。" % sa_row[0].strip()}
        if state == "PENDING":
            return CF_NOT_FROZEN, {
                "job": jid, "result": "pending", "owner": owner, "slurm_state": state,
                "note": "作业还在排队，没有被冻结，不需要这条命令。确认要取消、且得到"
                        "作业主人同意后，排队中的作业可以直接 scancel %s。%s" % (jid, whose)}
        return CF_NOT_FROZEN, {
            "job": jid, "result": "no-cgroup", "owner": owner, "slurm_state": state,
            "note": "作业状态是 %s，但找不到它的 cgroup（可能刚启动或正在收尾），判断不了"
                    "是否冻结。过一会儿再跑一次本命令，**先别 scancel**。%s" % (state, whose)}
    if fz != "1":
        return CF_NOT_FROZEN, {
            "job": jid, "result": "not-frozen", "owner": owner, "cgroup": cg,
            "slurm_state": state,
            "note": ("该作业**没有被冻结**（cgroup.freeze=%s），不需要这条命令。"
                     "确认要取消、且得到作业主人同意后，可以直接 scancel %s。%s"
                     % (fz or "读不到", jid, whose))}

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

PANEL_PORT = 3001


def panel_url(cfg, path=""):
    """面板地址。用 config 里的 `host`（Tailscale 地址），不用 resolve_host()：LAN 地址可能是 IPv6
    （拼进 URL 要加方括号，不加就必定连不上），而且面板按来源的 Tailscale 账号认人，
    走 LAN 时面板可能识别不了你的账号。环境变量 SPARK_PANEL_URL 只给测试用。"""
    base = os.environ.get("SPARK_PANEL_URL") or cfg.get("panel_url")
    if not base:
        h = cfg["host"]
        base = "http://%s:%d" % ("[%s]" % h if ":" in h else h, int(cfg.get("panel_port", PANEL_PORT)))
    return base.rstrip("/") + path


def cmd_rules(cfg):
    """服务器规则 + 共享软件/数据库 + 实时空闲资源 —— 管理员维护的唯一权威来源。

    内容就是服务器面板的 /ai.md（源文件 /opt/bio/AI_GUIDE.md，管理员改动后立即生效），
    所以本 skill 不再自带一份会过时的服务器事实。直接输出 Markdown（不是 JSON）：
    它是给模型读的说明文字。面板连不上时退回 ssh 读同一份文件 + spark-status。
    """
    url = cfg.get("rules_url") or panel_url(cfg, "/ai.md")
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            text = r.read().decode("utf-8")
        print(f"<!-- source: {url} -->")
    except Exception as e:
        text = ssh_run(cfg, "cat /opt/bio/AI_GUIDE.md; echo; echo '---'; spark-status", timeout=40)
        print(f"<!-- {url} unreachable ({e}); read over ssh -->")
    print(text)
    return 0


# ----------------------------- software: 公共工具与项目环境 -----------------------------


def _http_json(url, body=None, timeout=20):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:          # 面板的拒绝（比如设备没登记）也是 JSON，原样带回
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}


def _rank(q, names, cmds):
    """同面板的排序：名称完全一致 0 > 命令完全一致 1 > 名称开头 2 > 命令开头 3 > 名称包含 4 > 命令包含 5。"""
    best = 9
    for k, words in ((0, names), (1, cmds)):
        for w in words:
            v = str(w or "").lower()
            if v == q:
                best = min(best, k)
            elif v.startswith(q):
                best = min(best, k + 2)
            elif q in v:
                best = min(best, k + 4)
    return best


def _vkey(v):
    """数字段和字母段混在一起也能比（0.5.0 对 0.5.0a 不会抛 TypeError）；字母段排在数字段前面。"""
    return [(1, int(x), "") if x.isdigit() else (0, 0, x) for x in re.split(r"[.\-_]", str(v or ""))]


CONDA_CHANNELS = ("bioconda", "conda-forge")
SMOKE_TEST_STEP = ("把示例 tasks.smoke 换成真正检查工具的短命令，例如 <工具> --version。已有真实测试时沿用。"
                   "再在 Spark 项目目录运行 /usr/local/bin/pixi lock 更新锁。")


def _package_index_json(url):
    """Retry one transport failure, including a timeout while reading the response."""
    for attempt in range(2):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError:
            raise
        except (OSError, IncompleteRead):
            if attempt:
                raise


def _conda_arch(name):
    """查各渠道的 linux-aarch64 和 noarch 构建。iPHoP 等 noarch 包仍需用 ARM64 的锁验证依赖。"""
    out = {}
    for ch in CONDA_CHANNELS:
        url = f"https://api.anaconda.org/package/{ch}/{urllib.parse.quote(name, safe='')}"
        try:
            d = _package_index_json(url)
            files = d.get("files")
            if not isinstance(files, list):
                raise ValueError("Conda 响应缺少有效的构建清单")
            best = {}
            versions = [v for v in d.get("versions") or [] if isinstance(v, str) and v]
            if d.get("latest_version"):
                versions.append(d["latest_version"])
            for f in files:
                sub = (f.get("attrs") or {}).get("subdir")
                v = f.get("version")
                if not v:
                    continue
                versions.append(v)
                if sub in ("linux-aarch64", "noarch") and (sub not in best or _vkey(v) > _vkey(best[sub])):
                    best[sub] = v
            out[ch] = {"exists": True, "latest": max(versions, key=_vkey, default=None),
                       "linux-aarch64": best.get("linux-aarch64"), "noarch": best.get("noarch")}
        except urllib.error.HTTPError as e:
            out[ch] = {"exists": False} if e.code == 404 else {"error": f"HTTP {e.code}"}
        except (OSError, IncompleteRead, AttributeError, TypeError, ValueError) as e:
            out[ch] = {"error": str(e)[:120] or type(e).__name__}
    return out


def _pypi_arch(name):
    """Find a non-yanked Python 3 wheel for Linux ARM64 or a pure Python wheel."""
    url = f"https://pypi.org/pypi/{urllib.parse.quote(name, safe='')}/json"
    try:
        data = _package_index_json(url)
        releases = dict(data.get("releases") or {})
        info = data.get("info") or {}
        latest = info.get("version")
        result = {"exists": True, "wheel": None, "summary": info.get("summary") or None,
                  "home_page": info.get("home_page") or (info.get("project_urls") or {}).get("Homepage"),
                  "project_url": info.get("project_url") or f"https://pypi.org/project/{urllib.parse.quote(name, safe='')}/"}
        if latest and data.get("urls"):
            releases[latest] = data["urls"]
        matches = []
        for version, files in releases.items():
            for f in files:
                filename = f.get("filename", "")
                if f.get("packagetype") != "bdist_wheel" or f.get("yanked") or not filename.endswith(".whl"):
                    continue
                tags = filename[:-4].rsplit("-", 3)
                if len(tags) != 4:
                    continue
                _, python, abi, platform = tags
                if not any(re.fullmatch(r"(?:py|cp|pp)3[0-9]*", tag) for tag in python.split(".")):
                    continue
                pure = platform == "any" and abi == "none"
                arm = all(re.fullmatch(r"linux_aarch64|manylinux(?:[0-9]+|_[0-9]+_[0-9]+)_aarch64", tag)
                          for tag in platform.split("."))
                if pure or arm:
                    matches.append((version == latest, f.get("upload_time_iso_8601") or "", filename, version))
        if matches:
            _, _, wheel, version = max(matches)
            result.update(wheel=wheel, version=version)
        return result
    except urllib.error.HTTPError as e:
        return {"exists": False} if e.code == 404 else {"error": f"HTTP {e.code}"}
    except (OSError, IncompleteRead, AttributeError, TypeError, ValueError) as e:
        return {"error": str(e)[:120] or type(e).__name__}


def _on_server(cfg):
    host = socket.gethostname().split(".")[0]
    return host == cfg.get("node") or os.path.isdir("/opt/bio/sbin")


class SoftwarePanelError(ValueError):
    pass


def software_inventory(cfg):
    try:
        response = _http_json(panel_url(cfg, "/api/software"))
    except (OSError, IncompleteRead) as e:
        raise SoftwarePanelError(f"面板连不上，无法读取软件清单 ({type(e).__name__})。") from e
    except ValueError as e:
        raise SoftwarePanelError("面板的软件清单响应无法解析。") from e
    inv = response.get("inventory") if isinstance(response, dict) else None
    if not isinstance(inv, dict) or (inv.get("error") and "recipes" not in inv):
        raise SoftwarePanelError("面板已响应，但未提供可用的软件清单。")
    return response, inv


def cmd_software_on_spark(cfg, paths=()):
    import software_scan

    _, inv = software_inventory(cfg)
    catalog = software_scan.recipe_catalog(inv.get("recipes"))
    paths = [server_path(path) for path in paths]
    if _on_server(cfg):
        return software_scan.spark_operation(paths, catalog)
    script = _bundle("software_scan.py", "run_record")
    script += "\n_rc, _output = spark_operation(_sys.argv[1:], %r)\nprint(json.dumps(_output, ensure_ascii=True))\n_sys.exit(_rc)\n" % catalog
    remote = " ".join(["python3", "-I", "-", *(shlex.quote(path) for path in paths)])
    try:
        rc, out, _ = ssh_try(cfg, remote, timeout=180, stdin_data=script)
    except (OSError, subprocess.SubprocessError):
        return 1, {"error": "无法连接 Spark 或读取操作超时。"}
    if rc == 255:
        return rc, {"error": "无法连接 Spark 执行软件扫描或导出。"}
    try:
        return rc, json.loads(out)
    except ValueError:
        return rc or 1, {"error": "Spark 未返回软件目录或扫描结果，请检查连接和路径后重试。"}


SOFTWARE_RULES_STEP = "先运行 rules 查这个工具是否有专门安排。有就按服务器规则处理。没有专门安排时，再继续下面的步骤。"
RECIPE_RETRY = "先重查 software recipes 并核对服务器规则。确认所需版本、频道和全部依赖后再写项目锁。"


def project_tool_steps(add_steps):
    return [
        SOFTWARE_RULES_STEP,
        "对已存在的 Spark 项目目录运行 env init --dir <项目目录>，预览配方。",
        "缺 pixi.toml 时，确认后运行 env init --dir <项目目录> --yes，写出配方并生成初始锁。",
        *add_steps,
        SMOKE_TEST_STEP,
        "运行 env build --dir <项目目录> 看构建计划，确认后加 --yes --wait 构建。",
        "环境就绪后，用 gen-sbatch --name <作业名> --mem-gb <GB> --workdir <Spark 项目绝对路径> "
        "--env <项目目录> --command '<命令>' 生成脚本。",
    ]


def recipe_lock_steps(recipe):
    steps = []
    channels = " ".join(shlex.quote(ch) for ch in recipe["channels"])
    first, *remaining = recipe["channels"] or [None]
    if first and first.startswith("file://"):
        steps.append("在 Spark 项目目录让本地频道排在配方其余频道之前。若尚未添加，运行 "
                     f"`/usr/local/bin/pixi workspace channel add --no-install --prepend {shlex.quote(first)}`。"
                     f"其余频道按配方顺序保留或补齐：{' '.join(remaining)}。"
                     "本地频道已存在时，在 pixi.toml 调整频道顺序后再解析依赖。")
    elif channels:
        steps.append("在 Spark 项目目录核对配方所列频道，先补齐缺少的频道。按配方顺序选入缺少的频道："
                     f"`/usr/local/bin/pixi workspace channel add --no-install {channels}`。"
                     "若已有频道的优先级与配方不同，先按配方顺序调整 pixi.toml 的频道列表，再解析依赖。")
    specs = []
    for name, dep in recipe["dependencies"].items():
        version, channel = (dep["version"], dep.get("channel")) if isinstance(dep, dict) else (dep, None)
        separator = "" if version.startswith(("=", "<", ">", "!", "~")) else "="
        spec = (channel + "::" if channel else "") + name + separator + version
        quoted = shlex.quote(spec)
        specs.append("'" + spec + "'" if channel and quoted == spec else quoted)
    steps.append("在 Spark 项目目录运行 `/usr/local/bin/pixi add --no-install " + " ".join(specs) + "`，写入全部约束并更新锁。")
    if recipe["db"]:
        steps.append(f"用 database find {shlex.quote(recipe['db'])} 核对库版本，在 databases.lock.json 里锁定所选版本。")
    return steps


def cmd_software_find(cfg, name):
    import software_scan

    q = name.strip().lower()
    if not q:
        return {"error": "名字不能为空"}
    if software_scan.SERVER_TOOLS in q:
        return {"error": "请输入要锁进项目环境的课题工具名。"}
    public_r = {"r": "R", "rscript": "Rscript", "/opt/bio/bin/r": "R", "/opt/bio/bin/rscript": "Rscript"}
    if q in public_r:
        return {"query": name, "tools": [{"package": "r", "env": "r", "commands": ["R", "Rscript"], "n_commands": 2}],
                "usage": f"使用公共 R，直接运行 /opt/bio/bin/{public_r[q]}。"}
    try:
        d, inv = software_inventory(cfg)
        catalog = software_scan.recipe_catalog(inv.get("recipes"))
    except (SoftwarePanelError, software_scan.CatalogError) as e:
        return {"query": name, "error": str(e), "next": [RECIPE_RETRY]}
    issues = {"recipe_errors": catalog["recipe_errors"]} if "recipe_errors" in catalog else {}
    matches = []
    for recipe in catalog["recipes"]:
        r = _rank(q, [recipe["tool"], recipe["env"]], recipe["commands"] + recipe["compat"])
        if r <= 1:
            matches.append((r, recipe["tool"], recipe))
    page = panel_url(cfg, "/software")
    if matches:
        ranked = [recipe for _, _, recipe in sorted(matches, key=lambda m: m[:2])]
        return {"query": name, "me": d.get("me"), "page": page, "recipes": ranked,
                "environment_tip": "合并工具前核对各配方的 dependencies、channels 和 notes。版本或频道优先级冲突时，按 references/project-envs.md 拆具名环境。",
                "next": [dict(tool=recipe["tool"], steps=project_tool_steps(recipe_lock_steps(recipe)))
                         for recipe in ranked], **issues}
    if any(q == issue["tool"].lower() for issue in issues.get("recipe_errors", ())):
        return {"query": name, "error": "所查工具的推荐配方有误。", "next": [RECIPE_RETRY], **issues}

    tools = []
    for t in inv.get("tools", []):
        if software_scan.SERVER_TOOLS in json.dumps(t, ensure_ascii=False):
            continue
        r = _rank(q, [t.get("package"), t.get("env")], t.get("exposed") or [])
        if r < 9:
            cmds = sorted(t.get("exposed") or [], key=lambda c: _rank(q, [], [c]))
            tools.append((r, t.get("package") or "", {
                "package": t.get("package"), "version": t.get("version"), "env": t.get("env"),
                "commands": cmds[:10], "n_commands": len(t.get("exposed") or [])}))
    out = {"query": name, "me": d.get("me"),
           "tools": [x[2] for x in sorted(tools, key=lambda x: (x[0], x[1]))][:10],
           "page": page, **issues}
    if out["tools"]:
        out["usage"] = "工具清单可用于核对名称。按构建查询结果把课题工具锁进项目环境。"
    n_db = sum(1 for x in inv.get("dbs", []) if _db_rank(q, x)[0] < 99)
    if n_db:
        out["dbs_hint"] = "公共区还有和「%s」相关的数据库（%d 个版本）：查库用 database find %s" % (name.strip(), n_db, name.strip())
    exact_packages = {entry["package"] for rank, _, entry in tools if rank <= 1 and entry["package"]}
    package = next(iter(exact_packages)) if len(exact_packages) == 1 else name.strip()
    arch = out["arch"] = _conda_arch(package)
    pkg = shlex.quote(package.lower())
    failed = [ch for ch in CONDA_CHANNELS if "exists" not in arch.get(ch, {})]
    add_step = "在 Spark 项目目录用 /usr/local/bin/pixi add --no-install <包名> 更新配方和锁。"
    if any(r.get("linux-aarch64") for r in arch.values()):
        out["availability"] = "Conda 有 linux-aarch64 构建。"
        add_step = f"在 Spark 项目目录用 /usr/local/bin/pixi add --no-install {pkg} 更新配方和锁。"
    elif any(r.get("noarch") for r in arch.values()):
        out["availability"] = ("Conda 找到 noarch 构建。包本身适用于 ARM64，"
                               "依赖可能缺少 ARM64 构建，以 /usr/local/bin/pixi lock 能否解析成功为准。")
        add_step = (f"在 Spark 项目目录用 /usr/local/bin/pixi add --no-install {pkg} 更新配方和锁，以 ARM64 锁能否解析成功为准。"
                    "若因缺少 ARM64 构建而解析失败，把缺少的依赖告诉用户。")
    elif failed:
        out["availability"] = f"Conda 渠道查询失败（{'、'.join(failed)}），可用构建未知。"
        out["next"] = [f"software find {pkg} --arch",
                       "重查仍失败时，到失败渠道的 Anaconda 项目页核对包名和构建。"
                       "仍无法确认时，告诉用户哪些结果未知，等确认后再选依赖。"]
    else:
        pypi = out["pypi"] = _pypi_arch(package)
        if pypi.get("wheel"):
            out["availability"] = ("PyPI 有同名项目提供 Linux ARM64 或纯 Python wheel。"
                                   "先核对简介和主页或项目链接，确认是用户要的工具，再核对 wheel 支持的 Python 版本。")
            dep = shlex.quote(package.lower() + "==" + pypi["version"])
            add_step = ("先核对 PyPI 简介和主页或项目链接，确认是用户要的工具且 wheel 支持项目的 Python 版本，"
                        f"再在 Spark 项目目录用 /usr/local/bin/pixi add --no-install --pypi {dep} 更新配方和锁。")
        elif pypi.get("error"):
            out["availability"] = "PyPI 查询失败，暂时无法确认是否有可用 wheel。"
        else:
            out["availability"] = "Conda 未找到 ARM64 或 noarch 构建，PyPI 未找到 Linux ARM64 或纯 Python wheel。"
    if "next" not in out:
        out["next"] = project_tool_steps([add_step])
        out["env_note"] = "env-store 只接受可锁定的 Conda 包和 PyPI wheel，拒收 sdist 和 Git 依赖。"
    else:
        out["next"].insert(0, SOFTWARE_RULES_STEP)
    return out


def _db_rank(q, x):
    """-> (排序键, 命中的别名)。库名命中排最前，其次别名（搜 UHGV 能找到包含它的 virodb），只命中版本号的排最后。"""
    aliases = [v.strip() for v in (x.get("aliases") or "").split(",") if v.strip()]
    nr = _rank(q, [x.get("name")], [])
    ar = sorted(((_rank(q, [v], []), v) for v in aliases), key=lambda z: (z[0], z[1]))
    matched = ar[0][1] if ar and ar[0][0] < 9 else None
    vr = _rank(q, [], [x.get("version")])
    r = nr if nr < 9 else (10 + ar[0][0] if matched else (20 + vr if vr < 9 else 99))
    return r, (matched if nr >= 9 else None)


# ----------------------------- cleanup: 清理和公共区重复的个人安装 -----------------------------
# 逻辑都在服务器命令 spark-cleanup 里（以用户身份运行，默认只看不动）；这里只转一手、原样返回 JSON。

def server_path(p):
    """Windows 上的 Git Bash（MSYS）会把传给 Windows 程序的 /home/... 参数悄悄改成
    C:/Program Files/Git/home/...，服务器就收到一个不存在的路径。服务器上的路径不可能长成
    Windows 路径，认出来就还原。也可以直接传带引号的 '~/…'，它不会被改写。"""
    q = p.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", q):
        m = re.search(r"/(home|opt|mnt|tmp|data|scratch)/", q)
        if m:
            return q[m.start():]
    return p


def cmd_cleanup(cfg, argv, timeout):
    remote = "spark-cleanup " + " ".join(shlex.quote(a) for a in argv) + " --json"
    rc, out, err = ssh_try(cfg, remote, timeout=timeout)
    if rc == 255 and not out.strip():
        return 255, {"error": "ssh 没成功：" + err.strip()[-300:],
                     "hint": "apply 在服务器上是忽略断线继续跑的；先 cleanup scan 看看它是不是已经做完了"}
    if rc == 127 or "command not found" in err:
        return 4, {"error": "服务器上还没有 spark-cleanup（需要管理员部署）"}
    if not out.strip():      # 服务器那边直接退出（比如另一个 spark-cleanup 正在执行），原因在 stderr 里
        return (rc or 1), {"error": (err.strip() or "spark-cleanup 没有输出")[-400:]}
    try:
        payload = json.loads(out.strip())
    except ValueError:
        return 1, {"error": "spark-cleanup 的输出不是 JSON", "stdout": out.strip()[-400:], "stderr": err.strip()[-400:]}
    return rc, payload


# 版本差距在本机算（scan 结果里已有每个同名包的 version / shared_version），不用改服务器
_GAP_ORDER = {"same": 0, "patch": 1, "minor": 2, "major": 3, "unknown": 4}


def _vparts(v):
    """'2.13.0' -> [2, 13, 0]；带字母的段（'10.941cd33'）只取开头的数字，后面不再看。"""
    parts = []
    for seg in re.split(r"[.\-_+]", v or ""):
        m = re.match(r"\d+", seg)
        if not m:
            break
        parts.append(int(m.group()))
    return parts


def version_gap(mine, public):
    """-> (gap, direction)。gap 看第一个不同的数字段：major / minor / patch；对不上的一律 unknown。
    direction：newer = 公共版更新；older = 公共版更旧（换过去等于降级）。判断规则在 references/cleanup.md。"""
    if not mine or not public:
        return "unknown", "unknown"
    if mine == public:
        return "same", "same"
    a, b = _vparts(mine), _vparts(public)
    if not a or not b:                   # 读不出数字（不是版本号的写法）
        return "unknown", "unknown"
    n = max(len(a), len(b))
    a, b = a + [0] * (n - len(a)), b + [0] * (n - len(b))
    if a == b:                           # 数字一样、字符串不同（构建号 / 后缀）：判断不了
        return "unknown", "unknown"
    i = next(k for k in range(n) if a[k] != b[k])
    return ("major" if i == 0 else "minor" if i == 1 else "patch"), ("newer" if b > a else "older")


def annotate_gaps(payload):
    """给 scan 结果里每个同名包补 gap / direction；每项补 max_gap 和 public_older（公共版更旧的包）。"""
    for it in payload.get("items") or []:
        if it.get("type") == "conda-prefix":     # 项目里的环境：脚本多用相对路径或变量拼路径，服务器的 refs 认不出
            it["refs_note"] = ("refs 只认绝对路径和按名字激活；这个环境不在 conda 的 envs/ 下，脚本常写成 "
                               "./_data/envs/<名> 或 $D/_data/envs/<名>，查不到。refs 为空不算数，自己在项目脚本里搜 envs/<名>")
        ov = it.get("overlap") or []
        for o in ov:
            o["gap"], o["direction"] = version_gap(o.get("version"), o.get("shared_version"))
        if ov:
            it["max_gap"] = max((o["gap"] for o in ov), key=_GAP_ORDER.get)
            it["public_older"] = sorted(o["pkg"] for o in ov if o["direction"] == "older")
    return payload


# 个人搬运清单：服务器上原处的 *.ON_NAS.txt 才是准的，这份每次 archived 都重写；备注只在本机
ARCHIVE_LIST = os.path.join(HERE, "..", "references", "nas-archive.local.md")
ARCHIVE_NOTES = os.path.join(HERE, "..", "references", "nas-archive.local.json")


def _note_key(cfg, path):
    """备注以 ~/… 为键：服务器绝对路径、Git Bash 改写过的路径、说明文件路径都换成这一种。"""
    p = server_path(path).replace("\\", "/").rstrip("/")
    if p.endswith(".ON_NAS.txt"):
        p = p[:-len(".ON_NAS.txt")]
    home = "/home/%s" % cfg["user"]
    if p == home or p.startswith(home + "/"):
        p = "~" + p[len(home):]
    return p


def _load_notes():
    try:
        with open(ARCHIVE_NOTES, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def cmd_cleanup_note(cfg, path, text):
    notes = _load_notes()
    key = _note_key(cfg, path)
    notes[key] = {"note": text, "at": time.strftime("%Y-%m-%d")}
    with open(ARCHIVE_NOTES, "w", encoding="utf-8", newline="\n") as f:
        json.dump(notes, f, ensure_ascii=False, indent=1)
        f.write("\n")
    return {"path": key, "note": text, "saved_to": os.path.abspath(ARCHIVE_NOTES)}


def write_archive_list(cfg, payload):
    notes = _load_notes()
    rows = []
    for it in payload.get("items") or []:
        key = _note_key(cfg, it.get("path_display") or it.get("path", ""))
        it["note"] = (notes.get(key) or {}).get("note")
        size = it.get("bytes")          # 旧格式的说明文件（nas-archive 写的）不记原始大小，只好写压缩包的
        size_txt = "%.1f GB" % (size / 1e9) if size else \
            "压缩后 %.1f GB" % (it["archive_bytes"] / 1e9) if it.get("archive_bytes") else "?"
        state = "在" if it.get("on_nas") else "**找不到**"
        if it.get("back_in_place"):
            state += "（原处又有东西了）"
        rows.append("| `%s` | %s | %s | %s | `%s` %s | %s |" % (
            key, it.get("date") or "?", size_txt, it.get("by") or "?",
            os.path.basename(it.get("archive") or "?"), state, it.get("note") or ""))
    lines = ["# 搬到 NAS 的东西（个人清单）", "",
             "> 由 `advise.py cleanup archived` 生成于 %s。以服务器上原处的 `*.ON_NAS.txt` 为准，这份只是方便查；"
             "备注只存在本机 `nas-archive.local.json`，用 `cleanup note <路径> \"<备注>\"` 写。" % time.strftime("%Y-%m-%d %H:%M"),
             "", "| 原路径 | 搬走日期 | 大小 | 谁搬的 | NAS 上的包 | 备注 |", "|---|---|---|---|---|---|"]
    lines += rows or ["| （没有） | | | | | |"]
    for title, xs in (("中途留下的目录（确认没用了可以删）", payload.get("leftovers")),
                      ("NAS 上写到一半的包（确认没在跑可以删）", payload.get("orphan_parts"))):
        if xs:
            lines += ["", "## " + title, ""] + ["- `%s`" % x for x in xs]
    with open(ARCHIVE_LIST, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    payload["personal_list"] = os.path.abspath(ARCHIVE_LIST)
    return payload


# 服务器上的项目：代码推了没、推 Gitea。逻辑在 remote_project.py，经 ssh 交给服务器上的 python3 -I - 执行；
# 它查数据库引用要用 db_project.py，两个文件打成一个包一起送过去（服务器上没有本 skill 的文件）。
def _source(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def _bundle(main, *modules):
    """给 `python3 -I -` 的 stdin：先从源码建好 modules（import 得到），再跑 main。"""
    parts = ["import sys as _sys, types as _types"]
    for m in modules:
        parts.append("_m = _types.ModuleType(%r)\nexec(compile(%r, %r, 'exec'), _m.__dict__)\n_sys.modules[%r] = _m\ndel _m"
                     % (m, _source(m + ".py"), m + ".py", m))
    parts.append(_source(main))
    return "\n".join(parts)


def cmd_project(cfg, argv, timeout=180):
    script = _bundle("remote_project.py", "db_project")
    remote = "GIT_TERMINAL_PROMPT=0 python3 -I - " + " ".join(shlex.quote(a) for a in argv)
    rc, out, err = ssh_try(cfg, remote, timeout=timeout, stdin_data=script)
    if rc == 255 and not out.strip():
        return 255, {"error": "ssh 没成功：" + err.strip()[-300:],
                     "hint": "推送可能已经在服务器上做完了：先 project check 看看，别直接重推"}
    if not out.strip():
        return (rc or 1), {"error": (err.strip() or "remote_project 没有输出")[-400:]}
    try:
        return rc, json.loads(out)
    except ValueError:
        return 1, {"error": "remote_project 的输出不是 JSON", "stdout": out.strip()[-400:], "stderr": err.strip()[-400:]}


# ----------------------------- database: 公共数据库 -----------------------------
# 查（面板 /api/software）、自己装进 NAS 公共库（服务器的 db-install：plan 预演 → 用户同意 → submit）、
# 项目的数据库清单 databases.lock.json（db_project.py：项目在本机就在本机扫，否则送到服务器上扫）、
# 个人副本的比对与收编。会写东西的默认只预演。

DB_ROOT = "/opt/bio/db"
DB_INSTALL = "/usr/local/bin/db-install"
DB_CACHE = "/usr/local/bin/db-cache"
CACHE_ROOT = "/scratch/db-cache"
NAS_MIN_BYTES = 2 * 10 ** 12          # 同服务器：NAS 装完至少剩 2 TB
LOCAL_MIN_BYTES = 350 * 10 ** 9       # 同服务器：本地至少留 350 GB，不够就取不进缓存
RECIPES = ("generic", "gtdbtk", "bakta", "checkv", "genomad")
LOCATION_CN = {"local": "本地", "nas": "NAS", "mixed": "两边都有"}
VERIFICATION_CN = {"official": "已和官方校验值核对", "self": "上游没有校验值，只核对了下载完整",
                   "local-copy": "本地副本，未与官方核对"}
COMPAT_CN = {"pass": "服务端兼容检查通过", "fail": "服务端兼容检查失败", "untested": "未取得检查结果"}
INSTALL_STATUS_CN = {"running": "安装中", "success": "装好了", "failed": "失败", "exists": "公共区已有，没有装",
                     "pending": "排队中", "submitted": "已提交"}
_DB_PROJECT = None


def _db_project():
    """按路径载入 db_project.py：ws 经 PC 引擎调用时 sys.path 里没有本目录，不能直接 import。"""
    global _DB_PROJECT
    if _DB_PROJECT is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("db_project", os.path.join(HERE, "db_project.py"))
        _DB_PROJECT = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_DB_PROJECT)
    return _DB_PROJECT


def _server(cfg, command, timeout=60, stdin_data=None):
    """在服务器上跑一条命令：本机就是服务器时直接跑，否则经 ssh。-> (rc, stdout, stderr)"""
    if not _on_server(cfg):
        return ssh_try(cfg, command, timeout=timeout, stdin_data=stdin_data)
    try:
        r = subprocess.run(["bash", "-c", command], capture_output=True, timeout=timeout,
                           input=stdin_data.encode("utf-8") if stdin_data is not None else None,
                           stdin=subprocess.DEVNULL if stdin_data is None else None)
    except subprocess.TimeoutExpired:
        return 255, "", f"timed out after {timeout}s"
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def _gb(n):
    try:
        return round(int(n) / 1e9, 1)
    except (TypeError, ValueError):
        return None


def _size(n):
    """给人看的大小（10^3 进位，同本 skill 其他地方的 GB）：不足 1 MB 用 KB，不足 1 GB 用 MB，其余用 GB。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    div, unit = (1e9, "GB") if abs(n) >= 10 ** 9 else (1e6, "MB") if abs(n) >= 10 ** 6 else (1e3, "KB")
    return "%.1f %s" % (n / div, unit)


def _size_bytes(s):
    """meta 的 size：du -sh 的写法（84G、348M、1.4T），也认纯字节数。读不出返回 None。"""
    s = str(s or "").strip()
    if re.fullmatch(r"[0-9]+", s):
        return int(s)
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGTP])(?:i?B)?", s, re.I)
    return int(float(m.group(1)) * 1024 ** ("KMGTP".index(m.group(2).upper()) + 1)) if m else None


def _row_bytes(x):
    """面板行（每个版本一行，带 meta 的键）的大小：优先 size_bytes（字节数），没有再解析 size。"""
    try:
        return int(x.get("size_bytes"))
    except (TypeError, ValueError):
        return _size_bytes(x.get("size"))


def _db_install(cfg, argv, timeout=300):
    """调服务器的 db-install（--json）。成功 -> (0, dict)；它的报错是 stderr 上的一行 JSON。"""
    rc, out, err = _server(cfg, DB_INSTALL + " " + " ".join(shlex.quote(a) for a in argv) + " --json", timeout)
    if rc == 0:
        try:
            return 0, json.loads(out)
        except ValueError:
            return 1, {"error": "db-install 的输出不是 JSON", "stdout": out.strip()[-400:]}
    if rc == 127 or "No such file or directory" in err and DB_INSTALL in err:
        return 4, {"error": "服务器上还没有 db-install（需要管理员部署）"}
    for line in reversed(err.strip().splitlines()):
        try:
            d = json.loads(line)
            if isinstance(d, dict) and d.get("error"):
                return 1, {"error": d["error"]}
        except ValueError:
            continue
    if rc == 255 and not out.strip():
        return 255, {"error": "ssh 没成功：" + err.strip()[-300:]}
    return (rc or 1), {"error": (err or out).strip()[-400:] or "db-install 失败"}


def _db_remote(cfg, argv, timeout=180):
    """把 db_project.py 送到服务器上执行一个子命令，原样带回 JSON 和退出码。"""
    remote = "GIT_TERMINAL_PROMPT=0 python3 -I - " + " ".join(shlex.quote(a) for a in argv)
    rc, out, err = _server(cfg, remote, timeout, _source("db_project.py"))
    if rc == 255 and not out.strip():
        return 255, {"error": "ssh 没成功：" + err.strip()[-300:]}
    try:
        return rc, json.loads(out)
    except ValueError:
        return (rc or 1), {"error": "db_project 的输出不是 JSON", "stdout": out.strip()[-400:], "stderr": err.strip()[-400:]}


def db_facts(cfg, pairs=(), fingerprints=False):
    """服务器上的库信息（公共库清单、current 指向、登录环境的数据库变量、工具版本、指纹），给本机扫描用。
    ws 的 ws_forward.py 也经 PC 引擎调这个函数。"""
    if _on_server(cfg):
        return _db_project().gather_facts(pairs, fingerprints)
    argv = ["facts"] + [x for n, v in pairs for x in ("--pair", n + "/" + v)] + (["--fingerprints"] if fingerprints else [])
    rc, out, err = _server(cfg, "python3 -I - " + " ".join(shlex.quote(a) for a in argv),
                           600 if fingerprints else 90, _source("db_project.py"))
    try:
        d = json.loads(out)
        if isinstance(d, dict) and "catalog" in d:
            return d
    except ValueError:
        pass
    return {"schema": 1, "catalog": None, "current": {}, "env": {}, "tools": {}, "fingerprints": {}, "db_check": None,
            "errors": ["服务器上的库信息取不到：" + ((err or out).strip()[-300:] or "rc=%s" % rc)]}


def _install_method(x):
    if x.get("via") == "self":
        who = x.get("installed_by") or x.get("added_by") or ""
        return "自助（%s）" % who if who else "自助"
    return "管理员直接装"


def _cache_state(cfg):
    """-> (缓存清单 | None, 缓存所在盘的剩余字节 | None)。一次服务器调用拿全。"""
    cmd = (DB_CACHE + " list --json; echo; echo '===FREE==='; python3 -c "
           + shlex.quote("import os;s=os.statvfs(%r);print(s.f_bavail*s.f_frsize)" % CACHE_ROOT))
    rc, out, err = _server(cfg, cmd, 60)
    head, _, tail = out.partition("===FREE===")
    try:
        cache = json.loads(head)
    except ValueError:
        cache = None
    try:
        free = int(tail.strip().splitlines()[-1])
    except (ValueError, IndexError):
        free = None
    return cache, free


def cmd_db_find(cfg, name):
    q = name.strip().lower()
    if not q:
        return 2, {"error": "名字不能为空"}
    try:
        d = _http_json(panel_url(cfg, "/api/software"))
    except Exception as e:
        return 1, {"error": f"面板连不上（{panel_url(cfg)}）：{e}", "hint": "确认这台电脑连着 Tailscale"}
    rows, groups = (d.get("inventory") or {}).get("dbs", []), {}
    for x in rows:                     # 哪些库命中（别名可能只写在某个版本的 meta 里）
        r, alias = _db_rank(q, x)
        if r < 99:
            g = groups.setdefault(x.get("name") or "", {"rank": r, "matched_alias": alias, "rows": []})
            g["rank"] = min(g["rank"], r)
            g["matched_alias"] = g["matched_alias"] or alias
    for x in rows:                     # 命中的库列出全部版本
        if (x.get("name") or "") in groups:
            groups[x.get("name") or ""]["rows"].append(x)
    need_cache = any(x.get("location") in ("nas", "mixed") or x.get("nas") for g in groups.values() for x in g["rows"])
    cache, free = _cache_state(cfg) if need_cache else (None, None)
    dbs = []
    for nm, g in sorted(groups.items(), key=lambda kv: (kv[1]["rank"], kv[0]))[:10]:
        versions = []
        for x in sorted(g["rows"], key=lambda x: _vkey(x.get("version")), reverse=True):
            loc = x.get("location") or ("mixed" if x.get("nas") else "local")
            v = {"version": x.get("version"), "path": x.get("path") or "%s/%s/%s" % (DB_ROOT, nm, x.get("version")),
                 "current": bool(x.get("current")), "location": loc, "location_cn": LOCATION_CN.get(loc, loc),
                 "size": _size(_row_bytes(x)), "size_bytes": _row_bytes(x),
                 "install_method": _install_method(x),
                 "via": "admin" if x.get("via") in ("request", "registered") else x.get("via") or "admin",
                 "installed_by": x.get("installed_by") or x.get("added_by") or None,
                 "verification": x.get("verification") or None,
                 "verification_cn": VERIFICATION_CN.get(x.get("verification") or "", ""),
                 "compat": x.get("compat") or None, "compat_cn": COMPAT_CN.get(x.get("compat") or "", ""),
                 "compat_tool": x.get("compat_tool") or None, "fingerprint": x.get("fingerprint") or None,
                 "layout": x.get("layout") or None}
            if loc in ("nas", "mixed"):
                prefix = "%s/%s/%s/" % (CACHE_ROOT, nm, x.get("version"))
                ready = cache is not None and any(e.get("status") == "ready" and str(e.get("path", "")).startswith(prefix)
                                                  for e in cache.get("items", []))
                v["cache_status"] = ("查询失败（不等于没缓存）" if cache is None else
                                     "已缓存（部分或全部，以 db-cache path 为准）" if ready else "未缓存")
                # 同 db-cache 的规则：剩余 − 还缺的（包按解开后的大小）− 其中最大的一个包 ≥ 350 GB（解开期间包要先放在本地）。
                # 面板只给整版大小：打包的按 3 倍估解开后的大小，最大的包按整版的包算（只有一个包时正好是它）
                packed = _row_bytes(x) or 0
                unpacked, held = (packed * 3, packed) if x.get("layout") == "archive" else (packed, 0)
                if not ready and free is not None and packed and free - unpacked - held < LOCAL_MIN_BYTES:
                    v["cache_hint"] = ("装好了，但本地只剩 %s：取进缓存要约 %s%s，取完会低于 350 GB 底线，"
                                       "暂时取不进缓存，还差约 %s。可以直接读 NAS（约 100 MB/s，适合查几条记录），"
                                       "或等本地腾出空间" % (
                                           _size(free), _size(unpacked),
                                           "（打包存放：解开后的大小按 3 倍估，解开期间还要先放下约 %s 的包）" % _size(held)
                                           if held else "", _size(LOCAL_MIN_BYTES + unpacked + held - free)))
            versions.append(v)
        cur = next((v["version"] for v in versions if v["current"]), None)
        dbs.append({"name": nm, "current_version": cur, "aliases": (g["rows"][0].get("aliases") or ""),
                    "matched_alias": g["matched_alias"], "versions": versions})
    names = {n.lower() for n in groups}
    installs = [_db_status_view(i)
                for i in d.get("db_installs") or []
                if (q in str(i.get("name") or "").lower() or str(i.get("name") or "").lower() in names)
                and i.get("status") in ("running", "failed")]
    out = {"query": name, "me": d.get("me"), "dbs": dbs,
           "installs": installs[:10], "page": panel_url(cfg, "/software"),
           "local_free_gb": _gb(free) if free is not None else None}
    if dbs:
        out["usage"] = ("脚本里写带版本号的路径 %s/<名>/<版本>，结果才可复现；current 和默认变量（如 $GTDBTK_DATA_PATH）"
                        "会随公共区更新而变，只在第一次挑版本时用。项目用到的库用 database lock 写进 databases.lock.json。"
                        "NAS 上的库用 gen-sbatch --stage <库路径> 先取进本地缓存。" % DB_ROOT)
    else:
        out["next"] = ("公共区没有时可自己装进 NAS 公共库。用 database plan <名> --url <官方下载地址> 看计划，"
                       "确认后用 database install。")
    return 0, out


def _db_status_view(record):
    item = dict(record, status_cn=INSTALL_STATUS_CN.get(record.get("status"), record.get("status")))
    legacy_id = item.pop("request_id", None)
    if item.get("via") in ("request", "registered") or (not item.get("via") and legacy_id):
        item["via"] = "admin"
        if "install_method" in item:
            item["install_method"] = _install_method(item)
    return item


def _install_argv(name, version, urls, recipe):
    return ([name] + (["--version", version] if version else []) + ["--url"] + list(urls) +
            (["--recipe", recipe] if recipe else []))


def _plan_view(p):
    spec = p.get("spec") or {}
    name, version = spec.get("name"), p.get("version")
    nas, local = p.get("nas") or {}, p.get("local_cache") or {}
    files = [{"name": f.get("name"), "url": f.get("url"), "bytes": f.get("bytes"), "size": _size(f.get("bytes")),
              "expanded_size": _size(f.get("bytes") if f.get("expanded_bytes") is None else f.get("expanded_bytes")),
              "checksums": ["%s（%s）" % (c.get("algorithm"), c.get("source")) for c in f.get("checksums") or []]}
             for f in p.get("files") or []]
    out = {"name": name, "version": version, "recipe": p.get("recipe"),
           "files": files, "plan_sha256": p.get("plan_sha256"),
           "verification": "official" if files and all(f["checksums"] for f in files) else "self",
           "nas": {"free": _size(nas.get("free_bytes")), "estimate": _size(nas.get("estimate_bytes")),
                   "after_install": _size(nas.get("remaining_bytes")), "minimum": _size(nas.get("minimum_bytes") or NAS_MIN_BYTES),
                   "allowed": bool(nas.get("allowed"))},
           "local_cache": {"free": _size(local.get("free_bytes")), "cache": _size(local.get("estimated_cache_bytes")),
                           "minimum": _size(local.get("minimum_bytes") or LOCAL_MIN_BYTES), "allowed": bool(local.get("allowed"))}}
    if p.get("same_version"):
        out.update(status="exists", path="%s/%s/%s" % (DB_ROOT, name, version),
                   next="公共区已经有 %s %s：直接用这个路径，不用装" % (name, version))
        return out
    if p.get("other_versions"):
        out["other_versions"] = [{"version": v, "path": "%s/%s/%s" % (DB_ROOT, name, v)}
                                 for v in sorted(p["other_versions"], key=_vkey, reverse=True)]
        out["ask_user"] = ("公共区已经有 %s 的 %s：先问用户是用已有的版本，还是另装 %s" %
                           (name, "、".join(x["version"] for x in out["other_versions"]), version))
    if not out["nas"]["allowed"]:
        out.update(status="blocked", error="NAS 装完只剩约 %s，低于 2 TB 底线，装不了：告诉用户，请管理员处理 NAS 空间" %
                   out["nas"]["after_install"])
        return out
    out["status"] = "ready"
    if not out["local_cache"]["allowed"]:
        out["cache_note"] = ("装得上，但装好后本地剩余不足 350 GB，暂时取不进缓存（本地剩 %s，这个库取进缓存约 %s）："
                             "可以先直接读 NAS，或等本地腾出空间" % (out["local_cache"]["free"], out["local_cache"]["cache"]))
    if out["verification"] == "self":
        out["verification_note"] = "有文件在上游找不到校验值：只能核对下载完整，登记为 verification=self"
    # 用服务器校验过的 spec 拼命令：版本、配方都写明，submit 重算出的 plan_sha256 不变
    full = ["database", "install"] + _install_argv(name, version, spec.get("urls") or [], spec.get("recipe")) + ["--expected-plan", p.get("plan_sha256") or "", "--yes"]
    out["install_command"] = " ".join(shlex.quote(a) for a in full)
    out["next"] = ("把计划给用户看（库名、版本、文件和大小、校验值来源、NAS 余量%s）；用户明确同意后运行 install_command。"
                   "安装以用户的名义交一个 Slurm 作业（2 核 4G）下载、核对、登记" % ("、要不要另装" if out.get("ask_user") else ""))
    return out


def _db_plan_error(cfg, error):
    msg = str(error)
    lower = msg.lower()
    result = {"error": "数据库预演失败：" + msg}
    storage_error = re.search(
        r"host is down|transport endpoint is not connected|stale file handle|no route to host|"
        r"network is unreachable|\berrno\s+(?:101|107|112|113|116)\b", lower)
    if storage_error and not lower.startswith("ssh 没成功") and "://" not in msg:
        # db-install 扫描 NAS 暂存区时可能只报相对路径 'db'。无路径的网络错误也可能来自上游。
        db_path = re.search(r"""['"]db(?:/|['"])""", lower)
        if db_path or _nas_status(cfg)["online"] is False:
            return {"error": "NAS 现在连不上。公共库放在 NAS 上。",
                    "detail": msg, "hint": "请告诉管理员，等 NAS 恢复后再运行 database plan。"}
    hints = {
        "cannot infer a unique version": "从官方下载页确认版本，再显式传 --version。",
        "subscription database is not accepted": "公共库只收可公开下载的库。KEGG 这类订阅库不收。",
        "only public https urls are allowed": "请用官网下载页提供的公网 HTTPS 下载地址。",
        "non-public address": "请用官网下载页提供的公网 HTTPS 下载地址。",
    }
    for reason, hint in hints.items():
        if reason in lower:
            result["hint"] = hint
            break
    return result


def cmd_db_plan(cfg, name, version, urls, recipe):
    argv = _install_argv(name, version, urls, recipe)
    rc, p = _db_install(cfg, ["plan"] + argv)
    if rc:
        return rc, _db_plan_error(cfg, p.get("error"))
    return 0, _plan_view(p)


def cmd_db_install(cfg, name, version, urls, recipe, expected, yes):
    argv = _install_argv(name, version, urls, recipe)
    if not yes:
        rc, p = _db_install(cfg, ["plan"] + argv)
        if rc:
            return rc, _db_plan_error(cfg, p.get("error"))
        view = _plan_view(p)
        view.update(executed=False, plan_matches=(p.get("plan_sha256") == expected) if expected else None)
        return 0, view
    if not expected or not re.fullmatch(r"[0-9a-f]{64}", expected):
        return 2, {"error": "--yes 要带 --expected-plan <database plan 给出的 plan_sha256>"}
    rc, r = _db_install(cfg, ["submit"] + argv + ["--expected-plan", expected])
    if rc:
        msg = str(r.get("error"))
        if "plan changed" in msg:
            return 1, {"error": "预演之后上游文件或空间情况变了（plan_sha256 对不上），没有提交：重新 database plan 给用户看",
                       "submitted": False}
        if "NAS space" in msg:
            return 1, {"error": "NAS 装完会低于 2 TB 底线，没有提交", "submitted": False}
        return rc, {"error": "提交失败：" + msg, "submitted": False}
    if r.get("status") == "exists":
        return 0, {"status": "exists", "submitted": False, "path": r.get("path"),
                   "note": "公共区已经有这个版本，没有提交安装：直接用 path"}
    return 0, {"status": "submitted", "submitted": True, "job_id": r.get("id"), "name": name,
               "version": (r.get("spec") or {}).get("version") or version,
               "next": "database status %s 看进度（下载、核对、登记都在作业里，可能要几个小时）；装好后 database find %s" %
                       (r.get("id"), name)}


def cmd_db_status(cfg, job_id):
    if job_id and not re.fullmatch(r"[0-9]+", job_id):
        return 2, {"error": "作业号只能是数字"}
    rc, d = _db_install(cfg, ["status"] + ([job_id] if job_id else []), timeout=60)
    if rc:
        return rc, d
    jobs = []
    for j in d.get("jobs") or []:
        j = dict(_db_status_view(j), mine=j.get("user") in (None, cfg.get("user")))
        if j.get("status") == "success" and j.get("current_switched"):
            j["note"] = "已经设成这个库的 current"
        jobs.append(j)
    jobs.sort(key=lambda j: str(j.get("time") or "9999"), reverse=True)   # 新的在前；只在队列里、还没有事件的算最新
    jobs.sort(key=lambda j: not j["mine"])                                # 本人的在前（稳定排序，保留上面的先后）
    out = {"jobs": jobs[:30], "scheduler": d.get("scheduler")}
    if d.get("scheduler") != "available":
        out["note"] = "队列查不了（%s）：历史记录里的「安装中」不代表作业还在跑" % (d.get("scheduler_error") or "原因未知")
    if job_id and not jobs:
        out["note"] = "没有作业 %s 的安装记录：作业还没开始，或者号写错了" % job_id
    return 0, out


def _adopt_view(p, name, version, path):
    v = p.get("version")
    out = {"name": name, "requested_version": version, "version": v, "path": path,
           "size": _size((p.get("spec") or {}).get("bytes")), "plan_sha256": p.get("plan_sha256"),
           "nas": {"after_install": _size((p.get("nas") or {}).get("remaining_bytes")),
                   "allowed": bool((p.get("nas") or {}).get("allowed"))},
           "local_cache_allowed": bool((p.get("local_cache") or {}).get("allowed"))}
    if p.get("same_version"):
        out.update(status="exists", public_path="%s/%s/%s" % (DB_ROOT, name, v),
                   next="公共区已经有 %s %s（同一份）：不用收编，脚本改用这个路径（database pin），之后可以删个人副本" % (name, v))
        return out
    if v != version:
        out["naming"] = ("和已登记的官方版本对不上：会登记成 %s，面板标「本地副本，未与官方核对」"
                         "（只有和 verification=official 的版本整份指纹一致时才沿用官方版本号）" % v)
    if not out["nas"]["allowed"]:
        out.update(status="blocked", error="NAS 收编后会低于 2 TB 底线，收不了")
        return out
    out["status"] = "ready"
    out["next"] = ("给用户看（放进 NAS 公共库后叫 %s，大小约 %s）；用户同意后：database adopt %s %s --path %s "
                   "--expected-plan %s --yes。收编在用户自己的 Slurm 作业里打包传给服务端，原目录不动；"
                   "作业里会逐个文件和这次预演核对，作业结束前别改这个目录" %
                   (v, out["size"], name, version, shlex.quote(path), p.get("plan_sha256")))
    return out


ADOPT_PLAN_WAIT = 45     # 预演作业交上去后最多等这么久：小于 AI 调命令的常见超时，算完就直接返回预演


def cmd_db_adopt(cfg, name, version, path, expected, yes, plan_job=False, result=None):
    """收编个人副本。预演和提交都由服务器上的 db_project 做：预演超过 1 GB 的目录放进低优先级作业（--plan-job），
    提交用服务器上保存的预演原文（db-install adopt-submit 只做轻量核对，逐个文件的校验在安装作业里）。"""
    p = server_path(path)
    if yes:
        if not expected or not re.fullmatch(r"[0-9a-f]{64}", expected):
            return 2, {"error": "--yes 要带 --expected-plan <database adopt 预演给出的 plan_sha256>"}
        rc, r = _db_remote(cfg, ["adopt-submit", name, version, "--path", p, "--expected-plan", expected], timeout=900)
        if rc:
            msg = str(r.get("error"))
            if "plan changed" in msg:
                return 1, {"error": "预演之后空间情况或目录变了（plan_sha256 对不上），没有提交：重新预演给用户看",
                           "submitted": False}
            if any(k in msg for k in ("changed since plan", "file count", "bytes", "owner", "belong")):
                return 1, {"error": "目录在预演之后变了（文件数、总大小或属主和预演不一致），没有提交：重新预演给用户看",
                           "detail": msg, "submitted": False}
            return rc, {"error": msg if "预演" in msg else "提交失败：" + msg, "submitted": False}
        if r.get("status") == "exists":
            return 0, {"status": "exists", "submitted": False, "path": r.get("path"), "note": "公共区已经有这一份，没有提交"}
        return 0, {"status": "submitted", "submitted": True, "job_id": r.get("id"),
                   "next": "database status %s 看进度。提交时服务器只做了轻量核对；安装作业里会逐个文件和预演的指纹核对，"
                           "对不上就失败、不登记，所以作业结束前别改这个目录。登记好之后脚本改用公共路径，"
                           "再按 database.md 的条件删个人副本" % r.get("id")}
    argv = ["adopt-plan", name, version, "--path", p, "--partition", cfg.get("partition", "main")]
    if plan_job:
        argv += ["--plan-job", "--wait", str(ADOPT_PLAN_WAIT)]
    if result:
        argv += ["--result", result]
    rc, r = _db_remote(cfg, argv, timeout=ADOPT_PLAN_WAIT + 240 if plan_job else 900)
    if r.get("plan"):
        view = _adopt_view(r["plan"], name, version, p)
        view.update(executed=False, preview=("作业 %s 里做的" % r["job_id"]) if r.get("job_id") else "当场做的")
        return 0, view
    if r.get("error"):
        return (rc or 1), {"error": "服务器没有接受这次收编：" + str(r["error"]), "job_id": r.get("job_id"),
                           "log": r.get("log"), "hint": "目录里不能有链接、特殊文件或不属于你的文件"}
    return rc, r       # 大目录：作业计划（等用户同意后 --plan-job），或作业还没算完（之后 --result 取）


def cmd_db_compare(cfg, path, name, version, yes, result):
    argv = ["compare", server_path(path), name] + ([version] if version else []) + (["--yes"] if yes else []) + \
        (["--result", result] if result else []) + ["--partition", cfg.get("partition", "main")]
    return _db_remote(cfg, argv, timeout=900)


TIDY_NEXT = {
    "verify": ["database compare {p} {name} {version}",
               "对上了：cleanup apply {p} --action remove --db {name} 先预演，用户同意再加 --yes。脚本里还写着这个路径："
               "先 database pin 改成公共版本路径，或者用 --action link（换成指向公共版本的链接，脚本不用改）"],
    "close": ["先把差的几个文件（diff）给用户看；用户同意后，把它们换成公共版本 /opt/bio/db/{name}/{version}/ 里的同名文件、"
              "删掉多出来的",
              "database compare {p} {name} {version}",
              "对上了：cleanup apply {p} --action remove --db {name} 先预演，用户同意再加 --yes（脚本里还写着这个路径："
              "先 database pin，或者用 --action link）"],
    "ask_public": ["database compare {p} {name} 看差在哪",
                   "问用户这份是什么、还用不用；要放进 NAS 公共库就 database adopt {name} <版本> --path {p}"
                   "（对不上官方版本会登记成 <版本>-local-<用户名>）"],
    "ask_other": ["问用户这是什么：官方发布的库先 database find <库名> 看公共区有没有，没有就 database adopt <库名> <版本> "
                  "--path {p} 放进 NAS 公共库；用自己的数据建的索引就留着"],
}


def cmd_db_tidy(cfg, min_gb=None):
    """home 里所有像数据库的目录（服务器上的 spark-cleanup dbscan，只看不动），每一项加上 spark-advisor 的下一步。"""
    rc, r = cmd_cleanup(cfg, ["dbscan"] + (["--min-gb", str(min_gb)] if min_gb is not None else []), timeout=1500)
    if "items" not in r:
        if "invalid choice" in str(r.get("error", "")) + str(r.get("stderr", "")):
            return 4, {"error": "服务器上的 spark-cleanup 还没有 dbscan（需要管理员部署新版）"}
        return (rc or 1), r
    counts, frees = {}, 0
    for it in r["items"]:
        s, m = it.get("suggest") or {}, it.get("match") or {}
        key = "ask_public" if s.get("action") == "ask" and m else "ask_other" if s.get("action") == "ask" else s.get("action")
        it["next"] = [x.format(p=it["path_display"], name=m.get("name", ""), version=m.get("version", ""))
                      for x in TIDY_NEXT.get(key, [])]
        it["size"], it["frees"] = _size(it["bytes"]["total"]), _size(it["bytes"]["private"])
        counts[it["category"]] = counts.get(it["category"], 0) + 1
        if s.get("action") in ("verify", "close") and not it.get("inside"):
            frees += it["bytes"]["private"]
    groups = {}
    for it in r["items"]:
        if it.get("category") == "other_db":
            g = groups.setdefault(it.get("group") or it["path_display"], {"items": 0, "bytes": 0, "looks_like": set()})
            g["items"] += 1
            g["bytes"] += it["bytes"]["total"]
            g["looks_like"].update(it.get("looks_like") or [])
    r["summary"] = {"by_category": counts, "verify_frees": _size(frees),
                    "other_db_groups": [{"group": k, "items": v["items"], "size": _size(v["bytes"]),
                                         "looks_like": sorted(v["looks_like"])}
                                        for k, v in sorted(groups.items(), key=lambda kv: -kv[1]["bytes"])],
                    "note": "verify / close 的几项核对校验值对上了才删（close 先补齐差的几个小文件）；frees 已扣掉和别处共用的硬链接。"
                            "inside 不为空的在另一项里面，先处理外面那项。认不出的库按 other_db_groups 一组问一次"}
    if not r.get("scan_complete"):
        r["warning"] = "home 太大，服务器在时限内没扫完：只是一部分"
    r["disk"] = disk_hint(cfg)
    return 0, r


def cmd_db_project(cfg, op, path, dbs=(), yes=False, plan_id=None):
    """scan / check / lock / pin：项目目录在本机就在本机做（改的是用户平时改代码的那份），否则送到服务器上做。"""
    dp = _db_project()
    local = os.path.expanduser(local_path(path))
    if _on_server(cfg) or os.path.isdir(local):
        return dp.project_op(op, local, lambda pairs, fp: db_facts(cfg, pairs, fp), dbs, yes, plan_id,
                             jobs_fn=dp.project_jobs if _on_server(cfg) else None,
                             display=None if _on_server(cfg) else (lambda top: top))
    argv = [op, server_path(path)] + [x for d in dbs for x in ("--db", d)] + (["--yes"] if yes else []) + \
        (["--plan-id", plan_id] if plan_id else [])
    return _db_remote(cfg, argv, timeout=900 if op in ("check", "lock", "pin") else 300)


def cmd_database(cfg, a):
    c = a.dbcmd
    if c == "find":
        return cmd_db_find(cfg, a.name)
    if c == "plan":
        return cmd_db_plan(cfg, a.name, a.version, [u for g in a.url for u in g], a.recipe)
    if c == "install":
        return cmd_db_install(cfg, a.name, a.version, [u for g in a.url for u in g], a.recipe,
                              a.expected_plan, a.yes)
    if c == "status":
        return cmd_db_status(cfg, a.id)
    if c == "adopt":
        return cmd_db_adopt(cfg, a.name, a.version, a.path, a.expected_plan, a.yes, a.plan_job, a.result)
    if c == "compare":
        return cmd_db_compare(cfg, a.dir, a.name, a.version, a.yes, a.result)
    if c == "tidy":
        return cmd_db_tidy(cfg, a.min_gb)
    if c in ("scan", "check", "lock", "pin"):
        return cmd_db_project(cfg, c, a.path, getattr(a, "db", []), getattr(a, "yes", False), getattr(a, "plan_id", None))


# ----------------------------- 项目环境（env-store） -----------------------------

def env_call(cfg, argv, timeout=60):
    """env-store 的错误 JSON 在 stderr；保留退出码，不改变 ssh_run 的默认行为。"""
    rc, out, err = ssh_try(cfg, "/usr/local/bin/env-store " + shlex.join(argv + ["--json"]), timeout=timeout)
    raw = (err if rc and err.strip() else out).strip()
    try:
        result = json.loads(raw)
    except ValueError:
        result = {"schema": 1, "status": "error", "error": "无法解析 env-store JSON：" + raw[:300]}
    if not isinstance(result, dict) or result.get("schema") != 1 or not result.get("status"):
        result = {"schema": 1, "status": "error", "error": "env-store 返回的不是 schema 1 JSON 对象"}
    result["returncode"] = rc
    if err.strip():
        result["stderr"] = err.strip()
    if rc:
        result.setdefault("error", "env-store 调用失败")
        result["next"] = {3: "环境已存档，请用 env-store fetch <fp> --wait 取回。",
                          4: "环境正在构建或取回，请等待返回的作业号完成。",
                          5: "环境失败或不存在；有存档先 fetch，没有存档才 env build。"}.get(
                              rc, "env-store 调用失败，请查看 error 和 stderr；连接失败时先恢复连接。")
    return result


def env_error(result):
    return bool(result.get("returncode") or result.get("error")) or result.get("status") in ("error", "failed", "unknown", "not_found")


def env_has_archive(status):
    return bool(status.get("archived")) and not status.get("archive_problem")


# env-store status 带着完整的 meta（每个包、原样的 pixi.lock 文本），塞进输出会让 AI 白白读几十 KB。
# 嵌进结果时只留判断用得上的字段；完整记录在服务器上用 env-store status <fp> --json 看。
ENV_STATUS_KEEP = ("schema", "status", "fp", "state", "local", "archived", "archive_pending", "archive_problem",
                   "job", "stale", "queue_available", "jobs", "last_used", "last_user", "use_sh", "created",
                   "creator", "source", "conda", "pypi", "bytes", "ready_at", "restored_at", "returncode",
                   "next", "error", "stderr", "detail", "summary")


def env_compact(status):
    if not isinstance(status, dict):
        return status
    out = {k: status[k] for k in ENV_STATUS_KEEP if k in status}
    if "envs" in status:  # status without fp lists every environment
        out["envs"] = {fp: env_compact(e) for fp, e in status["envs"].items()} if isinstance(status["envs"], dict) else status["envs"]
    return out


def env_restore_hint(fp):
    return ("环境已有存档，不重建；请用 env-store fetch %s --wait 取回，"
            "或直接 submit 交主作业，由 submit 自动取回。" % fp)


def env_project(cfg, path):
    path = server_path(path)
    if any(c in path for c in "\r\n\0"):
        die("环境项目路径不能包含换行或 NUL")
    if path.startswith("~/"):
        code = "import json,os,sys; print(json.dumps(os.path.expanduser(sys.argv[1])))"
        path = json.loads(ssh_run(cfg, "python3 -c " + shlex.quote(code) + " " + shlex.quote(path)))
    if not path.startswith("/"):
        die("环境项目目录必须是服务器上的绝对路径，或带引号的 '~/project'")
    return path


def env_fp(value):
    # 契约没有规定指纹长度；只限制为可安全嵌入脚本的单个 token。
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value):
        die("无效环境 fp：" + str(value))
    return value


def env_args(project, environment):
    if not environment or any(c in environment for c in "\r\n\0"):
        die("无效环境名")
    return [project, "-e", environment]


def env_plan_summary(plan):
    def count(value):
        return value if isinstance(value, int) else len(value or [])
    conda, pypi = count(plan.get("conda")), count(plan.get("pypi"))
    return {"fp": plan.get("fp"), "packages": conda + pypi, "conda": conda, "pypi": pypi,
            "estimated_size": _size(plan.get("estimated_bytes")),
            "warnings": plan.get("warnings", []), "rejections": plan.get("rejections", []),
            "note": "计划允许构建" if plan.get("allowed") else "计划拒收，未提交构建"}


_ENV_INIT = r'''
import json, os, subprocess, sys
def init(p):
    project = p['project']
    manifest = os.path.join(project, 'pixi.toml')
    project_exists = os.path.isdir(project)
    exists = project_exists and os.path.lexists(manifest)
    result = {'schema': 1, 'status': 'preview', 'project': project, 'project_exists': project_exists,
              'exists': exists, 'manifest_exists': exists, 'written': False, 'lock_run': False}
    if not project_exists:
        return dict(result, status='error', error='Spark 项目目录不存在或不可用：' + project)
    if exists and not os.path.isfile(manifest):
        return dict(result, status='error', error='pixi.toml 路径不是可用文件：' + manifest)
    if not exists:
        result['content'] = p['content']
    if not p['yes']:
        return result
    try:
        if not exists:
            try:
                with open(manifest, 'x', encoding='utf-8', newline='\n') as f:
                    f.write(p['content'])
                result.update(written=True, manifest_exists=True)
            except FileExistsError:
                result.update(exists=True, manifest_exists=True)
        proc = subprocess.run(['/usr/local/bin/pixi', 'lock'], cwd=project,
                              capture_output=True, text=True)
        result.update(status='locked' if proc.returncode == 0 else 'error', lock_run=True,
                      lock={'returncode': proc.returncode, 'stdout': proc.stdout, 'stderr': proc.stderr})
    except OSError as e:
        project_exists = os.path.isdir(project)
        result.update(status='error', error=str(e), project_exists=project_exists,
                      manifest_exists=project_exists and os.path.lexists(manifest))
    return result
print(json.dumps(init(json.load(sys.stdin)), ensure_ascii=False))
'''


_ENV_ADOPT_INSPECT = r'''
import json, os, subprocess, sys
p = json.load(sys.stdin)
path = os.path.join(p['project'], '.pixi', 'envs', p['environment'])
local = {'path': path, 'exists': os.path.lexists(path), 'size_kib': None, 'du_status': 'not_needed'}
if local['exists']:
    try:
        proc = subprocess.run(['du', '-s', '-k', '--', path], capture_output=True, text=True, timeout=10)
        if proc.returncode:
            local.update(du_status='error', warning='du 查询失败：' + proc.stderr.strip()[:300])
        else:
            local.update(size_kib=int(proc.stdout.split()[0]), du_status='ok')
    except subprocess.TimeoutExpired:
        local.update(du_status='timeout', warning='du 超过 10 秒，项目环境体积未知；不影响收编。')
    except (OSError, ValueError, IndexError) as e:
        local.update(du_status='error', warning='du 无法取得体积：' + str(e)[:300])
print(json.dumps({'schema': 1, 'status': 'ok',
                  'manifest_exists': os.path.isfile(os.path.join(p['project'], 'pixi.toml')),
                  'lock_exists': os.path.isfile(os.path.join(p['project'], 'pixi.lock')),
                  'project_environment': local}, ensure_ascii=False))
'''


def cmd_env_adopt(cfg, a):
    project = env_project(cfg, a.dir)
    env_args(project, a.environment)
    if a.environment in (".", "..") or any(c in a.environment for c in "/\\"):
        die("无效环境名：必须是 .pixi/envs 下的单个目录名")
    rc, out, err = ssh_try(cfg, "python3 -c " + shlex.quote(_ENV_ADOPT_INSPECT), timeout=30,
                          stdin_data=json.dumps({"project": project, "environment": a.environment}))
    try:
        inspection = json.loads(out)
    except ValueError:
        inspection = None
    if rc or not isinstance(inspection, dict) or inspection.get("status") != "ok":
        return 1, {"summary": {"note": "项目只读检查失败，未提交"}, "submitted": False,
                   "error": err.strip()[:300] or out.strip()[:300] or "无法解析项目检查结果",
                   "next": "恢复连接并确认项目目录可读后，重试 env adopt。"}
    local = inspection["project_environment"]
    local["size"] = _size(local["size_kib"] * 1024) if local["size_kib"] is not None else None
    if not inspection["manifest_exists"] or not inspection["lock_exists"]:
        missing = [name for name, key in (("pixi.toml", "manifest_exists"), ("pixi.lock", "lock_exists"))
                   if not inspection[key]]
        return 1, {**inspection, "summary": {"note": "缺少 " + "、".join(missing) + "，未收编"},
                   "submitted": False, "next": "在服务器项目目录先用 /usr/local/bin/pixi lock 生成 v7 锁；"
                   "没有 pixi.toml 时先用 env init --dir <项目> 初始化，确认后加 --yes。"}
    rc, result = cmd_env_build(cfg, a, project=project, adopt=True)
    result["project_environment"] = local
    if local.get("warning"):
        result.setdefault("warnings", []).append(local["warning"])
    if local["exists"]:
        fp = result.get("plan", {}).get("fp") or "<fp>"
        result["next"] += (" 待锁获准且 env-store 里的环境 ready 后，按顺序："
                           "1. 用 gen-sbatch --env %s -e %s 重新生成作业脚本，或将现有脚本的环境激活改为 "
                           "ENV1=$(env-store use %s) || exit 3 加 source \"$ENV1\"；"
                           "2. 跑一次作业，确认结果没有变化；"
                           "3. 再运行 cleanup scan --path %s，用 cleanup 清掉该项目环境。"
                           "spark-cleanup 会识别和 env-store 里同一份环境完全重复的包，删除前逐个先问；"
                           "/usr/local/bin/pixi run 以后需要时会自己重装。" %
                           (shlex.quote(project), shlex.quote(a.environment), shlex.quote(fp), shlex.quote(local["path"])))
    else:
        result["next"] += " 项目中不存在所选 .pixi/envs 环境，无需清理。"
    return rc, result


def cmd_env(cfg, a):
    c = a.envcmd
    if c == "check":
        return cmd_env_check(cfg, a.script)
    if c == "adopt":
        return cmd_env_adopt(cfg, a)
    if c == "init":
        project = env_project(cfg, a.dir)
        name = a.name or os.path.basename(project.rstrip("/")) or "project"
        content = ('[workspace]\nname = ' + json.dumps(name, ensure_ascii=False) + '\n'
                   'channels = ["conda-forge", "bioconda"]\nplatforms = ["linux-aarch64"]\n\n'
                   '[tasks]\nsmoke = "echo smoke-ok"\n')
        raw = ssh_run(cfg, "python3 -c " + shlex.quote(_ENV_INIT), timeout=600,
                      stdin_data=json.dumps({"project": project, "content": content, "yes": a.yes}))
        try:
            result = json.loads(raw)
        except ValueError:
            die("无法解析环境初始化结果：" + raw.strip()[:300])
        if not result["project_exists"]:
            result["next"] = "先确认 Spark 项目路径，选一个已存在的目录，或确认项目落点并建好目录后重新运行 env init。"
        elif env_error(result):
            result["next"] = "先查看 error 或 lock.stderr，修正配方路径或锁定错误后重新运行 env init。"
        else:
            edit = "在 Spark 项目目录用 /usr/local/bin/pixi add --no-install <包> 更新配方和锁。" + SMOKE_TEST_STEP
            path = shlex.quote(project)
            if not result["manifest_exists"]:
                name_opt = " --name " + shlex.quote(a.name) if a.name else ""
                edit = f"确认后运行 env init --dir {path}{name_opt} --yes，写出配方并生成初始锁。随后" + edit
            result["next"] = edit + f"锁定成功后运行 env build --dir {path} 看构建计划。"
        return (1 if env_error(result) else 0), result
    if c == "status":
        if a.dir:
            fp_result = env_current_fp(cfg, env_project(cfg, a.dir), a.environment)
            fp = fp_result["fp"]
        else:
            fp = env_fp(a.fp)
        result = env_call(cfg, ["status", fp])
        state_cn = {"ready": "本地可用", "archived": "仅 NAS 存档", "building": "构建中",
                    "restoring": "取回中", "failed": "上次失败"}
        result = env_compact(result)
        result["summary"] = state_cn.get(result.get("state"), "环境状态查询；完整记录在服务器上用 env-store status <fp> --json 看")
        return (1 if env_error(result) else 0), result
    return cmd_env_build(cfg, a)


def cmd_env_build(cfg, a, project=None, adopt=False):
    project = project or env_project(cfg, a.dir)
    argv = env_args(project, a.environment)
    plan = env_call(cfg, ["plan"] + argv)
    result = {"plan": plan, "summary": env_plan_summary(plan), "submitted": False}
    if env_error(plan) or not plan.get("allowed"):
        result["next"] = ("按 rejections 修正 pixi.toml 并用 /usr/local/bin/pixi lock 更新 v7 锁：仅允许指定 channel 和 wheel；"
                          "移除 git、sdist 和本地路径依赖，再运行 env build。")
        return 1, result
    fp = env_fp(plan.get("fp"))
    status = env_call(cfg, ["status", fp])
    result["environment_status"] = env_compact(status)
    if adopt and not env_error(status) and status.get("state") == "ready":
        result["summary"]["note"] = "env-store 里已有，本地可用，未提交构建"
        result["next"] = "可用 gen-sbatch --env 生成引用此 fp 的作业脚本。"
        return 0, result
    if status.get("state") == "archived" or (status.get("state") == "failed" and env_has_archive(status)):
        if adopt:
            result["summary"]["note"] = "env-store 里已有，有存档，未提交构建"
        result["next"] = env_restore_hint(fp)
        return 0, result
    # 不存在的 fp 会返回非零；仍由 submit 原子地复核存在性和存档状态。
    if env_error(status) and status.get("returncode") not in (1, 5):
        result["next"] = status.get("next", "查询环境状态失败，请先核查 env status。")
        return 1, result
    if a.yes:
        expected = plan.get("plan_sha256")
        if not expected:
            die("env-store plan 缺少 plan_sha256，不能提交")
        built = env_call(cfg, ["submit"] + argv + ["--expected-plan", expected] +
                         (["--wait"] if a.wait else []), timeout=86400 if a.wait else 120)
        result.update(result=built, submitted=built.get("status") == "submitted" or (built.get("status") == "ready" and bool(built.get("id"))),
                      next="用 env status 查询环境状态；ready 后可以生成作业脚本。")
        if built.get("status") == "archived":
            if adopt:
                result["summary"]["note"] = "env-store 里已有，有存档，未提交构建"
            result["next"] = env_restore_hint(fp)
        elif adopt and built.get("status") in ("exists", "ready") and not result["submitted"]:
            result["summary"]["note"] = "env-store 里已有，未提交构建"
        elif env_error(built):
            result["next"] = built.get("next", "构建提交失败，请查看 result。")
        return (1 if env_error(built) else 0), result
    result["next"] = "确认计划后加 --yes 提交本人名下的 Slurm 构建作业；可加 --wait 等待。"
    return 0, result


def env_current_fp(cfg, project, environment):
    argv = env_args(project, environment)
    result = env_call(cfg, ["fp"] + argv)
    if env_error(result) or not result.get("fp"):
        plan = env_call(cfg, ["plan"] + argv)
        die("环境锁不合格，请更新锁后 env build：" + json.dumps(plan, ensure_ascii=False))
    env_fp(result["fp"])
    return result


def script_envs(script):
    entries = []
    for line in script.splitlines():
        if not re.match(r"^# ENV-STORE(?:\s|$)", line):
            continue
        try:
            value = json.loads(line[len("# ENV-STORE"):].strip())
        except ValueError:
            die("无效 # ENV-STORE JSON 标记：" + line)
        if (not isinstance(value, dict) or not isinstance(value.get("project"), str)
                or not value["project"].startswith("/")
                or any(c in value["project"] for c in "\r\n\0")
                or not isinstance(value.get("env"), str)):
            die("# ENV-STORE 标记须包含 fp、服务器绝对 project 路径和 env")
        env_fp(value.get("fp"))
        env_args(value["project"], value["env"])
        if value not in entries:
            entries.append(value)
    return entries


def cmd_env_check(cfg, script_path):
    try:
        with open(local_path(script_path), encoding="utf-8") as f:
            entries = script_envs(f.read())
    except (OSError, UnicodeDecodeError) as e:
        die("读不了作业脚本：" + str(e))
    checks = []
    for entry in entries:
        current = env_call(cfg, ["fp"] + env_args(entry["project"], entry["env"]))
        status = env_call(cfg, ["status", entry["fp"]])
        changed = current.get("fp") != entry["fp"]
        state = status.get("state")
        missing = state not in ("ready", "archived", "building", "restoring")
        notes = []
        if env_error(current) or not current.get("fp"):
            notes.append("当前锁无法计算 fp，请修正锁后 env build。")
        elif changed:
            notes.append("锁已改变：env build 重建并重新 gen-sbatch；或明确确认继续使用旧 fp 复现旧结果。")
        if state == "archived" or (state == "failed" and env_has_archive(status)):
            notes.append(env_restore_hint(entry["fp"]))
        elif state == "restoring" and (not status.get("job") or status.get("stale")):
            notes.append("取回作业已过期；请 fetch，或 submit 自动重新取回。")
        elif state == "building" and (not status.get("job") or status.get("stale")):
            missing = True
            notes.append("构建作业已过期，请重新 env build。")
        elif missing:
            notes.append(status.get("next") if status.get("returncode") in (3, 4, 5)
                         else "脚本环境从未构建或已失败，请先 env build。")
        checks.append({**entry, "current": current, "environment_status": env_compact(status),
                       "changed": changed, "ok": not changed and not missing and not env_error(status),
                       "notes": notes or ["锁与脚本一致。"]})
    ok = all(e["ok"] for e in checks)
    return (0 if ok else 1), {"environments": checks, "ok": ok,
                             "summary": "环境检查通过" if ok else "环境需要处理，见各标记 notes"}


def env_submit_plan(cfg, entries):
    plan = []
    seen = set()
    for entry in entries:
        fp = entry["fp"]
        if fp in seen:
            continue
        seen.add(fp)
        status = env_call(cfg, ["status", fp])
        state = status.get("state")
        will_fetch = (state == "archived" or (state == "failed" and env_has_archive(status)) or
                      (state == "restoring" and (not status.get("job") or status.get("stale"))))
        if env_error(status) or (state not in ("ready", "building", "restoring") and not will_fetch):
            hint = status.get("next") if status.get("returncode") in (3, 4, 5) else "没有可用存档，请先 env build。"
            die("环境 %s 不可用（%s），%s：%s" %
                (fp, state or "unknown", hint, json.dumps(status, ensure_ascii=False)))
        if state == "building" and (not status.get("job") or status.get("stale")):
            die("环境 %s 的构建作业已过期，请重新 env build；不使用过期作业号做依赖。" % fp)
        if state in ("building", "restoring") and not will_fetch:
            env_job_id(status.get("job"))
        if state == "ready" and status.get("local") is False:
            die("环境 %s 报 ready 但本地不存在，请核查状态；有存档先 fetch，没有存档才 env build" % fp)
        plan.append({**entry, "state": state, "status": env_compact(status), "will_fetch": will_fetch})
    return plan


def env_job_id(value):
    value = str(value) if value is not None else ""
    if not re.fullmatch(r"[0-9]+", value):
        die("环境作业号缺失或无效，未提交主作业：" + value)
    return value


def disk_hint(cfg):
    """本地盘还剩多少、你的 home 多大（spark-status --json）；剩余低于 /ai.md 的 300 GB 线就提示可以 cleanup。
    只给 status 子命令用：cmd_status 还被 recommend / gen-sbatch / debug-start 调用，别拖慢它们。"""
    rc, out, err = ssh_try(cfg, "spark-status --json", timeout=40)
    try:
        d = json.loads(out)
    except ValueError:
        return {"error": "spark-status 读不到：" + (err or out).strip()[-200:]}
    nv = (d.get("disks") or {}).get("local_nvme") or {}
    res = {"local_free_gb": nv.get("free_gb"), "local_used_pct": nv.get("used_pct"),
           "my_home_gb": (d.get("my_storage") or {}).get("home_used_gb")}
    if isinstance(res["local_free_gb"], (int, float)) and res["local_free_gb"] < 300:
        res["hint"] = ("本地盘只剩 %d GB，低于 /ai.md 的 300 GB 线：可以 /spark-advisor cleanup 腾空间"
                       "（你的 home 占 %s GB）" % (res["local_free_gb"], res["my_home_gb"]))
    return res


_NAS_PROBE = r"""
import os, subprocess, sys

sys.path.insert(0, '/opt/bio/sbin')
try:
    from db_install_lib import Paths
    path = str(Paths().nas)
except Exception:
    print('config_error')
    sys.exit(0)
if not os.path.isabs(path) or any(c in path for c in '\r\n\0'):
    print('invalid_path')
    sys.exit(0)

proc = subprocess.Popen(['stat', '-t', os.path.join(path, '.')], stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    rc = proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    # A blocked filesystem syscall may not see SIGKILL until the kernel returns.
    # Do not wait again or leave the child holding SSH's output pipes.
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    print('timeout')
else:
    if rc:
        print('unreachable')
    else:
        with open('/proc/self/mountinfo', encoding='utf-8') as stream:
            mounted = any(line.split()[4] == path and line.split(' - ', 1)[1].split()[0] == 'cifs'
                          for line in stream)
        print('online' if mounted else 'unmounted')
"""


def _nas_status(cfg):
    rc, out, err = ssh_try(cfg, "/usr/bin/python3 -I -c " + shlex.quote(_NAS_PROBE), timeout=8)
    state = out.strip()
    notes = {"online": "NAS 在线。", "unreachable": "NAS 路径无法读取。",
             "unmounted": "NAS 未挂载。", "timeout": "NAS 路径探测超过 5 秒。"}
    if rc or state not in notes:
        config_notes = {"config_error": "无法从服务器 db-install 配置读取 NAS 路径。",
                        "invalid_path": "服务器 db-install 配置未返回有效的绝对 NAS 路径。"}
        if not rc and state in config_notes:
            return {"online": None, "state": "unknown", "note": config_notes[state]}
        return {"online": None, "state": "unknown", "note": "未取得 NAS 探测结果。",
                "detail": (err or out).strip()[:200]}
    return {"online": state == "online", "state": state, "note": notes[state]}


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
    # 按作业号的**数值**倒序：按字符串排 113 会排在 99 后面，作业号一跨位数，
    # 最新的作业反而被下面的 rows[:40] 截掉。数组作业形如 1264_3，取前面的号。
    rows.sort(key=lambda r: int(re.match(r"\d*", r["job"]).group() or 0), reverse=True)
    peaks = [r["host_peak_gb"] for r in rows if r.get("host_peak_gb")]
    return {
        "user": user, "tool_filter": tool, "days": days,
        "count": len(rows),
        "host_mem_peaks_gb": peaks,
        "note": "MaxRSS = HOST (CPU-side) memory. Reliable for CPU jobs; DISTORTED for GPU "
                "jobs (cudaMalloc bypasses cgroup accounting -> under-reports by ~100x). "
                "Per-job VRAM is sampled server-side since 2026-07-20 (gpu_proc_samples); "
                "read it with `advise.py gpu-peak <jobid>` (a LOWER bound -- 5s sampling "
                "misses spikes). This history command does not fetch VRAM, so do not expect "
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
    # lua 的兜底是 96000M（MiB）≈ 93.75 GiB；可调度内存按 GiB 算，比较和算余量都用这个数
    floor_gib = round(floor * 1000 / 1024, 2)
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
                f"(the default would leave only {round(pool - floor_gib, 1)}G).")
            rec["warnings"].append(
                "Declare honestly. spark-memguard reconciles actual usage against this number "
                "when memory gets tight (>=88%) and freezes whoever exceeds their own declaration "
                "by the most. Under-declaring puts you first in line to be frozen. "
                "Rounding UP costs nothing while memory is plentiful; rounding down is never free. "
                "But when schedulable memory is short, a bigger declaration makes you QUEUE (see run_now).")
        else:
            # 第一次跑某个工具/某个输入规模时，猜不如量。让 lua 兜底给 96G 跑一趟，
            # 采样器会记下真实峰值，下一次就能精确申报。
            rec["mem_gb"] = None
            rec["confidence"] = "low"
            rec["basis"].append(
                f"GPU job with no estimate: OMIT --mem for this first run -> lua gives the safe "
                f"{floor}G default. To get an exact number for NEXT time, make the job measure "
                f"itself (PyTorch: print torch.cuda.max_memory_reserved()/1024**3 at the end, then "
                f"declare that + the host-side peak, with margin). "
                f"Afterwards `advise.py gpu-peak <jobid>` reads the per-job VRAM the server sampled -- "
                f"a LOWER bound only (5s sampling can miss spikes). If fewer than {floor_gib}G are "
                f"schedulable right now, omitting --mem makes the job QUEUE (see run_now). "
                f"This applies to sbatch only: an interactive debug session cannot omit it "
                f"(debug-start needs --mem-gb; see debug-session.md for how to estimate).")
            rec["basis"].append(
                f"Cost of that default: only {round(pool - floor_gib, 1)}G left for everyone else "
                f"while this runs. Worth it once, to get a real measurement -- but for a long run "
                f"(a day or more), measure with a few-minute short run first instead of holding "
                f"{floor_gib}G for days.")
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

    # 超过整机可申请的上限，sbatch 当场拒收（"Requested node configuration is not available"，
    # 2026-10-12 实测），不是排队。估计 × 余量很容易冲过去（80G × 1.5 = 120G）—— 压到上限并说清楚。
    if rec["mem_gb"] and pool and rec["mem_gb"] > pool:
        rec["warnings"].append(
            f"{rec['mem_gb']}G is more than any job can request here ({pool}G): sbatch would reject it "
            f"outright, it would not queue. Capped to {int(pool)}G. If the job really needs more, "
            f"it cannot run on this server.")
        rec["mem_gb"] = int(pool)

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
            "run -- worse than not capping. Stuck GPU jobs are reclaimed by the idle-watchdog (idleguard: "
            "GPU+CPU both quiet ~1h -> auto scancel), not by wall-clock; CPU-only jobs are NOT watched, "
            "a hung one keeps its cores and memory until the 14-day limit. So pass NOTHING for --time to "
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
        # 卡空着也可能排队：GPU 作业一样要内存和核。2026-10-13 可调度内存只剩 23G 时，
        # 申报 30G 的 GPU 作业照样得等。没写 --mem 的按 lua 兜底的 96G 算。
        gpu_ok = not st["gpu_job_running"]
        mem_need = rec["mem_gb"] or floor_gib
        mem_ok = mem_need <= st["mem_schedulable_gb"]
        cpu_ok = rec["cpus"] is None or cpu_free is None or rec["cpus"] <= cpu_free
        rec["run_now"] = gpu_ok and mem_ok and cpu_ok
        if not gpu_ok:
            rec["warnings"].append(
                f"GPU is BUSY now ({st['pending_count']} already queued) -> your job will QUEUE.")
        if not mem_ok:
            rec["warnings"].append(
                f"Needs {mem_need}G" + ("" if rec["mem_gb"] else f" (no --mem -> lua default 96000M = {floor_gib}G)")
                + f" but only {st['mem_schedulable_gb']}G schedulable now -> will QUEUE"
                + (" even though the GPU is free." if gpu_ok else "."))
        if not cpu_ok:
            rec["warnings"].append(
                f"Needs {rec['cpus']} cores but only {cpu_free}/{cpu_total} cores free now -> will QUEUE.")
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


def vram_cap_block(mem_gb, declared=None):
    """生成一段可直接粘进脚本的显存自限提示：上限 = 申报量。

    刻意生成成**注释**而不是可执行代码：不同工具限制显存的方式完全不同，
    自动插一行 torch 调用可能直接改坏用户的程序。

    比例不在这里算死：GB10 是统一内存，nvidia-smi 报不出显存总量（[N/A]），torch 的
    total_memory 接近整机（约 121.7 GiB），比 Slurm 可分配的 111 GiB 大。拿可分配量当分母，
    上限会比申报高约 9%（2026-10-14 验收时发现）。所以给算式，让作业里按 total_memory 现算。
    """
    if not mem_gb:
        return []
    frac = "min(0.95, %s * 1024**3 / torch.cuda.get_device_properties(0).total_memory)" % mem_gb
    return [
        "# ── 显存自限（强烈建议）──────────────────────────────",
        "# 本机 --mem 管不住显存，%s是**申报**：机器内存吃紧时" % (declared or "上面那个 %sG " % mem_gb),
        "# spark-memguard 按它对账，超出最多的作业会被冻结。让程序自己守住（上限 = 申报量）：",
        "#   PyTorch  : torch.cuda.set_per_process_memory_fraction(%s)" % frac,
        "#   vLLM     : --gpu-memory-utilization $(python -c 'import torch; print(round(%s, 2))')" % frac,
        "#   不支持的工具（如 Boltz）：控制输入规模；结尾自己打印",
        "#     torch.cuda.max_memory_reserved()/1024**3，下次申报 = 它 + 主机侧峰值，再留余量",
        "# ─────────────────────────────────────────────────────",
    ]


def workdir_warning(workdir):
    """没给落点 → 日志会散在 SSH 登录目录（通常是 home 根）。"""
    if workdir:
        return None
    return ("没传 --workdir：Slurm 的 --output 是相对路径，作业经 SSH 提交时相对的是登录目录"
            "（通常就是 home 根）。跑几次之后 home 根会散一地 <作业名>-<ID>.log，"
            "看不出哪个对应哪次分析。把项目目录传给 --workdir，日志就进 <workdir>/logs/。")


# 低优先级：只在用户主动要求调低优先级时用（如说不急、可以让别人先跑），见 references/submit-flow.md「降低优先级」。
# 数值要大于公平份额权重：2026-11-07 线上 PriorityWeightFairShare=100000，相邻两档份额差约 12500 分，
# 小的 nice（如 100）只能调整自己几个作业之间的先后，让不了别人。管理员改权重时同步改这里。
LOW_PRIORITY_NICE = 100000
VERIFY_JOB_MARKER = "# SPARK-ADVISOR-VERIFY"


def env_activation_lines(variable):
    return [f'source "${variable}"',
            "# 课题工具由锁定的项目环境提供。",
            '_env_path=()', 'IFS=":" read -r -a _env_parts <<< "$PATH"',
            'for _env_part in "${_env_parts[@]}"; do',
            '  case "$_env_part" in /opt/bio/bin|/opt/bio/compat|/opt/bio/pixi/envs/*|/opt/spark-sys|/opt/spark-sys/*) ;; *) _env_path+=("$_env_part");; esac',
            'done', 'export PATH="$(IFS=":"; echo "${_env_path[*]}")"',
            'unset _env_path _env_parts _env_part']


def cmd_gen_sbatch(cfg, name, gres, mem_gb, time, cpus, command, out, workdir=None,
                   low_priority=False, stage=None, env=None, environment=None, env_steps=None, verify=False):
    if env_steps and (not env or environment is not None or command is not None):
        die("--env-step 需要 --env <项目>。每一步已指定环境和命令，请使用这一组参数。")
    if env_steps and any(not step_command.strip() for _, step_command in env_steps):
        die("每个 --env-step 都需要非空命令。")
    # 纯 CPU 作业不写 --mem 会被 job_submit.lua 在提交阶段直接拒绝，
    # 生成这样的脚本只会让用户白跑一趟拿个报错。宁可在这里就拦住。
    if not mem_gb and not (gres and "gpu" in str(gres).lower()):
        die("CPU 作业必须指定 --mem-gb：服务器的 job_submit.lua 会拒绝没写 --mem 的 CPU 作业。"
            "先用 recommend 拿一个推荐值，或让用户给个估计。")
    # 登录 shell 加载站点的 Slurm 与数据库辅助命令。课题工具由项目环境提供。
    L = ["#!/bin/bash -l",
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
        L.append("#   print('VRAM peak GB:', torch.cuda.max_memory_reserved()/1024**3)")
        L.append("# 下次改成 #SBATCH --mem=<显存峰值 + 主机侧峰值，再加 30%>G，别人就能和你同时用机器。")
        L.append("# 要跑一天以上的，先用同样参数跑几分钟短版拿峰值，别让别人连着几天只剩 ~18G。")
        L.append("# （跑完也能用 advise.py gpu-peak <作业号> 读服务器采到的显存，但那只是下界。）")
    if time:
        L.append(f"#SBATCH --time={time}")
    if low_priority:
        L.append(f"#SBATCH --nice={LOW_PRIORITY_NICE}")
        L.append("# 低优先级：排在所有人的普通作业之后。排队期间想恢复，运行 scontrol update jobid=<作业号> nice=0")
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
    # 和 /opt/bio/share/templates 的模板一致：出错就停，管道里任何一步失败都算失败。
    # 模板写的是 -euo，这里不带 -u：--command 原样写进来，命令里常有 conda activate，
    # 它的激活脚本在 -u 下会报 unbound variable。必须放在最后一个 #SBATCH 之后 ——
    # sbatch 遇到第一条真命令就不再读 #SBATCH。
    L += ["", "set -eo pipefail"]
    use_option = " --verify" if verify else ""
    if verify:
        L.append(VERIFY_JOB_MARKER)
    stages = cache_plan(cfg, stage or [])
    for i, entry in enumerate(stages, 1):
        path = entry["path"]
        L += ["# DB-CACHE-STAGE " + json.dumps(path, ensure_ascii=True),
              f"D{i}=$(db-cache use{use_option} {shlex.quote(path)})"]
    if env:
        project = env_project(cfg, env)
        names = dict.fromkeys(e for e, _ in env_steps) if env_steps else ["default" if environment is None else environment]
        variables = {}
        for i, selected in enumerate(names, 1):
            current = env_current_fp(cfg, project, selected)
            marker = {"fp": current["fp"], "project": project, "env": selected}
            variable = variables[selected] = f"ENV{i}"
            L += ["# ENV-STORE " + json.dumps(marker, ensure_ascii=True),
                  f"{variable}=$(env-store use{use_option} {current['fp']}) || exit 3"]
        if not env_steps:
            L += env_activation_lines("ENV1")
    if is_gpu:
        # 没写 --mem 的按 lua 兜底的 96000M 算：第一次跑、没估计正是最需要自限的时候
        floor_gib = round(cfg.get("gpu_floor_gb", 96) * 1000 / 1024, 2)
        L += [""] + vram_cap_block(
            mem_gb or floor_gib,
            None if mem_gb else "这趟没写 --mem，lua 兜底预留的 96000M（≈%sG）" % floor_gib)
    if env_steps:
        for selected, step_command in env_steps:
            L += ["", "(", *env_activation_lines(variables[selected]), step_command, ")"]
        L.append("")
    else:
        L += ["", command or "# TODO: your command here", ""]
    script = "\n".join(L)
    result = {"sbatch_script": script}
    if out:
        out = local_path(out)
        try:
            with open(out, "w", encoding="utf-8", newline="\n") as f:
                f.write(script)
        except OSError as e:
            die(f"写不了 --out {out}：{e}")
        result["written_to"] = out
    commands = "\n".join(step for _, step in env_steps) if env_steps else command
    warns = [w for w in (gpu_cap_warning(gres, commands),
                         cpus_warning(gres, cpus),
                         workdir_warning(workdir),
                         boltz_num_workers_warning(commands)) if w]
    if warns:
        result["warnings"] = warns
    if low_priority:
        result["low_priority"] = (
            "已加 --nice=%d。讲给用户：排队时排在所有人的普通作业之后，同样降级的作业之间按提交先后；"
            "资源空着照样马上跑，开始后不会被打断；排队期间想恢复，运行 "
            "scontrol update jobid=<作业号> nice=0。" % LOW_PRIORITY_NICE)
    if stages:
        result["stage"] = stages
        result["stage_note"] = ("验证用数据库在提交主作业前取进缓存，取回和登记均保持最近使用记录。"
                                if verify else "提交时先取未缓存的库，主作业带 afterok 依赖；仍须先展示计划并取得用户同意。")
    result["next"] = "Review, get user OK, then: advise.py submit --script <file>"
    return result


def cache_plan(cfg, paths):
    result = []
    for path in dict.fromkeys(paths):
        if (not path.startswith("/opt/bio/db/") or any(c in path for c in "\r\n\0")
                or ".." in path.split("/")):
            die("无效 --stage 库路径：" + path)
        entry = json.loads(ssh_run(cfg, "/usr/local/bin/db-cache _plan " + shlex.quote(path)))
        result.append(entry)
    return result


def script_stages(script):
    return [json.loads(m) for m in re.findall(r"^# DB-CACHE-STAGE (.+)$", script, re.M)]


def env_script_dependency(script):
    values = []
    for value in re.findall(r"^#SBATCH\s+(?:--dependency(?:=|\s+)|-d(?:=|\s*))(.*)$", script, re.M):
        try:
            words = shlex.split(value, comments=True)
        except ValueError:
            die("无法解析脚本依赖，请手动合并依赖后再提交。")
        if len(words) != 1 or "?" in words[0]:
            die("脚本依赖含 ? 或格式不支持，请把用户依赖与环境依赖手动合并后再提交。")
        values.append(words[0])
    # 和 sbatch 一样，重复指定时取最后一条；上面仍检查每条的 OR 运算。
    return values[-1] if values else None


def env_dependency_note(entries):
    jobs = [str(e["status"]["job"]) for e in entries
            if e["state"] == "building" and not e["will_fetch"]]
    jobs += [str(e["fetch"]["id"]) for e in entries
             if e.get("fetch", {}).get("status") == "building" and e["fetch"].get("id")]
    logs = "、".join("env-store-%s.log" % job for job in dict.fromkeys(jobs)) or "env-store-<作业>.log"
    return ("环境依赖使用 --kill-on-invalid-dep=yes；构建或取回依赖失败时，Slurm 自动取消主作业，"
            "避免主作业一直排队并 pin 环境。若冒烟测试失败，请查看构建日志 %s。" % logs)


def cmd_submit(cfg, script_path, check_only=False):
    script_path = local_path(script_path)
    if not os.path.exists(script_path):
        die(f"script not found: {script_path}")
    try:
        with open(script_path, encoding="utf-8") as f:
            script = f.read()
    except (OSError, UnicodeDecodeError) as e:
        die(f"读不了 --script {script_path}：{e}")
    verify = VERIFY_JOB_MARKER in script.splitlines()
    stages = cache_plan(cfg, script_stages(script))
    plan = {"stage": stages, "will_fetch": [e["path"] for e in stages if not e["cached"]],
            "dependency": "afterok:<fetch-job-ids>" if not verify and any(not e["cached"] for e in stages) else None}
    if verify:
        plan["verify"] = True
        if plan["will_fetch"]:
            plan["stage_note"] = "先等验证用数据库取回，再提交主作业并登记保护。"
    envs = env_submit_plan(cfg, script_envs(script))
    needs_env_dependency = any(e["will_fetch"] or e["state"] in ("building", "restoring") for e in envs)
    user_dependency = env_script_dependency(script) if needs_env_dependency else None
    if envs:
        plan["environments"] = envs
        plan["env_will_fetch"] = [e["fp"] for e in envs if e["will_fetch"]]
        if needs_env_dependency:
            plan["dependency"] = ",".join(filter(None, [user_dependency, "afterok:<fetch/build/restore-job-ids>"]))
            plan["kill_on_invalid_dep"] = True
            plan["dependency_note"] = env_dependency_note(envs)
    if check_only:
        return {"check_only": True, **plan, "sbatch_script": script, "submitted": False}
    dependencies = []
    for entry in stages:
        if entry["cached"]: continue
        if verify:
            # Verification registration requires a ready cache, unlike ordinary pin.
            rc, out, err = ssh_try(cfg, "/usr/local/bin/db-cache fetch --verify --wait " + shlex.quote(entry["path"]),
                                   timeout=86400)
            if rc or out.strip() != entry["local"]:
                die("验证用数据库取回失败或路径与预演不符，尚未提交主作业：" +
                    (err.strip() or out.strip() or entry["path"]))
            continue
        value = ssh_run(cfg, "/usr/local/bin/db-cache fetch " + shlex.quote(entry["path"])).strip()
        if re.fullmatch(r"[0-9]+", value): dependencies.append(value)
        elif value != entry["local"]: die("无法解析 db-cache fetch 结果：" + value)
    db_fetch_jobs = list(dependencies)
    env_fetch_jobs = []
    env_dependencies = []
    for entry in envs:
        if entry["will_fetch"]:
            fetched = env_call(cfg, ["fetch", entry["fp"]], timeout=120)
            entry["fetch"] = fetched
            if fetched.get("status") in ("submitted", "building", "restoring") and not env_error(fetched):
                job = env_job_id(fetched.get("id"))
                env_fetch_jobs.append(job)
                dependencies.append(job)
                env_dependencies.append(job)
            elif fetched.get("status") != "ready" or env_error(fetched):
                die("环境取回失败，未提交主作业；" + fetched.get("next", "请核查存档后重新 fetch；没有存档才 env build。") +
                    "：" + json.dumps(fetched, ensure_ascii=False))
        elif entry["state"] in ("building", "restoring"):
            job = env_job_id(entry["status"].get("job"))
            dependencies.append(job)
            env_dependencies.append(job)
    dependencies = list(dict.fromkeys(dependencies))
    dependency = "afterok:" + ":".join(dependencies) if dependencies else None
    if env_dependencies:
        dependency = ",".join(filter(None, [user_dependency, dependency]))
    if needs_env_dependency:
        # fetch 可能已经变 ready；此时没有环境依赖，保持脚本自己的依赖。
        if not env_dependencies and dependency and user_dependency:
            dependency = user_dependency + "," + dependency
        plan.update(dependency=dependency or user_dependency, kill_on_invalid_dep=bool(env_dependencies))
        if env_dependencies:
            plan["dependency_note"] = env_dependency_note(envs)
        else:
            plan.pop("dependency_note", None)
    # --chdir 的目标和它下面的 logs/ 必须在作业启动前就存在：Slurm 打不开 --output
    # 指定的文件时，作业直接失败，而且失败信息本身也没地方写。
    created = None
    m = re.search(r"^#SBATCH\s+--chdir=(\S+)", script, re.M)
    if m:
        wd = m.group(1)
        ssh_run(cfg, f"mkdir -p {shlex.quote(wd)}/logs")
        created = f"{wd}/logs"
    command = "sbatch" + (" --dependency=" + shlex.quote(dependency) if dependency else "")
    if env_dependencies:
        command += " --kill-on-invalid-dep=yes"
    out = ssh_run(cfg, command, stdin_data=script)
    result = {"submitted": out.strip(), "as_user": cfg["user"]}
    result.update(plan, fetch_jobs=db_fetch_jobs)
    if envs:
        result["env_fetch_jobs"] = env_fetch_jobs
        result["env_dependency_jobs"] = list(dict.fromkeys(env_dependencies))
    if stages or envs:
        match = re.search(r"Submitted batch job ([0-9]+)", out)
        if not match: die("无法解析主作业号，不能登记缓存使用：" + out)
        # Pin queued jobs immediately, including files whose fetch hasn't finished.
        for entry in stages:
            key_and_job = shlex.quote(entry["path"]) + " " + match.group(1)
            worker = "sudo -n -u dbcache /usr/local/bin/db-cache _worker "
            if verify:
                rc, _, err = ssh_try(cfg, worker + "use " + key_and_job + " --verify")
                if rc:
                    result.setdefault("warnings", []).append("验证用数据库登记失败：%s。主作业 %s 已提交，请核对登记。%s" %
                                                             (entry["path"], match.group(1), err.strip()))
            else:
                ssh_run(cfg, worker + "pin " + key_and_job)
        for entry in envs:
            pinned = _soft(env_call, cfg, ["pin", entry["fp"], "--job", match.group(1)])
            if pinned is None:
                pinned = {"status": "error", "error": "pin 调用失败，请按主作业号核查并重试 pin"}
            entry["pin"] = pinned
            if env_error(pinned) or pinned.get("status") != "pinned":
                # 主作业已提交，保留 JobID 和全部副作用供调用者处理。
                result.setdefault("warnings", []).append("环境 pin 失败：%s；主作业 %s 已提交，请核查并重试 pin，勿重复提交主作业。" %
                                                        (entry["fp"], match.group(1)))
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
DEBUG_GPU_DEFAULT_TIME = "2:00:00"  # 带 --gpu 的会话默认 2 小时：会话开着就占着唯一那块卡，
                                    # 超过 1 小时卡闲着又有人等卡，面板会公开点名（debug-session.md）
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
    time_str = time_str or (DEBUG_GPU_DEFAULT_TIME if gpu else DEBUG_DEFAULT_TIME)
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
                "server_now": _soft(cmd_status, cfg),   # 查不到就是 null，不丢掉这份结果
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
                    "名字 '%s' —— 有名有姓有上限，不会被当成卡死的作业。" % name,
            "warnings": [w for w in [cpus_warning("gpu:1" if gpu else None, cpus)] if w]}


DEBUG_RUN_DIR = "$HOME/.spark-debug"      # 每个会话一个子目录，存 cmd/out/rc

# 启动一次运行。用户命令走 stdin 落成 run-N.cmd，再生成一个 run-N.sh 包装脚本，
# 最后 nohup setsid 起飞 —— 三层都不带嵌套引号，命令里有啥字符都不会炸。
_DEBUG_LAUNCH = r"""
d="$HOME/.spark-debug/%(jid)s"; mkdir -p "$d" || exit 1
n=$(ls "$d"/run-*.cmd 2>/dev/null | wc -l); n=$((n+1))
cat > "$d/run-$n.cmd"
w="$d/run-$n.sh"
{ echo "srun --jobid=%(jid)s bash -l \"$d/run-$n.cmd\" > \"$d/run-$n.out\" 2>&1"
  echo "echo \$? > \"$d/run-$n.rc\""; } > "$w"
nohup setsid sh "$w" </dev/null >/dev/null 2>&1 &
echo "RUN=$n"
"""

# 在服务器上轮询（一次 ssh 往返，不是 N 次）。四种终态之外才叫「还在跑」。
#
# ★ 必须显式查冻结：被 memguard 冻结的作业在 squeue 里**状态仍是 RUNNING**，
#   .rc 文件也永远不会出现 —— 不查的话我会把它一直报成"还在跑"，等到天荒地老。
#   两个来源都认（memguard 清单 + 直接读 cgroup.freeze），理由同 cmd_wait：
#   只认清单的话，memguard 没跑 / 刚重启 / 文件还没生成时会漏判。
_DEBUG_POLL = r"""
d="$HOME/.spark-debug/%(jid)s"; n=%(run)s
out() { echo "STATE=$1"; echo "RC=$2"; echo "---OUT---"; cat "$d/run-$n.out" 2>/dev/null; exit 0; }
i=0; k=0
while [ $i -lt %(wait)s ]; do
  [ -f "$d/run-$n.rc" ] && out done "$(cat "$d/run-$n.rc")"
  k=$((k+1))
  if [ $((k %% 10)) -eq 0 ]; then
    CG=$(ls -d /sys/fs/cgroup/system.slice/*slurmstepd.scope/job_%(jid)s 2>/dev/null | head -1)
    [ -n "$CG" ] && [ "$(cat "$CG/cgroup.freeze" 2>/dev/null)" = "1" ] && out frozen ""
    grep -q '"job_id"[[:space:]]*:[[:space:]]*%(jid)s\b' \
         /run/spark-usage/frozen-jobs.json 2>/dev/null && out frozen ""
    squeue -h -j %(jid)s -o '%%T' 2>/dev/null | grep -q . || out session_gone ""
  fi
  sleep 1; i=$((i+1))
done
[ -f "$d/run-$n.rc" ] && out done "$(cat "$d/run-$n.rc")"
out running ""
"""


def _parse_poll(text):
    """轮询输出 -> (state, rc, output)。拿不到 state 说明远端脚本没跑起来。"""
    state, rc, out = None, None, ""
    head, sep, body = text.partition("---OUT---\n")
    if not sep:
        head, body = text, ""
    for line in head.splitlines():
        if line.startswith("STATE="):
            state = line[6:].strip()
        elif line.startswith("RC="):
            v = line[3:].strip()
            rc = int(v) if v.isdigit() else None
    return state, rc, body


def cmd_debug_run(cfg, jobid, command, wait):
    info = _debug_session_info(cfg, jobid)
    if not info:
        die("会话 %s 不在队列里 —— 已经到期或被取消了。"
            "用 `advise.py debug-list` 看还有哪些活着的会话。" % jobid)
    if info["state"] != "RUNNING":
        die("会话 %s 现在是 %s，还不能往里塞活。" % (jobid, info["state"]))

    jid = str(int(jobid))          # 只允许纯数字，杜绝拼进 shell 的注入面
    t0 = time.time()
    rc, out, err = ssh_try(cfg, _DEBUG_LAUNCH % {"jid": jid},
                           timeout=45, stdin_data=command)
    run_no = None
    for line in (out or "").splitlines():
        if line.startswith("RUN="):
            run_no = line[4:].strip()
    if not run_no:
        die("没能在会话 %s 里起活：%s" % (jid, ((err or out) or "")[:300]))

    # 轮询。本地 ssh 超时留足余量 —— 但**即便本地这条 ssh 再次超时，活也不会丢**，
    # 它是 detach 的、输出在文件里，用 `debug-log` 随时捞得回来。这正是这次改动的意义。
    prc, pout, perr = ssh_try(cfg, _DEBUG_POLL % {"jid": jid, "run": run_no,
                                                  "wait": int(wait)},
                              timeout=int(wait) + 30)
    state, exit_rc, output = _parse_poll(pout or "")
    elapsed = round(time.time() - t0, 2)
    after = _debug_session_info(cfg, jobid)

    result = _debug_outcome(jid, run_no, state, exit_rc, output, after, wait, perr)
    result["wall_seconds"] = elapsed
    # gen-sbatch 会提醒的坑，会话这条路也要提醒：Boltz 卡死时 debug-run / debug-log
    # 只会一直返回 pending，调用方照规矩接着等，就成了无限轮询
    w = boltz_num_workers_warning(command)
    if w:
        result.setdefault("warnings", []).append(w)
    return result


# 终态收口：调用方只看 outcome 一个字段就够，不用自己拼 state/rc/session_gone。
# 用户该被打扰的只有两件事 —— 活到了终态、会话过了 3/4 时限。其余都是实现细节。
def _debug_outcome(jid, run_no, state, exit_rc, output, after, wait, perr=None):
    r = {"jobid": jid, "run": str(run_no), "rc": exit_rc, "stdout": output,
         "log_on_server": "~/.spark-debug/%s/run-%s.out" % (jid, run_no),
         "time_left": (after or {}).get("time_left"),
         "warn": _debug_time_warning(after)}       # ← 提醒②：3/4 时限
    # ★ 先判「是不是被外力掐的」，再判程序自己的退出码。顺序反了就会把
    #   「会话被收走」误报成「你的程序失败了」—— 2026-09-07 实测踩过：scancel 会话后
    #   srun 收 SIGTERM 退出 143，.rc 照样写出来，于是被判成 done+failed。
    killed = (exit_rc is not None and exit_rc > 128) or "CANCELLED AT" in (output or "")
    if state == "done" and killed and after is None:
        r["outcome"] = "session_gone"; r["done"] = True
        r["note"] = ("⚠️ 会话没了，这次运行是**被掐断的**（退出码 %s），"
                     "不是你的程序自己报错。已跑出来的部分在 stdout 里，"
                     "但活没做完、也不会自动重跑 —— 开个新会话重来。" % exit_rc)
    elif state == "done":
        r["outcome"] = "ok" if exit_rc == 0 else "failed"
        r["done"] = True
        if exit_rc != 0:
            r["note"] = "程序退出码 %s —— 它跑完了但报错了，看 stdout。" % exit_rc
        if after is None:
            r["note"] = ((r.get("note") or "") +
                         "（另：会话已经结束了，要再跑得先开一个新的。）")
    elif state == "frozen":
        r["outcome"] = "frozen"; r["done"] = True
        r["note"] = ("⚠️ 这次运行被 memguard **冻结**了：实际用量超出会话申报量。"
                     "它没有死，已算完的部分都在 stdout 里。**别直接 scancel** —— "
                     "对冻结的作业发信号会让它永久卡在 COMPLETING 且不还内存，"
                     "用 `advise.py cancel-frozen %s`。下次开会话把 --mem 报大些。" % jid)
    elif state == "session_gone" or after is None:
        r["outcome"] = "session_gone"; r["done"] = True
        r["note"] = ("⚠️ 会话没了（多半是撞上 --time 被掐）。这次的活没跑完，"
                     "也不会自动重跑 —— 开个新会话重来。")
    else:
        # 还没到终态。**这不是结果，不该报给用户** —— 调用方应当自己接着轮询。
        r["outcome"] = "pending"; r["done"] = False
        r["resume_with"] = ("advise.py debug-log --jobid %s --run %s --wait 570"
                            % (jid, run_no))
        r["note"] = ("还没到终态（已等 %ss）。活是 detach 的、没有中断。"
                     "**别把这个当结果告诉用户** —— 用 resume_with 接着等，"
                     "直到 done=true。" % int(wait))
        if perr:
            r["poll_error"] = perr[:200]
    return r


def cmd_debug_log(cfg, jobid, run, wait):
    """捞某次运行的输出。不给 --run 就取最新一次。--wait 可继续等它跑完。"""
    jid = str(int(jobid))
    if run is None:
        rc, out, _ = ssh_try(
            cfg, 'ls "$HOME/.spark-debug/%s"/run-*.cmd 2>/dev/null | wc -l' % jid)
        run = (out or "").strip().splitlines()[-1] if (out or "").strip() else "0"
        if run == "0":
            die("会话 %s 名下还没有任何运行记录。" % jid)
    prc, pout, perr = ssh_try(cfg, _DEBUG_POLL % {"jid": jid, "run": run,
                                                  "wait": int(wait)},
                              timeout=int(wait) + 30)
    state, exit_rc, output = _parse_poll(pout or "")
    after = _debug_session_info(cfg, jobid)
    return _debug_outcome(jid, run, state, exit_rc, output, after, wait)


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
    # Match the retired commands before argparse and before loading config.
    if len(sys.argv) >= 3 and sys.argv[1] in ("software", "database") and sys.argv[2] in ("request", "vote"):
        steps = (["software find <工具名>", "env init --dir <项目目录>", "env build --dir <项目目录>",
                  "gen-sbatch --env <项目目录>"] if sys.argv[1] == "software"
                 else ["database find <库名>", "database plan <库名> --url <官方下载地址>",
                       "database install <计划中的参数> --expected-plan <plan_sha256> --yes"])
        emit({"ok": False, "error": "工具和数据库支持自助安装。查推荐配方，把工具锁进项目；查公共库，把缺少的数据库装进 NAS 公共库。",
              "next": steps})
        sys.exit(2)

    ap = argparse.ArgumentParser(description="spark-advisor engine (facts + baseline; AI does final judgment)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    sub.add_parser("rules")

    # 项目工具：查配方和可用的构建，扫描脚本里的工具箱调用
    sw = sub.add_parser("software")
    swsub = sw.add_subparsers(dest="swcmd", required=True)
    swf = swsub.add_parser("find")
    swf.add_argument("name")
    swf.add_argument("--arch", action="store_true", help="无匹配配方时查 ARM64 / noarch 构建与 PyPI wheel（也是默认行为）")
    swsub.add_parser("recipes", help="导出推荐配方和 Spark 系统命令名单 JSON")
    swsub.add_parser("scan", help="只读扫描 Spark 脚本里的工具箱调用").add_argument("paths", nargs="+", metavar="路径")

    # 公共数据库：查、自己装进 NAS 公共库、项目锁版本、个人副本。会写东西的不带 --yes 只预演
    db = sub.add_parser("database")
    dbsub = db.add_subparsers(dest="dbcmd", required=True)
    dbp = {}
    for nm in ("find", "plan", "install", "status", "adopt", "compare", "tidy", "scan", "check", "lock", "pin"):
        dbp[nm] = dbsub.add_parser(nm)
        dbp[nm].add_argument("--json", action="store_true", help="输出总是 JSON；写上也行")
    dbp["find"].add_argument("name")
    for nm in ("plan", "install"):
        dbp[nm].add_argument("name")
        dbp[nm].add_argument("--version", default="", help="不写时只从网址里唯一明确的版本号推断")
        dbp[nm].add_argument("--url", action="append", nargs="+", required=True, help="官方下载地址（公网 https），可写多个")
        dbp[nm].add_argument("--recipe", choices=RECIPES, default=None, help="内置配方；不写按库名选")
    dbp["install"].add_argument("--expected-plan", default=None, help="database plan 给出的 plan_sha256")
    dbp["install"].add_argument("--yes", action="store_true", help="真的提交安装作业。不带只重新预演")
    dbp["status"].add_argument("id", nargs="?")
    dbp["adopt"].add_argument("name")
    dbp["adopt"].add_argument("version")
    dbp["adopt"].add_argument("--path", required=True, help="本人目录（服务器上的绝对路径）")
    dbp["adopt"].add_argument("--plan-job", action="store_true",
                              help="目录超过 1 GB：把预演交给低优先级作业（要用户同意）")
    dbp["adopt"].add_argument("--result", default=None, help="取预演作业的结果（作业号）")
    dbp["adopt"].add_argument("--expected-plan", default=None)
    dbp["adopt"].add_argument("--yes", action="store_true")
    dbp["tidy"].add_argument("--min-gb", type=float, default=None, help="只列至少这么大的目录（默认 0.5 GB）")
    dbp["compare"].add_argument("dir", help="本人目录（服务器上的路径）")
    dbp["compare"].add_argument("name")
    dbp["compare"].add_argument("version", nargs="?")
    dbp["compare"].add_argument("--yes", action="store_true", help="目录大时交一个低优先级作业去算")
    dbp["compare"].add_argument("--result", default=None, help="取那个作业的结果（作业号）")
    for nm in ("scan", "check", "lock", "pin"):
        dbp[nm].add_argument("path", help="项目目录：在本机就扫本机，否则是服务器上的路径")
    for nm in ("lock", "pin"):
        dbp[nm].add_argument("--db", action="append", default=[], metavar="名=版本", help="指定锁哪个版本，可重复")
        dbp[nm].add_argument("--yes", action="store_true")
        dbp[nm].add_argument("--plan-id", default=None)

    ep = sub.add_parser("env", help="项目只读环境：init/build/adopt/status/check")
    eps = ep.add_subparsers(dest="envcmd", required=True)
    for nm in ("init", "build", "adopt", "status", "check"):
        parser = eps.add_parser(nm)
        parser.add_argument("--json", action="store_true", help="输出总是 JSON；写上也行")
        if nm in ("init", "build", "adopt", "status"):
            parser.add_argument("--dir", required=nm != "status", help="服务器上的项目目录")
        if nm in ("build", "adopt", "status"):
            parser.add_argument("-e", dest="environment", default="default")
        if nm in ("init", "build", "adopt"):
            parser.add_argument("--yes", action="store_true")
        if nm == "init":
            parser.add_argument("--name")
        elif nm in ("build", "adopt"):
            parser.add_argument("--wait", action="store_true")
        elif nm == "status":
            parser.add_argument("fp", nargs="?")
        else:
            parser.add_argument("script")

    # 清理和公共区重复的个人安装：转给服务器上的 spark-cleanup
    cl = sub.add_parser("cleanup")
    clsub = cl.add_subparsers(dest="clcmd", required=True)
    cls = clsub.add_parser("scan")
    cls.add_argument("--path", action="append", default=[])
    cls.add_argument("--db", default=None, help="每日清单以外的数据库副本：写公共库的库名")
    clr = clsub.add_parser("refs")
    clr.add_argument("path")
    cla = clsub.add_parser("apply")
    cla.add_argument("path")
    cla.add_argument("--action", choices=["remove", "link", "archive"], required=True)
    cla.add_argument("--env", action="store_true", help="和 --action archive 一起用：删环境前先原样打包到 NAS")
    cla.add_argument("--db", default=None, help="每日清单以外的数据库副本：写公共库的库名")
    cla.add_argument("--yes", action="store_true", help="真的执行。不带只预演")
    clc = clsub.add_parser("cold", help="大、而且很久没改过的目录")
    clc.add_argument("--min-gb", type=float, default=None)
    clc.add_argument("--days", type=int, default=None)
    clsub.add_parser("archived", help="搬到 NAS 的东西；顺手重写个人清单 references/nas-archive.local.md")
    clrs = clsub.add_parser("restore", help="从 NAS 搬回原处（不带 --yes 只预演）")
    clrs.add_argument("path")
    clrs.add_argument("--yes", action="store_true")
    clpark = clsub.add_parser("park", help="将从 NAS 恢复的目录核对后放回（默认只预演）")
    clpark.add_argument("path")
    clpark.add_argument("--yes", action="store_true")
    clpark.add_argument("--expect", help="预演返回的 plan_sha256")
    clj = clsub.add_parser("job", help="搬运 / 搬回作业的进度和结果")
    clj.add_argument("id")
    clv = clsub.add_parser("verify", help="转发既有服务器验证请求")
    clv.add_argument("path", nargs="?")
    clv.add_argument("--cmd", dest="cmds", nargs="+", required=True)
    cln = clsub.add_parser("note", help="给搬走的东西写备注（只存在本机）")
    cln.add_argument("path")
    cln.add_argument("text")

    # 服务器上的项目：代码推了没、推 Gitea（remote_project.py 经 ssh 在服务器上执行）
    pj = sub.add_parser("project")
    pjsub = pj.add_subparsers(dest="pjcmd", required=True)
    pjc = pjsub.add_parser("check")
    pjc.add_argument("path")
    pji = pjsub.add_parser("ignore")
    pji.add_argument("path")
    pji.add_argument("--add", nargs="+", required=True)
    pji.add_argument("--yes", action="store_true")
    pjp = pjsub.add_parser("push")
    pjp.add_argument("path")
    pjp.add_argument("-m", "--message", required=True)
    pjp.add_argument("--set-upstream", action="store_true")
    pjp.add_argument("--yes", action="store_true")
    pjp.add_argument("--plan-id", default=None)
    pjn = pjsub.add_parser("init")
    pjn.add_argument("path")
    pjn.add_argument("--remote", required=True)
    pjn.add_argument("--yes", action="store_true")

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
    g.add_argument("--low-priority", action="store_true",
                   help="只在用户主动要求调低优先级时用（如说不急、可以让别人先跑）：加 --nice=%d，"
                        "排到所有人的普通作业之后。默认不加" % LOW_PRIORITY_NICE)

    g.add_argument("--stage", action="append", default=[], help="NAS 库文件/目录；可重复，提交时先取库")
    g.add_argument("--verify", action="store_true", help="验证用作业，登记环境及缓存库，保留最近使用记录")

    g.add_argument("--env", help="服务器项目目录；使用其 pixi.lock 对应的只读环境")
    g.add_argument("-e", dest="environment", help="--command 使用的环境，默认 default")
    g.add_argument("--env-step", nargs=2, action="append", metavar=("环境", "命令"),
                   help="与 --env 一起使用，可重复。按给出的顺序在独立环境中执行每一步。")

    s = sub.add_parser("submit")
    s.add_argument("--script", required=True)
    s.add_argument("--check-only", action="store_true", help="只展示取库及依赖计划，不提交")

    # 盯住一个作业，出事就返回。给 AI agent 用：正常跑的时候一声不吭、不花 token，
    # 作业结束/失败/**被冻结**/被取消时立刻返回，冻结于是变成一个会主动送达的事件。
    w = sub.add_parser("wait")
    w.add_argument("jobid")
    w.add_argument("--interval", type=float, default=8.0,
                   help="轮询间隔秒数（默认 8）")
    w.add_argument("--max-wait", type=float, default=0,
                   help="监控自身的最长存活秒数，0 = 不限（作业自己卡死、没被冻结时会一直等到作业时限）。"
                        "建议按预计时长带上。超时返回 20，含义是「监控放弃了」而不是「作业超时了」。"
                        "返回码：0 正常 / 10 退出码非零 / 11 被冻结 / 12 被取消 / 13 失败 / "
                        "20 监控超时 / 21 查询失败 / 22 没有这个作业 / 255 连不上")

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
                    help="会话时长，默认 6:00:00；带 --gpu 默认 2:00:00（占着唯一那块卡）。"
                         "时限不能事后延长，CPU 会话往大了报")
    ds.add_argument("--cpus", type=int, default=None)
    ds.add_argument("--mem-gb", type=int, required=True,
                    help="必填。不写的话：CPU 会话被 job_submit.lua 拒绝；GPU 会话按 96G 兜底，"
                         "内存紧时根本申请不到。拿不准就先用 recommend 估一个")
    ds.add_argument("--gpu", action="store_true", help="会话要占 GPU")
    ds.add_argument("--name", default=None,
                    help="squeue 里显示的名字，默认 debug-<user>")
    ds.add_argument("--wait", type=int, default=20,
                    help="等不到资源就放弃的秒数（默认 20），避免调用方被吊住")

    dr = sub.add_parser("debug-run")
    dr.add_argument("--jobid", required=True)
    dr.add_argument("--command", required=True)
    dr.add_argument("--wait", type=int, default=570,
                    help="单次最多等多少秒（默认 570，压在调用方 10 分钟的工具上限内）。"
                         "等不到不是失败：outcome=pending，调用方自己接着轮询即可")
    dr.add_argument("--timeout", type=int, default=None,
                    help="旧名，等同 --wait（保留兼容）")

    dl = sub.add_parser("debug-log")
    dl.add_argument("--jobid", required=True)
    dl.add_argument("--run", default=None, help="第几次运行，默认最新一次")
    dl.add_argument("--wait", type=int, default=0,
                    help="再等它跑完多少秒（默认 0 = 只看一眼就返回）")

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

    # 验收测试的保险：子 agent 拿着真的 ssh 跑，光靠嘱咐不够，这里直接拒绝一切真动手。
    # 开关是环境变量，或 skill 目录里的 .no-yes 文件（子 agent 的命令拿不到我们设的环境变量）
    testing = os.environ.get("SPARK_ADVISOR_NO_YES") or os.path.exists(os.path.join(HERE, "..", ".no-yes"))
    if testing and (getattr(args, "yes", False) or getattr(args, "plan_job", False)
                    or args.cmd in ("submit", "debug-start", "debug-run")):
        die("测试模式（SPARK_ADVISOR_NO_YES / .no-yes）：不执行 --yes、提交作业或调试会话")

    # these bootstrap first-run config, so they must NOT require an existing config
    if args.cmd == "detect-server":
        emit(cmd_detect_server(args.pattern))
        return
    if args.cmd == "init":
        emit(cmd_init(args.user, args.host, args.node, args.partition,
                      args.gpu_pool_gb, args.gpu_floor_gb))
        return

    if args.cmd == "env" and args.envcmd == "status" and args.fp and args.dir:
        ap.error("env status 的 fp 和 --dir 不能同时指定")
    if args.cmd == "env" and args.envcmd == "status" and not (args.fp or args.dir):
        ap.error("env status 要指定 --dir <项目> 或 fp")
    cfg = load_config()
    if args.cmd == "env":
        rc, payload = cmd_env(cfg, args)
        emit(payload)
        sys.exit(rc)
    if args.cmd == "rules":
        return cmd_rules(cfg)
    if args.cmd == "software":
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import software_scan

        if args.swcmd == "find":
            emit(cmd_software_find(cfg, args.name))
            return
        try:
            rc, payload = cmd_software_on_spark(cfg, getattr(args, "paths", ()))
        except (SoftwarePanelError, software_scan.CatalogError) as e:
            die(str(e))
        emit(payload)
        sys.exit(rc)
    if args.cmd == "database":
        rc, payload = cmd_database(cfg, args)
        emit(payload)
        sys.exit(rc)
    if args.cmd == "cleanup":
        yes = ["--yes"] if getattr(args, "yes", False) else []
        if args.clcmd == "note":
            emit(cmd_cleanup_note(cfg, args.path, args.text))
            return
        if args.clcmd == "scan":
            argv = ["scan"] + [x for p in args.path for x in ("--path", server_path(p))] + \
                   (["--db", args.db] if args.db else [])
            rc, payload = cmd_cleanup(cfg, argv, timeout=600)
            annotate_gaps(payload)
        elif args.clcmd == "refs":
            rc, payload = cmd_cleanup(cfg, ["refs", server_path(args.path)], timeout=300)
        elif args.clcmd == "cold":
            argv = ["cold"] + (["--min-gb", str(args.min_gb)] if args.min_gb is not None else []) + \
                   (["--days", str(args.days)] if args.days is not None else [])
            rc, payload = cmd_cleanup(cfg, argv, timeout=900)
        elif args.clcmd == "archived":          # 不透传 --verify：逐个重算 NAS 上的包要几个小时
            rc, payload = cmd_cleanup(cfg, ["archived"], timeout=700)
            if rc == 0 and "items" in payload:
                write_archive_list(cfg, payload)
        elif args.clcmd in ("restore", "park"):
            expect = ["--expect", args.expect] if args.clcmd == "park" and args.expect else []
            rc, payload = cmd_cleanup(cfg, [args.clcmd, server_path(args.path)] + yes + expect, timeout=300)
        elif args.clcmd == "job":
            rc, payload = cmd_cleanup(cfg, ["job", args.id], timeout=60)
        elif args.clcmd == "verify":
            argv = ["verify"] + ([server_path(args.path)] if args.path else []) + ["--cmd"] + args.cmds
            rc, payload = cmd_cleanup(cfg, argv, timeout=120 * len(args.cmds) + 60)
        else:
            argv = ["apply", server_path(args.path), "--action", args.action] + \
                   (["--env"] if args.env else []) + (["--db", args.db] if args.db else []) + yes
            rc, payload = cmd_cleanup(cfg, argv, timeout=1800)
        emit(payload)
        sys.exit(rc)
    if args.cmd == "project":
        p = server_path(args.path)
        if args.pjcmd == "check":
            rc, payload = cmd_project(cfg, ["check", p])
        elif args.pjcmd == "ignore":
            rc, payload = cmd_project(cfg, ["ignore", p, "--add"] + args.add + (["--yes"] if args.yes else []))
        elif args.pjcmd == "init":
            rc, payload = cmd_project(cfg, ["init", p, "--remote", args.remote] + (["--yes"] if args.yes else []))
        else:
            argv = ["push", p, "-m", args.message] + (["--set-upstream"] if args.set_upstream else []) + \
                   (["--yes"] if args.yes else []) + (["--plan-id", args.plan_id] if args.plan_id else [])
            rc, payload = cmd_project(cfg, argv, timeout=660 if args.yes else 180)
        emit(payload)
        sys.exit(rc)
    if args.cmd == "status":
        out = cmd_status(cfg)
        out["disk"] = disk_hint(cfg)
        out["nas"] = _nas_status(cfg)
        emit(out)
    elif args.cmd == "history":
        emit(cmd_history(cfg, args.user, args.tool, args.days))
    elif args.cmd == "recommend":
        emit(cmd_recommend(cfg, args.tool, args.gpu, args.mem_guess, args.cpus, args.time))
    elif args.cmd == "gen-sbatch":
        emit(cmd_gen_sbatch(cfg, args.name, args.gres, args.mem_gb, args.time, args.cpus, args.command,
                            args.out, args.workdir, low_priority=args.low_priority, stage=args.stage,
                            env=args.env, environment=args.environment, env_steps=args.env_step, verify=args.verify))
    elif args.cmd == "submit":
        emit(cmd_submit(cfg, args.script, check_only=args.check_only))
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
        emit(cmd_debug_run(cfg, args.jobid, args.command,
                           args.wait if args.timeout is None else args.timeout))
    elif args.cmd == "debug-log":
        emit(cmd_debug_log(cfg, args.jobid, args.run, args.wait))
    elif args.cmd == "debug-list":
        emit(cmd_debug_list(cfg))
    elif args.cmd == "debug-end":
        emit(cmd_debug_end(cfg, args.jobid))


if __name__ == "__main__":
    main()
