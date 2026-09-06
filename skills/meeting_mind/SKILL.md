---
name: meeting_mind
description: |
  自动录制线上会议（Teams/Zoom/腾讯会议/Edge 直播等）的音频和 PPT 截图，
  会后本地转录文字并 AI 逐页解读 + 整体总结。当用户说"录会议"、"录个会议"、
  "开始录制"、"start recording a meeting"等触发。系统静音/调音量不影响录制。

  录制 → Qwen3-ASR 转录 → 5 张/批并行 sub-agent 视觉解读 →
  interpretation.md + summary.md。完全本地，不外发会议内容。
---

# MeetingMind — 自动录会议 + 转录

当用户表达录会议意图（"录会议"、"开始录制"、"record meeting"），
按以下三段流程执行。**不要省略任何一步、不要替用户自决参数**。

## 关键约定：使用 skill 自带的 Python

本 skill 是 **self-contained** — 它自己有 venv，里面装好了 `meetingmind`
包和所有依赖。所有 CLI 调用**必须**走 skill 自己的 venv Python：

- **Windows**（默认）: `~/.claude/skills/meeting_mind/.venv/Scripts/python.exe`
- **macOS / Linux**: `~/.claude/skills/meeting_mind/.venv/bin/python`

下面所有 Bash 命令的 `<PY>` 就是这个绝对路径——**不要**用 `python` /
`python3` / `py` 这些可能命中错误环境的快捷命令，那会找不到 `meetingmind`
模块。Bash tool 的 `~` 会被 shell 自动展开。

如果这个路径不存在（venv 还没建），告诉用户：
> "MeetingMind skill 的 venv 不存在。请到 `~/.claude/skills/meeting_mind/`
> 目录跑 `python install.py` 完成安装（5-15 分钟，看网速）。装好后重新
> 触发我。"

---

## 前置检查（每次触发先做）

1. 确认 skill venv 存在 + `meetingmind` CLI 可用：
   ```
   Bash: ~/.claude/skills/meeting_mind/.venv/Scripts/python.exe -m meetingmind --version
   ```
   - 成功 → 继续
   - `No such file or directory` → venv 没装。告诉用户跑 install.py（见上）
   - `ModuleNotFoundError` → venv 在但包没装好。让用户重跑 install.py

2. 确认有可录的会议软件在跑：
   ```
   Bash: ~/.claude/skills/meeting_mind/.venv/Scripts/python.exe -m meetingmind list-processes
   ```
   读输出，记下哪些进程有 `ON` 标记（活跃音频会话）。如果**没有任何
   活跃会话**，告诉用户："当前没检测到活跃会议软件。请先打开 Teams/
   Zoom/腾讯会议/Edge 等并进入会议，然后告诉我继续。" 然后等用户回应。

---

## Phase 1: 参数收集

向用户问 4 个参数。**会议主题（topic）**是自由文本，先在对话里问；
**其他 3 个**用 AskUserQuestion 一次问完。

### Step 1.1: 问主题（对话方式）

直接对用户说：
> "好，开始准备录制。这场会议的主题是什么？给一个简短的名字即可
> （例如：lab-meeting、weekly-sync、ml-paper-reading）。会作为会议
> 目录名的一部分。"

等用户回复，把答复存为 `<topic>`。
- 若用户跳过 / 说"不重要"：默认 `<topic>` = `"meeting"`。
- 把空格、特殊字符等问题交给 CLI（`session.py:_slugify` 已经处理）。

### Step 1.2: 问其他 3 项（AskUserQuestion）

用 AskUserQuestion 工具，一次发 3 个 question：

