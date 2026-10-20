"""Wire numbers, tags, cross-reference, terminals, I/O, and schematic/panel audit."""

from pathlib import Path

import pytest
import yaml

from panelcad import Session
from panelcad.cli.agent import Agent


def _coil_and_light(s: Session, sheet: str | None = None) -> None:
    s.place("iec.contactors.coil", ref="K1", x=30, y=30, sheet_id=sheet)
    s.place("iec.operators.pilot_light", ref="H1", x=90, y=30, sheet_id=sheet)
    s.wire("K1.A2", "H1.X1", net="24V", sheet_id=sheet)


def test_one_net_gets_one_wire_number_on_every_sheet(tmp_path: Path):
    s = Session.create("Numbers")
    _coil_and_light(s)
    s.add_sheet("control", kind="schematic")
    s.place("iec.contactors.coil", ref="K2", x=30, y=40, sheet_id="control")
    s.place("iec.operators.pilot_light", ref="H2", x=90, y=40, sheet_id="control")
    s.wire("K2.A2", "H2.X1", net="24V", sheet_id="control")
    s.wire("K2.A1", "H2.X2", net="0V", sheet_id="control")
    assert s.sheet("main").wires[0].number == "24V"
    assert s.sheet("control").wires[0].number == "24V"
    assert s.sheet("control").wires[1].number == "0V"
    s.set_wire_spec(s.sheet("control").wires[0].id, number="100", locked=True, sheet_id="control")
    result = s.number_wires()
    assert result["nets"] >= 2
    assert s.sheet("control").wires[0].number == "100"
    assert s.sheet("control").wires[0].number_locked is True
    assert s.sheet("control").wires[1].number == "0V"
    assert s.sheet("main").wires[0].caption() == "24V"
    s.set_wire_spec(s.sheet("main").wires[0].id, color="BU", gauge="1.5", cable="W1", sheet_id="main")
    assert s.sheet("main").wires[0].spec_text() == "W1 BU 1.5"
    path = tmp_path / "n.pcad.yaml"
    s.save(path)
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    wire = loaded["sheets"][0]["wires"][0]
    assert wire["number"] == "24V"
    assert wire["color"] == "BU"
    assert wire["gauge"] == "1.5"
    assert wire["cable"] == "W1"
    other = loaded["sheets"][1]["wires"][1]
    assert "color" not in other
    again = Session.open(path)
    assert again.sheet().wires[0].number == "24V"
    svg = again.export_svg(tmp_path / "n.svg").read_text(encoding="utf-8")
    assert "24V" in svg
    assert "W1 BU 1.5" in svg


def test_wire_number_conflict_is_a_validation_error():
    s = Session.create("Clash")
    _coil_and_light(s)
    s.place("iec.operators.pb_no", ref="S1", x=40, y=80)
    s.wire("S1.13", "S1.14", net="START")
    s.sheet().wires[0].number = "7"
    s.sheet().wires[1].number = "7"
    errors = s.validate()
    assert any("wire number 7" in err for err in errors)


def test_retag_keeps_a_contact_on_its_coil_and_renames_a_second_coil():
    s = Session.create("Tags")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20)
    s.add_sheet("control", kind="schematic")
    s.place("iec.contactors.aux_no", ref="K1", x=20, y=20, sheet_id="control")
    s.wire("K1.13", "K1.14", net="HOLD", sheet_id="control")
    s.add_sheet("spare", kind="schematic")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20, sheet_id="spare")
    s.wire("K1.A1", "K1.A2", net="COIL", sheet_id="spare")
    renamed = s.retag("duplicates")
    assert s.sheet("control").component("K1").library_id.endswith("aux_no")
    assert "K1.13" in {w.frm for w in s.sheet("control").wires}
    assert {row["to"] for row in renamed} == {"K2"}
    assert s.sheet("spare").wires[0].frm.startswith("K2.")
    derived = s.cross_reference_rows()
    coil = next(row for row in derived if row["sheet"] == "main" and row["child"] == "K1")
    aux = next(row for row in derived if row["sheet"] == "control")
    assert aux["parent"] == "K1"
    assert aux["state"] == "NO"
    assert aux["xref"] == coil["location"]
    assert aux["location"] in coil["xref"]
    assert "xref" not in s.sheet("control").component("K1").properties
    s.update_cross_references()
    assert s.sheet("control").component("K1").properties["xref"]


def test_child_contact_tag_cross_references_its_coil():
    s = Session.create("Child")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20)
    s.place("iec.contactors.aux_nc", ref="K1-21", x=70, y=20)
    rows = s.cross_reference_rows()
    aux = next(row for row in rows if row["child"] == "K1-21")
    assert aux["parent"] == "K1"
    assert aux["state"] == "NC"
    coil = next(row for row in rows if row["child"] == "K1" and row["role"] == "coil")
    assert aux["xref"] == coil["location"]


def test_family_retag_renumbers_in_reading_order():
    s = Session.create("Family")
    s.place("iec.breakers.mcb_1p_16a", ref="Q9", x=20, y=80)
    s.place("iec.breakers.mcb_1p_16a", ref="Q5", x=20, y=20)
    s.wire("Q5.2", "Q9.1", net="L1")
    renamed = s.retag("family")
    assert {row["to"] for row in renamed} == {"Q1", "Q2"}
    assert s.sheet().component("Q1").y == 20
    assert s.sheet().wires[0].frm == "Q1.2"
    assert s.sheet().wires[0].to == "Q2.1"


