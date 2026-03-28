# AI 写作痕迹速查表

> 基于 Humanizer Skill 整理，供论文自查使用。
> 核心原则：**问题不在单个词，而在模式、频率和分布。**

---

## 一、RED 级：必须替换的词句

这些词句在高质量人类论文中几乎不出现，是强 AI 信号。

### 1.1 装饰性词汇

| AI 用词 | 建议替换 |
|---------|---------|
| delve into | examine / investigate |
| shed light on | clarify / reveal |
| explore the intricacies of | examine |
| paradigm-shifting | （删除或具体描述变化） |
| unraveling | characterizing / identifying |
| myriad | many / numerous |
| paramount | important / essential |
| cutting-edge | recent / advanced |
| indispensable | necessary / essential |
| harness the power of | use / apply |
| tapestry / indelible mark | （删除） |
| groundbreaking（比喻义） | （删除或用具体数据说明创新） |

### 1.2 夸大重要性的套话

| AI 套话 | 处理方式 |
|---------|---------|
| pivotal moment in the evolution of | 删除，直接陈述事实 |
| enduring testament to | 删除 |
| represents a major shift | 用数据说明变化幅度 |
| evolving landscape | 改为 field / area |
| in today's rapidly evolving world | 删除 |
| it goes without saying | 删除 |
| there is no doubt that | 删除 |
| of paramount importance | important |
| far-reaching implications | 具体说明什么影响 |

### 1.3 AI 句式开头

| 直接删除或改写 |
|---------------|
| It is worth noting that → 直接写内容 |
| It is important to note that → 直接写内容 |
| It is evident that → 直接写内容 |
| It is imperative that → 直接写内容 |
| In the current scholarly discourse → 删除 |

### 1.4 聊天机器人残留

| 必须删除 |
|---------|
| I hope this helps / Of course! / Certainly! |
| Great question! / Would you like me to... |
| Here is a comprehensive overview |
| As of [date] / Up to my last training update |

---

## 二、YELLOW 级：控制频率的词汇

这些词本身合法，但 AI 过度使用导致它们成为信号。

| 词/短语 | 上限（每 1000 词） | 注意事项 |
|---------|-------------------|---------|
| Furthermore / Moreover / Additionally | 1（三者合计） | 不得连续 2 段以此开头 |
| However / Nevertheless / Nonetheless | 1.5（合计） | 交替用 but, yet, still |
| Therefore / Consequently / Hence / Thus | 1（合计） | 常可删除，直接写结论 |
| In conclusion / To summarize | 0.2 | 全文仅末段用一次 |
| This study aims to / This paper presents | 0.2 | 摘要或引言用一次，不重复 |
| crucial / critical / vital / pivotal / key | 2（合计） | 过度使用=一切都同等重要 |
| significant（非统计义） | 1 | 留给统计显著性语境 |
| highlight / underscore / emphasize | 1.5（合计） | |
| enhance / improve / optimize | 1.5（合计） | |
| novel / innovative / unprecedented | 0.5 | 仅真正创新时使用 |
| comprehensive / extensive / thorough | 1（合计） | |
| robust | 0.5 | 统计语境可用；通用夸奖是 AI 信号 |
| facilitate / foster / cultivate | 0.5（合计） | |
| landscape / realm / arena（抽象名词） | 0 | 改为 field 或 area |
| interplay / synergy | 0.3 | |
| multifaceted / diverse / various | 1（合计） | |
| elucidate / delineate | 0.3（合计） | 用 explain / describe |

---

## 三、GREEN 级：学术白名单（不要误杀）

以下词常被 AI 检测器误标，但它们是正常学术用语：

- **方法类**：analyze, evaluate, assess, validate, implement, quantify, characterize, investigate, examine, determine
- **结果类**：indicate, suggest, demonstrate, reveal, show, confirm, support, exhibit
- **结构类**：although, while, whereas, despite, given that, in contrast, specifically, namely, respectively
- **模糊限定（科学必需）**：may, might, could, possibly, potentially, likely, appear to, seem to, tend to
- **统计类**：significant (带 p 值), correlation, association, regression, variance, confidence interval

