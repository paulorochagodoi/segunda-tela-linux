from __future__ import annotations

import argparse
import logging
import os
import select
import signal
import sys

from . import __app_name__, __version__
from .util.logging import setup_logging


def _usb_initiate_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-usb-launch", action="store_true",
                   help="USB: do not try to open the receiver app/tile on the tablet via adb")
    p.add_argument("--usb-sink-activity", metavar="PKG/.Activity",
                   help="USB: tablet activity to start via adb (am start -n) before dialling the sink")
    p.add_argument("--usb-initiate-timeout", type=float, default=60.0,
                   help="USB: seconds to keep dialling the tablet's sink port (default 60)")


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
    p.add_argument("--dump-rtsp", action="store_true",
                   help="Print every RTSP request/response (M1–M16) verbatim on the console "
                        "(always written to the log file)")
    p.add_argument("--log-file", metavar="PATH",
                   help="Debug log file (default ~/.local/state/segunda-tela/segunda-tela.log, rotated)")
    hdcp = p.add_mutually_exclusive_group()
    hdcp.add_argument("--no-hdcp", dest="probe_hdcp", action="store_false",
                      help="Never mention content protection to the sink (default; same as gnome-network-displays)")
    hdcp.add_argument("--probe-hdcp", dest="probe_hdcp", action="store_true",
                      help="Diagnostics: also query wfd_content_protection in M3 (HDCP is still never selected; "
                           "some Samsung sinks then refuse with 'not secure')")
    p.set_defaults(probe_hdcp=False)
    p.add_argument(
        "--resolution",
        choices=("1080",),
        default=None,
        help="Force the stream and the EVDI monitor to 1920x1080p30 with H.264 level 4.0. "
             "gnome-network-displays sends 1080p30 to the Tab S10 FE and the tablet plays it. "
             "Without this flag the mode stays inside the sink's advertised level (720p30 when "
             "that level is only 3.1).",
    )
    p.add_argument("--width", type=int, default=None, help="Stream/monitor width (default: negotiate, monitor 1920)")
    p.add_argument("--height", type=int, default=None, help="Stream/monitor height (default: negotiate, monitor 1200)")
    p.add_argument("--fps", type=int, default=None, help="Max fps (default 60 with VAAPI, 30 with x264)")
    p.add_argument("--bitrate", type=int, default=None, help="H.264 bitrate kbps (default: by resolution)")
    p.add_argument("--mode", choices=["extend", "mirror", "region"], default="extend")
    p.add_argument(
        "--device",
        metavar="NOME|MAC|PATH",
        help="Wi-Fi P2P sink to connect to (see `segunda-tela list`). Without it: listen on the LAN.",
    )
    p.add_argument("--discover-timeout", type=float, default=20.0, help="P2P discovery seconds (default 20)")
    p.add_argument(
        "--capture",
        choices=["auto", "portal", "evdi", "x11", "test"],
        default="auto",
        help="Frame source (auto: extend → evdi, portal only as fallback; mirror → portal/x11): "
             "evdi (grab the virtual output directly), portal (ScreenCast), x11, test pattern",
    )
    p.add_argument(
        "--encoder",
        default="auto",
        help="H.264 encoder: auto|vah264enc|vaapih264enc|x264enc|openh264enc",
    )
    p.add_argument(
        "--audio",
        action="store_true",
        help="Send desktop audio. A silent AAC/LPCM track is already negotiated when the sink offers one "
        "(Samsung treats a video-only MPEG-TS as not secure)",
    )
    p.add_argument(
        "--no-touch",
        action="store_true",
        help="Do not negotiate WFD UIBC or inject finger/S Pen events. "
             "By default the source asks wfd_uibc_capability, listens for generic and HIDC "
             "multitouch (and Samsung TCP 50000), and injects contacts on the EVDI output only.",
    )
    p.add_argument(
        "--gnd-compat",
        action="store_true",
        help="Literal gnome-network-displays resolution: always 1920x1080p30, and do not force the "
             "SPS level. Without this flag the RTSP messages and encoder graph already match that "
             "project, but the mode is the best one inside the sink's H.264 level (720p30 on the "
             "Tab S10 FE, whose level byte 0x01 is only 3.1) and the SPS level matches that mode.",
    )
    p.add_argument("--port", type=int, default=7236, help="WFD RTSP port (default 7236)")
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--ui", action="store_true", help="Launch GTK UI")
    p.add_argument(
        "--usb",
        action="store_true",
        help="USB cable mode: detect Samsung (VID 04e8), use tether/adb, then WFD over that link",
    )
    p.add_argument("--prefer-adb", action="store_true", help="With --usb, try adb reverse before USB tethering")
    p.add_argument("--usb-wait", type=float, default=20.0, help="Seconds to wait for USB tether interface (default 20)")
    _usb_initiate_args(p)
    p.add_argument(
        "--loopback-test",
        action="store_true",
        help="Run WFD (M1–M7) + plain RTSP handshakes against self and exit (CI / no tablet)",
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
    _usb_initiate_args(usb_p)
    list_p = sub.add_parser("list", help="Discover Wi-Fi P2P peers (Miracast sinks)")
    list_p.add_argument("--timeout", type=float, default=15.0, help="Discovery seconds (default 15)")
    list_p.add_argument("--all", action="store_true", help="Also show peers without WFD IEs")
    sub.add_parser("doctor", help="Check EVDI (and that COSMIC extends onto it), GStreamer encoders, "
                                  "NetworkManager P2P, portal and USB/adb")
    return p


def cmd_list(args) -> int:
    from .wfd.p2p import NMWifiP2P, P2PError, format_peer_table

    try:
        nm = NMWifiP2P()
        last = {"n": -1}

        def progress(peers):
            if len(peers) != last["n"]:
                last["n"] = len(peers)
                print(f"… {len(peers)} peer(s)", file=sys.stderr, flush=True)

        peers = nm.discover(args.timeout, on_update=progress)
    except P2PError as exc:
        print(f"P2P: {exc}", file=sys.stderr)
        return 2
    if not args.all:
        peers = [p for p in peers if p.wfd_capable]
    if not peers:
        print("Nenhum sink Miracast encontrado. No tablet, deixe a tela de recepção (Smart View / "
              "‘Segunda tela’ / Wireless display) aberta e rode de novo. Use --all para ver todos os peers.")
        return 1
    print(format_peer_table(peers))
    return 0


def cmd_doctor(_args) -> int:
    import os
    import shutil

    from .display import evdi as ev
    from .wfd.pipeline import ENCODER_ORDER, gst_element_available, pick_encoder

    ok = True

    def line(good: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"[{'ok' if good else '!!'}] {label}{(' — ' + detail) if detail else ''}")

    line(bool(shutil.which("gst-launch-1.0")), "gst-launch-1.0")
    for el in ("mpegtsmux", "rtpmp2tpay", "pipewiresrc", "rawvideoparse"):
        line(gst_element_available(el), f"GStreamer element {el}")
    encs = [e for e in ENCODER_ORDER if gst_element_available(e)]
    line(bool(encs), "H.264 encoders", ", ".join(encs) or "none (install gstreamer1.0-plugins-bad / -vaapi / -ugly)")
    print(f"     → would use: {pick_encoder()}")
    line(ev.module_loaded(), "evdi kernel module", "" if ev.module_loaded() else
         "sudo ./scripts/setup-evdi.sh (Secure Boot: cadastre a chave MOK, ver docs/COSMIC.md)")
    try:
        ev.load_libevdi()
        line(True, "libevdi")
    except ev.EvdiError:
        line(False, "libevdi", "sudo ./scripts/setup-evdi.sh")
    line(bool(ev.evdi_cards()), "evdi DRM device", ev.device_node_hint())
    uinput = os.access("/dev/uinput", os.W_OK)
    print(f"[{'ok' if uinput else '..'}] /dev/uinput"
          + ("" if uinput else " — sudo ./scripts/setup-evdi.sh (then log out and back in) for touch"))
    session = os.environ.get("XDG_SESSION_TYPE", "?")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "?")
    print(f"[..] session={session} desktop={desktop}")
    try:
        from .wfd.p2p import NMWifiP2P

        dev, iface = NMWifiP2P().p2p_device()
        line(True, "NetworkManager Wi-Fi P2P device", f"{iface} ({dev})")
    except Exception as exc:  # noqa: BLE001
        line(False, "NetworkManager Wi-Fi P2P device", str(exc))
    try:
        from .display.portal import _GioSession

        s = _GioSession(timeout_s=5)
        try:
            line(True, "ScreenCast portal", f"version {s.prop('version')}")
        finally:
            s.finish_setup()
    except Exception as exc:  # noqa: BLE001
        line(False, "ScreenCast portal", str(exc))

    from .display.randr import layout_checks, probe_evdi_layout

    print("-- Tela estendida (COSMIC) --")
    can_probe = ev.module_loaded() and bool(ev.evdi_cards())
    for status, label, detail in layout_checks(probe=probe_evdi_layout if can_probe else None):
        if status == "info":
            print(f"[..] {label}{(' — ' + detail) if detail else ''}")
        else:
            line(status == "ok", label, detail)

    from .usb.doctor import usb_checks

    print("-- USB --")
    for status, label, detail in usb_checks(rtsp_port=getattr(_args, "port", 7236)):
        if status == "info":
            print(f"[..] {label}{(' — ' + detail) if detail else ''}")
        else:
            line(status == "ok", label, detail)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_path = setup_logging(args.verbose, dump_rtsp=args.dump_rtsp, log_file=args.log_file)
    if log_path:
        print(f"Log: {log_path}", file=sys.stderr, flush=True)
    logging.getLogger("segunda_tela").info("%s v%s argv=%s", __app_name__, __version__,
                                           sys.argv[1:] if argv is None else argv)

    if args.command == "list":
        return cmd_list(args)
    if args.command == "doctor":
        return cmd_doctor(args)

    use_usb = bool(getattr(args, "usb", False) or args.command == "usb")
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

    if use_usb and args.device:
        print("Use --usb ou --device, não ambos.", file=sys.stderr)
        return 2

    usb_link = None
    advertise = None
    if use_usb:
        from .usb.link import UsbLinkError, prepare_usb_link

        try:
            usb_link = prepare_usb_link(rtsp_port=args.port, prefer_adb=prefer_adb, wait_tether_s=usb_wait)
        except UsbLinkError as exc:
            print(f"USB: {exc}", file=sys.stderr)
            return 2
        for n in usb_link.notes:
            print(f"USB: {n}", flush=True)
        if usb_link.transport != "adb-reverse":
            advertise = usb_link.local_ip

    from .session import SecondScreenSession

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
        capture=args.capture,
        encoder=args.encoder,
        audio=args.audio,
        probe_hdcp=args.probe_hdcp,
        gnd_compat=args.gnd_compat,
        force_1080p30=args.resolution == "1080",
        touch=not args.no_touch,
    )

    # Wake the main thread with a byte. Stopping GStreamer inside this
    # handler is what left gst_poll_set_flushing and a KeyboardInterrupt
    # traceback in the log.
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_w, False)

    def _stop(*_a):
        try:
            os.write(wake_w, b"\0")
        except OSError:
            pass

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        session.start()
    except Exception as exc:  # noqa: BLE001
        print(f"Erro ao iniciar: {exc}", file=sys.stderr)
        session.stop()
        return 2
    for note in session.notes:
        print(note, flush=True)

    how = "USB+WFD" if use_usb else "WFD"
    if use_usb:
        session.start_usb_initiator(
            launch=not args.no_usb_launch,
            sink_activity=args.usb_sink_activity,
            timeout_s=args.usb_initiate_timeout,
            say=lambda m: print(m, flush=True),
        )
    if args.device:
        from .wfd.p2p import P2PError, find_and_connect

        print(f"Procurando '{args.device}' via Wi-Fi Direct ({args.discover_timeout:.0f}s)…", flush=True)
        try:
            link = find_and_connect(args.device, discover_s=args.discover_timeout, rtsp_port=args.port)
        except P2PError as exc:
            print(f"P2P: {exc}", file=sys.stderr)
            session.stop()
            return 2
        session.attach_p2p(link)
        how = f"P2P+WFD {link.interface}"
        print(f"Wi-Fi Direct conectado a {link.peer.label} ({link.interface}, {link.local_ip}). "
              f"Aguardando o tablet abrir RTSP…", flush=True)

    print(
        f"{__app_name__} v{__version__} ({how}, {args.mode}) pronto. "
        f"Sink → rtsp://{session.advertise_ip}:{args.port}",
        flush=True,
    )
    print("Unofficial project — not affiliated with Samsung. Ctrl+C to stop.", flush=True)
    try:
        while True:
            ready, _, _ = select.select([wake_r], [], [], 3600)
            if ready:
                break
    finally:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        session.stop()
        os.close(wake_r)
        os.close(wake_w)


if __name__ == "__main__":
    raise SystemExit(main())
