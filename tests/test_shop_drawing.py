"""Shop checks for the five circuit sheets: hops, dots, leads, and clear labels."""

from __future__ import annotations

from math import hypot

import pytest

from panelcad import Session
from panelcad.core.geometry import rotate_point
from panelcad.core.ink import device_ref_layout, pin_label_anchor, stacked_power_tuck, text_box
from panelcad.core.ladder import sheet_drawing_style
from panelcad.core.pins import pin_caption
from panelcad.core.route import GRID
from panelcad.core.phase_color import wire_phase_pens
from panelcad.core.xref import iec_mirror_rows, xref_families
from panelcad.core.zones import column_from_header, header_columns, zone_grid
from panelcad.render.crossings import hop_routes
from panelcad.render.junctions import junction_points
from panelcad.render.overlap import ink_polylines
from panelcad.render.phase_ink import line_side_runs
from panelcad.render.sheet import pin_side_world, pin_world
from panelcad.render.titleblock import working_rect
from panelcad.render.wire_labels import (
    box_route_distance,
    bundle_spec_plan,
    boxes_overlap,
    device_outline_boxes,
    mark_box,
    place_wire_marks,
    routes_near,
    routes_share_collinear_run,
    routes_share_line,
)
from panelcad.render.wiring import route_sheet_wires, wire_ends
from panelcad.render.xref import annotation_box, place_xref_labels


def _sheets():
    iec = Session.create("star delta shop")
    iec.set_title_block(standard="IEC")
    star = iec.insert_circuit(kind="star_delta", control="local", timer_seconds=8)
    yield "iec-star-delta-power", iec, star["sheet"]
    yield "iec-star-delta-control", iec, star["control_sheet"]
    rev = Session.create("iec reversing shop")
    rev.set_title_block(standard="IEC")
    control = rev.insert_circuit(kind="reversing", control="local", pilot_lights=True)
    yield "iec-reversing-control", rev, control["control_sheet"]
    nema480 = Session.create("nema 480 shop")
    power480 = nema480.insert_circuit(kind="reversing", control="local", voltage=480)
    yield "nema-reversing-power-480", nema480, power480["sheet"]
    nema240 = Session.create("nema 240 shop")
    power240 = nema240.insert_circuit(kind="reversing", control="local", voltage=240)
    yield "nema-reversing-power-240", nema240, power240["sheet"]


def _xy(point) -> tuple[float, float]:
    if hasattr(point, "x"):
        return float(point.x), float(point.y)
    return float(point[0]), float(point[1])


def _world(comp, lx: float, ly: float) -> tuple[float, float]:
    scale = comp.visual_scale()
    rx, ry = rotate_point(lx * scale, ly * scale, int(comp.rotation or 0))
    if comp.mirror:
        rx = -rx
    return comp.x + rx, comp.y + ry


def _overlap(a, b, box: tuple[float, float, float, float]) -> float:
    """Length of segment ab that lies inside an axis-aligned box."""

    x0, y0, x1, y1 = box
    ax, ay = _xy(a)
    bx, by = _xy(b)
    dx, dy = bx - ax, by - ay
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
        if abs(p) < 1e-9:
            if q < 0:
                return 0.0
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return 0.0
            t0 = max(t0, r)
        else:
            if r < t0:
                return 0.0
            t1 = min(t1, r)
    if t1 < t0:
        return 0.0
    return hypot(dx, dy) * (t1 - t0)


def _on_segment(x: float, y: float, a, b) -> bool:
    ax, ay = _xy(a)
    bx, by = _xy(b)
    if hypot(bx - ax, by - ay) < 0.2:
        return hypot(x - ax, y - ay) <= 0.35
    if abs(ax - bx) <= 0.35:
        if abs(x - (ax + bx) * 0.5) > 0.35:
            return False
        return min(ay, by) - 0.05 <= y <= max(ay, by) + 0.05
    if abs(ay - by) <= 0.35:
        if abs(y - (ay + by) * 0.5) > 0.35:
            return False
        return min(ax, bx) - 0.05 <= x <= max(ax, bx) + 0.05
    return False


def _interior(x: float, y: float, a, b) -> bool:
    ax, ay = _xy(a)
    bx, by = _xy(b)
    if min(hypot(x - ax, y - ay), hypot(x - bx, y - by)) <= 0.5:
        return False
    return _on_segment(x, y, a, b)


def _cross(a, b, c, d):
    ax, ay = _xy(a)
    bx, by = _xy(b)
    cx, cy = _xy(c)
    dx, dy = _xy(d)
    denom = (bx - ax) * (dy - cy) - (by - ay) * (dx - cx)
    if abs(denom) < 1e-6:
        return None
    t = ((cx - ax) * (dy - cy) - (cy - ay) * (dx - cx)) / denom
    u = ((cx - ax) * (by - ay) - (cy - ay) * (bx - ax)) / denom
    if 0.012 < t < 0.988 and 0.012 < u < 0.988:
        return t, u, ax + t * (bx - ax), ay + t * (by - ay)
    return None


def _segments(routes):
    found = []
    for wire_id, pts in routes.items():
        for a, b in zip(pts, pts[1:]):
            if hypot(b.x - a.x, b.y - a.y) >= 0.4:
                found.append((wire_id, a, b))
    return found


def _sheet_pins(session: Session, sheet) -> list[tuple[float, float, str]]:
    pins = []
    for comp in sheet.components:
        try:
            device = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        hidden = {str(pin_id) for pin_id in (comp.properties.get("hidden_pins") or [])}
        for pin in device.pins:
            if pin.id in hidden:
                continue
            px, py = pin_world(comp, pin)
            pins.append((px, py, comp.id))
    return pins


