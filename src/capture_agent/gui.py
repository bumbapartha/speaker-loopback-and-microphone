"""Small Tkinter window for explicit device selection and manual W2 checks."""

from __future__ import annotations

import json
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any
from datetime import datetime
from uuid import uuid4

from .capture import CaptureError, CaptureSession
from .devices import AudioDevice, DeviceDiscoveryError, create_device_selection, enumerate_wasapi_devices


class CaptureWindow:
    def __init__(self, root: tk.Tk, config_path: str | Path) -> None:
        self.root = root
        self.config_path = Path(config_path)
        self.session: CaptureSession | None = None
        self.microphones: list[AudioDevice] = []
        self.playbacks: list[AudioDevice] = []
        self._mic_by_label: dict[str, AudioDevice] = {}
        self._playback_by_label: dict[str, AudioDevice] = {}
        self.root.title("Windows Capture Agent")
        self.root.minsize(650, 430)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.state_var = tk.StringVar(value="CAPTURE OFF")
        self.message_var = tk.StringVar(value="Select devices, then press Start when ready.")
        self.mic_var = tk.StringVar()
        self.playback_var = tk.StringVar()
        self.speaker_priority_var = tk.BooleanVar(value=True)
        self.save_recordings_var = tk.BooleanVar(value=True)
        self._load_saved_config()
        self._build_widgets()
        self.refresh_devices()
        self.root.after(200, self._refresh_meters)

    def _load_saved_config(self) -> None:
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
            candidate = config["sources"]["candidate"]
            interviewer = config["sources"]["interviewer"]
            self.mic_var.set(str(candidate.get("device_index", "")))
            self.playback_var.set(str(interviewer.get("playback_index", "")))
            self.speaker_priority_var.set(bool(config.get("speaker_priority", True)))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _build_widgets(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        outer.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        outer.columnconfigure(1, weight=1)

        ttk.Label(outer, textvariable=self.state_var, font=("Segoe UI", 16, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4)
        )
        ttk.Label(outer, textvariable=self.message_var, wraplength=610).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(0, 14)
        )

        ttk.Label(outer, text="CANDIDATE microphone").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=4)
        self.mic_combo = ttk.Combobox(outer, textvariable=self.mic_var, state="readonly", width=66)
        self.mic_combo.grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Label(outer, text="INTERVIEWER playback").grid(row=3, column=0, sticky="w", padx=(0, 12), pady=4)
        self.playback_combo = ttk.Combobox(outer, textvariable=self.playback_var, state="readonly", width=66)
        self.playback_combo.grid(row=3, column=1, columnspan=2, sticky="ew", pady=4)

        self.priority_check = ttk.Checkbutton(
            outer,
            text="Speaker priority: suppress Candidate while speaker audio is active",
            variable=self.speaker_priority_var,
        )
        self.priority_check.grid(row=4, column=0, columnspan=3, sticky="w", pady=(7, 12))

        self.recording_check = ttk.Checkbutton(
            outer,
            text="Save separate Candidate and Interviewer WAV files under private\\recordings",
            variable=self.save_recordings_var,
        )
        self.recording_check.grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 10))

        ttk.Separator(outer).grid(row=6, column=0, columnspan=3, sticky="ew", pady=4)
        self.candidate_rms = tk.StringVar(value="RMS 0.000 | peak 0.000 | captured 0.0 s")
        self.interviewer_rms = tk.StringVar(value="RMS 0.000 | peak 0.000 | captured 0.0 s")
        ttk.Label(outer, text="CANDIDATE").grid(row=7, column=0, sticky="w", pady=(8, 2))
        self.candidate_bar = ttk.Progressbar(outer, maximum=100, mode="determinate")
        self.candidate_bar.grid(row=7, column=1, sticky="ew", padx=8, pady=(8, 2))
        ttk.Label(outer, textvariable=self.candidate_rms).grid(row=8, column=0, columnspan=3, sticky="w")
        ttk.Label(outer, text="INTERVIEWER").grid(row=9, column=0, sticky="w", pady=(10, 2))
        self.interviewer_bar = ttk.Progressbar(outer, maximum=100, mode="determinate")
        self.interviewer_bar.grid(row=9, column=1, sticky="ew", padx=8, pady=(10, 2))
        ttk.Label(outer, textvariable=self.interviewer_rms).grid(row=10, column=0, columnspan=3, sticky="w")

        controls = ttk.Frame(outer)
        controls.grid(row=11, column=0, columnspan=3, sticky="ew", pady=(16, 0))
        self.refresh_button = ttk.Button(controls, text="Refresh devices", command=self.refresh_devices)
        self.refresh_button.pack(side="left")
        self.save_button = ttk.Button(controls, text="Save selection", command=self.save_selection)
        self.save_button.pack(side="left", padx=(8, 0))
        self.start_button = ttk.Button(controls, text="Start", command=self.start_capture)
        self.start_button.pack(side="right")
        self.stop_button = ttk.Button(controls, text="Stop", command=self.stop_capture, state="disabled")
        self.stop_button.pack(side="right", padx=(0, 8))

        ttk.Label(
            outer,
            text="No audio is captured until Start is confirmed. Stop or closing this window releases both inputs.",
            wraplength=610,
        ).grid(row=12, column=0, columnspan=3, sticky="w", pady=(12, 0))

    @staticmethod
    def _choice_label(device: AudioDevice) -> str:
        return f"[{device.index}] {device.name} ({device.default_sample_rate} Hz)"

    def refresh_devices(self) -> None:
        try:
            self.microphones, self.playbacks, _ = enumerate_wasapi_devices()
        except DeviceDiscoveryError as exc:
            self.message_var.set(str(exc))
            return
        self._mic_by_label = {self._choice_label(device): device for device in self.microphones}
        self._playback_by_label = {self._choice_label(device): device for device in self.playbacks}
        self.mic_combo["values"] = tuple(self._mic_by_label)
        self.playback_combo["values"] = tuple(self._playback_by_label)
        saved_mic = self.mic_var.get()
        saved_playback = self.playback_var.get()
        mic_match = next((label for label, device in self._mic_by_label.items() if str(device.index) == saved_mic), None)
        playback_match = next((label for label, device in self._playback_by_label.items() if str(device.index) == saved_playback), None)
        self.mic_var.set(mic_match or "")
        self.playback_var.set(playback_match or "")
        if not self.microphones or not self.playbacks:
            self.message_var.set("No usable WASAPI microphone or playback devices were found.")
        elif (saved_mic and not mic_match) or (saved_playback and not playback_match):
            self.message_var.set("A saved device is missing or its index changed. Select devices again; nothing was switched automatically.")
        else:
            self.message_var.set("Capture is Off. The built-in microphone and speaker are selected when available.")

    def _build_selection(self) -> dict[str, Any]:
        mic = self._mic_by_label.get(self.mic_var.get())
        playback = self._playback_by_label.get(self.playback_var.get())
        if mic is None or playback is None:
            raise DeviceDiscoveryError("Choose both a microphone and playback device.")
        config = create_device_selection(mic.index, playback.index)
        config["speaker_priority"] = bool(self.speaker_priority_var.get())
        return config

    def _persist_config(self, config: dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    def save_selection(self) -> None:
        if self.session is not None:
            return
        try:
            config = self._build_selection()
            self._persist_config(config)
        except (DeviceDiscoveryError, OSError) as exc:
            messagebox.showerror("Cannot save selection", str(exc), parent=self.root)
            return
        self.message_var.set(f"Selection saved to {self.config_path}. Capture remains Off.")

    def start_capture(self) -> None:
        if self.session is not None:
            return
        try:
            config = self._build_selection()
        except DeviceDiscoveryError as exc:
            messagebox.showerror("Invalid device selection", str(exc), parent=self.root)
            return
        candidate = config["sources"]["candidate"]
        interviewer = config["sources"]["interviewer"]
        gate_text = "Candidate is suppressed during speaker activity." if config["speaker_priority"] else "Candidate is not suppressed."
        save_text = (
            "Both streams will be saved as separate WAV files under private\\recordings."
            if self.save_recordings_var.get()
            else "Audio will not be saved; temporary buffers will be cleared on Stop."
        )
        notice = (
            "Start audio capture?\n\n"
            f"CANDIDATE microphone: {candidate['device_name']}\n"
            f"INTERVIEWER speakers: {interviewer['playback_name']}\n\n"
            f"Speaker priority: {gate_text}\n"
            f"Recording: {save_text}\n"
            "Both input streams will open only after you confirm."
        )
        if not messagebox.askyesno("Confirm capture", notice, parent=self.root):
            return
        try:
            self._persist_config(config)
            recording_dir = None
            if self.save_recordings_var.get():
                capture_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
                recording_dir = self.config_path.parent / "recordings" / capture_id
            session = CaptureSession(self.config_path, recording_dir=recording_dir)
            session.start()
        except (CaptureError, OSError, DeviceDiscoveryError) as exc:
            self.session = None
            self.state_var.set("CAPTURE OFF")
            self.message_var.set(str(exc))
            messagebox.showerror("Capture did not start", str(exc), parent=self.root)
            return
        self.session = session
        self.state_var.set("CAPTURE ON")
        self.message_var.set("Both streams are active. Speaker audio is never suppressed by speaker-priority mode.")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.refresh_button.configure(state="disabled")
        self.save_button.configure(state="disabled")
        self.mic_combo.configure(state="disabled")
        self.playback_combo.configure(state="disabled")
        self.priority_check.configure(state="disabled")
        self.recording_check.configure(state="disabled")

    def _refresh_meters(self) -> None:
        if self.session is not None:
            try:
                self.session.check_health()
                candidate = self.session.metrics("candidate")
                interviewer = self.session.metrics("interviewer")
                self.candidate_bar["value"] = min(100.0, float(candidate["peak"]) * 100.0)
                self.interviewer_bar["value"] = min(100.0, float(interviewer["peak"]) * 100.0)
                self.candidate_rms.set(
                    f"RMS {candidate['rms']:.3f} | peak {candidate['peak']:.3f} | captured {candidate['captured_seconds']:.1f} s | suppressed {candidate['speaker_suppressed_frames']} frames"
                )
                self.interviewer_rms.set(
                    f"RMS {interviewer['rms']:.3f} | peak {interviewer['peak']:.3f} | captured {interviewer['captured_seconds']:.1f} s"
                )
            except CaptureError as exc:
                self.stop_capture()
                self.message_var.set(str(exc))
                messagebox.showerror("Capture stopped", str(exc), parent=self.root)
        self.root.after(200, self._refresh_meters)

    def stop_capture(self) -> None:
        session = self.session
        self.session = None
        if session is not None:
            session.stop(clear=True)
        self.state_var.set("CAPTURE OFF")
        self.candidate_bar["value"] = 0
        self.interviewer_bar["value"] = 0
        self.candidate_rms.set("RMS 0.000 | peak 0.000 | captured 0.0 s")
        self.interviewer_rms.set("RMS 0.000 | peak 0.000 | captured 0.0 s")
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.refresh_button.configure(state="normal")
        self.save_button.configure(state="normal")
        self.mic_combo.configure(state="readonly")
        self.playback_combo.configure(state="readonly")
        self.priority_check.configure(state="normal")
        self.recording_check.configure(state="normal")
        if session is not None and session.fatal_message:
            self.message_var.set(session.fatal_message)
        elif session is not None and session.recording_paths:
            self.message_var.set(
                f"Capture stopped; WAVs saved to {session.recording_dir}. Both inputs are released."
            )
            messagebox.showinfo(
                "Capture saved",
                "Separate WAV files were saved:\n\n"
                f"CANDIDATE: {session.recording_paths['candidate']}\n"
                f"INTERVIEWER: {session.recording_paths['interviewer']}",
                parent=self.root,
            )
        elif session is not None:
            self.message_var.set("Capture stopped. Both inputs are released and temporary audio buffers were cleared.")

    def close(self) -> None:
        self.stop_capture()
        self.root.destroy()


def launch(config_path: str | Path = "private/agent.json") -> None:
    root = tk.Tk()
    CaptureWindow(root, config_path)
    root.mainloop()
