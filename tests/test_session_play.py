"""PLAY must build the WFD pipeline from an EVDI grab without touching capture code."""

from segunda_tela.display.virtual import VirtualDisplay
from segunda_tela.session import SecondScreenSession
from segunda_tela.wfd.formats import H264_LEVEL_31, Negotiated, find_mode
from segunda_tela.wfd.pipeline import default_bitrate_kbps
from segunda_tela.wfd.rtsp import SessionState


class _Monitor:
    """Stand-in for EvdiMonitor: a mode is set and frames are a solid buffer."""

    def __init__(self, width: int, height: int, fps: int) -> None:
        self.mode = (width, height, fps)
        self.fps = fps

    def wait_frame(self, after_seq: int, timeout: float):
        return after_seq + 1, bytes(self.mode[0] * self.mode[1] * 4)


def test_on_play_evdi_grab_uses_negotiated_bitrate():
    width, height = 320, 240
    session = SecondScreenSession(encoder="x264enc", fps=60)
    session.display = VirtualDisplay(
        width, height, "extend", "evdi-grab", fps=60, evdi=_Monitor(width, height, 60)
    )
    mode = find_mode(1280, 720, 30)
    assert mode is not None
    negotiated = Negotiated(
        mode, level=H264_LEVEL_31, audio="AAC 00000001 00",
        stream_level=H264_LEVEL_31, bitrate_kbps=4096,
    )
    state = SessionState(remote="10.42.0.120", negotiated=negotiated)
    try:
        session._on_play(state)
        pipe = session.pipeline
        assert pipe is not None
        assert pipe.bitrate_kbps == 4096
        assert pipe.level_idc == 31
        assert pipe.audio == "AAC 00000001 00"
        assert pipe.source.backend == "evdi-grab"
        assert pipe.source.stdin_feeder is not None
        desc = pipe.description
        assert "fdsrc fd=0" in desc
        assert "rawvideoparse" in desc and "format=rgbx" in desc
        assert "framerate=30/1" in desc
        assert session.display.fps == 30 and session.display.evdi.fps == 30
        assert f"width={width}" in desc and f"height={height}" in desc
        assert "keepalive-time" not in desc and "rtpbin" not in desc
        assert pipe.running()
    finally:
        session.stop()


def test_on_play_without_negotiation_still_starts():
    session = SecondScreenSession(encoder="x264enc", fps=30)
    session.display = VirtualDisplay(320, 240, "extend", "test", fps=30)
    state = SessionState(remote="10.42.0.120", width=1280, height=720, fps=30)
    try:
        session._on_play(state)
        assert session.pipeline is not None
        assert session.pipeline.bitrate_kbps == default_bitrate_kbps(1280, 720, 30)
        assert session.pipeline.audio is None
    finally:
        session.stop()
