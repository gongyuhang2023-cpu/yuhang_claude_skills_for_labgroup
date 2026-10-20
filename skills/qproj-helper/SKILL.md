---
name: qproj-helper
description: |
  qproj 工作流指导助手（按 qproj 0.12.0 写）。当用户提到"qproj"、"创建qmd"、"新建分析步骤"、"初始化分析项目"、
  "use_qmd"、"analyses目录"、"path_target"、"path_source"、"proj_doctor"、"lineage"、"运行记录"、"qg"时触发。
  分析用户当前项目状态，给出应在 R Console 中执行的命令。
  区分两类工作：你直接编辑 .qmd 并可跑非交互命令（quarto render / qg / Rscript -e）；
  项目脚手架等交互操作则给用户可粘贴的 R Console 命令。
---

# qproj Helper — 工作流指导助手

## 核心定位

你有两类工作，别混为一谈：

- **A. 在已有 qproj 项目里写/改分析代码（最高频）**——你直接用 Edit/Write 改 `.qmd`，并用 Bash/PowerShell 跑非交互命令。见**场景 0**。
- **B. 指导用户在 R Console 手动执行**——建项目、开项目、交互式建步骤等需要活着的 IDE session、或属于结构决策的操作。给可粘贴命令 + 一句话说明。

**谁来跑（分工判据）**：

| 操作 | 谁 | 命令 |
|------|-----|------|
| 渲染某步骤 | 你 | `quarto render analyses/NN-step.qmd --to gfm` |
| 查/扫依赖图 | 你 | `bash .qproj/graph/qg status` / `qg <cmd>`；`Rscript -e "qproj::proj_scan_graph()"` |
| 体检缓存 / 库设置 | 你 | `Rscript -e "qproj::proj_doctor()"`（静态检查，只读） |
| 查哪些步骤的输入在它上次运行后变了 | 你 | `Rscript -e "qproj::proj_lineage_check()"`（读运行记录，只读） |
| 查/装/更新依赖 | 你 | `Rscript -e "qproj::proj_check_deps()"` 等（非交互、只写文件） |
| 建项目/工作流/新步骤 | 用户 | 给用户 Console 命令；建 qmd 是结构决策，交给用户在环内（见场景 2，不代跑） |
| 逐 chunk 跑 / 活 session 渲染 | 用户 | 涉及全局环境 path 绑定，见「交互模式注意事项」 |

给用户 Console 命令时，格式统一：
```
请在 R Console 中执行：
<代码块>

这会：<一句话说明效果>
```

> qproj 的 path 绑定（`path_target` 等）是 R **session 级**状态，项目结构又是用户的决策——所以 B 类默认交给用户，你只在明确安全的非交互操作上自己动手。

**例外：项目在远端（spark / WSL）时，B 类的「建项目/工作流/新步骤」由你非交互跑。**
上面那条纪律的前提是「用户面前有一个活着的 R Console」——远端没有（比如 IDE 的远程会话用不了时）。把命令给用户，他也无处可粘。

```bash
# spark：R 是公共的 /opt/bio/bin/Rscript，qproj 由管理员装在公共库 /opt/bio/R/4.5/library。
# 2026-10-08 起学生 conda 环境里的 R 已移除 —— 不要用 conda/pixi 装 R 或 R 包；
# 缺包就在公共 R 里 install.packages() / remotes::install_github()，自动进个人库。
# 目录里已有 .git 时，非交互下 usethis 会判「嵌套项目」直接 abort → 先开 allow_nested_project
ssh -n spark "cd <项目路径> && /opt/bio/bin/Rscript -e '
  options(usethis.allow_nested_project = TRUE)
  usethis::create_package(\".\", open = FALSE)
  qproj::proj_use_workflow(\"analyses\")
'"
```

- **仍然照 single source of truth 走 qproj 的函数**，不手抄模板（那条纪律与谁来跑无关）。
- **结构决策仍归用户**：步骤名、编号、要不要新开 qmd，先问清楚再跑。
- 一次 `Rscript -e` 是一个新 session，不存在 path 绑定残留 —— 这正是原纪律要防的东西，
  在非交互模式下自动不成立。

> **版本**：本 skill 按 qproj 0.12.0 写（spark 公共库 2026-10-20 为 0.12.0 @40219c7）。
> 行为与本文不符时先 `packageVersion("qproj")`：低于 0.2.0 的旧装法里越序读取只警告、`clean` 默认 `TRUE`。

**规则的归宿（架构要点）**：要让一条规则在 Claude **日常写代码时自动生效**，它必须进**项目 `CLAUDE.md`**（每 session 自动加载）；留在本 skill 只有 `/qproj-helper` 被调用时才读得到，平时不生效。三层分工——

