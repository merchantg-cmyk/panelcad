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
        assert pins["X2"].y == pytest.approx(50.0 if library_id.startswith("nema.") else 28.0)
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


def test_nema_breakers_use_pure_arc_poles_and_contactors_use_circle_coil():
    catalog = default_catalog()
    for lib_id, poles in (
        ("nema.breakers.mcb_1p_16a", 1),
        ("nema.breakers.mcb_3p_32a", 3),
        ("nema.breakers.mccb_3p_100a", 3),
    ):
        dev = catalog.get(lib_id)
        arcs = _arcs(dev)
        circles = [prim for prim in dev.primitives if prim.type == "circle"]
        lines = [prim for prim in dev.primitives if prim.type == "line"]
        assert len(arcs) == poles, lib_id
        assert len(circles) == poles * 2, lib_id
        assert all(abs(line.x - line.x2) <= 0.05 for line in lines), (lib_id, lines)

    for lib_id in ("nema.contactors.ac3_9a", "nema.contactors.ac3_32a"):
        dev = catalog.get(lib_id)
        rects = [prim for prim in dev.primitives if prim.type == "rect"]
        circles = [prim for prim in dev.primitives if prim.type == "circle"]
        assert len(rects) == 1, lib_id
        assert len(circles) == 1, lib_id

    ol = catalog.get("nema.protection.overload")
    rects = [prim for prim in ol.primitives if prim.type == "rect"]
    assert len(rects) == 1
    box_right = rects[0].x + rects[0].w
    for prim in ol.primitives:
        if prim.type == "line":
            assert max(prim.x, prim.x2) <= box_right + 0.05
    for pin_id in ("95", "96"):
        pin = next(pin for pin in ol.pins if pin.id == pin_id)
        assert pin.x == pytest.approx(box_right)


def test_single_pole_nema_controls_do_not_receive_three_phase_supply_stubs():
    from panelcad.core.phase_color import pin_phase_map
    from panelcad.render.phase_ink import line_side_runs
    from panelcad.render.svg import sheet_to_svg

    session = Session.create("NEMA controls")
    session.place("nema.operators.pb_no", ref="PB1", x=25, y=40, snap=False)
    session.place("nema.operators.pb_nc", ref="PB2", x=75, y=40, snap=False)
    session.place("nema.operators.estop", ref="ESTOP1", x=125, y=40, snap=False)
    session.place("nema.protection.fuse_10a", ref="FU1", x=175, y=40, snap=False)
    assert pin_phase_map(session.sheet(), session.catalog) == {}
    runs = line_side_runs(session.project, session.sheet(), session.catalog)
    assert runs == []
    svg = sheet_to_svg(session.sheet(), session.catalog, project=session.project)
    assert 'width="100%"' not in svg
    assert 'fill="#f7f4ea"' in svg


