"""Backpanel wire routing, interactive zip-tie waypoints, per-wire cut lengths,
Wire-by-Wire schedule generation, and Technician Wiring Walkthrough state.
"""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from panelcad.core.layout import (
    effective_footprint,
    footprint_size,
    is_layout_sheet,
)

display_footprint_size = footprint_size
from panelcad.core.models import Component, Device, Project, Sheet, WireTray
from panelcad.library.catalog import Catalog

INCH_MM = 25.4
DEFAULT_Z_DROP_MM = 42.0
DEFAULT_DUCT_DEPTH_ALLOWANCE_MM = 25.0
DEFAULT_STRIP_SLACK_MM = 30.0
CUT_INCREMENT_MM = 10.0
CUT_INCREMENT_IN = 0.5
DEFAULT_BEND_RADIUS_DRAW_MM = 11.0
CURVE_SAMPLES_PER_BEND = 16

WIRE_BY_WIRE_COLUMNS: tuple[str, ...] = (
    "step",
    "wire_number",
    "from_tag",
    "from_pin",
    "to_tag",
    "to_pin",
    "gauge",
    "color",
    "type",
    "via_path",
    "cut_length_in",
    "cut_length_mm",
    "installed",
)

_TOP_PINS = frozenset(
    {
        "1",
        "3",
        "5",
        "7",
        "11",
        "13",
        "21",
        "23",
        "31",
        "33",
        "41",
        "43",
        "53",
        "61",
        "71",
        "83",
        "L1",
        "L2",
        "L3",
        "L",
        "N",
        "PE",
        "A1",
        "PLUS",
        "+",
        "L+",
        "LPLUS",
        "24V",
        "X1",
        "H1",
        "H2",
        "H3",
        "H4",
    }
)

_BOTTOM_PINS = frozenset(
    {
        "2",
        "4",
        "6",
        "8",
        "12",
        "14",
        "22",
        "24",
        "32",
        "34",
        "42",
        "44",
        "54",
        "62",
        "72",
        "84",
        "T1",
        "T2",
        "T3",
        "T4",
        "T5",
        "T6",
        "U",
        "V",
        "W",
        "U1",
        "V1",
        "W1",
        "U2",
        "V2",
        "W2",
        "A2",
        "MINUS",
        "-",
        "L-",
        "LMINUS",
        "M",
        "0V",
        "DCM",
        "ACM",
        "GND",
        "X2",
    }
)

_POLE_FRAC: dict[str, float] = {
    "1": 0.24,
    "2": 0.24,
    "L1": 0.24,
    "T1": 0.24,
    "U1": 0.24,
    "U": 0.24,
    "11": 0.28,
    "12": 0.28,
    "13": 0.28,
    "14": 0.28,
    "PLUS": 0.30,
    "+": 0.30,
    "L+": 0.30,
    "LPLUS": 0.30,
    "3": 0.50,
    "4": 0.50,
    "L2": 0.50,
    "T2": 0.50,
    "V1": 0.50,
    "V": 0.50,
    "21": 0.46,
    "22": 0.46,
    "23": 0.46,
    "24": 0.46,
    "MINUS": 0.68,
    "-": 0.68,
    "L-": 0.68,
    "LMINUS": 0.68,
    "M": 0.68,
    "5": 0.76,
    "6": 0.76,
    "L3": 0.76,
    "T3": 0.76,
    "W1": 0.76,
    "W": 0.76,
    "31": 0.64,
    "32": 0.64,
    "33": 0.64,
    "34": 0.64,
    "7": 0.84,
    "8": 0.84,
    "41": 0.80,
    "42": 0.80,
    "43": 0.80,
    "44": 0.80,
    "A1": 0.82,
    "A2": 0.82,
    "X1": 0.35,
    "X2": 0.65,
}


@dataclass(frozen=True)
class PinLanding:
    """Physical landing point of one device pin on a panel layout sheet."""

    ref: str
    pin: str
    x: float
    y: float
    z_mm: float
    side: Literal["top", "bottom", "left", "right"] = "top"
    comp_x: float = 0.0
    comp_y: float = 0.0
    comp_w: float = 0.0
    comp_h: float = 0.0
    synthetic_exit: bool = False


@dataclass
class ZipTieWaypoint:
    """Interactive zip-tie bundle waypoint on a panel layout sheet."""

    id: str
    x: float
    y: float
    orientation: Literal["h", "v"] = "h"
    wire_numbers: list[str] = field(default_factory=list)
    slice_line: tuple[float, float, float, float] | None = None
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "x": round(float(self.x), 2),
            "y": round(float(self.y), 2),
            "orientation": self.orientation,
            "wire_numbers": [str(w) for w in self.wire_numbers],
        }
        if self.slice_line is not None:
            data["slice_line"] = [round(float(v), 2) for v in self.slice_line]
        if self.label:
            data["label"] = self.label
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ZipTieWaypoint:
        sl = raw.get("slice_line")
        slice_tuple = (
            (float(sl[0]), float(sl[1]), float(sl[2]), float(sl[3]))
            if isinstance(sl, (list, tuple)) and len(sl) == 4
            else None
        )
        orient = str(raw.get("orientation") or "h").lower()
        return cls(
            id=str(raw.get("id") or "zt-1"),
            x=float(raw.get("x") or 0.0),
            y=float(raw.get("y") or 0.0),
            orientation="v" if orient == "v" else "h",
            wire_numbers=[str(w) for w in (raw.get("wire_numbers") or [])],
            slice_line=slice_tuple,
            label=str(raw.get("label") or ""),
        )


WireBundleTie = ZipTieWaypoint


@dataclass
class PanelWireRoute:
    """Routed 2D backpanel path and 3D physical cut length for one wire."""

    step_index: int
    wire_id: str
    wire_number: str
    from_ref: str
    from_pin: str
    to_ref: str
    to_pin: str
    gauge: str
    color: str
    wire_type: str
    points: list[tuple[float, float]]
    via: list[str]
    cut_length_mm: float
    cut_length_in: float
    raw_length_mm: float
    path_2d_mm: float
    z_drop_from_mm: float = DEFAULT_Z_DROP_MM
    z_drop_to_mm: float = DEFAULT_Z_DROP_MM
    duct_depth_allowance_mm: float = DEFAULT_DUCT_DEPTH_ALLOWANCE_MM
    strip_slack_mm: float = DEFAULT_STRIP_SLACK_MM
    installed: bool = False
    from_landing: PinLanding | None = None
    to_landing: PinLanding | None = None

    @property
    def smooth_points(self) -> list[tuple[float, float]]:
        return self.points

    @property
    def length_2d_mm(self) -> float:
        return self.path_2d_mm

    @property
    def from_tag(self) -> str:
        return self.from_ref

    @property
    def to_tag(self) -> str:
        return self.to_ref

    @property
    def from_endpoint(self) -> str:
        return f"{self.from_ref}:{self.from_pin}" if self.from_pin else self.from_ref

    @property
    def to_endpoint(self) -> str:
        return f"{self.to_ref}:{self.to_pin}" if self.to_pin else self.to_ref

    @property
    def via_path(self) -> str:
        return " -> ".join(self.via) if self.via else "direct"

    @property
    def length_mm(self) -> float:
        return self.cut_length_mm

    @property
    def length_in(self) -> float:
        return self.cut_length_in

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": str(self.step_index + 1),
            "step_index": self.step_index,
            "wire_id": self.wire_id,
            "wire_number": self.wire_number,
            "from_tag": self.from_ref,
            "from_pin": self.from_pin,
            "from": self.from_endpoint,
            "to_tag": self.to_ref,
            "to_pin": self.to_pin,
            "to": self.to_endpoint,
            "gauge": self.gauge,
            "color": self.color,
            "type": self.wire_type,
            "via": list(self.via),
            "via_path": self.via_path,
            "cut_length_mm": self.cut_length_mm,
            "cut_length_in": self.cut_length_in,
            "length_mm": self.cut_length_mm,
            "length_in": self.cut_length_in,
            "raw_length_mm": round(self.raw_length_mm, 1),
            "path_2d_mm": round(self.path_2d_mm, 1),
            "length_2d_mm": round(self.path_2d_mm, 1),
            "points": [(round(x, 2), round(y, 2)) for x, y in self.points],
            "installed": "Yes" if self.installed else "No",
            "installed_bool": self.installed,
        }


@dataclass
class WireByWireSchedule:
    """Complete Wire-by-Wire cut & routing schedule for a panel layout sheet."""

    sheet_id: str
    rows: list[PanelWireRoute]
    zip_ties: list[ZipTieWaypoint] = field(default_factory=list)
    draw_scale: float = 1.0

    @property
    def total_wires(self) -> int:
        return len(self.rows)

    @property
    def installed_wires(self) -> int:
        return sum(1 for r in self.rows if r.installed)

    @property
    def total_length_mm(self) -> float:
        return round(sum(r.cut_length_mm for r in self.rows), 1)

    @property
    def total_length_in(self) -> float:
        return round(sum(r.cut_length_in for r in self.rows), 1)

    def to_dicts(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.rows]


def _unwrap_project_and_catalog(
    session_or_project: Any,
    catalog: Catalog | None = None,
) -> tuple[Project, Catalog | None]:
    if isinstance(session_or_project, Project):
        return session_or_project, catalog
    proj = getattr(session_or_project, "project", None)
    cat = catalog or getattr(session_or_project, "catalog", None)
    if isinstance(proj, Project):
        return proj, cat
    raise TypeError(f"Expected Session or Project, got {type(session_or_project)!r}")


