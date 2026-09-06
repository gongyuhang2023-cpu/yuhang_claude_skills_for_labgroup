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
- **管理员：新用户开通全流程**（**你自己积累的**，不随本仓库分发、升级时不会被覆盖） → `references/onboard-user.local.md`（backrest 备份实例 / Tailscale 共享 / 密码保管 / 两个已付学费的坑。⚠️ 新用户的 `TasksMax` 会话封顶是**模板 drop-in 自动继承**的，注册流程里不用加步骤）

## ⛔ 红线：GPU / 重活只能走 Slurm，绝不 ssh 裸跑

**IMPORTANT — YOU MUST**：任何 **GPU 活 / 重活**只能经 `gen-sbatch → submit`（或 `srun` 调试）走队列，**绝不** `ssh <server> 'python xxx.py'` 裸跑——哪怕用户说"就跑一下"。裸跑 = 不留痕（`sacct` 记不到 → 用量/采购/公平调度全失真）+ 逃过 memguard 反连累守规矩的人 + 抢显存撞崩别人的卡 + 不受 fairshare（四条详见 `references/server-facts.md`）。**轻活**（改代码、跑几秒的小脚本、`squeue`/`nvidia-smi` 查看类）可直接跑；拿不准就走队列（留痕 + 受保护，无坏处）。

## 首次配置（仅当 config.json 缺失；有就跳过）

`config.json`（gitignore）存服务器连接信息。缺了才走：① `advise.py detect-server` 扫 tailnet 得 IP+名 → ② 问"你在服务器上的用户名？（通常是姓名拼音小写）" → ③ `advise.py init --user <名> --host <IP> --node <名>`。**绝不硬编码服务器地址**，IP 只落进 gitignore 的 `config.json`。

## 环境感知（在服务器上跑程序前）

读 `env-profile.local.json`（gitignore，缓存本机 CUDA/Python + 你的 venv/项目路径）决定 sbatch 要不要 `source xxx/activate`。缺了先 `advise.py probe-env` 生成；`probed_at` >14 天、某 env 命令失败、或刚装新环境就重跑。某包能不能在 aarch64 装 → `references/env-notes.md`。

⚠ **`command -v` 查不到不等于没装。** 非交互 ssh 不初始化 conda，所以 `conda` / `Rscript` 必然 not found —— 而 **R 恰恰只装在 conda env 里**（系统无 R）。以 profile 的 `system.conda` / `system.r` 字段为准，调用走绝对路径（`~/miniforge3/envs/<env>/bin/Rscript`）。判据与落点表 → `references/env-notes.md`。

## 核心流程

**0 · 执行地判定**（先做）——这活该本地跑还是上服务器？
- 快速判据：**GPU / 峰值内存 >19G / 带宽·BLAS 密集 → 服务器**；**sklearn 树模型（RF/GBM）等分支密集 CPU 活 → 本地更快**；单线程指纹类两边打平。
- 拿不准 / 要对新任务判 → 读 `references/execution-strategy.md`（带 benchmark 与判据）。
- **判为本地 → 给本地建议，到此为止、不碰服务器。** 判为服务器 → 往下走（并守红线：走队列）。
- **判为服务器后还要再分一次叉**：这活是**要盯着看、拿结果改代码、再跑**的调试循环？
  → 走 §「调试循环」用交互式会话，**别一轮一轮排队**。提交完就关电脑走人 → 继续下面的流程。

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

**跑过一次之后，用 `gpu-peak` 收紧下一次的 `--mem`**：

```
python scripts/advise.py gpu-peak <上次的 JobID>
```

它给三样东西：**观测下界**（采样，可能漏掉尖峰）、**当时的机器水位**、**Slurm 终态**，
并直接判定「这次的申报值算不算有效上界」。

