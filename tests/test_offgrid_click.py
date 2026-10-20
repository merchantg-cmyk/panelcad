"""A click must not pull off-grid parts onto the grid or push an undo step."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from panelcad import Session
from panelcad.core.models import Component
from panelcad.gui.canvas import SNAP, ComponentItem
from panelcad.gui.main_window import MainWindow
from panelcad.library.panel import effective_footprint

_SCHEMATIC = "iec.operators.pb_no"
_LAYOUT = "iec.breakers.mcb_1p_16a"


def _positions(session: Session) -> dict[str, tuple[float, float]]:
    return {comp.id: (comp.x, comp.y) for comp in session.sheet().components}


def _scene_positions(canvas) -> dict[str, tuple[float, float]]:
    found: dict[str, tuple[float, float]] = {}
    for item in canvas.scene.items():
        if isinstance(item, ComponentItem):
            found[item.component_id] = (item.pos().x(), item.pos().y())
    return found


def _item(canvas, ref: str) -> ComponentItem:
    return next(
        i
        for i in canvas.scene.items()
        if isinstance(i, ComponentItem) and i.component_id == ref
    )


def _viewport_point(canvas, scene_pos: QPointF):
    return canvas.mapFromScene(scene_pos)


def _open(kind: str):
    app = QApplication.instance() or QApplication([])
    session = Session.create("OffGrid")
    if kind == "layout":
        session.add_sheet("panel", kind="panel_layout")
        session.set_active_sheet("panel")
        device = session.catalog.get(_LAYOUT)
        assert effective_footprint(device).din_rail
        assert session.sheet().rails
        sheet = session.sheet()
        # Far enough apart that the padded footprints do not share a hit point.
        sheet.components.append(Component(id="A1", library_id=_LAYOUT, x=41.3, y=80.4))
        sheet.components.append(Component(id="A2", library_id=_LAYOUT, x=220.6, y=180.2))
    else:
        session.place(_SCHEMATIC, ref="A1", x=41.3, y=52.7)
        session.place(_SCHEMATIC, ref="A2", x=160.4, y=90.2)
    window = MainWindow(session)
    window.resize(1400, 1000)
    window.show()
    app.processEvents()
    return app, session, window


def _drag(canvas, item: ComponentItem, dx: float, dy: float) -> None:
    start = item.sceneBoundingRect().center()
    end = QPointF(start.x() + dx, start.y() + dy)
    viewport = canvas.viewport()
    press_at = _viewport_point(canvas, start)
    release_at = _viewport_point(canvas, end)
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
    QTest.mouseRelease(
        viewport,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        release_at,
    )
    QApplication.processEvents()


@pytest.mark.parametrize("kind", ["schematic", "layout"])
def test_click_without_drag_leaves_offgrid_parts_and_undo_alone(kind: str):
    _app, session, window = _open(kind)
    canvas = window.canvas
    before = _positions(session)
    scene_before = _scene_positions(canvas)
    names = list(session.undo_command_names())
    depth = len(session._undo)
    item = _item(canvas, "A1")
    center = item.sceneBoundingRect().center()
    viewport = canvas.viewport()
    at = _viewport_point(canvas, center)
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at)
    assert canvas._gesture_edit
    assert session.command_open()
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at)
    assert not session.command_open()
    assert _positions(session) == before
    assert _scene_positions(canvas) == scene_before
    assert session.undo_command_names() == names
    assert len(session._undo) == depth
    assert session._redo == []
    window.close()


@pytest.mark.parametrize("kind", ["schematic", "layout"])
def test_drag_one_offgrid_part_snaps_only_that_part(kind: str):
    _app, session, window = _open(kind)
    canvas = window.canvas
    before = _positions(session)
    names = list(session.undo_command_names())
    depth = len(session._undo)
    item = _item(canvas, "A1")
    _drag(canvas, item, 30.0, 0.0)
    assert not session.command_open()
    moved = _positions(session)
    assert moved["A2"] == before["A2"]
    assert moved["A1"] != before["A1"]
    assert _scene_positions(canvas)["A2"] == before["A2"]
    if kind == "schematic":
        ax, ay = moved["A1"]
        assert abs(ax / SNAP - round(ax / SNAP)) < 1e-6
        assert abs(ay / SNAP - round(ay / SNAP)) < 1e-6
    assert len(session._undo) == depth + 1
    assert session.undo_command_names() == [*names, "move"]
    assert session.undo()
    assert _positions(session) == before
    assert len(session._undo) == depth
    assert session.undo_command_names() == names
    window.close()