def _tees(routes, pins=None) -> list[tuple[float, float]]:
    """Perpendicular branch, including a T that lands on a device pin.

    A bus that only touches another wire at an endpoint used to be ignored,
    so every control-sheet seal and tap passed with no dot. A 2-terminal
    symbol hanging off a straight bus is a tee at that pin too.
    """

    pins = list(pins or [])
    segs = _segments(routes)
    tees: list[tuple[float, float]] = []

    def _near_pin(x: float, y: float):
        best = None
        dist = 0.55
        for px, py, cid in pins:
            gap = hypot(x - px, y - py)
            if gap <= dist:
                dist = gap
                best = (px, py, cid)
        return best

    for wid, pts in routes.items():
        xy = [_xy(point) for point in pts]
        for index, (x, y) in enumerate(xy):
            host = None
            landed = _near_pin(x, y)
            for oid, a, b in segs:
                if oid == wid:
                    continue
                if _interior(x, y, a, b) or (landed is not None and _on_segment(x, y, a, b)):
                    host = (a, b)
                    break
            if host is None:
                continue
            ax, ay = _xy(host[0])
            bx, by = _xy(host[1])
            host_horiz = abs(ay - by) <= 0.35
            perpendicular = False
            for other in (index - 1, index + 1):
                if other < 0 or other >= len(xy):
                    continue
                ox, oy = xy[other]
                if hypot(ox - x, oy - y) < 0.4:
                    continue
                branch_horiz = abs(oy - y) <= 0.35 and abs(ox - x) > 0.4
                branch_vert = abs(ox - x) <= 0.35 and abs(oy - y) > 0.4
                if host_horiz and branch_vert or (not host_horiz and branch_horiz):
                    perpendicular = True
                    break
            if perpendicular:
                tees.append((x, y))
                continue
            if landed is None:
                continue
            owned = [item for item in pins if item[2] == landed[2]]
            if len(owned) != 2:
                continue
            other_pin = owned[0] if hypot(owned[0][0] - x, owned[0][1] - y) > 0.55 else owned[1]
            if hypot(other_pin[0] - x, other_pin[1] - y) <= 0.55:
                continue
            wired = False
            for _oid, a, b in segs:
                if hypot(other_pin[0] - _xy(a)[0], other_pin[1] - _xy(a)[1]) <= 0.55 or hypot(
                    other_pin[0] - _xy(b)[0], other_pin[1] - _xy(b)[1]
                ) <= 0.55:
                    wired = True
                    break
            if not wired:
                continue
            pair_horiz = abs(other_pin[1] - y) <= 0.35
            # The bus is one axis and the symbol is the other.
            if host_horiz != pair_horiz:
                tees.append((x, y))
    return tees


def _boxes_hit(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    """True when two boxes share area, not merely an edge."""

    return a[0] < b[2] - 0.05 and b[0] < a[2] - 0.05 and a[1] < b[3] - 0.05 and b[1] < a[3] - 0.05


def _seg_dist(x: float, y: float, a, b) -> float:
    ax, ay = _xy(a)
    bx, by = _xy(b)
    dx, dy = bx - ax, by - ay
    span = dx * dx + dy * dy
    if span < 1e-9:
        return hypot(x - ax, y - ay)
    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / span))
    return hypot(x - (ax + t * dx), y - (ay + t * dy))


def _route_dist(x: float, y: float, pts) -> float:
    best = 1e9
    for a, b in zip(pts, pts[1:]):
        best = min(best, _seg_dist(x, y, a, b))
    return best


def _near_dot(x: float, y: float, dots: list[tuple[float, float]]) -> bool:
    return any(hypot(x - dx, y - dy) <= 0.7 for dx, dy in dots)


def _hop_drawn(wire_id: str, x: float, y: float, hopped: dict) -> bool:
    pts = hopped.get(wire_id) or []
    for px, py in (_xy(point) for point in pts):
        if abs(px - x) <= 1.3 and 0.25 <= (y - py) <= 1.4:
            return True
    return False


def _bodies_and_labels(session: Session, sheet, routes):
    style = sheet_drawing_style(sheet, session.project)
    ladder = style == "ladder"
    column = style == "iec"
    elementary = ladder or column
    iec_numbers = column or str(getattr(sheet, "standard", "") or "") == "IEC"
    marks = place_wire_marks(sheet, session.catalog, routes, ladder=elementary, project=session.project)
    bodies: list[tuple[str, tuple[float, float, float, float]]] = []
    labels: list[tuple[str, str, tuple[float, float, float, float]]] = []
    pins: list[tuple[str, str, float, float]] = []
    power_pins: list[tuple[float, float]] = []
    if not elementary:
        for comp in sheet.components:
            try:
                device = session.catalog.get(comp.library_id)
            except KeyError:
                continue
            for pin in device.pins:
                power_pins.append(pin_world(comp, pin))
    for comp in sheet.components:
        try:
            device = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        scale = comp.visual_scale()
        for prim in device.primitives:
            if prim.type == "rect":
                corners = [
                    _world(comp, prim.x, prim.y),
                    _world(comp, prim.x + prim.w, prim.y),
                    _world(comp, prim.x, prim.y + prim.h),
                    _world(comp, prim.x + prim.w, prim.y + prim.h),
                ]
                xs = [point[0] for point in corners]
                ys = [point[1] for point in corners]
                bodies.append((comp.id, (min(xs), min(ys), max(xs), max(ys))))
            elif prim.type == "circle":
                cx, cy = _world(comp, prim.x, prim.y)
                radius = prim.r * scale
                bodies.append((comp.id, (cx - radius, cy - radius, cx + radius, cy + radius)))
            elif prim.type == "text" and str(prim.text).strip():
                wx, wy = _world(comp, prim.x, prim.y)
                box = text_box(wx, wy, str(prim.text), max(1.2, prim.size * scale), "middle")
                if box is not None:
                    labels.append((comp.id, str(prim.text), box))
        layout = device_ref_layout(device, comp, ref_align="middle" if elementary else "start")
        if layout is not None:
            tx, ty, align, size = layout
            box = text_box(tx, ty, comp.id, size, align)
            if box is not None:
                labels.append((comp.id, comp.id, box))
        drawn = {str(prim.text).strip() for prim in device.primitives if prim.type == "text" and prim.text}
        hidden = {str(pin_id) for pin_id in (comp.properties.get("hidden_pins") or [])}
        selector = "selector" in (device.category or "").lower()
        for pin in device.pins:
            if pin.id in hidden:
                continue
            wx, wy = pin_world(comp, pin)
            pins.append((comp.id, pin.id, wx, wy))
            cap = pin_caption(pin, comp)
            override = bool(cap) and cap != pin.id
            show_id = iec_numbers and cap == pin.id and pin.id not in drawn
            if column and selector and cap in {"H", "A"}:
                continue
            if ladder and not override:
                continue
            if not cap or (cap == pin.id and not show_id):
                continue
            side = pin_side_world(comp, pin)
            tuck = (not elementary) and stacked_power_tuck(side, wx, wy, power_pins)
            tx, ty, align = pin_label_anchor(
                wx, wy, side, ladder=ladder, column=column, caption=cap, tuck=tuck
            )
            size = 3.2 if ladder else 1.9
            box = text_box(tx, ty, cap, size, align)
            if box is not None:
                labels.append((f"{comp.id}.{pin.id}", cap, box))
    drawn_specs = bundle_spec_plan(sheet, routes, ladder=elementary)
    for wire in sheet.wires:
        at = marks.get(wire.id)
        mark = wire.drawn_mark(ladder=elementary)
        if at and mark:
            labels.append(
                (
                    wire.id,
                    mark,
                    mark_box(at[0], at[1], mark, align=at[2], spec=drawn_specs.get(wire.id, "")),
                )
            )
    placed = place_xref_labels(session.project, session.catalog, sheet, ladder=elementary)
    xrefs = [(ann.owner, annotation_box(ann, x, y, align)) for ann, x, y, align in placed]
    return bodies, labels, pins, xrefs


