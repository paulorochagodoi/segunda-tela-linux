"""USB mode: make the PC *initiate* the WFD session instead of only listening.

Over a tether nothing on a Galaxy Tab dials a Linux PC by itself, so after the
IP link is up we:

1. (adb, if authorized) `adb reverse tcp:7236` so a sink on the tablet can reach
   us at 127.0.0.1:7236; look for receiver/"second screen" packages, activities
   and quick-settings tiles and try to open them; read the tablet's LISTEN
   sockets (/proc/net/tcp*) to find a WFD sink RTSP port.
2. Dial each candidate sink port — over the tether IP, or through
   `adb forward` — and run the source M1–M7 flow on that socket
   (`WfdRtspServer.connect_to`). RTP still goes to the tether peer IP.
3. If nothing accepts, print exactly what to do on the tablet (or that the
   Samsung cable path needs the proprietary Connlib and Wi-Fi Direct should be
   used instead).

Every attempt and its outcome is reported through `say()`.
"""
from __future__ import annotations

import errno
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .adb import WFD_PORTS, Adb, Listener, parse_dumpsys_activities, parse_qs_tiles, same_subnet, sink_packages
from .link import UsbLink

log = logging.getLogger(__name__)

# How long a dialled session must survive to count as "the sink took it".
SESSION_SETTLE_S = 6.0


def dial_tcp(host: str, port: int, timeout: float = 1.5) -> tuple[Optional[socket.socket], str]:
    """Connect once. → (socket, "open") or (None, "refused" | "timeout" | "unreachable" | "error: …").

    "refused" means the host answered (reachable) but nothing listens on the port.
    """
    try:
        return socket.create_connection((host, port), timeout=timeout), "open"
    except ConnectionRefusedError:
        return None, "refused"
    except socket.timeout:
        return None, "timeout"
    except OSError as exc:
        if exc.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH):
            return None, "unreachable"
        return None, f"error: {exc}"


def probe_tcp(host: str, port: int, timeout: float = 1.5) -> str:
    sock, state = dial_tcp(host, port, timeout)
    if sock is not None:
        sock.close()
    return state


@dataclass
class Target:
    host: str
    port: int
    via: str  # "tether" | "adb-forward"
    media_host: str = ""

    def __str__(self) -> str:
        return f"{self.host}:{self.port} ({self.via})"


@dataclass
class InitiateResult:
    connected: Optional[Target] = None
    adb_state: str = "absent"  # absent | none | unauthorized | device | offline
    listeners: list[Listener] = field(default_factory=list)
    sink_pkgs: list[str] = field(default_factory=list)
    source_pkgs: list[str] = field(default_factory=list)
    launched: list[str] = field(default_factory=list)
    attempts: list[str] = field(default_factory=list)


def pick_sink_ports(listeners: list[Listener], sink_uids: set[int]) -> list[Listener]:
    """Tablet LISTEN sockets worth dialling: WFD ports first, then ports owned by sink apps."""
    wfd = [l for l in listeners if l.port in WFD_PORTS]
    owned = [l for l in listeners if l.uid in sink_uids and l.port not in WFD_PORTS and l.port >= 1024]
    wfd.sort(key=lambda l: WFD_PORTS.index(l.port))
    return wfd + sorted(owned, key=lambda l: l.port)


def build_targets(peer_ip: str, listeners: list[Listener], adb_ok: bool) -> list[Target]:
    """Order of dial attempts for the given tablet sockets (pure)."""
    targets: list[Target] = []
    ports = [l.port for l in listeners] or list(WFD_PORTS)
    by_port = {l.port: l for l in listeners}
    for port in ports:
        lis = by_port.get(port)
        if peer_ip and (lis is None or lis.wildcard or lis.addr == peer_ip):
            targets.append(Target(peer_ip, port, "tether"))
        if adb_ok and lis is not None and (lis.wildcard or lis.loopback_only):
            # host/port filled in after `adb forward` allocates a local port
            targets.append(Target("127.0.0.1", port, "adb-forward", media_host=peer_ip))
    return targets