- **项目 `CLAUDE.md`**（自动加载、常驻）= Claude 写代码时自动遵守的规则：path 绑定铁律、内容组织判据……
- **本 SKILL.md**（`/qproj-helper` 时）= 顾问决策树 + **把规则写进那份 `CLAUDE.md`**
- **`references/`**（按需读）= 详版 + 范例

所以本 skill 的一个核心产出，是把规则**落进项目 `CLAUDE.md`**（场景 1.5 新建时、场景 9 补写时），而非把规则留在自己肚子里。

## 决策树

收到用户请求后，按以下顺序判断：

### 场景 0：在已有 .qmd 里写 / 改分析代码（你自己动手，最高频）

**信号**：项目已是 qproj 结构（`analyses/*.qmd`，每个文件顶部有 `setup` chunk），用户让你实现或修改某步分析。**直接编辑 `.qmd` 的 `## Tasks` 区，别让用户代劳。**

**动手前**：读项目根 `CLAUDE.md`（若有）+ 目标 `.qmd` 的 `setup` chunk，确认约定，别凭记忆重构。

**Tasks 里的 qproj 铁律**：

```r
# 写产出：只写自己步骤的目录，永远用 path_target()，别手拼 here::here("data", ...)
saveRDS(res, path_target("result.rds"))
write.csv(df, path_target("table.csv"), row.names = FALSE)

# 读上游"产出"（别人用 path_target 写的）：用 path_source(步骤名, 文件名) —— 两个参数
df <- read.csv(path_source("010-import-metadata", "meta_clean.csv"))

# 读本步自己的原始输入 / 项目共享资源（都在 00-raw 下，字符串绑定）：
raw <- readr::read_tsv(file.path(path_data, "samples.tsv"))      # data/00-raw/d<本步>/
ref <- readr::read_tsv(file.path(path_resource, "ref.tsv"))      # data/00-raw/d00-resource/

# ✗ 别读别的步骤的 d<step>/，如 here::here(path_raw, "d070-prophage-census", ...)：
#   运行时不拦，但 qg bypass 会把它列为反模式，而且这次读取不进运行记录 ——
#   那份文件改了，proj_lineage_check() 照样报本步 ok（0.12.0 实测）。
# ✓ 让拥有它的步骤用 path_target() 发布（清洗或原样），下游 path_source() 读；
#   多个步骤都要的外部大件（参考库、外部工具的整批输出）放 d00-resource/。
```

**关键点**：
- `path_source` 是 setup 里建好的**闭包**，调用形如 `path_source("步骤名", "文件名")`。**不要**写 `qproj::proj_path_source("步骤名","文件名")`——那是工厂函数，只收一个参数（当前步骤名），两参会报错。
- 顺序校验：读编号不小于自己的步骤 → **直接中止**（`qproj_order_error`）；`00-raw` 永远允许。`options(qproj.order_check = "warn")` 只用于临时排查，别写进项目代码。
- 身份校验：渲染时 `params$name` 必须等于文件名去掉 `.qmd`，否则 `qproj_identity_error` 中止 —— 所以别复制已有 `.qmd` 改名当新步骤（场景 2）。
- 加代码就加 chunk 放进 `## Tasks`；文件末尾列产出的 `list-files-target` chunk 保持在最后（新模板里它还调 `qproj::proj_run_finish(params$name)` 收尾运行记录，别删）。
- 别跨 `.qmd` 复用 path 绑定：每个文件的 setup 拥有自己的绑定。
- **缓存逐 chunk 开**：`_quarto.yml` 不设 `cache`；慢 chunk 自己加 `#| cache: true`。缓存 chunk 读文件 → 加 `#| cache.extra: !expr tools::md5sum(path_source("步骤", "文件"))`，否则上游重算后它回放旧结果；缓存 chunk **不写文件 / 表**（命中缓存时写入不发生）。改完跑 `proj_doctor()` 核一遍。
- **项目数据库**用 `con <- qproj::proj_db_connect()` / `qproj::proj_db_disconnect(con)`（建库那一步 `create = TRUE`），别在 `R/utils.R` 另写 `load_db()`；0.12 起每步读写了哪些表也进运行记录（场景 12）。
- 跨多步骤数据流的大改动（改产出文件名/格式、删 `saveRDS`、调步骤顺序）→ 先跑 `qg impact`（场景 8）看影响面再动手；单步内部小改（加图、调参）不必。

> 代码怎么组织——**何时另开 `.qmd`、何时把逻辑下沉到 `R/`、怎么让分析产出直接对接论文**——见**场景 9**。

### 场景 1：从零创建项目

