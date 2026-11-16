# 公共数据库：查、自己装进 NAS 公共库、取进缓存、锁版本、个人副本

> 什么时候读：用户问「有没有某个数据库」「要哪个版本」「缺数据库、自己装一个」「脚本用的是哪个版本的库」「锁定数据库版本」「我自己下了一份库，能不能放进公共区、能不能删」，或者作业要用 NAS 上的库时。
> 公共工具（命令）看 `software.md`。

公共数据库在 `/opt/bio/db/<名>/<版本>/`，一个版本一个目录；`/opt/bio/db/<名>/current` 指向其中一个版本。库的文件可能在服务器本地盘，也可能在 NAS 上（读起来约 100 MB/s）。

    python scripts/advise.py database find <名>
    python scripts/advise.py database plan <名> --url <下载地址> [--url …] [--version <版本>] [--recipe <配方>] [--request <申请编号>]
    python scripts/advise.py database install <同 plan 的参数> --expected-plan <plan_sha256> --yes
    python scripts/advise.py database status [<作业号>]
    python scripts/advise.py database scan|check <项目>
    python scripts/advise.py database lock|pin <项目> [--db 名=版本 …] [--yes --plan-id <id>]
    python scripts/advise.py database compare <目录> <名> [<版本>] [--yes | --result <作业号>]
    python scripts/advise.py database adopt <名> <版本> --path <目录> [--plan-job | --result <作业号>]
    python scripts/advise.py database adopt <名> <版本> --path <目录> --expected-plan <plan_sha256> --yes
    python scripts/advise.py database request --name <名> [--version …] [--source …] [--reason …] [--yes]
    python scripts/advise.py database vote <申请编号> [--yes]

输出都是 JSON（写不写 `--json` 都一样）。会写东西的命令不带 `--yes` 只预演。

## 1. 先查：`database find <名>`

- 按库名、子库别名、版本号匹配：搜 `UHGV` 能找到包含它的 virodb。拿不准就多试一两种写法。
- 每个库列出全部版本（新的在前），每个版本有：
  - `path`：带版本号的路径。**脚本里写这个**，结果才可复现。
  - `current`：是不是 current 指向的版本。
  - `location_cn`：本地 / NAS / 两边都有（比如工具用的部分在本地，官网全套在 NAS）。
  - `install_method`：申请 #编号 / 自助（谁装的）/ 管理员直接装。
  - `verification`：`official` 已和官方校验值核对；`self` 上游没有校验值，只核对了下载完整；`local-copy` 是收编的个人副本，未与官方核对。
  - `compat`：只有和工具绑定的库（GTDB-Tk、Bakta、CheckV、geNomad）才有：`pass` 公共区的工具读得了，`fail` 读不了，`untested` 没测出来。
  - `cache_status`：NAS 上的库有没有取进本地缓存。`cache_hint` 有内容时照说：库装好了，但本地剩余不足，暂时取不进缓存。
- `requests` 是有没有人申请过（§5），`installs` 是正在装或装失败的记录。
- 有 `warning` 说面板没认出这台电脑：能查，不能申请。

## 2. 公共区没有：自己装进 NAS 公共库

不用审批，谁都可以装，装好全组都能用。

1. **预演**：`database plan <名> --url <下载地址> …`
   - 地址用官方下载页上的公网 https 地址，一个文件一个地址。不写 `--version` 时，只有网址里有唯一、明确的版本号才推断得出来；推断不了就问用户写哪个版本。
   - 是在回应面板上的某条申请，加 `--request <编号>`，面板会显示「申请 #编号」。
   - 看返回的 `status`：
     - `exists`：公共区已有同一版本，直接用 `path`，不用装。
     - `blocked`：NAS 装完会低于 2 TB 底线，装不了，照 `error` 告诉用户。
     - `ready`：可以装。`files` 是要下载的文件、大小和校验值来源，`nas.after_install` 是装完 NAS 还剩多少。
   - 有 `other_versions` 和 `ask_user`：公共区已经有这个库的别的版本。**先问用户**是用已有的版本，还是另装一版，别替用户定。
   - 有 `cache_note`：装得上，但装好后本地剩余不足 350 GB，暂时取不进缓存（§3）。有 `verification_note`：上游没给校验值，只能核对下载完整。
2. **给用户看计划**：库名、版本、文件和大小、校验值来源、NAS 余量；有别的版本时说清是另装一版。**用户明确同意**再往下走。
3. **安装**：运行返回的 `install_command`（`database install … --expected-plan <plan_sha256> --yes`）。
   - 服务器会重新读一遍上游、重算 plan_sha256，和预演的对不上（上游文件变了、空间情况变了）就不提交：重新预演、重新确认。
   - 提交的是以用户名义的一个 Slurm 作业（2 核 4G），在作业里下载、核对、打包、登记，返回 `job_id`。
