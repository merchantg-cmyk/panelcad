"""Motor and conductor sizing: published table cells, Circuit Builder, checks, CLI, agent."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.core.motor_sizing import (
    ampacity_of,
    branch_device_max,
    citation_markdown,
    control_circuit,
    feeder_ampacity,
    motor_flc,
    select_wire,
    size_motor_circuit,
)
from panelcad.core.sizing_checks import SIZING_CHECKS


def _artifact_dir(tmp_path: Path) -> Path:
    """Folder for sizing screenshots. ``PANELCAD_TEST_ARTIFACTS`` overrides the temp dir."""

    raw = os.environ.get("PANELCAD_TEST_ARTIFACTS", "").strip()
    dest = Path(raw) if raw else tmp_path
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def _lib(result: dict, role: str) -> str:
    return next(item["library_id"] for item in result["components"] if item["role"] == role)


def _id(result: dict, role: str) -> str:
    return next(item["id"] for item in result["components"] if item["role"] == role)


def _sizing_rows(session: Session) -> list[dict]:
    return [row for row in session.design_checks() if row["check"] in SIZING_CHECKS]


def _assert_sheet_text_clear(session: Session, sheet_id: str) -> None:
    """Existing wire-over-text and text-overlap checks, on a sized power sheet."""

    import importlib.util

    from panelcad.render.sheet_labels import colliding_sheet_labels

    path = Path(__file__).resolve().parent / "test_wire_labels.py"
    spec = importlib.util.spec_from_file_location("panelcad_test_wire_labels", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._marks_clear(session, sheet_id)
    sheet = session.sheet(sheet_id)
    hits = [
        hit
        for hit in colliding_sheet_labels(session.project, session.catalog, sheet, ladder=False, sheet_no=1)
        if "wire:" in hit or "closer than one text height" in hit
    ]
    assert hits == [], hits


def test_table_430_250_and_248_spot_checks():
    assert motor_flc(hp=5, voltage=460, phases=3) == (5.0, 460.0, 7.6)
    assert motor_flc(hp=5, voltage=230, phases=3)[2] == 15.2
    assert motor_flc(hp=5, voltage=200, phases=3)[2] == 17.5
    assert motor_flc(hp=5, voltage=575, phases=3)[2] == 6.1
    assert motor_flc(hp=10, voltage=230, phases=3)[2] == 28.0
    assert motor_flc(hp=10, voltage=460, phases=3)[2] == 14.0
    assert motor_flc(hp=10, voltage=480, phases=3)[2] == 14.0
    assert motor_flc(hp=10, voltage=208, phases=3)[2] == 30.8
    assert motor_flc(hp=0.5, voltage=115, phases=3)[2] == 4.4
    assert motor_flc(hp=1.5, voltage=575, phases=3)[2] == 2.4
    assert motor_flc(hp=25, voltage=460, phases=3)[2] == 34.0
    assert motor_flc(hp=10, voltage=400, phases=3) is None
    assert motor_flc(hp=5, voltage=460, phases=1) is None
    assert motor_flc(hp=1 / 6, voltage=115, phases=1)[2] == 4.4
    assert motor_flc(hp=1, voltage=115, phases=1)[2] == 16.0
    assert motor_flc(hp=1 / 3, voltage=208, phases=1)[2] == 4.0
    assert motor_flc(hp=5, voltage=230, phases=1)[2] == 28.0
    assert motor_flc(hp=10, voltage=230, phases=1)[2] == 50.0


def test_conductor_breaker_fuse_and_wire_tables():
    sized = size_motor_circuit(hp=5, volts=460)
    assert sized["ok"] is True
    assert sized["flc_a"] == 7.6
    assert sized["conductor_min_a"] == 9.5
    assert sized["wire"]["gauge"] == "14 AWG"
    assert sized["wire"]["ampacity_a"] == 15
    assert sized["wire"]["temp_c"] == 60
    assert sized["wire"]["governed_by"] == "both"
    assert "29.6" in sized["report"]
    assert "310.16" in sized["report"]
    assert "28.1" in sized["report"]
    assert "not verified against the standard" in sized["report"]
    assert "not verified against an official NFPA text" in sized["report"]
    assert sized["ocpd"]["calculated_a"] == 19
    assert sized["ocpd"]["rating_a"] == 20
    assert sized["overload"]["setting_a"] == 7.6
    assert sized["overload"]["trip_percent"] == 115
    assert sized["overload"]["trip_max_a"] == pytest.approx(8.74)
    assert sized["contactor_frame_a"] == 9
    assert sized["control"]["gauge"] == "16 AWG"
    assert sized["control"]["column_a_separate_a"] == 10

    ten = size_motor_circuit(hp=10, volts=230)
    assert ten["flc_a"] == 28
    assert ten["conductor_min_a"] == 35
    assert ten["wire"]["gauge"] == "8 AWG"
    assert ten["wire"]["temp_c"] == 60
    assert ten["wire"]["nec_310_16"]["gauge"] == "8 AWG"
    assert ten["wire"]["ul_508a_28_1"]["gauge"] == "8 AWG"
    assert ten["ocpd"]["rating_a"] == 70
    assert "breaker_frame_below_rating" in ten["report"]
    marked = size_motor_circuit(hp=10, volts=230, termination_c=75)
    assert marked["wire"]["temp_c"] == 75
    assert marked["wire"]["nec_310_16"]["gauge"] == "10 AWG"
    assert marked["wire"]["ul_508a_28_1"]["gauge"] == "8 AWG"
    assert marked["wire"]["gauge"] == "8 AWG"
    assert marked["wire"]["governed_by"] == "ul_508a_28_1"
    assert "10 AWG" in marked["report"]
    assert "8 AWG" in marked["report"]
    assert "28.1" in marked["report"]
    big = size_motor_circuit(hp=50, volts=230)
    assert big["wire"]["temp_c"] == 75
    assert big["wire"]["gauge"] == "2/0 AWG"
    assert "above 100" in big["report"]

    fuse = branch_device_max(7.6, device="dual_element_fuse")
    assert fuse["calculated_a"] == pytest.approx(13.3)
    assert fuse["rating_a"] == 15
    instant = branch_device_max(7.6, device="instantaneous_trip")
    assert instant["rating_a"] == pytest.approx(60.8)
    assert instant["permitted_max_a"] == instant["calculated_a"]

    assert select_wire(35, table="nec_310_16", temp_c=60)["gauge"] == "8 AWG"
    assert select_wire(9.5, table="ul_508a_28_1", temp_c=75)["gauge"] == "14 AWG"
    assert ampacity_of("14 AWG", table="ul_508a_28_1", temp_c=75) == 15
    assert select_wire(35, table="ul_508a_28_1", temp_c=75)["gauge"] == "8 AWG"
    assert feeder_ampacity([14, 7.6])["required_a"] == pytest.approx(25.1)
    assert control_circuit(14)["column_a_separate_a"] is None

    nameplate = size_motor_circuit(hp=5, volts=460, fla=8)
    assert nameplate["conductor_min_a"] == 9.5
    assert nameplate["flc_a"] == 7.6
    assert nameplate["overload"]["setting_a"] == 8
    assert nameplate["ocpd"]["rating_a"] == 20

    missing = size_motor_circuit(hp=6, volts=460)
    assert missing["ok"] is False
    nearest = size_motor_circuit(hp=6, volts=460, allow_nearest_hp=True)
    assert nearest["ok"] is True
    assert nearest["hp"] == 5

    iec = size_motor_circuit(kw=7.5, volts=400, standard="IEC")
    assert iec["ok"] is False
    assert iec["flc_a"] is None
    assert "not" in iec["report"].lower()
    assert "invented" in iec["report"].lower() or "not used" in iec["report"].lower()
    named = size_motor_circuit(kw=7.5, volts=400, standard="IEC", fla=14)
    assert named["ok"] is True
    assert named["flc_a"] is None
    assert named["nameplate_fla_a"] == 14
    assert "wire" not in named

    hot = size_motor_circuit(hp=5, volts=460, temp_c=90)
    assert hot["ok"] is False


def test_dol_5hp_460_sizes_wire_breaker_and_overload():
    session = Session.create("5 hp DOL")
    result = session.insert_circuit(kind="dol", hp=5, voltage=460)
    assert _lib(result, "breaker") == "nema.breakers.mcb_3p_32a"
    assert _lib(result, "contactor") == "nema.contactors.ac3_9a"
    sheet = session.sheet(result["sheet"])
    breaker = sheet.component(_id(result, "breaker"))
    motor = sheet.component(_id(result, "motor"))
    overload = sheet.component(_id(result, "overload"))
    assert breaker.properties["In"] == "20A"
    assert breaker.properties["sized_by"] == "nec-430"
    assert motor.properties["FLA"] == 7.6
    assert motor.properties["wire_gauge"] == "14 AWG"
    assert motor.properties["conductor_min_a"] == 9.5
    assert overload.properties.get("range") in (None, "")
    assert "amps_min" not in overload.properties
    assert "7.6-7.6" not in str(overload.properties)
    assert "7.6" in str(overload.properties.get("setting"))
    assert overload.properties.get("suggested_part") == "Allen-Bradley 193-T1AC10"
    note = str(overload.properties.get("ol_library_note"))
    assert "193-T1AC10" in note
    assert "Fits 100-C09…100-C23." in note
    assert " only." not in note
    from panelcad.core.pins import rating_captions

    assert "7.6-7.6" not in " ".join(rating_captions(overload))
    assert any("7.6" in line for line in rating_captions(overload))
    power = [wire for wire in sheet.wires if wire.gauge]
    assert power
    assert {wire.gauge for wire in power} == {"14 AWG"}
    assert all(wire.get("gauge") for wire in result["wires"])
    rows = _sizing_rows(session)
    assert {row["check"] for row in rows} == {"overload_needs_nameplate"}
    assert "430.32" in rows[0]["message"]
    assert "430.6" in rows[0]["message"]
    _assert_sheet_text_clear(session, result["sheet"])
    gauges = {row["gauge"] for row in session.wire_list_rows() if row.get("gauge")}
    assert "14 AWG" in gauges


def test_overload_fit_sentences_copy_published_families():
    """List and range fits stay in the publisher's words. 193-T1A cites 193-CT001."""

    from panelcad.core.sizing_checks import catalog_fit_sentence

    session = Session.create("families")
    expected = {
        "RTD32-400": "Fits GH15BN to GH15FT series contactors.",
        "RTD32-600": "Fits GH15BN to GH15FT series contactors.",
        "RTD32-900": "Fits GH15BN to GH15FT series contactors.",
        "RTD32-1400": "Fits GH15CN to GH15FT series contactors.",
        "RTD32-1800": "Fits GH15DN, GH15ET, or GH15FT series contactors.",
        "RTD32-2400": "Fits GH15ET or GH15FT series contactors.",
        "RTD32-3200": "Fits GH15ET or GH15FT 45 mm contactors.",
        "193-T1AC10": "Fits 100-C09…100-C23.",
        "193-T1AC16": "Fits 100-C09…100-C23.",
        "193-T1AB63": "Fits 100-C09…100-C23.",
    }
    doc = "https://literature.rockwellautomation.com/idc/groups/literature/documents/ct/193-ct001_-en-e.pdf"
    for catalog, sentence in expected.items():
        part = session.parts.get(catalog)
        assert catalog_fit_sentence(part) == sentence
        assert " only." not in sentence
    for catalog in ("193-T1AC10", "193-T1AC16", "193-T1AB63"):
        part = session.parts.get(catalog)
        assert "193-CT001" in part.notes
        assert doc in part.notes
        assert "100-C12" not in part.description
    ac10 = session.parts.get("193-T1AC10")
    assert ac10.source.endswith("193-T1AC10.html")
    assert session.parts.get("RTD32-3200").amps_min == 22
    assert session.parts.get("193-T1AC16").amps_min == 11.3
    assert session.parts.get("193-T1AC10").amps_min == 7.2


