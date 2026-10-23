"""Where a pin meets its symbol's own graphics.

A wire is drawn to the pin point. A symbol whose body is inset from its frame
(a library rect at 2,2 with the pin on the frame edge) leaves the wire ending
in white space beside the body. ``pin_lead`` finds the gap in symbol-local
units so the painter can draw a lead from the pin to the body, and the design
checks can flag a pin that has no graphics at all near it.
"""

from __future__ import annotations

from math import atan2, degrees, hypot, sqrt

from panelcad.core.models import Component, Device, PinDef
from panelcad.core.primitives import Primitive

# Ink closer than this (symbol mm) already touches the pin.
TOUCH = 0.35
# A lead longer than this is not a lead. The pin is somewhere else entirely.
MAX_LEAD = 12.0

_INWARD = {"left": (1.0, 0.0), "right": (-1.0, 0.0), "top": (0.0, 1.0), "bottom": (0.0, -1.0)}


def _segments(prim: Primitive) -> list[tuple[float, float, float, float]]:
    if prim.type == "rect":
        x, y, w, h = prim.x, prim.y, prim.w, prim.h
        return [(x, y, x + w, y), (x + w, y, x + w, y + h), (x + w, y + h, x, y + h), (x, y + h, x, y)]
    if prim.type == "line":
        return [(prim.x, prim.y, prim.x2, prim.y2)]
    if prim.type == "polyline":
        pts = list(prim.points or [])
        return [(a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:])]
    return []


def _pt_seg(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    dx, dy = x2 - x1, y2 - y1
    den = dx * dx + dy * dy
    t = 0.0 if den <= 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / den))
    return hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _on_arc(prim: Primitive, px: float, py: float) -> bool:
    if prim.type != "arc":
        return True
    ang = degrees(atan2(-(py - prim.y), px - prim.x)) % 360.0
    start = float(prim.start or 0.0) % 360.0
    sweep = float(prim.sweep or 360.0)
    rel = (ang - start) % 360.0 if sweep >= 0 else (start - ang) % 360.0
    return rel <= abs(sweep) + 1.0


def ink_distance(prims: list[Primitive], px: float, py: float) -> float:
    """Distance from a symbol-local point to the nearest stroke of *prims*."""

    best = float("inf")
    for prim in prims:
        for seg in _segments(prim):
            best = min(best, _pt_seg(px, py, *seg))
        if prim.type in ("circle", "arc") and prim.r:
            d = abs(hypot(px - prim.x, py - prim.y) - prim.r)
            if d < best and _on_arc(prim, px, py):
                best = d
    return best


def _ray_hit(prims: list[Primitive], px: float, py: float, ux: float, uy: float) -> float | None:
    best: float | None = None

    def take(t: float) -> None:
        nonlocal best
        if t > 1e-6 and (best is None or t < best):
            best = t

    for prim in prims:
        for x1, y1, x2, y2 in _segments(prim):
            ex, ey = x2 - x1, y2 - y1
            den = ux * ey - uy * ex
            if abs(den) < 1e-9:
                # Collinear stroke: the lead meets its near end.
                if _pt_seg(px, py, x1, y1, x2, y2) - min(hypot(x1 - px, y1 - py), hypot(x2 - px, y2 - py)) < 1e-6:
                    if abs((x1 - px) * uy - (y1 - py) * ux) < 1e-6:
                        for qx, qy in ((x1, y1), (x2, y2)):
                            take((qx - px) * ux + (qy - py) * uy)
                continue
            t = ((x1 - px) * ey - (y1 - py) * ex) / den
            u = ((x1 - px) * uy - (y1 - py) * ux) / den
            if -1e-6 <= u <= 1 + 1e-6:
                take(t)
        if prim.type in ("circle", "arc") and prim.r:
            fx, fy = px - prim.x, py - prim.y
            b = fx * ux + fy * uy
            c = fx * fx + fy * fy - prim.r * prim.r
            disc = b * b - c
            if disc >= 0:
                for t in (-b - sqrt(disc), -b + sqrt(disc)):
                    if t > 1e-6 and _on_arc(prim, px + t * ux, py + t * uy):
                        take(t)
                        break
    return best


def drawn_primitives(device: Device) -> list[Primitive]:
    out = []
    for prim in device.primitives:
        if not isinstance(prim, Primitive):
            prim = Primitive.model_validate(prim)
        if prim.type != "text":
            out.append(prim)
    return out


def _lead(prims: list[Primitive], px: float, py: float, side: str) -> tuple[float, float] | None:
    if not prims or ink_distance(prims, px, py) <= TOUCH:
        return None
    ux, uy = _INWARD.get(side, (0.0, 0.0))
    if ux == 0.0 and uy == 0.0:
        return None
    t = _ray_hit(prims, px, py, ux, uy)
    if t is None or t > MAX_LEAD:
        return None
    return (px + t * ux, py + t * uy)


def pin_lead(device: Device, component: Component, pin: PinDef) -> tuple[float, float] | None:
    """Symbol-local end of the lead a pin needs, or None when ink already reaches it.

    The lead runs inward from the pin side to the first stroke of the symbol.
    """

    px, py = component.pin_xy(pin)
    return _lead(drawn_primitives(device), float(px), float(py), component.pin_side(pin))


def pin_gap(device: Device, component: Component, pin: PinDef) -> float:
    """Symbol mm between the pin point and the symbol ink, after any lead."""

    prims = drawn_primitives(device)
    if not prims:
        return 0.0
    px, py = component.pin_xy(pin)
    d = ink_distance(prims, float(px), float(py))
    if d <= TOUCH:
        return 0.0
    if pin_lead(device, component, pin) is not None:
        return 0.0
    return d


def lead_segments(device: Device, component: Component) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Sheet-space (pin id, x1, y1, x2, y2) of every lead the painter adds.

    The label pass needs these: a lead is ink like any conductor, and a wire
    number parked on one reads as crossed out.
    """

    from panelcad.core.geometry import rotate_point

    hidden = {str(p) for p in (component.properties.get("hidden_pins") or [])}
    s = component.visual_scale()
    out = []
    for pin in device.pins:
        if pin.id in hidden:
            continue
        end = pin_lead(device, component, pin)
        if end is None:
            continue
        px, py = component.pin_xy(pin)
        pts = []
        for lx, ly in ((float(px), float(py)), end):
            x, y = rotate_point(lx * s, ly * s, component.rotation)
            if component.mirror:
                x = -x
            pts.append((component.x + x, component.y + y))
        out.append((pin.id, (pts[0][0], pts[0][1], pts[1][0], pts[1][1])))
    return out
