"""Scene positions must not clobber a command, and a tag is not a position.

A move that arrives through ``panelcad send`` updates the model while the open
page still shows the old picture. Save, the dirty snapshot, and a sheet switch
all copy that picture back. The copy has to refresh from the model.

The same tag on two pages is two placements. The power-page contactor and the
control-page coil can both be M1, and each keeps the position it was given.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from panelcad.api.activity import send
from panelcad.api.session import Session


def _app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _device(canvas, ref: str):
    from panelcad.gui.canvas import ComponentItem

    return next(
        item
        for item in canvas.scene.items()
        if isinstance(item, ComponentItem) and item.component_id == ref
    )


def _two_pages(tmp_path: Path) -> tuple[Session, Path]:
    session = Session.create("Two pages")
    session.place("iec.contactors.ac3_9a", ref="M1", x=40, y=40)
    session.place("iec.operators.pb_no", ref="PB1", x=20, y=20)
    session.add_sheet("control", title="Control")
    session.place("iec.contactors.coil", ref="M1", x=80, y=60, sheet_id="control")
    session.place("iec.operators.pb_no", ref="S1", x=30, y=30, sheet_id="control")
    session.set_active_sheet("main")
    path = tmp_path / "pages.pcad.yaml"
    session.save(path)
    return session, path


def _window(session: Session):
    from panelcad.gui.main_window import MainWindow

    app = _app()
    window = MainWindow(session)
    window._ai_timer.stop()
    window.resize(1400, 900)
    window.show()
    app.processEvents()
    return app, window


def _xy(session: Session, sheet_id: str, ref: str) -> tuple[float, float]:
    comp = session.sheet(sheet_id).component(ref)
    return (comp.x, comp.y)


def test_command_move_on_the_open_page_survives_sync_save_and_switch(tmp_path: Path) -> None:
    session, path = _two_pages(tmp_path)
    app, window = _window(session)
    try:
        assert _device(window.canvas, "M1").sheet_id == "main"
        assert _device(window.canvas, "M1").pos().x() == pytest.approx(40)

        # Off-grid on purpose: a refresh must not snap the command onto the drawing grid.
        session.move("M1", 141.2, 91.3, sheet_id="main")
        session.move("PB1", 70, 25, sheet_id="main")
        session.move("M1", 180, 70, sheet_id="control")
        session.move("S1", 55, 110, sheet_id="control")

        window.canvas.sync_positions()
        window._remember_saved_project()
        assert _xy(session, "main", "M1") == pytest.approx((141.2, 91.3))
        assert _xy(session, "main", "PB1") == pytest.approx((70, 25))
        assert _xy(session, "control", "M1") == pytest.approx((180, 70))
        assert _xy(session, "control", "S1") == pytest.approx((55, 110))
        shown = _device(window.canvas, "M1")
        assert shown.sheet_id == "main"
        assert shown.pos().x() == pytest.approx(141.2)
        assert shown.pos().y() == pytest.approx(91.3)

        assert window.save_project() is True
        window._switch_sheet("control")
        app.processEvents()
        control = _device(window.canvas, "M1")
        assert control.sheet_id == "control"
        assert control.pos().x() == pytest.approx(180)
        assert control.pos().y() == pytest.approx(70)
        assert _device(window.canvas, "S1").pos().x() == pytest.approx(55)

        window._switch_sheet("main")
        app.processEvents()
        assert _xy(session, "main", "M1") == pytest.approx((141.2, 91.3))
        assert _xy(session, "control", "M1") == pytest.approx((180, 70))
        assert _device(window.canvas, "M1").pos().x() == pytest.approx(141.2)

        opened = Session.open(path)
        assert _xy(opened, "main", "M1") == pytest.approx((141.2, 91.3))
        assert _xy(opened, "main", "PB1") == pytest.approx((70, 25))
        assert _xy(opened, "control", "M1") == pytest.approx((180, 70))
        assert _xy(opened, "control", "S1") == pytest.approx((55, 110))
    finally:
        window.close()
        app.processEvents()


def test_sync_writes_a_drag_onto_that_page_only(tmp_path: Path) -> None:
    session, _path = _two_pages(tmp_path)
    app, window = _window(session)
    try:
        # The model tab can change before the picture does. The picture still
        # belongs to main, so it must not be stored as control's M1.
        session.set_active_sheet("control")
        window.canvas.sync_positions()
        assert _xy(session, "main", "M1") == pytest.approx((40, 40))
        assert _xy(session, "control", "M1") == pytest.approx((80, 60))

        item = _device(window.canvas, "M1")
        assert item.sheet_id == "main"
        window.canvas._begin_gesture("move")
        window.canvas._moved_since_press = True
        window.canvas._dragged_ids.add("M1")
        item.setPos(200, 120)
        window.canvas.sync_positions()
        window.canvas._finish_gesture(moved=True)
        assert _xy(session, "main", "M1") == pytest.approx((200, 120))
        assert _xy(session, "control", "M1") == pytest.approx((80, 60))
        assert _xy(session, "control", "S1") == pytest.approx((30, 30))
    finally:
        window.close()
        app.processEvents()


def test_inbox_move_refreshes_the_open_page_and_keeps_the_other(tmp_path: Path) -> None:
    session, path = _two_pages(tmp_path)
    app, window = _window(session)
    try:
        send(path, {"op": "move", "ref": "M1", "x": 141.2, "y": 91.3, "sheet": "main"})
        send(path, {"op": "move", "ref": "PB1", "x": 70, "y": 25, "sheet": "main"})
        send(path, {"op": "move", "ref": "M1", "x": 180, "y": 70, "sheet": "control"})
        send(path, {"op": "move", "ref": "S1", "x": 55, "y": 110, "sheet": "control"})
        window._poll_ai()
        app.processEvents()

        assert _xy(session, "main", "M1") == pytest.approx((141.2, 91.3))
        assert _xy(session, "main", "PB1") == pytest.approx((70, 25))
        assert _xy(session, "control", "M1") == pytest.approx((180, 70))
        assert _xy(session, "control", "S1") == pytest.approx((55, 110))
        shown = _device(window.canvas, "M1")
        assert shown.sheet_id == "main"
        assert (shown.pos().x(), shown.pos().y()) == pytest.approx((141.2, 91.3))

        assert window.save_project() is True
        window._switch_sheet("control")
        app.processEvents()
        window._switch_sheet("main")
        app.processEvents()
        assert _xy(session, "main", "M1") == pytest.approx((141.2, 91.3))
        assert _xy(session, "control", "M1") == pytest.approx((180, 70))
        assert _device(window.canvas, "M1").pos().y() == pytest.approx(91.3)

        opened = Session.open(path)
        assert _xy(opened, "main", "M1") == pytest.approx((141.2, 91.3))
        assert _xy(opened, "control", "M1") == pytest.approx((180, 70))
        assert _xy(opened, "control", "S1") == pytest.approx((55, 110))
    finally:
        window.close()
        app.processEvents()
