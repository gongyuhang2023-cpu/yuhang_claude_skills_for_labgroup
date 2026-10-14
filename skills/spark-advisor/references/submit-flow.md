# 提交作业：完整流程与细节

> 什么时候读：要把活交到服务器（生成 sbatch、提交、提交后监控）时。SKILL.md 里只有骨架，这里是每一步的细节、判据和实测数字。


**0 · 执行地判定**（先做）——这活该本地跑还是上服务器？
- 快速判据：**GPU / 峰值内存 >19G / 带宽·BLAS 密集 → 服务器**；**sklearn 树模型（RF/GBM）等分支密集 CPU 活 → 本地更快**；单线程指纹类两边打平。
- 拿不准 / 要对新任务判 → 读 `references/execution-strategy.md`（带 benchmark 与判据）。
- **判为本地 → 给本地建议，到此为止、不碰服务器。** 判为服务器 → 往下走（并守红线：走队列）。
- **判为服务器后还要再分一次叉**：这活是**要盯着看、拿结果改代码、再跑**的调试循环？
  → 走交互式会话（`debug-session.md`），**别一轮一轮排队**。提交完就关电脑走人 → 继续下面的流程。

**1 · 抽输入特征**（你做）→ 决定规模：Boltz token ≈ 蛋白残基 + 配体原子；Uni-Dock 盒子 × 并行配体数；纯 CPU 用 `--mem-guess <GB>`。

**2 · 查现状 + 历史 + baseline**（引擎一条命令）：
```
python scripts/advise.py recommend --tool <tool> [--gpu] [--mem-guess <GB>] [--cpus N]
```
→ 返回 `recommendation` + `server_now` + `history_summary`。
用户没说是什么工具就不传 `--tool`。这时 `history_summary` 混着这个账号的所有作业，GPU 作业别拿它当依据
（MaxRSS 本来就不含显存）；用户有同类作业的旧作业号，就用 `gpu-peak <作业号>` 拿实测下界。

**3 · 定三个参数、出推荐**（依 `references/recommend-rules.md`）——人话讲清，**务必带置信度 + 依据**：

| 参数 | 默认动作 | 为什么 |
|---|---|---|
| `--mem` | 用 `recommendation.mem_gb`；GPU 侧宁可多报；**没测过就别写**（走 96G 兜底跑一趟）——但可调度内存不到 96G 时不写就得排队，先看 `run_now`。`rules` 里写了这个工具要多少内存（如 GTDB-Tk `classify_wf` 80–100 GB）就直接用那个数，别用无历史时的 16G 默认，也别再当 `--mem-guess` 加 50% | 它管不住显存，是被 memguard 对账的**申报**——报低 = 排在第一个被冻。CPU 作业**必填**，不写会被 lua 直接拒收 |
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
  引擎已会拦下并提示，但不如一开始就带上。本机的 `--out` / `--script` 路径（`/c/...`、`/tmp/...`）引擎会自己还原，不受这个变量影响。
- **和 `/ai.md` 的模板是什么关系**：`/ai.md` 让大家从 `/opt/bio/share/templates/` 的模板起步写 sbatch；
  `gen-sbatch` 生成的是同一个结构（`#!/bin/bash -l`、`#SBATCH` 资源行、`logs/` 日志、出错就停）。
  结构上只差一处：模板写 `set -euo pipefail`，`gen-sbatch` 写 `set -eo pipefail`，不带 `-u`——
  命令里常有 `conda activate`，它的激活脚本在 `-u` 下会报 unbound variable。
  模板里的资源数值（`-c 8`、`--mem=48G`、`-t 1-00:00:00`）只是示例。用户手里已经有从模板改出来的脚本，
  可以直接 `submit`，不必重新生成，但提交前照样核对资源行：`--mem`、`--cpus` 照 `recommend` 改；
  `--time` 照本 skill 的时间策略，默认不写，估短了会被 TIMEOUT，整段白跑。
- **GPU 作业必查显存自限**：`--mem` 管不住显存（`cudaMalloc` 绕过 cgroup），只有程序自己能限。支持的工具（vLLM `--gpu-memory-utilization`）生成时直接带上；不支持的（Boltz）**明说拦不住**，靠控制输入规模 + 让程序结尾自己打印峰值。写法与机制 → `references/recommend-rules.md`「GPU 显存自限」。
- 引擎会回 `warnings`（漏 `--cpus` / 无显存自限 / Boltz `num_workers` 死锁）——**别忽略**。

**6 · 提交前确认门（强制）**：**IMPORTANT — YOU MUST** 先把资源给用户看、问一句"要现在提交吗？"、明确同意才提交。**绝不自动提交 GPU 作业。**
→ 用户点头 → `submit --script /tmp/job.sh` → 返回 JobID。

**6b · 提交后立刻起监控（GPU 作业必做）**

```
python scripts/advise.py wait <JobID> --max-wait <秒>
```

**用后台方式跑它**，然后该干嘛干嘛。`--max-wait` 按预计时长给：不带就是不限时，
程序自己卡死、没被冻结的作业会让它一直挂到作业时限。它正常运行期间一声不吭、不花 token；作业
**结束 / 失败 / 被冻结 / 被取消** 时立刻返回，你自然就收到了。

固定返回码（认这几个数即可，不必解析文本）：

| 码 | 含义 | 该做什么 |
|---|---|---|
| 0 | 正常结束 | 收结果 |
| 10 | 跑完但退出码非零 | 看日志 |
| **11** | **被冻结** | **别 scancel**，见 `frozen-jobs.md` |
| 12 / 13 | 被取消 / 失败 | 看原因 |
| 20 | 监控自身超时 | **不是**作业失败，状态未知 |
| 21 / 255 | 查询失败 / 连不上 | 状态未知，别当成完成 |
| 22 | 没有这个作业号 | 多半是号写错了，用 `squeue -u $USER` 或 `history` 核对 |

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
