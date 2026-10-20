"""Panel cutout geometry, datum dimensions, and shop warnings.

Coordinates on a surface match the layout sheets: millimetres, origin at the
top-left of that surface, Y down. A cutout's local origin is the centre of
the opening. Library numbers are loaded from ``libraries/builtin/cutouts.yaml``
and are never invented here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from panelcad.core.geometry import rotate_point
from panelcad.core.layout import is_layout_sheet
from panelcad.core.models import Component, Project, Sheet
from panelcad.library.catalog import Catalog
from panelcad.library.panel import effective_footprint
from panelcad.library.paths import builtin_library_dir

MM_PER_INCH = 25.4
SURFACES: tuple[str, ...] = ("door", "backplate", "left", "right", "top", "bottom")
DATUMS: tuple[str, ...] = ("top_left", "top_right", "bottom_left", "bottom_right")
VIEWS: tuple[str, ...] = ("front", "back")
STYLES: tuple[str, ...] = ("ordinate", "baseline")
UNITS: tuple[str, ...] = ("mm", "inch")
LAYOUT_SURFACES = {"door_layout": "door", "panel_layout": "backplate"}

# Same-row test for IEC Table 3. Not a manufacturer clearance.
_ROW_ALIGN_MM = 1.0

ShapeName = Literal["", "round", "rect", "d"]
KeySide = Literal["top", "bottom", "left", "right"]


class DrillHole(BaseModel):
    """One mounting hole. ``x_mm`` / ``y_mm`` are from the opening centre, Y down."""

    model_config = ConfigDict(extra="ignore")

    diameter_mm: float
    x_mm: float = 0.0
    y_mm: float = 0.0
    note: str = ""

    @field_validator("diameter_mm")
    @classmethod
    def _positive(cls, value: float) -> float:
        number = float(value)
        if number <= 0:
            raise ValueError("drill diameter must be positive")
        return number


class PanelCutout(BaseModel):
    """One published opening, plus optional drills and required clearances."""

    model_config = ConfigDict(extra="ignore")

    unknown: bool = False
    shape: ShapeName = ""
    diameter_mm: float | None = None
    diameter_plus_mm: float = 0.0
    diameter_minus_mm: float = 0.0
    width_mm: float | None = None
    height_mm: float | None = None
    width_plus_mm: float = 0.0
    height_plus_mm: float = 0.0
    width_minus_mm: float = 0.0
    height_minus_mm: float = 0.0
    corner_radius_mm: float = 0.0
    key_width_mm: float | None = None
    key_width_plus_mm: float = 0.0
    key_span_mm: float | None = None
    key_span_plus_mm: float = 0.0
    key_side: KeySide = "top"
    key_optional: bool = False
    holes: list[DrillHole] = Field(default_factory=list)
    clearance_mm: float | None = None
    min_center_a_mm: float | None = None
    min_center_b_mm: float | None = None
    # Axis-specific centre minimums. ``x`` is horizontal, ``y`` is vertical.
    # When ``centers_interchangeable`` is set, ``min_center_a_mm`` and
    # ``min_center_b_mm`` may be swapped (IEC Table 3). Otherwise ``x`` and
    # ``y`` stay on those axes (PPC5, Allen-Bradley instruction sheets).
    min_center_x_mm: float | None = None
    min_center_y_mm: float | None = None
    centers_interchangeable: bool = False
    depth_behind_mm: float | None = None
    panel_thickness_min_mm: float | None = None
    panel_thickness_max_mm: float | None = None
    # False when the figures were not checked on an official publication.
    # The numbers stay; the sheet and the inspector say they are unverified.
    verified: bool = True
    source: str = ""
    note: str = ""

    @model_validator(mode="after")
    def _geometry(self) -> PanelCutout:
        if self.unknown:
            return self
        if self.shape in {"round", "d"}:
            if self.diameter_mm is None or self.diameter_mm <= 0:
                raise ValueError("round and D cutouts need a positive diameter_mm")
        elif self.shape == "rect":
            if not self.width_mm or not self.height_mm or self.width_mm <= 0 or self.height_mm <= 0:
                raise ValueError("rectangular cutouts need positive width_mm and height_mm")
        else:
            raise ValueError("cutout shape must be round, rect, or d")
        if self.shape == "d" and not self.key_optional:
            if not self.key_width_mm or not self.key_span_mm:
                raise ValueError("D cutout needs key_width_mm and key_span_mm")
        if self.corner_radius_mm < 0:
            raise ValueError("corner radius cannot be negative")
        for name in ("min_center_a_mm", "min_center_b_mm", "min_center_x_mm", "min_center_y_mm"):
            value = getattr(self, name)
            if value is not None and float(value) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in (
            "diameter_plus_mm",
            "diameter_minus_mm",
            "width_plus_mm",
            "height_plus_mm",
            "width_minus_mm",
            "height_minus_mm",
            "key_width_plus_mm",
            "key_span_plus_mm",
        ):
            if float(getattr(self, name) or 0) < 0:
                raise ValueError(f"{name} cannot be negative")
        return self

    def cuts_key(self, include_key: bool | None = None) -> bool:
        if self.unknown or not self.key_width_mm or not self.key_span_mm:
            return False
        if include_key is not None:
            return bool(include_key)
        return self.shape == "d" and not self.key_optional

    def to_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        # A rectangle has no key. The model default ``key_side: top`` is not a setting.
        if not (self.key_width_mm and self.key_span_mm):
            for key in (
                "key_width_mm",
                "key_width_plus_mm",
                "key_span_mm",
                "key_span_plus_mm",
                "key_side",
                "key_optional",
            ):
                data.pop(key, None)
        return data


# Preferred gap between source lines when they share the title block's free column.
TITLE_LINE_MM = 4.6


def title_reserve_mm(source_count: int = 1) -> float:
    """Millimetres kept under the panel for the title block, including padding.

    The block is a fixed height. Citations sit in its free column, so a second
    or third source does not push a 600 × 800 door that fits at 892 mm over
    the 900 mm page cap (that drop is 1:2). ``source_count`` is unused and
    kept so older callers stay valid.
    """

    del source_count
    return 36.0


def resolve_centers_interchangeable(
    pitch_swap: bool | None, centers_interchangeable: bool | None
) -> bool | None:
    """One pitch-swap flag. The two names are the same setting.

    Passing both with different values is an error. ``--pitch-swap`` must not
    silently win over ``--no-centers-interchangeable``.
    """

    if (
        pitch_swap is not None
        and centers_interchangeable is not None
        and bool(pitch_swap) != bool(centers_interchangeable)
    ):
        raise ValueError("pitch-swap and centers-interchangeable disagree; pass only one")
    if pitch_swap is not None:
        return bool(pitch_swap)
    if centers_interchangeable is None:
        return None
    return bool(centers_interchangeable)


def citation_line(cutout: PanelCutout) -> str:
    """Source line for a title block. Unverified figures stay marked as such."""

    source = str(cutout.source or "").strip()
    if not source:
        return ""
    if cutout.verified:
        return source
    return f"UNVERIFIED {source}"


def parse_cutout(raw: Any) -> PanelCutout | None:
    if raw is None:
        return None
    if isinstance(raw, PanelCutout):
        return raw
    if not isinstance(raw, dict):
        raise ValueError("cutout must be a mapping")
    return PanelCutout.model_validate(raw)


@dataclass
class CutoutLibrary:
    by_library_id: dict[str, PanelCutout] = field(default_factory=dict)
    by_catalog: dict[tuple[str, str], PanelCutout] = field(default_factory=dict)


_LIBRARY: CutoutLibrary | None = None


def load_cutout_library(path: Any | None = None) -> CutoutLibrary:
    from pathlib import Path

    lib = CutoutLibrary()
    file = Path(path) if path else builtin_library_dir() / "cutouts.yaml"
    if not file.is_file():
        return lib
    data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{file}: cutout file must be a mapping")
    for raw in data.get("records") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("cutout"), dict):
            continue
        cutout = PanelCutout.model_validate(raw["cutout"])
        for library_id in raw.get("library_ids") or []:
            lib.by_library_id[str(library_id)] = cutout
        for catalog in raw.get("catalogs") or []:
            if not isinstance(catalog, dict):
                continue
            key = _catalog_key(str(catalog.get("manufacturer") or ""), str(catalog.get("catalog") or ""))
            if key[1]:
                lib.by_catalog[key] = cutout
    return lib


def cutout_library() -> CutoutLibrary:
    global _LIBRARY
    if _LIBRARY is None:
        _LIBRARY = load_cutout_library()
    return _LIBRARY


def library_cutout(library_id: str) -> PanelCutout | None:
    return cutout_library().by_library_id.get(library_id)


def catalog_cutout(manufacturer: str, catalog: str) -> PanelCutout | None:
    """Published cutout for one manufacturer SKU, if the library cites one."""

    return cutout_library().by_catalog.get(_catalog_key(manufacturer, catalog))


def _catalog_key(manufacturer: str, catalog: str) -> tuple[str, str]:
    return (str(manufacturer or "").strip().casefold(), str(catalog or "").strip().casefold())


def _apply_spacing_props(cutout: PanelCutout, props: dict[str, Any]) -> PanelCutout:
    """Overlay a per-part centre-distance edit without replacing the hole."""

    if cutout.unknown:
        return cutout
    touched = [key for key in ("min_center_x_mm", "min_center_y_mm", "centers_interchangeable") if key in props]
    if not touched:
        return cutout
    data = cutout.to_dict()
    if "min_center_x_mm" in props and props["min_center_x_mm"] not in (None, ""):
        data["min_center_x_mm"] = float(props["min_center_x_mm"])
    if "min_center_y_mm" in props and props["min_center_y_mm"] not in (None, ""):
        data["min_center_y_mm"] = float(props["min_center_y_mm"])
    if "centers_interchangeable" in props:
        data["centers_interchangeable"] = bool(props["centers_interchangeable"])
    if data.get("centers_interchangeable"):
        horizontal = data.get("min_center_x_mm") or data.get("min_center_a_mm")
        vertical = data.get("min_center_y_mm") or data.get("min_center_b_mm")
        if horizontal and vertical:
            data["min_center_a_mm"] = float(horizontal)
            data["min_center_b_mm"] = float(vertical)
            data["min_center_x_mm"] = None
            data["min_center_y_mm"] = None
    elif data.get("min_center_x_mm") and data.get("min_center_y_mm"):
        data["min_center_a_mm"] = None
        data["min_center_b_mm"] = None
    return parse_cutout(data)


def resolve_cutout(device_library_id: str, properties: dict[str, Any] | None) -> PanelCutout | None:
    """Instance override, then assigned catalog SKU, then the symbol record."""

    props = properties or {}
    if isinstance(props.get("cutout"), dict):
        return _apply_spacing_props(parse_cutout(props["cutout"]), props)
    manufacturer = str(props.get("manufacturer") or "")
    catalog = str(props.get("catalog") or props.get("part_number") or "")
    if manufacturer and catalog:
        found = cutout_library().by_catalog.get(_catalog_key(manufacturer, catalog))
        if found is not None:
            return _apply_spacing_props(found, props)
    found = library_cutout(device_library_id)
    return _apply_spacing_props(found, props) if found is not None else None


# --- geometry -----------------------------------------------------------------


def _tol(nominal: float, plus: float, minus: float) -> str:
    if plus == 0 and minus == 0:
        return ""
    return f" +{plus:g}/{minus:g}"


def size_label(cutout: PanelCutout, *, include_key: bool) -> str:
    if cutout.unknown or not cutout.shape:
        return "UNKNOWN"
    if cutout.shape == "rect":
        text = f"{cutout.width_mm:g}×{cutout.height_mm:g}"
        if cutout.corner_radius_mm:
            text += f" R{cutout.corner_radius_mm:g}"
        plus = cutout.width_plus_mm if cutout.width_plus_mm == cutout.height_plus_mm else 0
        minus = cutout.width_minus_mm if cutout.width_minus_mm == cutout.height_minus_mm else 0
        if plus or minus:
            text += _tol(0, plus, minus)
        elif cutout.width_plus_mm or cutout.height_plus_mm:
            text += f" +{cutout.width_plus_mm:g}/+{cutout.height_plus_mm:g}"
        return text
    assert cutout.diameter_mm is not None
    text = f"Ø{cutout.diameter_mm:g}" + _tol(cutout.diameter_mm, cutout.diameter_plus_mm, cutout.diameter_minus_mm)
    if include_key and cutout.cuts_key(True):
        text += " D"
    return text


def d_key_extents(diameter_mm: float, key_width_mm: float, key_span_mm: float) -> tuple[float, float]:
    """Return (intersection height from centre, outer height from centre) in mm, Y up."""

    radius = diameter_mm / 2.0
    half = key_width_mm / 2.0
    if half >= radius:
        raise ValueError("key recess is wider than the mounting hole")
    if key_span_mm <= diameter_mm:
        raise ValueError("key span must be larger than the hole diameter")
    intersect = math.sqrt(radius * radius - half * half)
    outer = key_span_mm - radius
    return intersect, outer


def opens_as_circle(cutout: PanelCutout, include_key: bool) -> bool:
    """True when the shop opening is a single circle (no key, no rectangle)."""

    return (not cutout.unknown) and cutout.shape in {"round", "d"} and not cutout.cuts_key(include_key)


def local_profile(cutout: PanelCutout, *, include_key: bool) -> list[tuple[float, float]]:
    """Closed profile around the opening centre. X right, Y down. Nominal size."""

    if cutout.shape == "rect":
        return _rounded_rect(float(cutout.width_mm or 0), float(cutout.height_mm or 0), cutout.corner_radius_mm)
    if cutout.shape in {"round", "d"} and not cutout.cuts_key(include_key):
        return _circle_points(float(cutout.diameter_mm or 0) / 2.0, 64)
    return _d_points(cutout)


def _circle_points(radius: float, count: int) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for index in range(count):
        angle = 2.0 * math.pi * index / count
        # Y down: mathematical angle uses Y up, so flip the sine.
        points.append((radius * math.cos(angle), -radius * math.sin(angle)))
    points.append(points[0])
    return points


def _rounded_rect(width: float, height: float, radius: float) -> list[tuple[float, float]]:
    half_w = width / 2.0
    half_h = height / 2.0
    radius = max(0.0, min(radius, half_w, half_h))
    if radius <= 0:
        return [
            (-half_w, -half_h),
            (half_w, -half_h),
            (half_w, half_h),
            (-half_w, half_h),
            (-half_w, -half_h),
        ]
    points: list[tuple[float, float]] = []
    # Corners in Y-down order: top-right, bottom-right, bottom-left, top-left.
    corners = (
        (half_w - radius, -half_h + radius, 90, 0),
        (half_w - radius, half_h - radius, 0, -90),
        (-half_w + radius, half_h - radius, -90, -180),
        (-half_w + radius, -half_h + radius, 180, 90),
    )
    for cx, cy, start, end in corners:
        sweep = end - start
        steps = 8
        for step in range(steps + 1):
            angle = math.radians(start + sweep * step / steps)
            points.append((cx + radius * math.cos(angle), cy - radius * math.sin(angle)))
    points.append(points[0])
    return points


def _d_points(cutout: PanelCutout) -> list[tuple[float, float]]:
    """D-hole with the key at the top, then rotated so ``key_side`` matches. Y down."""

    points = _d_points_top(cutout)
    turns = {"top": 0, "right": 90, "bottom": 180, "left": 270}
    turn = turns[cutout.key_side]
    if turn == 0:
        return points
    return [rotate_point(x, y, turn) for x, y in points]


def _d_points_top(cutout: PanelCutout) -> list[tuple[float, float]]:
    diameter = float(cutout.diameter_mm or 0)
    radius = diameter / 2.0
    key_w = float(cutout.key_width_mm or 0)
    key_span = float(cutout.key_span_mm or 0)
    intersect, outer = d_key_extents(diameter, key_w, key_span)
    half = key_w / 2.0
    # Y-up angles of the notch intersections. The short gap between them is the key.
    right = math.atan2(intersect, half)
    left = math.atan2(intersect, -half)
    short = (left - right) % (2.0 * math.pi)
    travel = -((2.0 * math.pi) - short)
    points_up: list[tuple[float, float]] = []
    steps = 48
    for step in range(steps + 1):
        angle = right + travel * step / steps
        points_up.append((radius * math.cos(angle), radius * math.sin(angle)))
    # Arc ends on the left intersection. Step out to the recess, across, and close.
    points_up.extend([(-half, outer), (half, outer)])
    points_up.append(points_up[0])
    return [(x, -y) for x, y in points_up]


@dataclass(frozen=True)
class ArcCut:
    """Circular cut in view millimetres. Angles are mathematical: 0° at +X, CCW toward page-up."""

    cx: float
    cy: float
    radius: float
    start_deg: float
    sweep_deg: float


@dataclass(frozen=True)
class LineCut:
    x1: float
    y1: float
    x2: float
    y2: float


def d_cut_paths(
    cutout: PanelCutout,
    *,
    cx: float,
    cy: float,
    rotation: int,
    panel_width: float,
    view: str,
) -> tuple[ArcCut, list[LineCut]]:
    """Keyed hole as one true arc plus the three key lines. The arc radius is the nominal diameter / 2."""

    diameter = float(cutout.diameter_mm or 0)
    radius = diameter / 2.0
    key_w = float(cutout.key_width_mm or 0)
    key_span = float(cutout.key_span_mm or 0)
    intersect, outer = d_key_extents(diameter, key_w, key_span)
    half = key_w / 2.0
    right_ang = math.degrees(math.atan2(intersect, half))
    left_ang = math.degrees(math.atan2(intersect, -half))
    sweep = (right_ang - left_ang) % 360.0
    turns = {"top": 0, "right": 90, "bottom": 180, "left": 270}
    turn = (turns[cutout.key_side] + int(rotation or 0)) % 360
    start = left_ang - turn
    # Local Y-down, key toward the top of the page, before the key/part rotation.
    corners = ((half, -intersect), (half, -outer), (-half, -outer), (-half, -intersect))
    placed: list[tuple[float, float]] = []
    for lx, ly in corners:
        rx, ry = rotate_point(lx, ly, turn)
        wx, wy = cx + rx, cy + ry
        if view == "back":
            wx = panel_width - wx
        placed.append((wx, wy))
    vx, vy = view_point(cx, cy, panel_width, view)
    if view == "back":
        # Mirroring X reverses the arc. Keep the long way around the circle.
        start = 180.0 - (start + sweep)
    lines = [
        LineCut(placed[0][0], placed[0][1], placed[1][0], placed[1][1]),
        LineCut(placed[1][0], placed[1][1], placed[2][0], placed[2][1]),
        LineCut(placed[2][0], placed[2][1], placed[3][0], placed[3][1]),
    ]
    return ArcCut(vx, vy, radius, start, sweep), lines


def local_holes(cutout: PanelCutout) -> list[tuple[float, float, float]]:
    return [(hole.x_mm, hole.y_mm, hole.diameter_mm) for hole in cutout.holes]


def opening_reach_mm(
    cutout: PanelCutout, *, include_key: bool, rotation: int, downward: bool = False
) -> float:
    """How far the opening reaches from its centre, after rotation.

    Page Y grows downward. A 60 × 20 rectangle turned 90° reaches 30 mm up
    and down, which is the unrotated width, not the 10 mm unrotated height.
    """

    if cutout.unknown or not cutout.shape:
        return 0.0
    points = list(local_profile(cutout, include_key=include_key))
    for hx, hy, diameter in local_holes(cutout):
        radius = float(diameter) / 2.0
        points.append((hx, hy - radius))
        points.append((hx, hy + radius))
    if not points:
        return 0.0
    turned = int(rotation or 0)
    ys = [rotate_point(x, y, turned)[1] for x, y in points]
    reach = max(ys) if downward else -min(ys)
    return max(0.0, float(reach))


def transform_local(
    points: Iterable[tuple[float, float]],
    *,
    cx: float,
    cy: float,
    rotation: int,
    panel_width: float,
    view: str,
) -> list[tuple[float, float]]:
    world: list[tuple[float, float]] = []
    for lx, ly in points:
        rx, ry = rotate_point(lx, ly, rotation)
        wx, wy = cx + rx, cy + ry
        if view == "back":
            wx = panel_width - wx
        world.append((wx, wy))
    return world


def view_point(x: float, y: float, panel_width: float, view: str) -> tuple[float, float]:
    if view == "back":
        return (panel_width - x, y)
    return (x, y)


def datum_xy(x: float, y: float, width: float, height: float, datum: str) -> tuple[float, float]:
    """Positive distances from the chosen corner. Y grows into the panel."""

    token = str(datum or "top_left").strip().lower()
    if token not in DATUMS:
        raise ValueError(f"datum must be one of {', '.join(DATUMS)}")
    if token == "top_left":
        return (x, y)
    if token == "top_right":
        return (width - x, y)
    if token == "bottom_left":
        return (x, height - y)
    return (width - x, height - y)


def baseline_stacks(values: list[float]) -> list[int]:
    """Stack index for each value, ordered along the baseline so lines do not share a station."""

    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    stacks = [0] * len(values)
    for rank, index in enumerate(order):
        stacks[index] = rank
    return stacks


def centers_meet(dx: float, dy: float, a_mm: float, b_mm: float) -> bool:
    """IEC Table 3 for one adjacent pair. ``a`` and ``b`` may be swapped.

    A single row or column (the other axis inside ``_ROW_ALIGN_MM``) uses the
    smaller pitch. A close grid needs one axis at each pitch. Callers must
    not apply this to operators that are not neighbours.
    """

    dx = abs(dx)
    dy = abs(dy)
    small, large = (a_mm, b_mm) if a_mm <= b_mm else (b_mm, a_mm)
    if dy <= _ROW_ALIGN_MM:
        return dx + 1e-9 >= small
    if dx <= _ROW_ALIGN_MM:
        return dy + 1e-9 >= small
    return (dx + 1e-9 >= small and dy + 1e-9 >= large) or (dx + 1e-9 >= large and dy + 1e-9 >= small)


@dataclass(frozen=True)
class CenterPitch:
    """``kind`` is ``swap`` (axes may trade) or ``axis`` (x horizontal, y vertical)."""

    kind: str
    x_mm: float
    y_mm: float


def center_pitch(cutout: PanelCutout) -> CenterPitch | None:
    """Published centre minimum for one part, or None when the source gives none."""

    if cutout.unknown:
        return None
    if cutout.centers_interchangeable and cutout.min_center_a_mm and cutout.min_center_b_mm:
        return CenterPitch("swap", float(cutout.min_center_a_mm), float(cutout.min_center_b_mm))
    if cutout.min_center_x_mm and cutout.min_center_y_mm:
        return CenterPitch("axis", float(cutout.min_center_x_mm), float(cutout.min_center_y_mm))
    if cutout.min_center_a_mm and cutout.min_center_b_mm:
        return CenterPitch("swap", float(cutout.min_center_a_mm), float(cutout.min_center_b_mm))
    return None


def _combine_pitch(left: CenterPitch, right: CenterPitch) -> CenterPitch:
    if left.kind == "swap" and right.kind == "swap":
        return CenterPitch("swap", max(left.x_mm, right.x_mm), max(left.y_mm, right.y_mm))

    def _axes(pitch: CenterPitch) -> tuple[float, float]:
        if pitch.kind == "axis":
            return pitch.x_mm, pitch.y_mm
        small, large = sorted((pitch.x_mm, pitch.y_mm))
        return small, large

    lx, ly = _axes(left)
    rx, ry = _axes(right)
    return CenterPitch("axis", max(lx, rx), max(ly, ry))


def _bezel_span(feature: CutFeature) -> tuple[float, float]:
    """Panel bezel width and height after a 90° turn. Used only to see if two operators share a row or column."""

    if int(feature.rotation or 0) % 180 == 90:
        return feature.footprint_h, feature.footprint_w
    return feature.footprint_w, feature.footprint_h


def _zones_overlap(delta: float, left_size: float, right_size: float) -> bool:
    """True when the centre on this axis falls inside the other operator's bezel."""

    band = min(max(left_size, 0.0), max(right_size, 0.0)) / 2.0
    return abs(delta) <= band + 1e-9


