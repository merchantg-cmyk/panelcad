"""Cutout sheets: datum math, mirror, DXF layers, and clearance warnings."""

from __future__ import annotations

import math
from pathlib import Path

import ezdxf
import pytest
import yaml

from tests.support import patch_layout_place_confirms
from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.core.checks import CHECK_IDS, STRUCTURAL_CHECKS, run_checks
from panelcad.core.cutout import (
    MM_PER_INCH,
    baseline_stacks,
    catalog_cutout,
    centers_meet,
    cutout_library,
    d_cut_paths,
    d_key_extents,
    datum_xy,
    library_cutout,
    load_cutout_library,
    spacing_failure,
    view_point,
)
from panelcad.core.cutout import CenterPitch
from panelcad.render.cutout_draw import printed_scale_note
from panelcad.core.cutout import _d_points_top
from panelcad.library.catalog import Catalog
from panelcad.library.paths import builtin_library_dir
from panelcad.render.print_package import DEFAULT_SECTIONS, SECTIONS, plan_print_package


@pytest.fixture(autouse=True)
def _confirm_layout_places(monkeypatch):
    patch_layout_place_confirms(monkeypatch)


def _door(name: str = "Cutouts") -> Session:
    session = Session.create(name)
    session.add_sheet("door", "Door", kind="door_layout")
    session.set_enclosure(width_mm=400, height_mm=500, depth_mm=210, x=0, y=0, sheet_id="door")
    session.set_title_block(company="ECS", drawing_number="CUT-1", rev="A")
    return session


def _iec() -> dict:
    raw = library_cutout("iec.operators.pb_no")
    assert raw is not None and not raw.unknown
    return raw


def test_published_records_cite_drawings_and_do_not_guess():
    library = load_cutout_library()
    seen = list(library.by_library_id.values()) + list(library.by_catalog.values())
    assert seen
    for cutout in seen:
        if cutout.unknown:
            assert not cutout.diameter_mm
            assert not cutout.width_mm
            continue
        assert cutout.source.startswith("https://"), cutout.source
        if not cutout.verified:
            assert "unverified" in (cutout.note or "").lower()
    iec = _iec()
    assert iec.verified is False
    assert iec.shape == "d"
    assert iec.diameter_mm == 22.3
    assert iec.diameter_plus_mm == 0.4
    assert iec.key_width_mm == 3.2
    assert iec.key_span_mm == 24.1
    assert iec.panel_thickness_min_mm == 1
    assert iec.panel_thickness_max_mm == 6
    assert (iec.min_center_a_mm, iec.min_center_b_mm) == (30, 50)
    assert iec.centers_interchangeable is True
    ppc = library_cutout("adc.prosense.ppc5_1102")
    assert ppc is not None
    assert ppc.shape == "rect"
    assert (ppc.width_mm, ppc.height_mm) == (92, 92)
    assert ppc.width_plus_mm == 0.8
    assert ppc.corner_radius_mm == 0
    assert ppc.depth_behind_mm == 65
    assert ppc.holes == []
    assert (ppc.min_center_x_mm, ppc.min_center_y_mm) == (117, 145)
    assert ppc.centers_interchangeable is False
    ab = catalog_cutout("Allen-Bradley", "800FP-F3")
    assert ab is not None and ab.shape == "round" and ab.diameter_mm == 22.3
    assert ab.diameter_plus_mm == 0.4 and ab.diameter_minus_mm == 0
    assert ab.key_optional is True
    assert ab.cuts_key(None) is False
    assert ab.cuts_key(True) is True
    assert (ab.min_center_x_mm, ab.min_center_y_mm) == (30, 40)
    pilot = catalog_cutout("Allen-Bradley", "800FD-P3N3R")
    assert pilot is not None and (pilot.min_center_x_mm, pilot.min_center_y_mm) == (30, 30)
    assert pilot.panel_thickness_min_mm == 1
    assert pilot.panel_thickness_max_mm == 10.5
    assert "10.5" in (pilot.note or "")
    assert "diagram A" in (pilot.note or "")
    selector = catalog_cutout("Allen-Bradley", "800FP-LSM23PN5GX01")
    assert selector is not None and selector.verified is True
    assert (selector.min_center_x_mm, selector.min_center_y_mm) == (30, 40)
    assert "standard knob" in (selector.note or "").lower()
    assert "50 mm vertical" in (selector.note or "")
    assert "48 mm horizontal and 50 mm vertical" not in (selector.note or "")
    assert ab.verified is True and ppc.verified is True
    assert ab.panel_thickness_max_mm == 6
    assert "p. 47" in (ab.note or "")
    assert "1-5" in (ppc.note or "")
    assert library_cutout("iec.hmi.panel_7in") is not None
    assert library_cutout("iec.hmi.panel_7in").unknown is True
    assert library_cutout("iec.protection.disconnect_3p").unknown is True
    data = yaml.safe_load((builtin_library_dir() / "cutouts.yaml").read_text(encoding="utf-8"))
    assert data["kind"] == "cutouts"
    catalog = Catalog()
    catalog.load_file(builtin_library_dir() / "cutouts.yaml")
    assert catalog.devices == {}
    assert "cutout_unknown" in CHECK_IDS
    assert "cutout_edge" in CHECK_IDS
    assert "cutout_overlap" in CHECK_IDS
    assert "cutout_clearance" in CHECK_IDS
    assert "cutout_flange" in CHECK_IDS
    assert not (STRUCTURAL_CHECKS & {"cutout_unknown", "cutout_edge", "cutout_overlap"})


def test_d_profile_matches_iec_table_2():
    cutout = _iec()
    intersect, outer = d_key_extents(22.3, 3.2, 24.1)
    assert abs(intersect - math.sqrt(11.15**2 - 1.6**2)) < 1e-9
    assert abs(outer - (24.1 - 11.15)) < 1e-9
    points = _d_points_top(cutout)
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    assert abs((max(ys) - min(ys)) - 24.1) < 0.05
    top = [point for point in points if abs(point[1] - min(ys)) < 1e-6]
    assert abs(max(point[0] for point in top) - min(point[0] for point in top) - 3.2) < 1e-6
    assert max(xs) - min(xs) <= 22.3 + 0.05
    assert centers_meet(30, 0, 30, 50)
    assert centers_meet(0, 30, 30, 50)
    assert centers_meet(30, 50, 30, 50)
    assert centers_meet(50, 30, 30, 50)
    assert not centers_meet(29, 0, 30, 50)
    assert not centers_meet(30, 40, 30, 50)
    stacks = baseline_stacks([10, 30, 20])
    assert stacks == [0, 2, 1]


@pytest.mark.confirm_generic_layout
def test_datum_and_back_view_mirror():
    assert datum_xy(100, 40, 400, 500, "top_left") == (100, 40)
    assert datum_xy(100, 40, 400, 500, "top_right") == (300, 40)
    assert datum_xy(100, 40, 400, 500, "bottom_left") == (100, 460)
    assert datum_xy(100, 40, 400, 500, "bottom_right") == (300, 460)
    assert view_point(100, 40, 400, "back") == (300, 40)
    assert view_point(100, 40, 400, "front") == (100, 40)

    session = _door()
    # Footprint 22 mm → centre (100, 40) when the corner is (89, 29).
    session.place("iec.operators.pb_no", ref="S1", x=89, y=29, sheet_id="door", snap=False)
    front = session.cutouts_generate({"surfaces": ["door"], "view": "front", "datum": "top_left"})
    back = session.cutouts_generate({"surfaces": ["door"], "view": "back", "datum": "top_left"})
    front_hole = next(row for row in front["drawings"][0]["holes"] if row["tag"] == "S1")
    back_hole = next(row for row in back["drawings"][0]["holes"] if row["tag"] == "S1")
    assert abs(front_hole["x"] - 100) < 1e-6
    assert abs(front_hole["y"] - 40) < 1e-6
    assert abs(back_hole["x"] - 300) < 1e-6
    assert abs(back_hole["y"] - 40) < 1e-6
    assert front["drawings"][0]["scale"] == "1:1"


@pytest.mark.confirm_generic_layout
def test_dxf_layers_units_and_mirror(tmp_path: Path):
    session = _door()
    session.place("iec.operators.pb_no", ref="S1", x=89, y=29, sheet_id="door", snap=False)
    session.assign_part("S1", "800FP-F3", manufacturer="Allen-Bradley")
    mm_path = tmp_path / "door.dxf"
    result = session.cutouts_export_dxf(mm_path, {"surfaces": ["door"], "units": "mm", "view": "front"})
    assert Path(result["paths"][0]) == mm_path
    doc = ezdxf.readfile(str(mm_path))
    assert int(doc.header["$INSUNITS"]) == 4
    names = {layer.dxf.name for layer in doc.layers}
    assert {"CUT", "DRILL", "DIM", "TEXT", "OUTLINE"} <= names
    msp = doc.modelspace()
    circles = [entity for entity in msp if entity.dxftype() == "CIRCLE" and entity.dxf.layer == "CUT"]
    assert len(circles) == 1
    assert abs(circles[0].dxf.radius - 11.15) < 1e-6
    # Confirmed 22×22 body keeps the punch at the placed centre; Ø22.3 is the hole.
    assert abs(circles[0].dxf.center.x - 100) < 1e-4
    assert abs(circles[0].dxf.center.y - 460) < 1e-4
    assert any(entity.dxftype() == "LWPOLYLINE" and entity.dxf.layer == "OUTLINE" for entity in msp)
    assert any(entity.dxftype() == "LINE" and entity.dxf.layer == "DIM" for entity in msp)
    assert any(entity.dxftype() == "TEXT" and entity.dxf.layer == "TEXT" for entity in msp)
    assert not any(entity.dxf.layer == "CUT" and entity.dxftype() != "CIRCLE" for entity in msp)

    inch = session.cutouts_export_dxf(tmp_path / "inch", {"surfaces": ["door"], "units": "inch", "view": "back"})
    back = ezdxf.readfile(inch["paths"][0])
    assert int(back.header["$INSUNITS"]) == 1
    back_circles = [entity for entity in back.modelspace() if entity.dxftype() == "CIRCLE" and entity.dxf.layer == "CUT"]
    assert abs(back_circles[0].dxf.radius - (11.15 / MM_PER_INCH)) < 1e-6
    assert abs(back_circles[0].dxf.center.x - (300 / MM_PER_INCH)) < 1e-4
    assert abs(back_circles[0].dxf.center.y - (460 / MM_PER_INCH)) < 1e-4


