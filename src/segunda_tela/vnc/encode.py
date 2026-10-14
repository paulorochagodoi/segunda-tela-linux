"""RFB rectangle encoders: Tight and ZRLE, plus Raw.

The EVDI grab is RGBx (byte0 = R). bVNC's default 24-bit mode asks for
little-endian shifts red=16, green=8, blue=0, so a CPIXEL is B,G,R.
Tight and ZRLE use that 3-byte CPIXEL when bpp is 32 and depth is 24 or
less (RFC 6143). Solid rectangles are a Tight fill instead of a compressed
bitmap.

bVNC (and TightVNC) treat the low 4 bits of the Tight control byte as zlib
stream resets and the high nibble, after shifting those bits off, as the
subencoding. Fill is 0x80, JPEG is 0x90, basic compression with stream 0
reset is 0x01. A low-nibble 0x09 is basic compression to that client, which
is why a JPEG has to carry the type in the high nibble.

Photo-like damage is Tight JPEG around quality 70 (simplejpeg, then
turbojpeg, then Pillow). UI-like damage stays zlib level 1. The byte budget
retries a lower quality so one rectangle stays near 200 KB over adb.
"""
from __future__ import annotations

import io
import logging
import zlib
from dataclasses import dataclass

log = logging.getLogger(__name__)

try:
    import numpy as np
except ImportError:  # pragma: no cover - Pop!_OS ships python3-numpy; the loop remains for CI
    np = None

ENCODING_RAW = 0
ENCODING_TIGHT = 7
ENCODING_ZRLE = 16
ENCODING_DESKTOP_SIZE = -223
ENCODING_CONTINUOUS = -313  # pseudo; client offers it, then sends message 150

SUPPORTED_RECT_ENCODINGS = (ENCODING_TIGHT, ENCODING_ZRLE, ENCODING_RAW)
TIGHT_MIN_COMPRESS = 12
# High nibble = subencoding, low 4 bits = zlib stream resets. See module doc.
TIGHT_BASIC_RESET = 0x01
TIGHT_FILL = 0x80
TIGHT_JPEG = 0x90
JPEG_QUALITY = 70
JPEG_BYTE_BUDGET = 200 * 1024
PHOTO_MIN_PIXELS = 96 * 96


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
    """The single RGBx pixel if every pixel matches, else None."""
    if len(buf) < 4 or len(buf) % 4:
        return None
    if np is not None and len(buf) >= 16:
        pix = np.frombuffer(buf, dtype=np.uint8).reshape(-1, 4)
        if not np.any(pix[1:] != pix[0]):
            return bytes(pix[0].tolist())
        return None
    raw = bytes(buf)
    if raw[4:] != raw[:-4]:
        return None
    return raw[:4]


def crop_rgbx(buf: bytes | bytearray | memoryview, stride_px: int, x: int, y: int, w: int, h: int) -> bytes:
    """Tightly packed RGBx rectangle out of a (possibly wider) frame."""
    if np is not None and stride_px > 0 and len(buf) >= stride_px * (y + h) * 4:
        rows = len(buf) // (stride_px * 4)
        frame = np.frombuffer(buf, dtype=np.uint8).reshape(rows, stride_px, 4)
        return np.ascontiguousarray(frame[y:y + h, x:x + w]).tobytes()
    row = w * 4
    out = bytearray(row * h)
    src = memoryview(buf)
    for i in range(h):
        off = ((y + i) * stride_px + x) * 4
        out[i * row:(i + 1) * row] = src[off:off + row]
    return bytes(out)


def _pixels(src: bytes | bytearray | memoryview):
    arr = np.frombuffer(src, dtype=np.uint8)
    n = arr.size // 4
    return arr[: n * 4].reshape(n, 4)


def _pack_bgr(src: bytes | bytearray | memoryview) -> bytes:
    """RGBx → B,G,R. Little-endian CPIXEL when red is at shift 16."""
    if np is not None:
        pix = _pixels(src)
        out = np.empty((pix.shape[0], 3), dtype=np.uint8)
        out[:, 0] = pix[:, 2]
        out[:, 1] = pix[:, 1]
        out[:, 2] = pix[:, 0]
        return out.tobytes()
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


