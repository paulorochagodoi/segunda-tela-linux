"""Wi-Fi Display capability tables, sink-capability parsing and mode negotiation.

Tables and field layouts follow the public Wi-Fi Display Technical
Specification v1.0 (sections 6.1.3 / Tables 5-10..5-14, 6.1.2 audio, 6.1.4
client RTP ports). Written for this project; no code copied from other
implementations.

WFD R1 only defines three resolution tables (CEA, VESA, HH). The largest
16:10 mode is VESA 1920x1200; 2560x1600 cannot be signalled in R1, so a
2560x1600 virtual monitor is scaled down to the negotiated stream size.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

TABLE_CEA = 0
TABLE_VESA = 1
TABLE_HH = 2
TABLE_NAMES = {TABLE_CEA: "CEA", TABLE_VESA: "VESA", TABLE_HH: "HH"}

# (width, height, fps, interlaced). Index == bit position in the support bitmap.
CEA_MODES: tuple[tuple[int, int, int, bool], ...] = (
    (640, 480, 60, False),     # 0 (mandatory)
    (720, 480, 60, False),     # 1
    (720, 480, 60, True),      # 2
    (720, 576, 50, False),     # 3
    (720, 576, 50, True),      # 4
    (1280, 720, 30, False),    # 5
    (1280, 720, 60, False),    # 6
    (1920, 1080, 30, False),   # 7
    (1920, 1080, 60, False),   # 8
    (1920, 1080, 60, True),    # 9
    (1280, 720, 25, False),    # 10
    (1280, 720, 50, False),    # 11
    (1920, 1080, 25, False),   # 12
    (1920, 1080, 50, False),   # 13
    (1920, 1080, 50, True),    # 14
    (1280, 720, 24, False),    # 15
    (1920, 1080, 24, False),   # 16
)

VESA_MODES: tuple[tuple[int, int, int, bool], ...] = tuple(
    (w, h, r, False)
    for (w, h) in (
        (800, 600), (1024, 768), (1152, 864), (1280, 768), (1280, 800),
        (1360, 768), (1366, 768), (1280, 1024), (1400, 1050), (1440, 900),
        (1600, 900), (1600, 1200), (1680, 1024), (1680, 1050), (1920, 1200),
    )
    for r in (30, 60)
)  # bits 0..29; bit 29 == 1920x1200p60

HH_MODES: tuple[tuple[int, int, int, bool], ...] = tuple(
    (w, h, r, False)
    for (w, h) in ((800, 480), (854, 480), (864, 480), (640, 360), (960, 540), (848, 480))
    for r in (30, 60)
)

TABLES = {TABLE_CEA: CEA_MODES, TABLE_VESA: VESA_MODES, TABLE_HH: HH_MODES}

# H.264 profile / level bitmaps.
H264_PROFILE_CBP = 0x01  # Constrained Baseline
H264_PROFILE_CHP = 0x02  # Constrained High
H264_LEVEL_31 = 0x01
H264_LEVEL_32 = 0x02
H264_LEVEL_40 = 0x04
H264_LEVEL_41 = 0x08
H264_LEVEL_42 = 0x10
# level bit -> (level_idc, MaxMBPS per ITU-T H.264 Table A-1)
_LEVELS = (
    (H264_LEVEL_31, 31, 108000),
    (H264_LEVEL_32, 32, 216000),
    (H264_LEVEL_40, 40, 245760),
    (H264_LEVEL_41, 41, 245760),
    (H264_LEVEL_42, 42, 522240),
)
LEVEL_IDC = {bit: idc for bit, idc, _ in _LEVELS}

# Audio bitmaps (LPCM 44.1k/48k stereo; AAC 48k stereo).
LPCM_44K_2CH = 0x1
LPCM_48K_2CH = 0x2
AAC_48K_2CH = 0x1

DEFAULT_RTP_PORT = 19000


@dataclass(frozen=True)
class VideoMode:
    width: int
    height: int
    fps: int
    table: int = TABLE_CEA
    index: int = 0

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def __str__(self) -> str:
        return f"{self.width}x{self.height}p{self.fps} ({TABLE_NAMES[self.table]}#{self.index})"


# Fallback when the sink tells us nothing usable (observed with Galaxy Tab
# Smart View via gnome-network-displays: 1080p is what ends up negotiated).
FALLBACK_MODE = VideoMode(1920, 1080, 30, TABLE_CEA, 7)
MANDATORY_MODE = VideoMode(640, 480, 60, TABLE_CEA, 0)


def modes_in_bitmap(table: int, bitmap: int) -> list[VideoMode]:
    out = []
    for bit, (w, h, r, interlaced) in enumerate(TABLES[table]):
        if bitmap & (1 << bit) and not interlaced:
            out.append(VideoMode(w, h, r, table, bit))
    return out


def find_mode(width: int, height: int, fps: int) -> Optional[VideoMode]:
    """Look a progressive mode up in CEA, then VESA, then HH."""
    for table in (TABLE_CEA, TABLE_VESA, TABLE_HH):
        for bit, (w, h, r, interlaced) in enumerate(TABLES[table]):
            if (w, h, r) == (width, height, fps) and not interlaced:
                return VideoMode(w, h, r, table, bit)
    return None


# ---------------------------------------------------------------- source caps

def cea_bitmap(width: int, height: int, fps: int) -> int:
    """Progressive CEA modes that fit within width x height at <= fps."""
    mask = 0
    for bit, (w, h, r, interlaced) in enumerate(CEA_MODES):
        if not interlaced and w <= width and h <= height and r <= fps:
            mask |= 1 << bit
    return mask | 1  # 640x480p60 is mandatory


def vesa_bitmap(width: int, height: int, fps: int) -> int:
    mask = 0
    for bit, (w, h, r, _i) in enumerate(VESA_MODES):
        if w <= width and h <= height and r <= fps:
            mask |= 1 << bit
    return mask


def cea_native_index(width: int, height: int, fps: int) -> int:
    """Best progressive CEA index at or below the requested mode (0 = VGA)."""
    best, best_key = 0, (0, 0)
    for bit, (w, h, r, interlaced) in enumerate(CEA_MODES):
        if interlaced or w > width or h > height or r > fps:
            continue
        key = (w * h, r)
        if key > best_key:
            best, best_key = bit, key
    return best


def h264_level_for(width: int, height: int, fps: int) -> int:
    """Smallest WFD H.264 level bit whose macroblock rate covers the mode."""
    mbps = ((width + 15) // 16) * ((height + 15) // 16) * fps
    for bit, _idc, max_mbps in _LEVELS:
        if mbps <= max_mbps:
            return bit
    return H264_LEVEL_42


def wfd_video_formats(width: int, height: int, fps: int, profile: int = H264_PROFILE_CBP) -> str:
    """Source capability string (used when a sink queries *us*)."""
    native = (cea_native_index(width, height, fps) << 3) | TABLE_CEA
    return (
        f"{native:02x} 00 {profile:02x} {h264_level_for(width, height, fps):02x} "
        f"{cea_bitmap(width, height, fps):08x} {vesa_bitmap(width, height, fps):08x} 00000000 "
        "00 0000 0000 00 none none"
    )


def wfd_audio_codecs() -> str:
    return f"LPCM {LPCM_44K_2CH | LPCM_48K_2CH:08x} 00, AAC {AAC_48K_2CH:08x} 00"


def requested_keys(body: str) -> list[str]:
    """Parameter names listed in a GET_PARAMETER body (one per line)."""
    return [line.strip() for line in body.splitlines() if line.strip()]


# ------------------------------------------------------------ sink parsing

def parse_parameters(body: str) -> dict[str, str]:
    """`key: value` lines of a text/parameters body (keys lower-cased)."""
    out: dict[str, str] = {}
    for line in body.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        out[key.strip().lower()] = value.strip()
    return out


def _hex(token: str) -> int:
    return int(token, 16) if token and token.lower() != "none" else 0


@dataclass
class H264Codec:
    profile: int
    level: int
    cea: int
    vesa: int
    hh: int
    latency: int = 0
    min_slice_size: int = 0
    slice_enc_params: int = 0
    frame_rate_control: int = 0
    max_hres: Optional[int] = None
    max_vres: Optional[int] = None

    def modes(self) -> list[VideoMode]:
        return (
            modes_in_bitmap(TABLE_CEA, self.cea | 1)
            + modes_in_bitmap(TABLE_VESA, self.vesa)
            + modes_in_bitmap(TABLE_HH, self.hh)
        )

    @property
    def max_level(self) -> int:
        best = H264_LEVEL_31
        for bit, _idc, _m in _LEVELS:
            if self.level & bit:
                best = bit
        return best

    def supports_level_for(self, mode: VideoMode) -> bool:
        return h264_level_for(mode.width, mode.height, mode.fps) <= self.max_level


@dataclass
class SinkVideoCaps:
    native_table: int
    native_index: int
    preferred_display_mode: int
    codecs: list[H264Codec] = field(default_factory=list)

    @property
    def native_mode(self) -> Optional[VideoMode]:
        table = TABLES.get(self.native_table)
        if table is None or self.native_index >= len(table):
            return None
        w, h, r, interlaced = table[self.native_index]
        if interlaced:
            return None
        return VideoMode(w, h, r, self.native_table, self.native_index)


def parse_video_formats(value: str) -> Optional[SinkVideoCaps]:
    """Parse a sink's `wfd_video_formats` (R1 layout). Returns None for `none`/garbage."""
    value = (value or "").strip()
    if not value or value.lower() == "none":
        return None
    parts = value.split(None, 2)
    if len(parts) != 3:
        log.warning("wfd_video_formats malformed: %r", value)
        return None
    try:
        native = int(parts[0], 16)
        preferred = int(parts[1], 16)
    except ValueError:
        log.warning("wfd_video_formats malformed header: %r", value)
        return None
    caps = SinkVideoCaps(native & 0x7, native >> 3, preferred)
    for desc in parts[2].split(","):
        f = desc.split()
        if len(f) < 9:
            log.warning("skipping short H.264 descriptor: %r", desc)
            continue
        try:
            caps.codecs.append(
                H264Codec(
                    profile=int(f[0], 16),
                    level=int(f[1], 16),
                    cea=int(f[2], 16),
                    vesa=int(f[3], 16),
                    hh=int(f[4], 16),
                    latency=int(f[5], 16),
                    min_slice_size=int(f[6], 16),
                    slice_enc_params=int(f[7], 16),
                    frame_rate_control=int(f[8], 16),
                    max_hres=_hex(f[9]) or None if len(f) > 9 else None,
                    max_vres=_hex(f[10]) or None if len(f) > 10 else None,
                )
            )
        except ValueError:
            log.warning("skipping malformed H.264 descriptor: %r", desc)
    return caps


