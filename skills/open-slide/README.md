# open-slide — 自管理 HTML 幻灯片生成器

基于 [open-slide](https://github.com/1weiho/open-slide) 开源框架的 Claude Code Skill。对话中说"做个 slide"即可从任意项目提取内容，生成 React 幻灯片并实时预览。

## 特性

- **自管理 runtime**：首次运行自动安装依赖到 `runtime/` 目录，无需手动配置 Node.js 项目
- **路径可控**：每次创建 slide 时选择输出路径，不绑定固定目录
- **热重载预览**：Vite dev server 实时刷新，修改即见效果
- **1920×1080 固定画布**：所有尺寸用 px，缩放由框架处理
- **纯 React + CSS**：无额外依赖，支持 CSS keyframe 动画

## 安装

```bash
# 复制到 Claude Code skills 目录
cp -r skills/open-slide ~/.claude/skills/

# Windows PowerShell
Copy-Item -Recurse skills/open-slide $env:USERPROFILE/.claude/skills/
```

首次使用时 skill 会自动运行 `npm install`（约 30-60 秒），后续启动秒开。

### 前置要求

- **Node.js ≥ 18**（`node --version` 检查）
- **npm**（随 Node.js 附带）
- Claude Code（CLI / Desktop / IDE 插件均可）

## 使用

在 Claude Code 对话中触发：

```
做个幻灯片
制作 slide
/slide
把这篇 paper 做成演示
准备组会汇报
```

Skill 会引导你完成：
1. **选择输出路径** — slide 文件存放位置
2. **提取内容** — 从当前项目或口述内容
3. **风格决策** — 审美方向 / 页数 / 文字密度 / 动画
4. **编写 TSX** — 生成 `<id>/index.tsx` + `assets/`
5. **实时预览** — 自动启动浏览器，`F` 全屏，方向键翻页
6. **迭代修改** — "调整第 3 页"、"换配色" 等，热重载即时生效

## 目录结构

```
open-slide/
├── SKILL.md                      ← 完整工作流定义（Phase 0-6）
├── README.md                     ← 本文件
├── scripts/
│   ├── bootstrap.py              ← runtime 初始化（幂等：创建 package.json → npm install → 写配置）
│   └── server.py                 ← dev server 生命周期（start / stop / status / open）
├── references/
│   └── slide-authoring-guide.md  ← TSX 编写规范（画布 / 字号 / 动画 / 反模式）
└── runtime/                      ← ⚠️ 自动生成，不要手动修改或提交
    ├── package.json
    ├── node_modules/
    └── open-slide.config.ts      ← 动态写入（slidesDir 指向用户路径）
```

## 工作原理

```
用户说"做个 slide"
  → SKILL.md 触发 Phase 0：AskUserQuestion 选择输出路径
  → bootstrap.py 确保 runtime/ 就绪（首次 npm install，后续跳过）
  → 动态写入 open-slide.config.ts（slidesDir = 用户指定的绝对路径）
  → server.py 在 runtime/ 下启动 npm run dev（Vite dev server）
  → 框架通过 slidesDir 配置发现并编译用户路径下的 slide
  → 浏览器自动打开 http://localhost:5173/s/<slide-id>
```

核心技巧：`@open-slide/core` 的 `slidesDir` 配置支持绝对路径（`path.resolve(cwd, slidesDir)` — 绝对路径直接透传），因此 runtime 和 slide 文件可以在不同位置。

## 上游框架

[**open-slide**](https://github.com/1weiho/open-slide)（MIT License）— Write slides in React components, we handle the rest.

- 作者：[1weiho](https://github.com/1weiho)
- 版本：`@open-slide/core ^1.2.0`
- 技术栈：React 18 + Vite 5 + TypeScript
- 核心特性：固定 1920×1080 画布、自动缩放、Tailwind CSS 内置、slide browser UI

本 Skill 不 fork 也不修改上游代码，仅通过 npm 依赖 `@open-slide/core` 包。
