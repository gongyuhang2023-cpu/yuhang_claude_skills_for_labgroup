---
name: ppt-master
description: |
  科研 PPT 的规范层，骑在第三方 ppt-master 引擎上，不自己生成。管科研汇报的页面结构、结果呈现、现场讲稿和排版规范，导出前把关。
  触发："/ppt-master"、"用 ppt-master 做组会/科研 PPT"、"科研 PPT"、组会汇报、progress report、journal club 的幻灯片或讲稿、"把科研规范注入 ppt-master"。
---

# ppt-master — 科研规范层（壳，非生成器）

> ⚠️ 这是一个**薄壳**：它**不生成** PPT，生成交给开源引擎 [ppt-master](https://github.com/hugohe3/ppt-master)。
> **引擎与本 skill 是两个东西**（同名但不同物）。引擎部署见本目录 `README.md`。

本目录是科研适配层，升级上游引擎时不得覆盖。对接的引擎版本为 **6.7.2**（`604ecdec`）；科研规范、词典和内容组织参考保持独立。

## 个人习惯区

先看 `references/` 下有没有 `*.local.md`。那是使用者自己的习惯和环境，例如引擎装在哪、用哪些标志、封面怎么放、个人素材库在哪。它不随本 skill 分发，升级 skill 时保留，不要删。和公共规则冲突时，以它为准；没有就按公共规则走。新的个人习惯也记在这里，不改公共规则。

## 定位

引擎负责美学、排版和出片，不懂科研汇报。本 skill 管引擎不管的四件事：

| 管什么 | 在哪 |
|---|---|
| 汇报骨架、页面单元、证据呈现、实验室版式基线 | `references/talk-structure.md` |
| 现场讲稿 | `references/talk-notes.md` |
| 斜体、单位、统计、禁红绿、缩写 | `references/scientific-norms.md`、`references/glossary.md` |
| 个人素材（标志、封面）和本机环境 | 个人习惯区写明位置；素材本身不在本目录 |

## 工作流

### Step 0：确认引擎就位

引擎目录以个人习惯区写的为准，下面用 `<引擎目录>` 代替。

Linux / macOS：
```bash
test -f <引擎目录>/skills/ppt-master/SKILL.md
<引擎目录>/.venv/bin/python -c "import pptx,svglib,PIL,numpy; print(pptx.__version__)"
```

Windows：
```powershell
Test-Path "<引擎目录>\skills\ppt-master\SKILL.md"
Test-Path "<引擎目录>\.venv\Scripts\python.exe"
```
任一检查失败 → 引擎未部署，**按本目录 `README.md` 部署后再继续**，并把引擎目录记进个人习惯区。

### Step 1：定类型，读规范

- 判断汇报类型：progress report、journal club 或其他科研汇报；确认时长，以及是否要中英两版。
- 读 `scientific-norms.md`（生成全程遵守）和 `talk-structure.md`；要生成讲稿时读 `talk-notes.md`；`glossary.md` 按内容查。
- 只写讲稿、改讲稿或审已有 deck 时，直接按这三份规范做，不走引擎。

### Step 2：盘点材料和素材

- 按 `talk-structure.md` §0 盘点真实材料，建 `facts.md` 事实表（走引擎时放工程目录，只写讲稿或只做规划时放在讲稿或规划文件旁边）；缺的数字标 `[MISSING]`，不估计。
- 个人习惯区写了素材库时读它的清单：标志按个人习惯区和 `talk-structure.md` §4 放；清单里有封面素材时，问用户一次用不用。封面由底图加几张小图组成时，在 PPT 里分层放：底图一张，每张小图各是一个独立图片对象，让用户能在 PowerPoint 里右键"更改图片"自己换；不用合成好的整张图。合成图只用于需要一张图的场合，比如 HTML 幻灯片或预览。没有素材库就跳过，不报错，交付时提一句。
- 用到的素材复制进本次 deck 的工程目录，不复制进任何 skill 目录。

### Step 3：内容规划（注入引擎的两阶段规划）

读 `<引擎目录>/skills/ppt-master/SKILL.md`，用引擎自己 venv 里的 Python（Linux / macOS 是 `.venv/bin/python`，Windows 是 `.venv/Scripts/python.exe`）。以对应解释器执行其 attribution guard，再按 `workflows/routing.md` 选择工作流。普通生成在 Default 的两阶段规划确认中注入下列约束；用户明确要求 Quick 时，在 Quick 的规划中同样落实，不套用旧版"八项确认"流程：

- Stage 1：沟通合同写明听众、汇报类型、每个项目或每个问题的核心信息。
- Stage 2：叙事骨架按 `talk-structure.md` §1，作为用户提供的结构保留；风格按 §4 的实验室版式基线，作为用户指定；讲稿开启时，§X 写入 `talk-notes.md` 的要求。
- §IX 每页写明：本页目的、标题、主证据文件、结论条、脚注、强调标记、口头要点、分配时长；数字引用 `facts.md` 的编号。
- **标题链先审**：Stage 2 把 `refine_spec` 设为 true。Design Spec 写好后，在 refine-spec 的审阅里先给用户看标题链（每页一行：页号、本页目的、标题、主证据文件），认可后再生成页面。用的是引擎原有的审阅步骤，不另加一轮确认。
- **图片**：结果用真实数据图或论文原图，`image_usage` 选 `provided`/`user`，**不要** AI 配图。
- **配色**：**禁止红/绿语义编码**（色盲，见 norms），正负趋势用蓝/橙或主色深浅。
- **公式**：使用引擎当前的公式渲染契约，保持科研符号与单位规范；不再使用已移除的旧版 `mixed` 策略字段。

用户要求动态内容时，读引擎 `references/animations.md`，按 `workflows/stages/customize-animations.md` 对接对象动画和 Morph。Morph 的前后状态在页面规划时设计；不把新版默认关闭的对象动画误当作已开启。沿用用户明确授权，不为升级检查或既定选择重复发起确认。

### Step 4：生成讲稿

讲稿内容按 `talk-notes.md`；文件格式照引擎 `executor-notes.md`。两者冲突时，内容以 `talk-notes.md` 为准。

### Step 5：导出前检查（本 skill 的把关核心）

- 逐项过三份清单：`scientific-norms.md`、`talk-structure.md`、`talk-notes.md` 末尾。不适用的项写 N/A 和一句理由，不为了勾上而补分析；规划阶段的 `[MISSING]` 记为待办。
- 看实际渲染，不只看 SVG：字号、字数、连线按 `talk-structure.md` 的清单逐项量；再看图件大小、连线和箭头是否穿字、强调标记是否压字。技术检查通过不等于演示质量通过。

### Step 6：真实数据图配色提醒

R/Python 出的数据图配色要重新匹配本 deck 选定的色板（从 `spec_lock.md` 取 primary/accent），否则数据图与页面割裂。图保持白底。旧的莫兰迪 `bio_palette` 作废，不再使用。

## 不做什么

- ❌ 不自己写 SVG / 不自己出 pptx（那是引擎的事）
- ❌ 不改引擎代码；风格只通过引擎规划阶段的用户指定传入
- ❌ 不把引擎的 1GB 仓库搬进 skills（见 README 架构说明）
- ❌ 不把私人素材复制进任何 skill 目录、Git 仓库或共享位置
- ❌ 不把个人习惯写进公共规则文件；个人的记在 `references/*.local.md`
