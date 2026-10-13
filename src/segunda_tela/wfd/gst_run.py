"""Child process that runs the WFD pipeline and honours IDR requests.

`StreamPipeline` spawns this when PyGObject's Gst bindings are importable.
The launch description arrives in `SEGUNDA_TELA_PIPELINE`. File descriptor
argv[1] is a pipe: each line (`idr`) posts an upstream force-key-unit event
at `h264parse`, which is the same event gnome-network-displays sends for
`wfd_idr_request`.

gst-launch cannot receive that event, so it is only the fallback.
"""
from __future__ import annotations

import os
import signal
import sys
import threading


def force_key_unit(pipeline) -> bool:
    """Send GstForceKeyUnit upstream from the named h264parse element."""
    from gi.repository import Gst

    element = pipeline.get_by_name("wfd-h264parse")
    if element is None:
        print("wfd-h264parse not in pipeline; cannot force IDR", file=sys.stderr)
        return False
    pad = element.get_static_pad("src")
    if pad is None:
        return False
    try:
        import gi

        gi.require_version("GstVideo", "1.0")
        from gi.repository import GstVideo

        # Introspection name is video_event_new_upstream_force_key_unit.
        # CLOCK_TIME_NONE is the all-ones guint64; all-headers asks for SPS/PPS.
        event = GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0)
    except (ImportError, ValueError, AttributeError):
        parsed = Gst.Structure.from_string(
            "GstForceKeyUnit,running-time=(guint64)18446744073709551615,"
            "all-headers=(boolean)true,count=(uint)0"
        )
        structure = parsed[0] if isinstance(parsed, tuple) else parsed
        event = Gst.Event.new_custom(Gst.EventType.CUSTOM_UPSTREAM, structure)
    return bool(pad.send_event(event))


def _idr_loop(pipeline, fd: int) -> None:
    try:
        with os.fdopen(fd, "r", closefd=True) as handle:
            for line in handle:
                if line.strip():
                    force_key_unit(pipeline)
    except OSError as exc:
        print(f"IDR pipe closed: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    description = os.environ.get("SEGUNDA_TELA_PIPELINE", "").strip()
    if not description:
        print("SEGUNDA_TELA_PIPELINE is empty", file=sys.stderr)
        return 2
    idr_fd = int(argv[1]) if len(argv) > 1 else -1

    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    try:
        pipeline = Gst.parse_launch(description)
    except Exception as exc:  # noqa: BLE001 — GLib may raise GError
        print(f"parse_launch failed: {exc}", file=sys.stderr)
        return 1

    def _stop(*_args: object) -> None:
        pipeline.send_event(Gst.Event.new_eos())

    signal.signal(signal.SIGTERM, _stop)
    pipeline.set_state(Gst.State.PLAYING)
    if idr_fd >= 0:
        threading.Thread(target=_idr_loop, args=(pipeline, idr_fd), name="wfd-idr", daemon=True).start()

    bus = pipeline.get_bus()
    try:
        while True:
            msg = bus.timed_pop_filtered(
                100 * Gst.MSECOND,
                Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.WARNING,
            )
            if msg is None:
                continue
            if msg.type == Gst.MessageType.WARNING:
                warn, _debug = msg.parse_warning()
                print(warn.message, file=sys.stderr)
                continue
            if msg.type == Gst.MessageType.ERROR:
                err, _debug = msg.parse_error()
                print(err.message, file=sys.stderr)
                return 1
            return 0
    finally:
        pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    sys.exit(main())
