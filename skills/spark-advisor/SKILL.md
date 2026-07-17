---
name: spark-advisor
description: Slurm GPU 服务器作业顾问。查服务器现状+历史，为要提交的作业推荐正确的 --mem/--gres/--time 参数并可选直接提交，避免统一内存 OOM 或空占资源。触发：“把任务/作业交到 spark/服务器”、“这个任务要多少内存/显存”、“服务器现在什么情况/忙不忙”、“该排队还是能跑”、“推荐 slurm 参数”，或准备用 sbatch/srun 提交 GPU 作业时。
---

# spark-advisor — Slurm 作业参数顾问

> 帮用户（或调用你的 AI 工作流）在向共享 GPU 服务器提交作业前，把 `--mem/--gres/--time` 定对，避免统一内存 OOM 或空占资源。
> 引擎在 `scripts/advise.py`（纯标准库；把 SSH 和 slurm 命令封装好、只返回干净 JSON——**别自己现攒 ssh/slurm 命令**）。
> **推荐的判断规则在 `references/recommend-rules.md`，推荐前先读它。** 机器的硬事实在 `references/server-facts.md`。

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

**⑤ 提交前确认门（强制）**：
> **IMPORTANT — YOU MUST**：生成 sbatch 后，**先把推荐参数（资源/时间）给用户看，问一句"要现在提交吗？"，用户明确同意后才提交。** 提交 GPU 作业消耗贵资源，绝不自动提交。
- 生成：`advise.py gen-sbatch --name <n> --gres <g> --mem-gb <m> --time <t> --command "<cmd>" --out /tmp/job.sh`
- 用户点头后：`advise.py submit --script /tmp/job.sh` → 返回 JobID
- 提交后告诉用户：JobID + "可以关机走人，`squeue` 看进度；`sacct -j <id>` 看用量"

## 只是查状态？
用户只问"服务器现在什么情况" → `advise.py status`，用人话汇总（GPU 空/忙、可调度 X G、队列 N 个），**不提交任何东西**。

## 诚实纪律（照 references/recommend-rules.md 执行）
- 没历史就标**置信度低**，别装准；主动提议先跑标定采样。
- GPU 显存历史目前 `sacct` 抓不到、尚未追踪 → GPU 侧靠"宁可多报 + 标定"，**别声称有 GPU 历史**。
- 估错有 cgroup 兜底（只崩用户自己的作业、可重交）——可据此安心给推荐，但仍要诚实标不确定。
