"""Cleanup is the path that has to leave a readable sheet.

Each case calls Session.cleanup. tidy_schematic_sheet is not the contract.
"""

from __future__ import annotations

from pathlib import Path

from panelcad import Session
from panelcad.core.ladder import rail_xs
from panelcad.render.label_avoid import ON_WIRE, anchor_on_route, resolve_labels
from panelcad.render.sheet import pin_world
from panelcad.render.sheet_labels import sheet_symbol_bodies, sheet_text_boxes
from panelcad.render.titleblock import working_rect
from panelcad.render.wire_labels import boxes_overlap
from panelcad.render.wiring import route_sheet_wires

_ROOT = Path(__file__).resolve().parents[1]


def _phase_x(session: Session, sheet_id: str, ref: str) -> float:
    return _phase_pin_x(session, sheet_id, ref, 0)


def _middle_phase_x(session: Session, sheet_id: str, ref: str) -> float:
    sheet = session.project.sheet(sheet_id)
    comp = sheet.component(ref)
    device = session.catalog.get(comp.library_id)
    pins = sorted((pin for pin in device.pins if pin.side == "top"), key=lambda pin: pin.x)
    return _phase_pin_x(session, sheet_id, ref, len(pins) // 2)


def _phase_pin_x(session: Session, sheet_id: str, ref: str, index: int) -> float:
    sheet = session.project.sheet(sheet_id)
    comp = sheet.component(ref)
    device = session.catalog.get(comp.library_id)
    pins = sorted((pin for pin in device.pins if pin.side == "top"), key=lambda pin: pin.x)
    return pin_world(comp, pins[index])[0]


def _pose(session: Session, sheet_id: str):
    sheet = session.project.sheet(sheet_id)
    return [
        (comp.id, round(comp.x, 4), round(comp.y, 4), round(comp.scale, 4))
        for comp in sheet.components
    ]


def _no_body_overlap(session: Session, sheet_id: str) -> None:
    sheet = session.project.sheet(sheet_id)
    bodies = sheet_symbol_bodies(sheet, session.catalog)
    for index, (name, box) in enumerate(bodies):
        for other, obox in bodies[index + 1 :]:
            assert not boxes_overlap(box, obox), (name, other, box, obox)


def _collinear_pairs(session: Session, sheet_id: str) -> list[tuple[str, str]]:
    """Different wire numbers that share more than 1 mm of one horizontal or vertical run."""

    sheet = session.project.sheet(sheet_id)
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    segs: list[tuple[str, float, float, float, float]] = []
    for wire in sheet.wires:
        number = str(wire.number or wire.id)
        pts = routes.get(wire.id) or []
        for start, end in zip(pts, pts[1:]):
            segs.append((number, start.x, start.y, end.x, end.y))
    hits: list[tuple[str, str]] = []
    for index, (num_a, ax, ay, bx, by) in enumerate(segs):
        for num_b, cx, cy, dx, dy in segs[index + 1 :]:
            if num_a == num_b:
                continue
            if _shared_run(ax, ay, bx, by, cx, cy, dx, dy) > 1.0:
                hits.append((num_a, num_b))
    return hits


def _shared_run(
    ax: float, ay: float, bx: float, by: float, cx: float, cy: float, dx: float, dy: float
) -> float:
    if abs(ay - by) <= 0.4 and abs(cy - dy) <= 0.4 and abs(ay - cy) <= 0.4:
        a0, a1 = sorted((ax, bx))
        b0, b1 = sorted((cx, dx))
        return min(a1, b1) - max(a0, b0)
    if abs(ax - bx) <= 0.4 and abs(cx - dx) <= 0.4 and abs(ax - cx) <= 0.4:
        a0, a1 = sorted((ay, by))
        b0, b1 = sorted((cy, dy))
        return min(a1, b1) - max(a0, b0)
    return 0.0


def _numbers_on_wires(session: Session, sheet_id: str) -> None:
    sheet = session.project.sheet(sheet_id)
    ladder = str(getattr(sheet, "drawing_style", "") or "") == "ladder" or str(
        getattr(session.project.title_block, "drawing_style", "") or ""
    ) == "ladder"
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        assert len(pts) >= 2, wire.id
        for start, end in zip(pts, pts[1:]):
            assert abs(start.x - end.x) <= 0.6 or abs(start.y - end.y) <= 0.6, wire.id
    resolved = resolve_labels(session.project, session.catalog, sheet, ladder=ladder, sheet_no=1)
    for wire in sheet.wires:
        at = resolved.marks.get(wire.id)
        pts = resolved.routes.get(wire.id) or []
        if at is None or len(pts) < 2:
            continue
        assert anchor_on_route(at[0], at[1], pts, ON_WIRE), (wire.id, wire.number, at)


def _inside_rails(session: Session, sheet_id: str) -> None:
    sheet = session.project.sheet(sheet_id)
    left, right = rail_xs(sheet)
    _wx, wy, _ww, wh = working_rect(sheet)
    for comp in sheet.components:
        device = session.catalog.get(comp.library_id)
        scale = comp.visual_scale()
        box = (
            comp.x,
            comp.y,
            comp.x + float(device.schematic_width) * scale,
            comp.y + float(device.schematic_height) * scale,
        )
        assert box[0] >= left - 0.6, (comp.id, box, left)
        assert box[2] <= right + 0.6, (comp.id, box, right)
        assert box[1] >= wy - 0.6, comp.id
        assert box[3] <= wy + wh + 0.6, (comp.id, box[3], wy + wh)


def test_cleanup_keeps_the_dol_power_column():
    session = Session.open(_ROOT / "examples" / "dol_starter.pcad.yaml")
    session.cleanup("power")
    sheet = session.project.sheet("power")
    order = [sheet.component(ref).y for ref in ("Q1", "K1", "F1", "M1")]
    assert order == sorted(order)
    assert sheet.component("K1").y > sheet.component("Q1").y + 20
    xs = [_phase_x(session, "power", ref) for ref in ("Q1", "K1", "F1")]
    assert max(xs) - min(xs) < 0.6, xs
    assert abs(_middle_phase_x(session, "power", "F1") - _middle_phase_x(session, "power", "M1")) < 0.6
    assert abs(sheet.component("F1").x - sheet.component("Q1").x) < 8
    _no_body_overlap(session, "power")
    assert _collinear_pairs(session, "power") == []


def _build_feeder(style: str, branches: int = 3) -> Session:
    session = Session.create(f"Feeder {style or 'open'}")
    session.set_title_block(standard="NEMA/ANSI", drawing_style=style, rails="L1/L2")
    sheet = session.sheet()
    if style == "ladder":
        sheet.drawing_style = "ladder"

    def put(library_id: str, ref: str, x: float, y: float):
        session.place(library_id, ref=ref, x=x, y=y, snap=False)

    put("nema.protection.disconnect_3p", "DISC1", 36, 30)
    put("nema.protection.fuse_3p", "FU1", 36, 100)
    for index in range(1, branches + 1):
        origin = 80 + index * 70
        put("nema.breakers.mcb_3p_32a", f"CB{index}", origin, 30)
        put("nema.contactors.ac3_9a", f"M{index}", origin - 2, 90)
        put("nema.protection.overload", f"OL{index}", origin, 170)
        put("nema.loads.motor_3ph", f"MTR{index}", origin, 230)
    session.wire("DISC1.T1", "FU1.1")
    session.wire("DISC1.T2", "FU1.3")
    session.wire("DISC1.T3", "FU1.5")
    for index in range(1, branches + 1):
        session.wire("FU1.2", f"CB{index}.1")
        session.wire("FU1.4", f"CB{index}.3")
        session.wire("FU1.6", f"CB{index}.5")
        session.wire(f"CB{index}.2", f"M{index}.L1")
        session.wire(f"CB{index}.4", f"M{index}.L2")
        session.wire(f"CB{index}.6", f"M{index}.L3")
        session.wire(f"M{index}.T1", f"OL{index}.T1")
        session.wire(f"M{index}.T2", f"OL{index}.T2")
        session.wire(f"M{index}.T3", f"OL{index}.T3")
        session.wire(f"OL{index}.2", f"MTR{index}.T1")
        session.wire(f"OL{index}.4", f"MTR{index}.T2")
        session.wire(f"OL{index}.6", f"MTR{index}.T3")
    return session


def _assert_feeder_columns(session: Session, branches: int = 3) -> None:
    sheet_id = session.sheet().id
    disc = _phase_x(session, sheet_id, "DISC1")
    fuse = _phase_x(session, sheet_id, "FU1")
    assert abs(disc - fuse) < 0.6
    columns = []
    for index in range(1, branches + 1):
        xs = [
            _phase_x(session, sheet_id, ref)
            for ref in (f"CB{index}", f"M{index}", f"OL{index}", f"MTR{index}")
        ]
        assert max(xs) - min(xs) < 0.6, (index, xs)
        columns.append(xs[0])
        assert abs(xs[0] - disc) > 15
    assert columns == sorted(columns)
    assert columns[-1] - columns[0] > 20
    for comp in session.sheet().components:
        assert comp.visual_scale() >= 0.62
    _no_body_overlap(session, sheet_id)
    _inside_rails(session, sheet_id)
    assert _collinear_pairs(session, sheet_id) == []
    _tags_clear_of_bus_lanes(session, sheet_id)


def test_cleanup_gives_each_feeder_branch_its_own_column():
    for style in ("ladder", ""):
        session = _build_feeder(style)
        session.cleanup()
        _assert_feeder_columns(session)


def test_cleanup_does_not_rewire_a_generated_ladder():
    """A generated DOL ladder already reaches both rails. Cleanup must not short the coil."""

    session = Session.create("Generated")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", rails="L1/L2")
    session.insert_circuit(kind="dol", control="local", hp=5, voltage=480)
    sheet = session.project.sheet("motor-control")
    before = {(wire.frm, wire.to, str(wire.number or "")) for wire in sheet.wires}
    session.cleanup("motor-control")
    after = {(wire.frm, wire.to, str(wire.number or "")) for wire in sheet.wires}
    assert after == before
    assert _collinear_pairs(session, "motor-control") == []
    assert _wires_through_bodies(session, "motor-control") == []
    # The supply stays on its own rung, and the coil stays the return end.
    supply = sheet.component("PS1")
    contact = sheet.component("OL1-95")
    coil = sheet.component("M1")
    assert supply.y + 8 < contact.y
    assert coil.x > contact.x


def _wires_through_bodies(session: Session, sheet_id: str) -> list[tuple[str, str]]:
    """Wires whose middle runs through a symbol, ignoring the pin stub at the edge."""

    from math import hypot

    from panelcad.core.route import component_aabb

    sheet = session.project.sheet(sheet_id)
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    hits: list[tuple[str, str]] = []
    bodies = []
    for comp in sheet.components:
        try:
            device = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        scale = comp.visual_scale()
        width = float(device.schematic_width) * scale
        height = float(device.schematic_height) * scale
        x0, y0, x1, y1 = component_aabb(comp.x, comp.y, width, height, int(comp.rotation or 0), bool(comp.mirror))
        inset = min(4.0, (x1 - x0) * 0.2, (y1 - y0) * 0.2)
        bodies.append((comp.id, (x0 + inset, y0 + inset, x1 - inset, y1 - inset)))
    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        ends = {wire.frm.split(".", 1)[0], wire.to.split(".", 1)[0]}
        for start, end in zip(pts, pts[1:]):
            if hypot(end.x - start.x, end.y - start.y) < 3.0:
                continue
            for name, rect in bodies:
                if name in ends:
                    continue
                if _segment_inside(start, end, rect) > 1.5:
                    hits.append((str(wire.number or wire.id), name))
    return hits


def _segment_inside(start, end, rect: tuple[float, float, float, float]) -> float:
    from math import hypot

    x0, y0, x1, y1 = rect
    dx, dy = end.x - start.x, end.y - start.y
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, start.x - x0), (dx, x1 - start.x), (-dy, start.y - y0), (dy, y1 - start.y)):
        if abs(p) < 1e-9:
            if q < 0:
                return 0.0
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t) if t <= t1 else t0
            if t > t1:
                return 0.0
        else:
            if t < t0:
                return 0.0
            t1 = min(t1, t)
    if t1 <= t0:
        return 0.0
    return (t1 - t0) * hypot(dx, dy)


