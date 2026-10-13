import socket
import struct
import threading

import pytest

from segunda_tela.usb.adb import Adb, Listener
from segunda_tela.usb.detect import UsbDevice
from segunda_tela.usb.doctor import usb_checks
from segunda_tela.usb.initiate import UsbInitiator, dial_tcp, source_ready_targets
from segunda_tela.usb.link import UsbLink
from segunda_tela.usb.samsung import (
    CABLE_PORTS,
    MICE_SOURCE_READY,
    MICE_STOP_PROJECTION,
    TLV_FRIENDLY_NAME,
    TLV_RTSP_PORT,
    TLV_SOURCE_ID,
    config_json,
    config_path,
    installed_hosts,
    mice_source_ready,
    mice_stop_projection,
    parse_mice,
)
from segunda_tela.wfd.fakesink import run_fake_sink
from segunda_tela.wfd.rtsp import WfdRtspServer

SID = bytes(range(16))


def test_cable_port_map_matches_windows_app():
    got = {(p.name, p.port, p.proto, p.pc_role) for p in CABLE_PORTS}
    assert got == {
        ("side-channel", 7260, "tcp", "listen"), ("source-ready", 7250, "tcp", "connect"),
        ("rtsp", 7236, "tcp", "listen"), ("rtp", 19000, "udp", "send"), ("rtcp", 19001, "udp", "send"),
        ("uibc", 50000, "tcp", "listen"), ("hw-cursor", 50001, "udp", "send"),
        ("hw-cursor-2", 50002, "udp", "send"), ("hdcp", 25030, "tcp", "connect"),
    }
    assert [p.name for p in CABLE_PORTS if p.required] == ["side-channel", "source-ready", "rtsp", "rtp", "rtcp"]


def test_hosts_and_config():
    pk = ["com.android.chrome", "com.samsung.android.smartmirroring", "com.samsung.android.secondscreen"]
    assert installed_hosts(pk) == ["com.samsung.android.secondscreen", "com.samsung.android.smartmirroring"]
    assert config_path("com.samsung.android.secondscreen") == \
        "/sdcard/Android/data/com.samsung.android.secondscreen/files/config/secondscreen.data"
    assert config_json() == '{"USB":1}' and config_json(False) == '{"USB":0}'


def test_mice_source_ready_layout():
    msg = mice_source_ready("pc", 7236, SID)
    # header: size, version 1, command SOURCE_READY
    assert struct.unpack_from(">HBB", msg) == (len(msg), 1, MICE_SOURCE_READY)
    # first TLV: friendly name, UTF-16LE
    assert msg[4:7] == bytes([TLV_FRIENDLY_NAME, 0, 4]) and msg[7:11] == "pc".encode("utf-16-le")
    cmd, tlvs = parse_mice(msg)
    assert cmd == MICE_SOURCE_READY
    assert tlvs[TLV_FRIENDLY_NAME].decode("utf-16-le") == "pc"
    assert tlvs[TLV_RTSP_PORT] == b"\x1c\x44"  # 7236 big-endian
    assert tlvs[TLV_SOURCE_ID] == SID
    assert parse_mice(mice_stop_projection("pc", SID))[0] == MICE_STOP_PROJECTION


def test_mice_rejects_bad_input():
    with pytest.raises(ValueError):
        mice_source_ready("pc", 7236, b"short")
    msg = mice_source_ready("pc", 7236, SID)
    with pytest.raises(ValueError):
        parse_mice(msg[:-1])
    with pytest.raises(ValueError):
        parse_mice(b"\x00\x04\x02\x01")  # wrong version


def test_source_ready_targets():
    assert [(t.host, t.via) for t in source_ready_targets("10.0.0.1", [], adb_ok=True)] == [
        ("127.0.0.1", "adb-forward"), ("10.0.0.1", "tether")]
    only_lo = [Listener("127.0.0.1", 7250, 10300)]
    assert [t.via for t in source_ready_targets("10.0.0.1", only_lo, adb_ok=True)] == ["adb-forward"]
    assert source_ready_targets("", only_lo, adb_ok=False) == []
    assert all(t.port == 7250 for t in source_ready_targets("10.0.0.1", [], adb_ok=True))


class FakeSamsungTablet:
    """Serves source-ready like the Second Screen app: on SOURCE_READY, connects back to
    the RTSP port it names (on the address it was reached from) and acts as the WFD sink."""

    def __init__(self):
        self.ls = socket.socket()
        self.ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ls.bind(("127.0.0.1", 0))
        self.ls.listen(1)
        self.port = self.ls.getsockname()[1]
        self.messages, self.results = [], []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn, (peer, _) = self.ls.accept()
        try:
            head = conn.recv(4)
            size = struct.unpack(">H", head[:2])[0]
            data = head
            while len(data) < size:
                data += conn.recv(size - len(data))
            cmd, tlvs = parse_mice(data)
            self.messages.append((cmd, tlvs))
            rtsp_port = struct.unpack(">H", tlvs[TLV_RTSP_PORT])[0]
            self.results.append(run_fake_sink(peer, rtsp_port))
        except Exception as exc:  # noqa: BLE001
            self.results.append(exc)
        finally:
            conn.close()
            self.ls.close()


