"""File → Print: fit-to-page and QPrinter page-range / current-page."""

from __future__ import annotations

from pathlib import Path

import pytest

from panelcad.examples.dol import build_dol


def _qt_app():
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        QApplication([])
    return QApplication.instance()


def _letter_pdf_printer(path: Path, *, landscape: bool = True) -> "QPrinter":
    from PySide6.QtGui import QPageLayout, QPageSize
    from PySide6.QtPrintSupport import QPrinter

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(path))
    printer.setPageSize(QPageSize(QPageSize.PageSizeId.Letter))
    printer.setPageOrientation(
        QPageLayout.Orientation.Landscape if landscape else QPageLayout.Orientation.Portrait
    )
    return printer


def _content_bbox_pts(pdf: Path) -> tuple[float, float, float, float, float, float]:
    """Return (minx, miny, maxx, maxy, page_w, page_h) in PDF points via raster ink."""
    import pymupdf

    doc = pymupdf.open(str(pdf))
    assert doc.page_count >= 1
    page = doc[0]
    scale = 2.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    w, h = pix.width, pix.height
    samples = pix.samples
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        row = samples[y * w * 3 : (y + 1) * w * 3]
        for x in range(w):
            r, g, b = row[x * 3], row[x * 3 + 1], row[x * 3 + 2]
            if r < 250 or g < 250 or b < 250:
                if x < minx:
                    minx = x
                if y < miny:
                    miny = y
                if x > maxx:
                    maxx = x
                if y > maxy:
                    maxy = y
    assert maxx >= 0 and maxy >= 0, "expected ink on the page"
    # Convert pixel bounds back to PDF points.
    return (
        minx / scale,
        miny / scale,
        (maxx + 1) / scale,
        (maxy + 1) / scale,
        float(page.rect.width),
        float(page.rect.height),
    )


def test_fit_to_page_keeps_content_inside_letter_page_rect(tmp_path: Path) -> None:
    """A3 landscape sheets must scale into Letter without spilling past the page."""
    _qt_app()
    from panelcad.render.page import paint_project_on_device, paint_rect_mm

    session = build_dol()
    sheet_w, sheet_h = session.project.sheets[0].page.dimensions()
    assert sheet_w > 280 and sheet_h > 200  # A3 landscape, larger than Letter

    dest = tmp_path / "fit_letter.pdf"
    printer = _letter_pdf_printer(dest, landscape=True)
    paint_project_on_device(
        printer,
        session.project,
        session.catalog,
        sheets=session.project.sheets[:1],
        fit_to_page=True,
    )
    assert dest.is_file() and dest.stat().st_size > 1000

    minx, miny, maxx, maxy, page_w, page_h = _content_bbox_pts(dest)
    # Letter landscape is 792×612 pt; content must sit inside that page.
    assert page_w == pytest.approx(792.0, abs=1.0)
    assert page_h == pytest.approx(612.0, abs=1.0)
    assert minx >= -0.5
    assert miny >= -0.5
    assert maxx <= page_w + 0.5
    assert maxy <= page_h + 0.5

    # And the printable rect the painter used is smaller than the sheet.
    printer2 = _letter_pdf_printer(tmp_path / "probe.pdf", landscape=True)
    rect = paint_rect_mm(printer2)
    assert rect.width() < sheet_w
    assert rect.height() < sheet_h


def test_page_range_2_2_prints_exactly_one_control_sheet(tmp_path: Path) -> None:
    _qt_app()
    from PySide6.QtPrintSupport import QPrinter

    from panelcad.render.page import paint_project_on_device

    session = build_dol()
    assert [s.id for s in session.project.sheets[:3]] == ["power", "control", "io"]

    dest = tmp_path / "range_2_2.pdf"
    printer = _letter_pdf_printer(dest, landscape=True)
    printer.setPrintRange(QPrinter.PrintRange.PageRange)
    printer.setFromTo(2, 2)
    paint_project_on_device(
        printer,
        session.project,
        session.catalog,
        fit_to_page=True,
        current_index=0,
    )

    import pymupdf

    doc = pymupdf.open(str(dest))
    assert doc.page_count == 1
    text = doc[0].get_text()
    assert "Control" in text
    assert "24 VDC control" in text


