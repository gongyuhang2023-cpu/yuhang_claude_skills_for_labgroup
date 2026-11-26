# LRZ 开通与第一次连接

> 什么时候读：还没有 LRZ 账号、刚拿到账号要激活、第一次在某台机器上连 LRZ，或者登录报错时。已经能登录、连接复用也配好了，就不用读。日常使用读 [LRZ](lrz.md)。
>
> 依据 LRZ 官方文档，2026-11-26 查阅；标「实测」的内容来自当天课题组的激活过程。

每个人的机器不一样：有的直接在 Spark 上工作，有的在 Windows 上 ssh 到 Spark，有的用 Mac。具体配置交给自己的 AI，按下面这些事实来做。

## 开通（每人一次）

1. **账号**：找课题组的 master user 开一个带 Linux Cluster 权限的个人账号。用起始密码登录 IDM 门户 2（https://idmportal2.lrz.de），按提示改密码，建议同时勾选生成 PUK，忘记密码时可以用它自助重置。
2. **出口管制声明**：在 IDM 门户 Person → Policies 里签署，每 12 个月重签一次。签署后大约 1 小时 LRZ 才会建好 HOME，在那之前不要登录。中途改过账号属性（比如重签了声明），可能要等到第二天早上 6 点。
3. **2FA**：必须开，而且按账号绑定，有几个账号就绑几次。
   - 在 https://simmfa.sim.lrz.de 选 Enroll Token → TOTP，OTP length 选 8，点 Enroll Token，然后用 App 扫**新出现**的二维码。页面上方那两个码是下载 App 用的，不要扫。
   - App 用 privacyIDEA Authenticator。LRZ 收到过 Google 和 Microsoft Authenticator 的问题报告。
   - **不要设 PIN**，也不要把二维码存到电脑上。绑好后在 All tokens 里点 Test token 测试。
   - 另外生成一份 TAN 列表，打印或离线保存，手机丢了时用它登录。

## 第一次登录

- 运行 `ssh <账号>@cool.hpc.lrz.de`。先提示 `Password:`，输入密码；再提示 `2FA:`，输入 App 上的动态码。
- SSH 密钥省不掉 2FA：密钥只能走 2222 端口，仍然要 2FA，LRZ 也不推荐这种方式。
- 第一次连接会问是否信任这台主机，核对指纹后再输 yes：
  - ED25519 `SHA256:UczZUx+1t3gslr46ADhTfnaaK2lfGp2s7AULfnfIp6w`
  - ECDSA `SHA256:vW10yfpMtcUz14Ckv4+7hXdEYpbE6lKaagbzDZQ2t+4`
- 看到命令提示符，并且 `ls -ld $HOME` 有输出，就算开通完成。
- 登录后提示 `Could not chdir to home directory … No such file or directory`，说明登录本身成功了，只是 HOME 还没建好。先退出，过一段时间再登录检查（实测）。
- Spark 可以直接连 LRZ（实测）。

## 配置连接复用（每台机器一次）

让 AI 能执行命令的前提，是本人登录一次之后，后续命令复用这条连接。

- **推荐在 Spark 上配置。** 任务先在 Spark 上跑通，数据库和数据也都在 Spark 和 NAS 上，从 Spark 推到 LRZ 最快。只有数据在自己电脑上时，才在本机配置。
- Linux、macOS、WSL 和 Spark 上的 OpenSSH 都支持连接复用。**Windows 自带的 OpenSSH 不支持**（Win32-OpenSSH issue #405），Windows 用户在 Spark 或 WSL 上配置。
- 只有数据库和数据能直接传：Spark 和 NAS 上的环境、工具都是 ARM64 构建，到 LRZ 要按配方重建。
- 在 `~/.ssh/config` 里加一个 Host，例如：

  ```
  Host lrz
      HostName cool.hpc.lrz.de
      User <你的 LRZ 账号>
      ControlMaster auto
      ControlPath ~/.ssh/cm-%C
      ControlPersist 8h
      ServerAliveInterval 100
  ```

- 本人在终端运行 `ssh -fN lrz`，依次输入密码和动态码，连接会转到后台。之后用 `ssh -O check lrz` 确认连接还在。

## 登录失败时

`Permission denied (keyboard-interactive)` 常见的原因有：

- 密码或动态码输错了。动态码有时效，等 App 换下一个码再试。
- 出口管制声明没签、已过期，或者刚签还不到 1 小时。
- 这个账号没有 Linux Cluster 权限。如果一个人有多个账号，要用带权限的那个，不确定就问 master user。
- 这个账号还没绑 2FA，或者绑的时候设了 PIN。

不要连续反复尝试登录。查清原因后再试，仍然不行就把账号名和报错发给 master user 或 LRZ Servicedesk。

## 来源

- 首次开通：https://doku.lrz.de/compact-guide-to-first-time-linux-cluster-access-process-11484378.html
- 登录与 2FA：https://doku.lrz.de/access-and-login-to-the-linux-cluster-10745974.html
- 2FA token：https://doku.lrz.de/two-factor-authentication-via-totp-token-35882375.html
- 登录排错：https://doku.lrz.de/i-cannot-login-to-the-linux-cluster-3184661020.html
- SSH 与密钥：https://doku.lrz.de/ssh-secure-shell-on-lrz-hpc-systems-10746160.html
- 主机密钥：https://doku.lrz.de/list-of-ssh-key-fingerprints-for-the-linux-cluster-2535752303.html
- Windows 不支持连接复用：https://github.com/PowerShell/Win32-OpenSSH/issues/405
