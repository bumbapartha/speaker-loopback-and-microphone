"""Playback of bounded previews after capture has fully stopped."""

from __future__ import annotations

import numpy as np
import pyaudiowpatch as pyaudio
import soxr
import wave
from pathlib import Path

from .conversion import INPUT_RATE, PreviewFrame
from .capture import CaptureError
from .devices import enumerate_wasapi_devices


def play_preview(frames: list[PreviewFrame], device_index: int, device_name: str, rate: int, channels: int) -> None:
    if not frames:
        print("  No preview frames were captured for this source.")
        return
    _, playbacks, _ = enumerate_wasapi_devices()
    current = next((item for item in playbacks if item.index == device_index), None)
    if current is None or current.name != device_name or current.output_format_supported is not True:
        raise CaptureError("Selected preview output changed or is unavailable. Re-run device selection; no other device was used.")
    pcm = b"".join(frame.pcm for frame in frames)
    mono = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    resampler = soxr.ResampleStream(INPUT_RATE, rate, num_channels=1, dtype="float32", quality="HQ")
    output = resampler.resample_chunk(mono, last=True)
    output = np.clip(output, -1.0, 1.0)
    samples = np.rint(output * 32767.0).astype("<i2", copy=False)
    if channels > 1:
        samples = np.repeat(samples[:, None], channels, axis=1).reshape(-1)
    manager = pyaudio.PyAudio()
    stream = None
    try:
        stream = manager.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=rate,
            output=True,
            output_device_index=device_index,
            start=True,
        )
        bytes_per_chunk = rate * channels * 2 // 5
        output_bytes = samples.tobytes()
        for offset in range(0, len(output_bytes), bytes_per_chunk):
            stream.write(output_bytes[offset : offset + bytes_per_chunk])
    finally:
        try:
            if stream is not None:
                try:
                    if stream.is_active():
                        stream.stop_stream()
                finally:
                    stream.close()
        finally:
            manager.terminate()


def save_preview_wav(frames: list[PreviewFrame], destination: str | Path) -> Path:
    """Write one normalized, mono preview WAV after capture has stopped."""
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(INPUT_RATE)
        wav_file.writeframes(b"".join(frame.pcm for frame in frames))
    return path