**信号**：用户说"新建项目"、"创建分析项目"、"初始化"，且当前目录无 DESCRIPTION。

```r
# 步骤 1：创建项目脚手架（目录名可带编号如 p0101-example；name 给合法的 R 包名）
qproj::proj_create("项目路径", name = "p0101example")

# 步骤 2：在 Positron（推荐）或 RStudio 中打开该文件夹，然后执行：
qproj::proj_use_workflow("analyses")

# 步骤 3：创建第一个分析步骤
qproj::use_qmd("01-步骤名", path_proj = "analyses", open = FALSE)
```

提醒用户：
- `proj_create()` 要求目标目录为空或不存在
- 如果目录已有文件，改用 `usethis::create_package(".", open = FALSE)`
- Windows 路径用正斜杠：`"C:/Users/xxx/Desktop/project"`
- 目录名含连字符（`tailocin-01`）不是合法包名 → 必须给 `name`
- 步骤 2 和 3 必须在打开项目后的 Console 中执行
- `proj_use_workflow()` 同时生成 `_quarto.yml`（**不设 `cache`**，缓存逐 chunk 开）、`data/00-raw/d00-resource/` 和 `.gitignore` 规则（数据、workflow 内 SQLite、Quarto/knitr 缓存、graph HTML）
- 稿件不用 `use_qmd`：`qproj::use_manuscript(open = FALSE)` → `analyses/manuscript/<编号>-manuscript.qmd`，自带独立 `_quarto.yml`；编号取自目录名前缀（`p0101-x` → `p0101`），取不到时默认 `090-manuscript`
- **推荐 Positron**：支持多 session 隔离，避免切换 QMD 时 path 绑定污染全局环境

### 场景 1.5：初始化完成后 — 生成项目 CLAUDE.md

**触发时机**：场景 1 的三步命令用户都执行完毕后，主动询问：

> 项目骨架已就绪。是否要生成项目级 CLAUDE.md？它会让 Claude 在写代码时自动遵守 qproj 的 path binding 规则。

**用户同意后**，执行以下流程：

#### Step A：生成 CLAUDE.md 骨架

在项目根目录创建 `CLAUDE.md`，写入 qproj 固定规则部分（Commands、Data Layout、Workflow Rules、**Code Organization**）。
这部分内容来自模板 `templates/CLAUDE-qproj.md`，**不需要用户填写**——其中 `Code Organization` 节让 Claude 日后写代码时自动遵守"何时拆 qmd / 何时抽函数到 `R/` / 怎么接论文"。

#### Step B：逐板块引导用户完善

依次询问以下信息，用户回答后填入对应板块。用户说"跳过"则留空该板块：

1. **项目概述**："请用一句话描述这个项目的研究问题或分析目标"
2. **R 编码规范**："这个项目有没有特殊的包选型或编码约定？（比如用什么数据容器、哪些分析方法）没有的话跳过"
3. **出图规范**："目标期刊是哪个？有特殊的图表尺寸/格式要求吗？没有的话我用 Nature 默认值"
4. **数据字典**："项目的关键数据文件叫什么？核心变量有哪些？（如因变量、自变量、协变量）"
5. **项目陷阱**："有什么已知的坑或特殊决策需要记录？（如排除的异常样本、批次效应）没有的话跳过"

#### Step C：清理指引文本

所有板块填写完毕后，删除 CLAUDE.md 中残留的 `[填写指引]` 段落和顶部的模板说明。

**注意**：如果用户跳过了某个板块，保留该 `##` 标题但内容留空，方便日后补充。

#### 模板文件位置

模板存放在 `~/.claude/skills/qproj-helper/templates/CLAUDE-qproj.md`。

执行 Step A 时，读取该模板，复制到项目根目录并重命名为 `CLAUDE.md`：
```bash
cp ~/.claude/skills/qproj-helper/templates/CLAUDE-qproj.md ./CLAUDE.md
```
然后根据 Step B 的用户回答，编辑 `./CLAUDE.md` 中对应的板块，并删除所有 `[填写指引]` 段落。

---

### 场景 2：已有项目，添加新分析步骤

**信号**：用户说"加一个步骤"、"创建新的 qmd"、"添加分析"。

**操作**：先扫描当前 analyses/ 下已有的 .qmd 文件，确定下一个编号：

```bash
ls analyses/*.qmd 2>/dev/null
```

然后告诉用户：

```r
qproj::use_qmd("XX-步骤名", path_proj = "analyses", open = FALSE)
```

其中 XX 是下一个可用编号。帮用户拟好步骤名（英文、连字符分隔、简洁描述）。

**执行方式：建 qmd 交给用户在 Console 跑**——把上面的 `use_qmd(...)` 命令给用户即可（这是结构决策，让用户在环内）。

