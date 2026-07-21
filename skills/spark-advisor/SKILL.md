---
name: spark-advisor
description: Slurm GPU 服务器作业顾问。查服务器现状+历史，为要提交的作业推荐正确的 --mem/--gres/--time 参数并可选直接提交，避免统一内存 OOM 或空占资源。触发：“把任务/作业交到 spark/服务器”、“这个任务要多少内存/显存”、“服务器现在什么情况/忙不忙”、“该排队还是能跑”、“推荐 slurm 参数”，或准备用 sbatch/srun 提交 GPU 作业时。
---

# spark-advisor — Slurm 作业参数顾问

> 帮用户（或调用你的 AI 工作流）在向共享 GPU 服务器提交作业前，把 `--mem/--gres/--time` 定对，避免统一内存 OOM 或空占资源。
> 引擎在 `scripts/advise.py`（纯标准库；把 SSH 和 slurm 命令封装好、只返回干净 JSON——**别自己现攒 ssh/slurm 命令**）。
> **推荐的判断规则在 `references/recommend-rules.md`，推荐前先读它。** 机器的硬事实在 `references/server-facts.md`。

## ⛔ 红线：GPU / 重活只能走 Slurm，绝不 ssh 裸跑

**IMPORTANT — YOU MUST**：帮用户在服务器上跑任何 **GPU 活 / 重活**，**只能**经本 skill 的
`gen-sbatch → submit`（或交互调试用 `srun`）走 Slurm 队列。**绝不 `ssh <server> 'python xxx.py'` 裸跑** ——
哪怕用户说"就跑一下"、哪怕图省事直接 ssh 进去更快。裸跑绕过 Slurm，有四重后果：

1. **不留痕** —— `sacct` 没有记录 → 用量统计算不到这个人 → 采购决策 / 公平调度的依据全失真。
2. **逃过看门狗，反而连累守规矩的人** —— 裸跑进程不在任何 `job_<N>` cgroup 里，`spark-memguard`
   按作业对账时归因不到它。内存吃紧时它超了也不会被冻，系统只能去冻**走了队列**的作业。
   最可能吃爆内存的恰恰是失控的裸跑进程，却正好逃过全机唯一那道防线。
3. **抢显存一起崩** —— 绕过 GPU 串行调度，和别人的作业撞同一块卡（这台机器的已知崩溃模式）。
4. **不受 fairshare** —— 多吃不给用得少的人让位。

**什么算可以直接跑的"轻活"**：改代码、跑几秒的小脚本、`nvidia-smi` / `squeue` 这类查看命令。
判据同使用指南——**会吃 GPU 或跑得久 = 走队列；瞬间完成的杂事 = 直接跑**。拿不准就走队列
（留痕 + 受保护，没有坏处）。

## 前置：首次配置（仅当 config.json 缺失时；有就跳过、直接干活）

skill 根目录的 `config.json`（gitignore）存服务器连接信息。**没有它才走下面这套无感配置**：

1. **自动扫服务器**：`python scripts/advise.py detect-server` → 从本机 tailnet 找到服务器的 IP + 名字（`node` 一般就用这个名字）。
2. **问用户名**：一句"你在服务器上的用户名？（= 你名字的拼音小写，如龚宇航是 `yuhang`）"。
3. **写入**：`python scripts/advise.py init --user <名字> --host <IP> --node <名字>` → 生成 config.json，此后永久无感。

**IP 的三个来源**（按可得性用）：① `detect-server` 自动扫到；② 用户把**实验室使用说明**发给了你 → 从里面读出 IP；③ 直接问用户。**绝不硬编码服务器地址**——IP 只落进 gitignore 的 `config.json`。

## 环境感知（要在服务器上跑程序前先做）

服务器是 **ARM64/aarch64**，跑程序需要知道它的环境（CUDA/Python 版本、该激活哪个 venv、某包能不能装）。**别每次进去重新调研** —— 有缓存：

1. **读 `env-profile.local.json`**（skill 根目录，gitignored）：这台机的系统环境 + 本用户的 venv/conda/项目路径。生成 sbatch 时据此决定要不要 `source ~/xxx/bin/activate` 之类。
2. **文件不存在 → 先生成**：`python scripts/advise.py probe-env`（一次 SSH 探测并写缓存）。
3. **陈旧就刷新**：`probed_at` 超过 ~14 天、或某条 env 相关命令失败、或用户刚装了新环境 → 重跑 `probe-env`。
4. **ARM 生态"能不能装"的问题** → 读 `references/env-notes.md`（哪些 aarch64 wheel 已确认可用/未验证、无系统 conda 等耐用知识）。