```
Q1: "用哪个会议软件？"
  options:
    - "Teams"           → keyword: teams
    - "Zoom"            → keyword: zoom
    - "腾讯会议"          → keyword: tencent
    - "Edge / 浏览器"      → keyword: edge
  （"Other" 由 UI 自动补，用户自填关键字时用对应 substring）

Q2: "截图灵敏度？（多久检测一次 PPT 翻页）"
  options:
    - "标准（5 秒，5% 变化）" → interval=5, threshold=5
    - "高（2 秒，2% 变化，多截图）" → interval=2, threshold=2
    - "低（10 秒，10% 变化，少截图）" → interval=10, threshold=10

Q3: "录麦克风（你自己的发言）？"
  options:
    - "不录，只录会议音频"
    - "也录我的麦"
```

把 Q1/Q2/Q3 答案分别存为 `<software>` / `<interval>` & `<threshold>` /
`<mic>`。

⚠️ **关于 mic**：v1 实现的 ProcTap 只捕获目标进程音频，**不录麦**。
如果用户选"也录我的麦"，告诉用户：
> "当前版本（v1）只捕获会议软件的输出音频（包括其他参会者的声音和你
> 听到的内容），不录你自己的麦克风。后续版本会加。继续吗？"
等用户确认 → 继续；用户拒绝 → 取消流程。

---

## Phase 2: 启动后台录制

用 Bash 工具的 `run_in_background=true` 模式启动 `record`：

```
Bash:
  command: ~/.claude/skills/meeting_mind/.venv/Scripts/python.exe -m meetingmind record \
             --process <software> \
             --topic "<topic>" \
             --interval <interval> \
             --threshold <threshold> \
             --output-root ~/Report/Group_Meeting \
             <若 software=teams 则加 --system-audio> \
             --quiet
  description: "Recording <topic> via meetingmind in background"
  run_in_background: true
```

⚠️ **Teams 必须加 `--system-audio`**。新版 Teams（WebView2/Chromium 内核）的会议音频由
Chromium audio-service 子进程渲染，逃出目标进程树，进程级 loopback **全程录成静音**（RMS=0，
历史反复复现）。`--system-audio` 改录**默认输出端点的系统混音**（扬声器在放什么就录什么），
绕开进程归属——声音在响就一定录得到。代价：会混入系统其它声音（通知等），录会前建议静音通知。
- `<software>` = **teams** → **加** `--system-audio`
- Zoom / 腾讯会议 / Edge 等 → **不加**（进程级 loopback 正常，音质更干净）；若录完出现音频
  静音警告（`recording.wav` 静音），重录时再加 `--system-audio` 兜底。

⚠️ **必须加 `--output-root ~/Report/Group_Meeting`**(大组会归档处)。Claude Code 的工作目录可能是
`system32` 等无写入权限的位置，默认 `./meetings` 会 Access Denied。

Bash 会立刻返回一个 task ID 和 output file 路径。**记下这两个**。

### 读取 meeting_dir 和 STOP 文件路径

`meetingmind record` 启动后会在 stdout 前两行打印 `meeting_dir` 和
`STOP file` 路径。等 3 秒后用 Read tool 读 task output 文件。

在输出中找到包含绝对路径的前两行（形如 `C:\Users\...` 或 `/home/...`），
分别存为 `<meeting_dir>` 和 `<stop_file>`。

**Fallback**：如果读不到路径行（输出为空或只有错误信息），尝试用命名
规则推断：`~/Report/Group_Meeting/YYYY-MM-DD-<topic>` 目录是否存在。用 PowerShell
`Get-ChildItem ~/Report/Group_Meeting -Directory` 确认。STOP 文件路径 =
`<meeting_dir>/STOP`。

**如果 output 文件包含错误信息**（如 `Failed to start`、`No process
matches`），把整个 output 内容贴给用户，告诉他们出了什么错，
**取消流程**。常见原因：
- 没有匹配的进程 → 让用户检查会议软件是否在跑
- 没有活跃音频会话 → 让用户先加入会议

### 告诉用户已经开始录制

成功读到 `<meeting_dir>` 后，对用户说：
> "✓ 录制已开始。
> 
> - 会议目录：`<meeting_dir>`
> - 主题：`<topic>` | 软件：`<software>`
> 
> 你现在可以正常开会。会议结束后跟我说"结束了"或"停止录制"，
> 我会停止录制并自动转录。
> 
> ⚠️ 系统静音/调音量不影响录制 — 安心听会。"