def find_layout_sheet(project: Project, sheet_id: str | None = None) -> Sheet:
    """Return the requested layout sheet, or the first panel_layout / layout sheet."""

    if sheet_id:
        sheet = project.sheet(sheet_id)
        if sheet.kind in ("wire_by_wire", "wire_schedule"):
            target = str((sheet.properties or {}).get("layout_sheet_id") or "").strip()
            if target:
                return project.sheet(target)
        if is_layout_sheet(sheet):
            return sheet
    for s in project.sheets:
        if s.kind in ("panel_layout", "layout") and (s.components or s.trays or (s.properties or {}).get("layout_items")):
            return s
    for s in project.sheets:
        if is_layout_sheet(s):
            return s
    raise ValueError("Project has no panel_layout sheet")


def _sheet_draw_scale(sheet: Sheet) -> float:
    if sheet.enclosure is not None and sheet.enclosure.draw_scale and sheet.enclosure.draw_scale > 0:
        return float(sheet.enclosure.draw_scale)
    for comp in sheet.components:
        if comp.scale and comp.scale > 0 and abs(float(comp.scale) - 1.0) > 1e-3:
            return float(comp.scale)
    return 1.0


def _infer_pin_side_on_footprint(pin: str, device: Device | None = None) -> Literal["top", "bottom"]:
    token = str(pin or "").strip().upper()
    if not token:
        return "top"
    if token in _TOP_PINS:
        return "top"
    if token in _BOTTOM_PINS:
        return "bottom"
    if token.startswith(("I0", "I1", "DI", "AI", "COM", "ETH", "IN", "X")):
        return "top"
    if token.startswith(("Q0", "Q1", "DO", "AO", "Y", "OUT")):
        return "bottom"
    if token.isdigit():
        num = int(token)
        return "top" if num % 2 == 1 else "bottom"
    if device is not None:
        for p in device.pins:
            if p.id.upper() == token:
                if p.side in ("top", "left"):
                    return "top"
                if p.side in ("bottom", "right"):
                    return "bottom"
                return "top" if p.y <= device.schematic_height / 2.0 else "bottom"
    return "top"


def _infer_pin_fraction(pin: str, device: Device | None = None) -> float:
    token = str(pin or "").strip().upper()
    if token in _POLE_FRAC:
        return _POLE_FRAC[token]
    m = re.search(r"(\d+)$", token)
    if m:
        idx = int(m.group(1))
        return 0.20 + ((idx * 19) % 61) / 100.0
    if device is not None and device.pins:
        for idx, p in enumerate(device.pins):
            if p.id.upper() == token:
                count = max(1, len(device.pins))
                return 0.20 + 0.60 * (idx / max(1, count - 1))
    acc = sum((i + 1) * ord(ch) for i, ch in enumerate(token))
    return 0.22 + (acc % 57) / 100.0


def _component_boxes_on_sheet(
    sheet: Sheet,
    catalog: Catalog | None,
) -> dict[str, tuple[float, float, float, float, float, Device | None]]:
    """Map ref -> (x, y, w, h, z_drop_mm, device) in drawn sheet millimetres."""

    scale = _sheet_draw_scale(sheet)
    boxes: dict[str, tuple[float, float, float, float, float, Device | None]] = {}
    for comp in sheet.components:
        device: Device | None = None
        if catalog is not None:
            try:
                device = catalog.get(comp.library_id)
            except KeyError:
                device = None
        if device is not None:
            w, h = display_footprint_size(device, comp)
            depth = float(getattr(device.panel, "depth_mm", 70.0) or 70.0)
        else:
            w = float(comp.properties.get("width_mm") or 25.0) * scale
            h = float(comp.properties.get("height_mm") or 75.0) * scale
            depth = float(comp.properties.get("depth_mm") or 70.0)
        z_drop = round(max(25.0, min(90.0, depth * 0.6)), 1)
        boxes[comp.id] = (float(comp.x), float(comp.y), max(4.0, float(w)), max(4.0, float(h)), z_drop, device)
    raw_items = (sheet.properties or {}).get("layout_items") or (sheet.properties or {}).get("items") or []
    for item in raw_items:
        row = item.to_row() if hasattr(item, "to_row") else (item if isinstance(item, dict) else {})
        kind = str(row.get("kind") or "")
        ref = str(row.get("ref") or "").strip()
        if not ref or kind in ("duct", "rail", "enclosure"):
            continue
        if ref not in boxes:
            x = float(row.get("x") or 0.0)
            y = float(row.get("y") or 0.0)
            w = max(4.0, float(row.get("w") or 20.0))
            h = max(4.0, float(row.get("h") or 40.0))
            boxes[ref] = (x, y, w, h, DEFAULT_Z_DROP_MM, None)
    return boxes


def resolve_pin_landing(
    sheet: Sheet,
    catalog: Catalog | None,
    ref: str,
    pin: str,
    *,
    boxes: dict[str, tuple[float, float, float, float, float, Device | None]] | None = None,
) -> PinLanding | None:
    """Resolve ``ref:pin`` to its `(x, y, z_mm, side)` landing on `sheet`."""

    comp_boxes = boxes if boxes is not None else _component_boxes_on_sheet(sheet, catalog)
    tag = str(ref or "").strip()
    if not tag:
        return None
    hit = comp_boxes.get(tag)
    if hit is None:
        upper = tag.upper()
        for k, v in comp_boxes.items():
            if k.upper() == upper:
                hit = v
                tag = k
                break
    if hit is None:
        return None
    cx, cy, cw, ch, z_drop, device = hit
    side = _infer_pin_side_on_footprint(pin, device)
    frac = _infer_pin_fraction(pin, device)
    px = round(cx + cw * frac, 2)
    py = round(cy if side == "top" else cy + ch, 2)
    return PinLanding(
        ref=tag,
        pin=str(pin or "").strip(),
        x=px,
        y=py,
        z_mm=z_drop,
        side=side,
        comp_x=cx,
        comp_y=cy,
        comp_w=cw,
        comp_h=ch,
    )


@dataclass
class _DuctChannel:
    id: str
    orientation: Literal["h", "v"]
    coord: float  # y for 'h', x for 'v'
    span_min: float
    span_max: float
    width_mm: float = 16.0


def _extract_sheet_trays(sheet: Sheet) -> list[WireTray]:
    """Return all physical WireTray ducts defined on `sheet` (from `sheet.trays` and `layout_items`)."""

    trays: list[WireTray] = list(sheet.trays or [])
    seen_ids = {t.id for t in trays}
    raw_items = (sheet.properties or {}).get("layout_items") or (sheet.properties or {}).get("items") or []
    for item in raw_items:
        row = item.to_row() if hasattr(item, "to_row") else (item if isinstance(item, dict) else {})
        if str(row.get("kind") or "") != "duct":
            continue
        ident = str(row.get("id") or row.get("ref") or f"wt-{len(trays)+1}")
        if ident in seen_ids:
            continue
        x = float(row.get("x") or 0.0)
        y = float(row.get("y") or 0.0)
        if "length_mm" in row or "width_mm" in row:
            length_mm = float(row.get("length_mm") or 100.0)
            width_mm = float(row.get("width_mm") or 16.0)
            orient = str(row.get("orientation") or "h").lower()
            orient_lit: Literal["h", "v"] = "v" if orient.startswith("v") else "h"
            trays.append(WireTray(id=ident, x=x, y=y, length_mm=length_mm, width_mm=width_mm, orientation=orient_lit))
        else:
            w = float(row.get("w") or 16.0)
            h = float(row.get("h") or 16.0)
            if h >= w:
                trays.append(WireTray(id=ident, x=x, y=y, length_mm=h, width_mm=w, orientation="v"))
            else:
                trays.append(WireTray(id=ident, x=x, y=y, length_mm=w, width_mm=h, orientation="h"))
        seen_ids.add(ident)
    return trays


def _build_duct_channels(
    sheet: Sheet,
    boxes: dict[str, tuple[float, float, float, float, float, Device | None]],
) -> list[_DuctChannel]:
    channels: list[_DuctChannel] = []
    trays = _extract_sheet_trays(sheet)

    for tray in trays:
        if tray.orientation == "h":
            yc = float(tray.y) + float(tray.width_mm) / 2.0
            channels.append(
                _DuctChannel(
                    id=tray.id,
                    orientation="h",
                    coord=yc,
                    span_min=float(tray.x),
                    span_max=float(tray.x) + float(tray.length_mm),
                    width_mm=float(tray.width_mm),
                )
            )
        else:
            xc = float(tray.x) + float(tray.width_mm) / 2.0
            channels.append(
                _DuctChannel(
                    id=tray.id,
                    orientation="v",
                    coord=xc,
                    span_min=float(tray.y),
                    span_max=float(tray.y) + float(tray.length_mm),
                    width_mm=float(tray.width_mm),
                )
            )

    # Fallback only when a sheet has no trays defined at all
    if not channels and boxes:
        x_min = min(b[0] for b in boxes.values()) - 15.0
        x_max = max(b[0] + b[2] for b in boxes.values()) + 15.0
        y_min = min(b[1] for b in boxes.values()) - 12.0
        y_max = max(b[1] + b[3] for b in boxes.values()) + 12.0
        channels.append(_DuctChannel(id="wt-top", orientation="h", coord=y_min, span_min=x_min, span_max=x_max))
        channels.append(_DuctChannel(id="wt-bot", orientation="h", coord=y_max, span_min=x_min, span_max=x_max))
        channels.append(_DuctChannel(id="wt-left", orientation="v", coord=x_min, span_min=y_min, span_max=y_max))
        channels.append(_DuctChannel(id="wt-right", orientation="v", coord=x_max, span_min=y_min, span_max=y_max))

    return channels


