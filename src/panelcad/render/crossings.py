"""Draw unavoidable wire crossings as a hop, with no junction dot.

Geometry checks still use the stored route. Only the stroke is replaced.
The horizontal jumps over the vertical. A tee at a pin stays straight.
"""

from __future__ import annotations

from math import cos, hypot, pi, sin

HOP_R = 1.05
_END_KEEP = 2.0
_MIN_SEG = 2.8
_MIN_R = 0.48


def hop_routes(routes: dict[str, list]) -> dict[str, list[tuple[float, float]]]:
    """wire id → polyline. Accepts Point or (x, y). Verticals are not hopped."""

    polylines = {wid: [_xy(p) for p in pts] for wid, pts in routes.items()}
    verticals: list[tuple[str, tuple[float, float], tuple[float, float]]] = []
    horizontals: list[tuple[float, float, float]] = []
    for wid, pts in polylines.items():
        for a, b in zip(pts, pts[1:]):
            if _vertical(a, b):
                verticals.append((wid, a, b))
            elif _horizontal(a, b):
                horizontals.append(((a[1] + b[1]) * 0.5, min(a[0], b[0]), max(a[0], b[0])))
    return {wid: _hop_one(wid, pts, verticals, horizontals) for wid, pts in polylines.items()}


def _xy(point: object) -> tuple[float, float]:
    if hasattr(point, "x"):
        return (float(point.x), float(point.y))
    pair = point  # type: ignore[index]
    return (float(pair[0]), float(pair[1]))


def _vertical(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return abs(a[0] - b[0]) <= 0.35 and abs(a[1] - b[1]) > 0.35


def _horizontal(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return abs(a[1] - b[1]) <= 0.35 and abs(a[0] - b[0]) > 0.35


def _hop_one(
    wid: str,
    pts: list[tuple[float, float]],
    verticals: list[tuple[str, tuple[float, float], tuple[float, float]]],
    horizontals: list[tuple[float, float, float]],
) -> list[tuple[float, float]]:
    if len(pts) < 2:
        return list(pts)
    built: list[tuple[float, float]] = [pts[0]]
    for a, b in zip(pts, pts[1:]):
        if not _horizontal(a, b) or hypot(b[0] - a[0], b[1] - a[1]) < _MIN_SEG:
            built.append(b)
            continue
        y = (a[1] + b[1]) * 0.5
        direction = 1.0 if b[0] >= a[0] else -1.0
        hits: list[float] = []
        for other, va, vb in verticals:
            if other == wid:
                continue
            hit = _cross(a, b, va, vb)
            if hit is not None:
                hits.append(hit)
        hits.sort(key=lambda x: (x - a[0]) * direction)
        kept: list[float] = []
        for x in hits:
            radius = _radius(y, x, horizontals)
            if kept and abs(x - kept[-1]) < 2.3 * radius:
                continue
            start_x = x - direction * radius
            end_x = x + direction * radius
            if not _on_span(start_x, a[0], b[0]) or not _on_span(end_x, a[0], b[0]):
                continue
            built.append((start_x, y))
            built.extend(_arc(x, y, radius, direction))
            built.append((end_x, y))
            kept.append(x)
        built.append(b)
    return _dedupe(built)


def _on_span(x: float, a: float, b: float) -> bool:
    lo, hi = (a, b) if a <= b else (b, a)
    return lo - 1e-6 <= x <= hi + 1e-6


def _cross(
    ha: tuple[float, float],
    hb: tuple[float, float],
    va: tuple[float, float],
    vb: tuple[float, float],
) -> float | None:
    """X of a crossing that is clear of both segment ends. None for a tee."""

    x = (va[0] + vb[0]) * 0.5
    y = (ha[1] + hb[1]) * 0.5
    if min(abs(x - ha[0]), abs(x - hb[0])) < _END_KEEP:
        return None
    if min(abs(y - va[1]), abs(y - vb[1])) < _END_KEEP:
        return None
    if not (min(ha[0], hb[0]) < x < max(ha[0], hb[0])):
        return None
    if not (min(va[1], vb[1]) < y < max(va[1], vb[1])):
        return None
    return x


def _radius(y: float, x: float, horizontals: list[tuple[float, float, float]]) -> float:
    """Stay under the next lane above this run so two hops do not pile up."""

    room = HOP_R
    for hy, x0, x1 in horizontals:
        if hy >= y - 0.3:
            continue
        if x1 < x - 1.4 or x0 > x + 1.4:
            continue
        room = min(room, y - hy - 0.35)
    return max(_MIN_R, min(HOP_R, room))


def _arc(cx: float, cy: float, radius: float, direction: float) -> list[tuple[float, float]]:
    """Three points of a semicircle bulging up the page (decreasing y)."""

    fracs = (0.75, 0.5, 0.25) if direction > 0 else (0.25, 0.5, 0.75)
    return [
        (cx + radius * cos(pi * frac), cy - radius * sin(pi * frac))
        for frac in fracs
    ]


def _dedupe(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for point in pts:
        if out and hypot(point[0] - out[-1][0], point[1] - out[-1][1]) < 0.05:
            continue
        out.append(point)
    return out
