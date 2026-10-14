---
name: spark-advisor
description: 共享 GPU 服务器（Slurm/ARM64）顾问：先判任务该本地还是服务器跑，再定 --mem/--gres/--time 并提交，避免统一内存 OOM；要反复改、反复跑的调试循环开交互式会话（/spark-advisor debug）。也管公共软件和数据库（查有没有装、申请安装：software）；腾空间（cleanup）：智能清理和公共区重复的环境、把冷数据搬到 NAS 再搬回来、记个人搬运清单；服务器上的项目（project）：代码推了没、推到 Gitea；查明脚本用的数据库版本（versions）。触发：/spark-advisor、把作业交到 spark/服务器、这活本地还是服务器跑、要多少内存/显存、服务器忙不忙、该排队还是能跑、推荐 slurm 参数、在服务器上调试、开个调试会话、每跑一次都要重新排队、服务器上有没有装、申请装软件、缺数据库、重复安装、清理环境、磁盘不够了、服务器上腾空间、冷数据、搬到 NAS、从 NAS 搬回来、服务器上的代码推了没、服务器上用的数据库版本。
---

# spark-advisor — 共享 GPU 服务器顾问

> 在共享 GPU 服务器（Slurm/ARM64）上正确跑活、管好空间和代码。引擎 `scripts/advise.py`（纯标准库，封装好 SSH+slurm、只返回干净 JSON——**别自己现攒 ssh/slurm 命令**）。例外是只读的查看：读文件、`ls`、`du`、`grep`、`squeue` / `sacct`、git 的只读命令（`status` / `log` / `diff`，前面加 `GIT_OPTIONAL_LOCKS=0`，免得回写索引）可以直接 ssh，先看体量再读，别把大文件整个拉回来；会改东西的一律走引擎。

## ★ 先读服务器规则：`advise.py rules`（唯一权威来源）

出任何推荐、写 sbatch、查软件、清理或搬数据之前，先跑 `python scripts/advise.py rules`（只查、只推服务器上的代码不用）。它读服务器面板的 `/ai.md`：管理员维护的使用规则、共享软件（`/opt/bio/bin`）与共享数据库（`/opt/bio/db`）、sbatch 模板和**此刻**的空闲资源。

- 与本 skill 的 references **冲突时以 `/ai.md` 为准**；references 只提供背景和历史实测。
- 生成 sbatch 时**优先用共享软件和共享数据库**（如 `$CHECKVDB`），不要让用户自建环境或另下一份数据库。
- 一次会话读一次即可；要看最新的空闲资源用 `status`。

## ⛔ 红线与总闸（动手前必看）

- **GPU / 重活只走 Slurm**（`gen-sbatch → submit` 或调试会话），**绝不** `ssh <server> 'python xxx.py'` 裸跑，哪怕用户说"就跑一下"：裸跑不留痕、逃过 memguard、抢显存（详见 `references/server-facts.md`）。轻活（改代码、几秒的小脚本、`squeue`/`nvidia-smi`）可以直接跑；拿不准就走队列。
- **总闸：下面每一样都先把计划给用户看、用户明确同意才做。** 提交作业；取消作业；开调试会话；任何带 `--yes` 的命令（删环境、搬到 NAS、搬回、推代码、写 `.gitignore`）；改用户的脚本；改服务器上的 git 配置。「能删的都删」这类笼统的话不算同意，要逐项点名。
- **作业在跑但没进度，禁止直接 `scancel`**：对冻结的作业 scancel 会让它永久卡住、不还内存。先 `wait <id>` 或 `status` 判断；冻结的用 `cancel-frozen <id>`（先不带 `--yes` 看计划）：返回 0 = 退干净；30 = 请求发了但资源没确认释放（**不算成功**）；32 = 这个账号没权限，请用户找管理员。细节读 `references/frozen-jobs.md`。没冻结、确认是用户自己的作业，用户点头后可以直接 `scancel <id>`。
- **`wait` 返回码**：0 正常结束；10 跑完但退出码非零；**11 被冻结（别 scancel）**；12 / 13 被取消 / 失败；20 监控自己超时（作业状态未知）；21 / 255 查询失败（**别当成完成**）。作业从 `squeue` 消失不等于成功。
- **备份不是归档**：NAS 备份只能找回 1–2 个月内删掉的东西。要长期留，代码推 Gitea、数据搬进 NAS 归档（`references/storage.md`）。
- **动手前先读下面路由表指向的那个文件。**

## 路由：用户怎么说 → 做什么 → 先读什么

