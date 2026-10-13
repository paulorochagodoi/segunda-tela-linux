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


def mode_mbps(width: int, height: int, fps: int) -> int:
    return ((width + 15) // 16) * ((height + 15) // 16) * fps


def h264_level_for(width: int, height: int, fps: int) -> int:
    """Smallest WFD H.264 level bit whose macroblock rate covers the mode."""
    mbps = mode_mbps(width, height, fps)
    for bit, _idc, max_mbps in _LEVELS:
        if mbps <= max_mbps:
            return bit
    return H264_LEVEL_42


def mode_within_level(mode: VideoMode, level_byte: int) -> bool:
    """True when some advertised H.264 level can carry this mode's macroblock rate.

    Level 0x01 is only 3.1 (108000 MB/s): 1280x720p30 fits, 720p60 (3.2) and
    1080p (4.x) do not. A higher advertised bit may carry a lower mode.
    """
    cap = 0
    highest = 0
    for bit, _idc, max_mbps in _LEVELS:
        if level_byte & bit and bit >= highest:
            highest = bit
            cap = max_mbps
    if not highest or mode_mbps(mode.width, mode.height, mode.fps) > cap:
        return False
    return h264_level_for(mode.width, mode.height, mode.fps) <= highest


def gnd_bitrate_kbps(level_byte: int, profile: int) -> int:
    """gnome-network-displays caps the initial bitrate at 4096 kbit/s.

    The per-level table is the one in wfd_video_codec_get_max_bitrate_kbit;
    every defined level is then clamped with MIN(..., 512 * 8).
    """
    table = {0x01: 14000, 0x02: 20000, 0x04: 20000, 0x08: 50000, 0x10: 50000}
    bitrate = table.get(level_byte, 14000)
    if profile == H264_PROFILE_CHP:
        bitrate = int(bitrate * 1.25)
    return min(bitrate, 4096)


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
        return mode_within_level(mode, self.level)


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

    @property
    def supports_lpcm48(self) -> bool:
        return any(a.name == "LPCM" and a.modes & LPCM_48K_2CH for a in self.audio)


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
# gnome-network-displays wfd_params_m3_query_params: mandatory, then the extra
# three. It does not ask wfd_content_protection or wfd_coupled_sink.
M3_QUERY_KEYS = (
    "wfd_client_rtp_ports",
    "wfd_audio_codecs",
    "wfd_video_formats",
    "wfd_display_edid",
    "wfd_idr_request_capability",
    "microsoft_cursor",
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
    level: int  # level byte copied into M4 (the sink codec's advertised bitmap)
    profile: int = H264_PROFILE_CBP
    audio: Optional[str] = None  # M4 wfd_audio_codecs descriptor
    reason: str = ""
    # Single level bit the encoder must write into the SPS. 0 leaves the
    # encoder alone (the gnome-network-displays path, which never sets a level).
    stream_level: int = 0
    latency: int = 0
    min_slice_size: int = 0
    frame_rate_control: int = 0
    bitrate_kbps: int = 4096

    @property
    def level_idc(self) -> Optional[int]:
        if not self.stream_level:
            return None
        return LEVEL_IDC.get(self.stream_level)

    @property
    def gst_profile(self) -> str:
        return "high" if self.profile == H264_PROFILE_CHP else "constrained-baseline"

    @property
    def audio_kind(self) -> Optional[str]:
        if not self.audio:
            return None
        name = self.audio.split()[0].upper()
        if name == "AAC":
            return "aac"
        if name == "LPCM":
            return "lpcm"
        return None


def _profile_of(codec: H264Codec, *, allow_chp: bool) -> int:
    if codec.profile & H264_PROFILE_CBP:
        return H264_PROFILE_CBP
    if allow_chp and codec.profile & H264_PROFILE_CHP:
        return H264_PROFILE_CHP
    return H264_PROFILE_CBP


def _prefer_codec(codecs: list[H264Codec], *, allow_chp: bool) -> Optional[H264Codec]:
    """gnome-network-displays: first codec, then one whose profile is CBP, then a higher level."""
    chosen: Optional[H264Codec] = None
    for item in codecs:
        if not allow_chp and not (item.profile & H264_PROFILE_CBP):
            continue
        if chosen is None:
            chosen = item
            continue
        item_cbp = bool(item.profile & H264_PROFILE_CBP)
        chosen_cbp = bool(chosen.profile & H264_PROFILE_CBP)
        if item_cbp and not chosen_cbp:
            chosen = item
        elif item_cbp == chosen_cbp and item.level > chosen.level:
            chosen = item
    return chosen


def _pick_video(
    caps: Optional[SinkVideoCaps],
    want: Optional[tuple[int, int]] = None,
    max_fps: int = 60,
    max_pixels: int = 1920 * 1200,
    allow_chp: bool = True,
    gnd_compat: bool = False,
) -> tuple[VideoMode, Optional[H264Codec], str]:
    if caps is None or not caps.codecs:
        return FALLBACK_MODE, None, "sink sent no video caps; 1080p30 fallback"
    codec = _prefer_codec(caps.codecs, allow_chp=allow_chp)
    if codec is None:
        return FALLBACK_MODE, None, "no usable H.264 profile; 1080p30 fallback"
    if gnd_compat:
        # wfd_client_select_codec_and_resolution compiles the native-resolution
        # search out (#if 0) and always falls back to 1920x1080p30.
        return FALLBACK_MODE, codec, "gnome-network-displays hardcoded 1920x1080p30"

    candidates: list[tuple[VideoMode, H264Codec]] = []
    for item in caps.codecs:
        if not allow_chp and not (item.profile & H264_PROFILE_CBP):
            continue
        for mode in item.modes():
            if mode.fps > max_fps or mode.pixels > max_pixels:
                continue
            if not item.supports_level_for(mode):
                continue
            candidates.append((mode, item))
    if not candidates:
        return FALLBACK_MODE, codec, "no listed mode fits the sink H.264 level; 1080p30 fallback"

    def key(c: tuple[VideoMode, H264Codec]):
        mode = c[0]
        exact = bool(want) and (mode.width, mode.height) == (want[0], want[1])
        inside = bool(want) and mode.width <= want[0] and mode.height <= want[1]
        cbp = 1 if c[1].profile & H264_PROFILE_CBP else 0
        return (exact, inside, mode.pixels, mode.fps, cbp)

    mode, chosen = max(candidates, key=key)
    if want and (mode.width, mode.height) == (want[0], want[1]):
        reason = f"requested {want[0]}x{want[1]} within sink level 0x{chosen.level:02x}"
    else:
        reason = f"best mode within sink level 0x{chosen.level:02x}"
    return mode, chosen, reason


def select_video(
    caps: Optional[SinkVideoCaps],
    want: Optional[tuple[int, int]] = None,
    max_fps: int = 60,
    max_pixels: int = 1920 * 1200,
    target_aspect: Optional[float] = None,
    allow_chp: bool = True,
    gnd_compat: bool = False,
) -> tuple[VideoMode, int, int, str]:
    """Pick (mode, m4_level_byte, profile, reason).

    Default: the largest progressive mode the sink listed whose macroblock
    rate fits the advertised H.264 level (CBP over CHP). The Tab S10 FE's
    level byte 0x01 is only 3.1, so 720p30 is in and 720p60/1080p are not.
    The returned level byte is the sink codec's bitmap, echoed in M4.

    `gnd_compat` is the literal gnome-network-displays choice: 1920x1080p30.
    `target_aspect` is accepted for callers and is not a tie-break; pixel
    count and frame rate decide.
    """
    del target_aspect  # kept so existing callers do not break
    mode, codec, reason = _pick_video(caps, want, max_fps, max_pixels, allow_chp, gnd_compat)
    if codec is None:
        return mode, h264_level_for(mode.width, mode.height, mode.fps), H264_PROFILE_CBP, reason
    return mode, codec.level, _profile_of(codec, allow_chp=allow_chp), reason


def select_audio(sink: SinkCaps) -> Optional[str]:
    """AAC 48 kHz stereo when the sink lists it, else LPCM 48 kHz.

    Descriptor text matches wfd_audio_get_descriptor (`AAC %08X %02X`).
    gnome-network-displays only selects AAC mode 0x1; LPCM is the fallback
    for a sink that did not offer that AAC mode. A silent track is muxed
    either way — Samsung treats a video-only MPEG-TS as not secure.
    """
    if sink.supports_aac:
        return f"AAC {AAC_48K_2CH:08X} 00"
    if sink.supports_lpcm48:
        return f"LPCM {LPCM_48K_2CH:08X} 00"
    return None


def negotiate(
    sink: SinkCaps,
    want: Optional[tuple[int, int]] = None,
    max_fps: int = 60,
    max_pixels: int = 1920 * 1200,
    target_aspect: Optional[float] = None,
    audio: bool = True,
    encoder: str = "auto",
    gnd_compat: bool = False,
) -> Negotiated:
    del target_aspect
    allow_chp = encoder != "openh264enc"
    mode, codec, reason = _pick_video(
        sink.video, want, max_fps, max_pixels, allow_chp=allow_chp, gnd_compat=gnd_compat,
    )
    if codec is None:
        level, profile = h264_level_for(mode.width, mode.height, mode.fps), H264_PROFILE_CBP
        latency = min_slice = frame_rate = 0
    else:
        level, profile = codec.level, _profile_of(codec, allow_chp=allow_chp)
        latency, min_slice, frame_rate = codec.latency, codec.min_slice_size, codec.frame_rate_control
    # The literal GND path never writes an SPS level. Otherwise the SPS level
    # is exactly the one bit this mode needs (3.1 for 720p30).
    stream_level = 0 if gnd_compat else h264_level_for(mode.width, mode.height, mode.fps)
    audio_desc = select_audio(sink) if audio else None
    return Negotiated(
        mode=mode, level=level, profile=profile, audio=audio_desc, reason=reason,
        stream_level=stream_level, latency=latency, min_slice_size=min_slice,
        frame_rate_control=frame_rate, bitrate_kbps=gnd_bitrate_kbps(level, profile),
    )


def m4_video_descriptor(
    mode: VideoMode,
    level: int,
    profile: int = H264_PROFILE_CBP,
    latency: int = 0,
    min_slice_size: int = 0,
    frame_rate_control: int = 0,
) -> str:
    """Single-mode `wfd_video_formats` value, same layout as gnome-network-displays.

    Profile, level and bitmaps are uppercase; the frame-rate-control byte is
    the sink's skipping bit (`%02x` in wfd_video_codec_get_descriptor_for_resolution).
    Slice-encoding params are forced to 0, as that function does.
    """
    bits = {TABLE_CEA: 0, TABLE_VESA: 0, TABLE_HH: 0}
    bits[mode.table] = 1 << mode.index
    return (
        f"00 00 {profile:02X} {level:02X} {bits[TABLE_CEA]:08X} {bits[TABLE_VESA]:08X} "
        f"{bits[TABLE_HH]:08X} {latency:02X} {min_slice_size:04X} 0000 {frame_rate_control & 0x1:02x} none none"
    )
