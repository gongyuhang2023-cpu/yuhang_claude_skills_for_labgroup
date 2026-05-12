# MeetingMind — 组会录制

## Description
一键录制组会：自动录音（WASAPI loopback 系统音频）+ 自动截图（PPT 翻页检测）+ 语音转文字（Qwen3-ASR）。纯本地运行，不依赖第三方软件。

## Trigger
当用户提到以下任一关键词时触发：
"录制组会"、"开始录制"、"组会录制"、"/meeting"、"meeting mind"、"meetingmind"、"录制会议"、"开始会议录制"

## Workflow

### Phase 0: 首次使用检测

1. Read `~/.claude/skills/meeting_mind/config.yaml`
2. If `output.base_dir` is empty → **首次使用**:
   - Read `~/.claude/skills/meeting_mind/SETUP_GUIDE.md`
   - Follow the guide: check environment, troubleshoot if needed, ask save location + ASR preference
   - For save location: recommend setting to llm-wiki raw path (e.g. `C:/Users/Yuhang/Library/PhD wiki/PhD wiki/raw/sources`) so recordings auto-import into wiki
   - Write choices back to config.yaml (`output.base_dir` and `transcription.engine`)
   - Then continue to Phase 1
3. If `output.base_dir` is set → **非首次**, skip directly to Phase 1

### Phase 1: 参数确认

Use **AskUserQuestion** with 3 questions:

Question 1 — Meeting software:
- header: "会议软件"
- Options: "Teams" (default), "Zoom", "腾讯会议"

Question 2 — Screenshot settings:
- header: "截图设置"
- Options:
  - "默认 (间隔5s, 阈值5%)" — recommended
  - "敏感 (间隔3s, 阈值3%)" — fast presentations
  - "宽松 (间隔10s, 阈值8%)" — slow discussions

Question 3 — Microphone recording:
- header: "麦克风"
- Options:
  - "仅系统声音" — recommended, only record what others say (loopback)
  - "系统 + 麦克风" — also record your voice, mixed into one file

### Phase 2: 启动录制

1. Read `output.base_dir` from config.yaml, determine output: `<base_dir>/YYYY-MM-DD/`

2. Map settings to CLI args:
   - 默认 → `--interval 5 --threshold 5`
   - 敏感 → `--interval 3 --threshold 3`
   - 宽松 → `--interval 10 --threshold 8`
   - Teams → `--meeting teams`, Zoom → `--meeting zoom`, 腾讯会议 → `--meeting tencent`
   - 系统 + 麦克风 → `--mic`
   - config `audio.virtual_cable.enabled` is true → `--virtual-cable "<keyword>"` (keyword from config)

3. **Launch in background**:
   ```bash
   python ~/.claude/skills/meeting_mind/scripts/run.py session.py \
     --meeting <software> --interval <N> --threshold <N> \
     [--mic] \
     [--virtual-cable "<audio.virtual_cable.keyword>"] \
     --output "<base_dir>/YYYY-MM-DD"
   ```
   Use `run_in_background: true`.

4. Inform user: software, interval, threshold, virtual cable status, output path, task ID.

### Phase 3: 停止 + 后处理

When user says "结束了"、"会议结束"、"停止录制"、"stop":

#### Step 1: 停止录制

1. **Graceful stop**: Create a STOP file in the output directory to trigger graceful shutdown:
   ```bash
   touch "<output_dir>/STOP"
   ```
   Wait up to 10 seconds for the task to finish (check with TaskOutput block=true timeout=10000).
   If still running, **TaskStop** as fallback.
2. **Parse JSON** between `===JSON_OUTPUT===` and `===END_JSON===`
3. JSON structure:
   ```json
   {
     "status": "completed",
     "audio_path": "...", "output_dir": "...",
     "start_time": "HH:MM:SS", "end_time": "HH:MM:SS",
     "total_slides": N,
     "slides": [
       {"filename": "slide_001.png", "slide_number": 1, "timestamp": "HH:MM:SS"},
       {"type": "revisit", "original_slide": 1, "timestamp": "HH:MM:SS"},
       ...
     ]
   }
   ```

#### Step 2: 转录

If config `transcription.engine` is not "skip", launch transcription:
```bash
python ~/.claude/skills/meeting_mind/scripts/run.py transcribe.py \
  --audio "<audio_path>" --output "<output_dir>/transcript" \
  --vocabulary "~/.claude/skills/meeting_mind/vocabulary.txt"
```
Use `run_in_background: true`, inform user processing time. Wait for completion before proceeding.

#### Step 3: 读取转录文本

Read `<output_dir>/transcript/transcript.md` into memory. This is pure text, small token cost.

#### Step 4: 分批读取截图 + 生成解读 (interpretation.md)

1. **Filter unique slides** from JSON: only entries where `"type"` is absent. Skip `"type": "revisit"` entries (PPT pages flipped back during discussion). Record revisit timestamps for timeline context.

2. **Split into batches of 5 slides each**.

3. **Spawn sub-agents in parallel** (one Agent per batch). Each agent receives:
   - The 5 slide image paths (agent reads them via Read tool)
   - The full transcript.md text (passed as string in prompt)
   - The slide timestamps + revisit timestamps for this batch's time range
   - Instructions:

   > 你是组会解读助手。请阅读以下 5 张幻灯片截图和转录文本，完成：
   > 1. 用 Read 工具读取每张截图图片
   > 2. 根据截图时间戳，定位转录文本中对应的段落
   > 3. 对每张 slide 生成解读，格式如下：
   >
   > ## Slide N (HH:MM:SS)
   > **视觉内容**: [截图中的文字、图表、公式等完整描述]
   > **对应转录**: [该 slide 时间段内的转录文本摘要]
   > **内容解读**: [结合截图和转录的综合分析]
   >
   > 最后返回所有 slide 解读的拼接文本。

4. **Collect all agent results**, concatenate in slide order.

5. **Write `interpretation.md`** to `<output_dir>/`:

```markdown
# 组会解读 — YYYY-MM-DD

## 会议信息
- 日期: YYYY-MM-DD
- 时长: XX 分钟 (HH:MM ~ HH:MM)
- 独立幻灯片: N 张

## Slide 1 (HH:MM:SS)
**视觉内容**: [...]
**对应转录**: [...]
**内容解读**: [...]

## Slide 2 (HH:MM:SS)
...

## 关键术语
- Term: 定义
- ...
```

#### Step 5: 生成总结 (summary.md)

Based on the interpretation.md text (no need to re-read images), generate `summary.md` in `<output_dir>/`:

```markdown
# 组会总结 — YYYY-MM-DD

> 共 N 张独立幻灯片 | 录音时长 XX:XX | HH:MM ~ HH:MM

## 整体概要
[2-3 sentences capturing the main topic and conclusion]

## 关键要点
- ...

## 方法与结果
- ...

## 待跟进问题
- ...

## 录音与转录
- 录音文件: recording.wav
- 转录文件: transcript/transcript.md
```

#### 最终目录结构

所有文件直接生成在 `<output_dir>/`（即 wiki raw 目录），无需复制：

```
<output.base_dir>/seminars/YYYY-MM-DD/
├── interpretation.md    ← llm-wiki 主要 ingest 对象（图片已文字化）
├── summary.md           ← 全局总结
├── audio/
│   └── recording.wav    ← 录音（llm-wiki 不会 ingest .wav）
├── slides/              ← 截图原件
│   ├── slide_001.png
│   └── ...
├── transcript/
│   └── transcript.md    ← 转录文本
└── metadata.json        ← session 元数据
```
