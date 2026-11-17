# Windows Interview Capture Agent — Implementation and Manual Test Plan

Status: proposed implementation plan; no capture-agent code has been implemented.
Prepared: 17 November 2026.
Suggested new repository: `interview-capture-windows`.

Copy this document into the new repository as `IMPLEMENTATION_PLAN.md`. It is
self-contained so implementation can begin on the Windows HP laptop without
access to the Mac repository. Commands and protocol examples below describe the
planned application; they are not existing commands or endpoints.

## 1. Objective and fixed role mapping

Build a small personal-use Windows application that captures two independent
audio streams and eventually sends them to the existing interview assistant on
the Mac:

| Windows audio source | Assigned role | Initial mock-interview source |
|---|---|---|
| Selected microphone input | CANDIDATE | You speaking into the HP microphone/headset |
| Loopback of selected playback device | INTERVIEWER | ChatGPT Voice speaking through the selected headphones |

Roles come from the capture source, not voice characteristics. No pitch filter,
voice calibration, voiceprint matching, gender inference or speaker diarisation
is required to distinguish these two roles. Multiple remote interviewers can
share the INTERVIEWER role. This does not identify individual interviewers.

Assumptions:

- Only the candidate speaks into the local microphone during an interview.
- ChatGPT Voice and, later, Teams use the selected playback device.
- Headphones are used for the first POC, keeping playback out of the microphone.
- The Windows agent is capture/transport software. The Mac owns transcription,
  CV/JD context, AI cues and the interview session.
- The first useful test must work entirely on Windows, without a Mac or API key
  in the capture agent.

## 2. Scope and practical limits

### Initial scope

- Explicit microphone and playback-loopback selection.
- Independent live level meters, counters and role labels.
- Start and Stop; release both devices cleanly on Stop or failure.
- Short, bounded in-memory previews for checking each channel separately.
- A manually operated ChatGPT Voice mock interview on Windows.
- A versioned transport contract and a local Windows mock receiver.
- Later, authenticated LAN delivery to a Mac receiver and role-aware transcripts.

### Deferred

- A background Windows service, automatic startup, installer and tray integration.
- Capturing only a particular application's audio.
- Loudspeaker acoustic echo cancellation, automatic ducking or stream suppression.
- Voice profiles, separating individual remote participants and overlap diarisation.
- Automatic control of ChatGPT Voice or Teams.
- Transcription, answer generation or OpenAI credentials inside the Windows agent.
- Saved recordings, cloud audio upload and exporting ChatGPT voice recordings.

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
6. Connect headphones, preferably a wired/USB headset for the initial test. Select
   the same microphone/playback devices in the browser's voice conversation.
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
- VAD belongs downstream per source. It must not remove the source label or cause
  genuine microphone speech to be dropped while playback is active.

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

Headphones are the initial requirement. Keep both streams during simultaneous
speech. **Do not mute or duck the microphone just because playback has energy.**
That would lose candidate answers during interruptions.

