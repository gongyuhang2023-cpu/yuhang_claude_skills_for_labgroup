# Colab：spark 排不上时，把小任务放到 Google 云端

> 什么时候读：用户点名 Colab；或者提交作业时 spark 的 GPU、核心或内存被占满，而任务本身不大。
>
> 规则以 `/ai.md` 和服务器上的 `/opt/bio/share/colab/README.md` 为准，这里只写怎么判断、怎么调用。

每人用自己的 Google 账号，在 Google 的临时机器上计算，只花免费额度。一律用服务器命令 `spark-colab`：它负责把 spark 上的文件传到 Google、申请机器、跑任务、存结果、释放机器，全程在后台照看。不要自己拼 `colab-free` 的多步命令，也不要用 nohup 或 tmux 挂着。

## 先判断合不合适

免费机器的规格（2026-11-06 实测，Google 不公布，可能变化）：x86_64，2 核，12G 内存，约 110G 磁盘；T4 显存 15G。最长约 12 小时，GPU 不保证有，机器可能中途被收回。

| 情况 | 怎么办 |
|---|---|
| spark 此刻排不上，任务放得下上面的规格 | 主动提一句可以放 Colab，用户同意才跑 |
| spark 上这个工具因为 ARM 装不了，任务又不大 | 可以提 Colab，远端是 x86 |
| 任务本身超过 spark 可调度内存 | Colab 更放不下，明说，建议拆小；拆不开就问管理员有没有更大的机器可用 |
| 需要 `/opt/bio/db` 的公共数据库 | Colab 上没有，继续排队 |
| 跑得久，又不能断点续跑 | 被收回就全丢，继续排队 |
| 数据不能传给 Google | 继续排队 |

CPU 不够时，Colab 只有 2 核，只对小任务有意义。

## 第一次用

运行 `colab-free usage`。报未授权或授权过期，就读 `references/colab-first-login.md` 带用户登录。

## 给用户看的计划（总闸）

跑之前把这些事项列进计划，按统一规则取得单步或整份计划的同意。[同意规则](../SKILL.md#同意规则)

- 用哪个 Google 账号（`gws auth status` 的 `user`）。
- 传什么数据、多大。数据会传给 Google，放进 Drive 的 `spark-lab` 目录，服务器上的其他用户也能读。
- 用 CPU 还是 T4，预计跑多久。
- 结果放哪。任务成功后，Drive 上的输入副本会自动删掉，`output` 副本留在 `transfer/<任务名>/`；任务失败时输入也留着，都由用户决定什么时候清。
- 要不要改用户的脚本，比如改成读 `$INPUT_DIR`、写 `$OUTPUT_DIR`。

## 写任务脚本

脚本是 `.py` 或 `.sh`，在 Colab 机器上运行，用这几个环境变量：

| 变量 | 内容 |
|---|---|
| `INPUT_DIR` | `--input` 传上来的内容 |
| `OUTPUT_DIR` | 结果写这里，全部会取回 spark |
| `CHECKPOINT_DIR` | 断点文件写这里，运行中定期复制回 spark 的 `checkpoint/`。只复制这一层的文件，不含子目录 |

- 脚本在机器上的工作目录是 `$RUN_DIR`。用到多个代码文件，就和数据一起放进 `--input`，脚本可以直接 `import`。
- 依赖用 `--pip` 或 `--requirements` 装，每台新机器都要重装。
- 断点文件复制时会整个读进内存，单个文件控制在几百 MB 以内。
- **续跑要自己接上：** 新机器上 `CHECKPOINT_DIR` 是空的。把上次 `checkpoint/` 里的文件放进新任务的 `--input`，脚本从 `$INPUT_DIR` 读取。脚本没写这段逻辑就会从头跑，不要说成已恢复。
- 文件很多的输入先打成一个包再传，在脚本里自己解包，上传会快很多。

## 调用

这些命令都在 spark 上运行，路径也是 spark 上的路径。脚本和数据还不在 spark 上，就先传上去，再到对应目录下运行：

```bash
spark-colab start --script job.py --gpu --input ./data --pip scanpy
spark-colab wait <任务名>
spark-colab status [任务名]
spark-colab cancel <任务名>
```

- `start` 立刻返回任务名，后台开始跑。不加 `--gpu` 就是免费 CPU。
- 结果默认放在当前目录的 `colab-results/<任务名>/`。
- 同一账号同一时间只跑一个任务。

**等结果：** 任务不需要 AI 守着。存结果、释放机器都由后台进程完成，电脑关了也不影响，AI 只负责告诉用户。计划里先估一个大概时长。

- **Claude Code：** 后台运行 `spark-colab wait <任务名> --max-wait <秒>`，秒数取预估时长，长任务最多取一小时。任务一结束命令就返回并唤醒 agent；到时间还没结束就返回 20，这时用 `status` 看 `last_output` 有没有进展，再调一次。这一条命令既是完成时的唤醒，也是定期检查。
- **Codex：** 不要守着等，它每次查看都要调用模型，实测 7 分钟用掉约 50 万输入 token。启动后告诉用户预计多久，然后结束这一轮。用户回来时先运行 `status`，再汇报结果。只有几分钟的任务可以在前台 `wait`。
- **AI 会话已经关了：** 下次打开时用 `status` 查，向用户补报。

`status` 返回任务列表。`active` 是 `true` 表示还在跑，这时看 `stage`（`upload`、`allocate`、`prepare`、`running`、`saving`、`releasing`、`fetching`）和 `last_output`；上传和准备阶段还没有 `last_output`，不等于没进展。结束后 `state` 的含义和下表相同。

## 结果怎么看

| `wait` 返回码 | `state` | 告诉用户 |
|---|---|---|
| 0 | `done` | 跑完了，结果在 `results` 指向的目录 |
| 10 | `job-failed` | 脚本报错退出，看 `job.log` |
| 12 | `cancelled` | 已取消 |
| 13 | `failed` | 没跑完，看 `message`。常见的有 T4 暂时没资源、机器被收回 |
| 13 | `monitor-lost` | 后台进程没了，机器可能还开着，运行 `spark-colab cancel <任务名>` |
| 14 | `results-incomplete` | 任务跑完了，但结果没有全部取回。按 `message` 里的命令补救，不要说成功 |
| 20 | 任务还在跑 | `--max-wait` 到了，再调一次 |
| 22 | | 没有这个任务名 |

- 已有输出是否取回，看 `results_verified`，不要只看状态。
- `vm_released` 是 `false` 时，机器还开着。结果都拿到了就运行 `spark-colab cancel <任务名>` 释放；`message` 说结果还在机器上时，先按它的命令重试，别急着释放。
- 机器被收回后，只有 `checkpoint/` 里已复制的文件还在。
- T4 没资源时，告诉用户可以稍后再试、改用 CPU，或继续排队。

结果目录里还有 `job.log`、`monitor.log`、`run.json`（脚本校验值和时间）、`run.ipynb`，以及 `output/_colab_env.txt`（Python、GPU、全部包版本）。这些跟着结果一起保留，写方法时用得上。

## 不要做

- 不要买额度、充值，也不要换账号绕过限额。Google 禁止用多个账号绕过使用限制。
- 不要用 `colab-free` 以外的客户端。
- 余额非零时不要继续尝试，告诉用户原因（见首次登录文档）。