def test_terminal_strip_internal_external_jumper_and_sequence():
    s = Session.create("Strip")
    s.place("iec.terminals.x_2p5", ref="X1-2", x=40, y=60)
    s.place("iec.terminals.x_2p5", ref="X1-1", x=40, y=20)
    s.place("iec.terminals.x_2p5", ref="X1-3", x=40, y=100)
    s.wire("X1-1.A", "X1-2.A", net="INT")
    s.wire("X1-1.B", "X1-3.B", net="FIELD")
    s.number_wires(start=10, overwrite=True, scheme="sequential")
    rows = s.terminal_strip_rows()
    assert [row["terminal"] for row in rows] == ["1", "2", "3"]
    top = rows[0]
    assert top["ref"] == "X1-1"
    assert top["internal"] == "10"
    assert top["external"] == "10"
    assert rows[2]["spare"] == ""
    s.place("iec.terminals.x_2p5", ref="X1-9", x=40, y=140)
    spare = next(row for row in s.terminal_strip_rows() if row["ref"] == "X1-9")
    assert spare["spare"] == "yes"
    jumped = s.jumper_terminals(["X1-1", "X1-2"])
    assert jumped["jumper"] == "J1"
    assert s.sheet().component("X1-1").properties["jumper"] == "J1"
    ordered = s.sequence_terminal_strip("X1", rename=True)
    assert [row["ref"] for row in ordered] == ["X1-1", "X1-2", "X1-3", "X1-4"]
    assert s.sheet().component("X1-1").y == 20


@pytest.mark.confirm_generic_layout
def test_schematic_panel_audit_and_footprint_wire_note(tmp_path: Path):
    s = Session.create("Audit")
    s.place("iec.contactors.coil", ref="K1", x=30, y=30)
    s.place("iec.operators.pb_no", ref="S1", x=80, y=30)
    s.place("iec.loads.motor_3ph", ref="M1", x=140, y=30)
    issues = {(row["ref"], row["issue"]) for row in s.audit_schematic_panel()}
    assert ("K1", "missing_panel") in issues
    assert ("S1", "missing_door") in issues
    assert not any(row["ref"] == "M1" for row in s.audit_schematic_panel())
    s.add_sheet("panel", kind="panel_layout")
    s.place("iec.contactors.coil", ref="K1", x=40, y=40, sheet_id="panel")
    s.add_sheet("door", kind="door_layout")
    s.place("iec.operators.pb_no", ref="S1", x=30, y=30, sheet_id="door")
    left = {row["ref"] for row in s.audit_schematic_panel()}
    assert "K1" not in left
    assert "S1" not in left
    s.place("iec.breakers.mcb_1p_16a", ref="Q9", x=80, y=40, sheet_id="panel")
    assert any(row["ref"] == "Q9" and row["issue"] == "missing_schematic" for row in s.audit_schematic_panel())
    s.wire("K1.A1", "K1.A2", net="COIL", sheet_id="main")
    s.set_wire_spec(frm="K1.A1", to="K1.A2", number="42", color="RD", gauge="18", sheet_id="main")
    note = s.panel_wire_annotations()
    hit = next(row for row in note if row["number"] == "42")
    assert hit["from_panel"] == "panel"
    svg = s.export_svg(tmp_path / "panel.svg", sheet_id="panel").read_text(encoding="utf-8")
    assert "42 RD 18" in svg


def test_plc_io_list_import_and_item_numbers(tmp_path: Path):
    s = Session.create("IO")
    s.place("iec.plc.compact_16dio", ref="A1", x=40, y=20)
    s.place("iec.operators.pb_no", ref="S1", x=160, y=40)
    s.wire("S1.13", "A1.I0_0", net="START")
    s.number_wires(start=3, overwrite=True, scheme="sequential")
    rows = s.io_rows()
    addresses = {row["address"]: row for row in rows}
    assert "Lplus" not in addresses
    assert addresses["I0_0"]["type"] == "DI"
    assert addresses["I0_0"]["wire"] == "3"
    assert addresses["I0_0"]["device"] == "S1"
    assert addresses["Q0_0"]["type"] == "DO"
    src = tmp_path / "io.csv"
    src.write_text("module,address,description,type,wire\nA1,I0_0,Start button,DI,3\n", encoding="utf-8")
    result = s.import_io_list(src)
    assert result["updated"] == ["A1.I0_0"]
    assert result["errors"] == []
    assert s.sheet().component("A1").properties["pin_labels"]["I0_0"] == "Start button"
    count = s.assign_item_numbers()
    assert count == 2
    bom = s.export_bom(tmp_path / "bom.csv").read_text(encoding="utf-8")
    assert "A1" in bom
    s.set_wire_spec(frm="S1.13", to="A1.I0_0", cable="C12")
    cables = s.cable_rows()
    assert cables[0]["cable"] == "C12"
    assert cables[0]["count"] == "1"


def test_agent_number_wires_op():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "Agent"})["ok"]
    assert agent.handle({"id": 2, "op": "place", "library_id": "iec.contactors.coil", "ref": "K1", "x": 20, "y": 20})["ok"]
    assert agent.handle({"id": 3, "op": "place", "library_id": "iec.operators.pilot_light", "ref": "H1", "x": 80, "y": 20})["ok"]
    wired = agent.handle({"id": 4, "op": "wire", "from": "K1.A2", "to": "H1.X1", "net": "24V", "color": "BU", "gauge": "1.5"})
    assert wired["ok"], wired
    assert wired["result"]["number"] == "24V"
    numbered = agent.handle({"id": 5, "op": "number_wires"})
    assert numbered["ok"], numbered
    listed = agent.handle({"id": 6, "op": "list_wires"})
    assert listed["result"]["wires"][0]["number"] == "24V"
    assert listed["result"]["wires"][0]["color"] == "BU"
