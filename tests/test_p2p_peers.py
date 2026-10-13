import pytest

from segunda_tela.wfd import p2p
from segunda_tela.wfd.p2p import (
    P2PError,
    P2PLink,
    connection_settings,
    find_and_connect,
    format_peer_table,
    match_peer,
    peer_from_props,
)

SINK_IES = list(bytes.fromhex("00000600111c440032"))  # NM unpacks `ay` to a list or bytes

FIXTURE = {
    "/org/freedesktop/NetworkManager/WifiP2PPeer/3": {
        "Name": "Galaxy Tab S9",
        "HwAddress": "AA:BB:CC:DD:EE:01",
        "Strength": 72,
        "Manufacturer": "Samsung",
        "Model": "SM-X710",
        "WfdIEs": SINK_IES,
        "LastSeen": 1234,
    },
    "/org/freedesktop/NetworkManager/WifiP2PPeer/4": {
        "Name": "DIRECT-printer",
        "HwAddress": "AA:BB:CC:DD:EE:02",
        "Strength": 90,
        "WfdIEs": b"",
    },
    "/org/freedesktop/NetworkManager/WifiP2PPeer/5": {
        "Name": "Galaxy Phone",
        "HwAddress": "AA:BB:CC:DD:EE:03",
        "Strength": 40,
        "WfdIEs": bytes.fromhex("00000600101c440032"),  # WFD *source*
    },
}


def peers():
    return [peer_from_props(path, props) for path, props in FIXTURE.items()]


def test_peer_from_props():
    tab, printer, phone = peers()
    assert tab.label == "Galaxy Tab S9" and tab.strength == 72
    assert tab.wfd_capable and tab.is_wfd_sink and tab.wfd.rtsp_port == 7236
    assert not printer.wfd_capable and not printer.is_wfd_sink
    assert phone.wfd_capable and not phone.is_wfd_sink
    assert peer_from_props("/x", {}).label == ""


def test_match_peer():
    ps = peers()
    assert match_peer(ps, "Galaxy Tab S9").hw_address.endswith("01")
    assert match_peer(ps, "galaxy tab s9").hw_address.endswith("01")
    assert match_peer(ps, "aa:bb:cc:dd:ee:02").name == "DIRECT-printer"
    assert match_peer(ps, "/org/freedesktop/NetworkManager/WifiP2PPeer/5").name == "Galaxy Phone"
    assert match_peer(ps, "5").name == "Galaxy Phone"
    assert match_peer(ps, "Tab").name == "Galaxy Tab S9"
    assert match_peer(ps, "SM-X710").name == "Galaxy Tab S9"
    # "Galaxy" matches two peers but only one is a WFD sink
    assert match_peer(ps, "Galaxy").name == "Galaxy Tab S9"
    assert match_peer(ps, "nope") is None
    assert match_peer(ps, "") is None


def test_match_peer_ambiguous():
    ps = peers() + [peer_from_props("/p/9", {"Name": "Galaxy Tab A", "WfdIEs": SINK_IES})]
    with pytest.raises(P2PError):
        match_peer(ps, "Galaxy Tab")


def test_connection_settings():
    tab = peers()[0]
    s = connection_settings(tab, rtsp_port=7236)
    assert s["connection"]["type"] == "wifi-p2p"
    assert s["wifi-p2p"]["peer"] == "AA:BB:CC:DD:EE:01"
    assert s["wifi-p2p"]["wfd-ies"] == bytes.fromhex("00000600901c4400c8")
    assert s["ipv4"]["never-default"] is True
    for group, values in s.items():
        for key in values:
            assert (group, key) in p2p._SETTING_TYPES


def test_format_peer_table():
    out = format_peer_table(peers())
    lines = out.splitlines()
    assert "NAME" in lines[0]
    assert lines[1].startswith("Galaxy Tab S9")  # sinks first
    assert "primary-sink" in lines[1] and "72%" in lines[1]


class FakeNM:
    def __init__(self):
        self.connected = None

    def discover(self, seconds, until=None, on_update=None):
        ps = peers()
        assert until is not None
        until(ps)
        return ps

    def connect(self, peer, rtsp_port=7236, timeout_s=60):
        self.connected = peer
        return P2PLink(peer=peer, active_path="/ac/1", interface="p2p-wlo1-0", local_ip="192.168.49.1")


def test_find_and_connect_with_fake_nm():
    nm = FakeNM()
    link = find_and_connect("Galaxy Tab S9", nm=nm)
    assert nm.connected.name == "Galaxy Tab S9"
    assert link.interface == "p2p-wlo1-0" and link.local_ip == "192.168.49.1"
    with pytest.raises(P2PError):
        find_and_connect("Apple TV", nm=nm, discover_s=0)
