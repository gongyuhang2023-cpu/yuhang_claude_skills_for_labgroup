# 项目环境：env-store

课题工具由项目的 `pixi.lock` 固定，作业引用固定 fp。env-store 在配方完全相同时只存一份环境，按需在 Spark 取回。
查工具先读 `software.md`。短暂查看也用当前项目环境，项目外用自己的 scratch 项目。生成的项目作业从锁定环境取工具。作业里使用公共 R 时写 `/opt/bio/bin/R` 或 `/opt/bio/bin/Rscript`。

## 完整流程

以下 `~/project` 指服务器上的项目目录；带引号传给引擎，由服务器展开。
`--workdir` 仍须填写服务器上的真实绝对目录。先确认项目落点与目录，`env init` 只初始化已有目录。
预览分别报告 `project_exists` 和 `manifest_exists`。目录缺失时，预览和 `--yes` 都返回错误和下一步。选定已有的 Spark 目录，或确认项目落点并建好目录后再试。

新项目可一次提交完整计划给用户审阅。以已确认的 Spark 空项目 `~/project` 为例，可写成「我会在 `~/project/pixi.toml` 加入所列配方并生成 `~/project/pixi.lock` → 预演并构建 `default` 环境（1 核、4 GiB，示例预计 5–30 分钟）→ 生成 `~/project/scripts/analysis.sbatch` 并提交所列命令（2 核、8 GiB，示例预计 1–5 分钟）→ 核对 `~/project/logs/` 和 `~/project/results/` 下的结果」。展示前补全配方、脚本命令、实际输入输出、允许的版本和频道范围及删除、覆盖或移动事项。锁与 fp 以前一步的实际结果为准。涉及 Git 提交或副本同步时，把实际文件和目标列入同一份计划。执行仍按下面的流程逐步预演并核对。[同意规则](../SKILL.md#同意规则)

```bash
# 1. 对已有 Spark 项目目录预览；缺配方时，经用户确认后写出最小配方并生成初始锁
python scripts/advise.py env init --dir '~/project' --name analysis
python scripts/advise.py env init --dir '~/project' --name analysis --yes
```

第 2 步在 Spark 项目目录按 `software find` 返回的配方补齐频道，写入全部依赖约束。下面以添加固定 Python 系列版本为例。

```bash
(cd "$HOME/project" && /usr/local/bin/pixi add --no-install 'python=3.12.*')
```

PyPI 依赖用 `/usr/local/bin/pixi add --no-install --pypi <包名>`。冒烟测试见下文。编辑后，在同一目录运行 `/usr/local/bin/pixi lock`。

```bash
# 3. 看构建计划；展示计划并取得用户同意后提交 Slurm 构建作业
python scripts/advise.py env build --dir '~/project' -e default
python scripts/advise.py env build --dir '~/project' -e default --yes --wait
python scripts/advise.py env status --dir '~/project' -e default
# 4. 环境就绪后用项目锁生成脚本。以下 workdir 换成实际服务器项目绝对路径
python scripts/advise.py gen-sbatch --name analysis --cpus 2 --mem-gb 8 \
  --workdir /path/to/project --env '~/project' -e default \
  --command 'python analyze.py' --out job.sbatch
python scripts/advise.py env check job.sbatch --json
# 5. 先只检查；按提交总闸取得用户同意后再交
python scripts/advise.py submit --script job.sbatch --check-only
python scripts/advise.py submit --script job.sbatch
```

`env init` 不覆盖已有 pixi.toml；不带 `--yes` 只预览、不运行 lock；带 `--yes` 才运行
`/usr/local/bin/pixi lock`。已有配方可直接加依赖，再用更新后的锁预演构建。`--name` 只用于新配方。
`/usr/local/bin/pixi add --no-install` 更新配方和锁，构建留给 env-store 的 Slurm 作业。
`env build` 总是重新 plan，摘要显示 fp、conda/PyPI 包数、估算体积、警告与拒收原因；
允许的计划先查 fp 的 status；已有存档（archived，或 failed 且 archived 为 true、无 archive_problem）时不重建，
提示 `env-store fetch <fp> --wait`，或直接 submit 交主作业自动取回。即使查询后状态改变、
服务器 submit 返回 archived，也按取回处理。无存档时才可用本次 `plan_sha256` 提交构建。
`--wait` 只有配合 `--yes` 才等构建完成。
构建由 `env-store submit` 在本人 Slurm 小作业里进行，不能在登录节点手工构建。
查本项目的环境用 `env status --dir`，也可指定 fp；两者必须给一个，不能同时指定。
命令沿用引擎 JSON 输出，中文摘要和服务器原始字段一起返回，`--json` 可以显式写出。

## 一个项目的多个环境

先合看配方的 `dependencies`、`channels` 和 `notes`。约束兼容、能按同一频道顺序解析的工具可以合在一个环境里。遇到 Python 版本、传递依赖或频道优先级冲突，给不同工具组各写一个 Pixi feature，放在同一份 `pixi.toml` 和 `pixi.lock` 里。每个环境设 `no-default-feature = true`，并在自己的 feature 里列全频道，保留各自的顺序。不要把任意一个工具的 Python 或 BLAST 写在共享的顶层依赖中。

下面用 geNomad、fastp、SeqKit 共用 `virus`，CheckV 和 Bakta 各用独立环境。CheckV 的本地频道排第一。Bakta 的本地频道排最后，BLAST 单独指定来源，并按配方备注锁定 Bioconda 的 DIAMOND 和 pyhmmer 版本。

```toml
[workspace]
name = "virome-example"
channels = ["conda-forge", "bioconda"]
platforms = ["linux-aarch64"]

[feature.virus]
channels = ["conda-forge", "bioconda"]

[feature.virus.dependencies]
genomad = "==1.12.0"
python = "==3.12.15"
fastp = "==1.4.0"
seqkit = "==2.14.0"

[feature.checkv]
channels = ["file:///opt/bio/channel", "conda-forge", "bioconda"]

[feature.checkv.dependencies]
checkv = "==1.1.1"
diamond = "==2.1.8"
python = "==3.12.15"

[feature.bakta]
channels = ["conda-forge", "bioconda", "file:///opt/bio/channel"]

[feature.bakta.dependencies]
bakta = "==1.12.2"
blast = { version = "==2.17.0", channel = "file:///opt/bio/channel" }
python = "==3.12.16"
diamond = "==2.2.8"
pyhmmer = "==0.12.3"

[environments]
virus = { features = ["virus"], no-default-feature = true }
checkv = { features = ["checkv"], no-default-feature = true }
bakta = { features = ["bakta"], no-default-feature = true }
```

用 `/usr/local/bin/pixi lock` 生成整份锁，再按所需环境分别预演 `env build --dir '~/project' -e virus`、`-e checkv` 或 `-e bakta`。构建计划可合并请用户同意，执行时逐个核对并加 `--yes --wait`。单一步骤用 `gen-sbatch --env '~/project' -e bakta --command '…'`。同一作业有多个步骤时，用下文的 `--env-step`。从锁中核对每个环境的版本和来源，特别是两个环境各自的 DIAMOND。命令示例的输入和库路径按本项目的实际文件和锁填写。

### 冒烟测试在哪里写

env-store 在构建作业中用原始 manifest 的顶层 `[tasks].smoke`，先 `source` 所选 fp 的 `use.sh`，再从项目目录用 `/bin/bash -c` 运行任务，限时 600 秒，以提交者身份执行。可以写字符串或 `{cmd = "…"}`。重定向和 `&&` 走 Bash 语义。任务需要遇错即停时写 `&&` 或在任务里设 `set -e`。
`[feature.<名字>.tasks]` 可用于 Pixi 自己的任务，env-store 的构建冒烟只看顶层项，即使环境用了 `no-default-feature`。已有 ready 环境或取回存档时，另跑该环境的短命令以验证本次使用。

上面的多环境 manifest 可选加顶层 `smoke = "bash -e scripts/env-smoke.sh"`（放在 `[tasks]` 下），让同一个脚本按已激活的工具分派检查。用该环境的 `bin` 判断，避免把继承的 PATH 上另一份旧命令算作通过。示例脚本如下。

```bash
#!/bin/bash
set -eo pipefail
IFS=: read -r -a dirs <<< "$PATH"
for bin in "${dirs[@]}"; do
  case "$bin" in /opt/bio/envs/*/env/bin) break ;; *) bin= ;; esac
done
if [[ -z $bin ]]; then
  printf '%s\n' 'env-store environment bin missing' >&2
  exit 1
fi
if [[ -x $bin/genomad ]]; then
  "$bin/genomad" --help && "$bin/fastp" --version && "$bin/seqkit" version
elif [[ -x $bin/checkv ]]; then
  "$bin/checkv" -h
elif [[ -x $bin/bakta ]]; then
  "$bin/bakta" --help
else
  printf '%s\n' 'no smoke command matches the active environment' >&2
  exit 1
fi
```

## 收编已有项目

服务器项目已有 `pixi.toml` 和 v7 `pixi.lock` 时，用 `env adopt` 把它建进 env-store，
不搬动项目自己的环境目录。没有锁先在项目目录执行 `/usr/local/bin/pixi lock`；没有 manifest 可先用 `env init`。

```bash
# 先看计划；展示计划并取得用户同意后，才加 --yes 提交构建
python scripts/advise.py env adopt --dir '~/project' -e default
python scripts/advise.py env adopt --dir '~/project' -e default --yes --wait
```

adopt 只读检查项目文件，由 `env-store plan` 核验 v7 锁与依赖；拒收原因和修法与 `env build` 相同。
ready、archived 或 failed 但有已核对存档时报告「env-store 里已有」，不 submit、也不自动 fetch。
archived 或 failed 且有存档时先用 `env-store fetch <fp> --wait` 取回，也可由主作业 submit 自动取回。
需要新建时，不带 `--yes` 只打印计划；带 `--yes` 使用本次计划哈希提交本人名下的 Slurm 构建作业，
`--wait` 只有配合 `--yes` 才等构建完成。已有构建/取回作业的复用仍交由服务器 submit 判定。

JSON 的 `project_environment` 列出所选 `<项目>/.pixi/envs/<环境名>` 是否存在、体积和 `du_status`。
体积使用 `du -s -k`，最多等 10 秒；超时或读取失败显示体积未知并给出 warning，不阻止收编。
检查本身失败则停止，不提交。没有项目环境时无需清理；存在时，待 env-store 里的环境 ready 后按顺序做：

1. 用 `gen-sbatch --env <项目> -e <环境>` 重新生成完整作业脚本，或按下一节修改已有脚本的激活块。
   手改时保留 `# ENV-STORE` 标记，
   供 submit 检查状态、取回和 pin；运行命令直接调用锁定工具，避免继续用 `/usr/local/bin/pixi run` 包装。
2. 跑一次作业，确认结果没有变化。
3. 再运行 `cleanup scan --path <项目>/.pixi/envs/<环境名>`，通过既有 `cleanup` 流程清理。
   spark-cleanup 会识别和 env-store 里同一份环境完全重复的 `.pixi/envs`，删除前逐个先问；以实际扫描结果为准。
   `env adopt` 不调用清理、不删除任何东西。以后再用 `/usr/local/bin/pixi run`，缺失的项目环境会自己重装。

env-store 里的环境是只读的（配方相同时和别人共用同一份），不要往里 `pip install`。要改依赖就改 manifest、更新锁、重新 build。

## 脚本、取回与复现

单环境可以用 `--command` 和 `-e`。一个作业用多个环境时，重复 `--env-step <环境> '<命令>'`，给同一份项目锁的步骤排序。生成器为每个环境写一个顶格的 `# ENV-STORE` 标记和一个激活变量，每一步分别在子 shell 里激活。步骤间通过文件传递结果。某一步失败，后续步骤停止。

```bash
python scripts/advise.py gen-sbatch --name virus-qc --cpus 4 --mem-gb 16 \
  --workdir /path/to/project --env /path/to/project \
  --env-step virus 'genomad end-to-end --threads "$SLURM_CPUS_PER_TASK" input/contigs.fna results/genomad /opt/bio/db/genomad/1.9' \
  --env-step checkv 'checkv end_to_end results/genomad/contigs_summary/contigs_virus.fna results/checkv -d /opt/bio/db/checkv/1.5 -t "$SLURM_CPUS_PER_TASK"' \
  --out scripts/virus-qc.sbatch
python scripts/advise.py env check scripts/virus-qc.sbatch --json
python scripts/advise.py submit --script scripts/virus-qc.sbatch --check-only
```

创建好输出脚本的父目录，正式脚本留在项目里，便于 `database scan` 和 `database lock` 看到实际库路径。提前核对库和输出路径，再按提交总闸确认。

手改带 `set -euo pipefail` 的脚本时，按下面方式切换。生成器用 `set -eo pipefail`，因为部分激活脚本会读取未定义变量。手改时只在 `source` 期间用 `set +u`，然后恢复 `set -u`。用子 shell 隔离 PATH 等环境变量，并沿用生成脚本中的 PATH 过滤块。

```bash
# ENV-STORE {"fp": "aaaaaaaaaaaaaaaa", "project": "/path/to/project", "env": "virus"}
ENV1=$(env-store use aaaaaaaaaaaaaaaa) || exit 3
# ENV-STORE {"fp": "bbbbbbbbbbbbbbbb", "project": "/path/to/project", "env": "checkv"}
ENV2=$(env-store use bbbbbbbbbbbbbbbb) || exit 3
(
  set +u
  source "$ENV1"
  set -u
  # 在这里保留生成脚本的 PATH 过滤块，再运行 geNomad 步骤。
)
(
  set +u
  source "$ENV2"
  set -u
  # 在这里保留生成脚本的 PATH 过滤块，再运行 CheckV 步骤。
)
```

示例 fp 须换成对应环境的实际 fp，project 为服务器绝对路径。
`env check` 逐条比较各环境当前锁与脚本的 fp，并检查各 fp 的状态。锁改变时，选择重建并重新生成脚本，
或明确确认继续用旧 fp 复现旧结果；环境已有存档时先 fetch（也可 submit 自动取回），
缺失或失败且无可用存档时才 `env build`。check 不修改脚本。
改锁就是新 fp，需要重建；旧脚本继续使用旧 fp，submit 不会自动换成当前锁，保留旧结果的复现入口。

submit 对每个唯一 fp 检查状态：ready 直接交；archived 或 failed 且有已核对存档
（archived 为 true、没有 archive_problem）先 fetch。building/restoring 使用仍在队列里的 status.job。
restoring 的 job 为 null 或 stale 时重新 fetch；building 的 job 为 null 或 stale 时拒绝并提示重新 env build。
failed 且无可用存档、unknown 或缺失环境阻止提交。pending tar 的恢复由服务器 fetch 判断，调用方不推测未约定的 tar 路径。
取回、构建和数据库 `--stage` 依赖合成 `afterok:…`，重复作业号只保留一次。
有环境依赖时保留脚本已有的 `#SBATCH --dependency=`：用 `,` 合并，全部依赖都要满足；
若原依赖含 `?`，在 fetch 前拒绝并提示用户手动合并。结果 dependency 给出合并值。
只要实际依赖包含环境作业，额外传 `--kill-on-invalid-dep=yes`，依赖失败时由 Slurm 自动取消主作业，
避免一直排队并 pin 环境。冒烟测试失败时查看构建日志 `env-store-<作业>.log`。
只有 db-cache 依赖时保留原行为，不增加该参数，也不改变原先的依赖处理。
主作业提交成功后马上对每个 fp pin，包括 building 环境，排队期间也受保护。
`--check-only` 只查询和展示计划，不 fetch、不 submit、不 pin，也不创建日志目录。
结果中的 `environments` 是环境计划、`env_fetch_jobs` 是环境 fetch 返回的作业号；
`env_dependency_jobs` 是实际等待的环境作业号；`kill_on_invalid_dep` 和 `dependency_note` 说明自动取消策略。
`stage` 与 `fetch_jobs` 保留数据库缓存的原有含义。pin 出错时保留已交主作业号并警告，不能重复交主作业。

env-store 的失败由独立的返回码/标准错误通道读取；错误 JSON 在 stderr，保留 returncode、stderr、next。
退出码 3 提示先取回，4 提示等构建/取回作业，5 提示有存档取回、无存档重建；SSH 查询失败不会被当成 ready。

## 服务器约束

- 命令为 `/usr/local/bin/env-store`，锁由 `/usr/local/bin/pixi`（0.81）生成，只接受 v7 锁。
- 只接收 conda-forge、bioconda、`/opt/bio/channel`，以及来自 files.pythonhosted.org 的 wheel。
- 拒收 git、sdist、本地路径；项目自身的 `pypi: ./` 跳过、不安装。
- 不执行 post-link 或 `[activation]`。构建作业中的冒烟测试见「冒烟测试在哪里写」。
- 环境对所有人只读，激活设置 `PYTHONNOUSERSITE=1`。不要往环境里 `pip install`；改 manifest、更新锁、重新 build。
- 最近一次使用后本地保留 60 天，之后仅留 NAS 存档；取回必须交 fetch 小作业。
- 不在 `/tmp` 下运行 Python 做测试：其他人遗留的 `.py` 可能遮蔽标准库。用项目中的干净测试目录。
