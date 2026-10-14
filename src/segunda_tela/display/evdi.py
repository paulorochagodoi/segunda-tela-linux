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
# Memory order R,G,B,X. DRM XRGB8888 is B,G,R,X (GStreamer bgrx), but the
# extended desktop on the Tab S10 FE showed red and blue swapped with bgrx:
# a pixel whose first byte is red is blue when parsed as bgrx and red when
# parsed as rgbx. libevdi's EvdiBuffer is a userspace pointer — no dma-buf
# fd — so the encoder cannot import the grab zero-copy.
EVDI_RAW_FORMAT = "rgbx"


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


def fourcc_str(value: int) -> str:
    raw = bytes((value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF, (value >> 24) & 0xFF))
    if all(32 <= b < 127 for b in raw):
        return raw.decode("ascii")
    return f"0x{value:08x}"


def blit_damage(
    dest: bytearray,
    src: bytes | bytearray | memoryview,
    width: int,
    height: int,
    rects: list[tuple[int, int, int, int]],
) -> int:
    """Copy dirty rectangles from `src` into `dest`. Returns bytes copied.

    A region covering at least half the frame is one memcpy. Empty or
    inverted rects copy nothing. Coordinates are clamped to the frame.
    """
    stride = width * 4
    size = stride * height
    if len(dest) < size:
        raise ValueError("dest shorter than the frame")
    src_mv = memoryview(src)
    if src_mv.itemsize != 1 or src_mv.ndim != 1:
        src_mv = src_mv.cast("B")
    if len(src_mv) < size:
        raise ValueError("src shorter than the frame")
    norm: list[tuple[int, int, int, int]] = []
    total = 0
    for x1, y1, x2, y2 in rects:
        x1 = 0 if x1 < 0 else width if x1 > width else x1
        x2 = 0 if x2 < 0 else width if x2 > width else x2
        y1 = 0 if y1 < 0 else height if y1 > height else y1
        y2 = 0 if y2 < 0 else height if y2 > height else y2
        if x2 <= x1 or y2 <= y1:
            continue
        total += (x2 - x1) * (y2 - y1) * 4
        norm.append((x1, y1, x2, y2))
    if not norm:
        return 0
    if total >= size // 2:
        dest[:size] = src_mv[:size]
        return size
    copied = 0
    for x1, y1, x2, y2 in norm:
        row = (x2 - x1) * 4
        for y in range(y1, y2):
            off = y * stride + x1 * 4
            dest[off:off + row] = src_mv[off:off + row]
            copied += row
    return copied


class ShadowFrames:
    """Two full frames. The grab paints into the buffer the feeder is not writing.

    libevdi writes dirty rectangles into one registered userspace buffer (there
    is no dma-buf to hand to vah264enc). Publishing that buffer directly would
    tear. This keeps a stable copy for the feeder and only memcpys the damage,
    or the whole frame when the spare copy is stale.
    """

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.size = width * height * 4
        self.shadows = [bytearray(self.size), bytearray(self.size)]
        self.published = 0
        self.reading: Optional[int] = None
        self.missed = [True, True]
        self.seq = 0

    def apply(self, src: bytes | bytearray | memoryview, rects: list[tuple[int, int, int, int]]) -> bool:
        """Fold `rects` from `src` into the shadow the feeder is not reading.

        A stale shadow is replaced from the whole kernel buffer (that buffer
        already contains every pixel; the rect list is only the latest damage).
        """
        if not rects:
            return False
        if self.reading is None:
            target = self.published if not self.missed[self.published] else 0
        else:
            target = 1 - self.reading
        if self.missed[target]:
            blit_damage(self.shadows[target], src, self.width, self.height, [(0, 0, self.width, self.height)])
            self.missed[target] = False
        else:
            blit_damage(self.shadows[target], src, self.width, self.height, rects)
        if self.reading is not None:
            self.missed[self.reading] = True
        self.published = target
        self.seq += 1
        return True

    def acquire(self) -> Optional[memoryview]:
        if self.seq <= 0:
            return None
        self.reading = self.published
        return memoryview(self.shadows[self.published])

    def release(self) -> None:
        self.reading = None


