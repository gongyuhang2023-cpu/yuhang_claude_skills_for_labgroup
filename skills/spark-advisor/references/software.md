# 把课题工具锁进项目

## 1. 先看服务器规则

按 [服务器规则顺序](../SKILL.md#先读本次会话的服务器规则) 查看工具是否有专门安排，例如 iPHoP。公共 R 可直接用 `/opt/bio/bin/R` 或 `/opt/bio/bin/Rscript`。`/opt/spark-sys` 专供服务器服务使用。课题脚本使用各自的项目环境。

## 2. 查推荐配方

运行 `python scripts/advise.py software find <工具名>`。按工具名、环境名、命令名或兼容命令名精确匹配。查看 `recipes` 里的版本、依赖约束、频道、命令、绑定的数据库和备注。对应的 `next[].steps` 给出锁定步骤。命中多个配方时，按脚本实际需要选定工具。

目录正常且没有匹配配方时，查看 Conda 的 `linux-aarch64` 和 `noarch` 构建。`noarch` 的依赖仍须由 ARM64 锁解析验证。各 Conda 渠道确认缺少构建后再看 PyPI。用 PyPI 前核对项目主页、wheel 支持的 Python 版本和平台。渠道查询失败时先补查，再选包。有数据库绑定时，用 `database find` 核对工具和库的版本及格式。

`recipe_errors` 会点名格式有误的工具和字段，其余配方可以继续用。面板不可用、目录格式错误或所需工具的配方有误时，按提示重查 `software recipes` 和 `rules`。也可在 Spark 只读查看 `/opt/bio/share/recipes.json`，核对版本、频道和全部依赖后再写锁。

迁移时用 `software scan` 找脚本里的工具箱调用。被 Spark 系统命令盖住的裸命令会自动排除，剩余裸命令按原作业的 PATH 和激活步骤核对。显式工具箱路径照常计入。见 [工具箱迁移步骤](toolbox-migration.md)。

## 3. 锁进项目

对已有 Spark 项目目录先用 `env init --dir <项目目录>` 预览。缺配方时，经用户同意加 `--yes` 初始化。合用多个工具时先比较各配方的依赖、频道顺序和备注。兼容的可以合并。CheckV 与 Bakta 这类冲突组合按 [多环境 manifest 示例](project-envs.md#一个项目的多个环境) 分为具名环境，各自列全频道和版本。下面的命令是分别加到合适环境的示例。一个项目已经用具名 feature 时，在 Pixi 命令中指定 `--feature <名字>` 或编辑对应 TOML 段，再核对整份锁。

`dependencies` 的值可以是版本字符串，也可以是含 `version` 和可选 `channel` 的表。用 `/usr/local/bin/pixi add --no-install` 写入约束。先把本地频道按配方要求加入选定环境的频道列表。带 `channel` 的依赖用 `channel::包名+版本约束` 指定包来源，例如 Bakta 的 BLAST。

```bash
/usr/local/bin/pixi add --no-install gtdbtk==2.5.2 'python=3.12.*'
/usr/local/bin/pixi add --no-install bakta==1.12.2 'file:///opt/bio/channel::blast==2.17.0' python==3.12.16 diamond==2.2.8 pyhmmer==0.12.3
```

PyPI wheel 用 `/usr/local/bin/pixi add --no-install --pypi <包名和版本约束>`。按 `project-envs.md` 设置顶层 `[tasks].smoke`，或沿用已覆盖所选环境的测试。运行 `/usr/local/bin/pixi lock`，将 `pixi.toml` 和 `pixi.lock` 与项目代码放在一起。公共库用 `databases.lock.json` 固定版本。

## 4. 在作业里用

按 [项目环境流程](project-envs.md) 用 `env build --dir <项目目录> -e <环境>` 看各所需环境的计划。经用户同意后加 `--yes --wait` 构建。用 `gen-sbatch --env <项目目录> --workdir <Spark 项目绝对路径>` 生成脚本。单环境用 `-e`，多个环境用重复的 `--env-step`。用 `env check` 核对脚本中的环境，再预演提交。

## 5. 临时查看文件或试用工具

“当前项目”是用户已指定承载本次分析的目录，空目录也可以是项目。已有构建时优先复用该项目环境，用 `env-store use <fp>` 取得激活脚本路径。`source` 后用 `command -v <工具>` 确认命令来自该 fp 的环境，再运行秒级查看命令。未指定项目时可复用自己的 scratch 项目，例如 Spark 上的 `/home/<用户>/Workspace/Projects/scratch`，按需加工具和更新锁。作业的日志放在项目的 `logs/`。

查公共库和本地文件的目录、大小、格式或少量表头可直接做。还没确定库或版本路径时先跑 `database find <库名>`。FASTA 全量计数以本次总读入量达到 1,000,000,000 字节为排队参考线。低于这条线且确认几秒可完成时可以直接做。共享存储、全量解压或磁盘负载使耗时拿不准时，走 Slurm。

小任务没有现成环境时，可以一次展示全程计划。例如用 CheckV 检查小 FASTA，把固定数据库版本、写配方及锁、按计划构建环境、生成脚本、提交和查看结果写在一起。附拟写文件、完整输入输出路径和作业资源及预计时长。环境指纹和作业号以前一步的实际结果为准。[同意规则](../SKILL.md#同意规则)

