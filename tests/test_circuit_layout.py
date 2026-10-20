"""Panel, door, and BOM placement for Circuit Builder inserts."""

from __future__ import annotations

import pytest

from panelcad import Session
from panelcad.core.layout import footprint_size, heading_rect, legend_plate_rect, rects_overlap, tray_aabb
from tests.support import confirm_layout_occupants


@pytest.fixture(autouse=True)
def _confirm_before_circuit_pack(monkeypatch):
    """Packing-math tests confirm library envelopes; production code does not."""

    from panelcad.core import circuit_layout

    orig = circuit_layout.pack_circuit_layouts

    def _pack(session, parts, **kwargs):
        confirm_layout_occupants(session)
        return orig(session, parts, **kwargs)

    monkeypatch.setattr(circuit_layout, "pack_circuit_layouts", _pack)
from panelcad.io.bom import grouped_rows, instance_rows
from panelcad.library.parts import CatalogPart, PartsCatalog


def _iec(name: str) -> Session:
    session = Session.create(name)
    session.set_title_block(standard="IEC")
    return session


def _boxes(session: Session, sheet_id: str, *, legends: bool = False) -> list[tuple]:
    sheet = session.project.sheet(sheet_id)
    boxes = []
    for comp in sheet.components:
        device = session.catalog.get(comp.library_id)
        width, height = footprint_size(device, comp)
        boxes.append((comp.id, comp.x, comp.y, width, height))
        legend = str(comp.properties.get("legend") or "").strip()
        if legends and legend:
            boxes.append((comp.id + ":legend", *legend_plate_rect(comp.x, comp.y, width, height, legend)))
        if legends and str(comp.properties.get("layout_heading") or "").strip():
            boxes.append(
                (
                    comp.id + ":heading",
                    *heading_rect(comp.x, comp.y, width, height, str(comp.properties.get("layout_heading"))),
                )
            )
    for tray in sheet.trays:
        boxes.append((tray.id, *tray_aabb(tray)))
    return boxes


def _assert_clear(boxes: list[tuple]) -> None:
    for index, left in enumerate(boxes):
        for right in boxes[index + 1 :]:
            assert not rects_overlap(left[1], left[2], left[3], left[4], right[1], right[2], right[3], right[4]), (
                f"{left[0]} overlaps {right[0]}"
            )


def _panel(session: Session, result: dict):
    return session.project.sheet(result["layout"]["sheet"])


def _by_role(result: dict) -> dict[str, str]:
    return {row["role"]: row["id"] for row in result["components"]}


@pytest.mark.confirm_generic_layout
def test_star_delta_backplate_groups_and_door_legends():
    session = _iec("Star-delta layout")
    result = session.insert_circuit(kind="star_delta", control="local", kw=7.5)
    panel = _panel(session, result)
    roles = _by_role(result)
    main, delta, star = roles["main"], roles["delta"], roles["star"]
    assert {main, delta, star} == {"K1", "K2", "K3"}
    bodies = {comp.id: comp for comp in panel.components}
    assert {main, delta, star, roles["overload"], roles["breaker"], roles["timer"], roles["psu"]} <= set(bodies)
    rail_y = {}
    for ref in (main, delta, star):
        device = session.catalog.get(bodies[ref].library_id)
        from panelcad.core.layout import clip_offset, nearest_rail

        rail = nearest_rail(panel, bodies[ref].y + clip_offset(device, bodies[ref]))
        assert rail is not None
        rail_y[ref] = rail.id
    assert len(set(rail_y.values())) == 1
    order = sorted((bodies[ref].x, ref) for ref in (main, delta, star))
    assert [ref for _x, ref in order] == [main, delta, star]
    interlocks = [comp for comp in panel.components if comp.properties.get("accessory") == "mechanical_interlock"]
    assert len(interlocks) == 1
    interlock = interlocks[0]
    assert interlock.properties.get("part_number", "") == ""
    assert interlock.properties.get("catalog", "") == ""
    assert bodies[delta].x < interlock.x < bodies[star].x
    assert any(comp.library_id.endswith("accessories.end_stop") for comp in panel.components)
    assert sum(1 for comp in panel.components if comp.library_id.endswith("terminals.x_2p5")) == 3
    assert panel.trays, "wire duct between rails"
    _assert_clear(_boxes(session, panel.id))

    door = session.project.sheet(result["layout"]["door"])
    legends = {comp.properties.get("legend") for comp in door.components}
    assert {"E-STOP", "STOP", "START"} <= legends
    _assert_clear(_boxes(session, door.id, legends=True))


@pytest.mark.confirm_generic_layout
def test_plc_motor_hoa_panel_and_door():
    session = Session.create("PLC HOA")
    result = session.insert_circuit(kind="plc_motor", control="local", hoa=True, hp=5, voltage=480)
    panel = _panel(session, result)
    ids = {comp.id for comp in panel.components}
    roles = _by_role(result)
    assert {roles["plc"], roles["relay"], roles["psu"], roles["contactor"], roles["overload"]} <= ids
    door = session.project.sheet(result["layout"]["door"])
    hoa = next(comp for comp in door.components if comp.properties.get("role") == "hoa")
    assert hoa.properties.get("legend") == "HAND-OFF-AUTO"
    assert {comp.properties.get("legend") for comp in door.components} >= {"E-STOP", "STOP", "START", "HAND-OFF-AUTO"}
    _assert_clear(_boxes(session, panel.id))
    _assert_clear(_boxes(session, door.id, legends=True))


@pytest.mark.confirm_generic_layout
def test_reversing_pair_has_generic_interlock_between_contactors():
    session = Session.create("Reversing pair")
    result = session.insert_circuit(kind="reversing", control="local", hp=10, voltage=480)
    panel = _panel(session, result)
    fwd = panel.component("M1")
    rev = panel.component("M2")
    interlock = next(comp for comp in panel.components if comp.library_id.endswith("contactors.mech_interlock"))
    assert fwd.x < interlock.x < rev.x
    assert abs(fwd.y - rev.y) < 2.0
    assert interlock.properties.get("part_number", "") == ""
    assert interlock.properties.get("catalog", "") == ""
    _assert_clear(_boxes(session, panel.id))


@pytest.mark.confirm_generic_layout
def test_second_circuit_does_not_overlap_the_first():
    session = _iec("Two circuits")
    first = session.insert_circuit(kind="reversing", control="local", kw=4)
    second = session.insert_circuit(kind="soft_starter", control="local", kw=5.5, bypass=True)
    panel = _panel(session, second)
    assert panel.id == first["layout"]["sheet"]
    ids = {comp.id for comp in panel.components}
    assert _by_role(first)["fwd"] in ids
    assert _by_role(second)["softstarter"] in ids
    _assert_clear(_boxes(session, panel.id))
    door = session.project.sheet(second["layout"]["door"])
    _assert_clear(_boxes(session, door.id, legends=True))
    rows = grouped_rows(session.project, session.catalog)
    kinds = {}
    for row in rows:
        kinds.setdefault(row["library_id"], 0)
        kinds[row["library_id"]] += int(row["qty"])
    assert kinds.get("iec.contactors.mech_interlock", 0) == 1
    assert kinds.get("iec.drives.softstarter", 0) + kinds.get("iec.contactors.ac3_9a", 0) >= 1
    assert all(not row["part_number"] for row in rows if row["library_id"].endswith("mech_interlock"))
    assert all(not row["part_number"] for row in rows if "end_stop" in row["library_id"])


@pytest.mark.confirm_generic_layout
def test_bom_counts_placed_devices_and_generic_accessories():
    session = _iec("BOM counts")
    result = session.insert_circuit(kind="star_delta", control="local", pilot_lights=True)
    panel_refs = {comp.id for comp in _panel(session, result).components}
    bom_refs = {row["ref"] for row in instance_rows(session.project, session.catalog)}
    assert panel_refs <= bom_refs
    qty = {}
    for row in grouped_rows(session.project, session.catalog):
        qty[row["library_id"]] = qty.get(row["library_id"], 0) + int(row["qty"])
    assert qty["iec.contactors.ac3_9a"] >= 3
    # Schematic aux and overload contacts roll up to the parent. They are not parts.
    assert "iec.contactors.aux_no" not in qty
    assert "iec.contactors.aux_nc" not in qty
    assert "iec.protection.ol_contact" not in qty
    assert qty["iec.contactors.mech_interlock"] == 1
    assert qty["iec.accessories.end_stop"] == 2
    assert qty["iec.terminals.x_2p5"] == 3
    assert qty["iec.terminals.pe_4"] >= 1


@pytest.mark.confirm_generic_layout
def test_place_layout_false_skips_panel_and_jsonl_still_places():
    session = _iec("Schematic only")
    result = session.insert_circuit(kind="reversing", control="local", place_layout=False)
    assert result["layout"]["placed"] is False
    assert result["layout"]["sheet"] is None
    assert not any(sheet.kind == "panel_layout" for sheet in session.project.sheets)
    assert session.sheet(result["sheet"]).components

    from panelcad.cli.agent import Agent

    agent = Agent()
    created = agent.handle({"op": "create_project", "name": "JSONL layout"})
    assert created["ok"] is True
    inserted = agent.handle(
        {"op": "insert_circuit", "kind": "plc_motor", "control": "local", "hoa": True, "place_layout": True}
    )
    assert inserted["ok"] is True, inserted
    project = agent.session.project
    assert any(sheet.kind == "panel_layout" and sheet.components for sheet in project.sheets)
    assert any(sheet.kind == "door_layout" and sheet.components for sheet in project.sheets)
    skipped = agent.handle({"op": "insert_circuit", "kind": "dol", "place_layout": False, "new_sheet": True})
    assert skipped["ok"] is True, skipped
    assert skipped["result"]["layout"]["placed"] is False


