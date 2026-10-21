"""Spacing grid, page density, and enclosure fill target."""

from __future__ import annotations

import pytest

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.core.route import path_hits_obstacles
from panelcad.core.spacing import page_capacity, spread_row
from panelcad.library.enclosures import load_enclosures
from panelcad.render.titleblock import working_rect
from panelcad.render.wiring import component_obstacle, route_sheet_wires


def test_spread_row_uses_the_width_and_keeps_a_minimum_gap():
    origins = spread_row(10.0, 210.0, [20.0, 20.0, 20.0], 18.0)
    assert origins[0] == 10.0
    rights = [origin + 20.0 for origin in origins]
    assert rights[-1] == 210.0
    gaps = [origins[i + 1] - rights[i] for i in range(2)]
    assert min(gaps) >= 18.0 - 1e-6
    assert max(gaps) - min(gaps) < 1e-6
    packed = spread_row(0.0, 40.0, [20.0, 20.0, 20.0], 18.0)
    assert packed[1] - (packed[0] + 20.0) >= 18.0 - 1e-6


def test_page_capacity_follows_usable_area():
    session = Session.create("Area")
    small = page_capacity(session.sheet())
    session.sheet().page.size = "A2"
    large = page_capacity(session.sheet())
    assert large["usable_width_mm"] > small["usable_width_mm"]
    assert large["max_devices"] > small["max_devices"]
    assert large["max_rungs"] >= small["max_rungs"]
    assert small["max_devices"] == small["max_per_rung"] * small["max_rungs"]


def test_dol_column_is_not_squeezed_left_and_rungs_have_clearance():
    session = Session.create("DOL space")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", rails="L1/L2")
    result = session.insert_circuit(kind="dol", control="local", hp=5, voltage=480)
    power = session.project.sheet(result["sheet"])
    wx, _wy, ww, _wh = working_rect(power)
    breaker = next(comp for comp in power.components if comp.properties.get("role") == "breaker")
    assert breaker.x > wx + ww * 0.2
    roles = {comp.properties.get("role"): comp for comp in power.components}
    assert abs(roles["breaker"].x - roles["contactor"].x) < 8.0
    control = session.project.sheet(result["control_sheet"])
    contacts = [
        comp
        for comp in control.components
        if comp.properties.get("role") in {"estop", "stop", "start"}
    ]
    contacts.sort(key=lambda comp: comp.x)
    assert len(contacts) >= 2
    for left, right in zip(contacts, contacts[1:]):
        device = session.catalog.get(left.library_id)
        gap = right.x - (left.x + float(device.schematic_width))
        assert gap >= 16.0, (left.id, right.id, gap)
    assert any(row.get("sheet") == control.id for row in result["density"])


def test_cleanup_spreads_a_cramped_rung_and_stays_put():
    session = Session.create("Cramped")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_nc", ref="STOP", x=30, y=40, snap=False)
    session.place("nema.operators.pb_no", ref="START", x=48, y=40, snap=False)
    session.place("nema.contactors.coil", ref="M", x=70, y=40, snap=False)
    session.wire("STOP.2", "START.3")
    session.wire("START.4", "M.A1")
    session.wire("STOP.1", "L1")
    session.wire("M.A2", "L2")
    first = session.cleanup()
    assert first["sheets"][0]["spaced"] >= 1
    sheet = session.sheet()
    stop, start = sheet.component("STOP"), sheet.component("START")
    gap = start.x - (stop.x + session.catalog.get(stop.library_id).schematic_width)
    assert gap >= 16.0
    pose = [(comp.id, round(comp.x, 3), round(comp.y, 3)) for comp in sheet.components]
    session.cleanup()
    again = [(comp.id, round(comp.x, 3), round(comp.y, 3)) for comp in sheet.components]
    assert again == pose


def test_supply_wire_does_not_cross_the_supply_body():
    session = Session.create("Supply")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", rails="L1/L2")
    result = session.insert_circuit(kind="dol", control="local", hp=5, voltage=480)
    sheet = session.project.sheet(result["control_sheet"])
    supply = next(comp for comp in sheet.components if comp.properties.get("role") in {"psu", "transformer"})
    body = component_obstacle(sheet, session.catalog, supply, pad=0.0)
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    hits = []
    for wire in sheet.wires:
        points = routes.get(wire.id) or []
        if path_hits_obstacles(points, [body], skip_ends=True):
            hits.append(wire.id)
    assert hits == []


