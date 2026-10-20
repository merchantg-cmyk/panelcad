"""End-to-end path an AI takes: list, view, edit, validate, print."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

from panelcad import Session
from panelcad.cli.agent import Agent, run_agent
from panelcad.examples.dol import build_dol


def test_session_describe_walks_every_sheet():
    s = build_dol()
    snap = s.describe()
    assert snap["sheet_count"] == 6
    ids = [sh["id"] for sh in snap["sheets"]]
    assert ids == ["power", "control", "io", "enclosure", "ethernet", "serial"]
    power = snap["sheets"][0]
    assert any(c["id"] == "Q1" for c in power["components"])
    assert "2" in next(c["pins"] for c in power["components"] if c["id"] == "Q1")
    io = snap["sheets"][2]
    assert any(c["library_id"] == "adc.click.c0_16nd3" for c in io["components"])
    assert snap["title_block"]["drawing_number"] == "E-001"
    assert snap["errors"] == []


def test_ai_can_edit_view_and_print(tmp_path: Path):
    s = build_dol()
    s.set_active_sheet("control")
    s.place("iec.terminals.x_2p5", ref="X9", x=80, y=80)
    s.set_component_properties("X9", {"manufacturer": "Wago", "part_number": "2002-1201"})
    s.move("X9", 90, 85)
    s.set_title_block(drawing_number="E-001-A", rev="B", company="AI Edited LLC")
    s.set_sheet_title("Control & Interlocks", "control")

    snap = s.describe("control")
    x9 = next(c for c in snap["sheets"][0]["components"] if c["id"] == "X9")
    assert x9["x"] == 90
    assert x9["properties"]["part_number"] == "2002-1201"
    assert snap["title_block"]["company"] == "AI Edited LLC"
    assert snap["sheets"][0]["title"] == "Control & Interlocks"
    assert s.validate() == []

    pdf = tmp_path / "walk.pdf"
    s.export_pdf(pdf)
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        QApplication([])
    from PySide6.QtPdf import QPdfDocument

    doc = QPdfDocument()
    doc.load(str(pdf))
    assert doc.pageCount() == 6


def test_agent_help_and_full_walkthrough(tmp_path: Path):
    agent = Agent()
    help_r = agent.handle({"id": 0, "op": "help"})
    assert help_r["ok"]
    assert "inspect" in help_r["result"]["ops"]
    assert "list_sheets" in help_r["result"]["ops"]

    src = tmp_path / "job.pcad.yaml"
    build_dol().save(src)
    steps = [
        {"id": 1, "op": "open", "path": str(src)},
        {"id": 2, "op": "list_sheets"},
        {"id": 3, "op": "inspect"},
        {"id": 4, "op": "set_active_sheet", "sheet_id": "io"},
        {"id": 5, "op": "inspect_device", "library_id": "adc.durapulse.gs21_20p5"},
        {"id": 6, "op": "place", "library_id": "iec.terminals.x_2p5", "ref": "X20", "x": 50, "y": 180},
        {"id": 7, "op": "set_properties", "ref": "X20", "manufacturer": "Wago", "part_number": "2002-1201"},
        {"id": 8, "op": "move", "ref": "X20", "x": 55, "y": 185},
        {"id": 9, "op": "set_title_block", "drawing_number": "E-AI-1", "rev": "C"},
        {"id": 10, "op": "validate"},
        {"id": 11, "op": "export_pdf", "path": str(tmp_path / "ai.pdf")},
        {"id": 12, "op": "export_dxf", "path": str(tmp_path / "ai.dxf"), "sheet": "power"},
        {"id": 13, "op": "save", "path": str(src)},
    ]
    stdin = StringIO("\n".join(json.dumps(s) for s in steps) + "\n")
    stdout = StringIO()
    run_agent(stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert all(r["ok"] for r in replies), replies
    sheets = next(r for r in replies if r["id"] == 2)["result"]["sheets"]
    assert len(sheets) == 6
    inspect = next(r for r in replies if r["id"] == 3)["result"]
    assert inspect["sheet_count"] == 6
    pins = next(r for r in replies if r["id"] == 5)["result"]["pins"]
    assert any(p["id"] in ("L", "DI1") for p in pins)
    again = Session.open(src)
    assert again.project.title_block.drawing_number == "E-AI-1"
    assert again.sheet("io").component("X20").x == 55


def test_gui_click_does_not_drop_title_block():
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import TitleBlockItem
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow(build_dol())
    win.show()
    canvas = win.canvas
    before = sum(1 for i in canvas.scene.items() if isinstance(i, TitleBlockItem))
    assert before == 1
    # Click empty schematic space and the title-block strip.
    for pt in (QPoint(200, 200), QPoint(400, 700), QPoint(80, 80)):
        ev = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            pt.toPointF(),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        canvas.mousePressEvent(ev)
        ev2 = QMouseEvent(
            QMouseEvent.Type.MouseButtonRelease,
            pt.toPointF(),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        canvas.mouseReleaseEvent(ev2)
        app.processEvents()
    after = [i for i in canvas.scene.items() if isinstance(i, TitleBlockItem)]
    assert len(after) == 1
    assert after[0].isVisible()
    win.close()
