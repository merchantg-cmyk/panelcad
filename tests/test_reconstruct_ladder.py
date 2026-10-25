"""End-to-end tests for clean as-built NEMA ladder synthesis from panel photos."""

from __future__ import annotations

from pathlib import Path

from panelcad.api.session import Session
from panelcad.importer.ladder_synth import check_converted_sheet_layout
from panelcad.reconstruct.placeholder import project_has_needs_info
from panelcad.reconstruct.service import (
    apply_to_print,
    record_wire_landing,
    synthesize_reconstruct_schematic,
)
from panelcad.reconstruct.vision import FakeVision
from panelcad.reconstruct.fixtures import write_backpanel_photo


def _build_photo_reconstruct_session(tmp_path: Path) -> Session:
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    photo_path = tmp_path / "front_open.png"
    meta = write_backpanel_photo(photo_path)
    session = Session.create("AsBuiltStarter")
    added = session.reconstruct_add_photos([photo_path], role="backpanel")
    photo_id = added["added"][0]["id"]
    corners = meta["corners"]
    session.reconstruct_set_corners(photo_id, corners)
    session.reconstruct_set_scale(photo_id, "face", meta["corners"][0], meta["corners"][1], known_mm=meta.get("enclosure_width_mm") or 400, known_height_mm=meta.get("enclosure_height_mm") or 500)

    recordings = {
        "p1": {
            "devices": [
                {
                    "tag": "CB1",
                    "part_number": "5SY4332-7",
                    "manufacturer": "Siemens",
                    "category": "breaker",
                    "confidence": 0.96,
                    "x": 200, "y": 140, "w": 90, "h": 140,
                },
                {
                    "tag": "M1",
                    "part_number": "MC-9b",
                    "manufacturer": "LS Electric",
                    "category": "contactor",
                    "confidence": 0.96,
                    "x": 340, "y": 140, "w": 90, "h": 140,
                },
                {
                    "tag": "OL1",
                    "part_number": "MT-32",
                    "manufacturer": "LS Electric",
                    "category": "overload",
                    "confidence": 0.95,
                    "x": 470, "y": 140, "w": 90, "h": 140,
                },
                {
                    "tag": "S1",
                    "part_number": "XB4-BA42",
                    "manufacturer": "Schneider",
                    "category": "operator",
                    "confidence": 0.93,
                    "x": 620, "y": 140, "w": 60, "h": 60,
                },
                {
                    "tag": "S2",
                    "part_number": "XB4-BA31",
                    "manufacturer": "Schneider",
                    "category": "operator",
                    "confidence": 0.93,
                    "x": 700, "y": 140, "w": 60, "h": 60,
                },
                {
                    "tag": "PL1",
                    "part_number": "XB4-BVB3",
                    "manufacturer": "Schneider",
                    "category": "pilot_light",
                    "confidence": 0.92,
                    "x": 780, "y": 140, "w": 60, "h": 60,
                },
            ],
            "terminal_strips": [],
            "texts": [],
        }
    }
    session.reconstruct_identify(vision=FakeVision(recordings))
    items_by_tag = {
        row["tag"]: row["id"]
        for row in session.reconstruct_status()["items"]
        if row["role"] == "part" and row.get("tag")
    }
    session.reconstruct_confirm_item(
        items_by_tag["CB1"],
        envelope_w_mm=54.0,
        envelope_h_mm=80.0,
        envelope_d_mm=70.0,
        mount="din_rail",
        tag="CB1",
        library_id="iec.breakers.mcb_3p_32a",
    )
    session.reconstruct_confirm_item(items_by_tag["M1"], catalog="MC-9b", tag="M1")
    session.reconstruct_confirm_item(
        items_by_tag["OL1"],
        envelope_w_mm=45.0,
        envelope_h_mm=75.0,
        envelope_d_mm=80.0,
        mount="din_rail",
        tag="OL1",
        library_id="iec.protection.overload_9a",
    )
    session.reconstruct_confirm_item(
        items_by_tag["S1"],
        envelope_w_mm=30.0,
        envelope_h_mm=40.0,
        envelope_d_mm=55.0,
        mount="door",
        tag="S1",
        library_id="nema.operators.pb_nc",
    )
    session.reconstruct_confirm_item(
        items_by_tag["S2"],
        envelope_w_mm=30.0,
        envelope_h_mm=40.0,
        envelope_d_mm=55.0,
        mount="door",
        tag="S2",
        library_id="nema.operators.pb_no",
    )
    session.reconstruct_confirm_item(
        items_by_tag["PL1"],
        envelope_w_mm=30.0,
        envelope_h_mm=40.0,
        envelope_d_mm=55.0,
        mount="door",
        tag="PL1",
        library_id="nema.operators.pilot_light",
    )
    return session