def test_over_dense_page_warns_and_splits_with_offpage():
    session = Session.create("Dense")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    page = session.sheet().page
    page.size = "custom"
    page.width_mm = 140.0
    page.height_mm = 110.0
    for index in range(8):
        session.place(
            "nema.operators.pb_no",
            ref=f"S{index}",
            x=20.0 + (index % 4) * 22.0,
            y=24.0 + (index // 4) * 28.0,
            snap=False,
        )
    report = session.page_density()
    assert report["over_dense"]
    warnings = [row for row in session.design_checks() if row["check"] == "page_over_dense"]
    assert warnings
    assert "split_sheet" in warnings[0]["message"]
    preview = session.split_sheet(apply=False)
    assert preview["applied"] is False
    assert preview["move"]
    assert preview["keep"]
    applied = session.split_sheet(apply=True)
    assert applied["applied"] is True
    dest = session.project.sheet(applied["continuation"])
    assert dest.components
    session.undo()
    assert all(sheet.id != dest.id for sheet in session.project.sheets)


def test_agent_page_density_and_enclosure_fill_are_visible():
    agent = Agent()
    assert agent.handle({"op": "create_project", "name": "Tools"})["ok"]
    help_text = agent.handle({"op": "help"})["result"]["help"]
    assert "usable area" in help_text["page_density"].lower() or "usable-area" in help_text["page_density"]
    assert "off-page" in help_text["split_sheet"]
    assert "fill" in help_text["recommend_enclosure"].lower()
    placed = agent.handle(
        {"op": "place", "library_id": "iec.breakers.mcb_1p_16a", "ref": "Q1", "x": 40, "y": 30}
    )
    assert placed["ok"]
    density = agent.handle({"op": "page_density"})
    assert density["ok"]
    assert density["result"]["sheets"]
    rec = agent.handle({"op": "recommend_enclosure", "spare_pct": 0.25, "fill_target": 0.65})
    assert rec["ok"]
    box = rec["result"]["enclosure"]
    assert box["id"] in {item.id for item in load_enclosures().boxes}
    assert rec["result"]["fill"]["target"] == 0.65
    # The recommendation is a library row, not a made-up size.
    assert box["width_mm"] > 0 and box["height_mm"] > 0


def test_enclosure_steps_up_when_the_tight_box_is_too_full():
    session = Session.create("Step")
    session.set_title_block(standard="NEMA/ANSI")
    session.place("iec.breakers.mcb_3p_32a", ref="Q1", x=20, y=20)
    session.place("iec.contactors.ac3_9a", ref="K1", x=40, y=20)
    session.place("iec.protection.overload_9a", ref="F1", x=60, y=20)
    session.place("iec.power.psu_24v_5a", ref="G1", x=80, y=20)
    tight = session.recommend_enclosure(fill_max=1, fill_min=0, fill_target=1, spare_pct=0)
    stepped = session.recommend_enclosure()
    library_ids = {item.id for item in load_enclosures().boxes}
    assert tight["enclosure"]["id"] in library_ids
    assert stepped["enclosure"]["id"] in library_ids
    assert stepped["fill"]["max"] == 0.70
    tight_area = tight["enclosure"]["panel_width_mm"] * tight["enclosure"]["panel_height_mm"]
    stepped_area = stepped["enclosure"]["panel_width_mm"] * stepped["enclosure"]["panel_height_mm"]
    if tight["fill"]["ratio"] > 0.70 or tight["fill"]["height_spare"] < 0.25:
        assert stepped_area + 1.0 >= tight_area
        assert stepped["fill"]["stepped_up"] or stepped["fill"]["ratio"] <= 0.70 + 0.02
    if stepped["enclosure"]["catalog"]:
        assert stepped["enclosure"]["catalog"] in {item.catalog for item in load_enclosures().boxes if item.catalog}
    assert "invent" not in " ".join(stepped["reasons"]).lower() or "not invented" in " ".join(stepped["reasons"]).lower()


def _body(session: Session, sheet_id: str, ref: str) -> tuple[float, float, float, float]:
    from panelcad.core.spacing import _body_box

    sheet = session.project.sheet(sheet_id)
    comp = sheet.component(ref)
    return _body_box(comp, session.catalog.get(comp.library_id))


def _inside_drawing(session: Session, sheet_id: str) -> None:
    from panelcad.render.titleblock import body_hits_title_block, working_rect

    sheet = session.project.sheet(sheet_id)
    wx, wy, ww, wh = working_rect(sheet)
    for comp in sheet.components:
        if "offpage" in comp.library_id or "notes" in comp.library_id:
            continue
        device = session.catalog.get(comp.library_id)
        box = _body(session, sheet_id, comp.id)
        assert box[0] >= wx - 0.6, comp.id
        assert box[1] >= wy - 0.6, comp.id
        assert box[2] <= wx + ww + 0.6, (comp.id, box, wx + ww)
        assert box[3] <= wy + wh + 0.6, (comp.id, box, wy + wh)
        assert not body_hits_title_block(sheet, comp.x, comp.y, device.schematic_width, device.schematic_height), comp.id


def test_several_dol_columns_stay_inside_and_share_the_width():
    """4, 5, and 6 power columns fit in one row. They are not stacked on the title block."""

    from panelcad.render.titleblock import working_rect

    for count in (4, 5, 6):
        session = Session.create(f"{count} DOL")
        for _ in range(count):
            session.insert_circuit(kind="dol", hp=5, voltage=480)
        power = next(sheet for sheet in session.project.sheets if any(
            comp.properties.get("role") == "breaker" for comp in sheet.components
        ))
        breakers = [comp for comp in power.components if comp.properties.get("role") == "breaker"]
        assert len(breakers) == count
        _inside_drawing(session, power.id)
        xs = sorted(comp.x for comp in breakers)
        wx, _wy, ww, _wh = working_rect(power)
        assert xs[-1] - xs[0] > ww * 0.35
        gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
        assert max(gaps) - min(gaps) < 8.0
        assert max(comp.x for comp in breakers) < wx + ww * 0.92
        density = session.page_density(power.id)["sheets"][0]
        assert density["columns"] == count


def test_seven_dol_circuits_stay_on_one_sheet_without_a_crash():
    """Main fits seven DOL columns in one row. The builder must not spill or throw."""

    session = Session.create("7 DOL")
    for _ in range(7):
        session.insert_circuit(kind="dol", control="local", hp=5, voltage=480)
    power_sheets = [
        sheet
        for sheet in session.project.sheets
        if any(comp.properties.get("role") == "breaker" for comp in sheet.components)
    ]
    assert len(power_sheets) == 1
    breakers = [comp for comp in power_sheets[0].components if comp.properties.get("role") == "breaker"]
    assert len(breakers) == 7
    _inside_drawing(session, power_sheets[0].id)
    assert not (session.project.properties.get("power_column_overflow") or {})


def test_split_six_dol_control_sheet_is_under_the_limit_with_refs():
    session = Session.create("Six motors")
    for _ in range(6):
        session.insert_circuit(kind="dol", control="local", hp=5, voltage=480)
    control = session.project.sheet("motor-control")
    assert session.page_density(control.id)["sheets"][0]["over_dense"]
    seals = [comp.id for comp in control.components if comp.properties.get("role") == "seal"]
    coils = {comp.id for comp in control.components if comp.properties.get("role") == "coil"}
    assert seals and coils
    result = session.split_sheet(control.id, apply=True)
    assert result["applied"] is True
    assert result["offpage"]
    pages = result["pages"]
    assert len(pages) >= 2
    for sid in pages:
        row = session.page_density(sid)["sheets"][0]
        assert row["over_dense"] is False, (sid, row["message"], row["outside"])
        _inside_drawing(session, sid)
    def home_of(ref: str, role: str | None = None) -> str | None:
        for sheet in session.project.sheets:
            for comp in sheet.components:
                if comp.id != ref:
                    continue
                if role is not None and comp.properties.get("role") != role:
                    continue
                return sheet.id
        return None

    for seal in seals:
        parent = ""
        for sheet in session.project.sheets:
            for comp in sheet.components:
                if comp.id == seal:
                    parent = str(comp.properties.get("parent") or "")
        coil_home = home_of(parent, "coil") if parent else None
        if coil_home:
            assert home_of(seal, "seal") == coil_home, (seal, parent)
    assert any("offpage" in comp.library_id for sheet in session.project.sheets for comp in sheet.components)


def _inset_body(box: tuple[float, float, float, float], pad: float = 1.2) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = box
    if x1 - x0 <= pad * 2.0 or y1 - y0 <= pad * 2.0:
        return box
    return (x0 + pad, y0 + pad, x1 - pad, y1 - pad)


def test_split_feeds_share_one_pair_per_rail_and_wires_stay_clear():
    """24V and 0V each get one arrow pair, under the kept rungs, clear of every symbol."""

    from panelcad.core.route import path_hits_obstacles
    from panelcad.core.spacing import _body_box, _boxes_hit
    from panelcad.render.titleblock import body_hits_title_block
    from panelcad.render.wiring import component_obstacle, route_sheet_wires

    session = Session.create("M")
    for _ in range(6):
        session.insert_circuit(kind="dol", hp=5, voltage=480, control="local", pilot_lights=True)
    result = session.split_sheet("motor-control", apply=True)
    assert result["offpage"]
    fed = {str(link.get("to") or "") for link in result["offpage"]} | {
        str(link.get("net") or "") for link in result["offpage"]
    }
    assert "0V" in fed
    assert "24V" in fed
    pages = [session.project.sheet(sid) for sid in result["pages"]]
    for sheet in pages:
        wx, wy, ww, wh = working_rect(sheet)
        bodies = []
        for comp in sheet.components:
            device = session.catalog.get(comp.library_id)
            bodies.append((comp, _body_box(comp, device)))
        devices = [(comp, box) for comp, box in bodies if "offpage" not in comp.library_id]
        arrows = [(comp, box) for comp, box in bodies if "offpage" in comp.library_id]
        assert len(arrows) == 2, [comp.id for comp, _box in arrows]
        content_bottom = max(box[3] for _comp, box in devices)
        for comp, box in arrows:
            assert box[0] >= wx - 0.6, comp.id
            assert box[1] >= wy - 0.6, comp.id
            assert box[2] <= wx + ww + 0.6, (comp.id, box[2], wx + ww)
            assert box[3] <= wy + wh + 0.6, (comp.id, box[3], wy + wh)
            assert box[1] + 0.6 >= content_bottom, (comp.id, box[1], content_bottom)
            assert not body_hits_title_block(sheet, box[0], box[1], box[2] - box[0], box[3] - box[1]), comp.id
            for other, obox in bodies:
                if other is comp:
                    continue
                assert not _boxes_hit(box, obox, pad=0.0), (sheet.id, comp.id, other.id)
        routes = route_sheet_wires(sheet, session.catalog, project=session.project)
        for wire in sheet.wires:
            points = routes.get(wire.id) or []
            owners = {str(wire.frm).split(".", 1)[0], str(wire.to).split(".", 1)[0]}
            rects = [
                _inset_body(component_obstacle(sheet, session.catalog, comp, pad=0.0))
                for comp in sheet.components
                if comp.id not in owners
            ]
            assert not path_hits_obstacles(points, rects, skip_ends=True), (sheet.id, wire.frm, wire.to)


def test_outside_the_drawing_area_is_a_warning():
    session = Session.create("Outside")
    session.place("nema.operators.pb_no", ref="S0", x=2.0, y=2.0, snap=False)
    row = session.page_density()["sheets"][0]
    assert row["over_dense"] is True
    assert "S0" in row["outside"]
    assert row["severity"] == "warning"
    findings = [item for item in session.design_checks() if item["check"] == "page_over_dense"]
    assert findings
    assert findings[0]["severity"] == "warning"
    assert not any(
        item["check"] == "symbol_on_title_block" and item["ref"] == "S0" for item in session.design_checks()
    )


def test_title_block_parts_are_not_listed_again_as_over_dense():
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "examples" / "dol_starter.pcad.yaml"
    session = Session.open(path)
    row = session.page_density("power")["sheets"][0]
    assert "M1" not in row["outside"]
    assert row["severity"] != "error"
    checks = session.design_checks()
    assert any(item["check"] == "symbol_on_title_block" and item["ref"] == "M1" for item in checks)
    for item in checks:
        if item["check"] != "page_over_dense":
            continue
        assert item["severity"] == "warning"
        assert "M1" not in item["message"]


def test_cleanup_does_not_push_the_estop_into_the_supply():
    from pathlib import Path

    from panelcad.core.spacing import _boxes_hit

    path = Path(__file__).resolve().parents[1] / "examples" / "dol_starter.pcad.yaml"
    session = Session.open(path)
    session.cleanup("control")
    sheet = session.project.sheet("control")
    boxes = [
        (comp.id, _body(session, "control", comp.id))
        for comp in sheet.components
        if "offpage" not in comp.library_id
    ]
    for index, (name, box) in enumerate(boxes):
        for other, obox in boxes[index + 1 :]:
            assert not _boxes_hit(box, obox, pad=0.0), (name, other)
    g1 = _body(session, "control", "G1")
    s0 = _body(session, "control", "S0")
    assert not _boxes_hit(g1, s0, pad=0.0)


def test_split_keeps_a_wired_rung_together():
    session = Session.create("Wired rungs")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    page = session.sheet().page
    page.size = "custom"
    page.width_mm = 160.0
    page.height_mm = 120.0
    for index in range(6):
        session.place("nema.operators.pb_no", ref=f"PB{index}", x=24.0, y=22.0 + index * 6.0, snap=False)
        session.place("nema.contactors.coil", ref=f"CR{index}", x=70.0, y=36.0 + index * 14.0, snap=False)
        session.wire(f"PB{index}.4", f"CR{index}.A1")
    applied = session.split_sheet(apply=True)
    assert applied["applied"] is True

    def home(ref: str) -> str:
        for sheet in session.project.sheets:
            if any(comp.id == ref for comp in sheet.components):
                return sheet.id
        raise AssertionError(ref)

    for index in range(6):
        assert home(f"PB{index}") == home(f"CR{index}")
    assert applied["continuation"]
    assert any(home(f"PB{index}") == applied["continuation"] for index in range(6))


def test_fill_target_keeps_the_dialog_range_and_does_not_say_inside_when_over():
    from panelcad.core.enclosure import (
        FILL_TARGET_MAX,
        FILL_TARGET_MIN,
        SizingOptions,
        StandardEnclosure,
        _Fit,
        _pick_for_fill,
        fill_headline,
    )

    chosen = SizingOptions(fill_target=0.40)
    assert chosen.fill_target == pytest.approx(0.40)
    assert FILL_TARGET_MIN <= chosen.fill_target <= FILL_TARGET_MAX
    assert chosen.fill_min <= 0.40 + 1e-6
    high = SizingOptions(fill_target=0.90)
    assert high.fill_target == pytest.approx(0.90)
    assert high.fill_max >= 0.90 - 1e-6
    over = fill_headline(
        fill_ratio=0.82,
        height_spare=0.1,
        options=SizingOptions(),
        chosen_name="wall-small",
        chosen_catalog="N412",
        stepped_up=False,
        tight_name="wall-small",
    )
    assert "inside the" not in over
    assert "over the" in over
    assert "No larger library size" in over

    def _enc(ident: str, width: float, height: float, mount: str = "wall") -> StandardEnclosure:
        return StandardEnclosure(
            id=ident,
            mount=mount,
            nema="12",
            material="steel",
            height_mm=height,
            width_mm=width,
            depth_mm=200.0,
            panel_height_mm=height - 40.0,
            panel_width_mm=width - 40.0,
            catalog=ident,
        )

    small = _enc("wall-small", 400.0, 500.0)
    nxt = _enc("wall-next", 520.0, 640.0)
    floor = _enc("floor-huge", 1800.0, 900.0, mount="floor")
    fits = [
        _Fit(small, [], 200.0, 200.0, 80.0, 0.92, 0.02),
        _Fit(nxt, [], 200.0, 200.0, 80.0, 0.78, 0.08),
        _Fit(floor, [], 200.0, 200.0, 80.0, 0.12, 3.0),
    ]
    picked, _tight, _stepped, unreachable = _pick_for_fill(fits, SizingOptions())
    assert picked.box.id != "floor-huge"
    assert picked.box.mount == "wall"
    assert unreachable is True
