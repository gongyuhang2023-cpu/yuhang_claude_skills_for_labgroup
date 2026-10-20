# qproj 内容组织与论文映射指南

> 目的：让 Claude 在 qproj 项目里写的代码——**步骤划分合理、模块清爽、逻辑通畅**，并**天然对接可复现论文写作**（论文里每个数字都能回溯到产生它的代码）。
> 本文是"何时拆、拆到哪、怎么接论文"的判据集，每条都锚定真实项目范例 —— 下文的 `010`、`074`、`090-manuscript` 等都出自 pc047e2-HpyloriDefense（github.com/rujinlong/pc047e2-HpyloriDefense，2026-06 版，早于 qproj 0.2.0）。SKILL.md 场景 9 是速查版，需要展开时读本文。

## 一、心智模型：项目是一条可复现的有向链

- 一个 qproj 项目 = 一条 step 链：`010-import → 020-derep → … → 090-manuscript`
- 每个 step（一个 `.qmd`）= 链上一个**节点**：吃上游产出 + 自己的原始输入，吐出**带名字的产出**（`path_target()` 的表/图，或落进共享 DB 的表）
- 边 = 数据依赖：下游用 `path_source("上游步骤","文件")` 或 DB 读回
- manuscript 是最后一个节点：只**读**各 step 的产出、把数字/图**内联引用**进正文——这就是"有理有据"的物理基础

> 一句话纪律：**先想清楚"这一步产出什么、给谁用"，再动手写代码。**

## 二、何时开一个新的 `.qmd`（step 划分判据）

一个 step 应是"**一个可独立复现、产出一组语义内聚结果的分析单元**"。

**开新 step**（满足任一）：
- 产出会被**多个下游**复用（如 pc047e2 的 `010` 的 `meta_clean.csv` 被 `040`/`060` 都读）——独立成 step 让依赖显式、可缓存
- 换了**输入源 / 数据粒度 / 方法族**（如 `030` 建树 → `050` 在树上做 PCM，容器和方法完全不同）
- 有**重型外部工具**介入（geNomad / DefenseFinder / skani …），其输出值得单独 census 一步
- 对应论文里一个**独立的 Results 块 / 一张主图**（如 pc047e2 的 `074` → Figure 7 + 一段 phage-side 论证）
- 需要能**单独 render / 单独复现**，而不必跑全链

**别开新 step**（留在当前 step 加 `### Task N` 或 chunk）：
- 只是同一分析的下一个子问题、共享同一批输入
- 只是换个角度出个补充图表
- 纯粹想让"文件短一点"——step 的边界是**语义**，不是行数

> 反面判据：如果新步骤除了"读上一步 → 加工 → 立刻被下一步读"没有独立复现价值、也没人复用，它更像上一步的一个 Task，别拆。

## 三、step 内部：Task 与 chunk 的粒度

层级：`## Tasks` → `### Task N — 一句话论点` → 若干 `#| label:` 命名 chunk → **chunk 后一段结论散文**。

- **一个 Task = 论文里的一个论点 / 一段 Results。** 例：pc047e2 的 `074` Task 1 = "phage 没有专门的抗限制工具（真阴性）"。
- **一个 chunk = 支撑该论点的一个具体计算 + 一个产出**（一张表 / 一张图 / 一次落库），单一职责。例：`074` Task 1 拆成 census chunk + positive-control chunk（一个出数、一个验证工具灵敏度）。
- **chunk 后紧跟结论散文**（见 `074` census 之后那段）：用**具体数字 / 效应量**陈述该 Task 结论。这段散文就是 manuscript Results 的草稿原料，且因为紧挨着产生数据的代码，天然可核对。
- chunk 元数据：`#| label:` 用有意义的名字；缓存逐 chunk 开 —— 只给重计算 / 慢的 chunk 标 `#| cache: true`（`_quarto.yml` 不设全局 `cache`）。缓存 chunk 读文件要加 `#| cache.extra: !expr tools::md5sum(path_source(...))`，且不写文件 / 表（详见 `qproj-guide.md` 第十五节）。
- 顺序：数据读取 chunk 靠前、产出 / 落库靠后、文件末尾保留 `list-files-target` chunk。

## 四、何时把逻辑下沉到 `R/`（函数化）

理想状态：**qmd 只剩"编排 + 展示 + 叙事"，"怎么算"藏进 `R/*.R` 函数。**

