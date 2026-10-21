"""Orthogonal wire routing that goes around symbols instead of through them."""

from __future__ import annotations

import heapq
from typing import Iterable

from panelcad.core.geometry import Point, Side, manhattan_route, rotate_point

GRID = 2.5
PAD = 2.5
STUB = 6.0
TURN = 2.0
CHANNEL_PAD = 4.0

Rect = tuple[float, float, float, float]


def component_aabb(x: float, y: float, width: float, height: float, rotation: int = 0, mirror: bool = False) -> Rect:
    """World AABB of a symbol rectangle [0,width]×[0,height] at (x,y)."""
    xs: list[float] = []
    ys: list[float] = []
    for px, py in ((0.0, 0.0), (width, 0.0), (width, height), (0.0, height)):
        if mirror:
            px = -px
        rx, ry = rotate_point(px, py, rotation)
        xs.append(x + rx)
        ys.append(y + ry)
    return (min(xs), min(ys), max(xs), max(ys))


def expand_rect(rect: Rect, pad: float) -> Rect:
    x0, y0, x1, y1 = rect
    return (x0 - pad, y0 - pad, x1 + pad, y1 + pad)


def _segment_hits_rect(x0: float, y0: float, x1: float, y1: float, rect: Rect) -> bool:
    rx0, ry0, rx1, ry1 = rect
    if rx1 < rx0:
        rx0, rx1 = rx1, rx0
    if ry1 < ry0:
        ry0, ry1 = ry1, ry0
    eps = 0.15
    if abs(y0 - y1) <= eps:
        y = y0
        if y < ry0 - eps or y > ry1 + eps:
            return False
        a, b = (x0, x1) if x0 <= x1 else (x1, x0)
        return not (b <= rx0 + eps or a >= rx1 - eps)
    if abs(x0 - x1) <= eps:
        x = x0
        if x < rx0 - eps or x > rx1 + eps:
            return False
        a, b = (y0, y1) if y0 <= y1 else (y1, y0)
        return not (b <= ry0 + eps or a >= ry1 - eps)
    return False


def _is_stub_seg(a: Point, b: Point) -> bool:
    return abs(a.x - b.x) + abs(a.y - b.y) <= STUB + 1.5


def path_as_obstacles(
    points: list[Point],
    *,
    half: float = 1.4,
    skip_stubs: bool = True,
) -> list[Rect]:
    """Fatten path segments into thin rectangles so later wires can miss them."""

    if len(points) < 2:
        return []
    segs = list(zip(points, points[1:]))
    if skip_stubs and len(segs) >= 3:
        if _is_stub_seg(*segs[0]):
            segs = segs[1:]
        if segs and _is_stub_seg(*segs[-1]):
            segs = segs[:-1]
    rects: list[Rect] = []
    for a, b in segs:
        if abs(a.y - b.y) <= 0.2:
            x0, x1 = (a.x, b.x) if a.x <= b.x else (b.x, a.x)
            rects.append((x0, a.y - half, x1, a.y + half))
        elif abs(a.x - b.x) <= 0.2:
            y0, y1 = (a.y, b.y) if a.y <= b.y else (b.y, a.y)
            rects.append((a.x - half, y0, a.x + half, y1))
        else:
            x0, x1 = (a.x, b.x) if a.x <= b.x else (b.x, a.x)
            y0, y1 = (a.y, b.y) if a.y <= b.y else (b.y, a.y)
            rects.append((x0 - half, y0 - half, x1 + half, y1 + half))
    return rects


ALIGN = 0.75
MIN_RUN = 8.0
SEP = GRID
# Closer than this and a different number sits on the stroke. One grid step (2.5 mm) stays.
LANE_GAP = 2.0


def _seg_kind(a: Point, b: Point) -> str:
    if abs(a.y - b.y) <= ALIGN:
        return "h"
    if abs(a.x - b.x) <= ALIGN:
        return "v"
    return ""


def _runs(
    points: list[Point], *, skip_stubs: bool = True, min_run: float = MIN_RUN
) -> list[tuple[str, float, float, float, int]]:
    """Long axis-aligned runs: (kind, channel, lo, hi, seg_index)."""

    out: list[tuple[str, float, float, float, int]] = []
    n = len(points)
    for i in range(n - 1):
        a, b = points[i], points[i + 1]
        kind = _seg_kind(a, b)
        if not kind:
            continue
        if skip_stubs and n >= 3 and (i == 0 or i == n - 2) and _is_stub_seg(a, b):
            continue
        if kind == "h":
            lo, hi = (a.x, b.x) if a.x <= b.x else (b.x, a.x)
            ch = (a.y + b.y) / 2.0
        else:
            lo, hi = (a.y, b.y) if a.y <= b.y else (b.y, a.y)
            ch = (a.x + b.x) / 2.0
        if hi - lo >= min_run:
            out.append((kind, ch, lo, hi, i))
    return out


