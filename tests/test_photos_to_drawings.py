"""Photos → drawings: one run, a short review queue, existing steps unchanged."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QFileDialog, QPushButton

from panelcad import Session
from panelcad.gui.main_window import MainWindow
from panelcad.gui.photos_to_drawings import PhotosToDrawingsDialog
from panelcad.gui.reconstruct_wizard import ReconstructWizard
from panelcad.reconstruct.models import ObservedItem, item_is_layout_ready
from panelcad.reconstruct.review_queue import REVIEW_CONFIDENCE_FLOOR, ReviewQueue, queue_line
from panelcad.reconstruct.vision import FakeVision, StubVision
from tests.artifacts import artifact_dir

_SHOTS = "photos-to-drawings"


def _app() -> QApplication:
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont("DejaVu Sans", 10))
    return app


def _photo_session(tmp_path: Path):
    from panelcad.reconstruct.fixtures import write_backpanel_photo

    _app()
    shot = tmp_path / "backpanel.png"
    meta = write_backpanel_photo(shot)
    job = tmp_path / "asbuilt.pcad.yaml"
    session = Session.create("Photos to drawings")
    session.save(job)
    session = Session.open(job)
    session.reconstruct_add_photos([shot], role="backpanel")
    photo_id = session.project.reconstruct.photos[0].id
    return session, photo_id, meta


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


def _device(meta: dict, *, tag: str, part_number: str, brand: str, confidence: float) -> dict:
    region = meta["part_region"]
    width = float(meta["width_px"])
    height = float(meta["height_px"])
    return {
        "tag": tag,
        "brand": brand,
        "part_number": part_number,
        "label_text": part_number or tag,
        "nx": region["x"] / width,
        "ny": region["y"] / height,
        "nw": region["w"] / width,
        "nh": region["h"] / height,
        "confidence": confidence,
        "unreadable": False,
    }


def _vision(photo_id: str, meta: dict, devices: list[dict]) -> FakeVision:
    return FakeVision(
        {
            photo_id: {
                "devices": devices,
                "strips": [],
                "rails": [],
                "ducts": [],
                "texts": [],
                "unreadable": False,
            }
        }
    )


def _catalog_device(meta: dict, *, tag: str = "K1", confidence: float = 0.92) -> dict:
    return _device(
        meta,
        tag=tag,
        part_number="MC-9b",
        brand="LS Electric",
        confidence=confidence,
    )


def _save_shot(widget, name: str, tmp_path: Path) -> Path:
    dest_dir = artifact_dir(tmp_path, _SHOTS)
    dest = dest_dir / name
    pixmap = widget.grab()
    assert not pixmap.isNull()
    assert pixmap.save(str(dest), "PNG")
    assert dest.stat().st_size > 5000
    mirror = Path("/opt/cursor/artifacts") / _SHOTS
    try:
        mirror.mkdir(parents=True, exist_ok=True)
        probe = mirror / ".write-probe"
        probe.write_bytes(b"ok")
        probe.unlink(missing_ok=True)
        assert pixmap.save(str(mirror / name), "PNG")
    except OSError:
        if not os.environ.get("PANELCAD_ARTIFACTS_DIR"):
            pass
    return dest


def _menu(window: MainWindow, label: str):
    for action in window.menuBar().actions():
        if action.text().replace("&", "") == label:
            return action.menu()
    raise AssertionError(f"menu {label} not found")


def _action(menu, label: str):
    for action in menu.actions():
        if action.text().replace("&", "") == label:
            return action
    raise AssertionError(f"action {label} not found")


def test_review_queue_accepts_any_kind_and_keeps_extra_fields():
    queue = ReviewQueue()
    row = queue.accept(
        kind="terminal_strip",
        reason="missing_info",
        title="Strip A",
        detail="Terminal labels were not read.",
        terminals=12,
        gap="upper",
    )
    assert row.kind == "terminal_strip"
    assert row.payload["terminals"] == 12
    assert row.payload["gap"] == "upper"
    assert queue.by_kind("terminal_strip") == [row]
    custom = queue.accept(kind="busbar", reason="needs_another_look", title="Bar")
    assert custom.reason == "needs_another_look"
    line = queue_line(custom)
    assert "needs another look" in line.lower()
    queue.dismiss(row.id)
    assert queue.by_kind("terminal_strip") == []
    blank = queue.accept(kind="duct", title="Duct")
    assert blank.reason == "missing_info"


def test_clean_run_generates_drawings_without_a_queue(tmp_path: Path):
    session, photo_id, meta = _photo_session(tmp_path)
    _face_scale(session, photo_id, meta)
    result = session.reconstruct_photos_to_drawings(
        vision=_vision(photo_id, meta, [_catalog_device(meta)]),
    )
    assert result["stopped"] is False
    assert result["drawings_generated"] is True
    assert result["queue"] == []
    assert result["placed"]
    assert result["drawings"]["schematic_sheet"]
    assert any(sheet.kind == "schematic" for sheet in session.project.sheets)
    item = session.project.reconstruct.item(result["placed"][0]["id"])
    assert item_is_layout_ready(item)
    assert item.placed_ref


def test_low_confidence_stops_and_does_not_generate_drawings(tmp_path: Path):
    session, photo_id, meta = _photo_session(tmp_path)
    _face_scale(session, photo_id, meta)
    result = session.reconstruct_photos_to_drawings(
        vision=_vision(photo_id, meta, [_catalog_device(meta, confidence=0.59)]),
    )
    assert result["stopped"] is True
    assert result["drawings_generated"] is False
    assert result["drawings"] is None
    assert len(result["queue"]) == 1
    row = result["queue"][0]
    assert row["reason"] == "low_confidence"
    assert row["kind"] == "part"
    assert row["confidence"] == pytest.approx(0.59)
    item = session.project.reconstruct.item(row["source_id"])
    assert item_is_layout_ready(item) is False
    assert result["placed"] == []
    assert all(sheet.id != "asbuilt" for sheet in session.project.sheets)


def test_confidence_at_the_floor_passes(tmp_path: Path):
    session, photo_id, meta = _photo_session(tmp_path)
    _face_scale(session, photo_id, meta)
    assert REVIEW_CONFIDENCE_FLOOR == 0.6
    result = session.reconstruct_photos_to_drawings(
        vision=_vision(photo_id, meta, [_catalog_device(meta, confidence=0.6)]),
    )
    assert result["queue"] == []
    assert result["drawings_generated"] is True


def test_missing_info_and_missing_scale_are_queued(tmp_path: Path):
    session, photo_id, meta = _photo_session(tmp_path)
    unread = session.reconstruct_photos_to_drawings(
        vision=_vision(
            photo_id,
            meta,
            [_device(meta, tag="M1", part_number="", brand="", confidence=0.95)],
        ),
    )
    assert unread["stopped"] is True
    assert unread["drawings_generated"] is False
    assert unread["queue"][0]["reason"] == "missing_info"
    assert "catalog" in unread["queue"][0]["detail"].lower()

    session2, photo2, meta2 = _photo_session(tmp_path / "scaled")
    ready = session2.reconstruct_photos_to_drawings(
        vision=_vision(photo2, meta2, [_catalog_device(meta2, tag="K2")]),
    )
    assert ready["stopped"] is True
    assert ready["queue"][0]["reason"] == "missing_info"
    assert "scale" in ready["queue"][0]["detail"].lower()
    assert ready["placed"] == []


def test_unflagged_item_passes_while_a_flagged_item_stops_the_run(tmp_path: Path):
    session, photo_id, meta = _photo_session(tmp_path)
    _face_scale(session, photo_id, meta)
    devices = [
        _catalog_device(meta, tag="K1", confidence=0.93),
        _device(meta, tag="M1", part_number="", brand="", confidence=0.91),
    ]
    result = session.reconstruct_photos_to_drawings(vision=_vision(photo_id, meta, devices))
    assert result["stopped"] is True
    assert result["drawings_generated"] is False
    titles = [row["title"] for row in result["queue"]]
    assert "K1" not in titles
    assert "M1" in titles
    assert result["placed"]
    assert result["placed"][0]["ref"]
    passed = session.project.reconstruct.item(result["placed"][0]["id"])
    assert passed.tag == "K1"
    held = next(item for item in session.project.reconstruct.items if item.tag == "M1")
    assert held.id not in result["passed"]
    assert passed.id in result["passed"]


def test_queue_stops_for_a_terminal_strip_flag_and_for_any_role(tmp_path: Path):
    session, photo_id, meta = _photo_session(tmp_path)
    _face_scale(session, photo_id, meta)
    session.project.reconstruct.items.append(
        ObservedItem(
            id="term-ready",
            photo_id=photo_id,
            role="terminal",
            tag="XT",
            terminal="1",
            confidence=0.95,
        )
    )
    session.project.reconstruct.items.append(
        ObservedItem(
            id="term-low",
            photo_id=photo_id,
            role="terminal",
            tag="XT",
            terminal="2",
            confidence=0.2,
        )
    )
    result = session.reconstruct_photos_to_drawings(
        vision=_vision(photo_id, meta, [_catalog_device(meta)]),
        extra_flags=[
            {
                "id": "strip-1",
                "kind": "terminal_strip",
                "reason": "possible_missed_device",
                "title": "Strip",
                "detail": "A strip may have been missed.",
                "source_id": "strip-1",
                "payload": {"slots": 8},
            }
        ],
    )
    kinds = {row["kind"] for row in result["queue"]}
    assert "terminal_strip" in kinds
    assert "terminal" in kinds
    assert result["stopped"] is True
    assert result["drawings_generated"] is False
    strip = next(row for row in result["queue"] if row["kind"] == "terminal_strip")
    assert strip["payload"]["slots"] == 8
    assert result["placed"]
    low = next(row for row in result["queue"] if row["source_id"] == "term-low")
    assert low["reason"] == "low_confidence"
    assert "term-ready" in result["passed"]
    assert "term-low" not in result["passed"]


def test_possible_missed_device_can_be_any_kind_and_can_be_dismissed(tmp_path: Path):
    session, photo_id, meta = _photo_session(tmp_path)
    _face_scale(session, photo_id, meta)
    callouts = [
        {
            "id": "gap-1",
            "kind": "terminal_strip",
            "label": "Unboxed strip",
            "detail": "Possible missed strip on the rail.",
            "x": 10,
            "y": 20,
            "w": 30,
            "h": 40,
        }
    ]
    vision = _vision(photo_id, meta, [_catalog_device(meta)])
    stopped = session.reconstruct_photos_to_drawings(vision=vision, unboxed_callouts=callouts)
    assert stopped["drawings_generated"] is False
    row = next(item for item in stopped["queue"] if item["reason"] == "possible_missed_device")
    assert row["kind"] == "terminal_strip"
    assert row["payload"]["x"] == 10
    finished = session.reconstruct_photos_to_drawings(
        vision=vision,
        unboxed_callouts=callouts,
        dismissed_ids=[row["id"]],
    )
    assert finished["queue"] == []
    assert finished["drawings_generated"] is True


def test_accepting_a_low_confidence_read_then_continues_to_drawings(tmp_path: Path):
    session, photo_id, meta = _photo_session(tmp_path)
    _face_scale(session, photo_id, meta)
    vision = _vision(photo_id, meta, [_catalog_device(meta, confidence=0.4)])
    stopped = session.reconstruct_photos_to_drawings(vision=vision)
    source = stopped["queue"][0]["source_id"]
    item = session.project.reconstruct.item(source)
    session.reconstruct_confirm_item(
        item.id,
        catalog=item.matches[0].catalog,
        manufacturer=item.matches[0].manufacturer,
        tag=item.tag,
    )
    finished = session.reconstruct_photos_to_drawings(vision=vision)
    assert finished["stopped"] is False
    assert finished["drawings_generated"] is True
    assert finished["placed"]


def test_no_photos_and_no_reader_stop_without_drawings(tmp_path: Path):
    _app()
    empty = Session.create("Empty")
    with pytest.raises(ValueError, match="Add photos"):
        empty.reconstruct_photos_to_drawings()

    session, photo_id, _meta = _photo_session(tmp_path)
    result = session.reconstruct_photos_to_drawings(vision=StubVision())
    assert result["stopped"] is True
    assert result["drawings_generated"] is False
    assert result["queue"][0]["kind"] == "photo"
    assert result["queue"][0]["reason"] == "missing_info"
    assert "aren't read automatically" in result["queue"][0]["detail"]


def test_existing_reconstruct_steps_still_run_on_their_own(tmp_path: Path):
    session, photo_id, meta = _photo_session(tmp_path)
    wizard = ReconstructWizard(session)
    wizard.resize(820, 620)
    wizard.show()
    _app().processEvents()
    assert wizard.stack.count() == 5
    assert "Step 1 of 5" in wizard.step_header.text()
    assert wizard.next.text() == "Next"
    wizard.next.click()
    assert "Step 2 of 5" in wizard.step_header.text()
    wizard.next.click()
    assert "Step 3 of 5" in wizard.step_header.text()
    assert hasattr(wizard, "photo_feedback_btn")
    wizard.next.click()
    wizard.next.click()
    assert wizard.stack.currentIndex() == 4
    assert "Apply to print" in wizard.next.text() or "Close" in wizard.next.text()
    wizard.close()

    _face_scale(session, photo_id, meta)
    identified = session.reconstruct_identify(
        vision=_vision(photo_id, meta, [_catalog_device(meta, tag="K9")])
    )
    assert identified["items"]
    report = session.reconstruct_photo_feedback(photo_id=photo_id)
    assert report["total_detected"] >= 1
    item = next(row for row in session.project.reconstruct.items if row.tag == "K9")
    session.reconstruct_confirm_item(
        item.id,
        catalog="MC-9b",
        manufacturer="LS Electric",
        tag="K9",
    )
    placed = session.reconstruct_place_confirmed()
    assert "placed" in placed and "skipped" in placed
    item = session.project.reconstruct.item(item.id)
    assert item_is_layout_ready(item)
    assert item.placed_ref
    drawn = session.reconstruct_apply_to_print()
    assert drawn["schematic_sheet"]
    assert session.project.sheet(drawn["schematic_sheet"]).kind == "schematic"


def test_gui_run_shows_only_the_flagged_queue_and_writes_shots(tmp_path: Path):
    session, photo_id, meta = _photo_session(tmp_path)
    _face_scale(session, photo_id, meta)
    devices = [
        _catalog_device(meta, tag="K1", confidence=0.94),
        _catalog_device(meta, tag="K2", confidence=0.35),
        _device(meta, tag="M1", part_number="", brand="", confidence=0.88),
    ]
    dialog = PhotosToDrawingsDialog(
        session,
        vision=_vision(photo_id, meta, devices),
        unboxed_callouts=[
            {
                "id": "gap-9",
                "kind": "device",
                "label": "Unboxed device",
                "detail": "Possible missed device on the rail.",
            }
        ],
    )
    dialog.resize(960, 700)
    dialog.show()
    _app().processEvents()
    assert dialog.photo_list.count() == 1
    assert dialog.flagged_rows() == []
    _save_shot(dialog, "01-before-run.png", tmp_path)

    dialog.run_btn.click()
    _app().processEvents()
    rows = dialog.flagged_rows()
    titles = [row["title"] for row in rows]
    assert "K1" not in titles
    assert "K2" in titles
    assert "M1" in titles
    assert any(row["reason"] == "possible_missed_device" for row in rows)
    assert dialog.result_info is not None
    assert dialog.result_info.drawings_generated is False
    assert dialog.continue_btn.isEnabled()
    assert "Stopped" in dialog.status.text()
    dialog.queue.setCurrentRow(0)
    _app().processEvents()
    _save_shot(dialog, "02-review-queue.png", tmp_path)
    dialog.close()

    clean_session, clean_photo, clean_meta = _photo_session(tmp_path / "clean")
    _face_scale(clean_session, clean_photo, clean_meta)
    clean = PhotosToDrawingsDialog(
        clean_session,
        vision=_vision(clean_photo, clean_meta, [_catalog_device(clean_meta)]),
    )
    clean.resize(960, 700)
    clean.show()
    _app().processEvents()
    clean.run_btn.click()
    _app().processEvents()
    assert clean.flagged_rows() == []
    assert clean.result_info is not None
    assert clean.result_info.drawings_generated is True
    assert "Drawings generated" in clean.status.text()
    _save_shot(clean, "03-drawings-ready.png", tmp_path)
    clean.close()


def test_gui_accept_read_clears_a_low_confidence_stop(tmp_path: Path):
    session, photo_id, meta = _photo_session(tmp_path)
    _face_scale(session, photo_id, meta)
    dialog = PhotosToDrawingsDialog(
        session,
        vision=_vision(photo_id, meta, [_catalog_device(meta, confidence=0.33)]),
    )
    dialog.show()
    _app().processEvents()
    dialog.run_btn.click()
    assert len(dialog.flagged_rows()) == 1
    assert dialog.accept_btn.isEnabled()
    dialog.accept_btn.click()
    _app().processEvents()
    assert dialog.flagged_rows() == []
    assert dialog.result_info is not None
    assert dialog.result_info.drawings_generated is True
    dialog.close()


def test_partial_and_longer_labels_stay_queued_and_keep_their_score(tmp_path: Path):
    session, photo_id, meta = _photo_session(tmp_path)
    _face_scale(session, photo_id, meta)
    partial = session.reconstruct_photos_to_drawings(
        vision=_vision(
            photo_id,
            meta,
            [_device(meta, tag="K1", part_number="MC-9", brand="LS Electric", confidence=0.91)],
        ),
    )
    assert partial["drawings_generated"] is False
    assert partial["queue"][0]["reason"] == "low_confidence"
    partial_item = session.project.reconstruct.item(partial["queue"][0]["source_id"])
    assert partial_item.status == "needs_info"
    assert item_is_layout_ready(partial_item) is False
    assert partial_item.label_part_number == "MC-9"
    assert partial_item.matches[0].catalog == "MC-9b"
    assert partial_item.matches[0].confidence == pytest.approx(0.59, abs=0.01)
    assert partial_item.matches[0].confidence < 0.8

    longer_session, longer_photo, longer_meta = _photo_session(tmp_path / "longer")
    _face_scale(longer_session, longer_photo, longer_meta)
    longer = longer_session.reconstruct_photos_to_drawings(
        vision=_vision(
            longer_photo,
            longer_meta,
            [
                _device(
                    longer_meta,
                    tag="K2",
                    part_number="MC-9b-XYZ123",
                    brand="LS Electric",
                    confidence=0.93,
                )
            ],
        ),
    )
    assert longer["drawings_generated"] is False
    assert longer["queue"][0]["reason"] == "low_confidence"
    longer_item = longer_session.project.reconstruct.item(longer["queue"][0]["source_id"])
    assert longer_item.matches[0].catalog == "MC-9b"
    assert longer_item.matches[0].confidence == pytest.approx(0.54, abs=0.01)
    assert item_is_layout_ready(longer_item) is False


def test_exact_label_passes_and_keeps_the_match_score(tmp_path: Path):
    session, photo_id, meta = _photo_session(tmp_path)
    _face_scale(session, photo_id, meta)
    result = session.reconstruct_photos_to_drawings(
        vision=_vision(
            photo_id,
            meta,
            [_device(meta, tag="K1", part_number="MC-9b", brand="LS Electric", confidence=0.91)],
        ),
    )
    assert result["queue"] == []
    assert result["drawings_generated"] is True
    item = next(row for row in session.project.reconstruct.items if row.tag == "K1")
    assert item_is_layout_ready(item)
    assert item.matches[0].catalog == "MC-9b"
    assert item.matches[0].confidence == pytest.approx(0.99, abs=0.01)
    assert item.label_part_number == "MC-9b"


def test_exact_label_with_another_plausible_part_stays_queued(tmp_path: Path):
    session, photo_id, meta = _photo_session(tmp_path)
    _face_scale(session, photo_id, meta)
    result = session.reconstruct_photos_to_drawings(
        vision=_vision(
            photo_id,
            meta,
            [
                _device(
                    meta,
                    tag="K1",
                    part_number="MT-32/3K2.1",
                    brand="LS Electric",
                    confidence=0.95,
                )
            ],
        ),
    )
    assert result["drawings_generated"] is False
    assert result["queue"][0]["reason"] == "low_confidence"
    item = session.project.reconstruct.item(result["queue"][0]["source_id"])
    assert item_is_layout_ready(item) is False
    assert item.matches[0].confidence == pytest.approx(0.99, abs=0.01)
    assert any(match.catalog == "MT-32/3K21.5" for match in item.matches)


def test_accepting_a_partial_label_keeps_the_fuzzy_score(tmp_path: Path):
    session, photo_id, meta = _photo_session(tmp_path)
    _face_scale(session, photo_id, meta)
    vision = _vision(
        photo_id,
        meta,
        [_device(meta, tag="K1", part_number="MC-9", brand="LS Electric", confidence=0.92)],
    )
    stopped = session.reconstruct_photos_to_drawings(vision=vision)
    source = stopped["queue"][0]["source_id"]
    held = session.project.reconstruct.item(source)
    assert held.matches[0].confidence == pytest.approx(0.59, abs=0.01)
    finished = session.reconstruct_photos_to_drawings(vision=vision, accepted_ids=[source])
    assert finished["drawings_generated"] is True
    item = session.project.reconstruct.item(source)
    assert item_is_layout_ready(item)
    assert item.label_part_number == "MC-9"
    assert item.matches[0].catalog == "MC-9b"
    assert item.matches[0].confidence == pytest.approx(0.59, abs=0.01)


def test_existing_needs_info_is_not_auto_confirmed(tmp_path: Path):
    session, photo_id, meta = _photo_session(tmp_path)
    _face_scale(session, photo_id, meta)
    vision = _vision(
        photo_id,
        meta,
        [_device(meta, tag="K1", part_number="MC-9b", brand="LS Electric", confidence=0.95)],
    )
    first = session.reconstruct_photos_to_drawings(vision=vision)
    assert first["drawings_generated"] is True
    item = next(row for row in session.project.reconstruct.items if row.tag == "K1")
    item.status = "needs_info"
    again = session.reconstruct_photos_to_drawings(vision=vision)
    assert again["drawings_generated"] is False
    held = session.project.reconstruct.item(item.id)
    assert held.status == "needs_info"
    assert item_is_layout_ready(held) is False
    assert any(row["source_id"] == held.id for row in again["queue"])


def test_low_read_with_no_catalog_match_is_low_confidence(tmp_path: Path):
    session, photo_id, meta = _photo_session(tmp_path)
    _face_scale(session, photo_id, meta)
    result = session.reconstruct_photos_to_drawings(
        vision=_vision(
            photo_id,
            meta,
            [_device(meta, tag="M1", part_number="", brand="", confidence=0.42)],
        ),
    )
    assert result["drawings_generated"] is False
    assert len(result["queue"]) == 1
    assert result["queue"][0]["reason"] == "low_confidence"
    assert result["queue"][0]["confidence"] == pytest.approx(0.42)


def test_flag_without_a_reason_does_not_crash(tmp_path: Path):
    session, photo_id, meta = _photo_session(tmp_path)
    _face_scale(session, photo_id, meta)
    result = session.reconstruct_photos_to_drawings(
        vision=_vision(photo_id, meta, [_catalog_device(meta)]),
        extra_flags=[{"kind": "terminal_strip", "title": "Strip", "confidence": "n/a"}],
    )
    assert result["stopped"] is True
    assert result["drawings_generated"] is False
    row = next(item for item in result["queue"] if item["kind"] == "terminal_strip")
    assert row["reason"] == "missing_info"


def test_other_role_photos_are_not_skipped(tmp_path: Path):
    from panelcad.reconstruct.fixtures import write_backpanel_photo

    session, photo_id, meta = _photo_session(tmp_path)
    _face_scale(session, photo_id, meta)
    for index in range(4):
        shot = tmp_path / f"extra-{index}.png"
        write_backpanel_photo(shot, seed=30 + index)
        session.reconstruct_add_photos([shot], role="other")
    ids = [photo.id for photo in session.project.reconstruct.photos]
    assert ids[0] == photo_id
    assert "p5" in ids
    result = session.reconstruct_photos_to_drawings(
        vision=_vision(photo_id, meta, [_catalog_device(meta)]),
    )
    queued = {row["photo_id"] for row in result["queue"] if row["kind"] == "photo"}
    assert "p5" in queued
    assert result["drawings_generated"] is False


def test_cli_accept_and_dismiss_clear_a_stopped_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import json

    from typer.testing import CliRunner

    from panelcad.cli.main import app

    monkeypatch.setenv("PANELCAD_RECONSTRUCT_VISION", "stub")
    session, photo_id, meta = _photo_session(tmp_path)
    _face_scale(session, photo_id, meta)
    for index in range(1):
        shot = tmp_path / "second.png"
        from panelcad.reconstruct.fixtures import write_backpanel_photo

        write_backpanel_photo(shot, seed=44)
        session.reconstruct_add_photos([shot], role="other")
    session.save(session.path)
    project = str(session.path)
    runner = CliRunner()
    stopped = runner.invoke(app, ["reconstruct", "photos-to-drawings", project])
    assert stopped.exit_code == 0, stopped.output
    payload = json.loads(stopped.output)
    assert payload["stopped"] is True
    photo_flags = [row["id"] for row in payload["queue"] if row["kind"] == "photo"]
    assert photo_flags
    cleared = runner.invoke(
        app,
        ["reconstruct", "photos-to-drawings", project, "--dismiss", photo_flags[0]],
    )
    assert cleared.exit_code == 0, cleared.output
    # The first photo may still be unread without a vision backend, so dismiss every photo flag.
    again = json.loads(cleared.output)
    remaining = [row["id"] for row in again["queue"] if row["kind"] == "photo"]
    if remaining:
        args = ["reconstruct", "photos-to-drawings", project]
        for flag_id in photo_flags:
            args.extend(["--dismiss", flag_id])
        cleared = runner.invoke(app, args)
        assert cleared.exit_code == 0, cleared.output
        again = json.loads(cleared.output)
    assert again["stopped"] is False
    assert again["drawings_generated"] is True

    fuzzy = _photo_session(tmp_path / "fuzzy")
    fuzzy_session, fuzzy_photo, fuzzy_meta = fuzzy
    _face_scale(fuzzy_session, fuzzy_photo, fuzzy_meta)
    fuzzy_session.reconstruct_identify(
        vision=_vision(
            fuzzy_photo,
            fuzzy_meta,
            [
                _device(
                    fuzzy_meta,
                    tag="K1",
                    part_number="MC-9",
                    brand="LS Electric",
                    confidence=0.9,
                )
            ],
        )
    )
    fuzzy_session.save(fuzzy_session.path)
    held = runner.invoke(app, ["reconstruct", "photos-to-drawings", str(fuzzy_session.path)])
    assert held.exit_code == 0, held.output
    held_payload = json.loads(held.output)
    assert held_payload["stopped"] is True
    source = held_payload["queue"][0]["source_id"]
    accepted = runner.invoke(
        app,
        ["reconstruct", "photos-to-drawings", str(fuzzy_session.path), "--accept", source],
    )
    assert accepted.exit_code == 0, accepted.output
    accepted_payload = json.loads(accepted.output)
    assert accepted_payload["drawings_generated"] is True
    opened = Session.open(fuzzy_session.path)
    item = next(row for row in opened.project.reconstruct.items if row.tag == "K1")
    assert item.matches[0].confidence == pytest.approx(0.59, abs=0.01)


def test_add_photos_button_and_menu_open_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from panelcad.reconstruct.fixtures import write_backpanel_photo

    _app()
    shot = tmp_path / "panel.png"
    write_backpanel_photo(shot)
    session = Session.create("Add")
    dialog = PhotosToDrawingsDialog(session, vision=StubVision())
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileNames",
        lambda *_args, **_kwargs: ([str(shot)], ""),
    )
    dialog.findChild(QPushButton, "photosToDrawingsAdd").click()
    assert dialog.photo_list.count() == 1
    dialog.close()

    opened: dict[str, str] = {}

    def _exec(self: PhotosToDrawingsDialog) -> int:
        opened["title"] = self.windowTitle()
        opened["name"] = self.objectName()
        return 0

    monkeypatch.setattr(PhotosToDrawingsDialog, "exec", _exec)
    window = MainWindow(Session.create("Menu"))
    window.show()
    _app().processEvents()
    file_menu = _menu(window, "File")
    tools_menu = _menu(window, "Tools")
    assert _action(file_menu, "Reconstruct from photos…") is not None
    assert _action(tools_menu, "Cabinet Photo Feedback Overlay…") is not None
    _action(file_menu, "Photos → drawings…").trigger()
    assert opened == {"title": "Photos → drawings", "name": "photosToDrawings"}
    opened.clear()
    _action(tools_menu, "Photos → drawings…").trigger()
    assert opened["name"] == "photosToDrawings"
    window.close()
