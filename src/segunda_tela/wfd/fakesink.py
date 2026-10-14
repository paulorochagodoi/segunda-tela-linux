"""Scripted WFD *sink* used for loopback tests (no tablet required).

Behaves like a typical Miracast sink: answers M1, sends M2, answers M3 with
its capabilities, accepts M4/M5, then issues SETUP (M6) and PLAY (M7).
"""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Optional

from .formats import parse_parameters
from .rtsp import MessageReader, RtspMessage, encode_message

# A Galaxy-Tab-like sink: CBP+CHP level 4.2, CEA up to 1080p60, VESA up to 1920x1200p60.
TABLET_VIDEO_FORMATS = (
    "28 00 02 10 0001ffff 3fffffff 00000fff 00 0000 0000 11 none none, "
    "01 10 0001ffff 3fffffff 00000fff 00 0000 0000 11 none none"
)
TABLET_AUDIO = "LPCM 00000003 00, AAC 00000001 00"


@dataclass
class FakeSinkResult:
    m1: Optional[RtspMessage] = None
    m3: Optional[RtspMessage] = None
    m4: Optional[RtspMessage] = None
    m4_params: dict[str, str] = field(default_factory=dict)
    m5: Optional[RtspMessage] = None
    setup_reply: Optional[RtspMessage] = None
    play_reply: Optional[RtspMessage] = None
    keepalive: Optional[RtspMessage] = None
    idr_reply: Optional[RtspMessage] = None
    sink_keepalive_reply: Optional[RtspMessage] = None
    teardown_reply: Optional[RtspMessage] = None


class FakeSink:
    def __init__(self, host: str, port: int, timeout: float = 5.0, sock: Optional[socket.socket] = None) -> None:
        self.sock = sock if sock is not None else socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.reader = MessageReader(self.sock)
        self.cseq = 0

    def next(self) -> RtspMessage:
        while True:
            msg = self.reader.pop()
            if msg is not None:
                return msg
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError("source closed the connection")
            self.reader.feed(data)

    def expect_request(self, method: str) -> RtspMessage:
        msg = self.next()
        if msg.is_response or msg.method != method:
            raise AssertionError(f"expected {method} request, got {msg.first_line!r}")
        return msg

    def reply(self, req: RtspMessage, extra: Optional[dict] = None, body: str = "") -> None:
        headers = {"CSeq": req.cseq}
        if extra:
            headers.update(extra)
        self.sock.sendall(encode_message("RTSP/1.0 200 OK", headers, body))

    def request(self, method: str, url: str, extra: Optional[dict] = None, body: str = "") -> RtspMessage:
        self.cseq += 1
        headers = {"CSeq": str(self.cseq)}
        if extra:
            headers.update(extra)
        self.sock.sendall(encode_message(f"{method} {url} RTSP/1.0", headers, body))
        msg = self.next()
        if not msg.is_response or msg.cseq != str(self.cseq):
            raise AssertionError(f"expected reply to CSeq {self.cseq}, got {msg.first_line!r}")
        return msg

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def run_fake_sink(
    host: str,
    port: int,
    video_formats: str = TABLET_VIDEO_FORMATS,
    audio_codecs: str = TABLET_AUDIO,
    rtp_ports: str = "RTP/AVP/UDP;unicast 19000 0 mode=play",
    display_edid: str = "none",
    content_protection: str = "none",
    vendor: dict[str, str] | None = None,
    wait_keepalive: bool = False,
    idr_request: bool = False,
    uibc_capability: Optional[str] = None,
    sink_keepalive: bool = False,
    timeout: float = 5.0,
    sock: Optional[socket.socket] = None,
) -> FakeSinkResult:
    """Drive one sink session. With `sock`, use that (already accepted) connection."""
    res = FakeSinkResult()
    s = FakeSink(host, port, timeout, sock=sock)
    try:
        res.m1 = s.expect_request("OPTIONS")
        assert res.m1.headers.get("require") == "org.wfa.wfd1.0", res.m1.headers
        s.reply(res.m1, {"Public": "org.wfa.wfd1.0, SET_PARAMETER, GET_PARAMETER"})

        m2 = s.request("OPTIONS", "*", {"Require": "org.wfa.wfd1.0"})
        assert m2.status == 200 and "org.wfa.wfd1.0" in m2.headers.get("public", ""), m2

        res.m3 = s.expect_request("GET_PARAMETER")
        answers = {
            "wfd_video_formats": video_formats,
            "wfd_audio_codecs": audio_codecs,
            "wfd_client_rtp_ports": rtp_ports,
            "wfd_display_edid": display_edid,
            "wfd_connector_type": "05",
            "wfd_content_protection": content_protection,
            "wfd_idr_request_capability": "1",
        }
        if uibc_capability is not None:
            answers["wfd_uibc_capability"] = uibc_capability
        keys = [k.strip() for k in res.m3.body.splitlines() if k.strip()]
        body = "".join(f"{k}: {answers[k]}\r\n" for k in keys if k in answers)
        body += "".join(f"{k}: {v}\r\n" for k, v in (vendor or {}).items())
        s.reply(res.m3, body=body)

        res.m4 = s.expect_request("SET_PARAMETER")
        res.m4_params = parse_parameters(res.m4.body)
        s.reply(res.m4)

        res.m5 = s.expect_request("SET_PARAMETER")
        assert "wfd_trigger_method: SETUP" in res.m5.body, res.m5.body
        s.reply(res.m5)

        url = res.m4_params.get("wfd_presentation_url", "rtsp://localhost/wfd1.0/streamid=0").split()[0]
        nums = [tok for tok in rtp_ports.split() if tok.isdigit()]
        rtp = nums[0] if nums else "19000"
        second = nums[1] if len(nums) > 1 else "0"
        # Samsung SETUP always offers a range (19000-19001) even when M3 said
        # the RTCP port is 0. The source must not treat that as "RTCP on".
        if second in ("0", rtp):
            client_port = f"{rtp}-{int(rtp) + 1}"
        else:
            client_port = f"{rtp}-{second}"
        res.setup_reply = s.request("SETUP", url, {"Transport": f"RTP/AVP/UDP;unicast;client_port={client_port}"})
        session = res.setup_reply.headers.get("session", "").split(";")[0]
        res.play_reply = s.request("PLAY", url.rsplit("/streamid", 1)[0], {"Session": session})

        if idr_request:
            res.idr_reply = s.request(
                "SET_PARAMETER", "rtsp://localhost/wfd1.0",
                {"Session": session, "Content-Type": "text/parameters"},
                body="wfd_idr_request: 1\r\n",
            )
        if sink_keepalive:
            res.sink_keepalive_reply = s.request(
                "GET_PARAMETER", "rtsp://localhost/wfd1.0", {"Session": session},
            )

        if wait_keepalive:
            res.keepalive = s.expect_request("GET_PARAMETER")
            s.reply(res.keepalive)

        res.teardown_reply = s.request("TEARDOWN", url, {"Session": session})
        return res
    finally:
        s.close()
