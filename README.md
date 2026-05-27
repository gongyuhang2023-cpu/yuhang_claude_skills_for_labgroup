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

### `meeting_mind` — MeetingMind 组会录制全流程（重写版，self-contained）

**用途**：一键录制组会，自动完成：进程级音频抓取 + PPT 翻页截图 + 本地 ASR 转录 + Claude 分批并行解读 → `interpretation.md` + `summary.md` 两份产物。

**触发词**：`录会议`、`录个会议`、`开始录制`、`start recording a meeting`

**相比上一版的关键变化**（本 skill 已**完全重写**，旧版的 PyAudioWPatch + virtual cable 方案被 ProcTap 替代，自动 LLM Wiki 导入被精简掉）：

| 功能 | group_meeting_recorder | meeting_mind (新版) |
|------|----------------------|--------------|
| 截图 | mss 屏幕截图（窗口遮挡截不到） | Windows Graphics Capture API（遮挡/离屏/跨虚拟桌面均可） |
| 录音 | 无 | **ProcTap 进程级 WASAPI Loopback** — 调系统音量/静音不影响录制 |
| 语音转文字 | 无 | Qwen3-ASR-1.7B 本地 GPU 推理（~0.14× 实时） |
| PPT 回翻去重 | 无 | 全历史像素差去重，revisit 元数据记录 |
| 会议软件 | 仅 Teams | Teams / Zoom / 腾讯会议 / Edge 浏览器直播 |
| AI 后处理 | 一次性读全部截图生成 summary | **5 张/批并行 sub-agent** → interpretation.md + summary.md（per-slide 视觉/转录/解读三段） |
| 包结构 | 散装 scripts/ | **bundle 化**：`SKILL.md + install.py + src/meetingmind/ + pyproject.toml`，`cp -r` 即装 |

**工作流**：
1. 说"录会议" → AskUserQuestion 一次问 4 项（主题/软件/灵敏度/麦克风）
2. 后台启动 `meetingmind record`（Bash run_in_background），停止信号靠写 `STOP` 文件
3. 说"结束了" → 触发 STOP → 等录制进程退出 → 自动 `postprocess`（resample + Qwen3-ASR + 写 ai_input.json）
4. **自动进入 AI 解读**：读 ai_input.json，过滤 revisit → spawn 并行 sub-agent（每批 5 张图 + 完整 transcript）→ 合并 interpretation.md → 1 个 summary sub-agent → summary.md

**安装**：
```bash
cp -r skills/meeting_mind ~/.claude/skills/
cd ~/.claude/skills/meeting_mind
python install.py
```
6 步全自动（Python/Windows/CUDA 检测 → 建自己的 venv → pip install `[transcribe]` extras → skill 已就位）。首次跑会下 ~3.4 GB 的 Qwen3-ASR 模型到 `~/.cache/huggingface/`（HF cache，下次共享）。

**依赖**（installer 自动装到 skill 自带 venv，**不污染你其他项目**）：
- 核心：`proc-tap`、`windows-capture`、`pycaw`、`psutil`、`pygetwindow`、`pywin32`、`pillow`、`numpy`
- 转录：`torch==2.11.0+cu128`、`transformers==4.57.6`、`qwen-asr==0.0.6`、`librosa`、`soundfile`、`accelerate`

**系统要求**：Windows 10 build 19041+ / Windows 11 + Python 3.10+ + NVIDIA GPU（推荐 6 GB+ 显存；CPU 推理可跑但慢 5-10×）

