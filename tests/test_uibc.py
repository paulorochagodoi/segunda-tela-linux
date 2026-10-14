"""WFD UIBC negotiation, packet decode, and injection onto the EVDI output."""
from __future__ import annotations

import os
import socket
import time
from pathlib import Path

from segunda_tela.display.touch import (
    ABS_MT_POSITION_X,
    ABS_MT_POSITION_Y,
    ABS_MT_TOOL_TYPE,
    ABS_MT_TRACKING_ID,
    BTN_TOOL_PEN,
    DEVICE_NAME,
    EV_ABS,
    EV_KEY,
    MT_TOOL_PEN,
    ContactTracker,
    TouchInjector,
    pack_input_event,
    scale_point,
    to_global,
    uinput_abs_setup,
    uinput_setup_bytes,
    upsert_cosmic_input_map,
    write_cosmic_touch_map,
)
from segunda_tela.wfd.fakesink import run_fake_sink
from segunda_tela.wfd.formats import m3_query_keys
from segunda_tela.wfd.rtsp import WfdRtspServer
from segunda_tela.wfd.uibc import (
    SAMSUNG_UIBC_PORT,
    UibcServer,
    UibcStream,
    decode_hid_touch,
    encode_generic_touch,
    m4_uibc_parameters,
    parse_uibc_capability,
)


SINK_UIBC = (
    "input_category_list=GENERIC, HIDC;"
    "generic_cap_list=SingleTouch, MultiTouch;"
    "hidc_cap_list=SingleTouch/USB, MultiTouch/USB;"
    "port=none"
)


def _hid(*items: tuple[int, int, int]) -> bytes:
    """(prefix_without_size, data, size 0/1/2/4) → a HID descriptor."""
    out = bytearray()
    size_code = {0: 0, 1: 1, 2: 2, 4: 3}
    for prefix, data, size in items:
        out.append(prefix | size_code[size])
        if size:
            out += data.to_bytes(size, "little")
    return bytes(out)


def _stylus_descriptor() -> bytes:
    # Digitizer / Stylus application collection: tip (1 bit) + pad, then X and Y.
    usage_page, usage, collection, end, logical_min, logical_max = 0x04, 0x08, 0xA0, 0xC0, 0x14, 0x24
    report_size, report_count, input_item = 0x74, 0x94, 0x80
    return _hid(
        (usage_page, 0x0D, 1),
        (usage, 0x20, 1),          # Stylus
        (collection, 0x01, 1),
        (usage_page, 0x0D, 1),
        (usage, 0x42, 1),          # Tip switch
        (logical_min, 0, 1),
        (logical_max, 1, 1),
        (report_size, 1, 1),
        (report_count, 1, 1),
        (input_item, 0x02, 1),     # data, var, abs
        (report_count, 7, 1),
        (input_item, 0x03, 1),     # constant pad
        (usage_page, 0x01, 1),     # Generic Desktop
        (usage, 0x30, 1),          # X
        (logical_max, 4095, 2),
        (report_size, 16, 1),
        (report_count, 1, 1),
        (input_item, 0x02, 1),
        (usage, 0x31, 1),          # Y
        (input_item, 0x02, 1),
        (end, 0, 0),
    )


def test_m3_asks_uibc_only_when_touch_is_on():
    plain = m3_query_keys()
    assert "wfd_uibc_capability" not in plain
    touched = m3_query_keys(touch=True)
    assert touched[-1] == "wfd_uibc_capability"
    assert m3_query_keys(probe_hdcp=True, touch=True)[-1] == "wfd_content_protection"


