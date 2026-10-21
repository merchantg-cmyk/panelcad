"""Several control panels in one project. A file with no panels list stays a single panel."""

from __future__ import annotations

from pathlib import Path

import pytest

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.core.models import Component
from panelcad.core.panels import (
    iec_designation,
    package_sheet_order,
    schematic_location_caption,
    schematic_tag_text,
)
from panelcad.core.xref import component_location, offpage_callout
from panelcad.core.zones import sheet_number, use_sheet_order
from panelcad.io.bom import grouped_rows, instance_rows
from panelcad.io.yaml_format import dump_project, load_project
from panelcad.library.enclosures import load_enclosures

CONFIRMED = "wall-20x12x8-n12"
PLAIN = "wall-16x12x8-n12"


def _box(box_id: str):
    found = load_enclosures().by_id(box_id)
    assert found is not None
    return found


def test_single_panel_file_has_no_panel_keys(tmp_path: Path):
    session = Session.create("Pump")
    session.place("iec.breakers.mcb_1p_16a", ref="Q1", x=20, y=20)
    text = dump_project(session.project)
    assert "panels:" not in text
    assert "active_panel" not in text
    assert "location_system" not in text
    assert "\n    panel:" not in text
    path = tmp_path / "old.pcad.yaml"
    path.write_text(
        "\n".join(
            [
                "format: panelcad",
                'version: "0.1"',
                "name: Old",
                "sheets:",
                "  - id: main",
                "    title: Main",
                "    kind: schematic",
                "    components:",
                "      - {id: Q1, library_id: iec.breakers.mcb_1p_16a, x: 20, y: 30}",
            ]
        ),
        encoding="utf-8",
    )
    loaded = load_project(path)
    assert loaded.panels == []
    assert loaded.sheets[0].panel == ""
    assert loaded.sheets[0].component("Q1").panel == ""
    again = dump_project(loaded)
    assert "panels:" not in again
    rows = instance_rows(loaded, Session.open(path).catalog)
    assert rows and "panel" not in rows[0]


def test_add_panel_uses_catalog_enclosure_and_claims_existing_layout():
    session = Session.create("Job")
    session.add_sheet("enclosure", title="Enclosure", kind="panel_layout")
    bare = session.add_panel("CP1", name="Main")
    assert bare["id"] == "cp1"
    assert session.project.sheet("enclosure").panel == "cp1"
    assert session.project.sheet("enclosure").enclosure is None
    assert sum(1 for sheet in session.project.sheets if sheet.kind == "panel_layout") == 1
    assert any(sheet.kind == "door_layout" and sheet.panel == "cp1" for sheet in session.project.sheets)
    assert any(sheet.kind == "terminal" and sheet.strip == "CP1-TB" for sheet in session.project.sheets)
    text = dump_project(session.project)
    assert "enclosure_id" not in text

    box = _box(CONFIRMED)
    sized = session.set_panel_enclosure("CP1", "A201208LP")
    enc = session.project.sheet("enclosure").enclosure
    assert sized["catalog"] == "A201208LP"
    assert enc.catalog == "A201208LP"
    assert enc.manufacturer == box.manufacturer
    assert enc.width_mm == pytest.approx(box.width_mm)
    assert enc.height_mm == pytest.approx(box.height_mm)
    assert enc.depth_mm == pytest.approx(box.depth_mm)
    assert enc.panel_width_mm == pytest.approx(box.panel_width_mm)
    assert enc.sized is True
    assert session.project.panels[0].enclosure_id == CONFIRMED


def test_unknown_enclosure_is_refused_and_does_not_stick():
    session = Session.create("Job")
    with pytest.raises(ValueError, match="enclosures.yaml"):
        session.add_panel("CP1", enclosure_id="400x500x200")
    assert session.project.panels == []
    assert "panels:" not in dump_project(session.project)


