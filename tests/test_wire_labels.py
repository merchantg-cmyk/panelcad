"""Wire-number text sits beside the conductor, not on dots or other numbers."""

import shutil
from math import hypot
from pathlib import Path

from panelcad import Session
from panelcad.core.geometry import Point
from panelcad.examples.dol import build_dol
from panelcad.render.wire_labels import (
    CLEAR,
    MARK_GAP,
    box_gap,
    boxes_overlap,
    fallback_anchor,
    mark_box,
    place_wire_marks,
)
from panelcad.render.wiring import route_sheet_wires


def _marks_clear(session: Session, sheet_id: str | None = None) -> None:
    from panelcad.core.ladder import is_ladder_sheet
    from panelcad.render.titleblock import working_rect
    from panelcad.render.wire_labels import bundle_spec_plan

    sheet = session.sheet(sheet_id)
    if sheet.kind in {"panel_layout", "door_layout", "terminal"}:
        return
    ladder = is_ladder_sheet(sheet, session.project)
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    marks = place_wire_marks(sheet, session.catalog, routes, ladder=ladder, project=session.project)
    specs = bundle_spec_plan(sheet, routes, ladder=ladder)
    boxes: list[tuple[str, tuple[float, float, float, float]]] = []
    for wire in sheet.wires:
        at = marks.get(wire.id)
        if not at:
            continue
        pts = routes.get(wire.id) or []
        assert pts, wire.id
        x, y, align = at
        box = mark_box(
            x,
            y,
            wire.drawn_mark(ladder=ladder),
            align=align,
            spec=specs.get(wire.id, ""),
        )
        wx, wy, ww, wh = working_rect(sheet)
        assert (
            box[0] >= wx - 0.05
            and box[1] >= wy - 0.05
            and box[2] <= wx + ww + 0.05
            and box[3] <= wy + wh + 0.05
        ), f"{sheet.id}:{wire.id} outside working area {box} vs {(wx, wy, ww, wh)}"
        for end in (pts[0], pts[-1]):
            # A short stub's number sits beside the terminal, so the anchor can
            # be within 2 mm of the dot. Covering the terminal point is not.
            assert not (
                box[0] <= end.x <= box[2] and box[1] <= end.y <= box[3]
            ), f"{sheet.id}:{wire.id} covers endpoint"
            assert hypot(x - end.x, y - end.y) >= 2.0, f"{sheet.id}:{wire.id} on endpoint"
        for a, b in zip(pts, pts[1:]):
            seg = (
                min(a.x, b.x) - 0.45,
                min(a.y, b.y) - 0.45,
                max(a.x, b.x) + 0.45,
                max(a.y, b.y) + 0.45,
            )
            assert not boxes_overlap(box, seg), f"{sheet.id}:{wire.id} overlaps conductor"
        for ox, oy in ((pts[0].x, pts[0].y), (pts[-1].x, pts[-1].y)):
            dot = (ox - 1.4, oy - 1.4, ox + 1.4, oy + 1.4)
            assert not boxes_overlap(box, dot), f"{sheet.id}:{wire.id} overlaps connection dot"
        for other in sheet.wires:
            if other.id == wire.id:
                continue
            opts = routes.get(other.id) or []
            for a, b in zip(opts, opts[1:]):
                seg = (
                    min(a.x, b.x) - 0.45,
                    min(a.y, b.y) - 0.45,
                    max(a.x, b.x) + 0.45,
                    max(a.y, b.y) + 0.45,
                )
                assert not boxes_overlap(box, seg), f"{sheet.id}:{wire.id} overlaps {other.id}"
            if len(opts) >= 2:
                for ox, oy in ((opts[0].x, opts[0].y), (opts[-1].x, opts[-1].y)):
                    dot = (ox - 1.4, oy - 1.4, ox + 1.4, oy + 1.4)
                    assert not boxes_overlap(box, dot), f"{sheet.id}:{wire.id} overlaps {other.id} dot"
        for other_id, other in boxes:
            assert box_gap(box, other) >= MARK_GAP - 1e-6, (
                f"{sheet.id}:{wire.id} within {MARK_GAP} mm of {other_id}"
            )
        leads = [
            ((a.x + b.x) / 2.0, min(a.y, b.y), max(a.y, b.y))
            for a, b in zip(pts, pts[1:])
            if abs(a.x - b.x) <= 0.4 and abs(a.y - b.y) >= 2.0
        ]
        if len(leads) >= 2:
            lo_x = min(item[0] for item in leads)
            hi_x = max(item[0] for item in leads)
            y0 = min(item[1] for item in leads)
            y1 = max(item[2] for item in leads)
            # Beside its own leads is fine. Stretching across them is not, even
            # when the anchor sits on the bridge: that is not an on-wire label.
            spans_leads = box[0] < lo_x + 0.3 and box[2] > hi_x - 0.3
            near_leads = box[1] <= y1 + 2.0 and box[3] >= y0 - 2.0
            assert not (spans_leads and near_leads), (
                f"{sheet.id}:{wire.id} crosses its own leads {box}"
            )
        boxes.append((wire.id, box))


