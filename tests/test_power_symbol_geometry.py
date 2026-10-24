"""Power-page symbol geometry: motor leads land on the circle, the control
transformer shows two equal primary windings, H1-H4 labels and its jumpers."""

from __future__ import annotations

import math

from panelcad import Session
from panelcad.core.primitives import Primitive, expand_draw_item, land_leads_on_circle
from panelcad.core.transformer_jumpers import jumper_primitives
from panelcad.library.catalog import _device_from_raw


def _body(device):
    return max((p for p in device.primitives if p.type == "circle" and not p.fill), key=lambda p: p.r)


def _lead_ends_on_circle(device, tol=0.06):
    c = _body(device)
    pins = [(float(p.x), float(p.y)) for p in device.pins]
    lines = [p for p in device.primitives if p.type == "line"]
    for px, py in pins:
        # Walk the lead from the pin through any joined segments to its end.
        x, y, seen = px, py, set()
        while True:
            nxt = None
            for i, ln in enumerate(lines):
                if i in seen:
                    continue
                if math.hypot(ln.x - x, ln.y - y) < 1e-3:
                    nxt = (i, ln.x2, ln.y2)
                elif math.hypot(ln.x2 - x, ln.y2 - y) < 1e-3:
                    nxt = (i, ln.x, ln.y)
                if nxt:
                    break
            if nxt is None:
                break
            seen.add(nxt[0])
            x, y = nxt[1], nxt[2]
        assert seen, f"pin at {(px, py)} has no lead"
        assert abs(math.hypot(x - c.x, y - c.y) - c.r) <= tol, f"lead from {(px, py)} ends at {(x, y)}, off the circle"


def test_builtin_motor_leads_end_on_the_circle():
    session = Session.create("m")
    for lib in ("nema.loads.motor_3ph", "nema.loads.motor_1ph", "nema.loads.motor_3ph_6lead"):
        _lead_ends_on_circle(session.catalog.get(lib))


def test_side_entry_motor_leads_that_miss_the_circle_are_bent_onto_it():
    raw = {
        "id": "m3",
        "category": "load.motor",
        "schematic": {
            "width": 40,
            "height": 52,
            "pins": [
                {"id": "T1", "x": 0, "y": 8, "side": "left"},
                {"id": "T2", "x": 0, "y": 26, "side": "left"},
                {"id": "T3", "x": 0, "y": 44, "side": "left"},
            ],
            "draw": [
                {"line": [0, 8, 12, 8]},
                {"line": [0, 26, 10, 26]},
                {"line": [0, 44, 12, 44]},
                {"circle": [26, 26, 16]},
            ],
        },
    }
    _lead_ends_on_circle(_device_from_raw("job", raw))


def test_short_and_overlong_leads_are_trimmed_to_the_circle():
    prims = [expand_draw_item({"circle": [20, 20, 10]}), expand_draw_item({"line": [20, 0, 20, 4]}), expand_draw_item({"line": [0, 20, 15, 20]})]

    class P:
        def __init__(self, x, y):
            self.x, self.y = x, y

    out = land_leads_on_circle("load.motor", [P(20, 0), P(0, 20)], prims)
    ends = [(p.x2, p.y2) for p in out if p.type == "line"]
    assert ends == [(20.0, 10.0), (10.0, 20.0)]


def test_non_motor_symbols_are_untouched():
    prims = [Primitive(type="circle", x=5, y=5, r=3), Primitive(type="line", x=0, y=0, x2=1, y2=0)]
    assert land_leads_on_circle("pilot.light", [], prims) is prims


def test_control_transformer_has_two_equal_primaries_and_full_secondary():
    device = Session.create("t").catalog.get("nema.power.control_transformer")
    pins = {p.id: (p.x, p.y) for p in device.pins}
    assert pins["H2"][1] - pins["H1"][1] == pins["H4"][1] - pins["H3"][1]
    left = sorted((p for p in device.primitives if p.type == "arc" and p.x < 20), key=lambda p: p.y)
    right = [p for p in device.primitives if p.type == "arc" and p.x > 20]
    assert len(left) == 6 and len({round(p.r, 3) for p in left}) == 1
    # Each primary winding's humps run exactly from its first to its second terminal.
    for first, last, (a, b) in ((left[0], left[2], ("H1", "H2")), (left[3], left[5], ("H3", "H4"))):
        assert abs(first.y - first.r - pins[a][1]) < 1e-3 and abs(last.y + last.r - pins[b][1]) < 1e-3
    # Humps of each winding touch end to end, so the coil reads as one smooth line.
    for coil in (left[:3], left[3:], sorted(right, key=lambda p: p.y)):
        for a, b in zip(coil, coil[1:]):
            assert abs((a.y + a.r) - (b.y - b.r)) < 1e-3
    sec = sorted(right, key=lambda p: p.y)
    assert abs(sec[0].y - sec[0].r - pins["X1"][1]) < 1e-3 and abs(sec[-1].y + sec[-1].r - pins["X2"][1]) < 1e-3
    labels = {p.text for p in device.primitives if p.type == "text"}
    assert {"H1", "H2", "H3", "H4", "X1", "X2"} <= labels


