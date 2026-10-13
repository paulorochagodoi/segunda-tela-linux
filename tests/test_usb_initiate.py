import socket
import threading

from segunda_tela.usb.adb import Adb, Listener
from segunda_tela.usb.detect import UsbDevice
from segunda_tela.usb.initiate import (
    Target, UsbInitiator, build_targets, dial_tcp, guidance, pick_sink_ports, probe_tcp,
)
from segunda_tela.usb.link import UsbLink
from segunda_tela.wfd.fakesink import run_fake_sink
from segunda_tela.wfd.rtsp import WfdRtspServer


def _link(**kw):
    d = dict(transport="ncm", iface="enxa27231c0d825", local_ip="10.108.120.2", peer_ip="10.108.120.1",
             devices=[UsbDevice("04e8", "6863", "RXGYB09YJXJ", "/sys/x")])
    d.update(kw)
    return UsbLink(**d)


def _listening_sink(results, **kw):
    """A fake *tablet* that listens like a sink and runs M1–M7 once the PC dials in."""
    ls = socket.socket()
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(("127.0.0.1", 0))
    ls.listen(1)

    def serve():
        conn, _ = ls.accept()
        try:
            results.append(run_fake_sink("", 0, sock=conn, **kw))
        except Exception as exc:  # noqa: BLE001
            results.append(exc)
        finally:
            ls.close()

    threading.Thread(target=serve, daemon=True).start()
    return ls.getsockname()[1]


def _server(port, events):
    srv = WfdRtspServer(host="127.0.0.1", port=port, advertise_host="127.0.0.1", m1_delay_s=0.1,
                        on_play=lambda st: events.append(("play", st.rtp_host, st.rtp_port, st.outbound)))
    srv.start_background()
    return srv


def test_connect_to_runs_source_flow_outbound():
    events, results = [], []
    sink_port = _listening_sink(results)
    srv = _server(17311, events)
    try:
        th = srv.connect_to("127.0.0.1", sink_port, media_host="10.108.120.1")
        th.join(5)
    finally:
        srv.stop()
    res = results[0]
    assert not isinstance(res, Exception), res
    assert res.m1.headers["require"] == "org.wfa.wfd1.0"
    assert "wfd_trigger_method: SETUP" in res.m5.body
    assert events == [("play", "10.108.120.1", 19000, True)]


def test_pick_and_build_targets():
    ls = [Listener("127.0.0.1", 5037, 2000), Listener("0.0.0.0", 9000, 10234),
          Listener("::", 7236, 10234), Listener("127.0.0.1", 7250, 10234)]
    picked = pick_sink_ports(ls, {10234})
    # 7250 is Samsung's source-ready (MS-MICE) port, never dialled with RTSP M1.
    assert [l.port for l in picked] == [7236, 9000]
    ts = build_targets("10.108.120.1", picked, adb_ok=True)
    assert [(t.host, t.port, t.via) for t in ts] == [
        ("10.108.120.1", 7236, "tether"), ("127.0.0.1", 7236, "adb-forward"),
        ("10.108.120.1", 9000, "tether"), ("127.0.0.1", 9000, "adb-forward"),
    ]
    assert all(t.media_host == "10.108.120.1" for t in ts if t.via == "adb-forward")
    # No adb info: blind-dial the standard WFD port over the tether.
    assert [(t.port, t.via) for t in build_targets("10.108.120.1", [], adb_ok=False)] == [(7236, "tether")]


def test_probe_tcp_open_and_refused():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    assert probe_tcp("127.0.0.1", port) == "open"
    s.close()
    assert probe_tcp("127.0.0.1", port) == "refused"


