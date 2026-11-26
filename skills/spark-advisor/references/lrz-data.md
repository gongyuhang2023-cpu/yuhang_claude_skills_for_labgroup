# LRZ 环境、存储和数据搬运

> 什么时候读：要在 CoolMUC-4 装工具、准备数据库、搬入数据或回传结果。资源、提交和查作业读 [LRZ 作业](lrz.md)。

## 环境

**在 CoolMUC-4 为每套项目环境装一次，试跑和全量复用。** CoolMUC-4 是 x86_64。Spark 的 ARM64 二进制、Conda 环境和容器镜像不能直接用，按配方重建或准备 x86_64 版本。

- 先用 `module list`、`module avail stack` 查软件栈。固定已验证的 stack、模块、工具及镜像版本，批处理脚本显式加载 `slurm_setup` 和所需模块。在目标分区验证。
- 小环境和配置可放 `$HOME`。项目环境和缓存放自己的 `~/dss`，容器镜像放 `~/dss-shared/containers/`（见下文「存储」）；还没有 DSS 权限时在 `$SCRATCH` 放可重建的工作副本，配方和版本记录留在 Spark。
- **和 Spark 保持同一套版本，用 pixi（2026-11-26 实测通过）。** Spark 的项目环境由 pixi 管理，锁文件只锁了 `linux-aarch64`。**不要**在 Spark 项目的 `pixi.toml` 里直接加 `linux-64`：锁文件一变，Spark 上的环境指纹也会变，可能触发重建。做法：
  1. 在项目里另放一份 LRZ 用的配置，例如 `lrz/pixi.toml`，写 `platforms = ["linux-64"]`。依赖照抄 Spark 那份，版本钉成 Spark `pixi.lock` 里实际解析出的版本（`==x.y.z`）。只用公共频道；Spark 的本地频道 `file:///opt/bio/channel` 在 LRZ 上不存在，依赖它的包要换来源。
  2. 在 Spark 上运行 `pixi lock` 求解（x86 平台也能在 Spark 上解，不用安装），把 `lrz/pixi.toml` 和 `lrz/pixi.lock` 提交进项目仓库。
  3. 用 Spark 上已登录的连接 `scp` 到 LRZ 的项目目录，放在 `~/dss/<项目>/`；还没有 DSS 权限时放 SCRATCH。
  4. pixi 装在本人 LRZ 的 HOME 里（约 77 MB），版本和 Spark 一致（用 `pixi --version` 查）：`curl -fsSL https://pixi.sh/install.sh | PIXI_VERSION=v<版本> PIXI_NO_PATH_UPDATE=1 bash`。
  5. 安装：`PIXI_CACHE_DIR=~/dss/.cache/pixi ~/.pixi/bin/pixi install --frozen`（没有 DSS 权限时缓存放 SCRATCH）。`--frozen` 严格按锁文件安装、不重新求解。缓存不要放 HOME。
  6. 作业里用 `~/.pixi/bin/pixi run --manifest-path <项目>/lrz/pixi.toml <命令>` 运行。

  实测：seqkit 2.14.0 加 samtools 1.25 的锁文件在 Spark 上求解，在 LRZ 登录节点 5.5 秒装好，环境 22 MB、缓存 141 MB，两个工具都能运行（LRZ 的 glibc 为 2.38）。环境放在 SCRATCH 上，14 天后可能被删；删了就用同一份锁文件重装，有 DSS 权限后改放 DSS。只下载解压的安装属于登录节点上允许的准备工作，LRZ 的 conda 文档也是在登录节点上建环境；不要在登录节点上编译大软件（最多 2 个线程）。
