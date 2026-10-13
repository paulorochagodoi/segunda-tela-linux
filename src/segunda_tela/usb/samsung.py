"""What Samsung Second Screen's cable mode actually does, as data (see docs/USB.md).

Findings from the Windows app (REA, clean-room — facts only, no code):

- The cable carries ordinary WFD/RTSP. Samsung's Connlib only tunnels
  individual TCP/UDP ports over USB bulk; Windows' own Miracast source does
  the projecting.
- Handshake is Miracast-over-Infrastructure style: the PC dials the tablet's
  "SourceReady" port 7250 and sends an MS-MICE SOURCE_READY; the tablet then
  connects to the PC's RTSP port 7236 and the PC sends M1.
- The tablet app is woken over Bluetooth (MCF start intent, not reproducible
  here) and told "use the cable" by a tiny JSON config file pushed over MTP
  into the app's external files dir.

On Linux, adb reverse/forward stand in for Connlib's TCP mappings.
The SOURCE_READY encoding follows Microsoft's public [MS-MICE] spec.
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Iterable

# Receiver packages, preferred first (new app, then the legacy Smart View host).
SECOND_SCREEN_PACKAGES = ("com.samsung.android.secondscreen", "com.samsung.android.smartmirroring")
# Separate "local PC" mirroring feature — not the Second Screen receiver.
LOCAL_PC_PACKAGE = "com.samsung.android.mirroringplayer"

RTSP_PORT = 7236
SOURCE_READY_PORT = 7250
SIDE_CHANNEL_PORT = 7260
RTP_PORT = 19000
RTCP_PORT = 19001
UIBC_PORT = 50000
HDCP_PORT = 25030


@dataclass(frozen=True)
class TunnelPort:
    name: str
    port: int
    proto: str  # "tcp" | "udp"
    pc_role: str  # "listen" (tablet connects to PC) | "connect" (PC dials tablet) | "send" (UDP PC → tablet)
    required: bool


# The mappings the Windows app opens over the cable, in its order.
CABLE_PORTS = (
    TunnelPort("side-channel", SIDE_CHANNEL_PORT, "tcp", "listen", True),
    TunnelPort("source-ready", SOURCE_READY_PORT, "tcp", "connect", True),
    TunnelPort("rtsp", RTSP_PORT, "tcp", "listen", True),
    TunnelPort("rtp", RTP_PORT, "udp", "send", True),
    TunnelPort("rtcp", RTCP_PORT, "udp", "send", True),
    TunnelPort("uibc", UIBC_PORT, "tcp", "listen", False),
    TunnelPort("hw-cursor", 50001, "udp", "send", False),
    TunnelPort("hw-cursor-2", 50002, "udp", "send", False),
    TunnelPort("hdcp", HDCP_PORT, "tcp", "connect", False),
)


def installed_hosts(packages: Iterable[str]) -> list[str]:
    """Second Screen receiver packages present on the tablet, preferred first."""
    have = set(packages)
    return [p for p in SECOND_SCREEN_PACKAGES if p in have]


def config_dir(package: str) -> str:
    return f"/sdcard/Android/data/{package}/files/config"


def config_path(package: str) -> str:
    return f"{config_dir(package)}/secondscreen.data"


def config_json(usb: bool = True) -> str:
    """Contents of secondscreen.data: one `USB` flag (1 = cable mode on)."""
    return json.dumps({"USB": 1 if usb else 0}, separators=(",", ":"))


# ------------------------------------------------------------------ MS-MICE
# Message: Size (u16 BE, whole message) · Version (u8 = 1) · Command (u8) · TLVs.
# TLV: Type (u8) · Length (u16 BE) · Value.
MICE_VERSION = 0x01
MICE_SOURCE_READY = 0x01
MICE_STOP_PROJECTION = 0x02
TLV_FRIENDLY_NAME = 0x00
TLV_RTSP_PORT = 0x02
TLV_SOURCE_ID = 0x03


def _tlv(kind: int, value: bytes) -> bytes:
    return struct.pack(">BH", kind, len(value)) + value


def mice_message(command: int, tlvs: list[tuple[int, bytes]]) -> bytes:
    body = b"".join(_tlv(k, v) for k, v in tlvs)
    return struct.pack(">HBB", 4 + len(body), MICE_VERSION, command) + body


def mice_source_ready(friendly_name: str, rtsp_port: int, source_id: bytes) -> bytes:
    """SOURCE_READY: tells the sink which RTSP port to connect back to."""
    if len(source_id) != 16:
        raise ValueError("source_id must be 16 bytes")
    return mice_message(MICE_SOURCE_READY, [
        (TLV_FRIENDLY_NAME, friendly_name.encode("utf-16-le")),
        (TLV_RTSP_PORT, struct.pack(">H", rtsp_port)),
        (TLV_SOURCE_ID, source_id),
    ])


def mice_stop_projection(friendly_name: str, source_id: bytes) -> bytes:
    return mice_message(MICE_STOP_PROJECTION, [
        (TLV_FRIENDLY_NAME, friendly_name.encode("utf-16-le")),
        (TLV_SOURCE_ID, source_id),
    ])


def parse_mice(data: bytes) -> tuple[int, dict[int, bytes]]:
    """→ (command, {tlv_type: value}). Raises ValueError on a malformed message."""
    if len(data) < 4:
        raise ValueError("short MICE header")
    size, version, command = struct.unpack_from(">HBB", data)
    if version != MICE_VERSION or size != len(data):
        raise ValueError(f"bad MICE header (size={size}/{len(data)}, version={version})")
    tlvs: dict[int, bytes] = {}
    i = 4
    while i < size:
        if i + 3 > size:
            raise ValueError("truncated TLV header")
        kind, length = struct.unpack_from(">BH", data, i)
        i += 3
        if i + length > size:
            raise ValueError("truncated TLV value")
        tlvs[kind] = data[i:i + length]
        i += length
    return command, tlvs
