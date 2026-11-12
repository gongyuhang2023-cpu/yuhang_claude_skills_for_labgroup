# 查公共软件 / 数据库、申请安装

服务器上有一个**公共区**：管理员统一装好的工具（已在每个人的 PATH 里）和数据库（每个版本一个目录）。
用户要用某个工具或数据库时，**先查公共区有没有**，没有再申请。别让用户自己再装一份：那会变成重复安装，占全组的磁盘。

## 流程

1. **查**：`python scripts/advise.py software find <名字>`
   - 按名字和命令名匹配：搜 `blastn` 能找到 `blast`。名字拿不准就多试一两种写法。
     命令名沾边的包也会带出来（搜 `gtdb` 会带出 metaphlan，因为它有个 `sgb_to_gtdb_profile` 命令），看 `commands` 再判断。
   - `tools` 有结果：告诉用户直接敲命令就行（已经在 PATH 里）。
     `gen-sbatch` 生成的脚本是登录 shell（`#!/bin/bash -l`），也可以直接写命令名。
     **cron 和 `ssh spark '<命令>'` 这类非登录方式拿不到这条 PATH，要写绝对路径 `/opt/bio/bin/<命令>`。**
   - `dbs` 有结果：给**带版本号的路径**（`path` 字段），写进脚本结果才可复现。`…/current` 会随管理员更新而变。
     很多数据库还有现成的环境变量（如 `$CHECKVDB`、`$GTDBTK_DATA_PATH`），以 `rules` 的输出为准。
     这些变量都指向 `current`（如 `GTDBTK_DATA_PATH=/opt/bio/db/gtdbtk/current`、`BAKTA_DB=/opt/bio/db/bakta/current/db`）。
     要锁版本，就在命令前 export，把 `current` 换成版本目录：`export GTDBTK_DATA_PATH=/opt/bio/db/gtdbtk/r226`。
   - 有 `warning` 说面板没认出这台电脑：能查，不能提交。告诉用户先连上 Tailscale，或者找管理员登记设备。
2. **已经有人申请过**（`requests` 里有同名的，`status_cn` 是「待处理」或「已接受，安装中」）：不要重复提交，建议用户 +1。
   - `voted_by_me` 为 true 说明用户已经投过，告诉用户就行，不用再投。
   - 先不带 `--yes` 跑 `software vote <id>`，把要做的事给用户看。
   - 用户同意后再跑 `software vote <id> --yes`。这条命令只加票不撤票：已经投过的会直接返回 `already_voted`。想撤票的话，让用户在网页上点。
3. **公共区没有、也没人申请过**：先确认 ARM 上能不能装。
   - `software find` 找不到时会自动带上 `arch`，也可以手动加 `--arch`。
   - 服务器是 **ARM（aarch64）**：如果 bioconda 和 conda-forge 都没有 `linux-aarch64` 或 `noarch` 构建，就装不上。
     直接告诉用户，再找替代工具（比如 roary 可以换成 panaroo），别提交注定办不了的申请。
   - 能装的话先起草：`software request --kind tool|db --name <名字> [--version …] [--source …] [--reason …]`。
     不带 `--yes` 只出草稿。`reason` 写清楚用途、急不急、是不是已经自己装了一份（装在哪）。
   - **把草稿给用户看，用户明确同意后**才加 `--yes` 提交。申请全组都看得见。
   - 面板会把同名申请合并成一条（等于 +1），不用担心重复。
4. **等不及**：给用户 `pixi global install -c conda-forge -c bioconda <名字>`，装在他自己的 `~/.pixi`。
   同时提醒：等申请变成「已安装」，就用 `pixi global uninstall <名字>` 删掉个人版，不然它会出现在「我的重复安装」里。

## 规矩

- **提交和 +1 必须在用户自己的电脑上跑**：面板按发出请求的设备认人。
  - AI 如果运行在服务器上，`--yes` 会被拒绝。
  - 这时让用户去面板网页提交，地址在 `page` 字段里。
- 不替用户编造用途。`reason` 用用户的原话，或者先经用户确认。

## 放在 NAS 上的数据库

`software find` 的数据库结果里有这几项：

- `location`：库放在哪。`local` 是本地盘；`nas` 是整个库在 NAS；`mixed` 是本地和 NAS 各有一部分（比如 UHGV：分类工具用的库在本地，官网全套在 NAS）。
- `nas`：NAS 上对应的路径。它也可能只是下载包或归档，不一定能直接拿来跑。
- `aliases`、`matched_alias`：库里包含的子库和别名。搜 `UHGV` 能搜出 virodb，就是因为 virodb 里包含 UHGV。
- `cache_status`、`cache_usage`：NAS 上的文件有没有取到本地缓存、怎么取。「已缓存」可能只是其中一部分文件，要用的那个以 `db-cache path` 为准；查询失败时会单独说明，不等于没缓存。

NAS 读起来慢（约 100 MB/s），也不适合随机读。查几条记录可以直接读；要拿整个文件跑比对、建索引之类的，先用 `db-cache` 取到本地缓存：

- **在作业里用（首选）**：`gen-sbatch --stage <库路径>`，可以写多个。生成的脚本开头会有 `D1=$(db-cache use <库路径>)`，命令里用 `$D1`、`$D2`。提交时没缓存的先交一个 1 核 2G 的取库作业，主作业等它完成再跑。细节见 `submit-flow.md`。
- **手动取**：`db-cache fetch <库路径> --wait`，取完打印本地路径（`/scratch/db-cache/...`）。它也会提交作业，同样要用户同意。
- **看缓存**：`db-cache list` 列出缓存了什么、多大、谁在用；`db-cache path <库路径>` 看某个文件在不在。
- `<库路径>` 写 `/opt/bio/db/<名>/<版本>/<文件或目录>`，`current` 会换成实际版本。只取要用的文件，别把整个库都取下来。

缓存的规矩，用户问起时照说：

- 缓存只读，取完和清单核对过 sha256 才能用；清单里没有校验值的，会标「未与官方校验值核对」。压缩包不会自动解开。
- 取之前要保证取完后本地还剩至少 350 GB，不够就拒绝，并说明差多少。
- 刚取完 48 小时内、有作业在用或排队、有进程开着的不会清；其余 14 天没人用就清掉，本地快满时先清最久没用的。清掉了下次再取就行，NAS 上的原件一直在。
- 别把 NAS 上的库自己复制一份到 home：会被查成重复安装，也占全组的盘。
