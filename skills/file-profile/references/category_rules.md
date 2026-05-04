# 文件分类规则

Claude 生成描述时参考以下规则自动分配标签。

## 按扩展名分类

| 扩展名 | 标签 | 说明 |
|--------|------|------|
| `.R`, `.r` | 脚本 | R 脚本 |
| `.qmd`, `.Rmd` | 文档, 分析 | Quarto/R Markdown 分析文档 |
| `.py` | 脚本 | Python 脚本 |
| `.sh`, `.bash` | 脚本 | Shell 脚本 |
| `.csv`, `.tsv`, `.xlsx` | 数据 | 表格数据 |
| `.fastq`, `.fastq.gz`, `.fasta` | 原始数据 | 测序数据 |
| `.rds`, `.RData` | 中间数据 | R 序列化数据 |
| `.h5`, `.hdf5` | 中间数据 | 大型数据存储 |
| `.png`, `.jpg`, `.pdf`, `.svg`, `.tiff` | 图表 | 图像/图表文件 |
| `.md`, `.txt`, `.docx` | 文档 | 文档文件 |
| `.yaml`, `.yml`, `.json`, `.toml` | 配置 | 配置文件 |
| `.html` | 输出 | 渲染输出 |
| `.bib` | 参考文献 | 文献引用数据库 |

## 按文件夹名分类

| 文件夹名模式 | 标签 | 说明 |
|-------------|------|------|
| `raw`, `raw_data` | 原始数据 | 不可修改的源数据 |
| `data`, `processed` | 数据 | 处理后的数据 |
| `scripts`, `src`, `code`, `R` | 脚本 | 代码文件 |
| `output`, `results`, `out` | 计算结果 | 脚本产出 |
| `figures`, `figs`, `plots` | 图表 | 可视化输出 |
| `docs`, `documentation` | 文档 | 项目文档 |
| `config`, `conf` | 配置 | 配置目录 |
| `tmp`, `temp`, `scratch` | 临时 | 可清理的临时文件 |
| `references`, `refs`, `literature` | 参考资料 | 文献和参考材料 |
| `protocols`, `methods` | 实验方案 | 实验操作规程 |

## 按文件名模式分类

| 模式 | 标签 |
|------|------|
| `README*`, `CLAUDE*` | 文档 |
| `LICENSE*` | 项目管理 |
| `*_test.*`, `test_*` | 测试 |
| `*config*`, `*setting*` | 配置 |
| `Makefile`, `Dockerfile`, `docker-compose*` | 构建 |
| `.gitignore`, `.Rprofile` | 项目管理 |
| 以数字前缀开头（如 `01_`, `02_`） | 流程步骤 |
