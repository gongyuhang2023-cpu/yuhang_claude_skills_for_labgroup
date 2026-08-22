---
name: spark-advisor
description: 共享 GPU 服务器（Slurm/ARM64）作业顾问：先判任务该本地还是上服务器跑，上服务器的再定 --mem/--gres/--time 并可提交，避免统一内存 OOM。触发：把作业交到 spark/服务器、这活本地还是服务器跑、要多少内存/显存、服务器忙不忙、该排队还是能跑、推荐 slurm 参数。
---

# spark-advisor — Slurm 作业顾问

> 在共享 GPU 服务器（Slurm/ARM64）上正确跑活：先判**本地还是上服务器**，上服务器的再把 `--mem/--gres/--time` 定对。引擎 `scripts/advise.py`（纯标准库，封装好 SSH+slurm、只返回干净 JSON——**别自己现攒 ssh/slurm 命令**）。

**references 导航（按需读，别预载）**：
- 推荐判断规则 + 诚实纪律 → `references/recommend-rules.md`（出推荐前读）
- 硬约束 / `--mem` 语义 / 提交规则 / cgroup 兜底 → `references/server-facts.md`
- 本地 vs 服务器怎么判（带 benchmark） → `references/execution-strategy.md`
- aarch64 某包能不能装 → `references/env-notes.md`
- 本机踩过的坑（**你自己积累的**，不随本仓库分发、升级时不会被覆盖） → `references/spark-pitfalls.md`
- 目录约定 / 产物落在哪（**你自己积累的**，不随本仓库分发、升级时不会被覆盖） → `references/workspace-layout.local.md`（生成 sbatch 前读；**文件不存在就问用户**，别自己编一个路径）

## ⛔ 红线：GPU / 重活只能走 Slurm，绝不 ssh 裸跑

**IMPORTANT — YOU MUST**：任何 **GPU 活 / 重活**只能经 `gen-sbatch → submit`（或 `srun` 调试）走队列，**绝不** `ssh <server> 'python xxx.py'` 裸跑——哪怕用户说"就跑一下"。裸跑 = 不留痕（`sacct` 记不到 → 用量/采购/公平调度全失真）+ 逃过 memguard 反连累守规矩的人 + 抢显存撞崩别人的卡 + 不受 fairshare（四条详见 `references/server-facts.md`）。**轻活**（改代码、跑几秒的小脚本、`squeue`/`nvidia-smi` 查看类）可直接跑；拿不准就走队列（留痕 + 受保护，无坏处）。

## 首次配置（仅当 config.json 缺失；有就跳过）

`config.json`（gitignore）存服务器连接信息。缺了才走：① `advise.py detect-server` 扫 tailnet 得 IP+名 → ② 问"你在服务器上的用户名？（通常是姓名拼音小写）" → ③ `advise.py init --user <名> --host <IP> --node <名>`。**绝不硬编码服务器地址**，IP 只落进 gitignore 的 `config.json`。

## 环境感知（在服务器上跑程序前）

读 `env-profile.local.json`（gitignore，缓存本机 CUDA/Python + 你的 venv/项目路径）决定 sbatch 要不要 `source xxx/activate`。缺了先 `advise.py probe-env` 生成；`probed_at` >14 天、某 env 命令失败、或刚装新环境就重跑。某包能不能在 aarch64 装 → `references/env-notes.md`。

## 核心流程

**0 · 执行地判定**（先做）——这活该本地跑还是上服务器？
- 快速判据：**GPU / 峰值内存 >19G / 带宽·BLAS 密集 → 服务器**；**sklearn 树模型（RF/GBM）等分支密集 CPU 活 → 本地更快**；单线程指纹类两边打平。
- 拿不准 / 要对新任务判 → 读 `references/execution-strategy.md`（带 benchmark 与判据）。
- **判为本地 → 给本地建议，到此为止、不碰服务器。** 判为服务器 → 往下走（并守红线：走队列）。

**1 · 抽输入特征**（你做）→ 决定规模：Boltz token ≈ 蛋白残基 + 配体原子；Uni-Dock 盒子 × 并行配体数；纯 CPU 用 `--mem-guess <GB>`。

**2 · 查现状 + 历史 + baseline**（引擎一条命令）：
```
python scripts/advise.py recommend --tool <tool> [--gpu] [--mem-guess <GB>] [--cpus N]
```
→ 返回 `recommendation` + `server_now` + `history_summary`。

**3 · 定三个参数、出推荐**（依 `references/recommend-rules.md`）——人话讲清，**务必带置信度 + 依据**：