def test_fuses_draw_box_before_leads_and_iec_3p_has_through_conductor():
    """Opaque fuse rectangles precede internal leads/S-curves so ink is not covered."""

    catalog = default_catalog()
    for lib_id in ("nema.protection.fuse_10a", "nema.protection.fuse", "nema.protection.fuse_3p"):
        dev = catalog.get(lib_id)
        rects = [(i, p) for i, p in enumerate(dev.primitives) if p.type == "rect"]
        assert rects, lib_id
        for r_idx, rect in rects:
            rx1, rx2 = rect.x, rect.x + rect.w
            ry1, ry2 = rect.y, rect.y + rect.h
            for p_idx, prim in enumerate(dev.primitives):
                if prim.type == "arc" and rx1 <= prim.x <= rx2 and ry1 <= prim.y <= ry2:
                    assert r_idx < p_idx, (lib_id, r_idx, p_idx)
                elif prim.type == "line":
                    # Any lead intersecting this pole's box must be drawn after the rect.
                    if max(prim.x, prim.x2) > rx1 and min(prim.x, prim.x2) < rx2 and max(prim.y, prim.y2) > ry1 and min(prim.y, prim.y2) < ry2:
                        assert r_idx < p_idx, (lib_id, r_idx, p_idx)
        # Every S-curve endpoint must meet a lead line.
        lines = [p for p in dev.primitives if p.type == "line"]
        ends = {(round(l.x, 3), round(l.y, 3)) for l in lines} | {(round(l.x2, 3), round(l.y2, 3)) for l in lines}
        arcs = [p for p in dev.primitives if p.type == "arc"]
        assert len(arcs) >= 2
        # Check outer endpoints of each 2-arc S-curve pair
        for i in range(0, len(arcs), 2):
            a1, a2 = arcs[i], arcs[i + 1]
            p_start = arc_point(a1.x, a1.y, a1.r, a1.start)
            p_end = arc_point(a2.x, a2.y, a2.r, a2.start + a2.sweep)
            assert (round(p_start[0], 3), round(p_start[1], 3)) in ends, (lib_id, p_start)
            assert (round(p_end[0], 3), round(p_end[1], 3)) in ends, (lib_id, p_end)

    iec_3p = catalog.get("iec.protection.fuse_3p")
    for x in (8.0, 18.0, 28.0):
        rect_idx = next(i for i, p in enumerate(iec_3p.primitives) if p.type == "rect" and p.x < x < p.x + p.w)
        line_idx = next(
            i
            for i, p in enumerate(iec_3p.primitives)
            if p.type == "line" and p.x == p.x2 == x and min(p.y, p.y2) == 0.0 and max(p.y, p.y2) == 50.0
        )
        assert rect_idx < line_idx, x


def test_iec_contactor_aux_contact_and_nema_safety_relay_geometry():
    """IEC 9A contactor has parallel blades + clean NO aux contact; NEMA safety relay has orthogonal coil leads."""

    catalog = default_catalog()
    km = catalog.get("iec.contactors.ac3_9a")
    blades = [p for p in km.primitives if p.type == "line" and p.y == 16.0 and p.y2 == 22.0]
    assert len(blades) == 3
    assert [round(b.x2 - b.x, 3) for b in sorted(blades, key=lambda b: b.x)] == [3.0, 3.0, 3.0]
    gang = next(p for p in km.primitives if p.type == "line" and p.y == p.y2 == 22.0)
    assert (gang.x, gang.x2) == pytest.approx((13.0, 33.0))
    aux_lines = [p for p in km.primitives if p.type == "line" and min(p.x, p.x2) >= 36.0]
    ends = {(round(p.x, 2), round(p.y, 2)) for p in aux_lines} | {(round(p.x2, 2), round(p.y2, 2)) for p in aux_lines}
    assert (40.0, 20.0) in ends and (40.0, 28.0) in ends
    assert len([p for p in aux_lines if p.x != p.x2 and p.y != p.y2]) == 1

    sr = catalog.get("nema.relays.safety_24vdc")
    coil_lines = [p for p in sr.primitives if p.type == "line" and min(p.x, p.x2) <= 16.0 and max(p.y, p.y2) <= 28.0]
    assert coil_lines
    assert all(p.x == p.x2 or p.y == p.y2 for p in coil_lines)
    texts = {p.text for p in sr.primitives if p.type == "text"}
    assert {"SR", "SFTY"} <= texts
    # Output contacts have vertical ticks at x=32 and x=30 and blades at y=16->20 and y=32->36.
    ticks = [p for p in sr.primitives if p.type == "line" and p.x == p.x2 in (30.0, 32.0)]
    assert len(ticks) == 4


