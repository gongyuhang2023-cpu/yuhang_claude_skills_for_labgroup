# qproj 创建 QMD 文件使用指南

> 按 **qproj 0.12.0**（2026-10）写。0.2.0 起几处默认行为变了：越序读取直接中止、`clean` 默认 `FALSE`、
> setup 不再建 `00-raw` 下的目录、模板不设 `cache`。0.1.x 时代建的项目怎么过渡见**第二十节**。
> 上游权威来源：包内 `README.md`、`NEWS.md`、`vignette("design-philosophy")`。

## 一、qproj 是什么？

**qproj** 是 rujinlong 基于 projthis 精简重写的 R 包，专为 Quarto `.qmd` 工作流设计。核心理念是通过目录结构和命名约定，组织分析文件的执行顺序和数据流向。

0.3.0 之后它还记录**每次渲染读了什么、写了什么**（运行记录），并能据此找出输入已变、需要重跑的步骤（第十六节）；配套有缓存体检、项目数据库与样本 metadata 工作簿的工具。

## 二、安装

```r
# install.packages("pak")
pak::pak("rujinlong/qproj")
```

> 如果提示缺少其他包，根据报错信息用 `install.packages()` 补装。


- **spark 上 R 是公共的**：`/opt/bio/bin/Rscript`，qproj 由管理员装在公共库 `/opt/bio/R/4.5/library`，不用自己装；查版本 `packageDescription("qproj")$RemoteSha`。
- **spark 不要用 conda / pixi 装 R 或 R 包**（服务器规则）：2026-10-08 起学生 conda 环境里的 R 已移除（环境里留有 `R-MOVED-TO-SHARED.txt`），环境内的 `Rscript` 就是公共 R。缺包在公共 R 里 `install.packages()` / `remotes::install_github()`，自动进个人库 `~/R/aarch64-conda-linux-gnu-library/4.5`。个人库排在公共库前面，**个人库里的旧 qproj 会盖住公共库的新版**，查版本时看 `find.package("qproj")`。
- **Linux / WSL 上 conda 环境里的 R 用 pak 装会失败两次**（spark 以外的机器）：先是系统依赖检查要 `sudo apt`（`PKG_SYSREQS=false` 跳过），再是 pkgbuild 找不到编译工具链（非交互 shell 没激活 conda）——哪怕 qproj 是纯 R 包。直接用 remotes（依赖已齐时）：
  ```bash
  ~/miniforge3/envs/<env>/bin/Rscript -e 'remotes::install_github("rujinlong/qproj", dependencies = FALSE, upgrade = "never")'
  ```
- 三处想保持同一版本时，装指定 commit：`pak::pak("rujinlong/qproj@<sha>")` / `remotes::install_github("rujinlong/qproj", ref = "<sha>")`。

---

## 三、完整工作流程（从零创建项目）

### 第 1 步：创建项目

#### 方式 A：使用 qproj 自带函数（推荐，RStudio / Positron 通用）

```r
# 创建项目脚手架（自动生成 DESCRIPTION / NAMESPACE / README / .gitignore）
qproj::proj_create("~/Desktop/p0101-example", name = "p0101example")
```

目录名可以带项目编号与连字符（`p0101-example`），但 R 包名只能是字母、数字和点、以字母开头 —— 目录名不合法时必须给 `name`，否则 `proj_create()` 直接报错、什么也不建。

