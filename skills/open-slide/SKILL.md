---
name: open-slide
description: |
  全局 HTML 幻灯片生成器。当用户要求"制作幻灯片"、"做个 slide"、"做个演示"、
  "调用 open-slide"、"将内容转为演示"、"/slide"时触发。
  从当前项目提取内容，生成 React 幻灯片到用户指定路径，
  自动启动 dev server 并打开浏览器预览。
  运行时环境自管理（{SKILL_DIR}/runtime/），无需预装 open-slide。
---

# open-slide — 跨项目 HTML 幻灯片生成器

从任意项目中提取内容，生成高质量 React 幻灯片，自动预览。

## 架构

```
{SKILL_DIR}/
├── SKILL.md                      ← 本文件
├── scripts/
│   ├── bootstrap.py              ← runtime 初始化（幂等）
│   └── server.py                 ← dev server 生命周期管理
├── references/
│   └── slide-authoring-guide.md  ← TSX 编写规范
└── runtime/                      ← 自动生成，不手动修改
    ├── package.json
    ├── node_modules/
    └── open-slide.config.ts      ← 每次启动动态写入
```

## 硬规则

- Slide 文件写入**用户指定路径**下的 `<id>/` 子目录
- 每个 slide = 一个 `index.tsx` + 可选 `assets/`，不创建其他文件
- 不手动修改 `runtime/` 目录内任何文件（由 bootstrap.py 管理）
- 不添加 npm 依赖，只用 `react` + 标准 Web API
- 写 TSX 前**必须先读** `{SKILL_DIR}/references/slide-authoring-guide.md`

## 工作流

### Phase 0 — 确定输出路径

用 `AskUserQuestion` 询问用户 slide 文件存放位置。提供当前工作目录作为参考。

示例问题："幻灯片文件保存到哪个目录？"，选项可包含：
- 用户之前提到的路径（如果有）
- 当前项目目录下的子文件夹
- 用户自定义路径

确定后，该路径即为本次会话的 **SLIDES_DIR**。后续所有文件操作基于此路径。

> **首次运行**：如果 `{SKILL_DIR}/runtime/node_modules/` 不存在，bootstrap 会自动安装依赖（约 30-60 秒）。提前告知用户。

### Phase 1 — 提取内容（当前项目）

从用户当前项目中收集幻灯片素材。根据项目类型智能搜索：

**academic-os 项目**：
- `learning-pages/` — 已有的学习笔记
- `raw/<paper>/` — 文献原始数据
- `projects/` — 项目文档
- `sparks.md` — 想法记录

**一般项目**：
- README.md、CLAUDE.md — 项目概述
- 用户指定的文件或目录
- 用户口述的内容

**科研项目**：
- `Experiments/` — 实验记录和数据
- `data/` — 图表和结果

提取后整理为结构化的**内容简报**（不写入文件，在内存中保持）：
- 核心主题
- 关键论点 / 数据 / 图表
- 目标受众

如果用户直接口述内容而非从项目提取，跳过搜索直接进入 Phase 2。

### Phase 2 — 规划幻灯片

#### Step 2.1: 确认主题

如果用户的请求不够具体（"做个幻灯片"、"帮我准备组会"），先用 `AskUserQuestion` 确认：
- 主题是什么
- 目标受众
- 有无草稿大纲

如果主题已经明确，跳过直接进入 Step 2.2。

#### Step 2.2: 风格决策（一次性问完）

用**一个** `AskUserQuestion` 调用问四个问题：

1. **审美方向** — 根据主题提出 3 个视觉方向。每个选项 = 氛围词 + 具体视觉线索（配色、字体、motif）。三个方向要有明显差异。标注推荐项。
   - 示例（科研组会）：**暗色技术** (深蓝/amber 高对比，mono 标题) · **学术清爽** (off-white，单色 accent，大留白) · **数据导向** (图表为主角，中性配色 + 状态色)
2. **页数** — 3-5（短）/ 6-10（标准）/ 11-20（深度）/ 自定
3. **文字密度** — minimal（一行/大数字）/ light（标题 + 2-3 bullets）/ standard（标题 + 4-5 bullets）/ dense（多列/详细）
4. **动画** — 静态 / 微妙（fade/entrance）/ 丰富（keyframes/staggered）

