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
Rscript -e "qproj::proj_doctor()"              # Static check of cache / database settings (read-only)
Rscript -e "qproj::proj_lineage_check()"       # Steps whose recorded inputs changed since they last ran
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
├── XX-name/              # OUTPUT — path_target writes here (emptied only when clean = TRUE)
└── 00-raw/               # INPUT zone (never auto-cleaned) — 通过网盘同步
    ├── d00-resource/     # Shared resources — all steps can read (path_resource)
    └── dXX-name/         # Private raw input — only this step reads (path_data); create it by hand
```

Each render also writes a run record to `.qproj/provenance/` at the project root (`latest/<step>.json` is committed; `runs/` stays local).

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

**Data flow is single-direction**: `path_source()` reads only steps earlier in render order; reading a later step aborts the render (`qproj_order_error`). Computed products MUST be published via `path_target` and read downstream via `path_source("step", "file")` (a two-arg call on the setup-created closure — NOT `qproj::proj_path_source(step, file)`). Reads through `path_source()` are recorded (with sha256) in the step's run record; that is what `proj_lineage_check()` uses.

**`path_data` is private to its step.** Do NOT read another step's `d<step>/` via `here::here(path_raw, "dNN-name", ...)`: nothing stops it at run time, but `qg bypass` flags it and the read escapes the run record, so lineage checks cannot see it. Instead, the owning step publishes the data via `path_target`, or truly shared external inputs (reference DBs, whole tool outputs) go to `d00-resource/`.

**Step identity**: `params$name` must equal the file name without `.qmd`, or the render aborts (`qproj_identity_error`). Create steps with `use_qmd()`; never copy an existing `.qmd` as a new step.

**Chunk cache is opt-in per chunk**: `_quarto.yml` sets no `cache` key. Put `#| cache: true` only on slow chunks. A cached chunk that reads a file needs `#| cache.extra: !expr tools::md5sum(path_source("step", "file"))`; a cached chunk must not write files or tables (on a cache hit the write does not happen). Run `proj_doctor()` after changing cache settings.

**Project database**: open it with `con <- qproj::proj_db_connect()` (`create = TRUE` only in the step that builds it) and close with `qproj::proj_db_disconnect(con)`; one table is written by exactly one step.

**Change impact check**: When a modification touches cross-step data flow (renaming output files, changing output format, reordering steps, removing a `saveRDS` call), first check the blast radius before editing downstream files:
```bash
bash .qproj/graph/qg status             # exit 0 current / 3 out of date / 2 unknown — rescan first if not 0
bash .qproj/graph/qg impact <step-id>   # transitive downstream — full blast radius of changing <step-id>
bash .qproj/graph/qg deps <step-id>     # transitive upstream — everything <step-id> depends on
```
If `.qproj/graph/qproj-graph.json` does not exist or is out of date, run `Rscript -e "qproj::proj_scan_graph()"` from the project root (add `formats = c("json", "md")` where `visNetwork` is not installed). On Windows, do not paste the rescan line `qg` prints — its MSYS path (`/c/...`) is not a path R understands. Run `bash .qproj/graph/qg --help` for the full subcommand set (`impact`/`deps` are transitive; `downstream`/`upstream` are one-hop). Routine single-step edits (tweaking a plot, adjusting a parameter) do NOT require this check.

## Code Organization

The project is a reproducible chain: each `XX-name.qmd` step reads upstream products, computes, and publishes named outputs; the manuscript reads those outputs and cites the numbers inline. Organize code so results map cleanly onto the paper.

- **New `.qmd` vs. new Task**: open a new step when its output is reused by multiple downstream steps, when the input source or method family changes, when a heavy external tool is involved, or when it maps to a distinct Results block / main figure. Otherwise add a `### Task N` to the current step — do NOT split a step just to shorten a file.
- **Granularity**: one Task = one claim / paragraph of Results; one chunk = one computation + one output (table / figure / DB write), single responsibility, with a meaningful `#| label:` and opt-in `#| cache: true` on slow chunks only. Follow each Task with a short prose conclusion stating the concrete numbers — that prose is the Results draft.
- **Sink logic into `R/`** when it is pure computation / plotting >~15 lines, reused, or worth documenting: write a roxygen-documented function with explicit `::` namespaces and path arguments, load it via `devtools::load_all()`, and keep the `.qmd` to orchestration + display + narrative. Keep one-off glue helpers inline in the chunk.
- **Step → manuscript**: publish key results via `path_target` (or a shared DB) and have the manuscript read them back via `path_source` / DB and cite inline, so re-rendering keeps every number in sync and traceable to the step that produced it.

## R Coding Conventions

[填写指引 — R 编码规范]
只写 Claude 容易犯错的项目特定规则。通用 R 规范（命名空间冲突、管道风格、
snake_case 命名等）已在全局 ~/.codex/rules/r-coding-standards.md 中定义，无需重复。
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
