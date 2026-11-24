import asyncio
import base64
import csv
import json
import secrets
import socket
import struct
import sys
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pynput.keyboard import Controller as KeyboardController
from pynput.keyboard import Key
from pynput.mouse import Button
from pynput.mouse import Controller as MouseController

from remote_mouse.config import PORT, WEB_INDEX_FILE, get_local_ip, is_admin, logger

# ---------------------------------------------------------------------------
# Latency Profiler — zero overhead when disabled
# ---------------------------------------------------------------------------
_profiling: bool = False          # toggled by /api/profile/toggle
_server_log: deque = deque(maxlen=20_000)   # {seq, t, recv_ns, inject_ns}
_profile_start_wall: float = 0.0  # wall-clock epoch at profile start (for report header)

def _prof_record(seq: int, evt_type: str, recv_ns: int, inject_ns: int) -> None:
    """Append one server-side timing entry. Called only when _profiling is True."""
    _server_log.append({
        "seq": seq, "t": evt_type,
        "recv_ns": recv_ns, "inject_ns": inject_ns,
    })
from remote_mouse.security import (
    TRUSTED_LOCK,
    consume_migration_token,
    create_migration_token,
    load_trusted_devices,
    rotate_pairing_pin,
    save_trusted_devices,
    verify_p256_signature,
    verify_pairing_pin,
)

app = FastAPI()

mouse = MouseController()
keyboard = KeyboardController()
_qr_callback: Callable[[], None] | None = None

# ---------------------------------------------------------------------------
# Profile REST endpoints
# ---------------------------------------------------------------------------

@app.get("/api/profile/toggle")
async def profile_toggle():
    """Enable or disable latency profiling. Returns new state."""
    global _profiling, _profile_start_wall
    _profiling = not _profiling
    if _profiling:
        _server_log.clear()
        _profile_start_wall = time.time()
        logger.info("Latency profiling ENABLED")
    else:
        logger.info("Latency profiling DISABLED — %d server events captured", len(_server_log))
    return JSONResponse({"profiling": _profiling, "server_events": len(_server_log)})


@app.get("/api/profile/status")
async def profile_status():
    return JSONResponse({"profiling": _profiling, "server_events": len(_server_log)})


@app.post("/api/profile/upload")
async def profile_upload(payload: dict):
    """
    Receive client-side log from the phone, merge with server log, write report files.
    Returns a summary and the path to the saved CSV.
    """
    client_log = payload.get("data", [])      # [{seq, t, cts_ms}, ...]
    clock_offset_ms = payload.get("clock_offset_ms", 0.0)  # NTP-estimated offset

    report_dir = Path.home() / ".remote_mouse"
    report_dir.mkdir(parents=True, exist_ok=True)
    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # Write raw client log
    client_path = report_dir / f"profile_client_{ts_tag}.jsonl"
    with client_path.open("w", encoding="utf-8") as f:
        for entry in client_log:
            f.write(json.dumps(entry) + "\n")

    # Write raw server log
    server_path = report_dir / f"profile_server_{ts_tag}.jsonl"
    srv_entries = list(_server_log)
    with server_path.open("w", encoding="utf-8") as f:
        for entry in srv_entries:
            f.write(json.dumps(entry) + "\n")

    # Build merged CSV indexed by seq
    srv_by_seq = {e["seq"]: e for e in srv_entries}
    csv_path = report_dir / f"profile_report_{ts_tag}.csv"
    matched = 0
    rows = []
    for ce in client_log:
        seq = ce.get("seq")
        se = srv_by_seq.get(seq)
        if se is None:
            continue
        matched += 1
        recv_ns  = se["recv_ns"]
        inject_ns = se["inject_ns"]
        cts_ms   = ce.get("cts_ms", 0.0)
        # Estimated one-way transport latency using clock offset calibration
        srv_recv_ms = recv_ns / 1_000_000.0
        # adjusted client send time in server clock space
        adj_send_ms = cts_ms + clock_offset_ms
        transport_ms = srv_recv_ms - adj_send_ms
        inject_overhead_ms = (inject_ns - recv_ns) / 1_000_000.0
        rows.append({
            "seq": seq,
            "event": ce.get("t", "?"),
            "client_send_ms": round(cts_ms, 3),
            "srv_recv_ms": round(srv_recv_ms, 3),
            "srv_inject_ms": round(inject_ns / 1_000_000.0, 3),
            "transport_est_ms": round(transport_ms, 3),
            "inject_overhead_ms": round(inject_overhead_ms, 3),
            "total_est_ms": round(transport_ms + inject_overhead_ms, 3),
        })

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "seq", "event", "client_send_ms", "srv_recv_ms", "srv_inject_ms",
            "transport_est_ms", "inject_overhead_ms", "total_est_ms"
        ])
        writer.writeheader()
        writer.writerows(rows)

    # Summary stats
    if rows:
        transports = [r["transport_est_ms"] for r in rows]
        overheads  = [r["inject_overhead_ms"] for r in rows]
        totals     = [r["total_est_ms"] for r in rows]
        summary = {
            "matched_events": matched,
            "transport_avg_ms":  round(sum(transports) / len(transports), 2),
            "transport_p95_ms":  round(sorted(transports)[int(len(transports) * 0.95)], 2),
            "inject_avg_ms":     round(sum(overheads) / len(overheads), 4),
            "inject_p95_ms":     round(sorted(overheads)[int(len(overheads) * 0.95)], 4),
            "total_avg_ms":      round(sum(totals) / len(totals), 2),
            "total_p95_ms":      round(sorted(totals)[int(len(totals) * 0.95)], 2),
            "report_csv":        str(csv_path),
        }
    else:
        summary = {"matched_events": 0, "note": "No matching seq numbers found", "report_csv": str(csv_path)}

    logger.info("Profile report written: %s (%d matched events)", csv_path, matched)
    return JSONResponse(summary)

