"""Absolute touchscreen on /dev/uinput, sized to the EVDI framebuffer.

libinput treats INPUT_PROP_DIRECT + ABS_X/ABS_Y + BTN_TOUCH as a touchscreen.
cosmic-comp then applies `map_to_output` (see layout.py). A plain absolute
pointer would ignore that setting and land on whichever output is active.
"""
from __future__ import annotations

import logging
import os
import struct
import threading
from typing import Optional

from .keys import (
    REL_HWHEEL,
    REL_WHEEL,
    contact_buttons,
    plan_key,
    used_keycodes,
)
from .layout import UINPUT_NAME

log = logging.getLogger(__name__)

# linux/input.h and linux/uinput.h, amd64 (Pop!_OS 24.04 on the AMD machine).
# ioctl numbers are _IO/_IOW('U', nr, size) with the sizes checked against
# the kernel headers: input_event 24, uinput_setup 92, uinput_abs_setup 28.
BUS_VIRTUAL = 6
EV_SYN, EV_KEY, EV_REL, EV_ABS = 0, 1, 2, 3
SYN_REPORT = 0
BTN_TOUCH = 330
BTN_TOOL_FINGER = 325
ABS_X, ABS_Y = 0, 1
ABS_MT_SLOT = 47
ABS_MT_POSITION_X = 53
ABS_MT_POSITION_Y = 54
ABS_MT_TRACKING_ID = 57
INPUT_PROP_DIRECT = 1


def _iow(nr: int, size: int) -> int:
    return (1 << 30) | (size << 16) | (ord("U") << 8) | nr


def _io(nr: int) -> int:
    return (ord("U") << 8) | nr


UI_DEV_CREATE = _io(1)
UI_DEV_DESTROY = _io(2)
UI_DEV_SETUP = _iow(3, 92)
UI_ABS_SETUP = _iow(4, 28)
UI_SET_EVBIT = _iow(100, 4)
UI_SET_KEYBIT = _iow(101, 4)
UI_SET_RELBIT = _iow(102, 4)
UI_SET_ABSBIT = _iow(103, 4)
UI_SET_PROPBIT = _iow(110, 4)

KEYBOARD_NAME = "Segunda Tela VNC Teclado"


def input_event(ev_type: int, code: int, value: int) -> bytes:
    """One `struct input_event` (24 bytes on amd64).

    Python's struct code `l` is 4 bytes; the kernel's `long` here is 8 (`q`).
    """
    return struct.pack("=qqHHi", 0, 0, ev_type, code, value)


def touch_report(x: int, y: int, *, contact: bool, tracking_id: int) -> bytes:
    """Protocol-B slot 0 plus the single-touch axes libinput also reads."""
    parts = [input_event(EV_ABS, ABS_MT_SLOT, 0)]
    if contact:
        parts.append(input_event(EV_ABS, ABS_MT_TRACKING_ID, tracking_id))
        parts.append(input_event(EV_ABS, ABS_MT_POSITION_X, x))
        parts.append(input_event(EV_ABS, ABS_MT_POSITION_Y, y))
        parts.append(input_event(EV_ABS, ABS_X, x))
        parts.append(input_event(EV_ABS, ABS_Y, y))
        parts.append(input_event(EV_KEY, BTN_TOUCH, 1))
        parts.append(input_event(EV_KEY, BTN_TOOL_FINGER, 1))
    else:
        parts.append(input_event(EV_ABS, ABS_MT_TRACKING_ID, -1))
        parts.append(input_event(EV_KEY, BTN_TOUCH, 0))
        parts.append(input_event(EV_KEY, BTN_TOOL_FINGER, 0))
    parts.append(input_event(EV_SYN, SYN_REPORT, 0))
    return b"".join(parts)


def _setup_blob(name: str) -> bytes:
    raw = name.encode("utf-8")[:79]
    name_b = raw + b"\x00" * (80 - len(raw))
    return struct.pack("=HHHH", BUS_VIRTUAL, 0x5347, 1, 1) + name_b + struct.pack("=I", 0)


def _abs_blob(code: int, minimum: int, maximum: int) -> bytes:
    return struct.pack("=HH6i", code, 0, 0, minimum, maximum, 0, 0, 0)


class UinputError(RuntimeError):
    pass


