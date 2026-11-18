# Windows Interview Capture Agent

W0–W2 capture and W4 LAN sender: a Windows app that discovers WASAPI devices,
explicitly captures a selected microphone and playback loopback, and streams
them to the configured Mac receiver. Microphones are
assigned the **CANDIDATE** role; playback loopback is assigned **INTERVIEWER**.

## Safety and current scope

The app starts **OFF**. Importing it, listing devices, and saving a device
selection do not open an audio stream. `monitor` and `preview` display the two
selected sources and require an exact interactive confirmation before opening
both input streams. `monitor` shows separate meters until you press `S` or
Ctrl+C, then clears queues and previews. `preview` captures at most ten seconds
into bounded RAM, stops and releases both inputs, then plays Candidate and
Interviewer separately. It pauses after capture so you can stop ChatGPT Voice
before pressing Enter to play, or type `Q` to skip playback. Preview buffers are
cleared after playback.

No audio driver, virtual device, Windows audio setting, or system-wide routing is
installed or changed. The app does not require administrator privileges. Audio
is sent only to the explicitly configured receiver after you click Connect / Start
and `capture.accepted` plus transmit=true arrive. This sender does not save local audio
recordings. Transcription and AI remain on the Mac.

## Windows setup

Use 64-bit Python 3.12 as specified in the implementation plan. Python 3.13 is
also supported and was used for the device discovery on this laptop.

```powershell
py -3.12 -m venv .venv
\.venv\Scripts\python.exe -m pip install --upgrade pip
\.venv\Scripts\python.exe -m pip install -e .
\.venv\Scripts\python.exe -m capture_agent devices
```

The command lists WASAPI microphones and output endpoints with their indexes,
native rates and channel counts, and the corresponding input devices in
loopback mode. It checks whether the advertised native rate/channel count can
be queried as signed 16-bit PCM without opening the stream. A failed format
check is reported for inspection; it does not trigger capture or a device
change. Re-run device discovery after reconnecting hardware or rebooting because
PortAudio indexes are session-dependent.

After identifying the built-in microphone and speaker, save the explicit mapping with
the current indexes:

```powershell
\.venv\Scripts\python.exe -m capture_agent select --microphone-index 9 --playback-index 8
\.venv\Scripts\python.exe -m capture_agent monitor --config private\agent.json
\.venv\Scripts\python.exe -m capture_agent preview --config private\agent.json --seconds 10
\.venv\Scripts\python.exe -m capture_agent preview --config private\agent.json --seconds 10 --save
\.venv\Scripts\python.exe -m capture_agent gui --config private\agent.json
```

In the GUI, paste the Mac receiver URL (for example
`ws://192.168.1.103:8765/ws/windows-agent`) and click **Connect / Start** once.
You can start Windows before the Mac app. While disconnected, the agent checks
`http://<MAC_IP>:8765/api/v1/windows-receiver/health` every five seconds. Mac
unavailable or ready=false means **WAITING**, with both audio devices Off.
Once the interview is active and **Use Windows audio** is checked, it connects
automatically. The URL is saved in ignored `private/receiver.json`.

After capture.accepted with transmit=true, the sender opens the selected inputs.
Unchecking Windows audio sends capture.control transmit=false: both inputs close,
queued audio is discarded, and the socket keeps exchanging one-second heartbeats.
Rechecking resumes live capture on the same connection with the same captureId,
sequence counters and normalized timestamps. No paused audio is replayed. Source
timestamps freeze during the pause; candidate suppression during active streaming
still sends silence frames to preserve timing. Interviewer audio is never gated.

Connection loss, normal close 1000, Mac Finish/End or ten seconds without a pong
or acknowledgement releases both inputs and returns to five-second readiness
polling. Reconnection uses a new captureId and counters, without an old backlog.
**Stop** or closing the window disables automatic reconnect and releases inputs.
Queue overflow, capture interruptions and network failures release both inputs,
clear queued audio and retry automatically after five seconds without an error
popup. A new connection uses fresh capture counters and never replays old audio.
If a selected device remains unavailable, the agent keeps waiting; use Stop to
change device selection. No certificate, token or session ID is needed.

