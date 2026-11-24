import os
import sys
import threading
import tkinter as tk
from typing import TYPE_CHECKING

import pystray
import qrcode
from PIL import Image, ImageDraw, ImageTk

from remote_mouse.config import (
    APP_NAME,
    DATA_DIR,
    PORT,
    get_local_ip,
    is_admin,
    is_packaged_msix,
    is_startup_enabled,
    relaunch_as_admin,
    set_windows_startup,
)
from remote_mouse.security import (
    TRUSTED_LOCK,
    get_pairing_pin,
    load_trusted_devices,
    rotate_pairing_pin,
)

if TYPE_CHECKING:
    from remote_mouse.server import BackgroundServer


def create_tray_icon_image() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(37, 99, 235, 255))
    draw.rounded_rectangle((18, 12, 46, 52), radius=12, fill=(240, 244, 248, 255))
    draw.line((32, 14, 32, 28), fill=(37, 99, 235, 255), width=3)
    return img


class TrayApp:
    def __init__(self, server: "BackgroundServer", usb_stop: threading.Event):
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

        if is_packaged_msix():
            startup_item = pystray.MenuItem("Startup Settings (Windows)", self._on_open_startup_settings)
        else:
            startup_item = pystray.MenuItem(
                "Start with Windows",
                self._on_toggle_startup,
                checked=lambda _item: is_startup_enabled(),
            )

        if is_admin():
            admin_item = pystray.MenuItem("🛡️ Running as Administrator", lambda _icon, _item: None, enabled=False)
        else:
            admin_item = pystray.MenuItem("🛡️ Run as Administrator", self._on_relaunch_admin)

        self.icon = pystray.Icon(
            APP_NAME,
            create_tray_icon_image(),
            "DeviLX KeyWave (HTTPS + ECDSA)" + (" [Admin]" if is_admin() else ""),
            menu=pystray.Menu(
                pystray.MenuItem("Show QR Code & PIN", self._on_show, default=True),
                pystray.MenuItem("Regenerate Pairing PIN", self._on_new_pin),
                pystray.MenuItem("Open Data Folder", self._on_open_data_dir),
                pystray.Menu.SEPARATOR,
                admin_item,
                startup_item,
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._on_quit),
            ),
        )

    def refresh_popup(self) -> None:
        ip = get_local_ip()
        pin = get_pairing_pin()
        base_url = f"https://{ip}:{PORT}"
        qr_url = f"{base_url}/?pin={pin}"

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
        self.pin_var.set(f"Pairing PIN: {pin}")
        admin_tag = " · 🛡️ Admin" if is_admin() else " · ⚠️ Standard User"
        if ip == "127.0.0.1":
            self.info_var.set(f"No Wi-Fi/Hotspot yet — USB active{admin_tag} | Refresh IP once connected")
        else:
            self.info_var.set(f"Encrypted WSS + ECDSA P-256{admin_tag}  |  Trusted: {trusted_count}")

    def show_popup(self) -> None:
        self.refresh_popup()
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(200, lambda: self.root.attributes("-topmost", False))

    def request_show_popup(self) -> None:
        """Thread-safe trigger called from the FastAPI WebSocket loop."""
        self.root.after(0, self.show_popup)

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

    def _on_open_startup_settings(self, _icon, _item) -> None:
        if sys.platform == "win32":
            os.startfile("ms-settings:startupapps")

    def _on_open_data_dir(self, _icon, _item) -> None:
        if sys.platform == "win32":
            os.startfile(str(DATA_DIR))

    def _on_relaunch_admin(self, _icon, _item) -> None:
        if relaunch_as_admin():
            self._on_quit(_icon, _item)

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