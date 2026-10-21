"""Enclosure auto-size: fit, catalog pick, heat, units, undo."""

from pathlib import Path

import pytest

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.core.enclosure import (
    SizingOptions,
    device_heat,
    enclosure_surface_m2,
    hp_to_kw,
    loss_from_output,
    recommend_enclosure,
    temperature_rise_k,
)
from panelcad.examples.dol import build_dol
from panelcad.library.enclosures import CONFIRMED_CATALOGS, load_enclosures
from tests.support import confirm_layout_occupants


@pytest.fixture(autouse=True)
def _confirm_before_enclosure_size(monkeypatch):
    from panelcad.api.session import Session
    from panelcad.core import enclosure as enc_mod

    orig_sess = Session.recommend_enclosure
    orig_core = enc_mod.recommend_enclosure

    def _sess(self, *args, **kwargs):
        confirm_layout_occupants(self)
        return orig_sess(self, *args, **kwargs)

    def _core(project, catalog, **kwargs):
        confirm_layout_occupants(project=project, catalog=catalog)
        return orig_core(project, catalog, **kwargs)

    monkeypatch.setattr(Session, "recommend_enclosure", _sess)
    monkeypatch.setattr(enc_mod, "recommend_enclosure", _core)


def test_shipped_catalog_numbers_are_confirmed():
    library = load_enclosures()
    assert library.boxes
    for box in library.boxes:
        if not box.catalog:
            continue
        assert box.catalog in CONFIRMED_CATALOGS, box.catalog
        assert box.url.startswith(
            ("https://www.nvent.com/", "https://cdn.automationdirect.com/")
        ), box.catalog


def test_heat_math_and_vfd_estimate():
    # 100 W on 1 m² at 5.5 W/m²·K → 18.18 K
    assert abs(temperature_rise_k(100.0, 1.0, 5.5) - 100.0 / 5.5) < 1e-9
    assert abs(hp_to_kw(10.0) - 7.46) < 1e-9
    # 1.5 kW at 97% → P*(1/η − 1)
    assert abs(loss_from_output(1500.0, 0.97) - 1500.0 * (1 / 0.97 - 1)) < 1e-6
    s = Session.create("Heat")
    u = s.place("iec.drives.vfd_1k5", ref="U1", x=20, y=20)
    watts, source = device_heat(s.catalog.get(u.library_id), u, SizingOptions())
    assert source == "vfd_estimate"
    assert watts is not None and watts > 30
    s.set_component_properties("U1", {"watts": 12})
    watts, source = device_heat(s.catalog.get(u.library_id), s.sheet().component("U1"), SizingOptions())
    assert source == "library"
    assert watts == 12


def test_wall_mount_surface_excludes_back():
    library = load_enclosures()
    box = next(b for b in library.boxes if b.id == "wall-16x12x8-n12")
    h, w, d = box.height_mm / 1000.0, box.width_mm / 1000.0, box.depth_mm / 1000.0
    assert abs(enclosure_surface_m2(box) - (w * h + 2 * h * d + 2 * w * d)) < 1e-9
    floor = next(b for b in library.boxes if b.id == "floor-60x36x12-n12")
    fh, fw, fd = floor.height_mm / 1000.0, floor.width_mm / 1000.0, floor.depth_mm / 1000.0
    assert abs(enclosure_surface_m2(floor) - (2 * fh * fw + 2 * fh * fd + fw * fd)) < 1e-9


def test_smallest_standard_box_fits_din_row():
    s = Session.create("Fit")
    s.set_title_block(standard="NEMA/ANSI")
    s.place("iec.breakers.mcb_1p_16a", ref="Q1", x=20, y=20)
    s.place("iec.contactors.ac3_9a", ref="K1", x=40, y=20)
    rec = s.recommend_enclosure()
    assert rec["applied"] is False
    box = rec["enclosure"]
    assert box is not None
    assert rec["fit"] is True
    packed_w = rec["needed"]["packed_width_mm"]
    packed_h = rec["needed"]["packed_height_mm"]
    spare = float(rec["needed"]["spare_pct"])
    # needed.height still reports packed height times (1 + spare). The box
    # itself is the smallest confirmed catalog row whose panel covers the
    # pack and meets the fill band plus spare height. A tight 12×12 that
    # covers the row with almost no leftover height steps up one library size.
    assert rec["needed"]["height_mm"] + 1e-6 >= packed_h * (1.0 + spare)
    assert box["panel_width_mm"] + 1e-6 >= packed_w
    assert box["panel_height_mm"] + 1e-6 >= packed_h
    assert box["depth_mm"] + 1e-6 >= rec["needed"]["depth_mm"]
    assert box["catalog"], box["id"]
    fill = rec["fill"]
    assert fill["target"] == 0.65
    if fill["stepped_up"]:
        assert fill["tight_id"] and fill["tight_id"] != box["id"]
    else:
        from panelcad.core.enclosure import _candidate_boxes, options_from_defaults
        from panelcad.library.enclosures import load_enclosures

        options = options_from_defaults(rec["options"])
        for cand in _candidate_boxes(load_enclosures(), options):
            if cand.id == box["id"]:
                break
            if not cand.catalog:
                continue
            covers = (
                cand.panel_width_mm + 1e-6 >= packed_w
                and cand.panel_height_mm + 1e-6 >= packed_h
                and cand.depth_mm + 1e-6 >= rec["needed"]["depth_mm"]
            )
            assert not covers, cand.id
    # A couple of DIN modules should not jump to a floor-mount cabinet.
    assert box["mount"] == "wall"
    assert box["height_in"] <= 24
    # Recommend never writes.
    assert s.project.sheets[0].enclosure is None or s.project.sheets[0].kind != "panel_layout"


