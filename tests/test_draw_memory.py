"""Memory bounds for drawing: undo diffs, sheet scenes, images, and PDF documents."""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from panelcad.api.history import apply_value, diff_value
from panelcad.api.session import Session
from panelcad.memory import (
    pixmap_cache_bytes,
    scene_cache_slots,
    sheet_cache_limit,
    total_ram_bytes,
)


def test_diff_round_trip_covers_edits_inserts_and_reorders():
    old = {
        "project": {
            "name": "Job",
            "sheets": [
                {"id": "a", "components": [{"id": "S1", "x": 10.0}], "wires": [{"id": "W1", "number": "1"}]},
                {"id": "b", "components": [{"id": "K1", "x": 20.0}], "wires": []},
            ],
        },
        "active_sheet_id": "a",
        "catalog": object(),
    }
    new = {
        "project": {
            "name": "Job",
            "sheets": [
                {"id": "b", "components": [{"id": "K1", "x": 24.0}, {"id": "K2", "x": 30.0}], "wires": []},
                {"id": "a", "components": [{"id": "S1", "x": 10.0}], "wires": []},
            ],
        },
        "active_sheet_id": "b",
        "catalog": old["catalog"],
    }
    patch = diff_value(old, new)
    assert patch is not None
    assert apply_value(old, patch) == new
    assert diff_value(new, new) is None


def test_undo_stores_diffs_and_still_round_trips():
    session = Session.create("Diff undo")
    session.place("iec.operators.pb_no", ref="S1", x=10, y=10)
    session.place("iec.operators.pb_no", ref="S2", x=40, y=10)
    session.wire("S1.14", "S2.13")
    for index in range(10):
        session.move("S1", 12 + index, 15, snap=False)
    patches = [entry for entry in session._undo if "patch" in entry]
    fulls = [entry for entry in session._undo if isinstance(entry.get("snap"), dict)]
    assert patches
    assert len(patches) > len(fulls)
    tip = session._undo_tip
    assert tip is not None
    one = len(json.dumps(tip["project"], default=str))
    kept = 0
    for entry in session._undo:
        if isinstance(entry.get("snap"), dict):
            kept += len(json.dumps(entry["snap"]["project"], default=str))
        else:
            kept += len(repr(entry.get("patch")))
    assert kept < one * 3
    final = session.project.model_dump(mode="json")
    catalog = session.catalog
    names = session.undo_command_names()
    assert names
    steps = 0
    while session.undo():
        steps += 1
    assert steps == len(names)
    assert session.catalog is catalog
    assert session.sheet().components == []
    assert session.sheet().wires == []
    for _ in range(steps):
        assert session.redo()
    assert session.project.model_dump(mode="json") == final
    assert not session.can_redo()


def test_trimmed_diff_history_restores_the_oldest_kept_step():
    session = Session.create("Trim")
    session.MAX_UNDO = 5
    session.place("iec.operators.pb_no", ref="S1", x=10, y=10)
    for index in range(8):
        session.move("S1", 20 + index, 30, snap=False)
    assert len(session._undo) == 5
    assert isinstance(session._undo[0].get("snap"), dict)
    expected_x = 20 + (8 - 5) - 1
    # Eight moves land on x=27. Five undos return to the state before the last five,
    # which is the position after the third move (x=22).
    assert session.sheet().component("S1").x == 27
    for _ in range(5):
        assert session.undo()
    assert not session.undo()
    assert session.sheet().component("S1").x == expected_x