#### Step 2.3: 选择 slide id

kebab-case，短且描述性。示例：`gut-microbiome-talk`、`q2-progress`、`paper-review-cregger`。

检查 `{SLIDES_DIR}/` 下避免与已有文件夹冲突。

#### Step 2.4: 规划页面结构

列出每页的角色和内容概要。常见页面类型参见 `references/slide-authoring-guide.md`。

**原则：一页一个想法。**

### Phase 3 — 编写 slide

1. **读取 authoring guide**：读 `{SKILL_DIR}/references/slide-authoring-guide.md`
2. **确定视觉方向**：选定 palette / type scale / aesthetic，声明 `export const design: DesignSystem`
3. **编写 `index.tsx`**：
   - 所有内容写入一个文件 `{SLIDES_DIR}/<id>/index.tsx`
   - 遵循 authoring guide 的全部规范
   - 每页验算垂直预算（font × line_height × lines + gaps + 2×padding ≤ 1080）
   - 如果需要 assets，在 `{SLIDES_DIR}/<id>/assets/` 下放置
4. **Self-review**：逐项检查 authoring guide 末尾的 checklist

### Phase 4 — 启动预览

slide 写入完成后，自动启动 dev server 并打开浏览器：

```bash
python "{SKILL_DIR}/scripts/server.py" start <slide-id> --slides-dir "{SLIDES_DIR}"
```

server.py 会：
1. 调用 bootstrap.py 确保 runtime 就绪（首次自动安装依赖）
2. 动态写入 `open-slide.config.ts`（slidesDir 指向用户路径）
3. 后台启动 `npm run dev`（不阻塞终端）
4. 等待 server 就绪（最多 15 秒）
5. 自动打开浏览器到 `http://localhost:5173/s/<slide-id>`

告知用户：
> 幻灯片已生成并在浏览器中打开。热重载已启用，后续修改会实时更新。
> - `F` 进入全屏播放模式
> - Arrow keys 导航页面
> - Esc 退出全屏

### Phase 5 — 迭代（用户请求修改时）

用户可能说"调整第 3 页"、"换个配色"、"加一页"等。处理方式：

1. 读取当前 `{SLIDES_DIR}/<id>/index.tsx`
2. 按要求修改（用 Edit tool）
3. 浏览器会自动热重载，无需重启 server
4. 如需打开已关闭的浏览器：`python "{SKILL_DIR}/scripts/server.py" open <slide-id>`

### Phase 6 — 收尾

当用户表示满意或明确结束时：

```
是否关闭 open-slide dev server？
```

用 `AskUserQuestion` 确认。如果用户选择关闭：

```bash
python "{SKILL_DIR}/scripts/server.py" stop
```

## 特殊场景

### 从 paper 生成讲解 slide

如果用户说"把这篇 paper 做成幻灯片"：
1. 在当前项目中定位 paper 的学习笔记（`learning-pages/`）或原始数据（`raw/`）
2. 提取关键信息：背景、方法、结果、讨论
3. 按 paper 讲解的标准结构规划：
   - Cover（论文标题 + 作者 + 期刊）
   - Background / Gap
   - Methods（1-2 页，重点图表）
   - Results（关键发现，每页一个）
   - Discussion（意义 + 局限）
   - Take-home message

### 科研组会进展报告

如果用户说"准备组会汇报"：
1. 扫描实验记录和近期数据
2. 按组会汇报结构规划：
   - Cover（日期 + 汇报人）
   - 上次进展回顾（1 页）
   - 本周实验（方法 + 结果）
   - 问题 / 讨论
   - 下一步计划

### 直接口述内容

如果用户直接描述要做什么（而非从项目提取），跳过 Phase 1 的文件搜索，直接用口述内容进入 Phase 2。

## 服务器管理命令参考

```bash
# 启动并打开浏览器（指定 slides 路径）
python "{SKILL_DIR}/scripts/server.py" start <slide-id> --slides-dir "{SLIDES_DIR}"

# 仅打开浏览器（server 已在运行时）
python "{SKILL_DIR}/scripts/server.py" open <slide-id>

# 检查状态
python "{SKILL_DIR}/scripts/server.py" status

# 关闭
python "{SKILL_DIR}/scripts/server.py" stop
```
