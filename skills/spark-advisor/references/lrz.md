# LRZ：Spark 跑不动的大任务，放到 LRZ 集群（CoolMUC-4）

> 什么时候读：用户点名 LRZ；或者任务已经在 Spark 上跑通，但需要的核数、内存或排队时长超出了 Spark 的能力；或者工具只有 x86 构建。还没开通、第一次在某台机器上连接，或者登录报错时，先读 [LRZ 开通与连接](lrz-first-login.md)。
>
> 依据 LRZ 官方文档，2026-11-26 查阅；标「实测」的内容来自当天的登录检查。文档之间说法矛盾的地方逐条标出。之后实测结果与本文不符时，以实测为准，并更新本文。

## 先判断合不合适

**LRZ 要花课题组经费，只放大任务。** 上 LRZ 之前：

1. 先在 Spark 上用有代表性的小样本跑通，按实测定好核数、内存和 `--time`，不多申请。
2. 只有 Spark 跑不动的大批量任务才搬上去，例如核数、内存或排队时长超出 Spark 的能力。
3. 工具没有 ARM64 构建、Spark 上装不了时，在 LRZ 的 serial_std 上用小样本试跑，代替第 1 步。试跑通过后再跑全量。

另外：

- CoolMUC-4 是 x86_64。Spark 上建的 conda 环境和容器不能直接用，要在 LRZ 上重建。两边 CPU 不同，速度和内存峰值会有出入，跑全量之前先用一个样本核对资源设置。
- 没有 GPU，LRZ 的 GPU 集群预计 2027 年上线。GPU 任务留在 Spark。
- 不能处理人类受试者的个人数据（DSGVO 第 9 条规定的特殊类别，如遗传数据、健康数据），除非另签协议。人源样本的测序数据要先去除宿主 reads 再上传（推断，置信度中）。
- 集群经常很满（实测 2026-11-26 中午：cm4 的 100 个节点中 98 个在用，serial 分区全满），排队时间要算进去，不一定比 Spark 快。

## 提交前：估算资源并确认

1. 从 Spark 试跑作业的实际用量出发：运行时长、内存峰值、CPU 利用率。看 `sacct -j <作业号> --format=JobID,Elapsed,MaxRSS,TotalCPU,AllocCPUS`（按作业步骤汇总，不加 `-X`），或用 spark-advisor 的 `history`。
2. 按样本数或数据量推算全量需要的总时长、内存峰值和核数。多线程工具要先确认加核确实能变快，CPU 利用率低就不要多要核。
3. 按下面的分区表选：1–16 核用 serial_std，超过 24 小时用 serial_long；17–112 核、单节点用 cm4_tiny；内存超过单节点时用 teramem_inter。cm4_std 是 2–4 个整节点独占，最费钱，只有确实能跨节点并行的任务才用。
4. `--time` 按推算值留出余量（比如多 20%–30%），不要直接填分区上限。
5. 提交前把分区、核数、内存、`--time` 和预计核时（核数 × 小时）给用户看，用户同意再交。第一批只交一个样本或一小批，核对实际用量后再交其余部分。

## 每次使用：连接

每次登录都要动态码，AI 没法自己登录。做法是本人登录一次，之后的命令复用这条已登录的连接。推荐把这条连接建在 Spark 上：数据库和数据都在 Spark 和 NAS 上，从 Spark 推最快。只有数据在自己电脑上时，才在本机连。连接复用怎么配置，见 [LRZ 开通与连接](lrz-first-login.md)。

- AI 执行前先运行 `ssh -O check <host>`，它只检查本机，不连网。检查不通就请本人重新登录，**不要反复试连**，每次失败的登录 LRZ 都有记录。
- 命令一律加 `-o BatchMode=yes`，避免卡在密码提示上，例如 `ssh -o BatchMode=yes <host> 'squeue -M cm4 --me'`。
- 密码和动态码只由本人在终端里输入，不要让用户把它们发给 AI。
- 下面几种情况连接会断，需要本人重新登录：连续闲置超过 ControlPersist 设定的时长、本机重启或断网、LRZ 维护时重启登录节点。
- LRZ 文档没有提到连接复用，既没允许也没禁止。