class GrabClock:
    """One EVDI update in flight, grabbed on a fixed period.

    The old loop requested an update, waited out the ioctl, then `select`ed
    for another full frame. A 10 ms grab on top of a 33 ms wait is 43 ms,
    which is the steady 23.3 fps the tablet showed. The next request is
    issued as soon as the grab returns (one frame ahead) and the following
    grab stays on the original period, so that ioctl time sits inside the
    frame instead of after it.
    """

    def __init__(self, now: float, fps: int = 30) -> None:
        self.interval = 1.0 / max(1, fps)
        self.next_slot = now
        self.phase = "idle"  # idle | pending | ready

    def set_fps(self, fps: int) -> None:
        self.interval = 1.0 / max(1, fps)

    def want_request(self, now: float) -> bool:
        return self.phase == "idle" and now + 1e-6 >= self.next_slot - self.interval

    def requested(self, immediate: bool) -> None:
        self.phase = "ready" if immediate else "pending"

    def update_ready(self) -> None:
        if self.phase == "pending":
            self.phase = "ready"

    def want_grab(self, now: float) -> bool:
        return self.phase == "ready" and now + 1e-6 >= self.next_slot

    def grabbed(self, now: float) -> None:
        self.phase = "idle"
        self.next_slot += self.interval
        if self.next_slot < now:
            behind = now - self.next_slot
            self.next_slot += (int(behind / self.interval) + 1) * self.interval

    def timeout(self, now: float) -> float:
        if self.phase == "ready":
            wait = self.next_slot - now
        elif self.phase == "idle":
            wait = self.next_slot - self.interval - now
        else:
            wait = self.interval
        if wait < 0.0:
            wait = 0.0
        return min(wait, 0.05)


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
        self.pixel_format = 0
        self.dpms: Optional[int] = None
        self.on_mode_changed: Optional[Callable[[int, int, int], None]] = None
        self._lib: Optional[ctypes.CDLL] = None
        self._handle: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._frame_lock = threading.Condition()
        self._frame_seq = 0
        self._slots: Optional[ShadowFrames] = None
        self._buf = None
        self._buf_desc: Optional[EvdiBuffer] = None
        self._rects = (EvdiRect * MAX_DIRTY_RECTS)()
        self._buf_id = 1
        self._pending = False
        self._clock = GrabClock(time.monotonic(), self.fps)
        self._stat_t0 = time.monotonic()
        self._stat_grabs = 0
        self._stat_frames = 0
        self._stat_requests = 0
        self._stat_immediate = 0
        self._stat_req_s = 0.0
        self._stat_grab_s = 0.0
        self._stat_blit_s = 0.0
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
    def wait_frame(self, after_seq: int, timeout: float) -> tuple[int, Optional[memoryview]]:
        """Return the latest stable frame. The caller must `release_frame`.

        The memoryview aliases the published shadow. It is not copied. A
        repeat of the previous frame (no new damage) returns that same buffer
        so the feeder can keep a constant rate.
        """
        with self._frame_lock:
            if self._frame_seq <= after_seq:
                self._frame_lock.wait(timeout)
            view = self._slots.acquire() if self._slots is not None else None
            return self._frame_seq, view

    def release_frame(self) -> None:
        with self._frame_lock:
            if self._slots is not None:
                self._slots.release()

    # -------------------------------------------------------------- callbacks
    def _on_dpms(self, mode: int, _ud) -> None:
        self.dpms = mode
        log.debug("EVDI dpms=%s", mode)

    def _on_crtc(self, state: int, _ud) -> None:
        log.debug("EVDI crtc state=%s", state)

    def _on_mode(self, mode: EvdiMode, _ud) -> None:
        self.mode = (mode.width, mode.height, mode.refresh_rate)
        self.pixel_format = int(mode.pixel_format)
        log.info(
            "EVDI mode set by compositor: %sx%s@%s bpp=%s fourcc=%s (0x%08x); rawvideoparse format=%s",
            mode.width, mode.height, mode.refresh_rate, mode.bits_per_pixel,
            fourcc_str(self.pixel_format), self.pixel_format, EVDI_RAW_FORMAT,
        )
        if self.grab:
            self._register_buffer(mode.width, mode.height)
        if self.on_mode_changed:
            try:
                self.on_mode_changed(*self.mode)
            except Exception as exc:  # noqa: BLE001
                log.warning("mode change callback: %s", exc)

    def _on_update_ready(self, _buffer_id: int, _ud) -> None:
        # The loop grabs on the frame slot. Grabbing here would add the
        # ioctl on top of the period (the 23.3 fps bug).
        self._pending = False
        self._clock.update_ready()

    # -------------------------------------------------------------- internals
    def _register_buffer(self, width: int, height: int) -> None:
        assert self._lib and self._handle
        if self._buf_desc is not None:
            self._lib.evdi_unregister_buffer(self._handle, self._buf_id)
            self._buf_id += 1
        stride = width * 4
        self._buf = (ctypes.c_char * (stride * height))()
        self._slots = ShadowFrames(width, height)
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
        if not (self._lib and self._handle and self._buf_desc is not None and self._buf is not None):
            return
        n = ctypes.c_int(0)
        t0 = time.monotonic()
        # The ioctl copies only dirty rectangles into the registered buffer.
        self._lib.evdi_grab_pixels(self._handle, self._rects, ctypes.byref(n))
        grab_s = time.monotonic() - t0
        count = max(0, int(n.value))
        rects = [
            (int(self._rects[i].x1), int(self._rects[i].y1), int(self._rects[i].x2), int(self._rects[i].y2))
            for i in range(min(count, MAX_DIRTY_RECTS))
        ]
        t1 = time.monotonic()
        with self._frame_lock:
            self._stat_grabs += 1
            self._stat_grab_s += grab_s
            if self._slots is not None and self._slots.apply(memoryview(self._buf), rects):
                self._frame_seq = self._slots.seq
                self._stat_frames += 1
                self._frame_lock.notify_all()
            self._stat_blit_s += time.monotonic() - t1
            self._log_grab_fps()

    def _log_grab_fps(self) -> None:
        now = time.monotonic()
        dt = now - self._stat_t0
        if dt < 5.0 or self._stat_grabs <= 0:
            return
        req_n = max(1, self._stat_requests)
        log.info(
            "EVDI grab fps %.1f (request %.2fms x%d, grab_pixels %.2fms x%d, blit %.2fms, "
            "updated %d, immediate %d, fourcc=%s)",
            self._stat_grabs / dt,
            1000.0 * self._stat_req_s / req_n, self._stat_requests,
            1000.0 * self._stat_grab_s / self._stat_grabs, self._stat_grabs,
            1000.0 * self._stat_blit_s / self._stat_grabs,
            self._stat_frames, self._stat_immediate, fourcc_str(self.pixel_format),
        )
        self._stat_grabs = 0
        self._stat_frames = 0
        self._stat_requests = 0
        self._stat_immediate = 0
        self._stat_req_s = 0.0
        self._stat_grab_s = 0.0
        self._stat_blit_s = 0.0
        self._stat_t0 = now

    def _loop(self) -> None:
        assert self._lib and self._handle
        fd = self._lib.evdi_get_event_ready(self._handle)
        self._clock = GrabClock(time.monotonic(), self.fps)
        while self._running:
            # PLAY drops grabbing from the EDID rate to the negotiated rate
            # without restarting this thread.
            self._clock.set_fps(self.fps)
            if self.grab and self._buf_desc is None and self.mode is not None:
                self._register_buffer(self.mode[0], self.mode[1])
            now = time.monotonic()
            if self.grab and self._buf_desc is not None and self._clock.want_request(now):
                t0 = time.monotonic()
                immediate = bool(self._lib.evdi_request_update(self._handle, self._buf_id))
                self._stat_req_s += time.monotonic() - t0
                self._stat_requests += 1
                if immediate:
                    self._stat_immediate += 1
                self._pending = not immediate
                self._clock.requested(immediate)
                now = time.monotonic()
            if self.grab and self._buf_desc is not None and self._clock.want_grab(now):
                self._grab_now()
                self._clock.grabbed(time.monotonic())
            try:
                ready, _, _ = select.select([fd], [], [], self._clock.timeout(time.monotonic()))
            except (OSError, ValueError):
                break
            if ready:
                self._lib.evdi_handle_events(self._handle, ctypes.byref(self._ctx))