def test_second_panel_devices_bom_and_designation():
    session = Session.create("Plant")
    session.set_title_block(standard="IEC")
    session.add_panel("CP1", name="Main", enclosure_id=CONFIRMED, system="A1")
    session.add_panel("RIO2", name="Remote I/O", enclosure_id=PLAIN)
    session.place("iec.relays.k_4co_24vdc", ref="K1", x=40, y=40, sheet_id="main", panel="CP1")
    session.place("iec.relays.k_4co_24vdc", ref="K2", x=90, y=40, sheet_id="main", panel="RIO2")
    sheet = session.project.sheet("main")
    k1 = sheet.component("K1")
    k2 = sheet.component("K2")
    assert k1.panel == "cp1"
    assert k2.panel == "rio2"
    assert schematic_tag_text(session.project, sheet, k1) == "=A1+CP1-K1"
    assert iec_designation(session.project, session.project.panels[0], "K1") == "=A1+CP1-K1"
    assert schematic_location_caption(session.project, session.sheet(), k1) == ""

    session.set_title_block(standard="NEMA/ANSI")
    assert schematic_tag_text(session.project, sheet, k1) == "K1"
    assert schematic_location_caption(session.project, sheet, k1) == "CP1"

    split = grouped_rows(session.project, session.catalog)
    relay = [row for row in split if row["library_id"] == "iec.relays.k_4co_24vdc"]
    assert {row["panel"] for row in relay} == {"CP1", "RIO2"}
    assert {row["qty"] for row in relay} == {"1"}
    rolled = grouped_rows(session.project, session.catalog, by_panel=False)
    relay_all = next(row for row in rolled if row["library_id"] == "iec.relays.k_4co_24vdc")
    assert relay_all["qty"] == "2"
    assert relay_all["panel"] == "CP1, RIO2"
    rio = session.project.panels[1]
    enc = next(sheet.enclosure for sheet in session.project.sheets if sheet.panel == rio.id and sheet.kind == "panel_layout")
    plain = _box(PLAIN)
    assert enc.catalog == ""
    assert enc.width_mm == pytest.approx(plain.width_mm)


def test_interconnect_and_offpanel_callout():
    session = Session.create("Link")
    session.add_panel("CP1", enclosure_id=CONFIRMED)
    session.add_panel("RIO2", enclosure_id=PLAIN)
    session.assign_sheet("main", "CP1")
    rio = session.add_sheet("rio", title="RIO schematic", kind="schematic")
    session.assign_sheet(rio.id, "RIO2")
    session.place("iec.relays.k_4co_24vdc", ref="K1", x=40, y=40, sheet_id="main", panel="CP1")
    session.place("iec.relays.k_4co_24vdc", ref="K2", x=40, y=40, sheet_id=rio.id, panel="RIO2")
    jumped = session.wire_offpage("K1.A1", "K2.A1", from_sheet="main", to_sheet=rio.id)
    assert jumped["callout"].startswith("TO RIO2 ")
    assert jumped["callout"].endswith(" / K2")
    from panelcad.core.xref import offpage_callout

    arrow = next(comp for comp in session.project.sheet("main").components if "offpage" in comp.library_id)
    text = offpage_callout(session.project, session.catalog, session.project.sheet("main"), arrow)
    assert text.startswith("TO RIO2 ")

    cable = session.add_interconnect(
        "CP1",
        "RIO2",
        from_strip="CP1-TB",
        to_strip="RIO2-TB",
        cable_type="4C #18 TC-ER",
        conductors=4,
        gauge="#18",
        length=40,
    )
    assert cable["kind"] == "interpanel"
    assert cable["from_panel"] == "cp1"
    assert cable["to_panel"] == "rio2"
    rows = session.interconnect_rows()
    assert rows[0]["from_panel"] == "CP1"
    assert rows[0]["to_panel"] == "RIO2"
    assert rows[0]["from_strip"] == "CP1-TB"
    dumped = dump_project(session.project)
    assert "kind: interpanel" in dumped
    assert "from_panel: cp1" in dumped
    plan = session.plan_print_package(sections=["drawings", "bom", "interconnect"])
    drawing_ids = [page["id"] for page in plan["sheets"] if page["id"]]
    assert drawing_ids[0] == "main"
    cp1_at = drawing_ids.index("cp1-backpanel")
    rio_at = drawing_ids.index("rio2-backpanel")
    assert cp1_at < rio_at
    assert any(title.startswith("CP1 —") for title in (row["title"] for row in plan["index"]))
    assert "Bill of Materials — CP1" in [row["title"] for row in plan["index"]]
    assert "Bill of Materials" in [row["title"] for row in plan["index"]]
    assert "Interconnect Schedule" in [row["title"] for row in plan["index"]]