def test_reconstruct_end_to_end_ladder_and_power_separation(tmp_path: Path):
    session = _build_photo_reconstruct_session(tmp_path)

    # Record control ladder wire landings (Rung 1: Stop/Start/Seal-in -> M1 coil -> OL1 NC -> L2; Rung 2: aux -> PL1 -> L2)
    control_landings = [
        ("101", "L1", "", "S1", "1"),
        ("102", "S1", "2", "S2", "3"),
        ("103", "S2", "4", "M1", "A1"),
        ("102A", "M1-13", "13", "S2", "3"),
        ("103A", "M1-13", "14", "M1", "A1"),
        ("104", "M1", "A2", "OL1", "95"),
        ("105", "OL1", "96", "L2", ""),
        ("201", "L1", "", "M1-23", "13"),
        ("202", "M1-23", "14", "PL1", "X1"),
        ("203", "PL1", "X2", "L2", ""),
    ]
    for wnum, f_tag, f_pin, t_tag, t_pin in control_landings:
        record_wire_landing(
            session,
            wire_number=wnum,
            from_tag=f_tag,
            from_pin=f_pin,
            to_tag=t_tag,
            to_pin=t_pin,
        )

    # Record 3-phase power wires (CB1 -> M1 -> OL1 -> MTR1)
    power_landings = [
        ("L1", "CB1", "2", "M1", "1"),
        ("L2", "CB1", "4", "M1", "3"),
        ("L3", "CB1", "6", "M1", "5"),
        ("T1", "M1", "2", "OL1", "L1"),
        ("T2", "M1", "4", "OL1", "L2"),
        ("T3", "M1", "6", "OL1", "L3"),
        ("M1_T1", "OL1", "T1", "MTR1", "T1"),
        ("M1_T2", "OL1", "T2", "MTR1", "T2"),
        ("M1_T3", "OL1", "T3", "MTR1", "T3"),
    ]
    for wnum, f_tag, f_pin, t_tag, t_pin in power_landings:
        record_wire_landing(
            session,
            wire_number=wnum,
            from_tag=f_tag,
            from_pin=f_pin,
            to_tag=t_tag,
            to_pin=t_pin,
        )

    result = apply_to_print(session)
    assert result["schematic_sheet"] == "asbuilt"
    assert result["power_sheet"] == "asbuilt_power"
    assert result["rungs"] >= 2

    from panelcad.core.ladder import (
        is_iec_column_sheet,
        is_ladder_sheet,
        rail_geometry,
    )

    asbuilt = session.project.sheet("asbuilt")
    assert asbuilt.drawing_style == "ladder"
    assert asbuilt.rail_hot == "L1"
    assert asbuilt.rail_return == "L2"

    pwr = session.project.sheet("asbuilt_power")
    assert pwr.drawing_style == "freeform"
    assert pwr.rail_hot == ""
    assert pwr.rail_return == ""
    assert rail_geometry(pwr, session.project) is None
    assert not is_ladder_sheet(pwr, session.project)
    assert not is_iec_column_sheet(pwr, session.project)

    # Distinct rung y coordinates (not flat y = 40.0)
    comp_map = {c.id: c for c in asbuilt.components}
    assert not all(abs(c.y - 40.0) < 1e-6 for c in asbuilt.components)
    assert abs(comp_map["S1"].y - comp_map["M1-23"].y) >= 35.0
    assert abs(comp_map["M1"].y - comp_map["PL1"].y) >= 35.0
    assert abs(comp_map["M1-13"].y - comp_map["S2"].y) >= 15.0

    # Coils/lights placed to the right of contacts on their rung
    assert comp_map["S1"].x < comp_map["S2"].x < comp_map["M1"].x < comp_map["OL1"].x
    assert comp_map["M1-23"].x < comp_map["PL1"].x

    # 100% From-To netlist preservation across control and power sheets
    all_recorded = {
        (w.number, w.frm, w.to)
        for s in (session.project.sheet("asbuilt"), session.project.sheet("asbuilt_power"))
        for w in s.wires
    }
    expected_control = {
        ("101", "L1", "S1.1"),
        ("102", "S1.2", "S2.3"),
        ("103", "S2.4", "M1.A1"),
        ("102A", "M1-13.13", "S2.3"),
        ("103A", "M1-13.14", "M1.A1"),
        ("104", "M1.A2", "OL1.95"),
        ("105", "OL1.96", "L2"),
        ("201", "L1", "M1-23.13"),
        ("202", "M1-23.14", "PL1.X1"),
        ("203", "PL1.X2", "L2"),
    }
    expected_power = {
        ("L1", "CB1.2", "M1.1"),
        ("L2", "CB1.4", "M1.3"),
        ("L3", "CB1.6", "M1.5"),
        ("T1", "M1.2", "OL1.L1"),
        ("T2", "M1.4", "OL1.L2"),
        ("T3", "M1.6", "OL1.L3"),
        ("M1_T1", "OL1.T1", "MTR1.T1"),
        ("M1_T2", "OL1.T2", "MTR1.T2"),
        ("M1_T3", "OL1.T3", "MTR1.T3"),
    }
    assert expected_control <= all_recorded
    assert expected_power <= all_recorded

    # Zero validation errors and zero layout collisions
    assert session.validate() == []
    assert check_converted_sheet_layout(session) == []


