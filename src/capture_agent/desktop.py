"""Windows tray, single instance and per-user sign-in launch. No administrator rights."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import queue
import subprocess
import sys
import threading
from typing import Any, Callable

APP_NAME = "speaker & microphone"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "InterviewPreparationAudio"


def enable_startup(config_path: Path) -> bool:
    """Register this installed environment's pythonw and an absolute config path."""
    try:
        import winreg
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        executable = str(pythonw if pythonw.exists() else Path(sys.executable))
        command = subprocess.list2cmdline([executable, "-m", "capture_agent", "gui", "--config", str(config_path.resolve()), "--tray"])
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, command)
        return True
    except Exception:
        return False


class DesktopInstance:
    """Keep one agent per desktop session; another launch asks it to show its UI."""
    def __init__(self) -> None:
        self.primary = True
        self._handles: list[Any] = []
        self.kernel: Any = None
        self.event: Any = None
        try:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            kernel.CreateMutexW.restype = wintypes.HANDLE
            kernel.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
            kernel.CreateEventW.restype = wintypes.HANDLE
            kernel.SetEvent.argtypes = [wintypes.HANDLE]
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            ctypes.set_last_error(0)
            mutex = kernel.CreateMutexW(None, False, r"Local\InterviewPreparationAudio.Instance")
            if not mutex:
                return
            self.primary = ctypes.get_last_error() != 183
            self.kernel = kernel
            self._handles.append(mutex)
            self.event = kernel.CreateEventW(None, False, False, r"Local\InterviewPreparationAudio.Show")
            if self.event:
                self._handles.append(self.event)
                if not self.primary:
                    kernel.SetEvent(self.event)
        except Exception:
            pass

    def show_requested(self) -> bool:
        return bool(self.kernel and self.event and self.kernel.WaitForSingleObject(self.event, 0) == 0)

    def close(self) -> None:
        if self.kernel:
            for handle in self._handles:
                self.kernel.CloseHandle(handle)
        self._handles.clear()


class TrayController:
    """Tray callbacks use a queue; Tk is accessed exclusively on its main thread."""
    def __init__(self, root: Any, instance: DesktopInstance, on_exit: Callable[[], None], hidden: bool) -> None:
        self.root, self.instance, self.on_exit = root, instance, on_exit
        self.actions: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.available = False
        self.stopped = False
        self.icon: Any = None
        self.hidden = hidden
        threading.Thread(target=self._run, name="windows-tray", daemon=True).start()
        self.root.after(100, self._poll)

    def _run(self) -> None:
        try:
            import pystray
            from PIL import Image, ImageDraw
            image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((2, 2, 62, 62), radius=14, fill="#173f38")
            draw.line((19, 17, 19, 47), fill="#edf3d9", width=6)
            draw.line((32, 47, 32, 17, 43, 17), fill="#edf3d9", width=6)
            draw.arc((32, 17, 50, 35), -90, 90, fill="#edf3d9", width=6)
            draw.ellipse((46, 45, 55, 54), fill="#a8cf7b")
            self.icon = pystray.Icon("interview-preparation", image, APP_NAME, pystray.Menu(
                pystray.MenuItem(f"Open {APP_NAME}", lambda icon, item: self.actions.put("show"), default=True),
                pystray.MenuItem("Exit", lambda icon, item: self.actions.put("exit")),
            ))
            def ready(icon: Any) -> None:
                icon.visible = True
                self.actions.put("ready")
            self.icon.run(setup=ready)
        except Exception:
            pass
        finally:
            if not self.stopped:
                self.actions.put("unavailable")

    def show(self) -> None:
        self.hidden = False
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self) -> None:
        if self.available:
            self.root.withdraw()
        else:
            self.on_exit()

    def _poll(self) -> None:
        if self.stopped:
            return
        if self.instance.show_requested():
            self.show()
        while not self.actions.empty():
            action = self.actions.get()
            if action == "exit":
                self.on_exit()
                return
            if action == "unavailable":
                self.available = False
                self.show()
            elif action == "show":
                self.show()
            elif action == "ready":
                self.available = True
                if self.hidden:
                    self.root.withdraw()
        self.root.after(100, self._poll)

    def stop(self) -> None:
        self.stopped = True
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
