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

- **`tier`**：
  - `exact`：完全重复。同名包的版本都和公共的一样，也没有别的显式包、pip 包和不属于任何包的文件，删了不丢东西。
  - `partial`：部分重复，其余情况都归这里。`reasons` 里逐条写了差异。
  - `none`：和公共区不重名。
- **空间**：报给用户的空间用下面两个数，别用 `total`。其余部分和用户别的环境共用，删了也不会释放。
  - `bytes.private`：删了**立刻**腾出的空间。
  - `bytes.after_clean`：清缓存后**还能再**腾出的空间（估算）。
- **`refs`**：脚本、配置、crontab、Jupyter kernel 里用到它的地方（文件:行）。`refs_complete` 为 false，表示 home 太大没查完。
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
   | 部分重复 | 保留，除非用户明确说这个环境或课题不再用了 |
   | 数据库副本，`verified` 有值（和公共某个版本逐个文件对得上） | 可以 link 或 remove |
   | 数据库副本，`verified` 没有值 | 保留，可能是用户自己定制的 |
   | `in_use` 有内容 | 等用完再处理 |
   | `refs_complete` 为 false | 保留 |
3. **先列出完整计划**，用户确认后才动手。计划里写清：每一项删什么、改哪些脚本的哪几行、预计能腾出多少空间。
4. **改引用**（用户允许直接改脚本）：
   - 去掉 `conda activate <环境>` 或 `source …/activate <环境>`，再把用到的工具命令改成**绝对路径** `/opt/bio/bin/<命令>`。
     **不能只删 activate 那一行**：sbatch 和 cron 拿不到登录 shell 的 PATH，会找不到命令。
   - 写死的环境路径（比如 `~/miniforge3/envs/<环境>/bin/<命令>`）也改成 `/opt/bio/bin/<命令>`。
   - 改之前把原文件复制一份到 `~/.spark-cleanup/<日期>/scripts/`。放在 git 仓库里的文件可以靠 git 恢复，不必另存。
   - 改完把 diff 给用户看。
   - 部分重复的环境里还有公共区没有的包（`reasons` 里列着）。脚本用到这些包的话，**就不要删这个环境**。
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
  - 以前留下的软链接要拆，用 `cleanup apply <链接> --action remove`，它只删链接本身。
  - **不要**对软链接跑 `conda env remove`，它会顺着链接去删公共环境里的文件。
- R 包不归这里管：面板「我的重复安装」里直接给了删除命令。