@pytest.mark.confirm_generic_layout
def test_drills_land_on_drill_layer(tmp_path: Path):
    session = _door()
    session.place("iec.operators.pb_no", ref="S1", x=120, y=120, sheet_id="door", snap=False)
    session.part_set_cutout(
        "S1",
        {
            "shape": "rect",
            "width_mm": 40,
            "height_mm": 30,
            "corner_radius_mm": 2,
            "holes": [
                {"diameter_mm": 5, "x_mm": -15, "y_mm": -10},
                {"diameter_mm": 5, "x_mm": 15, "y_mm": -10},
                {"diameter_mm": 5, "x_mm": 15, "y_mm": 10},
                {"diameter_mm": 5, "x_mm": -15, "y_mm": 10},
            ],
            "source": "shop note",
        },
    )
    path = session.cutouts_export_dxf(tmp_path / "drills", {"surfaces": ["door"]})["paths"][0]
    doc = ezdxf.readfile(path)
    drills = [entity for entity in doc.modelspace() if entity.dxftype() == "CIRCLE" and entity.dxf.layer == "DRILL"]
    cuts = [entity for entity in doc.modelspace() if entity.dxf.layer == "CUT"]
    assert len(drills) == 4
    assert all(abs(entity.dxf.radius - 2.5) < 1e-6 for entity in drills)
    assert cuts and all(entity.dxftype() == "LWPOLYLINE" for entity in cuts)
    arcs = [entity for entity in cuts[0].virtual_entities() if entity.dxftype() == "ARC"]
    assert len(arcs) == 4
    assert all(abs(entity.dxf.radius - 2.0) < 1e-4 for entity in arcs)
    assert session.undo()
    assert "cutout" not in session.project.sheet("door").component("S1").properties


@pytest.mark.confirm_generic_layout
def test_clearance_overlap_edge_and_depth():
    session = _door()
    session.place("iec.operators.pb_no", ref="S1", x=0, y=80, sheet_id="door", snap=False)
    session.place("iec.operators.pb_nc", ref="S2", x=20, y=80, sheet_id="door", snap=False)
    session.place("adc.prosense.ppc5_1102", ref="TIC1", x=200, y=200, sheet_id="door", snap=False)
    session.project.properties["cutout_edge_clearance_mm"] = 20
    session.project.properties["cutout_flange_mm"] = 25
    session.project.properties["cutout_panel_thickness_mm"] = 8
    session.set_enclosure(depth_mm=40, sheet_id="door")
    checks = {row.check for row in run_checks(session.project, session.catalog)}
    assert "cutout_edge" in checks
    assert "cutout_flange" in checks
    assert "cutout_overlap" in checks
    assert "cutout_clearance" in checks
    messages = [row.message for row in run_checks(session.project, session.catalog)]
    assert any("overlap" in message for message in messages)
    assert any("behind" in message for message in messages)
    assert any("thickness" in message for message in messages)
    # A normal 30 mm row does not warn. Centres are 100 mm apart.
    spaced = _door("Spaced")
    spaced.place("iec.operators.pilot_light", ref="H1", x=80, y=80, sheet_id="door", snap=False)
    spaced.place("iec.operators.selector_2pos", ref="S3", x=180, y=80, sheet_id="door", snap=False)
    spaced_checks = {row.check for row in run_checks(spaced.project, spaced.catalog)}
    assert "cutout_overlap" not in spaced_checks
    assert "cutout_clearance" not in spaced_checks
    assert "cutout_edge" not in spaced_checks


@pytest.mark.confirm_generic_layout
def test_unknown_cutout_is_not_machined(tmp_path: Path):
    session = _door()
    session.place("iec.hmi.panel_7in", ref="HMI1", x=80, y=80, sheet_id="door", snap=False)
    session.place("iec.loads.fan_24v", ref="FAN1", x=200, y=200, sheet_id="door", snap=False)
    session.place("iec.protection.disconnect_3p", ref="DS1", x=40, y=40, sheet_id="main")
    rows = run_checks(session.project, session.catalog)
    assert any(row.check == "cutout_unknown" and row.ref == "HMI1" for row in rows)
    assert any(row.check == "cutout_unknown" and row.ref == "FAN1" for row in rows)
    assert not any(row.ref == "DS1" and row.check.startswith("cutout_") for row in rows)
    path = session.cutouts_export_dxf(tmp_path / "unknown", {"surfaces": ["door"]})["paths"][0]
    doc = ezdxf.readfile(path)
    cut_geometry = [
        entity
        for entity in doc.modelspace()
        if entity.dxf.layer in {"CUT", "DRILL"} and entity.dxftype() in {"CIRCLE", "LWPOLYLINE", "LINE"}
    ]
    assert cut_geometry == []
    text = " ".join(entity.dxf.text for entity in doc.modelspace() if entity.dxftype() == "TEXT")
    assert "CUTOUT UNKNOWN" in text
    session.part_set_cutout("HMI1", unknown=True)
    assert session.project.sheet("door").component("HMI1").properties["cutout"]["unknown"] is True


@pytest.mark.confirm_generic_layout
def test_assign_part_replaces_generic_d_with_ab_round():
    session = _door()
    session.place("iec.operators.pb_no", ref="S1", x=80, y=80, sheet_id="door", snap=False)
    before = session.cutouts_generate({"surfaces": ["door"]})["drawings"][0]
    assert before["features"][0]["shape"] == "d"
    assert before["features"][0]["include_key"] is True
    session.assign_part("S1", "800FP-F3")
    after = session.cutouts_generate({"surfaces": ["door"]})["drawings"][0]
    feature = after["features"][0]
    assert feature["shape"] == "round"
    assert feature["size"].startswith("Ø22.3")
    assert feature["include_key"] is False
    assert "800f-in016" in feature["source"]
    with pytest.raises(ValueError, match="does not fit"):
        session.assign_part("S1", "100-C09D10", manufacturer="Allen-Bradley")
    kept = session.project.sheet("door").component("S1").properties
    assert kept.get("catalog") == "800FP-F3"


@pytest.mark.confirm_generic_layout
def test_agent_cli_and_optional_package_section(tmp_path: Path):
    agent = Agent()
    created = agent.handle({"id": "corr", "op": "create_project", "name": "Agent cut"})
    assert created["ok"] is True
    assert agent.handle({"op": "add_sheet", "sheet": "door", "kind": "door_layout"})["ok"]
    assert agent.handle(
        {"op": "set_enclosure", "sheet": "door", "width_mm": 400, "height_mm": 500, "depth_mm": 200, "x": 0, "y": 0}
    )["ok"]
    assert agent.handle(
        {"op": "place", "library_id": "iec.operators.estop", "ref": "ES1", "x": 100, "y": 100, "sheet": "door", "snap": False}
    )["ok"]
    generated = agent.handle({"id": "req-1", "op": "cutouts_generate", "view": "back", "datum": "top_left"})
    assert generated["ok"] is True
    assert generated["id"] == "req-1"
    door = next(item for item in generated["result"]["drawings"] if item["surface"] == "door")
    assert door["view"] == "back"
    exported = agent.handle({"op": "cutouts_export_dxf", "path": str(tmp_path / "agent"), "units": "mm", "surfaces": "door"})
    assert exported["ok"] is True
    assert ezdxf.readfile(exported["result"]["paths"][0])
    marked = agent.handle({"op": "part_set_cutout", "ref": "ES1", "unknown": True})
    assert marked["ok"] is True
    assert agent.session.project.sheet("door").component("ES1").properties["cutout"]["unknown"] is True
    help_result = agent.handle({"op": "help"})
    for name in ("cutouts_generate", "cutouts_export_dxf", "cutouts_export_pdf", "part_set_cutout"):
        assert name in help_result["result"]["ops"]
        assert name in help_result["result"]["help"]

    project = tmp_path / "job.pcad.yaml"
    agent.session.save(project)
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    runner = CliRunner()
    listed = runner.invoke(app, ["cutouts", "generate", str(project), "--surfaces", "door"])
    assert listed.exit_code == 0, listed.output
    assert "door" in listed.output
    dxf = runner.invoke(app, ["cutouts", "dxf", str(project), "--out", str(tmp_path / "cli"), "--units", "mm", "--surfaces", "door"])
    assert dxf.exit_code == 0, dxf.output
    assert "CUT" in {layer.dxf.name for layer in ezdxf.readfile(next((tmp_path / "cli").glob("*.dxf"))).layers}

    assert "cutouts" in SECTIONS
    assert "cutouts" not in DEFAULT_SECTIONS
    package = plan_print_package(agent.session, sections=None)
    assert "cutouts" not in package.sections
    opted = plan_print_package(agent.session, sections=["cutouts"])
    assert any(page.section == "cutouts" and page.sheet_id == "door" for page in opted.pages)
    pdf = tmp_path / "pkg.pdf"
    written = agent.session.print_package(pdf, sections=["cutouts"])
    assert Path(written["path"]).is_file()
    assert written["pages"] >= 1


@pytest.mark.confirm_generic_layout
def test_gui_preview_and_menu(tmp_path: Path):
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.cutout_dialog import CutoutDialog
    from panelcad.gui.main_window import MainWindow
    from panelcad.gui.package_dialog import PrintPackageDialog

    app = QApplication.instance() or QApplication([])
    session = _door("Preview")
    session.place("iec.operators.pb_no", ref="S1", x=70, y=70, sheet_id="door", snap=False)
    session.place("iec.operators.pilot_light", ref="H1", x=170, y=70, sheet_id="door", snap=False)
    session.place("adc.prosense.ppc5_1102", ref="TIC1", x=180, y=220, sheet_id="door", snap=False)
    dialog = CutoutDialog(session)
    dialog.resize(900, 760)
    app.processEvents()
    dialog.show()
    dialog.resize(900, 760)
    app.processEvents()
    dialog.refresh_preview()
    dialog.zoom_to_fit()
    app.processEvents()
    assert dialog.preview.pixmap() is not None
    assert not dialog.preview.pixmap().isNull()
    fitted = dialog.preview.pixmap()
    viewport = dialog.preview_scroll.viewport().size()
    assert fitted.width() <= viewport.width()
    assert fitted.height() <= viewport.height()
    assert fitted.height() > 40
    dialog.view.setCurrentIndex(dialog.view.findData("back"))
    app.processEvents()
    assert dialog._drawings
    assert all(drawing.view == "back" for drawing in dialog._drawings)
    assert dialog.preview.pixmap() is not None and not dialog.preview.pixmap().isNull()
    pdf = dialog.export_pdf_to(tmp_path / "preview.pdf")
    assert pdf is not None and pdf.is_file()
    dxfs = dialog.export_dxf_to(tmp_path / "gui-dxf")
    assert dxfs
    assert session.project.properties.get("cutout_view") == "back"
    dialog.close()

    window = MainWindow(session)
    titles: list[str] = []

    def walk(menu) -> None:
        for action in menu.actions():
            child = action.menu()
            if child is not None:
                walk(child)
            titles.append(action.text().replace("&", ""))

    for action in window.menuBar().actions():
        menu = action.menu()
        if menu is not None:
            walk(menu)
    assert "Cutout drawings…" in titles
    package = PrintPackageDialog()
    assert "cutouts" not in package.values()["sections"]
    window.close()
    _ = app


