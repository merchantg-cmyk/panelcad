"""One user action is one undo step, and a no-op is not a step."""

from __future__ import annotations

from pathlib import Path

import pytest

from panelcad import Session
from panelcad.diag.report import ReportRequest, create_bug_report, recent_actions_text


def _dump(session: Session) -> dict:
    return session.project.model_dump(mode="python")


def _one_step(session: Session, before: dict, action) -> None:
    depth = len(session._undo)
    action()
    assert _dump(session) != before
    assert len(session._undo) == depth + 1
    assert session.undo()
    assert _dump(session) == before
    assert session.redo()
    assert _dump(session) != before


def test_representative_edits_undo_and_redo_in_one_step():
    session = Session.create("Steps")
    session.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    session.place("iec.contactors.coil", ref="K1", x=120, y=40)

    before = _dump(session)
    _one_step(session, before, lambda: session.place("iec.operators.pb_nc", ref="S2", x=80, y=40))

    before = _dump(session)
    _one_step(session, before, lambda: session.wire("S1.14", "K1.A1"))

    before = _dump(session)
    _one_step(
        session,
        before,
        lambda: session.set_wire_spec(session.sheet().wires[0].id, number="101", color="red", gauge="14"),
    )

    before = _dump(session)
    _one_step(session, before, lambda: session.set_wire_label(session.sheet().wires[0].id, label="run"))

    before = _dump(session)
    _one_step(session, before, lambda: session.rename_component("S2", "S2A"))

    before = _dump(session)
    _one_step(
        session,
        before,
        lambda: session.set_component_properties("K1", {"notes": "coil in the bucket"}),
    )

    before = _dump(session)
    _one_step(session, before, lambda: session.set_address("K1", ip="10.0.0.8", node="4"))

    before = _dump(session)
    _one_step(
        session,
        before,
        lambda: session.assign_part("K1", "100-C09D10", manufacturer="Allen-Bradley"),
    )

    before = _dump(session)
    _one_step(session, before, lambda: session.set_title_block(company="Acme Controls"))

    before = _dump(session)
    _one_step(session, before, lambda: session.add_sheet("enclosure", title="Enclosure", kind="panel_layout"))
    session.set_active_sheet("enclosure")
    before = _dump(session)
    _one_step(session, before, lambda: session.add_rail("din1", 40, 80, 300, sheet_id="enclosure"))

    session.set_active_sheet("main")
    before = _dump(session)
    _one_step(session, before, lambda: session.apply_enclosure(sheet_id="enclosure", place_parts=False))


def test_noop_and_view_changes_do_not_push_undo():
    session = Session.create("Noop")
    session.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    session.set_title_block(company="Acme")
    session.set_scale("S1", 1.25)
    depth = len(session._undo)
    session.set_title_block(company="Acme")
    session.set_scale("S1", 1.25)
    session.set_component_properties("S1", {})
    session.set_active_sheet("main")
    session.recommend_cabinet(apply=False)
    assert len(session._undo) == depth
    assert session.undo_command_names()[-1] == "set_scale"


def test_nested_duplicate_and_offpage_are_one_step():
    session = Session.create("Nested")
    session.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    before = _dump(session)
    depth = len(session._undo)
    copy = session.duplicate_component("S1")
    assert copy.id != "S1"
    assert len(session._undo) == depth + 1
    assert session.undo_command_names()[-1] == "duplicate_component"
    assert session.undo()
    assert _dump(session) == before

    session.add_sheet("next", title="Next", kind="schematic")
    session.place("iec.contactors.coil", ref="K1", x=40, y=40, sheet_id="next")
    session.set_active_sheet("main")
    before = _dump(session)
    depth = len(session._undo)
    session.wire_offpage("S1.14", "K1.A1", to_sheet="next")
    assert len(session._undo) == depth + 1
    assert session.undo()
    assert _dump(session) == before
    assert session.redo()
    assert session.sheet("next").components


def test_import_print_replaces_the_project_in_one_step(monkeypatch: pytest.MonkeyPatch):
    session = Session.create("Print")
    session.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    before = _dump(session)
    depth = len(session._undo)
    built = Session.create("From print")
    built.place("iec.operators.pb_nc", ref="S9", x=12, y=12)

    class Job:
        @staticmethod
        def start(*_args, **_kwargs):
            return Job()

        def build(self, out=None):
            return type("Built", (), {"project": built.project, "catalog": built.catalog, "path": built.path})()

        def status(self):
            return {"ok": True}

    monkeypatch.setattr("panelcad.importer.pipeline.ImportJob", Job)
    session.import_print("page.pdf")
    assert session.project.name == "From print"
    assert len(session._undo) == depth + 1
    assert session.undo_command_names()[-1] == "import_print"
    assert session.undo()
    assert _dump(session) == before
    assert session.project.name == "Print"
    assert session.redo()
    assert session.project.name == "From print"


