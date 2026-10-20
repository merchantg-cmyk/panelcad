"""Live design checks: one test per check, plus the shared validate/audit path."""

from pathlib import Path

import pytest

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.core.checks import CHECK_IDS, STRUCTURAL_CHECKS, run_checks
from panelcad.core.models import Component, Wire
from panelcad.examples.dol import build_dol


def _by_check(session: Session, check: str, *, include_suppressed: bool = False) -> list[dict[str, str]]:
    return [row for row in session.design_checks(include_suppressed=include_suppressed) if row["check"] == check]


def test_check_catalog_covers_every_named_check():
    assert "duplicate_tag" in CHECK_IDS
    assert "dangling_wire" in CHECK_IDS
    assert "layout_not_ready" in CHECK_IDS
    assert STRUCTURAL_CHECKS <= set(CHECK_IDS)


def test_validate_stays_structural_on_dol():
    s = build_dol()
    assert s.validate() == []
    checks = {row["check"] for row in s.design_checks()}
    assert "dangling_wire" not in checks
    assert "wire_same_pin" not in checks


def test_duplicate_tag_same_sheet():
    s = Session.create("Dup")
    s.place("iec.breakers.mcb_1p_16a", ref="Q1", x=20, y=20)
    s.sheet().components.append(
        Component(id="Q1", library_id="iec.breakers.mcb_1p_16a", x=60, y=20)
    )
    errors = s.validate()
    assert any("duplicate ref Q1" in err for err in errors)
    assert _by_check(s, "duplicate_tag")


def test_duplicate_device_tag_across_sheets():
    s = Session.create("Tags")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20)
    s.add_sheet("two", kind="schematic")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20, sheet_id="two")
    rows = _by_check(s, "duplicate_device_tag")
    assert rows
    assert rows[0]["ref"] == "K1"
    s.add_sheet("contacts", kind="schematic")
    s.place("iec.contactors.aux_no", ref="K1", x=20, y=20, sheet_id="contacts")
    family = Session.create("Family")
    family.place("iec.contactors.coil", ref="K1", x=20, y=20)
    family.add_sheet("aux", kind="schematic")
    family.place("iec.contactors.aux_no", ref="K1", x=20, y=20, sheet_id="aux")
    assert _by_check(family, "duplicate_device_tag") == []


def test_unknown_library_and_format():
    s = Session.create("Lib")
    s.sheet().components.append(Component(id="Z1", library_id="no.such.part", x=10, y=10))
    assert any("unknown library_id" in err for err in s.validate())
    assert _by_check(s, "unknown_library")


def test_dangling_wire_and_same_pin():
    s = Session.create("Wires")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20)
    s.sheet().wires.append(Wire.model_validate({"id": "wbad", "from": "K1.A1", "to": "K9.A2"}))
    s.sheet().wires.append(Wire.model_validate({"id": "wloop", "from": "K1.A2", "to": "K1.A2"}))
    errors = s.validate()
    assert any("wbad" in err for err in errors)
    assert any("same pin" in err for err in errors)
    assert _by_check(s, "dangling_wire")
    assert _by_check(s, "wire_same_pin")


def test_unconnected_device():
    s = Session.create("Open")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20)
    rows = _by_check(s, "unconnected_device")
    assert any(row["ref"] == "K1" for row in rows)
    s.wire("K1.A1", "K1.A2", net="COIL")
    assert not any(row["ref"] == "K1" for row in _by_check(s, "unconnected_device"))


def test_wire_number_conflict_is_still_validate():
    s = Session.create("Clash")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20)
    s.place("iec.operators.pilot_light", ref="H1", x=80, y=20)
    s.place("iec.operators.pb_no", ref="S1", x=40, y=80)
    s.wire("K1.A2", "H1.X1", net="24V")
    s.wire("S1.13", "S1.14", net="START")
    s.sheet().wires[0].number = "7"
    s.sheet().wires[1].number = "7"
    assert any("wire number 7" in err for err in s.validate())
    assert _by_check(s, "wire_number_conflict")


@pytest.mark.confirm_generic_layout
def test_missing_panel_door_and_schematic():
    s = Session.create("Audit")
    s.place("iec.contactors.coil", ref="K1", x=30, y=30)
    s.place("iec.operators.pb_no", ref="S1", x=80, y=30)
    checks = {(row["ref"], row["check"]) for row in s.design_checks()}
    assert ("K1", "missing_panel") in checks
    assert ("S1", "missing_door") in checks
    audit = {(row["ref"], row["issue"]) for row in s.audit_schematic_panel()}
    assert ("K1", "missing_panel") in audit
    s.add_sheet("panel", kind="panel_layout")
    s.sheet("panel").components.append(
        Component(id="Q9", library_id="iec.breakers.mcb_1p_16a", x=80, y=40)
    )
    assert any(row["ref"] == "Q9" and row["check"] == "missing_schematic" for row in s.design_checks())


