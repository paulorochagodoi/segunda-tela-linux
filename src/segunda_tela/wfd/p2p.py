"""Wi-Fi P2P (Wi-Fi Direct) discovery + connection through NetworkManager D-Bus.

Flow (same NetworkManager API that gnome-network-displays relies on):

1. Find the NM device of type WIFI_P2P (e.g. `p2p-dev-wlo1`).
2. `Device.WifiP2P.StartFind` and read `Peers`; each `WifiP2PPeer` exposes
   `Name`, `HwAddress`, `Strength`, `WfdIEs` (sinks advertise a WFD device
   information subelement there).
3. `AddAndActivateConnection2` with a volatile `wifi-p2p` connection carrying
   *our* source WFD IEs, bound to this D-Bus client (torn down if we exit).
4. Wait for the active connection to reach ACTIVATED and read the group
   interface + IPv4 address; the sink then connects to our RTSP port 7236.

Pure helpers (`peer_from_props`, `match_peer`, `connection_settings`) are unit
tested with fixtures; everything touching D-Bus is a hardware path.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .ies import WfdDeviceInfo, build_source_ies, parse_wfd_ies

log = logging.getLogger(__name__)

NM_BUS = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
NM_IFACE = "org.freedesktop.NetworkManager"
IFACE_DEVICE = "org.freedesktop.NetworkManager.Device"
IFACE_P2P_DEVICE = "org.freedesktop.NetworkManager.Device.WifiP2P"
IFACE_PEER = "org.freedesktop.NetworkManager.WifiP2PPeer"
IFACE_ACTIVE = "org.freedesktop.NetworkManager.Connection.Active"
IFACE_IP4 = "org.freedesktop.NetworkManager.IP4Config"
NM_DEVICE_TYPE_WIFI_P2P = 30
AC_STATE = {0: "unknown", 1: "activating", 2: "activated", 3: "deactivating", 4: "deactivated"}


class P2PError(RuntimeError):
    pass


@dataclass
class P2PPeer:
    path: str
    name: str
    hw_address: str
    strength: int = 0
    manufacturer: str = ""
    model: str = ""
    wfd: Optional[WfdDeviceInfo] = None
    last_seen: int = -1

    @property
    def wfd_capable(self) -> bool:
        return self.wfd is not None

    @property
    def is_wfd_sink(self) -> bool:
        return self.wfd is not None and self.wfd.is_sink

    @property
    def label(self) -> str:
        return self.name or self.model or self.hw_address


@dataclass
class P2PLink:
    peer: P2PPeer
    active_path: str
    interface: str
    local_ip: str
    nm: Optional["NMWifiP2P"] = None

    def close(self) -> None:
        if self.nm is not None:
            self.nm.deactivate(self.active_path)
            self.nm = None


# ------------------------------------------------------------------ pure helpers

def _to_bytes(v: Any) -> bytes:
    if v is None:
        return b""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    return bytes(int(x) & 0xFF for x in v)


def peer_from_props(path: str, props: dict[str, Any]) -> P2PPeer:
    """Build a peer from (unpacked) WifiP2PPeer properties."""
    return P2PPeer(
        path=path,
        name=str(props.get("Name") or ""),
        hw_address=str(props.get("HwAddress") or ""),
        strength=int(props.get("Strength") or 0),
        manufacturer=str(props.get("Manufacturer") or ""),
        model=str(props.get("Model") or ""),
        wfd=parse_wfd_ies(_to_bytes(props.get("WfdIEs"))),
        last_seen=int(props.get("LastSeen", -1) if props.get("LastSeen") is not None else -1),
    )


def match_peer(peers: list[P2PPeer], query: str) -> Optional[P2PPeer]:
    """Resolve `--device`: D-Bus path, MAC, exact name, then unique substring."""
    q = query.strip()
    if not q:
        return None
    for p in peers:
        if p.path == q or (q.isdigit() and p.path.rsplit("/", 1)[-1] == q):
            return p
    for p in peers:
        if p.hw_address.lower() == q.lower():
            return p
    exact = [p for p in peers if p.name.lower() == q.lower()]
    if exact:
        return max(exact, key=lambda p: (p.is_wfd_sink, p.strength))
    partial = [p for p in peers if q.lower() in p.name.lower() or q.lower() in p.model.lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        sinks = [p for p in partial if p.is_wfd_sink]
        if len(sinks) == 1:
            return sinks[0]
        raise P2PError("ambíguo / ambiguous: " + ", ".join(p.label for p in partial))
    return None


def connection_settings(peer: P2PPeer, rtsp_port: int = 7236, conn_id: Optional[str] = None) -> dict:
    """Plain-python settings for a volatile wifi-p2p connection (variants added later)."""
    return {
        "connection": {
            "id": conn_id or f"segunda-tela {peer.label}",
            "type": "wifi-p2p",
            "autoconnect": False,
        },
        "wifi-p2p": {
            "peer": peer.hw_address,
            "wfd-ies": build_source_ies(rtsp_port=rtsp_port),
        },
        "ipv4": {"method": "auto", "never-default": True},
        "ipv6": {"method": "auto", "may-fail": True, "never-default": True},
    }


_SETTING_TYPES = {
    ("connection", "id"): "s",
    ("connection", "type"): "s",
    ("connection", "autoconnect"): "b",
    ("wifi-p2p", "peer"): "s",
    ("wifi-p2p", "wfd-ies"): "ay",
    ("ipv4", "method"): "s",
    ("ipv4", "never-default"): "b",
    ("ipv6", "method"): "s",
    ("ipv6", "may-fail"): "b",
    ("ipv6", "never-default"): "b",
}


def format_peer_table(peers: list[P2PPeer]) -> str:
    rows = [("NOME/NAME", "WFD", "TIPO/TYPE", "SINAL", "MAC", "PATH")]
    for p in sorted(peers, key=lambda p: (not p.is_wfd_sink, -p.strength, p.label)):
        rows.append(
            (
                p.label,
                "sim" if p.wfd_capable else "não",
                p.wfd.type_name if p.wfd else "-",
                f"{p.strength}%",
                p.hw_address,
                p.path.rsplit("/", 1)[-1],
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)


# ------------------------------------------------------------------ D-Bus layer

class NMWifiP2P:
    """Thin synchronous NetworkManager client using Gio (python3-gi)."""

    def __init__(self) -> None:
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib
        except Exception as exc:  # noqa: BLE001
            raise P2PError(f"PyGObject (python3-gi) é necessário para Wi-Fi P2P: {exc}") from exc
        self.Gio, self.GLib = Gio, GLib
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except Exception as exc:  # noqa: BLE001
            raise P2PError(f"system D-Bus unavailable: {exc}") from exc

    # --- generic helpers
    def _call(self, path: str, iface: str, method: str, args=None, reply: Optional[str] = None, timeout_ms: int = 10000):
        GLib = self.GLib
        return self.bus.call_sync(
            NM_BUS, path, iface, method, args,
            GLib.VariantType(reply) if reply else None,
            self.Gio.DBusCallFlags.NONE, timeout_ms, None,
        )

    def get_all(self, path: str, iface: str) -> dict[str, Any]:
        res = self._call(path, "org.freedesktop.DBus.Properties", "GetAll",
                         self.GLib.Variant("(s)", (iface,)), "(a{sv})")
        return res.unpack()[0]

    def get(self, path: str, iface: str, name: str) -> Any:
        res = self._call(path, "org.freedesktop.DBus.Properties", "Get",
                         self.GLib.Variant("(ss)", (iface, name)), "(v)")
        return res.unpack()[0]

    # --- devices
    def p2p_device(self) -> tuple[str, str]:
        """(object path, interface name) of the first Wi-Fi P2P device."""
        try:
            devices = self._call(NM_PATH, NM_IFACE, "GetDevices", None, "(ao)").unpack()[0]
        except Exception as exc:  # noqa: BLE001
            raise P2PError(f"NetworkManager not reachable: {exc}") from exc
        for dev in devices:
            props = self.get_all(dev, IFACE_DEVICE)
            if props.get("DeviceType") == NM_DEVICE_TYPE_WIFI_P2P:
                return dev, props.get("Interface", "")
        raise P2PError(
            "nenhum dispositivo Wi-Fi P2P no NetworkManager (esperado p2p-dev-wlo1). "
            "Verifique `nmcli device` e se o wpa_supplicant tem suporte P2P."
        )

    def start_find(self, dev: str, timeout_s: int = 30) -> None:
        opts = {"timeout": self.GLib.Variant("i", int(timeout_s))}
        try:
            self._call(dev, IFACE_P2P_DEVICE, "StartFind", self.GLib.Variant("(a{sv})", (opts,)))
        except Exception as exc:  # noqa: BLE001
            # Already finding is fine.
            log.debug("StartFind: %s", exc)

    def stop_find(self, dev: str) -> None:
        try:
            self._call(dev, IFACE_P2P_DEVICE, "StopFind", None)
        except Exception as exc:  # noqa: BLE001
            log.debug("StopFind: %s", exc)

    def peers(self, dev: str) -> list[P2PPeer]:
        out = []
        for path in self.get(dev, IFACE_P2P_DEVICE, "Peers") or []:
            try:
                out.append(peer_from_props(path, self.get_all(path, IFACE_PEER)))
            except Exception as exc:  # noqa: BLE001
                log.debug("peer %s vanished: %s", path, exc)
        return out

    def discover(
        self,
        seconds: float = 15.0,
        until: Optional[Callable[[list[P2PPeer]], bool]] = None,
        on_update: Optional[Callable[[list[P2PPeer]], None]] = None,
        poll_s: float = 1.0,
    ) -> list[P2PPeer]:
        dev, iface = self.p2p_device()
        log.info("P2P discovery on %s (%s) for %.0fs", iface, dev, seconds)
        self.start_find(dev, timeout_s=max(5, int(seconds) + 5))
        deadline = time.monotonic() + seconds
        found: list[P2PPeer] = []
        try:
            while time.monotonic() < deadline:
                found = self.peers(dev)
                if on_update:
                    on_update(found)
                if until and until(found):
                    break
                time.sleep(poll_s)
        finally:
            self.stop_find(dev)
        return found

    # --- connect
    def _settings_variant(self, settings: dict):
        GLib = self.GLib
        out = {}
        for group, values in settings.items():
            out[group] = {k: GLib.Variant(_SETTING_TYPES[(group, k)], v) for k, v in values.items()}
        return out

    def connect(self, peer: P2PPeer, rtsp_port: int = 7236, timeout_s: float = 60.0) -> P2PLink:
        GLib = self.GLib
        dev, _iface = self.p2p_device()
        self.start_find(dev, timeout_s=int(timeout_s))  # peer must be fresh for GO negotiation
        settings = self._settings_variant(connection_settings(peer, rtsp_port))
        options = {
            "persist": GLib.Variant("s", "volatile"),
            "bind-activation": GLib.Variant("s", "dbus-client"),
        }
        log.info("P2P: connecting to %s (%s)…", peer.label, peer.hw_address)
        try:
            res = self._call(
                NM_PATH, NM_IFACE, "AddAndActivateConnection2",
                GLib.Variant("(a{sa{sv}}ooa{sv})", (settings, dev, peer.path, options)),
                "(ooa{sv})", timeout_ms=int(timeout_s * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            raise P2PError(f"AddAndActivateConnection2 falhou: {exc}") from exc
        _conn_path, active = res.unpack()[:2]
        return self._wait_active(peer, active, timeout_s)

    def _wait_active(self, peer: P2PPeer, active: str, timeout_s: float) -> P2PLink:
        deadline = time.monotonic() + timeout_s
        state = 0
        while time.monotonic() < deadline:
            try:
                state = int(self.get(active, IFACE_ACTIVE, "State"))
            except Exception as exc:  # noqa: BLE001
                raise P2PError(f"conexão P2P sumiu durante ativação: {exc}") from exc
            if state == 2:
                break
            if state == 4:
                raise P2PError("P2P desativada pelo NetworkManager (o tablet recusou ou timeout de GO negotiation)")
            time.sleep(0.5)
        else:
            self.deactivate(active)
            raise P2PError(f"timeout P2P ({timeout_s:.0f}s), estado={AC_STATE.get(state, state)}")

        iface, ip = "", ""
        for _ in range(40):  # DHCP on the group can lag ACTIVATED slightly
            iface, ip = self._link_info(active)
            if ip:
                break
            time.sleep(0.5)
        if not ip:
            raise P2PError(f"P2P ativa em {iface or '?'} mas sem IPv4")
        log.info("P2P up: iface=%s ip=%s peer=%s", iface, ip, peer.label)
        return P2PLink(peer=peer, active_path=active, interface=iface, local_ip=ip, nm=self)

    def _link_info(self, active: str) -> tuple[str, str]:
        props = self.get_all(active, IFACE_ACTIVE)
        iface = ""
        for dev in props.get("Devices") or []:
            iface = self.get(dev, IFACE_DEVICE, "IpInterface") or iface
        ip4 = props.get("Ip4Config")
        ip = ""
        if ip4 and ip4 != "/":
            data = self.get(ip4, IFACE_IP4, "AddressData") or []
            if data:
                ip = data[0].get("address", "")
        return iface, ip

    def deactivate(self, active: str) -> None:
        try:
            self._call(NM_PATH, NM_IFACE, "DeactivateConnection", self.GLib.Variant("(o)", (active,)))
            log.info("P2P desconectado")
        except Exception as exc:  # noqa: BLE001
            log.debug("DeactivateConnection: %s", exc)


def find_and_connect(
    query: str,
    discover_s: float = 20.0,
    rtsp_port: int = 7236,
    connect_timeout_s: float = 60.0,
    nm: Optional[NMWifiP2P] = None,
) -> P2PLink:
    nm = nm or NMWifiP2P()

    def found(peers: list[P2PPeer]) -> bool:
        try:
            return match_peer(peers, query) is not None
        except P2PError:
            return False

    peers = nm.discover(discover_s, until=found)
    peer = match_peer(peers, query)
    if peer is None:
        listing = format_peer_table(peers) if peers else "(nenhum peer)"
        raise P2PError(f"dispositivo '{query}' não encontrado.\n{listing}")
    if not peer.is_wfd_sink:
        log.warning("%s não anunciou WFD IEs de sink; tentando mesmo assim", peer.label)
    return nm.connect(peer, rtsp_port=rtsp_port, timeout_s=connect_timeout_s)
