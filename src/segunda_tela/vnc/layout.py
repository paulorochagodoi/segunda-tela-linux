"""Put the EVDI output to the right of the internal panel, and map touch to it.

cosmic-comp maps a touchscreen (INPUT_PROP_DIRECT) with
`~/.config/cosmic/com.system76.CosmicComp/v1/input_devices`:

    "Segunda Tela VNC": (state: Enabled, map_to_output: Some("DVI-I-1"))

Absolute *pointers* ignore that key (cosmic-comp#2103). The uinput device is
therefore a touchscreen, which the touch handler does honor.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from ..display.randr import Output, _overlaps

log = logging.getLogger(__name__)

UINPUT_NAME = "Segunda Tela VNC"
_PRIMARY = ("eDP-1", "eDP-2", "LVDS-1", "LVDS-2")
Runner = Callable[[list[str], float], tuple[int, str]]


def input_devices_path(home: Optional[Path] = None) -> Path:
    base = home if home is not None else Path.home()
    return base / ".config" / "cosmic" / "com.system76.CosmicComp" / "v1" / "input_devices"


def primary_output(outputs: list[Output], evdi: Optional[Output]) -> Optional[Output]:
    """The panel the virtual monitor should sit beside. Prefer eDP-1."""
    for name in _PRIMARY:
        for out in outputs:
            if out.name == name and out.enabled and out.position and out.logical_size:
                return out
    others = [
        out for out in outputs
        if out is not evdi and out.enabled and out.position and out.logical_size
    ]
    if not others:
        return None
    return min(others, key=lambda out: (out.x if out.x is not None else 0, out.name))


def is_extended_right(evdi: Output, anchor: Output) -> bool:
    """True when `evdi` is already beside `anchor`, not on top of it."""
    if evdi.position is None or anchor.position is None or not anchor.logical_size:
        return False
    if not evdi.enabled or _overlaps(evdi, anchor):
        return False
    ex, ey = evdi.position
    ax, ay = anchor.position
    aw, ah = anchor.logical_size
    eh = evdi.logical_size[1] if evdi.logical_size else 0
    beside = ex >= ax + aw
    vertical = ey < ay + ah and ay < ey + max(eh, 1)
    return beside and vertical


def target_right_of(anchor: Output) -> tuple[int, int]:
    if anchor.position is None or not anchor.logical_size:
        raise ValueError("anchor sem geometria")
    ax, ay = anchor.position
    return ax + anchor.logical_size[0], ay


def position_commands(
    output: str, x: int, y: int, width: Optional[int] = None, height: Optional[int] = None,
) -> list[list[str]]:
    """cosmic-randr first (Pop!_OS 24.04), then the mode form, then wlr-randr."""
    cmds = [["cosmic-randr", "position", output, str(int(x)), str(int(y))]]
    if width and height:
        cmds.append([
            "cosmic-randr", "mode", output, str(int(width)), str(int(height)),
            "--pos-x", str(int(x)), "--pos-y", str(int(y)),
        ])
    cmds.append(["wlr-randr", "--output", output, "--pos", f"{int(x)},{int(y)}"])
    return cmds


def _default_runner(argv: list[str], timeout: float) -> tuple[int, str]:
    if not shutil.which(argv[0]):
        return 127, ""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    text = (proc.stderr or proc.stdout or "").strip()
    return proc.returncode, text


def apply_position(
    output: str,
    x: int,
    y: int,
    width: Optional[int] = None,
    height: Optional[int] = None,
    runner: Runner = _default_runner,
) -> tuple[bool, str]:
    """Run the first compositor tool that accepts the new position."""
    errors: list[str] = []
    for argv in position_commands(output, x, y, width, height):
        rc, text = runner(argv, 5.0)
        if rc == 127:
            continue
        if rc == 0:
            return True, " ".join(argv)
        errors.append(f"{' '.join(argv)} → rc={rc} {text[:160]}")
    if not errors:
        return False, "cosmic-randr/wlr-randr não instalados"
    return False, "; ".join(errors)


def upsert_touch_map(text: str, output: str, device: str = UINPUT_NAME) -> str:
    """Insert or retarget `device` → `output` in a cosmic input_devices RON file."""
    if '"' in output or "\n" in output or not output:
        raise ValueError(f"nome de saída inválido: {output!r}")
    entry = (
        f'"{device}": (\n'
        f"state: Enabled,\n"
        f'map_to_output: Some("{output}")\n'
        f")"
    )
    pattern = re.compile(
        rf'"{re.escape(device)}"\s*:\s*\([\s\S]*?map_to_output:\s*Some\("([^"]*)"\)',
    )
    if pattern.search(text or ""):
        def _retarget(match: re.Match[str]) -> str:
            return match.group(0).replace(f'Some("{match.group(1)}")', f'Some("{output}")', 1)
        return pattern.sub(_retarget, text, count=1)
    body = (text or "").strip()
    if not body:
        return "{\n" + entry + ",\n}\n"
    # Drop a trailing comma-less close so we can insert before the last brace.
    idx = body.rfind("}")
    if idx == -1:
        return body + "\n{\n" + entry + ",\n}\n"
    prefix = body[:idx].rstrip()
    if prefix and not prefix.endswith(",") and not prefix.endswith("{"):
        prefix += ","
    return prefix + "\n" + entry + ",\n}\n"


def install_touch_map(
    output: str,
    path: Optional[Path] = None,
    device: str = UINPUT_NAME,
) -> Path:
    dest = path or input_devices_path()
    current = dest.read_text(encoding="utf-8") if dest.is_file() else ""
    updated = upsert_touch_map(current, output, device)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(updated, encoding="utf-8")
    tmp.replace(dest)
    log.info("toque VNC mapeado para %s em %s", output, dest)
    return dest
