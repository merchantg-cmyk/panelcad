"""Pull a library part from the BOM onto another sheet. One catalog object."""

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.render.sheet import pin_world


def test_place_from_bom_same_ref_other_sheet_does_not_clone_library():
    s = Session.create("BomPull")
    s.place("adc.stride.sio_mb12cdr", ref="A1", x=40, y=40)
    s.sheet().component("A1").properties["catalog"] = "SIO-MB12CDR"
    s.add_sheet("panel", kind="panel_layout")
    result = s.place_from_bom("adc.stride.sio_mb12cdr", ref="A1", x=80, y=80, sheet_id="panel")
    assert result["placed"] is True
    assert result["id"] == "A1"
    assert result["kind"] == "panel_layout"
    panel = s.sheet("panel").component("A1")
    assert panel.library_id == "adc.stride.sio_mb12cdr"
    assert panel.properties["catalog"] == "SIO-MB12CDR"
    again = s.place_from_bom("adc.stride.sio_mb12cdr", ref="A1", sheet_id="panel")
    assert again["placed"] is False
    assert again["reason"] == "already_on_sheet"
    assert sum(1 for sh in s.project.sheets for c in sh.components if c.library_id == "adc.stride.sio_mb12cdr") == 2


def test_place_from_bom_unknown_library_raises():
    s = Session.create("NoLib")
    try:
        s.place_from_bom("adc.missing.part")
    except KeyError as exc:
        assert "unknown library_id" in str(exc)
    else:
        raise AssertionError("missing library must raise")


def test_place_from_bom_ladder_lands_on_rung():
    s = Session.create("LadderBom")
    s.set_title_block(drawing_style="ladder")
    s.place("nema.operators.pb_nc", ref="STOP", x=40, y=40)
    s.cleanup()
    stop = s.sheet().component("STOP")
    device = s.catalog.get("nema.operators.pb_nc")
    pin = next(p for p in device.pins if p.side in ("left", "right"))
    stop_pin_y = pin_world(stop, pin)[1]
    result = s.place_from_bom("nema.operators.pb_no", ref="START", x=90, y=180)
    assert result["placed"] is True
    start = s.sheet().component("START")
    start_dev = s.catalog.get("nema.operators.pb_no")
    start_pin = next(p for p in start_dev.pins if p.side in ("left", "right"))
    start_pin_y = pin_world(start, start_pin)[1]
    assert abs(start_pin_y - stop_pin_y) < 1.0


def test_place_from_bom_network_and_io_use_same_library():
    s = Session.create("NetBom")
    s.place("adc.stride.sio_mb12cdr", ref="A1", x=30, y=30)
    s.add_sheet("eth", kind="network")
    net = s.place_from_bom("adc.stride.sio_mb12cdr", ref="A1", x=41, y=47, sheet_id="eth")
    assert net["placed"] is True
    assert net["kind"] == "network"
    assert s.sheet("eth").component("A1").x == 40.0
    s.add_sheet("io", kind="io")
    io = s.place_from_bom("adc.stride.sio_mb12cdr", ref="A1", x=10, y=10, sheet_id="io")
    assert io["placed"] is True
    assert io["kind"] == "io"
    assert s.sheet("io").component("A1").x > 80.0


def test_agent_place_from_bom():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "AgentBom"})["ok"]
    assert agent.handle({"id": 2, "op": "place", "library_id": "iec.breakers.mcb_3p_32a", "ref": "Q1", "x": 20, "y": 20})["ok"]
    assert agent.handle({"id": 3, "op": "add_sheet", "sheet_id": "eth", "kind": "network"})["ok"]
    r = agent.handle(
        {"id": 4, "op": "place_from_bom", "library_id": "iec.breakers.mcb_3p_32a", "ref": "Q1", "sheet": "eth"}
    )
    assert r["ok"] is True, r
    assert r["result"]["id"] == "Q1"
    assert r["result"]["placed"] is True