def test_operators_estop_2nc_selector_and_limit_switches():
    """E-stop 2NC stem reaches lower blade, 2-pos selector pointer stays inside circle,
    rotated column selector labels stay clear of branches, and limit switches have closed cams."""
    from panelcad.core.models import Component
    from panelcad.render.painter import Painter, draw_device
    from panelcad.render.sheet import pin_world

    catalog = default_catalog()

    estop2 = catalog.get("iec.operators.estop_2nc")
    stems = [p for p in estop2.primitives if p.type == "line" and p.x == p.x2 == 14.0]
    assert stems and max(max(s.y, s.y2) for s in stems) >= 36.0

    sel2 = catalog.get("iec.operators.selector_2pos")
    circle = next(p for p in sel2.primitives if p.type == "circle")
    pointer = next(
        p for p in sel2.primitives if p.type == "line" and (p.x, p.y) == (circle.x, circle.y)
    )
    assert math.hypot(pointer.x2 - circle.x, pointer.y2 - circle.y) <= circle.r + 1e-6

    class _RecPainter(Painter):
        def __init__(self) -> None:
            self.texts: list[tuple[float, float, str, str]] = []

        def set_stroke(self, color: str, width: float) -> None:
            pass

        def set_fill(self, color: str | None) -> None:
            pass

        def rect(self, x: float, y: float, w: float, h: float) -> None:
            pass

        def line(self, x1: float, y1: float, x2: float, y2: float) -> None:
            pass

        def circle(self, x: float, y: float, r: float) -> None:
            pass

        def polyline(self, points: list[tuple[float, float]]) -> None:
            pass

        def text(
            self,
            x: float,
            y: float,
            text: str,
            size: float,
            align: str = "middle",
            rotate: float = 0,
        ) -> None:
            self.texts.append((x, y, text, align))

    sel3 = catalog.get("iec.operators.selector_3pos")
    comp = Component(id="S1", library_id="iec.operators.selector_3pos", x=40.0, y=40.0, rotation=90)
    rp = _RecPainter()
    draw_device(rp, sel3, comp, captions=False)
    hx, _ = pin_world(comp, sel3.pin("12"))
    ax, _ = pin_world(comp, sel3.pin("14"))
    lo_x, hi_x = min(hx, ax), max(hx, ax)
    by_label = {txt: (x, y, align) for x, y, txt, align in rp.texts}
    assert "Hand" in by_label and "Auto" in by_label
    assert by_label["Hand"][0] < lo_x or by_label["Hand"][0] > hi_x
    assert by_label["Auto"][0] < lo_x or by_label["Auto"][0] > hi_x

    for lib_id in (
        "iec.operators.limit_no",
        "iec.operators.limit_nc",
        "nema.operators.limit_no",
        "nema.operators.limit_nc",
    ):
        dev = catalog.get(lib_id)
        cams = [p for p in dev.primitives if p.type == "polyline"]
        assert len(cams) == 1, lib_id
        pts = cams[0].points
        assert len(pts) == 4 and pts[0] == pts[-1], (lib_id, pts)
        cam_bottom_y = max(pt[1] for pt in pts)
        stems = [
            p
            for p in dev.primitives
            if p.type == "line" and p.x == p.x2 and min(p.y, p.y2) == pytest.approx(cam_bottom_y)
        ]
        assert len(stems) == 1, lib_id
        blade = max(
            (p for p in dev.primitives if p.type == "line" and p.x != p.x2 and p.y != p.y2),
            key=lambda p: abs(p.x2 - p.x),
        )
        stem_x = stems[0].x
        stem_bot = max(stems[0].y, stems[0].y2)
        t = (stem_x - blade.x) / (blade.x2 - blade.x)
        assert 0.0 < t < 1.0
        blade_y_at_stem = blade.y + t * (blade.y2 - blade.y)
        assert stem_bot == pytest.approx(blade_y_at_stem, abs=0.05), lib_id