> **两条纪律**：
> - **不要自己 `Rscript -e` 代跑建 qmd**。`use_qmd` 本身无隐藏副作用（只挡 `00-` 前缀、生成 UUID、填 `workflow.qmd` 模板），但仍把命令交给用户执行。
> - **不要在 skill 里复刻 `use_qmd` 的模板自己拼 qmd**：qproj 升级后模板（setup chunk、路径绑定）会变，手写副本会漂移、与项目其它文件不一致——这恰恰是 qproj 要消除的东西。skill 编排 qproj，不替代它；建 qmd 一律走 `use_qmd`（用户跑），保持 single source of truth。
>   复制一个已有 `.qmd` 改名当新步骤同样不行：`params$name` 没跟着改，渲染时 `qproj_identity_error` 中止（0.2.0 起）。

### 场景 3：数据应该放哪里

**信号**：用户问"数据放哪"、"原始文件放哪个目录"、"怎么导入数据"。

回答模板：

| 数据类型 | 放到 | 路径 |
|----------|------|------|
| 本步骤专属原始数据 | `path_data` | `analyses/data/00-raw/dXX-步骤名/` |
| 项目共享资源（参考库等） | `path_resource` | `analyses/data/00-raw/d00-resource/` |
| 计算产出（代码自动写入） | `path_target` | `analyses/data/XX-步骤名/` |

提醒：`d00-resource/` 由 `proj_use_workflow()` 建好；`data/XX-步骤名/` 渲染时自动建。本步私有输入目录 `d<步骤名>/` **不会自动创建**（0.2.0 起 setup 不再 `dir.create` 任何 `00-raw` 目录），要放私有原始数据时手动建：

```r
dir.create("analyses/data/00-raw/dXX-步骤名", recursive = TRUE)
```

`d<步骤名>/` 只给本步读（其他步骤读它就是场景 0 里的 bypass）。每次渲染收尾，它和本步产出目录 `data/<步骤名>/` 都会被逐文件 sha256 记进运行记录（单文件 ≤ 512 MiB），所以几十 GB 的原始数据或产出会拖慢收尾 —— 必要时 `options(qproj.hash_max_bytes = …)` 调低阈值（超过的只记大小与修改时间），或 `options(qproj.manifest = FALSE)` 整个关掉记录。

**团队共享**：`proj_use_workflow()` 已自动将 `analyses/data/*` 加入 `.gitignore`，数据不走 git。团队协作时：
- `data/00-raw/` → 通过网盘（OneDrive/Google Drive 等）同步，确保所有人用相同原始输入
- `data/[01-99]*/` → **不同步**，各自本地 render 重新生成，用于验证可重复性
- 大文件（>1GB 测序数据等）或敏感数据 → 走机构存储或 rsync，不用消费级网盘
- `.qproj/provenance/latest/`（每步最近一次运行记录）与 `.qproj/graph/` 的 JSON / MD / `qg` **进 git**；`.qproj/provenance/runs/` 自带 `.gitignore` 只留本地，graph HTML 已被忽略

### 场景 4：怎么读取上游步骤的数据

**信号**：用户问"怎么读上一步的结果"、"path_source 怎么用"。

```r
# 在当前 .qmd 的 Tasks 区域中：
df <- readRDS(path_source("01-import", "result.rds"))
```

提醒：`path_source()` 会校验顺序——读编号不小于自己的步骤直接中止（`qproj_order_error`）。要读的东西若在更晚的步骤里产出，说明它该挪到逻辑上先产出它的那一步。

经 `path_source()` 读的文件会连同 sha256 记进本步运行记录，`proj_lineage_check()` 据此判断本步是否过期（场景 10）；手拼路径读的不会。

### 场景 5：在已有文件夹中初始化 qproj

**信号**：用户在 Positron 中打开了普通文件夹，报错"does not appear to be inside a project"。

```r
# 如果文件夹为空：
qproj::proj_create(".")

# 如果文件夹已有文件：
usethis::create_package(".", open = FALSE)

# 然后创建工作流：
qproj::proj_use_workflow("analyses")
```

### 场景 6：依赖管理

**信号**：用户问"怎么安装包"、"依赖管理"、"DESCRIPTION"。

```r
# 扫描代码引用的包，自动更新 DESCRIPTION
qproj::proj_update_deps()

# 安装 DESCRIPTION 中声明的所有包
qproj::proj_install_deps()

# 检查代码引用 vs DESCRIPTION 是否一致
qproj::proj_check_deps()
```

提醒：这套机制只声明依赖不锁版本。需要锁版本时升级到 renv。

### 场景 7：概念解释