def test_dialog_values_place_on_the_panel():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.circuit_dialog import CircuitBuilderDialog

    app = QApplication.instance() or QApplication([])
    dialog = CircuitBuilderDialog()
    dialog.kind.setCurrentIndex(dialog.kind.findData("soft_starter"))
    assert dialog.values()["place_layout"] is True
    session = _iec("Dialog place")
    result = session.insert_circuit(**dialog.values())
    assert result["layout"]["placed"] is True
    assert _panel(session, result).components
    dialog.place_layout.setChecked(False)
    bare = _iec("Dialog skip")
    skipped = bare.insert_circuit(**dialog.values())
    assert skipped["layout"]["placed"] is False
    dialog.close()
    app.processEvents()


@pytest.mark.confirm_generic_layout
def test_catalog_footprint_and_only_real_accessory_skus():
    session = _iec("Catalog footprint")
    result = session.insert_circuit(kind="dol")
    session.assign_part("Q1", "1489-M3C160", "Allen-Bradley")
    panel = _panel(session, result).component("Q1")
    device = session.catalog.get(panel.library_id)
    width, height = footprint_size(device, panel)
    assert width == pytest.approx(52.5)
    assert height == pytest.approx(111)

    catalog = PartsCatalog()
    real = CatalogPart(
        manufacturer="Allen-Bradley",
        catalog="100-FA20",
        library_id="iec.contactors.aux_no",
        category="switching.aux",
        verified="full",
        source="https://example.test/100-FA20",
    )
    parent = CatalogPart(
        manufacturer="Allen-Bradley",
        catalog="100-C09D10",
        library_id="iec.contactors.ac3_9a",
        category="switching.contactor",
        verified="full",
        source="https://example.test/100-C09",
        accessories=["100-FA20", "NOT-A-REAL-SKU"],
    )
    catalog.parts[real.key()] = real
    catalog.parts[parent.key()] = parent
    resolved = catalog.resolved_accessories(parent)
    assert [part.catalog for part in resolved] == ["100-FA20"]


@pytest.mark.confirm_generic_layout
def test_apply_enclosure_keeps_groups_legends_and_clearance():
    session = _iec("Resize")
    result = session.insert_circuit(kind="star_delta", control="local")
    rec = session.recommend_enclosure()
    session.apply_enclosure(place_parts=True, recommendation=rec)
    panel = _panel(session, result)
    _assert_clear(_boxes(session, panel.id))
    door = session.project.sheet(result["layout"]["door"])
    assert any(comp.properties.get("legend") == "E-STOP" for comp in door.components)
    bands = {comp.properties.get("layout_band") for comp in panel.components}
    assert "power" in bands
    assert "terminal" in bands
    power_bottom = max(comp.y for comp in panel.components if comp.properties.get("layout_band") == "power")
    terminal_top = min(comp.y for comp in panel.components if comp.properties.get("layout_band") == "terminal")
    assert terminal_top > power_bottom


_CHILD_TAG = __import__("re").compile(r"^[A-Za-z]+\d+[-.]\d+$")


def _contact_refs(session: Session) -> list[str]:
    """Schematic contact tags that must not become panel modules or BOM lines."""

    found = []
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if not _CHILD_TAG.match(comp.id):
                continue
            lid = comp.library_id.lower()
            if "ol_contact" in lid or lid.endswith("aux_no") or lid.endswith("aux_nc"):
                if comp.properties.get("addon"):
                    continue
                found.append(comp.id)
    return found


def _assert_contacts_rollup(session: Session) -> None:
    banned = set(_contact_refs(session))
    assert banned, "expected schematic contact tags such as K1-13 or F1-95"
    for sheet in session.project.sheets:
        if sheet.kind != "panel_layout":
            continue
        assert not banned & {comp.id for comp in sheet.components}
    descriptions = []
    for row in instance_rows(session.project, session.catalog):
        assert row["ref"] not in banned
        descriptions.append(row["description"].lower())
    blob = "\n".join(descriptions)
    assert "auxiliary contact" not in blob
    assert "overload contact" not in blob


@pytest.mark.confirm_generic_layout
def test_child_contacts_have_no_footprint_and_no_bom_line():
    session = _iec("Rollup")
    result = session.insert_circuit(kind="star_delta", control="local", pilot_lights=True)
    _assert_contacts_rollup(session)
    rec = session.recommend_enclosure()
    session.apply_enclosure(place_parts=True, recommendation=rec)
    _assert_contacts_rollup(session)
    panel = _panel(session, result)
    roles = _by_role(result)
    overload = panel.component(roles["overload"])
    star = panel.component(roles["star"])
    from panelcad.core.layout import clip_offset, nearest_rail

    def _rail(comp):
        device = session.catalog.get(comp.library_id)
        return nearest_rail(panel, comp.y + clip_offset(device, comp))

    overload_rail = _rail(overload)
    star_rail = _rail(star)
    assert overload_rail is not None and star_rail is not None
    if overload_rail.id == star_rail.id:
        assert overload.x > star.x
    else:
        assert overload_rail.y > star_rail.y


@pytest.mark.confirm_generic_layout
def test_each_overload_stays_with_its_contactor():
    session = _iec("Two motors")
    first = session.insert_circuit(kind="reversing", control="local", kw=4)
    second = session.insert_circuit(kind="dol", control="local", kw=1.5)
    panel = _panel(session, second)
    from panelcad.core.layout import clip_offset, nearest_rail

    def _rail(comp):
        device = session.catalog.get(comp.library_id)
        return nearest_rail(panel, comp.y + clip_offset(device, comp))

    def _with_contactor(result: dict, role: str) -> None:
        overload = panel.component(_by_role(result)["overload"])
        contactor = panel.component(_by_role(result)[role])
        overload_rail = _rail(overload)
        contactor_rail = _rail(contactor)
        assert overload_rail is not None and contactor_rail is not None
        if overload_rail.id == contactor_rail.id:
            assert overload.x > contactor.x
        else:
            assert overload_rail.y > contactor_rail.y

    _with_contactor(first, "fwd")
    _with_contactor(second, "contactor")
    _assert_contacts_rollup(session)


@pytest.mark.confirm_generic_layout
def test_second_insert_reuses_supply_and_estop():
    session = _iec("Share")
    first = session.insert_circuit(kind="reversing", control="local", kw=4)
    second = session.insert_circuit(kind="soft_starter", control="local", kw=5.5)
    assert second["shared_supply"] == _by_role(first)["psu"]
    assert second["shared_estop"] == _by_role(first)["estop"]

    def _ids(role: str) -> set[str]:
        found = set()
        for sheet in session.project.sheets:
            if sheet.kind not in {"schematic", "io"}:
                continue
            for comp in sheet.components:
                if str(comp.properties.get("role") or "") == role:
                    found.add(comp.id)
        return found

    assert _ids("psu") == {_by_role(first)["psu"]}
    assert _ids("estop") == {_by_role(first)["estop"]}
    panel = _panel(session, second)
    assert _by_role(first)["psu"] in {comp.id for comp in panel.components}
    assert sum(1 for comp in panel.components if comp.properties.get("role") == "psu") == 1


def test_shared_flags_off_add_a_supply_and_estop():
    session = _iec("Separate")
    session.insert_circuit(kind="dol", control="local")
    second = session.insert_circuit(kind="dol", control="local", shared_supply=False, shared_estop=False)
    assert "shared_supply" not in second
    psu = set()
    estop = set()
    for sheet in session.project.sheets:
        if sheet.kind not in {"schematic", "io"}:
            continue
        for comp in sheet.components:
            role = str(comp.properties.get("role") or "")
            if role == "psu":
                psu.add(comp.id)
            if role == "estop":
                estop.add(comp.id)
    assert len(psu) == 2
    assert len(estop) == 2