---

## 四、10 类结构性 AI 模式

### S1. 句长均匀（最强 AI 信号）

**特征**：连续 5+ 句子长度相近（都在 15-25 词）。

**修复**：刻意混合短句（8-12 词）和长句（25-40 词）。

### S2. 段首公式化连接词

**特征**：>30% 段落以 Furthermore / Moreover / Additionally 开头。

**修复**：直接用主题句开头，如 "Tumor volume decreased by 45%..."

### S3. 三连词组（Rule of Three）

**特征**：AI 强迫性地三个一组。

> ~~improved stability, enhanced bioavailability, and reduced toxicity~~
> → improved both stability and bioavailability. Toxicity was also reduced...

### S4. 系动词回避

**特征**：用 serves as / stands as / represents / constitutes 替代简单的 is。

> ~~This approach serves as a promising strategy~~ → This approach is a promising strategy

### S5. Not only...but also 句式

> ~~not only improved drug loading but also enhanced cellular uptake~~
> → improved both drug loading and cellular uptake

### S6. 同义词循环

**特征**：nanoparticles → nanocarriers → nanosystems → formulations，不自然地轮换。

**修复**：统一用一个术语，用句式变化代替词汇变化。

### S7. 破折号过多

**上限**：每 1000 词不超过 2 个 em dash（—），多数改为逗号或括号。

### S8. 虚假范围

> ~~range from drug delivery to gene therapy~~ → include drug delivery and gene therapy

### S9. 模糊限定堆叠

> ~~could potentially be argued that this might possibly suggest~~ → These data suggest

### S10. 连字符过度一致

人类写作中 well-known / well known 会不一致，AI 则完美统一。名词后（"the method is well established"）应去掉连字符。

---

## 五、各章节 AI 模板与人类写法对比

### Abstract

| AI 模板 | 人类写法 |
|---------|---------|
| [宏观背景]. [This study investigates X]. [Results showed significant improvement]. [This work contributes to a deeper understanding of Y]. | [具体问题 1-2 句] → [做了什么+方法创新] → [定量关键结果] → [精确意义] |

### Introduction

| AI 模板 | 人类写法 |
|---------|---------|
| [跨越百年的宏大背景]. [Study A found X. Furthermore, Study B found Y. Moreover, Study C found Z.] [Despite numerous studies, a gap remains...] [This study aims to bridge this lacuna...] | [Hook：具体现象/矛盾] → [分析性文献综述，层层推进到 gap] → [精确 gap + 为什么重要] → [我们的具体做法] |

**禁用**：bridge this lacuna, fill this gap, address this void, has attracted increasing attention

### Results

| AI 模板 | 人类写法 |
|---------|---------|
| As shown in Figure 3, the treatment group showed a significant decrease (p < 0.05). | Tumor volume decreased by 45% (mean diff: 120 mm³, 95% CI: 85-155, p = 0.003; Figure 3). |

**要点**：先写发现再引图；报效应量+CI，不只报 p 值。

### Discussion

| AI 模板 | 人类写法 |
|---------|---------|
| Our findings are consistent with previous research. This study has several limitations. Future research should explore... In conclusion, this study sheds light on... | [主要发现+即时解读] → [与具体文献比较，解释为何一致/不一致] → [具体局限如何影响解读] → [下一步具体实验] |

**禁用**：sheds light on, paves the way for, opens new avenues, the future looks bright

---

## 六、自查流程（Checklist）

1. **RED 扫描**：全文搜索上述 RED 词句，全部替换
2. **YELLOW 计数**：统计频率，超标的替换或删除
3. **句长检查**：任意连续 5 句是否长度雷同？
4. **段首检查**：连续段落是否用同类连接词开头？
5. **三连检查**：是否频繁出现 "A, B, and C" 三连结构？
6. **各章节对照**：是否落入上述 AI 模板？
7. **反 AI 终审**：通读全文，问自己——"审稿人会怀疑哪里是 AI 写的？"

---

*整理自 Claude Code Humanizer Skill v3.0 | 2026-03-28*
