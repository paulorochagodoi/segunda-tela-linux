"""EVDI virtual monitor via libevdi (ctypes).

Uses the distro `evdi-dkms` kernel module and `libevdi1` userspace library
(DisplayLink's open-source EVDI). We do not ship or build a kernel module.

Once connected with our EDID, the compositor (cosmic-comp, mutter, …) sees a
new DRM connector and can extend the desktop onto it. Frames are grabbed
straight from EVDI (default in extend mode, no portal dialog); the
ScreenCast portal is only a fallback.

Hardware path — not exercised in CI.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import select
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

MAX_DIRTY_RECTS = 16
EVDI_AVAILABLE = 0  # enum evdi_device_status: AVAILABLE, UNRECOGNIZED, NOT_PRESENT


class EvdiError(RuntimeError):
    pass


class EvdiRect(ctypes.Structure):
    _fields_ = [("x1", ctypes.c_int), ("y1", ctypes.c_int), ("x2", ctypes.c_int), ("y2", ctypes.c_int)]


class EvdiMode(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("refresh_rate", ctypes.c_int),
        ("bits_per_pixel", ctypes.c_int),
        ("pixel_format", ctypes.c_uint),
    ]


class EvdiBuffer(ctypes.Structure):
    _fields_ = [
        ("id", ctypes.c_int),
        ("buffer", ctypes.c_void_p),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("stride", ctypes.c_int),
        ("rects", ctypes.POINTER(EvdiRect)),
        ("rect_count", ctypes.c_int),
    ]


_DPMS_CB = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_void_p)
_MODE_CB = ctypes.CFUNCTYPE(None, EvdiMode, ctypes.c_void_p)
_UPDATE_CB = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_void_p)
_CRTC_CB = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_void_p)


class EvdiEventContext(ctypes.Structure):
    _fields_ = [
        ("dpms_handler", _DPMS_CB),
        ("mode_changed_handler", _MODE_CB),
        ("update_ready_handler", _UPDATE_CB),
        ("crtc_state_handler", _CRTC_CB),
        ("cursor_set_handler", ctypes.c_void_p),
        ("cursor_move_handler", ctypes.c_void_p),
        ("ddcci_data_handler", ctypes.c_void_p),
        ("user_data", ctypes.c_void_p),
    ]


SETUP_HINT = (
    "EVDI indisponível. No Pop!_OS 24.04:\n"
    "  sudo ./scripts/setup-evdi.sh      # distro evdi-dkms, ou release do GitHub via DKMS;\n"
    "                                    # modprobe + initial_device_count=1 + udev; Secure Boot/MOK\n"
    "Ver docs/COSMIC.md."
)


def module_loaded(sys_root: Path = Path("/sys")) -> bool:
    return (sys_root / "module" / "evdi").exists()


def evdi_cards(sys_root: Path = Path("/sys")) -> list[int]:
    """DRM card indices backed by evdi platform devices."""
    cards: list[int] = []
    base = sys_root / "bus" / "platform" / "devices"
    if not base.is_dir():
        return cards
    for dev in sorted(base.glob("evdi*")):
        drm = dev / "drm"
        if drm.is_dir():
            for c in drm.glob("card*"):
                try:
                    cards.append(int(c.name[4:]))
                except ValueError:
                    pass
    return sorted(cards)


def load_libevdi() -> ctypes.CDLL:
    names = [ctypes.util.find_library("evdi"), "libevdi.so.1", "libevdi.so.0", "libevdi.so"]
    last: Optional[OSError] = None
    for name in filter(None, names):
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError as exc:
            last = exc
    else:
        raise EvdiError(f"libevdi not found ({last}).\n{SETUP_HINT}")

    lib.evdi_open.restype = ctypes.c_void_p
    lib.evdi_open.argtypes = [ctypes.c_int]
    lib.evdi_open_attached_to.restype = ctypes.c_void_p
    lib.evdi_open_attached_to.argtypes = [ctypes.c_char_p]
    lib.evdi_check_device.restype = ctypes.c_int
    lib.evdi_check_device.argtypes = [ctypes.c_int]
    lib.evdi_close.argtypes = [ctypes.c_void_p]
    lib.evdi_connect.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint32]
    lib.evdi_disconnect.argtypes = [ctypes.c_void_p]
    lib.evdi_enable_cursor_events.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    lib.evdi_grab_pixels.argtypes = [ctypes.c_void_p, ctypes.POINTER(EvdiRect), ctypes.POINTER(ctypes.c_int)]
    lib.evdi_register_buffer.argtypes = [ctypes.c_void_p, EvdiBuffer]
    lib.evdi_unregister_buffer.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.evdi_request_update.restype = ctypes.c_bool
    lib.evdi_request_update.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.evdi_handle_events.argtypes = [ctypes.c_void_p, ctypes.POINTER(EvdiEventContext)]
    lib.evdi_get_event_ready.restype = ctypes.c_int
    lib.evdi_get_event_ready.argtypes = [ctypes.c_void_p]
    return lib


class EvdiMonitor:
    """One EVDI connector presenting `edid` to the compositor."""

    def __init__(self, edid: bytes, area_limit: int = 2560 * 1600, grab: bool = False, fps: int = 60) -> None:
        self.edid = edid
        self.area_limit = area_limit
        self.grab = grab
        self.fps = max(1, fps)
        self.mode: Optional[tuple[int, int, int]] = None  # w, h, refresh
        self.dpms: Optional[int] = None
        self.on_mode_changed: Optional[Callable[[int, int, int], None]] = None
        self._lib: Optional[ctypes.CDLL] = None
        self._handle: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._frame_lock = threading.Condition()
        self._frame: Optional[bytes] = None
        self._frame_seq = 0
        self._buf = None
        self._buf_desc: Optional[EvdiBuffer] = None
        self._rects = (EvdiRect * MAX_DIRTY_RECTS)()
        self._buf_id = 1
        self._pending = False
        self._ctx = EvdiEventContext(
            dpms_handler=_DPMS_CB(self._on_dpms),
            mode_changed_handler=_MODE_CB(self._on_mode),
            update_ready_handler=_UPDATE_CB(self._on_update_ready),
            crtc_state_handler=_CRTC_CB(self._on_crtc),
        )

    # -------------------------------------------------------------- lifecycle
    def open(self) -> None:
        if not module_loaded():
            raise EvdiError(f"kernel module evdi not loaded.\n{SETUP_HINT}")
        self._lib = load_libevdi()
        handle = None
        tried: list[str] = []
        # Prefer an existing evdi card (created by setup-evdi.sh / initial_device_count):
        # evdi_open_attached_to(NULL) may try to *add* a device via sysfs, which needs root.
        for card in evdi_cards():
            status = self._lib.evdi_check_device(card)
            if status != EVDI_AVAILABLE:
                tried.append(f"card{card}: status {status}")
                continue
            if not os.access(f"/dev/dri/card{card}", os.R_OK | os.W_OK):
                tried.append(f"card{card}: /dev/dri/card{card} sem permissão rw")
                continue
            handle = self._lib.evdi_open(card)
            if handle:
                log.info("EVDI: using /dev/dri/card%d", card)
                break
            tried.append(f"card{card}: evdi_open falhou (em uso por outro processo?)")
        if not handle and hasattr(self._lib, "evdi_open_attached_to"):
            # NULL parent = first free generic device (adds one if permitted).
            handle = self._lib.evdi_open_attached_to(None)
            if not handle:
                tried.append("evdi_open_attached_to(NULL) falhou")
        if not handle:
            raise EvdiError(
                "no usable evdi device (" + ("; ".join(tried) or "none present") + ").\n" + SETUP_HINT
            )
        self._handle = handle
        self._lib.evdi_connect(self._handle, self.edid, len(self.edid), self.area_limit)
        self._lib.evdi_enable_cursor_events(self._handle, False)  # cursor blended into frames
        log.info("EVDI connected (EDID %d bytes); compositor should now show a new output", len(self.edid))
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="evdi-events", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        if self._lib and self._handle:
            try:
                if self._buf_desc is not None:
                    self._lib.evdi_unregister_buffer(self._handle, self._buf_id)
                self._lib.evdi_disconnect(self._handle)
                self._lib.evdi_close(self._handle)
            finally:
                self._handle = None
                log.info("EVDI disconnected")

    def enable_grab(self) -> None:
        """Start grabbing frames after open() (portal fallback → EVDI grab).

        The buffer is registered by the event thread, which owns libevdi calls.
        """
        self.grab = True

    # -------------------------------------------------------------- frames
    def wait_frame(self, after_seq: int, timeout: float) -> tuple[int, Optional[bytes]]:
        with self._frame_lock:
            if self._frame_seq <= after_seq:
                self._frame_lock.wait(timeout)
            return self._frame_seq, self._frame

    # -------------------------------------------------------------- callbacks
    def _on_dpms(self, mode: int, _ud) -> None:
        self.dpms = mode
        log.debug("EVDI dpms=%s", mode)

    def _on_crtc(self, state: int, _ud) -> None:
        log.debug("EVDI crtc state=%s", state)

    def _on_mode(self, mode: EvdiMode, _ud) -> None:
        self.mode = (mode.width, mode.height, mode.refresh_rate)
        log.info("EVDI mode set by compositor: %sx%s@%s bpp=%s", mode.width, mode.height, mode.refresh_rate, mode.bits_per_pixel)
        if self.grab:
            self._register_buffer(mode.width, mode.height)
        if self.on_mode_changed:
            try:
                self.on_mode_changed(*self.mode)
            except Exception as exc:  # noqa: BLE001
                log.warning("mode change callback: %s", exc)

    def _on_update_ready(self, _buffer_id: int, _ud) -> None:
        self._pending = False
        self._grab_now()

    # -------------------------------------------------------------- internals
    def _register_buffer(self, width: int, height: int) -> None:
        assert self._lib and self._handle
        if self._buf_desc is not None:
            self._lib.evdi_unregister_buffer(self._handle, self._buf_id)
            self._buf_id += 1
        stride = width * 4
        self._buf = (ctypes.c_char * (stride * height))()
        self._buf_desc = EvdiBuffer(
            self._buf_id,
            ctypes.cast(self._buf, ctypes.c_void_p),
            width,
            height,
            stride,
            self._rects,
            MAX_DIRTY_RECTS,
        )
        self._lib.evdi_register_buffer(self._handle, self._buf_desc)
        self._pending = False

    def _grab_now(self) -> None:
        if not (self._lib and self._handle and self._buf_desc is not None):
            return
        n = ctypes.c_int(0)
        self._lib.evdi_grab_pixels(self._handle, self._rects, ctypes.byref(n))
        with self._frame_lock:
            self._frame = bytes(self._buf)
            self._frame_seq += 1
            self._frame_lock.notify_all()

    def _loop(self) -> None:
        assert self._lib and self._handle
        fd = self._lib.evdi_get_event_ready(self._handle)
        interval = 1.0 / self.fps
        next_req = 0.0
        while self._running:
            if self.grab and self._buf_desc is None and self.mode is not None:
                self._register_buffer(self.mode[0], self.mode[1])
            now = time.monotonic()
            if self.grab and self._buf_desc is not None and not self._pending and now >= next_req:
                next_req = now + interval
                if self._lib.evdi_request_update(self._handle, self._buf_id):
                    self._grab_now()
                else:
                    self._pending = True
            try:
                ready, _, _ = select.select([fd], [], [], min(interval, 0.1))
            except (OSError, ValueError):
                break
            if ready:
                self._lib.evdi_handle_events(self._handle, ctypes.byref(self._ctx))


class EvdiFrameFeeder:
    """Writes the latest EVDI frame (BGRx) to a pipe at a fixed rate."""

    def __init__(self, monitor: EvdiMonitor, width: int, height: int, fps: int) -> None:
        self.monitor = monitor
        self.frame_size = width * height * 4
        self.fps = fps
        self._stop = threading.Event()

    def __call__(self, pipe) -> None:
        seq, last = 0, bytes(self.frame_size)
        interval = 1.0 / self.fps
        warned = False
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                seq, frame = self.monitor.wait_frame(seq, interval)
                if frame is not None:
                    if len(frame) == self.frame_size:
                        last = frame
                    elif not warned:
                        log.warning("EVDI frame size changed; restart the stream to follow the new mode")
                        warned = True
                pipe.write(last)
                pipe.flush()
                delay = interval - (time.monotonic() - t0)
                if delay > 0:
                    time.sleep(delay)
        except (BrokenPipeError, ValueError, OSError):
            pass

    def stop(self) -> None:
        self._stop.set()


def device_node_hint() -> str:
    cards = evdi_cards()
    if not cards:
        return "no evdi card present"
    bad = [c for c in cards if not os.access(f"/dev/dri/card{c}", os.R_OK | os.W_OK)]
    if bad:
        return f"/dev/dri/card{bad[0]} not read/write for this user — run scripts/setup-evdi.sh (udev rule)"
    return f"evdi cards: {cards}"
