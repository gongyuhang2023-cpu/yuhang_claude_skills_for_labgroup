# {{项目名称}}

> 带有 `[填写指引]` 标记的段落是填写说明，填写完成后删除整段指引（含本段）。

[填写指引 — 项目概述]
用一句话写明核心研究问题或分析目标。
例如："基于 16S rRNA 扩增子测序，比较抗生素处理组与对照组小鼠肠道菌群差异"。
填写后删除本指引。

## Commands

```bash
quarto render analyses/                        # Render entire workflow
quarto render analyses/01-import.qmd           # Render single step
Rscript -e "qproj::proj_install_deps()"        # Install deps from DESCRIPTION
Rscript -e "qproj::proj_check_deps()"          # Check dependency drift
```

IMPORTANT: To create new analysis steps, ALWAYS use `use_qmd()` — never hand-write `.qmd` boilerplate:
```r
# Determine the next number by checking existing files in analyses/, then:
qproj::use_qmd("02-clean", path_proj = "analyses", open = FALSE)
```

## qproj Data Layout

Each step `XX-name.qmd` maps to three directories under `analyses/data/`:

```
analyses/data/
├── XX-name/              # OUTPUT — path_target writes here (auto-cleaned when clean=TRUE)
└── 00-raw/               # INPUT zone (never auto-cleaned) — 通过网盘同步
    ├── d00-resource/     # Shared resources — all steps can read (path_resource)
    └── dXX-name/         # Private raw input — only this step reads (path_data)
```

**Sync strategy**: `data/00-raw/` syncs via cloud storage (OneDrive/Google Drive etc.) for team sharing; `data/[01-99]*/` stays local — each member re-renders to verify reproducibility. Large files (>1GB) or sensitive data use institutional storage instead.

## qproj Workflow Rules

IMPORTANT: All `.qmd` in `analyses/` follow qproj conventions. YOU MUST use the path bindings below — never construct data paths with `here::here("data", ...)` manually.

**Naming**: `00-` is reserved for `data/00-raw/`. User steps start from `01-`.

**Five path bindings** (created by each `.qmd`'s `setup` chunk via `proj_path_target()` / `proj_path_source()`):

| Binding | Type | Points to | Use for |
|---------|------|-----------|---------|
| `path_target` | function | `data/<step>/` | Writing outputs: `saveRDS(obj, path_target("result.rds"))` |
| `path_source` | function | `data/<prev>/` | Reading upstream: `readRDS(path_source("01-import", "x.rds"))` |
| `path_raw` | string | `data/00-raw/` | Raw data root (rarely used directly) |
| `path_resource` | string | `data/00-raw/d00-resource/` | Shared input: `file.path(path_resource, "ref.csv")` |
| `path_data` | string | `data/00-raw/d<step>/` | This step's private raw input: `file.path(path_data, "raw.csv")` |

**Data flow is single-direction**: `path_source()` only reads steps with *smaller* numbers (enforced by warning). `path_data` is strictly private — downstream cannot access it via any qproj API. Raw input must be processed and published to `path_target` for downstream use.

## R Coding Conventions

[填写指引 — R 编码规范]
只写 Claude 容易犯错的项目特定规则。通用 R 规范（命名空间冲突、管道风格、
snake_case 命名等）已在全局 ~/.claude/rules/r-coding-standards.md 中定义，无需重复。
典型条目示例：
- 数据容器统一用 TreeSummarizedExperiment，禁止 phyloseq
- DAA：ANCOM-BC + ALDEx2 双验证取交集
- Alpha 多样性用 mia::addAlpha()，不用已弃用的 estimateDiversity()
填写后删除本指引。

## Figure Standards

[填写指引 — 出图规范]
按目标期刊填写。如无特殊要求可采用以下 Nature 默认值直接保留：
- 尺寸：单栏 89mm / 双栏 183mm，300 dpi，PNG+PDF
- 字体：Helvetica/Arial，正文 5-7pt，面板标签 8pt 粗体小写 (a, b, c)
- 配色：ggsci NPG/Lancet（色盲友好）
- 保存：`ggsave(path_target("fig_01.png"), width = 89, height = 70, units = "mm", dpi = 300)`
填写后删除本指引（可保留上方条目）。

## Data Dictionary

[填写指引 — 数据字典]
列出关键数据文件和变量，防止 Claude 猜测列名或文件位置。格式示例：

| File | Location | Description |
|------|----------|-------------|
| sample_metadata.csv | `path_data` in step 01 | 样本元信息，含分组、批次 |
| silva_138.2.fasta | `path_resource` | SILVA 参考数据库 |

Key variables:
- DV: Shannon diversity index
- IV: treatment_group (control / antibiotic)
- Covariates: age, sex, batch
填写后删除本指引。

## Gotchas

[填写指引 — 项目特有陷阱]
记录非显而易见的行为、已知问题或特殊决策。示例：
- 样本 S12 为离群值——02-clean 中按 PI 决定剔除
- run_A 与 run_B 存在批次效应——所有模型必须纳入 batch
- 16S 数据使用 SILVA v138.2——禁止混用 Greengenes
填写后删除本指引。