def test_reversing_10hp_230_then_deliberate_undersize():
    session = Session.create("10 hp reversing")
    result = session.insert_circuit(kind="reversing", control="local", hp=10, voltage=230)
    assert _lib(result, "fwd") == "nema.contactors.ac3_32a"
    assert _lib(result, "breaker") == "nema.breakers.mcb_3p_32a"
    power = session.sheet(result["sheet"])
    breaker = power.component(_id(result, "breaker"))
    overload = power.component(_id(result, "overload"))
    motor = power.component(_id(result, "motor"))
    assert breaker.properties["In"] == "70A"
    assert motor.properties["FLA"] == 28
    assert motor.properties["conductor_min_a"] == 35
    assert motor.properties["wire_gauge"] == "8 AWG"
    assert {wire.gauge for wire in power.wires} == {"8 AWG"}
    control = session.sheet(result["control_sheet"])
    assert control.wires
    assert {wire.gauge for wire in control.wires} == {"16 AWG"}
    assert overload.properties.get("range") in (None, "")
    assert "28-28" not in str(overload.properties)
    assert "28" in str(overload.properties.get("setting"))
    assert overload.properties.get("suggested_part") == "AutomationDirect RTD32-3200"
    ol_note = str(overload.properties.get("ol_library_note"))
    assert "Fits GH15ET or GH15FT 45 mm contactors." in ol_note
    assert " only." not in ol_note
    assert "23.0-32.0" in ol_note
    assert "22.0-32.0" in ol_note
    assert "2026-10-08" in ol_note
    assert breaker.properties["frame_a"] == 32
    assert breaker.properties["suggested_library_id"] == "nema.breakers.mccb_3p_100a"
    initial = _sizing_rows(session)
    assert {row["check"] for row in initial} == {"overload_needs_nameplate", "breaker_frame_below_rating"}
    frame = next(row for row in initial if row["check"] == "breaker_frame_below_rating")
    assert frame["severity"] == "error"
    assert "mccb_3p_100a" in frame["message"]
    assert "No catalog breaker covers 70 A" in frame["message"]

    power.wires[0].gauge = "14 AWG"
    breaker.properties["In"] = "100A"
    overload.properties["range"] = "1.6-10A"
    overload.properties["amps_min"] = 1.6
    overload.properties["amps_max"] = 10
    found = {row["check"] for row in _sizing_rows(session)}
    assert found == {
        "wire_undersized",
        "protection_above_430_52",
        "overload_range_misses_fla",
        "overload_needs_nameplate",
        "breaker_frame_below_rating",
    }
    assert "contactor_undersized" not in found
    messages = " ".join(row["message"] for row in _sizing_rows(session))
    assert "14 AWG" in messages
    assert "35" in messages
    assert "100" in messages
    assert "70" in messages
    assert "28" in messages