4. **看进度**：`database status [<作业号>]`，`status_cn` 是排队中 / 安装中 / 装好了 / 失败 / 公共区已有。装好后 `database find <名>` 能查到，面板的「安装记录」里也有。

规矩：
- **只用内置配方**：`generic` 只下载、核对、归档，不运行建索引之类的命令；`gtdbtk`、`bakta`、`checkv`、`genomad` 还会检查公共区的工具读不读得了这个库。不写 `--recipe` 时按库名选。
- **不执行用户提供的命令**：安装请求里不能带命令、建库步骤或脚本。用户想「下载后再跑某个命令」，告诉他装好后在自己的作业里跑。
- **KEGG 这类要订阅的库不收**，服务器直接拒绝。
- NAS 上还放着大家 home 的备份，**装完 NAS 至少要剩 2 TB**。
- 官方给的 `.tar.gz` 会转成 `.tar.zst` 包加成员清单存进 NAS（NAS 上不展开小文件），取进缓存时再解开。
- 别把公共库自己再下一份到 home：会被查成重复安装，也占全组的盘。

### current 怎么定

- 公共区还没有这个库：第一次装的版本直接设成 current。
- 不和工具绑定的库：新版本能明确排出比现在的 current 新（数字版本、r / v 前缀、日期）才切过去；排不出先后不切；补装旧版不会把 current 拉回去。
- 和工具绑定的库（GTDB-Tk、Bakta、CheckV、geNomad）：还要公共区的工具读得了（`compat=pass`）才切；读不了或没测出来就不切，结果里带 `warning`。工具升级后重新检查时，current 读不了只报警，不回退。
- **current 只在第一次挑版本时用。** 分析一律写带版本号的路径，并锁进项目清单（§4）：current 和默认变量（如 `$GTDBTK_DATA_PATH`、`$BAKTA_DB`、`$CHECKVDB`，都指向 current）以后会变。

## 3. NAS 上的库：取进本地缓存

NAS 不适合随机读。查几条记录可以直接读；要拿整个文件跑比对、建索引之类的，先取进本地的共享缓存（`/scratch/db-cache`）：

- **在作业里用（首选）**：`gen-sbatch --stage <库路径>`，可以写多个。生成的脚本开头有 `D1=$(db-cache use <库路径>)`、`D2=…`，命令里用 `$D1`、`$D2`；`current` 在生成时就换成实际版本。
  - `submit` 时没缓存的先交一个 1 核 2G 的取库作业，主作业用 `--dependency=afterok:<取库作业号>` 等它完成，并马上登记，免得排队期间缓存被清掉；已经缓存的直接提交。
  - `submit --check-only` 只看计划（会不会先取库、依赖哪个作业），不取库、不提交。**总闸不变**：先把资源和计划给用户看，用户同意才 submit。
- **手动取**：`db-cache fetch <库路径> --wait`，取完打印本地路径。它也会提交作业，同样要用户同意。
- **看缓存**：`db-cache list` 列出缓存了什么、多大、谁在用；`db-cache path <库路径>` 看某个文件在不在。
- `<库路径>` 写 `/opt/bio/db/<名>/<版本>/<文件或目录>`，只取要用的部分，清单里没有的文件会被拒绝。
- 打包存放的库（`<版本>.members.tsv` 里列了成员的 `.tar.zst` 包）：`<库路径>` 写包本身（`…/<包>.tar.zst`）或它所在的目录。取到本地后先核对包的 sha256，再按成员清单逐个核对、解开，`db-cache use` 返回解开后的目录。包要整个取，不能只取包里的某个文件；成员清单里没有的压缩文件原样取。
- 手写的脚本也要用 `db-cache use` 登记，不然缓存可能被清掉。

缓存的规矩，用户问起时照说：
- 缓存只读，取完和清单核对过 sha256 才能用；清单里没有校验值的会标「未与官方校验值核对」。
- 取之前要满足：本地剩余 − 这次还缺的部分（包按解开后的大小算）− 其中最大的一个包 ≥ 350 GB（解开期间包要先放在本地）。不够就拒绝，并说明差多少。这时可以先直接读 NAS，或等本地腾出空间；库本身已经在 NAS 公共库里。
- 刚取完 48 小时内、有作业在用或排队、有进程开着的不会清；其余 14 天没人用就清掉，本地快满时先清最久没用的。清掉了下次再取，NAS 上的原件一直在。

