---
name: spark-advisor
description: 共享 GPU 服务器（Slurm/ARM64）顾问：先判任务该本地还是服务器跑，再定 --mem/--gres/--time 并提交，避免统一内存 OOM；要反复改、反复跑的调试循环开交互式会话（/spark-advisor debug）。也管项目工具（software find 查推荐配方，env 锁进项目，software scan 扫描脚本里的工具箱调用）；公共数据库（database）：查有哪些版本、自己把库装进 NAS 公共库、NAS 上的库取到本地缓存、给项目锁定数据库版本（databases.lock.json）、整理 home 里的数据库（tidy：找出所有副本，比对后删掉或放进公共库）；腾空间（cleanup）：核对项目环境和数据库副本、把冷数据搬到 NAS 再搬回来、记个人搬运清单；服务器上的项目（project）：代码推了没、推到 Gitea；spark 排不上时把小任务放到 Google Colab 免费算力（colab）。触发：/spark-advisor、把作业交到 spark/服务器、这活本地还是服务器跑、要多少内存/显存、服务器忙不忙、该排队还是能跑、推荐 slurm 参数、在服务器上调试、开个调试会话、每跑一次都要重新排队、服务器上有没有装、缺工具、装个软件、pip 装不上、缺数据库、自己装数据库、下载数据库到公共区、NAS 上的数据库、把库取到本地、数据库版本、锁定数据库版本、脚本用的哪个版本的库、我自己下的库放进公共区、整理我 home 里的数据库、重复安装、清理环境、磁盘不够了、服务器上腾空间、冷数据、搬到 NAS、从 NAS 搬回来、服务器上的代码推了没、服务器上用的数据库版本、Colab、用 Google 云跑、免费 GPU、spark 排不上、绑定 Google 账号、绑定云盘、个人云盘。
---

# spark-advisor — 共享 GPU 服务器顾问

> 在共享 GPU 服务器（Slurm/ARM64）上正确跑活、管好空间和代码。引擎 `scripts/advise.py` 用标准库封装 SSH 和 Slurm，默认返回 JSON。`rules` 原样返回 `/ai.md` 的 Markdown。读文件、`ls`、`du`、`grep`、`squeue`、`sacct` 和 git 的只读命令可在有直连权限的机器上用 SSH 查看。查看 git 时加 `GIT_OPTIONAL_LOCKS=0`，先看文件体量再读。写操作走引擎，`spark-colab` 用服务器封装的公共命令。

## ★ 先读服务器规则：`advise.py rules`（唯一权威来源）

出推荐、写 sbatch、查询软件或数据库、清理或搬数据之前，先跑 `python scripts/advise.py rules`。仅查询服务器代码状态或仅推送服务器代码时，可以跳过这一步。它读服务器面板的 `/ai.md`，其中有管理员维护的使用规则、软件的专门安排、共享数据库（`/opt/bio/db`）、sbatch 模板和**此刻**的空闲资源。

- 与本 skill 的 references **冲突时以 `/ai.md` 为准**；references 只提供背景和历史实测。
- 把课题工具锁进项目的 `pixi.lock`，用 `env-store` 构建，生成作业用 `gen-sbatch --env`。临时查看优先用当前项目环境，项目外用自己的 scratch 项目。公共 R 可直接用。`/opt/spark-sys` 专供服务器服务使用。数据库优先用共享版本，不要另下一份数据库。数据库写带版本号的路径（`$CHECKVDB` 这类默认变量指向 current，会变），见 `references/database.md`。
- 一次会话读一次即可；要看最新的空闲资源用 `status`。

## ⛔ 红线与总闸（动手前必看）

### 同意规则

本节适用于 SKILL.md 和 references 中所有需要用户同意的步骤。服务器规则（`rules`）优先。提交或取消作业、开调试会话、用 Colab 跑任务（数据会传给 Google）、任何带 `--yes` 的操作、改用户脚本和改服务器的 Git 配置，都须事先得到用户明确同意。

1. 可以把多步写成一份完整计划，一次请用户明确同意，也可以逐步请用户同意。用户要求逐步确认时，按用户的要求执行。只说「帮我清理」或「能删的都删」时，先列清具体计划再请用户同意。
2. 逐步写清要做什么、写到哪里、改哪些文件（给 diff 或拟改内容）、提交哪些作业（列资源和预计时长），以及要删什么。删除、覆盖和移动等不可逆操作逐项列出确切路径。需要 `plan_sha256`、`--expect` 或其他预演凭据的命令，照旧绑定到对应的预演结果。
3. 依赖前一步才能确定的内容，例如锁文件内容、环境指纹或作业号，写明「以前一步的实际结果为准」，同时说明允许的版本、路径或资源范围。结果在范围内时，整份计划的同意覆盖这些后续步骤。
4. 用户明确同意后，按计划逐步执行并逐步核对。`--yes` 等真执行参数在用户同意相应单步或整份计划后才加，测试仍受 `.no-yes` 等机器闸约束。任一步出错、结果与计划不符（例如版本、路径或释放空间不同），或需要计划外操作时，停下说明情况并重新请用户确认。