def test_field_cable_yaml_omits_interpanel_fields():
    session = Session.create("Field")
    session.add_cable(tag="W101", to_device="MTR1", cable_type="3C+G #14 TC-ER", conductors=4)
    text = dump_project(session.project)
    assert "kind: field" not in text
    assert "kind: interpanel" not in text
    assert "from_panel" not in text
    assert "to_strip" not in text


def test_agent_add_panel_and_assign():
    agent = Agent()
    opened = agent.handle({"id": 1, "op": "create_project", "name": "MCC"})
    assert opened["ok"] is True
    added = agent.handle(
        {"id": 2, "op": "add_panel", "tag": "MCC1", "name": "Lineup", "enclosure": CONFIRMED}
    )
    assert added["ok"] is True, added
    assert added["result"]["tag"] == "MCC1"
    placed = agent.handle(
        {
            "id": 3,
            "op": "place",
            "library_id": "iec.breakers.mcb_1p_16a",
            "ref": "CB1",
            "x": 30,
            "y": 30,
            "sheet": "main",
            "panel": "MCC1",
        }
    )
    assert placed["ok"] is True, placed
    listed = agent.handle({"id": 4, "op": "list_panels"})
    assert listed["result"]["panels"][0]["devices"] == ["CB1"]
    help_text = agent.handle({"id": 5, "op": "help"})["result"]["help"]
    assert "libraries/enclosures.yaml" in help_text["add_panel"]
    assert "assign_panel" in help_text


def test_sheet_bar_lists_panels():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.sheet_bar import SheetBar

    QApplication.instance() or QApplication([])
    empty = Session.create("One")
    bar = SheetBar()
    bar.set_session(empty)
    assert bar.panel_combo.isEnabled() is False
    session = Session.create("Two")
    session.add_panel("CP1", enclosure_id=CONFIRMED)
    session.add_panel("RIO2", enclosure_id=PLAIN)
    bar.set_session(session)
    assert bar.panel_combo.isEnabled() is True
    assert bar.panel_combo.count() == 2
    assert bar.panel_combo.currentData() == "rio2"


def test_panel_backpanel_refuses_typed_enclosure_dims():
    session = Session.create("RIO")
    session.add_panel("RIO2", enclosure_id=PLAIN)
    sheet = session.project.sheet("rio2-backpanel")
    before = sheet.enclosure.width_mm
    with pytest.raises(ValueError, match="enclosures.yaml"):
        session.set_enclosure(width_mm=555, height_mm=400, depth_mm=200, sheet_id=sheet.id)
    assert sheet.enclosure.width_mm == before
    box = _box(CONFIRMED)
    session.set_enclosure(enclosure_id="A201208LP", sheet_id=sheet.id)
    assert sheet.enclosure.catalog == "A201208LP"
    assert sheet.enclosure.width_mm == pytest.approx(box.width_mm)
    assert sheet.enclosure.width_mm != 555

    session.project.active_panel = ""
    session.add_sheet("loose", title="Loose", kind="panel_layout")
    loose = session.project.sheet("loose")
    assert loose.panel == ""
    session.set_enclosure(width_mm=555, height_mm=400, depth_mm=200, sheet_id="loose")
    assert loose.enclosure.width_mm == 555

    agent = Agent()
    agent.session = session
    refused = agent.handle(
        {"id": 1, "op": "set_enclosure", "sheet": "rio2-backpanel", "width_mm": 555, "height_mm": 400}
    )
    assert refused["ok"] is False
    assert "555" in refused["error"] or "enclosures.yaml" in refused["error"]
    assert session.project.sheet("rio2-backpanel").enclosure.width_mm == pytest.approx(box.width_mm)


