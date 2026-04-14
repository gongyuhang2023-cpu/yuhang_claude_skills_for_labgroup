---
name: meeting-ppt-vba
description: |
  VBA 宏方案组会PPT生成工具。生成完整 VBA 代码，用户在 PowerPoint 中执行即可。
  所有元素为原生 PowerPoint 对象，100% 可编辑，不会布局错乱。
  莫兰迪配色、ABT叙事、AE标题、色盲友好。
  当用户提到"/ppt-vba"、"生成VBA PPT"、"可编辑PPT"时触发。
---

# 组会PPT — VBA 宏方案 (Meeting PPT VBA)

## 概述

本技能生成**完整 VBA 宏代码**，用户在 PowerPoint 中一键执行即可生成专业组会 PPT。

**核心优势**（对比 python-pptx 方案）：
- **100% 原生可编辑**：所有文本框、表格、形状均为 PowerPoint 原生对象
- **自适应文字**：`TextFrame.AutoSize = ppAutoSizeShapeToFitText`，编辑时不会溢出
- **无需安装 Python**：只需 PowerPoint + 粘贴代码
- **莫兰迪配色**：低饱和高级感，替代旧版 Deng Lab 蓝绿

**工作流**：框架规划(含图片映射) → 规范审查 → 图片提取/验证 → Gemini 审核 → VBA 代码生成(含 AddPicture)

## 触发条件

- "/ppt-vba"
- "生成VBA PPT"、"可编辑PPT"
- "用VBA做PPT"

---

## 懒加载规则（重要！）

> **不要在技能启动时一次性读取所有 references！按阶段需要逐步读取。**

| 阶段 | 需要读取的文件 | 时机 |
|------|---------------|------|
| 阶段一 | 无 | 仅收集信息、扫描项目、生成框架 |
| 阶段二 | `references/scientific_norms.md` + `references/design_guide.md` | 执行规范审查时 |
| 阶段三 | 无 | 图片检查基于项目目录扫描 |
| 阶段四 | `references/vba_templates.md` + `references/outline_spec.md` | 生成 VBA 代码时 |

---

## 阶段一：框架规划与知识联动

### Step 1.1: 收集信息与工作目录确认

```
请告诉我：
1. 组会日期：____年__月__日
2. 汇报时长：___分钟（10/15/20分钟）
3. 汇报重点：（可选）
4. 是否包含下一步计划：是/否
5. 输出目录：（默认: [项目根]/PPT/[日期]/）
```

#### 工作目录隔离（防多终端冲突）

确认信息后，**立即**为本次 PPT 生成创建独立工作目录：

```
命名格式: [输出目录]/[章节号]_[主题关键词]_[语言]/
示例:     PPT/20260223/02_functional_profiling_CN/
```

**规则**：
- 每次 skill 调用创建**独立子目录**，所有临时文件和最终 .bas 均在其中
- 如果用户同时要求中英双版本，创建两个子目录（`..._CN/` 和 `..._EN/`）
- 目录已存在时询问用户：覆盖 / 新建带时间戳的目录 / 取消
- 最终 .bas 文件同时**复制一份到上级目录**（方便用户查找）

```
创建目录步骤:
1. 拼出工作目录路径（基于用户确认的输出目录 + 章节_主题_语言）
2. mkdir -p 创建目录
3. 后续所有 Agent 输出和临时文件均写入此目录
4. 拼装完成后，将最终 .bas 复制到上级目录
```

### Step 1.2: 查询 NotebookLM 规范（可选）

如果涉及特定领域（如噬菌体、脂质体），查询相关规范：

```bash
cd ~/.claude/skills/notebooklm && python scripts/run.py ask_question.py \
  --question "制作关于[主题]的科研汇报PPT，需要注意哪些展示规范和术语翻译？" \
  --notebook-id "科研ppt汇报规范"
```

### Step 1.3: 扫描项目内容

