"""UL 508A Supplement SB short-circuit current rating."""

from __future__ import annotations

from pathlib import Path

import pytest

from panelcad import Session
from panelcad.electrical.sccr import (
    compute_sccr,
    motor_controller_rating,
    nameplate_text,
    peak_let_through,
    transformer_secondary_ka,
)
from panelcad.electrical.sccr_tables import FUSE_IP_KA
from panelcad.library.parts import CatalogPart


def _part(result: dict, ref: str) -> dict:
    return next(item for item in result["components"] if item["ref"] == ref)


def _place(session: Session, library_id: str, ref: str, x: float, y: float, **props):
    return session.place(library_id, ref=ref, x=x, y=y, properties=props or None, snap=False)


def test_table_sb41_defaults_and_motor_bands():
    assert motor_controller_rating(10, None)[0] == 5
    assert motor_controller_rating(500, None)[0] == 30
    assert motor_controller_rating(1000, None)[0] == 85
    assert motor_controller_rating(1500, None)[0] == 85
    gap_ka, _source, gap_flags = motor_controller_rating(50.5, None)
    assert gap_ka == 5
    assert gap_flags
    assert motor_controller_rating(2000, None)[0] is None

    session = Session.create("SB41")
    _place(session, "nema.breakers.mcb_3p_32a", "CB1", 40, 20)
    _place(session, "nema.protection.overload", "OL1", 40, 80)
    _place(session, "iec.terminals.bus_v", "TB1", 40, 140)
    _place(session, "nema.breakers.mcb_3p_32a", "CB2", 120, 20, ul_type="supplementary")
    _place(session, "iec.power.receptacle", "REC1", 120, 80)
    _place(session, "iec.power.receptacle", "REC2", 120, 140, gfci=True)
    _place(session, "nema.contactors.ac3_9a", "M500", 200, 20, hp=500)
    result = session.compute_sccr()
    assert _part(result, "CB1")["base_ka"] == 5
    assert _part(result, "OL1")["base_ka"] == 5
    assert _part(result, "TB1")["base_ka"] == 10
    assert _part(result, "CB2")["base_ka"] == 0.2
    assert _part(result, "REC1")["base_ka"] == 10
    assert _part(result, "REC2")["base_ka"] == 2
    assert _part(result, "M500")["base_ka"] == 30
    assert result["panel_sccr_ka"] == 0.2
    assert "CB2" in result["limiting_refs"]


def test_mercury_switch_is_user_entered_until_volts_amps_and_kva_are_known():
    session = Session.create("Mercury")
    _place(session, "nema.protection.disconnect_3p", "DS1", 40, 20, ul_type="mercury")
    result = session.compute_sccr()
    assert _part(result, "DS1")["base_ka"] is None
    assert result["incomplete"] is True
    assert result["panel_sccr_ka"] is None
    session.set_part_sccr("DS1", ul_type="mercury", volts=240, amps=30, kva=1)
    rated = session.compute_sccr()
    assert _part(rated, "DS1")["base_ka"] == 1
    session.set_part_sccr("DS1", volts=240, amps=30, kva=3, ul_type="mercury")
    assert _part(session.compute_sccr(), "DS1")["base_ka"] == 3.5
    session.set_part_sccr("DS1", volts=480, amps=30, kva=1, ul_type="mercury")
    assert _part(session.compute_sccr(), "DS1")["base_ka"] == 5


def test_unknown_power_device_is_flagged_and_not_invented():
    session = Session.create("Drive")
    _place(session, "nema.drives.vfd_1k5", "VFD1", 40, 20)
    result = session.compute_sccr()
    part = _part(result, "VFD1")
    assert part["base_ka"] is None
    assert result["panel_sccr_ka"] is None
    assert result["incomplete"] is True
    assert any("sccr_ka" in flag for flag in part["flags"])


def test_class_j_let_through_raises_a_10ka_bus_and_not_a_5ka_starter():
    assert peak_let_through("J", 60, 100)[0] == 10
    assert peak_let_through("J", 45, 100)[0] == 10
    assert FUSE_IP_KA[("T300", 100)][2] == 15
    assert FUSE_IP_KA[("RK1", 600)][0] == 45

    bus = Session.create("Bus")
    _place(bus, "nema.protection.fuse_10a", "FU1", 20, 20, fuse_class="J", fuse_amps=60, ir_ka=100)
    _place(bus, "iec.terminals.bus_v", "TB1", 20, 80)
    bus.wire("FU1.2", "TB1.IN")
    rated = bus.compute_sccr()
    assert _part(rated, "TB1")["base_ka"] == 10
    assert _part(rated, "TB1")["effective_ka"] == 100
    assert rated["panel_sccr_ka"] == 100

    starter = Session.create("Starter")
    _place(starter, "nema.protection.fuse_10a", "FU1", 20, 20, fuse_class="J", fuse_amps=60, ir_ka=100)
    _place(starter, "nema.contactors.ac3_9a", "M1", 20, 80)
    starter.wire("FU1.2", "M1.L1")
    held = starter.compute_sccr()
    assert _part(held, "M1")["base_ka"] == 5
    assert _part(held, "M1")["effective_ka"] == 5
    assert held["panel_sccr_ka"] == 5


