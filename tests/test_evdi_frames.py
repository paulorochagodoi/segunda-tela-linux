"""EVDI pixel format and damage-rect copies.

A red RGBx pixel parsed as bgrx is blue. That is the swap the tablet showed
while rawvideoparse was tagged bgrx. The grab is therefore rgbx, and a
test pattern has to come back red.
"""
from __future__ import annotations

import ctypes
import shutil
import subprocess

import pytest

from segunda_tela.display.evdi import EVDI_RAW_FORMAT, ShadowFrames, blit_damage, fourcc_str
from segunda_tela.wfd.gst_run import FpsMeter


def test_fourcc_and_grab_format():
    # DRM_FORMAT_XRGB8888 is 'X','R','2','4' little-endian. We still parse as
    # rgbx: that fourcc's memory layout is bgrx, and bgrx was the swapped one.
    assert fourcc_str(0x34325258) == "XR24"
    assert EVDI_RAW_FORMAT == "rgbx"


def test_blit_damage_copies_only_the_rect():
    width, height = 4, 2
    src = bytearray(width * height * 4)
    src[0:4] = bytes((255, 0, 0, 255))  # red at (0, 0), RGBx
    dest = bytearray(width * height * 4)
    copied = blit_damage(dest, src, width, height, [(0, 0, 1, 1)])
    assert copied == 4
    assert dest[0:4] == bytes((255, 0, 0, 255))
    assert dest[4:] == bytes(len(dest) - 4)
    assert blit_damage(dest, src, width, height, []) == 0
    assert blit_damage(dest, src, width, height, [(3, 1, 1, 0)]) == 0


def test_blit_damage_full_rect_is_one_copy_from_ctypes():
    width, height = 2, 2
    raw = (ctypes.c_char * (width * height * 4))()
    raw[:] = bytes((0, 0, 255, 255)) * (width * height)
    dest = bytearray(width * height * 4)
    assert blit_damage(dest, memoryview(raw), width, height, [(0, 0, width, height)]) == width * height * 4
    assert dest == bytes(raw)


def test_shadow_stays_stable_while_the_feeder_holds_it():
    width, height = 4, 2
    frames = ShadowFrames(width, height)
    red = bytes((255, 0, 0, 255)) * (width * height)
    assert frames.apply(red, [(0, 0, width, height)])
    held = frames.acquire()
    assert held is not None and bytes(held[0:4]) == bytes((255, 0, 0, 255))
    blue = bytearray(red)
    blue[0:4] = bytes((0, 0, 255, 255))
    # One pixel changed while the feeder still owns the published buffer.
    assert frames.apply(blue, [(0, 0, 1, 1)])
    assert bytes(held[0:4]) == bytes((255, 0, 0, 255))
    frames.release()
    latest = frames.acquire()
    assert latest is not None
    assert bytes(latest[0:4]) == bytes((0, 0, 255, 255))
    # The rest of the frame survived the partial update (full copy of the
    # stale spare, which came from the kernel buffer).
    assert bytes(latest[4:8]) == bytes((255, 0, 0, 255))
    frames.release()


def test_shadow_partial_update_without_a_reader():
    width, height = 4, 1
    frames = ShadowFrames(width, height)
    src = bytearray((255, 0, 0, 255)) * (width * height)
    frames.apply(src, [(0, 0, width, height)])
    src[4:8] = bytes((0, 255, 0, 255))
    assert frames.apply(src, [(1, 0, 2, 1)])
    view = frames.acquire()
    assert view is not None
    assert bytes(view[0:4]) == bytes((255, 0, 0, 255))
    assert bytes(view[4:8]) == bytes((0, 255, 0, 255))
    frames.release()
    # No damage: the feeder keeps repeating the previous frame.
    assert frames.apply(src, []) is False


def test_shadow_view_roundtrips_through_a_pipe():
    import os

    frames = ShadowFrames(2, 1)
    src = bytes((255, 0, 0, 255, 0, 255, 0, 255))
    assert frames.apply(src, [(0, 0, 2, 1)])
    read_fd, write_fd = os.pipe()
    view = frames.acquire()
    assert view is not None
    os.write(write_fd, view)
    frames.release()
    assert os.read(read_fd, 8) == src
    os.close(read_fd)
    os.close(write_fd)


def test_fps_meter_reports_every_five_seconds():
    clock = {"t": 0.0}
    meter = FpsMeter(5.0, now=lambda: clock["t"])
    assert meter.tick() is None
    clock["t"] = 4.9
    assert meter.tick() is None
    clock["t"] = 5.0
    msg = meter.tick()
    assert msg is not None
    assert msg.startswith("stream fps 0.6")
    assert "3 frames in 5.0s" in msg
    assert meter.tick() is None


def _decode_raw(fmt: str, raw: bytes, tmp_path) -> bytes:
    """Parse `raw` as `fmt` and return packed RGBx (R,G,B,X per pixel)."""
    src = tmp_path / f"in-{fmt}.raw"
    dst = tmp_path / f"out-{fmt}.rgbx"
    src.write_bytes(raw)
    proc = subprocess.run(
        [
            "gst-launch-1.0", "-q",
            "filesrc", f"location={src}",
            "!", "rawvideoparse", "use-sink-caps=false",
            "width=4", "height=1", f"format={fmt}", "framerate=1/1",
            "!", "videoconvert",
            "!", "video/x-raw,format=RGBx",
            "!", "filesink", f"location={dst}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return dst.read_bytes()


def test_rgbx_test_pattern_keeps_red_and_bgrx_swaps_it(tmp_path):
    """One row: red, green, blue, white. Parsed as rgbx it stays; as bgrx, R/B swap."""
    if not shutil.which("gst-launch-1.0"):
        pytest.skip("gst-launch")
    red = bytes((255, 0, 0, 255))
    green = bytes((0, 255, 0, 255))
    blue = bytes((0, 0, 255, 255))
    white = bytes((255, 255, 255, 255))
    pixels = red + green + blue + white
    as_rgbx = _decode_raw("rgbx", pixels, tmp_path)
    as_bgrx = _decode_raw("bgrx", pixels, tmp_path)
    assert as_rgbx[:16] == pixels
    # The tablet bug: a red pixel tagged bgrx is blue.
    assert as_bgrx[0:4] == bytes((0, 0, 255, 255))
    assert as_bgrx[8:12] == bytes((255, 0, 0, 255))
    assert EVDI_RAW_FORMAT == "rgbx"
