"""GStreamer encode + WFD media transport (gst-launch-1.0 subprocess).

WFD sinks expect H.264 in an MPEG-2 Transport Stream, payloaded as RTP/MP2T
(payload type 33) — video on PID 0x1011, audio on PID 0x1100. Plain
`rtph264pay` (what v0.2 sent) is not decodable by Miracast sinks.

Encoder preference (first available wins):
  vah264enc (GStreamer `va` plugin, AMD/Intel)  →  vaapih264enc (gstreamer1.0-vaapi)
  →  x264enc (zerolatency)  →  openh264enc
"""
from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Optional

log = logging.getLogger(__name__)

VIDEO_PID = 0x1011
AUDIO_PID = 0x1100
ENCODER_ORDER = ("vah264enc", "vaapih264enc", "x264enc", "openh264enc")
HW_ENCODERS = ("vah264enc", "vaapih264enc")


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


def encoder_fragment(encoder: str, bitrate_kbps: int, fps: int) -> str:
    gop = max(1, fps)  # 1 s GOP: we cannot inject IDRs into gst-launch on demand
    if encoder == "vah264enc":
        return (
            f"vah264enc rate-control=cbr bitrate={bitrate_kbps} key-int-max={gop} "
            f"b-frames=0 ref-frames=1 cabac=false dct8x8=false num-slices=1"
        )
    if encoder == "vaapih264enc":
        return (
            f"vaapih264enc rate-control=cbr bitrate={bitrate_kbps} keyframe-period={gop} "
            f"max-bframes=0 refs=1 cabac=false dct8x8=false num-slices=1"
        )
    if encoder == "openh264enc":
        return f"openh264enc bitrate={bitrate_kbps * 1000} gop-size={gop} usage-type=screen rate-control=bitrate"
    return (
        f"x264enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate_kbps} "
        f"key-int-max={gop} bframes=0 byte-stream=true"
    )


def default_bitrate_kbps(width: int, height: int, fps: int) -> int:
    """~0.1 bit/pixel/frame for desktop content, clamped for Wi-Fi Direct."""
    return max(4000, min(20000, int(width * height * fps * 0.1 / 1000)))


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
) -> str:
    """gst-launch description for capture → H.264 → MPEG-TS → RTP/UDP (+RTCP)."""
    video = (
        f"{source.gst} ! queue leaky=downstream max-size-buffers=2 ! "
        f"videoconvert ! videoscale ! videorate ! "
        f"video/x-raw,format=NV12,width={width},height={height},framerate={fps}/1,pixel-aspect-ratio=1/1 ! "
        f"{encoder_fragment(encoder, bitrate_kbps, fps)} ! "
        f"video/x-h264,profile=constrained-baseline,stream-format=byte-stream ! "
        f"h264parse config-interval=-1 ! queue max-size-buffers=4 ! mux.sink_{VIDEO_PID}"
    )
    mux = "mpegtsmux name=mux alignment=7 ! queue max-size-buffers=2 ! rtpmp2tpay pt=33 perfect-rtptime=false"
    if rtcp_port:
        transport = (
            f"{mux} ! rtpbin.send_rtp_sink_0 "
            f"rtpbin name=rtpbin rtpbin.send_rtp_src_0 ! "
            f"udpsink host={dest_host} port={rtp_port} bind-port={local_rtp_port} sync=false async=false "
            f"rtpbin.send_rtcp_src_0 ! udpsink host={dest_host} port={rtcp_port} bind-port={local_rtp_port + 1} "
            f"sync=false async=false "
            f"udpsrc port={local_rtp_port + 1} reuse=true ! rtpbin.recv_rtcp_sink_0"
        )
    else:
        transport = f"{mux} ! udpsink host={dest_host} port={rtp_port} bind-port={local_rtp_port} sync=false async=false"
    parts = [video, transport]
    if audio_source:
        parts.append(
            f"{audio_source} ! queue ! audioconvert ! audioresample ! "
            f"audio/x-raw,rate=48000,channels=2 ! avenc_aac ! aacparse ! "
            f"audio/mpeg,mpegversion=4,stream-format=adts ! queue ! mux.sink_{AUDIO_PID}"
        )
    return " ".join(parts)


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
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._feeder_thread: Optional[threading.Thread] = None
        self._log_thread: Optional[threading.Thread] = None

    @property
    def description(self) -> str:
        return build_pipeline(
            self.source, self.dest_host, self.dest_port, self.width, self.height, self.fps,
            self.bitrate_kbps, self.encoder, self.rtcp_port, self.local_rtp_port, self.audio_source,
        )

    def _build_cmd(self) -> list[str]:
        return ["gst-launch-1.0", "-e", "-q"] + shlex.split(self.description)

    def start(self) -> None:
        if not shutil.which("gst-launch-1.0"):
            raise RuntimeError(
                "GStreamer not found. Install: sudo apt install gstreamer1.0-tools "
                "gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad "
                "gstreamer1.0-plugins-ugly gstreamer1.0-libav"
            )
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return
            cmd = self._build_cmd()
            log.info("Streaming %sx%s@%s via %s (%s kbps) → %s:%s%s",
                     self.width, self.height, self.fps, self.encoder, self.bitrate_kbps,
                     self.dest_host, self.dest_port, f" rtcp:{self.rtcp_port}" if self.rtcp_port else "")
            log.debug("Pipeline: %s", self.description)
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if self.source.stdin_feeder else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                pass_fds=self.source.pass_fds,
            )
            proc = self._proc
            self._log_thread = threading.Thread(target=self._pump_log, args=(proc,), daemon=True)
            self._log_thread.start()
            if self.source.stdin_feeder:
                self._feeder_thread = threading.Thread(
                    target=self.source.stdin_feeder, args=(proc.stdin,), name="frame-feeder", daemon=True
                )
                self._feeder_thread.start()

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
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
