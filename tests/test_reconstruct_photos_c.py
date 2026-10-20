"""Reconstruct-from-photos fixes for the photo scale, trace, and identify bugs."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from panelcad import Session
from panelcad.core.checks import run_checks
from panelcad.gui.problems import _CHECK_LABELS
from panelcad.gui.reconstruct_wizard import ReconstructWizard
from panelcad.io.bom import instance_rows
from panelcad.library.catalog import default_catalog
from panelcad.library.parts import default_parts_catalog
from panelcad.reconstruct.fixtures import write_backpanel_photo
from panelcad.reconstruct.geometry import mm_per_px, scale_payload
from panelcad.reconstruct.match import match_catalog
from panelcad.reconstruct.models import PERSPECTIVE_SCALE_FRACTION, Photo, ScaleReference
from panelcad.reconstruct.vision import NO_AI_MESSAGE, FakeVision, StubVision


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _session(tmp_path: Path) -> tuple[Session, dict]:
    _app()
    shot = tmp_path / "backpanel.png"
    meta = write_backpanel_photo(shot)
    job = tmp_path / "asbuilt.pcad.yaml"
    session = Session.create("Photos")
    session.save(job)
    session = Session.open(job)
    session.reconstruct_add_photos([shot], role="backpanel")
    return session, meta


def _recordings(meta: dict, **extra) -> dict:
    region = meta["part_region"]
    w = float(meta["width_px"])
    h = float(meta["height_px"])
    device = {
        "tag": extra.get("tag", "K1"),
        "brand": extra.get("brand", "Acme"),
        "part_number": extra.get("part_number", "LABEL-100"),
        "label_text": extra.get("label_text", extra.get("part_number", "LABEL-100")),
        "nx": region["x"] / w,
        "ny": region["y"] / h,
        "nw": region["w"] / w,
        "nh": region["h"] / h,
        "confidence": 0.9,
    }
    return {
        "p1": {
            "devices": [device],
            "strips": extra.get("strips", []),
            "unreadable": False,
        }
    }


def test_two_axis_scale_allows_normal_perspective():
    photo = Photo(id="p1", path="p.jpg", width_px=800, height_px=600)
    photo.corners = []
    # 63 px vertical = 35 mm. 660 px horizontal claimed a few percent off 35/63.
    photo.scale = ScaleReference(kind="din_rail", known_mm=35, p1_x=100, p1_y=100, p2_x=100, p2_y=163)
    photo.scale_b = ScaleReference(kind="measured", known_mm=350, p1_x=100, p1_y=100, p2_x=760, p2_y=100)
    assert mm_per_px(photo) is not None
    assert photo.scale_error_mm is not None
    assert photo.scale_error_mm > 8
    longer = max(35 / 63, 350 / 660) * 800
    assert photo.scale_error_mm / longer <= PERSPECTIVE_SCALE_FRACTION


def _axis_photo(*, relative: float | None = None, error_mm: float | None = None) -> Photo:
    """800 px on x, 600 px on y, sy = 1 mm/px. The two marks are perpendicular."""

    span = 800.0
    if relative is not None:
        sx = 1.0 - relative
    else:
        sx = 1.0 - float(error_mm) / span
    photo = Photo(id="p1", path="p.jpg", width_px=800, height_px=600)
    photo.scale = ScaleReference(
        kind="measured", known_mm=sx * span, p1_x=0, p1_y=100, p2_x=span, p2_y=100
    )
    photo.scale_b = ScaleReference(
        kind="measured", known_mm=600, p1_x=100, p1_y=0, p2_x=100, p2_y=600
    )
    return photo


def test_perspective_allowance_warns_inside_12_percent_and_refuses_20():
    for fraction in (0.03, 0.10):
        payload = scale_payload(_axis_photo(relative=fraction))
        assert payload["scaled"] is True
        assert "differ by" in payload["message"]
        assert "normal for a photo" in payload["message"]
        assert f"({int(round(fraction * 100))}%)" in payload["message"]
    quiet = scale_payload(_axis_photo(error_mm=6))
    assert quiet["scaled"] is True
    assert quiet["message"] == ""
    assert quiet["error_mm"] <= 8
    refused = scale_payload(_axis_photo(relative=0.20))
    assert refused["scaled"] is False
    assert "disagree" in refused["message"].lower()


def test_perspective_warning_is_one_problems_row_and_shows_on_the_scale_page(tmp_path: Path, monkeypatch):
    from PySide6.QtGui import QColor, QImage

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    _app()
    image = QImage(800, 600, QImage.Format.Format_RGB32)
    image.fill(QColor("#778899"))
    shot = tmp_path / "angle.png"
    image.save(str(shot), "PNG")
    job = tmp_path / "angle.pcad.yaml"
    session = Session.create("Angle")
    session.save(job)
    session = Session.open(job)
    session.reconstruct_add_photos([shot], role="backpanel")
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    session.reconstruct_set_corners(photo_id, [(0, 0), (800, 0), (800, 600), (0, 600)])
    session.reconstruct_set_scale(photo_id, "measured", (0, 100), (800, 100), known_mm=720)
    payload = session.reconstruct_set_scale(photo_id, "measured", (100, 0), (100, 600), known_mm=600)
    assert payload["scaled"] is True
    assert "differ by" in payload["message"]
    rows = [row for row in run_checks(session.project, session.catalog) if row.check == "reconstruct_scale_perspective"]
    assert len(rows) == 1
    assert rows[0].severity == "info"
    assert rows[0].message == payload["message"]
    dialog = ReconstructWizard(session)
    dialog.stack.setCurrentIndex(1)
    dialog.corners.setText("0,0 800,0 800,600 0,600")
    for index in range(dialog.scale_kind.count()):
        if dialog.scale_kind.itemData(index) == "measured":
            dialog.scale_kind.setCurrentIndex(index)
            break
    dialog.p1.setText("0,100")
    dialog.p2.setText("800,100")
    dialog.known.setValue(720)
    dialog.p3.setText("100,0")
    dialog.p4.setText("100,600")
    dialog.known_height.setValue(600)
    dialog._apply_scale()
    assert "normal for a photo" in dialog.scale_status.text()


def test_scale_refusal_names_the_disagreement(tmp_path: Path):
    session, meta = _session(tmp_path)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    session.reconstruct_set_corners(photo_id, meta["corners"])
    session.reconstruct_set_scale(photo_id, "measured", meta["corners"][0], meta["corners"][1], known_mm=400)
    payload = session.reconstruct_set_scale(
        photo_id, "measured", meta["corners"][0], meta["corners"][3], known_mm=40
    )
    assert payload["scaled"] is False
    assert "disagree" in str(payload.get("message") or "").lower()
    assert "mm" in str(payload.get("message") or "")
    assert mm_per_px(session.project.reconstruct.photo(photo_id)) is None


def test_one_din_rail_mark_says_why(tmp_path: Path):
    session, meta = _session(tmp_path)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    payload = session.reconstruct_set_scale(photo_id, "din_rail", meta["rail_p1"], meta["rail_p2"])
    assert payload["scaled"] is False
    assert "one length" in str(payload.get("message") or "").lower()


def test_scale_page_shows_the_photo_and_takes_clicks(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    session, _meta = _session(tmp_path)
    dialog = ReconstructWizard(session)
    dialog.stack.setCurrentIndex(1)
    assert dialog.scale_view._pix.isNull() is False
    assert dialog.scale_kind.itemData(0) == "face"
    dialog._set_scale_mode("len1")
    dialog.scale_view.mark_photo(12, 34)
    dialog.scale_view.mark_photo(80, 34)
    assert dialog.p1.text().startswith("12")
    assert dialog.p2.text().startswith("80")
    dialog._set_scale_mode("corners")
    dialog.corners.clear()
    for point in ((1, 2), (3, 4), (5, 6), (7, 8)):
        dialog.scale_view.mark_photo(*point)
    assert "1.0,2.0" in dialog.corners.text()
    assert dialog.corners.text().count(",") == 4


def test_parts_sit_on_the_measured_enclosure(tmp_path: Path):
    session, meta = _session(tmp_path)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta, part_number="MC-9b", brand="LS Electric")))
    item = next(row for row in session.reconstruct_status()["items"] if row["role"] == "part")
    session.reconstruct_confirm_item(item["id"], catalog="MC-9b", manufacturer="LS Electric", tag="K1")
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    session.reconstruct_set_corners(photo_id, meta["corners"])
    session.reconstruct_set_scale(
        photo_id,
        "backpanel",
        meta["corners"][0],
        meta["corners"][1],
        known_mm=meta["enclosure_width_mm"],
        known_height_mm=meta["enclosure_height_mm"],
    )
    placed = session.reconstruct_place_confirmed()
    assert placed["placed"]
    sheet = session.project.sheet(placed["placed"][0]["sheet"])
    enc = sheet.enclosure
    assert enc is not None
    assert abs(enc.width_mm - meta["enclosure_width_mm"]) < 1
    assert abs(enc.height_mm - meta["enclosure_height_mm"]) < 1
    assert sheet.rails
    assert abs(sheet.rails[0].length_mm - enc.width_mm) < 1
    x, y = placed["placed"][0]["x"], placed["placed"][0]["y"]
    assert x >= enc.x - 0.1
    assert y >= enc.y - 0.1
    assert x < enc.x + enc.width_mm
    assert y < enc.y + enc.height_mm


def test_junk_trace_is_rejected_and_a_real_pin_draws_the_wire(tmp_path: Path):
    session, meta = _session(tmp_path)
    strips = [
        {
            "tag": "X1",
            "terminals": [{"id": "1", "nx": 0.55, "ny": 0.35}],
            "wire_numbers": [{"number": "104", "terminal": "1", "nx": 0.45, "ny": 0.52}],
        }
    ]
    session.reconstruct_identify(
        vision=FakeVision(_recordings(meta, part_number="MC-9b", brand="LS Electric", strips=strips))
    )
    item = next(row for row in session.reconstruct_status()["items"] if row["role"] == "part")
    session.reconstruct_confirm_item(item["id"], catalog="MC-9b", tag="K1")
    trace = next(step for step in session.reconstruct_checklist()["checklist"] if step["kind"] == "trace_wire")
    junk = session.reconstruct_answer(trace["id"], "somewhere near the top")
    assert junk["status"] == "open"
    assert "K1:A1" in str(junk.get("message") or "")
    assert not any(wire.inferred for sheet in session.project.sheets for wire in sheet.wires)
    answered = session.reconstruct_answer(trace["id"], "K1:A1")
    assert answered["status"] == "answered"
    wires = [wire for sheet in session.project.sheets for wire in sheet.wires if wire.inferred]
    assert wires
    assert wires[0].caption().startswith("?")
    assert any(step["kind"] == "confirm_connection" for step in session.reconstruct_checklist()["checklist"])


def test_trace_does_not_invent_a_terminal_for_an_envelope(tmp_path: Path):
    session, meta = _session(tmp_path)
    strips = [
        {
            "tag": "X1",
            "terminals": [{"id": "1", "nx": 0.5, "ny": 0.4}],
            "wire_numbers": [{"number": "101", "nx": 0.52, "ny": 0.42}],
        }
    ]
    session.reconstruct_identify(
        vision=FakeVision(_recordings(meta, tag="A9", part_number="LABEL-100", brand="Acme", strips=strips))
    )
    item = next(row for row in session.reconstruct_status()["items"] if row["role"] == "part")
    session.reconstruct_confirm_item(
        item["id"],
        envelope_w_mm=40,
        envelope_h_mm=80,
        envelope_d_mm=70,
        mount="din_rail",
        tag="A9",
    )
    trace = next(step for step in session.reconstruct_checklist()["checklist"] if step["kind"] == "trace_wire")
    answered = session.reconstruct_answer(trace["id"], "A9:24V")
    assert answered["status"] == "answered"
    terminals = [
        comp
        for sheet in session.project.sheets
        for comp in sheet.components
        if comp.library_id == "iec.terminals.x_2p5" and comp.id == "A9"
    ]
    assert terminals == []
    envelopes = [
        comp
        for sheet in session.project.sheets
        for comp in sheet.components
        if comp.id == "A9" and comp.library_id == "reconstruct.measured_envelope"
    ]
    assert envelopes
    wires = [wire for sheet in session.project.sheets for wire in sheet.wires if wire.inferred]
    assert wires
    assert any("A9" in wire.frm or "A9" in wire.to for wire in wires)


def test_envelope_confirm_keeps_the_label_part_number(tmp_path: Path):
    session, meta = _session(tmp_path)
    session.reconstruct_identify(
        vision=FakeVision(_recordings(meta, tag="K9", part_number="LABEL-100", brand="Acme"))
    )
    item = next(row for row in session.reconstruct_status()["items"] if row["role"] == "part")
    confirmed = session.reconstruct_confirm_item(
        item["id"],
        envelope_w_mm=45,
        envelope_h_mm=80,
        envelope_d_mm=90,
        mount="din_rail",
        tag="K9",
    )
    assert confirmed["part_number"] == "LABEL-100"
    assert confirmed["brand"] == "Acme"
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    session.reconstruct_set_corners(photo_id, meta["corners"])
    session.reconstruct_set_scale(
        photo_id,
        "face",
        meta["corners"][0],
        meta["corners"][1],
        known_mm=meta["enclosure_width_mm"],
        known_height_mm=meta["enclosure_height_mm"],
    )
    session.reconstruct_place_confirmed()
    rows = instance_rows(session.project, session.catalog)
    hit = next(row for row in rows if row["ref"] == "K9")
    assert hit["manufacturer"] == "Acme"
    assert hit["part_number"] == "LABEL-100"


def test_match_includes_a_library_device_with_published_size():
    catalog = default_catalog()
    chosen = None
    for device in catalog.devices.values():
        if str(device.library_id).startswith("reconstruct."):
            continue
        props = device.properties or {}
        number = str(props.get("part_number") or props.get("catalog") or "").strip()
        panel = device.panel
        if (
            number
            and str(panel.dim_source) == "catalog"
            and float(panel.width_mm or 0) > 0
            and float(panel.height_mm or 0) > 0
            and float(panel.depth_mm or 0) > 0
        ):
            chosen = device
            break
    assert chosen is not None
    number = str((chosen.properties or {}).get("part_number") or chosen.properties.get("catalog"))
    hits = match_catalog(default_parts_catalog(), part_number=number, devices=catalog)
    assert hits
    assert hits[0].library_id == chosen.library_id
    assert hits[0].width_mm == pytest.approx(chosen.panel.width_mm)
    assert hits[0].depth_mm == pytest.approx(chosen.panel.depth_mm)


def test_pdf_old_print_is_read_and_a_broken_pdf_is_not(tmp_path: Path):
    import pymupdf

    session, meta = _session(tmp_path)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta, tag="K1", part_number="MC-9b", brand="LS Electric")))
    item = next(row for row in session.reconstruct_status()["items"] if row["role"] == "part")
    session.reconstruct_confirm_item(item["id"], catalog="MC-9b", tag="K1")
    pdf = tmp_path / "old.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=1190, height=842)
    page.insert_text((300, 300), "K1", fontsize=18)
    page.insert_text((700, 300), "Q1", fontsize=18)
    page.insert_text((500, 340), "104", fontsize=10)
    doc.save(str(pdf))
    doc.close()
    compared = session.reconstruct_compare_print(pdf)
    assert compared["extracted"] is True
    assert compared["extractor"]
    checks = {row["check"] for row in compared["mismatches"]}
    assert "reconstruct_print_unextracted" not in checks
    assert checks

    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\nnot a real pdf")
    session.project.reconstruct.prints.clear()
    attached = session.reconstruct_attach_print(broken)
    assert attached["extracted"] is False


def test_no_ai_asks_for_the_part_number(tmp_path: Path):
    session, _meta = _session(tmp_path)
    result = session.reconstruct_identify(vision=StubVision("vision unavailable, using offline reader"))
    assert result["warning"] == NO_AI_MESSAGE
    assert "reshoot" not in result["warning"].lower()
    photo = session.project.reconstruct.photos[0]
    assert photo.unreadable is False
    findings = run_checks(session.project, session.catalog)
    assert not any(row.check == "reconstruct_unreadable" for row in findings)


def test_review_lists_each_part_once_and_scrolls(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    session, meta = _session(tmp_path)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta, tag="K9", part_number="LABEL-100")))
    dialog = ReconstructWizard(session)
    while dialog._page < dialog.stack.count() - 1:
        dialog._next()
    text = dialog.review_summary.text()
    assert "needs size" in text
    assert text.count("LABEL-100") == 1
    assert "i1" not in text.split()
    assert dialog.review_scroll.widget() is dialog.review_summary


def test_needs_info_is_one_problems_row(tmp_path: Path):
    session, meta = _session(tmp_path)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    session.reconstruct_set_corners(photo_id, meta["corners"])
    session.reconstruct_set_scale(
        photo_id,
        "face",
        meta["corners"][0],
        meta["corners"][1],
        known_mm=meta["enclosure_width_mm"],
        known_height_mm=meta["enclosure_height_mm"],
    )
    session.reconstruct_identify(vision=FakeVision(_recordings(meta, tag="", part_number="LABEL-100", brand="Acme")))
    session.reconstruct_place_needs_info()
    findings = [row for row in run_checks(session.project, session.catalog) if row.check == "reconstruct_needs_info"]
    assert len(findings) == 1
    assert "LABEL-100" in findings[0].message
    assert "size unknown" in findings[0].message
    assert "terminals" not in findings[0].message
    assert _CHECK_LABELS["reconstruct_needs_info"] == "Not ready for layout"
    markers = [
        comp
        for sheet in session.project.sheets
        for comp in sheet.components
        if comp.library_id == "reconstruct.needs_info"
    ]
    assert markers
    assert markers[0].id != "U"
    assert not markers[0].id.startswith("U")
    assert markers[0].properties.get("marker_label") == "LABEL-100"
    rows = instance_rows(session.project, session.catalog)
    assert all(row["library_id"] != "reconstruct.needs_info" for row in rows)


def test_photo_keeps_its_original_name_and_errors_are_plain(tmp_path: Path):
    session, _meta = _session(tmp_path)
    photo = session.reconstruct_list_photos()["photos"][0]
    assert photo["original_name"] == "backpanel.png"
    missing = tmp_path / "gone.jpg"
    with pytest.raises(FileNotFoundError, match="Can't find gone.jpg"):
        session.reconstruct_add_photos([missing])
    heic = tmp_path / "phone.heic"
    heic.write_bytes(b"ftypheic")
    with pytest.raises(ValueError, match="HEIC"):
        session.reconstruct_add_photos([heic])
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with pytest.raises(ValueError, match="Old drawing"):
        session.reconstruct_add_photos([pdf])


def _sheet_furniture(session: Session):
    sheets = [item for item in session.project.sheets if item.kind == "panel_layout"]
    if sheets:
        sheet = sheets[0]
    else:
        sheet = session.add_sheet("panel", kind="panel_layout")
    enc = sheet.enclosure
    rail = sheet.rails[0]
    return sheet, (enc.width_mm, enc.height_mm, rail.length_mm)


def _inside(sheet, comp) -> bool:
    from panelcad.library.catalog import default_catalog
    from panelcad.library.panel import effective_footprint

    enc = sheet.enclosure
    dev = default_catalog().get(comp.library_id)
    fp = effective_footprint(dev, comp)
    w, h = float(fp.width_mm or 0), float(fp.height_mm or 0)
    return (
        comp.x + 0.05 >= enc.x
        and comp.y + 0.05 >= enc.y
        and comp.x + w <= enc.x + enc.width_mm + 0.5
        and comp.y + h <= enc.y + enc.height_mm + 0.5
    )


def test_part_corners_do_not_resize_the_enclosure(tmp_path: Path):
    session, meta = _session(tmp_path)
    region = meta["part_region"]
    corners = [
        (region["x"], region["y"]),
        (region["x"] + region["w"], region["y"]),
        (region["x"] + region["w"], region["y"] + region["h"]),
        (region["x"], region["y"] + region["h"]),
    ]
    sheet, before = _sheet_furniture(session)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    session.reconstruct_set_corners(photo_id, corners)
    payload = session.reconstruct_set_scale(
        photo_id,
        "face",
        corners[0],
        corners[1],
        known_mm=77.9,
        known_height_mm=92.5,
    )
    assert payload["scaled"] is True
    assert payload["face_is_backpanel"] is False
    session.reconstruct_identify(
        vision=FakeVision(_recordings(meta, tag="K1", part_number="MC-9b", brand="LS Electric"))
    )
    item = next(row for row in session.reconstruct_status()["items"] if row["role"] == "part")
    session.reconstruct_confirm_item(item["id"], catalog="MC-9b", manufacturer="LS Electric", tag="K1")
    session.reconstruct_place_confirmed()
    enc = sheet.enclosure
    assert (enc.width_mm, enc.height_mm, sheet.rails[0].length_mm) == before
    assert abs(enc.width_mm - 77.9) > 1
    comp = sheet.component("K1")
    assert _inside(sheet, comp)


def test_two_lengths_do_not_resize_the_enclosure(tmp_path: Path):
    from PySide6.QtGui import QColor, QImage

    _app()
    image = QImage(800, 600, QImage.Format.Format_RGB32)
    image.fill(QColor("#778899"))
    shot = tmp_path / "flat.png"
    image.save(str(shot), "PNG")
    job = tmp_path / "flat.pcad.yaml"
    session = Session.create("Flat")
    session.save(job)
    session = Session.open(job)
    session.reconstruct_add_photos([shot], role="backpanel")
    meta = {
        "part_region": {"x": 200, "y": 140, "w": 80, "h": 100},
        "width_px": 800,
        "height_px": 600,
    }
    sheet, before = _sheet_furniture(session)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    session.reconstruct_set_corners(photo_id, [(0, 0), (800, 0), (800, 600), (0, 600)])
    session.reconstruct_set_scale(photo_id, "din_rail", (100, 100), (100, 163))
    payload = session.reconstruct_set_scale(photo_id, "measured", (100, 100), (760, 100), known_mm=350)
    assert payload["scaled"] is True
    assert payload["face_is_backpanel"] is False
    session.reconstruct_identify(
        vision=FakeVision(_recordings(meta, tag="K1", part_number="MC-9b", brand="LS Electric"))
    )
    item = next(row for row in session.reconstruct_status()["items"] if row["role"] == "part")
    session.reconstruct_confirm_item(item["id"], catalog="MC-9b", manufacturer="LS Electric", tag="K1")
    session.reconstruct_place_confirmed()
    enc = sheet.enclosure
    assert (enc.width_mm, enc.height_mm, sheet.rails[0].length_mm) == before
    comp = next(c for c in sheet.components if c.id == "K1")
    assert _inside(sheet, comp)


def test_named_envelope_pin_prints_and_stays_dashed(tmp_path: Path):
    from PySide6.QtGui import QColor, QImage, QPainter

    from panelcad.gui.canvas import SchematicView
    from panelcad.render.page import paint_sheet

    session, meta = _session(tmp_path)
    strips = [
        {
            "tag": "X1",
            "terminals": [{"id": "1", "nx": 0.5, "ny": 0.4}],
            "wire_numbers": [{"number": "101", "nx": 0.52, "ny": 0.42}],
        }
    ]
    session.reconstruct_identify(
        vision=FakeVision(_recordings(meta, tag="A9", part_number="LABEL-100", brand="Acme", strips=strips))
    )
    item = next(row for row in session.reconstruct_status()["items"] if row["role"] == "part")
    session.reconstruct_confirm_item(
        item["id"],
        envelope_w_mm=40,
        envelope_h_mm=80,
        envelope_d_mm=70,
        mount="din_rail",
        tag="A9",
    )
    trace = next(step for step in session.reconstruct_checklist()["checklist"] if step["kind"] == "trace_wire")
    answered = session.reconstruct_answer(trace["id"], "A9:24V")
    assert answered["status"] == "answered"
    wires = [wire for sheet in session.project.sheets for wire in sheet.wires if wire.inferred]
    assert wires
    assert wires[0].caption().startswith("?")
    checks = session.design_checks()
    assert isinstance(checks, list)
    run_checks(session.project, session.catalog)
    csv_path = tmp_path / "wires.csv"
    session.export_wire_list(csv_path)
    assert csv_path.is_file()
    pdf = tmp_path / "pkg.pdf"
    session.print_package(pdf)
    assert pdf.is_file() and pdf.stat().st_size > 500
    schematic = next(sheet for sheet in session.project.sheets if any(wire.inferred for wire in sheet.wires))
    image = QImage(400, 300, QImage.Format.Format_RGB32)
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    paint_sheet(painter, session.project, schematic, session.catalog, sheet_index=0, sheet_count=1)
    painter.end()
    session.set_active_sheet(schematic.id)
    view = SchematicView(session)
    view.reload()
    assert view._items
    assert wires[0].inferred is True
    view.close()


def test_marker_refs_are_numbered(tmp_path: Path):
    from panelcad.reconstruct.service import _prefix_for_category, _unique_ref

    assert _prefix_for_category("control.plc") == "A"
    assert _prefix_for_category("protection.breaker") == "Q"
    assert _prefix_for_category("electrical.isolation") == ""
    assert _prefix_for_category("connection.io") == "A"

    session, meta = _session(tmp_path)
    sheet, _before = _sheet_furniture(session)
    panel_id = sheet.id
    assert _unique_ref(session, "NI", panel_id) == "NI1"
    assert _unique_ref(session, "A", panel_id) == "A1"
    assert _unique_ref(session, "P", panel_id) == "P1"
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    session.reconstruct_set_corners(photo_id, meta["corners"])
    session.reconstruct_set_scale(
        photo_id,
        "backpanel",
        meta["corners"][0],
        meta["corners"][1],
        known_mm=meta["enclosure_width_mm"],
        known_height_mm=meta["enclosure_height_mm"],
    )
    session.reconstruct_identify(vision=FakeVision(_recordings(meta, tag="", part_number="LABEL-100", brand="Acme")))
    session.reconstruct_place_needs_info()
    item = next(row for row in session.project.reconstruct.items if row.role == "part")
    ref = item.placed_ref
    assert ref[-1].isdigit()
    assert ref != "NI"
    assert ref != "P"
    findings = [row for row in run_checks(session.project, session.catalog) if row.check == "reconstruct_needs_info"]
    assert findings
    assert "backpanel.png" in findings[0].message


def test_longer_label_is_not_a_likely_shorter_catalog():
    from panelcad.reconstruct.match import LIKELY_CONFIDENCE

    hits = match_catalog(default_parts_catalog(), part_number="KV-B16TCP", devices=default_catalog())
    for hit in hits:
        if hit.catalog.replace("-", "").casefold() != "kvb16tcp":
            assert hit.confidence < LIKELY_CONFIDENCE
    exact = match_catalog(default_parts_catalog(), part_number="KV-B16TC", devices=default_catalog())
    assert exact
    assert exact[0].catalog == "KV-B16TC"
    assert exact[0].confidence >= LIKELY_CONFIDENCE


def test_photo_viewer_shows_the_original_filename(tmp_path: Path):
    from panelcad.gui.photo_viewer import PhotoViewerDialog

    session, _meta = _session(tmp_path)
    payload = session.reconstruct_photo_region(photo_id="p1")
    dialog = PhotoViewerDialog(session, payload)
    assert "backpanel.png" in dialog.caption.text()
    dialog.close()
