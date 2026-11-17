# Windows Interview Capture Agent — Implementation and Manual Test Plan

Status: W0–W2 implemented and manually confirmed for separate WAV capture; W4 WebSocket sender implemented in the GUI. End-to-end Mac receiver validation remains pending.
Prepared: 17 November 2026. The active version-1 Mac handoff is `WINDOWS_AGENT_HANDOFF.md`; where W4 details here differ, follow that handoff.
Suggested new repository: `interview-capture-windows`.

Copy this document into the new repository as `IMPLEMENTATION_PLAN.md`. It is
self-contained so implementation can proceed on the Windows HP laptop without
access to the Mac repository. The device, selection, monitor and preview
device, selection, monitor and preview commands are implemented. The W4 sender
is in the GUI; there is no CLI `send` command or mock-receiver command.

## 1. Objective, capture control and fixed role mapping

Build a small personal-use Windows application that captures two independent
audio streams and eventually sends them to the existing interview assistant on
the Mac:

### Capture must be explicitly switched on

- The agent is **OFF by default** whenever it starts. Importing, launching,
  listing devices, monitoring configuration or connecting to a receiver must not
  open a capture stream or read microphone/playback audio.
- Capture starts only after the user performs a clearly labeled Start/On action.
  A visible indicator must show that both sources are actively being captured.
- Stop/Off must stop both sources promptly, release their audio handles, clear
  queued and preview audio from memory, and prevent any further audio delivery.
  Capture must also stop both sources on an error or when the agent exits.
  `preview` is the deliberate exception: after its bounded capture ends, it may
  retain that preview in RAM only until separate playback finishes, then it must
  clear it. No new audio is read during that playback.
- While Off, normal laptop audio use remains under Windows and the user's other
  applications: calls and other discussions continue normally, and the agent
  does not capture, buffer new audio or send audio. The only retained audio may
  be the bounded preview awaiting playback described above. The agent must not
  mute, duck, reroute or otherwise interfere with those applications.
- The agent is a user-mode application. It must not install, replace, modify or
  disable audio drivers; create virtual audio devices; or change Windows audio
  device, privacy, routing, default-device or system-wide settings. It may
  enumerate devices and open only the explicitly selected microphone and
  corresponding playback-loopback stream while On. It must not require
  administrator privileges for normal operation.
- If a selected device fails or disappears, stop both streams, return to Off,
  release resources and show an actionable error. Do not auto-restart capture or
  silently switch devices.

| Windows audio source | Assigned role | Initial mock-interview source |
|---|---|---|
| Selected microphone input | CANDIDATE | You speaking into the built-in laptop microphone |
| Loopback of selected playback device | INTERVIEWER | ChatGPT Voice speaking through the built-in speakers |

Roles come from the capture source, not voice characteristics. No pitch filter,
voice calibration, voiceprint matching, gender inference or speaker diarisation
is required to distinguish these two roles. Multiple remote interviewers can
share the INTERVIEWER role. This does not identify individual interviewers.

Assumptions:

- Only the candidate speaks into the local microphone during an interview.
- ChatGPT Voice and, later, Teams use the selected playback device.
- The user's initial setup is the laptop's built-in microphone and speaker. The
  playback loopback captures the speaker mix directly; the built-in microphone
  may also acoustically pick up that speaker audio.
- Speaker-priority mode is enabled by default: when loopback playback is active,
  Candidate preview frames are replaced with silence for the overlap plus a
  short hangover. The Interviewer loopback stream is never gated. The user
  accepts losing Candidate speech during those intervals to protect the
  Interviewer channel from speaker bleed.
- The Windows agent is capture/transport software. The Mac owns transcription,
  CV/JD context, AI cues and the interview session.
- The first useful test must work entirely on Windows, without a Mac or API key
  in the capture agent.

## 2. Scope and practical limits

### Initial scope

- Off by default, with explicit Start/On and Stop/Off controls and a persistent
  visible capture-state indicator.
