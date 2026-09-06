# proctap (vendored, patched) — meeting_mind 专用

这是开源库 **ProcTap**（PyPI `proc-tap`）的**精简 + 打补丁**源码副本，随 meeting_mind skill
一起管理。meeting_mind 的录音模块 `meetingmind/audio.py` 通过 `from proctap import ProcessAudioCapture`
依赖它，在 Windows 上用 WASAPI process-loopback 按进程抓会议音频。

> 这里**不是 git 仓库**（已刻意去除 `.git`，统一随 skill 管理）。要跟上游更新，见文末「重建/更新」。

## 来源（Provenance）

- Upstream: <https://github.com/m96-chan/ProcTap.git>
- Base: tag **v1.0.3** / commit **d9b2e199524195cc5f39bfea780a18b9513350b6**
- 本地改动: 仅 `src/proctap/_native.cpp`（见 `proctap-v1.0.3-loopback-fix.diff`，相对上述 base）

## 补丁内容（为什么存在）

WASAPI process-loopback 抓音频的两处修复，全部在 `src/proctap/_native.cpp`：

1. **格式自动转换**：两处 `IAudioClient::Initialize`（主格式 + fallback）都加上
   `AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM`，让 WASAPI 自动把采集流转成请求的 PCM 格式
   （对齐微软官方 ApplicationLoopback 示例）。否则 process-loopback 初始化易因格式不匹配失败。
2. **静音缓冲处理**：当采集回调收到 `AUDCLNT_BUFFERFLAGS_SILENT` 时，源缓冲 `pData` 内容不确定，
   改为 `memset` 写零（而非 `memcpy` 拷垃圾），保持时间轴对齐。

**影响**：不打这个补丁，录出来的会议音频可能是**全静音 / 全零**或初始化失败。
meeting_mind 的 `audio.py` 注释里明确在等这个 patch。

## install.py 如何保证补丁不被覆盖

skill 的依赖声明是 `proc-tap>=1.0.3`，`install.py` 跑 `pip install -e ".[transcribe]"` 时会从
**PyPI 拉原版（无补丁）**。为防止它覆盖修复，`install.py` 在 editable 安装之后**多跑一步**：
强制安装本地补丁版（优先用 `dist/` 里匹配当前解释器的预编译 wheel，找不到则从本目录源码现编）：

```
pip install --force-reinstall --no-deps <dist 里匹配的 .whl>      # 首选：无需编译器
pip install --force-reinstall --no-deps <本目录>                  # 回退：现编，需 MSVC + Windows SDK
```

预编译 wheel：`dist/proc_tap-1.0.3-cp313-cp313-win_amd64.whl`（**CPython 3.13 / win_amd64 专用**）。

## 重建 / 更新

- **换了 Python 版本**（wheel ABI 不匹配）→ 在本目录重新编译 wheel：
  ```
  <skill venv python> -m pip wheel . --no-deps -w dist
  ```
  需 MSVC Build Tools + Windows SDK。
- **直接装进当前 venv**（顺手验证）：
  ```
  <skill venv python> -m pip install --force-reinstall --no-deps .
  ```
- **跟上游新版本**：重新 `git clone` m96-chan/ProcTap，`git apply proctap-v1.0.3-loopback-fix.diff`
  （或对照 diff 手动改 `_native.cpp`），再替换本目录源码 + 重编 wheel。
