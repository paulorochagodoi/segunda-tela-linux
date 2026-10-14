"""GStreamer encode + WFD media transport.

The graph follows gnome-network-displays (wfd-media-factory.c):
videorate → scale/NV12 → queue(1, leaky) → encoder → identity →
h264parse config-interval=-1 → byte-stream caps → small queue →
mpegtsmux alignment=7 latency=0 sink_4113, audio on sink_4352, rtpmp2tpay
ssrc=1 perfect-rtptime=false, udpsink sync=false. `rtpbin` is not used.

EVDI has no dma-buf. When `vapostproc` is installed it uploads, scales and
converts to NV12 on the GPU for `vah264enc`. Otherwise `videoscale` and
`videoconvert` run with a few threads and the encoder receives NV12 (I420
for openh264, which only accepts that).

`pipewiresrc keepalive-time>0` unrefs a NULL last-buffer on the first frame
(gstpipewiresrc, PipeWire commit 6c25924). The capture fragment may set that
property; it is stripped here so the critical does not fire. videorate is
only `drop-only` (60 fps portal node → the negotiated rate) and does not use
`skip-to-first`, which duplicates buffers across a bad segment.

Encoder preference (first available wins):
  vah264enc (GStreamer `va` plugin, AMD/Intel)  →  vaapih264enc (gstreamer1.0-vaapi)
  →  x264enc (zerolatency)  →  openh264enc
"""
from __future__ import annotations

import fcntl
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Optional, TextIO

log = logging.getLogger(__name__)

VIDEO_PID = 0x1011
AUDIO_PID = 0x1100
ENCODER_ORDER = ("vah264enc", "vaapih264enc", "x264enc", "openh264enc")
HW_ENCODERS = ("vah264enc", "vaapih264enc")
# mpegtsmux defaults (90 kHz): PAT/PMT 100 ms, PCR 40 ms. GND sets only alignment.
PAT_INTERVAL_TICKS = 9000
PMT_INTERVAL_TICKS = 9000
PCR_INTERVAL_TICKS = 3600
SILENT_AUDIO_SOURCE = "audiotestsrc is-live=true wave=silence"
# pipewiresrc unrefs NULL when this is > 0. See module docstring.
_KEEPALIVE_TIME = re.compile(r"\s*keepalive-time=\d+")


@dataclass
class CaptureSource:
    """GStreamer source fragment + whatever the child process needs to run it."""

    gst: str
    backend: str = "testsrc"
    pass_fds: tuple[int, ...] = ()
    stdin_feeder: Optional[Callable] = None  # called with the child's stdin
    feeder_stop: Optional[Callable[[], None]] = None
    notes: list[str] = field(default_factory=list)
    # EVDI: push into appsrc in this process. `pipe_gst` is the fdsrc graph
    # used only when the in-process pipeline cannot start.
    appsrc_pusher: Optional[Callable] = None
    pipe_gst: Optional[str] = None


def pattern_source(width: int, height: int, fps: int) -> CaptureSource:
    return CaptureSource(
        gst=(
            f"videotestsrc is-live=true pattern=smpte ! "
            f"video/x-raw,width={width},height={height},framerate={fps}/1"
        ),
        backend="testsrc",
    )