def test_rotated_starter_bus_keeps_each_phase_on_its_own_lane():
    """Three sideways starters on one off-page bus do not draw the phases as one wire."""

    session = Session.create("Bused starters")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", rails="L1/L2")
    session.sheet().drawing_style = "ladder"
    for index, y in enumerate((50.0, 120.0, 190.0), start=1):
        session.place("nema.loads.motor_3ph", ref=f"{index}MTR", x=50, y=y, rotation=90, snap=False)
        session.place("nema.protection.overload", ref=f"{index}OL", x=130, y=y, rotation=90, snap=False)
        session.place("nema.contactors.ac3_9a", ref=f"{index}M", x=210, y=y, rotation=90, snap=False)
        session.wire(f"{index}MTR.T1", f"{index}OL.T1", number=f"{index}0T1")
        session.wire(f"{index}MTR.T2", f"{index}OL.T2", number=f"{index}0T2")
        session.wire(f"{index}MTR.T3", f"{index}OL.T3", number=f"{index}0T3")
        session.wire(f"{index}OL.2", f"{index}M.L1", number=f"{index}02")
        session.wire(f"{index}OL.4", f"{index}M.L2", number=f"{index}04")
        session.wire(f"{index}OL.6", f"{index}M.L3", number=f"{index}06")
    for phase, pin, number, y in (
        (0, "L1", "101", 36.0),
        (1, "L2", "103", 52.0),
        (2, "L3", "105", 68.0),
    ):
        session.place("iec.connection.offpage", ref=f"BUS{phase}", x=340, y=y, snap=False)
        for index in range(1, 4):
            session.wire(f"BUS{phase}.A", f"{index}M.{pin}", number=number)
    session.cleanup()
    assert _collinear_pairs(session, session.sheet().id) == []
    first = _pose(session, session.sheet().id)
    session.cleanup()
    assert _pose(session, session.sheet().id) == first