def test_50hp_460_frame_contactor_and_overload_setpoint():
    """50 hp places a 100 A frame at 175 A and a 32 A contactor on a 65 A load."""

    session = Session.create("50 hp DOL")
    result = session.insert_circuit(kind="dol", hp=50, voltage=460)
    assert _lib(result, "breaker") == "nema.breakers.mccb_3p_100a"
    assert _lib(result, "contactor") == "nema.contactors.ac3_32a"
    sheet = session.sheet(result["sheet"])
    breaker = sheet.component(_id(result, "breaker"))
    contactor = sheet.component(_id(result, "contactor"))
    overload = sheet.component(_id(result, "overload"))
    motor = sheet.component(_id(result, "motor"))
    assert breaker.properties["In"] == "175A"
    assert breaker.properties["frame_a"] == 100
    assert "suggested_library_id" not in breaker.properties
    assert "No larger builtin breaker frame covers 175 A" in breaker.properties["frame_note"]
    assert "No catalog breaker covers 175 A" in breaker.properties["frame_note"]
    assert motor.properties["FLA"] == 65
    assert motor.properties["wire_gauge"] == "4 AWG"
    assert motor.properties["conductor_min_a"] == 81.25
    assert "SC-E3-110VAC" in str(motor.properties.get("contactor_note"))
    assert contactor.properties["suggested_part"] == "AutomationDirect SC-E3-110VAC"
    assert "65" in contactor.properties["contactor_note"]
    assert overload.properties.get("range") in (None, "")
    assert "65-65" not in str(overload.properties)
    assert "65" in str(overload.properties.get("setting"))
    assert "No published overload range includes 65 A" in str(overload.properties.get("ol_library_note"))
    rows = _sizing_rows(session)
    found = {row["check"] for row in rows}
    assert found == {
        "overload_needs_nameplate",
        "breaker_frame_below_rating",
        "contactor_undersized",
        "overload_range_misses_fla",
    }
    assert next(row for row in rows if row["check"] == "breaker_frame_below_rating")["severity"] == "error"
    contactor_row = next(row for row in rows if row["check"] == "contactor_undersized")
    assert contactor_row["severity"] == "error"
    assert "SC-E3-110VAC" in contactor_row["message"]
    assert "65" in next(row["message"] for row in rows if row["check"] == "overload_range_misses_fla")
    sized = size_motor_circuit(hp=50, volts=460)
    assert "contactor_undersized" in sized["report"]
    assert "breaker_frame_below_rating" in sized["report"]

    contactor.properties["part_number"] = "SC-E3-110VAC"
    contactor.properties["Ie"] = "65A"
    cleared = {row["check"] for row in _sizing_rows(session)}
    assert "contactor_undersized" not in cleared