@pytest.mark.confirm_generic_layout
def test_side_wall_uses_enclosure_depth():
    session = _door()
    session.place(
        "iec.operators.pb_no",
        ref="S1",
        x=40,
        y=80,
        sheet_id="door",
        snap=False,
        properties={"surface": "left"},
    )
    drawings = {item["surface"]: item for item in session.cutouts_generate()["drawings"]}
    assert drawings["left"]["width_mm"] == 210
    assert drawings["left"]["height_mm"] == 500
    assert any(feature["ref"] == "S1" for feature in drawings["left"]["features"])
    assert not any(feature["ref"] == "S1" for feature in drawings["door"]["features"])


def test_cutout_library_is_loaded():
    assert cutout_library().by_library_id["iec.operators.estop"].shape == "d"


def _on_ccw_arc(start: float, sweep: float, angle: float) -> bool:
    start %= 360.0
    end = (start + sweep) % 360.0
    angle %= 360.0
    if start <= end:
        return start - 1e-6 <= angle <= end + 1e-6
    return angle >= start - 1e-6 or angle <= end + 1e-6


def test_d_hole_arc_is_the_nominal_diameter():
    cutout = _iec()
    arc, lines = d_cut_paths(cutout, cx=100, cy=40, rotation=0, panel_width=400, view="front")
    assert abs(arc.radius - (22.3 / 2.0)) < 1e-9
    assert abs(arc.sweep_deg - 360.0) > 10.0
    assert _on_ccw_arc(arc.start_deg, arc.sweep_deg, 270.0)
    assert not _on_ccw_arc(arc.start_deg, arc.sweep_deg, 90.0)
    assert len(lines) == 3
    flat = lines[1]
    assert abs(flat.y1 - flat.y2) < 1e-6
    assert abs(flat.y1 - (40.0 - (24.1 - 11.15))) < 1e-6
    assert abs(abs(flat.x2 - flat.x1) - 3.2) < 1e-6


@pytest.mark.confirm_generic_layout
def test_d_hole_dxf_uses_a_true_arc(tmp_path: Path):
    session = _door()
    session.place("iec.operators.pb_no", ref="S1", x=89, y=29, sheet_id="door", snap=False)
    path = session.cutouts_export_dxf(tmp_path / "d.dxf", {"surfaces": ["door"]})["paths"][0]
    doc = ezdxf.readfile(path)
    arcs = [entity for entity in doc.modelspace() if entity.dxftype() == "ARC" and entity.dxf.layer == "CUT"]
    lines = [entity for entity in doc.modelspace() if entity.dxftype() == "LINE" and entity.dxf.layer == "CUT"]
    polylines = [entity for entity in doc.modelspace() if entity.dxftype() == "LWPOLYLINE" and entity.dxf.layer == "CUT"]
    assert len(arcs) == 1
    assert abs(arcs[0].dxf.radius - 11.15) < 1e-6
    assert len(lines) == 3
    assert polylines == []
    start = float(arcs[0].dxf.start_angle) % 360.0
    end = float(arcs[0].dxf.end_angle) % 360.0
    sweep = (end - start) % 360.0
    assert _on_ccw_arc(start, sweep, 270.0)
    assert not _on_ccw_arc(start, sweep, 90.0)


@pytest.mark.confirm_generic_layout
def test_spacing_ignores_operators_that_are_not_neighbours():
    buttons = CenterPitch("swap", 30, 50)
    assert spacing_failure(20, 500, buttons, 22, 22, 22, 22) is None
    assert spacing_failure(20, 0, buttons, 22, 22, 22, 22) is not None
    assert spacing_failure(30, 40, buttons, 22, 22, 22, 22) is not None
    assert spacing_failure(30, 50, buttons, 22, 22, 22, 22) is None
    ppc = CenterPitch("axis", 117, 145)
    assert spacing_failure(100, 0, ppc, 96, 96, 96, 96) is not None
    assert spacing_failure(117, 0, ppc, 96, 96, 96, 96) is None
    assert spacing_failure(0, 140, ppc, 96, 96, 96, 96) is not None
    assert spacing_failure(0, 145, ppc, 96, 96, 96, 96) is None
    assert spacing_failure(100, 500, ppc, 96, 96, 96, 96) is None

    close = _door("Close row")
    close.place("iec.operators.pb_no", ref="S1", x=89, y=89, sheet_id="door", snap=False)
    close.place("iec.operators.pb_nc", ref="S9", x=109, y=589, sheet_id="door", snap=False)
    messages = [row.message for row in run_checks(close.project, close.catalog) if row.check == "cutout_clearance"]
    assert messages == []
    close.place("iec.operators.pilot_light", ref="H1", x=109, y=89, sheet_id="door", snap=False)
    warned = [row.message for row in run_checks(close.project, close.catalog) if row.check == "cutout_clearance"]
    assert warned
    assert all("500" not in message for message in warned)


@pytest.mark.confirm_generic_layout
def test_centre_minimum_is_editable_per_part():
    session = _door()
    session.place("iec.operators.pb_no", ref="S1", x=80, y=80, sheet_id="door", snap=False)
    session.place("iec.operators.pb_nc", ref="S2", x=100, y=80, sheet_id="door", snap=False)
    assert any(row.check == "cutout_clearance" for row in run_checks(session.project, session.catalog))
    session.part_set_cutout("S1", min_center_x_mm=15, min_center_y_mm=15, centers_interchangeable=False)
    session.part_set_cutout("S2", min_center_x_mm=15, min_center_y_mm=15, centers_interchangeable=False)
    assert not any(row.check == "cutout_clearance" for row in run_checks(session.project, session.catalog))


@pytest.mark.confirm_generic_layout
def test_bottom_right_dimensions_leave_that_corner(tmp_path: Path):
    session = _door()
    session.place("iec.operators.pb_no", ref="S1", x=89, y=29, sheet_id="door", snap=False)
    session.part_set_cutout("S1", include_key=False)
    path = session.cutouts_export_dxf(
        tmp_path / "br.dxf",
        {"surfaces": ["door"], "datum": "bottom_right"},
    )["paths"][0]
    doc = ezdxf.readfile(path)
    width, height = 400.0, 500.0
    horizontals = []
    verticals = []
    for entity in doc.modelspace():
        if entity.dxftype() != "LINE" or entity.dxf.layer != "DIM":
            continue
        if abs(entity.dxf.start.y - entity.dxf.end.y) < 1e-4:
            horizontals.append(entity.dxf.start.y)
        if abs(entity.dxf.start.x - entity.dxf.end.x) < 1e-4:
            verticals.append(entity.dxf.start.x)
    assert any(y < -1.0 for y in horizontals)
    assert all(y <= height + 0.1 for y in horizontals)
    assert any(x > width + 1.0 for x in verticals)
    assert all(x >= -0.1 for x in verticals)


def _ink(image, x_mm: float, y_mm: float, px: float) -> bool:
    x = int(round(x_mm * px))
    y = int(round(y_mm * px))
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            color = image.pixelColor(x + dx, y + dy)
            if color.red() < 160 and color.green() < 160 and color.blue() < 160:
                return True
    return False


@pytest.mark.confirm_generic_layout
def test_sheet_dimensions_are_drawn_from_the_datum_corner():
    """The dimension line leaves the chosen corner, and the page reserves that band."""

    from PySide6.QtWidgets import QApplication

    from panelcad.core.cutout import CutoutSettings, build_drawings
    from panelcad.render.cutout_draw import render_cutout_qimage

    app = QApplication.instance() or QApplication([])
    px = 6.0

    def sheet(datum: str):
        session = _door(datum)
        session.place("iec.operators.pb_no", ref="S1", x=89, y=29, sheet_id="door", snap=False)
        drawing = build_drawings(session.project, session.catalog, CutoutSettings(datum=datum, surfaces=("door",)))[0]
        image = render_cutout_qimage(drawing, session.project, px_per_mm=px)
        return drawing, image

    top, top_image = sheet("top_left")
    assert (top.panel_origin_x, top.panel_origin_y) == (38.0, 30.0)
    # 100 mm ordinate above the top edge; 40 mm ordinate left of the left edge.
    assert _ink(top_image, top.panel_origin_x + 50, top.panel_origin_y - 9, px)
    assert _ink(top_image, top.panel_origin_x - 9, top.panel_origin_y + 20, px)
    assert not _ink(top_image, top.panel_origin_x + 50, top.panel_origin_y + 500 + 9, px)

    bottom, bottom_image = sheet("bottom_right")
    assert (bottom.panel_origin_x, bottom.panel_origin_y) == (18.0, 18.0)
    assert bottom.page_height_mm - (bottom.panel_origin_y + bottom.panel_height_mm) >= bottom.gutter_y + 36
    assert bottom.page_width_mm - (bottom.panel_origin_x + bottom.panel_width_mm) >= bottom.gutter_x + 120
    # 300 mm ordinate under the bottom edge, from the right corner; 460 mm to the right.
    assert _ink(bottom_image, bottom.panel_origin_x + 250, bottom.panel_origin_y + 500 + 9, px)
    assert _ink(bottom_image, bottom.panel_origin_x + 400 + 9, bottom.panel_origin_y + 270, px)
    assert not _ink(bottom_image, bottom.panel_origin_x + 250, bottom.panel_origin_y - 9, px)
    assert not _ink(bottom_image, bottom.panel_origin_x - 9, bottom.panel_origin_y + 270, px)
    _ = app