扫描目录获取可用内容：
- `Experiments/*.md` - 实验记录
- `Experiments/Data/` - 数据和图片
- `CLAUDE.md` - 项目概述

### Step 1.4: 生成框架大纲

**必须遵循 ABT 叙事结构**：

```markdown
## PPT框架规划

**总时长**: X 分钟
**叙事结构**: ABT (And-But-Therefore)

| 序号 | 章节 | 时长 | 叙事角色 | 核心要点 | 视觉元素 | 推荐图片来源 |
|------|------|------|----------|----------|----------|-------------|
| 1 | 封面 | 0.5min | - | 项目名称、日期 | Logo | - |
| 2 | 背景介绍 | 2min | **And** | 领域现状、已知知识 | 示意图 | `![caption](path)` from source doc |
| 3 | 问题提出 | 1min | **But** | 未解决的瓶颈 | 对比图 | `![caption](path)` from source doc |
| 4 | 研究方案 | 1.5min | **Therefore** | 我们的解决策略 | 流程图 | `![caption](path)` from source doc |
| 5-8 | 实验结果 | 6min | 证据 | 关键发现（AE标题） | 数据图 | `![caption](path)` from source doc |
| 9 | 结论 | 2min | 回应 | 回答开头的问题 | 要点 | - |
| 10 | 下一步 | 1min | 展望 | 后续计划 | 时间线 | - |
| 11 | 致谢 | 0.5min | - | - | Logo | - |

**AE 标题示例**（断言式，非标签式）：
- ✅ "噬菌体 R1 表现出最强裂解活性"
- ❌ "实验结果"

**图片映射**（从源文档自动提取）：
| 幻灯片 | 图片文件 | 来源 |
|--------|---------|------|
| Slide 5 | fig_beta_diversity.png | `![Beta diversity](data/.../fig_beta_diversity.png)` |
| Slide 6 | fig_alpha_shannon.png | `![Shannon index](data/.../fig_alpha_shannon.png)` |
| ... | ... | ... |

请确认框架，或告诉我需要调整的地方。
```

### Step 1.5: 图片路径提取（自动）

当源文档（如 markdown 解读文件、分析报告）中包含 `![caption](path)` 格式的图片引用时：

1. **扫描源文档**中所有 `![...](...)` 标记
2. **提取图片路径**，将相对路径转换为绝对路径（基于源文档所在目录）
3. **映射到幻灯片**：根据图片内容和上下文，将每张图分配到最相关的幻灯片
4. 在框架大纲的「推荐图片来源」列填入对应的 `![caption](path)`
5. 有图片的幻灯片标记为 `TwoColumnImage` 布局（左文字、右图片）

### ⚠️ 强制停顿

**必须等待用户确认后才能进入阶段二！**

---

## 阶段二：规范与美学审查

> **此阶段读取**：`references/scientific_norms.md` + `references/design_guide.md`

### Step 2.1: 科研规范检查

根据 `references/scientific_norms.md`，检查内容：

| 检查项 | 规则 | 示例 |
|--------|------|------|
| 细菌名 | 斜体 | *E. coli*, *S. aureus* |
| 基因名 | 斜体 | *lacZ*, *VEGFA* |
| 蛋白名 | 正体 | LacZ, VEGFA |
| 拉丁术语 | 斜体 | *in vitro*, *in vivo* |
| P 值 | *P* 斜体 | *P* < 0.05 |
| 单位 | 数字空格单位 | 10 μL, 37°C |

### Step 2.2: 美学规范检查

根据 `references/design_guide.md`，检查：

| 检查项 | 标准 |
|--------|------|
| 文字量 | 每页 ≤6 行，每行 ≤6 词 |
| 标题类型 | 断言式（AE模型） |
| 配色 | 莫兰迪色系 |
| 字号 | 标题 ≥28pt，正文 ≥20pt |

### Step 2.3: 输出审查报告

