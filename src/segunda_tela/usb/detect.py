"""Detect Samsung USB devices (vendor 04e8) via sysfs — no proprietary libs."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

log = logging.getLogger(__name__)

SAMSUNG_VID = "04e8"
# Common Galaxy composite PID seen in Windows AQS filter evidence (MTP/ADB):
DEFAULT_PIDS = {"6860", "6863", "6864", "6865", "6866", "6877", "685d"}


@dataclass(frozen=True)
class UsbDevice:
    vid: str
    pid: str
    serial: str
    sysfs: str
    product: str = ""
    manufacturer: str = ""

    @property
    def is_samsung(self) -> bool:
        return self.vid.lower() == SAMSUNG_VID


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def iter_usb_devices(sys_bus_usb: str | Path = "/sys/bus/usb/devices") -> Iterable[UsbDevice]:
    root = Path(sys_bus_usb)
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        # Skip interfaces (contain ':') and hubs without idVendor
        if ":" in entry.name:
            continue
        vid = _read(entry / "idVendor").lower()
        pid = _read(entry / "idProduct").lower()
        if not vid or not pid:
            continue
        yield UsbDevice(
            vid=vid,
            pid=pid,
            serial=_read(entry / "serial"),
            sysfs=str(entry),
            product=_read(entry / "product"),
            manufacturer=_read(entry / "manufacturer"),
        )


def list_samsung_usb_devices(
    sys_bus_usb: str | Path = "/sys/bus/usb/devices",
    pids: Optional[set[str]] = None,
) -> List[UsbDevice]:
    allow = {p.lower() for p in (pids or DEFAULT_PIDS)}
    out: List[UsbDevice] = []
    for d in iter_usb_devices(sys_bus_usb):
        if not d.is_samsung:
            continue
        # Accept any Samsung PID; preferred list is advisory
        if allow and d.pid not in allow:
            log.debug("Samsung device VID=%s PID=%s (not in preferred list)", d.vid, d.pid)
        out.append(d)
    return out


def find_usb_net_ifaces() -> List[str]:
    """Network interfaces that look like USB tethering (rndis/ncm/cdc)."""
    net = Path("/sys/class/net")
    if not net.is_dir():
        return []
    hits = []
    for iface in sorted(net.iterdir()):
        name = iface.name
        if name == "lo":
            continue
        # usb0/usb1 common; also enx* for CDC
        uevent = _read(iface / "uevent").lower()
        driver = ""
        try:
            driver = os.path.basename(os.readlink(iface / "device" / "driver"))
        except OSError:
            pass
        blob = f"{name} {uevent} {driver}"
        if any(k in blob for k in ("rndis", "ncm", "cdc_ether", "cdc_ncm", "usb0", "usb1")):
            hits.append(name)
        elif name.startswith("usb") or name.startswith("enx"):
            # device path under usb
            try:
                real = str((iface / "device").resolve())
                if "/usb" in real:
                    hits.append(name)
            except OSError:
                pass
    return hits
