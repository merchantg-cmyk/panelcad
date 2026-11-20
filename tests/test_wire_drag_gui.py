"""Offscreen GUI and unit tests for interactive wire end/segment/corner dragging."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QFocusEvent, QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from panelcad.api.session import Session
from panelcad.core.electrical import wire_number_conflicts
from panelcad.core.geometry import Point
from panelcad.core.ladder import rail_geometry
from panelcad.core.route import STUB, move_wire_corner, nearest_corner, set_run_channel
from panelcad.core.wire_numbering import wire_networks
from panelcad.gui.canvas import (
    ComponentItem,
    LogoSlotItem,
    PinItem,
    RailJunctionItem,
    WireHandleItem,
    WireItem,
)
from panelcad.gui.main_window import MainWindow
from panelcad.render.sheet import iter_wire_points, pin_world
from panelcad.render.titleblock import title_block_rect
from panelcad.render.wiring import PIN_KEEP_MM


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _dump(session: Session) -> dict:
    return session.project.model_dump(mode="python")


def _open_window(session: Session) -> tuple[QApplication, MainWindow]:
    app = _app()
    win = MainWindow(session)
    win.resize(1400, 950)
    win.show()
    app.processEvents()
    win.canvas.fit_page()
    app.processEvents()
    win.canvas.set_select_mode()
    app.processEvents()
    return app, win


def _pin_item(canvas, ref: str, pin_id: str) -> PinItem:
    return next(
        item
        for item in canvas.scene.items()
        if isinstance(item, PinItem) and item.component_id == ref and item.pin_id == pin_id
    )


def _wire_item(canvas, wire_id: str) -> WireItem:
    return next(
        item
        for item in canvas.scene.items()
        if isinstance(item, WireItem) and item.wire_id == wire_id
    )


def _handles(canvas, wire_id: str | None = None) -> list[WireHandleItem]:
    items = [i for i in canvas.scene.items() if isinstance(i, WireHandleItem)]
    if wire_id is not None:
        items = [i for i in items if i.wire_id == wire_id]
    return items


def _ensure_visible_pair(canvas, app: QApplication, a: QPointF, b: QPointF) -> tuple[QPoint, QPoint]:
    mid = QPointF((a.x() + b.x()) / 2.0, (a.y() + b.y()) / 2.0)
    canvas.centerOn(mid)
    app.processEvents()
    pa = canvas.mapFromScene(a)
    pb = canvas.mapFromScene(b)
    rect = canvas.viewport().rect()
    if not (rect.contains(pa) and rect.contains(pb)):
        canvas.fit_page()
        app.processEvents()
        pa = canvas.mapFromScene(a)
        pb = canvas.mapFromScene(b)
    assert rect.contains(pa) and rect.contains(pb), (pa, pb, rect)
    return pa, pb


def _drag_scene(canvas, app: QApplication, start_scene: QPointF, end_scene: QPointF) -> None:
    p_start, p_end = _ensure_visible_pair(canvas, app, start_scene, end_scene)
    vp = canvas.viewport()
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p_start)
    app.processEvents()
    mid_pt = QPoint((p_start.x() + p_end.x()) // 2, (p_start.y() + p_end.y()) // 2)
    QTest.mouseMove(vp, mid_pt)
    app.processEvents()
    QTest.mouseMove(vp, p_end)
    app.processEvents()
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p_end)
    app.processEvents()


def _click_scene(canvas, app: QApplication, pos_scene: QPointF) -> None:
    canvas.centerOn(pos_scene)
    app.processEvents()
    pt = canvas.mapFromScene(pos_scene)
    assert canvas.viewport().rect().contains(pt)
    QTest.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pt)
    app.processEvents()


def _assert_orthogonal(pts: list[Point], tol: float = 0.2) -> None:
    assert len(pts) >= 2
    for a, b in zip(pts, pts[1:]):
        assert abs(a.x - b.x) <= tol or abs(a.y - b.y) <= tol, (a, b)


def _wire_endpoints_xy(session: Session, wire_id: str) -> tuple[tuple[float, float], tuple[float, float]]:
    from panelcad.render.wiring import wire_ends

    wire = session.find_wire(wire_id=wire_id)
    ax, ay, _, bx, by, _ = wire_ends(session.sheet(), session.catalog, wire, session.project)
    return (ax, ay), (bx, by)


def test_core_set_run_channel_and_corner_helpers() -> None:
    assert STUB >= PIN_KEEP_MM

    # 2-point horizontal wire keeps STUB-offset horizontal exits clear of adjacent pins.
    h2 = [Point(x=10.0, y=20.0), Point(x=100.0, y=20.0)]
    pulled_h = set_run_channel(h2, 0, "h", 40.0)
    assert [(p.x, p.y) for p in pulled_h] == [
        (10.0, 20.0),
        (10.0 + STUB, 20.0),
        (10.0 + STUB, 40.0),
        (100.0 - STUB, 40.0),
        (100.0 - STUB, 20.0),
        (100.0, 20.0),
    ]

    # 2-point vertical wire keeps STUB-offset vertical exits clear of adjacent pins.
    v2 = [Point(x=30.0, y=10.0), Point(x=30.0, y=90.0)]
    pulled_v = set_run_channel(v2, 0, "v", 55.0)
    assert [(p.x, p.y) for p in pulled_v] == [
        (30.0, 10.0),
        (30.0, 10.0 + STUB),
        (55.0, 10.0 + STUB),
        (55.0, 90.0 - STUB),
        (30.0, 90.0 - STUB),
        (30.0, 90.0),
    ]

    # End-adjacent segment of an L-bend keeps STUB-offset exit clear of the pin.
    l3 = [Point(x=0.0, y=0.0), Point(x=50.0, y=0.0), Point(x=50.0, y=40.0)]
    pulled_l = set_run_channel(l3, 0, "h", 15.0)
    assert [(p.x, p.y) for p in pulled_l] == [
        (0.0, 0.0),
        (STUB, 0.0),
        (STUB, 15.0),
        (50.0, 15.0),
        (50.0, 40.0),
    ]

    # Pin-adjacent corner drag on an L-bend keeps STUB exits clear at BOTH pins.
    moved_l = move_wire_corner(l3, 1, 70.0, 15.0)
    assert [(p.x, p.y) for p in moved_l] == [
        (0.0, 0.0),
        (STUB, 0.0),
        (STUB, 15.0),
        (70.0, 15.0),
        (70.0, 40.0 - STUB),
        (50.0, 40.0 - STUB),
        (50.0, 40.0),
    ]

    # Rail-attached run drag slides the rail end while keeping a STUB on the device pin.
    slid_rail = set_run_channel(h2, 0, "h", 40.0, slide_start=True)
    assert [(p.x, p.y) for p in slid_rail] == [
        (10.0, 40.0),
        (100.0 - STUB, 40.0),
        (100.0 - STUB, 20.0),
        (100.0, 20.0),
    ]

    # Interior corner lookup & movement keeps endpoints pinned and orthogonal.
    z4 = [
        Point(x=0.0, y=0.0),
        Point(x=0.0, y=20.0),
        Point(x=50.0, y=20.0),
        Point(x=50.0, y=40.0),
        Point(x=100.0, y=40.0),
    ]
    assert nearest_corner(z4, 51.0, 21.0) == 2
    assert nearest_corner(z4, 0.5, 0.5) is None  # pinned endpoints are not interior corners
    moved = move_wire_corner(z4, 2, 70.0, 30.0)
    assert [(p.x, p.y) for p in moved] == [
        (0.0, 0.0),
        (0.0, 30.0),
        (70.0, 30.0),
        (70.0, 40.0),
        (100.0, 40.0),
    ]


def test_session_reconnect_wire_validation_and_undo() -> None:
    session = Session.create("Reconnect API")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=60, y=70)
    session.place("nema.operators.pb_no", ref="PB2", x=60, y=120)
    session.place("nema.contactors.coil", ref="M1", x=180, y=70)
    session.place("iec.loads.motor_3ph", ref="MOT1", x=280, y=140)
    w = session.wire(
        "PB1.4",
        "M1.A1",
        net="START",
        label="START",
        number="101",
        color="red",
        gauge="16",
        cable="C1",
        wire_type="MTW",
    )
    session.set_wire_route(w.id, [Point(x=90, y=90), Point(x=160, y=90)])
    session._undo.clear()
    session._undo_tip = None

    with pytest.raises(ValueError, match="which"):
        session.reconnect_wire(w.id, "middle", "PB2.4")
    with pytest.raises(ValueError, match="same pin"):
        session.reconnect_wire(w.id, "from", "M1.A1")
    with pytest.raises(ValueError, match="motor"):
        session.reconnect_wire(w.id, "to", "MOT1.U1")
    with pytest.raises(KeyError):
        session.reconnect_wire(w.id, "from", "PB1.NOPE")
    assert session._undo == []

    # Same-pin drop is a no-op with 0 undo steps and keeps points.
    pts_before = [(p.x, p.y) for p in w.points]
    session.reconnect_wire(w.id, "from", "PB1.4")
    assert session._undo == []
    assert [(p.x, p.y) for p in w.points] == pts_before

    # Valid reconnect takes 1 undo step, clears points, and keeps all attributes.
    before = _dump(session)
    session.reconnect_wire(w.id, "from", "PB2.4")
    assert len(session._undo) == 1
    assert session.undo_command_names()[-1] == "reconnect_wire"
    assert w.frm == "PB2.4"
    assert w.to == "M1.A1"
    assert w.points == []
    assert (
        w.number,
        w.number_locked,
        w.gauge,
        w.color,
        w.wire_type,
        w.cable,
        w.label,
        w.net,
    ) == ("101", True, "16", "red", "MTW", "C1", "START", "START")
    assert session.undo()
    assert _dump(session) == before
    assert session.redo()
    assert session.find_wire(wire_id=w.id).frm == "PB2.4"


def test_gui_wire_end_drag_to_pin_and_rail_with_single_undo() -> None:
    session = Session.create("WireEndGUI")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=70, y=70)
    session.place("nema.operators.pb_no", ref="PB2", x=70, y=120)
    session.place("nema.contactors.coil", ref="M1", x=180, y=70)
    session.place("nema.contactors.coil", ref="M2", x=180, y=120)
    wire = session.wire(
        "PB1.4",
        "M1.A1",
        net="RUN",
        label="RUN",
        number="205",
        color="blue",
        gauge="14",
        cable="C2",
        wire_type="THHN",
    )
    session.set_wire_route(wire.id, [Point(x=110, y=94), Point(x=110, y=104), Point(x=160, y=104)])
    session._undo.clear()
    session._undo_tip = None

    app, win = _open_window(session)
    try:
        canvas = win.canvas
        # Plain click on wire selects it and creates handles with 0 undo steps.
        w_item = _wire_item(canvas, wire.id)
        click_pt = w_item.path().pointAtPercent(0.5)
        _click_scene(canvas, app, click_pt)
        assert w_item.isSelected()
        assert len(session._undo) == 0
        end_handles = [h for h in _handles(canvas, wire.id) if h.kind == "end"]
        assert len(end_handles) == 2
        assert all(h.zValue() >= 30 for h in end_handles)

        # Plain click on an endpoint handle falls back to the underlying pin (starts wiring, 0 undo steps).
        to_handle = next(h for h in end_handles if h.which == "to")
        _click_scene(canvas, app, to_handle.pos())
        assert len(session._undo) == 0
        assert canvas.wire_from == "M1.A1"
        canvas.cancel_wire()
        app.processEvents()
        assert canvas.wire_from is None

        # Drag "to" end handle off M1.A1 onto M2.A1.
        before_pin_drag = _dump(session)
        m2_a1 = _pin_item(canvas, "M2", "A1").scenePos()
        to_handle = next(h for h in _handles(canvas, wire.id) if h.kind == "end" and h.which == "to")
        _drag_scene(canvas, app, to_handle.pos(), m2_a1)

        live = session.find_wire(wire_id=wire.id)
        assert live.frm == "PB1.4"
        assert live.to == "M2.A1"
        assert live.points == []
        assert (
            live.id,
            live.number,
            live.number_locked,
            live.gauge,
            live.color,
            live.wire_type,
            live.cable,
            live.label,
            live.net,
        ) == (wire.id, "205", True, "14", "blue", "THHN", "C2", "RUN", "RUN")
        assert len(session._undo) == 1

        # 1-step Undo and Redo.
        win.undo_edit()
        app.processEvents()
        assert _dump(session) == before_pin_drag
        win.redo_edit()
        app.processEvents()
        assert session.find_wire(wire_id=wire.id).to == "M2.A1"

        # Now drag "from" end handle off PB1.4 onto ladder rail L1.
        geom = rail_geometry(session.sheet(), session.project)
        assert geom is not None
        rail_target = QPointF(geom.left, 105.0)
        canvas.select_ref(wire.id)
        app.processEvents()
        from_handle = next(h for h in _handles(canvas, wire.id) if h.kind == "end" and h.which == "from")
        before_rail_drag = _dump(session)
        depth = len(session._undo)
        _drag_scene(canvas, app, from_handle.pos(), rail_target)

        live = session.find_wire(wire_id=wire.id)
        assert live.frm == "L1"
        assert live.to == "M2.A1"
        assert live.from_at is not None
        assert abs(live.from_at.x - geom.left) < 0.2
        assert abs(live.from_at.y - 105.0) < 1.5
        assert (
            live.number,
            live.number_locked,
            live.gauge,
            live.color,
            live.wire_type,
            live.cable,
            live.label,
            live.net,
        ) == ("205", True, "14", "blue", "THHN", "C2", "RUN", "RUN")
        assert len(session._undo) == depth + 1

        win.undo_edit()
        app.processEvents()
        assert _dump(session) == before_rail_drag
        win.redo_edit()
        app.processEvents()
        assert session.find_wire(wire_id=wire.id).frm == "L1"

        # Drag "from" end off rail L1 onto PB2.3 -> clears from_at cleanly in 1 step.
        canvas.select_ref(wire.id)
        app.processEvents()
        from_handle = next(h for h in _handles(canvas, wire.id) if h.kind == "end" and h.which == "from")
        pb2_3 = _pin_item(canvas, "PB2", "3").scenePos()
        depth = len(session._undo)
        _drag_scene(canvas, app, from_handle.pos(), pb2_3)
        live = session.find_wire(wire_id=wire.id)
        assert live.frm == "PB2.3"
        assert live.from_at is None
        assert len(session._undo) == depth + 1
    finally:
        win.close()


def test_gui_wire_end_drag_refuses_invalid_drops_with_zero_undo() -> None:
    session = Session.create("InvalidDrops")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=70, y=70)
    session.place("nema.contactors.coil", ref="M1", x=180, y=70)
    w_pin = session.wire("PB1.4", "M1.A1", number="101")
    w_rail = session.wire("L1", "PB1.3", number="100")
    session.set_wire_route(w_pin.id, [Point(x=115, y=94), Point(x=115, y=104), Point(x=155, y=104)])

    # Separate sheet with a 3-phase motor to exercise control/power conflict drop refusal.
    session.add_sheet("pwr", title="Power", kind="schematic", make_active=False)
    session.place("nema.contactors.coil", ref="K2", x=60, y=70, sheet_id="pwr")
    session.place("nema.operators.pb_no", ref="S2", x=140, y=70, sheet_id="pwr")
    session.place("iec.loads.motor_3ph", ref="MOT1", x=240, y=130, sheet_id="pwr")
    w_ctrl = session.wire("S2.4", "K2.A1", sheet_id="pwr", number="201")

    session._undo.clear()
    session._undo_tip = None

    app, win = _open_window(session)
    try:
        canvas = win.canvas
        geom = rail_geometry(session.sheet(), session.project)
        assert geom is not None

        # 1. Empty canvas drop -> refused, 0 undo steps.
        snap_before = _dump(session)
        canvas.select_ref(w_pin.id)
        app.processEvents()
        to_h = next(h for h in _handles(canvas, w_pin.id) if h.kind == "end" and h.which == "to")
        _drag_scene(canvas, app, to_h.pos(), QPointF(150.0, 180.0))
        assert _dump(session) == snap_before
        assert session._undo == []

        # 2. Self-loop onto wire's own other end -> refused, 0 undo steps.
        from_pt = _pin_item(canvas, "PB1", "4").scenePos()
        to_h = next(h for h in _handles(canvas, w_pin.id) if h.kind == "end" and h.which == "to")
        _drag_scene(canvas, app, to_h.pos(), from_pt)
        assert _dump(session) == snap_before
        assert session._undo == []

        # 3. Drag off starting pin and drop back on same starting pin -> no-op, keeps points, 0 undo steps.
        to_pt = _pin_item(canvas, "M1", "A1").scenePos()
        to_h = next(h for h in _handles(canvas, w_pin.id) if h.kind == "end" and h.which == "to")
        p0, p_out = _ensure_visible_pair(canvas, app, to_h.pos(), QPointF(to_pt.x() + 25.0, to_pt.y() + 25.0))
        p_back = canvas.mapFromScene(to_pt)
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
        QTest.mouseMove(canvas.viewport(), p_out)
        app.processEvents()
        QTest.mouseMove(canvas.viewport(), p_back)
        app.processEvents()
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p_back)
        app.processEvents()
        assert canvas.wire_from is None
        assert _dump(session) == snap_before
        assert session._undo == []

        # 4. Rail-to-rail short (L1 -> PB1.3, drag PB1.3 end onto L2) -> refused, 0 undo steps.
        canvas.select_ref(w_rail.id)
        app.processEvents()
        rail_to_h = next(h for h in _handles(canvas, w_rail.id) if h.kind == "end" and h.which == "to")
        _drag_scene(canvas, app, rail_to_h.pos(), QPointF(geom.right, 75.0))
        assert _dump(session) == snap_before
        assert session._undo == []

        # 5. Esc mid-drag cancels cleanly with 0 undo steps.
        to_h = next(h for h in _handles(canvas, w_rail.id) if h.kind == "end" and h.which == "to")
        p0, p1 = _ensure_visible_pair(canvas, app, to_h.pos(), QPointF(140.0, 140.0))
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
        QTest.mouseMove(canvas.viewport(), p1)
        app.processEvents()
        assert canvas._wire_end_drag is not None
        canvas.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
        app.processEvents()
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        app.processEvents()
        assert canvas._wire_end_drag is None
        assert _dump(session) == snap_before
        assert session._undo == []

        # 6. FocusOut mid-drag cancels cleanly with 0 undo steps.
        canvas.select_ref(w_rail.id)
        app.processEvents()
        to_h = next(h for h in _handles(canvas, w_rail.id) if h.kind == "end" and h.which == "to")
        p0, p1 = _ensure_visible_pair(canvas, app, to_h.pos(), QPointF(140.0, 140.0))
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
        QTest.mouseMove(canvas.viewport(), p1)
        app.processEvents()
        canvas.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
        app.processEvents()
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        app.processEvents()
        assert canvas._wire_end_drag is None
        assert _dump(session) == snap_before
        assert session._undo == []

        # 7. Control/power conflict (control wire onto motor U1) -> refused, 0 undo steps.
        win._switch_sheet("pwr")
        app.processEvents()
        canvas.fit_page()
        app.processEvents()
        snap_pwr = _dump(session)
        canvas.select_ref(w_ctrl.id)
        app.processEvents()
        mot_u1 = _pin_item(canvas, "MOT1", "U1").scenePos()
        ctrl_to_h = next(h for h in _handles(canvas, w_ctrl.id) if h.kind == "end" and h.which == "to")
        _drag_scene(canvas, app, ctrl_to_h.pos(), mot_u1)
        assert _dump(session) == snap_pwr
        assert session._undo == []
    finally:
        win.close()


def test_gui_two_point_segment_and_corner_drag_persist_and_undo(tmp_path: Path) -> None:
    session = Session.create("SegAndCorner")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=60, y=70)
    session.place("nema.contactors.coil", ref="M1", x=180, y=80)
    w = session.wire("PB1.4", "M1.A1", number="301")
    ax, ay = pin_world(
        session.sheet().component("PB1"),
        session.catalog.get("nema.operators.pb_no").pin("4"),
    )
    m1_dev = session.catalog.get("nema.contactors.coil")
    m1_comp = session.sheet().component("M1")
    m1_comp.y = ay - m1_dev.pin("A1").y
    _bx, by = pin_world(m1_comp, m1_dev.pin("A1"))
    assert abs(ay - by) < 0.05
    init_pts = iter_wire_points(session.sheet(), session.catalog, w, project=session.project)
    assert len(init_pts) == 2
    session._undo.clear()
    session._undo_tip = None

    app, win = _open_window(session)
    try:
        canvas = win.canvas
        w_item = _wire_item(canvas, w.id)
        mid = w_item.path().pointAtPercent(0.5)

        # Plain click on 2-pt wire does NOT push an undo step.
        _click_scene(canvas, app, mid)
        assert len(session._undo) == 0

        # Drag 2-point straight segment down by 20 mm -> inserts orthogonal jog points!
        before_seg = _dump(session)
        _drag_scene(canvas, app, mid, QPointF(mid.x(), mid.y() + 20.0))

        live = session.find_wire(wire_id=w.id)
        assert len(live.points) >= 2
        routed = iter_wire_points(session.sheet(), session.catalog, live, project=session.project)
        _assert_orthogonal(routed)
        end_a, end_b = _wire_endpoints_xy(session, w.id)
        assert abs(routed[0].x - end_a[0]) < 0.05 and abs(routed[0].y - end_a[1]) < 0.05
        assert abs(routed[-1].x - end_b[0]) < 0.05 and abs(routed[-1].y - end_b[1]) < 0.05
        # Pin exits stay horizontal along pin side for at least PIN_KEEP_MM (STUB).
        assert abs(routed[1].y - end_a[1]) < 0.05 and abs(routed[1].x - end_a[0]) >= PIN_KEEP_MM
        assert abs(routed[-2].y - end_b[1]) < 0.05 and abs(routed[-2].x - end_b[0]) >= PIN_KEEP_MM
        assert any(abs(p.y - (ay + 20.0)) <= 2.5 for p in routed[1:-1])
        assert len(session._undo) == 1

        # 1-step Undo and Redo for segment drag.
        win.undo_edit()
        app.processEvents()
        assert _dump(session) == before_seg
        assert session.find_wire(wire_id=w.id).points == []
        win.redo_edit()
        app.processEvents()
        assert len(session.find_wire(wire_id=w.id).points) >= 2

        # Corner handle drag on the newly created interior corner!
        canvas.select_ref(w.id)
        app.processEvents()
        corners = sorted(
            (h for h in _handles(canvas, w.id) if h.kind == "corner"),
            key=lambda h: h.corner_index,
        )
        assert len(corners) >= 2
        assert all(h.zValue() >= 30 for h in corners)

        # Plain click on corner handle adds 0 undo steps.
        _click_scene(canvas, app, corners[0].pos())
        assert len(session._undo) == 1

        before_corner = _dump(session)
        corner0 = corners[0]
        target_corner = QPointF(corner0.pos().x() + 15.0, corner0.pos().y() + 10.0)
        _drag_scene(canvas, app, corner0.pos(), target_corner)

        live = session.find_wire(wire_id=w.id)
        routed2 = iter_wire_points(session.sheet(), session.catalog, live, project=session.project)
        _assert_orthogonal(routed2)
        assert abs(routed2[0].x - end_a[0]) < 0.05 and abs(routed2[0].y - end_a[1]) < 0.05
        assert abs(routed2[-1].x - end_b[0]) < 0.05 and abs(routed2[-1].y - end_b[1]) < 0.05
        assert abs(routed2[1].y - end_a[1]) < 0.05 and abs(routed2[1].x - end_a[0]) >= PIN_KEEP_MM
        assert abs(routed2[-2].y - end_b[1]) < 0.05 and abs(routed2[-2].x - end_b[0]) >= PIN_KEEP_MM
        assert any(abs(p.x - target_corner.x()) < 0.5 for p in routed2[1:-1])
        assert len(session._undo) == 2

        # 1-step Undo and Redo for corner drag.
        saved_after_corner = [(p.x, p.y) for p in live.points]
        win.undo_edit()
        app.processEvents()
        assert _dump(session) == before_corner
        win.redo_edit()
        app.processEvents()
        assert [(p.x, p.y) for p in session.find_wire(wire_id=w.id).points] == saved_after_corner

        # Esc during segment drag cancels cleanly with 0 undo steps.
        snap_before_esc = _dump(session)
        w_item = _wire_item(canvas, w.id)
        seg_mid = w_item.path().pointAtPercent(0.5)
        p0, p1 = _ensure_visible_pair(canvas, app, seg_mid, QPointF(seg_mid.x(), seg_mid.y() + 15.0))
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
        QTest.mouseMove(canvas.viewport(), p1)
        app.processEvents()
        canvas.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
        app.processEvents()
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        app.processEvents()
        assert _dump(session) == snap_before_esc
        assert len(session._undo) == 2

        # Save and reload persistence of wire.points.
        out_file = tmp_path / "persisted_wire_drag.pcad.yaml"
        session.save(out_file)
        reloaded = Session.open(out_file)
        rw = reloaded.find_wire(wire_id=w.id)
        assert [(round(p.x, 3), round(p.y, 3)) for p in rw.points] == [
            (round(p.x, 3), round(p.y, 3)) for p in session.find_wire(wire_id=w.id).points
        ]
        reloaded_route = iter_wire_points(reloaded.sheet(), reloaded.catalog, rw, project=reloaded.project)
        assert [(round(p.x, 3), round(p.y, 3)) for p in reloaded_route] == [
            (round(p.x, 3), round(p.y, 3)) for p in routed2
        ]

        # Handle cleanup across sheet switch and release_scenes.
        session.add_sheet("page2", make_active=False)
        win._switch_sheet("page2")
        app.processEvents()
        assert _handles(canvas) == []
        win._switch_sheet(session.project.sheets[0].id)
        app.processEvents()
        assert _handles(canvas) == []
    finally:
        win.close()


def test_gui_iec_vertical_segment_and_rail_drag() -> None:
    session = Session.create("IECDrag")
    session.set_title_block(standard="IEC", rails="L+/0V")
    session.sheet().drawing_style = "iec"
    session.place("iec.operators.pb_no", ref="S1", x=100, y=50)
    session.place("iec.contactors.coil", ref="K1", x=100, y=130)
    s1_14 = pin_world(session.sheet().component("S1"), session.catalog.get("iec.operators.pb_no").pin("14"))
    k1_dev = session.catalog.get("iec.contactors.coil")
    k1 = session.sheet().component("K1")
    k1.x = s1_14[0] - k1_dev.pin("A1").x
    w_vert = session.wire("S1.14", "K1.A1", number="11")

    session.place("iec.operators.pb_no", ref="S2", x=220, y=50)
    session.place("iec.contactors.coil", ref="K2", x=220, y=130)
    w_top = session.wire("S2.13", "K2.A2", number="10")
    assert len(iter_wire_points(session.sheet(), session.catalog, w_vert, project=session.project)) == 2
    session._undo.clear()
    session._undo_tip = None

    app, win = _open_window(session)
    try:
        canvas = win.canvas
        geom = rail_geometry(session.sheet(), session.project)
        assert geom is not None and geom.kind == "iec"

        # Drag vertical 2-point segment horizontally -> inserts orthogonal jog points in 1 undo step.
        w_item = _wire_item(canvas, w_vert.id)
        mid = w_item.path().pointAtPercent(0.5)
        before_v = _dump(session)
        _drag_scene(canvas, app, mid, QPointF(mid.x() + 20.0, mid.y()))
        live_v = session.find_wire(wire_id=w_vert.id)
        assert len(live_v.points) >= 2
        routed_v = iter_wire_points(session.sheet(), session.catalog, live_v, project=session.project)
        _assert_orthogonal(routed_v)
        assert len(session._undo) == 1

        win.undo_edit()
        app.processEvents()
        assert _dump(session) == before_v

        # Drag wire end onto horizontal IEC rail L+.
        canvas.select_ref(w_top.id)
        app.processEvents()
        assert len(_handles(canvas, w_top.id)) >= 2
        to_h = next(h for h in _handles(canvas, w_top.id) if h.kind == "end" and h.which == "to")
        rail_pt = QPointF(245.0, geom.top)
        _drag_scene(canvas, app, to_h.pos(), rail_pt)
        live_top = session.find_wire(wire_id=w_top.id)
        assert live_top.to == "L+"
        assert live_top.to_at is not None
        assert abs(live_top.to_at.y - geom.top) < 0.2
        assert len(session._undo) == 1

        # Switching to Wire mode hides wire handles so pins are exposed without clearing selection.
        canvas.select_ref(w_top.id)
        app.processEvents()
        assert _handles(canvas, w_top.id)
        canvas.set_wire_mode()
        app.processEvents()
        assert _handles(canvas) == []
        assert _wire_item(canvas, w_top.id).isSelected()
        canvas.set_select_mode()
        app.processEvents()
        assert _handles(canvas, w_top.id)
    finally:
        win.close()


def test_gui_end_handle_pin_click_daisy_chain_unlocked_slide_and_wire_mode() -> None:
    """B1: Plain-clicking a pin under a selected wire's end handle starts a wire or slides an unlocked pin."""
    session = Session.create("B1PinClick")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=60, y=70)
    session.place("nema.operators.pb_no", ref="PB2", x=60, y=120)
    session.place("nema.contactors.coil", ref="M1", x=180, y=70)
    session.place("nema.contactors.coil", ref="M2", x=180, y=120)
    w = session.wire("PB1.4", "M1.A1")
    session._undo.clear()
    session._undo_tip = None

    app, win = _open_window(session)
    try:
        canvas = win.canvas
        canvas.select_ref(w.id)
        app.processEvents()
        to_h = next(h for h in _handles(canvas, w.id) if h.kind == "end" and h.which == "to")

        # 1. Select mode, locked pin: plain click on M1.A1 under selected wire's end handle starts daisy-chain wire.
        _click_scene(canvas, app, to_h.pos())
        assert canvas.wire_from == "M1.A1"
        assert _handles(canvas) == []
        assert len(session._undo) == 0
        _click_scene(canvas, app, _pin_item(canvas, "M2", "A1").scenePos())
        assert canvas.wire_from is None
        assert len(session.sheet().wires) == 2
        assert {session.sheet().wires[-1].frm, session.sheet().wires[-1].to} == {"M1.A1", "M2.A1"}
        assert len(session._undo) == 1
        win.undo_edit()
        app.processEvents()
        assert len(session.sheet().wires) == 1

        # 2. Select mode, unlocked pin: dragging slides the pin instead of dragging the wire end;
        #    plain-clicking still falls back to starting a wire.
        session.set_pins_locked("PB1", False)
        canvas._apply_pin_mode()
        session._undo.clear()
        session._undo_tip = None
        canvas.select_ref(w.id)
        app.processEvents()
        pb1_pin = _pin_item(canvas, "PB1", "4")
        start_xy = pb1_pin.scenePos()
        _drag_scene(canvas, app, start_xy, QPointF(start_xy.x(), start_xy.y() + 8.0))
        comp = session.sheet().component("PB1")
        assert "4" in comp.pin_overrides
        assert session.find_wire(wire_id=w.id).frm == "PB1.4"
        assert len(session._undo) == 1
        win.undo_edit()
        app.processEvents()

        # Plain click on unlocked pin under selected wire's end handle starts wiring.
        canvas.select_ref(w.id)
        app.processEvents()
        pb1_pin = _pin_item(canvas, "PB1", "4")
        _click_scene(canvas, app, pb1_pin.scenePos())
        assert canvas.wire_from == "PB1.4"
        canvas.cancel_wire()
        app.processEvents()

        # 3. Wire mode: clicking a wire does not spawn end handles, so clicking its terminal immediately starts wiring.
        session.set_pins_locked("PB1", True)
        canvas._apply_pin_mode()
        canvas.set_wire_mode()
        app.processEvents()
        w_item = _wire_item(canvas, w.id)
        _click_scene(canvas, app, w_item.path().pointAtPercent(0.5))
        assert _handles(canvas) == []
        _click_scene(canvas, app, _pin_item(canvas, "PB1", "4").scenePos())
        assert canvas.wire_from == "PB1.4"
        _click_scene(canvas, app, _pin_item(canvas, "PB2", "4").scenePos())
        assert canvas.wire_from is None
        assert len(session.sheet().wires) == 2
    finally:
        win.close()


