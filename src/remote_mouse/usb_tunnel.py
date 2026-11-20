import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from remote_mouse.config import PORT, logger


def find_adb() -> str | None:
    found = shutil.which("adb")
    if found:
        return found
    sdk_adb = Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk" / "platform-tools" / "adb.exe"
    return str(sdk_adb) if sdk_adb.exists() else None


def usb_adb_watcher(stop_event: threading.Event) -> None:
    """Automatically map phone localhost:8000 -> PC localhost:8000 over USB."""
    adb_path = find_adb()
    if not adb_path:
        logger.info("ADB not found in PATH or Android SDK; USB reverse tunnel disabled.")
        return

    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    forwarded_devices: set[str] = set()

    while not stop_event.is_set():
        try:
            out = subprocess.check_output(
                [adb_path, "devices"],
                text=True,
                timeout=3,
                creationflags=flags,
                stderr=subprocess.DEVNULL,
            )
            current = {
                line.split()[0]
                for line in out.splitlines()[1:]
                if line.strip() and "\tdevice" in line
            }
            for serial in current - forwarded_devices:
                res = subprocess.run(
                    [adb_path, "-s", serial, "reverse", f"tcp:{PORT}", f"tcp:{PORT}"],
                    timeout=3,
                    creationflags=flags,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if res.returncode == 0:
                    logger.info("Enabled USB reverse tunnel for device %s", serial)
            forwarded_devices = current
        except Exception:
            forwarded_devices.clear()
        stop_event.wait(2.5)