def _pick_entry_duct(
    landing: PinLanding,
    h_ducts: list[_DuctChannel],
) -> _DuctChannel:
    """Pick the nearest horizontal wire tray in the terminal's exit direction."""

    if landing.side == "top":
        above = [d for d in h_ducts if d.coord <= landing.y + 1.0]
        if above:
            return min(above, key=lambda d: abs(landing.y - d.coord))
    else:
        below = [d for d in h_ducts if d.coord >= landing.y - 1.0]
        if below:
            return min(below, key=lambda d: abs(d.coord - landing.y))
    return min(h_ducts, key=lambda d: abs(d.coord - landing.y))


def _dedup_points(
    pts: list[tuple[float, float]],
    min_dist: float = 0.08,
) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for x, y in pts:
        p = (round(float(x), 3), round(float(y), 3))
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) >= min_dist:
            out.append(p)
    if len(out) == 1 and len(pts) >= 2:
        out.append((round(float(pts[-1][0]), 3), round(float(pts[-1][1]), 3)))
    return out


def _simplify_polyline(
    pts: list[tuple[float, float]],
    preserve: set[tuple[float, float]] | None = None,
) -> list[tuple[float, float]]:
    """Remove duplicate and collinear interior vertices while preserving explicit waypoints."""

    dedup = _dedup_points(pts, min_dist=0.05)
    if len(dedup) <= 2:
        return dedup
    out: list[tuple[float, float]] = [dedup[0]]
    for i in range(1, len(dedup) - 1):
        ax, ay = out[-1]
        bx, by = dedup[i]
        cx, cy = dedup[i + 1]
        if preserve and any(math.hypot(bx - px, by - py) < 0.15 for px, py in preserve):
            out.append((bx, by))
            continue
        if (abs(ax - bx) < 0.05 and abs(bx - cx) < 0.05) or (abs(ay - by) < 0.05 and abs(by - cy) < 0.05):
            continue
        out.append((bx, by))
    out.append(dedup[-1])
    return out


def _polyline_length(pts: list[tuple[float, float]]) -> float:
    total = 0.0
    for i in range(len(pts) - 1):
        total += math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
    return total


def max_turn_angle_deg(pts: list[tuple[float, float]], *, min_seg_len: float = 0.05) -> float:
    """Return the maximum consecutive segment turn angle (in degrees, `0..180`) along `pts`.

    A straight line has `0.0°` turn angle; a sharp right-angle 90° bend has `90.0°`.
    Smoothly swept physical wires have small incremental turn angles (`< 22°` per step)
    and zero 90° corners.
    """

    clean: list[tuple[float, float]] = []
    for p in pts:
        if not clean or math.hypot(p[0] - clean[-1][0], p[1] - clean[-1][1]) >= min_seg_len:
            clean.append(p)
    if len(clean) < 3:
        return 0.0
    worst = 0.0
    for i in range(1, len(clean) - 1):
        v1x = clean[i][0] - clean[i - 1][0]
        v1y = clean[i][1] - clean[i - 1][1]
        v2x = clean[i + 1][0] - clean[i][0]
        v2y = clean[i + 1][1] - clean[i][1]
        l1 = math.hypot(v1x, v1y)
        l2 = math.hypot(v2x, v2y)
        if l1 < 1e-6 or l2 < 1e-6:
            continue
        cos_t = max(-1.0, min(1.0, (v1x * v2x + v1y * v2y) / (l1 * l2)))
        ang = math.degrees(math.acos(cos_t))
        if ang > worst:
            worst = ang
    return round(worst, 2)


def _sample_cubic_bezier(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    steps: int = CURVE_SAMPLES_PER_BEND,
) -> list[tuple[float, float]]:
    """Sample a cubic Bézier curve `B(t)` for `t in [0, 1]` into `steps + 1` points."""

    n = max(8, int(steps))
    out: list[tuple[float, float]] = []
    for i in range(n + 1):
        t = i / n
        u = 1.0 - t
        b0 = u * u * u
        b1 = 3.0 * u * u * t
        b2 = 3.0 * u * t * t
        b3 = t * t * t
        x = b0 * p0[0] + b1 * p1[0] + b2 * p2[0] + b3 * p3[0]
        y = b0 * p0[1] + b1 * p1[1] + b2 * p2[1] + b3 * p3[1]
        out.append((round(x, 3), round(y, 3)))
    return out


def _smooth_corner_fillet_polyline(
    control_pts: list[tuple[float, float]],
    *,
    bend_radius: float = 4.2,
    samples_per_corner: int = CURVE_SAMPLES_PER_BEND,
    preserve: set[tuple[float, float]] | None = None,
) -> list[tuple[float, float]]:
    """Replace every interior corner in `control_pts` with a compact smooth bend sweep
    that stays strictly inside the wire tray boundaries (`bend_radius <= 4.2 mm`).
    """

    pts = _simplify_polyline(control_pts, preserve=preserve)
    if len(pts) <= 2:
        return pts

    seg_lengths = [
        math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
        for i in range(len(pts) - 1)
    ]

    result: list[tuple[float, float]] = [pts[0]]
    kappa = 0.55228475

    for i in range(1, len(pts) - 1):
        prev_pt = pts[i - 1]
        curr_pt = pts[i]
        next_pt = pts[i + 1]

        if preserve and any(math.hypot(curr_pt[0] - px, curr_pt[1] - py) < 0.15 for px, py in preserve):
            result.append(curr_pt)
            continue

        len_in = seg_lengths[i - 1]
        len_out = seg_lengths[i]
        if len_in < 0.25 or len_out < 0.25:
            result.append(curr_pt)
            continue

        ux_in = (curr_pt[0] - prev_pt[0]) / len_in
        uy_in = (curr_pt[1] - prev_pt[1]) / len_in
        ux_out = (next_pt[0] - curr_pt[0]) / len_out
        uy_out = (next_pt[1] - curr_pt[1]) / len_out

        dot = max(-1.0, min(1.0, ux_in * ux_out + uy_in * uy_out))
        if dot > 0.999:
            continue

        limit_in = 0.80 * len_in if i == 1 else 0.45 * len_in
        limit_out = 0.80 * len_out if i == len(pts) - 2 else 0.45 * len_out
        r = min(float(bend_radius), limit_in, limit_out)
        if r < 0.25:
            result.append(curr_pt)
            continue

        entry = (curr_pt[0] - ux_in * r, curr_pt[1] - uy_in * r)
        exit_ = (curr_pt[0] + ux_out * r, curr_pt[1] + uy_out * r)
        c1 = (entry[0] + ux_in * r * kappa, entry[1] + uy_in * r * kappa)
        c2 = (exit_[0] - ux_out * r * kappa, exit_[1] - uy_out * r * kappa)

        curve = _sample_cubic_bezier(entry, c1, c2, exit_, steps=samples_per_corner)
        result.extend(curve)

    result.append(pts[-1])
    return _dedup_points(result, min_dist=0.04)


def _lane_offset_for_step(step_index: int, duct_width_mm: float = 16.0) -> float:
    """Deterministic parallel lane offset inside a tray (`[-2.25, +2.25] mm` for 16 mm tray)."""

    slot = (int(step_index) % 7) - 3
    max_off = max(0.8, min(2.25, float(duct_width_mm) * 0.15))
    return round((slot / 3.0) * max_off, 3)


def _build_in_tray_skeleton(
    start: PinLanding,
    end: PinLanding,
    channels: list[_DuctChannel],
    *,
    step_index: int = 0,
    ties: list[ZipTieWaypoint] | None = None,
) -> tuple[list[tuple[float, float]], list[_DuctChannel]]:
    """Build the orthogonal in-tray skeleton from `start` into the nearest horizontal tray,
    along the tray network, and straight out to `end`.
    Returns `(skeleton_points, ducts_along_segments)`.
    """

    h_ducts = [c for c in channels if c.orientation == "h"]
    v_ducts = [c for c in channels if c.orientation == "v"]
    if not h_ducts:
        mid_y = round((start.y + end.y) / 2.0, 2)
        dummy = _DuctChannel(id="direct", orientation="h", coord=mid_y, span_min=min(start.x, end.x), span_max=max(start.x, end.x))
        return [(start.x, start.y), (start.x, mid_y), (end.x, mid_y), (end.x, end.y)], [dummy]

    d_start = _pick_entry_duct(start, h_ducts)
    d_end = _pick_entry_duct(end, h_ducts)

    off_s = _lane_offset_for_step(step_index, d_start.width_mm)
    off_e = _lane_offset_for_step(step_index, d_end.width_mm)
    y_start = round(d_start.coord + off_s, 3)
    y_end = round(d_end.coord + off_e, 3)

    # Check if any assigned zip-tie forces routing through a vertical duct
    tie_forces_v = False
    if ties and v_ducts:
        for t in ties:
            if any(abs(t.x - vd.coord) <= max(10.0, vd.width_mm * 0.75) for vd in v_ducts):
                if abs(t.y - d_start.coord) > d_start.width_mm * 0.65:
                    tie_forces_v = True

    if (d_start.id == d_end.id or abs(d_start.coord - d_end.coord) < 0.5) and not tie_forces_v:
        pts = [
            (start.x, start.y),
            (start.x, y_start),
            (end.x, y_start),
            (end.x, end.y),
        ]
        return pts, [d_start]

    if v_ducts:
        best_vd = v_ducts[0]
        best_score = float("inf")
        for vd in v_ducts:
            off_v = _lane_offset_for_step(step_index, vd.width_mm)
            xv = vd.coord + off_v
            cand_pts = [
                (start.x, start.y),
                (start.x, y_start),
                (xv, y_start),
                (xv, y_end),
                (end.x, y_end),
                (end.x, end.y),
            ]
            score = _polyline_length(cand_pts)
            if ties:
                # Strongly prefer the vertical duct that hosts an assigned zip-tie
                for t in ties:
                    if abs(t.x - vd.coord) <= max(25.0, vd.width_mm * 1.5):
                        score -= 500.0
            if score < best_score:
                best_score = score
                best_vd = vd

        off_v = _lane_offset_for_step(step_index, best_vd.width_mm)
        x_vert = round(best_vd.coord + off_v, 3)
        pts = [
            (start.x, start.y),
            (start.x, y_start),
            (x_vert, y_start),
            (x_vert, y_end),
            (end.x, y_end),
            (end.x, end.y),
        ]
        return pts, [d_start, best_vd, d_end]

    pts = [
        (start.x, start.y),
        (start.x, y_start),
        (end.x, y_start),
        (end.x, end.y),
    ]
    return pts, [d_start]