def coincident_run_length(p1: list[Point], p2: list[Point]) -> float:
    """How many millimetres two paths share a collinear stacked run. Crossings do not count."""

    total = 0.0
    for kind, ch, lo, hi, _i in _runs(p1):
        for okind, och, olo, ohi, _j in _runs(p2):
            if kind != okind or abs(ch - och) > ALIGN:
                continue
            total += max(0.0, min(hi, ohi) - max(lo, olo))
    return total


def needs_separation(points: list[Point], others: list[list[Point]]) -> bool:
    """True when *points* sits on another number, or runs in its lane for a full run."""

    if not others:
        return False
    if shared_lane_length(points, others, min_len=0.5) > 0.5:
        return True
    return any(_wide_overlap(points, other) > 0.0 for other in others)


def shared_lane_length(points: list[Point], others: list[list[Point]], *, min_len: float = 0.5) -> float:
    """Collinear overlap with *others* on the same channel. Any run longer than *min_len* counts."""

    total = 0.0
    for kind, ch, lo, hi, _i in _runs(points, skip_stubs=False, min_run=min_len):
        for other in others:
            for okind, och, olo, ohi, _j in _runs(other, skip_stubs=False, min_run=min_len):
                if kind != okind or abs(ch - och) > ALIGN:
                    continue
                span = min(hi, ohi) - max(lo, olo)
                if span > min_len:
                    total += span
    return total


def _copy_pts(points: list[Point]) -> list[Point]:
    return [Point(x=p.x, y=p.y) for p in points]


def _dedupe_pts(points: list[Point]) -> list[Point]:
    if not points:
        return points
    out = [Point(x=points[0].x, y=points[0].y)]
    for p in points[1:]:
        prev = out[-1]
        if abs(prev.x - p.x) < 1e-6 and abs(prev.y - p.y) < 1e-6:
            continue
        out.append(Point(x=p.x, y=p.y))
    return out


def _restore_ortho(points: list[Point]) -> list[Point]:
    """Keep a pulled path orthogonal without moving the pin endpoints."""

    if len(points) < 2:
        return points
    last = points[-1]
    out = [Point(x=points[0].x, y=points[0].y)]
    for p in points[1:]:
        prev = out[-1]
        if abs(prev.x - p.x) < 1e-6 and abs(prev.y - p.y) < 1e-6:
            continue
        if abs(prev.x - p.x) < 1e-6 or abs(prev.y - p.y) < 1e-6:
            out.append(Point(x=p.x, y=p.y))
            continue
        arriving = abs(p.x - last.x) < 1e-6 and abs(p.y - last.y) < 1e-6
        if arriving:
            out.append(Point(x=prev.x, y=p.y))
            out.append(Point(x=p.x, y=p.y))
        else:
            out.append(Point(x=p.x, y=prev.y))
            out.append(Point(x=p.x, y=p.y))
    return _dedupe_pts(out)


def _nudge_seg(points: list[Point], i: int, kind: str, delta: float) -> list[Point] | None:
    n = len(points)
    if i < 0 or i >= n - 1:
        return None
    out = _copy_pts(points)
    a, b = out[i], out[i + 1]
    if kind == "h":
        ny = a.y + delta
        a.y = ny
        b.y = ny
    else:
        nx = a.x + delta
        a.x = nx
        b.x = nx
    # Pin ends stay put.
    out[0] = Point(x=points[0].x, y=points[0].y)
    out[-1] = Point(x=points[-1].x, y=points[-1].y)
    return _restore_ortho(out)


def nearest_run(points: list[Point], x: float, y: float, max_dist: float = 4.0) -> tuple[int, str] | None:
    """Index and kind of the closest H/V segment, or None if farther than max_dist."""

    best: tuple[int, str] | None = None
    best_d = max_dist
    for i, (a, b) in enumerate(zip(points, points[1:])):
        kind = _seg_kind(a, b)
        if not kind:
            continue
        d = _point_seg_dist(x, y, a, b)
        if d < best_d:
            best_d = d
            best = (i, kind)
    return best


def _point_seg_dist(x: float, y: float, a: Point, b: Point) -> float:
    dx, dy = b.x - a.x, b.y - a.y
    len2 = dx * dx + dy * dy
    if len2 < 1e-9:
        return ((x - a.x) ** 2 + (y - a.y) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((x - a.x) * dx + (y - a.y) * dy) / len2))
    px, py = a.x + t * dx, a.y + t * dy
    return ((x - px) ** 2 + (y - py) ** 2) ** 0.5


def set_run_channel(points: list[Point], i: int, kind: str, channel: float) -> list[Point]:
    """Pull one orthogonal run onto a new X (vertical) or Y (horizontal) channel."""

    if i < 0 or i >= len(points) - 1:
        return _copy_pts(points)
    a, b = points[i], points[i + 1]
    if kind == "h":
        cur = (a.y + b.y) / 2.0
    else:
        cur = (a.x + b.x) / 2.0
    out = _nudge_seg(points, i, kind, channel - cur)
    return out if out is not None else _copy_pts(points)


