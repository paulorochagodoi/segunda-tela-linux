"""Display side: create the extra monitor (extend) and pick a capture source.

extend:  EVDI virtual connector with our EDID → compositor adds an output →
         grab *that* output's frames straight from EVDI (default; no portal).
         The ScreenCast portal is a fallback (EVDI unusable, or
         `--capture portal`); a remembered portal choice that is not the
         EVDI output (e.g. eDP-1) is forgotten and the picker asked again.
mirror:  portal screencast of an existing output (Wayland) / ximagesrc (X11).
test:    videotestsrc (CI, no desktop).

Uses existing user-space mechanisms only — no custom kernel module.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from ..wfd.pipeline import CaptureSource, pattern_source

log = logging.getLogger(__name__)

CAPTURE_CHOICES = ("auto", "portal", "evdi", "x11", "test")


@dataclass
class VirtualDisplay:
    width: int
    height: int
    mode: str  # "extend" | "mirror" | "region"
    backend: str
    fps: int = 30
    evdi: Optional[object] = None  # EvdiMonitor
    portal: Optional[object] = None  # PortalCapture
    layout: Optional[object] = None  # randr.EvdiLayoutStatus
    notes: list[str] = field(default_factory=list)

    @property
    def capture_source(self) -> str:
        """Back-compat: gst fragment of a fresh capture source."""
        return self.new_capture().gst

    def new_capture(self) -> CaptureSource:
        """A fresh CaptureSource for one pipeline run."""
        if self.backend == "portal" and self.portal is not None:
            from .portal import pipewiresrc_fragment

            return CaptureSource(
                gst=pipewiresrc_fragment(self.portal.fd, self.portal.node_id, self.fps),
                backend="portal",
                pass_fds=(self.portal.fd,),
            )
        if self.backend == "evdi-grab" and self.evdi is not None:
            from .evdi import EVDI_RAW_FORMAT, EvdiFrameFeeder

            mon = self.evdi
            deadline = time.monotonic() + 5
            while mon.mode is None and time.monotonic() < deadline:
                time.sleep(0.1)
            if mon.mode is None:
                log.warning("compositor has not enabled the EVDI output yet; streaming test pattern")
                return pattern_source(self.width, self.height, self.fps)
            w, h, _r = mon.mode
            feeder = EvdiFrameFeeder(mon, w, h, self.fps)
            return CaptureSource(
                gst=(
                    f"fdsrc fd=0 blocksize={w * h * 4} do-timestamp=true ! "
                    f"rawvideoparse use-sink-caps=false width={w} height={h} format={EVDI_RAW_FORMAT} "
                    f"framerate={self.fps}/1"
                ),
                backend="evdi-grab",
                stdin_feeder=feeder,
                feeder_stop=feeder.stop,
            )
        if self.backend == "x11":
            return CaptureSource(
                gst=f"ximagesrc use-damage=false show-pointer=true ! video/x-raw,framerate={self.fps}/1",
                backend="x11",
            )
        return pattern_source(self.width, self.height, self.fps)

    def close(self) -> None:
        if self.portal is not None:
            try:
                self.portal.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("portal cleanup: %s", exc)
            self.portal = None
        if self.evdi is not None:
            try:
                self.evdi.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("evdi cleanup: %s", exc)
            self.evdi = None


def _wayland() -> bool:
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))


def _x11() -> bool:
    return bool(os.environ.get("DISPLAY")) and not _wayland()


def resolve_capture(mode: str, capture: str, wayland: bool, x11: bool) -> str:
    """Decide the capture backend (pure, unit-tested)."""
    if capture != "auto":
        return capture
    if mode == "extend":
        return "evdi"
    if wayland:
        return "portal"
    if x11:
        return "x11"
    return "test"


def evdi_modes(width: int, height: int, fps: int) -> list[tuple[int, int, int]]:
    """EDID mode list: requested/negotiated mode first, then tablet-ish modes."""
    modes = [(width, height, 60 if fps >= 50 else fps)]
    for m in ((1920, 1200, 60), (2560, 1600, 60), (1920, 1080, 60)):
        if m not in modes:
            modes.append(m)
    return modes


def _fallback_backend(wayland: bool, x11: bool) -> str:
    if wayland:
        return "portal"
    return "x11" if x11 else "test"


def create_virtual_display(
    width: int = 1920,
    height: int = 1200,
    mode: str = "extend",
    fps: int = 30,
    capture: str = "auto",
    portal_restore: bool = True,
) -> VirtualDisplay:
    wayland, x11 = _wayland(), _x11()
    backend = resolve_capture(mode, capture, wayland, x11)
    disp = VirtualDisplay(width=width, height=height, mode=mode, backend=backend, fps=fps)

    if backend == "test":
        log.warning("Using videotestsrc (test pattern)")
        return disp

    if mode == "extend":
        from .edid import build_edid
        from .evdi import EvdiError, EvdiMonitor

        modes = evdi_modes(width, height, fps)
        try:
            mon = EvdiMonitor(build_edid(modes), area_limit=max(w * h for w, h, _ in modes),
                              grab=(backend == "evdi"), fps=fps)
            mon.open()
            disp.evdi = mon
            disp.notes.append(f"EVDI output criado: {modes[0][0]}x{modes[0][1]} (+{len(modes) - 1} modos)")
        except EvdiError as exc:
            msg = f"EXTEND indisponível (EVDI): {exc}"
            log.error("%s", msg)
            disp.notes.append(msg)
            if capture == "evdi":
                raise
            if backend == "evdi":
                backend = disp.backend = _fallback_backend(wayland, x11)
            disp.notes.append(f"Continuando com captura {backend} (efetivamente espelhamento).")
        # Give the compositor a moment to light up the new connector.
        if disp.evdi is not None:
            deadline = time.monotonic() + 5
            while disp.evdi.mode is None and time.monotonic() < deadline:
                time.sleep(0.2)
            if disp.evdi.mode is None:
                disp.notes.append(
                    "O compositor ainda não ativou o monitor virtual. Em Configurações → Telas, "
                    "ative/posicione 'Segunda Tela' (ver docs/COSMIC.md)."
                )
            _check_layout(disp)
        if backend == "evdi":
            disp.backend = "evdi-grab"
            log.info("Captura: leitura direta do EVDI (sem portal)")
            return disp

    if backend == "portal":
        _open_portal(disp, portal_restore)
        return disp

    if backend == "x11":
        return disp

    raise ValueError(f"unknown capture backend {backend!r}")


def _check_layout(disp: VirtualDisplay) -> None:
    """Log the compositor layout; note clearly if EVDI is mirrored or absent."""
    from .randr import check_layout

    try:
        st = check_layout()
    except Exception as exc:  # noqa: BLE001 — diagnostics must never stop the session
        log.warning("layout check failed: %s", exc)
        return
    disp.layout = st
    if st.status in ("absent", "disabled", "mirrored"):
        disp.notes.append(f"AVISO (tela estendida): {st.message}")


def _open_portal(disp: VirtualDisplay, restore: bool) -> None:
    from .portal import PortalError, forget_restore_token, load_restore_token, open_screencast, stream_is_output

    # Without an EVDI output the portal can only mirror: keep that choice apart
    # from the remembered "extend" one.
    extend = disp.mode == "extend" and disp.evdi is not None
    key = "extend" if extend else "mirror"
    log.info("Abrindo seletor do portal ScreenCast — escolha a tela %s",
             "'Segunda Tela' (virtual)" if extend else "a espelhar")
    for attempt in (1, 2):
        remembered = restore and load_restore_token(key) is not None
        try:
            cap = open_screencast(persist_key=key if restore else None)
        except PortalError as exc:
            disp.close()
            raise RuntimeError(f"ScreenCast portal: {exc}") from exc
        if not extend:
            disp.portal = cap
            return
        mon = disp.evdi
        expected = mon.mode[:2] if mon.mode else None
        stream = cap.streams[0]
        evdi_out = getattr(disp.layout, "output", None)
        if stream_is_output(stream, expected, evdi_out.position if evdi_out else None):
            disp.portal = cap
            return
        cap.close()
        forgot = forget_restore_token(key)
        log.warning(
            "Portal: a tela %s (pos=%s size=%s) não é a saída EVDI 'Segunda Tela' (parece eDP-1)%s",
            "lembrada" if remembered else "escolhida", stream.position, stream.size,
            " — escolha salva apagada de ~/.config/segunda-tela/portal.json" if forgot else "",
        )
        if attempt == 1:
            log.warning("Abrindo o seletor de novo — escolha 'Segunda Tela'")
    disp.notes.append(
        "AVISO: o portal não entregou a tela virtual; usando leitura direta do EVDI em vez dela."
    )
    disp.evdi.enable_grab()
    disp.backend = "evdi-grab"
