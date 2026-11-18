# Windows capture agent: Mac receiver contract and Codex handoff

Protocol version: **1**, minimal LAN POC. This replaces the earlier TLS/token draft.
The Mac receiver is implemented against this contract. Keep source capture unchanged except for normalization and transport.

## Prompt to give Codex in the Windows repository

> Reuse the Windows app's working microphone and loopback capture. Add a minimal
> LAN WebSocket sender to the Mac receiver at `ws://<MAC_LAN_IP>:8765/ws/windows-agent`.
> This is an in-house POC: no TLS, CA certificate, pairing token, login or API key.
> The only connection configuration is the receiver URL. Start the Windows agent
> anytime; it waits until the Mac interview selects Use Windows audio. There is one
> active interview and one Windows sender, so no session ID lookup is needed.
>
> Use the protocol below. Microphone is `candidate`; playback loopback is
> `interviewer`. Keep them separate, normalize to 16 kHz mono signed 16-bit
> little-endian PCM, and send 20 ms / 640-byte frames as base64 in JSON messages.
> Keep a sequence counter per source and a new UUID captureId per connection.
> Send capture.start, wait for capture.accepted, then send both channels. Fill
> silence with zero frames. Preserve resampler state between device callbacks.
> Prioritize interviewer audio: candidate suppression during interviewer speech is allowed.
> Replace suppressed candidate audio with zero frames; keep its sequence and timestamps advancing. Never suppress interviewer audio.
>
> Add receiver URL, Connect/Start, Stop, connection status and simple per-source
> sent/acked counts. Send capture.ping every second. Stop both device streams on
> capture.stop, disconnect or capture error, then automatically poll readiness every
> five seconds and reconnect as described below. Do not
> replay old queued audio. Keep queues small and use the acknowledged sequence
> values for the specified bound. No recordings or large automated test suite.
>
> Test manually with ChatGPT Voice on Windows and headphones: its spoken replies
> must arrive as Speaker; my microphone must arrive as Mic. The Mac
> handles transcription and AI. Do not add local recognition or voice calibration
> in this task. Verify Stop/Finish and brief simultaneous speech. Follow the exact
> message shapes below and report actual Windows results.

## Automatic readiness and reconnect (current receiver)

Give the Windows Codex this update:

> Poll `http://<MAC_LAN_IP>:8765/api/v1/windows-receiver/health` every 5 seconds
> while disconnected. Derive HTTP host/port from the configured WebSocket URL.
> HTTP 200 returns `{ "status": "up", "protocol": 1, "ready": false, "retryAfterMs": 5000 }`.
> When unavailable or ready=false, stay waiting and send no audio. When ready=true,
> connect the existing WebSocket and perform capture.start/accepted automatically.
> Do not require a Connect click after the initial configuration/start of the agent.
> The Mac sends `{ "type": "capture.control", "transmit": true|false }` when the
> checkbox/control connection changes. Wait for capture.accepted before any audio.
> accepted now also includes transmit. Honor false by dropping queued PCM and
> stopping audio transmission, keeping the socket and capture.ping alive every second.
> On true resume live audio with the same captureId and per-source counters. During
> this pause freeze the normalized transport timestamps: each next frame remains
> firstTimestampUs + sequence*20000. Do not send zero frames for the unchecked time.
> Mic suppression DURING transmission still sends zero candidate frames.
> Heartbeats before capture.start may omit captureId; after accepted include it.
> Process control messages throughout the handshake, applying the newest value.
> On close (including normal 1000), transport failure, missing pong for 10 seconds,
> or capture.stop: release capture, discard queues and return to the 5-second health
> poll loop. Use a fresh captureId and counters on the next connection. Prevent
> parallel sockets/retry loops. A user Stop/Exit disables automatic reconnect.
> No old audio replay and no API keys/certificates/tokens. Test start-before-Mac,
> checkbox off/on, Finish, Mac restart and Wi-Fi disconnect/reconnect manually.