def test_two_point_vertical_uses_midpoint_not_endpoint():
    a = Point(x=60, y=20)
    b = Point(x=60, y=80)
    x, y, align = fallback_anchor([a, b])
    assert abs(y - 50) < 1.0
    assert abs(x - 60) >= CLEAR - 0.05
    assert align in {"start", "end"}


def test_power_tags_clear_wire_labels_by_one_text_height():
    """K1 must not share the L1/L2/L3 row, and 104 must not sit on M1."""

    s = build_dol()
    sheet = s.sheet("power")
    routes = route_sheet_wires(sheet, s.catalog, project=s.project)
    marks = place_wire_marks(sheet, s.catalog, routes, project=s.project)
    for number, tag in (("L1", "K1"), ("L2", "K1"), ("L3", "K1"), ("104", "M1"), ("105", "M1"), ("106", "M1")):
        wire = next(item for item in sheet.wires if item.number == number)
        at = marks[wire.id]
        box = mark_box(at[0], at[1], wire.drawn_mark(), align=at[2])
        comp = sheet.component(tag)
        tag_box = mark_box(comp.x, comp.y - 2.5, tag, align="start", size=3.0)
        assert not boxes_overlap(box, tag_box), f"{number} overlaps {tag}"
        assert box_gap(box, tag_box) >= 0.0


def test_dol_power_numbers_clear_of_motor_dots():
    s = build_dol()
    _marks_clear(s, "power")
    sheet = s.sheet("power")
    routes = route_sheet_wires(sheet, s.catalog, project=s.project)
    marks = place_wire_marks(sheet, s.catalog, routes, project=s.project)
    for net in ("U1", "V1", "W1"):
        motor = next(w for w in sheet.wires if w.net == net)
        at = marks[motor.id]
        end = routes[motor.id][-1]
        assert hypot(at[0] - end.x, at[1] - end.y) > 2.4
    motor = sheet.component("M1")
    tag = mark_box(motor.x, motor.y - 2.5, "M1", align="start", size=3.0)
    for net in ("U1", "V1", "W1"):
        w = next(item for item in sheet.wires if item.net == net)
        at = marks[w.id]
        box = mark_box(at[0], at[1], w.drawn_mark(), align=at[2])
        assert not boxes_overlap(box, tag), f"{net} overlaps M1 tag"


def test_every_builtin_and_nema_circuit_builder_sheet_is_clear(tmp_path: Path):
    jobs = [build_dol()]
    examples = Path(__file__).resolve().parents[1] / "examples"
    for path in sorted(examples.glob("*.pcad.yaml")):
        copy = tmp_path / path.name
        shutil.copy2(path, copy)
        opened = Session.open(copy)
        opened.number_wires()
        jobs.append(opened)
    dol = Session.create("NEMA DOL")
    dol.insert_circuit(kind="dol", branch="breaker", control="local")
    jobs.append(dol)
    vfd = Session.create("NEMA VFD")
    vfd.insert_circuit(kind="vfd", drive="generic_1k5", branch="breaker", control="local")
    jobs.append(vfd)
    reversing = Session.create("Reversing")
    reversing.insert_circuit(kind="reversing", control="local")
    jobs.append(reversing)
    for hp in (50, 100):
        star = Session.create(f"Star-delta {hp}")
        built = star.insert_circuit(kind="star_delta", control="local", hp=hp, voltage=460)
        jobs.append(star)
        _star_delta_counts_distinct_columns(star, built["sheet"])
    soft = Session.create("Soft starter")
    soft.insert_circuit(kind="soft_starter", control="local")
    jobs.append(soft)
    plc = Session.create("PLC motor")
    plc.insert_circuit(kind="plc_motor", control="local")
    jobs.append(plc)
    for session in jobs:
        for sheet in session.project.sheets:
            _marks_clear(session, sheet.id)