def test_first_panel_and_assign_sheet_take_unassigned_devices():
    session = Session.create("Claim")
    session.add_sheet("enclosure", title="Enclosure", kind="panel_layout")
    sheet = session.project.sheet("enclosure")
    sheet.components.append(Component(id="Q1", library_id="iec.breakers.mcb_1p_16a", x=30, y=40))
    session.add_panel("CP1")
    assert sheet.panel == "cp1"
    assert sheet.component("Q1").panel == "cp1"

    session.add_panel("RIO2", enclosure_id=PLAIN)
    session.project.active_panel = ""
    session.place("iec.relays.k_4co_24vdc", ref="K9", x=40, y=40, sheet_id="main")
    assert session.project.sheet("main").component("K9").panel == ""
    session.assign_sheet("main", "CP1")
    assert session.project.sheet("main").component("K9").panel == "cp1"
    session.place("iec.relays.k_4co_24vdc", ref="K8", x=80, y=40, sheet_id="main", panel="RIO2")
    session.assign_sheet("main", "CP1")
    assert session.project.sheet("main").component("K8").panel == "rio2"


def test_moving_a_layout_sheet_moves_its_devices_and_keeps_the_last_backpanel():
    session = Session.create("Move")
    session.add_panel("CP1", enclosure_id=CONFIRMED)
    session.add_panel("RIO2", enclosure_id=PLAIN)
    with pytest.raises(ValueError, match="only backpanel"):
        session.assign_sheet("cp1-backpanel", "RIO2")
    assert session.project.sheet("cp1-backpanel").panel == "cp1"

    session.set_active_panel("CP1")
    session.add_sheet("cp1-b2", title="CP1 extra", kind="panel_layout")
    back = session.project.sheet("cp1-backpanel")
    back.components.append(
        Component(id="Q1", library_id="iec.breakers.mcb_1p_16a", x=30, y=40, panel="cp1")
    )
    session.assign_sheet("cp1-backpanel", "RIO2")
    assert back.panel == "rio2"
    assert back.component("Q1").panel == "rio2"


def test_delete_panel_reassigns_or_clears_and_undoes():
    session = Session.create("Drop")
    session.add_panel("CP1", enclosure_id=CONFIRMED)
    session.add_panel("RIO2", enclosure_id=PLAIN)
    session.place("iec.relays.k_4co_24vdc", ref="K1", x=40, y=40, sheet_id="main", panel="CP1")
    session.add_interconnect("CP1", "RIO2", from_strip="CP1-TB", to_strip="RIO2-TB", cable_type="4C #18")
    cleared = session.delete_panel("CP1")
    assert cleared["deleted"] == "cp1"
    assert session.project.panels[0].id == "rio2"
    assert all(sheet.panel != "cp1" for sheet in session.project.sheets)
    assert session.project.sheet("main").component("K1").panel == ""
    cable = session.project.cables[0]
    assert cable.from_panel == ""
    assert cable.to_panel == "rio2"
    session.undo()
    assert any(panel.id == "cp1" for panel in session.project.panels)
    assert session.project.sheet("main").component("K1").panel == "cp1"
    assert session.project.cables[0].from_panel == "cp1"

    moved = session.delete_panel("CP1", reassign="RIO2")
    assert moved["dropped_cables"]
    assert session.project.cables == []
    assert session.project.sheet("cp1-backpanel").panel == "rio2"
    assert session.project.sheet("main").component("K1").panel == "rio2"
    assert "cp1" not in {panel.id for panel in session.project.panels}


