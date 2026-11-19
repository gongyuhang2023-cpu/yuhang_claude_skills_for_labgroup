# 清理重复的项目环境和数据库副本

> 什么时候读：用户要腾空间、查项目环境或数据库是否有重复副本时。

项目环境以当前 `pixi.lock` 在 env-store 解析出的 fp 为准。核对项目环境是否和该存档对应，检查引用和可重建性后再清理。服务器自用工具目录由管理员按服务管理。

数据库整理先用 `database tidy`（见 `database.md` 第 6 节）。清单以外的副本用 `scan --path` 和 `apply --db <公共库名>`。

可按项目把迁移、验证及后续归档或删除列进同一份计划。一项一项写清脚本 diff、删除或覆盖的确切路径、搬运的源和目标、预计释放空间及相关作业资源与时长。使用原有预演，按同意的计划逐项核对和执行。[同意规则](../SKILL.md#同意规则)

清理逻辑在服务器命令 `spark-cleanup` 里。它的特点：
- 以用户自己的身份运行，只动他自己的 home。
- 默认只看不动。

本 skill 只是把命令转过去：

    python scripts/advise.py cleanup scan [--path P …]                       # 逐项检查，不改任何东西
    python scripts/advise.py cleanup refs P                                   # 只看哪里用到了它
    python scripts/advise.py cleanup apply P --action remove                  # 预演删除
    python scripts/advise.py cleanup apply P --action remove|link --db <库名>  # 每日清单以外的数据库副本
    python scripts/advise.py cleanup apply P --action archive --env           # 预演：先原样打包到 NAS 再删
    python scripts/advise.py cleanup apply P --action … --yes                 # 真执行

环境路径用 scan 结果里的 `path`；它属于哪个项目，看 `created_from` 和 `refs` 指向的位置。要找 scan 结果以外的项目目录时照 SKILL.md：先读 `references/workspace-layout.local.md`，没有这个文件就问用户，别自己编。
- 在 Windows 的 Git Bash 里直接传绝对路径也可以，被 Git Bash 改写的路径会自动还原；统一带 `MSYS_NO_PATHCONV=1` 最省事（`gen-sbatch` 的 `--workdir` 不会自动还原）。
- 也可以传带引号的 `'~/…'`。

**两种删法，选哪种：**
- `--action remove`：就地删，删前只存一份几 KB 的重建配方（history、`@EXPLICIT` 精确包清单、pip 包清单）。靠配方能重建环境，但环境里另外放的数据救不回来；NAS 备份也不备份 conda 环境。照配方或项目的 `environment.yml` 重建时，去掉 `r-base` 和 `r-*`：服务器不许用 conda 装 R，用公共的 R。
- `--action archive --env`（只限 conda 环境）：先存配方，再把环境**原样**打包到 NAS、逐个文件核对，然后才删本地。出了问题 `cleanup restore P` 原样搬回，原路径不变，`conda run` / `conda activate` 照常能用。提交成 Slurm 作业，几 GB 的环境一般几分钟。**希望原样重建的旧环境优先用这种。**

## 清理已核对的数据库子集

用 `--subset` 处理单个文件、夹有个人文件的数据库目录，或者和公共版本只在归档、校验文件、`ON_NAS` 提示上有差异的副本。**先迁移实际调用并验证，确认没有运行或排队的作业会读取旧路径，再清理。** 仅在 NAS 上的文件，在 Slurm 作业里用 `F=$(db-cache use /opt/bio/db/<库名>/<版本>/<文件路径>)`，工具读取 `"$F"`。打包版本先用 `db-cache use` 取出整个包，再访问解包目录中的成员。

单文件的在用检查能看到可访问进程已打开的文件，以及部分命令行、作业脚本和工作目录。作业脚本可能用 `$CACHE/METAVR.dmnd` 这样的变量拼接路径，或等到后续步骤才打开文件，需人工对照运行和排队作业。

子集模式直接调用 Spark 上的 `spark-cleanup`。当前 `advise.py cleanup apply` 只透传整份副本的参数。

```bash
# 在 Spark 上执行。大文件的预演和执行都放进 Slurm 作业。
spark-cleanup apply "$HOME/my-metavr/METAVR.dmnd" --action remove --db metavr/2026-08 --subset --json
spark-cleanup apply "$HOME/my-kofam" --action remove --db kofam/2026-08-02 --subset --json
# 用户核对预演并同意后，复制它返回的 plan_sha256。
EXPECT='<预演返回的 plan_sha256>'
spark-cleanup apply "$HOME/my-kofam" --action remove --db kofam/2026-08-02 --subset --json --yes --expect "$EXPECT"
```

- 子集必须写 `--db <库名>/<版本>`。先看 `matched`、`kept` 和 `frees`，用户同意后用原参数加 `--yes --expect <plan_sha256>`。执行前重新核对，摘要不同须重新预演。整份数据库模式也可用 `--expect` 绑定预演。预演不写说明、日志或锁。
- 目录按整份模式的层级对齐，只核对同一相对路径上大小及完整 SHA256 都一致的文件。同路径文件有大小或 SHA256 差异时，预演会在 `conflicts` 列出这些文件并拒绝整次清理。检查差异，需要保留的私人变体先移开再重跑预演。明确输入单个文件时，可以跨目录匹配同名文件。0 字节文件及缺少完整 SHA256 的文件无法匹配。
- 软链接须在对齐后的公共目录里有相同的相对目标文本，且在副本内解析到本次会处理的文件或整棵子树。公共链接读不到时保留。若某个保留链接会因处理而悬空，计划列出 `dangling_links` 并拒绝执行。
- 公共版本只有归档包、没有展开目录时，副本里的内部软链接核对不上。目录清理只检查根目录内的链接，单文件只检查直接父目录里的链接。清理前用 `spark-cleanup refs P` 查脚本和配置，再检查从副本外项目目录指进来的软链接。
- 清理器按副本路径最具体的挂载记录判断，位于 `fuse.*`、`cifs`、`smb3`、`nfs` 或 `nfs4` 挂载下的副本会被拒绝。普通本地挂载下的副本可按原流程核对。
- `remove` 只处理匹配项，并清掉因此变空的目录。`link` 把匹配项逐个换成指向固定公共版本的软链接，供旧路径兼容；包内成员须先迁移脚本再用 `remove`。迁移会删除或改写私人缓存的脚本时，把这些操作留在自己的工作目录。
- 副本根计划保留时，先在根里写 `.PUBLIC_DB.txt`；单文件或计划剪枝到根时，写在原路径的父目录。重名时另存一份。单文件另存到 `~/.spark-cleanup/notes/<回执号>.txt`。说明先记录计划，处理后追加最终状态。逐文件的 SHA256、设备号、inode 和 mtime 可在 JSON 与 `~/.spark-cleanup/log.tsv` 中按 `receipt` 核对。释放量按已分配磁盘块估算，`nlink > 1` 按零字节计。
- 整份模式中断时，先在 `~/.spark-cleanup/log.tsv` 用原路径和时间找到对应回执号，再按第六列回执号看这一组记录。`whole-plan` 是原计划，`whole-rename` 的 `step` 记录临时目录 `*.spark-cleanup-deleting-*`，`whole-rmtree` 和 `whole-symlink` 分别表示删除和建链接已完成。没有 `whole-ok` 时，以最后一条已完成步骤为线索，核对原路径、临时目录和公共链接；删除可能只完成了一部分。
- 人读输出遇到非 UTF-8 文件名时可能报编码错误。AI 用 `--json` 读取，文件名中的字节会以转义形式输出。

## 怎么读 scan 的结果

- **范围**：用 `scan --path` 查某个项目环境。每日候选名单可作为找路径的线索；环境是否可清理要靠项目锁和已构建环境确认。数据库副本用公共库版本和校验值核对。
- **新鲜度**：候选名单来自每日扫描（`inventory_generated`）。空间、引用和占用状态按本次扫描结果。
- **环境**：用 `env status --dir <项目>` 取得当前 fp 和状态。项目内的 pixi 环境按 [收编已有项目](project-envs.md#收编已有项目) 核对。旧 conda 环境先记录精确包版本及用途，已验证的项目环境接替后再考虑清理。
- **空间**：报给用户的空间用下面两个数，别用 `total`。其余部分和用户别的环境共用，删了也不会释放。
  - `bytes.private`：删了**立刻**腾出的空间。
  - `bytes.after_clean`：清缓存后**还能再**腾出的空间（估算）。删完会清整个包缓存，实际腾出的往往更多。
  - `bytes.listed` 是每日清单里记的大小，`scan --path` 查清单以外的环境时没有这一项。
  - 几项一起删时，把各项相加是保守估计：它们彼此共用的包，一起删后缓存里可能多腾出一些。
- **`refs`**：脚本、配置、crontab、Jupyter kernel 里用到它的地方（文件:行）。`refs_complete` 为 false，表示 home 太大没查完。
  - 只认按环境名激活和绝对路径（含 `~/`、`$HOME/` 写法）。**相对路径和用变量拼的路径查不到**，比如 `./_data/envs/<名>`、`$D/_data/envs/<名>`。
  - 所以环境在项目目录里（如 `<项目>/_data/envs/<名>`，`type` 是 `conda-prefix`，引擎会附一句 `refs_note`）时，`refs` 为空不算数，要自己在那个项目里只读搜一遍，再问用户项目还用不用它：
    - 搜 `envs/<名>`、`activate <名>`、`-n <名>`，别搜光秃秃的环境名（`bio` 会命中 biopython）；
    - 加 `--exclude-dir=_data --exclude-dir=.git`（环境自己就在 `_data` 里），用 `--include` 只看脚本和配置：`.sh .bash .py .R .qmd .Rmd .smk .sbatch .slurm .nf .yml .yaml`（文档 `.md` 里的命中只作参考）。
- **`in_use`**：正在用它的进程和作业（包括脚本里按环境名激活它的排队作业）。`other_jobs` 是用户另外还有几个作业在排队或在跑。
- **`created_from`**：环境是用什么命令建的。从项目的 `environment.yml` 建出来的，多半是项目环境。
- **`suggest.action`**：结合本次核对的项目锁、占用和引用判断是否可以归档、删除或保留。软链接方案适用于已核对的数据库副本。

## 将旧环境迁入项目或归档

1. 看 `refs` 和 `created_from`，核对脚本调用的命令、导入的库及项目。等占用旧环境的排队和运行作业结束后再处理。
2. 仍在用的环境先保留精确版本清单。按 `software.md` 把实际用到的依赖锁进项目，保持数据库版本，沿用已有分析的固定版本。出现兼容问题时保留旧环境。
3. 将 diff 和环境中数据文件的具体放置位置都写进计划。经同意后切换到项目环境。试跑一个代表性作业，核对结果及引用。
4. 已结束的项目或完成迁移的旧环境可用 `cleanup apply P --action archive --env` 预演原样归档。`plan.refs` 或 `warnings` 有引用时，先核对这些脚本和作业。
5. 用户同意后加 `--yes`。用 `cleanup job <作业号>` 查看归档结果，报告释放空间和重建配方的位置。

打包作业要 `plan.job_resources` 写的核数和内存（固定 2 核 / 8G）。`warnings` 里有「交上去会先排队」那句，就把它原样转告用户：它写明了空闲几个核、作业要几个，交上去要等别人的作业结束才开始。

## 删除已验证可重建的副本

1. **扫描**：跑 `cleanup scan`，用**一张表**给用户讲清每一项：版本或 fp、可释放的空间、引用、建议。
2. **在计划里列出拟处理的每一项。** 默认建议：

   | 情况 | 建议 |
   |---|---|
   | 和 env-store 存档对应、可重建且无引用的环境副本 | 可预演删除 |
   | 有引用或可重建性尚未确定的环境 | 按上面的迁移和归档流程核对 |
   | 数据库副本，`verified` 有值（和公共某个版本逐个文件对得上） | 可以 link 或 remove |
   | 数据库副本，`verified` 没有值 | 保留，可能是用户自己定制的 |
   | `in_use` 有内容 | 等用完再处理 |
   | `refs_complete` 为 false | 保留 |
3. **先列出完整计划**，用户确认后才动手。
   - 列计划前，对每个候选跑一次预演 `cleanup apply <path> --action remove`（不带 `--yes`）。
     - `steps` 是将执行的每一步，最后一步能看出清的是哪个 conda 安装。
     - `refuse` 不为空就是不能删，照原话告诉用户。
     - `warnings` 为空不代表没风险：会丢的数据写在 `reasons` 里。
   计划里写清：
   - 每一项删什么、改哪些脚本的哪几行、预计能腾出多少空间。
   - `reasons` 里提到的不属于任何包的文件（比如下载进环境的数据库）：删了靠配方也救不回来，单独写出来。
   - 删完会清一次包缓存：conda 环境跑 `conda clean --all -y`，作用于整个 conda 安装（比如整个 `~/miniforge3`）；pixi 环境跑 `/usr/local/bin/pixi clean cache --conda -y`。
     现有环境照常能用，以后新建环境要重新下载包。
4. **逐项执行**：`cleanup apply <path> --action remove --yes`。
   - 以下情况它会拒绝：
     - home 以外的路径
     - 路径里有软链接
     - 正在被使用
     - conda 的安装目录（base）
     - 把项目目录当成了环境
     - 环境里嵌着别的环境
     - pixi 项目环境
   - 被拒绝时照 `refuse` 的原话告诉用户，不要想办法绕过去。
5. **报告**：
   - 每一项实际腾出了多少（`freed.immediate` + `freed.cache`）
   - 重建配方存在哪
   - 迁移后使用的项目 fp 或公共库版本

## 不做的事

- 不用 sudo，不碰别人的 home。
- 不给环境做软链接：链到公共环境后，在里面 `pip install` 会悄悄装进 `~/.local`。
  - 以前换成的工具箱软链按 `toolbox-migration.md` 找齐引用，先迁脚本并试跑。经用户确认后处理链接本身。
  - **不要**对软链接跑 `conda env remove`，它会顺着链接去删公共环境里的文件。
- 点开头目录里的环境（如 `~/.conda/envs/…`）不能 `--env` 打包，只能 `remove`。
- R 包不归这里管：面板「我的重复安装」里直接给了删除命令。
