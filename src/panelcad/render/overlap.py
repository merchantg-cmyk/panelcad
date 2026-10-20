"""Drop a wire stroke that lies on a longer run of another wire.

A reversing swap joins the forward drop at a tee. The router still owns the
pin, so the swap used to paint its own color down the drop. The longer drop
keeps the ink. The swap stops where it meets that drop.
"""

from __future__ import annotations

from math import hypot


def ink_polylines(routes: dict) -> dict[str, list[list[tuple[float, float]]]]:
    """wire id → strokes to draw. A covered tail is omitted."""

    segments = _segments(routes)
    endpoints = _endpoints(routes)
    out: dict[str, list[list[tuple[float, float]]]] = {}
    for wid, pts in routes.items():
        xy = [_xy(point) for point in pts]
        runs: list[list[tuple[float, float]]] = []
        current: list[tuple[float, float]] = []
        for index, (a, b) in enumerate(zip(xy, xy[1:])):
            kept = _uncovered(wid, a, b, segments, endpoints)
            if not kept:
                if len(current) >= 2:
                    runs.append(current)
                current = []
                continue
            for piece in kept:
                if not current:
                    current = [piece[0], piece[1]]
                    continue
                if hypot(current[-1][0] - piece[0][0], current[-1][1] - piece[0][1]) <= 0.15:
                    current.append(piece[1])
                else:
                    if len(current) >= 2:
                        runs.append(current)
                    current = [piece[0], piece[1]]
        if len(current) >= 2:
            runs.append(current)
        out[str(wid)] = runs
    return out


def _segments(routes: dict) -> list[tuple[str, float, float, float, float, float]]:
    found = []
    for wid, pts in routes.items():
        xy = [_xy(point) for point in pts]
        for a, b in zip(xy, xy[1:]):
            length = hypot(b[0] - a[0], b[1] - a[1])
            if length < 0.35:
                continue
            found.append((str(wid), a[0], a[1], b[0], b[1], length))
    return found


def _endpoints(routes: dict) -> dict[str, tuple[tuple[float, float], tuple[float, float]]]:
    found = {}
    for wid, pts in routes.items():
        xy = [_xy(point) for point in pts]
        if len(xy) >= 2:
            found[str(wid)] = (xy[0], xy[-1])
    return found


def _at_end(point: tuple[float, float], ends: tuple[tuple[float, float], tuple[float, float]] | None) -> bool:
    if ends is None:
        return False
    return any(hypot(point[0] - end[0], point[1] - end[1]) <= 0.45 for end in ends)


def _uncovered(
    wid: str,
    a: tuple[float, float],
    b: tuple[float, float],
    segments: list[tuple[str, float, float, float, float, float]],
    endpoints: dict[str, tuple[tuple[float, float], tuple[float, float]]] | None = None,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    length = hypot(b[0] - a[0], b[1] - a[1])
    if length < 0.35:
        return []
    vertical = abs(a[0] - b[0]) <= 0.35
    horizontal = abs(a[1] - b[1]) <= 0.35
    if not vertical and not horizontal:
        return [(a, b)]
    if vertical:
        lo, hi = (a[1], b[1]) if a[1] <= b[1] else (b[1], a[1])
        axis = (a[0] + b[0]) * 0.5
    else:
        lo, hi = (a[0], b[0]) if a[0] <= b[0] else (b[0], a[0])
        axis = (a[1] + b[1]) * 0.5
    covers: list[tuple[float, float]] = []
    for oid, x1, y1, x2, y2, other_len in segments:
        # The longer run keeps the ink. A few tenths still counts: the swap
        # and the drop differ by less than a stroke, and the short one was
        # repainting the drop in the other phase color.
        if oid == wid or other_len <= length + 0.05:
            continue
        # A pin lead belongs to the wire that ends there. A longer wire that
        # only passes that pin does not erase it. The lead is a fraction of
        # the longer run, not the whole drop.
        ends = (endpoints or {}).get(wid)
        other_ends = (endpoints or {}).get(oid)
        if (
            length <= other_len * 0.35
            and (_at_end(a, ends) or _at_end(b, ends))
            and not (_at_end(a, other_ends) or _at_end(b, other_ends))
        ):
            continue
        if vertical:
            if abs(x1 - x2) > 0.35 or abs(((x1 + x2) * 0.5) - axis) > 0.35:
                continue
            clo, chi = (y1, y2) if y1 <= y2 else (y2, y1)
        else:
            if abs(y1 - y2) > 0.35 or abs(((y1 + y2) * 0.5) - axis) > 0.35:
                continue
            clo, chi = (x1, x2) if x1 <= x2 else (x2, x1)
        left, right = max(lo, clo), min(hi, chi)
        if right - left > 0.3:
            covers.append((left, right))
    spans = _subtract(lo, hi, covers)
    pieces = []
    for start, end in spans:
        if end - start < 0.45:
            continue
        if vertical:
            y0, y1 = (start, end) if a[1] <= b[1] else (end, start)
            pieces.append(((axis, y0), (axis, y1)))
        else:
            x0, x1 = (start, end) if a[0] <= b[0] else (end, start)
            pieces.append(((x0, axis), (x1, axis)))
    return pieces


def _subtract(lo: float, hi: float, covers: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not covers:
        return [(lo, hi)]
    merged: list[list[float]] = []
    for start, end in sorted(covers):
        if merged and start <= merged[-1][1] + 0.05:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    spans: list[tuple[float, float]] = []
    cursor = lo
    for start, end in merged:
        if start > cursor + 0.05:
            spans.append((cursor, min(start, hi)))
        cursor = max(cursor, end)
        if cursor >= hi:
            break
    if cursor < hi - 0.05:
        spans.append((cursor, hi))
    return spans


def _xy(point: object) -> tuple[float, float]:
    if hasattr(point, "x"):
        return (float(point.x), float(point.y))
    pair = point  # type: ignore[index]
    return (float(pair[0]), float(pair[1]))
