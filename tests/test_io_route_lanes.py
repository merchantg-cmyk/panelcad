"""Auto routes on a packed I/O sheet keep off other numbers' runs."""

from __future__ import annotations

from panelcad import Session
from panelcad.render.io_ladder import io_bus_xs
from panelcad.render.junctions import junction_points
from panelcad.render.sheet import pin_world
from panelcad.render.wiring import _conductor_key, route_sheet_wires


def _segs(pts):
    return list(zip(pts, pts[1:]))


def _collinear(a, b) -> bool:
    (p1, p2), (q1, q2) = a, b
    if abs(p1.x - p2.x) < 0.01 and abs(q1.x - q2.x) < 0.01 and abs(p1.x - q1.x) < 0.5:
        lo = max(min(p1.y, p2.y), min(q1.y, q2.y))
        hi = min(max(p1.y, p2.y), max(q1.y, q2.y))
        return hi - lo > 0.5
    if abs(p1.y - p2.y) < 0.01 and abs(q1.y - q2.y) < 0.01 and abs(p1.y - q1.y) < 0.5:
        lo = max(min(p1.x, p2.x), min(q1.x, q2.x))
        hi = min(max(p1.x, p2.x), max(q1.x, q2.x))
        return hi - lo > 0.5
    return False


def _inputs_sheet(card_x: float = 250.0, pitch: float = 14.0) -> Session:
    s = Session.create("IO lanes")
    s.set_title_block(rail_hot="+24V", rail_return="0V")
    s.add_sheet("in", "Inputs", kind="io")
    card = "oem.keyence.kv_b16xc"
    s.place(card, ref="A1", x=card_x, y=32, sheet_id="in", snap=False)
    pins = [p.id for p in s.catalog.get(card).pins if p.id.upper().startswith(("X", "I"))][:6]
    pb = s.catalog.get("nema.operators.pb_no").pins
    for i, pin in enumerate(pins):
        # Field devices on their own pitch, not the card's channel pitch.
        s.place("nema.operators.pb_no", ref=f"PB{i + 1}", x=28, y=46 + pitch * i, sheet_id="in", snap=False)
        s.wire(f"PB{i + 1}.{pb[1].id}", f"A1.{pin}", sheet_id="in", number=f"1{i:02d}")
        s.wire("+24V", f"PB{i + 1}.{pb[0].id}", sheet_id="in", net="+24V")
    return s


def test_staggered_inputs_do_not_share_a_run_or_get_dots():
    for card_x, pitch in ((250.0, 14.0), (165.0, 10.0), (250.0, 20.0)):
        s = _inputs_sheet(card_x, pitch)
        sheet = s.project.sheet("in")
        routes = route_sheet_wires(sheet, s.catalog, project=s.project)
        wires = {w.id: w for w in sheet.wires}
        ids = list(routes)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if _conductor_key(wires[a]) == _conductor_key(wires[b]):
                    continue
                assert not any(
                    _collinear(p, q) for p in _segs(routes[a]) for q in _segs(routes[b])
                ), (card_x, pitch, a, b)
        pins = [
            (*pin_world(c, p), c.id)
            for c in sheet.components
            for p in s.catalog.get(c.library_id).pins
        ]
        for x, y in junction_points(routes, pins):
            keys = {
                _conductor_key(wires[wid])
                for wid, pts in routes.items()
                for p, q in _segs(pts)
                if min(p.x, q.x) - 0.5 <= x <= max(p.x, q.x) + 0.5
                and min(p.y, q.y) - 0.5 <= y <= max(p.y, q.y) + 0.5
            }
            assert len(keys) == 1, (card_x, pitch, x, y, keys)
        # Rail wires still land on the painted bus, and ends stay on their pins.
        supply, _zero = io_bus_xs()
        for w in sheet.wires:
            if w.frm == "+24V":
                assert abs(routes[w.id][0].x - supply) < 0.01


def test_output_loads_still_land_on_0v_bus():
    s = Session.create("IO lanes")
    s.set_title_block(rail_hot="+24V", rail_return="0V")
    s.add_sheet("out", "Outputs", kind="io")
    card = "oem.keyence.kv_b16tc"
    s.place(card, ref="A2", x=88, y=32, sheet_id="out", snap=False)
    outs = [p.id for p in s.catalog.get(card).pins if p.id.upper().startswith(("Y", "Q"))][:4]
    for i, q in enumerate(outs):
        s.place("nema.operators.pilot_light", ref=f"LT{i + 1}", x=328, y=46 + 14 * i, sheet_id="out", snap=False)
        s.wire(f"A2.{q}", f"LT{i + 1}.X1", sheet_id="out", number=f"Q{i}")
        s.wire(f"LT{i + 1}.X2", "0V", sheet_id="out", net="0V")
    sheet = s.project.sheet("out")
    routes = route_sheet_wires(sheet, s.catalog, project=s.project)
    _supply, zero = io_bus_xs()
    for w in sheet.wires:
        if w.to == "0V":
            assert abs(routes[w.id][-1].x - zero) < 0.01
