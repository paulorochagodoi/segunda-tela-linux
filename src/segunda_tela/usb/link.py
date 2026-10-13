"""Bring up an IP path to a Galaxy Tab over USB, then reuse WFD/RTSP.

Clean-room mirror of the Windows behaviour observed via REA (docs/USB.md):

- Samsung's Connlib (`ss_conn_lib2` + the `ss_conn_service2` driver service)
  is a per-port TCP/UDP tunnel over the USB bulk endpoints of
  `USB\\VID_04E8&PID_6860`; on top of it runs ordinary WFD/RTSP from
  Windows' own Miracast source (RTSP 7236, source-ready 7250, RTP 19000).
- MTP is only a side channel for the receiver's cable-mode config file.
- On USB failure Windows falls back to Wi-Fi Direct.

We do **not** reimplement Connlib. On Linux we use standard mechanisms:

1. USB tethering (RNDIS / NCM / CDC) via NetworkManager — needed for RTP/UDP.
2. `adb reverse` / `adb forward` as the TCP port tunnel (usb/initiate.py).

The existing WFD RTSP source then binds on that link; `UsbInitiator` performs
the source-ready handshake. Wi-Fi mode stays the fallback.
"""
from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .detect import (
    SAMSUNG_VID,
    UsbDevice,
    find_usb_net_ifaces,
    list_samsung_usb_devices,
)

log = logging.getLogger(__name__)


class UsbLinkError(RuntimeError):
    pass