def test_conflict_cells_use_the_higher_published_let_through():
    session = Session.create("Conflict")
    _place(
        session,
        "nema.protection.fuse_10a",
        "FU1",
        20,
        20,
        fuse_class="T300",
        fuse_amps=100,
        ir_ka=200,
    )
    _place(session, "iec.terminals.bus_v", "TB1", 20, 80, sccr_ka=14)
    session.wire("FU1.2", "TB1.IN")
    rated = session.compute_sccr()
    assert _part(rated, "TB1")["effective_ka"] == 100

    rk = Session.create("RK1")
    _place(rk, "nema.protection.fuse_10a", "FU1", 20, 20, fuse_class="RK1", fuse_amps=600, ir_ka=50)
    _place(rk, "iec.terminals.bus_v", "TB1", 20, 80, sccr_ka=44)
    rk.wire("FU1.2", "TB1.IN")
    assert _part(rk.compute_sccr(), "TB1")["effective_ka"] == 44


def test_feeder_component_upstream_of_a_current_limiting_fuse_is_not_raised():
    session = Session.create("Feeder")
    _place(session, "nema.protection.disconnect_3p", "DS1", 20, 10, sccr_ka=65)
    _place(session, "nema.protection.fuse_10a", "FU1", 20, 40, fuse_class="J", fuse_amps=60, ir_ka=200)
    _place(session, "iec.terminals.bus_v", "TB1", 20, 80)
    session.wire("DS1.T1", "FU1.1")
    session.wire("FU1.2", "TB1.IN")
    rated = session.compute_sccr()
    assert _part(rated, "DS1")["effective_ka"] == 65
    assert _part(rated, "DS1")["circuit"] == "feeder"
    assert _part(rated, "TB1")["effective_ka"] == 100


def test_branch_breaker_interrupting_rating_is_not_series_rated():
    session = Session.create("Series")
    _place(session, "nema.protection.fuse_10a", "FU1", 20, 20, fuse_class="J", fuse_amps=60, ir_ka=200)
    _place(session, "nema.breakers.mcb_3p_32a", "CB1", 20, 80, ir_ka=35)
    _place(session, "nema.contactors.ac3_9a", "M1", 20, 150, sccr_ka=65)
    session.wire("FU1.2", "CB1.1")
    session.wire("CB1.2", "M1.L1")
    rated = session.compute_sccr()
    assert _part(rated, "CB1")["effective_ka"] == 35
    assert _part(rated, "M1")["effective_ka"] == 200
    assert rated["panel_sccr_ka"] == 35
    assert rated["limiting_refs"] == ["CB1"]


def test_current_limiting_breaker_needs_published_let_through():
    session = Session.create("CL breaker")
    _place(session, "nema.breakers.mcb_3p_32a", "CB1", 20, 20, ir_ka=65, current_limiting=True)
    _place(session, "nema.contactors.ac3_9a", "M1", 20, 90)
    session.wire("CB1.2", "M1.L1")
    blocked = session.compute_sccr()
    assert _part(blocked, "M1")["effective_ka"] == 5
    assert any("let_through" in flag for flag in _part(blocked, "CB1")["flags"])

    session.set_part_sccr("CB1", let_through_ka=4, let_through_at_ka=50, current_limiting=True, ir_ka=65)
    raised = session.compute_sccr()
    assert _part(raised, "M1")["effective_ka"] == 50
    assert _part(raised, "CB1")["effective_ka"] == 65