class FakeTabletAdb(Adb):
    """adb against a fake tablet whose sink listens on `sink_port` (via adb forward)."""

    def __init__(self, sink_port):
        super().__init__(runner=self._run)
        self.sink_port = sink_port
        self.cmds = []

    def _run(self, argv, timeout):
        cmd = " ".join(argv[1:])
        self.cmds.append(cmd)
        if argv[1:] == ["devices"]:
            return 0, "List of devices attached\nRXGYB09YJXJ\tdevice\n"
        if "pm list packages" in cmd:
            return 0, "package:com.vendor.secondscreen uid:10234\npackage:com.android.chrome uid:10050\n"
        if "dumpsys package" in cmd:
            return 0, "  1 com.vendor.secondscreen/.SinkActivity filter\n"
        if "am start" in cmd:
            return 0, "Starting: Intent { cmp=com.vendor.secondscreen/.SinkActivity }\n"
        if "ip -4 -o addr" in cmd:
            return 0, "30: rndis0    inet 10.108.120.129/24 scope global rndis0\n"
        if "/proc/net/tcp" in cmd:
            return 0, ("   0: 00000000:1C44 00000000:0000 0A 00000000:00000000 00:00000000 00000000 "
                       "10234        0 1 1\n")
        if argv[-2:] == ["tcp:0", "tcp:7236"]:
            return 0, f"{self.sink_port}\n"
        if argv[-2:] == ["tcp:0", "tcp:7250"]:
            return 0, f"{_closed_port()}\n"  # nothing serves source-ready on this tablet
        return 0, ""


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_initiator_launches_sink_forwards_and_dials():
    events, results, said = [], [], []
    sink_port = _listening_sink(results, wait_keepalive=False)
    srv = _server(17312, events)
    adb = FakeTabletAdb(sink_port)
    # Tether peer unreachable in this sandbox → only the adb-forward target opens.
    dial = lambda h, p, t: dial_tcp(h, p, t) if h == "127.0.0.1" else (None, "timeout")
    ini = UsbInitiator(srv, _link(), rtsp_port=17312, adb=adb, timeout_s=10, retry_s=0.2,
                       say=said.append, dial=dial, settle_s=2.0)
    try:
        res = ini.run()
    finally:
        ini.stop()
        srv.stop()
    assert "reverse tcp:17312 tcp:17312" in " | ".join(adb.cmds)
    assert any("am start -n com.vendor.secondscreen/.SinkActivity" in c for c in adb.cmds)
    assert res.launched == ["com.vendor.secondscreen/.SinkActivity"]
    assert ini.link.peer_ip == "10.108.120.129"  # real tablet IP learned via adb
    assert res.connected is not None and res.connected.via.startswith("adb-forward")
    assert results and not isinstance(results[0], Exception), results
    # RTSP over adb forward, RTP to the tablet's tether IP.
    assert events == [("play", "10.108.120.129", 19000, True)]
    assert any("forward --remove" in c for c in adb.cmds)
    assert any("reverse --remove tcp:17312" in c for c in adb.cmds)


def test_initiator_without_adb_prints_guidance():
    said = []
    srv = WfdRtspServer(host="127.0.0.1", port=17313)
    srv.start_background()
    adb = Adb(exe="definitely-not-adb-xyz")
    ini = UsbInitiator(srv, _link(), rtsp_port=17313, adb=adb, timeout_s=0.3, retry_s=0.1,
                       say=said.append, dial=lambda h, p, t: (None, "refused"))
    try:
        res = ini.run()
    finally:
        srv.stop()
    text = "\n".join(said)
    assert res.adb_state == "absent" and res.connected is None
    assert "sudo apt install adb" in text
    assert "Connlib" in text and "segunda-tela --mode extend --device" in text
    assert "rtsp://10.108.120.2:17313" in text


def test_guidance_unauthorized():
    from segunda_tela.usb.initiate import InitiateResult

    g = guidance(_link(), InitiateResult(adb_state="unauthorized"), 7236)
    assert "Permitir depuração USB" in g


def test_target_str():
    assert str(Target("10.108.120.1", 7236, "tether")) == "10.108.120.1:7236 (tether)"