def _star_delta_counts_distinct_columns(session: Session, sheet_id: str) -> None:
    """Overlaid star-delta routes are one drawn line, not 3 x or 9 x conductors."""

    from panelcad.render.wire_labels import _bundle_column, bundle_spec_plan

    sheet = session.sheet(sheet_id)
    routes = route_sheet_wires(sheet, session.catalog)
    specs = bundle_spec_plan(sheet, routes)
    multiplied = [spec for spec in specs.values() if " x " in spec]
    assert not any(spec.startswith("9 x ") for spec in multiplied), multiplied
    # Main's right-side 6-lead motor draws 107 as a short star jumper (about
    # 2 mm stubs), not as two long verticals, so there is no "2 x" column.
    # A real parallel bundle is still "3 x", never "9 x".
    stacked: dict[float, list[tuple[str, float, float]]] = {}
    for wire in sheet.wires:
        column = _bundle_column(routes.get(wire.id) or [])
        if column is None:
            continue
        stacked.setdefault(round(column[0], 1), []).append((specs.get(wire.id, ""), column[1], column[2]))
    for column_x, rows in stacked.items():
        for index, (spec, y0, y1) in enumerate(rows):
            if " x " not in spec:
                continue
            covered = [
                other
                for other_index, other in enumerate(rows)
                if other_index != index and min(y1, other[2]) - max(y0, other[1]) >= 6.0
            ]
            assert not covered, (column_x, spec, covered)
        # One physical stroke carries one gauge. Copies on the same column are blank.
        overlapping = []
        for index, (spec, y0, y1) in enumerate(rows):
            if not spec or " x " in spec:
                continue
            if any(
                min(y1, oy1) - max(y0, oy0) >= 6.0
                for other_index, (other, oy0, oy1) in enumerate(rows)
                if other_index != index and other and " x " not in other
            ):
                overlapping.append(spec)
        assert len(overlapping) <= 1, (column_x, rows)
    marks = place_wire_marks(sheet, session.catalog, routes)
    for wire in sheet.wires:
        if wire.number not in {"104", "105", "106"}:
            continue
        at = marks.get(wire.id)
        if not at:
            continue
        box = mark_box(at[0], at[1], wire.drawn_mark(), align=at[2], spec=specs.get(wire.id, ""))
        pts = routes[wire.id]
        gap = min(
            box_gap(box, (min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y)))
            for a, b in zip(pts, pts[1:])
        )
        if gap > CLEAR + 0.8:
            # Phase risers a couple of millimetres apart cannot each hold a
            # glyph inside CLEAR without covering the neighbour. The number
            # then sits on the open side, with that riser between it and its wire.
            own_xs = [
                (a.x + b.x) / 2.0
                for a, b in zip(pts, pts[1:])
                if abs(a.x - b.x) <= 0.6 and abs(a.y - b.y) > 1.0
            ]
            assert own_xs, (wire.number, at)
            center = (box[0] + box[2]) / 2.0
            own_x = min(own_xs, key=lambda value: abs(value - center))
            if box[2] < own_x:
                gap_lo, gap_hi = box[2], own_x
            elif box[0] > own_x:
                gap_lo, gap_hi = own_x, box[0]
            else:
                gap_lo, gap_hi = own_x, own_x
            blocked = False
            for other in sheet.wires:
                if other.id == wire.id:
                    continue
                for a, b in zip(routes.get(other.id) or [], (routes.get(other.id) or [])[1:]):
                    if abs(a.x - b.x) > 0.6:
                        continue
                    if min(max(a.y, b.y), box[3]) - max(min(a.y, b.y), box[1]) < 0.4:
                        continue
                    mid_x = (a.x + b.x) / 2.0
                    if gap_lo + 0.2 < mid_x < gap_hi - 0.2:
                        blocked = True
            assert blocked, (wire.number, at, round(gap, 2))
    from panelcad.render.titleblock import working_rect

    wx, wy, ww, wh = working_rect(sheet)
    for wire in sheet.wires:
        if wire.number != "107" or wire.id not in marks:
            continue
        at = marks[wire.id]
        box = mark_box(at[0], at[1], wire.drawn_mark(), align=at[2], spec=specs.get(wire.id, ""))
        assert (
            box[0] >= wx - 0.05
            and box[1] >= wy - 0.05
            and box[2] <= wx + ww + 0.05
            and box[3] <= wy + wh + 0.05
        ), (wire.id, box)
        leads = [
            (a.x + b.x) / 2.0
            for a, b in zip(routes[wire.id], routes[wire.id][1:])
            if abs(a.x - b.x) <= 0.4 and abs(a.y - b.y) >= 2.0
        ]
        if len(leads) >= 2:
            lo_x, hi_x = min(leads), max(leads)
            assert not (box[0] < lo_x + 0.3 and box[2] > hi_x - 0.3), (wire.id, box, leads)