def _same_net_pin(number: str, px: float, py: float, sheet, routes) -> bool:
    token = str(number or "")
    for wire in sheet.wires:
        if str(wire.number or "") != token:
            continue
        pts = routes.get(wire.id) or []
        if not pts:
            continue
        for point in (pts[0], pts[-1]):
            if hypot(point.x - px, point.y - py) <= 0.8:
                return True
    return False


@pytest.mark.parametrize(
    "name,session,sheet_id",
    [pytest.param(name, session, sheet_id, id=name) for name, session, sheet_id in _sheets()],
)
def test_sheet_is_shop_ready(name: str, session: Session, sheet_id: str):
    """Tees are dotted, crossings hop, leads land, and ink stays off the text."""

    sheet = session.sheet(sheet_id)
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    hopped = hop_routes(routes)
    pins = _sheet_pins(session, sheet)
    dots = junction_points(routes, pins)
    segs = []
    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        for a, b in zip(pts, pts[1:]):
            if hypot(b.x - a.x, b.y - a.y) >= 0.4:
                segs.append((wire, a, b))

    for x, y in _tees(routes, pins):
        assert _near_dot(x, y, dots), (name, "tee has no dot", round(x, 1), round(y, 1))

    for index, (wa, a1, a2) in enumerate(segs):
        for wb, b1, b2 in segs[index + 1 :]:
            if wa.id == wb.id:
                continue
            hit = _cross(a1, a2, b1, b2)
            if hit is None:
                continue
            _t, _u, x, y = hit
            d1 = min(hypot(x - a1.x, y - a1.y), hypot(x - a2.x, y - a2.y))
            d2 = min(hypot(x - b1.x, y - b1.y), hypot(x - b2.x, y - b2.y))
            assert d1 >= 2.0 and d2 >= 2.0, (
                name,
                "crossing is not hopped",
                wa.number,
                wb.number,
                round(x, 1),
                round(y, 1),
                round(min(d1, d2), 2),
            )
            horizontal = wa.id if abs(a1.y - a2.y) <= 0.35 else wb.id
            assert _hop_drawn(horizontal, x, y, hopped), (
                name,
                "hop is not drawn",
                wa.number,
                wb.number,
                round(x, 1),
                round(y, 1),
            )

    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        assert len(pts) >= 2, (name, wire.id)
        ax, ay, _aside, bx, by, _bside = wire_ends(sheet, session.catalog, wire)
        assert hypot(pts[0].x - ax, pts[0].y - ay) <= 0.8, (name, wire.frm, "start dangles")
        assert hypot(pts[-1].x - bx, pts[-1].y - by) <= 0.8, (name, wire.to, "end dangles")

    bodies, labels, pins, xrefs = _bodies_and_labels(session, sheet, routes)
    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        own = []
        for end in (wire.frm, wire.to):
            if "." not in end:
                continue
            ref, pin_id = end.split(".", 1)
            for cid, pid, px, py in pins:
                if cid == ref and pid == pin_id:
                    own.append((px, py))
        for a, b in zip(pts, pts[1:]):
            for cid, box in bodies:
                if _overlap(a, b, box) <= 0.3:
                    continue
                assert _overlap(a, b, box) <= 1.2 and any(
                    min(hypot(a.x - px, a.y - py), hypot(b.x - px, b.y - py)) <= 1.2 for px, py in own
                ), (name, wire.number, wire.frm, "through body", cid)
            for owner, text, box in labels:
                if owner == wire.id:
                    continue
                assert _overlap(a, b, box) <= 0.3, (
                    name,
                    wire.number,
                    f"{wire.frm}->{wire.to}",
                    "through label",
                    owner,
                    text,
                )
            for owner, box in xrefs:
                assert _overlap(a, b, box) <= 0.3, (
                    name,
                    wire.number,
                    f"{wire.frm}->{wire.to}",
                    "through cross-ref",
                    owner,
                )
            for cid, pid, px, py in pins:
                if any(hypot(px - ox, py - oy) <= 0.4 for ox, oy in own):
                    continue
                if _overlap(a, b, (px - 1.1, py - 1.1, px + 1.1, py + 1.1)) <= 0.3:
                    continue
                assert _same_net_pin(str(wire.number or ""), px, py, sheet, routes), (
                    name,
                    wire.number,
                    f"{wire.frm}->{wire.to}",
                    "through pin",
                    f"{cid}.{pid}",
                )

    for comp in sheet.components:
        if "motor" not in comp.library_id:
            continue
        device = session.catalog.get(comp.library_id)
        for pin in device.pins:
            wx, wy = pin_world(comp, pin)
            landed = False
            for pts in routes.values():
                if not pts:
                    continue
                if hypot(pts[0].x - wx, pts[0].y - wy) <= 0.6 or hypot(pts[-1].x - wx, pts[-1].y - wy) <= 0.6:
                    landed = True
                    break
            assert landed, (name, comp.id, pin.id, "motor terminal has no wire")

    for index, (wa, a1, a2) in enumerate(segs):
        for wb, b1, b2 in segs[index + 1 :]:
            if str(wa.number or "") == str(wb.number or ""):
                continue
            pair = ((a1, a2), (b1, b2))
            horiz = abs(a1.y - a2.y) <= 0.35 and abs(b1.y - b2.y) <= 0.35
            vert = abs(a1.x - a2.x) <= 0.35 and abs(b1.x - b2.x) <= 0.35
            if not horiz and not vert:
                continue
            if horiz:
                sep = abs((a1.y + a2.y) * 0.5 - (b1.y + b2.y) * 0.5)
                span = min(max(a1.x, a2.x), max(b1.x, b2.x)) - max(min(a1.x, a2.x), min(b1.x, b2.x))
                length = min(abs(a2.x - a1.x), abs(b2.x - b1.x))
            else:
                sep = abs((a1.x + a2.x) * 0.5 - (b1.x + b2.x) * 0.5)
                span = min(max(a1.y, a2.y), max(b1.y, b2.y)) - max(min(a1.y, a2.y), min(b1.y, b2.y))
                length = min(abs(a2.y - a1.y), abs(b2.y - b1.y))
            if span <= 2.0 or length <= 3.0:
                continue
            assert sep >= GRID - 0.05, (
                name,
                wa.number,
                wb.number,
                "parallel runs closer than one grid",
                round(sep, 2),
            )

    pin_boxes = [(owner, text, box) for owner, text, box in labels if "." in str(owner)]
    for index, (owner, text, box) in enumerate(pin_boxes):
        for other, other_text, other_box in pin_boxes[index + 1 :]:
            assert not _boxes_hit(box, other_box), (name, owner, text, other, other_text)
    for comp in sheet.components:
        hidden = {str(pin_id) for pin_id in (comp.properties.get("hidden_pins") or [])}
        for owner, text, _box in pin_boxes:
            if owner.startswith(f"{comp.id}.") and owner.split(".", 1)[1] in hidden:
                raise AssertionError((name, owner, text, "hidden pin still captioned"))

    wire_ids = {wire.id for wire in sheet.wires}
    outlines = device_outline_boxes(sheet, session.catalog)
    by_wire = {wire.id: wire for wire in sheet.wires}
    elementary = sheet_drawing_style(sheet, session.project) in {"ladder", "iec"}
    rail_marks = {"24V", "0V", "X1", "X2", "L+", "M", "N"}
    if elementary:
        rail_marks.update({"L1", "L2"})
    for owner, text, box in labels:
        if owner not in wire_ids:
            continue
        cx = (box[0] + box[2]) * 0.5
        cy = (box[1] + box[3]) * 0.5
        own = _route_dist(cx, cy, routes.get(owner) or [])
        nearest_name, nearest = "", 1e9
        for other in sheet.wires:
            if other.id == owner:
                continue
            dist = _route_dist(cx, cy, routes.get(other.id) or [])
            if dist < nearest:
                nearest_name, nearest = str(other.number or other.id), dist
        assert nearest + 0.35 >= own, (
            name,
            text,
            "sits on",
            nearest_name,
            round(own, 2),
            round(nearest, 2),
        )
        if text in rail_marks:
            continue
        own_box = box_route_distance(box, routes.get(owner) or [])
        nearest_box_name, nearest_box = "", 1e9
        own_pts = routes.get(owner) or []
        for other in sheet.wires:
            if other.id == owner:
                continue
            other_pts = routes.get(other.id) or []
            other_mark = other.drawn_mark(ladder=elementary)
            if other_mark == text and (
                routes_share_line(own_pts, other_pts)
                or routes_share_collinear_run(own_pts, other_pts)
                or routes_near(own_pts, other_pts)
            ):
                continue
            dist = box_route_distance(box, other_pts)
            if dist < nearest_box:
                nearest_box_name, nearest_box = str(other.number or other.id), dist
        assert own_box + 0.15 < nearest_box, (
            name,
            text,
            "not nearer its own conductor",
            nearest_box_name,
            round(own_box, 2),
            round(nearest_box, 2),
        )
        for outline in outlines:
            assert not boxes_overlap(box, outline, pad=0.05), (
                name,
                text,
                "inside a device body",
                by_wire[owner].frm,
            )
        for xref_owner, xref_box in xrefs:
            assert not _boxes_hit(box, xref_box), (name, text, "overlaps cross-ref", xref_owner)

    _wx, wy, _ww, _wh = working_rect(sheet)
    for run in line_side_runs(session.project, sheet, session.catalog):
        top = min(point[1] for point in run.points)
        assert top >= wy - 0.05, (name, run.ref, round(top, 2), wy)
    for owner, text, box in pin_boxes:
        if text in {"1", "3", "5"} and box[3] < wy + 28:
            assert box[1] >= wy - 0.05, (name, owner, text, round(box[1], 2), wy)

    pens = wire_phase_pens(session.project, sheet, session.catalog)
    if pens:
        verts: list[tuple[str, str, float, float, float]] = []
        for wid, strokes in ink_polylines(hopped).items():
            pen = pens.get(wid)
            color = pen.stroke if pen is not None else ""
            for stroke in strokes:
                for a, b in zip(stroke, stroke[1:]):
                    if abs(a[0] - b[0]) <= 0.35 and abs(a[1] - b[1]) >= 0.6:
                        verts.append((wid, color, (a[0] + b[0]) * 0.5, min(a[1], b[1]), max(a[1], b[1])))
        for index, (wa, ca, ax, alo, ahi) in enumerate(verts):
            for wb, cb, bx, blo, bhi in verts[index + 1 :]:
                if wa == wb or not ca or not cb or ca == cb:
                    continue
                if abs(ax - bx) > 0.4:
                    continue
                overlap = min(ahi, bhi) - max(alo, blo)
                assert overlap <= 0.45, (name, "drop changes color", round(ax, 1), round(overlap, 2))

    for comp in sheet.components:
        try:
            device = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        layout = device_ref_layout(device, comp, ref_align="middle")
        if layout is None:
            continue
        tx, ty, align, size = layout
        tag = text_box(tx, ty, comp.id, size, align)
        if tag is None:
            continue
        for other in sheet.components:
            if other.id == comp.id:
                continue
            try:
                other_device = session.catalog.get(other.library_id)
            except KeyError:
                continue
            for prim in other_device.primitives:
                if prim.type != "circle":
                    continue
                cx, cy = _world(other, prim.x, prim.y)
                radius = prim.r * other.visual_scale()
                nearest_x = min(max(cx, tag[0]), tag[2])
                nearest_y = min(max(cy, tag[1]), tag[3])
                assert hypot(nearest_x - cx, nearest_y - cy) > radius - 0.05, (
                    name,
                    comp.id,
                    "tag on",
                    other.id,
                )