def test_six_lead_motor_leads_meet_the_circle():
    """Main's six-lead symbol lands the end leads on the right of the circle.

    The old bottom pins were two verticals at x=8. The merged symbol draws
    T6/T4/T5 (and W2/U2/V2) as horizontals that meet the circle, and the
    top leads as stubs that meet the upper arc.
    """

    from math import hypot

    session = Session.create("6-lead")
    for library_id in ("nema.loads.motor_3ph_6lead", "iec.loads.motor_3ph_6lead"):
        device = session.catalog.get(library_id)
        circle = next(item for item in device.primitives if item.type == "circle")
        lines = []
        for item in device.primitives:
            if item.type == "line":
                lines.append(((item.x, item.y), (item.x2, item.y2)))
            elif item.type == "polyline":
                pts = [(float(x), float(y)) for x, y in item.points]
                lines.extend(zip(pts, pts[1:]))
        assert len(device.pins) == 6, library_id
        for pin in device.pins:
            seen = {(round(pin.x, 2), round(pin.y, 2))}
            stack = [(pin.x, pin.y)]
            reached = None
            while stack:
                x, y = stack.pop()
                if abs(hypot(x - circle.x, y - circle.y) - circle.r) <= 0.15:
                    reached = (x, y)
                    break
                for start, end in lines:
                    for src, dst in ((start, end), (end, start)):
                        if hypot(src[0] - x, src[1] - y) > 0.15:
                            continue
                        key = (round(dst[0], 2), round(dst[1], 2))
                        if key in seen:
                            continue
                        seen.add(key)
                        stack.append(dst)
            assert reached is not None, (library_id, pin.id)
            if pin.id in {"U1", "W1", "T1", "T3", "V1", "T2"}:
                assert reached[1] < circle.y - 4.0, (library_id, pin.id, reached)