```markdown
## 规范审查报告

### ✅ 通过项
- ABT 叙事结构完整
- 标题采用断言式

### ⚠️ 需修正
| 问题 | 位置 | 修正建议 |
|------|------|----------|
| 细菌名未斜体 | 第3页 | *E. coli* → 添加斜体 |
| P值格式 | 第6页 | p<0.05 → *P* < 0.05 |

已自动修正，请确认后继续。
```

---

## 阶段三：图片资源检查

### Step 3.1: 从源文档提取图片引用

扫描所有输入源文档（markdown 解读文件、分析报告等），提取 `![caption](path)` 引用：

```python
# 伪代码：提取逻辑
import re
pattern = r'!\[([^\]]*)\]\(([^)]+)\)'
for match in re.finditer(pattern, source_doc_content):
    caption, rel_path = match.group(1), match.group(2)
    abs_path = resolve_to_absolute(rel_path, source_doc_dir)
    image_registry.append({"caption": caption, "path": abs_path, "slide": None})
```

同时扫描项目常见图片目录：
- `Experiments/Data/` - 实验数据和图片
- `data/` - 分析输出图片（qmd 生成的 PNG/PDF）
- `assets/` - 资源文件

### Step 3.2: 图片-幻灯片映射

根据 Step 1.4 框架大纲中的「推荐图片来源」列，将提取的图片分配到具体幻灯片：

```markdown
## 图片映射表

| 幻灯片 | 布局类型 | 图片文件 | 绝对路径 | 状态 |
|--------|---------|---------|---------|------|
| Slide 3 | TwoColumnImage | fig_beta_pcoa.png | C:\Users\...\data\01_analysis\fig_beta_pcoa.png | ✓ 存在 |
| Slide 5 | TwoColumnImage | fig_alpha_shannon.png | C:\Users\...\data\01_analysis\fig_alpha_shannon.png | ✓ 存在 |
| Slide 7 | TwoColumnImage | fig_network.png | C:\Users\...\data\04_network\fig_network.png | ✗ 缺失 |
```

**映射原则**：
- 每张有数据图的幻灯片使用 `CreateTwoColumnImageSlide` 布局（左文字、右图片）
- 没有图片的幻灯片仍使用 `CreateContentSlide` 或其他适当布局
- 一张幻灯片最多映射一张主图（多图拆分为多页或使用拼图）

### Step 3.3: 磁盘验证与路径转换

对映射表中的每张图片执行**磁盘存在性检查**：

```
1. 将相对路径转为绝对 Windows 路径（反斜杠）
2. 使用 Glob/ls 确认文件存在
3. 记录文件大小（过大的图片需提醒用户压缩）
4. 确认图片格式为 PPT 支持格式（PNG/JPG/BMP/EMF/WMF）
```

**路径转换规则**：
- 源文档中的 `![caption](data/01_analysis/fig.png)` → `C:\Users\xxx\project\data\01_analysis\fig.png`
- 所有 VBA 代码中的图片路径必须使用**绝对 Windows 路径**（反斜杠 `\`）
- 路径中不得包含 `/`（Unix 风格），VBA `Dir()` 函数不支持

### Step 3.4: 输出图片检查报告

```markdown
## 图片资源检查报告

### ✓ 已确认图片（将自动插入 PPT）
| 图片 | 用于幻灯片 | 绝对路径 | 布局 |
|------|-----------|---------|------|
| Beta diversity PCoA | Slide 5 | C:\Users\...\fig_beta_pcoa.png | TwoColumnImage |
| Shannon index | Slide 6 | C:\Users\...\fig_alpha_shannon.png | TwoColumnImage |

### ✗ 缺失图片（将生成占位符）
| 所需图片 | 用于幻灯片 | 预期路径 | 处理 |
|----------|-----------|---------|------|
| 网络图 | Slide 8 | C:\Users\...\fig_network.png | 奶油色占位框 + 文件名提示 |

