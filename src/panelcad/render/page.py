"""Paint a full sheet (frame + title block + devices + wires) onto a Qt paint device.

Used by PDF export and File → Print so the bottom information boxes are never
clipped by printer/PDF margins.

File → Print fits each sheet into the printer's paint rect (paper size, orientation,
margins, DPI) and honors QPrinter page ranges / current page. Export PDF keeps one
native-size page per sheet via ``configure_full_page``.
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


def sheet_orientation(width_mm: float, height_mm: float) -> "QPageLayout.Orientation":
    if width_mm >= height_mm:
        return QPageLayout.Orientation.Landscape
    return QPageLayout.Orientation.Portrait


def apply_sheet_orientation(device: QPagedPaintDevice, width_mm: float, height_mm: float) -> None:
    """Match the device page orientation to the sheet without changing paper size.

    Must run before QPrintPreviewDialog paints (and before QPainter begins on a
    page). Changing orientation after the preview has locked a page buffer leaves
    a portrait bitmap while fit math may read landscape layout sizes — that clips
    the right edge of A3/landscape sheets.
    """
    if not hasattr(device, "setPageOrientation"):
        return
    device.setPageOrientation(sheet_orientation(width_mm, height_mm))


def sync_preview_orientation(preview, width_mm: float, height_mm: float) -> None:
    """Keep QPrintPreviewWidget's portrait/landscape chrome in sync with the sheet."""
    try:
        from PySide6.QtPrintSupport import QPrintPreviewWidget
    except ImportError:
        return
    orient = sheet_orientation(width_mm, height_mm)
    for widget in preview.findChildren(QPrintPreviewWidget):
        if hasattr(widget, "setOrientation"):
            widget.setOrientation(orient)


def paint_rect_mm(device: QPagedPaintDevice, painter: QPainter | None = None) -> QRectF:
    """Actual paint buffer in millimetres, origin at (0, 0).

    QPainter on a QPrinter uses the top-left of the printable area as (0, 0).
    Layout ``paintRect(Millimeter)`` is full-page coordinates (margins as x/y);
    using that origin double-offsets and can overflow the real buffer. Prefer the
    active painter viewport (or ``pageRect(DevicePixel)``) converted via DPI.
    """
    dpi_x = float(device.logicalDpiX() or 72)
    dpi_y = float(device.logicalDpiY() or 72)
    if painter is not None and painter.isActive():
        vp = painter.viewport()
        if vp.width() > 1 and vp.height() > 1:
            return QRectF(0.0, 0.0, vp.width() * 25.4 / dpi_x, vp.height() * 25.4 / dpi_y)
    if hasattr(device, "pageRect"):
        try:
            from PySide6.QtPrintSupport import QPrinter

            rect = device.pageRect(QPrinter.Unit.DevicePixel)
            if rect.width() > 1 and rect.height() > 1:
                return QRectF(
                    0.0,
                    0.0,
                    float(rect.width()) * 25.4 / dpi_x,
                    float(rect.height()) * 25.4 / dpi_y,
                )
        except Exception:
            pass
    unit = QPageLayout.Unit.Millimeter
    layout = device.pageLayout()
    rect = layout.paintRect(unit)
    if rect.width() > 0.5 and rect.height() > 0.5:
        return QRectF(0.0, 0.0, float(rect.width()), float(rect.height()))
    full = layout.fullRect(unit)
    return QRectF(0.0, 0.0, float(full.width()), float(full.height()))


def fit_sheet_to_rect(
    painter: QPainter,
    sheet_w: float,
    sheet_h: float,
    page: QRectF,
) -> float:
    """Scale and center a sheet (mm) into ``page`` (mm). Returns the scale factor."""
    if sheet_w <= 0 or sheet_h <= 0 or page.width() <= 0 or page.height() <= 0:
        return 1.0
    scale = min(page.width() / sheet_w, page.height() / sheet_h)
    painter.translate(
        page.x() + (page.width() - sheet_w * scale) / 2.0,
        page.y() + (page.height() - sheet_h * scale) / 2.0,
    )
    painter.scale(scale, scale)
    return scale