def spacing_failure(
    dx: float,
    dy: float,
    pitch: CenterPitch,
    left_w: float,
    left_h: float,
    right_w: float,
    right_h: float,
) -> str | None:
    """Why two operators are too close, or None.

    A pitch is enforced only for neighbours: closer than the minimum on one
    axis while the other axis overlaps the bezel, or both axes inside the
    published neighbour box. Operators half a panel apart are not a pair.
    """

    dx = abs(dx)
    dy = abs(dy)
    y_aligned = _zones_overlap(dy, left_h, right_h)
    x_aligned = _zones_overlap(dx, left_w, right_w)
    if pitch.kind == "swap":
        small, large = sorted((pitch.x_mm, pitch.y_mm))
        if y_aligned and dx + 1e-9 < small:
            return f"horizontal {dx:.1f} mm is under the {small:g} mm minimum for operators in one row"
        if x_aligned and dy + 1e-9 < small:
            return f"vertical {dy:.1f} mm is under the {small:g} mm minimum for operators in one column"
        if dx + 1e-9 < large and dy + 1e-9 < large and not (x_aligned or y_aligned):
            if not centers_meet(dx, dy, pitch.x_mm, pitch.y_mm):
                return (
                    f"{dx:.1f} × {dy:.1f} mm apart; adjacent operators need "
                    f"{pitch.x_mm:g} mm and {pitch.y_mm:g} mm (axes may be swapped)"
                )
        return None
    if y_aligned and dx + 1e-9 < pitch.x_mm:
        return f"horizontal {dx:.1f} mm is under the {pitch.x_mm:g} mm minimum"
    if x_aligned and dy + 1e-9 < pitch.y_mm:
        return f"vertical {dy:.1f} mm is under the {pitch.y_mm:g} mm minimum"
    if dx + 1e-9 < pitch.x_mm and dy + 1e-9 < pitch.y_mm and not (x_aligned or y_aligned):
        return (
            f"{dx:.1f} × {dy:.1f} mm apart; minimum centres are "
            f"{pitch.x_mm:g} mm horizontal and {pitch.y_mm:g} mm vertical"
        )
    return None


