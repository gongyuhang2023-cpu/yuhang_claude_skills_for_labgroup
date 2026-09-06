# 科研叙事与内容组织（🟡 提升科研 deck 质量）

> ppt-master 引擎懂排版，不懂"生信内容怎么组织成 deck"。这里补上。萃取自 meeting-ppt-vba。

## 1. ABT 叙事骨架
科研汇报主线：**And → But → Therefore**
- **And**（背景共识）：噬菌体疗法是应对耐药的有前景策略…
- **But**（Gap/冲突）：然而宿主特异性限制了广泛应用…
- **Therefore**（方案/结果）：因此我们分离筛选了广谱噬菌体…

## 2. AE 断言-证据标题
**标题是结论，不是标签。**
| ❌ 标签式 | ✅ 断言式 |
|----------|----------|
| 实验结果 | 噬菌体 R1 表现出最强裂解活性 |
| 宿主范围测定 | 5 株噬菌体均为窄宿主范围 |
| 数据分析 | 滴度与 MOI 呈剂量依赖关系 |

> 注：ppt-master 引擎本就自带 AE 理念（每页标题是断言句），这里强化即可。

## 3. 生信典型章节结构（含 ABT 角色）
| 章节 | ABT 角色 | 典型内容 |
|------|----------|----------|
| 背景：疾病/现象 | And | BioRender 器官/菌群示意 |
| 背景：现有研究 | And | 关键文献发现 |
| 问题：Gap | But | 未解决的科学问题 |
| 方法：采样+测序 | Therefore | 样本信息 + 测序流程 |
| 方法：分析管线 | Therefore | 生信工具链 |
| 结果：多样性/组成/差异/功能/网络 | Evidence | 各类数据图(见下) |
| 讨论 | Interpretation | 机制解读 |
| 结论 | Summary | 关键发现列表 |
| 下一步 | Future | 计划时间线 |

## 4. 图表类型 → 呈现映射（什么数据用什么图，图占比）
| 数据 | 典型图 | 图占比 |
|------|--------|--------|
| Alpha diversity | 箱线图/小提琴图 | ~50% |
| Beta diversity | PCoA/NMDS 散点 | ~60% |
| 物种组成 | 堆叠柱状图 | ~55% |
| 差异物种 | 火山图/森林图 | ~60% |
| 功能注释 | 气泡图/柱状图(KEGG/COG) | ~50% |
| 网络分析 | igraph/Cytoscape 网络(宽图) | ~70% |
| 热图 | ComplexHeatmap(全宽) | ~80% |
| 生存 | Kaplan-Meier | ~50% |

> 映射到引擎：在 Strategist 阶段据此定每页 layout/图占比；这些是**真实数据图(provided)**，不是 AI 配图。

## 5. 封面 Power Words（科研钩子标题）
| 类型 | 弱 | 强 |
|------|----|----|
| 发现型 | 噬菌体宿主范围分析 | Decoding Phage-Host Interactions in the Gut Virome |
| 问题型 | 肠道菌群与哮喘 | Can Gut Viruses Drive Childhood Asthma? |
| 方法型 | 宏基因组分析流程 | Unlocking the Hidden Virome: A Metagenomic Approach |

## 6. 讲稿密度反相关（生成 speaker notes 时）
页面越空、讲稿越详：
| 页面密度 | 讲稿 |
|----------|------|
| 留白页(封面/呼吸) | 详细(3-5 句) |
| 轻叙事 | 标准(2-3 句) |
| 标准结果页 | 提示词(1-2 句) |
| 密集数据/表格 | 引导词(1 句，"请看第三行") |

> 节奏理念(冲击后呼吸/D3 连续≤3/结论前留白)与引擎自带的 page_rhythm(anchor/dense/breathing)思路一致，引擎已覆盖核心，无需另造。