def test_first_panel_assigns_devices_that_were_already_placed():
    session = Session.create("Early")
    session.place("iec.relays.k_4co_24vdc", ref="K1", x=40, y=40)
    session.add_panel("CP1")
    assert session.project.sheet("main").component("K1").panel == "cp1"


def test_delete_only_backpanel_is_refused():
    session = Session.create("Keep")
    session.add_panel("CP1", enclosure_id=CONFIRMED)
    session.add_sheet("notes", title="Notes", kind="schematic")
    with pytest.raises(ValueError, match="only backpanel"):
        session.delete_sheet("cp1-backpanel")
    assert any(sheet.id == "cp1-backpanel" for sheet in session.project.sheets)
    session.set_active_panel("CP1")
    session.add_sheet("cp1-b2", title="CP1 extra", kind="panel_layout")
    session.delete_sheet("cp1-backpanel")
    assert all(sheet.id != "cp1-backpanel" for sheet in session.project.sheets)
    assert session.project.sheet("cp1-b2").panel == "cp1"


def test_add_panel_without_a_box_stays_unsized_until_a_circuit():
    session = Session.create("Open")
    session.add_panel("CP1")
    sheet = session.project.sheet("cp1-backpanel")
    assert sheet.enclosure is None
    assert any(row["check"] == "panel_needs_enclosure" for row in session.design_checks())
    session.insert_circuit(kind="dol")
    assert sheet.enclosure is not None
    box = _box(session.project.panels[0].enclosure_id)
    assert sheet.enclosure.width_mm == pytest.approx(box.width_mm)
    assert abs(sheet.enclosure.width_mm - 261.0) > 1.0
    assert not any(row["check"] == "panel_needs_enclosure" for row in session.design_checks())


def _parts_outside(session: Session, sheet_id: str) -> list[str]:
    from panelcad.core.layout import footprint_size

    sheet = session.project.sheet(sheet_id)
    enc = sheet.enclosure
    assert enc is not None
    scale = float(enc.draw_scale or 1.0) or 1.0
    right = enc.x + enc.width_mm * scale
    bottom = enc.y + enc.height_mm * scale
    outside: list[str] = []
    for comp in sheet.components:
        device = session.catalog.get(comp.library_id)
        width, height = footprint_size(device, comp)
        if width <= 0 and height <= 0:
            continue
        if comp.x < enc.x - 0.5 or comp.y < enc.y - 0.5 or comp.x + width > right + 0.5 or comp.y + height > bottom + 0.5:
            outside.append(comp.id)
    return outside


@pytest.mark.parametrize(
    "enclosure_id",
    ["wall-36x30x8-n12", "wall-30x24x8-n12"],
)
def test_insert_circuit_keeps_a_chosen_enclosure(enclosure_id: str):
    from panelcad.core.layout import fit_standard_enclosure_scale
    from panelcad.render.titleblock import working_rect

    session = Session.create("Chosen")
    session.set_title_block(standard="IEC")
    session.add_panel("CP1", enclosure_id=enclosure_id)
    sheet = session.project.sheet("cp1-backpanel")
    width = sheet.enclosure.width_mm
    result = session.insert_circuit(kind="soft_starter", control="local", kw=7.5)
    assert session.project.panels[0].enclosure_id == enclosure_id
    assert sheet.enclosure.width_mm == pytest.approx(width)
    _wx, _wy, ww, wh = working_rect(sheet)
    expect = fit_standard_enclosure_scale(sheet.enclosure.width_mm, sheet.enclosure.height_mm, ww, wh)
    assert sheet.enclosure.draw_scale == pytest.approx(expect)
    assert sheet.enclosure.draw_scale < 1.0
    assert sheet.components
    assert _parts_outside(session, sheet.id) == []
    if enclosure_id == "wall-36x30x8-n12":
        from panelcad.core.layout import backplate_sheet_bounds, rail_aabb, tray_aabb

        plate = backplate_sheet_bounds(sheet.enclosure)
        plate_w = plate[2] - plate[0]
        plate_h = plate[3] - plate[1]
        xs: list[float] = []
        ys: list[float] = []
        for rail in sheet.rails:
            x, y, w, h = rail_aabb(rail)
            xs += [x, x + w]
            ys += [y, y + h]
        for tray in sheet.trays:
            x, y, w, h = tray_aabb(tray)
            xs += [x, x + w]
            ys += [y, y + h]
        assert xs and plate_w > 0 and plate_h > 0
        assert (max(xs) - min(xs)) / plate_w > 0.75
        assert (max(ys) - min(ys)) / plate_h > 0.75
    notes = " ".join(str(line) for line in (result.get("layout") or {}).get("warnings") or [])
    assert "not fit" not in notes
    assert "does not fit" not in notes
    assert not any(row["check"] in {"panel_enclosure_tight", "outside_enclosure"} for row in session.design_checks())