class FakeSamsungAdb(Adb):
    def __init__(self, tablet_port):
        super().__init__(runner=self._run)
        self.tablet_port = tablet_port
        self.cmds = []

    def _run(self, argv, timeout):
        cmd = " ".join(argv[1:])
        self.cmds.append(cmd)
        if argv[1:] == ["devices"]:
            return 0, "List of devices attached\nR52W\tdevice\n"
        if "pm list packages" in cmd:
            return 0, ("package:com.samsung.android.secondscreen uid:10301\n"
                       "package:com.samsung.android.smartmirroring uid:10111\n")
        if "printf" in cmd:
            return 0, "WROTE\n"
        if "monkey" in cmd:
            return 0, "Events injected: 1\n"
        if argv[-2:] == ["tcp:0", "tcp:7250"]:
            return 0, f"{self.tablet_port}\n"
        return 0, ""


def test_initiator_source_ready_handshake_over_adb():
    events, said = [], []
    tablet = FakeSamsungTablet()
    srv = WfdRtspServer(host="127.0.0.1", port=17330, advertise_host="127.0.0.1", m1_delay_s=0.1,
                        on_play=lambda st: events.append(("play", st.remote, st.rtp_host, st.rtp_port)))
    srv.start_background()
    adb = FakeSamsungAdb(tablet.port)
    link = UsbLink(transport="ncm", iface="enx0", local_ip="10.108.120.2", peer_ip="10.108.120.1",
                   devices=[UsbDevice("04e8", "6860", "R52W", "/sys/x")])
    # Tether peer unreachable in this sandbox → only the adb-forward path works.
    dial = lambda h, p, t: dial_tcp(h, p, t) if h == "127.0.0.1" else (None, "timeout")
    ini = UsbInitiator(srv, link, rtsp_port=17330, adb=adb, timeout_s=10, retry_s=0.2, say=said.append,
                       dial=dial, settle_s=4.0, friendly_name="linux-pc")
    try:
        res = ini.run()
        for _ in range(50):
            if tablet.results:
                break
            threading.Event().wait(0.1)
    finally:
        ini.stop()
        srv.stop()
    cmds = " | ".join(adb.cmds)
    assert "reverse tcp:17330 tcp:17330" in cmds
    # Cable flag written where the Windows app puts it (new package first), legacy too.
    assert "/sdcard/Android/data/com.samsung.android.secondscreen/files/config/secondscreen.data" in cmds
    assert '{"USB":1}' in cmds
    assert res.configured == ["com.samsung.android.secondscreen", "com.samsung.android.smartmirroring"]
    # Opens the new app, never Smart View's launcher.
    assert "monkey -p com.samsung.android.secondscreen" in cmds and "monkey -p com.samsung.android.smartmirroring" not in cmds
    cmd, tlvs = tablet.messages[0]
    assert cmd == MICE_SOURCE_READY and tlvs[TLV_FRIENDLY_NAME].decode("utf-16-le") == "linux-pc"
    assert struct.unpack(">H", tlvs[TLV_RTSP_PORT])[0] == 17330
    assert res.connected is not None and res.connected.via == "adb-forward→tablet:7250"
    assert tablet.results and not isinstance(tablet.results[0], Exception), tablet.results
    # Sink came in via loopback (adb reverse model) → RTP still aimed at the tether IP.
    assert events == [("play", "127.0.0.1", "10.108.120.1", 19000)]


def test_doctor_reports_second_screen_and_source_ready(tmp_path):
    d = tmp_path / "1-2"
    d.mkdir()
    (d / "idVendor").write_text("04e8\n")
    (d / "idProduct").write_text("6860\n")
    (d / "serial").write_text("R52W\n")

    def runner(argv, timeout):
        cmd = " ".join(argv[1:])
        if cmd == "devices":
            return 0, "List of devices attached\nR52W\tdevice\n"
        if "pm list packages" in cmd:
            return 0, "package:com.samsung.android.secondscreen uid:10301\n"
        if "cat '/sdcard/Android/data/com.samsung.android.secondscreen" in cmd:
            return 0, '{"USB":1}'
        if "/proc/net/tcp" in cmd:
            return 0, ("   0: 00000000:1C52 00000000:0000 0A 00000000:00000000 00:00000000 00000000 "
                       "10301        0 1 1\n")
        return 0, ""

    checks = list(usb_checks(rtsp_port=17331, adb=Adb(runner=runner), sys_bus_usb=str(tmp_path),
                             ifaces=["enx0"], iface_ip=lambda i: "10.108.120.2",
                             probe=lambda h, p, t: "open" if p == 7250 else "refused"))
    by = {label: (st, detail) for st, label, detail in checks}
    assert by["Segunda tela Samsung (pacote)"] == ("ok", "com.samsung.android.secondscreen")
    assert by["config USB com.samsung.android.secondscreen"] == ("info", '{"USB":1}')
    assert by["tablet escutando source-ready :7250"] == ("ok", "0.0.0.0:7250")
    assert by["source-ready 10.108.120.1:7250"] == ("ok", "open")
    assert "dica" not in by