def _is_spike(a: Point, b: Point, c: Point) -> bool:
    """True when B runs past A–C and the stroke comes back along itself.

    A one-grid stub (2.5 mm) is left. The wire number sits on that stub. A
    longer hook, such as a 6 mm exit that passes the next pin, is removed.
    """

    col_h = abs(a.y - b.y) <= 0.05 and abs(b.y - c.y) <= 0.05
    col_v = abs(a.x - b.x) <= 0.05 and abs(b.x - c.x) <= 0.05
    if col_h:
        lo, hi = (a.x, c.x) if a.x <= c.x else (c.x, a.x)
        if b.x < lo:
            extra = lo - b.x
        elif b.x > hi:
            extra = b.x - hi
        else:
            return False
        return extra >= 2.0
    if col_v:
        lo, hi = (a.y, c.y) if a.y <= c.y else (c.y, a.y)
        if b.y < lo:
            extra = lo - b.y
        elif b.y > hi:
            extra = b.y - hi
        else:
            return False
        return extra >= 2.0
    return False


def drop_retraces(points: list[Point]) -> list[Point]:
    """Remove an out-and-back along one line. The lead was being stroked twice."""

    pts = _dedupe_pts(points)
    changed = True
    while changed and len(pts) >= 3:
        changed = False
        out = [pts[0]]
        index = 1
        while index < len(pts) - 1:
            if _is_spike(out[-1], pts[index], pts[index + 1]):
                changed = True
                index += 1
                continue
            out.append(pts[index])
            index += 1
        out.append(pts[-1])
        pts = _dedupe_pts(out)
    return pts


def _covered_runs(points: list[Point]) -> tuple:
    """Union of horizontal and vertical ink, so a there-and-back compares equal."""

    horizontal: dict[float, list[list[float]]] = {}
    vertical: dict[float, list[list[float]]] = {}
    for start, end in zip(points, points[1:]):
        if abs(start.y - end.y) <= 0.05:
            key = round((start.y + end.y) / 2.0, 2)
            lo, hi = sorted((start.x, end.x))
            horizontal.setdefault(key, []).append([lo, hi])
        elif abs(start.x - end.x) <= 0.05:
            key = round((start.x + end.x) / 2.0, 2)
            lo, hi = sorted((start.y, end.y))
            vertical.setdefault(key, []).append([lo, hi])

    def merge(groups: dict[float, list[list[float]]]) -> tuple:
        found = []
        for key, spans in sorted(groups.items()):
            spans.sort()
            merged = [spans[0]]
            for lo, hi in spans[1:]:
                if lo <= merged[-1][1] + 0.05:
                    merged[-1][1] = max(merged[-1][1], hi)
                else:
                    merged.append([lo, hi])
            found.append((key, tuple((round(lo, 2), round(hi, 2)) for lo, hi in merged)))
        return tuple(found)

    return (merge(horizontal), merge(vertical))


def present_route(points: list[Point]) -> list[Point]:
    """Drop a hook that draws a second stroke. Keep a backtrack that adds no ink.

    A lead that runs past the next pin and returns on the same line covers the
    same pixels either way. Removing those vertices moves the wire number.
    A hook that leaves the column does draw a second stroke, and that one goes.
    """

    cleaned = drop_retraces(points)
    if _covered_runs(points) == _covered_runs(cleaned):
        return _dedupe_pts(points)
    return cleaned


def _wide_overlap(p1: list[Point], p2: list[Point]) -> float:
    """Shared length of different conductors that are closer than one lane."""

    total = 0.0
    for kind, ch, lo, hi, _i in _runs(p1, min_run=2.0):
        for okind, och, olo, ohi, _j in _runs(p2, min_run=2.0):
            if kind != okind:
                continue
            gap = abs(ch - och)
            if gap >= LANE_GAP:
                continue
            overlap = min(hi, ohi) - max(lo, olo)
            if overlap >= MIN_RUN:
                total += overlap
    return total


def _crowd_cost(points: list[Point], same: list[list[Point]], other: list[list[Point]]) -> float:
    tight = sum(coincident_run_length(points, path) for path in same)
    tight += sum(coincident_run_length(points, path) for path in other)
    wide = sum(_wide_overlap(points, path) for path in other)
    return tight + wide


def _crowd_segment(
    points: list[Point], same: list[list[Point]], other: list[list[Point]]
) -> tuple[str, int] | None:
    found: tuple[float, str, int] | None = None

    def take(paths: list[list[Point]], *, wide: bool) -> None:
        nonlocal found
        min_run = 2.0 if wide else MIN_RUN
        for kind, ch, lo, hi, index in _runs(points, min_run=min_run):
            for path in paths:
                for okind, och, olo, ohi, _j in _runs(path, min_run=min_run):
                    if kind != okind:
                        continue
                    gap = abs(ch - och)
                    limit = LANE_GAP if wide else ALIGN + 1e-6
                    if gap >= limit:
                        continue
                    overlap = min(hi, ohi) - max(lo, olo)
                    # A different number on this channel counts at half a millimetre.
                    # A long near-miss still has to be a real run. The same number may share a trunk.
                    need = 0.5 if wide and gap <= ALIGN else MIN_RUN
                    if overlap <= need:
                        continue
                    if found is None or overlap > found[0]:
                        found = (overlap, kind, index)

    take(same, wide=False)
    take(other, wide=True)
    if found is None:
        return None
    return found[1], found[2]


