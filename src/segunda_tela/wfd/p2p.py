"""Wi-Fi P2P helpers via NetworkManager (nmcli). Optional — many setups use same LAN."""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import List, Tuple

log = logging.getLogger(__name__)


def nmcli_available() -> bool:
    return shutil.which("nmcli") is not None


def list_wifi_devices() -> List[str]:
    if not nmcli_available():
        return []
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"],
            text=True,
        )
    except subprocess.CalledProcessError:
        return []
    return [line.split(":")[0] for line in out.splitlines() if ":wifi" in line]


def start_p2p_group(device: str | None = None) -> Tuple[bool, str]:
    """Best-effort autonomous GO. Driver support varies on Pop!_OS laptops."""
    if not nmcli_available():
        return False, "nmcli not found"
    devs = list_wifi_devices()
    if not device:
        device = devs[0] if devs else ""
    if not device:
        return False, "no wifi device"
    # NetworkManager wifi-p2p support is incomplete on many stacks; document failure.
    try:
        subprocess.check_call(
            ["nmcli", "device", "wifi", "hotspot", "ifname", device, "ssid", "SEGUNDA-TELA-P2P", "password", "segundatela"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        return True, f"hotspot on {device} (fallback; not full WFD P2P)"
    except subprocess.CalledProcessError as exc:
        return False, f"P2P/hotspot failed: {exc}. Connect tablet and PC to the same LAN, or use gnome-network-displays' P2P stack."
