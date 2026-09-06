---
name: ppt-master
description: |
  科研 PPT 的「科研规范层」——骑在第三方 ppt-master 美学引擎上，只管科研内容对错，不管生成。
  当用户提到 "/ppt-master"、"用 ppt-master 做组会/科研 PPT"、"科研 PPT"、"把科研规范注入 ppt-master" 时触发。
  美学/排版/出片全交给引擎(C:\Users\<你>\Tools\ppt-master)；本 skill 负责注入斜体/单位/统计/禁红绿/ABT/缩写规范，并在导出前 checklist 把关。
---

# ppt-master — 科研规范层（壳，非生成器）

> ⚠️ 这是一个**薄壳**：它**不生成** PPT，生成交给 `C:\Users\<你>\Tools\ppt-master` 的开源引擎。
> 本 skill 的唯一职责 = 把"科研内容对错"的规范注入引擎的生成流程，并在导出前审查。
> **引擎与本 skill 是两个东西**（同名但不同物）。引擎部署见本目录 `README.md`。

## 定位（一句话）

ppt-master 引擎 = 通用美学引擎（好看，但不懂科研对错）。
本 skill = 科研 PPT 的 **linter + 内容组织器**（斜体/统计/禁红绿/ABT）。两者**嫁接**使用。

## 工作流

### Step 0：确认引擎就位
```powershell
Test-Path "C:\Users\<你>\Tools\ppt-master\skills\ppt-master\SKILL.md"
Test-Path "C:\Users\<你>\Tools\ppt-master\.venv\Scripts\python.exe"
```
任一为 False → 引擎未部署，**按本目录 `README.md` 部署后再继续**。

### Step 1：加载科研规范（按需读）
- `references/scientific-norms.md` —— 🟢 斜体/单位/统计/禁红绿/缩写（**生成全程必须遵守**）
- `references/bio-structure.md` —— 🟡 ABT 叙事 + 生信章节结构 + 图表-布局映射 + 封面 Power Words
- `references/glossary.md` —— 菌名/缩写词典（按内容查）

### Step 2：内容规划（注入科研叙事）
组织 deck 内容时套用 `bio-structure.md`：
- **ABT 叙事**：And(背景共识) → But(Gap) → Therefore(方案/结果)
- **生信章节结构** + **图表-布局映射**（什么数据用什么呈现）
- 标题用 **AE 断言句**（"噬菌体 R1 表现出最强裂解活性"，非"实验结果"）

### Step 3：驱动引擎生成（读引擎的 SKILL.md 跑它的 pipeline）
读 `C:\Users\<你>\Tools\ppt-master\skills\ppt-master\SKILL.md`，按它的 Strategist→Executor→Export 走，但**在 Strategist 八项确认里注入以下科研约束**：
- **配色**：交给引擎自由发挥（用户认可其美学）；**但禁止红/绿语义编码**（色盲，见 norms）——正负趋势改用蓝/橙或引擎主色的深浅。
- **字体/标题**：标题 AE 断言式；正文规范见 norms。
- **图片**：科研用**真实数据图**(R/Python 出图)，`image_usage` 选 `provided`/`user`，**不要** AI 配图。
- **公式**：用引擎的 formula 渲染（mixed 策略）。

### Step 4：导出前 checklist（本 skill 的把关核心）
按 `scientific-norms.md` 末尾清单逐项审 SVG/内容：
- [ ] 菌名/基因名斜体、蛋白正体、*P*/*n* 斜体
- [ ] 单位带空格(10 μL)、温度 37 °C、科学计数法用 ×、μL 非 ul
- [ ] 精确 P 值(非 P<0.05)、报效应量、高通量报 FDR/BH、图标 n
- [ ] **无红绿语义编码**
- [ ] 缩写首次展开(HGT/PCoA/MAG…)

### Step 5：真实数据图配色提醒
⚠️ 既然用引擎的配色，R/Python 出的数据图配色**要重新匹配引擎本 deck 选定的色板**（从 `spec_lock.md` 取 primary/accent），否则数据图与 slide 美学割裂。旧的莫兰迪 `bio_palette` 作废。

## 不做什么
- ❌ 不自己写 SVG / 不自己出 pptx（那是引擎的事）
- ❌ 不碰引擎的配色/布局体系（用户认可其美学）
- ❌ 不把引擎的 1GB 仓库搬进 skills（见 README 架构说明）