def _jumper_spans(device, comp):
    prims = jumper_primitives(device, comp)
    ys = {p.y for p in device.pins}
    spans = set()
    for p in prims:
        if p.type == "line":
            assert p.y in ys and p.y2 in ys
            spans.add(frozenset({p.y, p.y2}))
    return spans


def test_transformer_jumpers_tie_the_right_terminals():
    session = Session.create("t")
    device = session.catalog.get("nema.power.control_transformer")
    y = {p.id: float(p.y) for p in device.pins}
    hi = session.place("nema.power.control_transformer", x=60, y=60, properties={"primary": "480/240 V", "primary_voltage": "480 V"})
    lo = session.place("nema.power.control_transformer", x=160, y=60, properties={"primary": "480/240 V", "primary_voltage": "240 V"})
    assert _jumper_spans(device, hi) == {frozenset({y["H2"], y["H3"]})}
    assert _jumper_spans(device, lo) == {frozenset({y["H1"], y["H3"]}), frozenset({y["H2"], y["H4"]})}


class _Pin:
    def __init__(self, x, y):
        self.x, self.y = x, y


def test_motor_lines_that_are_not_plain_leads_are_left_alone():
    circle = expand_draw_item({"circle": [20, 20, 10]})
    pe_lead = expand_draw_item({"line": [40, 20, 40, 34]})  # PE pin down to a ground mark
    ground = expand_draw_item({"line": [36, 34, 44, 34]})
    far = expand_draw_item({"line": [0, 0, 0, 40]})  # pin to pin, nowhere near the body
    through = expand_draw_item({"line": [20, -2, 20, 50]})  # pin through the body to a pin
    prims = [circle, pe_lead, ground, far, through]
    pins = [_Pin(40, 20), _Pin(0, 0), _Pin(0, 40), _Pin(20, -2), _Pin(20, 50)]
    out = land_leads_on_circle("load.motor", pins, prims)
    assert [(p.x, p.y, p.x2, p.y2) for p in out if p.type == "line"] == [
        (p.x, p.y, p.x2, p.y2) for p in prims if p.type == "line"
    ]


def test_enclosure_box_interior_clearance():
    """All internal heater, contact, and breaker primitives inside a power device
    enclosure box must maintain at least 2.0 mm clearance from the left and right
    box borders so no heater wave or contact bar ever touches or protrudes through
    its enclosure box."""
    session = Session.create("clearance")
    checked = (
        "nema.protection.overload",
        "iec.protection.overload_9a",
        "nema.breakers.mcb_1p_16a",
        "nema.breakers.mcb_3p_32a",
        "nema.breakers.mccb_3p_100a",
        "nema.breakers.mpcb_3p",
        "nema.contactors.ac3_9a",
        "nema.contactors.ac3_32a",
        "nema.protection.disconnect_3p",
    )
    for lib_id in checked:
        dev = session.catalog.get(lib_id)
        rects = [p for p in dev.primitives if p.type == "rect"]
        assert rects, f"{lib_id} has no enclosure rect"
        outer = max(rects, key=lambda r: r.w * r.h)
        bx1, by1, bx2, by2 = outer.x, outer.y, outer.x + outer.w, outer.y + outer.h
        for p in dev.primitives:
            if p is outer or p.type == "text":
                continue
            if p.type == "arc":
                ax1, ax2 = p.x - p.r, p.x + p.r
                ay1, ay2 = p.y - p.r, p.y + p.r
                assert ax1 - bx1 >= 2.0 - 1e-6, f"{lib_id} arc at x={p.x}, r={p.r} too close to left box wall x={bx1}"
                assert bx2 - ax2 >= 2.0 - 1e-6, f"{lib_id} arc at x={p.x}, r={p.r} too close to right box wall x={bx2}"
                assert ay1 - by1 >= 2.0 - 1e-6, f"{lib_id} arc at y={p.y}, r={p.r} too close to top box wall y={by1}"
                assert by2 - ay2 >= 2.0 - 1e-6, f"{lib_id} arc at y={p.y}, r={p.r} too close to bottom box wall y={by2}"
            elif p.type == "line":
                lx1, lx2 = min(p.x, p.x2), max(p.x, p.x2)
                ly1, ly2 = min(p.y, p.y2), max(p.y, p.y2)
                # Skip lead lines that cross the box border to reach external pins, or auxiliary contacts outside the box
                if ly1 < by1 or ly2 > by2 or lx1 < bx1 or lx2 > bx2:
                    continue
                assert lx1 - bx1 >= 2.0 - 1e-6, f"{lib_id} internal line ({p.x},{p.y})->({p.x2},{p.y2}) too close to left wall x={bx1}"
                assert bx2 - lx2 >= 2.0 - 1e-6, f"{lib_id} internal line ({p.x},{p.y})->({p.x2},{p.y2}) too close to right wall x={bx2}"