def test_six_lead_motor_stubs_meet_the_circle():
    """Every terminal stub of a 6-lead motor reaches the circle outline."""

    session = Session.create("stubs")
    for library_id in ("iec.loads.motor_3ph_6lead", "nema.loads.motor_3ph_6lead"):
        device = session.catalog.get(library_id)
        circle = next(prim for prim in device.primitives if prim.type == "circle")
        lines = []
        for prim in device.primitives:
            if prim.type == "line":
                lines.append(((prim.x, prim.y), (prim.x2, prim.y2)))
            elif prim.type == "polyline":
                pts = [(float(x), float(y)) for x, y in prim.points]
                lines.extend(zip(pts, pts[1:]))
        for pin in device.pins:
            seen = {(round(pin.x, 2), round(pin.y, 2))}
            stack = [(pin.x, pin.y)]
            reached = None
            while stack:
                x, y = stack.pop()
                if abs(hypot(x - circle.x, y - circle.y) - circle.r) <= 0.15:
                    reached = (x, y)
                    break
                for a, b in lines:
                    for src, dst in ((a, b), (b, a)):
                        if hypot(src[0] - x, src[1] - y) > 0.15:
                            continue
                        key = (round(dst[0], 2), round(dst[1], 2))
                        if key in seen:
                            continue
                        seen.add(key)
                        stack.append(dst)
            assert reached is not None, (library_id, pin.id, "stub does not meet the circle")
            # U1 and W1 land on the upper arc, the same way V1 meets the top.
            if pin.id in {"U1", "W1", "T1", "T3", "V1", "T2"}:
                assert reached[1] < circle.y - 4.0, (library_id, pin.id, reached, "lands on the side")


