# LRZ：在 CoolMUC-4 跑大任务

> 什么时候读：用户点名 LRZ，或要将 Spark 上跑通的大批量 CPU 任务交给 LRZ，或工具只有 x86 构建。开通、首次连接或登录报错，读 [LRZ 开通与连接](lrz-first-login.md)。准备软件、数据库、存储和搬运，读 [LRZ 环境与数据](lrz-data.md)。

## 什么时候用

**只放大任务。速度优先，兼顾资源效率。** 核数和分区按能明显缩短总时长来定，给足工具能用上的核和内存。加核不再变快就停，小任务用共享分区。使用 LRZ 花课题组经费（按组里的安排）。

1. 先在 Spark 上用代表性输入跑通，按实测定核数、内存和 `--time`。
2. 比较全量所需的核数、内存、搬运和等待时间。选定 LRZ 后准备 x86_64 环境，先在 LRZ 小规模复核资源再跑全量。
3. **例外**：工具没有 ARM64 构建、Spark 上装不了时，任务仍须是大任务。能在 Spark 上验证的步骤先验证，向用户说明哪一步跑不了、为什么；是否按例外直接在 LRZ 小规模试跑，由用户在确认首次试跑时决定。试跑和全量仍各自单独确认。

- **GPU**：CoolMUC-4 没有 GPU。LRZ 的 GPU 要项目另外开通，本组暂未开通，GPU 任务用 Spark。
- **数据**：普通 LRZ HPC 环境不得存放或处理 DSGVO 第 9 条的特殊类别个人数据，例如遗传或健康数据。需要例外时，由项目负责人先通过机构与 LRZ 确认专门协议及所需的安全环境。

## 提交前：估算资源并确认

1. 从 Spark 小样本的实测用量出发：在 Spark 上用 `history` 或 `sacct -j <作业号> --format=JobID,Elapsed,MaxRSS,TotalCPU,AllocCPUS` 核对时长、内存峰值和 CPU 利用率，用作业步骤数据汇总，别加 `-X`。在 LRZ 上查自己的作业要加 `-M <集群>`。
2. 在 LRZ 用代表性输入复核（这也是在 LRZ 分配资源，先按第 5 步确认），包括最大或最复杂的样本，测加核和批量并发是否缩短总时长。比较单任务耗时、固定批次的吞吐，以及含排队和搬运的预计完成时间；收益超出测量和排队的不确定性、值得多占资源时才加。LRZ 的任务打包指南把约 70% 的并行效率当经验参考，不是硬阈值。打包或并发跑时，按同一节点上同时运行的任务估算节点内存峰值，不能只取单个任务的 MaxRSS。按结果设全量资源，给内存峰值和时限留合理余量。先确认输入、工作目录和日志目录已存在且权限正确。
3. 按下面的分区表选。少核批处理先用 `serial_std`，超过 24 小时且符合内存限额时选 `serial_long`。单节点高效并行选 `cm4_tiny`。单节点高内存作业超出普通分区限额时考虑 `teramem_inter`。只有能高效跨节点或用打包任务填满 2–4 个独占节点时，才考虑 `cm4_std`。
4. 串行和并行阶段需要不同资源时，分开定资源、确认和提交。同一 Slurm 集群内可用 `--dependency=afterok:<作业号>` 串接已确认的作业。
5. **每次提交前单独确认，小规模测试和全量各算一次；AI 发起的任何新 LRZ 资源分配（包括 `salloc` 交互作业）都要先确认。** 告诉用户任务会提交到 LRZ、会花课题组经费。列集群、分区、节点数、核数和程序线程数、内存（注明每节点或合计）、时限、预计核时及粗略等候时间。预计核时按预计分配的核数 × 小时数估算，作为资源效率的参照。整份计划的同意不能代替这一步。

## 每次使用：连接

本人先在 Spark 登录 CoolMUC-4，AI 在 Spark 复用这条已登录的 SSH 连接发送管理命令。配置见 [LRZ 开通与连接](lrz-first-login.md)。

- 先在 Spark 执行 `ssh -O check lrz` 检查复用连接。连接失效请本人重登。复用发命令时加 `-o BatchMode=yes`，避免非交互命令卡在密码提示。
- 密码与第二因子只由本人操作。`ControlPersist yes` 是客户端设置，LRZ 文档未提 CoolMUC 的服务端会话寿命。维护、断网或本人退出都可能断开连接。

## 红线（违反可能被封号）

- 登录节点做管理、搬运与必要编译（最多 2 个线程）。计算和工作流放进 Slurm。AI 编程工具只在 Spark 或本地运行，AI 的 LRZ 管理命令从 Spark 发出。人工 IDE 用下文的 OOD。
- LRZ 禁止高频轮询，官方以每 10 分钟查询为例。本组 Slurm 查询至少间隔 10 分钟，提交前估算也算；提交后查状态统一用 `spark-extlog lrz check`。
- 超过 100 个且每个不足 2 分钟的作业（含数组）要打包。若开作业邮件通知，用有效的邮箱。账号不得共用，LRZ 上禁用 `cron` 和 `at`。

## Slurm

