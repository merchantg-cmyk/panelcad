"""Schematic hops where two different nets cross on a generated I/O sheet.

A 3-wire sensor's signal lead has to meet the return spine. The spine stays
continuous through the crossing and is opened only under the crown of the hop,
so the two conductors do not touch. Same-net wires are left alone: a tee is a
junction dot, not a hop.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import atan2, cos, hypot, pi, sin

from panelcad.core.geometry import Point
from panelcad.core.layout import is_generated_io_sheet
from panelcad.core.models import Sheet, Wire
from panelcad.core.route import Rect, path_hits_obstacles
from panelcad.library.catalog import Catalog
from panelcad.render.wiring import component_obstacle, route_sheet_wires

# Hop radius and the gap opened in the crossed wire under the crown.
# The gap is wider than the stroke so the crown does not kiss the other net.
RADIUS = 2.0
HALF_GAP = 0.9
MIN_RADIUS = 1.15
_END_PAD = 0.45


@dataclass
class _Hop:
    seg: int
    t: float
    cx: float
    cy: float
    r: float
    ux: float
    uy: float
    px: float
    py: float


@dataclass
class _Gap:
    x: float
    y: float
    half: float


def rendered_wire_paths(
    sheet: Sheet,
    catalog: Catalog,
    routes: dict[str, list[Point]] | None = None,
) -> dict[str, list[list[Point]]]:
    """One polyline per electrical run, split where a hop opens a gap."""

    if routes is None:
        routes = route_sheet_wires(sheet, catalog)
    plain = {wid: [list(pts)] for wid, pts in routes.items() if len(pts) >= 2}
    if not is_generated_io_sheet(sheet):
        return plain
    hops, gaps = _plan(sheet, catalog, routes)
    out: dict[str, list[list[Point]]] = {}
    for wid, pts in routes.items():
        if len(pts) < 2:
            continue
        hopped = _insert_hops(pts, hops.get(wid, []))
        out[wid] = _split_gaps(hopped, gaps.get(wid, []))
    return out


def label_routes(
    electrical: dict[str, list[Point]],
    paths: dict[str, list[list[Point]]],
) -> dict[str, list[Point]]:
    """Routes for wire-number placement.

    A single hopped run keeps the arc so the number can miss it. A gapped run
    keeps the electrical centerline; bridging the gap would draw the number in
    the opening.
    """

    out: dict[str, list[Point]] = {}
    for wid, pts in electrical.items():
        subs = paths.get(wid) or []
        if len(subs) == 1 and len(subs[0]) >= 2:
            out[wid] = subs[0]
        else:
            out[wid] = list(pts)
    return out


def conductor_hits(sheet: Sheet, catalog: Catalog) -> list[str]:
    """Different-net crossings and collinear overlaps, after hops are applied."""

    routes = route_sheet_wires(sheet, catalog)
    paths = rendered_wire_paths(sheet, catalog, routes)
    wires = {wire.id: wire for wire in sheet.wires}
    hits: list[str] = []
    ids = [wire.id for wire in sheet.wires if wire.id in paths]
    for i, left in enumerate(ids):
        for right in ids[i + 1 :]:
            if _same_net(wires[left], wires[right]):
                continue
            for seg_a in _segments(paths[left]):
                for seg_b in _segments(paths[right]):
                    cross = _proper_cross(*seg_a, *seg_b)
                    if cross is not None:
                        hits.append(
                            f"cross {left} {wires[left].frm}->{wires[left].to} "
                            f"x {right} {wires[right].frm}->{wires[right].to} "
                            f"at {cross[0]:.1f},{cross[1]:.1f}"
                        )
                    over = _collinear_overlap(*seg_a, *seg_b)
                    if over > 0.8:
                        hits.append(
                            f"overlap {left} {wires[left].frm}->{wires[left].to} "
                            f"x {right} {wires[right].frm}->{wires[right].to}"
                        )
    return hits


def _plan(
    sheet: Sheet,
    catalog: Catalog,
    routes: dict[str, list[Point]],
) -> tuple[dict[str, list[_Hop]], dict[str, list[_Gap]]]:
    obstacles = _obstacles(sheet, catalog)
    hops: dict[str, list[_Hop]] = {}
    gaps: dict[str, list[_Gap]] = {}
    wires = list(sheet.wires)
    for i, left in enumerate(wires):
        pts_a = routes.get(left.id) or []
        if len(pts_a) < 2:
            continue
        for right in wires[i + 1 :]:
            if _same_net(left, right):
                continue
            pts_b = routes.get(right.id) or []
            if len(pts_b) < 2:
                continue
            for ia, (a0, a1) in enumerate(zip(pts_a, pts_a[1:])):
                for ib, (b0, b1) in enumerate(zip(pts_b, pts_b[1:])):
                    cross = _proper_cross(a0, a1, b0, b1)
                    if cross is None:
                        continue
                    placed = _place_hop(
                        a0, a1, b0, b1, cross, obstacles, hopper_first=True
                    )
                    if placed is None:
                        placed = _place_hop(
                            b0, b1, a0, a1, cross, obstacles, hopper_first=False
                        )
                    if placed is None:
                        continue
                    hop, gap, on_a = placed
                    if on_a:
                        hop.seg = ia
                        hops.setdefault(left.id, []).append(hop)
                        gaps.setdefault(right.id, []).append(gap)
                    else:
                        hop.seg = ib
                        hops.setdefault(right.id, []).append(hop)
                        gaps.setdefault(left.id, []).append(gap)
    return hops, gaps


def _place_hop(
    h0: Point,
    h1: Point,
    c0: Point,
    c1: Point,
    cross: tuple[float, float],
    obstacles: list[Rect],
    *,
    hopper_first: bool,
):
    """Hop ``h0-h1`` over ``c0-c1`` when the run is the more horizontal one."""

    hdx = abs(h1.x - h0.x)
    cdx = abs(c1.x - c0.x)
    # The caller tries each side. Hop the run that is at least as horizontal.
    if hdx + 0.05 < cdx:
        return None
    ux, uy, length = _unit(h0, h1)
    if length < 2 * MIN_RADIUS + 1.2:
        return None
    radius = min(RADIUS, (length - 1.2) / 2.0)
    if radius < MIN_RADIUS:
        return None
    cx, cy = cross
    t = hypot(cx - h0.x, cy - h0.y) / length
    # Prefer a crown toward the bottom of the sheet. Wire numbers sit above.
    perps = ((-uy, ux), (uy, -ux))
    perps = tuple(sorted(perps, key=lambda item: -item[1]))
    chosen: tuple[_Hop, _Gap] | None = None
    for px, py in perps:
        apex_x, apex_y = cx + px * radius, cy + py * radius
        if _project(apex_x, apex_y, c0, c1) is None:
            continue
        crossed = hypot(c1.x - c0.x, c1.y - c0.y)
        half = min(HALF_GAP, crossed / 2.0 - 0.35)
        if half < 0.45:
            continue
        hop = _Hop(0, t, cx, cy, radius, ux, uy, px, py)
        arc = _arc_points(hop)
        if path_hits_obstacles(arc, obstacles, skip_ends=False):
            if chosen is None:
                chosen = (hop, _Gap(apex_x, apex_y, half))
            continue
        return hop, _Gap(apex_x, apex_y, half), hopper_first
    if chosen is None:
        return None
    return chosen[0], chosen[1], hopper_first


def _obstacles(sheet: Sheet, catalog: Catalog) -> list[Rect]:
    rects: list[Rect] = []
    for comp in sheet.components:
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        if (device.category or "").lower().startswith("connection.notes"):
            continue
        if "waypoint" in comp.library_id or "junction" in comp.library_id:
            continue
        rects.append(component_obstacle(sheet, catalog, comp, pad=0.3))
    return rects


def _insert_hops(pts: list[Point], hops: list[_Hop]) -> list[Point]:
    if not hops:
        return list(pts)
    by_seg: dict[int, list[_Hop]] = {}
    for hop in hops:
        by_seg.setdefault(hop.seg, []).append(hop)
    out: list[Point] = [pts[0]]
    for index, (start, end) in enumerate(zip(pts, pts[1:])):
        for hop in sorted(by_seg.get(index, []), key=lambda item: item.t):
            entry = Point(x=hop.cx - hop.ux * hop.r, y=hop.cy - hop.uy * hop.r)
            exit_ = Point(x=hop.cx + hop.ux * hop.r, y=hop.cy + hop.uy * hop.r)
            if hypot(out[-1].x - entry.x, out[-1].y - entry.y) > 0.05:
                out.append(entry)
            for point in _arc_points(hop):
                if hypot(out[-1].x - point.x, out[-1].y - point.y) > 0.04:
                    out.append(point)
            if hypot(out[-1].x - exit_.x, out[-1].y - exit_.y) > 0.05:
                out.append(exit_)
        if hypot(out[-1].x - end.x, out[-1].y - end.y) > 0.05:
            out.append(end)
    return out


def _arc_points(hop: _Hop, steps: int = 6) -> list[Point]:
    entry = (hop.cx - hop.ux * hop.r, hop.cy - hop.uy * hop.r)
    exit_ = (hop.cx + hop.ux * hop.r, hop.cy + hop.uy * hop.r)
    apex = (hop.cx + hop.px * hop.r, hop.cy + hop.py * hop.r)
    mx = (entry[0] + exit_[0]) / 2.0
    my = (entry[1] + exit_[1]) / 2.0
    a0 = atan2(entry[1] - my, entry[0] - mx)
    a1 = atan2(apex[1] - my, apex[0] - mx)
    a2 = atan2(exit_[1] - my, exit_[0] - mx)
    forward = (a2 - a0) % (2.0 * pi)
    mid = (a1 - a0) % (2.0 * pi)
    sweep = forward if mid <= forward + 1e-6 else forward - 2.0 * pi
    points: list[Point] = []
    count = max(steps, 2)
    for step in range(count + 1):
        ang = a0 + sweep * step / count
        points.append(Point(x=mx + hop.r * cos(ang), y=my + hop.r * sin(ang)))
    return points


def _split_gaps(pts: list[Point], gaps: list[_Gap]) -> list[list[Point]]:
    if not gaps or len(pts) < 2:
        return [list(pts)]
    subs: list[list[Point]] = []
    current: list[Point] = [pts[0]]

    def _emit() -> None:
        nonlocal current
        if len(current) >= 2 and hypot(current[0].x - current[-1].x, current[0].y - current[-1].y) > 0.3:
            subs.append(current)
        current = []

    for start, end in zip(pts, pts[1:]):
        length = hypot(end.x - start.x, end.y - start.y)
        if length < 1e-6:
            continue
        intervals: list[tuple[float, float]] = []
        for gap in gaps:
            proj = _project(gap.x, gap.y, start, end)
            if proj is None:
                continue
            half_t = gap.half / length
            lo, hi = proj - half_t, proj + half_t
            if hi <= 0.0 or lo >= 1.0:
                continue
            intervals.append((max(lo, 0.0), min(hi, 1.0)))
        if not intervals:
            current.append(end)
            continue
        intervals.sort()
        merged: list[list[float]] = [[intervals[0][0], intervals[0][1]]]
        for lo, hi in intervals[1:]:
            if lo <= merged[-1][1] + 1e-4:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        cursor = 0.0

        def _at(param: float) -> Point:
            return Point(
                x=start.x + (end.x - start.x) * param,
                y=start.y + (end.y - start.y) * param,
            )

        for lo, hi in merged:
            if (lo - cursor) * length >= 0.35:
                current.append(_at(lo))
                _emit()
            current = [_at(hi)]
            cursor = hi
        if (1.0 - cursor) * length >= 0.35:
            current.append(end)
    _emit()
    return subs or [list(pts)]


def _segments(subs: list[list[Point]]) -> list[tuple[Point, Point]]:
    found: list[tuple[Point, Point]] = []
    for pts in subs:
        found.extend((a, b) for a, b in zip(pts, pts[1:]) if hypot(b.x - a.x, b.y - a.y) > 0.2)
    return found


def _same_net(left: Wire, right: Wire) -> bool:
    number_l = str(left.number or "").strip()
    number_r = str(right.number or "").strip()
    if number_l and number_l == number_r:
        return True
    net_l = str(left.net or "").strip()
    net_r = str(right.net or "").strip()
    return bool(net_l and net_l == net_r)


def _unit(a: Point, b: Point) -> tuple[float, float, float]:
    dx, dy = b.x - a.x, b.y - a.y
    length = hypot(dx, dy)
    if length < 1e-9:
        return 0.0, 0.0, 0.0
    return dx / length, dy / length, length


def _proper_cross(
    a: Point, b: Point, c: Point, d: Point, end_pad: float = _END_PAD
) -> tuple[float, float] | None:
    rx, ry = b.x - a.x, b.y - a.y
    sx, sy = d.x - c.x, d.y - c.y
    den = rx * sy - ry * sx
    if abs(den) < 1e-8:
        return None
    qpx, qpy = c.x - a.x, c.y - a.y
    tee = (qpx * sy - qpy * sx) / den
    you = (qpx * ry - qpy * rx) / den
    if not (0.0 < tee < 1.0 and 0.0 < you < 1.0):
        return None
    ix, iy = a.x + tee * rx, a.y + tee * ry
    if min(hypot(ix - a.x, iy - a.y), hypot(ix - b.x, iy - b.y)) <= end_pad:
        return None
    if min(hypot(ix - c.x, iy - c.y), hypot(ix - d.x, iy - d.y)) <= end_pad:
        return None
    return ix, iy


def _project(px: float, py: float, a: Point, b: Point, tol: float = 0.4) -> float | None:
    dx, dy = b.x - a.x, b.y - a.y
    length2 = dx * dx + dy * dy
    if length2 < 1e-9:
        return None
    tee = ((px - a.x) * dx + (py - a.y) * dy) / length2
    if tee < -0.02 or tee > 1.02:
        return None
    tee = min(1.0, max(0.0, tee))
    qx, qy = a.x + tee * dx, a.y + tee * dy
    if hypot(px - qx, py - qy) > tol:
        return None
    return tee


def _collinear_overlap(a: Point, b: Point, c: Point, d: Point) -> float:
    if abs(a.y - b.y) <= 0.4 and abs(c.y - d.y) <= 0.4 and abs(a.y - c.y) <= 0.4:
        lo = max(min(a.x, b.x), min(c.x, d.x))
        hi = min(max(a.x, b.x), max(c.x, d.x))
        return hi - lo
    if abs(a.x - b.x) <= 0.4 and abs(c.x - d.x) <= 0.4 and abs(a.x - c.x) <= 0.4:
        lo = max(min(a.y, b.y), min(c.y, d.y))
        hi = min(max(a.y, b.y), max(c.y, d.y))
        return hi - lo
    return 0.0
