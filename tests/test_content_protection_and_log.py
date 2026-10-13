"""HDCP must never be negotiated, and the RTSP exchange must land verbatim in the log file."""
import logging

import pytest

from segunda_tela.util.logging import WIRE_LOGGER, default_log_path, setup_logging
from segunda_tela.wfd.fakesink import run_fake_sink
from segunda_tela.wfd.formats import M3_QUERY_KEYS, m3_query_keys, parse_sink_caps, vendor_params
from segunda_tela.wfd.rtsp import WfdRtspServer

SAMSUNG_HDCP = "HDCP2.1 port=1189"


def _run(port, sink_kw=None, **srv_kw):
    srv = WfdRtspServer(host="127.0.0.1", port=port, advertise_host="127.0.0.1", m1_delay_s=0.1, **srv_kw)
    srv.start_background()
    try:
        return run_fake_sink("127.0.0.1", port, **(sink_kw or {}))
    finally:
        srv.stop()


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    saved = list(root.handlers), root.level
    yield
    for h in list(root.handlers):
        if getattr(h, "_segunda_tela", False):
            root.removeHandler(h)
            h.close()
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])


def test_m3_keys_match_gnome_network_displays():
    assert "wfd_content_protection" not in M3_QUERY_KEYS
    assert set(M3_QUERY_KEYS) >= {"wfd_video_formats", "wfd_audio_codecs", "wfd_client_rtp_ports"}
    assert m3_query_keys(probe_hdcp=True)[-1] == "wfd_content_protection"


def test_default_never_mentions_content_protection():
    res = _run(17311, {"content_protection": SAMSUNG_HDCP})
    assert "wfd_content_protection" not in res.m3.body
    assert "wfd_content_protection" not in res.m4.body
    assert res.play_reply.status == 200


def test_probe_hdcp_asks_but_m4_still_omits_it():
    res = _run(17312, {"content_protection": SAMSUNG_HDCP}, probe_hdcp=True)
    assert "wfd_content_protection" in res.m3.body.split()
    assert "wfd_content_protection" not in res.m4.body
    assert set(res.m4_params) == {"wfd_video_formats", "wfd_presentation_url", "wfd_client_rtp_ports"}


def test_sink_caps_hdcp_and_vendor_params():
    caps = parse_sink_caps(f"wfd_content_protection: {SAMSUNG_HDCP}\r\nsec_device_type: tablet\r\n")
    assert caps.offers_hdcp and caps.content_protection == SAMSUNG_HDCP
    assert not parse_sink_caps("wfd_content_protection: none\r\n").offers_hdcp
    assert vendor_params(caps.raw) == {"sec_device_type": "tablet"}


def test_default_log_path_honours_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_log_path() == tmp_path / "segunda-tela" / "segunda-tela.log"


def test_log_file_gets_full_rtsp_exchange(tmp_path, restore_logging):
    path = setup_logging(log_file=tmp_path / "st" / "segunda-tela.log")
    assert path is not None
    _run(17313, {"content_protection": SAMSUNG_HDCP, "vendor": {"sec_screen_mode": "1"}}, probe_hdcp=True)
    for h in logging.getLogger().handlers:
        h.flush()
    text = path.read_text()
    for needle in ("OPTIONS * RTSP/1.0", "Require: org.wfa.wfd1.0",       # M1
                   "GET_PARAMETER rtsp://localhost/wfd1.0 RTSP/1.0",     # M3 request
                   f"wfd_content_protection: {SAMSUNG_HDCP}",             # M3 response, verbatim
                   "wfd_client_rtp_ports: RTP/AVP/UDP;unicast 19000 0 mode=play",
                   "wfd_trigger_method: SETUP",                           # M5
                   "SETUP rtsp://", "PLAY rtsp://", "TEARDOWN rtsp://",   # M6, M7, teardown
                   "vendor-specific params: {'sec_screen_mode': '1'}",
                   "we do not negotiate HDCP"):
        assert needle in text, needle
    assert f"{WIRE_LOGGER}" in text


def test_console_hides_wire_unless_dump_rtsp(tmp_path, restore_logging, capsys):
    setup_logging(log_file=tmp_path / "a.log")
    logging.getLogger(WIRE_LOGGER).debug(">>> to x\nOPTIONS * RTSP/1.0")
    assert "OPTIONS" not in capsys.readouterr().err
    setup_logging(dump_rtsp=True, log_file=tmp_path / "b.log")
    logging.getLogger(WIRE_LOGGER).debug(">>> to x\nOPTIONS * RTSP/1.0")
    logging.getLogger("segunda_tela.other").debug("noise")
    err = capsys.readouterr().err
    assert "OPTIONS * RTSP/1.0" in err and "noise" not in err


def test_cli_flags_parse():
    from segunda_tela.cli import build_parser

    p = build_parser()
    assert p.parse_args([]).probe_hdcp is False
    assert p.parse_args(["--no-hdcp"]).probe_hdcp is False
    a = p.parse_args(["--probe-hdcp", "--dump-rtsp", "--log-file", "/tmp/x.log"])
    assert a.probe_hdcp and a.dump_rtsp and a.log_file == "/tmp/x.log"
