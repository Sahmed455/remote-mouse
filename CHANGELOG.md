# Changelog

All notable changes to this project will be documented in this file.

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
