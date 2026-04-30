---
name: qproj-helper
description: |
  qproj 工作流指导助手。当用户提到"qproj"、"创建qmd"、"新建分析步骤"、"初始化分析项目"、
  "use_qmd"、"analyses目录"、"path_target"、"path_source"时触发。
  分析用户当前项目状态，给出应在 R Console 中执行的命令。
  注意：Claude Code 无法直接在 R Console 中输入，所有 R 命令需要告知用户自行粘贴执行。
---

# qproj Helper — 工作流指导助手

## 核心定位

你是 qproj 使用顾问。用户来问问题时，你需要：
1. **判断用户处于哪个阶段**（见下方决策树）
2. **给出可直接粘贴到 R Console 的命令**
3. **解释命令会做什么**（一句话）

输出格式统一为：
```
请在 R Console 中执行：
<代码块>

这会：<一句话说明效果>
```

**关键约束**：Claude Code 无法操作 R Console。所有 qproj 函数（`proj_create`、`use_qmd` 等）必须由用户在 RStudio/Positron 的 Console 中手动执行。

## 决策树

收到用户请求后，按以下顺序判断：

### 场景 1：从零创建项目

**信号**：用户说"新建项目"、"创建分析项目"、"初始化"，且当前目录无 DESCRIPTION。

```r
# 步骤 1：创建项目脚手架
qproj::proj_create("项目路径")

# 步骤 2：在 RStudio/Positron 中打开该文件夹，然后执行：
qproj::proj_use_workflow("analyses")

# 步骤 3：创建第一个分析步骤
qproj::use_qmd("01-步骤名", path_proj = "analyses", open = FALSE)
```

提醒用户：
- `proj_create()` 要求目标目录为空或不存在
- 如果目录已有文件，改用 `usethis::create_package(".", open = FALSE)`
- Windows 路径用正斜杠：`"C:/Users/xxx/Desktop/project"`
- 步骤 2 和 3 必须在打开项目后的 Console 中执行

### 场景 1.5：初始化完成后 — 生成项目 CLAUDE.md

**触发时机**：场景 1 的三步命令用户都执行完毕后，主动询问：

> 项目骨架已就绪。是否要生成项目级 CLAUDE.md？它会让 Claude 在写代码时自动遵守 qproj 的 path binding 规则。

**用户同意后**，执行以下流程：

#### Step A：生成 CLAUDE.md 骨架

在项目根目录创建 `CLAUDE.md`，写入 qproj 固定规则部分（Commands、Data Layout、Workflow Rules）。
这部分内容从下方「CLAUDE.md 模板」章节复制，**不需要用户填写**。

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

### 场景 3：数据应该放哪里

**信号**：用户问"数据放哪"、"原始文件放哪个目录"、"怎么导入数据"。

回答模板：

| 数据类型 | 放到 | 路径 |
|----------|------|------|
| 本步骤专属原始数据 | `path_data` | `analyses/data/00-raw/dXX-步骤名/` |
| 项目共享资源（参考库等） | `path_resource` | `analyses/data/00-raw/d00-resource/` |
| 计算产出（代码自动写入） | `path_target` | `analyses/data/XX-步骤名/` |

提醒：首次 Render 该 .qmd 后目录才会自动创建。也可以手动建：

```r
dir.create("analyses/data/00-raw/dXX-步骤名", recursive = TRUE)
```

### 场景 4：怎么读取上游步骤的数据

**信号**：用户问"怎么读上一步的结果"、"path_source 怎么用"。

```r
# 在当前 .qmd 的 Tasks 区域中：
df <- readRDS(path_source("01-import", "result.rds"))
```

提醒：`path_source()` 会校验顺序——只能读编号比自己小的步骤。

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

## 快速参考

### 四个路径绑定

| 绑定 | 类型 | 指向 | 作用 |
|------|------|------|------|
| `path_target` | 函数 | `data/<step>/` | 本步骤唯一写入位置 |
| `path_source` | 函数 | `data/<prev>/` | 读上游产出（有顺序校验） |
| `path_resource` | 字符串 | `data/00-raw/d00-resource/` | 项目共享资源 |
| `path_data` | 字符串 | `data/00-raw/d<step>/` | 本步骤私有原始输入 |

### 核心函数

| 函数 | 用途 | 何时用 |
|------|------|--------|
| `proj_create(path)` | 创建项目脚手架 | 从零开始时 |
| `proj_use_workflow("analyses")` | 创建工作流目录 | 项目初始化后 |
| `use_qmd("XX-name")` | 创建分析步骤 | 添加新步骤时 |
| `proj_install_deps()` | 安装依赖 | 克隆项目后 |
| `proj_update_deps()` | 更新 DESCRIPTION | 加了新包后 |
| `proj_check_deps()` | 检查依赖一致性 | 提交前 |

### 命名约定

- `00-` 前缀：框架保留（`data/00-raw/`），用户不要用
- `01-`、`02-`…：用户分析步骤
- `d` 前缀：原始数据子目录标记（`d01-import/` = 步骤 01 的私有输入）
- 步骤名：英文、连字符分隔（`01-taxonomic-profiling`）

### 数据流规则

- 单向链式：只能读编号比自己小的步骤产出
- `path_data` 严格私有：下游无法通过 qproj API 访问
- `clean = TRUE` 只清空产出目录，不动 `00-raw/` 下的原始数据

## 回答原则

1. **先判断场景再回答**——不要一上来就倒出全部知识
2. **给命令，不给教程**——用户要的是可粘贴的代码，不是原理讲解
3. **主动帮用户确定编号和命名**——扫描现有文件后给出具体的 `use_qmd()` 调用
4. **遇到复杂问题才读 references/**——日常问题用上方速查表即可
