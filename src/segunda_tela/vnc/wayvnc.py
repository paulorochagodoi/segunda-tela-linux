"""Why `--usb-vnc` does not shell out to wayvnc on this machine.

Pop!_OS 24.04 (Ubuntu noble) ships wayvnc 0.7.2. That release captures
through `wlr-screencopy-unstable-v1` only. cosmic-comp does not implement
that protocol (pop-os/cosmic-comp#560) and will not: it implements
`ext-image-copy-capture-v1` instead (merged in cosmic-comp#1280, present
since COSMIC 1.0.0~beta.8). wayvnc learned that protocol in 0.9.0, which
is not in the noble archive (0.9.1 is in Ubuntu 25.10+).

wayvnc 0.9 can select one output (`--output DVI-I-1`, the same name
cosmic-randr shows for the EVDI connector) via ext-image-copy-capture.
Its pointer path still requires `wlr-virtual-pointer-unstable-v1`, which
cosmic-comp does not implement — the cosmic-comp authors confirmed wayvnc
only captures there with `--disable-input`. A disabled-input wayvnc also
owns the RFB connection, so those pointer events cannot be reinjected
through a uinput device mapped to the EVDI output alone.

The server in this package therefore always grabs the EVDI framebuffer
(the same pixels the WFD path already reads) and injects touch itself.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass

# Wayland interface names (wayland-info).
WLR_SCREENCOPY = "zwlr_screencopy_manager_v1"
EXT_IMAGE_COPY = "ext_image_copy_capture_manager_v1"
EXT_OUTPUT_CAPTURE = "ext_output_image_capture_source_manager_v1"
WLR_VIRTUAL_POINTER = "zwlr_virtual_pointer_manager_v1"

# First wayvnc release with ext-image-copy-capture output capture.
WAYVNC_OUTPUT_CAPTURE = (0, 9, 0)


@dataclass(frozen=True)
class WayvncAssessment:
    """`use_wayvnc` is false: the EVDI RFB server is the one that can both
    show only that output and feed uinput. `capture_possible` records
    whether a new enough wayvnc could at least name the output."""

    use_wayvnc: bool
    backend: str
    capture_possible: bool
    summary: str
    details: tuple[str, ...]


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def parse_wayland_interfaces(text: str) -> set[str]:
    return set(re.findall(r"interface:\s*'([^']+)'", text or ""))


def assess_wayvnc(
    wayvnc_text: str | None,
    protocols: set[str] | None,
) -> WayvncAssessment:
    """Decide the capture backend.

    `protocols` is None when this process is not inside a Wayland session
    (doctor from ssh, CI). The known COSMIC facts are used then, and the
    summary says so.
    """
    version = parse_version(wayvnc_text or "")
    installed = bool(wayvnc_text) and version is not None
    guessed = protocols is None
    prots = protocols or set()
    has_wlr = WLR_SCREENCOPY in prots
    # None means we could not probe. Do not invent interfaces; the summary
    # states the known COSMIC facts separately.
    has_ext = EXT_IMAGE_COPY in prots
    has_pointer = WLR_VIRTUAL_POINTER in prots
    new_enough = version is not None and version >= WAYVNC_OUTPUT_CAPTURE
    # Capture of one named output needs a protocol this wayvnc speaks.
    capture_possible = False
    if installed and has_wlr:
        capture_possible = True
    if installed and new_enough and has_ext:
        capture_possible = True

    details: list[str] = []
    if not installed:
        details.append("wayvnc não está instalado (não é necessário para este modo).")
    else:
        details.append(
            f"wayvnc {version[0]}.{version[1]}.{version[2]} "
            + ("fala ext-image-copy-capture (≥ 0.9)." if new_enough
               else "só fala wlr-screencopy (o 0.9 é que aprendeu ext-image-copy-capture).")
        )
    if guessed:
        details.append(
            "Sem sessão Wayland aqui para o wayland-info. No COSMIC do Pop!_OS 24.04 "
            "o cosmic-comp não implementa wlr-screencopy (cosmic-comp#560) e implementa "
            "ext-image-copy-capture desde 1.0.0~beta.8 (cosmic-comp#1280). "
            "O apt do noble é o wayvnc 0.7.2, que não fala esse protocolo."
        )
    else:
        if has_wlr:
            details.append("O compositor anuncia wlr-screencopy.")
        else:
            details.append("O compositor não anuncia wlr-screencopy.")
        if has_ext:
            source = " (com fonte por saída)" if EXT_OUTPUT_CAPTURE in prots else ""
            details.append(f"O compositor anuncia ext-image-copy-capture{source} — dá para nomear uma saída.")
        else:
            details.append("O compositor não anuncia ext-image-copy-capture.")
        if has_pointer:
            details.append("O compositor anuncia wlr-virtual-pointer.")
        else:
            details.append(
                "O compositor não anuncia wlr-virtual-pointer — o wayvnc não injeta ponteiro "
                "(no cosmic-comp isso é --disable-input; o toque tem de ser uinput)."
            )
    details.append(
        "O toque do tablet entra por um dispositivo uinput absoluto (touchscreen) "
        "mapeado só na saída EVDI. O wayvnc ficaria com o socket RFB, então esses "
        "eventos não chegam no uinput. O servidor VNC lê o framebuffer do EVDI "
        "(a mesma captura do modo estender) e serve só essa saída."
    )
    if capture_possible and not has_pointer:
        summary = (
            "wayvnc até capturaria a saída EVDI, mas não entrega o ponteiro no COSMIC; "
            "usando a captura direta do EVDI"
        )
    elif not capture_possible:
        summary = "wayvnc não consegue escolher a saída EVDI neste COSMIC; usando a captura direta do EVDI"
    else:
        summary = "wayvnc capturaria a saída, mas o toque pede uinput na saída EVDI; usando a captura direta do EVDI"
    return WayvncAssessment(
        use_wayvnc=False,
        backend="evdi",
        capture_possible=capture_possible,
        summary=summary,
        details=tuple(details),
    )


def detect_wayvnc_text() -> str | None:
    exe = shutil.which("wayvnc")
    if not exe:
        return None
    for args in (["--version"], ["-V"], []):
        try:
            proc = subprocess.run([exe, *args], capture_output=True, text=True, timeout=3, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        if parse_version(text):
            return text
    return "wayvnc (versão ilegível)"


def detect_protocols() -> set[str] | None:
    """Interfaces of the current compositor, or None outside a Wayland session."""
    import os

    if not os.environ.get("WAYLAND_DISPLAY") or not shutil.which("wayland-info"):
        return None
    try:
        proc = subprocess.run(["wayland-info"], capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 and not proc.stdout:
        return None
    return parse_wayland_interfaces(proc.stdout or "")
