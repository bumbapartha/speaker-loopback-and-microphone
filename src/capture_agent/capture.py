"""Explicitly started dual-device WASAPI capture; no persistence or transport."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import queue
import threading
import time
from typing import Any, Callable
import wave

from .conversion import FRAME_SAMPLES, INPUT_RATE, PreviewRing, StreamConverter
from .devices import DeviceDiscoveryError, enumerate_wasapi_devices
from .speaker_priority import SpeakerPriorityGate


FRAME_BUFFER = 1024
RAW_QUEUE_CHUNKS = 50
SILENCE_LAG_US = 250_000
MIC_STALL_US = 500_000


class CaptureError(RuntimeError):
    """Actionable fatal capture error. Both sources must stop on this error."""


def load_selection(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CaptureError(
            f"No device selection at {config_path}. Run 'capture_agent select' after choosing devices."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptureError(f"Cannot read device selection {config_path}: {exc}") from exc
    try:
        candidate = config["sources"]["candidate"]
        interviewer = config["sources"]["interviewer"]
        required = (candidate["device_index"], candidate["device_name"], candidate["sample_rate"], candidate["channels"],
                    interviewer["playback_index"], interviewer["playback_name"], interviewer["loopback_index"],
                    interviewer["loopback_name"], interviewer["sample_rate"], interviewer["channels"],
                    interviewer["playback_sample_rate"], interviewer["playback_channels"])
    except (KeyError, TypeError) as exc:
        raise CaptureError(f"Device selection {config_path} is incomplete. Re-run 'capture_agent select'.") from exc
    if any(item is None for item in required):
        raise CaptureError(f"Device selection {config_path} is incomplete. Re-run 'capture_agent select'.")
    return config


def validate_selection(config: dict[str, Any]) -> None:
    """Fail closed when stored indexes/names no longer match current WASAPI devices."""
    try:
        microphones, playbacks, loopbacks = enumerate_wasapi_devices()
    except DeviceDiscoveryError as exc:
        raise CaptureError(f"Cannot validate selected audio devices: {exc}") from exc
    candidate = config["sources"]["candidate"]
    interviewer = config["sources"]["interviewer"]
    mic = next((item for item in microphones if item.index == candidate["device_index"]), None)
    output = next((item for item in playbacks if item.index == interviewer["playback_index"]), None)
    loopback = next((item for item in loopbacks if item.index == interviewer["loopback_index"]), None)
    if mic is None or mic.name != candidate["device_name"]:
        raise CaptureError("Selected microphone is missing or its index changed. Run 'devices' and select it again.")
    if output is None or output.name != interviewer["playback_name"]:
        raise CaptureError("Selected playback device is missing or its index changed. Run 'devices' and select it again.")
    if output.loopback_index != interviewer["loopback_index"] or loopback is None or loopback.name != interviewer["loopback_name"]:
        raise CaptureError("Selected playback loopback no longer matches. Run 'devices' and select the playback device again.")
    if output.output_format_supported is not True:
        raise CaptureError("Selected playback output no longer supports the preview format. Run 'devices' again.")
    if mic.format_supported is not True or loopback.format_supported is not True:
        raise CaptureError("A selected input no longer supports the validated PCM16 format. Rerun 'devices'.")


@dataclass
class SourceRuntime:
    key: str
    label: str
    device_index: int
    device_name: str
    sample_rate: int
    channels: int
    queue: queue.Queue
    ring: PreviewRing
    converter: StreamConverter | None = None
    stream: Any = None
    worker: threading.Thread | None = None
    dropped_frames: int = 0
    packets: int = 0
    last_callback_ns: int = 0
    start_us: int = 0


class CaptureSession:
    """Owns both input handles and processing workers for one capture attempt."""

    def __init__(
        self,
        config_path: str | Path,
        *,
        recording_dir: str | Path | None = None,
        frame_sinks: dict[str, Callable[[Any], None]] | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.recording_dir = Path(recording_dir) if recording_dir is not None else None
        self.frame_sinks = frame_sinks or {}
        self.recording_paths: dict[str, Path] = {}
        self._recording_writers: dict[str, Any] = {}
        self.config = load_selection(self.config_path)
        self.stopping = threading.Event()
        self.error_event = threading.Event()
        self._error_lock = threading.Lock()
        self._fatal_message: str | None = None
        self._origin_ns = 0
        self._stop_us = 0
        self._manager: Any = None
        self._started = False
        self.speaker_priority = SpeakerPriorityGate(enabled=bool(self.config.get("speaker_priority", True)))
        candidate = self.config["sources"]["candidate"]
        interviewer = self.config["sources"]["interviewer"]
        self.sources = {
            "candidate": SourceRuntime(
                "candidate", "CANDIDATE", candidate["device_index"], candidate["device_name"],
                candidate["sample_rate"], candidate["channels"],
                queue.Queue(maxsize=max(1, min(RAW_QUEUE_CHUNKS, candidate["sample_rate"] // FRAME_BUFFER))), PreviewRing(),
            ),
            "interviewer": SourceRuntime(
                "interviewer", "INTERVIEWER", interviewer["loopback_index"], interviewer["loopback_name"],
                interviewer["sample_rate"], interviewer["channels"],
                queue.Queue(maxsize=max(1, min(RAW_QUEUE_CHUNKS, interviewer["sample_rate"] // FRAME_BUFFER))), PreviewRing(),
            ),
        }

    @property
    def fatal_message(self) -> str | None:
        with self._error_lock:
            return self._fatal_message

    def _fail(self, message: str) -> None:
        with self._error_lock:
            if self._fatal_message is None:
                self._fatal_message = message
        self.error_event.set()
        self.stopping.set()

    def _callback(self, source: SourceRuntime, pa: Any):
        def callback(in_data, frame_count, time_info, status_flags):
            if self.stopping.is_set():
                return None, pa.paAbort
            if status_flags & getattr(pa, "paInputOverflow", 0):
                self._fail(f"{source.label} input overflowed. Both captures are stopping; recheck the selected device.")
                return None, pa.paAbort
            if in_data is None:
                self._fail(f"{source.label} returned an empty input packet. Both captures are stopping.")
                return None, pa.paAbort
            received_ns = time.perf_counter_ns()
            duration_us = round(frame_count * 1_000_000 / source.sample_rate)
            timestamp_us = max(0, (received_ns - self._origin_ns) // 1000 - duration_us)
            packet = (bytes(in_data), int(frame_count), int(timestamp_us), received_ns)
            try:
                source.queue.put_nowait(packet)
                source.last_callback_ns = received_ns
                source.packets += 1
            except queue.Full:
                source.dropped_frames += math.ceil(frame_count * INPUT_RATE / source.sample_rate / FRAME_SAMPLES)
                self._fail(f"{source.label} queue overflowed. Audio processing fell behind; releasing both capture streams.")
                return None, pa.paAbort
            return None, pa.paContinue

        return callback

    def _worker(self, source: SourceRuntime) -> None:
        assert source.converter is not None
        try:
            while not self.stopping.is_set() or not source.queue.empty():
                try:
                    pcm, frame_count, timestamp_us, received_ns = source.queue.get(timeout=0.02)
                    source.converter.push(pcm, timestamp_us)
                except queue.Empty:
                    now_ns = time.perf_counter_ns()
                    if source.key == "interviewer":
                        target = max(source.start_us, (now_ns - self._origin_ns) // 1000 - SILENCE_LAG_US)
                        source.converter.append_silence_until(target)
                    elif self._started and source.last_callback_ns and not self.stopping.is_set():
                        if now_ns - source.last_callback_ns > MIC_STALL_US * 1000:
                            self._fail("Microphone stopped delivering audio; this capture is being released for recovery.")
                except CaptureError as exc:
                    self._fail(str(exc))
                    return
                except Exception as exc:
                    self._fail(f"{source.label} conversion failed: {exc}. Both captures are stopping.")
                    return
            if source.key == "interviewer":
                source.converter.append_silence_until(self._stop_us)
            source.converter.finish()
        except Exception as exc:
            self._fail(f"{source.label} processing failed: {exc}. Both captures are stopping.")
        finally:
            if source.key == "interviewer":
                self.speaker_priority.close()

    def start(self) -> None:
        if self._started:
            raise CaptureError("Capture session is already On.")
        validate_selection(self.config)
        try:
            import pyaudiowpatch as pyaudio
            from .conversion import StreamConverter

            if self.recording_dir is not None:
                self.recording_dir.mkdir(parents=True, exist_ok=False)
                for key in ("candidate", "interviewer"):
                    path = self.recording_dir / f"{key}.wav"
                    writer = wave.open(str(path), "wb")
                    writer.setnchannels(1)
                    writer.setsampwidth(2)
                    writer.setframerate(INPUT_RATE)
                    self._recording_writers[key] = writer
                    self.recording_paths[key] = path

            self._manager = pyaudio.PyAudio()
            for source in self.sources.values():
                supported = self._manager.is_format_supported(
                    source.sample_rate,
                    input_device=source.device_index,
                    input_channels=source.channels,
                    input_format=pyaudio.paInt16,
                )
                if not supported:
                    raise CaptureError(f"{source.label} PCM16 format is no longer supported. Rerun device selection.")

            self._origin_ns = time.perf_counter_ns()
            for source in self.sources.values():
                source.stream = self._manager.open(
                    format=pyaudio.paInt16,
                    channels=source.channels,
                    rate=source.sample_rate,
                    input=True,
                    input_device_index=source.device_index,
                    frames_per_buffer=FRAME_BUFFER,
                    stream_callback=self._callback(source, pyaudio),
                    start=False,
                )

            self._started = True
            for source in self.sources.values():
                source.start_us = max(0, (time.perf_counter_ns() - self._origin_ns) // 1000)
                frame_filter = (
                    self.speaker_priority.candidate_frame
                    if source.key == "candidate"
                    else self.speaker_priority.interviewer_frame
                )
                sinks: list[Callable[[Any], None]] = []
                if source.key in self._recording_writers:
                    writer = self._recording_writers[source.key]
                    sinks.append(lambda frame, output=writer: output.writeframes(frame.pcm))
                if source.key in self.frame_sinks:
                    sinks.append(self.frame_sinks[source.key])
                frame_sink = (lambda frame, outputs=tuple(sinks): [sink(frame) for sink in outputs]) if sinks else None
                source.converter = StreamConverter(
                    source.sample_rate,
                    source.channels,
                    source.ring,
                    source.start_us,
                    frame_filter=frame_filter,
                    frame_sink=frame_sink,
                )
                source.last_callback_ns = time.perf_counter_ns()
                source.worker = threading.Thread(target=self._worker, args=(source,), name=f"capture-{source.key}", daemon=True)
                source.worker.start()
                source.stream.start_stream()
        except Exception as exc:
            self._fail(str(exc) if isinstance(exc, CaptureError) else f"Could not start both audio sources: {exc}")
            self.stop(clear=True)
            if isinstance(exc, CaptureError):
                raise
            raise CaptureError(self.fatal_message or "Could not start both audio sources.") from exc

    def check_health(self) -> None:
        if self.error_event.is_set():
            raise CaptureError(self.fatal_message or "Audio capture failed. Both sources have stopped.")
        if self._started and not self.stopping.is_set():
            for source in self.sources.values():
                if source.stream is not None and not source.stream.is_active():
                    self._fail(f"{source.label} stream stopped unexpectedly. Both captures are stopping.")
                    raise CaptureError(self.fatal_message or "Audio stream stopped unexpectedly.")

    def metrics(self, key: str) -> dict[str, float | int]:
        source = self.sources[key]
        rms, peak = source.converter.level if source.converter else (0.0, 0.0)
        frames = source.converter.captured_seconds if source.converter else 0.0
        return {
            "rms": rms,
            "peak": peak,
            "captured_seconds": frames,
            "queued_frames": math.ceil(source.queue.qsize() * FRAME_BUFFER * INPUT_RATE / source.sample_rate / FRAME_SAMPLES),
            "dropped_frames": source.dropped_frames,
            "speaker_suppressed_frames": source.converter.suppressed_frames if source.converter else 0,
        }

    def stop(self, *, clear: bool) -> None:
        self.speaker_priority.close()  # Release any candidate wait before joining workers.
        if not self.stopping.is_set():
            self.stopping.set()
        self._stop_us = max(0, (time.perf_counter_ns() - self._origin_ns) // 1000) if self._origin_ns else 0
        for source in self.sources.values():
            stream = source.stream
            if stream is not None:
                try:
                    if stream.is_active():
                        stream.stop_stream()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
                source.stream = None
        for source in self.sources.values():
            if source.worker is not None:
                source.worker.join(timeout=3.0)
                if source.worker.is_alive():
                    self._fail(f"{source.label} processor did not stop promptly. Restart the agent before retrying.")
                source.worker = None
            while True:
                try:
                    source.queue.get_nowait()
                except queue.Empty:
                    break
            if clear:
                source.ring.clear()
                if source.converter:
                    source.converter.clear()
        for writer in self._recording_writers.values():
            try:
                writer.close()
            except Exception:
                pass
        self._recording_writers.clear()
        if self._manager is not None:
            try:
                self._manager.terminate()
            finally:
                self._manager = None
        self._started = False

    def clear_previews(self) -> None:
        for source in self.sources.values():
            source.ring.clear()
            if source.converter:
                source.converter.clear()