@lru_cache(maxsize=None)
def gst_element_available(name: str) -> bool:
    if not shutil.which("gst-inspect-1.0"):
        return False
    try:
        return (
            subprocess.run(
                ["gst-inspect-1.0", "--exists", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def pick_encoder(preferred: str = "auto", available: Callable[[str], bool] = gst_element_available) -> str:
    if preferred and preferred != "auto":
        return preferred
    for name in ENCODER_ORDER:
        if available(name):
            return name
    return "x264enc"


def _level_option(level_idc: Optional[int]) -> str:
    """x264 `level=` so the SPS level_idc is exactly the negotiated level."""
    if not level_idc:
        return ""
    major, minor = divmod(int(level_idc), 10)
    return f' option-string="level={major}.{minor}"'


def encoder_fragment(
    encoder: str,
    bitrate_kbps: int,
    fps: int,
    profile: str = "constrained-baseline",
    level_idc: Optional[int] = None,
) -> str:
    """Property values from wfd_configure_media_element.

    GOP is one second (`key-int-max` / `gop-size` = refresh rate). CBP is
    cabac off and dct8x8 off. x264's `pass=4` in that source is the enum
    value of constant-quantizer on GStreamer 1.x (`pass=quant`); the comment
    there says CBR, but the integer it sets is quant. `level_idc` is an
    extra x264 option so the SPS matches the sink level exactly — GND itself
    never sets a level.
    """
    gop = max(1, fps)
    high = profile == "high"
    cabac = "true" if high else "false"
    dct = "true" if high else "false"
    if encoder == "vah264enc":
        # target-usage 7 is the fastest step (1..7). CBR, no B frames, one
        # reference, GOP = one second. qos off so the encoder does not add
        # its own latency wait on top of the pipeline latency.
        return (
            f"vah264enc qos=false rate-control=cbr target-usage=7 "
            f"key-int-max={gop} b-frames=0 ref-frames=1 num-slices=1 "
            f"cabac={cabac} dct8x8={dct} bitrate={bitrate_kbps}"
        )
    if encoder == "vaapih264enc":
        return (
            f"vaapipostproc ! vaapih264enc qos=true rate-control=2 keyframe-period={gop} "
            f"max-bframes=0 refs=1 num-slices=1 cabac={cabac} dct8x8={dct} "
            f"compliance-mode=0 bitrate={bitrate_kbps}"
        )
    if encoder == "openh264enc":
        bits = bitrate_kbps * 1000
        return (
            f"openh264enc qos=true multi-thread=1 usage-type=screen slice-mode=n-slices "
            f"num-slices=1 rate-control=bitrate gop-size={gop} enable-frame-skip=false "
            f"scene-change-detection=true background-detection=true complexity=low "
            f"bitrate={bits} max-bitrate={bits}"
        )
    return (
        f"x264enc qos=true pass=quant tune=zerolatency speed-preset=ultrafast "
        f"rc-lookahead=0 threads=1 vbv-buf-capacity=50 dct8x8={dct} ref=1 cabac={cabac} "
        f"sync-lookahead=0 b-adapt=false bframes=0 key-int-max={gop} interlaced=false "
        f"bitrate={bitrate_kbps} insert-vui=true sliced-threads=false"
        + _level_option(level_idc)
    )


def default_bitrate_kbps(width: int, height: int, fps: int) -> int:
    """~0.1 bit/pixel/frame for desktop content, clamped for Wi-Fi Direct."""
    return max(4000, min(20000, int(width * height * fps * 0.1 / 1000)))


def _audio_kind(audio: Optional[str]) -> Optional[str]:
    if not audio:
        return None
    name = audio.split()[0].upper()
    if name in ("AAC", "A"):
        return "aac"
    if name == "LPCM":
        return "lpcm"
    if name in ("aac", "lpcm"):
        return name
    return None


def _audio_branch(kind: str, audio_source: Optional[str]) -> str:
    # Order matches wfd_media_factory_create_audio_element: resample, convert,
    # encoder, then a short queue onto PID 0x1100. Caps are channels + rate only.
    src = audio_source or SILENT_AUDIO_SOURCE
    head = f"{src} ! audioresample ! audioconvert"
    # Short, non-leaky: dropping AAC mid-stream makes the sink declare the
    # audio elementary stream broken. 80 ms is four AAC frames.
    queue = (
        f"queue max-size-buffers=4 max-size-bytes=0 max-size-time=80000000 leaky=0 "
        f"! mux.sink_{AUDIO_PID}"
    )
    if kind == "lpcm":
        return (
            f"{head} ! audio/x-raw,format=S16LE,rate=48000,channels=2 ! "
            f"capssetter join=false replace=true "
            f'caps="audio/x-lpcm,width=(int)16,rate=(int)48000,channels=(int)2,'
            f'dynamic_range=(int)0,emphasis=(boolean)false,mute=(boolean)false" ! {queue}'
        )
    return f"{head} ! avenc_aac ! audio/mpeg,channels=2,rate=48000 ! {queue}"


def convert_threads() -> int:
    return max(1, min(4, os.cpu_count() or 1))


def adapt_fragment(
    encoder: str,
    width: int,
    height: int,
    fps: int,
    available: Callable[[str], bool] = gst_element_available,
) -> str:
    """Drop to `fps` first, then scale and convert.

    Scaling every grabbed frame and converting afterwards was the expensive
    order (60 Hz 1920x1200 → 30 Hz 720p). `vapostproc` does both on the GPU
    and leaves NV12 in VA memory for vah264enc. There is no EVDI dma-buf to
    import, so this upload is the zero-copy step we can actually do.
    """
    threads = convert_threads()
    rate = f"videorate drop-only=true qos=false ! video/x-raw,framerate={fps}/1"
    if encoder == "vah264enc" and available("vapostproc"):
        return (
            f"{rate} ! vapostproc ! "
            f"video/x-raw,format=NV12,width={width},height={height}"
        )
    if encoder == "vaapih264enc" and available("vaapipostproc"):
        # The encoder fragment already starts with vaapipostproc.
        return (
            f"{rate} ! videoscale n-threads={threads} qos=false ! "
            f"video/x-raw,width={width},height={height}"
        )
    # openh264enc accepts only I420. NV12 is what VA encoders want; x264 takes both.
    pixel = "I420" if encoder == "openh264enc" else "NV12"
    return (
        f"{rate} ! videoscale n-threads={threads} qos=false ! "
        f"video/x-raw,width={width},height={height} ! "
        f"videoconvert n-threads={threads} qos=false ! video/x-raw,format={pixel}"
    )


def sanitize_capture_gst(gst: str) -> str:
    """Drop pipewiresrc keepalive-time so the first buffer is not unref'd as NULL."""
    return _KEEPALIVE_TIME.sub("", gst)


def build_pipeline(
    source: CaptureSource,
    dest_host: str,
    rtp_port: int,
    width: int,
    height: int,
    fps: int,
    bitrate_kbps: int,
    encoder: str,
    rtcp_port: int = 0,
    local_rtp_port: int = 19100,
    audio_source: Optional[str] = None,
    audio: Optional[str] = None,
    profile: str = "constrained-baseline",
    level_idc: Optional[int] = None,
) -> str:
    """gst-launch description: capture → H.264 → MPEG-TS → RTP/UDP port 19000.

    `rtcp_port` is accepted for callers that still track the SETUP range and
    is ignored: RTP only, no rtpbin. `audio` is an M4 descriptor (`AAC …` /
    `LPCM …`) or the kind name; with no `audio_source` the branch is silence.
    """
    del rtcp_port  # RTP only — see module docstring
    if encoder == "openh264enc":
        profile = "constrained-baseline"
    if profile == "high":
        codec_caps = "video/x-h264,stream-format=byte-stream,profile=high"
    else:
        # Same two profiles gnome-network-displays puts on one capsfilter.
        # A set avoids a semicolon, which gst-launch would treat as a new element.
        codec_caps = "video/x-h264,stream-format=byte-stream,profile=(string){constrained-baseline,baseline}"
    capture = sanitize_capture_gst(source.gst)
    # Raw frames may be dropped (leaky=downstream). Encoded frames may not:
    # a missing P-frame is garbage until the next IDR.
    video = (
        f"{capture} ! {adapt_fragment(encoder, width, height, fps)} ! "
        f"queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
        f"{encoder_fragment(encoder, bitrate_kbps, fps, profile, level_idc)} ! "
        f"identity ! h264parse name=wfd-h264parse config-interval=-1 ! "
        f"{codec_caps} ! "
        f"queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=0 ! mux.sink_{VIDEO_PID}"
    )
    transport = (
        f"mpegtsmux name=mux alignment=7 latency=0 ! "
        f"queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=0 ! "
        f"rtpmp2tpay pt=33 ssrc=1 perfect-rtptime=false timestamp-offset=0 seqnum-offset=0 ! "
        f"udpsink host={dest_host} port={rtp_port} bind-port={local_rtp_port} sync=false async=false"
    )
    parts = [video, transport]
    kind = _audio_kind(audio) or (_audio_kind("aac") if audio_source else None)
    if kind:
        parts.append(_audio_branch(kind, audio_source))
    return " ".join(parts)


def gst_bindings_available() -> bool:
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst  # noqa: F401
        return True
    except (ImportError, ValueError):
        return False


class StreamPipeline:
    def __init__(
        self,
        capture_source,
        dest_host: str,
        dest_port: int,
        width: int = 1920,
        height: int = 1080,
        bitrate_kbps: int = 8000,
        fps: int = 30,
        rtcp_port: int = 0,
        local_rtp_port: int = 19100,
        encoder: str = "auto",
        audio_source: Optional[str] = None,
        audio: Optional[str] = None,
        profile: str = "constrained-baseline",
        level_idc: Optional[int] = None,
    ) -> None:
        if isinstance(capture_source, str):
            capture_source = CaptureSource(gst=capture_source, backend="custom")
        self.source: CaptureSource = capture_source
        self.dest_host = dest_host
        self.dest_port = dest_port
        self.rtcp_port = rtcp_port
        self.local_rtp_port = local_rtp_port
        self.width = width
        self.height = height
        self.bitrate_kbps = bitrate_kbps
        self.fps = fps
        self.encoder = pick_encoder(encoder)
        self.audio_source = audio_source
        self.audio = audio
        self.profile = profile
        self.level_idc = level_idc
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._feeder_thread: Optional[threading.Thread] = None
        self._log_thread: Optional[threading.Thread] = None
        self._idr_write: Optional[TextIO] = None
        self._idr_pending = False
        self._gst_thread: Optional[threading.Thread] = None
        self._gst_stop: Optional[threading.Event] = None
        self._gst_ready = threading.Event()
        self._gst_ok = False
        self._pipeline_ref = None

    @property
    def description(self) -> str:
        return build_pipeline(
            self.source, self.dest_host, self.dest_port, self.width, self.height, self.fps,
            self.bitrate_kbps, self.encoder, self.rtcp_port, self.local_rtp_port, self.audio_source,
            audio=self.audio, profile=self.profile, level_idc=self.level_idc,
        )

    def _build_cmd(self) -> list[str]:
        return ["gst-launch-1.0", "-e", "-q"] + shlex.split(self.description)

    def request_idr(self) -> bool:
        """Ask the encoder for an IDR now (WFD `wfd_idr_request`).

        The gst-launch fallback cannot be signalled; the GOP is one second so
        an IDR still arrives on its own. The runner child (`gst_run`) accepts
        a line on the IDR pipe and posts a force-key-unit event.
        """
        with self._lock:
            pipeline = self._pipeline_ref
            writer = self._idr_write
            if pipeline is None and writer is None:
                self._idr_pending = True
                log.info("IDR requested; next regular keyframe is within 1 s (GOP = %s)", self.fps)
                return False
            if writer is not None:
                try:
                    writer.write("idr\n")
                    writer.flush()
                except OSError as exc:
                    log.info("IDR request failed (%s); next regular keyframe is within 1 s", exc)
                    return False
        if pipeline is not None:
            from .gst_run import force_key_unit

            ok = bool(force_key_unit(pipeline))
            if ok:
                log.info("IDR request: forced a keyframe")
            return ok
        log.info("IDR request: forced a keyframe")
        return True

    def start(self) -> None:
        with self._lock:
            if self.running():
                return
            if self.source.appsrc_pusher is not None and gst_bindings_available():
                if self._start_inprocess():
                    log.info("Streaming %sx%s@%s via %s (%s kbps, %s, latency %sms, appsrc) → %s:%s RTP/MP2T",
                             self.width, self.height, self.fps, self.encoder, self.bitrate_kbps,
                             self.profile, self._latency_ms(), self.dest_host, self.dest_port)
                    return
                log.warning("in-process appsrc did not start; using the frame pipe")
                if self._gst_stop is not None:
                    self._gst_stop.set()
                if self._gst_thread is not None:
                    self._gst_thread.join(timeout=2)
                    self._gst_thread = None
            if self.source.pipe_gst:
                self.source.gst = self.source.pipe_gst
            if gst_bindings_available() and self._spawn(self._runner_cmd()):
                log.info("Streaming %sx%s@%s via %s (%s kbps, %s, latency %sms) → %s:%s RTP/MP2T",
                         self.width, self.height, self.fps, self.encoder, self.bitrate_kbps,
                         self.profile, self._latency_ms(), self.dest_host, self.dest_port)
                return
            if not shutil.which("gst-launch-1.0"):
                raise RuntimeError(
                    "GStreamer not found. Install: sudo apt install gstreamer1.0-tools "
                    "gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad "
                    "gstreamer1.0-plugins-ugly gstreamer1.0-libav"
                )
            log.info("Streaming %sx%s@%s via %s (%s kbps, %s) → %s:%s RTP/MP2T (gst-launch, IDR on GOP only)",
                     self.width, self.height, self.fps, self.encoder, self.bitrate_kbps,
                     self.profile, self.dest_host, self.dest_port)
            if not self._spawn(self._build_cmd()):
                raise RuntimeError("failed to start GStreamer")

    def _start_inprocess(self) -> bool:
        """Run the pipeline on a thread in this process and push EVDI via appsrc.

        Must be called with self._lock held. The GStreamer thread never takes
        that lock. Waiting for PLAYING here is safe.
        """
        self._gst_stop = threading.Event()
        self._gst_ready.clear()
        self._gst_ok = False
        self._pipeline_ref = None
        self._gst_thread = threading.Thread(target=self._gst_main, name="wfd-gst", daemon=True)
        self._gst_thread.start()
        self._gst_ready.wait(timeout=5)
        return self._gst_ok

    def _gst_main(self) -> None:
        from .gst_run import serve_pipeline

        def on_playing(pipeline) -> None:
            self._pipeline_ref = pipeline
            appsrc = pipeline.get_by_name("evdi-src")
            if appsrc is None or self.source.appsrc_pusher is None:
                log.error("evdi-src appsrc missing from the pipeline")
                if self._gst_stop is not None:
                    self._gst_stop.set()
                return
            self._feeder_thread = threading.Thread(
                target=self.source.appsrc_pusher, args=(appsrc,), name="evdi-appsrc", daemon=True
            )
            self._feeder_thread.start()
            self._gst_ok = True
            self._gst_ready.set()

        try:
            assert self._gst_stop is not None
            serve_pipeline(
                self.description, self._gst_stop, on_playing=on_playing, latency_ms=self._latency_ms(),
                emit=lambda msg: log.info("gst: %s", msg),
                note=lambda msg: log.warning("gst: %s", msg),
            )
        except Exception as exc:  # noqa: BLE001
            log.error("in-process pipeline: %s", exc)
        finally:
            self._pipeline_ref = None
            self._gst_ready.set()

    def _latency_ms(self) -> int:
        # openh264 screen mode spikes; VA and x264 ultrafast stay inside 100 ms.
        return 500 if self.encoder == "openh264enc" else 100

    def _runner_cmd(self) -> Optional[list[str]]:
        read_fd, write_fd = os.pipe()
        try:
            cmd = [sys.executable, "-m", "segunda_tela.wfd.gst_run", str(read_fd)]
            self._pending_idr_fds = (read_fd, write_fd)
            return cmd
        except OSError:
            os.close(read_fd)
            os.close(write_fd)
            return None

    def _spawn(self, cmd: Optional[list[str]]) -> bool:
        """Start `cmd`. Returns False if the runner could not be launched.

        Must be called with self._lock held. A runner command carries an IDR
        pipe in `self._pending_idr_fds`; gst-launch does not.
        """
        if not cmd:
            return False
        idr_fds = getattr(self, "_pending_idr_fds", None)
        self._pending_idr_fds = None
        extra: tuple[int, ...] = ()
        write_fd: Optional[int] = None
        if idr_fds:
            read_fd, write_fd = idr_fds
            extra = (read_fd,)
        env = os.environ.copy()
        env["SEGUNDA_TELA_PIPELINE"] = self.description
        # openh264 screen mode used to need 500 ms. VA and x264 ultrafast
        # stay ahead of 100 ms, which is the latency the child applies.
        env["SEGUNDA_TELA_LATENCY_MS"] = str(self._latency_ms())
        log.debug("Pipeline: %s", self.description)
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if self.source.stdin_feeder else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                pass_fds=tuple(self.source.pass_fds) + extra,
                env=env,
                # Ctrl+C goes to this process. The child is stopped with SIGTERM
                # so its poll loop can leave GStreamer on the main thread.
                start_new_session=True,
            )
            if proc.stdin is not None:
                try:
                    # One write of a 1920x1200 frame instead of 64 KiB chunks.
                    fcntl.fcntl(proc.stdin.fileno(), 1031, 8 * 1024 * 1024)  # F_SETPIPE_SZ
                except OSError:
                    pass
        except OSError as exc:
            log.warning("failed to start %s: %s", cmd[0], exc)
            if idr_fds:
                os.close(idr_fds[0])
                os.close(idr_fds[1])
            return False
        if idr_fds:
            os.close(idr_fds[0])
            assert write_fd is not None
            self._idr_write = os.fdopen(write_fd, "w", buffering=1)
            if self._idr_pending:
                try:
                    self._idr_write.write("idr\n")
                    self._idr_write.flush()
                    self._idr_pending = False
                except OSError:
                    pass
        self._proc = proc
        self._log_thread = threading.Thread(target=self._pump_log, args=(proc,), daemon=True)
        self._log_thread.start()
        if self.source.stdin_feeder:
            self._feeder_thread = threading.Thread(
                target=self.source.stdin_feeder, args=(proc.stdin,), name="frame-feeder", daemon=True
            )
            self._feeder_thread.start()
        return True

    @staticmethod
    def _pump_log(proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            if line.startswith("stream fps"):
                log.info("gst: %s", line)
            else:
                log.warning("gst: %s", line)
        code = proc.wait()
        if code not in (0, -15):
            log.error("gst-launch exited with %s", code)

    def running(self) -> bool:
        if self._gst_thread is not None and self._gst_thread.is_alive() and self._gst_ok:
            return True
        return bool(self._proc and self._proc.poll() is None)

    def stop(self) -> None:
        with self._lock:
            thread = self._gst_thread
            proc = self._proc
            if thread is None and proc is None:
                return
            log.info("Stopping stream")
            if self.source.feeder_stop:
                self.source.feeder_stop()
            if self._gst_stop is not None:
                self._gst_stop.set()
            if self._idr_write is not None:
                try:
                    self._idr_write.close()
                except OSError:
                    pass
                self._idr_write = None
            self._gst_thread = None
            self._proc = None
        if thread is not None:
            thread.join(timeout=3)
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
