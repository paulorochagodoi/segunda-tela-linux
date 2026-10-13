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
    p.add_argument("--ui", action="store_true", help="Launch GTK tray UI")
    p.add_argument(
        "--loopback-test",
        action="store_true",
        help="Run RTSP handshake against self and exit (CI / no tablet)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)

    if args.ui:
        from .ui.app import run_ui

        return run_ui(args)

    if args.loopback_test:
        from .wfd.loopback import run_loopback_test

        return 0 if run_loopback_test(port=args.port + 1000) else 1

    session = SecondScreenSession(
        width=args.width,
        height=args.height,
        fps=args.fps,
        mode=args.mode,
        bind_host=args.bind,
        rtsp_port=args.port,
        bitrate_kbps=args.bitrate,
    )
    session.start()

    def _stop(*_a):
        session.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    print(
        f"{__app_name__} v{__version__} listening. "
        f"On the tablet, open wireless display / Smart View and pick this PC, "
        f"or point a WFD sink at rtsp://{session.advertise_ip}:{args.port}",
        flush=True,
    )
    print("Unofficial project — not affiliated with Samsung. Ctrl+C to stop.", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    raise SystemExit(main())