### 🔎 可选操作
- **"搜索"**：调用 BioRender 搜索素材
- **"跳过"**：缺失图片生成占位符（奶油色矩形 + 文件名）
- **"手动添加"**：稍后自己替换占位符
```

### Step 3.5: BioRender 集成

如果用户选择"搜索"，调用 `biorender-auto` 技能。

---

## 阶段 3.6：Gemini 内容审核

### Step 3.6.1: 审核时机

在图片检查完成后、正式生成 VBA 前执行。

### Step 3.6.2: Gemini 审核命令

```bash
gemini -p "请审核以下组会 PPT 内容草稿，指出潜在问题：

**PPT 框架**：
[粘贴阶段一确定的框架大纲]

**关键数据/结论**：
[列出各页的核心数据和结论]

请审核：
1. **叙事逻辑**：ABT 结构是否连贯？
2. **数据解读**：数据到结论的推理是否合理？
3. **结论措辞**：是否过于绝对？
4. **遗漏检查**：是否遗漏重要对照、局限性？
5. **常见坑**：这类实验常见的解读错误？"
```

### Step 3.6.3: ⚠️ Gemini 意见是参考而非答案

**核心原则**：
- Gemini 不了解项目细节，Claude 和用户才是内部团队
- **禁止**直接采纳 Gemini 所有建议
- 每条建议需 Claude 结合项目实际独立判断

### Step 3.6.4: 审核结果输出

```markdown
## Gemini 内容审核结果

### Claude 分析与决策

| Gemini 建议 | Claude 判断 | 决策 | 理由 |
|-------------|------------|------|------|
| [建议1] | [分析] | 采纳/调整/不采纳 | [理由] |

确认后继续生成 VBA 代码。
```

---

## 阶段四：VBA 代码生成（核心差异）

> **此阶段读取**：`references/vba_templates.md` + `references/outline_spec.md`

### Step 4.1: 分片生成 VBA 代码（骨架 + 临时文件 + 拼装）

> **核心原则**：模板固定、内容分片、临时文件落盘、逐片可检查可修复。

#### 4.1.0 确定分片策略

根据幻灯片总数决定分片：

| 总页数 | 分片数 | 每片页数 | 说明 |
|--------|--------|----------|------|
| <=10 | 1 | 全部 | 小 PPT，主进程直接写 |
| 11-20 | 2 | ~10 | 中等 PPT |
| 21-30 | 3 | ~8-10 | 大 PPT（如本项目的 25 页） |
| >30 | 4 | ~8 | 超大 PPT，最多 4 片 |

**分片边界建议**：尽量在章节/主题切换处分片，避免拆散逻辑连续的幻灯片。

#### 4.1.1 主进程写骨架文件（~400 行，固定模板）

主进程（Claude 主线程）直接用 Write 工具写入骨架 `.bas` 文件，包含：

```
骨架文件结构：
├── Option Explicit
├── 常量区（IMG_BASE, FONT_MAIN, 字号, 尺寸）
├── 7 个工具函数（从 vba_templates.md 原样复制）
│   ├── AddFormattedTextbox
│   ├── AddSpeakerNotes
│   ├── AddColoredShape
│   ├── AddStandardFrame
│   ├── AddBulletLines
│   ├── ApplyHighlight
│   └── AddImage
├── 8 个幻灯片类型 Sub（从 vba_templates.md 原样复制）
│   ├── CreateTitleSlide
│   ├── CreateSectionSlide
│   ├── CreateContentSlide
│   ├── CreateTwoColumnSlide
│   ├── CreateTwoColumnImageSlide
│   ├── CreateTableSlide
│   ├── CreateConclusionSlide
│   └── CreateThankYouSlide
└── Sub GeneratePresentation()（仅头尾 + 占位符）
    ├── Dim prs, c(), h(), d(), f(), ns()
    ├── prs = Application.Presentations.Add + PageSetup
    ├── ' @@SLIDES_PLACEHOLDER@@          ← Agent 内容插入点
    └── MsgBox "Done!" ...
