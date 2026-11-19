"""GUI edit paths must push undo steps, including after reopen and cancelled gestures."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from panelcad import Session
from panelcad.gui.canvas import ComponentItem
from panelcad.gui.main_window import MainWindow

from tests.test_offgrid_click import _drag, _item

_PART = "iec.operators.pb_no"
_COIL = "iec.contactors.coil"


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _dump(session: Session) -> dict:
    return session.project.model_dump(mode="python")


def _window(session: Session) -> MainWindow:
    window = MainWindow(session)
    window.resize(1400, 1000)
    window.show()
    _app().processEvents()
    return window


def _left(view: QPointF, kind: QMouseEvent.Type) -> QMouseEvent:
    button = Qt.MouseButton.LeftButton
    buttons = button if kind == QMouseEvent.Type.MouseButtonPress else Qt.MouseButton.NoButton
    return QMouseEvent(kind, view, button, buttons, Qt.KeyboardModifier.NoModifier)


def test_sheet_switch_mid_drag_does_not_swallow_later_edits():
    """PgUp/PgDown used to leave the move command open; later places pushed nothing."""

    app = _app()
    session = Session.create("SheetSwitch")
    session.place(_PART, ref="S1", x=40, y=40)
    session.add_sheet("second", make_active=False)
    session._undo.clear()
    session._undo_tip = None
    window = _window(session)
    try:
        canvas = window.canvas
        item = _item(canvas, "S1")
        view = QPointF(canvas.mapFromScene(item.sceneBoundingRect().center()))
        canvas.mousePressEvent(_left(view, QMouseEvent.Type.MouseButtonPress))
        assert session.command_open()
        assert canvas._gesture_edit
        window._switch_sheet("second")
        app.processEvents()
        assert session._history_lock == 0
        assert not session.command_open()
        assert not canvas._gesture_edit
        depth = len(session._undo)
        session.place(_PART, ref="S2", x=80, y=40)
        assert len(session._undo) == depth + 1
        assert session.undo_command_names()[-1] == "place"
        assert session.can_undo()
        before = _dump(session)
        window.canvas.place_at(_COIL, 120, 40)
        app.processEvents()
        assert len(session._undo) == depth + 2
        assert session.undo()
        assert _dump(session) == before
    finally:
        window.close()


def test_escape_mid_drag_does_not_swallow_later_edits():
    app = _app()
    session = Session.create("Escape")
    session.place(_PART, ref="S1", x=40, y=40)
    session._undo.clear()
    session._undo_tip = None
    window = _window(session)
    try:
        canvas = window.canvas
        item = _item(canvas, "S1")
        view = QPointF(canvas.mapFromScene(item.sceneBoundingRect().center()))
        canvas.mousePressEvent(_left(view, QMouseEvent.Type.MouseButtonPress))
        assert session.command_open()
        canvas.keyPressEvent(
            QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        )
        app.processEvents()
        assert session._history_lock == 0
        assert not session.command_open()
        assert not canvas._gesture_edit
        depth = len(session._undo)
        session.set_title_block(company="Shop")
        assert len(session._undo) == depth + 1
        assert session.undo_command_names()[-1] == "set_title_block"
    finally:
        window.close()


def test_gui_edit_types_undo_and_redo_after_reopen(tmp_path: Path):
    """Reopen starts clean; place / move / wire / label / rotate / delete each push one step."""

    app = _app()
    path = tmp_path / "reopen.pcad.yaml"
    seed = Session.create("Reopen")
    seed.place(_PART, ref="S1", x=40, y=40)
    seed.place(_COIL, ref="K1", x=120, y=40)
    seed.save(path)

    session = Session.open(path)
    assert session._undo == []
    assert session._redo == []
    window = _window(session)
    try:
        canvas = window.canvas

        def undo_gui() -> None:
            window.undo_edit()
            app.processEvents()

        def redo_gui() -> None:
            window.redo_edit()
            app.processEvents()

        before = _dump(session)
        depth = len(session._undo)
        canvas.place_at(_PART, 200, 40)
        app.processEvents()
        assert len(session._undo) == depth + 1
        assert session.undo_command_names()[-1] == "place"
        undo_gui()
        assert _dump(session) == before
        assert not session.can_undo()
        assert session.can_redo()
        redo_gui()
        undo_gui()  # leave the reopened drawing with a clean stack again
        assert session._undo == []

        start = (session.sheet().component("S1").x, session.sheet().component("S1").y)
        depth = len(session._undo)
        _drag(canvas, _item(canvas, "S1"), 20, 0)
        assert (session.sheet().component("S1").x, session.sheet().component("S1").y) != start
        assert len(session._undo) == depth + 1
        assert session.undo_command_names()[-1] == "move"
        undo_gui()
        assert (session.sheet().component("S1").x, session.sheet().component("S1").y) == start

        depth = len(session._undo)
        wire_count = len(session.sheet().wires)
        session.wire("S1.14", "K1.A1")
        canvas._redraw_wires()
        assert len(session.sheet().wires) == wire_count + 1
        assert len(session._undo) == depth + 1
        assert session.undo_command_names()[-1] == "wire"
        wid = session.sheet().wires[-1].id
        undo_gui()
        assert len(session.sheet().wires) == wire_count
        redo_gui()

        depth = len(session._undo)
        label_before = session.sheet().wires[0].label
        session.set_wire_label(wid, label="run")
        assert len(session._undo) == depth + 1
        undo_gui()
        assert session.sheet().wires[0].label == label_before
        redo_gui()

        depth = len(session._undo)
        canvas.select_ref("K1")
        rot = session.sheet().component("K1").rotation
        canvas.rotate_selected()
        app.processEvents()
        assert session.sheet().component("K1").rotation != rot
        assert len(session._undo) == depth + 1
        assert session.undo_command_names()[-1] == "rotate"
        undo_gui()
        assert session.sheet().component("K1").rotation == rot
        redo_gui()

        depth = len(session._undo)
        ids = {c.id for c in session.sheet().components}
        canvas.select_ref("S1")
        canvas._delete_selected()
        app.processEvents()
        assert "S1" not in {c.id for c in session.sheet().components}
        assert len(session._undo) == depth + 1
        undo_gui()
        assert {c.id for c in session.sheet().components} == ids
        assert session.can_undo()
    finally:
        window.close()


def test_scale_commit_drops_routes_inside_one_undo_step():
    from panelcad.core.models import Point

    app = _app()
    session = Session.create("Scale")
    session.place(_PART, ref="S1", x=40, y=40)
    session.place(_COIL, ref="K1", x=120, y=40)
    session.wire("S1.14", "K1.A1")
    wire = session.sheet().wires[0]
    wire.points = [Point(x=80.0, y=20.0), Point(x=100.0, y=20.0)]
    session._undo.clear()
    session._undo_tip = None
    window = _window(session)
    try:
        item = _item(window.canvas, "S1")
        before_points = [(p.x, p.y) for p in session.sheet().wires[0].points]
        before_scale = session.sheet().component("S1").scale
        item.apply_scale(1.5)
        assert [(p.x, p.y) for p in session.sheet().wires[0].points] == before_points
        assert session._undo == []
        item.commit_scale()
        assert session.sheet().component("S1").scale == 1.5
        assert session.sheet().wires[0].points == []
        assert len(session._undo) == 1
        assert session.undo()
        assert session.sheet().component("S1").scale == before_scale
        assert [(p.x, p.y) for p in session.sheet().wires[0].points] == before_points
    finally:
        window.close()


def test_disk_reload_starts_clean_and_records_new_edits(tmp_path: Path):
    app = _app()
    path = tmp_path / "poll.pcad.yaml"
    session = Session.create("Poll")
    session.place(_PART, ref="S1", x=40, y=40)
    session.save(path)
    window = _window(session)
    try:
        session.place(_COIL, ref="K1", x=120, y=40)
        assert session.can_undo()
        session.save(path)
        window._remember_saved_project()
        window._note_yaml_mtime()
        # External writer replaces the file while the buffer matches the save.
        other = Session.open(path)
        other.place(_PART, ref="S9", x=200, y=40)
        other.save(path)
        import os

        os.utime(path, (window._yaml_mtime + 2, window._yaml_mtime + 2))
        window._poll_yaml()
        app.processEvents()
        assert window.session is not session
        assert window.session._undo == []
        assert {c.id for c in window.session.sheet().components} == {"S1", "K1", "S9"}
        depth = len(window.session._undo)
        window.session.set_title_block(company="Reloaded")
        assert len(window.session._undo) == depth + 1
        assert window.session.undo()
        assert not window.session.project.title_block.company
    finally:
        window.close()