def test_m4_selects_touch_and_omits_it_when_the_sink_has_none():
    caps = parse_uibc_capability(SINK_UIBC)
    assert caps.usable
    lines = m4_uibc_parameters(caps, 50000)
    assert lines[1] == "wfd_uibc_setting: enable"
    body = lines[0]
    assert "input_category_list=GENERIC, HIDC" in body
    assert "generic_cap_list=SingleTouch, MultiTouch" in body
    assert "hidc_cap_list=SingleTouch/USB, MultiTouch/USB" in body
    assert "port=50000" in body
    assert m4_uibc_parameters(parse_uibc_capability("none"), 50000) == []
    mouse = parse_uibc_capability(
        "input_category_list=GENERIC;generic_cap_list=Mouse;hidc_cap_list=none;port=none"
    )
    assert m4_uibc_parameters(mouse, 50000) == []


def test_generic_multitouch_roundtrip_and_samsung_length_prefix():
    packet = encode_generic_touch("down", [(0, 640, 360), (1, 10, 20)], timestamp=0x1234)
    stream = UibcStream()
    events = stream.feed(packet + packet)
    assert len(events) == 2
    assert events[0].action == "down" and events[0].timestamp == 0x1234
    assert [(p.pointer_id, p.x, p.y, p.tool) for p in events[0].pointers] == [
        (0, 640, 360, "finger"),
        (1, 10, 20, "finger"),
    ]
    up = encode_generic_touch("up", [(0, 640, 360)])
    prefixed = len(up).to_bytes(2, "big") + up
    again = UibcStream().feed(prefixed)
    assert again[0].action == "up" and again[0].pointers[0].down is False


def test_hidc_stylus_report_is_an_s_pen():
    desc = _stylus_descriptor()
    # tip down, X=100, Y=50, little-endian after the 8-bit tip/pad.
    report = bytes((0x01, 100, 0, 50, 0))
    pointers = decode_hid_touch(desc, report)
    assert len(pointers) == 1
    assert pointers[0].tool == "pen"
    assert (pointers[0].x, pointers[0].y) == (100, 50)
    lifted = decode_hid_touch(desc, bytes(5))
    assert lifted == []


def test_uibc_server_receives_on_the_negotiated_port_and_the_fallback():
    seen: list = []
    server = UibcServer(seen.append, host="127.0.0.1", advertise_port=0, samsung_port=0)
    port = server.start()
    try:
        assert port > 0 and port != SAMSUNG_UIBC_PORT
        _send(port, encode_generic_touch("move", [(3, 8, 9)]))
        deadline = time.monotonic() + 2
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
        assert seen and seen[0].action == "move" and seen[0].pointers[0].pointer_id == 3
        server.set_enabled(False)
        _send(port, encode_generic_touch("down", [(0, 1, 1)]))
        time.sleep(0.2)
        assert len(seen) == 1
        server.allow("10.9.8.7")
        assert server._allow is not None and "127.0.0.1" in server._allow and "10.9.8.7" in server._allow
    finally:
        server.stop()


def _two_ports() -> tuple[int, int]:
    first, second = socket.socket(), socket.socket()
    first.bind(("127.0.0.1", 0))
    second.bind(("127.0.0.1", 0))
    ports = first.getsockname()[1], second.getsockname()[1]
    first.close()
    second.close()
    return ports


def test_samsung_port_is_a_second_listener():
    seen: list = []
    negotiated, fallback = _two_ports()
    server = UibcServer(
        seen.append, host="127.0.0.1", advertise_port=negotiated, samsung_port=fallback,
    )
    port = server.start()
    try:
        assert port != fallback
        _send(fallback, encode_generic_touch("down", [(1, 4, 5)]))
        deadline = time.monotonic() + 2
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
        assert seen and seen[0].pointers[0].x == 4
    finally:
        server.stop()


def _send(port: int, payload: bytes) -> None:
    sock = socket.create_connection(("127.0.0.1", port), timeout=2)
    sock.sendall(payload)
    time.sleep(0.05)
    sock.close()