def test_current_page_prints_only_active_sheet(tmp_path: Path) -> None:
    _qt_app()
    from PySide6.QtPrintSupport import QPrinter

    from panelcad.render.page import paint_project_on_device

    session = build_dol()
    dest = tmp_path / "current.pdf"
    printer = _letter_pdf_printer(dest, landscape=True)
    printer.setPrintRange(QPrinter.PrintRange.CurrentPage)
    # Active sheet index 2 → io
    paint_project_on_device(
        printer,
        session.project,
        session.catalog,
        fit_to_page=True,
        current_index=2,
    )

    import pymupdf

    doc = pymupdf.open(str(dest))
    assert doc.page_count == 1
    text = doc[0].get_text()
    assert "PLC I/O and VFD" in text


def test_print_dialog_enables_page_range_and_current_page() -> None:
    _qt_app()
    from PySide6.QtPrintSupport import QPrintDialog, QPrinter
    from PySide6.QtWidgets import QWidget

    from panelcad.render.page import configure_sheet_printer, print_dialog_options

    parent = QWidget()
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    configure_sheet_printer(printer, 6, current_page=2)
    dialog = QPrintDialog(printer, parent)
    dialog.setOptions(print_dialog_options())
    dialog.setMinMax(1, 6)
    opts = dialog.options()
    Opt = QPrintDialog.PrintDialogOption
    assert opts & Opt.PrintPageRange
    assert opts & Opt.PrintCurrentPage
    assert dialog.minPage() == 1
    assert dialog.maxPage() == 6
    assert printer.fromPage() == 1
    assert printer.toPage() == 6


def test_export_pdf_still_uses_native_sheet_size(tmp_path: Path) -> None:
    """fit_to_page must not change Export PDF (native A3 pages)."""
    _qt_app()
    session = build_dol()
    dest = tmp_path / "native.pdf"
    session.export_pdf(dest)
    import pymupdf

    doc = pymupdf.open(str(dest))
    assert doc.page_count == len(session.project.sheets)
    # A3 landscape ≈ 1191 × 842 pt at 72 dpi
    assert doc[0].rect.width == pytest.approx(1191.0, abs=2.0)
    assert doc[0].rect.height == pytest.approx(842.0, abs=2.0)


def test_print_dialog_initial_size_covers_content_and_fits_screen(monkeypatch) -> None:
    """fit_print_dialog grows to sizeHint and stays on-screen (no native Print UI).

    A shop laptop had to scroll inside a too-small print box. Showing a
    real QPrintDialog on Windows opens the native Print dialog even under
    QT_QPA_PLATFORM=offscreen and blocks the suite — use a plain QDialog
    stand-in for size/show coverage, and only construct QPrintDialog to prove
    the theme will not scroll-wrap it.
    """
    _qt_app()
    from PySide6.QtCore import QRect, QSize, Qt
    from PySide6.QtPrintSupport import QPrintDialog, QPrinter
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QDialogButtonBox,
        QLabel,
        QScrollArea,
        QVBoxLayout,
        QWidget,
    )

    from panelcad.gui import screen_fit
    from panelcad.gui.screen_fit import fit_print_dialog
    from panelcad.gui.theme import (
        _ThemeHooks,
        _ensure_dialog_scroll,
        apply_stylesheet,
        clamp_widget_to_screen,
    )
    from panelcad.render.page import configure_sheet_printer, print_dialog_options

    class _PrintStandIn(QDialog):
        """Tall options form that mimics an expanded QPrintDialog sizeHint."""

        def sizeHint(self) -> QSize:  # noqa: N802
            return QSize(560, 480)

    apply_stylesheet()
    app = QApplication.instance()
    assert app is not None
    app.installEventFilter(_ThemeHooks(app))

    for width, height in ((1920, 1080), (1366, 768)):
        monkeypatch.setattr(
            screen_fit,
            "available_work_area",
            lambda _widget, w=width, h=height: QRect(0, 0, w, h),
        )
        parent = QWidget()
        dialog = _PrintStandIn(parent)
        dialog.setWindowTitle("Print stand-in")
        root = QVBoxLayout(dialog)
        body = QLabel("Print range / Current page / Options\n" * 12)
        body.setAlignment(Qt.AlignmentFlag.AlignTop)
        root.addWidget(body)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        root.addWidget(buttons)
        # Start undersized the way Qt often opens QPrintDialog.
        dialog.resize(320, 200)
        fit_print_dialog(dialog)
        dialog.show()
        app.processEvents()
        clamp_widget_to_screen(dialog)
        app.processEvents()

        hint = dialog.sizeHint()
        avail_w, avail_h = width, height
        assert dialog.property("_pc_own_fit") is True
        assert dialog.property("_pc_scrolled") in (None, False)
        assert not any(isinstance(child, QScrollArea) for child in dialog.findChildren(QScrollArea))
        assert dialog.width() >= min(hint.width(), avail_w - 48)
        assert dialog.height() >= min(hint.height(), avail_h - 64)
        assert dialog.width() <= avail_w
        assert dialog.height() <= avail_h
        assert dialog.maximumWidth() >= dialog.width()
        assert dialog.maximumHeight() >= dialog.height()
        assert dialog.minimumWidth() <= dialog.width()
        assert dialog.minimumHeight() <= dialog.height()
        dialog.close()
        parent.close()

    # QPrintDialog must opt out of theme scroll-wrap without ever showing
    # (show() opens the native Windows Print dialog and blocks CI).
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    configure_sheet_printer(printer, 6, current_page=1)
    native = QPrintDialog(printer)
    native.setOptions(print_dialog_options())
    native.setMinMax(1, 6)
    fit_print_dialog(native)
    assert native.property("_pc_own_fit") is True
    _ensure_dialog_scroll(native)
    assert native.property("_pc_scrolled") in (None, False)
    native.deleteLater()


