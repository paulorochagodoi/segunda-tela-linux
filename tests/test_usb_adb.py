from segunda_tela.usb.adb import (
    Adb,
    parse_adb_devices,
    parse_dumpsys_activities,
    parse_ip_addrs,
    parse_packages_uid,
    parse_proc_net_tcp,
    parse_qs_tiles,
    same_subnet,
    sink_packages,
)

PROC_TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:1C44 00000000:0000 0A 00000000:00000000 00:00000000 00000000 10234        0 1 1
   1: 0100007F:13AD 00000000:0000 0A 00000000:00000000 00:00000000 00000000  2000        0 2 1
   2: 027A6C0A:A1B2 017A6C0A:1C44 01 00000000:00000000 00:00000000 00000000 10234        0 3 1
  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000000000000:1C52 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000 10234        0 4 1
   1: 0000000000000000FFFF00000131A8C0:2000 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000 10300        0 5 1
"""


def test_parse_adb_devices():
    out = "* daemon started successfully\nList of devices attached\nRXGYB09YJXJ\tunauthorized\nemulator-5554\tdevice\n\n"
    assert parse_adb_devices(out) == [("RXGYB09YJXJ", "unauthorized"), ("emulator-5554", "device")]


def test_parse_proc_net_tcp_listeners_only():
    ls = parse_proc_net_tcp(PROC_TCP)
    got = [(l.addr, l.port, l.uid) for l in ls]
    assert got == [
        ("0.0.0.0", 7236, 10234),
        ("127.0.0.1", 5037, 2000),
        ("::", 7250, 10234),
        ("192.168.49.1", 8192, 10300),
    ]
    assert ls[0].wildcard and ls[1].loopback_only and not ls[3].wildcard


def test_packages_and_sink_split():
    pk = parse_packages_uid(
        "package:com.samsung.android.smartmirroring uid:10111\n"
        "package:com.example.wfdsink uid:10234\n"
        "package:com.android.chrome uid:10050\n"
        "package:com.vendor.secondscreen\n"
    )
    assert pk["com.example.wfdsink"] == 10234 and pk["com.vendor.secondscreen"] == -1
    sinks, sources = sink_packages(pk)
    assert sinks == ["com.example.wfdsink", "com.vendor.secondscreen"]
    assert sources == ["com.samsung.android.smartmirroring"]


def test_dumpsys_activities_and_tiles():
    dump = (
        "  Activity Resolver Table:\n"
        "      a1b2 com.vendor.secondscreen/.MainActivity filter 1\n"
        "      c3d4 com.vendor.secondscreen/.sink.SinkReceiverActivity filter 2\n"
        "      c3d4 com.vendor.secondscreen/.sink.SinkReceiverActivity filter 3\n"
    )
    assert parse_dumpsys_activities(dump, "com.vendor.secondscreen") == [
        "com.vendor.secondscreen/.sink.SinkReceiverActivity"
    ]
    tiles = "wifi,bt,custom(com.vendor.secondscreen/.SecondScreenTile),custom(com.x/.FlashTile)\n"
    assert parse_qs_tiles(tiles) == ["com.vendor.secondscreen/.SecondScreenTile"]


def test_ip_addrs_and_subnet():
    out = ("1: lo    inet 127.0.0.1/8 scope host lo\n"
           "23: rndis0    inet 10.108.120.1/24 brd 10.108.120.255 scope global rndis0\n")
    assert parse_ip_addrs(out) == [("lo", "127.0.0.1", 8), ("rndis0", "10.108.120.1", 24)]
    assert same_subnet("10.108.120.1", "10.108.120.2") and not same_subnet("10.108.121.1", "10.108.120.2")


def test_adb_select_prefers_samsung_serial_and_logs():
    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        return 0, "List of devices attached\nother\tdevice\nRXGYB09YJXJ\tdevice\n"

    adb = Adb(runner=runner)
    assert adb.select(["RXGYB09YJXJ"]) == ("RXGYB09YJXJ", "device")
    adb.shell("true")
    assert calls[-1][:3] == ["adb", "-s", "RXGYB09YJXJ"]
    assert adb.log and adb.log[0].startswith("$ devices")


def test_adb_select_unauthorized():
    adb = Adb(runner=lambda a, t: (0, "List of devices attached\nRXGYB09YJXJ\tunauthorized\n"))
    assert adb.select(["RXGYB09YJXJ"]) == (None, "unauthorized")
