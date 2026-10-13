"""Self-test: local RTSP client walks OPTIONS → GET_PARAMETER → SETUP → PLAY → TEARDOWN."""
from __future__ import annotations

import logging
import socket
import time

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


def run_loopback_test(port: int = 17236) -> bool:
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
        log.info("loopback RTSP test OK")
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("loopback failed: %s", exc)
        return False
    finally:
        srv.stop()