## 4. 锁版本：项目数据库清单 `databases.lock.json`

项目用到哪些公共库、锁定哪个版本，记在项目根目录的 `databases.lock.json`，**进 git**。`<项目>` 是项目目录：在用户自己的电脑上有这个目录，就扫、改电脑上的那份；没有就当成服务器上的路径。

- `scan`：只看，脚本里用到哪些库、哪个版本（`databases` 是按库汇总）。
- `check`：核对脚本和清单，给出错 / 警告和退出码（下表），每一项带 `fix`。
- `lock`：预演要写进清单的条目，给出 `plan_id`；用户同意后带 `--yes --plan-id <id>` 写。
- `pin`：预演把脚本改成版本路径的 diff（`changes`）和清单的改动（`lock_diff`）；用户看过、同意后带 `--yes --plan-id <id>` 才改脚本，同时更新清单。
- 预演之后项目或库的信息变了，`plan_id` 对不上，会被拒绝：重新预演、重新确认。

scan 认这些写法：
- 字面路径 `/opt/bio/db/<名>/<版本或 current>/…`，包括 `db-cache use` 的参数。
- 变量：`$VAR`、`${VAR}`，R 的 `Sys.getenv("VAR")`，Python 的 `os.environ["VAR"]`、`os.environ.get`、`os.getenv`。VAR 是登录环境里指向公共库的变量，或项目自己设成公共库路径的变量。项目里 `export` / `Sys.setenv` / `os.environ[...] =` 设过的，按设的值算。
- 调用了绑定工具却没设对应变量：`gtdbtk`（`GTDBTK_DATA_PATH`）、`bakta`（`BAKTA_DB`）、`checkv`（`CHECKVDB`）。不设的话工具读服务器的默认值，也就是 current。`bakta --db`、`checkv -d` 显式写了库路径的不算。
- 个人目录里像数据库的路径（`~/…`、`/home/<用户名>/…` 里带库名，或目录名是 db / database）。
- current 只看它此刻指向哪个版本，也就是今天的版本。

清单里每项：`name`、`version`、`path`、`env`（项目用哪个变量指它）、`fingerprint`、`verification`、`tool`（绑定工具的名字和锁定时的版本）、`locked_at`、`locked_by`。`fingerprint` 是这个版本全部文件（包括包里的成员）的（相对路径、字节数、sha256）排序后整体算的 sha256；有文件缺校验值时前面带 `partial:`。

check 的判断：

| 退出码 | 情况 |
|---|---|
| 2 错 | 清单里的版本在公共区不存在；指纹变了；用了 current（字面路径、`db-cache use` 里、没改掉的默认变量、调用了工具却没设变量）；脚本里的版本和清单不一致；脚本里的版本不存在；绑定库 `compat=fail` |
| 1 警告 | 脚本用到的库不在清单里；还没有清单；指纹 partial 或清单里没有指纹；工具版本变了；NAS 每日核对报错；用了收编的个人副本或个人目录里的库 |
| 3 判断不了 | 库的信息取不到；清单读不懂；指纹查不到；变量设成了动态值，看不出指向哪个版本 |
| 0 | 没有问题 |

既有错又有判断不了的，返回 2。

流程：
1. `check`，照 `errors`、`warnings` 的原话和 `fix` 讲给用户。
2. 还没有清单：`lock` 预演，给用户看要锁的版本，同意后写。用了 current 的，按今天的 current 锁。
3. 脚本里有 current 或默认变量：`pin` 预演，把 diff 给用户看，**用户同意后**才改。
   - current 换成清单里的版本，清单里没有就用今天的 current。
   - 默认变量和没设变量的工具调用：shell 脚本在头部（`#SBATCH` 和 `set` 之后）加 `export 变量=版本路径`；Python 在 `import os` 之后加 `os.environ[...] = ...`；R 脚本加 `Sys.setenv(...)`。别的文件（Rmd、Nextflow、Makefile 等）列在 `manual`，照说明手动改。
4. 同一个库落到两个版本（脚本之间，或脚本和清单）：`conflicts` 列出各自在哪，问用户统一到哪个版本，再用 `--db 名=版本`。换版本会影响结果。
5. 改完把脚本和清单一起提交进 git，再 `check` 一次。

- **改在用户平时改代码的那份上**：代码在用户电脑上就改电脑上的，再照常同步到服务器；只在服务器上的就改服务器上的。
- 在服务器上 `pin` 时，项目下有运行或排队的作业就不改（排队的作业启动时会读到改过的脚本），等作业结束。
- `project check` 的 `db_refs` 用同样的规则扫描。
- 写论文以出结果那天的作业日志、运行记录为准；锁了清单之后，`check` 能说明库的内容有没有变过。