def guidance(link: UsbLink, res: InitiateResult, rtsp_port: int) -> str:
    """Actionable text for when no sink could be reached over the cable."""
    lines = ["Nenhum receptor WFD/Miracast no tablet aceitou conexão pelo cabo USB."]
    if res.adb_state == "absent":
        lines.append("• adb não está instalado (sudo apt install adb) — sem ele não dá para abrir o "
                     "receptor no tablet nem ver as portas dele.")
    elif res.adb_state in ("none", "offline"):
        lines.append("• adb não vê o tablet: ative Opções do desenvolvedor → Depuração USB e reconecte o cabo.")
    elif res.adb_state == "unauthorized":
        lines.append("• adb: tablet NÃO autorizado — desbloqueie a tela e aceite “Permitir depuração USB”.")
    if res.adb_state == "device":
        if res.sink_pkgs:
            lines.append("• Apps receptores encontrados: " + ", ".join(res.sink_pkgs)
                         + (" (abertos: " + ", ".join(res.launched) + ")" if res.launched else ""))
        else:
            lines.append("• Nenhum app receptor (sink) Miracast/WFD instalado no tablet.")
        if res.source_pkgs:
            lines.append("  (" + ", ".join(res.source_pkgs) + " é Smart View: transmite DO tablet, não recebe.)")
        if res.listeners:
            lines.append("• Portas TCP abertas por apps receptores: "
                         + ", ".join(f"{l.addr}:{l.port}" for l in res.listeners) + " — nenhuma falou WFD.")
    lines += [
        "",
        "O que fazer:",
        "1. A “Segunda tela” da Samsung por cabo usa o protocolo proprietário Connlib (só o app do "
        "Windows). Pelo USB isso NÃO é possível com este projeto.",
        "2. Use Wi-Fi Direct (mesmo receptor, protocolo aberto): no tablet abra o painel rápido → "
        "“Segunda tela” e deixe na tela de espera; no PC:",
        "     segunda-tela list",
        "     segunda-tela --mode extend --device \"<nome do tablet>\"",
        f"3. Se você tiver um app receptor Miracast/RTSP aberto no tablet, aponte-o para "
        f"rtsp://{link.local_ip or '?'}:{rtsp_port}"
        + (f" (ou rtsp://127.0.0.1:{rtsp_port} — adb reverse ativo)" if res.adb_state == "device" else "")
        + " — o PC continua escutando.",
    ]
    return "\n".join(lines)


