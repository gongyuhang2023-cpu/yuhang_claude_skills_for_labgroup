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

### `meeting_mind` — 会议录制全流程（线上 + **面对面**）

**用途**：一键录制会议，自动完成音频抓取 + PPT 翻页截图 + 本地 ASR 转录 + Claude 分批并行解读 → `interpretation.md` + `summary.md` 两份产物。

**触发词**：`录会议`、`录个会议`、`开始录制`、`start recording a meeting`

**本次更新相比库里的上一版，是一个代际的差距**：

| 能力 | 上一版 | 现在 |
|------|--------|------|
| 录音路径 | 只有 ProcTap 进程级抓取 | 三选一：进程级 / **`--system-audio` 系统混音**（修 Teams 整场录成静音）/ **`--mic-only` 单麦** |
| **面对面会议** | ❌ 只能录线上 | ✅ **`--mic-only` + `--diarize` + `--voiceprint`** —— 一支麦克风录两个人，自动分离说话人并按声纹认名字 |
| 自己的声音 | 不录 | `--mic` 录成第二路 `audio/mic.wav`（1:1 会议两路分开） |
| 长会议音频 | 内存缓冲 —— **长会议曾整段丢失** | **边录边落盘**（新 `wavsink` 模块），录多久都不丢 |
| 纯语音会议 | 必须有窗口可截 | `--no-slides` 音频-only |
| 录制中断 | 无恢复路径 | 新 `recovery` 模块 |
| 产物落点 | `~/meetings/` | `~/Report/Group_Meeting/`（`--output-root` 可改） |
| ProcTap | 上游 PyPI 版 | 随包带 **`vendor/proctap` 补丁版**（修 WASAPI loopback 静音），`install.py` 第 5b 步自动覆盖 |

新增模块：`wavsink.py`（边录边落盘）、`voiceprint.py`（声纹认人）、`diarize.py`（说话人分离）、`mic.py`（麦克风采集）、`recovery.py`（中断恢复）、`audioio.py`。

**工作流**：
1. 说"录会议" → 一次问清主题/软件/灵敏度/麦克风
2. 后台启动 `meetingmind record`，停止信号靠写 `STOP` 文件
3. 说"结束了" → 触发 STOP → 等录制进程退出 → 自动 `postprocess`（resample + Qwen3-ASR + 写 `ai_input.json`）
4. **自动进入 AI 解读**：过滤 revisit → 并行 sub-agent（每批 5 张图 + 完整 transcript）→ 合并 `interpretation.md` → `summary.md`

**安装**：
```bash
cp -r skills/meeting_mind ~/.claude/skills/
cd ~/.claude/skills/meeting_mind
python install.py
```
6 步全自动（Python/Windows/CUDA 检测 → 建自己的 venv → 装 `[transcribe]` extras → 覆盖为 `vendor/proctap` 补丁版）。首次跑会下 ~3.4 GB 的 Qwen3-ASR 模型到 `~/.cache/huggingface/`。

**依赖**（装进 skill 自带 venv，**不污染你其他项目**）：
- 核心：`proc-tap`、`windows-capture`、`pycaw`、`psutil`、`pygetwindow`、`pywin32`、`pillow`、`numpy`
- 转录：`torch+cu128`、`transformers`、`qwen-asr`、`librosa`、`soundfile`、`accelerate`

**系统要求**：Windows 10 build 19041+ / Windows 11 + Python 3.10+ + NVIDIA GPU（推荐 6 GB+ 显存；CPU 可跑但慢 5-10×）

