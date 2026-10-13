"""Minimal EDID 1.4 builder/parser for the EVDI virtual monitor.

Timings use VESA CVT reduced blanking (v1), which is what real 16:10 panels
and DisplayPort sinks typically report. Only the 128-byte base block is
produced; no extension blocks are needed for the modes we offer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

EDID_HEADER = b"\x00\xff\xff\xff\xff\xff\xff\x00"

# Tablet-ish defaults: a 11" 16:10 panel (Galaxy Tab S-class).
DEFAULT_MODES: tuple[tuple[int, int, int], ...] = ((1920, 1200, 60), (2560, 1600, 60), (1920, 1080, 60))
DEFAULT_SIZE_MM = (237, 148)
DEFAULT_NAME = "Segunda Tela"
DEFAULT_VENDOR = "SGT"  # 3-letter PNP-style id; not a registered vendor


@dataclass(frozen=True)
class Timing:
    width: int
    height: int
    refresh: int
    pixel_clock_khz: int
    h_total: int
    h_front: int
    h_sync: int
    v_total: int
    v_front: int
    v_sync: int
    hsync_positive: bool = True
    vsync_positive: bool = False

    @property
    def h_blank(self) -> int:
        return self.h_total - self.width

    @property
    def v_blank(self) -> int:
        return self.v_total - self.height

    @property
    def exact_refresh(self) -> float:
        return self.pixel_clock_khz * 1000 / (self.h_total * self.v_total)


def _cvt_vsync_lines(width: int, height: int) -> int:
    for (aw, ah), lines in (((4, 3), 4), ((16, 9), 5), ((16, 10), 6), ((5, 4), 7), ((15, 9), 7)):
        if width * ah == height * aw:
            return lines
    return 10


def cvt_rb(width: int, height: int, refresh: int = 60) -> Timing:
    """VESA CVT v1 reduced-blanking timing."""
    h_pixels = (width // 8) * 8
    v_sync = _cvt_vsync_lines(width, height)
    min_vblank_us = 460.0
    v_front, min_v_bporch = 3, 6
    h_period_est = ((1_000_000.0 / refresh) - min_vblank_us) / height
    vbi = math.floor(min_vblank_us / h_period_est) + 1
    vbi = max(vbi, v_front + v_sync + min_v_bporch)
    v_total = height + vbi
    h_total = h_pixels + 160
    clock_mhz = 0.25 * math.floor((refresh * v_total * h_total / 1_000_000.0) / 0.25)
    return Timing(
        width=h_pixels,
        height=height,
        refresh=refresh,
        pixel_clock_khz=int(round(clock_mhz * 1000)),
        h_total=h_total,
        h_front=48,
        h_sync=32,
        v_total=v_total,
        v_front=v_front,
        v_sync=v_sync,
        hsync_positive=True,
        vsync_positive=False,
    )


def _dtd(t: Timing, size_mm: tuple[int, int]) -> bytes:
    clk = t.pixel_clock_khz // 10
    hb, vb = t.h_blank, t.v_blank
    hmm, vmm = size_mm
    flags = 0x18 | (0x04 if t.vsync_positive else 0) | (0x02 if t.hsync_positive else 0)
    return bytes(
        [
            clk & 0xFF,
            clk >> 8,
            t.width & 0xFF,
            hb & 0xFF,
            ((t.width >> 8) << 4) | (hb >> 8),
            t.height & 0xFF,
            vb & 0xFF,
            ((t.height >> 8) << 4) | (vb >> 8),
            t.h_front & 0xFF,
            t.h_sync & 0xFF,
            ((t.v_front & 0xF) << 4) | (t.v_sync & 0xF),
            ((t.h_front >> 8) << 6) | ((t.h_sync >> 8) << 4) | ((t.v_front >> 4) << 2) | (t.v_sync >> 4),
            hmm & 0xFF,
            vmm & 0xFF,
            ((hmm >> 8) << 4) | (vmm >> 8),
            0,
            0,
            flags,
        ]
    )


def _text_descriptor(tag: int, text: str) -> bytes:
    data = text.encode("ascii", "replace")[:13]
    if len(data) < 13:
        data += b"\n" + b" " * (12 - len(data))
    return bytes([0, 0, 0, tag, 0]) + data


def _range_descriptor(timings: Iterable[Timing]) -> bytes:
    ts = list(timings)
    max_h_khz = max(math.ceil(t.pixel_clock_khz / t.h_total) for t in ts) + 1
    max_clock_10mhz = math.ceil(max(t.pixel_clock_khz for t in ts) / 10_000)
    return bytes(
        [0, 0, 0, 0xFD, 0, 24, 75, 30, min(255, max_h_khz), min(255, max_clock_10mhz), 0x01, 0x0A]
        + [0x20] * 6
    )


def _standard_timing(width: int, height: int, refresh: int) -> Optional[bytes]:
    if not 256 <= width <= 2288 or width % 8 or not 60 <= refresh <= 123:
        return None
    for code, (aw, ah) in ((0, (16, 10)), (1, (4, 3)), (2, (5, 4)), (3, (16, 9))):
        if width * ah == height * aw:
            return bytes([width // 8 - 31, (code << 6) | (refresh - 60)])
    return None


def _chroma() -> bytes:
    # sRGB primaries + D65 white, 10-bit fixed point.
    pts = [(0.640, 0.330), (0.300, 0.600), (0.150, 0.060), (0.3127, 0.3290)]
    v = [round(c * 1024) for xy in pts for c in xy]
    lo1 = ((v[0] & 3) << 6) | ((v[1] & 3) << 4) | ((v[2] & 3) << 2) | (v[3] & 3)
    lo2 = ((v[4] & 3) << 6) | ((v[5] & 3) << 4) | ((v[6] & 3) << 2) | (v[7] & 3)
    return bytes([lo1, lo2] + [x >> 2 for x in v])


def _vendor_id(code: str) -> bytes:
    code = (code.upper() + "AAA")[:3]
    val = ((ord(code[0]) - 64) << 10) | ((ord(code[1]) - 64) << 5) | (ord(code[2]) - 64)
    return val.to_bytes(2, "big")


def build_edid(
    modes: Iterable[tuple[int, int, int]] = DEFAULT_MODES,
    name: str = DEFAULT_NAME,
    size_mm: tuple[int, int] = DEFAULT_SIZE_MM,
    vendor: str = DEFAULT_VENDOR,
    product_code: int = 0x5354,
    serial: int = 1,
    year: int = 2026,
) -> bytes:
    """128-byte EDID. First mode is the preferred/native one.

    Up to two modes go into detailed timing descriptors; any remaining mode
    that fits a standard-timing slot (<= 2288 px wide) is listed there too.
    """
    modes = list(dict.fromkeys(modes))
    if not modes:
        raise ValueError("at least one mode required")
    timings = [cvt_rb(w, h, r) for (w, h, r) in modes]

    e = bytearray(EDID_HEADER)
    e += _vendor_id(vendor)
    e += product_code.to_bytes(2, "little")
    e += serial.to_bytes(4, "little")
    e += bytes([1, max(0, year - 1990)])
    e += bytes([1, 4])  # EDID 1.4
    e += bytes([0xA5])  # digital, 8 bpc, DisplayPort
    e += bytes([max(1, size_mm[0] // 10), max(1, size_mm[1] // 10)])
    e += bytes([120])  # gamma 2.2
    e += bytes([0x06])  # sRGB default, preferred timing is native, RGB 4:4:4
    e += _chroma()
    e += bytes([0, 0, 0])  # no established timings

    std = [s for s in (_standard_timing(*m) for m in modes) if s]
    std = std[:8] + [b"\x01\x01"] * (8 - len(std[:8]))
    e += b"".join(std)

    descriptors = [_dtd(t, size_mm) for t in timings[:2]]
    descriptors.append(_range_descriptor(timings))
    descriptors.append(_text_descriptor(0xFC, name))
    if len(descriptors) < 4:
        descriptors.insert(1, _text_descriptor(0xFF, f"{serial:08d}"))
    e += b"".join(descriptors[:4])
    e += bytes([0])  # no extension blocks
    e += bytes([(-sum(e)) & 0xFF])
    assert len(e) == 128
    return bytes(e)


# ------------------------------------------------------------------ parsing

@dataclass
class EdidInfo:
    vendor: str
    product_code: int
    name: str = ""
    modes: list[tuple[int, int, int]] = field(default_factory=list)  # w, h, refresh
    size_mm: tuple[int, int] = (0, 0)

    @property
    def preferred(self) -> Optional[tuple[int, int, int]]:
        return self.modes[0] if self.modes else None


def edid_checksum_ok(data: bytes) -> bool:
    return len(data) >= 128 and sum(data[:128]) & 0xFF == 0


def parse_edid(data: bytes) -> Optional[EdidInfo]:
    if len(data) < 128 or data[:8] != EDID_HEADER:
        return None
    vid = int.from_bytes(data[8:10], "big")
    vendor = "".join(chr(((vid >> s) & 0x1F) + 64) for s in (10, 5, 0))
    info = EdidInfo(vendor=vendor, product_code=int.from_bytes(data[10:12], "little"))
    for off in range(54, 126, 18):
        d = data[off : off + 18]
        clk = int.from_bytes(d[0:2], "little")
        if clk:
            w = d[2] | ((d[4] & 0xF0) << 4)
            hb = d[3] | ((d[4] & 0x0F) << 8)
            h = d[5] | ((d[7] & 0xF0) << 4)
            vb = d[6] | ((d[7] & 0x0F) << 8)
            if w and h and (w + hb) and (h + vb):
                refresh = round(clk * 10_000 / ((w + hb) * (h + vb)))
                info.modes.append((w, h, refresh))
                if info.size_mm == (0, 0):
                    info.size_mm = (d[12] | ((d[14] & 0xF0) << 4), d[13] | ((d[14] & 0x0F) << 8))
        elif d[3] == 0xFC:
            info.name = d[5:18].split(b"\n")[0].decode("ascii", "replace").strip()
    return info
