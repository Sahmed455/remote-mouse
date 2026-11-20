import argparse
import threading

import uvicorn

from remote_mouse.config import (
    CERT_FILE,
    HOST,
    KEY_FILE,
    PORT,
    get_local_ip,
    is_packaged_msix,
    logger,
    set_windows_startup,
)
from remote_mouse.security import (
    TRUSTED_LOCK,
    ensure_tls_cert,
    load_trusted_devices,
    save_trusted_devices,
)
from remote_mouse.server import BackgroundServer, app, set_qr_callback
from remote_mouse.tray import TrayApp
from remote_mouse.usb_tunnel import usb_adb_watcher


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
        if is_packaged_msix():
            print("Managed via Windows Settings -> Apps -> Startup for MSIX packages.")
        else:
            ok = set_windows_startup(True)
            print("Windows startup enabled." if ok else "Failed to enable Windows startup.")
        return True, False

    if args.remove_startup:
        if is_packaged_msix():
            print("Managed via Windows Settings -> Apps -> Startup for MSIX packages.")
        else:
            ok = set_windows_startup(False)
            print("Windows startup disabled." if ok else "Failed to disable Windows startup.")
        return True, False

    return False, args.show


def main() -> None:
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
    set_qr_callback(tray_app.request_show_popup)
    try:
        tray_app.run(show_on_start=show_on_start)
    finally:
        set_qr_callback(None)
        usb_stop.set()