def test_selection_picks_smaller_before_larger():
    s = Session.create("Pick")
    s.set_title_block(standard="NEMA/ANSI")
    s.place("iec.breakers.mcb_1p_16a", ref="Q1", x=20, y=20)
    small = s.recommend_enclosure()
    s.place("iec.drives.vfd_1k5", ref="U1", x=80, y=20)
    s.place("iec.drives.vfd_1k5", ref="U2", x=160, y=20)
    s.place("iec.power.control_transformer", ref="T1", x=80, y=100)
    big = s.recommend_enclosure()
    def _vol(row: dict) -> float:
        return row["height_in"] * row["width_in"] * row["depth_in"]

    assert _vol(small["enclosure"]) < _vol(big["enclosure"])


def test_units_follow_drawing_standard():
    iec = Session.create("IEC")
    iec.set_title_block(standard="IEC")
    iec.place("iec.breakers.mcb_1p_16a", ref="Q1", x=20, y=20)
    rec_iec = iec.recommend_enclosure()
    assert rec_iec["units"] == "mm"
    assert rec_iec["standard"] == "IEC"
    assert "mm" in rec_iec["enclosure"]["name"]

    nema = Session.create("NEMA")
    nema.set_title_block(standard="NEMA/ANSI")
    nema.place("iec.breakers.mcb_1p_16a", ref="Q1", x=20, y=20)
    rec_nema = nema.recommend_enclosure()
    assert rec_nema["units"] == "in"
    assert "in" in rec_nema["enclosure"]["name"]


def test_apply_is_one_undo_and_does_not_write_without_apply():
    s = build_dol()
    before = s.project.sheet("enclosure").enclosure.model_dump()
    rec = s.recommend_enclosure()
    after_rec = s.project.sheet("enclosure").enclosure.model_dump()
    assert after_rec == before
    applied = s.apply_enclosure(place_parts=True, recommendation=rec)
    assert applied["applied"] is True
    enc = s.project.sheet("enclosure").enclosure
    assert enc is not None
    assert abs(enc.width_mm - rec["enclosure"]["width_mm"]) < 1e-6
    assert enc.nema_type
    assert s.can_undo()
    assert s.undo()
    restored = s.project.sheet("enclosure").enclosure
    assert restored is not None
    assert abs(restored.width_mm - before["width_mm"]) < 1e-6
    assert restored.name == before["name"]
    assert s.redo()
    assert abs(s.project.sheet("enclosure").enclosure.width_mm - rec["enclosure"]["width_mm"]) < 1e-6


@pytest.mark.confirm_generic_layout
def test_place_parts_puts_din_and_blocks_on_the_panel():
    s = build_dol()
    rec = s.apply_enclosure(place_parts=True)
    enc = s.sheet("enclosure")
    refs = {c.id for c in enc.components}
    assert "Q1" in refs
    assert "K1" in refs
    assert "U1" in refs
    assert "M1" not in refs
    assert enc.rails
    assert enc.trays
    roles = {item["ref"]: item["role"] for item in rec["arrangement"]["items"] if item.get("ref")}
    assert roles.get("U1") == "block"
    assert rec["door"]["count"] >= 1


def test_heat_rise_check_and_missing_footprint():
    s = Session.create("Checks")
    s.add_sheet("panel", kind="panel_layout")
    s.set_enclosure(width_mm=200, height_mm=200, depth_mm=100, x=20, y=20, sheet_id="panel")
    s.place("iec.drives.vfd_1k5", ref="U1", x=30, y=30, sheet_id="main")
    s.set_component_properties("U1", {"watts": 400}, sheet_id="main")
    heat = [row for row in s.design_checks() if row["check"] == "heat_rise_over_limit"]
    assert heat
    t = Session.create("NoSize")
    t.place("iec.breakers.mcb_1p_16a", ref="Q1", x=20, y=20)
    t.set_panel("Q1", width_mm=0, height_mm=0, in_panel=True, din_rail=True)
    missing = [row for row in t.design_checks() if row["check"] == "missing_footprint" and row["ref"] == "Q1"]
    assert missing


def test_agent_and_cli_recommend_do_not_apply(tmp_path: Path):
    s = build_dol()
    path = tmp_path / "dol.pcad.yaml"
    s.save(path)
    before = Session.open(path).project.sheet("enclosure").enclosure.width_mm
    agent = Agent()
    assert agent.handle({"id": 1, "op": "open", "path": str(path)})["ok"]
    r = agent.handle({"id": 2, "op": "size_enclosure", "apply": False})
    assert r["ok"]
    assert r["result"]["applied"] is False
    assert r["result"]["enclosure"]
    again = Session.open(path)
    assert again.project.sheet("enclosure").enclosure.width_mm == before
    applied = agent.handle({"id": 3, "op": "apply_enclosure", "place_parts": False})
    assert applied["ok"]
    assert applied["result"]["applied"] is True


def test_dol_example_gets_a_real_box():
    s = build_dol()
    rec = s.recommend_enclosure()
    assert rec["fit"] is True
    assert rec["parts"]
    assert rec["heat"]["formula"]
    assert rec["assumptions"]["spare_pct"] == 0.25
    assert rec["enclosure"]["nema"] in {"12", "4", "4X", "1"}
    assert rec["enclosure"]["mount"] == "wall"
    assert rec["enclosure"]["height_in"] <= 48
    text = "\n".join(rec["reasons"])
    assert "Target spare" in text
    assert "Heat is reported and does not change the catalog box." in text
    raw = recommend_enclosure(s.project, s.catalog)
    assert raw["enclosure"]["id"] == rec["enclosure"]["id"]


