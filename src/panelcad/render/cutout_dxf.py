"""1:1 cutout DXF for a CNC or laser shop.

Origin is the bottom-left of the viewed panel, +X right, +Y up. Hole-table
X/Y stay in the sheet datum (top-left, Y down) and are labeled as such.
Unknown openings are text only — nothing is written on CUT or DRILL for them.
"""

from __future__ import annotations

import math
from pathlib import Path

import ezdxf
from ezdxf import units as dxf_units
from ezdxf.enums import TextEntityAlignment

from panelcad.core.cutout import (
    MM_PER_INCH,
    CutFeature,
    SurfaceDrawing,
    d_cut_paths,
    local_holes,
    local_profile,
    opening_reach_mm,
    opens_as_circle,
    surface_title,
    transform_local,
    view_point,
)
from panelcad.core.geometry import rotate_point
from panelcad.core.models import Project

DXF_VERSION = "R2010"
LAYERS: dict[str, int] = {
    "CUT": 1,
    "DRILL": 5,
    "DIM": 3,
    "TEXT": 7,
    "OUTLINE": 8,
}


def write_cutout_dxf(path: Path | str, drawing: SurfaceDrawing, project: Project | None = None) -> Path:
    """Write one surface. Re-opens the file with ezdxf and requires the shop layers."""

    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    doc = _document(drawing)
    _draw(doc, drawing, project)
    doc.saveas(str(dest))
    _require_layers(dest)
    return dest


def write_cutout_dxfs(
    dest: Path | str,
    drawings: list[SurfaceDrawing],
    project: Project | None = None,
) -> list[Path]:
    """One DXF per surface. A ``.dxf`` path with several sheets becomes ``stem_surface.dxf``."""

    target = Path(dest)
    if not drawings:
        raise ValueError("no cutout sheets to export")
    paths: list[Path] = []
    if target.suffix.lower() == ".dxf" and len(drawings) == 1:
        paths.append(write_cutout_dxf(target, drawings[0], project))
        return paths
    folder = target if target.suffix.lower() != ".dxf" else target.parent
    folder.mkdir(parents=True, exist_ok=True)
    stem = target.stem if target.suffix.lower() == ".dxf" else ""
    for drawing in drawings:
        name = f"{stem}_{drawing.surface}.dxf" if stem else f"{drawing.surface}.dxf"
        paths.append(write_cutout_dxf(folder / name, drawing, project))
    return paths


def _document(drawing: SurfaceDrawing) -> ezdxf.Drawing:
    doc = ezdxf.new(DXF_VERSION, setup=True)
    if drawing.units == "inch":
        doc.units = dxf_units.IN
        doc.header["$INSUNITS"] = int(dxf_units.IN)
        doc.header["$MEASUREMENT"] = 0
    else:
        doc.units = dxf_units.MM
        doc.header["$INSUNITS"] = int(dxf_units.MM)
        doc.header["$MEASUREMENT"] = 1
    doc.header["$LUNITS"] = 2
    doc.header["$LUPREC"] = 4 if drawing.units == "inch" else 3
    for name, color in LAYERS.items():
        doc.layers.add(name, color=color)
    return doc


def _unit(drawing: SurfaceDrawing) -> float:
    return MM_PER_INCH if drawing.units == "inch" else 1.0


def _xy(x_mm: float, y_mm: float, height_mm: float, unit: float) -> tuple[float, float]:
    """View millimetres (Y down) to DXF (Y up, drawing units)."""

    return (x_mm / unit, (height_mm - y_mm) / unit)


def _draw(doc: ezdxf.Drawing, drawing: SurfaceDrawing, project: Project | None) -> None:
    msp = doc.modelspace()
    unit = _unit(drawing)
    height = drawing.panel_height_mm
    width = drawing.panel_width_mm
    text_h = 2.5 / unit

    if drawing.outline_set:
        corners = [
            _xy(0.0, 0.0, height, unit),
            _xy(width, 0.0, height, unit),
            _xy(width, height, height, unit),
            _xy(0.0, height, height, unit),
        ]
        msp.add_lwpolyline(corners, close=True, dxfattribs={"layer": "OUTLINE"})

    for feature in drawing.features:
        _draw_feature(msp, drawing, feature, unit)

    if drawing.outline_set:
        _draw_dimensions(msp, drawing, unit, text_h)
        _draw_table(msp, drawing, unit, text_h)
        _draw_title(msp, drawing, project, unit, text_h)


