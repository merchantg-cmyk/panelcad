from panelcad import Session
from panelcad.core.layout import is_io_sheet
from panelcad.examples.dol import build_dol
from panelcad.render.io_ladder import is_io_module


def test_dol_io_sheet_is_ladder():
    s = build_dol()
    io = s.sheet("io")
    assert is_io_sheet(io)
    cpu = s.catalog.get("adc.click.c2_03cpu")
    assert is_io_module(cpu)
    assert s.sheet("io").component("A2").x >= 140
    vfd = s.sheet("io").component("U1")
    assert vfd.x > s.sheet("io").component("A2").x


def test_layout_io_from_empty_sheet():
    s = Session.create("IO")
    s.place("adc.click.c2_03cpu", ref="A2", x=10, y=10)
    s.place("iec.operators.pb_no", ref="S1", x=80, y=10)
    s.wire("S1.14", "A2.X0")
    s.add_sheet("ladder", title="I/O", kind="io")
    result = s.layout_io()
    assert result["modules"] >= 1
    assert is_io_sheet(s.sheet("ladder"))


def test_layout_io_field_in_load_out():
    s = Session.create("Ladder")
    s.place("adc.click.c2_03cpu", ref="A2", x=10, y=10)
    s.place("iec.operators.pb_no", ref="S1", x=80, y=10)
    s.place("iec.operators.pilot_light", ref="H1", x=120, y=10)
    s.wire("S1.14", "A2.X0")
    s.wire("A2.Y0", "H1.X1")
    s.add_sheet("io", title="I/O", kind="io")
    s.layout_io()
    io = s.sheet("io")
    assert io.component("S1").x < io.component("A2").x < io.component("H1").x


def test_io_input_card_right_output_card_left():
    from panelcad.render.io_ladder import IN_CARD_X, OUT_CARD_X, CARD_X, io_module_kind

    s = Session.create("Cols")
    s.place("iec.plc.di16", ref="DI", x=10, y=10)
    s.place("iec.plc.do16", ref="DO", x=80, y=10)
    s.place("iec.plc.compact_16dio", ref="CPU", x=40, y=10)
    s.add_sheet("io", title="I/O", kind="io")
    s.layout_io()
    io = s.sheet("io")
    assert io_module_kind(s.catalog.get("iec.plc.di16")) == "input"
    assert io_module_kind(s.catalog.get("iec.plc.do16")) == "output"
    assert io_module_kind(s.catalog.get("iec.plc.compact_16dio")) == "combo"
    assert io.component("DI").x == IN_CARD_X
    assert io.component("DO").x == OUT_CARD_X
    assert io.component("CPU").x == CARD_X


def _wire_count(canvas) -> int:
    from panelcad.gui.canvas import WireItem

    return sum(1 for item in canvas.scene.items() if isinstance(item, WireItem))


def test_click_wire_does_not_remove_it():
    import gc

    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import WireItem
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    s = Session.create("Wires")
    s.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    s.place("iec.operators.pilot_light", ref="H1", x=120, y=40)
    s.wire("S1.14", "H1.X1", net="RUN")
    win = MainWindow(s)
    win.show()
    app.processEvents()
    canvas = win.canvas
    mids = [
        item.path().pointAtPercent(0.5)
        for item in canvas.scene.items()
        if isinstance(item, WireItem)
    ]
    assert mids
    model_n = len(s.sheet().wires)
    gc.collect()
    app.processEvents()
    assert _wire_count(canvas) == model_n
    pt = canvas.mapFromScene(mids[0])
    QTest.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pt)
    app.processEvents()
    gc.collect()
    app.processEvents()
    assert _wire_count(canvas) == model_n
    assert len(s.sheet().wires) == model_n
    selected = [i for i in canvas.scene.items() if isinstance(i, WireItem) and i.isSelected()]
    assert selected
    win.close()


def test_dol_control_wire_clicks_survive_gc():
    """Labeled wires on the DOL control sheet vanished on left-click (PySide GC)."""
    import gc

    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import WireItem
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    s = build_dol()
    s.set_active_sheet("control")
    win = MainWindow(s)
    win.show()
    win.resize(1400, 860)
    app.processEvents()
    canvas = win.canvas
    canvas.fit_page()
    app.processEvents()
    model_n = len(s.sheet().wires)
    assert model_n >= 1
    mids = [
        (item.wire_id, item.path().pointAtPercent(0.5))
        for item in canvas.scene.items()
        if isinstance(item, WireItem)
    ]
    assert len(mids) == model_n
    gc.collect()
    assert _wire_count(canvas) == model_n
    for _wid, mid in mids:
        pt = canvas.mapFromScene(mid)
        QTest.mouseClick(
            canvas.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            pt,
        )
        app.processEvents()
        gc.collect()
        assert _wire_count(canvas) == model_n
        assert len(s.sheet().wires) == model_n
        canvas.cancel_wire()
    win.close()
