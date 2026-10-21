"""The toolbar Undo turns on after a drag, including after sheet switches."""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

from panelcad import Session
from panelcad.gui.main_window import MainWindow

from tests.test_offgrid_click import _drag, _item

_PART = "iec.operators.pb_no"


def _window_with_sheets():
    app = QApplication.instance() or QApplication([])
    session = Session.create("UndoToolbar")
    first = session.active_sheet_id
    session.place(_PART, ref="A1", x=40, y=40)
    session.add_sheet("second", make_active=False)
    session.add_sheet("third", make_active=False)
    session.set_active_sheet("second")
    session.place(_PART, ref="B1", x=60, y=60)
    session.set_active_sheet(first)
    # A project opened from disk starts with an empty undo list.
    session._undo.clear()
    session._undo_tip = None
    window = MainWindow(session)
    window.resize(1400, 1000)
    window.show()
    app.processEvents()
    return app, session, window, first


def _pos(session: Session, ref: str) -> tuple[float, float]:
    comp = session.sheet().component(ref)
    return (comp.x, comp.y)


def test_toolbar_undo_reverts_drags_after_sheet_switches():
    app, session, window, first = _window_with_sheets()
    try:
        undo = window._undo_action
        assert not undo.isEnabled()
        for sid in ("second", "third", first, "second"):
            window._switch_sheet(sid)
            app.processEvents()
        assert session.active_sheet_id == "second"
        start = _pos(session, "B1")
        for _ in range(3):
            _drag(window.canvas, _item(window.canvas, "B1"), 20, 0)
        assert _pos(session, "B1") != start
        assert session.undo_command_names() == ["move", "move", "move"]

        button = window.toolbar.widgetForAction(undo)
        assert button is not None and button.defaultAction() is undo
        assert undo.isEnabled() and button.isEnabled()
        for _ in range(3):
            button.click()
            app.processEvents()
        assert _pos(session, "B1") == start
        assert not undo.isEnabled()
        assert window._redo_action.isEnabled()
        item = _item(window.canvas, "B1")
        assert (item.pos().x(), item.pos().y()) == start
    finally:
        window.close()


def test_history_watcher_fires_for_push_undo_and_redo():
    session = Session.create("Watch")
    calls: list[int] = []
    session.watch_history(lambda: calls.append(1))
    session.watch_history(session.can_undo)  # a repeat-safe second watcher
    session.place(_PART, ref="S1", x=40, y=40)
    pushed = len(calls)
    assert pushed >= 1
    assert session.undo()
    assert len(calls) == pushed + 1
    assert session.redo()
    assert len(calls) == pushed + 2


def test_history_watcher_does_not_keep_owner_alive():
    import gc
    import weakref

    class Owner:
        def __init__(self) -> None:
            self.calls = 0

        def tick(self) -> None:
            self.calls += 1

    session = Session.create("WeakWatch")
    owner = Owner()
    session.watch_history(owner.tick)
    session.watch_history(owner.tick)  # repeat is ignored
    session.place(_PART, ref="S1", x=40, y=40)
    assert owner.calls == 1
    probe = weakref.ref(owner)
    del owner
    gc.collect()
    assert probe() is None
    session.place(_PART, ref="S2", x=80, y=40)  # dead watcher is dropped quietly
    assert session._history_watchers == []