### 运行红线

- **GPU / 重活只走 Slurm**（`gen-sbatch → submit` 或调试会话），**绝不** `ssh <server> 'python xxx.py'` 裸跑，哪怕用户说"就跑一下"：裸跑不留痕、逃过 memguard、抢显存（详见 `references/server-facts.md`）。轻活（改代码、几秒的小脚本、`squeue`/`nvidia-smi`）可以直接跑；拿不准就走队列。
- 把用户数据放进共享盘 `/mnt/gdrive/share` 前，先征得用户同意。数据会传到 Google，Spark 上的用户都能改名或删除共享盘中的文件。
- **作业在跑但没进度，禁止直接 `scancel`**：对冻结的作业 scancel 会让它永久卡住、不还内存。先 `wait <id>` 或 `status` 判断；冻结的用 `cancel-frozen <id>`（先不带 `--yes` 看计划）：返回 0 = 退干净；30 = 请求发了但资源没确认释放（**不算成功**）；31 = 这条命令不适用（没有这个作业、已结束、在排队、没冻结或暂时判断不了），照 `note` 做；32 = 这个账号没权限，请用户找管理员；33 = 只检查了、没执行（没带 `--yes`）。细节读 `references/frozen-jobs.md`。没冻结、确认是用户自己的作业，用户点头后可以直接 `scancel <id>`。
- **`wait` 返回码**：0 正常结束；10 跑完但退出码非零；**11 被冻结（别 scancel）**；12 / 13 被取消 / 失败；20 监控自己超时（作业状态未知）；21 / 255 查询失败（**别当成完成**）；22 没有这个作业号（多半写错了）。作业从 `squeue` 消失不等于成功。调用时带 `--max-wait`（按预计时长给）：不带就一直等，程序自己卡死、没被冻结的作业会把它挂到作业时限。
- **备份不是归档**：NAS 备份只能找回 1–2 个月内删掉的东西。要长期留，代码推 Gitea、数据搬进 NAS 归档（`references/storage.md`）。
- **动手前先读下面路由表指向的那个文件。**

## 路由：用户怎么说 → 做什么 → 先读什么

| 用户说 | 子命令 / 流程 | 先读 |
|---|---|---|
| 这活本地还是服务器跑 | 执行地判定 | `references/execution-strategy.md` |
| 帮我交到服务器、要多少内存 / 显存 | 默认：下面「提交作业」 | `references/recommend-rules.md` → `references/submit-flow.md` |
| 想自己调低作业优先级：不急、可以让别人先跑（用户自己说的） | 「提交作业」第 5 步加 `--low-priority` | `references/submit-flow.md`「降低优先级」 |
| 要盯着结果改代码、反复跑；开个调试会话 | `debug-start` / `debug-log` / `debug-list` / `debug-end` | `references/debug-session.md` |
| 作业不动了、卡住了、帮我 scancel | `wait` / `status` → `cancel-frozen` | `references/frozen-jobs.md` |
| 服务器忙不忙、NAS 通不通 | `status` 汇总 GPU、内存、空闲核、队列和 `nas.online`。`nas.note` 和 `disk.hint` 有内容就转达 | `references/database.md` |
| spark 排不上、用 Colab 跑、用 Google 云跑 | `spark-colab`（在 spark 上运行） | `references/colab.md` |
| 绑定 Google 账号、绑定云盘、Colab 授权过期 | `spark-colab login` | `references/colab-first-login.md` |
| 用某个工具做分析、服务器上有没有装某个工具、缺工具、装个软件、pip 装不上 | `rules` → `software find` → 按配方锁进项目 → `env build` → `gen-sbatch --env` | `references/software.md` → `references/project-envs.md` |
| 看公共库目录或小文件 | 已知目录直接做只读检查，库或版本不清时用 `database find` | `references/software.md` |
| 看一眼某个文件、临时试个工具 | 优先使用当前项目环境，项目外用自己的 scratch 项目 | `references/software.md` |
| 迁移工具箱、扫描脚本 | `software scan <路径>…` → 核对近期作业 → 按项目迁移 | `references/toolbox-migration.md` |
| 有没有某个数据库、有哪些版本、缺数据库、自己装一个库、申请装库、请管理员装库 | `rules` → `database find` → `plan` → `install` | `references/database.md` |
| 同时需要工具和数据库，例如 RGI 加 CARD | 先读 `rules`，分别用 `software find` 和 `database find` 查工具和库，并核对版本与格式 | `references/software.md` 和 `references/database.md` |
| 要用 NAS 上的数据库、把库取到本地 | `gen-sbatch --stage` / `db-cache` | `references/database.md` |
| 磁盘不够、服务器上腾空间 | 按顺序：`project check` → `cleanup scan` → `cleanup cold` | `references/storage.md` |
| 项目环境有重复副本，能删吗 | `cleanup scan` + 智能判断 | `references/cleanup.md` |
| 把 X 搬到 NAS、从 NAS 搬回来、我搬过什么 | `cleanup apply --action archive` / `restore` / `archived` | `references/cold-data.md` |
| 服务器上的代码推了没、推到 Gitea | `project check` / `push` / `init` | `references/code-check.md` |
| 这个项目还要不要留在服务器上 | `project check` → `cleanup scan`（项目专用的环境）→ 搬到 NAS | `references/storage.md` |
| 脚本用的是哪个版本的库、锁定数据库版本 | `database scan` / `check` / `lock` / `pin` | `references/database.md` |
| 整理 home 里的数据库；我自己下了一份库：能不能放进公共区、能不能删 | `database tidy` → `compare` → `adopt` / `cleanup apply --db` | `references/database.md` |
| 删掉的文件还能找回吗 | — | `references/storage.md` |
| 正式分析要复现、项目环境、pixi 锁、环境构建或取回 | `env init/build/status/check` → `gen-sbatch --env` → `submit` | `references/project-envs.md` |
| 已有 pixi.lock 的项目建进 env-store、清掉项目里重复的环境 | `env adopt --dir <项目> [-e ENV]` → 改作业脚本 → 验证一次 → `cleanup` | `references/project-envs.md`「收编已有项目」 |
| 某个包能不能在 aarch64 装 | — | `references/env-notes.md` |