def test_transformer_formula_and_screens():
    isc, _source, flags, mode = transformer_secondary_ka(
        kva=10, secondary_voltage=480, phases=3, impedance_pct=None
    )
    assert mode == "formula"
    assert isc == pytest.approx(0.573, abs=0.001)
    assert any("2.1" in flag for flag in flags)

    def _stack(**transformer):
        session = Session.create("XFMR")
        _place(session, "nema.breakers.mcb_3p_32a", "CB1", 20, 10, ir_ka=65)
        _place(session, "nema.power.control_transformer", "T1", 20, 70, **transformer)
        _place(session, "nema.contactors.ac3_9a", "M1", 20, 140, sccr_ka=transformer.pop("load_ka", 5))
        session.wire("CB1.2", "T1.H1")
        session.wire("T1.X1", "M1.L1")
        return session.compute_sccr()

    raised = _stack(kva=10, secondary_voltage=480, phases=3)
    assert _part(raised, "M1")["effective_ka"] == 65
    assert "SB4.3.1" in _part(raised, "M1")["effective_source"]
    assert raised["panel_sccr_ka"] == 65

    held = _stack(kva=10, secondary_voltage=480, phases=3, load_ka=0.2)
    assert _part(held, "M1")["effective_ka"] == 0.2
    assert held["panel_sccr_ka"] == 0.2

    screened = _stack(kva=10)
    assert _part(screened, "M1")["effective_ka"] == 65

    low = _stack(kva=10, load_ka=2)
    assert _part(low, "M1")["effective_ka"] == 2

    small = _stack(kva=5, secondary_voltage_ceiling=120, load_ka=2)
    assert _part(small, "M1")["effective_ka"] == 65
    tiny = _stack(kva=5, secondary_voltage_ceiling=120, load_ka=1)
    assert _part(tiny, "M1")["effective_ka"] == 1


def test_panel_rating_is_the_lowest_result():
    session = Session.create("Lowest")
    _place(session, "nema.breakers.mcb_3p_32a", "CB1", 20, 20, ir_ka=65)
    _place(session, "nema.contactors.ac3_9a", "M1", 20, 80, sccr_ka=25)
    _place(session, "nema.protection.overload", "OL1", 20, 140, sccr_ka=18)
    session.wire("CB1.2", "M1.L1")
    session.wire("M1.T1", "OL1.T1")
    rated = session.compute_sccr()
    assert rated["panel_sccr_ka"] == 18
    assert rated["limiting_refs"] == ["OL1"]


def test_available_fault_check_fires_only_when_the_panel_is_below_it():
    session = Session.create("Fault")
    _place(session, "nema.breakers.mcb_3p_32a", "CB1", 40, 20, ir_ka=10)
    checks = {row["check"] for row in session.design_checks()}
    assert "sccr_below_available" not in checks
    session.set_available_fault(10)
    assert "sccr_below_available" not in {row["check"] for row in session.design_checks()}
    session.set_available_fault(18, voltage=240)
    rows = [row for row in session.design_checks() if row["check"] == "sccr_below_available"]
    assert len(rows) == 1
    assert rows[0]["severity"] == "warning"
    assert "240" in rows[0]["message"]
    bare = Session.create("Bare fault")
    _place(bare, "nema.drives.vfd_1k5", "VFD1", 40, 20)
    bare.set_available_fault(5)
    assert any(row["check"] == "sccr_below_available" for row in bare.design_checks())


def test_symbol_icu_is_not_a_ul_rating_and_catalog_string_is_flagged():
    iec = Session.create("IEC DOL")
    iec.set_title_block(standard="IEC")
    iec.insert_circuit(kind="dol")
    rated = iec.compute_sccr()
    breaker = _part(rated, "Q1")
    assert breaker["base_ka"] == 5
    assert breaker["effective_ka"] == 5
    assert any("IEC" in flag for flag in breaker["flags"])
    assert rated["voltage"] == 400

    session = Session.create("Catalog")
    _place(session, "iec.contactors.ac3_9a", "K1", 40, 40)
    session.assign_part("K1", "LC1D09BD", manufacturer="Schneider Electric")
    part = _part(session.compute_sccr(), "K1")
    assert part["base_ka"] == 100
    assert any("SB4.2.3" in flag or "specified" in flag for flag in part["flags"])


def test_catalog_part_accepts_numeric_sccr():
    part = CatalogPart(
        manufacturer="Acme",
        catalog="X1",
        library_id="nema.contactors.ac3_9a",
        category="switching.contactor",
        verified="partial",
        source="https://example.com/x1",
        sccr_ka=65,
    )
    assert part.assignment_properties()["sccr_ka"] == 65
    assert part.to_row()["sccr_ka"] == "65"
    with pytest.raises(ValueError):
        CatalogPart(
            manufacturer="Acme",
            catalog="X2",
            library_id="nema.contactors.ac3_9a",
            category="switching.contactor",
            verified="partial",
            source="https://example.com/x2",
            sccr_ka=-1,
        )


