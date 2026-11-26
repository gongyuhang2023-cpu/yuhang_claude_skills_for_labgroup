---
name: spark-advisor
description: 为生信和数据分析选择执行资源。当分析依赖专用工具或共享数据库、本机算力受限、数据搬运成本较高，或需要管理 Spark 作业与 NAS 存储时，主动核对可用能力、说明取舍，并在已确认的范围内执行。也处理卡住的作业、交互调试、数据长期保存（个人保留区）与腾空间，并在合适时评估 Colab 或 LRZ 集群（课题组付费，只放大任务），以及引导第一次开通和连接。
---

# spark-advisor

## 算力资源：什么情况用什么

| 资源 | 什么情况用 | 怎么用 | 第一次连接 |
|---|---|---|---|
| Spark | 默认执行地。课题工具、公共数据库和 GPU 都在这里 | 本文下面各节 | [环境说明](references/env-notes.md) 的「首次配置与探测」 |
| Colab | Spark 排不上、任务不大，并且数据可以传到 Google | [Colab](references/colab.md) | [Colab 首次登录](references/colab-first-login.md) |
| LRZ（CoolMUC-4，CPU） | Spark 跑不动的大批量 CPU 任务。**花课题组经费**，先在 Spark 跑通、定好资源，速度优先、避免浪费（工具只有 x86 构建时的例外见 LRZ 作业） | [LRZ 作业](references/lrz.md)、[环境与数据](references/lrz-data.md) | [LRZ 开通与连接](references/lrz-first-login.md) |

CoolMUC-4 没有 GPU。LRZ 的 GPU 要项目另外开通，本组暂未开通，GPU 任务用 Spark。

本机就能完成的小任务直接在本机做。拿不准放在哪里时，按 [执行地判定](references/execution-strategy.md) 比较。

## 先读本次会话的服务器规则

确定任务涉及 Spark 后，在推荐资源、写 sbatch、查工具或库、清理和搬运前，先运行 `python scripts/advise.py rules`。输出是服务器的 `/ai.md`，是工具专门安排、作业和数据规则的权威来源。只查或只推送服务器代码时可跳过。空闲资源变化时按需用 `status` 或 `recommend` 刷新。

## 能力清单

- **Slurm 算力与 GPU** — `status` 看队列、资源和物理 GPU，`recommend` 按任务和历史估资源，`wait` 看作业状态。
- **课题工具** — `software recipes` 看推荐配方目录，`software find` 核对工具的版本、依赖和适用构建，`env status --dir` 查项目环境。
- **公共数据库** — `database find` 查库的版本、格式、存放位置和兼容记录，`database check` 核对项目数据库锁。
- **NAS 与保留区** — `status` 看 `nas.online`、`nas.note` 和 `disk.hint`；`keep status` 看个人保留区（NAS 上有额度的长期存储）的用量和核对提示；`cleanup cold` 找冷数据，`cleanup scan` 查空间和环境副本。
- **Colab** — Spark 上的 `colab-free usage` 查授权与额度，`spark-colab` 管理云上作业。适用条件和数据去向见 [Colab](references/colab.md)。

## 主动判断用什么

