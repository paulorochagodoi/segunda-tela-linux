"""Minimal Wi-Fi Display (Miracast) RTSP source server.

Implements enough of the WFD source role for a sink to negotiate H.264 over RTP.
Based on public Wi-Fi Display specs / open implementations (behavior only).
Not affiliated with Samsung. Not a reimplementation of Samsung's proprietary
RTSP body types (Version/Mcc/HardwareCursor/…).
"""
from __future__ import annotations

import logging
import random
import socket
import socketserver
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)

WFD_DEFAULT_PORT = 7236


@dataclass
class SessionState:
    session_id: str = field(default_factory=lambda: str(random.randint(1, 99999999)))
    url: str = "rtsp://127.0.0.1/wfd1.0"
    rtp_port: int = 19000
    width: int = 1920
    height: int = 1080
    fps: int = 30
    playing: bool = False
    cseq: int = 0
    remote: str = ""


class _Handler(socketserver.StreamRequestHandler):
    server: "WfdRtspServer"  # type: ignore[name-defined]

    def handle(self) -> None:
        peer = self.client_address[0]
        log.info("RTSP client connected from %s", peer)
        state = SessionState(
            width=self.server.width,
            height=self.server.height,
            fps=self.server.fps,
            remote=peer,
            url=f"rtsp://{self.server.advertise_host}/wfd1.0",
        )
        buf = b""
        try:
            while True:
                chunk = self.request.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\r\n\r\n" in buf:
                    header, buf = buf.split(b"\r\n\r\n", 1)
                    text = header.decode("utf-8", "replace")
                    lines = text.split("\r\n")
                    req_line = lines[0]
                    headers = {}
                    for line in lines[1:]:
                        if ":" in line:
                            k, v = line.split(":", 1)
                            headers[k.strip().lower()] = v.strip()
                    content_len = int(headers.get("content-length", "0") or 0)
                    body = b""
                    if content_len:
                        while len(buf) < content_len:
                            more = self.request.recv(4096)
                            if not more:
                                break
                            buf += more
                        body, buf = buf[:content_len], buf[content_len:]
                    self._dispatch(state, req_line, headers, body.decode("utf-8", "replace"))
        finally:
            if state.playing and self.server.on_teardown:
                self.server.on_teardown(state)
            log.info("RTSP client %s disconnected", peer)

    def _reply(self, state: SessionState, code: int, reason: str, extra: Optional[dict] = None, body: str = "") -> None:
        state.cseq += 0  # keep last
        cseq = self._last_cseq
        lines = [f"RTSP/1.0 {code} {reason}", f"CSeq: {cseq}"]
        if extra:
            for k, v in extra.items():
                lines.append(f"{k}: {v}")
        if body:
            lines.append(f"Content-Length: {len(body.encode())}")
            lines.append("Content-Type: text/parameters")
        lines.append("")
        lines.append(body)
        data = "\r\n".join(lines).encode()
        if not body:
            # ensure ending CRLFCRLFstyle when no body — already have trailing empty
            if not data.endswith(b"\r\n\r\n"):
                data = data.rstrip(b"\r\n") + b"\r\n\r\n"
        self.request.sendall(data)
        log.debug("→ %s %s", code, reason)

    def _dispatch(self, state: SessionState, req_line: str, headers: dict, body: str) -> None:
        parts = req_line.split()
        method = parts[0].upper() if parts else ""
        self._last_cseq = headers.get("cseq", "0")
        log.info("← %s (CSeq %s)", method, self._last_cseq)

        if method == "OPTIONS":
            self._reply(
                state,
                200,
                "OK",
                {"Public": "org.wfa.wfd1.0, GET_PARAMETER, SET_PARAMETER, OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN"},
            )
            # WFD often triggers source→sink GET_PARAMETER via SET; we answer sink requests only here.
            return

        if method == "GET_PARAMETER":
            params = self._wfd_params(state)
            self._reply(state, 200, "OK", body=params)
            return

        if method == "SET_PARAMETER":
            if "wfd_trigger_method: SETUP" in body:
                self._reply(state, 200, "OK")
                # Optionally send SETUP trigger response path — many sinks send SETUP themselves
                return
            if "wfd_trigger_method: PLAY" in body:
                self._reply(state, 200, "OK")
                return
            # honor client RTP ports if present
            for line in body.splitlines():
                if line.startswith("wfd_client_rtp_ports:"):
                    # format: RTP/AVP/UDP;unicast <rtp> <rtcp> mode=play
                    bits = line.split()
                    for b in bits:
                        if b.isdigit():
                            state.rtp_port = int(b)
                            break
            self._reply(state, 200, "OK")
            return

        if method == "SETUP":
            transport = headers.get("transport", "RTP/AVP/UDP;unicast;client_port=19000-19001")
            # parse client_port
            if "client_port=" in transport:
                try:
                    cp = transport.split("client_port=")[1].split(";")[0]
                    state.rtp_port = int(cp.split("-")[0])
                except ValueError:
                    pass
            resp_transport = (
                f"RTP/AVP/UDP;unicast;client_port={state.rtp_port}-{state.rtp_port+1};"
                f"server_port={self.server.server_rtp_port}-{self.server.server_rtp_port+1}"
            )
            self._reply(
                state,
                200,
                "OK",
                {
                    "Transport": resp_transport,
                    "Session": f"{state.session_id};timeout=30",
                },
            )
            return

        if method == "PLAY":
            self._reply(
                state,
                200,
                "OK",
                {
                    "Session": state.session_id,
                    "Range": "npt=0.000-",
                },
            )
            state.playing = True
            if self.server.on_play:
                self.server.on_play(state)
            return

        if method == "PAUSE":
            self._reply(state, 200, "OK", {"Session": state.session_id})
            return

        if method == "TEARDOWN":
            self._reply(state, 200, "OK", {"Session": state.session_id})
            state.playing = False
            if self.server.on_teardown:
                self.server.on_teardown(state)
            return

        if method == "DESCRIBE":
            sdp = self._sdp(state)
            self._reply(
                state,
                200,
                "OK",
                {"Content-Base": state.url + "/", "Content-Type": "application/sdp"},
                body=sdp,
            )
            return

        self._reply(state, 501, "Not Implemented")

    def _wfd_params(self, state: SessionState) -> str:
        # CEA 1920x1080p30 bitmap bit is commonly set; keep a simple profile.
        # wfd_video_formats: <misc> <cea> <vesa> <hh> ...
        return "\r\n".join(
            [
                "wfd_video_formats: 00 00 01 01 00000080 00000000 00000000 00 0000 0000 00 none none",
                "wfd_audio_codecs: LPCM 00000002 00",
                "wfd_client_rtp_ports: RTP/AVP/UDP;unicast 19000 0 mode=play",
                "wfd_content_protection: none",
                "wfd_uibc_capability: none",
                "wfd_display_edid: none",
                "wfd_connector_type: 05",
                "",
            ]
        )

    def _sdp(self, state: SessionState) -> str:
        return "\r\n".join(
            [
                "v=0",
                f"o=- {state.session_id} 1 IN IP4 {self.server.advertise_host}",
                "s=SegundaTelaWFD",
                f"c=IN IP4 {state.remote or self.server.advertise_host}",
                "t=0 0",
                "m=video 0 RTP/AVP 96",
                "a=rtpmap:96 H264/90000",
                "a=fmtp:96 profile-level-id=42e01f",
                "",
            ]
        )


class WfdRtspServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = WFD_DEFAULT_PORT,
        advertise_host: str = "0.0.0.0",
        width: int = 1920,
        height: int = 1080,
        fps: int = 30,
        server_rtp_port: int = 19100,
        on_play: Optional[Callable[[SessionState], None]] = None,
        on_teardown: Optional[Callable[[SessionState], None]] = None,
    ) -> None:
        super().__init__((host, port), _Handler)
        self.advertise_host = advertise_host
        self.width = width
        self.height = height
        self.fps = fps
        self.server_rtp_port = server_rtp_port
        self.on_play = on_play
        self.on_teardown = on_teardown
        self._thread: Optional[threading.Thread] = None

    def start_background(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, name="wfd-rtsp", daemon=True)
        self._thread.start()
        log.info("WFD RTSP source listening on %s:%s", *self.server_address)

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
