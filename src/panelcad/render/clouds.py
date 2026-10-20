"""Revision clouds: scalloped boxes and a delta triangle with the rev."""

from __future__ import annotations

import math

from panelcad.core.models import Project, Sheet
from panelcad.render.painter import Painter
from panelcad.render.titleblock import working_rect

CLOUD = "#b00020"
CLOUD_WIDTH = 0.6
PAD = 5.0
ARC = 3.4
MIN_AUTO = 16.0


def cloud_polyline(x: float, y: float, w: float, h: float, radius: float = ARC) -> list[tuple[float, float]]:
    """Clockwise scallops around a rectangle. Y increases downward."""

    w = max(float(w), radius * 2)
    h = max(float(h), radius * 2)
    corners = (
        (x, y, x + w, y),
        (x + w, y, x + w, y + h),
        (x + w, y + h, x, y + h),
        (x, y + h, x, y),
    )
    points: list[tuple[float, float]] = []
    for x0, y0, x1, y1 in corners:
        points.extend(_edge_bumps(x0, y0, x1, y1, radius))
    if points and points[0] != points[-1]:
        points.append(points[0])
    return points


def _edge_bumps(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    radius: float,
) -> list[tuple[float, float]]:
    length = math.hypot(x1 - x0, y1 - y0)
    if length < 1e-6:
        return [(x0, y0)]
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    nx, ny = uy, -ux
    span = max(radius * 1.7, 4.0)
    n = max(1, int(round(length / span)))
    step = length / n
    pts: list[tuple[float, float]] = []
    for i in range(n):
        cx = x0 + ux * (i + 0.5) * step
        cy = y0 + uy * (i + 0.5) * step
        half = step / 2.0
        bulge = min(radius, half * 0.95)
        for k in range(7):
            ang = math.pi * (k / 6.0)
            along = -math.cos(ang)
            out = math.sin(ang)
            pts.append((cx + ux * along * half + nx * out * bulge, cy + uy * along * half + ny * out * bulge))
    return pts


NOTE_SIZE = 2.4
_DELTA_CORNERS = ("tr", "tl", "br", "bl")


def _delta_scale(size: float) -> tuple[float, float, float, float]:
    scale = max(0.6, float(size) / 7.0)
    return (9.2 * scale, 8.6 * scale, 2.0 * scale, 1.8 * scale)


def delta_at(
    x: float,
    y: float,
    w: float,
    h: float,
    corner: str,
    size: float = 7.0,
) -> list[tuple[float, float]]:
    """Delta in one exterior corner quadrant, one vertex on that cloud corner."""

    rise, reach, wing_x, tip_y = _delta_scale(size)
    name = corner if corner in _DELTA_CORNERS else "tr"
    sign_x = -1.0 if name in {"tl", "bl"} else 1.0
    sign_y = -1.0 if name in {"tr", "tl"} else 1.0
    origins = {
        "tr": (x + w, y),
        "tl": (x, y),
        "br": (x + w, y + h),
        "bl": (x, y + h),
    }
    origin = origins[name]
    wing = (origin[0] + sign_x * wing_x, origin[1] + sign_y * rise)
    tip = (origin[0] + sign_x * reach, origin[1] + sign_y * tip_y)
    return [origin, tip, wing]


def delta_triangle(
    x: float,
    y: float,
    w: float,
    h: float,
    size: float = 7.0,
) -> list[tuple[float, float]]:
    """Delta in the exterior corner quadrant, with one vertex on the cloud corner.

    Scallops bulge outward along each edge and shrink to nothing at the corner,
    so the open quadrant past that corner is clear of the cloud. Top-right when
    there is room above the cloud, otherwise bottom-right.
    """

    rise, _reach, _wing, _tip = _delta_scale(size)
    corner = "tr" if y >= rise + 1.0 else "br"
    return delta_at(x, y, w, h, corner, size)


def _rects_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def _inflate(
    box: tuple[float, float, float, float],
    pad: float,
) -> tuple[float, float, float, float]:
    return (box[0] - pad, box[1] - pad, box[2] + 2 * pad, box[3] + 2 * pad)