def test_nameplate_and_star_delta_winding_current():
    session = Session.create("Nameplate")
    result = session.insert_circuit(kind="dol", hp=5, voltage=460, fla=8)
    motor = session.sheet(result["sheet"]).component(_id(result, "motor"))
    overload = session.sheet(result["sheet"]).component(_id(result, "overload"))
    assert motor.properties["FLA"] == 8
    assert motor.properties["table_flc"] == 7.6
    assert motor.properties["conductor_min_a"] == 9.5
    assert "8" in str(overload.properties["setting"])
    assert session.sheet(result["sheet"]).component(_id(result, "breaker")).properties["In"] == "20A"

    delta = Session.create("Delta")
    built = delta.insert_circuit(kind="star_delta", hp=15, voltage=460)
    ol = delta.sheet(built["sheet"]).component(_id(built, "overload"))
    assert ol.properties["ol_setpoint"] == pytest.approx(12.18, rel=0.02)
    assert float(ol.properties["ol_fla"]) == pytest.approx(12.18, rel=0.02)
    assert {row["check"] for row in _sizing_rows(delta)} == {
        "overload_needs_nameplate",
        "breaker_frame_below_rating",
    }
    named_rows = _sizing_rows(session)
    assert named_rows == []


def test_iec_insert_does_not_invent_fla():
    session = Session.create("IEC")
    session.set_title_block(standard="IEC")
    result = session.insert_circuit(kind="dol", kw=7.5)
    motor = session.sheet(result["sheet"]).component(_id(result, "motor"))
    assert motor.properties.get("kW") == 7.5
    assert not motor.properties.get("FLA")
    assert result["sizing"]["code"] == "IEC"
    assert result["sizing"]["ok"] is False
    breaker = session.sheet(result["sheet"]).component(_id(result, "breaker"))
    assert breaker.properties.get("sized_by") != "nec-430"
    assert _lib(result, "breaker") == "iec.breakers.mcb_3p_32a"
    assert _lib(result, "contactor") == "iec.contactors.ac3_32a"