def test_reconnect_wire_renumbers_unlocked_preserves_locked_and_orients_two_pin_flow() -> None:
    """B2: reconnect_wire updates unlocked wire numbers, preserves locked numbers, runs orient_two_pin_flow, and skips no-op logging."""
    session = Session.create("B2Numbering")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=60, y=50)
    session.place("nema.operators.pb_no", ref="PB2", x=60, y=100)
    session.place("nema.operators.pb_no", ref="PB3", x=60, y=150)
    session.place("nema.contactors.coil", ref="M1", x=180, y=50)
    session.place("nema.contactors.coil", ref="M2", x=180, y=100)
    session.place("nema.operators.pilot_light", ref="LT1", x=240, y=50)
    session.place("nema.operators.pilot_light", ref="LT2", x=240, y=100)

    w1 = session.wire("PB1.4", "M1.A1")
    w4 = session.wire("PB1.4", "LT1.X1")
    w2 = session.wire("PB2.4", "M2.A1")
    w_locked = session.wire("PB3.3", "LT2.X1", number="555")
    assert not w1.number_locked and w1.number == w4.number and w1.number
    assert not w2.number_locked and w2.number and w2.number != w1.number
    assert w_locked.number_locked and w_locked.number == "555"
    orig_w1_num = w1.number
    orig_w2_num = w2.number
    session._undo.clear()
    session._undo_tip = None

    # No-op reconnect logs 0 activity entries and 0 undo steps.
    act_before = len(session.activity)
    session.reconnect_wire(w1.id, "from", "PB1.4")
    assert len(session.activity) == act_before
    assert session._undo == []

    # Reconnect unlocked w1 onto PB2.4 (shares node with w2) -> adopts w2's number, 0 conflicts.
    before_adopt = _dump(session)
    session.reconnect_wire(w1.id, "from", "PB2.4")
    assert w1.number == orig_w2_num
    assert not w1.number_locked
    assert w4.number == orig_w1_num
    assert wire_number_conflicts(session.project, session.catalog) == []
    assert len(session.activity) == act_before + 1
    assert len(session._undo) == 1
    assert session.undo()
    assert _dump(session) == before_adopt

    # Re-fetch live wire objects after undo replaced session.project.
    w1 = session.find_wire(wire_id=w1.id)
    w4 = session.find_wire(wire_id=w4.id)
    w_locked = session.find_wire(wire_id=w_locked.id)

    # Reconnect unlocked w1 onto isolated node PB3.4 (while w4 keeps orig_w1_num on PB1.4) -> gets fresh unique number.
    session.reconnect_wire(w1.id, "from", "PB3.4")
    assert w1.number and w1.number != orig_w1_num and w1.number != orig_w2_num
    assert w4.number == orig_w1_num
    assert wire_number_conflicts(session.project, session.catalog) == []

    # Split-network case: disconnecting a bridge wire splits an unlocked network cleanly with 0 conflicts.
    w_tail = session.wire("M1.A1", "LT2.X2")
    assert w_tail.number == w1.number
    w_head = session.wire("PB3.4", "LT1.X2")
    assert w_head.number == w1.number
    shared_num = w1.number
    session.reconnect_wire(w1.id, "from", "PB1.3")
    assert wire_number_conflicts(session.project, session.catalog) == []
    assert w_head.number == shared_num
    assert w1.number == w_tail.number and w1.number != shared_num

    # Locked wire preserves its number when reconnected.
    session.reconnect_wire(w_locked.id, "from", "PB2.3")
    assert w_locked.number == "555" and w_locked.number_locked

    # Two-pin flow orientation (orient_two_pin_flow) updates on reconnect & syncs in GUI.
    s_flow = Session.create("Flow")
    s_flow.place("iec.protection.fuse_10a", ref="F1", x=100, y=60)
    s_flow.place("iec.operators.pb_no", ref="S_H", x=160, y=60)
    s_flow.place("iec.operators.pb_no", ref="S_V", x=100, y=140)
    wf = s_flow.wire("F1.2", "S_H.13")
    assert s_flow.sheet().component("F1").rotation == 0
    s_flow._undo.clear()
    s_flow._undo_tip = None
    s_flow.reconnect_wire(wf.id, "to", "S_V.13")
    assert s_flow.sheet().component("F1").rotation == 90
    assert s_flow.undo()
    assert s_flow.sheet().component("F1").rotation == 0

    # wire() shares validate_wire_endpoints: freeform bare L1->L2 allowed, ladder L1->L2 refused.
    s_free = Session.create("Free")
    s_free.sheet().drawing_style = "freeform"
    w_free = s_free.wire("L1", "L2")
    assert (w_free.frm, w_free.to) == ("L1", "L2")


