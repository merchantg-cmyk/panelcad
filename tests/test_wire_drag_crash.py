"""Regression tests for the 0.5.29 wire corner/segment drag crash (0xc0000005).

Before the fix, ``WireItem.boundingRect()`` and ``WireHandleItem.boundingRect()``
read ``view.transform().m11()`` live, and ``zoom_by()`` / ``fit_page()`` /
``fit_drawing()`` changed the view transform *before* calling
``prepareGeometryChange()``. Zooming in shrank item bounds behind
``QGraphicsScene``'s BSP tree index, leaving dangling ``QGraphicsItem*``
pointers when wire items and handles were removed during a wire drag. Releasing
a corner/segment drag then crashed with 0xc0000005 inside
``mouseReleaseEvent -> _commit_wire_corner_drag -> _finalize_wire_route_drag ->
_select_wire_item -> _sync_wire_handles`` (or on the next ``paintEvent``).

Separately, ``LiveCheckRunner``'s background ``_CheckWorker`` called
``nameplate_findings() -> build_nameplate() -> layout_marks() ->
qt_painter.text_advance_mm()``, constructing ``QFont`` / ``QFontMetricsF`` on a
non-GUI thread.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QMouseEvent, QPainterPath
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGraphicsScene

from panelcad.api.session import Session
from panelcad.core.checks import run_checks
from panelcad.core.geometry import Point
from panelcad.electrical.nameplate import build_nameplate
from panelcad.gui.canvas import (
    ComponentItem,
    PinItem,
    SchematicView,
    WireHandleItem,
    WireItem,
)
from panelcad.gui.main_window import MainWindow
from panelcad.render.sheet import iter_wire_points
from panelcad.render.wiring import wire_ends

_ROOT = Path(__file__).resolve().parents[1]
_DOL = _ROOT / "examples" / "dol_starter.pcad.yaml"


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _dump(session: Session) -> dict:
    return session.project.model_dump(mode="python")


def _wire_snapshot(session: Session) -> dict[str, tuple[str, str]]:
    return {w.id: (str(w.frm), str(w.to)) for w in session.sheet().wires}


def _assert_orthogonal(pts: list[Point], tol: float = 0.25) -> None:
    assert len(pts) >= 2
    for a, b in zip(pts, pts[1:]):
        assert abs(a.x - b.x) <= tol or abs(a.y - b.y) <= tol, (a, b)


def _assert_wire_connected(session: Session, wire_id: str) -> list[Point]:
    sheet = session.sheet()
    wire = session.find_wire(wire_id=wire_id)
    ax, ay, _as, bx, by, _bs = wire_ends(sheet, session.catalog, wire, session.project)
    pts = iter_wire_points(sheet, session.catalog, wire, project=session.project)
    _assert_orthogonal(pts)
    assert abs(pts[0].x - ax) <= 0.25 and abs(pts[0].y - ay) <= 0.25, (pts[0], (ax, ay))
    assert abs(pts[-1].x - bx) <= 0.25 and abs(pts[-1].y - by) <= 0.25, (pts[-1], (bx, by))
    return pts


def _handles(canvas: SchematicView, wire_id: str | None = None) -> list[WireHandleItem]:
    out = [item for item in canvas.scene.items() if isinstance(item, WireHandleItem)]
    if wire_id is not None:
        out = [item for item in out if item.wire_id == wire_id]
    return out


def _wire_item(canvas: SchematicView, wire_id: str) -> WireItem:
    return next(
        item
        for item in canvas.scene.items()
        if isinstance(item, WireItem) and item.wire_id == wire_id
    )


def _hover_viewport(canvas: SchematicView, pt: QPoint) -> None:
    pos_f = QPointF(pt)
    QApplication.sendEvent(
        canvas.viewport(),
        QMouseEvent(
            QMouseEvent.Type.MouseMove,
            pos_f,
            canvas.viewport().mapToGlobal(pos_f),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )


def _drag_real_mouse(
    canvas: SchematicView,
    app: QApplication,
    start_scene: QPointF,
    end_scene: QPointF,
    *,
    hover_first: bool = True,
    process_mid: bool = True,
) -> None:
    mid = QPointF((start_scene.x() + end_scene.x()) / 2.0, (start_scene.y() + end_scene.y()) / 2.0)
    canvas.centerOn(mid)
    app.processEvents()
    p0 = canvas.mapFromScene(start_scene)
    p1 = canvas.mapFromScene(end_scene)
    rect = canvas.viewport().rect()
    if not (rect.contains(p0) and rect.contains(p1)):
        canvas.fit_page()
        app.processEvents()
        p0 = canvas.mapFromScene(start_scene)
        p1 = canvas.mapFromScene(end_scene)
    assert rect.contains(p0) and rect.contains(p1), (p0, p1, rect)
    vp = canvas.viewport()
    if hover_first:
        _hover_viewport(canvas, p0)
        app.processEvents()
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
    if process_mid:
        app.processEvents()
    p_mid = QPoint((p0.x() + p1.x()) // 2, (p0.y() + p1.y()) // 2)
    QTest.mouseMove(vp, p_mid)
    if process_mid:
        app.processEvents()
    QTest.mouseMove(vp, p1)
    if process_mid:
        app.processEvents()
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
    app.processEvents()


def test_wire_corner_drag_after_fit_page_and_zoom_in_does_not_crash() -> None:
    """Exact reproducer for the 0.5.29 0xc0000005 crash in _sync_wire_handles / paintEvent."""
    app = _app()
    session = Session.open(_DOL)
    session.set_active_sheet("control")
    session._undo.clear()
    session._undo_tip = None
    orig_wires = _wire_snapshot(session)

    view = SchematicView(session)
    try:
        # 1100x550 mirrors a 1080p shop viewport where fit_page scale (~1.74 px/mm)
        # gives wire/handle hit pads larger than the 3.0 mm floor.
        view.resize(1100, 550)
        view.show()
        view.fit_page()
        app.processEvents()

        dragged_any = 0
        for w in list(session.sheet().wires):
            view.fit_page()
            app.processEvents()
            view.select_ref(w.id)
            app.processEvents()
            corners = [h for h in _handles(view, w.id) if h.kind == "corner"]
            if not corners:
                continue
            # Wheel-zoom in 6 ticks: before the fix this shrank WireItem/WireHandleItem
            # boundingRect() before prepareGeometryChange() and corrupted the BSP tree.
            for _ in range(6):
                view.zoom_by(1.15)
            c = corners[0]
            view.centerOn(c.pos())
            app.processEvents()
            before = _dump(session)
            p0 = view.mapFromScene(c.pos())
            p1 = view.mapFromScene(QPointF(c.pos().x() + 5.0, c.pos().y() + 5.0))
            assert view.viewport().rect().contains(p0) and view.viewport().rect().contains(p1)
            # No intermediate processEvents between move and release so the BSP lookup
            # inside _select_wire_item -> _sync_wire_handles executes immediately on release.
            QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
            QTest.mouseMove(view.viewport(), p1)
            QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
            app.processEvents()
            # Full-scene BSP query must never hit freed items.
            _ = view.scene.items(QRectF(0, 0, 420, 297))
            assert _wire_snapshot(session) == orig_wires
            _assert_wire_connected(session, w.id)
            if _dump(session) != before:
                assert session.undo()
                assert _dump(session) == before
                view.reload()
                app.processEvents()
            dragged_any += 1
        assert dragged_any >= 3
    finally:
        view.release_scenes()
        view.close()


def test_repeated_wire_segment_and_corner_drags_with_undo_redo_and_zoom() -> None:
    """Repeated segment + corner drags across zoom changes, with undo/redo verification."""
    app = _app()
    session = Session.create("RepeatedDragCrash")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=60, y=60)
    session.place("nema.operators.pb_no", ref="PB2", x=60, y=110)
    session.place("nema.contactors.coil", ref="M1", x=180, y=60)
    session.place("nema.contactors.coil", ref="M2", x=180, y=110)
    w1 = session.wire("PB1.4", "M1.A1", number="101")
    w2 = session.wire("PB2.4", "M2.A1", number="102")
    session.wire("L1", "PB1.3", number="100")
    session.wire("M1.A2", "L2", number="103")
    session._undo.clear()
    session._undo_tip = None
    orig_wires = _wire_snapshot(session)

    win = MainWindow(session)
    try:
        win.resize(1100, 620)
        win.show()
        app.processEvents()
        canvas = win.canvas
        canvas.set_select_mode()

        # 1. Start zoomed way out (pad >> 3 mm), then zoom in and pull a 2-point segment.
        canvas.resetTransform()
        canvas.scale(0.35, 0.35)
        canvas._wire_hit_scale = -1.0
        canvas._refresh_wire_hit_geometry()
        app.processEvents()
        _ = canvas.scene.items(QRectF(0, 0, 420, 297))

        for _ in range(8):
            canvas.zoom_by(1.25)
        app.processEvents()

        before_seg = _dump(session)
        wi1 = _wire_item(canvas, w1.id)
        mid = wi1.path().pointAtPercent(0.5)
        _drag_real_mouse(canvas, app, mid, QPointF(mid.x(), mid.y() + 15.0))
        assert len(session._undo) == 1
        assert _wire_snapshot(session) == orig_wires
        _assert_wire_connected(session, w1.id)
        after_seg = _dump(session)

        # 2. Repeated corner drags across fit_page -> zoom_by cycles.
        snapshots = [before_seg, after_seg]
        for step in range(6):
            canvas.fit_page()
            app.processEvents()
            canvas.select_ref(w1.id)
            app.processEvents()
            _ = canvas.scene.items(QRectF(0, 0, 420, 297))
            for _ in range(5):
                canvas.zoom_by(1.18)
            corners = sorted(
                (h for h in _handles(canvas, w1.id) if h.kind == "corner"),
                key=lambda h: h.corner_index,
            )
            assert corners
            c = corners[step % len(corners)]
            dx = 7.5 if step % 2 == 0 else -5.0
            dy = 5.0 if step % 3 != 0 else -5.0
            _drag_real_mouse(
                canvas,
                app,
                c.pos(),
                QPointF(c.pos().x() + dx, c.pos().y() + dy),
                process_mid=(step % 2 == 0),
            )
            _ = canvas.scene.items(QRectF(0, 0, 420, 297))
            assert _wire_snapshot(session) == orig_wires
            _assert_wire_connected(session, w1.id)
            snapshots.append(_dump(session))

        assert len(session._undo) == len(snapshots) - 1

        # 3. Full undo stack back to the start, then full redo back to the end.
        for expected in reversed(snapshots[:-1]):
            win.undo_edit()
            app.processEvents()
            assert _dump(session) == expected
            assert _wire_snapshot(session) == orig_wires
        assert not session.can_undo()

        for expected in snapshots[1:]:
            win.redo_edit()
            app.processEvents()
            assert _dump(session) == expected
            assert _wire_snapshot(session) == orig_wires

        # 4. Also drag w2 after undo/redo to ensure handles & scene stay healthy.
        canvas.fit_page()
        app.processEvents()
        for _ in range(5):
            canvas.zoom_by(1.15)
        wi2 = _wire_item(canvas, w2.id)
        mid2 = wi2.path().pointAtPercent(0.5)
        _drag_real_mouse(canvas, app, mid2, QPointF(mid2.x(), mid2.y() + 12.5))
        _assert_wire_connected(session, w2.id)
        assert _wire_snapshot(session) == orig_wires
    finally:
        win.close()


def test_wire_drag_after_reroute_component_move_and_cached_sheet_switch() -> None:
    """Drag corners and segments after manual reroute, device move auto-reroute, and cached sheet switch."""
    app = _app()
    session = Session.create("RerouteAndSwitch")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=60, y=60)
    session.place("nema.contactors.coil", ref="M1", x=180, y=80)
    w = session.wire("PB1.4", "M1.A1", number="201")
    session.add_sheet("page2", title="Page 2", kind="schematic", make_active=False)
    session.place("nema.operators.pb_no", ref="PB2", x=80, y=80, sheet_id="page2")
    session._undo.clear()
    session._undo_tip = None
    orig_wires = _wire_snapshot(session)

    win = MainWindow(session)
    try:
        win.resize(1100, 620)
        win.show()
        app.processEvents()
        canvas = win.canvas
        canvas.fit_page()
        app.processEvents()

        # 1. Explicit reroute via session API, then zoom in and drag a corner.
        session.set_wire_route(w.id, [Point(x=115, y=84), Point(x=115, y=104), Point(x=155, y=104)])
        canvas._redraw_wires()
        app.processEvents()
        canvas.select_ref(w.id)
        app.processEvents()
        for _ in range(6):
            canvas.zoom_by(1.15)
        corners = sorted(
            (h for h in _handles(canvas, w.id) if h.kind == "corner"),
            key=lambda h: h.corner_index,
        )
        assert corners
        _drag_real_mouse(
            canvas,
            app,
            corners[0].pos(),
            QPointF(corners[0].pos().x() + 10.0, corners[0].pos().y() + 10.0),
        )
        _assert_wire_connected(session, w.id)
        assert _wire_snapshot(session) == orig_wires

        # 2. Switch to page2 while zoomed out, zoom in on page2, switch back to cached page1!
        sheet0_id = session.project.sheets[0].id
        canvas.fit_page()
        app.processEvents()
        win._switch_sheet("page2")
        app.processEvents()
        for _ in range(7):
            canvas.zoom_by(1.2)
        app.processEvents()
        win._switch_sheet(sheet0_id)
        app.processEvents()

        # Drag corner on the remounted cached scene!
        canvas.select_ref(w.id)
        app.processEvents()
        corners = sorted(
            (h for h in _handles(canvas, w.id) if h.kind == "corner"),
            key=lambda h: h.corner_index,
        )
        assert corners
        _drag_real_mouse(
            canvas,
            app,
            corners[0].pos(),
            QPointF(corners[0].pos().x() - 7.5, corners[0].pos().y() + 5.0),
        )
        _assert_wire_connected(session, w.id)
        assert _wire_snapshot(session) == orig_wires

        # 3. Drag component M1 so wire drops stored waypoints and auto-reroutes, then drag wire again.
        canvas.fit_page()
        app.processEvents()
        m1_item = next(
            i for i in canvas.scene.items() if isinstance(i, ComponentItem) and i.component_id == "M1"
        )
        m1_center = m1_item.sceneBoundingRect().center()
        _drag_real_mouse(canvas, app, m1_center, QPointF(m1_center.x() + 10.0, m1_center.y() + 15.0))
        assert session.find_wire(wire_id=w.id).points == []

        for _ in range(6):
            canvas.zoom_by(1.15)
        wi = _wire_item(canvas, w.id)
        mid = wi.path().pointAtPercent(0.5)
        _drag_real_mouse(canvas, app, mid, QPointF(mid.x(), mid.y() + 12.5))
        _assert_wire_connected(session, w.id)
        assert _wire_snapshot(session) == orig_wires

        # Undo segment drag -> back to auto-routed; redo -> back to pulled route.
        pulled_pts = [(p.x, p.y) for p in session.find_wire(wire_id=w.id).points]
        assert pulled_pts
        win.undo_edit()
        app.processEvents()
        assert session.find_wire(wire_id=w.id).points == []
        win.redo_edit()
        app.processEvents()
        assert [(p.x, p.y) for p in session.find_wire(wire_id=w.id).points] == pulled_pts
        assert _wire_snapshot(session) == orig_wires
    finally:
        win.close()


def test_wire_and_pin_bounding_rects_respect_prepare_geometry_change() -> None:
    """Zoom changes must never mutate WireItem/WireHandleItem boundingRect before prepareGeometryChange."""
    app = _app()
    session = Session.create("BoundingRectContract")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=60, y=70)
    session.place("nema.contactors.coil", ref="M1", x=180, y=70)
    w = session.wire("PB1.4", "M1.A1", number="101")
    session.set_wire_route(w.id, [Point(x=110, y=94), Point(x=110, y=114), Point(x=160, y=114)])

    view = SchematicView(session)
    try:
        view.resize(1100, 550)
        view.show()
        view.fit_page()
        app.processEvents()
        view.select_ref(w.id)
        app.processEvents()

        wi = _wire_item(view, w.id)
        handles = _handles(view, w.id)
        assert handles

        br_wire_before = wi.boundingRect()
        br_handle_before = handles[0].boundingRect()

        # Raw view transform change without calling _refresh_wire_hit_geometry:
        # boundingRect() MUST NOT change behind QGraphicsScene's BSP index.
        view.scale(4.0, 4.0)
        assert wi.boundingRect() == br_wire_before
        assert handles[0].boundingRect() == br_handle_before

        # _refresh_wire_hit_geometry updates WireItem via prepareGeometryChange() FIRST.
        events: list[tuple[str, QRectF]] = []
        orig_pgc = wi.prepareGeometryChange

        def spy_pgc() -> None:
            events.append(("prepare", wi.boundingRect()))
            orig_pgc()

        wi.prepareGeometryChange = spy_pgc  # type: ignore[method-assign]
        view._refresh_wire_hit_geometry()
        assert events == [("prepare", br_wire_before)]
        assert wi.boundingRect() != br_wire_before
        assert handles[0].boundingRect() == br_handle_before

        # PinItem.set_locked() must also call prepareGeometryChange() BEFORE changing boundingRect().
        pin = next(i for i in view.scene.items() if isinstance(i, PinItem))
        pin_br_locked = pin.boundingRect()
        pin_events: list[tuple[str, QRectF]] = []
        orig_pin_pgc = pin.prepareGeometryChange

        def spy_pin_pgc() -> None:
            pin_events.append(("prepare", pin.boundingRect()))
            orig_pin_pgc()

        pin.prepareGeometryChange = spy_pin_pgc  # type: ignore[method-assign]
        pin.set_locked(False)
        assert pin_events == [("prepare", pin_br_locked)]
        pin_br_unlocked = pin.boundingRect()
        assert pin_br_unlocked != pin_br_locked
        pin.set_locked(True)
        assert pin_events[-1] == ("prepare", pin_br_unlocked)
        assert pin.boundingRect() == pin_br_locked
    finally:
        view.release_scenes()
        view.close()


def test_background_check_worker_never_calls_qt_font_metrics(monkeypatch) -> None:
    """run_checks() and off-GUI-thread text_advance_mm() must not construct QFont/QFontMetricsF."""
    import panelcad.render.qt_painter as qt_painter

    session = Session.open(_DOL)

    # build_nameplate(include_marks=False) must keep the exact same validation issues.
    full_plate = build_nameplate(session.project, session.catalog, include_marks=True)
    fast_plate = build_nameplate(session.project, session.catalog, include_marks=False)
    assert fast_plate.issues == full_plate.issues

    # Spy QFontMetricsF to guarantee run_checks on a worker thread never invokes it.
    qt_calls: list[str] = []

    class _ForbiddenFontMetrics:
        def __init__(self, *args, **kwargs):
            qt_calls.append(threading.current_thread().name)
            raise AssertionError("QFontMetricsF must not be called from run_checks / worker thread")

    monkeypatch.setattr(qt_painter, "QFontMetricsF", _ForbiddenFontMetrics)

    result_rows: list = []
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            rows = run_checks(
                session.project,
                session.catalog,
                include_suppressed=True,
                parts=session.parts,
                project_path=session.path,
            )
            result_rows.extend(rows)
            # Even direct text_advance_mm() on a worker thread must avoid QFontMetricsF.
            width = qt_painter.text_advance_mm("480 V 3 PH", 2.2)
            assert width > 0.0
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=_worker, name="test-check-worker")
    t.start()
    t.join(timeout=30.0)
    assert not t.is_alive()
    assert not errors, errors
    assert not qt_calls
    assert result_rows


def test_two_windows_concurrent_wire_drags_and_live_checks() -> None:
    """Two open MainWindows (project + copy) with live checks active drag wires cleanly."""
    app = _app()
    s1 = Session.open(_DOL)
    s1.set_active_sheet("control")
    s2 = Session.open(_DOL)
    s2.set_active_sheet("control")

    w1 = MainWindow(s1)
    w2 = MainWindow(s2)
    try:
        for win in (w1, w2):
            win.resize(1100, 580)
            win.show()
            win.canvas.fit_page()
        app.processEvents()

        w1._check_runner.run_now()
        w2._check_runner.run_now()

        for win, sess in ((w1, s1), (w2, s2)):
            canvas = win.canvas
            orig_wires = _wire_snapshot(sess)
            target_wire = None
            for wire in sess.sheet().wires:
                canvas.select_ref(wire.id)
                app.processEvents()
                if any(h.kind == "corner" for h in _handles(canvas, wire.id)):
                    target_wire = wire
                    break
            assert target_wire is not None
            for _ in range(6):
                canvas.zoom_by(1.15)
            corners = [h for h in _handles(canvas, target_wire.id) if h.kind == "corner"]
            _drag_real_mouse(
                canvas,
                app,
                corners[0].pos(),
                QPointF(corners[0].pos().x() + 5.0, corners[0].pos().y() + 5.0),
                process_mid=False,
            )
            _ = canvas.scene.items(QRectF(0, 0, 420, 297))
            assert _wire_snapshot(sess) == orig_wires
            _assert_wire_connected(sess, target_wire.id)
    finally:
        w2.close()
        w1.close()
        app.processEvents()


def test_multi_wire_selection_redraw_creates_no_transient_handles(monkeypatch) -> None:
    """Redrawing a multi-wire selection must not allocate transient WireHandleItems mid-loop."""
    app = _app()
    session = Session.create("MultiWireRedraw")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=60, y=60)
    session.place("nema.operators.pb_no", ref="PB2", x=60, y=110)
    session.place("nema.contactors.coil", ref="M1", x=180, y=60)
    session.place("nema.contactors.coil", ref="M2", x=180, y=110)
    w1 = session.wire("PB1.4", "M1.A1", number="101")
    w2 = session.wire("PB2.4", "M2.A1", number="102")
    session.set_wire_route(w1.id, [Point(x=110, y=84), Point(x=110, y=96), Point(x=155, y=96)])
    session.set_wire_route(w2.id, [Point(x=110, y=134), Point(x=110, y=146), Point(x=155, y=146)])

    view = SchematicView(session)
    try:
        view.resize(1100, 550)
        view.show()
        view.fit_page()
        app.processEvents()

        wi1 = _wire_item(view, w1.id)
        wi2 = _wire_item(view, w2.id)
        wi1.setSelected(True)
        wi2.setSelected(True)
        app.processEvents()
        assert len(view.scene.selectedItems()) == 2
        assert _handles(view) == []

        created_handles: list[str] = []
        orig_init = WireHandleItem.__init__

        def spy_init(self, wire_id: str, *args, **kwargs) -> None:
            created_handles.append(wire_id)
            orig_init(self, wire_id, *args, **kwargs)

        monkeypatch.setattr(WireHandleItem, "__init__", spy_init)
        view._redraw_wires()
        app.processEvents()
        assert created_handles == []
        assert _handles(view) == []

        # Single-wire selection after redraw still creates handles normally.
        view.select_ref(w1.id)
        app.processEvents()
        assert len(created_handles) >= 3
        assert len(_handles(view, w1.id)) == len(created_handles)
    finally:
        view.release_scenes()
        view.close()


def test_deleted_graphics_items_are_pruned_and_real_runtime_errors_propagate(monkeypatch) -> None:
    """Deleted C++ wrappers are detected via shiboken6.isValid, logged, and pruned without masking real errors."""
    app = _app()
    session = Session.create("DeletedItemCheck")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_no", ref="PB1", x=60, y=70)
    session.place("nema.contactors.coil", ref="M1", x=180, y=70)
    w = session.wire("PB1.4", "M1.A1", number="101")

    messages: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    canvas_log = logging.getLogger("panelcad.gui.canvas")
    handler = _Capture(logging.DEBUG)
    prev_level = canvas_log.level
    canvas_log.addHandler(handler)
    canvas_log.setLevel(logging.DEBUG)

    view = SchematicView(session)
    try:
        view.resize(1100, 550)
        view.show()
        view.fit_page()
        app.processEvents()

        # Construct a real WireItem whose underlying C++ object is then deleted by clearing a temp scene.
        temp_scene = QGraphicsScene()
        dead_wire = WireItem(QPainterPath(), wire_id=w.id)
        temp_scene.addItem(dead_wire)
        temp_scene.clear()

        messages.clear()
        view._items.insert(0, dead_wire)
        view._wire_hit_scale = -1.0
        view._refresh_wire_hit_geometry()
        assert dead_wire not in view._items
        assert any("Pruning deleted WireItem in _refresh_wire_hit_geometry" in msg for msg in messages)

        # Also verify _select_wire_item skips/prunes a deleted WireItem and selects the live one.
        messages.clear()
        view._items.insert(0, dead_wire)
        view._select_wire_item(w.id)
        assert dead_wire not in view._items
        assert _wire_item(view, w.id).isSelected()
        assert any("Pruning deleted WireItem in _select_wire_item" in msg for msg in messages)

        # A real RuntimeError raised by a live WireItem must NOT be silently swallowed.
        live_wire = _wire_item(view, w.id)

        def boom(_width: float) -> None:
            raise RuntimeError("real bug")

        monkeypatch.setattr(live_wire, "set_hit_width_scene", boom)
        view._wire_hit_scale = -1.0
        with pytest.raises(RuntimeError, match="real bug"):
            view._refresh_wire_hit_geometry()
    finally:
        canvas_log.removeHandler(handler)
        canvas_log.setLevel(prev_level)
        view.release_scenes()
        view.close()
