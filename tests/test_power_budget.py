"""Control power budget: trace, checks, GUI, JSONL, drawing agent, and CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.cli.main import app
from panelcad.core.checks import CHECK_IDS, run_checks
from panelcad.examples.dol import build_dol
from panelcad.examples.power_budget import build_overloaded, build_plc_hoa
from panelcad.gui.canvas import FindingMarker
from panelcad.gui.main_window import MainWindow
from panelcad.library.parts import default_parts_catalog
from panelcad.render.print_package import extract_pdf_text, render_package_page_png


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _kinds(report: dict) -> dict[str, dict]:
    supply = report["supplies"][0]
    return {row["kind"]: row for row in supply["loads"]}


def test_check_ids_include_control_power():
    for name in (
        "control_power_margin",
        "control_power_inrush",
        "control_power_protection",
        "control_power_method",
    ):
        assert name in CHECK_IDS


def test_catalog_publishes_cited_consumption():
    parts = default_parts_catalog()
    psu = parts.get("1606-XLE120E")
    assert psu.output_trip is not None
    assert psu.output_trip.publication == "1606-SR027"
    assert psu.output_trip.pages == "17"
    load_c = next(row for row in psu.output_trip.load_end if row.curve == "C")
    supply_c = next(row for row in psu.output_trip.supply_end if row.curve == "C")
    assert load_c.max_amps == 3
    assert load_c.loop_mohm == 270
    assert supply_c.max_amps == 4
    bigger_trip = parts.get("1606-XLE240E").output_trip
    assert bigger_trip is not None and bigger_trip.publication == "1606-SR022"
    assert next(row for row in bigger_trip.load_end if row.curve == "C").max_amps == 4
    assert next(row for row in bigger_trip.supply_end if row.curve == "C").max_amps == 13
    assert psu.power_w == 120
    assert psu.current_a == 5
    assert psu.assignment_properties()["power_origin"] == "catalog"
    bigger = parts.get("1606-XLE240E")
    assert bigger.power_w == 240
    assert bigger.current_a == 10
    coil = parts.get("100-C09KJ10")
    assert coil.coil_va_inrush == 75
    assert coil.coil_va_sealed == 9.5
    assert coil.power_w == 2.7
    light = parts.get("800FD-P3N3R")
    assert light.current_a == 0.012
    cpu = parts.get("C2-03CPU")
    assert cpu.power_w == 3.12
    assert cpu.current_a == 0.130
    di = parts.get("C0-16ND3")
    assert di.current_a == 0.040
    xf = parts.get("9070T100D23")
    assert xf.va == 100
    assert xf.inrush_method == "schneider_9070_type_t"
    plain = parts.get("1489-M1C010")
    assert "power_origin" not in plain.assignment_properties()


def test_hoa_traces_loads_and_labels_estimates():
    session = build_plc_hoa()
    report = session.power_budget()
    assert len(report["supplies"]) == 1
    supply = report["supplies"][0]
    assert supply["kind"] == "dc"
    assert supply["rating_w"] == 120
    assert supply["margin"] == 0.8
    assert supply["ok_margin"] is True
    assert supply["steady_w"] < 96
    kinds = _kinds(report)
    for name in ("coil", "relay", "plc_cpu", "plc_io", "pilot", "hmi", "sensor", "fan"):
        assert name in kinds, sorted(kinds)
    assert kinds["plc_cpu"]["estimate"] is False
    assert kinds["plc_cpu"]["steady_w"] == pytest.approx(3.12)
    assert "clickpower" in kinds["plc_cpu"]["source"]
    assert kinds["plc_io"]["estimate"] is False
    assert kinds["plc_io"]["steady_w"] == pytest.approx(0.96)
    assert kinds["pilot"]["estimate"] is False
    assert kinds["pilot"]["steady_w"] == pytest.approx(0.288)
    assert kinds["pilot"]["catalog"] == "800FD-P3N3R"
    for name in ("coil", "relay", "hmi", "sensor", "fan"):
        assert kinds[name]["estimate"] is True
    assert kinds["coil"]["steady_w"] == pytest.approx(5.0)
    assert kinds["relay"]["steady_w"] == pytest.approx(1.2)
    assert kinds["hmi"]["steady_w"] == pytest.approx(15.0)
    assert kinds["sensor"]["steady_w"] == pytest.approx(0.5)
    assert kinds["fan"]["steady_w"] == pytest.approx(8.0)
    assert supply["inrush_va"] == 0
    assert supply["utilization"] == "34.1 W of 120 W, 28%; limit 80%"
    assert kinds["coil"]["inrush_published"] is False
    assert kinds["coil"]["inrush_note"] == "not published"
    assert "not checked" in supply["boost_note"]
    assert "No output fuse" in supply["protection_note"]
    assert "not checked" in supply["protection_note"]
    assert supply["protection"] == []
    checks = {row["check"] for row in session.design_checks()}
    assert "control_power_margin" not in checks
    assert report["estimate_loads"]
    assert {row["kind"] for row in report["estimate_loads"]} >= {"coil", "relay", "hmi", "sensor", "fan"}


def test_user_estimate_overrides_and_margin_is_configurable():
    session = build_plc_hoa()
    before = session.power_budget()["supplies"][0]["steady_w"]
    session.set_power_estimate("HMI1", power_w=2.0)
    after = session.power_budget()["supplies"][0]
    hmi = next(row for row in after["loads"] if row["kind"] == "hmi")
    assert hmi["estimate"] is True
    assert hmi["steady_w"] == pytest.approx(2.0)
    assert after["steady_w"] == pytest.approx(before - 13.0)
    session.set_control_power_margin(0.1)
    tight = session.power_budget()["supplies"][0]
    assert tight["margin"] == pytest.approx(0.1)
    assert tight["ok_margin"] is False
    assert "1606-XLE240E" in (tight["suggestion"] or {}).get("catalog", "")
    assert any(row["check"] == "control_power_margin" for row in session.design_checks())


def test_overloaded_psu_warns_and_suggests_next_size():
    session = build_overloaded()
    supply = session.power_budget()["supplies"][0]
    assert supply["steady_w"] == pytest.approx(100.0)
    assert supply["ok_margin"] is False
    assert supply["ok_protection"] is False
    assert supply["suggestion"]["catalog"] == "1606-XLE240E"
    checks = session.design_checks()
    margin = next(row for row in checks if row["check"] == "control_power_margin")
    protection = next(row for row in checks if row["check"] == "control_power_protection")
    assert margin["ref"] == "PS1"
    assert "1606-XLE240E" in margin["message"]
    assert protection["ref"] == "PS1"
    advice = supply["protection"][0]
    assert advice["suggestion"] is None
    text = advice["message"]
    assert text == protection["message"]
    assert "1489-M1C010" in text
    assert "1489-M1C100" in text
    assert "Next: Allen-Bradley 1489-M1C100" not in text
    assert "1606-SR027" in text
    assert "1606-SR022" in text
    assert "page 17" in text
    assert "1492-SG122" in text
    assert "125%" in text
    assert "limited to library parts with a source" in text
    assert "≤ 3 A" in text
    assert "≤ 4 A" in text
    assert "Wire gauge is not on every protected conductor" in text
    assert text.startswith(
        "Q1 (1 A) is too small for 4.167 A, and no single C-curve breaker both carries this load "
        "and is documented to trip on either supply. Split the 24 V loads into separately protected branches."
    )
    assert text.index("Split the 24 V loads") < text.index("PS1 output protection")
    assert text.index("On 1606-XLE240E") < text.index("On 1606-XLE120E")
    assert "Branch 1: FAN1, 2.083 A, 125% 2.604 A." in text
    assert "Branch 2: HMI1, 2.083 A, 125% 2.604 A." in text
    assert "needs 2.604–4 A; no library part with a source in that range" in text
    assert "needs 2.604–3 A; no library part with a source in that range" in text
    assert "83%" in supply["utilization"]
    assert "limit 80%" in supply["utilization"]
    assert margin["x"] and margin["y"]
    findings = run_checks(session.project, session.catalog, parts=session.parts)
    assert any(item.check == "control_power_margin" and item.ref == "PS1" for item in findings)


def _three_amp_breaker():
    from panelcad.library.parts import CatalogPart

    # Not in the builtin library. Stands in for a sourced 3 A C-curve so the
    # positive path can be proved without inventing a catalog row.
    return CatalogPart(
        manufacturer="Allen-Bradley",
        catalog="1489-M1C030",
        library_id="iec.breakers.mcb_1p_16a",
        category="protection.breaker",
        description="test double, 1 pole, 3 A, C-curve",
        verified="partial",
        source="https://literature.rockwellautomation.com/idc/groups/literature/documents/sg/1492-sg122_-en-p.pdf",
        series="1489-M",
        amps=3,
        poles=1,
    )


def _breaker_load(watts: float, *, assign_supply: bool = True) -> Session:
    session = Session.create("Breaker window")
    session.place("nema.power.psu_24v_5a", ref="PS1", x=40, y=40)
    session.place("iec.breakers.mcb_1p_16a", ref="Q1", x=110, y=30)
    session.place("iec.loads.fan_24v", ref="FAN1", x=180, y=30)
    session.wire("PS1.plus", "Q1.1")
    session.wire("Q1.2", "FAN1.Lplus")
    session.wire("PS1.minus", "FAN1.M")
    if assign_supply:
        session.assign_part("PS1", "1606-XLE120E")
    session.assign_part("Q1", "1489-M1C010")
    session.set_power_estimate("FAN1", power_w=watts)
    return session


def test_next_library_breaker_is_too_big_for_the_supply_to_trip():
    session = _breaker_load(48.0)
    advice = session.power_budget()["supplies"][0]["protection"][0]
    assert advice["suggestion"] is None
    text = advice["message"]
    assert "Needs at least 2.5 A" in text
    assert "≤ 3 A" in text
    assert "1489-M1C100" in text
    assert "10 A is above the published downstream trip current 3 A" in text
    assert "limited to library parts with a source" in text
    assert "Next: Allen-Bradley 1489-M1C100" not in text
    assert "1606-XLE240E" not in text
    assert text.startswith("Q1 (1 A) is too small for 2 A.")
    assert "needs 2.5–3 A; no library part with a source in that range" in text
    assert "Branch 1: FAN1, 2 A, 125% 2.5 A." in text
    assert "Split the" not in text
    assert "1489-M1C030" not in text


def test_split_uses_the_smallest_sourced_breaker_on_each_branch():
    session = build_overloaded()
    session.place("iec.sensors.prox_24v", ref="PX1", x=180.0, y=120.0)
    session.wire("Q1.2", "PX1.Lplus", net="24V")
    session.wire("PS1.minus", "PX1.M", net="0V")
    # 1 A + 1 A share a branch (125% is 2.5 A). 1.5 A needs its own. Together they
    # are 3.5 A, and 125% of that is above the 3 A load-end limit.
    session.set_power_estimate("FAN1", power_w=24.0)
    session.set_power_estimate("PX1", power_w=24.0)
    session.set_power_estimate("HMI1", power_w=36.0)
    part = _three_amp_breaker()
    session.parts.parts[part.key()] = part
    supply = session.power_budget()["supplies"][0]
    assert supply["suggestion"] is None
    advice = supply["protection"][0]
    assert advice["suggestion"] is None
    text = advice["message"]
    assert text.startswith(
        "Q1 (1 A) is too small for 3.5 A, and no single C-curve breaker both carries this load "
        "and is documented to trip on 1606-XLE120E. Split the 24 V loads into separately protected branches."
    )
    assert "On 1606-XLE240E" not in text
    assert "Branch 1: FAN1, PX1, 2 A, 125% 2.5 A. Allen-Bradley 1489-M1C030 (3 A)." in text
    assert "Branch 2: HMI1, 1.5 A, 125% 1.875 A. Allen-Bradley 1489-M1C030 (3 A)." in text
    assert "Next: Allen-Bradley 1489-M1C030" not in text
    assert "Next: Allen-Bradley 1489-M1C100" not in text


def test_one_load_above_the_trip_limit_is_not_given_a_breaker():
    session = _breaker_load(80.0)
    supply = session.power_budget()["supplies"][0]
    assert supply["suggestion"] is None
    text = supply["protection"][0]["message"]
    assert text.startswith(
        "Q1 (1 A) is too small for 3.333 A, and no single C-curve breaker both carries this load "
        "and is documented to trip on 1606-XLE120E."
    )
    assert "Split the" not in text
    assert "Branch " not in text
    assert "FAN1, 3.333 A, 125% 4.167 A, cannot be protected by a C-curve breaker" in text
    assert "1606-SR027" in text
    assert "downstream ≤ 3 A" in text
    assert "Verify with the supply maker." in text
    assert "needs " not in text
    assert "Next:" not in text
    assert "1606-XLE240E" not in text


def test_sourced_breaker_inside_the_trip_window_is_suggested():
    session = _breaker_load(48.0)
    part = _three_amp_breaker()
    session.parts.parts[part.key()] = part
    advice = session.power_budget()["supplies"][0]["protection"][0]
    assert advice["suggestion"]["catalog"] == "1489-M1C030"
    text = advice["message"]
    assert "Next: Allen-Bradley 1489-M1C030 (3 A)" in text
    assert "1606-XLE120E is documented to trip this rating" in text
    assert "1489-M1C100" not in text


def test_unpublished_trip_does_not_suggest_a_breaker_size():
    session = _breaker_load(48.0, assign_supply=False)
    supply = session.power_budget()["supplies"][0]
    assert supply["suggestion"] is None
    advice = supply["protection"][0]
    assert advice["suggestion"] is None
    assert "breaker tripping capability not published for this supply; verify" in advice["message"]
    assert "1489-M1C100" not in advice["message"]
    assert "Next:" not in advice["message"]


def test_known_wire_gauge_caps_the_breaker_rating():
    session = build_overloaded()
    session.set_wire_spec(frm="Q1.2", to="FAN1.Lplus", gauge="0.5 mm2")
    session.set_wire_spec(frm="Q1.2", to="HMI1.Lplus", gauge="0.5 mm2")
    text = session.power_budget()["supplies"][0]["protection"][0]["message"]
    assert "0.5 mm2 (6 A)" in text
    assert "Breaker rating must be ≤ 6 A" in text
    assert "above the conductor ampacity 6 A" in text


def test_transformer_inrush_uses_schneider_chart():
    session = Session.create("CPT inrush")
    session.place("iec.power.control_transformer", ref="T1", x=30, y=30)
    session.assign_part("T1", "9070T100D23")
    for index in range(7):
        ref = f"K{index + 1}"
        session.place("iec.contactors.coil", ref=ref, x=80, y=20 + index * 12)
        session.assign_part(ref, "100-C09KJ10")
        session.wire("T1.X1", f"{ref}.A1")
        session.wire(f"{ref}.A2", "T1.X2")
    supply = session.power_budget()["supplies"][0]
    assert supply["kind"] == "ac"
    assert supply["rating_va"] == 100
    assert supply["steady_va"] == pytest.approx(66.5)
    assert supply["inrush_va"] == pytest.approx(525)
    assert supply["ok_margin"] is True
    assert supply["ok_inrush"] is False
    assert supply["inrush_limit_va"] == 499
    assert supply["suggestion"]["catalog"] == "9070T150D23"
    assert any(row["check"] == "control_power_inrush" for row in session.design_checks())
    assert all(not row["estimate"] for row in supply["loads"])


def test_generic_transformer_flags_missing_inrush_method():
    session = Session.create("Generic CPT")
    session.place("nema.power.control_transformer", ref="T1", x=30, y=30)
    session.place("nema.contactors.coil", ref="K1", x=90, y=30)
    session.wire("T1.X1", "K1.A1")
    session.wire("K1.A2", "T1.X2")
    supply = session.power_budget()["supplies"][0]
    assert supply["rating_va"] == 150
    assert supply["ok_margin"] is True
    assert supply["loads"][0]["estimate"] is True
    assert supply["loads"][0]["inrush_published"] is False
    assert supply["loads"][0]["inrush_note"] == "not published"
    assert supply["inrush_va"] == 0
    assert supply["method_missing"] is False
    assert "not checked" in supply["boost_note"]
    assert not any(row["check"] == "control_power_method" for row in session.design_checks())
    assert session.power_budget()["failed"] is False
    session.assign_part("K1", "100-C09KJ10")
    published = session.power_budget()["supplies"][0]
    assert published["loads"][0]["inrush_va"] == pytest.approx(75)
    assert published["loads"][0]["inrush_published"] is True
    assert published["method_missing"] is True
    assert any(row["check"] == "control_power_method" for row in session.design_checks())


def test_power_page_is_opt_in(tmp_path: Path):
    dol = build_dol()
    assert "power" not in dol.plan_print_package()["sections"]
    session = build_plc_hoa()
    plan = session.plan_print_package(sections="power")
    assert plan["sections"] == ["power"]
    assert len(plan["order"]) == 1
    pdf = tmp_path / "power.pdf"
    session.export_power_budget_pdf(pdf)
    text = extract_pdf_text(pdf)
    assert "CONTROL POWER" in text.upper() or "Control power" in text or "CONTROL POWER BUDGET" in text
    supply = session.power_budget()["supplies"][0]["ref"]
    assert supply in text
    assert "estimate" in text.lower()
    assert "28%" in text
    assert "limit 80%" in text
    assert "not published" in text.lower()
    assert "no output fuse" in text.lower()
    assert "not checked" in text.lower()
    _app()
    png = tmp_path / "power.png"
    render_package_page_png(pdf, 0, png, width_px=400)
    from PySide6.QtGui import QImage

    image = QImage(str(png))
    paper = image.pixelColor(image.width() // 2, 24)
    assert paper.red() > 200 and paper.green() > 200 and paper.blue() > 200
    dark = 0
    for y in range(0, image.height(), 5):
        for x in range(0, image.width(), 5):
            ink = image.pixelColor(x, y)
            if ink.red() < 60 and ink.green() < 60 and ink.blue() < 60:
                dark += 1
    assert dark > 30


def _wait_checks(win: MainWindow, app: QApplication, tick: int, timeout_ms: int = 4000) -> None:
    from PySide6.QtCore import QElapsedTimer

    timer = QElapsedTimer()
    timer.start()
    while win._checks_tick <= tick and timer.elapsed() < timeout_ms:
        app.processEvents()
    assert win._checks_tick > tick


def test_power_caption_clears_the_dock_tabs():
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QTabBar

    app = _app()
    app.setStyle("Fusion")
    win = MainWindow(build_plc_hoa())
    win.resize(1600, 1000)
    win.show()
    app.processEvents()
    win.power_wrap.raise_()
    app.processEvents()
    win.power._clear_dock_tabs()
    app.processEvents()
    assert win.power.caption.text() == "Control power — 1 supply"
    top_left = win.power.caption.mapTo(win, win.power.caption.rect().topLeft())
    caption = QRect(top_left, win.power.caption.size())
    for bar in win.findChildren(QTabBar):
        if not bar.isVisible():
            continue
        tabs = [bar.tabText(index) for index in range(bar.count())]
        if "Power" not in tabs and "BOM" not in tabs:
            continue
        origin = bar.mapTo(win, bar.rect().topLeft())
        assert not caption.intersects(QRect(origin, bar.size())), tabs
    win.close()


def test_power_summary_stays_clear_of_the_dock_tabs():
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtWidgets import QTabBar

    app = _app()
    app.setStyle("Fusion")
    win = MainWindow(build_overloaded())
    win.resize(1600, 1000)
    win.show()
    app.processEvents()
    tick = win._checks_tick
    win._check_runner.run_now()
    _wait_checks(win, app, tick)
    win.power_wrap.show()
    win.power_wrap.raise_()
    app.processEvents()

    def mapped(widget):
        origin = widget.mapTo(win, widget.rect().topLeft())
        return QRect(origin, widget.size())

    heights: list[int] = []
    for height in (280, 640):
        win.resizeDocks([win.power_wrap], [height], Qt.Orientation.Vertical)
        app.processEvents()
        win.power._clear_dock_tabs()
        app.processEvents()
        heights.append(win.power_wrap.height())
        summary = mapped(win.power.totals)
        assert summary.height() >= win.power.totals.minimumHeight()
        assert not summary.intersects(mapped(win.power.table))
        assert not summary.intersects(mapped(win.power.watts))
        for bar in win.findChildren(QTabBar):
            if not bar.isVisible() or bar.size().isEmpty():
                continue
            tabs = [bar.tabText(index) for index in range(bar.count())]
            if "Power" not in tabs and "BOM" not in tabs:
                continue
            bar_rect = mapped(bar)
            assert not summary.intersects(bar_rect), (height, tabs, summary, bar_rect)
    assert heights[0] < heights[1] - 40, heights
    win.close()


def test_power_dock_selects_supply_and_canvas_marks_psu():
    app = _app()
    session = build_overloaded()
    win = MainWindow(session)
    win.resize(1400, 900)
    win.show()
    app.processEvents()
    tick = win._checks_tick
    win._check_runner.run_now()
    _wait_checks(win, app, tick)
    win.power_wrap.show()
    win.power_wrap.raise_()
    win.power.select_supply("PS1")
    app.processEvents()
    assert win.power.caption.text() == "Control power — 1 supply"
    assert win.power.table.rowCount() == 2
    sources = {win.power.table.item(row, 5).text() for row in range(2)}
    assert sources == {"estimate"}
    inrush = {win.power.table.item(row, 4).text() for row in range(2)}
    assert inrush == {"not published"}
    plain = win.power.totals.toPlainText()
    markup = win.power.totals.toHtml()
    assert "83%" in plain
    assert "limit 80%" in plain
    assert "1489-M1C100" in plain
    assert "Next: Allen-Bradley 1489-M1C100" not in plain
    assert "limited to library parts with a source" in plain
    assert "1606-SR027" in plain
    assert "1606-XLE240E" in plain
    assert "Split the 24 V loads into separately protected branches." in plain
    assert "needs 2.604–4 A; no library part with a source in that range" in plain
    assert "font-weight:700" in markup or "<b>" in markup
    assert "<li" in markup
    markers = [item for item in win.canvas._items if isinstance(item, FindingMarker)]
    assert any(item.anchor_ref == "PS1" for item in markers)
    win.close()


def test_jsonl_check_control_power(tmp_path: Path):
    session = build_plc_hoa()
    path = tmp_path / "hoa.pcad.yaml"
    session.save(path)
    agent = Agent()
    opened = agent.handle({"op": "open", "path": str(path)})
    assert opened["ok"]
    csv_path = tmp_path / "budget.csv"
    pdf_path = tmp_path / "budget.pdf"
    reply = agent.handle(
        {
            "op": "check_control_power",
            "margin": 0.8,
            "path": str(csv_path),
            "pdf": str(pdf_path),
        }
    )
    assert reply["ok"], reply.get("error")
    result = reply["result"]
    assert result["supplies"]
    assert result["failed"] is False
    assert csv_path.is_file()
    assert "estimate" in csv_path.read_text(encoding="utf-8")
    assert pdf_path.is_file()
    assert "check_control_power" in agent.handle({"op": "help"})["result"]["ops"]
    hot = tmp_path / "hot.pcad.yaml"
    build_overloaded().save(hot)
    agent.handle({"op": "open", "path": str(hot)})
    checked = agent.handle({"op": "check_control_power"})
    assert checked["ok"], checked.get("error")
    message = checked["result"]["supplies"][0]["protection"][0]["message"]
    assert "1606-SR022" in message
    assert "Next: Allen-Bradley 1489-M1C100" not in message
    assert message.startswith("Q1 (1 A) is too small for 4.167 A")
    assert "Split the 24 V loads into separately protected branches." in message
    assert "needs 2.604–3 A; no library part with a source in that range" in message


def test_drawing_agent_check_control_power_tool(tmp_path: Path):
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from panelcad.ai.drawing import drawing_agent, open_drawing

    session = build_overloaded()
    path = tmp_path / "hot.pcad.yaml"
    session.save(path)
    seen: dict[str, str] = {}

    def listing(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for tool in info.function_tools:
            seen[tool.name] = tool.description or ""
        return ModelResponse(parts=[TextPart(content="ready")])

    drawing = open_drawing(path)
    with drawing_agent.override(model=FunctionModel(listing)):
        drawing_agent.run_sync("What power tools do you have?", deps=drawing)
    assert "check_control_power" in seen
    assert "estimate" in seen["check_control_power"].lower()
    assert "invent" in seen["check_control_power"].lower()
    assert "trip" in seen["check_control_power"].lower()
    assert "branch" in seen["check_control_power"].lower()

    steps = [
        ModelResponse(parts=[ToolCallPart(tool_name="check_control_power", args={}, tool_call_id="power-1")]),
        ModelResponse(parts=[TextPart(content="checked")]),
    ]

    def caller(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return steps.pop(0)

    with drawing_agent.override(model=FunctionModel(caller)):
        result = drawing_agent.run_sync("Check the control power.", deps=drawing)
    assert result.output == "checked"
    from pydantic_ai.messages import ToolReturnPart

    returned = [
        part.content
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == "check_control_power"
    ]
    assert returned, "check_control_power did not return a report"
    payload = returned[0]
    if isinstance(payload, str):
        payload = json.loads(payload)
    tool_message = payload["supplies"][0]["protection"][0]["message"]
    assert tool_message.startswith("Q1 (1 A) is too small for 4.167 A")
    assert "Split the 24 V loads into separately protected branches." in tool_message
    assert "needs 2.604–4 A; no library part with a source in that range" in tool_message


def test_cli_power_budget_exit_codes(tmp_path: Path):
    from typer.testing import CliRunner

    runner = CliRunner()
    healthy = tmp_path / "hoa.pcad.yaml"
    hot = tmp_path / "hot.pcad.yaml"
    build_plc_hoa().save(healthy)
    build_overloaded().save(hot)
    csv_path = tmp_path / "loads.csv"
    pdf_path = tmp_path / "page.pdf"
    ok = runner.invoke(app, ["electrical", "power-budget", str(healthy), "--out", str(csv_path), "--pdf", str(pdf_path)])
    assert ok.exit_code == 0, ok.output
    assert "ok" in ok.output
    assert "estimate" in ok.output
    assert csv_path.is_file()
    text = extract_pdf_text(pdf_path)
    assert "estimate" in text.lower()
    bad = runner.invoke(app, ["electrical", "power-budget", str(hot)])
    assert bad.exit_code == 1
    assert "1606-XLE240E" in bad.output
    assert "1606-SR027" in bad.output
    assert "limited to library parts with a source" in bad.output
    assert "Next: Allen-Bradley 1489-M1C100" not in bad.output
    assert "Split the 24 V loads into separately protected branches." in bad.output
    assert "needs 2.604–4 A; no library part with a source in that range" in bad.output
    pdf_path = tmp_path / "hot.pdf"
    rendered = runner.invoke(app, ["electrical", "power-budget", str(hot), "--pdf", str(pdf_path)])
    assert rendered.exit_code == 1
    page = " ".join(extract_pdf_text(pdf_path).split())
    assert "Split the 24 V loads into separately protected branches." in page
    assert "needs 2.604–4 A; no library part with a source in that range" in page
    assert "needs 2.604–3 A; no library part with a source in that range" in page
    assert "Verify with the supply maker." not in page