def test_nema_4x_uses_stainless_k():
    s = Session.create("4X")
    s.place("iec.drives.vfd_1k5", ref="U1", x=20, y=20)
    rec = s.recommend_enclosure(nema="4X", material="stainless")
    assert rec["enclosure"]["nema"] == "4X"
    assert rec["enclosure"]["material"] == "stainless"
    assert rec["heat"]["k_w_m2k"] == 4.5


def _multi_motor_session() -> Session:
    s = Session.create("Several DOL + VFD")
    s.set_title_block(standard="NEMA/ANSI", company="George's Shop", drawing_number="E-SIZE")
    s.insert_circuit(kind="dol", control="local", kw=1.5)
    s.insert_circuit(kind="dol", control="local", kw=4.0)
    s.insert_circuit(kind="vfd", drive="generic_1k5", control="local", kw=5.5)
    s.insert_circuit(kind="vfd", drive="generic_1k5", kw=15.0)
    return s


def test_din_wraps_across_rows_and_picks_by_panel_area():
    """Many DIN modules wrap; do not lock a 36-wide / 48-tall box for one long rail."""

    s = _multi_motor_session()
    rec = s.recommend_enclosure()
    box = rec["enclosure"]
    assert rec["fit"] is True
    assert box["mount"] == "wall"
    assert box["height_in"] * box["width_in"] < 48 * 36 - 1
    assert box["height_in"] <= 36
    assert box["width_in"] <= 30
    din_parts = [p for p in rec["parts"] if p["role"] == "din"]
    assert len(din_parts) >= 8
    din_rails = [item for item in rec["arrangement"]["items"] if item["kind"] == "rail" and item["role"] == "din"]
    assert din_rails


def test_terminal_strip_of_30_changes_box_size():
    s = Session.create("Thirty terminals")
    s.set_title_block(standard="NEMA/ANSI")
    s.place("iec.breakers.mcb_1p_16a", ref="Q1", x=20, y=20)
    small = s.recommend_enclosure()
    assert sum(1 for p in small["parts"] if p["role"] == "terminal") == 0
    for _ in range(30):
        s.add_terminal("X1")
    big = s.recommend_enclosure()
    terminals = [p for p in big["parts"] if p["role"] == "terminal" and "terminals" in p["library_id"]]
    hardware = [p for p in big["parts"] if p["role"] == "terminal" and "accessor" in p["category"]]
    assert len(terminals) == 30
    assert hardware, "end covers / end stops must be counted with the strip"
    assert any("30 terminal" in line for line in big["reasons"])
    term_rails = [item for item in big["arrangement"]["items"] if item.get("role") == "terminal" and item["kind"] == "rail"]
    assert term_rails
    small_area = small["enclosure"]["height_in"] * small["enclosure"]["width_in"]
    big_area = big["enclosure"]["height_in"] * big["enclosure"]["width_in"]
    grew = (
        big["enclosure"]["id"] != small["enclosure"]["id"]
        or big["needed"]["height_mm"] > small["needed"]["height_mm"] + 20
        or big_area > small_area
    )
    assert grew
    assert abs(big["arrangement"]["width_mm"] - big["enclosure"]["width_mm"]) < 1e-3
    assert abs(big["arrangement"]["height_mm"] - big["enclosure"]["height_mm"]) < 1e-3


def _interior_overlap(ax, ay, aw, ah, bx, by, bw, bh, eps: float = 0.05) -> bool:
    return ax + eps < bx + bw and ax + aw > bx + eps and ay + eps < by + bh and ay + ah > by + eps


def _assert_wiring_field_clear(items: list[dict], frame_w: float, frame_h: float, label: str) -> None:
    ducts = [i for i in items if i.get("kind") == "duct"]
    placed = [i for i in items if i.get("kind") in {"device", "block", "rail"}]
    assert ducts, f"{label}: missing ducts"
    assert placed, f"{label}: missing parts or rails"
    for item in placed:
        assert item["x"] >= -0.05, f"{label}: {item.get('ref')} left of back panel"
        assert item["y"] >= -0.05, f"{label}: {item.get('ref')} above back panel"
        assert item["x"] + item["w"] <= frame_w + 0.05, f"{label}: {item.get('ref')} past right edge"
        assert item["y"] + item["h"] <= frame_h + 0.05, f"{label}: {item.get('ref')} past bottom edge"
        for duct in ducts:
            assert not _interior_overlap(
                item["x"], item["y"], item["w"], item["h"],
                duct["x"], duct["y"], duct["w"], duct["h"],
            ), f"{label}: {item.get('kind')} {item.get('ref')} intersects {duct.get('ref')}"


def _applied_layout_items(session: Session, sheet_id: str | None = None) -> tuple[list[dict], float, float]:
    from panelcad.core.layout import rail_aabb, tray_aabb
    from panelcad.library.panel import display_footprint_size

    sheet = session.sheet(sheet_id) if sheet_id else next(sh for sh in session.project.sheets if sh.kind == "panel_layout")
    enc = sheet.enclosure
    assert enc is not None
    scale = float(enc.draw_scale or 1.0)
    items: list[dict] = []
    for tray in sheet.trays:
        x, y, w, h = tray_aabb(tray)
        items.append({"kind": "duct", "ref": tray.id, "x": x, "y": y, "w": w, "h": h})
    for rail in sheet.rails:
        x, y, w, h = rail_aabb(rail)
        items.append({"kind": "rail", "ref": rail.id, "x": x, "y": y, "w": w, "h": h})
    for comp in sheet.components:
        dw, dh = display_footprint_size(session.catalog.get(comp.library_id), comp)
        items.append({"kind": "device", "ref": comp.id, "x": comp.x, "y": comp.y, "w": dw, "h": dh})
    return items, enc.x + enc.width_mm * scale, enc.y + enc.height_mm * scale


