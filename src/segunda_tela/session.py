"""Orchestrates virtual display + RTSP + GStreamer."""
from __future__ import annotations

import logging
from typing import Optional

from .display.virtual import VirtualDisplay, create_virtual_display
from .wfd.pipeline import StreamPipeline
from .wfd.rtsp import SessionState, WfdRtspServer, local_ip

log = logging.getLogger(__name__)


class SecondScreenSession:
    def __init__(
        self,
        width: int = 1920,
        height: int = 1080,
        fps: int = 30,
        mode: str = "extend",
        bind_host: str = "0.0.0.0",
        rtsp_port: int = 7236,
        bitrate_kbps: int = 8000,
    ) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.mode = mode
        self.bind_host = bind_host
        self.rtsp_port = rtsp_port
        self.bitrate_kbps = bitrate_kbps
        self.display: Optional[VirtualDisplay] = None
        self.server: Optional[WfdRtspServer] = None
        self.pipeline: Optional[StreamPipeline] = None
        self.advertise_ip = local_ip()

    def start(self) -> None:
        self.display = create_virtual_display(self.width, self.height, self.mode, self.fps)
        log.info(
            "Display backend=%s mode=%s %sx%s — connect a Miracast/WFD sink to rtsp://%s:%s",
            self.display.backend,
            self.display.mode,
            self.width,
            self.height,
            self.advertise_ip,
            self.rtsp_port,
        )

        def on_play(state: SessionState) -> None:
            log.info("PLAY from %s → RTP %s:%s", state.remote, state.remote, state.rtp_port)
            if self.pipeline:
                self.pipeline.stop()
            assert self.display is not None
            self.pipeline = StreamPipeline(
                capture_source=self.display.capture_source,
                dest_host=state.remote,
                dest_port=state.rtp_port,
                width=self.width,
                height=self.height,
                bitrate_kbps=self.bitrate_kbps,
                fps=self.fps,
            )
            try:
                self.pipeline.start()
            except Exception as exc:  # noqa: BLE001
                log.error("Failed to start pipeline: %s", exc)

        def on_teardown(state: SessionState) -> None:
            log.info("TEARDOWN from %s", state.remote)
            if self.pipeline:
                self.pipeline.stop()
                self.pipeline = None

        self.server = WfdRtspServer(
            host=self.bind_host,
            port=self.rtsp_port,
            advertise_host=self.advertise_ip,
            width=self.width,
            height=self.height,
            fps=self.fps,
            on_play=on_play,
            on_teardown=on_teardown,
        )
        self.server.start_background()

    def stop(self) -> None:
        if self.pipeline:
            self.pipeline.stop()
            self.pipeline = None
        if self.server:
            self.server.stop()
            self.server = None
        if self.display:
            self.display.close()
            self.display = None