| 用户说 | 子命令 / 流程 | 先读 |
|---|---|---|
| 这活本地还是服务器跑 | 执行地判定 | `references/execution-strategy.md` |
| 帮我交到服务器、要多少内存 / 显存 | 默认：下面「提交作业」 | `references/recommend-rules.md` → `references/submit-flow.md` |
| 要盯着结果改代码、反复跑；开个调试会话 | `debug-start` / `debug-log` / `debug-list` / `debug-end` | `references/debug-session.md` |
| 作业不动了、卡住了、帮我 scancel | `wait` / `status` → `cancel-frozen` | `references/frozen-jobs.md` |
| 服务器忙不忙 | `status`：人话汇总 GPU 空/忙、可调度内存、空闲核、队列；`disk.hint` 有内容就转达 | — |
| 服务器上有没有装 X、缺数据库、申请安装 | `software find` / `request` / `vote` | `references/software.md` |
| 磁盘不够、服务器上腾空间 | 按顺序：`project check` → `cleanup scan` → `cleanup cold` | `references/storage.md` |
| 这几个环境和公共区重复，能删吗 | `cleanup scan` + 智能判断 | `references/cleanup.md` |
| 把 X 搬到 NAS、从 NAS 搬回来、我搬过什么 | `cleanup apply --action archive` / `restore` / `archived` | `references/cold-data.md` |
| 服务器上的代码推了没、推到 Gitea | `project check` / `push` / `init` | `references/code-check.md` |
| 这个项目还要不要留在服务器上 | `project check` → `cleanup scan`（项目专用的环境）→ 搬到 NAS | `references/storage.md` |
| 查明脚本里用的数据库是哪个版本 | `versions`（把 `current` 和数据库变量解析成实际版本） | `references/code-check.md` |
| 删掉的文件还能找回吗 | — | `references/storage.md` |
| 某个包能不能在 aarch64 装 | — | `references/env-notes.md` |

子命令只是让意图更确定，不是唯一入口：用户说人话时按上表判断即可。

## 提交作业（骨架；每步细节在 `references/submit-flow.md`）

0. **执行地判定**：GPU / 峰值内存 >19G / 带宽·BLAS 密集 → 服务器；sklearn 树模型等分支密集的 CPU 活 → 本地更快。判为本地就给本地建议，到此为止。判为服务器后再问一句：要盯着看、反复改吗？是 → `debug`。
1. **抽输入特征**（你做）：Boltz token、Uni-Dock 盒子 × 并行配体数；纯 CPU 用 `--mem-guess <GB>`。
2. **查现状 + 历史**：`advise.py recommend --tool <tool> [--gpu] [--mem-guess <GB>] [--cpus N]`。
3. **定参数**（读 `references/recommend-rules.md`）：`--mem` 用推荐值（CPU 作业必填）；`--cpus` 别漏传；`--time` 默认不传。人话讲清，**带置信度和依据**。
4. **判排队还是能跑**：看 `run_now` / `warnings`。
5. **生成脚本**：`gen-sbatch … --workdir <项目在服务器上的绝对路径>`。`--workdir` 别省；Git Bash 下带 `MSYS_NO_PATHCONV=1`；GPU 作业要处理显存自限；引擎回的 `warnings` 别忽略。
6. **提交前确认（总闸）** → 用户点头 → `submit --script …` → JobID。
7. **GPU 作业提交后**：后台跑 `advise.py wait <JobID>`，按返回码处理。提交前看 `status` 的 `gpu_physical`：Slurm 说空闲不等于卡上没人。

跑过一次后，用 `gpu-peak <JobID>` 收紧下一次的 `--mem`（`references/recommend-rules.md`「标定的正确姿势」）。

## 首次配置 / 环境感知

- `config.json`（gitignore）缺了才走：① `advise.py detect-server` 扫 tailnet 得 IP+名 → ② 问"你在服务器上的用户名？（通常是姓名拼音小写）" → ③ `advise.py init --user <名> --host <IP> --node <名>`。**绝不硬编码服务器地址**，IP 只落进 gitignore 的 `config.json`。
- 在服务器上跑程序前读 `env-profile.local.json`（缺了、超过 14 天、或刚装新环境就跑 `advise.py probe-env`）。**`command -v` 查不到不等于没装**：非交互 ssh 不初始化 conda、不加载公共区 PATH。R 是公共的，走 `/opt/bio/bin/Rscript`。判据 → `references/env-notes.md`。

## 个人资料

- 往 spark 传数据 / 百度网盘 → `references/data-transfer.md`（**你自己积累的**，不随本仓库分发、升级时不会被覆盖）
- 本机踩过的坑 → `references/spark-pitfalls.md`（**你自己积累的**，不随本仓库分发、升级时不会被覆盖）
- 目录约定、产物落在哪 → `references/workspace-layout.local.md`（**你自己积累的**，不随本仓库分发、升级时不会被覆盖）。要用服务器上的项目路径时（生成 sbatch、`project`、`cleanup`）先读；**文件不存在就问用户"这个项目在服务器上的目录是哪个"**，别自己编一个路径。
- 搬到 NAS 的个人清单 → `references/nas-archive.local.md`（`cleanup archived` 自动生成，备注在 `nas-archive.local.json`）
- 管理员：新用户开通 → `references/onboard-user.local.md`（**你自己积累的**，不随本仓库分发、升级时不会被覆盖）

## 踩坑即记（服务器相关坑，个人积累）

跑活时若撞到**和服务器（ARM / 统一内存 / slurm / 工具在本机的默认值）相关**的坑、且排出了根因或绕法 → **先问用户一句"要记进 `references/spark-pitfalls.md` 吗？下次不再踩"**，同意才追加（症状 → 根因 → 绕法 → 日期 + 置信度）。跑某类活前若可能撞已知坑，先扫 `spark-pitfalls.md`。该文件是**你自己的**，不在本仓库里、升级 skill 时也不会被覆盖；够通用且验证充分的坑，值得手动挪进 `env-notes.md`。
