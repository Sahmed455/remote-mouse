import argparse
import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pystray
import qrcode
import uvicorn
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from PIL import Image, ImageDraw, ImageTk
from pynput.keyboard import Controller as KeyboardController
from pynput.keyboard import Key
from pynput.mouse import Button
from pynput.mouse import Controller as MouseController

if sys.platform == "win32":
    import winreg

HOST = "0.0.0.0"
PORT = 8000
APP_NAME = "RemoteMouseServer"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def is_bundled() -> bool:
    """Detect whether running as a compiled Nuitka or PyInstaller executable."""
    return getattr(sys, "frozen", False) or "__compiled__" in globals()


def get_app_dir() -> Path:
    """Resolve persistent storage beside the EXE (bundled) or script (source)."""
    if is_bundled():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


DATA_DIR = get_app_dir() / ".remote_mouse"
CERT_FILE = DATA_DIR / "server.crt"
KEY_FILE = DATA_DIR / "server.key"
TRUSTED_FILE = DATA_DIR / "trusted_devices.json"
LOG_FILE = DATA_DIR / "server.log"

DATA_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("remote_mouse")

PAIRING_PIN = f"{secrets.randbelow(1_000_000):06d}"
FAILED_PIN_ATTEMPTS = 0
PIN_LOCKOUT_UNTIL = 0.0
TRUSTED_LOCK = threading.Lock()
ACTIVE_TRAY_APP = None

mouse = MouseController()
keyboard = KeyboardController()

SPECIAL_KEYS = {
    "backspace": Key.backspace,
    "enter": Key.enter,
    "tab": Key.tab,
    "esc": Key.esc,
    "space": Key.space,
    "up": Key.up,
    "down": Key.down,
    "left": Key.left,
    "right": Key.right,
    "delete": Key.delete,
    "home": Key.home,
    "end": Key.end,
    "page_up": Key.page_up,
    "page_down": Key.page_down,
    "win": Key.cmd,
    "insert": getattr(Key, "insert", None),
    "print_screen": getattr(Key, "print_screen", None),
    **{f"f{i}": getattr(Key, f"f{i}", None) for i in range(1, 13)},
}

MOD_KEYS = {
    "ctrl": Key.ctrl,
    "alt": Key.alt,
    "shift": Key.shift,
    "win": Key.cmd,
}

MEDIA_KEYS = {
    "play_pause": getattr(Key, "media_play_pause", None),
    "next": getattr(Key, "media_next", None),
    "prev": getattr(Key, "media_previous", None),
    "vol_up": getattr(Key, "media_volume_up", None),
    "vol_down": getattr(Key, "media_volume_down", None),
    "mute": getattr(Key, "media_volume_mute", None),
}


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


# --- Windows Startup Registry Helpers (Nuitka, PyInstaller & Script Aware) ---

def get_pythonw_path() -> str:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else exe)


def get_startup_command() -> str:
    if is_bundled():
        return f'"{Path(sys.executable).resolve()}"'
    return f'"{get_pythonw_path()}" "{Path(__file__).resolve()}"'


def is_startup_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, APP_NAME)
            return val == get_startup_command()
    except OSError:
        return False


def set_windows_startup(enable: bool) -> bool:
    if sys.platform != "win32":
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


# --- Automatic ADB USB Reverse Tunnel Watcher ---

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


# --- Local Trusted Devices Store ---

