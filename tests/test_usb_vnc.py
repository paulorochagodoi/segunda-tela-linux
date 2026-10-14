"""USB VNC: RFB (Tight/ZRLE/Raw), adb reverse plan, uinput reports, COSMIC layout.

No EVDI device, no tablet, no /dev/uinput. The socket test talks to the
real server on 127.0.0.1 with a fixed challenge.
"""
from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from segunda_tela.cli import build_parser, main
from segunda_tela.display.evdi import EvdiError, EvdiMonitor, ShadowFrames
from segunda_tela.display.randr import Output
from segunda_tela.usb.adb import Adb
from segunda_tela.vnc.auth import des_encrypt_block, random_vnc_password, vnc_key, vnc_response
from segunda_tela.vnc.doctor import vnc_checks
from segunda_tela.vnc.encode import (
    ENCODING_RAW,
    ENCODING_TIGHT,
    ENCODING_ZRLE,
    PixelFormat,
    ZrleDecoder,
    ZrleEncoder,
    coalesce_rects,
    decode_tight,
    encode_rect,
    encode_tight,
    parse_pixel_format,
    pick_encoding,
    pixel_format_bytes,
    solid_rgbx,
)
from segunda_tela.vnc.input import (
    ABS_X,
    EV_ABS,
    UI_ABS_SETUP,
    UI_DEV_SETUP,
    _abs_blob,
    _setup_blob,
    input_event,
    touch_report,
)
from segunda_tela.vnc.layout import (
    UINPUT_NAME,
    apply_position,
    install_touch_map,
    is_extended_right,
    position_commands,
    primary_output,
    target_right_of,
    upsert_touch_map,
)
from segunda_tela.vnc.link import VncLinkError, bvnc_shell, bvnc_uri, launch_bvnc, listen_plan, prepare_vnc_transport
from segunda_tela.vnc.server import VncServer
from segunda_tela.vnc.wayvnc import EXT_IMAGE_COPY, WLR_SCREENCOPY, WLR_VIRTUAL_POINTER, assess_wayvnc, parse_version


