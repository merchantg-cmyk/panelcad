"""Problems dock, canvas markers, and a PNG of the live checks UI."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from panelcad import Session
from panelcad.core.models import Wire
from panelcad.gui.canvas import (
    ComponentItem,
    FindingMarker,
    device_drawn_scene_bounds,
    finding_content_rect,
    group_finding_rows,
)
from panelcad.gui.main_window import MainWindow


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_checks(win: MainWindow, app: QApplication, tick: int, timeout_ms: int = 4000) -> None:
    from PySide6.QtCore import QElapsedTimer

    timer = QElapsedTimer()
    timer.start()
    while win._checks_tick <= tick and timer.elapsed() < timeout_ms:
        app.processEvents()
    assert win._checks_tick > tick


def _problem_session() -> Session:
    s = Session.create("Live Checks Demo")
    s.place("iec.contactors.coil", ref="K1", x=40, y=40)
    s.place("iec.operators.pb_no", ref="S1", x=120, y=40)
    s.place("iec.terminals.x_2p5", ref="GND", x=40, y=100)
    s.wire("K1.A1", "K1.A2", net="120VAC")
    s.sheet().wires.append(Wire.model_validate({"id": "wbad", "from": "S1.13", "to": "X9-9.A"}))
    return s


def test_problems_panel_and_canvas_markers():
    app = _app()
    win = MainWindow(_problem_session())
    win.resize(1400, 860)
    win.show()
    app.processEvents()
    tick = win._checks_tick
    win._check_runner.run_now()
    _wait_checks(win, app, tick)
    win.problems.show_ignored.setChecked(True)
    win.problems._rebuild()
    checks = {row["check"] for row in win.problems._rows}
    assert "dangling_wire" in checks
    assert "coil_voltage_mismatch" in checks
    assert "terminal_not_on_strip" in checks
    assert win.problems.table.rowCount() >= 3
    markers = [item for item in win.canvas._items if isinstance(item, FindingMarker)]
    assert markers
    by_ref = {item.anchor_ref: item for item in markers}
    assert "K1" in by_ref
    k1_marker = by_ref["K1"]
    assert k1_marker.count >= 2
    k1 = next(
        item
        for item in win.canvas._items
        if isinstance(item, ComponentItem) and item.component_id == "K1"
    )
    content = k1.mapRectToScene(finding_content_rect(k1))
    badge = k1_marker.mapRectToScene(k1_marker.boundingRect())
    assert not content.intersects(badge)
    assert badge.left() >= content.right() - 0.4
    assert badge.center().y() <= content.center().y() + 0.4
    shapes = {item.shape_kind() for item in markers}
    assert "triangle" in shapes
    info = next((item for item in markers if item.severity() == "info"), None)
    if info is not None:
        assert info.shape_kind() == "circle"
    win.problems.table.selectRow(0)
    row = win.problems.current_row()
    assert row
    win._jump_finding(row)
    app.processEvents()
    win.close()


def test_markers_do_not_intersect_their_device_bounds():
    """Badge rect stays off the device's drawn symbol + tag + pins."""

    app = _app()
    win = MainWindow(_problem_session())
    win.resize(1400, 860)
    win.show()
    app.processEvents()
    tick = win._checks_tick
    win._check_runner.run_now()
    _wait_checks(win, app, tick)
    devices = {
        item.component_id: item
        for item in win.canvas._items
        if isinstance(item, ComponentItem)
    }
    markers = [item for item in win.canvas._items if isinstance(item, FindingMarker)]
    assert markers
    checked = 0
    for marker in markers:
        device = devices.get(marker.anchor_ref)
        if device is None:
            continue
        badge = marker.mapRectToScene(marker.boundingRect())
        drawn = device_drawn_scene_bounds(device)
        assert not badge.intersects(drawn), (
            f"{marker.anchor_ref}: marker {badge} overlaps device {drawn}"
        )
        gap_x = badge.left() - drawn.right()
        gap_y = drawn.top() - badge.bottom()
        assert gap_x >= -0.05, f"{marker.anchor_ref}: marker not to the right ({gap_x})"
        assert gap_y >= -0.05, f"{marker.anchor_ref}: marker not above ({gap_y})"
        assert gap_x <= 4.0 * 2.5, f"{marker.anchor_ref}: marker too far right ({gap_x})"
        assert gap_y <= 4.0 * 2.5, f"{marker.anchor_ref}: marker too far above ({gap_y})"
        checked += 1
    assert checked >= 3
    win.close()


