"""WFD User Input Back Channel (spec 4.11 / 6.1.15).

The source asks `wfd_uibc_capability` in M3. When the sink lists generic
touch and/or HIDC touch, M4 names the TCP port the source is already
listening on and sets `wfd_uibc_setting: enable`. The sink connects and
sends one TCP stream of UIBC packets.

Samsung's cable mode also opens TCP 50000 (`UibcPort`, the PC listens).
That socket speaks the same packets, so it is a second listener when it
is not already the advertised port.

Generic touch coordinates are pixels of the negotiated video stream, origin
at the top left. HIDC carries a USB HID report; a report descriptor (usage
0x01) is remembered and later input reports (usage 0x00) are decoded,
including an S Pen stylus collection.
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)

SAMSUNG_UIBC_PORT = 50000

# Generic Input Type ID (Table 15).
GENERIC_DOWN = 0
GENERIC_UP = 1
GENERIC_MOVE = 2
_GENERIC_TOUCH = {GENERIC_DOWN: "down", GENERIC_UP: "up", GENERIC_MOVE: "move"}

# HIDC HID Type (Table 24). Single Touch = 2, Multi Touch = 3.
HIDC_SINGLE_TOUCH = 2
HIDC_MULTI_TOUCH = 3
HIDC_USAGE_REPORT = 0
HIDC_USAGE_DESCRIPTOR = 1

GENERIC_TOUCH_TYPES = ("SingleTouch", "MultiTouch")
_HIDC_TOUCH_NAMES = ("SingleTouch", "MultiTouch")


@dataclass(frozen=True)
class Pointer:
    pointer_id: int
    x: int
    y: int
    tool: str = "finger"  # "finger" or "pen"
    down: bool = True
    pressure: int = 0


@dataclass(frozen=True)
class TouchEvent:
    """One user input. `snapshot` replaces the set of contacts (HIDC)."""

    action: str  # "down" | "move" | "up" | "snapshot"
    pointers: tuple[Pointer, ...]
    timestamp: Optional[int] = None

    @property
    def snapshot(self) -> bool:
        return self.action == "snapshot"


@dataclass(frozen=True)
class UibcCaps:
    """What the sink advertised. `usable` is the touch subset we can inject."""

    categories: tuple[str, ...]
    generic: tuple[str, ...]
    hidc: tuple[str, ...]
    raw: str = ""

    def selected_generic(self) -> tuple[str, ...]:
        have = {item.lower() for item in self.generic}
        return tuple(name for name in GENERIC_TOUCH_TYPES if name.lower() in have)

    def selected_hidc(self) -> tuple[str, ...]:
        found: list[str] = []
        for item in self.hidc:
            name, _, path = item.partition("/")
            name, path = name.strip(), path.strip()
            if name not in _HIDC_TOUCH_NAMES:
                continue
            if path not in ("USB", "BT", "Wi-Fi", "No-SP"):
                path = "USB"
            token = f"{name}/{path}"
            if token not in found:
                found.append(token)
        return tuple(found)

    @property
    def usable(self) -> bool:
        return bool(self.selected_generic() or self.selected_hidc())


def parse_uibc_capability(text: str) -> UibcCaps:
    """Parse a `wfd_uibc_capability` value. `none` is an empty capability."""
    raw = (text or "").strip()
    if not raw or raw.lower() == "none":
        return UibcCaps((), (), (), raw)

    parts: dict[str, str] = {}
    for piece in raw.split(";"):
        if "=" not in piece:
            continue
        key, value = piece.split("=", 1)
        parts[key.strip()] = value.strip()

    def items(key: str) -> tuple[str, ...]:
        value = parts.get(key, "none")
        if not value or value.lower() == "none":
            return ()
        return tuple(part.strip() for part in value.split(",") if part.strip() and part.strip().lower() != "none")

    return UibcCaps(items("input_category_list"), items("generic_cap_list"), items("hidc_cap_list"), raw)


def m4_uibc_parameters(caps: UibcCaps, port: int) -> list[str]:
    """M4 lines selecting the sink's touch categories and our TCP port.

    Empty when the sink did not advertise generic or HIDC touch: naming
    UIBC in M4 then can make the sink reject the whole parameter set.
    """
    generic = caps.selected_generic()
    hidc = caps.selected_hidc()
    advertised = {item.upper() for item in caps.categories}
    categories: list[str] = []
    if generic and ("GENERIC" in advertised or not advertised):
        categories.append("GENERIC")
    if hidc and ("HIDC" in advertised or not advertised):
        categories.append("HIDC")
    if not categories or port <= 0:
        return []
    line = (
        "wfd_uibc_capability: "
        f"input_category_list={', '.join(categories)};"
        f"generic_cap_list={', '.join(generic) if generic else 'none'};"
        f"hidc_cap_list={', '.join(hidc) if hidc else 'none'};"
        f"port={port}"
    )
    return [line, "wfd_uibc_setting: enable"]


def encode_generic_touch(
    action: str,
    pointers: list[tuple[int, int, int]],
    timestamp: Optional[int] = None,
) -> bytes:
    """One generic touch packet. `pointers` is `(id, x, y)` in stream pixels."""
    type_id = {"down": GENERIC_DOWN, "up": GENERIC_UP, "move": GENERIC_MOVE}[action]
    describe = bytes([len(pointers)])
    for pointer_id, x, y in pointers:
        describe += bytes([pointer_id & 0xFF]) + struct.pack(">HH", x & 0xFFFF, y & 0xFFFF)
    body = bytes([type_id]) + struct.pack(">H", len(describe)) + describe
    return _packet(category=0, body=body, timestamp=timestamp)


def _packet(category: int, body: bytes, timestamp: Optional[int] = None) -> bytes:
    t_flag = 1 if timestamp is not None else 0
    # Version 0 (3 bits), T, reserved 0, then reserved 0 and the category.
    head = bytes([(t_flag << 4) & 0xFF, category & 0x0F])
    ts = struct.pack(">H", timestamp & 0xFFFF) if timestamp is not None else b""
    total = 4 + len(ts) + len(body)
    pad = b"\x00" if total % 2 else b""
    total += len(pad)
    return head + struct.pack(">H", total) + ts + body + pad


def _header_length(buf: bytearray | bytes, offset: int) -> int:
    """UIBC packet length at `offset`, or 0 when those bytes are not a header."""
    if len(buf) < offset + 4:
        return 0
    version = buf[offset] >> 5
    category = buf[offset + 1] & 0x0F
    length = struct.unpack_from(">H", buf, offset + 2)[0]
    if version != 0 or category not in (0, 1):
        return 0
    if length < 4 or length > 4096 or length % 2:
        return 0
    return length


class UibcStream:
    """Byte stream → touch events. Learns a Samsung length prefix if present."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.prefix: Optional[int] = None  # 0 raw, 2 or 4 length-prefixed
        self.descriptors: dict[tuple[int, int], bytes] = {}

    def feed(self, data: bytes) -> list[TouchEvent]:
        self.buf.extend(data)
        events: list[TouchEvent] = []
        while True:
            parsed, consumed = self._pull()
            if consumed == 0:
                break
            del self.buf[:consumed]
            events.extend(parsed)
        return events

    def _pull(self) -> tuple[list[TouchEvent], int]:
        if len(self.buf) < 4:
            return [], 0
        if self.prefix is None:
            if _header_length(self.buf, 0):
                self.prefix = 0
            elif len(self.buf) >= 6 and _header_length(self.buf, 2):
                declared = struct.unpack_from(">H", self.buf, 0)[0]
                pkt = _header_length(self.buf, 2)
                if declared in (pkt, pkt + 2):
                    self.prefix = 2
                    log.info("UIBC stream uses a 2-byte length prefix (Samsung-style)")
            elif len(self.buf) >= 8 and _header_length(self.buf, 4):
                declared = struct.unpack_from(">I", self.buf, 0)[0]
                pkt = _header_length(self.buf, 4)
                if declared in (pkt, pkt + 4):
                    self.prefix = 4
                    log.info("UIBC stream uses a 4-byte length prefix")
            if self.prefix is None:
                if len(self.buf) > 1024:
                    return [], 1
                return [], 0
        assert self.prefix is not None
        offset = self.prefix
        length = _header_length(self.buf, offset)
        if not length:
            return [], 1
        if len(self.buf) < offset + length:
            return [], 0
        packet = bytes(self.buf[offset:offset + length])
        return parse_uibc_packet(packet, self.descriptors), offset + length