然后**正常进入对话状态**，等用户开口。

---

## Phase 3: 监听停止信号 → 转录

### Step 3.1: 识别停止信号

当用户说以下任一短语，进入停止流程：
- "结束了"、"会议结束"、"停止录制"、"录完了"
- "stop recording"、"end the meeting"

⚠️ **不要被一般的"结束"误触发**（例如用户说"这次讨论结束了"指对话
内某个话题）。只在用户明显指代**录制会议本身**时触发。不确定时
反问："你是说要停止录制吗？"

### Step 3.2: 写 STOP 文件

```
Bash: echo "" > "<stop_file>"
  description: "Signal stop to background record process"
```

STOP 文件出现后，后台的 `record` 进程会在下一次 wait 轮询（默认 0.5s）
读到，调 `sess.stop()` 写 metadata，退出。

### Step 3.3: 等后台进程退出

`run_in_background` 启动的 task 会自动通知 Claude 完成状态。等到通知
后再继续。如果 30 秒还没退出，告诉用户："录制进程没在 30 秒内停止，
可能卡在 audio/slides teardown。我等等再试或者你按 Ctrl+C。"

### Step 3.4: 跑 postprocess（前台同步）

```
Bash:
  command: ~/.claude/skills/meeting_mind/.venv/Scripts/python.exe -m meetingmind postprocess "<meeting_dir>" --device cuda --chunk-minutes 3
  description: "Transcribe and build ai_input.json"
  (不开 run_in_background — 这步需要拿到结果)
  timeout: 600000  # 10 minutes
```

⚠️ **显存安全（2026-06 修复后）**：转录现默认 fp16 加载（权重 6.8GB→3.4GB）+
`max_new_tokens=1024` 上限 + 每 chunk 后 `empty_cache()` + 显存 fraction 0.85 护栏 +
增量落盘（`transcript/_partial.md`，崩了不丢已转部分）。**默认 chunk 已是 3 分钟**，
12GB 卡峰值约 5GB、恒定不爬升。`--chunk-minutes 3` 可省（已是默认）；显存更小的卡可降到 2。
⚠️ **不要调回 5 分钟 + fp32**：旧版那样长会议会显存逐块累积到第 N 块触顶 → 经 Windows
WDDM 共享显存溢出到系统内存 → **整机硬重启**（2026-06-15 实测踩过，详见 memory）。

⚠️ **耗时预期**（参考 ADR-003，RTX 5070）：
- 15 分钟会议：~2 分钟
- 1 小时会议：~9 分钟
- 长会议要提前告诉用户："会议长 X 分钟，转录预计 Y 分钟，请稍等。"

stdout 第一行是 `ai_input.json` 路径。读出来存为 `<ai_input>`。

### Step 3.5: 简短确认 + 自动进入 Phase 4

对用户说：
> "✓ 转录完成（`<meeting_dir>/transcript/transcript.md`）。
> 现在开始 AI 解读和总结，预计还要 1-3 分钟。"

**不要等用户回应**，立刻进入 Phase 4。

---

## Phase 4: AI 解读 + 总结（紧接 Phase 3 完成后自动跑）

Phase 3 拿到 `<ai_input>` 路径（即 `<meeting_dir>/ai_input.json`）后**立即继续**，
不等用户主动触发。目标：在 `<meeting_dir>` 根目录写两个 markdown：

- `interpretation.md` — 逐页解读（视觉内容 / 对应转录 / 内容解读）
- `summary.md` — 整体总结（概要 / 要点 / 方法结果 / 待跟进）

### Step 4.1: 读 ai_input.json + 过滤 revisit

Read tool 读 `<ai_input>`。把 JSON 解析后：