def test_inspector_ai_tabs_are_not_duplicated_on_problems():
    from PySide6.QtWidgets import QTabBar

    app = _app()
    win = MainWindow(_problem_session())
    win.resize(1400, 860)
    win.show()
    app.processEvents()
    win.problems_wrap.show()
    win.problems_wrap.raise_()
    app.processEvents()
    inspector_ai = []
    for bar in win.findChildren(QTabBar):
        titles = [bar.tabText(i).replace("&", "") for i in range(bar.count())]
        if "Inspector" in titles and "AI" in titles:
            inspector_ai.append(titles)
    assert len(inspector_ai) == 1, inspector_ai
    win.close()


def test_group_finding_rows_collapses_one_device():
    groups = group_finding_rows(
        [
            {"ref": "K1", "severity": "info", "check": "missing_part_number"},
            {"ref": "K1", "severity": "warning", "check": "missing_panel"},
            {"ref": "wbad", "severity": "error", "check": "dangling_wire"},
        ],
        {"K1"},
        {"wbad"},
    )
    kinds = {(kind, ident, len(rows), rows[0]["severity"]) for kind, ident, rows in groups}
    assert ("comp", "K1", 2, "warning") in kinds
    assert ("wire", "wbad", 1, "error") in kinds
    parked = group_finding_rows(
        [
            {"ref": "wbad", "target": "X9-9.A", "severity": "error", "check": "dangling_wire"},
            {"ref": "S1", "severity": "warning", "check": "missing_panel"},
        ],
        {"K1", "S1"},
        set(),
        hosts={"wbad": "S1"},
    )
    parked_kinds = {(kind, ident, len(rows), rows[0]["severity"]) for kind, ident, rows in parked}
    assert ("comp", "S1", 2, "error") in parked_kinds
    assert FindingMarker({"severity": "info", "message": "no pn"}).shape_kind() == "circle"
    assert FindingMarker({"severity": "warning", "message": "gap"}).shape_kind() == "triangle"
    assert FindingMarker({"severity": "error", "message": "bad"}).shape_kind() == "triangle"


def test_gui_ignore_finding_persists_on_the_project():
    app = _app()
    win = MainWindow(_problem_session())
    win.show()
    app.processEvents()
    tick = win._checks_tick
    win._check_runner.run_now()
    _wait_checks(win, app, tick)
    hit = next(row for row in win.problems._rows if row["check"] == "missing_part_number")
    tick = win._checks_tick
    win._suppress_finding(hit["id"])
    _wait_checks(win, app, tick)
    assert hit["id"] in win.session.project.properties.get("suppressed_checks", [])
    assert not any(
        row["id"] == hit["id"] and row.get("suppressed") != "yes" for row in win.problems.visible_rows()
    )
    win.close()


def test_render_problems_panel_png(tmp_path: Path):
    from PySide6.QtGui import QFont

    app = _app()
    app.setStyle("Fusion")
    app.setFont(QFont("DejaVu Sans", 10))
    win = MainWindow(_problem_session())
    win.resize(1480, 920)
    win.show()
    app.processEvents()
    win.problems_wrap.show()
    win.problems_wrap.raise_()
    win.resizeDocks([win.problems_wrap], [240], Qt.Orientation.Vertical)
    tick = win._checks_tick
    win._check_runner.run_now()
    _wait_checks(win, app, tick)
    win.canvas.resetTransform()
    # Wide enough that a 1-grid standoff still reads as "next to" the device.
    win.canvas.scale(3.5, 3.5)
    win.canvas.centerOn(92, 78)
    app.processEvents()
    pix = win.grab()
    assert not pix.isNull()
    from tests.artifacts import artifact_dir

    dest = artifact_dir(tmp_path) / "problems-panel-live-checks.png"
    assert pix.save(str(dest), "PNG")
    assert dest.is_file() and dest.stat().st_size > 1000
    win.close()


