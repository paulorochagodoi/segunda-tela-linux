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
    m3_keys = [line.strip() for line in res.m3.body.splitlines() if line.strip()]
    assert m3_keys == [
        "wfd_client_rtp_ports",
        "wfd_audio_codecs",
        "wfd_video_formats",
        "wfd_display_edid",
        "wfd_idr_request_capability",
        "microsoft_cursor",
    ]
    assert "wfd_content_protection" not in m3_keys
    assert "wfd_coupled_sink" not in m3_keys
    assert res.m4_params["wfd_presentation_url"].startswith("rtsp://127.0.0.1:")
    assert res.m4_params["wfd_presentation_url"].endswith("/wfd1.0/streamid=0 none")
    assert res.m4_params["wfd_audio_codecs"] == "AAC 00000001 00"
    assert res.setup_reply.headers["transport"].startswith("RTP/AVP/UDP;unicast;client_port=19000")
    assert ";timeout=30" in res.setup_reply.headers["session"]
    mode, rtp, rtcp = ev["play"][0]
    # Server display is 1920x1080 and the fixture lists it, so that wins over 1200p.
    # SETUP still offers client_port=19000-19001; RTCP stays off (M3 said 0).
    assert (mode.width, mode.height, mode.fps, rtp, rtcp) == (1920, 1080, 60, 19000, 0)
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


# Galaxy Tab S10 FE 5G, Wi-Fi Direct, 2026-10-13. Level byte is only 3.1 but
# the CEA bitmap includes 1080p60. Audio is LPCM 48k + AAC. RTCP port 0.
S10_VIDEO = (
    "00 00 01 01 0001bde1 00300000 000003c0 00 0000 0000 0f none none, "
    "02 01 0001bde1 00300000 000003c0 00 0000 0000 0f none none"
)
S10_AUDIO = "LPCM 00000002 00, AAC 00000001 00"


def test_galaxy_tab_s10_m4_picks_720p30_and_aac():
    idrs = []
    srv, ev = _server(17306, width=1920, height=1200, on_idr_request=lambda st: idrs.append(st.remote))
    try:
        res = run_fake_sink(
            "127.0.0.1", 17306,
            video_formats=S10_VIDEO, audio_codecs=S10_AUDIO,
            idr_request=True, sink_keepalive=True,
        )
    finally:
        srv.stop()
    fields = res.m4_params["wfd_video_formats"].split()
    # CBP, echoed level 0x01, CEA bit 5 = 1280x720p30. 720p60 needs level 3.2.
    assert fields[2] == "01" and fields[3] == "01"
    assert fields[4] == "00000020" and fields[5] == "00000000"
    assert fields[-3] == "01"  # sink frame-rate-control 0x0f, skipping bit
    assert res.m4_params["wfd_audio_codecs"] == "AAC 00000001 00"
    assert "wfd_content_protection" not in res.m4.body
    mode, rtp, rtcp = ev["play"][0]
    negotiated = ev["negotiated"][0]
    assert (mode.width, mode.height, mode.fps, rtp, rtcp) == (1280, 720, 30, 19000, 0)
    assert negotiated.level_idc == 31
    assert negotiated.bitrate_kbps == 4096
    assert res.idr_reply is not None and res.idr_reply.status == 200
    assert res.idr_reply.headers.get("session")
    assert idrs == ["127.0.0.1"]
    assert res.sink_keepalive_reply is not None and res.sink_keepalive_reply.status == 200
    assert res.sink_keepalive_reply.body.strip() == ""
    assert res.sink_keepalive_reply.headers.get("session")


def test_gnd_compat_hardcodes_1080p30():
    srv, ev = _server(17309, width=1920, height=1200, gnd_compat=True)
    try:
        res = run_fake_sink("127.0.0.1", 17309, video_formats=S10_VIDEO, audio_codecs=S10_AUDIO)
    finally:
        srv.stop()
    fields = res.m4_params["wfd_video_formats"].split()
    assert fields[4] == "00000080"  # CEA bit 7 = 1920x1080p30
    mode, _rtp, _rtcp = ev["play"][0]
    assert (mode.width, mode.height, mode.fps) == (1920, 1080, 30)
    assert ev["negotiated"][0].level_idc is None


def test_bare_idr_request_line_is_answered():
    from segunda_tela.wfd.fakesink import FakeSink

    idrs = []
    srv, _ev = _server(17307, on_idr_request=lambda st: idrs.append(True))
    try:
        res = run_fake_sink("127.0.0.1", 17307, video_formats="none", audio_codecs="none")
        # The helper already tore the session down. Drive a second one and send
        # the bare parameter (no colon) before teardown by using FakeSink below.
    finally:
        srv.stop()
    assert res.m4_params.get("wfd_audio_codecs") is None

    srv, _ev = _server(17308, on_idr_request=lambda st: idrs.append(True))
    s = FakeSink("127.0.0.1", 17308)
    try:
        m1 = s.expect_request("OPTIONS")
        s.reply(m1, {"Public": "org.wfa.wfd1.0, SET_PARAMETER, GET_PARAMETER"})
        s.request("OPTIONS", "*", {"Require": "org.wfa.wfd1.0"})
        m3 = s.expect_request("GET_PARAMETER")
        s.reply(m3, body="wfd_video_formats: none\r\nwfd_audio_codecs: none\r\nwfd_client_rtp_ports: RTP/AVP/UDP;unicast 19000 0 mode=play\r\n")
        m4 = s.expect_request("SET_PARAMETER")
        s.reply(m4)
        m5 = s.expect_request("SET_PARAMETER")
        s.reply(m5)
        url = "rtsp://127.0.0.1/wfd1.0/streamid=0"
        setup = s.request("SETUP", url, {"Transport": "RTP/AVP/UDP;unicast;client_port=19000-19001"})
        session = setup.headers["session"].split(";")[0]
        s.request("PLAY", url, {"Session": session})
        bare = s.request("SET_PARAMETER", "rtsp://localhost/wfd1.0", {"Session": session}, body="wfd_idr_request\r\n")
        assert bare.status == 200
        assert idrs == [True]
    finally:
        s.close()
        srv.stop()


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
