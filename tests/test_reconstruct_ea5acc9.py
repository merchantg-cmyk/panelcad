"""Regression tests for the ea5acc9 Windows review (N10–N13, item 10)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMimeData, QPointF, Qt
from PySide6.QtGui import QColor, QDropEvent, QImage
from PySide6.QtWidgets import QApplication, QMessageBox

from panelcad import Session
from panelcad.core.checks import run_checks
from panelcad.diag.crash import crashes_dir, install_hooks
from panelcad.gui.canvas import LIBRARY_MIME
from panelcad.gui.main_window import MainWindow
from panelcad.gui.reconstruct_wizard import ReconstructWizard
from panelcad.reconstruct.fixtures import write_backpanel_photo
from panelcad.reconstruct.geometry import mm_per_px, photo_to_mm
from panelcad.reconstruct.vision import FakeVision


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _write_face(path: Path, width_px: int = 900, height_px: int = 700) -> None:
    image = QImage(width_px, height_px, QImage.Format.Format_RGB32)
    image.fill(QColor("#556677"))
    image.save(str(path), "PNG")


def test_n10_two_length_scale_does_not_invent_a_face(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    _app()
    shot = tmp_path / "face.png"
    _write_face(shot)
    job = tmp_path / "scale.pcad.yaml"
    session = Session.create("Scale")
    session.save(job)
    session = Session.open(job)
    session.reconstruct_add_photos([shot], role="backpanel")
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    corners = [(0, 0), (900, 0), (900, 700), (0, 700)]
    dialog = ReconstructWizard(session)
    dialog.scale_photo.setCurrentIndex(0)
    for index in range(dialog.scale_kind.count()):
        if dialog.scale_kind.itemData(index) == "measured":
            dialog.scale_kind.setCurrentIndex(index)
            break
    dialog.corners.setText("0,0 900,0 900,700 0,700")
    dialog.p1.setText("100,100")
    dialog.p2.setText("500,100")
    dialog.known.setValue(200)
    dialog.p3.setText("100,100")
    dialog.p4.setText("100,300")
    dialog.known_height.setValue(100)
    dialog._apply_scale()
    photo = session.project.reconstruct.photo(photo_id)
    assert photo.face_width_mm in (None, 0)
    assert photo.face_height_mm in (None, 0)
    assert photo.scale is not None
    assert photo.scale_b is not None
    mapped = photo_to_mm(photo, 900, 700)
    assert mapped is not None
    assert abs(mapped[0] - 450) <= 8
    assert abs(mapped[1] - 350) <= 8

    session2 = Session.create("Duct")
    session2.save(tmp_path / "duct.pcad.yaml")
    session2 = Session.open(tmp_path / "duct.pcad.yaml")
    session2.reconstruct_add_photos([shot], role="backpanel")
    pid = session2.reconstruct_list_photos()["photos"][0]["id"]
    dialog2 = ReconstructWizard(session2)
    for index in range(dialog2.scale_kind.count()):
        if dialog2.scale_kind.itemData(index) == "din_rail":
            dialog2.scale_kind.setCurrentIndex(index)
            break
    dialog2.corners.setText("0,0 900,0 900,700 0,700")
    dialog2.p1.setText("100,100")
    dialog2.p2.setText("170,100")
    dialog2.known.setValue(35)
    dialog2.p3.setText("100,100")
    dialog2.p4.setText("220,100")
    dialog2.known_height.setValue(60)
    dialog2._apply_scale()
    photo2 = session2.project.reconstruct.photo(pid)
    assert photo2.face_width_mm in (None, 0)
    assert photo2.face_height_mm in (None, 0)
    assert photo2.scale is not None and photo2.scale_b is not None

    session.reconstruct_set_corners(photo_id, corners)
    payload = session.reconstruct_set_scale(
        photo_id,
        "measured",
        (100, 100),
        (500, 100),
        known_mm=200,
        known_height_mm=100,
    )
    photo = session.project.reconstruct.photo(photo_id)
    assert photo.face_width_mm in (None, 0)
    assert "ignored" in str(payload.get("message") or "").lower() or photo.face_width_mm in (None, 0)
    assert payload.get("face") is not True


def test_n12_wizard_cancel_restores_converted_component(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    _app()
    shot = tmp_path / "back.png"
    meta = write_backpanel_photo(shot)
    job = tmp_path / "wiz.pcad.yaml"
    session = Session.create("Cancel")
    session.save(job)
    session = Session.open(job)
    session.reconstruct_add_photos([shot], role="backpanel")
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    session.reconstruct_set_corners(photo_id, meta["corners"])
    session.reconstruct_set_scale(
        photo_id,
        "face",
        meta["corners"][0],
        meta["corners"][1],
        known_mm=meta.get("enclosure_width_mm") or 400,
        known_height_mm=meta.get("enclosure_height_mm") or 500,
    )
    session.reconstruct_identify(
        vision=FakeVision(
            {
                "p1": {
                    "devices": [
                        {
                            "tag": "U7",
                            "label_text": "??",
                            "nx": 0.2,
                            "ny": 0.2,
                            "nw": 0.1,
                            "nh": 0.1,
                            "confidence": 0.4,
                        }
                    ]
                }
            }
        )
    )
    session.reconstruct_place_needs_info()
    before = session.project.model_dump(mode="json")
    dialog = ReconstructWizard(session)
    item = next(row for row in session.project.reconstruct.items if row.role == "part")
    session.reconstruct_confirm_item(
        item.id,
        envelope_w_mm=50,
        envelope_h_mm=80,
        envelope_d_mm=90,
        mount="backplate",
        tag="U7",
    )
    dialog.reject()
    assert session.project.model_dump(mode="json") == before


def test_n13_refused_gui_drop_does_not_open_crash_dialog(tmp_path: Path, monkeypatch):
    _app()
    opened = {"crash": False}

    def _boom(*_a, **_k):
        opened["crash"] = True
        raise AssertionError("CrashDialog must not open")

    monkeypatch.setattr("panelcad.gui.bug_report_dialog.CrashDialog", _boom)
    install_hooks(gui=True)
    before = {path.name for path in crashes_dir().glob("*.json")}
    session = Session.create("DropGate")
    session.add_sheet("panel", kind="panel_layout")
    win = MainWindow(session)
    win.show()
    win._switch_sheet("panel")
    canvas = win.canvas
    mime = QMimeData()
    mime.setData(LIBRARY_MIME, b"iec.contactors.ac3_9a")
    pos = QPointF(canvas.mapFromScene(QPointF(80, 80)))
    event = QDropEvent(
        pos,
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    canvas.dropEvent(event)
    _app().processEvents()
    assert not session.project.sheet("panel").components
    assert opened["crash"] is False
    after = {path.name for path in crashes_dir().glob("*.json")}
    assert after == before
    win.close()


def test_item_10_missing_photo_is_a_problems_row(tmp_path: Path):
    _app()
    shot = tmp_path / "orig" / "back.png"
    shot.parent.mkdir()
    write_backpanel_photo(shot)
    job = tmp_path / "orig" / "job.pcad.yaml"
    session = Session.create("Missing")
    session.save(job)
    session = Session.open(job)
    session.reconstruct_add_photos([shot], role="backpanel")
    session.save(job)
    dest = tmp_path / "elsewhere" / "copy.pcad.yaml"
    dest.parent.mkdir()
    dest.write_text(job.read_text(encoding="utf-8"), encoding="utf-8")
    reopened = Session.open(dest)
    store = reopened.project.reconstruct
    assert store is not None
    photo = store.photos[0]
    # Force a missing file even if recover copied from source_path.
    photo.path = "photos/gone.png"
    photo.source_path = ""
    findings = [row for row in run_checks(reopened.project, reopened.catalog) if row.check == "reconstruct_photo_missing"]
    assert findings
    assert photo.id in findings[0].message
    assert "re-attach" in findings[0].message.lower()
    payload = reopened.reconstruct_photo_region(photo_id=photo.id)
    assert payload.get("missing") or not Path(str(payload.get("path") or "")).is_file()
    from panelcad.gui.photo_viewer import PhotoViewerDialog

    dialog = PhotoViewerDialog(reopened, payload)
    dialog.show_payload(payload)
    dialog.close()
    row = findings[0].to_row()
    win = MainWindow(reopened)
    win.canvas.jump_to_finding(row)
    win.close()
