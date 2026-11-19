"""Windows Teams routing and monitoring with simulated audio and Win32 APIs."""

import contextlib
import json
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from meetingmind import audio, session, teams_audio
from meetingmind.cli import main
from meetingmind.wavsink import _float32_to_int16
from tests.fakes import FakeMicrophone
from tests.helpers import wait_until

SOURCE = "{0.0.0.00000000}.{12345678-1234-1234-1234-123456789abc}"


class WindowsSys:
    platform = "win32"

    @property
    def stderr(self):
        return sys.stderr


@pytest.fixture
def route(monkeypatch, tmp_path):
    appdata = tmp_path / "appdata"
    path = appdata / "MeetingMind" / "teams-audio.json"
    path.parent.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))
    monkeypatch.setattr(session, "sys", WindowsSys())
    monkeypatch.setattr(audio, "sys", session.sys)
    monkeypatch.setitem(sys.modules, "pythoncom", SimpleNamespace(
        COINIT_MULTITHREADED=0, CoInitializeEx=lambda _: None, CoUninitialize=lambda: None,
    ))
    return path


@pytest.fixture
def mutexes(monkeypatch):
    locks = {}
    contended = threading.Event()

    def create(_security, initial_owner, name):
        assert not initial_owner and name.startswith("Local\\MeetingMind.TeamsAudio.")
        return SimpleNamespace(lock=locks.setdefault(name, threading.Lock()), Close=lambda: None)

    def wait(handle, milliseconds):
        assert milliseconds == 0
        if handle.lock.acquire(blocking=False):
            return 0
        contended.set()
        return 258

    monkeypatch.setitem(sys.modules, "win32event", SimpleNamespace(
        CreateMutex=create, WaitForSingleObject=wait, ReleaseMutex=lambda handle: handle.lock.release(),
        WAIT_OBJECT_0=0, WAIT_ABANDONED=128, WAIT_TIMEOUT=258,
    ))
    monkeypatch.setattr(teams_audio, "_RETRY_SECONDS", 0.02)
    return contended


@pytest.mark.parametrize("alias", ["teams", "ms-teams", "ms-teams.exe"])
def test_configured_record_uses_exact_virtual_id(route, monkeypatch, tmp_path, capsys, alias):
    route.write_text(json.dumps({"capture_device_id": SOURCE}), encoding="utf-8-sig")
    data = np.full((4800, 2), 0.25, dtype=np.float32)
    mic = FakeMicrophone(name="Test Virtual Speakers", data=data)
    mic.id = SOURCE
    wrong = SimpleNamespace(id="another-id", name=mic.name, isloopback=True)
    monitor_called = threading.Event()

    def start_monitor(source, stop, rate, channels):
        if (source, rate, channels) == (SOURCE, 48000, 2):
            monitor_called.set()
        stop.wait(timeout=3)

    monkeypatch.setitem(sys.modules, "soundcard", SimpleNamespace(
        all_microphones=lambda include_loopback: [wrong, mic],
        default_speaker=lambda: SimpleNamespace(id="physical"),
    ))
    monkeypatch.setattr(teams_audio, "monitor", start_monitor)

    def inspect_recording(sess, **_):
        meta = json.loads((sess.meeting_dir / "metadata.json").read_text(encoding="utf-8"))
        assert meta["recording_status"] == "recording"
        assert meta["audio_device"] == f"{mic.name} ({SOURCE})"
        assert meta["window_keyword"] == "Teams"
        assert monitor_called.wait(timeout=2)
        assert wait_until(lambda: sess._recorder.duration_seconds >= 0.1, timeout=2)

    monkeypatch.setattr(session.MeetingSession, "wait", inspect_recording)
    assert main(["record", "--process", alias, "--system-audio", "--no-slides",
                 "--quiet", "--output-root", str(tmp_path)]) == 0
    output = capsys.readouterr()
    assert SOURCE in output.err
    (wav,) = tmp_path.glob("*/audio/recording.wav")
    assert wav.read_bytes()[44:] == _float32_to_int16(data.reshape(-1)).tobytes()