def test_cleanup_fits_eight_branches_and_reports_fourteen():
    """Eight columns scale down and fit. Fourteen report overflow without new overlaps."""

    session = _build_feeder("ladder", branches=8)
    session.cleanup()
    _assert_feeder_columns(session, branches=8)
    assert session.sheet().id not in (session.project.properties.get("power_column_overflow") or {})


def _overlap_ids(session: Session, sheet_id: str) -> set[tuple[str, str]]:
    sheet = session.project.sheet(sheet_id)
    bodies = sheet_symbol_bodies(sheet, session.catalog)
    hits: set[tuple[str, str]] = set()
    for index, (name, box) in enumerate(bodies):
        for other, obox in bodies[index + 1 :]:
            if boxes_overlap(box, obox):
                hits.add(tuple(sorted((name, other))))
    return hits


def test_overflow_feeder_is_a_fixed_point():
    """Branches that do not fit stay put. A second Cleanup does not move them."""

    for style in ("ladder", ""):
        for branches in (11, 14):
            session = _build_feeder(style, branches=branches)
            sheet_id = session.sheet().id
            before_overlap = _overlap_ids(session, sheet_id)
            before_wires = len(_collinear_pairs(session, sheet_id))
            session.cleanup()
            assert sheet_id in (session.project.properties.get("power_column_overflow") or {})
            assert _overlap_ids(session, sheet_id) <= before_overlap
            assert len(_collinear_pairs(session, sheet_id)) <= before_wires
            first = _pose(session, sheet_id)
            session.cleanup()
            assert _pose(session, sheet_id) == first