| 分区 | `-M` | 节点/作业 | 物理核/作业 | 最长 | 每人运行（已提交）上限 | 内存 | 节点 |
|---|---|---|---|---|---|---|---|
| serial_std | serial | 1 | 1–16 | 24 h | 96（200） | 默认 6.2 GiB/逻辑 CPU，节点上限 1000 GiB | 共享 |
| serial_long | serial | 1 | 1–16 | 168 h | 96（200） | 默认同 serial_std，每人运行作业合计 100 GB | 共享 |
| cm4_tiny | cm4 | 1 | 17–112 | 24 h | 4（25） | 默认 2.1 GiB/逻辑 CPU，节点上限 488 GiB | 共享 |
| cm4_std | cm4 | 2–4 | 112–448 | 24 h | 2（25） | 每节点 488 GiB | 独占 |
| cm4_inter | inter | 1–4 | 1–112 | 8 h | 1（2） | 同 cm4_tiny | 共享 |
| teramem_inter | inter | 1 | 1–96 | 240 h | 1（1） | 约 60 GiB/物理核，节点约 6 TB | 共享 |

- 提交显式写集群 `-M` 和分区 `-p`，两项都省略时默认 `inter`。查询按需筛分区，`sacct` 用 `--partition` 或 `-r`；按 JobID 查询或取消时写明集群。当前分区以 `sinfo -M cm4,serial,inter -s` 为准。
- **核数口径**：表中核数为物理核。`--cpus-per-task` 算的是逻辑 CPU（超线程），默认开超线程时 `-c 32` 只占 16 个物理核；要每个线程独占一个物理核，加 `--threads-per-core=1`，这时 `-c 16` 就是 16 个物理核（LRZ 串行指南按每人 96 核上限给的示例：前者可同时跑 6 个，后者 3 个）。程序线程数设成 `$SLURM_CPUS_PER_TASK`。计算密集的工具先按物理核试跑，开超线程是否更快要实测。确认单里分别写请求的 CPU 数、程序线程数和实际物理核数。`serial` 全集群每人运行作业合计最多 96 个物理核。
- 共享节点按分配 CPU 给默认内存。按实测填 `--mem`（每节点），过大的内存请求可能连带分到更多核。表中节点内存上限和用户限额分开看。LRZ 政策另列每用户跨作业合计内存上限为单节点的 50%，完整适用分区文档未提（置信度中），接近上限时核对目标分区。
- `cm4_std` 需 `--qos=cm4_std`，`serial_long` 需 `--qos=cm4_serial_long`。`cm4_tiny` 是否必填 QoS，分区表、并行指南和速查表说法不一（置信度中），可先用 `sbatch --test-only` 看目标分区接不接受这个请求。
- 串行批处理指南写省略 CPU 资源时默认单核，交互指南写省略任务数时默认 2 tasks、时限默认 15 分钟。显式写资源。示例 `salloc -M inter -p cm4_inter -n 20 -t 00:30:00`。多节点 `cm4_inter` 的 CPU 限额细分口径文档未提（置信度中）。`teramem_inter` 也可提交批处理。
- 写明 `--time`，合适的时限有助于调度器 backfill。

批处理脚本开头，以 `serial_std` 为例。先准备工作目录和输出路径，按试跑结果替换尖括号。

```bash
#!/bin/bash
#SBATCH -J <作业名>
#SBATCH -D ./
#SBATCH -o ./%x.%j.%N.out
#SBATCH --get-user-env
#SBATCH --export=NONE
#SBATCH --clusters=serial
#SBATCH --partition=serial_std
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=<物理核数>
#SBATCH --threads-per-core=1        # 每个物理核只跑 1 个线程；去掉则两个线程共用一个物理核
#SBATCH --mem=<每节点内存，如 48G>
#SBATCH --time=<HH:MM:SS>
module load slurm_setup
# 显式加载作业所需的模块；工具线程数用 $SLURM_CPUS_PER_TASK
```

## 作业打包与工作流

- 大量短任务放进同一个 Slurm 作业。可用 `srun --exact … &` 配合 `wait`，或用 GNU Parallel 管并发。按每个任务的核数和内存控制同时运行数，每个样本留日志和退出状态，续跑补失败项。每个作业最多 `cm4` 40,000 steps、`serial` 1,000 steps。
- **计算节点不能调用 `sbatch`。** Nextflow 在外层 Slurm 作业内运行，单节点用 `local`。多节点确有收益时按 LRZ 试运行指南评估 `flux`（推荐 Nextflow 晚于 24.10.0）。Snakemake 先验证单节点本地执行，LRZ 文档未提专用配置。
- Nextflow/nf-core 固定验证过的 pipeline、profile 和镜像版本，设置每任务资源、`workDir` 和 `NXF_APPTAINER_CACHEDIR`（LRZ 经验报告里写成了 `NFX_`，是笔误）。为重跑保留工作状态，提前检查存储容量和回收期限。

## 看排队

