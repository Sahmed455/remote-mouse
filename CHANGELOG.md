# Changelog

All notable changes to this project will be documented in this file.

## [v1.3.0.0] - 2026-11-25

### 🚀 Low-Latency Transport
- **Unreliable WebRTC UDP DataChannel**: Routed continuous `mouse_move` and `air_move` cursor deltas over an unordered, zero-retransmit WebRTC DataChannel (`{ ordered: false, maxRetransmits: 0 }`) signaled via WebSocket, eliminating Wi-Fi TCP head-of-line stalls.
- **Automatic Fallback & USB Bypass**: Seamlessly falls back to WebSocket if UDP ICE fails or times out (5 s watchdog), and skips WebRTC over USB ADB tunnels (`localhost`).
- **Smart App Control Resilient WebRTC**: Data-only `aiortc` loader gracefully stubs unused PyAV audio/video media types if native FFmpeg DLLs are blocked by Windows Smart App Control.

### ⌨️ Win32 Keyboard & Non-Blocking Input
- **Keyboard Injection Rewrite**: Fixed silent `SendInput` failures and `VkKeyScanW` `ctypes.ArgumentError` crashes caused by shared `windll.user32` `argtypes` mutation from `pynput`. Added isolated `WinDLL('user32', use_last_error=True)`, 40-byte `INPUT` union, `KEYEVENTF_UNICODE` text injection, `MapVirtualKeyW` hardware scancodes, and `KEYEVENTF_EXTENDEDKEY` flags.
- **High-Priority Input Worker**: Enabled 1 ms Windows timer resolution (`timeBeginPeriod(1)`), `THREAD_PRIORITY_HIGHEST`, batched `[DOWN, UP]` clicks, and non-blocking `SimpleQueue` dispatch to remove 10–18 ms scheduler stalls.

### 📊 Bidirectional Latency Profiler
- **Movement Inter-Arrival & Clock Sync**: Added high-frequency `mouse_move` / `air_move` profiling, `prof_ping` / `prof_pong` 5-sample RTT clock calibration, CSV jitter/burst-gap columns (`est_one_way_ms`, `rel_transport_jitter_ms`, `client_dt_ms`, `server_recv_dt_ms`, `burst_gap_ms`), and JSON/console percentile summaries.

### 🛠️ CI / Release Pipeline
- **Opt-In Test Release Builds**: Made GitHub Actions release packaging opt-in (`workflow_dispatch` with `build_release=true` and optional `publish_prerelease=true`, or `v*` tags) so branch pushes to `test` do not automatically trigger builds.

## [v1.2.1.0] - 2026-11-24

### 📦 Dual MSIX Build Pipeline
- **Store MSIX** (`*_Store.msix`): Unsigned package for direct upload to Microsoft Partner Center — Microsoft signs it, SAC trusts it, installs silently on any machine.
- **Sideload MSIX** (`*_Sideload.msix`): Self-signed package for rapid local dev testing without waiting for Store certification (requires SAC off + one-time cert trust on target machine).
- **Release Notes**: GitHub Release now includes an installation guide table distinguishing both variants.

### 🔒 Security & Code Signing
- **Persistent Release Certificate**: Embedded a persistent 10-year Code Signing Certificate (`RemoteMouse_Persistent.pfx`) so all CI and local builds sign with the exact same trusted certificate instead of disposable runners.
- **UIAccess Support**: Enabled `--windows-uac-uiaccess` in Nuitka compilation and GitHub Actions packaging, allowing installed MSIX applications to interact with Administrator terminals, Task Manager, Windows Security, and Authenticator popups.

### ⚡ Hardware Input & Elevation
- **Direct Win32 Hardware Pipeline**: Replaced pynput mouse clicks, holds, and scrolls with direct Win32 `_win32_mouse_event`.
- **Physical Virtual-Key Input**: Added `VkKeyScanW` translation and `_win32_keybd_event` with `KEYEVENTF_EXTENDEDKEY` support for `⊞ Win`, arrows, and special keys so PIN and credential fields accept typed characters.
- **In-App Elevation**: Added a one-click `🛡️ Run as Administrator` option in the PC system tray menu and `--elevate` CLI flag, with visual `🛡️ Admin` status on the mobile web topbar.
- **Stability Fix**: Removed leaky desktop handle synchronization that caused input queue freezes.

## [v1.2.0.0] - 2026-11-24

### 🚀 Features & Enhancements
- **Air Mouse 3D Controller**: Control cursor motion by tilting and rotating your mobile phone in 3D space using hardware orientation sensor fusion (Azimuth & Pitch).
- **Handheld Control Deck**: Dedicated Air Mouse interface with Left Click (Hold to Drag), Scroll Wheel strip, Middle Click, Right Click, Host Cursor Centering, and Invert X/Y axis toggles.
- **High-Speed Binary Motion Protocol**: 5-byte compact WebSocket binary stream (`[0x01, dx, dy]`) slashing network overhead by 85% and bypassing JSON serialization.
- **Low-Latency Fast-Path**: 125Hz fast-path flush bypassing mobile display vsync delays, sub-pixel rounding with error diffusion, and direct Win32 relative mouse injection.
- **PC Cursor Centering**: One-touch center button (`center_cursor`) snapping cursor to primary display center and re-anchoring sensor baseline.
- **Touchpad Double-Tap & Drag Fix**: Relaxed tap deadzone, phantom click prevention buffer, and silky-smooth non-linear acceleration curve.

## [v1.1.1.0] - 2026-11-23

### 🐛 Bug Fixes
- restore TargetDeviceFamily MinVersion and use XML-aware version update (5d99f78)

## [v1.1.0.0] - 2026-11-23

### 🚀 Features & Enhancements
- add seamless USB-to-WiFi handoff button and token migration (7438fb9)

### 📦 Build & Documentation
- update app display name to DeviLX KeyWave (cef60ee)
- adopt GNU General Public License v3.0 (81188e1)

## [v1.0.1.0] - 2026-11-23

### 🔒 Security Updates
- avoid eager evaluation of deprecated not_valid_after (9da7ec9)

### 📦 Build & Documentation
- add privacy policy for Microsoft Store listing (0397141)
- set Store display name to Remote MouseKB Server (037c186)
- finalize modular Nuitka and MSIX packaging pipeline (6c8bf46)