**注意**：
- 录到的会议数据**默认存在** `meetings/<date>-<topic>/` 下，**不外发**。AI 解读步骤会把截图 + transcript 作为 prompt 发给 Anthropic（Claude API），受 [Anthropic Privacy Policy](https://www.anthropic.com/legal/privacy) 约束。会议特别敏感时考虑只跑到 transcript 阶段。
- v1 不录用户麦克风（只录会议软件输出音频，即其他人讲话）。
- 会议窗口不能最小化到任务栏（WGC 限制），可以拖到角落或换桌面。
- **架构参考**：详见 skill 内 `SKILL.md`（Phase 1-4 完整指令）和 `README.md`（朋友圈安装与隐私说明）。

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

### `open-slide` — 自管理 HTML 幻灯片生成器

**用途**：对话中说"做个 slide"即可生成 React 幻灯片。基于 [open-slide](https://github.com/1weiho/open-slide) 开源框架（MIT），自管理 runtime，无需预装 Node 项目。

**触发词**：`做个幻灯片`、`制作 slide`、`/slide`、`把 paper 做成演示`、`准备组会汇报`

**核心特性**：
- 首次自动 `npm install`（约 30-60 秒），后续秒开
- 每次选择输出路径，不绑定固定目录
- 1920×1080 固定画布，React 18 + Vite 5 热重载
- 支持 CSS keyframe 动画，纯 React 无额外依赖
- 内置 authoring guide：字号/间距/垂直预算/反模式检查

**工作流**（6 个 Phase）：
1. 选择输出路径 → 2. 提取内容（从项目或口述）→ 3. 风格决策 → 4. 编写 TSX → 5. 实时预览 → 6. 迭代修改

**依赖**：[Node.js ≥ 18](https://nodejs.org/)（需预装；npm 包 `@open-slide/core`、`react`、`react-dom` 首次使用时自动安装到 skill 内部 `runtime/`，无需手动操作）

**架构参考**：详见 [`skills/open-slide/README.md`](skills/open-slide/README.md)

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
- `meeting_mind` 的录制产物（`interpretation.md` / `summary.md` / `transcript.md`）可手动复制到 LLM Wiki 的 `raw/sources/` 目录
- LLM Wiki 自动检测新文件 → 分析内容 → 生成 Wiki 页面 → 关联到已有知识网络
- 支持知识图谱可视化、社区检测、语义搜索
- 兼容 Obsidian（生成的 Wiki 可直接用 Obsidian 打开浏览）

> 注：新版 `meeting_mind` 不自动导入 LLM Wiki（精简了原集成逻辑）。需要的话手动 `cp meetings/<date>-<topic>/{interpretation,summary}.md <wiki>/raw/sources/` 即可，wiki 软件会自动识别。

**快速开始**：
1. 从 [Releases](https://github.com/nashsu/llm_wiki/releases) 下载安装（Windows .msi / macOS .dmg / Linux .AppImage）
2. 创建一个 Wiki 项目，LLM 后端可选 DeepSeek / OpenAI / Anthropic / Ollama
3. 录会议结束后手动把 `interpretation.md` / `summary.md` 复制到 wiki 的 `raw/sources/` 目录
4. Wiki 软件自动生成知识页面

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
├── meeting_mind/                  ← 重写版（self-contained bundle，推荐）
│   ├── SKILL.md                   ← 完整工作流（Phase 1-4：参数确认 / 后台录制 / 转录 / AI 解读）
│   ├── README.md                  ← 朋友圈安装 + 使用 + 隐私说明
│   ├── install.py                 ← 6 步一键安装：Python/Win/CUDA 检测 → 建 venv → pip → SKILL.md 就位
│   ├── pyproject.toml             ← Python 包元数据（[transcribe] extras pin 到 production 版本）
│   ├── vocabulary.txt.example     ← ASR 热词示例（# 分类、## 注释）
│   └── src/meetingmind/           ← 完整 Python 包（cli/audio/slides/session/transcribe/postprocess/...）
│       ├── __main__.py            ← `python -m meetingmind` 入口
│       ├── cli.py                 ← list-processes / record / transcribe / postprocess 子命令
│       ├── audio.py               ← ProcTap 进程级录音
│       ├── slides.py              ← WGC 截图 + 像素差去重
│       ├── session.py             ← 录音 + 截图编排
│       ├── transcribe.py          ← Qwen3-ASR wrapper（chunked + GPU/CPU 选择）
│       ├── postprocess.py         ← transcript + ai_input.json 组装
│       ├── process_finder.py      ← 进程探测（pycaw + psutil）
│       └── vocabulary.py          ← 热词文件解析
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
├── open-slide/
│   ├── SKILL.md                   ← 完整工作流（Phase 0-6）
│   ├── README.md                  ← 安装 / 使用 / 架构 / 上游说明
│   ├── scripts/
│   │   ├── bootstrap.py           ← 幂等 runtime 初始化（package.json → npm install → 动态配置）
│   │   └── server.py              ← dev server 生命周期（start / stop / status / open + IPv6）
│   └── references/
│       └── slide-authoring-guide.md ← TSX 编写规范（画布/字号/动画/反模式 checklist）
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
