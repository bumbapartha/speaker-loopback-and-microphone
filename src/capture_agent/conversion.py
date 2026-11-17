"""Per-source PCM conversion and bounded 20 ms preview frames."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import threading
from typing import Callable

import numpy as np
import soxr


INPUT_RATE = 16_000
FRAME_SAMPLES = 320
PREVIEW_SECONDS = 10
PREVIEW_FRAME_LIMIT = PREVIEW_SECONDS * INPUT_RATE // FRAME_SAMPLES


@dataclass(frozen=True)
class PreviewFrame:
    timestamp_us: int
    pcm: bytes
    rms: float
    peak: float


class PreviewRing:
    """Ten seconds maximum, stored as normalized PCM in memory only."""

    def __init__(self) -> None:
        self._frames: deque[PreviewFrame] = deque(maxlen=PREVIEW_FRAME_LIMIT)
        self._lock = threading.Lock()
        self.total_samples = 0

    def append(self, frame: PreviewFrame) -> None:
        with self._lock:
            self._frames.append(frame)
            self.total_samples += FRAME_SAMPLES

    def snapshot(self) -> list[PreviewFrame]:
        with self._lock:
            return list(self._frames)

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()
            self.total_samples = 0


class StreamConverter:
    """Stateful native-rate to 16 kHz mono PCM converter for one input."""

    def __init__(
        self,
        sample_rate: int,
        channels: int,
        ring: PreviewRing,
        first_timestamp_us: int,
        frame_filter: Callable[[PreviewFrame], tuple[PreviewFrame, bool]] | None = None,
        frame_sink: Callable[[PreviewFrame], None] | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.ring = ring
        self.frame_filter = frame_filter
        self.frame_sink = frame_sink
        self._resampler = soxr.ResampleStream(
            sample_rate, INPUT_RATE, num_channels=1, dtype="float32", quality="HQ"
        )
        self._expected_input_us: int | None = None
        self._pending = np.empty(0, dtype=np.float32)
        self._pending_start_us = first_timestamp_us
        self._next_output_us = first_timestamp_us
        self._total_output_samples = 0
        self._lock = threading.Lock()
        self.latest_rms = 0.0
        self.latest_peak = 0.0
        self.gap_samples = 0
        self.overlap_samples = 0
        self.suppressed_frames = 0

    def _downmix(self, pcm: bytes) -> np.ndarray:
        samples = np.frombuffer(pcm, dtype="<i2")
        if len(samples) % self.channels:
            raise ValueError("Input packet ended mid-sample frame.")
        samples = samples.reshape(-1, self.channels).astype(np.float32)
        return (samples.mean(axis=1) / 32768.0).astype(np.float32, copy=False)

    def push(self, pcm: bytes, timestamp_us: int) -> None:
        mono = self._downmix(pcm)
        packet_frames = len(mono)
        if packet_frames == 0:
            return
        if self._expected_input_us is None:
            self._expected_input_us = timestamp_us
            self._pending_start_us = timestamp_us
            self._next_output_us = timestamp_us
        else:
            delta_us = timestamp_us - self._expected_input_us
            if delta_us > 5_000:
                missing = round(delta_us * self.sample_rate / 1_000_000)
                self.gap_samples += missing
                while missing:
                    count = min(missing, 4096)
                    self._resample(np.zeros(count, dtype=np.float32))
                    missing -= count
            elif delta_us < -5_000:
                overlap = min(packet_frames, round(-delta_us * self.sample_rate / 1_000_000))
                mono = mono[overlap:]
                timestamp_us += round(overlap * 1_000_000 / self.sample_rate)
                self.overlap_samples += overlap
                if not len(mono):
                    return
        self._resample(mono)
        self._expected_input_us = timestamp_us + round(len(mono) * 1_000_000 / self.sample_rate)

    def _resample(self, mono: np.ndarray) -> None:
        converted = self._resampler.resample_chunk(mono, last=False)
        if len(converted):
            self._append_output(converted)

    def finish(self) -> None:
        """Flush the stateful filter once after the input stream has stopped."""
        converted = self._resampler.resample_chunk(np.empty(0, dtype=np.float32), last=True)
        if len(converted):
            self._append_output(converted)

    def append_silence_until(self, target_us: int) -> None:
        """Add timed zero frames for idle loopback periods, bounded in memory."""
        expected = self._expected_input_us
        if expected is None:
            expected = self._next_output_us
        if target_us <= expected:
            return
        count = round((target_us - expected) * self.sample_rate / 1_000_000)
        if count <= 0:
            return
        self.gap_samples += count
        remaining = count
        while remaining:
            chunk = min(remaining, 4096)
            self._resample(np.zeros(chunk, dtype=np.float32))
            remaining -= chunk
        self._expected_input_us = target_us

    def _append_output(self, converted: np.ndarray) -> None:
        if len(self._pending) == 0:
            self._pending_start_us = self._next_output_us
        self._pending = np.concatenate((self._pending, converted.astype(np.float32, copy=False)))
        while len(self._pending) >= FRAME_SAMPLES:
            chunk = np.clip(self._pending[:FRAME_SAMPLES], -1.0, 1.0)
            pcm = np.rint(chunk * 32767.0).astype("<i2", copy=False).tobytes()
            rms = float(math.sqrt(float(np.mean(chunk * chunk))))
            peak = float(np.max(np.abs(chunk)))
            frame = PreviewFrame(self._pending_start_us, pcm, rms, peak)
            suppressed = False
            if self.frame_filter is not None:
                frame, suppressed = self.frame_filter(frame)
            if self.frame_sink is not None:
                self.frame_sink(frame)
            if suppressed:
                self.suppressed_frames += 1
            self.ring.append(frame)
            with self._lock:
                self.latest_rms = frame.rms
                self.latest_peak = frame.peak
            self._total_output_samples += FRAME_SAMPLES
            self._pending_start_us += 20_000
            self._next_output_us = self._pending_start_us
            self._pending = self._pending[FRAME_SAMPLES:]

    @property
    def captured_seconds(self) -> float:
        return self._total_output_samples / INPUT_RATE

    @property
    def level(self) -> tuple[float, float]:
        with self._lock:
            return self.latest_rms, self.latest_peak

    def clear(self) -> None:
        self._pending = np.empty(0, dtype=np.float32)
        self._resampler = None