def test_jsonl_and_cli_share_the_same_supply(tmp_path):
    from typer.testing import CliRunner

    from panelcad.cli.agent import Agent
    from panelcad.cli.main import app

    agent = Agent()
    assert agent.handle({"op": "create_project", "name": "JSONL share"})["ok"]
    agent.session.set_title_block(standard="IEC")
    first = agent.handle({"op": "insert_circuit", "kind": "dol", "control": "local"})
    second = agent.handle({"op": "insert_circuit", "kind": "dol", "control": "local"})
    assert first["ok"] and second["ok"], (first, second)
    assert second["result"]["shared_supply"]
    assert second["result"]["shared_estop"]
    own = agent.handle(
        {
            "op": "insert_circuit",
            "kind": "dol",
            "control": "local",
            "shared_supply": False,
            "shared_estop": False,
            "new_sheet": True,
        }
    )
    assert own["ok"], own
    assert "shared_supply" not in own["result"]

    from panelcad.ai.drawing import insert_circuit
    from panelcad.core.circuit_builder import INSERT_CIRCUIT_SCHEMA, supply_load_warning

    assert "shared_supply" in INSERT_CIRCUIT_SCHEMA["args"]
    assert "shared_estop" in INSERT_CIRCUIT_SCHEMA["args"]
    assert "shared_supply" in insert_circuit.__annotations__ or "shared_supply" in insert_circuit.__code__.co_varnames
    assert supply_load_warning(120, 10, "G1") is None
    warning = supply_load_warning(120, 14, "G1")
    assert warning and "80%" in warning and "estimate" in warning

    path = tmp_path / "share.pcad.yaml"
    job = Session.create("CLI share")
    job.set_title_block(standard="IEC")
    job.save(path)
    runner = CliRunner()
    one = runner.invoke(app, ["electrical", "circuit", str(path), "--kind", "dol", "--control", "local"])
    two = runner.invoke(app, ["electrical", "circuit", str(path), "--kind", "dol", "--control", "local"])
    assert one.exit_code == 0, one.output
    assert two.exit_code == 0, two.output
    opened = Session.open(path)
    supplies = [
        comp.id
        for sheet in opened.project.sheets
        if sheet.kind in {"schematic", "io"}
        for comp in sheet.components
        if comp.properties.get("role") == "psu"
    ]
    assert len(set(supplies)) == 1
    three = runner.invoke(
        app,
        [
            "electrical",
            "circuit",
            str(path),
            "--kind",
            "dol",
            "--control",
            "local",
            "--no-shared-supply",
            "--no-shared-estop",
            "--new-sheet",
        ],
    )
    assert three.exit_code == 0, three.output
    opened = Session.open(path)
    supplies = {
        comp.id
        for sheet in opened.project.sheets
        if sheet.kind in {"schematic", "io"}
        for comp in sheet.components
        if comp.properties.get("role") == "psu"
    }
    assert len(supplies) == 2


def _door_ink(session: Session, door) -> list[tuple[float, float, float, float]]:
    ink = []
    for comp in door.components:
        device = session.catalog.get(comp.library_id)
        width, height = footprint_size(device, comp)
        ink.append((comp.x, comp.y, width, height))
        legend = str(comp.properties.get("legend") or "").strip()
        if legend:
            ink.append(legend_plate_rect(comp.x, comp.y, width, height, legend))
        heading = str(comp.properties.get("layout_heading") or "").strip()
        if heading:
            ink.append(heading_rect(comp.x, comp.y, width, height, heading))
    return ink


@pytest.mark.confirm_generic_layout
def test_door_grid_stays_inside_the_outline():
    from panelcad.core.layout import enclosure_drawn_size

    session = _iec("Door grid")
    first = session.insert_circuit(kind="star_delta", control="local", pilot_lights=True)
    second = session.insert_circuit(kind="dol", control="local", pilot_lights=True)
    rec = session.recommend_enclosure()
    assert any("Heat is reported and does not change the catalog box." in line for line in rec["reasons"])
    assert any("Target spare" in line for line in rec["reasons"])
    session.apply_enclosure(place_parts=True, recommendation=rec)
    door = session.project.sheet(first["layout"]["door"])
    legends = {comp.properties.get("legend") for comp in door.components}
    assert {"E-STOP", "STOP", "START"} <= legends
    assert any(str(comp.properties.get("role") or "").startswith("pilot") for comp in door.components)
    headings = [comp.properties.get("layout_heading") for comp in door.components]
    assert sum(1 for heading in headings if str(heading or "").startswith("Motor ")) == 2
    assert sum(1 for comp in door.components if comp.properties.get("role") == "estop") == 1
    _assert_clear(_boxes(session, door.id, legends=True))
    assert door.enclosure is not None
    drawn_w, drawn_h, scale = enclosure_drawn_size(door.enclosure)
    left, top = door.enclosure.x, door.enclosure.y
    for x, y, width, height in _door_ink(session, door):
        assert x >= left - 0.8 and y >= top - 0.8
        assert x + width <= left + drawn_w + 0.8
        assert y + height <= top + drawn_h + 0.8
    rows: dict[tuple, list] = {}
    for comp in door.components:
        if comp.properties.get("role") == "estop":
            continue
        device = session.catalog.get(comp.library_id)
        width, _height = footprint_size(device, comp)
        if width / scale > 30.0:
            continue
        key = (str(comp.properties.get("layout_group") or ""), round(comp.y, 0))
        rows.setdefault(key, []).append(comp)
    checked = 0
    for members in rows.values():
        members.sort(key=lambda comp: comp.x)
        for left_op, right_op in zip(members, members[1:], strict=False):
            pitch = (right_op.x - left_op.x) / scale
            legends = " ".join(str(comp.properties.get("legend") or "") for comp in members)
            limit = 100.0 if len(legends) >= 12 else 76.0
            assert 50.0 <= pitch <= limit, (pitch, legends)
            checked += 1
    assert checked >= 2
    for comp in door.components:
        heading = str(comp.properties.get("layout_heading") or "").strip()
        if not heading:
            continue
        device = session.catalog.get(comp.library_id)
        width, height = footprint_size(device, comp)
        hx, hy, hw, hh = heading_rect(comp.x, comp.y, width, height, heading)
        assert not rects_overlap(hx, hy, hw, hh, comp.x, comp.y, width, height), comp.id
        assert comp.y - (hy + hh) >= max(1.0, height * 0.08), comp.id


@pytest.mark.confirm_generic_layout
def test_catalog_aux_mounts_beside_its_contactor_and_is_a_bom_line():
    session = _iec("Addon")
    result = session.insert_circuit(kind="dol", control="local")
    real = CatalogPart(
        manufacturer="Allen-Bradley",
        catalog="100-FA20",
        library_id="iec.contactors.aux_no",
        category="switching.aux",
        verified="full",
        source="https://example.test/100-FA20",
        description="Auxiliary contact block",
    )
    parent = CatalogPart(
        manufacturer="Allen-Bradley",
        catalog="100-C09D10",
        library_id="iec.contactors.ac3_9a",
        category="switching.contactor",
        verified="full",
        source="https://example.test/100-C09",
        accessories=["100-FA20"],
    )
    session.parts.parts[real.key()] = real
    session.parts.parts[parent.key()] = parent
    assigned = session.assign_part("K1", "100-C09D10", "Allen-Bradley")
    assert assigned["mounted_aux"]
    panel = _panel(session, result)
    block = next(comp for comp in panel.components if comp.properties.get("addon"))
    host = panel.component("K1")
    device = session.catalog.get(host.library_id)
    host_w, _host_h = footprint_size(device, host)
    assert block.properties.get("parent") == "K1"
    assert block.x >= host.x + host_w - 0.5
    neighbour = panel.component("F1")
    assert neighbour.x >= block.x
    rows = instance_rows(session.project, session.catalog)
    assert any(row["ref"] == block.id and row["catalog"] == "100-FA20" for row in rows)
    assert all(row["ref"] != "K1-13" for row in rows)
    _assert_clear(_boxes(session, panel.id))


def test_terminal_numbers_are_rotated(tmp_path):
    from panelcad.core.layout import footprint_size, rects_overlap, tray_aabb
    from panelcad.render.layout import plan_terminal_labels

    session = _iec("Terminal labels")
    session.insert_circuit(kind="reversing", control="local", kw=4)
    result = session.insert_circuit(kind="soft_starter", control="local", kw=5.5)
    rec = session.recommend_enclosure()
    session.apply_enclosure(place_parts=True, recommendation=rec)
    panel = _panel(session, result)
    terminals = [
        comp
        for comp in panel.components
        if "terminals" in comp.library_id or comp.library_id.endswith("end_stop")
    ]
    assert len(terminals) >= 4
    shop = [comp for comp in terminals if not str(comp.id).startswith("ES-")]
    furniture = [comp for comp in terminals if str(comp.id).startswith("ES-")]
    labels = plan_terminal_labels(panel, session.catalog)
    assert set(labels) >= {comp.id for comp in shop}
    assert not any(str(comp_id).startswith("ES-") for comp_id in labels)
    from panelcad.core.models import Component

    panel.components.append(
        Component(
            id="ES-r-din-1-R",
            library_id="iec.accessories.end_stop",
            x=10.0,
            y=10.0,
            scale=0.5,
            properties={"accessory": "end_stop", "addon": True, "layout_furniture": True},
        )
    )
    assert "ES-r-din-1-R" not in plan_terminal_labels(panel, session.catalog)
    rects = []
    above = below = 0
    for comp in shop:
        label = labels[comp.id]
        device = session.catalog.get(comp.library_id)
        _w, height = footprint_size(device, comp)
        body_top = comp.y
        body_bot = comp.y + height
        assert label.rotate == -90.0
        if label.rect[1] + label.rect[3] <= body_top + 0.2:
            above += 1
        elif label.rect[1] >= body_bot - 0.2:
            below += 1
        else:
            raise AssertionError(f"{comp.id} label intersects the terminal body")
        assert label.size >= 1.8
        rects.append(label.rect)
    assert above >= 2 and below >= 2
    for index, left in enumerate(rects):
        for right in rects[index + 1 :]:
            assert not rects_overlap(*left, *right)
    for tray in panel.trays:
        duct = tray_aabb(tray)
        for rect in rects:
            assert not rects_overlap(*rect, *duct), tray.id
    from panelcad.core.layout import rail_height
    from panelcad.render.layout import _rail_label_x, caption_width

    scale = float(panel.enclosure.draw_scale or 1.0)
    for rail in panel.rails:
        if "terminal" not in rail.id:
            continue
        true_len = rail.length_mm / scale
        caption = f"{rail.id}  TS35  {true_len:.0f} mm"
        x = _rail_label_x(rail, caption, 2.6, rects)
        half = rail_height(rail) / 2.0
        baseline = rail.y - half - 1.5
        caption_rect = (x, baseline - 2.6 * 0.92, caption_width(caption, 2.6), 2.6 * 1.2)
        for rect in rects:
            assert not rects_overlap(*caption_rect, *rect), rail.id
    dest = tmp_path / "panel.svg"
    session.export_svg(dest, sheet_id=panel.id)
    svg = dest.read_text(encoding="utf-8")
    assert "rotate(-90" in svg
    assert shop[0].id in svg
    assert all(comp.id not in svg for comp in furniture)


