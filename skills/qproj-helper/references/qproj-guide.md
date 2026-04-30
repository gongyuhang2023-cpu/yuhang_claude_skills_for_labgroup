# qproj 创建 QMD 文件使用指南

## 一、qproj 是什么？

**qproj** 是 rujinlong 基于 projthis 精简重写的 R 包，专为 Quarto `.qmd` 工作流设计。核心理念是通过目录结构和命名约定，组织分析文件的执行顺序和数据流向。

## 二、安装

```r
# install.packages("pak")
pak::pak("rujinlong/qproj")
```

> 如果提示缺少其他包，根据报错信息用 `install.packages()` 补装。

---

## 三、完整工作流程（从零创建项目）

### 第 1 步：创建项目

#### 方式 A：使用 qproj 自带函数（推荐，RStudio / Positron 通用）

```r
# 创建项目脚手架（自动生成 DESCRIPTION / NAMESPACE / README / .gitignore）
qproj::proj_create("~/Desktop/pc047")
```

> **Windows 路径小工具**：Windows 资源管理器复制的路径是反斜杠格式，R 中不能直接用。安装 [rpath-copier](https://github.com/gongyuhang2023-cpu/rpath-copier) 后，在资源管理器中选中文件夹按 `Ctrl+Shift+C`，即可复制正斜杠格式路径（如 `C:/Users/xxx/Desktop/pc047`），直接粘贴到 R Console 中使用。

然后在 RStudio 或 Positron 中打开该文件夹：
- **RStudio**：File → Open Project → 选择 `pc047` 文件夹
- **Positron**：File → Open Folder → 选择 `pc047` 文件夹

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

> **注意**：`qproj::proj_create()` 要求目录为空或不存在。如果目录已有文件，建议先新建一个空文件夹，用 `proj_create()` 初始化后再把文件移进去。

### 第 2 步：创建 analyses 工作流目录

在 Console（RStudio 或 Positron 均可）中执行：

```r
qproj::proj_use_workflow("analyses")
```

**这条命令会：**
1. 在项目根目录下创建 `analyses/` 文件夹及 `analyses/README.md`
2. 在 `analyses/` 下创建 `data/` 文件夹及 `data/README.md`
3. 在 `.gitignore` 中添加规则，忽略 `analyses/data/*`（但保留 README）

> **注意**：工作流目录**建议命名为 `analyses`**，这是团队约定。

### 第 3 步：创建 QMD 分析文件

```r
qproj::use_qmd("01-taxonomic-profiling")
```

**这条命令会：**
1. 在 `analyses/` 下生成 `01-taxonomic-profiling.qmd` 文件
2. 自动填入 YAML 头信息（标题、日期）
3. 生成唯一 UUID 标识符（用于 `here::i_am()` 定位）
4. 预置标准代码 chunk 模板（params、setup、packages、tasks、output）

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

### 第 4 步：运行 QMD 文件

#### RStudio
打开 `01-taxonomic-profiling.qmd`，点击 **Render** 按钮（或 `Ctrl+Shift+K`）。

#### Positron
打开 `01-taxonomic-profiling.qmd`，有以下几种方式 Render：
- 点击编辑器右上角的 **Preview** 按钮
- 使用快捷键 `Ctrl+Shift+K`
- 在终端中手动执行：`quarto render analyses/01-taxonomic-profiling.qmd`

**首次 Render 会自动创建（由模板 setup chunk 完成）：**

1. `data/00-raw/d01-taxonomic-profiling/` — 存放该分析所需的**原始数据**（私有，仅本步骤使用）
2. `data/00-raw/d00-resource/` — 存放**公共资源**文件（所有步骤共享）
3. `data/01-taxonomic-profiling/` — 存放该分析的**计算结果**（图、表、中间文件）

---

## 四、生成的项目结构

```
pc047/                                    # 项目根目录
├── README.md                             # 项目说明
├── DESCRIPTION                           # 包依赖声明
├── NAMESPACE                             # 命名空间
├── .git/                                 # Git 版本控制
├── .gitignore
│
└── analyses/                             # 工作流目录
    ├── README.md                         # 工作流说明（自动生成）
    ├── 01-taxonomic-profiling.qmd        # 分析文件 1
    ├── 02-diversity-analysis.qmd         # 分析文件 2
    ├── 03-differential-abundance.qmd     # 分析文件 3
    │
    └── data/                             # 数据目录
        ├── README.md                     # 说明文件（确保 Git 追踪此目录）
        │
        │── 01-taxonomic-profiling/       # 01 的计算结果（path_target 写入）
        │── 02-diversity-analysis/        # 02 的计算结果
        │── 03-differential-abundance/    # 03 的计算结果
        │
        └── 00-raw/                       # 外部输入总目录（与计算结果隔离）
            ├── d00-resource/             # 项目共享资源（参考基因组等）
            ├── d01-taxonomic-profiling/  # 01 的专属原始数据（私有）
            ├── d02-diversity-analysis/   # 02 的专属原始数据（私有）
            └── d03-differential-abundance/
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
```

模板 YAML 简洁，更多格式配置推荐统一放在 `_quarto.yml` 中（见第八节）。

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

wd <- "analyses"
if (basename(getwd()) != wd) {
    setwd(here::here(wd))
}
here::i_am(paste0(params$name, ".qmd"), uuid = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")

qproj::proj_create_dir_target(params$name, clean = FALSE)

# ---- 五个路径绑定 ----
path_target   <- qproj::proj_path_target(params$name)            # 函数
path_source   <- qproj::proj_path_source(params$name)            # 函数
path_raw      <- path_source("00-raw")                           # 字符串
path_resource <- here::here(path_raw, "d00-resource")            # 字符串
path_data     <- here::here(path_raw, paste0("d", params$name))  # 字符串

# 自动创建目录
dir.create(path_raw, recursive = TRUE, showWarnings = FALSE)
dir.create(path_data, recursive = TRUE, showWarnings = FALSE)
dir.create(path_resource, recursive = TRUE, showWarnings = FALSE)
```

> **关键理解**：
> - `here::i_am()` + UUID 确保无论从哪里运行，都能正确定位项目根目录
> - `clean = FALSE` 是**模板**默认值，保留已有输出文件，适合增量开发；改为 `clean = TRUE` 则每次 Render 清空产出目录重建（不会影响 `00-raw/` 下的原始数据）
> - 注意：`proj_create_dir_target()` **函数本身**默认是 `clean = TRUE`，模板故意覆盖为 `FALSE`

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
| `path_source` | **函数** | 读取上游步骤的产出 | 自带顺序校验（读编号更大的会警告） |
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
  devtools::load_all()  # 加载项目自定义函数（如果有的话）
})
```

### Tasks 区域（用户编写分析代码）

```r
## Tasks

### Task 1: Data import

# 在此编写数据导入、清洗、分析、可视化代码
```

### Output chunk（列出输出文件）

```r
#| label: list-files-target

knitr::kable(qproj::proj_dir_info(path_target(), tz = "CET"))
```

---

## 六、核心函数速查

### 项目与工作流

| 函数 | 用途 | 示例 |
|------|------|------|
| `proj_create()` | 创建项目脚手架 | `qproj::proj_create("my-project")` |
| `proj_use_workflow()` | 创建工作流目录 | `qproj::proj_use_workflow("analyses")` |
| `use_qmd()` | 创建 QMD 分析文件 | `qproj::use_qmd("01-import")` |

### 目录与路径管理（在 QMD 文件内部使用）

| 函数 | 用途 | 说明 |
|------|------|------|
| `proj_create_dir_target(name, clean)` | 创建输出目录 | 函数默认 `clean=TRUE`（清空重建），模板覆盖为 `FALSE`（保留已有） |
| `proj_path_target(name)` | 获取输出路径函数 | 返回函数，用 `path_target("file.rds")` 调用 |
| `proj_path_source(name)` | 获取前序步骤的数据路径函数 | 自动验证依赖顺序 |
| `proj_dir_info(path)` | 列出目录内文件信息 | 返回 tibble（路径、类型、大小、时间） |

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
- 每个 QMD **只能读取**编号比自己小的 `data/` 子目录（通过 `path_source`）
- `path_source()` 会自动验证依赖顺序，违反时发出警告（warning，不阻断执行）
- 每个 QMD 的原始数据（`path_data`）是**私有**的，下游步骤无法通过 qproj 函数访问

**在 QMD 中读取前序数据的方式：**

```r
# 在 02-diversity-analysis.qmd 中，setup 已创建 path_source
# 直接用 path_source 读取 01 的产出
taxa_data <- readRDS(path_source("01-taxonomic-profiling", "taxa_table.rds"))
```

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

### 克隆项目后一键安装所有包

```r
qproj::proj_install_deps()
```

使用 `pak::local_install_deps()` 安装 DESCRIPTION 中声明的所有包。

> **注意**：这套机制**只声明依赖，不锁定版本**，与 `renv` 的定位不同。如果将来需要锁版本，`renv` 原生支持读 DESCRIPTION，升级路径平滑。

---

## 九、完整操作示例

以创建微生物组分析项目 `pc047` 为例：

```r
# ===== 1. 创建项目 =====
qproj::proj_create("~/Desktop/pc047")
setwd("~/Desktop/pc047")

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
# 先 Render 一次 01，让模板自动创建目录结构
# 然后将原始数据放入对应的 d{name} 文件夹：
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
# git add . → git commit → git push
```

---

## 十、交互式工作流注意事项

在 RStudio 的交互 session 中，所有打开的 `.qmd` 文件**共用同一个 R 全局环境**。五个路径绑定（`path_target` 等）是 session 级变量，最近一次跑过的 setup chunk 决定了它们指向哪里。

### 两种典型出错场景

**场景 1：绑定残留（静默错误，最危险）**

你在 `01-import.qmd` 中跑完了 setup，然后切到 `02-clean.qmd` 直接执行代码 chunk，**忘了重跑 02 的 setup**。此时 `path_target` 仍然指向 `data/01-import/`，你的结果会写到错误目录，**且没有任何报错**。

**场景 2：绑定缺失（直接报错）**

当前 session 里根本没跑过任何 setup，第一个使用 `path_target` 的 chunk 就会报 `Error: object 'path_target' not found`。

### 经验法则

> **每次切到另一个 `.qmd` 准备执行代码，先重跑它的 `params` 和 `setup` chunk。**

### 规避方法（从轻到重）

1. **用 `quarto render` 跑完整文件**——自动获得全新 session 和正确绑定
2. **切文件前重启 R**（RStudio：`Ctrl+Shift+F10`）
3. **用 Positron**——支持每个 `.qmd` 独立 session，从结构上消除此问题

---

## 十一、渲染顺序配置

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

> **注意**：这是"逃生口"，不是推荐做法。正常项目靠数字编号就够了。

---

## 十二、命名约定总结

| 元素 | 命名规则 | 示例 |
|------|----------|------|
| QMD 文件 | `{编号}-{描述}.qmd` | `01-taxonomic-profiling.qmd` |
| 原始数据目录 | `d{编号}-{描述}/` | `d01-taxonomic-profiling/` |
| 输出数据目录 | `{编号}-{描述}/` | `01-taxonomic-profiling/` |
| 公共资源 | `d00-resource/` | 固定名称 |
| 工作流目录 | `analyses/` | 团队约定 |

**编号规则：**

| 前缀 | 使用者 | 含义 |
|------|--------|------|
| `00-` | **框架保留** | 仅用于 `data/00-raw/`，用户不要用 |
| `01-`、`02-`… | 用户 | 正常分析步骤 |
| `001-`、`010-`… | 用户 | 需要在已有步骤之间插入时使用 |

> **为什么用户步骤必须从 `01-` 起？** 因为模板中 `path_source("00-raw")` 依赖字典序校验（`"00-raw"` 排在 `"01-..."` 前面才能通过）。如果把某步命名为 `00-import`，会触发校验警告。

---

## 十三、常见问题

**Q: 为什么 qproj 模板中 `proj_create_dir_target(name, clean = FALSE)`？**
A: 模板写 `clean = FALSE` 是为了增量开发——修改代码后不会丢失之前的结果。如需每次 Render 都从零开始（确保完全可重复），改为 `clean = TRUE`。注意 `clean = TRUE` 只清空计算结果目录（`data/{name}/`），**不会动** `00-raw/` 下的原始数据，所以可以放心开启。

**Q: 如何在后续 QMD 中读取前序步骤的数据？**
A: 在 setup chunk 已经创建了 `path_source`，直接用它读取：

```r
# 在 02-diversity-analysis.qmd 中读取 01 的输出
taxa_data <- readRDS(path_source("01-taxonomic-profiling", "taxa_table.rds"))
```

**Q: 为什么我不能用 path_source 读取其他步骤的原始数据（path_data）？**
A: 这是设计意图。每个步骤的原始数据（`data/00-raw/d{name}/`）是私有的，qproj 没有提供函数让下游访问它。原始数据应该由当前步骤清洗处理后，通过 `path_target` 发布给下游。如果确实需要跨步骤读原始数据，可以用 `here::here()` 手动拼路径，但不推荐。

**Q: 可以在 analyses/ 下创建子文件夹放 QMD 吗？**
A: 不可以。`use_qmd()` 强制所有 QMD 文件在同一目录层级（`analyses/` 下），不允许子目录嵌套。这是设计约束。

**Q: 工作流目录只能叫 `analyses` 吗？可以自定义名称吗？**
A: `proj_use_workflow()` 的 `path_proj` 参数可以接受任意名称，但**团队约定使用 `analyses`**。如果一个项目需要多个独立的工作流（例如不同课题方向），可以创建多个工作流目录：

```r
qproj::proj_use_workflow("analyses")    # 主分析
qproj::proj_use_workflow("analyses_2")  # 第二套分析
qproj::proj_use_workflow("analyses_3")  # 第三套分析
```

每个工作流目录都是独立的，各自拥有自己的 `data/` 子结构。创建 QMD 时通过 `path_proj` 参数指定目标目录：

```r
qproj::use_qmd("01-something", path_proj = "analyses_2")
```

> **注意**：不同工作流目录之间的数据默认是隔离的，`path_source()` 只能引用同一工作流内的前序步骤。跨工作流读取数据需要用 `here::here()` 手动指定路径。

**Q: Positron 中运行报错 "does not appear to be inside a project"？**
A: 这是因为当前目录缺少 DESCRIPTION 文件。使用 `qproj::proj_create(".")` 初始化（目录须为空），或 `usethis::create_package(".", open = FALSE)`（目录可非空）即可解决。