def test_import_sheet_is_one_step(tmp_path: Path):
    session = Session.create("Sheet")
    sheet = tmp_path / "extra.yaml"
    sheet.write_text(
        "id: extra\ntitle: Extra\nkind: schematic\npage: {width: 420, height: 297}\n",
        encoding="utf-8",
    )
    before = _dump(session)
    depth = len(session._undo)
    assert session.import_sheet(sheet) == "extra"
    assert len(session._undo) == depth + 1
    assert session.undo()
    assert _dump(session) == before
    assert session.redo()
    assert session.project.sheet("extra").title == "Extra"


def test_recent_actions_include_undo_command_names():
    session = Session.create("Names")
    session.place("iec.operators.pb_no", ref="S1", x=10, y=10)
    session.set_title_block(company="Acme")
    text = recent_actions_text(session)
    assert "undo steps: 2" in text
    assert "undo commands: place, set_title_block" in text
    assert "set_title_block" in text


def test_bug_report_requires_a_description():
    with pytest.raises(ValueError, match="description is required"):
        create_bug_report(ReportRequest(title="Only a title", description="  "))
    session = Session.create("Report")
    with pytest.raises(ValueError, match="description is required"):
        session.report_bug("Only a title", "   ")


def test_bug_report_dialog_blocks_an_empty_description():
    from PySide6.QtWidgets import QApplication, QMessageBox

    from panelcad.gui.bug_report_dialog import BugReportDialog

    app = QApplication.instance() or QApplication([])
    session = Session.create("Dialog")
    dialog = BugReportDialog(None, session, title="Coil", what="")
    assert dialog.send_btn.isEnabled() is False
    assert dialog.save_btn.isEnabled() is False
    called = {"n": 0}

    def boom(*_args, **_kwargs):
        called["n"] += 1
        raise AssertionError("empty report was sent")

    dialog._session.report_bug = boom  # type: ignore[method-assign]
    QMessageBox.information = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    dialog._commit(send=True)
    dialog._commit(send=False)
    assert called["n"] == 0
    dialog.what.setPlainText("The coil tag did not move.")
    assert dialog.send_btn.isEnabled() is True
    assert dialog.save_btn.isEnabled() is True
    dialog.close()
    app.processEvents()


