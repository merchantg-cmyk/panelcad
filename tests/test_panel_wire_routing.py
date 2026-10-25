from __future__ import annotations

import csv
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from panelcad.api.session import Session
from panelcad.core.models import (
    Component,
    Enclosure,
    Project,
    Rail,
    Sheet,
    Wire,
    WireTray,
)
from panelcad.core.panel_wiring import (
    PanelWireRoute,
    WireBundleTie,
    WireByWireSchedule,
    ZipTieWaypoint,
    add_wire_by_wire_sheet,
    add_zip_tie_slice,
    add_zip_tie_waypoint,
    build_wire_by_wire_schedule,
    export_wire_by_wire_csv,
    get_panel_wiring_mode,
    get_walkthrough_step,
    list_zip_tie_waypoints,
    mark_wire_installed,
    max_turn_angle_deg,
    move_zip_tie_waypoint,
    remove_zip_tie_waypoint,
    render_panel_wiring_png,
    render_walkthrough_step_png,
    resolve_pin_landing,
    route_panel_wires,
    set_panel_wiring_mode,
    set_walkthrough_step,
    step_walkthrough,
    verify_routes_inside_trays,
)
from panelcad.io.yaml_format import dump_project, load_project
from panelcad.render.pdf import sheet_to_pdf
from panelcad.render.svg import sheet_to_svg


def _build_layout_kind_project() -> Project:
    """Build a project using kind='layout' + layout_items (devices & Panduit ducts) as well as schematic wires."""
    sch = Sheet(
        id="s1",
        title="Main Schematic",
        kind="schematic",
        components=[
            Component(id="CB1", library_id="iec.breakers.mcb_3p_32a", x=40.0, y=40.0),
            Component(id="K1", library_id="iec.contactors.ac3_9a", x=100.0, y=40.0),
            Component(id="F1", library_id="iec.protection.overload_9a", x=160.0, y=40.0),
        ],
        wires=[
            Wire(
                id="w1",
                number="101",
                gauge="14 AWG",
                color="BK",
                type="MTW",
                frm="CB1.2",
                to="K1.L1",
            ),
            Wire(
                id="w2",
                number="102",
                gauge="14 AWG",
                color="RD",
                type="MTW",
                frm="CB1.4",
                to="K1.L2",
            ),
            Wire(
                id="w3",
                number="103",
                gauge="14 AWG",
                color="BU",
                type="MTW",
                frm="CB1.6",
                to="K1.L3",
            ),
            Wire(
                id="w4",
                number="201",
                gauge="18 AWG",
                color="WH",
                type="MTW",
                frm="K1.T1",
                to="F1.1",
            ),
        ],
    )
    lay = Sheet(
        id="layout1",
        title="Backpanel Layout",
        kind="layout",
        enclosure=Enclosure(x=20.0, y=60.0, width_mm=400.0, height_mm=400.0, depth_mm=200.0, draw_scale=0.5),
        rails=[Rail(id="din1", x=40.0, y=120.0, length_mm=160.0, height_mm=14.0)],
        properties={
            "layout_items": [
                {"id": "wt-top", "kind": "duct", "x": 30.0, "y": 80.0, "length_mm": 180.0, "width_mm": 16.0, "orientation": "h"},
                {"id": "wt-mid", "kind": "duct", "x": 30.0, "y": 170.0, "length_mm": 180.0, "width_mm": 16.0, "orientation": "h"},
                {"id": "wt-left", "kind": "duct", "x": 30.0, "y": 80.0, "length_mm": 110.0, "width_mm": 16.0, "orientation": "v"},
                {"id": "wt-right", "kind": "duct", "x": 194.0, "y": 80.0, "length_mm": 110.0, "width_mm": 16.0, "orientation": "v"},
                {"ref": "CB1", "kind": "device", "device": "iec.breakers.mcb_3p_32a", "x": 55.0, "y": 110.0, "scale": 0.5},
                {"ref": "K1", "kind": "device", "device": "iec.contactors.ac3_9a", "x": 100.0, "y": 110.0, "scale": 0.5},
                {"ref": "F1", "kind": "device", "device": "iec.protection.overload_9a", "x": 145.0, "y": 110.0, "scale": 0.5},
            ]
        },
    )
    return Project(name="ZipTie Test Project", sheets=[sch, lay])