**信号**：用户问"path_target 是什么"、"为什么有 d 前缀"、"qproj 怎么工作的"。

先用下方速查表回答，如需深入解释则读取 `references/qproj-guide.md`。

### 场景 8：查看步骤间依赖关系（变更影响检查）

**信号**：用户说"依赖关系"、"哪些步骤受影响"、"dependency graph"、"blast radius"、"影响范围"。

**操作**：先检查 `.qproj/graph/qproj-graph.json` 是否存在。

**若不存在**，**你可以直接生成**（非交互、只写文件）：

```bash
Rscript -e "qproj::proj_scan_graph()"                              # 装了 visNetwork / htmlwidgets 的机器
Rscript -e 'qproj::proj_scan_graph(formats = c("json", "md"))'     # 没装 visNetwork（如 spark 公共库）：默认带 html 会直接报错
```
（或让用户在 Console 跑 `qproj::proj_scan_graph()`。）这会扫描 `analyses/` 所有 `.qmd`（含 child 文档）和 `R/*.R` 的数据读写与函数调用，在 `.qproj/graph/` 下生成：`qproj-graph.json`（机读）、`QPROJ_GRAPH_REPORT.md`（文字报告，改代码前先读它）、`qproj-graph.html`（交互拓扑图，已被 `.gitignore`）、`qg`（查询 CLI）。JSON / MD / `qg` 进 git。需 `jq`。

**若已存在**，用 `qg` CLI 查询（`jq` 必需）。**先跑 `bash .qproj/graph/qg --help` 看全部子命令，别猜。** 先看图新不新鲜，再查（示例用本项目真实步骤名）：

```bash
bash .qproj/graph/qg status     # exit 0 新鲜 / 3 过期（列出增删改的步骤）/ 2 无法判定；过期就先重扫
bash .qproj/graph/qg impact     010-import-metadata   # 传递闭包：改它会波及的全部下游（blast radius）
bash .qproj/graph/qg deps       050-pcm-analysis      # 传递闭包：它依赖的全部上游
bash .qproj/graph/qg downstream 010-import-metadata   # 直接一跳：谁直接消费它
bash .qproj/graph/qg upstream   050-pcm-analysis      # 直接一跳：谁直接喂它
bash .qproj/graph/qg paths 010-import-metadata 090-manuscript   # A→B 所有依赖路径
bash .qproj/graph/qg list step        # 列所有步骤节点（也可 function / table）
bash .qproj/graph/qg unused           # 定义了没调用的函数 + 写了没人读的表
bash .qproj/graph/qg stale --days 60  # 无下游且 60 天没提交的步骤
bash .qproj/graph/qg bypass           # 手拼路径读别的步骤 d<step>/ 的地方（反模式，场景 0）
bash .qproj/graph/qg edges  050-pcm-analysis      # 原始边（含 render_order、置信度）
bash .qproj/graph/qg version          # qg 与写图的扫描器版本
```
每个子命令都支持 `--json`（空结果输出 `[]`）；`--human` 只用于 `node` 与 `status`。

提醒：
- `impact`/`deps` 是**传递闭包**，`downstream`/`upstream` 只看**直接一跳**——别混用
- `qg` 依赖 `jq`，Windows：`winget install jqlang.jq`
- 依赖图是静态快照。0.11.4 起 `qg` 每次查询前自动比对项目：过期时仍按旧图回答，但在 stderr 列出变动的步骤和重扫命令 —— 看到就先重扫（`QG_STRICT=1` 让它直接以 exit 3 拒答）
- **Windows Git Bash 里那行重扫命令带 MSYS 路径（`/c/…`、`/tmp/…`），Windows R 认不得、直接报错**（实测）→ 在项目根照上面的命令重扫
- 旧版 `proj_scan_graph()` 生成的 `qg` 不认识新子命令；重扫会替换没被手改过的旧 `qg`
- 浏览器打开 `.qproj/graph/qproj-graph.html` 看交互拓扑；改代码前先读 `QPROJ_GRAPH_REPORT.md`

**你写代码时**：跨多步骤数据流改动（改产出格式、重命名产出、调步骤顺序）先 `qg impact` 看影响面再动手；单步内部小改（加图、调参）不必。

### 场景 9：代码怎么组织 —— 何时拆 qmd / 何时抽函数 / 怎么接论文

**信号**：用户问"这段该放哪"、"要不要新开一个 qmd"、"怎么让代码清爽点"、"函数要不要抽出来"、"这些分析怎么对应到论文"。