def test_gui_click_and_grouped_edits_are_one_step():
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import ComponentItem
    from panelcad.gui.main_window import MainWindow
    from panelcad.gui.object_editor import ObjectEditor

    app = QApplication.instance() or QApplication([])
    session = Session.create("Gui")
    session.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    session.place("iec.operators.pb_nc", ref="S2", x=90, y=40)
    window = MainWindow(session)
    window.show()
    app.processEvents()
    canvas = window.canvas
    depth = len(session._undo)
    item = next(i for i in canvas.scene.items() if isinstance(i, ComponentItem) and i.component_id == "S1")
    view = QPointF(canvas.mapFromScene(item.sceneBoundingRect().center()))

    def event(kind, button, buttons):
        return QMouseEvent(kind, view, button, buttons, Qt.KeyboardModifier.NoModifier)

    canvas.mousePressEvent(
        event(QMouseEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    )
    canvas.mouseReleaseEvent(
        event(QMouseEvent.Type.MouseButtonRelease, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    )
    assert len(session._undo) == depth

    for scene_item in canvas.scene.items():
        if isinstance(scene_item, ComponentItem):
            scene_item.setSelected(True)
    before = _dump(session)
    depth = len(session._undo)
    canvas.rotate_selected()
    assert len(session._undo) == depth + 1
    assert session.undo_command_names()[-1] == "rotate"
    assert session.undo()
    assert _dump(session) == before
    assert session.redo()
    assert {comp.rotation for comp in session.sheet().components} == {90}

    editor = ObjectEditor(session, "S1", window)
    editor.notes.setText("door station")
    editor.ip.setText("10.1.1.5")
    before = _dump(session)
    depth = len(session._undo)
    editor._save()
    assert len(session._undo) == depth + 1
    assert session.undo_command_names()[-1] == "object editor"
    assert session.undo()
    assert _dump(session) == before
    assert session.redo()
    assert session.sheet().component("S1").properties["notes"] == "door station"
    editor.close()
    window.close()


def _left_click(canvas, view, kind):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QMouseEvent

    button = Qt.MouseButton.LeftButton
    buttons = button if kind == QMouseEvent.Type.MouseButtonPress else Qt.MouseButton.NoButton
    return QMouseEvent(kind, view, button, buttons, Qt.KeyboardModifier.NoModifier)


def test_second_press_without_a_release_does_not_leave_the_step_open():
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QFocusEvent, QMouseEvent
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import ComponentItem
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    session = Session.create("Leak")
    session.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    window = MainWindow(session)
    window.show()
    app.processEvents()
    canvas = window.canvas
    item = next(i for i in canvas.scene.items() if isinstance(i, ComponentItem) and i.component_id == "S1")
    view = QPointF(canvas.mapFromScene(item.sceneBoundingRect().center()))
    press = QMouseEvent.Type.MouseButtonPress
    release = QMouseEvent.Type.MouseButtonRelease

    depth = len(session._undo)
    canvas.mousePressEvent(_left_click(canvas, view, press))
    assert session.command_open()
    canvas.mousePressEvent(_left_click(canvas, view, press))
    canvas.mouseReleaseEvent(_left_click(canvas, view, release))
    assert session._history_lock == 0
    assert not session.command_open()
    assert len(session._undo) == depth

    session.place("iec.operators.pb_nc", ref="S2", x=80, y=40)
    assert len(session._undo) == depth + 1
    assert session.undo_command_names()[-1] == "place"
    assert session.undo()
    assert [c.id for c in session.sheet().components] == ["S1"]

    canvas.mousePressEvent(_left_click(canvas, view, press))
    assert session.command_open()
    canvas.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
    assert session._history_lock == 0
    session.set_title_block(company="Acme")
    assert len(session._undo) == depth + 1
    assert session.undo_command_names()[-1] == "set_title_block"
    window.close()


def test_undo_while_the_mouse_is_down_does_not_push_the_drag_back():
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import ComponentItem
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    session = Session.create("Held")
    session.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    window = MainWindow(session)
    window.show()
    app.processEvents()
    canvas = window.canvas
    item = next(i for i in canvas.scene.items() if isinstance(i, ComponentItem) and i.component_id == "S1")
    view = QPointF(canvas.mapFromScene(item.sceneBoundingRect().center()))
    canvas.mousePressEvent(_left_click(canvas, view, QMouseEvent.Type.MouseButtonPress))
    assert session.command_open()
    assert session.undo()
    assert [c.id for c in session.sheet().components] == []
    assert session.can_redo()
    assert not session.command_open()
    canvas.mouseReleaseEvent(_left_click(canvas, view, QMouseEvent.Type.MouseButtonRelease))
    assert [c.id for c in session.sheet().components] == []
    assert session.can_redo()
    assert session._history_lock == 0
    assert session._undo == []
    assert session.redo()
    assert [c.id for c in session.sheet().components] == ["S1"]
    window.close()


def test_place_at_keyerror_and_delete_always_close_the_step():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import ComponentItem
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    session = Session.create("Key")
    session.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    window = MainWindow(session)
    window.show()
    app.processEvents()
    canvas = window.canvas
    depth = len(session._undo)
    with pytest.raises(KeyError):
        canvas.place_at("no.such.device", 20, 20)
    assert session._history_lock == 0
    with pytest.raises(KeyError):
        canvas.place_from_bom_at("no.such.device", None, 20, 20)
    assert session._history_lock == 0
    item = next(i for i in canvas.scene.items() if isinstance(i, ComponentItem) and i.component_id == "S1")
    canvas._middle_hit_device = item
    canvas._middle_hit_wire = None

    def explode(*_args, **_kwargs):
        raise KeyError("gone")

    session.delete_component = explode  # type: ignore[method-assign]
    canvas._delete_middle_hit()
    assert session._history_lock == 0
    assert [c.id for c in session.sheet().components] == ["S1"]
    session.set_title_block(company="Shop")
    assert len(session._undo) == depth + 1
    window.close()


def test_keyboard_interrupt_closes_the_open_step():
    session = Session.create("Interrupt")
    session.place("iec.operators.pb_no", ref="S1", x=10, y=10)

    def boom(_library_id):
        raise KeyboardInterrupt

    session.catalog.get = boom  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        session.place("iec.operators.pb_nc", ref="S2", x=20, y=20)
    assert session._history_lock == 0
    assert not session.command_open()
    assert [c.id for c in session.sheet().components] == ["S1"]


def test_cli_bug_report_empty_description_exits_cleanly():
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    result = CliRunner().invoke(app, ["bug-report", "--title", "Coil", "--description", "   "])
    text = result.stdout + result.stderr
    assert result.exit_code == 1
    assert "description is required" in text
    assert "Traceback" not in text