def pull_path(
    points: list[Point],
    prior: list[list[Point]],
    obstacles: Iterable[Rect] | None = None,
    *,
    keys: list[str] | None = None,
    own: str = "",
) -> list[Point]:
    """Offset runs that sit on, or in the lane of, an earlier conductor.

    The same wire number may share a trunk. A different number gets its own
    lane, at least ``LANE_GAP`` away, so the two can be read apart. Crossings stay.
    """

    walls = list(obstacles or [])
    same: list[list[Point]] = []
    other: list[list[Point]] = []
    for index, path in enumerate(prior):
        key = keys[index] if keys is not None and index < len(keys) else ""
        if own and key == own:
            same.append(path)
        else:
            other.append(path)
    pts = present_route(points)
    if not same and not other:
        return pts
    for _ in range(16):
        hit = _crowd_segment(pts, same, other)
        if hit is None:
            return pts
        kind, index = hit
        before = _crowd_cost(pts, same, other)
        before_share = shared_lane_length(pts, other, min_len=0.5)
        best: tuple[int, list[Point], float] | None = None
        for step in range(1, 10):
            for sign in (1, -1):
                cand = _nudge_seg(pts, index, kind, sign * step * GRID)
                if cand is None or len(cand) < 2:
                    continue
                # Any shared channel longer than half a millimetre still counts.
                # Landing on a different number does not clear the one we left.
                share = shared_lane_length(cand, other, min_len=0.5)
                if share > before_share - 0.05:
                    continue
                if walls and path_hits_obstacles(cand, walls, skip_ends=True):
                    continue
                cost = _crowd_cost(cand, same, other) + share
                if cost >= before + before_share - 0.5:
                    continue
                if best is None or cost < best[2] - 0.5 or (
                    cost <= best[2] + 0.5 and step < best[0]
                ):
                    best = (step, cand, cost)
            if best is not None and best[2] <= 0.5:
                break
        if best is None:
            return pts
        pts = best[1]
        if best[2] <= 0.5:
            return pts
    return pts


def _trim_end(points: list[Point], dist: float) -> list[Point]:
    if len(points) < 2 or dist <= 0:
        return points
    left = dist
    index = 0
    while index < len(points) - 1 and left > 1e-6:
        start, end = points[index], points[index + 1]
        span = abs(end.x - start.x) + abs(end.y - start.y)
        if span <= left + 1e-6:
            left -= span
            index += 1
            continue
        scale = left / span if span else 0.0
        cut = Point(
            x=start.x + (end.x - start.x) * scale,
            y=start.y + (end.y - start.y) * scale,
        )
        return [cut, *points[index + 1 :]]
    return points[index:]


def _trim_ends(points: list[Point], dist: float) -> list[Point]:
    trimmed = _trim_end(points, dist)
    flipped = _trim_end(list(reversed(trimmed)), dist)
    flipped.reverse()
    return flipped


def path_hits_obstacles(
    points: list[Point],
    obstacles: Iterable[Rect],
    *,
    skip_ends: bool = True,
    end_clearance: float = 0.0,
) -> bool:
    if end_clearance > 0:
        points = _trim_ends(points, end_clearance)
    if len(points) < 2:
        return False
    segs = list(zip(points, points[1:]))
    if skip_ends and len(segs) >= 3:
        # Only the short pin stubs are exempt — a long first run through a body is a hit.
        if _is_stub_seg(*segs[0]):
            segs = segs[1:]
        if segs and _is_stub_seg(*segs[-1]):
            segs = segs[:-1]
    for a, b in segs:
        for rect in obstacles:
            if _segment_hits_rect(a.x, a.y, b.x, b.y, rect):
                return True
    return False


def _cell(x: float, y: float) -> tuple[int, int]:
    return (int(round(x / GRID)), int(round(y / GRID)))


def _xy(i: int, j: int) -> tuple[float, float]:
    return (i * GRID, j * GRID)


def _inside(x: float, y: float, rect: Rect) -> bool:
    x0, y0, x1, y1 = rect
    return x0 <= x <= x1 and y0 <= y <= y1