def test_low_ram_shrinks_the_cache_budgets(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PANELCAD_SCENE_SLOTS", raising=False)
    monkeypatch.delenv("PANELCAD_PIXMAP_BYTES", raising=False)
    monkeypatch.setenv("PANELCAD_RAM_BYTES", str(4 * 1024**3))
    assert total_ram_bytes() == 4 * 1024**3
    assert scene_cache_slots() == 1
    assert sheet_cache_limit() == 12
    assert pixmap_cache_bytes() <= 24 * 1024 * 1024

    monkeypatch.setenv("PANELCAD_RAM_BYTES", str(12 * 1024**3))
    assert scene_cache_slots() == 4
    assert sheet_cache_limit() == 16

    monkeypatch.setenv("PANELCAD_RAM_BYTES", str(24 * 1024**3))
    assert scene_cache_slots() == 8
    assert sheet_cache_limit() == 24

    monkeypatch.setenv("PANELCAD_RAM_BYTES", str(64 * 1024**3))
    assert scene_cache_slots() >= 8
    assert sheet_cache_limit() >= 24


def test_pixmap_cache_is_an_lru_with_a_byte_cap(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from PIL import Image
    from PySide6.QtWidgets import QApplication

    from panelcad.render.images import cached_qimage, clear_image_cache, image_cache_bytes

    QApplication.instance() or QApplication([])
    monkeypatch.setenv("PANELCAD_PIXMAP_BYTES", str(8 * 1024 * 1024))
    clear_image_cache()
    first = tmp_path / "a.png"
    Image.new("RGB", (20, 20), (10, 20, 30)).save(first)
    loaded = cached_qimage(first)
    assert not loaded.isNull()
    assert loaded.pixelColor(0, 0).red() == 10
    again = cached_qimage(first)
    assert again.cacheKey() == loaded.cacheKey()
    weight = image_cache_bytes()
    assert weight > 0
    # Three copies of this image fit. The fourth has to evict the oldest.
    monkeypatch.setenv("PANELCAD_PIXMAP_BYTES", str(weight * 3 + 1))
    for index in range(3):
        path = tmp_path / f"extra-{index}.png"
        Image.new("RGB", (20, 20), (index, 0, 0)).save(path)
        cached_qimage(path)
    assert image_cache_bytes() <= weight * 3 + 1
    # A file that changes on disk is decoded again.
    Image.new("RGB", (20, 20), (200, 0, 0)).save(first)
    changed = cached_qimage(first)
    assert changed.pixelColor(0, 0).red() == 200
    clear_image_cache()
    assert image_cache_bytes() == 0


def test_sheet_scenes_are_reused_then_released(monkeypatch: pytest.MonkeyPatch):
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import ComponentItem, SchematicView, WireItem

    monkeypatch.setenv("PANELCAD_SCENE_SLOTS", "2")
    QApplication.instance() or QApplication([])
    session = Session.create("Scenes")
    session.place("iec.operators.pb_no", ref="S1", x=30, y=30)
    session.place("iec.operators.pb_no", ref="S2", x=80, y=30)
    session.wire("S1.14", "S2.13")
    session.add_sheet("second", title="Second")
    session.place("iec.operators.pb_no", ref="S9", x=40, y=40, sheet_id="second")
    session.add_sheet("third", title="Third")
    session.set_active_sheet("main")
    view = SchematicView(session)
    try:
        main_scene = view.scene
        session.set_active_sheet("second")
        view.reload()
        second_scene = view.scene
        assert second_scene is not main_scene
        assert set(view._sheet_scenes) == {"main", "second"}
        session.set_active_sheet("main")
        view.reload()
        assert view.scene is main_scene
        wires = [item for item in view._items if isinstance(item, WireItem)]
        assert wires
        path = wires[0].path()
        points = tuple((round(path.elementAt(i).x, 2), round(path.elementAt(i).y, 2)) for i in range(path.elementCount()))
        fresh = SchematicView(session)
        try:
            fresh_wires = [item for item in fresh._items if isinstance(item, WireItem)]
            fresh_path = fresh_wires[0].path()
            fresh_points = tuple(
                (round(fresh_path.elementAt(i).x, 2), round(fresh_path.elementAt(i).y, 2))
                for i in range(fresh_path.elementCount())
            )
        finally:
            fresh.close()
        assert points == fresh_points
        session.set_active_sheet("third")
        view.reload()
        assert len(view._sheet_scenes) == 2
        assert "second" not in view._sheet_scenes
        session.move("S1", 140, 50, sheet_id="main", snap=False)
        session.set_active_sheet("main")
        view.reload()
        shown = next(item for item in view.scene.items() if isinstance(item, ComponentItem) and item.component_id == "S1")
        assert shown.pos().x() == pytest.approx(140)
        assert shown.pos().y() == pytest.approx(50)
    finally:
        view.close()


def test_one_scene_slot_releases_the_sheet_that_was_left(monkeypatch: pytest.MonkeyPatch):
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import SchematicView

    monkeypatch.setenv("PANELCAD_SCENE_SLOTS", "1")
    QApplication.instance() or QApplication([])
    session = Session.create("One slot")
    session.add_sheet("other", title="Other")
    session.set_active_sheet("main")
    view = SchematicView(session)
    try:
        session.set_active_sheet("other")
        view.reload()
        assert list(view._sheet_scenes) == ["other"]
    finally:
        view.close()


def test_open_pdf_page_is_closed_and_matches_the_page_reader(tmp_path):
    import pymupdf

    from panelcad.importer.ingest import ingest_paths
    from panelcad.importer.read import release_mupdf_store, tokens_from_page, tokens_from_pdf
    from panelcad.importer.vectors import strokes_from_page, strokes_from_pdf
    from panelcad.importer.workspace import Workspace

    path = tmp_path / "print.pdf"
    doc = pymupdf.open()
    for index in range(2):
        page = doc.new_page(width=420, height=297)
        page.insert_text((36, 48), f"MOTOR K{index}")
        page.draw_line((40, 80), (180, 80))
    doc.save(path)
    doc.close()

    tokens, _meta = tokens_from_pdf(path, 0)
    assert [item["text"] for item in tokens] == ["MOTOR", "K0"]
    opened = pymupdf.open(path)
    try:
        again, _meta = tokens_from_page(opened[0])
        strokes = strokes_from_page(opened[0])
    finally:
        opened.close()
        release_mupdf_store()
    assert [item["text"] for item in again] == ["MOTOR", "K0"]
    assert strokes_from_pdf(path, 0) == strokes
    pages = ingest_paths([path], Workspace(tmp_path / "ws"))
    assert len(pages) == 2
    assert pages[0]["token_count"] == 2
    assert "MOTOR" in pages[0]["text"]
    assert opened.is_closed


def test_parts_cache_follows_the_overlay_and_keeps_sessions_apart(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from panelcad.library.parts import default_parts_catalog

    monkeypatch.delenv("PANELCAD_LIBRARY_PATH", raising=False)
    plain = default_parts_catalog()
    sample = next(iter(plain.parts.values()))
    plain.parts["not-a-real-part"] = sample
    again = default_parts_catalog()
    assert "not-a-real-part" not in again.parts

    extra = tmp_path / "extra"
    extra.mkdir()
    extra.joinpath("eaton_fix.yaml").write_text(
        """
kind: manufacturer_parts
manufacturer: Eaton
parts:
  - catalog: "FAZ-C10/1-NA"
    library_id: iec.breakers.mcb_1p_16a
    category: protection.breaker
    description: Overlay FAZ note
    verified: full
    source: https://www.eaton.com/us/en-us/skuPage.102087.html
    amps: 10
    poles: 1
    width_mm: 17.7
    height_mm: 105
    depth_mm: 75.5
    mounting: din
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PANELCAD_LIBRARY_PATH", str(extra))
    overlaid = default_parts_catalog()
    assert overlaid.get("FAZ-C10/1-NA").description == "Overlay FAZ note"
    note = extra / "eaton_fix.yaml"
    note.write_text(note.read_text(encoding="utf-8").replace("Overlay FAZ note", "Edited FAZ note"), encoding="utf-8")
    os.utime(note, (note.stat().st_atime, note.stat().st_mtime + 5))
    edited = default_parts_catalog()
    assert edited.get("FAZ-C10/1-NA").description == "Edited FAZ note"


def test_wizard_cancel_does_not_undo_back_into_discarded_sheets(monkeypatch: pytest.MonkeyPatch):
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.reconstruct_wizard import ReconstructWizard

    QApplication.instance() or QApplication([])
    session = Session.create("Cancel tips")
    session.place("iec.operators.pb_no", ref="S1", x=10, y=10)
    dialog = ReconstructWizard(session)
    session.add_sheet("extra-a")
    session.add_sheet("extra-b")
    session.place("iec.operators.pb_no", ref="S2", x=20, y=20, sheet_id="extra-b")
    assert len(session.project.sheets) == 3
    dialog.reject()
    assert [sheet.id for sheet in session.project.sheets] == ["main"]
    assert session.undo()
    assert [sheet.id for sheet in session.project.sheets] == ["main"]
    assert [comp.id for comp in session.sheet().components] == []


def test_direct_model_edit_and_mid_drag_follow_the_model(monkeypatch: pytest.MonkeyPatch):
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import ComponentItem, SchematicView

    monkeypatch.setenv("PANELCAD_SCENE_SLOTS", "256")
    QApplication.instance() or QApplication([])
    session = Session.create("Picture")
    session.place("iec.operators.pb_no", ref="S1", x=5, y=5, snap=False)
    session.add_sheet("other", title="Other")
    session.set_active_sheet("main")
    view = SchematicView(session)
    try:
        comp = session.sheet("main").component("S1")
        comp.x, comp.y = 82.5, 235
        session.set_active_sheet("other")
        view.reload()
        session.set_active_sheet("main")
        view.reload()
        shown = next(item for item in view.scene.items() if isinstance(item, ComponentItem) and item.component_id == "S1")
        assert shown.pos().x() == pytest.approx(82.5)
        assert shown.pos().y() == pytest.approx(235)

        shown.setPos(90, 225)
        comp.x, comp.y = 82.5, 235
        session.set_active_sheet("other")
        view.reload()
        session.set_active_sheet("main")
        view.reload()
        shown = next(item for item in view.scene.items() if isinstance(item, ComponentItem) and item.component_id == "S1")
        assert (shown.pos().x(), shown.pos().y()) == pytest.approx((82.5, 235))
        assert (comp.x, comp.y) == pytest.approx((82.5, 235))
    finally:
        view.close()


def test_deleted_sheet_drops_its_scene(monkeypatch: pytest.MonkeyPatch):
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import SchematicView

    monkeypatch.setenv("PANELCAD_SCENE_SLOTS", "8")
    QApplication.instance() or QApplication([])
    session = Session.create("Drop sheet")
    session.add_sheet("other", title="Other")
    session.set_active_sheet("main")
    view = SchematicView(session)
    try:
        session.set_active_sheet("other")
        view.reload()
        session.set_active_sheet("main")
        view.reload()
        assert "other" in view._sheet_scenes
        session.delete_sheet("other")
        view.reload()
        assert "other" not in view._sheet_scenes
        assert all(slot.scene is not None for slot in view._sheet_scenes.values())
    finally:
        view.close()


def test_unreadable_ram_uses_the_middle_tier(monkeypatch: pytest.MonkeyPatch):
    import panelcad.memory as memory

    monkeypatch.delenv("PANELCAD_RAM_BYTES", raising=False)
    monkeypatch.delenv("PANELCAD_SCENE_SLOTS", raising=False)
    memory._RAM_BYTES = None
    monkeypatch.setattr("panelcad.memory._ram_linux", lambda: None)
    monkeypatch.setattr("panelcad.memory._ram_windows", lambda: None)
    monkeypatch.setattr("panelcad.memory._ram_macos", lambda: None)
    assert total_ram_bytes() == 12 * 1024**3
    assert scene_cache_slots() == 4
    assert sheet_cache_limit() == 16
    memory._RAM_BYTES = None


def test_detected_ram_is_read_once(monkeypatch: pytest.MonkeyPatch):
    import panelcad.memory as memory

    monkeypatch.delenv("PANELCAD_RAM_BYTES", raising=False)
    memory._RAM_BYTES = None
    calls = {"n": 0}

    def reader() -> int:
        calls["n"] += 1
        return 24 * 1024**3

    monkeypatch.setattr(memory, "_ram_linux", reader)
    monkeypatch.setattr(memory, "_ram_windows", lambda: None)
    monkeypatch.setattr(memory, "_ram_macos", lambda: None)
    assert memory.sheet_cache_limit() == 24
    assert memory.scene_cache_slots() == 8
    assert memory.pixmap_cache_bytes() == 128 * 1024 * 1024
    assert calls["n"] == 1
    memory._RAM_BYTES = None


def test_cached_scene_release_after_a_sheet_switch_follows_the_model(monkeypatch: pytest.MonkeyPatch):
    """A 256-scene pool must not keep a mid-drag picture after the button comes up."""

    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import ComponentItem
    from panelcad.gui.main_window import MainWindow

    monkeypatch.setenv("PANELCAD_RAM_BYTES", str(64 * 1024**3))
    monkeypatch.delenv("PANELCAD_SCENE_SLOTS", raising=False)
    app = QApplication.instance() or QApplication([])
    session = Session.open("examples/dol_starter.pcad.yaml")
    window = MainWindow(session)
    window.show()
    app.processEvents()
    try:
        canvas = window.canvas
        home = session.active_sheet_id
        comp = session.sheet().component("K1")
        comp.x, comp.y = 82.5, 235.0
        canvas.reload()
        item = next(i for i in canvas.scene.items() if isinstance(i, ComponentItem) and i.component_id == "K1")
        assert (item.pos().x(), item.pos().y()) == pytest.approx((82.5, 235.0))

        view = QPointF(canvas.mapFromScene(item.sceneBoundingRect().center()))
        press = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            view,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        canvas.mousePressEvent(press)
        # Five snap steps. The picture moves and the model does not, which is
        # the stale frame the 256-scene pool was parking.
        item.setFlag(item.GraphicsItemFlag.ItemSendsGeometryChanges, False)
        for step in range(1, 6):
            item.setPos(82.5 + 1.5 * step, 235.0 - 2.0 * step)
        item.setFlag(item.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        canvas._moved_since_press = True
        canvas._dragged_ids.add("K1")
        assert (item.pos().x(), item.pos().y()) == pytest.approx((90.0, 225.0))
        assert (comp.x, comp.y) == pytest.approx((82.5, 235.0))

        other = next(sheet.id for sheet in session.project.sheets if sheet.id != home)
        window._switch_sheet(other)
        app.processEvents()
        window._switch_sheet(home)
        app.processEvents()
        shown = next(i for i in canvas.scene.items() if isinstance(i, ComponentItem) and i.component_id == "K1")
        # The button is still down. Coming back must already show the model,
        # and the release must not slide the parked symbol away from it.
        assert (shown.pos().x(), shown.pos().y()) == pytest.approx((comp.x, comp.y))
        release = QMouseEvent(
            QMouseEvent.Type.MouseButtonRelease,
            QPointF(canvas.mapFromScene(shown.sceneBoundingRect().center())),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        canvas.mouseReleaseEvent(release)
        app.processEvents()
        shown = next(i for i in canvas.scene.items() if isinstance(i, ComponentItem) and i.component_id == "K1")
        assert (comp.x, comp.y) == pytest.approx((82.5, 235.0))
        assert (shown.pos().x(), shown.pos().y()) == pytest.approx((82.5, 235.0))
    finally:
        window.close()
