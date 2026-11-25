# ppt-master skill — 部署与可移植说明

> 给"换电脑 / 别人接手"看的部署文档。本 skill 是**薄壳**，依赖一个**外部引擎**，两部分分开放，本文档说明怎么把缺的那部分补齐。

## 架构（为什么文件在两处）

| 部分 | 位置 | 体量 | 说明 |
|------|------|------|------|
| **科研规范层**（本 skill） | 你的 skills 目录下的 `ppt-master/` | 几百 KB | 科研汇报的结构、讲稿和版式规范 |
| **ppt-master 引擎** | 你选的工具目录，例如 `~/Tools/ppt-master` 或 `C:\Users\<你>\Tools\ppt-master` | ~1.3 GB | 第三方开源仓库（hugohe3/ppt-master），含 1.1GB examples 和独立 .git，**故意不放进 skills** |
| **个人习惯区** | 本 skill 的 `references/*.local.md` | 几 KB | 你自己的引擎路径、标志、封面、素材库位置，不随本 skill 分发，升级时保留 |

**为什么分开**：引擎是活跃维护的第三方仓库，整个塞进 skills 会污染 skill 体系、撑爆备份，上游更新也要手动合并。薄壳骑在它上面是可持续的"嫁接"。

## 部署引擎

### Linux / macOS

```bash
git clone https://github.com/hugohe3/ppt-master.git ~/Tools/ppt-master
cd ~/Tools/ppt-master
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt svglib reportlab cairosvg
.venv/bin/python -c "import pptx,svglib,PIL,numpy; print('CORE OK', pptx.__version__)"
```

ARM64 机器用本机的 wheel 建 venv，不复制别的机器的 site-packages。

### Windows

```powershell
git clone https://github.com/hugohe3/ppt-master.git "C:\Users\<你>\Tools\ppt-master"
cd "C:\Users\<你>\Tools\ppt-master"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt svglib reportlab cairosvg
.\.venv\Scripts\python.exe -c "import pptx,svglib,PIL,numpy; print('CORE OK', pptx.__version__)"
```

装好后，在 `references/` 下建一个 `personal.local.md`，写上引擎目录。SKILL.md 里的 `<引擎目录>` 以它为准。

## 预览渲染说明（Windows 坑）

- **cairosvg 在 Windows 缺 libcairo dll，`import cairosvg` 会失败** → 渲染 SVG 为 PNG 改用 **Edge headless**：
  ```powershell
  & "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --headless=new --disable-gpu --screenshot="out.png" --window-size=1280,720 "page.svg"
  ```
- 某些 SVG 只有 viewBox、没有 width/height，Edge 截图会缩小 → 用 HTML wrapper（`<img src=svg width=1280 height=720>`，body margin:0）再截。

## API Key（一般科研不需要）

- 核心 SVG→PPTX 导出**零 key**。
- key 只为 **AI 配图**（`image_gen.py`，OpenAI/Gemini 等）和 **网络图搜**。
- **科研用真实数据图（R/Python 出图），通常不配 key**。要配见引擎 `.env.example`。

## 引擎更新

在引擎目录 `git fetch`，确认历史连续、本地修改已保护后再 `git pull`。更新前备份科研适配层、引擎及 `projects/` 等本地文件。若上游改写历史导致无共同祖先，用旁路 clone 和独立 venv 验证后切换，不强拉或重置旧仓库。本 skill 的科研规范独立演进；更新后检查路由、字段和脚本路径是否仍匹配，不用上游同名 skill 覆盖本目录。

## 更新本 skill

`references/*.local.md` 是你自己的，不在本仓库里。整目录替换升级时，先把它们拷出来，装好后放回。

## 相关

- 引擎仓库：https://github.com/hugohe3/ppt-master