def test_cleanup_leaves_a_rejoining_power_sheet():
    """Forward/reverse and star/delta meet again. Cleanup must not split them."""

    for kind in ("reversing", "star_delta"):
        session = Session.create(kind)
        session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", rails="L1/L2")
        result = session.insert_circuit(kind=kind, control="local", hp=5, voltage=480)
        sheet_id = result["sheet"]
        before = {
            (wire.frm, wire.to, str(wire.number or ""))
            for wire in session.project.sheet(sheet_id).wires
        }
        assert _collinear_pairs(session, sheet_id) == []
        assert _wires_through_bodies(session, sheet_id) == []
        session.cleanup(sheet_id)
        after = {
            (wire.frm, wire.to, str(wire.number or ""))
            for wire in session.project.sheet(sheet_id).wires
        }
        assert after == before
        assert _collinear_pairs(session, sheet_id) == []
        assert _wires_through_bodies(session, sheet_id) == []
        first = _pose(session, sheet_id)
        session.cleanup(sheet_id)
        assert _pose(session, sheet_id) == first


def test_mirrored_starter_bus_keeps_each_phase_on_its_own_lane():
    """Markers on the left of rotation-270 starters still get one lane per phase."""

    session = Session.create("Bused starters left")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", rails="L1/L2")
    session.sheet().drawing_style = "ladder"
    for index, y in enumerate((50.0, 120.0, 190.0), start=1):
        session.place("nema.contactors.ac3_9a", ref=f"{index}M", x=110, y=y, rotation=270, snap=False)
        session.place("nema.protection.overload", ref=f"{index}OL", x=190, y=y, rotation=270, snap=False)
        session.place("nema.loads.motor_3ph", ref=f"{index}MTR", x=270, y=y, rotation=270, snap=False)
        session.wire(f"{index}OL.2", f"{index}M.T1", number=f"{index}02")
        session.wire(f"{index}OL.4", f"{index}M.T2", number=f"{index}04")
        session.wire(f"{index}OL.6", f"{index}M.T3", number=f"{index}06")
    for phase, pin, number, y in (
        (0, "L1", "101", 30.0),
        (1, "L2", "103", 46.0),
        (2, "L3", "105", 62.0),
    ):
        session.place("iec.connection.offpage", ref=f"BUS{phase}", x=60, y=y, snap=False)
        for index in range(1, 4):
            session.wire(f"BUS{phase}.A", f"{index}M.{pin}", number=number)
    session.cleanup()
    assert _collinear_pairs(session, session.sheet().id) == []


