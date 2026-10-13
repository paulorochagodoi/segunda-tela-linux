from segunda_tela.usb.adb import Adb
from segunda_tela.usb.doctor import usb_checks


def _sysfs(tmp_path):
    d = tmp_path / "1-2"
    d.mkdir()
    (d / "idVendor").write_text("04e8\n")
    (d / "idProduct").write_text("6863\n")
    (d / "serial").write_text("RXGYB09YJXJ\n")
    return str(tmp_path)


def test_usb_doctor_with_device(tmp_path):
    def runner(argv, timeout):
        cmd = " ".join(argv[1:])
        if cmd == "devices":
            return 0, "List of devices attached\nRXGYB09YJXJ\tunauthorized\n"
        return 1, ""

    checks = list(usb_checks(rtsp_port=17320, adb=Adb(runner=runner), sys_bus_usb=_sysfs(tmp_path),
                             ifaces=["enxa27231c0d825"], iface_ip=lambda i: "10.108.120.2",
                             probe=lambda h, p, t: "refused"))
    by = {label: (st, detail) for st, label, detail in checks}
    assert by["Samsung USB device"][0] == "ok"
    assert by["tether USB (interface/IP)"] == ("ok", "enxa27231c0d825 local=10.108.120.2 peer≈10.108.120.1")
    assert by["adb dispositivo"][0] == "fail" and "NÃO autorizado" in by["adb dispositivo"][1]
    assert by["tablet 10.108.120.1 alcançável"][0] == "ok"  # refused ⇒ host answered
    assert by["sink RTSP 10.108.120.1:7236"] == ("info", "refused")
    assert "dica" in by


def test_usb_doctor_no_device_is_informational(tmp_path):
    checks = list(usb_checks(rtsp_port=17321, adb=Adb(exe="definitely-not-adb-xyz"),
                             sys_bus_usb=str(tmp_path), ifaces=[], iface_ip=lambda i: None))
    assert not [c for c in checks if c[0] == "fail"]


def test_usb_session_uses_extend_display_path(monkeypatch):
    import segunda_tela.session as sess
    from segunda_tela.display.virtual import VirtualDisplay
    from segunda_tela.usb.link import UsbLink

    seen = {}

    def fake_create(w, h, mode, fps, capture="auto"):
        seen.update(mode=mode, capture=capture)
        return VirtualDisplay(w, h, mode, backend="evdi-grab")

    monkeypatch.setattr(sess, "create_virtual_display", fake_create)
    s = sess.SecondScreenSession(mode="extend", usb_link=UsbLink(transport="ncm", local_ip="10.108.120.2"),
                                 encoder="x264enc")
    s.prepare_display()
    assert seen == {"mode": "extend", "capture": "auto"}
    assert s.advertise_ip == "10.108.120.2"
    assert not [n for n in s.notes if "espelhamento" in n]