**判据速答**（详细版 + pc047e2 范例读 `references/qproj-authoring-guide.md`）：
- **另开 `.qmd`** 当：产出被多个下游复用 / 换了输入源或方法族 / 有重型外部工具 / 对应论文一个独立 Results 块或主图。只是同一分析的下一子问题 → 加 `### Task N`，别为缩短文件而拆。
- **一个 Task = 论文一个论点**；**一个 chunk = 一个计算 + 一个产出**（单一职责）；**chunk 后写一段带具体数字的结论散文**——这就是 Results 草稿。
- **下沉函数到 `R/`** 当：纯计算/绘图 >~15 行、会复用、或值得文档化（roxygen）。qmd 只留"编排 + 展示 + 叙事"；R/ 里命名空间显式 `::`、路径用参数传入。只用一次的胶水 helper 就地留在 qmd。
- **接论文**：每个 step 用 `path_target` 落关键产出（或落共享 DB），manuscript 只用 `path_source`/DB 读回、inline 引用数字 → 改上游重跑、论文自动同步、可回溯。

> pc047e2 范例：`074` 的 Task/chunk/结论散文分层 + 把 `run_vale_rebuttal`/`make_fig7` 下沉 `R/`；`090-manuscript` 用 `dbReadTable("stepNN_…")` 汇聚上游产出成文。

**让判据常驻生效（关键落地）**：这套规则要在 Claude 日常写代码时自动起作用，就得进**项目 `CLAUDE.md`**（见开头「规则的归宿」），不能只留在本 skill：
- **新项目**：场景 1.5 生成的 `CLAUDE.md` 已含 `## Code Organization` 节，自动带上。
- **已有项目**（有 `CLAUDE.md` 但缺这节）：把 `templates/CLAUDE-qproj.md` 的 `## Code Organization` 节**追加**进现有 `CLAUDE.md`（用 Edit 追加、别覆盖用户已有内容），动手前**先提议**征得用户同意。

### 场景 10：体检、过期检查与旧项目迁移

**信号**：用户说"升级了 qproj"、"渲染突然报错"、"哪些步骤要重跑"、"投稿前检查一遍"、"缓存是不是旧的"。

```bash
Rscript -e "qproj::proj_doctor()"                       # 缓存与数据库设置的静态体检（只读）
Rscript -e "qproj::proj_lineage_check()"                # 每步上次运行后，它读过的文件 / 表变了没
Rscript -e "qproj::proj_lineage_check(strict = TRUE)"   # 有 stale / missing 就报错：投稿前的门槛
bash .qproj/graph/qg status                              # 依赖图是否跟上代码
```

- `proj_lineage_check()` 只读已有的运行记录 `.qproj/provenance/latest/<step>.json`。**没在 ≥ 0.3.0 下渲染过的步骤根本不出现在结果里** —— 结果为空或全 `ok` 不等于整条链都新鲜，先看每个步骤是否都有记录。
- 它也只认经 `path_source()` 读的文件和本步自己的 `d<step>/`；手拼路径读的东西不在判断范围内（场景 0）。
- `proj_doctor()` 的 `cache` 族查：全局开缓存（`global_cache`）、缓存 chunk 读文件却没 `cache.extra`（`cached_input`）、缓存 chunk 写文件或表（`cached_side_effect`）等；`database` 族查数据库字典与表记录。逐条说明见 `references/qproj-guide.md` 第十五节。

**0.1.x 时代建的项目，升级后要看的**（不用重建脚手架，旧 setup chunk 照常能跑）：
1. `analyses/_quarto.yml` 里的 `cache: true`（旧模板全局开缓存）→ `proj_doctor()` 报 `global_cache` 等。全局缓存下，写文件的 chunk 命中缓存时不会执行写入（一直如此，现在才被查出来）。新模板的做法是删掉这一行、慢 chunk 自己 `#| cache: true`。改了渲染行为 —— **先提议，用户同意再改**。
2. 第一次在新版下渲染：每个缓存 chunk 的哈希都变了（0.8.0 起缓存键含项目代码与包版本指纹），**会整体重算一遍**。重计算步骤先估时间，必要时走 spark 队列。
3. 旧版生成的 `.qproj/graph/qg` → 重扫一次（场景 8）。
4. 项目 `CLAUDE.md` 若是旧模板生成的，`qproj Workflow Rules` 节可能还写着"越序只警告""raw 区可 `here::here` 跨步读"→ 提议换成 `templates/CLAUDE-qproj.md` 的同名节。
5. 新的两道中止（越序读取、身份不符）在渲染前静态查，顺带列出 bypass 与 `cache` 设置（只读）：
   ```bash
   Rscript ~/.claude/skills/qproj-helper/scripts/preflight.R <项目根>
   ```
   输出 `IDENTITY` / `ORDER` 行的步骤，升级后渲染会中止；`BYPASS` 行不中止，见场景 0。

