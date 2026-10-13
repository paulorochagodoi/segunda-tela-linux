"""GStreamer encode + WFD media transport.

WFD sinks expect H.264 in an MPEG-2 Transport Stream, payloaded as RTP/MP2T
(payload type 33) — video on PID 0x1011, audio on PID 0x1100 — sent to the
sink's RTP port (19000 on Samsung). This matches gnome-network-displays:
`mpegtsmux alignment=7`, `rtpmp2tpay pt=33 ssrc=1 perfect-rtptime=false`,
SPS/PPS in front of every IDR (`h264parse config-interval=-1`), a fixed GOP,
constant frame rate, and an AAC (or LPCM) elementary stream.

`rtpbin` is intentionally absent. Linking it from gst-launch
(`rtpbin.send_rtp_sink_0` + `recv_rtcp_sink`) hits
`gst_mini_object_unref: assertion 'mini_object != NULL' failed` in
`gst_rtp_session_send_rtp` when the send pad is not ready, and the tablet
then tears the session down. RTCP is not required: the Tab S10 FE reports
RTCP port 0.

Encoder preference (first available wins):
  vah264enc (GStreamer `va` plugin, AMD/Intel)  →  vaapih264enc (gstreamer1.0-vaapi)
  →  x264enc (zerolatency)  →  openh264enc
"""
from __future__ import annotations

import logging
import os
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
# 90 kHz MPEG clock. PAT/PMT every 100 ms, PCR every 40 ms (ISO/IEC 13818-1
# requires a PCR at least every 100 ms; 40 ms is mpegtsmux's own default).
PAT_INTERVAL_TICKS = 9000
PMT_INTERVAL_TICKS = 9000
PCR_INTERVAL_TICKS = 3600
SILENT_AUDIO_SOURCE = "audiotestsrc is-live=true wave=silence do-timestamp=true"


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


def _level_token(level_idc: Optional[int]) -> str:
    if not level_idc:
        return ""
    major, minor = divmod(int(level_idc), 10)
    return f":level={major}.{minor}"


def encoder_fragment(
    encoder: str,
    bitrate_kbps: int,
    fps: int,
    profile: str = "constrained-baseline",
    level_idc: Optional[int] = None,
) -> str:
    """One IDR per second (`key-int-max` = fps), no B-frames.

    CBP (the WFD mandatory profile) is cabac off and 8x8dct off. CHP is the
    high profile with the same no-B-frame constraint. x264 also repeats
    SPS/PPS on every IDR (`repeat-headers`) and disables scene-cut so the
    GOP stays regular; `h264parse config-interval=-1` does the same again
    downstream, which is what gnome-network-displays relies on.
    """
    gop = max(1, fps)
    high = profile == "high"
    cabac = "true" if high else "false"
    dct = "true" if high else "false"
    if encoder == "vah264enc":
        return (
            f"vah264enc rate-control=cbr bitrate={bitrate_kbps} key-int-max={gop} "
            f"b-frames=0 ref-frames=1 cabac={cabac} dct8x8={dct} num-slices=1"
        )
    if encoder == "vaapih264enc":
        return (
            f"vaapih264enc rate-control=cbr bitrate={bitrate_kbps} keyframe-period={gop} "
            f"max-bframes=0 refs=1 cabac={cabac} dct8x8={dct} num-slices=1"
        )
    if encoder == "openh264enc":
        # openh264 is constrained-baseline only.
        return (
            f"openh264enc bitrate={bitrate_kbps * 1000} gop-size={gop} "
            f"usage-type=screen rate-control=bitrate enable-frame-skip=false"
        )
    level = _level_token(level_idc)
    return (
        f"x264enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate_kbps} "
        f"key-int-max={gop} bframes=0 byte-stream=true cabac={cabac} dct8x8={dct} ref=1 "
        f"insert-vui=true aud=true option-string=\"scenecut=0:repeat-headers=1{level}\""
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
    src = audio_source or SILENT_AUDIO_SOURCE
    head = (
        f"{src} ! queue max-size-time=500000000 leaky=0 ! audioconvert ! audioresample ! "
        f"audio/x-raw,rate=48000,channels=2"
    )
    if kind == "lpcm":
        # mpegtsmux accepts audio/x-lpcm (Blu-ray private stream). capssetter
        # relabels S16LE; join=false is required or it keeps audio/x-raw.
        return (
            f"{head},format=S16LE ! capssetter join=false replace=true "
            f'caps="audio/x-lpcm,width=(int)16,rate=(int)48000,channels=(int)2,'
            f'dynamic_range=(int)0,emphasis=(boolean)false,mute=(boolean)false" '
            f"! queue ! mux.sink_{AUDIO_PID}"
        )
    # ADTS so mpegtsmux does not require a separate codec_data buffer.
    return (
        f"{head} ! avenc_aac bitrate=128000 ! aacparse ! "
        f"audio/mpeg,mpegversion=4,stream-format=adts,framed=true,rate=48000,channels=2 ! "
        f"queue ! mux.sink_{AUDIO_PID}"
    )


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
    video = (
        f"{source.gst} ! queue leaky=downstream max-size-buffers=2 ! "
        f"videoconvert ! videoscale ! videorate drop-only=false skip-to-first=true ! "
        f"video/x-raw,format=NV12,width={width},height={height},framerate={fps}/1,pixel-aspect-ratio=1/1 ! "
        f"{encoder_fragment(encoder, bitrate_kbps, fps, profile, level_idc)} ! "
        f"h264parse name=wfd-h264parse config-interval=-1 ! "
        f"video/x-h264,profile={profile},stream-format=byte-stream,alignment=au ! "
        f"queue max-size-buffers=4 max-size-time=500000000 ! mux.sink_{VIDEO_PID}"
    )
    transport = (
        f"mpegtsmux name=mux alignment=7 pat-interval={PAT_INTERVAL_TICKS} "
        f"pmt-interval={PMT_INTERVAL_TICKS} pcr-interval={PCR_INTERVAL_TICKS} ! "
        f"queue max-size-buffers=1 leaky=0 ! "
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
