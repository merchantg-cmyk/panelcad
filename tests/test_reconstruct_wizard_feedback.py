"""Reconstruct from photos closed with no explanation.

Regression for the offscreen repro: no AI, identify returns 0 items, Place
reports nothing, and Next/Done used to accept the dialog with the project
unchanged and no message.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from panelcad import Session
from panelcad.gui.main_window import MainWindow
from panelcad.gui.reconstruct_wizard import ReconstructWizard
from panelcad.io.bom import grouped_rows, instance_rows
from panelcad.library.panel import is_layout_ready
from panelcad.quote.build import build_quote
from panelcad.reconstruct.fixtures import write_backpanel_photo
from panelcad.reconstruct.models import item_is_layout_ready
from panelcad.reconstruct.vision import FakeVision, StubVision


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _photo_session(tmp_path: Path, name: str = "Sample") -> tuple[Session, Path, dict]:
    _app()
    shot = tmp_path / "backpanel.png"
    meta = write_backpanel_photo(shot)
    job = tmp_path / "asbuilt.pcad.yaml"
    session = Session.create(name)
    session.save(job)
    session = Session.open(job)
    session.reconstruct_add_photos([shot], role="backpanel")
    return session, job, meta


def _face_scale(session: Session, photo_id: str, meta: dict) -> None:
    session.reconstruct_set_corners(photo_id, meta["corners"])
    session.reconstruct_set_scale(
        photo_id,
        "face",
        meta["corners"][0],
        meta["corners"][1],
        known_mm=meta.get("enclosure_width_mm") or 400,
        known_height_mm=meta.get("enclosure_height_mm") or 500,
    )


def _recordings(meta: dict) -> dict:
    region = meta["part_region"]
    w = float(meta["width_px"])
    h = float(meta["height_px"])
    return {
        "p1": {
            "devices": [
                {
                    "tag": "K1",
                    "brand": "LS Electric",
                    "part_number": "MC-9b",
                    "label_text": "MC-9b",
                    "nx": region["x"] / w,
                    "ny": region["y"] / h,
                    "nw": region["w"] / w,
                    "nh": region["h"] / h,
                    "confidence": 0.9,
                    "unreadable": False,
                }
            ],
            "strips": [],
            "rails": [],
            "ducts": [],
            "texts": [],
            "unreadable": False,
        }
    }


def _part(session: Session):
    return next(item for item in session.project.reconstruct.items if item.role == "part")


def _row_text(dialog: ReconstructWizard, item_id: str) -> str:
    for index in range(dialog.item_list.count()):
        row = dialog.item_list.item(index)
        if row.data(Qt.ItemDataRole.UserRole) == item_id:
            return row.text()
    raise AssertionError(f"{item_id} is not in the item list")


def _select(dialog: ReconstructWizard, item_id: str) -> None:
    for index in range(dialog.item_list.count()):
        row = dialog.item_list.item(index)
        if row.data(Qt.ItemDataRole.UserRole) == item_id:
            dialog.item_list.setCurrentRow(index)
            return
    raise AssertionError(f"{item_id} is not in the item list")


def _silence_boxes(monkeypatch) -> None:
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))


def test_empty_identify_explains_and_does_not_close(tmp_path: Path, monkeypatch) -> None:
    """repro.py: 1 photo, stub identify → 0 items, Place 0, Next must not accept."""

    _silence_boxes(monkeypatch)
    session, _job, _meta = _photo_session(tmp_path)
    before = [(sheet.id, sheet.kind, len(sheet.components)) for sheet in session.project.sheets]
    dialog = ReconstructWizard(session, vision=StubVision("vision unavailable, using offline reader"))
    dialog.show()
    _app().processEvents()
    assert dialog.step_header.text().startswith("Step 1 of 5")
    dialog._next()
    assert dialog.step_header.text().startswith("Step 2 of 5: Set scale")
    assert "Required before parts can be placed" in dialog.step_header.text()
    dialog._next()
    assert dialog.step_header.text().startswith("Step 3 of 5")
    assert int(dialog.result()) != int(QDialog.DialogCode.Accepted)

    dialog._identify()
    assert dialog.item_list.count() == 0
    assert dialog.identify_note.isVisible()
    note = dialog.identify_note.text()
    assert "No AI connected" in note
    assert "part number" in note.lower()
    assert "reshoot" not in note.lower()
    assert "could not be read" not in note.lower()

    dialog._place()
    place = dialog.place_note.text()
    assert "Placed 0" in place
    assert "Skipped 0" in place
    assert "No AI connected" in place

    dialog._next()
    dialog._next()
    assert dialog.step_header.text().startswith("Step 5 of 5")
    assert int(dialog.result()) != int(QDialog.DialogCode.Accepted)
    assert dialog.isVisible()
    assert dialog.next.text() == "Close without changes"
    summary = dialog.review_summary.text()
    assert "Nothing was applied" in summary
    assert "No AI connected" in summary
    assert "add parts by hand" in summary
    assert [(sheet.id, sheet.kind, len(sheet.components)) for sheet in session.project.sheets] == before

    dialog.next.click()
    assert int(dialog.result()) == int(QDialog.DialogCode.Accepted)
    assert dialog.apply_result is not None
    assert dialog.apply_result.applied is False
    assert dialog.apply_result.sheet_id == ""
    assert "Nothing was applied" in dialog.apply_result.summary
    assert [(sheet.id, sheet.kind, len(sheet.components)) for sheet in session.project.sheets] == before
    dialog.close()


def test_place_shows_why_each_item_was_skipped(tmp_path: Path, monkeypatch) -> None:
    _silence_boxes(monkeypatch)
    session, _job, meta = _photo_session(tmp_path, "Skip")
    dialog = ReconstructWizard(session, vision=FakeVision(_recordings(meta)))
    dialog._identify()
    part = _part(session)
    assert "needs catalog" in _row_text(dialog, part.id)

    dialog._place()
    unconfirmed = dialog.place_note.text()
    assert part.id in unconfirmed
    assert "not confirmed" in unconfirmed
    assert "measured envelope" in unconfirmed

    _select(dialog, part.id)
    dialog.confirm_catalog.setText("MC-9b")
    dialog.confirm_tag.setText("K1")
    dialog._confirm_catalog()
    part = _part(session)
    assert item_is_layout_ready(part)
    assert "needs scale" in _row_text(dialog, part.id)
    dialog._place()
    unscaled = dialog.place_note.text()
    assert part.id in unscaled
    assert "no scale" in unscaled

    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    _face_scale(session, photo_id, meta)
    dialog._refresh_items()
    assert "ready" in _row_text(dialog, part.id)
    dialog.reject()


def test_apply_to_print_switches_sheet_and_shows_summary(tmp_path: Path, monkeypatch) -> None:
    _silence_boxes(monkeypatch)
    session, _job, meta = _photo_session(tmp_path, "Apply")
    window = MainWindow(session)
    window.show()
    _app().processEvents()
    assert session.active_sheet_id != "panel"
    holder: dict = {}

    def drive(dialog: ReconstructWizard) -> int:
        holder["dialog"] = dialog
        dialog.vision = FakeVision(_recordings(meta))
        dialog._identify()
        part = _part(session)
        _select(dialog, part.id)
        dialog.confirm_catalog.setText("MC-9b")
        dialog.confirm_tag.setText("K1")
        dialog._confirm_catalog()
        photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
        _face_scale(session, photo_id, meta)
        while dialog._page < dialog.stack.count() - 1:
            dialog._next()
        assert int(dialog.result()) != int(QDialog.DialogCode.Accepted)
        assert dialog.next.text() == "Apply to print"
        preview = dialog.review_summary.text()
        assert "Will add" in preview
        assert "Panel" in preview
        assert "K1" in preview
        dialog._next()
        return int(dialog.result())

    monkeypatch.setattr(ReconstructWizard, "exec", drive)
    window.reconstruct_from_photos()
    dialog = holder["dialog"]
    assert dialog.apply_result is not None
    assert dialog.apply_result.applied is True
    assert dialog.apply_result.sheet_id
    assert session.active_sheet_id == dialog.apply_result.sheet_id
    sheet = session.sheet()
    assert sheet.kind == "panel_layout"
    assert sheet.components
    comp = sheet.components[0]
    assert comp.library_id != "reconstruct.needs_info"
    assert is_layout_ready(session.catalog.get(comp.library_id), comp)
    message = window.statusBar().currentMessage()
    assert message == dialog.apply_result.summary
    assert message.startswith("Added ")
    assert f"sheet {sheet.title}" in message
    window.close()


def test_cancel_escape_and_close_ask_before_discard(tmp_path: Path, monkeypatch) -> None:
    prompts: list[tuple] = []
    answer = {"value": QMessageBox.StandardButton.No}

    def question(*args, **kwargs):
        prompts.append(args)
        return answer["value"]

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    session, _job, _meta = _photo_session(tmp_path, "Discard")
    dialog = ReconstructWizard(session)
    dialog.show()
    _app().processEvents()
    session.add_sheet("extra", kind="schematic")
    assert dialog._dirty()

    dialog.cancel.click()
    assert prompts
    assert prompts[-1][1] == "Discard changes?"
    assert prompts[-1][2] == "Discard changes made in this wizard?"
    assert dialog.isVisible()
    assert any(sheet.id == "extra" for sheet in session.project.sheets)

    prompts.clear()
    dialog.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
    assert prompts
    assert "Discard changes made in this wizard?" in prompts[-1]
    assert dialog.isVisible()
    assert any(sheet.id == "extra" for sheet in session.project.sheets)

    prompts.clear()
    assert dialog.close() is False
    _app().processEvents()
    assert prompts
    assert dialog.isVisible()
    assert any(sheet.id == "extra" for sheet in session.project.sheets)

    answer["value"] = QMessageBox.StandardButton.Yes
    dialog.cancel.click()
    assert not dialog.isVisible()
    assert all(sheet.id != "extra" for sheet in session.project.sheets)


def test_zero_mm_envelope_stays_refused_and_needs_info_is_unpriced(tmp_path: Path, monkeypatch) -> None:
    warnings: list[tuple] = []

    def warning(*args, **kwargs):
        warnings.append(args)
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(warning))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    session, _job, meta = _photo_session(tmp_path, "Envelope")
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    _face_scale(session, photo_id, meta)
    dialog = ReconstructWizard(session, vision=FakeVision(_recordings(meta)))
    assert dialog.env_lib.text() == ""
    assert dialog.env_w.value() == 0
    dialog._identify()
    part = _part(session)
    _select(dialog, part.id)
    dialog.env_w.setValue(0)
    dialog.env_h.setValue(0)
    dialog.env_d.setValue(0)
    dialog._confirm_envelope()
    assert warnings
    assert "at least 1 mm" in str(warnings[-1][2])
    part = _part(session)
    assert part.envelope_confirmed is False
    assert item_is_layout_ready(part) is False
    with pytest.raises(ValueError, match="at least 1 mm"):
        session.reconstruct_confirm_item(
            part.id,
            envelope_w_mm=0,
            envelope_h_mm=0,
            envelope_d_mm=0,
            mount="din_rail",
        )

    markers = [
        comp
        for sheet in session.project.sheets
        for comp in sheet.components
        if comp.library_id == "reconstruct.needs_info"
    ]
    assert markers
    for comp in markers:
        assert "width_mm" not in comp.properties
        assert "height_mm" not in comp.properties
        assert "depth_mm" not in comp.properties
        assert comp.properties.get("bom") == "no"

    bom = instance_rows(session.project, session.catalog)
    assert all(row["library_id"] != "reconstruct.needs_info" for row in bom)
    grouped = grouped_rows(session.project, session.catalog)
    assert all(row.get("library_id") != "reconstruct.needs_info" for row in grouped)
    quote = build_quote(session.project, session.catalog, session.parts)
    assert [row for row in quote["parts"] if row.get("needs_info")] == []
    assert quote["needs_info"] > 0
    assert "need info, not included" in quote["total_label"]

    while dialog._page < dialog.stack.count() - 1:
        dialog._next()
    review = dialog.review_summary.text()
    assert "need info" in review or "needs size" in review
    assert "needs size" in review
    assert "0.00" not in review
    dialog.reject()