def _simplify(points: list[Point]) -> list[Point]:
    if len(points) < 3:
        return points
    out: list[Point] = [points[0]]
    for p in points[1:]:
        if abs(out[-1].x - p.x) < 1e-6 and abs(out[-1].y - p.y) < 1e-6:
            continue
        out.append(p)
    simp: list[Point] = [out[0]]
    for i in range(1, len(out) - 1):
        a, b, c = simp[-1], out[i], out[i + 1]
        col_h = abs(a.y - b.y) < 1e-6 and abs(b.y - c.y) < 1e-6
        col_v = abs(a.x - b.x) < 1e-6 and abs(b.x - c.x) < 1e-6
        if col_h or col_v:
            continue
        simp.append(b)
    simp.append(out[-1])
    # Drop 1-cell diagonal A* wobbles (both axes move by ≤ one grid).
    compact: list[Point] = [simp[0]]
    i = 1
    while i < len(simp) - 1:
        a, b, c = compact[-1], simp[i], simp[i + 1]
        dx, dy = abs(a.x - b.x), abs(a.y - b.y)
        ac_h = abs(a.y - c.y) < 1e-6
        ac_v = abs(a.x - c.x) < 1e-6
        if dx <= GRID + 0.2 and dy <= GRID + 0.2 and dx > 1e-6 and dy > 1e-6 and (ac_h or ac_v):
            i += 1
            continue
        compact.append(b)
        i += 1
    compact.append(simp[-1])
    return compact


def _stub_end(x: float, y: float, side: Side, stub: float) -> tuple[float, float]:
    if side == "left":
        return (x - stub, y)
    if side == "right":
        return (x + stub, y)
    if side == "top":
        return (x, y - stub)
    return (x, y + stub)


def _path_cost(points: list[Point]) -> float:
    """Length plus corners, plus a penalty for running far off the pin-to-pin box."""
    if len(points) < 2:
        return 0.0
    dist = 0.0
    corners = 0
    for i in range(1, len(points)):
        dist += abs(points[i].x - points[i - 1].x) + abs(points[i].y - points[i - 1].y)
        if i >= 2:
            a, b, c = points[i - 2], points[i - 1], points[i]
            col_h = abs(a.y - b.y) < 1e-6 and abs(b.y - c.y) < 1e-6
            col_v = abs(a.x - b.x) < 1e-6 and abs(b.x - c.x) < 1e-6
            if not (col_h or col_v):
                corners += 1
    y0, y1 = points[0].y, points[-1].y
    x0, x1 = points[0].x, points[-1].x
    lo_y, hi_y = min(y0, y1) - 10.0, max(y0, y1) + 10.0
    lo_x, hi_x = min(x0, x1) - 10.0, max(x0, x1) + 10.0
    excursion = 0.0
    for p in points:
        if p.y < lo_y:
            excursion += lo_y - p.y
        elif p.y > hi_y:
            excursion += p.y - hi_y
        if p.x < lo_x:
            excursion += lo_x - p.x
        elif p.x > hi_x:
            excursion += p.x - hi_x
    return dist + corners * 8.0 + excursion * 1.6


def _path_cost_bounded(points: list[Point], bounds: Rect | None) -> float:
    cost = _path_cost(points)
    if bounds is None or len(points) < 2:
        return cost
    top = bounds[1] + 10.0
    bot = bounds[3] - 6.0
    extra = 0.0
    for p in points:
        if p.y < top:
            extra += (top - p.y) * 6.0
        if p.y > bot:
            extra += (p.y - bot) * 6.0
    return cost + extra


def _channel_routes(
    ax: float,
    ay: float,
    a_side: Side,
    bx: float,
    by: float,
    b_side: Side,
    obstacles: list[Rect],
    stub: float,
    bounds: Rect | None = None,
) -> list[list[Point]]:
    """3–4 corner paths that run entirely above / below / left / right of obstacles."""
    if not obstacles:
        return []
    sx, sy = _stub_end(ax, ay, a_side, stub)
    ex, ey = _stub_end(bx, by, b_side, stub)
    # Title block spans the sheet — do not park a channel under it.
    cluster = [r for r in obstacles if (r[2] - r[0]) < 200.0 and (r[3] - r[1]) < 120.0]
    if not cluster:
        cluster = list(obstacles)
    minx = min(r[0] for r in cluster)
    miny = min(r[1] for r in cluster)
    maxx = max(r[2] for r in cluster)
    maxy = max(r[3] for r in cluster)
    pad = CHANNEL_PAD
    channels: list[tuple[str, float]] = [
        ("h", miny - pad),
        ("h", maxy + pad),
        ("v", minx - pad),
        ("v", maxx + pad),
    ]
    out: list[list[Point]] = []
    for kind, pos in channels:
        pos = _clip_channel_pos(kind, pos, bounds)
        if kind == "h":
            pts = [
                Point(x=ax, y=ay),
                Point(x=sx, y=sy),
                Point(x=sx, y=pos),
                Point(x=ex, y=pos),
                Point(x=ex, y=ey),
                Point(x=bx, y=by),
            ]
        else:
            pts = [
                Point(x=ax, y=ay),
                Point(x=sx, y=sy),
                Point(x=pos, y=sy),
                Point(x=pos, y=ey),
                Point(x=ex, y=ey),
                Point(x=bx, y=by),
            ]
        pts = _simplify(pts)
        if not path_hits_obstacles(pts, obstacles):
            out.append(pts)
    return out