- `slides` 数组里 `type == "slide"` 的项**留下**，记为 `<valid_slides>`
- `type == "revisit"` 的项**过滤掉**（回顾型截图，内容上不是新增信息）
- `transcript_segments` 整个数组保留为 `<full_transcript>`（**不切片**）
- `meta` 块整体保留为 `<meta>`

计算 `N = len(valid_slides)`、`M = len(slides) - N`（被过滤的 revisit 数）、
`K = ceil(N / 5)`（批数）。

告诉用户：
> "开始 AI 解读：共 N 张有效幻灯片（过滤掉 M 张回顾），分 K 批并行处理，
> 预计 K × 30-60 秒。"

**边界**：如果 N == 0（一张图也没截到），跳过 Phase 4 整段，直接告诉用户：
> "本次录制没有截图，跳过 AI 解读。transcript.md 可单独参考。"
然后正常退出流程，**不要写空的 interpretation.md / summary.md**。

### Step 4.2: 分批 + 并行 spawn sub-agent

把 `<valid_slides>` 按 5 张一组切成 `K` 批（最后一批可少于 5 张）。

**关键**：在**同一条 Claude 回复里**发出 `K` 个 Agent tool 调用
（`subagent_type="general-purpose"`），让 Claude harness 并行执行。
不要串行 K 次调用——那会失去并行加速。

每个 sub-agent 的 prompt 模板（精确照抄，把 `<...>` 替换成本批数据）：

```
你是 MeetingMind 的批处理 sub-agent，负责把 5 张幻灯片 + 完整会议
转录整合成 per-slide 的逐页解读。本批共 <batch_size> 张图，slide_number
从 <first> 到 <last>。其他批由别的 agent 处理，**不要碰你这 5 张之外
的内容**。

═══ 会议上下文 ═══

- 主题: <meta.topic>
- 日期: <meta.date>
- 录制起始时间（墙钟）: <meta.start_time>
- 总时长: <meta.duration_seconds> 秒

═══ 本批的 5 张幻灯片 ═══

按 slide_number 顺序：

Slide <slide_number>:
  - 文件路径（相对 meeting_dir）: <path>
  - 拍摄时间（墙钟）: <captured_at>
  - 录制偏移（[HH:MM:SS] 进入录制后多久）: <offset>

…（共 5 项 / 本批实际张数）

绝对路径：<meeting_dir>/<path>

═══ 完整 transcript_segments（不要切片） ═══

<完整 JSON 数组贴在这里，每项含 index/start/end/text>

═══ 输出格式（精确照抄） ═══

按 slide_number 升序，每张图一段（**标题下紧跟一行图片引用** `![Slide <N>](<path>)`，
让合并后的 interpretation.md 图文对照；`<path>` 用上面给你的相对路径 `slides/slide_xxx.png`）：

## Slide <N> [<offset>]

![Slide <N>](<path>)
**视觉内容**: <一段 1-3 句话描述图上画/写的什么。识别 PPT 标题、
图表类型、代码片段、UI 截图、公式等。**只看本张图**，不要参考其他 slide。>
**对应转录**: <从 transcript_segments 找出与本图 offset 前后 1-2 分钟
最相关的口语内容。引用 1-3 句关键话，前面带 [start-end] 时间区间。>
**内容解读**: <综合视觉+口语，2-3 句话讲清楚这张图想说什么、为什么
重要。不要重复"视觉内容"或"对应转录"已经说过的话。>

5 张图都处理完后，最后追加一段：

## 关键术语（本批次）
- <Term>: <定义或上下文，一句话>
- …

只列本批 5 张图里出现的非常规术语；最多 5 个；常见词（meeting / slide /
project 等）不列。

═══ 关键约束 ═══

- 用 Read 工具读图（你是多模态，能直接看 PNG）
- 路径解析失败 / 图损坏 → 该 slide 段写
  `**视觉内容**: [图像读取失败：<错误>]` 然后跳过另外两段
- 不要输出前言、不要输出收尾，**只输出上述 markdown 段落**
- 不要重新解释会议主题——上面已经告诉你了
- 不要总结全场——那是后续 summary 步骤的事，你只做你这 5 张
- **最终输出必须写文件**：处理完所有 slide 后，用 Write tool 把上述全部
  markdown 内容写到 `<meeting_dir>/interpretation_batch_<NN>.md`
  （<NN> = 本批序号，两位数零填充，如 01、02、12）。
  写入后只需回复一句："✓ 已写入 interpretation_batch_<NN>.md（<batch_size> 张 slide）"
  **不要**在回复文本中重复 markdown 内容——文件才是产物，回复只是确认。
```