@pytest.mark.confirm_generic_layout
def test_control_shares_the_power_rail_unless_asked():
    from panelcad.core.layout import clip_offset, nearest_rail

    session = _iec("Shared rail")
    result = session.insert_circuit(kind="star_delta", control="local", kw=7.5)
    assert session.project.properties["enclosure_sizing"]["separate_control_rail"] is False
    rec = session.recommend_enclosure()
    session.apply_enclosure(place_parts=True, recommendation=rec)
    panel = _panel(session, result)
    roles = _by_role(result)

    def _rail(ref: str):
        comp = panel.component(ref)
        device = session.catalog.get(comp.library_id)
        return nearest_rail(panel, comp.y + clip_offset(device, comp))

    power = _rail(roles["main"])
    timer = _rail(roles["timer"])
    supply = _rail(roles["psu"])
    assert power is not None and timer is not None and supply is not None
    assert timer.id == power.id
    assert supply.id == power.id
    scale = float(panel.enclosure.draw_scale or 1.0)
    power_right = 0.0
    for ref, comp in ((roles["main"], panel.component(roles["main"])),):
        device = session.catalog.get(comp.library_id)
        width, _height = footprint_size(device, comp)
        power_right = max(power_right, comp.x + width)
    for ref in (roles["breaker"], roles["delta"], roles["star"], roles["overload"]):
        comp = panel.component(ref)
        device = session.catalog.get(comp.library_id)
        width, _height = footprint_size(device, comp)
        if _rail(ref) is not None and _rail(ref).id == power.id:
            power_right = max(power_right, comp.x + width)
    control_left = min(panel.component(roles["timer"]).x, panel.component(roles["psu"]).x)
    assert (control_left - power_right) / scale >= 19.0
    split = session.recommend_enclosure(separate_control_rail=True)
    timer_y = None
    power_y = None
    for item in split["arrangement"]["items"]:
        if item.get("ref") == roles["timer"]:
            timer_y = item["y"]
        if item.get("ref") == roles["main"]:
            power_y = item["y"]
    assert timer_y is not None and power_y is not None
    assert timer_y > power_y + 10.0


@pytest.mark.confirm_generic_layout
def test_soft_starter_sits_on_the_row_that_fits():
    """U1 sits beside a rail when width remains; otherwise it wraps onto the next row."""

    session = _iec("Starter beside")
    session.insert_circuit(kind="reversing", control="local", kw=4)
    result = session.insert_circuit(kind="soft_starter", control="local", kw=5.5, bypass=True)
    roles = _by_role(result)
    rec = session.recommend_enclosure()
    items = rec["arrangement"]["items"]
    starter = next(item for item in items if item.get("ref") == roles["softstarter"])
    din = [item for item in items if item.get("kind") == "device" and item.get("role") == "din"]
    box = rec["enclosure"]
    assert rec["fit"] is True
    assert box["id"] == "wall-30x24x10-n12"
    assert box["height_in"] == 30
    assert box["width_in"] == 24
    assert box["depth_in"] == 10
    assert box["panel_width_mm"] == pytest.approx(533.4, abs=0.2)
    assert box["panel_height_mm"] == pytest.approx(685.8, abs=0.2)
    assert rec["needed"]["packed_width_mm"] == pytest.approx(490.0, abs=0.5)
    assert rec["needed"]["packed_height_mm"] == pytest.approx(678.0, abs=0.5)
    assert box["panel_width_mm"] + 1e-6 >= rec["needed"]["packed_width_mm"]
    assert box["panel_height_mm"] + 1e-6 >= rec["needed"]["packed_height_mm"]
    # 363 mm of DIN plus the airflow gap and the 80 mm starter is wider than the
    # 30×24 usable width, so U1 is on the next row rather than a 36-wide box.
    assert starter["y"] > max(item["y"] for item in din) + 20.0


def test_soft_starter_motor_records_internal_overload():
    from panelcad.core.checks import run_checks

    session = _iec("Soft starter OL")
    session.insert_circuit(kind="reversing", control="local", kw=4)
    result = session.insert_circuit(kind="soft_starter", control="local", kw=5.5, bypass=True)
    roles = _by_role(result)
    motor = None
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if comp.id == roles["motor"]:
                motor = comp
    assert motor is not None
    assert "overload: soft starter internal" in str(motor.properties.get("description") or "")
    bom = [row for row in grouped_rows(session.project, session.catalog) if motor.id in row["ref"].split(", ")]
    assert len(bom) == 1
    assert bom[0]["ref"] == motor.id
    assert "overload: soft starter internal" in bom[0]["description"]
    warnings = [item for item in run_checks(session.project, session.catalog) if item.check == "soft_starter_overload"]
    assert not warnings
    motor.properties["description"] = "Motor"
    warnings = [item for item in run_checks(session.project, session.catalog) if item.check == "soft_starter_overload"]
    assert any(item.ref == motor.id for item in warnings)


def test_allen_bradley_catalog_drives_the_bom_and_the_footprint():
    session = _iec("AB DOL")
    result = session.insert_circuit(kind="dol", control="local", kw=1.5)
    roles = _by_role(result)
    session.assign_part(roles["breaker"], "1489-M3C160", "Allen-Bradley")
    session.assign_part(roles["contactor"], "100-C09D10", "Allen-Bradley")
    session.assign_part(roles["overload"], "193-T1AB63", "Allen-Bradley")
    session.assign_part(roles["psu"], "1606-XLE120E", "Allen-Bradley")
    rec = session.recommend_enclosure()
    session.apply_enclosure(place_parts=True, recommendation=rec)
    rows = {row["ref"]: row for row in instance_rows(session.project, session.catalog)}
    assert rows[roles["contactor"]]["manufacturer"] == "Allen-Bradley"
    assert rows[roles["contactor"]]["part_number"] == "100-C09D10"
    assert rows[roles["breaker"]]["part_number"] == "1489-M3C160"
    assert rows[roles["psu"]]["part_number"] == "1606-XLE120E"
    assert rows[roles["overload"]]["part_number"] == "193-T1AB63"
    panel = _panel(session, result)
    contactor = panel.component(roles["contactor"])
    device = session.catalog.get(contactor.library_id)
    width, height = footprint_size(device, contactor)
    assert width == pytest.approx(45.0 * contactor.scale, rel=0.02)
    assert height == pytest.approx(81.0 * contactor.scale, rel=0.02)
    supply = panel.component(roles["psu"])
    sw, sh = footprint_size(session.catalog.get(supply.library_id), supply)
    assert sw == pytest.approx(32.0 * supply.scale, rel=0.02)
    assert sh == pytest.approx(124.0 * supply.scale, rel=0.02)


def _sizing_options(session: Session):
    from panelcad.core.enclosure import options_from_defaults
    from panelcad.library.enclosures import load_enclosures

    library = load_enclosures()
    merged = dict(library.defaults)
    stored = session.project.properties.get("enclosure_sizing") or {}
    if isinstance(stored, dict):
        merged.update(stored)
    return options_from_defaults(merged), library


def _pack_on_box(session: Session, box_id: str):
    """Re-pack this job to one catalog box and say whether its panel holds the pack."""

    from panelcad.core.enclosure import (
        arrange_backpanel,
        arrange_door,
        collect_parts,
        usable_inner_width,
    )

    opts, library = _sizing_options(session)
    box = next(item for item in library.boxes if item.id == box_id)
    cabinet, door = collect_parts(session.project, session.catalog)
    _door_items, door_w, door_h, door_depth = arrange_door(door, opts)
    items, trial_w, trial_h, trial_d = arrange_backpanel(
        cabinet, opts, inner_w=usable_inner_width(box, opts)
    )
    packed_w = max(trial_w, max((item.x + item.w for item in items), default=trial_w))
    bottoms = max((item.y + item.h for item in items), default=trial_h)
    packed_h = max(trial_h, bottoms + opts.wall_gutter_mm)
    need_w = max(packed_w, door_w)
    need_h = max(packed_h, door_h)
    need_d = max(trial_d, opts.duct_depth_mm) + door_depth + opts.rail_to_duct_mm
    holds = (
        box.panel_width_mm + 1e-6 >= need_w
        and box.panel_height_mm + 1e-6 >= need_h
        and box.depth_mm + 1e-6 >= need_d
    )
    return holds, packed_w, packed_h, box, items


