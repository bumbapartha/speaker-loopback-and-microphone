"""WASAPI device enumeration. This module never opens an audio stream."""

from __future__ import annotations

import platform
from dataclasses import dataclass
from typing import Any


class DeviceDiscoveryError(RuntimeError):
    """Raised when WASAPI devices cannot be enumerated safely."""


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    host_api: str
    max_input_channels: int
    max_output_channels: int
    default_sample_rate: int
    is_loopback: bool
    format_supported: bool | None = None
    format_note: str | None = None
    loopback_index: int | None = None
    output_format_supported: bool | None = None
    output_format_note: str | None = None


def _clean_name(value: Any) -> str:
    return str(value).strip()


def enumerate_wasapi_devices() -> tuple[list[AudioDevice], list[AudioDevice], list[AudioDevice]]:
    """Return (microphones, playback endpoints, loopback inputs).

    PortAudio is initialized only for metadata enumeration. No input/output
    stream is opened, so calling this function cannot begin audio capture.
    """
    if platform.system() != "Windows":
        raise DeviceDiscoveryError("Device discovery requires Windows WASAPI.")

    try:
        import pyaudiowpatch as pyaudio
    except ImportError as exc:
        raise DeviceDiscoveryError(
            "PyAudioWPatch is not installed. Install this project in its virtual environment first."
        ) from exc

    pa = None
    try:
        pa = pyaudio.PyAudio()
        wasapi_index = pa.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
        wasapi_name = str(pa.get_host_api_info_by_index(wasapi_index).get("name", "WASAPI"))
        all_devices: list[dict[str, Any]] = []
        for index in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(index)
            if int(info.get("hostApi", -1)) == int(wasapi_index):
                all_devices.append(info)

        def to_device(info: dict[str, Any], supported: bool | None = None, note: str | None = None) -> AudioDevice:
            return AudioDevice(
                index=int(info["index"]),
                name=_clean_name(info.get("name", "(unnamed device)")),
                host_api=wasapi_name,
                max_input_channels=int(info.get("maxInputChannels", 0)),
                max_output_channels=int(info.get("maxOutputChannels", 0)),
                default_sample_rate=int(float(info.get("defaultSampleRate", 0))),
                is_loopback=bool(info.get("isLoopbackDevice", False)),
                format_supported=supported,
                format_note=note,
            )

        loopback_indexes = {
            int(info["index"])
            for info in pa.get_loopback_device_info_generator()
            if int(info.get("hostApi", -1)) == int(wasapi_index)
        }
        microphones: list[AudioDevice] = []
        playbacks: list[AudioDevice] = []
        loopbacks: list[AudioDevice] = []
        playback_loopbacks: dict[int, int] = {}

        for info in all_devices:
            index = int(info["index"])
            is_loopback = index in loopback_indexes or bool(info.get("isLoopbackDevice", False))
            input_channels = int(info.get("maxInputChannels", 0))
            output_channels = int(info.get("maxOutputChannels", 0))
            if is_loopback and input_channels > 0:
                loopbacks.append(to_device(info))
            elif output_channels > 0:
                playbacks.append(to_device(info))
                try:
                    analogue = pa.get_wasapi_loopback_analogue_by_dict(info)
                except Exception:
                    analogue = None
                if analogue:
                    analogue_index = int(analogue["index"])
                    playback_loopbacks[index] = analogue_index
                    found = next((item for item in loopbacks if item.index == analogue_index), None)
                    if found is None:
                        loopbacks.append(to_device(analogue))
            elif input_channels > 0:
                microphones.append(to_device(info))

        # The library's helper can add the analogue before the device iterator
        # reaches that same virtual input. Keep one row per PortAudio index.
        loopbacks = list({device.index: device for device in loopbacks}.values())

        # Check the advertised native format without opening a stream.
        checked: list[AudioDevice] = []
        for device in microphones + loopbacks:
            channels = min(max(device.max_input_channels, 1), 2)
            rate = device.default_sample_rate
            try:
                ok = bool(
                    pa.is_format_supported(
                        rate,
                        input_device=device.index,
                        input_channels=channels,
                        input_format=pyaudio.paInt16,
                    )
                )
                note = f"signed 16-bit PCM, {channels} ch, {rate} Hz"
            except Exception as exc:  # PortAudio raises version-specific exceptions here.
                ok = False
                note = f"advertised format rejected: {exc}"
            checked.append(
                AudioDevice(
                    **{**device.__dict__, "format_supported": ok, "format_note": note}
                )
            )
        support_by_index = {device.index: device for device in checked}
        microphones = [support_by_index[item.index] for item in microphones]
        loopbacks = [support_by_index.get(item.index, item) for item in loopbacks]
        checked_playbacks: list[AudioDevice] = []
        for device in playbacks:
            channels = min(max(device.max_output_channels, 1), 2)
            try:
                ok = bool(
                    pa.is_format_supported(
                        device.default_sample_rate,
                        output_device=device.index,
                        output_channels=channels,
                        output_format=pyaudio.paInt16,
                    )
                )
                note = f"signed 16-bit PCM, {channels} ch, {device.default_sample_rate} Hz"
            except Exception as exc:
                ok = False
                note = f"advertised output format rejected: {exc}"
            checked_playbacks.append(
                AudioDevice(
                    **{
                        **device.__dict__,
                        "loopback_index": playback_loopbacks.get(device.index),
                        "output_format_supported": ok,
                        "output_format_note": note,
                    }
                )
            )
        playbacks = checked_playbacks
        return microphones, playbacks, loopbacks
    except Exception as exc:
        if isinstance(exc, DeviceDiscoveryError):
            raise
        raise DeviceDiscoveryError(f"WASAPI enumeration failed: {exc}") from exc
    finally:
        if pa is not None:
            pa.terminate()