**注意**：
- 会议数据默认存本地 `~/Report/Group_Meeting/<date>-<topic>/`，**不外发**。AI 解读会把截图 + transcript 发给 Claude API，受 [Anthropic Privacy Policy](https://www.anthropic.com/legal/privacy) 约束；会议特别敏感时只跑到 transcript 阶段。
- 声纹认人是**辅助不是判决**：两个人的会永远给出两个名字，拿不准时以你自己的记忆为准。
- 线上会议窗口不能最小化到任务栏（WGC 限制），可拖到角落或换虚拟桌面。

---

### `humanizer` — 学术论文去 AI 痕迹工具（**待优化**）

> ⚠️ **待优化**：本 skill 尚在打磨中，规则与阈值可能变动，产出请自行复核后再用。
> 遇到误判/漏判欢迎反馈，会一并收进下一版。

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

### `ppt-master` — 科研 PPT 的「科研规范层」

**用途**：**替代原来的 `meeting-ppt-vba` 与 `open-slide`。** 它是一层**薄壳**——不自己生成 PPT，美学/排版/出片全部交给第三方开源引擎 [hugohe3/ppt-master](https://github.com/hugohe3/ppt-master)，本 skill 只负责把**科研内容规范**注入进去，并在导出前逐条把关。

**触发词**：`/ppt-master`、`科研 PPT`、`用 ppt-master 做组会 PPT`

**为什么换掉前两个**：`meeting-ppt-vba`（VBA 宏）和 `open-slide`（React 幻灯片）各自维护一套自己的排版引擎 —— 排版是别人已经做得更好的事，我们真正的增量在**科研正确性**。分层之后，引擎升级不用我们跟。

**注入的科研规范**：
- 物种名/基因名斜体、单位与有效数字、统计标注（精确 P 值 + 效应量 + n）
- **禁红绿配色**（色盲不友好）
- ABT 叙事（And→But→Therefore）+ AE 断言式标题（标题即结论）
- 缩写首次全称、图注自足

**需要先装引擎**（~1.3 GB，含独立 .git，故意不放进本仓库）：
```bash
git clone https://github.com/hugohe3/ppt-master.git "C:\Users\<你>\Tools\ppt-master"
```
然后把本 skill `SKILL.md` 里的引擎路径改成你自己的路径 —— 详见 [`skills/ppt-master/README.md`](skills/ppt-master/README.md)。

**依赖**：ppt-master 引擎（自行 clone）+ 其自带 venv。

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
│   ├── viewer.html                ← 可视化查看器（列式浏览器 + 依赖关系图）
│   ├── test-data.json             ← 示例 descriptions.json
│   ├── scripts/
│   │   ├── snapshot_tool.py       ← 文件快照扫描与变更检测
│   │   ├── dep_scanner.py         ← 依赖图扫描（qproj 图导入 / R 代码 I/O 正则）
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
├── meeting_mind/                  ← 会议录制引擎（线上 + 面对面，self-contained）
│   ├── SKILL.md                   ← 完整工作流（Phase 1-4：参数确认 / 后台录制 / 转录 / AI 解读）
│   ├── README.md                  ← 安装 + 使用 + 隐私说明
│   ├── install.py                 ← 6 步一键安装（含第 5b 步：覆盖为 vendor/proctap 补丁版）
│   ├── pyproject.toml             ← Python 包元数据（[transcribe] extras）
│   ├── vocabulary.txt.example     ← ASR 热词示例
│   ├── vendor/proctap/            ← ★ ProcTap 补丁版（修 WASAPI loopback 静音），install 第 5b 步覆盖上游
│   └── src/meetingmind/
│       ├── cli.py                 ← record / transcribe / postprocess 等子命令
│       ├── audio.py               ← ProcTap 进程级录音
│       ├── audioio.py             ← ★ 音频 IO 抽象
│       ├── wavsink.py             ← ★ 边录边落盘（修长会议音频整段丢失）
│       ├── mic.py                 ← ★ 麦克风采集（--mic / --mic-only）
│       ├── diarize.py             ← ★ 说话人分离
│       ├── voiceprint.py          ← ★ 声纹认人（两个人的会永远给两个名字）
│       ├── recovery.py            ← ★ 录制中断恢复
│       ├── slides.py              ← WGC 截图 + 像素差去重
│       ├── session.py             ← 录音 + 截图编排
│       ├── transcribe.py          ← Qwen3-ASR wrapper（chunked + GPU/CPU 选择）
│       ├── postprocess.py         ← transcript + ai_input.json 组装
│       ├── process_finder.py      ← 进程探测（pycaw + psutil）
│       └── vocabulary.py          ← 热词文件解析
├── ppt-master/                    ← 科研规范层（薄壳，引擎另行 clone）
│   ├── SKILL.md                   ← 注入规范 + 导出前 checklist
│   ├── README.md                  ← 引擎安装（hugohe3/ppt-master）与路径改写说明
│   └── references/
│       ├── scientific-norms.md    ← 斜体/单位/统计/禁红绿
│       ├── bio-structure.md       ← 生物领域结构惯例
│       └── glossary.md            ← 术语表
├── spark-advisor/                 ← 共享 GPU 服务器（Slurm）作业顾问
│   ├── SKILL.md                   ← 判本地/服务器 → 定参数 → 提交；含交互式调试会话
│   ├── config.example.json        ← 连接信息模板（真 config.json 不入库）
│   ├── scripts/advise.py          ← 引擎（纯标准库，封装 SSH+slurm，只吐 JSON）
│   └── references/                ← 推荐规则 / 服务器事实 / 执行地判据 / aarch64 装包
├── humanizer/
│   ├── SKILL.md
│   ├── README.md
│   ├── LICENSE
│   ├── WARP.md
│   └── consultation-notes.md
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
