"""Compositor output layout via cosmic-randr (fallback wlr-randr).

Used to verify that COSMIC really exposes the EVDI connector as an
*extended* display: present, enabled, not mirroring and not overlapping
another output. The EVDI output's position also tells the portal fallback
which ScreenCast stream is the virtual monitor.

Parsers are pure and unit-tested; tool output formats differ between
versions, so they are deliberately tolerant.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

from .edid import DEFAULT_NAME, DEFAULT_VENDOR
from .evdi import evdi_cards

log = logging.getLogger(__name__)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
MODE_RE = re.compile(r"(\d+)\s*x\s*(\d+)(?:\s*px)?[\s,@]*([\d.]+)?")

# (argv, parser name) in preference order.
COMMANDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("cosmic-randr", "list", "--kdl"), "kdl"),
    (("cosmic-randr", "list"), "text"),
    (("wlr-randr", "--json"), "json"),
    (("wlr-randr",), "text"),
)

Runner = Callable[[list[str], float], tuple[int, str]]
# (status, label, detail) — same shape as usb.doctor.Check
Check = tuple[str, str, str]


@dataclass
class Output:
    name: str
    enabled: bool = True
    make: str = ""
    model: str = ""
    x: Optional[int] = None
    y: Optional[int] = None
    width: Optional[int] = None  # current mode, physical pixels
    height: Optional[int] = None
    refresh: Optional[float] = None
    scale: float = 1.0
    transform: str = "normal"
    mirroring: Optional[str] = None

    @property
    def position(self) -> Optional[tuple[int, int]]:
        return None if self.x is None or self.y is None else (self.x, self.y)

    @property
    def logical_size(self) -> Optional[tuple[int, int]]:
        if self.width is None or self.height is None:
            return None
        w, h = self.width, self.height
        if any(t in self.transform for t in ("90", "270")):
            w, h = h, w
        s = self.scale or 1.0
        return round(w / s), round(h / s)

    def describe(self) -> str:
        mode = f"{self.width}x{self.height}@{self.refresh:g}" if self.width and self.refresh else (
            f"{self.width}x{self.height}" if self.width else "sem modo")
        parts = [
            self.name,
            "ligada" if self.enabled else "DESLIGADA",
            mode,
            f"pos={self.x},{self.y}" if self.position else "pos=?",
            f"escala={self.scale:g}",
        ]
        if self.transform not in ("", "normal"):
            parts.append(f"rot={self.transform}")
        if self.mirroring:
            parts.append(f"espelhando={self.mirroring}")
        if self.make or self.model:
            parts.append(f"({' '.join(p for p in (self.make, self.model) if p)})")
        return " ".join(parts)


@dataclass
class Layout:
    tool: str  # "cosmic-randr", "wlr-randr" or "" (none worked)
    outputs: list[Output]
    raw: str = ""
    error: str = ""


@dataclass
class EvdiLayoutStatus:
    status: str  # "extended" | "mirrored" | "disabled" | "absent" | "unknown"
    output: Optional[Output]
    message: str

    @property
    def ok(self) -> bool:
        return self.status == "extended"


# ------------------------------------------------------------------ parsers

def _bool(v: str) -> bool:
    return v.strip().strip('"').lstrip("#").lower() in ("true", "yes", "1", "enabled", "on")


def _scale(v: str) -> float:
    v = v.strip().strip('"')
    try:
        return float(v[:-1]) / 100 if v.endswith("%") else float(v)
    except ValueError:
        return 1.0


def _ints(v: str) -> list[int]:
    return [int(n) for n in re.findall(r"-?\d+", v)]


def parse_kdl(text: str) -> list[Output]:
    """`cosmic-randr list --kdl`: `output "eDP-1" enabled=#true { ... }` blocks."""
    outs: list[Output] = []
    cur: Optional[Output] = None
    for raw in ANSI_RE.sub("", text).splitlines():
        line = raw.strip()
        m = re.match(r'output\s+"([^"]+)"(.*)', line)
        if m:
            cur = Output(name=m.group(1))
            en = re.search(r"enabled=(#?\w+)", m.group(2))
            if en:
                cur.enabled = _bool(en.group(1))
            outs.append(cur)
            continue
        if cur is None or not line:
            continue
        key, _, rest = line.partition(" ")
        attrs = dict(re.findall(r'(\w+)=("[^"]*"|\S+)', rest))
        if key == "description":
            cur.make = attrs.get("make", "").strip('"')
            cur.model = attrs.get("model", "").strip('"')
        elif key in ("make", "model"):
            setattr(cur, key, rest.strip().strip('"'))
        elif key == "enabled":
            cur.enabled = _bool(rest)
        elif key == "position":
            nums = _ints(rest)
            if len(nums) >= 2:
                cur.x, cur.y = nums[0], nums[1]
        elif key == "scale":
            cur.scale = _scale(rest)
        elif key == "transform":
            cur.transform = rest.strip().strip('"')
        elif key in ("mirroring", "mirror"):
            cur.mirroring = rest.strip().strip('"') or None
        elif key == "mode" and re.search(r"current=#?true", rest):
            nums = _ints(rest)
            if len(nums) >= 2:
                cur.width, cur.height = nums[0], nums[1]
                if len(nums) >= 3:
                    # cosmic-randr reports mHz.
                    cur.refresh = nums[2] / 1000 if nums[2] > 1000 else float(nums[2])
    return outs


def parse_text(text: str) -> list[Output]:
    """Human output of `cosmic-randr list` or `wlr-randr` (same overall shape)."""
    outs: list[Output] = []
    cur: Optional[Output] = None
    for raw in ANSI_RE.sub("", text).splitlines():
        if not raw.strip():
            continue
        if not raw[0].isspace():
            name = raw.split()[0].strip('"')
            cur = Output(name=name)
            low = raw.lower()
            if "(disabled)" in low:
                cur.enabled = False
            outs.append(cur)
            continue
        if cur is None:
            continue
        line = raw.strip()
        if ":" in line and not MODE_RE.match(line):
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if key == "make":
                cur.make = val
            elif key == "model":
                cur.model = val
            elif key == "enabled":
                cur.enabled = _bool(val)
            elif key == "position":
                nums = _ints(val)
                if len(nums) >= 2:
                    cur.x, cur.y = nums[0], nums[1]
            elif key == "scale":
                cur.scale = _scale(val)
            elif key == "transform":
                cur.transform = val
            elif key in ("mirroring", "mirror", "mirrors"):
                cur.mirroring = val or None
            continue
        if "current" in line.lower():
            m = MODE_RE.match(line)
            if m:
                cur.width, cur.height = int(m.group(1)), int(m.group(2))
                if m.group(3):
                    cur.refresh = float(m.group(3))
    return outs


def parse_json(text: str) -> list[Output]:
    """`wlr-randr --json`."""
    outs: list[Output] = []
    for item in json.loads(text):
        o = Output(
            name=item.get("name", "?"),
            enabled=bool(item.get("enabled", True)),
            make=item.get("make") or "",
            model=item.get("model") or "",
            scale=float(item.get("scale") or 1.0),
            transform=str(item.get("transform") or "normal"),
        )
        pos = item.get("position") or {}
        if "x" in pos and "y" in pos:
            o.x, o.y = int(pos["x"]), int(pos["y"])
        for mode in item.get("modes") or []:
            if mode.get("current"):
                o.width, o.height = int(mode["width"]), int(mode["height"])
                o.refresh = float(mode.get("refresh") or 0) or None
        outs.append(o)
    return outs


PARSERS = {"kdl": parse_kdl, "text": parse_text, "json": parse_json}


# ------------------------------------------------------------------ query

def _run(argv: list[str], timeout: float) -> tuple[int, str]:
    if not shutil.which(argv[0]):
        return 127, ""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return p.returncode, p.stdout if p.returncode == 0 else (p.stderr or p.stdout)


def query_layout(runner: Runner = _run, timeout: float = 5.0) -> Layout:
    errors = []
    for argv, kind in COMMANDS:
        rc, out = runner(list(argv), timeout)
        if rc == 127:
            continue
        if rc != 0:
            errors.append(f"{' '.join(argv)}: rc={rc} {out.strip()[:200]}")
            continue
        try:
            outputs = PARSERS[kind](out)
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"{' '.join(argv)}: {exc}")
            continue
        if outputs:
            return Layout(tool=argv[0], outputs=outputs, raw=out)
        errors.append(f"{' '.join(argv)}: nenhuma saída reconhecida")
    if not errors:
        errors.append("cosmic-randr/wlr-randr não instalados (sudo apt install cosmic-randr)")
    return Layout(tool="", outputs=[], error="; ".join(errors))


def evdi_connector_names(sys_root: Path = Path("/sys")) -> set[str]:
    """DRM connector names (e.g. DVI-I-1) that belong to evdi cards."""
    names: set[str] = set()
    drm = sys_root / "class" / "drm"
    for card in evdi_cards(sys_root):
        for conn in drm.glob(f"card{card}-*"):
            names.add(conn.name.split("-", 1)[1])
    return names


def find_evdi_output(outputs: list[Output], connectors: set[str]) -> Optional[Output]:
    for o in outputs:
        if o.name in connectors:
            return o
    for o in outputs:
        if DEFAULT_NAME.lower() in f"{o.make} {o.model}".lower() or o.make == DEFAULT_VENDOR:
            return o
    return None


def _overlaps(a: Output, b: Output) -> bool:
    if a.position is None or b.position is None or not a.logical_size or not b.logical_size:
        return False
    (ax, ay), (aw, ah) = a.position, a.logical_size
    (bx, by), (bw, bh) = b.position, b.logical_size
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def evdi_status(layout: Layout, connectors: set[str]) -> EvdiLayoutStatus:
    if not layout.tool:
        return EvdiLayoutStatus("unknown", None, f"layout do compositor indisponível: {layout.error}")
    out = find_evdi_output(layout.outputs, connectors)
    if out is None:
        return EvdiLayoutStatus(
            "absent", None,
            "a saída EVDI 'Segunda Tela' NÃO aparece no layout do compositor"
            + (f" (conectores evdi: {', '.join(sorted(connectors))})" if connectors else ""),
        )
    if not out.enabled:
        return EvdiLayoutStatus(
            "disabled", out,
            f"{out.name} existe mas está DESLIGADA — ative em Configurações → Telas",
        )
    others = [o for o in layout.outputs if o is not out and o.enabled]
    mirrored_by = [o.name for o in others if o.mirroring == out.name]
    if out.mirroring or mirrored_by:
        src = out.mirroring or ", ".join(mirrored_by)
        return EvdiLayoutStatus(
            "mirrored", out,
            f"{out.name} está ESPELHANDO {src}, não estendendo — em Configurações → Telas "
            f"escolha 'Estender'/'Juntar telas'",
        )
    overlap = [o.name for o in others if _overlaps(out, o)]
    if overlap:
        return EvdiLayoutStatus(
            "mirrored", out,
            f"{out.name} em {out.x},{out.y} sobrepõe {', '.join(overlap)} (efetivamente espelhado) — "
            f"arraste 'Segunda Tela' para o lado em Configurações → Telas",
        )
    return EvdiLayoutStatus("extended", out, f"{out.name} estendida: {out.describe()}")


def check_layout(
    sys_root: Path = Path("/sys"),
    runner: Runner = _run,
) -> EvdiLayoutStatus:
    """Query + log the full layout; warn clearly unless EVDI is extended."""
    layout = query_layout(runner)
    if layout.tool:
        log.info("Layout do compositor (%s): %d saída(s)", layout.tool, len(layout.outputs))
        for o in layout.outputs:
            log.info("  %s", o.describe())
        log.debug("%s raw:\n%s", layout.tool, layout.raw.rstrip())
    st = evdi_status(layout, evdi_connector_names(sys_root))
    if st.ok:
        log.info("EVDI: %s", st.message)
    else:
        log.warning("EVDI: %s", st.message)
    return st


def probe_evdi_layout(wait_s: float = 6.0) -> EvdiLayoutStatus:
    """Connect a throwaway EVDI monitor, let the compositor enable it, check it."""
    import time

    from .edid import build_edid
    from .evdi import EvdiError, EvdiMonitor
    from .virtual import evdi_modes

    modes = evdi_modes(1920, 1200, 60)
    mon = EvdiMonitor(build_edid(modes), area_limit=max(w * h for w, h, _ in modes))
    try:
        mon.open()
    except EvdiError as exc:
        return EvdiLayoutStatus("absent", None, f"EVDI não abriu: {str(exc).splitlines()[0]}")
    try:
        deadline = time.monotonic() + wait_s
        while mon.mode is None and time.monotonic() < deadline:
            time.sleep(0.2)
        if mon.mode is None:
            return EvdiLayoutStatus(
                "absent", None,
                f"o compositor não ativou o conector EVDI em {wait_s:g}s (cosmic-comp não usa o card evdi?)",
            )
        time.sleep(1.0)  # let cosmic-randr see the new head
        return check_layout()
    finally:
        mon.close()


def layout_checks(
    probe: Optional[Callable[[], EvdiLayoutStatus]] = None,
    sys_root: Path = Path("/sys"),
    runner: Runner = _run,
) -> Iterator[Check]:
    """Doctor section. `probe` briefly connects EVDI when no EVDI output is up."""
    layout = query_layout(runner)
    if not layout.tool:
        yield "fail", "layout do compositor (cosmic-randr/wlr-randr)", layout.error
        return
    yield "ok", "layout do compositor", f"{layout.tool}, {len(layout.outputs)} saída(s)"
    for o in layout.outputs:
        yield "info", "  saída", o.describe()
    st = evdi_status(layout, evdi_connector_names(sys_root))
    if st.status == "absent" and probe is not None:
        yield "info", "EVDI", "nenhuma saída EVDI ativa; conectando o monitor virtual por alguns segundos…"
        st = probe()
    if st.status == "absent" and probe is None:
        yield "info", "saída EVDI 'Segunda Tela'", "ausente (normal com o segunda-tela parado)"
        return
    yield ("ok" if st.ok else "fail"), "saída EVDI estendida", st.message