- 看分区忙闲用 `sinfo -M cm4,serial,inter -s`。看本人排队原因和粗略开跑时间可用 `squeue -M cm4,serial,inter --me --start`，LRZ 只允许查看自己的作业。
- 提交前可用组内预检 `sbatch --test-only <脚本>`，只估算、不提交。预计时间可能缺失或变动，汇报给用户，由用户判断去向。
- 以上查询同样至少间隔 10 分钟。提交后按下节查状态。

## 提交后：记录与查看

第一次用 LRZ 或 Colab 时，按 [LRZ 开通与连接](lrz-first-login.md#使用记录每人一次) 的「使用记录」问一次本人是否同意记录。

- `sbatch` 成功后，马上写一条提交记录，数值和用户确认过的单子一致：

  ```
  spark-extlog lrz submit --job <作业号> --cluster <serial|cm4|inter> --partition <分区> \
      --cpus <请求的 CPU 数> --mem <内存> --time <时限> [--nodes <节点数>] \
      [--est-core-h <预计核时>] [--est-start <预计开始>] --name <作业名>
  ```

- 之后查作业状态，一律用 `spark-extlog lrz check`。它一次查清自己所有作业的状态，并把已经结束的作业补进记录；本人同意记录时，还会顺带记一次 LRZ 存储用量（最多每 6 小时一次），显示在面板磁盘卡的「LRZ 存储」标签上。不要另外用 `squeue`、`sacct` 反复查。
- 本人没同意记录时，这两个命令照样能用，只是不写记录。记录里的核时按 Slurm 的 `AllocCPUS`（逻辑 CPU）× 运行时长计，用来比较资源占用。
- 退出码：
  - 0 正常；
  - 2 参数错；
  - 3 LRZ 连接不在，请本人重新登录；
  - 4 记录文件缺失，请管理员补建；
  - 5 距上次查询不到 10 分钟，按提示的秒数再等。
- 这些命令都在 Spark 上运行：AI 在 Spark 上时直接运行；在自己电脑上时用 `ssh <spark> '<命令>'`。

## 失败排查

- 先看提交报错、作业终态、退出码和日志。作业刚启动就失败时，先核对工作目录和 stdout/stderr 路径是否存在、有写权限。
- `COMPLETED` 表示退出码为 0，再核对产物。`FAILED` 看非零退出与应用日志；`OUT_OF_MEMORY` 复核单节点内存与并发；`TIMEOUT` 复核时限或分批续跑；`NODE_FAIL` 核对是否节点故障。提交被拒先核对集群、分区、QoS 和限额。
- 仍需 LRZ 协助时，向 Linux Cluster Servicedesk 提供集群、分区、JobID、发生时间、提交脚本及文字报错。重交前单独确认。

## 交互式用 OOD

人工用 RStudio、Jupyter 或 Code Server，走 [CoolMUC OOD](https://ood.hpc.lrz.de)。选 CPU、内存、运行时长和环境，及时保存结果。Code Server 的终端在计算节点上，不能提交或管理 Slurm 作业。

## 来源

依据 2026-11-26 的 LRZ 文档快照。

- [Linux Cluster 分区和限额](https://doku.lrz.de/pages/viewpage.action?pageId=10745970)、[选资源](https://doku.lrz.de/pages/viewpage.action?pageId=11484181)、[运行政策](https://doku.lrz.de/pages/viewpage.action?pageId=1307289651)
- [串行脚本](https://doku.lrz.de/pages/viewpage.action?pageId=11484000)、[并行指南](https://doku.lrz.de/pages/viewpage.action?pageId=11484078)、[交互作业](https://doku.lrz.de/pages/viewpage.action?pageId=1303879201)、[速查表](https://doku.lrz.de/pages/viewpage.action?pageId=1646760668)
- [登录与联网](https://doku.lrz.de/pages/viewpage.action?pageId=10745974)、[首次开通与数据限制](https://doku.lrz.de/pages/viewpage.action?pageId=11484378)、[存储规定](https://doku.lrz.de/pages/viewpage.action?pageId=1668714889)
- [Linux Cluster 扩建](https://doku.lrz.de/pages/viewpage.action?pageId=10745971)、[AI Systems 权限](https://doku.lrz.de/pages/viewpage.action?pageId=10746642)
- [Job farming](https://doku.lrz.de/pages/viewpage.action?pageId=771233178)、[GNU Parallel](https://doku.lrz.de/pages/viewpage.action?pageId=10746427)、[Nextflow（试运行）](https://doku.lrz.de/pages/viewpage.action?pageId=788693597)、[nf-core 经验](https://doku.lrz.de/pages/viewpage.action?pageId=2238563906)、[Flux](https://doku.lrz.de/pages/viewpage.action?pageId=646524052)
- [Slurm 命令示例](https://doku.lrz.de/pages/viewpage.action?pageId=1894257007)、[故障工单写法](https://doku.lrz.de/pages/viewpage.action?pageId=10745908)、[Linux Cluster Servicedesk](https://doku.lrz.de/pages/viewpage.action?pageId=10746034)
- [CoolMUC OOD](https://doku.lrz.de/pages/viewpage.action?pageId=1646756765)、[Code Server 限制](https://doku.lrz.de/pages/viewpage.action?pageId=1643512554)