def test_pin_landing_and_duct_routing_with_cut_length():
    proj = _build_layout_kind_project()
    session = Session(proj)
    sheet = session.sheet("layout1")

    # Line pins (1/3/5/L1/L2/L3) land on top edge; load pins (2/4/6/T1/T2/T3) land on bottom edge
    p_cb1_2 = resolve_pin_landing(sheet, session.catalog, "CB1", "2")
    p_k1_l1 = resolve_pin_landing(sheet, session.catalog, "K1", "L1")
    assert p_cb1_2 is not None and p_cb1_2.side == "bottom"
    assert p_k1_l1 is not None and p_k1_l1.side == "top"
    assert p_cb1_2.y > p_k1_l1.y
    assert p_cb1_2.z_mm > 0 and p_k1_l1.z_mm > 0

    routes = route_panel_wires(session, "layout1")
    assert len(routes) == 4
    r0 = routes[0]
    assert isinstance(r0, PanelWireRoute)
    assert r0.wire_number == "101"
    assert r0.from_ref == "CB1" and r0.from_pin == "2"
    assert r0.to_ref == "K1" and r0.to_pin == "L1"
    assert len(r0.points) >= 4
    # Traverses ducts (wt-mid -> wt-left -> wt-top)
    assert any(v.startswith("wt-") for v in r0.via)
    # Cut length formula: 2D path (unscaled to real mm) + z_from + z_to + 2*duct_depth + 2*slack, rounded to 10mm / 0.5in
    assert r0.cut_length_mm >= r0.path_2d_mm + p_cb1_2.z_mm + p_k1_l1.z_mm + 100.0
    assert r0.cut_length_mm % 10.0 == pytest.approx(0.0)
    assert (r0.cut_length_in * 2.0) == pytest.approx(round(r0.cut_length_in * 2.0))
    assert r0.length_mm == r0.cut_length_mm
    assert r0.length_in == r0.cut_length_in


def test_zip_tie_slice_bundle_move_and_remove(tmp_path: Path):
    proj = _build_layout_kind_project()
    session = Session(proj)

    routes_before = route_panel_wires(session, "layout1")
    lengths_before = {r.wire_number: r.cut_length_mm for r in routes_before}

    # Slice across the three phase wires (101, 102, 103) in the left vertical duct around y=130
    tie = add_zip_tie_slice(session, "layout1", 25.0, 130.0, 50.0, 130.0)
    assert isinstance(tie, ZipTieWaypoint)
    assert WireBundleTie is ZipTieWaypoint
    assert set(tie.wire_numbers) >= {"101", "102", "103"}

    routes_bundled = route_panel_wires(session, "layout1")
    for r in routes_bundled:
        if r.wire_number in ("101", "102", "103"):
            assert tie.id in r.via
            assert any(abs(px - tie.x) < 0.2 and abs(py - tie.y) < 0.2 for px, py in r.points)

    # Moving the zip-tie waypoint dynamically stretches the captured wires and increases cut length
    moved = move_zip_tie_waypoint(session, "layout1", tie.id, 15.0, 155.0)
    assert moved.x == pytest.approx(15.0)
    assert moved.y == pytest.approx(155.0)
    routes_moved = route_panel_wires(session, "layout1")
    for r in routes_moved:
        if r.wire_number in ("101", "102", "103"):
            assert any(abs(px - 15.0) < 0.2 and abs(py - 155.0) < 0.2 for px, py in r.points)
            assert r.cut_length_mm >= lengths_before[r.wire_number]

    # Persistence round-trip through .pcad.yaml
    yaml_path = tmp_path / "roundtrip.pcad.yaml"
    yaml_path.write_text(dump_project(session.project), encoding="utf-8")
    reloaded = load_project(yaml_path)
    reloaded_ties = list_zip_tie_waypoints(reloaded, "layout1")
    assert len(reloaded_ties) == 1
    assert reloaded_ties[0].id == tie.id
    assert reloaded_ties[0].x == pytest.approx(15.0)

    # Deleting the zip-tie unbundles the wires back to their original duct routes and cut lengths
    removed = remove_zip_tie_waypoint(session, "layout1", tie.id)
    assert removed is True
    assert list_zip_tie_waypoints(session, "layout1") == []
    routes_after = route_panel_wires(session, "layout1")
    for r in routes_after:
        assert tie.id not in r.via
        assert r.cut_length_mm == pytest.approx(lengths_before[r.wire_number])

    # Explicit add_zip_tie_waypoint also works
    explicit_tie = add_zip_tie_waypoint(session, "layout1", 80.0, 88.0, wire_numbers=["101", "102"])
    assert explicit_tie.wire_numbers == ["101", "102"]


