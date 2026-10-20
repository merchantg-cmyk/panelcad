"""Wire hits: at least 3 mm and about 7 screen pixels. Offscreen."""

from __future__ import annotations

import pytest


def _app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _view():
    from PySide6.QtGui import QPainterPath
    from PySide6.QtWidgets import QGraphicsRectItem

    from panelcad.api.session import Session
    from panelcad.gui.canvas import SchematicView, WireItem

    app = _app()
    session = Session.create("Wire hit")
    view = SchematicView(session)
    view.resize(1000, 800)
    view.show()
    app.processEvents()
    view.set_select_mode()

    def add(wire_id: str, y: float) -> WireItem:
        path = QPainterPath()
        path.moveTo(20.0, y)
        path.lineTo(320.0, y)
        item = WireItem(path, wire_id)
        view._add(item)
        return item

    near = add("near", 40.0)
    # 120 mm away. A 7 px band at 0.35 px/mm is 20 mm, so it does not reach this wire.
    far = add("far", 160.0)
    # A page-sized item above the wires. The old index returned this and the
    # click never reached the conductor.
    cover = QGraphicsRectItem(0, 0, 2000, 2000)
    cover.setZValue(200)
    cover.setFlag(cover.GraphicsItemFlag.ItemIsSelectable, True)
    cover.setAcceptedMouseButtons(cover.acceptedMouseButtons())
    view._add(cover)
    view._wire_hit_scale = -1.0
    view._refresh_wire_hit_geometry()
    app.processEvents()
    return app, view, near, far, cover


def _scene_for_pixels(view, x_mm: float, y_mm: float, dx_px: float, dy_px: float):
    from PySide6.QtCore import QPointF

    scale = abs(view.transform().m11())
    return QPointF(x_mm + dx_px / scale, y_mm + dy_px / scale)


def _click_scene(view, app, scene_pos) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    view.centerOn(scene_pos)
    app.processEvents()
    point = view.mapFromScene(scene_pos)
    assert view.viewport().rect().contains(point), (point.x(), point.y(), view.viewport().rect())
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    app.processEvents()


def _selected_wire(view) -> str:
    from panelcad.gui.canvas import WireItem

    chosen = [item.wire_id for item in view.scene.selectedItems() if isinstance(item, WireItem)]
    assert len(chosen) == 1
    return chosen[0]


def _radius_mm(view, wire) -> float:
    view._wire_hit_scale = -1.0
    view._refresh_wire_hit_geometry()
    return wire.hit_width_scene() * 0.5


@pytest.mark.parametrize("scale", [0.35, 1.0, 3.0, 8.0])
def test_wire_hit_follows_the_larger_of_mm_and_pixels(scale: float) -> None:
    from PySide6.QtCore import QPointF

    from panelcad.gui.canvas import WIRE_HIT_MM, WIRE_HIT_PX, SheetChromeItem, WireItem

    app, view, near, far, cover = _view()
    try:
        view.resetTransform()
        view.scale(scale, scale)
        radius = _radius_mm(view, near)
        assert abs(radius - max(WIRE_HIT_MM, WIRE_HIT_PX / scale)) < 1e-6

        inside = QPointF(120.0, 40.0 + radius * 0.5)
        outside = QPointF(120.0, 40.0 + radius * 1.25)
        assert near.shape().contains(near.mapFromScene(inside))
        assert not near.shape().contains(near.mapFromScene(outside))
        assert near.boundingRect().contains(near.mapFromScene(inside))
        listed = view.items(view.mapFromScene(inside))
        assert any(isinstance(item, WireItem) and item.wire_id == "near" for item in listed)
        assert any(item is cover for item in listed)
        chrome = next(item for item in view.scene.items() if isinstance(item, SheetChromeItem))
        assert chrome.shape().isEmpty()
        assert not chrome.shape().contains(chrome.mapFromScene(inside))

        assert view.wire_at_scene(inside) is near
        assert view.wire_at_scene(outside) is None
        assert view.wire_at_scene(inside) is not far

        view.scene.clearSelection()
        _click_scene(view, app, inside)
        assert _selected_wire(view) == "near"
    finally:
        view.close()