@dataclass(frozen=True)
class AudioCodec:
    name: str  # LPCM | AAC | AC3
    modes: int
    latency: int = 0


def parse_audio_codecs(value: str) -> list[AudioCodec]:
    value = (value or "").strip()
    if not value or value.lower() == "none":
        return []
    out = []
    for desc in value.split(","):
        f = desc.split()
        if len(f) < 2:
            continue
        try:
            out.append(AudioCodec(f[0].upper(), int(f[1], 16), int(f[2], 16) if len(f) > 2 else 0))
        except ValueError:
            continue
    return out


def parse_client_rtp_ports(value: str) -> tuple[int, int]:
    """`RTP/AVP/UDP;unicast 19000 0 mode=play` -> (19000, 0).

    RTCP port 0 (or equal to RTP) means the sink does not want RTCP.
    """
    nums = [int(tok) for tok in (value or "").split() if tok.isdigit()]
    rtp = nums[0] if nums and nums[0] > 0 else DEFAULT_RTP_PORT
    rtcp = nums[1] if len(nums) > 1 else 0
    if rtcp == rtp:
        rtcp = 0
    return rtp, rtcp


def parse_display_edid(value: str) -> Optional[bytes]:
    """`wfd_display_edid: 0001 00ffffff...` -> raw EDID bytes."""
    value = (value or "").strip()
    if not value or value.lower() == "none":
        return None
    parts = value.split()
    if len(parts) != 2:
        return None
    try:
        blocks = int(parts[0], 16)
        raw = bytes.fromhex(parts[1])
    except ValueError:
        return None
    if blocks <= 0 or len(raw) != 128 * blocks:
        return None
    return raw