@pytest.mark.confirm_generic_layout
def test_star_delta_uses_24x20_when_the_wrapped_pack_fits_its_height():
    """Wrap to the 24×20 usable width. Take that box only when its panel height holds the pack."""

    session = _iec("Star-delta wrap")
    result = session.insert_circuit(kind="star_delta", control="local", kw=7.5, pilot_lights=True)
    roles = _by_role(result)
    rec = session.recommend_enclosure()
    holds, packed_w, packed_h, small, packed_items = _pack_on_box(session, "wall-24x20x8-n12")
    box = rec["enclosure"]
    assert box["panel_width_mm"] + 1e-6 >= rec["needed"]["packed_width_mm"]
    assert box["panel_height_mm"] + 1e-6 >= rec["needed"]["packed_height_mm"]
    if holds:
        assert box["height_in"] == 24
        assert box["width_in"] == 20
    else:
        # Hoffman 24×20 panel is 431.8 mm wide, so the 458.5 mm row wraps and
        # the wrapped height misses that panel. AutomationDirect N412242008C is
        # the same nominal 24×20 with a 462 mm panel, so the row stays one high.
        assert packed_w <= small.panel_width_mm + 1e-6
        assert packed_h > small.panel_height_mm
        assert box["id"] == "ad-n412242008c"
        assert box["height_in"] == 24
        assert box["width_in"] == 20
        assert rec["needed"]["packed_width_mm"] == pytest.approx(458.5, abs=0.5)
        assert rec["needed"]["packed_height_mm"] == pytest.approx(416.4, abs=0.5)
        assert box["panel_width_mm"] == pytest.approx(462.28, abs=0.2)
        assert box["panel_height_mm"] == pytest.approx(563.88, abs=0.2)
    # Generic DIN overload stays on the rail beside the contactors.
    chosen = {item["ref"]: item for item in rec["arrangement"]["items"]}
    overload = chosen[roles["overload"]]
    main = chosen[roles["main"]]
    assert abs(overload["y"] - main["y"]) < 1.0
    assert overload["x"] > main["x"]
    # The wrapped 24×20 pack keeps the overload on the contactor row.
    wrapped = {item.ref: item for item in packed_items}
    cluster = [
        wrapped[roles["main"]],
        wrapped[roles["delta"]],
        wrapped[roles["star"]],
        wrapped[roles["overload"]],
    ]
    assert max(item.y for item in cluster) - min(item.y for item in cluster) < 1.0


@pytest.mark.confirm_generic_layout
def test_plc_hoa_wraps_before_it_takes_a_wider_box():
    session = _iec("PLC wrap")
    session.set_title_block(standard="NEMA/ANSI")
    session.insert_circuit(kind="plc_motor", control="local", hp=5, voltage=480)
    rec = session.recommend_enclosure()
    holds, packed_w, packed_h, small, _items = _pack_on_box(session, "wall-24x20x8-n12")
    box = rec["enclosure"]
    assert rec["fit"] is True
    if holds:
        assert box["height_in"] == 24
        assert box["width_in"] == 20
    else:
        assert packed_w <= small.panel_width_mm + 1e-6
        assert packed_h > small.panel_height_mm
        assert box["id"] == "ad-n412242008c"
        assert box["height_in"] == 24
        assert box["width_in"] == 20
        assert rec["needed"]["packed_width_mm"] == pytest.approx(433.0, abs=0.5)
        assert rec["needed"]["packed_height_mm"] == pytest.approx(416.4, abs=0.5)
        assert box["panel_width_mm"] == pytest.approx(462.28, abs=0.2)
        assert box["panel_height_mm"] == pytest.approx(563.88, abs=0.2)


@pytest.mark.confirm_generic_layout
def test_motor_cluster_stays_together_when_the_rail_wraps():
    session = _iec("Cluster wrap")
    result = session.insert_circuit(kind="star_delta", control="local", kw=7.5)
    roles = _by_role(result)
    _holds, _pw, _ph, _box, items = _pack_on_box(session, "wall-20x16x8-n12")
    by_ref = {item.ref: item for item in items}
    cluster_refs = [roles["main"], roles["delta"], roles["star"], roles["overload"]]
    interlock = next(
        comp.id
        for sheet in session.project.sheets
        for comp in sheet.components
        if comp.properties.get("accessory") == "mechanical_interlock"
    )
    cluster_refs.append(interlock)
    ys = [by_ref[ref].y for ref in cluster_refs]
    assert max(ys) - min(ys) < 1.0
    breaker = by_ref[roles["breaker"]]
    assert abs(breaker.y - ys[0]) > 10.0
    timer = by_ref[roles["timer"]]
    supply = by_ref[roles["psu"]]
    assert timer.y > max(ys) + 10.0
    assert supply.y > max(ys) + 10.0
    assert abs(timer.y - supply.y) < 1.0


@pytest.mark.confirm_generic_layout
def test_plug_on_overload_stacks_under_its_contactor():
    session = _iec("Plug-on")
    result = session.insert_circuit(kind="dol", control="local", kw=1.5)
    roles = _by_role(result)
    session.assign_part(roles["breaker"], "1489-M3C160", "Allen-Bradley")
    session.assign_part(roles["contactor"], "100-C09D10", "Allen-Bradley")
    session.assign_part(roles["overload"], "193-T1AB63", "Allen-Bradley")
    session.assign_part(roles["psu"], "1606-XLE120E", "Allen-Bradley")
    # 193-T1AB63 has no published body. Confirm the shop envelope before sizing.
    session.confirm_envelope(
        roles["overload"],
        width_mm=45,
        height_mm=70,
        depth_mm=80,
        mount="din_rail",
        confirmed_by="test",
    )
    rec = session.recommend_enclosure()
    items = {item["ref"]: item for item in rec["arrangement"]["items"]}
    contactor = items[roles["contactor"]]
    overload = items[roles["overload"]]
    supply = items[roles["psu"]]
    assert overload["x"] == pytest.approx(contactor["x"], abs=0.2)
    assert overload["y"] == pytest.approx(contactor["y"] + contactor["h"], abs=0.2)
    assert overload["w"] == pytest.approx(contactor["w"], abs=0.2)
    assert overload["h"] == pytest.approx(70.0, abs=0.2)
    part = next(row for row in rec["parts"] if row["ref"] == roles["overload"])
    assert part["dim_source"] == "measured"
    assert part["height_mm"] == pytest.approx(70.0, abs=0.2)
    rail = next(
        item
        for item in rec["arrangement"]["items"]
        if item["kind"] == "rail" and item["role"] == "din"
    )
    assert rail["w"] == pytest.approx(150.5, abs=0.4)
    assert rail["x"] + rail["w"] == pytest.approx(supply["x"] + supply["w"], abs=0.4)
    assert overload["y"] > rail["y"] + rail["h"]
    assert supply["x"] - (contactor["x"] + contactor["w"]) == pytest.approx(20.0, abs=0.4)
    box = rec["enclosure"]
    assert box["id"] == "wall-20x16x8-n12"
    assert rec["needed"]["packed_width_mm"] == pytest.approx(277.5, abs=0.5)
    assert rec["needed"]["packed_height_mm"] == pytest.approx(328.8, abs=0.5)
    assert box["panel_width_mm"] == pytest.approx(330.2, abs=0.2)
    assert box["panel_height_mm"] == pytest.approx(431.8, abs=0.2)
    ducts = [item for item in rec["arrangement"]["items"] if item["kind"] == "duct"]
    for duct in ducts:
        assert not (
            overload["x"] + 0.05 < duct["x"] + duct["w"]
            and overload["x"] + overload["w"] > duct["x"] + 0.05
            and overload["y"] + 0.05 < duct["y"] + duct["h"]
            and overload["y"] + overload["h"] > duct["y"] + 0.05
        )


@pytest.mark.confirm_generic_layout
def test_catalog_says_mounts_on_stacks_the_overload():
    """E1 Plus stays DIN in the library, and the description says it mounts on the contactor."""

    session = _iec("Mounts on")
    result = session.insert_circuit(kind="dol", control="local", kw=1.5)
    roles = _by_role(result)
    session.assign_part(roles["contactor"], "100-C09D10", "Allen-Bradley")
    session.assign_part(roles["overload"], "193-EEDB", "Allen-Bradley")
    session.confirm_envelope(
        roles["overload"],
        width_mm=45,
        height_mm=70,
        depth_mm=80,
        mount="din_rail",
        confirmed_by="test",
    )
    overload = None
    for sheet in session.project.sheets:
        try:
            overload = sheet.component(roles["overload"])
            break
        except KeyError:
            continue
    assert overload is not None
    assert overload.properties.get("mounting") == "din"
    assert "mounts on" in str(overload.properties.get("description") or "").lower()
    rec = session.recommend_enclosure()
    items = {item["ref"]: item for item in rec["arrangement"]["items"]}
    contactor = items[roles["contactor"]]
    stacked = items[roles["overload"]]
    assert stacked["x"] == pytest.approx(contactor["x"], abs=0.2)
    assert stacked["y"] == pytest.approx(contactor["y"] + contactor["h"], abs=0.2)
    assert stacked["h"] == pytest.approx(70.0, abs=0.2)
    part = next(row for row in rec["parts"] if row["ref"] == roles["overload"])
    assert part["dim_source"] == "measured"