def test_preview_paint_path_fits_landscape_sheet_on_portrait_default_printer(
    tmp_path: Path,
) -> None:
    """Same path as File → Print preview: portrait-default QPrinter, fit_to_page.

    The shop bug was a landscape A3 sheet painted 1:1 onto a portrait Letter
    page in the preview. The full sheet border must land inside the page rect,
    and orientation must flip to landscape to match the sheet.
    """
    _qt_app()
    from PySide6.QtGui import QPageLayout, QPageSize
    from PySide6.QtPrintSupport import QPrinter

    from panelcad.render.page import (
        apply_sheet_orientation,
        paint_project_on_device,
    )

    session = build_dol()
    sheet = session.project.sheets[0]
    sw, sh = sheet.page.dimensions()
    assert sw > sh  # landscape drawing

    dest = tmp_path / "preview_path.pdf"
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(dest))
    printer.setPageSize(QPageSize(QPageSize.PageSizeId.Letter))
    # Portrait default — do not set landscape here; paint path must pick it.
    printer.setPageOrientation(QPageLayout.Orientation.Portrait)
    assert printer.pageLayout().orientation() == QPageLayout.Orientation.Portrait

    # Mirror MainWindow.print_sheet: orient before paint, then fit_to_page paint.
    apply_sheet_orientation(printer, sw, sh)
    assert printer.pageLayout().orientation() == QPageLayout.Orientation.Landscape

    paint_project_on_device(
        printer,
        session.project,
        session.catalog,
        sheets=[sheet],
        fit_to_page=True,
        current_index=0,
    )

    import pymupdf

    doc = pymupdf.open(str(dest))
    assert doc.page_count == 1
    page = doc[0]
    # Landscape Letter ≈ 792 × 612 pt
    assert page.rect.width > page.rect.height
    assert page.rect.width == pytest.approx(792.0, abs=2.0)
    assert page.rect.height == pytest.approx(612.0, abs=2.0)

    minx, miny, maxx, maxy, page_w, page_h = _content_bbox_pts(dest)
    # Full sheet bounding box inside the page (small tolerance for rasterization).
    assert minx >= -1.0
    assert miny >= -1.0
    assert maxx <= page_w + 1.0
    assert maxy <= page_h + 1.0
    # Content must use most of the width (old bug: left strip only, right clipped).
    # Height can letterbox: A3 420×297 into Letter paint is width-limited.
    assert (maxx - minx) > page_w * 0.90
    assert (maxy - miny) > page_h * 0.70
    assert minx < page_w * 0.05
    assert maxx > page_w * 0.95