def parse_uibc_packet(packet: bytes, descriptors: Optional[dict[tuple[int, int], bytes]] = None) -> list[TouchEvent]:
    """Parse one UIBC packet (no length prefix). `descriptors` stores HIDC descriptors."""
    if _header_length(packet, 0) != len(packet):
        return []
    t_flag = (packet[0] >> 4) & 1
    category = packet[1] & 0x0F
    offset = 4
    timestamp: Optional[int] = None
    if t_flag:
        if len(packet) < 6:
            return []
        timestamp = struct.unpack_from(">H", packet, 4)[0]
        offset = 6
    if category == 0:
        return _parse_generic(packet, offset, timestamp)
    if category == 1:
        return _parse_hidc(packet, offset, timestamp, descriptors if descriptors is not None else {})
    return []


def _parse_generic(packet: bytes, offset: int, timestamp: Optional[int]) -> list[TouchEvent]:
    events: list[TouchEvent] = []
    end = len(packet)
    while offset + 3 <= end:
        type_id = packet[offset]
        describe_len = struct.unpack_from(">H", packet, offset + 1)[0]
        start = offset + 3
        if start + describe_len > end:
            break
        describe = packet[start:start + describe_len]
        offset = start + describe_len
        action = _GENERIC_TOUCH.get(type_id)
        if action is None or not describe:
            continue
        count = describe[0]
        pointers: list[Pointer] = []
        cursor = 1
        for _ in range(count):
            if cursor + 5 > len(describe):
                break
            pointer_id = describe[cursor]
            x, y = struct.unpack_from(">HH", describe, cursor + 1)
            pointers.append(Pointer(pointer_id, x, y, "finger", down=(action != "up")))
            cursor += 5
        if pointers:
            events.append(TouchEvent(action, tuple(pointers), timestamp))
    return events


