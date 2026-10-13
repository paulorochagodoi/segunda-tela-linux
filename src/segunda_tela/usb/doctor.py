"""USB section of `segunda-tela doctor` (adb, authorization, tether, Second Screen app, ports)."""
from __future__ import annotations

import socket
from pathlib import Path
from typing import Callable, Iterator, Optional

from .adb import WFD_PORTS, Adb, same_subnet, sink_packages
from .detect import find_usb_net_ifaces, list_samsung_usb_devices
from .initiate import pick_sink_ports, probe_tcp
from .link import _guess_peer_ip, _iface_ipv4
from .samsung import SOURCE_READY_PORT, config_path, installed_hosts

UDEV_RULE = Path("/etc/udev/rules.d/99-segunda-tela-samsung.rules")

# (status, label, detail) — status is "ok" | "fail" | "info"
Check = tuple[str, str, str]


def local_port_free(port: int, host: str = "0.0.0.0") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def usb_checks(
    rtsp_port: int = 7236,
    adb: Optional[Adb] = None,
    probe: Callable[[str, int, float], str] = probe_tcp,
    sys_bus_usb: str = "/sys/bus/usb/devices",
    ifaces: Optional[list[str]] = None,
    iface_ip: Callable[[str], Optional[str]] = _iface_ipv4,
) -> Iterator[Check]:
    """Failures are only reported as "fail" when a Samsung device is plugged in."""
    devs = list_samsung_usb_devices(sys_bus_usb)
    if devs:
        yield "ok", "Samsung USB device", ", ".join(f"04e8:{d.pid} {d.serial or '?'}" for d in devs)
    else:
        yield "info", "Samsung USB device", "nenhum conectado — checagens USB abaixo são informativas"
    bad = "fail" if devs else "info"
    yield ("ok" if UDEV_RULE.exists() else "info"), "udev rule Samsung", (
        str(UDEV_RULE) if UDEV_RULE.exists() else "sudo ./scripts/install-udev-samsung.sh")

    free = local_port_free(rtsp_port)
    yield ("ok" if free else "fail"), f"porta local TCP {rtsp_port} livre", (
        "" if free else "outro processo usa a porta (segunda-tela já rodando?)")

    # --- tether
    ifaces = find_usb_net_ifaces() if ifaces is None else ifaces
    local_ip = peer_ip = ""
    for iface in ifaces:
        ip = iface_ip(iface)
        if ip:
            local_ip, peer_ip = ip, _guess_peer_ip(ip)
            yield "ok", "tether USB (interface/IP)", f"{iface} local={ip} peer≈{peer_ip}"
            break
    else:
        yield bad, "tether USB (interface/IP)", (
            f"interface {ifaces[0]} sem IPv4" if ifaces else
            "nenhuma usb0/enx*; no tablet: Conexões → Hotspot e tethering → Tethering USB")

    # --- adb
    adb = adb if adb is not None else Adb()
    serial, state = None, "absent"
    if not adb.available():
        yield bad, "adb", "não instalado: sudo apt install adb"
    else:
        yield "ok", "adb", "presente"
        serial, state = adb.select([d.serial for d in devs if d.serial])
        detail = {
            "device": f"{serial} autorizado",
            "unauthorized": "NÃO autorizado — desbloqueie o tablet e aceite 'Permitir depuração USB'",
            "none": "nenhum dispositivo — ative Depuração USB",
            "offline": "offline — reconecte o cabo",
        }.get(state, state)
        yield ("ok" if state == "device" else bad), "adb dispositivo", detail

    listeners = []
    mice_listener = None
    if state == "device":
        if local_ip:
            for iface, ip, prefix in adb.ipv4_addrs():
                if ip != local_ip and same_subnet(ip, local_ip, min(prefix, 30)):
                    peer_ip = ip
                    yield "ok", "IP do tablet no tether (via adb)", f"{iface} {ip}"
                    break
        pkgs = adb.packages()
        sinks, sources = sink_packages(pkgs)
        hosts = installed_hosts(pkgs)
        yield ("ok" if hosts else bad), "Segunda tela Samsung (pacote)", (
            ", ".join(hosts) or "com.samsung.android.secondscreen não encontrado")
        for h in hosts:
            cfg = adb.read_file(config_path(h))
            yield "info", f"config USB {h}", (cfg.strip() if cfg else "ausente (o PC grava ao conectar)")
        others = [p for p in sinks if p not in hosts]
        if others:
            yield "info", "outros apps receptores", ", ".join(others)
        uids = {pkgs[p] for p in sinks + hosts if pkgs.get(p, -1) >= 0}
        all_listeners = adb.listeners()
        mice_listener = next((l for l in all_listeners if l.port == SOURCE_READY_PORT), None)
        yield ("ok" if mice_listener else "info"), f"tablet escutando source-ready :{SOURCE_READY_PORT}", (
            f"{mice_listener.addr}:{mice_listener.port}" if mice_listener else
            "não — abra a Segunda tela no tablet (ou /proc/net/tcp oculto)")
        listeners = pick_sink_ports(all_listeners, uids)
        yield ("ok" if listeners else "info"), "portas RTSP de sink escutando no tablet", (
            ", ".join(f"{l.addr}:{l.port}" for l in listeners) or
            f"nenhuma em {'/'.join(map(str, WFD_PORTS))} (normal: a Segunda tela conecta de volta no PC)")

    # --- peer reachability + sink ports over the tether
    if peer_ip:
        ports = [SOURCE_READY_PORT] + ([l.port for l in listeners] or list(WFD_PORTS))
        results = {p: probe(peer_ip, p, 1.5) for p in ports}
        reachable = any(r in ("open", "refused") for r in results.values())
        yield ("ok" if reachable else bad), f"tablet {peer_ip} alcançável", (
            "responde TCP" if reachable else "sem resposta (firewall/IP errado?)")
        for p, r in results.items():
            what = "source-ready" if p == SOURCE_READY_PORT else "sink RTSP"
            yield ("ok" if r == "open" else "info"), f"{what} {peer_ip}:{p}", r
    if devs and mice_listener is None and not any(l.port in WFD_PORTS for l in listeners):
        yield "info", "dica", ("abra a 'Segunda tela' no painel rápido do tablet e rode "
                               "segunda-tela -v --usb; se não atender pelo cabo use Wi-Fi Direct: "
                               "segunda-tela list; segunda-tela --device \"<nome>\"")
