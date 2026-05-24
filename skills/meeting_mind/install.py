"""MeetingMind 一键安装脚本（朋友圈版）.

朋友拿到 repo 后：
    python install.py

会完成六件事：
    1. Python ≥ 3.10 检查
    2. Windows 10 build 19041+ 检查（非 Windows 警告但允许继续）
    3. CUDA / GPU 探测（nvidia-smi）
    4. 创建 .venv（已存在则跳过）
    5. 装 [transcribe] extras（torch / qwen-asr / librosa 等）
    6. 部署 Claude Code skill 到 ~/.claude/skills/meetingmind/

整个流程跑在**系统 Python** 下（朋友的 Python，未激活 venv），
通过 subprocess 派生子进程调 .venv 里的 Python。

错误处理：失败统一 exit 1 + 打印中文修复建议 + 命令字面值保留英文。
不回滚 .venv（失败留着方便下次复用）。
"""

from __future__ import annotations

import contextlib
import platform
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
VENV_DIR = REPO_ROOT / ".venv"
PYPI_CUDA_INDEX = "https://download.pytorch.org/whl/cu128"
PYTHON_DOWNLOAD_URL = "https://www.python.org/downloads/"
WINDOWS_UPDATE_URL = "https://support.microsoft.com/windows/update"


# --------------------------------------------------------------------- helpers


def _harden_stdio_encoding() -> None:
    """Force UTF-8 on stdout/stderr so the Chinese banner + ✓ checkmark
    don't crash on terminals defaulting to GBK/CP936 (PowerShell's default
    in zh-CN locale). Characters the underlying terminal still can't render
    fall back to '?' instead of raising UnicodeEncodeError mid-print.

    Same trick cli.py uses — see src/meetingmind/cli.py:_harden_stdio_encoding.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        # Best-effort: terminal may refuse the reconfigure call.
        with contextlib.suppress(Exception):
            reconfigure(encoding="utf-8", errors="replace")


def _format_step_header(step_num: int, title: str) -> str:
    """统一的步骤分隔符 — 所有步骤视觉上一致，输出像 checklist。"""
    bar = "═" * 60
    return f"\n{bar}\n[步骤 {step_num}/6] {title}\n{bar}"


def _parse_python_version(version: tuple[int, int, int]) -> tuple[bool, str]:
    """检查 Python 版本是否 ≥ 3.10，返回 (ok, 中文友好消息).

    Python 4.x （假设性的）也通过 — 我们只拦 Python 3.9 及以下。
    """
    major, minor, micro = version[0], version[1], version[2]
    current = f"{major}.{minor}.{micro}"
    if major < 3 or (major == 3 and minor < 10):
        return False, (
            f"Python 版本太老（当前 {current}，需要 ≥ 3.10）。\n"
            f"请去 {PYTHON_DOWNLOAD_URL} 下载 Python 3.13（推荐），"
            f"装完重新跑 `python install.py`。"
        )
    return True, f"Python {current} ✓"


def _parse_nvidia_smi_output(stdout: str) -> str | None:
    """解析 nvidia-smi 输出，提取第一块 GPU 名字。

    多卡机只取第一行 — installer 关心"有没有 GPU"而不是拓扑。
    空输出 / 全空白 → None（无 GPU 或驱动没装）。
    """
    for line in stdout.splitlines():
        name = line.strip()
        if name:
            return name
    return None


# --------------------------------------------------------------------- checks


def check_python_version() -> bool:
    ok, msg = _parse_python_version(sys.version_info[:3])
    print(msg)
    return ok


def check_windows() -> bool:
    """检查是否 Windows 10 build 19041+。非 Windows 警告但允许继续。

    返回值：True = 继续；False = 用户主动取消（当前实现总返回 True，
    走警告路径，跟朋友圈定位一致）。
    """
    system = platform.system()
    if system != "Windows":
        print(
            f"⚠️ 非 Windows 系统（检测到 {system}）。\n"
            f"MeetingMind 的录音模块（ProcTap）+ 截图模块（Windows Graphics "
            f"Capture API）只在 Windows 10 build 19041+ / Windows 11 工作。\n"
            f"你可以继续装、看代码 / 跑测试，但 record 子命令会失败。"
        )
        return True

    # Windows: try to get the build number via platform.version() like "10.0.26200"
    version_str = platform.version()
    parts = version_str.split(".")
    try:
        build = int(parts[2]) if len(parts) >= 3 else 0
    except ValueError:
        build = 0

    if build < 19041:
        print(
            f"⚠️ Windows 版本可能太老（检测到 build {build or '?'}，需要 ≥ 19041）。\n"
            f"如果功能异常，去 {WINDOWS_UPDATE_URL} 更新到最新 Windows 10 或换 Windows 11。\n"
            f"继续安装。"
        )
        return True

    print(f"Windows build {build} ✓")
    return True


def detect_cuda() -> str | None:
    """检测是否有可用 NVIDIA GPU。返回 GPU 型号字符串或 None。

    不要尝试装 nvidia-smi —— 那是用户驱动层的事，installer 不碰。
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        print(
            "⚠️ 未找到 nvidia-smi 命令 —— 通常意味着没装 NVIDIA 驱动或没有 N 卡。\n"
            "可以继续装（走 CPU 推理路径），但 ASR 转录会比 GPU 慢 5-10 倍。"
        )
        return None
    except subprocess.TimeoutExpired:
        print("⚠️ nvidia-smi 超时（10s），假设没 GPU。")
        return None

    if result.returncode != 0:
        print(
            f"⚠️ nvidia-smi 退出码 {result.returncode}，假设没 GPU。\n"
            f"stderr 摘要: {result.stderr.strip()[:200]}"
        )
        return None

    gpu = _parse_nvidia_smi_output(result.stdout)
    if gpu is None:
        print("⚠️ nvidia-smi 返回空，假设没 GPU。")
        return None

    print(f"GPU 检测到: {gpu} ✓")
    return gpu


