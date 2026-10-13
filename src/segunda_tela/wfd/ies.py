"""WFD Information Element subelements (Wi-Fi Display spec v1.0, section 5.1).

NetworkManager exposes/accepts the *subelement* payload (without the vendor
IE header/OUI): a sequence of `id (1 byte) | length (2 bytes BE) | body`.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

SUBELEM_DEVICE_INFO = 0x00
SUBELEM_ASSOCIATED_BSSID = 0x01
SUBELEM_COUPLED_SINK = 0x06
SUBELEM_EXT_CAPABILITY = 0x07

DEVICE_TYPES = {0: "source", 1: "primary-sink", 2: "secondary-sink", 3: "dual-role"}

# WFD Device Information bitmap.
_TYPE_MASK = 0x0003
_SESSION_AVAILABLE = 0x0010  # bits 5:4 == 01
_SERVICE_DISCOVERY = 0x0040
_PREFER_TDLS = 0x0080
_CONTENT_PROTECTION = 0x0100
_TIME_SYNC = 0x0200


@dataclass(frozen=True)
class WfdDeviceInfo:
    device_type: int
    session_available: bool
    rtsp_port: int
    max_throughput_mbps: int
    content_protection: bool = False
    raw_info: int = 0

    @property
    def type_name(self) -> str:
        return DEVICE_TYPES.get(self.device_type, f"type{self.device_type}")

    @property
    def is_sink(self) -> bool:
        return self.device_type in (1, 2, 3)


def build_source_ies(
    rtsp_port: int = 7236,
    max_throughput_mbps: int = 200,
    session_available: bool = True,
    prefer_tdls_bit: bool = True,
) -> bytes:
    """Device-information subelement advertising us as a WFD source.

    `prefer_tdls_bit` defaults on because that exact flag combination
    (0x0090) is what a Galaxy Tab was observed to accept from a Linux source.
    """
    info = 0  # device type 0 = source
    if session_available:
        info |= _SESSION_AVAILABLE
    if prefer_tdls_bit:
        info |= _PREFER_TDLS
    body = struct.pack(">HHH", info, rtsp_port, max_throughput_mbps)
    return struct.pack(">BH", SUBELEM_DEVICE_INFO, len(body)) + body


def iter_subelements(data: bytes):
    i = 0
    while i + 3 <= len(data):
        sid = data[i]
        (length,) = struct.unpack(">H", data[i + 1 : i + 3])
        body = data[i + 3 : i + 3 + length]
        if len(body) < length:
            return
        yield sid, body
        i += 3 + length


def parse_wfd_ies(data: Optional[bytes]) -> Optional[WfdDeviceInfo]:
    """Extract the device-information subelement; None if absent/garbled."""
    if not data:
        return None
    data = bytes(data)
    # Tolerate a full vendor IE (dd len 50:6f:9a 0a ...) as well as raw subelements.
    if len(data) > 6 and data[0] == 0xDD and data[2:6] == b"\x50\x6f\x9a\x0a":
        data = data[6:]
    for sid, body in iter_subelements(data):
        if sid == SUBELEM_DEVICE_INFO and len(body) >= 6:
            info, port, tput = struct.unpack(">HHH", body[:6])
            return WfdDeviceInfo(
                device_type=info & _TYPE_MASK,
                session_available=(info >> 4) & 0x3 == 0x1,
                rtsp_port=port,
                max_throughput_mbps=tput,
                content_protection=bool(info & _CONTENT_PROTECTION),
                raw_info=info,
            )
    return None
