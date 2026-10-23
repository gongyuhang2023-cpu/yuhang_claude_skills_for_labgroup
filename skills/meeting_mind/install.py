"""MeetingMind 一键安装脚本（朋友圈版）.

朋友拿到 repo 后：
    python install.py                      # 会问转录在哪里跑
    python install.py --profile server     # 不问：server / local / both

转录在哪里跑（--profile）：
    server  交给实验室服务器 spark（不装 torch、不下模型；Mac / 核显电脑也能用）
    local   在这台电脑上跑（要 N 卡，多装约 5 GB 依赖 + 4.4 GB 模型）
    both    两个都装，每次转录前再选

会完成这些事：
    1. Python ≥ 3.10 检查
    2. 系统检查（Windows 10 build 19041+；其它系统警告但允许继续）
    3. 选档位（不给 --profile 时沿用已有配置；已装过本机版的保持 local；
       全新安装默认 server）
    4. CUDA / GPU 探测（nvidia-smi，仅 local / both）
    5. 创建 .venv（已存在则跳过）
    6. 装依赖：server 只装录制部分；local / both 加装 [transcribe]
       6b. 覆盖为 vendor/proctap 的补丁版 proctap（修 WASAPI loopback 静音，仅 Windows）
    7. 连 spark 自检（仅 server / both）
    8. 写配置 ~/.meetingmind/config.json，部署 Claude Code skill

整个流程跑在**系统 Python** 下（朋友的 Python，未激活 venv），
通过 subprocess 派生子进程调 .venv 里的 Python。

错误处理：失败统一 exit 1 + 打印中文修复建议 + 命令字面值保留英文。
不回滚 .venv（失败留着方便下次复用）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
VENV_DIR = REPO_ROOT / ".venv"
PYPI_CUDA_INDEX = "https://download.pytorch.org/whl/cu128"
PYTHON_DOWNLOAD_URL = "https://www.python.org/downloads/"
WINDOWS_UPDATE_URL = "https://support.microsoft.com/windows/update"
TOTAL_STEPS = 8

#: --profile → the backends written to the config (first = default).
PROFILES: dict[str, list[str]] = {
    "server": ["spark"],
    "local": ["local"],
    "both": ["spark", "local"],
}
DEFAULT_SPARK_HOST = "spark"


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
    return f"\n{bar}\n[步骤 {step_num}/{TOTAL_STEPS}] {title}\n{bar}"


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
            f"Capture API）目前只在 Windows 10 build 19041+ / Windows 11 工作，"
            f"macOS 录制正在开发。\n"
            f"可以继续装：转录（尤其 --profile server）不受影响，但 record 子命令暂时用不了。"
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
        ans = input(
            "没检测到 GPU，本机转录会慢 5-10 倍（可改用 --profile server 交给 spark）。"
            "继续装本机版吗？(Y/n): "
        ).strip().lower()
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


def _config_path() -> Path:
    """Same rule as meetingmind.config.config_path (install.py runs outside the venv)."""
    override = os.environ.get("MEETINGMIND_CONFIG")
    return Path(override).expanduser() if override else Path.home() / ".meetingmind" / "config.json"


def read_existing_profile() -> str | None:
    """已有配置对应的档位；没有或读不了则 None。"""
    try:
        data = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    backends = data.get("backends") or []
    for name, wanted in PROFILES.items():
        if sorted(wanted) == sorted(backends):
            return name
    return None


def venv_has_torch() -> bool:
    """.venv 里是否已经装了 torch —— 说明这是一次本机版安装的重跑。"""
    venv_py = _venv_python()
    if not venv_py.is_file():
        return False
    code = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('torch') else 1)"
    try:
        return subprocess.run([str(venv_py), "-c", code], timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def choose_profile(requested: str | None) -> str:
    """定下档位。没给 --profile 时**不改变已有安装的行为**：

    更新 skill 时常常是 AI 不带参数重跑本脚本。已经在用本机转录的人，
    不能因为新版默认 server 就被悄悄改成只能交给服务器。
    """
    if requested:
        return requested
    existing = read_existing_profile()
    if existing:
        default, why = existing, "沿用已有配置"
    elif venv_has_torch():
        default, why = "local", "已装过本机转录"
    else:
        default, why = "server", "全新安装"
    print(
        "转录在哪里跑？\n"
        "  server  交给实验室服务器 spark：不装 torch、不下模型，Mac / 核显电脑也能用\n"
        "  local   在这台电脑上跑：需要 N 卡，多装约 5 GB 依赖 + 4.4 GB 模型\n"
        "  both    两个都装，每次转录前再选\n"
        "（下次可直接 `python install.py --profile server|local|both`）"
    )
    try:
        ans = input(f"选择 [默认 {default}，{why}]: ").strip().lower()
    except EOFError:
        # 非交互环境（AI 代跑 / pipe）→ 用默认，并说清楚用了哪个
        print(f"（非交互运行，使用 {default}：{why}）")
        return default
    if ans in PROFILES:
        return ans
    if ans:
        print(f"⚠️ 不认识「{ans}」，使用 {default}。")
    return default


def check_spark(host: str) -> bool:
    """用 venv 里刚装好的 meetingmind 连一次 spark：能免密登录、服务器端已部署、协议一致。"""
    venv_py = _venv_python()
    code = (
        "import json, sys\n"
        "from meetingmind.config import DEFAULT_SPARK_COMMAND\n"
        "from meetingmind.remote import PROTOCOL, RemoteError, SparkClient\n"
        "try:\n"
        "    d = SparkClient(sys.argv[1], DEFAULT_SPARK_COMMAND).call('doctor', timeout=60)\n"
        "except RemoteError as e:\n"
        "    d = dict(e.data, error=str(e))\n"
        "d['client_protocol'] = PROTOCOL\n"
        "print(json.dumps(d, ensure_ascii=False))\n"
    )
    try:
        out = subprocess.run(
            [str(venv_py), "-c", code, host],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        info = json.loads(out.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError) as exc:
        print(f"❌ spark 自检没跑起来：{exc}")
        return False

    if info.get("error") and "protocol" not in info:
        print(
            f"❌ 连不上 spark 或服务器端没部署好：\n   {info['error']}\n"
            f"   检查：Tailscale 是否在线；终端里 `ssh {host}` 能否不输密码直接登录；\n"
            f"   都没问题的话，服务器端转录可能还没装好，联系管理员。"
        )
        return False
    if info.get("protocol") != info.get("client_protocol"):
        print(
            f"❌ 服务器端版本（protocol {info.get('protocol')}）和本机 skill"
            f"（protocol {info.get('client_protocol')}）不一致：先更新 skill，"
            f"仍不一致就请管理员更新服务器端。"
        )
        return False
    if not info.get("ok"):
        print(f"❌ 服务器端自检没通过，请联系管理员：{json.dumps(info, ensure_ascii=False)}")
        return False
    print(f"spark 连通 ✓（服务器端 {info.get('version')}，模型 {info.get('model_dir')}）")
    return True


def write_config(profile: str, host: str) -> bool:
    venv_py = _venv_python()
    backends = PROFILES[profile]
    # Only the transcription fields change: an existing cloud sign-in survives a re-run.
    code = (
        "import dataclasses, sys\n"
        "from meetingmind import config\n"
        "b = tuple(sys.argv[1].split(','))\n"
        "try:\n"
        "    old = config.load()\n"
        "except RuntimeError:\n"
        "    old = config.Config()\n"
        "new = dataclasses.replace(old, backends=b, default=b[0], spark_host=sys.argv[2], path=None)\n"
        "print(config.save(new))\n"
    )
    try:
        out = subprocess.run([str(venv_py), "-c", code, ",".join(backends), host],
                             capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"❌ 写配置失败：{exc}")
        return False
    print(f"配置已写入 ✓ {out.stdout.strip()}\n  转录后端：{', '.join(backends)}（默认 {backends[0]}）")
    return True


def pip_install(profile: str, has_cuda: bool) -> bool:
    """在 .venv 里装依赖：server 只装录制部分，local / both 加 [transcribe]。"""
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

    local = "local" in PROFILES[profile]
    cmd = [
        str(venv_py),
        "-m", "pip", "install",
        "-e", ".[transcribe]" if local else ".",
    ]
    if not local:
        print("装录制部分（转录交给 spark，不装 torch，几十 MB）...")
    elif has_cuda:
        cmd.extend(["--extra-index-url", PYPI_CUDA_INDEX])
        print("装 [transcribe] extras（含 CUDA torch wheel，~2 GB 下载）...")
        print(f"  额外索引: {PYPI_CUDA_INDEX}")
        print("（torch wheel 较大，可能要 3-10 分钟，看网速。请耐心等。）")
    else:
        print("装 [transcribe] extras（CPU 版 torch，~200 MB 下载）...")
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

    print("\n依赖安装成功 ✓")
    return True


def _quote_cmd(cmd: list[str]) -> str:
    """把命令列表拼成可复制粘贴的字符串，含空格的参数加双引号（Windows 友好）。"""
    return " ".join(f'"{a}"' if " " in a else a for a in cmd)


def _matching_proctap_wheel(venv_py: Path, dist_dir: Path) -> Path | None:
    """返回 dist/ 里匹配 venv 解释器 ABI 的 proctap wheel，无匹配则 None.

    通过子进程问 venv 自己的 (cpXY, platform) 标签，再按 wheel 文件名匹配 ——
    比在系统 Python 里 import packaging.tags 更可靠（install.py 跑在系统 Python 下）。
    """
    if not dist_dir.is_dir():
        return None
    wheels = sorted(dist_dir.glob("proc_tap-*.whl"))
    if not wheels:
        return None
    code = (
        "import sys, sysconfig; "
        "print(f'cp{sys.version_info.major}{sys.version_info.minor}'); "
        "print(sysconfig.get_platform().replace('-', '_').replace('.', '_'))"
    )
    try:
        out = subprocess.run(
            [str(venv_py), "-c", code],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    pytag, plat = lines[0], lines[1]
    for whl in wheels:
        if f"-{pytag}-" in whl.name and whl.name.endswith(f"-{plat}.whl"):
            return whl
    return None


def install_patched_proctap() -> bool:
    """用本地打过补丁的 proctap 覆盖 PyPI 原版（否则录音可能全静音）.

    Step 5 的 `pip install -e ".[transcribe]"` 会从 PyPI 解析 `proc-tap>=1.0.3`
    （原版，无补丁）。这里在其之后强制装 vendor/proctap 的补丁版。
    补丁内容 + 原理见 vendor/proctap/PATCH-NOTES.md。

    策略：优先装 dist/ 里匹配当前解释器的预编译 wheel（无需编译器）；找不到匹配
    wheel 则从 vendor/proctap 源码现编（需 MSVC Build Tools + Windows SDK）。
    best-effort —— 失败只警告不中止安装（skill 其余功能仍可用）。
    """
    venv_py = _venv_python()
    vendor = REPO_ROOT / "vendor" / "proctap"
    if not vendor.is_dir():
        print(
            "⚠️ 未找到 vendor/proctap，跳过补丁版 proctap 安装。\n"
            "   录音将用 PyPI 原版 proc-tap，WASAPI process-loopback 可能录出静音。"
        )
        return False

    wheel = _matching_proctap_wheel(venv_py, vendor / "dist")
    from_wheel = wheel is not None
    target = wheel if from_wheel else vendor
    if from_wheel:
        print(f"装本地补丁版 proctap（预编译 wheel）：{target.name}")
    else:
        print(
            "未找到匹配当前 Python 的预编译 wheel → 从 vendor/proctap 源码现编"
            "（需 MSVC Build Tools + Windows SDK）。"
        )

    cmd = [
        str(venv_py),
        "-m", "pip", "install",
        "--force-reinstall", "--no-deps",
        str(target),
    ]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        # 失败兜底：录音会退回 PyPI 原版 → 可能静音。按"装 wheel"还是"源码现编"分流提示。
        if from_wheel:
            hint = f"   预编译 wheel 安装异常，手动重试：\n   {_quote_cmd(cmd)}"
        else:
            hint = (
                "   源码现编失败几乎都是缺 C++ 工具链：装 MSVC Build Tools + Windows SDK 后\n"
                "   重跑 `python install.py`；或换用与 vendor/proctap/dist 内预编译 wheel\n"
                "   匹配的 Python 版本（见该 wheel 文件名上的 cpXY 标记）。"
            )
        print(
            f"\n⚠️ 补丁版 proctap 安装失败（exit {exc.returncode}）。\n"
            f"   skill 其余功能可用，但录音会用 PyPI 原版 proc-tap，\n"
            f"   WASAPI process-loopback 可能录出静音。修复详见 vendor/proctap/PATCH-NOTES.md。\n"
            f"{hint}"
        )
        return False

    print("\n补丁版 proctap 安装成功 ✓（录音用打补丁的 WASAPI loopback）")
    return True


def deploy_skill() -> bool:
    """部署 Claude Code skill 到 ~/.codex/skills/meeting_mind/.

    两种运行场景：
      - **dev 模式**（MeetingMind 项目根）：`skill/install.py` 存在 →
        调它把 `skill/SKILL.md` 拷到 ~/.codex/skills/meeting_mind/。
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
    print("部署 Claude Code skill 到 ~/.codex/skills/meeting_mind/...")
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MeetingMind 安装")
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        default=None,
        help="转录在哪里跑：server（交给 spark）/ local（本机）/ both。不给就询问；"
             "非交互时沿用已有安装，全新安装用 server。",
    )
    parser.add_argument(
        "--spark-host",
        default=DEFAULT_SPARK_HOST,
        help=f"连 spark 用的 ssh 别名（默认 {DEFAULT_SPARK_HOST}，即服务器指南里配的那个）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _harden_stdio_encoding()  # before argparse: --help prints Chinese
    args = parse_args(argv)
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

    # Step 2: 系统
    print(_format_step_header(2, "系统检查"))
    check_windows()  # 返回值忽略 — 非 Windows / 旧版本都是警告不拦

    # Step 3: 档位
    print(_format_step_header(3, "选择转录方式"))
    profile = choose_profile(args.profile)
    local = "local" in PROFILES[profile]
    server = "spark" in PROFILES[profile]
    print(f"档位：{profile}")

    # Step 4: CUDA（只有本机转录需要）
    print(_format_step_header(4, "CUDA / GPU 探测"))
    has_cuda = False
    if local:
        gpu_name = detect_cuda()
        has_cuda = gpu_name is not None
        if not has_cuda and not confirm_continue_without_gpu():
            print("\n用户取消安装。.venv 未创建，无副作用。")
            return 1
    else:
        print("server 档不在本机跑模型，跳过。")

    # Step 5: venv
    print(_format_step_header(5, ".venv 创建"))
    if not create_venv():
        return 1

    # Step 6: pip install
    print(_format_step_header(6, "装依赖"))
    if not pip_install(profile, has_cuda):
        return 1

    # Step 6b: 用本地补丁版 proctap 覆盖 PyPI 原版（否则录音可能静音）
    print("\n— 步骤 6b：覆盖为本地补丁版 proctap（vendor/proctap）—")
    if platform.system() == "Windows":
        proctap_patched = install_patched_proctap()  # best-effort：失败只警告，不中止安装
    else:
        proctap_patched = True
        print("非 Windows，跳过（补丁只针对 Windows 的 WASAPI 回环）。")

    # Step 7: spark 自检
    print(_format_step_header(7, "连 spark 自检"))
    spark_ok = True
    if server:
        spark_ok = check_spark(args.spark_host)
        if not spark_ok and local:
            print("⚠️ 服务器转录暂时用不了，本机转录不受影响；处理好后重跑本脚本即可。")
    else:
        print("local 档不用 spark，跳过。")

    # Step 8: 配置 + skill
    print(_format_step_header(8, "写配置、部署 Claude Code skill"))
    if not write_config(profile, args.spark_host):
        return 1
    if not deploy_skill():
        return 1

    where = {
        "server": "录完后交给 spark 排队转录（音频转完即删）",
        "local": "录完后在这台电脑上转录",
        "both": "每次转录前会问你：交给 spark 还是本机",
    }[profile]
    print(
        "\n" + "═" * 60 +
        f"\n✓ 安装完成（档位 {profile}：{where}）。\n"
        "\n下一步：\n"
        "  1. 重启 Claude Code（或开一个新对话）\n"
        '  2. 对 Claude 说「录会议」 / 「录个会议」 / 「开始录制」 触发 skill\n'
        "  3. 跟着 Claude 的提示走（会问 4 个问题：主题/软件/灵敏度/麦克风）\n"
        "\n实验室云盘（查有没有人传过同一场会、上传结果）：第一次用到时会在浏览器里请你\n"
        "登录实验室账号授权一次；也可以现在就运行 `.venv 里的 python -m meetingmind cloud login`。\n"
        "\n出问题找 Yuhang。\n"
        + "═" * 60
    )
    if not proctap_patched:
        print(
            "\n⚠️ 重要：录音补丁（proctap）未生效，会议音频可能录成静音 —— "
            "按上方「步骤 6b」的提示修复后再录。"
        )
    if not spark_ok and not local:
        print(
            "\n❌ server 档需要连得上 spark，现在还不行 —— 按上方「步骤 7」的提示处理后，"
            "重跑 `python install.py --profile server`。"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