def _tags_clear_of_bus_lanes(session: Session, sheet_id: str) -> None:
    """Breaker tags sit off the horizontal phase lanes above the columns."""

    from math import hypot

    from panelcad.core.ink import text_box

    sheet = session.project.sheet(sheet_id)
    resolved = resolve_labels(session.project, session.catalog, sheet, ladder=True, sheet_no=1)
    lanes = []
    for pts in resolved.routes.values():
        for start, end in zip(pts, pts[1:]):
            if abs(start.y - end.y) > 0.6 or hypot(end.x - start.x, end.y - start.y) < 18.0:
                continue
            lanes.append(
                (
                    min(start.x, end.x) - 0.4,
                    min(start.y, end.y) - 0.4,
                    max(start.x, end.x) + 0.4,
                    max(start.y, end.y) + 0.4,
                )
            )
    for comp in sheet.components:
        if not comp.id.startswith("CB"):
            continue
        layout = resolved.tags.get(comp.id)
        if layout is None:
            continue
        box = text_box(layout[0], layout[1], comp.id, layout[3], layout[2])
        if box is None:
            continue
        for lane in lanes:
            assert not boxes_overlap(box, lane, pad=0.0), (comp.id, box, lane)


def test_cleanup_fits_six_branches_inside_the_rails():
    session = _build_feeder("ladder", branches=6)
    session.cleanup()
    _assert_feeder_columns(session, branches=6)
    first = _pose(session, session.sheet().id)
    session.cleanup()
    session.cleanup()
    assert _pose(session, session.sheet().id) == first


def test_generated_ladder_is_stable_and_keeps_its_wires(tmp_path: Path):
    session = Session.create("Generated")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", rails="L1/L2")
    session.insert_circuit(kind="dol", control="local", hp=5, voltage=480)
    session.cleanup("motor-control")
    sheet = session.project.sheet("motor-control")
    assert sheet.wires
    first = _pose(session, "motor-control")
    session.cleanup("motor-control")
    assert _pose(session, "motor-control") == first
    pdf = session.export_pdf(tmp_path / "generated.pdf")
    assert pdf.is_file() and pdf.stat().st_size > 1000
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    assert all(len(routes.get(wire.id) or []) >= 2 for wire in sheet.wires)


def test_sideways_starters_stay_put_after_the_first_cleanup():
    session = Session.create("GE 9C style")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", rails="L1/L2")
    session.sheet().drawing_style = "ladder"
    # Three horizontal starter rows. Rotation 90 turns the phase pins into a stack of lanes.
    for index, y in enumerate((40, 110, 180), start=1):
        session.place("nema.loads.motor_3ph", ref=f"{index}MTR", x=40, y=y, rotation=90, snap=False)
        session.place("nema.protection.overload", ref=f"{index}OL", x=100, y=y, rotation=90, snap=False)
        session.place("nema.contactors.ac3_9a", ref=f"{index}M", x=170, y=y, rotation=90, snap=False)
        session.wire(f"{index}MTR.T1", f"{index}OL.T1", number=f"{index}0T1")
        session.wire(f"{index}MTR.T2", f"{index}OL.T2", number=f"{index}0T2")
        session.wire(f"{index}MTR.T3", f"{index}OL.T3", number=f"{index}0T3")
        session.wire(f"{index}OL.2", f"{index}M.L1", number=f"{index}02")
        session.wire(f"{index}OL.4", f"{index}M.L2", number=f"{index}04")
        session.wire(f"{index}OL.6", f"{index}M.L3", number=f"{index}06")
    session.cleanup()
    _inside_working(session, session.sheet().id)
    assert _collinear_pairs(session, session.sheet().id) == []
    first = _pose(session, session.sheet().id)
    session.cleanup()
    session.cleanup()
    assert _pose(session, session.sheet().id) == first


def _inside_working(session: Session, sheet_id: str) -> None:
    sheet = session.project.sheet(sheet_id)
    wx, wy, ww, wh = working_rect(sheet)
    for comp in sheet.components:
        device = session.catalog.get(comp.library_id)
        scale = comp.visual_scale()
        # Rotation swaps the drawn box. Use the axis-aligned body from the layout helper's idea:
        width = float(device.schematic_width) * scale
        height = float(device.schematic_height) * scale
        if int(comp.rotation or 0) % 180 == 90:
            width, height = height, width
        assert comp.x >= wx - 0.6, comp.id
        assert comp.y >= wy - 0.6, comp.id
        assert comp.x + width <= wx + ww + 0.6, (comp.id, comp.x + width)
        assert comp.y + height <= wy + wh + 0.6, (comp.id, comp.y + height)


