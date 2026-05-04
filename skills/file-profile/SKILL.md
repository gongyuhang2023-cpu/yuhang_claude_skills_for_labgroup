---
name: file-profile
description: |
  项目文件画像工具。当用户提到"扫描项目"、"文件简介"、"file profile"、"更新文件描述"、"项目文件画像"时触发。
  扫描项目文件夹，通过快照对比检测变更，利用 Claude Code 对项目的理解自动生成文件/文件夹描述，
  存入 .project-meta/descriptions.json。支持批量归类、排除规则、增量更新。
---

# File Profile - 项目文件画像

## 作用

扫描当前项目文件夹，为每个文件和文件夹生成一句话描述（说明其在项目中的用途），存入结构化 JSON，供查看器展示。

## 触发方式

- 对话中说"扫描项目"、"文件简介"、"file profile"、"更新文件描述"
- 直接调用 `/file-profile`

## 执行流程

### Step 1：初始化检查

1. 确认当前工作目录是一个项目根目录（有意义的项目，非系统目录）
2. 检查 `.project-meta/` 目录是否存在，若无则创建：
   ```bash
   mkdir -p .project-meta
   ```
3. 检查是否存在 `.project-meta/ignore` 排除规则文件，若无则从 references 生成默认版本

### Step 2：扫描与变更检测（脚本执行）

运行快照工具进行扫描和对比：

```bash
python ~/.claude/skills/file-profile/scripts/snapshot_tool.py --action scan --root .
```

脚本输出 JSON 格式的变更报告：
```json
{
  "added": ["path/to/new_file.R", ...],
  "modified": ["path/to/changed_file.py", ...],
  "deleted": ["path/to/removed_file.txt", ...],
  "unchanged_count": 42
}
```

如果是首次扫描（无 snapshot.json），所有文件视为"added"。

### Step 3：描述生成（Claude 核心能力）

针对变更报告中的 added 和 modified 文件：

1. **批量读取**：对每个需要描述的文件，读取其内容（代码文件读前 50-100 行即可，大文件只读头部）
2. **结合项目上下文**：参考 CLAUDE.md、README.md、项目目录.md 等已有文档理解项目整体
3. **生成描述**：为每个文件写一句话描述（中文，20-50 字），说明其在项目中的职责
4. **文件夹描述**：为包含变更文件的文件夹也生成描述
5. **自动分类**：参考 `references/category_rules.md` 为文件分配标签

**描述质量要求：**
- 说"做什么"而非"是什么"（好："清洗原始测序数据并过滤低质量序列"；差："R 脚本文件"）
- 如果能判断文件间的依赖关系（谁产出谁、谁调用谁），记录到 `produces` / `depends_on` 字段
- 对无法判断内容的二进制文件，基于文件名和扩展名推断用途

### Step 4：写入元数据（脚本执行）

将生成的描述通过脚本写入：

```bash
python ~/.claude/skills/file-profile/scripts/meta_updater.py --action update --root . --file .project-meta/update_data.json
```

**推荐流程**：先用 Write 工具将描述 JSON 写入 `.project-meta/update_data.json`，再通过 `--file` 传入。这避免了命令行参数长度限制（大项目 JSON 可能超过 shell 参数上限）。也仍支持 `--data '<json_string>'` 直接传入（适合小量更新）。

JSON 格式：
```json
{
  "files": {
    "scripts/01_clean.R": {
      "description": "清洗原始测序数据，过滤低质量序列和低丰度 OTU",
      "tags": ["脚本", "质控"],
      "produces": ["data/processed/cleaned_phyloseq.rds"],
      "depends_on": ["data/raw/16S_seq.fastq.gz"]
    }
  },
  "folders": {
    "data/raw": {
      "description": "测序原始数据，不可修改",
      "tags": ["原始数据"]
    }
  },
  "deleted": ["path/to/removed_file.txt"]
}
```

脚本负责：
- 增量合并到现有 descriptions.json（不覆盖未变更文件的描述）
- 移除 deleted 文件的描述
- 更新快照文件 snapshot.json
- 更新 `lastScan` 时间戳

### Step 5：生成快捷查看器

每次描述更新完成后，运行生成脚本创建/更新项目快捷方式：

```bash
python ~/.claude/skills/file-profile/scripts/generate_launcher.py --root .
```

脚本产出两个文件：
- `.project-meta/launch_viewer.html` — 自包含查看器（viewer.html + 内嵌数据，双击可直接打开）
- `文件画像.url` — 项目根目录的 Windows 快捷方式（带星形文件夹图标），指向上述 HTML

首次扫描时自动创建，后续每次描述更新都会重新生成以同步最新数据。

### Step 6：汇报结果

向用户展示简洁的汇总：

```
项目文件画像更新完成：
- 扫描文件：52 个
- 新增描述：3 个（01_clean.R, metadata.xlsx, config.yaml）
- 更新描述：1 个（02_analysis.R）
- 删除记录：1 个（old_script.R）
- 跳过（排除）：8 个（.git/, node_modules/ 等）
- 快捷方式：文件画像.url ✓
```

## 批量归类模式

当用户说"批量归类"或"给 XX 文件夹分类"时：

1. 读取目标文件夹内所有文件
2. 询问用户归类规则（如"这个文件夹都是计算结果"）
3. 对该文件夹及其内部所有文件批量应用标签和描述模板
4. 写入 descriptions.json

示例：
```
用户："data/output 文件夹都是 R 脚本跑出来的结果"
→ 文件夹描述："R 脚本计算输出的结果文件"
→ 内部文件标签：["计算结果", "自动生成"]
→ 内部文件描述：基于文件名推断（如 "fig_01.png" → "分析脚本生成的图表"）
```

## 排除规则

读取 `.project-meta/ignore` 文件（语法同 .gitignore）：
```
.git/
.Rproj.user/
node_modules/
__pycache__/
*.rds
*.RData
.DS_Store
Thumbs.db
.project-meta/
文件画像.url
```

排除的文件不扫描、不生成描述、不计入快照。

## descriptions.json 完整格式

```json
{
  "projectName": "项目名称",
  "projectDescription": "项目整体描述",
  "lastScan": "2026-04-30T15:00:00",
  "version": "1.0",
  "files": {
    "相对路径/文件名": {
      "description": "一句话描述",
      "tags": ["标签1", "标签2"],
      "size": 1048576,
      "modified": "2026-04-20T10:30:00",
      "produces": ["输出文件路径"],
      "depends_on": ["依赖文件路径"]
    }
  },
  "folders": {
    "相对路径/文件夹名": {
      "description": "一句话描述",
      "tags": ["标签"]
    }
  }
}
```

## 注意事项

- 大项目（>200 文件）时，分批处理，每批 30-50 个文件
- 二进制文件（.rds, .pdf, 图片等）不读内容，基于文件名和上下文推断描述
- 首次扫描可能耗时较长，后续增量更新很快
- 不修改任何项目原始文件，所有元数据写入 `.project-meta/`
