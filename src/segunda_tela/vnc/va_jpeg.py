"""Tight JPEG via GStreamer VA-API (`vapostproc ! vajpegenc`).

One long-lived `gst-launch-1.0` process per size. The frame is RGB on stdin
of the pipeline (an fd) and one JPEG (SOI…EOI) comes back. If the plugin or
the GPU is missing, `encode` returns None and the caller uses libjpeg.

bVNC has no H.264 rectangle. This is the hardware path that client can show.
"""
from __future__ import annotations

import logging
import os
import select
import shutil
import subprocess
import threading
import time

log = logging.getLogger(__name__)

# Full-screen-sized damage is where CPU JPEG was ~18 fps. Smaller windows
# already encode in a few milliseconds with libjpeg-turbo.
VA_MIN_PIXELS = 640 * 480


def _gst_exists(name: str) -> bool:
    if not shutil.which("gst-inspect-1.0"):
        return False
    try:
        return subprocess.run(
            ["gst-inspect-1.0", "--exists", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def va_jpeg_elements_present() -> bool:
    return (
        _gst_exists("vajpegenc")
        and _gst_exists("vapostproc")
        and _gst_exists("rawvideoparse")
        and bool(shutil.which("gst-launch-1.0"))
    )


def va_jpeg_command(width: int, height: int, quality: int, in_fd: int, out_fd: int) -> list[str]:
    """Pipeline argv. RGB in, JPEG out. `quality` is 0–100."""
    frame = width * height * 3
    return [
        "gst-launch-1.0",
        "fdsrc", f"fd={in_fd}", f"blocksize={frame}",
        "!", "rawvideoparse", f"width={width}", f"height={height}", "format=rgb", "framerate=30/1",
        "!", "vapostproc",
        "!", "vajpegenc", f"quality={max(1, min(100, int(quality)))}",
        "!", "fdsink", f"fd={out_fd}", "sync=false",
    ]


class _VaJpegPipe:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._in_w: int | None = None
        self._out_r: int | None = None
        self._size: tuple[int, int, int] | None = None
        self._disabled = False
        self._stderr = ""
        self._out_buf = bytearray()

    @property
    def disabled(self) -> bool:
        return self._disabled

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        for fd in (self._in_w, self._out_r):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._in_w = None
        self._out_r = None
        self._size = None
        self._out_buf = bytearray()
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass

    def encode(self, rgb: bytes, width: int, height: int, quality: int) -> bytes | None:
        if self._disabled or width < 1 or height < 1 or len(rgb) < width * height * 3:
            return None
        with self._lock:
            if self._disabled:
                return None
            key = (width, height, int(quality))
            if self._size != key or self._proc is None or self._proc.poll() is not None:
                self.close()
                if not self._start(width, height, quality):
                    self._disabled = True
                    return None
            assert self._in_w is not None and self._out_r is not None
            blob = self._exchange(rgb[: width * height * 3])
            if not blob or blob[:2] != b"\xff\xd8":
                err = (self._stderr or "sem JPEG").strip().splitlines()
                detail = err[-1] if err else "sem JPEG"
                log.info("VA-API JPEG falhou (%s); Tight JPEG segue na CPU", detail[:200])
                self._disabled = True
                self.close()
                return None
            return blob

    def _start(self, width: int, height: int, quality: int) -> bool:
        if not va_jpeg_elements_present():
            log.info("VA-API JPEG indisponível (vajpegenc/vapostproc); Tight JPEG na CPU")
            return False
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        cmd = va_jpeg_command(width, height, quality, in_r, out_w)
        try:
            proc = subprocess.Popen(
                cmd,
                pass_fds=(in_r, out_w),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            os.close(in_r)
            os.close(in_w)
            os.close(out_r)
            os.close(out_w)
            log.info("VA-API JPEG não iniciou (%s); Tight JPEG na CPU", exc)
            return False
        os.close(in_r)
        os.close(out_w)
        try:
            # One 1920×1200 RGB frame is ~7 MB. The default pipe is 64 KB.
            import fcntl
            fcntl.fcntl(in_w, 1031, 8 * 1024 * 1024)  # F_SETPIPE_SZ
        except OSError:
            pass
        self._proc = proc
        self._in_w = in_w
        self._out_r = out_r
        self._size = (width, height, int(quality))
        self._stderr = ""
        threading.Thread(target=self._drain_stderr, args=(proc,), name="va-jpeg-err", daemon=True).start()
        log.info(
            "VNC encoder ativo: VA-API JPEG (vajpegenc quality=%s, %sx%s)",
            quality, width, height,
        )
        return True

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        pipe = proc.stderr
        if pipe is None:
            return
        chunks: list[bytes] = []
        try:
            while True:
                data = pipe.read(4096)
                if not data:
                    break
                chunks.append(data)
                if sum(len(c) for c in chunks) > 8192:
                    break
        except OSError:
            pass
        self._stderr = b"".join(chunks).decode("utf-8", "replace")

    def _exchange(self, rgb: bytes) -> bytes | None:
        """Write one RGB frame, then read one JPEG.

        `rawvideoparse` only pushes a buffer after the full frame, so the
        JPEG is not produced until the write finishes. The write is
        non-blocking: a pipeline that never reads must not freeze the VNC
        thread (the default pipe is 64 KB and `F_SETPIPE_SZ` often cannot
        grow past 1 MB).
        """
        assert self._in_w is not None and self._out_r is not None
        if not self._write_frame(rgb, time.monotonic() + 3.0):
            return None
        return self._read_jpeg(time.monotonic() + 2.0) or None

    def _write_frame(self, rgb: bytes, deadline: float) -> bool:
        assert self._in_w is not None
        import fcntl
        try:
            flags = fcntl.fcntl(self._in_w, fcntl.F_GETFL)
            fcntl.fcntl(self._in_w, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        except OSError:
            return False
        view = memoryview(rgb)
        while view:
            if time.monotonic() >= deadline:
                return False
            try:
                wrote = os.write(self._in_w, view)
            except BlockingIOError:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    return False
                try:
                    _, ready, _ = select.select([], [self._in_w], [], min(0.2, remain))
                except (OSError, ValueError):
                    return False
                if not ready and self._proc is not None and self._proc.poll() is not None:
                    return False
                continue
            except OSError:
                return False
            if wrote <= 0:
                return False
            view = view[wrote:]
        return True

    def _read_jpeg(self, deadline: float) -> bytes:
        assert self._out_r is not None
        buf = self._out_buf
        while time.monotonic() < deadline:
            start = buf.find(b"\xff\xd8")
            end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
            if start >= 0 and end > start:
                blob = bytes(buf[start:end + 2])
                del buf[:end + 2]
                return blob
            if start > 0:
                del buf[:start]
            try:
                ready, _, _ = select.select([self._out_r], [], [], 0.2)
            except (OSError, ValueError):
                return b""
            if not ready:
                if self._proc is not None and self._proc.poll() is not None:
                    return b""
                continue
            try:
                chunk = os.read(self._out_r, 65536)
            except OSError:
                return b""
            if not chunk:
                return b""
            buf += chunk
        return b""


_PIPE = _VaJpegPipe()


def encode_va_jpeg(rgb: bytes, width: int, height: int, quality: int) -> bytes | None:
    if width * height < VA_MIN_PIXELS:
        return None
    return _PIPE.encode(rgb, width, height, quality)


def va_jpeg_disabled() -> bool:
    return _PIPE.disabled


def close_va_jpeg() -> None:
    _PIPE.close()