def test_cleanup_three_times_does_not_shrink_or_move():
    session = _build_feeder("ladder")
    session.cleanup()
    _assert_feeder_columns(session)
    first = _pose(session, session.sheet().id)
    session.cleanup()
    second = _pose(session, session.sheet().id)
    session.cleanup()
    third = _pose(session, session.sheet().id)
    assert second == first
    assert third == first
    assert min(row[3] for row in third) >= 0.62


def test_cleanup_power_sheet_keeps_terminal_and_motor_columns():
    """The conveyor power shape: a fuse column, a motor column, and strip X1."""

    session = Session.create("Two columns")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", rails="L1/L2")
    session.sheet().drawing_style = "ladder"

    def put(library_id: str, ref: str, x: float, y: float, **props):
        session.place(library_id, ref=ref, x=x, y=y, snap=False, properties=props or None)

    put("nema.protection.disconnect_3p", "DISC1", 40, 40)
    put("nema.protection.fuse_3p", "FU1", 40, 110)
    put("nema.power.control_transformer", "T1", 120, 180)
    put("nema.protection.fuse_10a", "FU2", 190, 175)
    put("nema.breakers.mcb_3p_32a", "CB1", 225, 22)
    put("nema.contactors.ac3_9a", "M1", 225, 80)
    put("nema.protection.overload", "OL1", 225, 155)
    put("nema.loads.motor_3ph", "MTR1", 225, 210)
    for index, y in enumerate((40, 67, 95, 125, 152, 180), start=1):
        put("iec.terminals.x_2p5", f"X1-{index}", 40, y, strip="X1", terminal=str(index))
    put("iec.terminals.pe_4", "X1-7", 40, 207, strip="X1", terminal="7")
    session.wire("DISC1.T1", "FU1.1", number="101")
    session.wire("DISC1.T2", "FU1.3", number="102")
    session.wire("DISC1.T3", "FU1.5", number="103")
    session.wire("FU1.2", "T1.H1", number="104")
    session.wire("FU1.4", "T1.H2", number="105")
    session.wire("T1.X1", "FU2.1", number="106")
    session.wire("CB1.2", "M1.L1", number="L1")
    session.wire("CB1.4", "M1.L2", number="L2")
    session.wire("CB1.6", "M1.L3", number="L3")
    session.wire("M1.T1", "OL1.T1", number="107")
    session.wire("M1.T2", "OL1.T2", number="108")
    session.wire("M1.T3", "OL1.T3", number="109")
    session.wire("OL1.2", "MTR1.T1", number="107")
    session.wire("OL1.4", "MTR1.T2", number="108")
    session.wire("OL1.6", "MTR1.T3", number="109")
    session.cleanup()
    sheet_id = session.sheet().id
    assert abs(_phase_x(session, sheet_id, "DISC1") - _phase_x(session, sheet_id, "FU1")) < 0.6
    load = [_phase_x(session, sheet_id, ref) for ref in ("CB1", "M1", "OL1", "MTR1")]
    assert max(load) - min(load) < 0.6, load
    assert load[0] > _phase_x(session, sheet_id, "FU1") + 30
    terminals = [session.sheet().component(f"X1-{index}") for index in range(1, 8)]
    assert len({round(comp.x, 2) for comp in terminals}) == 1
    assert [comp.y for comp in terminals] == sorted(comp.y for comp in terminals)
    assert terminals[0].x + 8 < session.sheet().component("DISC1").x
    assert session.sheet().component("T1").x > session.sheet().component("FU1").x
    _no_body_overlap(session, sheet_id)
    _numbers_on_wires(session, sheet_id)


