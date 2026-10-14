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
        probe_hdcp: bool = False,
        gnd_compat: bool = False,
        force_1080p30: bool = False,
        touch: bool = True,
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
        self.probe_hdcp = probe_hdcp
        self.gnd_compat = gnd_compat
        self.force_1080p30 = force_1080p30
        self.touch = touch
        self.uibc = None
        self.injector = None
        self._touch_stop = threading.Event()
        self._touch_wait_logged = False
        self._touch_mapped = False
        if force_1080p30:
            self.want_size = (1920, 1080)
        self.display: Optional[VirtualDisplay] = None
        self.server: Optional[WfdRtspServer] = None
        self.pipeline: Optional[StreamPipeline] = None
        self._idr_pending = False
        self.last_state: Optional[SessionState] = None
        self.usb_initiator = None
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
            log.info("P2P group up on %s (%s). NetworkManager runs its own dnsmasq for the tablet's DHCP; "
                     "a dnsmasq CAP_CHOWN warning in the journal is harmless (dnsmasq only fails to drop/"
                     "keep a privilege it does not need here; DHCP still serves the tablet).",
                     link.interface, link.local_ip)

    def start_usb_initiator(self, **kw):
        """USB: dial the tablet's sink (adb/tether) instead of only waiting for it."""
        from .usb.initiate import UsbInitiator

        assert self.server is not None and self.usb_link is not None
        self.usb_initiator = UsbInitiator(self.server, self.usb_link, rtsp_port=self.rtsp_port, **kw)
        self.usb_initiator.start()
        return self.usb_initiator

    def prepare_display(self) -> None:
        if self.usb_link:
            for note in self.usb_link.notes:
                log.info("USB: %s", note)
            # Same display path as Wi-Fi: extend → EVDI connector + capture of that output.
            log.info("USB link %s: display mode=%s (same EVDI/portal path as Wi-Fi)", self.usb_link.transport, self.mode)

        vw, vh = self.want_size or DEFAULT_VIRTUAL_SIZE
        # --resolution 1080 locks the EVDI EDID at 1920x1080p30 so the
        # compositor does not pick 1920x1200 and does not composite at 60 Hz.
        display_fps = 30 if self.force_1080p30 else self.fps
        self.display = create_virtual_display(
            vw, vh, self.mode, display_fps, capture=self.capture, lock_mode=self.force_1080p30,
        )
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
        uibc_port = self._start_uibc()
        self.server = WfdRtspServer(
            host=self.bind_host,
            port=self.rtsp_port,
            advertise_host=self.advertise_ip,
            width=vw,
            height=vh,
            fps=self.fps,
            on_play=self._on_play,
            on_teardown=self._on_teardown,
            on_idr_request=self._on_idr,
            want_size=self.want_size,
            max_fps=self.fps,
            # Always negotiate an audio elementary stream when the sink offers
            # one. `--audio` only switches that track from silence to the desktop.
            audio=True,
            encoder=self.encoder,
            probe_hdcp=self.probe_hdcp,
            gnd_compat=self.gnd_compat,
            force_1080p30=self.force_1080p30,
            touch=self.touch and bool(uibc_port),
            uibc_port=uibc_port,
            uibc=self.uibc,
        )
        self.server.start_background()
        # Open the virtual touchscreen only after COSMIC is told which output
        # it belongs to. An unmapped touchscreen is delivered to the built-in
        # panel, which is the opposite of 'Segunda Tela' only.
        if self.touch and self.injector is not None and not self._arm_touch():
            threading.Thread(target=self._touch_map_loop, name="touch-map", daemon=True).start()

    def _start_uibc(self) -> int:
        if not self.touch:
            log.info("touch: off (--no-touch)")
            return 0
        from .display.touch import TouchInjector
        from .wfd.uibc import UibcServer

        self.injector = TouchInjector()
        self.uibc = UibcServer(self._on_touch)
        port = self.uibc.start()
        if not port:
            log.warning("touch: UIBC listener did not start")
        return port

    def _on_touch(self, event) -> None:
        if self.injector is not None:
            self.injector.handle(event)

    def _ensure_touch_device(self) -> None:
        if self.injector is None:
            return
        width = height = 0
        evdi = self.display.evdi if self.display is not None else None
        mode = getattr(evdi, "mode", None)
        if mode:
            width, height = int(mode[0]), int(mode[1])
        elif self.display is not None:
            width, height = self.display.width, self.display.height
        if width and height:
            self.injector.configure(width, height)

    def _map_touch(self) -> bool:
        """Bind the virtual touchscreen to the EVDI connector in COSMIC."""
        if not self.touch or self.injector is None or self.display is None:
            return False
        from .display.randr import evdi_connector_names, evdi_status, query_layout
        from .display.touch import DEVICE_NAME, write_cosmic_touch_map

        try:
            status = evdi_status(query_layout(), evdi_connector_names())
        except Exception as exc:  # noqa: BLE001
            log.info("touch: layout unavailable (%s)", exc)
            return False
        output = status.output
        if output is None:
            if not self._touch_wait_logged:
                log.info("touch: waiting for the EVDI output before mapping '%s'", DEVICE_NAME)
                self._touch_wait_logged = True
            return False
        try:
            path = write_cosmic_touch_map(output.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("touch: could not write the COSMIC map (%s). Device name is '%s'.", exc, DEVICE_NAME)
            return False
        if not self._touch_mapped:
            log.info(
                "touch: '%s' mapped to %s (%s). Coordinates are this output's pixels; "
                "udev calibration is the identity matrix.",
                DEVICE_NAME, output.name, path,
            )
            self._touch_mapped = True
        if output.width and output.height:
            self.injector.configure(output.width, output.height)
        else:
            self._ensure_touch_device()
        return bool(self.injector.open)

    def _arm_touch(self) -> bool:
        """Write the COSMIC output map, then create the uinput device."""
        return self._map_touch()

    def _touch_map_loop(self) -> None:
        for _ in range(15):
            if self._touch_stop.is_set():
                return
            if self._arm_touch():
                return
            self._touch_stop.wait(2)

    def _on_play(self, state: SessionState) -> None:
        self.last_state = state
        mode = state.mode
        negotiated = state.negotiated
        if self.usb_link and self.usb_link.transport.startswith("adb"):
            log.info("PLAY via adb — RTP/UDP may need USB tethering (adb reverse only carries TCP)")
        assert self.display is not None
        # 720p60 is above H.264 level 3.1. Grab and parse at the negotiated
        # rate (720p30 on the Tab S10 FE) instead of the EDID's 60 Hz.
        self.display.fps = mode.fps
        evdi = self.display.evdi
        if evdi is not None and hasattr(evdi, "fps"):
            evdi.fps = max(1, int(mode.fps))
        if self.injector is not None:
            self.injector.set_stream(mode.width, mode.height)
            if not self.injector.open:
                self._arm_touch()
            else:
                self._ensure_touch_device()
        bitrate = self.bitrate_kbps or (
            negotiated.bitrate_kbps if negotiated else default_bitrate_kbps(mode.width, mode.height, mode.fps)
        )
        with self._plock:
            if self.pipeline:
                self.pipeline.stop()
            self.pipeline = StreamPipeline(
                capture_source=self.display.new_capture(),
                dest_host=state.rtp_host,
                dest_port=state.rtp_port,
                rtcp_port=0,  # RTP only; RTCP/rtpbin trips gst_mini_object_unref
                local_rtp_port=self.server.server_rtp_port if self.server else 19100,
                width=mode.width,
                height=mode.height,
                bitrate_kbps=bitrate,
                fps=mode.fps,
                encoder=self.encoder,
                audio=negotiated.audio if negotiated else None,
                audio_source=AUDIO_SOURCE if self.audio and negotiated and negotiated.audio else None,
                profile=negotiated.gst_profile if negotiated else "constrained-baseline",
                level_idc=negotiated.level_idc if negotiated else None,
            )
            try:
                self.pipeline.start()
                if self._idr_pending:
                    self.pipeline.request_idr()
                    self._idr_pending = False
            except Exception as exc:  # noqa: BLE001
                log.error("Failed to start pipeline: %s", exc)

    def _on_idr(self, state: SessionState) -> None:
        with self._plock:
            if self.pipeline and self.pipeline.request_idr():
                return
            self._idr_pending = True
            log.info("IDR request queued until the stream is playing")

    def _on_teardown(self, state: SessionState) -> None:
        log.info("TEARDOWN/PAUSE from %s", state.remote)
        with self._plock:
            if self.pipeline:
                self.pipeline.stop()
                self.pipeline = None

    def stop(self) -> None:
        self._touch_stop.set()
        if self.uibc is not None:
            self.uibc.stop()
            self.uibc = None
        if self.injector is not None:
            self.injector.close()
            self.injector = None
        if self.usb_initiator is not None:
            self.usb_initiator.stop()
            self.usb_initiator = None
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
