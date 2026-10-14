# 服务器上的项目：代码推了没、推到 Gitea

> 什么时候读：用户问「服务器上的代码推了没」「这个项目还要不要留在服务器上」「写论文要写数据库版本」「把服务器上的项目推到 Gitea」时。
> 本机上的项目用 git-auto-sync；这里只管**服务器上**的项目。

逻辑在 `scripts/remote_project.py`，引擎经 ssh 交给服务器上的 `python3` 执行。它以用户自己的身份运行，从不 `--force`，令牌不经过它：

    python scripts/advise.py project check <项目目录>                                   # 只看不动
    python scripts/advise.py project ignore <项目目录> --add data/ results/ [--yes]     # 把数据目录写进 .gitignore
    python scripts/advise.py project push <项目目录> -m "<说明>"                         # 预演：要提交的文件、大小、plan_id
    python scripts/advise.py project push <项目目录> -m "<说明>" --yes --plan-id <id>
    python scripts/advise.py project init <项目目录> --remote <仓库网址> [--yes]         # 还不是仓库的项目

## check 看什么

- 是不是 git 仓库、在哪个分支、有没有上游；没提交的改动、没推的提交、远端是否领先本地。
  - `remotes` 列出每个远端的名字和网址（凭据已打码），`upstream_remote` 是当前分支推往哪一个。有好几个远端时，跟用户说清会推到哪个。
  - `uncommitted` 含没跟踪的新文件，`untracked` 单列其中有几个。`cleanup cold` 里的 `dirty` 只数已跟踪文件的改动，两个数对不上是正常的。
  - `uncommitted_sample` 只列前 15 个；`uncommitted_by_dir` 按顶层目录给出全部的数目，先看它，免得几十个日志把脚本挤出样本。
  - `other_branches`：别的本地分支里有没推的提交、或从没推过的；`stash`：只存在服务器上的 stash 个数。问「都推了吗」时这两样也要说。
  - `remote_ahead` 是当场查远端的；`unpushed_*`、`ahead_behind` 比的是本地记着的远端分支（不 fetch），可能是旧的。
- **不该进 git 的东西**：单个超过 50 MB 的文件（没跟踪的、已暂存的、没推的提交里的都算）；数据目录 `data/`、`_data/` 和作业日志目录 `logs/` 没写进 `.gitignore`；像密钥的文件。一次要推的总量接近 100 MB 会提醒。
  - `results/` 不强制忽略：小的结果表可以进 git，单个超过 50 MB 的照样拦；大的结果按 `cold-data.md` 搬到 NAS。
  - 用 `gen-sbatch` 跑过作业的项目，`logs/` 里全是作业日志：第一次推之前用 `project ignore` 把它写进 `.gitignore`。日志照样留在服务器上。
- 嵌套的仓库、属主是 root 的文件（git 会因为 dubious ownership 拒绝）、git 身份（`user.name` / `user.email`）有没有配。
- **用到的公共数据库**：脚本里写的 `/opt/bio/db/<库>/<版本|current>`，以及登录环境里指向它的变量，都解析成实际版本。`current` 和变量解析出来的是**今天**的版本，不一定是当初出结果时用的；写论文以出结果那天的作业日志为准。
- 有没有环境说明文件（environment.yml / pixi.lock / requirements.txt 等）。
- `todo` 列出要做的事。
  - 项目能不能离开服务器，看 `code_safe`：有远端，没有没提交的改动和没推的提交，别的分支和 stash 也都推了。
  - `clean` 更严，还要求 `.gitignore`、环境说明文件、git 身份都配好，这些是为以后推送准备的。`clean: false` 而 `code_safe: true`，代码已经都在远端了。

## 推送：先预演，再带 plan_id 确认

1. `push` 不带 `--yes`：列出将提交的全部文件（含已暂存的）、每个的大小、要推的提交和总量，并给出 `plan_id`。有 `refuse` 就照原话告诉用户，先处理；被拒的预演不给 `plan_id`。
   - 清单要逐个过一眼：引擎只拦密钥、数据、日志和大文件，误生成的小文件（比如名字就是一个引号的空文件）会被一起提交。
2. 把清单和提交说明给用户看，**用户点头**再带 `--yes --plan-id <id>` 执行。预演之后仓库又变了（比如作业写了新文件、说明改了），会被拒绝：重新预演、重新确认。
   - 要先处理 `refuse`（比如写 `.gitignore`）时，用户可以一次同意整套：先处理，再按讲好的清单推。重新预演后清单和讲给用户的一模一样才执行，有任何出入就停下再问。
3. 分支第一次推送（还没有上游）加 `--set-upstream`。
4. 远端有本地没有的提交：请用户先 `git pull`，不强推。
5. 引擎只推上游（`upstream_remote`）。别的远端（比如 GitHub 镜像）也要同步，把命令写给用户，由用户自己推。
6. 引擎把全部改动做成**一个**提交。想分几次提交，请用户在服务器上自己分批提交，再用 `project push` 推：没有新改动时它只推已有的提交。
7. 有个别文件不该提交：用 `project ignore` 写进 `.gitignore`；要删的文件列出精确路径，由用户自己删。

## 第一次用：凭据和 git 身份（用户自己做）

- **令牌不经过 AI。** 请用户在自己的终端里 ssh 到服务器，先运行 `git config --global credential.helper store`，再进项目目录运行一次 `git ls-remote origin`，按提示输入用户名和令牌（令牌只需要仓库读写权限）。`store` 把令牌以明文存在用户自己的 home 里，只有本人和管理员读得到。
- **git 身份**：`check` 报身份没配时，请用户自己设 `git config --global user.name` / `user.email`。改服务器上的 git 配置属于总闸，先问。

## 还不是仓库的项目

1. 请用户先在 Gitea 网页上建一个**私有**仓库（建库这一步本 skill 不代做）。
2. `project init <目录> --remote <网址>` 预演，用户同意后加 `--yes`：只做 `git init` 和关联远端。
3. 用 `project ignore` 把数据目录写进 `.gitignore`，再用 `project push --set-upstream` 预演第一次推送。

数据不进 git：大文件和原始数据按 `cold-data.md` 搬到 NAS。
