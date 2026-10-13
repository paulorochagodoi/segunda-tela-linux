from segunda_tela.wfd.rtsp import (
    H264_LEVEL_32,
    H264_LEVEL_40,
    H264_LEVEL_42,
    cea_bitmap,
    cea_native_index,
    h264_level_for,
    requested_keys,
    wfd_audio_codecs,
    wfd_video_formats,
)


def test_cea_1080p30():
    assert cea_native_index(1920, 1080, 30) == 7
    mask = cea_bitmap(1920, 1080, 30)
    assert mask & (1 << 7) and mask & 1
    assert not mask & (1 << 8)  # no 1080p60
    assert not mask & (1 << 9)  # never advertise interlaced


def test_cea_1080p60():
    assert cea_native_index(1920, 1080, 60) == 8
    assert cea_bitmap(1920, 1080, 60) & (1 << 8)


def test_levels():
    assert h264_level_for(1280, 720, 60) == H264_LEVEL_32
    assert h264_level_for(1920, 1080, 30) == H264_LEVEL_40
    assert h264_level_for(1920, 1080, 60) == H264_LEVEL_42


def test_video_formats_layout():
    v = wfd_video_formats(1920, 1080, 60)
    fields = v.split()
    assert len(fields) == 13
    assert fields[0] == "40"  # CEA index 8 << 3
    assert fields[3] == "10"  # level 4.2
    assert len(fields[4]) == 8


def test_audio_and_keys():
    assert wfd_audio_codecs().startswith("LPCM 00000003 00")
    assert requested_keys("wfd_video_formats\r\nwfd_audio_codecs\r\n") == [
        "wfd_video_formats",
        "wfd_audio_codecs",
    ]
    assert requested_keys("") == []
