"""Small command-line entry point; capture commands require interactive confirmation."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path
from datetime import datetime
from uuid import uuid4

from . import __version__
from .devices import DeviceDiscoveryError, create_device_selection, enumerate_wasapi_devices


def _device_line(device, role: str) -> str:
    channels = device.max_input_channels if role != "PLAYBACK" else device.max_output_channels
    detail = f"{channels} ch, {device.default_sample_rate} Hz"
    if device.format_supported is not None:
        state = "supported" if device.format_supported else "unsupported"
        detail += f"; PCM16 input check: {state} ({device.format_note})"
    if role == "PLAYBACK":
        paired = f"loopback index {device.loopback_index}" if device.loopback_index is not None else "matching loopback not identified"
        detail += f"; {paired}"
        if device.output_format_supported is not None:
            state = "supported" if device.output_format_supported else "unsupported"
            detail += f"; PCM16 output check: {state} ({device.output_format_note})"
    return f"  [{device.index}] {device.name} — {role} — {detail}"


def command_devices(_args: argparse.Namespace) -> int:
    try:
        microphones, playbacks, loopbacks = enumerate_wasapi_devices()
    except DeviceDiscoveryError as exc:
        print(f"Device discovery unavailable: {exc}", file=sys.stderr)
        return 2

    print("Capture state: OFF (device enumeration only; no audio stream is open).")
    print("\nMicrophones (Mic):")
    if microphones:
        for device in microphones:
            print(_device_line(device, "Mic"))
    else:
        print("  No WASAPI microphone inputs found.")

    print("\nPlayback endpoints (select one for Speaker loopback):")
    if playbacks:
        for device in playbacks:
            print(_device_line(device, "PLAYBACK"))
    else:
        print("  No WASAPI playback endpoints found.")

    print("\nPlayback loopback inputs (Speaker):")
    if loopbacks:
        for device in loopbacks:
            print(_device_line(device, "Speaker"))
    else:
        print("  No WASAPI loopback inputs found.")
    print("\nDevice indexes can change after reconnects or reboot. Re-run this command before selection.")
    return 0


def command_select(args: argparse.Namespace) -> int:
    try:
        config = create_device_selection(args.microphone_index, args.playback_index)
    except DeviceDiscoveryError as exc:
        print(f"Device discovery unavailable: {exc}", file=sys.stderr)
        return 2
    output_path = Path(args.config)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    candidate = config["sources"]["candidate"]
    interviewer = config["sources"]["interviewer"]
    print("Selected devices saved. Capture state remains OFF; no audio stream was opened.")
    print(f"Mic microphone: [{candidate['device_index']}] {candidate['device_name']}")
    print(f"Speaker playback: [{interviewer['playback_index']}] {interviewer['playback_name']}")
    print(f"Speaker loopback: [{interviewer['loopback_index']}] {interviewer['loopback_name']}")
    print(f"Configuration: {output_path}")
    return 0


def _capture_notice(args: argparse.Namespace) -> bool | None:
    from .capture import CaptureError, load_selection

    try:
        config = load_selection(args.config)
    except CaptureError as exc:
        print(exc, file=sys.stderr)
        return False
    candidate = config["sources"]["candidate"]
    interviewer = config["sources"]["interviewer"]
    if not sys.stdin.isatty():
        print("An interactive console is required to start capture. Capture remains OFF.", file=sys.stderr)
        return None
    print("\nCAPTURE NOTICE: confirming will turn audio capture ON for these two inputs:")
    print(f"  Mic microphone: [{candidate['device_index']}] {candidate['device_name']}")
    print(f"  Speaker playback loopback: [{interviewer['loopback_index']}] {interviewer['loopback_name']}")
    print("The agent captures both sources independently. It does not change drivers or mute other apps.")
    print(f"Speaker priority: {'ON' if config.get('speaker_priority', True) else 'OFF'}; when ON, Mic audio is suppressed during speaker activity.")
    if args.command == "preview" and args.save:
        print(f"This preview will also save two separate WAV files under: {args.save_dir}")
    expected = "PREVIEW" if args.command == "preview" else "START"
    try:
        answer = input(f"Type {expected} to switch capture ON; anything else cancels: ")
    except EOFError:
        print("No interactive confirmation was received. Capture remains OFF.")
        return None
    if answer != expected:
        print("Cancelled. Capture state: OFF.")
        return None
    return True


def command_monitor(args: argparse.Namespace) -> int:
    confirmed = _capture_notice(args)
    if confirmed is not True:
        return 2 if confirmed is False else 0

    from .capture import CaptureError, CaptureSession
    import msvcrt
    import time

    session = CaptureSession(args.config)
    try:
        session.start()
        started = time.perf_counter()
        print("\nCAPTURE ON. Press S or Ctrl+C to Stop both sources.")
        while True:
            session.check_health()
            if msvcrt.kbhit() and msvcrt.getwch().lower() == "s":
                break
            candidate = session.metrics("candidate")
            interviewer = session.metrics("interviewer")
            elapsed = time.perf_counter() - started
            line = (
                f"\rON {elapsed:6.1f}s | Mic rms {candidate['rms']:.3f} peak {candidate['peak']:.3f} "
                f"captured {candidate['captured_seconds']:.1f}s queued {candidate['queued_frames']} dropped {candidate['dropped_frames']} "
                f"speaker-suppressed {candidate['speaker_suppressed_frames']} "
                f"| Speaker rms {interviewer['rms']:.3f} peak {interviewer['peak']:.3f} "
                f"captured {interviewer['captured_seconds']:.1f}s queued {interviewer['queued_frames']} dropped {interviewer['dropped_frames']}"
            )
            print(line, end="", flush=True)
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    except CaptureError as exc:
        print(f"\nCapture stopped: {exc}", file=sys.stderr)
        return 1
    finally:
        session.stop(clear=True)
        print("\nCAPTURE OFF. Both devices released; in-memory queues and previews cleared.")
    return 0


def command_preview(args: argparse.Namespace) -> int:
    if not 0 < args.seconds <= 10:
        print("Preview duration must be greater than 0 and no more than 10 seconds.", file=sys.stderr)
        return 2
    confirmed = _capture_notice(args)
    if confirmed is not True:
        return 2 if confirmed is False else 0

    from .capture import CaptureError, CaptureSession
    import time

    session = CaptureSession(args.config)
    captured = False
    try:
        session.start()
        print(f"\nCAPTURE ON for up to {args.seconds:g} seconds. Press Ctrl+C to stop early.")
        deadline = time.perf_counter() + args.seconds
        while time.perf_counter() < deadline:
            session.check_health()
            remaining = max(0.0, deadline - time.perf_counter())
            print(f"\rCAPTURE ON | {args.seconds - remaining:.1f}/{args.seconds:g}s", end="", flush=True)
            time.sleep(min(0.05, remaining))
        captured = True
    except KeyboardInterrupt:
        captured = True
    except CaptureError as exc:
        print(f"\nCapture stopped: {exc}", file=sys.stderr)
        return 1
    finally:
        session.stop(clear=not captured)

    if not captured:
        return 1
    try:
        interviewer = session.config["sources"]["interviewer"]
        from .preview import play_preview, save_preview_wav

        print("\nCAPTURE OFF. Both input devices are released.")
        if args.save:
            capture_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
            output_dir = Path(args.save_dir) / capture_id
            candidate_path = save_preview_wav(session.sources["candidate"].ring.snapshot(), output_dir / "mic.wav")
            interviewer_path = save_preview_wav(session.sources["interviewer"].ring.snapshot(), output_dir / "speaker.wav")
            print("Saved separate mono 16 kHz PCM WAV files:")
            print(f"  Mic: {candidate_path}")
            print(f"  Speaker: {interviewer_path}")
        print("Stop ChatGPT Voice now so it cannot speak over or react to the preview.")
        try:
            playback_choice = input("Press Enter to play the previews separately, or type Q to skip playback: ")
        except EOFError:
            playback_choice = "Q"
        if playback_choice.strip().lower() == "q":
            print("Playback skipped. Any explicitly saved WAV files remain in place. Capture remains OFF.")
            return 0
        for key in ("candidate", "interviewer"):
            source = session.sources[key]
            if key == "candidate" and source.converter:
                print(f"Mic frames suppressed during speaker activity: {source.converter.suppressed_frames}.")
            print(f"Playing {source.label} preview ({len(source.ring.snapshot()) * 0.02:.1f} seconds).")
            play_preview(
                source.ring.snapshot(),
                interviewer["playback_index"],
                interviewer["playback_name"],
                interviewer["playback_sample_rate"],
                interviewer["playback_channels"],
            )
    except Exception as exc:
        print(f"Preview playback failed: {exc}", file=sys.stderr)
        return 1
    finally:
        session.clear_previews()
        print("Preview buffers cleared from memory (best effort). Capture remains OFF.")
    return 0


def command_gui(args: argparse.Namespace) -> int:
    from .gui import launch

    launch(args.config)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capture-agent",
        description="Windows dual-source capture. Capture remains OFF until explicitly confirmed.",
    )
    parser.add_argument("--version", action="version", version=f"capture-agent {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    devices = subparsers.add_parser("devices", help="list WASAPI microphones and playback loopbacks")
    devices.set_defaults(handler=command_devices)
    select = subparsers.add_parser("select", help="save explicit devices for the fixed role mapping (capture stays off)")
    select.add_argument("--microphone-index", type=int, required=True)
    select.add_argument("--playback-index", type=int, required=True)
    select.add_argument("--config", default="private/agent.json")
    select.set_defaults(handler=command_select)
    monitor = subparsers.add_parser("monitor", help="explicitly start live meters; press S or Ctrl+C to stop")
    monitor.add_argument("--config", default="private/agent.json")
    monitor.set_defaults(handler=command_monitor)
    preview = subparsers.add_parser("preview", help="explicitly capture up to 10 seconds, stop, then play sources separately")
    preview.add_argument("--config", default="private/agent.json")
    preview.add_argument("--seconds", type=float, default=10.0)
    preview.add_argument("--save", action="store_true", help="save separate WAV files under the ignored private folder")
    preview.add_argument("--save-dir", default="private/recordings")
    preview.set_defaults(handler=command_preview)
    gui = subparsers.add_parser("gui", help="open the Start/Stop window with meters and device selection")
    gui.add_argument("--config", default="private/agent.json")
    gui.set_defaults(handler=command_gui)
    return parser


def main() -> int:
    if platform.system() != "Windows":
        print("This capture agent currently supports Windows only.", file=sys.stderr)
        return 2
    args = build_parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())

