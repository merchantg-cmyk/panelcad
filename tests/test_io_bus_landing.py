"""A wire to a rail on a packed I/O sheet lands on the bus that sheet paints."""

from __future__ import annotations

from panelcad import Session
from panelcad.render.io_ladder import io_bus_xs
from panelcad.render.wiring import route_sheet_wires


def test_return_rail_wire_ends_on_painted_0v_bus():
    s = Session.create("IO bus")
    s.set_title_block(rail_hot="+24V", rail_return="0V")
    s.add_sheet("out", "Outputs", kind="io")
    s.place("oem.keyence.kv_b16tc", ref="A1", x=88, y=32, sheet_id="out", snap=False)
    s.place("nema.operators.pilot_light", ref="LT1", x=328, y=56, sheet_id="out", snap=False)
    s.wire("A1.Q0", "LT1.X1", sheet_id="out")
    w = s.wire("LT1.X2", "0V", sheet_id="out", net="0V")
    pts = route_sheet_wires(s.project.sheet("out"), s.catalog, project=s.project)[w.id]
    _supply, zero = io_bus_xs()
    assert abs(pts[-1].x - zero) < 0.01


def test_hot_rail_wire_ends_on_painted_supply_bus():
    s = Session.create("IO bus")
    s.set_title_block(rail_hot="+24V", rail_return="0V")
    s.add_sheet("in", "Inputs", kind="io")
    s.place("oem.keyence.kv_b16xc", ref="A1", x=250, y=32, sheet_id="in", snap=False)
    s.place("nema.operators.pb_no", ref="PB1", x=28, y=42, sheet_id="in", snap=False)
    w = s.wire("+24V", "PB1." + s.catalog.get("nema.operators.pb_no").pins[0].id, sheet_id="in", net="+24V")
    pts = route_sheet_wires(s.project.sheet("in"), s.catalog, project=s.project)[w.id]
    supply, _zero = io_bus_xs()
    assert abs(pts[0].x - supply) < 0.01