def test_winding_end_stubs_share_the_wire_y():
    """W2/U2/V2 shelves are the pin Y. A 0.2 mm step reads as 2 px at sheet scale."""

    session = Session.create("stub align")
    session.set_title_block(standard="IEC")
    result = session.insert_circuit(kind="star_delta", control="local", timer_seconds=8)
    sheet = session.sheet(result["sheet"])
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    motor = next(comp for comp in sheet.components if "6lead" in comp.library_id)
    device = session.catalog.get(motor.library_id)
    for pin_id in ("W2", "U2", "V2"):
        px, py = pin_world(motor, device.pin(pin_id))
        touched = False
        for wire in sheet.wires:
            if f"{motor.id}.{pin_id}" not in {wire.frm, wire.to}:
                continue
            pts = routes.get(wire.id) or []
            for a, b in zip(pts, pts[1:]):
                if min(hypot(a.x - px, a.y - py), hypot(b.x - px, b.y - py)) > 0.6:
                    continue
                touched = True
                assert abs(a.y - py) <= 0.08 and abs(b.y - py) <= 0.08, (
                    pin_id,
                    round(py, 2),
                    round(a.y, 2),
                    round(b.y, 2),
                )
                break
        assert touched, pin_id


def test_iec_cross_reference_uses_the_placed_column():
    """The mirror column is the contact's pin, and K1 lists both aux pairs."""

    for name, session, sheet_id in _sheets():
        if "control" not in name:
            continue
        sheet = session.sheet(sheet_id)
        grid = zone_grid(sheet)
        families = xref_families(session.project, session.catalog)
        rows = iec_mirror_rows(families["K1"])
        terminals = {row["terminals"] for row in rows}
        assert {"13-14", "23-24"} <= terminals, (name, terminals)
        for family in families.values():
            for row in iec_mirror_rows(family):
                comp = next((item for item in sheet.components if item.id == row["contact"]), None)
                if comp is None:
                    continue
                device = session.catalog.get(comp.library_id)
                pins = [pin for pin in device.pins if (pin.side or "") in {"left", "right"}]
                prefer = {"A1", "A", "3", "1", "13", "21", "95", "11"}
                pin = next((item for item in pins if str(item.id) in prefer), pins[0] if pins else None)
                if pin is None:
                    continue
                px, _py = pin_world(comp, pin)
                printed = str(row["location"]).split(".")[-1]
                assert printed == str(grid.col_at(px)), (
                    name,
                    row["contact"],
                    row["terminals"],
                    row["location"],
                    round(px, 1),
                    grid.col_at(px),
                )


def test_aux_contact_art_clears_the_body():
    """95–98 and 13–14 are drawn beside the outline, not through it."""

    session = Session.create("aux art")
    for library_id in (
        "iec.protection.overload_9a",
        "nema.protection.overload",
        "iec.contactors.ac3_9a",
    ):
        device = session.catalog.get(library_id)
        rects = [prim for prim in device.primitives if prim.type == "rect"]
        body = max(rects, key=lambda prim: prim.w * prim.h)
        right = body.x + body.w
        outside = []
        for prim in device.primitives:
            if prim.type != "line":
                continue
            lo = min(prim.x, prim.x2)
            if lo + 0.05 < right:
                continue
            outside.append(prim)
            assert lo >= right + 1.5, (library_id, round(lo, 2), round(right, 2))
        assert outside, library_id


def test_nema_coil_box_clears_the_first_pole():
    session = Session.create("coil box")
    for library_id, pole_x in (("nema.contactors.ac3_9a", 10.0), ("nema.contactors.ac3_32a", 11.0)):
        device = session.catalog.get(library_id)
        coils = [prim for prim in device.primitives if prim.type == "rect" and prim.w < 12 and prim.h < 16]
        assert coils, library_id
        for coil in coils:
            assert coil.x + coil.w <= pole_x - 0.4, (library_id, coil.x + coil.w, pole_x)


def test_star_delta_phase_label_is_once_and_on_its_terminal():
    """L1/L2/L3 are the terminal captions. 2/4/6 stay on Q1, not on K1's line side."""

    session = Session.create("phase labels")
    session.set_title_block(standard="IEC")
    result = session.insert_circuit(kind="star_delta", control="local", timer_seconds=8)
    sheet = session.sheet(result["sheet"])
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    marks = place_wire_marks(sheet, session.catalog, routes, ladder=False, project=session.project)
    drawn = set()
    for wire in sheet.wires:
        if marks.get(wire.id):
            drawn.add(wire.drawn_mark(ladder=False))
    assert "L1" not in drawn and "L2" not in drawn and "L3" not in drawn
    pins: list[tuple[float, float]] = []
    located: dict[tuple[str, str], tuple[float, float]] = {}
    for comp in sheet.components:
        device = session.catalog.get(comp.library_id)
        for pin in device.pins:
            wx, wy = pin_world(comp, pin)
            pins.append((wx, wy))
            located[(comp.id, pin.id)] = (wx, wy)
    for comp in sheet.components:
        device = session.catalog.get(comp.library_id)
        for pin in device.pins:
            if (comp.id, pin.id) not in {("Q1", "2"), ("Q1", "4"), ("Q1", "6"), ("K1", "L1"), ("K1", "L2"), ("K1", "L3"), ("Q1", "1"), ("Q1", "3"), ("Q1", "5")}:
                continue
            wx, wy = located[(comp.id, pin.id)]
            side = pin_side_world(comp, pin)
            cap = pin_caption(pin, comp)
            tuck = stacked_power_tuck(side, wx, wy, pins)
            tx, ty, align = pin_label_anchor(wx, wy, side, caption=cap, tuck=tuck)
            box = text_box(tx, ty, cap, 1.9, align)
            assert box is not None
            if comp.id == "K1":
                assert box[1] > wy, (pin.id, box, wy)
            else:
                assert box[3] < wy, (comp.id, pin.id, box, wy)


