"""Thin `adb` wrapper + pure parsers used by USB initiation and `doctor`.

Everything that talks to the tablet goes through `Adb.run()`, which can be
replaced by a fake in tests. Parsers take plain text so they are unit-tested
against captured command output.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

log = logging.getLogger(__name__)

# Standard WFD RTSP port (spec) and the R2/alt port some sinks use.
WFD_PORTS = (7236, 7250)

# Package/activity/tile names that look like a *receiver* (sink) on the tablet.
SINK_HINT = re.compile(r"second.?screen|wfd.?sink|sink|receiv|wireless.?display|wifi.?display|miracast", re.I)
# Samsung Smart View is primarily a *source* (casts from the tablet); listed but not auto-launched.
SOURCE_HINT = re.compile(r"smartmirroring|smart.?view|screen.?mirror|cast", re.I)

Runner = Callable[[Sequence[str], float], "tuple[int, str]"]


def _default_runner(argv: Sequence[str], timeout: float) -> tuple[int, str]:
    try:
        p = subprocess.run(list(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=timeout, check=False)
        return p.returncode, p.stdout or ""
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout:.0f}s"
    except OSError as exc:
        return 127, str(exc)


# ------------------------------------------------------------------ parsers

def parse_adb_devices(text: str) -> list[tuple[str, str]]:
    """`adb devices` → [(serial, state)] where state is device|unauthorized|offline|…"""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            out.append((parts[0], parts[1]))
    return out


@dataclass(frozen=True)
class Listener:
    addr: str  # "0.0.0.0", "127.0.0.1", "::", "192.168.49.1", …
    port: int
    uid: int

    @property
    def wildcard(self) -> bool:
        return self.addr in ("0.0.0.0", "::")

    @property
    def loopback_only(self) -> bool:
        return self.addr.startswith("127.") or self.addr == "::1"


def _hex_ipv4(h: str) -> str:
    b = bytes.fromhex(h)
    return ".".join(str(x) for x in reversed(b))


def _hex_ipv6(h: str) -> str:
    if int(h, 16) == 0:
        return "::"
    words = [h[i:i + 8] for i in range(0, 32, 8)]
    raw = b"".join(bytes.fromhex(w)[::-1] for w in words)
    if raw[:12] == b"\0" * 10 + b"\xff\xff":  # v4-mapped
        return ".".join(str(x) for x in raw[12:])
    if raw == b"\0" * 15 + b"\x01":
        return "::1"
    return ":".join(raw[i:i + 2].hex() for i in range(0, 16, 2))


def parse_proc_net_tcp(text: str) -> list[Listener]:
    """LISTEN sockets from /proc/net/tcp and /proc/net/tcp6 contents (concatenated ok)."""
    out: list[Listener] = []
    seen = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 8 or ":" not in parts[1] or parts[0] == "sl":
            continue
        if parts[3].upper() != "0A":  # TCP_LISTEN
            continue
        addr_hex, port_hex = parts[1].rsplit(":", 1)
        try:
            addr = _hex_ipv4(addr_hex) if len(addr_hex) == 8 else _hex_ipv6(addr_hex)
            lis = Listener(addr, int(port_hex, 16), int(parts[7]))
        except ValueError:
            continue
        if (lis.addr, lis.port) not in seen:
            seen.add((lis.addr, lis.port))
            out.append(lis)
    return out


def parse_packages_uid(text: str) -> dict[str, int]:
    """`pm list packages -U` → {package: uid}."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        m = re.match(r"\s*package:(\S+)\s+uid:(\d+)", line)
        if m:
            out[m.group(1)] = int(m.group(2))
        else:
            m = re.match(r"\s*package:(\S+)\s*$", line)
            if m:
                out.setdefault(m.group(1), -1)
    return out


def sink_packages(packages: dict[str, int]) -> tuple[list[str], list[str]]:
    """Split installed packages into (likely sinks, mirroring/source apps)."""
    sinks = sorted(p for p in packages if SINK_HINT.search(p))
    sources = sorted(p for p in packages if p not in sinks and SOURCE_HINT.search(p))
    return sinks, sources


def parse_dumpsys_activities(text: str, package: str) -> list[str]:
    """Activity components of `package` named like a receiver/second-screen UI."""
    comps = re.findall(rf"({re.escape(package)}/[\w.$]+)", text)
    seen, out = set(), []
    for c in comps:
        if c not in seen and SINK_HINT.search(c.split("/", 1)[1]):
            seen.add(c)
            out.append(c)
    return out


