"""Hand-build defects A04, A07, and the small print and editor bugs beside them."""

from __future__ import annotations

import pytest

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.core.layout import (
    backplate_sheet_bounds,
    body_inside,
    footprint_overlaps_tray,
    footprint_size,
    rail_aabb,
    rects_overlap,
    sits_fully_on_a_rail,
    symbol_body_size,
    tray_aabb,
)
from panelcad.render.print_package import plan_print_package, project_description
from panelcad.render.titleblock import body_hits_title_block


def _ready(session: Session, ref: str, width: float, height: float, depth: float = 60.0) -> None:
    session.confirm_envelope(
        ref,
        width_mm=width,
        height_mm=height,
        depth_mm=depth,
        mount="din_rail",
        confirmed_by="test",
    )


def test_layout_keeps_the_applied_enclosure_and_does_not_overlap():
    session = Session.create("Sized")
    session.place("nema.protection.fuse_10a", ref="FU1", x=40, y=40)
    session.place("iec.accessories.end_stop", ref="XS1", x=70, y=40)
    session.place("iec.accessories.end_stop", ref="XS2", x=90, y=40)
    _ready(session, "FU1", 18, 80, 60)
    _ready(session, "XS1", 9.5, 47, 48)
    _ready(session, "XS2", 9.5, 47, 48)
    session.add_sheet("panel", title="Backpanel", kind="panel_layout")
    session.set_enclosure(
        width_mm=609.6,
        height_mm=508.0,
        depth_mm=203.2,
        name="24 × 20 × 8 in NEMA 12",
        nema_type="12",
        material="steel",
        catalog="N412242008C",
        manufacturer="AutomationDirect",
        sheet_id="panel",
    )
    result = session.layout_from_schematic("panel")
    enc = session.sheet("panel").enclosure
    assert enc is not None
    assert enc.catalog == "N412242008C"
    assert enc.manufacturer == "AutomationDirect"
    assert enc.nema_type == "12"
    assert enc.material == "steel"
    assert enc.width_mm == pytest.approx(609.6)
    assert enc.height_mm == pytest.approx(508.0)
    assert enc.depth_mm == pytest.approx(203.2)
    assert enc.name == "24 × 20 × 8 in NEMA 12"
    assert result.get("needs_info") is False
    refs = {comp.id for comp in session.sheet("panel").components}
    assert {"FU1", "XS1", "XS2"} <= refs
    overlaps = [row for row in session.design_checks() if row["check"] == "footprint_overlap"]
    assert overlaps == []


def test_layout_without_an_enclosure_is_needs_info():
    session = Session.create("Bare")
    session.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40)
    _ready(session, "K1", 45, 75, 85)
    session.add_sheet("panel", kind="panel_layout")
    session.sheet("panel").enclosure = None
    session.sheet("panel").rails = []
    result = session.layout_from_schematic("panel")
    assert result["needs_info"] is True
    assert "generic" in result["reason"].lower() or "enclosure" in result["reason"].lower()
    assert session.sheet("panel").enclosure is None
    assert session.sheet("panel").components == []
    assert result["din"] == 0


def test_default_page_box_is_needs_info_and_places_nothing():
    session = Session.create("Page box")
    session.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40)
    session.place("nema.protection.fuse_10a", ref="FU1", x=80, y=40)
    _ready(session, "K1", 45, 75, 85)
    _ready(session, "FU1", 18, 80, 60)
    session.add_sheet("panel", kind="panel_layout")
    enc = session.sheet("panel").enclosure
    assert enc is not None
    assert enc.sized is False
    result = session.layout_from_schematic("panel")
    assert result["needs_info"] is True
    assert result["din"] == 0
    assert result["reason"] == (
        "No enclosure is set. Size the enclosure before building the layout. "
        "A generic box is not used."
    )
    assert session.sheet("panel").components == []
    assert session.sheet("panel").enclosure is enc
    from panelcad.core.thermal import compute_heat_load

    report = compute_heat_load(session.project, session.catalog, session.parts)
    assert report["needs_info"] is True
    assert report["enclosure"] is None


