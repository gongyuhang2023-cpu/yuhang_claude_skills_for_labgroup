# spark-colab privacy note

spark-colab is a Google OAuth client used by members of a research group. It connects each member's own Google account to command-line tools on the group's server, so they can move their own files through Google Drive and run jobs on Google Colab.

- **What the app collects:** nothing. It has no website or backend of its own and sends no data to its developer.
- **Sign-in tokens:** created when you grant access and stored in your own account on the group's server. The server's administrators have root access to that server.
- **Your files:** they move only between your Google Drive, your Colab runtime and the group's server, when you or an agent acting for you runs a command.
- **The `spark-lab` Drive folder:** while it is mounted on the server, other server users can read the files in it. Other parts of your Drive are not mounted.
- **Revoking access:** you can revoke access at any time at <https://myaccount.google.com/permissions>.

Google's own handling of your data is covered by the [Google Privacy Policy](https://policies.google.com/privacy). Questions: open an issue in this repository.

## 中文

spark-colab 是课题组成员使用的 Google OAuth 客户端，把每个人自己的 Google 账号连接到组内服务器上的命令行工具，用于通过 Google Drive 传文件、在 Google Colab 上跑任务。

- 应用本身不收集数据，没有自己的网站或后端，也不向开发者发送任何数据。
- 登录凭据在你授权后生成，保存在你自己的服务器账号下。服务器管理员拥有该服务器的 root 权限。
- 文件只在你的 Google Drive、Colab 机器和组内服务器之间流动，由你或代你操作的 AI 运行命令时才发生。
- Drive 里的 `spark-lab` 文件夹挂载到服务器后，其他服务器用户可以读取；Drive 的其他内容不挂载。
- 可以随时在 <https://myaccount.google.com/permissions> 撤销授权。