def _parse_hidc(
    packet: bytes,
    offset: int,
    timestamp: Optional[int],
    descriptors: dict[tuple[int, int], bytes],
) -> list[TouchEvent]:
    events: list[TouchEvent] = []
    end = len(packet)
    while offset + 5 <= end:
        path = packet[offset]
        hid_type = packet[offset + 1]
        usage = packet[offset + 2]
        value_len = struct.unpack_from(">H", packet, offset + 3)[0]
        start = offset + 5
        if start + value_len > end:
            break
        value = packet[start:start + value_len]
        offset = start + value_len
        if hid_type not in (HIDC_SINGLE_TOUCH, HIDC_MULTI_TOUCH):
            continue
        key = (path, hid_type)
        if usage == HIDC_USAGE_DESCRIPTOR:
            descriptors[key] = value
            log.info("UIBC HIDC descriptor path=%s type=%s (%d bytes)", path, hid_type, len(value))
            continue
        if usage != HIDC_USAGE_REPORT:
            continue
        desc = descriptors.get(key)
        if not desc:
            log.info("UIBC HIDC report without a descriptor (path=%s type=%s, %d bytes)", path, hid_type, len(value))
            continue
        pointers = decode_hid_touch(desc, value)
        events.append(TouchEvent("snapshot", tuple(pointers), timestamp))
    return events


@dataclass(frozen=True)
class _HidField:
    page: int
    usage: int
    bit_offset: int
    bit_size: int
    logical_min: int
    logical_max: int
    report_id: int
    tool: str


