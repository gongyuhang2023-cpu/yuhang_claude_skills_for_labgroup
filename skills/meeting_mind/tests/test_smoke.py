"""Smoke tests: the package imports and the test helpers behave as advertised."""

from __future__ import annotations

import numpy as np

from tests.helpers import (
    UNALIGNED_CHUNK_SIZES,
    chunked,
    legacy_int16_bytes,
    make_float32_samples,
    to_pcm_bytes,
)


def test_package_imports() -> None:
    import meetingmind

    assert meetingmind.__name__ == "meetingmind"


def test_generator_is_deterministic() -> None:
    a = make_float32_samples(1000)
    b = make_float32_samples(1000)
    assert np.array_equal(a, b)
    assert make_float32_samples(1000, seed=99).tobytes() != a.tobytes()


def test_generator_contains_edge_values() -> None:
    x = make_float32_samples(64)
    assert x[0] == np.float32(1.0)
    assert x[1] == np.float32(-1.0)
    assert x[2] == np.float32(0.0)
    # Sub-int16-resolution values must survive as non-zero floats, otherwise
    # the "accumulate RMS from float32, not int16" test has nothing to catch.
    assert x[3] != 0.0
    assert np.clip(x[3] * 32767.0, -32768.0, 32767.0).astype(np.int16) == 0


def test_chunking_is_lossless_and_unaligned() -> None:
    data = to_pcm_bytes(make_float32_samples(50_000))
    pieces = list(chunked(data))
    assert b"".join(pieces) == data
    # Every configured size must break stereo frame alignment; that is the
    # whole point of the residue tests downstream.
    assert all(n % 8 != 0 for n in UNALIGNED_CHUNK_SIZES)


def test_legacy_reference_truncates_to_whole_frames() -> None:
    # 7 samples of stereo = 3 whole frames + 1 orphan sample.
    x = make_float32_samples(7)
    out = legacy_int16_bytes(x, channels=2)
    assert len(out) == 3 * 2 * 2  # 3 frames x 2 channels x int16


def test_rss_sampler_reports_growth(rss) -> None:
    assert rss.baseline > 0
    ballast = bytearray(64 * 1024 * 1024)
    ballast[::4096] = b"\x01" * len(ballast[::4096])  # touch pages so RSS moves
    assert len(ballast) == 64 * 1024 * 1024
    del ballast