把 `K` 个 Agent 调用并行发出去，等所有返回。

### Step 4.3: 从文件合并 → interpretation.md

所有 sub-agent 返回后：

1. **从磁盘读取批次文件**：按 batch 序号顺序 Read
   `<meeting_dir>/interpretation_batch_01.md` 到
   `<meeting_dir>/interpretation_batch_<KK>.md`（KK = 总批数的两位零填充）。
   如果某个文件不存在（对应 sub-agent 失败），用占位段落替代（见 Phase 4 失败兜底）。
2. **关键术语合并去重**：从每个批次文件末尾抽出 `## 关键术语（本批次）`
   下的列表，合并成全局唯一一份 `## 关键术语`（同名 Term 取最早出现的定义，
   最多 20 项）。
3. **加 header**（精确照抄）：

```
# 组会解读 — <meta.date>

## 会议信息
- 主题: <meta.topic>
- 日期: <meta.date>
- 录制起止: <meta.start_time> + <meta.duration_seconds // 60> 分钟
- 幻灯片: 总 <slides total> 张 / 有效 <N> 张（过滤 <M> 张回顾）

```

4. 用 Write tool 写到 `<meeting_dir>/interpretation.md`。
5. **清理批次文件**：合并成功后，删除所有 `interpretation_batch_*.md`
   临时文件（用 Bash `rm "<meeting_dir>"/interpretation_batch_*.md`），
   保持 meeting_dir 整洁。

### Step 4.4: 生成 summary.md

**不要复用内存里的 sub-agent 输出**——从文件读 interpretation.md
确保一致性。先 Read `<meeting_dir>/interpretation.md`。

然后**单独**一个 Agent 调用（`subagent_type="general-purpose"`，**不并行**）
生成 summary。prompt 模板：

```
你是 MeetingMind 的总结 sub-agent。基于下面的 interpretation.md 全文，
生成一份会议总结。

═══ 会议元信息 ═══

- 主题: <meta.topic>
- 日期: <meta.date>
- 时长: <meta.duration_seconds // 60> 分钟
- 起止时间: <meta.start_time> ~ <end_time>
- 有效幻灯片: <N> 张

═══ interpretation.md 全文 ═══

<完整内容贴在这里>

═══ 输出格式（精确照抄） ═══

# 组会总结 — <meta.date>
> <N> 张幻灯片 | 时长 <minutes> 分钟 | <start_time> ~ <end_time>

## 整体概要
<2-3 句：本场会议讲什么、谁讲（若 interpretation 里能看出）、主线是什么。>

## 关键要点
<3-5 个 bullet，每个 ≤ 1 句话，提炼一个独立 take-away。可引用具体
Slide N。>

## 方法与结果
<如果是技术汇报 / 研究报告：1-2 段说方法 + 结果。如果是一般讨论会：
写"（本次会议未涉及具体方法/结果讨论）"。>

## 待跟进问题
<2-4 个 bullet：未解决的问题、待办、要决策的事。无则写"（无明显
待跟进项）"。>

═══ 关键约束 ═══

- 总长度 < 500 字（中文字符 + 数字一起算）
- 具体（引用 Slide N 或具体术语），不空话
- **不要重新读 transcript.md**——interpretation.md 已经整合好了视觉+口语
- 不要输出前言、不要输出收尾，只输出上面四段
```

