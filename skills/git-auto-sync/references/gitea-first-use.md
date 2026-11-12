# 首次使用实验室 Gitea

当用户还没有 Gitea 远程、没有登录凭据，或明确要求首次接入时读本页。先看已有配置，只补缺项。安装、检查凭据和创建远程都不代表已经获准推送当前项目。

## 1. 登录自己的账号

从 Spark Dashboard 的 Gitea 入口打开，或访问 <https://git.denglab.xyz>。已有账号直接登录；没有账号时按页面提供的入口注册，若没有开放注册或需要审批，请找实验室管理员开通。

查看个人主页，确认 **Gitea 实际用户名**。它不一定等于服务器的 SSH 用户名，也不能使用技能作者的账号。网站打不开时先确认地址及实验室要求的网络接入方式，不自行改网络配置。

## 2. 选择仓库位置，建立空远程

先告诉用户选项，再沿用其选择：

- **推荐：个人名下 + Private**，适合日常课题代码；普通成员访问由仓库权限决定，实例管理员仍能管理服务器。
- 需要组内共同维护时，再选有权限的组织及团队；不要默认放进组织，不假定所有组织成员都必然有同样权限。
- 选择 GitHub 的用户继续用 GitHub；双推意味着两边都有副本，须由用户明确选择。

用户确定位置、名称和可见性后，在网页新建仓库。已有本地代码时，**不要初始化 README、.gitignore 或 License**，以免远程先产生另一段历史。已有远程仓库则直接使用，不重复新建。

从仓库页面复制实际 clone URL。可以选择 HTTPS，或站点提供且已配置好的 SSH URL。SSH 主机、端口以页面为准，不假定它与登录 Spark 的 SSH 地址相同。Git 同步只放本次要版本管理的代码和文档；数据、模型、环境目录按项目已有 `.gitignore` 和约定处理。

## 3. 在运行 Git 的机器上配置认证

先看已有凭据助手：

```bash
git config --show-origin --get-all credential.helper
```

有可用配置就复用。Windows 常用 Git Credential Manager，macOS 可用 Keychain，Linux 使用已安装的 credential helper。不要为装技能覆盖用户的全局 Git 配置。

**HTTPS**：在网页个人设置 **Settings → Applications → Manage Access Tokens** 创建令牌，按任务选仓库读写权限；令牌只显示一次，保存在用户自己的密码管理工具中。具体入口和权限由 [Gitea 官方说明](https://docs.gitea.com/development/api-usage/) 解释。用户在自己的交互终端或凭据助手提示中输入实际用户名，并把令牌作为密码输入；不要放进 clone URL、聊天、脚本或 Git 仓库。

Linux / 服务器无可用凭据助手时，先让用户选择适用方式：可用的系统钥匙串，或经用户接受后使用仓库本地的 `git config --local credential.helper store`。`store` 默认把凭据明文写入用户 home 的 `.git-credentials`，需要确认文件仅本人可读；它是可选方案，不自动启用。临时 `cache` 到期后需要重新登录。

**SSH**：把用户自己的公钥添加到 Gitea 账号的 SSH Keys，复用已有 key / ssh-agent；不复制他人的私钥。站点未提供 SSH clone URL 就使用 HTTPS。

配置要在运行本技能的机器完成：电脑登录成功不等于服务器上的 Git 已登录。脚本禁止交互等待，因此第一次凭据输入在用户终端完成，完成后 AI 再做只读验证。

## 4. 添加 remote，验证连接

先检查当前项目，确认路径正确。尚非 Git 仓库时，在用户确定项目根目录后初始化；不要把上层目录或技能库误当项目。

```bash
git remote -v
```

把下面的 `<仓库页面复制的URL>` 替换为实际地址。没有同名 remote 时添加；`gitea` 已存在就核对 URL，地址相符直接复用，地址不同则先商量另取名字或更正，不直接覆盖：

```bash
git remote add gitea "<仓库页面复制的URL>"
git ls-remote gitea
```

`git ls-remote` 只读，不上传代码；**空仓库可能没有输出，退出码 0 才表示连接成功**。若远程已有提交，先比较历史和项目规则，不覆盖或强推。

若 GitHub 的 remote 已存在，保留它。向用户说明现有日常目标，并确定是否改为只推 Gitea、只推 GitHub 或双推，然后按实际 remote 名设置，例如：

```bash
git config --local sync.targets "gitea"
python "{SKILL_DIR}/scripts/sync.py" --detect
```

只想临时推一次 Gitea 时无需改 `sync.targets`；该平台只有一个 remote 时可用 `--to gitea`。同平台有多个 remote 时，使用已选好的 `sync.targets` 执行，不再用 `--to`。

## 5. 首次推送与核对

首次提交前检查作者身份：

```bash
git config user.name
git config user.email
```

缺少哪项就请用户提供其提交署名 / 邮箱，优先用 `git config --local user.name "<用户提供的署名>"` 和 `git config --local user.email "<用户提供的邮箱>"` 配在当前仓库；不要借用技能作者或他人的身份，也不要顺手修改全局配置。

目标、可见性和文件范围清楚，且用户已经授权这次推送后，按上一步已配置的日常目标运行：

```bash
python "{SKILL_DIR}/scripts/sync.py" "首次保存项目"
```

已有提交、只想推它们时改用 `--push-only`。若未改日常目标、仅想临时推一个 Gitea remote，且该平台只有这一个 remote，才加 `--to gitea`。随后只读核对 `git ls-remote gitea "refs/heads/<当前分支>"` 中的提交号与 `git rev-parse HEAD` 相同，并在网页查看分支和文件。报告实际成功的目标；只完成安装或认证时就只报告该阶段完成。
