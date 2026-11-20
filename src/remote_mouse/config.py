import ctypes
import logging
import os
import shutil
import socket
import sys
from pathlib import Path

if sys.platform == "win32":
    import winreg

HOST = "0.0.0.0"
PORT = 8000
APP_NAME = "RemoteMouseServer"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

PACKAGE_DIR = Path(__file__).resolve().parent
WEB_INDEX_FILE = PACKAGE_DIR / "web" / "index.html"


def is_bundled() -> bool:
    """Detect whether running as a compiled Nuitka or PyInstaller executable."""
    return getattr(sys, "frozen", False) or "__compiled__" in globals()


def is_packaged_msix() -> bool:
    """Return True when running inside a Windows MSIX / Microsoft Store package."""
    if sys.platform != "win32":
        return False
    try:
        length = ctypes.c_uint32(0)
        rc = ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(length), None)
        return rc in (0, 122)  # 0 = ERROR_SUCCESS, 122 = ERROR_INSUFFICIENT_BUFFER
    except Exception:
        return False


def get_data_dir() -> Path:
    """Store certs, trusted keys, and logs in %LOCALAPPDATA%\\RemoteMouse on Windows."""
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "RemoteMouse"
    return Path.home() / ".remote_mouse"


def _migrate_legacy_data(target_dir: Path) -> None:
    """One-time copy of paired keys/certs from an old side-by-side .remote_mouse folder."""
    if is_packaged_msix():
        return

    candidates: list[Path] = []
    if is_bundled():
        candidates.append(Path(sys.executable).resolve().parent / ".remote_mouse")
    else:
        if sys.argv and sys.argv[0]:
            candidates.append(Path(sys.argv[0]).resolve().parent / ".remote_mouse")
        candidates.append(Path.cwd() / ".remote_mouse")

    seen: set[Path] = set()
    for legacy_dir in candidates:
        try:
            resolved = legacy_dir.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.exists() or resolved == target_dir.resolve():
            continue
        seen.add(resolved)
        target_dir.mkdir(parents=True, exist_ok=True)
        for fname in ("trusted_devices.json", "server.crt", "server.key"):
            src = resolved / fname
            dst = target_dir / fname
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)


DATA_DIR = get_data_dir()
_migrate_legacy_data(DATA_DIR)
DATA_DIR.mkdir(parents=True, exist_ok=True)

CERT_FILE = DATA_DIR / "server.crt"
KEY_FILE = DATA_DIR / "server.key"
TRUSTED_FILE = DATA_DIR / "trusted_devices.json"
LOG_FILE = DATA_DIR / "server.log"

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("remote_mouse")


def get_local_ip() -> str:
    """Detect the PC's current LAN/hotspot IPv4 address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def get_pythonw_path() -> str:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else exe)


def get_startup_command() -> str:
    if is_bundled():
        return f'"{Path(sys.executable).resolve()}"'
    return f'"{get_pythonw_path()}" -m remote_mouse'


def is_startup_enabled() -> bool:
    if sys.platform != "win32" or is_packaged_msix():
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, APP_NAME)
            return val == get_startup_command()
    except OSError:
        return False


def set_windows_startup(enable: bool) -> bool:
    if sys.platform != "win32" or is_packaged_msix():
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enable:
                cmd = get_startup_command()
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
                logger.info("Enabled Windows startup: %s", cmd)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                    logger.info("Disabled Windows startup")
                except FileNotFoundError:
                    pass
        return True
    except OSError as exc:
        logger.error("Failed to update Windows startup key: %s", exc)
        return False