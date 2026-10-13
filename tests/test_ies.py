from segunda_tela.wfd.ies import build_source_ies, parse_wfd_ies


def test_source_ies_bytes():
    ies = build_source_ies(rtsp_port=7236, max_throughput_mbps=200)
    assert ies == bytes.fromhex("000006 0090 1c44 00c8".replace(" ", ""))
    info = parse_wfd_ies(ies)
    assert info.device_type == 0 and info.type_name == "source"
    assert info.session_available and info.rtsp_port == 7236
    assert not info.is_sink


def test_parse_sink_ies():
    # primary sink, session available, port 7236, 50 Mbps + an extra subelement
    data = bytes.fromhex("0000060011" + "1c44" + "0032") + bytes.fromhex("0700020000")
    info = parse_wfd_ies(data)
    assert info.is_sink and info.type_name == "primary-sink"
    assert info.max_throughput_mbps == 50


def test_parse_vendor_ie_wrapper_and_garbage():
    sub = bytes.fromhex("00000600111c440032")
    wrapped = bytes([0xDD, 4 + len(sub)]) + bytes.fromhex("506f9a0a") + sub
    assert parse_wfd_ies(wrapped).is_sink
    assert parse_wfd_ies(b"") is None
    assert parse_wfd_ies(None) is None
    assert parse_wfd_ies(b"\x00\x00\x06\x00") is None  # truncated
    assert parse_wfd_ies(bytes.fromhex("0700020000")) is None  # no device info