判据：作业正常完成 **且** 期间水位到过 88%（查账线）→ 申报值是有效上界，
真值夹在 `[下界, 上界]` 里。**水位没到 88% 就不算** —— memguard 当时压根没查账，
"没被冻"只说明没人来查你，不说明你没超申报。细节与迭代收敛法见
`references/recommend-rules.md` §「标定的正确姿势」。

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
→ 用户点头 → `submit --script /tmp/job.sh` → 返回 JobID。

**6b · 提交后立刻起监控（GPU 作业必做）**

```
python scripts/advise.py wait <JobID>
```

**用后台方式跑它**，然后该干嘛干嘛。它正常运行期间一声不吭、不花 token；作业
**结束 / 失败 / 被冻结 / 被取消** 时立刻返回，你自然就收到了。

固定返回码（认这几个数即可，不必解析文本）：

| 码 | 含义 | 该做什么 |
|---|---|---|
| 0 | 正常结束 | 收结果 |
| 10 | 跑完但退出码非零 | 看日志 |
| **11** | **被冻结** | **别 scancel**，见下方 §「作业没进度」 |
| 12 / 13 | 被取消 / 失败 | 看原因 |
| 20 | 监控自身超时 | **不是**作业失败，状态未知 |
| 21 / 255 | 查询失败 / 连不上 | 状态未知，别当成完成 |

> ⚠️ **作业从 `squeue` 消失不等于成功** —— 可能是失败、被取消，或只是那次查询失败了。
> `wait` 已经处理了这个（终态一律以 `sacct` 为准，并给 accounting 留收敛时间）；
> 你自己写轮询的话别踩这个坑。

**7 · GPU 提交前看一眼卡上的物理真相**

`status` 现在同时给两个视角：`gpu_free` 是 **Slurm 的记账**，`gpu_physical` 才是
`nvidia-smi` 看到的真相。**没走队列的进程 Slurm 完全不知道**，它照旧报告"空闲"
（2026-08-27 实测如此）。看 `gpu_conflict_risk` 与 `warnings`，有冲突就别交。

`gpu_physical.state` 四档：`free` / `system-use`（只有桌面服务这类常驻服务，
用户计算意义上是空的）/ `slurm-job` / `unattributed`（**有人没走队列在用卡**）/
`unknown`（**查不到 ≠ 空闲**）。

⚠️ 这只保证如实展示，**挡不住所有撞卡**：检查完到作业真正被调度之间，别人可能
才开始裸跑，或排队作业才被派上。

## 调试循环 → 交互式会话，别一轮一轮排队

**判据一句话：要不要盯着看？**

| 问自己 | 走哪条 |
|---|---|
| 要**盯着结果改代码、然后再跑** | **交互式会话**（本节） |
| 提交完就关电脑走人 | **照常 sbatch**（上面的核心流程） |

**为什么值得单开一档**：调试是「跑 2 分钟 → 想 20 分钟 → 再跑」的循环。每轮走 sbatch
就要重排一次队 —— 2026-09-02 实测同一个只跑 **51 秒**的作业排了 **2h51m**；同一天
用户排了至少 4 次。会话里每次启动 **33–61ms**（2026-09-06 实测），排队只付一次。

原理：`salloc` 把**壳**和**内容**拆开 —— 壳（分配）按申请的时长活着，内容（每次
`srun`）想跑几次跑几次。跨 ssh 连接照样进得去，所以**用户不必自己开终端、不必挂 tmux**。

**四条命令**：

```
python scripts/advise.py debug-start [--time 6:00:00] [--cpus N] [--mem-gb N] [--gpu]
python scripts/advise.py debug-run --jobid <id> --command "<要跑的命令>"
python scripts/advise.py debug-list
python scripts/advise.py debug-end --jobid <id>
```

`debug-start` 拿不到资源时返回 `ok:false` + `server_now`。**别自动重试、别自动缩小**——
把现状告诉用户，让他选（缩小 / 去掉 GPU / 等正在跑的结束）。

### 你（AI）在这个模式里的四个职责