def _draw_feature(msp, drawing: SurfaceDrawing, feature: CutFeature, unit: float) -> None:
    height = drawing.panel_height_mm
    width = drawing.panel_width_mm if drawing.panel_width_mm > 0 else 0.0
    vx, vy = feature.view_center(width, drawing.view)
    if feature.cutout.unknown or not feature.cutout.shape:
        center = _xy(vx, vy, height, unit)
        msp.add_text(
            f"{feature.ref} CUTOUT UNKNOWN",
            dxfattribs={"layer": "TEXT", "height": 2.5 / unit},
        ).set_placement(center, align=TextEntityAlignment.MIDDLE_CENTER)
        return
    if opens_as_circle(feature.cutout, feature.include_key):
        radius = float(feature.cutout.diameter_mm or 0.0) / 2.0 / unit
        msp.add_circle(_xy(vx, vy, height, unit), radius, dxfattribs={"layer": "CUT"})
    elif feature.cutout.cuts_key(feature.include_key) and feature.cutout.diameter_mm:
        _draw_d_hole(msp, drawing, feature, unit)
    elif feature.cutout.shape == "rect" and float(feature.cutout.corner_radius_mm or 0.0) > 0:
        _draw_rounded_rect(msp, drawing, feature, unit)
    else:
        points = transform_local(
            local_profile(feature.cutout, include_key=feature.include_key),
            cx=feature.cx,
            cy=feature.cy,
            rotation=feature.rotation,
            panel_width=width,
            view=drawing.view,
        )
        mapped = [_xy(x, y, height, unit) for x, y in points]
        if len(mapped) >= 2 and mapped[0] == mapped[-1]:
            mapped = mapped[:-1]
        if len(mapped) >= 2:
            msp.add_lwpolyline(mapped, close=True, dxfattribs={"layer": "CUT"})
    for hx, hy, diameter in local_holes(feature.cutout):
        rx, ry = rotate_point(hx, hy, feature.rotation)
        hvx, hvy = view_point(feature.cx + rx, feature.cy + ry, width, drawing.view)
        msp.add_circle(
            _xy(hvx, hvy, height, unit),
            (diameter / 2.0) / unit,
            dxfattribs={"layer": "DRILL"},
        )
    msp.add_text(
        feature.ref,
        dxfattribs={"layer": "TEXT", "height": 2.5 / unit},
    ).set_placement(_xy(vx, vy - _tag_rise(feature), height, unit), align=TextEntityAlignment.MIDDLE_CENTER)


def _draw_rounded_rect(msp, drawing: SurfaceDrawing, feature: CutFeature, unit: float) -> None:
    """Closed LWPOLYLINE with a bulge on each corner, so the radius is a true arc."""

    cutout = feature.cutout
    half_w = float(cutout.width_mm or 0.0) / 2.0
    half_h = float(cutout.height_mm or 0.0) / 2.0
    radius = min(float(cutout.corner_radius_mm or 0.0), half_w, half_h)
    if radius <= 1e-6 or half_w <= 0 or half_h <= 0:
        return
    # Positive bulge is counter-clockwise in DXF (Y up). A quarter turn is tan(π/8).
    bulge = math.tan(math.pi / 8.0)
    if drawing.view == "back":
        bulge = -bulge
    height = drawing.panel_height_mm
    width = drawing.panel_width_mm if drawing.panel_width_mm > 0 else 0.0

    def _at(local_x: float, local_y_up: float) -> tuple[float, float]:
        # Local Y-up offsets, then the same Y-down rotation the rest of the sheet uses.
        rx, ry = rotate_point(local_x, -local_y_up, feature.rotation)
        wx, wy = feature.cx + rx, feature.cy + ry
        if drawing.view == "back":
            wx = width - wx
        return _xy(wx, wy, height, unit)

    # Counter-clockwise in Y-up, starting on the bottom edge. Bulge sits on the
    # vertex that begins each corner arc.
    corners = (
        (-half_w + radius, -half_h, 0.0),
        (half_w - radius, -half_h, bulge),
        (half_w, -half_h + radius, 0.0),
        (half_w, half_h - radius, bulge),
        (half_w - radius, half_h, 0.0),
        (-half_w + radius, half_h, bulge),
        (-half_w, half_h - radius, 0.0),
        (-half_w, -half_h + radius, bulge),
    )
    points = []
    for local_x, local_y, turn in corners:
        x, y = _at(local_x, local_y)
        points.append((x, y, 0.0, 0.0, turn))
    msp.add_lwpolyline(points, format="xyseb", close=True, dxfattribs={"layer": "CUT"})


