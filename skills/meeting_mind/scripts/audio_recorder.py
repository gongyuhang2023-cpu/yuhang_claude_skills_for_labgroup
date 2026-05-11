"""
WASAPI Loopback audio recorder.
Passively captures system audio output without affecting playback.
"""

import threading
import wave
from pathlib import Path


class WasapiLoopbackRecorder:

    def __init__(self, output_path: Path):
        self._output_path = Path(output_path)
        self._pa = None
        self._stream = None
        self._wf = None
        self._recording = False
        self._lock = threading.Lock()
        self._sample_rate = None
        self._channels = None

    def start(self):
        import pyaudiowpatch as pyaudio

        self._pa = pyaudio.PyAudio()

        try:
            loopback = self._pa.get_default_wasapi_loopback()
        except OSError as e:
            self._pa.terminate()
            raise RuntimeError(
                f"No WASAPI loopback device found. "
                f"Ensure speakers/headphones are connected and set as default output. "
                f"Original error: {e}"
            )

        self._sample_rate = int(loopback["defaultSampleRate"])
        self._channels = loopback["maxInputChannels"]

        print(f"  [Audio] Device: {loopback['name']}")
        print(f"  [Audio] Sample rate: {self._sample_rate} Hz, Channels: {self._channels}")

        self._wf = wave.open(str(self._output_path), 'wb')
        self._wf.setnchannels(self._channels)
        self._wf.setsampwidth(self._pa.get_sample_size(pyaudio.paInt16))
        self._wf.setframerate(self._sample_rate)

        self._recording = True

        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=self._channels,
            rate=self._sample_rate,
            frames_per_buffer=512,
            input=True,
            input_device_index=loopback["index"],
            stream_callback=self._callback,
        )

        print(f"  [Audio] Recording to {self._output_path.name}")

    def _callback(self, in_data, frame_count, time_info, status):
        import pyaudiowpatch as pyaudio

        with self._lock:
            if self._recording and self._wf:
                self._wf.writeframes(in_data)
        return (in_data, pyaudio.paContinue)

    def stop(self) -> Path:
        with self._lock:
            self._recording = False

        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        if self._wf:
            self._wf.close()
            self._wf = None

        if self._pa:
            self._pa.terminate()
            self._pa = None

        size_mb = self._output_path.stat().st_size / (1024 * 1024) if self._output_path.exists() else 0
        print(f"  [Audio] Stopped. File size: {size_mb:.1f} MB")
        return self._output_path

    @property
    def sample_rate(self):
        return self._sample_rate

    @property
    def channels(self):
        return self._channels