@dataclass
class SinkCaps:
    video: Optional[SinkVideoCaps] = None
    audio: list[AudioCodec] = field(default_factory=list)
    rtp_port: int = DEFAULT_RTP_PORT
    rtcp_port: int = 0
    edid: Optional[bytes] = None
    idr_request: bool = False
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def content_protection(self) -> str:
        """Sink's wfd_content_protection answer ("" when not asked / not sent)."""
        return self.raw.get("wfd_content_protection", "").strip()

    @property
    def offers_hdcp(self) -> bool:
        return self.content_protection.upper().startswith("HDCP")

    @property
    def supports_aac(self) -> bool:
        return any(a.name == "AAC" and a.modes & AAC_48K_2CH for a in self.audio)


def parse_sink_caps(body: str) -> SinkCaps:
    params = parse_parameters(body)
    caps = SinkCaps(raw=params)
    caps.video = parse_video_formats(params.get("wfd_video_formats", ""))
    caps.audio = parse_audio_codecs(params.get("wfd_audio_codecs", ""))
    if "wfd_client_rtp_ports" in params:
        caps.rtp_port, caps.rtcp_port = parse_client_rtp_ports(params["wfd_client_rtp_ports"])
    caps.edid = parse_display_edid(params.get("wfd_display_edid", ""))
    caps.idr_request = params.get("wfd_idr_request_capability", "").strip() == "1"
    return caps


