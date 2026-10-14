"""Inject WFD touch into the EVDI output only.

A uinput device named "Segunda Tela Touch" is an absolute touchscreen
(INPUT_PROP_DIRECT, BTN_TOUCH, ABS_MT_*). Its range is the EVDI mode in
pixels. Finger contacts use MT_TOOL_FINGER; an S Pen uses MT_TOOL_PEN.

COSMIC maps a touchscreen to one output with
`~/.config/cosmic/com.system76.CosmicComp/v1/input_devices`
(`state: Enabled`, `map_to_output: Some("<connector>")`). The udev rule
marks the device ID_INPUT_TOUCHSCREEN and sets an identity calibration,
so libinput uses these pixels unchanged. Coordinates stay in that
output's space: the compositor places them on 'Segunda Tela' and nowhere
else. Scaling from the negotiated stream (1280x720) onto the EVDI mode
(1920x1080 or 1920x1200) happens before the events are written.
"""
from __future__ import annotations

import logging
import os
import re
import struct
import threading
from pathlib import Path
from typing import Optional

from ..wfd.uibc import Pointer, TouchEvent

log = logging.getLogger(__name__)

DEVICE_NAME = "Segunda Tela Touch"
COSMIC_INPUT_CONFIG = Path(".config/cosmic/com.system76.CosmicComp/v1/input_devices")

# linux/input-event-codes.h
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
SYN_REPORT = 0
BTN_TOOL_PEN = 0x140
BTN_TOOL_FINGER = 0x145
BTN_TOUCH = 0x14A
ABS_X, ABS_Y = 0x00, 0x01
ABS_PRESSURE = 0x18
ABS_MT_SLOT = 0x2F
ABS_MT_TOUCH_MAJOR = 0x30
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TOOL_TYPE = 0x37
ABS_MT_TRACKING_ID = 0x39
ABS_MT_PRESSURE = 0x3A
MT_TOOL_FINGER, MT_TOOL_PEN = 0, 1
INPUT_PROP_DIRECT = 0x01
BUS_VIRTUAL = 0x06

# linux/uinput.h ioctl numbers (_IOC on a 32/64-bit Linux).
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_DEV_SETUP = 0x405C5503  # _IOW('U', 3, struct uinput_setup) — 92 bytes
UI_ABS_SETUP = 0x401C5504  # _IOW('U', 4, struct uinput_abs_setup) — 28 bytes
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_ABSBIT = 0x40045567
UI_SET_PROPBIT = 0x4004556E

UINPUT_SETUP_SIZE = 92
UINPUT_ABS_SETUP_SIZE = 28
MAX_SLOTS = 10


def scale_point(x: int, y: int, src_w: int, src_h: int, dst_w: int, dst_h: int) -> tuple[int, int]:
    """Stream pixel → EVDI output pixel. Endpoints stay on the output."""
    if src_w <= 0 or src_h <= 0 or dst_w <= 0 or dst_h <= 0:
        return 0, 0
    x = max(0, min(src_w - 1, x))
    y = max(0, min(src_h - 1, y))
    return int(x * dst_w / src_w), int(y * dst_h / src_h)


def to_global(x: int, y: int, origin_x: int, origin_y: int) -> tuple[int, int]:
    """Output-local pixel → desktop coordinate. Used when no per-output map exists."""
    return x + origin_x, y + origin_y


def pack_input_event(ev_type: int, code: int, value: int) -> bytes:
    """One `struct input_event` (64-bit timeval, so 24 bytes)."""
    return struct.pack("qqHHi", 0, 0, ev_type, code, value)


def uinput_setup_bytes(name: str, vendor: int = 0x5347, product: int = 0x0001) -> bytes:
    raw = name.encode("utf-8")[:79]
    ident = struct.pack("<HHHH", BUS_VIRTUAL, vendor, product, 1)
    return ident + raw + b"\x00" * (80 - len(raw)) + struct.pack("<I", 0)


def uinput_abs_setup(code: int, minimum: int, maximum: int, resolution: int = 0) -> bytes:
    return struct.pack("<HHiiiiii", code, 0, 0, minimum, maximum, 0, 0, resolution)


