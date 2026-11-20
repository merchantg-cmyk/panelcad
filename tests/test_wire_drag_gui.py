"""Offscreen GUI and unit tests for interactive wire end/segment/corner dragging."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QFocusEvent, QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from panelcad.api.session import Session
from panelcad.core.geometry import Point
from panelcad.core.ladder import rail_geometry
from panelcad.core.route import move_wire_corner, nearest_corner, set_run_channel
from panelcad.gui.canvas import PinItem, WireHandleItem, WireItem
from panelcad.gui.main_window import MainWindow
from panelcad.render.sheet import iter_wire_points, pin_world


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
    # 2-point horizontal wire gets clean orthogonal jog points pinned to ends.
    h2 = [Point(x=10.0, y=20.0), Point(x=100.0, y=20.0)]
    pulled_h = set_run_channel(h2, 0, "h", 40.0)
    assert [(p.x, p.y) for p in pulled_h] == [
        (10.0, 20.0),
        (10.0, 40.0),
        (100.0, 40.0),
        (100.0, 20.0),
    ]

    # 2-point vertical wire gets clean orthogonal jog points pinned to ends.
    v2 = [Point(x=30.0, y=10.0), Point(x=30.0, y=90.0)]
    pulled_v = set_run_channel(v2, 0, "v", 55.0)
    assert [(p.x, p.y) for p in pulled_v] == [
        (30.0, 10.0),
        (55.0, 10.0),
        (55.0, 90.0),
        (30.0, 90.0),
    ]

    # End-adjacent segment of an L-bend keeps pin end fixed and jogs cleanly.
    l3 = [Point(x=0.0, y=0.0), Point(x=50.0, y=0.0), Point(x=50.0, y=40.0)]
    pulled_l = set_run_channel(l3, 0, "h", 15.0)
    assert [(p.x, p.y) for p in pulled_l] == [
        (0.0, 0.0),
        (0.0, 15.0),
        (50.0, 15.0),
        (50.0, 40.0),
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

        # Plain click on an endpoint handle adds 0 undo steps.
        to_handle = next(h for h in end_handles if h.which == "to")
        _click_scene(canvas, app, to_handle.pos())
        assert len(session._undo) == 0

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

        # 3. Same starting pin drop -> no-op, keeps points, 0 undo steps.
        to_pt = _pin_item(canvas, "M1", "A1").scenePos()
        to_h = next(h for h in _handles(canvas, w_pin.id) if h.kind == "end" and h.which == "to")
        _drag_scene(canvas, app, to_h.pos(), to_pt)
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
        assert any(abs(p.x - round(target_corner.x() / 2.5) * 2.5) < 0.5 for p in routed2[1:-1])
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

        # Switching to Wire mode clears selection and wire handles so pins are exposed.
        canvas.select_ref(w_top.id)
        app.processEvents()
        assert _handles(canvas, w_top.id)
        canvas.set_wire_mode()
        app.processEvents()
        assert _handles(canvas) == []
    finally:
        win.close()
