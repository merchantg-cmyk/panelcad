"""Wire tool clicks on ladder rails (NEMA L1/L2 and IEC L+/0V).

Clicks go through the offscreen viewport the way a person uses the Wire tool.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _click(canvas, app, x: float, y: float) -> tuple[float, float]:
    """Click the viewport pixel under (x, y). Returns the scene point that pixel is."""

    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest

    canvas.centerOn(x, y)
    app.processEvents()
    pt = canvas.mapFromScene(QPointF(x, y))
    assert canvas.viewport().rect().contains(pt), (pt.x(), pt.y(), canvas.viewport().rect())
    delivered = canvas.mapToScene(pt)
    QTest.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pt)
    app.processEvents()
    return delivered.x(), delivered.y()


def _open(session):
    from panelcad.gui.main_window import MainWindow

    app = _app()
    win = MainWindow(session)
    win.resize(1400, 900)
    win.show()
    app.processEvents()
    win.canvas.fit_page()
    app.processEvents()
    win.canvas.set_wire_mode()
    app.processEvents()
    return app, win


def _pin(canvas, ref: str, pin_id: str):
    from PySide6.QtCore import Qt

    from panelcad.gui.canvas import PinItem

    pin = next(
        item
        for item in canvas.scene.items()
        if isinstance(item, PinItem) and item.component_id == ref and item.pin_id == pin_id
    )
    pin.setZValue(40)
    for other in canvas.scene.items():
        if not isinstance(other, PinItem) or other is pin or other.component_id != ref:
            continue
        if abs(other.scenePos().x() - pin.scenePos().x()) < 0.2 and abs(other.scenePos().y() - pin.scenePos().y()) < 0.2:
            other.setZValue(-1)
            other.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
    return pin


def _click_pin(canvas, app, ref: str, pin_id: str) -> None:
    pin = _pin(canvas, ref, pin_id)
    _click(canvas, app, pin.scenePos().x(), pin.scenePos().y())


def _junctions(canvas) -> list[tuple[float, float]]:
    from panelcad.gui.canvas import RailJunctionItem

    return [(item.rect().center().x(), item.rect().center().y()) for item in canvas.scene.items() if isinstance(item, RailJunctionItem)]


def _near(points, x: float, y: float, tol: float = 1.2) -> bool:
    return any(abs(px - x) <= tol and abs(py - y) <= tol for px, py in points)


def test_wires_help_mentions_rails() -> None:
    from panelcad.guidance import help_topic

    topic = help_topic("wires")
    body = topic["body"]
    assert topic["keywords"] == "wire number wire numbers"
    assert "rail" in body.lower()
    assert "L1" in body and "L2" in body
    assert "L+" in body and "0V" in body
    assert "junction" in body.lower()
    assert "The Inspector sets the scheme." in body
    assert "assigned from the drawing prefs" not in body
    assert "Import wires" in body and "reads a CSV with From and To columns." in body


def test_gui_click_connects_nema_ladder_rail(tmp_path: Path) -> None:
    from panelcad.api.session import Session
    from panelcad.core.ladder import rail_geometry
    from panelcad.render.wiring import route_sheet_wires

    app = _app()
    session = Session.create("Ladder")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_nc", ref="PB1", x=90, y=70)
    session.place("nema.power.control_transformer", ref="T1", x=150, y=70)
    session.place("nema.contactors.coil", ref="M1", x=230, y=70)
    _app_win = _open(session)
    app, win = _app_win
    canvas = win.canvas
    geom = rail_geometry(session.sheet(), session.project)
    assert geom is not None and geom.kind == "ladder"
    assert geom.hot == "L1" and geom.ret == "L2"

    pb = _pin(canvas, "PB1", "1")
    # Same height as the pushbutton: a straight rung onto L1.
    _click_pin(canvas, app, "PB1", "1")
    assert canvas.wire_from == "PB1.1"
    _click(canvas, app, geom.left, pb.scenePos().y())
    assert canvas.wire_from is None
    rung = session.sheet().wires[-1]
    assert {rung.frm, rung.to} == {"PB1.1", "L1"}
    assert rung.from_at is None and rung.to_at is None

    # Anywhere on L2, not on the pin's rung. The junction stays at that click.
    _click_pin(canvas, app, "PB1", "2")
    land_y = pb.scenePos().y() + 36.0
    _dx, dy = _click(canvas, app, geom.right, land_y)
    branch = session.sheet().wires[-1]
    assert {branch.frm, branch.to} == {"PB1.2", "L2"}
    stored = branch.to_at if branch.to == "L2" else branch.from_at
    assert stored is not None
    assert abs(stored.x - geom.right) < 0.2
    # The viewport click is one pixel. The junction is that pixel, not the pin.
    assert abs(dy - land_y) < 2.0
    assert abs(stored.y - dy) < 0.05
    assert abs(stored.y - pb.scenePos().y()) > 20.0

    routes = route_sheet_wires(session.sheet(), session.catalog, session.project)
    end = routes[branch.id][-1] if branch.to == "L2" else routes[branch.id][0]
    assert abs(end.x - geom.right) < 0.2 and abs(end.y - stored.y) < 0.05
    assert _near(_junctions(canvas), geom.right, stored.y)
    assert _near(_junctions(canvas), geom.left, pb.scenePos().y())

    # Secondary of the control transformer onto the same rails, then the coil.
    for ref, pin_id, rail_x, rail_name in (
        ("T1", "X1", geom.left, "L1"),
        ("T1", "X2", geom.right, "L2"),
        ("M1", "A1", geom.left, "L1"),
        ("M1", "A2", geom.right, "L2"),
    ):
        pin = _pin(canvas, ref, pin_id)
        _click_pin(canvas, app, ref, pin_id)
        _click(canvas, app, rail_x, pin.scenePos().y())
        made = session.sheet().wires[-1]
        assert rail_name in {made.frm, made.to}
        assert f"{ref}.{pin_id}" in {made.frm, made.to}

    l1 = [wire.number for wire in session.sheet().wires if "L1" in {wire.frm, wire.to}]
    l2 = [wire.number for wire in session.sheet().wires if "L2" in {wire.frm, wire.to}]
    assert l1 and l2
    assert len(set(l1)) == 1 and l1[0]
    assert len(set(l2)) == 1 and l2[0]
    assert l1[0] != l2[0]

    supply = next(row for row in session.power_budget()["supplies"] if row["ref"] == "T1")
    assert supply["steady_va"] > 0

    path = tmp_path / "ladder.pcad.yaml"
    session.save(path)
    again = Session.open(path)
    assert {(w.frm, w.to) for w in again.sheet().wires} == {(w.frm, w.to) for w in session.sheet().wires}
    saved = next(w for w in again.sheet().wires if {w.frm, w.to} == {"PB1.2", "L2"})
    saved_at = saved.to_at if saved.to == "L2" else saved.from_at
    assert saved_at is not None and abs(saved_at.y - stored.y) < 0.05
    assert next(row for row in again.power_budget()["supplies"] if row["ref"] == "T1")["steady_va"] > 0

    before = len(session.sheet().wires)
    win.undo_edit()
    app.processEvents()
    assert len(session.sheet().wires) == before - 1
    win.redo_edit()
    app.processEvents()
    assert len(session.sheet().wires) == before
    assert next(row for row in session.power_budget()["supplies"] if row["ref"] == "T1")["steady_va"] > 0
    win.close()


def test_gui_click_connects_iec_column_rail() -> None:
    from panelcad.api.session import Session
    from panelcad.core.ladder import rail_geometry

    app = _app()
    session = Session.create("IEC")
    session.set_title_block(standard="IEC", rails="L+/0V")
    session.sheet().drawing_style = "iec"
    session.place("iec.operators.pb_no", ref="S1", x=120, y=80)
    session.place("iec.power.control_transformer", ref="T1", x=40, y=80)
    session.place("iec.contactors.coil", ref="K1", x=200, y=90)
    _opened = _open(session)
    app, win = _opened
    canvas = win.canvas
    geom = rail_geometry(session.sheet(), session.project)
    assert geom is not None and geom.kind == "iec"
    assert geom.hot == "L+" and geom.ret == "0V"

    pin = _pin(canvas, "S1", "13")
    _click_pin(canvas, app, "S1", "13")
    # Anywhere along the top rail, not above the pin.
    land_x = pin.scenePos().x() + 28.0
    dx, _dy = _click(canvas, app, land_x, geom.top)
    made = session.sheet().wires[-1]
    assert {made.frm, made.to} == {"S1.13", "L+"}
    stored = made.to_at if made.to == "L+" else made.from_at
    assert stored is not None
    assert abs(dx - land_x) < 2.0
    assert abs(stored.x - dx) < 0.05 and abs(stored.y - geom.top) < 0.2
    assert abs(stored.x - pin.scenePos().x()) > 20.0
    assert _near(_junctions(canvas), stored.x, geom.top)

    _click_pin(canvas, app, "S1", "14")
    _click(canvas, app, pin.scenePos().x(), geom.bottom)
    ret = session.sheet().wires[-1]
    assert {ret.frm, ret.to} == {"S1.14", "0V"}

    for ref, pin_id, rail_y, rail_name in (
        ("T1", "X1", geom.top, "L+"),
        ("T1", "X2", geom.bottom, "0V"),
        ("K1", "A1", geom.top, "L+"),
        ("K1", "A2", geom.bottom, "0V"),
    ):
        terminal = _pin(canvas, ref, pin_id)
        _click_pin(canvas, app, ref, pin_id)
        _click(canvas, app, terminal.scenePos().x(), rail_y)
        wired = session.sheet().wires[-1]
        assert rail_name in {wired.frm, wired.to}
        assert f"{ref}.{pin_id}" in {wired.frm, wired.to}

    supply = next(row for row in session.power_budget()["supplies"] if row["ref"] == "T1")
    assert supply["steady_va"] > 0
    hot_numbers = [wire.number for wire in session.sheet().wires if "L+" in {wire.frm, wire.to}]
    assert hot_numbers and len(set(hot_numbers)) == 1 and hot_numbers[0]
    win.close()


def test_gui_click_on_io_ladder_rail_starts_a_wire() -> None:
    from panelcad.api.session import Session
    from panelcad.core.ladder import rail_geometry

    app = _app()
    session = Session.create("IO rails")
    session.set_title_block(drawing_style="ladder", rails="24VDC/0VDC")
    sheet = session.add_sheet("io1", kind="io")
    sheet.drawing_style = "ladder"
    _opened = _open(session)
    app, win = _opened
    geom = rail_geometry(session.sheet(), session.project)
    assert geom is not None and geom.hot == "24VDC"
    _click(win.canvas, app, geom.left, (geom.top + geom.bottom) / 2.0)
    assert win.canvas.wire_from == "24VDC"
    win.close()


def test_schematic_sheet_has_no_rail_to_click() -> None:
    from panelcad.api.session import Session
    from panelcad.core.ladder import rail_xs
    from panelcad.render.titleblock import working_rect

    app = _app()
    session = Session.create("Power")
    session.set_title_block(drawing_style="schematic")
    session.place("iec.operators.pb_no", ref="S1", x=80, y=80)
    _opened = _open(session)
    app, win = _opened
    _wx, wy, _ww, wh = working_rect(session.sheet())
    left, _right = rail_xs(session.sheet())
    _click(win.canvas, app, left, wy + wh / 2.0)
    assert win.canvas.wire_from is None
    assert session.sheet().wires == []
    win.close()


@pytest.mark.parametrize("style", ["ladder", "iec"])
def test_hit_rail_rejects_a_miss(style: str) -> None:
    from panelcad.api.session import Session
    from panelcad.core.ladder import hit_rail, rail_geometry

    session = Session.create(style)
    if style == "iec":
        session.set_title_block(rails="L+/0V")
        session.sheet().drawing_style = "iec"
    else:
        session.set_title_block(drawing_style="ladder")
    geom = rail_geometry(session.sheet(), session.project)
    assert geom is not None
    assert hit_rail(session.sheet(), (geom.left + geom.right) / 2.0, (geom.top + geom.bottom) / 2.0, session.project) is None


def _last_rung_y(sheet, project) -> tuple[int, float]:
    from panelcad.core.zones import rung_y
    from panelcad.render.titleblock import working_rect

    _wx, wy, _ww, wh = working_rect(sheet)
    limit = wy + wh
    index = 1
    y = rung_y(sheet, 1, project=project)
    while index < 99:
        nxt = rung_y(sheet, index + 1, project=project)
        if nxt > limit + 1e-6:
            break
        index += 1
        y = nxt
    return index, y


def test_select_and_place_ignore_the_rail() -> None:
    from panelcad.api.session import Session
    from panelcad.core.ladder import rail_geometry
    from panelcad.gui.canvas import WireItem

    app = _app()
    session = Session.create("Modes")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_nc", ref="PB1", x=90, y=70)
    _opened = _open(session)
    app, win = _opened
    canvas = win.canvas
    geom = rail_geometry(session.sheet(), session.project)
    assert geom is not None
    pin = _pin(canvas, "PB1", "1")
    _click_pin(canvas, app, "PB1", "1")
    _click(canvas, app, geom.left, pin.scenePos().y())
    assert len(session.sheet().wires) == 1

    canvas.set_select_mode()
    app.processEvents()
    # On the rail itself: Select does not start a wire.
    _click(canvas, app, geom.left, pin.scenePos().y() + 24.0)
    assert canvas.wire_from is None
    assert canvas.mode == "select"
    assert len(session.sheet().wires) == 1

    # A rung 2 mm in from the rail is the wire, not a new rail connection.
    _click(canvas, app, geom.left + 2.0, pin.scenePos().y())
    assert canvas.wire_from is None
    assert len(session.sheet().wires) == 1
    selected = [item for item in canvas.scene.selectedItems() if isinstance(item, WireItem)]
    assert selected and selected[0].wire_id == session.sheet().wires[0].id

    before = len(session.sheet().components)
    canvas.set_place_mode("nema.operators.pb_no")
    app.processEvents()
    _click(canvas, app, geom.left + 2.0, geom.top + 18.0)
    assert canvas.wire_from is None
    assert canvas.mode == "place"
    assert len(session.sheet().components) == before + 1
    assert len(session.sheet().wires) == 1
    win.close()


def test_wire_tool_refuses_to_short_the_rails() -> None:
    from panelcad.api.session import Session
    from panelcad.core.ladder import rail_geometry

    session = Session.create("Short")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    with pytest.raises(ValueError, match="L1 to L2 would short the rails"):
        session.wire("L1", "L2")
    assert session.sheet().wires == []

    iec = Session.create("IEC short")
    iec.set_title_block(standard="IEC", rails="L+/0V")
    iec.sheet().drawing_style = "iec"
    with pytest.raises(ValueError, match=r"L\+ to 0V would short the rails"):
        iec.wire("L+", "0V")
    assert iec.sheet().wires == []

    app = _app()
    _opened = _open(session)
    app, win = _opened
    canvas = win.canvas
    geom = rail_geometry(session.sheet(), session.project)
    assert geom is not None
    _click(canvas, app, geom.left, (geom.top + geom.bottom) / 2.0)
    assert canvas.wire_from == "L1"
    _click(canvas, app, geom.right, (geom.top + geom.bottom) / 2.0)
    assert session.sheet().wires == []
    assert canvas.wire_from == "L1"
    assert "L1 to L2 would short the rails" in win.statusBar().currentMessage()
    win.close()

    _opened = _open(iec)
    app, win = _opened
    canvas = win.canvas
    geom = rail_geometry(iec.sheet(), iec.project)
    assert geom is not None and geom.kind == "iec"
    _click(canvas, app, (geom.left + geom.right) / 2.0, geom.top)
    assert canvas.wire_from == "L+"
    _click(canvas, app, (geom.left + geom.right) / 2.0, geom.bottom)
    assert iec.sheet().wires == []
    assert canvas.wire_from == "L+"
    assert "L+ to 0V would short the rails" in win.statusBar().currentMessage()
    win.close()


@pytest.mark.parametrize("style", ["ladder", "power", "freeform"])
def test_devices_named_like_rails_wire_pin_to_pin(style: str) -> None:
    """L1 and L2 are normal device tags on a power sheet. Their pins are not rails."""

    from panelcad.api.session import Session

    session = Session.create(style)
    if style == "ladder":
        session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.sheet().drawing_style = style  # type: ignore[assignment]
    session.place("nema.operators.pilot_light", ref="L1", x=100, y=80)
    session.place("nema.operators.pilot_light", ref="L2", x=200, y=80)
    made = session.wire("L1.X2", "L2.X1")
    assert {made.frm, made.to} == {"L1.X2", "L2.X1"}


@pytest.mark.parametrize("style", ["power", "freeform"])
def test_bare_l1_to_l2_is_allowed_when_the_sheet_draws_no_rails(style: str) -> None:
    from panelcad.api.session import Session

    session = Session.create(style)
    session.sheet().drawing_style = style  # type: ignore[assignment]
    made = session.wire("L1", "L2")
    assert made.frm == "L1" and made.to == "L2"


def test_nudge_and_drag_keep_the_rail_landing() -> None:
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QKeyEvent, QMouseEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from panelcad.api.session import Session
    from panelcad.core.ladder import rail_geometry
    from panelcad.gui.canvas import ComponentItem
    from panelcad.render.wiring import route_sheet_wires

    app = _app()
    session = Session.create("Nudge")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pilot_light", ref="PL1", x=120, y=80)
    _opened = _open(session)
    app, win = _opened
    canvas = win.canvas
    geom = rail_geometry(session.sheet(), session.project)
    assert geom is not None
    pin = _pin(canvas, "PL1", "X2")
    _click_pin(canvas, app, "PL1", "X2")
    land_y = pin.scenePos().y() + 30.0
    _click(canvas, app, geom.right, land_y)
    wire = session.sheet().wires[-1]
    stored = wire.to_at if wire.to == "L2" else wire.from_at
    assert stored is not None
    kept_y = stored.y
    assert abs(kept_y - pin.scenePos().y()) > 20.0

    canvas.set_select_mode()
    canvas.select_ref("PL1")
    canvas.setFocus()
    app.processEvents()
    QTest.keyClick(canvas, Qt.Key.Key_Right)
    app.processEvents()
    if abs(session.sheet().component("PL1").x - 120.0) < 0.1:
        canvas.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier))
        app.processEvents()
    assert session.sheet().component("PL1").x > 120.0
    wire = session.sheet().wires[0]
    stored = wire.to_at if wire.to == "L2" else wire.from_at
    assert stored is not None and abs(stored.y - kept_y) < 1e-6
    route = route_sheet_wires(session.sheet(), session.catalog, session.project)[wire.id]
    end = route[-1] if wire.to == "L2" else route[0]
    assert abs(end.y - kept_y) < 0.05 and abs(end.x - geom.right) < 0.2

    item = next(i for i in canvas.scene.items() if isinstance(i, ComponentItem) and i.component_id == "PL1")
    x_before_drag = session.sheet().component("PL1").x
    # Above the rung, so the drag grabs the symbol and not the wire.
    start = QPointF(item.scenePos().x() + 12.0, item.scenePos().y() + 4.0)
    end_pt = QPointF(start.x() + 16.0, start.y())
    viewport = canvas.viewport()
    press_at = canvas.mapFromScene(start)
    release_at = canvas.mapFromScene(end_pt)
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, press_at)
    QApplication.sendEvent(
        viewport,
        QMouseEvent(
            QMouseEvent.Type.MouseMove,
            QPointF(release_at),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, release_at)
    app.processEvents()
    assert session.sheet().component("PL1").x > x_before_drag + 1.0
    wire = session.sheet().wires[0]
    stored = wire.to_at if wire.to == "L2" else wire.from_at
    assert stored is not None and abs(stored.y - kept_y) < 1e-6
    route = route_sheet_wires(session.sheet(), session.catalog, session.project)[wire.id]
    end = route[-1] if wire.to == "L2" else route[0]
    assert abs(end.y - kept_y) < 0.05
    win.close()


@pytest.mark.parametrize("size", ["A3", "LETTER", "TABLOID"])
def test_last_numbered_rung_connects_to_the_rail(size: str) -> None:
    from panelcad.api.session import Session
    from panelcad.core.ladder import hit_rail, rail_geometry
    from panelcad.render.sheet import pin_world
    from panelcad.render.titleblock import working_rect

    app = _app()
    session = Session.create(size)
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.sheet().page.size = size
    session.sheet().page.orientation = "landscape"
    _wx, wy, _ww, wh = working_rect(session.sheet())
    _index, rung_y = _last_rung_y(session.sheet(), session.project)
    session.place("nema.operators.pb_nc", ref="PB1", x=90, y=40)
    comp = session.sheet().component("PB1")
    device = session.catalog.get(comp.library_id)
    _px, py = pin_world(comp, device.pin("1"))
    comp.y += rung_y - py
    _opened = _open(session)
    app, win = _opened
    canvas = win.canvas
    geom = rail_geometry(session.sheet(), session.project)
    assert geom is not None
    assert geom.bottom + 1e-6 >= rung_y
    hit = hit_rail(session.sheet(), geom.left, rung_y, session.project)
    assert hit is not None and abs(hit.y - rung_y) < 0.2
    # A3's last rung sits below the old wy+wh-8 inset. Letter and Tabloid do not.
    if size == "A3":
        assert rung_y > wy + wh - 8.0
    pin = _pin(canvas, "PB1", "1")
    assert abs(pin.scenePos().y() - rung_y) < 0.5
    _click_pin(canvas, app, "PB1", "1")
    _click(canvas, app, geom.left, rung_y)
    assert canvas.wire_from is None
    made = session.sheet().wires[-1]
    assert {made.frm, made.to} == {"PB1.1", "L1"}
    win.close()
