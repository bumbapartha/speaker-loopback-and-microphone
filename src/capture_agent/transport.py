"""Protocol-v1 WebSocket sender for normalized capture frames."""

from __future__ import annotations

import asyncio
import base64
from collections import deque
import json
import queue
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from .capture import CaptureError, CaptureSession
from .conversion import PreviewFrame


PROTOCOL = 1
DEFAULT_RECEIVER_URL = "ws://192.168.1.103:8765/ws/windows-agent"
FRAME_BYTES = 640
FRAME_MS = 20
MAX_CONTROL_BYTES = 4096
LOCAL_QUEUE_FRAMES = 50
LIVENESS_TIMEOUT_SECONDS = 5.0


class TransportError(RuntimeError):
    """Connection or protocol failure that requires both capture streams to stop."""


def validate_receiver_url(url: str) -> str:
    value = url.strip()
    parts = urlsplit(value)
    if parts.scheme != "ws" or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise TransportError("Enter the Mac receiver URL, for example ws://192.168.1.50:8765/ws/windows-agent.")
    try:
        port = parts.port
    except ValueError as exc:
        raise TransportError("Receiver URL contains an invalid port.") from exc
    if port is not None and not 1 <= port <= 65535:
        raise TransportError("Receiver URL port must be between 1 and 65535.")
    if len(value.encode("utf-8")) > 512:
        raise TransportError("Receiver URL is too long.")
    return value