# ------------------------------------------------------------ negotiation

# Same set gnome-network-displays queries (it mirrors to Galaxy tablets fine).
# `wfd_content_protection` is deliberately *not* asked for: we never run HDCP,
# and a Samsung sink that is asked for it and answers HDCP2.x then expects an
# HDCP session and shows "can't display content because it's not secure".
M3_QUERY_KEYS = (
    "wfd_video_formats",
    "wfd_audio_codecs",
    "wfd_client_rtp_ports",
    "wfd_display_edid",
    "wfd_idr_request_capability",
)
M3_HDCP_PROBE_KEY = "wfd_content_protection"

# Every parameter defined by WFD R1/R2; anything else a sink sends is vendor-specific.
STANDARD_PARAMS = frozenset({
    "wfd_audio_codecs", "wfd_video_formats", "wfd_3d_video_formats", "wfd_content_protection",
    "wfd_display_edid", "wfd_coupled_sink", "wfd_trigger_method", "wfd_presentation_url",
    "wfd_client_rtp_ports", "wfd_route", "wfd_i2c", "wfd_av_format_change_timing",
    "wfd_preferred_display_mode", "wfd_uibc_capability", "wfd_uibc_setting",
    "wfd_standby_resume_capability", "wfd_standby", "wfd_connector_type", "wfd_idr_request",
    "wfd_idr_request_capability", "wfd2_audio_codecs", "wfd2_video_codecs",
    "wfd2_aux_stream_formats", "wfd2_buffer_length", "wfd2_audio_playback_status",
    "wfd2_video_playback_status", "wfd2_cta_datablock_collection",
})


def m3_query_keys(probe_hdcp: bool = False) -> tuple[str, ...]:
    """Keys for the M3 GET_PARAMETER body. `probe_hdcp` adds wfd_content_protection (diagnostics only)."""
    return M3_QUERY_KEYS + ((M3_HDCP_PROBE_KEY,) if probe_hdcp else ())


