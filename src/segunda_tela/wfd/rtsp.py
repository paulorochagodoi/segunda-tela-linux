"""Wi-Fi Display (Miracast) RTSP *source* (WFD spec v1.0, section 6.4).

Message flow once a sink opens TCP to our port 7236:

    M1  source → sink   OPTIONS * (Require: org.wfa.wfd1.0)
    M2  sink → source   OPTIONS *
    M3  source → sink   GET_PARAMETER  (sink capabilities)
    M4  source → sink   SET_PARAMETER  (chosen format, presentation URL, RTP ports)
    M5  source → sink   SET_PARAMETER  wfd_trigger_method: SETUP
    M6  sink → source   SETUP
    M7  sink → source   PLAY           → we start MPEG-TS/RTP to the sink
    M16 source → sink   GET_PARAMETER  (keep-alive, every ~25 s)

The source being the side that speaks first is essential: a server that only
waits for requests (as earlier versions of this project did) never gets past
the TCP connect with a real sink.

If the peer speaks first (OPTIONS before our M1, e.g. VLC or the legacy
loopback test) we fall back to plain reactive RTSP handling.

`WfdRtspServer.connect_to()` runs the same source state machine over an
*outbound* TCP connection (USB mode: the PC dials a sink port on the tablet
instead of waiting for the tablet to dial us). M1 is sent immediately.

Written from the public spec; behaviour cross-checked against the open-source
gnome-network-displays project. Not affiliated with Samsung.
"""
from __future__ import annotations

import logging
import random
import socket
import socketserver
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .formats import (  # re-exported for backwards compatibility
    CEA_MODES,
    DEFAULT_RTP_PORT,
    FALLBACK_MODE,
    H264_LEVEL_31,
    H264_LEVEL_32,
    H264_LEVEL_40,
    H264_LEVEL_41,
    H264_LEVEL_42,
    H264_PROFILE_CBP,
    H264_PROFILE_CHP,
    LEVEL_IDC,
    M3_HDCP_PROBE_KEY,
    M3_QUERY_KEYS,
    Negotiated,
    SinkCaps,
    VideoMode,
    cea_bitmap,
    cea_native_index,
    h264_level_for,
    m3_query_keys,
    m4_video_descriptor,
    negotiate,
    parse_client_rtp_ports,
    parse_parameters,
    parse_sink_caps,
    requested_keys,
    vendor_params,
    wfd_audio_codecs,
    wfd_video_formats,
)

__all__ = [
    "CEA_MODES", "H264_LEVEL_31", "H264_LEVEL_32", "H264_LEVEL_40", "H264_LEVEL_41", "H264_LEVEL_42",
    "H264_PROFILE_CBP", "H264_PROFILE_CHP", "cea_bitmap", "cea_native_index", "h264_level_for",
    "requested_keys", "wfd_audio_codecs", "wfd_video_formats", "SessionState", "WfdRtspServer",
    "RtspMessage", "local_ip",
]

log = logging.getLogger(__name__)
# Verbatim RTSP traffic at DEBUG: always in the log file, on the console with --dump-rtsp / -v.
wire_log = logging.getLogger(__name__ + ".wire")

WFD_DEFAULT_PORT = 7236
KEEPALIVE_S = 25
SESSION_TIMEOUT_S = 30
PUBLIC_METHODS = "org.wfa.wfd1.0, SET_PARAMETER, GET_PARAMETER"
PLAIN_PUBLIC = "org.wfa.wfd1.0, GET_PARAMETER, SET_PARAMETER, OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN"


# ------------------------------------------------------------------ messages

@dataclass
class RtspMessage:
    first_line: str
    headers: dict[str, str] = field(default_factory=dict)  # lower-case keys
    body: str = ""
    raw: str = ""  # message exactly as received (for the wire log)

    @property
    def is_response(self) -> bool:
        return self.first_line.startswith("RTSP/")

    @property
    def method(self) -> str:
        return "" if self.is_response else self.first_line.split(" ", 1)[0].upper()

    @property
    def status(self) -> int:
        if not self.is_response:
            return 0
        try:
            return int(self.first_line.split()[1])
        except (IndexError, ValueError):
            return 0

    @property
    def cseq(self) -> str:
        return self.headers.get("cseq", "0")