def copy_frame_buffer(data: bytes | bytearray | memoryview, pts: int, duration: int):
    """One copy of `data` into a Gst.Buffer. Repeats share this memory."""
    from gi.repository import Gst

    raw = data if isinstance(data, (bytes, bytearray)) else bytes(data)
    buf = Gst.Buffer.new_allocate(None, len(raw), None)
    buf.fill(0, raw)
    buf.pts = pts
    buf.duration = duration
    return buf


def repeat_frame_buffer(src, pts: int, duration: int):
    """New buffer, same pixels. No memcpy — the memory is shared."""
    from gi.repository import Gst

    out = Gst.Buffer.new()
    for index in range(src.n_memory()):
        mem = src.peek_memory(index)
        out.append_memory(mem.share(0, mem.size))
    out.pts = pts
    out.duration = duration
    return out


class EvdiAppsrcPusher:
    """Push EVDI frames into an in-process appsrc at a fixed rate.

    A changed frame is copied once into a Gst.Buffer. An unchanged slot
    shares that memory and only gets a new timestamp, so a static desktop
    does not memcpy or write a full frame 30 times a second. The pipe path
    (`EvdiFrameFeeder`) is the fallback when GStreamer is not in-process.
    """

    def __init__(self, monitor: EvdiMonitor, width: int, height: int, fps: int) -> None:
        self.monitor = monitor
        self.frame_size = width * height * 4
        self.fps = max(1, fps)
        self.copies = 0
        self.repeats = 0
        self._stop = threading.Event()

    def __call__(self, appsrc) -> None:
        from gi.repository import Gst

        duration = Gst.SECOND // self.fps
        interval = 1.0 / self.fps
        pts = 0
        seq = 0
        last = None
        blank = bytes(self.frame_size)
        next_t = time.monotonic()
        sent = 0
        win_copies = win_repeats = 0
        copy_s = push_s = 0.0
        window = next_t
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                timeout = max(0.0, next_t - now)
                prev = seq
                seq, frame = self.monitor.wait_frame(seq, timeout)
                try:
                    changed = seq != prev and frame is not None and len(frame) == self.frame_size
                    t0 = time.monotonic()
                    if changed or last is None:
                        last = copy_frame_buffer(frame if changed else blank, pts, duration)
                        copy_s += time.monotonic() - t0
                        self.copies += 1
                        win_copies += 1
                        out = last
                    else:
                        self.repeats += 1
                        win_repeats += 1
                        out = repeat_frame_buffer(last, pts, duration)
                    t1 = time.monotonic()
                    ret = appsrc.emit("push-buffer", out)
                    push_s += time.monotonic() - t1
                    if ret == Gst.FlowReturn.FLUSHING or ret == Gst.FlowReturn.EOS:
                        break
                finally:
                    release = getattr(self.monitor, "release_frame", None)
                    if release is not None:
                        release()
                pts += duration
                sent += 1
                next_t += interval
                now = time.monotonic()
                if now - window >= 5.0:
                    log.info(
                        "EVDI feed fps %.1f (copy %.2fms x%d, push %.2fms x%d, repeats %d)",
                        sent / (now - window),
                        1000.0 * copy_s / max(1, win_copies), win_copies,
                        1000.0 * push_s / max(1, sent), sent, win_repeats,
                    )
                    sent = 0
                    win_copies = win_repeats = 0
                    copy_s = push_s = 0.0
                    window = now
                if now > next_t + interval:
                    next_t = now
                elif next_t > now:
                    time.sleep(next_t - now)
        except (ValueError, OSError) as exc:
            log.info("EVDI appsrc pusher stopped: %s", exc)

    def stop(self) -> None:
        self._stop.set()