def parse_qs_tiles(value: str) -> list[str]:
    """`settings get secure sysui_qs_tiles` → custom tile components that look like a sink."""
    out = []
    for spec in value.strip().split(","):
        m = re.fullmatch(r"custom\((.+)\)", spec.strip())
        if m and SINK_HINT.search(m.group(1)):
            out.append(m.group(1))
    return out


def parse_ip_addrs(text: str) -> list[tuple[str, str, int]]:
    """`ip -4 -o addr` → [(iface, ip, prefixlen)]."""
    out = []
    for line in text.splitlines():
        m = re.search(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line.strip())
        if m:
            out.append((m.group(1), m.group(2), int(m.group(3))))
    return out


def same_subnet(a: str, b: str, prefix: int = 24) -> bool:
    def n(ip: str) -> int:
        x = 0
        for o in ip.split("."):
            x = (x << 8) | int(o)
        return x

    try:
        mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
        return (n(a) & mask) == (n(b) & mask)
    except ValueError:
        return False


# ------------------------------------------------------------------ wrapper

class Adb:
    def __init__(self, serial: Optional[str] = None, runner: Optional[Runner] = None, exe: str = "adb") -> None:
        self.serial = serial
        self.exe = exe
        self._runner = runner
        self.log: List[str] = []  # every command + short result, for verbose reporting

    def available(self) -> bool:
        return self._runner is not None or shutil.which(self.exe) is not None

    def run(self, *args: str, timeout: float = 10.0, device: bool = True) -> tuple[int, str]:
        argv = [self.exe]
        if device and self.serial:
            argv += ["-s", self.serial]
        argv += list(args)
        rc, out = (self._runner or _default_runner)(argv, timeout)
        short = out.strip().splitlines()[0][:120] if out.strip() else ""
        self.log.append(f"$ {' '.join(argv[1:])} → rc={rc}{(' ' + short) if short else ''}")
        log.debug("adb %s → rc=%s %s", " ".join(argv[1:]), rc, out.strip()[:500])
        return rc, out

    def shell(self, cmd: str, timeout: float = 10.0) -> tuple[int, str]:
        return self.run("shell", cmd, timeout=timeout)

    def devices(self) -> list[tuple[str, str]]:
        if not self.available():
            return []
        rc, out = self.run("devices", timeout=5, device=False)
        return parse_adb_devices(out) if rc == 0 else []

    def select(self, prefer_serials: Sequence[str] = ()) -> tuple[Optional[str], str]:
        """Pick an authorized device (matching a Samsung USB serial if possible).

        Returns (serial|None, state summary like "device", "unauthorized", "none").
        """
        devs = self.devices()
        if not devs:
            return None, "none"
        ready = [s for s, st in devs if st == "device"]
        for s in prefer_serials:
            if s in ready:
                self.serial = s
                return s, "device"
        if ready:
            self.serial = ready[0]
            return ready[0], "device"
        return None, devs[0][1]

    # -- higher-level helpers
    def packages(self) -> dict[str, int]:
        rc, out = self.shell("pm list packages -U")
        if rc != 0 or "uid:" not in out:
            rc, out = self.shell("pm list packages")
        return parse_packages_uid(out) if rc == 0 else {}

    def listeners(self) -> list[Listener]:
        rc, out = self.shell("cat /proc/net/tcp /proc/net/tcp6 2>/dev/null")
        return parse_proc_net_tcp(out) if out else []

    def ipv4_addrs(self) -> list[tuple[str, str, int]]:
        rc, out = self.shell("ip -4 -o addr show")
        return parse_ip_addrs(out) if rc == 0 else []

    def reverse(self, port: int) -> bool:
        rc, _ = self.run("reverse", f"tcp:{port}", f"tcp:{port}")
        return rc == 0

    def forward(self, remote_port: int) -> Optional[int]:
        """`adb forward tcp:0 tcp:<remote>` → allocated local port."""
        rc, out = self.run("forward", "tcp:0", f"tcp:{remote_port}")
        if rc != 0:
            return None
        m = re.search(r"(\d+)", out)
        return int(m.group(1)) if m else None

    def remove_forward(self, local_port: int) -> None:
        self.run("forward", "--remove", f"tcp:{local_port}")

    def remove_reverse(self, port: int) -> None:
        self.run("reverse", "--remove", f"tcp:{port}")