**下沉到 `R/`**（满足任一）：
- 纯计算 / 绘图逻辑 **> ~15 行**（pc047e2 把功效模拟、拼图、解析器都下沉了：`run_vale_rebuttal()` / `make_fig7()` / `read_bacphlip()` / `parse_premethylation_coverage()`）
- 会被**复用**（跨 Task 或跨 step）
- 逻辑本身**值得被文档化 / 测试**（写 roxygen `@param`/`@return`，将来可加单测）
- 想让 qmd 这段"读起来像论文的方法段"，而不是一屏实现细节

**留在 qmd 就地定义**（别下沉）：
- 只在本步用一次的**胶水 helper**（pc047e2 的 `074` 就地放了 `count_fasta` / `read_tsv_safe` / `rm_func` / `%||%`）

**下沉后的规范**：
- R/ 函数里**命名空间全部显式 `::`**——这样 `devtools::load_all()` 后干净可用，不依赖 `library()` 顺序（pc047e2 的 `fig7.R` 注释明说这一点）
- **路径参数化**：把硬编码路径改成函数参数，调用方用 `path_target()` / `path_source()` / `path_data` 传入
- qmd 的 packages chunk 里 `devtools::load_all()` 已加载 `R/`，直接调用

清爽调用范式：
```r
# qmd 里只剩：调用函数 → 落图/落表 → 出 kable → 写结论散文
# 输入经 path_source() 传进去（读取进运行记录），输出目录经 path_target() 传进去
res <- run_vale_rebuttal(skani_edges_file = path_source("020-dereplication", "skani_edges.tsv"),
                         ..., out_dir = path_target("vale_rebuttal"))
knitr::kable(res$power, caption = "…")
```

## 五、step → manuscript：可复现的"有理有据"

目标：manuscript **不手抄数字**，而是**内联引用**上游 step 的产出——改了上游、重跑，论文数字自动跟着变，且每个数字可回溯。

两种落地机制（选其一，**团队/项目内统一**）：

1. **core qproj 方式（默认，无额外依赖）**：每个 step 用 `path_target()` 写关键结果为 `.rds`/`.csv`；manuscript 的 setup 建 `path_source`，用 `path_source("040-defense-census","census_matrix_clean.csv")` 读回，正文用 inline R（`` `r round(x, 1)` ``）引用。
2. **共享 DB 汇聚方式（pc047e2 采用）**：每个 step 把结果表写进共享 SQLite（pc047e2 的 `write_table_db(con, "step04_census_matrix", df)`）；manuscript 一次性 `DBI::dbReadTable(con, "stepNN_xxx")` 全读回（见 `090-manuscript` 的取数 chunk）。适合表很多、想要统一命名空间的项目。
   > 连接本身已是 core qproj：0.7 起用 `qproj::proj_db_connect()` / `proj_db_disconnect()`，取代 pc047e2 那种各项目自写的 `load_db()` / `write_table_db()`；0.12 起每步读写了哪些表进运行记录，`proj_lineage_check()` 按表判断过期，表与列的含义写进 `db-dictionary.yml`（`qproj-guide.md` 第十七节）。**仍是项目级约定的**：用不用库、表名怎么起（`stepNN_xxx`）—— 写进项目 `CLAUDE.md`；全局默认走机制 1。一张表只由一个 step 写。

**写作纪律**：
- 每个 Task 的**结论散文**在 step 里就写好（紧挨数据）；manuscript 阶段是"搬运 + 串逻辑链"，不是重新发现结论。
- 论文里每个关键数字，都应能指到"**哪个 step 的哪个产出表 / 图**"——这就是审稿人问"这个数哪来的"时的答案。
- 图同理：主图函数下沉 `R/`（如 `make_fig7()`），step 里生成到 `path_target()`，manuscript 引用该图文件。

## 六、写代码前一页速查

- [ ] 这坨逻辑属于**现有 step 的新 Task**，还是**够格开新 step**？（判据见 §二）
- [ ] 每个 chunk 是否**单一职责**、有 `#| label:`、慢的标了 `cache`（读文件的带 `cache.extra`、不写文件）？
- [ ] 每个 Task 后是否有**带具体数字的结论散文**？
- [ ] 有没有 **>15 行 / 会复用 / 值得文档化**的计算该**下沉 `R/`**？下沉函数是否 `::` 显式、路径参数化？
- [ ] 产出是否**只经 `path_target` 写**、下游**只经 `path_source`/DB 读**？（别手拼读别的步骤的 `d<step>/`；外部工具的整批输出由跑工具的步骤发布，或放 `d00-resource/`）
- [ ] 关键产出是否**可被 manuscript 引用**（落 rds/csv 或落库、命名可回溯）？
