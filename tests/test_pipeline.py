import shlex

import pytest

from segunda_tela.wfd.pipeline import (
    CaptureSource,
    StreamPipeline,
    adapt_fragment,
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
    both = lambda n: n in {"vah264enc", "x264enc"}
    assert pick_encoder(available=both, allow_hw=False) == "x264enc"
    assert pick_encoder("vah264enc", available=both, allow_hw=False) == "x264enc"


def test_encoder_fragments():
    va = encoder_fragment("vah264enc", 4096, 30)
    assert "qos=false" in va and "key-int-max=30" in va and "bitrate=4096" in va
    assert "rate-control=cbr" in va and "target-usage=7" in va
    assert "b-frames=0" in va and "ref-frames=1" in va
    assert "cabac=false" in va and "dct8x8=false" in va and "num-slices=1" in va
    assert "max-bframes=0" in encoder_fragment("vaapih264enc", 4096, 30)
    x264 = encoder_fragment("x264enc", 4096, 30, level_idc=31)
    assert "pass=quant" in x264 and "tune=zerolatency" in x264 and "speed-preset=ultrafast" in x264
    assert 'option-string="level=3.1"' in x264
    assert "level=" not in encoder_fragment("x264enc", 4096, 30)
    oh = encoder_fragment("openh264enc", 4096, 30)
    assert "usage-type=screen" in oh and "enable-frame-skip=false" in oh and "gop-size=30" in oh


def test_wfd_transport_is_mpegts_over_rtp():
    desc = build_pipeline(pattern_source(1920, 1200, 60), "192.168.49.2", 19000, 1920, 1200, 60, 12000, "vah264enc")
    assert "mpegtsmux name=mux alignment=7" in desc
    assert "mux.sink_4113" in desc  # PID 0x1011
    assert "rtpmp2tpay pt=33" in desc
    assert "udpsink host=192.168.49.2 port=19000" in desc
    assert "rtph264pay" not in desc
    assert "rtpbin" not in desc  # sink sent RTCP port 0
    assert "constrained-baseline" in desc
    assert "framerate=60/1" in desc and "width=1920,height=1200" in desc
    assert "videorate drop-only=true" in desc and "skip-to-first" not in desc
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
    src = CaptureSource(gst="pipewiresrc fd=10 path=91 do-timestamp=true keepalive-time=16 ! videoconvert")
    desc = build_pipeline(
        src, "10.42.0.120", 19000, 1280, 720, 30, 4096, "x264enc",
        rtcp_port=19001, audio="AAC 00000001 00", profile="constrained-baseline", level_idc=31,
    )
    assert "keepalive-time" not in desc
    assert "pat-interval" not in desc and "pmt-interval" not in desc and "pcr-interval" not in desc
    assert "mpegtsmux name=mux alignment=7" in desc
    assert "rtpmp2tpay pt=33 ssrc=1 perfect-rtptime=false timestamp-offset=0 seqnum-offset=0" in desc
    assert "udpsink host=10.42.0.120 port=19000" in desc and "sync=false" in desc
    assert "h264parse name=wfd-h264parse config-interval=-1" in desc
    assert "videorate drop-only=true" in desc
    assert "videoscale n-threads=" in desc and "format=NV12" in desc
    assert "latency=0" in desc and "leaky=downstream" in desc
    assert "framerate=30/1" in desc and "width=1280,height=720" in desc
    assert "profile=(string){constrained-baseline,baseline}" in desc
    assert "pass=quant" in desc and 'option-string="level=3.1"' in desc
    assert "identity !" in desc
    assert "wave=silence" in desc and "avenc_aac" in desc and "audioresample ! audioconvert" in desc
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


def test_adapt_uses_vapostproc_for_va_and_i420_for_openh264():
    gpu = adapt_fragment("vah264enc", 1280, 720, 30, available=lambda name: name == "vapostproc")
    assert "vapostproc" in gpu and "format=NV12,width=1280,height=720" in gpu
    assert "videoconvert" not in gpu and "videoscale" not in gpu
    assert gpu.index("framerate=30/1") < gpu.index("vapostproc")
    soft = adapt_fragment("openh264enc", 1280, 720, 30, available=lambda _name: False)
    assert "format=I420" in soft and "videoconvert n-threads=" in soft
    assert "videoscale" in soft and soft.index("videorate") < soft.index("videoscale")


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