1. **开会话前问一句。** 它占着资源，跟提交作业一样不自动开。
2. **每次 `debug-run` 后看 `warn` 字段，非空就原样转达，别吞掉。** 它的含义是
   「剩余不足 1/4，现在就该去排下一个会话了」——因为时限不能延长，只能重开，
   而重开要重新排队。
3. **活干完主动提议 `debug-end`。** 会话不会自己早退，只会一直耗到时限。
4. **看到 `session_gone: true`** → 会话在那次运行期间没了，多半是撞时限被掐。
   **说清楚"这次的活没跑完且不会自动重跑"**，别让用户读成"程序自己失败了"。
5. **开完会话顺手挂一个后台计时器**，睡到剩 1/4 时自动退出 —— 它一退出会唤醒你，
   你就能**主动**去提醒用户，不必等他先开口。睡多久 = `session.time_limit_seconds` × 0.75
   （6 小时的会话 → `sleep 16200`）。用 Bash 工具的 `run_in_background` 跑：

   ```
   sleep <秒数>; echo "调试会话 <jobid> 剩余不足 1/4，该去排下一个会话了"
   ```

   ⚠️ **这一层是加分项，不是保障**：它只在当前 Claude Code 会话活着时有效，用户关掉窗口
   就没了。真正的保障是第 2 条那个 `warn` 字段（每次 `debug-run` 都带，跑不掉）
   和 `--time` 本身。**别因为挂了计时器就不转达 `warn`。**

### 三条硬事实（2026-09-06 实测，别凭直觉推翻）

| 事实 | 后果 |
|---|---|
| idleguard **豁免**交互式作业（`BatchFlag=0`，见 `idleguard.py:35`） | 好处：你想多久都不会被回收。代价：**忘了收工没有任何东西会来收** —— `--time` 是唯一那道网 |
| 时限**只能调小、不能调大**（调大直接 `Access/permission denied`） | 「先开短的、不够再延长」**走不通**，必须一开始就报够 |
| 到点**真掐**：SIGTERM（rc=143）、`DUE TO TIME LIMIT`、终态 TIMEOUT、**不重排** | 跑到一半的活直接没，只能重开会话从头来 |

⇒ 风险是**不对称**的：报少了修不了，报多了随时能 `debug-end` 释放。
所以默认 **6 小时** —— 够长到不会在干活途中被掐，又短到**不能拿会话当批处理使**
（正经长活该走 sbatch，那样能关电脑走人、还不占着这 6 小时）。
用户要更长**不拦**，但把上面第一条的代价说清楚。

### 别人看得见 —— 这是这套做法正当的前提

会话在 `squeue` 里就是**一个正常作业**，跟别人的作业同一张表、同样的列：

```
JOBID   USER      NAME            STATE    TIME     CPUS  MIN_MEMORY  TIME_LIMIT
  651  <user>    debug-<user>    RUNNING  1:23:40      2         8G     6:00:00
```

名字默认 `debug-<user>`，别人一眼知道是**有人在交互调试**、占多少、最多占到什么时候。
**别改成看不懂的名字** —— 不起名的话默认可能显示成 `bash`，会被当成卡死的僵尸作业。

面板（3001）分两种视角，**别搞混**：

| 视角 | 看得到调试会话吗 |
|---|---|
| **实时**「谁正占着机器」 | ❌ 面板**没有**这个视图（无 `/api/jobs`、无 `/api/queue`）。要看实时只有 `squeue` |
| **历史**按程序名累计 | ✅ 有。会话按 `debug-<user>` 累计进 `totals.programs`（含 wall_hours / cpu_core_hours / 次数） |

### ⚠️ `--gpu` 要额外克制 —— 面板会公开点名

`usage_stats.py:620` 有一条 `gpu_held_idle` 规则，同时满足这四条就把作业**列进面板的
findings 公开点名**：

1. 占着 GPU  2. 跑了 **>1 小时**  3. GPU 利用率 **P90 < 1%**  4. **同期有别人在排队等卡**