def decode_hid_touch(descriptor: bytes, report: bytes, width: int = 0, height: int = 0) -> list[Pointer]:
    """Contacts in a HID touch report. X/Y are scaled into `width`×`height` when set.

    A stylus collection (digitizer usage 0x20) is an S Pen. A finger collection
    (0x22) or anything else is a finger. Tip switch 0 omits the contact.
    """
    fields = _hid_input_fields(descriptor)
    if not fields or not report:
        return []
    report_ids = {field.report_id for field in fields}
    report_id = 0
    data = report
    if any(report_ids):
        report_id = report[0]
        data = report[1:]
        fields = [field for field in fields if field.report_id == report_id]
    pointers: list[Pointer] = []
    current: dict[str, int | str] = {}

    def flush() -> None:
        if "x" not in current or "y" not in current:
            current.clear()
            return
        tip = int(current.get("tip", 1))
        if not tip:
            current.clear()
            return
        x = _scale_axis(int(current["x"]), int(current.get("xmin", 0)), int(current.get("xmax", 0)), width)
        y = _scale_axis(int(current["y"]), int(current.get("ymin", 0)), int(current.get("ymax", 0)), height)
        pointers.append(Pointer(
            int(current.get("id", len(pointers))),
            x, y,
            tool=str(current.get("tool", "finger")),
            pressure=int(current.get("pressure", 0)),
        ))
        current.clear()

    for field in fields:
        value = _hid_bits(data, field.bit_offset, field.bit_size)
        page, usage = field.page, field.usage
        if page == 0x0D and usage == 0x51:  # contact id
            if current:
                flush()
            current["id"] = value
            current["tool"] = field.tool
        elif page == 0x0D and usage == 0x42:  # tip switch
            current["tip"] = value
            current["tool"] = field.tool
        elif page == 0x01 and usage == 0x30:  # X
            if "x" in current:
                flush()
            current["x"] = value
            current["xmin"] = field.logical_min
            current["xmax"] = field.logical_max
            current.setdefault("tool", field.tool)
        elif page == 0x01 and usage == 0x31:  # Y
            current["y"] = value
            current["ymin"] = field.logical_min
            current["ymax"] = field.logical_max
        elif page == 0x0D and usage == 0x30:  # tip pressure
            current["pressure"] = value
    flush()
    return pointers


def _scale_axis(value: int, lo: int, hi: int, size: int) -> int:
    if size <= 0 or hi <= lo:
        return value
    if value < lo:
        value = lo
    if value > hi:
        value = hi
    return int((value - lo) * (size - 1) / (hi - lo))


