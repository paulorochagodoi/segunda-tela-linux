from pathlib import Path

from segunda_tela.usb.detect import list_samsung_usb_devices


def test_list_samsung_from_fake_sysfs(tmp_path: Path):
    dev = tmp_path / "1-1"
    dev.mkdir()
    (dev / "idVendor").write_text("04e8\n")
    (dev / "idProduct").write_text("6860\n")
    (dev / "serial").write_text("R52X123\n")
    (dev / "product").write_text("Galaxy Tab\n")
    (dev / "manufacturer").write_text("Samsung\n")
    # noise
    other = tmp_path / "1-2"
    other.mkdir()
    (other / "idVendor").write_text("1d6b\n")
    (other / "idProduct").write_text("0002\n")
    iface = tmp_path / "1-1:1.0"
    iface.mkdir()

    found = list_samsung_usb_devices(tmp_path)
    assert len(found) == 1
    assert found[0].vid == "04e8"
    assert found[0].pid == "6860"
    assert found[0].serial == "R52X123"


def test_no_device(tmp_path: Path):
    assert list_samsung_usb_devices(tmp_path) == []
