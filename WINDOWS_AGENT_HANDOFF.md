# Windows capture agent: Mac receiver contract and Codex handoff

Protocol version: **1**, minimal LAN POC. This replaces the earlier TLS/token draft.
The Mac receiver is implemented against this contract. Keep source capture unchanged except for normalization and transport.

## Prompt to give Codex in the Windows repository

> Reuse the Windows app's working microphone and loopback capture. Add a minimal
> LAN WebSocket sender to the Mac receiver at `ws://<MAC_LAN_IP>:8765/ws/windows-agent`.
> This is an in-house POC: no TLS, CA certificate, pairing token, login or API key.
> The only connection configuration is the receiver URL. The Mac operator starts
> an interview and selects Use Windows audio before you connect. There is one
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
> capture.stop, disconnect or capture error. Explicit reconnect is enough; do not
> replay old queued audio. Keep queues small and use the acknowledged sequence
> values for the specified bound. No recordings or large automated test suite.
>
> Test manually with ChatGPT Voice on Windows and headphones: its spoken replies
> must arrive as Interviewer; my microphone must arrive as Candidate. The Mac
> handles transcription and AI. Do not add local recognition or voice calibration
> in this task. Verify Stop/Finish and brief simultaneous speech. Follow the exact
> message shapes below and report actual Windows results.

## Configuration

```json
{
  "url": "ws://192.168.1.50:8765/ws/windows-agent",
  "protocol": 1
}
```

Use the actual URL displayed on the Mac, not the example IP. The Copy connection
JSON button supplies these fields. No authentication header, certificate file or
session ID is required. This plain WebSocket receiver is for the trusted in-house
LAN POC. It is not an Internet-facing service.

The Mac must have Use Windows audio checked and its control connection open.
Unchecking it keeps the HP connection open: the Mac acknowledges and discards audio.
Checking it again resumes processing automatically on the same connection, without
a new capture.start or replay. Keep sending frames and heartbeats while unchecked.
Finish, End or losing the active Windows control connection stops the sender.
On a stopped/disconnected session, click Reconnect receiver on the Mac before
reconnecting the Windows sender. The URL stays the same.

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