class EvdiTouch:
    """One absolute touch device whose 0..width-1 / 0..height-1 range is the EVDI output."""

    def __init__(self, name: str = UINPUT_NAME, path: str = "/dev/uinput") -> None:
        self.name = name
        self.path = path
        self._fd: Optional[int] = None
        self._w = 1
        self._h = 1
        self._down = False
        self._track = 0
        self._lock = threading.Lock()

    @property
    def open_fd(self) -> bool:
        return self._fd is not None

    def open(self, width: int, height: int) -> None:
        import fcntl
        if width < 1 or height < 1:
            raise UinputError("tamanho do toque")
        self.close()
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            raise UinputError(
                f"não abri {self.path} ({exc}). Rode sudo ./scripts/install-deps-popos.sh "
                "e entre de novo na sessão (a regra udev dá uaccess em /dev/uinput)."
            ) from exc
        try:
            for bit in (EV_SYN, EV_KEY, EV_ABS):
                fcntl.ioctl(fd, UI_SET_EVBIT, bit)
            for bit in (BTN_TOUCH, BTN_TOOL_FINGER):
                fcntl.ioctl(fd, UI_SET_KEYBIT, bit)
            axes = (
                (ABS_X, 0, width - 1),
                (ABS_Y, 0, height - 1),
                (ABS_MT_SLOT, 0, 0),
                (ABS_MT_TRACKING_ID, 0, 65535),
                (ABS_MT_POSITION_X, 0, width - 1),
                (ABS_MT_POSITION_Y, 0, height - 1),
            )
            for code, lo, hi in axes:
                fcntl.ioctl(fd, UI_SET_ABSBIT, code)
                fcntl.ioctl(fd, UI_ABS_SETUP, _abs_blob(code, lo, hi))
            fcntl.ioctl(fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT)
            fcntl.ioctl(fd, UI_DEV_SETUP, _setup_blob(self.name))
            fcntl.ioctl(fd, UI_DEV_CREATE, 0)
        except OSError as exc:
            os.close(fd)
            raise UinputError(f"uinput setup falhou: {exc}") from exc
        self._fd = fd
        self._w, self._h = width, height
        self._down = False
        log.info("uinput '%s' %sx%s em %s", self.name, width, height, self.path)

    def close(self) -> None:
        import fcntl
        fd = self._fd
        self._fd = None
        self._down = False
        if fd is None:
            return
        try:
            fcntl.ioctl(fd, UI_DEV_DESTROY, 0)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def reconfigure(self, width: int, height: int) -> None:
        if self._fd is None or (width == self._w and height == self._h):
            self._w, self._h = width, height
            return
        self.open(width, height)

    def pointer(self, x: int, y: int, buttons: int) -> None:
        """VNC pointer event. Button 1 is finger contact; hover is ignored."""
        if self._fd is None:
            return
        x = 0 if x < 0 else self._w - 1 if x >= self._w else x
        y = 0 if y < 0 else self._h - 1 if y >= self._h else y
        # Bits 0–2 are the finger. Wheel bits (3–6) must not click.
        contact = bool(contact_buttons(buttons))
        with self._lock:
            if contact:
                if not self._down:
                    self._down = True
                    self._track = (self._track + 1) & 0xFFFF
                report = touch_report(x, y, contact=True, tracking_id=self._track)
            elif self._down:
                self._down = False
                report = touch_report(x, y, contact=False, tracking_id=-1)
            else:
                return
            try:
                os.write(self._fd, report)
            except OSError as exc:
                log.warning("uinput write: %s", exc)


class EvdiKeyboard:
    """Keyboard plus scroll wheel. Not a touchscreen, so COSMIC does not map it to one output."""

    def __init__(self, name: str = KEYBOARD_NAME, path: str = "/dev/uinput") -> None:
        self.name = name
        self.path = path
        self._fd: Optional[int] = None
        self._lock = threading.Lock()
        self._shift_down = False
        self._synth_shift = False

    @property
    def open_fd(self) -> bool:
        return self._fd is not None

    def open(self) -> None:
        import fcntl
        self.close()
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            raise UinputError(
                f"não abri {self.path} ({exc}). Rode sudo ./scripts/install-deps-popos.sh "
                "e entre de novo na sessão (a regra udev dá uaccess em /dev/uinput)."
            ) from exc
        try:
            for bit in (EV_SYN, EV_KEY, EV_REL):
                fcntl.ioctl(fd, UI_SET_EVBIT, bit)
            for code in used_keycodes():
                fcntl.ioctl(fd, UI_SET_KEYBIT, code)
            for code in (REL_WHEEL, REL_HWHEEL):
                fcntl.ioctl(fd, UI_SET_RELBIT, code)
            fcntl.ioctl(fd, UI_DEV_SETUP, _setup_blob(self.name))
            fcntl.ioctl(fd, UI_DEV_CREATE, 0)
        except OSError as exc:
            os.close(fd)
            raise UinputError(f"uinput teclado falhou: {exc}") from exc
        self._fd = fd
        self._shift_down = False
        self._synth_shift = False
        log.info("uinput '%s' (teclado e roda) em %s", self.name, self.path)

    def close(self) -> None:
        import fcntl
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            fcntl.ioctl(fd, UI_DEV_DESTROY, 0)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def _write(self, payload: bytes) -> None:
        if self._fd is None or not payload:
            return
        try:
            os.write(self._fd, payload)
        except OSError as exc:
            log.warning("uinput teclado: %s", exc)

    def key(self, keysym: int, down: bool) -> None:
        if self._fd is None:
            return
        with self._lock:
            planned = plan_key(
                keysym, down, shift_down=self._shift_down, synth_shift=self._synth_shift,
            )
            if planned is None:
                log.info("VNC key sem mapa keysym=%#x", keysym)
                return
            events, self._shift_down, self._synth_shift = planned
            payload = b"".join(input_event(EV_KEY, code, value) for code, value in events)
            payload += input_event(EV_SYN, SYN_REPORT, 0)
            self._write(payload)

    def wheel(self, dx: int, dy: int) -> None:
        if self._fd is None or (dx == 0 and dy == 0):
            return
        parts = []
        if dy:
            parts.append(input_event(EV_REL, REL_WHEEL, dy))
        if dx:
            parts.append(input_event(EV_REL, REL_HWHEEL, dx))
        parts.append(input_event(EV_SYN, SYN_REPORT, 0))
        with self._lock:
            self._write(b"".join(parts))
