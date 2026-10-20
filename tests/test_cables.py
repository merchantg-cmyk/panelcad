"""Field cables: propose, schedule, pull list, terminations, GUI, JSONL, and CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from PySide6.QtWidgets import QApplication
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from panelcad import Session
from panelcad.ai.drawing import drawing_agent, open_drawing
from panelcad.cli.agent import Agent
from panelcad.core.cables import (
    COLOR_UNSET,
    FIELD_CUT_BACK,
    SPARE_END,
    cable_rows,
    color_text,
    gland_text,
    library_part_for,
    pull_rows,
    role_text,
    schedule_row,
    termination_rows,
)
from panelcad.core.checks import run_checks
from panelcad.examples.field_cables import build_plc_hoa_analog_sample, build_reversing_motor_sample
from panelcad.gui.cable_dialog import CableEditorDialog
from panelcad.gui.main_window import MainWindow
from panelcad.io.yaml_format import dump_project
from panelcad.library.parts import CatalogPart
from panelcad.render.print_package import extract_pdf_text


def _place_terminal(session: Session, ref: str, y: float, *, strip: str, terminal: str) -> None:
    library = "iec.terminals.pe_4" if terminal.upper() in {"G", "PE"} else "iec.terminals.x_2p5"
    session.place(
        library,
        ref=ref,
        x=180,
        y=y,
        properties={"strip": strip, "terminal": terminal},
        snap=False,
    )


def _motor_strip(
    gauges: tuple[str | None, str | None, str | None] | None = ("#14", "#14", "#14"),
    *,
    fla: float | None = None,
    standard: str = "NEMA/ANSI",
) -> Session:
    session = Session.create("Motor strip")
    session.project.title_block.standard = standard
    session.place("nema.loads.motor_3ph", ref="M1", x=280, y=40, snap=False)
    if fla is not None:
        session.sheet().component("M1").properties["FLA"] = fla
    for index, pin in enumerate(("T1", "T2", "T3"), start=1):
        ref = f"TB1-{index}"
        _place_terminal(session, ref, 20 + index * 16, strip="TB1", terminal=str(index))
        gauge = None if gauges is None else gauges[index - 1]
        session.wire(f"{ref}.B", f"M1.{pin}", gauge=gauge)
    _place_terminal(session, "TB1-G", 90, strip="TB1", terminal="G")
    return session


def _response(tool_name: str, args: dict, call_id: str) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args, tool_call_id=call_id)])


def test_legacy_cable_rows_keep_the_marker_columns():
    session = Session.create("Legacy")
    session.place("iec.operators.pb_no", ref="S1", x=20, y=20, snap=False)
    session.place("iec.plc.compact_16dio", ref="A1", x=80, y=20, snap=False)
    session.wire("S1.13", "A1.I0_0", net="START")
    session.set_wire_spec(frm="S1.13", to="A1.I0_0", cable="C12", color="RD", gauge="18")
    rows = session.cable_rows()
    assert rows == [
        {
            "cable": "C12",
            "count": "1",
            "wire_numbers": rows[0]["wire_numbers"],
            "from": "S1.13",
            "to": "A1.I0_0",
            "color": "RD",
            "gauge": "18",
        }
    ]
    assert set(rows[0]) == {"cable", "count", "wire_numbers", "from", "to", "color", "gauge"}
    assert "cables:" not in dump_project(session.project)


def test_reversing_motor_sample_is_3c_g_14(tmp_path: Path):
    session = build_reversing_motor_sample()
    assert len(session.project.cables) == 1
    cable = session.project.cables[0]
    assert cable.tag == "W101"
    assert cable.to_kind == "motor"
    assert cable.cable_type == "3C+G #14 TC-ER"
    assert cable.conductors == 4
    assert cable.gauge == "#14"
    assert cable.shield is False
    assert cable.part_number == ""
    assert cable.part_source == ""
    assert cable.length == 120
    assert cable.slack_pct == 10
    assert cable.route == "TRAY-A"
    assert cable.gland == ""
    assert gland_text(cable) == "size per cable OD"
    assert schedule_row(cable)["gland"] == "size per cable OD"
    assert cable.from_strip == "TB1"
    assert cable.from_terminals == ["TB1-1", "TB1-2", "TB1-3", "TB1-G"]
    lands = {item.id: item for item in cable.assignments}
    assert lands["1"].panel_terminal == "TB1-1"
    assert lands["1"].field_terminal.endswith(".T1")
    assert lands["G"].panel_terminal == "TB1-G"
    assert lands["G"].field_terminal.endswith(".PE")
    assert lands["G"].color == ""
    assert role_text(lands["G"]) == "GND"
    assert all(item.color == "" for item in cable.assignments)
    assert all(color_text(item) == COLOR_UNSET for item in cable.assignments)
    assert "SH" not in lands
    marked = [wire for wire in session.sheet("main").wires if wire.cable == "W101"]
    assert len(marked) == 3
    svg = session.export_svg(tmp_path / "rev-cables.svg", sheet_id="main").read_text(encoding="utf-8")
    assert "W101" in svg
    pulls = pull_rows(session.project)
    assert pulls[0]["cable_type"] == "3C+G #14 TC-ER"
    assert pulls[0]["pull_length"] == "132"
    assert pulls[0]["run_length"] == "120"
    terms = termination_rows(session.project)
    phase = next(row for row in terms if row["conductor"] == "1")
    assert phase["panel_terminal"] == "TB1-1"
    assert ".T1" in phase["field_terminal"]
    ground = next(row for row in terms if row["conductor"] == "G")
    assert ground["panel_terminal"] == "TB1-G"
    assert ground["field_terminal"].endswith(".PE")
    assert ground["color"] == COLOR_UNSET
    assert ground["role"] == "GND"
    assert phase["color"] == COLOR_UNSET


def test_plc_hoa_analog_sample_splits_vfd_and_instrument(tmp_path: Path):
    session = build_plc_hoa_analog_sample()
    by_kind = {cable.to_kind: cable for cable in session.project.cables}
    assert set(by_kind) == {"motor", "instrument"}
    motor = by_kind["motor"]
    analog = by_kind["instrument"]
    assert motor.tag == "W101"
    assert motor.from_device == "VFD1"
    assert motor.from_strip == ""
    assert motor.from_terminals == ["VFD1.U", "VFD1.V", "VFD1.W"]
    assert motor.cable_type == "3C+G #12 VFD shielded"
    assert motor.conductors == 4
    assert motor.gauge == "#12"
    assert motor.shield and motor.drain
    assert motor.shield_bond == "both"
    assert "520-um001" in motor.shield_source.lower()
    assert motor.gland == ""
    assert gland_text(motor) == "size per cable OD"
    assert motor.length == 75 and motor.slack_pct == 10
    assert motor.route == "TRAY-A"
    assert motor.part_number == ""
    lands = {item.id: item for item in motor.assignments}
    assert lands["1"].panel_terminal == "VFD1.U"
    assert lands["1"].field_terminal.endswith(".T1")
    assert lands["G"].panel_terminal == "VFD1 PE"
    assert lands["G"].field_terminal == "MTR1 frame"
    assert lands["SH"].panel_terminal == "VFD1 PE / EMC shield clamp"
    assert lands["SH"].field_terminal == "MTR1 frame"
    assert not any(item.spare for item in motor.assignments)
    assert all(item.color == "" for item in motor.assignments)
    assert all(item.color == "" for item in analog.assignments)
    assert analog.tag == "W102"
    assert analog.to_device == "TT1"
    assert analog.from_strip == "TB2"
    assert "TB2-G" in analog.from_terminals
    assert analog.cable_type == "2PR #18 shielded instrument"
    assert analog.conductors == 4
    assert analog.gauge == "#18"
    assert analog.pairs == 2
    assert analog.length == 40
    assert analog.shield_bond == "panel"
    assert "1756-in063" in analog.shield_source.lower()
    assert analog.gland == ""
    spare = [item for item in analog.assignments if item.spare]
    assert [item.id for item in spare] == ["2A", "2B"]
    assert all(item.panel_terminal == SPARE_END and item.field_terminal == SPARE_END for item in spare)
    assert all("SP" not in f"{item.panel_terminal}{item.field_terminal}" for item in analog.assignments)
    shield = next(item for item in analog.assignments if item.id == "SH")
    assert shield.panel_terminal == "TB2-G"
    assert shield.field_terminal == FIELD_CUT_BACK
    quiet = {item.check for item in run_checks(session.project, session.catalog)}
    assert "shield_ungrounded" not in quiet
    assert "instrument_shield_both_ends" not in quiet
    pdf = session.export_cable_schedule_pdf(tmp_path / "plc.pdf")
    text = extract_pdf_text(pdf)
    assert "3C+G #12 VFD shielded" in text
    assert "4C #12" not in text
    assert "size per cable OD" in text
    assert "520-UM001" in text
    assert "1756-IN063" in text
    assert SPARE_END in text
    assert FIELD_CUT_BACK in text
    assert "VFD1.U" in text
    assert COLOR_UNSET in text
    assert "GND" in text
    assert "BLK" not in text
    assert "GRN" not in text
    assert "DRAIN" not in text
    kinds = {cable.to_device for cable in session.project.cables}
    assert "TT1" in kinds
    hoa = [
        comp.id
        for sheet in session.project.sheets
        for comp in sheet.components
        if comp.properties.get("role") == "hoa" or "selector" in comp.library_id
    ]
    assert hoa
    assert not any(cable.to_device in hoa for cable in session.project.cables)
    pulls = {row["cable_type"]: row for row in pull_rows(session.project)}
    assert pulls["3C+G #12 VFD shielded"]["pull_length"] == "82.5"
    assert pulls["2PR #18 shielded instrument"]["pull_length"] == "44"


def test_iec_tags_and_prefix_override():
    session = _motor_strip()
    session.project.title_block.standard = "IEC"
    session.propose_cables()
    cable = session.project.cables[0]
    assert cable.tag == "-W1"
    assert cable.length_unit == "m"
    assert cable.cable_type == "3C+G #14 TC-ER"
    session.delete_cable(cable.tag)
    session.project.title_block.standard = "NEMA/ANSI"
    session.project.ref_prefixes["cable"] = "CW"
    session.propose_cables()
    assert session.project.cables[0].tag == "CW101"


def test_spares_gauge_and_devices_that_are_not_field_cables():
    session = _motor_strip(("#14", "#12", "#18"))
    session.propose_cables()
    assert session.project.cables[0].gauge == "#12"
    assert session.project.cables[0].cable_type == "3C+G #12 TC-ER"

    fla = _motor_strip(None, fla=21)
    fla.propose_cables()
    assert fla.project.cables[0].gauge == "#12"
    assert fla.project.cables[0].cable_type == "3C+G #12 TC-ER"

    iec = _motor_strip(None, fla=21, standard="IEC")
    iec.propose_cables()
    assert iec.project.cables[0].gauge == "4 mm²"
    assert iec.project.cables[0].tag == "-W1"

    blank = Session.create("Sensor")
    blank.place("nema.operators.limit_no", ref="LS1", x=240, y=40, snap=False)
    _place_terminal(blank, "TB1-1", 40, strip="TB1", terminal="1")
    blank.wire("TB1-1.B", "LS1.3")
    blank.propose_cables()
    sensor = blank.project.cables[0]
    assert sensor.to_kind == "sensor"
    assert sensor.gauge == ""
    assert sensor.cable_type == "2C TC-ER"
    assert sensor.part_number == ""

    with pytest.raises(ValueError):
        blank.propose_cables(spares=-1)

    door = Session.create("Door")
    door.place("nema.operators.pb_no", ref="PB1", x=240, y=40, snap=False)
    _place_terminal(door, "TB1-1", 40, strip="TB1", terminal="1")
    door.wire("TB1-1.B", "PB1.4")
    assert door.propose_cables()["count"] == 0

    station = Session.create("Station")
    station.place("nema.operators.pb_no", ref="PBS1", x=240, y=40, snap=False)
    station.sheet().component("PBS1").properties.update({"location": "field", "station": "PBS-1"})
    _place_terminal(station, "TB1-1", 40, strip="TB1", terminal="1")
    _place_terminal(station, "TB1-2", 56, strip="TB1", terminal="2")
    station.wire("TB1-1.B", "PBS1.3")
    station.wire("TB1-2.B", "PBS1.4")
    station.propose_cables()
    remote = station.project.cables[0]
    assert remote.to_kind == "pushbutton"
    assert remote.conductors == 3
    assert remote.cable_type.startswith("3C ")

    direct = Session.create("Direct")
    direct.place("nema.contactors.ac3_32a", ref="M", x=40, y=40, snap=False)
    direct.place("nema.loads.motor_3ph", ref="MTR", x=160, y=40, snap=False)
    direct.wire("M.T1", "MTR.T1")
    assert direct.propose_cables()["count"] == 0


def test_instrument_spare_override_and_pull_totals():
    session = build_plc_hoa_analog_sample()
    session.propose_cables(spares=0, replace=True)
    analog = next(cable for cable in session.project.cables if cable.to_kind == "instrument")
    assert analog.cable_type == "1PR #18 shielded instrument"
    assert analog.conductors == 2
    assert not any(item.spare for item in analog.assignments)

    session.add_cable(
        to_device="M2",
        to_kind="motor",
        cable_type="3C+G #14 TC-ER",
        gauge="#14",
        conductors=4,
        length=10,
        slack_pct=0,
        length_unit="ft",
    )
    session.add_cable(
        to_device="M3",
        to_kind="motor",
        cable_type="3C+G #14 TC-ER",
        gauge="#14",
        conductors=4,
        length=20,
        slack_pct=0,
        length_unit="ft",
    )
    grouped = next(row for row in pull_rows(session.project) if row["cable_type"] == "3C+G #14 TC-ER")
    assert grouped["cables"] == "2"
    assert grouped["pull_length"] == "30"
    assert grouped["run_length"] == "30"


def test_library_part_requires_one_cable_match_with_a_url():
    description = "3C+G #14 TC-ER"
    part = CatalogPart(
        manufacturer="Shop",
        catalog="TCER-3C14",
        library_id="cable.generic",
        category="cable.power",
        description=description,
        verified="partial",
        source="https://example.com/cables/tcer-3c14",
    )
    session = _motor_strip()
    session.parts.parts[part.key()] = part
    session.propose_cables()
    cable = session.project.cables[0]
    assert cable.part_number == "TCER-3C14"
    assert cable.part_source == "https://example.com/cables/tcer-3c14"

    twin = part.model_copy(update={"catalog": "TCER-3C14-B"})
    session.parts.parts[twin.key()] = twin
    session.delete_cable(cable.tag)
    session.propose_cables()
    assert session.project.cables[0].part_number == ""

    other = CatalogPart(
        manufacturer="Shop",
        catalog="BLOCK",
        library_id="terminal.block",
        category="terminal.block",
        description=description,
        verified="partial",
        source="https://example.com/blocks",
    )

    bare = type("Bare", (), {})()
    bare.category = "cable.power"
    bare.source = "notes/local.pdf"
    bare.description = description
    bare.catalog = "LOCAL"

    assert library_part_for(type("P", (), {"parts": {"a": bare, "b": other}})(), description) == ("", "")


def test_yaml_roundtrip_lengths_and_schematic_tag_toggle(tmp_path: Path):
    empty = dump_project(Session.create("Empty").project)
    assert "cables:" not in empty
    session = build_reversing_motor_sample()
    text = dump_project(session.project)
    assert "cable_type: '3C+G #14 TC-ER'" in text or 'cable_type: "3C+G #14 TC-ER"' in text
    assert "length: 120" in text
    assert "slack_pct: 10" in text
    assert "show_tag" not in text
    assert "part_number" not in text
    assert "gland:" not in text
    assert "1/2 in NPT" not in text
    assert "3/4 in NPT" not in text
    assert "BLK" not in text
    assert "GRN" not in text
    path = tmp_path / "rev.pcad.yaml"
    session.save(path)
    loaded = Session.open(path)
    assert loaded.project.cables[0].cable_type == "3C+G #14 TC-ER"
    assert loaded.project.cables[0].length == 120
    assert loaded.project.cables[0].assignments[0].panel_terminal == "TB1-1"

    loaded.propose_cables(show_tags=False, replace=True)
    assert loaded.project.cables[0].show_tag is False
    assert all(not wire.cable for sheet in loaded.project.sheets for wire in sheet.wires)
    hidden = loaded.export_svg(tmp_path / "hidden.svg", sheet_id="main").read_text(encoding="utf-8")
    assert "W101" not in hidden

    session.sheet("main").wires[0].cable = "C12"
    rows = cable_rows(session.project)
    assert any(row["cable"] == "C12" and row["count"] == "1" and row["cable_type"] == "" for row in rows)
    assert any(row["tag"] == "W101" and row["cable_type"] == "3C+G #14 TC-ER" for row in rows)


def test_undo_propose_and_assign_keeps_wire_identity():
    session = _motor_strip()
    session.propose_cables()
    assert session.project.cables
    assert session.undo()
    assert session.project.cables == []
    assert all(not wire.cable for wire in session.sheet().wires)
    session.propose_cables()
    cable = session.project.cables[0]
    first = cable.assignments[0]
    session.assign_cable_conductors(
        cable.tag,
        [
            {
                "id": first.id,
                "color": "ORG",
                "panel_terminal": first.panel_terminal,
                "field_terminal": first.field_terminal,
                "sheet": first.sheet,
                "wire_id": first.wire_id,
                "wire_number": first.wire_number,
                "role": first.role,
            }
        ],
    )
    saved = session.project.cables[0].assignments[0]
    assert saved.color == "ORG"
    assert saved.wire_id == first.wire_id
    marked = next(wire for wire in session.sheet().wires if wire.id == first.wire_id)
    assert marked.cable == cable.tag


def test_jsonl_ops_do_not_use_the_request_id_as_a_tag(tmp_path: Path):
    session = _motor_strip()
    agent = Agent()
    agent.session = session
    proposed = agent.handle({"id": "REQ-1", "op": "propose_cables"})
    assert proposed["ok"], proposed
    assert proposed["id"] == "REQ-1"
    assert proposed["result"]["cables"][0]["tag"] == "W101"
    assert proposed["result"]["cables"][0]["part_number"] == ""
    bonded = agent.handle({"id": "REQ-2", "op": "update_cable", "tag": "W101", "shield_bond": "both"})
    assert bonded["ok"], bonded
    assert bonded["result"]["shield_bond"] == "both"
    assert "520-um001" in bonded["result"]["shield_source"].lower()
    shield = next(item for item in bonded["result"]["assignments"] if item["id"] == "SH")
    assert shield["panel_terminal"] == "TB1-G"
    assert shield["field_terminal"].endswith("frame")
    kept = agent.handle({"id": "REQ-3", "op": "update_cable", "tag": "W101", "length": 40})
    assert kept["ok"], kept
    assert kept["result"]["length"] == 40
    assert next(item for item in kept["result"]["assignments"] if item["id"] == "SH")["panel_terminal"] == "TB1-G"
    added = agent.handle(
        {
            "id": "W999",
            "op": "add_cable",
            "to_device": "XV1",
            "to_kind": "valve",
            "cable_type": "2C #16 TC-ER",
            "conductors": 2,
            "gauge": "#16",
            "length": 15,
            "slack_pct": 10,
            "route": "TRAY-B",
        }
    )
    assert added["ok"], added
    assert added["result"]["tag"] == "W102"
    assert added["result"]["tag"] != "W999"
    edited = agent.handle({"id": 3, "op": "update_cable", "tag": "W102", "length": 25, "gland": "1/2 in NPT"})
    assert edited["ok"], edited
    assert edited["result"]["length"] == 25
    assigned = agent.handle(
        {
            "id": 4,
            "op": "assign_cable_conductors",
            "tag": "W102",
            "conductors": [
                {
                    "id": "1",
                    "color": "BLK",
                    "panel_terminal": "TB9-1",
                    "field_terminal": "XV1.1",
                    "sheet": "main",
                    "wire_id": "w-xv",
                    "role": "signal",
                }
            ],
        }
    )
    assert assigned["ok"], assigned
    assert assigned["result"]["assignments"][0]["panel_terminal"] == "TB9-1"
    schedule = tmp_path / "cables.csv"
    pdf = tmp_path / "cables.pdf"
    pull = tmp_path / "pull.csv"
    terms = tmp_path / "terms.csv"
    exported = agent.handle(
        {
            "id": 5,
            "op": "export_cable_schedule",
            "path": str(pdf),
            "pull": str(pull),
            "terminations": str(terms),
        }
    )
    assert exported["ok"], exported
    assert pdf.is_file() and pull.is_file() and terms.is_file()
    listed = agent.handle({"id": 6, "op": "cable_schedule", "path": str(schedule)})
    assert listed["ok"], listed
    assert schedule.is_file()
    assert any(row["tag"] == "W101" for row in listed["result"]["rows"])
    text = extract_pdf_text(pdf)
    assert "W101" in text
    assert "3C+G #14 TC-ER" in text


def test_drawing_agent_tools_propose_add_and_export(tmp_path: Path):
    session = _motor_strip()
    src = tmp_path / "job.pcad.yaml"
    session.save(src)
    seen: list[str] = []
    steps = [
        _response("propose_cables", {"spares": 0}, "propose"),
        _response(
            "add_cable",
            {
                "to_device": "XV1",
                "cable_type": "2C #16 TC-ER",
                "length": 12,
                "show_tag": True,
                "from_device": "PLC1",
                "shield_bond": "panel",
            },
            "add",
        ),
        _response(
            "export_cable_schedule",
            {
                "path": str(tmp_path / "schedule.pdf"),
                "pull": str(tmp_path / "pull.csv"),
                "terminations": str(tmp_path / "terms.csv"),
            },
            "export",
        ),
        ModelResponse(parts=[TextPart(content="Cables proposed and exported.")]),
    ]

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(tool.name for tool in info.function_tools)
        return steps.pop(0)

    drawing = open_drawing(src)
    with drawing_agent.override(model=FunctionModel(model)):
        result = drawing_agent.run_sync("Propose the field cables and export the schedule.", deps=drawing)
    assert "exported" in result.output
    assert {"propose_cables", "add_cable", "export_cable_schedule"} <= set(seen)
    opened = drawing.operator.session
    assert opened is not None
    tags = {cable.tag for cable in opened.project.cables}
    assert "W101" in tags
    added = next(cable for cable in opened.project.cables if cable.to_device == "XV1")
    assert added.from_device == "PLC1"
    assert added.shield_bond == "panel"
    assert any(item.id == "SH" and item.field_terminal == FIELD_CUT_BACK for item in added.assignments)
    assert (tmp_path / "schedule.pdf").is_file()
    assert "3C+G #14 TC-ER" in (tmp_path / "pull.csv").read_text(encoding="utf-8")
    assert "TB1-1" in (tmp_path / "terms.csv").read_text(encoding="utf-8")


def test_cli_electrical_cables(tmp_path: Path):
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    session = _motor_strip()
    path = tmp_path / "job.pcad.yaml"
    session.save(path)
    out = tmp_path / "cables.csv"
    pdf = tmp_path / "cables.pdf"
    pull = tmp_path / "pull.csv"
    terms = tmp_path / "terms.csv"
    result = CliRunner().invoke(
        app,
        [
            "electrical",
            "cables",
            str(path),
            "--propose",
            "--out",
            str(out),
            "--pdf",
            str(pdf),
            "--pull",
            str(pull),
            "--terminations",
            str(terms),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "proposed 1" in result.output
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["cables"][0]["tag"] == "W101"
    assert saved["cables"][0]["cable_type"] == "3C+G #14 TC-ER"
    assert "gland" not in saved["cables"][0]
    assert "3C+G #14 TC-ER" in out.read_text(encoding="utf-8")
    assert "size per cable OD" in out.read_text(encoding="utf-8")
    assert COLOR_UNSET in terms.read_text(encoding="utf-8")
    assert "GND" in terms.read_text(encoding="utf-8")
    pull_text = pull.read_text(encoding="utf-8")
    assert "3C+G #14 TC-ER" in pull_text
    assert "TB1-1" in terms.read_text(encoding="utf-8")
    assert "W101" in extract_pdf_text(pdf)
    assert "size per cable OD" in extract_pdf_text(pdf)
    again = CliRunner().invoke(app, ["electrical", "cables", str(path)])
    assert again.exit_code == 0, again.output
    assert "W101" in again.output
    assert "3C+G #14 TC-ER" in again.output


def test_gui_editor_creates_edits_and_assigns():
    app = QApplication.instance() or QApplication([])
    session = build_reversing_motor_sample()
    window = MainWindow(session)
    window.show()
    app.processEvents()
    reports: list[str] = []
    for action in window.menuBar().actions():
        menu = action.menu()
        if menu is None or menu.title().replace("&", "") != "Reports":
            continue
        reports = [item.text().replace("&", "") for item in menu.actions() if item.text()]
    assert "Field cables…" in reports
    assert "Export cable schedule…" in reports
    assert "Export cable pull list…" in reports
    assert "Export cable terminations…" in reports
    window.close()

    dialog = CableEditorDialog(session)
    dialog.resize(1280, 820)
    dialog.show()
    app.processEvents()
    assert dialog.table.rowCount() == 1
    assert dialog.spare_count.specialValueText() == "default"
    assert dialog.spare_count.minimumWidth() >= 110
    dialog.select_tag("W101")
    app.processEvents()
    assert dialog.conductors_table.rowCount() >= 4
    dialog.set_length(90)
    dialog.set_slack(5)
    dialog.route.setText("TRAY-B")
    dialog.apply_form()
    cable = session.project.cables[0]
    assert cable.length == 90
    assert cable.slack_pct == 5
    assert cable.route == "TRAY-B"
    assert cable.gland == ""
    assert dialog.gland.placeholderText() == "blank — size per cable OD"
    dialog.shield_bond.setCurrentText("both")
    dialog.apply_form()
    cable = session.project.cables[0]
    assert cable.length == 90
    assert cable.shield_bond == "both"
    assert cable.gland == ""
    shield = next(item for item in cable.assignments if item.id == "SH")
    assert shield.panel_terminal == "TB1-G"
    assert shield.field_terminal.endswith("frame")
    assert cable.assignments[0].wire_id
    dialog.set_conductor_color(0, "ORG")
    dialog.apply_conductors()
    assert session.project.cables[0].assignments[0].color == "ORG"
    assert session.project.cables[0].assignments[0].wire_id
    before = len(session.project.cables)
    dialog.add_cable()
    assert len(session.project.cables) == before + 1
    added = session.project.cables[-1].tag
    dialog.select_tag(added)
    dialog.delete_selected()
    assert all(item.tag != added for item in session.project.cables)
    shot = dialog.grab()
    assert not shot.isNull()
    screen = dialog.screen() or app.primaryScreen()
    avail = screen.availableGeometry().width() if screen is not None else shot.width()
    # 900px when the screen allows it. A smaller offscreen screen clamps the dialog.
    assert shot.width() >= min(900, max(1, avail - 16))
    assert shot.width() <= avail + 8
    dialog.close()


def test_vfd_output_lands_on_the_drive_and_a_strip_still_wins():
    session = Session.create("VFD direct")
    session.place("nema.drives.vfd_1k5", ref="VFD1", x=40, y=40, snap=False)
    session.place("nema.loads.motor_3ph", ref="MTR1", x=220, y=40, snap=False)
    session.wire("VFD1.U", "MTR1.T1", gauge="#12")
    session.wire("VFD1.V", "MTR1.T2", gauge="#12")
    session.wire("VFD1.W", "MTR1.T3", gauge="#12")
    session.wire("VFD1.L1", "MTR1.T1", gauge="#12")
    session.wire("VFD1.DI1", "MTR1.T2")
    session.propose_cables()
    assert len(session.project.cables) == 1
    cable = session.project.cables[0]
    assert cable.from_device == "VFD1"
    assert cable.from_strip == ""
    assert cable.from_terminals == ["VFD1.U", "VFD1.V", "VFD1.W"]
    assert cable.cable_type == "3C+G #12 VFD shielded"
    assert cable.conductors == 4
    assert cable.gland == ""
    assert [item.wire_id for item in cable.assignments if item.role == "phase"]
    assert all(item.panel_terminal.startswith("VFD1.") for item in cable.assignments if item.role == "phase")

    session.place("iec.terminals.x_2p5", ref="TB1-1", x=140, y=20, properties={"strip": "TB1", "terminal": "1"}, snap=False)
    session.place("iec.terminals.x_2p5", ref="TB1-2", x=140, y=36, properties={"strip": "TB1", "terminal": "2"}, snap=False)
    session.place("iec.terminals.x_2p5", ref="TB1-3", x=140, y=52, properties={"strip": "TB1", "terminal": "3"}, snap=False)
    session.place("iec.terminals.pe_4", ref="TB1-G", x=140, y=68, properties={"strip": "TB1", "terminal": "G"}, snap=False)
    session.wire("VFD1.U", "TB1-1.A", gauge="#12")
    session.wire("TB1-1.B", "MTR1.T1", gauge="#12")
    session.wire("VFD1.V", "TB1-2.A", gauge="#12")
    session.wire("TB1-2.B", "MTR1.T2", gauge="#12")
    session.wire("VFD1.W", "TB1-3.A", gauge="#12")
    session.wire("TB1-3.B", "MTR1.T3", gauge="#12")
    session.propose_cables(replace=True)
    landed = session.project.cables[0]
    assert landed.from_strip == "TB1"
    assert landed.from_device == ""
    assert landed.cable_type == "3C+G #12 VFD shielded"
    assert landed.shield_bond == "both"
    shield = next(item for item in landed.assignments if item.id == "SH")
    assert shield.panel_terminal == "TB1-G"
    assert shield.field_terminal == "MTR1 frame"
    assert "VFD1.U" not in landed.from_terminals
    assert "TB1-1" in landed.from_terminals


def test_library_od_is_shown_and_not_turned_into_a_gland():
    part = CatalogPart(
        manufacturer="Shop",
        catalog="TCER-3C14",
        library_id="cable.generic",
        category="cable.power",
        description="3C+G #14 TC-ER",
        notes="OD 11.4 mm",
        verified="partial",
        source="https://example.com/cables/tcer-3c14",
    )
    session = _motor_strip()
    session.parts.parts[part.key()] = part
    session.propose_cables()
    cable = session.project.cables[0]
    assert cable.part_number == "TCER-3C14"
    assert cable.od == "11.4 mm"
    assert cable.od_source == part.source
    assert cable.gland == ""
    assert schedule_row(cable)["gland"] == "OD 11.4 mm"
    session.update_cable(cable.tag, gland="1/2 in NPT")
    assert schedule_row(session.project.cables[0])["gland"] == "1/2 in NPT"
    colored = part.model_copy(update={"notes": "OD 11.4 mm\ncolors: 1=BLK, 2=RED, 3=BLU, G=GRN"})
    session.parts.parts.clear()
    session.parts.parts[colored.key()] = colored
    session.delete_cable(session.project.cables[0].tag)
    session.sheet().wires[0].color = "ORG"
    session.propose_cables()
    painted = session.project.cables[0]
    by_id = {item.id: item for item in painted.assignments}
    assert by_id["1"].color == "ORG"
    assert by_id["2"].color == "RED"
    assert by_id["3"].color == "BLU"
    assert by_id["G"].color == "GRN"
    assert color_text(by_id["G"]) == "GRN"
    assert role_text(by_id["G"]) == "GND"


def test_shield_checks_flag_an_open_drain_and_a_grounded_instrument_pair():
    session = build_plc_hoa_analog_sample()
    motor = next(cable for cable in session.project.cables if cable.to_kind == "motor")
    analog = next(cable for cable in session.project.cables if cable.to_kind == "instrument")
    session.update_cable(motor.tag, length=80)
    assert next(item for item in motor.assignments if item.id == "SH").panel_terminal.startswith("VFD1 PE")
    session.update_cable(motor.tag, shield_bond="panel")
    landed = next(item for item in session.project.cables if item.tag == motor.tag)
    shield = next(item for item in landed.assignments if item.id == "SH")
    assert shield.field_terminal == FIELD_CUT_BACK
    assert landed.length == 80
    session.update_cable(motor.tag, length=81)
    shield = next(item for item in session.project.cables if item.tag == motor.tag)
    assert next(item for item in shield.assignments if item.id == "SH").field_terminal == FIELD_CUT_BACK

    for item in session.project.cables:
        if item.tag != motor.tag:
            continue
        for land in item.assignments:
            if land.id == "SH":
                land.panel_terminal = ""
                land.field_terminal = ""
    open_ids = {item.check for item in run_checks(session.project, session.catalog)}
    assert "shield_ungrounded" in open_ids

    for land in analog.assignments:
        if land.id == "SH":
            land.panel_terminal = "TB2-G"
            land.field_terminal = "TT1 frame"
    both = {item.check for item in run_checks(session.project, session.catalog)}
    assert "instrument_shield_both_ends" in both

    plain = Session.create("Plain shield")
    plain.add_cable(to_device="XT1", to_kind="sensor", shield=True, drain=True, cable_type="2C shielded")
    flagged = {item.check for item in run_checks(plain.project, plain.catalog)}
    assert "shield_ungrounded" in flagged
    assert plain.project.cables[0].shield_bond == ""
    assert plain.project.cables[0].gland == ""