@pytest.mark.confirm_generic_layout
def test_empty_faces_are_not_exported(tmp_path: Path):
    session = _door()
    session.place("iec.operators.pb_no", ref="S1", x=80, y=80, sheet_id="door", snap=False)
    result = session.cutouts_export_dxf(tmp_path / "shop")
    assert result["surfaces"] == ["door"]
    assert "backplate" in result["skipped"]
    assert "left" in result["skipped"]
    assert "right" in result["skipped"]
    assert len(result["paths"]) == 1
    assert Path(result["paths"][0]).is_file()
    from panelcad.render.print_package import plan_print_package

    package = plan_print_package(session, sections=["cutouts"])
    cutout_pages = [page.sheet_id for page in package.pages if page.section == "cutouts"]
    assert cutout_pages == ["door"]


def test_shrunk_package_does_not_say_one_to_one():
    assert printed_scale_note(1, 1) == "1:1"
    assert printed_scale_note(1, 0.5) == "1:2"
    assert printed_scale_note(1, 0.72) == "NOT TO SCALE – use DXF"


@pytest.mark.confirm_generic_layout
def test_part_properties_edit_key_and_spacing():
    from PySide6.QtWidgets import QApplication, QCheckBox, QLabel

    from panelcad.gui.docks import PropertiesDock

    app = QApplication.instance() or QApplication([])
    session = _door("Props")
    session.place("iec.operators.pb_no", ref="S1", x=80, y=80, sheet_id="door", snap=False)
    session.assign_part("S1", "800FP-F3", manufacturer="Allen-Bradley")
    dock = PropertiesDock()
    dock.show_component(session, "S1")
    key = dock.findChild(QCheckBox, "cutoutIncludeKey")
    assert key is not None
    assert key.isChecked() is False
    key.setChecked(True)
    app.processEvents()
    assert session.project.sheet("door").component("S1").properties["include_key"] is True
    feature = session.cutouts_generate({"surfaces": ["door"]})["drawings"][0]["features"][0]
    assert feature["include_key"] is True
    assert feature["size"].endswith(" D")
    swap = dock.findChild(QCheckBox, "cutoutPitchSwap")
    assert swap is not None and swap.isChecked() is False
    unknown_label = dock.findChild(QLabel, "cutoutUnknownLabel")
    swap_label = dock.findChild(QLabel, "cutoutPitchSwapLabel")
    assert unknown_label is not None and unknown_label.wordWrap()
    assert swap_label is not None and swap_label.wordWrap()
    assert "Unknown" in unknown_label.text()
    dock.close()
    _ = app


@pytest.mark.confirm_generic_layout
def test_ppc5_pitch_is_not_applied_to_a_pushbutton():
    """PPC5 117 × 145 is a PPC5-to-PPC5 pitch (manual §1.3), not a clearance to a 22 mm operator."""

    session = _door("Mixed")
    session.place("adc.prosense.ppc5_1102", ref="P1", x=100, y=100, sheet_id="door", snap=False)
    session.place("iec.operators.pb_no", ref="S9", x=204, y=250, sheet_id="door", snap=False)
    messages = [row.message for row in run_checks(session.project, session.catalog) if row.check == "cutout_clearance"]
    assert messages == []
    pair = _door("PPC5 pair")
    pair.place("adc.prosense.ppc5_1102", ref="P1", x=40, y=40, sheet_id="door", snap=False)
    pair.place("adc.prosense.ppc5_1102", ref="P2", x=140, y=40, sheet_id="door", snap=False)
    warned = [row.message for row in run_checks(pair.project, pair.catalog) if row.check == "cutout_clearance"]
    assert any("117" in message for message in warned)


@pytest.mark.confirm_generic_layout
def test_same_family_uses_the_larger_centre_pitch():
    """IEC Table 3 (30/50, unverified) and an 800-TD008 flush column (30×40) combine to the larger axis."""

    session = _door("Larger")
    session.place("iec.operators.pb_no", ref="S1", x=80, y=80, sheet_id="door", snap=False)
    session.place("iec.operators.pb_nc", ref="S2", x=80, y=120, sheet_id="door", snap=False)
    session.assign_part("S2", "800FP-F3", manufacturer="Allen-Bradley")
    close = [row.message for row in run_checks(session.project, session.catalog) if row.check == "cutout_clearance"]
    assert any("50" in message for message in close)
    session.project.sheet("door").component("S2").y = 130
    quiet = [row.message for row in run_checks(session.project, session.catalog) if row.check == "cutout_clearance"]
    assert quiet == []


@pytest.mark.confirm_generic_layout
def test_plain_pilot_panel_accepts_10_5_mm():
    session = _door("Pilot")
    session.place("iec.operators.pilot_light", ref="H1", x=80, y=80, sheet_id="door", snap=False)
    session.assign_part("H1", "800FD-P3N3R", manufacturer="Allen-Bradley")
    session.project.properties["cutout_panel_thickness_mm"] = 10.5
    assert not any("thickness" in row.message for row in run_checks(session.project, session.catalog))
    session.project.properties["cutout_panel_thickness_mm"] = 11
    assert any("10.5" in row.message for row in run_checks(session.project, session.catalog))


@pytest.mark.confirm_generic_layout
def test_unchecking_unknown_restores_key_and_spacing():
    from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox

    from panelcad.gui.docks import PropertiesDock

    app = QApplication.instance() or QApplication([])
    session = _door("Restore")
    session.place("iec.operators.pb_no", ref="H3", x=80, y=80, sheet_id="door", snap=False)
    session.assign_part("H3", "800FP-F3", manufacturer="Allen-Bradley")
    session.part_set_cutout("H3", include_key=True, min_center_x_mm=36, min_center_y_mm=44)
    dock = PropertiesDock()
    dock.show_component(session, "H3")
    unknown = dock.findChild(QCheckBox, "cutoutUnknown")
    assert unknown is not None
    unknown.setChecked(True)
    app.processEvents()
    props = session.project.sheet("door").component("H3").properties
    assert props["cutout"]["unknown"] is True
    assert props["cutout_restore"]["include_key"] is True
    assert props["cutout_restore"]["min_center_x_mm"] == 36
    unknown = dock.findChild(QCheckBox, "cutoutUnknown")
    assert unknown is not None and unknown.isChecked()
    shape = dock.findChild(QComboBox, "cutoutShape")
    assert shape is not None and shape.isEnabled() is False
    unknown.setChecked(False)
    app.processEvents()
    shape = dock.findChild(QComboBox, "cutoutShape")
    assert shape is not None and shape.isEnabled()
    props = session.project.sheet("door").component("H3").properties
    assert props.get("include_key") is True
    assert props.get("min_center_x_mm") == 36
    assert props.get("min_center_y_mm") == 44
    assert "cutout_restore" not in props
    feature = session.cutouts_generate({"surfaces": ["door"]})["drawings"][0]["features"][0]
    assert feature["include_key"] is True
    assert feature["size"].startswith("Ø22.3")
    assert not feature["size"].startswith("UNKNOWN")
    dock.close()
    _ = app


@pytest.mark.confirm_generic_layout
def test_inspector_enters_a_shop_rectangle():
    from PySide6.QtWidgets import QApplication, QComboBox, QDoubleSpinBox, QLineEdit

    from panelcad.gui.docks import PropertiesDock

    app = QApplication.instance() or QApplication([])
    session = _door("Shop")
    session.place("iec.operators.pb_no", ref="S1", x=80, y=80, sheet_id="door", snap=False)
    dock = PropertiesDock()
    dock.show_component(session, "S1")
    width = dock.findChild(QDoubleSpinBox, "cutoutWidth")
    height = dock.findChild(QDoubleSpinBox, "cutoutHeight")
    plus = dock.findChild(QDoubleSpinBox, "cutoutPlus")
    shape = dock.findChild(QComboBox, "cutoutShape")
    assert None not in (width, height, plus, shape)
    width.setValue(30)
    height.setValue(20)
    plus.setValue(0.5)
    shape.setCurrentIndex(shape.findData("rect"))
    app.processEvents()
    feature = session.cutouts_generate({"surfaces": ["door"]})["drawings"][0]["features"][0]
    assert feature["shape"] == "rect"
    assert feature["size"].startswith("30×20")
    stored = session.project.sheet("door").component("S1").properties["cutout"]
    assert stored["width_mm"] == 30
    assert stored["height_mm"] == 20
    assert stored["width_plus_mm"] == 0.5
    assert stored["source"] == ""
    assert "Shop" in stored["note"]
    assert not stored.get("diameter_mm")
    assert not stored.get("key_width_mm")
    assert not stored.get("min_center_a_mm")
    assert not stored.get("min_center_x_mm")
    assert not stored.get("panel_thickness_max_mm")
    assert not stored.get("centers_interchangeable")
    assert "key_side" not in stored
    assert "key_optional" not in stored
    holes = dock.findChild(QLineEdit, "cutoutHoles")
    depth = dock.findChild(QDoubleSpinBox, "cutoutDepth")
    clearance = dock.findChild(QDoubleSpinBox, "cutoutClearance")
    corner = dock.findChild(QDoubleSpinBox, "cutoutCorner")
    assert None not in (holes, depth, clearance, corner)
    holes.setText("4 -10 -8; 4 10 8")
    depth.setValue(20)
    clearance.setValue(1.5)
    corner.setValue(2)
    holes.editingFinished.emit()
    app.processEvents()
    stored = session.project.sheet("door").component("S1").properties["cutout"]
    assert stored["corner_radius_mm"] == 2
    assert stored["depth_behind_mm"] == 20
    assert stored["clearance_mm"] == 1.5
    assert len(stored["holes"]) == 2
    assert stored["holes"][0]["diameter_mm"] == 4
    agent = Agent()
    agent.session = session
    again = agent.handle(
        {
            "op": "part_set_cutout",
            "ref": "S1",
            "cutout": {"shape": "round", "diameter_mm": 30, "diameter_plus_mm": 0.2},
        }
    )
    assert again["ok"] is True
    round_feature = session.cutouts_generate({"surfaces": ["door"]})["drawings"][0]["features"][0]
    assert round_feature["size"].startswith("Ø30")
    dock.close()
    _ = app


