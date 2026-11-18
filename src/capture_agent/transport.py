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
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, ProxyHandler, build_opener
from uuid import uuid4

from .capture import CaptureError, CaptureSession
from .conversion import PreviewFrame


PROTOCOL = 1
DEFAULT_RECEIVER_URL = "ws://192.168.1.103:8765/ws/windows-agent"
FRAME_BYTES = 640
FRAME_MS = 20
MAX_CONTROL_BYTES = 4096
LOCAL_QUEUE_FRAMES = 50
LIVENESS_TIMEOUT_SECONDS = 10.0
POLL_SECONDS = 5.0


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
    if parts.path != "/ws/windows-agent" or parts.query:
        raise TransportError("Receiver URL must end in /ws/windows-agent without query parameters.")
    if len(value.encode("utf-8")) > 512:
        raise TransportError("Receiver URL is too long.")
    return value


class WebSocketCaptureSender:
    """After Start, poll readiness and own one live connection/capture at a time."""

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
        self._set_state("WAITING", "Checking Mac readiness; audio devices remain Off.")
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

    def _enqueue(self, source: str, enabled: threading.Event) -> Callable[[PreviewFrame], None]:
        target = self._queues[source]

        def add(frame: PreviewFrame) -> None:
            if not enabled.is_set() or self.stop_requested.is_set():
                return
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

    def _ready(self) -> bool:
        parts = urlsplit(self.url)
        url = urlunsplit(("http", parts.netloc, "/api/v1/windows-receiver/health", "", ""))
        request = Request(url, headers={"Cache-Control": "no-cache"})
        # LAN requests must not be routed through a configured corporate proxy.
        with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
            payload = response.read(MAX_CONTROL_BYTES + 1)
        if len(payload) > MAX_CONTROL_BYTES:
            raise TransportError("Receiver health response exceeds the size limit.")
        result = json.loads(payload)
        return isinstance(result, dict) and result.get("status") == "up" and result.get("protocol") == PROTOCOL and result.get("ready") is True

    async def _wait_retry(self) -> None:
        deadline = time.monotonic() + POLL_SECONDS
        while not self.stop_requested.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)

    def _clear_queues(self) -> None:
        for outgoing in self._queues.values():
            while True:
                try:
                    outgoing.get_nowait()
                except queue.Empty:
                    break

    async def _run(self) -> None:
        while not self.stop_requested.is_set():
            try:
                ready = await asyncio.to_thread(self._ready)
                if self.stop_requested.is_set():
                    break
                if ready:
                    await self._connect()
                else:
                    self._set_state("WAITING", "Mac is up; waiting for an interview with Use Windows audio checked. Checking every 5 seconds.")
            except CaptureError:
                # _stream's finally releases both devices and clears queued PCM.
                # Re-enter readiness polling with a fresh captureId on reconnect.
                self._set_state("WAITING", "Audio capture interrupted. Recovering automatically in 5 seconds; queued audio was cleared. Stop to change devices if needed.")
            except Exception:
                self._set_state("WAITING", "Mac unavailable or disconnected. Retrying in 5 seconds; audio devices are Off.")
            if not self.stop_requested.is_set():
                await self._wait_retry()
        self._set_state("OFF", "Stopped. Automatic reconnect disabled; both audio devices are Off.")

    async def _connect(self) -> None:
        from websockets.asyncio.client import connect

        self.capture_id = str(uuid4())
        self._clear_queues()
        with self._lock:
            self._sent = {source: 0 for source in self._queues}
            self._acked = {source: 0 for source in self._queues}
        self._set_state("CONNECTING", "Mac is ready; connecting…")
        async with connect(
            self.url, open_timeout=5, close_timeout=1, max_size=MAX_CONTROL_BYTES,
            max_queue=16, ping_interval=None, compression=None, proxy=None,
        ) as websocket:
            if self.stop_requested.is_set():
                return
            await self._send_json(websocket, {
                "type": "capture.start", "protocol": PROTOCOL, "captureId": self.capture_id,
                "sampleRate": 16000, "channels": 1, "format": "pcm_s16le",
                "sources": ["candidate", "interviewer"],
            })
            self._set_state("WAITING", "Connected; waiting for receiver acceptance. Audio devices remain Off.")
            deadline = time.monotonic() + 10
            next_ping = time.monotonic()
            transmit = False
            while not self.stop_requested.is_set():
                if time.monotonic() > deadline:
                    raise TransportError("Timed out waiting for receiver acceptance.")
                if time.monotonic() >= next_ping:
                    await self._send_json(websocket, {"type": "capture.ping", "captureId": self.capture_id})
                    next_ping = time.monotonic() + 1
                try:
                    message = self._decode_message(await asyncio.wait_for(websocket.recv(), timeout=0.1))
                except asyncio.TimeoutError:
                    continue
                kind = message.get("type")
                if kind == "capture.control":
                    transmit = self._transmit_flag(message)
                elif kind == "capture.accepted":
                    params = self._validate_accepted(message)
                    transmit = self._transmit_flag(message)
                    await self._stream(websocket, params, time.monotonic(), transmit)
                    break
                elif kind == "capture.stop":
                    self._set_state("WAITING", "Mac stopped capture; waiting for its next ready interview.")
                    return
                elif kind in ("error", "capture.error"):
                    raise TransportError("Mac rejected capture.")
            if self.stop_requested.is_set():
                try:
                    await self._send_json(websocket, {"type": "capture.stop", "captureId": self.capture_id}, timeout=1)
                except Exception:
                    pass

    @staticmethod
    def _transmit_flag(message: dict[str, Any]) -> bool:
        value = message.get("transmit")
        if type(value) is not bool:
            raise TransportError("Receiver transmission control must contain a boolean transmit value.")
        return value

    def _validate_accepted(self, message: dict[str, Any]) -> dict[str, int]:
        if message.get("protocol") != PROTOCOL or message.get("captureId") != self.capture_id:
            raise TransportError("Receiver acceptance protocol or captureId did not match this connection.")
        required = ("frameBytes", "frameMs", "ackEveryFrames", "maxUnackedFrames", "heartbeatMs")
        if any(type(message.get(key)) is not int for key in required):
            raise TransportError("Receiver acceptance is missing numeric frame or flow-control settings.")
        if message["frameBytes"] != FRAME_BYTES or message["frameMs"] != FRAME_MS:
            raise TransportError("Receiver requested an unsupported audio frame size; expected 640 bytes / 20 ms.")
        if not 1 <= message["ackEveryFrames"] <= message["maxUnackedFrames"] <= LOCAL_QUEUE_FRAMES:
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
        self, websocket: Any, params: dict[str, int], accepted_at: float, transmit: bool,
    ) -> None:
        source_order = ("interviewer", "candidate")
        sequence = {source: 0 for source in source_order}
        acked_through = {source: -1 for source in source_order}
        first_timestamp: dict[str, int] = {}
        unacked: dict[str, deque[int]] = {source: deque() for source in source_order}
        last_progress = accepted_at
        next_ping = accepted_at
        session: CaptureSession | None = None
        enabled = threading.Event()

        async def release() -> None:
            nonlocal session
            enabled.clear()  # Old capture callbacks can never enqueue into a resumed session.
            if session is not None:
                old, session = session, None
                await asyncio.to_thread(old.stop, clear=True)
            with self._lock:
                self._session = None
            self._clear_queues()

        try:
            while not self.stop_requested.is_set():
                # Receive control before sending another audio batch.
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=0.01)
                    message = self._decode_message(raw)
                    kind = message.get("type")
                    if kind == "capture.control":
                        transmit = self._transmit_flag(message)
                    elif kind == "audio.ack":
                        source, seq = message.get("source"), message.get("sequence")
                        if message.get("captureId") != self.capture_id or not isinstance(source, str) or source not in sequence or type(seq) is not int or not 0 <= seq < sequence[source]:
                            raise TransportError("Receiver acknowledged an invalid or unsent audio sequence.")
                        if seq > acked_through[source]:
                            acked_through[source] = seq
                            while unacked[source] and unacked[source][0] <= seq:
                                unacked[source].popleft()
                            with self._lock:
                                self._acked[source] = seq + 1
                        last_progress = time.monotonic()
                    elif kind == "capture.pong":
                        if message.get("captureId") != self.capture_id:
                            raise TransportError("Receiver pong captureId did not match.")
                        last_progress = time.monotonic()
                    elif kind == "capture.stop":
                        if message.get("captureId") != self.capture_id:
                            raise TransportError("Receiver stop captureId did not match.")
                        self._set_state("WAITING", "Mac stopped capture; waiting for its next ready interview.")
                        return
                    elif kind in ("error", "capture.error"):
                        raise TransportError("Receiver reported a capture error.")
                except asyncio.TimeoutError:
                    pass

                now = time.monotonic()
                if now - last_progress > LIVENESS_TIMEOUT_SECONDS:
                    raise TransportError("No acknowledgement or pong for 10 seconds.")
                if now >= next_ping:
                    await self._send_json(websocket, {"type": "capture.ping", "captureId": self.capture_id})
                    next_ping = now + min(params["heartbeatMs"], 1000) / 1000

                if not transmit:
                    if session is not None:
                        await release()
                    self._set_state("PAUSED", "Mac unchecked Windows audio. Devices are Off; heartbeat stays connected.")
                    continue
                if session is None:
                    self._clear_queues()
                    enabled = threading.Event()
                    enabled.set()
                    session = CaptureSession(self.config_path, frame_sinks={source: self._enqueue(source, enabled) for source in source_order})
                    with self._lock:
                        self._session = session
                    await asyncio.to_thread(session.start)
                    self._set_state("ON", "Streaming Candidate and Interviewer to the Mac.")
                    # Recheck queued controls after device startup before sending PCM.
                    continue
                session.check_health()
                for source in source_order:
                    if len(unacked[source]) >= params["maxUnackedFrames"]:
                        continue
                    try:
                        frame = self._queues[source].get_nowait()
                    except queue.Empty:
                        continue
                    if len(frame.pcm) != FRAME_BYTES:
                        raise TransportError("Converter frame must contain exactly 640 bytes.")
                    timestamp = first_timestamp.setdefault(source, min(5_000_000, max(0, int(frame.timestamp_us))))
                    seq = sequence[source]
                    await self._send_json(websocket, {
                        "type": "audio.frame", "captureId": self.capture_id, "source": source,
                        "sequence": seq, "timestampUs": timestamp + seq * 20_000,
                        "sampleCount": 320, "pcmBase64": base64.b64encode(frame.pcm).decode("ascii"),
                    })
                    sequence[source] += 1
                    unacked[source].append(seq)
                    with self._lock:
                        self._sent[source] = sequence[source]
        finally:
            await release()