class UsbInitiator:
    """Runs in a background thread after the RTSP source is listening."""

    def __init__(
        self,
        server,
        link: UsbLink,
        rtsp_port: int = 7236,
        adb: Optional[Adb] = None,
        launch: bool = True,
        sink_activity: Optional[str] = None,
        timeout_s: float = 60.0,
        retry_s: float = 4.0,
        say: Callable[[str], None] = print,
        dial: Callable[[str, int, float], "tuple[Optional[socket.socket], str]"] = dial_tcp,
        settle_s: float = SESSION_SETTLE_S,
    ) -> None:
        self.server = server
        self.link = link
        self.rtsp_port = rtsp_port
        self.adb = adb if adb is not None else Adb()
        self.launch = launch
        self.sink_activity = sink_activity
        self.timeout_s = timeout_s
        self.retry_s = retry_s
        self.say = say
        self.dial = dial
        self.settle_s = settle_s
        self.result = InitiateResult()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._forwards: dict[int, int] = {}  # tablet port → local port
        self._last_probe: dict[str, str] = {}
        self._reversed = False
        self._sink_uids: set[int] = set()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="usb-initiate", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        for lp in self._forwards.values():
            self.adb.remove_forward(lp)
        self._forwards.clear()
        if self._reversed and not self.link._adb_reversed:
            self.adb.remove_reverse(self.rtsp_port)
            self._reversed = False

    def _note(self, msg: str) -> None:
        self.result.attempts.append(msg)
        log.info("USB initiate: %s", msg)
        self.say(f"USB: {msg}")

    # ------------------------------------------------------------ steps
    def setup_adb(self) -> None:
        res = self.result
        if not self.adb.available():
            res.adb_state = "absent"
            self._note("adb não encontrado — pulando abertura do receptor/detecção de portas")
            return
        serials = [d.serial for d in self.link.devices if d.serial]
        serial, state = self.adb.select(serials)
        res.adb_state = state
        if serial is None:
            self._note(f"adb: nenhum dispositivo autorizado (estado: {state})")
            return
        self._note(f"adb: dispositivo {serial} autorizado")

        if not self.link._adb_reversed:
            ok = self.adb.reverse(self.rtsp_port)
            self._reversed = ok
            self._note(f"adb reverse tcp:{self.rtsp_port} → {'ok' if ok else 'falhou'}")

        # Real tether peer IP (instead of the .1 guess) from the tablet's own addresses.
        if self.link.local_ip and self.link.transport != "adb-reverse":
            for iface, ip, prefix in self.adb.ipv4_addrs():
                if ip != self.link.local_ip and same_subnet(ip, self.link.local_ip, min(prefix, 30)):
                    if ip != self.link.peer_ip:
                        self._note(f"IP do tablet no tether: {ip} ({iface}); estimativa era {self.link.peer_ip or '?'}")
                        self.link.peer_ip = ip
                    break

        pkgs = self.adb.packages()
        res.sink_pkgs, res.source_pkgs = sink_packages(pkgs)
        self._sink_uids = {pkgs[p] for p in res.sink_pkgs if pkgs.get(p, -1) >= 0}
        self._note("apps receptores no tablet: " + (", ".join(res.sink_pkgs) or "nenhum")
                   + (f"; Smart View/espelhamento (source): {', '.join(res.source_pkgs)}" if res.source_pkgs else ""))

    def launch_sink(self) -> None:
        if self.result.adb_state != "device" or not self.launch:
            return
        comps: list[str] = []
        if self.sink_activity:
            comps.append(self.sink_activity)
        for pkg in self.result.sink_pkgs:
            rc, out = self.adb.shell(f"dumpsys package {pkg}", timeout=15)
            comps += [c for c in parse_dumpsys_activities(out, pkg) if c not in comps]
        for comp in comps:
            rc, out = self.adb.shell(f"am start -n {comp}")
            ok = rc == 0 and "Error" not in out and "Exception" not in out
            self._note(f"am start -n {comp} → {'ok' if ok else out.strip().splitlines()[-1] if out.strip() else rc}")
            if ok:
                self.result.launched.append(comp)
                return
        for pkg in self.result.sink_pkgs:
            rc, out = self.adb.shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
            ok = rc == 0 and "No activities found" not in out and "aborted" not in out.lower()
            self._note(f"abrir launcher de {pkg} → {'ok' if ok else 'sem atividade de launcher'}")
            if ok:
                self.result.launched.append(pkg)
                return
        rc, tiles = self.adb.shell("settings get secure sysui_qs_tiles")
        for tile in parse_qs_tiles(tiles if rc == 0 else ""):
            rc, out = self.adb.shell(f"cmd statusbar click-tile {tile}")
            self._note(f"tocar bloco do painel rápido {tile} → {'ok' if rc == 0 else out.strip()[:80]}")
            if rc == 0:
                self.result.launched.append(tile)
                return
        if not comps and not self.result.sink_pkgs:
            self._note("nada para abrir no tablet (nenhum receptor encontrado)")

    def discover_listeners(self) -> list[Listener]:
        if self.result.adb_state != "device":
            return []
        found = pick_sink_ports(self.adb.listeners(), self._sink_uids)
        if found != self.result.listeners:
            self.result.listeners = found
            self._note("portas de sink no tablet: "
                       + (", ".join(f"{l.addr}:{l.port} (uid {l.uid})" for l in found) or "nenhuma escutando"))
        return found

    def try_target(self, t: Target) -> bool:
        if t.via == "adb-forward":
            lp = self._forwards.get(t.port) or self.adb.forward(t.port)
            if lp is None:
                self._note(f"adb forward → tablet:{t.port} falhou")
                return False
            self._forwards[t.port] = lp
            t = Target("127.0.0.1", lp, f"adb-forward→tablet:{t.port}", t.media_host)
            if not t.media_host:
                self._note("AVISO: sem IP de tether, o RTP (UDP) não passa pelo adb; ative Tethering USB")
        # Dial once and hand that very socket to the RTSP source: some sinks accept
        # a single connection, so a separate "probe" connect would burn it.
        sock, state = self.dial(t.host, t.port, 1.5)
        if self._last_probe.get(str(t)) != state:  # log changes only; we retry every few seconds
            self._last_probe[str(t)] = state
            self._note(f"sonda TCP {t} → {state}")
        if sock is None:
            return False
        th = self.server.connect_to(t.host, t.port, media_host=t.media_host, sock=sock)
        deadline = time.monotonic() + self.settle_s
        while th.is_alive() and time.monotonic() < deadline:
            st = getattr(th, "wfd_state", None)
            if st is not None and st.m1_answered:
                break
            th.join(0.1)
        st = getattr(th, "wfd_state", None)
        if st is not None and st.m1_answered:
            self._note(f"sessão WFD iniciada pelo PC em {t}: sink respondeu ao M1 → M3/M4/M5 seguem")
            self.result.connected = t
            return True
        if th.is_alive():
            self._note(f"{t} aceitou TCP mas não respondeu ao M1 em {self.settle_s:.0f}s; mantendo a conexão")
            self.result.connected = t
            return True
        self._note(f"{t} fechou a conexão sem responder ao M1 — não é um sink WFD")
        return False

    # ------------------------------------------------------------ main loop
    def run(self) -> InitiateResult:
        link = self.link
        self._note(f"iniciando conexão ativa ao tablet (peer {link.peer_ip or '?'}, até {self.timeout_s:.0f}s)")
        self.setup_adb()
        self.launch_sink()
        deadline = time.monotonic() + self.timeout_s
        hinted = False
        while not self._stop.is_set() and time.monotonic() < deadline:
            if self.server.active_sessions:
                self._note("o tablet conectou no PC (sessão RTSP de entrada ativa)")
                return self.result
            listeners = self.discover_listeners()
            peer = link.peer_ip if link.transport != "adb-reverse" else ""
            for t in build_targets(peer, listeners, self.result.adb_state == "device"):
                if self._stop.is_set():
                    break
                if self.try_target(t):
                    return self.result
            if not hinted:
                hinted = True
                self.say(guidance(link, self.result, self.rtsp_port))
                self.say(f"USB: continuo tentando por até {self.timeout_s:.0f}s e escutando em :{self.rtsp_port}…")
            self._stop.wait(self.retry_s)
        if not self._stop.is_set() and not self.server.active_sessions:
            self._note("tempo esgotado sem sink pelo cabo; o PC segue escutando (Ctrl+C para sair)")
        return self.result