def _assert_example_field_clear(session: Session, label: str) -> None:
    rec = session.recommend_enclosure()
    arr = rec["arrangement"]
    _assert_wiring_field_clear(arr["items"], arr["width_mm"], arr["height_mm"], f"{label} preview")
    session.apply_enclosure(place_parts=True, recommendation=rec)
    sheet = next(sh for sh in session.project.sheets if sh.kind == "panel_layout")
    applied, right, bottom = _applied_layout_items(session, sheet.id)
    # Applied coords are on the sheet; shift the frame origin to the enclosure.
    enc = sheet.enclosure
    scale = float(enc.draw_scale or 1.0)
    shifted = [
        {**row, "x": row["x"] - enc.x, "y": row["y"] - enc.y}
        for row in applied
    ]
    _assert_wiring_field_clear(shifted, enc.width_mm * scale, enc.height_mm * scale, f"{label} applied")
    del right, bottom


def test_wiring_field_keeps_parts_and_rails_off_ducts():
    _assert_example_field_clear(build_dol(), "DOL")
    _assert_example_field_clear(_multi_motor_session(), "multi")
    s = Session.create("Thirty terminals")
    s.set_title_block(standard="NEMA/ANSI")
    s.place("iec.breakers.mcb_1p_16a", ref="Q1", x=20, y=20)
    for _ in range(30):
        s.add_terminal("X1")
    _assert_example_field_clear(s, "30-terminal")


def test_applied_arrangement_matches_preview_frame():
    s = build_dol()
    rec = s.recommend_enclosure()
    assert abs(rec["arrangement"]["width_mm"] - rec["enclosure"]["width_mm"]) < 1e-3
    assert abs(rec["arrangement"]["height_mm"] - rec["enclosure"]["height_mm"]) < 1e-3
    applied = s.apply_enclosure(place_parts=True, recommendation=rec)
    assert abs(applied["arrangement"]["width_mm"] - applied["enclosure"]["width_mm"]) < 1e-3
    assert abs(applied["arrangement"]["height_mm"] - applied["enclosure"]["height_mm"]) < 1e-3
    enc = s.project.sheet("enclosure").enclosure
    scale = float(enc.draw_scale or 1.0)
    for item in applied["arrangement"]["items"]:
        assert item["x"] >= -1
        assert item["y"] >= -1
        assert item["x"] + item["w"] <= enc.width_mm + 1
        assert item["y"] + item["h"] <= enc.height_mm + 1
    for comp in s.project.sheet("enclosure").components:
        assert comp.x + 1 >= enc.x
        assert comp.y + 1 >= enc.y
        assert comp.x <= enc.x + enc.width_mm * scale + 1


def _recommendation_for_box(session: Session, box_id: str) -> dict:
    """A real arrangement framed on one catalog box (the 36×30 the shop applied)."""

    from panelcad.core.enclosure import (
        arrange_backpanel,
        collect_parts,
        expand_frame_to_panel,
        frame_on_box,
        options_from_defaults,
        usable_inner_width,
    )

    rec = session.recommend_enclosure()
    lib = load_enclosures(session.path.parent if session.path else None)
    box = lib.by_id(box_id)
    assert box is not None
    opts = options_from_defaults(lib.defaults)
    cabinet, _door = collect_parts(session.project, session.catalog)
    inner = usable_inner_width(box, opts)
    items, _packed_w, _packed_h, _packed_d = arrange_backpanel(cabinet, opts, inner_w=inner)
    items = expand_frame_to_panel(items, box.panel_width_mm, box.panel_height_mm, opts)
    items, frame_w, frame_h = frame_on_box(items, box)
    row = box.to_row()
    row["name"] = box.display_name(units="mm")
    rec = dict(rec)
    rec["enclosure"] = row
    rec["arrangement"] = {
        "width_mm": frame_w,
        "height_mm": frame_h,
        "items": [item.to_row() for item in items],
    }
    return rec


def _assert_no_part_overlap(session: Session, sheet) -> None:
    from panelcad.core.layout import footprint_size, rects_overlap

    boxes = []
    for comp in sheet.components:
        device = session.catalog.get(comp.library_id)
        width, height = footprint_size(device, comp)
        if width <= 0 or height <= 0:
            continue
        boxes.append((comp.id, comp.x, comp.y, width, height))
    for index, (ref, x, y, width, height) in enumerate(boxes):
        for other, ox, oy, ow, oh in boxes[index + 1 :]:
            assert not rects_overlap(x, y, width, height, ox, oy, ow, oh), (ref, other)


