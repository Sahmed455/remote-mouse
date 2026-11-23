# Remote Mouse & Keyboard Server (Remote MouseKB)

[![Release](https://github.com/Sahmed455/remote-mouse/actions/workflows/release.yml/badge.svg)](https://github.com/Sahmed455/remote-mouse/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

An encrypted, zero-cloud local network and USB server that turns any mobile browser into a low-latency touchpad, keyboard, and media remote for your Windows PC.

---

## ✨ Features

- **Zero Cloud, 100% Local & Private**: No cloud relay or external telemetry. Operates entirely over your local Wi-Fi or a tethered USB cable.
- **End-to-End Encryption & Pairing**:
  - Automatically provisions self-signed ECDSA TLS certificates (`server.crt` / `server.key`).
  - Challenge-response authentication backed by non-extractable client ECDSA P-256 keys and a rotating 6-digit PIN.
- **Touchpad & Multi-Touch Gestures**:
  - Smooth relative cursor navigation with acceleration curves and sensitivity controls.
  - Multi-touch gestures: two-finger tap for right click, two-finger scroll with optional natural/inverted scroll.
  - Drag-and-drop lock support.
- **Full Keyboard & Media Controls**:
  - Standard alphanumeric input and special keys (Enter, Backspace, Tab, Esc, Win, Arrow keys, F1–F12).
  - Modifier toggles (`Ctrl`, `Alt`, `Shift`, `Win`).
  - Dedicated media buttons (Volume Up/Down, Mute, Play/Pause, Next/Previous Track).
- **Zero App Install on Phone**: Uses modern Web standards (HTML5, WebSockets, Web Crypto API) directly inside Chrome, Safari, Firefox, or Edge.
- **Instant Connect via QR Code**: Displays a pairing QR code with your LAN IP and PIN directly from the Windows system tray.
- **Ultra-Fast USB Tunneling**: Automatic ADB reverse-proxy (`adb reverse tcp:8000 tcp:8000`) for low-latency tethering without Wi-Fi.
- **Flexible Distribution**: Run directly via Python, standalone compiled executable (via Nuitka), or package as a signed Windows MSIX / Microsoft Store app.

---

## 🚀 Quick Start (Development / Running from Source)

### Prerequisites
- Python 3.12+ (Python 3.14 recommended)
- Windows 10 / 11

### 1. Clone & Set Up Virtual Environment

```powershell
git clone https://github.com/Sahmed455/remote-mouse.git
cd remote-mouse

# Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install package dependencies
pip install -e .
```

### 2. Run the Application

```powershell
# Run using the wrapper
python run.py

# Or directly as a module
python -m remote_mouse
```

Once running:
1. A system tray icon (green mouse) will appear in your Windows taskbar notification area.
2. Left-click the tray icon to view the **Pairing QR Code** and current PIN.
3. On your phone, scan the QR code (or browse to `https://<YOUR_PC_IP>:8000`).
4. Accept the local self-signed certificate in your phone browser, enter the 6-digit pairing PIN, and start controlling your PC!

---

## 🛠️ CLI Options

The server provides a command-line interface for device and startup management:

```powershell
python run.py --help
```

| Argument | Description |
| :--- | :--- |
| `--show` | Launch the server and immediately pop open the pairing QR code window |
| `--devices` | List all locally trusted and paired devices |
| `--revoke <DEVICE_ID>` | Revoke access for a specific device ID |
| `--revoke-all` | Revoke all paired devices (forces re-pairing) |
| `--install-startup` | Enable automatic silent launch on Windows user login (Registry) |
| `--remove-startup` | Disable automatic launch on Windows login |

---

## 📦 Packaging & Building

### Standalone Executable (Nuitka)
Compile into a standalone, optimized single-folder Windows executable without requiring Python on the target machine:

```powershell
# Install build dependencies
pip install -e ".[build]"

# Build standalone executable (placed in C:\BuildCache\RemoteMouse\dist)
.\packaging\nuitka\build_exe.ps1
```

### Windows MSIX Package
Package the compiled application into a modern Windows MSIX package suitable for sideloading or Microsoft Store submission:

```powershell
# Requires Windows SDK (MakeAppx.exe, SignTool.exe)
.\packaging\msix\build_msix.ps1
```

---

## 🏷️ Release & Version Management

This repository includes a unified release automation script `scripts/release.ps1` that coordinates versions across **Python (`pyproject.toml`)**, **Windows MSIX (`AppxManifest.xml`)**, **Nuitka PE headers**, **`CHANGELOG.md`**, and **Git tags**:

```powershell
# Patch / Bug fix release (e.g., 1.0.0 -> 1.0.1 / 1.0.1.0):
.\scripts\release.ps1 -Type patch -CommitAndTag

# Feature / Minor release (e.g., 1.0.1 -> 1.1.0 / 1.1.0.0):
.\scripts\release.ps1 -Type minor -CommitAndTag

# Major breaking release (e.g., 1.1.0 -> 2.0.0 / 2.0.0.0):
.\scripts\release.ps1 -Type major -CommitAndTag

# Explicit version target:
.\scripts\release.ps1 -Version "1.0.1.0" -CommitAndTag
```

When pushed with tags (`git push origin main --tags`), GitHub Actions triggers `.github/workflows/release.yml` to compile the Windows standalone binary and publish a GitHub Release.

---

## 🔒 Security & Privacy

For full privacy disclosures, see [PRIVACY.md](PRIVACY.md).
- Certificates and trusted device keys are stored strictly in `%LOCALAPPDATA%\RemoteMouse`.
- No clipboard scraping or key logging is performed.
- Device pairings can be inspected or wiped at any time through the system tray menu or `--revoke-all`.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