def _route_via_ducts(
    start: PinLanding,
    end: PinLanding,
    channels: list[_DuctChannel],
    *,
    bend_radius: float = 4.0,
    step_index: int = 0,
) -> tuple[list[tuple[float, float]], list[str]]:
    """Route a wire from `start` into the nearest tray, strictly along the tray interior,
    and straight out to `end` with compact smooth in-tray corner radii.
    """

    skel_pts, skel_ducts = _build_in_tray_skeleton(start, end, channels, step_index=step_index)
    min_w = min((d.width_mm for d in skel_ducts), default=16.0)
    # Keep corner fillet radius <= 0.25 * width_mm so the entire 90-deg turn stays inside the tray walls
    r_in_tray = min(4.1, max(2.8, min_w * 0.25), float(bend_radius))
    via: list[str] = []
    for d in skel_ducts:
        if not via or via[-1] != d.id:
            via.append(d.id)
    return _smooth_corner_fillet_polyline(skel_pts, bend_radius=r_in_tray), via


def _sample_in_tray_pinch_segment(
    p_start: tuple[float, float],
    p_end: tuple[float, float],
    seg_ties: list[ZipTieWaypoint],
    orientation: Literal["h", "v"],
) -> list[tuple[float, float]]:
    """Sample a straight tray segment `p_start -> p_end` that smoothly pinches to each
    in-tray `ZipTieWaypoint` `(tie.x, tie.y)` and returns to the wire's lane without leaving the tray.
    """

    if not seg_ties:
        return [p_start, p_end]

    out: list[tuple[float, float]] = [p_start]
    if orientation == "v":
        x_lane = p_start[0]
        y0, y1 = p_start[1], p_end[1]
        direction = 1.0 if y1 >= y0 else -1.0
        ordered = sorted(seg_ties, key=lambda t: (t.y - y0) * direction)
        curr_y = y0
        for tie in ordered:
            tx, ty = round(float(tie.x), 3), round(float(tie.y), 3)
            if abs(tx - x_lane) < 0.08:
                out.append((tx, ty))
                curr_y = ty
                continue
            avail_before = (ty - curr_y) * direction
            avail_after = (y1 - ty) * direction
            span_before = min(13.0, max(4.0, avail_before * 0.88))
            span_after = min(13.0, max(4.0, avail_after * 0.88))
            if avail_before >= 4.0 and avail_after >= 4.0:
                n_half = 8
                for k in range(-n_half, 0):
                    frac = k / n_half  # -1.0 .. 0
                    yk = ty + direction * frac * span_before
                    w = 0.5 * (1.0 + math.cos(math.pi * frac))
                    xk = x_lane + (tx - x_lane) * w
                    out.append((round(xk, 3), round(yk, 3)))
                out.append((tx, ty))
                for k in range(1, n_half + 1):
                    frac = k / n_half  # 0 .. +1.0
                    yk = ty + direction * frac * span_after
                    w = 0.5 * (1.0 + math.cos(math.pi * frac))
                    xk = x_lane + (tx - x_lane) * w
                    out.append((round(xk, 3), round(yk, 3)))
                curr_y = ty + direction * span_after
            else:
                out.append((tx, ty))
        out.append(p_end)
    else:
        y_lane = p_start[1]
        x0, x1 = p_start[0], p_end[0]
        direction = 1.0 if x1 >= x0 else -1.0
        ordered = sorted(seg_ties, key=lambda t: (t.x - x0) * direction)
        curr_x = x0
        for tie in ordered:
            tx, ty = round(float(tie.x), 3), round(float(tie.y), 3)
            if abs(ty - y_lane) < 0.08:
                out.append((tx, ty))
                curr_x = tx
                continue
            avail_before = (tx - curr_x) * direction
            avail_after = (x1 - tx) * direction
            span_before = min(13.0, max(4.0, avail_before * 0.88))
            span_after = min(13.0, max(4.0, avail_after * 0.88))
            if avail_before >= 4.0 and avail_after >= 4.0:
                n_half = 8
                for k in range(-n_half, 0):
                    frac = k / n_half
                    xk = tx + direction * frac * span_before
                    w = 0.5 * (1.0 + math.cos(math.pi * frac))
                    yk = y_lane + (ty - y_lane) * w
                    out.append((round(xk, 3), round(yk, 3)))
                out.append((tx, ty))
                for k in range(1, n_half + 1):
                    frac = k / n_half
                    xk = tx + direction * frac * span_after
                    w = 0.5 * (1.0 + math.cos(math.pi * frac))
                    yk = y_lane + (ty - y_lane) * w
                    out.append((round(xk, 3), round(yk, 3)))
                curr_x = tx + direction * span_after
            else:
                out.append((tx, ty))
        out.append(p_end)

    return _dedup_points(out, min_dist=0.04)


