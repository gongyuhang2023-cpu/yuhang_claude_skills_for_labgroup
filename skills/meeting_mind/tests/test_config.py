"""Test config — which backends an install has; a missing file must mean 'as before'."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meetingmind import config


def test_missing_file_is_local_only(tmp_path: Path) -> None:
    """Every install from before backends existed has no config file.

    Reading that as anything but local-only would change what those users'
    `postprocess` does the day they update the skill.
    """
    cfg = config.load(tmp_path / "nope.json")
    assert cfg.backends == ("local",)
    assert cfg.default == "local"
    assert cfg.path is None


def test_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "config.json"
    written = config.Config(backends=("spark", "local"), default="spark", spark_host="lab")
    config.save(written, path)
    cfg = config.load(path)
    assert cfg.backends == ("spark", "local")
    assert cfg.default == "spark"
    assert cfg.spark_host == "lab"
    assert cfg.spark_command == config.DEFAULT_SPARK_COMMAND
    assert cfg.path == path


def test_unknown_backends_dropped_and_default_repaired(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"backends": ["cloud", "spark"], "default": "cloud"}), encoding="utf-8")
    cfg = config.load(path)
    assert cfg.backends == ("spark",)
    assert cfg.default == "spark"


def test_corrupt_file_says_how_to_fix(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="install.py"):
        config.load(path)


def test_env_var_points_elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "elsewhere.json"
    monkeypatch.setenv(config.CONFIG_ENV, str(target))
    assert config.config_path() == target


def test_resolve_auto_is_the_default() -> None:
    cfg = config.Config(backends=("spark", "local"), default="spark")
    assert config.resolve_backend("auto", cfg) == "spark"
    assert config.resolve_backend("local", cfg) == "local"


def test_resolve_refuses_a_backend_that_is_not_set_up() -> None:
    with pytest.raises(RuntimeError, match="--profile"):
        config.resolve_backend("spark", config.Config())


def test_resolve_rejects_unknown_names() -> None:
    with pytest.raises(ValueError):
        config.resolve_backend("cloud", config.Config())