### 场景 11：Bash / Python 步骤（spark 上的 Slurm 驱动）

**信号**：步骤的主体是外部工具（geNomad、DefenseFinder、Nextflow…），要写成 `sbatch` 脚本或 Python 脚本，而不是 `.qmd`。

qproj 0.10 起给 Bash 和 Python 同一套路径纪律和运行记录 —— 外部工具的整批输出由这样的步骤写进自己的 `data/<step>/`，下游 `.qmd` 用 `path_source()` 读，取代"工具输出堆在 `00-raw/dNN` 里被别的步骤直接读"。

```bash
# 取脚本位置（spark：/opt/bio/R/4.5/library/qproj/scripts/qproj.sh）
Rscript -e 'cat(qproj::proj_shell_lib())'      # qproj.sh
Rscript -e 'cat(qproj::proj_python_lib())'     # qproj.py（只用标准库）
# sbatch 跑的是 spool 副本，$0 / BASH_SOURCE 都不可靠 → 把这段官方引导块原样贴进驱动脚本：
Rscript -e 'cat(qproj::proj_shell_bootstrap(step = "070-prophage-census"))'
```

驱动脚本里：`path_target` / `path_source` / `create_dir_target [--clean]` / `qproj_run_start` … `qproj_run_finish`（写运行记录）；Nextflow 的 work / cache 放 `path_run_state`（`data/.run-state/<step>/`，`--clean` 不会删它）。

⚠️ 两个坑：
- **shell 和 Python 里的 `path_raw` = 本步私有的 `00-raw/d<step>/`**（相当于 R 里的 `path_data`）；R 里的 `path_raw` 是 `00-raw` 根。照搬 R 代码的路径会错位。
- 驱动跑在 `set -euo pipefail` 下时，**赋值和使用分两行**：`input="$(path_source 010-import x.fq)"` 再 `tool "$input"`；写成 `tool "$(path_source …)"` 会吞掉替换失败。

资源参数、排队、提交一律走 `/spark-advisor`（`gen-sbatch → submit`），本 skill 只管路径与记录。驱动脚本完整写法与 Python 版见 `references/qproj-guide.md` 第十九节。

### 场景 12：项目数据库与样本 metadata 工作簿

**信号**：用户说"把结果落库"、"SQLite"、"metadata 表"、"样本信息表"、"ENA 提交表"。

- **数据库**：一个项目一个 SQLite。路径依次取 `QPROJ_DB` 环境变量 → `analyses/_qproj.yml` 的 `database:` → `analyses/<编号>.sqlite`；目录名推不出编号（`exd001e1`、`tailocin-01`）时是 `analyses/project.sqlite`，想要有意义的名字就在 `_qproj.yml` 写 `database:`。workflow 内的 `*.sqlite` 已被 `.gitignore`。
- 每张表、每列的含义写进 `analyses/db-dictionary.yml`，`qproj::proj_db_describe()` 合并字典与表记录给人和 AI 看；0.12 起写表的步骤在 `.qproj/provenance/tables/` 留表记录，`proj_lineage_check()` 按表判断过期。同一张表只由一个步骤写。
- **样本 metadata**：一本工作簿 `analyses/<编号>-metadata.xlsx`，`dictionary` sheet 即 schema。`proj_metadata_template()` 新建或迁移旧表；导入步骤用 `proj_metadata_import(snapshot = TRUE, step = params$name)` 校验并把快照写进本步 `data/<step>/metadata/`，下游经 `path_source()` 读快照，**分析不直接读工作簿**。

函数与参数细节见 `references/qproj-guide.md` 第十七、十八节。

## 快速参考

### 五个路径绑定

| 绑定 | 类型 | 指向 | 作用 |
|------|------|------|------|
| `path_target` | 函数 | `data/<step>/` | 本步骤唯一写入位置 |
| `path_source` | 函数 | `data/<prev>/` | 读上游产出（越序直接中止；读的文件进运行记录） |
| `path_raw` | 字符串 | `data/00-raw/` | 原始数据总入口（一般不直接使用） |
| `path_resource` | 字符串 | `data/00-raw/d00-resource/` | 项目共享资源 |
| `path_data` | 字符串 | `data/00-raw/d<step>/` | 本步骤私有原始输入 |

> 注：`path_target` 和 `path_source` 在 QMD 内由 setup chunk 创建，底层调用包级 API `qproj::proj_path_target()` / `qproj::proj_path_source()`。
> Bash / Python 步骤里同名的 `path_raw` 指本步 `d<step>/`，不是 `00-raw` 根（场景 11）。

### 核心函数

