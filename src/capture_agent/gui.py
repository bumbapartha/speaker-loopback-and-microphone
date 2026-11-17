"""Small Tkinter window for explicit device selection and manual W2 checks."""

from __future__ import annotations

import json
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from .devices import AudioDevice, DeviceDiscoveryError, create_device_selection, enumerate_wasapi_devices
from .transport import DEFAULT_RECEIVER_URL, TransportError, WebSocketCaptureSender, validate_receiver_url


class CaptureWindow:
    def __init__(self, root: tk.Tk, config_path: str | Path) -> None:
        self.root = root
        self.config_path = Path(config_path)
        self.receiver_config_path = self.config_path.parent / "receiver.json"
        self.sender: WebSocketCaptureSender | None = None
        self.microphones: list[AudioDevice] = []
        self.playbacks: list[AudioDevice] = []
        self._mic_by_label: dict[str, AudioDevice] = {}
        self._playback_by_label: dict[str, AudioDevice] = {}
        self.root.title("Windows Capture Agent")
        self.root.minsize(820, 500)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.state_var = tk.StringVar(value="CAPTURE OFF")
        self.message_var = tk.StringVar(
            value="Paste the Mac receiver URL and click Connect / Start once. The agent waits for the Mac interview, follows its Windows audio checkbox, and reconnects automatically."
        )
        self.mic_var = tk.StringVar()
        self.playback_var = tk.StringVar()
        self.speaker_priority_var = tk.BooleanVar(value=True)
        self.receiver_url_var = tk.StringVar(value=DEFAULT_RECEIVER_URL)
        self._last_sender_state = "OFF"
        self._load_receiver_config()
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

        ttk.Label(outer, text="Mac receiver URL").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=4)
        self.receiver_entry = ttk.Entry(outer, textvariable=self.receiver_url_var, width=66)
        self.receiver_entry.grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Label(outer, text="CANDIDATE microphone").grid(row=3, column=0, sticky="w", padx=(0, 12), pady=4)
        self.mic_combo = ttk.Combobox(outer, textvariable=self.mic_var, state="readonly", width=66)
        self.mic_combo.grid(row=3, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Label(outer, text="INTERVIEWER playback").grid(row=4, column=0, sticky="w", padx=(0, 12), pady=4)
        self.playback_combo = ttk.Combobox(outer, textvariable=self.playback_var, state="readonly", width=66)
        self.playback_combo.grid(row=4, column=1, columnspan=2, sticky="ew", pady=4)

        self.priority_check = ttk.Checkbutton(
            outer,
            text="Speaker priority: suppress Candidate while speaker audio is active",
            variable=self.speaker_priority_var,
        )
        self.priority_check.grid(row=5, column=0, columnspan=3, sticky="w", pady=(7, 8))

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

        self.transport_stats = tk.StringVar(value="Sent: Candidate 0 · Interviewer 0 | Acknowledged: Candidate 0 · Interviewer 0")
        ttk.Label(outer, textvariable=self.transport_stats).grid(row=11, column=0, columnspan=3, sticky="w", pady=(8, 0))

        controls = ttk.Frame(outer)
        controls.grid(row=12, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        self.refresh_button = ttk.Button(controls, text="Refresh devices", command=self.refresh_devices)
        self.refresh_button.pack(side="left")
        self.save_button = ttk.Button(controls, text="Save selection", command=self.save_selection)
        self.save_button.pack(side="left", padx=(8, 0))
        self.start_button = ttk.Button(controls, text="Connect / Start", command=self.start_capture)
        self.start_button.pack(side="right")
        self.stop_button = ttk.Button(controls, text="Stop", command=self.stop_capture, state="disabled")
        self.stop_button.pack(side="right", padx=(0, 8))

        ttk.Label(
            outer,
            text="While started, Mac readiness is checked every 5 seconds. Capture runs only when the Mac requests it. Stop or closing releases inputs and disables automatic reconnect.",
            wraplength=610,
        ).grid(row=13, column=0, columnspan=3, sticky="w", pady=(10, 0))

    def _load_receiver_config(self) -> None:
        try:
            config = json.loads(self.receiver_config_path.read_text(encoding="utf-8"))
            if config.get("protocol") == 1:
                self.receiver_url_var.set(str(config.get("url", "")))
        except (OSError, ValueError, TypeError):
            pass

    def _save_receiver_config(self, url: str) -> None:
        self.receiver_config_path.parent.mkdir(parents=True, exist_ok=True)
        self.receiver_config_path.write_text(
            json.dumps({"url": url, "protocol": 1}, indent=2) + "\n", encoding="utf-8"
        )

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
        if self.sender is not None and self.sender.snapshot()["state"] != "OFF":
            return
        try:
            config = self._build_selection()
            self._persist_config(config)
        except (DeviceDiscoveryError, OSError) as exc:
            messagebox.showerror("Cannot save selection", str(exc), parent=self.root)
            return
        self.message_var.set(f"Selection saved to {self.config_path}. Capture remains Off.")

    def start_capture(self) -> None:
        if self.sender is not None and self.sender.snapshot()["state"] != "OFF":
            return
        try:
            config = self._build_selection()
        except DeviceDiscoveryError as exc:
            messagebox.showerror("Invalid device selection", str(exc), parent=self.root)
            return
        try:
            receiver_url = validate_receiver_url(self.receiver_url_var.get())
        except TransportError as exc:
            messagebox.showerror("Invalid receiver URL", str(exc), parent=self.root)
            return
        try:
            self._persist_config(config)
            self._save_receiver_config(receiver_url)
            sender = WebSocketCaptureSender(receiver_url, str(self.config_path))
            sender.start()
        except (OSError, DeviceDiscoveryError, TransportError) as exc:
            self.sender = None
            self.state_var.set("CAPTURE OFF")
            self.message_var.set(str(exc))
            messagebox.showerror("Connection did not start", str(exc), parent=self.root)
            return
        self.sender = sender
        self._last_sender_state = "CONNECTING"
        self.transport_stats.set("Sent: Candidate 0 · Interviewer 0 | Acknowledged: Candidate 0 · Interviewer 0")
        self.state_var.set("CONNECTING")
        self.message_var.set("Checking Mac readiness. Audio devices remain Off until Windows audio is selected.")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.refresh_button.configure(state="disabled")
        self.save_button.configure(state="disabled")
        self.mic_combo.configure(state="disabled")
        self.playback_combo.configure(state="disabled")
        self.priority_check.configure(state="disabled")
        self.receiver_entry.configure(state="disabled")

    def _refresh_meters(self) -> None:
        if self.sender is not None:
            snapshot = self.sender.snapshot()
            state = snapshot["state"]
            self.state_var.set("CAPTURE ON" if state == "ON" else state)
            self.message_var.set(snapshot["detail"] or "")
            candidate = snapshot["metrics"].get("candidate")
            interviewer = snapshot["metrics"].get("interviewer")
            if candidate:
                self.candidate_bar["value"] = min(100.0, float(candidate["peak"]) * 100.0)
                self.candidate_rms.set(
                    f"RMS {candidate['rms']:.3f} | peak {candidate['peak']:.3f} | captured {candidate['captured_seconds']:.1f} s | suppressed {candidate['speaker_suppressed_frames']} frames"
                )
            else:
                self.candidate_bar["value"] = 0
                self.candidate_rms.set("RMS 0.000 | peak 0.000 | captured 0.0 s")
            if interviewer:
                self.interviewer_bar["value"] = min(100.0, float(interviewer["peak"]) * 100.0)
                self.interviewer_rms.set(
                    f"RMS {interviewer['rms']:.3f} | peak {interviewer['peak']:.3f} | captured {interviewer['captured_seconds']:.1f} s"
                )
            else:
                self.interviewer_bar["value"] = 0
                self.interviewer_rms.set("RMS 0.000 | peak 0.000 | captured 0.0 s")
            sent, acked = snapshot["sent"], snapshot["acked"]
            self.transport_stats.set(
                f"Sent: Candidate {sent['candidate']} · Interviewer {sent['interviewer']} | "
                f"Acknowledged: Candidate {acked['candidate']} · Interviewer {acked['interviewer']}"
            )
            if state == "OFF" and self._last_sender_state != "OFF":
                self._reset_connection_controls()
                if snapshot["error"]:
                    counts = (
                        f"Last counts — sent: Candidate {sent['candidate']}, Interviewer {sent['interviewer']}; "
                        f"acknowledged: Candidate {acked['candidate']}, Interviewer {acked['interviewer']}; "
                        f"still queued: Candidate {snapshot['queued']['candidate']}, "
                        f"Interviewer {snapshot['queued']['interviewer']}."
                    )
                    messagebox.showerror(
                        "Capture / connection stopped",
                        f"{snapshot['error']}\n\n{counts}",
                        parent=self.root,
                    )
            self._last_sender_state = state
        self.root.after(200, self._refresh_meters)

    def stop_capture(self) -> None:
        if self.sender is not None and self.sender.snapshot()["state"] != "OFF":
            self.sender.stop()
            self.state_var.set("STOPPING")
            self.message_var.set("Stopping capture and automatic reconnect…")
            self.stop_button.configure(state="disabled")
            return
        self._reset_connection_controls()

    def _reset_connection_controls(self) -> None:
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
        self.receiver_entry.configure(state="normal")

    def close(self) -> None:
        if self.sender is not None and self.sender.snapshot()["state"] != "OFF":
            self.sender.stop()
            if self.sender._thread is not None:
                self.sender._thread.join(timeout=7.0)
        self.root.destroy()


def launch(config_path: str | Path = "private/agent.json") -> None:
    root = tk.Tk()
    CaptureWindow(root, config_path)
    root.mainloop()