def _pack_bgrx(src: bytes | bytearray | memoryview) -> bytes:
    """RGBx → B,G,R,0. Raw PIXEL for bVNC C24bit (red shift 16, little endian)."""
    if np is not None:
        pix = _pixels(src)
        out = np.zeros((pix.shape[0], 4), dtype=np.uint8)
        out[:, 0] = pix[:, 2]
        out[:, 1] = pix[:, 1]
        out[:, 2] = pix[:, 0]
        return out.tobytes()
    n = len(src) // 4
    out = bytearray(n * 4)
    mv = memoryview(src)
    for i in range(n):
        s = i * 4
        out[s] = mv[s + 2]
        out[s + 1] = mv[s + 1]
        out[s + 2] = mv[s]
    return bytes(out)


def _pack_rgb(src: bytes | bytearray | memoryview) -> bytes:
    """RGBx → R,G,B (drop the pad). Tight truecolor bytes, see `pack_tight`."""
    if np is not None:
        pix = _pixels(src)
        return np.ascontiguousarray(pix[:, :3]).tobytes()
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


def pack_cpixels(rgbx: bytes | bytearray | memoryview, fmt: PixelFormat) -> bytes:
    """ZRLE CPIXEL. Honors shifts: bVNC's ZRLE decoder uses byte0 as blue for C24bit."""
    if fmt.is_bgr_cpixel():
        return _pack_bgr(rgbx)
    if fmt.cpixel_bytes == 3 and fmt.is_rgbx_bytes():
        return _pack_rgb(rgbx)
    if fmt.cpixel_bytes == 4 and fmt.is_rgbx_bytes():
        return bytes(rgbx)
    return _pack_generic(rgbx, fmt, fmt.cpixel_bytes)


def pack_tight(rgbx: bytes | bytearray | memoryview, fmt: PixelFormat) -> bytes:
    """Tight truecolor bytes.

    bVNC `Decoder.handleTightRect` paints `(b0<<16)|(b1<<8)|b2` into an
    Android ARGB int, so byte0 is red. It does not apply redShift. Raw and
    ZRLE in that same class do (`b2` is red), which matches C24bit shifts
    16,8,0. Tight on this client is therefore R,G,B from the RGBx grab.
    A shift-based B,G,R triplet is what made the tablet show red and blue
    swapped.
    """
    if fmt.true_color and fmt.cpixel_bytes == 3 and fmt.red_max == 255 and not fmt.big_endian:
        return _pack_rgb(rgbx)
    return pack_cpixels(rgbx, fmt)


def pack_pixels(rgbx: bytes | bytearray | memoryview, fmt: PixelFormat) -> bytes:
    """Full PIXEL (Raw encoding), not the 3-byte CPIXEL.

    bVNC's Raw path uses byte0 as blue when it asked for red shift 16.
    """
    if fmt.is_rgbx_bytes() and fmt.bpp == 32:
        return bytes(rgbx)
    if (
        fmt.bpp == 32 and not fmt.big_endian and fmt.true_color
        and (fmt.red_shift, fmt.green_shift, fmt.blue_shift) == (16, 8, 0)
        and fmt.red_max == 255 and fmt.green_max == 255 and fmt.blue_max == 255
    ):
        return _pack_bgrx(rgbx)
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


def _tight_triplet_to_rgbx(pix: bytes) -> bytes:
    """Inverse of `pack_tight` for a 24-bit triplet (byte0 = red)."""
    if len(pix) == 3:
        return bytes((pix[0], pix[1], pix[2], 255))
    return cpixel_to_rgbx(pix, PixelFormat.rgbx())


def tight_subencoding(control: int) -> int:
    """Subencoding after bVNC shifts the four zlib-reset bits off."""
    comp = control & 0xFF
    for _ in range(4):
        comp >>= 1
    return comp


def looks_photographic(rgbx: bytes | bytearray | memoryview, width: int, height: int) -> bool:
    """True when a rectangle is smooth and has many colors (a photo, not UI).

    Text and flat chrome stay on zlib: they have few colors or hard edges,
    and JPEG rings around glyphs. A gradient or camera image has many similar
    neighbors.
    """
    if width < 2 or height < 2 or width * height < PHOTO_MIN_PIXELS:
        return False
    if len(rgbx) < width * height * 4:
        return False
    if np is not None:
        return _photo_numpy(rgbx, width, height)
    return _photo_python(rgbx, width, height)