def _hid_bits(data: bytes, offset: int, size: int) -> int:
    if size <= 0:
        return 0
    value = 0
    for bit in range(size):
        index = offset + bit
        byte = data[index // 8] if index // 8 < len(data) else 0
        if byte & (1 << (index % 8)):
            value |= 1 << bit
    return value


def _hid_input_fields(descriptor: bytes) -> list[_HidField]:
    usage_page = 0
    logical_min = 0
    logical_max = 0
    report_size = 0
    report_count = 0
    report_id = 0
    usages: list[tuple[int, int]] = []
    collection: list[tuple[int, int]] = []
    bits_at: dict[int, int] = {}
    fields: list[_HidField] = []
    i = 0
    while i < len(descriptor):
        prefix = descriptor[i]
        i += 1
        size = (0, 1, 2, 4)[prefix & 0x03]
        kind = (prefix >> 2) & 0x03
        tag = (prefix >> 4) & 0x0F
        raw = descriptor[i:i + size]
        i += size
        data = int.from_bytes(raw, "little") if raw else 0
        signed = int.from_bytes(raw, "little", signed=True) if raw else 0
        if kind == 1:  # global
            if tag == 0:
                usage_page = data
            elif tag == 1:
                logical_min = signed
            elif tag == 2:
                logical_max = signed
            elif tag == 7:
                report_size = data
            elif tag == 8:
                report_id = data
                bits_at.setdefault(report_id, 0)
            elif tag == 9:
                report_count = data
        elif kind == 2 and tag == 0:  # local usage
            usages.append((usage_page, data))
        elif kind == 0 and tag == 0xA:  # collection
            usage = usages.pop(0) if usages else (usage_page, 0)
            collection.append(usage)
            usages.clear()
        elif kind == 0 and tag == 0xC:  # end collection
            if collection:
                collection.pop()
            usages.clear()
        elif kind == 0 and tag == 0x8:  # input
            constant = bool(data & 0x01)
            tool = "finger"
            for page, usage in collection:
                if page == 0x0D and usage == 0x20:
                    tool = "pen"
                elif page == 0x0D and usage == 0x22:
                    tool = "finger"
            for _n in range(report_count):
                if not usages:
                    page, usage = 0, 0
                elif len(usages) == 1:
                    page, usage = usages[0]
                else:
                    page, usage = usages.pop(0)
                bit = bits_at.get(report_id, 0)
                if not constant:
                    fields.append(_HidField(
                        page, usage, bit, report_size, logical_min, logical_max, report_id, tool,
                    ))
                bits_at[report_id] = bit + report_size
            usages.clear()
    return fields


class UibcServer:
    """TCP listeners for negotiated UIBC and, when it differs, Samsung port 50000."""

    def __init__(
        self,
        on_event: Callable[[TouchEvent], None],
        host: str = "0.0.0.0",
        advertise_port: int = SAMSUNG_UIBC_PORT,
        samsung_port: int = SAMSUNG_UIBC_PORT,
    ) -> None:
        self.on_event = on_event
        self.host = host
        self.advertise_port = advertise_port
        self.samsung_port = samsung_port
        self.port = 0
        self.enabled = True
        self.connections = 0
        self._socks: list[socket.socket] = []
        self._stop = threading.Event()
        self._allow: Optional[set[str]] = None
        self._lock = threading.Lock()

    def allow(self, host: str) -> None:
        """After M4, only the sink (and loopback) may connect."""
        with self._lock:
            if self._allow is None:
                self._allow = {"127.0.0.1", "::1"}
            self._allow.add(host)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        log.info("UIBC %s", "enabled" if enabled else "disabled")

    def start(self) -> int:
        """Bind and return the port to put in M4. 0 when nothing is listening."""
        primary = self._listen(self.advertise_port, "uibc")
        if primary is None and self.advertise_port != 0:
            primary = self._listen(0, "uibc")
        if primary is None:
            return 0
        self.port = primary.getsockname()[1]
        log.info("UIBC listening on %s:%s", self.host, self.port)
        if self.samsung_port and self.samsung_port != self.port:
            extra = self._listen(self.samsung_port, "samsung-uibc")
            if extra is None:
                log.info(
                    "Samsung touch port %s is not available; events stay on the negotiated port %s",
                    self.samsung_port, self.port,
                )
            else:
                log.info("Samsung touch fallback listening on TCP %s", self.samsung_port)
        return self.port

    def _listen(self, port: int, name: str) -> Optional[socket.socket]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, port))
            sock.listen(2)
            sock.settimeout(0.5)
        except OSError as exc:
            log.info("UIBC bind %s:%s (%s) failed: %s", self.host, port, name, exc)
            sock.close()
            return None
        self._socks.append(sock)
        threading.Thread(target=self._accept, args=(sock, name), name=name, daemon=True).start()
        return sock

    def _accept(self, sock: socket.socket, name: str) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            host = addr[0]
            with self._lock:
                allowed = None if self._allow is None else set(self._allow)
            if allowed is not None and host not in allowed:
                log.info("UIBC reject %s on %s (not the WFD sink)", host, name)
                conn.close()
                continue
            self.connections += 1
            log.info("UIBC connected from %s (%s)", host, name)
            threading.Thread(target=self._read, args=(conn, name), name=f"{name}-read", daemon=True).start()

    def _read(self, conn: socket.socket, name: str) -> None:
        stream = UibcStream()
        conn.settimeout(0.5)
        try:
            while not self._stop.is_set():
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                for event in stream.feed(data):
                    if not self.enabled:
                        continue
                    try:
                        self.on_event(event)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("touch event dropped: %s", exc)
        except OSError as exc:
            log.info("UIBC %s read ended: %s", name, exc)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            log.info("UIBC %s closed", name)

    def stop(self) -> None:
        self._stop.set()
        for sock in self._socks:
            try:
                sock.close()
            except OSError:
                pass
        self._socks.clear()