class EvdiFrameFeeder:
    """Pipe fallback: write RGBx frames to fdsrc when appsrc is not in-process.

    Unchanged frames still cross the pipe; the appsrc pusher is what skips
    that copy. Release the shadow after the write so the next grab can reuse it.
    """

    def __init__(self, monitor: EvdiMonitor, width: int, height: int, fps: int) -> None:
        self.monitor = monitor
        self.frame_size = width * height * 4
        self.fps = max(1, fps)
        self._stop = threading.Event()

    def __call__(self, pipe) -> None:
        seq, blank = 0, bytes(self.frame_size)
        interval = 1.0 / self.fps
        warned = False
        sent = 0
        window = time.monotonic()
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                payload: bytes | memoryview = blank
                try:
                    seq, frame = self.monitor.wait_frame(seq, interval)
                    if frame is not None and len(frame) == self.frame_size:
                        payload = frame
                    elif frame is not None and not warned:
                        log.warning("EVDI frame size changed; restart the stream to follow the new mode")
                        warned = True
                    pipe.write(payload)
                    pipe.flush()
                    sent += 1
                finally:
                    release = getattr(self.monitor, "release_frame", None)
                    if release is not None:
                        release()
                now = time.monotonic()
                if now - window >= 5.0:
                    log.info("EVDI feed fps %.1f (%d frames in %.1fs)", sent / (now - window), sent, now - window)
                    sent = 0
                    window = now
                delay = interval - (now - t0)
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