## 5. 申请（等管理员装）

- `requests` 里已有同名同版本的申请（「待处理」或「已接受，安装中」）：不重复提交，建议用户 +1。`voted_by_me` 为 true 就是投过了。先不带 `--yes` 跑 `database vote <编号>`，用户同意后加 `--yes`；只加票不撤票，想撤票让用户在网页上点。
- 没人申请过：`database request --name <名> [--version …] [--source <官方地址>] [--reason …]` 出草稿，用户明确同意后加 `--yes`。`reason` 写清用途、急不急、是不是已经自己下了一份（在哪），用用户的原话。申请全组都看得见；同名同版本的会合并成一条。
- 提交和 +1 必须在用户自己的电脑上跑：面板按发出请求的设备认人，AI 在服务器上时 `--yes` 会被拒，这时让用户到面板网页提交（`page` 字段）。
- 用户愿意自己装的话，§2 更快，不用等。

## 6. 个人副本：比对、放进 NAS 公共库、删掉

用户自己在 home 里下过一份库时：

1. **比对**：`database compare <目录> <库名> [<版本>]`
   - 不超过 2 GB、文件不多的当场算；更大的给出一个低优先级作业的计划（1 核 2G，排在所有人的普通作业之后），用户同意后加 `--yes` 提交，算完用 `--result <作业号>` 取结果。
   - 逐个文件和公共版本比，官方压缩包里的成员也比。`match` 有值就是公共区已有同一份；`versions` 里每个版本给出对得上的文件数和不同、多出、缺少的文件。
   - `level_note` 说明这份和官方包在哪一层对上：同一层、在官方包的顶层目录里面，或者多了一层 `<版本>/`。`adopt_path` 是收编时 `--path` 该指的那一层。
2. **公共区已有同一份**：脚本改用公共版本的路径（`pin`），再按第 4 步删个人副本。
3. **对不上任何公共版本**：`database adopt <库名> <版本> --path <目录>` 预演，把这份放进 NAS 公共库。
   - 先看第 1 步 compare 的结果：判一致（`match` 有值）就不用收编，按第 2 步直接用公共版本、清理个人副本。要收编时，`--path` 指到和官方包相同的那一层（compare 给的 `adopt_path`）。打包存放的官方版本只按包内成员（加上不在包里的文件）比；成员都带 `<版本>/` 时剥掉这一层，但上游包自带的顶层目录（如 `checkv-db-v1.5/`）不剥。`--path` 指在这层目录里面，就会被判成不同、登记成 `<版本>-local-<用户名>`。
   - 预演要逐个文件算 sha256。目录不超过 1 GB 当场做；更大的不在登录节点上算，返回一个低优先级作业的计划（1 核 2G、时限 1 天，排在所有人的普通作业之后）。用户同意后加 `--plan-job` 提交，最多等 45 秒：算完就直接返回预演，没算完给作业号，之后用 `--result <作业号>` 取。
   - 和已登记的官方版本按上面的规则对得上，才沿用官方版本号；否则登记成 `<版本>-local-<用户名>`，面板标「本地副本，未与官方核对」。
   - 目录里不能有链接、特殊文件或不属于本人的文件。
   - 预演结果按 plan_sha256 存在服务器上本人的 `~/.spark-advisor/db-adopt/` 里，提交时用它。
   - 用户同意后带 `--expected-plan <plan_sha256> --yes` 提交。提交时只做轻量核对（目录还在、属主是本人、文件数和总大小和预演一致），不再把整个目录重算一遍；逐个文件的校验放在安装作业里，和预演的指纹对不上，作业就失败、不登记。所以**作业结束前别改这个目录**；改过就重新预演。收编在用户自己的作业里打包传给服务端，原目录不动。
4. **删个人副本，必须同时满足**：公共版本已经登记，且和这份对得上（compare 的 `match`，或刚收编的就是这一份）；用到它的脚本已经改成公共路径并提交；没有运行或排队的作业在用它。删除走 `spark-cleanup`（见 `cleanup.md`），每一份都先问用户。

## 规矩

- 总闸照旧：`install`、`adopt --plan-job`、`adopt --yes`、`compare --yes`、`lock --yes`、`pin --yes`、`request --yes`、`vote --yes` 都要先给用户看计划、用户明确同意。
- 跟用户说「放进 NAS 公共库」，不说「备份」：放进去的是全组共用的公共库。
- 不替用户编造版本号和用途。
