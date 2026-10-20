"""Graphical terminal strip: drawing, report, YAML, and BOM stay on the same records."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from PySide6.QtWidgets import QApplication

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.core.electrical import symbol_role
from panelcad.gui.canvas import ComponentItem
from panelcad.gui.main_window import MainWindow
from panelcad.gui.strip_canvas import StripBlockItem
from panelcad.gui.terminal_dialog import TerminalStripDialog
from panelcad.io.bom import grouped_rows
from panelcad.io.yaml_format import dump_project
from panelcad.render.svg import sheet_to_svg
from tests.support import confirm_layout_occupants


@pytest.fixture(autouse=True)
def _confirm_before_strip_panel(monkeypatch):
    from panelcad.core import terminal_strip

    orig = terminal_strip.sync_strip_panel

    def _sync(session, strip):
        confirm_layout_occupants(session)
        return orig(session, strip)

    monkeypatch.setattr(terminal_strip, "sync_strip_panel", _sync)


def _qty(session: Session, library_id: str) -> int:
    rows = grouped_rows(session.project, session.catalog)
    return sum(int(row["qty"]) for row in rows if row["library_id"] == library_id)


def _wired_strip() -> Session:
    session = Session.create("Strip drawing")
    session.place("iec.terminals.x_2p5", ref="X1-3", x=40, y=20)
    session.place("iec.terminals.x_2p5", ref="X1-1", x=40, y=80)
    session.place("iec.terminals.x_2p5", ref="X1-2", x=40, y=50)
    session.set_wire_numbering(enabled=False)
    session.wire("X1-1.A", "X1-2.A", net="INT")
    session.wire("X1-1.B", "X1-3.B", net="FIELD")
    session.number_wires(start=10, scheme="sequential")
    for wire in session.sheet().wires:
        ends = f"{wire.frm} {wire.to}"
        wire.cable = "CBL-A" if ".A" in ends else "CBL-B"
    return session


def test_accessories_are_not_strip_levels():
    session = Session.create("Roles")
    for library_id in (
        "iec.accessories.end_plate",
        "iec.accessories.end_stop",
        "iec.accessories.partition",
    ):
        assert symbol_role(session.catalog.get(library_id)) == "other"


@pytest.mark.confirm_generic_layout
def test_open_draws_schematic_order_at_catalog_width():
    session = _wired_strip()
    layout = session.open_terminal_strip("X1")
    terminals = [item for item in layout["items"] if item["kind"] == "terminal"]
    assert [item["ref"] for item in terminals] == ["X1-3", "X1-2", "X1-1"]
    assert abs(terminals[0]["w"] - 5.2) < 0.05
    marked = next(item for item in terminals if item["ref"] == "X1-1")
    assert "10" in marked["internal"] and "CBL-A" in marked["internal"]
    assert "10" in marked["external"] and "CBL-B" in marked["external"]
    assert [item["kind"] for item in layout["items"][:2]] == ["end_stop", "end_plate"]
    assert [item["kind"] for item in layout["items"][-2:]] == ["end_plate", "end_stop"]
    session.open_terminal_strip("X1")
    assert _qty(session, "iec.accessories.end_plate") == 2
    assert _qty(session, "iec.accessories.end_stop") == 2


@pytest.mark.confirm_generic_layout
def test_svg_and_pdf_draw_the_strip(tmp_path: Path):
    session = _wired_strip()
    session.jumper_terminals(["X1-1", "X1-2"])
    session.open_terminal_strip("X1")
    sheet = session.project.sheet("strip-x1")
    svg = sheet_to_svg(sheet, session.catalog, session.project)
    assert "Terminal strip X1" in svg
    assert "X1-1" in svg
    assert "10 CBL-A" in svg
    assert "10 CBL-B" in svg
    assert "TS35" in svg
    assert "J1" in svg
    assert "rotate(" in svg
    pdf = tmp_path / "strip.pdf"
    session.export_pdf(pdf)
    assert pdf.stat().st_size > 500


def test_reorder_moves_schematic_and_matches_sequence():
    """Strip reorder wins by moving the schematic. Sequence then changes nothing."""

    session = _wired_strip()
    before = _qty(session, "iec.terminals.x_2p5")
    session.reorder_terminals("X1", ["X1-1", "X1-3", "X1-2"])
    main = session.project.sheet("main")
    assert main.component("X1-1").y == 20
    assert main.component("X1-2").y == 50
    assert main.component("X1-3").y == 80
    rows = [row for row in session.terminal_strip_rows() if row["strip"] == "X1"]
    assert [row["ref"] for row in rows] == ["X1-1", "X1-2", "X1-3"]
    assert [row["terminal"] for row in rows] == ["1", "2", "3"]
    again = session.sequence_terminal_strip("X1", rename=True)
    assert [row["ref"] for row in again] == ["X1-1", "X1-2", "X1-3"]
    assert main.component("X1-1").y == 20
    assert _qty(session, "iec.terminals.x_2p5") == before
    with pytest.raises(ValueError):
        session.reorder_terminals("X1", ["X1-1"])


def test_add_terminal_lands_on_the_schematic_and_the_bom():
    session = _wired_strip()
    before = _qty(session, "iec.terminals.x_2p5")
    added = session.add_terminal("X1", after="X1-3")
    assert _qty(session, "iec.terminals.x_2p5") == before + 1
    assert added["sheet"] == "main"
    assert added["terminal"] == "2"
    home = session.project.sheet("main")
    assert home.component(added["ref"]).library_id == "iec.terminals.x_2p5"
    rows = [row["ref"] for row in session.terminal_strip_rows() if row["strip"] == "X1"]
    assert rows[1] == added["ref"]
    assert session.undo()
    assert _qty(session, "iec.terminals.x_2p5") == before


def test_retag_follows_level_order_and_rewrites_wires():
    session = Session.create("Retag")
    session.place("iec.terminals.x_2p5", ref="X1-1", x=40, y=20)
    session.place("iec.terminals.x_2p5", ref="X1-2", x=40, y=50)
    session.wire("X1-2.A", "X1-1.B", net="N1")
    session.reorder_terminals("X1", ["X1-2", "X1-1"])
    session.retag_terminal_strip("X1")
    wire = session.project.sheet("main").wires[0]
    assert {wire.frm, wire.to} == {"X1-1.A", "X1-2.B"}
    assert session.project.sheet("main").component("X1-1").y == 20
    again = session.sequence_terminal_strip("X1", rename=True)
    assert [row["ref"] for row in again] == ["X1-1", "X1-2"]
    assert session.project.sheet("main").component("X1-1").y == 20


@pytest.mark.confirm_generic_layout
def test_partition_is_hardware_between_blocks_not_a_level():
    session = _wired_strip()
    session.open_terminal_strip("X1")
    added = session.add_strip_accessory("X1", "partition", after="X1-3")
    layout = session.open_terminal_strip("X1")
    items = layout["items"]
    index = next(i for i, item in enumerate(items) if item["ref"] == added["ref"])
    assert items[index - 1]["ref"] == "X1-3"
    assert items[index + 1]["kind"] == "terminal"
    assert _qty(session, "iec.accessories.partition") == 1
    assert all(row["ref"] != added["ref"] for row in session.terminal_strip_rows())
    with pytest.raises(ValueError):
        session.add_strip_accessory("X1", "jumper_bar")


@pytest.mark.confirm_generic_layout
def test_yaml_round_trip_keeps_strip_and_jumper(tmp_path: Path):
    session = _wired_strip()
    session.jumper_terminals(["X1-1", "X1-2"], group="J1")
    session.open_terminal_strip("X1")
    text = dump_project(session.project)
    loaded = yaml.safe_load(text)
    for sheet in loaded["sheets"]:
        if sheet.get("kind") == "terminal":
            assert sheet["strip"] == "X1"
        else:
            assert "strip" not in sheet
    jumped = False
    for sheet in loaded["sheets"]:
        for comp in sheet.get("components") or []:
            if comp["id"] == "X1-1":
                assert comp["properties"]["jumper"] == "J1"
                jumped = True
    assert jumped
    dest = tmp_path / "job.pcad.yaml"
    session.save(dest)
    again = Session.open(dest)
    assert again.project.sheet("strip-x1").strip == "X1"
    assert again.project.sheet("main").component("X1-1").properties["jumper"] == "J1"


def test_move_terminal_follows_the_same_order_rule():
    session = _wired_strip()
    session.move_terminal("X1", "X1-3", "later")
    main = session.project.sheet("main")
    assert [main.component(ref).y for ref in ("X1-1", "X1-2", "X1-3")] == [20, 50, 80]
    rows = [row["ref"] for row in session.terminal_strip_rows() if row["strip"] == "X1"]
    assert rows == ["X1-1", "X1-2", "X1-3"]
    sequenced = [row["ref"] for row in session.sequence_terminal_strip("X1", rename=True)]
    assert sequenced == rows
    with pytest.raises(ValueError):
        session.move_terminal("X1", "X1-1", "sideways")


@pytest.mark.confirm_generic_layout
def test_agent_open_terminal_strip_after_create_project():
    agent = Agent()
    created = agent.handle({"id": 1, "op": "create_project", "name": "Strip"})
    assert created["ok"]
    opened = agent.handle({"id": 2, "op": "open_terminal_strip", "strip": "X1"})
    assert opened["ok"] is True
    assert opened["result"]["strip"] == "X1"
    assert opened["result"]["sheet"] == "strip-x1"
    listed = agent.handle({"id": 3, "op": "list_terminal_strips"})
    assert listed["result"]["strips"][0]["strip"] == "X1"
    help_result = agent.handle({"id": 4, "op": "help"})
    for op in (
        "open_terminal_strip",
        "list_terminal_strips",
        "add_terminal",
        "reorder_terminals",
        "move_terminal",
        "retag_terminal_strip",
        "add_strip_accessory",
        "delete_strip_component",
        "export_terminal_strip",
        "clear_terminal_jumpers",
    ):
        assert op in help_result["result"]["ops"]
        assert op in help_result["result"]["help"]


@pytest.mark.confirm_generic_layout
def test_gui_strip_sheet_draws_blocks():
    app = QApplication.instance() or QApplication([])
    session = _wired_strip()
    window = MainWindow(session)
    window.show()
    app.processEvents()
    titles: list[str] = []
    strip_titles: list[str] = []
    for action in window.menuBar().actions():
        menu = action.menu()
        if menu is None:
            continue
        label = menu.title().replace("&", "")
        if label == "Insert":
            titles = [item.text().replace("&", "") for item in menu.actions()]
        if label == "Strip":
            strip_titles = [item.text().replace("&", "") for item in menu.actions() if item.text()]
    assert "Terminal strip…" in titles
    assert "Jumper selected" in strip_titles
    assert "Remove jumper" in strip_titles
    dialog = TerminalStripDialog(["X2"])
    assert dialog.strip_name() == "X2"
    dialog.close()
    window.session.open_terminal_strip("X1")
    window._refresh_from_session(fit=True)
    app.processEvents()
    blocks = [item for item in window.canvas.scene.items() if isinstance(item, StripBlockItem)]
    refs = {item.component_id for item in blocks}
    assert {"X1-1", "X1-2", "X1-3"} <= refs
    assert not any(isinstance(item, ComponentItem) for item in window.canvas.scene.items())
    assert "terminal strip X1" in window.canvas._strip_caption
    for item in window.canvas.scene.items():
        if isinstance(item, StripBlockItem) and item.component_id == "X1-2":
            item.setSelected(True)
    window.canvas._delete_selected()
    app.processEvents()
    gone = {comp.id for sheet in window.session.project.sheets for comp in sheet.components}
    assert "X1-2" not in gone
    assert window.session.undo()
    window._refresh_from_session(fit=True)
    back = {comp.id for sheet in window.session.project.sheets for comp in sheet.components}
    assert "X1-2" in back
    window.close()


@pytest.mark.confirm_generic_layout
def test_cli_strip_open_and_add(tmp_path: Path):
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    path = tmp_path / "job.pcad.yaml"
    session = Session.create("CLI strip")
    session.place("iec.terminals.x_2p5", ref="X1-1", x=30, y=30)
    session.save(path)
    runner = CliRunner()
    opened = runner.invoke(app, ["electrical", "strip-open", str(path), "--strip", "X1"])
    assert opened.exit_code == 0, opened.output
    added = runner.invoke(app, ["electrical", "strip-add", str(path), "--strip", "X1"])
    assert added.exit_code == 0, added.output
    again = Session.open(path)
    refs = {comp.id for sheet in again.project.sheets for comp in sheet.components}
    assert "X1-2" in refs
    assert again.project.sheet("strip-x1").strip == "X1"


@pytest.mark.confirm_generic_layout
def test_long_strip_keeps_catalog_width_and_continues():
    session = Session.create("Long strip")
    session.place("iec.terminals.x_2p5", ref="X1-1", x=40, y=20)
    opened = session.open_terminal_strip("X1")
    page = session.project.sheet(opened["sheet"]).page
    page.size = "custom"
    page.width_mm = 90
    page.height_mm = 220
    for _ in range(19):
        session.add_terminal("X1")
    layout = session.open_terminal_strip("X1")
    terminals = [item for item in layout["items"] if item["kind"] == "terminal"]
    assert len(terminals) == 20
    assert all(abs(item["w"] - 5.2) < 0.05 for item in terminals)
    assert int(layout["parts"]) >= 2
    assert any(sheet.kind == "terminal" and sheet.strip == "X1" and int(sheet.strip_part) == 2 for sheet in session.project.sheets)
    text = dump_project(session.project)
    loaded = yaml.safe_load(text)
    first = next(sheet for sheet in loaded["sheets"] if sheet.get("kind") == "terminal" and sheet.get("strip") == "X1" and "strip_part" not in sheet)
    assert first["id"] == "strip-x1"
    continued = next(sheet for sheet in loaded["sheets"] if sheet.get("strip_part") == 2)
    assert continued["strip"] == "X1"


@pytest.mark.confirm_generic_layout
def test_end_hardware_is_on_the_panel_rail():
    session = _wired_strip()
    session.open_terminal_strip("X1")
    session.add_strip_accessory("X1", "partition", after="X1-3")
    panel = next(sheet for sheet in session.project.sheets if sheet.kind == "panel_layout")
    owned = [comp for comp in panel.components if str(comp.properties.get("strip") or "") == "X1"]
    owned.sort(key=lambda comp: comp.x)
    kinds = [str(comp.properties.get("accessory") or "terminal") for comp in owned]
    assert kinds == ["end_stop", "end_plate", "terminal", "partition", "terminal", "terminal", "end_plate", "end_stop"]
    from panelcad.core.layout import footprint_size

    for left, right in zip(owned, owned[1:]):
        width, _height = footprint_size(session.catalog.get(left.library_id), left)
        assert abs((right.x - left.x) - width) < 0.05
    assert session.project.sheet("strip-x1").components == []
    assert _qty(session, "iec.accessories.end_plate") == 2
    assert _qty(session, "iec.accessories.partition") == 1
    assert _qty(session, "iec.terminals.x_2p5") == 3


@pytest.mark.confirm_generic_layout
def test_delete_strip_block_removes_schematic_and_undoes():
    session = _wired_strip()
    session.open_terminal_strip("X1")
    removed = session.delete_strip_component("X1-2")
    assert removed["ref"] == "X1-2"
    refs = {comp.id for sheet in session.project.sheets for comp in sheet.components}
    assert "X1-2" not in refs
    assert all("X1-2" not in f"{wire.frm} {wire.to}" for sheet in session.project.sheets for wire in sheet.wires)
    assert session.undo()
    assert session.project.sheet("main").component("X1-2").library_id == "iec.terminals.x_2p5"
    assert any(comp.id == "X1-2" and sheet.kind == "panel_layout" for sheet in session.project.sheets for comp in sheet.components)


@pytest.mark.confirm_generic_layout
def test_vendor_strip_export(tmp_path: Path):
    session = _wired_strip()
    session.open_terminal_strip("X1")
    phoenix = session.export_terminal_strip(tmp_path / "phoenix.csv", "X1", "phoenix")
    wago = session.export_terminal_strip(tmp_path / "wago.csv", "X1", "smartdesigner")
    wmc = session.export_terminal_strip(tmp_path / "wmc.csv", "X1", "weidmueller")
    assert phoenix["format"] == "csv" and phoenix["delimiter"] == ","
    assert "PROJECT complete" in phoenix["product"]
    assert wago["format"] == "csv" and "smartDESIGNER" in wago["product"]
    assert wmc["format"] == "csv" and wmc["delimiter"] == ";" and wmc["header"] == "no"
    phx = (tmp_path / "phoenix.csv").read_text(encoding="utf-8").splitlines()
    assert phx[0] == "Strip,Position,Tag,Article,Internal,External,Jumper,Accessory"
    assert phx[1].startswith("X1,1,")
    assert "3022218" in phx[1]
    assert phoenix.get("unmapped", 0) == 0
    wago_text = (tmp_path / "wago.csv").read_text(encoding="utf-8")
    assert wago_text.splitlines()[0] == phx[0]
    body = (tmp_path / "wmc.csv").read_text(encoding="utf-8").splitlines()
    assert body[0].count(";") == 2
    assert "Strip," not in body[0]
    assert "X1-3" in "\n".join(body)
    with pytest.raises(ValueError):
        session.export_terminal_strip(tmp_path / "no.csv", "X1", "abb")
