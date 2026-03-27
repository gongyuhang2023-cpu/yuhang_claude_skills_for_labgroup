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

### `group_meeting_recorder` — 组会自动截图 + AI 总结

**用途**：Teams 线上组会时后台自动截图（检测 PPT 翻页），会后由 Claude 生成图文总结。

**触发词**：`截屏组会`、`自动截PPT`、`/capture`

**工作流**：
1. 说"开始截图"→ 脚本后台运行，自动检测 PPT 翻页并截图
2. 会议结束说"生成总结"→ Claude 读取截图，生成 `summary.md`

**依赖**：`mss`, `PyGetWindow`, `Pillow`, `numpy`（首次运行自动安装到 `.venv`）

**注意**：目前适配 Windows + Teams，截图保存到 `~/Desktop/meeting_captures/`

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

---

## 目录结构

```
skills/
├── git-auto-sync/
│   ├── SKILL.md
│   └── scripts/
│       ├── sync.py
│       └── update_directory.py
├── group_meeting_recorder/
│   ├── SKILL.md
│   ├── requirements.txt
│   └── scripts/
│       ├── run.py
│       ├── capture.py
│       └── setup_environment.py
└── humanizer/
    ├── SKILL.md
    ├── README.md
    ├── LICENSE
    ├── WARP.md
    └── consultation-notes.md
```