if sys.platform == "win32":
    import ctypes
    _user32 = ctypes.windll.user32
    _win32_mouse_event = _user32.mouse_event
    _win32_get_metric = _user32.GetSystemMetrics
    _win32_set_cursor = _user32.SetCursorPos
    _win32_keybd_event = _user32.keybd_event
    _win32_vk_key_scan = _user32.VkKeyScanW

    # Win32 Mouse Event Flags
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_MIDDLEDOWN = 0x0020
    MOUSEEVENTF_MIDDLEUP = 0x0040
    MOUSEEVENTF_WHEEL = 0x0800

    SPECIAL_VK = {
        "backspace": 0x08,
        "tab": 0x09,
        "enter": 0x0D,
        "esc": 0x1B,
        "space": 0x20,
        "page_up": 0x21,
        "page_down": 0x22,
        "end": 0x23,
        "home": 0x24,
        "left": 0x25,
        "up": 0x26,
        "right": 0x27,
        "down": 0x28,
        "delete": 0x2E,
        "win": 0x5B,
        "insert": 0x2D,
        "print_screen": 0x2C,
        **{f"f{i}": 0x6F + i for i in range(1, 13)},
    }

    MOD_VK = {
        "ctrl": 0x11,
        "alt": 0x12,
        "shift": 0x10,
        "win": 0x5B,
    }

    def _fast_relative_move(dx: int, dy: int) -> None:
        _win32_mouse_event(MOUSEEVENTF_MOVE, dx, dy, 0, 0)

    def _do_click(btn_name: str) -> None:
        if btn_name == "right":
            _win32_mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
            _win32_mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
        elif btn_name == "middle":
            _win32_mouse_event(MOUSEEVENTF_MIDDLEDOWN, 0, 0, 0, 0)
            _win32_mouse_event(MOUSEEVENTF_MIDDLEUP, 0, 0, 0, 0)
        else:
            _win32_mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            _win32_mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def _do_mouse_down(btn_name: str) -> None:
        if btn_name == "right":
            _win32_mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
        elif btn_name == "middle":
            _win32_mouse_event(MOUSEEVENTF_MIDDLEDOWN, 0, 0, 0, 0)
        else:
            _win32_mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)

    def _do_mouse_up(btn_name: str) -> None:
        if btn_name == "right":
            _win32_mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
        elif btn_name == "middle":
            _win32_mouse_event(MOUSEEVENTF_MIDDLEUP, 0, 0, 0, 0)
        else:
            _win32_mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def _do_scroll(dy: float) -> None:
        wheel_delta = int(dy * 120)
        if wheel_delta:
            _win32_mouse_event(MOUSEEVENTF_WHEEL, 0, 0, wheel_delta, 0)

    def _win32_type_char(ch: str) -> bool:
        """Types a character using physical Virtual Key codes so password/PIN fields accept it."""
        res = _win32_vk_key_scan(ord(ch))
        if res == -1:
            return False
        vk = res & 0xFF
        shift = (res >> 8) & 1
        ctrl = (res >> 8) & 2
        alt = (res >> 8) & 4

        if shift: _win32_keybd_event(0x10, 0, 0, 0)
        if ctrl: _win32_keybd_event(0x11, 0, 0, 0)
        if alt: _win32_keybd_event(0x12, 0, 0, 0)

        _win32_keybd_event(vk, 0, 0, 0)
        _win32_keybd_event(vk, 0, 2, 0)

        if alt: _win32_keybd_event(0x12, 0, 2, 0)
        if ctrl: _win32_keybd_event(0x11, 0, 2, 0)
        if shift: _win32_keybd_event(0x10, 0, 2, 0)
        return True

    def _do_type(text: str, mods: list[str] | None = None) -> None:
        mods = mods or []
        if mods and len(text) == 1:
            for mod in mods:
                mvk = MOD_VK.get(mod)
                if mvk: _win32_keybd_event(mvk, 0, 0, 0)
            _win32_type_char(text.lower())
            for mod in reversed(mods):
                mvk = MOD_VK.get(mod)
                if mvk: _win32_keybd_event(mvk, 0, 2, 0)
        else:
            for ch in text:
                if not _win32_type_char(ch):
                    keyboard.type(ch)

    EXTENDED_VKS = {0x5B, 0x5C, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}

    def _do_key(key_name: str, mods: list[str] | None = None) -> None:
        mods = mods or []
        vk = SPECIAL_VK.get(key_name)
        if vk:
            ext = 1 if vk in EXTENDED_VKS else 0
            for mod in mods:
                mvk = MOD_VK.get(mod)
                if mvk:
                    mext = 1 if mvk in EXTENDED_VKS else 0
                    _win32_keybd_event(mvk, 0, mext, 0)
            _win32_keybd_event(vk, 0, ext, 0)
            _win32_keybd_event(vk, 0, ext | 2, 0)
            for mod in reversed(mods):
                mvk = MOD_VK.get(mod)
                if mvk:
                    mext = 1 if mvk in EXTENDED_VKS else 0
                    _win32_keybd_event(mvk, 0, mext | 2, 0)
        else:
            key_obj = SPECIAL_KEYS.get(key_name)
            if key_obj:
                _press_with_mods(mods, lambda: keyboard.tap(key_obj))