def _recv(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError(f"short read {len(buf)}/{n}")
        buf += chunk
    return bytes(buf)


def _rgbx(w: int, h: int, pixel: bytes) -> bytes:
    assert len(pixel) == 4
    return pixel * (w * h)


# ------------------------------------------------------------------- auth / DES

def test_des_fips_vector_and_vnc_key_bits():
    cipher = des_encrypt_block(bytes.fromhex("133457799BBCDFF1"), bytes.fromhex("0123456789ABCDEF"))
    assert cipher.hex() == "85e813540f0ab405"
    assert vnc_key(b"\x01")[0] == 0x80
    chal = bytes(range(16))
    assert vnc_response("secret12", chal) == vnc_response(b"secret12", chal)
    assert vnc_response("secret12", chal) != vnc_response("otherpas", chal)


def test_password_is_eight_safe_chars():
    pw = random_vnc_password()
    assert len(pw) == 8
    assert pw.isascii() and pw.isalnum()


# ------------------------------------------------------------------- encodings

def test_pixel_format_roundtrip_and_preference():
    raw = pixel_format_bytes(PixelFormat.bvnc())
    assert len(raw) == 16
    parsed = parse_pixel_format(raw)
    assert (parsed.red_shift, parsed.green_shift, parsed.blue_shift) == (16, 8, 0)
    assert parsed.cpixel_bytes == 3
    assert pick_encoding([ENCODING_ZRLE, ENCODING_TIGHT, ENCODING_RAW]) == ENCODING_ZRLE
    assert pick_encoding([-313, 7]) == ENCODING_TIGHT
    assert pick_encoding([]) == ENCODING_RAW


def test_tight_solid_and_pattern_roundtrip_bvnc_format():
    fmt = PixelFormat.bvnc()
    red = _rgbx(8, 6, bytes((255, 0, 0, 255)))
    back = decode_tight(encode_tight(red, fmt), 8, 6, fmt)
    assert back == red
    # Not solid: two colours. Must survive B,G,R packing.
    mixed = bytearray(red)
    mixed[0:4] = bytes((0, 255, 10, 255))
    back = decode_tight(encode_tight(bytes(mixed), fmt), 8, 6, fmt)
    assert back == bytes(mixed)


def test_zrle_stream_survives_two_rectangles_and_partial_tiles():
    fmt = PixelFormat.rgbx()
    enc, dec = ZrleEncoder(), ZrleDecoder()
    solid = _rgbx(70, 10, bytes((1, 2, 3, 255)))  # wider than one 64px tile
    out = dec.decode(enc.encode(solid, 70, 10, fmt), 70, 10, fmt)
    assert out == solid
    changed = bytearray(solid)
    changed[4:8] = bytes((9, 8, 7, 255))
    out = dec.decode(enc.encode(bytes(changed), 70, 10, fmt), 70, 10, fmt)
    assert out[4:8] == bytes((9, 8, 7, 255))
    assert out[0:4] == bytes((1, 2, 3, 255))


def test_raw_rect_keeps_rgbx_bytes():
    fmt = PixelFormat.rgbx()
    src = _rgbx(2, 2, bytes((4, 5, 6, 255)))
    enc, payload = encode_rect(src, 2, 2, fmt, ENCODING_RAW)
    assert enc == ENCODING_RAW and payload == src


def test_solid_detector_and_coalesce():
    assert solid_rgbx(b"") is None
    assert solid_rgbx(bytes((1, 2, 3, 4)) * 3) == bytes((1, 2, 3, 4))
    assert solid_rgbx(bytes((1, 2, 3, 4, 1, 2, 3, 5))) is None
    full = coalesce_rects([(0, 0, 10, 10), (10, 0, 20, 10)], 20, 10)
    assert full == [(0, 0, 20, 10)]
    small = coalesce_rects([(1, 1, 2, 2), (4, 4, 5, 5)], 100, 100)
    assert (1, 1, 2, 2) in small and (4, 4, 5, 5) in small
    assert coalesce_rects([], 10, 10) == []


def test_damage_tight_holds_30fps_for_a_window():
    """A 320x180 window (not the whole 1920x1200 frame) is the damage path."""
    fmt = PixelFormat.bvnc()
    w, h = 320, 180
    src = bytearray(w * h * 4)
    for y in range(h):
        src[y * w * 4:(y + 1) * w * 4] = bytes((y & 255, 40, 80, 255)) * w
    t0 = time.perf_counter()
    for i in range(30):
        src[0] = i & 255  # keep it from collapsing to a single fill
        payload = encode_tight(bytes(src), fmt)
        assert len(payload) > 4
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, f"30 damage frames took {elapsed:.3f}s"


# ------------------------------------------------------------------- wayvnc decision

def test_wayvnc_on_pop_2404_cannot_select_the_evdi_output():
    # noble package is 0.7.2; cosmic-comp has ext-image-copy-capture, not wlr-screencopy.
    assessed = assess_wayvnc("wayvnc 0.7.2", {EXT_IMAGE_COPY})
    assert parse_version("wayvnc 0.7.2") == (0, 7, 2)
    assert assessed.use_wayvnc is False
    assert assessed.backend == "evdi"
    assert assessed.capture_possible is False
    text = " ".join(assessed.details)
    assert "wlr-screencopy" in text
    assert "uinput" in text


def test_wayvnc_0_9_can_capture_but_not_feed_uinput():
    assessed = assess_wayvnc(
        "wayvnc 0.9.1",
        {EXT_IMAGE_COPY, WLR_VIRTUAL_POINTER},
    )
    assert assessed.capture_possible is True
    assert assessed.use_wayvnc is False  # pointer must be the EVDI uinput device
    no_pointer = assess_wayvnc("0.9.1", {EXT_IMAGE_COPY})
    assert no_pointer.capture_possible is True
    assert "virtual-pointer" in " ".join(no_pointer.details) or "uinput" in no_pointer.summary
    # wlr-screencopy alone is enough for old wayvnc to name an output.
    old_on_wlroots = assess_wayvnc("0.7.2", {WLR_SCREENCOPY, WLR_VIRTUAL_POINTER})
    assert old_on_wlroots.capture_possible is True
    assert old_on_wlroots.use_wayvnc is False


def test_wayvnc_without_a_session_states_the_cosmic_fact():
    assessed = assess_wayvnc(None, None)
    blob = assessed.summary + " " + " ".join(assessed.details)
    assert "COSMIC" in blob or "cosmic" in blob
    assert assessed.capture_possible is False


# ------------------------------------------------------------------- transport

def test_listen_plan_and_bvnc_intent():
    assert listen_plan("adb-reverse") == (["127.0.0.1"], "127.0.0.1")
    hosts, client = listen_plan("tether", "192.168.42.129")
    assert hosts == ["127.0.0.1", "192.168.42.129"]
    assert client == "192.168.42.129"
    uri = bvnc_uri("127.0.0.1", 5900, "Abcdef12")
    assert uri.startswith("vnc://127.0.0.1:5900?")
    assert "VncPassword=Abcdef12" in uri
    shell = bvnc_shell("127.0.0.1", 5900, "Abcdef12")
    assert "com.iiordanov.freebVNC" in shell
    assert "android.intent.action.VIEW" in shell
    assert "Abcdef12" in shell


def test_prepare_prefers_adb_reverse_over_tether():
    def runner(argv, timeout):
        if "devices" in argv:
            return 0, "List of devices attached\nRX\tdevice\n"
        if "reverse" in argv:
            return 0, ""
        return 1, "no"
    link = prepare_vnc_transport(
        adb=Adb(runner=runner), wait_iface=lambda _s: "usb0", iface_ip=lambda _i: "10.0.0.2",
    )
    try:
        assert link.kind == "adb-reverse"
        assert link.bind_hosts == ["127.0.0.1"]
        assert link.client_host == "127.0.0.1"
        assert link.can_shell is True
    finally:
        link.close()


def test_prepare_falls_back_to_tether_ip():
    def runner(argv, timeout):
        if argv[1:] == ["devices"]:
            return 0, "List of devices attached\nRX\tunauthorized\n"
        return 1, ""
    link = prepare_vnc_transport(
        adb=Adb(runner=runner), wait_iface=lambda _s: "usb0", iface_ip=lambda _i: "192.168.42.129",
    )
    assert link.kind == "tether"
    assert link.client_host == "192.168.42.129"
    assert "127.0.0.1" in link.bind_hosts
    assert link.can_shell is False


def test_prepare_explains_when_nothing_works():
    with pytest.raises(VncLinkError) as exc:
        prepare_vnc_transport(
            adb=Adb(exe="definitely-not-adb"),
            wait_iface=lambda _s: None,
            iface_ip=lambda _i: None,
        )
    assert "Depuração USB" in str(exc.value) or "Tethering" in str(exc.value)


def test_launch_bvnc_redacts_the_password_and_checks_the_package():
    seen = []

    def runner(argv, timeout):
        seen.append(list(argv))
        if "pm list" in " ".join(argv):
            return 0, "package:com.iiordanov.freebVNC uid:1000\n"
        return 0, "Starting: Intent { act=android.intent.action.VIEW }\n"

    adb = Adb(runner=runner)
    ok, _detail = launch_bvnc(adb, "127.0.0.1", 5900, "S3cret99")
    assert ok
    assert any("S3cret99" in part for cmd in seen for part in cmd)
    assert all("S3cret99" not in line for line in adb.log)
    assert any("********" in line for line in adb.log)

    def missing(argv, timeout):
        if "pm list" in " ".join(argv):
            return 0, "package:com.android.settings uid:1000\n"
        return 1, ""

    ok, detail = launch_bvnc(Adb(runner=missing), "127.0.0.1", 5900, "S3cret99")
    assert not ok and "freebVNC" in detail


# ------------------------------------------------------------------- layout / touch

def test_place_evdi_to_the_right_of_edp1():
    edp = Output(name="eDP-1", x=0, y=0, width=1920, height=1200)
    overlap = Output(name="DVI-I-1", x=0, y=0, width=1920, height=1200)
    right = Output(name="DVI-I-1", x=1920, y=0, width=1920, height=1200)
    assert not is_extended_right(overlap, edp)
    assert is_extended_right(right, edp)
    assert target_right_of(edp) == (1920, 0)
    assert primary_output([right, edp], right).name == "eDP-1"
    cmds = position_commands("DVI-I-1", 1920, 0, 1920, 1200)
    assert cmds[0] == ["cosmic-randr", "position", "DVI-I-1", "1920", "0"]
    assert cmds[-1][0] == "wlr-randr"

    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        if argv[1:2] == ["position"]:
            return 127, ""
        return 0, ""

    ok, detail = apply_position("DVI-I-1", 1920, 0, 1920, 1200, runner=runner)
    assert ok and "mode" in detail
    assert calls[0][1] == "position" and calls[1][1] == "mode"


def test_cosmic_touch_map_upsert(tmp_path):
    existing = '{\n"Other Pad": (\nstate: Enabled,\nmap_to_output: Some("eDP-1")\n),\n}\n'
    updated = upsert_touch_map(existing, "DVI-I-1")
    assert 'Some("eDP-1")' in updated
    assert 'Some("DVI-I-1")' in updated
    assert UINPUT_NAME in updated
    again = upsert_touch_map(updated, "DVI-I-2")
    assert again.count(UINPUT_NAME) == 1
    assert 'Some("DVI-I-2")' in again
    assert 'Some("eDP-1")' in again
    path = install_touch_map("HDMI-A-1", path=tmp_path / "input_devices")
    assert 'Some("HDMI-A-1")' in path.read_text()


def test_touch_report_is_a_direct_contact():
    down = touch_report(10, 20, contact=True, tracking_id=3)
    assert len(down) % 24 == 0
    parsed = [struct.unpack_from("=qqHHi", down, i)[2:] for i in range(0, len(down), 24)]
    assert (EV_ABS, ABS_X, 10) in parsed
    assert (EV_ABS, 1, 20) in parsed  # ABS_Y
    up = touch_report(10, 20, contact=False, tracking_id=-1)
    values = [struct.unpack_from("=qqHHi", up, i)[4] for i in range(0, len(up), 24)]
    assert -1 in values
    assert len(input_event(0, 0, 0)) == 24
    assert len(_setup_blob("Segunda Tela VNC")) == 92
    assert len(_abs_blob(0, 0, 1919)) == 28
    # Numbers from linux/uinput.h on amd64 (UI_DEV_SETUP size 92, UI_ABS_SETUP size 28).
    assert UI_DEV_SETUP == 1079792899
    assert UI_ABS_SETUP == 1075598596


def test_evdi_damage_is_handed_out_once():
    mon = EvdiMonitor(b"\x00" * 128)
    mon._slots = ShadowFrames(4, 2)
    assert mon._slots.apply(bytes((255, 0, 0, 255)) * 8, [(0, 0, 4, 2)])
    mon._frame_seq = mon._slots.seq
    mon._damage = [(1, 0, 2, 1)]
    seq, frame, damage = mon.wait_frame_damage(0, 0.01)
    try:
        assert damage == [(1, 0, 2, 1)]
        assert frame is not None and bytes(frame[:4]) == bytes((255, 0, 0, 255))
    finally:
        mon.release_frame()
    seq2, _frame2, damage2 = mon.wait_frame_damage(seq, 0.05)
    mon.release_frame()
    assert seq2 == seq
    assert damage2 == []


# ------------------------------------------------------------------- RFB server

def _handshake(port: int, password: str) -> tuple[socket.socket, int, int]:
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(5)
    assert _recv(sock, 12) == b"RFB 003.008\n"
    sock.sendall(b"RFB 003.008\n")
    ntypes = _recv(sock, 1)[0]
    types = _recv(sock, ntypes)
    assert 2 in types
    sock.sendall(bytes((2,)))
    challenge = _recv(sock, 16)
    sock.sendall(vnc_response(password, challenge))
    result = struct.unpack(">I", _recv(sock, 4))[0]
    assert result == 0
    sock.sendall(bytes((1,)))  # shared
    header = _recv(sock, 24)
    width, height = struct.unpack(">HH", header[:4])
    name = _recv(sock, struct.unpack(">I", header[20:24])[0])
    assert name == b"Segunda Tela"
    return sock, width, height


def _request(sock: socket.socket, incremental: int, width: int, height: int) -> None:
    sock.sendall(struct.pack(">BBHHHH", 3, incremental, 0, 0, width, height))


def _read_raw_update(sock: socket.socket) -> list[tuple[int, int, int, int, bytes]]:
    hdr = _recv(sock, 4)
    assert hdr[0] == 0
    count = struct.unpack(">H", hdr[2:4])[0]
    rects = []
    for _ in range(count):
        x, y, w, h, enc = struct.unpack(">HHHHi", _recv(sock, 12))
        assert enc == ENCODING_RAW
        rects.append((x, y, w, h, _recv(sock, w * h * 4)))
    return rects


def test_rfb_serves_only_damaged_rgbx_and_rejects_a_bad_password():
    pointer = []
    ready = threading.Event()

    def on_pointer(x, y, buttons):
        pointer.append((x, y, buttons))
        ready.set()

    server = VncServer(
        "s3cret99", 32, 24, ["127.0.0.1"], port=0, fps=60,
        on_pointer=on_pointer, challenge_fn=lambda n: b"\x11" * n,
    )
    server.start()
    try:
        sock, width, height = _handshake(server.port, "s3cret99")
        assert (width, height) == (32, 24)
        _request(sock, 1, width, height)
        full = _read_raw_update(sock)
        assert full[0][2:] == (32, 24, bytes(32 * 24 * 4))  # black until the first grab

        src = bytearray(32 * 24 * 4)
        for y in range(8):
            for x in range(8):
                src[(y * 32 + x) * 4:(y * 32 + x) * 4 + 4] = bytes((255, 0, 0, 255))
        server.submit(src, [(0, 0, 8, 8)], 32, 24)
        _request(sock, 1, width, height)
        damaged = _read_raw_update(sock)
        assert damaged, "damage update missing"
        x, y, w, h, pixels = damaged[0]
        assert (x, y) == (0, 0) and w >= 8 and h >= 8
        assert pixels[:4] == bytes((255, 0, 0, 255))
        sock.sendall(struct.pack(">BBHH", 5, 1, 3, 4))
        assert ready.wait(2)
        assert pointer[-1] == (3, 4, 1)
        sock.close()
    finally:
        server.stop()

    bad = VncServer("s3cret99", 8, 8, ["127.0.0.1"], port=0, challenge_fn=lambda n: b"\x22" * n)
    bad.start()
    try:
        sock = socket.create_connection(("127.0.0.1", bad.port), timeout=5)
        sock.settimeout(5)
        _recv(sock, 12)
        sock.sendall(b"RFB 003.008\n")
        ntypes = _recv(sock, 1)[0]
        _recv(sock, ntypes)
        sock.sendall(bytes((2,)))
        challenge = _recv(sock, 16)
        sock.sendall(vnc_response("wrongpas", challenge))
        assert struct.unpack(">I", _recv(sock, 4))[0] == 1
        sock.close()
    finally:
        bad.stop()


# ------------------------------------------------------------------- doctor / cli

def test_doctor_vnc_section(tmp_path):
    node = tmp_path / "uinput"
    node.write_bytes(b"")
    node.chmod(0o600)
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    port = held.getsockname()[1]
    try:
        checks = list(vnc_checks(
            port=port, uinput_path=str(node),
            wayvnc_text="wayvnc 0.7.2", protocols={EXT_IMAGE_COPY}, probe=False,
        ))
    finally:
        held.close()
    by = {label: (st, detail) for st, label, detail in checks}
    assert by["uinput (toque na saída EVDI)"][0] == "ok"
    assert by[f"porta local TCP {port} livre"][0] == "fail"
    assert any("wayvnc" in label or label == "servidor VNC" for label in by)
    node.chmod(0o000)
    try:
        denied = list(vnc_checks(
            port=9, uinput_path=str(node), wayvnc_text=None, protocols=None, probe=False,
        ))
    finally:
        node.chmod(0o600)
    uinput = [c for c in denied if c[1].startswith("uinput")]
    assert uinput and uinput[0][0] == "fail"


def test_cli_usb_vnc_flag_and_conflict(monkeypatch):
    args = build_parser().parse_args(["--usb-vnc", "--width", "1280", "--height", "800", "--fps", "30"])
    assert args.usb_vnc and args.width == 1280 and args.fps == 30
    assert main(["--usb-vnc", "--usb"]) == 2

    def boom(**_k):
        raise EvdiError("no evdi in this test")

    monkeypatch.setattr("segunda_tela.vnc.session.create_virtual_display", boom)
    monkeypatch.setattr("segunda_tela.vnc.session.detect_wayvnc_text", lambda: None)
    monkeypatch.setattr("segunda_tela.vnc.session.detect_protocols", lambda: None)
    assert main(["--usb-vnc"]) == 2