## 红线（违反可能被封号）

- 登录节点只做管理操作：准备和提交作业、查看状态、编译（最多 2 个线程）、搬运数据。不要在上面跑长时间、占大量内存或并行的程序。
- **登录节点上禁止运行 AI 编程工具和远程 IDE（VS Code、Jupyter）**，基本的编辑除外。需要 Jupyter、RStudio 或 Code Server 时用 OOD 门户（http://ood.hpc.lrz.de/）。AI 只在本地或 Spark 上运行，通过 ssh 发送管理命令。不要在 LRZ 上安装或启动 Claude Code、Codex 一类工具。
- 查询 Slurm 最快每 10 分钟一次，例如 `watch -n 600 squeue -M cm4`。AI 等作业时的轮询也要守这条。
- 不要提交 100 个以上、每个不到 2 分钟的作业（作业数组也算在内），合并成一个作业。
- 作业邮件通知只能填真实有效的邮箱，填错会被撤销提交权限。
- 不能用 cron 或 at，账号不能与他人共用。

## Slurm

| 分区 | `-M` | 节点/作业 | 核/作业 | 最长 | 每人运行（排队）上限 | 内存 | 节点 |
|---|---|---|---|---|---|---|---|
| serial_std | serial | 1 | 1–16 | 24 h | 96（200） | 默认 6.2 GiB/逻辑核，最多 1000 GiB | 共享 |
| serial_long | serial | 1 | 1–16 | 168 h | 96（200） | 每人合计 100 GB | 共享 |
| cm4_tiny | cm4 | 1 | 17–112 | 24 h | 4（25） | 默认 2.1 GiB/逻辑核，最多 488 GiB | 共享 |
| cm4_std | cm4 | 2–4 | 112–448 | 24 h | 2（25） | 每节点 488 GiB | 独占 |
| cm4_inter | inter | 1–4 | 1–112 | 8 h | 1（2） | 同 cm4_tiny | 共享 |
| teramem_inter | inter | 1 | 1–96 | 240 h | 1（1） | 约 60 GiB/物理核（共 6 TB） | 共享 |

- 当前有哪些分区以 `sinfo --clusters=cm4,serial,inter -s` 的输出为准。2026-11-26 实测与上表一致，另外 inter 集群里还有一个 cm4_inter_ood 分区，供 OOD 门户使用。
- 每条 Slurm 命令都要带 `-M`（`--clusters`）和 `-p`。漏掉 `-M` 时，命令默认作用于 inter 集群。
- cm4_std 还要加 `--qos=cm4_std`，serial_long 还要加 `--qos=cm4_serial_long`。cm4_tiny 要不要加 qos，文档之间说法矛盾，以实测为准。
- 不写核数时只分到 1 个核。`--mem` 设得过大，会被多分配用不上的核，所以按实测填。
- 1–16 核的作业用 serial_std。LRZ 要求核数少的作业走 serial，cm4 分区留给并行效率高的作业。serial 上一个人所有运行中的作业合计最多 96 核。
- 每人所有作业合计用的内存不能超过单个节点内存的 50%（2026-07-01 起）。需要更多内存时用 teramem_inter。
- 交互作业：`salloc -M inter -p cm4_inter -n 20 -t 00:30:00`。不写 `-t` 的话默认只有 15 分钟。
- 计算节点不能提交作业，所以 Nextflow 或 Snakemake 不能在作业里再派作业。做法是提交一个大作业，在作业内用 local 执行器跑流程。
- 写明 `--time`，调度器可以用 backfill 让作业提前开始。
- 硬件：cm4 节点是 Sapphire Rapids，112 核，可用内存 488 GiB；serial 节点是 Ice Lake，80 核，1 TB 内存；teramem 节点 96 核，6 TB 内存。节点本地有 1.7 TB SSD。
- 查看优先级：`squeue -M cm4 --Format=jobid,priority,state,reason`；查看已完成作业：`sacct -M cm4 ...`。