def _cross(origin: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    return (a[0] - origin[0]) * (b[1] - origin[1]) - (a[1] - origin[1]) * (b[0] - origin[0])


def _barycentric(
    point: tuple[float, float],
    tri: list[tuple[float, float]],
) -> tuple[float, float, float] | None:
    (x, y), (x1, y1), (x2, y2), (x3, y3) = point, tri[0], tri[1], tri[2]
    den = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    if abs(den) < 1e-9:
        return None
    w1 = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / den
    w2 = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / den
    return w1, w2, 1.0 - w1 - w2


def _point_in_rect(
    px: float,
    py: float,
    box: tuple[float, float, float, float],
    eps: float = 1e-6,
) -> bool:
    x, y, w, h = box
    return x - eps <= px <= x + w + eps and y - eps <= py <= y + h + eps


def _point_in_tri(
    point: tuple[float, float],
    tri: list[tuple[float, float]],
    eps: float = 1e-6,
) -> bool:
    weights = _barycentric(point, tri)
    return weights is not None and all(weight >= -eps for weight in weights)


def _on_segment(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> bool:
    return (
        min(a[0], c[0]) - 1e-6 <= b[0] <= max(a[0], c[0]) + 1e-6
        and min(a[1], c[1]) - 1e-6 <= b[1] <= max(a[1], c[1]) + 1e-6
    )


def _segments_hit(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    d1 = _cross(c, d, a)
    d2 = _cross(c, d, b)
    d3 = _cross(a, b, c)
    d4 = _cross(a, b, d)
    if d1 * d2 < 0 and d3 * d4 < 0:
        return True
    if abs(d1) <= 1e-6 and _on_segment(c, a, d):
        return True
    if abs(d2) <= 1e-6 and _on_segment(c, b, d):
        return True
    if abs(d3) <= 1e-6 and _on_segment(a, c, b):
        return True
    if abs(d4) <= 1e-6 and _on_segment(a, d, b):
        return True
    return False


def triangle_hits_rect(
    tri: list[tuple[float, float]],
    box: tuple[float, float, float, float],
) -> bool:
    """True when the triangle touches or crosses an axis-aligned box."""

    if any(_point_in_rect(px, py, box) for px, py in tri):
        return True
    x, y, w, h = box
    corners = ((x, y), (x + w, y), (x + w, y + h), (x, y + h))
    if any(_point_in_tri(point, tri) for point in corners):
        return True
    edges = tuple(zip(corners, corners[1:] + corners[:1]))
    tri_edges = tuple(zip(tri, tri[1:] + tri[:1]))
    return any(_segments_hit(a, b, c, d) for a, b in tri_edges for c, d in edges)


def delta_glyph_box(
    tri: list[tuple[float, float]],
    rev: str,
    size: float,
) -> tuple[float, float, float, float]:
    """Ink box of the rev letter. Y is the top of the glyphs."""

    from panelcad.render.qt_painter import text_width_mm

    label = str(rev or "").strip() or "A"
    cx, cy = delta_text_point(tri, size)
    width = text_width_mm(label, size)
    return (cx - width / 2.0, cy - 0.93 * size, width, 1.17 * size)


def choose_delta(
    x: float,
    y: float,
    w: float,
    h: float,
    obstacles: list[tuple[float, float, float, float]] | None = None,
    *,
    rev: str = "",
    size: float = 7.0,
) -> list[tuple[float, float]]:
    """First corner whose triangle and rev letter miss every obstacle.

    Order is top-right, top-left, bottom-right, bottom-left. Upward corners
    are skipped when the sheet has no room above the cloud. With nothing in
    the way this matches ``delta_triangle``.
    """

    blocks = [_inflate(box, 0.5) for box in (obstacles or []) if box[2] > 0 and box[3] > 0]
    text_size = 2.6 if len(str(rev or "").strip()) > 2 else 3.2
    rise, _reach, _wing, _tip = _delta_scale(size)
    fallback: list[tuple[float, float]] | None = None
    for name in _DELTA_CORNERS:
        if name in {"tr", "tl"} and y < rise + 1.0:
            continue
        tri = delta_at(x, y, w, h, name, size)
        if fallback is None:
            fallback = tri
        glyph = delta_glyph_box(tri, rev, text_size)
        if any(triangle_hits_rect(tri, box) or _rects_overlap(glyph, box) for box in blocks):
            continue
        return tri
    return fallback if fallback is not None else delta_triangle(x, y, w, h, size)


def _path_point(point: object) -> tuple[float, float]:
    if hasattr(point, "x") and hasattr(point, "y"):
        return (float(point.x), float(point.y))  # type: ignore[attr-defined]
    pair = point  # type: ignore[assignment]
    return (float(pair[0]), float(pair[1]))  # type: ignore[index]


def _path_boxes(points: list, inflate: float) -> list[tuple[float, float, float, float]]:
    seq = [_path_point(point) for point in points]
    boxes: list[tuple[float, float, float, float]] = []
    for (x0, y0), (x1, y1) in zip(seq, seq[1:]):
        boxes.append(
            (
                min(x0, x1) - inflate,
                min(y0, y1) - inflate,
                max(abs(x1 - x0) + 2 * inflate, 0.4),
                max(abs(y1 - y0) + 2 * inflate, 0.4),
            )
        )
    return boxes


def cloud_obstacles(
    sheet: Sheet,
    catalog: object | None = None,
) -> list[tuple[float, float, float, float]]:
    """Rails, devices, and wire routes the rev triangle must stay off."""

    boxes: list[tuple[float, float, float, float]] = []
    for rail in list(getattr(sheet, "rails", []) or []):
        half = float(rail.height_mm) / 2.0
        boxes.append(
            (
                float(rail.x),
                float(rail.y) - half - 4.0,
                float(rail.length_mm),
                float(rail.height_mm) + 4.0,
            )
        )
    if catalog is None:
        return boxes
    from panelcad.core.revisions import _component_box, _device

    for comp in sheet.components:
        boxes.append(_component_box(sheet, comp, _device(catalog, comp.library_id)))
    from panelcad.render.wiring import route_sheet_wires

    for pts in route_sheet_wires(sheet, catalog).values():
        boxes.extend(_path_boxes(list(pts), 1.2))
    return boxes


def delta_text_point(tri: list[tuple[float, float]], size: float) -> tuple[float, float]:
    """Baseline center for the rev letter inside ``tri``."""

    cx = (tri[0][0] + tri[1][0] + tri[2][0]) / 3.0
    cy = (tri[0][1] + tri[1][1] + tri[2][1]) / 3.0 + 0.35 * float(size)
    return (cx, cy)


def _note_width(text: str) -> float:
    from panelcad.render.qt_painter import text_width_mm

    return text_width_mm(text, NOTE_SIZE)


def note_anchor(x: float, y: float, w: float, h: float, note: str) -> tuple[float, float] | None:
    """Lower interior of the cloud, or just below it when the words do not fit."""

    text = " ".join(str(note or "").split())
    if not text:
        return None
    width = _note_width(text)
    inner = w - 5.6
    baseline = y + h - 2.6
    if width <= inner and baseline - NOTE_SIZE >= y + 3.4:
        return (x + 2.8, baseline)
    return (x + 1.6, y + h + ARC + NOTE_SIZE + 1.8)


def note_bounds(
    x: float,
    y: float,
    w: float,
    h: float,
    note: str,
) -> tuple[float, float, float, float] | None:
    """Glyph box for a cloud note. Y is the top of the ink."""

    text = " ".join(str(note or "").split())
    anchor = note_anchor(x, y, w, h, text)
    if anchor is None:
        return None
    width = _note_width(text)
    top = anchor[1] - 0.93 * NOTE_SIZE
    return (anchor[0], top, width, 1.17 * NOTE_SIZE)


def _pad(box: tuple[float, float, float, float], pad: float) -> tuple[float, float, float, float]:
    x, y, w, h = box
    return (x - pad, y - pad, w + 2 * pad, h + 2 * pad)


def _ensure_min(box: tuple[float, float, float, float], minimum: float) -> tuple[float, float, float, float]:
    x, y, w, h = box
    if w < minimum:
        x -= (minimum - w) / 2.0
        w = minimum
    if h < minimum:
        y -= (minimum - h) / 2.0
        h = minimum
    return (x, y, w, h)


def _clamp(sheet: Sheet, box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    wx, wy, ww, wh = working_rect(sheet)
    x, y, w, h = box
    x0 = max(wx + 0.5, x)
    y0 = max(wy + 0.5, y)
    x1 = min(wx + ww - 0.5, x + w)
    y1 = min(wy + wh - 0.5, y + h)
    if x1 - x0 < 6.0 or y1 - y0 < 6.0:
        return box
    return (x0, y0, x1 - x0, y1 - y0)


def drawn_box(cloud: object, sheet: Sheet) -> tuple[float, float, float, float]:
    """Rectangle actually stroked. Auto clouds are padded; manual clouds stay as drawn."""

    box = (float(cloud.x), float(cloud.y), float(cloud.w), float(cloud.h))  # type: ignore[attr-defined]
    if not bool(getattr(cloud, "manual", False)):
        box = _ensure_min(_pad(box, PAD), MIN_AUTO)
    return _clamp(sheet, box)


def paint_revision_cloud(
    painter: Painter,
    cloud: object,
    sheet: Sheet,
    catalog: object | None = None,
) -> None:
    """One scalloped cloud, its shop note, and the rev delta."""

    x, y, w, h = drawn_box(cloud, sheet)
    painter.set_layer("REVCLOUD")
    painter.set_stroke(CLOUD, CLOUD_WIDTH)
    painter.set_fill(None)
    painter.polyline(cloud_polyline(x, y, w, h))
    note = " ".join(str(getattr(cloud, "note", "") or "").split())
    anchor = note_anchor(x, y, w, h, note)
    if note and anchor is not None:
        painter.text(anchor[0], anchor[1], note, NOTE_SIZE, align="start")
    rev = str(getattr(cloud, "rev", "") or "").strip()
    if not rev:
        return
    blocks = cloud_obstacles(sheet, catalog)
    ink = note_bounds(x, y, w, h, note)
    if ink is not None:
        blocks.append(ink)
    tri = choose_delta(x, y, w, h, blocks, rev=rev)
    painter.set_fill("#ffffff")
    painter.set_stroke(CLOUD, 0.45)
    painter.polygon(tri)
    painter.set_fill(None)
    size = 2.6 if len(rev) > 2 else 3.2
    cx, cy = delta_text_point(tri, size)
    painter.text(cx, cy, rev, size, align="middle")


def draw_revision_clouds(
    painter: Painter,
    project: Project,
    sheet: Sheet,
    catalog: object | None = None,
) -> None:
    """Draw stored clouds for this sheet. No-op when the project has none."""

    store = project.revision_control
    if store is None or not store.clouds:
        return
    mine = [cloud for cloud in store.clouds if cloud.sheet == sheet.id and cloud.w > 0 and cloud.h > 0]
    if not mine:
        return
    for cloud in mine:
        paint_revision_cloud(painter, cloud, sheet, catalog)