def _assert_true_size_on_backplate(session: Session) -> None:
    from panelcad.core.layout import backplate_sheet_bounds, rects_overlap, tray_aabb
    from panelcad.core.models import Component
    from panelcad.library.panel import display_footprint_size

    sheet = next(sh for sh in session.project.sheets if sh.kind == "panel_layout")
    enc = sheet.enclosure
    assert enc is not None
    left, top, right, bottom = backplate_sheet_bounds(enc)
    ducts = [tray_aabb(tray) for tray in sheet.trays]
    assert right > left and bottom > top
    for comp in sheet.components:
        device = session.catalog.get(comp.library_id)
        ghost = Component(
            id=comp.id,
            library_id=comp.library_id,
            x=0.0,
            y=0.0,
            scale=1.0,
            properties=dict(comp.properties),
        )
        true_w, true_h = display_footprint_size(device, ghost)
        drawn_w, drawn_h = display_footprint_size(device, comp)
        assert comp.scale == pytest.approx(float(enc.draw_scale), rel=0.02, abs=0.002)
        assert drawn_w == pytest.approx(true_w * comp.scale, rel=0.02, abs=0.05)
        assert drawn_h == pytest.approx(true_h * comp.scale, rel=0.02, abs=0.05)
        assert comp.x >= left - 0.6, comp.id
        assert comp.y >= top - 0.6, comp.id
        assert comp.x + drawn_w <= right + 0.6, comp.id
        assert comp.y + drawn_h <= bottom + 0.6, comp.id
        for duct in ducts:
            assert not rects_overlap(comp.x, comp.y, drawn_w, drawn_h, *duct), comp.id


def _resized_36x30() -> Session:
    """DOL enclosure forced onto a landscape A3 36×30 box, keeping the existing parts."""

    from panelcad.core.layout import fit_enclosure_scale
    from panelcad.core.models import Component
    from panelcad.library.panel import display_footprint_size
    from panelcad.render.titleblock import working_rect

    session = build_dol()
    sheet = session.project.sheet("enclosure")
    sheet.page.orientation = "landscape"
    enc = sheet.enclosure
    assert enc is not None
    wx, wy, ww, wh = working_rect(sheet)
    scale = fit_enclosure_scale(enc.width_mm, enc.height_mm, ww - 8.0, wh - 12.0)
    enc.x = wx + 4.0
    enc.y = wy + 4.0
    enc.draw_scale = scale
    cursor = enc.x + 4.0
    row_y = enc.y + 6.0
    limit = enc.x + enc.width_mm * scale - 4.0
    for comp in sheet.components:
        if comp.id in {"U1", "P1"}:
            continue
        comp.scale = scale
        device = session.catalog.get(comp.library_id)
        width, height = display_footprint_size(device, comp)
        if cursor + width > limit:
            cursor = enc.x + 4.0
            row_y += height + 2.0
        comp.x = cursor
        comp.y = row_y
        cursor += width + 2.0
    outline_bottom = enc.y + enc.height_mm * scale
    drive = sheet.component("U1")
    drive.scale = scale
    drive_w, drive_h = display_footprint_size(session.catalog.get(drive.library_id), drive)
    drive.x = enc.x + 6.0
    drive.y = outline_bottom - drive_h - 0.5
    hmi = Component(
        id="P1",
        library_id="iec.hmi.panel_7in",
        x=0.0,
        y=0.0,
        scale=scale,
        properties={},
    )
    hmi_w, hmi_h = display_footprint_size(session.catalog.get(hmi.library_id), hmi)
    hmi.x = drive.x + drive_w + 4.0
    hmi.y = outline_bottom - hmi_h - 0.5
    sheet.components.append(hmi)
    assert drive.y + drive_h <= outline_bottom + 0.2
    assert hmi.y + hmi_h <= outline_bottom + 0.2
    rec = _recommendation_for_box(session, "wall-36x30x10-n12")
    session.apply_enclosure(place_parts=False, recommendation=rec)
    return session


def test_resize_to_36x30_keeps_true_mm_inside_the_backplate():
    """0.4.0 left parts at the old draw scale, so P1/U1 hung past the new subpanel."""

    session = _resized_36x30()
    sheet = session.project.sheet("enclosure")
    enc = sheet.enclosure
    assert enc is not None
    assert enc.width_mm == pytest.approx(762.0, abs=0.2)
    assert enc.height_mm == pytest.approx(914.4, abs=0.2)
    assert sheet.component("P1").id == "P1"
    assert sheet.component("U1").id == "U1"
    _assert_true_size_on_backplate(session)


@pytest.mark.confirm_generic_layout
def test_resize_seats_every_din_device_on_a_rail():
    """After apply, each DIN clip lies on a rail. A short row gains another TS35."""

    from panelcad.core.layout import MODULE_GAP_MM, clip_offset, footprint_size, is_rail_module

    session = _resized_36x30()
    sheet = session.project.sheet("enclosure")
    enc = sheet.enclosure
    assert enc is not None
    scale = float(enc.draw_scale)
    seen = set()
    for comp in sheet.components:
        device = session.catalog.get(comp.library_id)
        if not is_rail_module(device, comp):
            continue
        seen.add(comp.id)
        width, _height = footprint_size(device, comp)
        clip_y = comp.y + clip_offset(device, comp)
        mounted = [
            rail
            for rail in sheet.rails
            if abs(clip_y - rail.y) <= 0.6
            and comp.x >= rail.x - 0.6
            and comp.x + width <= rail.x + rail.length_mm + 0.6
        ]
        assert mounted, comp.id
    # F1 is a published plug-on overload (stacks under K1). G1 is an 85 mm PSU block.
    assert {"Q1", "K1", "A1", "A2", "A3", "A4"} <= seen
    assert "F1" not in seen
    assert "G1" not in seen
    gap = MODULE_GAP_MM * scale

    def _separated(left_id: str, right_id: str) -> None:
        left = sheet.component(left_id)
        right = sheet.component(right_id)
        left_dev = session.catalog.get(left.library_id)
        right_dev = session.catalog.get(right.library_id)
        lw, lh = footprint_size(left_dev, left)
        rw, rh = footprint_size(right_dev, right)
        assert (
            right.x >= left.x + lw + gap - 0.15
            or left.x >= right.x + rw + gap - 0.15
            or right.y >= left.y + lh + gap - 0.15
            or left.y >= right.y + rh + gap - 0.15
        ), f"{right_id} is flush against {left_id}"

    _separated("A2", "A3")
    _separated("A2", "A4")