def test_cleanup_reflows_an_overlapping_ladder_inside_the_rails():
    session = Session.create("Crowded ladder")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", rails="L1/L2")
    session.sheet().drawing_style = "ladder"

    def put(library_id: str, ref: str, x: float, y: float):
        session.place(library_id, ref=ref, x=x, y=y, snap=False)

    # Pin Y of a pushbutton is origin+24; a coil is origin+14; a lamp is origin+12.
    rungs = (40, 68, 96, 124, 152, 180)
    put("nema.operators.pb_no", "PB1", 70, rungs[0])
    put("nema.contactors.coil", "CR1", 280, rungs[0] + 10)
    put("nema.operators.pb_nc", "PB2", 70, rungs[1])
    put("nema.operators.pilot_light", "PL1", 280, rungs[1])
    put("nema.contactors.aux_no", "CR1-1", 120, rungs[2])
    put("nema.operators.pilot_light", "PL2", 280, rungs[2] - 4)
    put("nema.loads.solenoid", "SOL1", 300, rungs[3])
    put("nema.operators.pilot_light", "PL3", 292, rungs[3] + 4)
    put("nema.operators.limit_no", "LS1", 70, rungs[4])
    put("nema.contactors.coil", "CR2", 520, rungs[4] + 10)
    put("nema.contactors.aux_no", "CR2-1", 70, rungs[5])
    put("nema.protection.ol_contact", "OL1", 560, rungs[5])
    session.wire("PB1.3", "CR1.A1", number="201")
    session.wire("PB2.1", "PL1.X1", number="202")
    session.wire("CR1-1.3", "PL2.X1", number="210")
    session.wire("SOL1.A1", "PL3.X1", number="211")
    session.wire("LS1.3", "CR2.A1", number="212")
    session.wire("CR2-1.3", "OL1.95", number="215")
    session.cleanup()
    _no_body_overlap(session, session.sheet().id)
    _inside_rails(session, session.sheet().id)
    _numbers_on_wires(session, session.sheet().id)
    first = _pose(session, session.sheet().id)
    session.cleanup()
    assert _pose(session, session.sheet().id) == first


def test_conveyor_sheets_do_not_stack_different_wire_numbers():
    candidates = [
        Path("/workspace/panelcad-testpass/out/A/a1_step7.pcad.yaml"),
        Path("/home/ubuntu/.cursor/projects/workspace/uploads/a1_step7.pcad_67e7.yaml"),
    ]
    path = next((item for item in candidates if item.is_file()), None)
    if path is None:
        return
    session = Session.open(path)
    session.cleanup("main")
    session.cleanup("control-ladder")
    assert _collinear_pairs(session, "main") == []
    assert _collinear_pairs(session, "control-ladder") == []
    _no_body_overlap(session, "main")
    _no_body_overlap(session, "control-ladder")


def test_cleanup_builds_a_ladder_that_is_not_already_runged():
    """One button and a coil are not two rungs yet. Cleanup still builds the ladder."""

    session = Session.create("Clear ladder")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", rails="L1/L2")
    session.sheet().drawing_style = "ladder"
    session.place("nema.operators.pb_no", ref="PB1", x=70, y=40, snap=False)
    session.place("nema.contactors.coil", ref="CR1", x=260, y=36, snap=False)
    session.place("nema.operators.pilot_light", ref="PL1", x=250, y=120, snap=False)
    session.wire("PB1.3", "CR1.A1", number="201")
    session.wire("CR1.A2", "PL1.X1", number="202")
    session.cleanup()
    sheet = session.sheet()
    left, right = rail_xs(sheet)
    assert sheet.component("PB1").x < sheet.component("CR1").x
    assert sheet.component("CR1").x > (left + right) / 2
    _no_body_overlap(session, sheet.id)
    _numbers_on_wires(session, sheet.id)
    _inside_rails(session, sheet.id)


def _text_on_other_symbols(session: Session, sheet_id: str) -> list[tuple[str, str]]:
    """Device tags and extra captions that land on some other symbol."""

    sheet = session.project.sheet(sheet_id)
    bodies = sheet_symbol_bodies(sheet, session.catalog)
    hits: list[tuple[str, str]] = []
    for name, box in sheet_text_boxes(session.project, session.catalog, sheet):
        if not (name.startswith("tag:") or name.startswith("caption:")):
            continue
        owner = name.split(":")[1]
        for body_name, body in bodies:
            other = body_name.split(":", 1)[1]
            if other == owner:
                continue
            if boxes_overlap(box, body):
                hits.append((name, other))
    return hits


def _caption_overlaps(session: Session, sheet_id: str) -> list[tuple[str, str]]:
    sheet = session.project.sheet(sheet_id)
    caps = [
        (name, box)
        for name, box in sheet_text_boxes(session.project, session.catalog, sheet)
        if name.startswith("caption:")
    ]
    hits: list[tuple[str, str]] = []
    for index, (name, box) in enumerate(caps):
        owner = name.split(":")[1]
        for other, obox in caps[index + 1 :]:
            if other.split(":")[1] == owner:
                continue
            if boxes_overlap(box, obox):
                hits.append((name, other))
    return hits


