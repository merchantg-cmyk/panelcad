"""Multi-element device cross-references and the troubleshooting index API."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.cli.main import app
from panelcad.core.xref import contact_list_label, xref_annotations, xref_families
from panelcad.examples.xref_demo import build_nema_seal_in
from panelcad.render.wire_labels import boxes_overlap
from panelcad.render.xref import annotation_box, place_xref_labels


def _two_sheet(name: str, standard: str = "NEMA/ANSI") -> Session:
    session = Session.create(name, sheet_id="power", title="Power")
    session.set_title_block(standard=standard, drawing_style="ladder")
    session.add_sheet("control", title="Control", kind="schematic")
    return session


def _elements(session: Session, ref: str) -> set[str]:
    return {row["element"] for row in session.device_index(ref)["elements"]}


def _refs(session: Session, ref: str) -> set[str]:
    return {row["ref"] for row in session.device_index(ref)["elements"]}


def _no_overlap(session: Session) -> None:
    for sheet in session.project.sheets:
        if sheet.kind not in {"schematic", "io"}:
            continue
        placed = place_xref_labels(session.project, session.catalog, sheet, ladder=True)
        boxes = [(ann.owner, annotation_box(ann, x, y, align)) for ann, x, y, align in placed]
        for i, (owner, box) in enumerate(boxes):
            for other, other_box in boxes[i + 1 :]:
                assert not boxes_overlap(box, other_box, pad=0.05), (sheet.id, owner, other, box, other_box)


def test_audit_known_families_still_group_coil_and_contact():
    session = build_nema_seal_in()
    families = xref_families(session.project, session.catalog)
    assert "M1" in families and families["M1"].coils and families["M1"].contacts
    assert "CR1" in families
    assert any(row["element"] == "ol_contact" for row in session.device_index("OL1")["elements"])


def test_motor_starter_index_lists_coil_poles_aux_and_heater():
    session = _two_sheet("Starter")
    session.place("nema.contactors.coil", ref="M1", x=80, y=40, sheet_id="power")
    session.place("nema.contactors.aux_no", ref="M1-13", x=80, y=40, sheet_id="control")
    session.place("nema.protection.overload", ref="OL1", x=140, y=40, sheet_id="power")
    session.place("nema.protection.ol_contact", ref="OL1-95", x=140, y=80, sheet_id="control")
    families = xref_families(session.project, session.catalog)
    assert "M1" in families and "OL1" in families
    m1 = session.device_index("M1")
    assert {row["ref"] for row in m1["elements"]} >= {"M1", "M1-13"}
    assert "contact" in _elements(session, "M1")
    ol = session.device_index("OL1")
    assert {row["element"] for row in ol["elements"]} >= {"heater", "ol_contact"}
    anns = xref_annotations(session.project, session.catalog, session.sheet("control"))
    assert any(item.owner == "M1-13" and item.kind == "contact" for item in anns)
    _no_overlap(session)


def test_breaker_and_mpcb_aux_and_trip_alarm():
    session = _two_sheet("Breakers", standard="IEC")
    session.place("iec.breakers.mcb_3p_32a", ref="Q1", x=40, y=40, sheet_id="power")
    session.place("iec.contactors.aux_no", ref="Q1-13", x=40, y=40, sheet_id="control")
    session.place("iec.breakers.mpcb_3p", ref="Q2", x=120, y=40, sheet_id="power")
    session.place(
        "iec.contactors.aux_nc",
        ref="Q2-95",
        x=120,
        y=80,
        sheet_id="control",
        properties={"role": "trip_alarm"},
    )
    assert "Q1" in xref_families(session.project, session.catalog)
    assert "contact" in _elements(session, "Q1")
    assert "main_poles" in _elements(session, "Q1")
    q2 = session.device_index("Q2")
    assert any(row["element"] == "trip_alarm" for row in q2["elements"])
    anns = xref_annotations(session.project, session.catalog, session.sheet("power"))
    assert any(item.owner == "Q1" and item.lines for item in anns)
    _no_overlap(session)


def test_disconnect_with_aux_contact():
    session = _two_sheet("Disconnect")
    session.place("nema.protection.disconnect_3p", ref="DISC1", x=40, y=40, sheet_id="power")
    session.place("nema.contactors.aux_no", ref="DISC1-13", x=40, y=40, sheet_id="control")
    assert "DISC1" in xref_families(session.project, session.catalog)
    assert _refs(session, "DISC1") >= {"DISC1", "DISC1-13"}
    _no_overlap(session)


def test_timer_and_safety_relay_contacts():
    session = _two_sheet("Timing", standard="IEC")
    session.place("iec.timers.on_delay", ref="K2", x=40, y=40, sheet_id="power")
    session.place("iec.contactors.aux_no", ref="K2-15", x=40, y=40, sheet_id="control")
    session.place("iec.relays.safety_24vdc", ref="K3", x=140, y=40, sheet_id="power")
    session.place("iec.contactors.aux_nc", ref="K3-31", x=140, y=80, sheet_id="control")
    assert {row["element"] for row in session.device_index("K2")["elements"]} >= {"timer", "contact"}
    assert {row["element"] for row in session.device_index("K3")["elements"]} >= {"safety", "contact"}
    _no_overlap(session)


def test_vfd_and_soft_starter_power_versus_control():
    session = _two_sheet("Drives", standard="IEC")
    session.place("iec.drives.vfd_1k5", ref="U1", x=40, y=40, sheet_id="power")
    session.place("iec.contactors.aux_no", ref="U1-FAULT", x=40, y=40, sheet_id="control")
    session.place("iec.drives.softstarter", ref="SS1", x=140, y=40, sheet_id="power")
    session.place("iec.contactors.aux_no", ref="SS1-13", x=140, y=80, sheet_id="control")
    u1 = session.device_index("U1")
    kinds = {row["element"] for row in u1["elements"]}
    assert "drive" in kinds or "drive_power" in kinds
    assert "fault" in kinds
    assert any(row["element"] == "drive_control" for row in u1["elements"])
    assert _refs(session, "SS1") >= {"SS1", "SS1-13"}
    _no_overlap(session)


def test_pushbutton_and_selector_contact_blocks():
    session = _two_sheet("Operators")
    session.place("nema.operators.pb_no", ref="PB1", x=40, y=40, sheet_id="control")
    session.place("nema.contactors.aux_nc", ref="PB1-21", x=40, y=80, sheet_id="power")
    session.place("nema.operators.selector_3pos", ref="SEL1", x=140, y=40, sheet_id="control")
    session.place("nema.contactors.aux_no", ref="SEL1-13", x=140, y=80, sheet_id="power")
    assert _refs(session, "PB1") >= {"PB1", "PB1-21"}
    assert _refs(session, "SEL1") >= {"SEL1", "SEL1-13"}
    _no_overlap(session)


def test_plc_io_stays_in_the_index_and_keeps_see_refs():
    session = Session.create("PLC", sheet_id="rack")
    session.place("iec.plc.compact_16dio", ref="A1", x=40, y=40)
    session.sheet("rack").component("A1").properties["xref_rack"] = "5-A2"
    rows = session.device_index("A1")["elements"]
    assert rows
    assert any(row["element"] in {"plc", "plc_io"} for row in rows)


def test_offpage_continuation_and_broken_reference():
    session = build_nema_seal_in()
    off = [row for row in session.device_index_rows() if row["element"] == "offpage"]
    assert off
    assert any(row["dest_ref"] for row in off)
    broken = Session.create("BrokenOff")
    broken.place(
        "iec.connection.offpage",
        ref="OP9",
        x=40,
        y=40,
        properties={"dest_sheet": "missing", "dest_ref": "K9"},
    )
    checks = {row["check"] for row in broken.design_checks()}
    assert "broken_offpage" in checks


def test_orphan_contact_still_flags_when_parent_is_missing():
    session = Session.create("OrphanAux")
    session.place("iec.contactors.aux_no", ref="K9-13", x=20, y=20)
    checks = {row["check"] for row in session.design_checks()}
    assert "contact_without_coil" in checks


def test_index_api_session_cli_agent_parity(tmp_path: Path):
    session = build_nema_seal_in()
    device = session.device_index("M1")
    zone = session.lookup_zone(device["elements"][0]["page_zone"])
    wire_no = next(
        (part for row in device["elements"] for part in str(row["wires"]).split(",") if part),
        "",
    )
    traced = session.trace_wire(wire_no) if wire_no else {"number": "", "segments": []}
    assert device["device"] == "M1"
    assert device["elements"]
    assert zone["matches"]
    path = tmp_path / "seal.pcad.yaml"
    session.save(path)

    agent = Agent()
    assert agent.handle({"id": 1, "op": "open", "path": str(path)})["ok"]
    listed = agent.handle({"id": 2, "op": "device_index", "ref": "M1"})
    assert listed["ok"], listed
    assert listed["result"]["count"] == device["count"]
    at = agent.handle({"id": 3, "op": "lookup_zone", "zone": device["elements"][0]["page_zone"]})
    assert at["ok"] and at["result"]["count"] == zone["count"]
    if wire_no:
        follow = agent.handle({"id": 4, "op": "trace_wire", "number": wire_no})
        assert follow["ok"]
        assert follow["result"]["number"] == traced["number"]
        assert follow["result"]["count"] == traced["count"]
    help_r = agent.handle({"id": 5, "op": "help"})
    assert "device_index" in help_r["result"]["ops"]
    assert "lookup_zone" in help_r["result"]["ops"]
    assert "trace_wire" in help_r["result"]["ops"]
    assert "device_index" in help_r["result"]["schemas"]

    runner = CliRunner()
    shown = runner.invoke(app, ["electrical", "xref", str(path), "--device", "M1"])
    assert shown.exit_code == 0, shown.output
    assert '"M1"' in shown.output
    zoned = runner.invoke(app, ["electrical", "xref", str(path), "--zone", device["elements"][0]["page_zone"]])
    assert zoned.exit_code == 0, zoned.output
    assert '"matches"' in zoned.output
    if wire_no:
        wired = runner.invoke(app, ["electrical", "xref", str(path), "--wire", wire_no])
        assert wired.exit_code == 0, wired.output
        assert wire_no in wired.output


def test_print_package_includes_device_index(tmp_path: Path):
    session = build_nema_seal_in()
    plan = session.plan_print_package()
    assert "xref" in plan["sections"]
    assert any(row["title"] == "Device Index" for row in plan["index"])
    dest = tmp_path / "xref_pkg.pdf"
    result = session.print_package(dest, sections=["xref"])
    assert result["pages"] >= 1
    assert dest.is_file()


def test_lookup_zone_accepts_iec_and_nema_forms():
    session = build_nema_seal_in()
    row = session.device_index("M1")["elements"][0]
    by_page = session.lookup_zone(row["page_zone"])
    by_line = session.lookup_zone(row["line"])
    assert by_page["count"] >= 1
    assert by_line["count"] >= 1
    assert any(item["ref"] == row["ref"] for item in by_page["matches"])


def _nameplate_and_pin_boxes(session: Session, sheet) -> list[tuple[str, tuple[float, float, float, float]]]:
    from panelcad.core.ink import text_box
    from panelcad.core.pins import rating_caption_positions
    from panelcad.render.sheet_labels import pin_caption_boxes

    items = list(pin_caption_boxes(sheet, session.catalog))
    for comp in sheet.components:
        device = session.catalog.devices.get(comp.library_id)
        if device is None:
            continue
        scale = comp.visual_scale()
        for cx, cy, line in rating_caption_positions(
            comp, device.schematic_height, scale, device.schematic_width
        ):
            box = text_box(cx, cy, line, 2.2, "start")
            if box is not None:
                items.append((f"nameplate:{comp.id}:{line}", box))
    return items


def test_iec_location_list_misses_nameplate_and_pin_text():
    session = Session.create("IEC SS xref", sheet_id="power", title="Power")
    session.set_title_block(standard="IEC", drawing_style="iec")
    session.insert_circuit(
        kind="soft_starter",
        control="local",
        bypass=True,
        pilot_lights=True,
        standard="IEC",
    )
    control = session.add_sheet("xref_aux", title="Aux", kind="schematic")
    session.place("iec.breakers.mcb_3p_32a", ref="Q9", x=40, y=40, sheet_id="power")
    session.place("iec.contactors.aux_no", ref="Q9-13", x=40, y=40, sheet_id=control.id)
    for sheet in session.project.sheets:
        if sheet.kind not in {"schematic", "io"}:
            continue
        placed = place_xref_labels(session.project, session.catalog, sheet, ladder=True)
        lists = [
            (ann, annotation_box(ann, x, y, align))
            for ann, x, y, align in placed
            if ann.kind == "coil"
        ]
        obstacles = _nameplate_and_pin_boxes(session, sheet)
        for ann, box in lists:
            for name, other in obstacles:
                assert not boxes_overlap(box, other, pad=0.05), (sheet.id, ann.owner, name, box, other)
    _assert_xref_misses_ink(session)


def _assert_xref_misses_ink(session: Session, *, threshold: int = 150, min_px: int = 8) -> None:
    """New dark pixels from xref must not land on nameplates, pins, tags, or wire numbers."""

    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from panelcad.core.ink import device_ref_layout, text_box
    from panelcad.render.page import paint_sheet

    QApplication.instance() or QApplication([])
    scale = 4.0

    def _paint(on: bool) -> dict[str, QImage]:
        session.set_cross_reference(enabled=on)
        images = {}
        for sheet in session.project.sheets:
            if sheet.kind not in {"schematic", "io"}:
                continue
            width, height = sheet.page.dimensions()
            image = QImage(int(width * scale), int(height * scale), QImage.Format.Format_RGB32)
            image.fill(QColor("#ffffff"))
            painter = QPainter(image)
            painter.scale(scale, scale)
            paint_sheet(painter, session.project, sheet, session.catalog, sheet_index=0, sheet_count=1)
            painter.end()
            images[sheet.id] = image
        return images

    full = _paint(True)
    bare = _paint(False)
    session.set_cross_reference(enabled=True)
    from panelcad.render.wire_labels import place_wire_marks
    from panelcad.render.wiring import route_sheet_wires

    for sheet in session.project.sheets:
        if sheet.kind not in {"schematic", "io"}:
            continue
        regions = list(_nameplate_and_pin_boxes(session, sheet))
        align = "middle"
        for comp in sheet.components:
            device = session.catalog.devices.get(comp.library_id)
            if device is None:
                continue
            layout = device_ref_layout(device, comp, ref_align=align)
            if layout is None:
                continue
            box = text_box(layout[0], layout[1], comp.id, layout[3], layout[2])
            if box is not None:
                regions.append((f"tag:{comp.id}", box))
        try:
            routes = route_sheet_wires(sheet, session.catalog, project=session.project)
        except (KeyError, ValueError, IndexError):
            routes = {}
        marks = place_wire_marks(sheet, session.catalog, routes, ladder=True, project=session.project)
        for wire in sheet.wires:
            at = marks.get(wire.id)
            mark = wire.drawn_mark(ladder=True)
            if not at or not mark:
                continue
            box = text_box(at[0], at[1], mark, 2.3, at[2])
            if box is not None:
                regions.append((f"wire:{wire.id}:{mark}", box))
        img_full = full[sheet.id]
        img_bare = bare[sheet.id]
        for name, box in regions:
            x0 = max(0, int(box[0] * scale))
            y0 = max(0, int(box[1] * scale))
            x1 = min(img_full.width(), int(box[2] * scale) + 1)
            y1 = min(img_full.height(), int(box[3] * scale) + 1)
            hits = 0
            for yy in range(y0, y1):
                for xx in range(x0, x1):
                    painted = img_full.pixelColor(xx, yy)
                    prior = img_bare.pixelColor(xx, yy)
                    if max(painted.red(), painted.green(), painted.blue()) < threshold and max(
                        prior.red(), prior.green(), prior.blue()
                    ) >= threshold:
                        hits += 1
            assert hits < min_px, (sheet.id, name, hits, box)


def test_offpage_callout_iec_drops_double_slash():
    from panelcad.core.xref import offpage_callout
    from panelcad.examples.xref_demo import build_iec_mirror

    iec = build_iec_mirror()
    iec.add_sheet("far", title="Far", kind="schematic")
    iec.place(
        "iec.connection.offpage",
        ref="OP1",
        x=40,
        y=40,
        sheet_id="main",
        properties={"dest_sheet": "far", "dest_ref": "K9", "role": "to"},
    )
    iec.place("iec.contactors.coil", ref="K9", x=80, y=40, sheet_id="far")
    iec.place(
        "iec.connection.offpage",
        ref="OP2",
        x=20,
        y=40,
        sheet_id="far",
        properties={"dest_sheet": "main", "dest_ref": "OP1", "role": "from"},
    )
    text = offpage_callout(iec.project, iec.catalog, iec.sheet("main"), iec.sheet("main").component("OP1"))
    assert text.startswith("TO ")
    assert "/ /" not in text
    _prefix, loc = text.split(" / ", 1)
    assert loc and not loc.startswith("/")
    term = next(row["terminals"] for row in iec.device_index("OP1")["elements"] if row["element"] == "offpage")
    assert term == text

    nema = build_nema_seal_in()
    nema_comp, nema_sheet = next(
        (comp, sheet)
        for sheet in nema.project.sheets
        for comp in sheet.components
        if "offpage" in (comp.library_id or "").lower()
    )
    nema_text = offpage_callout(nema.project, nema.catalog, nema_sheet, nema_comp)
    assert nema_text.startswith("TO ") or nema_text.startswith("FROM ")
    assert "/ /" not in nema_text
    assert "/" not in nema_text.split(" / ", 1)[1]
    nema_term = next(
        row["terminals"]
        for row in nema.device_index_rows()
        if row["element"] == "offpage" and row["ref"] == nema_comp.id
    )
    assert nema_term == nema_text


def test_nema_aux_and_trip_on_same_rung_differ():
    session = _two_sheet("SameRung")
    session.place("nema.breakers.mpcb_3p", ref="MCP1", x=40, y=40, sheet_id="power")
    session.place("nema.contactors.aux_no", ref="MCP1-13", x=80, y=40, sheet_id="control")
    session.place(
        "nema.contactors.aux_nc",
        ref="MCP1-95",
        x=140,
        y=40,
        sheet_id="control",
        properties={"role": "trip"},
    )
    anns = [item for item in xref_annotations(session.project, session.catalog) if item.owner == "MCP1"]
    assert anns
    lines = [line for ann in anns for line in ann.lines]
    assert len(lines) >= 2
    assert lines[0] != lines[1]
    family = xref_families(session.project, session.catalog)["MCP1"]
    by_child = {contact.comp.id: contact_list_label(contact, "nema", "underline")[0] for contact in family.contacts}
    assert by_child["MCP1-13"] != by_child["MCP1-95"]
    assert by_child["MCP1-13"] in lines
    assert by_child["MCP1-95"] in lines
    back = [item for item in xref_annotations(session.project, session.catalog) if item.kind == "contact"]
    assert all(item.lines and item.lines[0] for item in back)


def test_drive_timer_safety_index_pins_and_fault_role():
    session = _two_sheet("PinGroups", standard="IEC")
    session.place("iec.drives.softstarter", ref="SS1", x=40, y=40, sheet_id="power")
    session.place(
        "iec.contactors.aux_no",
        ref="SS1-FAULT",
        x=40,
        y=40,
        sheet_id="control",
        properties={"role": "fault"},
    )
    session.place("iec.timers.on_delay", ref="K4", x=140, y=40, sheet_id="power")
    session.place("iec.relays.safety_24vdc", ref="K5", x=240, y=40, sheet_id="power")
    ss = {row["element"]: row for row in session.device_index("SS1")["elements"]}
    assert ss["fault"]["element"] == "fault"
    assert "ST" in ss["drive_control"]["terminals"]
    assert "95" in ss["drive_relay"]["terminals"] or "96" in ss["drive_relay"]["terminals"]
    timer = {row["element"]: row for row in session.device_index("K4")["elements"]}
    assert "15" in timer["timed_contact"]["terminals"] and "18" in timer["timed_contact"]["terminals"]
    safety = {row["element"]: row for row in session.device_index("K5")["elements"]}
    assert "13" in safety["safety_output"]["terminals"]
    assert "24" in safety["safety_output"]["terminals"]


def test_aux_named_k4_18_uses_symbol_pin_ids():
    session = _two_sheet("PinIds", standard="IEC")
    session.place("iec.timers.on_delay", ref="K4", x=40, y=40, sheet_id="power")
    session.place("iec.contactors.aux_no", ref="K4-18", x=40, y=40, sheet_id="control")
    row = next(item for item in session.device_index("K4")["elements"] if item["ref"] == "K4-18")
    device = session.catalog.get("iec.contactors.aux_no")
    pins = [str(pin.id) for pin in device.pins]
    assert row["terminals"] == f"{pins[0]}-{pins[1]}"
    assert row["terminals"] == "13-14"


def test_lookup_zone_and_trace_wire_reject_bad_queries(tmp_path: Path):
    session = build_nema_seal_in()
    session.add_sheet("spare", title="Spare", kind="schematic")
    path = tmp_path / "zones.pcad.yaml"
    session.save(path)
    good = session.device_index("M1")["elements"][0]["page_zone"]
    ok = session.lookup_zone(good)
    assert ok["count"] >= 1
    assert ok["matches"]
    bad = ["Z9", "99-A1", "foo", "5-", "1-A99", "2-H1", ""]
    for query in bad:
        try:
            session.lookup_zone(query)
            raise AssertionError(f"lookup_zone({query!r}) should have failed")
        except ValueError as exc:
            assert str(exc)
    try:
        session.trace_wire("NO-SUCH-WIRE")
        raise AssertionError("unknown wire should fail")
    except ValueError as exc:
        assert "NO-SUCH-WIRE" in str(exc)

    agent = Agent()
    assert agent.handle({"id": 1, "op": "open", "path": str(path)})["ok"]
    for query in bad:
        reply = agent.handle({"id": 2, "op": "lookup_zone", "zone": query})
        assert reply["ok"] is False, (query, reply)
        assert reply.get("error")
    wired = agent.handle({"id": 3, "op": "trace_wire", "number": "NO-SUCH-WIRE"})
    assert wired["ok"] is False and wired.get("error")
    good_agent = agent.handle({"id": 4, "op": "lookup_zone", "zone": good})
    assert good_agent["ok"] and good_agent["result"]["count"] == ok["count"]

    runner = CliRunner()
    for query in bad:
        shown = runner.invoke(app, ["electrical", "xref", str(path), "--zone", query])
        assert shown.exit_code != 0, (query, shown.output)
        assert shown.exception is None or isinstance(shown.exception, SystemExit)
        assert "Traceback" not in (shown.output or "")
    miss = runner.invoke(app, ["electrical", "xref", str(path), "--wire", "NO-SUCH-WIRE"])
    assert miss.exit_code != 0
    assert "Traceback" not in (miss.output or "")
    listed = runner.invoke(app, ["electrical", "xref", str(path), "--zone", good])
    assert listed.exit_code == 0, listed.output


def test_device_index_lamp_exact_tag_and_plc_point_zone():
    session = Session.create("LampsPLC", sheet_id="control")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder")
    session.place("nema.operators.pilot_light", ref="H1", x=80, y=80)
    session.place("nema.contactors.coil", ref="M1", x=160, y=80)
    session.place("nema.contactors.coil", ref="M10", x=240, y=80)
    session.wire("H1.X1", "M1.A1", number="208")
    session.place(
        "iec.connection.offpage",
        ref="OP-H1",
        x=40,
        y=40,
        properties={"dest_sheet": "control", "dest_ref": "H1X", "role": "to"},
    )
    lamps = session.device_index("H1")
    assert lamps["count"] >= 1
    lamp = next(row for row in lamps["elements"] if row["ref"] == "H1")
    assert lamp["page_zone"]
    assert "X1" in lamp["terminals"] or "X2" in lamp["terminals"]
    assert "208" in lamp["wires"]
    assert all(row["element"] != "offpage" for row in lamps["elements"])
    m1 = {row["ref"] for row in session.device_index("M1")["elements"]}
    assert "M1" in m1
    assert "M10" not in m1

    session.place("iec.plc.compact_16dio", ref="A1", x=40, y=160)
    session.place("nema.contactors.coil", ref="CR1", x=200, y=200)
    session.wire("A1.Q0_0", "CR1.A1")
    plc = session.device_index("A1")
    points = [row for row in plc["elements"] if row["element"] == "plc_io"]
    assert points
    out = next((row for row in points if row["ref"].endswith("Q0_0") or "Q0_0" in row["terminals"]), points[0])
    assert out["address"] == "Q0_0" or out["terminals"] == "Q0_0"
    assert out["page_zone"]
    assert out.get("field_device") == "CR1" or "CR1" in out.get("wires", "") or out.get("wires")


def test_contact_count_skips_operator_and_mpcb_trip():
    pb = Session.create("PB blocks")
    pb.place("iec.operators.pb_no", ref="PB1", x=20, y=20)
    pb.place("iec.contactors.aux_no", ref="PB1-13", x=20, y=60)
    pb.place("iec.contactors.aux_nc", ref="PB1-21", x=20, y=100)
    assert not any(row["check"] == "contact_count_exceeded" for row in pb.design_checks())

    sel = Session.create("SEL blocks")
    sel.place("iec.operators.selector_3pos", ref="SEL1", x=20, y=20)
    sel.place("iec.contactors.aux_no", ref="SEL1-13", x=20, y=60)
    sel.place("iec.contactors.aux_nc", ref="SEL1-21", x=20, y=100)
    assert not any(row["check"] == "contact_count_exceeded" for row in sel.design_checks())

    mcp = Session.create("MCP trip")
    mcp.place("nema.breakers.mpcb_3p", ref="MCP1", x=20, y=20)
    mcp.place("nema.contactors.aux_no", ref="MCP1-13", x=20, y=80)
    mcp.place(
        "nema.contactors.aux_nc",
        ref="MCP1-95",
        x=20,
        y=120,
        properties={"role": "trip"},
    )
    checks = mcp.design_checks()
    assert not any(row["check"] == "contact_count_exceeded" for row in checks)
    assert not any(row["check"] == "contact_without_coil" for row in checks)

    over = Session.create("Coil over")
    over.place("iec.contactors.coil", ref="K1", x=20, y=20)
    over.place("iec.contactors.aux_no", ref="K1-13", x=20, y=60)
    over.place("iec.contactors.aux_nc", ref="K1-21", x=20, y=100)
    assert not any(row["check"] == "contact_count_exceeded" for row in over.design_checks())
    over.assign_part("K1", "100-C09EJ10")
    assert any(row["check"] == "contact_count_exceeded" for row in over.design_checks())

    orphan = Session.create("Orphan still")
    orphan.place("iec.contactors.aux_no", ref="K9-13", x=20, y=20)
    broken = Session.create("Broken still")
    broken.place(
        "iec.connection.offpage",
        ref="OP9",
        x=40,
        y=40,
        properties={"dest_sheet": "missing", "dest_ref": "K9"},
    )
    assert any(row["check"] == "contact_without_coil" for row in orphan.design_checks())
    assert any(row["check"] == "broken_offpage" for row in broken.design_checks())


def _error_on_four_surfaces(path: Path, *, zone: str | None = None, wire: str | None = None) -> list[str]:
    """Session, JSONL agent, CLI, and drawing-tool sentences for one bad query."""

    from types import SimpleNamespace

    from pydantic_ai import ModelRetry

    from panelcad.ai.drawing import _run, open_drawing
    from panelcad.core.xref_index import QueryError

    session = Session.open(path)
    if zone is not None:
        try:
            session.lookup_zone(zone)
            raise AssertionError("Session lookup_zone should have failed")
        except QueryError as exc:
            session_msg = str(exc)
    else:
        try:
            session.trace_wire("" if wire is None else wire)
            raise AssertionError("Session trace_wire should have failed")
        except QueryError as exc:
            session_msg = str(exc)

    agent = Agent()
    assert agent.handle({"id": 1, "op": "open", "path": str(path)})["ok"]
    if zone is not None:
        reply = agent.handle({"id": 2, "op": "lookup_zone", "zone": zone})
    else:
        reply = agent.handle({"id": 2, "op": "trace_wire", "number": "" if wire is None else wire})
    assert reply["ok"] is False
    assert "crash" not in reply
    agent_msg = str(reply.get("error") or "")

    runner = CliRunner()
    if zone is not None:
        shown = runner.invoke(app, ["electrical", "xref", str(path), "--zone", zone])
    else:
        shown = runner.invoke(app, ["electrical", "xref", str(path), "--wire", "" if wire is None else wire])
    assert shown.exit_code == 1, shown.output
    assert "Traceback" not in (shown.output or "")
    cli_msg = (shown.stderr or shown.output or "").strip().splitlines()[0]

    drawing = open_drawing(path)
    ctx = SimpleNamespace(deps=drawing)
    try:
        if zone is not None:
            _run(ctx, "lookup_zone", zone=zone)
        else:
            _run(ctx, "trace_wire", number="" if wire is None else wire)
        raise AssertionError("drawing tool should have failed")
    except ModelRetry as exc:
        draw_msg = str(exc)
    assert "crash_id" not in draw_msg
    return [session_msg, agent_msg, cli_msg, draw_msg]


def test_empty_wire_and_bad_zone_are_the_same_sentence_not_a_crash(tmp_path: Path):
    session = build_nema_seal_in()
    path = tmp_path / "query.pcad.yaml"
    session.save(path)
    from panelcad.diag.paths import crashes_dir

    empty = _error_on_four_surfaces(path, wire="")
    assert empty[0] == "wire number is required"
    assert empty == ["wire number is required"] * 4
    before = {item.name for item in crashes_dir().glob("*.json")}
    zone_msgs = _error_on_four_surfaces(path, zone="Z9")
    assert len(set(zone_msgs)) == 1
    assert zone_msgs[0]
    after = {item.name for item in crashes_dir().glob("*.json")}
    assert after == before


def test_trace_wire_uses_the_plc_pin_zone():
    from panelcad.core.xref import component_location, location_of
    from panelcad.render.sheet import pin_world

    session = Session.create("TracePLC")
    session.set_title_block(standard="IEC", drawing_style="iec")
    session.place("iec.plc.compact_16dio", ref="PLC1", x=40, y=40)
    session.place("iec.contactors.coil", ref="M1", x=200, y=40)
    session.wire("PLC1.Q0_0", "M1.A1", number="205")
    sheet = session.sheet()
    plc = sheet.component("PLC1")
    device = session.catalog.get(plc.library_id)
    origin = component_location(session.project, sheet, plc, device)
    px, py = pin_world(plc, device.pin("Q0_0"))
    pin_loc = location_of(session.project, sheet, px, py)
    point = next(
        row
        for row in session.device_index("PLC1")["elements"]
        if row.get("address") == "Q0_0" or row["ref"].endswith("Q0_0")
    )
    assert point["page_zone"] == f"{pin_loc.sheet_no}-{pin_loc.zone()}"
    traced = session.trace_wire("205")
    seg = next(item for item in traced["segments"] if "Q0_0" in item["from"] or "Q0_0" in item["to"])
    plc_zone = seg["from_page_zone"] if seg["from"].endswith("Q0_0") else seg["to_page_zone"]
    plc_loc = seg["from_location"] if seg["from"].endswith("Q0_0") else seg["to_location"]
    assert plc_zone == point["page_zone"]
    assert plc_loc == pin_loc.display("iec")
    assert plc_zone != f"{origin.sheet_no}-{origin.zone()}" or plc_loc != origin.display("iec")


def test_trace_wire_keeps_non_plc_end_on_its_index_row():
    """M1-13 on wire 205 stays on the contact row; PLC1.Q0_0 still matches plc_io."""

    from panelcad.core.xref import component_location, location_of
    from panelcad.core.zones import zone_grid
    from panelcad.render.sheet import pin_world

    session = Session.create("TraceIndexRow")
    session.set_title_block(standard="IEC", drawing_style="iec")
    session.place("iec.plc.compact_16dio", ref="PLC1", x=40, y=40)
    sheet = session.sheet()
    grid = zone_grid(sheet)
    # Origin in one column, pin 14 (+28 mm) in the next — the measured 2-A9 / 2-A10 drift.
    session.place("iec.contactors.aux_no", ref="M1-13", x=grid.wx + grid.cw - 4.0, y=40)
    session.wire("PLC1.Q0_0", "M1-13.14", number="205")

    contact = sheet.component("M1-13")
    device = session.catalog.get(contact.library_id)
    symbol = component_location(session.project, sheet, contact, device)
    px, py = pin_world(contact, device.pin("14"))
    pin_loc = location_of(session.project, sheet, px, py)
    assert f"{symbol.sheet_no}-{symbol.zone()}" != f"{pin_loc.sheet_no}-{pin_loc.zone()}"

    contact_row = next(row for row in session.device_index("M1")["elements"] if row["ref"] == "M1-13")
    assert contact_row["element"] != "plc_io"
    assert contact_row["page_zone"] == f"{symbol.sheet_no}-{symbol.zone()}"

    plc_row = next(row for row in session.device_index("PLC1")["elements"] if row.get("address") == "Q0_0")
    assert plc_row["element"] == "plc_io"

    traced = session.trace_wire("205")
    seg = traced["segments"][0]
    if seg["from_pin"] == "Q0_0":
        plc_zone, contact_zone = seg["from_page_zone"], seg["to_page_zone"]
        plc_loc, contact_loc = seg["from_location"], seg["to_location"]
    else:
        plc_zone, contact_zone = seg["to_page_zone"], seg["from_page_zone"]
        plc_loc, contact_loc = seg["to_location"], seg["from_location"]
    assert contact_zone == contact_row["page_zone"]
    assert contact_loc == contact_row["location"]
    assert contact_zone != f"{pin_loc.sheet_no}-{pin_loc.zone()}"
    assert plc_zone == plc_row["page_zone"]
    assert plc_loc == plc_row["location"]


def test_trace_wire_selects_plc_pin_by_exact_id():
    """Q0_1 and Q0_10 on one module trace to their own plc_io zones."""

    from panelcad.core.models import PinDef
    from panelcad.core.xref import location_of
    from panelcad.render.sheet import pin_world

    session = Session.create("ExactPlcPin")
    session.set_title_block(standard="IEC", drawing_style="iec")
    plc_dev = session.catalog.get("iec.plc.compact_16dio")
    plc_dev.pins.append(PinDef(id="Q0_10", x=250, y=40, side="right", electrical="output"))
    session.place("iec.plc.compact_16dio", ref="PLC1", x=40, y=40)
    session.place("iec.contactors.coil", ref="K1", x=120, y=40)
    session.place("iec.contactors.coil", ref="K2", x=120, y=140)
    session.wire("PLC1.Q0_1", "K1.A1", number="301")
    session.wire("PLC1.Q0_10", "K2.A1", number="310")

    sheet = session.sheet()
    plc = sheet.component("PLC1")
    q1 = location_of(session.project, sheet, *pin_world(plc, plc_dev.pin("Q0_1")))
    q10 = location_of(session.project, sheet, *pin_world(plc, plc_dev.pin("Q0_10")))
    assert q1.zone() != q10.zone()

    points = session.device_index("PLC1")["elements"]
    row1 = next(row for row in points if row.get("address") == "Q0_1")
    row10 = next(row for row in points if row.get("address") == "Q0_10")
    assert row1["page_zone"] != row10["page_zone"]

    def _plc_end(number: str, pin: str) -> dict[str, str]:
        traced = session.trace_wire(number)
        seg = next(item for item in traced["segments"] if item["from_pin"] == pin or item["to_pin"] == pin)
        if seg["from_pin"] == pin:
            return {"page_zone": seg["from_page_zone"], "location": seg["from_location"]}
        return {"page_zone": seg["to_page_zone"], "location": seg["to_location"]}

    end1 = _plc_end("301", "Q0_1")
    end10 = _plc_end("310", "Q0_10")
    assert end1["page_zone"] == row1["page_zone"]
    assert end1["location"] == row1["location"]
    assert end10["page_zone"] == row10["page_zone"]
    assert end10["location"] == row10["location"]
    assert end1["page_zone"] != row10["page_zone"]
    assert end10["page_zone"] != row1["page_zone"]