第 4 条是这条规则的良心 —— 没人等卡时占着不点名。但**带 `--gpu` 的长调试会话正好容易
四条全中**（你在想事情，卡是闲着的）。规则自带的建议原话就是：
「交互调试用完 exit 释放；长任务改用 sbatch 提交」。

⇒ **给用户的默认**：
- **调试循环每轮真的用到 GPU** → 带 `--gpu`，利用率不会是 0，不会中招
- **只是偶尔用一下 GPU** → **别带 `--gpu`**，要用的时候单独提一个小作业
- 带 GPU 的会话**把时长压短**（1–2 小时），别用 6 小时默认值
- 实证：一个**只为占位、并不计算**的作业已经被这条规则点名过 ——
  「占用 GPU 1.0 小时，利用率 P90=0.0%，同期有其他 GPU 作业在排队」。
  想靠占位作业卡住队头的做法**从来不是隐形的**，面板会自己把它列出来。

## ⛔ 作业"在跑但没进度" —— 禁止直接 scancel

**IMPORTANT — YOU MUST**：看到作业状态是运行中、日志却半天不动时，**不要直接
`scancel`**。先判断属于哪一种，五种里只有一种以"取消"为正确处置：

| 现象 | 可能是 | 正确处置 |
|---|---|---|
| 状态 `PD` | 还在排队没轮到 | 等 |
| 状态 `R`，日志不动 | **被 memguard 冻结** | **`cancel-frozen`，绝不 `scancel`** |
| 状态 `R`，日志不动 | 程序自己卡死 / 等 IO | 可以取消 |
| 状态 `R`，日志不动 | 输出缓冲还没刷出来 | 正常，继续等 |
| 状态 `CG` 很久 | 清理卡住了 | 查是不是被冻着 |

**怎么分辨**：`advise.py wait <id>` 返回 11 就是被冻了；或看 `status` 的
`gpu_physical.frozen_jobs`。

**为什么这条是硬规则**：对一个**已冻结**的作业 `scancel`，信号送不进去（冻结的
进程处理不了信号），作业会永远卡在 `COMPLETING`、内存一直不还、`squeue` 里赖着
不走。2026-08-27 就是这么把整台机器堵了三小时、全组作业全部排不进去。

**被冻了怎么办**（顺序不能换）：

```
python scripts/advise.py cancel-frozen <id>          # 先看它要做什么（dry-run）
python scripts/advise.py cancel-frozen <id> --yes    # 确认后执行
```

它按「解冻 → 取消 → 四项确认」走完，返回 0 才算真的退干净；返回 30
（`cleanup-stuck`）表示请求发出去了但**资源没确认释放** —— 那正是事故当天的状态，
别当成成功。

**然后改申报重交。** 注意：**不要拿任何观测到的数字当作它的真实需求** —— 作业是在
越线那一刻被打断的，观测值只是"被打断时用到哪"，真实峰值只会更高，照着填还会再被冻。
要测真值，在程序里加显存监测、用**小规模完整跑完**一次；且该数字只对当前这个输入
有效，换蛋白或换规模就得重测。

## 踩坑即记（服务器相关坑，个人积累）

跑活时若撞到**和服务器（ARM / 统一内存 / slurm / 工具在本机的默认值）相关**的坑、且排出了根因或绕法 → **先问用户一句"要记进 `references/spark-pitfalls.md` 吗？下次不再踩"**，同意才追加（症状 → 根因 → 绕法 → 日期 + 置信度）。跑某类活前若可能撞已知坑，先扫 `spark-pitfalls.md`。该文件是**你自己的**，不在本仓库里、升级 skill 时也不会被覆盖；够通用且验证充分的坑，值得手动挪进 `env-notes.md`。

## 只是查状态？

用户只问"服务器现在什么情况" → `advise.py status`，人话汇总（GPU 空/忙、可调度 X G、CPU `cpu_free`/`cpu_total` 核空闲、队列 N 个），**不提交任何东西**。
