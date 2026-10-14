"""USB path for the VNC socket: adb reverse, or the tether IP if adb cannot.

`adb reverse tcp:5900 tcp:5900` needs no USB tethering. The tablet dials
its own 127.0.0.1:5900 and the bytes come out on the PC's 127.0.0.1:5900.
The server binds that address only. When reverse is unavailable, the
tablet dials the PC's address on the tether subnet, and the server binds
127.0.0.1 plus that address — never 0.0.0.0.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlencode

from ..usb.adb import Adb
from ..usb.link import _iface_ipv4, _wait_usb_iface

log = logging.getLogger(__name__)

VNC_PORT = 5900
BVNC_PACKAGE = "com.iiordanov.freebVNC"


class VncLinkError(RuntimeError):
    pass


def listen_plan(kind: str, tether_ip: str = "") -> tuple[list[str], str]:
    """(bind hosts, host the tablet's VNC client should dial)."""
    if kind == "adb-reverse":
        return ["127.0.0.1"], "127.0.0.1"
    if kind == "tether":
        if not tether_ip:
            raise ValueError("tether sem IP")
        hosts = ["127.0.0.1"]
        if tether_ip not in hosts:
            hosts.append(tether_ip)
        return hosts, tether_ip
    raise ValueError(kind)


def bvnc_uri(host: str, port: int, password: str) -> str:
    """bVNC VIEW intent. `VncPassword` is the parameter bVNC documents for RFC 7869."""
    query = urlencode({"VncPassword": password})
    return f"vnc://{host}:{port}?{query}"


def bvnc_shell(host: str, port: int, password: str) -> str:
    uri = bvnc_uri(host, port, password)
    return (
        "am start -a android.intent.action.VIEW "
        f"-p {BVNC_PACKAGE} -d {shlex.quote(uri)}"
    )


@dataclass
class VncTransport:
    kind: str  # "adb-reverse" | "tether"
    bind_hosts: list[str]
    client_host: str
    port: int
    can_shell: bool
    notes: list[str] = field(default_factory=list)
    _adb: Optional[Adb] = None
    _reversed: bool = False

    def close(self) -> None:
        if self._reversed and self._adb is not None:
            self._adb.remove_reverse(self.port)
            self._reversed = False


def launch_bvnc(adb: Adb, host: str, port: int, password: str) -> tuple[bool, str]:
    """Open bVNC Free on the tablet. The password is not written to the log."""
    pkgs = adb.packages()
    if BVNC_PACKAGE not in pkgs:
        return False, (
            f"{BVNC_PACKAGE} não está instalado. Na Play Store, instale "
            "\"bVNC\" (bVNC: Secure VNC Viewer, pacote com.iiordanov.freebVNC)."
        )
    rc, out = adb.shell(bvnc_shell(host, port, password), timeout=15, redact=password)
    text = (out or "").strip()
    if rc == 0 and "Error" not in text and "Exception" not in text:
        return True, text or "bVNC aberto"
    return False, text or f"am start rc={rc}"


def prepare_vnc_transport(
    port: int = VNC_PORT,
    wait_tether_s: float = 20.0,
    adb: Optional[Adb] = None,
    wait_iface: Optional[Callable[[float], Optional[str]]] = None,
    iface_ip: Optional[Callable[[str], Optional[str]]] = None,
) -> VncTransport:
    """adb reverse first. USB tethering only if reverse cannot be set up."""
    adb = adb if adb is not None else Adb()
    wait_iface = wait_iface or _wait_usb_iface
    iface_ip = iface_ip or _iface_ipv4
    notes: list[str] = []
    can_shell = False

    if not adb.available():
        notes.append("adb ausente — sudo apt install adb. Sem adb não há túnel reverso nem o bVNC abre sozinho.")
    else:
        _serial, state = adb.select()
        if state == "device":
            can_shell = True
            if adb.reverse(port):
                hosts, client = listen_plan("adb-reverse")
                notes.append(
                    f"adb reverse tcp:{port} tcp:{port}: o tablet liga em 127.0.0.1:{port} "
                    "(não precisa de tethering USB)."
                )
                return VncTransport(
                    kind="adb-reverse", bind_hosts=hosts, client_host=client, port=port,
                    can_shell=True, notes=notes, _adb=adb, _reversed=True,
                )
            notes.append(f"adb reverse tcp:{port} falhou; tentando tethering USB.")
        elif state == "unauthorized":
            notes.append("adb NÃO autorizado — desbloqueie o tablet e aceite 'Permitir depuração USB'.")
        elif state == "none":
            notes.append("nenhum aparelho no adb — ative Depuração USB e aceite o aviso.")
        else:
            notes.append(f"adb em estado {state}.")

    iface = wait_iface(wait_tether_s)
    ip = iface_ip(iface) if iface else None
    if iface and ip:
        hosts, client = listen_plan("tether", ip)
        notes.append(
            f"Tethering USB: {iface} {ip}. No tablet o bVNC liga em {ip}:{port} "
            "(esse é o IP do PC, não o do tablet)."
        )
        return VncTransport(
            kind="tether", bind_hosts=hosts, client_host=client, port=port,
            can_shell=can_shell, notes=notes, _adb=adb if can_shell else None, _reversed=False,
        )
    if iface and not ip:
        notes.append(f"Interface {iface} sem IPv4. Ative o Tethering USB e espere o DHCP.")
    else:
        notes.append(
            "Sem interface usb0/enx. Para o fallback: Configurações → Conexões → "
            "Hotspot e tethering → Tethering USB."
        )
    raise VncLinkError(
        "Não há adb reverse nem IP de tethering.\n" + "\n".join(notes) +
        "\n\nLigue o cabo, ative a Depuração USB (e aceite o aviso) ou o Tethering USB."
    )