@pytest.mark.confirm_generic_layout
def test_unknown_false_restores_from_jsonl_and_cli(tmp_path: Path):
    agent = Agent()
    assert agent.handle({"op": "create_project", "name": "Restore cli"})["ok"]
    assert agent.handle({"op": "add_sheet", "sheet": "door", "kind": "door_layout"})["ok"]
    assert agent.handle(
        {"op": "place", "library_id": "iec.operators.pb_no", "ref": "S1", "x": 80, "y": 80, "sheet": "door", "snap": False}
    )["ok"]
    assert agent.handle(
        {"op": "part_set_cutout", "ref": "S1", "include_key": True, "min_center_x_mm": 33, "min_center_y_mm": 41}
    )["ok"]
    assert agent.handle({"op": "part_set_cutout", "ref": "S1", "unknown": True})["ok"]
    restored = agent.handle({"op": "part_set_cutout", "ref": "S1", "unknown": False})
    assert restored["ok"] is True
    props = agent.session.project.sheet("door").component("S1").properties
    assert props.get("include_key") is True
    assert props.get("min_center_x_mm") == 33
    assert "cutout_restore" not in props
    project = tmp_path / "restore.pcad.yaml"
    agent.session.save(project)
    loaded = Session.open(project)
    loaded.part_set_cutout("S1", unknown=True)
    loaded.save(project)
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    result = CliRunner().invoke(app, ["cutouts", "set", str(project), "S1", "--no-unknown"])
    assert result.exit_code == 0, result.output
    props = Session.open(project).project.sheet("door").component("S1").properties
    assert props.get("include_key") is True
    assert props.get("min_center_x_mm") == 33


@pytest.mark.confirm_generic_layout
def test_cutout_exports_log_the_path(tmp_path: Path):
    from panelcad.diag.log import configure_logging, log_file_path

    configure_logging()
    session = _door("Log")
    session.place("iec.operators.pb_no", ref="S1", x=80, y=80, sheet_id="door", snap=False)
    folder = tmp_path / "dxf"
    session.cutouts_export_dxf(folder)
    pdf = tmp_path / "cutouts.pdf"
    session.cutouts_export_pdf(pdf, {"surfaces": ["door"]})
    text = log_file_path().read_text(encoding="utf-8")
    assert "cutouts_export_dxf " in text
    assert "cutouts_export_pdf " in text
    assert pdf.name in text
    assert "ECS" not in text


@pytest.mark.confirm_generic_layout
def test_tolerance_edit_is_a_shop_value():
    from PySide6.QtWidgets import QApplication, QDoubleSpinBox

    from panelcad.gui.docks import PropertiesDock

    app = QApplication.instance() or QApplication([])
    session = _door("Tol")
    session.place("iec.operators.pb_no", ref="H3", x=80, y=80, sheet_id="door", snap=False)
    session.assign_part("H3", "800FP-F3", manufacturer="Allen-Bradley")
    dock = PropertiesDock()
    dock.show_component(session, "H3")
    plus = dock.findChild(QDoubleSpinBox, "cutoutPlus")
    minus = dock.findChild(QDoubleSpinBox, "cutoutMinus")
    assert plus is not None and minus is not None
    assert "—" not in minus.text()
    assert minus.value() == 0
    plus.setValue(1.0)
    plus.editingFinished.emit()
    app.processEvents()
    stored = session.project.sheet("door").component("H3").properties["cutout"]
    assert stored["diameter_plus_mm"] == 1.0
    assert stored["diameter_mm"] == 22.3
    assert stored["source"] == ""
    assert stored["note"].startswith("Shop opening")
    assert "800-td008" not in stored["note"].lower()
    assert not stored.get("panel_thickness_max_mm")
    assert stored["verified"] is True
    dock.close()
    _ = app


@pytest.mark.confirm_generic_layout
def test_cross_family_gap_uses_the_smaller_opening_clearance():
    """A 3 mm gap warns. The PPC5 117 mm pitch does not, and a ~10 mm gap stays quiet."""

    close = _door("Gap")
    close.place("adc.prosense.ppc5_1102", ref="P1", x=100, y=100, sheet_id="door", snap=False)
    # Centres: PPC5 (148, 148), button (148, 208.15). Cut gap = 46 + 11.15 + 3.
    close.place("iec.operators.pb_no", ref="S5", x=137, y=197.15, sheet_id="door", snap=False)
    warned = [row.message for row in run_checks(close.project, close.catalog) if row.check == "cutout_clearance" and "S5" in row.message]
    assert warned
    assert any("117" not in message and "145" not in message for message in warned)
    assert any("30" in message for message in warned)
    far = _door("Far gap")
    far.place("adc.prosense.ppc5_1102", ref="P1", x=100, y=100, sheet_id="door", snap=False)
    far.place("iec.operators.pb_no", ref="S9", x=204, y=250, sheet_id="door", snap=False)
    quiet = [row.message for row in run_checks(far.project, far.catalog) if row.check == "cutout_clearance"]
    assert quiet == []
    edged = _door("Edge")
    edged.place("adc.prosense.ppc5_1102", ref="P1", x=100, y=100, sheet_id="door", snap=False)
    edged.place("iec.operators.pb_no", ref="S5", x=137, y=202.15, sheet_id="door", snap=False)
    edged.project.properties["cutout_edge_clearance_mm"] = 10
    edged_msgs = [
        row.message for row in run_checks(edged.project, edged.catalog) if row.check == "cutout_clearance" and "S5" in row.message
    ]
    assert any("edge clearance" in message for message in edged_msgs)


@pytest.mark.confirm_generic_layout
def test_narrow_inspector_label_grows():
    from PySide6.QtWidgets import QApplication, QLabel

    from panelcad.gui.docks import PropertiesDock

    app = QApplication.instance() or QApplication([])
    session = _door("Narrow")
    session.place("iec.operators.pb_no", ref="S1", x=80, y=80, sheet_id="door", snap=False)
    dock = PropertiesDock()
    dock.resize(205, 700)
    dock.show()
    dock.show_component(session, "S1")
    app.processEvents()
    label = dock.findChild(QLabel, "cutoutPitchSwapLabel")
    assert label is not None
    host = label.parentWidget()
    assert host is not None and host.objectName() == "cutoutPitchSwapRow"
    assert "swapped" in label.text()
    assert host.hasHeightForWidth()
    assert label.height() + 2 >= label.heightForWidth(max(20, label.width()))
    assert "pitches" in label.text()
    dock.close()
    _ = app


@pytest.mark.confirm_generic_layout
def test_jsonl_false_is_false_and_pdf_and_cli_centres(tmp_path: Path):
    agent = Agent()
    assert agent.handle({"op": "create_project", "name": "Bool"})["ok"]
    assert agent.handle({"op": "add_sheet", "sheet": "door", "kind": "door_layout"})["ok"]
    assert agent.handle(
        {"op": "place", "library_id": "iec.operators.pb_no", "ref": "S1", "x": 80, "y": 80, "sheet": "door", "snap": False}
    )["ok"]
    marked = agent.handle({"op": "part_set_cutout", "ref": "S1", "unknown": "false"})
    assert marked["ok"] is True
    props = agent.session.project.sheet("door").component("S1").properties
    assert not (isinstance(props.get("cutout"), dict) and props["cutout"].get("unknown"))
    assert agent.handle({"op": "part_set_cutout", "ref": "S1", "unknown": "true"})["ok"]
    assert agent.session.project.sheet("door").component("S1").properties["cutout"]["unknown"] is True
    restored = agent.handle({"op": "part_set_cutout", "ref": "S1", "unknown": "false"})
    assert restored["ok"] is True
    assert "cutout_restore" not in agent.session.project.sheet("door").component("S1").properties
    keyed = agent.handle({"op": "part_set_cutout", "ref": "S1", "include_key": "false"})
    assert keyed["ok"] is True
    assert agent.session.project.sheet("door").component("S1").properties["include_key"] is False
    bad = agent.handle({"op": "part_set_cutout", "ref": "S1", "unknown": "nope"})
    assert bad["ok"] is False
    pdf = agent.handle({"op": "cutouts_export_pdf", "path": str(tmp_path / "sheets.pdf"), "surfaces": "door"})
    assert pdf["ok"] is True, pdf
    assert Path(pdf["result"]["path"]).is_file()
    project = tmp_path / "centres.pcad.yaml"
    agent.session.save(project)
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    result = CliRunner().invoke(
        app,
        [
            "cutouts",
            "set",
            str(project),
            "S1",
            "--min-center-x",
            "36",
            "--min-center-y",
            "44",
            "--no-centers-interchangeable",
        ],
    )
    assert result.exit_code == 0, result.output
    saved = Session.open(project).project.sheet("door").component("S1").properties
    assert saved["min_center_x_mm"] == 36
    assert saved["min_center_y_mm"] == 44
    assert saved["centers_interchangeable"] is False


@pytest.mark.confirm_generic_layout
def test_pdf_title_lists_every_source(tmp_path: Path):
    from PySide6.QtWidgets import QApplication

    from panelcad.render.print_package import extract_pdf_text

    app = QApplication.instance() or QApplication([])
    session = _door("Sources")
    session.place("iec.operators.pb_no", ref="S1", x=40, y=40, sheet_id="door", snap=False)
    session.place("iec.operators.pb_nc", ref="S2", x=80, y=40, sheet_id="door", snap=False)
    session.assign_part("S2", "800FP-F3", manufacturer="Allen-Bradley")
    session.place("adc.prosense.ppc5_1102", ref="P1", x=140, y=140, sheet_id="door", snap=False)
    pdf = tmp_path / "sources.pdf"
    session.cutouts_export_pdf(pdf, {"surfaces": ["door"]})
    text = extract_pdf_text(pdf)
    assert "UNVERIFIED" in text
    assert "is.iec.60947.5.1.2003.pdf" in text
    assert "800f-in016" in text
    assert "ppc5manual.pdf" in text
    _ = app


def _settings_row_heights(dialog) -> list[int]:
    from PySide6.QtWidgets import QWidget

    names = (
        "cutoutDatum",
        "cutoutStyle",
        "cutoutView",
        "cutoutUnits",
        "cutoutEdge",
        "cutoutFlange",
        "cutoutThickness",
    )
    heights = []
    for name in names:
        widget = dialog.findChild(QWidget, name)
        assert widget is not None, name
        heights.append(widget.height())
    return heights


