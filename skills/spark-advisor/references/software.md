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