def test_wire_by_wire_schedule_sheet_and_csv_export(tmp_path: Path):
    sample = Path(__file__).resolve().parents[1] / "examples" / "dol_starter.pcad.yaml"
    session = Session.open(sample)
    session.number_wires()

    sched = build_wire_by_wire_schedule(session, "enclosure")
    assert isinstance(sched, WireByWireSchedule)
    assert sched.total_wires == 12
    assert sched.total_length_mm > 0
    assert sched.total_length_in > 0

    dicts = sched.to_dicts()
    assert len(dicts) == 12
    first = dicts[0]
    for key in (
        "step_index",
        "wire_number",
        "from_tag",
        "from_pin",
        "to_tag",
        "to_pin",
        "gauge",
        "color",
        "via_path",
        "cut_length_mm",
        "cut_length_in",
        "points",
    ):
        assert key in first

    csv_path = export_wire_by_wire_csv(session, tmp_path / "wire_by_wire.csv", sheet_id="enclosure")
    assert csv_path.is_file()
    with csv_path.open("r", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 12
    assert float(rows[0]["cut_length_mm"]) > 0
    assert float(rows[0]["cut_length_in"]) > 0

    wbw_sheet = add_wire_by_wire_sheet(session, sheet_id="wire_by_wire")
    assert wbw_sheet.kind == "wire_by_wire"
    svg_text = sheet_to_svg(wbw_sheet, session.catalog, project=session.project)
    assert "WIRE-BY-WIRE CUT" in svg_text
    assert "CUT (IN)" in svg_text

    pdf_path = tmp_path / "wire_by_wire.pdf"
    sheet_to_pdf(wbw_sheet, session.catalog, pdf_path, project=session.project)
    assert pdf_path.is_file() and pdf_path.stat().st_size > 1000


def test_walkthrough_stepping_headless_and_gui(tmp_path: Path):
    _app = QApplication.instance() or QApplication([])
    from panelcad.gui.main_window import MainWindow

    sample = Path(__file__).resolve().parents[1] / "examples" / "dol_starter.pcad.yaml"
    session = Session.open(sample)
    session.number_wires()
    add_zip_tie_slice(session, "enclosure", 30.0, 140.0, 50.0, 140.0)

    # Headless walkthrough stepping 0 .. N-1
    total = len(route_panel_wires(session, "enclosure"))
    assert total == 12
    for idx in range(total):
        active = set_walkthrough_step(session, idx, "enclosure")
        assert active is not None
        assert active.step_index == idx
        assert get_panel_wiring_mode(session.sheet("enclosure")) == "walkthrough"
        assert get_walkthrough_step(session.sheet("enclosure"), total) == idx

    # Step forward / backward
    set_walkthrough_step(session, 0, "enclosure")
    r1 = step_walkthrough(session, 1, "enclosure")
    assert r1 is not None and r1.step_index == 1
    r0 = step_walkthrough(session, -1, "enclosure")
    assert r0 is not None and r0.step_index == 0

    # Mark wire installed
    assert mark_wire_installed(session, 0, True, "enclosure") is True
    sched = build_wire_by_wire_schedule(session, "enclosure")
    assert sched.rows[0].installed is True

    # Render PNGs & SVG in walkthrough and all modes
    p_all = render_panel_wiring_png(session, tmp_path / "all.png", mode="all", sheet_id="enclosure", dpi=120.0)
    p_step = render_walkthrough_step_png(session, 2, tmp_path / "step2.png", sheet_id="enclosure", dpi=120.0)
    assert p_all.is_file() and p_all.stat().st_size > 5000
    assert p_step.is_file() and p_step.stat().st_size > 5000

    svg_walk = sheet_to_svg(session.sheet("enclosure"), session.catalog, project=session.project)
    assert "TECHNICIAN WIRING WALKTHROUGH" in svg_walk
    assert "FROM:" in svg_walk and "TO:" in svg_walk

    # Qt GUI MainWindow + LayoutView + WiringWalkthroughPanel verification
    win = MainWindow(session)
    try:
        panel = win.show_wiring_walkthrough()
        assert panel.table.rowCount() == 12

        # Select Technician Walkthrough mode and step with Next / Prev buttons
        panel.select_wire_step(0)
        panel.set_mode("walkthrough")
        assert get_panel_wiring_mode(session.sheet("enclosure")) == "walkthrough"

        panel.next_wire()
        assert get_walkthrough_step(session.sheet("enclosure")) == 1

        # Clicking row 5 in the Wire-by-Wire table jumps the layout view to step 5
        panel._on_table_row_clicked(5, 0)
        assert get_walkthrough_step(session.sheet("enclosure")) == 5
        assert "Wire 6 of 12" in panel.step_label.text()

        # Interactive zip-tie slice commit on LayoutView
        ties_before = len(list_zip_tie_waypoints(session, "enclosure"))
        win.view.set_zip_tie_mode(True)
        assert win.view.zip_tie_mode is True
        created = win.view.commit_zip_tie_slice(82.0, 98.0, 82.0, 116.0)
        assert created is not None
        assert len(list_zip_tie_waypoints(session, "enclosure")) == ties_before + 1
    finally:
        win.close()


def test_no_sharp_90_degree_corners_on_panel_layout_and_schematic_untouched():
    """Backpanel layout wires must have smooth physical sweeps (zero 90° corners),
    while schematic print pages must remain 100% orthogonal and untouched.
    """
    sample = Path(__file__).resolve().parents[1] / "examples" / "dol_starter.pcad.yaml"
    session = Session.open(sample)
    session.number_wires()

    # 1. Standard Panduit wireway routing on backpanel layout: every curve step is smooth (< 25 deg)
    routes_duct = route_panel_wires(session, "enclosure")
    assert len(routes_duct) == 12
    for r in routes_duct:
        turn_deg = max_turn_angle_deg(r.points)
        assert turn_deg < 25.0, f"Wire {r.wire_number} has sharp turn {turn_deg}°"
        assert r.length_2d_mm == pytest.approx(r.path_2d_mm)
        assert len(r.smooth_points) == len(r.points)

    # 2. Zip-tie harness bundle routing: smooth cubic splines through ZT-1 and ZT-2 (< 25 deg per step)
    add_zip_tie_slice(session, "enclosure", 30.0, 140.0, 50.0, 140.0)
    add_zip_tie_slice(session, "enclosure", 82.0, 98.0, 82.0, 116.0)
    routes_bundled = route_panel_wires(session, "enclosure")
    for r in routes_bundled:
        turn_deg = max_turn_angle_deg(r.points)
        assert turn_deg < 25.0, f"Bundled wire {r.wire_number} has sharp turn {turn_deg}°"

    # 3. Schematic print page routing (kind == 'schematic') is 100% untouched and does not render panel wiring overlay
    sch_sheet = session.sheet("power")
    assert sch_sheet.kind == "schematic"
    svg_sch = sheet_to_svg(sch_sheet, session.catalog, project=session.project)
    assert "BACKPANEL WIRE ROUTING" not in svg_sch
    assert "TECHNICIAN WIRING WALKTHROUGH" not in svg_sch


def test_wires_travel_into_nearest_tray_and_path_strictly_inside_trays():
    """Every wire must travel straight from its terminal into the nearest horizontal tray,
    stay 100% inside the wire tray rectangles between tray entry and tray exit (both with
    and without zip-tie waypoints), and exit straight into the destination terminal.
    """
    sample = Path(__file__).resolve().parents[1] / "examples" / "dol_starter.pcad.yaml"
    session = Session.open(sample)
    session.number_wires()

    # 1. Verify 100% in-tray containment without zip-ties
    chk_plain = verify_routes_inside_trays(session, "enclosure")
    assert chk_plain["ok"], f"In-tray violations (no ties): {chk_plain['violations']}"
    assert chk_plain["wire_count"] == 12
    assert chk_plain["tray_count"] == 5
    for w in chk_plain["wires"]:
        assert w["outside_tray_points"] == 0
        assert w["max_turn_angle_deg"] < 25.0

    # 2. Verify 100% in-tray containment with zip-ties across wt-left, wt-top, and wt-mid
    add_zip_tie_slice(session, "enclosure", 31.7, 150.0, 47.7, 150.0)
    add_zip_tie_slice(session, "enclosure", 78.0, 98.7, 78.0, 114.7)
    add_zip_tie_slice(session, "enclosure", 150.0, 184.0, 150.0, 200.0)
    chk_ties = verify_routes_inside_trays(session, "enclosure")
    assert chk_ties["ok"], f"In-tray violations (with ties): {chk_ties['violations']}"
    for w in chk_ties["wires"]:
        assert w["outside_tray_points"] == 0
        assert w["max_turn_angle_deg"] < 25.0

    # 3. Also verify on synthetic layout_kind project
    proj_syn = _build_layout_kind_project()
    chk_syn = verify_routes_inside_trays(proj_syn, "layout1")
    assert chk_syn["ok"], f"In-tray violations (synthetic): {chk_syn['violations']}"