```

**骨架文件特点**：
- **永远不需要 Agent 生成**——主进程从 `vba_templates.md` 直接复制
- **FONT_MAIN** 根据语言版本设置：CN = `"Microsoft YaHei"`, EN = `"Arial"`
- **`@@SLIDES_PLACEHOLDER@@`** 是拼装时的替换锚点（单独一行注释）

#### 4.1.2 并行 Agent 生成幻灯片分片（临时文件落盘）

启动 2-4 个 Agent，**每个只负责生成一段 VBA 幻灯片调用代码**。

**Agent 输入（嵌入 prompt，禁止读外部文件）**：
- 该分片包含的幻灯片编号和内容（标题、要点、图片路径、讲稿）
- IMG_BASE 路径
- Split() 语法说明
- GBK 编码规则

**Agent 输出**：写入**工作目录**中的临时文件，命名格式：

```
工作目录/                                    ← Step 1.1 创建的隔离目录
├── _tmp_slides_01_09.vba.tmp     ← Agent A 输出
├── _tmp_slides_10_17.vba.tmp     ← Agent B 输出
└── _tmp_slides_18_25.vba.tmp     ← Agent C 输出
```

**临时文件内容**（纯幻灯片调用代码，无函数定义，无 Sub 头尾）：

```vba
    ' ===== Slide 1: Title =====
    CreateTitleSlide prs, "Title", "Subtitle", "Presenter", "Date", _
        "Speaker notes..."

    ' ===== Slide 2: Content =====
    c = Split("bullet1|bullet2|bullet3", "|")
    CreateContentSlide prs, "AE Title", c, 2, _
        "Speaker notes..."

    ' ===== Slide 3: TwoColumnImage =====
    c = Split("point1|point2|point3", "|")
    CreateTwoColumnImageSlide prs, "AE Title", c, _
        IMG_BASE & "filename.png", 3, "Caption", _
        "Speaker notes..."
    ...
```

**Agent Prompt 模板**：

```
你的任务: 为 PPT 生成 VBA 幻灯片调用代码。
输出文件: [临时文件路径]

不要读取任何外部文件！所有内容已提供。
不要写 Option Explicit、常量、函数定义、Sub 头尾。
只写幻灯片调用代码（从 Slide X 到 Slide Y）。

GBK 编码规则: [规则列表]
VBA 字符串引号规则: 字符串内严禁出现未转义的 ASCII 双引号 "。
  中文强调用法（如 "配方" "成分"）必须改为单引号 '配方' 或去掉引号。
  否则 VBA 会将 " 视为字符串结束符，导致编译错误。
IMG_BASE = "[路径]"

=== 幻灯片内容 ===
Slide X: [type] / [title] / [bullets] / [image] / [notes]
Slide X+1: ...
...
Slide Y: ...

=== 代码模式 ===
每张幻灯片格式:
    ' ===== Slide N: [描述] =====
    c = Split("...|...", "|")
    CreateXxxSlide prs, "title", c, N, "notes"