def vendor_params(params: dict[str, str]) -> dict[str, str]:
    """Non-standard (e.g. Samsung-specific) keys in a sink's parameter set."""
    return {k: v for k, v in params.items() if k not in STANDARD_PARAMS}


@dataclass(frozen=True)
class Negotiated:
    mode: VideoMode
    level: int
    profile: int = H264_PROFILE_CBP
    audio: Optional[str] = None  # descriptor for M4, or None (video only)
    reason: str = ""


def _aspect(w: int, h: int) -> float:
    return round(w / h, 2)


def select_video(
    caps: Optional[SinkVideoCaps],
    want: Optional[tuple[int, int]] = None,
    max_fps: int = 60,
    max_pixels: int = 1920 * 1200,
    target_aspect: Optional[float] = None,
) -> tuple[VideoMode, int, str]:
    """Pick (mode, level_bit, reason) from sink caps.

    Policy: honour an explicit `want` size if the sink lists it; otherwise
    prefer the sink/tablet aspect ratio (16:10 tablets → VESA 1920x1200), then
    most pixels, then highest fps <= max_fps. No usable caps → 1080p30.
    """
    if caps is None or not caps.codecs:
        return FALLBACK_MODE, h264_level_for(1920, 1080, 30), "sink sent no video caps; 1080p30 fallback"

    candidates: list[tuple[VideoMode, H264Codec]] = []
    for codec in caps.codecs:
        for mode in codec.modes():
            if mode.fps <= max_fps and mode.pixels <= max_pixels and codec.supports_level_for(mode):
                candidates.append((mode, codec))
    if not candidates:
        return FALLBACK_MODE, h264_level_for(1920, 1080, 30), "no sink mode fits limits; 1080p30 fallback"

    if want:
        exact = [c for c in candidates if (c[0].width, c[0].height) == tuple(want)]
        if exact:
            mode, codec = max(exact, key=lambda c: c[0].fps)
            return mode, codec.max_level, f"requested {want[0]}x{want[1]} supported by sink"
        log.info("sink does not list %sx%s; choosing automatically", *want)

    if target_aspect is None:
        native = caps.native_mode
        if native and native.table != TABLE_CEA:
            target_aspect = _aspect(native.width, native.height)

    def key(c: tuple[VideoMode, H264Codec]):
        m = c[0]
        aspect_ok = target_aspect is not None and abs(_aspect(m.width, m.height) - target_aspect) < 0.02
        return (aspect_ok, m.pixels, m.fps)

    mode, codec = max(candidates, key=key)
    return mode, codec.max_level, "best sink mode" + (f" for aspect {target_aspect}" if target_aspect else "")


def negotiate(
    sink: SinkCaps,
    want: Optional[tuple[int, int]] = None,
    max_fps: int = 60,
    max_pixels: int = 1920 * 1200,
    target_aspect: Optional[float] = None,
    audio: bool = False,
) -> Negotiated:
    if target_aspect is None and sink.edid:
        from ..display.edid import parse_edid

        info = parse_edid(sink.edid)
        if info and info.preferred:
            target_aspect = _aspect(info.preferred[0], info.preferred[1])
    mode, level, reason = select_video(sink.video, want, max_fps, max_pixels, target_aspect)
    audio_desc = f"AAC {AAC_48K_2CH:08x} 00" if audio and sink.supports_aac else None
    return Negotiated(mode=mode, level=level, audio=audio_desc, reason=reason)


def m4_video_descriptor(mode: VideoMode, level: int, profile: int = H264_PROFILE_CBP) -> str:
    """Single-mode `wfd_video_formats` value for M4 (exactly one bit set)."""
    bits = {TABLE_CEA: 0, TABLE_VESA: 0, TABLE_HH: 0}
    bits[mode.table] = 1 << mode.index
    return (
        f"00 00 {profile:02x} {level:02x} {bits[TABLE_CEA]:08x} {bits[TABLE_VESA]:08x} "
        f"{bits[TABLE_HH]:08x} 00 0000 0000 00 none none"
    )
