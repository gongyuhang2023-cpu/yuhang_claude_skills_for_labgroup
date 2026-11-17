# 项目环境：env-store

正式分析、需要复现结果时用项目环境：版本由 pixi.lock 固定，作业引用固定 fp。
临时查看文件、查询工具版本可用共享工具箱。项目环境不能用工具箱补齐未锁定的依赖；
正式脚本默认从 PATH 移除 `/opt/bio/bin` 和 `/opt/bio/compat`。
只有用户明确需要混用时才加 `--keep-toolbox`，并说明额外工具不受该锁约束。

## 完整流程

以下 `~/project` 指服务器上的项目目录；带引号传给引擎，由服务器展开。
`--workdir` 仍须填写服务器上的真实绝对目录。已有目录才可初始化，不自动建项目目录。
在 ws 上按 SKILL.md 的入口规则将 `scripts/advise.py` 替换为 `scripts/ws_forward.py`。

```bash
# 1. 预览最小 manifest；确认后才写入并生成依赖锁
python scripts/advise.py env init --dir '~/project' --name analysis
python scripts/advise.py env init --dir '~/project' --name analysis --yes
# 2. 在服务器项目目录编辑 pixi.toml，或用 pixi add <包> 加依赖
#    把 tasks.smoke 换成真正的短冒烟测试；示例 echo 仅用于起步
# 3. 在服务器项目目录执行 /usr/local/bin/pixi lock（或以下命令）
python scripts/advise.py env init --dir '~/project' --yes
# 4. 看构建计划；展示计划并取得用户同意后提交 Slurm 构建作业
python scripts/advise.py env build --dir '~/project' -e default
python scripts/advise.py env build --dir '~/project' -e default --yes --wait
python scripts/advise.py env status --dir '~/project' -e default
python scripts/advise.py env find 'numpy==2.0'
# 5. 用项目锁生成脚本。以下 workdir 换成实际服务器项目绝对路径
python scripts/advise.py gen-sbatch --name analysis --cpus 2 --mem-gb 8 \
  --workdir /path/to/project --env '~/project' -e default \
  --command 'python analyze.py' --out job.sbatch
python scripts/advise.py env check job.sbatch --json
# 6. 先只检查；按提交总闸取得用户同意后再交
python scripts/advise.py submit --script job.sbatch --check-only
python scripts/advise.py submit --script job.sbatch
```

`env init` 不覆盖已有 pixi.toml；不带 `--yes` 只预览、不运行 lock；带 `--yes` 才运行
`pixi lock`。已有 manifest 也遵守这条规则。`--name` 只用于新 manifest。
`env build` 总是重新 plan，摘要显示 fp、conda/PyPI 包数、估算体积、警告与拒收原因；
允许的计划先查 fp 的 status；已有存档（archived，或 failed 且 archived 为 true、无 archive_problem）时不重建，
提示 `env-store fetch <fp> --wait`，或直接 submit 交主作业自动取回。即使查询后状态改变、
服务器 submit 返回 archived，也按取回处理。无存档时才可用本次 `plan_sha256` 提交构建。
`--wait` 只有配合 `--yes` 才等构建完成。
构建由 `env-store submit` 在本人 Slurm 小作业里进行，不能在登录节点手工构建。
`env status` 可不带参数列出环境，也可指定 fp；fp 和 `--dir` 不能同时指定。
命令沿用引擎 JSON 输出，中文摘要和服务器原始字段一起返回，`--json` 可以显式写出。

## 收编已有项目

服务器项目已有 `pixi.toml` 和 v7 `pixi.lock` 时，用 `env adopt` 收编进公共环境库，
不搬动项目自己的环境目录。没有锁先在项目目录执行 `pixi lock`；没有 manifest 可先用 `env init`。

```bash
# 先看计划；展示计划并取得用户同意后，才加 --yes 提交构建
python scripts/advise.py env adopt --dir '~/project' -e default
python scripts/advise.py env adopt --dir '~/project' -e default --yes --wait
```

adopt 只读检查项目文件，由 `env-store plan` 核验 v7 锁与依赖；拒收原因和修法与 `env build` 相同。
ready、archived 或 failed 但有已核对存档时报告「已在库中」，不 submit、也不自动 fetch。
archived 或 failed 且有存档时先用 `env-store fetch <fp> --wait` 取回，也可由主作业 submit 自动取回。
需要新建时，不带 `--yes` 只打印计划；带 `--yes` 使用本次计划哈希提交本人名下的 Slurm 构建作业，
`--wait` 只有配合 `--yes` 才等构建完成。已有构建/取回作业的复用仍交由服务器 submit 判定。

JSON 的 `project_environment` 列出所选 `<项目>/.pixi/envs/<环境名>` 是否存在、体积和 `du_status`。
体积使用 `du -s -k`，最多等 10 秒；超时或读取失败显示体积未知并给出 warning，不阻止收编。
检查本身失败则停止，不提交。没有项目环境时无需清理；存在时，待库中环境 ready 后按顺序做：

1. 用 `gen-sbatch --env <项目> -e <环境>` 重新生成完整作业脚本，或把已有脚本的环境激活改成
   `ENV1=$(env-store use <fp>) || exit 3` 加 `source "$ENV1"`。手改时按下一节保留 `# ENV-STORE` 标记，
   供 submit 检查状态、取回和 pin；运行命令直接调用锁定工具，避免继续用 `pixi run` 包装。
2. 跑一次作业，确认结果没有变化。
3. 再运行 `cleanup scan --path <项目>/.pixi/envs/<环境名>`，通过既有 `cleanup` 流程清理。
   spark-cleanup 会识别与项目环境库完全重复的环境，删除前逐个先问；以实际扫描结果为准。
   `env adopt` 不调用清理、不删除任何东西。以后再用 `pixi run`，缺失的项目环境会自己重装。

库里的环境是共享、只读的，不要再往里 `pip install`。要改依赖就改 manifest、更新锁、重新 build。

## 脚本、取回与复现

生成的作业片段遵守服务器契约：

```bash
# ENV-STORE {"fp": "<fp>", "project": "/path/to/project", "env": "default"}
ENV1=$(env-store use <fp>) || exit 3
source "$ENV1"
```

实际 fp 是安全的单个标识；示例中的尖括号须替换。标记里的 project 为服务器绝对路径。
`env check` 比较当前锁的 fp 和脚本 fp，并检查脚本 fp 的状态。锁改变时，选择重建并重新生成脚本，
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
- 不执行 post-link 或 `[activation]`。冒烟测试以调用者本人身份运行 `[tasks].smoke`。
- 环境对所有人只读，激活设置 `PYTHONNOUSERSITE=1`。不要往环境里 `pip install`；改 manifest、更新锁、重新 build。
- 最近一次使用后本地保留 60 天，之后仅留 NAS 存档；取回必须交 fetch 小作业。
- 不在 `/tmp` 下运行 Python 做测试：其他人遗留的 `.py` 可能遮蔽标准库。用项目中的干净测试目录。