## 核心流程（5 步）

当用户要"把某工具的作业交到服务器"或问"这个任务要多少资源"时：

**① 抽输入特征**（你做）——你手里有输入文件，抽出决定规模的特征：
- Boltz：最大复合物的 token 数 ≈ 蛋白残基数 + 配体原子数
- Uni-Dock：对接盒子大小 × 并行配体数
- 纯 CPU 脚本：粗估要多少内存（用 `--mem-guess <GB>` 传给引擎）

**② 查现状+历史+baseline**（引擎做）——一条命令全拿到：
```
python scripts/advise.py recommend --tool <tool> [--gpu] [--mem-guess <GB>] [--cpus N]
```
返回 `recommendation`（gres/mem/time/confidence/basis/warnings/run_now）+ `server_now` + `history_summary`。

**③ 出推荐**（你做，依 `references/recommend-rules.md`）——把 baseline 用人话讲清，**务必带置信度 + 依据**。GPU 侧宁可多报。

**④ 判断能跑还是排队**——看 `recommendation.run_now` 和 `warnings`：GPU 忙就明说会排队、前面几个、看 `pending_jobs` 和 running 的 `time_left` 估等多久；内存不够也明说会排队。

**④· GPU 作业必做：显存自限 + 讲清 `--mem` 的新语义**

`--mem` 管不住显存（`cudaMalloc` 绕过 cgroup，实测 8 GB 只记 0.08 GB），只有程序自己能限制自己：
- 工具支持自限（如 vLLM `--gpu-memory-utilization`）→ **生成脚本时直接带上**，用户不必知道
- 工具不支持（如 Boltz）→ **明说拦不住**，给替代建议（控制输入规模 / 按实测峰值申报）
- 审用户已有脚本时，GPU 作业缺上限 → **主动指出**，这是职责不是可选项

★**2026-07-20 起 `--mem` 的语义变了，必须跟用户讲清**：
- 写了就**按写的给**（不再一律抬到 96G）；不写才兜底 96G
- 它是一份**申报**：内存 ≥88% 时 `spark-memguard` 按"实际占用 vs 申报量"对账，
  **冻结超出最多的那个作业**（冻结≠杀掉，可解冻续跑，但会打断）
- ⇒ 申报小不是占便宜，是把自己排到第一个被冻的位置。**没把握就别写**，
  走 96G 默认跑一趟，采样器会记下真实峰值，下次照着填

**⑤ 提交前确认门（强制）**：
> **IMPORTANT — YOU MUST**：生成 sbatch 后，**先把推荐参数（资源/时间）给用户看，问一句"要现在提交吗？"，用户明确同意后才提交。** 提交 GPU 作业消耗贵资源，绝不自动提交。
- 生成：`advise.py gen-sbatch --name <n> --gres <g> --mem-gb <m> --time <t> --command "<cmd>" --out /tmp/job.sh`
- 用户点头后：`advise.py submit --script /tmp/job.sh` → 返回 JobID
- 提交后告诉用户：JobID + "可以关机走人，`squeue` 看进度；`sacct -j <id>` 看用量"

## 只是查状态？
用户只问"服务器现在什么情况" → `advise.py status`，用人话汇总（GPU 空/忙、可调度 X G、队列 N 个），**不提交任何东西**。

## 诚实纪律（照 references/recommend-rules.md 执行）
- 没历史就标**置信度低**，别装准。
- ★ GPU 显存**现在有追踪了**：`sacct` 依然抓不到，但服务器的采样器在 GPU 作业运行时
  每 5 秒按作业记一次显存（`nvidia-smi --query-compute-apps` → PID → `job_<N>`）。
  ⇒ **低置信时别再提议"先做标定试跑"** —— 正常跑一次就有真实峰值了。正确建议是
  "这次不写 `--mem` 走默认，跑完看实测再定"。
- 估错有 cgroup 兜底（只崩用户自己的作业、可重交）——可据此安心给推荐，但仍要诚实标不确定。
  ★ 但**这条只对 CPU 内存成立**：`cudaMalloc` 的显存绕过 cgroup，GPU 侧没有兜底，
  别把"估错也没关系"这句话套到 GPU 显存上。
- CPU 作业的 `--mem` **必填**（不写会被拒绝提交），推荐里不能省。