class WebSocketCaptureSender:
    """Owns one explicit receiver connection and one dual-device capture session."""

    def __init__(
        self,
        url: str,
        config_path: str,
    ) -> None:
        self.url = validate_receiver_url(url)
        self.config_path = config_path
        self.capture_id = str(uuid4())
        self.stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._state = "OFF"
        self._detail = ""
        self._error: str | None = None
        self._session: CaptureSession | None = None
        self._queues = {
            source: queue.Queue(maxsize=LOCAL_QUEUE_FRAMES)
            for source in ("candidate", "interviewer")
        }
        self._sent = {source: 0 for source in self._queues}
        self._acked = {source: 0 for source in self._queues}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.stop_requested.clear()
        self._thread = threading.Thread(target=self._thread_main, name="windows-agent-websocket", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_requested.set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state, detail, error = self._state, self._detail, self._error
            sent, acked = dict(self._sent), dict(self._acked)
            session = self._session
        metrics = {}
        if session is not None:
            for source in ("candidate", "interviewer"):
                try:
                    metrics[source] = session.metrics(source)
                except Exception:
                    pass
        return {
            "state": state,
            "detail": detail,
            "error": error,
            "sent": sent,
            "acked": acked,
            "metrics": metrics,
            "queued": {source: outgoing.qsize() for source, outgoing in self._queues.items()},
        }

    def _set_state(self, state: str, detail: str = "", error: str | None = None) -> None:
        with self._lock:
            self._state, self._detail, self._error = state, detail, error

    def _enqueue(self, source: str) -> Callable[[PreviewFrame], None]:
        target = self._queues[source]

        def add(frame: PreviewFrame) -> None:
            try:
                target.put_nowait(frame)
            except queue.Full as exc:
                with self._lock:
                    sent = self._sent[source]
                    acked = self._acked[source]
                raise CaptureError(
                    f"{source.title()} transport queue reached {LOCAL_QUEUE_FRAMES} frames "
                    f"(sent {sent}, acknowledged {acked}). "
                    "Both audio streams are stopping; check receiver acknowledgements and network speed."
                ) from exc

        return add

    @staticmethod
    async def _send_json(websocket: Any, message: dict[str, Any], *, timeout: float = 5.0) -> None:
        payload = json.dumps(message, separators=(",", ":"))
        if len(payload.encode("utf-8")) > MAX_CONTROL_BYTES:
            raise TransportError("Outgoing protocol message exceeds the 4 KiB limit.")
        await asyncio.wait_for(websocket.send(payload), timeout=timeout)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:
            self._set_state("OFF", "Connection stopped.", str(exc))
        finally:
            with self._lock:
                if self._state not in ("OFF",):
                    self._state = "OFF"
                if not self._detail:
                    self._detail = "Connection closed; both capture streams are Off."

    async def _run(self) -> None:
        from websockets.asyncio.client import connect

        self._set_state("CONNECTING", "Connecting to the Mac receiver…")
        session: CaptureSession | None = None
        remote_terminal = False
        start_sent = False
        reason = "Stopped; both audio streams are Off."
        try:
            if self.stop_requested.is_set():
                reason = "Cancelled before connecting; audio devices remain Off."
                return
            async with connect(
                self.url,
                open_timeout=5,
                close_timeout=2,
                max_size=MAX_CONTROL_BYTES,
                max_queue=16,
                ping_interval=None,
                compression=None,
                proxy=None,
            ) as websocket:
                if self.stop_requested.is_set():
                    reason = "Cancelled before sending capture.start; audio devices remain Off."
                    return
                try:
                    await self._send_json(websocket, {
                        "type": "capture.start",
                        "protocol": PROTOCOL,
                        "captureId": self.capture_id,
                        "sampleRate": 16000,
                        "channels": 1,
                        "format": "pcm_s16le",
                        "sources": ["candidate", "interviewer"],
                    })
                    start_sent = True
                    self._set_state("WAITING", "Connected; waiting for capture.accepted. Audio devices remain Off.")
                    deadline = time.monotonic() + 10
                    accepted_message = None
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TransportError("Timed out waiting for capture.accepted; audio devices remain Off.")
                        try:
                            raw = await asyncio.wait_for(websocket.recv(), timeout=min(remaining, 0.1))
                        except asyncio.TimeoutError:
                            if self.stop_requested.is_set():
                                reason = "Cancelled while waiting for receiver acceptance; audio devices remain Off."
                                break
                            continue
                        accepted_message = self._decode_message(raw)
                        if accepted_message.get("type") in ("capture.accepted", "capture.stop", "error", "capture.error"):
                            break
                        # Informational messages may be added without breaking protocol 1 startup.
                    if accepted_message is None:
                        if start_sent:
                            await self._send_json(websocket, {"type": "capture.stop", "captureId": self.capture_id})
                    elif accepted_message.get("type") == "capture.stop":
                        if accepted_message.get("captureId") != self.capture_id:
                            raise TransportError("Receiver stop captureId did not match this connection.")
                        remote_terminal = True
                        reason = f"Receiver stopped capture: {accepted_message.get('reason', 'unspecified reason')}"
                    elif accepted_message.get("type") in ("error", "capture.error"):
                        raise TransportError(f"Receiver rejected capture: {accepted_message.get('message', accepted_message)}")
                    elif accepted_message.get("type") != "capture.accepted":
                        raise TransportError("Receiver did not send capture.accepted; audio devices remain Off.")
                    else:
                        params = self._validate_accepted(accepted_message)
                        accepted_at = time.monotonic()
                        if self.stop_requested.is_set():
                            reason = "Cancelled before capture started; both audio streams remain Off."
                        else:
                            await self._send_json(websocket, {"type": "capture.ping", "captureId": self.capture_id})
                            frame_sinks = {source: self._enqueue(source) for source in self._queues}
                            session = CaptureSession(self.config_path, frame_sinks=frame_sinks)
                            with self._lock:
                                self._session = session
                            session.start()
                            self._set_state("ON", f"Capture and streaming On · {self.capture_id}")
                            reason, remote_terminal = await self._stream(websocket, session, params, accepted_at)
                except Exception as exc:
                    reason = str(exc)
                    self._set_state("STOPPING", reason, error=reason)
                    if session is not None:
                        await asyncio.to_thread(session.stop, clear=True)
                    if start_sent and not remote_terminal:
                        try:
                            await asyncio.wait_for(
                                self._send_json(websocket, {"type": "capture.stop", "captureId": self.capture_id}, timeout=1.0),
                                timeout=1.0,
                            )
                        except Exception:
                            pass
                    raise
                if session is not None:
                    await asyncio.to_thread(session.stop, clear=True)
                if start_sent and not remote_terminal:
                    await self._send_json(websocket, {"type": "capture.stop", "captureId": self.capture_id})
        except Exception as exc:
            reason = str(exc)
            if self.snapshot()["error"] is None:
                self._set_state("STOPPING", reason, error=reason)
        finally:
            if session is not None:
                try:
                    await asyncio.to_thread(session.stop, clear=True)
                except Exception as exc:
                    reason = f"{reason} Stop cleanup issue: {exc}"
            detail = reason
            error = self.snapshot()["error"]
            self._set_state("OFF", detail, error=error)
            with self._lock:
                self._session = None

    def _validate_accepted(self, message: dict[str, Any]) -> dict[str, int]:
        if message.get("protocol") != PROTOCOL or message.get("captureId") != self.capture_id:
            raise TransportError("Receiver acceptance protocol or captureId did not match this connection.")
        required = ("frameBytes", "frameMs", "ackEveryFrames", "maxUnackedFrames", "heartbeatMs")
        if any(type(message.get(key)) is not int for key in required):
            raise TransportError("Receiver acceptance is missing numeric frame or flow-control settings.")
        if message["frameBytes"] != FRAME_BYTES or message["frameMs"] != FRAME_MS:
            raise TransportError("Receiver requested an unsupported audio frame size; expected 640 bytes / 20 ms.")
        if not 1 <= message["ackEveryFrames"] <= 1000 or not 1 <= message["maxUnackedFrames"] <= LOCAL_QUEUE_FRAMES:
            raise TransportError("Receiver flow-control values are outside the supported bounded range (1–50 frames).")
        if not 100 <= message["heartbeatMs"] <= 10_000:
            raise TransportError("Receiver heartbeat interval must be between 100 and 10,000 ms.")
        return {key: message[key] for key in required}

    @staticmethod
    def _decode_message(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, str):
            raise TransportError("Receiver sent a binary WebSocket message; protocol 1 requires JSON text.")
        if len(raw.encode("utf-8")) > MAX_CONTROL_BYTES:
            raise TransportError("Receiver sent a message larger than the 4 KiB protocol limit.")
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise TransportError(f"Receiver sent invalid JSON: {exc}") from exc
        if not isinstance(message, dict):
            raise TransportError("Receiver message must be a JSON object.")
        return message

    async def _stream(
        self,
        websocket: Any,
        session: CaptureSession,
        params: dict[str, int],
        accepted_at: float,
    ) -> tuple[str, bool]:
        source_order = ("candidate", "interviewer")
        sequence = {source: 0 for source in source_order}
        acked_through = {source: -1 for source in source_order}
        first_timestamp: dict[str, int] = {}
        unacked: dict[str, deque[int]] = {source: deque() for source in source_order}
        last_progress = accepted_at
        next_ping = accepted_at + min(params["heartbeatMs"], 1000) / 1000.0
        drain_deadline: float | None = None
        stop_reason = "Stopped by Windows operator; both audio streams are Off."
        remote_terminal = False

        while True:
            now = time.monotonic()
            if self.stop_requested.is_set() and drain_deadline is None:
                self._set_state("STOPPING", "Stopping both audio devices and draining queued frames…")
                await asyncio.to_thread(session.stop, clear=True)
                drain_deadline = time.monotonic() + LIVENESS_TIMEOUT_SECONDS

            if session.error_event.is_set():
                raise CaptureError(session.fatal_message or "Capture failed; both streams are stopping.")
            if drain_deadline is None:
                session.check_health()

            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=0.01)
                message = self._decode_message(raw)
                msg_type = message.get("type")
                if msg_type == "audio.ack":
                    source = message.get("source")
                    seq = message.get("sequence")
                    if message.get("captureId") != self.capture_id or not isinstance(source, str) or source not in acked_through or type(seq) is not int:
                        raise TransportError("Receiver sent an invalid audio.ack message.")
                    if seq < 0:
                        raise TransportError("Receiver acknowledged a negative audio sequence.")
                    if seq >= sequence[source]:
                        raise TransportError(f"Receiver acknowledged unsent {source} sequence {seq}.")
                    if seq > acked_through[source]:
                        acked_through[source] = seq
                        while unacked[source] and unacked[source][0] <= seq:
                            unacked[source].popleft()
                        with self._lock:
                            self._acked[source] = seq + 1
                    last_progress = time.monotonic()
                elif msg_type == "capture.pong":
                    if message.get("captureId") != self.capture_id:
                        raise TransportError("Receiver pong captureId did not match this connection.")
                    last_progress = time.monotonic()
                elif msg_type == "capture.stop":
                    if message.get("captureId") != self.capture_id:
                        raise TransportError("Receiver stop captureId did not match this connection.")
                    remote_terminal = True
                    stop_reason = f"Receiver stopped capture: {message.get('reason', 'unspecified reason')}"
                    break
                elif msg_type in ("error", "capture.error"):
                    raise TransportError(f"Receiver error: {message.get('message', message)}")
                # Unknown informational message types are intentionally ignored.
            except asyncio.TimeoutError:
                pass

            now = time.monotonic()
            if now - last_progress > LIVENESS_TIMEOUT_SECONDS:
                raise TransportError("No receiver acknowledgement or pong for more than five seconds; capture stopped.")
            if now >= next_ping:
                await self._send_json(websocket, {"type": "capture.ping", "captureId": self.capture_id})
                next_ping = now + min(params["heartbeatMs"], 1000) / 1000.0

            for source in source_order:
                if len(unacked[source]) >= params["maxUnackedFrames"]:
                    continue
                try:
                    frame = self._queues[source].get_nowait()
                except queue.Empty:
                    continue
                timestamp = first_timestamp.setdefault(source, min(5_000_000, max(0, int(frame.timestamp_us))))
                seq = sequence[source]
                packet = {
                    "type": "audio.frame",
                    "captureId": self.capture_id,
                    "source": source,
                    "sequence": seq,
                    "timestampUs": timestamp + seq * 20_000,
                    "sampleCount": 320,
                    "pcmBase64": base64.b64encode(frame.pcm).decode("ascii"),
                }
                if len(frame.pcm) != FRAME_BYTES:
                    raise TransportError(f"{source.title()} converter produced a frame that was not exactly 640 bytes.")
                await self._send_json(websocket, packet)
                sequence[source] += 1
                unacked[source].append(seq)
                with self._lock:
                    self._sent[source] = sequence[source]

            if drain_deadline is not None:
                queues_empty = all(outgoing.empty() for outgoing in self._queues.values())
                outstanding_empty = all(not pending for pending in unacked.values())
                if queues_empty and outstanding_empty:
                    break
                if time.monotonic() >= drain_deadline:
                    break

        return stop_reason, remote_terminal

