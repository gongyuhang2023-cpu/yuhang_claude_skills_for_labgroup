# Colab 首次登录

> 什么时候读：`spark-colab start` 或 `colab-free usage` 报“还没授权”“授权已过期”，或用户说要开通 Colab、绑定 Google 账号、绑定云盘时。已经授权过就不用读。

每人只做一次。凭据保存在各人 Linux 账号的 `~/.config/gws/`，只有本人可读。AI 用谁的 SSH 身份登录，绑的就是谁的账号；多人共用的管理员账号上不要绑定，`spark-colab` 也会拒绝。

## 流程

开始前先告诉用户两点，用户说现在不用就跳过，以后要用时再做：

- 授权范围是读写整个 Google Drive 和 Google Cloud，凭据保存在他自己的服务器账号下。
- 只有 Drive 里的 `spark-lab` 目录会挂载到服务器，这个目录服务器上的其他用户都能读。

**1. 在 spark 上启动登录：**

```bash
spark-colab login
```

它返回授权链接 `url`、回调端口 `port` 和隧道命令 `tunnel`。后台进程会一直等浏览器回调。

**2. 在有浏览器的电脑上开隧道：** 浏览器要能把授权结果送回 spark 上的这个端口。

- AI 跑在用户电脑上：自己在后台运行 `tunnel` 里的命令，地址换成这台电脑连 spark 用的别名或地址。
- AI 跑在 spark 上：把命令给用户，请用户在自己电脑的终端里运行，并保持窗口开着。

隧道报 `bind ... Permission denied` 或端口被占（Windows 上常见）：重新运行 `spark-colab login` 换一个端口，旧的登录进程会自动结束。报 `Permission denied (publickey)` 是这台电脑连不上 spark，换端口没用，先解决 SSH 登录。

**3. 把 `url` 发给用户，请用户在这台电脑的浏览器里打开：**

1. 选自己的 Google 账号。
2. 看到“Google 未验证此应用”时，点「高级」，再点「转至 spark-colab（不安全）」。这是组内自建的应用，没有提交 Google 审核，所以有这个提示。
3. 权限页有勾选框就全部勾上，再点继续。
4. 看到登录成功的页面就完成了。

**4. 完成设置：**

```bash
spark-colab login --finish
```

它会等到授权结束，再创建 `My Drive/spark-lab/transfer`，把它挂载到 `/mnt/gdrive/<用户名>`，最后返回账号和余额。Claude Code 发完链接就把这条放后台运行；Codex 等用户说点好了再运行。要分段等，加 `--max-wait 秒`，超时返回 20。

**5. 把结果告诉用户，并请用户确认：**

| 返回项 | 正常值 | 不对时 |
|---|---|---|
| `account` | 用户本人的邮箱 | 选错账号：重新运行第 1 步 |
| `drive_mounted` | `true` | 运行 `gdrive-personal status`，看 `journalctl --user -u gdrive-personal.service -n 30` |
| `paid_cu_balance` | `0.0` | 数字非零见下一节；`null` 是查询失败，不是余额非零，运行 `colab-free usage` 看原因 |
| `colab_free_eligible` | `true` | 同上 |

结束后可以关掉隧道。

## 余额不是 0

`colab-free` 只放行余额为 0 的账号，保证只花免费额度。Colab 额度只发给 Google AI 付费套餐的主账号，家庭组里被拉进来的成员余额是 0。所以如果余额非零，这个账号多半是家庭组管理员，或者自己买过额度。

这不是故障。云盘照常能用，只是不能用 `spark-colab` 计算。告诉用户原因，让用户决定换一个账号还是继续在 spark 排队。不要尝试消耗余额，也不要绕过检查。

## 授权反复过期

大约 7 天就过期，多半是自己建的客户端还停在“测试”状态。到 Google Cloud 控制台把应用发布成正式版；或者把 `~/.config/gws/client_secret.json` 换成共用客户端 `/opt/bio/share/colab/client_secret.json`，再重新走上面的流程。

## 共用客户端不可用时：自己建一个

正常情况下不需要这一节，`spark-colab login` 会用组内共用的 OAuth 客户端。只有它报找不到共用客户端，或者用户想用自己的项目时才走这里。整个过程在用户的浏览器里做，AI 只能指路。如果要绑卡或开结算，不用绑。

控制台地址：<https://console.cloud.google.com/>。界面名称可能略有差异。

1. **新建项目：** 点左上角的项目选择器，在弹窗里点「新建项目」，名称填 `spark-colab`。确认左上角已切换到新项目，别改已有的项目。
2. **启用 API：** 顶部搜索 `Google Drive API`，点「启用」。
3. **配置应用：** 顶部搜索 `Google Auth Platform`，点「开始」，填应用名称和邮箱，受众群体选「外部」。
4. **补全品牌信息：** 发布前必须填。进左侧「品牌塑造」，填「应用首页」和「应用隐私权政策链接」，都要是能公开访问的页面；再在「已获授权的网域」里加上这两个链接的域名，保存。
5. **发布应用：** 进左侧「目标对象」，点「发布应用」并确认，状态变成正式版。按钮是灰色的，就是第 4 步还没填完。不发布的话，授权 7 天就会过期。
6. **创建客户端：** 进左侧「客户端」，点「创建客户端」，应用类型选「桌面应用」。“将此客户端用于 AI 赋能的智能体”不用勾。在创建完成的弹窗里直接下载 JSON，关掉弹窗后可能拿不到密钥。
7. **放到 spark：** 把 JSON 传到 spark 的 `~/.config/gws/client_secret.json`，权限设为 600。不要打印文件内容。然后回到上面流程的第 1 步。