1. 规划分析步骤时，先看任务目标、输入和所需产出，判断是否需要专门工具、公共库、较大算力或搬运数据。若只需查看本地小文件、解释现成结果，或已确认在本地完成，直接做本地任务。执行地有疑问时读 [执行地判定](references/execution-strategy.md)。
2. 从分析步骤提出候选工具。工具名不确定时看 `rules` 或 `software recipes`，再用 `software find` 核对配方。分析需要参考库或配方绑定数据库时，用 `database find` 核对库的内容、版本和格式是否符合该步骤。
3. 检查当前项目是否已有版本相符且可复用的环境。只有配方或构建时，说明还需验证依赖和构建条件。主动讲清做法、收益和代价，例如复用项目锁与库版本、构建等待、排队、搬运或外传。在已有授权范围内能继续的就执行。
4. 替用户把原始数据传上 Spark 之后，提一句可以存一份进保留区、服务器上那份照用，只在这一刻提一次。项目刚建、分析进行中不提；整理存储等用户自己调 cleanup（[存储](references/storage.md#整理一次)）。

## 同意规则

- 在已有访问权限内，可直接做与任务有关的轻量只读查询。提交或取消作业、开调试会话、构建环境、取数落盘、改用户脚本或服务器 Git 配置、数据外传（含 Colab）以及任何 `--yes` 操作，须有用户明确授权。可逐步确认，也可一次确认整份计划。用户要求逐步确认时，按用户的要求执行。
- **LRZ 要花课题组经费**：每次往 LRZ 提交作业（小规模测试和全量各算一次）之前都要单独确认，明确告诉用户这个任务会提交到 LRZ、会花课题组经费，并列出分区、核数、内存、时长和预计核时。对整份计划的同意不能代替这一步。
- **外部算力使用记录**：第一次用 LRZ 或 Colab 时，按 [LRZ 开通与连接](references/lrz-first-login.md#使用记录每人一次) 的「使用记录」问一次本人是否同意记录到组里的面板，答过就不再问。
- 计划逐步列清要做什么、写到哪里、改哪些文件（附 diff 或拟改内容）、提交哪些作业（资源和预计时长）。删除、覆盖、移动逐项列出确切路径；只有这一份的（保留区、NAS 和公共库里都没有）标「只有这一份」，能重新生成的注明，不另外再问。外传列出数据和接收方。涉及他人资源时，先核实处置权限。
- 依赖前一步的结果，如锁内容、环境指纹或作业号，写明「以前一步的实际结果为准」和允许的版本、路径或资源范围。结果在范围内时，可继续执行已获同意的后续步骤。
- 获得授权后才加 `--yes` 等执行参数。按预演逐步执行并核对。失败、结果不符或需要计划外操作时，停下说明并重新确认。照常使用 `plan_sha256`、`--expect` 等预演凭据，并遵守 `.no-yes` 等测试闸。宿主客户端放行命令不等于用户同意此次资源与数据安排。

## 运行红线

- **GPU 和重活走 Slurm**。提交前必读 [资源推荐规则](references/recommend-rules.md) 和 [提交与监控流程](references/submit-flow.md)，核对统一内存预算、物理 GPU 占用和工具显存自限。`--mem` 管不住设备显存，机制见 [服务器说明](references/server-facts.md)。
- 提交后用 `wait` 监控，按终态核对。作业卡住时先用 `wait` 或 `status` 判明是否冻结，再读 [冻结作业](references/frozen-jobs.md) 处置。冻结作业禁止直接 `scancel`。
- **LRZ 登录节点只做管理**：不在上面跑计算，不在上面运行 AI 编程工具，查 Slurm 最快 10 分钟一次。违反可能被封号，细则见 [LRZ](references/lrz.md) 的「红线」。

## 按任务找文档

| 任务 | 读什么 |
|---|---|
| 用工具或查看文件 | [软件配方与轻量查看](references/software.md)；要锁定、构建、取回或收编环境时读 [项目环境](references/project-envs.md) |
| 用公共数据库 | 用 `rg` 或 `grep` 在 [数据库](references/database.md) 中定位 `database find`、`取进本地缓存`、`锁版本`、`自己装进` 或 `个人副本`，只读相关段落 |
| 选执行地、推荐资源与提交 | 先看上面的「算力资源」表，拿不准时读 [执行地](references/execution-strategy.md)；选定 Spark 后按上面的运行红线阅读提交文档 |
| 反复调试或排查卡住的作业 | [交互调试](references/debug-session.md) 或 [冻结作业](references/frozen-jobs.md) |
| 整理存储、腾空间（含面板复制的 cleanup 提示词）、找回误删文件 | [存储和备份](references/storage.md) 的「整理一次」；环境和数据库副本读 [清理](references/cleanup.md) |
| 存进、查看、取回、腾本地、删除保留区里的数据，挪旧归档 | [保留区](references/cold-storage.md) |
| Colab 执行或授权 | [Colab](references/colab.md)；首次登录或授权过期读 [登录](references/colab-first-login.md) |
| LRZ 开通、连接或提交 | 开通、首次连接或登录报错读 [LRZ 开通与连接](references/lrz-first-login.md)；提交读 [LRZ 作业](references/lrz.md)；环境、存储和搬运读 [LRZ 环境与数据](references/lrz-data.md) |
| 服务器上的项目 | [代码状态、Git 初始化与推送](references/code-check.md) |
| 工具箱迁移（过渡期） | [扫描、迁移或冻结](references/toolbox-migration.md)，含验证用作业与验证后释放资源 |

## 首次配置、个人资料与踩坑即记

缺少连接配置或要探测 ARM64 环境时，读 [环境说明](references/env-notes.md) 的对应小节。项目路径以用户确认的目录或个人目录约定为准。