def test_phase_conductors_number_and_label_left_to_right():
    """L1/L2/L3 and 101/102/103 each sit on their own conductor, left to right."""

    from panelcad.render.wire_labels import MARK_SIZE, SPEC_SIZE, bundle_spec_plan, spec_origin

    s = Session.create("Phase order")
    result = s.insert_circuit(kind="dol", hp=5, voltage=460)
    sheet = s.sheet(result["sheet"])
    routes = route_sheet_wires(sheet, s.catalog)
    marks = place_wire_marks(sheet, s.catalog, routes)
    specs = bundle_spec_plan(sheet, routes)
    assert SPEC_SIZE >= MARK_SIZE

    def column_x(wire) -> float:
        pts = routes[wire.id]
        xs = [a.x for a, b in zip(pts, pts[1:]) if abs(a.x - b.x) <= 0.6 and abs(a.y - b.y) > 1.0]
        assert xs, wire.id
        return sum(xs) / len(xs)

    load = [w for w in sheet.wires if w.frm.startswith("M1.T") and w.to.startswith("OL1.")]
    load.sort(key=column_x)
    assert [w.number for w in load] == ["101", "102", "103"]
    assert [column_x(w) for w in load] == sorted(column_x(w) for w in load)
    label_x = []
    for wire in load:
        at = marks[wire.id]
        cx = column_x(wire)
        assert at[2] == "end"
        assert cx - 12.0 <= at[0] <= cx - 1.5, (wire.number, at, cx)
        others = [column_x(other) for other in load if other is not wire]
        assert abs(at[0] - cx) < min(abs(at[0] - ox) for ox in others)
        box = mark_box(at[0], at[1], wire.drawn_mark(), align=at[2], spec=specs.get(wire.id, ""))
        assert box[2] <= cx - 1.0
        label_x.append(at[0])
    assert label_x == sorted(label_x)
    assert any(specs.get(wire.id, "").startswith("3 x ") for wire in load)
    host = next(wire for wire in load if specs.get(wire.id, "").startswith("3 x "))
    _sx, sy, _sa, ssize = spec_origin(
        marks[host.id][0],
        marks[host.id][1],
        host.drawn_mark(),
        specs[host.id],
        marks[host.id][2],
    )
    assert ssize >= MARK_SIZE
    assert abs(sy - marks[host.id][1]) < 0.05

    line = [w for w in sheet.wires if w.number in {"L1", "L2", "L3"}]
    line.sort(key=column_x)
    assert [w.number for w in line] == ["L1", "L2", "L3"]
    assert [marks[w.id][0] for w in line] == sorted(marks[w.id][0] for w in line)
    for wire in line:
        cx = column_x(wire)
        assert cx - 12.0 <= marks[wire.id][0] <= cx - 1.5
        others = [column_x(other) for other in line if other is not wire]
        assert abs(marks[wire.id][0] - cx) < min(abs(marks[wire.id][0] - ox) for ox in others)

    leads = [w for w in sheet.wires if w.frm.startswith("OL1.") and w.to.startswith("MTR1.")]
    assert len(leads) == 3
    for wire in leads:
        pts = routes[wire.id]
        assert len({round(p.x, 1) for p in pts}) == 1, [(p.x, p.y) for p in pts]
        for a, b in zip(pts, pts[1:]):
            assert abs(a.x - b.x) <= 0.6 or abs(a.y - b.y) <= 0.6
    _marks_clear(s, result["sheet"])


def test_device_tags_clear_the_wire_label_row():
    """Tags sit above and left of the symbol, one text height off the wire labels and dots."""

    from panelcad.core.ink import PIN_DOT_R, TAG_SIZE, text_box
    from panelcad.render.sheet import pin_world
    from panelcad.render.sheet_labels import (
        colliding_sheet_labels,
        device_tag_faults,
        place_device_tags,
        sheet_conductor_boxes,
    )
    from panelcad.render.titleblock import working_rect
    from panelcad.render.wire_labels import box_gap

    session = Session.create("Tag clearance")
    result = session.insert_circuit(kind="dol", hp=5, voltage=460)
    sheet = session.sheet(result["sheet"])
    # Main's circuit stack keeps the default origin y=22. The previous lock
    # was y=21.2. Mark x and the offset from the contactor and overload are
    # the same; y follows the symbols by 0.8 mm. The motor sits 1 mm lower
    # relative to the overload than it did before that stack change.
    assert [(comp.id, comp.x, comp.y) for comp in sheet.components] == [
        ("CB1", 40.0, 22.0),
        ("M1", 38.0, 81.0),
        ("OL1", 40.0, 155.0),
        ("MTR1", 40.0, 210.0),
    ]
    routes = route_sheet_wires(sheet, session.catalog)
    marks = place_wire_marks(sheet, session.catalog, routes)
    expected = {
        "L1": (37.0, 79.0),
        "L2": (55.2, 79.25),
        "L3": (65.2, 79.25),
        "101": (39.2, 153.25),
        "102": (55.2, 153.25),
        "103": (65.2, 153.25),
    }
    seen: dict[str, tuple[float, float, str]] = {}
    for wire in sheet.wires:
        at = marks.get(wire.id)
        if at and wire.number in expected and wire.number not in seen:
            seen[wire.number] = at
    assert set(seen) == set(expected)
    for number, (x, y) in expected.items():
        at = seen[number]
        assert abs(at[0] - x) < 0.05 and abs(at[1] - y) < 0.05, (number, at)
        assert at[2] == "end"

    layouts = place_device_tags(sheet, session.catalog, ladder=False, project=session.project)
    wx, wy, ww, wh = working_rect(sheet)
    conductors = [box for _name, box in sheet_conductor_boxes(sheet, session.catalog)]
    dots = []
    for comp in sheet.components:
        device = session.catalog.get(comp.library_id)
        for pin in device.pins:
            px, py = pin_world(comp, pin)
            dots.append((px - PIN_DOT_R, py - PIN_DOT_R, px + PIN_DOT_R, py + PIN_DOT_R))
    from panelcad.render.wire_labels import bundle_spec_plan, drawn_mark_box

    specs = bundle_spec_plan(sheet, routes)
    wire_boxes = []
    for wire in sheet.wires:
        at = marks.get(wire.id)
        mark = wire.drawn_mark()
        if not at or not mark:
            continue
        wire_boxes.append(drawn_mark_box(at[0], at[1], mark, at[2], specs.get(wire.id, "")))
    for comp in sheet.components:
        layout = layouts[comp.id]
        assert layout[2] == "start", comp.id
        box = text_box(layout[0], layout[1], comp.id, layout[3], layout[2])
        assert box is not None
        assert box[0] <= comp.x - 0.15, (comp.id, box, comp.x)
        assert box[3] <= comp.y - 0.2, (comp.id, box, comp.y)
        assert box[0] >= wx - 0.05 and box[1] >= wy - 0.05
        assert box[2] <= wx + ww + 0.05 and box[3] <= wy + wh + 0.05
        for other in wire_boxes + conductors + dots:
            assert box_gap(box, other) >= TAG_SIZE - 1e-6, (comp.id, box, other, box_gap(box, other))
    faults = device_tag_faults(session.project, session.catalog, sheet, ladder=False, sheet_no=1)
    close = [hit for hit in faults if "closer than one text height" in hit or "touches" in hit]
    assert close == [], close
    hits = colliding_sheet_labels(session.project, session.catalog, sheet, ladder=False, sheet_no=1)
    crowded = [
        hit
        for hit in hits
        if hit.split(": ", 1)[-1].startswith("tag:") and "overlaps body:" in hit
    ]
    assert crowded == [], crowded
    _marks_clear(session, result["sheet"])