def test_timer_labels_clear_leads_and_contacts_have_valid_delay_marks():
    """On-delay timer labels clear pin-18/3 leads; timed contacts have non-overlapping delay marks."""

    catalog = default_catalog()
    for lib_id in ("iec.timers.on_delay", "nema.timers.on_delay"):
        dev = catalog.get(lib_id)
        for prim in dev.primitives:
            if prim.type == "text":
                assert not (prim.x >= 22.0 and abs(prim.y - 30.0) <= 3.0), (lib_id, prim)

    for lib_id in (
        "iec.timers.contact_notc",
        "iec.timers.contact_ncto",
        "iec.timers.contact_noto",
        "iec.timers.contact_nctc",
    ):
        dev = catalog.get(lib_id)
        arcs = [p for p in dev.primitives if p.type == "arc"]
        assert len(arcs) == 1, lib_id
        arc = arcs[0]
        # Vertical stem joins the blade to the delay arc center/crest.
        stems = [
            p
            for p in dev.primitives
            if p.type == "line" and p.x == p.x2 == pytest.approx(arc.x) and min(p.y, p.y2) < 10.0
        ]
        assert len(stems) == 1, lib_id

    for lib_id in (
        "nema.timers.contact_notc",
        "nema.timers.contact_ncto",
        "nema.timers.contact_noto",
        "nema.timers.contact_nctc",
    ):
        dev = catalog.get(lib_id)
        bars = [p for p in dev.primitives if p.type == "line" and p.x == p.x2 and p.x in (11.0, 17.0)]
        assert len(bars) == 2, lib_id
        bar_top = min(min(b.y, b.y2) for b in bars)
        bar_bot = max(max(b.y, b.y2) for b in bars)
        arrow_stem = next(p for p in dev.primitives if p.type == "line" and p.x == p.x2 == 14.0)
        arrow_head = next(p for p in dev.primitives if p.type == "polyline")
        arrow_bot = max(arrow_stem.y, arrow_stem.y2, *(pt[1] for pt in arrow_head.points))
        txt = next(p for p in dev.primitives if p.type == "text")
        assert bar_top > arrow_bot + 0.5, (lib_id, bar_top, arrow_bot)
        assert bar_bot < txt.y - txt.size + 0.05, (lib_id, bar_bot, txt)


def test_terminals_pe_fan_and_offpage_geometry():
    """Terminal 'X' sits inside its box, PE earth bars stand off the box with a stem,
    24V fan leads touch the circle, and off-page TO/FROM labels stay inside bounds."""

    catalog = default_catalog()

    for lib_id in ("iec.terminals.x_2p5", "iec.terminals.phoenix_ut_2p5"):
        dev = catalog.get(lib_id)
        rect = next(p for p in dev.primitives if p.type == "rect")
        txt = next(p for p in dev.primitives if p.type == "text" and p.text == "X")
        assert rect.x < txt.x < rect.x + rect.w
        assert rect.y + txt.size <= txt.y <= rect.y + rect.h

    pe = catalog.get("iec.terminals.pe_4")
    rect = next(p for p in pe.primitives if p.type == "rect")
    box_bot = rect.y + rect.h
    bars = [p for p in pe.primitives if p.type == "line" and p.y == p.y2 and p.y > box_bot]
    assert len(bars) == 3
    top_bar_y = min(b.y for b in bars)
    assert top_bar_y > box_bot + 1.0
    stems = [
        p
        for p in pe.primitives
        if p.type == "line"
        and p.x == p.x2 == 8.0
        and min(p.y, p.y2) == pytest.approx(box_bot)
        and max(p.y, p.y2) == pytest.approx(top_bar_y)
    ]
    assert len(stems) == 1

    fan = catalog.get("iec.loads.fan_24v")
    circle = next(p for p in fan.primitives if p.type == "circle")
    leads = [p for p in fan.primitives if p.type == "line" and min(p.x, p.x2) == 0.0]
    assert len(leads) == 2
    for lead in leads:
        end_x = max(lead.x, lead.x2)
        assert _on_circle(end_x, lead.y, circle.x, circle.y, circle.r, tol=0.05)

    for lib_id in ("iec.connection.offpage", "iec.connection.offpage_from"):
        dev = catalog.get(lib_id)
        txt = next(p for p in dev.primitives if p.type == "text")
        assert 0.0 <= txt.y - txt.size and txt.y <= 5.5, (lib_id, txt)





