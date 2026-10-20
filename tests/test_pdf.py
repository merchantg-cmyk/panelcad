from pathlib import Path

from panelcad.examples.dol import build_dol


def test_simple_circuit_print_is_three_pages(tmp_path: Path):
    from PySide6.QtWidgets import QApplication

    from panelcad.examples.simple_circuit import write_print

    if QApplication.instance() is None:
        QApplication([])
    dest = tmp_path / "simple.pdf"
    write_print(dest)
    assert dest.is_file()
    from PySide6.QtPdf import QPdfDocument

    doc = QPdfDocument()
    assert doc.load(str(dest)) == QPdfDocument.Error.None_
    assert doc.pageCount() == 3


def test_pdf_export(tmp_path: Path):
    s = build_dol()
    dest = tmp_path / "dol.pdf"
    s.export_pdf(dest)
    assert dest.is_file()
    assert dest.stat().st_size > 1000


def test_pdf_is_multipage(tmp_path: Path):
    from PySide6.QtWidgets import QApplication

    s = build_dol()
    assert len(s.project.sheets) == 6
    dest = tmp_path / "set.pdf"
    s.export_pdf(dest)
    if QApplication.instance() is None:
        QApplication([])
    try:
        from PySide6.QtPdf import QPdfDocument
    except ImportError:
        raw = dest.read_bytes()
        # Fallback: one /Type /Page (or /Page\n) per sheet, plus the catalog.
        assert raw.count(b"/Type /Page") >= 6
        return
    doc = QPdfDocument()
    assert doc.load(str(dest)) == QPdfDocument.Error.None_
    assert doc.pageCount() == 6


def test_title_block_ink_at_bottom_of_page():
    """Regression: print/PDF used to clip the bottom title-block boxes."""
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from panelcad.render.page import paint_sheet
    from panelcad.render.titleblock import TITLE_H, MARGIN

    if QApplication.instance() is None:
        QApplication([])
    s = build_dol()
    sheet = s.sheet()
    width, height = sheet.page.dimensions()
    scale = 2.0
    img = QImage(int(width * scale), int(height * scale), QImage.Format.Format_RGB32)
    img.fill(QColor("#ffffff"))
    painter = QPainter(img)
    painter.scale(scale, scale)
    paint_sheet(painter, s.project, sheet, s.catalog, sheet_index=0, sheet_count=1)
    painter.end()
    # Horizontal rule at the top of the title block must be ink, not clipped away.
    y = height - MARGIN - TITLE_H
    sample = img.pixelColor(int((MARGIN + 20) * scale), int(y * scale))
    assert sample.lightness() < 200, sample.getRgb()
    # And the bottom border of the sheet is also present.
    bottom = img.pixelColor(int((width / 2) * scale), int((height - MARGIN) * scale))
    assert bottom.lightness() < 200, bottom.getRgb()