def _fill_until_tight(session, limit=8):
    """Insert 7.5 kW soft starters until the chosen box keeps its size and warns."""

    results = []
    for _ in range(limit):
        result = session.insert_circuit(kind="soft_starter", control="local", kw=7.5)
        results.append(result)
        notes = " ".join(str(line) for line in (result.get("layout") or {}).get("warnings") or [])
        if "keeps enclosure" in notes:
            return results
    raise AssertionError("the 16x12 never ran out of backplate")


def test_three_soft_starters_in_a_16x12_warn_unplaced_parts():
    """A circuit that never lands on the plate is still a fit failure.

    How many circuits fit depends on layout spacing, so fill until the first miss.
    """

    from panelcad.core.circuit_layout import backplate_fit_refs

    session = Session.create("Three")
    session.set_title_block(standard="IEC")
    session.add_panel("CP1", enclosure_id="wall-16x12x6-n12")
    results = _fill_until_tight(session)
    assert len(results) >= 2
    sheet = session.project.sheet("cp1-backpanel")
    assert session.project.panels[0].enclosure_id == "wall-16x12x6-n12"
    ids = {comp.id for comp in sheet.components}
    assert "XS1" in ids
    for earlier in results[:-1]:
        notes = " ".join(str(line) for line in (earlier.get("layout") or {}).get("warnings") or [])
        assert "not fit in the free backplate" not in notes
        assert "keeps enclosure" not in notes
    last = [str(line) for line in (results[-1].get("layout") or {}).get("warnings") or []]
    missing = [ref for line in last for ref in backplate_fit_refs(line)]
    assert missing
    for ref in missing:
        assert ref not in ids
    notes = " ".join(last)
    assert "keeps enclosure" in notes
    assert "does not fit" in notes
    again = session.insert_circuit(kind="soft_starter", control="local", kw=7.5)
    again_notes = " ".join(str(line) for line in (again.get("layout") or {}).get("warnings") or [])
    assert "not fit in the free backplate" in again_notes
    assert "keeps enclosure" in again_notes
    tight = [row for row in session.design_checks() if row["check"] == "panel_enclosure_tight"]
    assert tight
    assert "not fit" in tight[0]["message"]


def test_small_chosen_box_is_scaled_onto_the_sheet():
    """16×12 stays the chosen box and is drawn to the page. Warn only if a part exceeds it."""

    from panelcad.core.layout import fit_standard_enclosure_scale
    from panelcad.render.titleblock import working_rect

    session = Session.create("Small")
    session.set_title_block(standard="IEC")
    session.add_panel("CP1", enclosure_id="wall-16x12x6-n12")
    sheet = session.project.sheet("cp1-backpanel")
    width = sheet.enclosure.width_mm
    result = session.insert_circuit(kind="soft_starter", control="local", kw=7.5)
    assert session.project.panels[0].enclosure_id == "wall-16x12x6-n12"
    assert sheet.enclosure.width_mm == pytest.approx(width)
    _wx, _wy, ww, wh = working_rect(sheet)
    expect = fit_standard_enclosure_scale(sheet.enclosure.width_mm, sheet.enclosure.height_mm, ww, wh)
    assert sheet.enclosure.draw_scale == pytest.approx(expect)
    assert sheet.enclosure.draw_scale < 1.0
    outside = _parts_outside(session, sheet.id)
    notes = " ".join(str(line) for line in (result.get("layout") or {}).get("warnings") or [])
    tight = any(row["check"] == "panel_enclosure_tight" for row in session.design_checks())
    if outside:
        assert tight or "not fit" in notes or "does not fit" in notes
    else:
        assert "not fit" not in notes
        assert "does not fit" not in notes
        assert not tight