- 不需要和 Spark 对齐版本的环境，也可以用 LRZ 自带的 Conda：`module load miniforge3`，也可用 Mamba。把 Conda 初始化段放 `~/.conda_init`，在 `~/.bashrc`（或 `~/.profile`）和作业脚本里按需写 `source ~/.conda_init`，再激活所需环境。Slurm 作业不会自动读取 `~/.bashrc`。
- 自建软件和模块可用 `module load user_spack`。Apptainer/Singularity 和 Charliecloud 不能直接挂载 SIF 或 squashfs；需要时提前解包成 sandbox，放在作业可访问的共享存储，避免每个任务重新解包。
- 批量 R 用 `Rscript`。固定 `r` 模块及 R 包环境，系统依赖先查环境模块。人工 RStudio 走 [CoolMUC OOD](lrz.md#交互式用-ood)。

## 存储

| 位置 | 容量 | 备份 | 用途与期限 |
|---|---|---|---|
| `$HOME` | 100 GB，配额固定 | 磁带备份，夜间快照最多保留 7 天 | 代码、配置、小环境。运维页列至 LRZ 项目到期 |
| 组里的 DSS：`~/dss`（私人）、`~/dss-shared`（共享） | 每人最多 1 TB；整个容器 5 TB 全组共用 | 无 | 私人目录放自己的项目数据、环境和结果；共享目录放组里共用的数据库、容器镜像等。可用期随 data project |
| `$SCRATCH` / `$SCRATCH_DSS` | 共享空间 | 无 | 输入暂存、批处理中间文件和待回传结果。按文件时间约 14 天滚动回收，高水位可能提前删 |
| `$TMPDIR_LOCAL` | 每节点约 1.7 TB，共享节点上也共享 | 无 | 节点本地高速暂存，数据仅在作业期间可用 |

- 登录后用 `dssusrinfo all` 查 HOME 用量、可访问的容器和配额，确认路径属于本项目，再设置 `WORK` 或 `PROJECT`。HOME 快照在 `/dss/dsshome1/.snapshots/`。
- **组里的 DSS（2026-11-26 开通）**：HOME 里的两个链接 `~/dss`（指向本人的私人目录）和 `~/dss-shared`（共享目录，现有 `db/` 数据库、`containers/` 容器镜像、`bin/`、`shell-pack/`）。每人上限 1 TB，但整个容器只有 5 TB、全组共用（开通时已用约 3 TB），放大数据前先用 `dssusrinfo all` 看容器余量。DSS 没有备份，要长期保留的结果仍要回传 Spark 或 NAS。
- 新成员要请 master user（DSS 的 data curator）把账号加进容器授权，重新登录后才生效；授权前 `dssusrinfo all` 列不出容器，HOME 里也没有这两个链接，这不代表组里没有 DSS。
- 项目确实没有容器时，由 master user 从 Linux Cluster Servicedesk 申请，核对 IDM 联系邮箱并按邮件完成激活。基础档最多 10 TB，无额外存储费用。扩容或备份由 master user 向 LRZ 确认。
- SCRATCH 和 DSS 尽量用较大的文件，避免大量小文件。大量输出写 SCRATCH，避免填满 HOME 导致写入失败。SCRATCH 高水位通常在 80–90% 触发，阈值可能变动。

## 大数据库放哪

- **放共享目录**：组里共用的数据库放 `~/dss-shared/db/`，容器镜像放 `~/dss-shared/containers/`，全组共用一份。放之前先看有没有同版本，按名称和版本分子目录。共享区的东西不要随手删改，动之前先问管理的人。只自己用的放 `~/dss`。
- **暂时没有 DSS 权限**：每轮从 Spark 把所需版本推到 SCRATCH，作业内拷到用库节点的 `$TMPDIR_LOCAL`，每节点准备一次。事先核对节点共享盘的可用空间和拷贝时间。原库留在 Spark，SCRATCH 只留当轮工作副本，不靠 `touch` 续期。

## 数据进出与回传

1. 先确认数据不属于 DSGVO 第 9 条的特殊类别个人数据（见 [LRZ 作业](lrz.md#什么时候用)），普通的传输同意不能替代机构协议。再列数据与镜像清单、容量、LRZ 落点和回传路径，按 [同意规则](../SKILL.md#同意规则) 确认数据传输范围。
2. 从 Spark 用 `scp` 或 `rsync` 推输入到 LRZ。CoolMUC-4 到外部 SSH 的 22 端口被封；登录节点可在已挂载的 DSS 与 SCRATCH 间做必要的复制。
3. 离线分区开跑前备齐输入、索引、镜像和软件依赖。登录节点、`cm4_inter`、`serial_std`、`serial_long` 可联网；`cm4_tiny`、`cm4_std`、`teramem_inter` 默认无互联网。解包旧归档时可用 `tar -mx`，避免旧修改时间使文件很快被回收。开跑前核对大小或校验值。
4. 中间结果写 SCRATCH，从节点临时盘产生的产物要在作业退出前搬出；要在 LRZ 上继续用的放 `~/dss`。要长期保留的结果从 Spark 拉回 Spark 或 NAS 并核对（DSS 没有备份）；在 SCRATCH 上的要在 14 天内拉回，文件时间和空间压力都可能让实际期限更短。
5. 大批量数据可用 Globus。登录时机构选 Leibniz Rechenzentrum，使用 Linux Cluster ID，选 **Leibniz Supercomputing Centre's DSS - CILogon** collection。可访问自己有权限的 HOME、SCRATCH、DSS。

## 账号与数据期限

- 个人账号默认有效到项目结束。若设提前到期日，到期锁定，三个月后自动删除。出口管制声明每 12 个月重签。
- **项目或账号到期前回传并核对要保留的数据。** CoolMUC 存储页把 HOME 可用期列至 LRZ 项目到期，DSS 列至 data project 到期。通用指引写 HOME/WORK 在项目结束一年后删除。到期后是否能访问及实际删除日需向 LRZ 核对。

## 论文致谢

在项目记录里注明实际用过的 LRZ 资源。论文使用 CoolMUC 或 DSS 时，从 [Acknowledgment of Cluster and DSS use](https://doku.lrz.de/pages/viewpage.action?pageId=11484381) 选模板，得到 LRZ 技术支持时用包含 support 的版本。

## 来源

依据 2026-11-26 的 LRZ 文档快照。

- [CoolMUC 存储与 Globus](https://doku.lrz.de/pages/viewpage.action?pageId=10745972)、[登录与联网](https://doku.lrz.de/pages/viewpage.action?pageId=10745974)、[HOME 配额](https://doku.lrz.de/pages/viewpage.action?pageId=10747244)
- [通用数据期限](https://doku.lrz.de/pages/viewpage.action?pageId=1668714889)、[账号提前到期](https://doku.lrz.de/pages/viewpage.action?pageId=10747273)、[登录排查与出口管制](https://doku.lrz.de/pages/viewpage.action?pageId=3184661020)
- [Spack 软件栈](https://doku.lrz.de/pages/viewpage.action?pageId=11482330)、[Conda/Mamba](https://doku.lrz.de/pages/viewpage.action?pageId=41616204)、[R](https://doku.lrz.de/pages/viewpage.action?pageId=10746490)
- [Apptainer/Singularity](https://doku.lrz.de/pages/viewpage.action?pageId=400558542)、[Charliecloud](https://doku.lrz.de/pages/viewpage.action?pageId=11482422)