Agent 返回后，用 Write tool 写到 `<meeting_dir>/summary.md`。

### Step 4.5: 汇报最终产物

对用户说：
> "✓ AI 总结完成。本次会议产物：
> 
> - **逐页解读**：`<meeting_dir>/interpretation.md`（<N> 张 slide）
> - **整体总结**：`<meeting_dir>/summary.md`
> - 转录原文：`<meeting_dir>/transcript/transcript.md`
> - AI 输入数据：`<meeting_dir>/ai_input.json`
> - 原始音频：`<meeting_dir>/audio/recording.wav`
> - 截图：`<meeting_dir>/slides/`
> 
> 完整流程到此结束。"

### Phase 4 失败兜底

- **某个 sub-agent 返回错误 / 超时**：先检查对应的
  `interpretation_batch_NN.md` 是否存在且非空——sub-agent 可能已经
  写了文件后才超时。如果文件存在且非空，优先用文件内容。
  如果文件不存在或为空，在合并 interpretation.md 时，
  对应批次填占位段落：
  ```
  ## Slide <N> [<offset>]
  **视觉内容**: [本批 AI 处理失败 — <错误>，请人工检查截图]
  **对应转录**: [跳过 — 见 transcript.md 同时段]
  **内容解读**: [跳过]
  ```
  继续处理其它批次，不要因为一批失败放弃全部。
- **summary.md 生成失败**：仅产 interpretation.md，告诉用户：
  > "summary 生成失败：<错误>。interpretation.md 已生成，可手动让 Claude
  > 读它再生成 summary，或者直接看 interpretation."
- **全部 sub-agent 失败**（罕见，全是图损坏 / 文件系统问题）：
  **不要**写空 interpretation.md。告诉用户：
  > "AI 处理完全失败：<错误>。原始数据保留在 `<meeting_dir>`，可重试。"

---

## 失败兜底

### 录制启动失败（Phase 2）

`record` 子进程在 stdout 输出错误而不是 meeting_dir。常见 stderr 关键词：
- `No process matches '<keyword>'` → 用户没开会议软件
- `No active audio session` → 软件在跑但没在会议中
- `Failed to start: ...` → 其他启动错误

直接把错误内容贴给用户，建议解决路径。**不要重试** — 用户得先处理
环境问题（开软件 / 加入会议）才有意义。

### 录制被中断（进程没走到 stop）

**症状**：`postprocess` 抛 `Recording did not finish cleanly`，`recording_status` 是
`recording` 或 `incomplete`。

- `recording` —— 进程根本没走到收尾就没了（窗口重建、被杀、崩溃、断电）
- `incomplete` —— 走到了收尾，但音频写入线程被卡住、`finish()` 等超时放弃了。
  时长和静音判定都没算出来，所以 metadata 不敢声称 `complete`。

**含义**：两种都不会丢音频。
**音频不会因此丢失** —— 从 2026-08-04 起音频是边录边落盘的，`recording.wav`
里已经有截止那一刻的全部内容。缺的只是 metadata 的收尾信息（结束时间、
时长、幻灯片索引）。

**处理**：跑 recover 补全，然后正常 postprocess：

```
~/.claude/skills/meeting_mind/.venv/Scripts/python.exe -m meetingmind recover "<meeting_dir>"
~/.claude/skills/meeting_mind/.venv/Scripts/python.exe -m meetingmind postprocess "<meeting_dir>"
```

想先看会改什么就加 `--dry-run`。recover **不会**自动跑，也不该自动跑 ——
它要合并 metadata、WAV 头、截图三个来源，静默合并出错的代价比多敲一行命令高。

recover 之后 `metadata.json` 的 `recording_status` 变成 `recovered`，并带一个
`recovery` 段说明做了什么。注意 revisit（回顾型截图）记录**无法恢复**，
它只存在于内存中；重建出的索引只有正式幻灯片。