def test_dol_end_to_end_is_5ka_at_480():
    session = Session.create("DOL")
    session.insert_circuit(kind="dol")
    rated = session.compute_sccr()
    assert rated["panel_sccr_ka"] == 5
    assert rated["voltage"] == 480
    assert set(rated["limiting_refs"]) == {"CB1", "M1", "OL1"}
    motor = _part(rated, "MTR1")
    assert motor["exempt"] is True
    assert motor["limits"] is False
    refs = {part["ref"] for part in rated["components"]}
    assert refs == {"CB1", "M1", "OL1", "MTR1"}
    assert nameplate_text(session.project, session.catalog) == "SCCR 5 kA @ 480 V"
    empty = Session.create("Empty")
    assert nameplate_text(empty.project, empty.catalog) == ""


def test_reversing_end_to_end_includes_both_contactors():
    session = Session.create("REV")
    session.insert_circuit(kind="reversing", control="none")
    rated = session.compute_sccr()
    assert rated["panel_sccr_ka"] == 5
    assert rated["voltage"] == 480
    forward = _part(rated, "M1")
    reverse = _part(rated, "M2")
    assert forward["exempt"] is False
    assert reverse["exempt"] is False
    assert forward["effective_ka"] == 5
    assert reverse["effective_ka"] == 5
    assert {"M1", "M2", "CB1", "OL1"} <= set(rated["limiting_refs"])
    roles = {part["role"] for part in rated["components"]}
    assert "fwd" in roles and "rev" in roles
    assert "estop" not in roles
    assert "fwd_pb" not in roles

    star = Session.create("Star")
    _place(star, "nema.contactors.ac3_9a", "S1", 40, 40, role="star")
    _place(star, "nema.breakers.mcb_3p_32a", "CB1", 40, 10)
    skipped = star.compute_sccr()
    assert _part(skipped, "S1")["exempt"] is True
    assert skipped["panel_sccr_ka"] == 5
    coil = Session.create("Coil")
    _place(coil, "nema.contactors.coil", "M1", 40, 40)
    _place(coil, "nema.protection.ol_contact", "OL1", 80, 40)
    assert coil.compute_sccr()["components"] == []


def test_multimotor_panel_is_limited_by_the_lower_branch_breaker():
    session = _multimotor()
    rated = session.compute_sccr()
    assert _part(rated, "DS1")["effective_ka"] == 65
    assert _part(rated, "TB1")["effective_ka"] == 100
    assert _part(rated, "CB1")["effective_ka"] == 65
    assert _part(rated, "CB2")["effective_ka"] == 35
    assert rated["panel_sccr_ka"] == 35
    assert rated["limiting_refs"] == ["CB2"]
    assert rated["voltage"] == 480


def test_jsonl_ops_and_drawing_agent_tool(tmp_path: Path):
    from panelcad.cli.agent import Agent

    agent = Agent()
    assert agent.handle({"id": "req-1", "op": "create_project", "name": "Agent SCCR"})["ok"]
    assert agent.handle({"id": "req-2", "op": "insert_circuit", "kind": "dol"})["ok"]
    rated = agent.handle({"id": "req-3", "op": "compute_sccr"})
    assert rated["ok"] is True
    assert rated["id"] == "req-3"
    assert rated["result"]["panel_sccr_ka"] == 5
    assert "req-3" not in {part["ref"] for part in rated["result"]["components"]}
    updated = agent.handle({"id": "req-4", "op": "set_part_sccr", "ref": "CB1", "sccr_ka": 65})
    assert updated["ok"] is True
    assert _part(updated["result"], "CB1")["effective_ka"] == 65
    assert updated["result"]["panel_sccr_ka"] == 5
    fault = agent.handle({"id": "req-5", "op": "set_available_fault", "ka": 25})
    assert fault["ok"] is True
    assert fault["result"]["below_available"] is True
    checks = agent.handle({"id": "req-6", "op": "design_checks"})
    assert any(row["check"] == "sccr_below_available" for row in checks["result"]["rows"])

    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from panelcad.ai.drawing import drawing_agent, open_drawing

    drawing = open_drawing(name="Agent SCCR")
    drawing.operator.handle({"op": "insert_circuit", "kind": "dol"})
    seen: list[str] = []

    def model(messages, info):
        seen.extend(tool.name for tool in info.function_tools)
        blob = str(messages)
        if "panel_sccr_ka" in blob:
            return ModelResponse(parts=[TextPart(content="rated")])
        return ModelResponse(
            parts=[ToolCallPart(tool_name="compute_sccr", args={}, tool_call_id="sccr-tool")]
        )

    with drawing_agent.override(model=FunctionModel(model)):
        result = drawing_agent.run_sync("Rate the panel.", deps=drawing)
    assert result.output == "rated"
    assert {"compute_sccr", "set_available_fault", "set_part_sccr"} <= set(seen)


