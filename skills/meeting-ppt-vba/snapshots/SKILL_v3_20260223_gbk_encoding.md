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

### Step 1.1: 收集信息

```
请告诉我：
1. 组会日期：____年__月__日
2. 汇报时长：___分钟（10/15/20分钟）
3. 汇报重点：（可选）
4. 是否包含下一步计划：是/否
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

### Step 4.1: 生成 VBA 代码

Claude 根据以下输入生成完整 VBA 模块：
- **JSON outline**（阶段一确定的大纲）
- **`references/vba_templates.md`**（VBA 代码模板库）
- **审查修正后的内容**

**生成规则**：
1. 读取 `vba_templates.md` 中的模板结构
2. 根据 JSON outline 的每个 slide，选择对应的 Sub 模板
3. **有图片的幻灯片**使用 `CreateTwoColumnImageSlide` 模板（左文字、右图片）
4. 填充实际内容（标题、要点、表格数据、讲稿）
5. 应用高亮标记（`**关键词**` → VBA 加粗+变色）
6. 应用斜体标记（细菌名/基因名 → VBA Font.Italic）
7. 在常量区生成 `IMG_BASE` 常量（图片目录的公共前缀）
8. 对每张有图片的幻灯片，调用 `AddImage` 工具函数插入图片
9. 生成完整的 `Sub GeneratePresentation()` 主入口

**图片插入规则**：
- VBA 中使用 `Slide.Shapes.AddPicture` 插入图片
- 图片路径必须为**绝对 Windows 路径**（反斜杠 `\`），例如 `"C:\Users\xxx\project\data\fig.png"`
- 使用 `IMG_BASE` 常量拼接文件名，减少路径重复
- 调用 `AddImage` 工具函数，内含 `Dir()` 检查：文件存在则插入，不存在则绘制占位符
- 占位符样式：奶油色矩形（`RGB(235, 228, 219)`）+ 居中显示文件名
- 图片默认位置：右侧区域（Left=490, Top=115, Width=432, Height=370）
- 图片使用 `LockAspectRatio = msoTrue` 保持比例

**⚠️ 特殊字符规则**：
- VBA 源码中**禁止直接使用 GBK 不兼容的 Unicode 字符**，包括：
  - `²`（上标2）→ 用 `ChrW(178)` 或写成 `R2`
  - `→`（箭头）→ 用 `->` 或 `ChrW(8594)`
  - `×`（乘号）→ 用 `x` 或 `ChrW(215)`
  - `≈`（约等于）→ 用 `~` 或 `ChrW(8776)`
  - `""`（弯引号）→ 用直引号 `""`

**⚠️ 变量声明规则（关键）**：
- 代码顶部有 `Option Explicit`，要求**每个 Sub/Function 内的所有变量必须 Dim 声明**
- 常见遗漏：循环变量 `idx`、`i`、`rw`、`c` 在每个 Sub 里都要单独 `Dim`
- 生成后用脚本验证：检查每个 Sub 中 `xxx =` 是否有对应 `Dim xxx`

### Step 4.2: 保存 VBA 文件

#### 🔴 GBK 编码（最关键，必须执行！）

> **根本问题**：Claude Code 的 Write 工具**默认保存 UTF-8 编码**，但中文 Windows 的 VBA 编辑器**只能读取 GBK (CP936) 编码**。
> UTF-8 中文字符（3字节）被 VBA 按 GBK（2字节）解读 → 出现乱码 → **编译语法错误**。
> 英文版不受影响（ASCII 在两种编码中相同）。

**强制执行流程（含中文的 .bas 文件必须走完全部3步）**：

```
Step A: Write 工具写入文件（此时为 UTF-8）
Step B: 立即用 Python 转 GBK 编码（不可跳过！）
Step C: 验证 GBK 编码正确性
```

**Step B — Python GBK 转换命令**（直接复制执行）：
```python
python -c "
path = r'<.bas文件绝对路径>'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()
with open(path, 'w', encoding='gbk') as f:
    f.write(content)
print('GBK conversion done')
"
```

**Step C — 验证命令**：
```python
python -c "
import sys; sys.stdout.reconfigure(encoding='utf-8')
path = r'<.bas文件绝对路径>'
with open(path, 'r', encoding='gbk') as f:
    content = f.read()
cn = sum(1 for c in content if '\u4e00' <= c <= '\u9fff')
print(f'Lines: {len(content.splitlines())}, CN chars: {cn}')
assert cn > 0, 'ERROR: No Chinese characters found!'
print('GBK verification PASSED')
"
```

**⚠️ 注意**：
- `iconv` 在 Windows bash 中不可靠，**必须用 Python** 做编码转换
- 纯英文 .bas 文件无需转换（ASCII 兼容）
- 如果 Step B 报 `UnicodeEncodeError`，说明代码中有 GBK 不兼容字符，回到 Step 4.1 修正

---

#### 命名规则（防覆盖，必须遵守）

```
格式：[章节号]_[主题关键词]_[类型]_[语言].bas
```

| 场景 | 文件名示例 |
|------|-----------|
| 单语言 | `04_network_analysis_discussion.bas` |
| 中文版 | `04_network_analysis_discussion_CN.bas` |
| 英文版 | `04_network_analysis_discussion_EN.bas` |
| 无章节号 | `caga_microbiome_meeting_CN.bas` |
| 通用组会 | `weekly_progress_20260223.bas` |

**命名原则**：
- **禁止**使用 `presentation.bas` 等泛用名（会被其他 PPT 任务覆盖）
- 文件名必须包含**内容标识**（章节号/主题）+ **语言标识**（CN/EN，多语言时必须）
- VBA 模块内部 `Attribute VB_Name` 也应对应命名（如 `"Mod04Network_CN"`）
- 同一日期目录下可能有多个 PPT 任务的输出，命名必须能区分来源

```
保存为：[项目目录]/PPT/[日期]/[章节]_[主题]_[类型]_[语言].bas
```

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
│   ├── 20260223/
│   │   ├── 04_network_analysis_discussion_CN.bas   ← 中文版 VBA
│   │   ├── 04_network_analysis_discussion_EN.bas   ← 英文版 VBA
│   │   ├── 01_species_diversity_meeting_CN.bas     ← 其他章节不冲突
│   │   └── ...
```

**命名规则**: `[章节]_[主题]_[类型]_[语言].bas`
- 禁止泛用名（`presentation.bas`、`output.bas`）
- 多版本必须带语言后缀（`_CN`、`_EN`）
- 同目录下多个 PPT 任务互不覆盖
