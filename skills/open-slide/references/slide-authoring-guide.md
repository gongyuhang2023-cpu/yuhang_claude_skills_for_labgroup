# open-slide Authoring Guide

open-slide 框架的完整编写规范。写 TSX 前必读。

## 文件契约

```tsx
// slides/<id>/index.tsx
import type { DesignSystem, Page, SlideMeta } from '@open-slide/core';

export const design: DesignSystem = { /* ... */ };

const Cover: Page = () => <div style={{ width: '100%', height: '100%' }}>…</div>;
const Body: Page = () => <div style={{ width: '100%', height: '100%' }}>…</div>;

export const meta: SlideMeta = { title: 'My slide' };
export default [Cover, Body] satisfies Page[];
```

- `export default` 是 **非空 Page[] 数组**，每个 Page 是零 prop React 组件
- `meta.title`（可选）显示在 slide header
- slide id = 文件夹名，kebab-case
- 一个 slide = 一个 `index.tsx` + `assets/`，**不创建**其他 .tsx/.ts 文件
- **不添加依赖**，只用 `react` 和标准 Web API
- **不修改** `package.json`、`open-slide.config.ts`、其他 slide

## 画布

**固定 1920 x 1080**，框架自动缩放。用**绝对像素值**设计。

- `font-size`、padding、定位都用 px。禁用 rem、vw/vh、% for type
- 每个 page 根元素：`width: '100%'; height: '100%'`
- 首选 inline `style={{ … }}`。CSS 是全局的，需要 scope classnames

### Type Scale

| 元素 | 尺寸 |
|------|------|
| Hero title | 140–200px |
| Section heading | 80–120px |
| Page heading | 56–80px |
| Body text | 32–44px |
| Caption / label | 22–28px |

### 间距

- 内容 padding：**100–160px**，文字不可碰边
- line-height：heading 1.2，body 1.5–1.7
- 元素间距：32–64px

### 垂直预算 — 内容必须在 1080px 内

画布**不滚动**。超出 1080px 的内容被裁切。写 JSX 前先算高度。

**可用高度** = `1080 − top_padding − bottom_padding`
- 120px padding 每边 → **840px**
- 160px padding 每边 → **760px**

**元素高度** = `font_size × line_height × lines`，加上 gap。

验算示例（120px padding，budget = 840px）：

| 元素 | 高度 |
|------|------|
| Heading 80px × 1.2 × 1 行 | 96px |
| Gap | 64px |
| Body 40px × 1.6 × 3 行 | 192px |
| Gap | 48px |
| 5 bullets 40px × 1.6 × 1 行 | 320px |
| 4 gaps 24px | 96px |
| **合计** | **816px (fits)** |

规则：
- 一个 heading + body **或** heading + ≤5 短 bullets，不要两者都堆
- Bullet 不应换行。换行了就缩短或拆页
- Hero title 页只放标题 + 副标题
- 如果需要缩小字号或 padding 来塞下内容 → **拆成两页**
- **禁止** `overflow: auto/scroll/hidden`、负 margin、transform 隐藏溢出

## DesignSystem（推荐使用）

```tsx
import type { DesignSystem, Page } from '@open-slide/core';

export const design: DesignSystem = {
  palette: { bg: '#0f172a', text: '#f8fafc', accent: '#fbbf24' },
  fonts: {
    display: 'system-ui, -apple-system, sans-serif',
    body: 'system-ui, -apple-system, sans-serif',
  },
  typeScale: { hero: 180, body: 40 },
  radius: 12,
};
```

CSS 变量（用于 inline style，拖拽 Design 面板时实时更新）：
`--osd-bg`, `--osd-text`, `--osd-accent`, `--osd-font-display`, `--osd-font-body`, `--osd-size-hero`, `--osd-size-body`, `--osd-radius`

```tsx
<div style={{
  background: 'var(--osd-bg)',
  color: 'var(--osd-text)',
  fontFamily: 'var(--osd-font-body)',
  fontSize: 'var(--osd-size-body)',
  borderRadius: 'var(--osd-radius)',
}}>
```

格式要求：
- 必须是 `export const design: DesignSystem = { … }` 形式
- 对象必须是字面量（无 spread、无函数调用）

## Starter Template