def test_reconstruct_unconfirmed_parts_keep_not_for_build_and_wizard_rebuild(tmp_path: Path):
    from PySide6.QtWidgets import QApplication
    from panelcad.gui.reconstruct_wizard import ReconstructWizard

    _app = QApplication.instance() or QApplication([])
    photo_path = tmp_path / "front_open.png"
    meta = write_backpanel_photo(photo_path)
    session = Session.create("NeedsInfoHold")
    added = session.reconstruct_add_photos([photo_path], role="backpanel")
    photo_id = added["added"][0]["id"]
    session.reconstruct_set_corners(photo_id, meta["corners"])
    session.reconstruct_set_scale(photo_id, "face", meta["corners"][0], meta["corners"][1], known_mm=meta.get("enclosure_width_mm") or 400, known_height_mm=meta.get("enclosure_height_mm") or 500)

    recordings = {
        "p1": {
            "devices": [
                {
                    "tag": "M1",
                    "part_number": "MC-9b",
                    "manufacturer": "LS Electric",
                    "category": "contactor",
                    "confidence": 0.95,
                    "x": 240, "y": 140, "w": 90, "h": 140,
                },
                {
                    "tag": "U1",
                    "part_number": "UNKNOWN-BOX-99",
                    "manufacturer": "",
                    "category": "plc",
                    "confidence": 0.40,
                    "x": 450, "y": 140, "w": 110, "h": 140,
                },
            ],
            "terminal_strips": [],
            "texts": [],
        }
    }
    session.reconstruct_identify(vision=FakeVision(recordings))
    items_by_tag = {
        row["tag"]: row["id"]
        for row in session.reconstruct_status()["items"]
        if row["role"] == "part" and row.get("tag")
    }
    session.reconstruct_confirm_item(items_by_tag["M1"], catalog="MC-9b", tag="M1")
    # U1 remains unconfirmed -> must keep not_for_build safety hold
    session.reconstruct_record_wire_landing("101", from_tag="L1", to_tag="S1", to_pin="1")
    session.reconstruct_record_wire_landing("102", from_tag="S1", from_pin="2", to_tag="M1", to_pin="A1")
    session.reconstruct_record_wire_landing("103", from_tag="M1", from_pin="A2", to_tag="L2")

    out = apply_to_print(session)
    assert out["not_for_build"] is True
    assert project_has_needs_info(session.project) is True

    wiz = ReconstructWizard(session)
    wiz._refresh_steps()
    assert "3 traced" in wiz.wire_schedule_summary.text()
    assert "S1.2 -> M1.A1" in wiz.wire_schedule_summary.text()
    rebuilt = wiz._rebuild_schematic()
    assert rebuilt["rungs"] == 1
    assert check_converted_sheet_layout(session) == []


