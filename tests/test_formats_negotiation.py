from segunda_tela.wfd.fakesink import TABLET_VIDEO_FORMATS
from segunda_tela.wfd.formats import (
    FALLBACK_MODE,
    H264_LEVEL_31,
    TABLE_CEA,
    TABLE_VESA,
    find_mode,
    m4_video_descriptor,
    negotiate,
    parse_audio_codecs,
    parse_client_rtp_ports,
    parse_display_edid,
    parse_sink_caps,
    parse_video_formats,
    select_video,
)


def test_parse_tablet_video_formats():
    caps = parse_video_formats(TABLET_VIDEO_FORMATS)
    assert caps is not None
    assert len(caps.codecs) == 2
    assert caps.native_table == TABLE_CEA and caps.native_index == 5
    c = caps.codecs[1]
    assert c.profile == 0x01 and c.level == 0x10
    modes = {(m.width, m.height, m.fps) for m in c.modes()}
    assert (1920, 1200, 60) in modes and (1920, 1080, 60) in modes
    assert (1920, 1080, 60) in modes
    # interlaced CEA entries are never offered
    assert all(not (m.table == TABLE_CEA and m.index in (2, 4, 9, 14)) for m in c.modes())


def test_parse_none_and_garbage():
    assert parse_video_formats("none") is None
    assert parse_video_formats("") is None
    assert parse_video_formats("zz") is None
    assert parse_video_formats("00 00 garbage") is not None  # header ok, codec skipped
    assert parse_video_formats("00 00 garbage").codecs == []


def test_select_prefers_1200p60_for_tablet():
    # The display's own size wins when the sink lists it (session passes this).
    mode, level, profile, _ = select_video(parse_video_formats(TABLET_VIDEO_FORMATS), want=(1920, 1200))
    assert (mode.width, mode.height, mode.fps) == (1920, 1200, 60)
    assert mode.table == TABLE_VESA and mode.index == 29
    assert level == 0x10 and profile == 1


def test_select_without_want_uses_real_sink_native():
    # Fixture native byte 0x28 is CEA #5 (720p30), not the VGA placeholder.
    mode, level, profile, reason = select_video(parse_video_formats(TABLET_VIDEO_FORMATS))
    assert (mode.width, mode.height, mode.fps) == (1280, 720, 30)
    assert "native" in reason
    assert level == 0x10 and profile == 1


def test_select_respects_max_fps_and_want():
    caps = parse_video_formats(TABLET_VIDEO_FORMATS)
    mode, _, _, _ = select_video(caps, max_fps=30, want=(1920, 1200))
    assert (mode.width, mode.height, mode.fps) == (1920, 1200, 30)
    mode, _, _, reason = select_video(caps, want=(1920, 1080))
    assert (mode.width, mode.height, mode.fps) == (1920, 1080, 60)
    assert mode.table == TABLE_CEA and mode.index == 8
    assert "requested" in reason
    # 2560x1600 cannot be signalled in WFD R1 → largest listed mode that fits
    mode, _, _, _ = select_video(caps, want=(2560, 1600))
    assert (mode.width, mode.height) == (1920, 1200)


def test_select_fallbacks():
    assert select_video(None)[0] == FALLBACK_MODE
    assert FALLBACK_MODE.width == 1920 and FALLBACK_MODE.height == 1080
    caps = parse_video_formats("00 00 01 01 00000001 00000000 00000000 00 0000 0000 00 none none")
    mode, _, _, _ = select_video(caps)
    assert (mode.width, mode.height) == (640, 480)  # only the mandatory mode


def test_select_level_byte_does_not_drop_listed_modes():
    # Level 3.1 cannot *carry* 1080p, but the Tab S10 FE lists 1080p with
    # level byte 0x01 anyway. The bitmap wins; M4 echoes 0x01.
    caps = parse_video_formats(f"00 00 01 {H264_LEVEL_31:02x} 000001ff 00000000 00000000 00 0000 0000 00 none none")
    mode, level, profile, _ = select_video(caps, want=(1920, 1080))
    assert (mode.width, mode.height, mode.fps) == (1920, 1080, 60)
    assert level == H264_LEVEL_31 and profile == 1


def test_aspect_preference_from_native():
    # Native is VESA 1680x1050p60 (16:10, index 27): with max_pixels capping at
    # 1080p-ish area, prefer 16:10 over a bigger 16:9.
    caps = parse_video_formats("d9 00 01 10 000001ff 0c000000 00000000 00 0000 0000 00 none none")
    assert caps.native_mode.width == 1680
    mode, _, _, _ = select_video(caps)
    assert (mode.width, mode.height) == (1680, 1050)


def test_m4_descriptor_single_bit():
    d = m4_video_descriptor(find_mode(1920, 1200, 60), 0x10).split()
    assert d[:4] == ["00", "00", "01", "10"]
    assert d[4] == "00000000" and d[5] == "20000000" and d[6] == "00000000"
    d = m4_video_descriptor(find_mode(1920, 1080, 30), 0x04).split()
    assert d[4] == "00000080" and d[5] == "00000000"
    assert len(d) == 13


def test_rtp_ports():
    assert parse_client_rtp_ports("RTP/AVP/UDP;unicast 19000 0 mode=play") == (19000, 0)
    assert parse_client_rtp_ports("RTP/AVP/UDP;unicast 1028 1029 mode=play") == (1028, 1029)
    assert parse_client_rtp_ports("RTP/AVP/UDP;unicast 19000 19000 mode=play") == (19000, 0)
    assert parse_client_rtp_ports("garbage") == (19000, 0)


def test_audio_and_sink_caps():
    a = parse_audio_codecs("LPCM 00000003 00, AAC 00000001 00")
    assert [x.name for x in a] == ["LPCM", "AAC"]
    assert parse_audio_codecs("none") == []
    body = (
        f"wfd_video_formats: {TABLET_VIDEO_FORMATS}\r\n"
        "wfd_audio_codecs: LPCM 00000003 00, AAC 00000001 00\r\n"
        "wfd_client_rtp_ports: RTP/AVP/UDP;unicast 19000 0 mode=play\r\n"
        "wfd_idr_request_capability: 1\r\n"
    )
    caps = parse_sink_caps(body)
    assert caps.rtp_port == 19000 and caps.rtcp_port == 0
    assert caps.supports_aac and caps.idr_request
    n = negotiate(caps)
    assert n.audio == "AAC 00000001 00"  # Samsung rejects a program with no audio
    assert negotiate(caps, audio=False).audio is None
    lpcm = parse_sink_caps("wfd_audio_codecs: LPCM 00000002 00\r\n")
    assert negotiate(lpcm).audio == "LPCM 00000002 00"


def test_display_edid_drives_aspect():
    from segunda_tela.display.edid import build_edid

    edid = build_edid([(2560, 1600, 60)])
    value = "0001 " + edid.hex()
    assert parse_display_edid(value) == edid
    assert parse_display_edid("0002 " + edid.hex()) is None
    caps = parse_sink_caps(
        "wfd_video_formats: 00 00 01 10 0001ffff 3fffffff 00000000 00 0000 0000 00 none none\r\n"
        f"wfd_display_edid: {value}\r\n"
    )
    n = negotiate(caps, max_pixels=1920 * 1080)  # cap forces a choice between aspects
    assert (n.mode.width, n.mode.height) == (1680, 1050)