```tsx
import type { DesignSystem, Page, SlideMeta } from '@open-slide/core';

export const design: DesignSystem = {
  palette: { bg: '#0f172a', text: '#f8fafc', accent: '#fbbf24' },
  fonts: {
    display: 'system-ui, -apple-system, sans-serif',
    body: 'system-ui, -apple-system, sans-serif',
  },
  typeScale: { hero: 180, body: 40 },
  radius: 12,
};

const muted = '#94a3b8';

const fill = {
  width: '100%',
  height: '100%',
  fontFamily: 'var(--osd-font-body)',
} as const;

const Cover: Page = () => (
  <div
    style={{
      ...fill,
      background: 'var(--osd-bg)',
      color: 'var(--osd-text)',
      display: 'flex',
      flexDirection: 'column',
      justifyContent: 'center',
      padding: '0 160px',
    }}
  >
    <div style={{ fontSize: 28, color: 'var(--osd-accent)', letterSpacing: '0.2em' }}>
      CHAPTER 01
    </div>
    <h1
      style={{
        fontFamily: 'var(--osd-font-display)',
        fontSize: 'var(--osd-size-hero)',
        fontWeight: 900,
        margin: '32px 0',
        lineHeight: 1.05,
      }}
    >
      The Big Idea
    </h1>
    <p style={{ fontSize: 'var(--osd-size-body)', color: muted, maxWidth: 1200 }}>
      A short subtitle that explains what this slide is about.
    </p>
  </div>
);

const Content: Page = () => (
  <div style={{ ...fill, background: 'var(--osd-bg)', color: 'var(--osd-text)', padding: 120 }}>
    <h2 style={{ fontFamily: 'var(--osd-font-display)', fontSize: 80, fontWeight: 800, margin: 0 }}>
      Section heading
    </h2>
    <ul style={{ fontSize: 'var(--osd-size-body)', lineHeight: 1.6, marginTop: 64, paddingLeft: 48 }}>
      <li>One clear point per line</li>
      <li>Keep to 3-5 bullets</li>
      <li>Let the space breathe</li>
    </ul>
  </div>
);

export const meta: SlideMeta = { title: 'The Big Idea' };
export default [Cover, Content] satisfies Page[];
```

## 视觉方向

选一个方向，全 deck 保持一致：
- **Palette** — 1 background + 1 primary text + 1 accent + 1 muted，定义为常量
- **Typography** — 一个 display font + 一个 body font。标题 800-900 weight，正文 400-500
- **Layout grid** — 选一个 content padding（如 120px）并坚持。左对齐偏 editorial，居中偏 ceremonial
- **Aesthetic** — 选一个：minimal / maximalist / editorial / retro / brutalist / soft / neon / paper

## 资源文件

放在 `slides/<id>/assets/` 下，ES module import：

```tsx
import hero from './assets/hero.jpg';
<img src={hero} style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
```

URL-only：
```tsx
const videoUrl = new URL('./assets/intro.mp4', import.meta.url).href;
```

纯文字 slide 无需 assets 文件夹。

## ImagePlaceholder

仅当确实需要用户提供真实图片时使用：

```tsx
import { ImagePlaceholder } from '@open-slide/core';
<ImagePlaceholder hint="Q3 revenue chart" width={1280} height={720} />
```

**不要**用于装饰、stock photo 填充、或可以用排版/图标替代的位置。

## 重复元素：用组件，不用 map

```tsx
// OK — 每个 Card 独立 JSX 节点
<Card src={alpha} label="Alpha" />
<Card src={beta}  label="Beta" />
<Card src={gamma} label="Gamma" />

// BAD — map 共享模板，inspector 无法独立编辑
items.map(item => <div><img src={item.src} /></div>)
```

组件定义在**同一个 `index.tsx`** 中，不创建 sibling 文件。

## Page 类型速查

| 类型 | 用途 |
|------|------|
| Cover | 标题 + 副标题，强视觉 |
| Agenda | 3-5 项概览 |
| Section divider | 章节间大标签 |
| Content | heading + 2-5 bullets 或 heading + 一个视觉 |
| Big number | 一个统计数字占满画布 |
| Quote | Pull-quote + 出处 |
| Comparison | 两栏 before/after 或 A vs B |
| Closing | CTA / 感谢 / 联系方式 |

**原则：一页一个想法。想放两个就拆。**

## 动画（可选）

用 CSS `@keyframes` + inline style + `useEffect`。不加额外库。

常用模式：
```tsx
const styles = `
  @keyframes fadeUp {
    from { opacity: 0; transform: translateY(16px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  .fadeUp { opacity: 0; animation: fadeUp .85s cubic-bezier(.2,.7,.2,1) forwards; }
`;
const Styles = () => <style>{styles}</style>;
```

## 导航行为（运行时自带）

- Arrow / PageUp / PageDown 导航
- `F` 进入全屏播放模式
- 播放模式：Space/→ 下一页，← 上一页，Esc 退出
- 热重载：编辑 `index.tsx` 浏览器自动更新

## Self-Review Checklist

- [ ] `export default` 非空 `Page[]`
- [ ] 每个 page 根元素 fill 100%
- [ ] 内容在 padding 内（不碰边）
- [ ] **每页验算：(font × line_height × lines) + gaps + 2×padding ≤ 1080**
- [ ] 无 bullet 换行
- [ ] 全 deck 一致的视觉方向
- [ ] 声明了 `export const design: DesignSystem`，用 `var(--osd-X)` 引用
- [ ] 一页一想法
- [ ] 重复元素用组件实例化，不用 map
- [ ] import 的 assets 在磁盘上存在
- [ ] ImagePlaceholder 仅用于真正需要的图
- [ ] 没有修改 `slides/<id>/` 之外的任何文件

## Anti-Patterns

- 文字墙（一页 >40 words → 拆）
- 用满画布写 body copy（需 100-160px padding）
- 垂直溢出 1080px
- `overflow: auto/scroll/hidden` 掩盖溢出
- 缩小字号 <28px
- 各页 palette 不一致
- 装 npm 包
- 创建 README.md 或 sibling .tsx
