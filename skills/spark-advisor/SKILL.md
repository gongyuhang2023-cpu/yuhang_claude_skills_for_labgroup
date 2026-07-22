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
- 本机踩过的坑（**个人**，不进公开库） → `references/spark-pitfalls.md`

## ⛔ 红线：GPU / 重活只能走 Slurm，绝不 ssh 裸跑

**IMPORTANT — YOU MUST**：任何 **GPU 活 / 重活**只能经 `gen-sbatch → submit`（或 `srun` 调试）走队列，**绝不** `ssh <server> 'python xxx.py'` 裸跑——哪怕用户说"就跑一下"。裸跑 = 不留痕（`sacct` 记不到 → 用量/采购/公平调度全失真）+ 逃过 memguard 反连累守规矩的人 + 抢显存撞崩别人的卡 + 不受 fairshare（四条详见 `references/server-facts.md`）。**轻活**（改代码、跑几秒的小脚本、`squeue`/`nvidia-smi` 查看类）可直接跑；拿不准就走队列（留痕 + 受保护，无坏处）。

## 首次配置（仅当 config.json 缺失；有就跳过）

`config.json`（gitignore）存服务器连接信息。缺了才走：① `advise.py detect-server` 扫 tailnet 得 IP+名 → ② 问"你在服务器上的用户名？（拼音小写，如龚宇航 = `yuhang`）" → ③ `advise.py init --user <名> --host <IP> --node <名>`。**绝不硬编码服务器地址**，IP 只落进 gitignore 的 `config.json`。

## 环境感知（在服务器上跑程序前）

读 `env-profile.local.json`（gitignore，缓存本机 CUDA/Python + 你的 venv/项目路径）决定 sbatch 要不要 `source xxx/activate`。缺了先 `advise.py probe-env` 生成；`probed_at` >14 天、某 env 命令失败、或刚装新环境就重跑。某包能不能在 aarch64 装 → `references/env-notes.md`。

## 核心流程

**第 0 步 · 执行地判定**（先做）——这活该本地跑还是上服务器？
- 快速判据：**GPU / 峰值内存 >19G / 带宽·BLAS 密集 → 服务器**；**sklearn 树模型（RF/GBM）等分支密集 CPU 活 → 本地更快**；单线程指纹类两边打平。
- 拿不准 / 要对新任务判 → 读 `references/execution-strategy.md`（带 benchmark 与判据）。
- **判为本地 → 给本地建议，到此为止、不碰服务器。** 判为服务器 → 往下走（并守红线：走队列）。

**① 抽输入特征**（你做）→ 决定规模：Boltz token ≈ 蛋白残基 + 配体原子；Uni-Dock 盒子 × 并行配体数；纯 CPU 用 `--mem-guess <GB>`。

**② 查现状 + 历史 + baseline**（引擎一条命令）：
```
python scripts/advise.py recommend --tool <tool> [--gpu] [--mem-guess <GB>] [--cpus N]
```
→ 返回 `recommendation` + `server_now` + `history_summary`。

**③ 出推荐**（依 `references/recommend-rules.md`）——人话讲清，**务必带置信度 + 依据**，GPU 侧宁可多报。

**④ 判排队还是能跑**——看 `run_now` / `warnings`：GPU 忙就明说排队、前面几个、按正在跑那个的 `time_left` 估等多久；内存不够也明说会排队。

**④· GPU 作业必做 · 显存自限**：`--mem` 管不住显存（`cudaMalloc` 绕过 cgroup），只有程序自己能限。支持的工具（vLLM `--gpu-memory-utilization`）生成脚本时直接带上；不支持的（Boltz）明说拦不住 + 按实测峰值申报。`--mem` 是会被 memguard 对账的**申报**（写了按写的给、报低 = 优先被冻）。**各工具写法 / 机制 → `references/recommend-rules.md`「GPU 显存自限」+ `references/server-facts.md`。**

**④· 时间（--time）· ★默认不设限**：不替用户估紧 `--time`——估短了作业被 TIMEOUT 杀、整段白跑，比不设限更糟。不写 `--time` = 拿满 14 天默认（正规路径）；防"卡死作业空占卡"的是服务器端 **idleguard 闲置看门狗**（GPU+CPU 连续 ~1h 双静默 → 自动回收），不是墙钟。→ **gen-sbatch 默认不传 `--time`**；仅当用户主动给安全上限（想让忘关的作业早点释放）才传。预计 >14 天才跑得完的活 → 提醒得 checkpoint 分段。详见 `references/recommend-rules.md §时间`。

**⑤ 提交前确认门（强制）**：**IMPORTANT — YOU MUST** 先把资源（默认无时限）给用户看、问一句"要现在提交吗？"、明确同意才提交。绝不自动提交 GPU 作业。
- `gen-sbatch --name <n> --gres <g> --mem-gb <m> --command "<cmd>" --out /tmp/job.sh`（**默认不带 `--time`**，除非用户给了上限）→ 用户点头 → `submit --script /tmp/job.sh` → 返回 JobID（告诉用户："可关机走人，`squeue` 看进度、`sacct -j <id>` 看用量"）。

## 踩坑即记（服务器相关坑，个人积累）

跑活时若撞到**和服务器（ARM / 统一内存 / slurm / 工具在本机的默认值）相关**的坑、且排出了根因或绕法 → **先问用户一句"要记进 `references/spark-pitfalls.md` 吗？下次不再踩"**，同意才追加（症状 → 根因 → 绕法 → 日期 + 置信度）。跑某类活前若可能撞已知坑，先扫 `spark-pitfalls.md`。该文件是**个人库**、不进分享给实验室的公开库；够通用且验证充分的坑可手动晋级到 `env-notes.md` 再随公开库发出。

## 只是查状态？

用户只问"服务器现在什么情况" → `advise.py status`，人话汇总（GPU 空/忙、可调度 X G、队列 N 个），**不提交任何东西**。
