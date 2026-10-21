"""Normalized drawing primitives used by both library symbols and renderers."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, Field

# A quarter-circle keeps at least this many chords. Fewer reads as a stair.
MIN_ARC_STEPS_PER_QUARTER = 8
# Chord may leave the true arc by this much. 0.15 mm is about one pixel at 200 dpi.
MAX_ARC_CHORD_MM = 0.15


class Primitive(BaseModel):
    type: Literal["rect", "line", "circle", "polyline", "arc", "text"]
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    r: float = 0.0
    # Arc: 0° at +X, positive sweep toward the top of the page (decreasing Y).
    start: float = 0.0
    sweep: float = 0.0
    points: list[tuple[float, float]] = Field(default_factory=list)
    text: str = ""
    size: float = 3.0
    fill: bool = False


def arc_steps(radius: float, sweep_deg: float) -> int:
    """How many chords a curve needs so it does not stair-step.

    The count is at least eight per quarter-turn, and more when a chord would
    leave the true arc by more than ``MAX_ARC_CHORD_MM``.
    """

    sweep = abs(float(sweep_deg))
    radius = abs(float(radius))
    if sweep < 1e-6 or radius <= 1e-6:
        return 1
    minimum = max(2, int(math.ceil((sweep / 90.0) * MIN_ARC_STEPS_PER_QUARTER)))
    if radius <= MAX_ARC_CHORD_MM:
        return minimum
    cos_half = max(-1.0, min(1.0, 1.0 - MAX_ARC_CHORD_MM / radius))
    half = math.acos(cos_half)
    if half <= 1e-6:
        return minimum
    needed = int(math.ceil(math.radians(sweep) / (2.0 * half)))
    return max(minimum, needed)


def arc_point(cx: float, cy: float, radius: float, deg: float) -> tuple[float, float]:
    """Point on an arc. 0° is +X. Positive degrees move toward the top of the page."""

    ang = math.radians(float(deg))
    return (
        float(cx) + float(radius) * math.cos(ang),
        float(cy) - float(radius) * math.sin(ang),
    )


def arc_polyline(
    cx: float, cy: float, radius: float, start_deg: float, sweep_deg: float
) -> list[tuple[float, float]]:
    """Smooth stand-in for an arc. Endpoints are exact. Chords stay inside the error budget."""

    count = arc_steps(radius, sweep_deg)
    if abs(float(sweep_deg)) < 1e-9 or abs(float(radius)) <= 1e-9:
        return [arc_point(cx, cy, radius, start_deg)]
    points = []
    for step in range(count + 1):
        deg = float(start_deg) + float(sweep_deg) * step / count
        points.append(arc_point(cx, cy, radius, deg))
    return points


def arc_through(
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
) -> tuple[float, float, float, float, float] | None:
    """Circle arc through three points: center, radius, start degrees, sweep degrees.

    The sweep is the one that passes through the middle point. Collinear points
    return None so the caller can keep a straight line.
    """

    x1, y1 = float(p1[0]), float(p1[1])
    x2, y2 = float(p2[0]), float(p2[1])
    x3, y3 = float(p3[0]), float(p3[1])
    det = 2.0 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(det) < 1e-6:
        return None
    ux = (
        (x1 * x1 + y1 * y1) * (y2 - y3)
        + (x2 * x2 + y2 * y2) * (y3 - y1)
        + (x3 * x3 + y3 * y3) * (y1 - y2)
    ) / det
    uy = (
        (x1 * x1 + y1 * y1) * (x3 - x2)
        + (x2 * x2 + y2 * y2) * (x1 - x3)
        + (x3 * x3 + y3 * y3) * (x2 - x1)
    ) / det
    radius = math.hypot(x1 - ux, y1 - uy)
    if radius < 0.2:
        return None

    def _deg(x: float, y: float) -> float:
        return math.degrees(math.atan2(-(y - uy), x - ux))

    start = _deg(x1, y1)
    end = _deg(x3, y3)
    mid = _deg(x2, y2)
    ccw = (end - start) % 360.0
    if ccw < 1e-6:
        ccw = 360.0
    if (mid - start) % 360.0 <= ccw + 0.05:
        sweep = ccw
    else:
        sweep = ccw - 360.0
    return (ux, uy, radius, start, sweep)


def chord_sagitta(points: list[tuple[float, float]], cx: float, cy: float, radius: float) -> float:
    """Largest gap between a chord and the arc, measured through the chord midpoint."""

    radius = abs(float(radius))
    worst = 0.0
    for start, end in zip(points, points[1:]):
        dist = math.hypot((start[0] + end[0]) / 2.0 - cx, (start[1] + end[1]) / 2.0 - cy)
        worst = max(worst, abs(radius - dist))
    return worst


def expand_draw_item(item: dict[str, Any]) -> Primitive:
    if "rect" in item:
        x, y, w, h = item["rect"]
        return Primitive(type="rect", x=float(x), y=float(y), w=float(w), h=float(h), fill=bool(item.get("fill", False)))
    if "line" in item:
        x1, y1, x2, y2 = item["line"]
        return Primitive(type="line", x=float(x1), y=float(y1), x2=float(x2), y2=float(y2))
    if "circle" in item:
        cx, cy, r = item["circle"]
        return Primitive(type="circle", x=float(cx), y=float(cy), r=float(r), fill=bool(item.get("fill", False)))
    if "arc" in item:
        cx, cy, r, start, sweep = item["arc"]
        return Primitive(
            type="arc",
            x=float(cx),
            y=float(cy),
            r=float(r),
            start=float(start),
            sweep=float(sweep),
        )
    if "polyline" in item:
        pts = [(float(p[0]), float(p[1])) for p in item["polyline"]]
        return Primitive(type="polyline", points=pts)
    if "text" in item:
        raw = item["text"]
        x, y, text = raw[0], raw[1], raw[2]
        size = float(raw[3]) if len(raw) > 3 else float(item.get("size", 3.0))
        return Primitive(type="text", x=float(x), y=float(y), text=str(text), size=size)
    if "type" in item:
        return Primitive.model_validate(item)
    raise ValueError(f"unknown draw item: {item!r}")


def land_leads_on_circle(category: str, pins: list[Any], primitives: list[Primitive], tol: float = 0.05) -> list[Primitive]:
    """Make every motor lead end exactly on the motor circle.

    A lead is a line with one end on a pin. When its far end stops short of the
    circle, or pokes inside it, the end is moved to where the line meets the circle.
    When the line, extended, would miss the circle, a short connector runs from the
    lead end to the nearest point on the circle. Only load.motor symbols are touched,
    and a symbol whose leads already land is returned unchanged.
    """

    if not str(category or "").lower().startswith("load.motor"):
        return primitives
    circles = [p for p in primitives if p.type == "circle" and not p.fill and p.r > 0]
    if not circles:
        return primitives
    body = max(circles, key=lambda p: p.r)
    cx, cy, r = float(body.x), float(body.y), float(body.r)
    pin_pts = [(float(getattr(p, "x", 0.0)), float(getattr(p, "y", 0.0))) for p in pins]

    def at_pin(x: float, y: float) -> bool:
        return any(abs(x - px) <= tol and abs(y - py) <= tol for px, py in pin_pts)

    line_ends: list[tuple[float, float]] = []
    for p in primitives:
        if p.type == "line":
            line_ends += [(float(p.x), float(p.y)), (float(p.x2), float(p.y2))]

    def joins_other(x: float, y: float) -> bool:
        # The far end meets another line (a ground symbol, terminal box, bend).
        hits = sum(1 for ex, ey in line_ends if abs(x - ex) <= tol and abs(y - ey) <= tol)
        return hits > 1

    out: list[Primitive] = []
    for prim in primitives:
        if prim.type != "line":
            out.append(prim)
            continue
        (ax, ay), (bx, by) = (prim.x, prim.y), (prim.x2, prim.y2)
        if not at_pin(ax, ay) and at_pin(bx, by):
            (ax, ay), (bx, by) = (bx, by), (ax, ay)
        if (
            not at_pin(ax, ay)
            or at_pin(bx, by)
            or joins_other(bx, by)
            or math.hypot(ax - cx, ay - cy) <= r + tol
            # Only a lead that stops near the body is a motor lead; a long line
            # that ends far from the circle belongs to something else.
            or math.hypot(bx - cx, by - cy) - r > max(6.0, 0.75 * r)
        ):
            out.append(prim)
            continue
        if abs(math.hypot(bx - cx, by - cy) - r) <= tol:
            out.append(prim)
            continue
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length <= 1e-9:
            out.append(prim)
            continue
        ux, uy = dx / length, dy / length
        fx, fy = ax - cx, ay - cy
        b = fx * ux + fy * uy
        disc = b * b - (fx * fx + fy * fy - r * r)
        if disc >= 0 and -b - math.sqrt(disc) > 0:
            t = -b - math.sqrt(disc)
            out.append(Primitive(type="line", x=ax, y=ay, x2=round(ax + ux * t, 4), y2=round(ay + uy * t, 4)))
            continue
        # The straight lead misses the circle: keep it, then bend onto the circle.
        if math.hypot(bx - cx, by - cy) < r:
            out.append(prim)
            continue
        out.append(Primitive(type="line", x=ax, y=ay, x2=bx, y2=by))
        d = math.hypot(bx - cx, by - cy)
        out.append(
            Primitive(type="line", x=bx, y=by, x2=round(cx + (bx - cx) * r / d, 4), y2=round(cy + (by - cy) * r / d, 4))
        )
    return out