def create_device_selection(microphone_index: int, playback_index: int) -> dict[str, Any]:
    """Validate explicit WASAPI choices and return an Off-state config."""
    microphones, playbacks, loopbacks = enumerate_wasapi_devices()
    microphone = next((item for item in microphones if item.index == microphone_index), None)
    playback = next((item for item in playbacks if item.index == playback_index), None)
    if microphone is None:
        raise DeviceDiscoveryError(f"Microphone index {microphone_index} is not a current WASAPI microphone. Re-run 'devices'.")
    if playback is None:
        raise DeviceDiscoveryError(f"Playback index {playback_index} is not a current WASAPI playback endpoint. Re-run 'devices'.")
    if playback.output_format_supported is not True:
        raise DeviceDiscoveryError(f"Playback '{playback.name}' does not pass the advertised PCM16 output format check.")
    if microphone.format_supported is not True:
        raise DeviceDiscoveryError(f"Microphone '{microphone.name}' does not pass the advertised PCM16 format check.")
    loopback = next((item for item in loopbacks if item.index == playback.loopback_index), None)
    if loopback is None:
        raise DeviceDiscoveryError(f"No matching WASAPI loopback input was identified for '{playback.name}'.")
    if loopback.format_supported is not True:
        raise DeviceDiscoveryError(f"Loopback for '{playback.name}' does not pass the advertised PCM16 format check.")

    return {
        "capture_enabled": False,
        "speaker_priority": True,
        "sources": {
            "candidate": {
                "device_index": microphone.index,
                "device_name": microphone.name,
                "host_api": microphone.host_api,
                "sample_rate": microphone.default_sample_rate,
                "channels": min(microphone.max_input_channels, 2),
                "sample_format": "signed 16-bit PCM",
            },
            "interviewer": {
                "playback_index": playback.index,
                "playback_name": playback.name,
                "loopback_index": loopback.index,
                "loopback_name": loopback.name,
                "host_api": playback.host_api,
                "sample_rate": loopback.default_sample_rate,
                "channels": min(loopback.max_input_channels, 2),
                "playback_sample_rate": playback.default_sample_rate,
                "playback_channels": min(playback.max_output_channels, 2),
                "sample_format": "signed 16-bit PCM",
            },
        },
    }

