# MeetingMind

> 给朋友圈用的会议录制 + 转录 + AI 解读小工具。Windows + GPU。

<!-- 演示 gif 占位，等真实使用 demo 后补 -->
<!-- ![demo](docs/assets/demo.gif) -->

## 这是什么

跑在你 Windows 机器上的一个 Claude Code skill。在 Claude 主对话里说一句**"录会议"**，它会：

1. **后台录**进程级音频（Teams / Zoom / 腾讯会议 / Edge 浏览器直播都行）。**调系统音量、静音都不影响录制**——这是它跟一般录屏软件的核心区别。
2. **自动截图**会议窗口的 PPT 翻页（变化检测，跨虚拟桌面也能抓）。
3. 你开完会跟 Claude 说"**结束了**"。
4. **本地 Qwen3-ASR 转录**音频成文字（GPU 几分钟跑完一小时会议）。
5. Claude 主对话**并行 spawn sub-agent** 读每张截图 + 转录，生成两份 markdown：
   - `interpretation.md`：逐页解读（视觉内容 / 对应转录 / 内容解读）
   - `summary.md`：整体总结（概要 / 要点 / 方法结果 / 待跟进）

整个过程从触发到拿到总结，一小时会议大约 **10-15 分钟**完成（GPU）。

---

## 系统要求

| 项 | 最低 | 推荐 |
|---|---|---|
| 操作系统 | Windows 10 build 19041+ | Windows 11 |
| Python | 3.10 | 3.13 |
| GPU | 无（CPU 能跑但慢 5-10 倍）| NVIDIA RTX 30 系以上，6 GB+ 显存 |
| CUDA | n/a | 12.8（torch 2.11.0+cu128）|
| 磁盘 | ~5 GB（Qwen3-ASR-1.7B 模型 + torch wheel） | 同左 |
| 网络 | 首次运行下模型（~3.4 GB） | 同左；之后离线工作 |

---

## 安装（朋友照抄版）

PowerShell 里跑（**项目根目录**）：

```powershell
# 1. 装 Python 3.13（如果还没装）— 推荐 winget
winget install Python.Python.3.13

# 2. 克隆这个 repo（你拿到的路径）然后进去
cd <你存放 MeetingMind 的路径>

# 3. 一键安装（建 venv + 装依赖 + 部署 skill，6 步全自动）
python install.py
```

`install.py` 会自检 Python 版本、Windows 版本、CUDA / GPU；然后建 `.venv`、装
`[transcribe]` 依赖（torch + qwen-asr + librosa 等，~2 GB 下载）、部署 skill。
每一步失败都会打印中文修复建议。

**首次跑会下 ~3.4 GB 模型**到 `~/.cache/huggingface/`，国内网络可能要挂代理或设镜像：

```powershell
$env:HF_ENDPOINT = "https://hf-mirror.com"
```

### 手动安装（高级）

如果你想自己 step-by-step 控制（或者 `install.py` 哪步出了问题想绕开重试）：

```powershell
# 建虚拟环境 + 装核心依赖
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e ".[transcribe]" --extra-index-url https://download.pytorch.org/whl/cu128

# 部署 Claude Code skill（让 Claude 主对话能识别"录会议"）
.venv\Scripts\python.exe skill\install.py
```

---

## 怎么用

部署完 skill 后，**重启 Claude Code**（或开个新对话），然后：

1. 在 Claude 对话里说 **"录会议"** 或 **"录个会议"**。Claude 会跟你问 4 个问题（主题 / 会议软件 / 截图灵敏度 / 是否录麦）。
2. 答完之后 Claude 后台启动录制。你**正常去开你的会**——Teams / Zoom / Edge 浏览器都行，可以最小化到角落或拖到另一个桌面，但**不能最小化到任务栏**（WGC 截图限制）。
3. 开完会跟 Claude 说 **"结束了"** 或 **"会议结束"**。等 ~10 分钟（一小时会议），Claude 会给你两份 markdown 路径：
   - `meetings/<date>-<topic>/interpretation.md`
   - `meetings/<date>-<topic>/summary.md`

