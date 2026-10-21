"""Symbol curves stay smooth, and motor leads land straight.

The overload heater used to be a five-point zigzag, and the third pole was a
stair. The control transformer was two circles. A motor a few millimetres
under the overload got a stub that ran past the pin and came back, and the
IEC motor pitch (12 mm) did not match the 10 mm pole pitch, so U/V/W jogged
by different amounts.
"""

from __future__ import annotations

import math

import pytest

from panelcad import Session
from panelcad.core.primitives import (
    MAX_ARC_CHORD_MM,
    MIN_ARC_STEPS_PER_QUARTER,
    arc_point,
    arc_polyline,
    arc_steps,
    arc_through,
    chord_sagitta,
)
from panelcad.gui.symbol_draw import DrawModel
from panelcad.library.catalog import default_catalog
from panelcad.render.wiring import route_sheet_wires


def _arcs(device):
    return [prim for prim in device.primitives if prim.type == "arc"]


def _on_circle(x: float, y: float, cx: float, cy: float, radius: float, tol: float = 0.05) -> bool:
    return abs(math.hypot(x - cx, y - cy) - radius) <= tol


def test_arc_approximation_meets_the_segment_budget():
    """A quarter-turn has at least eight chords, and none leaves the arc by 0.15 mm."""

    assert arc_steps(8.0, 90.0) >= MIN_ARC_STEPS_PER_QUARTER
    points = arc_polyline(10.0, 12.0, 8.0, 20.0, -180.0)
    assert len(points) - 1 >= MIN_ARC_STEPS_PER_QUARTER * 2
    for x, y in points:
        assert _on_circle(x, y, 10.0, 12.0, 8.0, tol=1e-6)
    assert chord_sagitta(points, 10.0, 12.0, 8.0) <= MAX_ARC_CHORD_MM + 1e-6
    assert points[0] == pytest.approx(arc_point(10.0, 12.0, 8.0, 20.0))
    assert points[-1] == pytest.approx(arc_point(10.0, 12.0, 8.0, 20.0 - 180.0))


def test_arc_tool_stores_a_curve_instead_of_a_snapped_polyline():
    """The 1 mm grid snap is for corners. An arc keeps its solved center."""

    model = DrawModel(snap=1.0)
    assert model.add_arc_through((0.0, 0.0), (3.0, 2.0), (6.0, 0.0))
    item = model.to_draw()[-1]
    assert "arc" in item
    assert "polyline" not in item
    cx, cy, radius, start, sweep = item["arc"]
    assert radius == pytest.approx(math.hypot(cx, cy - 0.0), abs=1e-6)
    points = arc_polyline(cx, cy, radius, start, sweep)
    assert len(points) - 1 >= MIN_ARC_STEPS_PER_QUARTER
    # A 1 mm stair through the same three clicks would not sit on this circle.
    off_grid = [
        pt
        for pt in points[1:-1]
        if abs(pt[0] - round(pt[0])) > 0.05 or abs(pt[1] - round(pt[1])) > 0.05
    ]
    assert off_grid
    direct = arc_through((0.0, 0.0), (3.0, 2.0), (6.0, 0.0))
    assert direct is not None
    assert (cx, cy, radius) == pytest.approx(direct[:3])


def test_overload_heaters_are_smooth_matching_waves():
    """Each pole is the same sine of semicircles, not a stair on the third pole."""

    catalog = default_catalog()
    for library_id in (
        "iec.protection.overload_9a",
        "nema.protection.overload",
        "iec.breakers.mpcb_3p",
        "nema.breakers.mpcb_3p",
    ):
        device = catalog.get(library_id)
        arcs = _arcs(device)
        assert len(arcs) >= 12, library_id
        by_x: dict[float, list] = {}
        for arc in arcs:
            by_x.setdefault(round(arc.x, 2), []).append(arc)
        assert len(by_x) >= 3, library_id
        shapes = []
        for x, group in sorted(by_x.items()):
            assert len(group) >= 4, (library_id, x)
            for arc in group:
                assert abs(abs(arc.sweep) - 180.0) < 0.1, library_id
                points = arc_polyline(arc.x, arc.y, arc.r, arc.start, arc.sweep)
                assert len(points) - 1 >= MIN_ARC_STEPS_PER_QUARTER
                assert chord_sagitta(points, arc.x, arc.y, arc.r) <= MAX_ARC_CHORD_MM + 1e-6
            sweeps = [round(arc.sweep, 1) for arc in group]
            assert sweeps[0] == -sweeps[1]
            shapes.append([(round(arc.y - group[0].y, 2), round(arc.sweep, 1)) for arc in group])
        assert shapes[0] == shapes[1] == shapes[2], library_id


