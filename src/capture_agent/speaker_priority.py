"""Speaker-priority microphone gate based on the direct playback-loopback stream."""

from __future__ import annotations

from collections import deque
import threading
import time

from .conversion import FRAME_SAMPLES, PreviewFrame


FRAME_DURATION_US = 20_000
ALIGNMENT_MARGIN_US = 40_000
SPEAKER_HANGOVER_US = 350_000
GATE_WAIT_SECONDS = 1.2
ACTIVE_RMS_THRESHOLD = 0.002
ACTIVE_PEAK_THRESHOLD = 0.008
ZERO_PCM = bytes(FRAME_SAMPLES * 2)


class SpeakerPriorityGate:
    """Keep interviewer frames intact and silence overlapping candidate frames.

    Candidate frames wait briefly for the interviewer timeline to reach the
    same point. A bounded history of active playback intervals covers callback
    jitter and a short acoustic tail from the laptop speakers.
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._condition = threading.Condition()
        self._watermark_us = -1
        self._closed = False
        self._active: deque[tuple[int, int]] = deque(maxlen=5000)

    def interviewer_frame(self, frame: PreviewFrame) -> tuple[PreviewFrame, bool]:
        if not self.enabled:
            return frame, False
        start_us = frame.timestamp_us - ALIGNMENT_MARGIN_US
        end_us = frame.timestamp_us + FRAME_DURATION_US + SPEAKER_HANGOVER_US
        active = frame.rms >= ACTIVE_RMS_THRESHOLD or frame.peak >= ACTIVE_PEAK_THRESHOLD
        with self._condition:
            if active:
                if self._active and start_us <= self._active[-1][1]:
                    previous_start, previous_end = self._active[-1]
                    self._active[-1] = (previous_start, max(previous_end, end_us))
                else:
                    self._active.append((start_us, end_us))
            self._watermark_us = max(self._watermark_us, frame.timestamp_us + FRAME_DURATION_US)
            prune_before = self._watermark_us - 2_000_000
            while self._active and self._active[0][1] < prune_before:
                self._active.popleft()
            self._condition.notify_all()
        return frame, False

    def candidate_frame(self, frame: PreviewFrame) -> tuple[PreviewFrame, bool]:
        if not self.enabled:
            return frame, False
        frame_start = frame.timestamp_us
        frame_end = frame.timestamp_us + FRAME_DURATION_US
        timeline_target = frame_end + ALIGNMENT_MARGIN_US
        deadline = time.monotonic() + GATE_WAIT_SECONDS
        with self._condition:
            while self._watermark_us < timeline_target and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            playback_active = any(
                interval_start <= frame_end and interval_end >= frame_start
                for interval_start, interval_end in self._active
            )
            timeline_missing = self._watermark_us < timeline_target
        if playback_active or timeline_missing:
            # If the playback timeline is late, fail toward the speaker stream:
            # discard this candidate frame rather than risk retaining speaker echo.
            return PreviewFrame(frame.timestamp_us, ZERO_PCM, 0.0, 0.0), True
        return frame, False

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