def resolve_print_sheets(
    sheets: list[Sheet],
    device: QPagedPaintDevice,
    *,
    current_index: int = 0,
) -> list[Sheet]:
    """Honor QPrinter AllPages / PageRange / CurrentPage / Selection when present."""
    if not sheets or not hasattr(device, "printRange"):
        return list(sheets)
    try:
        from PySide6.QtPrintSupport import QPrinter
    except ImportError:
        return list(sheets)
    rng = device.printRange()
    n = len(sheets)
    idx = max(0, min(int(current_index), n - 1))
    if rng == QPrinter.PrintRange.CurrentPage:
        return [sheets[idx]]
    if rng == QPrinter.PrintRange.Selection:
        # File → Print has no sheet-selection model; treat as the active sheet.
        return [sheets[idx]]
    if rng == QPrinter.PrintRange.PageRange:
        start = int(device.fromPage() or 1)
        end = int(device.toPage() or n)
        start = max(1, start)
        end = min(n, max(start, end))
        return sheets[start - 1 : end]
    return list(sheets)


def configure_sheet_printer(printer, page_count: int, *, current_page: int = 1) -> None:
    """Enable page-range / current-page controls on a QPrinter for File → Print.

    ``current_page`` is documented for callers; paint uses ``current_index`` when
    the dialog's print range is CurrentPage.
    """
    n = max(1, int(page_count))
    if current_page < 1 or current_page > n:
        current_page = max(1, min(current_page, n))
    if hasattr(printer, "setFromTo"):
        printer.setFromTo(1, n)
    if hasattr(printer, "setPrintRange"):
        from PySide6.QtPrintSupport import QPrinter

        # Default preview shows every sheet; the print dialog can narrow it.
        printer.setPrintRange(QPrinter.PrintRange.AllPages)


def print_dialog_options():
    """QPrintDialog options that unlock page range and current page."""
    from PySide6.QtPrintSupport import QPrintDialog

    Opt = QPrintDialog.PrintDialogOption
    return (
        Opt.PrintToFile
        | Opt.PrintPageRange
        | Opt.PrintCurrentPage
        | Opt.PrintShowPageSize
        | Opt.PrintCollateCopies
    )


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
    fit_to_page: bool = False,
    current_index: int = 0,
) -> None:
    """Print sheets as pages.

    When ``fit_to_page`` is False (Export PDF), each page is sized to the sheet.
    When True (File → Print), the printer's paper/margins stay put and each sheet
    is scaled to fit the paint rect, preserving aspect ratio. QPrinter print
    ranges are honored when the device supports them.
    """
    pages = resolve_print_sheets(
        list(sheets) if sheets is not None else list(project.sheets),
        device,
        current_index=current_index,
    )
    if not pages:
        raise ValueError("project has no sheets to print")
    w0, h0 = pages[0].page.dimensions()
    if fit_to_page:
        apply_sheet_orientation(device, w0, h0)
    else:
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
                fit_to_page=fit_to_page,
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
    fit_to_page: bool,
) -> None:
    total = sheet_count_override if sheet_count_override is not None else len(project.sheets)
    for i, sheet in enumerate(pages):
        width, height = sheet.page.dimensions()
        if i > 0:
            # fit_to_page keeps the job orientation fixed (set from pages[0]
            # before the painter). Flipping mid-job for a later portrait sheet
            # leaves QPrintPreviewDialog in portrait and re-frames page 1 clipped.
            if not fit_to_page:
                configure_full_page(device, width, height)
            device.newPage()
        dpi_x = float(device.logicalDpiX() or 72)
        dpi_y = float(device.logicalDpiY() or 72)
        painter.resetTransform()
        # Device units are dots; sheet coordinates are millimetres.
        painter.scale(dpi_x / 25.4, dpi_y / 25.4)
        if fit_to_page:
            # Use the real paint buffer (viewport), not layout paintRect which
            # still carries margin x/y and may disagree with a locked preview page.
            page = paint_rect_mm(device, painter)
            fit_sheet_to_rect(painter, width, height, page)
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
