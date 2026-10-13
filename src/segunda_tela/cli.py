from __future__ import annotations

import argparse
import signal
import sys
import time

from . import __app_name__, __version__
from .session import SecondScreenSession
from .util.logging import setup_logging


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="segunda-tela",
        description=(
            f"{__app_name__} — unofficial Wi-Fi Display (Miracast) source for Linux. "
            "Not affiliated with Samsung Electronics."
        ),
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1080)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--bitrate", type=int, default=8000, help="H.264 bitrate kbps")
    p.add_argument("--mode", choices=["extend", "mirror", "region"], default="extend")
    p.add_argument("--port", type=int, default=7236, help="WFD RTSP port (default 7236)")
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--ui", action="store_true", help="Launch GTK UI")
    p.add_argument(
        "--usb",
        action="store_true",
        help="USB cable mode: detect Samsung (VID 04e8), use tether/adb, then WFD over that link",
    )
    p.add_argument(
        "--prefer-adb",
        action="store_true",
        help="With --usb, try adb reverse before USB tethering",
    )
    p.add_argument(
        "--usb-wait",
        type=float,
        default=20.0,
        help="Seconds to wait for USB tether interface (default 20)",
    )
    p.add_argument(
        "--loopback-test",
        action="store_true",
        help="Run RTSP handshake against self and exit (CI / no tablet)",
    )
    p.add_argument(
        "--usb-detect-test",
        action="store_true",
        help="Only list Samsung USB devices from sysfs and exit (no stream)",
    )

    sub = p.add_subparsers(dest="command")
    usb_p = sub.add_parser("usb", help="Same as --usb (subcommand form)")
    usb_p.add_argument("--prefer-adb", action="store_true")
    usb_p.add_argument("--usb-wait", type=float, default=20.0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)

    use_usb = bool(getattr(args, "usb", False) or args.command == "usb")
    if args.command == "usb":
        # subcommand flags
        prefer_adb = args.prefer_adb
        usb_wait = args.usb_wait
    else:
        prefer_adb = args.prefer_adb
        usb_wait = args.usb_wait

    if args.ui:
        from .ui.app import run_ui

        return run_ui(args)

    if args.loopback_test:
        from .wfd.loopback import run_loopback_test

        return 0 if run_loopback_test(port=args.port + 1000) else 1

    if args.usb_detect_test:
        from .usb.detect import list_samsung_usb_devices

        devs = list_samsung_usb_devices()
        if not devs:
            print("Nenhum Samsung USB (04e8) no sysfs.")
            return 1
        for d in devs:
            print(f"{d.vid}:{d.pid} serial={d.serial or '-'} {d.product} ({d.sysfs})")
        return 0

    usb_link = None
    advertise = None
    if use_usb:
        from .usb.link import UsbLinkError, prepare_usb_link

        try:
            usb_link = prepare_usb_link(
                rtsp_port=args.port,
                prefer_adb=prefer_adb,
                wait_tether_s=usb_wait,
            )
        except UsbLinkError as exc:
            print(f"USB: {exc}", file=sys.stderr)
            return 2
        for n in usb_link.notes:
            print(f"USB: {n}", flush=True)
        if usb_link.transport != "adb-reverse":
            advertise = usb_link.local_ip

    session = SecondScreenSession(
        width=args.width,
        height=args.height,
        fps=args.fps,
        mode=args.mode,
        bind_host=args.bind,
        rtsp_port=args.port,
        bitrate_kbps=args.bitrate,
        advertise_ip=advertise,
        usb_link=usb_link,
    )
    session.start()

    def _stop(*_a):
        session.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    how = "USB+WFD" if use_usb else "WFD"
    print(
        f"{__app_name__} v{__version__} ({how}) listening. "
        f"Sink → rtsp://{session.advertise_ip}:{args.port}",
        flush=True,
    )
    print("Unofficial project — not affiliated with Samsung. Ctrl+C to stop.", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    raise SystemExit(main())