def test_single_phase_report_names_the_phase_once():
    """CLI, the sizing panel, and the agent all print the same report text."""

    from PySide6.QtWidgets import QApplication
    from typer.testing import CliRunner

    from panelcad.cli.main import app
    from panelcad.gui.sizing_panel import SizingPanel

    sized = size_motor_circuit(hp=1, volts=115, phases=1)
    assert sized["ok"] is True
    report = sized["report"]
    assert "1 hp, 115 V, single-phase." in report
    assert "single-phase, single-phase" not in report
    three = size_motor_circuit(hp=5, volts=460, phases=3)
    assert "3-phase, squirrel-cage" in three["report"]

    runner = CliRunner()
    done = runner.invoke(app, ["electrical", "size-motor", "--hp", "1", "--volts", "115", "--phases", "1"])
    assert done.exit_code == 0, done.output
    assert "1 hp, 115 V, single-phase." in done.output
    assert "single-phase, single-phase" not in done.output

    agent = Agent()
    replied = agent.handle({"op": "size_motor_circuit", "hp": 1, "volts": 115, "phases": 1})
    assert replied["ok"] is True
    agent_text = replied["result"]["report"]
    assert "1 hp, 115 V, single-phase." in agent_text
    assert "single-phase, single-phase" not in agent_text

    app_qt = QApplication.instance() or QApplication([])
    panel = SizingPanel()
    panel.hp.setText("1")
    panel.volts.setText("115")
    panel.phases.setCurrentIndex(panel.phases.findData(1))
    panel.calculate()
    shown = panel.report.toPlainText()
    assert "1 hp, 115 V, single-phase." in shown
    assert "single-phase, single-phase" not in shown
    panel.close()
    panel.deleteLater()
    app_qt.processEvents()


def test_cli_size_motor():
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    runner = CliRunner()
    done = runner.invoke(app, ["electrical", "size-motor", "--hp", "5", "--volts", "460"])
    assert done.exit_code == 0, done.output
    assert "7.6" in done.output
    assert "14 AWG" in done.output
    assert "20" in done.output
    assert "430.250" in done.output
    missing = runner.invoke(app, ["electrical", "size-motor", "--hp", "5", "--volts", "400"])
    assert missing.exit_code == 1


def test_agent_size_and_check():
    agent = Agent()
    sized = agent.handle({"op": "size_motor_circuit", "hp": 5, "volts": 460})
    assert sized["ok"] is True
    assert sized["result"]["flc_a"] == 7.6
    assert sized["result"]["wire"]["gauge"] == "14 AWG"
    help_result = agent.handle({"op": "help"})
    assert "size_motor_circuit" in help_result["result"]["ops"]
    assert "check_sizing" in help_result["result"]["schemas"]

    session = Session.create("Agent check")
    session.insert_circuit(kind="dol", hp=5, voltage=460)
    session.insert_circuit(kind="dol", hp=10, voltage=460, x=120, y=22)
    agent.session = session
    report = agent.handle({"op": "check_sizing"})
    assert report["ok"] is True
    assert report["result"]["count"] == 3
    assert {row["check"] for row in report["result"]["findings"]} == {
        "overload_needs_nameplate",
        "breaker_frame_below_rating",
    }
    feeder = report["result"]["feeder"]
    assert feeder is not None
    assert feeder["required_a"] == pytest.approx(25.1)


