# MeetingMind 首次使用引导

> Claude 仅在检测到 config.yaml 的 output.base_dir 为空时读取本文件。

## Step 1: 环境检查

运行以下命令检查环境：

```bash
python ~/.claude/skills/meeting_mind/scripts/run.py session.py --help
```

- **成功**：显示帮助信息 → 跳到 Step 3
- **失败**：venv 不存在或依赖安装失败 → 继续 Step 2

## Step 2: 依赖安装排障

### 2a. 自动安装（首选）

```bash
python ~/.claude/skills/meeting_mind/scripts/setup_environment.py
```

### 2b. 常见问题

**windows-capture 安装失败**：
- 原因：没有当前 Python 版本的预编译 wheel
- 解决：
  1. 升级 pip: `python -m pip install --upgrade pip`
  2. 如果仍然失败，需要安装 [Rust 工具链](https://rustup.rs/) 和 Visual Studio Build Tools（C++ 桌面开发工作负载）
  3. 或降级到有 wheel 的 Python 版本（3.11/3.12）

**PyAudioWPatch 安装失败**：
- 原因：PortAudio 二进制兼容性
- 解决：`pip install PyAudioWPatch --only-binary :all:` 强制使用预编译 wheel

**pywin32 安装后 import 失败**：
- 运行：`python -m pywin32_postinstall -install`

### 2c. 验证核心依赖

```python
python -c "
import pyaudiowpatch; print('PyAudioWPatch OK')
import windows_capture; print('windows-capture OK')
import numpy; print('numpy OK')
from PIL import Image; print('Pillow OK')
import pygetwindow; print('PyGetWindow OK')
import win32gui; print('pywin32 OK')
import yaml; print('PyYAML OK')
print('All OK')
"
```

用 venv 的 Python 执行：`~/.claude/skills/meeting_mind/.venv/Scripts/python.exe`

## Step 3: 用户配置

通过 AskUserQuestion 询问以下内容：

### 3a. 保存位置

```
Question: "会议录制文件保存到哪里？"
Options:
  - ~/Desktop/meeting_archive (推荐)
  - ~/Documents/meeting_archive
  - [用户自定义路径]
```

获取后写入 config.yaml 的 `output.base_dir` 字段。

### 3b. ASR 本地模型（可选）

```
Question: "是否安装本地语音转文字模型？（Qwen3-ASR-1.7B，需 CUDA GPU + ~4GB 显存）"
Options:
  - 安装（需下载 ~3.5GB 模型，首次转录时自动下载）
  - 暂不安装（之后可随时添加）
```

如果选择安装，执行：
```bash
~/.claude/skills/meeting_mind/.venv/Scripts/python.exe -m pip install -r ~/.claude/skills/meeting_mind/requirements-asr.txt
```

注意：
- torch 会自动安装 CUDA 版本（~2.5GB）
- 模型本身在首次运行 transcribe.py 时从 HuggingFace 下载
- 无 NVIDIA GPU 的机器选"暂不安装"

验证 CUDA：
```python
python -c "import torch; print(f'CUDA: {torch.cuda.is_available()}, Device: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"N/A\"}')"
```

### 3c. 更新 config.yaml

将用户选择写入 config.yaml：
- `output.base_dir`: 用户选择的保存路径
- `transcription.engine`: "qwen3-asr-local" 或 "skip"

写入后 base_dir 不再为空，下次使用不会再触发本引导。

## Step 4: 快速验证（可选）

建议用户打开一个任意窗口（如记事本），运行 5 秒测试：

```bash
python ~/.claude/skills/meeting_mind/scripts/run.py session.py --meeting teams --interval 3 --threshold 5 --output ~/Desktop/meeting_test
```

5 秒后 Ctrl+C 停止，检查输出目录中是否有 recording.wav 和 slide_001.png。
