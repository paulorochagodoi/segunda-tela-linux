import json

from segunda_tela.display.randr import (
    Layout,
    Output,
    evdi_connector_names,
    evdi_status,
    layout_checks,
    parse_json,
    parse_kdl,
    parse_text,
    query_layout,
)

KDL = '''
output "eDP-1" enabled=#true {
    description make="BOE" model="0x0BCA"
    physical 302 189
    position 0 0
    scale 1.25
    transform "normal"
    modes {
        mode 1920 1200 60000 current=#true preferred=#true
        mode 1600 1200 60000
    }
}
output "DVI-I-1" enabled=#true {
    description make="SGT" model="Segunda Tela"
    position 1536 0
    scale 1.0
    modes {
        mode 2560 1600 60000
        mode 1920 1200 60000 current=#true preferred=#true
    }
}
'''

COSMIC_TEXT = (
    "\x1b[1meDP-1\x1b[0m (enabled)\n"
    "  Make: BOE\n  Model: 0x0BCA\n  Physical Size: 302 x 189 mm\n"
    "  Position: 0,0\n  Scale: 125%\n  Transform: normal\n  Modes:\n"
    "    1920x1200 @ 60.000 Hz (current) (preferred)\n"
    "DVI-I-1 (enabled)\n  Make: SGT\n  Model: Segunda Tela\n  Position: 0,0\n  Scale: 100%\n"
    "  Modes:\n    1920x1200 @ 60.000 Hz (current) (preferred)\n    2560x1600 @ 60.000 Hz\n"
)

WLR_TEXT = '''eDP-1 "BOE 0x0BCA (eDP-1)"
  Make: BOE
  Model: 0x0BCA
  Enabled: yes
  Modes:
    1920x1200 px, 60.002998 Hz (preferred, current)
  Position: 0,0
  Transform: normal
  Scale: 1.000000
DVI-I-1 "SGT Segunda Tela (DVI-I-1)"
  Make: SGT
  Model: Segunda Tela
  Enabled: no
  Modes:
    1920x1200 px, 60.000000 Hz (preferred)
'''

WLR_JSON = json.dumps([
    {"name": "eDP-1", "make": "BOE", "model": "0x0BCA", "enabled": True,
     "modes": [{"width": 1920, "height": 1200, "refresh": 60.0, "current": True}],
     "position": {"x": 0, "y": 0}, "transform": "normal", "scale": 1.0},
    {"name": "DVI-I-1", "make": "SGT", "model": "Segunda Tela", "enabled": True,
     "modes": [{"width": 1920, "height": 1200, "refresh": 60.0, "current": True}],
     "position": {"x": -1920, "y": 0}, "transform": "normal", "scale": 1.0},
])


def test_parse_kdl_extended():
    outs = parse_kdl(KDL)
    assert [o.name for o in outs] == ["eDP-1", "DVI-I-1"]
    edp, evdi = outs
    assert edp.logical_size == (1536, 960) and edp.refresh == 60
    assert evdi.position == (1536, 0) and (evdi.width, evdi.height) == (1920, 1200)
    st = evdi_status(Layout("cosmic-randr", outs), {"DVI-I-1"})
    assert st.status == "extended" and st.output is evdi


def test_parse_cosmic_text_overlap_is_mirrored():
    outs = parse_text(COSMIC_TEXT)
    assert outs[0].scale == 1.25 and outs[0].position == (0, 0)
    assert outs[1].make == "SGT"
    # Identified by EDID make even without sysfs connector names.
    st = evdi_status(Layout("cosmic-randr", outs), set())
    assert st.status == "mirrored" and "sobrepõe eDP-1" in st.message


def test_parse_wlr_text_disabled():
    outs = parse_text(WLR_TEXT)
    assert outs[0].enabled and not outs[1].enabled
    assert (outs[0].width, outs[0].height) == (1920, 1200)
    assert evdi_status(Layout("wlr-randr", outs), {"DVI-I-1"}).status == "disabled"


def test_parse_wlr_json_and_explicit_mirroring():
    outs = parse_json(WLR_JSON)
    assert evdi_status(Layout("wlr-randr", outs), {"DVI-I-1"}).status == "extended"
    outs[1].mirroring = "eDP-1"
    assert evdi_status(Layout("wlr-randr", outs), {"DVI-I-1"}).status == "mirrored"


def test_absent_and_unknown():
    edp = Output("eDP-1", x=0, y=0, width=1920, height=1200)
    assert evdi_status(Layout("cosmic-randr", [edp]), {"DVI-I-1"}).status == "absent"
    assert evdi_status(Layout("", [], error="x"), set()).status == "unknown"


def test_query_falls_back_to_wlr_randr():
    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        if argv[0] == "cosmic-randr":
            return 127, ""
        return (0, WLR_JSON) if argv == ["wlr-randr", "--json"] else (1, "")

    lay = query_layout(runner)
    assert lay.tool == "wlr-randr" and len(lay.outputs) == 2
    assert calls[-1] == ["wlr-randr", "--json"]
    none = query_layout(lambda a, t: (127, ""))
    assert not none.tool and "não instalados" in none.error


def test_evdi_connector_names(tmp_path):
    (tmp_path / "bus/platform/devices/evdi.0/drm/card2").mkdir(parents=True)
    (tmp_path / "class/drm/card2-DVI-I-1").mkdir(parents=True)
    (tmp_path / "class/drm/card1-eDP-1").mkdir(parents=True)
    assert evdi_connector_names(tmp_path) == {"DVI-I-1"}


def test_layout_checks_probe_when_absent(tmp_path):
    from segunda_tela.display.randr import EvdiLayoutStatus

    edp_only = "eDP-1 (enabled)\n  Position: 0,0\n  Modes:\n    1920x1200 @ 60.000 Hz (current)\n"
    runner = lambda a, t: (0, edp_only) if a == ["cosmic-randr", "list"] else (1, "")  # noqa: E731
    checks = list(layout_checks(probe=lambda: EvdiLayoutStatus("mirrored", None, "espelhando"),
                                sys_root=tmp_path, runner=runner))
    assert checks[0][0] == "ok" and checks[-1] == ("fail", "saída EVDI estendida", "espelhando")
    checks = list(layout_checks(probe=None, sys_root=tmp_path, runner=runner))
    assert checks[-1][0] == "info" and "ausente" in checks[-1][2]
