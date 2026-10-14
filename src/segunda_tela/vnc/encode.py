"""RFB rectangle encoders: Tight and ZRLE, plus Raw.

The EVDI grab is RGBx (byte0 = R). bVNC's default 24-bit mode asks for
little-endian shifts red=16, green=8, blue=0, so a CPIXEL is B,G,R.
Tight and ZRLE use that 3-byte CPIXEL when bpp is 32 and depth is 24 or
less (RFC 6143). Solid rectangles are a Tight fill (4 bytes on the wire)
instead of a compressed bitmap.

zlib level 1 is deliberate: desktop damage is highly compressible and the
30 fps budget is spent on packing pixels, not on squeezing the last byte.
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass

ENCODING_RAW = 0
ENCODING_TIGHT = 7
ENCODING_ZRLE = 16
ENCODING_DESKTOP_SIZE = -223
ENCODING_CONTINUOUS = -313  # pseudo; client offers it, then sends message 150

SUPPORTED_RECT_ENCODINGS = (ENCODING_TIGHT, ENCODING_ZRLE, ENCODING_RAW)
TIGHT_MIN_COMPRESS = 12


@dataclass(frozen=True)
class PixelFormat:
    bpp: int = 32
    depth: int = 24
    big_endian: bool = False
    true_color: bool = True
    red_max: int = 255
    green_max: int = 255
    blue_max: int = 255
    red_shift: int = 0
    green_shift: int = 8
    blue_shift: int = 16

    @staticmethod
    def rgbx() -> "PixelFormat":
        """Server-native: memory R,G,B,X is little-endian shifts 0,8,16."""
        return PixelFormat()

    @staticmethod
    def bvnc() -> "PixelFormat":
        """bVNC C24bit: writeSetPixelFormat(32, 24, ..., 16, 8, 0)."""
        return PixelFormat(red_shift=16, green_shift=8, blue_shift=0)

    @property
    def bytes_per_pixel(self) -> int:
        return self.bpp // 8

    @property
    def cpixel_bytes(self) -> int:
        if self.true_color and self.bpp == 32 and self.depth <= 24:
            return 3
        return max(1, self.bpp // 8)

    def is_rgbx_bytes(self) -> bool:
        return (
            not self.big_endian and self.bpp == 32 and self.true_color
            and self.red_max == 255 and self.green_max == 255 and self.blue_max == 255
            and (self.red_shift, self.green_shift, self.blue_shift) == (0, 8, 16)
        )

    def is_bgr_cpixel(self) -> bool:
        return (
            not self.big_endian and self.true_color and self.cpixel_bytes == 3
            and self.red_max == 255 and self.green_max == 255 and self.blue_max == 255
            and (self.red_shift, self.green_shift, self.blue_shift) == (16, 8, 0)
        )


def pixel_format_bytes(fmt: PixelFormat) -> bytes:
    import struct
    return struct.pack(
        ">BBBBHHHBBB3x",
        fmt.bpp, fmt.depth, int(fmt.big_endian), int(fmt.true_color),
        fmt.red_max, fmt.green_max, fmt.blue_max,
        fmt.red_shift, fmt.green_shift, fmt.blue_shift,
    )


def parse_pixel_format(raw: bytes) -> PixelFormat:
    import struct
    if len(raw) < 16:
        raise ValueError("pixel format is 16 bytes")
    rmax, gmax, bmax = struct.unpack_from(">HHH", raw, 4)
    return PixelFormat(
        bpp=raw[0], depth=raw[1], big_endian=bool(raw[2]), true_color=bool(raw[3]),
        red_max=rmax, green_max=gmax, blue_max=bmax,
        red_shift=raw[10], green_shift=raw[11], blue_shift=raw[12],
    )


def pick_encoding(offered: list[int]) -> int:
    """First client preference we can produce. Raw if they offered nothing we know."""
    for enc in offered:
        if enc in SUPPORTED_RECT_ENCODINGS:
            return enc
    return ENCODING_RAW


def solid_rgbx(buf: bytes | bytearray | memoryview) -> bytes | None:
    """The single RGBx pixel if every pixel matches, else None.

    Adjacent pixels are compared in one memory compare: a solid buffer is
    unchanged when shifted by one pixel.
    """
    if len(buf) < 4 or len(buf) % 4:
        return None
    raw = bytes(buf)
    if raw[4:] != raw[:-4]:
        return None
    return raw[:4]


def crop_rgbx(buf: bytes | bytearray | memoryview, stride_px: int, x: int, y: int, w: int, h: int) -> bytes:
    """Tightly packed RGBx rectangle out of a (possibly wider) frame."""
    row = w * 4
    out = bytearray(row * h)
    src = memoryview(buf)
    for i in range(h):
        off = ((y + i) * stride_px + x) * 4
        out[i * row:(i + 1) * row] = src[off:off + row]
    return bytes(out)


def _pack_bgr(src: bytes) -> bytes:
    n = len(src) // 4
    out = bytearray(n * 3)
    mv = memoryview(src)
    o = 0
    for i in range(0, n * 4, 4):
        out[o] = mv[i + 2]
        out[o + 1] = mv[i + 1]
        out[o + 2] = mv[i]
        o += 3
    return bytes(out)


def _pack_rgb(src: bytes) -> bytes:
    n = len(src) // 4
    out = bytearray(n * 3)
    mv = memoryview(src)
    o = 0
    for i in range(0, n * 4, 4):
        out[o:o + 3] = mv[i:i + 3]
        o += 3
    return bytes(out)


def _pack_generic(src: bytes, fmt: PixelFormat, nbytes: int) -> bytes:
    out = bytearray(len(src) // 4 * nbytes)
    o = 0
    for i in range(0, len(src), 4):
        r, g, b, x = src[i], src[i + 1], src[i + 2], src[i + 3]
        pixel = (
            (r & fmt.red_max) << fmt.red_shift
            | (g & fmt.green_max) << fmt.green_shift
            | (b & fmt.blue_max) << fmt.blue_shift
        )
        raw = pixel.to_bytes(4, "big" if fmt.big_endian else "little")
        take = raw[:nbytes] if not fmt.big_endian else raw[4 - nbytes:]
        out[o:o + nbytes] = take
        o += nbytes
        del x
    return bytes(out)


def pack_cpixels(rgbx: bytes, fmt: PixelFormat) -> bytes:
    if fmt.is_bgr_cpixel():
        return _pack_bgr(rgbx)
    if fmt.cpixel_bytes == 3 and fmt.is_rgbx_bytes():
        return _pack_rgb(rgbx)
    if fmt.cpixel_bytes == 4 and fmt.is_rgbx_bytes():
        return rgbx
    return _pack_generic(rgbx, fmt, fmt.cpixel_bytes)


def pack_pixels(rgbx: bytes, fmt: PixelFormat) -> bytes:
    """Full PIXEL (Raw encoding), not the 3-byte CPIXEL."""
    if fmt.is_rgbx_bytes() and fmt.bpp == 32:
        return rgbx
    return _pack_generic(rgbx, fmt, fmt.bytes_per_pixel)


def cpixel_to_rgbx(pixel: bytes, fmt: PixelFormat) -> bytes:
    if len(pixel) == 3:
        raw = (b"\x00" + pixel) if fmt.big_endian else (pixel + b"\x00")
    else:
        raw = pixel[:4]
    value = int.from_bytes(raw, "big" if fmt.big_endian else "little")
    r = (value >> fmt.red_shift) & fmt.red_max
    g = (value >> fmt.green_shift) & fmt.green_max
    b = (value >> fmt.blue_shift) & fmt.blue_max
    return bytes((r, g, b, 255))


def write_compact(length: int) -> bytes:
    if length < 0 or length > 0x3FFFFF:
        raise ValueError("tight length out of range")
    if length < 128:
        return bytes((length,))
    if length < 16384:
        return bytes((((length & 0x7F) | 0x80), (length >> 7) & 0x7F))
    return bytes((
        (length & 0x7F) | 0x80,
        ((length >> 7) & 0x7F) | 0x80,
        (length >> 14) & 0xFF,
    ))


def read_compact(data: bytes, offset: int) -> tuple[int, int]:
    b0 = data[offset]
    offset += 1
    value = b0 & 0x7F
    if b0 & 0x80:
        b1 = data[offset]
        offset += 1
        value |= (b1 & 0x7F) << 7
        if b1 & 0x80:
            b2 = data[offset]
            offset += 1
            value |= b2 << 14
    return value, offset


def encode_tight(rgbx: bytes, fmt: PixelFormat, level: int = 1) -> bytes:
    """One Tight rectangle payload (no x/y/w/h header)."""
    solid = solid_rgbx(rgbx)
    if solid is not None:
        return bytes((0x08,)) + pack_cpixels(solid, fmt)
    pixels = pack_cpixels(rgbx, fmt)
    # Bit 4 resets zlib stream 0. Low nibble 0 = basic compression, copy filter.
    control = 0x10
    if len(pixels) < TIGHT_MIN_COMPRESS:
        return bytes((control,)) + pixels
    compressed = zlib.compress(pixels, level)
    return bytes((control,)) + write_compact(len(compressed)) + compressed


def decode_tight(payload: bytes, width: int, height: int, fmt: PixelFormat) -> bytes:
    """Inverse of `encode_tight` for the subset we emit (fill and copy filter)."""
    if not payload:
        raise ValueError("empty tight payload")
    control = payload[0]
    low = control & 0x0F
    offset = 1
    count = width * height
    if low == 0x08:
        pix = payload[offset:offset + fmt.cpixel_bytes]
        one = cpixel_to_rgbx(pix, fmt)
        return one * count
    if low in (0x09, 0x0A):
        raise ValueError("JPEG/PNG tight is not produced")
    if low & 0x04:
        offset += 1  # explicit filter id; we only emit copy (0) and usually omit it
    raw_len = count * fmt.cpixel_bytes
    if raw_len < TIGHT_MIN_COMPRESS:
        pixels = payload[offset:offset + raw_len]
    else:
        comp_len, offset = read_compact(payload, offset)
        pixels = zlib.decompress(payload[offset:offset + comp_len])
    cb = fmt.cpixel_bytes
    out = bytearray(count * 4)
    for i in range(count):
        out[i * 4:(i + 1) * 4] = cpixel_to_rgbx(pixels[i * cb:(i + 1) * cb], fmt)
    return bytes(out)


def _zrle_tiles(rgbx: bytes, width: int, height: int, fmt: PixelFormat) -> bytes:
    out = bytearray()
    for ty in range(0, height, 64):
        th = min(64, height - ty)
        for tx in range(0, width, 64):
            tw = min(64, width - tx)
            tile = crop_rgbx(rgbx, width, tx, ty, tw, th)
            solid = solid_rgbx(tile)
            if solid is not None:
                out.append(1)
                out += pack_cpixels(solid, fmt)
            else:
                out.append(0)
                out += pack_cpixels(tile, fmt)
    return bytes(out)


class ZrleEncoder:
    """One zlib stream per RFB connection, sync-flushed on every rectangle."""

    def __init__(self, level: int = 1) -> None:
        self._z = zlib.compressobj(level)

    def encode(self, rgbx: bytes, width: int, height: int, fmt: PixelFormat) -> bytes:
        tiles = _zrle_tiles(rgbx, width, height, fmt)
        blob = self._z.compress(tiles) + self._z.flush(zlib.Z_SYNC_FLUSH)
        return len(blob).to_bytes(4, "big") + blob


class ZrleDecoder:
    def __init__(self) -> None:
        self._z = zlib.decompressobj()

    def decode(self, payload: bytes, width: int, height: int, fmt: PixelFormat) -> bytes:
        length = int.from_bytes(payload[:4], "big")
        tiles = self._z.decompress(payload[4:4 + length])
        return _paint_zrle(tiles, width, height, fmt)


def _paint_zrle(data: bytes, width: int, height: int, fmt: PixelFormat) -> bytes:
    cb = fmt.cpixel_bytes
    canvas = bytearray(width * height * 4)
    offset = 0
    for ty in range(0, height, 64):
        th = min(64, height - ty)
        for tx in range(0, width, 64):
            tw = min(64, width - tx)
            sub = data[offset]
            offset += 1
            count = tw * th
            if sub == 1:
                pix = cpixel_to_rgbx(data[offset:offset + cb], fmt)
                offset += cb
                pixels = pix * count
            elif sub == 0:
                raw = data[offset:offset + count * cb]
                offset += count * cb
                pixels = b"".join(cpixel_to_rgbx(raw[i:i + cb], fmt) for i in range(0, len(raw), cb))
            else:
                raise ValueError(f"ZRLE subencoding {sub} is not produced by this server")
            row_bytes = tw * 4
            for row in range(th):
                dest = ((ty + row) * width + tx) * 4
                canvas[dest:dest + row_bytes] = pixels[row * row_bytes:(row + 1) * row_bytes]
    return bytes(canvas)


def encode_rect(
    rgbx: bytes,
    width: int,
    height: int,
    fmt: PixelFormat,
    encoding: int,
    zrle: ZrleEncoder | None = None,
    level: int = 1,
) -> tuple[int, bytes]:
    if encoding == ENCODING_TIGHT:
        return ENCODING_TIGHT, encode_tight(rgbx, fmt, level=level)
    if encoding == ENCODING_ZRLE:
        enc = zrle or ZrleEncoder(level)
        return ENCODING_ZRLE, enc.encode(rgbx, width, height, fmt)
    return ENCODING_RAW, pack_pixels(rgbx, fmt)


def coalesce_rects(
    rects: list[tuple[int, int, int, int]],
    width: int,
    height: int,
    max_rects: int = 8,
) -> list[tuple[int, int, int, int]]:
    """Merge exclusive-end damage rects. A large total area becomes one frame."""
    norm: list[tuple[int, int, int, int]] = []
    for x1, y1, x2, y2 in rects:
        x1 = 0 if x1 < 0 else width if x1 > width else x1
        x2 = 0 if x2 < 0 else width if x2 > width else x2
        y1 = 0 if y1 < 0 else height if y1 > height else y1
        y2 = 0 if y2 < 0 else height if y2 > height else y2
        if x2 > x1 and y2 > y1:
            norm.append((x1, y1, x2, y2))
    if not norm:
        return []
    area = sum((x2 - x1) * (y2 - y1) for x1, y1, x2, y2 in norm)
    if area * 2 >= width * height:
        return [(0, 0, width, height)]
    merged = _merge_touching(norm)
    if len(merged) > max_rects:
        return [(
            min(r[0] for r in merged), min(r[1] for r in merged),
            max(r[2] for r in merged), max(r[3] for r in merged),
        )]
    return merged


def _touch(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    # 1px slack so neighbouring damage becomes one Tight rectangle.
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    return ax1 <= bx2 + 1 and bx1 <= ax2 + 1 and ay1 <= by2 + 1 and by1 <= ay2 + 1


def _merge_touching(rects: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    pending = list(rects)
    changed = True
    while changed and len(pending) > 1:
        changed = False
        out: list[tuple[int, int, int, int]] = []
        used = [False] * len(pending)
        for i, rect in enumerate(pending):
            if used[i]:
                continue
            x1, y1, x2, y2 = rect
            for j in range(i + 1, len(pending)):
                if used[j]:
                    continue
                other = pending[j]
                if _touch((x1, y1, x2, y2), other):
                    x1 = min(x1, other[0])
                    y1 = min(y1, other[1])
                    x2 = max(x2, other[2])
                    y2 = max(y2, other[3])
                    used[j] = True
                    changed = True
            out.append((x1, y1, x2, y2))
        pending = out
    return pending


def xywh(rects: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    return [(x1, y1, x2 - x1, y2 - y1) for x1, y1, x2, y2 in rects]
