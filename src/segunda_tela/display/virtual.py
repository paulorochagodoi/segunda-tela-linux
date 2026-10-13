"""Virtual / capture display helpers for extend/mirror modes.

Uses existing user-space mechanisms only — no custom kernel module.
Priority: PipeWire portal (Wayland) → X11/XRandR dummy → fallback primary capture.
Optional EVDI is documented in scripts/install-evdi.sh but not required.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class VirtualDisplay:
    width: int
    height: int
    mode: str  # "extend" | "mirror" | "region"
    capture_source: str  # gstreamer source description fragment
    backend: str
    cleanup: Optional[object] = None

    def close(self) -> None:
        if callable(self.cleanup):
            try:
                self.cleanup()
            except Exception as exc:  # noqa: BLE001
                log.warning("display cleanup: %s", exc)


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _session_type() -> str:
    return os.environ.get("XDG_SESSION_TYPE", "").lower()


def create_virtual_display(
    width: int = 1920,
    height: int = 1080,
    mode: str = "extend",
    fps: int = 30,
) -> VirtualDisplay:
    """Pick a capture pipeline for the chosen mode.

    True "extend" (extra monitor) needs compositor support or EVDI.
    When unavailable we fall back to mirroring the primary output and
    document the limitation.
    """
    session = _session_type()

    if mode == "mirror" or mode == "region":
        if session == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
            # pipewiresrc needs portal permission at runtime
            return VirtualDisplay(
                width=width,
                height=height,
                mode=mode,
                backend="pipewire",
                capture_source=(
                    f"pipewiresrc do-timestamp=true ! "
                    f"video/x-raw,framerate={fps}/1 ! videoconvert"
                ),
            )
        if os.environ.get("DISPLAY"):
            return VirtualDisplay(
                width=width,
                height=height,
                mode=mode,
                backend="x11",
                capture_source=(
                    f"ximagesrc use-damage=false show-pointer=true ! "
                    f"video/x-raw,framerate={fps}/1 ! videoconvert"
                ),
            )

    # extend: try mutter/gnome via dbus virtual monitor if available later;
    # for v0.1 use PipeWire screencast of a dedicated region / whole screen
    # and recommend EVDI for a real extra output.
    if _have("gnome-network-displays"):
        log.info("gnome-network-displays is installed; this app can coexist with it")

    if session == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
        log.warning(
            "Extend mode without EVDI/compositor virtual monitor will stream "
            "a portal screencast (effectively mirror/region). See docs/VIRTUAL_DISPLAY.md"
        )
        return VirtualDisplay(
            width=width,
            height=height,
            mode="extend",
            backend="pipewire-portal",
            capture_source=(
                f"pipewiresrc do-timestamp=true ! "
                f"video/x-raw,framerate={fps}/1 ! videoconvert ! "
                f"videoscale ! video/x-raw,width={width},height={height}"
            ),
        )

    if os.environ.get("DISPLAY") and _have("xrandr"):
        # Try a disconnected output with --newmode/--addmode if present
        dummy = _try_xrandr_dummy(width, height)
        if dummy:
            return dummy
        return VirtualDisplay(
            width=width,
            height=height,
            mode="extend",
            backend="x11-primary",
            capture_source=(
                f"ximagesrc use-damage=false ! "
                f"video/x-raw,framerate={fps}/1 ! videoconvert ! "
                f"videoscale ! video/x-raw,width={width},height={height}"
            ),
        )

    # headless / CI: videotestsrc
    log.warning("No graphical session; using videotestsrc (test pattern)")
    return VirtualDisplay(
        width=width,
        height=height,
        mode=mode,
        backend="testsrc",
        capture_source=(
            f"videotestsrc is-live=true pattern=smpte ! "
            f"video/x-raw,width={width},height={height},framerate={fps}/1 ! videoconvert"
        ),
    )


def _try_xrandr_dummy(width: int, height: int) -> Optional[VirtualDisplay]:
    try:
        out = subprocess.check_output(["xrandr"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return None
    # Prefer an already-connected extra output; otherwise leave creation to install script
    for line in out.splitlines():
        if " disconnected" in line:
            continue
    log.debug("xrandr dummy creation deferred to scripts/setup-xdummy.sh")
    return None
