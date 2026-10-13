"""Orchestrates virtual display + RTSP + GStreamer (+ optional USB link)."""
from __future__ import annotations

import logging
from typing import Optional

from .display.virtual import VirtualDisplay, create_virtual_display
from .usb.link import UsbLink
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
        advertise_ip: Optional[str] = None,
        usb_link: Optional[UsbLink] = None,
    ) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.mode = mode
        self.bind_host = bind_host
        self.rtsp_port = rtsp_port
        self.bitrate_kbps = bitrate_kbps
        self.usb_link = usb_link
        self.display: Optional[VirtualDisplay] = None
        self.server: Optional[WfdRtspServer] = None
        self.pipeline: Optional[StreamPipeline] = None
        if advertise_ip:
            self.advertise_ip = advertise_ip
        elif usb_link and usb_link.local_ip and usb_link.transport != "adb-reverse":
            self.advertise_ip = usb_link.local_ip
        else:
            self.advertise_ip = local_ip()

    def start(self) -> None:
        if self.usb_link:
            for note in self.usb_link.notes:
                log.info("USB: %s", note)

        self.display = create_virtual_display(self.width, self.height, self.mode, self.fps)
        log.info(
            "Display backend=%s mode=%s %sx%s — WFD sink → rtsp://%s:%s%s",
            self.display.backend,
            self.display.mode,
            self.width,
            self.height,
            self.advertise_ip,
            self.rtsp_port,
            f" (usb/{self.usb_link.transport})" if self.usb_link else "",
        )

        def on_play(state: SessionState) -> None:
            dest = state.remote
            # Over adb reverse the sink may report 127.0.0.1 from tablet side;
            # RTP still needs a reachable host from the PC. Prefer USB peer.
            if self.usb_link and self.usb_link.transport.startswith("adb"):
                log.info(
                    "PLAY via adb — RTP to reported %s:%s (adb reverse handles RTSP; "
                    "RTP/UDP may need adb forward or tether for media)",
                    dest,
                    state.rtp_port,
                )
            log.info("PLAY from %s → RTP %s:%s", state.remote, dest, state.rtp_port)
            if self.pipeline:
                self.pipeline.stop()
            assert self.display is not None
            self.pipeline = StreamPipeline(
                capture_source=self.display.capture_source,
                dest_host=dest,
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
        if self.usb_link:
            self.usb_link.close()
            self.usb_link = None
