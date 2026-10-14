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
import time
from typing import Callable, Optional


class FpsMeter:
    """Emit `stream fps …` once `interval` seconds of frames have elapsed."""

    def __init__(self, interval: float = 5.0, now: Callable[[], float] = time.monotonic) -> None:
        self.interval = interval
        self._now = now
        self.n = 0
        self.t0 = now()

    def tick(self, now: Optional[float] = None) -> Optional[str]:
        self.n += 1
        current = self._now() if now is None else now
        dt = current - self.t0
        if dt < self.interval or self.n <= 0:
            return None
        msg = f"stream fps {self.n / dt:.1f} ({self.n} frames in {dt:.1f}s)"
        self.n = 0
        self.t0 = current
        return msg


def attach_stream_fps_probe(pipeline, interval: float = 5.0, emit: Optional[Callable[[str], None]] = None) -> bool:
    """Count encoded frames on `wfd-h264parse` and emit fps every `interval`."""
    from gi.repository import Gst

    element = pipeline.get_by_name("wfd-h264parse")
    if element is None:
        return False
    pad = element.get_static_pad("src")
    if pad is None:
        return False
    meter = FpsMeter(interval)
    sink = emit or (lambda msg: print(msg, flush=True))

    def _probe(_pad, _info):
        msg = meter.tick()
        if msg:
            sink(msg)
        return Gst.PadProbeReturn.OK

    pad.add_probe(Gst.PadProbeType.BUFFER, _probe)
    return True


def serve_pipeline(
    description: str,
    stop: threading.Event,
    on_playing: Optional[Callable] = None,
    latency_ms: int = 100,
    emit: Optional[Callable[[str], None]] = None,
    note: Optional[Callable[[str], None]] = None,
) -> int:
    """Run `description` until `stop` is set, EOS, or an error.

    GStreamer is only touched on this thread. A signal handler must set
    `stop` and return; calling set_state from the handler is what produced
    `gst_poll_set_flushing` and a KeyboardInterrupt traceback.
    """
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    note = note or (lambda msg: print(msg, file=sys.stderr, flush=True))
    Gst.init(None)
    try:
        pipeline = Gst.parse_launch(description)
    except Exception as exc:  # noqa: BLE001 — GLib may raise GError
        note(f"parse_launch failed: {exc}")
        return 1
    pipeline.set_latency(max(0, latency_ms) * Gst.MSECOND)
    attach_stream_fps_probe(pipeline, emit=emit)
    pipeline.set_state(Gst.State.PLAYING)
    # Start the appsrc feeder before waiting out preroll. A non-live wait
    # would deadlock on the first buffer, which the feeder is what provides.
    if on_playing is not None:
        on_playing(pipeline)
    pipeline.get_state(2 * Gst.SECOND)

    bus = pipeline.get_bus()
    code = 0
    try:
        while not stop.is_set():
            msg = bus.timed_pop_filtered(
                100 * Gst.MSECOND,
                Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.WARNING,
            )
            if msg is None:
                continue
            if msg.type == Gst.MessageType.WARNING:
                warn, _debug = msg.parse_warning()
                note(warn.message)
                continue
            if msg.type == Gst.MessageType.ERROR:
                err, _debug = msg.parse_error()
                note(err.message)
                code = 1
                break
            break
    finally:
        # One teardown, on this thread, after the bus poll has returned.
        pipeline.set_state(Gst.State.NULL)
        pipeline.get_state(2 * Gst.SECOND)
    return code


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
    try:
        latency_ms = max(0, int(os.environ.get("SEGUNDA_TELA_LATENCY_MS", "100")))
    except ValueError:
        latency_ms = 100

    stop = threading.Event()

    def _stop(*_args: object) -> None:
        # Flag only. serve_pipeline tears GStreamer down on this thread.
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if idr_fd >= 0:
        # The IDR thread needs the pipeline. It starts once PLAYING begins,
        # from on_playing, so force-key-unit is not sent during preroll.
        holder: dict[str, object] = {}

        def on_playing(pipeline) -> None:
            holder["pipeline"] = pipeline
            threading.Thread(target=_idr_loop, args=(pipeline, idr_fd), name="wfd-idr", daemon=True).start()
    else:
        on_playing = None
    try:
        return serve_pipeline(description, stop, on_playing=on_playing, latency_ms=latency_ms)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
