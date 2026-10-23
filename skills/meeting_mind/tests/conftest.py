"""Pytest fixtures. Helpers live in `tests/helpers.py` — see the note there."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.helpers import RSSSampler


@pytest.fixture
def rss() -> Iterator[RSSSampler]:
    sampler = RSSSampler()
    with sampler:
        yield sampler


@pytest.fixture
def wav_path(tmp_path: Path) -> Path:
    """A path inside a not-yet-created subdirectory.

    Deliberately nested: WavSink.start() is responsible for mkdir'ing the
    parent, and a bug there must fail loudly in every test rather than
    silently pass because pytest already made the directory.
    """
    return tmp_path / "audio" / "recording.wav"


@pytest.fixture
def meeting_dir(tmp_path: Path) -> Path:
    d = tmp_path / "2026-08-03-test-meeting"
    (d / "slides").mkdir(parents=True)
    (d / "audio").mkdir(parents=True)
    return d
