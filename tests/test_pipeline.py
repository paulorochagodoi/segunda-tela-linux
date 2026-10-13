import shlex

import pytest

from segunda_tela.wfd.pipeline import (
    PCR_INTERVAL_TICKS,
    PAT_INTERVAL_TICKS,
    PMT_INTERVAL_TICKS,
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


def test_rtcp_port_never_pulls_in_rtpbin():
    # rtcp_port used to build an rtpbin graph. That graph is what logged
    # gst_mini_object_unref(NULL) on the Tab S10 FE (SETUP 19000-19001).
    desc = build_pipeline(pattern_source(1280, 720, 30), "10.0.0.2", 1028, 1280, 720, 30, 4000, "x264enc",
                          rtcp_port=1029, audio="AAC 00000001 00")
    assert "rtpbin" not in desc
    assert "port=1029" not in desc
    assert "udpsink host=10.0.0.2 port=1028" in desc
    assert "mux.sink_4352" in desc and "avenc_aac" in desc
    assert "wave=silence" in desc  # no desktop source → silent AAC
    shlex.split(desc)


def test_stream_matches_gnome_network_displays():
    desc = build_pipeline(
        pattern_source(1920, 1080, 60), "10.42.0.120", 19000, 1920, 1080, 60, 8000, "x264enc",
        audio="AAC 00000001 00", profile="constrained-baseline", level_idc=42,
    )
    assert f"pat-interval={PAT_INTERVAL_TICKS}" in desc
    assert f"pmt-interval={PMT_INTERVAL_TICKS}" in desc
    assert f"pcr-interval={PCR_INTERVAL_TICKS}" in desc
    assert "rtpmp2tpay pt=33 ssrc=1 perfect-rtptime=false timestamp-offset=0 seqnum-offset=0" in desc
    assert "udpsink host=10.42.0.120 port=19000" in desc
    assert "h264parse name=wfd-h264parse config-interval=-1" in desc
    assert "videorate drop-only=false skip-to-first=true" in desc
    assert "framerate=60/1" in desc
    assert "profile=constrained-baseline" in desc
    assert "alignment=au" in desc
    assert "repeat-headers=1:level=4.2" in desc
    assert "scenecut=0" in desc
    assert "wave=silence" in desc and "avenc_aac" in desc and "stream-format=adts" in desc
    assert "mux.sink_4113" in desc and "mux.sink_4352" in desc
    assert "rtpbin" not in desc and "rtph264pay" not in desc
    shlex.split(desc)


def test_lpcm_branch_and_high_profile():
    desc = build_pipeline(
        pattern_source(1280, 720, 30), "10.0.0.2", 19000, 1280, 720, 30, 4000, "vah264enc",
        audio="LPCM 00000002 00", profile="high",
    )
    assert "audio/x-lpcm" in desc and "join=false" in desc and "avenc_aac" not in desc
    assert "cabac=true" in desc and "dct8x8=true" in desc
    shlex.split(desc)


def test_request_idr_writes_the_runner_pipe():
    sp = StreamPipeline(pattern_source(320, 240, 30), "127.0.0.1", 19000, encoder="x264enc", audio="AAC 00000001 00")

    class _Buf:
        def __init__(self):
            self.buf = ""

        def write(self, text):
            self.buf += text
            return len(text)

        def flush(self):
            pass

    sink = _Buf()
    sp._idr_write = sink
    assert sp.request_idr() is True
    assert sink.buf == "idr\n"
    sp._idr_write = None
    assert sp.request_idr() is False
    assert sp._idr_pending is True


def test_stream_pipeline_accepts_legacy_string():
    sp = StreamPipeline("videotestsrc", "127.0.0.1", 19000, encoder="x264enc")
    assert isinstance(sp.source, CaptureSource)
    assert sp._build_cmd()[:3] == ["gst-launch-1.0", "-e", "-q"]


def test_bitrate_heuristic():
    assert default_bitrate_kbps(1920, 1200, 60) == 13824
    assert default_bitrate_kbps(640, 480, 30) == 4000
    assert default_bitrate_kbps(3840, 2160, 60) == 20000


def test_force_key_unit_posts_upstream_event():
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    from segunda_tela.wfd.gst_run import force_key_unit
    from segunda_tela.wfd.pipeline import gst_element_available

    if not gst_element_available("x264enc"):
        pytest.skip("x264enc")
    Gst.init(None)
    pipeline = Gst.parse_launch(
        "videotestsrc num-buffers=3 ! video/x-raw,width=320,height=240,framerate=30/1 ! "
        "x264enc tune=zerolatency key-int-max=30 bframes=0 ! "
        "h264parse name=wfd-h264parse ! fakesink sync=false"
    )
    pipeline.set_state(Gst.State.PLAYING)
    try:
        assert force_key_unit(pipeline) is True
    finally:
        pipeline.set_state(Gst.State.NULL)


def test_encoded_transport_stream_is_h264_plus_aac(tmp_path):
    """Encode the production graph to a file and confirm both elementary streams."""
    import json
    import re
    import shutil
    import subprocess

    from segunda_tela.wfd.pipeline import gst_element_available

    if not shutil.which("gst-launch-1.0") or not shutil.which("ffprobe"):
        pytest.skip("gst-launch or ffprobe")
    if not gst_element_available("x264enc") or not gst_element_available("avenc_aac"):
        pytest.skip("x264enc/avenc_aac")
    dest = tmp_path / "wfd.ts"
    desc = build_pipeline(
        pattern_source(640, 360, 30), "10.42.0.120", 19000, 640, 360, 30, 1500, "x264enc",
        rtcp_port=19001, audio="AAC 00000001 00", profile="constrained-baseline", level_idc=31,
    )
    desc = re.sub(
        r"udpsink host=\S+ port=\d+ bind-port=\d+ sync=false async=false",
        f"filesink location={dest} sync=false",
        desc,
        count=1,
    )
    assert "rtpbin" not in desc
    proc = subprocess.run(
        ["timeout", "--signal=INT", "3", "gst-launch-1.0", "-e", "-q", *shlex.split(desc)],
        capture_output=True,
        text=True,
        check=False,
    )
    log = proc.stdout + proc.stderr
    assert "CRITICAL" not in log
    assert "mini_object" not in log
    assert dest.exists() and dest.stat().st_size > 2000
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(dest),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    streams = json.loads(probe.stdout)["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")
    audio = next(s for s in streams if s["codec_type"] == "audio")
    assert video["codec_name"] == "h264"
    assert video["width"] == 640 and video["height"] == 360
    assert "Baseline" in video.get("profile", "") or "Constrained" in video.get("profile", "")
    assert video.get("level") == 31
    assert audio["codec_name"] == "aac"
    assert int(audio["sample_rate"]) == 48000
    assert audio["channels"] == 2