def test_drawing_agent_exposes_sizing_tools():
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from panelcad.ai.drawing import drawing_agent, open_drawing

    seen: list[str] = []

    def model(messages, info: AgentInfo) -> ModelResponse:
        seen.extend(tool.name for tool in info.function_tools)
        return ModelResponse(parts=[TextPart(content="ready")])

    drawing = open_drawing(name="Sizing tools")
    with drawing_agent.override(model=FunctionModel(model)):
        result = drawing_agent.run_sync("Which sizing tools exist?", deps=drawing)
    assert result.output == "ready"
    assert "size_motor_circuit" in seen
    assert "check_sizing" in seen

    steps = [
        ModelResponse(
            parts=[ToolCallPart(tool_name="size_motor_circuit", args={"hp": 5, "volts": 460}, tool_call_id="s1")]
        ),
        ModelResponse(parts=[TextPart(content="Sized 5 hp.")]),
    ]

    def caller(messages, info: AgentInfo) -> ModelResponse:
        return steps.pop(0)

    with drawing_agent.override(model=FunctionModel(caller)):
        ran = drawing_agent.run_sync("Size a 5 hp 460 V motor.", deps=drawing)
    assert "Sized" in ran.output


def test_gui_sizing_panel_shows_the_math():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    session = Session.create("Sizing panel")
    session.insert_circuit(kind="dol", hp=5, voltage=460)
    window = MainWindow(session)
    window.resize(1400, 900)
    window.show()
    app.processEvents()
    window.show_motor_sizing()
    app.processEvents()
    text = window.sizing_panel.report.toPlainText()
    assert "7.6" in text
    assert "430.250" in text
    assert "14 AWG" in text
    assert "430.22" in text
    assert "29.6" in text
    assert "28.1" in text
    names = [action.text().replace("&", "") for action in window.menuBar().actions()]
    assert "Tools" in names
    window.close()
    window.deleteLater()
    app.processEvents()