def test_jogged_breaker_labels_follow_conductor_order():
    """100 A MCCB into a 32 A contactor must not print L3 before L2, or 103 before 102."""

    for label, kwargs in (
        ("50 hp", {"kind": "dol", "hp": 50, "voltage": 460}),
        ("37 kW", {"kind": "dol", "kw": 37}),
    ):
        session = Session.create(label)
        result = session.insert_circuit(**kwargs)
        sheet = session.sheet(result["sheet"])
        from panelcad.render.wire_labels import bundle_spec_plan

        routes = route_sheet_wires(sheet, session.catalog)
        marks = place_wire_marks(sheet, session.catalog, routes)
        specs = bundle_spec_plan(sheet, routes)

        def column_x(wire) -> float:
            pts = routes[wire.id]
            xs = [
                (a.x + b.x) / 2.0
                for a, b in zip(pts, pts[1:])
                if abs(a.x - b.x) <= 0.6 and abs(a.y - b.y) > 1.0
            ]
            assert xs, (label, wire.number)
            return sum(xs) / len(xs)

        def label_center(wire) -> float:
            at = marks[wire.id]
            box = mark_box(at[0], at[1], wire.drawn_mark(), align=at[2])
            return (box[0] + box[2]) / 2.0

        for numbers in (("L1", "L2", "L3"), ("101", "102", "103")):
            wires = [wire for wire in sheet.wires if wire.number in numbers and wire.id in marks]
            assert sorted(wire.number for wire in wires) == sorted(numbers), (
                label,
                [wire.number for wire in wires],
            )
            by_conductor = sorted(wires, key=column_x)
            by_label = sorted(wires, key=label_center)
            assert [wire.number for wire in by_label] == [wire.number for wire in by_conductor], label
            hosts = [wire for wire in wires if " x " in specs.get(wire.id, "")]
            assert len(hosts) == 1, (label, [(wire.number, specs.get(wire.id, "")) for wire in wires])
            assert specs[hosts[0].id] == f"3 x {wires[0].gauge}", (label, specs[hosts[0].id])
            for wire in wires:
                if wire is not hosts[0]:
                    assert specs.get(wire.id, "") == "", (label, wire.number, specs.get(wire.id, ""))
            for wire in wires:
                own = column_x(wire)
                center = label_center(wire)
                lo, hi = sorted((own, center))
                for other in wires:
                    if other is wire:
                        continue
                    other_x = column_x(other)
                    assert not (lo + 0.4 < other_x < hi - 0.4), (
                        label,
                        wire.number,
                        other.number,
                        center,
                        own,
                        other_x,
                    )
        _marks_clear(session, result["sheet"])


def _assert_mark_misses_conductor_strokes(session: Session, sheet_id: str, numbers: set[str]) -> None:
    """The drawn number, including its gauge line, contains no conductor stroke.

    Own routes, same-numbered companions, symbol leads into the pin, and the
    phase ink over those leads all count. A centerline on the box edge is a hit.
    """

    from panelcad.core.ladder import is_ladder_sheet
    from panelcad.render.phase_ink import line_side_runs, pole_runs
    from panelcad.render.wire_labels import _drawn_lead_segments, _segment_meets_box, bundle_spec_plan

    sheet = session.sheet(sheet_id)
    ladder = is_ladder_sheet(sheet, session.project)
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    marks = place_wire_marks(sheet, session.catalog, routes, ladder=ladder, project=session.project)
    specs = bundle_spec_plan(sheet, routes, ladder=ladder)
    strokes: list[tuple[Point, Point]] = []
    for pts in routes.values():
        strokes.extend(zip(pts, pts[1:]))
    strokes.extend(_drawn_lead_segments(sheet, session.catalog))
    for run in pole_runs(session.project, sheet, session.catalog) + line_side_runs(
        session.project, sheet, session.catalog
    ):
        points = [Point(x=x, y=y) for x, y in run.points]
        strokes.extend(zip(points, points[1:]))
    seen = False
    for wire in sheet.wires:
        if wire.drawn_mark(ladder=ladder) not in numbers:
            continue
        at = marks.get(wire.id)
        if at is None:
            continue
        seen = True
        box = mark_box(
            at[0],
            at[1],
            wire.drawn_mark(ladder=ladder),
            align=at[2],
            spec=specs.get(wire.id, ""),
        )
        for a, b in strokes:
            assert not _segment_meets_box(a, b, box), (
                wire.drawn_mark(ladder=ladder),
                wire.id,
                at,
                box,
                (round(a.x, 2), round(a.y, 2), round(b.x, 2), round(b.y, 2)),
            )
    assert seen, numbers