def _route_via_zip_ties(
    start: PinLanding,
    end: PinLanding,
    ties: list[ZipTieWaypoint],
    channels: list[_DuctChannel],
    draw_scale: float,
    *,
    step_index: int = 0,
) -> tuple[list[tuple[float, float]], list[str]]:
    """Route `start -> ties -> end` strictly through the nearest wire trays.

    Wires enter the nearest horizontal tray vertically from their terminal, travel inside
    the connected wire trays with compact smooth corner radii (`R <= 4.1 mm`), cinch smoothly
    through any `ZipTieWaypoint` along the tray without bulging outside the tray walls,
    and exit vertically into the destination terminal.
    """

    skel_pts, skel_ducts = _build_in_tray_skeleton(
        start,
        end,
        channels,
        step_index=step_index,
        ties=ties,
    )
    min_w = min((d.width_mm for d in skel_ducts), default=16.0)
    r_in_tray = min(4.1, max(2.8, min_w * 0.25))

    # Map each zip-tie to the closest tray segment in `skel_pts` (segments 1 .. len(skel_pts)-2)
    seg_count = len(skel_ducts)
    in_tray_ties: dict[int, list[ZipTieWaypoint]] = {i: [] for i in range(seg_count)}
    external_ties: dict[int, list[ZipTieWaypoint]] = {i: [] for i in range(seg_count)}

    for tie in ties:
        best_seg = 0
        best_dist = float("inf")
        for s_idx in range(seg_count):
            ax, ay = skel_pts[s_idx + 1]
            bx, by = skel_pts[s_idx + 2]
            d = _point_to_segment_dist(float(tie.x), float(tie.y), ax, ay, bx, by)
            if d < best_dist:
                best_dist = d
                best_seg = s_idx
        duct = skel_ducts[best_seg]
        perp = abs(float(tie.y) - duct.coord) if duct.orientation == "h" else abs(float(tie.x) - duct.coord)
        if perp <= duct.width_mm * 0.55:
            in_tray_ties[best_seg].append(tie)
        else:
            external_ties[best_seg].append(tie)

    # If any zip-tie was explicitly dragged outside the tray (e.g. move_zip_tie_waypoint test),
    # insert a smooth filleted detour on that segment while preserving the exact tie coordinate.
    preserve_pts: set[tuple[float, float]] = set()
    expanded_pts: list[tuple[float, float]] = [skel_pts[0], skel_pts[1]]
    for s_idx in range(seg_count):
        p_a = expanded_pts[-1]
        p_b = skel_pts[s_idx + 2]
        duct = skel_ducts[s_idx]
        ext = external_ties[s_idx]
        if ext:
            for t in ext:
                tx, ty = round(float(t.x), 3), round(float(t.y), 3)
                preserve_pts.add((tx, ty))
                if duct.orientation == "v":
                    dy_dir = 1.0 if p_b[1] >= p_a[1] else -1.0
                    lead = 6.0 * dy_dir
                    expanded_pts.append((p_a[0], round(ty - lead, 3)))
                    expanded_pts.append((tx, round(ty - lead * 0.35, 3)))
                    expanded_pts.append((tx, ty))
                    expanded_pts.append((tx, round(ty + lead * 0.35, 3)))
                    expanded_pts.append((p_a[0], round(ty + lead, 3)))
                else:
                    dx_dir = 1.0 if p_b[0] >= p_a[0] else -1.0
                    lead = 6.0 * dx_dir
                    expanded_pts.append((round(tx - lead, 3), p_a[1]))
                    expanded_pts.append((round(tx - lead * 0.35, 3), ty))
                    expanded_pts.append((tx, ty))
                    expanded_pts.append((round(tx + lead * 0.35, 3), ty))
                    expanded_pts.append((round(tx + lead, 3), p_a[1]))
        expanded_pts.append(p_b)
    expanded_pts.append(skel_pts[-1])

    # If an in-tray zip-tie is close (< 13 mm) to either corner of its tray segment,
    # align that segment's lane directly with the zip-tie coordinate so the corner fillet
    # lands smoothly on the zip-tie line without a tight double S-bend next to the corner.
    skel_pts = list(skel_pts)
    for s_idx in range(seg_count):
        ties_here = in_tray_ties[s_idx]
        if not ties_here:
            continue
        duct = skel_ducts[s_idx]
        ax, ay = skel_pts[s_idx + 1]
        bx, by = skel_pts[s_idx + 2]
        for t in ties_here:
            if duct.orientation == "h":
                if min(abs(t.x - ax), abs(bx - t.x)) < 13.0:
                    ty = round(float(t.y), 3)
                    skel_pts[s_idx + 1] = (skel_pts[s_idx + 1][0], ty)
                    skel_pts[s_idx + 2] = (skel_pts[s_idx + 2][0], ty)
                    break
            else:
                if min(abs(t.y - ay), abs(by - t.y)) < 13.0:
                    tx = round(float(t.x), 3)
                    skel_pts[s_idx + 1] = (tx, skel_pts[s_idx + 1][1])
                    skel_pts[s_idx + 2] = (tx, skel_pts[s_idx + 2][1])
                    break

    # If we only have in-tray ties (normal case), fillet the tray corners first and then pinch
    # along the straight interior portion of each tray segment so 100% of points stay in the tray.
    if not any(external_ties.values()):
        n_pts = len(skel_pts)
        seg_lengths = [
            math.hypot(skel_pts[i + 1][0] - skel_pts[i][0], skel_pts[i + 1][1] - skel_pts[i][1])
            for i in range(n_pts - 1)
        ]
        corner_r: dict[int, float] = {}
        corner_curves: dict[int, tuple[tuple[float, float], tuple[float, float], list[tuple[float, float]]]] = {}
        kappa = 0.55228475
        for i in range(1, n_pts - 1):
            prev_pt = skel_pts[i - 1]
            curr_pt = skel_pts[i]
            next_pt = skel_pts[i + 1]
            len_in = seg_lengths[i - 1]
            len_out = seg_lengths[i]
            if len_in < 0.25 or len_out < 0.25:
                corner_r[i] = 0.0
                continue
            ux_in = (curr_pt[0] - prev_pt[0]) / len_in
            uy_in = (curr_pt[1] - prev_pt[1]) / len_in
            ux_out = (next_pt[0] - curr_pt[0]) / len_out
            uy_out = (next_pt[1] - curr_pt[1]) / len_out
            dot = max(-1.0, min(1.0, ux_in * ux_out + uy_in * uy_out))
            if dot > 0.999:
                corner_r[i] = 0.0
                continue
            r = min(r_in_tray, 0.42 * len_in, 0.42 * len_out)
            corner_r[i] = r
            entry = (curr_pt[0] - ux_in * r, curr_pt[1] - uy_in * r)
            exit_ = (curr_pt[0] + ux_out * r, curr_pt[1] + uy_out * r)
            c1 = (entry[0] + ux_in * r * kappa, entry[1] + uy_in * r * kappa)
            c2 = (exit_[0] - ux_out * r * kappa, exit_[1] - uy_out * r * kappa)
            curve = _sample_cubic_bezier(entry, c1, c2, exit_, steps=CURVE_SAMPLES_PER_BEND)
            corner_curves[i] = (entry, exit_, curve)

        result: list[tuple[float, float]] = [skel_pts[0]]
        if 1 in corner_curves:
            result.extend(corner_curves[1][2])
        else:
            result.append(skel_pts[1])

        for s_idx in range(seg_count):
            c_left = s_idx + 1
            c_right = s_idx + 2
            seg_start = corner_curves[c_left][1] if c_left in corner_curves else skel_pts[c_left]
            seg_end = corner_curves[c_right][0] if c_right in corner_curves else skel_pts[c_right]
            duct = skel_ducts[s_idx]
            ties_here = in_tray_ties[s_idx]
            if ties_here:
                pinched = _sample_in_tray_pinch_segment(seg_start, seg_end, ties_here, duct.orientation)
                result.extend(pinched[1:])
            if c_right in corner_curves:
                result.extend(corner_curves[c_right][2])
            else:
                result.append(skel_pts[c_right])

        result.append(skel_pts[-1])
        pts_final = _dedup_points(result, min_dist=0.04)
    else:
        pts_final = _smooth_corner_fillet_polyline(expanded_pts, bend_radius=r_in_tray, preserve=preserve_pts)

    via: list[str] = []
    for s_idx, duct in enumerate(skel_ducts):
        if not via or via[-1] != duct.id:
            via.append(duct.id)
        for t in in_tray_ties[s_idx] + external_ties[s_idx]:
            if t.id not in via:
                via.append(t.id)
    for t in ties:
        if t.id not in via:
            via.append(t.id)

    return pts_final, via


def list_zip_tie_waypoints(
    session_or_project: Any,
    sheet_id: str | None = None,
) -> list[ZipTieWaypoint]:
    project, _ = _unwrap_project_and_catalog(session_or_project)
    sheet = find_layout_sheet(project, sheet_id)
    raw_list = (sheet.properties or {}).get("zip_ties") or []
    return [ZipTieWaypoint.from_dict(item) for item in raw_list if isinstance(item, dict)]


def _save_zip_tie_waypoints(sheet: Sheet, ties: list[ZipTieWaypoint]) -> None:
    if sheet.properties is None:
        sheet.properties = {}
    if ties:
        sheet.properties["zip_ties"] = [t.to_dict() for t in ties]
        if sheet.properties.get("show_panel_wiring", "off") == "off":
            sheet.properties["show_panel_wiring"] = "all"
    else:
        sheet.properties.pop("zip_ties", None)


def _installed_wire_set(sheet: Sheet) -> set[str]:
    raw = (sheet.properties or {}).get("installed_wires") or []
    return {str(item) for item in raw}


def _collect_raw_conductors(
    project: Project,
    catalog: Catalog | None,
    layout_sheet: Sheet,
) -> list[dict[str, str]]:
    """Collect From-To conductors from schematic sheets and/or direct layout sheet wires."""

    from panelcad.core.from_to import from_to_rows

    rows: list[dict[str, str]] = []
    try:
        ft = from_to_rows(project, catalog, sort="number")
    except Exception:
        ft = []

    for idx, r in enumerate(ft):
        frm = str(r.get("from") or "").strip()
        to = str(r.get("to") or "").strip()
        if not frm or not to:
            continue
        f_tag, f_pin = (frm.split(":", 1) + [""])[:2] if ":" in frm else (frm.split(".", 1) + [""])[:2]
        t_tag, t_pin = (to.split(":", 1) + [""])[:2] if ":" in to else (to.split(".", 1) + [""])[:2]
        wnum = str(r.get("wire_number") or "").strip() or f"W{idx + 1}"
        gauge = str(r.get("size") or "").strip()
        if not gauge or gauge.upper() == "TBD":
            gauge = "14 AWG" if wnum in {"L1", "L2", "L3", "T1", "T2", "T3", "U1", "V1", "W1"} else "18 AWG"
        color = str(r.get("color") or "BU").strip() or "BU"
        wtype = str(r.get("type") or "MTW").strip() or "MTW"
        rows.append(
            {
                "wire_id": f"w-{idx+1}",
                "wire_number": wnum,
                "from_ref": f_tag.strip(),
                "from_pin": f_pin.strip(),
                "to_ref": t_tag.strip(),
                "to_pin": t_pin.strip(),
                "gauge": gauge,
                "color": color,
                "wire_type": wtype,
            }
        )

    # Also support direct wires on the layout sheet itself (e.g. unit tests)
    for idx, wire in enumerate(layout_sheet.wires or []):
        frm = str(wire.frm or "").strip()
        to = str(wire.to or "").strip()
        f_tag, f_pin = (frm.split(":", 1) + [""])[:2] if ":" in frm else (frm.split(".", 1) + [""])[:2]
        t_tag, t_pin = (to.split(":", 1) + [""])[:2] if ":" in to else (to.split(".", 1) + [""])[:2]
        wnum = str(wire.number or wire.label or wire.net or wire.id or f"W{len(rows)+idx+1}").strip()
        rows.append(
            {
                "wire_id": wire.id or f"lw-{idx+1}",
                "wire_number": wnum,
                "from_ref": f_tag.strip(),
                "from_pin": f_pin.strip(),
                "to_ref": t_tag.strip(),
                "to_pin": t_pin.strip(),
                "gauge": str(wire.gauge or "18 AWG").strip(),
                "color": str(wire.color or "BU").strip(),
                "wire_type": str(wire.wire_type or "MTW").strip(),
            }
        )
    return rows


