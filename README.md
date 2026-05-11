# Yuhang's Claude Skills for Lab

个人整理的 Claude Code Skill 合集，适配科研实验室日常工作流。

> 非通用工具，按需取用。

## 安装方式

将 `skills/` 下的目录复制到 `~/.claude/skills/` 即可。

```bash
cp -r skills/<skill-name> ~/.claude/skills/
```

---

## Skills 列表

### `git-auto-sync` — 一键 Git 同步

**用途**：对话中说"提交代码"、"git 保存并推送"即可触发，自动完成 `add → commit → push`。

**额外功能**：
- 若项目根目录存在 `项目目录.md`，自动更新时间戳和变更日志
- 科研实验项目一致性检测（检测 protocol/实验计划等关键文件变更时给出提醒）

**依赖**：无（纯 Python 标准库 + git）

---

### `group_meeting_recorder` — 组会自动截图 + AI 总结（旧版，仍可用）

**用途**：Teams 线上组会时后台自动截图（检测 PPT 翻页），会后由 Claude 生成图文总结。

**触发词**：`截屏组会`、`自动截PPT`、`/capture`

**工作流**：
1. 说"开始截图"→ 脚本后台运行，自动检测 PPT 翻页并截图
2. 会议结束说"生成总结"→ Claude 读取截图，生成 `summary.md`

**依赖**：`mss`, `PyGetWindow`, `Pillow`, `numpy`（首次运行自动安装到 `.venv`）

**注意**：目前适配 Windows + Teams，截图保存到 `~/Desktop/meeting_captures/`

> **推荐使用下方的 `meeting_mind`**，它是此工具的全面升级版。本工具保留供已有用户继续使用。

---

### `meeting_mind` — MeetingMind 组会录制全流程（新版）

**用途**：一键录制组会，自动完成：录音（系统音频）+ 截图（PPT 翻页检测）+ 语音转文字 + AI 解读 + 自动导入知识库。是 `group_meeting_recorder` 的全面升级版。

**触发词**：`录制组会`、`开始录制`、`/meeting`