def test_terminal_not_on_strip_and_missing():
    s = Session.create("Terms")
    s.place("iec.terminals.x_2p5", ref="GND", x=20, y=20)
    assert any(row["ref"] == "GND" for row in _by_check(s, "terminal_not_on_strip"))
    s.place("iec.terminals.x_2p5", ref="X1-1", x=20, y=60)
    assert not any(row["ref"] == "X1-1" for row in _by_check(s, "terminal_not_on_strip"))
    s.place("iec.contactors.coil", ref="K1", x=80, y=20)
    s.sheet().wires.append(Wire.model_validate({"id": "wx", "from": "K1.A1", "to": "X9-9.A"}))
    missing = _by_check(s, "terminal_missing")
    assert any(row["target"] == "X9-9" for row in missing)


def test_missing_part_number():
    s = Session.create("PN")
    s.place("iec.breakers.mcb_1p_16a", ref="Q1", x=20, y=20)
    assert any(row["ref"] == "Q1" for row in _by_check(s, "missing_part_number"))
    s.set_component_properties("Q1", {"part_number": "S202-C16"})
    assert not any(row["ref"] == "Q1" for row in _by_check(s, "missing_part_number"))


def test_protection_over_downstream_device_and_wire():
    s = Session.create("OCPD")
    s.place("iec.breakers.mcb_3p_32a", ref="Q1", x=20, y=20)
    s.place("iec.breakers.mcb_1p_16a", ref="Q2", x=20, y=90)
    s.wire("Q1.2", "Q2.1", net="L1")
    rows = _by_check(s, "protection_over_downstream")
    assert any(row["ref"] == "Q1" and row["target"] == "Q2" for row in rows)
    t = Session.create("Gauge")
    t.place("iec.breakers.mcb_3p_32a", ref="Q1", x=20, y=20)
    t.place("iec.loads.motor_3ph", ref="M1", x=20, y=90)
    t.wire("Q1.2", "M1.U1", net="U1")
    t.sheet().wires[0].gauge = "18 AWG"
    gauge = _by_check(t, "protection_over_downstream")
    assert any("18" in row["message"] for row in gauge)


def test_overload_outside_fla():
    s = Session.create("OL")
    s.place("iec.protection.overload_9a", ref="F1", x=20, y=20)
    s.place("iec.loads.motor_3ph", ref="M1", x=20, y=80)
    s.wire("F1.2", "M1.U1", net="U1")
    s.set_component_properties("M1", {"FLA": "8A"})
    s.set_component_properties("F1", {"setting": "2A"})
    rows = _by_check(s, "overload_outside_fla")
    assert any(row["ref"] == "F1" and row["target"] == "M1" for row in rows)
    s.set_component_properties("F1", {"setting": "8.5A"})
    assert not any(row["ref"] == "F1" for row in _by_check(s, "overload_outside_fla"))


def test_mpcb_range_used_by_overload_fla_check():
    s = Session.create("MPCB FLA")
    s.place("iec.breakers.mpcb_3p", ref="Q1", x=20, y=20)
    s.place("iec.loads.motor_3ph", ref="M1", x=20, y=80)
    s.wire("Q1.2", "M1.U1", net="U1")
    s.assign_part("Q1", "3RV2011-1DA25", manufacturer="Siemens")
    s.set_component_properties("M1", {"FLA": "2.8A"})
    s.set_component_properties("Q1", {"setting": "2.8A"})
    assert not any(row["ref"] == "Q1" for row in _by_check(s, "overload_outside_fla"))
    s.set_component_properties("Q1", {"setting": "8A"})
    rows = _by_check(s, "overload_outside_fla")
    assert any(row["ref"] == "Q1" for row in rows)


def test_unwired_contactor_coil():
    s = Session.create("Open coil")
    s.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40)
    rows = _by_check(s, "unwired_coil")
    assert any(row["ref"] == "K1" and "A1" in row["message"] for row in rows)
    s.wire("K1.A1", "K1.A2", net="coil")
    assert not any(row["ref"] == "K1" for row in _by_check(s, "unwired_coil"))


def test_coil_voltage_mismatch():
    s = Session.create("Coil")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20)
    s.wire("K1.A1", "K1.A2", net="120VAC")
    rows = _by_check(s, "coil_voltage_mismatch")
    assert any(row["ref"] == "K1" for row in rows)
    t = Session.create("Match")
    t.place("iec.contactors.coil", ref="K1", x=20, y=20)
    t.wire("K1.A1", "K1.A2", net="24VDC")
    assert not any(row["ref"] == "K1" for row in _by_check(t, "coil_voltage_mismatch"))


