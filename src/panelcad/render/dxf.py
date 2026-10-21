"""AutoCAD-compatible DXF export (R2010, millimetres).

One file per sheet. Schematic pages stay in sheet millimetres (same as SVG).
Panel and door layouts with an enclosure are written in true millimetres so a
45 mm contactor measures 45 drawing units. Y is flipped to AutoCAD's Y-up.
Text is native TEXT, not exploded geometry.
"""

from __future__ import annotations

import re
from pathlib import Path

import ezdxf
from ezdxf import colors as dxf_colors
from ezdxf import units as dxf_units
from ezdxf.enums import TextEntityAlignment

from panelcad.core.layout import is_layout_sheet
from panelcad.core.models import Project, Sheet
from panelcad.library.catalog import Catalog
from panelcad.library.panel import (
    display_footprint_size,
    effective_footprint,
    is_layout_ready,
    occupies_panel_layout,
)
from panelcad.render.contents import draw_sheet_body
from panelcad.render.painter import Painter
from panelcad.render.titleblock import TITLE_H, draw_revision_table, draw_sheet_frame

DXF_VERSION = "R2010"

LAYERS: dict[str, int] = {
    "OUTLINE": 8,
    "DEVICES": 7,
    "WIRES": 5,
    "TEXT": 7,
    "WIRE_NUMBERS": 3,
    "XREF": 4,
    "DUCT": 6,
    "RAIL": 8,
    "CUTOUTS": 1,
    "TITLEBLOCK": 7,
    "REVCLOUD": 1,
}

_LINEWEIGHTS = (
    5,
    9,
    13,
    15,
    18,
    20,
    25,
    30,
    35,
    40,
    50,
    53,
    60,
    70,
    80,
    90,
    100,
    106,
    120,
    140,
    158,
    200,
    211,
)

_ALIGN = {
    "start": TextEntityAlignment.LEFT,
    "middle": TextEntityAlignment.CENTER,
    "end": TextEntityAlignment.RIGHT,
}

_MOUNT_MM = re.compile(r"(\d+(?:\.\d+)?)\s*mm", re.IGNORECASE)


def _rgb(color: str) -> tuple[int, int, int]:
    raw = (color or "").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) != 6:
        return (17, 17, 17)
    try:
        return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))
    except ValueError:
        return (17, 17, 17)


def _lineweight(width_mm: float) -> int:
    hundredths = max(5, int(round(max(0.05, float(width_mm)) * 100.0)))
    return min(_LINEWEIGHTS, key=lambda value: abs(value - hundredths))


def _is_fill(color: str | None) -> bool:
    if not color:
        return False
    token = color.strip().lower()
    return token not in {"none", "transparent"}


