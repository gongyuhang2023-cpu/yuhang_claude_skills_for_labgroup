# 冷数据：搬到 NAS、搬回来、记清单

> 什么时候读：用户要腾空间，或说「把 X 搬到 NAS」「把 X 搬回来」「我搬过什么」时。为腾空间而搬，先按 `storage.md` 的顺序查；只是搬回、看清单，不用。
>
> `/ai.md` 写的搬运命令是 `nas-archive --delete`。`spark-cleanup` 也是管理员部署的：搬到 NAS 上同一个个人目录，原处留同名的说明文件（`<目录名>.ON_NAS.txt`），另外多做几道检查（引用、在不在用、备份计划、逐个文件读回核对）。这一处用 `spark-cleanup`，不算和 `/ai.md` 冲突。

逻辑都在服务器命令 `spark-cleanup` 里（以用户自己的身份运行，默认只看不动），本 skill 只转发：

    python scripts/advise.py cleanup cold [--min-gb 5] [--days 60]       # 列出冷数据，只看不动
    python scripts/advise.py cleanup apply <目录> --action archive          # 预演
    python scripts/advise.py cleanup apply <目录> --action archive --yes    # 提交搬运作业
    python scripts/advise.py cleanup job <作业号>                           # 看进度和结果
    python scripts/advise.py cleanup archived                              # 搬走了什么（不动服务器，只重写本机的个人清单）
    python scripts/advise.py cleanup restore <目录> [--yes]                 # 搬回原处
    python scripts/advise.py cleanup note <目录> "<备注>"                   # 给搬走的东西写备注（只存在本机）

`cold` 和 `archived` 要扫整个 home，可能要几分钟。路径用绝对路径，或带引号的 `'~/…'`：不加引号，Git Bash 会把 `~` 换成本机的 home。

## 挑：`cold`

列出 home 里深度 3 以内、大于 `--min-gb`、文件 `--days` 天没改过的目录。

- 默认 60 天、5 GB，会漏掉刚闲了一两个月的大目录。用户嫌腾得不够、或想看全一点，放宽到 `--days 30 --min-gb 1` 再跑一次。
- 点开头的目录（`~/.cache`、`~/.boltz` 这类缓存）`cold` 不列，也不搬：缓存删了会重新下载。用只读的 `du` 看大小，列给用户，由用户自己删。

每项看 `suggest.action`：

| action | 意思 | 怎么做 |
|---|---|---|
| `archive` | 可以搬 | 和用户确认后搬 |
| `push` | 代码还没推完 | 先 `project check` / `push`（见 `code-check.md`），再搬；`why` 里写了差什么 |
| `refs` | 还有脚本写着这个路径 | 搬走后那些脚本会找不到文件；用户确定不再跑才搬 |
| `dedupe` | 里面有公共数据库的副本 | 不用搬，走 `cleanup scan`（删掉或换成链接，见 `cleanup.md`） |
| `wait` | 正在被进程或作业使用 | 等用完再说 |
| `keep` | 先别动（在备份计划里、引用没查完等） | 照 `why` 原话告诉用户 |

## 搬：`apply --action archive`

- 先预演，把 `plan`（NAS 上的目标、大小、文件数、预计分钟、git 状态、步骤）讲给用户。**用户逐项点名同意才加 `--yes`。**
  - 预演会顺带查引用（最多两分钟）：`plan.refs` 不为空，说明还有脚本写着这个路径，搬走后它们会找不到文件。
  - `warnings` 照原话转告。里面有「交上去会先排队」时，告诉用户机器正忙：作业要 `plan.job_resources` 写的核数和内存（固定 2 核 / 8G，调不了），会等别人的作业结束才开始。要估等多久，看 `status` 里在跑作业的剩余时间（`time_left`）。
- `--yes` 提交成 Slurm 作业（断开 ssh 照常跑）；同时交好几个会排队依次跑，不会互相撞。
- 作业依次：打包压缩 → 从 NAS 读回、逐个文件核对 sha256 → 确认搬运期间目录没被改过 → 原处留 `<目录名>.ON_NAS.txt` → 先改名再删本地。任何一步没过，本地都不动。
- 用 `cleanup job <作业号>` 看进度。结束后报告：NAS 上的包、腾出了多少、说明文件在哪。
- 预演的「预计分钟」只算了 NAS 写一遍、读回一遍，没算压缩，实际会长一些。
- pip 的 venv（比如 `~/boltz-env`，没有 `conda-meta`）不算 conda 环境，按普通目录搬就行：原路径搬回，照常能用。
- 会被拒绝的：点开头的目录、conda 环境（走 `cleanup.md` 里的 `--env`）、单个文件、有 FIFO / 读不了 / 不属于用户的文件、在备份计划里列着的、正在用的。照 `refuse` 原话告诉用户，**不要想办法绕过去**。

## 记：个人清单和备注

- `cleanup archived` 列出原处的全部说明文件（包括管理员替用户搬的）、NAS 上的包在不在，并重写本机的 `references/nas-archive.local.md`。
- 服务器上原处的说明文件才是准的，清单只是方便查。
- 清单的「大小」是搬走前的原始大小。写着「压缩后」的，是管理员用 `nas-archive` 搬的，旧格式的说明文件没记原始大小。
- 备注存在 `references/nas-archive.local.json`（第一次写备注时生成）。
- **搬之前问用户一句这批数据是什么**（比如「某课题的原始测序」），搬完用 `cleanup note <目录> "<备注>"` 记下。半年后用户说「把那个课题的原始数据搬回来」，能从清单里对上。

## 搬回：`restore`

- 先预演：会核对配额，并保证搬回后本地剩余空间不低于 `/ai.md` 的线；原处已经有东西就拒绝，不覆盖。`refuse` 为空就说明空间和配额都够，预演里不单列这两个数。
  - `warnings` 的处理和「搬」一样，排队提示照原话转告。
  - 「预计分钟」只算了从 NAS 读包，解压另算，实际会长一些。
  - `plan.bytes_estimated` 为 true 时，大小是按压缩包 ×3 估的（旧格式的说明文件没记原始大小），跟用户说「约」。真执行时作业会先算准再核对空间。
- `--yes` 提交成作业：解压到临时目录 → 核对 → 原样放回 → 删掉说明文件。**NAS 上的包保留。**
  - 读写执行权限、软链接、硬链接都照原样；只有 setuid / setgid / 粘滞位会去掉。
- 结束后用 `cleanup archived` 刷新个人清单。
- 管理员用 `nas-archive` 搬的，说明文件里写着一条手动解压命令（`tar -I zstd -xf …`）。别为了不排队照它在 shell 里直接跑：解压几十 GB 是重活，要走 Slurm；手动解压也不核对 sha256、不删说明文件。
