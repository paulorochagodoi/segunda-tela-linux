import shlex

from segunda_tela.wfd.pipeline import (
    CaptureSource,
    StreamPipeline,
    build_pipeline,
    default_bitrate_kbps,
    encoder_fragment,
    pick_encoder,
    pattern_source,
)


def test_pick_encoder_order():
    assert pick_encoder(available=lambda n: n in {"x264enc", "vah264enc"}) == "vah264enc"
    assert pick_encoder(available=lambda n: n in {"x264enc", "vaapih264enc"}) == "vaapih264enc"
    assert pick_encoder(available=lambda n: n == "openh264enc") == "openh264enc"
    assert pick_encoder(available=lambda n: False) == "x264enc"
    assert pick_encoder("x264enc", available=lambda n: False) == "x264enc"


def test_encoder_fragments():
    assert "rate-control=cbr" in encoder_fragment("vah264enc", 10000, 60)
    assert "key-int-max=60" in encoder_fragment("vah264enc", 10000, 60)
    assert "b-frames=0" in encoder_fragment("vah264enc", 10000, 60)
    assert "max-bframes=0" in encoder_fragment("vaapih264enc", 10000, 30)
    assert "tune=zerolatency" in encoder_fragment("x264enc", 8000, 30)


def test_wfd_transport_is_mpegts_over_rtp():
    desc = build_pipeline(pattern_source(1920, 1200, 60), "192.168.49.2", 19000, 1920, 1200, 60, 12000, "vah264enc")
    assert "mpegtsmux name=mux alignment=7" in desc
    assert "mux.sink_4113" in desc  # PID 0x1011
    assert "rtpmp2tpay pt=33" in desc
    assert "udpsink host=192.168.49.2 port=19000" in desc
    assert "rtph264pay" not in desc
    assert "rtpbin" not in desc  # sink sent RTCP port 0
    assert "profile=constrained-baseline" in desc
    assert "width=1920,height=1200,framerate=60/1" in desc
    shlex.split(desc)


def test_rtcp_and_audio_branches():
    desc = build_pipeline(pattern_source(1280, 720, 30), "10.0.0.2", 1028, 1280, 720, 30, 4000, "x264enc",
                          rtcp_port=1029, audio_source="audiotestsrc is-live=true")
    assert "rtpbin name=rtpbin" in desc and "port=1029" in desc
    assert "mux.sink_4352" in desc and "avenc_aac" in desc


def test_stream_pipeline_accepts_legacy_string():
    sp = StreamPipeline("videotestsrc", "127.0.0.1", 19000, encoder="x264enc")
    assert isinstance(sp.source, CaptureSource)
    assert sp._build_cmd()[:3] == ["gst-launch-1.0", "-e", "-q"]


def test_bitrate_heuristic():
    assert default_bitrate_kbps(1920, 1200, 60) == 13824
    assert default_bitrate_kbps(640, 480, 30) == 4000
    assert default_bitrate_kbps(3840, 2160, 60) == 20000
