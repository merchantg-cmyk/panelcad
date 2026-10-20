"""A drag must drop stored waypoints on release and when the grab is lost."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFocusEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from panelcad.api.session import Session
from panelcad.core.geometry import Point
from panelcad.gui.canvas import ComponentItem, WireItem
from panelcad.render.sheet import pin_world
from panelcad.render.wiring import route_sheet_wires

_ROOT = Path(__file__).resolve().parents[1]
_STALE = (121.0, 68.0)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _control() -> tuple[Session, object]:
    session = Session.open(_ROOT / "examples" / "dol_starter.pcad.yaml")
    session.set_active_sheet("control")
    sheet = session.sheet("control")
    touched = [
        wire
        for wire in sheet.wires
        if str(wire.frm).split(".", 1)[0] == "G1" or str(wire.to).split(".", 1)[0] == "G1"
    ]
    assert any(wire.id == "w16" for wire in touched)
    for wire in touched:
        wire.points = [
            Point(x=40, y=40),
            Point(x=70, y=52),
            Point(x=90, y=58),
            Point(x=110, y=64),
            Point(x=_STALE[0], y=_STALE[1]),
        ]
    return session, touched


def _g1_item(view):
    return next(i for i in view._items if isinstance(i, ComponentItem) and i.component_id == "G1")


def _press_and_move(view, dx: int = 36):
    item = _g1_item(view)
    center = view.mapFromScene(item.sceneBoundingRect().center())
    start = QPoint(int(center.x()), int(center.y()))
    end = QPoint(start.x() + dx, start.y() + 8)
    vp = view.viewport()
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start, 0)
    QTest.mouseMove(vp, end, 0)
    return end


def _w16_follows_pin(session: Session) -> None:
    sheet = session.sheet("control")
    wire = session.find_wire(wire_id="w16", sheet_id="control")
    assert wire.points == []
    comp = sheet.component("G1")
    device = session.catalog.get(comp.library_id)
    pin = next(p for p in device.pins if p.id == "minus")
    px, py = pin_world(comp, pin)
    route = route_sheet_wires(sheet, session.catalog, project=session.project)["w16"]
    end = route[-1]
    assert abs(end.x - px) < 1.0 and abs(end.y - py) < 1.0
    assert abs(end.x - _STALE[0]) > 2 or abs(end.y - _STALE[1]) > 2


def _preview_meets_pin(view, session: Session) -> None:
    sheet = session.sheet("control")
    comp = sheet.component("G1")
    device = session.catalog.get(comp.library_id)
    pin = next(p for p in device.pins if p.id == "minus")
    px, py = pin_world(comp, pin)
    item = next(i for i in view._items if isinstance(i, WireItem) and i.wire_id == "w16")
    path = item.path()
    last = path.elementAt(path.elementCount() - 1)
    assert abs(last.x - px) < 1.0 and abs(last.y - py) < 1.0
    assert abs(last.x - _STALE[0]) > 2 or abs(last.y - _STALE[1]) > 2


def test_release_drops_stored_waypoints_and_reroutes() -> None:
    from panelcad.gui.main_window import MainWindow

    app = _app()
    session, touched = _control()
    window = MainWindow(session)
    window.show()
    app.processEvents()
    view = window.canvas
    end = _press_and_move(view)
    assert session.sheet("control").component("G1").x > 40
    _preview_meets_pin(view, session)
    # Waypoints stay until release. The preview is only on screen.
    assert session.find_wire(wire_id="w16", sheet_id="control").points[-1].x == _STALE[0]
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end, 0)
    app.processEvents()
    for wire in touched:
        assert wire.points == []
    _w16_follows_pin(session)
    window.close()


def _window(session: Session):
    from panelcad.gui.main_window import MainWindow

    app = _app()
    window = MainWindow(session)
    window.show()
    app.processEvents()
    return app, window


def _drag_device(view, comp_id: str, dx: int = 36, dy: int = 14):
    item = next(i for i in view._items if isinstance(i, ComponentItem) and i.component_id == comp_id)
    center = view.mapFromScene(item.sceneBoundingRect().center())
    start = QPoint(int(center.x()), int(center.y()))
    end = QPoint(start.x() + dx, start.y() + dy)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start, 0)
    QTest.mouseMove(view.viewport(), end, 0)
    return end


def _preview_matches_ends(view, session: Session, wire_id: str) -> None:
    from panelcad.render.wiring import wire_ends

    sheet = session.sheet()
    wire = session.find_wire(wire_id=wire_id)
    ax, ay, _a_side, bx, by, _b_side = wire_ends(sheet, session.catalog, wire, session.project)
    item = next(i for i in view._items if isinstance(i, WireItem) and i.wire_id == wire_id)
    path = item.path()
    assert path.elementCount() == 2
    start = path.elementAt(0)
    end = path.elementAt(1)
    assert abs(start.x - ax) < 1.0 and abs(start.y - ay) < 1.0
    assert abs(end.x - bx) < 1.0 and abs(end.y - by) < 1.0


def test_drag_preview_keeps_a_ladder_rail_on_its_landing() -> None:
    """A rung stays on the rail landing while the device moves, not the rail midpoint."""

    app, window = _window(_ladder_with_rail())
    session = window.session
    view = window.canvas
    _drag_device(view, "PB1")
    app.processEvents()
    assert session.sheet().component("PB1").x != 90
    _preview_matches_ends(view, session, "w1")
    window.close()


def test_drag_preview_keeps_a_clicked_rail_landing() -> None:
    session = _ladder_with_rail()
    wire = session.find_wire(wire_id="w1")
    assert wire.frm == "L1" or wire.to == "L1"
    if wire.frm == "L1":
        wire.from_at = Point(x=20, y=118)
    else:
        wire.to_at = Point(x=20, y=118)
    app, window = _window(session)
    view = window.canvas
    _drag_device(view, "PB1", dy=20)
    app.processEvents()
    _preview_matches_ends(view, window.session, "w1")
    window.close()


def test_drag_preview_keeps_an_iec_rail_on_the_horizontal_rail() -> None:
    session = Session.create("IEC columns")
    session.set_title_block(standard="IEC", rails="L+/0V")
    session.sheet().drawing_style = "iec"
    session.place("iec.operators.pb_no", ref="S1", x=90, y=70)
    session.wire("L+", "S1.13")
    # A stored riser from the old column. Release drops it, so the preview
    # has to follow the pin, not that X.
    wire = session.sheet().wires[0]
    wire.points = [Point(x=40, y=30), Point(x=40, y=70)]
    app, window = _window(session)
    view = window.canvas
    _drag_device(view, "S1", dx=28, dy=18)
    app.processEvents()
    sheet = window.session.sheet()
    live = window.session.find_wire(wire_id=wire.id)
    assert live.points and live.points[0].x == 40
    saved = list(live.points)
    live.points = []
    try:
        from panelcad.render.wiring import wire_ends

        ax, ay, _a_side, bx, by, _b_side = wire_ends(
            sheet, window.session.catalog, live, window.session.project
        )
    finally:
        live.points = saved
    item = next(i for i in view._items if isinstance(i, WireItem) and i.wire_id == wire.id)
    path = item.path()
    start = path.elementAt(0)
    end = path.elementAt(path.elementCount() - 1)
    assert abs(start.x - 40) > 5
    assert abs(start.x - ax) < 1.0 and abs(start.y - ay) < 1.0
    assert abs(end.x - bx) < 1.0 and abs(end.y - by) < 1.0
    window.close()


def _ladder_with_rail() -> Session:
    session = Session.create("Ladder")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_nc", ref="PB1", x=90, y=70)
    made = session.wire("L1", "PB1.1")
    made.id = "w1"
    return session


def test_lost_release_drops_stored_waypoints_and_reroutes() -> None:
    from PySide6.QtCore import QEvent

    from panelcad.gui.main_window import MainWindow

    app = _app()
    session, touched = _control()
    window = MainWindow(session)
    window.show()
    app.processEvents()
    view = window.canvas
    _press_and_move(view)
    assert session.command_open()
    _preview_meets_pin(view, session)
    view.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
    app.processEvents()
    assert not session.command_open()
    for wire in touched:
        assert wire.points == []
    _w16_follows_pin(session)
    window.close()