def test_gui_focus_out_right_click_and_lost_release_cancel_route_drags() -> None:
    """S2: FocusOut, right-click, and lost-release mouseMove cancel corner and segment drags with 0 undo."""
    from PySide6.QtGui import QMouseEvent

    session = Session.create("S2Cancel")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=60, y=70)
    session.place("nema.contactors.coil", ref="M1", x=180, y=70)
    w = session.wire("PB1.4", "M1.A1", number="401")
    session.set_wire_route(w.id, [Point(x=110, y=94), Point(x=110, y=114), Point(x=160, y=114)])
    session._undo.clear()
    session._undo_tip = None

    app, win = _open_window(session)
    try:
        canvas = win.canvas
        snap0 = _dump(session)

        def _begin_seg_drag():
            w_item = _wire_item(canvas, w.id)
            mid = w_item.path().pointAtPercent(0.5)
            p0, p1 = _ensure_visible_pair(canvas, app, mid, QPointF(mid.x(), mid.y() + 20.0))
            QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
            QTest.mouseMove(canvas.viewport(), p1)
            app.processEvents()
            assert canvas._pull_wire is not None and canvas._pull_moved
            assert session.command_open()
            return p1

        def _begin_corner_drag():
            canvas.select_ref(w.id)
            app.processEvents()
            corner = next(h for h in _handles(canvas, w.id) if h.kind == "corner")
            p0, p1 = _ensure_visible_pair(
                canvas, app, corner.pos(), QPointF(corner.pos().x() + 20.0, corner.pos().y() + 15.0)
            )
            QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
            QTest.mouseMove(canvas.viewport(), p1)
            app.processEvents()
            assert canvas._corner_wire is not None and canvas._corner_moved
            assert session.command_open()
            return p1

        # 1. FocusOut cancels segment drag with 0 undo.
        p1 = _begin_seg_drag()
        canvas.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
        app.processEvents()
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        app.processEvents()
        assert canvas._pull_wire is None and not canvas._gesture_edit and not session.command_open()
        assert _dump(session) == snap0 and session._undo == []

        # 2. Right-click cancels segment drag with 0 undo.
        p1 = _begin_seg_drag()
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, p1)
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, p1)
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        app.processEvents()
        assert canvas._pull_wire is None and not session.command_open()
        assert _dump(session) == snap0 and session._undo == []

        # 3. Lost-release mouseMove (NoButton) cancels segment drag with 0 undo.
        p1 = _begin_seg_drag()
        pos_f = QPointF(p1.x() + 5, p1.y() + 5)
        QApplication.sendEvent(
            canvas.viewport(),
            QMouseEvent(
                QEvent.Type.MouseMove,
                pos_f,
                canvas.viewport().mapToGlobal(pos_f),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )
        app.processEvents()
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        app.processEvents()
        assert canvas._pull_wire is None and not session.command_open()
        assert _dump(session) == snap0 and session._undo == []

        # 4. FocusOut cancels corner drag with 0 undo.
        p1 = _begin_corner_drag()
        canvas.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
        app.processEvents()
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        app.processEvents()
        assert canvas._corner_wire is None and not canvas._gesture_edit and not session.command_open()
        assert _dump(session) == snap0 and session._undo == []

        # 5. Right-click cancels corner drag with 0 undo.
        p1 = _begin_corner_drag()
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, p1)
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, p1)
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        app.processEvents()
        assert canvas._corner_wire is None and not session.command_open()
        assert _dump(session) == snap0 and session._undo == []

        # 6. Lost-release mouseMove (NoButton) cancels corner drag with 0 undo.
        p1 = _begin_corner_drag()
        pos_f = QPointF(p1.x() + 5, p1.y() + 5)
        QApplication.sendEvent(
            canvas.viewport(),
            QMouseEvent(
                QEvent.Type.MouseMove,
                pos_f,
                canvas.viewport().mapToGlobal(pos_f),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )
        app.processEvents()
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        app.processEvents()
        assert canvas._corner_wire is None and not session.command_open()
        assert _dump(session) == snap0 and session._undo == []

        # Subsequent edit still records undo cleanly.
        session.place("nema.operators.pb_no", ref="PB9", x=60, y=150)
        assert len(session._undo) == 1
    finally:
        win.close()


def test_gui_subthreshold_jitter_offgrid_wire_no_undo_and_delta_snap() -> None:
    """S3: 1px click jitter on an off-grid auto-routed wire or corner produces 0 undo and keeps auto-route."""
    session = Session.create("S3Jitter")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    # Place off the 2.5 mm grid so the auto-routed horizontal wire sits at an off-grid Y.
    session.place("nema.operators.pb_no", ref="PB1", x=61.3, y=71.3, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=181.3, y=71.3, snap=False)
    pb_pin = session.catalog.get("nema.operators.pb_no").pin("4")
    m1_pin = session.catalog.get("nema.contactors.coil").pin("A1")
    _ax, ay = pin_world(session.sheet().component("PB1"), pb_pin)
    session.sheet().component("M1").y = ay - m1_pin.y
    assert abs((ay % 2.5) - 0.0) > 0.2 and abs((ay % 2.5) - 2.5) > 0.2
    w = session.wire("PB1.4", "M1.A1", number="305")
    assert w.points == []
    session._undo.clear()
    session._undo_tip = None

    app, win = _open_window(session)
    try:
        canvas = win.canvas
        w_item = _wire_item(canvas, w.id)
        mid = w_item.path().pointAtPercent(0.5)
        canvas.centerOn(mid)
        app.processEvents()
        p0 = canvas.mapFromScene(mid)
        p_jitter = QPoint(p0.x() + 1, p0.y() + 1)

        # 1px jitter on off-grid auto-routed segment -> 0 undo, stays auto-routed (points == []).
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
        QTest.mouseMove(canvas.viewport(), p_jitter)
        app.processEvents()
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p_jitter)
        app.processEvents()
        assert session._undo == []
        assert session.find_wire(wire_id=w.id).points == []

        # Drag segment down by 10 mm -> delta-snaps from original off-grid Y (preserves off-grid offset).
        _drag_scene(canvas, app, mid, QPointF(mid.x(), mid.y() + 10.0))
        live = session.find_wire(wire_id=w.id)
        assert len(session._undo) == 1
        assert any(abs(p.y - (ay + 10.0)) < 0.15 for p in live.points)

        # 1px jitter on an off-grid corner handle -> 0 undo, points unchanged.
        canvas.select_ref(w.id)
        app.processEvents()
        corner = next(h for h in _handles(canvas, w.id) if h.kind == "corner")
        pts_before = [(p.x, p.y) for p in live.points]
        canvas.centerOn(corner.pos())
        app.processEvents()
        cp0 = canvas.mapFromScene(corner.pos())
        cp_j = QPoint(cp0.x() + 1, cp0.y() + 1)
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, cp0)
        QTest.mouseMove(canvas.viewport(), cp_j)
        app.processEvents()
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, cp_j)
        app.processEvents()
        assert len(session._undo) == 1
        assert [(p.x, p.y) for p in session.find_wire(wire_id=w.id).points] == pts_before
    finally:
        win.close()