@pytest.mark.confirm_generic_layout
def test_cutout_dialog_fits_800x600():
    from PySide6.QtWidgets import QApplication, QPushButton, QScrollArea, QWidget

    from panelcad.gui.cutout_dialog import CutoutDialog

    app = QApplication.instance() or QApplication([])
    session = _door("Dialog")
    session.place("iec.operators.pb_no", ref="S1", x=80, y=80, sheet_id="door", snap=False)
    dialog = CutoutDialog(session)
    dialog.resize(800, 600)
    dialog.show()
    app.processEvents()
    assert dialog.height() <= 600
    assert dialog.width() <= 800
    buttons = [button for button in dialog.findChildren(QPushButton) if button.isVisible()]
    assert buttons
    for button in buttons:
        assert button.geometry().top() >= dialog.preview_scroll.geometry().bottom() - 1 or button.objectName() == "cutoutZoomFit"
        assert button.geometry().bottom() <= dialog.height()
    assert dialog.preview_scroll.geometry().bottom() <= dialog.status.geometry().top() + 1
    assert min(_settings_row_heights(dialog)) >= 22
    host = dialog.findChild(QWidget, "cutoutSettings")
    scroll = dialog.findChild(QScrollArea, "cutoutSettingsScroll")
    assert host is not None and scroll is not None
    assert scroll.viewport().height() + 2 >= host.height()
    _assert_preview_fits(dialog)
    dialog.resize(640, 420)
    app.processEvents()
    assert min(_settings_row_heights(dialog)) >= 22
    assert scroll.viewport().height() + 2 < host.height()
    # The viewport is under 80 px here. The fit must use that size, not an 80 px floor.
    _assert_preview_fits(dialog)
    dialog.close()
    _ = app


def _assert_preview_fits(dialog) -> None:
    pixmap = dialog.preview.pixmap()
    assert pixmap is not None and not pixmap.isNull()
    viewport = dialog.preview_scroll.viewport().size()
    assert viewport.width() > 2 and viewport.height() > 2
    assert pixmap.width() <= viewport.width()
    assert pixmap.height() <= viewport.height()
    assert dialog._preview_image is not None
    assert pixmap.width() < dialog._preview_image.width() or pixmap.height() < dialog._preview_image.height()


@pytest.mark.confirm_generic_layout
def test_preview_fits_on_open_and_grows_on_a_large_screen():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.cutout_dialog import CutoutDialog, dialog_size_for_screen

    width, height = dialog_size_for_screen(1920, 1080)
    assert width > 860
    assert height > 700
    small_w, small_h = dialog_size_for_screen(800, 600)
    assert small_w <= 800
    assert small_h <= 600
    app = QApplication.instance() or QApplication([])
    session = _door("Large")
    session.place("iec.operators.pb_no", ref="S1", x=80, y=80, sheet_id="door", snap=False)
    dialog = CutoutDialog(session)
    dialog.resize(width, height)
    dialog.show()
    app.processEvents()
    _assert_preview_fits(dialog)
    assert dialog.preview_scroll.viewport().height() > 400
    dialog.resize(800, 600)
    app.processEvents()
    assert min(_settings_row_heights(dialog)) >= 22
    _assert_preview_fits(dialog)
    dialog.close()
    _ = app


@pytest.mark.confirm_generic_layout
def test_standard_600x800_door_stays_one_to_one(tmp_path: Path):
    """Review sample: 600×800 door, three sources, one close ordinate. Stays SCALE 1:1."""

    from PySide6.QtWidgets import QApplication

    from panelcad.core.layout import footprint_size
    from panelcad.render.print_package import extract_pdf_text

    app = QApplication.instance() or QApplication([])
    session = Session.create("Sample door")
    session.add_sheet("door", "Door", kind="door_layout")
    session.set_enclosure(width_mm=600, height_mm=800, depth_mm=300, x=0, y=0, sheet_id="door")
    session.set_title_block(company="ECS", drawing_number="CUT-36", rev="C")

    def at(lib_id: str, ref: str, cx: float, cy: float) -> None:
        device = session.catalog.get(lib_id)
        width, height = footprint_size(device, None)
        session.place(lib_id, ref=ref, x=cx - width / 2.0, y=cy - height / 2.0, sheet_id="door", snap=False)

    at("iec.operators.pb_no", "S1", 111, 111)
    at("iec.operators.pb_nc", "S2", 131, 111)
    at("iec.operators.pb_no", "H3", 161, 261)
    session.assign_part("H3", "800FP-F3", manufacturer="Allen-Bradley")
    at("iec.operators.pb_nc", "H4", 261, 261)
    session.assign_part("H4", "800FP-F4", manufacturer="Allen-Bradley")
    at("adc.prosense.ppc5_1102", "P1", 198, 498)
    at("adc.prosense.ppc5_1102", "P2", 298, 498)
    at("adc.prosense.ppc5_1102", "P3", 448, 498)
    at("iec.operators.pb_no", "S4", 461, 261)
    session.part_set_cutout(
        "S4",
        {
            "shape": "rect",
            "width_mm": 30,
            "height_mm": 20,
            "width_plus_mm": 0.5,
            "height_plus_mm": 0.5,
            "corner_radius_mm": 2,
            "source": "",
            "verified": True,
            "note": "Shop opening entered on the device. Not a cited manufacturer drawing.",
        },
    )
    at("iec.operators.pb_no", "S9", 131, 611)
    at("iec.operators.pb_no", "S8", 461, 661)
    at("iec.hmi.panel_7in", "HMI1", 400, 693)
    drawing = session.cutout_drawings({"surfaces": ["door"]})[0]
    assert drawing.scale_denom == 1.0
    assert abs(drawing.page_width_mm - 784.0) < 0.2
    assert abs(drawing.page_height_mm - 892.0) < 0.2
    assert sum(1 for line in drawing.sources if line.startswith("UNVERIFIED ")) == 1
    assert len(drawing.sources) == 3
    pdf = tmp_path / "sample.pdf"
    session.cutouts_export_pdf(pdf, {"surfaces": ["door"]})
    text = extract_pdf_text(pdf)
    assert "SCALE 1:1" in text
    assert "SCALE 1:2" not in text
    assert "UNVERIFIED" in text
    assert "is.iec.60947.5.1.2003.pdf" in text
    assert "800f-in016" in text
    assert "ppc5manual.pdf" in text
    _ = app


@pytest.mark.confirm_generic_layout
def test_iec_tolerance_keeps_unverified_pitch():
    from PySide6.QtWidgets import QApplication, QDoubleSpinBox, QLabel

    from panelcad.gui.docks import PropertiesDock

    app = QApplication.instance() or QApplication([])
    session = _door("IEC tol")
    session.place("iec.operators.pb_no", ref="S9", x=80, y=80, sheet_id="door", snap=False)
    dock = PropertiesDock()
    dock.show_component(session, "S9")
    plus = dock.findChild(QDoubleSpinBox, "cutoutPlus")
    assert plus is not None
    plus.setValue(0.5)
    plus.editingFinished.emit()
    app.processEvents()
    stored = session.project.sheet("door").component("S9").properties["cutout"]
    assert stored["diameter_plus_mm"] == 0.5
    assert stored["source"] == ""
    assert stored["note"].startswith("Shop opening")
    assert stored["min_center_a_mm"] == 30
    assert stored["min_center_b_mm"] == 50
    assert stored["centers_interchangeable"] is True
    assert stored["verified"] is False
    dock.show_component(session, "S9")
    app.processEvents()
    mark = dock.findChild(QLabel, "cutoutUnverified")
    assert mark is not None and mark.text() == "UNVERIFIED"
    session.place("adc.prosense.ppc5_1102", ref="P1", x=100, y=100, sheet_id="door", snap=False)
    session.project.sheet("door").component("S9").x = 137
    session.project.sheet("door").component("S9").y = 197.15
    warned = [
        row.message
        for row in run_checks(session.project, session.catalog)
        if row.check == "cutout_clearance" and "S9" in row.message
    ]
    assert warned
    assert any("not checked on an official publication" in message for message in warned)
    dock.close()
    _ = app


@pytest.mark.confirm_generic_layout
def test_rotated_rect_tag_stays_outside_the_cut(tmp_path: Path):
    """A 60×20 opening turned 90° reaches 30 mm up. The tag stays above that."""

    from PySide6.QtWidgets import QApplication

    from panelcad.render.cutout_draw import _page, render_cutout_qimage

    app = QApplication.instance() or QApplication([])
    session = _door("Rotated tag")
    session.place(
        "iec.operators.pb_no", ref="S4", x=150, y=180, sheet_id="door", snap=False, rotation=90
    )
    session.part_set_cutout(
        "S4",
        {"shape": "rect", "width_mm": 60, "height_mm": 20, "source": "shop note"},
    )
    drawing = session.cutout_drawings({"surfaces": ["door"]})[0]
    feature = next(item for item in drawing.features if item.ref == "S4")
    assert feature.rotation == 90

    path = session.cutouts_export_dxf(tmp_path / "rot", {"surfaces": ["door"]})["paths"][0]
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    texts = [
        entity
        for entity in msp
        if entity.dxftype() == "TEXT"
        and entity.dxf.text == "S4"
        and entity.dxf.layer == "TEXT"
        and int(entity.dxf.valign or 0) == 2
    ]
    assert len(texts) == 1
    text = texts[0]
    point = text.dxf.align_point
    xs: list[float] = []
    ys: list[float] = []
    for entity in msp:
        if entity.dxftype() == "LWPOLYLINE" and entity.dxf.layer == "CUT":
            for x, y, *_rest in entity.get_points("xy"):
                xs.append(float(x))
                ys.append(float(y))
    assert xs and ys
    assert float(point.y) > max(ys) + 2.0
    assert min(xs) - 2.0 <= float(point.x) <= max(xs) + 2.0

    image = render_cutout_qimage(drawing, session.project, px_per_mm=4)
    vx, vy = feature.view_center(drawing.panel_width_mm, drawing.view)
    cx, cy = _page(drawing, vx, vy)
    ppm = 4.0
    x0 = max(0, int(round((cx - 12) * ppm)))
    x1 = min(image.width(), int(round((cx + 12) * ppm)))
    y0 = max(0, int(round((cy - 48) * ppm)))
    y1 = min(image.height(), int(round((cy + 40) * ppm)))
    reds: list[int] = []
    inks: list[int] = []
    for y in range(y0, y1):
        for x in range(x0, x1):
            color = image.pixelColor(x, y)
            red, green, blue = color.red(), color.green(), color.blue()
            if red > 80 and red > green * 1.4 and red > blue:
                reds.append(y)
            elif red < 70 and green < 70 and blue < 70:
                inks.append(y)
    assert reds
    assert inks
    assert max(inks) < min(reds) - int(1.5 * ppm)
    _ = app