def _photo_numpy(rgbx: bytes | bytearray | memoryview, width: int, height: int) -> bool:
    pix = np.frombuffer(rgbx, dtype=np.uint8)
    frame = pix[: width * height * 4].reshape(height, width, 4)
    step_y = max(1, height // 48)
    step_x = max(1, width // 48)
    sample = frame[::step_y, ::step_x, :3]
    if sample.shape[0] < 2 or sample.shape[1] < 2:
        return False
    shifted = sample.astype(np.uint16) >> 3
    quant = (shifted[:, :, 0] << 10) | (shifted[:, :, 1] << 5) | shifted[:, :, 2]
    if int(np.unique(quant).size) < 32:
        return False
    diff = np.abs(sample[:, 1:, :].astype(np.int16) - sample[:, :-1, :].astype(np.int16)).sum(axis=2)
    small = int(np.count_nonzero(diff < 48))
    large = int(np.count_nonzero(diff > 160))
    return small > large * 2


def _photo_python(rgbx: bytes | bytearray | memoryview, width: int, height: int) -> bool:
    step_y = max(1, height // 24)
    step_x = max(1, width // 24)
    mv = memoryview(rgbx)
    colors: set[int] = set()
    small = 0
    large = 0
    y = 0
    while y < height:
        x = 0
        row = y * width * 4
        while x + step_x < width:
            i = row + x * 4
            r, g, b = mv[i], mv[i + 1], mv[i + 2]
            colors.add(((r >> 3) << 10) | ((g >> 3) << 5) | (b >> 3))
            j = i + step_x * 4
            dist = abs(mv[j] - r) + abs(mv[j + 1] - g) + abs(mv[j + 2] - b)
            if dist < 48:
                small += 1
            elif dist > 160:
                large += 1
            x += step_x
        y += step_y
    return len(colors) >= 32 and small > large * 2


_JPEG_BACKEND: str | None = None
_TURBO = None
_JPEG_WARNED = False


def jpeg_backend() -> str | None:
    """`simplejpeg`, `turbojpeg`, `pil`, or None when no encoder is installed."""
    global _JPEG_BACKEND, _TURBO
    if _JPEG_BACKEND is not None:
        return _JPEG_BACKEND or None
    if np is not None:
        try:
            import simplejpeg  # noqa: F401
            _JPEG_BACKEND = "simplejpeg"
            return _JPEG_BACKEND
        except ImportError:
            pass
        try:
            from turbojpeg import TurboJPEG
            _TURBO = TurboJPEG()
            _JPEG_BACKEND = "turbojpeg"
            return _JPEG_BACKEND
        except Exception:
            _TURBO = None
    try:
        from PIL import Image  # noqa: F401
        _JPEG_BACKEND = "pil"
        return _JPEG_BACKEND
    except ImportError:
        _JPEG_BACKEND = ""
        return None


def _rgb_pixels(rgbx: bytes | bytearray | memoryview, width: int, height: int):
    """Contiguous HxWx3 RGB. ndarray when numpy is present, else bytes."""
    if np is not None:
        pix = np.frombuffer(rgbx, dtype=np.uint8)
        frame = pix[: width * height * 4].reshape(height, width, 4)
        return np.ascontiguousarray(frame[:, :, :3])
    return _pack_rgb(rgbx)


def _encode_jpeg_rgb(rgb, width: int, height: int, quality: int) -> bytes | None:
    global _JPEG_WARNED
    backend = jpeg_backend()
    if backend is None:
        if not _JPEG_WARNED:
            _JPEG_WARNED = True
            log.info("Tight JPEG indisponível; instale pillow ou simplejpeg (o install.sh faz isso)")
        return None
    try:
        if backend == "simplejpeg":
            import simplejpeg
            arr = rgb if np is not None and not isinstance(rgb, (bytes, bytearray)) else None
            if arr is None:
                return None
            return simplejpeg.encode_jpeg(
                arr, quality=quality, colorspace="RGB", colorsubsampling="420", fastdct=True,
            )
        if backend == "turbojpeg":
            from turbojpeg import TJPF_RGB, TJSAMP_420
            if _TURBO is None or isinstance(rgb, (bytes, bytearray)):
                return None
            return _TURBO.encode(rgb, quality=quality, pixel_format=TJPF_RGB, jpeg_subsample=TJSAMP_420)
        from PIL import Image
        if np is not None and not isinstance(rgb, (bytes, bytearray)):
            image = Image.fromarray(rgb, "RGB")
        else:
            image = Image.frombytes("RGB", (width, height), bytes(rgb))
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=quality, subsampling=2)
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001 — missing libjpeg should fall back to zlib
        log.debug("jpeg %s: %s", backend, exc)
        return None


def _tight_jpeg(rgbx: bytes | bytearray | memoryview, width: int, height: int) -> bytes | None:
    """Tight JPEG rectangle (control 0x90 + compact length + JPEG), or None."""
    rgb = _rgb_pixels(rgbx, width, height)
    best: bytes | None = None
    for quality in (JPEG_QUALITY, 55, 40, 30):
        blob = _encode_jpeg_rgb(rgb, width, height, quality)
        if not blob or len(blob) < 4 or blob[:2] != b"\xff\xd8":
            break
        best = blob
        if len(blob) <= JPEG_BYTE_BUDGET:
            break
    if not best or len(best) > 0x3FFFFF:
        return None
    return bytes((TIGHT_JPEG,)) + write_compact(len(best)) + best


def encode_tight(
    rgbx: bytes,
    fmt: PixelFormat,
    level: int = 1,
    width: int = 0,
    height: int = 0,
) -> bytes:
    """One Tight rectangle payload (no x/y/w/h header)."""
    solid = solid_rgbx(rgbx)
    if solid is not None:
        return bytes((TIGHT_FILL,)) + pack_tight(solid, fmt)
    if (
        width > 0 and height > 0 and width * height * 4 == len(rgbx)
        and looks_photographic(rgbx, width, height)
    ):
        jpeg = _tight_jpeg(rgbx, width, height)
        if jpeg is not None:
            return jpeg
    pixels = pack_tight(rgbx, fmt)
    # Bit 0 resets zlib stream 0. High nibble 0 = basic compression, copy filter.
    control = TIGHT_BASIC_RESET
    if len(pixels) < TIGHT_MIN_COMPRESS:
        return bytes((control,)) + pixels
    compressed = zlib.compress(pixels, level)
    return bytes((control,)) + write_compact(len(compressed)) + compressed


def decode_tight(payload: bytes, width: int, height: int, fmt: PixelFormat) -> bytes:
    """Inverse of `encode_tight` for fill and basic compression (not JPEG)."""
    if not payload:
        raise ValueError("empty tight payload")
    comp = tight_subencoding(payload[0])
    offset = 1
    count = width * height
    if comp == 0x08:
        pix = payload[offset:offset + fmt.cpixel_bytes]
        one = _tight_triplet_to_rgbx(pix) if fmt.cpixel_bytes == 3 else cpixel_to_rgbx(pix, fmt)
        return one * count
    if comp == 0x09:
        raise ValueError("JPEG tight is display-only")
    if comp > 0x09:
        raise ValueError(f"tight subencoding {comp}")
    if comp & 0x04:
        offset += 1  # explicit filter id; we only emit copy (0) and usually omit it
    raw_len = count * fmt.cpixel_bytes
    if raw_len < TIGHT_MIN_COMPRESS:
        pixels = payload[offset:offset + raw_len]
    else:
        comp_len, offset = read_compact(payload, offset)
        pixels = zlib.decompress(payload[offset:offset + comp_len])
    cb = fmt.cpixel_bytes
    out = bytearray(count * 4)
    convert = _tight_triplet_to_rgbx if cb == 3 else (lambda pix: cpixel_to_rgbx(pix, fmt))
    for i in range(count):
        out[i * 4:(i + 1) * 4] = convert(pixels[i * cb:(i + 1) * cb])
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


def tight_slices(
    rgbx: bytes,
    width: int,
    height: int,
    fmt: PixelFormat,
    level: int = 1,
    y0: int = 0,
) -> list[tuple[int, int, int, int, bytes]]:
    """Tight rectangles covering `rgbx`.

    One zlib stream per slice. A slice whose compressed size does not fit
    in Tight's 22-bit length is split in half. Desktop frames usually stay
    one rectangle; random noise does not.
    """
    try:
        payload = encode_tight(rgbx, fmt, level=level, width=width, height=height)
    except ValueError:
        if height <= 1:
            raise
        mid = max(1, height // 2)
        stride = width * 4
        return (
            tight_slices(rgbx[: mid * stride], width, mid, fmt, level, y0)
            + tight_slices(rgbx[mid * stride:], width, height - mid, fmt, level, y0 + mid)
        )
    return [(0, y0, width, height, payload)]


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
        return ENCODING_TIGHT, encode_tight(rgbx, fmt, level=level, width=width, height=height)
    if encoding == ENCODING_ZRLE:
        enc = zrle or ZrleEncoder(level)
        return ENCODING_ZRLE, enc.encode(rgbx, width, height, fmt)
    return ENCODING_RAW, pack_pixels(rgbx, fmt)


def coalesce_rects(
    rects: list[tuple[int, int, int, int]],
    width: int,
    height: int,
    max_rects: int = 16,
) -> list[tuple[int, int, int, int]]:
    """Merge exclusive-end damage rects.

    Touching clips become one Tight rectangle. Extra clips are folded by the
    pair that adds the least empty pixels. A large or numerous update is not
    promoted to the whole framebuffer — that is what made every adb frame a
    1920×1200 rectangle.
    """
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
    merged = _merge_touching(norm)
    if len(merged) > max(1, max_rects):
        merged = _merge_to_limit(merged, max(1, max_rects))
    return merged


def _rect_area(rect: tuple[int, int, int, int]) -> int:
    return (rect[2] - rect[0]) * (rect[3] - rect[1])


def _union(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _merge_to_limit(
    rects: list[tuple[int, int, int, int]],
    limit: int,
) -> list[tuple[int, int, int, int]]:
    pending = list(rects)
    while len(pending) > limit:
        best_i = 0
        best_j = 1
        best_cost: int | None = None
        best_union = pending[0]
        for i in range(len(pending)):
            for j in range(i + 1, len(pending)):
                union = _union(pending[i], pending[j])
                cost = _rect_area(union) - _rect_area(pending[i]) - _rect_area(pending[j])
                if best_cost is None or cost < best_cost:
                    best_cost = cost
                    best_i, best_j, best_union = i, j, union
        pending = [rect for k, rect in enumerate(pending) if k != best_i and k != best_j]
        pending.append(best_union)
    return pending


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


# 12×19 arrow. 'X' outline, '+' fill, '.' transparent. Drawn into the VNC
# frame because the compositor cursor plane is not in the EVDI grab, so the
# tablet otherwise shows a picture with no pointer.
_CURSOR_ROWS = (
    "X...........",
    "XX..........",
    "X+X.........",
    "X++X........",
    "X+++X.......",
    "X++++X......",
    "X+++++X.....",
    "X++++++X....",
    "X+++++++X...",
    "X++++++++X..",
    "X+++++XXXX..",
    "X++X++X.....",
    "X+X.X++X....",
    "XX..X++X....",
    "X....X++X...",
    ".....X++X...",
    "......X++X..",
    "......X++X..",
    ".......XX...",
)
CURSOR_W = len(_CURSOR_ROWS[0])
CURSOR_H = len(_CURSOR_ROWS)


def cursor_sprite() -> bytes:
    out = bytearray(CURSOR_W * CURSOR_H * 4)
    for y, row in enumerate(_CURSOR_ROWS):
        for x, ch in enumerate(row):
            if ch == ".":
                continue
            # Outline black, fill white. Both opaque so it shows on any desktop.
            rgb = (0, 0, 0) if ch == "X" else (255, 255, 255)
            i = (y * CURSOR_W + x) * 4
            out[i:i + 4] = bytes((*rgb, 255))
    return bytes(out)


_SPRITE = cursor_sprite()


def cursor_bounds(x: int, y: int, width: int, height: int) -> tuple[int, int, int, int]:
    """Exclusive-end rectangle covering the sprite at `x, y`, clipped to the frame."""
    x1 = 0 if x < 0 else width if x > width else x
    y1 = 0 if y < 0 else height if y > height else y
    x2 = x + CURSOR_W
    y2 = y + CURSOR_H
    x2 = 0 if x2 < 0 else width if x2 > width else x2
    y2 = 0 if y2 < 0 else height if y2 > height else y2
    if x2 <= x1 or y2 <= y1:
        return (0, 0, 0, 0)
    return (x1, y1, x2, y2)


def composite_cursor(
    rgbx: bytes,
    rect_w: int,
    rect_h: int,
    rect_x: int,
    rect_y: int,
    cursor_x: int,
    cursor_y: int,
) -> bytes:
    """Paint the arrow onto a tightly packed RGBx rectangle. No-op when it misses."""
    if rect_w <= 0 or rect_h <= 0:
        return rgbx
    if cursor_x >= rect_x + rect_w or cursor_y >= rect_y + rect_h:
        return rgbx
    if cursor_x + CURSOR_W <= rect_x or cursor_y + CURSOR_H <= rect_y:
        return rgbx
    out = bytearray(rgbx)
    for sy in range(CURSOR_H):
        fy = cursor_y + sy
        if fy < rect_y or fy >= rect_y + rect_h:
            continue
        row = sy * CURSOR_W
        for sx in range(CURSOR_W):
            fx = cursor_x + sx
            if fx < rect_x or fx >= rect_x + rect_w:
                continue
            si = (row + sx) * 4
            if _SPRITE[si + 3] == 0:
                continue
            di = ((fy - rect_y) * rect_w + (fx - rect_x)) * 4
            out[di:di + 4] = _SPRITE[si:si + 4]
    return bytes(out)
