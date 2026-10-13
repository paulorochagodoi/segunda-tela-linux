from segunda_tela.display.edid import build_edid, cvt_rb, edid_checksum_ok, parse_edid


def test_cvt_rb_matches_vesa_dmt():
    # Reference values from VESA DMT/CVT-RB tables.
    t = cvt_rb(1920, 1200, 60)
    assert (t.pixel_clock_khz, t.h_total, t.v_total) == (154000, 2080, 1235)
    t = cvt_rb(2560, 1600, 60)
    assert (t.pixel_clock_khz, t.h_total, t.v_total) == (268500, 2720, 1646)
    t = cvt_rb(1920, 1080, 60)
    assert (t.pixel_clock_khz, t.h_total, t.v_total) == (138500, 2080, 1111)
    assert abs(t.exact_refresh - 60) < 0.1


def test_build_and_parse_default_edid():
    e = build_edid()
    assert len(e) == 128 and edid_checksum_ok(e)
    assert e[:8] == b"\x00\xff\xff\xff\xff\xff\xff\x00"
    assert e[18:20] == b"\x01\x04"
    info = parse_edid(e)
    assert info.vendor == "SGT"
    assert info.name == "Segunda Tela"
    assert info.preferred == (1920, 1200, 60)
    assert (2560, 1600, 60) in info.modes
    assert info.size_mm == (237, 148)
    # 1920x1200 and 1920x1080 also in standard timings (0xD1 = 1920/8 - 31)
    std = [e[38 + i : 40 + i] for i in range(0, 16, 2)]
    assert bytes([0xD1, 0x00]) in std and bytes([0xD1, 0xC0]) in std


def test_single_mode_edid_and_preferred_first():
    e = build_edid([(2560, 1600, 60)], name="A very long monitor name")
    assert edid_checksum_ok(e)
    info = parse_edid(e)
    assert info.preferred == (2560, 1600, 60)
    assert info.name == "A very long m"  # 13 chars max
    assert parse_edid(b"\x00" * 128) is None