def test_gui_multiselect_hides_handles_and_no_redundant_handle_rebuilds(monkeypatch) -> None:
    """S4 & S5: Multi-select creates 0 handles; pre-threshold and unchanged moves do not rebuild handles."""
    session = Session.create("S4S5Perf")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=60, y=60)
    session.place("nema.operators.pb_no", ref="PB2", x=60, y=110)
    session.place("nema.contactors.coil", ref="M1", x=180, y=60)
    session.place("nema.contactors.coil", ref="M2", x=180, y=110)
    w1 = session.wire("PB1.4", "M1.A1")
    w2 = session.wire("PB2.4", "M2.A1")
    session._undo.clear()
    session._undo_tip = None

    app, win = _open_window(session)
    try:
        canvas = win.canvas
        wi1 = _wire_item(canvas, w1.id)
        wi2 = _wire_item(canvas, w2.id)

        # Single selection shows handles.
        canvas.scene.clearSelection()
        wi1.setSelected(True)
        app.processEvents()
        assert len(_handles(canvas, w1.id)) >= 2

        # Multi-selecting 2 wires hides all handles.
        wi2.setSelected(True)
        app.processEvents()
        assert _handles(canvas) == []

        # Back to single selection restores handles.
        wi2.setSelected(False)
        app.processEvents()
        assert len(_handles(canvas, w1.id)) >= 2

        # S5: Spy _sync_wire_handles during pre-threshold moves and steady drag.
        calls = 0
        orig_sync = canvas._sync_wire_handles

        def spy_sync(*args, **kwargs):
            nonlocal calls
            calls += 1
            return orig_sync(*args, **kwargs)

        monkeypatch.setattr(canvas, "_sync_wire_handles", spy_sync)
        mid = wi1.path().pointAtPercent(0.5)
        canvas.centerOn(mid)
        app.processEvents()
        p0 = canvas.mapFromScene(mid)
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
        for step in range(5):
            QTest.mouseMove(canvas.viewport(), QPoint(p0.x() + (step % 2), p0.y() + 1))
            app.processEvents()
        assert calls == 0  # Pre-threshold jitter never rebuilds handles.
        # Move past threshold and keep dragging: still 0 _sync_wire_handles calls.
        for dy in (20, 35, 50):
            QTest.mouseMove(canvas.viewport(), QPoint(p0.x(), p0.y() + dy))
            app.processEvents()
        assert calls == 0
        canvas.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
        app.processEvents()
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
        app.processEvents()
    finally:
        win.close()


def test_gui_zoomed_out_handle_cap_and_closer_pin_wins() -> None:
    """S6: Handle hit radius stays capped when zoomed out and closer adjacent 5 mm pins win."""
    session = Session.create("S6Zoom")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=60, y=60)
    session.place("nema.contactors.coil", ref="M1", x=180, y=60)
    session.place("nema.contactors.coil", ref="M2", x=180, y=65, snap=False)
    w = session.wire("PB1.4", "M1.A1")
    p1 = pin_world(session.sheet().component("M1"), session.catalog.get("nema.contactors.coil").pin("A1"))
    p2 = pin_world(session.sheet().component("M2"), session.catalog.get("nema.contactors.coil").pin("A1"))
    assert abs((p2[1] - p1[1]) - 5.0) < 0.05

    app, win = _open_window(session)
    try:
        canvas = win.canvas
        canvas.select_ref(w.id)
        app.processEvents()
        # Zoom way out WITHOUT calling _refresh_wire_hit_geometry manually.
        canvas.resetTransform()
        canvas.scale(0.5, 0.5)
        app.processEvents()

        to_h = next(h for h in _handles(canvas, w.id) if h.kind == "end" and h.which == "to")
        assert to_h.hit_radius_scene() <= 3.5 + 1e-6

        # Clicking the adjacent 5 mm pin M2.A1 must hit M2.A1, not M1.A1's handle.
        m2_pos = _pin_item(canvas, "M2", "A1").scenePos()
        _click_scene(canvas, app, m2_pos)
        assert canvas.wire_from == "M2.A1"
        canvas.cancel_wire()
        app.processEvents()

        # Clicking 2 mm from M2.A1 (3 mm from M1.A1, inside handle cap): closer pin M2.A1 wins.
        near_m2 = QPointF(m2_pos.x(), m2_pos.y() - 2.0)
        _click_scene(canvas, app, near_m2)
        assert canvas.wire_from == "M2.A1"
    finally:
        win.close()


def test_gui_rail_run_drag_updates_landing_preview_and_junction_dot() -> None:
    """S7: Dragging a rail-attached run updates from_at/to_at and keeps preview, route, and junction dot aligned."""

    def _visible_junctions(canvas) -> list[tuple[float, float]]:
        return [
            (item.rect().center().x(), item.rect().center().y())
            for item in canvas.scene.items()
            if isinstance(item, RailJunctionItem) and item.isVisible()
        ]

    # 1. NEMA ladder vertical rail L1 -> PB1.3 horizontal rung dragged down by 20 mm.
    s_lad = Session.create("S7Ladder")
    s_lad.set_title_block(drawing_style="ladder", rails="L1/L2")
    s_lad.place("nema.operators.pb_no", ref="PB1", x=90, y=70)
    w_lad = s_lad.wire("L1", "PB1.3", number="100")
    geom_l = rail_geometry(s_lad.sheet(), s_lad.project)
    assert geom_l is not None and geom_l.kind == "ladder"
    s_lad._undo.clear()
    s_lad._undo_tip = None

    app, win = _open_window(s_lad)
    try:
        canvas = win.canvas
        w_item = _wire_item(canvas, w_lad.id)
        mid = w_item.path().pointAtPercent(0.5)
        old_y = mid.y()
        target = QPointF(mid.x(), old_y + 20.0)
        before = _dump(s_lad)

        # Press and drag (without releasing yet) to inspect live preview & junction dot.
        p0, p1 = _ensure_visible_pair(canvas, app, mid, target)
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
        QTest.mouseMove(canvas.viewport(), p1)
        app.processEvents()
        preview_pts = list(getattr(w_item, "_live_pts", []) or [])
        assert len(preview_pts) >= 3
        assert abs(preview_pts[0].x - geom_l.left) < 0.2
        assert abs(preview_pts[0].y - (old_y + 20.0)) < 1.5
        prev_dots = _visible_junctions(canvas)
        assert any(abs(jx - geom_l.left) < 0.5 and abs(jy - preview_pts[0].y) < 0.5 for jx, jy in prev_dots)
        assert not any(abs(jx - geom_l.left) < 0.5 and abs(jy - old_y) < 0.5 for jx, jy in prev_dots)

        # Release to commit.
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        app.processEvents()
        live = s_lad.find_wire(wire_id=w_lad.id)
        assert live.from_at is not None
        assert abs(live.from_at.x - geom_l.left) < 0.2
        assert abs(live.from_at.y - preview_pts[0].y) < 0.1
        committed = iter_wire_points(s_lad.sheet(), s_lad.catalog, live, project=s_lad.project)
        assert len(committed) == len(preview_pts)
        for a, b in zip(committed, preview_pts):
            assert abs(a.x - b.x) < 0.15 and abs(a.y - b.y) < 0.15
        dots = _visible_junctions(canvas)
        assert any(abs(jx - geom_l.left) < 0.5 and abs(jy - live.from_at.y) < 0.5 for jx, jy in dots)
        assert not any(abs(jx - geom_l.left) < 0.5 and abs(jy - old_y) < 0.5 for jx, jy in dots)
        assert len(s_lad._undo) == 1

        win.undo_edit()
        app.processEvents()
        assert _dump(s_lad) == before
        assert any(abs(jx - geom_l.left) < 0.5 and abs(jy - old_y) < 0.5 for jx, jy in _visible_junctions(canvas))
    finally:
        win.close()

    # 2. IEC horizontal rail L+ -> S1.13 vertical drop dragged right by 20 mm.
    s_iec = Session.create("S7IEC")
    s_iec.set_title_block(standard="IEC", rails="L+/0V")
    s_iec.sheet().drawing_style = "iec"
    s_iec.place("iec.operators.pb_no", ref="S1", x=100, y=70)
    w_iec = s_iec.wire("L+", "S1.13", number="10")
    geom_i = rail_geometry(s_iec.sheet(), s_iec.project)
    assert geom_i is not None and geom_i.kind == "iec"
    s_iec._undo.clear()
    s_iec._undo_tip = None

    app, win = _open_window(s_iec)
    try:
        canvas = win.canvas
        w_item = _wire_item(canvas, w_iec.id)
        mid = w_item.path().pointAtPercent(0.5)
        old_x = mid.x()
        before = _dump(s_iec)
        _drag_scene(canvas, app, mid, QPointF(old_x + 20.0, mid.y()))
        live = s_iec.find_wire(wire_id=w_iec.id)
        assert live.from_at is not None
        assert abs(live.from_at.y - geom_i.top) < 0.2
        assert abs(live.from_at.x - (old_x + 20.0)) < 1.5
        committed = iter_wire_points(s_iec.sheet(), s_iec.catalog, live, project=s_iec.project)
        _assert_orthogonal(committed)
        assert abs(committed[0].x - live.from_at.x) < 0.15
        dots = _visible_junctions(canvas)
        assert any(abs(jx - live.from_at.x) < 0.5 and abs(jy - geom_i.top) < 0.5 for jx, jy in dots)
        assert len(s_iec._undo) == 1
        win.undo_edit()
        app.processEvents()
        assert _dump(s_iec) == before
    finally:
        win.close()


