"""Part picker GUI, BOM with real SKUs, and shop artifacts."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from panelcad.examples.dol import build_dol
from panelcad.gui.main_window import MainWindow
from panelcad.gui.part_picker import PartPickerDialog
from panelcad.library.parts import default_parts_catalog, write_parts_csv


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_part_picker_search_and_category_filter():
    app = _app()
    session = build_dol()
    dlg = PartPickerDialog(session, category="protection.breaker")
    dlg.show()
    app.processEvents()
    assert dlg.table.rowCount() >= 1
    assert all(
        row["category"].startswith("protection.breaker") for row in dlg._hits
    )
    dlg.search.setText("1489-M3C160")
    app.processEvents()
    assert dlg.table.rowCount() == 1
    dlg.table.selectRow(0)
    assert dlg.table.item(0, 1).text() == "1489-M3C160"
    assert dlg.table.item(0, 2).text() == "full"
    assert dlg.selected_part().catalog == "1489-M3C160"
    dlg.close()


def test_part_picker_amps_min_max_filter():
    app = _app()
    session = build_dol()
    dlg = PartPickerDialog(session, category="protection.mpcb")
    dlg.show()
    app.processEvents()
    dlg.amps_min.setText("2.2")
    dlg.amps_max.setText("3.2")
    app.processEvents()
    catalogs = [row["catalog"] for row in dlg._hits]
    assert "3RV2011-1DA25" in catalogs
    assert "3RV2011-1BA10" not in catalogs
    dlg.close()


def test_render_part_picker_and_dol_bom_pngs(tmp_path: Path):
    app = _app()
    app.setStyle("Fusion")
    app.setFont(QFont("DejaVu Sans", 10))
    session = build_dol()
    session.assign_part("Q1", "1489-M3C160")
    session.assign_part("K1", "100-C09D10", manufacturer="Allen-Bradley")
    session.assign_part("F1", "193-T1AC10")

    from tests.artifacts import artifact_dir

    dest_dir = artifact_dir(tmp_path)
    write_parts_csv(dest_dir / "manufacturer-parts.csv", default_parts_catalog().parts.values())

    picker = PartPickerDialog(session, query="100-C09")
    picker.resize(980, 560)
    picker.show()
    app.processEvents()
    picker.table.selectRow(0)
    app.processEvents()
    pix = picker.grab()
    assert not pix.isNull()
    picker_path = dest_dir / "part-picker.png"
    assert pix.save(str(picker_path), "PNG")
    assert picker_path.is_file() and picker_path.stat().st_size > 1000
    picker.close()

    win = MainWindow(session)
    win.resize(1480, 920)
    win.show()
    app.processEvents()
    win.bom.refresh()
    win.bom_wrap.show()
    win.bom_wrap.raise_()
    win.resizeDocks([win.bom_wrap], [260], Qt.Orientation.Vertical)
    app.processEvents()
    bom_pix = win.grab()
    assert not bom_pix.isNull()
    bom_path = dest_dir / "dol-bom-catalog-parts.png"
    assert bom_pix.save(str(bom_path), "PNG")
    assert bom_path.is_file() and bom_path.stat().st_size > 1000
    texts = []
    for row in range(win.bom.table.rowCount()):
        texts.append(
            " ".join(
                win.bom.table.item(row, col).text()
                if win.bom.table.item(row, col) is not None
                else ""
                for col in range(win.bom.table.columnCount())
            )
        )
    joined = "\n".join(texts)
    assert "1489-M3C160" in joined
    assert "100-C09D10" in joined
    assert "Allen-Bradley" in joined
    win.close()
