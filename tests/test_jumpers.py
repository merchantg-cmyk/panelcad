"""Jumpers are catalog parts: family + pitch + span → BOM, export, drawing, undo."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from panelcad import Session
from panelcad.core.jumper import jumper_runs, pick_jumper
from panelcad.io.bom import grouped_rows, instance_rows
from panelcad.io.yaml_format import dump_project
from panelcad.library.catalog import Catalog, default_catalog
from panelcad.render.svg import sheet_to_svg


def test_jumper_catalog_loads_vendor_families():
    cat = default_catalog()
    phoenix = cat.get("iec.jumpers.phoenix_2p_5p2")
    assert phoenix.properties["part_number"] == "FBS 2-5"
    assert phoenix.properties["catalog"] == "3030161"
    assert phoenix.properties["manufacturer"] == "Phoenix Contact"
    assert phoenix.category == "connection.jumper"
    assert cat.get("iec.jumpers.phoenix_10p_5p2").properties["cut_to_length"] is True
    assert cat.get("iec.jumpers.wago_2p_5p2").properties["part_number"] == "2002-402"
    assert cat.get("iec.jumpers.weidmueller_2p_5p1").properties["catalog"] == "2108470000"
    assert cat.get("iec.jumpers.dinkle_2p_6p2").properties["part_number"] == "DN-T10SJ-2"
    ut25 = cat.get("iec.terminals.phoenix_ut_2p5")
    assert ut25.properties["jumper_family"] == "phoenix"
    assert ut25.properties["part_number"] == "UT 2,5"
    assert ut25.properties["catalog"] == "3044076"
    assert ut25.properties["catalog"] != "3044102"
    assert cat.get("iec.jumpers.phoenix_3p_5p2").properties["part_number"] == "FBS 3-5"
    assert cat.get("iec.jumpers.phoenix_3p_5p2").properties["catalog"] == "3030174"
    assert cat.get("iec.terminals.wago_2002_1201").properties["pitch_mm"] == 5.2
    assert cat.get("iec.terminals.weidmueller_wdu_2p5").properties["pitch_mm"] == 5.1
    assert cat.get("iec.terminals.adc_dn_t10").properties["jumper_family"] == "dinkle"


def test_pick_matches_family_pitch_and_poles():
    cat = default_catalog()
    session = Session.create("Pick")
    session.place("iec.terminals.phoenix_ut_2p5", ref="X1-1", x=40, y=20)
    session.place("iec.terminals.phoenix_ut_2p5", ref="X1-2", x=40, y=50)
    session.place("iec.terminals.phoenix_ut_2p5", ref="X1-3", x=40, y=80)
    two = pick_jumper(cat, [session.sheet().component("X1-1"), session.sheet().component("X1-2")])
    assert two["part_number"] == "FBS 2-5"
    assert two["catalog"] == "3030161"
    assert two["qty"] == 1
    three = pick_jumper(
        cat,
        [session.sheet().component(ref) for ref in ("X1-1", "X1-2", "X1-3")],
    )
    assert three["part_number"] == "FBS 3-5"
    assert three["catalog"] == "3030174"
    wago = Session.create("WAGO")
    wago.place("iec.terminals.wago_2002_1201", ref="X2-1", x=40, y=20)
    wago.place("iec.terminals.wago_2002_1201", ref="X2-2", x=40, y=50)
    picked = pick_jumper(cat, [wago.sheet().component("X2-1"), wago.sheet().component("X2-2")])
    assert picked["part_number"] == "2002-402"
    wm = Session.create("WM")
    wm.place("iec.terminals.weidmueller_wdu_2p5", ref="X3-1", x=40, y=20)
    wm.place("iec.terminals.weidmueller_wdu_2p5", ref="X3-2", x=40, y=50)
    picked = pick_jumper(cat, [wm.sheet().component("X3-1"), wm.sheet().component("X3-2")])
    assert picked["part_number"] == "ZQV 2.5N/2"
    adc = Session.create("ADC")
    adc.place("iec.terminals.adc_dn_t10", ref="X4-1", x=40, y=20)
    adc.place("iec.terminals.adc_dn_t10", ref="X4-2", x=40, y=50)
    picked = pick_jumper(cat, [adc.sheet().component("X4-1"), adc.sheet().component("X4-2")])
    assert picked["part_number"] == "DN-T10SJ-2"


def test_cut_to_length_counts_one_longer_bar():
    session = Session.create("Cut")
    for i in range(1, 8):
        session.place("iec.terminals.phoenix_ut_2p5", ref=f"X1-{i}", x=40, y=20 * i)
    session.jumper_terminals([f"X1-{i}" for i in range(1, 8)])
    run = jumper_runs(session.project, session.catalog, "X1")[0]
    assert run["poles"] == 7
    assert run["part_number"] == "FBS 10-5"
    assert run["qty"] == 1
    assert "cut to 7 poles" in run["notes"]
    rows = grouped_rows(session.project, session.catalog)
    jumper = next(row for row in rows if row["library_id"] == "iec.jumpers.phoenix_10p_5p2")
    assert jumper["qty"] == "1"
    assert jumper["catalog"] == "3030213"


def test_generic_builtin_terminals_get_a_generic_jumper():
    session = Session.create("Generic")
    session.place("iec.terminals.x_2p5", ref="X1-1", x=40, y=20)
    session.place("iec.terminals.x_2p5", ref="X1-2", x=40, y=50)
    session.jumper_terminals(["X1-1", "X1-2"])
    rows = instance_rows(session.project, session.catalog)
    jumper = next(row for row in rows if row["category"] == "connection.jumper")
    assert jumper["ref"] == "J1"
    assert jumper["library_id"] == "iec.jumpers.generic_2p_5p2"
    assert jumper["qty"] == "1"
    assert "X1-1" in jumper["notes"]


def test_old_property_only_project_still_derives_bom(tmp_path: Path):
    text = """