def test_gui_reload_mid_drag_and_deleted_wire_commit_safety() -> None:
    """S8: reload/_reload_sheet mid-drag drops open gestures; deleting a wire mid-drag doesn't leak an undo lock."""
    session = Session.create("S8Safety")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=60, y=70)
    session.place("nema.contactors.coil", ref="M1", x=180, y=70)
    w = session.wire("PB1.4", "M1.A1", number="601")
    session.set_wire_route(w.id, [Point(x=110, y=94), Point(x=110, y=114), Point(x=160, y=114)])
    session._undo.clear()
    session._undo_tip = None

    app, win = _open_window(session)
    try:
        canvas = win.canvas
        snap0 = _dump(session)

        # 1. reload() mid-segment-drag drops open gesture cleanly.
        w_item = _wire_item(canvas, w.id)
        mid = w_item.path().pointAtPercent(0.5)
        p0, p1 = _ensure_visible_pair(canvas, app, mid, QPointF(mid.x(), mid.y() + 20.0))
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
        QTest.mouseMove(canvas.viewport(), p1)
        app.processEvents()
        assert canvas._pull_wire is not None and session.command_open()
        canvas.reload()
        app.processEvents()
        assert canvas._pull_wire is None and not canvas._gesture_edit and not session.command_open()
        assert _dump(session) == snap0 and session._undo == []
        # Stale mouse release after reload must not crash or push undo.
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        app.processEvents()
        assert session._undo == []

        # 2. _reload_sheet() mid-corner-drag drops open gesture cleanly.
        canvas.select_ref(w.id)
        app.processEvents()
        corner = next(h for h in _handles(canvas, w.id) if h.kind == "corner")
        c0, c1 = _ensure_visible_pair(
            canvas, app, corner.pos(), QPointF(corner.pos().x() + 15.0, corner.pos().y() + 10.0)
        )
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c0)
        QTest.mouseMove(canvas.viewport(), c1)
        app.processEvents()
        assert canvas._corner_wire is not None and session.command_open()
        canvas._reload_sheet()
        app.processEvents()
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c1)
        app.processEvents()
        assert canvas._corner_wire is None and not canvas._gesture_edit and not session.command_open()
        assert _dump(session) == snap0 and session._undo == []

        # 3. Stale corner index on a handle resolves by position safely.
        canvas.select_ref(w.id)
        app.processEvents()
        corner = next(h for h in _handles(canvas, w.id) if h.kind == "corner")
        corner.corner_index = 999
        c0, c1 = _ensure_visible_pair(
            canvas, app, corner.pos(), QPointF(corner.pos().x() + 15.0, corner.pos().y())
        )
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c0)
        app.processEvents()
        assert canvas._corner_wire is not None and canvas._corner_index != 999
        canvas.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
        app.processEvents()
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c0)
        app.processEvents()

        # 4. Wire deleted externally mid-drag -> release handles KeyError without leaving undo command open.
        w_item = _wire_item(canvas, w.id)
        mid = w_item.path().pointAtPercent(0.5)
        p0, p1 = _ensure_visible_pair(canvas, app, mid, QPointF(mid.x(), mid.y() + 20.0))
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
        QTest.mouseMove(canvas.viewport(), p1)
        app.processEvents()
        assert canvas._pull_wire is not None and session.command_open()
        session.sheet().wires.clear()
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        app.processEvents()
        assert canvas._pull_wire is None and not canvas._gesture_edit and not session.command_open()
        depth = len(session._undo)
        session.place("nema.operators.pb_no", ref="PB8", x=60, y=140)
        assert len(session._undo) == depth + 1
    finally:
        win.close()


def test_reconnect_wire_refuses_conflicting_net_api_and_gui() -> None:
    """S9: Reconnecting onto a pin or rail whose existing wires carry a different net is refused."""
    session = Session.create("S9Nets")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=60, y=50)
    session.place("nema.operators.pb_no", ref="PB2", x=60, y=100)
    session.place("nema.contactors.coil", ref="M1", x=180, y=50)
    session.place("nema.contactors.coil", ref="M2", x=180, y=100)
    session.place("nema.contactors.coil", ref="M3", x=180, y=150)

    w_start = session.wire("PB1.4", "M1.A1", net="START", number="701")
    session.wire("PB2.4", "M2.A1", net="STOP", number="702")
    session.wire("PB2.3", "M3.A1", net="START", number="701")
    w_rail = session.wire("L1", "PB1.3", net="L1", number="100")
    session._undo.clear()
    session._undo_tip = None

    # API refuses conflicting net on target pin and conflicting rail net (0 undo steps).
    with pytest.raises(ValueError, match="already on net 'STOP'"):
        session.reconnect_wire(w_start.id, "to", "M2.A1")
    with pytest.raises(ValueError, match="rail L2"):
        session.reconnect_wire(w_rail.id, "from", "L2")
    assert session._undo == []

    # Same net on target pin is allowed.
    before_ok = _dump(session)
    session.reconnect_wire(w_start.id, "to", "M3.A1")
    assert w_start.to == "M3.A1"
    assert len(session._undo) == 1
    assert session.undo()
    assert _dump(session) == before_ok
    session._undo.clear()
    session._undo_tip = None

    # GUI end-drag onto M2.A1 (net STOP) refuses drop with 0 undo steps.
    app, win = _open_window(session)
    try:
        canvas = win.canvas
        snap0 = _dump(session)
        canvas.select_ref(w_start.id)
        app.processEvents()
        to_h = next(h for h in _handles(canvas, w_start.id) if h.kind == "end" and h.which == "to")
        m2_a1 = _pin_item(canvas, "M2", "A1").scenePos()
        _drag_scene(canvas, app, to_h.pos(), m2_a1)
        assert _dump(session) == snap0
        assert session._undo == []
        assert "STOP" in win.statusBar().currentMessage()
    finally:
        win.close()


def _assert_no_retrace(pts: list[Point], tol: float = 0.2) -> None:
    """Assert consecutive segments do not double back along themselves."""
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        if abs(a.y - b.y) <= tol and abs(b.y - c.y) <= tol:
            dx1, dx2 = b.x - a.x, c.x - b.x
            assert dx1 * dx2 >= -tol, (a, b, c)
        if abs(a.x - b.x) <= tol and abs(b.x - c.x) <= tol:
            dy1, dy2 = b.y - a.y, c.y - b.y
            assert dy1 * dy2 >= -tol, (a, b, c)


def _seg_len(a: Point, b: Point) -> float:
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5


def test_move_wire_corner_to_end_stub_clamping_stays_orthogonal_and_gui() -> None:
    """Blocker #1: clamping a to-adjacent corner keeps all segments orthogonal and respects STUB."""

    # 1. 3-point L-bend dragged within STUB of (and past) b0.
    l3 = [Point(x=0.0, y=0.0), Point(x=50.0, y=0.0), Point(x=50.0, y=40.0)]
    for target_y in (38.0, 52.0):
        out = move_wire_corner(l3, 1, 50.0, target_y)
        _assert_orthogonal(out, tol=1e-3)
        _assert_no_retrace(out)
        assert (out[0].x, out[0].y) == (0.0, 0.0)
        assert (out[-1].x, out[-1].y) == (50.0, 40.0)
        assert _seg_len(out[0], out[1]) >= STUB - 1e-3
        assert _seg_len(out[-2], out[-1]) >= STUB - 1e-3
        assert abs(out[-2].x - 50.0) <= 1e-3 and abs(out[-2].y - (40.0 - STUB)) <= 1e-3

    # 2. 4-point vertical to-exit dragged within STUB of (and past) b0.
    v4 = [Point(x=0.0, y=0.0), Point(x=0.0, y=20.0), Point(x=50.0, y=20.0), Point(x=50.0, y=40.0)]
    for target_y in (38.0, 52.0):
        out = move_wire_corner(v4, 2, 50.0, target_y)
        _assert_orthogonal(out, tol=1e-3)
        _assert_no_retrace(out)
        assert [(p.x, p.y) for p in out] == [
            (0.0, 0.0),
            (0.0, 40.0 - STUB),
            (50.0, 40.0 - STUB),
            (50.0, 40.0),
        ]
        assert _seg_len(out[-2], out[-1]) >= STUB - 1e-3

    # 3. 4-point horizontal to-exit dragged within STUB of (and past) b0.
    h4 = [Point(x=0.0, y=0.0), Point(x=20.0, y=0.0), Point(x=20.0, y=40.0), Point(x=50.0, y=40.0)]
    for target_x in (48.0, 62.0):
        out = move_wire_corner(h4, 2, target_x, 40.0)
        _assert_orthogonal(out, tol=1e-3)
        _assert_no_retrace(out)
        assert [(p.x, p.y) for p in out] == [
            (0.0, 0.0),
            (50.0 - STUB, 0.0),
            (50.0 - STUB, 40.0),
            (50.0, 40.0),
        ]
        assert _seg_len(out[-2], out[-1]) >= STUB - 1e-3

    # 4. Rail-start + pin-end (slide_start=True): a0 stays aligned with clamped corner.
    for target_y in (38.0, 52.0):
        out = move_wire_corner(l3, 1, 50.0, target_y, slide_start=True)
        _assert_orthogonal(out, tol=1e-3)
        _assert_no_retrace(out)
        assert [(p.x, p.y) for p in out] == [
            (0.0, 40.0 - STUB),
            (50.0, 40.0 - STUB),
            (50.0, 40.0),
        ]
        assert abs(out[0].y - out[1].y) <= 1e-3
        assert _seg_len(out[-2], out[-1]) >= STUB - 1e-3

    # 5. Live GUI path: drag the to-adjacent interior corner (index == n - 2) within STUB of and past the to pin.
    session = Session.create("Blocker1GUI")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=60, y=70)
    session.place("nema.contactors.coil", ref="M1", x=180, y=100)
    w = session.wire("PB1.4", "M1.A1", number="101")
    end_a, end_b = _wire_endpoints_xy(session, w.id)
    mid_x = round((end_a[0] + end_b[0]) / 2.0 / 2.5) * 2.5
    session.set_wire_route(w.id, [Point(x=mid_x, y=end_a[1]), Point(x=mid_x, y=end_b[1])])
    session._undo.clear()
    session._undo_tip = None

    app, win = _open_window(session)
    try:
        canvas = win.canvas
        for dx_from_b in (-2.0, 12.0):
            canvas.select_ref(w.id)
            app.processEvents()
            corners = sorted(
                (h for h in _handles(canvas, w.id) if h.kind == "corner"),
                key=lambda h: h.corner_index,
            )
            to_adj = corners[-1]
            assert to_adj.corner_index == len(iter_wire_points(session.sheet(), session.catalog, session.find_wire(wire_id=w.id), project=session.project)) - 2
            target = QPointF(end_b[0] + dx_from_b, end_b[1])
            p0, p1 = _ensure_visible_pair(canvas, app, to_adj.pos(), target)
            w_item = _wire_item(canvas, w.id)
            QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
            QTest.mouseMove(canvas.viewport(), p1)
            app.processEvents()
            preview = list(getattr(w_item, "_live_pts", []) or [])
            assert len(preview) >= 3
            _assert_orthogonal(preview, tol=0.05)
            _assert_no_retrace(preview)
            assert abs(preview[-2].y - end_b[1]) <= 0.05
            assert end_b[0] - preview[-2].x >= STUB - 0.05

            QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
            app.processEvents()
            live = session.find_wire(wire_id=w.id)
            committed = iter_wire_points(session.sheet(), session.catalog, live, project=session.project)
            _assert_orthogonal(committed, tol=0.05)
            _assert_no_retrace(committed)
            assert abs(committed[-2].y - end_b[1]) <= 0.05
            assert end_b[0] - committed[-2].x >= STUB - 0.05

            win.undo_edit()
            app.processEvents()
    finally:
        win.close()