打开看就行。原始音频、截图、纯文本转录都在 `meetings/<date>-<topic>/` 下，你可以单独存档或导出到笔记软件。

---

## 数据流和隐私（一定要看）

**会议内容总共会经过两个地方**：

1. **你本机**：
   - 音频被 ProcTap 抓到内存 → 写成 `.wav`
   - 截图被 Windows Graphics Capture API 抓到 → 写成 `.png`
   - Qwen3-ASR-1.7B 本地 GPU 推理转录 → 写成 `transcript.md`
   - **以上全程不出你这台机器**

2. **Anthropic（Claude）**：
   - 当 Claude 主对话执行 AI 解读步骤时，会把每批 5 张截图 + 完整 transcript 文字**作为 prompt 发给 Anthropic API**
   - 数据流向受 [Anthropic Privacy Policy](https://www.anthropic.com/legal/privacy) 约束
   - **这一步无法绕过**——AI 解读必须依赖 Claude，否则就没法生成 interpretation.md / summary.md
   - 如果会议**特别敏感**（公司机密、医患对话、法律商务谈判等），考虑只跑到 transcript 阶段（说"录会议但不要 AI 总结"，skill 会在 P3 结束停下），后面手动决定要不要上 AI

**本项目不存在任何"MeetingMind 服务器"** —— 没有任何第三方服务在 Anthropic 之外接收你的数据。整个流程要么本机、要么 Anthropic，二选一。

---

## 用了什么（依赖归因）

核心依赖都是开源软件，朋友装这个工具时会一并装上：

| 依赖 | 用途 | 来源 |
|---|---|---|
| [proc-tap](https://pypi.org/project/proc-tap/) | 进程级音频抓取（系统静音不影响录制的核心）| MIT 类型许可 |
| [windows-capture](https://pypi.org/project/windows-capture/) | Windows Graphics Capture API 的 Python 绑定 | MIT 类型许可 |
| [qwen-asr](https://pypi.org/project/qwen-asr/) + [qwen-omni-utils](https://pypi.org/project/qwen-omni-utils/) | Qwen3-ASR-1.7B 模型的 Python wrapper | Apache 2.0（阿里巴巴） |
| [Qwen3-ASR-1.7B 模型本身](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) | 本地语音识别模型 | Apache 2.0 + Qwen Research License（**商业使用要看 Qwen 自家条款**）|
| pycaw / psutil | Windows 进程和音频会话检测 | MIT / BSD |
| 其它（torch, transformers, librosa, soundfile, numpy, pillow 等）| 标准 ML 工具栈 | 各自开源协议 |

**注意**：本项目本身没采用开源协议（个人用），但**依赖于** Apache 2.0 / MIT / BSD 的开源软件。朋友自用、改一改都没问题；如果想拿去做商用产品，先**确认 Qwen3-ASR 模型的 Qwen Research License 商用条款**。

---

## 已知限制 / 出问题怎么找我

- **只支持 Windows**：ProcTap 用 Windows-specific 的 Process Loopback API，macOS / Linux 不能用。
- **会议窗口不能最小化到任务栏**：WGC 不抓不可见窗口。可以拖到角落或跨桌面，但完全隐藏就抓不到。
- **当前版本不录用户麦克风**：只录会议软件输出的声音（其他人讲话、共享音频等）。你自己的发言不录。
- **AI 解读依赖 Claude**：见上面隐私段。
- **没在多种 GPU 上系统测过**：开发机是 RTX 5070 / CUDA 12.8。RTX 30 系以下没测，但应该能跑（fp16 模型 ~4 GB 显存）。

出问题直接找我（Yuhang）。不要发到任何公开 Issue tracker——本项目没有公开 issue。
