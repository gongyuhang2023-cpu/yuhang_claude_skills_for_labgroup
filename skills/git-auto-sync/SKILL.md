---
name: git-auto-sync
description: 按用户要求提交代码或同步项目到 Gitea/GitHub，支持只读检测、选择单个平台或双推、首次接入 Gitea，以及维护项目目录。适用于 Windows、macOS 和 Linux 的原生 Git 环境。
---

# Git Auto Sync

把当前项目同步到用户选定的远程。实验室项目首次接入时推荐 **Gitea 个人私有仓库**；已有 GitHub 工作流继续尊重，不自动换平台或双推。

依赖 Python 3.10+ 和 Git，复用当前用户的 Git 凭据助手或 SSH key。脚本不保存账号、令牌，不依赖某个人的电脑或 Windows 桌面代推。本文 `{SKILL_DIR}` 要替换为本技能的**实际安装目录**，不要照搬他人的路径；使用系统可用的 `python` 或 `python3`。

## 安装 / 更新之后

安装技能不等于授权提交或推送。先读项目 `AGENTS.md` / `CLAUDE.md` 和已有 `项目目录.md`，只读检测用户要同步的项目：

```bash
python "{SKILL_DIR}/scripts/sync.py" --detect
```

未指定项目时先问目录，不拿安装用的技能库当用户项目。没有 Gitea 远程时，问用户是否需要首次接入；需要则读 [首次使用 Gitea](references/gitea-first-use.md)，带他完成账号、凭据和远程配置。已经接入可跳过；只想用 GitHub 也直接尊重。更新保留用户已有配置、仓库 remote、`sync.targets` 和凭据，不替换个人定制版。

## 日常流程

1. 读 `--detect` 的分支、变更、remote URL、`sync_targets` 和 `target_error`。核对本次文件范围及暂存区。默认同步会暂存项目内全部未忽略变更；含其他工作时，直接用 Git 精确暂存和提交，再用 `--push-only` 推送。
2. 用户只说“提交”时只做本地 `git add` / `git commit`，不推送。明确要求同步或推送时才执行下面的推送命令，已有授权不重复问。
3. 没有远程或平台选择不清楚时先配置。多个 remote 且未设置 `sync.targets` 时脚本会在暂存前停下；单个 remote 会沿用它。URL 用于识别平台，不靠 `origin`、`gitea` 等名字猜。
4. 若存在 `项目目录.md`，同步前可运行 `scripts/update_directory.py` 更新其时间戳和变更日志；索引正文按本次文件变化维护，详见 [项目目录维护](references/project-directory.md)。该脚本缺文件或失败时会跳过 / 警告，不阻止 Git 操作。科研实验项目若有既定一致性维护规则，对本次 protocol、实验计划变化提醒关联更新；纯分析项目无需套用。
5. 按用户选定目标执行，逐个报告远程结果。任何目标失败都不称全部成功；不强推，不撤回已经推成功的远程。

## 选择推送目标

```bash
# 临时只推一个平台；gitea 识别实验室 git.denglab.xyz
python "{SKILL_DIR}/scripts/sync.py" --to gitea "更新分析脚本"
python "{SKILL_DIR}/scripts/sync.py" --to github "更新分析脚本"

# 已经提交好，只推送；不会把当前尚未提交的修改带上
python "{SKILL_DIR}/scripts/sync.py" --push-only --to gitea
python "{SKILL_DIR}/scripts/sync.py" --push-only --to github

# 按日常目标同步 / 只推已有提交
python "{SKILL_DIR}/scripts/sync.py" "更新分析脚本"
python "{SKILL_DIR}/scripts/sync.py" --push-only
```

`--to denglab` 是 `--to gitea` 的兼容写法，不表示组织仓库。`--to` 不修改 `sync.targets`，没有新变更时仍会推送已有提交。同一平台有多个 remote 时，先用下面的持久配置选实际 remote，不擅自选其中一个。

**持久选择**使用仓库本地 `sync.targets`，值是 `--detect` 中实际存在的 remote 名，以下名字只是示例：

```bash
git config --local sync.targets "gitea"          # 日常只推这个 remote
git config --local sync.targets "github"         # 日常只推这个 remote
git config --local sync.targets "gitea github"   # 用户选择双推才设置
```

添加 Gitea 不会覆盖现有 GitHub 的 remote，也不会自动改变默认目标。已有设置含不存在的 remote、一个 remote 配了多个 push URL、或 detached HEAD 时，脚本先停止并说明原因。

## 失败与边界

- 认证失败：在**实际运行 Git 的机器和账号**中完成登录，按首次使用文档配置原生凭据；脚本不启动别人的桌面会话。
- 远程有新提交 / 分支受保护：保留本地提交，检查项目拉取、合并或 PR 规则；不自动 rebase、强推或绕过保护。
- 无参数执行会提交并推送，不拿它来检查安装。`--help` 和非法参数没有 Git 副作用。
- 本共享版不自动建库，不提供 `--setup`、`--fetch` 或 `--pull`。新仓库按首次使用文档在网页建空仓库；获取 / 合并远程变更按项目规则使用原生 Git。