作业脚本的开头按 LRZ 文档的写法（以 serial_std 为例，尖括号里的值按 Spark 实测填）：

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
#SBATCH --cpus-per-task=<核数>
#SBATCH --mem=<内存，如 48G>
#SBATCH --time=<HH:MM:SS>
module load slurm_setup
# 之后再加载需要的模块；模块不会自动加载
```

## 存储

| 位置 | 容量 | 备份 | 保留 |
|---|---|---|---|
| `$HOME` | 100 GB，不能扩 | 磁带备份，加上每晚快照（保留 7 天，在 `/dss/dsshome1/.snapshots/`） | 项目存续期间 |
| `$SCRATCH_DSS` | 很大，大家共用 | 无 | **14 天滑动删除**；用量到 80–90% 时会提前删 |
| DSS 项目存储 | 最多 10 TB 免费 | 无 | 要 master user 申请；用 `dssusrinfo all` 查看 |
| `$TMPDIR_LOCAL` | 1.7 TB，节点本地 | 无 | 作业结束就清空 |

- 查 HOME 用量和自己能访问的 DSS 容器：`dssusrinfo all`。2026-11-26 实测，课题组项目当时没有 DSS 容器，长期保存的数据、环境和数据库只能放进 HOME，要用 DSS 需要由 master user 申请。
- 大量输出不要写进 HOME，配额满了作业会写入失败、出错。
- 在 SCRATCH 解压用 `tar -mx`，因为保留旧时间戳的文件会被优先删除。不要产生海量小文件。

## 软件

- 登录后会默认加载 spack 软件栈。2026-11-26 实测默认是 24.4.0，登录提示里 LRZ 公告 2026-12-01 会上线新栈 26.0.0。以 `module list` 为准。
- conda：`module load miniforge3`。环境默认装进 HOME，很快会占满。Slurm 作业不会读 `~/.bashrc`：LRZ 建议把 conda 的初始化段放进单独的 `~/.conda_init`，`~/.bashrc` 和作业脚本里都只写 `source ~/.conda_init`。
- 自己编译的软件用 `module load user_spack`。容器可以用 Apptainer 或 Charliecloud，但不能挂载 sif 或 squashfs 文件。

## 数据搬运与联网

- 大批量数据推荐用 Globus，也可以在登录节点上用 scp 或 rsync，这是允许的。
- **LRZ 的出站 SSH（22 端口）被封**，不能从 LRZ 主动往外推或拉。传输一律从自己的机器或 Spark 发起。
- 能上网的地方：登录节点、cm4_inter、serial_std、serial_long。cm4_tiny、cm4_std 和 teramem_inter 默认不能上网，数据库和容器要先在 serial 作业里下好。

## 账号与数据期限

- 账号默认有效到项目结束。设了到期日的账号到期就会锁定，3 个月后删除。
- HOME 随 LRZ 项目到期一起删除；HPC 文件系统上的数据在项目结束一年后删除（2018 年的规定）。要留的数据在到期前搬走。
- 出口管制声明每 12 个月要重签，过期后登录不上。

## 来源

- 使用规则：https://doku.lrz.de/policies-on-the-linux-cluster-1307289651.html
- 分区与限额：https://doku.lrz.de/job-processing-on-the-linux-cluster-10745970.html
- 串行作业脚本：https://doku.lrz.de/running-serial-jobs-on-the-linux-cluster-11484000.html
- 登录与联网：https://doku.lrz.de/access-and-login-to-the-linux-cluster-10745974.html
- 硬件：https://doku.lrz.de/coolmuc-4-1082337877.html
- 存储：https://doku.lrz.de/file-systems-and-io-on-linux-cluster-10745972.html
- conda：https://doku.lrz.de/faq-conda-mamba-and-python-virtual-environment-on-lrz-hpc-clusters-41616204.html
- 速查表（2026-10-01 版）：https://doku.lrz.de/coolmuc-cheat-sheet-1646760668.html