def confirm_continue_without_gpu() -> bool:
    """没 GPU 时问朋友：还装吗？默认 yes（朋友可能想先装着以后买 GPU）。"""
    try:
        ans = input("没检测到 GPU，CPU 推理会慢 5-10 倍。继续装吗？(Y/n): ").strip().lower()
    except EOFError:
        # 非交互环境（CI / pipe）→ 默认继续
        return True
    return ans != "n"


# --------------------------------------------------------------------- venv / install / deploy


def _venv_python() -> Path:
    """返回 .venv 里的 python 可执行路径（跨平台）。"""
    if platform.system() == "Windows":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def create_venv() -> bool:
    """创建 .venv（已存在则跳过）。返回 True 表示可用，False 表示失败。"""
    venv_py = _venv_python()
    if venv_py.is_file():
        print(f".venv 已存在 ✓（跳过创建）\n  路径: {VENV_DIR}")
        return True

    print(f"创建 .venv 中... 路径: {VENV_DIR}")
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", str(VENV_DIR)],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(
            f"\n❌ venv 创建失败（exit {exc.returncode}）。\n"
            f"   检查 Python 安装是否完整（`python -m venv --help` 能否运行）。\n"
            f"   常见原因：Python 是 Microsoft Store 版本（受限）→ 装 python.org 官方版。"
        )
        return False

    if not venv_py.is_file():
        print(
            f"\n❌ venv 创建后找不到 Python 可执行：{venv_py}\n"
            f"   检查 .venv/ 目录是否完整。"
        )
        return False

    print(f".venv 创建成功 ✓\n  Python: {venv_py}")
    return True


