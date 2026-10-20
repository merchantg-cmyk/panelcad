"""NEMA/ANSI tags use JIC/NFPA 79 prefixes; IEC and saved files stay put."""

from pathlib import Path

import pytest
import yaml

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.core.refs import resolve_ref_prefix
from panelcad.io.yaml_format import load_project


OLD_NEMA_YAML = """
format: panelcad
version: "0.1"
name: Saved NEMA
title_block:
  standard: NEMA/ANSI
sheets:
  - id: main
    title: Power
    kind: schematic
    components:
      - id: Q1
        library_id: iec.breakers.mcb_3p_32a
        x: 40
        y: 22
      - id: K1
        library_id: iec.contactors.ac3_9a
        x: 38
        y: 82
      - id: S1
        library_id: iec.operators.pb_no
        x: 120
        y: 30
    wires:
      - id: w1
        from: Q1.2
        to: K1.L1
        net: L1
"""


def test_new_nema_project_uses_jic_prefixes():
    session = Session.create("New NEMA")
    assert session.project.title_block.standard == "NEMA/ANSI"
    assert session.place("iec.breakers.mcb_3p_32a").id == "CB1"
    assert session.place("iec.protection.fuse_10a").id == "FU1"
    assert session.place("iec.protection.disconnect_3p").id == "DISC1"
    assert session.place("iec.contactors.ac3_9a").id == "M1"
    assert session.place("iec.protection.overload_9a").id == "OL1"
    assert session.place("iec.breakers.mpcb_3p").id == "MCP1"
    assert session.place("iec.loads.motor_3ph").id == "MTR1"
    assert session.place("iec.drives.vfd_1k5").id == "VFD1"
    assert session.place("iec.operators.pb_no").id == "PB1"
    assert session.place("iec.operators.selector_3pos").id == "SEL1"
    assert session.place("iec.operators.pilot_light").id == "PL1"
    assert session.place("iec.relays.k_4co_24vdc").id == "CR1"
    assert session.place("iec.timers.on_delay").id == "TR1"
    assert session.place("iec.power.control_transformer").id == "T1"
    assert session.place("iec.power.psu_24v_5a").id == "PS1"
    assert session.place("iec.terminals.x_2p5").id == "TB1"
    assert session.place("iec.plc.compact_16dio").id == "PLC1"
    assert session.place("iec.operators.limit_no").id == "LS1"
    assert session.place("nema.operators.estop").id == "ES1"
    info = session.inspect_device("iec.breakers.mcb_3p_32a")
    assert info["ref_prefix"] == "CB"
    assert info["library_ref_prefix"] == "Q"


@pytest.mark.confirm_generic_layout
def test_nema_circuit_builder_dol_uses_us_tags():
    session = Session.create("NEMA DOL tags")
    result = session.insert_circuit(kind="dol")
    by_role = {c["role"]: c["id"] for c in result["components"]}
    assert by_role["breaker"] == "CB1"
    assert by_role["contactor"] == "M1"
    assert by_role["overload"] == "OL1"
    assert by_role["motor"] == "MTR1"
    wires = {(w["from"], w["to"], w["net"]) for w in result["wires"]}
    assert ("CB1.2", "M1.L1", "L1") in wires
    assert ("OL1.2", "MTR1.T1", "T1") in wires
    panel = next(sheet for sheet in session.project.sheets if sheet.kind == "panel_layout")
    assert {c.id for c in panel.components} >= {"CB1", "M1", "OL1"}
    assert "MTR1" not in {c.id for c in panel.components}


def test_iec_project_keeps_iec_81346_prefixes():
    session = Session.create("IEC tags")
    session.set_title_block(standard="IEC")
    assert session.place("iec.breakers.mcb_3p_32a").id == "Q1"
    assert session.place("iec.contactors.ac3_9a").id == "K1"
    assert session.place("iec.protection.overload_9a").id == "F1"
    assert session.place("iec.loads.motor_3ph").id == "M1"
    assert session.place("iec.operators.pb_no").id == "S1"
    assert session.place("iec.drives.vfd_1k5").id == "U1"
    assert session.inspect_device("iec.breakers.mcb_3p_32a")["ref_prefix"] == "Q"
    result = session.insert_circuit(kind="dol", x=200, y=22)
    by_role = {c["role"]: c["id"] for c in result["components"]}
    assert by_role["breaker"] == "Q2"
    assert by_role["contactor"] == "K2"
    assert by_role["motor"] == "M2"