def test_small_enclosure_leaves_overflow_off_the_sheet():
    session = Session.create("Tight box")
    for index in range(1, 15):
        session.place("nema.protection.fuse_10a", ref=f"FU{index}", x=40, y=40)
        _ready(session, f"FU{index}", 18, 80, 60)
    for index in range(1, 5):
        session.place("iec.accessories.end_stop", ref=f"XS{index}", x=70, y=40)
        _ready(session, f"XS{index}", 9.5, 47, 48)
    session.add_sheet("panel", kind="panel_layout")
    session.set_enclosure(width_mm=200, height_mm=260, depth_mm=150, sheet_id="panel")
    sheet = session.sheet("panel")
    page = sheet.page.dimensions()
    enc = sheet.enclosure
    assert enc is not None
    result = session.layout_from_schematic("panel")
    enc = sheet.enclosure
    assert enc is not None
    assert enc.width_mm == pytest.approx(200)
    assert enc.height_mm == pytest.approx(260)
    assert enc.depth_mm == pytest.approx(150)
    assert sheet.page.dimensions() == page
    assert result.get("needs_info") is False
    left, top, right, bottom = backplate_sheet_bounds(enc)
    placed = {comp.id for comp in sheet.components}
    assert placed
    for comp in sheet.components:
        device = session.catalog.get(comp.library_id)
        width, height = footprint_size(device, comp)
        assert body_inside(comp.x, comp.y, width, height, (left, top, right, bottom))
    for rail in sheet.rails:
        assert body_inside(*rail_aabb(rail), (left, top, right, bottom))
        for tray in sheet.trays:
            assert not rects_overlap(*rail_aabb(rail), *tray_aabb(tray))
    assert sheet.rails
    unplaced = {row["ref"]: row["reason"] for row in result["unplaced"]}
    assert placed.isdisjoint(unplaced)
    assert unplaced
    assert result["din"] == len(placed)
    assert result["field"] == 0
    for ref, reason in unplaced.items():
        assert reason == (
            f"{ref} does not fit in the 200 × 260 mm enclosure. Size a larger enclosure."
        )
    missing = [row for row in session.design_checks() if row["check"] == "missing_panel"]
    assert {row["ref"] for row in missing} >= set(unplaced)
    for row in missing:
        if row["ref"] in unplaced:
            assert "does not fit in the 200 × 260 mm enclosure" in row["message"]


def _pack_box(width: float, height: float, *, vfd: bool = False) -> tuple[Session, set[str]]:
    session = Session.create(f"{width:g}x{height:g}")
    refs: set[str] = set()
    for index in range(1, 21):
        ref = f"FU{index}"
        session.place("nema.protection.fuse_10a", ref=ref, x=40, y=40)
        _ready(session, ref, 18, 80, 60)
        refs.add(ref)
    if vfd:
        session.place("iec.drives.vfd_1k5", ref="VFD1", x=80, y=40)
        session.confirm_envelope(
            "VFD1",
            width_mm=72,
            height_mm=130,
            depth_mm=150,
            mount="backplate",
            din_rail=False,
            confirmed_by="test",
        )
        refs.add("VFD1")
    session.add_sheet("panel", kind="panel_layout")
    session.set_enclosure(width_mm=width, height_mm=height, depth_mm=150, sheet_id="panel")
    return session, refs


def test_kept_parts_stay_off_ducts_and_din_parts_stay_on_rails():
    cases = [(150, 200, False), (250, 300, False), (300, 400, False), (200, 800, False), (800, 200, False), (200, 260, True)]
    for width, height, vfd in cases:
        session, refs = _pack_box(width, height, vfd=vfd)
        result = session.layout_from_schematic("panel")
        sheet = session.sheet("panel")
        placed = {comp.id for comp in sheet.components}
        unplaced = {row["ref"] for row in result["unplaced"]}
        assert placed.isdisjoint(unplaced)
        assert placed | unplaced == refs
        for comp in sheet.components:
            device = session.catalog.get(comp.library_id)
            part_w, part_h = footprint_size(device, comp)
            assert not footprint_overlaps_tray(comp.x, comp.y, part_w, part_h, sheet.trays)
            assert sits_fully_on_a_rail(comp, device, sheet.rails)
        for rail in sheet.rails:
            for tray in sheet.trays:
                assert not rects_overlap(*rail_aabb(rail), *tray_aabb(tray)), (width, height, rail.id)


def test_resizing_the_enclosure_retires_the_old_fit_reason():
    session = _tight_overflow_session()
    result = session.layout_from_schematic("panel")
    ref = next(row["ref"] for row in result["unplaced"] if "does not fit" in row["reason"])
    before = next(row for row in session.design_checks() if row["check"] == "missing_panel" and row["ref"] == ref)
    assert "200 × 260" in before["message"]
    session.set_enclosure(width_mm=600, height_mm=800, depth_mm=200, sheet_id="panel")
    after = next(row for row in session.design_checks() if row["check"] == "missing_panel" and row["ref"] == ref)
    assert after["message"].endswith("Build layout again for the new enclosure size.")
    assert "200 × 260" not in after["message"]