def test_gui_iec_rail_straighten_clears_points_and_from_at() -> None:
    """Should-Fix #2: straightening an IEC rail wire back over its pin stores points == [] and from_at is None."""
    session = Session.create("SF2IECStraighten")
    session.set_title_block(standard="IEC", rails="L+/0V")
    session.sheet().drawing_style = "iec"
    session.place("iec.operators.pb_no", ref="S1", x=100, y=70)
    w = session.wire("L+", "S1.13", number="10")
    pin_x, _pin_y = pin_world(
        session.sheet().component("S1"),
        session.catalog.get("iec.operators.pb_no").pin("13"),
    )
    geom = rail_geometry(session.sheet(), session.project)
    assert geom is not None and geom.kind == "iec"
    session._undo.clear()
    session._undo_tip = None

    app, win = _open_window(session)
    try:
        canvas = win.canvas
        # 1. Drag L+ -> S1.13 out right by 20 mm, then drag vertical rail run back over S1.13.
        w_item = _wire_item(canvas, w.id)
        mid = w_item.path().pointAtPercent(0.5)
        _drag_scene(canvas, app, mid, QPointF(pin_x + 20.0, mid.y()))
        live = session.find_wire(wire_id=w.id)
        assert live.from_at is not None and abs(live.from_at.x - (pin_x + 20.0)) < 1.5
        assert len(live.points) >= 2

        routed_out = iter_wire_points(session.sheet(), session.catalog, live, project=session.project)
        rail_seg_mid = QPointF(
            (routed_out[0].x + routed_out[1].x) / 2.0,
            (routed_out[0].y + routed_out[1].y) / 2.0,
        )
        _drag_scene(canvas, app, rail_seg_mid, QPointF(pin_x, rail_seg_mid.y()))
        live = session.find_wire(wire_id=w.id)
        assert live.from_at is None
        assert live.points == []
        straight_pts = iter_wire_points(session.sheet(), session.catalog, live, project=session.project)
        assert len(straight_pts) == 2
        assert abs(straight_pts[0].x - pin_x) < 0.1

        # 2. Drag out again, then straighten back via corner 1 handle -> also clears points and from_at.
        w_item = _wire_item(canvas, w.id)
        mid = w_item.path().pointAtPercent(0.5)
        _drag_scene(canvas, app, mid, QPointF(pin_x + 20.0, mid.y()))
        assert session.find_wire(wire_id=w.id).from_at is not None
        canvas.select_ref(w.id)
        app.processEvents()
        corner1 = next(h for h in _handles(canvas, w.id) if h.kind == "corner" and h.corner_index == 1)
        _drag_scene(canvas, app, corner1.pos(), QPointF(pin_x, corner1.pos().y()))
        live = session.find_wire(wire_id=w.id)
        assert live.from_at is None
        assert live.points == []
    finally:
        win.close()


def test_gui_wire_end_drag_clears_target_pin_hot_on_cancel_and_commit() -> None:
    """Should-Fix #3: hovered target PinItem._hot is reset to False after cancel (Esc/FocusOut/right-click) AND commit."""
    session = Session.create("SF3PinHot")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=60, y=70)
    session.place("nema.contactors.coil", ref="M1", x=180, y=70)
    session.place("nema.contactors.coil", ref="M2", x=180, y=120)
    w = session.wire("PB1.4", "M1.A1", number="101")
    session._undo.clear()
    session._undo_tip = None

    app, win = _open_window(session)
    try:
        canvas = win.canvas
        m2_pin = _pin_item(canvas, "M2", "A1")

        def _hover_m2_during_drag() -> QPoint:
            canvas.select_ref(w.id)
            app.processEvents()
            to_h = next(h for h in _handles(canvas, w.id) if h.kind == "end" and h.which == "to")
            p0, p1 = _ensure_visible_pair(canvas, app, to_h.pos(), m2_pin.scenePos())
            QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
            QTest.mouseMove(canvas.viewport(), p1)
            app.processEvents()
            assert canvas._wire_end_drag is not None
            assert m2_pin._hot is True
            return p1

        # 1. Cancel via Esc.
        p1 = _hover_m2_during_drag()
        canvas.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
        app.processEvents()
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        app.processEvents()
        assert m2_pin._hot is False

        # 2. Cancel via FocusOut.
        p1 = _hover_m2_during_drag()
        canvas.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
        app.processEvents()
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        app.processEvents()
        assert m2_pin._hot is False

        # 3. Cancel via right-click.
        p1 = _hover_m2_during_drag()
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, p1)
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, p1)
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        app.processEvents()
        assert m2_pin._hot is False

        # 4. Commit drop onto M2.A1.
        p1 = _hover_m2_during_drag()
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        app.processEvents()
        assert session.find_wire(wire_id=w.id).to == "M2.A1"
        assert m2_pin._hot is False
    finally:
        win.close()


def test_reconnect_wire_multihop_offpage_and_shared_net_conflicts() -> None:
    """Should-Fix #4: 2-hop networks, cross-sheet off-page links, and shared-net onto unnamed circuit are caught."""
    session = Session.create("SF4NetConflicts")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=60, y=50)
    session.place("nema.operators.pb_no", ref="PB2", x=60, y=100)
    session.place("nema.operators.pb_no", ref="PB3", x=60, y=150)
    session.place("nema.contactors.coil", ref="M1", x=180, y=50)
    session.place("nema.contactors.coil", ref="M2", x=180, y=100)
    session.place("nema.operators.pilot_light", ref="LT1", x=240, y=50)
    session.place("nema.operators.pilot_light", ref="LT2", x=240, y=100)
    session.place("nema.operators.pilot_light", ref="LT3", x=240, y=150)
    session.place("iec.connection.offpage", ref="OP1", x=260, y=180, properties={"link": "LNK_STOP"})
    session.place("iec.connection.offpage", ref="OP3", x=260, y=210, properties={"link": "LNK_STOP"})

    # Sheet 2 carries the off-page peer on net STOP.
    session.add_sheet("s2", title="Sheet 2", kind="schematic", make_active=False)
    session.place("iec.connection.offpage_from", ref="OP2", x=40, y=60, properties={"link": "LNK_STOP"}, sheet_id="s2")
    session.place("nema.contactors.coil", ref="K2", x=140, y=60, sheet_id="s2")
    session.wire("OP2.A", "K2.A1", net="STOP", sheet_id="s2")

    # 1. 2-hop STOP network on PB2.4: PB2.4 -> M2.A1 (net=None) + M2.A1 -> LT2.X1 (net="STOP").
    w_start = session.wire("PB1.4", "M1.A1", net="START")
    session.wire("PB2.4", "M2.A1")
    session.wire("M2.A1", "LT2.X1", net="STOP")
    # 2. Off-page STOP network on PB3.4: PB3.4 -> OP1.A (net=None) linked to s2:OP2 (net="STOP").
    session.wire("PB3.4", "OP1.A")
    # 3. 2-hop unnamed bridge on LT1.X1 (inherits START via other_ref) for reverse check.
    w_bridge = session.wire("PB1.3", "M1.A1")
    session._undo.clear()
    session._undo_tip = None

    with pytest.raises(ValueError, match="STOP"):
        session.reconnect_wire(w_start.id, "from", "PB2.4")
    with pytest.raises(ValueError, match="STOP"):
        session.reconnect_wire(w_bridge.id, "from", "PB2.4")
    with pytest.raises(ValueError, match="STOP"):
        session.reconnect_wire(w_start.id, "from", "PB3.4")
    with pytest.raises(ValueError, match="STOP"):
        session.reconnect_wire(w_start.id, "from", "OP3.A")
    assert session._undo == []

    # 4. Moving a wire that shares net="START" with an untouched sibling onto an unnamed circuit must refuse.
    s_shared = Session.create("SF4SharedNet")
    s_shared.set_title_block(drawing_style="ladder", rails="L1/L2")
    s_shared.place("nema.operators.pb_no", ref="PB1", x=60, y=50)
    s_shared.place("nema.operators.pb_no", ref="PB2", x=60, y=100)
    s_shared.place("nema.contactors.coil", ref="M1", x=180, y=50)
    s_shared.place("nema.contactors.coil", ref="M2", x=180, y=100)
    s_shared.place("nema.operators.pilot_light", ref="LT1", x=240, y=50)
    w1 = s_shared.wire("PB1.4", "M1.A1", net="START")
    w2 = s_shared.wire("PB1.4", "LT1.X1", net="START")
    w3 = s_shared.wire("PB2.4", "M2.A1")
    assert w1.number == w2.number
    orig_w3_num = w3.number
    assert orig_w3_num and orig_w3_num != w1.number
    s_shared._undo.clear()
    s_shared._undo_tip = None

    with pytest.raises(ValueError, match="START"):
        s_shared.reconnect_wire(w1.id, "from", "PB2.4")
    assert s_shared._undo == []
    assert w1.frm == "PB1.4"
    assert w3.number == orig_w3_num
    nets = wire_networks(s_shared.project, s_shared.catalog)
    w1_grp = next(idx for idx, items in enumerate(nets) if any(w is w1 for _sh, w in items))
    w3_grp = next(idx for idx, items in enumerate(nets) if any(w is w3 for _sh, w in items))
    assert w1_grp != w3_grp


@pytest.mark.parametrize("scheme", ["iec", "sheet_line", "sequential"])
def test_reconnect_multiwire_branch_into_rail_unifies_numbers(scheme: str) -> None:
    """Should-Fix #5: reconnecting a multi-wire branch into a rail network leaves no stale unlocked tail numbers."""
    for rail_first in (True, False):
        session = Session.create(f"SF5_{scheme}_{rail_first}")
        session.set_title_block(drawing_style="ladder", rails="L1/L2")
        session.set_wire_numbering(scheme=scheme, enabled=True, start=1)
        session.place("nema.operators.pb_no", ref="PB1", x=60, y=50)
        session.place("nema.operators.pb_no", ref="PB2", x=60, y=100)
        session.place("nema.contactors.coil", ref="M1", x=180, y=100)
        session.place("nema.operators.pilot_light", ref="LT1", x=240, y=100)
        session.place("nema.operators.pilot_light", ref="LT2", x=240, y=150)

        if rail_first:
            w_rail = session.wire("L1", "PB1.3")
            w1 = session.wire("PB2.4", "M1.A1")
            w_tail1 = session.wire("M1.A1", "LT1.X1")
            w_tail2 = session.wire("LT1.X1", "LT2.X1")
        else:
            w1 = session.wire("PB2.4", "M1.A1")
            w_tail1 = session.wire("M1.A1", "LT1.X1")
            w_tail2 = session.wire("LT1.X1", "LT2.X1")
            w_rail = session.wire("L1", "PB1.3")

        assert w_rail.number and not w_rail.number_locked
        assert w1.number == w_tail1.number == w_tail2.number
        assert w1.number != w_rail.number
        rail_num = w_rail.number
        session._undo.clear()
        session._undo_tip = None

        # 1. Reconnect w1.from onto L1.
        before = _dump(session)
        session.reconnect_wire(w1.id, "from", "L1")
        assert w_rail.number == rail_num
        assert w1.number == rail_num
        assert w_tail1.number == rail_num
        assert w_tail2.number == rail_num
        assert wire_number_conflicts(session.project, session.catalog) == []
        assert session.undo()
        assert _dump(session) == before

        # Re-fetch live wires after undo and reconnect w1.from onto PB1.3.
        w_rail = session.find_wire(wire_id=w_rail.id)
        w1 = session.find_wire(wire_id=w1.id)
        w_tail1 = session.find_wire(wire_id=w_tail1.id)
        w_tail2 = session.find_wire(wire_id=w_tail2.id)
        session.reconnect_wire(w1.id, "from", "PB1.3")
        assert w_rail.number == rail_num
        assert w1.number == rail_num
        assert w_tail1.number == rail_num
        assert w_tail2.number == rail_num
        assert wire_number_conflicts(session.project, session.catalog) == []