def test_control_transformer_is_coil_arcs_with_h_and_x_terminals():
    catalog = default_catalog()
    for library_id in ("iec.power.control_transformer", "nema.power.control_transformer"):
        device = catalog.get(library_id)
        pins = {pin.id: pin for pin in device.pins}
        assert {"H1", "H2", "H3", "H4", "X1", "X2"} <= set(pins), library_id
        # Two separate primary windings, H1-H2 over H3-H4, each the same size.
        ys = [pins[name].y for name in ("H1", "H2", "H3", "H4")]
        assert ys == sorted(ys) and len(set(ys)) == 4, (library_id, ys)
        primary = [arc for arc in _arcs(device) if arc.x < device.schematic_width / 2]
        upper = [arc for arc in primary if ys[0] <= arc.y <= ys[1]]
        lower = [arc for arc in primary if ys[2] <= arc.y <= ys[3]]
        assert len(upper) == len(lower) >= 3, library_id
        assert pins["H1"].y == pytest.approx(12.0)
        assert pins["H2"].y == pytest.approx(28.0)
        assert pins["X1"].y == pytest.approx(12.0)
        assert pins["X2"].y == pytest.approx(28.0)
        arcs = _arcs(device)
        assert len(arcs) >= 9, library_id
        assert not any(prim.type == "circle" for prim in device.primitives)
        for arc in arcs:
            points = arc_polyline(arc.x, arc.y, arc.r, arc.start, arc.sweep)
            assert len(points) - 1 >= MIN_ARC_STEPS_PER_QUARTER * 2
            assert chord_sagitta(points, arc.x, arc.y, arc.r) <= MAX_ARC_CHORD_MM + 1e-6
        labels = {prim.text for prim in device.primitives if prim.type == "text"}
        assert {"H1", "H2", "H3", "H4", "X1", "X2"} <= labels, library_id


def _vertical_leads(device):
    circle = next(prim for prim in device.primitives if prim.type == "circle")
    leads = []
    for prim in device.primitives:
        if prim.type != "line":
            continue
        if abs(prim.x - prim.x2) > 0.05:
            continue
        if min(prim.y, prim.y2) > 0.2:
            continue
        end_y = prim.y2 if abs(prim.y) <= abs(prim.y2) else prim.y
        leads.append((prim.x, end_y))
        assert _on_circle(prim.x, end_y, circle.x, circle.y, circle.r)
    return circle, leads


def test_three_phase_motor_leads_are_straight_and_even():
    catalog = default_catalog()
    for library_id, names in (
        ("iec.loads.motor_3ph", ("U1", "V1", "W1")),
        ("nema.loads.motor_3ph", ("T1", "T2", "T3")),
        ("iec.loads.motor_3ph_6lead", ("U1", "V1", "W1")),
        ("nema.loads.motor_3ph_6lead", ("T1", "T2", "T3")),
    ):
        device = catalog.get(library_id)
        pins = sorted((pin for pin in device.pins if pin.id in names), key=lambda pin: pin.x)
        xs = [pin.x for pin in pins]
        gaps = [b - a for a, b in zip(xs, xs[1:])]
        assert gaps == pytest.approx([10.0, 10.0], abs=0.05), (library_id, xs)
        _circle, leads = _vertical_leads(device)
        lead_x = sorted(x for x, _y in leads)
        assert lead_x == pytest.approx(xs, abs=0.05), library_id
        for prim in device.primitives:
            if prim.type != "line":
                continue
            dx = abs(prim.x2 - prim.x)
            dy = abs(prim.y2 - prim.y)
            assert dx < 0.05 or dy < 0.05, (library_id, prim)


