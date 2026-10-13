"""Self-tests without a tablet.

1. WFD flow: a scripted sink walks M1→M7 against our source and we check
   the negotiated mode, presentation URL and RTP ports.
2. Plain RTSP flow: a client that speaks first (OPTIONS → GET_PARAMETER →
   SETUP → PLAY → TEARDOWN), as earlier versions/tests expected.
"""
from __future__ import annotations

import logging
import socket
import time

from .fakesink import run_fake_sink
from .rtsp import WfdRtspServer

log = logging.getLogger(__name__)


def _txn(sock: socket.socket, req: str) -> str:
    sock.sendall(req.encode())
    sock.settimeout(5)
    data = b""
    while b"\r\n\r\n" not in data:
        data += sock.recv(4096)
    header, rest = data.split(b"\r\n\r\n", 1)
    text = header.decode()
    cl = 0
    for line in text.split("\r\n"):
        if line.lower().startswith("content-length:"):
            cl = int(line.split(":", 1)[1].strip() or 0)
    while len(rest) < cl:
        rest += sock.recv(4096)
    return text + "\r\n\r\n" + rest[:cl].decode("utf-8", "replace")


def run_wfd_loopback(port: int = 17236) -> bool:
    events: dict = {}

    def on_play(st):
        events["play"] = (st.mode, st.rtp_port, st.rtcp_port)

    def on_teardown(_st):
        events["teardown"] = True

    srv = WfdRtspServer(host="127.0.0.1", port=port, advertise_host="127.0.0.1",
                        width=1920, height=1200, on_play=on_play, on_teardown=on_teardown, m1_delay_s=0.1)
    srv.start_background()
    try:
        res = run_fake_sink("127.0.0.1", port)
        vf = res.m4_params.get("wfd_video_formats", "")
        assert vf.split()[5] == "10000000", vf  # VESA bit 28 = 1920x1200p30 (p60 exceeds level 4.2)
        assert res.m4_params.get("wfd_audio_codecs", "").startswith("AAC "), res.m4_params
        assert res.m4_params["wfd_client_rtp_ports"].split()[1] == "19000"
        assert res.m4_params["wfd_presentation_url"].startswith("rtsp://127.0.0.1:")
        assert res.m4_params["wfd_presentation_url"].endswith("/wfd1.0/streamid=0 none")
        assert res.setup_reply and res.setup_reply.status == 200
        assert res.play_reply and res.play_reply.status == 200
        time.sleep(0.1)
        mode, rtp, _rtcp = events["play"]
        assert (mode.width, mode.height, mode.fps, rtp) == (1920, 1200, 30, 19000), events
        assert events.get("teardown")
        log.info("loopback WFD (M1–M7) OK: %s", mode)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("WFD loopback failed: %r", exc)
        return False
    finally:
        srv.stop()


def run_plain_loopback(port: int = 17237) -> bool:
    played = {"ok": False}

    def on_play(_s):
        played["ok"] = True

    srv = WfdRtspServer(host="127.0.0.1", port=port, advertise_host="127.0.0.1", on_play=on_play)
    srv.start_background()
    time.sleep(0.2)
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        r1 = _txn(s, "OPTIONS * RTSP/1.0\r\nCSeq: 1\r\n\r\n")
        assert "200" in r1.split("\r\n")[0], r1
        r2 = _txn(
            s,
            "GET_PARAMETER rtsp://127.0.0.1/wfd1.0 RTSP/1.0\r\nCSeq: 2\r\nContent-Type: text/parameters\r\nContent-Length: 19\r\n\r\nwfd_video_formats\r\n",
        )
        assert "200" in r2.split("\r\n")[0], r2
        assert "wfd_video_formats" in r2
        r3 = _txn(
            s,
            "SETUP rtsp://127.0.0.1/wfd1.0/streamid=0 RTSP/1.0\r\nCSeq: 3\r\n"
            "Transport: RTP/AVP/UDP;unicast;client_port=19000-19001\r\n\r\n",
        )
        assert "200" in r3.split("\r\n")[0], r3
        assert "Session:" in r3
        r4 = _txn(
            s,
            "PLAY rtsp://127.0.0.1/wfd1.0/streamid=0 RTSP/1.0\r\nCSeq: 4\r\nSession: x\r\n\r\n",
        )
        assert "200" in r4.split("\r\n")[0], r4
        time.sleep(0.1)
        assert played["ok"], "on_play not called"
        r5 = _txn(
            s,
            "TEARDOWN rtsp://127.0.0.1/wfd1.0/streamid=0 RTSP/1.0\r\nCSeq: 5\r\nSession: x\r\n\r\n",
        )
        assert "200" in r5.split("\r\n")[0], r5
        s.close()
        log.info("loopback plain RTSP OK")
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("plain loopback failed: %r", exc)
        return False
    finally:
        srv.stop()


def run_loopback_test(port: int = 17236) -> bool:
    ok_wfd = run_wfd_loopback(port)
    ok_plain = run_plain_loopback(port + 1)
    return ok_wfd and ok_plain