def opening_primitives(
    cutout: PanelCutout,
    *,
    include_key: bool,
    cx: float,
    cy: float,
    rotation: int,
    panel_width: float,
    view: str,
) -> list[dict[str, Any]]:
    """Circles and AABBs in view coordinates, for overlap tests."""

    if cutout.shape in {"round", "d"} and not cutout.cuts_key(include_key):
        radius = float(cutout.diameter_mm or 0) / 2.0
        vx, vy = view_point(cx, cy, panel_width, view)
        return [{"kind": "circle", "x": vx, "y": vy, "r": radius}]
    points = transform_local(
        local_profile(cutout, include_key=include_key),
        cx=cx,
        cy=cy,
        rotation=rotation,
        panel_width=panel_width,
        view=view,
    )
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    prims: list[dict[str, Any]] = []
    if cutout.shape in {"round", "d"}:
        radius = float(cutout.diameter_mm or 0) / 2.0
        vx, vy = view_point(cx, cy, panel_width, view)
        prims.append({"kind": "circle", "x": vx, "y": vy, "r": radius})
    prims.append({"kind": "aabb", "x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys)})
    return prims


def primitives_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left["kind"] == "circle" and right["kind"] == "circle":
        dx = left["x"] - right["x"]
        dy = left["y"] - right["y"]
        limit = left["r"] + right["r"]
        return dx * dx + dy * dy < limit * limit - 1e-6
    if left["kind"] == "circle" and right["kind"] == "aabb":
        return _circle_hits_aabb(left, right)
    if left["kind"] == "aabb" and right["kind"] == "circle":
        return _circle_hits_aabb(right, left)
    return _aabb_hits(left, right)


def _circle_hits_aabb(circle: dict[str, Any], box: dict[str, Any]) -> bool:
    nearest_x = min(max(circle["x"], box["x0"]), box["x1"])
    nearest_y = min(max(circle["y"], box["y0"]), box["y1"])
    dx = circle["x"] - nearest_x
    dy = circle["y"] - nearest_y
    return dx * dx + dy * dy < circle["r"] * circle["r"] - 1e-6


def _aabb_hits(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return left["x0"] < right["x1"] and left["x1"] > right["x0"] and left["y0"] < right["y1"] and left["y1"] > right["y0"]


def shape_gap_to_edges(prims: list[dict[str, Any]], width: float, height: float) -> dict[str, float]:
    """Smallest clearance from material removal to each edge. Negative means it crosses."""

    gaps = {"left": width, "right": width, "top": height, "bottom": height}
    for prim in prims:
        if prim["kind"] == "circle":
            gaps["left"] = min(gaps["left"], prim["x"] - prim["r"])
            gaps["right"] = min(gaps["right"], width - (prim["x"] + prim["r"]))
            gaps["top"] = min(gaps["top"], prim["y"] - prim["r"])
            gaps["bottom"] = min(gaps["bottom"], height - (prim["y"] + prim["r"]))
        else:
            gaps["left"] = min(gaps["left"], prim["x0"])
            gaps["right"] = min(gaps["right"], width - prim["x1"])
            gaps["top"] = min(gaps["top"], prim["y0"])
            gaps["bottom"] = min(gaps["bottom"], height - prim["y1"])
    return gaps


# --- placement ----------------------------------------------------------------


@dataclass
class CutFeature:
    ref: str
    sheet_id: str
    surface: str
    library_id: str
    catalog: str
    cx: float
    cy: float
    rotation: int
    cutout: PanelCutout
    include_key: bool
    footprint_w: float
    footprint_h: float

    def view_center(self, panel_width: float, view: str) -> tuple[float, float]:
        return view_point(self.cx, self.cy, panel_width, view)


@dataclass
class HoleRow:
    tag: str
    size: str
    qty: int
    x_mm: float
    y_mm: float
    kind: str
    ref: str


@dataclass
class DimMark:
    axis: str
    value_mm: float
    feature_x: float
    feature_y: float
    stack: int
    label: str


@dataclass
class SurfaceDrawing:
    surface: str
    panel_width_mm: float
    panel_height_mm: float
    depth_available_mm: float | None
    view: str
    datum: str
    style: str
    units: str
    scale_denom: float
    page_width_mm: float
    page_height_mm: float
    panel_origin_x: float
    panel_origin_y: float
    gutter_x: float
    gutter_y: float
    features: list[CutFeature]
    rows: list[HoleRow]
    dims: list[DimMark]
    sources: list[str]
    outline_set: bool


@dataclass
class CutoutSettings:
    datum: str = "top_left"
    style: str = "ordinate"
    view: str = "front"
    units: str = "mm"
    edge_clearance_mm: float | None = None
    flange_mm: float | None = None
    panel_thickness_mm: float | None = None
    surfaces: tuple[str, ...] | None = None

    def normalized(self) -> CutoutSettings:
        datum = str(self.datum or "top_left").strip().lower().replace("-", "_").replace(" ", "_")
        style = str(self.style or "ordinate").strip().lower()
        view = str(self.view or "front").strip().lower()
        units = str(self.units or "mm").strip().lower()
        if datum not in DATUMS:
            raise ValueError(f"datum must be one of {', '.join(DATUMS)}")
        if style not in STYLES:
            raise ValueError("style must be ordinate or baseline")
        if view not in VIEWS:
            raise ValueError("view must be front or back")
        if units in {"in", "inches"}:
            units = "inch"
        if units not in UNITS:
            raise ValueError("units must be mm or inch")
        surfaces = self.surfaces
        if surfaces is not None:
            cleaned = tuple(str(item).strip().lower() for item in surfaces if str(item).strip())
            bad = [item for item in cleaned if item not in SURFACES]
            if bad:
                raise ValueError(f"unknown surface {bad[0]}")
            surfaces = cleaned
        return CutoutSettings(
            datum=datum,
            style=style,
            view=view,
            units=units,
            edge_clearance_mm=_optional_positive(self.edge_clearance_mm, "edge clearance"),
            flange_mm=_optional_positive(self.flange_mm, "flange"),
            panel_thickness_mm=_optional_positive(self.panel_thickness_mm, "panel thickness"),
            surfaces=surfaces,
        )


def _optional_positive(value: float | None, label: str) -> float | None:
    if value is None or value == "":
        return None
    number = float(value)
    if number < 0:
        raise ValueError(f"{label} cannot be negative")
    return number


def apply_cutout_options(project: Project, options: dict[str, Any] | None) -> CutoutSettings:
    """Project cutout settings, with only the keys present in ``options`` replaced."""

    base = settings_from_project(project, None)
    if not options:
        return base
    data: dict[str, Any] = {
        "datum": base.datum,
        "style": base.style,
        "view": base.view,
        "units": base.units,
        "edge_clearance_mm": base.edge_clearance_mm,
        "flange_mm": base.flange_mm,
        "panel_thickness_mm": base.panel_thickness_mm,
        "surfaces": base.surfaces,
    }
    for key in ("datum", "style", "view", "units", "surfaces"):
        if key in options and options[key] not in (None, ""):
            data[key] = options[key]
    for key in ("edge_clearance_mm", "flange_mm", "panel_thickness_mm"):
        if key in options:
            data[key] = None if options[key] in (None, "") else options[key]
    return CutoutSettings(**data).normalized()


def write_cutout_options(project: Project, options: dict[str, Any]) -> None:
    """Store cutout sheet settings on ``project.properties``. Absent keys stay put."""

    props = project.properties
    mapping = {
        "datum": "cutout_datum",
        "style": "cutout_style",
        "view": "cutout_view",
        "units": "cutout_units",
        "edge_clearance_mm": "cutout_edge_clearance_mm",
        "flange_mm": "cutout_flange_mm",
        "panel_thickness_mm": "cutout_panel_thickness_mm",
    }
    for key, prop in mapping.items():
        if key not in options:
            continue
        value = options[key]
        if value is None or value == "":
            props.pop(prop, None)
        else:
            props[prop] = value


def settings_from_project(project: Project, override: CutoutSettings | None = None) -> CutoutSettings:
    props = project.properties or {}
    base = CutoutSettings(
        datum=str(props.get("cutout_datum") or "top_left"),
        style=str(props.get("cutout_style") or "ordinate"),
        view=str(props.get("cutout_view") or "front"),
        units=str(props.get("cutout_units") or "mm"),
        edge_clearance_mm=_prop_float(props.get("cutout_edge_clearance_mm")),
        flange_mm=_prop_float(props.get("cutout_flange_mm")),
        panel_thickness_mm=_prop_float(props.get("cutout_panel_thickness_mm")),
    )
    if override is None:
        return base.normalized()
    data = base.normalized()
    raw = override
    return CutoutSettings(
        datum=raw.datum or data.datum,
        style=raw.style or data.style,
        view=raw.view or data.view,
        units=raw.units or data.units,
        edge_clearance_mm=data.edge_clearance_mm if raw.edge_clearance_mm is None else raw.edge_clearance_mm,
        flange_mm=data.flange_mm if raw.flange_mm is None else raw.flange_mm,
        panel_thickness_mm=data.panel_thickness_mm if raw.panel_thickness_mm is None else raw.panel_thickness_mm,
        surfaces=raw.surfaces,
    ).normalized()


def _prop_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _component_surface(sheet: Sheet, component: Component) -> str:
    raw = str(component.properties.get("surface") or "").strip().lower()
    if raw in SURFACES:
        return raw
    return LAYOUT_SURFACES.get(sheet.kind, "backplate")


def _outline_enclosure(project: Project, surface: str) -> Any:
    """Door and backplate outlines come from that layout sheet when it has a box."""

    kind = "door_layout" if surface == "door" else "panel_layout" if surface == "backplate" else ""
    if not kind:
        return None
    for sheet in project.sheets:
        if sheet.kind == kind and sheet.enclosure is not None:
            return sheet.enclosure
    return None


def _box_enclosure(project: Project) -> Any:
    for sheet in project.sheets:
        if sheet.kind == "panel_layout" and sheet.enclosure is not None:
            return sheet.enclosure
    for sheet in project.sheets:
        if sheet.enclosure is not None:
            return sheet.enclosure
    return None


def _surface_size(enclosure: Any, surface: str) -> tuple[float, float, float | None]:
    if enclosure is None:
        return (0.0, 0.0, None)
    width = float(enclosure.width_mm or 0)
    height = float(enclosure.height_mm or 0)
    depth = float(enclosure.depth_mm or 0)
    if surface in {"door", "backplate"}:
        return (width, height, depth if depth > 0 else None)
    if surface in {"left", "right"}:
        return (depth, height, width if width > 0 else None)
    return (width, depth, height if height > 0 else None)


def _stored_scale(component: Component) -> float:
    """``component.scale`` with no 0.15 floor.

    ``visual_scale`` is for on-screen drawing. Shop ratios 1:8 and 1:10 are
    below that floor, so a cutout that reads the clamped scale is not catalog mm.
    """

    try:
        scale = float(component.scale)
    except (TypeError, ValueError):
        scale = 1.0
    if scale <= 1e-9:
        return 1.0
    return scale


def _catalog_body(device: Any, component: Component | None) -> tuple[float, float]:
    """Library body in millimetres. Display overrides and the draw clamp are not catalog.

    Generic table sizes are refused: cutouts must not invent a hole from a placeholder.
    """

    from panelcad.library.panel import is_layout_ready, occupies_panel_layout

    if occupies_panel_layout(device, component) and not is_layout_ready(device, component):
        return (0.0, 0.0)
    fp = effective_footprint(device, component)
    return (max(0.0, float(fp.width_mm)), max(0.0, float(fp.height_mm)))


def collect_features(project: Project, catalog: Catalog) -> dict[str, list[CutFeature]]:
    grouped: dict[str, list[CutFeature]] = {name: [] for name in SURFACES}
    for sheet in project.sheets:
        if not is_layout_sheet(sheet):
            continue
        enclosure = sheet.enclosure
        origin_x = float(enclosure.x) if enclosure is not None else 0.0
        origin_y = float(enclosure.y) if enclosure is not None else 0.0
        for component in sheet.components:
            from panelcad.library.panel import is_info_needed_placeholder

            try:
                device = catalog.get(component.library_id)
            except KeyError:
                device = None
            if is_info_needed_placeholder(component, device):
                continue
            if device is None:
                continue
            surface = _component_surface(sheet, component)
            cutout = resolve_cutout(component.library_id, component.properties)
            mount = effective_footprint(device, component).mount
            if cutout is None and mount != "door" and surface in {"door", "backplate"}:
                continue
            if cutout is None and surface not in {"left", "right", "top", "bottom"} and mount != "door":
                continue
            if cutout is None:
                cutout = PanelCutout(unknown=True, note="No cutout record for this device.")
            body_w, body_h = _catalog_body(device, component)
            if body_w <= 0 or body_h <= 0:
                # A manufacturer SKU with a cited opening may still be punched
                # when the body is not layout-ready. Use the published opening
                # only — never the generic table body for edge/flange/overlap.
                sku = str(component.properties.get("catalog") or component.properties.get("part_number") or "")
                maker = str(component.properties.get("manufacturer") or "")
                published = catalog_cutout(maker, sku) if sku else None
                if published is None or published.unknown:
                    continue
                if published.shape in {"round", "d"} and published.diameter_mm:
                    body_w = body_h = float(published.diameter_mm)
                elif published.width_mm and published.height_mm:
                    body_w = float(published.width_mm)
                    body_h = float(published.height_mm)
                else:
                    continue
            if body_w <= 0 or body_h <= 0:
                continue
            # Layout sheets store a scaled drawing. Cutout rules are true millimetres.
            # Half-size uses the stored scale, then divides by the sheet scale.
            # visual_scale() would stop at 0.15 and shift a 1:8 or 1:10 centre.
            sheet_scale = 1.0
            if enclosure is not None:
                sheet_scale = float(enclosure.draw_scale or 0.0)
                if sheet_scale <= 1e-6:
                    sheet_scale = 1.0
            part_scale = _stored_scale(component)
            width = body_w * part_scale
            height = body_h * part_scale
            local_x, local_y = rotate_point(width / 2.0, height / 2.0, int(component.rotation or 0))
            include = _include_key(component, cutout)
            grouped[surface].append(
                CutFeature(
                    ref=component.id,
                    sheet_id=sheet.id,
                    surface=surface,
                    library_id=component.library_id,
                    catalog=str(component.properties.get("catalog") or component.properties.get("part_number") or ""),
                    cx=(float(component.x) + local_x - origin_x) / sheet_scale,
                    cy=(float(component.y) + local_y - origin_y) / sheet_scale,
                    rotation=int(component.rotation or 0),
                    cutout=cutout,
                    include_key=include,
                    footprint_w=width / sheet_scale,
                    footprint_h=height / sheet_scale,
                )
            )
    return grouped


def _include_key(component: Component, cutout: PanelCutout) -> bool:
    raw = component.properties.get("include_key")
    if raw is None and isinstance(component.properties.get("cutout"), dict):
        raw = component.properties["cutout"].get("include_key")
    if raw is None:
        return cutout.cuts_key(None)
    return cutout.cuts_key(bool(raw))


def _fmt_length(value_mm: float, units: str) -> str:
    if units == "inch":
        return f"{value_mm / MM_PER_INCH:.3f}"
    if abs(value_mm - round(value_mm)) < 1e-6:
        return f"{value_mm:.0f}"
    return f"{value_mm:.1f}"


def build_drawings(project: Project, catalog: Catalog, settings: CutoutSettings | None = None) -> list[SurfaceDrawing]:
    opts = settings_from_project(project, settings)
    grouped = collect_features(project, catalog)
    enclosure = _box_enclosure(project)
    drawings: list[SurfaceDrawing] = []
    wanted = SURFACES if opts.surfaces is None else opts.surfaces
    for surface in SURFACES:
        if surface not in wanted:
            continue
        features = grouped.get(surface) or []
        # A door with no box of its own must not borrow the panel outline.
        # That borrow sets outline_set and hides the missing-outline warning.
        own = _outline_enclosure(project, surface)
        outline = own if surface == "door" else (own or enclosure)
        width, height, available = _surface_size(outline, surface)
        drawing = _compose_drawing(surface, width, height, available, features, opts)
        if features or drawing.outline_set or surface in {"door", "backplate", "left", "right"}:
            drawings.append(drawing)
    return drawings


def _compose_drawing(
    surface: str,
    width: float,
    height: float,
    available: float | None,
    features: list[CutFeature],
    opts: CutoutSettings,
) -> SurfaceDrawing:
    rows: list[HoleRow] = []
    dim_values_x: list[float] = []
    dim_values_y: list[float] = []
    dim_points: list[tuple[float, float, float, float]] = []
    sources: list[str] = []
    for feature in features:
        line = citation_line(feature.cutout)
        if line and line not in sources:
            sources.append(line)
        vx, vy = feature.view_center(width, opts.view)
        if feature.cutout.unknown:
            dx, dy = datum_xy(vx, vy, width, height, opts.datum) if width > 0 and height > 0 else (vx, vy)
            rows.append(HoleRow(feature.ref, "UNKNOWN", 0, dx, dy, "cut", feature.ref))
            continue
        _append_feature_rows(feature, width, height, opts, rows, dim_points, dim_values_x, dim_values_y)
    # One station per coordinate. Two holes on the same line share a dimension.
    x_points = _unique_stations(dim_points, 2)
    y_points = _unique_stations(dim_points, 3)
    x_values = [item[2] for item in x_points]
    y_values = [item[3] for item in y_points]
    stacks_x = baseline_stacks(x_values) if opts.style == "baseline" else _stagger(x_values)
    stacks_y = baseline_stacks(y_values) if opts.style == "baseline" else _stagger(y_values)
    dims: list[DimMark] = []
    for index, (fx, _fy, dx, _dy) in enumerate(x_points):
        dims.append(DimMark("x", dx, fx, _fy, stacks_x[index], _fmt_length(dx, opts.units)))
    for index, (_fx, fy, _dx, dy) in enumerate(y_points):
        dims.append(DimMark("y", dy, _fx, fy, stacks_y[index], _fmt_length(dy, opts.units)))
    max_x_stack = max((mark.stack for mark in dims if mark.axis == "x"), default=0)
    max_y_stack = max((mark.stack for mark in dims if mark.axis == "y"), default=0)
    gutter_x = 28.0 + max_y_stack * 8.0
    gutter_y = 20.0 + max_x_stack * 8.0
    scale, page_w, page_h, origin_x, origin_y = _fit_page(
        width, height, gutter_x, gutter_y, len(rows), opts.datum
    )
    return SurfaceDrawing(
        surface=surface,
        panel_width_mm=width,
        panel_height_mm=height,
        depth_available_mm=available,
        view=opts.view,
        datum=opts.datum,
        style=opts.style,
        units=opts.units,
        scale_denom=scale,
        page_width_mm=page_w,
        page_height_mm=page_h,
        panel_origin_x=origin_x,
        panel_origin_y=origin_y,
        gutter_x=gutter_x,
        gutter_y=gutter_y,
        features=features,
        rows=rows,
        dims=dims,
        sources=sources,
        outline_set=width > 0 and height > 0,
    )


def _append_feature_rows(
    feature: CutFeature,
    width: float,
    height: float,
    opts: CutoutSettings,
    rows: list[HoleRow],
    dim_points: list[tuple[float, float, float, float]],
    dim_values_x: list[float],
    dim_values_y: list[float],
) -> None:
    vx, vy = feature.view_center(width if width > 0 else 0.0, opts.view)
    dx, dy = datum_xy(vx, vy, width, height, opts.datum) if width > 0 and height > 0 else (vx, vy)
    rows.append(
        HoleRow(
            feature.ref,
            size_label(feature.cutout, include_key=feature.include_key),
            1,
            dx,
            dy,
            "cut",
            feature.ref,
        )
    )
    dim_points.append((vx, vy, dx, dy))
    dim_values_x.append(dx)
    dim_values_y.append(dy)
    for index, (hx, hy, diameter) in enumerate(local_holes(feature.cutout), start=1):
        rx, ry = rotate_point(hx, hy, feature.rotation)
        hole_x, hole_y = feature.cx + rx, feature.cy + ry
        hvx, hvy = view_point(hole_x, hole_y, width if width > 0 else 0.0, opts.view)
        hdx, hdy = datum_xy(hvx, hvy, width, height, opts.datum) if width > 0 and height > 0 else (hvx, hvy)
        rows.append(HoleRow(f"{feature.ref}-H{index}", f"Ø{diameter:g}", 1, hdx, hdy, "drill", feature.ref))
        dim_points.append((hvx, hvy, hdx, hdy))
        dim_values_x.append(hdx)
        dim_values_y.append(hdy)


def _unique_stations(
    points: list[tuple[float, float, float, float]], value_index: int
) -> list[tuple[float, float, float, float]]:
    kept: list[tuple[float, float, float, float]] = []
    for item in points:
        value = item[value_index]
        if any(abs(value - prev[value_index]) <= 0.4 for prev in kept):
            continue
        kept.append(item)
    return kept


def _stagger(values: list[float]) -> list[int]:
    """Ordinate text stack when two coordinates would print on top of each other."""

    order = sorted(range(len(values)), key=lambda index: values[index])
    stacks = [0] * len(values)
    last_value = None
    band = 0
    for index in order:
        value = values[index]
        if last_value is not None and abs(value - last_value) < 14.0:
            band += 1
        else:
            band = 0
        stacks[index] = band
        last_value = value
    return stacks


def _fit_page(
    panel_w: float,
    panel_h: float,
    gutter_x: float,
    gutter_y: float,
    row_count: int,
    datum: str = "top_left",
) -> tuple[float, float, float, float, float]:
    """Page size with the dimension band on the datum sides.

    ``gutter_x`` is the band for vertical dimensions, ``gutter_y`` for horizontal
    ones. A top-left datum keeps those bands above and left of the panel. A
    bottom or right datum moves the band to that edge so the lines are not
    drawn into the title block or the hole table.
    """

    margin = 10.0
    clear = 8.0
    table_w = 128.0
    table_h = 16.0 + max(1, row_count) * 6.2
    title_h = title_reserve_mm()
    from_right = str(datum).endswith("right")
    from_bottom = str(datum).startswith("bottom")
    left = clear if from_right else gutter_x
    right = gutter_x if from_right else clear
    top = clear if from_bottom else gutter_y
    bottom = gutter_y if from_bottom else clear
    scales = (1.0, 2.0, 2.5, 4.0, 5.0, 8.0, 10.0, 16.0, 20.0, 25.0, 50.0)
    real_w = panel_w if panel_w > 0 else 200.0
    real_h = panel_h if panel_h > 0 else 200.0
    chosen = scales[-1]
    for denom in scales:
        drawn_w = real_w / denom
        drawn_h = max(real_h / denom, table_h)
        page_w = margin + left + drawn_w + right + table_w + margin
        page_h = margin + top + drawn_h + bottom + title_h + margin
        if page_w <= 900.0 and page_h <= 900.0:
            chosen = denom
            break
    drawn_w = real_w / chosen
    drawn_h = max(real_h / chosen, table_h)
    page_w = margin + left + drawn_w + right + table_w + margin
    page_h = margin + top + drawn_h + bottom + title_h + margin
    origin_x = margin + left
    origin_y = margin + top
    return chosen, page_w, page_h, origin_x, origin_y


def drawing_to_dict(drawing: SurfaceDrawing) -> dict[str, Any]:
    return {
        "surface": drawing.surface,
        "width_mm": drawing.panel_width_mm,
        "height_mm": drawing.panel_height_mm,
        "view": drawing.view,
        "datum": drawing.datum,
        "style": drawing.style,
        "units": drawing.units,
        "scale": f"1:{drawing.scale_denom:g}",
        "page_width_mm": round(drawing.page_width_mm, 3),
        "page_height_mm": round(drawing.page_height_mm, 3),
        "outline": drawing.outline_set,
        "sources": list(drawing.sources),
        "features": [
            {
                "ref": feature.ref,
                "sheet": feature.sheet_id,
                "library_id": feature.library_id,
                "catalog": feature.catalog,
                "x_mm": feature.cx,
                "y_mm": feature.cy,
                "unknown": feature.cutout.unknown,
                "shape": feature.cutout.shape,
                "size": size_label(feature.cutout, include_key=feature.include_key),
                "source": feature.cutout.source,
                "include_key": feature.include_key,
            }
            for feature in drawing.features
        ],
        "holes": [
            {
                "tag": row.tag,
                "size": row.size,
                "qty": row.qty,
                "x": row.x_mm,
                "y": row.y_mm,
                "kind": row.kind,
            }
            for row in drawing.rows
        ],
    }


# --- design checks ------------------------------------------------------------


def unresolved_cutout_refs(project: Project, catalog: Catalog) -> list[tuple[str, str]]:
    """Door / layout occupants that are not layout-ready. No invented hole."""

    from panelcad.library.panel import is_layout_ready, occupies_panel_layout

    rows: list[tuple[str, str]] = []
    for sheet in project.sheets:
        if not is_layout_sheet(sheet):
            continue
        for component in sheet.components:
            try:
                device = catalog.get(component.library_id)
            except KeyError:
                continue
            if occupies_panel_layout(device, component) and not is_layout_ready(device, component):
                mount = effective_footprint(device, component).mount
                if mount == "door" or sheet.kind == "door_layout":
                    rows.append((sheet.id, component.id))
    return rows


def cutout_check_rows(project: Project, catalog: Catalog) -> list[dict[str, Any]]:
    opts = settings_from_project(project, None)
    # Warnings describe the real panel, independent of the sheet mirror.
    front = CutoutSettings(
        datum=opts.datum,
        style=opts.style,
        view="front",
        units="mm",
        edge_clearance_mm=opts.edge_clearance_mm,
        flange_mm=opts.flange_mm,
        panel_thickness_mm=opts.panel_thickness_mm,
    )
    drawings = build_drawings(project, catalog, front)
    rows: list[dict[str, Any]] = []
    for sheet_id, ref in unresolved_cutout_refs(project, catalog):
        rows.append(
            _row(
                "cutout_unresolved",
                f"{ref} is not layout-ready: no invented cutout. Pick a catalog part or confirm an envelope.",
                sheet=sheet_id,
                ref=ref,
                target="door",
            )
        )
    for drawing in drawings:
        if not drawing.outline_set and drawing.features:
            rows.append(
                _row(
                    "cutout_edge",
                    f"{_surface_name(drawing.surface)} has cutouts but no enclosure outline to dimension from.",
                    sheet=drawing.features[0].sheet_id,
                    target=drawing.surface,
                )
            )
        _pair_checks(drawing, rows, opts.edge_clearance_mm)
        for feature in drawing.features:
            _footprint_check(project, catalog, drawing, feature, rows)
            if feature.cutout.unknown:
                rows.append(
                    _row(
                        "cutout_unknown",
                        f"{feature.ref} on the {_surface_name(drawing.surface)} has no verified cutout.",
                        sheet=feature.sheet_id,
                        ref=feature.ref,
                        target=drawing.surface,
                        x=feature.cx,
                        y=feature.cy,
                    )
                )
                continue
            if drawing.outline_set:
                _edge_checks(drawing, feature, opts, rows)
            _depth_and_thickness(drawing, feature, opts, rows)
    return rows


def surface_title(surface: str) -> str:
    """Sheet title for one enclosure face."""

    return _surface_name(surface).title()


def _surface_name(surface: str) -> str:
    names = {
        "door": "door",
        "backplate": "backplate",
        "left": "left wall",
        "right": "right wall",
        "top": "top wall",
        "bottom": "bottom wall",
    }
    return names.get(surface, surface)


def _feature_prims(drawing: SurfaceDrawing, feature: CutFeature) -> list[dict[str, Any]]:
    if feature.cutout.unknown:
        half_w = max(feature.footprint_w, 1.0) / 2.0
        half_h = max(feature.footprint_h, 1.0) / 2.0
        vx, vy = feature.view_center(drawing.panel_width_mm, "front")
        return [{"kind": "aabb", "x0": vx - half_w, "y0": vy - half_h, "x1": vx + half_w, "y1": vy + half_h}]
    prims = opening_primitives(
        feature.cutout,
        include_key=feature.include_key,
        cx=feature.cx,
        cy=feature.cy,
        rotation=feature.rotation,
        panel_width=drawing.panel_width_mm,
        view="front",
    )
    for hx, hy, diameter in local_holes(feature.cutout):
        rx, ry = rotate_point(hx, hy, feature.rotation)
        vx, vy = view_point(feature.cx + rx, feature.cy + ry, drawing.panel_width_mm, "front")
        prims.append({"kind": "circle", "x": vx, "y": vy, "r": diameter / 2.0})
    return prims


def _footprint_check(
    project: Project,
    catalog: Catalog,
    drawing: SurfaceDrawing,
    feature: CutFeature,
    rows: list[dict[str, Any]],
) -> None:
    """A drill body that is not the catalog size has to be reported.

    Dividing a clamped on-screen scale by a 1:8 or 1:10 sheet scale makes a
    22 mm operator look like 26.4 mm. That must not pass quietly.
    """

    try:
        device = catalog.get(feature.library_id)
    except KeyError:
        return
    component: Component | None = None
    for sheet in project.sheets:
        if sheet.id != feature.sheet_id:
            continue
        try:
            component = sheet.component(feature.ref)
        except KeyError:
            component = None
        break
    body_w, body_h = _catalog_body(device, component)
    if body_w <= 0 or body_h <= 0:
        return
    if abs(feature.footprint_w - body_w) <= 0.05 and abs(feature.footprint_h - body_h) <= 0.05:
        return
    rows.append(
        _row(
            "cutout_edge",
            f"{feature.ref} is drawn {feature.footprint_w:.1f}×{feature.footprint_h:.1f} mm, "
            f"not its {body_w:.1f}×{body_h:.1f} mm catalog body.",
            sheet=feature.sheet_id,
            ref=feature.ref,
            target=drawing.surface,
            x=feature.cx,
            y=feature.cy,
        )
    )


def _edge_checks(drawing: SurfaceDrawing, feature: CutFeature, opts: CutoutSettings, rows: list[dict[str, Any]]) -> None:
    gaps = shape_gap_to_edges(_feature_prims(drawing, feature), drawing.panel_width_mm, drawing.panel_height_mm)
    for edge, gap in gaps.items():
        if gap < -1e-6:
            rows.append(
                _row(
                    "cutout_edge",
                    f"{feature.ref} crosses the {edge} edge of the {_surface_name(drawing.surface)}.",
                    sheet=feature.sheet_id,
                    ref=feature.ref,
                    target=edge,
                    x=feature.cx,
                    y=feature.cy,
                )
            )
        elif opts.edge_clearance_mm is not None and gap + 1e-6 < opts.edge_clearance_mm:
            rows.append(
                _row(
                    "cutout_edge",
                    f"{feature.ref} is {gap:.1f} mm from the {edge} edge of the {_surface_name(drawing.surface)} "
                    f"(minimum {opts.edge_clearance_mm:g} mm).",
                    sheet=feature.sheet_id,
                    ref=feature.ref,
                    target=edge,
                    x=feature.cx,
                    y=feature.cy,
                )
            )
        if opts.flange_mm is not None and gap + 1e-6 < opts.flange_mm:
            rows.append(
                _row(
                    "cutout_flange",
                    f"{feature.ref} is {gap:.1f} mm from the {edge} edge, inside the {opts.flange_mm:g} mm flange.",
                    sheet=feature.sheet_id,
                    ref=feature.ref,
                    target=edge,
                    x=feature.cx,
                    y=feature.cy,
                )
            )


def _pair_checks(
    drawing: SurfaceDrawing, rows: list[dict[str, Any]], edge_clearance_mm: float | None = None
) -> None:
    features = [feature for feature in drawing.features if not feature.cutout.unknown]
    prims = [(feature, _feature_prims(drawing, feature)) for feature in features]
    for index, (left, left_prims) in enumerate(prims):
        for right, right_prims in prims[index + 1 :]:
            if _sets_overlap(left_prims, right_prims):
                rows.append(
                    _row(
                        "cutout_overlap",
                        f"{left.ref} and {right.ref} overlap on the {_surface_name(drawing.surface)}.",
                        sheet=left.sheet_id,
                        ref=left.ref,
                        target=right.ref,
                        x=left.cx,
                        y=left.cy,
                    )
                )
            _spacing_check(drawing, left, right, rows, edge_clearance_mm)


def _sets_overlap(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    return any(primitives_overlap(a, b) for a in left for b in right)


def opening_span_mm(cutout: PanelCutout, axis: str) -> float:
    """Opening size on one axis. Round and D holes use the diameter on both."""

    if cutout.unknown:
        return 0.0
    if cutout.shape in {"round", "d"} and cutout.diameter_mm:
        return float(cutout.diameter_mm)
    if axis == "x":
        return float(cutout.width_mm or 0.0)
    return float(cutout.height_mm or 0.0)


def bezel_overhang_mm(cutout: PanelCutout, footprint_mm: float, axis: str) -> float:
    """How far a published bezel extends past the cut, per side. Zero when the bezel is not larger."""

    span = opening_span_mm(cutout, axis)
    if span <= 0 or footprint_mm <= span:
        return 0.0
    return (float(footprint_mm) - span) / 2.0


def implied_opening_gap_mm(cutout: PanelCutout, axis: str) -> float | None:
    """Panel left between two copies of this opening at its own centre pitch.

    ``(pitch - opening) / 2``. A swap pitch uses the smaller figure, matching
    ``centers_meet`` for a single row or column. None when this part has no pitch.
    """

    pitch = center_pitch(cutout)
    if pitch is None:
        return None
    opening = opening_span_mm(cutout, axis)
    if opening <= 0:
        return None
    if pitch.kind == "swap":
        need = min(pitch.x_mm, pitch.y_mm)
    else:
        need = pitch.x_mm if axis == "x" else pitch.y_mm
    return max(0.0, (need - opening) / 2.0)


def cross_family_clearance_mm(
    left: PanelCutout,
    right: PanelCutout,
    *,
    left_footprint: tuple[float, float],
    right_footprint: tuple[float, float],
    axis: str,
    edge_clearance_mm: float | None = None,
) -> tuple[float, str] | None:
    """Opening gap when the two centre pitches are not one family.

    A PPC5's 117 mm × 145 mm centres stay between PPC5s (manual §1.3). They are
    not applied to a 22.3 mm operator. The required gap is the larger of the
    documented keep-outs that do apply to this pair:

    - each part's ``clearance_mm``
    - the project edge clearance, when one is set
    - each part's published bezel overhang past its own cut
    - the opening gap implied by the smaller of the two centre pitches

    The smaller pitch is the one used, so the larger same-model figure is not
    stamped onto the other part. A 3 mm gap fails the 22.3 mm operator's own
    30 mm row pitch. A gap of about 10 mm does not.
    """

    candidates: list[tuple[float, str]] = []
    for cutout, footprint in ((left, left_footprint), (right, right_footprint)):
        span = footprint[0] if axis == "x" else footprint[1]
        overhang = bezel_overhang_mm(cutout, span, axis)
        if overhang > 0:
            candidates.append((overhang, f"the bezel extends {overhang:.1f} mm past the cut"))
        if cutout.clearance_mm:
            candidates.append(
                (float(cutout.clearance_mm), f"published clearance is {float(cutout.clearance_mm):g} mm")
            )
    implied: list[tuple[float, PanelCutout]] = []
    for cutout in (left, right):
        gap = implied_opening_gap_mm(cutout, axis)
        if gap is not None:
            implied.append((gap, cutout))
    if len(implied) == 2:
        gap, cutout = min(implied, key=lambda item: item[0])
        pitch = center_pitch(cutout)
        opening = opening_span_mm(cutout, axis)
        assert pitch is not None
        need = min(pitch.x_mm, pitch.y_mm) if pitch.kind == "swap" else (pitch.x_mm if axis == "x" else pitch.y_mm)
        why = (
            f"the smaller centre pitch leaves {gap:.1f} mm between openings "
            f"({need:g} mm centres on a {opening:g} mm opening)"
        )
        if not cutout.verified:
            why += "; that pitch was not checked on an official publication"
        candidates.append((gap, why))
    if edge_clearance_mm:
        candidates.append(
            (float(edge_clearance_mm), f"project edge clearance is {float(edge_clearance_mm):g} mm")
        )
    if not candidates:
        return None
    mm, why = max(candidates, key=lambda item: item[0])
    if mm <= 1e-6:
        return None
    return mm, why


def _spacing_check(
    drawing: SurfaceDrawing,
    left: CutFeature,
    right: CutFeature,
    rows: list[dict[str, Any]],
    edge_clearance_mm: float | None = None,
) -> None:
    pitch = _pair_pitch(left.cutout, right.cutout)
    if pitch is None:
        gaps = _opening_gaps(drawing, left, right)
        if gaps is None:
            return
        dx, dy = gaps
        if dx > 0 and dy > 0:
            axis = "x" if dx <= dy else "y"
        elif dy > 0:
            axis = "y"
        else:
            axis = "x"
        rule = cross_family_clearance_mm(
            left.cutout,
            right.cutout,
            left_footprint=_bezel_span(left),
            right_footprint=_bezel_span(right),
            axis=axis,
            edge_clearance_mm=edge_clearance_mm,
        )
        gap = 0.0 if dx == 0.0 and dy == 0.0 else math.hypot(dx, dy)
        if rule is not None and gap + 1e-6 < rule[0]:
            rows.append(
                _row(
                    "cutout_clearance",
                    f"{left.ref} and {right.ref} are {gap:.1f} mm apart; {rule[1]}.",
                    sheet=left.sheet_id,
                    ref=left.ref,
                    target=right.ref,
                    x=left.cx,
                    y=left.cy,
                )
            )
        return
    lw, lh = _bezel_span(left)
    rw, rh = _bezel_span(right)
    reason = spacing_failure(left.cx - right.cx, left.cy - right.cy, pitch, lw, lh, rw, rh)
    if reason is None:
        return
    rows.append(
        _row(
            "cutout_clearance",
            f"{left.ref} and {right.ref}: {reason}.",
            sheet=left.sheet_id,
            ref=left.ref,
            target=right.ref,
            x=left.cx,
            y=left.cy,
        )
    )


def spacing_family(cutout: PanelCutout) -> str:
    """Which neighbours a centre pitch was published for.

    800-TD008 pp. 47 and 89 are distances between 22.3 mm operators. The IEC
    60947-5-1 Table 3 figures are in that same hole family and are unverified
    against an official IEC publication. The PPC5 117 mm × 145 mm figures are
    centres between PPC5 controllers (manual §1.3, page 1-5), not a clearance
    to another device. A pitch applies to a pair only when both openings are
    in that family. Two different pitches in one family use the larger (see
    ``_combine_pitch``). A cross-family pair uses ``cross_family_clearance_mm``.
    """

    if (
        cutout.shape in {"round", "d"}
        and cutout.diameter_mm
        and abs(float(cutout.diameter_mm) - 22.3) <= 0.05
    ):
        return "22.3"
    source = str(cutout.source or "").strip()
    if source:
        return source
    pitch = center_pitch(cutout)
    if pitch is None:
        return ""
    return f"rule:{pitch.kind}:{pitch.x_mm:g}x{pitch.y_mm:g}"


def _pair_pitch(left: PanelCutout, right: PanelCutout) -> CenterPitch | None:
    left_pitch = center_pitch(left)
    right_pitch = center_pitch(right)
    if left_pitch is None or right_pitch is None:
        return None
    family = spacing_family(left)
    if not family or family != spacing_family(right):
        return None
    return _combine_pitch(left_pitch, right_pitch)


def _opening_gaps(drawing: SurfaceDrawing, left: CutFeature, right: CutFeature) -> tuple[float, float] | None:
    """Horizontal and vertical gaps between the two cut outlines. Zero on an axis that overlaps."""

    a = _bounds(_feature_prims(drawing, left))
    b = _bounds(_feature_prims(drawing, right))
    if a is None or b is None:
        return None
    dx = 0.0
    if a[2] < b[0]:
        dx = b[0] - a[2]
    elif b[2] < a[0]:
        dx = a[0] - b[2]
    dy = 0.0
    if a[3] < b[1]:
        dy = b[1] - a[3]
    elif b[3] < a[1]:
        dy = a[1] - b[3]
    return dx, dy


def _outline_gap(drawing: SurfaceDrawing, left: CutFeature, right: CutFeature) -> float | None:
    gaps = _opening_gaps(drawing, left, right)
    if gaps is None:
        return None
    dx, dy = gaps
    if dx == 0.0 and dy == 0.0:
        return 0.0
    return math.hypot(dx, dy)


def _bounds(prims: list[dict[str, Any]]) -> tuple[float, float, float, float] | None:
    boxes = []
    for prim in prims:
        if prim["kind"] == "circle":
            boxes.append((prim["x"] - prim["r"], prim["y"] - prim["r"], prim["x"] + prim["r"], prim["y"] + prim["r"]))
        else:
            boxes.append((prim["x0"], prim["y0"], prim["x1"], prim["y1"]))
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _depth_and_thickness(
    drawing: SurfaceDrawing, feature: CutFeature, opts: CutoutSettings, rows: list[dict[str, Any]]
) -> None:
    needed = feature.cutout.depth_behind_mm
    if needed and drawing.depth_available_mm is not None and drawing.depth_available_mm + 1e-6 < needed:
        rows.append(
            _row(
                "cutout_clearance",
                f"{feature.ref} needs {needed:g} mm behind the {_surface_name(drawing.surface)}; "
                f"the enclosure leaves {drawing.depth_available_mm:g} mm.",
                sheet=feature.sheet_id,
                ref=feature.ref,
                target="depth",
                x=feature.cx,
                y=feature.cy,
            )
        )
    thickness = opts.panel_thickness_mm
    if thickness is None:
        return
    low = feature.cutout.panel_thickness_min_mm
    high = feature.cutout.panel_thickness_max_mm
    if low is not None and thickness + 1e-6 < low:
        rows.append(
            _row(
                "cutout_clearance",
                f"Panel thickness {thickness:g} mm is below the {low:g} mm minimum published for {feature.ref}.",
                sheet=feature.sheet_id,
                ref=feature.ref,
                target="thickness",
                x=feature.cx,
                y=feature.cy,
            )
        )
    if high is not None and thickness > high + 1e-6:
        rows.append(
            _row(
                "cutout_clearance",
                f"Panel thickness {thickness:g} mm is above the {high:g} mm maximum published for {feature.ref}.",
                sheet=feature.sheet_id,
                ref=feature.ref,
                target="thickness",
                x=feature.cx,
                y=feature.cy,
            )
        )


def _row(
    check: str,
    message: str,
    *,
    sheet: str = "",
    ref: str = "",
    target: str = "",
    x: float | None = None,
    y: float | None = None,
) -> dict[str, Any]:
    return {
        "check": check,
        "severity": "warning",
        "message": message,
        "sheet": sheet,
        "ref": ref,
        "target": target,
        "x": x,
        "y": y,
    }
