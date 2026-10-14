"""RFB 3.8 server fed by an RGBx framebuffer.

One listening socket per bind address (127.0.0.1, plus the tether IP when
that path is the one in use). Clients authenticate with a VNC password.
Updates are damage rectangles, Tight or ZRLE as the client prefers, capped
at `fps` so a quiet desktop sends nothing and a busy one stays near the cap.
"""
from __future__ import annotations

import logging
import os
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .auth import vnc_response
from .encode import (
    ENCODING_CONTINUOUS,
    ENCODING_DESKTOP_SIZE,
    ENCODING_RAW,
    ENCODING_TIGHT,
    PixelFormat,
    ZrleEncoder,
    coalesce_rects,
    composite_cursor,
    crop_rgbx,
    cursor_bounds,
    encode_rect,
    tight_slices,
    parse_pixel_format,
    pick_encoding,
    pixel_format_bytes,
    xywh,
)

log = logging.getLogger(__name__)

PointerFn = Callable[[int, int, int], None]
_SERVER_VERSION = b"RFB 003.008\n"


@dataclass
class _Client:
    sock: socket.socket
    addr: tuple
    wake_r: int
    wake_w: int
    buf: bytearray = field(default_factory=bytearray)
    fmt: PixelFormat = field(default_factory=PixelFormat.rgbx)
    encodings: list[int] = field(default_factory=lambda: [ENCODING_RAW])
    encoding: int = ENCODING_RAW
    zrle: ZrleEncoder = field(default_factory=ZrleEncoder)
    dirty: list[tuple[int, int, int, int]] = field(default_factory=list)
    # Outstanding FramebufferUpdateRequests. An incremental request with no
    # damage is kept here until a rect exists — an empty reply makes bVNC
    # wait out its defer timer, so the picture freezes between grabs.
    pending_requests: int = 0
    incremental: bool = True
    force_full: bool = False
    continuous: bool = False
    need_desktop_size: bool = False
    next_send: float = 0.0
    alive: bool = True
    thread: Optional[threading.Thread] = None
    updates: int = 0
    bytes_out: int = 0


