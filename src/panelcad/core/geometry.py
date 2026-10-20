"""Millimetre geometry. Origin top-left, Y down, rotation clockwise."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Rotation = Literal[0, 90, 180, 270]
Side = Literal["left", "right", "top", "bottom"]

PAGE_SIZES_MM = {
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "A2": (420.0, 594.0),
    "LETTER": (215.9, 279.4),
    "TABLOID": (279.4, 431.8),
}


class Point(BaseModel):
    x: float
    y: float

    def as_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)


class Page(BaseModel):
    size: str = "A3"
    orientation: Literal["landscape", "portrait"] = "landscape"
    width_mm: float | None = None
    height_mm: float | None = None

    def dimensions(self) -> tuple[float, float]:
        if self.size.lower() == "custom":
            if self.width_mm is None or self.height_mm is None:
                raise ValueError("custom page requires width_mm and height_mm")
            return (self.width_mm, self.height_mm)
        w, h = PAGE_SIZES_MM[self.size.upper()]
        if self.orientation == "landscape" and w < h:
            return (h, w)
        if self.orientation == "portrait" and w > h:
            return (h, w)
        return (w, h)


def match_page_size(width_mm: float, height_mm: float) -> tuple[str, str]:
    """Nearest named sheet, or custom when nothing is within 8 mm on both axes.

    An 11×17 scan is tabloid. It does not collapse to A3.
    """

    best_name = "custom"
    best_orient = "portrait" if height_mm >= width_mm else "landscape"
    best_err = 8.0
    for name, (short, long) in PAGE_SIZES_MM.items():
        for orient, page_w, page_h in (
            ("portrait", min(short, long), max(short, long)),
            ("landscape", max(short, long), min(short, long)),
        ):
            err = max(abs(page_w - width_mm), abs(page_h - height_mm))
            if err < best_err:
                best_err = err
                best_name = name
                best_orient = orient
    return best_name, best_orient


def rotate_point(x: float, y: float, rotation: int, ox: float = 0.0, oy: float = 0.0) -> tuple[float, float]:
    dx, dy = x - ox, y - oy
    r = rotation % 360
    if r == 0:
        return (ox + dx, oy + dy)
    if r == 90:
        return (ox - dy, oy + dx)
    if r == 180:
        return (ox - dx, oy - dy)
    if r == 270:
        return (ox + dy, oy - dx)
    raise ValueError(f"rotation must be 0/90/180/270, got {rotation}")


def manhattan_route(
    ax: float,
    ay: float,
    a_side: Side,
    bx: float,
    by: float,
    b_side: Side,
    stub: float = 8.0,
) -> list[Point]:
    """Orthogonal path from pin A to pin B with a short stub out of each pin."""

    def stub_end(x: float, y: float, side: Side) -> tuple[float, float]:
        if side == "left":
            return (x - stub, y)
        if side == "right":
            return (x + stub, y)
        if side == "top":
            return (x, y - stub)
        return (x, y + stub)

    sx, sy = stub_end(ax, ay, a_side)
    ex, ey = stub_end(bx, by, b_side)
    pts = [Point(x=ax, y=ay), Point(x=sx, y=sy)]
    # Prefer a mid vertical or horizontal depending on remaining delta.
    if a_side in ("left", "right") and b_side in ("left", "right"):
        mid_x = (sx + ex) / 2.0
        pts.extend([Point(x=mid_x, y=sy), Point(x=mid_x, y=ey)])
    elif a_side in ("top", "bottom") and b_side in ("top", "bottom"):
        mid_y = (sy + ey) / 2.0
        pts.extend([Point(x=sx, y=mid_y), Point(x=ex, y=mid_y)])
    else:
        pts.append(Point(x=ex, y=sy))
    pts.extend([Point(x=ex, y=ey), Point(x=bx, y=by)])
    # Drop zero-length segments.
    cleaned: list[Point] = []
    for p in pts:
        if cleaned and abs(cleaned[-1].x - p.x) < 1e-6 and abs(cleaned[-1].y - p.y) < 1e-6:
            continue
        cleaned.append(p)
    return cleaned