def test_scale_maps_720p_touch_onto_the_evdi_mode():
    assert scale_point(640, 360, 1280, 720, 1920, 1080) == (960, 540)
    assert scale_point(0, 0, 1280, 720, 1920, 1200) == (0, 0)
    assert to_global(10, 20, 1920, 0) == (1930, 20)
    tracker = ContactTracker()
    from segunda_tela.wfd.uibc import TouchEvent, Pointer

    frame = tracker.events(
        TouchEvent("down", (Pointer(0, 640, 360, "pen"), Pointer(1, 0, 0, "finger"))),
        1280, 720, 1920, 1080,
    )
    xs = [value for kind, code, value in frame if kind == EV_ABS and code == ABS_MT_POSITION_X]
    ys = [value for kind, code, value in frame if kind == EV_ABS and code == ABS_MT_POSITION_Y]
    tools = [value for kind, code, value in frame if kind == EV_ABS and code == ABS_MT_TOOL_TYPE]
    tracks = [value for kind, code, value in frame if kind == EV_ABS and code == ABS_MT_TRACKING_ID]
    assert xs == [960, 0] and ys == [540, 0]
    assert tools == [MT_TOOL_PEN, 0]
    assert tracks == [1, 2]
    assert (EV_KEY, BTN_TOOL_PEN, 1) in frame
    released = tracker.events(TouchEvent("up", (Pointer(0, 640, 360, "pen", down=False),)), 1280, 720, 1920, 1080)
    assert (EV_ABS, ABS_MT_TRACKING_ID, -1) in released
    assert len(pack_input_event(EV_ABS, ABS_MT_POSITION_X, 960)) == 24


def test_uinput_setup_blobs_and_optional_device():
    assert len(uinput_setup_bytes(DEVICE_NAME)) == 92
    assert len(uinput_abs_setup(ABS_MT_POSITION_X, 0, 1919)) == 28
    assert uinput_setup_bytes(DEVICE_NAME).startswith(b"\x06\x00")
    injector = TouchInjector(device_path="/dev/uinput")
    if not os.access("/dev/uinput", os.W_OK):
        assert injector.configure(1920, 1080) is False
        return
    assert injector.configure(320, 240) is True
    injector.close()
    assert injector.open is False


def test_cosmic_map_keeps_other_devices(tmp_path: Path):
    existing = '{\n    "ILITEK": (\n        state: Enabled,\n        map_to_output: Some("eDP-1"),\n    ),\n}\n'
    text = upsert_cosmic_input_map(existing, DEVICE_NAME, "DVI-I-1")
    assert 'map_to_output: Some("eDP-1")' in text
    assert 'map_to_output: Some("DVI-I-1")' in text
    again = upsert_cosmic_input_map(text, DEVICE_NAME, "DP-2")
    assert again.count(DEVICE_NAME) == 1
    assert 'Some("DP-2")' in again and 'Some("DVI-I-1")' not in again
    path = write_cosmic_touch_map("DVI-I-1", tmp_path / "input_devices")
    assert "Segunda Tela Touch" in path.read_text()
    rule = Path("packaging/udev/71-segunda-tela-uinput.rules").read_text()
    assert 'KERNEL=="uinput"' in rule
    assert 'ENV{ID_INPUT_TOUCHSCREEN}="1"' in rule
    assert 'ENV{LIBINPUT_CALIBRATION_MATRIX}="1 0 0 0 1 0"' in rule
    assert 'ATTR{name}=="Segunda Tela Touch"' in rule
    script = Path("scripts/setup-evdi.sh").read_text()
    assert "71-segunda-tela-uinput.rules" in script


class _Uibc:
    def __init__(self) -> None:
        self.enabled = True
        self.allowed: list[str] = []

    def allow(self, host: str) -> None:
        self.allowed.append(host)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled


def test_rtsp_m4_advertises_uibc_and_m15_can_disable_it():
    gate = _Uibc()
    srv = WfdRtspServer(
        host="127.0.0.1", port=17321, advertise_host="127.0.0.1", m1_delay_s=0.1,
        touch=True, uibc_port=50000, uibc=gate,
    )
    srv.start_background()
    try:
        res = run_fake_sink("127.0.0.1", 17321, uibc_capability=SINK_UIBC)
    finally:
        srv.stop()
    keys = [line.strip() for line in res.m3.body.splitlines() if line.strip()]
    assert keys[-1] == "wfd_uibc_capability"
    assert "port=50000" in res.m4_params["wfd_uibc_capability"]
    assert res.m4_params["wfd_uibc_setting"] == "enable"
    assert "wfd_content_protection" not in res.m4.body
    assert gate.allowed == ["127.0.0.1"]

    gate2 = _Uibc()
    srv = WfdRtspServer(
        host="127.0.0.1", port=17322, advertise_host="127.0.0.1", m1_delay_s=0.1,
        touch=True, uibc_port=50000, uibc=gate2,
    )
    srv.start_background()
    try:
        res = run_fake_sink("127.0.0.1", 17322, uibc_capability="none")
    finally:
        srv.stop()
    assert "wfd_uibc_capability" in res.m3.body
    assert "wfd_uibc_capability" not in res.m4_params
    assert gate2.allowed == []


def test_sink_can_disable_uibc_after_play():
    from segunda_tela.wfd.fakesink import FakeSink

    gate = _Uibc()
    srv = WfdRtspServer(
        host="127.0.0.1", port=17323, advertise_host="127.0.0.1", m1_delay_s=0.1,
        touch=True, uibc_port=50000, uibc=gate,
    )
    srv.start_background()
    sink = FakeSink("127.0.0.1", 17323)
    try:
        m1 = sink.expect_request("OPTIONS")
        sink.reply(m1, {"Public": "org.wfa.wfd1.0, SET_PARAMETER, GET_PARAMETER"})
        sink.request("OPTIONS", "*", {"Require": "org.wfa.wfd1.0"})
        m3 = sink.expect_request("GET_PARAMETER")
        sink.reply(m3, body=(
            "wfd_video_formats: none\r\n"
            "wfd_audio_codecs: none\r\n"
            "wfd_client_rtp_ports: RTP/AVP/UDP;unicast 19000 0 mode=play\r\n"
            f"wfd_uibc_capability: {SINK_UIBC}\r\n"
        ))
        m4 = sink.expect_request("SET_PARAMETER")
        assert "port=50000" in m4.body
        sink.reply(m4)
        m5 = sink.expect_request("SET_PARAMETER")
        sink.reply(m5)
        setup = sink.request(
            "SETUP", "rtsp://127.0.0.1/wfd1.0/streamid=0",
            {"Transport": "RTP/AVP/UDP;unicast;client_port=19000-19001"},
        )
        session = setup.headers["session"].split(";")[0]
        sink.request("PLAY", "rtsp://127.0.0.1/wfd1.0", {"Session": session})
        reply = sink.request(
            "SET_PARAMETER", "rtsp://localhost/wfd1.0",
            {"Session": session, "Content-Type": "text/parameters"},
            body="wfd_uibc_setting: disable\r\n",
        )
        assert reply.status == 200
        assert gate.enabled is False
    finally:
        sink.close()
        srv.stop()


def test_cli_no_touch_flag():
    from segunda_tela.cli import build_parser

    assert build_parser().parse_args([]).no_touch is False
    assert build_parser().parse_args(["--no-touch"]).no_touch is True


def test_injector_logs_without_a_device(caplog):
    injector = TouchInjector(device_path="/dev/does-not-exist-uinput")
    injector.set_stream(1280, 720)
    injector.out_w, injector.out_h = 1920, 1080
    from segunda_tela.wfd.uibc import Pointer, TouchEvent

    with caplog.at_level("INFO"):
        injector.handle(TouchEvent("down", (Pointer(0, 1, 2),)))
    assert injector.events == 1
    assert any("touch down" in rec.message for rec in caplog.records)