def _compute_cut_length(
    pts: list[tuple[float, float]],
    draw_scale: float,
    z_from_mm: float,
    z_to_mm: float,
    duct_allowance_mm: float = DEFAULT_DUCT_DEPTH_ALLOWANCE_MM,
    strip_slack_mm: float = DEFAULT_STRIP_SLACK_MM,
) -> tuple[float, float, float, float]:
    """Return `(path_2d_mm, raw_length_mm, cut_length_mm, cut_length_in)`."""

    scale = draw_scale if draw_scale > 1e-6 else 1.0
    drawn_2d = _polyline_length(pts)
    path_2d_mm = drawn_2d / scale
    raw_mm = path_2d_mm + z_from_mm + z_to_mm + 2.0 * duct_allowance_mm + 2.0 * strip_slack_mm
    cut_mm = math.ceil(raw_mm / CUT_INCREMENT_MM) * CUT_INCREMENT_MM
    cut_in = math.ceil((raw_mm / INCH_MM) / CUT_INCREMENT_IN) * CUT_INCREMENT_IN
    return (round(path_2d_mm, 1), round(raw_mm, 1), float(cut_mm), round(float(cut_in), 2))


def route_panel_wires(
    session_or_project: Any,
    sheet_id: str | None = None,
    catalog: Catalog | None = None,
) -> list[PanelWireRoute]:
    """Route all panel wires on the layout sheet and calculate per-wire cut lengths."""

    project, cat = _unwrap_project_and_catalog(session_or_project, catalog)
    sheet = find_layout_sheet(project, sheet_id)
    draw_scale = _sheet_draw_scale(sheet)
    boxes = _component_boxes_on_sheet(sheet, cat)
    channels = _build_duct_channels(sheet, boxes)
    ties = list_zip_tie_waypoints(project, sheet.id)
    installed_set = _installed_wire_set(sheet)

    raw_conductors = _collect_raw_conductors(project, cat, sheet)
    routes: list[PanelWireRoute] = []

    for cond in raw_conductors:
        start = resolve_pin_landing(sheet, cat, cond["from_ref"], cond["from_pin"], boxes=boxes)
        end = resolve_pin_landing(sheet, cat, cond["to_ref"], cond["to_pin"], boxes=boxes)
        if start is None or end is None:
            continue

        step_idx = len(routes)
        wnum = cond["wire_number"]
        wid = cond["wire_id"]
        matching_ties = [
            t
            for t in ties
            if wnum in t.wire_numbers
            or wid in t.wire_numbers
            or f"{cond['from_ref']}:{cond['from_pin']}->{cond['to_ref']}:{cond['to_pin']}" in t.wire_numbers
            or "*" in t.wire_numbers
        ]
        if matching_ties:
            pts, via = _route_via_zip_ties(start, end, matching_ties, channels, draw_scale, step_index=step_idx)
        else:
            pts, via = _route_via_ducts(start, end, channels, bend_radius=4.0, step_index=step_idx)

        path_2d_mm, raw_mm, cut_mm, cut_in = _compute_cut_length(
            pts,
            draw_scale,
            start.z_mm,
            end.z_mm,
        )
        is_inst = (
            wnum in installed_set
            or wid in installed_set
            or str(step_idx) in installed_set
            or f"{cond['from_ref']}:{cond['from_pin']}->{cond['to_ref']}:{cond['to_pin']}" in installed_set
        )
        routes.append(
            PanelWireRoute(
                step_index=step_idx,
                wire_id=wid,
                wire_number=wnum,
                from_ref=start.ref,
                from_pin=start.pin,
                to_ref=end.ref,
                to_pin=end.pin,
                gauge=cond["gauge"],
                color=cond["color"],
                wire_type=cond["wire_type"],
                points=pts,
                via=via,
                cut_length_mm=cut_mm,
                cut_length_in=cut_in,
                raw_length_mm=raw_mm,
                path_2d_mm=path_2d_mm,
                z_drop_from_mm=start.z_mm,
                z_drop_to_mm=end.z_mm,
                installed=is_inst,
                from_landing=start,
                to_landing=end,
            )
        )
    return routes


def build_wire_by_wire_schedule(
    session_or_project: Any,
    sheet_id: str | None = None,
    catalog: Catalog | None = None,
) -> WireByWireSchedule:
    """Build the full `WireByWireSchedule` with routed paths, zip-ties, and cut lengths."""

    project, cat = _unwrap_project_and_catalog(session_or_project, catalog)
    sheet = find_layout_sheet(project, sheet_id)
    routes = route_panel_wires(project, sheet.id, cat)
    ties = list_zip_tie_waypoints(project, sheet.id)
    return WireByWireSchedule(
        sheet_id=sheet.id,
        rows=routes,
        zip_ties=ties,
        draw_scale=_sheet_draw_scale(sheet),
    )


def _ccw(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _segments_intersect(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    x3: float,
    y3: float,
    x4: float,
    y4: float,
) -> bool:
    d1 = _ccw(x1, y1, x2, y2, x3, y3)
    d2 = _ccw(x1, y1, x2, y2, x4, y4)
    d3 = _ccw(x3, y3, x4, y4, x1, y1)
    d4 = _ccw(x3, y3, x4, y4, x2, y2)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True
    eps = 1e-4
    if abs(d1) <= eps and min(x1, x2) - eps <= x3 <= max(x1, x2) + eps and min(y1, y2) - eps <= y3 <= max(y1, y2) + eps:
        return True
    if abs(d2) <= eps and min(x1, x2) - eps <= x4 <= max(x1, x2) + eps and min(y1, y2) - eps <= y4 <= max(y1, y2) + eps:
        return True
    if abs(d3) <= eps and min(x3, x4) - eps <= x1 <= max(x3, x4) + eps and min(y3, y4) - eps <= y1 <= max(y3, y4) + eps:
        return True
    if abs(d4) <= eps and min(x3, x4) - eps <= x2 <= max(x3, x4) + eps and min(y3, y4) - eps <= y2 <= max(y3, y4) + eps:
        return True
    return False


def _point_to_segment_dist(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    dx = x2 - x1
    dy = y2 - y1
    len_sq = dx * dx + dy * dy
    if len_sq <= 1e-9:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / len_sq))
    proj_x = x1 + t * dx
    proj_y = y1 + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def _polyline_intersects_slice(
    pts: list[tuple[float, float]],
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    capture_radius: float = 6.0,
) -> bool:
    for i in range(len(pts) - 1):
        ax, ay = pts[i]
        bx, by = pts[i + 1]
        if _segments_intersect(x1, y1, x2, y2, ax, ay, bx, by):
            return True
    mx = (x1 + x2) / 2.0
    my = (y1 + y2) / 2.0
    for i in range(len(pts) - 1):
        ax, ay = pts[i]
        bx, by = pts[i + 1]
        if _point_to_segment_dist(mx, my, ax, ay, bx, by) <= capture_radius:
            return True
    return False


def _next_zip_tie_id(ties: list[ZipTieWaypoint]) -> str:
    used = {t.id for t in ties}
    idx = 1
    while f"zt-{idx}" in used:
        idx += 1
    return f"zt-{idx}"


def add_zip_tie_slice(
    session_or_project: Any,
    sheet_id: str | None,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    catalog: Catalog | None = None,
) -> ZipTieWaypoint:
    """Slice across routed panel wires `(x1, y1) -> (x2, y2)` to bundle them into a Zip-Tie Waypoint."""

    project, cat = _unwrap_project_and_catalog(session_or_project, catalog)
    sheet = find_layout_sheet(project, sheet_id)
    routes = route_panel_wires(project, sheet.id, cat)
    captured: list[str] = []
    wnum_counts: dict[str, int] = {}
    for r in routes:
        wnum_counts[r.wire_number] = wnum_counts.get(r.wire_number, 0) + 1
    for r in routes:
        if _polyline_intersects_slice(r.points, x1, y1, x2, y2):
            token = r.wire_number if wnum_counts.get(r.wire_number, 1) == 1 else f"{r.from_endpoint}->{r.to_endpoint}"
            if token not in captured:
                captured.append(token)
            if r.wire_number not in captured and wnum_counts.get(r.wire_number, 1) == 1:
                captured.append(r.wire_number)
    if not captured and routes:
        # Fallback: capture the 3 nearest wires to the slice midpoint so a slice always bundles nearest wires
        mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        ranked = sorted(
            routes,
            key=lambda r: min(
                (_point_to_segment_dist(mx, my, r.points[i][0], r.points[i][1], r.points[i + 1][0], r.points[i + 1][1]) for i in range(len(r.points) - 1)),
                default=float("inf"),
            ),
        )
        for r in ranked[: max(2, min(4, len(ranked)))]:
            if r.wire_number not in captured:
                captured.append(r.wire_number)

    ties = list_zip_tie_waypoints(project, sheet.id)
    zt_id = _next_zip_tie_id(ties)
    cx = round((float(x1) + float(x2)) / 2.0, 2)
    cy = round((float(y1) + float(y2)) / 2.0, 2)
    orient: Literal["h", "v"] = "h" if abs(x2 - x1) >= abs(y2 - y1) else "v"
    boxes = _component_boxes_on_sheet(sheet, cat)
    channels = _build_duct_channels(sheet, boxes)
    for ch in channels:
        half_inner = ch.width_mm * 0.30
        if ch.orientation == "v" and orient == "h":
            if min(x1, x2) - 4.0 <= ch.coord <= max(x1, x2) + 4.0 and ch.span_min - 4.0 <= cy <= ch.span_max + 4.0:
                if abs(cx - ch.coord) > half_inner:
                    cx = round(ch.coord, 2)
                break
        elif ch.orientation == "h" and orient == "v":
            if min(y1, y2) - 4.0 <= ch.coord <= max(y1, y2) + 4.0 and ch.span_min - 4.0 <= cx <= ch.span_max + 4.0:
                if abs(cy - ch.coord) > half_inner:
                    cy = round(ch.coord, 2)
                break
    tie = ZipTieWaypoint(
        id=zt_id,
        x=cx,
        y=cy,
        orientation=orient,
        wire_numbers=captured,
        slice_line=(float(x1), float(y1), float(x2), float(y2)),
    )
    ties.append(tie)
    _save_zip_tie_waypoints(sheet, ties)
    return tie


