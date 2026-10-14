# 清理和公共区重复的个人安装

> 什么时候读：用户要腾空间、问「这几个环境和公共区重复，能删吗」，或面板「我的重复安装」里有东西时。**删任何东西之前读。**

服务器每天扫描各人的 home，把和公共工具或数据库**同名**的个人 conda / pixi 环境、数据库副本，列在面板「我的重复安装」里。

清理逻辑在服务器命令 `spark-cleanup` 里。它的特点：
- 以用户自己的身份运行，只动他自己的 home。
- 默认只看不动。

本 skill 只是把命令转过去：

    python scripts/advise.py cleanup scan [--path P …]                       # 逐项检查，不改任何东西
    python scripts/advise.py cleanup refs P                                   # 只看哪里用到了它
    python scripts/advise.py cleanup verify [P] --cmd 命令…                    # 公共版的这些命令能不能启动（只看不动）
    python scripts/advise.py cleanup apply P --action remove                  # 预演删除
    python scripts/advise.py cleanup apply P --action archive --env           # 预演：先原样打包到 NAS 再删
    python scripts/advise.py cleanup apply P --action … --yes                 # 真执行

环境路径用 scan 结果里的 `path`；它属于哪个项目，看 `created_from` 和 `refs` 指向的位置。要找 scan 结果以外的项目目录时照 SKILL.md：先读 `references/workspace-layout.local.md`，没有这个文件就问用户，别自己编。
- 在 Windows 的 Git Bash 里直接传绝对路径也可以，被 Git Bash 改写的路径会自动还原；统一带 `MSYS_NO_PATHCONV=1` 最省事（`gen-sbatch` 的 `--workdir` 不会自动还原）。
- 也可以传带引号的 `'~/…'`。

**两种删法，选哪种：**
- `--action remove`：就地删，删前只存一份几 KB 的重建配方（history、`@EXPLICIT` 精确包清单、pip 包清单）。靠配方能重建环境，但环境里另外放的数据救不回来；NAS 备份也不备份 conda 环境。照配方或项目的 `environment.yml` 重建时，去掉 `r-base` 和 `r-*`：服务器不许用 conda 装 R，用公共的 R。
- `--action archive --env`（只限 conda 环境）：先存配方，再把环境**原样**打包到 NAS、逐个文件核对，然后才删本地。出了问题 `cleanup restore P` 原样搬回，原路径不变，`conda run` / `conda activate` 照常能用。提交成 Slurm 作业，几 GB 的环境一般几分钟。**部分重复的环境默认用这种。**

## 怎么读 scan 的结果

- **范围**：不带 `--path` 时，只列每天扫描认定和公共区重名的环境和数据库副本。
  - 没列出来的就不是重复安装。想看某一个，用 `scan --path`。
  - 管理员换成软链接（指向 `/opt/bio`）的环境也不在里面。
- **新鲜度**：候选名单来自每天的扫描（`inventory_generated`，服务器本地时间）。档位、空间、引用、是否在用，都是这次 scan 当场查的。
- **`tier`**：
  - `exact`：完全重复。同名包的版本都和公共的一样，也没有别的显式包、pip 包和不属于任何包的文件，删了不丢东西。
  - `partial`：部分重复，其余情况都归这里。`reasons` 里逐条写了差异。
    - 同名的只占一小部分时（比如只因为带了 ffmpeg 被列进来），要跟用户说明：这是别的用途的环境，碰巧带了一个公共工具，不算重复安装。
  - `none`：和公共区不重名。
- **`overlap`**：每个同名包的 `version`（用户的）、`shared_version`（公共的），引擎在本机补上：
  - `gap`：差多少——`same` / `patch` / `minor` / `major`，读不出来就是 `unknown`。
  - `direction`：`newer` = 公共版更新；`older` = 公共版更旧（换过去等于降级）。
  - 每项另有 `max_gap`（最大的差距）和 `public_older`（公共版更旧的包）。
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
- **`suggest.action`**：`remove`、`keep`、`wait`、`link`（只用于数据库副本）、`none`。部分重复的一律是 `keep`：换不换由你按下面的规则和用户一起判断。