def test_wire_number_box_is_nearer_its_own_conductor():
    """Each wire-number box is closer to its wire than to any other, and off device bodies."""

    for name, session, sheet_id in _sheets():
        sheet = session.sheet(sheet_id)
        style = sheet_drawing_style(sheet, session.project)
        elementary = style in {"ladder", "iec"}
        routes = route_sheet_wires(sheet, session.catalog, project=session.project)
        marks = place_wire_marks(sheet, session.catalog, routes, ladder=elementary, project=session.project)
        drawn_specs = bundle_spec_plan(sheet, routes, ladder=elementary)
        outlines = device_outline_boxes(sheet, session.catalog)
        rail_marks = {"24V", "0V", "X1", "X2", "L+", "M", "N"}
        if elementary:
            rail_marks.update({"L1", "L2"})
        for wire in sheet.wires:
            at = marks.get(wire.id)
            mark = wire.drawn_mark(ladder=elementary)
            if not at or not mark or mark in rail_marks:
                continue
            box = mark_box(at[0], at[1], mark, align=at[2], spec=drawn_specs.get(wire.id, ""))
            own = box_route_distance(box, routes.get(wire.id) or [])
            nearest_name, nearest = "", 1e9
            own_pts = routes.get(wire.id) or []
            for other in sheet.wires:
                if other.id == wire.id:
                    continue
                other_pts = routes.get(other.id) or []
                if other.drawn_mark(ladder=elementary) == mark and (
                    routes_share_line(own_pts, other_pts)
                    or routes_share_collinear_run(own_pts, other_pts)
                    or routes_near(own_pts, other_pts)
                ):
                    continue
                dist = box_route_distance(box, other_pts)
                if dist < nearest:
                    nearest_name, nearest = str(other.number or other.id), dist
            assert own + 0.15 < nearest, (name, mark, nearest_name, round(own, 2), round(nearest, 2))
            for outline in outlines:
                assert not boxes_overlap(box, outline, pad=0.05), (name, mark, "inside a device body")


def test_contact_column_comes_from_the_header_cell():
    """The printed column is the header cell that contains the contact, not a neighbour."""

    for name, session, sheet_id in _sheets():
        if "control" not in name:
            continue
        sheet = session.sheet(sheet_id)
        cells = header_columns(sheet)
        nine = next(cell for cell in cells if cell[0] == 9)
        ten = next(cell for cell in cells if cell[0] == 10)
        # 3360-wide render is 8 px/mm. Column 9's label is near 1890 px, column 10 near 2104.
        assert abs(nine[3] * 8 - 1890) < 16, nine
        assert abs(ten[3] * 8 - 2104) < 16, ten
        sample = 1919 / 8.0
        assert nine[1] <= sample < nine[2]
        assert column_from_header(sheet, sample) == 9
        families = xref_families(session.project, session.catalog)
        kept = {("K4-18", "15-18"), ("K3-21", "21-22")}
        for family in families.values():
            for row in iec_mirror_rows(family):
                comp = next((item for item in sheet.components if item.id == row["contact"]), None)
                if comp is None:
                    continue
                device = session.catalog.get(comp.library_id)
                pins = [pin for pin in device.pins if (pin.side or "") in {"left", "right"}]
                prefer = {"A1", "A", "3", "1", "13", "21", "95", "11"}
                pin = next((item for item in pins if str(item.id) in prefer), pins[0] if pins else None)
                if pin is None:
                    continue
                px, _py = pin_world(comp, pin)
                cell = column_from_header(sheet, px)
                printed = str(row["location"]).split(".")[-1]
                assert printed == str(cell), (name, row["contact"], row["location"], cell, round(px, 1))
                if (row["contact"], row["terminals"]) in kept:
                    assert str(row["location"]).endswith(".9"), row


def test_overload_cross_reference_clears_aux_terminals():
    session = Session.create("f1 xref")
    session.set_title_block(standard="IEC")
    result = session.insert_circuit(kind="star_delta", control="local", timer_seconds=8)
    sheet = session.sheet(result["sheet"])
    placed = place_xref_labels(session.project, session.catalog, sheet, ladder=False)
    table = next(item for item in placed if item[0].owner == "F1")
    box = annotation_box(table[0], table[1], table[2], table[3])
    overload = next(comp for comp in sheet.components if comp.id == "F1")
    device = session.catalog.get(overload.library_id)
    for pin_id in ("95", "96", "97", "98"):
        wx, wy = pin_world(overload, device.pin(pin_id))
        side = pin_side_world(overload, device.pin(pin_id))
        cap = pin_caption(device.pin(pin_id), overload)
        tx, ty, align = pin_label_anchor(wx, wy, side, caption=cap)
        caption = text_box(tx, ty, cap, 1.9, align)
        assert caption is not None
        assert not boxes_overlap(box, caption, pad=0.4), (pin_id, box, caption)
        assert box[0] >= wx + 2.0, (pin_id, box[0], wx)


def test_reversing_cross_reference_stays_inside_the_frame():
    from panelcad.core.ladder import iec_rail_ys
    from panelcad.render.xref import mirror_size

    session = Session.create("rev frame")
    session.set_title_block(standard="IEC")
    result = session.insert_circuit(kind="reversing", control="local", pilot_lights=True)
    sheet = session.sheet(result["control_sheet"])
    _wx, wy, _ww, wh = working_rect(sheet)
    frame_bottom = wy + wh
    _top, rail = iec_rail_ys(sheet)
    placed = place_xref_labels(session.project, session.catalog, sheet, ladder=True)
    for ann, x, y, align in placed:
        if ann.kind != "mirror":
            continue
        box = annotation_box(ann, x, y, align)
        _w, h = mirror_size(ann)
        rect_bottom = y - 0.6 + h
        assert box[3] <= frame_bottom - 2.0, (ann.owner, box[3], frame_bottom)
        assert rect_bottom <= rail - 1.0, (ann.owner, rect_bottom, rail)


def test_psu_primary_is_an_offpage_arrow():
    """L, N, and PE get a short reference arrow. The control sheet draws no primary run."""

    for kind in ("star_delta", "reversing"):
        session = Session.create(kind)
        session.set_title_block(standard="IEC")
        kwargs = {"timer_seconds": 8} if kind == "star_delta" else {"pilot_lights": True}
        result = session.insert_circuit(kind=kind, control="local", **kwargs)
        sheet = session.sheet(result["control_sheet"])
        power = session.sheet(result["sheet"])
        supply = next(comp for comp in sheet.components if "psu" in comp.library_id)
        device = session.catalog.get(supply.library_id)
        routes = route_sheet_wires(sheet, session.catalog, project=session.project)
        arrows = [comp for comp in sheet.components if comp.properties.get("role") == "supply_ref"]
        assert len(arrows) == 3, kind
        stubs = []
        for pin_id in ("L", "N", "PE"):
            px, py = pin_world(supply, device.pin(pin_id))
            landed = [
                wire
                for wire in sheet.wires
                if f"{supply.id}.{pin_id}" in {wire.frm, wire.to}
            ]
            assert len(landed) == 1, (kind, pin_id)
            wire = landed[0]
            pts = routes.get(wire.id) or []
            span = sum(hypot(b.x - a.x, b.y - a.y) for a, b in zip(pts, pts[1:]))
            assert span < 6.0, (kind, pin_id, round(span, 2))
            other = wire.to if wire.frm.startswith(supply.id) else wire.frm
            other_id = other.split(".", 1)[0]
            arrow = sheet.component(other_id)
            assert "offpage" in arrow.library_id
            callout = str(arrow.properties.get("callout") or "")
            assert "/" in callout and any(ch.isdigit() for ch in callout), (kind, pin_id, callout)
            if pin_id == "PE":
                assert callout.startswith("PE"), callout
            ax, ay = pin_world(arrow, session.catalog.get(arrow.library_id).pin("A"))
            assert ax < px and hypot(ax - px, ay - py) < 4.0, (kind, pin_id, ax, px)
            assert wire.number == "" and wire.number_locked is True, (kind, pin_id, wire.number)
            stubs.append(wire.id)
        marks = place_wire_marks(sheet, session.catalog, routes, ladder=True, project=session.project)
        assert all(marks.get(wire_id) is None for wire_id in stubs), kind
        assert any(comp.properties.get("role") == "pe_bar" for comp in power.components), kind


