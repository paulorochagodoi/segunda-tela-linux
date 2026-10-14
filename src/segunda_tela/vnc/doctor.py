"""`segunda-tela doctor` checks for `--usb-vnc`."""
from __future__ import annotations

import os
from typing import Iterator, Optional

from ..usb.doctor import local_port_free
from .link import VNC_PORT
from .wayvnc import WayvncAssessment, assess_wayvnc, detect_protocols, detect_wayvnc_text

Check = tuple[str, str, str]


def uinput_writable(path: str = "/dev/uinput") -> tuple[bool, str]:
    if not os.path.exists(path):
        return False, f"{path} ausente — sudo modprobe uinput"
    if not os.access(path, os.W_OK):
        return False, (
            f"{path} sem permissão de escrita — sudo ./scripts/install-deps-popos.sh "
            "e entre de novo na sessão (regra udev uaccess)"
        )
    return True, path


def vnc_checks(
    port: int = VNC_PORT,
    uinput_path: str = "/dev/uinput",
    wayvnc_text: Optional[str] = None,
    protocols: Optional[set[str]] = None,
    probe: bool = True,
) -> Iterator[Check]:
    """USB-VNC section. wayvnc is informational: this mode does not use it."""
    if probe:
        if wayvnc_text is None:
            wayvnc_text = detect_wayvnc_text()
        if protocols is None:
            protocols = detect_protocols()
    assessment: WayvncAssessment = assess_wayvnc(wayvnc_text, protocols)
    yield "info", "servidor VNC", assessment.summary
    for line in assessment.details:
        yield "info", "  wayvnc", line
    ok_in, detail = uinput_writable(uinput_path)
    yield ("ok" if ok_in else "fail"), "uinput (toque na saída EVDI)", detail
    free = local_port_free(port, "127.0.0.1")
    yield ("ok" if free else "fail"), f"porta local TCP {port} livre", (
        "" if free else "outro processo escuta 127.0.0.1 (segunda-tela --usb-vnc já rodando?)")
    yield "info", "bVNC no tablet", (
        "instale com.iiordanov.freebVNC (Play Store: bVNC). "
        "Com o adb autorizado o app abre em "
        "com.iiordanov.freebVNC/com.iiordanov.bVNC.RemoteCanvasActivity "
        "(vnc:// sem componente no One UI cai em outro app)")
