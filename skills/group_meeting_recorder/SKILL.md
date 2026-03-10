# Group Meeting Recorder

## Description
Teams 线上组会自动截图工具。检测 PPT 翻页并截图，会议结束后由 Claude 生成内容总结。

## Trigger
当用户提到"截屏组会"、"会议截图"、"自动截PPT"、"capture meeting"、"开始截图"、"/capture"、"组会记录"时触发。

## Workflow

### Phase 1: Start Capture (Background)

When the user wants to start capturing:

1. **Launch capture script in background** (meetings last 30-60min, exceeds Bash timeout):
   ```bash
   python ~/.claude/skills/group_meeting_recorder/scripts/run.py capture.py [options]
   ```
   Use `run_in_background: true` for the Bash tool.

2. **Available options** (ask user if needed):
   - `--threshold <float>`: Change detection threshold, default 5.0 (% of changed pixels)
   - `--interval <float>`: Detection interval in seconds, default 5.0
   - `--title <keyword>`: Window title keyword, default "Teams"
   - `--output <dir>`: Output directory, default `~/Desktop/meeting_captures/YYYY-MM-DD/`

3. **Inform the user**:
   - "截图已在后台运行，检测间隔 Xs，输出到 <dir>"
   - "会议结束后告诉我，我会生成总结"
   - Provide the background task ID so they can check status

### Phase 2: Generate Summary (After Meeting)

When the user says "会议结束了"、"生成总结"、"meeting is over":

1. **Read background task output** using TaskOutput tool
2. **Parse JSON summary** between `===JSON_OUTPUT===` and `===END_JSON===` markers
3. **Read each captured slide** using the Read tool (Claude's multimodal capability)
4. **Generate summary.md** in the output directory:

```markdown
# 组会截图总结 - YYYY-MM-DD

> 共 N 张幻灯片 | HH:MM ~ HH:MM

## Slide 1 (HH:MM:SS)
![slide_001](slide_001.png)
**内容**: [Description of slide content based on visual analysis]

## Slide 2 (HH:MM:SS)
![slide_002](slide_002.png)
**内容**: [Description of slide content]

...

## 整体总结
[Overall summary of the meeting presentation]

## 关键要点
- [Key point 1]
- [Key point 2]
- ...

## 待跟进问题
- [Question or action item 1]
- [Question or action item 2]
```

## Notes

- The capture script uses pixel-change detection (not SSIM) — more intuitive and robust against minor UI animations
- Default 5% threshold: mouse movements cause <1% change, slide transitions cause 30-80%, notifications 2-4%
- Debounce mechanism (0.5s wait) prevents capturing mid-transition frames
- Window disappearing for 30s+ triggers auto-stop (meeting ended)
- DPI-aware: works correctly on high-DPI displays
- All output is UTF-8 encoded
