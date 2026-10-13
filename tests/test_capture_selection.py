import json

import pytest

import segunda_tela.display.evdi as evdi_mod
import segunda_tela.display.portal as portal
import segunda_tela.display.virtual as virtual
from segunda_tela.display.portal import PortalCapture, PortalStream, forget_restore_token, stream_is_output


class FakeMonitor:
    def __init__(self, *a, grab=False, **kw):
        self.grab = grab
        self.mode = (1920, 1200, 60)
        self.closed = False

    def open(self):
        pass

    def enable_grab(self):
        self.grab = True

    def close(self):
        self.closed = True


class BrokenMonitor(FakeMonitor):
    def open(self):
        raise evdi_mod.EvdiError("kernel module evdi not loaded")


def _cap(pos, size=(1920, 1200)):
    return PortalCapture(fd=-1, streams=[PortalStream(108, position=pos, size=size)])


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    return tmp_path / "segunda-tela" / "portal.json"


def test_extend_defaults_to_evdi_grab_without_portal(cfg, monkeypatch):
    monkeypatch.setattr(evdi_mod, "EvdiMonitor", FakeMonitor)
    monkeypatch.setattr(portal, "open_screencast", lambda **kw: pytest.fail("portal must not open"))
    disp = virtual.create_virtual_display(1920, 1200, "extend", 30)
    assert disp.backend == "evdi-grab" and disp.evdi.grab


def test_extend_falls_back_to_portal_without_evdi(cfg, monkeypatch):
    monkeypatch.setattr(evdi_mod, "EvdiMonitor", BrokenMonitor)
    keys = []

    def fake_open(persist_key=None, **kw):
        keys.append(persist_key)
        return _cap((0, 0))

    monkeypatch.setattr(portal, "open_screencast", fake_open)
    disp = virtual.create_virtual_display(1920, 1200, "extend", 30)
    assert disp.backend == "portal" and disp.portal is not None
    assert keys == ["mirror"]  # never pollutes the remembered extend choice
    assert any("espelhamento" in n for n in disp.notes)


def test_explicit_evdi_capture_does_not_fall_back(cfg, monkeypatch):
    monkeypatch.setattr(evdi_mod, "EvdiMonitor", BrokenMonitor)
    with pytest.raises(evdi_mod.EvdiError):
        virtual.create_virtual_display(1920, 1200, "extend", 30, capture="evdi")


def test_remembered_edp1_choice_is_forgotten_and_repicked(cfg, monkeypatch):
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"extend": "tok-edp1", "mirror": "tok-m"}))
    monkeypatch.setattr(evdi_mod, "EvdiMonitor", FakeMonitor)
    picks = iter([_cap((0, 0)), _cap((1920, 0))])
    seen_tokens = []

    def fake_open(persist_key=None, **kw):
        seen_tokens.append(portal.load_restore_token(persist_key))
        return next(picks)

    monkeypatch.setattr(portal, "open_screencast", fake_open)
    disp = virtual.create_virtual_display(1920, 1200, "extend", 30, capture="portal")
    assert seen_tokens == ["tok-edp1", None]  # second picker opened without the bad token
    assert disp.backend == "portal" and disp.portal.streams[0].position == (1920, 0)
    assert json.loads(cfg.read_text()) == {"mirror": "tok-m"}


def test_portal_never_gives_evdi_output_falls_back_to_grab(cfg, monkeypatch):
    monkeypatch.setattr(evdi_mod, "EvdiMonitor", FakeMonitor)
    monkeypatch.setattr(portal, "open_screencast", lambda **kw: _cap((0, 0)))
    disp = virtual.create_virtual_display(1920, 1200, "extend", 30, capture="portal")
    assert disp.backend == "evdi-grab" and disp.portal is None and disp.evdi.grab


def test_forget_restore_token(cfg):
    assert not forget_restore_token("extend")
    portal.save_restore_token("extend", "t")
    assert forget_restore_token("extend")
    assert portal.load_restore_token("extend") is None


def test_stream_is_output():
    s = PortalStream(1, position=(1920, 0), size=(1920, 1200))
    assert stream_is_output(s, (1920, 1200))
    assert stream_is_output(s, (1920, 1200), output_pos=(1920, 0))
    assert not stream_is_output(s, (1920, 1200), output_pos=(-1920, 0))
    assert not stream_is_output(PortalStream(2, position=(0, 0), size=(1920, 1200)), (1920, 1200))
    # Layout known: an EVDI output placed at (0,0) is accepted.
    assert stream_is_output(PortalStream(3, position=(0, 0), size=(1920, 1200)), (1920, 1200), output_pos=(0, 0))
