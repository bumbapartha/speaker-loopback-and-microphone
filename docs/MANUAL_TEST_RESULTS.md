# Manual Test Results

## W2 saved preview capture

Date: 2026-11-17  
Python: 3.13.14  
PyAudioWPatch: 0.2.12.9  
Devices: Intel Smart Sound microphone array (index 9); Realtek speaker playback
(index 8), matching loopback input (index 10)  
Capture state after test: Off; both input streams released.

| File | Duration | Format | RMS | Peak (PCM16) | Observation |
|---|---:|---|---:|---:|---|
| `private/recordings/20261117_202026_8111e5bb/candidate.wav` | 10.00 s | Mono, 16 kHz, PCM16 | 0.00003 | 20 | Near-silent mic noise; no clear speech |
| `private/recordings/20261117_202026_8111e5bb/interviewer.wav` | 10.00 s | Mono, 16 kHz, PCM16 | 0.00000 | 0 | No playback audio present during capture |

Result: **inconclusive, not a pass**. The commands opened and stopped both
streams and wrote two separate WAVs, but this capture window did not contain
audible test speech or speaker playback. Repeat while ChatGPT Voice is actively
speaking through the selected Realtek speakers. Candidate speech is optional in
speaker-priority mode; Candidate frames may be zeroed while playback is active.

These WAVs are local test artifacts under the ignored `private/` directory and
must not be committed. Future previews remain in-memory only unless `--save` is
explicitly supplied.

## W2 GUI recording confirmation

Date: 2026-11-17
Session folder: `private/recordings/20261117_204611_3c921679/`
Result: **user confirmed both separate recordings were present and sounded
correct** after a manual Start / Stop conversation. Numeric levels and exact
scenario details were not recorded. This validates the local W2 recording path;
it does not validate the new W4 network sender.

## Sender readiness/control check on Mac (synthetic audio only)

Date: 2026-11-17

Python source compilation and a temporary local HTTP/WebSocket mock check passed:
waiting with no capture before ready; automatic connection after ready; separate
Candidate/Interviewer payloads and counters; pause with heartbeat and no PCM;
resume with the same captureId/counters; automatic retry after normal close 1000;
fresh captureId/counters after reconnect; Stop disables retries. No test framework
or permanent test suite was added. A per-source sink callback binding was also
corrected so converters retain their own sinks.

This check used synthetic capture callbacks, not WASAPI hardware or the Tk GUI.
On the HP laptop, run the GUI, click Connect / Start once, then start the Mac
interview and toggle Use Windows audio off/on. Confirm both role transcripts,
paused audio silence, automatic recovery after restarting the Mac, and Stop.
Real Windows audio/GUI acceptance remains pending.

## Candidate queue overflow fix (synthetic reference stall)

The Candidate worker previously waited up to 1.2 seconds for loopback alignment,
longer than its native input queue capacity. A focused synthetic check reproduced
an overflow at 46 queued packets. With a 350 ms maximum wait and a silence fallback
that avoids repeated waits until the delayed reference catches up, the same check
completed without overflow (14 packets maximum in this run). Separate checks
confirmed recovery after the reference catches up, Candidate suppression during
speaker activity, and unchanged Interviewer frames. Source compilation passed.
Stop now releases alignment waits before joining workers; callback timestamps are
no longer overwritten by older queued packets. Actual HP audio retesting is pending.

## Automatic capture recovery check

Date: 2026-11-18

A focused synthetic sender check injected a Candidate queue-overflow CaptureError.
The sender released capture, showed a non-error recovery status, retried readiness
after five seconds and resumed with a fresh captureId/counters. Normal WebSocket
close also recovered automatically. Pause/resume heartbeats, role-separated frames
and Stop disabling retries still passed. Python compilation passed. Runtime error
popups were removed; configuration validation still reports invalid selections.
Actual Windows hardware recovery remains a manual check after updating the HP app.