Readiness means an interview is active, Windows audio is selected and its browser
monitor is connected. Existing connections can stay alive while unchecked. Closed
connections cannot exchange heartbeat; HTTP polling discovers recovery instead.

## Configuration

```json
{
  "url": "ws://192.168.1.50:8765/ws/windows-agent",
  "protocol": 1
}
```

Use the actual URL displayed on the Mac, not the example IP. The copy icon beside Windows receiver copies the URL; protocol stays fixed at 1. No authentication header, certificate file or
session ID is required. This plain WebSocket receiver is for the trusted in-house
LAN POC. It is not an Internet-facing service.

The Mac must have Use Windows audio checked and its control connection open.
Unchecking it keeps the HP connection open: the Mac acknowledges and discards audio.
Checking it again sends capture.control transmit=true and resumes on the same
connection without a new capture.start or replay. Keep heartbeats while unchecked,
but stop transmitting audio when capture.control transmit=false.
Finish, End or losing the active Windows control connection stops the sender.
After a disconnect, poll readiness and reconnect automatically. The URL stays the same.

## Start: client → server

```json
{
  "type": "capture.start",
  "protocol": 1,
  "captureId": "<NEW_UUID>",
  "sampleRate": 16000,
  "channels": 1,
  "format": "pcm_s16le",
  "sources": ["candidate", "interviewer"]
}
```

## Accepted: server → client

```json
{
  "type": "capture.accepted",
  "protocol": 1,
  "captureId": "<SAME_UUID>",
  "frameBytes": 640,
  "frameMs": 20,
  "ackEveryFrames": 10,
  "maxUnackedFrames": 50,
  "heartbeatMs": 1000
}
```

## Frame: client → server

```json
{
  "type": "audio.frame",
  "captureId": "<SAME_UUID>",
  "source": "candidate",
  "sequence": 0,
  "timestampUs": 0,
  "sampleCount": 320,
  "pcmBase64": "<BASE64_OF_EXACTLY_640_BYTES>"
}
```

- Text JSON WebSocket messages; binary frames are not accepted in protocol 1.
- Maximum frame/control message size is 4 KiB.
- Sequence increments independently per source with no missing frames. Duplicates
  are acknowledged without processing again; forward gaps are rejected.
- `timestampUs` is relative to this capture's shared normalized clock, not Unix
  time. The first timestamp for each source may be 0–5,000,000 microseconds to
  represent startup skew. Subsequent timestamps must equal that source's first
  timestamp plus `sequence * 20000`. Include zero frames for idle periods.
- An optional sessionId is accepted for compatibility but is unnecessary for the single-session POC.
- The Mac adds its existing interview offset internally. Never add a Mac/session
  offset to the Windows timestamps yourself.
- Persist resampler state and partial PCM remainders across device callbacks.
- Do not buffer the entire interview, mix sources or encode WAV headers.

## Acknowledgements, heartbeat and Stop

```json
{"type":"audio.ack","captureId":"<SAME_UUID>","source":"candidate","sequence":9}
```

Acknowledges accepted frames through sequence 9 for this source. Acceptance means
validated for transport; while unchecked it is discarded. It does not mean STT or AI has finished. Duplicate
frames may produce an extra acknowledgement.

```json
{"type":"capture.ping","captureId":"<SAME_UUID>"}
```

The server replies with `capture.pong` and the same captureId. Stop on more than
five seconds without a pong/ack after connection acceptance. The receiver also
expires idle agents. WebSocket library pings alone do not replace capture.ping.

```json
{"type":"capture.stop","captureId":"<SAME_UUID>"}
```

The client can send this before closing. The server can send capture.stop with a
fixed `reason` such as FINISHED, MONITOR_DISCONNECTED, IDLE,
BACKPRESSURE, INVALID_FRAME or AGENT_STOPPED, then close. Treat all server stop
reasons as terminal for this connection and release both device handles.

The Mac shows separate meters and source labels. It transcribes on the Mac and
sends role-tagged text to its existing AI conversation. The Windows agent should
ignore unknown informational message types safely; an explicit error/stop/close
must not be ignored.
