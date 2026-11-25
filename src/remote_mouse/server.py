import asyncio
import atexit
import base64
import csv
import json
import queue
import secrets
import socket
import struct
import sys
import threading
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

def _import_aiortc():
    """Import aiortc, tolerating PyAV native DLL blocks (e.g. Windows Smart App Control)
    when only SCTP/DTLS WebRTC DataChannels are used without audio/video tracks."""
    try:
        from aiortc import RTCPeerConnection, RTCSessionDescription
        from aiortc.sdp import candidate_from_sdp
        return RTCPeerConnection, RTCSessionDescription, candidate_from_sdp
    except ImportError:
        import types
        # Clean up any partial failed imports before installing data-only PyAV stub
        for k in [m for m in sys.modules if m == "av" or m.startswith("av.") or m == "aiortc" or m.startswith("aiortc.")]:
            sys.modules.pop(k, None)

        class _StubAVType:
            pass

        def _make_stub_mod(name: str, attrs: dict | None = None):
            mod = types.ModuleType(name)
            for ak, av_val in (attrs or {}).items():
                setattr(mod, ak, av_val)
            sys.modules[name] = mod
            return mod

        av_mod = _make_stub_mod("av", {
            "AudioFrame": _StubAVType,
            "VideoFrame": _StubAVType,
            "AudioResampler": _StubAVType,
            "CodecContext": _StubAVType,
        })
        av_mod.frame = _make_stub_mod("av.frame", {"Frame": _StubAVType})
        av_mod.packet = _make_stub_mod("av.packet", {"Packet": _StubAVType})
        av_mod.container = _make_stub_mod("av.container")
        av_mod.audio = _make_stub_mod("av.audio", {"AudioStream": _StubAVType})
        av_mod.video = _make_stub_mod("av.video")
        av_mod.video.codeccontext = _make_stub_mod("av.video.codeccontext", {"VideoCodecContext": _StubAVType})
        av_mod.video.stream = _make_stub_mod("av.video.stream", {"VideoStream": _StubAVType})

        from aiortc import RTCPeerConnection, RTCSessionDescription
        from aiortc.sdp import candidate_from_sdp
        return RTCPeerConnection, RTCSessionDescription, candidate_from_sdp


try:
    RTCPeerConnection, RTCSessionDescription, candidate_from_sdp = _import_aiortc()
    _HAS_WEBRTC = True
except Exception:
    _HAS_WEBRTC = False

from remote_mouse.config import PORT, WEB_INDEX_FILE, get_local_ip, is_admin, logger

# ---------------------------------------------------------------------------
# Latency Profiler — zero overhead when disabled
# ---------------------------------------------------------------------------
_profiling: bool = False          # toggled by /api/profile/toggle
_server_log: deque = deque(maxlen=50_000)   # {seq, t, recv_ns, inject_ns}
_profile_start_wall: float = 0.0  # wall-clock epoch at profile start (for report header)
_MOVE_EVENT_TYPES = frozenset(("mouse_move", "air_move", "move"))