```

**每个 Agent 输出量：~100-150 行**（vs 之前的 ~800 行），崩溃风险大幅降低。

#### 4.1.3 检查临时文件（可选但推荐）

拼装前可逐片验证：

```
对每个 .tmp 文件:
1. Read 检查是否完整（首尾幻灯片编号对不对）
2. Grep 检查是否包含 GBK 不兼容字符
3. 如果有问题: 只重跑该 Agent 或手动 Edit 修复
```

**修复策略**：
| 问题 | 处理 |
|------|------|
| Agent 崩溃未输出 | 重跑该 Agent |
| 内容错误（标题/数据不对） | 用 Edit 修正临时文件 |
| GBK 不兼容字符 | 用 Edit 替换为安全字符 |
| 缺少某张幻灯片 | 手动补写到临时文件 |

#### 4.1.4 拼装最终 .bas 文件

主进程将临时文件内容拼装到骨架中：

```
1. Read 骨架文件
2. Read 各临时文件（按顺序）
3. 将 @@SLIDES_PLACEHOLDER@@ 替换为所有临时文件内容拼接
4. Edit 写入最终 .bas 文件
5. 删除临时文件（可选，保留便于调试）
```

**拼装后验证**：
- 检查 `CreateTitleSlide` 出现次数 = 1（封面）
- 检查总 `Slide` 注释数 = 预期幻灯片数
- 检查 `MsgBox` 存在（Sub 正常结束）

#### 4.1.5 完整流程图

```
阶段四执行流:

  [读取 vba_templates.md]
         |
  [创建工作目录: PPT/日期/章节_主题_语言/]
         |
  [主进程 Write: 工作目录/skeleton.bas]  (~400 行, 固定模板)
         |
  [并行启动 Agent A/B/C]
     |          |          |
  [Agent A]  [Agent B]  [Agent C]
  Slide 1-9  Slide 10-17  Slide 18-25
     |          |          |
  [Write]    [Write]    [Write]
  工作目录/    工作目录/    工作目录/
  _tmp_01_09  _tmp_10_17  _tmp_18_25
     |          |          |
  [检查/修复（可选）]
         |
  [主进程 Edit: 拼装到 工作目录/skeleton.bas]
         |
  [输出: 工作目录/02_xxx_CN.bas]
         |
  [检测代码页 + 条件编码转换（中文版）]
         |
  [验证]
         |
  [复制最终 .bas 到上级目录]
```

---

### 通用规则（适用于所有生成方式）

**图片插入规则**：
- VBA 中使用 `Slide.Shapes.AddPicture` 插入图片
- 图片路径必须为**绝对 Windows 路径**（反斜杠 `\`），例如 `"C:\Users\xxx\project\data\fig.png"`
- 使用 `IMG_BASE` 常量拼接文件名，减少路径重复
- 调用 `AddImage` 工具函数，内含 `Dir()` 检查：文件存在则插入，不存在则绘制占位符
- 占位符样式：奶油色矩形（`RGB(235, 228, 219)`）+ 居中显示文件名
- 图片默认位置：右侧区域（Left=490, Top=115, Width=432, Height=370）
- 图片使用 `LockAspectRatio = msoTrue` 保持比例

**⚠️ GBK 编码规则（关键）**：
- VBA 源码中**禁止直接使用 GBK 不兼容的 Unicode 字符**，包括：
  - `²`（上标2）→ 用 `ChrW(178)` 或写成 `R2` 或 `^2`
  - `→`（箭头）→ 用 `->` 或 `ChrW(8594)`
  - `×`（乘号）→ 用 `x` 或 `ChrW(215)`
  - `≈`（约等于）→ 用 `~` 或 `ChrW(8776)`
  - `""`（弯引号）→ 用直引号 `""`
  - `≥` `≤` → 用 `>=` `<=`
  - `✓` `✗` → 用 `*` `x` 或 `[Y]` `[N]`
- **中文字符是 GBK 兼容的**，可直接使用
- **emoji 字符禁止使用**

**⚠️ VBA 字符串内引号规则（关键）**：
- VBA 中 `"` 是字符串定界符。字符串内容中**严禁出现未转义的 ASCII 双引号**
- 常见错误：中文语境中的 `"配方"` `"成分"` 等强调用法，直接写在 Split() 或赋值字符串中会导致**编译错误：语法错误**
- VBA 解析器遇到字符串内的 `"` 会认为字符串结束，后面的中文字符暴露在字符串外 → 语法错误
- **修复方案**（按优先级）：
  1. 用单引号替代：`'配方'`（推荐，最简洁）
  2. 用 VBA 转义双引号：`""配方""`（VBA 语法正确，但可读性差）
  3. 用书名号或其他标点：`《配方》` 或直接去掉引号
- **Agent Prompt 必须包含此规则**：提醒 Agent 在生成中文内容时，检查所有字符串中是否存在未转义的 `"`