def _draw_d_hole(msp, drawing: SurfaceDrawing, feature: CutFeature, unit: float) -> None:
    """ARC plus the key lines, so the round part is exactly the nominal diameter."""

    height = drawing.panel_height_mm
    arc, lines = d_cut_paths(
        feature.cutout,
        cx=feature.cx,
        cy=feature.cy,
        rotation=feature.rotation,
        panel_width=drawing.panel_width_mm,
        view=drawing.view,
    )
    center = _xy(arc.cx, arc.cy, height, unit)
    start = arc.start_deg % 360.0
    end = (arc.start_deg + arc.sweep_deg) % 360.0
    msp.add_arc(
        center,
        arc.radius / unit,
        start_angle=start,
        end_angle=end,
        dxfattribs={"layer": "CUT"},
    )
    for line in lines:
        msp.add_line(
            _xy(line.x1, line.y1, height, unit),
            _xy(line.x2, line.y2, height, unit),
            dxfattribs={"layer": "CUT"},
        )


def _tag_rise(feature: CutFeature) -> float:
    """Distance above the centre to the tag, past the rotated opening."""

    reach = opening_reach_mm(
        feature.cutout,
        include_key=feature.include_key,
        rotation=feature.rotation,
        downward=False,
    )
    return reach + 4.0


def _draw_dimensions(msp, drawing: SurfaceDrawing, unit: float, text_h: float) -> None:
    width = drawing.panel_width_mm
    height = drawing.panel_height_mm
    from_right = str(drawing.datum).endswith("right")
    from_bottom = str(drawing.datum).startswith("bottom")
    datum_x = width if from_right else 0.0
    datum_y = height if from_bottom else 0.0
    for mark in drawing.dims:
        gap = 8.0 + mark.stack * 8.0
        if mark.axis == "x":
            y_edge = height if from_bottom else 0.0
            y_dim = (height + gap) if from_bottom else -gap
            text_y = y_dim + 4.0 if from_bottom else y_dim - 4.0
            msp.add_line(
                _xy(mark.feature_x, y_edge, height, unit),
                _xy(mark.feature_x, y_dim, height, unit),
                dxfattribs={"layer": "DIM"},
            )
            msp.add_line(
                _xy(datum_x, y_dim, height, unit),
                _xy(mark.feature_x, y_dim, height, unit),
                dxfattribs={"layer": "DIM"},
            )
            msp.add_text(mark.label, dxfattribs={"layer": "DIM", "height": text_h}).set_placement(
                _xy(mark.feature_x, text_y, height, unit),
                align=TextEntityAlignment.MIDDLE_CENTER,
            )
        else:
            x_edge = width if from_right else 0.0
            x_dim = (width + gap) if from_right else -gap
            text_x = x_dim + 4.0 if from_right else x_dim - 4.0
            msp.add_line(
                _xy(x_edge, mark.feature_y, height, unit),
                _xy(x_dim, mark.feature_y, height, unit),
                dxfattribs={"layer": "DIM"},
            )
            msp.add_line(
                _xy(x_dim, datum_y, height, unit),
                _xy(x_dim, mark.feature_y, height, unit),
                dxfattribs={"layer": "DIM"},
            )
            msp.add_text(
                mark.label,
                dxfattribs={"layer": "DIM", "height": text_h, "rotation": 90},
            ).set_placement(
                _xy(text_x, mark.feature_y, height, unit),
                align=TextEntityAlignment.MIDDLE_CENTER,
            )