### 录制有丢失（Phase 2 结束时检查）

`record` 结束时如果丢过音频，stderr 会打一条醒目横幅：

```
====================================================================
⚠️  [session] 录制过程中有音频丢失 / audio was lost during capture:
  - audio: writer failed — ...
  - audio: 12345 bytes dropped (disk too slow)
====================================================================
```

**退出码仍然是 0，这是有意的** —— 丢了几秒的录音仍然需要转录，非零退出会让
本流程判定失败并跳过 Phase 3。所以**读 task output 时要主动扫这条横幅**，
有的话如实告诉用户丢了什么，别因为退出码是 0 就当一切正常。

精确数字也落在 `metadata.json` 的 `audio_stats` / `mic_stats` 里
（`dropped_bytes` / `late_calls` / `writer_error` / `peak_queued_bytes`），
控制台输出早没了之后仍然查得到。

### 转录失败（Phase 3）

`postprocess` 抛 RuntimeError 常见原因：
- `Transcription dependencies missing` → 用户没装 `[transcribe]` extras
- `Failed to decode <audio>` → 音频损坏（录制过程中磁盘满 / 进程崩）
- `CUDA OOM`（推理时）→ 进程干净退出（**不会** fallback CPU，那只在模型加载阶段），
  已转 chunk 保在 `transcript/_partial.md`；现默认 fp16 + 显存 fraction 0.85 护栏，
  推理 OOM 也只是进程退出、不会再溢出系统内存拖垮整机

把错误贴给用户。**transcript.md 没生成不要伪装成功**。会议数据
（音频 + 截图）已经在 `<meeting_dir>` 里，用户可以晚点手动重跑
`~/.claude/skills/meeting_mind/.venv/Scripts/python.exe -m meetingmind postprocess <meeting_dir> --force`。

### 用户中途取消

如果用户在 Phase 2 之后、Phase 3 之前说"取消"或"算了不录了"：
1. 写 STOP 文件让 record 干净退出（仍然优先这样做：干净收尾才有完整的
   metadata。但即使被 kill，音频也已经在盘上了，用 `recover` 能收尾）
2. **不**跑 postprocess
3. 告诉用户：录制已停止，原始数据保留在 `<meeting_dir>`，需要时可以
   手动跑 `~/.claude/skills/meeting_mind/.venv/Scripts/python.exe -m meetingmind postprocess <meeting_dir>` 后处理。

---

## 边界 / 已知限制（v1）

- **仅 Windows 10 build 19041+ / Windows 11**（ProcTap + WGC 限制）。
  其他平台触发时直接告诉用户"暂不支持，请在 Windows 上使用"。
- **会议窗口不能最小化到任务栏**（WGC 不抓不可见窗口）。可以拖到
  屏幕角落 / 跨虚拟桌面，但不能完全隐藏。
- **不录用户麦克风**（v1 限制，见 Phase 1 Step 1.2）。
- **每次会议要从触发到结束都在同一个 Claude 会话**。如果 Claude
  窗口关掉再开，后台 `record` 进程还在跑，但 skill 失去追踪 —
  用户可以手动写 STOP 文件到最后的 meeting_dir 来收尾。

---

## 不要做的事

- **不要替用户运行 `pip install`** 或修改用户的 Python 环境。
- **不要在录制中途改 audio/recording.wav** —— writer 线程正持有它。
  （**读**是安全的：从 2026-08-04 起 WAV 头每批都回写，录制中的文件在
  任何时刻都是合法可播的。metadata.json 同理，开录就写、原子替换，
  中途读到的是 `recording_status: "recording"` 的那一版。）
- **不要尝试加 AI 总结**（解读 / 摘要）—— P3.2 才做。本 SKILL.md
  的范围到 `postprocess` 产出 transcript.md + ai_input.json 为止。
- **不要把 meeting_dir 路径泄漏到不该泄漏的地方**（如复制粘贴到外部
  IM / 邮件）。会议内容是用户隐私。