**相比旧版的升级**：
| 功能 | group_meeting_recorder | meeting_mind |
|------|----------------------|--------------|
| 截图 | mss 屏幕截图（窗口被遮挡则截不到） | Windows Graphics Capture API（遮挡/离屏均可） |
| 录音 | 无 | WASAPI loopback 系统音频录制 |
| 语音转文字 | 无 | Qwen3-ASR-1.7B 本地推理 |
| PPT 回翻去重 | 无 | 全历史去重，revisit 元数据记录 |
| 会议软件 | 仅 Teams | Teams / Zoom / 腾讯会议 |
| AI 后处理 | 一次性读全部截图生成 summary | 分批 sub-agent 并行读图 → interpretation.md + summary.md |
| 知识库集成 | 无 | 自动导入 [LLM Wiki](https://github.com/nashsu/llm_wiki) |

**工作流**：
1. 说"录制组会" → 确认会议软件 + 截图灵敏度
2. 后台自动录音 + 截图（支持遮挡窗口）
3. 会议结束说"停止" → 自动转录 + 分批解读截图 + 生成总结
4. 输出自动进入 LLM Wiki raw 目录，wiki 软件自动识别并生成知识页面

**依赖**：
- **核心**：`PyAudioWPatch`, `windows-capture`, `Pillow`, `numpy`, `PyGetWindow`, `pywin32`, `PyYAML`
- **ASR（可选）**：`qwen-asr`, `torch`(CUDA), `librosa`, `soundfile`
- 首次使用时 Claude 自动引导环境配置

**系统要求**：Windows 10/11 + Python 3.10+ + NVIDIA GPU（ASR 推荐，非必须）

**知识库集成**：强烈推荐配合 [LLM Wiki](https://github.com/nashsu/llm_wiki) 使用（见下方说明）

---

### `humanizer` — 学术论文去 AI 痕迹工具

**用途**：检测并移除学术论文中的 AI 生成写作模式，使文本读起来像经验丰富的人类研究者所写。

**触发词**：`humanizer`、`去AI痕迹`、`论文润色去AI`

**核心功能**：
- **三级词汇分类**：RED（必须替换）/ YELLOW（控制频率）/ GREEN（学术白名单）
- **IMRAD 分段感知**：针对 Abstract、Introduction、Methods、Results、Discussion 各有专项检查规则
- **结构模式检测**：句长均匀度（burstiness）、公式化连接词、三连词组、同义词循环等 10 类 AI 特征
- **反 AI 审计**：重写后自问"审稿人还会怀疑什么？"并修复残余问题

**依赖**：无（纯 Skill prompt，无外部脚本）

**配套参考**：[`docs/AI写作痕迹速查表.md`](docs/AI写作痕迹速查表.md) — 从 Skill 规则中提炼的速查表，可打印自查用（不在 skill 目录内，不会随安装带入）

---

### `meeting-ppt-vba` — VBA 宏方案组会 PPT 生成器

**用途**：生成完整 VBA 宏代码，在 PowerPoint 中一键执行即可生成专业科研组会 PPT。所有元素为原生 PowerPoint 对象，100% 可编辑。

**触发词**：`/ppt-vba`、`生成VBA PPT`、`可编辑PPT`

**核心特性**：
- **莫兰迪配色**：低饱和高级感，7 色体系 + 双色分层原则，色盲友好
- **ABT 叙事结构**：And（背景）→ But（问题）→ Therefore（方案），逻辑清晰
- **AE 断言式标题**：标题即结论，非标签式（如"噬菌体 R1 裂解活性最强"而非"实验结果"）
- **8 种幻灯片布局**：封面 / 章节 / 内容 / 双栏 / 图片 / 表格 / 结论 / 致谢
- **自动图片插入**：从源文档提取 `![](path)` 引用，自动映射到幻灯片并插入
- **GBK 编码自适应**：自动检测系统代码页，条件转换编码，中文不乱码

**工作流**（五阶段）：
1. 框架规划（含图片映射）→ 2. 规范审查 → 3. 图片验证 → 4. Gemini 审核 → 5. VBA 代码生成

**依赖**：无（纯 VBA，只需 PowerPoint）。可选 `win32com` 实现 COM 自动化一键出 .pptx。

---

### `file-profile` — 项目文件画像工具

**用途**：扫描当前项目文件夹，为每个文件和文件夹自动生成一句话描述（说明其在项目中的用途），存入结构化 JSON，并生成可双击打开的可视化查看器。

**触发词**：`扫描项目`、`文件简介`、`file profile`、`更新文件描述`

**核心功能**：
- **增量快照对比**：基于文件大小和修改时间检测新增/修改/删除，避免重复扫描
- **AI 智能描述**：Claude 读取文件内容和项目上下文，生成"做什么"而非"是什么"的描述
- **自动分类打标**：根据扩展名、文件夹名、文件名模式自动分配标签
- **依赖关系追踪**：记录文件间的 `produces` / `depends_on` 关系
- **可视化查看器**：macOS Finder 风格的列式浏览器（自包含 HTML），支持编辑描述、管理标签、键盘导航
- **Windows 快捷方式**：项目根目录生成 `文件画像.url`，双击即可打开查看器
- **批量归类**：对整个文件夹批量应用标签和描述模板

**工作流**：
1. 初始化 `.project-meta/` 目录和排除规则
2. 快照扫描 → 对比变更 → Claude 生成描述 → 写入 `descriptions.json`
3. 自动生成 `launch_viewer.html` + `文件画像.url` 快捷方式

**依赖**：Python 3.10+（标准库即可，无第三方依赖）

---

### `qproj-helper` — qproj 工作流指导助手

**用途**：指导用户使用 [qproj](https://github.com/rujinlong/qproj)（轻量级 Quarto 分析工作流脚手架）。Claude Code 无法直接操作 R Console，因此本 Skill 的核心是**分析用户所处阶段，给出可粘贴到 Console 的 R 命令**。

**触发词**：`qproj`、`创建qmd`、`新建分析步骤`、`初始化分析项目`、`path_target`、`path_source`

**核心功能**：
- **7 场景决策树**：从零建项目、添加分析步骤、数据放哪、读上游数据、已有文件夹初始化、依赖管理、概念解释
- **CLAUDE.md 模板生成**：初始化完成后，引导用户生成项目级 CLAUDE.md（含 qproj path binding 规则），通过逐板块问答完善内容
- **qproj 速查表**：四个路径绑定、核心函数、命名约定、数据流规则

**配套文件**：
- `templates/CLAUDE-qproj.md` — 项目级 CLAUDE.md 模板（98 行），初始化时复制到项目根目录
- `references/qproj-guide.md` — 完整使用指南（553 行），复杂问题时按需读取

**依赖**：需先安装 qproj R 包（`pak::pak("rujinlong/qproj")`）

---

## Tools 列表

> `tools/` 下为独立分发的桌面工具，非 Claude Skill，直接安装使用。

### `weekly-summary` — 每周工作总结生成器

**用途**：从 Windows Sticky Notes 便笺中自动读取本周工作记录，调用 DeepSeek V4 Pro 生成中英文双语结构化周报。

**核心特性**：
- 自动识别活跃便笺（14 天内有更新），按项目分组
- 忠实于便笺原文——用户写了小结就转述，没写就不加，AI 不添油加醋
- 每周追加（不覆盖历史），Markdown 格式
- "启动下一周"一键在便笺中写入下周日期头
- Deep Navy 深色主题 GUI，高 DPI 支持

**安装**：从 [Releases](https://github.com/gongyuhang2023-cpu/yuhang_claude_skills_for_labgroup/releases) 下载 `WeeklySummary_Setup.exe` 双击安装，或从源码运行（见 [`tools/weekly-summary/README.md`](tools/weekly-summary/README.md)）。

**依赖**：DeepSeek API Key（[申请](https://platform.deepseek.com/api_keys)）

---

## 推荐搭配：LLM Wiki — AI 驱动的知识库

[**LLM Wiki**](https://github.com/nashsu/llm_wiki)（开源，GPLv3）是一个跨平台桌面应用，能自动将文档转化为结构化、互相关联的知识库。

**为什么推荐**：
- `meeting_mind` 的录制产物（转录文本、截图解读、总结）可直接放入 LLM Wiki 的 `raw/sources/` 目录
- LLM Wiki 自动检测新文件 → 分析内容 → 生成 Wiki 页面 → 关联到已有知识网络
- 支持知识图谱可视化、社区检测、语义搜索
- 兼容 Obsidian（生成的 Wiki 可直接用 Obsidian 打开浏览）

**快速开始**：
1. 从 [Releases](https://github.com/nashsu/llm_wiki/releases) 下载安装（Windows .msi / macOS .dmg / Linux .AppImage）
2. 创建一个 Wiki 项目，LLM 后端可选 DeepSeek / OpenAI / Anthropic / Ollama
3. 在 `meeting_mind` 首次配置时，将输出目录设为 Wiki 的 `raw/sources/` 路径
4. 之后每次组会录制结束，产物自动进入 Wiki，软件自动生成知识页面

**适用场景**：组会笔记沉淀、文献阅读管理、课程知识整理、项目文档归档

---

## 目录结构

```
skills/
├── file-profile/
│   ├── SKILL.md                   ← 完整执行流程（6 步）
│   ├── viewer.html                ← 可视化查看器（列式浏览器）
│   ├── test-data.json             ← 示例 descriptions.json
│   ├── scripts/
│   │   ├── snapshot_tool.py       ← 文件快照扫描与变更检测
│   │   ├── meta_updater.py        ← descriptions.json 增量更新
│   │   └── generate_launcher.py   ← 生成自包含 HTML + .url 快捷方式
│   └── references/
│       ├── exclude_patterns.md    ← 默认排除规则模板
│       └── category_rules.md      ← 自动分类标签规则
├── git-auto-sync/
│   ├── SKILL.md
│   └── scripts/
│       ├── sync.py
│       └── update_directory.py
├── group_meeting_recorder/        ← 旧版（保留，仍可用）
│   ├── SKILL.md
│   ├── requirements.txt
│   └── scripts/
│       ├── run.py
│       ├── capture.py
│       └── setup_environment.py
├── meeting_mind/                  ← 新版（推荐）
│   ├── SKILL.md                   ← 完整工作流（Phase 0-3）
│   ├── SETUP_GUIDE.md             ← 首次使用环境配置指南
│   ├── config.yaml                ← 持久配置
│   ├── requirements.txt           ← 核心依赖
│   ├── requirements-asr.txt       ← ASR 可选依赖
│   ├── vocabulary.txt             ← 领域词汇表
│   └── scripts/
│       ├── run.py                 ← venv 管理 + 脚本启动器
│       ├── setup_environment.py   ← 环境初始化
│       ├── session.py             ← 录音 + 截图并行主入口
│       ├── audio_recorder.py      ← WASAPI loopback 录音
│       ├── slide_capture.py       ← WGC 截图 + 去重
│       └── transcribe.py          ← Qwen3-ASR 语音转文字
├── humanizer/
│   ├── SKILL.md
│   ├── README.md
│   ├── LICENSE
│   ├── WARP.md
│   └── consultation-notes.md
├── meeting-ppt-vba/
│   ├── SKILL.md
│   ├── requirements.txt
│   ├── assets/
│   │   └── logo.png
│   ├── references/
│   │   ├── design_guide.md        ← 莫兰迪配色 + 排版规范
│   │   ├── vba_templates.md       ← 8 种幻灯片 VBA 模板
│   │   ├── scientific_norms.md    ← 科研格式规范（斜体/单位/P值）
│   │   └── outline_spec.md        ← JSON 大纲格式说明
│   ├── scripts/
│   │   ├── run.py
│   │   └── setup_environment.py
│   └── snapshots/                 ← 历史版本快照
└── qproj-helper/
    ├── SKILL.md                   ← 决策树 + 引导问题（220 行）
    ├── templates/
    │   └── CLAUDE-qproj.md        ← 项目级 CLAUDE.md 模板（98 行）
    └── references/
        └── qproj-guide.md         ← 完整使用指南（553 行，按需读取）
tools/
└── weekly-summary/
    ├── README.md
    ├── requirements.txt
    ├── weekly_summary_gui.py      ← 主程序（customtkinter GUI）
    ├── ai_summarizer.py           ← DeepSeek V4 Pro 双语总结
    ├── config_manager.py          ← 配置管理（%APPDATA%）
    ├── sticky_notes_reader.py     ← Windows 便笺读取/写入
    ├── build.bat                  ← PyInstaller + NSIS 打包脚本
    ├── installer.nsi              ← NSIS 安装包脚本
    ├── icon.ico / icon.png        ← 应用图标
    └── logo.png                   ← Deng Lab logo
docs/
└── AI写作痕迹速查表.md          ← humanizer 配套参考（不随 skill 安装）
```