def test_unsized_panel_soft_starter_has_rails():
    from panelcad.core.layout import fit_standard_enclosure_scale
    from panelcad.render.titleblock import working_rect

    session = Session.create("Empty rails")
    session.set_title_block(standard="IEC")
    session.add_panel("CP1")
    assert session.project.sheet("cp1-backpanel").rails == []
    session.insert_circuit(kind="soft_starter", control="local", kw=7.5)
    sheet = session.project.sheet("cp1-backpanel")
    assert sheet.rails
    assert sheet.enclosure is not None
    assert session.project.panels[0].enclosure_id
    _wx, _wy, ww, wh = working_rect(sheet)
    expect = fit_standard_enclosure_scale(sheet.enclosure.width_mm, sheet.enclosure.height_mm, ww, wh)
    assert sheet.enclosure.draw_scale == pytest.approx(expect)
    assert _parts_outside(session, sheet.id) == []


def test_dol_in_a_large_chosen_box_is_not_a_fit_warning():
    """Zero placed bodies is not a fit failure. The chosen box stays, scaled onto the sheet."""

    from panelcad.core.layout import fit_standard_enclosure_scale
    from panelcad.render.titleblock import working_rect

    session = Session.create("DOL fits")
    session.set_title_block(standard="IEC")
    session.add_panel("CP1", enclosure_id="wall-30x24x8-n12")
    sheet = session.project.sheet("cp1-backpanel")
    width = sheet.enclosure.width_mm
    result = session.insert_circuit(kind="dol")
    assert sheet.enclosure.width_mm == pytest.approx(width)
    assert session.project.panels[0].enclosure_id == "wall-30x24x8-n12"
    _wx, _wy, ww, wh = working_rect(sheet)
    expect = fit_standard_enclosure_scale(sheet.enclosure.width_mm, sheet.enclosure.height_mm, ww, wh)
    assert sheet.enclosure.draw_scale == pytest.approx(expect)
    assert sheet.enclosure.draw_scale < 1.0
    layout = result.get("layout") or {}
    assert int(layout.get("din") or 0) + int(layout.get("field") or 0) == 0
    notes = " ".join(str(line) for line in (layout.get("warnings") or []))
    assert "not fit" not in notes
    assert "does not fit" not in notes
    assert _parts_outside(session, sheet.id) == []
    assert not any(
        row["check"] in {"panel_enclosure_tight", "outside_enclosure"} for row in session.design_checks()
    )


def test_insert_circuit_and_size_enclosure_apply_use_a_library_box():
    session = Session.create("Starter")
    session.add_panel("CP1")
    session.insert_circuit(kind="dol")
    sheet = next(item for item in session.project.sheets if item.kind == "panel_layout" and item.panel == "cp1")
    applied = session.apply_enclosure(sheet_id=sheet.id, place_parts=False)
    assert applied["applied"] is True
    panel = session.project.panels[0]
    box = _box(panel.enclosure_id)
    enc = sheet.enclosure
    assert enc.width_mm == pytest.approx(box.width_mm)
    assert enc.height_mm == pytest.approx(box.height_mm)
    assert enc.depth_mm == pytest.approx(box.depth_mm)
    assert enc.catalog == box.catalog
    with pytest.raises(ValueError, match="Typed millimetres"):
        session.set_enclosure(width_mm=555, height_mm=400, depth_mm=200, sheet_id=sheet.id)
    assert sheet.enclosure.width_mm == pytest.approx(box.width_mm)