def add_zip_tie_waypoint(
    session_or_project: Any,
    sheet_id: str | None,
    x: float,
    y: float,
    wire_numbers: list[str] | None = None,
    *,
    orientation: Literal["h", "v"] = "h",
    catalog: Catalog | None = None,
) -> ZipTieWaypoint:
    """Place a Zip-Tie Waypoint at `(x, y)` capturing `wire_numbers` (or nearby wires when omitted)."""

    if wire_numbers is None:
        return add_zip_tie_slice(
            session_or_project,
            sheet_id,
            x - 10.0,
            y,
            x + 10.0,
            y,
            catalog=catalog,
        )
    project, _ = _unwrap_project_and_catalog(session_or_project, catalog)
    sheet = find_layout_sheet(project, sheet_id)
    ties = list_zip_tie_waypoints(project, sheet.id)
    zt_id = _next_zip_tie_id(ties)
    tie = ZipTieWaypoint(
        id=zt_id,
        x=round(float(x), 2),
        y=round(float(y), 2),
        orientation=orientation,
        wire_numbers=[str(w) for w in wire_numbers],
    )
    ties.append(tie)
    _save_zip_tie_waypoints(sheet, ties)
    return tie


def move_zip_tie_waypoint(
    session_or_project: Any,
    sheet_id: str | None,
    zip_tie_id: str,
    new_x: float,
    new_y: float,
) -> ZipTieWaypoint:
    """Move an existing Zip-Tie Waypoint to `(new_x, new_y)` and update captured wire routes."""

    project, _ = _unwrap_project_and_catalog(session_or_project)
    sheet = find_layout_sheet(project, sheet_id)
    ties = list_zip_tie_waypoints(project, sheet.id)
    for tie in ties:
        if tie.id == zip_tie_id:
            dx = float(new_x) - tie.x
            dy = float(new_y) - tie.y
            tie.x = round(float(new_x), 2)
            tie.y = round(float(new_y), 2)
            if tie.slice_line is not None:
                x1, y1, x2, y2 = tie.slice_line
                tie.slice_line = (round(x1 + dx, 2), round(y1 + dy, 2), round(x2 + dx, 2), round(y2 + dy, 2))
            _save_zip_tie_waypoints(sheet, ties)
            return tie
    raise KeyError(f"No zip-tie waypoint '{zip_tie_id}' on sheet '{sheet.id}'")


def remove_zip_tie_waypoint(
    session_or_project: Any,
    sheet_id: str | None,
    zip_tie_id: str,
) -> bool:
    """Delete a Zip-Tie Waypoint so its captured wires unbundle back to default duct routes."""

    project, _ = _unwrap_project_and_catalog(session_or_project)
    sheet = find_layout_sheet(project, sheet_id)
    ties = list_zip_tie_waypoints(project, sheet.id)
    kept = [t for t in ties if t.id != zip_tie_id]
    removed = len(kept) != len(ties)
    _save_zip_tie_waypoints(sheet, kept)
    return removed


def set_panel_wiring_mode(
    session_or_project: Any,
    mode: Literal["off", "all", "walkthrough"],
    sheet_id: str | None = None,
) -> str:
    """Set `show_panel_wiring` mode (`"off"`, `"all"`, or `"walkthrough"`) on the layout sheet."""

    norm = str(mode or "off").strip().lower()
    if norm not in ("off", "all", "walkthrough"):
        raise ValueError(f"Unknown panel wiring mode '{mode}' (expected 'off', 'all', or 'walkthrough')")
    project, _ = _unwrap_project_and_catalog(session_or_project)
    sheet = find_layout_sheet(project, sheet_id)
    if sheet.properties is None:
        sheet.properties = {}
    if norm == "off":
        sheet.properties.pop("show_panel_wiring", None)
    else:
        sheet.properties["show_panel_wiring"] = norm
        if norm == "walkthrough" and "walkthrough_step" not in sheet.properties:
            sheet.properties["walkthrough_step"] = 0
    return norm


def get_panel_wiring_mode(
    sheet_or_session: Any,
    project_or_sheet_id: Any = None,
) -> Literal["off", "all", "walkthrough"]:
    if isinstance(sheet_or_session, Sheet):
        sheet = sheet_or_session
        project = project_or_sheet_id if isinstance(project_or_sheet_id, Project) else None
    else:
        project, _ = _unwrap_project_and_catalog(sheet_or_session)
        sheet_id = project_or_sheet_id if isinstance(project_or_sheet_id, str) else None
        sheet = find_layout_sheet(project, sheet_id)
    raw = str((sheet.properties or {}).get("show_panel_wiring") or "").strip().lower()
    if not raw and project is not None:
        raw = str((project.properties or {}).get("show_panel_wiring") or "").strip().lower()
    if raw in ("all", "walkthrough"):
        return raw  # type: ignore[return-value]
    return "off"


def get_walkthrough_step(
    sheet_or_session: Any,
    total_steps_or_sheet_id: int | str | None = None,
) -> int:
    total_steps: int | None = None
    if isinstance(sheet_or_session, Sheet):
        sheet = sheet_or_session
        if isinstance(total_steps_or_sheet_id, int):
            total_steps = total_steps_or_sheet_id
    else:
        project, _ = _unwrap_project_and_catalog(sheet_or_session)
        sheet_id = total_steps_or_sheet_id if isinstance(total_steps_or_sheet_id, str) else None
        sheet = find_layout_sheet(project, sheet_id)
        if isinstance(total_steps_or_sheet_id, int):
            total_steps = total_steps_or_sheet_id
    try:
        idx = int((sheet.properties or {}).get("walkthrough_step") or 0)
    except (TypeError, ValueError):
        idx = 0
    if total_steps is not None and total_steps > 0:
        idx = max(0, min(total_steps - 1, idx))
    return max(0, idx)


def set_walkthrough_step(
    session_or_project: Any,
    step_index: int,
    sheet_id: str | None = None,
    *,
    catalog: Catalog | None = None,
) -> PanelWireRoute | None:
    """Switch the layout sheet to walkthrough mode at `step_index` (`0 .. N-1`) and return that wire route."""

    project, cat = _unwrap_project_and_catalog(session_or_project, catalog)
    sheet = find_layout_sheet(project, sheet_id)
    routes = route_panel_wires(project, sheet.id, cat)
    if sheet.properties is None:
        sheet.properties = {}
    sheet.properties["show_panel_wiring"] = "walkthrough"
    if not routes:
        sheet.properties["walkthrough_step"] = 0
        return None
    clamped = max(0, min(len(routes) - 1, int(step_index)))
    sheet.properties["walkthrough_step"] = clamped
    return routes[clamped]


def step_walkthrough(
    session_or_project: Any,
    delta: int = 1,
    sheet_id: str | None = None,
    *,
    catalog: Catalog | None = None,
) -> PanelWireRoute | None:
    """Advance or rewind the active Technician Wiring Walkthrough step by `delta`."""

    project, cat = _unwrap_project_and_catalog(session_or_project, catalog)
    sheet = find_layout_sheet(project, sheet_id)
    routes = route_panel_wires(project, sheet.id, cat)
    if not routes:
        return None
    cur = get_walkthrough_step(sheet, len(routes))
    nxt = (cur + int(delta)) % len(routes)
    return set_walkthrough_step(project, nxt, sheet.id, catalog=cat)


def mark_wire_installed(
    session_or_project: Any,
    wire_number_or_index: str | int,
    installed: bool = True,
    sheet_id: str | None = None,
    *,
    catalog: Catalog | None = None,
) -> bool:
    """Mark a wire (by `step_index` int or `wire_number` str) as installed in the walkthrough."""

    project, cat = _unwrap_project_and_catalog(session_or_project, catalog)
    sheet = find_layout_sheet(project, sheet_id)
    routes = route_panel_wires(project, sheet.id, cat)
    target_key: str | None = None
    if isinstance(wire_number_or_index, int):
        if 0 <= wire_number_or_index < len(routes):
            target_key = routes[wire_number_or_index].wire_number
    else:
        token = str(wire_number_or_index).strip()
        for r in routes:
            if token in (r.wire_number, r.wire_id, f"{r.from_endpoint}->{r.to_endpoint}"):
                target_key = r.wire_number
                break
        if target_key is None and token.isdigit():
            idx = int(token)
            if 0 <= idx < len(routes):
                target_key = routes[idx].wire_number
    if target_key is None:
        return False
    installed_set = _installed_wire_set(sheet)
    if installed:
        installed_set.add(target_key)
    else:
        installed_set.discard(target_key)
    if sheet.properties is None:
        sheet.properties = {}
    if installed_set:
        sheet.properties["installed_wires"] = sorted(installed_set)
    else:
        sheet.properties.pop("installed_wires", None)
    return True


