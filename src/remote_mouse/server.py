import asyncio
import base64
import secrets
from collections.abc import Callable
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pynput.keyboard import Controller as KeyboardController
from pynput.keyboard import Key
from pynput.mouse import Button
from pynput.mouse import Controller as MouseController

from remote_mouse.config import PORT, WEB_INDEX_FILE, get_local_ip, logger
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

        await ws.send_json({"type": "auth_ok", "device_id": device_id})
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
    await ws.send_json({"type": "auth_ok", "device_id": device_id, "paired": True})
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
                b_name = msg.get("button")
                btn = Button.right if b_name == "right" else (Button.middle if b_name == "middle" else Button.left)
                mouse.click(btn, 1)
            elif t == "mouse_down":
                b_name = msg.get("button")
                btn = Button.right if b_name == "right" else (Button.middle if b_name == "middle" else Button.left)
                mouse.press(btn)
                held_buttons.add(btn)
            elif t == "mouse_up":
                b_name = msg.get("button")
                btn = Button.right if b_name == "right" else (Button.middle if b_name == "middle" else Button.left)
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
            elif t == "show_qr" and _qr_callback is not None:
                _qr_callback()
    except WebSocketDisconnect:
        pass
    finally:
        for btn in list(held_buttons):
            mouse.release(btn)