def test_wire_offpage_op_and_package_uses_printed_sheet_numbers(monkeypatch):
    session = Session.create("Order")
    session.add_panel("CP1", enclosure_id=CONFIRMED)
    session.add_panel("RIO2", enclosure_id=PLAIN)
    session.add_sheet("cp1-io", title="CP1 IO", kind="schematic")
    session.assign_sheet("cp1-io", "CP1")
    session.add_sheet("remote-io", title="Remote IO", kind="schematic")
    session.assign_sheet("remote-io", "RIO2")
    remote = session.project.sheet("remote-io")
    session.project.sheets.remove(remote)
    session.project.sheets.insert(1, remote)
    session.place("iec.relays.k_4co_24vdc", ref="K1", x=40, y=80, sheet_id="cp1-io", panel="CP1")
    session.place("iec.relays.k_4co_24vdc", ref="K2", x=40, y=80, sheet_id="remote-io", panel="RIO2")

    agent = Agent()
    agent.session = session
    sent = agent.handle(
        {
            "id": 1,
            "op": "wire_offpage",
            "from": "K1.A1",
            "to": "K2.A1",
            "from_sheet": "cp1-io",
            "to_sheet": "remote-io",
        }
    )
    assert sent["ok"] is True, sent
    assert sent["result"]["to_sheet"] == "remote-io"

    printed = package_sheet_order(session.project)
    project_no = sheet_number(session.project.sheets, "remote-io")
    printed_no = sheet_number(printed, "remote-io")
    assert project_no != printed_no
    cp1 = session.project.sheet("cp1-io")
    arrow = next(comp for comp in cp1.components if "offpage" in comp.library_id and "offpage_from" not in comp.library_id)
    outside = offpage_callout(session.project, session.catalog, cp1, arrow)
    assert f"RIO2 {project_no} " in outside
    with use_sheet_order(printed):
        inside = offpage_callout(session.project, session.catalog, cp1, arrow)
        loc = component_location(session.project, remote, remote.component("K2"), session.catalog.get("iec.relays.k_4co_24vdc"))
    assert f"RIO2 {printed_no} " in inside
    assert loc.sheet_no == printed_no
    assert loc.line // 100 == printed_no

    import panelcad.core.xref as xref_mod

    seen: list[int] = []
    real = xref_mod.sheet_number

    def spy(sheets, sheet_id):
        number = real(sheets, sheet_id)
        if sheet_id == "remote-io":
            seen.append(number)
        return number

    monkeypatch.setattr(xref_mod, "sheet_number", spy)
    session.plan_print_package(sections=["drawings", "xref"])
    assert seen
    assert all(number == printed_no for number in seen)


def test_unplaced_warning_clears_when_the_circuit_is_deleted():
    session = Session.create("Gone")
    session.set_title_block(standard="IEC")
    session.add_panel("CP1", enclosure_id="wall-16x12x6-n12")
    results = _fill_until_tight(session)
    # Undo the circuit that missed, then insert it again to know its tags.
    session.undo()
    before = {(sheet.id, comp.id) for sheet in session.project.sheets for comp in sheet.components}
    session.insert_circuit(kind="soft_starter", control="local", kw=7.5)
    assert any(row["check"] == "panel_enclosure_tight" for row in session.design_checks())
    after = {(sheet.id, comp.id) for sheet in session.project.sheets for comp in sheet.components}
    for sheet_id, ref in sorted(after - before):
        if any(comp.id == ref for comp in session.project.sheet(sheet_id).components):
            session.delete_component(ref, sheet_id=sheet_id)
    assert not any(row["check"] == "panel_enclosure_tight" for row in session.design_checks())