子命令只是让意图更确定，不是唯一入口：用户说人话时按上表判断即可。

## 提交作业（骨架；每步细节在 `references/submit-flow.md`）

0. **执行地判定**：**默认服务器** —— 本地机的内存和 CPU 被日常进程占着，留在本地要有正面理由。GPU / 峰值内存 >19G / 带宽·BLAS 密集 / 会跑超过几分钟 → 服务器；sklearn 树模型等分支密集的 CPU 活**且数据小、几分钟内完** → 本地更快。判为本地就给本地建议，到此为止。判为服务器后再问一句：要盯着看、反复改吗？是 → `debug`。
1. **抽输入特征**（你做）：Boltz token、Uni-Dock 盒子 × 并行配体数；纯 CPU 用 `--mem-guess <GB>`。
2. **查现状 + 历史**：`advise.py recommend --tool <tool> [--gpu] [--mem-guess <GB>] [--cpus N]`。
3. **定参数**（读 `references/recommend-rules.md`）：`--mem` 用推荐值（CPU 作业必填）；`--cpus` 别漏传；`--time` 默认不传。人话讲清，**带置信度和依据**。
4. **判排队还是能跑**：看 `run_now` / `warnings`。排不上而任务不大时读 `references/colab.md`，合适就主动提一句可以放 Colab。
5. **生成脚本**：`gen-sbatch … --workdir <项目在服务器上的绝对路径>`。课题工具先按配方锁进项目，构建就绪后加 `--env <项目目录>`。Git Bash 下带 `MSYS_NO_PATHCONV=1`；GPU 作业要处理显存自限；引擎回的 `warnings` 别忽略。用户主动要求调低优先级（如说不急、可以让别人先跑）时才加 `--low-priority`，默认不加，也别按任务类型替他判断。
6. **提交前核对**：检查脚本和资源符合用户已同意的单步或整份计划，再用 `submit --script …` 取 JobID。
7. **GPU 作业提交后**：后台跑 `advise.py wait <JobID> --max-wait <秒>`，按返回码处理。提交前看 `gpu_physical`（`recommend` 的 `server_now` 里就有，刚跑过就不用再跑 `status`）：Slurm 说空闲不等于卡上没人。

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

跑活时若撞到**和服务器（ARM / 统一内存 / slurm / 工具在本机的默认值）相关**的坑、且排出了根因或绕法 → **先问用户一句"要记进 `references/spark-pitfalls.md` 吗？下次可直接查到"**，同意才追加（症状 → 根因 → 绕法 → 日期 + 置信度）。跑某类活前若可能撞已知坑，先扫 `spark-pitfalls.md`。该文件是**你自己的**，不在本仓库里、升级 skill 时也不会被覆盖；够通用且验证充分的坑，值得手动挪进 `env-notes.md`。