def test_star_delta_230v_mark_misses_the_paired_jumper():
    """100 hp at 230 V must not draw 107 across the same-numbered star jumper."""

    session = Session.create("Star-delta 100 hp 230")
    built = session.insert_circuit(kind="star_delta", control="local", hp=100, voltage=230)
    _assert_mark_misses_conductor_strokes(session, built["sheet"], {"107"})


def test_load_mark_misses_the_phase_lead():
    """102 stays off the drawn stem into the contactor pin, gauge line included."""

    iec_rev = Session.create("IEC reversing")
    iec_rev.set_title_block(standard="IEC")
    rev = iec_rev.insert_circuit(kind="reversing", control="local", pilot_lights=True)
    _assert_mark_misses_conductor_strokes(iec_rev, rev["sheet"], {"102"})

    iec_ss = Session.create("IEC soft starter")
    iec_ss.set_title_block(standard="IEC")
    started = iec_ss.insert_circuit(kind="soft_starter", control="local", bypass=True)
    _assert_mark_misses_conductor_strokes(iec_ss, started["sheet"], {"102"})

    nema = Session.create("NEMA reversing")
    nema_rev = nema.insert_circuit(kind="reversing", control="local")
    _assert_mark_misses_conductor_strokes(nema, nema_rev["sheet"], {"102"})


def test_shared_number_and_pin_stem_pay_the_cross_cost():
    """A companion wire and a drawn pin lead are conductors even when the route is not."""

    from panelcad.render.wire_labels import _CROSS_CONDUCTOR, _score

    parts = {
        "own": ([(Point(x=0.0, y=0.0), Point(x=10.0, y=0.0))], []),
        "pair": (
            [
                (Point(x=10.0, y=0.0), Point(x=22.0, y=0.0)),
                (Point(x=22.0, y=0.0), Point(x=22.0, y=8.0)),
            ],
            [],
        ),
    }
    on_pair = _score(
        (14.0, -1.5, 20.0, 1.5),
        [],
        "own",
        parts,
        [],
        [],
        10.0,
        False,
        "start",
        ignore_ids={"pair"},
    )
    clear = _score(
        (2.0, -6.0, 8.0, -3.0),
        [],
        "own",
        parts,
        [],
        [],
        10.0,
        False,
        "start",
        ignore_ids={"pair"},
    )
    assert on_pair - clear >= _CROSS_CONDUCTOR
    stem = [(Point(x=10.0, y=0.0), Point(x=10.0, y=20.0))]
    on_stem = _score(
        (6.0, 8.0, 10.0, 14.0),
        [],
        "own",
        {"own": ([], [])},
        [],
        [],
        8.0,
        False,
        "end",
        stem_segs=stem,
    )
    off_stem = _score(
        (2.0, 8.0, 6.0, 14.0),
        [],
        "own",
        {"own": ([], [])},
        [],
        [],
        8.0,
        False,
        "end",
        stem_segs=stem,
    )
    assert on_stem - off_stem >= _CROSS_CONDUCTOR


def test_each_covered_conductor_pays_the_cross_cost():
    """A second different conductor is not a cheap extra segment of the first."""

    from panelcad.render.wire_labels import _CROSS_CONDUCTOR, _score

    def vertical(x: float):
        return [(Point(x=x, y=0.0), Point(x=x, y=40.0))]

    parts = {
        "own": ([], []),
        "b": (vertical(20.0), []),
        "c": (vertical(30.0), []),
        "same": (
            [
                (Point(x=20.0, y=0.0), Point(x=20.0, y=20.0)),
                (Point(x=20.0, y=20.0), Point(x=20.0, y=40.0)),
            ],
            [],
        ),
    }
    both = _score((19.0, 10.0, 31.0, 16.0), [], "own", parts, [], [], 20.0, True, "start")
    one = _score((19.0, 10.0, 21.2, 16.0), [], "own", parts, [], [], 20.0, True, "start")
    repeat = _score((19.0, 15.0, 21.2, 25.0), [], "own", {"own": ([], []), "same": parts["same"]}, [], [], 20.0, True, "start")
    assert both >= 2 * _CROSS_CONDUCTOR
    assert _CROSS_CONDUCTOR <= one < 2 * _CROSS_CONDUCTOR
    assert _CROSS_CONDUCTOR <= repeat < 2 * _CROSS_CONDUCTOR