def load_trusted_devices() -> dict:
    if not TRUSTED_FILE.exists():
        return {}
    try:
        return json.loads(TRUSTED_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_trusted_devices(devices: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_file = TRUSTED_FILE.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(devices, indent=2), encoding="utf-8")
    tmp_file.replace(TRUSTED_FILE)


# --- Self-Signed TLS Certificate (Required for WSS + Browser Web Crypto) ---

def ensure_tls_cert(local_ip: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    if CERT_FILE.exists() and KEY_FILE.exists():
        try:
            existing = x509.load_pem_x509_certificate(CERT_FILE.read_bytes())
            san = existing.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            san_ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
            expiry = getattr(existing, "not_valid_after_utc", existing.not_valid_after.replace(tzinfo=timezone.utc))
            if (local_ip == "127.0.0.1" or local_ip in san_ips) and expiry > now + timedelta(days=7):
                return
        except Exception:
            pass

    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"RemoteMouse ({local_ip})")])
    san_entries = [
        x509.DNSName("localhost"),
        x509.DNSName(f"{socket.gethostname().lower()}.local"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    if local_ip != "127.0.0.1":
        san_entries.append(x509.IPAddress(ipaddress.ip_address(local_ip)))

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .sign(private_key, hashes.SHA256())
    )

    KEY_FILE.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    logger.info("Generated self-signed ECDSA TLS certificate for %s", local_ip)


# --- ECDSA P-256 Verification (Web Crypto IEEE P1363 -> ASN.1 DER) ---

def verify_p256_signature(public_key_b64: str, nonce_bytes: bytes, signature_b64: str) -> tuple[bool, str]:
    try:
        pub_der = base64.b64decode(public_key_b64, validate=True)
        raw_sig = base64.b64decode(signature_b64, validate=True)
        if len(raw_sig) != 64:
            return False, ""

        pub_key = serialization.load_der_public_key(pub_der)
        if not isinstance(pub_key, ec.EllipticCurvePublicKey) or not isinstance(pub_key.curve, ec.SECP256R1):
            return False, ""

        r = int.from_bytes(raw_sig[:32], "big")
        s = int.from_bytes(raw_sig[32:], "big")
        der_sig = encode_dss_signature(r, s)

        pub_key.verify(der_sig, nonce_bytes, ec.ECDSA(hashes.SHA256()))
        fingerprint = hashlib.sha256(pub_der).hexdigest()[:16]
        return True, fingerprint
    except (InvalidSignature, ValueError, TypeError):
        return False, ""


def rotate_pairing_pin() -> None:
    global PAIRING_PIN
    PAIRING_PIN = f"{secrets.randbelow(1_000_000):06d}"


app = FastAPI()

HTML_PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <title>Remote Mouse</title>
  <style>
    * { box-sizing: border-box; user-select: none; -webkit-user-select: none; -webkit-tap-highlight-color: transparent; }
    body {
      margin: 0; background: #0f1115; color: #f0f2f5;
      font-family: system-ui, -apple-system, sans-serif;
      display: flex; flex-direction: column; height: 100dvh; overflow: hidden;
    }

    .topbar {
      display: flex; align-items: center; justify-content: space-between;
      gap: 6px; padding: 8px 10px 4px;
    }
    .status { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #9aa3b2; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: #e05252; flex-shrink: 0; }
    .dot.ok { background: #34c759; }
    .media-bar { display: flex; gap: 4px; overflow-x: auto; flex-shrink: 0; }
    .icon-btn {
      background: #1d2129; color: #e2e6ee; border: 1px solid #2b313d;
      border-radius: 8px; padding: 7px 8px; font-size: 12px; font-weight: 600; line-height: 1;
    }
    .icon-btn.active { background: #2563eb; border-color: #3b82f6; color: #fff; }
    .icon-btn:active { background: #323948; }

    #settings-panel {
      display: none; margin: 4px 10px; padding: 12px 14px;
      background: #181c23; border: 1px solid #2b313d; border-radius: 14px;
      flex-direction: column; gap: 10px; font-size: 13px;
    }
    #settings-panel.open { display: flex; }
    .setting-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    .setting-row label { color: #cbd5e1; white-space: nowrap; }
    .slider-wrap { display: flex; align-items: center; gap: 8px; flex: 1; justify-content: flex-end; }
    .slider-wrap input[type="range"] { flex: 1; max-width: 170px; accent-color: #3b82f6; }
    .val-pill {
      min-width: 42px; text-align: center; font-variant-numeric: tabular-nums;
      background: #0f1115; border: 1px solid #2b313d; border-radius: 6px; padding: 3px 6px; font-size: 12px;
    }
    .step-btn {
      width: 26px; height: 26px; border-radius: 6px; border: 1px solid #2b313d;
      background: #232833; color: #fff; font-size: 15px; line-height: 1;
    }
    .toggle-row { display: flex; gap: 14px; flex-wrap: wrap; padding-top: 2px; }
    .toggle-label { display: flex; align-items: center; gap: 6px; color: #cbd5e1; font-size: 12px; }
    .key-info {
      display: flex; align-items: center; justify-content: space-between; gap: 8px;
      padding-top: 8px; border-top: 1px solid #252b36; font-size: 11px; color: #8c96a8;
    }
    .danger-btn {
      background: #2c1b1e; color: #fca5a5; border: 1px solid #5c282e;
      border-radius: 6px; padding: 4px 8px; font-size: 11px; font-weight: 600;
    }

    #pad {
      flex: 1; min-height: 120px; margin: 6px 10px; border-radius: 16px;
      background: #181c23; border: 1.5px solid #2b313d;
      touch-action: none; position: relative;
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      text-align: center; color: #566073; font-size: 13px; line-height: 1.5;
      transition: border-color 0.12s ease, background-color 0.12s ease;
    }
    #pad.dragging { border-color: #3b82f6; background: #172133; }
    #drag-badge {
      position: absolute; top: 10px; right: 12px;
      font-size: 11px; font-weight: 600; color: #60a5fa;
      background: rgba(59, 130, 246, 0.15); padding: 3px 8px; border-radius: 999px;
      opacity: 0; transition: opacity 0.12s ease;
    }
    #pad.dragging #drag-badge { opacity: 1; }
    #pad * { pointer-events: none; }

    #extra-keys {
      display: none; margin: 0 10px 6px; padding: 8px;
      background: #151921; border: 1px solid #2b313d; border-radius: 14px;
      flex-direction: column; gap: 8px;
    }
    #extra-keys.open { display: flex; }
    .fn-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 5px; }
    .nav-layout { display: grid; grid-template-columns: 1.15fr 1fr; gap: 8px; align-items: center; }
    .nav-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 5px; }
    .dpad { display: grid; grid-template-columns: repeat(3, 1fr); gap: 5px; }
    .dpad-spacer { visibility: hidden; }
    .key-btn {
      padding: 9px 4px; border-radius: 8px;
      background: #1f242e; color: #e2e6ee; border: 1px solid #2e3543;
      font-size: 12px; font-weight: 600; touch-action: none;
    }
    .key-btn:active, .key-btn.pressed { background: #2563eb; border-color: #60a5fa; color: #fff; }
    .arrow-btn { font-size: 15px; padding: 10px 4px; background: #242b38; }

    .mod-bar { display: flex; gap: 5px; padding: 2px 10px 6px; overflow-x: auto; }
    .mod-btn {
      flex: 1; min-width: 42px; padding: 9px 5px; border-radius: 10px;
      background: #1d2129; color: #d1d6e0; border: 1px solid #2b313d;
      font-size: 12px; font-weight: 600;
    }
    .mod-btn.active { background: #2563eb; border-color: #3b82f6; color: #fff; }
    .mod-btn:active { background: #323948; }
    .kb-btn { background: #263248; border-color: #364b70; color: #93c5fd; font-size: 15px; }

    .click-bar { display: flex; gap: 8px; padding: 0 10px 10px; }
    .click-btn {
      flex: 1; padding: 14px; border-radius: 12px;
      background: #232833; border: 1px solid #303746;
      color: #f0f2f5; font-size: 14px; font-weight: 600;
      touch-action: none;
    }
    .click-btn:active, .click-btn.held { background: #3b82f6; border-color: #60a5fa; }

    #kb-input {
      position: fixed; left: -9999px; top: 0; opacity: 0;
      width: 1px; height: 1px; font-size: 16px;
    }

    #pin-overlay {
      position: fixed; inset: 0; background: rgba(10, 12, 16, 0.96);
      display: none; align-items: center; justify-content: center; z-index: 50;
    }
    #pin-overlay.visible { display: flex; }
    .pin-card {
      width: 300px; background: #181c23; border: 1px solid #2b313d;
      border-radius: 16px; padding: 22px; text-align: center;
    }
    .pin-card h3 { margin: 0 0 6px; font-size: 18px; }
    .pin-card p { margin: 0 0 14px; font-size: 13px; color: #9aa3b2; line-height: 1.4; }
    #pin-field {
      width: 100%; padding: 12px; font-size: 22px; letter-spacing: 6px;
      text-align: center; border-radius: 10px; border: 1px solid #353d4d;
      background: #0f1115; color: #fff; outline: none; margin-bottom: 12px;
    }
    #pin-submit {
      width: 100%; padding: 12px; border: none; border-radius: 10px;
      background: #2563eb; color: #fff; font-size: 15px; font-weight: 600;
    }
    #pin-error { color: #f87171; font-size: 12px; min-height: 18px; margin-top: 8px; }
    .fp-tag { font-family: monospace; font-size: 11px; color: #64748b; margin-top: 8px; }
  </style>
</head>
<body>
  <div class="topbar">
    <div class="status">
      <span id="status-dot" class="dot"></span>
      <span id="status-text">Initializing key...</span>
    </div>
    <div class="media-bar">
      <button class="icon-btn" data-media="mute" title="Mute">🔇</button>
      <button class="icon-btn" data-media="vol_down" title="Volume Down">🔉</button>
      <button class="icon-btn" data-media="vol_up" title="Volume Up">🔊</button>
      <button class="icon-btn" data-media="prev" title="Previous">⏮</button>
      <button class="icon-btn" data-media="play_pause" title="Play/Pause">⏯</button>
      <button class="icon-btn" data-media="next" title="Next">⏭</button>
      <button class="icon-btn" id="qr-popup-btn" title="Open QR on PC">QR</button>
      <button class="icon-btn" id="settings-toggle" title="Mouse Settings">⚙</button>
    </div>
  </div>

  <div id="settings-panel">
    <div class="setting-row">
      <label for="sens-slider">Mouse Speed</label>
      <div class="slider-wrap">
        <button class="step-btn" id="sens-minus">−</button>
        <input id="sens-slider" type="range" min="0.5" max="5.0" step="0.1" />
        <button class="step-btn" id="sens-plus">+</button>
        <span class="val-pill" id="sens-val">1.9x</span>
      </div>
    </div>
    <div class="setting-row">
      <label for="scroll-slider">Scroll Speed</label>
      <div class="slider-wrap">
        <input id="scroll-slider" type="range" min="0.4" max="3.0" step="0.1" />
        <span class="val-pill" id="scroll-val">1.0x</span>
      </div>
    </div>
    <div class="toggle-row">
      <label class="toggle-label"><input type="checkbox" id="accel-toggle" /> Pointer Acceleration</label>
      <label class="toggle-label"><input type="checkbox" id="natural-toggle" /> Natural Scroll</label>
    </div>
    <div class="key-info">
      <span id="device-id-label">Key ID: —</span>
      <button class="danger-btn" id="reset-key-btn">Regenerate Key</button>
    </div>
  </div>

  <div id="pad">
    <span id="drag-badge">DRAGGING</span>
    <div>1-finger: Move · Tap: Click · 2x Tap &amp; Hold: Drag</div>
    <div>2-finger: Scroll · 2-finger Tap: Right Click</div>
  </div>

  <div id="extra-keys">
    <div class="fn-grid">
      <button class="key-btn" data-key="f1">F1</button>
      <button class="key-btn" data-key="f2">F2</button>
      <button class="key-btn" data-key="f3">F3</button>
      <button class="key-btn" data-key="f4">F4</button>
      <button class="key-btn" data-key="f5">F5</button>
      <button class="key-btn" data-key="f6">F6</button>
      <button class="key-btn" data-key="f7">F7</button>
      <button class="key-btn" data-key="f8">F8</button>
      <button class="key-btn" data-key="f9">F9</button>
      <button class="key-btn" data-key="f10">F10</button>
      <button class="key-btn" data-key="f11">F11</button>
      <button class="key-btn" data-key="f12">F12</button>
    </div>
    <div class="nav-layout">
      <div class="nav-grid">
        <button class="key-btn" data-key="home">Home</button>
        <button class="key-btn" data-key="page_up" data-repeat="1">PgUp</button>
        <button class="key-btn" data-key="delete" data-repeat="1">Del</button>
        <button class="key-btn" data-key="end">End</button>
        <button class="key-btn" data-key="page_down" data-repeat="1">PgDn</button>
        <button class="key-btn" data-key="print_screen">PrtSc</button>
        <button class="key-btn" data-key="win">⊞ Win</button>
        <button class="key-btn" data-key="insert">Ins</button>
        <button class="key-btn" data-key="backspace" data-repeat="1">⌫</button>
      </div>
      <div class="dpad">
        <div class="dpad-spacer"></div>
        <button class="key-btn arrow-btn" data-key="up" data-repeat="1">▲</button>
        <div class="dpad-spacer"></div>
        <button class="key-btn arrow-btn" data-key="left" data-repeat="1">◀</button>
        <button class="key-btn arrow-btn" data-key="down" data-repeat="1">▼</button>
        <button class="key-btn arrow-btn" data-key="right" data-repeat="1">▶</button>
      </div>
    </div>
  </div>

  <div class="mod-bar">
    <button class="mod-btn" data-mod="ctrl">Ctrl</button>
    <button class="mod-btn" data-mod="alt">Alt</button>
    <button class="mod-btn" data-mod="shift">Shift</button>
    <button class="mod-btn" data-mod="win">Win</button>
    <button class="mod-btn" data-key="tab">Tab</button>
    <button class="mod-btn" data-key="esc">Esc</button>
    <button class="mod-btn" id="fn-toggle">Fn ✛</button>
    <button class="mod-btn kb-btn" id="kb-toggle">⌨</button>
  </div>

  <div class="click-bar">
    <button class="click-btn" id="left-btn">Left Click (Hold to Drag)</button>
    <button class="click-btn" id="right-btn">Right Click</button>
  </div>

  <input id="kb-input" type="text" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" value=" " />

  <div id="pin-overlay">
    <div class="pin-card">
      <h3>Pair This Device</h3>
      <p>Enter the one-time 6-digit PIN from your PC tray popup. After pairing, this origin reconnects automatically using its ECDSA key.</p>
      <input id="pin-field" type="tel" maxlength="6" inputmode="numeric" placeholder="••••••" />
      <button id="pin-submit">Pair &amp; Trust Device</button>
      <div id="pin-error"></div>
      <div class="fp-tag" id="pin-fp-tag"></div>
    </div>
  </div>

  <script>
    const statusDot = document.getElementById("status-dot");
    const statusText = document.getElementById("status-text");
    const pinOverlay = document.getElementById("pin-overlay");
    const pinField = document.getElementById("pin-field");
    const pinError = document.getElementById("pin-error");
    const pinFpTag = document.getElementById("pin-fp-tag");
    const deviceIdLabel = document.getElementById("device-id-label");
    const pad = document.getElementById("pad");
    const kb = document.getElementById("kb-input");
    const leftBtn = document.getElementById("left-btn");
    const rightBtn = document.getElementById("right-btn");

    function bytesToBase64(bytes) {
      let binary = "";
      for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
      return btoa(binary);
    }

    function base64ToBytes(b64) {
      const binary = atob(b64);
      const out = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
      return out;
    }

    function bytesToHex(bytes) {
      return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    }

    function getDeviceLabel() {
      const ua = navigator.userAgent;
      const mode = location.hostname === "localhost" ? "USB" : "Wi-Fi";
      if (/Android/i.test(ua)) return `Android (${mode})`;
      if (/iPhone/i.test(ua)) return `iPhone (${mode})`;
      if (/iPad/i.test(ua)) return `iPad (${mode})`;
      return `Browser (${mode})`;
    }

    const IDB_NAME = "remote_mouse_crypto_v1";
    const IDB_STORE = "identity";

    function openKeyDb() {
      return new Promise((resolve, reject) => {
        const req = indexedDB.open(IDB_NAME, 1);
        req.onupgradeneeded = () => req.result.createObjectStore(IDB_STORE, { keyPath: "id" });
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      });
    }

    async function loadStoredIdentity() {
      const db = await openKeyDb();
      return new Promise((resolve, reject) => {
        const tx = db.transaction(IDB_STORE, "readonly");
        const req = tx.objectStore(IDB_STORE).get("primary");
        req.onsuccess = () => resolve(req.result || null);
        req.onerror = () => reject(req.error);
      });
    }

    async function saveStoredIdentity(record) {
      const db = await openKeyDb();
      return new Promise((resolve, reject) => {
        const tx = db.transaction(IDB_STORE, "readwrite");
        tx.objectStore(IDB_STORE).put(record);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      });
    }

    async function clearStoredIdentity() {
      const db = await openKeyDb();
      return new Promise((resolve, reject) => {
        const tx = db.transaction(IDB_STORE, "readwrite");
        tx.objectStore(IDB_STORE).delete("primary");
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      });
    }

    async function getOrCreateIdentity() {
      if (!window.crypto || !window.crypto.subtle) {
        throw new Error("HTTPS required for Web Crypto API.");
      }
      const existing = await loadStoredIdentity();
      if (existing && existing.privateKey && existing.publicKeyB64 && existing.deviceId) {
        return existing;
      }

      const keyPair = await crypto.subtle.generateKey(
        { name: "ECDSA", namedCurve: "P-256" },
        false,
        ["sign", "verify"]
      );
      const spkiBuf = await crypto.subtle.exportKey("spki", keyPair.publicKey);
      const spkiBytes = new Uint8Array(spkiBuf);
      const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", spkiBytes));

      const record = {
        id: "primary",
        deviceId: bytesToHex(digest).slice(0, 16),
        publicKeyB64: bytesToBase64(spkiBytes),
        privateKey: keyPair.privateKey,
      };
      await saveStoredIdentity(record);
      return record;
    }

    async function signChallenge(privateKey, nonceB64) {
      const nonceBytes = base64ToBytes(nonceB64);
      const sigBuf = await crypto.subtle.sign(
        { name: "ECDSA", hash: { name: "SHA-256" } },
        privateKey,
        nonceBytes
      );
      return bytesToBase64(new Uint8Array(sigBuf));
    }

    const defaultSettings = { sensitivity: 1.9, scrollSpeed: 1.0, accel: true, naturalScroll: true };
    let settings = { ...defaultSettings };
    try {
      const saved = JSON.parse(localStorage.getItem("rm_settings") || "{}");
      settings = { ...defaultSettings, ...saved };
    } catch {}

    function saveSettings() {
      try { localStorage.setItem("rm_settings", JSON.stringify(settings)); } catch {}
    }

    const settingsPanel = document.getElementById("settings-panel");
    const settingsToggle = document.getElementById("settings-toggle");
    const sensSlider = document.getElementById("sens-slider");
    const sensVal = document.getElementById("sens-val");
    const scrollSlider = document.getElementById("scroll-slider");
    const scrollVal = document.getElementById("scroll-val");
    const accelToggle = document.getElementById("accel-toggle");
    const naturalToggle = document.getElementById("natural-toggle");

    function renderSettings() {
      sensSlider.value = settings.sensitivity.toFixed(1);
      sensVal.textContent = `${settings.sensitivity.toFixed(1)}x`;
      scrollSlider.value = settings.scrollSpeed.toFixed(1);
      scrollVal.textContent = `${settings.scrollSpeed.toFixed(1)}x`;
      accelToggle.checked = settings.accel;
      naturalToggle.checked = settings.naturalScroll;
    }
    renderSettings();

    function updateSensitivity(val) {
      settings.sensitivity = Math.min(5.0, Math.max(0.5, Math.round(val * 10) / 10));
      renderSettings();
      saveSettings();
    }

    settingsToggle.addEventListener("click", () => {
      const isOpen = settingsPanel.classList.toggle("open");
      settingsToggle.classList.toggle("active", isOpen);
    });
    sensSlider.addEventListener("input", () => updateSensitivity(parseFloat(sensSlider.value)));
    document.getElementById("sens-minus").addEventListener("click", () => updateSensitivity(settings.sensitivity - 0.2));
    document.getElementById("sens-plus").addEventListener("click", () => updateSensitivity(settings.sensitivity + 0.2));
    scrollSlider.addEventListener("input", () => {
      settings.scrollSpeed = parseFloat(scrollSlider.value);
      renderSettings();
      saveSettings();
    });
    accelToggle.addEventListener("change", () => { settings.accel = accelToggle.checked; saveSettings(); });
    naturalToggle.addEventListener("change", () => { settings.naturalScroll = naturalToggle.checked; saveSettings(); });

    document.getElementById("reset-key-btn").addEventListener("click", async () => {
      if (!confirm("Generate a brand-new cryptographic key? You will need to pair again with a PIN.")) return;
      await clearStoredIdentity();
      location.reload();
    });

    const extraKeysPanel = document.getElementById("extra-keys");
    const fnToggle = document.getElementById("fn-toggle");
    fnToggle.addEventListener("click", () => {
      const isOpen = extraKeysPanel.classList.toggle("open");
      fnToggle.classList.toggle("active", isOpen);
    });

    const params = new URLSearchParams(location.search);
    let pendingPairingPin = params.get("pin") || "";
    if (pendingPairingPin) {
      history.replaceState({}, "", "/");
    }

    let identity = null;
    let ws = null;
    let authenticated = false;
    let reconnectTimer = null;

    function showPinPrompt(msg = "") {
      pinError.textContent = msg;
      pinOverlay.classList.add("visible");
      pinField.value = "";
      pinField.focus();
    }

    function connect() {
      if (!identity) return;
      clearTimeout(reconnectTimer);
      authenticated = false;
      statusDot.classList.remove("ok");
      statusText.textContent = "Verifying key...";

      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws`);

      ws.onmessage = async (event) => {
        let msg;
        try { msg = JSON.parse(event.data); } catch { return; }

        if (msg.type === "auth_challenge") {
          try {
            const signature = await signChallenge(identity.privateKey, msg.nonce);
            const payload = {
              type: "auth_response",
              device_id: identity.deviceId,
              device_name: getDeviceLabel(),
              signature,
            };
            if (pendingPairingPin) {
              payload.pairing_pin = pendingPairingPin;
              payload.public_key = identity.publicKeyB64;
            }
            ws.send(JSON.stringify(payload));
          } catch (err) {
            statusText.textContent = "Signing error";
          }
        } else if (msg.type === "auth_ok") {
          authenticated = true;
          pendingPairingPin = "";
          pinOverlay.classList.remove("visible");
          statusDot.classList.add("ok");
          const tag = location.hostname === "localhost" ? "USB" : "Wi-Fi";
          statusText.textContent = `🔒 ${tag} · ${identity.deviceId.slice(0, 6)}`;
        }
      };

      ws.onclose = (e) => {
        authenticated = false;
        statusDot.classList.remove("ok");
        if (e.code === 4001) {
          pendingPairingPin = "";
          statusText.textContent = "Pairing required";
          showPinPrompt(e.reason || "Enter the 6-digit Pairing PIN shown on your PC.");
        } else if (e.code === 4002) {
          pendingPairingPin = "";
          statusText.textContent = "Locked out";
          showPinPrompt(e.reason || "Too many wrong attempts. Wait 30s and check PC.");
        } else if (e.code === 4003) {
          statusText.textContent = "Signature invalid";
          showPinPrompt("Key signature verification failed.");
        } else {
          statusText.textContent = "Reconnecting...";
          reconnectTimer = setTimeout(connect, 1500);
        }
      };
    }

    const send = (data) => {
      if (ws && ws.readyState === WebSocket.OPEN && authenticated) {
        ws.send(JSON.stringify(data));
      }
    };

    function submitPairingPin() {
      const val = pinField.value.trim();
      if (!val) return;
      pendingPairingPin = val;
      pinError.textContent = "";
      connect();
    }

    document.getElementById("pin-submit").onclick = submitPairingPin;
    pinField.addEventListener("keydown", (e) => {
      if (e.key === "Enter") submitPairingPin();
    });

    (async () => {
      try {
        identity = await getOrCreateIdentity();
        deviceIdLabel.textContent = `Key ID: ${identity.deviceId}`;
        pinFpTag.textContent = `Device Key Fingerprint: ${identity.deviceId}`;
        connect();
      } catch (err) {
        statusText.textContent = "HTTPS Required";
        showPinPrompt(err.message);
      }
    })();

    let pendingDx = 0, pendingDy = 0, pendingScroll = 0;
    function flushMotion() {
      const ix = Math.trunc(pendingDx);
      const iy = Math.trunc(pendingDy);
      if (ix !== 0 || iy !== 0) {
        send({ type: "move", dx: ix, dy: iy });
        pendingDx -= ix;
        pendingDy -= iy;
      }
      if (Math.abs(pendingScroll) >= 0.05) {
        send({ type: "scroll", dy: pendingScroll });
        pendingScroll = 0;
      }
      requestAnimationFrame(flushMotion);
    }
    requestAnimationFrame(flushMotion);

    const DOUBLE_TAP_MS = 260;
    const TAP_MAX_MS = 220;
    const DEADZONE_PX = 6;

    let startX = 0, startY = 0, lastX = 0, lastY = 0;
    let startTime = 0, lastTapTime = 0;
    let moved = false, maxFingers = 0, isDragging = false, leftHeld = false;
    let activeTouchId = null;

    function getTouchById(touchList, id) {
      for (let i = 0; i < touchList.length; i++) {
        if (touchList[i].identifier === id) return touchList[i];
      }
      return null;
    }

    pad.addEventListener("touchstart", (e) => {
      const touches = e.targetTouches;
      maxFingers = Math.max(maxFingers, touches.length);

      if (touches.length === 1) {
        const t = touches[0];
        activeTouchId = t.identifier;
        startX = lastX = t.clientX;
        startY = lastY = t.clientY;
        startTime = Date.now();
        moved = false;

        if (!leftHeld && Date.now() - lastTapTime < DOUBLE_TAP_MS) {
          isDragging = true;
          lastTapTime = 0;
          pad.classList.add("dragging");
          send({ type: "mouse_down", button: "left" });
        }
      }
    }, { passive: true });

    pad.addEventListener("touchmove", (e) => {
      e.preventDefault();
      const touches = e.targetTouches;
      if (touches.length === 0) return;

      let t = getTouchById(touches, activeTouchId);
      if (!t) {
        t = touches[0];
        activeTouchId = t.identifier;
        lastX = t.clientX;
        lastY = t.clientY;
        return;
      }

      const dx = t.clientX - lastX;
      const dy = t.clientY - lastY;

      if (Math.hypot(t.clientX - startX, t.clientY - startY) > DEADZONE_PX) {
        moved = true;
      }

      if (touches.length === 1) {
        let factor = settings.sensitivity;
        if (settings.accel) {
          const speed = Math.hypot(dx, dy);
          factor *= 1 + Math.min(speed / 18, 2.0);
        }
        pendingDx += dx * factor;
        pendingDy += dy * factor;
      } else if (touches.length === 2) {
        const dir = settings.naturalScroll ? 1 : -1;
        pendingScroll += (dy / 18) * settings.scrollSpeed * dir;
      }

      lastX = t.clientX;
      lastY = t.clientY;
    }, { passive: false });

    function finishTouch(e) {
      const touches = e.targetTouches;
      if (touches.length > 0) {
        const remaining = touches[0];
        activeTouchId = remaining.identifier;
        lastX = remaining.clientX;
        lastY = remaining.clientY;
        return;
      }

      activeTouchId = null;

      if (isDragging) {
        isDragging = false;
        if (!leftHeld) pad.classList.remove("dragging");
        send({ type: "mouse_up", button: "left" });
      } else if (!leftHeld && !moved && Date.now() - startTime < TAP_MAX_MS) {
        if (maxFingers === 2) {
          send({ type: "click", button: "right" });
        } else if (maxFingers === 1) {
          send({ type: "click", button: "left" });
          lastTapTime = Date.now();
        }
      }
      maxFingers = 0;
    }

    pad.addEventListener("touchend", finishTouch);
    pad.addEventListener("touchcancel", finishTouch);

    const pressLeft = (e) => {
      e.preventDefault();
      if (leftHeld) return;
      leftHeld = true;
      leftBtn.classList.add("held");
      pad.classList.add("dragging");
      send({ type: "mouse_down", button: "left" });
    };
    const releaseLeft = (e) => {
      e.preventDefault();
      if (e.targetTouches && e.targetTouches.length > 0) return;
      if (!leftHeld) return;
      leftHeld = false;
      leftBtn.classList.remove("held");
      if (!isDragging) pad.classList.remove("dragging");
      send({ type: "mouse_up", button: "left" });
    };

    leftBtn.addEventListener("touchstart", pressLeft, { passive: false });
    leftBtn.addEventListener("touchend", releaseLeft, { passive: false });
    leftBtn.addEventListener("touchcancel", releaseLeft, { passive: false });
    leftBtn.addEventListener("mousedown", pressLeft);
    leftBtn.addEventListener("mouseup", releaseLeft);

    rightBtn.addEventListener("click", () => send({ type: "click", button: "right" }));

    const activeMods = new Set();

    document.querySelectorAll("[data-media]").forEach((btn) => {
      btn.addEventListener("click", () => send({ type: "media", action: btn.dataset.media }));
    });

    document.getElementById("qr-popup-btn").addEventListener("click", () => {
      send({ type: "show_qr" });
    });

    document.querySelectorAll("[data-mod]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const mod = btn.dataset.mod;
        if (activeMods.has(mod)) {
          activeMods.delete(mod);
          btn.classList.remove("active");
        } else {
          activeMods.add(mod);
          btn.classList.add("active");
        }
      });
    });

    function consumeMods() {
      const mods = Array.from(activeMods);
      activeMods.clear();
      document.querySelectorAll("[data-mod].active").forEach((b) => b.classList.remove("active"));
      return mods;
    }

    document.querySelectorAll("[data-key]").forEach((btn) => {
      const keyName = btn.dataset.key;
      const repeatable = btn.dataset.repeat === "1";

      if (!repeatable) {
        btn.addEventListener("click", () => {
          send({ type: "key", key: keyName, mods: consumeMods() });
        });
        return;
      }

      let holdTimeout = null;
      let repeatInterval = null;
      let heldMods = [];
      let isPressed = false;

      const startRepeat = (e) => {
        e.preventDefault();
        if (isPressed) return;
        isPressed = true;
        btn.classList.add("pressed");
        heldMods = Array.from(activeMods);
        send({ type: "key", key: keyName, mods: heldMods });

        holdTimeout = setTimeout(() => {
          repeatInterval = setInterval(() => {
            send({ type: "key", key: keyName, mods: heldMods });
          }, 75);
        }, 360);
      };

      const stopRepeat = (e) => {
        e.preventDefault();
        if (!isPressed) return;
        isPressed = false;
        btn.classList.remove("pressed");
        clearTimeout(holdTimeout);
        clearInterval(repeatInterval);
        if (heldMods.length > 0) consumeMods();
      };

      btn.addEventListener("touchstart", startRepeat, { passive: false });
      btn.addEventListener("touchend", stopRepeat, { passive: false });
      btn.addEventListener("touchcancel", stopRepeat, { passive: false });
      btn.addEventListener("mousedown", startRepeat);
      btn.addEventListener("mouseup", stopRepeat);
      btn.addEventListener("mouseleave", stopRepeat);
    });

    let composing = false;
    function resetKb() {
      kb.value = " ";
      kb.setSelectionRange(1, 1);
    }

    document.getElementById("kb-toggle").addEventListener("click", () => {
      resetKb();
      kb.focus();
    });

    function emitText(raw) {
      let text = raw;
      if (text.startsWith(" ")) text = text.slice(1);
      else if (text.endsWith(" ")) text = text.slice(0, -1);
      if (text.length > 0) {
        send({ type: "type", text, mods: consumeMods() });
      }
      resetKb();
    }

    kb.addEventListener("compositionstart", () => { composing = true; });
    kb.addEventListener("compositionend", () => {
      composing = false;
      emitText(kb.value);
    });

    kb.addEventListener("input", (e) => {
      if (composing) return;
      if (e.inputType === "deleteContentBackward" || kb.value.length === 0) {
        send({ type: "key", key: "backspace", mods: consumeMods() });
        resetKb();
      } else if (e.inputType === "insertLineBreak") {
        send({ type: "key", key: "enter", mods: consumeMods() });
        resetKb();
      } else if (kb.value.length > 1) {
        emitText(kb.value);
      }
    });

    kb.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        send({ type: "key", key: "enter", mods: consumeMods() });
        resetKb();
      }
    });
  </script>
</body>
</html>
"""


def _press_with_mods(mods: list[str], action) -> None:
    held = [MOD_KEYS[m] for m in mods if m in MOD_KEYS]
    try:
        for m in held:
            keyboard.press(m)
        action()
    finally:
        for m in reversed(held):
            keyboard.release(m)


async def authenticate_client(ws: WebSocket) -> str | None:
    global FAILED_PIN_ATTEMPTS, PIN_LOCKOUT_UNTIL

    nonce_bytes = secrets.token_bytes(32)
    nonce_b64 = base64.b64encode(nonce_bytes).decode("ascii")
    await ws.send_json({"type": "auth_challenge", "nonce": nonce_b64})

    try:
        resp = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
    except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
        await ws.close(code=4003, reason="Auth timeout")
        return None

    if resp.get("type") != "auth_response":
        await ws.close(code=4003, reason="Invalid auth message")
        return None

    device_id = str(resp.get("device_id", ""))
    signature_b64 = str(resp.get("signature", ""))

    with TRUSTED_LOCK:
        trusted = load_trusted_devices()
        stored_entry = trusted.get(device_id)

    if stored_entry is not None:
        valid, fp = verify_p256_signature(stored_entry["public_key"], nonce_bytes, signature_b64)
        if not valid or fp != device_id:
            logger.warning("Rejected spoofed signature for trusted device %s", device_id)
            await ws.close(code=4003, reason="Signature verification failed")
            return None

        with TRUSTED_LOCK:
            trusted = load_trusted_devices()
            if device_id in trusted:
                trusted[device_id]["last_seen"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                save_trusted_devices(trusted)

        await ws.send_json({"type": "auth_ok", "device_id": device_id})
        logger.info("Trusted device connected: %s", device_id)
        return device_id

    pairing_pin = str(resp.get("pairing_pin", ""))
    public_key_b64 = str(resp.get("public_key", ""))

    if not pairing_pin:
        await ws.close(code=4001, reason="Unpaired device. Enter the 6-digit PIN from PC.")
        return None

    now = time.monotonic()
    if now < PIN_LOCKOUT_UNTIL:
        wait_s = int(PIN_LOCKOUT_UNTIL - now) + 1
        await ws.close(code=4002, reason=f"Locked out after failed PINs. Wait {wait_s}s.")
        return None

    if not secrets.compare_digest(pairing_pin, PAIRING_PIN):
        FAILED_PIN_ATTEMPTS += 1
        if FAILED_PIN_ATTEMPTS >= 5:
            FAILED_PIN_ATTEMPTS = 0
            PIN_LOCKOUT_UNTIL = time.monotonic() + 30.0
            rotate_pairing_pin()
            logger.warning("5 failed PIN attempts. Rotated Pairing PIN.")
            await ws.close(code=4002, reason="Too many failed PINs. Check PC for new PIN.")
        else:
            await ws.close(code=4001, reason="Incorrect Pairing PIN.")
        return None

    valid, fp = verify_p256_signature(public_key_b64, nonce_bytes, signature_b64)
    if not valid or fp != device_id:
        await ws.close(code=4003, reason="Invalid device key or signature.")
        return None

    FAILED_PIN_ATTEMPTS = 0
    dev_name = str(resp.get("device_name", "Device"))[:40]
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with TRUSTED_LOCK:
        trusted = load_trusted_devices()
        trusted[device_id] = {
            "name": dev_name,
            "public_key": public_key_b64,
            "paired_at": now_str,
            "last_seen": now_str,
        }
        save_trusted_devices(trusted)
    rotate_pairing_pin()
    logger.info("Paired new device: %s (%s)", device_id, dev_name)
    await ws.send_json({"type": "auth_ok", "device_id": device_id, "paired": True})
    return device_id


@app.get("/")
async def index():
    return HTMLResponse(HTML_PAGE)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    device_id = await authenticate_client(ws)
    if not device_id:
        return

    held_buttons: set[Button] = set()
    try:
        while True:
            msg = await ws.receive_json()
            t = msg.get("type")

            if t == "move":
                mouse.move(msg.get("dx", 0), msg.get("dy", 0))
            elif t == "scroll":
                mouse.scroll(0, msg.get("dy", 0))
            elif t == "click":
                btn = Button.right if msg.get("button") == "right" else Button.left
                mouse.click(btn, 1)
            elif t == "mouse_down":
                btn = Button.right if msg.get("button") == "right" else Button.left
                mouse.press(btn)
                held_buttons.add(btn)
            elif t == "mouse_up":
                btn = Button.right if msg.get("button") == "right" else Button.left
                mouse.release(btn)
                held_buttons.discard(btn)
            elif t == "type":
                text = msg.get("text", "")
                mods = msg.get("mods", [])
                if mods and len(text) == 1:
                    _press_with_mods(mods, lambda: keyboard.tap(text.lower()))
                elif text:
                    keyboard.type(text)
            elif t == "key":
                key_obj = SPECIAL_KEYS.get(msg.get("key"))
                if key_obj:
                    _press_with_mods(msg.get("mods", []), lambda: keyboard.tap(key_obj))
            elif t == "media":
                media_obj = MEDIA_KEYS.get(msg.get("action"))
                if media_obj:
                    keyboard.tap(media_obj)
            elif t == "show_qr":
                if ACTIVE_TRAY_APP is not None:
                    ACTIVE_TRAY_APP.root.after(0, ACTIVE_TRAY_APP.show_popup)
    except WebSocketDisconnect:
        pass
    finally:
        for btn in list(held_buttons):
            mouse.release(btn)


# --- Background Uvicorn Server + System Tray & QR Popup ---

class BackgroundServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:
        pass  # Required when running Uvicorn off the main thread


def create_tray_icon_image() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(37, 99, 235, 255))
    draw.rounded_rectangle((18, 12, 46, 52), radius=12, fill=(240, 244, 248, 255))
    draw.line((32, 14, 32, 28), fill=(37, 99, 235, 255), width=3)
    return img


class TrayApp:
    def __init__(self, server: BackgroundServer, usb_stop: threading.Event):
        self.server = server
        self.usb_stop = usb_stop
        self.root = tk.Tk()
        self.root.title("Remote Mouse — Pair & Connect")
        self.root.configure(bg="#11151c")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_popup)
        self.root.withdraw()

        self.qr_label = tk.Label(self.root, bg="#ffffff", padx=8, pady=8)
        self.qr_label.pack(padx=20, pady=(18, 10))

        self.url_var = tk.StringVar()
        self.usb_var = tk.StringVar(value=f"USB Tunnel : https://localhost:{PORT}")
        self.pin_var = tk.StringVar()
        self.info_var = tk.StringVar()

        tk.Label(self.root, textvariable=self.url_var, bg="#11151c", fg="#93c5fd", font=("Consolas", 10, "bold")).pack(pady=(0, 2))
        tk.Label(self.root, textvariable=self.usb_var, bg="#11151c", fg="#86efac", font=("Consolas", 10, "bold")).pack(pady=(0, 6))
        tk.Label(self.root, textvariable=self.pin_var, bg="#11151c", fg="#f8fafc", font=("Consolas", 14, "bold")).pack(pady=(0, 4))
        tk.Label(self.root, textvariable=self.info_var, bg="#11151c", fg="#94a3b8", font=("Segoe UI", 9)).pack(pady=(0, 12))

        btn_row = tk.Frame(self.root, bg="#11151c")
        btn_row.pack(pady=(0, 16), padx=16, fill="x")

        tk.Button(
            btn_row, text="Refresh IP", command=self.refresh_popup,
            bg="#1f2937", fg="#f8fafc", relief="flat", padx=10, pady=6,
        ).pack(side="left", expand=True, fill="x", padx=(0, 4))
        tk.Button(
            btn_row, text="New PIN", command=self.new_pin_and_refresh,
            bg="#1f2937", fg="#f8fafc", relief="flat", padx=10, pady=6,
        ).pack(side="left", expand=True, fill="x", padx=4)
        tk.Button(
            btn_row, text="Hide", command=self.hide_popup,
            bg="#2563eb", fg="#ffffff", relief="flat", padx=10, pady=6,
        ).pack(side="left", expand=True, fill="x", padx=(4, 0))

        self.icon = pystray.Icon(
            APP_NAME,
            create_tray_icon_image(),
            "Remote Mouse (HTTPS + ECDSA)",
            menu=pystray.Menu(
                pystray.MenuItem("Show QR Code & PIN", self._on_show, default=True),
                pystray.MenuItem("Regenerate Pairing PIN", self._on_new_pin),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "Start with Windows",
                    self._on_toggle_startup,
                    checked=lambda _item: is_startup_enabled(),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._on_quit),
            ),
        )

    def refresh_popup(self) -> None:
        ip = get_local_ip()
        base_url = f"https://{ip}:{PORT}"
        qr_url = f"{base_url}/?pin={PAIRING_PIN}"

        qr = qrcode.QRCode(border=2, box_size=6)
        qr.add_data(qr_url)
        qr.make(fit=True)
        pil_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        photo = ImageTk.PhotoImage(pil_img)
        self.qr_label.configure(image=photo)
        self.qr_label.image = photo

        with TRUSTED_LOCK:
            trusted_count = len(load_trusted_devices())

        self.url_var.set(f"Wi-Fi URL  : {base_url}")
        self.pin_var.set(f"Pairing PIN: {PAIRING_PIN}")
        if ip == "127.0.0.1":
            self.info_var.set("No Wi-Fi/Hotspot yet — USB tunnel active | Refresh IP once connected")
        else:
            self.info_var.set(f"Encrypted WSS + ECDSA P-256  |  Trusted origins: {trusted_count}")

    def show_popup(self) -> None:
        self.refresh_popup()
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(200, lambda: self.root.attributes("-topmost", False))

    def hide_popup(self) -> None:
        self.root.withdraw()

    def new_pin_and_refresh(self) -> None:
        rotate_pairing_pin()
        self.refresh_popup()

    def _on_show(self, _icon, _item) -> None:
        self.root.after(0, self.show_popup)

    def _on_new_pin(self, _icon, _item) -> None:
        rotate_pairing_pin()
        if self.root.state() == "normal":
            self.root.after(0, self.refresh_popup)

    def _on_toggle_startup(self, icon, _item) -> None:
        set_windows_startup(not is_startup_enabled())
        icon.update_menu()

    def _on_quit(self, _icon, _item) -> None:
        self.usb_stop.set()
        self.server.should_exit = True
        self.icon.stop()
        self.root.after(0, self.root.destroy)

    def run(self, show_on_start: bool = False) -> None:
        tray_thread = threading.Thread(target=self.icon.run, daemon=True)
        tray_thread.start()
        if show_on_start:
            self.root.after(250, self.show_popup)
        self.root.mainloop()


def handle_cli() -> tuple[bool, bool]:
    parser = argparse.ArgumentParser(description="Secure Remote Mouse & Keyboard Host")
    parser.add_argument("--devices", action="store_true", help="List locally trusted devices")
    parser.add_argument("--revoke", metavar="DEVICE_ID", help="Revoke a trusted device by ID")
    parser.add_argument("--revoke-all", action="store_true", help="Revoke all trusted devices")
    parser.add_argument("--install-startup", action="store_true", help="Enable silent start on Windows login")
    parser.add_argument("--remove-startup", action="store_true", help="Disable start on Windows login")
    parser.add_argument("--show", action="store_true", help="Open QR popup immediately on launch")
    args = parser.parse_args()

    if args.devices:
        with TRUSTED_LOCK:
            trusted = load_trusted_devices()
        if not trusted:
            print("No trusted devices paired.")
        for dev_id, info in trusted.items():
            print(f"{dev_id} | {info.get('name')} | paired: {info.get('paired_at')} | last seen: {info.get('last_seen')}")
        return True, False

    if args.revoke:
        with TRUSTED_LOCK:
            trusted = load_trusted_devices()
            if args.revoke in trusted:
                del trusted[args.revoke]
                save_trusted_devices(trusted)
                print(f"Revoked device: {args.revoke}")
            else:
                print(f"Device ID not found: {args.revoke}")
        return True, False

    if args.revoke_all:
        with TRUSTED_LOCK:
            save_trusted_devices({})
        print("All trusted devices revoked.")
        return True, False

    if args.install_startup:
        ok = set_windows_startup(True)
        print("Windows startup enabled." if ok else "Failed to enable Windows startup.")
        return True, False

    if args.remove_startup:
        ok = set_windows_startup(False)
        print("Windows startup disabled." if ok else "Failed to disable Windows startup.")
        return True, False

    return False, args.show


def main() -> None:
    global ACTIVE_TRAY_APP

    handled, show_on_start = handle_cli()
    if handled:
        return

    local_ip = get_local_ip()
    ensure_tls_cert(local_ip)

    config = uvicorn.Config(
        app,
        host=HOST,
        port=PORT,
        ssl_certfile=str(CERT_FILE),
        ssl_keyfile=str(KEY_FILE),
        log_config=None,
    )
    server = BackgroundServer(config)
    threading.Thread(target=server.run, daemon=True).start()
    logger.info("Remote Mouse server started on https://%s:%d", local_ip, PORT)

    usb_stop = threading.Event()
    threading.Thread(target=usb_adb_watcher, args=(usb_stop,), daemon=True).start()

    tray_app = TrayApp(server, usb_stop)
    ACTIVE_TRAY_APP = tray_app
    try:
        tray_app.run(show_on_start=show_on_start)
    finally:
        usb_stop.set()


if __name__ == "__main__":
    main()