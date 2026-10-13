import threading
import time

from segunda_tela.wfd.fakesink import run_fake_sink
from segunda_tela.wfd.rtsp import WfdRtspServer


def _server(port, **kw):
    events = {"play": [], "teardown": 0, "negotiated": []}
    srv = WfdRtspServer(
        host="127.0.0.1", port=port, advertise_host="127.0.0.1", m1_delay_s=0.1,
        on_play=lambda st: events["play"].append((st.mode, st.rtp_port, st.rtcp_port)),
        on_teardown=lambda st: events.__setitem__("teardown", events["teardown"] + 1),
        on_negotiated=lambda st: events["negotiated"].append(st.negotiated),
        **kw,
    )
    srv.start_background()
    return srv, events


def test_wfd_flow_default_tablet():
    srv, ev = _server(17301)
    try:
        res = run_fake_sink("127.0.0.1", 17301)
    finally:
        srv.stop()
    m3_keys = res.m3.body.split()
    assert {"wfd_video_formats", "wfd_audio_codecs", "wfd_client_rtp_ports"} <= set(m3_keys)
    assert "wfd_audio_codecs" not in res.m4_params  # video only
    assert res.setup_reply.headers["transport"].startswith("RTP/AVP/UDP;unicast;client_port=19000")
    assert ";timeout=30" in res.setup_reply.headers["session"]
    mode, rtp, rtcp = ev["play"][0]
    assert (mode.width, mode.height, mode.fps, rtp, rtcp) == (1920, 1200, 60, 19000, 0)
    assert ev["teardown"] == 1


def test_wfd_flow_sink_without_caps_falls_back_to_1080p():
    srv, ev = _server(17302)
    try:
        res = run_fake_sink("127.0.0.1", 17302, video_formats="none", audio_codecs="none")
    finally:
        srv.stop()
    vf = res.m4_params["wfd_video_formats"].split()
    assert vf[4] == "00000080"  # CEA bit 7 = 1920x1080p30
    assert (ev["play"][0][0].width, ev["play"][0][0].height) == (1920, 1080)


def test_wfd_flow_requested_size_fps_and_audio_rtcp():
    srv, ev = _server(17303, want_size=(1920, 1080), max_fps=30, audio=True)
    try:
        res = run_fake_sink("127.0.0.1", 17303, rtp_ports="RTP/AVP/UDP;unicast 1028 1029 mode=play")
    finally:
        srv.stop()
    assert res.m4_params["wfd_video_formats"].split()[4] == "00000080"
    assert res.m4_params["wfd_audio_codecs"] == "AAC 00000001 00"
    assert res.m4_params["wfd_client_rtp_ports"] == "RTP/AVP/UDP;unicast 1028 1029 mode=play"
    mode, rtp, rtcp = ev["play"][0]
    assert (mode.fps, rtp, rtcp) == (30, 1028, 1029)


def test_wfd_keepalive_m16():
    srv, ev = _server(17304, keepalive_s=1)
    try:
        res = run_fake_sink("127.0.0.1", 17304, wait_keepalive=True, timeout=5)
    finally:
        srv.stop()
    assert res.keepalive is not None and res.keepalive.method == "GET_PARAMETER"
    assert res.keepalive.body == ""


def test_server_stop_closes_live_connection():
    import socket

    srv, _ = _server(17305)
    s = socket.create_connection(("127.0.0.1", 17305), timeout=3)
    s.recv(4096)  # M1
    t = threading.Thread(target=srv.stop)
    t.start()
    t.join(5)
    assert not t.is_alive()
    s.close()