@dataclass
class UsbLink:
    transport: str  # "rndis" | "ncm" | "cdc" | "adb-reverse" | "adb-forward"
    iface: str = ""
    local_ip: str = ""
    peer_ip: str = ""
    rtsp_port: int = 7236
    devices: List[UsbDevice] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    _adb_reversed: bool = False

    def close(self) -> None:
        if self._adb_reversed and shutil.which("adb"):
            try:
                subprocess.run(
                    ["adb", "reverse", "--remove", f"tcp:{self.rtsp_port}"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                pass
            self._adb_reversed = False


def _iface_ipv4(iface: str) -> Optional[str]:
    try:
        out = subprocess.check_output(["ip", "-4", "-o", "addr", "show", "dev", iface], text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    # "2: usb0    inet 192.168.42.129/24 ..."
    for part in out.split():
        if "/" in part and part[0].isdigit():
            return part.split("/", 1)[0]
    return None


def _guess_peer_ip(local_ip: str) -> str:
    """Common tether peer: host .x → device .1 or .2 on same /24."""
    try:
        octets = [int(x) for x in local_ip.split(".")]
        if len(octets) != 4:
            return ""
        # Android USB tethering often gives PC 192.168.42.129 and phone .1? or reverse
        # Also 192.168.44.x / 192.168.43.x variants.
        for candidate_last in (1, 2, 129, 130):
            if candidate_last != octets[3]:
                return f"{octets[0]}.{octets[1]}.{octets[2]}.{candidate_last}"
    except ValueError:
        pass
    return ""


def _nmcli_reapply(iface: str) -> None:
    if not shutil.which("nmcli"):
        return
    try:
        subprocess.run(
            ["nmcli", "device", "reapply", iface],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


def _wait_usb_iface(timeout: float = 15.0) -> Optional[str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        ifaces = find_usb_net_ifaces()
        for iface in ifaces:
            ip = _iface_ipv4(iface)
            if ip:
                return iface
            _nmcli_reapply(iface)
        time.sleep(0.5)
    ifaces = find_usb_net_ifaces()
    return ifaces[0] if ifaces else None


def _adb_devices() -> List[str]:
    if not shutil.which("adb"):
        return []
    try:
        out = subprocess.check_output(["adb", "devices"], text=True, timeout=5)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return []
    serials = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def _setup_adb_reverse(port: int) -> tuple[bool, str]:
    serials = _adb_devices()
    if not serials:
        return False, "adb: no authorized device (enable USB debugging and accept the prompt)"
    # reverse: tablet connecting to localhost:port reaches PC:port
    try:
        subprocess.check_call(
            ["adb", "reverse", f"tcp:{port}", f"tcp:{port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            timeout=10,
        )
        return True, f"adb reverse tcp:{port} on {serials[0]}"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return False, f"adb reverse failed: {exc}"


def prepare_usb_link(
    rtsp_port: int = 7236,
    prefer_adb: bool = False,
    wait_tether_s: float = 20.0,
    sys_bus_usb: str = "/sys/bus/usb/devices",
) -> UsbLink:
    """Detect Samsung USB device and establish an IP path for WFD/RTSP."""
    devices = list_samsung_usb_devices(sys_bus_usb)
    if not devices:
        raise UsbLinkError(
            f"Nenhum dispositivo USB Samsung (VID {SAMSUNG_VID}) encontrado. "
            "Conecte o cabo, desbloqueie o tablet e escolha Transferência de arquivos "
            "ou USB tethering."
        )

    notes = [
        f"Encontrado(s) {len(devices)} dispositivo(s) Samsung: "
        + ", ".join(f"{d.pid}:{d.serial or '?'}" for d in devices),
        "No Windows, o app usa Connlib (ss_conn_lib2) para obter IP+porta e então RTSP/RTP. "
        "Aqui usamos tether RNDIS/NCM ou adb — sem reimplementar Connlib.",
    ]

    order = ("adb", "tether") if prefer_adb else ("tether", "adb")

    for kind in order:
        if kind == "tether":
            notes.append(
                "Ative no tablet: Configurações → Conexões → Hotspot e tethering → "
                "Tethering USB (ou Modem USB)."
            )
            iface = _wait_usb_iface(timeout=wait_tether_s)
            if not iface:
                notes.append("Nenhuma interface usb0/enx*/RNDIS/NCM apareceu.")
                continue
            local_ip = _iface_ipv4(iface)
            if not local_ip:
                # try dhcp via nmcli
                _nmcli_reapply(iface)
                time.sleep(1)
                local_ip = _iface_ipv4(iface)
            if not local_ip:
                notes.append(f"Interface {iface} sem IPv4.")
                continue
            peer = _guess_peer_ip(local_ip)
            transport = "rndis"
            try:
                drv = Path(f"/sys/class/net/{iface}/device/driver").resolve().name
            except OSError:
                drv = ""
            if "ncm" in drv:
                transport = "ncm"
            elif "cdc_ether" in drv or "cdc" in drv:
                transport = "cdc"
            link = UsbLink(
                transport=transport,
                iface=iface,
                local_ip=local_ip,
                peer_ip=peer,
                rtsp_port=rtsp_port,
                devices=devices,
                notes=notes
                + [
                    f"Tether OK: {iface} local={local_ip} peer≈{peer or '?'}",
                    f"WFD/RTSP será anunciado em rtsp://{local_ip}:{rtsp_port}",
                    "Se o sink do tablet não abrir sozinho, use um cliente WFD/Smart View "
                    "apontando para esse endereço, ou o modo Wi-Fi.",
                ],
            )
            return link

        if kind == "adb":
            ok, msg = _setup_adb_reverse(rtsp_port)
            notes.append(msg)
            if not ok:
                continue
            # With reverse, tablet apps use 127.0.0.1:port → PC. Advertise loopback
            # for documentation; bind stays 0.0.0.0 on PC.
            link = UsbLink(
                transport="adb-reverse",
                iface="adb",
                local_ip="127.0.0.1",
                peer_ip="127.0.0.1",
                rtsp_port=rtsp_port,
                devices=devices,
                notes=notes
                + [
                    "adb reverse ativo: no tablet, um sink que conecte em "
                    f"rtsp://127.0.0.1:{rtsp_port} alcança este PC.",
                    "Apps Samsung Second Screen/MCF provavelmente ignoram isso "
                    "(esperam Connlib). Útil para sinks/testes abertos.",
                ],
                _adb_reversed=True,
            )
            return link

    raise UsbLinkError(
        "USB Samsung detectado, mas não houve tether IP nem adb utilizável.\n"
        + "\n".join(notes)
        + "\n\nDica: ative Tethering USB no tablet, ou USB debugging (adb). "
        "O protocolo Connlib proprietário da Samsung não está implementado."
    )


# re-export
__all__ = ["UsbLink", "UsbLinkError", "prepare_usb_link", "list_samsung_usb_devices"]
