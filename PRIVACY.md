# Privacy Policy — Remote MouseKB Server

**Effective Date:** November 21, 2026  
**Publisher:** DeviLX  
**Application:** Remote MouseKB Server (`DeviLX.RemoteMouseKBServer`)

## 1. Summary
Remote MouseKB Server is a local-only utility that lets you control your Windows PC's mouse, keyboard, and media playback from a mobile browser over your local Wi-Fi network or a direct USB cable. **We do not collect, transmit, store, sell, or share any personal data, usage analytics, or telemetry.**

## 2. Information We Do Not Collect
Remote MouseKB Server operates entirely on your local devices and never contacts external servers. Specifically, the application does **not** collect:
- Personal identifiers, account information, or contact details
- Keystrokes, clipboard contents, cursor movements, or screen data
- Crash reports, diagnostics, analytics, or advertising identifiers
- Location data or browsing history

## 3. Data Stored Locally on Your Devices
To provide encrypted local connections and persistent pairing without cloud accounts, the app stores minimal operational files strictly on your own hardware:

- **On your Windows PC (`%LOCALAPPDATA%\RemoteMouse`):**
  - `server.crt` and `server.key` — A locally generated self-signed ECDSA TLS certificate used to encrypt local HTTPS/WSS traffic between your phone and PC.
  - `trusted_devices.json` — Public keys (ECDSA P-256), device labels, and pairing timestamps for devices you explicitly authorize via the one-time 6-digit PIN.
  - `server.log` — Local startup and connection status logs for troubleshooting.
- **On your mobile browser:**
  - A non-extractable ECDSA P-256 key pair stored in browser IndexedDB for challenge-response authentication.
  - Your touchpad preferences (pointer sensitivity, scroll speed, acceleration, and scroll direction) stored in browser `localStorage`.

None of these files ever leave your local devices.

## 4. Network & USB Permissions
- **Local Network (`privateNetworkClientServer` / `internetClient`):** Used solely to host the local HTTPS/WSS server on TCP port `8000` and detect your PC's local LAN IP address so your phone can connect on the same network.
- **Optional USB Tunneling:** If Android Debug Bridge (`adb`) is installed on your PC, the app runs `adb reverse tcp:8000 tcp:8000` locally so a USB-connected Android device can reach `https://localhost:8000` without Wi-Fi.

## 5. Deleting Local Data & Revoking Devices
You remain in full control of all locally stored data:
- Delete paired devices or local certificates at any time by clicking **Open Data Folder** in the system tray menu and removing files inside `%LOCALAPPDATA%\RemoteMouse`.
- Uninstalling the application removes the program; clearing your mobile browser's site data removes the client-side key and preferences.

## 6. Contact
If you have questions about this Privacy Policy, please open an issue on the project repository:  
[https://github.com/Sahmed455/remote-mouse/issues](https://github.com/Sahmed455/remote-mouse/issues)