_CIRCUIT_KINDS = ("dol", "vfd", "reversing", "star_delta", "soft_starter", "plc_motor")


def _inside_frame(sheet, boxes: list[tuple]) -> None:
    from panelcad.core.layout import rail_aabb, rects_overlap, tray_aabb
    from panelcad.render.titleblock import title_block_rect, working_rect

    fx, fy, fw, fh = working_rect(sheet)
    tx, ty, tw, th = title_block_rect(sheet)

    def check(name: str, x: float, y: float, width: float, height: float) -> None:
        assert x >= fx - 0.6, name
        assert y >= fy - 0.6, name
        assert x + width <= fx + fw + 0.6, name
        assert y + height <= fy + fh + 0.6, name
        assert not rects_overlap(x, y, width, height, tx, ty, tw, th), name

    for name, x, y, width, height in boxes:
        check(name, x, y, width, height)
    for rail in sheet.rails:
        check(rail.id, *rail_aabb(rail))
    for tray in sheet.trays:
        check(tray.id, *tray_aabb(tray))


def _insert(session: Session, kind: str, **extra):
    kwargs = {"control": "local", "hoa": True, "kw": 5.5}
    if kind == "vfd":
        kwargs["drive"] = "generic_1k5"
    kwargs.update(extra)
    return session.insert_circuit(kind, **kwargs)


@pytest.mark.parametrize("kind", _CIRCUIT_KINDS)
def test_insert_stays_inside_the_drawing_frame(kind: str):
    session = _iec(kind)
    _insert(session, kind)
    for sheet in session.project.sheets:
        if sheet.kind not in {"panel_layout", "door_layout"}:
            continue
        _inside_frame(sheet, _boxes(session, sheet.id))
        _assert_clear(_boxes(session, sheet.id))


@pytest.mark.confirm_generic_layout
@pytest.mark.parametrize("kind", _CIRCUIT_KINDS)
def test_fresh_insert_adds_no_check_findings(kind: str):
    bare = _iec(kind + "-schematic")
    _insert(bare, kind, place_layout=False)
    base = {(row["check"], row["ref"]) for row in bare.design_checks()}
    laid = _iec(kind + "-layout")
    _insert(laid, kind, place_layout=True)
    if kind == "vfd":
        # Applying the box used to report a hairline footprint_overlap of the
        # end stop on Q1. The check tolerates that rounding.
        laid.apply_enclosure(place_parts=True)
    # A sized enclosure may report heat. Layout furniture must not add schematic or part-number findings.
    ignored = {"heat_rise_over_limit"}
    extra = [
        row
        for row in laid.design_checks()
        if row["check"] not in ignored and (row["check"], row["ref"]) not in base
    ]
    assert extra == [], [(row["check"], row["ref"], row["message"]) for row in extra]
    assert not any(row["check"] == "missing_schematic" for row in laid.design_checks())


def test_insert_circuit_kind_is_a_closed_list():
    from typing import Literal, get_args, get_origin, get_type_hints

    from panelcad.ai.drawing import insert_circuit

    ann = get_type_hints(insert_circuit, include_extras=True)["kind"]
    origin = get_origin(ann)
    args = get_args(ann)
    if origin is not Literal:
        inner = args[0]
        assert get_origin(inner) is Literal
        args = get_args(inner)
    assert set(args) == {"dol", "vfd", "reversing", "star_delta", "soft_starter", "plc_motor"}


def test_jsonl_insert_circuit_coerces_false_strings():
    from panelcad.cli.agent import Agent

    agent = Agent()
    created = agent.handle({"op": "create_project", "name": "Flags"})
    assert created["ok"], created
    result = agent.handle(
        {
            "op": "insert_circuit",
            "kind": "dol",
            "place_layout": "false",
            "shared_supply": "false",
            "shared_estop": "false",
            "separate_control_rail": "false",
        }
    )
    assert result["ok"], result
    assert not any(sheet.kind in {"panel_layout", "door_layout"} for sheet in agent.session.project.sheets)
    again = agent.handle(
        {
            "op": "insert_circuit",
            "kind": "dol",
            "new_sheet": "true",
            "place_layout": "no",
            "shared_supply": "yes",
        }
    )
    assert again["ok"], again
    assert not any(sheet.kind == "panel_layout" for sheet in agent.session.project.sheets)


_SECOND_PAIRS = (
    ("star_delta", {"kw": 7.5}, "reversing", {"kw": 4}),
    ("plc_motor", {"kw": 5.5, "hoa": True}, "plc_motor", {"kw": 4, "hoa": True}),
    ("reversing", {"kw": 4}, "dol", {"kw": 5.5}),
    ("dol", {"kw": 1.5}, "star_delta", {"kw": 7.5}),
)


def _assert_on_drawn_backplate(session: Session, sheet) -> None:
    from panelcad.core.layout import backplate_sheet_bounds, rail_aabb

    _inside_frame(sheet, _boxes(session, sheet.id, legends=sheet.kind == "door_layout"))
    enc = sheet.enclosure
    if enc is None:
        return
    left, top, right, bottom = backplate_sheet_bounds(enc)
    for name, x, y, width, height in _boxes(session, sheet.id, legends=sheet.kind == "door_layout"):
        assert x >= left - 0.8, name
        assert y >= top - 0.8, name
        assert x + width <= right + 0.8, name
        assert y + height <= bottom + 0.8, name
    for rail in sheet.rails:
        x, y, width, height = rail_aabb(rail)
        assert x >= left - 0.8 and y >= top - 0.8, rail.id
        assert x + width <= right + 0.8 and y + height <= bottom + 0.8, rail.id


@pytest.mark.confirm_generic_layout
@pytest.mark.parametrize(("first", "first_kw", "second", "second_kw"), _SECOND_PAIRS)
def test_second_insert_stays_inside_the_backplate(first, first_kw, second, second_kw):
    """A second circuit stays on the drawn backplate.

    When the current box is only earlier circuits, the insert redraws both
    into a catalog enclosure. A part that still cannot fit is left off the
    sheet and named in the warning list Session, JSONL, and the CLI share.
    """

    session = _iec(f"{first}+{second}")
    session.insert_circuit(first, control="local", **first_kw)
    result = session.insert_circuit(second, control="local", **second_kw)
    assert isinstance(result.get("warnings"), list)
    assert result["warnings"] == list((result.get("layout") or {}).get("warnings") or [])
    for sheet in session.project.sheets:
        if sheet.kind not in {"panel_layout", "door_layout"}:
            continue
        _assert_on_drawn_backplate(session, sheet)
        _assert_clear(_boxes(session, sheet.id, legends=sheet.kind == "door_layout"))
    if result["warnings"]:
        assert any("free backplate" in line or "door outline" in line for line in result["warnings"])


@pytest.mark.confirm_generic_layout
def test_second_insert_warning_reaches_jsonl_and_cli(tmp_path):
    """A hand-sized box is not redrawn. The insert warns instead of leaving the plate."""

    from typer.testing import CliRunner

    from panelcad.cli.agent import Agent
    from panelcad.cli.main import app

    agent = Agent()
    created = agent.handle({"op": "create_project", "name": "Tight"})
    assert created["ok"], created
    agent.handle({"op": "set_title_block", "standard": "IEC"})
    agent.handle({"op": "add_sheet", "sheet_id": "enc", "title": "Enclosure", "kind": "panel_layout"})
    agent.handle({"op": "set_enclosure", "sheet": "enc", "width_mm": 30, "height_mm": 30})
    parked = agent.handle(
        {
            "op": "place",
            "library_id": "iec.terminals.x_2p5",
            "ref": "XKEEP",
            "sheet": "main",
            "x": 20,
            "y": 20,
        }
    )
    assert parked["ok"], parked
    confirmed = agent.handle(
        {
            "op": "confirm_envelope",
            "ref": "XKEEP",
            "width_mm": 5.2,
            "height_mm": 47,
            "depth_mm": 48,
            "mount": "din_rail",
            "confirmed_by": "test",
        }
    )
    assert confirmed["ok"], confirmed
    placed = agent.handle(
        {
            "op": "place_from_bom",
            "library_id": "iec.terminals.x_2p5",
            "ref": "XKEEP",
            "sheet": "enc",
            "x": 20,
            "y": 20,
        }
    )
    assert placed["ok"], placed
    parked = agent.session.project.sheet("enc").component("XKEEP")
    parked_x, parked_y = parked.x, parked.y
    inserted = agent.handle({"op": "insert_circuit", "kind": "dol", "control": "none"})
    assert inserted["ok"], inserted
    warnings = inserted["result"].get("warnings")
    assert isinstance(warnings, list) and warnings
    assert any("free backplate" in line for line in warnings)
    assert inserted["result"]["warnings"] == list(inserted["result"]["layout"].get("warnings") or [])
    kept = agent.session.project.sheet("enc").component("XKEEP")
    assert kept.x == pytest.approx(parked_x)
    assert kept.y == pytest.approx(parked_y)

    path = tmp_path / "tight.pcad.yaml"
    agent.session.save(path)
    # The JSONL insert already filled the warning. A fresh file exercises the CLI.
    fresh = _iec("CLI tight")
    fresh.add_sheet("enc", title="Enclosure", kind="panel_layout")
    enc = fresh.sheet("enc")
    enc.enclosure.width_mm = 30
    enc.enclosure.height_mm = 30
    enc.rails[0].length_mm = 18
    fresh.place("iec.terminals.x_2p5", ref="XKEEP", x=40, y=20, sheet_id="main")
    fresh.confirm_envelope(
        "XKEEP",
        width_mm=5.2,
        height_mm=47,
        depth_mm=48,
        mount="din_rail",
        confirmed_by="test",
    )
    fresh.place_from_bom(
        "iec.terminals.x_2p5",
        ref="XKEEP",
        x=enc.rails[0].x,
        y=enc.rails[0].y,
        sheet_id="enc",
    )
    fresh.save(path)
    result = CliRunner().invoke(
        app,
        ["electrical", "circuit", str(path), "--kind", "dol"],
    )
    assert result.exit_code == 0, result.output
    assert "free backplate" in result.output
    assert "warning:" in result.output