def test_problems_rebuild_fills_big_table_once():
    """1371 findings fill in one layout pass and keep every cell."""

    import inspect
    import time

    from PySide6.QtWidgets import QHeaderView

    from panelcad.gui.problems import ProblemsDock

    source = inspect.getsource(ProblemsDock)
    assert source.count("fix_requested = Signal(dict)") == 1
    app = _app()
    dock = ProblemsDock()
    dock.resize(1100, 700)
    dock.show()
    app.processEvents()
    rows = []
    for i in range(1371):
        severity = "error" if i % 5 == 0 else "warning"
        rows.append(
            {
                "id": str(i),
                "severity": severity,
                "check": f"layout_not_ready_{i % 17}",
                "message": f"finding {i} " + ("needs a catalog envelope " * 8),
                "sheet": "power" if i % 2 == 0 else "control",
                "ref": f"K{i}",
                "suppressed": "no",
            }
        )
    started = time.perf_counter()
    dock.set_rows(rows)
    elapsed = time.perf_counter() - started
    assert dock.table.rowCount() == 1371
    assert elapsed < 2.0, f"live check table took {elapsed:.2f}s"
    for i in (0, 100, 1370):
        for col, field in enumerate(("severity", "check", "message", "sheet", "ref")):
            item = dock.table.item(i, col)
            assert item is not None
            assert item.text() == rows[i][field]
            assert item.data(Qt.ItemDataRole.UserRole) == rows[i]
            assert item.toolTip() == rows[i][field]
    header = dock.table.horizontalHeader()
    assert header.sectionResizeMode(0) == QHeaderView.ResizeMode.ResizeToContents
    assert header.sectionResizeMode(1) == QHeaderView.ResizeMode.Interactive
    assert header.sectionResizeMode(2) == QHeaderView.ResizeMode.Stretch
    vmode = dock.table.verticalHeader().sectionResizeMode(0)
    assert vmode == QHeaderView.ResizeMode.ResizeToContents
    assert dock.table.wordWrap()
    assert dock.table.updatesEnabled()
    assert not dock.table.isSortingEnabled()
    dock.filter.setCurrentIndex(1)
    assert dock.table.rowCount() == sum(1 for row in rows if row["severity"] == "error")
    assert dock.table.item(0, 4).text() == "K0"
    got = []
    dock.fix_requested.connect(got.append)
    dock.table.selectRow(0)
    dock._fix_current()
    assert len(got) == 1
    dock.close()


def test_canvas_reload_computes_wire_marks_once():
    """A cold visit places wire numbers once. The same sheet is not placed again."""

    from panelcad.gui.canvas import SchematicView
    from panelcad.render.label_avoid import clear_resolved_labels
    from panelcad.render.wire_labels import reset_wire_mark_stats, wire_mark_stats

    app = _app()
    session = Session.create("Reload marks")
    session.place("iec.contactors.coil", ref="K1", x=40, y=40)
    session.place("iec.operators.pb_no", ref="S1", x=120, y=40)
    session.wire("K1.A1", "S1.13", net="120VAC")
    clear_resolved_labels()
    reset_wire_mark_stats()
    view = SchematicView(session)
    app.processEvents()
    calls, computes = wire_mark_stats()
    assert calls > computes == 1
    reset_wire_mark_stats()
    view.reload()
    assert wire_mark_stats() == (0, 0)
    view.deleteLater()