class ContactTracker:
    """Generic down/move/up and HIDC snapshots → one multitouch frame."""

    def __init__(self) -> None:
        self.slots: dict[int, int] = {}  # pointer id → slot
        self.tools: dict[int, str] = {}
        self._tracks: dict[int, int] = {}
        self._next_track = 1
        self._frame: list[tuple[int, int, int]] = []

    def events(
        self,
        event: TouchEvent,
        src_w: int,
        src_h: int,
        dst_w: int,
        dst_h: int,
    ) -> list[tuple[int, int, int]]:
        if event.snapshot:
            self._snapshot(event.pointers, src_w, src_h, dst_w, dst_h)
        elif event.action == "up":
            for pointer in event.pointers:
                self._release(pointer.pointer_id)
        else:
            for pointer in event.pointers:
                self._contact(pointer, src_w, src_h, dst_w, dst_h, moving=event.action == "move")
        if not self._frame:
            return []
        frame = self._frame
        self._frame = []
        down = bool(self.slots)
        tools = set(self.tools.values())
        frame.append((EV_KEY, BTN_TOUCH, 1 if down else 0))
        frame.append((EV_KEY, BTN_TOOL_FINGER, 1 if "finger" in tools else 0))
        frame.append((EV_KEY, BTN_TOOL_PEN, 1 if "pen" in tools else 0))
        frame.append((EV_SYN, SYN_REPORT, 0))
        return frame

    def __init_frame(self) -> None:
        self._frame: list[tuple[int, int, int]] = []

    def _snapshot(self, pointers: tuple[Pointer, ...], src_w: int, src_h: int, dst_w: int, dst_h: int) -> None:
        active = {pointer.pointer_id: pointer for pointer in pointers if pointer.down}
        for pointer_id in list(self.slots):
            if pointer_id not in active:
                self._release(pointer_id)
        for pointer in active.values():
            self._contact(pointer, src_w, src_h, dst_w, dst_h, moving=pointer.pointer_id in self.slots)

    def _slot_for(self, pointer_id: int) -> Optional[int]:
        if pointer_id in self.slots:
            return self.slots[pointer_id]
        used = set(self.slots.values())
        for slot in range(MAX_SLOTS):
            if slot not in used:
                self.slots[pointer_id] = slot
                return slot
        return None

    def _contact(self, pointer: Pointer, src_w: int, src_h: int, dst_w: int, dst_h: int, moving: bool) -> None:
        slot = self._slot_for(pointer.pointer_id)
        if slot is None:
            return
        new = pointer.pointer_id not in self.tools or not moving
        if pointer.pointer_id not in self.tools:
            self.tools[pointer.pointer_id] = pointer.tool
        x, y = scale_point(pointer.x, pointer.y, src_w, src_h, dst_w, dst_h)
        tool = MT_TOOL_PEN if pointer.tool == "pen" else MT_TOOL_FINGER
        if new and pointer.pointer_id not in self._tracks:
            self._tracks[pointer.pointer_id] = self._next_track
            self._next_track += 1
        tracking = self._tracks[pointer.pointer_id]
        self._frame.append((EV_ABS, ABS_MT_SLOT, slot))
        if new:
            self._frame.append((EV_ABS, ABS_MT_TRACKING_ID, tracking))
            self._frame.append((EV_ABS, ABS_MT_TOOL_TYPE, tool))
        self._frame.append((EV_ABS, ABS_MT_POSITION_X, x))
        self._frame.append((EV_ABS, ABS_MT_POSITION_Y, y))
        if pointer.pressure:
            self._frame.append((EV_ABS, ABS_MT_PRESSURE, min(255, pointer.pressure)))
        if slot == 0:
            self._frame.append((EV_ABS, ABS_X, x))
            self._frame.append((EV_ABS, ABS_Y, y))

    def _release(self, pointer_id: int) -> None:
        slot = self.slots.pop(pointer_id, None)
        self.tools.pop(pointer_id, None)
        self._tracks.pop(pointer_id, None)
        if slot is None:
            return
        self._frame.append((EV_ABS, ABS_MT_SLOT, slot))
        self._frame.append((EV_ABS, ABS_MT_TRACKING_ID, -1))


