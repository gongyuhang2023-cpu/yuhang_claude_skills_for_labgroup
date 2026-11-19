# 查公共工具与自助安装

## 先查公共工具

先运行 `python scripts/advise.py rules`，看服务器 `/ai.md` 对这个工具有没有专门安排。iPHoP 等有专门安排的工具按服务器规则处理。流程涉及把用户数据放进共享盘时，先征得用户同意。

没有专门安排时，再走下面的通用流程。用户说「申请安装」或「请管理员装」时，告诉用户缺工具可以自己建项目环境，缺数据库可以自己装进 NAS 公共库，都不用等管理员。然后按工具或数据库的流程继续。

`python scripts/advise.py software find <工具名>` 按包名、环境名和命令名搜索公共工具。
`tools` 会列出匹配的包、版本和常用命令。也可以在面板「公共资源 → 公共工具」里搜。
安装前，在 Spark 上以用户身份运行 `which -a <工具>` 查实际命令。它能查到 PATH 上的个人安装和公共工具，`software find` 的工具清单只覆盖公共区。发现已有个人版本时，先核对它是否满足需求。
用 `--arch` 同时查询 Conda 的构建。公共工具没有匹配时会自动查询构建。

## 用已有的工具

登录后 `/opt/bio/bin` 已在 PATH，直接敲命令。用 `which -a <命令>` 查看实际用的是哪一份。
`gen-sbatch` 默认生成登录 shell 脚本，也可以直接用命令名。cron 等非登录环境可用 `/opt/bio/bin/<命令>`。
需要固定版本时，把依赖写进项目环境。

## 缺工具时建项目环境

临时试用也走项目环境。相同的配方可以复用已构建环境，版本会写进锁文件。

1. 看 `software find` 的 `arch` 和 `availability`，确定要加入配方的依赖。Conda 有 `linux-aarch64` 构建时选对应的 Conda 包。
   只有 `noarch` 时，包本身适用于 ARM64，依赖仍以 `/usr/local/bin/pixi lock` 能否解析成功为准。若因缺少 ARM64 构建而解析失败，把缺少的依赖告诉用户。
   如果 Conda 查询有失败，且没有渠道查到构建，先用 `software find <名> --arch` 重查。重查仍失败时，到失败渠道的 Anaconda 项目页核对包名和构建。仍无法确认时，告诉用户哪些结果未知，等确认后再选依赖。
   各 Conda 渠道都明确没有可用构建时，才看 `pypi`。PyPI 的同名项目可能是另一个工具。先核对 `summary` 和主页或项目链接，确认是用户要的工具，并核对 wheel 支持的 Python 版本。
   若只找到旧版 wheel，按返回的版本选择。env-store 接受 wheel，拒收 sdist 和 Git 依赖。没有可用构建就把查询结果告诉用户。
2. 对已存在的 Spark 项目目录运行 `env init --dir <项目目录>`。缺配方时，给用户看预览，确认后用 `env init --dir <项目目录> --yes` 写最小配方并生成初始锁。已有配方就沿用。目录缺失时先确认 Spark 项目落点，目录就绪后重跑。
3. 在 Spark 项目目录用 `/usr/local/bin/pixi add --no-install <包名>` 更新配方和锁。PyPI 依赖用 `/usr/local/bin/pixi add --no-install --pypi <包名>`。把示例 `tasks.smoke` 换成真正检查工具的短命令，例如 `<工具> --version`。已有真实测试时沿用。编辑后运行 `/usr/local/bin/pixi lock` 更新锁。把 `pixi.toml`、`pixi.lock` 和项目代码放在一起。
4. 用 `env build --dir <项目目录>` 看构建计划，用户同意后加 `--yes --wait` 构建。

完整参数与示例见 [项目环境](project-envs.md)。使用 ws 时按 SKILL.md 的入口规则用 `ws_forward.py`。

## 在作业里用项目环境

用 `gen-sbatch --env <项目目录>` 加上任务所需的 `--name`、`--workdir`、资源和 `--command` 等参数生成脚本，`--workdir` 写服务器上的绝对路径。脚本会激活锁定的环境，计算命令直接用环境里的工具。
用 `env check <脚本>` 核对，然后按提交流程让用户确认并用 `submit --script <脚本>` 交作业。
