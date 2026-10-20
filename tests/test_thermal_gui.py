"""Thermal dialog screenshots and report-page rasters."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from panelcad.core.thermal import citation_markdown
from panelcad.gui.main_window import MainWindow
from panelcad.gui.thermal_dialog import ThermalDialog
from panelcad.render.print_package import extract_pdf_text, render_package_page_png
from tests.artifacts import artifact_dir
from tests.test_thermal import small_dol, vfd_panel

ART = Path("artifacts") / "thermal"


@pytest.fixture(autouse=True)
def _artifact_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.modules[__name__], "ART", artifact_dir(tmp_path, "thermal"))


def _app() -> QApplication:
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont("DejaVu Sans", 10))
    return app


def _shot(widget, name: str) -> Path:
    ART.mkdir(parents=True, exist_ok=True)
    dest = ART / name
    pix = widget.grab()
    assert not pix.isNull()
    assert pix.save(str(dest), "PNG")
    assert dest.stat().st_size > 5000
    return dest


def _report(session, name: str) -> str:
    ART.mkdir(parents=True, exist_ok=True)
    pdf = ART / f"{name}.pdf"
    session.export_thermal_pdf(pdf)
    png = render_package_page_png(pdf, 0, ART / f"{name}-report.png", width_px=1700)
    assert png.stat().st_size > 5000
    return extract_pdf_text(pdf, 0)


def test_vfd_and_dol_thermal_dialog_and_report_pages():
    app = _app()
    (ART / "citations.md").write_text(citation_markdown(), encoding="utf-8")

    vfd = vfd_panel()
    win = MainWindow(vfd)
    win.resize(1280, 800)
    win.show()
    app.processEvents()
    names = []
    tools = next(act for act in win.menuBar().actions() if act.text().replace("&", "") == "Tools")
    panel = next(act for act in tools.menu().actions() if "Panel" in act.text().replace("&", ""))
    labels = [act.text().replace("&", "") for act in panel.menu().actions()]
    assert any("Thermal" in label for label in labels)
    dlg = ThermalDialog(vfd, win)
    dlg.resize(1100, 760)
    dlg.show()
    app.processEvents()
    assert "51.74" in dlg.summary.text()
    assert "Estimated" in dlg.summary.text()
    assert "Type 12" in dlg.summary.text()
    assert "IP54" in dlg.summary.text()
    assert "<b>" in dlg.summary.text()
    assert "30.80" in _table_blob(dlg) or "30.8" in _table_blob(dlg)
    assert "+5.0 K" in _table_blob(dlg)
    assert dlg.report["recommendation"]["kind"] == "filter_fan"
    _shot(dlg, "vfd-dialog.png")
    vfd_text = _report(vfd, "vfd")
    assert "Estimated" in vfd_text
    assert "ESTIMATE" in vfd_text
    assert "30.80" in vfd_text or "30.8" in vfd_text
    assert "Published" in vfd_text or "PUBLISHED" in vfd_text.upper()
    assert "Type 12" in vfd_text
    assert "IP54" in vfd_text
    assert "+5.0 K" in vfd_text
    assert "IEC 60890" in vfd_text or "Rittal" in vfd_text
    dlg.close()
    win.close()

    dol = small_dol()
    win = MainWindow(dol)
    win.resize(1280, 800)
    win.show()
    app.processEvents()
    dlg = ThermalDialog(dol, win)
    dlg.resize(1100, 760)
    dlg.show()
    app.processEvents()
    assert dlg.report["recommendation"]["kind"] == "none"
    assert "15.0 K allowance set by A1 (40 °C user rating)" in dlg.summary.text()
    assert "margin +0.1 K" in dlg.summary.text()
    assert "at the limit" in dlg.summary.text()
    assert "25.0 K" not in dlg.summary.text()
    assert "M1" not in _table_blob(dlg)
    assert "ESTIMATE" in _table_blob(dlg)
    assert "+0.1 K" in _table_blob(dlg)
    _shot(dlg, "dol-dialog.png")
    dol_text = _report(dol, "dol")
    assert "ESTIMATE" in dol_text
    dol_head = dol_text.split("Assumptions")[0]
    assert "15.0 K allowance set by A1" in dol_head
    assert "40 °C user rating" in dol_head
    assert "margin +0.1 K" in dol_head
    assert "at the limit" in dol_head
    assert "25.0 K" not in dol_head
    assert "shop setting" in dol_text
    assert "not a code value" in dol_text
    assert "+0.1 K" in dol_text
    assert "GS21" not in dol_text
    dlg.close()
    win.close()

    sealed = vfd_panel()
    sealed.set_enclosure(nema_type="4X", sheet_id="panel")
    sealed_text = _report(sealed, "vfd-4x")
    head = sealed_text.split("Assumptions")[0]
    assert "Closed-loop" in head
    assert "4X" in head
    assert "air conditioner 20.3 W" in head
    assert "air conditioner 20 W" not in head
    assert "m³/h" not in head

    over = vfd_panel()
    over.set_ambient(max_internal_c=60)
    over_text = _report(over, "vfd-over")
    over_head = over_text.split("Assumptions")[0]
    assert "limits the panel" in over_head
    assert "m³/h" in over_head
    assert "Natural convection is enough" not in over_head
    assert "-6" not in over_head
    _assert_bold_wrap(sealed.compute_heat_load()["recommendation"]["summary"])
    _assert_bold_wrap(over.compute_heat_load()["recommendation"]["summary"])
    _assert_bold_wrap(dol.compute_heat_load()["recommendation"]["summary"])
    for name in ("vfd-report.png", "dol-report.png", "vfd-4x-report.png", "vfd-over-report.png"):
        _assert_inside_margin(name)
    names.append("ok")
    assert names == ["ok"]


def _assert_bold_wrap(summary: str) -> None:
    from panelcad.render.print_package import MARGIN_X, PAPER_SIZES_MM, wrap_lines
    from panelcad.render.qt_painter import text_advance_mm

    width = PAPER_SIZES_MM["letter"][0] - 2 * MARGIN_X
    lines = wrap_lines(summary, width, 2.55, bold=True)
    assert lines
    for line in lines:
        assert text_advance_mm(line, 2.55, bold=True) <= width - 1.4, line


def _assert_inside_margin(name: str) -> None:
    """No text ink in the right margin, between the text box and the frame."""

    from PySide6.QtGui import QImage

    from panelcad.render.print_package import MARGIN_X, MARGIN_Y, PAPER_SIZES_MM

    image = QImage(str(ART / name))
    assert not image.isNull()
    page_w, page_h = PAPER_SIZES_MM["letter"]
    scale_x = image.width() / page_w
    scale_y = image.height() / page_h
    x0 = int((page_w - MARGIN_X + 0.7) * scale_x)
    x1 = int((page_w - MARGIN_X / 2 - 1.2) * scale_x)
    y0 = int((MARGIN_Y / 2 + 1.2) * scale_y)
    y1 = int((page_h - MARGIN_Y / 2 - 1.2) * scale_y)
    hits: list[tuple[int, int]] = []
    for y in range(y0, y1):
        for x in range(x0, max(x0 + 1, x1)):
            color = image.pixelColor(x, y)
            if color.red() < 100 and color.green() < 100 and color.blue() < 100:
                hits.append((x, y))
                if len(hits) > 8:
                    break
        if len(hits) > 8:
            break
    assert not hits, f"{name} draws outside the right margin near {hits[:3]}"


def _table_blob(dlg: ThermalDialog) -> str:
    bits: list[str] = []
    for row in range(dlg.table.rowCount()):
        for col in range(dlg.table.columnCount()):
            item = dlg.table.item(row, col)
            if item is not None:
                bits.append(item.text())
    return "\n".join(bits)