def test_sizing_artifacts(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.main_window import MainWindow
    from panelcad.sharpen.from_print import render_pdf_page

    art = _artifact_dir(tmp_path)
    (art / "citations.md").write_text(citation_markdown(), encoding="utf-8")
    cites = (art / "citations.md").read_text(encoding="utf-8")
    assert "430.250" in cites
    assert "2.4" in cites
    assert "not verified" in cites
    for banned in (
        "JobVolt",
        "VoltageLab",
        "ElectricalHelper",
        "NECMastery",
        "NORDIX",
        "EE Power",
        "myelectriccalculator",
        "HELUKABEL",
        "WireSizing",
        "Tekima",
        "VRIELECTRICAL",
        "GT Engineering",
        "Eaton Bussmann",
        "350 A versus 355",
    ):
        assert banned not in cites, banned

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont, QFontMetrics
    from PySide6.QtWidgets import QHeaderView

    app = QApplication.instance() or QApplication([])
    # Earlier tests in this process can leave a doubled application face.
    # The column check uses this font, then sizes the column from it.
    face = QFont("DejaVu Sans")
    face.setBold(False)
    face.setPixelSize(-1)
    face.setPointSize(10)
    app.setFont(face)
    session = Session.create("Sizing artifacts")
    result = session.insert_circuit(kind="dol", hp=5, voltage=460)
    window = MainWindow(session)
    window.resize(1500, 980)
    window.show()
    app.processEvents()
    window.show_motor_sizing()
    window.sizing_wrap.resize(720, 640)
    app.processEvents()
    panel_png = art / "sizing-panel.png"
    _grab(window.sizing_wrap, panel_png)
    assert panel_png.stat().st_size > 1000

    pdf = art / "schematic.pdf"
    session.export_pdf(pdf, sheet_id=result["sheet"])
    schematic = art / "schematic.png"
    render_pdf_page(pdf, 1, schematic, dpi=140)
    assert schematic.stat().st_size > 1000

    jogged = Session.create("50 hp proof")
    jogged_result = jogged.insert_circuit(kind="dol", hp=50, voltage=460)
    jogged_pdf = art / "dol-50hp.pdf"
    jogged.export_pdf(jogged_pdf, sheet_id=jogged_result["sheet"])
    jogged_png = art / "dol-50hp.png"
    render_pdf_page(jogged_pdf, 1, jogged_png, dpi=140)
    assert jogged_png.stat().st_size > 1000

    bad = Session.create("Undersize")
    built = bad.insert_circuit(kind="reversing", control="local", hp=10, voltage=230)
    power = bad.sheet(built["sheet"])
    power.wires[0].gauge = "14 AWG"
    power.component(_id(built, "breaker")).properties["In"] = "100A"
    power.component(_id(built, "overload")).properties["range"] = "1.6-10A"
    problems = MainWindow(bad)
    problems.problems.setFont(face)
    problems.problems.table.setFont(face)
    problems.resize(2400, 980)
    problems.show()
    app.processEvents()
    rows = [row for row in bad.design_checks() if row["check"] in SIZING_CHECKS]
    assert {row["check"] for row in rows} == {
        "wire_undersized",
        "protection_above_430_52",
        "overload_range_misses_fla",
        "overload_needs_nameplate",
        "breaker_frame_below_rating",
    }
    # The live checker would replace this filtered list before the grab.
    problems._check_runner.rows_ready.disconnect(problems._on_checks)
    problems.problems.set_rows(rows)
    problems.problems_wrap.setFloating(True)
    # The overload suggestion names the contactor family and the RTD32 range
    # conflict, so the message column needs a wider dock to show that sentence.
    problems.problems_wrap.setMinimumSize(2200, 480)
    problems.problems_wrap.resize(2200, 520)
    problems.problems_wrap.show()
    problems.problems_wrap.raise_()
    app.processEvents()
    problems.problems.setFont(face)
    problems.problems.table.setFont(face)
    problems.problems.set_rows(rows)
    problems.problems._refit_rows()
    table = problems.problems.table
    header = table.horizontalHeader()
    header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
    table.resizeColumnsToContents()
    app.processEvents()
    check_item = table.item(0, 1)
    assert check_item is not None
    metrics = QFontMetrics(table.font())
    longest = max((row["check"] for row in rows), key=len)
    advance = metrics.horizontalAdvance(longest)
    assert table.columnWidth(1) >= advance
    message = max((row["message"] for row in rows), key=len)
    msg_advance = metrics.horizontalAdvance(message)
    assert table.columnWidth(2) >= msg_advance
    header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
    nameplate_text = next(row["message"] for row in rows if row["check"] == "overload_needs_nameplate")
    assert "430.32 require the nameplate" in nameplate_text
    check_png = art / "design-checks.png"
    _grab(problems.problems_wrap, check_png)
    assert check_png.stat().st_size > 1000
    # A dock narrower than the sentence still shows it: the row grows and the line wraps.
    problems.problems_wrap.setMinimumSize(0, 0)
    problems.problems_wrap.resize(980, 420)
    app.processEvents()
    problems.problems._refit_rows()
    nameplate = next(row for row in rows if row["check"] == "overload_needs_nameplate")
    nameplate_advance = QFontMetrics(problems.problems.table.font()).horizontalAdvance(nameplate["message"])
    assert problems.problems.table.columnWidth(2) < nameplate_advance
    line = QFontMetrics(problems.problems.table.font()).height()
    nameplate_row = next(
        i
        for i in range(problems.problems.table.rowCount())
        if problems.problems.table.item(i, 1)
        and (problems.problems.table.item(i, 1).data(Qt.ItemDataRole.UserRole) or {}).get("check")
        == "overload_needs_nameplate"
    )
    assert problems.problems.table.rowHeight(nameplate_row) >= line * 2
    window.close()
    problems.close()
    window.deleteLater()
    problems.deleteLater()
    app.processEvents()


def _grab(widget, path: Path) -> None:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication

    widget.resize(max(widget.width(), 1100), max(widget.height(), 480))
    app = QApplication.instance()
    if app is not None:
        app.processEvents()
    image = QImage(widget.size(), QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    try:
        widget.render(painter, QPoint(0, 0))
    finally:
        painter.end()
    if not image.save(str(path), "PNG"):
        raise RuntimeError(f"failed to write {path}")