format: panelcad
version: "0.1"
name: Legacy jumper
sheets:
  - id: main
    kind: schematic
    components:
      - { id: X1-1, library_id: iec.terminals.x_2p5, x: 40, y: 20, properties: { jumper: J1 } }
      - { id: X1-2, library_id: iec.terminals.x_2p5, x: 40, y: 50, properties: { jumper: J1 } }
      - { id: X1-3, library_id: iec.terminals.x_2p5, x: 40, y: 80 }
"""
    dest = tmp_path / "legacy.pcad.yaml"
    dest.write_text(text, encoding="utf-8")
    session = Session.open(dest)
    assert session.project.sheet("main").component("X1-1").properties["jumper"] == "J1"
    assert session.project.sheet("main").component("X1-3").properties.get("jumper") in {None, ""}
    rows = grouped_rows(session.project, session.catalog)
    jumper = next(row for row in rows if row["ref"] == "J1")
    assert jumper["library_id"] == "iec.jumpers.generic_2p_5p2"
    assert jumper["qty"] == "1"
    dumped = yaml.safe_load(dump_project(session.project))
    props = next(
        comp["properties"]
        for sheet in dumped["sheets"]
        for comp in sheet.get("components") or []
        if comp["id"] == "X1-1"
    )
    assert props["jumper"] == "J1"
    assert "library_id" not in props


def test_edit_span_and_clear_update_bom_with_undo():
    session = Session.create("Edit")
    for i, y in enumerate((20, 50, 80, 110), start=1):
        session.place("iec.terminals.phoenix_ut_2p5", ref=f"X1-{i}", x=40, y=y)
    session.jumper_terminals(["X1-1", "X1-2"])
    two = next(row for row in grouped_rows(session.project, session.catalog) if row["category"] == "connection.jumper")
    assert two["part_number"] == "FBS 2-5"
    session.jumper_terminals(["X1-1", "X1-3"], group="J1")
    three = next(row for row in grouped_rows(session.project, session.catalog) if row["category"] == "connection.jumper")
    assert three["part_number"] == "FBS 3-5"
    assert session.project.sheet("main").component("X1-3").properties["jumper"] == "J1"
    assert session.undo()
    again = next(row for row in grouped_rows(session.project, session.catalog) if row["category"] == "connection.jumper")
    assert again["part_number"] == "FBS 2-5"
    assert "jumper" not in session.project.sheet("main").component("X1-3").properties
    session.clear_terminal_jumpers(refs=["X1-1", "X1-2"])
    assert all(row["category"] != "connection.jumper" for row in grouped_rows(session.project, session.catalog))
    assert session.undo()
    restored = next(row for row in grouped_rows(session.project, session.catalog) if row["category"] == "connection.jumper")
    assert restored["part_number"] == "FBS 2-5"


@pytest.mark.confirm_generic_layout
def test_vendor_csv_adds_jumper_accessory_row(tmp_path: Path):
    session = Session.create("Export")
    session.place("iec.terminals.phoenix_ut_2p5", ref="X1-1", x=40, y=20)
    session.place("iec.terminals.phoenix_ut_2p5", ref="X1-2", x=40, y=50)
    session.jumper_terminals(["X1-1", "X1-2"])
    session.open_terminal_strip("X1")
    phoenix = session.export_terminal_strip(tmp_path / "phoenix.csv", "X1", "phoenix")
    wago = session.export_terminal_strip(tmp_path / "wago.csv", "X1", "wago")
    wmc = session.export_terminal_strip(tmp_path / "wmc.csv", "X1", "weidmueller")
    phx = (tmp_path / "phoenix.csv").read_text(encoding="utf-8")
    assert phx.splitlines()[0] == "Strip,Position,Tag,Article,Internal,External,Jumper,Accessory"
    assert ",jumper" in phx
    assert "3030161" in phx
    assert "J1" in phx
    assert (tmp_path / "wago.csv").read_text(encoding="utf-8").splitlines()[0] == phx.splitlines()[0]
    assert "3030161" in (tmp_path / "wago.csv").read_text(encoding="utf-8")
    assert "3030161;J1;" in (tmp_path / "wmc.csv").read_text(encoding="utf-8")
    assert phoenix["rows"] >= 1 and wago["rows"] >= 1 and wmc["rows"] >= 1


@pytest.mark.confirm_generic_layout
def test_strip_drawing_shows_jumper_part():
    session = Session.create("Draw")
    session.place("iec.terminals.phoenix_ut_2p5", ref="X1-1", x=40, y=20)
    session.place("iec.terminals.phoenix_ut_2p5", ref="X1-2", x=40, y=50)
    session.place("iec.terminals.phoenix_ut_2p5", ref="X1-3", x=40, y=80)
    session.jumper_terminals(["X1-1", "X1-3"])
    layout = session.open_terminal_strip("X1")
    jumper = layout["jumpers"][0]
    assert jumper["id"] == "J1"
    assert jumper["part_number"] == "FBS 3-5"
    assert jumper["catalog"] == "3030174"
    assert jumper["poles"] == 3
    svg = sheet_to_svg(session.project.sheet(layout["sheet"]), session.catalog, session.project)
    assert "J1" in svg
    assert "FBS 3-5" in svg


def _label_box(jumper: dict) -> tuple[float, float, float, float]:
    from panelcad.core.jumper import _text_width

    size = float(jumper["label_size"])
    width = _text_width(str(jumper["label"]), size)
    cx = float(jumper["label_x"])
    bottom = float(jumper["label_y"])
    return (cx - width / 2.0, cx + width / 2.0, bottom - size, bottom)


@pytest.mark.confirm_generic_layout
def test_jumper_label_clears_dots_bar_and_neighbor():
    """2-pole, 3-pole, 10-pole, and two adjacent jumpers keep text off the hardware."""

    session = Session.create("Label clearance")
    for i in range(1, 16):
        session.place("iec.terminals.phoenix_ut_2p5", ref=f"X1-{i}", x=40, y=20 * i)
    session.jumper_terminals(["X1-1", "X1-3"])
    session.jumper_terminals(["X1-4", "X1-5"])
    session.jumper_terminals(["X1-6", "X1-15"])
    layout = session.open_terminal_strip("X1")
    jumpers = {jumper["id"]: jumper for jumper in layout["jumpers"]}
    assert set(jumpers) >= {"J1", "J2", "J3"}
    assert jumpers["J1"]["poles"] == 3
    assert jumpers["J2"]["poles"] == 2
    assert jumpers["J3"]["poles"] == 10
    assert jumpers["J2"]["label"] == "J2"
    assert "J3" in jumpers["J3"]["label"]
    assert "FBS" in jumpers["J3"]["label"]
    by_ref = {item["ref"]: item for item in layout["items"] if item["kind"] == "terminal"}
    for jumper in jumpers.values():
        block = by_ref[jumper["refs"][0]]
        radius = min(1.05, float(block["w"]) * 0.16)
        dot_y = float(block["y"]) + min(8.0, float(block["h"]) * 0.18)
        bar_y = float(jumper["y"])
        left, right, top, bottom = _label_box(jumper)
        assert bottom <= dot_y - radius - 1.5, (jumper["id"], bottom, dot_y)
        assert bottom <= bar_y - 1.5, (jumper["id"], bottom, bar_y)
        assert top < bottom
        assert left < right
    adjacent = (_label_box(jumpers["J1"]), _label_box(jumpers["J2"]))
    a, b = adjacent
    overlap_x = a[0] < b[1] and b[0] < a[1]
    overlap_y = a[2] < b[3] and b[2] < a[3]
    assert not (overlap_x and overlap_y)


def test_project_library_can_add_a_jumper(tmp_path: Path):
    dest = tmp_path / "job.pcad.yaml"
    session = Session.create("User jumper")
    session.save(dest)
    lib = dest.parent / "library" / "shop_jumpers.yaml"
    lib.parent.mkdir(parents=True)
    lib.write_text(
        "library: shop\n"
        "jumpers:\n"
        "  - family: generic\n"
        "    pitch_mm: 5.2\n"
        "    manufacturer: ShopCo\n"
        "    kind: bridge\n"
        "    items:\n"
        "      - { poles: 2, part_number: SHOP-J2, catalog: SJ-2 }\n",
        encoding="utf-8",
    )
    again = Session.open(dest)
    device = again.catalog.get("shop.jumpers.generic_2p_5p2")
    assert device.properties["part_number"] == "SHOP-J2"
    assert device.properties["manufacturer"] == "ShopCo"
    extra = Catalog()
    extra.load_file(lib)
    assert extra.get("shop.jumpers.generic_2p_5p2").properties["catalog"] == "SJ-2"
