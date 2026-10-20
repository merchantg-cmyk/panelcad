"""Enclosure thermal report: IEC 60890 / Rittal surface, maker watts, cooling."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from panelcad import Session
from panelcad.ai.drawing import drawing_agent, open_drawing
from panelcad.cli.agent import Agent
from panelcad.cli.main import app
from panelcad.core.enclosure import WATT_KEYS, device_heat
from panelcad.core.enclosure import options_from_defaults
from panelcad.core.thermal import (
    CFM_PER_M3H,
    HEADROOM_CAUTION_K,
    air_factor,
    citation_markdown,
    compute_heat_load,
    cooling_target,
    effective_area_m2,
    format_margin,
    material_k,
    protection_class,
)
from panelcad.library.enclosures import load_enclosures


def _response(tool_name: str, args: dict, call_id: str) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args, tool_call_id=call_id)])


def vfd_panel() -> Session:
    s = Session.create("VFD panel")
    s.set_title_block(company="George's Shop", drawing_number="E-VFD", standard="NEMA/ANSI", rev="A")
    s.place("adc.durapulse.gs21_20p5", ref="U1", x=40, y=30)
    s.place("adc.click.c2_03cpu", ref="A1", x=140, y=30)
    s.place("adc.click.c0_16nd3", ref="A2", x=240, y=30)
    s.place("adc.click.c0_08td1", ref="A3", x=320, y=30)
    s.place("iec.power.psu_24v_5a", ref="G1", x=40, y=140)
    s.assign_part("G1", "1606-XLE120E")
    s.place("iec.breakers.mcb_3p_32a", ref="Q1", x=140, y=140)
    s.place("iec.contactors.ac3_9a", ref="K1", x=220, y=140)
    s.add_sheet("panel", title="Enclosure", kind="panel_layout")
    s.set_enclosure(
        width_mm=400,
        height_mm=500,
        depth_mm=200,
        name="400×500×200",
        nema_type="12",
        material="steel",
        mount="wall",
        sheet_id="panel",
    )
    s.set_ambient(ambient_c=40, max_internal_c=50)
    return s


def small_dol() -> Session:
    s = Session.create("Small DOL")
    s.set_title_block(company="George's Shop", drawing_number="E-DOL", standard="NEMA/ANSI", rev="A")
    s.place("iec.breakers.mcb_3p_32a", ref="Q1", x=40, y=24)
    s.place("iec.contactors.ac3_9a", ref="K1", x=40, y=90)
    s.place("iec.protection.overload_9a", ref="F1", x=40, y=160)
    s.place("iec.loads.motor_3ph", ref="M1", x=40, y=220)
    s.place("iec.power.psu_24v_5a", ref="G1", x=160, y=30)
    s.place("iec.plc.compact_16dio", ref="A1", x=160, y=100)
    s.place("iec.operators.pilot_light", ref="H1", x=280, y=30)
    s.add_sheet("panel", title="Enclosure", kind="panel_layout")
    s.set_enclosure(
        width_mm=300,
        height_mm=400,
        depth_mm=200,
        name="300×400×200",
        nema_type="12",
        material="steel",
        mount="wall",
        sheet_id="panel",
    )
    s.set_ambient(ambient_c=25, max_internal_c=50)
    return s


def test_rittal_wall_example_and_material_k():
    area, mount, formula = effective_area_m2(800, 2000, 600, "wall")
    assert mount == "wall"
    assert area == pytest.approx(5.072, abs=0.0001)
    assert "1.4" in formula and "1.8" in formula
    assert material_k("steel")[0] == 5.5
    assert material_k("stainless")[0] == 4.5
    assert material_k("aluminum")[0] == 12.0
    assert material_k("aluminum double wall")[0] == 4.5
    assert material_k("polyester")[0] == 3.5
    assert air_factor(0) == 3.1
    assert air_factor(100) == 3.1
    assert air_factor(200) == 3.2
    assert air_factor(400) == 3.3
    assert air_factor(600) == 3.4
    assert air_factor(900) == 3.5
    assert air_factor(1200) == 3.8
    assert 3.1 * CFM_PER_M3H == pytest.approx(1.82, abs=0.01)


def test_vfd_panel_uses_maker_watts_and_labels_estimates():
    s = vfd_panel()
    report = s.compute_heat_load()
    by_ref = {row["ref"]: row for row in report["devices"]}
    assert set(by_ref) == {"U1", "A1", "A2", "A3", "G1", "Q1", "K1"}
    assert by_ref["U1"]["watts"] == pytest.approx(30.8)
    assert by_ref["U1"]["basis"] == "published"
    assert by_ref["U1"]["source_label"] == "Published"
    assert "gs20drives.pdf" in by_ref["U1"]["citation"]
    assert by_ref["U1"]["max_ambient_c"] == pytest.approx(50)
    assert by_ref["A1"]["watts"] == pytest.approx(3.12)
    assert by_ref["A2"]["watts"] == pytest.approx(0.96)
    assert by_ref["A3"]["watts"] == pytest.approx(1.56)
    assert by_ref["G1"]["watts"] == pytest.approx(7.3)
    assert by_ref["G1"]["basis"] == "published"
    assert "1606-XLE120E" in by_ref["G1"]["citation"]
    for ref in ("Q1", "K1"):
        assert by_ref[ref]["basis"] == "estimate"
        assert by_ref[ref]["source_label"] == "ESTIMATE"
        assert "Not manufacturer data" in by_ref[ref]["note"]
        assert by_ref[ref]["watts"] == pytest.approx(4.0)
    assert report["watts_published"] == pytest.approx(43.74)
    assert report["watts_estimate"] == pytest.approx(8.0)
    assert report["watts_total"] == pytest.approx(51.74)
    assert report["surface_m2"] == pytest.approx(0.572, abs=0.0001)
    assert report["k_w_m2k"] == 5.5
    assert report["rise_k"] == pytest.approx(51.74 / (5.5 * 0.572), abs=0.02)
    assert report["recommendation"]["kind"] == "filter_fan"
    assert report["recommendation"]["airflow_m3h"] == pytest.approx(3.1 * 51.74 / 10.0, abs=0.05)
    assert report["recommendation"]["airflow_cfm"] == pytest.approx(
        report["recommendation"]["airflow_m3h"] * CFM_PER_M3H, abs=0.05
    )
    assert report["recommendation"]["cooling_w"] > 0
    assert report["recommendation"]["cooling_btu_h"] == pytest.approx(
        report["recommendation"]["cooling_w"] * 3.412141633, abs=1.5
    )
    assert "ESTIMATE" not in by_ref["U1"]["source_label"]
    assert "m³/h" in report["recommendation"]["summary"] or "m3/h" in report["recommendation"]["summary"]
    assert "Type 12" in report["recommendation"]["summary"]
    assert "IP54" in report["recommendation"]["summary"]
    assert report["protection"] == "filter_rated"
    assert report["recommendation"]["nema4_fan"] is False
    assert by_ref["U1"]["margin_k"] == pytest.approx(0.0)
    assert format_margin(by_ref["U1"]["margin_k"]) == "+0.0 K"
    assert by_ref["A1"]["margin_k"] == pytest.approx(5.0)
    assert by_ref["Q1"]["margin_k"] is None
    enc = s.project.sheet("panel").enclosure
    assert (enc.width_mm, enc.height_mm, enc.depth_mm) == (400.0, 500.0, 200.0)


def test_user_estimate_replaces_published_and_stays_labeled():
    s = vfd_panel()
    s.set_watt_estimate("U1", 12)
    row = next(item for item in s.compute_heat_load()["devices"] if item["ref"] == "U1")
    assert row["watts"] == pytest.approx(12)
    assert row["basis"] == "estimate"
    assert row["source_label"] == "ESTIMATE"
    assert "User estimate" in row["note"]
    assert "manufacturer" in row["note"].lower()


def test_small_dol_estimates_and_natural_cooling_skip_the_motor():
    s = small_dol()
    report = s.compute_heat_load()
    refs = {row["ref"] for row in report["devices"]}
    assert "M1" not in refs
    assert "H1" in refs
    by_ref = {row["ref"]: row for row in report["devices"]}
    assert by_ref["G1"]["basis"] == "estimate"
    assert "88%" in by_ref["G1"]["note"] or "efficiency" in by_ref["G1"]["note"].lower()
    assert by_ref["G1"]["watts"] == pytest.approx(120 * (1 / 0.88 - 1), abs=0.02)
    assert by_ref["A1"]["watts"] == pytest.approx(5.0)
    assert by_ref["H1"]["watts"] == pytest.approx(1.0)
    assert report["watts_published"] == pytest.approx(0)
    assert report["recommendation"]["kind"] == "none"
    summary = report["recommendation"]["summary"]
    assert summary.startswith(
        "Natural convection is enough: rise 14.9 K within the 15.0 K allowance "
        "set by A1 (40 °C user rating); margin +0.1 K."
    )
    assert "25.0 K" not in summary
    assert "at the limit" in summary
    assert "small filter fan" in summary
    assert "larger enclosure" in summary
    assert report["recommendation"]["airflow_cfm"] is None
    assert report["design_c"] == pytest.approx(39.86, abs=0.02)
    assert by_ref["A1"]["max_ambient_c"] == pytest.approx(40)
    assert by_ref["A1"]["max_ambient_basis"] == "user"
    assert by_ref["A1"]["margin_k"] == pytest.approx(0.1)
    assert format_margin(by_ref["A1"]["margin_k"]) == "+0.1 K"
    assert by_ref["Q1"]["margin_k"] is None
    assert by_ref["G1"]["margin_k"] is None
    assert not any(item["check"] == "above_max_ambient" for item in report["findings"])


def test_air_conditioner_when_ambient_is_too_hot_and_heater_when_min_is_set():
    s = vfd_panel()
    hot = s.compute_heat_load(ambient_c=55, max_internal_c=50)
    assert hot["recommendation"]["kind"] == "air_conditioner"
    assert hot["recommendation"]["airflow_m3h"] is None
    assert hot["recommendation"]["cooling_w"] >= hot["watts_total"]
    assert hot["recommendation"]["cooling_btu_h"] > 0
    assert "air conditioner" in hot["recommendation"]["summary"].lower()
    cold = s.compute_heat_load(min_internal_c=10, min_ambient_c=-10, outdoor=True)
    area, _, _ = effective_area_m2(400, 500, 200, "wall")
    assert cold["recommendation"]["heater_w"] == pytest.approx(5.5 * area * 20 * 2, abs=0.2)
    stored = s.project.properties["thermal"]["ambient_c"]
    assert stored == pytest.approx(40)
    assert s.project.sheet("panel").enclosure.width_mm == 400


def test_nema4_fan_and_above_max_ambient_checks():
    s = vfd_panel()
    sealed = s.compute_heat_load(nema="4")
    assert sealed["recommendation"]["kind"] == "closed_loop"
    assert sealed["recommendation"]["airflow_m3h"] is None
    assert sealed["recommendation"]["nema4_fan"] is True
    assert sealed["recommendation"]["heat_exchanger_w_per_k"] == pytest.approx(2.03, abs=0.05)
    assert sealed["recommendation"]["cooling_w"] == pytest.approx(20.3, abs=0.3)
    assert "20.3 W" in sealed["recommendation"]["summary"]
    fan_finding = next(item for item in sealed["findings"] if item["check"] == "nema4_filter_fan")
    assert "20.3 W" in fan_finding["message"]
    assert any(item["check"] == "nema4_filter_fan" for item in sealed["findings"])
    assert "NEMA 4" in sealed["recommendation"]["summary"]
    assert "Closed-loop" in sealed["recommendation"]["summary"]
    assert "filter fan" not in sealed["recommendation"]["summary"].lower().split("standard")[0]
    loose = s.compute_heat_load(max_internal_c=60)
    assert loose["internal_c"] > 50
    assert loose["cooling_limit_c"] == pytest.approx(50)
    assert loose["recommendation"]["kind"] == "filter_fan"
    assert "Natural convection" not in loose["recommendation"]["summary"]
    assert "limits the panel" in loose["recommendation"]["summary"]
    assert loose["design_c"] == pytest.approx(50)
    assert all(row.get("margin_k") is None or row["margin_k"] >= 0 for row in loose["devices"])
    assert not any(item["check"] == "above_max_ambient" for item in loose["findings"])
    held = [row for row in s.design_checks() if row["check"] == "above_max_ambient"]
    assert held == []
    s.set_enclosure(nema_type="4X", sheet_id="panel")
    live = [row["check"] for row in s.design_checks()]
    assert "nema4_filter_fan" in live
    assert "above_max_ambient" not in live


def test_part_rating_lowers_the_cooling_target_before_natural_convection():
    s = vfd_panel()
    report = s.compute_heat_load(max_internal_c=60)
    assert report["max_internal_c"] == pytest.approx(60)
    assert report["cooling_governed_by"] == "part"
    assert report["cooling_limit_refs"] == ["U1"]
    assert report["cooling_limit_c"] == pytest.approx(50)
    assert report["internal_c"] == pytest.approx(56.45, abs=0.05)
    assert report["design_c"] == pytest.approx(50)
    assert report["recommendation"]["kind"] == "filter_fan"
    assert report["recommendation"]["airflow_m3h"] == pytest.approx(3.1 * 51.74 / 10.0, abs=0.05)
    summary = report["recommendation"]["summary"]
    assert summary.startswith("Cooling needed:")
    assert "GS21-20P5 rated 50 °C limits the panel" in summary
    assert "filter fan" in summary.lower()
    assert "m³/h" in summary
    assert "Natural convection" not in summary
    by_ref = {row["ref"]: row for row in report["devices"]}
    assert by_ref["U1"]["margin_k"] == pytest.approx(0.0)
    assert by_ref["A1"]["margin_k"] == pytest.approx(5.0)
    assert not any(item["check"] == "above_max_ambient" for item in report["findings"])

    sealed = s.compute_heat_load(max_internal_c=60, nema="4X")
    assert sealed["recommendation"]["kind"] == "closed_loop"
    assert sealed["recommendation"]["airflow_m3h"] is None
    assert "GS21-20P5 rated 50 °C limits the panel" in sealed["recommendation"]["summary"]
    assert "closed-loop" in sealed["recommendation"]["summary"].lower()
    assert sealed["design_c"] == pytest.approx(50)
    assert not any(item["check"] == "above_max_ambient" for item in sealed["findings"])

    dol = small_dol()
    quiet = dol.compute_heat_load()
    assert quiet["recommendation"]["kind"] == "none"
    assert "15.0 K allowance set by A1 (40 °C user rating)" in quiet["recommendation"]["summary"]
    roomy = dol.compute_heat_load(component_max_c=45)
    assert roomy["recommendation"]["kind"] == "none"
    assert roomy["cooling_governed_by"] == "part"
    roomy_summary = roomy["recommendation"]["summary"]
    assert "20.0 K allowance set by A1 (45 °C user rating)" in roomy_summary
    assert "+5.1 K" in roomy_summary
    assert "at the limit" not in roomy_summary
    assert "headroom" not in roomy_summary
    panel = dol.compute_heat_load(component_max_c=50)
    assert panel["cooling_governed_by"] == "user"
    assert "25.0 K allowance" in panel["recommendation"]["summary"]
    assert "at the limit" not in panel["recommendation"]["summary"]
    tight = dol.compute_heat_load(component_max_c=30)
    assert tight["cooling_limit_c"] == pytest.approx(30)
    assert tight["recommendation"]["kind"] == "filter_fan"
    assert "A1 user max ambient 30 °C limits the panel" in tight["recommendation"]["summary"]
    assert "Natural convection" not in tight["recommendation"]["summary"]
    a1 = next(row for row in tight["devices"] if row["ref"] == "A1")
    assert a1["margin_k"] >= 0

    bare = [{"ref": "U1", "catalog": "GS21-20P5", "max_ambient_c": 50, "max_ambient_basis": "published"}]
    assert cooling_target(bare, 60)["governed_by"] == "part"
    assert cooling_target(bare, 50)["governed_by"] == "user"


def test_enclosure_rating_selects_closed_loop_or_a_type_12_fan():
    assert protection_class("12") == "filter_rated"
    assert protection_class("IP54") == "filter_rated"
    assert protection_class("NEMA 4X") == "closed_loop"
    assert protection_class("4") == "closed_loop"
    assert protection_class("6") == "closed_loop"
    assert protection_class("6P") == "closed_loop"
    assert protection_class("IP66") == "closed_loop"
    assert protection_class("IP65") == "closed_loop"
    assert protection_class("1") == "open"
    assert protection_class("3R") == "open"
    assert protection_class("13") == "open"
    assert protection_class("IP55") == "open"

    s = vfd_panel()
    twelve = s.compute_heat_load(nema="12")
    assert twelve["recommendation"]["kind"] == "filter_fan"
    assert "Type 12" in twelve["recommendation"]["summary"]
    assert "IP54" in twelve["recommendation"]["summary"]
    assert twelve["recommendation"]["airflow_m3h"] == pytest.approx(3.1 * 51.74 / 10.0, abs=0.05)
    assert not any(item["check"] == "nema4_filter_fan" for item in twelve["findings"])

    ip54 = s.compute_heat_load(nema="IP54")
    assert ip54["recommendation"]["kind"] == "filter_fan"
    assert "Type 12" in ip54["recommendation"]["summary"]
    assert ip54["recommendation"]["airflow_m3h"] == twelve["recommendation"]["airflow_m3h"]

    for token in ("4X", "6", "IP66"):
        sealed = s.compute_heat_load(nema=token)
        assert sealed["recommendation"]["kind"] == "closed_loop", token
        assert sealed["recommendation"]["airflow_m3h"] is None
        assert sealed["recommendation"]["airflow_cfm"] is None
        assert sealed["recommendation"]["heat_exchanger_w_per_k"] == pytest.approx(2.03, abs=0.05)
        assert sealed["recommendation"]["cooling_w"] == pytest.approx(20.3, abs=0.3)
        assert any(item["check"] == "nema4_filter_fan" for item in sealed["findings"])
        assert "above_max_ambient" not in {item["check"] for item in sealed["findings"]}
        assert "Closed-loop" in sealed["recommendation"]["summary"]

    open_box = s.compute_heat_load(nema="1")
    assert open_box["recommendation"]["kind"] == "filter_fan"
    assert "Type 12" not in open_box["recommendation"]["summary"]
    assert open_box["recommendation"]["airflow_m3h"] == twelve["recommendation"]["airflow_m3h"]

    quiet = small_dol().compute_heat_load(nema="4X")
    assert quiet["recommendation"]["kind"] == "none"
    assert quiet["recommendation"]["nema4_fan"] is False
    assert not any(item["check"] == "nema4_filter_fan" for item in quiet["findings"])

    text = citation_markdown()
    assert "UL 50" in text
    assert "rittal.com" in text
    assert "Type 12" in text


def test_watt_loss_does_not_change_enclosure_sizing():
    assert "watt_loss" not in WATT_KEYS
    s = vfd_panel()
    options = options_from_defaults(load_enclosures().defaults)
    device = s.catalog.get("iec.drives.vfd_1k5")
    watts, source = device_heat(device, None, options)
    assert source == "vfd_estimate"
    assert watts is not None and watts > 30
    gs = s.catalog.get("adc.durapulse.gs21_20p5")
    maker, maker_source = device_heat(gs, None, options)
    assert maker_source != "library"
    assert maker != pytest.approx(30.8)
    before = s.recommend_enclosure()
    width = s.project.sheet("panel").enclosure.width_mm
    s.set_ambient(ambient_c=15, max_internal_c=35, min_internal_c=5)
    after = s.recommend_enclosure()
    assert after["enclosure"]["width_mm"] == before["enclosure"]["width_mm"]
    assert after["enclosure"]["height_mm"] == before["enclosure"]["height_mm"]
    assert after["enclosure"]["depth_mm"] == before["enclosure"]["depth_mm"]
    s.compute_heat_load()
    assert s.project.sheet("panel").enclosure.width_mm == width


def test_headroom_caution_is_a_shop_setting_and_watts_share_one_rounding(tmp_path: Path):
    assert HEADROOM_CAUTION_K == 2.0
    dol = small_dol()
    report = dol.compute_heat_load()
    assumptions = " ".join(report["assumptions"])
    assert f"{HEADROOM_CAUTION_K:g} K" in assumptions
    assert "shop setting" in assumptions
    assert "not a code value" in assumptions
    summary = report["recommendation"]["summary"]
    assert "margin +0.1 K" in summary
    assert summary.index("at the limit") > summary.index("15.0 K")

    vfd = vfd_panel().compute_heat_load()
    assert vfd["recommendation"]["kind"] == "filter_fan"
    assert "20.3 W" in vfd["recommendation"]["summary"]
    assert "at the limit" not in vfd["recommendation"]["summary"]
    assert "gives headroom" not in vfd["recommendation"]["summary"]

    sealed = vfd_panel()
    sealed.set_enclosure(nema_type="4X", sheet_id="panel")
    four = sealed.compute_heat_load()
    headline = four["recommendation"]["summary"]
    finding = next(item["message"] for item in four["findings"] if item["check"] == "nema4_filter_fan")
    assert "20.3 W" in headline
    assert "20.3 W" in finding
    assert "20 W" not in headline.replace("20.3", "")
    assert "at the limit" not in headline

    path = tmp_path / "dol.pcad.yaml"
    dol.save(path)
    from io import StringIO
    import json

    from panelcad.cli.agent import run_agent

    stdin = StringIO(
        json.dumps({"op": "open", "path": str(path), "id": "open"})
        + "\n"
        + json.dumps({"op": "compute_heat_load", "id": "heat"})
        + "\n"
    )
    stdout = StringIO()
    run_agent(stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    jsonl_summary = replies[-1]["result"]["recommendation"]["summary"]
    assert jsonl_summary == summary

    from typer.testing import CliRunner

    text = CliRunner().invoke(app, ["electrical", "thermal", str(path)])
    assert text.exit_code == 0, text.output
    assert summary in text.output

    drawing = open_drawing(path)
    steps = [
        _response("compute_heat_load", {}, "heat"),
        ModelResponse(parts=[TextPart(content="Reported the DOL heat.")]),
    ]

    def caller(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return steps.pop(0)

    with drawing_agent.override(model=FunctionModel(caller)):
        result = drawing_agent.run_sync("Compute the heat load.", deps=drawing)
    assert "Reported" in result.output
    blob = str(result.all_messages())
    assert "15.0 K allowance set by A1 (40 °C user rating)" in blob
    assert "margin +0.1 K" in blob
    assert "at the limit" in blob


def test_set_ambient_undo_and_agent_cli(tmp_path: Path):
    s = vfd_panel()
    path = tmp_path / "vfd.pcad.yaml"
    s.save(path)
    opened = Session.open(path)
    opened.set_ambient(ambient_c=33)
    assert opened.project.properties["thermal"]["ambient_c"] == pytest.approx(33)
    opened.undo()
    assert opened.project.properties["thermal"]["ambient_c"] == pytest.approx(40)
    agent = Agent()
    assert agent.handle({"op": "open", "path": str(path)})["ok"]
    heat = agent.handle({"op": "compute_heat_load", "id": "corr"})
    assert heat["ok"]
    assert heat["id"] == "corr"
    assert heat["result"]["watts_total"] == pytest.approx(51.74)
    assert heat["result"]["enclosure"]["width_mm"] == 400
    ambient = agent.handle({"op": "set_ambient", "ambient_c": 28, "max_internal_c": 45})
    assert ambient["ok"]
    assert agent.session.project.sheet("panel").enclosure.width_mm == 400
    assert agent.handle({"op": "undo"})["ok"]
    assert agent.session.project.properties["thermal"]["ambient_c"] == pytest.approx(40)
    help_ops = agent.handle({"op": "help"})["result"]["ops"]
    assert "compute_heat_load" in help_ops
    assert "set_ambient" in help_ops

    from typer.testing import CliRunner

    pdf = tmp_path / "thermal.pdf"
    result = CliRunner().invoke(app, ["electrical", "thermal", str(path), "--pdf", str(pdf), "--json"])
    assert result.exit_code == 0, result.output
    assert pdf.is_file() and pdf.stat().st_size > 2000
    assert "51.74" in result.output
    text = CliRunner().invoke(app, ["electrical", "thermal", str(path), "--ambient", "25"])
    assert text.exit_code == 0, text.output
    assert "ESTIMATE" in text.output
    assert "Q1" in text.output


def test_drawing_agent_thermal_tools_do_not_resize(tmp_path: Path):
    drawing = open_drawing(name="Thermal tools")
    drawing.operator.handle({"op": "create_project", "name": "Thermal tools"})
    session = drawing.operator.session
    assert session is not None
    session.place("adc.durapulse.gs21_20p5", ref="U1", x=40, y=30)
    session.add_sheet("panel", title="Enclosure", kind="panel_layout")
    session.set_enclosure(width_mm=400, height_mm=500, depth_mm=200, sheet_id="panel", nema_type="12", material="steel")
    tools: dict[str, str] = {}

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for tool in info.function_tools:
            tools[tool.name] = tool.description or ""
        return ModelResponse(parts=[TextPart(content="ready")])

    with drawing_agent.override(model=FunctionModel(model)):
        drawing_agent.run_sync("Which thermal tools do you have?", deps=drawing)
    assert "compute_heat_load" in tools
    assert "set_ambient" in tools
    assert "does not resize" in tools["compute_heat_load"].lower()
    assert "does not change the enclosure" in tools["set_ambient"].lower()

    steps = [
        _response("set_ambient", {"ambient_c": 30, "max_internal_c": 50}, "amb"),
        _response("compute_heat_load", {}, "heat"),
        ModelResponse(parts=[TextPart(content="Reported the heat load.")]),
    ]

    def caller(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return steps.pop(0)

    with drawing_agent.override(model=FunctionModel(caller)):
        result = drawing_agent.run_sync("Set the ambient and compute the heat.", deps=drawing)
    assert "Reported" in result.output
    assert session.project.sheet("panel").enclosure.width_mm == 400
    assert session.project.properties["thermal"]["ambient_c"] == pytest.approx(30)
    report = session.compute_heat_load()
    assert report["devices"][0]["ref"] == "U1"
    assert report["devices"][0]["watts"] == pytest.approx(30.8)


def test_citations_name_the_method_and_the_maker_rows(tmp_path: Path):
    text = citation_markdown()
    assert "IEC 60890" in text
    assert "5.5" in text
    assert "https://www.rittal.com/" in text
    assert "GS21-20P5" in text and "30.8" in text
    assert "ESTIMATE" in text
    assert "1606-XLE120E" in text
    from tests.artifacts import artifact_dir

    dest = artifact_dir(tmp_path, "thermal") / "citations.md"
    dest.write_text(text, encoding="utf-8")
    assert dest.is_file()


def test_compute_heat_load_matches_core_without_mutating():
    s = small_dol()
    snap = s.project.model_dump(mode="json")
    direct = compute_heat_load(s.project, s.catalog, s.parts)
    via = s.compute_heat_load()
    assert via["watts_total"] == direct["watts_total"]
    assert s.project.model_dump(mode="json") == snap