def _segment_hits_box(
    box: tuple[float, float, float, float],
    ax: float,
    ay: float,
    bx: float,
    by: float,
    pad: float = 0.3,
) -> bool:
    left, top, right, bottom = box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad

    def inside(x: float, y: float) -> bool:
        return left <= x <= right and top <= y <= bottom

    if inside(ax, ay) or inside(bx, by):
        return True

    def crosses(x1: float, y1: float, x2: float, y2: float, x3: float, y3: float, x4: float, y4: float) -> bool:
        den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        if abs(den) < 1e-9:
            return False
        tee = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
        you = ((x1 - x3) * (y1 - y2) - (y1 - y3) * (x1 - x2)) / den
        return 0.0 <= tee <= 1.0 and 0.0 <= you <= 1.0

    edges = (
        (left, top, right, top),
        (right, top, right, bottom),
        (right, bottom, left, bottom),
        (left, bottom, left, top),
    )
    return any(crosses(ax, ay, bx, by, *edge) for edge in edges)


def test_iec_power_tags_stay_on_their_symbols():
    """M1 stays on the motor. Q1 stays off the PE symbol. The screen uses that seat."""

    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import ComponentItem, SchematicView
    from panelcad.render.sheet_labels import place_device_tags, symbol_body_box

    QApplication.instance() or QApplication([])
    session = Session.create("tag seat")
    session.set_title_block(standard="IEC")
    result = session.insert_circuit(kind="star_delta", control="local")
    sheet = session.sheet(result["sheet"])
    tags = place_device_tags(sheet, session.catalog, project=session.project, sheet_no=1)
    motor = sheet.component(next(item["id"] for item in result["components"] if item["role"] == "motor"))
    overload = sheet.component(next(item["id"] for item in result["components"] if item["role"] == "overload"))
    breaker = sheet.component(next(item["id"] for item in result["components"] if item["role"] == "breaker"))
    earth = next(comp for comp in sheet.components if comp.properties.get("role") == "pe_bar")
    motor_box = text_box(*tags[motor.id][:3], tags[motor.id][3])
    assert motor_box is not None
    motor_mid = (motor_box[1] + motor_box[3]) * 0.5
    motor_body = symbol_body_box(motor, session.catalog.get(motor.library_id))
    overload_body = symbol_body_box(overload, session.catalog.get(overload.library_id))
    assert motor_body[1] - 1.0 <= motor_mid <= motor_body[3] + 1.0
    assert abs(motor_mid - (motor_body[1] + motor_body[3]) * 0.5) < abs(
        motor_mid - (overload_body[1] + overload_body[3]) * 0.5
    )
    breaker_box = text_box(*tags[breaker.id][:3], tags[breaker.id][3])
    assert breaker_box is not None
    earth_body = symbol_body_box(earth, session.catalog.get(earth.library_id))
    assert not boxes_overlap(breaker_box, earth_body)
    shares_x = breaker_box[2] > earth_body[0] + 0.6 and breaker_box[0] < earth_body[2] - 0.6
    if shares_x:
        assert breaker_box[1] >= earth_body[3] + 6.0
    session.set_active_sheet(result["sheet"])
    view = SchematicView(session)
    item = next(
        child for child in view.scene.items() if isinstance(child, ComponentItem) and child.component_id == motor.id
    )
    assert item._tag_layout is not None
    assert abs(item._tag_layout[0] + item.pos().x() - tags[motor.id][0]) < 0.05
    assert abs(item._tag_layout[1] + item.pos().y() - tags[motor.id][1]) < 0.05


def test_six_lead_names_clear_the_motor_stubs():
    """U1, V1, and W1 are beside the lead, not struck through by it."""

    from PySide6.QtWidgets import QApplication

    from panelcad.render.qt_painter import text_advance_mm

    QApplication.instance() or QApplication([])
    session = Session.create("lead names")
    session.set_title_block(standard="IEC")
    result = session.insert_circuit(kind="star_delta", control="local")
    sheet = session.sheet(result["sheet"])
    motor = sheet.component(next(item["id"] for item in result["components"] if item["role"] == "motor"))
    device = session.catalog.get(motor.library_id)
    lines = [prim for prim in device.primitives if prim.type == "line"]
    for prim in device.primitives:
        if prim.type != "text" or prim.text not in {"U1", "V1", "W1"}:
            continue
        advance = text_advance_mm(str(prim.text), float(prim.size))
        cx, cy = motor.x + prim.x, motor.y + prim.y
        box = (cx - advance / 2.0, cy - prim.size * 0.9, cx + advance / 2.0, cy + prim.size * 0.25)
        for line in lines:
            assert not _segment_hits_box(
                box,
                motor.x + line.x,
                motor.y + line.y,
                motor.x + line.x2,
                motor.y + line.y2,
            ), prim.text