@pytest.mark.parametrize("scale", [1.0, 1.64, 2.7, 4.0, 8.0])
def test_hit_radius_is_never_below_3mm_or_7px(scale: float) -> None:
    """Fit-page and 1–8 px/mm stay at least as wide as the old ±3 mm stroker."""

    from PySide6.QtCore import QPointF

    from panelcad.gui.canvas import WIRE_HIT_MM, WIRE_HIT_PX

    app, view, near, _far, _cover = _view()
    try:
        view.resetTransform()
        view.scale(scale, scale)
        radius = _radius_mm(view, near)
        radius_px = radius * scale
        assert radius + 1e-6 >= WIRE_HIT_MM
        assert radius_px + 1e-3 >= WIRE_HIT_PX
        # Both floors still select. The farther one is the real radius.
        for point in (
            QPointF(150.0, 40.0 + WIRE_HIT_MM * 0.95),
            _scene_for_pixels(view, 180.0, 40.0, 0.0, WIRE_HIT_PX * 0.9),
        ):
            view.scene.clearSelection()
            assert view.wire_at_scene(point) is near
            _click_scene(view, app, point)
            assert _selected_wire(view) == "near"
        beyond = QPointF(200.0, 40.0 + radius * 1.2)
        assert view.wire_at_scene(beyond) is None
    finally:
        view.close()


def test_fit_page_hit_is_at_least_3mm_and_7px() -> None:
    from PySide6.QtCore import QPointF

    from panelcad.gui.canvas import WIRE_HIT_MM, WIRE_HIT_PX

    app, view, near, _far, _cover = _view()
    try:
        view.fit_page()
        app.processEvents()
        scale = abs(view.transform().m11())
        assert scale > 0.2
        radius = _radius_mm(view, near)
        assert radius + 1e-6 >= WIRE_HIT_MM
        assert radius * scale + 1e-3 >= WIRE_HIT_PX
        inside_mm = QPointF(120.0, 40.0 + WIRE_HIT_MM * 0.95)
        inside_px = _scene_for_pixels(view, 160.0, 40.0, 0.0, WIRE_HIT_PX * 0.9)
        assert view.wire_at_scene(inside_mm) is near
        assert view.wire_at_scene(inside_px) is near
        assert near.boundingRect().contains(near.mapFromScene(inside_mm))
        assert near.boundingRect().contains(near.mapFromScene(inside_px))
        _click_scene(view, app, inside_mm)
        assert _selected_wire(view) == "near"
    finally:
        view.close()


def test_closest_wire_wins_over_a_covering_item() -> None:
    from PySide6.QtCore import QPointF

    app, view, near, far, _cover = _view()
    try:
        view.resetTransform()
        view.scale(2.0, 2.0)
        far.setZValue(80)
        # 1 mm is inside the 3 mm floor. The far wire is hundreds of mm away.
        click = QPointF(160.0, 41.0)
        assert view.wire_at_scene(click) is near
        far_y = far.path().pointAtPercent(0.5).y()
        assert view.wire_at_scene(QPointF(160.0, far_y - 1.0)) is far
        _click_scene(view, app, click)
        assert _selected_wire(view) == "near"
    finally:
        view.close()


def test_hover_shows_the_wire_that_will_be_picked() -> None:
    from PySide6.QtCore import QPointF
    from PySide6.QtTest import QTest

    app, view, near, far, _cover = _view()
    try:
        view.resetTransform()
        view.scale(2.0, 2.0)
        far_y = far.path().pointAtPercent(0.5).y()
        view.centerOn(QPointF(140.0, 40.0))
        app.processEvents()
        on_near = view.mapFromScene(QPointF(140.0, 41.0))
        QTest.mouseMove(view.viewport(), on_near)
        app.processEvents()
        assert near.hovered
        assert not far.hovered
        view.centerOn(QPointF(140.0, far_y))
        app.processEvents()
        on_far = view.mapFromScene(QPointF(140.0, far_y - 1.0))
        QTest.mouseMove(view.viewport(), on_far)
        app.processEvents()
        assert far.hovered
        assert not near.hovered
        away = view.mapFromScene(QPointF(140.0, 200.0))
        QTest.mouseMove(view.viewport(), away)
        app.processEvents()
        assert not near.hovered
        assert not far.hovered
    finally:
        view.close()