def _clip_channel_pos(kind: str, pos: float, bounds: Rect | None) -> float:
    if bounds is None:
        return pos
    x0, y0, x1, y1 = bounds
    if kind == "h":
        return min(max(pos, y0 + 1.0), y1 - 1.0)
    return min(max(pos, x0 + 1.0), x1 - 1.0)


def _local_detours(
    ax: float,
    ay: float,
    a_side: Side,
    bx: float,
    by: float,
    b_side: Side,
    obstacles: list[Rect],
    stub: float,
) -> list[list[Point]]:
    """Walk around only the boxes the naive path actually hits — not the whole sheet."""
    naive = manhattan_route(ax, ay, a_side, bx, by, b_side, stub=stub)
    hits = [r for r in obstacles if path_hits_obstacles(naive, [r])]
    if not hits:
        return []
    sx, sy = _stub_end(ax, ay, a_side, stub)
    ex, ey = _stub_end(bx, by, b_side, stub)
    out: list[list[Point]] = []
    boxes = list(hits)
    if len(hits) > 1:
        boxes.append(
            (
                min(r[0] for r in hits),
                min(r[1] for r in hits),
                max(r[2] for r in hits),
                max(r[3] for r in hits),
            )
        )
    for x0, y0, x1, y1 in boxes:
        pad = CHANNEL_PAD
        options = [
            ("h", y0 - pad),
            ("h", y1 + pad),
            ("v", x0 - pad),
            ("v", x1 + pad),
        ]
        for kind, pos in options:
            if kind == "h":
                pts = [
                    Point(x=ax, y=ay),
                    Point(x=sx, y=sy),
                    Point(x=sx, y=pos),
                    Point(x=ex, y=pos),
                    Point(x=ex, y=ey),
                    Point(x=bx, y=by),
                ]
            else:
                pts = [
                    Point(x=ax, y=ay),
                    Point(x=sx, y=sy),
                    Point(x=pos, y=sy),
                    Point(x=pos, y=ey),
                    Point(x=ex, y=ey),
                    Point(x=bx, y=by),
                ]
            pts = _simplify(pts)
            if not path_hits_obstacles(pts, obstacles):
                out.append(pts)
    return out


def orthogonal_interior(
    ax: float,
    ay: float,
    bx: float,
    by: float,
    points: list[Point],
) -> list[Point]:
    """Interior waypoints that stay on elbows.

    A single averaged midpoint is the old diagonal fallback. Drop it so the
    autorouter can run. Any other diagonal corner becomes an elbow.
    """

    if len(points) == 1:
        p = points[0]
        if abs(p.x - (ax + bx) / 2.0) <= 1.5 and abs(p.y - (ay + by) / 2.0) <= 1.5:
            return []
    full = [Point(x=ax, y=ay), *points, Point(x=bx, y=by)]
    out: list[Point] = [full[0]]
    for p in full[1:]:
        prev = out[-1]
        if abs(prev.x - p.x) <= 0.6 or abs(prev.y - p.y) <= 0.6:
            out.append(Point(x=p.x, y=p.y))
            continue
        out.append(Point(x=p.x, y=prev.y))
        out.append(Point(x=p.x, y=p.y))
    out = _dedupe_pts(out)
    if len(out) < 3:
        return []
    return out[1:-1]


def _open_stub(x: float, y: float, side: Side, obstacles: list[Rect], stub: float) -> float:
    """Shorten a pin stub that would end inside a symbol the pin is not already in.

    A 6 mm stub out of a fuse whose neighbor is 2 mm away starts inside that
    neighbor, and every detour is then rejected. The wire is drawn through it.
    """

    length = stub
    while length > 1.0:
        sx, sy = _stub_end(x, y, side, length)
        blocked = False
        for rect in obstacles:
            if _inside(x, y, rect):
                continue
            if _inside(sx, sy, rect) or _segment_hits_rect(x, y, sx, sy, rect):
                blocked = True
                break
        if not blocked:
            return length
        length -= 1.0
    return 1.0


def runs_along_lanes(points: list[Point], lanes: list[Rect]) -> bool:
    """True when a run travels inside another conductor's lane, not across it."""

    for start, end in zip(points, points[1:]):
        horizontal = abs(start.y - end.y) <= ALIGN
        vertical = abs(start.x - end.x) <= ALIGN
        if not horizontal and not vertical:
            continue
        if horizontal:
            y = (start.y + end.y) / 2.0
            lo, hi = (start.x, end.x) if start.x <= end.x else (end.x, start.x)
            if hi - lo < MIN_RUN:
                continue
            for rect in lanes:
                if (rect[2] - rect[0]) < (rect[3] - rect[1]):
                    continue
                # The outer edge is a full gap away. Only the inside counts.
                if y <= rect[1] + 0.05 or y >= rect[3] - 0.05:
                    continue
                if min(hi, rect[2]) - max(lo, rect[0]) >= MIN_RUN:
                    return True
        else:
            x = (start.x + end.x) / 2.0
            lo, hi = (start.y, end.y) if start.y <= end.y else (end.y, start.y)
            if hi - lo < MIN_RUN:
                continue
            for rect in lanes:
                if (rect[2] - rect[0]) >= (rect[3] - rect[1]):
                    continue
                if x <= rect[0] + 0.05 or x >= rect[2] - 0.05:
                    continue
                if min(hi, rect[3]) - max(lo, rect[1]) >= MIN_RUN:
                    return True
    return False