else:
    def _fast_relative_move(dx: int, dy: int) -> None:
        mouse.move(dx, dy)

    def _do_click(btn_name: str) -> None:
        btn = Button.right if btn_name == "right" else (Button.middle if btn_name == "middle" else Button.left)
        mouse.click(btn, 1)

    def _do_mouse_down(btn_name: str) -> None:
        btn = Button.right if btn_name == "right" else (Button.middle if btn_name == "middle" else Button.left)
        mouse.press(btn)

    def _do_mouse_up(btn_name: str) -> None:
        btn = Button.right if btn_name == "right" else (Button.middle if btn_name == "middle" else Button.left)
        mouse.release(btn)

    def _do_scroll(dy: float) -> None:
        mouse.scroll(0, dy)

    def _do_type(text: str, mods: list[str] | None = None) -> None:
        mods = mods or []
        if mods and len(text) == 1:
            _press_with_mods(mods, lambda: keyboard.tap(text.lower()))
        elif text:
            keyboard.type(text)

    def _do_key(key_name: str, mods: list[str] | None = None) -> None:
        mods = mods or []
        key_obj = SPECIAL_KEYS.get(key_name)
        if key_obj:
            _press_with_mods(mods, lambda: keyboard.tap(key_obj))

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


class BackgroundServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:
        pass  # Required when running Uvicorn off the main thread


def set_qr_callback(callback: Callable[[], None] | None) -> None:
    global _qr_callback
    _qr_callback = callback


def _press_with_mods(mods: list[str], action: Callable[[], None]) -> None:
    held = [MOD_KEYS[m] for m in mods if m in MOD_KEYS]
    try:
        for m in held:
            keyboard.press(m)
        action()
    finally:
        for m in reversed(held):
            keyboard.release(m)


async def authenticate_client(ws: WebSocket) -> str | None:
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

        await ws.send_json({"type": "auth_ok", "device_id": device_id, "is_admin": is_admin()})
        logger.info("Trusted device connected: %s", device_id)
        return device_id

    pairing_pin = str(resp.get("pairing_pin", ""))
    public_key_b64 = str(resp.get("public_key", ""))
    migration_token = str(resp.get("migration_token", ""))

    dev_name = str(resp.get("device_name", "Device"))[:40]

    # Check if client presents a valid one-time migration token from USB pairing
    token_valid = False
    if migration_token:
        mig_data = consume_migration_token(migration_token)
        if mig_data:
            token_valid = True
            dev_name = mig_data.get("name", dev_name)
            logger.info("Validated USB-to-Wi-Fi migration token for %s", device_id)

    if not token_valid:
        if not pairing_pin:
            await ws.close(code=4001, reason="Unpaired device. Enter the 6-digit PIN from PC.")
            return None

        ok, code, reason = verify_pairing_pin(pairing_pin)
        if not ok:
            await ws.close(code=code, reason=reason)
            return None

    valid, fp = verify_p256_signature(public_key_b64, nonce_bytes, signature_b64)
    if not valid or fp != device_id:
        await ws.close(code=4003, reason="Invalid device key or signature.")
        return None

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

    if not token_valid:
        rotate_pairing_pin()
    logger.info("Paired device: %s (%s)", device_id, dev_name)
    await ws.send_json({"type": "auth_ok", "device_id": device_id, "paired": True, "is_admin": is_admin()})
    return device_id