- Explicit microphone and playback-loopback selection.
- Independent live level meters, counters and role labels.
- Start and Stop; release both devices cleanly on Stop or failure.
- Short, bounded in-memory previews for checking each channel separately.
- A manually operated ChatGPT Voice mock interview on Windows.
- A versioned transport contract and a Windows sender to the trusted-LAN Mac
  receiver (the active v1 contract is in `WINDOWS_AGENT_HANDOFF.md`).
- A local Windows mock receiver remains a possible convenience for development.

### Deferred

- A background Windows service, automatic startup, installer and tray integration.
- Capturing only a particular application's audio.
- Loudspeaker acoustic echo cancellation, automatic ducking or stream suppression.
- Voice profiles, separating individual remote participants and overlap diarisation.
- Automatic control of ChatGPT Voice or Teams.
- Transcription, answer generation or OpenAI credentials inside the Windows agent.
- Cloud audio upload and exporting ChatGPT voice recordings. The GUI transport
  streams live to the explicitly configured Mac and does not record locally.
  CLI `preview --save` remains available for a bounded manual source check.

Driver installation/replacement and system-wide audio reconfiguration are out of
scope. The agent must use Windows' existing audio stack without changing it.

Standard WASAPI endpoint loopback captures the selected device's playback mix,
not just Teams or ChatGPT. Other applications and notifications on that endpoint
can appear as INTERVIEWER audio. Close/mute unrelated applications for the POC.
Microsoft documents both [endpoint loopback](https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording)
and a separate [process-loopback capture mechanism](https://learn.microsoft.com/en-us/samples/microsoft/windows-classic-samples/applicationloopbackaudio-sample/).
Process-specific capture is a possible later enhancement, not a first milestone.

Do not assume the microphone feed inherits Teams' echo cancellation or mute state.
Muting yourself in a call application may leave the agent's independent microphone
capture running. Its own capture indicator must always show what the agent is doing.

## 3. Technology choice

Use **Python 3.12, 64-bit, on Windows 11** for the first implementation. Confirm
package-wheel compatibility on the actual HP laptop during W0 and pin versions
that install successfully; do not invent a dependency lockfile on the Mac.

| Component | Proposed choice | Reason |
|---|---|---|
| Microphone and loopback capture | PyAudioWPatch / WASAPI | One Windows audio library for both inputs |
| PCM conversion and levels | NumPy | Small, explicit signal-processing operations |
| Streaming sample-rate conversion | Python-SoXR | Stateful conversion across callback boundaries |
| Transport and mock receiver | websockets | Small Python WebSocket client/server |
| Initial interface | Console CLI | Easy to run and diagnose on Windows |
| Later interface, if useful | Small Tkinter window | Device selectors, meters and Start/Stop only |

PyAudioWPatch provides loopback-device enumeration and Windows installation
instructions in its [maintainer documentation](https://github.com/s0d3s/PyAudioWPatch).
Use a loopback input corresponding to the chosen playback device, not an ordinary
microphone device that happens to have a similar name.

Use a separate stateful resampler per stream. Do not resample every small callback
as an independent recording, or assume that microphone and playback have identical
native formats. See [Python-SoXR streaming documentation](https://python-soxr.readthedocs.io/en/stable/soxr.html).

C# with NAudio is a reasonable fallback if the Windows audio spike fails. Do not
build Python and C# implementations in parallel. Switch only after documenting
an actual compatibility problem. [NAudio capture documentation](https://naudio.github.io/NAudio/docs/WasapiRecorder.html)

## 4. Repository structure

```text
interview-capture-windows/
  IMPLEMENTATION_PLAN.md
  README.md
  pyproject.toml
  requirements-lock.txt          # generated/pinned on Windows
  .gitignore
  config.example.json            # placeholders, no credentials
  src/capture_agent/
    __main__.py
    cli.py
    devices.py
    capture.py
    conversion.py
    preview.py
    protocol.py
    transport.py
    mock_receiver.py
  docs/
    WINDOWS_SETUP.md
    CHATGPT_MOCK_TEST.md
    TRANSPORT_CONTRACT.md
    MANUAL_TEST_RESULTS.md
    MAC_INTEGRATION.md
  private/                       # ignored: local configuration/certificates/token
```

Keep the capture package small. Do not add a database, Docker, distributed message
broker or local AI models. The mock receiver must be importable without opening
audio devices. If a later receiver component runs on the Mac, isolate Windows-only
imports so receiver tooling can run without PyAudioWPatch there.

## 5. Windows setup

1. Install Git and 64-bit Python 3.12. Confirm the interpreter with `py -0p`.
2. Create/clone the new repository on Windows and copy this plan into it.
3. Create a virtual environment using PowerShell:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install --upgrade pip
   ```

4. W0 will define the package and install its capture dependencies. A dependency
   spike can use the following command, then record/pin the working versions:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install PyAudioWPatch numpy soxr websockets
   ```

5. Enable microphone access for desktop applications in Windows privacy settings.
6. Select the laptop's built-in microphone and speaker. Use the same playback
   endpoint in the browser's voice conversation.
7. Keep the Mac, API billing and Teams out of the first capture check.

Use the virtual environment's Python executable directly; activating PowerShell
scripts or changing machine-wide execution policy is unnecessary.

## 6. Audio capture design

### Two independent pipelines

Each selected device has its own callback, format conversion, resampler, sequence
counter, bounded queue and metrics. Never mix the streams before transcription.

Capture callbacks must only copy a bounded packet into a queue and return. Network
operations, preview playback and resampling must not block the device callback.

Normalize each stream to:

| Property | Value |
|---|---|
| Sample rate | 16,000 Hz |
| Channels | Mono |
| Sample format | Signed 16-bit little-endian PCM |
| Transport frame | 20 ms = 320 samples = 640 bytes |

Open hardware at a supported native format, then convert to the normalized format.
Downmix stereo playback deliberately, clamp converted values, preserve incomplete
frame remainders, and retain resampler state between callbacks.

### Timing and silence

- Use a shared Windows monotonic clock for capture-relative timestamps.
- Preserve each stream's actual initial offset; do not assume simultaneous starts.
- Record device timestamps where available and verify their behavior on the laptop.
- Preserve timeline gaps. Do not concatenate audio from before and after silence
  as if no time elapsed.
- Loopback can stop delivering packets when playback is idle. Silence is not by
  itself a device failure. Represent idle playback explicitly with correctly timed
  zero audio or a defined silence event; the first protocol uses zero frames.
- Measure clock drift during longer tests. Approximate timestamps are acceptable,
  but cumulative skew must be visible rather than silently ignored.
- Speaker-priority suppression is a local Candidate-channel gate, not device
  muting or acoustic echo cancellation. The microphone stream remains open while
  capture is On so it can resume when playback is quiet; microphone frames are
  zeroed when loopback is active and for a 350 ms hangover. This intentionally
  loses Candidate speech during those intervals. The direct Interviewer stream
  is processed independently and is never suppressed by this gate. If alignment
  cannot be established within 1.2 seconds, suppress that Candidate frame as a
  safety fallback; never drop the corresponding Interviewer frame for gating.

### Bounded operation

- Maximum normalized queue: 50 frames / one second per source.
- Maximum preview ring: ten seconds per source, in memory only.
- A queue overflow, unplugged device or unsupported format stops both captures
  and shows a fixed, actionable error. Do not silently keep one role running.
- Do not automatically switch devices when the Windows default changes. Stop and
  ask the user to reselect; automatic switching can invalidate role routing.
- Show role, selected device, RMS/peak, captured seconds, queued frames and dropped
  frames. Counters do not establish transcription correctness.

### Echo policy

The initial POC uses the built-in microphone and speaker. Speaker-priority mode
zeros Candidate frames whenever the aligned loopback frame has energy above its
threshold, with a 350 ms hangover. This suppresses candidate interruptions while
the speaker is active; that is the user's chosen tradeoff. It does not mute or
duck the Windows microphone or change another app's audio. The interviewer
loopback keeps running independently. The current W2 build has no acoustic echo
cancellation and may still retain some echo at frame boundaries or below the
gate threshold. If the bleed makes Candidate audio unusable while the speaker is
otherwise quiet, add reference-based AEC as a separate milestone; simple VAD or
energy comparison is not a substitute. Echo cancellation requires the render
reference and microphone signals with appropriate timing. [WebRTC processing interface](https://webrtc.googlesource.com/src/+/e583174d1ed9ec00ab0e1b3ebb5989ef6aae4223/modules/audio_processing/include/audio_processing.h)

## 7. Planned commands and operator flow

These Windows commands are available in the current implementation; `select`
saves an explicit mapping while keeping capture disabled:

```powershell
.\.venv\Scripts\python.exe -m capture_agent devices
.\.venv\Scripts\python.exe -m capture_agent select --microphone-index <index> --playback-index <index>
.\.venv\Scripts\python.exe -m capture_agent monitor --config private\agent.json
.\.venv\Scripts\python.exe -m capture_agent preview --config private\agent.json --seconds 10
```

The following mock receiver command remains a future convenience. W4 transport
is started from the GUI using the URL displayed by the Mac:

```powershell
.\.venv\Scripts\python.exe -m capture_agent mock-receiver --host 127.0.0.1 --port 8765
.\.venv\Scripts\python.exe -m capture_agent send --config private\agent.json
```

`devices` lists input and playback-loopback devices, their native formats and current
indices/identifiers. Revalidate selections after reboot; a numeric device index
alone is not a durable identity.

`monitor` starts in Off state and requires typing `START` before it opens either
capture stream. It shows both meters and counters; press `S` or Ctrl+C to stop
both. It does not network or record files. `preview` requires typing `PREVIEW`
before it opens either capture stream. It captures both channels into bounded
RAM, stops capture, then lets the user listen to Candidate and Interviewer
separately. Never replay a preview while capture is active, or the playback
could be captured again as interviewer audio.
Stop the ChatGPT Voice conversation before replaying previews.

Connect / Start sends the protocol-v1 `capture.start`, waits for `capture.accepted`
before opening either device, then streams the two sources as separate JSON text
frames. The authoritative W4 handshake, frame, acknowledgement and heartbeat
contract is in `WINDOWS_AGENT_HANDOFF.md`. Stop closes both handles, sends
`capture.stop` when possible, clears in-memory buffers, and does not replay old
frames on a later explicit reconnect.

## 8. ChatGPT Voice mock interview — Windows only

Use a ChatGPT voice conversation in the **Windows browser**, with the built-in
speaker and microphone selected.
ChatGPT is the interviewer; you are the candidate. Voice availability and limits
are controlled by the user's ChatGPT account. The agent neither automates that
conversation nor uses an OpenAI API key. [Official ChatGPT Voice guide](https://help.openai.com/en/articles/20001274-chatgpt-voice)

The existing Mac answer assistant's API usage is separate and only becomes relevant
in W5. Do not introduce an API-powered voice interviewer for this first test.

### Interviewer instructions to paste into ChatGPT

> Act as my mock technical interviewer for a Java and distributed-systems role.
> Ask one question at a time and let me finish my answer before continuing.
> Start each main question with “Interviewer question number one,” then increment
> the number. Keep questions short. Ask occasional brief follow-ups about my answer.
> Do not speak my candidate marker phrase or repeat my entire answer aloud.
> After five questions, finish the interview and give brief verbal feedback.
> I may interrupt you once to check that both audio channels remain available.

This is a requested interview format, not a guarantee that ChatGPT will follow
every instruction. The test measures actual captured audio, not assumed behavior.

### Test A — microphone alone

1. Leave browser playback silent and run `monitor`.
2. Say: “Candidate answer alpha. I am speaking into the Windows microphone.”
3. Candidate meter/counters should respond. Interviewer should remain near silence.
4. Use a short preview, stop capture, and listen to each channel separately.
5. If your own voice appears clearly in loopback, check Windows “Listen to this
   device,” software microphone monitoring and virtual audio routing.

### Test B — ChatGPT alone

1. Start the browser voice conversation and the agent's monitoring/preview.
2. Ask ChatGPT to speak, then remain silent while it asks a question.
3. Its words should be clearly present in the Interviewer preview.
4. Speaker-priority mode should zero Candidate frames while Interviewer is
   active; the monitor reports the number of speaker-suppressed Candidate
   frames. Confirm interviewer playback continues and is clear in its preview.
   Some frame-boundary or quiet-speaker bleed may remain because this setup has
   no AEC.
5. If Interviewer is silent, verify the browser's playback endpoint and the matching
   loopback device. Capturing a different output device will not work.

### Test C — alternating interview

1. Run five question/answer turns using the prompt above.
2. Begin your answers with “Candidate answer alpha,” “Candidate answer beta,” etc.
3. Observe both meters and collect short separate previews as needed.
4. Verify that ChatGPT questions reach Interviewer and that candidate speech is
   retained when playback is quiet. Candidate speech during speaker playback may
   be suppressed. No voice profile should be involved.
5. For long runs, monitor only. Use bounded short previews for listening checks;
   do not accumulate a full interview recording in RAM.

### Test D — overlap and silence

1. Speak briefly while ChatGPT is speaking. Candidate speech may be intentionally
   suppressed; interviewer playback must continue independently and clearly.
2. Leave both sides silent for at least 20 seconds, then resume.
3. Check that loopback resumes and elapsed time includes the silence.
4. Repeat with a 15–30 minute mock interview to check drift and bounded queues.

### Test E — limitations and recovery

- Play an unrelated notification: endpoint loopback may capture it. This demonstrates
  why other playback should be quiet during a real interview.
- Disconnect the selected microphone or speaker endpoint: both captures should stop with an actionable error.
- Change the Windows default device: do not silently relabel or switch sources.
- Stop the agent while ChatGPT keeps speaking: no more audio should be captured.
- Without `--save`, check that no WAV files or audio-content logs were created.

**Pass condition:** the Interviewer preview contains the direct speaker loopback
without gaps or suppression while playback is active; Candidate is captured when
playback is quiet and zeroed during speaker activity plus hangover. Confirm the
overlap behavior and listen for residual echo in Candidate. This W2 build has no
AEC. Also observe stable alternating/speaker-overlap capture without a Mac or
calibration. Meters alone are insufficient evidence. Fix routing or stability
before adding transcription, networking or AI.

## 9. Proposed transport contract — version 1

Use one WebSocket connection carrying two role-tagged streams. Start with JSON
messages and base64 PCM for simplicity; binary framing can be a later measured
optimization. At 16 kHz mono PCM, both streams total 64,000 raw bytes/second;
base64 with per-frame padding increases the audio payload to 85,600 bytes/second before JSON
and protocol overhead. This is a design calculation, not a measured throughput.

### Connection and lifecycle

- Local mock: `ws://127.0.0.1:8765/ws/capture-agent`.
- Mac integration: `ws://<mac-host>:8765/ws/windows-agent` on the trusted
  in-house LAN. Protocol-v1 handoff intentionally has no TLS, token, login,
  certificate or API key; it is not an Internet-facing endpoint.
- The only transport configuration is the receiver URL, stored locally under
  ignored `private/receiver.json` with protocol fixed at 1. The Mac owns its
  single active interview; no session ID is required.

The active handshake field names and control-message requirements are specified
in `WINDOWS_AGENT_HANDOFF.md`; that contract replaces the earlier token/TLS draft.

The agent sends the protocol-v1 `capture.start` defined in
`WINDOWS_AGENT_HANDOFF.md`, with a new capture ID, normalized format and exactly
two sources. The receiver replies `capture.accepted` before audio starts.

Each audio frame contains:

| Field | Meaning |
|---|---|
| `type` | `audio.frame` |
| `captureId` | UUID for this capture attempt |
| `source` | `candidate` or `interviewer`; fixed device mapping |
| `sequence` | Increasing integer, independently per source, starting at zero |
| `timestampUs` | First-sample time relative to the shared Windows capture clock |
| `sampleCount` | 320 |
| `pcmBase64` | Base64 of exactly 640 PCM bytes |

Do not trust arbitrary role strings: the receiver maps the two allowed source
identifiers to CANDIDATE and INTERVIEWER. Hardware/device names need not travel
with every frame. They remain local diagnostics unless explicitly needed.

### Acknowledgements and failure

- Receiver acknowledges each source's highest accepted contiguous sequence every
  ten frames or 200 ms. Acceptance means queued for processing, not transcribed.
- Keep at most 50 unacknowledged frames per source and bound the receiver's queue.
- Use fair scheduling so one source cannot starve the other.
- Reject malformed PCM, unsupported formats, invalid sources, stale capture IDs
  and non-monotonic sequence/timestamps; handle duplicates without duplicate text.
- Network failure stops capture, clears queued audio and reports a gap. No disk
  spool and no replay of stale interview audio.
- Retry is explicit. A new capture ID may attach to the same still-active interview,
  with a visible gap and an agreed timeline offset. Finished sessions cannot resume.
- A Mac `capture.stop` or terminal session state stops both Windows devices.
- Stop sends a final control message when possible and immediately releases devices.
  The receiver also expires disconnected captures; graceful delivery is not guaranteed.

The Windows mock receiver only validates/routs frames and reports metadata counters.
It must not pretend to implement Whisper, speaker recognition or ChatGPT answers.

## 10. Mac integration boundary — separate work in the existing repo

The Mac application is in a separate repository. The supplied
`WINDOWS_AGENT_HANDOFF.md` says its v1 WebSocket receiver is already implemented;
that receiver cannot be inspected from this Windows repository. W4 therefore
implements only the Windows sender. Any later Mac work can connect the receiver's
two role-tagged streams to separate STT timelines and the existing interview flow.
Do not add a second Mac-side authentication or TLS layer to this W4 sender: the
active v1 contract is the trusted-LAN, no-auth contract in the handoff.

## 11. Implementation milestones

| Milestone | Deliverable | Manual acceptance |
|---|---|---|
| W0 — Windows bootstrap | Python package, pinned working dependencies, README, ignored private config; clean platform checks | Package starts on the HP; no audio begins at import/startup |
| W1 — Device discovery | Enumerate WASAPI mic/playback-loopback, explicit mapping and format validation | Correct microphone and playback devices can be selected; stale/invalid selections fail clearly |
| W2 — Dual capture and preview | Two independent streams, conversion, meters, bounded RAM preview and Stop | Microphone-alone and playback-alone tests pass; previews play only after capture stops |
| W3 — ChatGPT mock interview | Five-turn Windows-only workflow and manual results document | Alternating, overlap, silence and Stop checks pass without Mac/calibration |
| W4 — Mac LAN transport sender | Handoff-v1 framing, acknowledgements, heartbeats, bounded queues and terminal failure handling | Pending manual Mac receiver check: both sources arrive with correct roles; disconnect/reconnect does not replay old audio |
| W5 — Mac integration | Dedicated ingress plus existing-app source adapter and two-source STT | Windows ChatGPT mock produces correctly role-tagged Mac transcripts and short topic cues |
| W6 — Teams and longer runs | Real call-device checks, 30–60 minute session, recovery and timeline checks | Correct roles, bounded memory and clean Stop/End; observed latency/drift documented |
| W7 — Optional convenience | Packaging and installer only if the GUI POC is useful | Same capture behavior with device selectors and Start/Stop; no automatic background listening |

W3 remains a separate Windows-only acceptance milestone. The W4 sender code was
implemented now to match the newer Mac handoff; its end-to-end acceptance remains
pending a manual session with the Mac receiver. Keep manual tests as the primary
validation. Do not build a large mocked/automated test suite for this personal
POC. Small targeted checks for framing/conversion are optional when they resolve
a concrete defect.

## 12. Manual results to record

Use `docs/MANUAL_TEST_RESULTS.md` with one row per scenario:

| Scenario | Candidate source correct? | Interviewer source correct? | Echo/duplication? | Stop/recovery result | Notes |
|---|---|---|---|---|---|
| Candidate only | | | | | |
| ChatGPT only | | | | | |
| Five alternating turns | | | | | |
| Brief overlap | | | | | |
| Silence then resume | | | | | |
| Selected device disconnect | | | | | |
| Network disconnect (W4+) | | | | | |
| Mac Finish/End (W5+) | | | | | |
| Longer session | | | | | |

Record Windows/Python/package versions, device names, native formats, frame/drop
counts and timing measurements. Never include real CVs, credentials or raw audio
in Git. Record failures as failures; successful compilation is not proof that
Windows capture or role routing works.

Timing targets are provisional: capture/transport should not introduce more than
roughly one second of growing backlog; queue overflow is explicit. Measure STT
and AI delays separately after W5. A slow answer is not automatically a capture
problem. Fix role separation and stability before tuning inference performance.

## 13. Privacy and data lifetime

- Capture starts only after the user's explicit Start action and a visible notice
  explaining both sources. Display capture state continuously.
- W4 GUI transport does not persist local audio. Frames are sent only to the
  configured Mac after receiver acceptance and cleared on Stop. The separate
  CLI `preview --save` option remains limited to a ten-second local check.
- No audio payloads, base64 frames, tokens or transcripts in diagnostic logs.
- Windows sends audio only to the configured local mock receiver or trusted Mac.
- ChatGPT Voice itself receives the microphone through its separate browser session;
  the agent does not control that service's retention. The Mac may separately send
  interview text/context to OpenAI under its existing disclosed configuration.
- No cloud hosting, telemetry, browser automation or database is required.

## 14. Current Windows handoff

W0 setup, W1 discovery/selection, W2 dual capture/preview code, and a Tkinter
GUI with the W4 WebSocket sender are present in this repository. Use `README.md`
for setup and the GUI workflow. The GUI starts Off with explicit Connect / Start
and Stop controls; the selected devices remain closed until the Mac accepts.
The sender does not record audio locally. The
built-in microphone array and Realtek speakers are selected in
`private/agent.json`; capture remains disabled and speaker-priority suppression
is enabled. W2 stream separation and local WAV saving have been manually
confirmed. W4 still needs an end-to-end check against the Mac receiver using its
displayed URL. Preserve the user's built-in mic and speaker setup for that test;
the headphones suggestion in the handoff is optional guidance, not a device
requirement. The receiver URL must be reachable on the trusted LAN.

## 15. Source references

Implementation should recheck current APIs and package compatibility when work
starts. Primary references used to prepare this plan:

- [Microsoft WASAPI loopback recording](https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording)
- [Microsoft process-loopback capture sample](https://learn.microsoft.com/en-us/samples/microsoft/windows-classic-samples/applicationloopbackaudio-sample/)
- [PyAudioWPatch maintainer repository](https://github.com/s0d3s/PyAudioWPatch)
- [PyAudioWPatch package and wheels](https://pypi.org/project/PyAudioWPatch/)
- [Python-SoXR streaming resampler](https://python-soxr.readthedocs.io/en/stable/soxr.html)
- [websockets client documentation](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html)
- [NAudio capture reference](https://naudio.github.io/NAudio/docs/WasapiRecorder.html)
- [OpenAI ChatGPT Voice guide](https://help.openai.com/en/articles/20001274-chatgpt-voice)