> **Windows 路径小工具**：Windows 资源管理器复制的路径是反斜杠格式，R 中不能直接用。安装 [rpath-copier](https://github.com/gongyuhang2023-cpu/rpath-copier) 后，在资源管理器中选中文件夹按 `Ctrl+Shift+C`，即可复制正斜杠格式路径（如 `C:/Users/xxx/Desktop/pc047`），直接粘贴到 R Console 中使用。

然后在 RStudio 或 Positron 中打开该文件夹：
- **RStudio**：File → Open Project → 选择项目文件夹
- **Positron**：File → Open Folder → 选择项目文件夹

如需初始化 Git：
```r
usethis::use_git()
```

#### 方式 B：RStudio 图形界面

**File → New Project → New Directory → R Package**

- 项目名例如 `pc047`
- **勾选 "Create a git repository"**
- 创建完成后，编辑 `README.md` 说明项目背景

#### 方式 C：Positron 中已打开空文件夹

如果你已经用 Positron 打开了一个普通文件夹（没有 DESCRIPTION），直接运行 `qproj` 的依赖管理函数可能会报错。此时用 `proj_create()` 初始化：

```r
qproj::proj_create(".")
```

这会在当前目录生成 DESCRIPTION、NAMESPACE、README.md、.gitignore。

> **注意**：`qproj::proj_create()` 要求目录为空或不存在。如果目录已有文件，用 `usethis::create_package(".", open = FALSE)`；目录里已有 `.git` 时，非交互下 usethis 会判「嵌套项目」直接中止，先 `options(usethis.allow_nested_project = TRUE)`。

### 第 2 步：创建 analyses 工作流目录

在 Console（RStudio 或 Positron 均可）中执行：

```r
qproj::proj_use_workflow("analyses")
```

**这条命令会：**
1. 在项目根目录下创建 `analyses/` 文件夹及 `analyses/README.md`
2. 在 `analyses/` 下创建 `data/` 文件夹及 `data/README.md`
3. 建好 `analyses/data/00-raw/d00-resource/`（项目共享输入，只建这一次）
4. 在 `.gitignore` 中添加规则：`analyses/data/*`（保留 `data/README.md`）、workflow 内的 SQLite 及其 journal/WAL、`.quarto/`、`*_cache/`、Nextflow 运行目录、`.qproj/graph/qproj-graph.html`；重复调用不重复写
5. 在 `analyses/` 下创建 `_quarto.yml`（gfm 输出、code-fold、分页表格等默认配置；**不设 `cache`**，见第十五节）

> **注意**：工作流目录**建议命名为 `analyses`**，这是团队约定。

### 第 3 步：创建 QMD 分析文件

```r
qproj::use_qmd("01-taxonomic-profiling")
```

**这条命令会：**
1. 在 `analyses/` 下生成 `01-taxonomic-profiling.qmd` 文件
2. 自动填入 YAML 头信息（标题、日期、`params$name`）
3. 生成唯一 UUID 标识符（用于 `here::i_am()` 定位）
4. 预置标准代码 chunk 模板（params、setup、packages、Tasks、Files written）

`use_qmd()` 拒绝 `00-` 开头的名字（框架保留）。

**函数参数详解：**

```r
qproj::use_qmd(
  name,                          # 文件名（不带 .qmd 后缀）
  path_proj = "analyses",        # 工作流目录，默认 "analyses"
  open = rlang::is_interactive(),# 是否自动打开文件编辑
  ignore = FALSE                 # 是否添加到 .Rbuildignore
)
```

**可以继续创建更多分析文件：**

```r
qproj::use_qmd("02-diversity-analysis")
qproj::use_qmd("03-differential-abundance")
qproj::use_qmd("04-network-analysis")
```

> 新步骤一律用 `use_qmd()` 建。复制一个已有 `.qmd` 改名的话，`params$name` 和 `here::i_am()` 里的文件名都还是旧的，渲染时 `qproj_identity_error` 中止。

### 第 4 步：运行 QMD 文件

#### RStudio
打开 `01-taxonomic-profiling.qmd`，点击 **Render** 按钮（或 `Ctrl+Shift+K`）。

#### Positron
打开 `01-taxonomic-profiling.qmd`，有以下几种方式 Render：
- 点击编辑器右上角的 **Preview** 按钮
- 使用快捷键 `Ctrl+Shift+K`
- 在终端中手动执行：`quarto render analyses/01-taxonomic-profiling.qmd`

**渲染会：**

1. 建好 `data/01-taxonomic-profiling/` —— 该分析的**计算结果**（图、表、中间文件）只写这里
2. 写下这次运行的记录：`.qproj/provenance/runs/<run_id>/` 与 `.qproj/provenance/latest/01-taxonomic-profiling.json`（第十六节）

**不会**建 `data/00-raw/d01-taxonomic-profiling/`。本步要私有原始数据时手动建：

```r
dir.create("analyses/data/00-raw/d01-taxonomic-profiling", recursive = TRUE)
```

### 第 5 步（写稿时）：建稿件

```r
qproj::use_manuscript(open = FALSE)
```

在 `analyses/manuscript/` 下生成 `<编号>-manuscript.qmd`、`title.tex` 和一份独立的 `_quarto.yml`（稿件自成 Quarto 项目，不继承 `analyses/_quarto.yml` 的作者等设置）。编号取自项目目录名前缀（`p0101-example` → `p0101-manuscript.qmd`，`p0075e2-x` → `p0075e2-manuscript.qmd`），取不到时默认 `090-manuscript`。同一项目多篇稿件显式命名：`use_manuscript("p0075s1-manuscript")`。

稿件源文件名**固定不变**（不带日期版本），日期版本只出现在渲染产物上；稿件里照样用 `path_source()` 读各步骤产出、行内引用数字。

---

## 四、生成的项目结构

```
p0101-example/                            # 项目根目录
├── README.md                             # 项目说明
├── DESCRIPTION                           # 包依赖声明
├── NAMESPACE                             # 命名空间
├── .git/                                 # Git 版本控制
├── .gitignore
├── .qproj/                               # qproj 的记录（渲染后出现）
│   ├── provenance/
│   │   ├── latest/<step>.json            # 每步最近一次运行记录（进 git）
│   │   └── runs/<run_id>/                # 每次运行（自带 .gitignore，只留本地）
│   └── graph/                            # proj_scan_graph() 生成（JSON / MD / qg 进 git）
│
└── analyses/                             # 工作流目录
    ├── README.md                         # 工作流说明（自动生成）
    ├── _quarto.yml                       # 渲染配置（自动生成）
    ├── 01-taxonomic-profiling.qmd        # 分析文件 1
    ├── 02-diversity-analysis.qmd         # 分析文件 2
    ├── 03-differential-abundance.qmd     # 分析文件 3
    ├── manuscript/                       # use_manuscript() 生成（可选）
    │
    └── data/                             # 数据目录（除 README 外不进 git）
        ├── README.md
        │
        │── 01-taxonomic-profiling/       # 01 的计算结果（path_target 写入，渲染时建）
        │── 02-diversity-analysis/        # 02 的计算结果
        │── 03-differential-abundance/    # 03 的计算结果
        │
        └── 00-raw/                       # 外部输入总目录（与计算结果隔离）
            ├── d00-resource/             # 项目共享资源（proj_use_workflow 建）
            └── d01-taxonomic-profiling/  # 01 的专属原始数据（需要时手动建）
```

---

## 五、QMD 模板内容详解

`use_qmd()` 生成的模板包含以下结构：

### YAML 头信息

```yaml
---
title: "01-taxonomic-profiling"
date: today
params:
  name: "01-taxonomic-profiling"
---

**Rendered: `r format(Sys.time(), '%Y-%m-%d %H:%M:%S %Z')`.**
```

模板 YAML 简洁，更多格式配置推荐统一放在 `_quarto.yml` 中。渲染时间按系统时区显示。

### Params chunk（交互模式兼容）

```r
#| label: params
#| eval: !expr interactive()
#| include: false
params <- list(name = "01-taxonomic-profiling")
```

> 这个 chunk 仅在交互模式（非 Render）下执行，确保在 RStudio/Positron 中手动逐 chunk 运行时 `params` 也有值。

### Setup chunk（路径初始化 + 五个路径绑定）

```r
#| label: setup
#| include: false
#| message: false
#| warning: false
#| cache: false
options(warnPartialMatchDollar = TRUE)
wd <- "analyses"
if (basename(getwd()) != wd) {
    setwd(here::here(wd))
}
here::i_am("01-taxonomic-profiling.qmd", uuid = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
qproj::proj_create_dir_target(params$name)

# convenience helpers
path_target <- qproj::proj_path_target(params$name)
path_source <- qproj::proj_path_source(params$name)
path_raw <- path_source("00-raw")
path_resource <- here::here(path_raw, "d00-resource")
path_data <- here::here(path_raw, paste0("d", params$name))
```

> **关键理解**：
> - `here::i_am()` + UUID 确保无论从哪里运行，都能正确定位工作流根目录
> - `proj_create_dir_target()` 做四件事：校验步骤名；**渲染时核对 `params$name` 与正在渲染的文件名**，不符即中止（`qproj_identity_error`）；建 `data/<step>/`；打开本步运行记录并准备缓存指纹
> - 默认 `clean = FALSE`：保留已有输出，适合增量开发；正式重跑时改成 `proj_create_dir_target(params$name, clean = TRUE)`，每次渲染清空产出目录重建（不会影响 `00-raw/` 下的原始数据）
> - `options(warnPartialMatchDollar = TRUE)`：`df$na` 这类部分匹配到 `df$name` 的写法会给警告
> - 0.1.x 的旧模板写的是 `clean = FALSE` 加三行 `dir.create(...)`，照常能跑，不必改

#### 五个路径绑定详解

以 `01-taxonomic-profiling.qmd` 为例，setup chunk 创建的五个绑定分别指向：

```
data/
├── 01-taxonomic-profiling/              ← path_target    我的计算结果写到这里
│
└── 00-raw/                              ← path_raw       原始数据总目录（一般不直接用）
    ├── d00-resource/                    ← path_resource  项目共享资源（参考基因组等）
    └── d01-taxonomic-profiling/         ← path_data      我自己的原始数据（私有）
```

| 名称 | 类型 | 指向 | 用途 |
|------|------|------|------|
| `path_target` | **函数** | `data/01-taxonomic-profiling/` | 保存计算结果的唯一位置 |
| `path_source` | **函数** | 读取上游步骤的产出 | 自带顺序校验（读编号不小于自己的步骤直接中止）；读的文件进运行记录 |
| `path_raw` | 字符串 | `data/00-raw` | 原始数据总入口，一般不直接用 |
| `path_resource` | 字符串 | `data/00-raw/d00-resource` | 所有步骤共享的公共资源 |
| `path_data` | 字符串 | `data/00-raw/d01-taxonomic-profiling` | 仅本步骤使用的原始数据 |

**函数 vs 字符串**：`path_target` 和 `path_source` 是**函数**（路径生成器），你传入文件名它帮你拼完整路径；其余三个是普通**字符串**，直接就是目录地址。

```r
# path_target 是函数，可以拼文件名
path_target()                    # → "data/01-taxonomic-profiling/"
path_target("taxa_table.rds")   # → "data/01-taxonomic-profiling/taxa_table.rds"
saveRDS(my_result, path_target("taxa_table.rds"))

# path_source 是函数，读取前序步骤的输出（在 02 的 QMD 中）
path_source("01-taxonomic-profiling", "taxa_table.rds")
# → "data/01-taxonomic-profiling/taxa_table.rds"

# path_resource 是字符串，直接拼路径
ref_db <- read.csv(file.path(path_resource, "reference_genome.csv"))

# path_data 是字符串，直接拼路径
raw_seqs <- read.csv(file.path(path_data, "raw_sequences.csv"))
```

> **为什么 `path_data` 是"私有"的？** qproj 没有提供任何函数让下游步骤读取你的 `path_data`（不像 `path_source` 可以跨步骤读取 `path_target` 的产出）。这是设计意图：原始数据应当由本步骤清洗处理后，通过 `path_target` 发布给下游，而非裸传递。

> **哪些读取进运行记录**：经 `path_source()` 拼出的路径（含 sha256）、本步自己的 `d<step>/` 全部内容。经 `path_resource` / `path_raw` 字符串拼出的路径**不记**。要让 lineage 检查跟踪 `d00-resource/` 里的某个文件，可以改用 `path_source("00-raw", "d00-resource", "文件")` 读 —— `00-raw` 永远允许，这样读会被记录（据源码推论，上游文档没写这个用法）。

### Packages chunk（加载包）

```r
#| label: packages
#| message: false
#| warning: false
#| cache: false

suppressPackageStartupMessages({
  library(here)
  library(conflicted)
  library(tidyverse)
  library(data.table)
  devtools::load_all()  # 加载项目 R/ 下的自定义函数
})
# 预先解决 dplyr/data.table/stats 常见函数名冲突
conflicted::conflicts_prefer(
  dplyr::filter,
  dplyr::lag,
  dplyr::first,
  dplyr::last,
  dplyr::between,
  dplyr::setdiff,
  .quiet = TRUE
)
```

> `dplyr::setdiff` 是 0.11.2 加的：Quarto 的 knitr 钩子会从全局环境调 `setdiff()`，后面加载的包（如经 mia 带进来的 Biostrings）也导出 `setdiff` 时，conflicted 会让渲染停在第一个 chunk。旧项目渲染时撞到这个，就把这一行补进自己的 packages chunk。

### Tasks 区域（用户编写分析代码）

```r
## Tasks

#| label: data-import
# 在此编写数据导入、清洗、分析、可视化代码
```

新模板只留一个空的 `data-import` chunk，不再带示例说明文字。

### Files written chunk（收尾）

```r
#| label: list-files-target
#| message: false
qproj::proj_run_finish(params$name)
knitr::kable(qproj::proj_dir_info(path_target(), tz = NULL))
```

> `proj_run_finish()` 关闭本步运行记录（渲染时文档结束的钩子也会关）；交互逐 chunk 跑时，跑到这里才写记录。`tz = NULL` 用系统时区。

---

## 六、核心函数速查

### 项目与工作流

| 函数 | 用途 | 示例 |
|------|------|------|
| `proj_create()` | 创建项目脚手架 | `qproj::proj_create("p0101-x", name = "p0101x", fields = list(Title = "xxx"))` |
| `proj_use_workflow()` | 创建工作流目录 + `_quarto.yml` + `d00-resource/` + `.gitignore` 规则 | `qproj::proj_use_workflow("analyses")` |
| `use_qmd()` | 创建 QMD 分析文件 | `qproj::use_qmd("01-import")` |
| `use_manuscript()` | 创建稿件（独立 Quarto 项目） | `qproj::use_manuscript(open = FALSE)` |
| `proj_workflow_config()` | 读取 `_qproj.yml`（渲染顺序、数据库设置） | 一般不需要手动调用 |

### 目录与路径管理（在 QMD 文件内部使用）

| 函数 | 用途 | 说明 |
|------|------|------|
| `proj_create_dir_target(name, clean)` | 创建输出目录 | 默认 `clean = FALSE`；还负责身份校验、打开运行记录、准备缓存指纹 |
| `proj_path_target(name)` | 获取输出路径函数 | 返回函数，用 `path_target("file.rds")` 调用 |
| `proj_path_source(name)` | 获取前序步骤的数据路径函数 | 越序读取中止（`qproj_order_error`） |
| `proj_dir_info(path)` | 列出目录内文件信息 | 返回 tibble（路径、类型、大小、时间） |

### 运行记录与体检

| 函数 | 用途 | 说明 |
|------|------|------|
| `proj_run_start(name)` / `proj_run_finish()` | 显式开 / 关运行记录 | R 脚本、交互 session 用；渲染时自动 |
| `proj_lineage_check(path, strict)` | 找输入在上次运行后变了的步骤 | 第十六节；`strict = TRUE` 作投稿前门槛 |
| `proj_doctor(path, checks)` | 缓存与数据库设置的静态体检 | 第十五节；只读 |
| `proj_scan_graph()` | 扫描项目依赖关系图 | 生成 JSON / MD / HTML 与 `qg` CLI，第十二节 |

### 项目数据库与 metadata

| 函数 | 用途 |
|------|------|
| `proj_db_path()` / `proj_db_connect()` / `proj_db_disconnect()` | 定位与连接项目 SQLite（第十七节） |
| `proj_db_describe(tables, verify)` | 库结构 + 数据库字典 + 表记录 |
| `proj_db_fingerprint(tables)` | 给读库的缓存 chunk 当 `cache.extra` |
| `proj_metadata_template()` / `proj_metadata_import()` / `proj_metadata_validate()` | 样本 metadata 工作簿（第十八节） |
| `proj_metadata_summary()` / `proj_metadata_export()` / `proj_metadata_diff()` | 摘要、导出（tsv / sqlite / ena / samplesheet / submission）、版本比较 |

### Bash / Python 步骤与 vpipe

| 函数 | 用途 |
|------|------|
| `proj_shell_lib()` / `proj_python_lib()` | `qproj.sh` / `qproj.py` 的路径（第十九节） |
| `proj_shell_bootstrap(step)` | 给 sbatch 驱动脚本的官方引导块 |
| `proj_vpipe_pin()` / `proj_vpipe_resolve()` / `proj_vpipe_check()` | `analyses/vpipe.lock`：把项目钉到某个 vpipe 版本（vpipe 流水线用） |

### 依赖管理

| 函数 | 用途 | 说明 |
|------|------|------|
| `proj_check_deps()` | 检查依赖是否完整 | 对比代码引用 vs DESCRIPTION 声明 |
| `proj_update_deps()` | 扫描代码更新 DESCRIPTION | 自动检测用到的包 |
| `proj_install_deps()` | 安装 DESCRIPTION 中的包 | 基于 `pak`，克隆项目后一键装包 |

---

## 七、数据流规则（核心约定）

qproj 的数据流是**单向链式**的：

```
01-taxonomic-profiling.qmd
    ├── 读取: data/00-raw/d01-taxonomic-profiling/  （原始数据，path_data）
    ├── 读取: data/00-raw/d00-resource/              （公共资源，path_resource）
    └── 写入: data/01-taxonomic-profiling/            （输出结果，path_target）
                    ↓
02-diversity-analysis.qmd
    ├── 读取: data/01-taxonomic-profiling/  （上一步的输出，path_source）
    ├── 读取: data/00-raw/d02-diversity-analysis/
    └── 写入: data/02-diversity-analysis/
                    ↓
03-differential-abundance.qmd
    ├── 读取: data/01-taxonomic-profiling/  （可读取任何前序步骤）
    ├── 读取: data/02-diversity-analysis/
    └── 写入: data/03-differential-abundance/
```

**规则：**
- 每个 QMD **只能写入**自己的 `data/{name}/` 目录（通过 `path_target`）
- 每个 QMD **只能读取**渲染顺序在自己之前的步骤（通过 `path_source`）。违反时**直接中止**（`qproj_order_error`）；`options(qproj.order_check = "warn")` 降为警告、`"none"` 关闭，只用于临时排查
- 每个 QMD 的原始数据（`path_data`）严格私有。别的步骤要用，由本步经 `path_target` 发布；多个步骤共用的外部大件放 `d00-resource/`

**在 QMD 中读取前序数据的方式：**

```r
# 在 02-diversity-analysis.qmd 中，setup 已创建 path_source
# 直接用 path_source 读取 01 的产出
taxa_data <- readRDS(path_source("01-taxonomic-profiling", "taxa_table.rds"))
```

**反模式：手拼路径读别的步骤的 `d<step>/`**

```r
# ✗ 运行时不报错、文件也读得到，但：
x <- read.delim(here::here(path_raw, "d01-taxonomic-profiling", "raw.tsv"))
```

1. `proj_scan_graph()` 把它记成 `bypass_access` 边，`qg bypass` 会列出来；
2. 这次读取**不进本步运行记录**，`proj_lineage_check()` 看不见它。实测（0.12.0）：改了 `d01-…/raw.tsv`，直接读它的 02 仍报 `ok`，要等 01 重新渲染、它发布的文件变了，02 才被标 `stale`。

外部工具的整批输出（geNomad、DefenseFinder 等）想给多个步骤用：要么由跑工具的步骤（可以是 Bash / Python 步骤，第十九节）写进自己的 `data/<step>/` 再被 `path_source()` 读，要么当作项目共享输入放 `d00-resource/`。旧代码里见到这种手拼读取不必立刻改，新代码别写。

---

## 八、包依赖管理

qproj 利用 DESCRIPTION 文件管理项目依赖（类似 R 包的方式），底层使用 `pak` 安装：

### 检查依赖是否完整

```r
qproj::proj_check_deps()
```

对比代码中实际引用的包与 DESCRIPTION 中声明的包，报告缺失和多余的依赖。

### 更新 DESCRIPTION

```r
qproj::proj_update_deps()
```

扫描所有代码文件（内部使用 `renv::dependencies()`），自动将引用的包写入 DESCRIPTION 的 Imports 字段。

可选参数 `remove_extra = TRUE`：同时删除 DESCRIPTION 中有但代码里没用到的多余依赖。

> **注意**：`qproj` 自身也可能被检测为依赖，需要手动从 DESCRIPTION 中删除 `qproj` 这一条。

> **GitHub/私有仓库的包**：需要手动在 DESCRIPTION 中维护 `Remotes:` 字段，`proj_update_deps()` 不会自动处理。

> DESCRIPTION 的 `Depends` / `Imports` 里列的包，其版本也进缓存指纹（0.8.1 起）：升级这些包会让缓存 chunk 重跑。

### 克隆项目后一键安装所有包

```r
qproj::proj_install_deps()
```

使用 `pak::local_install_deps()` 安装 DESCRIPTION 中声明的所有包。

> **注意**：这套机制**只声明依赖，不锁定版本**，与 `renv` 的定位不同。如果将来需要锁版本，`renv` 原生支持读 DESCRIPTION，升级路径平滑。

---

## 九、完整操作示例

以创建微生物组分析项目 `p0101-example` 为例：

```r
# ===== 1. 创建项目 =====
qproj::proj_create("~/Desktop/p0101-example", name = "p0101example")
setwd("~/Desktop/p0101-example")

# 初始化 Git（可选）
usethis::use_git()

# ===== 2. 创建工作流目录 =====
qproj::proj_use_workflow("analyses")

# ===== 3. 创建分析文件（按分析流程编号） =====
qproj::use_qmd("01-taxonomic-profiling", open = FALSE)    # 物种注释
qproj::use_qmd("02-alpha-diversity", open = FALSE)         # Alpha 多样性
qproj::use_qmd("03-beta-diversity", open = FALSE)          # Beta 多样性
qproj::use_qmd("04-differential-abundance", open = FALSE)  # 差异丰度分析
qproj::use_qmd("05-functional-prediction", open = FALSE)   # 功能预测

# ===== 4. 准备数据 =====
# d00-resource/ 已由第 2 步建好；本步私有原始数据目录手动建：
dir.create("analyses/data/00-raw/d01-taxonomic-profiling", recursive = TRUE)
#   analyses/data/00-raw/d01-taxonomic-profiling/  ← 原始测序数据
#   analyses/data/00-raw/d00-resource/             ← 参考数据库

# ===== 5. 编写分析代码 =====
# 打开 01-taxonomic-profiling.qmd，在 Tasks 区域编写代码
# 输出结果保存到 path_target()：
#   saveRDS(taxa_table, path_target("taxa_table.rds"))
#   ggsave(path_target("fig_taxa_barplot.png"), plot = p)

# ===== 6. 逐个 Render =====
# 按顺序 Render 每个 QMD 文件（Ctrl+Shift+K）

# ===== 7. 更新依赖 + Git 提交 =====
qproj::proj_update_deps()
# 提交代码与 .qproj/provenance/latest/（运行记录），数据目录已被 .gitignore
```

---

## 十、交互式工作流注意事项

在 RStudio 的交互 session 中，所有打开的 `.qmd` 文件**共用同一个 R 全局环境**。五个路径绑定（`path_target` 等）是 session 级变量，最近一次跑过的 setup chunk 决定了它们指向哪里。

### 两种典型出错场景

**场景 1：绑定残留（静默错误，最危险）**

你在 `01-import.qmd` 中跑完了 setup，然后切到 `02-clean.qmd` 直接执行代码 chunk，**忘了重跑 02 的 setup**。此时 `path_target` 仍然指向 `data/01-import/`，你的结果会写到错误目录，**且没有任何报错**。

**场景 2：绑定缺失（直接报错）**

当前 session 里根本没跑过任何 setup，第一个使用 `path_target` 的 chunk 就会报 `Error: object 'params' not found`。

> `quarto render` 时有身份校验兜底：`params$name` 与正在渲染的文件名不符就中止，所以从兄弟文件粘过来的 `params` chunk 不会把产出悄悄写到别的步骤。**交互逐 chunk 跑没有这道闸。**

### 经验法则

> **每次切到另一个 `.qmd` 准备执行代码，先重跑它的 `params` 和 `setup` chunk。**

### 规避方法（从轻到重）

1. **用 `quarto render` 跑完整文件**——自动获得全新 session 和正确绑定
2. **切文件前重启 R**（RStudio：`Ctrl+Shift+F10`）
3. **用 Positron**——支持每个 `.qmd` 独立 session，从结构上消除此问题

交互跑完一个步骤想留运行记录，要跑到末尾调 `proj_run_finish()` 的 chunk；中途停下就没有这次的记录。

---

## 十一、渲染顺序与 `_qproj.yml`

默认情况下，`.qmd` 文件按**字典序**渲染（所以数字编号很重要）。另外：
- `README.qmd` 永远最后渲染
- 以下划线开头的文件（`_*.qmd`）会被跳过——它们被视为 partial（片段文件）

如果确实需要自定义渲染顺序，可以在工作流目录下创建 `_qproj.yml`：

```yaml
render:
  first:
    - 01-import.qmd
  last:
    - 99-publish.qmd
```

> **注意**：这是"逃生口"，不是推荐做法。正常项目靠数字编号就够了。0.2.0 起 `path_source()` 的顺序校验也认 `first` / `last`（带不带 `.qmd` 都行），其余按 C-locale 排序。

`_qproj.yml` 还可以放项目数据库设置（第十七节）：

```yaml
database:
  path: p0101.sqlite
  foreign_keys: true
  dictionary: db-dictionary.yml
```

---

## 十二、项目依赖图扫描（进阶）

当项目步骤变多后，记不住谁依赖谁、改了某个文件会影响哪些下游。`proj_scan_graph()` 静态扫描项目代码（不渲染、不运行），生成依赖关系图。三层，默认全开：

- **step 层**：`path_source()` 读取（`reads_from`）、`path_target()` 产出、手拼路径读别人 `d<step>/`（`bypass_access`）；含 `child:` 引入的 child 文档
- **R 层**：`.qmd` 调用了 `R/*.R` 里的哪些函数（`uses_function`），没人调用的函数标 `unused`
- **db 层**：DBI / dbplyr 读写了哪些表（`writes_table` / `reads_table`）

### 使用方式

```r
qproj::proj_scan_graph()                                  # 全部三种产物
qproj::proj_scan_graph(formats = c("json", "md"))         # 不要 HTML（没装 visNetwork 时必须这样）
qproj::proj_scan_graph(scan = "step")                     # 只扫 step 层
```

运行后在 `.qproj/graph/` 目录下生成以下文件：

| 文件 | 用途 | git |
|------|------|-----|
| `qproj-graph.json` | 机器可读的依赖数据（`qg` 读它；也给 AI 助手/脚本用） | 进 |
| `QPROJ_GRAPH_REPORT.md` | Markdown 文字版依赖报告（**改代码前先读它**） | 进 |
| `qg` | bash 查询 CLI（见下） | 进 |
| `qproj-graph.html` | **交互式可视化**（浏览器打开，可拖拽点击；需 R 包 `visNetwork`/`htmlwidgets`） | 已被 `.gitignore` |

> 默认 `formats` 含 `"html"`：缺 `visNetwork` 时非交互调用直接报错。2026-10 现状：spark 公共库没装 `visNetwork` → 在 spark 上用 `formats = c("json", "md")`。

扫描失败的文件（front matter、R 代码解析错误等）记进 `metadata.scan_errors`；用变量指定的 child 文档记进 `coverage_gaps`。有这两类时图被视为不完整。

### `qg` 命令行查询工具

`proj_scan_graph()` 会生成一个 `qg` CLI 到 `.qproj/graph/qg`（依赖 `jq`）。**先跑 `bash .qproj/graph/qg --help` 看全部子命令，别猜。**

```bash
# 新鲜度（先看这个）
qg status                      # exit 0 新鲜 / 3 过期（列出增删改的步骤与其他扫描输入）/ 2 无法判定

# 发现
qg list [step|function|table|external]  # 列节点 id；external = 经变量读取或 workflow 里找不到的步骤
qg node <id>                   # 节点元信息 + 原始边（--json 输出 {node, incoming, outgoing}）
qg edges <id>                  # 图里画出的原始边：方向、邻居、类型、置信度（含 render_order）

# 追踪（改结构前必查）
qg upstream <id>               # 直接一跳：谁直接喂 <id>（deps 的深度 1 版）
qg downstream <id>             # 直接一跳：谁直接消费 <id>（impact 的深度 1 版）
qg deps <id>                   # 传递闭包：<id> 的全部上游祖先
qg impact <id>                 # 传递闭包：改/删 <id> 波及的全部下游（blast radius）
qg paths <a> <b> [--max N]     # a→b 所有依赖路径（默认深度 6，1–100）

# 清理（投稿 / 归档前）
qg unused                      # 定义了没调用的函数 + 写了没人读的表
qg bypass                      # 手拼路径读别的步骤 d<step>/ 的地方（反模式）
qg stale [--days N]            # 无人读取且 N 天没提交的步骤（默认 90，需 git）

# 其他
qg version                     # qg 与写图的扫描器版本
```

> 输出格式：默认每行一条（可 grep / 管道）；**每个子命令都支持 `--json`**（空结果输出 `[]`）；`--human` 只用于 `node` 与 `status`。
> **要"改了 X 会影响谁"的完整答案，用 `impact`（传递闭包）；`downstream` 只给直接一跳。**

**新鲜度**：0.11.4 起 `qg` 每次查询前都把图和磁盘上的项目比一遍（步骤增删按文件名、编辑按 SHA-256；0.11.5 起还比 `R/` 函数文件、`_qproj.yml`、DESCRIPTION 的包名，0.11.6 起比 child 文档）。过期时仍按旧图回答，但在 stderr 列出变动和重扫命令。环境变量：`QG_STRICT=1` 过期时 exit 3 / 无法判定时 exit 2 拒答；`QG_NO_FRESHNESS=1` 跳过比对。

> ⚠️ **Windows Git Bash 里，`qg` 给出的重扫命令带 MSYS 路径**（`path = "/c/…"` 或 `"/tmp/…"`），Windows 的 R 认不得，照抄会报「系统找不到指定的路径」（2026-10-20 实测）。在项目根直接跑 `Rscript -e "qproj::proj_scan_graph()"` 即可（参数照抄那行里除 `path` 外的部分）。

> 旧版 `proj_scan_graph()` 生成的 `qg` 不认识新子命令。重扫会替换没被手改过的旧 `qg`（手改过的保留并警告）。

---

## 十三、命名约定总结

| 元素 | 命名规则 | 示例 |
|------|----------|------|
| QMD 文件 | `{编号}-{描述}.qmd` | `01-taxonomic-profiling.qmd` |
| 原始数据目录 | `d{编号}-{描述}/` | `d01-taxonomic-profiling/` |
| 输出数据目录 | `{编号}-{描述}/` | `01-taxonomic-profiling/` |
| 公共资源 | `d00-resource/` | 固定名称 |
| 工作流目录 | `analyses/` | 团队约定 |
| 稿件 | `analyses/manuscript/<编号>-manuscript.qmd` | `p0101-manuscript.qmd` |

**编号规则：**

| 前缀 | 使用者 | 含义 |
|------|--------|------|
| `00-` | **框架保留** | 仅用于 `data/00-raw/`；`use_qmd()` 拒绝创建，`proj_create_dir_target()` 拒绝为它建目录 |
| `01-`、`02-`… | 用户 | 正常分析步骤 |
| `001-`、`010-`… | 用户 | 需要在已有步骤之间插入时使用 |

> **为什么用户步骤必须从 `01-` 起？** 模板里的 `path_raw <- path_source("00-raw")` 要求 `00-raw` 排在所有步骤之前；`00-` 前缀由框架保留，`data/00-raw/` 下就只可能有人放进去的输入，任何步骤都不会往里写。

---

## 十四、常见问题

**Q: `proj_create_dir_target()` 的 `clean` 该怎么设？**
A: 0.2.0 起函数默认 `clean = FALSE`，模板也不再显式传 —— 修改代码后不会丢失之前的结果。需要每次 Render 都从零开始（确保完全可重复），在 setup 里改成 `proj_create_dir_target(params$name, clean = TRUE)`。`clean = TRUE` 只清空计算结果目录（`data/{name}/`），**不会动** `00-raw/` 下的原始数据；而且只在该目录解析后确实位于 `data/` 之内时才删，步骤目录是符号链接时直接拒绝。

**Q: 如何在后续 QMD 中读取前序步骤的数据？**
A: 在 setup chunk 已经创建了 `path_source`，直接用它读取：

```r
# 在 02-diversity-analysis.qmd 中读取 01 的输出
taxa_data <- readRDS(path_source("01-taxonomic-profiling", "taxa_table.rds"))
```

**Q: 能不能跨步骤读别的步骤 `00-raw/` 里的数据？**
A: 不要。分两种：
- **"产出"**（别的步骤用 `path_target` 算出来的）——只能、也必须走 `path_source("步骤名","文件名")`，不要手拼路径。
- **别的步骤的原始输入 `d<step>/`**（包括 shell 脚本预生成、多个步骤都要读的外部工具输出）——上游设计里它严格私有，`here::here(path_raw, "dNN-步骤名", …)` 是反模式：`qg bypass` 会列出，读取也不进运行记录、lineage 检查看不见（第七节有实测）。做法是让那个步骤把它经 `path_target` 发布，或把共享的外部大件放 `d00-resource/`，或把跑工具的那一步写成 Bash / Python 步骤（第十九节）。

> qproj 作者自己的 pc047e2-HpyloriDefense（2026-06 版）在 074、075 里就是手拼读 `d070-prophage-census` / `d020-dereplication` —— 那份代码早于 0.3.0 的运行记录，读它时知道这些读取不在 lineage 范围内即可，不必去改别人的代码。

**Q: 渲染报 `"03-x" is not previous to "02-y"`？**
A: 0.2.0 起越序读取直接中止。说明 02 要的东西是 03 产出的 —— 把产出它的逻辑挪到 02 之前的步骤，或重排编号。临时排查可以 `options(qproj.order_check = "warn")`，别留在代码里。

**Q: 渲染报 `Step name ... does not match the document being rendered`？**
A: `params$name`（YAML 头与 params chunk 两处）和文件名不一致，多半是复制了别的 `.qmd`。改成与文件名（去掉 `.qmd`）一致，`here::i_am()` 里的文件名也一并改；新步骤用 `use_qmd()` 建。

**Q: 可以在 analyses/ 下创建子文件夹放 QMD 吗？**
A: 分析步骤不可以。`use_qmd()` 强制所有步骤在同一目录层级（`analyses/` 下）。唯一的例外是稿件：`use_manuscript()` 放在 `analyses/manuscript/`，自带独立的 `_quarto.yml`，`here::i_am()` 仍锚在 `analyses/` 轴上，所以 `path_source()` 照常读 `analyses/data/`。

**Q: 工作流目录只能叫 `analyses` 吗？可以自定义名称吗？**
A: `proj_use_workflow()` 的 `path_proj` 参数可以接受任意名称，但**团队约定使用 `analyses`**。如果一个项目需要多个独立的工作流（例如不同课题方向），可以创建多个工作流目录：

```r
qproj::proj_use_workflow("analyses")    # 主分析
qproj::proj_use_workflow("analyses_2")  # 第二套分析
qproj::proj_use_workflow("analyses_3")  # 第三套分析
```

每个工作流目录都是独立的，各自拥有自己的 `data/` 子结构（`.gitignore` 规则同样会加上）。创建 QMD 时通过 `path_proj` 参数指定目标目录：

```r
qproj::use_qmd("01-something", path_proj = "analyses_2")
```

> **注意**：不同工作流目录之间的数据默认是隔离的，`path_source()` 只能引用同一工作流内的前序步骤。跨工作流读取只能用 `here::here()` 手动指定路径 —— 这种读取不进运行记录。

**Q: Positron 中运行报错 "does not appear to be inside a project"？**
A: 这是因为当前目录缺少 DESCRIPTION 文件。使用 `qproj::proj_create(".")` 初始化（目录须为空），或 `usethis::create_package(".", open = FALSE)`（目录可非空）即可解决。

---

## 十五、chunk 缓存与 `proj_doctor()`（0.8.0 – 0.12.0）

**原则：缓存逐 chunk 开。** 新模板的 `_quarto.yml` 不设 `cache`。Quarto 一旦在 `_quarto.yml`、`_metadata.yml` 或文档 front matter 里看到显式的 `cache: false`，会把**所有** chunk 的缓存强制关掉（`#| cache: true` 也失效）；全局 `cache: true` 则让每个 chunk 都缓存，包括写文件的。所以两个都不写，慢的 chunk 自己 `#| cache: true`。

渲染时 `proj_create_dir_target()` 给缓存 chunk 加上几个指纹，任何一个变了缓存就失效：

| 指纹 | 跟踪什么 | 固定住（不让它触发重跑） |
|------|----------|--------------------------|
| `qproj.code` | 项目包代码：`R/` 解析后的代码（注释与 roxygen 不算）、DESCRIPTION、NAMESPACE、`inst/`、`src/` 等 | `#| qproj.code: "fixed"` |
| `qproj.env` | R 版本、chunk 运行时已加载包的版本、DESCRIPTION `Depends` / `Imports` 包的版本 | `#| qproj.env: "fixed"` |
| `qproj.db` | chunk 读写的数据库表（0.12 按表；分析不出就跟整个库文件） | `#| qproj.db: "fixed"` |

另外 0.11.0 起每个 chunk 都依赖它前面所有 chunk（`knitr::dep_prev()`）：一个缓存 chunk 重跑，它后面所有缓存 chunk 的缓存一并作废，不用写 `#| dependson`。

**还得自己写 `cache.extra` 的情形**（指纹管不到的）：

```r
#| label: summarise
#| cache: true
#| cache.extra: !expr tools::md5sum(path_source("01-import", "x.tsv"))
x <- read.delim(path_source("01-import", "x.tsv"))
```

- 读文件：`tools::md5sum(path_source(...))` —— 文件内容不在 knitr 的缓存键里，上游重算后不加这个就回放旧结果
- 经项目函数间接读库、钩子没识别出来的：`qproj::proj_db_fingerprint()`（可给 `tables`）
- 用了前面**未缓存** chunk 算出的对象：`digest::digest(<对象>)`，或干脆 `cache: false`

**缓存 chunk 不写文件、不写表**：命中缓存时 chunk 不执行，写入不会发生；配合 `clean = TRUE` 会出现产出缺失。

`proj_doctor()`（只读、不建任何文件）的检查项：

| 检查 | 含义 |
|------|------|
| `global_cache` | `_quarto.yml` / `_metadata.yml` 全局开了缓存（旧模板的 `cache: true`） |
| `document_cache` | 文档 front matter 给全部 chunk 开了缓存 |
| `cache_disabled` | 显式 `cache: false` 让 `#| cache: true` 的 chunk 永远不缓存 |
| `cache_before_load_all` | 缓存 chunk 在 `load_all()` 那个 chunk 之前或就是它 |
| `cache_level` | `cache: 1` / `cache: 2`：只哈希固定选项，指纹够不着 |
| `unprotected_cache` | 缓存 chunk 之前没有未缓存的 `proj_create_dir_target()`，指纹没设上 |
| `cached_side_effect` | 缓存 chunk 写文件或表 |
| `cached_input` | 缓存 chunk 读文件 / 库却没有 `cache.extra` |
| `cached_stale_object` | 缓存 chunk 用了前面未缓存 chunk 赋值的对象 |
| `stale_packages` | knitr 缓存里记着已经找不到的包（下次渲染会自动清掉） |
| `db_*` | 数据库族：字典缺失 / 无效 / 未描述的表列 / 字典条目对不上、主键不唯一、写表步骤上次没跑完、多个步骤写同一张表 |

`proj_doctor(checks = "cache")` 只跑缓存族。旧模板（全局 `cache: true`）的项目通常一次报出 `global_cache`、`cache_before_load_all`、`unprotected_cache`、`cached_side_effect`、`cached_input` 一串，根子都是那一行全局设置。

总开关：`options(qproj.cache_guard = FALSE)` 关掉上面全部指纹机制；准备失败时本文档余下部分自动不缓存并给警告。

---

## 十六、运行记录与 lineage 检查（0.3.0+）

每次渲染一个步骤，qproj 在项目根的 `.qproj/provenance/` 下留一份记录：文档 sha256、项目 commit 与未提交改动、qproj / R / Quarto / 包版本、经 `path_source()` 读的每个路径（文件带 sha256）、本步 `d<step>/` 全部文件、产出目录里每个文件（标 `produced` 新产生 / `preexisting` 原有）。步骤代码不用改：`proj_create_dir_target()` 打开记录，渲染结束的钩子关闭它。

```
.qproj/provenance/
├── latest/<step>.json        # 每步最近一次完成的运行（进 git）
├── runs/<run_id>/            # 每次运行（start.json + finish.json；只有 start 说明没跑完）
│   └── .gitignore            # runs/ 只留本地
└── tables/                   # 0.12：写表的步骤留的表记录（进 git）
```

字段定义：`system.file("schema/run-manifest-v1.json", package = "qproj")`。

```r
qproj::proj_lineage_check()                 # 每个步骤记录的每个输入：ok / stale / missing / unhashed / writing / unregistered
qproj::proj_lineage_check(strict = TRUE)    # 有 stale / missing（含表的 writing）就报错 —— 投稿前门槛
```

读法：
- **只看有记录的步骤**。从没在 ≥ 0.3.0 下渲染过的步骤不出现在结果里 —— 空结果不等于全部新鲜。
- **只看被记录的读取**：`path_source()` 拼出的路径和本步 `d<step>/`。`path_resource` / `path_raw` 字符串拼的、跨工作流 `here::here()` 读的，都不在判断范围（第五节末尾有让 `d00-resource` 被记录的写法）。
- 0.12 起表也算：读表步骤记下读到的表签名，写表步骤在 `tables/` 留表记录；表变了报 `stale`，写表步骤上次没跑完报 `writing`，表没有完成的记录报 `unregistered`。
- 比对用 sha256（两边都有时），否则用大小 + 修改时间。

开销：收尾时对 `d<step>/` 和 `data/<step>/` 的每个文件算 sha256（单文件上限 `options(qproj.hash_max_bytes = 512 * 1024^2)`，超过的只记大小与时间）。几十 GB 的步骤收尾会明显变慢；`options(qproj.manifest = FALSE)` 关闭记录。

R 脚本或交互 session 里显式开关：

```r
qproj::proj_run_start("02-clean")   # 只在渲染之外用；渲染会自己打开
# ... 步骤代码 ...
qproj::proj_run_finish()
```

---

## 十七、项目数据库（0.7.0+）

一个项目一个 SQLite，各步骤用同一种方式打开：

```r
con <- qproj::proj_db_connect(create = TRUE)   # 建库的那一步
con <- qproj::proj_db_connect()                # 其余步骤：文件必须已存在（拼错路径立刻报错）
DBI::dbWriteTable(con, "sample_metadata", df, overwrite = TRUE)
qproj::proj_db_disconnect(con)                 # 只关一次；关闭出问题只给消息，不中断渲染
```

取代各项目 `R/utils.R` 里各写一份的 `load_db()` / `close_db()`。连接默认 30 秒 busy timeout，可选外键、只读（`read_only = TRUE`）。

**库文件位置**（`proj_db_path()`），依次取：
1. 环境变量 `QPROJ_DB`（重建脚本把渲染指向另一个库文件时用）
2. `analyses/_qproj.yml` 的 `database:`（一个路径，或带 `path` / `foreign_keys` / `busy_timeout` / `dictionary` 的映射）
3. `analyses/<编号>.sqlite`，编号取自项目目录名（`p0101-x` → `p0101`，`pc047dev` → `pc047`）；取不到编号时（`exd001e1`、`tailocin-01`、`ExD001`）是 `analyses/project.sqlite`

workflow 内的 `*.sqlite` 及 journal / WAL 已被 `.gitignore`。

**数据库字典** `analyses/db-dictionary.yml`（或 `_qproj.yml` 的 `database: dictionary:` 指定）：告诉人和 AI 每张表、每列是什么。一条描述一张表，或用 `table_pattern` 描述一族同构表：

```yaml
version: 1
tables:
  - table_pattern: '^(HCvirus|LCvirus)_abundance_count$'
    grain: one vOTU of the {1} catalog
    primary_key: [votu_id]
    description: mapped read counts per sample
    columns:
      - column: votu_id
        type: string
        role: primary_key
        description: vOTU identifier
      - column_pattern: '^S[0-9]{2}$'
        type: integer
        role: measure
        unit: reads
        description: reads of sample {column}
```

```r
qproj::proj_db_describe()                          # 每张表：grain、主键、行数、写入步骤
qproj::proj_db_describe("HCvirus_abundance_count") # 该表每一列
qproj::proj_db_describe(verify = TRUE)             # 核对记录的签名与当前内容（查出记录之外的改动）
```

**谱系**（0.12）：渲染时静态分析每步代码（含 child 文档和它调用的项目函数）读写了哪些表，写进运行记录；写表步骤在 `.qproj/provenance/tables/` 留表记录。规则：**一张表只由一个步骤写**（`proj_doctor()` 的 `db_multiple_writers` 会查）；表名或 SQL 运行时才拼出来的、连接交给别的包的函数的，记为"未解析"，缓存跟随整个库文件。

---

## 十八、样本 metadata 工作簿（0.4.0+）

样本与实验设计信息放一本工作簿 `analyses/<编号>-metadata.xlsx`，人直接编辑。`dictionary` sheet 描述每张表每一列（类型、单位、允许值、主键、引用、说明），工作簿自带 schema；`samples` 是分析样本表，需要时加 `subjects`、`libraries`、`design` 等表。

```r
qproj::proj_metadata_template()                              # 新建空白工作簿
qproj::proj_metadata_template(from = "old-metadata.xlsx")    # 迁移已有表：主表变 samples，按观察到的类型生成字典

md <- qproj::proj_metadata_import()          # 逐格读取、按字典转类型、校验 → 带类型的表 + 报告
md$tables$samples
md$report                                    # 错误与警告，带表、列、行
```

**分析不直接读工作簿。** 导入步骤（通常 `01-import`）：

```r
md <- qproj::proj_metadata_import(snapshot = TRUE, step = params$name)
```

校验后把规范化的表（TSV + `schema.json` + `provenance.json`）写进 `data/<step>/metadata/<snapshot_id>/`，并把工作簿摘要记进本步运行记录；下游经 `path_source()` 读快照，metadata 一改，lineage 检查就能看到。

其余：
- `proj_metadata_summary(md)`：给人和 AI 的 Markdown 摘要
- `proj_metadata_diff(old, new)`：按表、主键、列比较两版（行增删、值变化、列增删改名、类型单位变化）；`old` 可以是快照目录，`new` 默认当前工作簿
- `proj_metadata_export(md, format)`：`"tsv"`（git 友好的镜像）、`"digest"`、`"sqlite"`（写进项目库，带主键唯一索引）、`"ena"`（`vpipe ena samples` 的样本表）、`"samplesheet"`（并进 labdata samplesheet）、`"submission"`（给合作者 / 期刊的格式化工作簿）

---

## 十九、Bash / Python 步骤（0.10.0+）

步骤主体是外部工具、要在 spark 上以 `sbatch` 或 Python 脚本运行时，用 `qproj.sh` / `qproj.py` 获得与 `.qmd` 相同的路径纪律和运行记录：只写自己的 `data/<step>/`，经 `path_source` 读上游，跑完留记录，`proj_lineage_check()` 一视同仁。

```bash
Rscript -e 'cat(qproj::proj_shell_lib())'    # qproj.sh 的位置（spark：/opt/bio/R/4.5/library/qproj/scripts/qproj.sh）
Rscript -e 'cat(qproj::proj_python_lib())'   # qproj.py（只依赖 Python 标准库，3.8+）
```

**Bash 驱动**：

```bash
source "<qproj.sh 路径>"
qproj_init --step-file "${BASH_SOURCE[0]}"     # 定位 ROOT（含 _quarto.yml 与 data/ 的目录）与 STEP
qproj_run_start
input="$(path_source 010-import samples.tsv)"  # 赋值与使用分两行（见下）
out="$(path_target result.tsv)"
tool --in "$input" --out "$out"
qproj_run_finish
```

**sbatch 下**脚本从 spool 副本运行，`$0` / `BASH_SOURCE` 都不可靠：用 `Rscript -e 'cat(qproj::proj_shell_bootstrap(step = "070-prophage-census"))'` 生成官方引导块，原样贴进驱动脚本；它按 `$QPROJ_SH` → 已安装包 → `$QPROJ_HOME` 开发副本的顺序找 `qproj.sh`，显式给 `--step`。

**Python 步骤**：

```python
import os, sys
sys.path.insert(0, os.path.dirname(os.environ["QPROJ_PY"]))   # qproj.sh 会导出 QPROJ_PY
import qproj

qproj.init(step_file=__file__)
qproj.run_start()
x = qproj.path_source("010-import", "x.tsv")
out = qproj.path_target("result.tsv")
...
qproj.run_finish()
```

由已开记录的 Bash 驱动启动的 Python 步骤，读写会并进驱动的那份记录。

**shell / Python 的路径函数**：

| 函数 | 指向 | 注意 |
|------|------|------|
| `path_target f` | `data/<STEP>/f` | |
| `path_source up f` | `data/<up>/f` | |
| `path_raw f` | `data/00-raw/d<STEP>/f` | ⚠️ **= R 里的 `path_data`**；R 里的 `path_raw` 是 `00-raw` 根 |
| `path_resource f` | `data/00-raw/d00-resource/f` | |
| `path_run_state f` | `data/.run-state/<STEP>/f` | Nextflow 的 work / cache / history；与产出目录平级，`create_dir_target --clean` 不删它 |
| `create_dir_target [--clean]` | 建（并可清空）`data/<STEP>` | 唯一有副作用的路径函数 |

⚠️ 驱动通常跑在 `set -euo pipefail` 下：**赋值与使用分两行**（`input="$(path_source …)"` 再 `tool "$input"`）。写成 `tool "$(path_source …)"` 时，命令替换的失败会被外层命令的退出码盖掉。

⚠️ `qproj_run_start` 要**直接调用**，别放进 `$( … )`：它要导出 `QPROJ_RUN_ID` / `QPROJ_RUN_DIR`，在子 shell 里导出等于没导出，后面的读写就不进记录。

> 资源申请、排队与提交走 `/spark-advisor`（`gen-sbatch → submit`）；本节只管路径与记录。

---

## 二十、从 0.1.x 升级

**会变成中止的两件事**（以前只是警告或根本不查）：
1. `path_source()` 越序读取 → `qproj_order_error`
2. 渲染时 `params$name` ≠ 文件名 → `qproj_identity_error`

渲染前静态预检（只读）：

```bash
Rscript ~/.claude/skills/qproj-helper/scripts/preflight.R <项目根> [<项目根> ...]
```

输出 `IDENTITY` / `ORDER` 行的步骤升级后会中止；`BYPASS` 行不中止（第七节）；每个工作流还列出 `_quarto.yml` 的 `cache` 设置。

**不用改、照常能跑的**：旧 setup chunk（`clean = FALSE` + 三行 `dir.create`）、旧的 `here::i_am(paste0(params$name, ".qmd"))`、没有 `proj_run_finish()` 的收尾 chunk（渲染钩子会关闭记录）。

**建议改的**（会改变渲染行为，先征得用户同意）：
- `_quarto.yml` 的全局 `cache: true` → 删掉，慢 chunk 自己 `#| cache: true`，读文件的加 `cache.extra`（第十五节）；`cache: false` 同样删掉（它让所有 `#| cache: true` 失效）
- packages chunk 补 `dplyr::setdiff`（撞到 Biostrings 冲突时）
- 项目 `CLAUDE.md` 的 qproj 规则节换成 `templates/CLAUDE-qproj.md` 的版本
- `R/utils.R` 里自写的 `load_db()` / `close_db()` → `proj_db_connect()` / `proj_db_disconnect()`

**升级后第一次渲染**：缓存键里多了指纹，所有缓存 chunk 会重算一遍；重计算步骤先估时间，必要时上 spark 排队跑。旧版生成的 `.qproj/graph/qg` 重扫一次即被替换。