def test_gui_shared_rail_landing_does_not_rehash_per_mousemove(monkeypatch) -> None:
    """Should-Fix #6: dragging one of two rungs sharing a rail landing checks shared dots once, not every mouseMove."""
    import panelcad.render.wiring as wiring_mod

    session = Session.create("SF6SharedLanding")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=90, y=70)
    session.place("nema.operators.pb_no", ref="PB2", x=150, y=70)
    w1 = session.wire("L1", "PB1.3", number="100")
    w2 = session.wire("L1", "PB2.3", number="100")
    geom = rail_geometry(session.sheet(), session.project)
    assert geom is not None
    pts1 = iter_wire_points(session.sheet(), session.catalog, w1, project=session.project)
    pts2 = iter_wire_points(session.sheet(), session.catalog, w2, project=session.project)
    assert abs(pts1[0].y - pts2[0].y) < 0.2
    orig_y = pts1[0].y
    session._undo.clear()
    session._undo_tip = None

    app, win = _open_window(session)
    try:
        canvas = win.canvas
        w_item = _wire_item(canvas, w1.id)
        mid = w_item.path().pointAtPercent(0.5)
        p0, p_end = _ensure_visible_pair(canvas, app, mid, QPointF(mid.x(), orig_y + 25.0))

        calls = 0
        orig_route_sheet = wiring_mod.route_sheet_wires

        def spy_route_sheet(*args, **kwargs):
            nonlocal calls
            calls += 1
            return orig_route_sheet(*args, **kwargs)

        monkeypatch.setattr(wiring_mod, "route_sheet_wires", spy_route_sheet)

        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
        app.processEvents()
        calls = 0  # Measure only mouseMove events during the drag, not initial press/selection.
        for step in range(1, 9):
            frac = step / 8.0
            pt = QPoint(p0.x(), round(p0.y() + (p_end.y() - p0.y()) * frac))
            QTest.mouseMove(canvas.viewport(), pt)
            app.processEvents()
        assert canvas._route_rail_checked is True
        assert canvas._route_rail_hidden is None  # original dot stays visible for w2
        assert calls == 1

        # Both original dot (shared with w2) and live preview dot are visible.
        dots = [
            (item.rect().center().x(), item.rect().center().y())
            for item in canvas.scene.items()
            if isinstance(item, RailJunctionItem) and item.isVisible()
        ]
        assert any(abs(jx - geom.left) < 0.5 and abs(jy - orig_y) < 0.5 for jx, jy in dots)
        assert any(abs(jx - geom.left) < 0.5 and abs(jy - (orig_y + 25.0)) < 2.0 for jx, jy in dots)

        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p_end)
        app.processEvents()
        assert canvas._route_rail_checked is False
        assert session.find_wire(wire_id=w1.id).from_at is not None
        assert len(session._undo) == 1
    finally:
        win.close()


def test_gui_nits_orphan_drag_close_noop_rail_sync_rotation_and_unlocked_hover(monkeypatch) -> None:
    """N1-N6 and GUI rotation sync: orphan press cleanup, close mid-drag, no-op rail drop, handle sync, tag rotation, unlocked hover."""

    # --- N1, N3, N4, N6 on a ladder schematic ---
    session = Session.create("NitsSuite")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=80, y=70)
    session.place("nema.contactors.coil", ref="M1", x=180, y=70)
    w_rail = session.wire("L1", "PB1.3", number="100")
    w_pin = session.wire("PB1.4", "M1.A1", number="101")
    session.set_wire_route(w_pin.id, [Point(x=120, y=94), Point(x=120, y=114), Point(x=160, y=114)])
    session._undo.clear()
    session._undo_tip = None

    app, win = _open_window(session)
    try:
        canvas = win.canvas
        snap0 = _dump(session)

        def _start_orphan_pull():
            wi = _wire_item(canvas, w_pin.id)
            mid = wi.path().pointAtPercent(0.5)
            p0, p1 = _ensure_visible_pair(canvas, app, mid, QPointF(mid.x(), mid.y() + 15.0))
            QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
            QTest.mouseMove(canvas.viewport(), p1)
            app.processEvents()
            assert canvas._pull_wire is not None and session.command_open()

        # N1a: Orphaned route drag cancelled by new left press on empty space.
        _start_orphan_pull()
        empty_pt = QPointF(200.0, 200.0)
        canvas.centerOn(empty_pt)
        app.processEvents()
        pe = canvas.mapFromScene(empty_pt)
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pe)
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pe)
        app.processEvents()
        assert canvas._pull_wire is None and not session.command_open()
        assert _dump(session) == snap0 and session._undo == []

        # N1b: Orphaned route drag cancelled by new left press on title block.
        _start_orphan_pull()
        tb_x, tb_y, tb_w, tb_h = title_block_rect(session.sheet())
        tb_pt = QPointF(tb_x + tb_w * 0.5, tb_y + tb_h * 0.5)
        canvas.fit_page()
        app.processEvents()
        ptb = canvas.mapFromScene(tb_pt)
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, ptb)
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, ptb)
        app.processEvents()
        assert canvas._pull_wire is None and not session.command_open()
        assert _dump(session) == snap0 and session._undo == []

        # N1c: Orphaned route drag cancelled by new left press on LogoSlotItem.
        logo_item = next(i for i in canvas.scene.items() if isinstance(i, LogoSlotItem))
        _start_orphan_pull()
        logo_pt = logo_item.mapToScene(logo_item.boundingRect().center())
        canvas.fit_page()
        app.processEvents()
        plogo = canvas.mapFromScene(logo_pt)
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, plogo)
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, plogo)
        app.processEvents()
        assert canvas._pull_wire is None and not session.command_open()
        assert _dump(session) == snap0 and session._undo == []

        # N1d: Orphaned corner drag cancelled by new left press on an end handle.
        canvas.select_ref(w_pin.id)
        app.processEvents()
        corner = next(h for h in _handles(canvas, w_pin.id) if h.kind == "corner")
        end_h = next(h for h in _handles(canvas, w_pin.id) if h.kind == "end" and h.which == "to")
        end_pos = end_h.pos()
        c0, c1 = _ensure_visible_pair(canvas, app, corner.pos(), QPointF(corner.pos().x() + 10.0, corner.pos().y() + 10.0))
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c0)
        QTest.mouseMove(canvas.viewport(), c1)
        app.processEvents()
        assert canvas._corner_wire is not None and session.command_open()
        canvas.fit_page()
        app.processEvents()
        pend = canvas.mapFromScene(end_pos)
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pend)
        app.processEvents()
        assert canvas._corner_wire is None and not session.command_open()
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pend)
        app.processEvents()
        canvas.cancel_wire()
        app.processEvents()
        assert _dump(session) == snap0 and session._undo == []

        # N3: No-op same-rail-landing end drop does not redraw, emit project_changed, or say "reconnected".
        canvas.select_ref(w_rail.id)
        app.processEvents()
        from_h = next(h for h in _handles(canvas, w_rail.id) if h.kind == "end" and h.which == "from")
        rail_orig = from_h.pos()
        p0, p_out = _ensure_visible_pair(canvas, app, rail_orig, QPointF(rail_orig.x() + 25.0, rail_orig.y() + 25.0))
        p_back = canvas.mapFromScene(rail_orig)
        redraw_calls = 0
        changed_calls = 0
        orig_redraw = canvas._redraw_wires

        def spy_redraw():
            nonlocal redraw_calls
            redraw_calls += 1
            return orig_redraw()

        def on_changed():
            nonlocal changed_calls
            changed_calls += 1

        monkeypatch.setattr(canvas, "_redraw_wires", spy_redraw)
        canvas.project_changed.connect(on_changed)
        act_before = len(session.activity)
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
        QTest.mouseMove(canvas.viewport(), p_out)
        app.processEvents()
        QTest.mouseMove(canvas.viewport(), p_back)
        app.processEvents()
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p_back)
        app.processEvents()
        canvas.project_changed.disconnect(on_changed)
        monkeypatch.undo()
        assert redraw_calls == 0
        assert changed_calls == 0
        assert "reconnected" not in win.statusBar().currentMessage().lower()
        assert len(session._undo) == 0
        assert len(session.activity) == act_before

        # N4: No redundant _sync_wire_handles() calls on cancel or plain-click pin fallback.
        canvas.select_ref(w_pin.id)
        app.processEvents()
        sync_calls = 0
        orig_sync = canvas._sync_wire_handles

        def spy_sync(*args, **kwargs):
            nonlocal sync_calls
            sync_calls += 1
            return orig_sync(*args, **kwargs)

        monkeypatch.setattr(canvas, "_sync_wire_handles", spy_sync)
        to_h = next(h for h in _handles(canvas, w_pin.id) if h.kind == "end" and h.which == "to")
        _click_scene(canvas, app, to_h.pos())
        assert canvas.wire_from == "M1.A1"
        assert sync_calls == 1
        monkeypatch.undo()
        canvas.cancel_wire()
        app.processEvents()

        # Also verify _cancel_wire_route_drag does not call _sync_wire_handles redundantly after _select_wire_item.
        _start_orphan_pull()
        sync_calls_cancel = 0
        orig_sync2 = canvas._sync_wire_handles

        def spy_sync2(*args, **kwargs):
            nonlocal sync_calls_cancel
            sync_calls_cancel += 1
            return orig_sync2(*args, **kwargs)

        monkeypatch.setattr(canvas, "_sync_wire_handles", spy_sync2)
        canvas._cancel_wire_route_drag()
        app.processEvents()
        monkeypatch.undo()
        # 1 from removeItem(selected old WireItem), 1 from _redraw_wires(), 1 from _select_wire_item() = 3
        # (no extra redundant 4th _sync_wire_handles() call after _select_wire_item()).
        assert sync_calls_cancel == 3
        assert len(_handles(canvas, w_pin.id)) >= 2

        # N6: Unlocked pin under selected wire's end handle gets pin cursor & tooltip (not CrossCursor/wire-end).
        session.set_pins_locked("PB1", False)
        canvas._apply_pin_mode()
        canvas.select_ref(w_pin.id)
        app.processEvents()
        pb1_pin = _pin_item(canvas, "PB1", "4")
        from_h = next(h for h in _handles(canvas, w_pin.id) if h.kind == "end" and h.which == "from")
        canvas.centerOn(pb1_pin.scenePos())
        app.processEvents()
        top_item = canvas.itemAt(canvas.mapFromScene(pb1_pin.scenePos()))
        assert top_item is from_h
        assert top_item.cursor().shape() == Qt.CursorShape.SizeAllCursor
        assert top_item.toolTip() == pb1_pin.toolTip()
        assert "Unlocked" in top_item.toolTip()
        assert "Drag from end" not in top_item.toolTip()

        # Re-locking restores handle cursor, tooltip, and zValue.
        session.set_pins_locked("PB1", True)
        canvas._apply_pin_mode()
        app.processEvents()
        from_h_locked = next(h for h in _handles(canvas, w_pin.id) if h.kind == "end" and h.which == "from")
        assert from_h_locked.zValue() >= 30
        assert from_h_locked.cursor().shape() == Qt.CursorShape.CrossCursor
        assert "Drag from end" in from_h_locked.toolTip()

        # N2: Closing the window mid-drag (release_scenes) abandons the open session command.
        _start_orphan_pull()
        assert session.command_open()
    finally:
        win.close()
        app.processEvents()
    assert not session.command_open()
    depth_after_close = len(session._undo)
    session.place("nema.operators.pb_no", ref="PB9", x=60, y=150)
    assert len(session._undo) == depth_after_close + 1

    # --- N5 & GUI rotation sync: orient_two_pin_flow updates ComponentItem.rotation() AND _tag_layout ---
    s_flow = Session.create("N5GUIRotation")
    s_flow.place("iec.protection.fuse_10a", ref="F1", x=100, y=60)
    s_flow.place("iec.operators.pb_no", ref="S_H", x=180, y=60)
    s_flow.place("iec.operators.pb_no", ref="S_V", x=100, y=140)
    wf = s_flow.wire("F1.2", "S_H.13")
    assert s_flow.sheet().component("F1").rotation == 0
    s_flow._undo.clear()
    s_flow._undo_tip = None

    app2, win2 = _open_window(s_flow)
    try:
        canvas2 = win2.canvas
        f1_item = next(
            i for i in canvas2.scene.items() if isinstance(i, ComponentItem) and i.component_id == "F1"
        )
        assert round(f1_item.rotation()) % 360 == 0
        canvas2.select_ref(wf.id)
        app2.processEvents()
        to_h = next(h for h in _handles(canvas2, wf.id) if h.kind == "end" and h.which == "to")
        sv_13 = _pin_item(canvas2, "S_V", "13").scenePos()
        _drag_scene(canvas2, app2, to_h.pos(), sv_13)

        assert s_flow.sheet().component("F1").rotation == 90
        assert round(f1_item.rotation()) % 360 == 90
        expected_tags = canvas2._device_tag_layouts()
        if "F1" in expected_tags:
            wx, wy, align, size = expected_tags["F1"]
            expected_local = f1_item.mapFromScene(QPointF(wx, wy))
            assert f1_item._tag_layout is not None
            assert abs(f1_item._tag_layout[0] - expected_local.x()) < 0.05
            assert abs(f1_item._tag_layout[1] - expected_local.y()) < 0.05
            assert f1_item._tag_layout[2] == align
            assert abs(f1_item._tag_layout[3] - size) < 0.05

        win2.undo_edit()
        app2.processEvents()
        assert s_flow.sheet().component("F1").rotation == 0
    finally:
        win2.close()