@app.get("/")
async def index():
    return HTMLResponse(WEB_INDEX_FILE.read_text(encoding="utf-8"))


@app.get("/api/wifi-handoff")
async def get_wifi_handoff(device_id: str = ""):
    """Provide local Wi-Fi IP and a single-use handoff token for authenticated USB clients."""
    with TRUSTED_LOCK:
        trusted = load_trusted_devices()
        dev_info = trusted.get(device_id)

    if not dev_info:
        return {"error": "Device not trusted", "supported": False}

    local_ip = get_local_ip()
    token = create_migration_token(device_id, dev_info.get("name", "Device"))
    wifi_url = f"https://{local_ip}:{PORT}/?handoff={token}"

    return {
        "supported": local_ip != "127.0.0.1",
        "local_ip": local_ip,
        "port": PORT,
        "token": token,
        "wifi_url": wifi_url,
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()

    # Disable Nagle's buffering algorithm on Wi-Fi TCP sockets
    try:
        transport = ws.scope.get("transport")
        if transport:
            sock = transport.get_extra_info("socket")
            if sock:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception as e:
        logger.debug("Could not set TCP_NODELAY: %s", e)

    device_id = await authenticate_client(ws)
    if not device_id:
        return

    held_buttons: set[Button] = set()
    try:
        while True:
            raw = await ws.receive()
            raw_type = raw.get("type")
            if raw_type == "websocket.disconnect":
                break

            # Fast-path for binary motion packets (5 bytes: 0x01, int16 dx, int16 dy)
            data_bytes = raw.get("bytes")
            if data_bytes:
                if len(data_bytes) == 5 and data_bytes[0] == 0x01:
                    dx, dy = struct.unpack_from("<hh", data_bytes, 1)
                    if dx or dy:
                        _fast_relative_move(dx, dy)
                elif len(data_bytes) == 5 and data_bytes[0] == 0x02:
                    dy = struct.unpack_from("<f", data_bytes, 1)[0]
                    _do_scroll(dy)
                continue

            text = raw.get("text")
            if not text:
                continue

            msg = json.loads(text)
            t = msg.get("type")

            # Latency profiling — record recv timestamp before dispatch, inject timestamp after.
            # Branch is completely dead when _profiling is False (single bool check per JSON msg).
            _do_prof = _profiling and t not in ("ping_probe",)
            _recv_ns = time.perf_counter_ns() if _do_prof else 0
            _seq = msg.get("seq", -1) if _do_prof else -1

            if t == "move":
                dx = int(msg.get("dx", 0))
                dy = int(msg.get("dy", 0))
                if dx or dy:
                    _fast_relative_move(dx, dy)
            elif t == "center_cursor":
                try:
                    if sys.platform == "win32":
                        w = _win32_get_metric(0)
                        h = _win32_get_metric(1)
                        target = (w // 2, h // 2)
                        mouse.position = target
                        _win32_set_cursor(target[0], target[1])
                    else:
                        mouse.position = (960, 540)
                except Exception as e:
                    logger.warning("Could not center cursor: %s", e)
            elif t == "scroll":
                _do_scroll(msg.get("dy", 0))
            elif t == "click":
                _do_click(msg.get("button", "left"))
            elif t == "mouse_down":
                b_name = msg.get("button", "left")
                _do_mouse_down(b_name)
                held_buttons.add(b_name)
            elif t == "mouse_up":
                b_name = msg.get("button", "left")
                _do_mouse_up(b_name)
                held_buttons.discard(b_name)
            elif t == "type":
                _do_type(msg.get("text", ""), msg.get("mods", []))
            elif t == "key":
                _do_key(msg.get("key"), msg.get("mods", []))
            elif t == "media":
                media_obj = MEDIA_KEYS.get(msg.get("action"))
                if media_obj:
                    keyboard.tap(media_obj)
            elif t == "show_qr" and _qr_callback is not None:
                _qr_callback()
            elif t == "ping_probe":
                # Clock sync probe: echo immediately with server perf_counter_ns
                await ws.send_json({
                    "type": "pong_probe",
                    "id": msg.get("id"),
                    "srv_ns": time.perf_counter_ns(),
                })
                continue

            # Record inject timestamp after OS call completes
            if _do_prof and _seq >= 0:
                _prof_record(_seq, t, _recv_ns, time.perf_counter_ns())
    except WebSocketDisconnect:
        pass
    finally:
        for btn in list(held_buttons):
            _do_mouse_up(btn)