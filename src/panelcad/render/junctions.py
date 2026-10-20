"""Filled dots where three or more wire branches meet.

A hop is a crossing and stays bare. A tee — one conductor leaving another,
or three ends on one point — gets a dot. A corner of a single wire does not.
"""

from __future__ import annotations

from math import hypot

_CLUSTER = 0.45
_ON = 0.35


def junction_points(
    routes: dict,
    pins: list[tuple[float, float, str]] | None = None,
) -> list[tuple[float, float]]:
    """World points that should be drawn as junction dots.

    ``pins`` is ``(x, y, component id)``. A tee that lands on a device pin —
    two wires meeting at a right angle, or a 2-terminal symbol tapped off a
    bus — is a dot even though one leg of the T is the symbol itself.
    """

    segments: list[tuple[str, float, float, float, float]] = []
    vertices: list[tuple[str, float, float]] = []
    for wid, pts in routes.items():
        xy = [_xy(point) for point in pts]
        vertices.extend((str(wid), x, y) for x, y in xy)
        for a, b in zip(xy, xy[1:]):
            if hypot(b[0] - a[0], b[1] - a[1]) < 0.2:
                continue
            segments.append((str(wid), a[0], a[1], b[0], b[1]))

    candidates: list[tuple[float, float]] = []
    for wa, x, y in vertices:
        foreign = False
        for wb, x1, y1, x2, y2 in segments:
            if wb == wa:
                continue
            if _point_on(x, y, x1, y1, x2, y2) and _end_gap(x, y, x1, y1, x2, y2) > _ON:
                foreign = True
                break
        if not foreign:
            for wb, xb, yb in vertices:
                if wb != wa and hypot(x - xb, y - yb) <= _CLUSTER:
                    foreign = True
                    break
        if foreign:
            candidates.append((x, y))

    clusters: list[list[float]] = []
    for x, y in candidates:
        for cluster in clusters:
            if hypot(x - cluster[0], y - cluster[1]) <= _CLUSTER:
                count = cluster[2]
                cluster[0] = (cluster[0] * count + x) / (count + 1.0)
                cluster[1] = (cluster[1] * count + y) / (count + 1.0)
                cluster[2] = count + 1.0
                break
        else:
            clusters.append([x, y, 1.0])

    dots: list[tuple[float, float]] = []
    for cx, cy, _count in clusters:
        # Two wire objects that follow the same elbow are still one corner.
        # A tee is three or more different directions leaving the point.
        dirs: set[tuple[int, int]] = set()
        wires: set[str] = set()
        for wid, x1, y1, x2, y2 in segments:
            d1 = hypot(cx - x1, cy - y1)
            d2 = hypot(cx - x2, cy - y2)
            through = d1 > _CLUSTER and d2 > _CLUSTER and _point_on(cx, cy, x1, y1, x2, y2)
            if d1 > _CLUSTER and d2 > _CLUSTER and not through:
                continue
            wires.add(wid)
            if through:
                ends = ((x1, y1), (x2, y2))
            elif d1 <= _CLUSTER:
                ends = ((x2, y2),)
            else:
                ends = ((x1, y1),)
            for ox, oy in ends:
                card = _cardinal(cx, cy, ox, oy)
                if card is not None:
                    dirs.add(card)
        if _is_junction(cx, cy, dirs, wires, pins or [], segments):
            dots.append((cx, cy))
    return dots


def _is_junction(
    cx: float,
    cy: float,
    dirs: set[tuple[int, int]],
    wires: set[str],
    pins: list[tuple[float, float, str]],
    segments: list[tuple[str, float, float, float, float]],
) -> bool:
    if len(wires) < 2 or len(dirs) < 2:
        return False
    if len(dirs) >= 3:
        return True
    near = _pin_at(cx, cy, pins)
    if near is None:
        return False
    # An elbow of two wires on a pin is the seal-in / drop tee. A straight
    # bus through the pin is a tee only when the symbol leaves at a right angle.
    if not _straight(dirs):
        return True
    return _symbol_leaves_sideways(near, dirs, pins, segments)


def _pin_at(
    cx: float, cy: float, pins: list[tuple[float, float, str]]
) -> tuple[float, float, str] | None:
    found = None
    best = _CLUSTER + 0.1
    for pin in pins:
        dist = hypot(cx - pin[0], cy - pin[1])
        if dist <= best:
            best = dist
            found = pin
    return found


def _straight(dirs: set[tuple[int, int]]) -> bool:
    if len(dirs) != 2:
        return False
    a, b = tuple(dirs)
    return a[0] == -b[0] and a[1] == -b[1]


def _symbol_leaves_sideways(
    pin: tuple[float, float, str],
    dirs: set[tuple[int, int]],
    pins: list[tuple[float, float, str]],
    segments: list[tuple[str, float, float, float, float]],
) -> bool:
    """A 2-terminal symbol whose other pin is wired across the bus."""

    owned = [item for item in pins if item[2] == pin[2]]
    if len(owned) != 2:
        return False
    other = owned[0] if hypot(owned[1][0] - pin[0], owned[1][1] - pin[1]) <= 0.2 else owned[1]
    if hypot(other[0] - pin[0], other[1] - pin[1]) <= 0.2:
        other = owned[0]
    if not _wired(other[0], other[1], segments):
        return False
    axis = _cardinal(pin[0], pin[1], other[0], other[1])
    if axis is None:
        return False
    opposite = (-axis[0], -axis[1])
    return any(direction not in {axis, opposite} for direction in dirs)


def _wired(x: float, y: float, segments: list[tuple[str, float, float, float, float]]) -> bool:
    for _wid, x1, y1, x2, y2 in segments:
        if hypot(x - x1, y - y1) <= _CLUSTER or hypot(x - x2, y - y2) <= _CLUSTER:
            return True
        if _point_on(x, y, x1, y1, x2, y2) and _end_gap(x, y, x1, y1, x2, y2) > _ON:
            return True
    return False


def _cardinal(x: float, y: float, ox: float, oy: float) -> tuple[int, int] | None:
    dx, dy = ox - x, oy - y
    if hypot(dx, dy) < 0.2:
        return None
    if abs(dx) >= abs(dy):
        return (1 if dx > 0 else -1, 0)
    return (0, 1 if dy > 0 else -1)


def _xy(point: object) -> tuple[float, float]:
    if hasattr(point, "x"):
        return (float(point.x), float(point.y))
    pair = point  # type: ignore[index]
    return (float(pair[0]), float(pair[1]))


def _point_on(x: float, y: float, x1: float, y1: float, x2: float, y2: float) -> bool:
    if hypot(x2 - x1, y2 - y1) < 1e-6:
        return hypot(x - x1, y - y1) <= _ON
    if abs(x1 - x2) <= 0.35:
        if abs(x - (x1 + x2) * 0.5) > _ON:
            return False
        return min(y1, y2) - _ON <= y <= max(y1, y2) + _ON
    if abs(y1 - y2) <= 0.35:
        if abs(y - (y1 + y2) * 0.5) > _ON:
            return False
        return min(x1, x2) - _ON <= x <= max(x1, x2) + _ON
    # Off-axis: distance to the infinite line, then the span.
    dx, dy = x2 - x1, y2 - y1
    length = hypot(dx, dy)
    dist = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / length
    if dist > _ON:
        return False
    t = ((x - x1) * dx + (y - y1) * dy) / (length * length)
    return -0.02 <= t <= 1.02


def _end_gap(x: float, y: float, x1: float, y1: float, x2: float, y2: float) -> float:
    return min(hypot(x - x1, y - y1), hypot(x - x2, y - y2))
