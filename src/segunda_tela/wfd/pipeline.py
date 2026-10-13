"""GStreamer encode + WFD media transport.

The graph is the one gnome-network-displays builds in wfd-media-factory.c:
videoscale → framerate caps → videoconvert → queue(1) → encoder → identity →
h264parse config-interval=-1 → byte-stream caps → queue(500 ms) →
mpegtsmux alignment=7 sink_4113, audio on sink_4352, rtpmp2tpay ssrc=1
perfect-rtptime=false. `rtpbin` is not used.

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
        return (
            f"vah264enc qos=true key-int-max={gop} num-slices=1 "
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
    # encoder, then a 500 ms queue onto PID 0x1100. Caps are channels + rate only.
    src = audio_source or SILENT_AUDIO_SOURCE
    head = f"{src} ! audioresample ! audioconvert"
    queue = f"queue max-size-buffers=100000 max-size-time=500000000 leaky=0 ! mux.sink_{AUDIO_PID}"
    if kind == "lpcm":
        return (
            f"{head} ! audio/x-raw,format=S16LE,rate=48000,channels=2 ! "
            f"capssetter join=false replace=true "
            f'caps="audio/x-lpcm,width=(int)16,rate=(int)48000,channels=(int)2,'
            f'dynamic_range=(int)0,emphasis=(boolean)false,mute=(boolean)false" ! {queue}'
        )
    return f"{head} ! avenc_aac ! audio/mpeg,channels=2,rate=48000 ! {queue}"


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
    # drop-only videorate turns a 60 fps portal node into the negotiated rate.
    # GND has no videorate because its source already negotiates that rate.
    video = (
        f"{capture} ! videoscale qos=true ! videorate drop-only=true ! "
        f"video/x-raw,framerate={fps}/1,width={width},height={height} ! "
        f"videoconvert qos=true ! queue max-size-buffers=1 leaky=0 ! "
        f"{encoder_fragment(encoder, bitrate_kbps, fps, profile, level_idc)} ! "
        f"identity ! h264parse name=wfd-h264parse config-interval=-1 ! "
        f"{codec_caps} ! "
        f"queue max-size-buffers=1000 max-size-time=500000000 ! mux.sink_{VIDEO_PID}"
    )
    transport = (
        f"mpegtsmux name=mux alignment=7 ! "
        f"queue max-size-buffers=1 leaky=0 ! "
        f"rtpmp2tpay pt=33 ssrc=1 perfect-rtptime=false timestamp-offset=0 seqnum-offset=0 ! "
        f"udpsink host={dest_host} port={rtp_port} bind-port={local_rtp_port} sync=true async=false"
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
            writer = self._idr_write
            if writer is None:
                self._idr_pending = True
                log.info("IDR requested; next regular keyframe is within 1 s (GOP = %s)", self.fps)
                return False
            try:
                writer.write("idr\n")
                writer.flush()
            except OSError as exc:
                log.info("IDR request failed (%s); next regular keyframe is within 1 s", exc)
                return False
        log.info("IDR request: forced a keyframe")
        return True

    def start(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return
            if gst_bindings_available() and self._spawn(self._runner_cmd()):
                log.info("Streaming %sx%s@%s via %s (%s kbps, %s) → %s:%s RTP/MP2T",
                         self.width, self.height, self.fps, self.encoder, self.bitrate_kbps,
                         self.profile, self.dest_host, self.dest_port)
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
        log.debug("Pipeline: %s", self.description)
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if self.source.stdin_feeder else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                pass_fds=tuple(self.source.pass_fds) + extra,
                env=env,
            )
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
            if line:
                log.warning("gst: %s", line)
        code = proc.wait()
        if code not in (0, -15):
            log.error("gst-launch exited with %s", code)

    def running(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)

    def stop(self) -> None:
        with self._lock:
            if not self._proc:
                return
            log.info("Stopping stream")
            if self.source.feeder_stop:
                self.source.feeder_stop()
            if self._idr_write is not None:
                try:
                    self._idr_write.close()
                except OSError:
                    pass
                self._idr_write = None
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