@pytest.mark.confirm_generic_layout
def test_pitch_swap_conflict_is_an_error(tmp_path: Path):
    agent = Agent()
    assert agent.handle({"op": "create_project", "name": "Swap"})["ok"]
    assert agent.handle({"op": "add_sheet", "sheet": "door", "kind": "door_layout"})["ok"]
    assert agent.handle(
        {"op": "place", "library_id": "iec.operators.pb_no", "ref": "S1", "x": 80, "y": 80, "sheet": "door", "snap": False}
    )["ok"]
    conflict = agent.handle(
        {"op": "part_set_cutout", "ref": "S1", "pitch_swap": True, "centers_interchangeable": False}
    )
    assert conflict["ok"] is False
    assert "disagree" in conflict["error"]
    props = agent.session.project.sheet("door").component("S1").properties
    assert "centers_interchangeable" not in props
    agreed = agent.handle(
        {"op": "part_set_cutout", "ref": "S1", "pitch_swap": False, "centers_interchangeable": False}
    )
    assert agreed["ok"] is True
    assert agent.session.project.sheet("door").component("S1").properties["centers_interchangeable"] is False
    only = agent.handle({"op": "part_set_cutout", "ref": "S1", "pitch_swap": True})
    assert only["ok"] is True
    assert agent.session.project.sheet("door").component("S1").properties["centers_interchangeable"] is True

    project = tmp_path / "swap.pcad.yaml"
    agent.session.save(project)
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    bad = CliRunner().invoke(
        app,
        ["cutouts", "set", str(project), "S1", "--pitch-swap", "--no-centers-interchangeable"],
    )
    assert bad.exit_code != 0
    assert "disagree" in bad.output
    stored = Session.open(project).project.sheet("door").component("S1").properties
    assert stored["centers_interchangeable"] is True
    good = CliRunner().invoke(
        app,
        ["cutouts", "set", str(project), "S1", "--no-pitch-swap", "--no-centers-interchangeable"],
    )
    assert good.exit_code == 0, good.output
    stored = Session.open(project).project.sheet("door").component("S1").properties
    assert stored["centers_interchangeable"] is False


def test_cli_pitch_swap_help_is_not_clipped():
    import re

    from typer.testing import CliRunner

    from panelcad.cli.main import app

    result = CliRunner().invoke(app, ["cutouts", "set", "--help"], env={"COLUMNS": "80"})
    assert result.exit_code == 0, result.output
    # Rich paints the trailing switch inside a long flag, so the escape codes
    # sit between "--pitch" and "-swap". Judge the text a terminal shows.
    text = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "--pitch-swap" in text
    assert "--no-pitch-swap" in text
    assert "…" not in text
    assert "--centers-interchan" not in text


@pytest.mark.confirm_generic_layout
def test_shrunk_package_keeps_title_and_table_readable(tmp_path: Path):
    from PySide6.QtWidgets import QApplication

    from panelcad.render.cutout_draw import needs_package_legend
    from panelcad.render.print_package import extract_pdf_text

    app = QApplication.instance() or QApplication([])
    assert needs_package_legend(0.4)
    assert not needs_package_legend(1.0)
    session = _door("Letter")
    session.place("iec.operators.pb_no", ref="S1", x=80, y=80, sheet_id="door", snap=False)
    dest = tmp_path / "letter.pdf"
    session.print_package(dest, sections=["cutouts"], paper="letter")
    text = extract_pdf_text(dest)
    assert "NOT TO SCALE" in text
    assert "Print the 1:1 cutout sheet or the DXF." in text
    assert "S1" in text
    assert "UNVERIFIED" in text
    assert "is.iec.60947.5.1.2003.pdf" in text
    _ = app


def _floor_door(box_id: str):
    """A catalog floor box fitted to the door page at a shop ratio."""

    from panelcad.core.layout import center_in_frame, fit_standard_enclosure_scale
    from panelcad.library.enclosures import load_enclosures
    from panelcad.render.titleblock import working_rect

    box = next(item for item in load_enclosures().boxes if item.id == box_id)
    session = Session.create(box_id)
    session.add_sheet("door", "Door", kind="door_layout")
    sheet = session.project.sheet("door")
    wx, wy, ww, wh = working_rect(sheet)
    scale = fit_standard_enclosure_scale(box.width_mm, box.height_mm, ww, wh)
    ox, oy = center_in_frame((wx, wy, ww, wh), box.width_mm * scale, box.height_mm * scale)
    session.set_enclosure(
        width_mm=box.width_mm,
        height_mm=box.height_mm,
        depth_mm=box.depth_mm,
        x=ox,
        y=oy,
        name=box_id,
        sheet_id="door",
    )
    sheet.enclosure.draw_scale = scale
    return session, scale, box


def _place_true_center(session: Session, scale: float, library_id: str, ref: str, cx: float, cy: float):
    """Corner in sheet millimetres so the catalog centre lands on cx, cy true mm."""

    from panelcad.library.panel import effective_footprint

    device = session.catalog.get(library_id)
    body = effective_footprint(device)
    enc = session.project.sheet("door").enclosure
    assert enc is not None
    session.place(
        library_id,
        ref=ref,
        x=enc.x + (cx - body.width_mm / 2.0) * scale,
        y=enc.y + (cy - body.height_mm / 2.0) * scale,
        sheet_id="door",
        snap=False,
        scale=scale,
    )
    return body


@pytest.mark.parametrize(
    ("box_id", "ratio"),
    [("floor-72x48x18-n12", 0.125), ("floor-90x48x18-n12", 0.1)],
)
@pytest.mark.confirm_generic_layout
def test_floor_door_cutouts_stay_on_catalog_millimetres(box_id: str, ratio: float, tmp_path: Path):
    """1:8 and 1:10 must not use the 0.15 on-screen clamp.

    Shift if the clamp leaks in: catalog_mm * (0.15 / draw_scale - 1) / 2.
    """

    from PySide6.QtWidgets import QApplication

    from panelcad.render.print_package import extract_pdf_text

    app = QApplication.instance() or QApplication([])
    session, scale, _box = _floor_door(box_id)
    assert scale == pytest.approx(ratio)
    button = _place_true_center(session, scale, "iec.operators.pb_no", "S1", 200, 400)
    estop = _place_true_center(session, scale, "iec.operators.estop", "ES", 500, 900)
    assert session.project.sheet("door").component("S1").scale == pytest.approx(ratio)
    assert session.project.sheet("door").component("S1").visual_scale() == pytest.approx(0.15)
    drawing = session.cutout_drawings({"surfaces": ["door"]})[0]
    features = {item.ref: item for item in drawing.features}
    assert features["S1"].cx == pytest.approx(200)
    assert features["S1"].cy == pytest.approx(400)
    assert features["S1"].footprint_w == pytest.approx(button.width_mm)
    assert features["S1"].footprint_h == pytest.approx(button.height_mm)
    assert features["ES"].cx == pytest.approx(500)
    assert features["ES"].cy == pytest.approx(900)
    assert features["ES"].footprint_w == pytest.approx(estop.width_mm)
    assert features["ES"].footprint_h == pytest.approx(estop.height_mm)
    assert features["S1"].cutout.diameter_mm == pytest.approx(22.3)
    assert features["ES"].cutout.diameter_mm == pytest.approx(22.3)
    shift = 0.15 / ratio - 1.0
    holes = {row.tag: row for row in drawing.rows}
    assert holes["S1"].x_mm == pytest.approx(200)
    assert holes["S1"].y_mm == pytest.approx(400)
    assert holes["ES"].x_mm == pytest.approx(500)
    assert holes["ES"].y_mm == pytest.approx(900)

    pdf = tmp_path / "floor.pdf"
    session.cutouts_export_pdf(pdf, {"surfaces": ["door"]})
    text = extract_pdf_text(pdf)
    for bad in (
        f"{200 + button.width_mm * shift / 2.0:.1f}",
        f"{400 + button.height_mm * shift / 2.0:.1f}",
        f"{500 + estop.width_mm * shift / 2.0:.1f}",
        f"{900 + estop.height_mm * shift / 2.0:.1f}",
    ):
        assert bad not in text
    assert "200" in text and "400" in text and "500" in text and "900" in text

    path = session.cutouts_export_dxf(tmp_path / "floor", {"surfaces": ["door"]})["paths"][0]
    doc = ezdxf.readfile(path)
    arcs = [entity for entity in doc.modelspace() if entity.dxftype() == "ARC" and entity.dxf.layer == "CUT"]
    height = drawing.panel_height_mm

    def _at(cx: float, cy: float):
        return next(
            entity
            for entity in arcs
            if abs(entity.dxf.center.x - cx) < 0.05 and abs(entity.dxf.center.y - (height - cy)) < 0.05
        )

    s1 = _at(200, 400)
    es = _at(500, 900)
    assert s1.dxf.radius == pytest.approx(11.15)
    assert es.dxf.radius == pytest.approx(11.15)
    quiet = [
        row
        for row in run_checks(session.project, session.catalog)
        if row.ref in {"S1", "ES"} and str(row.check).startswith("cutout")
    ]
    assert quiet == []
    _ = app