class DxfPainter(Painter):
    """Painter that writes ezdxf entities in millimetres, Y-up."""

    def __init__(
        self,
        doc: ezdxf.Drawing,
        *,
        origin: tuple[float, float] = (0.0, 0.0),
        unit_scale: float = 1.0,
        flip_height: float = 0.0,
    ) -> None:
        self.doc = doc
        self.msp = doc.modelspace()
        self.origin_x, self.origin_y = origin
        self.unit_scale = unit_scale if unit_scale > 1e-12 else 1.0
        self.flip_height = float(flip_height)
        self._stroke = "#111111"
        self._sw = 0.35
        self._fill: str | None = None
        self._layer = "0"
        self._true_scale = False

    def set_true_scale(self, value: bool) -> None:
        self._true_scale = bool(value)

    def layout_is_true_scale(self) -> bool:
        return self._true_scale

    def set_layer(self, name: str) -> None:
        self._layer = name if name in LAYERS else (name or "0")

    def set_stroke(self, color: str, width: float) -> None:
        self._stroke = color
        self._sw = width

    def set_fill(self, color: str | None) -> None:
        self._fill = color

    def _pt(self, x: float, y: float) -> tuple[float, float]:
        tx = (x - self.origin_x) * self.unit_scale
        ty = (y - self.origin_y) * self.unit_scale
        return (tx, self.flip_height - ty)

    def _attribs(self, *, color: str | None = None) -> dict[str, object]:
        rgb = _rgb(color or self._stroke)
        return {
            "layer": self._layer if self._layer in self.doc.layers else "0",
            "true_color": dxf_colors.rgb2int(rgb),
            "lineweight": _lineweight(self._sw),
        }

    def _stroke_poly(self, points: list[tuple[float, float]], *, close: bool) -> None:
        if len(points) < 2:
            return
        mapped = [self._pt(x, y) for x, y in points]
        self.msp.add_lwpolyline(mapped, close=close, dxfattribs=self._attribs())

    def _hatch(self, points: list[tuple[float, float]]) -> None:
        if not _is_fill(self._fill) or len(points) < 3:
            return
        mapped = [self._pt(x, y) for x, y in points]
        hatch = self.msp.add_hatch(dxfattribs={"layer": self._attribs()["layer"]})
        hatch.set_solid_fill(color=7)
        hatch.dxf.true_color = dxf_colors.rgb2int(_rgb(self._fill or "#cccccc"))
        hatch.paths.add_polyline_path(mapped, is_closed=True)

    def rect(self, x: float, y: float, w: float, h: float) -> None:
        pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        self._hatch(pts)
        self._stroke_poly(pts, close=True)

    def line(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self.msp.add_line(self._pt(x1, y1), self._pt(x2, y2), dxfattribs=self._attribs())

    def arc(self, cx: float, cy: float, radius: float, start_deg: float, sweep_deg: float) -> None:
        radius_u = abs(radius) * self.unit_scale
        if radius_u <= 0 or abs(sweep_deg) < 1e-9:
            return
        # Y flip in ``_pt`` plus the page-up angle convention lands on DXF's CCW angles.
        start = float(start_deg) % 360.0
        end = (float(start_deg) + float(sweep_deg)) % 360.0
        if sweep_deg < 0:
            start, end = end, start
        if abs(start - end) < 1e-6:
            end = (start + 359.999) % 360.0
        self.msp.add_arc(self._pt(cx, cy), radius_u, start, end, dxfattribs=self._attribs())

    def circle(self, x: float, y: float, r: float) -> None:
        radius = abs(r) * self.unit_scale
        if radius <= 0:
            return
        center = self._pt(x, y)
        attribs = self._attribs()
        if _is_fill(self._fill):
            hatch = self.msp.add_hatch(dxfattribs={"layer": attribs["layer"]})
            hatch.set_solid_fill(color=7)
            hatch.dxf.true_color = dxf_colors.rgb2int(_rgb(self._fill or "#cccccc"))
            edge = hatch.paths.add_edge_path()
            edge.add_ellipse(center, major_axis=(radius, 0.0), ratio=1.0)
        self.msp.add_circle(center, radius, dxfattribs=attribs)

    def polyline(self, points: list[tuple[float, float]]) -> None:
        self._stroke_poly(points, close=False)

    def polygon(self, points: list[tuple[float, float]]) -> None:
        if len(points) < 3:
            return
        self._hatch(points)
        self._stroke_poly(points, close=True)

    def text(
        self,
        x: float,
        y: float,
        text: str,
        size: float,
        align: str = "middle",
        rotate: float = 0,
    ) -> None:
        if not text:
            return
        height = max(0.8, float(size) * self.unit_scale)
        entity = self.msp.add_text(
            text,
            height=height,
            dxfattribs={
                **self._attribs(color="#111111"),
                "style": "PANELCAD",
                "rotation": -float(rotate or 0),
            },
        )
        entity.set_placement(self._pt(x, y), align=_ALIGN.get(align, TextEntityAlignment.CENTER))


def _new_document() -> ezdxf.Drawing:
    doc = ezdxf.new(DXF_VERSION, setup=True)
    doc.units = dxf_units.MM
    doc.header["$INSUNITS"] = int(dxf_units.MM)
    doc.header["$MEASUREMENT"] = 1
    doc.header["$LUNITS"] = 2
    doc.header["$LUPREC"] = 3
    if "PANELCAD" not in doc.styles:
        doc.styles.add("PANELCAD", font="arial.ttf")
    for name, aci in LAYERS.items():
        if name not in doc.layers:
            doc.layers.add(name, color=aci)
    return doc


def _layout_transform(sheet: Sheet) -> tuple[tuple[float, float], float, float]:
    """Origin, unit scale (page → true mm), and Y-flip height for a layout sheet."""
    page_w, page_h = sheet.page.dimensions()
    enc = sheet.enclosure
    if enc is None:
        return (0.0, 0.0), 1.0, page_h
    scale = float(enc.draw_scale) if enc.draw_scale and enc.draw_scale > 1e-12 else 1.0
    return (float(enc.x), float(enc.y)), 1.0 / scale, float(enc.height_mm)


def _cutout_diameter_mm(device, component) -> float | None:
    props = {}
    if getattr(device, "properties", None):
        props.update(device.properties)
    if component is not None and getattr(component, "properties", None):
        props.update(component.properties)
    for key in ("mounting", "cutout", "hole", "cutout_mm"):
        raw = props.get(key)
        if raw is None:
            continue
        match = _MOUNT_MM.search(str(raw))
        if match:
            return float(match.group(1))
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def draw_door_cutouts(painter: Painter, sheet: Sheet, catalog: Catalog) -> list[str]:
    """True-size punch holes / openings for door-mounted hardware.

    Not-ready / generic parts produce no invented cutout. Their tags are
    listed as UNRESOLVED on the TEXT layer (same gate as ``cutout.py``).
    """
    painter.set_layer("CUTOUTS")
    painter.set_stroke("#cc0000", 0.35)
    painter.set_fill(None)
    unresolved: list[str] = []
    for component in sheet.components:
        device = catalog.get(component.library_id)
        if occupies_panel_layout(device, component) and not is_layout_ready(device, component):
            unresolved.append(component.id)
            continue
        width, height = display_footprint_size(device, component)
        if width <= 0 or height <= 0:
            if occupies_panel_layout(device, component):
                unresolved.append(component.id)
            continue
        hole = _cutout_diameter_mm(device, component)
        if hole and hole > 0:
            radius = (hole * 0.5) * component.visual_scale()
            painter.circle(component.x + width / 2.0, component.y + height / 2.0, radius)
            continue
        fp = effective_footprint(device, component)
        if fp.mount == "door":
            painter.rect(component.x, component.y, width, height)
    if unresolved:
        painter.set_layer("TEXT")
        painter.set_stroke("#cc0000", 0.35)
        painter.text(
            4.0,
            6.0,
            "UNRESOLVED: " + ", ".join(unresolved),
            3.0,
            align="start",
        )
    return unresolved


def _draw_layout_title_strip(
    painter: DxfPainter,
    project: Project,
    sheet: Sheet,
    width_mm: float,
    y0: float,
) -> None:
    """Compact title-block strip in true millimetres, sitting under the enclosure."""
    painter.set_layer("TITLEBLOCK")
    painter.set_stroke("#2a2a2a", 0.4)
    painter.set_fill("#f7f4ea")
    height = TITLE_H
    painter.rect(0.0, y0, max(120.0, width_mm), height)
    painter.set_fill(None)
    tb = project.title_block
    painter.text(4.0, y0 + 8.0, tb.company or project.name, 3.2, align="start")
    painter.text(4.0, y0 + 16.0, sheet.title or sheet.id, 2.6, align="start")
    rev = ""
    if tb.revisions:
        rev = str(tb.revisions[0].rev or "")
    painter.text(4.0, y0 + 24.0, f"DWG {tb.drawing_number or '-'}   REV {rev or '-'}   {sheet.kind}", 2.2, align="start")
    from panelcad.render.titleblock import sheet_scale_label

    painter.text(
        4.0,
        y0 + 32.0,
        f"{tb.standard or ''}  {tb.customer or ''}  SCALE {sheet_scale_label(sheet, tb)}".strip(),
        2.0,
        align="start",
    )
    if any(str(row.rev or row.date or row.description or "").strip() for row in tb.revisions):
        table_w = min(168.0, max(90.0, width_mm * 0.55))
        draw_revision_table(painter, max(0.0, width_mm - table_w), y0, table_w, height, tb)


def sheet_to_dxf(
    dest: Path | str,
    sheet: Sheet,
    catalog: Catalog,
    project: Project | None = None,
    sheet_index: int = 0,
    sheet_count: int = 1,
    base_dir: Path | None = None,
) -> Path:
    if project is None:
        project = Project(name=sheet.title or sheet.id, sheets=[sheet])
    _page_w, page_h = sheet.page.dimensions()
    layout = is_layout_sheet(sheet) and sheet.enclosure is not None
    if layout:
        origin, unit_scale, flip_height = _layout_transform(sheet)
    else:
        origin, unit_scale, flip_height = (0.0, 0.0), 1.0, page_h

    doc = _new_document()
    painter = DxfPainter(doc, origin=origin, unit_scale=unit_scale, flip_height=flip_height)
    painter.set_true_scale(layout)
    from panelcad.core.ladder import sheet_drawing_style

    coil = getattr(project.title_block, "coil_style", "circle") or "circle"
    style = sheet_drawing_style(sheet, project)

    if not layout:
        draw_sheet_frame(
            painter,
            project,
            sheet,
            sheet_index=sheet_index,
            sheet_count=sheet_count,
            base_dir=base_dir,
            catalog=catalog,
        )
    draw_sheet_body(
        painter,
        sheet,
        catalog,
        base_dir=base_dir,
        coil_style=coil,
        drawing_style=style,
        project=project,
    )
    from panelcad.render.clouds import draw_revision_clouds

    draw_revision_clouds(painter, project, sheet, catalog)
    if sheet.kind == "door_layout":
        draw_door_cutouts(painter, sheet, catalog)
    if layout and sheet.enclosure is not None:
        saved = (painter.origin_x, painter.origin_y, painter.unit_scale)
        painter.origin_x = 0.0
        painter.origin_y = 0.0
        painter.unit_scale = 1.0
        _draw_layout_title_strip(
            painter,
            project,
            sheet,
            float(sheet.enclosure.width_mm),
            float(sheet.enclosure.height_mm) + 8.0,
        )
        painter.origin_x, painter.origin_y, painter.unit_scale = saved

    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    return path


def project_to_dxf(
    project: Project,
    catalog: Catalog,
    dest: Path | str,
    *,
    sheets: list[Sheet] | None = None,
    base_dir: Path | None = None,
) -> list[Path]:
    """Write one DXF per sheet. ``dest`` is a directory or a ``.dxf`` stem."""
    root = Path(dest)
    pages = list(sheets) if sheets is not None else list(project.sheets)
    written: list[Path] = []
    count = len(project.sheets)
    for sheet in pages:
        index = project.sheets.index(sheet) if sheet in project.sheets else 0
        if root.suffix.lower() == ".dxf":
            if len(pages) == 1:
                path = root
            else:
                path = root.with_name(f"{root.stem}_{sheet.id}.dxf")
        else:
            path = root / f"{sheet.id}.dxf"
        written.append(
            sheet_to_dxf(
                path,
                sheet,
                catalog,
                project=project,
                sheet_index=index,
                sheet_count=count,
                base_dir=base_dir,
            )
        )
    return written