@pytest.mark.parametrize("fault, error", [
    ("json", "Cannot read Teams audio configuration"),
    ("empty", "requires capture_device_id"),
    ("missing", "Configured Teams render loopback unavailable"),
    ("broken", "capture device busy"),
    ("feedback", "Windows default output is the Teams capture endpoint"),
])
def test_bad_route_fails_start_without_wav(route, monkeypatch, tmp_path, capsys, fault, error):
    contents = {"json": "{", "empty": "{}"}.get(fault, json.dumps({"capture_device_id": SOURCE}))
    route.write_text(contents, encoding="utf-8")
    mic = FakeMicrophone(open_error=RuntimeError("capture device busy"))
    mic.id = SOURCE
    monkeypatch.setitem(sys.modules, "soundcard", SimpleNamespace(
        all_microphones=lambda include_loopback: [] if fault == "missing" else [mic],
        default_speaker=lambda: SimpleNamespace(id=SOURCE if fault == "feedback" else "physical"),
    ))
    assert main(["record", "--process", "teams", "--system-audio", "--no-slides",
                 "--quiet", "--output-root", str(tmp_path)]) == 2
    assert error in capsys.readouterr().err
    assert not list(tmp_path.glob("*/audio/recording.wav"))


@pytest.mark.parametrize("args, present", [
    (["--process", "teams"], False),
    ([], True),
    (["--process", "zoom"], True),
    (["--process", "teams", "--pid", "42"], True),
])
def test_other_system_recordings_keep_default_output(route, monkeypatch, tmp_path, args, present):
    if present:
        route.write_text("{", encoding="utf-8")  # Would fail if read.
    mic = FakeMicrophone(data=np.full((4800, 2), 0.25, dtype=np.float32))
    monkeypatch.setitem(sys.modules, "soundcard", SimpleNamespace(
        default_speaker=lambda: SimpleNamespace(name="Physical"),
        get_microphone=lambda name, include_loopback: mic,
    ))
    assert main(["record", "--system-audio", "--no-slides", "--duration", "0.01",
                 "--quiet", "--output-root", str(tmp_path), *args]) == 0


def test_monitor_is_independent_and_hands_over_after_stop(route, mutexes, monkeypatch, tmp_path):
    raw_data = np.full((9600, 2), 0.25, dtype=np.float32)
    mic = FakeMicrophone(data=raw_data)
    mic.id = SOURCE
    playing = threading.Event()
    release_player = threading.Event()
    threads = []

    def play(chunk):
        playing.set()
        release_player.wait(timeout=4)

    @contextlib.contextmanager
    def player(**options):
        assert options == {"samplerate": 48000, "channels": 2, "blocksize": 2400}
        threads.append(threading.current_thread())
        yield SimpleNamespace(play=play)

    speaker = SimpleNamespace(id="physical", name="Physical", player=player)
    monkeypatch.setitem(sys.modules, "soundcard", SimpleNamespace(
        all_microphones=lambda include_loopback: [mic], default_speaker=lambda: speaker,
    ))
    first, second = [audio.SystemAudioRecorder(tmp_path / f"{i}.wav", teams_device_id=SOURCE)
                     for i in range(2)]
    try:
        first.start()
        assert playing.wait(timeout=2)
        second.start()
        assert mutexes.wait(timeout=2)
        assert wait_until(lambda: second.duration_seconds >= 0.2, timeout=2)
        assert len(threads) == 1
        # The raw WAV finishes while the soundcard player is still blocked.
        first.stop()
        assert (tmp_path / "0.wav").read_bytes()[44:] == _float32_to_int16(raw_data.reshape(-1)).tobytes()
        assert threads[0].is_alive()
        release_player.set()
        assert wait_until(lambda: len(threads) == 2, timeout=2)
        assert wait_until(lambda: not threads[0].is_alive(), timeout=2)
    finally:
        release_player.set()
        first.stop()
        second.stop()
    assert all(not thread.is_alive() for thread in threads)


def test_monitor_resumes_on_new_default_and_never_plays_to_source(route, mutexes, monkeypatch, capsys):
    opened = []
    stop = threading.Event()
    mic = FakeMicrophone(data=np.full((4800, 2), 0.25, dtype=np.float32))
    mic.id = SOURCE
    default = "old"

    def play(_chunk):
        nonlocal default
        if default == "old":
            default = SOURCE
        elif default == "new":
            stop.set()

    def speaker():
        nonlocal default
        current = default
        if current == SOURCE:
            default = "new"

        def player(**_):
            opened.append(current)
            return contextlib.nullcontext(SimpleNamespace(play=play))

        return SimpleNamespace(id=current, name=current, player=player)

    monkeypatch.setitem(sys.modules, "soundcard", SimpleNamespace(
        all_microphones=lambda include_loopback: [mic], default_speaker=speaker,
    ))
    worker = threading.Thread(target=teams_audio.monitor, args=(SOURCE, stop, 48000, 2), daemon=True)
    worker.start()
    try:
        assert stop.wait(timeout=2)
    finally:
        stop.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert opened == ["old", "new"]
    assert "Windows default output is the Teams capture endpoint" in capsys.readouterr().err