def test_iec_reversing_power_labels_do_not_overprint():
    """Pin captions and wire numbers on the reversing power sheet stay apart."""

    from panelcad.render.sheet_labels import colliding_sheet_labels, pin_caption_boxes, sheet_text_boxes
    from panelcad.render.wiring import route_sheet_wires

    session = Session.create("rev labels")
    session.set_title_block(standard="IEC")
    result = session.insert_circuit(kind="reversing", control="local", pilot_lights=True)
    sheet = session.sheet(result["sheet"])
    labels = sheet_text_boxes(session.project, session.catalog, sheet, sheet_no=1)
    assert any(name.startswith("pin:") for name, _box in labels)
    pin_hits = [
        hit
        for hit in colliding_sheet_labels(session.project, session.catalog, sheet, sheet_no=1)
        if "pin:" in hit
    ]
    assert pin_hits == [], pin_hits
    captions = {name: box for name, box in pin_caption_boxes(sheet, session.catalog)}
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    motor = sheet.component(next(item["id"] for item in result["components"] if item["role"] == "motor"))
    device = session.catalog.get(motor.library_id)
    for pin in device.pins:
        if pin.id not in {"U1", "V1", "W1"}:
            continue
        box = captions[f"pin:{motor.id}:{pin.id}"]
        for prim in device.primitives:
            if prim.type != "line":
                continue
            assert not _segment_hits_box(
                box,
                motor.x + prim.x,
                motor.y + prim.y,
                motor.x + prim.x2,
                motor.y + prim.y2,
                pad=0.15,
            ), pin.id
    marks = place_wire_marks(sheet, session.catalog, routes, ladder=False, project=session.project)
    for wire in sheet.wires:
        if wire.caption() not in {"104", "105", "106"}:
            continue
        at = marks.get(wire.id)
        if at is None:
            continue
        number = mark_box(at[0], at[1], wire.caption(), align=at[2])
        if wire.caption() == "104":
            overload = sheet.component(
                next(item["id"] for item in result["components"] if item["role"] == "overload")
            )
            pin = session.catalog.get(overload.library_id).pin("2")
            from panelcad.render.sheet import pin_world

            px, py = pin_world(overload, pin)
            assert not boxes_overlap(number, (px - 1.2, py - 1.2, px + 1.2, py + 1.2))
            neighbor = captions.get(f"pin:{overload.id}:4")
            assert neighbor is not None
            gap_x = max(number[0] - neighbor[2], neighbor[0] - number[2])
            gap_y = max(number[1] - neighbor[3], neighbor[1] - number[3])
            assert gap_x >= 0.8 or gap_y >= 0.8, (number, neighbor)
        for prim in device.primitives:
            if prim.type != "line":
                continue
            assert not _segment_hits_box(
                number,
                motor.x + prim.x,
                motor.y + prim.y,
                motor.x + prim.x2,
                motor.y + prim.y2,
                pad=0.15,
            ), (wire.caption(), wire.id)
    breaker = sheet.component(next(item["id"] for item in result["components"] if item["role"] == "breaker"))
    for wire in sheet.wires:
        for end in (wire.frm, wire.to):
            if not end.startswith(f"{breaker.id}."):
                continue
            pin_id = end.split(".", 1)[1]
            if pin_id not in {"2", "4", "6"}:
                continue
            box = captions.get(f"pin:{breaker.id}:{pin_id}")
            assert box is not None
            for a, b in zip(routes[wire.id], routes[wire.id][1:]):
                assert not _segment_hits_box(box, a.x, a.y, b.x, b.y, pad=0.15), (pin_id, wire.id)


def test_canvas_omits_a_wire_number_the_print_does_not_draw():
    """107, 108, and 109 appear once, on the conductor that owns the mark."""

    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import SchematicView, WireItem
    from panelcad.render.wiring import route_sheet_wires

    QApplication.instance() or QApplication([])
    session = Session.create("screen numbers")
    session.set_title_block(standard="IEC")
    result = session.insert_circuit(kind="star_delta", control="local")
    sheet = session.sheet(result["sheet"])
    session.set_active_sheet(sheet.id)
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    marks = place_wire_marks(sheet, session.catalog, routes, ladder=False, project=session.project)
    view = SchematicView(session)
    shown: dict[str, str] = {}
    for item in view.scene.items():
        if isinstance(item, WireItem):
            shown[item.wire_id] = item._label
    for wire in sheet.wires:
        if wire.id not in marks:
            assert shown.get(wire.id, "") == "", (wire.id, wire.caption(), shown.get(wire.id))
        else:
            assert shown.get(wire.id, "") == wire.caption()
    for number in ("107", "108", "109"):
        by_id = {wire.id: wire for wire in sheet.wires}
        painted = [wire_id for wire_id, label in shown.items() if label == number]
        seated = [wire_id for wire_id, at in marks.items() if at and by_id[wire_id].caption() == number]
        assert set(painted) == set(seated), (number, painted, seated)


def test_iec_control_screen_matches_the_print():
    """Rotated tags, the timer coil, seated wire numbers, and the hidden E-stop contact."""

    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication, QStyleOptionGraphicsItem

    from panelcad.gui.canvas import ComponentItem, PinItem, SchematicView, WireItem
    from panelcad.render.label_avoid import resolve_labels

    QApplication.instance() or QApplication([])
    # The sheet title is the project name. "control" in that title would make
    # the power sheet look like a reusable Motor control page.
    session = Session.create("screen match")
    session.set_title_block(standard="IEC")
    result = session.insert_circuit(kind="star_delta", control="local", timer_seconds=8)
    sheet = session.sheet(result["control_sheet"])
    session.set_active_sheet(sheet.id)
    tags = resolve_labels(session.project, session.catalog, sheet, ladder=True).tags
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    marks = place_wire_marks(sheet, session.catalog, routes, ladder=True, project=session.project)
    view = SchematicView(session)
    shown: dict[str, str] = {}
    for item in view.scene.items():
        if isinstance(item, WireItem):
            shown[item.wire_id] = item._label
        if not isinstance(item, ComponentItem):
            continue
        if item.component_id in tags and item._tag_layout is not None:
            anchor = item.mapToScene(QPointF(item._tag_layout[0], item._tag_layout[1]))
            seat = tags[item.component_id]
            assert abs(anchor.x() - seat[0]) < 0.05, (item.component_id, anchor.x(), seat[0])
            assert abs(anchor.y() - seat[1]) < 0.05, (item.component_id, anchor.y(), seat[1])
    estop = sheet.component(next(item["id"] for item in result["components"] if item["role"] == "estop"))
    estop_item = next(item for item in view.scene.items() if isinstance(item, ComponentItem) and item.component_id == estop.id)
    pin_ids = {child.pin_id for child in estop_item.childItems() if isinstance(child, PinItem)}
    assert pin_ids.isdisjoint({"21", "22"})
    timer = next(
        comp
        for comp in sheet.components
        if "timer" in session.catalog.get(comp.library_id).category.lower()
    )
    timer_item = next(item for item in view.scene.items() if isinstance(item, ComponentItem) and item.component_id == timer.id)
    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    timer_item.paint(painter, QStyleOptionGraphicsItem(), None)
    painter.end()
    assert timer_item._painted_symbol == "on_delay"
    for wire in sheet.wires:
        if wire.id not in marks:
            assert shown.get(wire.id, "") == ""
        else:
            assert shown.get(wire.id, "") == wire.drawn_mark(ladder=True)
    seated = {
        wire.drawn_mark(ladder=True)
        for wire in sheet.wires
        if wire.id in marks and wire.drawn_mark(ladder=True)
    }
    assert seated & {str(number) for number in range(201, 210)}