def test_cleanup_leaves_a_dense_power_sheet_readable():
    """Single-pole fuses between multi-pole devices stay where they were drawn.

    Column layout used to gather that fuse row as side parts, so captions and
    boxes landed on each other and the phase wires cut through the fuses.
    """

    session = Session.create("Dense power")
    session.set_title_block(standard="IEC", drawing_style="")

    def put(library_id: str, ref: str, x: float, y: float, rotation: int = 0) -> None:
        session.place(library_id, ref=ref, x=x, y=y, rotation=rotation, snap=False)

    put("iec.protection.disconnect_3p", "Q1", 40, 20)
    for index, ref in enumerate(("F1A", "F1B", "F1C")):
        put("iec.protection.fuse_10a", ref, 22 + index * 44, 76, rotation=90)
    for index, ref in enumerate(("F2A", "F2B", "F2C")):
        put("iec.protection.fuse_10a", ref, 22 + index * 44, 116, rotation=90)
    put("iec.drives.vfd_1k5", "U1", 52, 156)
    put("iec.loads.motor_3ph", "M1", 150, 178)
    put("iec.relays.k_4co_24vdc", "K1", 150, 108)
    put("iec.protection.fuse_10a", "F3A", 248, 32, rotation=90)
    put("iec.protection.fuse_10a", "F3B", 288, 32, rotation=90)
    put("iec.power.control_transformer", "T1", 248, 78)
    put("iec.power.psu_24v_5a", "G1", 248, 138)
    session.wire("Q1.2", "F1A.1", number="L1")
    session.wire("Q1.4", "F1B.1", number="L2")
    session.wire("Q1.6", "F1C.1", number="L3")
    session.wire("F1A.2", "F2A.1", number="L1")
    session.wire("F1B.2", "F2B.1", number="L2")
    session.wire("F1C.2", "F2C.1", number="L3")
    session.wire("F2A.2", "U1.L1", number="L1")
    session.wire("F2B.2", "U1.L2", number="L2")
    session.wire("F2C.2", "U1.L3", number="L3")
    session.wire("U1.U", "M1.U1", number="201")
    session.wire("U1.V", "M1.V1", number="202")
    session.wire("U1.W", "M1.W1", number="203")
    session.wire("F1A.2", "F3A.1", number="L1")
    session.wire("F1B.2", "F3B.1", number="L2")
    session.wire("F3A.2", "T1.H1", number="L1")
    session.wire("F3B.2", "T1.H2", number="L2")
    session.wire("K1.14", "U1.DI1", number="204")
    session.wire("U1.COM", "G1.L", number="0V")
    sheet = session.sheet()
    for ref in ("F1A", "F1B", "F1C", "F2A", "F2B", "F2C", "F3A", "F3B"):
        assert sheet.component(ref).rotation == 90
    session.cleanup(sheet.id)
    for ref in ("F1A", "F1B", "F1C", "F2A", "F2B", "F2C", "F3A", "F3B"):
        assert sheet.component(ref).rotation == 90
    _no_body_overlap(session, sheet.id)
    assert _text_on_other_symbols(session, sheet.id) == []
    assert _caption_overlaps(session, sheet.id) == []
    assert _wires_through_bodies(session, sheet.id) == []
    leads = {"201", "202", "203", "0V"}
    bundled = [pair for pair in _collinear_pairs(session, sheet.id) if set(pair) <= leads]
    assert bundled == []
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        for index, (start, end) in enumerate(zip(pts, pts[1:])):
            for left, right in list(zip(pts, pts[1:]))[index + 1 :]:
                if abs(start.x - end.x) <= 0.4 and abs(left.x - right.x) <= 0.4 and abs(start.x - left.x) <= 0.4:
                    span = min(max(start.y, end.y), max(left.y, right.y)) - max(
                        min(start.y, end.y), min(left.y, right.y)
                    )
                    assert span <= 0.8, (wire.number, pts)
    parked = _pose(session, sheet.id)
    session.cleanup(sheet.id)
    assert _pose(session, sheet.id) == parked


def test_cleanup_keeps_rotated_starter_wire_numbers_on_the_wire():
    """Sideways starters are not restacked. Their numbers still sit on the lead."""

    session = Session.create("Sideways")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="", rails="L1/L2")
    session.place("nema.contactors.ac3_9a", ref="12M-G", x=80, y=60, rotation=90, snap=False)
    session.place("nema.protection.overload", ref="12OL-G", x=150, y=60, rotation=90, snap=False)
    session.place("nema.loads.motor_3ph", ref="12MTR", x=230, y=70, rotation=90, snap=False)
    session.wire("12M-G.T1", "12OL-G.T1", number="10T3")
    session.wire("12OL-G.2", "12MTR.T1", number="102")
    session.cleanup()
    for comp in session.sheet().components:
        assert comp.rotation == 90
    _numbers_on_wires(session, session.sheet().id)