The window shows WAITING, CONNECTING, PAUSED or CAPTURE ON, per-source meters and
sent/acknowledged counts. Each source retains its own bounded queue and flow-control
window. Transcription and AI run on the Mac. See WINDOWS_AGENT_HANDOFF.md for the
exact readiness and control contract.

Replace the example indexes with those shown by `devices`. Selection validates
that the microphone is an input, the playback device has a matching WASAPI
loopback input, and both pass the advertised PCM16 format check. It writes the
selection to ignored `private/agent.json`, with capture still disabled. If an
index is stale or invalid, selection fails and does not switch to another
device. No capture or playback stream is opened by `select`.

Use the selected playback endpoint's matching loopback input, not a similarly
named microphone. Keep the microphone-to-CANDIDATE and playback-loopback-to-
INTERVIEWER mapping fixed. Device names and indexes are diagnostic and local.

## Dependency pins and W1 device discovery

PyAudioWPatch `0.2.12.9`, NumPy `2.5.5`, SoXR `1.1.0`, and websockets
`15.0.1` are pinned. The
editable package and dependencies installed under Python 3.13.14 on this
laptop. Device discovery found one 2-channel, 48 kHz microphone and one
2-channel, 48 kHz Realtek playback endpoint with matching loopback; PCM16 format
checks passed for both inputs and the output endpoint. These built-in devices
are now saved in ignored `private/agent.json`, with capture still disabled.

With the built-in speaker, the microphone can also hear the interviewer
acoustically. Speaker-priority is enabled in the saved config: Candidate frames
are replaced with silence while loopback speaker activity is present, plus a
350 ms hangover. The Interviewer stream is never gated. Candidate speech during
that time will be lost, as you said is acceptable. This is software frame
suppression; it does not mute the Windows microphone device or use AEC. Quiet
speaker audio or frame-boundary echo can still appear in Candidate.

## W2 behavior

- Each source has an independent raw queue capped at 50 packets and less than
  one second of native audio, a stateful resampler, meter, and ten-second RAM
  preview ring. Audio is converted to mono signed 16-bit PCM at 16 kHz and split
  into 20 ms frames.
- Capture queue overflow, PortAudio input overflow, a stopped stream, microphone
  callback stall, format error, or device loss stops both streams and shows an
  error. Device indexes and names are rechecked before every start; the agent
  will not switch to another device.
- Playback loopback idle periods are represented with timed zero frames. Preview
  playback opens only the selected output after both input streams have stopped.
- Speaker-priority mode waits for the aligned loopback timeline, then zeros
  Candidate frames during active speaker playback and for 350 ms afterward. The
  monitor shows how many Candidate frames were suppressed. Interviewer data is
  processed independently and is not suppressed. If the loopback reference is
  late, Candidate waits at most 350 ms, then uses silence until the missing reference catches up. It does not wait repeatedly
  for each queued frame. Interviewer frames are kept unchanged.
- CLI capture opens only after the interactive `START` or `PREVIEW` confirmation.
  Noninteractive invocations cannot start capture.
- The `gui` window starts Off, lets you select and save the two devices, and
  shows independent live peak bars, RMS, sent / acknowledged counts, and
  speaker-suppressed Candidate frames. Clicking Connect / Start is the explicit
  action; both audio devices remain closed until the receiver accepts and requests
  transmission. Stop and window
  close release both inputs. The sender does not save recordings.

The user manually confirmed a saved GUI conversation sounded correct; see
`docs/MANUAL_TEST_RESULTS.md`. W4 Mac receiver transport acceptance is still
pending. Keep the selected built-in microphone and speaker for the user's setup.

The optional CLI preview `--save` flag writes two separate mono 16 kHz PCM WAV files under
`private/recordings/<capture-id>/candidate.wav` and `interviewer.wav`. That
folder is ignored by Git. Saving requires the explicit `PREVIEW` confirmation;
without `--save`, no audio files are written. Candidate WAVs contain the
speaker-priority suppression described above.