def encode_message(first_line: str, headers: dict[str, str], body: str = "") -> bytes:
    headers = dict(headers)
    raw_body = body.encode()
    if raw_body:
        headers.setdefault("Content-Type", "text/parameters")
        headers["Content-Length"] = str(len(raw_body))
    lines = [first_line] + [f"{k}: {v}" for k, v in headers.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + raw_body


class MessageReader:
    """Incremental RTSP message parser over a socket."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""

    def feed(self, data: bytes) -> None:
        self.buf += data

    def pop(self) -> Optional[RtspMessage]:
        if b"\r\n\r\n" not in self.buf:
            return None
        head, rest = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode("utf-8", "replace").split("\r\n")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        try:
            clen = int(headers.get("content-length", "0") or 0)
        except ValueError:
            clen = 0
        if len(rest) < clen:
            return None
        body, self.buf = rest[:clen], rest[clen:]
        raw = (head + b"\r\n\r\n" + body).decode("utf-8", "replace")
        return RtspMessage(lines[0].strip(), headers, body.decode("utf-8", "replace"), raw)


def _wire(direction: str, peer: str, text: str) -> None:
    if wire_log.isEnabledFor(logging.DEBUG):
        wire_log.debug("%s %s\n%s", direction, peer, text.replace("\r\n", "\n").rstrip("\n"))


# ------------------------------------------------------------------ session

@dataclass
class SessionState:
    session_id: str = field(default_factory=lambda: f"{random.randint(1, 0x7FFFFFFF):08X}")
    url: str = "rtsp://127.0.0.1/wfd1.0/streamid=0"
    rtp_port: int = DEFAULT_RTP_PORT
    rtcp_port: int = 0
    width: int = 1920
    height: int = 1080
    fps: int = 30
    playing: bool = False
    setup_done: bool = False
    cseq: int = 0
    remote: str = ""
    local: str = ""
    wfd: bool = True
    sink: Optional[SinkCaps] = None
    negotiated: Optional[Negotiated] = None
    m4_attempts: int = 0
    media_host: str = ""  # RTP destination when it differs from the RTSP peer (adb forward)
    outbound: bool = False
    m1_answered: bool = False  # peer replied to our M1 → it speaks RTSP/WFD

    @property
    def rtp_host(self) -> str:
        return self.media_host or self.remote

    @property
    def mode(self) -> VideoMode:
        if self.negotiated:
            return self.negotiated.mode
        return VideoMode(self.width, self.height, self.fps)


def _is_loopback(host: str) -> bool:
    return host.startswith("127.") or host in ("::1", "::ffff:127.0.0.1")


class _Handler(socketserver.BaseRequestHandler):
    server: "WfdRtspServer"

    def __init__(self, request, client_address, server, outbound: bool = False, media_host: str = "",
                 on_state: Optional[Callable[["SessionState"], None]] = None) -> None:
        self._outbound = outbound
        self._media_host = media_host
        self._on_state = on_state
        super().__init__(request, client_address, server)

    # --- plumbing
    def setup(self) -> None:
        self.sock: socket.socket = self.request
        self.reader = MessageReader(self.sock)
        self._cseq = 0
        self._pending: dict[str, str] = {}  # our CSeq -> purpose
        self._send_lock = threading.Lock()
        self._m1_ok = False
        self._m2_seen = False
        self._m3_sent = False
        self._m1_at = 0.0
        self._last_keepalive = time.monotonic()
        self._closing = False

    def _send(self, data: bytes) -> None:
        _wire(">>> to", self._peer_label(), data.decode("utf-8", "replace"))
        with self._send_lock:
            self.sock.sendall(data)

    def _peer_label(self) -> str:
        return f"{self.client_address[0]}:{self.client_address[1]}"

    def _request(self, method: str, url: str, purpose: str, body: str = "", extra: Optional[dict] = None) -> None:
        self._cseq += 1
        headers = {"CSeq": str(self._cseq)}
        if extra:
            headers.update(extra)
        self._pending[str(self._cseq)] = purpose
        log.info("→ %s %s (CSeq %s, %s)", method, url, self._cseq, purpose)
        self._send(encode_message(f"{method} {url} RTSP/1.0", headers, body))

    def _reply(self, msg: RtspMessage, code: int = 200, reason: str = "OK", extra: Optional[dict] = None, body: str = "") -> None:
        headers = {"CSeq": msg.cseq}
        if extra:
            headers.update(extra)
        self._send(encode_message(f"RTSP/1.0 {code} {reason}", headers, body))
        log.debug("→ %s %s (CSeq %s)", code, reason, msg.cseq)

    # --- main loop
    def handle(self) -> None:
        peer = self.client_address[0]
        srv = self.server
        state = SessionState(
            width=srv.width,
            height=srv.height,
            fps=srv.fps,
            remote=peer,
            local=self.sock.getsockname()[0],
            media_host=self._media_host or (srv.loopback_media_host if _is_loopback(peer) else ""),
            outbound=self._outbound,
        )
        # gnome-network-displays includes the local RTSP port:
        # rtsp://addr:port/wfd1.0/streamid=0
        state.url = f"rtsp://{state.local}:{self.sock.getsockname()[1]}/wfd1.0/streamid=0"
        self.state = state
        if self._on_state:
            self._on_state(state)
        srv._register(self)
        if self._outbound:
            log.info("RTSP: connected out to sink %s:%s (local %s, RTP → %s)",
                     peer, self.client_address[1], state.local, state.rtp_host)
        else:
            log.info("RTSP sink connected from %s (local %s)", peer, state.local)
        try:
            self._serve(state)
        finally:
            if state.playing and srv.on_teardown:
                state.playing = False
                srv.on_teardown(state)
            srv._unregister(self)
            log.info("RTSP sink %s disconnected", peer)

    def _serve(self, state: SessionState) -> None:
        srv = self.server
        first: bytes = b""
        if not self._outbound:
            # Give a plain RTSP client a moment to speak first.
            self.sock.settimeout(srv.m1_delay_s)
            try:
                first = self.sock.recv(4096)
                if not first:
                    return
            except socket.timeout:
                pass
            except OSError:
                return

        if first:
            state.wfd = False
            log.info("peer spoke first → plain RTSP mode")
            self.reader.feed(first)
        else:
            self._m1_at = time.monotonic()
            self._request("OPTIONS", "*", "M1", extra={"Require": "org.wfa.wfd1.0"})

        self.sock.settimeout(1.0)
        try:
            while not self._closing:
                msg = self.reader.pop()
                if msg is not None:
                    _wire("<<< from", self._peer_label(), msg.raw)
                    self._dispatch(msg)
                    continue
                try:
                    data = self.sock.recv(65536)
                except socket.timeout:
                    self._tick()
                    continue
                if not data:
                    break
                self.reader.feed(data)
        except OSError as exc:
            log.info("RTSP connection error: %s", exc)

    def _tick(self) -> None:
        now = time.monotonic()
        # Some sinks never send M2; don't stall the negotiation forever.
        if self.state.wfd and self._m1_ok and not self._m3_sent and now - self._m1_at > 3:
            log.info("no M2 from sink; sending M3 anyway")
            self._send_m3()
        if self.state.wfd and self.state.playing and now - self._last_keepalive >= self.server.keepalive_s:
            self._last_keepalive = now
            self._request("GET_PARAMETER", "rtsp://localhost/wfd1.0", "M16",
                          extra={"Session": self.state.session_id})

    # --- WFD source steps
    def _send_m3(self) -> None:
        if self._m3_sent:
            return
        self._m3_sent = True
        body = "\r\n".join(m3_query_keys(self.server.probe_hdcp)) + "\r\n"
        self._request("GET_PARAMETER", "rtsp://localhost/wfd1.0", "M3", body=body,
                      extra={"Content-Type": "text/parameters"})

    def _send_m4(self) -> None:
        st = self.state
        assert st.negotiated is not None
        st.m4_attempts += 1
        n = st.negotiated
        lines = [f"wfd_video_formats: {m4_video_descriptor(n.mode, n.level, n.profile, n.latency, n.min_slice_size, n.frame_rate_control)}"]
        if n.audio:
            lines.append(f"wfd_audio_codecs: {n.audio}")
        lines.append(f"wfd_presentation_URL: {st.url} none")
        lines.append(f"wfd_client_rtp_ports: RTP/AVP/UDP;unicast {st.rtp_port} {st.rtcp_port} mode=play")
        # No wfd_content_protection here, ever: naming it in M4 selects HDCP, which we don't implement.
        assert not any(line.startswith(M3_HDCP_PROBE_KEY) for line in lines)
        self._request("SET_PARAMETER", "rtsp://localhost/wfd1.0", "M4", body="\r\n".join(lines) + "\r\n",
                      extra={"Content-Type": "text/parameters"})

    def _send_m5(self, method: str = "SETUP") -> None:
        self._request("SET_PARAMETER", "rtsp://localhost/wfd1.0", f"M5-{method}",
                      body=f"wfd_trigger_method: {method}\r\n", extra={"Content-Type": "text/parameters"})

    def _on_response(self, msg: RtspMessage) -> None:
        purpose = self._pending.pop(msg.cseq, "?")
        log.info("← %s for %s (CSeq %s)", msg.status, purpose, msg.cseq)
        st = self.state
        srv = self.server
        if purpose == "M1":
            st.m1_answered = True
            if msg.status != 200 or "org.wfa.wfd1.0" not in msg.headers.get("public", "org.wfa.wfd1.0"):
                log.warning("sink M1 reply unexpected: %s %s", msg.first_line, msg.headers.get("public"))
            self._m1_ok = True
            if self._m2_seen:
                self._send_m3()
        elif purpose == "M3":
            if msg.status != 200:
                log.warning("M3 rejected (%s); using fallback caps", msg.status)
            st.sink = parse_sink_caps(msg.body)
            self._log_sink_caps(st.sink)
            st.rtp_port, st.rtcp_port = st.sink.rtp_port, st.sink.rtcp_port
            st.negotiated = srv.negotiate(st.sink)
            log.info("negotiated %s level=0x%02x audio=%s (%s); RTP→%s:%s RTCP=%s",
                     st.negotiated.mode, st.negotiated.level, st.negotiated.audio or "none",
                     st.negotiated.reason, st.remote, st.rtp_port, st.rtcp_port or "off")
            if srv.on_negotiated:
                srv.on_negotiated(st)
            self._send_m4()
        elif purpose == "M4":
            if msg.status == 200:
                self._send_m5("SETUP")
            elif st.m4_attempts < 2 and st.negotiated and st.negotiated.mode != FALLBACK_MODE:
                log.warning("sink rejected %s; retrying M4 with 1080p30", st.negotiated.mode)
                st.negotiated = Negotiated(FALLBACK_MODE, h264_level_for(1920, 1080, 30),
                                           audio=st.negotiated.audio, reason="M4 fallback")
                if srv.on_negotiated:
                    srv.on_negotiated(st)
                self._send_m4()
            else:
                log.error("sink rejected M4 (%s); giving up", msg.status)

    def _log_sink_caps(self, sink: SinkCaps) -> None:
        raw = sink.raw
        for key in ("wfd_video_formats", "wfd_audio_codecs", "wfd_client_rtp_ports",
                    "wfd_content_protection", "wfd_idr_request_capability"):
            if key in raw:
                log.info("M3 sink %s: %s", key, raw[key])
        if "wfd_display_edid" in raw:
            log.info("M3 sink wfd_display_edid: %s", "present" if sink.edid else raw["wfd_display_edid"])
        extra = vendor_params(raw)
        if extra:
            log.info("M3 sink vendor-specific params: %s", extra)
        if sink.offers_hdcp:
            log.warning("sink offers content protection (%s); we do not negotiate HDCP — "
                        "M4 omits wfd_content_protection, stream is unencrypted", sink.content_protection)

    def _dispatch(self, msg: RtspMessage) -> None:
        if msg.is_response:
            self._on_response(msg)
            return
        st = self.state
        method = msg.method
        log.info("← %s (CSeq %s)", method, msg.cseq)

        if method == "OPTIONS":
            self._m2_seen = True
            self._reply(msg, extra={"Public": PUBLIC_METHODS if st.wfd else PLAIN_PUBLIC})
            if st.wfd and self._m1_ok:
                self._send_m3()
            return

        if method == "GET_PARAMETER":
            keys = requested_keys(msg.body)
            # Empty body is the WFD keep-alive (M16 from the sink, or a reply
            # we must answer). Echo Session once SETUP has completed.
            self._reply(msg, extra=self._session_header(), body="" if not keys else self._source_params(keys))
            return

        if method == "SET_PARAMETER":
            params = parse_parameters(msg.body)
            bare = {line.strip().lower() for line in msg.body.splitlines() if line.strip() and ":" not in line}
            if "wfd_idr_request" in params or "wfd_idr_request" in bare:
                log.info("sink requested IDR")
                if self.server.on_idr_request:
                    self.server.on_idr_request(st)
            if "wfd_client_rtp_ports" in params:
                st.rtp_port, st.rtcp_port = parse_client_rtp_ports(params["wfd_client_rtp_ports"])
            self._reply(msg, extra=self._session_header())
            return

        if method == "SETUP":
            transport = msg.headers.get("transport", "")
            requested_client = ""
            if "client_port=" in transport:
                requested_client = transport.split("client_port=", 1)[1].split(";", 1)[0]
                try:
                    ports = requested_client.split("-")
                    st.rtp_port = int(ports[0])
                    # Samsung SETUP is `client_port=19000-19001` even when M3
                    # said RTCP port 0. Honouring that range turned RTCP on and
                    # pulled rtpbin into the pipeline (the mini_object critical).
                    m3_rtcp = st.sink.rtcp_port if st.sink is not None else None
                    if m3_rtcp == 0:
                        st.rtcp_port = 0
                    elif len(ports) > 1 and int(ports[1]) not in (0, st.rtp_port):
                        st.rtcp_port = int(ports[1])
                except ValueError:
                    pass
            st.setup_done = True
            sp = self.server.server_rtp_port
            if not requested_client:
                requested_client = f"{st.rtp_port}" + (f"-{st.rtcp_port}" if st.rtcp_port else "")
            self._reply(
                msg,
                extra={
                    "Transport": f"RTP/AVP/UDP;unicast;client_port={requested_client};server_port={sp}-{sp + 1}",
                    "Session": f"{st.session_id};timeout={SESSION_TIMEOUT_S}",
                },
            )
            return

        if method == "PLAY":
            self._reply(msg, extra={"Session": st.session_id, "Range": "npt=now-"})
            was_playing = st.playing
            st.playing = True
            self._last_keepalive = time.monotonic()
            if not was_playing and self.server.on_play:
                self.server.on_play(st)
            return

        if method == "PAUSE":
            self._reply(msg, extra={"Session": st.session_id})
            if st.playing:
                st.playing = False
                if self.server.on_teardown:
                    self.server.on_teardown(st)
            return

        if method == "TEARDOWN":
            self._reply(msg, extra={"Session": st.session_id})
            if st.playing:
                st.playing = False
                if self.server.on_teardown:
                    self.server.on_teardown(st)
            self._closing = True
            return

        if method == "DESCRIBE":
            self._reply(msg, extra={"Content-Base": st.url + "/", "Content-Type": "application/sdp"},
                        body=self._sdp(st))
            return

        self._reply(msg, 501, "Not Implemented")

    def _session_header(self) -> Optional[dict]:
        if self.state.setup_done:
            return {"Session": self.state.session_id}
        return None

    def _source_params(self, keys: list[str]) -> str:
        st = self.state
        values = {
            "wfd_video_formats": wfd_video_formats(st.width, st.height, st.fps),
            "wfd_audio_codecs": wfd_audio_codecs(),
            "wfd_client_rtp_ports": f"RTP/AVP/UDP;unicast {st.rtp_port} {st.rtcp_port} mode=play",
            "wfd_content_protection": "none",
            "wfd_uibc_capability": "none",
            "wfd_display_edid": "none",
            "wfd_connector_type": "05",
        }
        lines = [f"{k}: {values[k]}" for k in keys if k in values]
        return "\r\n".join(lines + [""])

    def _sdp(self, st: SessionState) -> str:
        return "\r\n".join(
            [
                "v=0",
                f"o=- {st.session_id} 1 IN IP4 {st.local}",
                "s=SegundaTelaWFD",
                f"c=IN IP4 {st.remote or st.local}",
                "t=0 0",
                "m=video 0 RTP/AVP 33",
                "a=rtpmap:33 MP2T/90000",
                "",
            ]
        )

    def close(self) -> None:
        self._closing = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


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
        on_negotiated: Optional[Callable[[SessionState], None]] = None,
        on_idr_request: Optional[Callable[[SessionState], None]] = None,
        want_size: Optional[tuple[int, int]] = None,
        max_fps: int = 60,
        max_pixels: int = 1920 * 1200,
        audio: bool = True,
        encoder: str = "auto",
        probe_hdcp: bool = False,
        gnd_compat: bool = False,
        m1_delay_s: float = 0.5,
        keepalive_s: float = KEEPALIVE_S,
    ) -> None:
        super().__init__((host, port), _Handler)
        self.advertise_host = advertise_host
        self.width = width
        self.height = height
        self.fps = fps
        self.server_rtp_port = server_rtp_port
        self.on_play = on_play
        self.on_teardown = on_teardown
        self.on_negotiated = on_negotiated
        self.on_idr_request = on_idr_request
        self.want_size = want_size
        self.max_fps = max_fps
        self.max_pixels = max_pixels
        self.audio = audio
        self.encoder = encoder
        self.probe_hdcp = probe_hdcp  # only *ask* the sink about HDCP (diagnostics); never select it
        # Literal gnome-network-displays resolution (hardcoded 1920x1080p30).
        # The RTSP bytes and encoder graph already follow that project either way.
        self.gnd_compat = gnd_compat
        self.m1_delay_s = m1_delay_s
        self.keepalive_s = keepalive_s
        # RTP destination for sinks that reach us via loopback (`adb reverse`):
        # their RTSP peer is 127.0.0.1 but media must go to the tablet's tether IP.
        self.loopback_media_host = ""
        self._thread: Optional[threading.Thread] = None
        self._handlers: set[_Handler] = set()
        self._hlock = threading.Lock()

    def negotiate(self, sink: SinkCaps) -> Negotiated:
        # Prefer the virtual display's own size when the user did not pass
        # --width/--height. That is the "native" resolution we can actually
        # show; it wins only if the sink's wfd_video_formats lists it.
        want = self.want_size or ((self.width, self.height) if self.width and self.height else None)
        return negotiate(sink, want=want, max_fps=self.max_fps, max_pixels=self.max_pixels,
                         audio=self.audio, encoder=self.encoder, gnd_compat=self.gnd_compat)

    def _register(self, h: _Handler) -> None:
        with self._hlock:
            self._handlers.add(h)

    def _unregister(self, h: _Handler) -> None:
        with self._hlock:
            self._handlers.discard(h)

    def start_background(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, name="wfd-rtsp", daemon=True)
        self._thread.start()
        log.info("WFD RTSP source listening on %s:%s", *self.server_address)

    @property
    def active_sessions(self) -> int:
        with self._hlock:
            return len(self._handlers)

    def connect_to(
        self,
        host: str,
        port: int = WFD_DEFAULT_PORT,
        timeout: float = 3.0,
        media_host: str = "",
        sock: Optional[socket.socket] = None,
    ) -> threading.Thread:
        """Dial a sink's RTSP port and run the source M1–M7 flow over that socket.

        `media_host` overrides the RTP destination (e.g. RTSP via `adb forward`
        on 127.0.0.1 while RTP goes to the tablet's tether IP). Raises OSError
        if the TCP connect fails. The returned thread gets a `wfd_state`
        attribute (SessionState) once the session starts.
        """
        if sock is None:
            sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(None)
        log.info("RTSP: TCP connected to %s:%s — sending M1", host, port)

        def run() -> None:
            try:
                _Handler(sock, (host, port), self, outbound=True, media_host=media_host,
                         on_state=lambda st: setattr(t, "wfd_state", st))
            except Exception as exc:  # noqa: BLE001
                log.warning("outbound RTSP session %s:%s ended: %s", host, port, exc)
            finally:
                try:
                    sock.close()
                except OSError:
                    pass

        t = threading.Thread(target=run, name=f"wfd-out-{host}:{port}", daemon=True)
        t.wfd_state = None  # type: ignore[attr-defined]
        t.start()
        return t

    def stop(self) -> None:
        with self._hlock:
            handlers = list(self._handlers)
        for h in handlers:
            h.close()
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