**⚠️ 变量声明规则**：
- `Option Explicit` 要求**每个 Sub/Function 内的所有变量必须 Dim 声明**
- 骨架模板中已包含所有工具函数和 Sub 的 Dim 声明
- GeneratePresentation() 的 Dim 在骨架头部声明：`prs`, `c()`, `h()`, `d()`, `f()`, `ns()`
- Agent 生成的分片代码**不需要额外 Dim**（复用骨架中的声明）

---

### Step 4.2: 保存与编码转换

#### 🔴 中文 .bas 编码处理（自动检测代码页）

> **背景**：VBA 编辑器使用系统 ANSI 代码页读取 .bas 文件。
> - **传统中文 Windows**（ACP=936）：VBA 期望 GBK 编码
> - **开启了 "Beta: 使用 Unicode UTF-8 提供全球语言支持"**（ACP=65001）：VBA 期望 UTF-8 编码
> - 英文版不受影响（ASCII 在所有编码中相同）

**强制执行流程（含中文的 .bas 文件必须走完全部3步）**：

```
Step A: 拼装完成（此时为 UTF-8）
Step B: 检测系统代码页 → 条件转换
Step C: 验证编码正确性
```

**Step B — 自动检测 + 条件转换命令**（直接复制执行）：
```bash
python -c "
import ctypes
path = r'<.bas文件绝对路径>'
acp = ctypes.windll.kernel32.GetACP()
print(f'System ANSI Code Page: {acp}')

if acp == 65001:
    print('UTF-8 codepage detected. Keeping UTF-8 encoding (no conversion needed).')
elif acp == 936:
    print('GBK codepage detected. Converting UTF-8 -> GBK...')
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    with open(path, 'w', encoding='gbk') as f:
        f.write(content)
    print('GBK conversion done.')
else:
    print(f'WARNING: Unexpected codepage {acp}. Keeping UTF-8. Check manually if VBA shows garbled text.')
"
```

**Step C — 验证命令**（自动适配编码）：
```bash
python -c "
import sys, ctypes; sys.stdout.reconfigure(encoding='utf-8')
path = r'<.bas文件绝对路径>'
acp = ctypes.windll.kernel32.GetACP()
enc = 'utf-8' if acp == 65001 else 'gbk'
with open(path, 'r', encoding=enc) as f:
    content = f.read()
cn = sum(1 for c in content if '\u4e00' <= c <= '\u9fff')
print(f'Encoding: {enc} (ACP={acp}), Lines: {len(content.splitlines())}, CN chars: {cn}')
assert cn > 0, 'ERROR: No Chinese characters found!'
print('Encoding verification PASSED')
"
```

**⚠️ 注意**：
- `iconv` 在 Windows bash 中不可靠，**必须用 Python** 做编码转换
- 纯英文 .bas 文件无需转换（ASCII 兼容）
- 如果转 GBK 时报 `UnicodeEncodeError`，说明代码中有 GBK 不兼容字符，回到临时文件修正
- `GetACP()` 返回值：936=GBK, 65001=UTF-8, 1252=西欧Latin

---

#### 命名规则（目录隔离 + 防覆盖）

**工作目录**（每次调用独立）：
```
格式：[输出目录]/[章节号]_[主题关键词]_[语言]/
示例：PPT/20260223/02_functional_profiling_CN/
```

**工作目录内的文件**：

| 文件类型 | 命名格式 | 示例 |
|----------|---------|------|
| 骨架文件 | `skeleton.bas` | `skeleton.bas` |
| 临时分片 | `_tmp_slides_[起始]_[结束].vba.tmp` | `_tmp_slides_01_09.vba.tmp` |
| 最终文件 | `[章节]_[主题]_[语言].bas` | `02_functional_profiling_CN.bas` |

**上级目录的副本**（方便用户查找）：

| 场景 | 文件名示例 |
|------|-----------|
| 中文版 | `PPT/20260223/02_functional_profiling_CN.bas` |
| 英文版 | `PPT/20260223/02_functional_profiling_EN.bas` |