def _draw_table(msp, drawing: SurfaceDrawing, unit: float, text_h: float) -> None:
    height = drawing.panel_height_mm
    x0 = drawing.panel_width_mm + 12.0
    y0 = 0.0
    headers = ("TAG", "SIZE", "QTY", "X", "Y")
    spans = (28.0, 46.0, 14.0, 16.0, 16.0)
    row_h = 6.0
    msp.add_text(
        "HOLE TABLE  (X/Y from datum, Y down on the sheet)",
        dxfattribs={"layer": "TEXT", "height": text_h},
    ).set_placement(_xy(x0, y0 - 4.0, height, unit), align=TextEntityAlignment.LEFT)
    cursor = y0 + 4.0
    _table_row(msp, headers, x0, cursor, spans, height, unit, text_h, header=True)
    cursor += row_h
    for row in drawing.rows:
        qty = "—" if row.qty == 0 else str(row.qty)
        values = (
            row.tag,
            row.size,
            qty,
            _len(row.x_mm, drawing.units),
            _len(row.y_mm, drawing.units),
        )
        _table_row(msp, values, x0, cursor, spans, height, unit, text_h, header=False)
        cursor += row_h


def _table_row(msp, values, x0, y_down, spans, height, unit, text_h, *, header: bool) -> None:
    x = x0
    for value, span in zip(values, spans, strict=True):
        msp.add_text(
            str(value),
            dxfattribs={"layer": "TEXT", "height": text_h * (0.9 if not header else 1.0)},
        ).set_placement(_xy(x, y_down, height, unit), align=TextEntityAlignment.LEFT)
        x += span


def _draw_title(msp, drawing: SurfaceDrawing, project: Project | None, unit: float, text_h: float) -> None:
    tb = project.title_block if project is not None else None
    company = (tb.company if tb else "") or "PanelCAD"
    number = (tb.drawing_number if tb else "") or ""
    rev = ""
    if tb and tb.revisions:
        rev = tb.revisions[0].rev or ""
    height = drawing.panel_height_mm
    y = height + 14.0
    x = 0.0
    view = "VIEW FROM BACK" if drawing.view == "back" else "VIEW FROM FRONT"
    lines = [
        f"{surface_title(drawing.surface).upper()} CUTOUT AND DRILL",
        f"{company}    {number}    REV {rev}".strip(),
        (
            f"{view}    DATUM {drawing.datum.replace('_', ' ').upper()}    "
            f"{drawing.style.upper()}    SCALE 1:1    {drawing.units.upper()}"
        ),
        "DXF origin is the bottom-left of this view, Y up. Hole-table X/Y use the datum, Y down.",
    ]
    for source in drawing.sources:
        lines.append(source)
    for index, line in enumerate(lines):
        msp.add_text(line, dxfattribs={"layer": "TEXT", "height": text_h}).set_placement(
            _xy(x, y + index * 5.0, height, unit),
            align=TextEntityAlignment.LEFT,
        )


def _len(value_mm: float, units: str) -> str:
    if units == "inch":
        return f"{value_mm / MM_PER_INCH:.3f}"
    if abs(value_mm - round(value_mm)) < 1e-6:
        return f"{value_mm:.0f}"
    return f"{value_mm:.1f}"


def _require_layers(path: Path) -> None:
    doc = ezdxf.readfile(str(path))
    names = {layer.dxf.name for layer in doc.layers}
    missing = set(LAYERS) - names
    if missing:
        raise RuntimeError(f"{path.name} is missing layers {', '.join(sorted(missing))}")