def test_cli_sccr_command(tmp_path: Path):
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    path = tmp_path / "dol.pcad.yaml"
    session = Session.create("CLI DOL")
    session.insert_circuit(kind="dol")
    session.save(path)
    pdf = tmp_path / "sccr.pdf"
    png = tmp_path / "sccr.png"
    result = CliRunner().invoke(
        app,
        [
            "electrical",
            "sccr",
            str(path),
            "--pdf",
            str(pdf),
            "--png",
            str(png),
            "--available-ka",
            "10",
            "--voltage",
            "480",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "5 kA" in result.output
    assert pdf.is_file() and pdf.stat().st_size > 1000
    assert png.is_file() and png.stat().st_size > 1000
    opened = Session.open(path)
    assert opened.project.properties["available_fault_ka"] == 10
    assert opened.project.properties["sccr_voltage"] == 480


def test_report_page_and_dialog_show_the_dol_result(tmp_path: Path):
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.sccr_dialog import SccrDialog
    from panelcad.render.svg import sheet_to_svg

    if QApplication.instance() is None:
        QApplication([])
    session = Session.create("Dialog")
    session.insert_circuit(kind="dol")
    dialog = SccrDialog(session)
    dialog.show()
    QApplication.processEvents()
    assert "5 kA" in dialog.summary.text()
    assert dialog.table.rowCount() >= 4
    dialog.recalculate()
    QApplication.processEvents()
    assert session.compute_sccr()["panel_sccr_ka"] == 5
    dialog.close()

    sheet = session.sheet()
    svg = sheet_to_svg(sheet, session.catalog, session.project)
    assert "SCCR 5 kA @ 480 V" in svg
    empty = Session.create("Blank")
    blank = sheet_to_svg(empty.sheet(), empty.catalog, empty.project)
    assert "SCCR" not in blank


def _multimotor() -> Session:
    """Feeder fuse, line-side disconnect, bus, and two branch breakers."""

    session = Session.create("Two motors")
    _place(session, "nema.protection.disconnect_3p", "DS1", 30, 10, sccr_ka=65)
    _place(
        session,
        "nema.protection.fuse_10a",
        "FU1",
        30,
        40,
        fuse_class="J",
        fuse_amps=60,
        ir_ka=200,
    )
    _place(session, "iec.terminals.bus_v", "TB1", 30, 80)
    _place(session, "nema.breakers.mcb_3p_32a", "CB1", 20, 150, ir_ka=65)
    _place(session, "nema.contactors.ac3_9a", "M1", 20, 220, sccr_ka=65)
    _place(session, "nema.protection.overload", "OL1", 20, 300, sccr_ka=65)
    _place(
        session,
        "nema.loads.motor_3ph",
        "MTR1",
        20,
        370,
        role="motor",
        voltage=480,
        hp=10,
    )
    _place(session, "nema.breakers.mcb_3p_32a", "CB2", 120, 150, ir_ka=35)
    _place(session, "nema.contactors.ac3_9a", "M2", 120, 220, sccr_ka=65)
    _place(session, "nema.protection.overload", "OL2", 120, 300, sccr_ka=65)
    _place(
        session,
        "nema.loads.motor_3ph",
        "MTR2",
        120,
        370,
        role="motor",
        voltage=480,
        hp=10,
    )
    session.wire("DS1.T1", "FU1.1")
    session.wire("FU1.2", "TB1.IN")
    session.wire("TB1.T1", "CB1.1")
    session.wire("CB1.2", "M1.L1")
    session.wire("M1.T1", "OL1.T1")
    session.wire("OL1.2", "MTR1.T1")
    session.wire("TB1.T2", "CB2.1")
    session.wire("CB2.2", "M2.L1")
    session.wire("M2.T1", "OL2.T1")
    session.wire("OL2.2", "MTR2.T1")
    return session


def test_iec_star_delta_sccr_ignores_the_pe_symbol():
    """The earth symbol is not a power device. The panel stays 5 kA at 400 V."""

    session = Session.create("IEC star sccr")
    session.set_title_block(standard="IEC")
    session.insert_circuit(kind="star_delta", control="local")
    pe = [
        comp
        for sheet in session.project.sheets
        for comp in sheet.components
        if comp.properties.get("role") == "pe_bar"
    ]
    assert pe and pe[0].library_id == "iec.connection.ground"
    rated = session.compute_sccr()
    assert rated["panel_sccr_ka"] == 5
    assert rated["voltage"] == 400
    assert "PE" not in rated["limiting_refs"]
    text = nameplate_text(session.project, session.catalog)
    assert "5 kA" in text and "400" in text