## 智能判断：部分重复的环境怎么处理

完全重复（`exact`）的不用判断：改完引用就删（见文末「只删」流程）。部分重复的先分清两件事：

- **换成公共版**：改脚本去用 `/opt/bio/bin` 下的命令，再删自己的环境。走下面 5 步。
- **打包到 NAS**：环境原样存走、腾出空间，不改任何脚本，以后能原样搬回。

然后依次问两个问题。

**① 这个环境还用不用？** 先弄清它属于哪个项目：
- 在项目目录里的（`<项目>/_data/envs/<名>`），就是那个项目的。
- 命名环境（`~/miniforge3/envs/<名>`）：看 `refs` 里的文件在哪个项目、`created_from` 是哪个项目的 `environment.yml`、名字像不像。对不上就问用户。
- 只凭名字对上的，只算线索，先问用户一句。
- 环境还被项目外的脚本用着（`refs` 在项目外）：就算项目已经搬走，也先告诉用户，打包后那些脚本会失效。

项目已经搬到 NAS（`cleanup archived` 里有；这条命令会顺手重写个人清单 `references/nas-archive.local.md`，无害。只想确认一个项目的话，看它原处有没有 `<名>.ON_NAS.txt` 就够了），或用户说课题结束了：**打包到 NAS 再删**（`--action archive --env`），不用再问 ②。

**② 还在用，换不换成公共版？** 从上往下看，**第一条对上的就是结论**。下表的「保留」只是不换；以后不用了，照样可以按 ① 打包。

| 情况 | 判断 |
|---|---|
| 只因带了一个公共工具被列进来：同名的包只有一两个，而且不是这个环境的主要用途（看环境名、`created_from`、用它的脚本在做什么） | 误报：不是重复安装，不换。同名的那个正是主工具时（比如 foldseek 项目里的 foldseek），不算误报，接着往下看 |
| 公共版比用户的**旧**（`public_older` 不为空） | 保留：换过去等于降级 |
| 和数据库版本绑定的工具（Bakta、GTDB-Tk、CheckV、geNomad 这类，工具版本决定能读哪版数据库） | 保留，除非用户确认数据库也一起换 |
| 课题还在写论文、已经用这个版本出过结果 | 保留（可复现）；论文发了再处理 |
| 脚本直接调环境里的 `python` | `/opt/bio/bin` 里没有 python。脚本只用标准库的，可以改用系统的 `/usr/bin/python3`（3.12），但要用小数据实测一遍；环境钉了别的 python 版本、或用到第三方包的，按下一行保留。R 不受影响：`Rscript` 走公共的 `/opt/bio/bin/Rscript` |
| 脚本是用 `import` / `library()` 调用环境里的库 | 保留：库换不成 `/opt/bio/bin` 下的命令 |
| 用到了公共区没有的包，或环境里放了数据（`reasons` 里有不属于任何包的文件，比如下载进环境的数据库） | 保留，或先用 `software` 申请装进公共区 |
| `major` 差距，或 `0.x` 版本的 `minor` 差距 | 默认保留：大版本、0.x 的小版本都可能不兼容 |
| 只差 `patch` / `minor`、公共版更新、只通过命令行调用 | **候选**：讲清楚后，用户逐个点名才走下面 5 步 |

「能删的都删」这类笼统的话不算点名。

**只打包（①）不用走 5 步的前 3 步**：直接预演 `cleanup apply P --action archive --env`。预演会顺带查引用：`plan.refs` 或 `warnings` 里有脚本还在用它，就先告诉用户，打包后那些脚本会找不到环境。预演的 `plan.bytes` 是要打包的量，不是能腾出的空间：能腾出多少照 scan 的 `bytes.private` 报，清缓存另加 `after_clean`。