def test_gui_to_rail_run_and_rail_corner_drags_with_junction_dots() -> None:
    """Test gap: to-rail run drags (M1.A2 -> L2 and K1.A2 -> 0V) and rail-attached corner drags with junction dots."""

    def _visible_junctions(canvas) -> list[tuple[float, float]]:
        return [
            (item.rect().center().x(), item.rect().center().y())
            for item in canvas.scene.items()
            if isinstance(item, RailJunctionItem) and item.isVisible()
        ]

    # 1. NEMA ladder to-rail wire M1.A2 -> L2 run drag + rail-attached corner drag.
    s_lad = Session.create("ToRailLadder")
    s_lad.set_title_block(drawing_style="ladder", rails="L1/L2")
    s_lad.place("nema.operators.pb_no", ref="PB1", x=90, y=70)
    s_lad.place("nema.contactors.coil", ref="M1", x=180, y=70)
    w_from = s_lad.wire("L1", "PB1.3", number="100")
    w_to = s_lad.wire("M1.A2", "L2", number="102")
    geom_l = rail_geometry(s_lad.sheet(), s_lad.project)
    assert geom_l is not None and geom_l.kind == "ladder"
    s_lad._undo.clear()
    s_lad._undo_tip = None

    app, win = _open_window(s_lad)
    try:
        canvas = win.canvas
        wi_to = _wire_item(canvas, w_to.id)
        mid = wi_to.path().pointAtPercent(0.5)
        old_y = mid.y()
        before = _dump(s_lad)

        # Drag M1.A2 -> L2 down by 20 mm -> updates to_at and moves L2 junction dot.
        _drag_scene(canvas, app, mid, QPointF(mid.x(), old_y + 20.0))
        live_to = s_lad.find_wire(wire_id=w_to.id)
        assert live_to.to_at is not None
        assert abs(live_to.to_at.x - geom_l.right) < 0.2
        assert abs(live_to.to_at.y - (old_y + 20.0)) < 1.5
        committed = iter_wire_points(s_lad.sheet(), s_lad.catalog, live_to, project=s_lad.project)
        _assert_orthogonal(committed)
        assert abs(committed[-1].y - live_to.to_at.y) < 0.15
        dots = _visible_junctions(canvas)
        assert any(abs(jx - geom_l.right) < 0.5 and abs(jy - live_to.to_at.y) < 0.5 for jx, jy in dots)
        assert not any(abs(jx - geom_l.right) < 0.5 and abs(jy - old_y) < 0.5 for jx, jy in dots)
        assert len(s_lad._undo) == 1

        # Rail-attached corner drag on to-rail wire (corner index == n - 2) slides to_at further down by 15 mm.
        canvas.select_ref(w_to.id)
        app.processEvents()
        corners_to = sorted(
            (h for h in _handles(canvas, w_to.id) if h.kind == "corner"),
            key=lambda h: h.corner_index,
        )
        rail_corner_to = corners_to[-1]
        prev_to_y = live_to.to_at.y
        _drag_scene(canvas, app, rail_corner_to.pos(), QPointF(rail_corner_to.pos().x(), prev_to_y + 15.0))
        live_to = s_lad.find_wire(wire_id=w_to.id)
        assert live_to.to_at is not None
        assert abs(live_to.to_at.y - (prev_to_y + 15.0)) < 1.5
        committed_c = iter_wire_points(s_lad.sheet(), s_lad.catalog, live_to, project=s_lad.project)
        _assert_orthogonal(committed_c)
        dots = _visible_junctions(canvas)
        assert any(abs(jx - geom_l.right) < 0.5 and abs(jy - live_to.to_at.y) < 0.5 for jx, jy in dots)
        assert len(s_lad._undo) == 2

        win.undo_edit()
        win.undo_edit()
        app.processEvents()
        assert _dump(s_lad) == before

        # Also test rail-attached corner drag on from-rail wire L1 -> PB1.3 (offset first, then corner 1 drag).
        wi_from = _wire_item(canvas, w_from.id)
        f_mid = wi_from.path().pointAtPercent(0.5)
        _drag_scene(canvas, app, f_mid, QPointF(f_mid.x(), f_mid.y() + 15.0))
        live_f = s_lad.find_wire(wire_id=w_from.id)
        assert live_f.from_at is not None
        canvas.select_ref(w_from.id)
        app.processEvents()
        corner1 = next(h for h in _handles(canvas, w_from.id) if h.kind == "corner" and h.corner_index == 1)
        prev_from_y = live_f.from_at.y
        _drag_scene(canvas, app, corner1.pos(), QPointF(corner1.pos().x(), prev_from_y + 15.0))
        live_f = s_lad.find_wire(wire_id=w_from.id)
        assert live_f.from_at is not None
        assert abs(live_f.from_at.y - (prev_from_y + 15.0)) < 1.5
        dots = _visible_junctions(canvas)
        assert any(abs(jx - geom_l.left) < 0.5 and abs(jy - live_f.from_at.y) < 0.5 for jx, jy in dots)
    finally:
        win.close()

    # 2. IEC to-rail wire K1.A2 -> 0V run drag + straighten back over pin.
    s_iec = Session.create("ToRailIEC")
    s_iec.set_title_block(standard="IEC", rails="L+/0V")
    s_iec.sheet().drawing_style = "iec"
    s_iec.place("iec.contactors.coil", ref="K1", x=120, y=100)
    w_ov = s_iec.wire("K1.A2", "0V", number="20")
    pin_x, _pin_y = pin_world(
        s_iec.sheet().component("K1"),
        s_iec.catalog.get("iec.contactors.coil").pin("A2"),
    )
    geom_i = rail_geometry(s_iec.sheet(), s_iec.project)
    assert geom_i is not None and geom_i.kind == "iec"
    s_iec._undo.clear()
    s_iec._undo_tip = None

    app2, win2 = _open_window(s_iec)
    try:
        canvas2 = win2.canvas
        wi = _wire_item(canvas2, w_ov.id)
        mid = wi.path().pointAtPercent(0.5)
        _drag_scene(canvas2, app2, mid, QPointF(pin_x + 20.0, mid.y()))
        live = s_iec.find_wire(wire_id=w_ov.id)
        assert live.to_at is not None
        assert abs(live.to_at.y - geom_i.bottom) < 0.2
        assert abs(live.to_at.x - (pin_x + 20.0)) < 1.5
        committed = iter_wire_points(s_iec.sheet(), s_iec.catalog, live, project=s_iec.project)
        _assert_orthogonal(committed)
        dots = _visible_junctions(canvas2)
        assert any(abs(jx - live.to_at.x) < 0.5 and abs(jy - geom_i.bottom) < 0.5 for jx, jy in dots)

        # Straighten back over K1.A2 -> points == [] and to_at is None.
        rail_seg_mid = QPointF(
            (committed[-2].x + committed[-1].x) / 2.0,
            (committed[-2].y + committed[-1].y) / 2.0,
        )
        _drag_scene(canvas2, app2, rail_seg_mid, QPointF(pin_x, rail_seg_mid.y()))
        live = s_iec.find_wire(wire_id=w_ov.id)
        assert live.to_at is None
        assert live.points == []
    finally:
        win2.close()


def test_gui_network_and_io_sheet_wire_dragging() -> None:
    """Test gap: wire end, segment, and corner dragging on both network (comms) and io sheets."""

    # 1. Network (comms) sheet.
    s_net = Session.create("NetSheetDrag")
    s_net.add_sheet("lan", title="LAN", kind="network", make_active=True)
    s_net.place("iec.network.switch_5p", ref="SW1", x=60, y=60, sheet_id="lan")
    s_net.place("iec.hmi.panel_7in", ref="HMI1", x=190, y=50, sheet_id="lan")
    s_net.place("iec.plc.compact_16dio", ref="PLC1", x=190, y=130, sheet_id="lan")
    w_net = s_net.wire("SW1.P4", "HMI1.ETH", sheet_id="lan", net="LAN")
    s_net._undo.clear()
    s_net._undo_tip = None

    app, win = _open_window(s_net)
    try:
        canvas = win.canvas
        # End drag: reconnect SW1.P4 -> HMI1.ETH to SW1.P5 -> HMI1.ETH.
        before_end = _dump(s_net)
        canvas.select_ref(w_net.id)
        app.processEvents()
        from_h = next(h for h in _handles(canvas, w_net.id) if h.kind == "end" and h.which == "from")
        p5_pos = _pin_item(canvas, "SW1", "P5").scenePos()
        _drag_scene(canvas, app, from_h.pos(), p5_pos)
        assert s_net.find_wire(wire_id=w_net.id, sheet_id="lan").frm == "SW1.P5"
        assert len(s_net._undo) == 1
        win.undo_edit()
        app.processEvents()
        assert _dump(s_net) == before_end
        win.redo_edit()
        app.processEvents()
        assert s_net.find_wire(wire_id=w_net.id, sheet_id="lan").frm == "SW1.P5"

        # Segment drag on network link.
        wi = _wire_item(canvas, w_net.id)
        mid = wi.path().pointAtPercent(0.5)
        before_seg = _dump(s_net)
        _drag_scene(canvas, app, mid, QPointF(mid.x() + 15.0, mid.y() + 15.0))
        live = s_net.find_wire(wire_id=w_net.id, sheet_id="lan")
        assert len(live.points) >= 1
        routed = iter_wire_points(s_net.sheet("lan"), s_net.catalog, live, project=s_net.project)
        _assert_orthogonal(routed)
        assert len(s_net._undo) == 2

        # Corner drag on network link.
        canvas.select_ref(w_net.id)
        app.processEvents()
        corners = [h for h in _handles(canvas, w_net.id) if h.kind == "corner"]
        assert corners
        c0 = corners[0]
        _drag_scene(canvas, app, c0.pos(), QPointF(c0.pos().x() + 10.0, c0.pos().y() + 10.0))
        live = s_net.find_wire(wire_id=w_net.id, sheet_id="lan")
        routed2 = iter_wire_points(s_net.sheet("lan"), s_net.catalog, live, project=s_net.project)
        _assert_orthogonal(routed2)
        assert len(s_net._undo) == 3

        win.undo_edit()
        win.undo_edit()
        app.processEvents()
        assert _dump(s_net) == before_seg
    finally:
        win.close()

    # 2. I/O sheet.
    s_io = Session.create("IOSheetDrag")
    s_io.add_sheet("io1", title="PLC I/O", kind="io", make_active=True)
    s_io.place("iec.operators.pb_no", ref="S1", x=60, y=60, sheet_id="io1")
    s_io.place("iec.plc.di16", ref="DI1", x=180, y=40, sheet_id="io1")
    w_io = s_io.wire("S1.14", "DI1.I0", sheet_id="io1")
    s_io._undo.clear()
    s_io._undo_tip = None

    app2, win2 = _open_window(s_io)
    try:
        canvas2 = win2.canvas
        # End drag: reconnect DI1.I0 -> DI1.I1.
        before_end = _dump(s_io)
        canvas2.select_ref(w_io.id)
        app2.processEvents()
        to_h = next(h for h in _handles(canvas2, w_io.id) if h.kind == "end" and h.which == "to")
        i1_pos = _pin_item(canvas2, "DI1", "I1").scenePos()
        _drag_scene(canvas2, app2, to_h.pos(), i1_pos)
        assert s_io.find_wire(wire_id=w_io.id, sheet_id="io1").to == "DI1.I1"
        assert len(s_io._undo) == 1
        win2.undo_edit()
        app2.processEvents()
        assert _dump(s_io) == before_end
        win2.redo_edit()
        app2.processEvents()
        assert s_io.find_wire(wire_id=w_io.id, sheet_id="io1").to == "DI1.I1"

        # Segment drag on I/O wire.
        wi = _wire_item(canvas2, w_io.id)
        mid = wi.path().pointAtPercent(0.5)
        before_seg = _dump(s_io)
        _drag_scene(canvas2, app2, mid, QPointF(mid.x() + 15.0, mid.y() + 15.0))
        live = s_io.find_wire(wire_id=w_io.id, sheet_id="io1")
        assert len(live.points) >= 1
        routed = iter_wire_points(s_io.sheet("io1"), s_io.catalog, live, project=s_io.project)
        _assert_orthogonal(routed)
        assert len(s_io._undo) == 2

        # Corner drag on I/O wire.
        canvas2.select_ref(w_io.id)
        app2.processEvents()
        corners = [h for h in _handles(canvas2, w_io.id) if h.kind == "corner"]
        assert corners
        c0 = corners[0]
        _drag_scene(canvas2, app2, c0.pos(), QPointF(c0.pos().x() + 10.0, c0.pos().y() + 10.0))
        live = s_io.find_wire(wire_id=w_io.id, sheet_id="io1")
        routed2 = iter_wire_points(s_io.sheet("io1"), s_io.catalog, live, project=s_io.project)
        _assert_orthogonal(routed2)
        assert len(s_io._undo) == 3

        win2.undo_edit()
        win2.undo_edit()
        app2.processEvents()
        assert _dump(s_io) == before_seg
    finally:
        win2.close()