def test_asbuilt_power_renders_zero_l1_l2_rails_and_flags_forced_rails(tmp_path: Path):
    import xml.etree.ElementTree as ET
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    from panelcad.core.ladder import (
        iec_rail_ys,
        rail_geometry,
    )
    from panelcad.render.svg import sheet_to_svg
    from panelcad.render.titleblock import working_rect

    session = _build_photo_reconstruct_session(tmp_path)
    for wnum, f_tag, f_pin, t_tag, t_pin in [
        ("101", "L1", "", "S1", "1"),
        ("102", "S1", "2", "S2", "3"),
        ("103", "S2", "4", "M1", "A1"),
        ("104", "M1", "A2", "OL1", "95"),
        ("105", "OL1", "96", "L2", ""),
        ("L1", "CB1", "2", "M1", "1"),
        ("L2", "CB1", "4", "M1", "3"),
        ("L3", "CB1", "6", "M1", "5"),
        ("T1", "M1", "2", "OL1", "L1"),
        ("T2", "M1", "4", "OL1", "L2"),
        ("T3", "M1", "6", "OL1", "L3"),
        ("M1_T1", "OL1", "T1", "MTR1", "T1"),
        ("M1_T2", "OL1", "T2", "MTR1", "T2"),
        ("M1_T3", "OL1", "T3", "MTR1", "T3"),
    ]:
        record_wire_landing(
            session,
            wire_number=wnum,
            from_tag=f_tag,
            from_pin=f_pin,
            to_tag=t_tag,
            to_pin=t_pin,
        )
    apply_to_print(session)

    pwr = session.project.sheet("asbuilt_power")
    assert pwr.drawing_style == "freeform"
    assert pwr.rail_hot == ""
    assert pwr.rail_return == ""
    assert rail_geometry(pwr, session.project) is None
    assert check_converted_sheet_layout(session) == []

    # Verify top-of-feeder L1/L2/L3 supply callouts and terminal caps are present above CB1
    from panelcad.render.phase_ink import line_side_callouts

    callouts = line_side_callouts(session.project, pwr, session.catalog)
    assert [c.text for c in callouts] == ["L1", "L2", "L3"]
    assert all(c.cap is not None for c in callouts)

    # Verify SVG has zero horizontal IEC rails and zero vertical NEMA ladder rails
    wx, wy, ww, wh = working_rect(pwr)
    svg = sheet_to_svg(pwr, session.catalog, project=session.project)
    root_el = ET.fromstring(svg)
    top_supply_texts = [
        (el.text or "").strip()
        for el in root_el.iter()
        if el.tag.endswith("text") and float(el.attrib.get("y", "999")) <= wy + 4.0
    ]
    assert {"L1", "L2", "L3"} <= set(top_supply_texts), top_supply_texts
    for el in root_el.iter():
        if el.tag.endswith("line"):
            x1 = float(el.attrib.get("x1", "0"))
            y1 = float(el.attrib.get("y1", "0"))
            x2 = float(el.attrib.get("x2", "0"))
            y2 = float(el.attrib.get("y2", "0"))
            assert not (
                abs(y1 - y2) < 0.5
                and abs(x2 - x1) >= 0.7 * ww
                and wy + 2.0 <= y1 <= wy + wh - 2.0
            ), f"Unexpected horizontal rail line in SVG: ({x1}, {y1}) -> ({x2}, {y2})"
            assert not (
                abs(x1 - x2) < 0.5
                and abs(y2 - y1) >= 0.7 * wh
                and wx + 2.0 <= x1 <= wx + ww - 2.0
            ), f"Unexpected vertical ladder rail line in SVG: ({x1}, {y1}) -> ({x2}, {y2})"

    # Verify PDF export succeeds cleanly
    pdf_path = tmp_path / "asbuilt_out.pdf"
    session.export_pdf(pdf_path)
    assert pdf_path.is_file() and pdf_path.stat().st_size > 1000

    # Verify rendered PNG has no top L1 rail or bottom L2 rail line across the sheet
    png_path = tmp_path / "asbuilt_power.png"
    pw, ph = pwr.page.dimensions()
    img_w, img_h = 1680, 1080
    renderer = QSvgRenderer(svg.encode("utf-8"))
    img = QImage(img_w, img_h, QImage.Format_ARGB32)
    img.fill(QColor("white"))
    painter = QPainter(img)
    renderer.render(painter)
    painter.end()
    img.save(str(png_path))
    assert png_path.is_file()

    iec_top_mm, iec_bot_mm = iec_rail_ys(pwr)
    top_rail_y_px = int(round((iec_top_mm / ph) * img_h))
    bot_rail_y_px = int(round((iec_bot_mm / ph) * img_h))
    for y_px in (top_rail_y_px, bot_rail_y_px):
        dark_pixels = 0
        for x_mm in range(160, 350, 5):
            x_px = int(round((x_mm / pw) * img_w))
            c = img.pixelColor(x_px, y_px)
            if c.red() < 80 and c.green() < 80 and c.blue() < 80:
                dark_pixels += 1
        assert dark_pixels == 0, f"Found {dark_pixels} dark rail pixels along y_px={y_px}"

    # Verify check_converted_sheet_layout flags power_sheet_has_ladder_rails when rails are forced
    for bad_style, bad_hot, bad_ret in [
        ("iec", "", ""),
        ("ladder", "L1", "L2"),
        ("freeform", "L1", "L2"),
    ]:
        broken = Session(session.project.model_copy(deep=True))
        b_pwr = broken.project.sheet("asbuilt_power")
        b_pwr.drawing_style = bad_style  # type: ignore[assignment]
        b_pwr.rail_hot = bad_hot
        b_pwr.rail_return = bad_ret
        flags = [
            f
            for f in check_converted_sheet_layout(broken)
            if f.get("sheet_id") == "asbuilt_power"
        ]
        flag_names = {f["flag"] for f in flags}
        assert "power_sheet_has_ladder_rails" in flag_names, (
            bad_style,
            bad_hot,
            bad_ret,
            flag_names,
        )

    # Verify check_converted_sheet_layout flags missing_three_phase_supply_callouts if callouts are suppressed
    import panelcad.render.phase_ink as _pi

    orig_callouts = _pi.line_side_callouts
    try:
        _pi.line_side_callouts = lambda *_a, **_kw: []
        flags_no_callouts = [
            f
            for f in check_converted_sheet_layout(session)
            if f.get("sheet_id") == "asbuilt_power"
        ]
        assert "missing_three_phase_supply_callouts" in {f["flag"] for f in flags_no_callouts}
    finally:
        _pi.line_side_callouts = orig_callouts