def _prof_record(seq: int, evt_type: str, recv_ns: int, inject_ns: int) -> None:
    """Append one server-side timing entry. Called only when _profiling is True."""
    _server_log.append({
        "seq": seq, "t": evt_type,
        "recv_ns": recv_ns, "inject_ns": inject_ns,
    })


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile over a pre-sorted non-empty list."""
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def _calc_stats(values: list[float], digits: int = 3) -> dict[str, float | int]:
    """Compute count, p50, p95, p99, and max for a metric series."""
    if not values:
        return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    s = sorted(values)
    return {
        "count": len(s),
        "p50": round(_percentile(s, 0.50), digits),
        "p95": round(_percentile(s, 0.95), digits),
        "p99": round(_percentile(s, 0.99), digits),
        "max": round(s[-1], digits),
    }
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
async def profile_toggle(enable: bool | None = None):
    """Enable or disable latency profiling. Returns new state."""
    global _profiling, _profile_start_wall
    _profiling = (not _profiling) if enable is None else enable
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
    Returns a summary and the paths to the saved CSV and JSON summary.
    """
    client_log = payload.get("data", [])      # [{seq, t, cts_ms, coalesced_count?}, ...]
    clock_offset_ms = float(payload.get("clock_offset_ms", 0.0))  # best-RTT NTP-style offset

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
    srv_by_seq = {e["seq"]: e for e in srv_entries if e.get("seq", -1) >= 0}
    matched_pairs: list[tuple[dict, dict, float]] = []
    for ce in client_log:
        seq = ce.get("seq")
        se = srv_by_seq.get(seq)
        if se is None:
            continue
        raw_offset_ms = (se["recv_ns"] / 1_000_000.0) - float(ce.get("cts_ms", 0.0))
        matched_pairs.append((ce, se, raw_offset_ms))

    csv_path = report_dir / f"profile_report_{ts_tag}.csv"
    summary_path = report_dir / f"profile_summary_{ts_tag}.json"
    matched = len(matched_pairs)
    rows = []

    event_counts: dict[str, int] = {}
    jitter_vals: list[float] = []
    inject_vals: list[float] = []
    move_recv_dts: list[float] = []

    if matched_pairs:
        min_raw_offset_ms = min(p[2] for p in matched_pairs)
        prev_move_cts_ms: float | None = None
        prev_move_srv_recv_ms: float | None = None

        for ce, se, raw_offset_ms in matched_pairs:
            evt = ce.get("t", "?")
            event_counts[evt] = event_counts.get(evt, 0) + 1

            recv_ns = se["recv_ns"]
            inject_ns = se["inject_ns"]
            cts_ms = float(ce.get("cts_ms", 0.0))
            srv_recv_ms = recv_ns / 1_000_000.0
            srv_inject_ms = inject_ns / 1_000_000.0

            est_one_way_ms = srv_recv_ms - (cts_ms + clock_offset_ms)
            rel_transport_jitter_ms = raw_offset_ms - min_raw_offset_ms
            inject_overhead_ms = (inject_ns - recv_ns) / 1_000_000.0
            total_rel_ms = rel_transport_jitter_ms + inject_overhead_ms

            jitter_vals.append(rel_transport_jitter_ms)
            inject_vals.append(inject_overhead_ms)

            is_move = evt in _MOVE_EVENT_TYPES
            if is_move and prev_move_cts_ms is not None and prev_move_srv_recv_ms is not None:
                c_dt = cts_ms - prev_move_cts_ms
                s_dt = srv_recv_ms - prev_move_srv_recv_ms
                b_gap = s_dt - c_dt
                client_dt_out: float | str = round(c_dt, 3)
                srv_dt_out: float | str = round(s_dt, 3)
                burst_gap_out: float | str = round(b_gap, 3)
                move_recv_dts.append(s_dt)
            else:
                client_dt_out = ""
                srv_dt_out = ""
                burst_gap_out = ""

            if is_move:
                prev_move_cts_ms = cts_ms
                prev_move_srv_recv_ms = srv_recv_ms

            coalesced = ce.get("coalesced_count", 1 if is_move else "")
            rows.append({
                "seq": ce.get("seq"),
                "event": evt,
                "coalesced_count": coalesced,
                "client_send_ms": round(cts_ms, 3),
                "srv_recv_ms": round(srv_recv_ms, 3),
                "srv_inject_ms": round(srv_inject_ms, 3),
                "est_one_way_ms": round(est_one_way_ms, 3),
                "rel_transport_jitter_ms": round(rel_transport_jitter_ms, 3),
                "inject_overhead_ms": round(inject_overhead_ms, 3),
                "total_rel_ms": round(total_rel_ms, 3),
                "client_dt_ms": client_dt_out,
                "server_recv_dt_ms": srv_dt_out,
                "burst_gap_ms": burst_gap_out,
            })

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "seq", "event", "coalesced_count",
            "client_send_ms", "srv_recv_ms", "srv_inject_ms",
            "est_one_way_ms", "rel_transport_jitter_ms", "inject_overhead_ms", "total_rel_ms",
            "client_dt_ms", "server_recv_dt_ms", "burst_gap_ms",
        ])
        writer.writeheader()
        writer.writerows(rows)

    if rows:
        jitter_stats = _calc_stats(jitter_vals, digits=3)
        inject_stats = _calc_stats(inject_vals, digits=4)
        recv_dt_stats = _calc_stats(move_recv_dts, digits=3)
        gaps_over_25ms = sum(1 for dt in move_recv_dts if dt > 25.0)
        summary = {
            "matched_events": matched,
            "clock_offset_ms": round(clock_offset_ms, 3),
            "event_counts": event_counts,
            "rel_transport_jitter_ms": jitter_stats,
            "inject_overhead_ms": inject_stats,
            "server_recv_dt_ms": recv_dt_stats,
            "gaps_over_25ms": gaps_over_25ms,
            "report_csv": str(csv_path),
            "report_summary": str(summary_path),
        }
        summary_text = (
            f"=== Latency Profile Summary ({ts_tag}) ===\n"
            f"Matched: {matched} | Clock offset: {clock_offset_ms:.3f} ms | Counts: {event_counts}\n"
            f"rel_transport_jitter_ms : p50={jitter_stats['p50']} p95={jitter_stats['p95']} "
            f"p99={jitter_stats['p99']} max={jitter_stats['max']}\n"
            f"inject_overhead_ms      : p50={inject_stats['p50']} p95={inject_stats['p95']} "
            f"p99={inject_stats['p99']} max={inject_stats['max']}\n"
            f"server_recv_dt_ms       : p50={recv_dt_stats['p50']} p95={recv_dt_stats['p95']} "
            f"p99={recv_dt_stats['p99']} max={recv_dt_stats['max']} (gaps >25ms: {gaps_over_25ms})\n"
            f"CSV: {csv_path}"
        )
        if sys.stdout is not None:
            print(summary_text, flush=True)
        logger.info("\n%s", summary_text)
    else:
        summary = {
            "matched_events": 0,
            "clock_offset_ms": round(clock_offset_ms, 3),
            "event_counts": {},
            "note": "No matching seq numbers found",
            "report_csv": str(csv_path),
            "report_summary": str(summary_path),
        }
        logger.info("Profile report written: %s (0 matched events)", csv_path)

    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    return JSONResponse(summary)

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    # Dedicated DLL instances with use_last_error=True so pynput's argtypes
    # mutations on ctypes.windll.user32 (e.g. VkKeyScanW.argtypes = [c_wchar])
    # cannot collide with our calls.
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _winmm = ctypes.WinDLL("winmm")

    _THREAD_PRIORITY_HIGHEST = 2
    try:
        _winmm.timeBeginPeriod(1)
        atexit.register(_winmm.timeEndPeriod, 1)
    except Exception:
        pass

    def _boost_thread_priority() -> None:
        try:
            _kernel32.SetThreadPriority(_kernel32.GetCurrentThread(), _THREAD_PRIORITY_HIGHEST)
        except Exception:
            pass

    _win32_get_metric = _user32.GetSystemMetrics
    _win32_set_cursor = _user32.SetCursorPos
    _win32_vk_key_scan = _user32.VkKeyScanW
    _win32_vk_key_scan.argtypes = (wintypes.WCHAR,)
    _win32_vk_key_scan.restype = ctypes.c_short
    _win32_map_vk = _user32.MapVirtualKeyW
    _win32_map_vk.argtypes = (wintypes.UINT, wintypes.UINT)
    _win32_map_vk.restype = wintypes.UINT

    # Win32 Mouse Event Flags
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_MIDDLEDOWN = 0x0020
    MOUSEEVENTF_MIDDLEUP = 0x0040
    MOUSEEVENTF_WHEEL = 0x0800

    # Win32 Keyboard Event Flags
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    MAPVK_VK_TO_VSC = 0

    # Win32 SendInput structs (full union -> sizeof(INPUT) == 40 on x64, 28 on x86)
    _INPUT_MOUSE = 0
    _INPUT_KEYBOARD = 1
    _ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        ]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        ]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("mi", _MOUSEINPUT),
            ("ki", _KEYBDINPUT),
            ("hi", _HARDWAREINPUT),
        ]

    class _INPUT(ctypes.Structure):
        _fields_ = [
            ("type", wintypes.DWORD),
            ("union", _INPUT_UNION),
        ]

    _INPUT_SIZE = ctypes.sizeof(_INPUT)
    _win32_send_input = _user32.SendInput
    _win32_send_input.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
    _win32_send_input.restype = wintypes.UINT

    def _send_inputs(arr, count: int) -> int:
        sent = _win32_send_input(count, arr, _INPUT_SIZE)
        if sent == 0:
            err = ctypes.get_last_error()
            logger.warning("SendInput returned 0 (count=%d, last_error=%d)", count, err)
        return sent

    def _make_mouse_input(flag: int) -> _INPUT:
        inp = _INPUT()
        inp.type = _INPUT_MOUSE
        inp.union.mi.dwFlags = flag
        return inp

    def _make_click_batch(down_flag: int, up_flag: int):
        arr = (_INPUT * 2)()
        arr[0].type = _INPUT_MOUSE
        arr[0].union.mi.dwFlags = down_flag
        arr[1].type = _INPUT_MOUSE
        arr[1].union.mi.dwFlags = up_flag
        return arr

    _CLICK_BATCHES = {
        "left": _make_click_batch(MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": _make_click_batch(MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": _make_click_batch(MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }
    _CLICK_LEFT = _CLICK_BATCHES["left"]

    _DOWN_INPUTS = {
        "left": _make_mouse_input(MOUSEEVENTF_LEFTDOWN),
        "right": _make_mouse_input(MOUSEEVENTF_RIGHTDOWN),
        "middle": _make_mouse_input(MOUSEEVENTF_MIDDLEDOWN),
    }
    _DOWN_LEFT = _DOWN_INPUTS["left"]

    _UP_INPUTS = {
        "left": _make_mouse_input(MOUSEEVENTF_LEFTUP),
        "right": _make_mouse_input(MOUSEEVENTF_RIGHTUP),
        "middle": _make_mouse_input(MOUSEEVENTF_MIDDLEUP),
    }
    _UP_LEFT = _UP_INPUTS["left"]

    # Pre-allocated single structs reused exclusively by the single-threaded input worker
    _MOVE_INPUT = _make_mouse_input(MOUSEEVENTF_MOVE)
    _WHEEL_INPUT = _make_mouse_input(MOUSEEVENTF_WHEEL)

    SPECIAL_VK = {
        "backspace": 0x08,
        "tab": 0x09,
        "enter": 0x0D,
        "return": 0x0D,
        "esc": 0x1B,
        "escape": 0x1B,
        "space": 0x20,
        "page_up": 0x21,
        "page_down": 0x22,
        "end": 0x23,
        "home": 0x24,
        "left": 0x25,
        "up": 0x26,
        "right": 0x27,
        "down": 0x28,
        "print_screen": 0x2C,
        "insert": 0x2D,
        "delete": 0x2E,
        "del": 0x2E,
        "win": 0x5B,
        "lwin": 0x5B,
        "rwin": 0x5C,
        "ctrl": 0x11,
        "alt": 0x12,
        "shift": 0x10,
        "caps_lock": 0x14,
        **{f"f{i}": 0x6F + i for i in range(1, 13)},
    }

    MOD_VK = {
        "ctrl": 0x11,
        "alt": 0x12,
        "shift": 0x10,
        "win": 0x5B,
    }

    EXTENDED_VKS = {
        0x21, 0x22, 0x23, 0x24,  # PageUp, PageDown, End, Home
        0x25, 0x26, 0x27, 0x28,  # Left, Up, Right, Down
        0x2C, 0x2D, 0x2E,        # PrintScreen, Insert, Delete
        0x5B, 0x5C,              # LWin, RWin
        0xA3, 0xA5,              # RCtrl, RAlt
    }

    def _fast_relative_move(dx: int, dy: int) -> None:
        _MOVE_INPUT.union.mi.dx = dx
        _MOVE_INPUT.union.mi.dy = dy
        _send_inputs(ctypes.byref(_MOVE_INPUT), 1)

    def _do_click(btn_name: str) -> None:
        batch = _CLICK_BATCHES.get(btn_name, _CLICK_LEFT)
        _send_inputs(batch, 2)

    def _do_mouse_down(btn_name: str) -> None:
        inp = _DOWN_INPUTS.get(btn_name, _DOWN_LEFT)
        _send_inputs(ctypes.byref(inp), 1)

    def _do_mouse_up(btn_name: str) -> None:
        inp = _UP_INPUTS.get(btn_name, _UP_LEFT)
        _send_inputs(ctypes.byref(inp), 1)

    def _do_scroll(dy: float) -> None:
        wheel_delta = int(dy * 120)
        if wheel_delta:
            _WHEEL_INPUT.union.mi.mouseData = wheel_delta & 0xFFFFFFFF
            _send_inputs(ctypes.byref(_WHEEL_INPUT), 1)

    def _fill_vk_input(inp: _INPUT, vk: int, is_up: bool = False) -> None:
        inp.type = _INPUT_KEYBOARD
        inp.union.ki.wVk = vk
        inp.union.ki.wScan = _win32_map_vk(vk, MAPVK_VK_TO_VSC) & 0xFFFF
        flags = KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VKS else 0
        if is_up:
            flags |= KEYEVENTF_KEYUP
        inp.union.ki.dwFlags = flags

    def _send_vk_combo(vk: int, mods: list[str] | None = None) -> None:
        mod_vks = [MOD_VK[m] for m in (mods or ()) if m in MOD_VK]
        total = (len(mod_vks) * 2) + 2
        arr = (_INPUT * total)()
        idx = 0
        for mvk in mod_vks:
            _fill_vk_input(arr[idx], mvk, is_up=False)
            idx += 1
        _fill_vk_input(arr[idx], vk, is_up=False)
        idx += 1
        _fill_vk_input(arr[idx], vk, is_up=True)
        idx += 1
        for mvk in reversed(mod_vks):
            _fill_vk_input(arr[idx], mvk, is_up=True)
            idx += 1
        _send_inputs(arr, total)

    def _do_type(text: str, mods: list[str] | None = None) -> None:
        if not text:
            return
        mods = mods or []
        if mods and len(text) == 1:
            res = _win32_vk_key_scan(text.lower())
            if res != -1:
                vk = res & 0xFF
                extra_mods = list(mods)
                if ((res >> 8) & 1) and "shift" not in extra_mods:
                    extra_mods.append("shift")
                _send_vk_combo(vk, extra_mods)
                return

        # Convert string to UTF-16 code units for SendInput KEYEVENTF_UNICODE
        units: list[tuple[bool, int]] = []  # (is_vk, value)
        utf16 = text.encode("utf-16-le")
        for i in range(0, len(utf16), 2):
            code = int.from_bytes(utf16[i:i + 2], "little")
            if code == 0x000A:  # newline -> VK_RETURN
                units.append((True, 0x0D))
            elif code == 0x0009:  # tab -> VK_TAB
                units.append((True, 0x09))
            else:
                units.append((False, code))

        total = len(units) * 2
        if not total:
            return
        arr = (_INPUT * total)()
        idx = 0
        for is_vk, val in units:
            if is_vk:
                _fill_vk_input(arr[idx], val, is_up=False)
                _fill_vk_input(arr[idx + 1], val, is_up=True)
            else:
                arr[idx].type = _INPUT_KEYBOARD
                arr[idx].union.ki.wVk = 0
                arr[idx].union.ki.wScan = val
                arr[idx].union.ki.dwFlags = KEYEVENTF_UNICODE

                arr[idx + 1].type = _INPUT_KEYBOARD
                arr[idx + 1].union.ki.wVk = 0
                arr[idx + 1].union.ki.wScan = val
                arr[idx + 1].union.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
            idx += 2
        _send_inputs(arr, total)

    def _do_key(key_name: str, mods: list[str] | None = None) -> None:
        if not key_name:
            return
        mods = mods or []
        norm = key_name.lower()
        vk = SPECIAL_VK.get(norm)
        if vk is not None:
            _send_vk_combo(vk, mods)
            return

        if len(key_name) == 1:
            res = _win32_vk_key_scan(key_name)
            if res != -1:
                vk = res & 0xFF
                shift_state = (res >> 8) & 0xFF
                effective_mods = list(mods)
                if (shift_state & 1) and "shift" not in effective_mods:
                    effective_mods.append("shift")
                if (shift_state & 2) and "ctrl" not in effective_mods:
                    effective_mods.append("ctrl")
                if (shift_state & 4) and "alt" not in effective_mods:
                    effective_mods.append("alt")
                _send_vk_combo(vk, effective_mods)
                return
            _do_type(key_name, mods)
            return

        key_obj = SPECIAL_KEYS.get(norm)
        if key_obj:
            _press_with_mods(mods, lambda: keyboard.tap(key_obj))
else:
    def _boost_thread_priority() -> None:
        pass
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


# ---------------------------------------------------------------------------
# Dedicated High-Priority Input Worker (keeps asyncio receive loop non-blocking)
# ---------------------------------------------------------------------------
_input_queue: queue.SimpleQueue = queue.SimpleQueue()


def _input_worker_loop() -> None:
    _boost_thread_priority()
    while True:
        op, arg, seq, evt_name, recv_ns = _input_queue.get()
        try:
            if op == "move":
                _fast_relative_move(arg[0], arg[1])
            elif op == "click":
                _do_click(arg)
            elif op == "down":
                _do_mouse_down(arg)
            elif op == "up":
                _do_mouse_up(arg)
            elif op == "scroll":
                _do_scroll(arg)
            elif op == "type":
                _do_type(arg[0], arg[1])
            elif op == "key":
                _do_key(arg[0], arg[1])
            elif op == "media":
                media_obj = MEDIA_KEYS.get(arg)
                if media_obj:
                    keyboard.tap(media_obj)
        except Exception as e:
            logger.error("Input worker error on %s: %s", op, e)
        if recv_ns and seq >= 0:
            _prof_record(seq, evt_name, recv_ns, time.perf_counter_ns())


_input_worker_thread = threading.Thread(target=_input_worker_loop, daemon=True, name="InputInjector")
_input_worker_thread.start()


def _enqueue_input(op: str, arg, seq: int = -1, evt_name: str = "", recv_ns: int = 0) -> None:
    _input_queue.put((op, arg, seq, evt_name, recv_ns))


def _dispatch_binary_motion(data_bytes: bytes | bytearray | memoryview) -> None:
    b0 = data_bytes[0]
    if b0 == 0x01 and len(data_bytes) == 5:
        dx, dy = struct.unpack_from("<hh", data_bytes, 1)
        if dx or dy:
            if _profiling:
                _enqueue_input("move", (dx, dy), -1, "mouse_move", time.perf_counter_ns())
            else:
                _enqueue_input("move", (dx, dy))
    elif (b0 == 0x03 or b0 == 0x04) and len(data_bytes) == 9:
        _recv_ns = time.perf_counter_ns() if _profiling else 0
        dx, dy, _seq = struct.unpack_from("<hhI", data_bytes, 1)
        if dx or dy:
            evt = "mouse_move" if b0 == 0x03 else "air_move"
            if _profiling:
                _enqueue_input("move", (dx, dy), _seq, evt, _recv_ns)
            else:
                _enqueue_input("move", (dx, dy))
    elif b0 == 0x02 and len(data_bytes) == 5:
        dy = struct.unpack_from("<f", data_bytes, 1)[0]
        _enqueue_input("scroll", dy)


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
    _boost_thread_priority()
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

    held_buttons: set[str] = set()
    rtc_pc = None
    try:
        while True:
            raw = await ws.receive()
            raw_type = raw.get("type")
            if raw_type == "websocket.disconnect":
                break

            # Fast-path for binary motion packets (fallback when WebRTC DataChannel is not active)
            data_bytes = raw.get("bytes")
            if data_bytes:
                _dispatch_binary_motion(data_bytes)
                continue

            text = raw.get("text")
            if not text:
                continue

            msg = json.loads(text)
            t = msg.get("type") or msg.get("t")

            if t == "prof_ping":
                # Clock sync handshake: reply immediately with server perf_counter_ns
                await ws.send_json({
                    "t": "prof_pong",
                    "type": "prof_pong",
                    "t0": msg.get("t0"),
                    "srv_ns": time.perf_counter_ns(),
                })
                continue

            if t == "rtc_offer":
                if not _HAS_WEBRTC:
                    await ws.send_json({"type": "rtc_unavailable"})
                    continue
                try:
                    if rtc_pc is not None:
                        await rtc_pc.close()
                    rtc_pc = RTCPeerConnection()

                    @rtc_pc.on("datachannel")
                    def _on_datachannel(channel):
                        logger.info("WebRTC DataChannel opened: %s (ordered=%s)", channel.label, channel.ordered)

                        @channel.on("message")
                        def _on_message(message):
                            if isinstance(message, (bytes, bytearray, memoryview)) and len(message) > 0:
                                _dispatch_binary_motion(message)

                    @rtc_pc.on("connectionstatechange")
                    async def _on_conn_state():
                        if rtc_pc is not None:
                            logger.info("WebRTC connection state: %s", rtc_pc.connectionState)

                    offer = RTCSessionDescription(sdp=msg["sdp"], type=msg.get("sdp_type", "offer"))
                    await rtc_pc.setRemoteDescription(offer)
                    answer = await rtc_pc.createAnswer()
                    await rtc_pc.setLocalDescription(answer)
                    await ws.send_json({
                        "type": "rtc_answer",
                        "sdp": rtc_pc.localDescription.sdp,
                        "sdp_type": rtc_pc.localDescription.type,
                    })
                except Exception as e:
                    logger.warning("WebRTC offer negotiation failed (using WebSocket fallback): %s", e)
                    if rtc_pc is not None:
                        try:
                            await rtc_pc.close()
                        except Exception:
                            pass
                        rtc_pc = None
                continue

            if t == "rtc_ice":
                cand_str = msg.get("candidate") or ""
                if _HAS_WEBRTC and rtc_pc is not None and cand_str:
                    try:
                        cand = candidate_from_sdp(cand_str.removeprefix("candidate:"))
                        cand.sdpMid = msg.get("sdpMid")
                        cand.sdpMLineIndex = msg.get("sdpMLineIndex")
                        await rtc_pc.addIceCandidate(cand)
                    except Exception as e:
                        logger.debug("Ignoring ICE candidate (%s): %s", cand_str, e)
                continue

            # Latency profiling — record recv timestamp before dispatch, inject timestamp on completion.
            # Branch is completely dead when _profiling is False (single bool check per JSON msg).
            _do_prof = _profiling
            _recv_ns = time.perf_counter_ns() if _do_prof else 0
            _seq = msg.get("seq", -1) if _do_prof else -1

            if t == "move":
                dx = int(msg.get("dx", 0))
                dy = int(msg.get("dy", 0))
                if dx or dy:
                    _enqueue_input("move", (dx, dy), _seq, t, _recv_ns)
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
                if _do_prof and _seq >= 0:
                    _prof_record(_seq, t, _recv_ns, time.perf_counter_ns())
            elif t == "scroll":
                _enqueue_input("scroll", msg.get("dy", 0), _seq, t, _recv_ns)
            elif t == "click":
                _enqueue_input("click", msg.get("button", "left"), _seq, t, _recv_ns)
            elif t == "mouse_down":
                b_name = msg.get("button", "left")
                held_buttons.add(b_name)
                _enqueue_input("down", b_name, _seq, t, _recv_ns)
            elif t == "mouse_up":
                b_name = msg.get("button", "left")
                held_buttons.discard(b_name)
                _enqueue_input("up", b_name, _seq, t, _recv_ns)
            elif t == "type":
                _enqueue_input("type", (msg.get("text", ""), msg.get("mods", [])), _seq, t, _recv_ns)
            elif t == "key":
                _enqueue_input("key", (msg.get("key"), msg.get("mods", [])), _seq, t, _recv_ns)
            elif t == "media":
                _enqueue_input("media", msg.get("action"), _seq, t, _recv_ns)
            elif t == "show_qr" and _qr_callback is not None:
                _qr_callback()
                if _do_prof and _seq >= 0:
                    _prof_record(_seq, t, _recv_ns, time.perf_counter_ns())
    except WebSocketDisconnect:
        pass
    finally:
        if rtc_pc is not None:
            try:
                await rtc_pc.close()
            except Exception:
                pass
        for btn in list(held_buttons):
            _enqueue_input("up", btn)