def _routes(session: Session):
    sheet = session.sheet()
    return route_sheet_wires(sheet, session.catalog, project=session.project)


def _orthogonal(points) -> bool:
    return all(abs(a.x - b.x) <= 0.4 or abs(a.y - b.y) <= 0.4 for a, b in zip(points, points[1:]))


def test_column_motor_leads_drop_straight_when_the_pitch_matches():
    session = Session.create("Aligned motor")
    session.place("iec.protection.overload_9a", ref="F1", x=40, y=30, snap=False)
    session.place("iec.loads.motor_3ph", ref="M1", x=40, y=90, snap=False)
    session.wire("F1.2", "M1.U1", number="U")
    session.wire("F1.4", "M1.V1", number="V")
    session.wire("F1.6", "M1.W1", number="W")
    routes = _routes(session)
    xs = []
    for wire in session.sheet().wires:
        pts = routes[wire.id]
        assert _orthogonal(pts), (wire.number, pts)
        # Straight drop: every vertex on one x. Collinear midpoints stay so print import can split pin-to-pin wires.
        assert all(abs(p.x - pts[0].x) <= 0.05 for p in pts), (wire.number, pts)
        assert abs(pts[0].x - pts[-1].x) <= 0.4
        assert pts[-1].y > pts[0].y
        xs.append(pts[0].x)
    assert [b - a for a, b in zip(xs, xs[1:])] == pytest.approx([10.0, 10.0], abs=0.05)


def test_short_motor_drop_enters_vertically_without_a_hook():
    """A 2.5 mm column offset is one step. The wire does not run past the pin."""

    session = Session.create("Offset motor")
    session.place("iec.protection.overload_9a", ref="F1", x=40, y=155, snap=False)
    session.place("iec.loads.motor_3ph", ref="M1", x=42.5, y=210, snap=False)
    session.wire("F1.2", "M1.U1", number="U")
    session.wire("F1.4", "M1.V1", number="V")
    session.wire("F1.6", "M1.W1", number="W")
    routes = _routes(session)
    spans = []
    for wire in session.sheet().wires:
        pts = routes[wire.id]
        assert _orthogonal(pts), (wire.number, pts)
        assert abs(pts[-1].x - pts[-2].x) <= 0.4, pts
        ys = [p.y for p in pts]
        assert max(ys) - min(ys) <= (pts[-1].y - pts[0].y) + 0.4
        assert ys == sorted(ys), pts
        spans.append(abs(pts[-1].x - pts[0].x))
    assert spans == pytest.approx([spans[0]] * 3, abs=0.05)
    assert max(spans) < 3.0


def test_side_approach_lands_straight_on_even_motor_terminals():
    session = Session.create("Beside")
    session.place("iec.drives.vfd_1k5", ref="U1", x=52, y=40, snap=False)
    session.place("iec.loads.motor_3ph", ref="M1", x=150, y=60, snap=False)
    session.wire("U1.U", "M1.U1", number="201")
    session.wire("U1.V", "M1.V1", number="202")
    session.wire("U1.W", "M1.W1", number="203")
    routes = _routes(session)
    landings = []
    for wire in session.sheet().wires:
        pts = routes[wire.id]
        assert _orthogonal(pts), (wire.number, pts)
        assert abs(pts[-1].x - pts[-2].x) <= 0.4
        assert pts[-1].y != pytest.approx(pts[-2].y)
        landings.append(pts[-1].x)
    gaps = [b - a for a, b in zip(landings, landings[1:])]
    assert gaps == pytest.approx([10.0, 10.0], abs=0.05)
