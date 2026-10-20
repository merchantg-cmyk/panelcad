"""Size Enclosure dialog and PNG artifacts for DOL and a larger panel."""

from pathlib import Path

import pytest
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from panelcad import Session
from panelcad.examples.dol import build_dol
from panelcad.gui.enclosure_dialog import SizeEnclosureDialog
from panelcad.gui.main_window import MainWindow
from tests.artifacts import artifact_dir
from tests.support import confirm_layout_occupants

_ART: Path | None = None


@pytest.fixture(autouse=True)
def _confirm_before_enclosure_size(monkeypatch):
    from panelcad.api.session import Session
    from panelcad.core import enclosure as enc_mod

    orig_sess = Session.recommend_enclosure
    orig_core = enc_mod.recommend_enclosure

    def _sess(self, *args, **kwargs):
        confirm_layout_occupants(self)
        return orig_sess(self, *args, **kwargs)

    def _core(project, catalog, **kwargs):
        confirm_layout_occupants(project=project, catalog=catalog)
        return orig_core(project, catalog, **kwargs)

    monkeypatch.setattr(Session, "recommend_enclosure", _sess)
    monkeypatch.setattr(enc_mod, "recommend_enclosure", _core)


@pytest.fixture(autouse=True)
def _artifact_root(tmp_path: Path) -> None:
    global _ART
    _ART = artifact_dir(tmp_path)
    yield
    _ART = None


@pytest.fixture(autouse=True)
def _confirm_before_enclosure_size(monkeypatch):
    from panelcad.api.session import Session
    from panelcad.core import enclosure as enc_mod

    orig_sess = Session.recommend_enclosure
    orig_core = enc_mod.recommend_enclosure

    def _sess(self, *args, **kwargs):
        confirm_layout_occupants(self)
        return orig_sess(self, *args, **kwargs)

    def _core(project, catalog, **kwargs):
        confirm_layout_occupants(project=project, catalog=catalog)
        return orig_core(project, catalog, **kwargs)

    monkeypatch.setattr(Session, "recommend_enclosure", _sess)
    monkeypatch.setattr(enc_mod, "recommend_enclosure", _core)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _save(pix, name: str) -> Path:
    assert _ART is not None
    dest = _ART / name
    assert pix.save(str(dest), "PNG")
    assert dest.is_file() and dest.stat().st_size > 1000
    return dest


def _big_panel() -> Session:
    s = Session.create("Several DOL + VFD")
    s.set_title_block(standard="NEMA/ANSI", company="George's Shop", drawing_number="E-SIZE")
    s.insert_circuit(kind="dol", control="local", kw=1.5)
    s.insert_circuit(kind="dol", control="local", kw=4.0)
    s.insert_circuit(kind="vfd", drive="generic_1k5", control="local", kw=5.5)
    s.insert_circuit(kind="vfd", drive="generic_1k5", kw=15.0)
    return s


def test_size_enclosure_dialog_dol_png():
    app = _app()
    app.setStyle("Fusion")
    app.setFont(QFont("DejaVu Sans", 10))
    session = build_dol()
    win = MainWindow(session)
    win.resize(1280, 800)
    win.show()
    app.processEvents()
    dlg = SizeEnclosureDialog(session, win)
    dlg.place.setChecked(True)
    dlg.resize(860, 640)
    dlg.show()
    app.processEvents()
    rec = dlg.recommendation
    assert rec["enclosure"]
    assert "DOL" in session.project.name or rec["parts"]
    assert abs(rec["arrangement"]["width_mm"] - rec["enclosure"]["width_mm"]) < 1.0
    assert abs(rec["arrangement"]["height_mm"] - rec["enclosure"]["height_mm"]) < 1.0
    assert dlg.reasons.toPlainText()
    assert "Heat" in dlg.heat.text()
    pix = dlg.grab()
    assert not pix.isNull()
    _save(pix, "enclosure-dialog-dol.png")
    dlg.apply_recommendation()
    assert dlg.recommendation["applied"] is True
    dlg.close()
    win.session.set_active_sheet("enclosure")
    win.canvas.reload()
    win.canvas.fit_page()
    app.processEvents()
    _save(win.canvas.grab(), "enclosure-layout-dol.png")
    win.close()


def test_size_enclosure_dialog_multi_motor_png():
    app = _app()
    app.setStyle("Fusion")
    app.setFont(QFont("DejaVu Sans", 10))
    session = _big_panel()
    rec = session.recommend_enclosure()
    assert rec["fit"] is True
    assert len(rec["parts"]) > 8
    win = MainWindow(session)
    win.resize(1280, 800)
    win.show()
    app.processEvents()
    dlg = SizeEnclosureDialog(session, win)
    dlg.place.setChecked(True)
    dlg.resize(860, 640)
    dlg.show()
    app.processEvents()
    dlg.recalculate()
    app.processEvents()
    assert dlg.recommendation["enclosure"]["height_in"] >= 20
    assert dlg.recommendation["enclosure"]["height_in"] * dlg.recommendation["enclosure"]["width_in"] < 48 * 36 - 1
    _save(dlg.grab(), "enclosure-dialog-multi.png")
    dlg.apply_recommendation()
    assert dlg.recommendation["applied"] is True
    dlg.close()
    panel = next(sh for sh in session.project.sheets if sh.kind == "panel_layout")
    win.session.set_active_sheet(panel.id)
    win.canvas.reload()
    win.canvas.fit_page()
    app.processEvents()
    _save(win.canvas.grab(), "enclosure-layout-multi.png")
    win.close()


def test_tools_menu_opens_size_enclosure():
    app = _app()
    win = MainWindow(build_dol())
    names = [act.text().replace("&", "") for act in win.menuBar().actions()]
    assert "Tools" in names
    tools = next(act for act in win.menuBar().actions() if act.text().replace("&", "") == "Tools")
    sub = [a.text().replace("&", "") for a in tools.menu().actions()]
    assert any("Panel" in t or "Size Enclosure" in t for t in sub)
    win.close()