class TouchInjector:
    """Own the uinput device and translate stream-space events onto it."""

    def __init__(self, device_path: str = "/dev/uinput") -> None:
        self.device_path = device_path
        self.out_w = 0
        self.out_h = 0
        self.src_w = 0
        self.src_h = 0
        self._fd: Optional[int] = None
        self._tracker = ContactTracker()
        self._lock = threading.Lock()
        self._logged_open_failure = False
        self.events = 0

    @property
    def open(self) -> bool:
        return self._fd is not None

    def set_stream(self, width: int, height: int) -> None:
        """Negotiated video size. UIBC coordinates are in this space."""
        self.src_w, self.src_h = max(1, width), max(1, height)

    def configure(self, width: int, height: int) -> bool:
        """(Re)create the device for the EVDI mode. False when uinput cannot be opened."""
        width, height = max(1, width), max(1, height)
        if self._fd is not None and (width, height) == (self.out_w, self.out_h):
            return True
        self.close()
        self.out_w, self.out_h = width, height
        if not self.src_w:
            self.src_w, self.src_h = width, height
        try:
            fd = os.open(self.device_path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            if not self._logged_open_failure:
                log.warning(
                    "touch: cannot open %s (%s). sudo ./scripts/setup-evdi.sh installs the "
                    "uinput udev rule; log out and back in so the seat ACL applies.",
                    self.device_path, exc,
                )
                self._logged_open_failure = True
            return False
        try:
            self._setup(fd, width, height)
        except OSError as exc:
            log.warning("touch: uinput setup failed: %s", exc)
            os.close(fd)
            return False
        self._fd = fd
        self._tracker = ContactTracker()
        log.info("touch: uinput '%s' %sx%s (stream %sx%s)", DEVICE_NAME, width, height, self.src_w, self.src_h)
        return True

    def _setup(self, fd: int, width: int, height: int) -> None:
        import fcntl

        for bit in (EV_KEY, EV_ABS):
            fcntl.ioctl(fd, UI_SET_EVBIT, bit)
        for key in (BTN_TOUCH, BTN_TOOL_FINGER, BTN_TOOL_PEN):
            fcntl.ioctl(fd, UI_SET_KEYBIT, key)
        axes = (
            (ABS_X, width - 1),
            (ABS_Y, height - 1),
            (ABS_PRESSURE, 255),
            (ABS_MT_SLOT, MAX_SLOTS - 1),
            (ABS_MT_TRACKING_ID, 65535),
            (ABS_MT_POSITION_X, width - 1),
            (ABS_MT_POSITION_Y, height - 1),
            (ABS_MT_TOOL_TYPE, MT_TOOL_PEN),
            (ABS_MT_TOUCH_MAJOR, 255),
            (ABS_MT_PRESSURE, 255),
        )
        for code, maximum in axes:
            fcntl.ioctl(fd, UI_SET_ABSBIT, code)
        fcntl.ioctl(fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT)
        for code, maximum in axes:
            minimum = -1 if code == ABS_MT_TRACKING_ID else 0
            buf = bytearray(uinput_abs_setup(code, minimum, maximum))
            fcntl.ioctl(fd, UI_ABS_SETUP, buf)
        setup = bytearray(uinput_setup_bytes(DEVICE_NAME))
        fcntl.ioctl(fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(fd, UI_DEV_CREATE, 0)

    def handle(self, event: TouchEvent) -> None:
        src_w = self.src_w or self.out_w
        src_h = self.src_h or self.out_h
        if self.events < 8 or event.action in ("down", "snapshot"):
            tools = ",".join(sorted({pointer.tool for pointer in event.pointers})) or "-"
            where = ", ".join(f"{pointer.pointer_id}:{pointer.x},{pointer.y}" for pointer in event.pointers)
            log.info("touch %s (%s) %s", event.action, tools, where)
        self.events += 1
        with self._lock:
            if self._fd is None or self.out_w <= 0:
                return
            frame = self._tracker.events(event, src_w, src_h, self.out_w, self.out_h)
            if not frame:
                return
            try:
                os.write(self._fd, b"".join(pack_input_event(*item) for item in frame))
            except OSError as exc:
                log.warning("touch: write failed: %s", exc)

    def close(self) -> None:
        import fcntl

        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.ioctl(fd, UI_DEV_DESTROY, 0)
        except OSError:
            pass
        os.close(fd)


def cosmic_entry(device: str, output: str) -> str:
    return (
        f'"{device}": (\n'
        "        state: Enabled,\n"
        f'        map_to_output: Some("{output}"),\n'
        "    )"
    )


def upsert_cosmic_input_map(existing: str, device: str, output: str) -> str:
    """Insert or replace this device's map_to_output entry. Other devices stay."""
    entry = cosmic_entry(device, output)
    text = existing.strip()
    if not text:
        return "{\n    " + entry + ",\n}\n"
    # One level of nesting covers `Some("DVI-I-1")`.
    pattern = re.compile(
        '"' + re.escape(device) + r'"\s*:\s*\((?:[^()]|\([^()]*\))*\)',
        re.S,
    )
    if pattern.search(text):
        replaced = pattern.sub(entry, text, count=1)
        return replaced if replaced.endswith("\n") else replaced + "\n"
    end = text.rfind("}")
    if end < 0:
        raise ValueError("cosmic input_devices has no top-level map")
    prefix = text[:end].rstrip()
    if not prefix.endswith("{"):
        if not prefix.endswith(","):
            prefix += ","
        prefix += "\n    " + entry + ",\n"
    else:
        prefix += "\n    " + entry + ",\n"
    return prefix + text[end:] + ("" if text.endswith("\n") else "\n")


def write_cosmic_touch_map(output_name: str, path: Optional[Path] = None, device: str = DEVICE_NAME) -> Path:
    """Point COSMIC at the EVDI connector for this touchscreen."""
    target = path or (Path.home() / COSMIC_INPUT_CONFIG)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    target.write_text(upsert_cosmic_input_map(existing, device, output_name), encoding="utf-8")
    return target