def pip_install_extras(has_cuda: bool) -> bool:
    """在 .venv 里跑 `pip install -e ".[transcribe]" [--extra-index-url ...]`."""
    venv_py = _venv_python()
    cmd = [
        str(venv_py),
        "-m", "pip", "install",
        "--upgrade", "pip",
    ]
    print("先升级 pip 自身...")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"\n⚠️ pip 升级失败（exit {exc.returncode}），继续尝试装项目（旧 pip 通常也能用）。")

    cmd = [
        str(venv_py),
        "-m", "pip", "install",
        "-e", ".[transcribe]",
    ]
    if has_cuda:
        cmd.extend(["--extra-index-url", PYPI_CUDA_INDEX])
        print("装 [transcribe] extras（含 CUDA torch wheel，~2 GB 下载）...")
        print(f"  额外索引: {PYPI_CUDA_INDEX}")
    else:
        print("装 [transcribe] extras（CPU 版 torch，~200 MB 下载）...")

    print("（torch wheel 较大，可能要 3-10 分钟，看网速。请耐心等。）")
    print(f"  完整命令: {' '.join(cmd)}\n")

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        print(
            f"\n❌ pip install 失败（exit {exc.returncode}）。\n"
            f"   看上面 pip 输出的最后几行，常见原因：\n"
            f"   1. 网络问题 / torch wheel 没下完 → 重新跑 `python install.py`\n"
            f"   2. 国内访问慢 → 设环境变量 HF_ENDPOINT=https://hf-mirror.com（HuggingFace 镜像），\n"
            f"      pip 镜像可用 `pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple`\n"
            f"   3. torch CUDA wheel 跟你的驱动版本不兼容 → 检查 `nvidia-smi` 显示的 CUDA 版本\n"
            f"      （我们装的是 cu128，需要驱动 ≥ R570；旧驱动需更新或换 cu121 索引）"
        )
        return False

    print("\n[transcribe] extras 安装成功 ✓")
    return True


def deploy_skill() -> bool:
    """部署 Claude Code skill 到 ~/.claude/skills/meeting_mind/.

    两种运行场景：
      - **dev 模式**（MeetingMind 项目根）：`skill/install.py` 存在 →
        调它把 `skill/SKILL.md` 拷到 ~/.claude/skills/meeting_mind/。
      - **in-place 模式**（朋友拿 lab repo bundle 部署后从 skill 目录
        自身跑 install.py）：`skill/install.py` 不存在 → SKILL.md 已经
        在 skill 目录里了，跳过部署。
    """
    skill_installer = REPO_ROOT / "skill" / "install.py"
    if not skill_installer.is_file():
        print(
            "已经在 skill 目录内运行 ✓ SKILL.md 已就位，跳过部署。\n"
            f"  当前 skill 目录: {REPO_ROOT}"
        )
        return True

    venv_py = _venv_python()
    print("部署 Claude Code skill 到 ~/.claude/skills/meeting_mind/...")
    try:
        subprocess.run(
            [str(venv_py), str(skill_installer)],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(
            f"\n❌ skill 部署失败（exit {exc.returncode}）。\n"
            f"   常见原因：~/.claude/ 目录权限问题（多账号机器可能写不进去）。\n"
            f"   手动跑：{venv_py} {skill_installer}"
        )
        return False

    return True


# --------------------------------------------------------------------- main


def main() -> int:
    _harden_stdio_encoding()
    print("MeetingMind 一键安装脚本（朋友圈版）")
    print(f"项目根: {REPO_ROOT}")

    # Step 1: Python
    print(_format_step_header(1, "Python 版本检查"))
    if not check_python_version():
        print(
            "\n❌ 安装中止：Python 版本不满足要求。"
            "\n   按上面的提示装好 Python 后重新跑 `python install.py`。"
        )
        return 1

    # Step 2: Windows
    print(_format_step_header(2, "Windows 版本检查"))
    check_windows()  # 返回值忽略 — 非 Windows / 旧版本都是警告不拦

    # Step 3: CUDA
    print(_format_step_header(3, "CUDA / GPU 探测"))
    gpu_name = detect_cuda()
    has_cuda = gpu_name is not None
    if not has_cuda and not confirm_continue_without_gpu():
        print("\n用户取消安装。.venv 未创建，无副作用。")
        return 1

    # Step 4: venv
    print(_format_step_header(4, ".venv 创建"))
    if not create_venv():
        return 1

    # Step 5: pip install
    print(_format_step_header(5, "装 [transcribe] 依赖"))
    if not pip_install_extras(has_cuda):
        return 1

    # Step 6: skill deploy
    print(_format_step_header(6, "部署 Claude Code skill"))
    if not deploy_skill():
        return 1

    # Closing message
    print(
        "\n" + "═" * 60 +
        "\n✓ 安装完成。\n"
        "\n下一步：\n"
        "  1. 重启 Claude Code（或开一个新对话）\n"
        '  2. 对 Claude 说「录会议」 / 「录个会议」 / 「开始录制」 触发 skill\n'
        "  3. 跟着 Claude 的提示走（会问 4 个问题：主题/软件/灵敏度/麦克风）\n"
        "\n出问题找 Yuhang。\n"
        + "═" * 60
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