If loudspeakers become necessary, add reference-based acoustic echo cancellation
as a separate milestone, preferably on Windows before transmission. Simple VAD
or energy comparison is not a substitute. Echo cancellation requires the render
reference and microphone signals with appropriate timing. [WebRTC processing interface](https://webrtc.googlesource.com/src/+/e583174d1ed9ec00ab0e1b3ebb5989ef6aae4223/modules/audio_processing/include/audio_processing.h)

## 7. Planned commands and operator flow

These commands are implementation targets, not currently installed tools:

```powershell
.\.venv\Scripts\python.exe -m capture_agent devices
.\.venv\Scripts\python.exe -m capture_agent monitor --config private\agent.json
.\.venv\Scripts\python.exe -m capture_agent preview --config private\agent.json --seconds 10
.\.venv\Scripts\python.exe -m capture_agent mock-receiver --host 127.0.0.1 --port 8765
.\.venv\Scripts\python.exe -m capture_agent send --config private\agent.json
```

`devices` lists input and playback-loopback devices, their native formats and current
indices/identifiers. Revalidate selections after reboot; a numeric device index
alone is not a durable identity.

`monitor` shows both meters and counters, without networking or file recording.
`preview` captures both channels into bounded RAM, stops capture, then lets the
user listen to Candidate and Interviewer separately. Never replay a preview while
capture is active, or the playback could be captured again as interviewer audio.
Stop the ChatGPT Voice conversation before replaying previews.

`send` opens the receiver connection, waits for acceptance, then captures and sends
both sources. Ctrl+C/Stop closes both handles, clears queues and terminates the
capture. There is no always-listening mode.

## 8. ChatGPT Voice mock interview — Windows only

Use a ChatGPT voice conversation in the **Windows browser**, with headphones.
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
4. Candidate audio should remain near silence with headphones; small physical
   leakage/noise is possible and must be evaluated by listening, not meters alone.
5. If Interviewer is silent, verify the browser's playback endpoint and the matching
   loopback device. Capturing a different output device will not work.

### Test C — alternating interview

1. Run five question/answer turns using the prompt above.
2. Begin your answers with “Candidate answer alpha,” “Candidate answer beta,” etc.
3. Observe both meters and collect short separate previews as needed.
4. Verify that ChatGPT questions are routed to Interviewer and your answers to
   Candidate. No voice profile should be involved.
5. For long runs, monitor only. Use bounded short previews for listening checks;
   do not accumulate a full interview recording in RAM.

### Test D — overlap and silence

1. Speak briefly while ChatGPT is speaking. Your microphone audio must still be
   captured; interviewer playback must continue independently.
2. Leave both sides silent for at least 20 seconds, then resume.
3. Check that loopback resumes and elapsed time includes the silence.
4. Repeat with a 15–30 minute mock interview to check drift and bounded queues.

### Test E — limitations and recovery

- Play an unrelated notification: endpoint loopback may capture it. This demonstrates
  why other playback should be quiet during a real interview.
- Unplug the selected headset: both captures should stop with an actionable error.
- Change the Windows default device: do not silently relabel or switch sources.
- Stop the agent while ChatGPT keeps speaking: no more audio should be captured.
- Check that no WAV files or audio-content logs were created.

**Pass condition:** you can hear correct source separation in short previews and
observe stable alternating/overlapping capture without a Mac or any calibration.
Meters alone are insufficient evidence. If this fails, fix capture/routing before
adding transcription, networking or AI.

## 9. Proposed transport contract — version 1

Use one WebSocket connection carrying two role-tagged streams. Start with JSON
messages and base64 PCM for simplicity; binary framing can be a later measured
optimization. At 16 kHz mono PCM, both streams total 64,000 raw bytes/second;
base64 with per-frame padding increases the audio payload to 85,600 bytes/second before JSON
and protocol overhead. This is a design calculation, not a measured throughput.

### Connection and lifecycle

- Local mock: `ws://127.0.0.1:8765/ws/capture-agent`.
- Mac integration: `wss://<mac-host>:8765/ws/capture-agent`.
- Authenticate the network connection with a bearer token in the handshake header,
  not in a URL. Verify TLS using a trusted certificate or a pinned private CA;
  never disable certificate verification as the normal setup.
- Tokens/configuration/certificates live under ignored `private/`. No OpenAI key
  belongs on the agent.
- Production session ID comes from the Mac interview UI. Local mock mode may use
  its own test session ID and must be explicitly marked as mock mode.

The agent sends a start message with protocol version, interview session ID, a new
capture ID, normalized format and exactly two sources: `candidate` and `interviewer`.
The receiver validates it and replies `capture.accepted` before audio starts.

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

The current Mac app has browser-originated microphone capture, loopback-only HTTP
exposure, and speech orchestration built around one active input. It does **not**
already support this protocol. Two sources cannot simply be pushed into the old
single-source socket or concatenated into one waveform.

Required Mac-side tasks:

1. Add a **Windows agent** source option to setup. In this mode, Start creates the
   interview/session pairing details and waits for the agent; the Mac browser must
   not request its own microphone or start duplicate local capture.
2. Add a dedicated authenticated LAN ingress. Keep CV/JD/session administration
   endpoints loopback-only rather than exposing the entire existing API to the LAN.
   A thin Python WebSocket/TLS bridge on port 8765 is the proposed ingress; it can
   forward only validated frames to a new loopback-only Java agent endpoint.
3. Reuse the existing Java/Angular application and native local Whisper service.
   The bridge is transport-only, not a rewrite of the Mac processor.
4. Add independent VAD/utterance state and stream timelines for candidate and
   interviewer. Refactor the current single-active-input orchestration as needed.
   Sharing one Whisper model is fine, but STT jobs need bounded, fair scheduling;
   one source must not overwrite or suppress the other's pending utterance.
5. Attach source-derived CANDIDATE/INTERVIEWER roles to partial and final text.
   Bypass voiceprint matching and calibration in Windows-agent mode.
6. Include stream and capture IDs in segment identity, preserve capture timestamps,
   and merge transcripts by speech time rather than recognition-completion order.
   Overlap is valid. Late STT results must retain their original source/time.
7. Add both roles to the same existing OpenAI interview conversation. Send CV/JD
   once; append new transcript turns with known source roles. Update AI instructions
   to distinguish source-provided roles from older estimated voice matches.
8. For out-of-order completion, carry timestamps and instruct context handling
   appropriately; order utterances over a small bounded window rather than waiting
   indefinitely for one stream. Do not silently claim perfect chronological order.
9. Show Windows connection state, both source meters, transcript roles, gaps and
   late results. Keep existing short topic cues and manual Refresh cues.
10. Finish/End must stop the agent, discard pending local audio and reject late
    frames. End uses the existing best-effort provider-conversation cleanup.

Receiver prerequisites are an integration milestone, not a blocker for Windows-only
capture tests. Define the version-1 protocol in both repositories before building
this adapter. Do not edit the existing Mac app as part of W0–W4.

## 11. Implementation milestones

| Milestone | Deliverable | Manual acceptance |
|---|---|---|
| W0 — Windows bootstrap | Python package, pinned working dependencies, README, ignored private config; clean platform checks | Package starts on the HP; no audio begins at import/startup |
| W1 — Device discovery | Enumerate WASAPI mic/playback-loopback, explicit mapping and format validation | Correct headset devices can be selected; stale/invalid selections fail clearly |
| W2 — Dual capture and preview | Two independent streams, conversion, meters, bounded RAM preview and Stop | Microphone-alone and playback-alone tests pass; previews play only after capture stops |
| W3 — ChatGPT mock interview | Five-turn Windows-only workflow and manual results document | Alternating, overlap, silence and Stop checks pass without Mac/calibration |
| W4 — Transport with Windows mock receiver | Version-1 framing, authentication, acknowledgements, bounded queues and failure handling | Both roles arrive at local mock receiver; disconnect/retry cannot replay old audio |
| W5 — Mac integration | Dedicated ingress plus existing-app source adapter and two-source STT | Windows ChatGPT mock produces correctly role-tagged Mac transcripts and short topic cues |
| W6 — Teams and longer runs | Real call-device checks, 30–60 minute session, recovery and timeline checks | Correct roles, bounded memory and clean Stop/End; observed latency/drift documented |
| W7 — Optional convenience | Small GUI and packaging only if the CLI POC is useful | Same capture behavior with device selectors and Start/Stop; no automatic background listening |

Implement in order. W3 is the first central success criterion; do not postpone it
until the Mac integration. Keep manual tests as the primary validation. Do not
build a large mocked/automated test suite for this personal POC. Small targeted
checks for framing/conversion are optional when they resolve a concrete defect.

## 12. Manual results to record

Use `docs/MANUAL_TEST_RESULTS.md` with one row per scenario:

| Scenario | Candidate source correct? | Interviewer source correct? | Echo/duplication? | Stop/recovery result | Notes |
|---|---|---|---|---|---|
| Candidate only | | | | | |
| ChatGPT only | | | | | |
| Five alternating turns | | | | | |
| Brief overlap | | | | | |
| Silence then resume | | | | | |
| Headset disconnect | | | | | |
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
- No persistent audio recording by default. Previews and transport buffers are
  bounded RAM only and cleared on Stop/End; managed-memory cleanup is best effort.
- No audio payloads, base64 frames, tokens or transcripts in diagnostic logs.
- Windows sends audio only to the configured local mock receiver or trusted Mac.
- ChatGPT Voice itself receives the microphone through its separate browser session;
  the agent does not control that service's retention. The Mac may separately send
  interview text/context to OpenAI under its existing disclosed configuration.
- No cloud hosting, telemetry, browser automation or database is required.

## 14. Handoff instruction for implementation on Windows

Copy the following request into the new repository's coding session:

> Read IMPLEMENTATION_PLAN.md. Implement W0 and W1 only on this Windows machine.
> Build a small Python CLI with explicit WASAPI microphone and playback-loopback
> device discovery. Microphone is Candidate; loopback is Interviewer. Verify the
> selected device formats and Windows dependencies, pin working versions, and
> document the exact commands used. Do not begin capture automatically, write
> recordings, introduce voice calibration or call OpenAI. Keep the implementation
> simple and use manual checks. Report device-enumeration results without secrets.
> Stop before W2 so I can inspect device selection.

After W1 is checked, proceed to W2 and then W3 to test the ChatGPT mock interview
on Windows. Only after those pass should transport and Mac integration begin.

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
