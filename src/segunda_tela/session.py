"""Orchestrates display (EVDI/portal) + WFD RTSP source + GStreamer (+ USB / P2P link)."""
from __future__ import annotations

import logging
import threading
from typing import Optional

from .display.virtual import VirtualDisplay, create_virtual_display
from .usb.link import UsbLink
from .wfd.pipeline import HW_ENCODERS, StreamPipeline, default_bitrate_kbps, pick_encoder
from .wfd.rtsp import SessionState, WfdRtspServer, local_ip

log = logging.getLogger(__name__)

DEFAULT_VIRTUAL_SIZE = (1920, 1200)
AUDIO_SOURCE = "pulsesrc device=@DEFAULT_MONITOR@ do-timestamp=true"


class SecondScreenSession:
    def __init__(
        self,
        width: Optional[int] = None,
        height: Optional[int] = None,
        fps: Optional[int] = None,
        mode: str = "extend",
        bind_host: str = "0.0.0.0",
        rtsp_port: int = 7236,
        bitrate_kbps: Optional[int] = None,
        advertise_ip: Optional[str] = None,
        usb_link: Optional[UsbLink] = None,
        p2p_link=None,
        capture: str = "auto",
        encoder: str = "auto",
        audio: bool = False,
    ) -> None:
        self.want_size = (width, height) if width and height else None
        self.mode = mode
        self.bind_host = bind_host
        self.rtsp_port = rtsp_port
        self.bitrate_kbps = bitrate_kbps
        self.usb_link = usb_link
        self.p2p_link = p2p_link
        self.capture = capture
        self.encoder = pick_encoder(encoder)
        self.fps = fps or (60 if self.encoder in HW_ENCODERS else 30)
        self.audio = audio
        self.display: Optional[VirtualDisplay] = None
        self.server: Optional[WfdRtspServer] = None
        self.pipeline: Optional[StreamPipeline] = None
        self.last_state: Optional[SessionState] = None
        self._plock = threading.Lock()
        if advertise_ip:
            self.advertise_ip = advertise_ip
        elif p2p_link is not None and p2p_link.local_ip:
            self.advertise_ip = p2p_link.local_ip
        elif usb_link and usb_link.local_ip and usb_link.transport != "adb-reverse":
            self.advertise_ip = usb_link.local_ip
        else:
            self.advertise_ip = local_ip()

    # Back-compat attributes used by older callers/UI.
    @property
    def width(self) -> int:
        return (self.want_size or DEFAULT_VIRTUAL_SIZE)[0]

    @property
    def height(self) -> int:
        return (self.want_size or DEFAULT_VIRTUAL_SIZE)[1]

    @property
    def notes(self) -> list[str]:
        return list(self.display.notes) if self.display else []

    def start(self) -> None:
        """Create the display/capture, then listen for the sink."""
        self.prepare_display()
        self.start_server()

    def attach_p2p(self, link) -> None:
        """P2P group formed after the server started listening (sink connects next)."""
        self.p2p_link = link
        if link is not None and link.local_ip:
            self.advertise_ip = link.local_ip

    def prepare_display(self) -> None:
        if self.usb_link:
            for note in self.usb_link.notes:
                log.info("USB: %s", note)

        vw, vh = self.want_size or DEFAULT_VIRTUAL_SIZE
        self.display = create_virtual_display(vw, vh, self.mode, self.fps, capture=self.capture)
        for note in self.display.notes:
            log.warning("%s", note)
        log.info(
            "Display backend=%s mode=%s %sx%s encoder=%s — WFD sink → rtsp://%s:%s%s",
            self.display.backend, self.mode, vw, vh, self.encoder, self.advertise_ip, self.rtsp_port,
            f" (usb/{self.usb_link.transport})" if self.usb_link else
            (f" (p2p/{self.p2p_link.interface})" if self.p2p_link else ""),
        )

    def start_server(self) -> None:
        vw, vh = self.want_size or DEFAULT_VIRTUAL_SIZE
        self.server = WfdRtspServer(
            host=self.bind_host,
            port=self.rtsp_port,
            advertise_host=self.advertise_ip,
            width=vw,
            height=vh,
            fps=self.fps,
            on_play=self._on_play,
            on_teardown=self._on_teardown,
            want_size=self.want_size,
            max_fps=self.fps,
            audio=self.audio,
        )
        self.server.start_background()

    def _on_play(self, state: SessionState) -> None:
        self.last_state = state
        mode = state.mode
        if self.usb_link and self.usb_link.transport.startswith("adb"):
            log.info("PLAY via adb — RTP/UDP may need USB tethering (adb reverse only carries TCP)")
        assert self.display is not None
        bitrate = self.bitrate_kbps or default_bitrate_kbps(mode.width, mode.height, mode.fps)
        with self._plock:
            if self.pipeline:
                self.pipeline.stop()
            self.pipeline = StreamPipeline(
                capture_source=self.display.new_capture(),
                dest_host=state.remote,
                dest_port=state.rtp_port,
                rtcp_port=state.rtcp_port,
                local_rtp_port=self.server.server_rtp_port if self.server else 19100,
                width=mode.width,
                height=mode.height,
                bitrate_kbps=bitrate,
                fps=mode.fps,
                encoder=self.encoder,
                audio_source=AUDIO_SOURCE if state.negotiated and state.negotiated.audio else None,
            )
            try:
                self.pipeline.start()
            except Exception as exc:  # noqa: BLE001
                log.error("Failed to start pipeline: %s", exc)

    def _on_teardown(self, state: SessionState) -> None:
        log.info("TEARDOWN/PAUSE from %s", state.remote)
        with self._plock:
            if self.pipeline:
                self.pipeline.stop()
                self.pipeline = None

    def stop(self) -> None:
        with self._plock:
            if self.pipeline:
                self.pipeline.stop()
                self.pipeline = None
        if self.server:
            self.server.stop()
            self.server = None
        if self.display:
            self.display.close()
            self.display = None
        if self.usb_link:
            self.usb_link.close()
            self.usb_link = None
        if self.p2p_link:
            self.p2p_link.close()
            self.p2p_link = None