**命名原则**：
- **每次调用在独立子目录中工作**，多终端不会互相覆盖
- 临时文件**不再需要语言后缀**（目录名已隔离语言）
- 拼装成功后可清理临时文件，也可保留便于调试
- 最终 .bas 复制到上级目录时带完整命名（章节_主题_语言）

### Step 4.3: 输出用户操作指南

```markdown
## VBA PPT 已生成！

### 执行步骤（约30秒）

1. 打开 PowerPoint（空白演示文稿）
2. 按 **Alt+F11** 打开 VBA 编辑器
3. 菜单「插入」→「模块」→ 粘贴以下代码（或导入 .bas 文件）
4. 按 **F5** 运行 `GeneratePresentation`
5. 关闭 VBA 编辑器 → 完成！

### 生成结果
- 共 X 页幻灯片
- 莫兰迪配色方案
- 所有元素均为**原生 PowerPoint 对象**，可自由编辑
- 讲稿已写入备注区域

### 后续编辑提示
- 修改文字：直接双击文本框编辑，布局自动适应
- 插入图片：右键图片占位区域 → 替换为实际图片
- 调整颜色：VBA 代码顶部的 Const 定义了所有颜色值
```

### Step 4.4: COM 自动化（可选增强）

如果用户希望一键执行（无需手动粘贴）：

```bash
python scripts/run.py --vba-file 04_network_analysis_discussion_CN.bas --output 04_network_analysis_CN.pptx
```

使用 `win32com.client` 自动化 PowerPoint → 注入 VBA → 执行 → 保存 → 删除宏。

---

## 参考文件

| 文件 | 路径 | 内容 |
|------|------|------|
| VBA 模板库 | `references/vba_templates.md` | 8 种幻灯片 VBA 代码模板 + 图片插入工具函数 |
| 设计指南 | `references/design_guide.md` | ABT、AE、莫兰迪配色 |
| 科研规范 | `references/scientific_norms.md` | 斜体、单位、格式 |
| 大纲规范 | `references/outline_spec.md` | JSON 格式说明 |

---

## 输出路径与命名

```
项目根目录/
├── PPT/
│   ├── 20260223/                                    ← 日期目录
│   │   ├── 02_functional_profiling_CN.bas           ← 最终副本（方便查找）
│   │   ├── 02_functional_profiling_EN.bas           ← 最终副本（方便查找）
│   │   │
│   │   ├── 02_functional_profiling_CN/              ← CN 工作目录（隔离）
│   │   │   ├── skeleton.bas                         ← 骨架模板
│   │   │   ├── _tmp_slides_01_09.vba.tmp            ← Agent A 分片
│   │   │   ├── _tmp_slides_10_17.vba.tmp            ← Agent B 分片
│   │   │   ├── _tmp_slides_18_25.vba.tmp            ← Agent C 分片
│   │   │   └── 02_functional_profiling_CN.bas       ← 拼装后的最终文件
│   │   │
│   │   ├── 02_functional_profiling_EN/              ← EN 工作目录（隔离）
│   │   │   ├── skeleton.bas
│   │   │   ├── _tmp_slides_01_09.vba.tmp
│   │   │   ├── _tmp_slides_10_17.vba.tmp
│   │   │   ├── _tmp_slides_18_25.vba.tmp
│   │   │   └── 02_functional_profiling_EN.bas
│   │   │
│   │   ├── 01_species_diversity_CN/                 ← 其他章节完全隔离
│   │   │   └── ...
│   │   └── ...
```

**目录隔离规则**：
- 每次 skill 调用创建**独立子目录**，命名 = `[章节]_[主题]_[语言]`
- 所有临时文件和骨架文件**只存在于工作子目录内**
- 拼装成功后，最终 .bas **复制一份到上级日期目录**
- 多终端并行调用互不干扰（各自在独立子目录中工作）
- 子目录已存在时需询问用户处理方式
