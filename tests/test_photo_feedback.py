"""Tests for the Cabinet Photo Feedback Overlay & Missed-Device Mechanism."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication

from panelcad.api.session import Session
from panelcad.gui.photo_viewer import PhotoFeedbackDialog, PhotoViewerDialog
from panelcad.gui.reconstruct_wizard import ReconstructWizard
from panelcad.reconstruct.feedback import (
    build_photo_feedback_report,
    export_photo_feedback,
    render_annotated_cabinet_photo,
)
from panelcad.reconstruct.models import ensure_reconstruction
from panelcad.reconstruct.vision import FakeVision


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _draw_realistic_cabinet_photo(path: Path, width: int = 1280, height: int = 860) -> Path:
    """Render a realistic control panel photo with DIN rails, wireway ducts, and 5 physical devices."""
    _app()
    img = QImage(width, height, QImage.Format.Format_ARGB32)
    img.fill(QColor("#2b3038"))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # Backplate (galvanized subpanel)
    p.setPen(QPen(QColor("#64748b"), 4))
    p.setBrush(QColor("#cbd5e1"))
    p.drawRect(48, 36, width - 96, height - 72)

    # Slotted grey Panduit wireway ducts (top, middle, bottom)
    p.setPen(QPen(QColor("#475569"), 2))
    p.setBrush(QColor("#94a3b8"))
    for dy in (60, 390, 720):
        p.drawRect(80, dy, width - 160, 44)
        for sx in range(95, width - 165, 18):
            p.drawLine(sx, dy + 6, sx, dy + 38)

    # Two horizontal 35 mm DIN rails
    p.setPen(QPen(QColor("#334155"), 2))
    p.setBrush(QColor("#e2e8f0"))
    p.drawRect(90, 215, width - 180, 35)
    p.drawRect(90, 545, width - 180, 35)

    title_font = QFont("DejaVu Sans", 11, QFont.Weight.Bold)
    small_font = QFont("DejaVu Sans", 9)

    # Device 1 (Top Rail Left): CB1 - Square D QOU220 (Confirmed)
    p.setPen(QPen(QColor("#0f172a"), 3))
    p.setBrush(QColor("#1e293b"))
    p.drawRoundedRect(120, 140, 150, 185, 6, 6)
    p.setBrush(QColor("#ef4444"))
    p.drawRect(160, 215, 70, 28)
    p.setPen(QColor("#f8fafc"))
    p.setFont(title_font)
    p.drawText(132, 168, "CB1")
    p.setFont(small_font)
    p.drawText(132, 192, "Square D QOU220")
    p.drawText(132, 285, "20A  240VAC")

    # Device 2 (Top Rail Center-Left): PSU1 - 24VDC Power Supply (Missing Part # & Verified W×H×D)
    p.setPen(QPen(QColor("#1e3a8a"), 3))
    p.setBrush(QColor("#dbeafe"))
    p.drawRoundedRect(340, 130, 190, 200, 6, 6)
    p.setPen(QColor("#0f172a"))
    p.setFont(title_font)
    p.drawText(355, 160, "PSU1 · 24VDC 5A")
    p.setFont(small_font)
    p.drawText(355, 185, "Cat # Worn Off")
    p.drawText(355, 210, "Input 120VAC / Out 24VDC")

    # Device 3 (Top Rail Center-Right): K1 - Contactor (Unreadable Nameplate & Open Wire #14 Landing)
    p.setPen(QPen(QColor("#312e81"), 3))
    p.setBrush(QColor("#e0e7ff"))
    p.drawRoundedRect(610, 138, 175, 190, 6, 6)
    p.setPen(QColor("#1e1b4b"))
    p.setFont(title_font)
    p.drawText(626, 168, "K1 · M1 CONTACTOR")
    p.setFont(small_font)
    p.drawText(626, 192, "[Glare on Side Label]")
    p.drawText(626, 295, "Wire #14 -> A1?")

    # Device 4 (Top Rail Far Right): CR2 - Safety / Ice-Cube Relay (COMPLETELY MISSED BY BOT)
    p.setPen(QPen(QColor("#7c2d12"), 3))
    p.setBrush(QColor("#ffedd5"))
    p.drawRoundedRect(880, 145, 165, 178, 6, 6)
    p.setPen(QColor("#7c2d12"))
    p.setFont(title_font)
    p.drawText(896, 175, "CR2 · 24VDC RELAY")
    p.setFont(small_font)
    p.drawText(896, 200, "Finder / Omron 8-Pin")
    p.drawText(896, 224, "(Physical Device on Rail)")

    # Device 5 (Bottom Rail Left): PLC1 - Allen-Bradley 1769-L16ER-BB1B (Confirmed)
    p.setPen(QPen(QColor("#14532d"), 3))
    p.setBrush(QColor("#dcfce7"))
    p.drawRoundedRect(130, 465, 280, 195, 6, 6)
    p.setPen(QColor("#064e3b"))
    p.setFont(title_font)
    p.drawText(148, 496, "PLC1 · CompactLogix")
    p.setFont(small_font)
    p.drawText(148, 522, "Allen-Bradley 1769-L16ER-BB1B")
    p.drawText(148, 548, "RUN / OK / ENET")

    p.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    assert img.save(str(path), "PNG")
    return path


def _build_mixed_reconstruct_session(tmp_path: Path) -> tuple[Session, str]:
    """Create a Session with 2 confirmed devices (CB1, PLC1) and 2 missing-info devices (PSU1, K1), leaving CR2 unboxed."""
    photo_path = _draw_realistic_cabinet_photo(tmp_path / "cabinet_overview.png")
    vision = FakeVision(
        recordings={
            "p1": {
                "devices": [
                    {
                        "tag": "CB1",
                        "label_text": "Square D QOU220",
                        "brand": "Square D",
                        "part_number": "QOU220",
                        "confidence": 0.96,
                        "x": 120,
                        "y": 140,
                        "w": 150,
                        "h": 185,
                    },
                    {
                        "tag": "PSU1",
                        "label_text": "24VDC 5A Power Supply",
                        "brand": "Mean Well",
                        "part_number": "",
                        "confidence": 0.62,
                        "x": 340,
                        "y": 130,
                        "w": 190,
                        "h": 200,
                    },
                    {
                        "tag": "K1",
                        "label_text": "M1 Contactor (glare on nameplate)",
                        "brand": "",
                        "part_number": "",
                        "confidence": 0.35,
                        "unreadable": True,
                        "x": 610,
                        "y": 138,
                        "w": 175,
                        "h": 190,
                    },
                    {
                        "tag": "PLC1",
                        "label_text": "Allen-Bradley 1769-L16ER-BB1B",
                        "brand": "Allen-Bradley",
                        "part_number": "1769-L16ER-BB1B",
                        "confidence": 0.94,
                        "x": 130,
                        "y": 465,
                        "w": 280,
                        "h": 195,
                    },
                ],
                "texts": [
                    {
                        "kind": "wire_number",
                        "tag": "K1",
                        "terminal": "A1",
                        "text": "14",
                        "x": 635,
                        "y": 290,
                        "w": 24,
                        "h": 14,
                    }
                ],
            }
        }
    )

    session = Session.create("Cabinet Feedback Test")
    session.save(tmp_path / "cabinet.pcad.yaml")
    added = session.reconstruct_add_photos([photo_path], role="backpanel")
    photo_id = added["added"][0]["id"]
    session.reconstruct_identify(vision=vision)

    items = session.reconstruct_status()["items"]
    by_tag = {it["tag"]: it["id"] for it in items if it.get("role") == "part"}
    session.reconstruct_confirm_item(
        by_tag["CB1"],
        envelope_w_mm=38.0,
        envelope_h_mm=103.0,
        envelope_d_mm=76.0,
        tag="CB1",
        mount="din_rail",
    )
    session.reconstruct_confirm_item(
        by_tag["PLC1"],
        envelope_w_mm=130.0,
        envelope_h_mm=118.0,
        envelope_d_mm=105.0,
        tag="PLC1",
        mount="din_rail",
    )
    return session, photo_id


def test_photo_feedback_report_classifies_confirmed_and_missing_info_devices(tmp_path: Path) -> None:
    session, photo_id = _build_mixed_reconstruct_session(tmp_path)

    report = session.reconstruct_photo_feedback(photo_id=photo_id, filter_mode="all")
    assert report["photo_id"] == photo_id
    assert report["total_detected"] == 4
    assert report["confirmed_count"] == 2
    assert report["missing_info_count"] == 2
    assert "without a box was missed" in report["missed_device_prompt"]

    boxes_by_tag = {b["tag"]: b for b in report["boxes"]}
    assert set(boxes_by_tag.keys()) == {"CB1", "PSU1", "K1", "PLC1"}

    # Confirmed devices have green status_category == 'confirmed' and no missing_reasons
    assert boxes_by_tag["CB1"]["status_category"] == "confirmed"
    assert boxes_by_tag["CB1"]["layout_ready"] is True
    assert boxes_by_tag["CB1"]["missing_reasons"] == []
    assert any("Square D QOU220" in line for line in boxes_by_tag["CB1"]["confirmed_summary"])

    assert boxes_by_tag["PLC1"]["status_category"] == "confirmed"
    assert boxes_by_tag["PLC1"]["layout_ready"] is True

    # Missing-info devices have status_category == 'missing_info' and explicit MISSING: reasons
    psu_box = boxes_by_tag["PSU1"]
    assert psu_box["status_category"] == "missing_info"
    assert psu_box["layout_ready"] is False
    assert any("MISSING: Part # / Catalog SKU" in r for r in psu_box["missing_reasons"])
    assert any("MISSING: Verified Dimensions (W×H×D mm)" in r for r in psu_box["missing_reasons"])

    k1_box = boxes_by_tag["K1"]
    assert k1_box["status_category"] == "missing_info"
    assert any("Needs Close-Up Photo" in r for r in k1_box["missing_reasons"])
    assert any("MISSING: Wire #14 Landing from K1:A1" in r for r in k1_box["missing_reasons"])


def test_photo_feedback_filter_modes(tmp_path: Path) -> None:
    session, photo_id = _build_mixed_reconstruct_session(tmp_path)

    missing_only = build_photo_feedback_report(session, photo_id=photo_id, filter_mode="missing_only")
    assert [b["tag"] for b in missing_only["boxes"]] == ["PSU1", "K1"]
    assert missing_only["total_detected"] == 4
    assert missing_only["missing_info_count"] == 2

    confirmed_only = build_photo_feedback_report(session, photo_id=photo_id, filter_mode="confirmed_only")
    assert [b["tag"] for b in confirmed_only["boxes"]] == ["CB1", "PLC1"]
    assert confirmed_only["confirmed_count"] == 2


def test_add_missed_device_on_photo_registers_box_and_updates_feedback(tmp_path: Path) -> None:
    session, photo_id = _build_mixed_reconstruct_session(tmp_path)

    # Human spots CR2 on the photo at (880, 145, 165, 178) that the bot completely missed
    added = session.reconstruct_add_missed_device(
        photo_id,
        {"x": 880, "y": 145, "w": 165, "h": 178},
        tag="CR2",
        brand="Finder",
        note="Missed 24VDC ice-cube relay on top DIN rail",
    )
    new_item = added["item"]
    assert new_item["tag"] == "CR2"
    assert new_item["status"] == "needs_info"
    assert new_item["region"]["x"] == 880.0
    assert new_item["region"]["w"] == 165.0

    report = added["feedback"]
    assert report["total_detected"] == 5
    assert report["missing_info_count"] == 3
    cr2_box = next(b for b in report["boxes"] if b["tag"] == "CR2")
    assert cr2_box["status_category"] == "missing_info"
    assert any("MISSING: Part # / Catalog SKU" in r for r in cr2_box["missing_reasons"])
    assert "Missed 24VDC ice-cube relay" in cr2_box["header_label"]

    # Undo & Redo work cleanly via Session activity tracking
    session.undo()
    assert session.reconstruct_photo_feedback(photo_id=photo_id)["total_detected"] == 4
    session.redo()
    assert session.reconstruct_photo_feedback(photo_id=photo_id)["total_detected"] == 5


def test_render_and_export_annotated_cabinet_photo_png(tmp_path: Path) -> None:
    session, photo_id = _build_mixed_reconstruct_session(tmp_path)

    out_png = tmp_path / "annotated_cabinet_feedback.png"
    res = export_photo_feedback(
        session,
        output_path=out_png,
        photo_id=photo_id,
        filter_mode="all",
        unboxed_callouts=[
            {
                "x": 880,
                "y": 145,
                "w": 165,
                "h": 178,
                "label": "? UNBOXED DEVICE — MISSED BY BOT (CR2)",
                "detail": "Drag a box here to add missed device CR2",
            }
        ],
    )
    assert out_png.is_file()
    assert out_png.stat().st_size > 15_000
    assert res["total_detected"] == 4
    assert res["confirmed_count"] == 2
    assert res["missing_info_count"] == 2

    img = render_annotated_cabinet_photo(session, photo_id=photo_id, filter_mode="all")
    assert img.width() == 1280
    # Top + bottom Site-Exit Readiness banners added to 860 px source height
    assert img.height() > 860 + 90


def test_gui_photo_feedback_dialog_inspect_confirm_and_drag_missed_device(tmp_path: Path) -> None:
    _app()
    session, photo_id = _build_mixed_reconstruct_session(tmp_path)

    dlg = PhotoFeedbackDialog(session, photo_id=photo_id)
    dlg.show()
    _app().processEvents()

    assert dlg.device_list.count() == 4
    assert "4 detected (2 confirmed ✓, 2 missing info ⚠)" in dlg.summary_label.text()

    # Click PSU1's bounding box directly on the photo view
    hit_id = dlg.view.simulate_click_photo_px(400, 200)
    assert hit_id is not None
    assert dlg.tag_edit.text() == "PSU1"
    assert "MISSING: Part # / Catalog SKU" in dlg.missing_details_label.text()

    # Fill in PSU1's missing catalog & dimensions right in the dialog and save
    dlg.part_edit.setText("SDR-120-24")
    dlg.brand_edit.setText("Mean Well")
    dlg.w_spin.setValue(40.0)
    dlg.h_spin.setValue(125.2)
    dlg.d_spin.setValue(113.5)
    idx = dlg.mount_combo.findData("din_rail")
    dlg.mount_combo.setCurrentIndex(idx)
    dlg.save_device_btn.click()
    _app().processEvents()

    # PSU1 is now confirmed! Confirmed count goes from 2 -> 3, missing from 2 -> 1
    assert "4 detected (3 confirmed ✓, 1 missing info ⚠)" in dlg.summary_label.text()

    # Now toggle 'Drag Box on Photo for Missed Device' and drag a box over unboxed CR2 (880, 145, 165, 178)
    dlg.draw_missed_btn.setChecked(True)
    dlg.view.simulate_drag_photo_region(880, 145, 165, 178)
    assert dlg.missed_region_edit.text() == "880,145,165,178"
    dlg.missed_tag_edit.setText("CR2")
    dlg.missed_note_edit.setText("Missed safety relay on top rail")
    dlg.add_missed_btn.click()
    _app().processEvents()

    assert dlg.device_list.count() == 5
    assert "5 detected (3 confirmed ✓, 2 missing info ⚠)" in dlg.summary_label.text()

    # Filter to 'Missing Info Only'
    dlg.set_filter_mode("missing_only")
    _app().processEvents()
    assert dlg.device_list.count() == 2

    # Export annotated PNG from the dialog
    exported = tmp_path / "dialog_exported_feedback.png"
    dlg.set_filter_mode("all")
    export_res = dlg.export_annotated_to_path(exported)
    assert Path(export_res["output_path"]).is_file()
    dlg.close()


def test_reconstruct_wizard_exposes_photo_feedback_buttons(tmp_path: Path) -> None:
    _app()
    session, _photo_id = _build_mixed_reconstruct_session(tmp_path)
    wiz = ReconstructWizard(session)
    assert hasattr(wiz, "photo_feedback_btn")
    assert hasattr(wiz, "export_feedback_btn")
    wiz.close()


def test_photo_viewer_dialog_preserves_caption_and_feedback_button(tmp_path: Path) -> None:
    _app()
    session, _photo_id = _build_mixed_reconstruct_session(tmp_path)
    items = [it for it in session.reconstruct_status()["items"] if it.get("role") == "part"]
    payload = session.reconstruct_photo_region(item_id=items[0]["id"])
    viewer = PhotoViewerDialog(session, payload)
    assert "cabinet_overview.png" in viewer.caption.text()
    assert hasattr(viewer, "feedback_btn")
    viewer.close()



def test_capture_phase_site_exit_readiness_and_closeup_linking(tmp_path: Path) -> None:
    """Capture-Phase Site-Exit Check blocks site exit until missing close-ups/specs are resolved."""
    session, photo_id = _build_mixed_reconstruct_session(tmp_path)

    # 1. Initial site-exit check: 2 confirmed, 2 missing info -> DO NOT LEAVE SITE YET
    check = session.reconstruct_site_exit_check(photo_id=photo_id)
    assert check["safe_to_leave_site"] is False
    assert check["site_exit_status"] == "do_not_leave"
    assert "DO NOT LEAVE SITE YET" in check["site_exit_banner"]
    assert check["missing_info_count"] == 2
    assert check["closeup_needed_count"] >= 1
    assert len(check["on_site_shot_list"]) == 2
    assert any("PSU1" in s and "close-up" in s for s in check["on_site_shot_list"])
    assert any("K1" in s for s in check["on_site_shot_list"])

    # 2. Tech takes a close-up photo of PSU1 on site and links it with the nameplate details
    closeup_img = QImage(640, 480, QImage.Format.Format_RGB32)
    closeup_img.fill(QColor("#334155"))
    closeup_path = tmp_path / "psu1_nameplate_closeup.png"
    assert closeup_img.save(str(closeup_path))

    store = ensure_reconstruction(session.project)
    psu_item = next(i for i in store.items if i.tag == "PSU1")
    k1_item = next(i for i in store.items if i.tag == "K1")

    link_res = session.reconstruct_link_closeup(
        psu_item.id,
        closeup_photo_path=closeup_path,
        tag="PSU1",
        brand="PULS",
        part_number="CP10.241",
        envelope_w_mm=39.0,
        envelope_h_mm=124.0,
        envelope_d_mm=117.0,
        mount="din_rail",
    )
    assert link_res["closeup_photo_id"] != ""
    assert link_res["feedback_box"]["status_category"] == "confirmed"
    assert link_res["feedback_box"]["closeup_photo_id"] == link_res["closeup_photo_id"]
    # Still 1 device (K1) missing info -> still not safe to leave site
    assert link_res["feedback"]["safe_to_leave_site"] is False
    assert link_res["feedback"]["missing_info_count"] == 1

    # 3. Tech confirms K1 dimensions and records wire #W104 landing on site before closing the cabinet door
    session.reconstruct_confirm_item(
        k1_item.id,
        tag="K1",
        envelope_w_mm=45.0,
        envelope_h_mm=85.0,
        envelope_d_mm=92.0,
        mount="din_rail",
    )
    from panelcad.reconstruct.service import record_wire_landing
    record_wire_landing(session, wire_number="14", from_ref="K1:A1", to_ref="PLC1:O0")
    final_check = session.reconstruct_site_exit_check(photo_id=photo_id)
    assert final_check["safe_to_leave_site"] is True
    assert final_check["site_exit_status"] == "ready"
    assert "SITE CAPTURE READY" in final_check["site_exit_banner"]
    assert final_check["missing_info_count"] == 0
    assert final_check["on_site_shot_list"] == []


def test_reconstruct_wizard_step1_capture_phase_site_exit_gui(tmp_path: Path) -> None:
    """Step 1 ('Add photos') of ReconstructWizard displays live Site-Exit Readiness & shot list."""
    _app = QApplication.instance() or QApplication([])
    from panelcad.gui.reconstruct_wizard import ReconstructWizard

    session, photo_id = _build_mixed_reconstruct_session(tmp_path)
    wiz = ReconstructWizard(session)
    wiz.resize(1180, 820)
    wiz.show()

    # Step 1 is index 0
    assert wiz.stack.currentIndex() == 0
    report = wiz.run_capture_site_exit_check()
    assert report["safe_to_leave_site"] is False
    assert "DO NOT LEAVE SITE YET" in wiz.site_exit_banner.text()
    assert wiz.shot_list.count() == 2

    # Drawing a missed device box directly in Step 1 capture view adds a device and updates shot list
    wiz.capture_draw_missed_btn.setChecked(True)
    wiz.capture_feedback_view.region_drawn.emit({"x": 880.0, "y": 145.0, "w": 165.0, "h": 178.0})
    assert wiz.shot_list.count() == 3
    wiz.close()


def test_reconstruct_wizard_capture_attach_closeup_button_and_vision_forwarding(
    tmp_path: Path, monkeypatch
) -> None:
    """Clicking capture_link_closeup_btn pre-selects the first missing-info device and links a close-up;
    run_capture_site_exit_check forwards vision=self.vision when identifying a fresh photo."""
    _app()
    from PySide6.QtWidgets import QFileDialog
    from panelcad.gui.reconstruct_wizard import ReconstructWizard

    # 1. Verify vision=self.vision is forwarded by run_capture_site_exit_check() on a fresh photo
    photo_path = _draw_realistic_cabinet_photo(tmp_path / "fresh_cabinet.png")
    vision = FakeVision(
        recordings={
            "p1": {
                "devices": [
                    {
                        "brand": "Mean Well",
                        "part_number": "",
                        "tag": "PSU1",
                        "label_text": "24VDC 5A",
                        "confidence": 0.42,
                        "unreadable": True,
                        "x": 380,
                        "y": 140,
                        "w": 170,
                        "h": 220,
                    }
                ],
                "wire_labels": [],
            }
        }
    )
    fresh_session = Session.create("Vision Forwarding Check")
    fresh_session.save(tmp_path / "fresh.pcad.yaml")
    fresh_session.reconstruct_add_photos([photo_path], role="backpanel")

    wiz_fresh = ReconstructWizard(fresh_session, vision=vision)
    rep = wiz_fresh.run_capture_site_exit_check()
    assert rep["total_detected"] == 1
    assert rep["missing_info_count"] == 1
    assert rep["boxes"][0]["tag"] == "PSU1"
    wiz_fresh.close()

    # 2. Verify clicking wiz.capture_link_closeup_btn attaches a close-up to the first missing-info device (PSU1)
    # even when item 0 (CB1) is already confirmed.
    session, photo_id = _build_mixed_reconstruct_session(tmp_path)
    closeup_img = QImage(320, 240, QImage.Format.Format_RGB32)
    closeup_img.fill(QColor("#1e293b"))
    closeup_file = tmp_path / "psu1_closeup_click.png"
    assert closeup_img.save(str(closeup_file))

    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *args, **kwargs: (str(closeup_file), "Images (*.png)")),
    )

    wiz = ReconstructWizard(session)
    wiz.show()
    _app().processEvents()

    # Before clicking: PSU1 has no closeup_photo_id
    before_rep = session.reconstruct_photo_feedback(photo_id=photo_id)
    psu_before = next(b for b in before_rep["boxes"] if b["tag"] == "PSU1")
    assert psu_before["closeup_photo_id"] == ""

    # Click the Step 1 'Attach Close-Up…' button directly
    wiz.capture_link_closeup_btn.click()
    _app().processEvents()

    after_rep = session.reconstruct_photo_feedback(photo_id=photo_id)
    psu_after = next(b for b in after_rep["boxes"] if b["tag"] == "PSU1")
    assert psu_after["closeup_photo_id"] != ""
    assert psu_after["needs_closeup"] is False
    wiz.close()


def test_mobile_field_capture_web_server_end_to_end_and_gui_qr_dialog(tmp_path: Path) -> None:
    """Turnkey Mobile Field Capture web server (HTTP API + PWA + QR pairing dialog) works end-to-end."""
    import json
    import urllib.request
    from panelcad.gui.photo_viewer import MobileCaptureServerDialog
    from panelcad.reconstruct.capture_web import CaptureWebServer, render_qr_svg

    _app()
    session, photo_id = _build_mixed_reconstruct_session(tmp_path)

    # 1. Test QR SVG generator & MobileCaptureServerDialog in Desktop GUI
    svg = render_qr_svg("http://127.0.0.1:8788/")
    assert "<svg" in svg and "viewBox" in svg

    dlg = MobileCaptureServerDialog(session)
    dlg.port_spin.setValue(0)  # ephemeral port
    url = dlg.start_server()
    assert url.startswith("http://")
    assert dlg.server is not None
    port = dlg.server.port
    assert port > 0
    base = f"http://127.0.0.1:{port}"

    try:
        # 2. Check /api/health, /, /manifest.json, /api/server-info
        with urllib.request.urlopen(f"{base}/api/health", timeout=5) as resp:
            health = json.loads(resp.read().decode("utf-8"))
            assert health["ok"] is True

        with urllib.request.urlopen(f"{base}/", timeout=5) as resp:
            html = resp.read().decode("utf-8")
            assert "PanelCAD Field Capture" in html
            assert "capture=\"environment\"" in html

        with urllib.request.urlopen(f"{base}/api/server-info", timeout=5) as resp:
            sinfo = json.loads(resp.read().decode("utf-8"))
            assert sinfo["port"] == port
            assert len(sinfo["urls"]) >= 1

        # 3. Check live desktop session status via /api/sessions/<id>/status
        sid = dlg.server.live_session_id
        with urllib.request.urlopen(f"{base}/api/sessions/{sid}/status", timeout=5) as resp:
            st = json.loads(resp.read().decode("utf-8"))
            assert st["safe_to_leave_site"] is False
            assert st["confirmed_count"] == 2
            assert st["missing_info_count"] == 2
            assert st["overlay_geometry"]["top_banner_px"] > 0

        # 4. Fetch annotated PNG from mobile endpoint
        with urllib.request.urlopen(f"{base}/api/sessions/{sid}/annotated.png", timeout=5) as resp:
            png_bytes = resp.read()
            assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
            assert len(png_bytes) > 10_000

        # 5. Finger-drag a missed device box from the mobile web API
        missed_payload = json.dumps(
            {
                "photo_id": photo_id,
                "region": {"x": 880, "y": 145, "w": 165, "h": 178},
                "tag": "SR1",
                "brand": "Dold",
                "part_number": "LG5924",
                "envelope_w_mm": 22.5,
                "envelope_h_mm": 90.0,
                "envelope_d_mm": 118.0,
                "mount": "din_rail",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/api/sessions/{sid}/missed-device",
            data=missed_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            after_missed = json.loads(resp.read().decode("utf-8"))
            assert after_missed["total_detected"] == 5
            assert after_missed["confirmed_count"] == 3

        # 6. Attach close-up + specs for PSU1 via mobile API
        psu_box = next(b for b in after_missed["boxes"] if b["tag"] == "PSU1")
        closeup_payload = json.dumps(
            {
                "tag": "PSU1",
                "brand": "Eaton",
                "part_number": "PSG240E24RM",
                "envelope_w_mm": 85.0,
                "envelope_h_mm": 125.0,
                "envelope_d_mm": 128.0,
                "mount": "din_rail",
            }
        ).encode("utf-8")
        req2 = urllib.request.Request(
            f"{base}/api/sessions/{sid}/items/{psu_box['item_id']}/closeup",
            data=closeup_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req2, timeout=5) as resp:
            after_closeup = json.loads(resp.read().decode("utf-8"))
            assert after_closeup["confirmed_count"] == 4
            assert after_closeup["missing_info_count"] == 1
    finally:
        dlg.stop_server()
        dlg.close()


def test_refine_device_box_on_photo_and_session_update_device_box_with_undo(tmp_path: Path) -> None:
    """refine_device_box_on_photo snaps a loose box to strong physical device borders, and
    session.reconstruct_update_device_box supports direct adjustment, edge-snapping, and undo/redo."""
    from panelcad.reconstruct.feedback import refine_device_box_on_photo, snap_drawn_box_to_device_edges

    session, photo_id = _build_mixed_reconstruct_session(tmp_path)
    photo_path = tmp_path / "cabinet_overview.png"

    # CB1 is drawn at (120, 140, 150, 185) on cabinet_overview.png.
    # Pass a loose box around CB1 and verify refine_device_box_on_photo tightens it toward the edges:
    coarse = (108, 128, 174, 208)
    rx, ry, rw, rh = refine_device_box_on_photo(photo_path, coarse)
    assert abs(rx - 120) <= 10
    assert abs(ry - 140) <= 10
    assert abs(rw - 150) <= 16
    assert abs(rh - 185) <= 16

    snap_tuple = snap_drawn_box_to_device_edges(photo_path, {"x": 108, "y": 128, "w": 174, "h": 208})
    assert snap_tuple == (rx, ry, rw, rh)

    # Preview via session.reconstruct_refine_device_box without mutating session
    preview = session.reconstruct_refine_device_box(photo_id, coarse)
    assert preview["x"] == float(rx) and preview["w"] == float(rw)

    # Mutate an existing item's box via session.reconstruct_update_device_box and verify undo/redo
    store = ensure_reconstruction(session.project)
    cb1 = next(i for i in store.items if i.tag == "CB1")
    orig_region = (cb1.region.x, cb1.region.y, cb1.region.w, cb1.region.h)

    upd = session.reconstruct_update_device_box(
        cb1.id,
        box={"x": 108.0, "y": 128.0, "w": 174.0, "h": 208.0},
        snap_to_edges=True,
    )
    assert upd["item"]["id"] == cb1.id
    assert abs(cb1.region.x - rx) <= 12

    # Undo restores original box
    session.undo()
    cb1_after_undo = next(i for i in ensure_reconstruction(session.project).items if i.tag == "CB1")
    assert (
        cb1_after_undo.region.x,
        cb1_after_undo.region.y,
        cb1_after_undo.region.w,
        cb1_after_undo.region.h,
    ) == orig_region

    # Redo reapplies refined box
    session.redo()
    cb1_after_redo = next(i for i in ensure_reconstruction(session.project).items if i.tag == "CB1")
    assert cb1_after_redo.region.x == float(upd["region"]["x"])


def test_mobile_web_update_box_and_refine_box_endpoints_and_two_click_mode(tmp_path: Path) -> None:
    """Mobile web /api/sessions/<id>/items/<item_id>/box and /refine-box endpoints + Desktop 2-click corner mode."""
    import json
    import urllib.request
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from panelcad.gui.photo_viewer import MobileCaptureServerDialog, PhotoFeedbackDialog

    _app()
    session, photo_id = _build_mixed_reconstruct_session(tmp_path)

    # 1. Test Desktop PhotoFeedbackDialog Auto-Snap Selected Box & Redraw Selected Box buttons
    dlg_fb = PhotoFeedbackDialog(session, photo_id=photo_id)
    dlg_fb.show()
    _app().processEvents()
    dlg_fb.view.simulate_click_photo_px(400, 200)  # select PSU1
    assert dlg_fb.tag_edit.text() == "PSU1"
    dlg_fb.snap_selected_btn.click()
    _app().processEvents()
    assert "Snapped" in dlg_fb.status_note.text()

    dlg_fb.redraw_selected_btn.setChecked(True)
    dlg_fb.view.simulate_drag_photo_region(378, 138, 174, 224)
    _app().processEvents()
    assert "Updated & edge-snapped box" in dlg_fb.status_note.text()
    dlg_fb.close()

    # 2. Test Mobile Web HTTP endpoints /refine-box and /items/<item_id>/box
    dlg = MobileCaptureServerDialog(session)
    dlg.port_spin.setValue(0)
    dlg.start_server()
    assert dlg.server is not None
    base = f"http://127.0.0.1:{dlg.server.port}"
    sid = dlg.server.live_session_id
    try:
        ref_payload = json.dumps(
            {"photo_id": photo_id, "region": {"x": 108, "y": 128, "w": 174, "h": 208}}
        ).encode("utf-8")
        req_ref = urllib.request.Request(
            f"{base}/api/sessions/{sid}/refine-box",
            data=ref_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_ref, timeout=5) as resp:
            ref_res = json.loads(resp.read().decode("utf-8"))
            assert "region" in ref_res
            assert abs(ref_res["region"]["x"] - 120) <= 10

        store = ensure_reconstruction(session.project)
        cb1_id = next(i.id for i in store.items if i.tag == "CB1")
        box_payload = json.dumps(
            {"region": {"x": 115, "y": 125, "w": 210, "h": 220}, "snap_to_edges": True}
        ).encode("utf-8")
        req_box = urllib.request.Request(
            f"{base}/api/sessions/{sid}/items/{cb1_id}/box",
            data=box_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_box, timeout=5) as resp:
            box_res = json.loads(resp.read().decode("utf-8"))
            assert box_res["total_detected"] == 4
    finally:
        dlg.stop_server()
        dlg.close()


def test_desktop_step1_and_phone_status_report_the_same_readiness(tmp_path: Path) -> None:
    """Desktop Step 1 and /api/sessions/<id>/status must check the same photo and count Ready the same way."""
    import json
    import urllib.request
    from panelcad.reconstruct.capture_web import CaptureWebServer
    from panelcad.reconstruct.feedback import default_capture_photo_id

    _app()
    session, photo_id = _build_mixed_reconstruct_session(tmp_path)
    # A door photo listed before the backpanel used to send desktop (first row) and the
    # phone page (its own role list) to different photos.
    door = QImage(320, 240, QImage.Format.Format_RGB32)
    door.fill(QColor("#475569"))
    door_path = tmp_path / "door_inside.png"
    assert door.save(str(door_path))
    session.reconstruct_add_photos([door_path], role="door_inside")
    store = ensure_reconstruction(session.project)
    store.photos.insert(0, store.photos.pop())
    assert default_capture_photo_id(session) == photo_id

    wiz = ReconstructWizard(session)
    wiz._refresh_photos()
    desktop = wiz.refresh_capture_feedback()
    assert desktop["photo_id"] == photo_id

    server = CaptureWebServer(tmp_path / "capture-ws", host="127.0.0.1", port=0, live_session=session)
    server.start()
    try:
        sid = server.live_session_id
        with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/api/sessions/{sid}/status", timeout=5) as resp:
            phone = json.loads(resp.read().decode("utf-8"))
    finally:
        server.stop()
        wiz.close()

    for key in ("photo_id", "total_detected", "confirmed_count", "missing_info_count", "site_exit_banner"):
        assert phone[key] == desktop[key], key
    assert "(2/4 Ready)" in desktop["site_exit_banner"]
    assert wiz.site_exit_banner.text() == phone["site_exit_banner"]


def test_callout_cards_do_not_cover_other_cards_or_device_boxes(tmp_path: Path) -> None:
    """Placement avoids other cards and boxes when there is room, for any box layout."""
    from PySide6.QtCore import QRectF
    from panelcad.reconstruct.feedback import place_callout_card

    _app()
    boxes = [QRectF(100 + i * 130, 200, 120, 160) for i in range(5)]
    labels: list[QRectF] = []
    for b in boxes:
        card = place_callout_card(
            b, 160, 40, min_y=0, max_y=800, max_x=1000, boxes=boxes, labels=labels
        )
        assert not any(card.intersects(o) for o in labels)
        assert not any(card.intersects(o) for o in boxes)
        labels.append(card)


def test_photo_feedback_dialog_banner_once_and_rows_wrap(tmp_path: Path) -> None:
    _app()
    session, photo_id = _build_mixed_reconstruct_session(tmp_path)
    dlg = PhotoFeedbackDialog(session, photo_id=photo_id)
    banner = dlg.site_exit_banner_label.text()
    assert banner and banner not in dlg.summary_label.text()
    for lst in (dlg.device_list, dlg.shot_list_widget):
        assert lst.wordWrap()
        assert lst.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    wiz = ReconstructWizard(session)
    assert wiz.shot_list.wordWrap()
    dlg.close()
    wiz.close()


def _part_item(session: Session, tag: str):
    store = ensure_reconstruction(session.project)
    return next(item for item in store.items if item.tag == tag and item.role == "part")


def test_apply_missing_info_rejects_placeholders_and_updates_counts(tmp_path: Path) -> None:
    """Saving a badge writes the project, refuses placeholders, and refreshes readiness counts."""
    session, photo_id = _build_mixed_reconstruct_session(tmp_path)
    psu = _part_item(session, "PSU1")
    k1 = _part_item(session, "K1")
    before = session.reconstruct_site_exit_check(photo_id=photo_id)
    assert before["missing_info_count"] == 2
    assert before["confirmed_count"] == 2
    assert before["safe_to_leave_site"] is False

    report = session.reconstruct_photo_feedback(photo_id=photo_id)
    by_tag = {box["tag"]: box for box in report["boxes"]}
    assert [badge["field"] for badge in by_tag["PSU1"]["badges"]] == [
        "part_number",
        "dimensions",
        "ratings",
        "terminals_wires",
    ]
    assert by_tag["CB1"]["badges"] == []
    assert by_tag["PLC1"]["badges"] == []

    for bogus in ("TBD", "N/A", "placeholder", "INFO NEEDED", "generic", "unknown"):
        for field in ("part_number", "ratings", "terminals_wires"):
            kwargs = {"value": bogus} if field != "terminals_wires" else {"terminal": bogus}
            try:
                session.reconstruct_apply_missing_info(psu.id, field, **kwargs)
            except ValueError:
                pass
            else:
                raise AssertionError(f"{field} accepted placeholder {bogus}")
    unchanged = session.reconstruct_site_exit_check(photo_id=photo_id)
    assert unchanged["missing_info_count"] == 2
    assert unchanged["confirmed_count"] == 2
    assert _part_item(session, "PSU1").part_number == ""
    assert _part_item(session, "PSU1").ratings == ""

    typed = session.reconstruct_apply_missing_info(psu.id, "part_number", value="NAMEPLATE-9F2K")
    assert typed["missing_info_count"] == 2
    assert _part_item(session, "PSU1").part_number == "NAMEPLATE-9F2K"
    assert _part_item(session, "PSU1").status != "catalog"
    assert _part_item(session, "PSU1").envelope_w_mm in (None, 0)

    rated = session.reconstruct_apply_missing_info(psu.id, "ratings", value="24VDC 5A")
    assert rated["missing_info_count"] == 2
    assert rated["confirmed_count"] == 2
    assert _part_item(session, "PSU1").ratings == "24VDC 5A"
    session.undo()
    assert _part_item(session, "PSU1").ratings == ""
    session.redo()
    assert _part_item(session, "PSU1").ratings == "24VDC 5A"
    psu_box = next(box for box in rated["feedback"]["boxes"] if box["tag"] == "PSU1")
    assert all(badge["field"] != "ratings" for badge in psu_box["badges"])

    session.save(tmp_path / "cabinet.pcad.yaml")
    reloaded = Session.open(tmp_path / "cabinet.pcad.yaml")
    assert _part_item(reloaded, "PSU1").ratings == "24VDC 5A"
    assert _part_item(reloaded, "PSU1").part_number == "NAMEPLATE-9F2K"
    session = reloaded
    psu = _part_item(session, "PSU1")
    k1 = _part_item(session, "K1")

    sized = session.reconstruct_apply_missing_info(
        psu.id,
        "dimensions",
        width_mm=40.0,
        height_mm=125.2,
        depth_mm=113.5,
        mount="din_rail",
    )
    assert sized["confirmed_count"] == 3
    assert sized["missing_info_count"] == 1
    assert sized["safe_to_leave_site"] is False
    assert "DO NOT LEAVE SITE YET" in sized["site_exit_banner"]
    assert "1 missing" in sized["site_exit_banner"] or sized["missing_info_count"] == 1

    k1_sized = session.reconstruct_apply_missing_info(
        k1.id,
        "dimensions",
        width_mm=45.0,
        height_mm=85.0,
        depth_mm=92.0,
        mount="din_rail",
    )
    assert k1_sized["missing_info_count"] == 1
    assert k1_sized["safe_to_leave_site"] is False

    landed = session.reconstruct_apply_missing_info(
        k1.id,
        "terminals_wires",
        terminal="A1",
        wire_number="14",
        to_ref="PLC1:O0",
    )
    assert landed["missing_info_count"] == 0
    assert landed["confirmed_count"] == 4
    assert landed["safe_to_leave_site"] is True
    assert landed["site_exit_status"] == "ready"
    assert "SITE CAPTURE READY" in landed["site_exit_banner"]
    assert _part_item(session, "K1").terminal == "A1"
    assert _part_item(session, "K1").wire_number == "14"


def test_apply_missing_info_library_pick_uses_published_part(tmp_path: Path) -> None:
    session, photo_id = _build_mixed_reconstruct_session(tmp_path)
    from panelcad.reconstruct.feedback import library_choices

    choices = library_choices(session, "1489-M1C010")
    pick = next(row for row in choices if row["catalog"] == "1489-M1C010")
    assert pick["width_mm"] and pick["height_mm"] and pick["depth_mm"]
    assert pick["ratings"]
    psu = _part_item(session, "PSU1")
    saved = session.reconstruct_apply_missing_info(
        psu.id,
        "part_number",
        catalog=pick["catalog"],
        manufacturer=pick["manufacturer"],
        library_id=pick["library_id"],
    )
    item = _part_item(session, "PSU1")
    assert item.status == "catalog"
    assert item.part_number == "1489-M1C010"
    assert item.matches[0].width_mm == pick["width_mm"]
    assert item.matches[0].height_mm == pick["height_mm"]
    assert item.matches[0].depth_mm == pick["depth_mm"]
    assert item.envelope_confirmed is False
    assert item.ratings == pick["ratings"]
    assert saved["confirmed_count"] == 3
    assert saved["missing_info_count"] == 1
    assert saved["feedback_box"]["status_category"] == "confirmed"
    check = session.reconstruct_site_exit_check(photo_id=photo_id)
    assert check["confirmed_count"] == saved["confirmed_count"]
    assert check["missing_info_count"] == saved["missing_info_count"]
    assert check["safe_to_leave_site"] is False


def test_gui_badge_inline_editor_saves_and_refreshes_counts(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QPushButton

    _app()
    session, photo_id = _build_mixed_reconstruct_session(tmp_path)
    dlg = PhotoFeedbackDialog(session, photo_id=photo_id)
    dlg.resize(1180, 780)
    dlg.show()
    _app().processEvents()

    hits = [hit for hit in dlg.view.badge_hits() if hit["field"] == "dimensions"]
    assert hits
    hit = hits[0]
    dlg.view.simulate_click_photo_px(hit["x"] + 4, hit["y"] + 4)
    _app().processEvents()
    assert dlg.inline_editor.isVisible()
    assert dlg.inline_editor.field == "dimensions"

    button = dlg.findChild(QPushButton, f"badge-{dlg.inline_editor.item_id}-ratings")
    assert button is not None
    button.click()
    _app().processEvents()
    assert dlg.inline_editor.field == "ratings"
    dlg.inline_editor.value_edit.setText("TBD")
    dlg.inline_editor.save_btn.click()
    _app().processEvents()
    assert "placeholder" in dlg.inline_editor.error_label.text().lower()
    assert "2 missing info" in dlg.summary_label.text()

    dlg.inline_editor.value_edit.setText("24VDC 5A")
    dlg.inline_editor.save_btn.click()
    _app().processEvents()
    assert not dlg.inline_editor.isVisible()
    assert "2 missing info" in dlg.summary_label.text()
    assert "DO NOT LEAVE SITE YET" in dlg.site_exit_banner_label.text()
    assert _part_item(session, "PSU1").ratings == "24VDC 5A"

    psu_id = _part_item(session, "PSU1").id
    dim_btn = dlg.findChild(QPushButton, f"badge-{psu_id}-dimensions")
    assert dim_btn is not None
    dim_btn.click()
    _app().processEvents()
    dlg.inline_editor.w_spin.setValue(40.0)
    dlg.inline_editor.h_spin.setValue(125.2)
    dlg.inline_editor.d_spin.setValue(113.5)
    dlg.inline_editor.mount_combo.setCurrentIndex(dlg.inline_editor.mount_combo.findData("din_rail"))
    dlg.inline_editor.save_btn.click()
    _app().processEvents()
    assert "3 confirmed" in dlg.summary_label.text() or "1 missing info" in dlg.summary_label.text()
    assert "1 missing info" in dlg.summary_label.text()
    check = session.reconstruct_site_exit_check(photo_id=photo_id)
    assert check["confirmed_count"] == 3
    assert check["missing_info_count"] == 1
    assert "DO NOT LEAVE SITE YET" in dlg.site_exit_banner_label.text()
    dlg.close()


def test_wizard_step1_badge_editor_updates_site_exit(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QPushButton

    _app()
    session, photo_id = _build_mixed_reconstruct_session(tmp_path)
    wiz = ReconstructWizard(session)
    wiz.resize(1180, 820)
    wiz.show()
    _app().processEvents()
    report = wiz.run_capture_site_exit_check()
    assert report["missing_info_count"] == 2
    psu = next(box for box in report["boxes"] if box["tag"] == "PSU1")
    button = wiz.findChild(QPushButton, f"badge-{psu['item_id']}-ratings")
    assert button is not None
    button.click()
    _app().processEvents()
    assert wiz.capture_inline_editor.isVisible()
    assert wiz.capture_inline_editor.field == "ratings"
    wiz.capture_inline_editor.value_edit.setText("24VDC 5A")
    wiz.capture_inline_editor.save_btn.click()
    _app().processEvents()
    assert "DO NOT LEAVE SITE YET" in wiz.site_exit_banner.text()
    assert _part_item(session, "PSU1").ratings == "24VDC 5A"
    assert session.reconstruct_site_exit_check(photo_id=photo_id)["missing_info_count"] == 2
    wiz.close()


def test_mobile_missing_info_badge_save_updates_counts(tmp_path: Path) -> None:
    import json
    import urllib.error
    import urllib.request
    from panelcad.gui.photo_viewer import MobileCaptureServerDialog

    _app()
    session, photo_id = _build_mixed_reconstruct_session(tmp_path)
    dlg = MobileCaptureServerDialog(session)
    dlg.port_spin.setValue(0)
    dlg.start_server()
    port = dlg.server.port
    base = f"http://127.0.0.1:{port}"
    sid = dlg.server.live_session_id
    try:
        with urllib.request.urlopen(f"{base}/", timeout=5) as resp:
            html = resp.read().decode("utf-8")
        assert 'id="inlineEditor"' in html
        assert "openInlineEditor" in html
        assert "missing-info" in html

        with urllib.request.urlopen(f"{base}/api/sessions/{sid}/status", timeout=5) as resp:
            status = json.loads(resp.read().decode("utf-8"))
        desktop = session.reconstruct_photo_feedback(photo_id=photo_id)
        phone_fields = {
            box["tag"]: [badge["field"] for badge in box.get("badges") or []]
            for box in status["boxes"]
        }
        desktop_fields = {
            box["tag"]: [badge["field"] for badge in box.get("badges") or []]
            for box in desktop["boxes"]
        }
        assert phone_fields == desktop_fields
        assert status["badge_hits"]
        assert status["missing_info_count"] == 2

        with urllib.request.urlopen(
            f"{base}/api/sessions/{sid}/library?q=1489-M1C010", timeout=5
        ) as resp:
            library = json.loads(resp.read().decode("utf-8"))
        pick = next(row for row in library["matches"] if row["catalog"] == "1489-M1C010")
        psu = next(box for box in status["boxes"] if box["tag"] == "PSU1")

        bogus = json.dumps({"field": "ratings", "value": "placeholder"}).encode("utf-8")
        bad_req = urllib.request.Request(
            f"{base}/api/sessions/{sid}/items/{psu['item_id']}/missing-info",
            data=bogus,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(bad_req, timeout=5)
            raise AssertionError("placeholder rating was accepted")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400

        payload = json.dumps(
            {
                "field": "part_number",
                "catalog": pick["catalog"],
                "manufacturer": pick["manufacturer"],
                "library_id": pick["library_id"],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/api/sessions/{sid}/items/{psu['item_id']}/missing-info",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            after = json.loads(resp.read().decode("utf-8"))
        assert after["confirmed_count"] == 3
        assert after["missing_info_count"] == 1
        assert after["safe_to_leave_site"] is False
        assert "DO NOT LEAVE SITE YET" in after["site_exit_banner"]
        assert _part_item(session, "PSU1").part_number == "1489-M1C010"
        assert _part_item(session, "PSU1").ratings == pick["ratings"]
        assert _part_item(session, "PSU1").envelope_confirmed is False
    finally:
        dlg.stop_server()
        dlg.close()