| 参数 | 默认动作 | 为什么 |
|---|---|---|
| `--mem` | 用 `recommendation.mem_gb`；GPU 侧宁可多报；**没测过就别写**（走 96G 兜底跑一趟） | 它管不住显存，是被 memguard 对账的**申报**——报低 = 排在第一个被冻。CPU 作业**必填**，不写会被 lua 直接拒收 |
| `--cpus` | 用 `recommendation.cpus`，**别漏传** | 不写落 Slurm 默认 1 核，把输入特征化 / MSA / BLAS 串行掉——**静默降级、无告警** |
| `--time` | **默认不传** | 估短了作业被 TIMEOUT 杀、整段白跑；不写 = 拿满 14 天默认（正规路径）。防"卡死作业空占卡"的是服务器端 idleguard 闲置看门狗，不是墙钟 |

- 仅当用户主动给安全上限时才传 `--time`；预计 >14 天才跑得完 → 提醒必须 checkpoint 分段。
- 每个参数具体怎么算、置信度怎么标 → `references/recommend-rules.md`。

**4 · 判排队还是能跑**——看 `run_now` / `warnings`：GPU 忙就明说排队、前面几个、按正在跑那个的 `time_left` 估等多久；内存或核数不够也明说会排队。

**5 · 生成脚本**：
```
gen-sbatch --name <n> --gres <g> --cpus <c> --mem-gb <m> --command "<cmd>" \
           --workdir <项目在服务器上的绝对路径> --out /tmp/job.sh
```
- **`--workdir` 决定产物落在哪，别省。** 作业会在该目录里跑、日志进 `<workdir>/logs/`（`submit` 会自动建这个目录）。
  **不传的下场**：Slurm 的 `--output` 是相对路径，而作业经 SSH 提交时相对的是**登录目录**——
  于是每跑一次就往 home 根扔一个 `<作业名>-<ID>.log`，攒几十个之后没人分得清哪个对应哪次分析
  （本机就是这么攒出来的）。**产物落在项目目录里还顺带进了备份，落 home 根则不在任何备份范围内。**
- 落点从哪来：本机约定读 `references/workspace-layout.local.md`；**该文件不存在（比如刚装到别人机器上）
  就问用户"这个项目在服务器上的目录是哪个"**——不要自己编一个，也不要默认落 home 根。
- ⚠ 在 Git Bash / MSYS 下调用要带 `MSYS_NO_PATHCONV=1`，否则 `/home/...` 会被自动改写成
  `C:/Program Files/Git/home/...`；这种脚本照样能提交，但作业一 chdir 就失败、且失败信息没地方写。
  引擎已会拦下并提示，但不如一开始就带上。
- **GPU 作业必查显存自限**：`--mem` 管不住显存（`cudaMalloc` 绕过 cgroup），只有程序自己能限。支持的工具（vLLM `--gpu-memory-utilization`）生成时直接带上；不支持的（Boltz）**明说拦不住**，靠控制输入规模 + 让程序结尾自己打印峰值。写法与机制 → `references/recommend-rules.md`「GPU 显存自限」。
- 引擎会回 `warnings`（漏 `--cpus` / 无显存自限 / Boltz `num_workers` 死锁）——**别忽略**。

**6 · 提交前确认门（强制）**：**IMPORTANT — YOU MUST** 先把资源给用户看、问一句"要现在提交吗？"、明确同意才提交。**绝不自动提交 GPU 作业。**
→ 用户点头 → `submit --script /tmp/job.sh` → 返回 JobID（告诉用户："可关机走人，`squeue` 看进度、`sacct -j <id>` 看用量"）。

## 踩坑即记（服务器相关坑，个人积累）

跑活时若撞到**和服务器（ARM / 统一内存 / slurm / 工具在本机的默认值）相关**的坑、且排出了根因或绕法 → **先问用户一句"要记进 `references/spark-pitfalls.md` 吗？下次不再踩"**，同意才追加（症状 → 根因 → 绕法 → 日期 + 置信度）。跑某类活前若可能撞已知坑，先扫 `spark-pitfalls.md`。该文件是**你自己的**，不在本仓库里、升级 skill 时也不会被覆盖；够通用且验证充分的坑，值得手动挪进 `env-notes.md`。

## 只是查状态？

用户只问"服务器现在什么情况" → `advise.py status`，人话汇总（GPU 空/忙、可调度 X G、CPU `cpu_free`/`cpu_total` 核空闲、队列 N 个），**不提交任何东西**。
