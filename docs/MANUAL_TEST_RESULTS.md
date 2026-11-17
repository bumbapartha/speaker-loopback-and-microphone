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