## 5 步：换成公共版，保证代码不坏

1. **查用法**：看 `refs`，打开那些脚本逐个读，弄清实际调用了环境里的哪些命令、`import` 了哪些库。
2. **改引用**：
   - 去掉 `conda activate <环境>` 或 `source …/activate <环境>`，再把用到的工具命令改成**绝对路径** `/opt/bio/bin/<命令>`。
     **不能只删 activate 那一行**：cron 和经 ssh 提交的作业拿不到登录 shell 的 PATH，会找不到命令。
     写成绝对路径在哪都能跑：公共区的命令启动时会把自己环境的 `bin` 加进 PATH，子工具也找得到。
   - 写死的环境路径（比如 `~/miniforge3/envs/<环境>/bin/<命令>`）也改成 `/opt/bio/bin/<命令>`。
   - 改之前把原文件复制一份到 `~/.spark-cleanup/<日期>/scripts/`。放在 git 仓库里的文件可以靠 git 恢复，不必另存。
   - 改完把 diff 给用户看，**用户点头才算改完**。
3. **试**：
   - `cleanup verify P --cmd <脚本真正用到的命令…>`：公共版能不能启动。只说明能启动，不说明结果一样。
   - 再用小数据交一个作业实际跑一遍（走 `submit-flow.md`，提交前问），和原来的结果比一比。
4. **留后路**：
   - 用 `cleanup apply P --action archive --env` 预演，给用户看计划；
   - 环境属于某个项目时，把精确版本清单写进项目仓库（`~/.spark-cleanup/<日期>/<名>.explicit.txt` 等），用户点头后用 `project push` 推上去（见 `code-check.md`），写论文用得上。项目已经搬走了就跳过这一条：后路是 NAS 上的环境包本身。项目和环境要一起搬时，想留这份清单就在搬项目之前写进仓库、推上去，不想留就跳过。
5. **删**：用户同意后加 `--yes`。打包作业核对无误才删本地，并清一次包缓存。用 `cleanup job <作业号>` 看结果，报告腾出了多少（`freed`）、NAS 上的包和重建配方在哪。

打包作业要 `plan.job_resources` 写的核数和内存（固定 2 核 / 8G）。`warnings` 里有「交上去会先排队」那句，就把它原样转告用户：它写明了空闲几个核、作业要几个，交上去要等别人的作业结束才开始。

## 只删、不留后路的流程（完全重复，或用户明确不要后路）

1. **扫描**：跑 `cleanup scan`，用**一张表**给用户讲清每一项：档位、差异、真正能腾出的空间、引用、建议。
2. **和用户一起定要处理哪几项。** 默认建议：

   | 情况 | 建议 |
   |---|---|
   | 完全重复，没有引用 | 删 |
   | 完全重复，有引用 | 先改引用（见 5 步里的第 2 步），再删 |
   | 部分重复 | 按上面「智能判断」 |
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
   - 删完会清一次包缓存：conda 环境跑 `conda clean --all -y`，作用于整个 conda 安装（比如整个 `~/miniforge3`）；pixi 环境跑 `pixi clean cache --conda -y`。
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
   - 删完后命令现在指向哪里（`which`）

## 不做的事

- 不用 sudo，不碰别人的 home。
- 不给环境做软链接：链到公共环境后，在里面 `pip install` 会悄悄装进 `~/.local`。
  - 管理员以前换成的软链接照常用（`/ai.md` 也这么说），不用拆。用户自己想拆时，用 `cleanup apply <链接> --action remove`，它只删链接本身。
  - **不要**对软链接跑 `conda env remove`，它会顺着链接去删公共环境里的文件。
- 点开头目录里的环境（如 `~/.conda/envs/…`）不能 `--env` 打包，只能 `remove`。
- R 包不归这里管：面板「我的重复安装」里直接给了删除命令。
