# 清理和公共区重复的个人安装

服务器每天扫描各人的 home，把和公共工具或数据库**同名**的个人 conda / pixi 环境、数据库副本，列在面板「我的重复安装」里。

清理逻辑在服务器命令 `spark-cleanup` 里。它的特点：
- 以用户自己的身份运行，只动他自己的 home。
- 默认只看不动。

本 skill 只是把命令转过去：

    python scripts/advise.py cleanup scan [--path P …]               # 逐项检查，不改任何东西
    python scripts/advise.py cleanup refs P                           # 只看哪里用到了它
    python scripts/advise.py cleanup apply P --action remove          # 预演
    python scripts/advise.py cleanup apply P --action remove --yes    # 真删

路径用 scan 结果里的 `path`。
- 在 Windows 的 Git Bash 里直接传绝对路径也可以，被 Git Bash 改写的路径会自动还原。
- 也可以传带引号的 `'~/…'`。

**删除是真删，撤不回来**：
- 没有回收区，NAS 备份也不备份 conda 环境。
- 删之前只会在 `~/.spark-cleanup/<日期>/` 存一份几 KB 的重建配方：history、`@EXPLICIT` 精确包清单、pip 包清单。
- 靠配方能把环境重新建出来，但环境里的数据救不回来。

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
- **空间**：报给用户的空间用下面两个数，别用 `total`。其余部分和用户别的环境共用，删了也不会释放。
  - `bytes.private`：删了**立刻**腾出的空间。
  - `bytes.after_clean`：清缓存后**还能再**腾出的空间（估算）。删完会清整个包缓存，实际腾出的往往更多。
  - 几项一起删时，把各项相加是保守估计：它们彼此共用的包，一起删后缓存里可能多腾出一些。
- **`refs`**：脚本、配置、crontab、Jupyter kernel 里用到它的地方（文件:行）。`refs_complete` 为 false，表示 home 太大没查完。
  - 只认按环境名激活和绝对路径（含 `~/`、`$HOME/` 写法），**相对路径查不到**。
  - 环境在项目目录里（如 `<项目>/_data/envs/<名>`）时，项目脚本常写成 `./_data/envs/<名>`。`refs` 为空也要问用户项目还用不用它。
- **`in_use`**：正在用它的进程和作业。`other_jobs` 是用户另外还有几个作业在排队或在跑。
- **`created_from`**：环境是用什么命令建的。从项目的 `environment.yml` 建出来的，多半是项目环境。
- **`suggest.action`**：`remove`、`keep`、`wait`、`link`（只用于数据库副本）、`none`。

## 流程

1. **扫描**：跑 `cleanup scan`，用**一张表**给用户讲清每一项：档位、差异、真正能腾出的空间、引用、建议。
2. **和用户一起定要处理哪几项。** 默认建议：

   | 情况 | 建议 |
   |---|---|
   | 完全重复，没有引用 | 删 |
   | 完全重复，有引用 | 先改引用（见第 4 步），再删 |
   | 部分重复 | 保留，除非用户点名说这个环境（或它对应的课题）不再用了。「能删的都删」这类笼统的话不算，要逐个点名 |
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
4. **改引用**（用户允许直接改脚本）：
   - 去掉 `conda activate <环境>` 或 `source …/activate <环境>`，再把用到的工具命令改成**绝对路径** `/opt/bio/bin/<命令>`。
     **不能只删 activate 那一行**：cron 和经 ssh 提交的作业拿不到登录 shell 的 PATH，会找不到命令。
     写成绝对路径在哪都能跑：公共区的命令启动时会把自己环境的 `bin` 加进 PATH，子工具也找得到。
   - 写死的环境路径（比如 `~/miniforge3/envs/<环境>/bin/<命令>`）也改成 `/opt/bio/bin/<命令>`。
   - 改之前把原文件复制一份到 `~/.spark-cleanup/<日期>/scripts/`。放在 git 仓库里的文件可以靠 git 恢复，不必另存。
   - 改完把 diff 给用户看。
   - 部分重复的环境里还有公共区没有的包（`reasons` 里列着）。脚本用不用这些包，`refs` 查不出来，要问用户；用到的话**就不要删这个环境**。
5. **逐项执行**：`cleanup apply <path> --action remove --yes`。
   - 以下情况它会拒绝：
     - home 以外的路径
     - 路径里有软链接
     - 正在被使用
     - conda 的安装目录（base）
     - 把项目目录当成了环境
     - 环境里嵌着别的环境
     - pixi 项目环境
   - 被拒绝时照 `refuse` 的原话告诉用户，不要想办法绕过去。
6. **报告**：
   - 每一项实际腾出了多少（`freed.immediate` + `freed.cache`）
   - 重建配方存在哪
   - 删完后命令现在指向哪里（`which`）

## 不做的事

- 不用 sudo，不碰别人的 home。
- 不给环境做软链接：链到公共环境后，在里面 `pip install` 会悄悄装进 `~/.local`。
  - 管理员以前换成的软链接照常用（`/ai.md` 也这么说），不用拆。用户自己想拆时，用 `cleanup apply <链接> --action remove`，它只删链接本身。
  - **不要**对软链接跑 `conda env remove`，它会顺着链接去删公共环境里的文件。
- R 包不归这里管：面板「我的重复安装」里直接给了删除命令。
