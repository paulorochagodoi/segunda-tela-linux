from pathlib import Path

from segunda_tela.display.evdi import evdi_cards, module_loaded
from segunda_tela.display.portal import (
    PortalStream,
    looks_like_primary,
    parse_streams,
    pipewiresrc_fragment,
    request_path,
    sender_to_path_component,
)
from segunda_tela.display.virtual import evdi_modes, resolve_capture


def test_resolve_capture():
    assert resolve_capture("extend", "auto", wayland=True, x11=False) == "portal"
    assert resolve_capture("extend", "auto", wayland=False, x11=True) == "evdi"
    assert resolve_capture("mirror", "auto", wayland=True, x11=False) == "portal"
    assert resolve_capture("mirror", "auto", wayland=False, x11=True) == "x11"
    assert resolve_capture("mirror", "auto", wayland=False, x11=False) == "test"
    assert resolve_capture("extend", "evdi", wayland=True, x11=False) == "evdi"


def test_evdi_modes():
    assert evdi_modes(1920, 1200, 60) == [(1920, 1200, 60), (2560, 1600, 60), (1920, 1080, 60)]
    assert evdi_modes(2560, 1600, 30)[0] == (2560, 1600, 30)
    assert len(evdi_modes(2560, 1600, 60)) == 3


def test_evdi_sysfs_scan(tmp_path: Path):
    assert not module_loaded(tmp_path)
    (tmp_path / "module" / "evdi").mkdir(parents=True)
    assert module_loaded(tmp_path)
    plat = tmp_path / "bus" / "platform" / "devices"
    (plat / "evdi.0" / "drm" / "card2").mkdir(parents=True)
    (plat / "evdi.1" / "drm" / "card3").mkdir(parents=True)
    (plat / "amdgpu" / "drm" / "card1").mkdir(parents=True)
    assert evdi_cards(tmp_path) == [2, 3]


def test_portal_helpers():
    assert sender_to_path_component(":1.42") == "1_42"
    assert request_path(":1.42", "tok") == "/org/freedesktop/portal/desktop/request/1_42/tok"
    streams = parse_streams([(57, {"position": (1920, 0), "size": (1920, 1200), "source_type": 1})])
    assert streams[0].node_id == 57 and streams[0].position == (1920, 0)
    assert not looks_like_primary(streams[0], (1920, 1200))
    assert looks_like_primary(PortalStream(58, position=(0, 0), size=(1920, 1200)), (1920, 1200))
    assert looks_like_primary(PortalStream(59, position=(1920, 0), size=(2560, 1600)), (1920, 1200))
    frag = pipewiresrc_fragment(7, 57, 60)
    assert "fd=7" in frag and "path=57" in frag