| 函数 | 用途 | 何时用 |
|------|------|--------|
| `proj_create(path, name)` | 创建项目脚手架 | 从零开始时（目录名不是合法包名时给 `name`） |
| `proj_use_workflow("analyses")` | 创建工作流目录 + `_quarto.yml` + `d00-resource/` + `.gitignore` 规则 | 项目初始化后 |
| `use_qmd("XX-name")` | 创建分析步骤 | 添加新步骤时 |
| `use_manuscript()` | 创建稿件 `analyses/manuscript/<编号>-manuscript.qmd` | 开始写稿时 |
| `proj_create_dir_target(name, clean = FALSE)` | 创建步骤产出目录 + 打开运行记录 + 准备缓存指纹 | setup chunk 自动调用（0.2.0 起默认不清空） |
| `proj_path_target(name)` | 返回 `path_target` 函数 | setup chunk 内绑定用 |
| `proj_path_source(name)` | 返回 `path_source` 函数 | setup chunk 内绑定用 |
| `proj_dir_info(path)` | 列出目录文件元信息 | output chunk 展示产出 |
| `proj_run_start(name)` / `proj_run_finish()` | 显式开 / 关运行记录 | R 脚本与交互 session（渲染时自动） |
| `proj_lineage_check(strict)` | 找输入在上次运行后变了的步骤 | 改了上游后、投稿前 |
| `proj_doctor()` | 缓存与数据库设置的静态体检 | 升级后、改缓存设置后 |
| `proj_scan_graph()` | 生成依赖图 + `qg` | 结构性改动前 |
| `proj_db_connect()` / `proj_db_disconnect()` / `proj_db_describe()` | 项目 SQLite 的连接与说明 | 落库 / 读库时 |
| `proj_metadata_template()` / `proj_metadata_import()` | 样本 metadata 工作簿的建立与校验导入 | 管样本信息时 |
| `proj_shell_lib()` / `proj_python_lib()` / `proj_shell_bootstrap()` | Bash / Python 步骤的路径与记录工具 | 写 Slurm 驱动时 |
| `proj_workflow_config(path_proj)` | 读取 `_qproj.yml` 渲染配置 | 自定义渲染顺序时 |
| `proj_install_deps()` | 安装依赖 | 克隆项目后 |
| `proj_update_deps()` | 更新 DESCRIPTION | 加了新包后 |
| `proj_check_deps()` | 检查依赖一致性 | 提交前 |

### 命名约定

- `00-` 前缀：框架保留（`data/00-raw/`），用户不要用
- `01-`、`02-`…：用户分析步骤
- `d` 前缀：原始数据子目录标记（`d01-import/` = 步骤 01 的私有输入）
- 步骤名：英文、连字符分隔（`01-taxonomic-profiling`）

### 数据流规则

- 单向链式：产出用 `path_target` 写，下游用 `path_source("步骤名","文件名")` 读——只读编号更小的步骤（越序直接中止）
- `path_data`（`00-raw/d<step>/`）严格私有：别的步骤要用，就由本步经 `path_target` 发布；多个步骤共用的外部大件放 `d00-resource/`。手拼 `here::here(path_raw, "dNN-其他步骤", …)` 会被 `qg bypass` 列出，也逃过运行记录与 lineage 检查（旧代码里见到不必立刻改，新代码不写）
- `clean = TRUE` 只清空产出目录（`data/NN-步骤名/`），不动 `00-raw/` 下的原始数据；函数默认 `clean = FALSE`

### 交互模式注意事项

> **RStudio 陷阱**：在 RStudio 中切换 QMD 文件时，上一个文件的 path 绑定（`path_target` 等）会残留在全局环境中，可能导致数据写入错误目录且**无任何报错**。推荐使用 **Positron**（多 session 隔离，从结构上消除此问题）或每次切文件前重启 R（`Ctrl+Shift+F10`）。
>
> `quarto render` 有身份校验兜底（`params$name` 与文件名不符即中止）；交互逐 chunk 跑没有这道闸。交互跑完一个步骤想留运行记录，要跑到末尾调 `proj_run_finish()` 的那个 chunk。

## 回答原则

1. **先认项目、再判场景**——先看是不是 qproj 项目（`analyses/` + setup chunk）、有没有项目 `CLAUDE.md`；有就读它、按它的约定来，别凭记忆重构
2. **分清谁动手**——能自己跑的非交互命令（render / qg / `Rscript -e`）自己跑；需要活 session 或属于结构决策的，给用户 Console 命令
3. **给命令，不给教程**——用户要的是可粘贴的代码，不是原理讲解
4. **主动帮用户确定编号和命名**——扫描现有 `.qmd` 后给出具体的 `use_qmd()` 调用
5. **遇到复杂问题才读 references/**——日常问题用上方速查表即可
