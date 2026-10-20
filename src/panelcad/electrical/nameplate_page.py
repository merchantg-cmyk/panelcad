"""Real-size nameplate PDF, engraving DXF, and the print-package page.

PDF pages are the plate (and the caution label, when one is required) at
1:1 millimetres. The DXF uses TEXT, CUT, and HOLE only. Engraved rules stay
on TEXT so a through-cut does not follow them.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import ezdxf
from ezdxf import units as dxf_units
from ezdxf.enums import TextEntityAlignment

from panelcad.electrical.nameplate import Mark, Nameplate, _wrap

DXF_VERSION = "R2010"
_LAYERS = {"TEXT": 7, "CUT": 1, "HOLE": 5}


def _ensure_app() -> None:
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        QApplication([])


def paint_marks(painter, marks: list[Mark], width_mm: float, height_mm: float) -> None:
    """Draw one plate. ``painter`` is a PanelCAD painter (Qt or the package page)."""

    painter.set_layer("TEXT")
    painter.set_fill("#ffffff")
    painter.set_stroke("#ffffff", 0.2)
    painter.rect(0, 0, width_mm, height_mm)
    for mark in marks:
        painter.set_layer(mark.layer if mark.layer in _LAYERS else "TEXT")
        if mark.kind == "rect":
            painter.set_fill(None)
            width = 0.4 if mark.layer == "CUT" else 0.2
            painter.set_stroke("#111111", width)
            painter.rect(mark.x, mark.y, mark.w, mark.h)
        elif mark.kind == "line":
            painter.set_stroke("#111111", 0.2)
            painter.line(mark.x, mark.y, mark.x + mark.w, mark.y)
        elif mark.kind == "circle":
            painter.set_fill(None)
            painter.set_stroke("#111111", 0.3)
            painter.circle(mark.x, mark.y, mark.r)
        elif mark.kind == "text" and mark.text:
            painter.set_fill("#111111")
            painter.text(mark.x, mark.y, mark.text, mark.size, align=mark.align or "start")


def _write_one_pdf(path: Path, width_mm: float, height_mm: float, marks: list[Mark], title: str) -> None:
    """One page, sized before the painter starts.

    QPdfWriter keeps the first page size on ``newPage()``, which clipped the
    caution label. Each page is its own file and the caller merges them.
    """

    from PySide6.QtGui import QPainter, QPdfWriter

    from panelcad.render.page import configure_full_page
    from panelcad.render.qt_painter import QtPainter

    path.parent.mkdir(parents=True, exist_ok=True)
    writer = QPdfWriter(str(path))
    writer.setResolution(300)
    writer.setTitle(title)
    writer.setCreator("PanelCAD")
    configure_full_page(writer, width_mm, height_mm)
    painter = QPainter(writer)
    try:
        dpi_x = float(writer.logicalDpiX() or 72)
        dpi_y = float(writer.logicalDpiY() or 72)
        painter.scale(dpi_x / 25.4, dpi_y / 25.4)
        paint_marks(QtPainter(painter, print_paper=True), marks, width_mm, height_mm)
    finally:
        if painter.isActive():
            painter.end()


def _merge_pdfs(dest: Path, parts: list[Path]) -> None:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for part in parts:
        reader = PdfReader(str(part))
        writer.add_page(reader.pages[0])
    with dest.open("wb") as handle:
        writer.write(handle)


def write_nameplate_pdf(path: Path | str, plate: Nameplate) -> Path:
    _ensure_app()
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    pages: list[tuple[float, float, list[Mark], str]] = [
        (plate.width_mm, plate.height_mm, plate.marks, "Nameplate")
    ]
    if plate.caution_marks and plate.caution_size_mm:
        pages.append((plate.caution_size_mm[0], plate.caution_size_mm[1], plate.caution_marks, "Nameplate caution"))
    if len(pages) == 1:
        width, height, marks, title = pages[0]
        _write_one_pdf(dest, width, height, marks, title)
        return dest
    temps: list[Path] = []
    try:
        for index, (width, height, marks, title) in enumerate(pages):
            temp = dest.with_name(f"{dest.stem}.page{index}{dest.suffix}")
            _write_one_pdf(temp, width, height, marks, title)
            temps.append(temp)
        _merge_pdfs(dest, temps)
    finally:
        for temp in temps:
            temp.unlink(missing_ok=True)
    return dest


def write_nameplate_png(path: Path | str, plate: Nameplate, *, scale: float = 8.0) -> Path:
    from PySide6.QtGui import QColor, QImage, QPainter

    from panelcad.render.qt_painter import QtPainter

    _ensure_app()
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(
        max(1, math.ceil(plate.width_mm * scale)),
        max(1, math.ceil(plate.height_mm * scale)),
        QImage.Format.Format_RGB32,
    )
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    try:
        painter.scale(scale, scale)
        paint_marks(QtPainter(painter, print_paper=True), plate.marks, plate.width_mm, plate.height_mm)
    finally:
        if painter.isActive():
            painter.end()
    image.save(str(dest))
    return dest


def write_caution_png(path: Path | str, plate: Nameplate, *, scale: float = 8.0) -> Path | None:
    if not plate.caution_marks or not plate.caution_size_mm:
        return None
    from PySide6.QtGui import QColor, QImage, QPainter

    from panelcad.render.qt_painter import QtPainter

    _ensure_app()
    width, height = plate.caution_size_mm
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(
        max(1, math.ceil(width * scale)),
        max(1, math.ceil(height * scale)),
        QImage.Format.Format_RGB32,
    )
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    try:
        painter.scale(scale, scale)
        paint_marks(QtPainter(painter, print_paper=True), plate.caution_marks, width, height)
    finally:
        if painter.isActive():
            painter.end()
    image.save(str(dest))
    return dest


def _dxf_doc():
    doc = ezdxf.new(DXF_VERSION)
    doc.units = dxf_units.MM
    for name, color in _LAYERS.items():
        doc.layers.add(name, color=color)
    return doc


def _write_dxf(path: Path, marks: list[Mark], width: float, height: float) -> Path:
    doc = _dxf_doc()
    msp = doc.modelspace()

    def up(y: float) -> float:
        return height - y

    for mark in marks:
        layer = mark.layer if mark.layer in _LAYERS else "TEXT"
        if mark.kind == "rect":
            x0, y0 = mark.x, up(mark.y + mark.h)
            x1, y1 = mark.x + mark.w, up(mark.y)
            msp.add_lwpolyline(
                [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
                close=True,
                dxfattribs={"layer": layer},
            )
        elif mark.kind == "line":
            msp.add_line((mark.x, up(mark.y)), (mark.x + mark.w, up(mark.y)), dxfattribs={"layer": layer})
        elif mark.kind == "circle":
            msp.add_circle((mark.x, up(mark.y)), mark.r, dxfattribs={"layer": layer})
        elif mark.kind == "text" and mark.text:
            entity = msp.add_text(mark.text, dxfattribs={"layer": "TEXT", "height": mark.size})
            point = (mark.x, up(mark.y))
            if mark.align == "middle":
                entity.set_placement(point, align=TextEntityAlignment.MIDDLE_CENTER)
            elif mark.align == "end":
                entity.set_placement(point, align=TextEntityAlignment.RIGHT)
            else:
                entity.set_placement(point, align=TextEntityAlignment.LEFT)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    return path


def write_nameplate_dxf(path: Path | str, plate: Nameplate) -> list[Path]:
    """Plate DXF, plus a sibling ``-caution`` file when the warning label applies."""

    dest = Path(path)
    written = [_write_dxf(dest, plate.marks, plate.width_mm, plate.height_mm)]
    if plate.caution_marks and plate.caution_size_mm:
        caution = dest.with_name(f"{dest.stem}-caution{dest.suffix}")
        written.append(_write_dxf(caution, plate.caution_marks, plate.caution_size_mm[0], plate.caution_size_mm[1]))
    return written


def paint_package_page(painter, plate: Nameplate, width_mm: float, height_mm: float, project_name: str) -> None:
    """Letter (or other report) page: the plate at real size, the working, and the caution label."""

    painter.set_fill("#ffffff")
    painter.set_stroke("#111111", 0.4)
    painter.rect(8, 8, width_mm - 16, height_mm - 16)
    painter.set_fill("#111111")
    painter.text(14, 18, "NAMEPLATE", 4.2, align="start")
    painter.text(14, 24, project_name or "Panel", 2.6, align="start")
    painter.text(
        14,
        29,
        f"Plate {plate.width_mm / 25.4:.2f} in × {plate.height_mm / 25.4:.2f} in, drawn at real size",
        2.1,
        align="start",
    )
    ox = 14.0
    oy = 34.0
    shifted = _Shifted(painter, ox, oy)
    paint_marks(shifted, plate.marks, plate.width_mm, plate.height_mm)
    cursor = oy + plate.height_mm + 6.0
    if plate.caution_marks and plate.caution_size_mm:
        painter.set_fill("#111111")
        painter.text(14, cursor + 3, "MULTIPLE SUPPLY CAUTION — real size. Confirm the wording before ordering.", 2.0, align="start")
        cursor += 6.0
        caution = _Shifted(painter, 14.0, cursor)
        paint_marks(caution, plate.caution_marks, plate.caution_size_mm[0], plate.caution_size_mm[1])
        cursor += plate.caution_size_mm[1] + 5.0
    painter.set_fill("#111111")
    painter.text(14, cursor + 3, "FULL-LOAD WORKING", 2.4, align="start")
    cursor += 7.0
    for line in plate.lines:
        for part in _wrap(line.text, width_mm - 28.0, 1.7) or [""]:
            if cursor > height_mm - 12:
                painter.text(14, cursor, "Further working lines are in Reports → Nameplate.", 1.8, align="start")
                return
            painter.text(14, cursor, part, 1.7, align="start")
            cursor += 3.4


class _Shifted:
    """Offset a painter without requiring save/restore."""

    def __init__(self, painter, dx: float, dy: float) -> None:
        self._painter = painter
        self._dx = dx
        self._dy = dy

    def set_layer(self, name: str) -> None:
        self._painter.set_layer(name)

    def set_fill(self, color: str | None) -> None:
        self._painter.set_fill(color)

    def set_stroke(self, color: str, width: float) -> None:
        self._painter.set_stroke(color, width)

    def rect(self, x: float, y: float, w: float, h: float) -> None:
        self._painter.rect(x + self._dx, y + self._dy, w, h)

    def line(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self._painter.line(x1 + self._dx, y1 + self._dy, x2 + self._dx, y2 + self._dy)

    def circle(self, x: float, y: float, r: float) -> None:
        self._painter.circle(x + self._dx, y + self._dy, r)

    def text(self, x: float, y: float, text: str, size: float, align: str = "middle", rotate: float = 0) -> None:
        self._painter.text(x + self._dx, y + self._dy, text, size, align=align, rotate=rotate)
