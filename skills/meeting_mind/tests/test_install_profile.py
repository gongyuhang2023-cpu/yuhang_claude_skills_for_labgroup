"""Test install.py's profile choice — re-running it must never change an existing install."""

from __future__ import annotations

import builtins
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

_INSTALL = Path(__file__).resolve().parents[1] / "install.py"


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("mm_install", _INSTALL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("MEETINGMIND_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(module, "venv_has_torch", lambda: False)

    def no_terminal(_prompt: str = "") -> str:
        raise EOFError  # an AI agent or a pipe running the installer

    monkeypatch.setattr(builtins, "input", no_terminal)
    return module


def _config(tmp_path: Path, backends: list[str]) -> None:
    (tmp_path / "config.json").write_text(json.dumps({"backends": backends}), encoding="utf-8")


def test_explicit_profile_wins(install: ModuleType, tmp_path: Path) -> None:
    _config(tmp_path, ["local"])
    assert install.choose_profile("server") == "server"


def test_fresh_install_defaults_to_server(install: ModuleType) -> None:
    assert install.choose_profile(None) == "server"


def test_rerun_keeps_the_configured_profile(install: ModuleType, tmp_path: Path) -> None:
    _config(tmp_path, ["local", "spark"])
    assert install.choose_profile(None) == "both"


def test_rerun_of_an_old_local_install_stays_local(install: ModuleType,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """An install from before profiles: torch in the venv, no config file.

    Updating the skill usually means an AI re-running install.py without
    arguments; that must not quietly turn local transcription into spark-only.
    """
    monkeypatch.setattr(install, "venv_has_torch", lambda: True)
    assert install.choose_profile(None) == "local"


def test_profiles_map_to_backends(install: ModuleType) -> None:
    assert install.PROFILES == {"server": ["spark"], "local": ["local"], "both": ["spark", "local"]}
