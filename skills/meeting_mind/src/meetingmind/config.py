"""Which transcription backends this install has, and which one is the default.

Two backends exist:
  - ``local`` — Qwen3-ASR on this machine (needs the [transcribe] extras).
  - ``spark`` — the lab server: audio goes up, a Slurm GPU job transcribes it
    with the shared environment and model, the text comes back (`remote.py`).

The choice lives in ``~/.meetingmind/config.json``, written by ``install.py``.
It sits outside the skill directory on purpose: updating or re-copying the
skill must not reset it.

**No file means local only.** Every install made before backends existed has
no config, and those installs must keep behaving exactly as they did.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

LOCAL = "local"
SPARK = "spark"
BACKENDS: tuple[str, ...] = (LOCAL, SPARK)

#: The ssh alias every lab member's setup guide configures.
DEFAULT_SPARK_HOST = "spark"
#: Absolute on purpose: a non-interactive `ssh host cmd` does not get the
#: login PATH that would otherwise contain /opt/bio/apps/bin.
DEFAULT_SPARK_COMMAND = "/opt/bio/apps/bin/meetingmind-asr"

#: Points the config somewhere else (tests; unusual setups).
CONFIG_ENV = "MEETINGMIND_CONFIG"


@dataclass(frozen=True)
class Config:
    backends: tuple[str, ...] = (LOCAL,)
    default: str = LOCAL
    spark_host: str = DEFAULT_SPARK_HOST
    spark_command: str = DEFAULT_SPARK_COMMAND
    #: Lab cloud (Nextcloud). Unset until `meetingmind cloud login`, which asks
    #: the lab server for the address and the shared meetings folder (they are
    #: not written into the published skill); the app password itself lives in
    #: the OS keychain, never here.
    cloud_url: str | None = None
    cloud_login: str | None = None
    cloud_user: str | None = None
    cloud_folder: str = ""
    #: Where this came from; None = no file, built-in defaults.
    path: Path | None = None


def config_path() -> Path:
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".meetingmind" / "config.json"


def load(path: Path | None = None) -> Config:
    """Read the config; a missing file yields the local-only default."""
    p = path or config_path()
    if not p.is_file():
        return Config()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Cannot read {p}: {exc}. Re-run install.py to rewrite it."
            f" / 读不了配置文件 {p}：{exc}。重跑 install.py 会重写它。"
        ) from exc

    backends = tuple(b for b in data.get("backends", []) if b in BACKENDS) or (LOCAL,)
    default = data.get("default")
    if default not in backends:
        default = backends[0]
    spark = data.get("spark") or {}
    cloud = data.get("cloud") or {}
    return Config(
        backends=backends,
        default=default,
        spark_host=str(spark.get("host") or DEFAULT_SPARK_HOST),
        spark_command=str(spark.get("command") or DEFAULT_SPARK_COMMAND),
        cloud_url=cloud.get("url"),
        cloud_login=cloud.get("login"),
        cloud_user=cloud.get("user"),
        cloud_folder=str(cloud.get("folder") or ""),
        path=p,
    )


def save(cfg: Config, path: Path | None = None) -> Path:
    """Write ``cfg`` atomically and return where it went."""
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {
        "backends": list(cfg.backends),
        "default": cfg.default,
        "spark": {"host": cfg.spark_host, "command": cfg.spark_command},
    }
    if cfg.cloud_url:
        data["cloud"] = {"url": cfg.cloud_url, "login": cfg.cloud_login,
                         "user": cfg.cloud_user, "folder": cfg.cloud_folder}
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def resolve_backend(requested: str, cfg: Config) -> str:
    """``auto`` → the configured default; a named backend must be set up here.

    Refusing an unconfigured backend up front beats the alternative: a missing
    torch surfacing ten lines into a model load, or an ssh error about a host
    this machine was never told about.
    """
    if requested == "auto":
        return cfg.default
    if requested not in BACKENDS:
        raise ValueError(f"unknown backend {requested!r}; expected one of {BACKENDS}")
    if requested not in cfg.backends:
        have = ", ".join(cfg.backends)
        profile = "server" if requested == SPARK else "local"
        raise RuntimeError(
            f"Backend '{requested}' is not set up on this machine (set up: {have}). "
            f"Run `python install.py --profile both` (or --profile {profile}) in the skill directory."
            f" / 本机没有配置 {requested} 转录（已配置：{have}）。"
            f"在 skill 目录运行 `python install.py --profile both`（或 --profile {profile}）。"
        )
    return requested