def test_one_unplaced_part_uses_the_singular():
    from panelcad.gui.main_window import _unplaced_layout_message

    text = _unplaced_layout_message(
        [{"ref": "FU9", "reason": "FU9 does not fit in the 200 × 260 mm enclosure. Size a larger enclosure."}]
    )
    assert text.startswith("1 part was not placed:")
    assert "parts were" not in text


def _tight_overflow_session() -> Session:
    session = Session.create("Tight box")
    for index in range(1, 15):
        session.place("nema.protection.fuse_10a", ref=f"FU{index}", x=40, y=40)
        _ready(session, f"FU{index}", 18, 80, 60)
    for index in range(1, 5):
        session.place("iec.accessories.end_stop", ref=f"XS{index}", x=70, y=40)
        _ready(session, f"XS{index}", 9.5, 47, 48)
    session.add_sheet("panel", kind="panel_layout")
    session.set_enclosure(width_mm=200, height_mm=260, depth_mm=150, sheet_id="panel")
    return session


def test_build_layout_tells_the_user_which_parts_were_left_off(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication
    from typer.testing import CliRunner

    from panelcad.cli.main import app
    from panelcad.gui.main_window import MainWindow

    app_qt = QApplication.instance() or QApplication([])
    session = _tight_overflow_session()
    notes: list[tuple] = []

    def _information(*args, **_kwargs):
        notes.append(args)

    monkeypatch.setattr("panelcad.gui.main_window.QMessageBox.information", _information)
    window = MainWindow(session)
    window.show()
    app_qt.processEvents()
    window.pack_from_schematic()
    app_qt.processEvents()
    assert notes
    text = str(notes[-1][2])
    assert text.startswith("15 parts were not placed:")
    assert "does not fit in the 200 × 260 mm enclosure" in text
    assert "… and 5 more" in text
    assert "15 not placed" in window.statusBar().currentMessage()
    window.close()

    path = tmp_path / "tight.pcad.yaml"
    session.save(path)
    cli = CliRunner().invoke(app, ["layout", str(path), "--sheet", "panel"])
    assert cli.exit_code == 0
    assert "does not fit in the 200 × 260 mm enclosure" in cli.output
    assert "FU4" in cli.output or "FU9" in cli.output


def test_invented_cabinet_name_is_unsized():
    session = Session.create("Old cabinet")
    session.place("nema.protection.fuse_10a", ref="FU1", x=40, y=40)
    _ready(session, "FU1", 18, 80, 60)
    session.add_sheet("panel", kind="panel_layout")
    enc = session.sheet("panel").enclosure
    assert enc is not None
    enc.name = "Enclosure 600 × 300 × 150 mm, 6 BOM items in cabinet"
    enc.width_mm = 600
    enc.height_mm = 300
    enc.depth_mm = 150
    enc.catalog = ""
    enc.manufacturer = ""
    enc.sized = False
    result = session.layout_from_schematic("panel")
    assert result["needs_info"] is True
    assert result["din"] == 0
    assert result["reason"] == (
        "No enclosure is set. Size the enclosure before building the layout. "
        "A generic box is not used."
    )
    assert session.sheet("panel").components == []
    assert session.sheet("panel").enclosure is enc


def test_cover_subtitle_skips_an_io_continuation():
    session = Session.create("Conveyor 3 Test")
    session.sheet().description = ""
    session.add_sheet("io2", title="IO2", kind="io")
    sheet = session.sheet("io2")
    sheet.description = "IO2 C0-16ND3 points 8–15 (cont.)"
    sheet.io_cont = "IO2:2"
    text = project_description(session.project)
    assert "cont." not in text
    assert "IO2" not in text
    assert text.endswith("drawing set")

    session.sheet("main").description = "Motor power circuit"
    assert project_description(session.project) == "Motor power circuit"


def test_terminal_strip_sheet_is_on_the_drawing_index():
    session = Session.create("Strip index")
    session.place("iec.terminals.x_2p5", ref="X1-1", x=40, y=30)
    session.open_terminal_strip("X1")
    plan = plan_print_package(session)
    sheet = next(item for item in session.project.sheets if item.kind == "terminal" and item.strip == "X1")
    row = next(item for item in plan.index_rows if item.number == "X1" or "X1" in item.title)
    assert "X1" in row.title
    assert row.number == "X1"
    assert plan.pages[row.target].section == "drawings"
    assert plan.pages[row.target].sheet_id == sheet.id


def test_editor_lifts_a_symbol_off_the_title_block():
    session = Session.create("Title")
    session.place("iec.operators.pilot_light", ref="PL3", x=300, y=230)
    comp = session.sheet().component("PL3")
    device = session.catalog.get(comp.library_id)
    width, height = symbol_body_size(session.sheet(), device, comp)
    assert body_hits_title_block(session.sheet(), comp.x, comp.y, width, height)
    hits = [row for row in session.design_checks() if row["check"] == "symbol_on_title_block"]
    assert hits and hits[0]["ref"] == "PL3"
    neighbor = session.place("iec.operators.pb_no", ref="PB9", x=40, y=40)
    assert session.lift_symbols_off_title_block() == []
    assert session.sheet().component("PL3").y == comp.y
    assert session.lift_symbols_off_title_block(["PL3"]) == ["PL3"]
    assert session.sheet().component("PB9").y == neighbor.y
    comp = session.sheet().component("PL3")
    width, height = symbol_body_size(session.sheet(), device, comp)
    assert not body_hits_title_block(session.sheet(), comp.x, comp.y, width, height)
    assert comp.y == pytest.approx(round(comp.y / 2.5) * 2.5)
    assert comp.y < 230


def test_move_sheet_reorders_and_undoes():
    session = Session.create("Order", sheet_id="main", title="Main")
    session.add_sheet("backpanel", title="Backpanel", kind="panel_layout")
    session.add_sheet("io", title="IO", kind="io")
    moved = session.move_sheet("io", direction="earlier")
    assert moved["moved"] is True
    assert [sheet.id for sheet in session.project.sheets] == ["main", "io", "backpanel"]
    session.reorder_sheets(["io", "backpanel", "main"])
    assert [sheet.id for sheet in session.project.sheets] == ["io", "backpanel", "main"]
    assert session.undo()
    assert [sheet.id for sheet in session.project.sheets] == ["main", "io", "backpanel"]
    agent = Agent()
    agent.session = session
    reply = agent.handle({"op": "move_sheet", "sheet": "backpanel", "direction": "earlier"})
    assert reply["ok"] is True
    assert [sheet.id for sheet in session.project.sheets] == ["main", "backpanel", "io"]


def test_ai_dock_and_checks_follow_the_open_sheet():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow(Session.create("Untitled Panel"))
    window.show()
    app.processEvents()
    window.session.add_sheet("panel", title="Backpanel", kind="panel_layout", make_active=False)
    window._switch_sheet("panel")
    app.processEvents()
    text = window.ai_dock.meta.text()
    assert "2/2" in text
    assert "Backpanel" in text
    window.session.project.name = "Conveyor 3 Test"
    window._title_block_changed()
    assert "Conveyor 3 Test" in window.ai_dock.meta.text()
    assert "Backpanel" in window.ai_dock.meta.text()
    window._on_checks(
        [
            {
                "severity": "error",
                "check": "footprint_overlap",
                "message": "panel: XS1 overlaps FU1",
                "sheet": "panel",
                "ref": "XS1",
                "suppressed": "no",
            }
        ]
    )
    assert window._checks_label.text() == "Checks  1"
    window.close()


def test_open_switch_wire_and_delete_do_not_move_a_title_block_symbol():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import ComponentItem
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    session = Session.create("Imported print")
    session.place("iec.protection.overload_9a", ref="OL9", x=120, y=227.4)
    session.place("iec.contactors.coil", ref="CR6", x=120, y=160)
    session.place("iec.operators.pb_no", ref="PB1", x=40, y=30)
    session.place("iec.operators.pilot_light", ref="PL1", x=100, y=30)
    session.wire("PB1.14", "PL1.X1")
    session.add_sheet("other", title="Other")
    before = {comp.id: (comp.x, comp.y) for comp in session.sheet("main").components}
    window = MainWindow(session)
    window.show()
    app.processEvents()
    window._switch_sheet("other")
    app.processEvents()
    window._switch_sheet("main")
    app.processEvents()
    window.canvas.sync_positions()
    for item in window.canvas.scene.items():
        if isinstance(item, ComponentItem) and item.component_id == "PL1":
            item.setSelected(True)
    window.canvas._delete_selected()
    app.processEvents()
    main = window.session.sheet("main")
    # Snap may round a stored coordinate. A title-block lift moves it by a rung.
    for ref in ("OL9", "CR6"):
        got = (main.component(ref).x, main.component(ref).y)
        assert abs(got[0] - before[ref][0]) < 1.0
        assert abs(got[1] - before[ref][1]) < 1.0
    assert "PL1" not in {comp.id for comp in main.components}
    assert any(
        row["check"] == "symbol_on_title_block" and row["ref"] == "OL9"
        for row in window.session.design_checks()
    )
    window.close()