class VncServer:
    def __init__(
        self,
        password: str,
        width: int,
        height: int,
        hosts: list[str],
        port: int = 5900,
        fps: int = 60,
        name: str = "Segunda Tela",
        on_pointer: Optional[PointerFn] = None,
        challenge_fn: Optional[Callable[[int], bytes]] = None,
    ) -> None:
        if width < 1 or height < 1:
            raise ValueError("framebuffer size")
        self.password = password
        self.w = width
        self.h = height
        self.hosts = list(hosts)
        self.port = port
        self.fps = max(1, fps)
        self.name = name.encode("utf-8")
        self.on_pointer = on_pointer
        self._challenge_fn = challenge_fn or os.urandom
        self.fb = bytearray(width * height * 4)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._listens: list[socket.socket] = []
        self._clients: list[_Client] = []
        self._accept: Optional[threading.Thread] = None
        self._stat_t0 = time.monotonic()
        self._stat_updates = 0
        self._stat_bytes = 0
        self._stat_pointers = 0
        self._cursor: Optional[tuple[int, int]] = None
        self._logged_format = False
        self._logged_pointer = False
        self.on_resize: Optional[Callable[[int, int], None]] = None

    # ----------------------------------------------------------------- lifecycle
    def start(self) -> None:
        port = self.port
        for host in self.hosts:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            if port == 0:
                port = sock.getsockname()[1]
            sock.listen(2)
            sock.setblocking(False)
            self._listens.append(sock)
            log.info("VNC escutando %s:%s", host, port)
        self.port = port
        self._accept = threading.Thread(target=self._accept_loop, name="vnc-accept", daemon=True)
        self._accept.start()

    def stop(self) -> None:
        self._stop.set()
        for sock in self._listens:
            try:
                sock.close()
            except OSError:
                pass
        self._listens.clear()
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            client.alive = False
            self._wake(client)
            try:
                client.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.sock.close()
            except OSError:
                pass
        if self._accept:
            self._accept.join(timeout=2)
            self._accept = None
        for client in clients:
            if client.thread:
                client.thread.join(timeout=1)
        with self._lock:
            self._clients.clear()

    def submit(
        self,
        src: bytes | bytearray | memoryview,
        rects: list[tuple[int, int, int, int]],
        width: int,
        height: int,
    ) -> None:
        """Copy `rects` (exclusive-end) from an RGBx frame into the framebuffer."""
        from ..display.evdi import blit_damage

        resize_to: Optional[tuple[int, int]] = None
        with self._lock:
            if width != self.w or height != self.h:
                self.w, self.h = width, height
                self.fb = bytearray(width * height * 4)
                rects = [(0, 0, width, height)]
                for client in self._clients:
                    client.dirty = [(0, 0, width, height)]
                    client.need_desktop_size = True
                resize_to = (width, height)
            if not rects:
                return
            blit_damage(self.fb, src, width, height, rects)
            wakes = []
            for client in self._clients:
                if not client.alive:
                    continue
                client.dirty.extend(rects)
                wakes.append(client)
        for client in wakes:
            self._wake(client)
        if resize_to and self.on_resize:
            try:
                self.on_resize(*resize_to)
            except Exception as exc:  # noqa: BLE001
                log.warning("on_resize: %s", exc)

    # ----------------------------------------------------------------- accept
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            if not self._listens:
                return
            try:
                ready, _, _ = select.select(self._listens, [], [], 0.5)
            except (OSError, ValueError):
                return
            for sock in ready:
                try:
                    conn, addr = sock.accept()
                except (BlockingIOError, OSError):
                    continue
                self._spawn(conn, addr)

    def _spawn(self, conn: socket.socket, addr) -> None:
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        wake_r, wake_w = os.pipe()
        os.set_blocking(wake_w, False)
        client = _Client(sock=conn, addr=addr, wake_r=wake_r, wake_w=wake_w)
        client.dirty.append((0, 0, self.w, self.h))
        client.thread = threading.Thread(
            target=self._client_loop, args=(client,), name=f"vnc-{addr}", daemon=True,
        )
        with self._lock:
            self._clients.append(client)
        log.info("VNC cliente %s", addr)
        client.thread.start()

    def _wake(self, client: _Client) -> None:
        try:
            os.write(client.wake_w, b"\0")
        except (OSError, BlockingIOError):
            pass

    def _drop(self, client: _Client) -> None:
        client.alive = False
        for fd in (client.wake_r, client.wake_w):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            client.sock.close()
        except OSError:
            pass
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
        log.info("VNC cliente saiu %s (updates=%s bytes=%s)", client.addr, client.updates, client.bytes_out)

    # ----------------------------------------------------------------- protocol
    def _client_loop(self, client: _Client) -> None:
        sock = client.sock
        try:
            sock.settimeout(15)
            self._handshake(client)
            sock.settimeout(None)
            while client.alive and not self._stop.is_set():
                if client.buf and not self._drain(client):
                    break
                self._flush(client)
                timeout = self._wait_timeout(client)
                try:
                    ready, _, _ = select.select([sock, client.wake_r], [], [], timeout)
                except (OSError, ValueError):
                    break
                if client.wake_r in ready:
                    try:
                        os.read(client.wake_r, 256)
                    except OSError:
                        break
                if sock in ready:
                    try:
                        chunk = sock.recv(65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    client.buf += chunk
                    if not self._drain(client):
                        break
                self._flush(client)
        except (OSError, EOFError, ValueError) as exc:
            log.debug("VNC %s: %s", client.addr, exc)
        finally:
            self._drop(client)

    def _recv(self, client: _Client, n: int) -> bytes:
        while len(client.buf) < n:
            chunk = client.sock.recv(max(4096, n - len(client.buf)))
            if not chunk:
                raise EOFError("cliente fechou")
            client.buf += chunk
        out = bytes(client.buf[:n])
        del client.buf[:n]
        return out

    def _handshake(self, client: _Client) -> None:
        client.sock.sendall(_SERVER_VERSION)
        client_ver = self._recv(client, 12)
        if not client_ver.startswith(b"RFB 003.00"):
            raise ValueError(f"versão RFB {client_ver!r}")
        minor = client_ver[10]
        # '3','7','8' — anything else we still try as 3.8 if it asked for 8.
        version = 8 if minor == ord("8") else 7 if minor == ord("7") else 3
        challenge = self._challenge_fn(16)
        if len(challenge) != 16:
            raise ValueError("challenge")
        if version == 3:
            client.sock.sendall(struct.pack(">I", 2))
        else:
            client.sock.sendall(bytes((1, 2)))
            chosen = self._recv(client, 1)[0]
            if chosen != 2:
                raise ValueError(f"security type {chosen}")
        client.sock.sendall(challenge)
        got = self._recv(client, 16)
        if got != vnc_response(self.password, challenge):
            if version >= 8:
                client.sock.sendall(struct.pack(">I", 1))
            raise ValueError("senha VNC recusada")
        if version >= 8:
            client.sock.sendall(struct.pack(">I", 0))
        self._recv(client, 1)  # shared-flag
        fmt = PixelFormat.rgbx()
        client.fmt = fmt
        with self._lock:
            width, height = self.w, self.h
        body = struct.pack(">HH", width, height) + pixel_format_bytes(fmt)
        body += struct.pack(">I", len(self.name)) + self.name
        client.sock.sendall(body)

    def _drain(self, client: _Client) -> bool:
        while client.buf and client.alive:
            kind = client.buf[0]
            if kind == 0:
                if len(client.buf) < 20:
                    return True
                raw = self._recv(client, 20)
                client.fmt = parse_pixel_format(raw[4:20])
                fmt = client.fmt
                log.info(
                    "VNC SetPixelFormat bpp=%s depth=%s big_endian=%s shifts r=%s g=%s b=%s",
                    fmt.bpp, fmt.depth, fmt.big_endian, fmt.red_shift, fmt.green_shift, fmt.blue_shift,
                )
            elif kind == 2:
                if len(client.buf) < 4:
                    return True
                count = struct.unpack_from(">H", client.buf, 2)[0]
                need = 4 + 4 * count
                if len(client.buf) < need:
                    return True
                raw = self._recv(client, need)
                encs = list(struct.unpack(f">{count}i", raw[4:]))
                client.encodings = encs
                client.encoding = pick_encoding(encs)
                log.info("VNC encodings prefer %s (oferecidas %s)", client.encoding, encs[:8])
                if ENCODING_CONTINUOUS in encs and not client.continuous:
                    # The enable message (150) is authoritative; listing it
                    # only records interest. Some clients push frames only
                    # after 150, which `_drain` handles below.
                    pass
            elif kind == 3:
                if len(client.buf) < 10:
                    return True
                raw = self._recv(client, 10)
                _x, _y, _w, _h = struct.unpack_from(">HHHH", raw, 2)
                client.incremental = bool(raw[1])
                client.pending_requests = min(8, client.pending_requests + 1)
                if not client.incremental:
                    client.force_full = True
                del _x, _y, _w, _h
            elif kind == 4:
                if len(client.buf) < 8:
                    return True
                self._recv(client, 8)  # key event: touch only
            elif kind == 5:
                if len(client.buf) < 6:
                    return True
                raw = self._recv(client, 6)
                buttons, x, y = raw[1], struct.unpack_from(">H", raw, 2)[0], struct.unpack_from(">H", raw, 4)[0]
                self._pointer(x, y, buttons)
            elif kind == 6:
                if len(client.buf) < 8:
                    return True
                length = struct.unpack_from(">I", client.buf, 4)[0]
                if len(client.buf) < 8 + length:
                    return True
                self._recv(client, 8 + length)
            elif kind == 150:
                if len(client.buf) < 10:
                    return True
                raw = self._recv(client, 10)
                client.continuous = bool(raw[1])
            else:
                log.warning("VNC mensagem desconhecida %s — fechando o cliente", kind)
                return False
        return True

    def _wait_timeout(self, client: _Client) -> float:
        if self._wants_send(client) and time.monotonic() < client.next_send:
            return max(0.0, client.next_send - time.monotonic())
        return 0.5

    def _pointer(self, x: int, y: int, buttons: int) -> None:
        """Inject the finger and dirty the sprite so the tablet shows the cursor."""
        with self._lock:
            width, height = self.w, self.h
        x = 0 if x < 0 else width - 1 if x >= width else x
        y = 0 if y < 0 else height - 1 if y >= height else y
        if self.on_pointer:
            try:
                self.on_pointer(x, y, buttons)
            except Exception as exc:  # noqa: BLE001
                log.warning("pointer: %s", exc)
        wakes: list[_Client] = []
        with self._lock:
            old = self._cursor
            self._cursor = (x, y)
            rects = []
            for pos in (old, (x, y)):
                if pos is None:
                    continue
                bounds = cursor_bounds(pos[0], pos[1], self.w, self.h)
                if bounds[2] > bounds[0] and bounds[3] > bounds[1]:
                    rects.append(bounds)
            for client in self._clients:
                if not client.alive:
                    continue
                client.dirty.extend(rects)
                wakes.append(client)
        for client in wakes:
            self._wake(client)
        self._stat_pointers += 1
        if not self._logged_pointer:
            self._logged_pointer = True
            log.info("VNC pointer x=%s y=%s buttons=%s (uinput se o toque abriu)", x, y, buttons)
        else:
            log.debug("VNC pointer x=%s y=%s buttons=%s", x, y, buttons)

    def _wants_send(self, client: _Client) -> bool:
        if not (client.continuous or client.pending_requests):
            return False
        return bool(client.dirty or client.force_full or client.need_desktop_size)

    def _flush(self, client: _Client) -> None:
        if not client.alive:
            return
        now = time.monotonic()
        if now < client.next_send and not client.force_full:
            return
        with self._lock:
            if not (client.continuous or client.pending_requests):
                return
            width, height = self.w, self.h
            cursor = self._cursor
            if client.force_full:
                areas = [(0, 0, width, height)]
                client.dirty.clear()
                client.force_full = False
            elif client.dirty:
                pending = list(client.dirty)
                if cursor is not None:
                    bounds = cursor_bounds(cursor[0], cursor[1], width, height)
                    if bounds[2] > bounds[0]:
                        pending.append(bounds)
                areas = xywh(coalesce_rects(pending, width, height))
                client.dirty.clear()
            elif client.need_desktop_size:
                areas = []
            else:
                # Request is outstanding and the framebuffer has not changed.
                # Keep it. The next submit() or pointer wakes this client.
                return
            desktop = None
            if client.need_desktop_size and ENCODING_DESKTOP_SIZE in client.encodings:
                desktop = (width, height)
            client.need_desktop_size = False
            snaps = [crop_rgbx(self.fb, width, x, y, w, h) for x, y, w, h in areas]
            fmt = client.fmt
            encoding = client.encoding
            zrle = client.zrle
            if not client.continuous and client.pending_requests:
                client.pending_requests -= 1
        if cursor is not None:
            snaps = [
                composite_cursor(snap, w, h, x, y, cursor[0], cursor[1])
                for (x, y, w, h), snap in zip(areas, snaps)
            ]
        if not areas and desktop is None:
            return
        t0 = time.perf_counter()
        pieces: list[tuple[int, int, int, int, int, bytes]] = []
        if desktop:
            pieces.append((0, 0, desktop[0], desktop[1], ENCODING_DESKTOP_SIZE, b""))
        for (x, y, w, h), rgbx in zip(areas, snaps):
            if w <= 0 or h <= 0:
                continue
            if encoding == ENCODING_TIGHT:
                for _sx, sy, sw, sh, payload in tight_slices(rgbx, w, h, fmt):
                    pieces.append((x, y + sy, sw, sh, ENCODING_TIGHT, payload))
                continue
            enc, payload = encode_rect(rgbx, w, h, fmt, encoding, zrle=zrle)
            pieces.append((x, y, w, h, enc, payload))
        if not pieces:
            with self._lock:
                if not client.continuous:
                    client.pending_requests = min(8, client.pending_requests + 1)
            return
        msg = bytearray(struct.pack(">BBH", 0, 0, len(pieces)))
        for x, y, w, h, enc, payload in pieces:
            msg += struct.pack(">HHHHi", x, y, w, h, enc)
            msg += payload
        try:
            client.sock.sendall(msg)
        except OSError:
            client.alive = False
            return
        client.updates += 1
        client.bytes_out += len(msg)
        encode_s = time.perf_counter() - t0
        if encode_s >= 0.05:
            log.info("VNC encode %.0f ms, %d rects, %d KB", encode_s * 1000, len(pieces), len(msg) // 1024)
        client.next_send = time.monotonic() + 1.0 / self.fps
        self._stat_updates += 1
        self._stat_bytes += len(msg)
        self._log_fps()

    def _log_fps(self) -> None:
        now = time.monotonic()
        dt = now - self._stat_t0
        if dt < 5.0:
            return
        log.info(
            "VNC fps %.1f (%d updates, %.0f KB in %.1fs, %d pointer)",
            self._stat_updates / dt, self._stat_updates, self._stat_bytes / 1024, dt,
            self._stat_pointers,
        )
        self._stat_updates = 0
        self._stat_bytes = 0
        self._stat_pointers = 0
        self._stat_t0 = now