def test_rail_gap_uses_tray_clearance_until_the_row_fills_the_opening():
    from panelcad.core.layout import TRAY_TO_RAIL_X_MM, _rail_gap_beside_ducts

    assert _rail_gap_beside_ducts(0.0, 400.0, 40.0, 0.5) == pytest.approx(20.0)
    assert _rail_gap_beside_ducts(0.0, 100.0, 90.0, 0.5) == pytest.approx(5.0)
    assert _rail_gap_beside_ducts(0.0, 100.0, 120.0, 1.0) == pytest.approx(0.8)
    assert TRAY_TO_RAIL_X_MM == 40.0


def test_short_row_starts_a_tray_clearance_off_the_duct():
    from panelcad.core.layout import (
        TRAY_TO_RAIL_X_MM,
        clip_offset,
        finish_rails,
        footprint_size,
        tray_aabb,
    )
    from panelcad.core.models import Component, Enclosure, Rail, WireTray

    session = Session.create("Short row")
    device = session.catalog.get("iec.breakers.mcb_3p_32a")
    scale = 0.5
    enc = Enclosure(
        x=10,
        y=10,
        width_mm=900,
        height_mm=700,
        depth_mm=250,
        draw_scale=scale,
        panel_width_mm=800,
        panel_height_mm=600,
    )
    left = WireTray(id="wt-left", x=40, y=30, length_mm=220, width_mm=22, orientation="v")
    right = WireTray(id="wt-right", x=420, y=30, length_mm=220, width_mm=22, orientation="v")
    rail = Rail(id="r1", x=70, y=120, length_mm=300, height_mm=35 * scale)
    ghost = Component(id="Q1", library_id=device.library_id, x=0, y=0, scale=scale)
    width, _height = footprint_size(device, ghost)
    clip = clip_offset(device, ghost)
    comp = Component(
        id="Q1",
        library_id=device.library_id,
        x=left.x + left.width_mm + 1.0,
        y=rail.y - clip,
        scale=scale,
        properties={"role": "breaker", "layout_band": "power"},
    )
    finish_rails([comp], [rail], [left, right], enc, lambda _comp: device)
    face = tray_aabb(left)[0] + tray_aabb(left)[2]
    assert comp.x == pytest.approx(face + TRAY_TO_RAIL_X_MM * scale, abs=0.4)
    assert comp.x + width < tray_aabb(right)[0] - 0.4
    assert abs(comp.y + clip - rail.y) < 0.4


def test_terminal_strip_moves_with_the_rail_end_stops_included():
    """A rail inset from the duct carries the whole strip, stops included.

    The bodies start 1.6 mm off the clip line, which used to hide them from
    the row match and leave the stops behind.
    """

    from panelcad.core.layout import (
        TRAY_TO_RAIL_X_MM,
        clip_offset,
        finish_rails,
        footprint_size,
        rects_overlap,
        tray_aabb,
    )
    from panelcad.core.models import Component, Enclosure, Rail, WireTray

    session = Session.create("Strip")
    terminal = session.catalog.get("iec.terminals.x_2p5")
    stop = session.catalog.get("iec.accessories.end_stop")
    scale = 0.5
    enc = Enclosure(
        x=10,
        y=10,
        width_mm=900,
        height_mm=700,
        depth_mm=250,
        draw_scale=scale,
        panel_width_mm=800,
        panel_height_mm=600,
    )
    left = WireTray(id="wt-left", x=40, y=30, length_mm=220, width_mm=22, orientation="v")
    right = WireTray(id="wt-right", x=420, y=30, length_mm=220, width_mm=22, orientation="v")
    face = tray_aabb(left)[0] + tray_aabb(left)[2]
    rail = Rail(id="r-terminal-1", x=face, y=120, length_mm=200, height_mm=35 * scale)
    parts: list[Component] = []
    cursor = face
    for ident, device in (
        ("XS1", stop),
        ("X1", terminal),
        ("X2", terminal),
        ("XS2", stop),
    ):
        ghost = Component(id=ident, library_id=device.library_id, x=0, y=0, scale=scale)
        width, _height = footprint_size(device, ghost)
        clip = clip_offset(device, ghost)
        parts.append(
            Component(
                id=ident,
                library_id=device.library_id,
                x=cursor,
                y=rail.y - clip - 1.6,
                scale=scale,
                properties={"role": "terminal", "layout_band": "terminal", "accessory": "end_stop"}
                if ident.startswith("XS")
                else {"role": "terminal", "layout_band": "terminal"},
            )
        )
        cursor += width
    finish_rails(parts, [rail], [left, right], enc, lambda comp: session.catalog.get(comp.library_id))
    xs = [comp.x for comp in parts]
    assert min(xs) == pytest.approx(face + TRAY_TO_RAIL_X_MM * scale, abs=0.6)
    assert min(xs) >= rail.x - 0.4
    for comp in parts:
        device = session.catalog.get(comp.library_id)
        width, height = footprint_size(device, comp)
        assert abs(comp.y + clip_offset(device, comp) - rail.y) < 0.2, comp.id
        assert comp.x + width <= rail.x + rail.length_mm + 0.4, comp.id
        for other in parts:
            if other.id <= comp.id:
                continue
            ow, oh = footprint_size(session.catalog.get(other.library_id), other)
            assert not rects_overlap(comp.x, comp.y, width, height, other.x, other.y, ow, oh)
    # The stops stayed at the ends of the strip. They did not land on a terminal.
    assert parts[0].id == "XS1"
    assert parts[0].x < parts[1].x < parts[2].x < parts[3].x


