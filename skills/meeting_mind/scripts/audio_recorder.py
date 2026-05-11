"""
WASAPI audio recorder.
Supports two modes:
  1. System audio only (loopback)
  2. System audio + microphone mixed into one file
"""

import threading
import wave
from pathlib import Path

import numpy as np


class WasapiLoopbackRecorder:

    def __init__(self, output_path: Path, record_mic: bool = False):
        self._output_path = Path(output_path)
        self._record_mic = record_mic
        self._pa = None
        self._loopback_stream = None
        self._mic_stream = None
        self._wf = None
        self._recording = False
        self._lock = threading.Lock()
        self._sample_rate = None
        self._channels = None
        self._mic_rate = None
        self._mic_channels = None
        self._mic_buffer = bytearray()
        self._loopback_buffer = bytearray()

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

        print(f"  [Audio] Loopback: {loopback['name']}")
        print(f"  [Audio] Sample rate: {self._sample_rate} Hz, Channels: {self._channels}")

        if self._record_mic:
            mic_info = self._pa.get_default_input_device_info()
            self._mic_rate = int(mic_info["defaultSampleRate"])
            self._mic_channels = mic_info["maxInputChannels"]
            print(f"  [Audio] Mic: {mic_info['name']}")
            print(f"  [Audio] Mic rate: {self._mic_rate} Hz, Channels: {self._mic_channels}")

        out_channels = 2
        self._wf = wave.open(str(self._output_path), 'wb')
        self._wf.setnchannels(out_channels)
        self._wf.setsampwidth(self._pa.get_sample_size(pyaudio.paInt16))
        self._wf.setframerate(self._sample_rate)

        self._recording = True

        self._loopback_stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=self._channels,
            rate=self._sample_rate,
            frames_per_buffer=512,
            input=True,
            input_device_index=loopback["index"],
            stream_callback=self._loopback_callback if self._record_mic else self._simple_callback,
        )

        if self._record_mic:
            self._mic_stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=self._mic_channels,
                rate=self._mic_rate,
                frames_per_buffer=512,
                input=True,
                stream_callback=self._mic_callback,
            )
            self._mix_thread = threading.Thread(target=self._mix_loop, daemon=True)
            self._mix_thread.start()

        mode = "system + mic" if self._record_mic else "system only"
        print(f"  [Audio] Recording ({mode}) to {self._output_path.name}")

    def _simple_callback(self, in_data, frame_count, time_info, status):
        import pyaudiowpatch as pyaudio
        with self._lock:
            if self._recording and self._wf:
                stereo = self._to_stereo(in_data, self._channels)
                self._wf.writeframes(stereo)
        return (in_data, pyaudio.paContinue)

    def _loopback_callback(self, in_data, frame_count, time_info, status):
        import pyaudiowpatch as pyaudio
        with self._lock:
            self._loopback_buffer.extend(in_data)
        return (in_data, pyaudio.paContinue)

    def _mic_callback(self, in_data, frame_count, time_info, status):
        import pyaudiowpatch as pyaudio
        with self._lock:
            self._mic_buffer.extend(in_data)
        return (in_data, pyaudio.paContinue)

    def _to_stereo(self, raw: bytes, src_channels: int) -> bytes:
        samples = np.frombuffer(raw, dtype=np.int16)
        if src_channels == 1:
            stereo = np.column_stack([samples, samples])
        elif src_channels == 2:
            return raw
        else:
            samples = samples.reshape(-1, src_channels)
            left = samples[:, 0]
            right = samples[:, 1] if src_channels > 1 else left
            stereo = np.column_stack([left, right])
        return stereo.astype(np.int16).tobytes()

    def _resample(self, data: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
        if src_rate == dst_rate:
            return data
        ratio = dst_rate / src_rate
        new_len = int(len(data) * ratio)
        indices = np.linspace(0, len(data) - 1, new_len).astype(int)
        return data[indices]

    def _mix_loop(self):
        bytes_per_sample = 2
        chunk_duration = 0.05  # 50ms chunks
        loopback_chunk = int(self._sample_rate * chunk_duration) * self._channels * bytes_per_sample
        mic_chunk = int(self._mic_rate * chunk_duration) * self._mic_channels * bytes_per_sample

        while self._recording:
            threading.Event().wait(timeout=chunk_duration)

            with self._lock:
                has_loopback = len(self._loopback_buffer) >= loopback_chunk
                has_mic = len(self._mic_buffer) >= mic_chunk

                if not has_loopback:
                    continue

                lb_raw = bytes(self._loopback_buffer[:loopback_chunk])
                del self._loopback_buffer[:loopback_chunk]

                if has_mic:
                    mic_raw = bytes(self._mic_buffer[:mic_chunk])
                    del self._mic_buffer[:mic_chunk]
                else:
                    mic_raw = None

            lb_samples = np.frombuffer(lb_raw, dtype=np.int16)
            if self._channels > 2:
                lb_samples = lb_samples.reshape(-1, self._channels)[:, 0]
            elif self._channels == 2:
                lb_samples = lb_samples.reshape(-1, 2)
                lb_samples = ((lb_samples[:, 0].astype(np.int32) + lb_samples[:, 1].astype(np.int32)) // 2).astype(np.int16)
            lb_mono = lb_samples

            if mic_raw is not None:
                mic_samples = np.frombuffer(mic_raw, dtype=np.int16)
                if self._mic_channels > 1:
                    mic_samples = mic_samples.reshape(-1, self._mic_channels)[:, 0]
                mic_mono = self._resample(mic_samples, self._mic_rate, self._sample_rate)

                min_len = min(len(lb_mono), len(mic_mono))
                lb_mono = lb_mono[:min_len]
                mic_mono = mic_mono[:min_len]

                mixed = np.clip(
                    lb_mono.astype(np.int32) + mic_mono.astype(np.int32),
                    -32768, 32767
                ).astype(np.int16)
            else:
                mixed = lb_mono

            stereo = np.column_stack([mixed, mixed]).astype(np.int16).tobytes()

            with self._lock:
                if self._recording and self._wf:
                    self._wf.writeframes(stereo)

    def stop(self) -> Path:
        with self._lock:
            self._recording = False

        if hasattr(self, '_mix_thread') and self._mix_thread:
            self._mix_thread.join(timeout=2)

        for stream in [self._loopback_stream, self._mic_stream]:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
        self._loopback_stream = None
        self._mic_stream = None

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
        return 2
