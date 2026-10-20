"""Perspective correction, real scale, and multi-photo alignment.

A phone photo is not a drawing: the panel face is a quad, not a rectangle.
The tech marks four corners and one known length. Nothing here invents a
millimetre.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

from panelcad.reconstruct.models import (
    DIN_RAIL_HEIGHT_MM,
    PERSPECTIVE_SCALE_FRACTION,
    SCALE_ERROR_TOLERANCE_MM,
    Photo,
    PhotoCorner,
    ScaleKind,
    ScaleReference,
)

_ONE_LENGTH = (
    "Scale not set: one length is not enough. "
    "Mark a second length across the panel, or click the 4 corners of a part whose size you know."
)
_BAD_MARKS = "Scale not set: those two marks do not make a length. Mark two different points."

_EPS = 1e-9


def default_corners(width_px: float, height_px: float) -> list[PhotoCorner]:
    w = float(width_px)
    h = float(height_px)
    return [
        PhotoCorner(x=0.0, y=0.0),
        PhotoCorner(x=w, y=0.0),
        PhotoCorner(x=w, y=h),
        PhotoCorner(x=0.0, y=h),
    ]


def corners_of(photo: Photo) -> list[tuple[float, float]]:
    if len(photo.corners) == 4:
        return [(float(c.x), float(c.y)) for c in photo.corners]
    return [(c.x, c.y) for c in default_corners(photo.width_px or 1, photo.height_px or 1)]


def rectified_size(photo: Photo) -> tuple[float, float]:
    """Destination rectangle for the marked panel face, in photo pixels."""

    pts = corners_of(photo)
    top = math.hypot(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
    bot = math.hypot(pts[2][0] - pts[3][0], pts[2][1] - pts[3][1])
    left = math.hypot(pts[3][0] - pts[0][0], pts[3][1] - pts[0][1])
    right = math.hypot(pts[2][0] - pts[1][0], pts[2][1] - pts[1][1])
    return (max(top, bot, 1.0), max(left, right, 1.0))


def homography(
    src: Iterable[tuple[float, float]],
    dst: Iterable[tuple[float, float]],
) -> list[list[float]]:
    """3×3 homography taking ``src`` quad points onto ``dst`` (last row scaled to 1)."""

    s = list(src)
    d = list(dst)
    if len(s) != 4 or len(d) != 4:
        raise ValueError("perspective needs four corner points")
    rows: list[list[float]] = []
    for (x, y), (u, v) in zip(s, d):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y, u])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y, v])
    solved = _solve(rows)
    h31, h32 = solved[6], solved[7]
    matrix = [
        [solved[0], solved[1], solved[2]],
        [solved[3], solved[4], solved[5]],
        [h31, h32, 1.0],
    ]
    return matrix


def apply_homography(matrix: list[list[float]], x: float, y: float) -> tuple[float, float]:
    a, b, c = matrix[0]
    d, e, f = matrix[1]
    g, h, i = matrix[2]
    w = g * x + h * y + i
    if abs(w) < _EPS:
        raise ValueError("perspective mapping is degenerate at that point")
    return ((a * x + b * y + c) / w, (d * x + e * y + f) / w)


def photo_to_rectified(photo: Photo, x: float, y: float) -> tuple[float, float]:
    width, height = rectified_size(photo)
    dest = [(0.0, 0.0), (width, 0.0), (width, height), (0.0, height)]
    matrix = homography(corners_of(photo), dest)
    return apply_homography(matrix, x, y)


def known_length_mm(
    kind: ScaleKind,
    known_mm: float | None = None,
    *,
    catalog_mm: float | None = None,
) -> float:
    """Resolve the real length the tech is pointing at. Refuses a guess."""

    if kind == "din_rail":
        value = float(known_mm) if known_mm not in (None, 0, "") else DIN_RAIL_HEIGHT_MM
        if value <= 0:
            raise ValueError("DIN rail height must be a positive millimetre figure")
        return value
    if kind == "catalog_part":
        value = float(catalog_mm) if catalog_mm not in (None, 0, "") else float(known_mm or 0)
        if value <= 0:
            raise ValueError("catalog part scale needs a published width or height")
        return value
    if kind in {"wire_duct", "measured"}:
        value = float(known_mm or 0)
        if value <= 0:
            raise ValueError(f"{kind} scale needs the tech-entered millimetre length")
        return value
    raise ValueError(f"unknown scale kind '{kind}'")


def validate_corners(
    corners: list[tuple[float, float]],
    width_px: float,
    height_px: float,
) -> list[tuple[float, float]]:
    """Require a convex TL→TR→BR→BL quad inside the photo. Raise otherwise."""

    if len(corners) != 4:
        raise ValueError("perspective needs four corner points (photo pixels)")
    pts = [(float(x), float(y)) for x, y in corners]
    w = float(width_px or 0)
    h = float(height_px or 0)
    if w <= 0 or h <= 0:
        raise ValueError("photo size is unknown; cannot check corners")
    for x, y in pts:
        if x < -0.5 or y < -0.5 or x > w + 0.5 or y > h + 0.5:
            raise ValueError("corners must lie inside the photo")
    area = _quad_area(pts)
    if abs(area) < 16.0:
        raise ValueError("perspective corners are degenerate")
    turns = [_zcross(pts[i], pts[(i + 1) % 4], pts[(i + 2) % 4]) for i in range(4)]
    if any(value * turns[0] <= 0 for value in turns):
        raise ValueError("corners are in the wrong order (need top-left, top-right, bottom-right, bottom-left)")
    if pts[0][0] >= pts[1][0] or pts[0][1] >= pts[3][1]:
        raise ValueError("wrong order: first corner must be the top-left of the panel face")
    if _segments_cross(pts[0], pts[1], pts[3], pts[2]) or _segments_cross(pts[0], pts[3], pts[1], pts[2]):
        raise ValueError("perspective corners cross")
    return pts


def _quad_area(pts: list[tuple[float, float]]) -> float:
    return 0.5 * sum(pts[i][0] * pts[(i + 1) % 4][1] - pts[(i + 1) % 4][0] * pts[i][1] for i in range(4))


def _zcross(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_cross(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    def _side(p, q, r) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    s1 = _side(a, b, c)
    s2 = _side(a, b, d)
    s3 = _side(c, d, a)
    s4 = _side(c, d, b)
    return s1 * s2 < 0 and s3 * s4 < 0


def _ref_ratio(photo: Photo, ref: ScaleReference) -> tuple[float, tuple[float, float], tuple[float, float]]:
    x1, y1 = photo_to_rectified(photo, ref.p1_x, ref.p1_y)
    x2, y2 = photo_to_rectified(photo, ref.p2_x, ref.p2_y)
    pixels = math.hypot(x2 - x1, y2 - y1)
    if pixels < _EPS:
        raise ValueError("scale points are the same after perspective correction")
    return float(ref.known_mm) / pixels, (x1, y1), (x2, y2)


def metric_scale(photo: Photo) -> dict[str, Any] | None:
    """Recover millimetres from a known rectangle or two independent lengths."""

    face_w = float(photo.face_width_mm or 0)
    face_h = float(photo.face_height_mm or 0)
    if face_w > 0 and face_h > 0 and len(photo.corners) == 4:
        dest = [(0.0, 0.0), (face_w, 0.0), (face_w, face_h), (0.0, face_h)]
        dest_w, dest_h = rectified_size(photo)
        sx = face_w / dest_w
        sy = face_h / dest_h
        photo.scale_error_mm = 0.0
        return {
            "ready": True,
            "face": True,
            "sx": sx,
            "sy": sy,
            "error_mm": 0.0,
            "dest_mm": dest,
        }
    refs = [item for item in (photo.scale, photo.scale_b) if item is not None and float(item.known_mm or 0) > 0]
    if len(refs) < 2:
        photo.scale_error_mm = None
        return {
            "ready": False,
            "face": False,
            "sx": 0.0,
            "sy": 0.0,
            "error_mm": None,
            "message": _ONE_LENGTH,
        }
    try:
        r1, a1, b1 = _ref_ratio(photo, refs[0])
        r2, a2, b2 = _ref_ratio(photo, refs[1])
    except ValueError:
        photo.scale_error_mm = None
        return {
            "ready": False,
            "face": False,
            "sx": 0.0,
            "sy": 0.0,
            "error_mm": None,
            "message": _BAD_MARKS,
        }
    v1 = (b1[0] - a1[0], b1[1] - a1[1])
    v2 = (b2[0] - a2[0], b2[1] - a2[1])
    n1 = math.hypot(*v1) or 1.0
    n2 = math.hypot(*v2) or 1.0
    cosine = abs(v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
    dest_w, dest_h = rectified_size(photo)
    if cosine < 0.5:
        # Independent axes: the more-horizontal mark sets sx, the more-vertical sets sy.
        # A phone that is not square-on legitimately has two scales. That
        # difference is perspective, not a bad mark, until it is extreme.
        if abs(v1[0]) >= abs(v1[1]):
            sx, sy = r1, r2
        else:
            sx, sy = r2, r1
        if abs(v2[0]) >= abs(v2[1]) and abs(v1[0]) < abs(v1[1]):
            sx, sy = r2, r1
        if abs(v2[1]) >= abs(v2[0]) and abs(v1[0]) >= abs(v1[1]):
            sx, sy = r1, r2
        isotropic = (sx + sy) / 2.0
        error, ready, warned, relative = _perspective_ready(sx, sy, dest_w, dest_h)
        photo.scale_error_mm = error
        message = ""
        if not ready:
            message = _disagree_message(error)
        elif warned:
            message = _perspective_warning(error, relative)
        return {
            "ready": ready,
            "face": False,
            "sx": sx,
            "sy": sy,
            "error_mm": error,
            "mean": isotropic,
            "message": message,
            "perspective_warning": warned,
        }
    mean = (r1 + r2) / 2.0
    error = abs(r1 - r2) * max(dest_w, dest_h)
    photo.scale_error_mm = error
    ready = error <= SCALE_ERROR_TOLERANCE_MM
    message = "" if ready else _disagree_message(error)
    return {
        "ready": ready,
        "face": False,
        "sx": mean,
        "sy": mean,
        "error_mm": error,
        "message": message,
    }


def _perspective_ready(
    sx: float, sy: float, dest_w: float, dest_h: float
) -> tuple[float, bool, bool, float]:
    """Accept independent-axis scales that only differ by a normal camera angle.

    The third value is true when the scale is kept only because of the
    perspective allowance (the millimetre error is past the tight tolerance).
    """

    span = max(dest_w, dest_h, 1.0)
    error = abs(sx - sy) * span
    longer = max(sx, sy) * span
    if longer <= _EPS:
        return error, False, False, 1.0
    relative = error / longer
    within_mm = error <= SCALE_ERROR_TOLERANCE_MM
    ready = within_mm or relative <= PERSPECTIVE_SCALE_FRACTION
    return error, ready, ready and not within_mm, relative


def _perspective_warning(error_mm: float, relative: float) -> str:
    shown = int(round(float(error_mm)))
    percent = int(round(float(relative) * 100))
    return (
        f"Scale set. The two lengths differ by {shown} mm ({percent}%), "
        "which is normal for a photo taken at an angle. "
        "If a length was typed wrong, sizes on this photo will be off. "
        "For best accuracy, click the 4 corners of a part or the backpanel."
    )


def _disagree_message(error_mm: float) -> str:
    shown = int(round(float(error_mm)))
    return (
        f"Scale not set: the two lengths disagree by {shown} mm. "
        "Mark the 4 corners of a part whose size you know instead."
    )


def mm_per_px(photo: Photo) -> float | None:
    """Pixels-to-mm after a sufficient metric constraint. None if underconstrained."""

    metric = metric_scale(photo)
    if metric is None or not metric.get("ready"):
        return None
    sx = float(metric["sx"])
    sy = float(metric["sy"])
    if sx <= 0 or sy <= 0:
        return None
    return (sx + sy) / 2.0


def photo_to_mm(photo: Photo, x: float, y: float) -> tuple[float, float] | None:
    """Map a photo pixel onto the panel face in millimetres. None if unscaled."""

    metric = metric_scale(photo)
    if metric is None or not metric.get("ready"):
        return None
    if metric.get("face"):
        matrix = homography(corners_of(photo), metric["dest_mm"])
        u, v = apply_homography(matrix, x, y)
        return (photo.origin_x_mm + u, photo.origin_y_mm + v)
    rx, ry = photo_to_rectified(photo, x, y)
    return (photo.origin_x_mm + rx * float(metric["sx"]), photo.origin_y_mm + ry * float(metric["sy"]))


def region_center_mm(photo: Photo, region: Any) -> tuple[float, float] | None:
    x = float(getattr(region, "x", 0) or 0) + float(getattr(region, "w", 0) or 0) / 2.0
    y = float(getattr(region, "y", 0) or 0) + float(getattr(region, "h", 0) or 0) / 2.0
    return photo_to_mm(photo, x, y)


def region_top_left_mm(photo: Photo, region: Any) -> tuple[float, float] | None:
    """Occupied-rectangle top-left in millimetres (layout x/y)."""

    return photo_to_mm(
        photo,
        float(getattr(region, "x", 0) or 0),
        float(getattr(region, "y", 0) or 0),
    )


def align_origins(photos: list[Photo], anchors: list[tuple[str, float, float, float, float]]) -> int:
    """Shift later photos so a shared landmark sits at the same millimetres.

    ``anchors`` are ``(photo_id, pixel_x, pixel_y, target_x_mm, target_y_mm)``.
    Photos without a scale are left alone. Returns how many origins were written.
    """

    by_id = {photo.id: photo for photo in photos}
    moved = 0
    for photo_id, px, py, tx, ty in anchors:
        photo = by_id.get(photo_id)
        if photo is None or mm_per_px(photo) is None:
            continue
        here = photo_to_mm(photo, px, py)
        if here is None:
            continue
        photo.origin_x_mm += tx - here[0]
        photo.origin_y_mm += ty - here[1]
        moved += 1
    return moved


def scale_payload(photo: Photo) -> dict[str, Any]:
    ratio = None
    error = None
    message = ""
    try:
        metric = metric_scale(photo)
        ratio = mm_per_px(photo)
        error = None if metric is None else metric.get("error_mm")
        if metric is not None and metric.get("message"):
            message = str(metric.get("message") or "")
    except ValueError as exc:
        ratio = None
        error = None
        message = str(exc)
    if ratio is None and not message:
        message = _ONE_LENGTH
    return {
        "photo": photo.id,
        "scaled": ratio is not None,
        "mm_per_px": ratio,
        "kind": photo.scale.kind if photo.scale else "",
        "known_mm": photo.scale.known_mm if photo.scale else None,
        "face_mm": [photo.face_width_mm, photo.face_height_mm],
        "face_is_backpanel": bool(photo.face_is_backpanel),
        "error_mm": error,
        "tolerance_mm": SCALE_ERROR_TOLERANCE_MM,
        "origin_mm": [photo.origin_x_mm, photo.origin_y_mm],
        "message": message,
    }


def build_scale(
    kind: str,
    p1: tuple[float, float],
    p2: tuple[float, float],
    *,
    known_mm: float | None = None,
    catalog_mm: float | None = None,
    note: str = "",
    catalog: str = "",
    manufacturer: str = "",
    dimension: str = "",
) -> ScaleReference:
    token = str(kind or "").strip().lower().replace("-", "_").replace(" ", "_")
    if token in {"din", "rail", "ts35", "dinrail"}:
        token = "din_rail"
    if token in {"catalog", "part", "sku"}:
        token = "catalog_part"
    if token in {"duct", "wireduct", "tray"}:
        token = "wire_duct"
    if token in {"measure", "enclosure", "width", "inside"}:
        token = "measured"
    if token not in {"din_rail", "catalog_part", "wire_duct", "measured"}:
        raise ValueError("scale kind must be din_rail, catalog_part, wire_duct, or measured")
    length = known_length_mm(token, known_mm, catalog_mm=catalog_mm)  # type: ignore[arg-type]
    return ScaleReference(
        kind=token,  # type: ignore[arg-type]
        known_mm=length,
        p1_x=float(p1[0]),
        p1_y=float(p1[1]),
        p2_x=float(p2[0]),
        p2_y=float(p2[1]),
        note=note,
        catalog=catalog,
        manufacturer=manufacturer,
        dimension=dimension if dimension in {"width", "height", "depth"} else "",
    )


def _solve(rows: list[list[float]]) -> list[float]:
    """Gaussian elimination for an 8×9 (or n×n+1) system."""

    n = len(rows)
    if n < 1 or any(len(row) != n + 1 for row in rows):
        raise ValueError("perspective solver got a bad matrix")
    a = [list(map(float, row)) for row in rows]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < _EPS:
            raise ValueError("perspective corners are degenerate")
        if pivot != col:
            a[col], a[pivot] = a[pivot], a[col]
        scale = a[col][col]
        for j in range(col, n + 1):
            a[col][j] /= scale
        for row in range(n):
            if row == col:
                continue
            factor = a[row][col]
            if abs(factor) < _EPS:
                continue
            for j in range(col, n + 1):
                a[row][j] -= factor * a[col][j]
    return [a[i][n] for i in range(n)]
