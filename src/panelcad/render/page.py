"""Paint a full sheet (frame + title block + devices + wires) onto a Qt paint device.

Used by PDF export and File → Print so the bottom information boxes are never
clipped by printer/PDF margins.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMarginsF, QRectF, QSizeF
from PySide6.QtGui import QColor, QPageLayout, QPageSize, QPagedPaintDevice, QPainter

from panelcad.core.models import Project, Sheet
from panelcad.library.catalog import Catalog
from panelcad.render.contents import draw_sheet_body
from panelcad.render.qt_painter import QtPainter
from panelcad.render.titleblock import draw_sheet_frame, share_title_nameplate

PAPER = QColor("#f7f4ea")  # on-screen sheet theme; print/PDF uses white
PRINT_PAPER = QColor("#ffffff")


def configure_full_page(device: QPagedPaintDevice, width_mm: float, height_mm: float) -> None:
    page_size = QPageSize(QSizeF(width_mm, height_mm), QPageSize.Unit.Millimeter)
    layout = QPageLayout(
        page_size,
        QPageLayout.Orientation.Portrait,
        QMarginsF(0.0, 0.0, 0.0, 0.0),
        QPageLayout.Unit.Millimeter,
    )
    mode = getattr(QPageLayout, "FullPageMode", None)
    if mode is None:
        modes = getattr(QPageLayout, "Mode", None)
        mode = getattr(modes, "FullPageMode", None) if modes is not None else None
    if mode is not None and hasattr(layout, "setMode"):
        layout.setMode(mode)
    device.setPageLayout(layout)
    if hasattr(device, "setFullPage"):
        device.setFullPage(True)


def paint_sheet(
    painter: QPainter,
    project: Project,
    sheet: Sheet,
    catalog: Catalog,
    *,
    sheet_index: int = 0,
    sheet_count: int = 1,
    base_dir: Path | None = None,
) -> None:
    """Paint in millimetre coordinates (origin top-left). Caller must scale the painter."""
    width, height = sheet.page.dimensions()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.fillRect(QRectF(0, 0, width, height), PRINT_PAPER)
    qp = QtPainter(painter, print_paper=True)
    draw_sheet_frame(
        qp,
        project,
        sheet,
        sheet_index=sheet_index,
        sheet_count=sheet_count,
        base_dir=base_dir,
        catalog=catalog,
    )
    from panelcad.core.ladder import sheet_drawing_style

    coil = getattr(project.title_block, "coil_style", "circle") or "circle"
    style = sheet_drawing_style(sheet, project)
    draw_sheet_body(
        qp,
        sheet,
        catalog,
        base_dir=base_dir,
        coil_style=coil,
        drawing_style=style,
        project=project,
        sheet_no=sheet_index + 1,
    )
    from panelcad.render.clouds import draw_revision_clouds

    draw_revision_clouds(qp, project, sheet, catalog)


def paint_sheet_on_device(
    device: QPagedPaintDevice,
    project: Project,
    sheet: Sheet,
    catalog: Catalog,
    *,
    sheet_index: int = 0,
    sheet_count: int = 1,
    base_dir: Path | None = None,
) -> None:
    paint_project_on_device(
        device,
        project,
        catalog,
        sheets=[sheet],
        base_dir=base_dir,
        number_from_project=False,
        sheet_index_override=sheet_index,
        sheet_count_override=sheet_count,
    )


def paint_project_on_device(
    device: QPagedPaintDevice,
    project: Project,
    catalog: Catalog,
    *,
    sheets: list[Sheet] | None = None,
    base_dir: Path | None = None,
    number_from_project: bool = True,
    sheet_index_override: int | None = None,
    sheet_count_override: int | None = None,
) -> None:
    """Print every sheet as its own page. Title block SHEET n / N follows project order."""
    pages = list(sheets) if sheets is not None else list(project.sheets)
    if not pages:
        raise ValueError("project has no sheets to print")
    w0, h0 = pages[0].page.dimensions()
    configure_full_page(device, w0, h0)
    painter = QPainter(device)
    try:
        with share_title_nameplate():
            _paint_project_pages(
                painter,
                device,
                project,
                catalog,
                pages,
                base_dir=base_dir,
                number_from_project=number_from_project,
                sheet_index_override=sheet_index_override,
                sheet_count_override=sheet_count_override,
            )
    finally:
        if painter.isActive():
            painter.end()


def _paint_project_pages(
    painter: QPainter,
    device: QPagedPaintDevice,
    project: Project,
    catalog: Catalog,
    pages: list[Sheet],
    *,
    base_dir: Path | None,
    number_from_project: bool,
    sheet_index_override: int | None,
    sheet_count_override: int | None,
) -> None:
    total = sheet_count_override if sheet_count_override is not None else len(project.sheets)
    for i, sheet in enumerate(pages):
        width, height = sheet.page.dimensions()
        if i > 0:
            configure_full_page(device, width, height)
            device.newPage()
        dpi_x = float(device.logicalDpiX() or 72)
        dpi_y = float(device.logicalDpiY() or 72)
        painter.resetTransform()
        painter.scale(dpi_x / 25.4, dpi_y / 25.4)
        if number_from_project and sheet in project.sheets:
            idx = project.sheets.index(sheet)
        elif sheet_index_override is not None and len(pages) == 1:
            idx = sheet_index_override
        else:
            idx = i
        paint_sheet(
            painter,
            project,
            sheet,
            catalog,
            sheet_index=idx,
            sheet_count=total,
            base_dir=base_dir,
        )