def _collapse_collinear(points: list[Point]) -> list[Point]:
    """Drop vertices that do not change direction. A straight lead stays two points."""

    pts = _dedupe_pts(points)
    if len(pts) < 3:
        return pts
    out = [pts[0]]
    for index in range(1, len(pts) - 1):
        prev, cur, nxt = out[-1], pts[index], pts[index + 1]
        same_x = abs(prev.x - cur.x) <= 0.05 and abs(cur.x - nxt.x) <= 0.05
        same_y = abs(prev.y - cur.y) <= 0.05 and abs(cur.y - nxt.y) <= 0.05
        if same_x or same_y:
            continue
        out.append(cur)
    out.append(pts[-1])
    return _dedupe_pts(out)


def _on_page(points: list[Point], bounds: Rect | None) -> bool:
    if bounds is None:
        return True
    x0, y0, x1, y1 = bounds
    pad = 8.0
    return all(x0 - pad <= p.x <= x1 + pad and y0 - pad <= p.y <= y1 + pad for p in points)


def _short_drop_clear(points: list[Point], obstacles: list[Rect], lanes: list[Rect]) -> bool:
    """True when a facing drop only clips the pin pad, not another symbol or lane."""

    if lanes and runs_along_lanes(points, lanes):
        return False
    return not path_hits_obstacles(points, obstacles, skip_ends=True, end_clearance=1.2)


def route_orthogonal(
    ax: float,
    ay: float,
    a_side: Side,
    bx: float,
    by: float,
    b_side: Side,
    obstacles: list[Rect],
    bounds: Rect | None = None,
    stub: float = STUB,
    lanes: list[Rect] | None = None,
    allow_astar: bool = True,
) -> list[Point]:
    """Orthogonal path from pin A to pin B that walks around *obstacles*.

    ``lanes`` are earlier conductors. The path may cross one. It may not travel
    along it.
    """
    lane_boxes = list(lanes or [])
    stub = min(
        _open_stub(ax, ay, a_side, obstacles, stub),
        _open_stub(bx, by, b_side, obstacles, stub),
    )
    facing = (a_side == "bottom" and b_side == "top" and by > ay) or (
        a_side == "top" and b_side == "bottom" and ay > by
    )
    # The symbol pad sticks 0.8 mm past the pin. A straight drop starts on that
    # edge, so the ordinary hit test calls it a body crossing. Trim that pad.
    short_gap = facing and abs(by - ay) < 2.0 * STUB + 0.4

    def clear(pts: list[Point], *, allow_lane: bool = False) -> bool:
        # Same test the geometry check uses: a stub at the pin is exempt, a run through a body is not.
        if path_hits_obstacles(pts, obstacles, skip_ends=True):
            return False
        if not allow_lane and lane_boxes and runs_along_lanes(pts, lane_boxes):
            return False
        return True

    def shown(pts: list[Point]) -> list[Point]:
        cleaned = present_route(pts)
        if cleaned is not pts and path_hits_obstacles(cleaned, obstacles, skip_ends=True):
            return pts
        return cleaned

    raw = manhattan_route(ax, ay, a_side, bx, by, b_side, stub=stub)
    naive = shown(raw)
    if short_gap and _short_drop_clear(naive, obstacles, lane_boxes) and _on_page(naive, bounds):
        # Keep the stub vertices. A bare two-point segment reads as symbol ink to the
        # print importer and merges the wire into the overload pole beside it.
        return _dedupe_pts(naive)
    if clear(naive):
        return naive
    short_stub = min(2.0, stub)
    if short_stub < stub - 0.2:
        short_raw = manhattan_route(ax, ay, a_side, bx, by, b_side, stub=short_stub)
        short = shown(short_raw)
        if clear(short):
            return short
    def on_sheet(pts: list[Point]) -> bool:
        if bounds is None:
            return True
        x0, y0, x1, y1 = bounds
        # A pin stub may stand just outside the working rect. A detour to y=-10 may not.
        pad = 8.0
        return all(x0 - pad <= p.x <= x1 + pad and y0 - pad <= p.y <= y1 + pad for p in pts)

    detours = _local_detours(ax, ay, a_side, bx, by, b_side, obstacles, stub)
    candidates = [pts for pts in detours if clear(pts) and on_sheet(pts)]
    # The grid search is the slow part. The lane repair asks for a detour only.
    if allow_astar:
        routed = _astar(ax, ay, a_side, bx, by, b_side, obstacles, bounds, stub, lanes=lane_boxes)
        if routed and clear(routed) and on_sheet(routed):
            candidates.append(routed)
    if not candidates and lane_boxes and allow_astar:
        # Going around the bundle left the page. Draw the crossing instead.
        return route_orthogonal(
            ax, ay, a_side, bx, by, b_side, obstacles, bounds=bounds, stub=stub, lanes=None
        )
    if not candidates:
        candidates.extend(pts for pts in detours if clear(pts, allow_lane=True) and on_sheet(pts))
    if not candidates:
        candidates.extend(
            pts
            for pts in _channel_routes(ax, ay, a_side, bx, by, b_side, obstacles, stub, bounds=bounds)
            if (clear(pts) or clear(pts, allow_lane=True)) and on_sheet(pts)
        )
    if not candidates:
        return naive
    return shown(min(candidates, key=lambda pts: _path_cost_bounded(pts, bounds)))


