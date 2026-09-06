# ppt-master skill — 部署与可移植说明

> 给"换电脑 / 别人接手"看的部署文档。本 skill 是**薄壳**，依赖一个**外部引擎**，两部分分开放，本文档说明怎么把缺的那部分补齐。

## 架构（为什么文件在两处）

| 部分 | 位置 | 体量 | 说明 |
|------|------|------|------|
| **科研规范层**（本 skill） | `~/.claude/skills/ppt-master/` | 几百 KB | 我们自己写的，随 Claude skills 走，可被 skill-snapshot 备份 |
| **ppt-master 引擎** | `C:\Users\<你>\Tools\ppt-master/` | ~1.3 GB | 第三方开源仓库(hugohe3/ppt-master)，含 1.1GB examples + 独立 .git，**故意不放进 skills**(太大/独立git/snapshot 会跳过) |

**为什么分开**：引擎是活跃维护的第三方仓库(29k★)，整个塞进 skills 会污染 skill 体系、撑爆备份、且上游更新要手动合并。薄壳骑在它上面是可持续的"嫁接"。

## 换电脑 / 别人首次部署（4 步）

```powershell
# 1) clone 引擎(到你选的工具目录，下面用 Tools 举例)
git clone https://github.com/hugohe3/ppt-master.git "C:\Users\<你>\Tools\ppt-master"

# 2) 建 venv + 装最小依赖(导出/渲染用，零 API key)
cd "C:\Users\<你>\Tools\ppt-master"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install python-pptx svglib reportlab Pillow numpy cairosvg
# 注意：不要加 --quiet(该 pip 不认这个选项)

# 3) 验证
.\.venv\Scripts\python.exe -c "import pptx,svglib,PIL,numpy; print('CORE OK', pptx.__version__)"

# 4) 改本 skill 的 SKILL.md：把引擎路径 C:\Users\<你>\Tools\ppt-master 全部替换为新机器路径
```

## 预览渲染说明（Windows 坑）

- **cairosvg 在 Windows 缺 libcairo dll，`import cairosvg` 会失败** → 渲染 SVG 为 PNG 改用 **Edge headless**：
  ```powershell
  & "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --headless=new --disable-gpu --screenshot="out.png" --window-size=1280,720 "page.svg"
  ```
- 某些 SVG(只有 viewBox 无 width/height)Edge 截图会缩小 → 用 HTML wrapper(`<img src=svg width=1280 height=720>`，body margin:0)再截。

## API Key（一般科研不需要）

- 核心 SVG→PPTX 导出**零 key**。
- key 只为 **AI 配图**(`image_gen.py`，OpenAI/Gemini 等)和 **网络图搜**。
- **科研用真实数据图(R/Python 出图)，通常不配 key**。要配见引擎 `.env.example`。

## 引擎更新

```powershell
cd "C:\Users\<你>\Tools\ppt-master"; git pull
```
更新引擎不影响本 skill；本 skill 的科研规范独立演进。

## 相关
- 引擎仓库：https://github.com/hugohe3/ppt-master
- 本 skill 科研规范萃取自 `~/.claude/skills/meeting-ppt-vba/`(VBA 版，已停用作主力但规范层是其精华)