def test_old_project_file_loads_unchanged(tmp_path: Path):
    path = tmp_path / "saved.pcad.yaml"
    path.write_text(OLD_NEMA_YAML, encoding="utf-8")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    session = Session.open(path)
    ids = [c.id for c in session.sheet().components]
    assert ids == ["Q1", "K1", "S1"]
    assert session.sheet().wires[0].frm == "Q1.2"
    assert session.project.title_block.standard == "NEMA/ANSI"
    loaded = load_project(path)
    assert [c.id for c in loaded.sheets[0].components] == ["Q1", "K1", "S1"]
    assert raw["sheets"][0]["components"][0]["id"] == "Q1"
    session.save(path)
    again = path.read_text(encoding="utf-8")
    assert "id: Q1" in again
    assert "id: K1" in again
    assert "id: S1" in again
    assert "from: Q1.2" in again
    assert "ref_prefixes" not in again


def test_retag_standard_applies_nema_prefixes_and_undoes():
    session = Session.create("Retag NEMA")
    session.place("iec.breakers.mcb_3p_32a", ref="Q1", x=20, y=20)
    session.place("iec.operators.pb_no", ref="S1", x=80, y=20)
    session.wire("S1.13", "Q1.1")
    unchanged = session.retag("family")
    assert session.sheet().component("Q1")
    assert session.sheet().component("S1")
    assert all(row["to"] not in {"CB1", "PB1"} for row in unchanged)
    renamed = session.retag("standard")
    assert {row["from"]: row["to"] for row in renamed} == {"Q1": "CB1", "S1": "PB1"}
    assert session.sheet().wires[0].frm == "PB1.13"
    assert session.can_undo()
    assert session.undo()
    assert {c.id for c in session.sheet().components} == {"Q1", "S1"}
    assert session.sheet().wires[0].frm == "S1.13"


def test_project_ref_prefixes_override_builtin_map():
    session = Session.create("Shop prefix")
    session.set_title_block(ref_prefixes={"protection.breaker": "MCB", "operator.selector": "SS"})
    assert session.place("iec.breakers.mcb_3p_32a").id == "MCB1"
    assert session.place("iec.operators.selector_3pos").id == "SS1"
    assert session.place("iec.operators.pb_no").id == "PB1"


def test_sheet_iec_override_keeps_iec_prefixes():
    session = Session.create("Mixed")
    assert session.project.title_block.standard == "NEMA/ANSI"
    session.sheet().standard = "IEC"
    assert session.place("iec.breakers.mcb_3p_32a").id == "Q1"
    assert session.ref_prefix_for("iec.operators.pb_no") == "S"


def test_agent_place_and_retag_agree():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "Agent tags"})["ok"]
    placed = agent.handle({"id": 2, "op": "place", "library_id": "iec.breakers.mcb_3p_32a"})
    assert placed["ok"], placed
    assert placed["result"]["id"] == "CB1"
    agent.handle({"id": 3, "op": "place", "library_id": "iec.breakers.mcb_1p_16a", "ref": "Q9"})
    retagged = agent.handle({"id": 4, "op": "retag", "mode": "standard"})
    assert retagged["ok"], retagged
    dests = {row["to"] for row in retagged["result"]["renamed"]}
    assert "CB2" in dests
    undone = agent.handle({"id": 5, "op": "undo"})
    assert undone["ok"] and undone["result"]["undone"] is True


def test_catalog_map_covers_pressure_switch_category():
    session = Session.create("PRS")
    device = session.catalog.get("nema.operators.limit_no")
    assert (
        resolve_ref_prefix(device, standard="NEMA/ANSI", maps=session.catalog.ref_prefix_maps)
        == "LS"
    )
    device.category = "operator.pressure"
    assert (
        resolve_ref_prefix(device, standard="NEMA/ANSI", maps=session.catalog.ref_prefix_maps)
        == "PRS"
    )