@pytest.mark.confirm_generic_layout
def test_outside_enclosure():
    s = Session.create("Box")
    s.add_sheet("panel", kind="panel_layout")
    s.set_enclosure(width_mm=80, height_mm=80, x=20, y=20, sheet_id="panel")
    s.sheet("panel").components.append(
        Component(id="Q1", library_id="iec.breakers.mccb_3p_100a", x=200, y=200)
    )
    rows = _by_check(s, "outside_enclosure")
    assert any(row["ref"] == "Q1" for row in rows)


def test_suppress_finding_is_saved_on_the_project(tmp_path: Path):
    s = Session.create("Ignore")
    s.place("iec.breakers.mcb_1p_16a", ref="Q1", x=20, y=20)
    hit = _by_check(s, "missing_part_number")[0]
    result = s.suppress_finding(hit["id"])
    assert result["suppressed"] is True
    assert _by_check(s, "missing_part_number") == []
    hidden = _by_check(s, "missing_part_number", include_suppressed=True)
    assert hidden and hidden[0]["suppressed"] == "yes"
    path = tmp_path / "job.pcad.yaml"
    s.save(path)
    again = Session.open(path)
    assert hit["id"] in again.project.properties["suppressed_checks"]
    again.unsuppress_finding(hit["id"])
    assert _by_check(again, "missing_part_number")


def test_agent_and_export_use_the_same_engine(tmp_path: Path):
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "Live"})["ok"]
    assert agent.handle(
        {"id": 2, "op": "place", "library_id": "iec.breakers.mcb_1p_16a", "ref": "Q1", "x": 20, "y": 20}
    )["ok"]
    r = agent.handle({"id": 3, "op": "design_checks"})
    assert r["ok"]
    checks = {row["check"] for row in r["result"]["rows"]}
    assert "missing_part_number" in checks
    assert "unconnected_device" in checks
    dest = tmp_path / "checks.csv"
    exported = agent.handle({"id": 4, "op": "export_checks", "path": str(dest)})
    assert exported["ok"]
    assert dest.is_file()
    finding_id = next(row["id"] for row in r["result"]["rows"] if row["check"] == "missing_part_number")
    ignored = agent.handle({"id": 5, "op": "suppress_check", "finding_id": finding_id})
    assert ignored["ok"]
    after = agent.handle({"id": 6, "op": "design_checks"})
    assert finding_id not in {row["id"] for row in after["result"]["rows"]}


@pytest.mark.confirm_generic_layout
def test_run_checks_includes_every_check_id_on_a_dirty_drawing():
    s = Session.create("All")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20)
    s.place("iec.operators.pb_no", ref="S1", x=80, y=20)
    s.place("iec.terminals.x_2p5", ref="GND", x=20, y=80)
    s.place("iec.protection.overload_9a", ref="F1", x=80, y=80)
    s.place("iec.loads.motor_3ph", ref="M1", x=140, y=80)
    s.set_component_properties("M1", {"FLA": "8A"})
    s.set_component_properties("F1", {"setting": "2A"})
    s.wire("F1.2", "M1.U1", net="U1")
    s.wire("K1.A1", "K1.A2", net="120VAC")
    s.sheet().wires.append(Wire.model_validate({"id": "wbad", "from": "K1.A1", "to": "X9-9.A"}))
    s.add_sheet("two", kind="schematic")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20, sheet_id="two")
    s.add_sheet("panel", kind="panel_layout")
    s.set_enclosure(width_mm=60, height_mm=60, x=20, y=20, sheet_id="panel")
    # Existing projects may already have a generic on the layout. Open must not crash.
    s.sheet("panel").components.append(
        Component(id="Q9", library_id="iec.breakers.mccb_3p_100a", x=180, y=180)
    )
    found = {item.check for item in run_checks(s.project, s.catalog)}
    for name in (
        "duplicate_device_tag",
        "dangling_wire",
        "unconnected_device",
        "missing_panel",
        "missing_door",
        "missing_schematic",
        "terminal_not_on_strip",
        "terminal_missing",
        "missing_part_number",
        "overload_outside_fla",
        "coil_voltage_mismatch",
        "outside_enclosure",
        "layout_not_ready",
    ):
        assert name in found, name


def test_large_generated_project_stays_under_a_second():
    import time

    s = Session.create("Big")
    sheet = s.sheet()
    for i in range(250):
        s.place("iec.terminals.x_2p5", ref=f"X1-{i + 1}", x=20 + (i % 20) * 12, y=20 + (i // 20) * 16)
    for i in range(249):
        s.wire(f"X1-{i + 1}.A", f"X1-{i + 2}.A", net=f"N{i}")
    s.add_sheet("panel", kind="panel_layout")
    panel = s.sheet("panel")
    for i in range(250):
        panel.components.append(
            Component(
                id=f"X1-{i + 1}",
                library_id="iec.terminals.x_2p5",
                x=30 + (i % 25) * 8,
                y=40,
            )
        )
    t0 = time.perf_counter()
    rows = s.design_checks()
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, elapsed
    assert isinstance(rows, list)
    assert len(sheet.components) == 250