def test_device_click_and_rubber_band_still_select() -> None:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import ComponentItem, WireItem

    app, view, _near, _far, _cover = _view()
    try:
        view.session.place("nema.operators.pb_no", ref="PB1", x=40.0, y=220.0)
        view.reload()
        app.processEvents()
        # reload drops the hand-built wires. Put a conductor back, far from PB1.
        from PySide6.QtGui import QPainterPath

        path = QPainterPath()
        path.moveTo(20.0, 40.0)
        path.lineTo(320.0, 40.0)
        wire = WireItem(path, "w")
        view._add(wire)
        device = next(item for item in view.scene.items() if isinstance(item, ComponentItem))
        view.resetTransform()
        view.scale(2.0, 2.0)
        view.scene.clearSelection()
        body = device.mapToScene(QPointF(6.0, -8.0))
        _click_scene(view, app, body)
        assert device.isSelected()
        assert not any(isinstance(item, WireItem) and item.isSelected() for item in view.scene.items())

        view.scene.clearSelection()
        view.centerOn(device.scenePos())
        app.processEvents()
        viewport = view.viewport()
        rect = viewport.rect().adjusted(24, 24, -24, -24)
        start = rect.bottomRight()
        end = rect.topLeft()
        assert viewport.rect().contains(start)
        assert viewport.rect().contains(end)
        QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
        QApplication.sendEvent(
            viewport,
            QMouseEvent(
                QMouseEvent.Type.MouseMove,
                QPointF(end),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
        app.processEvents()
        assert device.isSelected()
    finally:
        view.close()


def _scene_order_pick(view, scene_pos):
    """The pre-change picker: topmost scene item, then closest wire."""

    from panelcad.gui.canvas import WireItem, wire_hit_radius_scene

    limit = wire_hit_radius_scene(view)
    best = None
    best_d = limit
    px, py = float(scene_pos.x()), float(scene_pos.y())
    for item in view.scene.items():
        if not isinstance(item, WireItem) or not item.isVisible():
            continue
        distance = item.distance_to_scene(px, py)
        if distance > limit + 1e-6:
            continue
        closer = best is None or distance < best_d - 1e-6
        tie = best is not None and abs(distance - best_d) <= 1e-6 and item.zValue() > best.zValue()
        if closer or tie:
            best = item
            best_d = distance
    return best


def test_shared_segment_picks_the_top_wire() -> None:
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QPainterPath

    from panelcad.gui.canvas import WireItem

    app, view, _near, _far, _cover = _view()
    try:
        view.resetTransform()
        view.scale(1.0, 1.0)
        for name, y in (("bus-a", 40.0), ("bus-b", 40.0), ("bus-c", 40.0)):
            path = QPainterPath()
            path.moveTo(20.0, y)
            path.lineTo(320.0, y)
            view._add(WireItem(path, name))
        point = QPointF(120.0, 40.0)
        picked = view.wire_at_scene(point)
        assert picked is not None
        assert picked.wire_id == "bus-c"
        assert picked is _scene_order_pick(view, point)
    finally:
        view.close()


def test_zoom_1_pick_matches_stacking_order_on_the_wire() -> None:
    from PySide6.QtCore import QPointF

    from panelcad.gui.canvas import WireItem, wire_hit_radius_scene

    app, view, near, _far, _cover = _view()
    try:
        view.resetTransform()
        view.scale(1.0, 1.0)
        limit = wire_hit_radius_scene(view)
        samples = [
            QPointF(120.0, 40.0),
            QPointF(120.0, 40.0 + limit * 0.85),
            QPointF(120.0 + limit * 0.85, 40.0),
            QPointF(20.0, 40.0),
            QPointF(320.0, 40.0 - limit * 0.5),
        ]
        for point in samples:
            assert view.wire_at_scene(point) is _scene_order_pick(view, point)
        assert view.wire_at_scene(samples[1]) is near
        # A point just outside the radius is still empty.
        outside = QPointF(120.0, 40.0 + limit * 1.4)
        assert view.wire_at_scene(outside) is None
        assert _scene_order_pick(view, outside) is None
    finally:
        view.close()