def add_wire_by_wire_sheet(
    session_or_project: Any,
    sheet_id: str = "wire_by_wire",
    *,
    title: str = "Wire-by-Wire Cut Sheet",
    layout_sheet_id: str | None = None,
) -> Sheet:
    """Add or update a dedicated `wire_by_wire` cut-length schedule sheet in the project."""

    project, _ = _unwrap_project_and_catalog(session_or_project)
    layout_sheet = find_layout_sheet(project, layout_sheet_id)
    existing = next((s for s in project.sheets if s.id == sheet_id), None)
    if existing is not None:
        existing.kind = "wire_by_wire"
        existing.title = title
        existing.description = f"Backpanel wire routing & cut-length schedule ({layout_sheet.title or layout_sheet.id})"
        existing.properties["wire_by_wire"] = True
        existing.properties["layout_sheet_id"] = layout_sheet.id
        return existing

    sheet = Sheet(
        id=sheet_id,
        title=title,
        description=f"Backpanel wire routing & cut-length schedule ({layout_sheet.title or layout_sheet.id})",
        kind="wire_by_wire",
        page=layout_sheet.page.model_copy(deep=True),
        properties={
            "wire_by_wire": True,
            "layout_sheet_id": layout_sheet.id,
        },
    )
    sheet.page.orientation = "landscape"
    project.sheets.append(sheet)
    return sheet


def export_wire_by_wire_csv(
    session_or_project: Any,
    path: Path | str,
    sheet_id: str | None = None,
    *,
    catalog: Catalog | None = None,
) -> Path:
    """Export the Wire-by-Wire Cut Sheet schedule to a CSV file."""

    sched = build_wire_by_wire_schedule(session_or_project, sheet_id=sheet_id, catalog=catalog)
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(WIRE_BY_WIRE_COLUMNS),
            lineterminator="\r\n",
            extrasaction="ignore",
        )
        writer.writeheader()
        for row_dict in sched.to_dicts():
            writer.writerow({k: str(row_dict.get(k, "")) for k in WIRE_BY_WIRE_COLUMNS})
    return dest


def export_sheet_png(
    sheet: Sheet,
    output_path: Path | str,
    *,
    catalog: Catalog | None = None,
    dpi: float = 200.0,
    project: Project | None = None,
) -> Path:
    """Render any PanelCAD sheet (layout, schematic, or wire_by_wire) to a PNG image."""

    from PySide6.QtGui import QImage, QPainter
    from panelcad.api.session import _ensure_qt_app
    from panelcad.render.page import paint_sheet

    _ensure_qt_app()
    cat = catalog or Catalog.builtin()
    proj = project or Project(name=sheet.title or sheet.id, sheets=[sheet])
    width_mm, height_mm = sheet.page.dimensions()
    scale = float(dpi) / 25.4
    px_w = max(1, int(round(width_mm * scale)))
    px_h = max(1, int(round(height_mm * scale)))
    img = QImage(px_w, px_h, QImage.Format.Format_ARGB32)
    painter = QPainter(img)
    try:
        painter.scale(scale, scale)
        idx = proj.sheets.index(sheet) if sheet in proj.sheets else 0
        paint_sheet(
            painter,
            proj,
            sheet,
            cat,
            sheet_index=idx,
            sheet_count=max(1, len(proj.sheets)),
        )
    finally:
        if painter.isActive():
            painter.end()
    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(dest))
    return dest


def render_walkthrough_step_png(
    session_or_project: Any,
    step_index: int,
    output_path: Path | str,
    *,
    sheet_id: str | None = None,
    catalog: Catalog | None = None,
    dpi: float = 200.0,
) -> Path:
    """Set walkthrough mode to `step_index`, render the layout sheet to PNG, and return `output_path`."""

    project, cat = _unwrap_project_and_catalog(session_or_project, catalog)
    sheet = find_layout_sheet(project, sheet_id)
    set_walkthrough_step(project, step_index, sheet.id, catalog=cat)
    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    export_sheet_png(sheet, dest, catalog=cat, dpi=dpi, project=project)
    return dest


def render_panel_wiring_png(
    session_or_project: Any,
    output_path: Path | str,
    *,
    mode: Literal["off", "all", "walkthrough"] = "all",
    step_index: int = 0,
    sheet_id: str | None = None,
    catalog: Catalog | None = None,
    dpi: float = 200.0,
) -> Path:
    """Render the panel layout sheet in the requested wiring mode (`off`, `all`, or `walkthrough`) to PNG."""

    project, cat = _unwrap_project_and_catalog(session_or_project, catalog)
    sheet = find_layout_sheet(project, sheet_id)
    set_panel_wiring_mode(project, mode, sheet.id)
    if mode == "walkthrough":
        set_walkthrough_step(project, step_index, sheet.id, catalog=cat)
    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    export_sheet_png(sheet, dest, catalog=cat, dpi=dpi, project=project)
    return dest


def verify_routes_inside_trays(
    session_or_project: Any,
    sheet_id: str | None = None,
    *,
    catalog: Catalog | None = None,
    tol_mm: float = 0.25,
) -> dict[str, Any]:
    """Verify mathematically that every routed panel wire:
    1. Travels vertically from its source terminal into the nearest horizontal wire tray
       without crossing any other component body.
    2. Stays 100% inside the union of the sheet's wire tray rectangles between tray entry and tray exit.
    3. Exits vertically from the destination wire tray into the destination terminal
       without crossing any other component body.
    4. Uses smooth bends (`max_turn_angle_deg < 25.0°`, zero sharp 90° corners).
    """

    project, cat = _unwrap_project_and_catalog(session_or_project, catalog)
    sheet = find_layout_sheet(project, sheet_id)
    trays = _extract_sheet_trays(sheet)
    boxes = _component_boxes_on_sheet(sheet, cat)
    routes = route_panel_wires(project, sheet.id, cat)

    # Expand tray rectangles slightly at intersections so junction corners are continuous
    tray_rects: list[tuple[str, float, float, float, float]] = []
    for t in trays:
        if t.orientation == "h":
            tray_rects.append((t.id, float(t.x) - tol_mm, float(t.y) - tol_mm, float(t.x) + float(t.length_mm) + tol_mm, float(t.y) + float(t.width_mm) + tol_mm))
        else:
            tray_rects.append((t.id, float(t.x) - tol_mm, float(t.y) - tol_mm, float(t.x) + float(t.width_mm) + tol_mm, float(t.y) + float(t.length_mm) + tol_mm))

    def _in_any_tray(px: float, py: float) -> bool:
        return any(x0 <= px <= x1 and y0 <= py <= y1 for _, x0, y0, x1, y1 in tray_rects)

    def _in_other_component(px: float, py: float, ignore_refs: set[str]) -> str | None:
        for ref, (bx, by, bw, bh, _z, _dev) in boxes.items():
            if ref in ignore_refs:
                continue
            if bx + 0.3 < px < bx + bw - 0.3 and by + 0.3 < py < by + bh - 0.3:
                return ref
        return None

    violations: list[str] = []
    per_wire: list[dict[str, Any]] = []

    for r in routes:
        pts = r.points
        in_flags = [_in_any_tray(px, py) for px, py in pts]
        in_indices = [idx for idx, flag in enumerate(in_flags) if flag]
        if not in_indices:
            violations.append(f"{r.wire_number}: never enters any wire tray")
            continue

        idx_enter = in_indices[0]
        idx_exit = in_indices[-1]

        # 1. Straight vertical entry into nearest tray
        sx, sy = pts[0]
        for k in range(0, idx_enter + 1):
            px, py = pts[k]
            if abs(px - sx) > tol_mm:
                violations.append(f"{r.wire_number}: non-vertical entry point ({px}, {py}) vs start.x={sx}")
                break
            hit = _in_other_component(px, py, {r.from_ref, r.to_ref})
            if hit:
                violations.append(f"{r.wire_number}: entry lead crosses component {hit} at ({px}, {py})")
                break

        # 2. 100% of points and segment midpoints between idx_enter and idx_exit inside trays
        outside_pts: list[tuple[float, float]] = []
        for k in range(idx_enter, idx_exit + 1):
            px, py = pts[k]
            if not _in_any_tray(px, py):
                outside_pts.append((px, py))
            if k < idx_exit:
                mx = 0.5 * (px + pts[k + 1][0])
                my = 0.5 * (py + pts[k + 1][1])
                if not _in_any_tray(mx, my):
                    outside_pts.append((round(mx, 2), round(my, 2)))
        if outside_pts:
            violations.append(f"{r.wire_number}: {len(outside_pts)} interior points outside wire trays (first: {outside_pts[0]})")

        # 3. Straight vertical exit from nearest tray to destination terminal
        ex, ey = pts[-1]
        for k in range(idx_exit, len(pts)):
            px, py = pts[k]
            if abs(px - ex) > tol_mm:
                violations.append(f"{r.wire_number}: non-vertical exit point ({px}, {py}) vs end.x={ex}")
                break
            hit = _in_other_component(px, py, {r.from_ref, r.to_ref})
            if hit:
                violations.append(f"{r.wire_number}: exit lead crosses component {hit} at ({px}, {py})")
                break

        # 4. Smooth bend check
        turn_deg = max_turn_angle_deg(pts)
        if turn_deg >= 25.0:
            violations.append(f"{r.wire_number}: max_turn_angle_deg={turn_deg}° >= 25.0°")

        per_wire.append(
            {
                "wire_number": r.wire_number,
                "from": r.from_endpoint,
                "to": r.to_endpoint,
                "via": list(r.via),
                "idx_enter": idx_enter,
                "idx_exit": idx_exit,
                "total_points": len(pts),
                "outside_tray_points": len(outside_pts),
                "max_turn_angle_deg": turn_deg,
            }
        )

    return {
        "ok": len(violations) == 0,
        "wire_count": len(routes),
        "tray_count": len(tray_rects),
        "violations": violations,
        "wires": per_wire,
    }