@pytest.mark.confirm_generic_layout
def test_600x800_door_centers_stay_exact(tmp_path: Path):
    """SCALE 1:1. S1 at 111,111 and ES at 200,300. DXF arc at 111,689 r11.15."""

    session = Session.create("Exact door")
    session.add_sheet("door", "Door", kind="door_layout")
    session.set_enclosure(width_mm=600, height_mm=800, depth_mm=300, x=0, y=0, sheet_id="door")
    assert session.project.sheet("door").enclosure.draw_scale == pytest.approx(1.0)
    _place_true_center(session, 1.0, "iec.operators.pb_no", "S1", 111, 111)
    _place_true_center(session, 1.0, "iec.operators.estop", "ES", 200, 300)
    drawing = session.cutout_drawings({"surfaces": ["door"]})[0]
    assert drawing.scale_denom == pytest.approx(1.0)
    features = {item.ref: item for item in drawing.features}
    assert features["S1"].cx == pytest.approx(111)
    assert features["S1"].cy == pytest.approx(111)
    assert features["ES"].cx == pytest.approx(200)
    assert features["ES"].cy == pytest.approx(300)
    assert features["S1"].footprint_w == pytest.approx(22)
    assert features["ES"].footprint_w == pytest.approx(40)
    assert features["S1"].cutout.diameter_mm == pytest.approx(22.3)
    assert features["ES"].cutout.diameter_mm == pytest.approx(22.3)
    path = session.cutouts_export_dxf(tmp_path / "exact", {"surfaces": ["door"]})["paths"][0]
    doc = ezdxf.readfile(path)
    arcs = [entity for entity in doc.modelspace() if entity.dxftype() == "ARC" and entity.dxf.layer == "CUT"]
    s1 = next(entity for entity in arcs if abs(entity.dxf.center.x - 111) < 0.05)
    assert s1.dxf.center.y == pytest.approx(689)
    assert s1.dxf.radius == pytest.approx(11.15)


def _door_with_manual_operator(x: float, y: float) -> Session:
    """Landscape door, portrait panel box, one operator already on the door."""

    session = Session.create("Manual door")
    session.add_sheet("panel", kind="panel_layout")
    session.set_enclosure(width_mm=261, height_mm=348, depth_mm=200, x=18, y=18, sheet_id="panel")
    session.add_sheet("door", kind="door_layout")
    session.place("iec.operators.pb_no", ref="S9", x=x, y=y, sheet_id="door", snap=False)
    return session


def _assert_manual_operator_unchanged(session: Session, x: float, y: float) -> None:
    from panelcad.library.panel import effective_footprint

    door = session.project.sheet("door")
    assert door.enclosure is None
    part = door.component("S9")
    assert part.x == pytest.approx(x)
    assert part.y == pytest.approx(y)
    assert part.scale == pytest.approx(1.0)
    body = effective_footprint(session.catalog.get(part.library_id), part)
    drawing = next(item for item in session.cutout_drawings({"surfaces": ["door"]}) if item.surface == "door")
    assert drawing.outline_set is False
    feature = next(item for item in drawing.features if item.ref == "S9")
    assert feature.cx == pytest.approx(x + body.width_mm / 2.0)
    assert feature.cy == pytest.approx(y + body.height_mm / 2.0)
    assert feature.cx > 0 and feature.cy > 0
    assert feature.footprint_w == pytest.approx(body.width_mm)
    assert feature.footprint_h == pytest.approx(body.height_mm)
    messages = [row.message for row in run_checks(session.project, session.catalog) if row.ref == "S9"]
    assert not any("crosses" in message for message in messages)
    assert not any("catalog body" in message for message in messages)
    assert any(
        "no enclosure outline" in row.message
        for row in run_checks(session.project, session.catalog)
        if row.check == "cutout_edge"
    )


@pytest.mark.confirm_generic_layout
def test_insert_leaves_a_manual_door_part_inside_the_new_rect():
    """S9 at sheet 180,100 must not become 102.2, 142.8 with a 32.7 mm body."""

    session = _door_with_manual_operator(180, 100)
    session.insert_circuit("dol", control="local", kw=1.5)
    _assert_manual_operator_unchanged(session, 180, 100)
    feature = next(
        item
        for item in session.cutout_drawings({"surfaces": ["door"]})[0].features
        if item.ref == "S9"
    )
    assert abs(feature.cx - 102.2) > 1
    assert abs(feature.cy - 142.8) > 1
    assert abs(feature.footprint_w - 32.7) > 1


@pytest.mark.confirm_generic_layout
def test_insert_leaves_a_manual_door_part_outside_the_new_rect():
    """S9 at sheet 80,90 must not be rewritten to a negative drill coordinate."""

    session = _door_with_manual_operator(80, 90)
    session.insert_circuit("dol", control="local", kw=1.5)
    _assert_manual_operator_unchanged(session, 80, 90)
    feature = next(
        item
        for item in session.cutout_drawings({"surfaces": ["door"]})[0].features
        if item.ref == "S9"
    )
    assert feature.cx > 0
    assert abs(feature.cx - (-46.5)) > 1


@pytest.mark.confirm_generic_layout
def test_copied_box_without_rescaling_is_a_check_finding():
    """The old mirror left the part put. That state has to raise a check."""

    from panelcad.core.layout import fit_enclosure_scale
    from panelcad.core.models import Enclosure
    from panelcad.render.titleblock import working_rect

    session = _door_with_manual_operator(80, 90)
    door = session.project.sheet("door")
    panel = session.project.sheet("panel").enclosure
    assert panel is not None
    fx, fy, fw, fh = working_rect(door)
    scale = fit_enclosure_scale(panel.width_mm, panel.height_mm, fw, fh)
    drawn_w = panel.width_mm * scale
    drawn_h = panel.height_mm * scale
    door.enclosure = Enclosure(
        x=fx + max(0.0, (fw - drawn_w) / 2.0),
        y=fy + max(0.0, (fh - drawn_h) / 2.0),
        width_mm=panel.width_mm,
        height_mm=panel.height_mm,
        depth_mm=panel.depth_mm,
        draw_scale=scale,
    )
    feature = next(
        item
        for item in session.cutout_drawings({"surfaces": ["door"]})[0].features
        if item.ref == "S9"
    )
    assert feature.cx < 0
    assert feature.cx == pytest.approx(-46.5, abs=0.2)
    assert feature.footprint_w == pytest.approx(32.7, abs=0.2)
    found = [
        row.message
        for row in run_checks(session.project, session.catalog)
        if row.ref == "S9" and row.check == "cutout_edge"
    ]
    assert any("crosses" in message for message in found)
    assert any("catalog body" in message for message in found)

    inside = _door_with_manual_operator(180, 100)
    door = inside.project.sheet("door")
    panel = inside.project.sheet("panel").enclosure
    assert panel is not None
    fx, fy, fw, fh = working_rect(door)
    scale = fit_enclosure_scale(panel.width_mm, panel.height_mm, fw, fh)
    drawn_w = panel.width_mm * scale
    drawn_h = panel.height_mm * scale
    door.enclosure = Enclosure(
        x=fx + max(0.0, (fw - drawn_w) / 2.0),
        y=fy + max(0.0, (fh - drawn_h) / 2.0),
        width_mm=panel.width_mm,
        height_mm=panel.height_mm,
        depth_mm=panel.depth_mm,
        draw_scale=scale,
    )
    feature = next(
        item
        for item in inside.cutout_drawings({"surfaces": ["door"]})[0].features
        if item.ref == "S9"
    )
    assert feature.cx == pytest.approx(102.2, abs=0.2)
    assert feature.cy == pytest.approx(142.8, abs=0.2)
    assert feature.footprint_w == pytest.approx(32.7, abs=0.2)
    found = [
        row.message
        for row in run_checks(inside.project, inside.catalog)
        if row.ref == "S9" and row.check == "cutout_edge"
    ]
    assert any("catalog body" in message for message in found)


def _door_drawn_at(scale: float) -> Session:
    session = Session.create("Scaled drop")
    session.add_sheet("door", kind="door_layout")
    session.set_enclosure(width_mm=1800, height_mm=2200, depth_mm=400, x=20, y=20, sheet_id="door")
    session.project.sheet("door").enclosure.draw_scale = scale
    session.set_active_sheet("door")
    return session


@pytest.mark.confirm_generic_layout
def test_session_place_on_scaled_door_uses_draw_scale():
    session = _door_drawn_at(0.12)
    comp = session.place("iec.operators.pb_no", ref="S1", x=40, y=40, snap=False)
    assert comp.scale == pytest.approx(0.12)
    feature = next(item for item in session.cutout_drawings({"surfaces": ["door"]})[0].features if item.ref == "S1")
    assert feature.footprint_w == pytest.approx(22)
    assert feature.footprint_h == pytest.approx(22)
    assert feature.footprint_w < 30
    session.set_active_sheet(session.project.sheets[0].id)
    schematic = session.place("iec.operators.pb_nc", ref="S0", x=40, y=40)
    assert schematic.scale == pytest.approx(1.0)
    copied = session.place_from_bom("iec.operators.pb_nc", ref="S0", x=80, y=80, sheet_id="door")
    assert copied["placed"] is True
    assert session.project.sheet("door").component("S0").scale == pytest.approx(0.12)


@pytest.mark.confirm_generic_layout
def test_gui_canvas_drop_on_scaled_door_uses_draw_scale():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import SchematicView

    app = QApplication.instance() or QApplication([])
    session = _door_drawn_at(0.12)
    canvas = SchematicView(session)
    canvas.place_at("iec.operators.pb_no", 48, 36)
    comp = session.sheet("door").components[-1]
    assert comp.scale == pytest.approx(0.12)
    feature = next(
        item for item in session.cutout_drawings({"surfaces": ["door"]})[0].features if item.ref == comp.id
    )
    assert feature.footprint_w == pytest.approx(22)
    assert feature.footprint_h == pytest.approx(22)
    canvas.deleteLater()
    _ = app


@pytest.mark.confirm_generic_layout
def test_jsonl_and_cli_place_on_scaled_door_use_draw_scale(tmp_path: Path):
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    session = _door_drawn_at(0.12)
    path = tmp_path / "scaled.pcad.yaml"
    session.save(path)
    agent = Agent()
    assert agent.handle({"op": "open", "path": str(path)})["ok"]
    placed = agent.handle(
        {
            "op": "place",
            "library_id": "iec.operators.pb_no",
            "ref": "S1",
            "x": 40,
            "y": 40,
            "sheet": "door",
            "snap": False,
        }
    )
    assert placed["ok"]
    assert agent.session.project.sheet("door").component("S1").scale == pytest.approx(0.12)
    agent.session.save(path)
    result = CliRunner().invoke(
        app,
        ["place", str(path), "--lib", "iec.operators.pb_no", "--ref", "S2", "--x", "60", "--y", "60", "--sheet", "door"],
    )
    assert result.exit_code == 0, result.output
    again = Session.open(path)
    assert again.project.sheet("door").component("S2").scale == pytest.approx(0.12)
    features = {item.ref: item for item in again.cutout_drawings({"surfaces": ["door"]})[0].features}
    assert features["S1"].footprint_w == pytest.approx(22)
    assert features["S2"].footprint_w == pytest.approx(22)