def test_resize_rail_runs_duct_to_duct():
    from panelcad.core.layout import TRAY_TO_RAIL_X_MM, _draw_scale, rail_aabb, rects_overlap, tray_aabb

    session = _resized_36x30()
    sheet = session.project.sheet("enclosure")
    sides = [tray for tray in sheet.trays if tray.orientation == "v"]
    assert len(sides) >= 2
    left = min(sides, key=lambda tray: tray.x)
    right = max(sides, key=lambda tray: tray.x)
    lx, _ly, lw, _lh = tray_aabb(left)
    rx, _ry, _rw, _rh = tray_aabb(right)
    assert sheet.rails
    for rail in sheet.rails:
        box = rail_aabb(rail)
        for tray in sheet.trays:
            assert not rects_overlap(*box, *tray_aabb(tray)), f"{rail.id} overlaps {tray.id}"
        clear = TRAY_TO_RAIL_X_MM * _draw_scale(sheet.enclosure)
        # 0.8 mm is the stroke floor. TRAY_TO_RAIL_X_MM is used when the row
        # leaves that much room; a full row is not pushed into the far duct.
        assert lx + lw + 0.7 <= rail.x <= lx + lw + clear + 0.3
        end = rail.x + rail.length_mm
        assert rx - clear - 0.3 <= end <= rx - 0.7


def test_resize_labels_clear_parts_and_rails():
    from panelcad.core.layout import rail_aabb, rects_overlap
    from panelcad.library.panel import display_footprint_size
    from panelcad.render.layout import layout_label_avoid, plan_rail_label, plan_tray_label

    session = _resized_36x30()
    sheet = session.project.sheet("enclosure")
    enc = sheet.enclosure
    assert enc is not None
    scale = float(enc.draw_scale)
    parts = []
    for comp in sheet.components:
        device = session.catalog.get(comp.library_id)
        width, height = display_footprint_size(device, comp)
        if width > 0 and height > 0:
            parts.append((comp.id, (comp.x, comp.y, width, height)))
    rail_boxes = [(rail.id, rail_aabb(rail)) for rail in sheet.rails]
    for rail in sheet.rails:
        label = plan_rail_label(rail, scale, layout_label_avoid(sheet, session.catalog, "rail"))
        for ref, part in parts:
            assert not rects_overlap(*label.rect, *part), f"{rail.id} label hits {ref}"
        for ref, box in rail_boxes:
            assert not rects_overlap(*label.rect, *box), f"{rail.id} label hits {ref}"
    for tray in sheet.trays:
        label = plan_tray_label(tray, scale, layout_label_avoid(sheet, session.catalog, "tray"))
        for ref, part in parts:
            assert not rects_overlap(*label.rect, *part), f"{tray.id} label hits {ref}"
        for ref, box in rail_boxes:
            assert not rects_overlap(*label.rect, *box), f"{tray.id} label hits {ref}"


def test_resize_uses_the_largest_standard_scale_and_centers_the_box(tmp_path: Path):
    """1:1, 1:2, 1:2.5, 1:3, 1:3.5, 1:4, 1:5, 1:8, 1:10. Landscape A3 picks 1:4."""

    from panelcad.core.layout import STANDARD_DRAW_DENOMINATORS, format_draw_ratio
    from panelcad.render.titleblock import working_rect

    assert STANDARD_DRAW_DENOMINATORS == (1.0, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 8.0, 10.0)
    session = _resized_36x30()
    sheet = session.project.sheet("enclosure")
    enc = sheet.enclosure
    assert enc is not None
    assert enc.draw_scale == pytest.approx(0.25)
    assert format_draw_ratio(enc.draw_scale) == "1:4"
    wx, wy, ww, wh = working_rect(sheet)
    drawn_w = enc.width_mm * enc.draw_scale
    drawn_h = enc.height_mm * enc.draw_scale
    assert drawn_w <= ww + 0.1
    assert drawn_h <= wh + 0.1
    assert 914.4 / 3.5 > wh
    assert enc.x == pytest.approx(wx + (ww - drawn_w) / 2.0, abs=0.15)
    assert enc.y == pytest.approx(wy + (wh - drawn_h) / 2.0, abs=0.15)
    svg = session.export_svg(tmp_path / "resized.svg", sheet_id=sheet.id).read_text(encoding="utf-8")
    assert "(drawn 1:4)" in svg


