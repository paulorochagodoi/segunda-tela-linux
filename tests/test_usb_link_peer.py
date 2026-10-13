from segunda_tela.usb.link import UsbLink, _guess_peer_ip


def test_guess_peer_ip():
    assert _guess_peer_ip("192.168.42.129").startswith("192.168.42.")
    assert _guess_peer_ip("192.168.42.129") != "192.168.42.129"


def test_usb_link_close_noop():
    link = UsbLink(transport="rndis", local_ip="192.168.42.129")
    link.close()  # no adb