def _astar(
    ax: float,
    ay: float,
    a_side: Side,
    bx: float,
    by: float,
    b_side: Side,
    obstacles: list[Rect],
    bounds: Rect | None,
    stub: float,
    lanes: list[Rect] | None = None,
) -> list[Point] | None:
    sx, sy = _stub_end(ax, ay, a_side, stub)
    ex, ey = _stub_end(bx, by, b_side, stub)
    si, sj = _cell(sx, sy)
    ei, ej = _cell(ex, ey)

    if bounds is None:
        xs = [ax, bx, sx, ex]
        ys = [ay, by, sy, ey]
        for x0, y0, x1, y1 in obstacles:
            xs.extend((x0, x1))
            ys.extend((y0, y1))
        bounds = (min(xs) - 20.0, min(ys) - 20.0, max(xs) + 20.0, max(ys) + 20.0)
    i0, j0 = _cell(bounds[0], bounds[1])
    i1, j1 = _cell(bounds[2], bounds[3])
    if i1 < i0:
        i0, i1 = i1, i0
    if j1 < j0:
        j0, j1 = j1, j0
    i0 = min(i0, si, ei) - 6
    i1 = max(i1, si, ei) + 6
    j0 = min(j0, sj, ej) - 6
    j1 = max(j1, sj, ej) + 6

    free = {_cell(ax, ay), _cell(bx, by), (si, sj), (ei, ej)}
    for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1), (2, 0), (-2, 0), (0, 2), (0, -2)):
        free.add((si + di, sj + dj))
        free.add((ei + di, ej + dj))

    lane_boxes = list(lanes or [])

    def blocked(i: int, j: int, di: int = 0, dj: int = 0) -> bool:
        if i < i0 or i > i1 or j < j0 or j > j1:
            return True
        # The pin neighbourhood stays open so the run can arrive. A long
        # stretch of someone else's lane does not.
        if (i, j) in free:
            return False
        x, y = _xy(i, j)
        if any(_inside(x, y, r) for r in obstacles):
            return True
        for rect in lane_boxes:
            if not _inside(x, y, rect):
                continue
            horizontal = (rect[2] - rect[0]) >= (rect[3] - rect[1])
            if horizontal and dj == 0 and di != 0:
                return True
            if not horizontal and di == 0 and dj != 0:
                return True
        return False

    def heur(i: int, j: int) -> float:
        return abs(i - ei) + abs(j - ej)

    heap: list[tuple[float, float, int, int, int | None]] = [(heur(si, sj), 0.0, si, sj, None)]
    came: dict[tuple[int, int], tuple[int, int]] = {}
    best = {(si, sj): 0.0}
    dirs = ((1, 0), (-1, 0), (0, 1), (0, -1))
    found = False
    while heap:
        _f, g, i, j, pdir = heapq.heappop(heap)
        if g > best.get((i, j), 1e9) + 1e-9:
            continue
        if (i, j) == (ei, ej):
            found = True
            break
        for d, (di, dj) in enumerate(dirs):
            ni, nj = i + di, j + dj
            if blocked(ni, nj, di, dj):
                continue
            ng = g + 1.0 + (TURN if pdir is not None and pdir != d else 0.0)
            prev = best.get((ni, nj))
            if prev is not None and ng >= prev - 1e-9:
                continue
            best[(ni, nj)] = ng
            came[(ni, nj)] = (i, j)
            heapq.heappush(heap, (ng + heur(ni, nj), ng, ni, nj, d))

    if not found:
        return None
    cells = [(ei, ej)]
    cur = (ei, ej)
    while cur != (si, sj):
        cur = came[cur]
        cells.append(cur)
    cells.reverse()
    pts = [Point(x=ax, y=ay), Point(x=sx, y=sy)]
    pts.extend(Point(x=x, y=y) for x, y in (_xy(i, j) for i, j in cells))
    pts.extend((Point(x=ex, y=ey), Point(x=bx, y=by)))
    return _simplify(pts)