def test_short_rail_gains_a_second_rail_trimmed_to_the_devices():
    from panelcad.core.layout import END_STOP_MM, clip_offset, footprint_size, reseat_din_modules
    from panelcad.core.models import Component, Enclosure, Rail

    session = Session.create("Short rail")
    sheet = session.add_sheet("panel", kind="panel_layout")
    sheet.enclosure = Enclosure(
        x=10.0,
        y=10.0,
        width_mm=400.0,
        height_mm=400.0,
        draw_scale=1.0,
        panel_width_mm=400.0,
        panel_height_mm=400.0,
    )
    sheet.rails = [Rail(id="r1", x=30.0, y=80.0, length_mm=70.0, height_mm=35.0)]
    sheet.trays = []
    sheet.components = [
        Component(id="Q1", library_id="iec.breakers.mcb_3p_32a", x=0.0, y=0.0, scale=1.0),
        Component(id="K1", library_id="iec.contactors.ac3_9a", x=90.0, y=0.0, scale=1.0),
    ]
    notes = reseat_din_modules(
        sheet.components,
        sheet.rails,
        sheet.trays,
        sheet.enclosure,
        device_of=lambda comp: session.catalog.get(comp.library_id),
        order_x={"Q1": 0.0, "K1": 1.0},
    )
    assert len(sheet.rails) >= 2
    assert any("Added TS35" in line for line in notes)
    for comp in sheet.components:
        device = session.catalog.get(comp.library_id)
        width, _height = footprint_size(device, comp)
        clip_y = comp.y + clip_offset(device, comp)
        assert any(
            abs(clip_y - rail.y) <= 0.6
            and comp.x >= rail.x - 0.4
            and comp.x + width <= rail.x + rail.length_mm + 0.4
            for rail in sheet.rails
        ), comp.id
    for rail in sheet.rails:
        seated = []
        for comp in sheet.components:
            device = session.catalog.get(comp.library_id)
            width, _height = footprint_size(device, comp)
            if abs(comp.y + clip_offset(device, comp) - rail.y) <= 0.6:
                seated.append((comp.x, width))
        assert seated, rail.id
        span = max(x + width for x, width in seated) - min(x for x, _width in seated)
        assert rail.length_mm == pytest.approx(span + 2.0 * END_STOP_MM, abs=0.6)


@pytest.mark.confirm_generic_layout
def test_place_parts_keeps_door_hardware_without_a_door_sheet(tmp_path: Path):
    """size_enclosure apply+place must not delete the HMI and door operators."""

    from panelcad.core.models import Component

    session = build_dol()
    sheet = session.project.sheet("enclosure")
    enc = sheet.enclosure
    assert enc is not None
    scale = float(enc.draw_scale or 1.0)
    added = (
        ("P1", "iec.hmi.panel_7in", 24.0),
        ("S0", "iec.operators.estop", 80.0),
        ("S1", "iec.operators.pb_no", 110.0),
        ("S2", "iec.operators.pb_nc", 140.0),
    )
    for ref, library_id, dx in added:
        sheet.components.append(
            Component(
                id=ref,
                library_id=library_id,
                x=enc.x + dx,
                y=enc.y + 8.0,
                scale=scale,
                properties={"legend": ref},
            )
        )
    before = len(sheet.components)
    assert before == 13
    door_refs = {ref for ref, _lib, _dx in added}
    path = tmp_path / "no-door-sheet.pcad.yaml"
    session.save(path)
    agent = Agent()
    opened = agent.handle({"op": "open", "path": str(path)})
    assert opened["ok"], opened
    applied = agent.handle({"op": "size_enclosure", "apply": True, "place_parts": True})
    assert applied["ok"], applied
    warnings = applied["result"].get("warnings") or []
    assert any("Created door sheet" in line for line in warnings)
    panel = next(sh for sh in agent.session.project.sheets if sh.kind == "panel_layout")
    door = next(sh for sh in agent.session.project.sheets if sh.kind == "door_layout")
    panel_refs = {comp.id for comp in panel.components}
    door_ids = {comp.id for comp in door.components}
    assert door_refs <= door_ids
    assert not (door_refs & panel_refs)
    assert len(door.components) >= len(door_refs)
    _assert_true_size_on_backplate(agent.session)
    _assert_no_part_overlap(agent.session, panel)
    _assert_no_part_overlap(agent.session, door)


def test_heat_reason_states_the_full_unknown_count():
    from panelcad.core.enclosure import SizingOptions, _reasons

    refs = [f"K{index}" for index in range(1, 21)]
    lines = _reasons([], [], SizingOptions(), 100.0, 80.0, None, False, {"unknown_refs": refs, "unknown_count": 20}, False, [], "mm")
    reason = next(line for line in lines if line.startswith("Heat unknown"))
    assert reason.startswith("Heat unknown (20):")
    assert "K1" in reason
    assert "K12" in reason
    assert "and 8 more" in reason
    assert "K20" not in reason


def test_apply_rails_are_duct_to_duct_or_stopped():
    """Every device rail runs duct to duct, or is trimmed with an end stop at each end."""

    from panelcad.core.layout import _opening_between_side_ducts, footprint_size

    session = build_dol()
    session.apply_enclosure(place_parts=True)
    sheet = next(sh for sh in session.project.sheets if sh.kind == "panel_layout")
    opening = _opening_between_side_ducts(sheet.trays, sheet.enclosure)
    assert opening is not None
    ox, length = opening
    for rail in sheet.rails:
        stretched = abs(rail.x - ox) <= 0.8 and abs(rail.length_mm - length) <= 0.8
        if stretched:
            continue
        seated = []
        stops = []
        for comp in sheet.components:
            device = session.catalog.get(comp.library_id)
            width, _height = footprint_size(device, comp)
            if abs(comp.y + 8.0 - rail.y) > max(8.0, rail.height_mm):
                continue
            if "end_stop" in comp.library_id or str(comp.properties.get("accessory") or "") == "end_stop":
                stops.append((comp.x, width))
            else:
                seated.append((comp.x, width))
        assert seated, rail.id
        device_left = min(x for x, _w in seated)
        device_right = max(x + w for x, w in seated)
        if rail.x + 1.0 < device_left:
            assert any(abs(x - rail.x) <= 1.2 for x, _w in stops), rail.id
        if rail.x + rail.length_mm > device_right + 1.0:
            assert any(abs(x + w - (rail.x + rail.length_mm)) <= 1.2 for x, w in stops), rail.id