def test_wire_marks_are_computed_once_per_sheet_reload():
    """Canvas reload, tags, and cross-references share one placement."""

    from panelcad.render.sheet_labels import place_device_tags
    from panelcad.render.wire_labels import (
        reset_wire_mark_stats,
        share_wire_marks,
        wire_mark_stats,
    )
    from panelcad.render.xref import place_xref_labels

    session = Session.create("Mark cache")
    session.place("iec.contactors.coil", ref="K1", x=40, y=40)
    session.place("iec.operators.pb_no", ref="S1", x=120, y=40)
    session.wire("K1.A1", "S1.13", net="120VAC")
    sheet = session.sheet()
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    solo = place_wire_marks(sheet, session.catalog, routes, ladder=False, project=session.project)
    reset_wire_mark_stats()
    place_wire_marks(sheet, session.catalog, routes, ladder=False, project=session.project)
    place_wire_marks(sheet, session.catalog, routes, ladder=False, project=session.project)
    assert wire_mark_stats() == (2, 2)
    reset_wire_mark_stats()
    with share_wire_marks():
        place_device_tags(sheet, session.catalog, ladder=False, project=session.project)
        place_xref_labels(session.project, session.catalog, sheet, ladder=False)
        again = place_wire_marks(
            sheet, session.catalog, routes, ladder=False, project=session.project
        )
        other_ladder = place_wire_marks(
            sheet, session.catalog, routes, ladder=True, project=session.project
        )
    calls, computes = wire_mark_stats()
    assert again == solo
    assert calls >= 3
    assert computes == 2
    assert other_ladder == place_wire_marks(
        sheet, session.catalog, routes, ladder=True, project=session.project
    )


def test_box_route_distance_uses_a_spatial_index(monkeypatch):
    """Nearest-foreign distance matches a full scan and does not visit every wire."""

    from panelcad.render import wire_labels
    from panelcad.render.wire_labels import _CROSS_CONDUCTOR, _RouteIndex, _score

    parts: dict = {"own": ([(Point(x=0.0, y=0.0), Point(x=0.0, y=80.0))], [])}
    for i in range(1, 41):
        x = i * 12.0
        parts[f"w{i}"] = (
            [(Point(x=x, y=0.0), Point(x=x, y=80.0))],
            [(x, 0.0), (x, 80.0)],
        )
    parts["long"] = (
        [(Point(x=-180.0, y=50.0), Point(x=700.0, y=50.0))],
        [(-180.0, 50.0), (700.0, 50.0)],
    )
    index = _RouteIndex(parts)

    def brute_box(box, exclude):
        best = 1.0e9
        for wid, (segs, _ends) in parts.items():
            if wid in exclude:
                continue
            for a, b in segs:
                best = min(best, wire_labels.box_route_distance(box, [a, b]))
        return best

    def brute_point(x, y, exclude):
        best = 1.0e9
        for wid, (segs, _ends) in parts.items():
            if wid in exclude:
                continue
            for a, b in segs:
                best = min(best, wire_labels._point_segment_distance(x, y, a, b))
        return best

    queries = (
        ((10.0, 20.0, 14.0, 26.0), {"own"}),
        ((-40.0, -40.0, -30.0, -20.0), set()),
        ((690.0, 48.0, 696.0, 54.0), {"own"}),
        ((11.5, 30.0, 12.5, 36.0), {"own", "w1"}),
    )
    for box, exclude in queries:
        assert index.nearest_box(box, exclude) == brute_box(box, exclude)
        cx = (box[0] + box[2]) * 0.5
        cy = (box[1] + box[3]) * 0.5
        assert index.nearest_point(cx, cy, exclude) == brute_point(cx, cy, exclude)

    calls = {"n": 0}
    real = wire_labels.box_route_distance

    def counting(box, pts):
        calls["n"] += 1
        return real(box, pts)

    monkeypatch.setattr(wire_labels, "box_route_distance", counting)
    pts = [Point(x=0.0, y=0.0), Point(x=0.0, y=80.0)]
    on_near = _score(
        (11.0, 20.0, 13.0, 26.0),
        pts,
        "own",
        parts,
        [],
        [],
        20.0,
        True,
        "start",
        route_index=index,
    )
    measured = calls["n"]
    assert measured < 20, measured
    clear = _score(
        (-30.0, 20.0, -20.0, 26.0),
        pts,
        "own",
        parts,
        [],
        [],
        20.0,
        True,
        "start",
    )
    assert on_near >= _CROSS_CONDUCTOR
    assert clear < _CROSS_CONDUCTOR
    far_only = {
        "own": parts["own"],
        "far": ([(Point(x=400.0, y=0.0), Point(x=400.0, y=40.0))], []),
    }
    hit = _score((399.0, 10.0, 401.0, 16.0), pts, "own", far_only, [], [], 10.0, True, "start")
    assert hit >= _CROSS_CONDUCTOR