@pytest.mark.confirm_generic_layout
def test_gui_insert_shows_backplate_warning(monkeypatch):
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    session = _iec("Tight gui")
    session.add_sheet("enc", title="Enclosure", kind="panel_layout")
    enc = session.sheet("enc")
    enc.enclosure.width_mm = 30
    enc.enclosure.height_mm = 30
    enc.rails[0].length_mm = 18
    session.place("iec.terminals.x_2p5", ref="XKEEP", x=40, y=20, sheet_id="main")
    session.confirm_envelope(
        "XKEEP",
        width_mm=5.2,
        height_mm=47,
        depth_mm=48,
        mount="din_rail",
        confirmed_by="test",
    )
    keep = session.place_from_bom(
        "iec.terminals.x_2p5",
        ref="XKEEP",
        x=enc.rails[0].x,
        y=enc.rails[0].y,
        sheet_id="enc",
    )
    keep = session.project.sheet("enc").component("XKEEP")
    x0 = keep.x
    session.set_active_sheet("main")
    shown: list[str] = []

    def _warn(_parent, _title, text, *_args, **_kwargs):
        shown.append(str(text))

    monkeypatch.setattr(QMessageBox, "warning", _warn)

    class _Dlg:
        def __init__(self, *_args, **_kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def values(self):
            return {
                "kind": "dol",
                "drive": None,
                "branch": "breaker",
                "line_contactor": False,
                "control": "none",
                "kw": None,
                "hp": None,
                "voltage": None,
                "control_voltage": None,
                "fla": None,
                "pilot_lights": False,
                "bypass": False,
                "control_wiring": "3wire",
                "starter": "dol",
                "timer_seconds": None,
                "overload_placement": "windings",
                "hoa": True,
                "plc_model": None,
                "io_run": None,
                "io_ol": None,
                "io_auto": None,
                "io_out": None,
                "new_sheet": False,
                "manufacturer": None,
                "secondary_manufacturer": None,
                "place_layout": True,
                "shared_supply": True,
                "shared_estop": True,
                "separate_control_rail": False,
            }

    monkeypatch.setattr("panelcad.gui.circuit_dialog.CircuitBuilderDialog", _Dlg)
    window = MainWindow(session)
    window.insert_circuit()
    assert shown
    assert "free backplate" in shown[0]
    assert session.project.sheet("enc").component("XKEEP").x == pytest.approx(x0)
    window.close()
    app.processEvents()


def test_apply_enclosure_end_stops_are_idempotent():
    session = _iec("Stops")
    session.insert_circuit("soft_starter", control="local", kw=7.5, bypass=True)
    session.apply_enclosure(place_parts=True)

    def snap():
        panel = next(sheet for sheet in session.project.sheets if sheet.kind == "panel_layout")
        parts = sorted(
            (comp.id, round(comp.x, 3), round(comp.y, 3))
            for comp in panel.components
        )
        stops = [comp.id for comp in panel.components if str(comp.id).startswith("ES-")]
        bom = sum(
            int(row["qty"])
            for row in grouped_rows(session.project, session.catalog)
            if "end_stop" in row["library_id"]
        )
        return parts, stops, bom

    before = snap()
    session.apply_enclosure(place_parts=True)
    after = snap()
    assert before == after
    assert before[2] == after[2]
    assert all(not ident.endswith(("R2", "R3", "R4", "L2")) for ident in after[1])
    panel = next(sheet for sheet in session.project.sheets if sheet.kind == "panel_layout")
    terminal = next(rail for rail in panel.rails if "terminal" in rail.id)
    for comp in panel.components:
        if not str(comp.id).startswith("ES-"):
            continue
        assert abs(comp.y - terminal.y) > 20.0, comp.id


def test_overlap_tolerance_ignores_a_rounding_hair():
    from panelcad.core.layout import OVERLAP_TOL_MM, rects_overlap

    assert not rects_overlap(0, 0, 10, 10, 10, 0, 4, 10)
    assert not rects_overlap(0, 0, 10 + 1e-14, 10, 10, 0, 4, 10)
    assert rects_overlap(0, 0, 10 + OVERLAP_TOL_MM + 0.2, 10, 10, 0, 4, 10)
    session = _iec("VFD overlap")
    _insert(session, "vfd")
    session.apply_enclosure(place_parts=True)
    overlaps = [row for row in session.design_checks() if row["check"] == "footprint_overlap"]
    assert overlaps == []


@pytest.mark.confirm_generic_layout
def test_first_din_module_starts_on_the_rail():
    from panelcad.core.layout import (
        TRAY_TO_RAIL_X_MM,
        _opening_between_side_ducts,
        clip_offset,
        footprint_size,
        tray_aabb,
    )

    for kind in ("dol", "star_delta", "vfd", "reversing", "plc_motor", "soft_starter"):
        session = _iec(kind + "-clear")
        extra = {"bypass": True} if kind == "soft_starter" else {}
        if kind == "star_delta":
            extra["kw"] = 7.5
        _insert(session, kind, **extra)
        session.apply_enclosure(place_parts=True)
        sheet = next(item for item in session.project.sheets if item.kind == "panel_layout")
        sides = [tray for tray in sheet.trays if tray.orientation == "v"]
        assert sides, kind
        left = min(sides, key=lambda tray: tray.x)
        face = tray_aabb(left)[0] + tray_aabb(left)[2]
        checked = 0
        for rail in sheet.rails:
            modules = []
            for comp in sheet.components:
                if str(comp.id).startswith("ES-") or str(comp.properties.get("accessory") or "") == "end_stop":
                    continue
                device = session.catalog.get(comp.library_id)
                width, _height = footprint_size(device, comp)
                if width <= 0:
                    continue
                if abs(comp.y + clip_offset(device, comp) - rail.y) > 0.8:
                    continue
                modules.append(comp)
            if not modules:
                continue
            first = min(modules, key=lambda comp: comp.x)
            scale = float(sheet.enclosure.draw_scale or 1.0)
            clear = TRAY_TO_RAIL_X_MM * scale
            assert first.x + 0.05 >= rail.x, (kind, first.id, first.x, rail.x)
            gap = first.x - face
            assert gap + 0.05 >= 0.3, (kind, first.id, gap)
            # Full TRAY_TO_RAIL_X_MM when the opening has that much spare.
            # A row that fills the panel keeps a smaller gap so it does not enter the far duct.
            # A leading end stop sits on the clearance line, one gap ahead of the terminal.
            from panelcad.core.layout import END_STOP_MM, MODULE_GAP_MM

            allowance = clear + (END_STOP_MM + MODULE_GAP_MM) * scale + 0.6
            assert gap <= allowance, (kind, first.id, gap, allowance)
            checked += 1
        assert checked >= 1, kind


def _panel_sheet(session: Session):
    return next(sheet for sheet in session.project.sheets if sheet.kind == "panel_layout")


def _assert_legible_first_insert(session: Session, result: dict) -> None:
    from panelcad.core.layout import backplate_sheet_bounds, footprint_size

    warns = " ".join(str(line) for line in (result.get("warnings") or []))
    assert "not fit" not in warns, warns
    sheet = _panel_sheet(session)
    enc = sheet.enclosure
    assert enc is not None
    scale = float(enc.draw_scale or 1.0)
    plate = backplate_sheet_bounds(enc)
    for comp in sheet.components:
        assert float(comp.scale or 1.0) >= scale * 0.99, (comp.id, comp.scale, scale)
        device = session.catalog.get(comp.library_id)
        width, height = footprint_size(device, comp)
        if width <= 0 or height <= 0:
            continue
        assert comp.x >= plate[0] - 0.6, comp.id
        assert comp.y >= plate[1] - 0.6, comp.id
        assert comp.x + width <= plate[2] + 0.6, comp.id
        assert comp.y + height <= plate[3] + 0.6, comp.id


@pytest.mark.parametrize(
    ("standard", "extra"),
    [
        ("IEC", {"kw": 7.5}),
        ("IEC", {"kw": 7.5, "bypass": True}),
        ("NEMA/ANSI", {"hp": 10}),
        ("NEMA/ANSI", {"hp": 10, "bypass": True}),
    ],
)
@pytest.mark.confirm_generic_layout
def test_single_soft_starter_insert_refits_without_apply(standard: str, extra: dict):
    session = Session.create("soft starter")
    session.set_title_block(standard=standard)
    result = session.insert_circuit(kind="soft_starter", control="local", **extra)
    _assert_legible_first_insert(session, result)
    sheet = _panel_sheet(session)
    enc = sheet.enclosure
    assert enc is not None
    assert (round(enc.width_mm, 1), round(enc.height_mm, 1)) != (261.0, 348.0)
    assert enc.width_mm == pytest.approx(508.0, abs=1.0)
    assert enc.height_mm == pytest.approx(609.6, abs=1.0)
    assert enc.depth_mm == pytest.approx(254.0, abs=1.0)
    assert float(enc.draw_scale or 1.0) == pytest.approx(0.5, abs=0.02)
    starter = next(
        comp
        for comp in sheet.components
        if comp.properties.get("role") in {"softstarter", "soft_starter"}
        or "soft" in comp.library_id
    )
    assert float(starter.scale) == pytest.approx(float(enc.draw_scale), abs=0.02)


@pytest.mark.parametrize("kind", _CIRCUIT_KINDS)
def test_every_kind_is_legible_as_a_first_insert(kind: str):
    session = _iec(kind + "-first")
    extra = {"bypass": True} if kind == "soft_starter" else {}
    if kind == "star_delta":
        extra["kw"] = 7.5
    result = _insert(session, kind, **extra)
    _assert_legible_first_insert(session, result)


def test_second_terminal_group_shares_the_first_clip_line():
    from panelcad.core.layout import clip_offset

    session = _iec("Terminal line")
    session.insert_circuit(kind="star_delta", control="local", kw=7.5)
    session.insert_circuit(kind="reversing", control="local", kw=4)
    sheet = _panel_sheet(session)
    clips = []
    for comp in sheet.components:
        if comp.properties.get("layout_band") != "terminal":
            continue
        device = session.catalog.get(comp.library_id)
        clips.append(comp.y + clip_offset(device, comp))
    assert len(clips) >= 8
    assert max(clips) - min(clips) < 0.4


def test_terminal_tags_stay_off_the_duct_after_insert():
    from panelcad.render.layout import plan_terminal_labels

    session = _iec("Terminal tags")
    session.insert_circuit(kind="star_delta", control="local", kw=7.5)
    session.insert_circuit(kind="reversing", control="local", kw=4)
    sheet = _panel_sheet(session)
    labels = plan_terminal_labels(sheet, session.catalog)
    assert labels
    for tray in sheet.trays:
        duct = tray_aabb(tray)
        for ident, label in labels.items():
            assert not rects_overlap(*label.rect, *duct), (ident, tray.id)


@pytest.mark.confirm_generic_layout
def test_size_enclosure_keeps_the_box_the_insert_fits():
    session = _iec("Keep N412")
    session.insert_circuit(kind="star_delta", control="local", kw=7.5)
    session.insert_circuit(kind="reversing", control="local", kw=4)
    sheet = _panel_sheet(session)
    assert sheet.enclosure is not None
    assert sheet.enclosure.catalog == "N412242008C"
    assert float(sheet.enclosure.draw_scale) == pytest.approx(0.5, abs=0.02)
    rec = session.recommend_enclosure()
    box = rec["enclosure"]
    assert box["id"] == "ad-n412242008c"
    assert box["panel_height_mm"] + 1e-6 >= rec["needed"]["packed_height_mm"]
    assert box["panel_width_mm"] + 1e-6 >= rec["needed"]["packed_width_mm"]
    _assert_strip_on_rails(session)
    applied = session.apply_enclosure(place_parts=True, recommendation=rec)
    assert (applied.get("enclosure") or {}).get("id") == "ad-n412242008c"
    sheet = _panel_sheet(session)
    assert sheet.enclosure is not None
    _assert_strip_on_rails(session)
    assert sheet.enclosure.catalog == "N412242008C"
    assert float(sheet.enclosure.draw_scale) == pytest.approx(0.5, abs=0.02)
    _assert_legible_first_insert(session, {"warnings": applied.get("warnings") or []})


def _assert_strip_on_rails(session: Session) -> None:
    """No footprint clash, and every DIN module — end stops included — sits on a rail."""

    from panelcad.core.layout import clip_offset, is_rail_module

    overlaps = [row for row in session.design_checks() if row.get("check") == "footprint_overlap"]
    assert overlaps == [], overlaps
    sheet = _panel_sheet(session)
    off: list[str] = []
    for comp in sheet.components:
        try:
            device = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        if not is_rail_module(device, comp):
            continue
        width, _height = footprint_size(device, comp)
        if width <= 0:
            continue
        clip_y = comp.y + clip_offset(device, comp)
        mounted = any(
            abs(clip_y - rail.y) <= 0.6
            and comp.x >= rail.x - 0.6
            and comp.x + width <= rail.x + rail.length_mm + 0.6
            for rail in sheet.rails
        )
        if not mounted:
            off.append(comp.id)
    assert off == [], "DIN part not within a rail: " + ", ".join(off)


@pytest.mark.parametrize("kind", _CIRCUIT_KINDS)
def test_first_insert_has_no_rail_faults(kind: str):
    session = _iec(kind + "-rails")
    extra = {"bypass": True} if kind == "soft_starter" else {}
    if kind == "star_delta":
        extra["kw"] = 7.5
    _insert(session, kind, **extra)
    _assert_strip_on_rails(session)


def _breaker_duct_gap(session: Session) -> float:
    from panelcad.core.layout import TRAY_TO_RAIL_X_MM

    sheet = _panel_sheet(session)
    enc = sheet.enclosure
    assert enc is not None
    scale = float(enc.draw_scale or 1.0)
    sides = [tray for tray in sheet.trays if tray.orientation == "v"]
    left = min(sides, key=lambda tray: tray.x)
    face = tray_aabb(left)[0] + tray_aabb(left)[2]
    breaker = next(
        comp
        for comp in sheet.components
        if comp.properties.get("role") in {"breaker", "disconnect"}
    )
    true_gap = (breaker.x - face) / scale
    # The drive is one airflow gap past the row, so the opening cannot take
    # the full 40 mm. The trimmed rail uses _rail_gap_beside_ducts, which
    # returns half that spare (about 12 mm) instead of the 0.8 mm stroke.
    assert 8.0 < true_gap <= TRAY_TO_RAIL_X_MM + 0.5, true_gap
    return true_gap


@pytest.mark.confirm_generic_layout
def test_trimmed_soft_starter_row_clears_the_duct():
    session = _iec("SS duct")
    session.insert_circuit(kind="soft_starter", control="local", kw=7.5)
    _breaker_duct_gap(session)
    _assert_strip_on_rails(session)
    session.apply_enclosure(place_parts=True)
    _breaker_duct_gap(session)
    _assert_strip_on_rails(session)


@pytest.mark.confirm_generic_layout
def test_star_delta_reversing_pair_stays_on_the_rails():
    session = _iec("SD REV rails")
    session.insert_circuit(kind="star_delta", control="local", kw=7.5)
    session.insert_circuit(kind="reversing", control="local", kw=4)
    _assert_strip_on_rails(session)
    session.apply_enclosure(place_parts=True)
    _assert_strip_on_rails(session)


@pytest.mark.confirm_generic_layout
def test_star_delta_pilot_lights_and_reversing_stay_on_the_rails():
    session = _iec("SD PL rails")
    session.insert_circuit(kind="star_delta", control="local", kw=7.5, pilot_lights=True)
    session.insert_circuit(kind="reversing", control="local", kw=4, pilot_lights=True)
    _assert_strip_on_rails(session)
    session.apply_enclosure(place_parts=True)
    _assert_strip_on_rails(session)


@pytest.mark.confirm_generic_layout
def test_nema_star_delta_reversing_pair_stays_on_the_rails():
    session = Session.create("NEMA rails")
    session.set_title_block(standard="NEMA/ANSI")
    session.insert_circuit(kind="star_delta", control="local", hp=10)
    session.insert_circuit(kind="reversing", control="local", hp=5)
    _assert_strip_on_rails(session)
    session.apply_enclosure(place_parts=True)
    _assert_strip_on_rails(session)


@pytest.mark.confirm_generic_layout
def test_star_delta_reversing_and_dol_stay_on_the_rails():
    session = _iec("SD REV DOL")
    session.insert_circuit(kind="star_delta", control="local", kw=7.5)
    session.insert_circuit(kind="reversing", control="local", kw=4)
    session.insert_circuit(kind="dol", control="local", kw=1.5)
    _assert_strip_on_rails(session)
    session.apply_enclosure(place_parts=True)
    _assert_strip_on_rails(session)