def test_wire_overlap_index_matches_a_full_scan():
    """Segment and dot filters keep the same cover cost as reading every wire."""

    import random

    from panelcad.render.wire_labels import (
        DOT_R,
        WIRE_PAD,
        _RouteIndex,
        _axis_box_distance,
        _foreign_cover_cost,
        _other_conductor_between,
        box_route_distance,
    )

    rng = random.Random(0)
    parts: dict = {
        "own": ([(Point(x=0.0, y=0.0), Point(x=0.0, y=80.0))], [(0.0, 0.0), (0.0, 80.0)]),
        "long": (
            [
                (Point(x=-400.0, y=50.0), Point(x=700.0, y=50.0)),
                (Point(x=30.0, y=50.0), Point(x=30.0, y=90.0)),
            ],
            [(-400.0, 50.0), (30.0, 90.0)],
        ),
        "dot": ([], [(12.0, 22.0)]),
        "far": (
            [(Point(x=500.0, y=-40.0), Point(x=500.0, y=20.0))],
            [(500.0, -40.0), (500.0, 20.0)],
        ),
    }
    for i in range(25):
        x = rng.uniform(-20.0, 220.0)
        y = rng.uniform(-20.0, 120.0)
        span = rng.choice((0.0, rng.uniform(-30.0, 30.0)))
        segs = [(Point(x=x, y=y), Point(x=x + span, y=y + rng.uniform(4.0, 40.0)))]
        if rng.random() < 0.3:
            segs = [(Point(x=x, y=y), Point(x=x + rng.uniform(8.0, 60.0), y=y))]
        ends = [(segs[0][0].x, segs[0][0].y), (segs[-1][1].x, segs[-1][1].y)]
        parts[f"r{i}"] = (segs, ends)
    index = _RouteIndex(parts)
    spur = (28.5, 70.0, 31.5, 76.0)
    near = index.near_wire_ids(spur, seg_pad=WIRE_PAD, dot_r=DOT_R)
    assert "long" in near
    assert "far" not in near
    assert _foreign_cover_cost(spur, "own", parts, near) == _foreign_cover_cost(
        spur, "own", parts, None
    )
    assert _foreign_cover_cost(spur, "own", parts, near) >= 20000.0
    for _ in range(60):
        x0 = rng.uniform(-30.0, 240.0)
        y0 = rng.uniform(-30.0, 140.0)
        box = (x0, y0, x0 + rng.uniform(1.5, 18.0), y0 + rng.uniform(1.5, 8.0))
        got = index.near_wire_ids(box, seg_pad=WIRE_PAD, dot_r=DOT_R)
        assert _foreign_cover_cost(box, "own", parts, got) == _foreign_cover_cost(
            box, "own", parts, None
        )
        assert _foreign_cover_cost(
            box, "own", parts, None, strokes=index.stroke_hits(box)
        ) == _foreign_cover_cost(box, "own", parts, None)
        assert index.conductor_between(box, "own") == _other_conductor_between(box, "own", parts)
    for _ in range(40):
        x0 = rng.uniform(0.0, 80.0)
        y0 = rng.uniform(0.0, 80.0)
        box = (x0, y0, x0 + rng.uniform(2.0, 12.0), y0 + rng.uniform(2.0, 8.0))
        ax, ay = rng.uniform(-10.0, 100.0), rng.uniform(-10.0, 100.0)
        horizontal = (Point(x=ax, y=ay), Point(x=ax + rng.uniform(0.0, 40.0), y=ay))
        vertical = (Point(x=ax, y=ay), Point(x=ax, y=ay + rng.uniform(0.0, 40.0)))
        for a, b in (horizontal, vertical):
            assert _axis_box_distance(box, a, b) == box_route_distance(box, [a, b])
        exclude = {"own"}
        assert index.nearest_box(box, exclude) == min(
            (
                box_route_distance(box, [a, b])
                for wid, (segs, _ends) in parts.items()
                if wid not in exclude
                for a, b in segs
            ),
            default=1.0e9,
        )
        cx = (box[0] + box[2]) * 0.5
        cy = (box[1] + box[3]) * 0.5

        def _point_best(px: float = cx, py: float = cy) -> float:
            from panelcad.render.wire_labels import _point_segment_distance

            best = 1.0e9
            for wid, (segs, _ends) in parts.items():
                if wid in exclude:
                    continue
                for a, b in segs:
                    best = min(best, _point_segment_distance(px, py, a, b))
            return best

        assert index.nearest_point(cx, cy, exclude) == _point_best()


def test_short_lead_has_no_gauge_note():
    """A lead shorter than the gauge note keeps the number and drops the note."""

    from panelcad.render.wire_labels import bundle_spec_plan

    session = Session.create("Short gauge")
    session.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    session.place("iec.operators.pilot_light", ref="H1", x=80, y=40)
    wire = session.wire("S1.13", "H1.X1")
    wire.gauge = "14 AWG"
    sheet = session.sheet()
    short = {wire.id: [Point(x=0, y=0), Point(x=0, y=4)]}
    long = {wire.id: [Point(x=0, y=0), Point(x=0, y=40)]}
    assert bundle_spec_plan(sheet, short)[wire.id] == ""
    assert bundle_spec_plan(sheet, long)[wire.id] == "14 AWG"


def test_ethernet_hops_do_not_share_a_number():
    s = build_dol()
    eth = list(s.sheet("ethernet").wires)
    assert {(w.frm, w.to) for w in eth} == {("A2.ETH", "P1.ETH"), ("A1.ETH", "P1.ETH")}
    assert eth[0].number and eth[1].number
    assert eth[0].number != eth[1].number
    serial = list(s.sheet("serial").wires)
    assert serial
    assert serial[0].number not in {eth[0].number, eth[1].number}
