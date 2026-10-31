"""Terminal strips read from a cabinet photo: geometry, model, overlay, site exit."""

from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication

from panelcad.api.session import Session
from panelcad.core.electrical import is_pe_terminal, terminal_strip_rows
from panelcad.core.wire_numbering import wire_list_rows
from panelcad.io.bom import instance_rows
from panelcad.library.panel import LayoutNotReadyError
from panelcad.reconstruct.feedback import render_annotated_cabinet_photo
from panelcad.reconstruct.models import PhotoRegion
from panelcad.reconstruct.terminal_photo import detect_terminal_strips_at, strips_in_box
from panelcad.reconstruct.vision import StubVision

_TERMINAL_ROW_KEYS = (
    "strip",
    "terminal",
    "ref",
    "sheet",
    "spare",
    "jumper",
    "internal",
    "external",
    "catalog",
    "library_id",
)
_WIRE_LIST_KEYS = (
    "sheet",
    "wire",
    "number",
    "from",
    "to",
    "from_device",
    "from_terminal",
    "to_device",
    "to_terminal",
    "net",
    "label",
    "color",
    "gauge",
    "cable",
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _need_tesseract() -> None:
    pytest.importorskip("pytesseract")
    if shutil.which("tesseract") is None:
        pytest.skip("tesseract is not installed")


def _screw(painter: QPainter, rect: dict) -> None:
    """One dark clamp point near the bottom of a housing. Colour alone is not a block."""

    cx = rect["x"] + max(0, (rect["w"] - 6) // 2)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#141414"))
    painter.fillRect(cx, rect["y"] + rect["h"] - 14, 6, 6, QColor("#141414"))


def _paint_cabinet(path: Path) -> dict:
    """Straight-on cabinet: two strips on one rail, one strip on the next rail.

    Coordinates are the housings that were painted. Nothing here is a customer
    tag or a shop standard — the glyphs are only so a reader can be checked.
    """

    _app()
    width, height = 1100, 680
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#3a4048"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    block_h = 96
    block_w = 26
    gap = 4
    y = 160

    def housing(x: int, w: int, color: str) -> dict:
        painter.fillRect(x, y, w, block_h, QColor(color))
        return {"x": x, "y": y, "w": w, "h": block_h}

    def column(x: int, *, color: str = "#f4f1e8", marker: str = "", panel: str = "", field: str = "") -> dict:
        rect = housing(x, block_w, color)
        painter.setPen(QColor("#111111"))
        painter.setFont(QFont("DejaVu Sans", 11, QFont.Weight.Bold))
        if marker:
            painter.drawText(x, y + 36, block_w, 24, Qt.AlignmentFlag.AlignCenter, marker)
        painter.setFont(QFont("DejaVu Sans", 9, QFont.Weight.Bold))
        if panel:
            painter.fillRect(x, y - 22, block_w, 16, QColor("#fff8dc"))
            painter.drawText(x, y - 22, block_w, 16, Qt.AlignmentFlag.AlignCenter, panel)
        if field:
            painter.fillRect(x, y + block_h + 4, block_w, 16, QColor("#fff8dc"))
            painter.drawText(x, y + block_h + 4, block_w, 16, Qt.AlignmentFlag.AlignCenter, field)
        return rect

    x = 60
    left_plate = housing(x, 7, "#efeae0")
    x += 7 + gap
    first: list[dict] = []
    for marker, panel, field in (
        ("1", "11", "101"),
        ("2", "12", "102"),
        ("3", "13", "103"),
        ("4", "14", "104"),
    ):
        first.append(column(x, marker=marker, panel=panel, field=field))
        x += block_w + gap
    pe = housing(x, block_w, "#1b8f38")
    painter.fillRect(x + 12, y + 8, 8, block_h - 16, QColor("#f2de22"))
    painter.setPen(QColor("#102010"))
    painter.setFont(QFont("DejaVu Sans", 12, QFont.Weight.Bold))
    painter.drawText(x, y + 36, block_w, 24, Qt.AlignmentFlag.AlignCenter, "PE")
    painter.fillRect(x, y - 22, block_w, 16, QColor("#fff8dc"))
    painter.setPen(QColor("#111111"))
    painter.drawText(x, y - 22, block_w, 16, Qt.AlignmentFlag.AlignCenter, "15")
    painter.fillRect(x, y + block_h + 4, block_w, 16, QColor("#fff8dc"))
    painter.drawText(x, y + block_h + 4, block_w, 16, Qt.AlignmentFlag.AlignCenter, "105")
    x += block_w + gap
    right_plate = housing(x, 7, "#efeae0")
    # Orange comb across the first three feed-through blocks, bridging their gaps.
    comb_x = first[0]["x"]
    comb_w = first[2]["x"] + block_w - comb_x
    painter.fillRect(comb_x, y + 6, comb_w, 6, QColor("#e86a12"))
    painter.setPen(QColor("#111111"))
    painter.setFont(QFont("DejaVu Sans", 16, QFont.Weight.Bold))
    painter.fillRect(first[0]["x"], y - 70, 72, 26, QColor("#fff8dc"))
    painter.drawText(first[0]["x"], y - 68, 72, 24, Qt.AlignmentFlag.AlignCenter, "TB1")

    # Dark end stop in the gap, then a second group whose markers were not printed.
    stop_x = right_plate["x"] + right_plate["w"] + 44
    painter.fillRect(stop_x, y + 12, 18, 60, QColor("#14171c"))
    x = right_plate["x"] + right_plate["w"] + 70
    blank = []
    for _index in range(4):
        blank.append(housing(x, block_w, "#f4f1e8"))
        x += block_w + gap

    # Next rail, far enough down that it is not the same row.
    y = 420
    x = 80
    second = []
    for marker, panel, field in (("1", "21", "201"), ("2", "22", "202"), ("3", "23", "203"), ("4", "24", "204")):
        second.append(column(x, marker=marker, panel=panel, field=field))
        x += block_w + gap
    painter.setFont(QFont("DejaVu Sans", 16, QFont.Weight.Bold))
    painter.setPen(QColor("#111111"))
    painter.fillRect(second[0]["x"], y - 70, 72, 26, QColor("#fff8dc"))
    painter.drawText(second[0]["x"], y - 68, 72, 24, Qt.AlignmentFlag.AlignCenter, "TB2")

    # A square device must not become a terminal strip.
    painter.setPen(QPen(QColor("#0f172a"), 2))
    painter.setBrush(QColor("#dbe4ee"))
    painter.drawRect(860, 150, 120, 110)
    for rect in (*first, pe, *blank, *second):
        _screw(painter, rect)
    painter.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    assert image.save(str(path), "PNG")
    return {
        "path": path,
        "tb1_blocks": 5,
        "blank_blocks": 4,
        "tb2_blocks": 4,
        "left_plate": left_plate,
        "right_plate": right_plate,
        "pe": pe,
        "stop_x": stop_x,
    }


def _session(photo: Path) -> tuple[Session, str]:
    session = Session.create("Strip photo")
    added = session.reconstruct_add_photos([photo], role="backpanel")
    photo_id = added["added"][0]["id"]
    session.reconstruct_identify(photo_id=photo_id, vision=StubVision())
    return session, photo_id


def _hits(path: Path) -> list:
    hits = detect_terminal_strips_at(path)
    return sorted(hits, key=lambda hit: (hit.y, hit.x))


def test_detection_groups_rails_blocks_pe_jumpers_and_plates(tmp_path: Path) -> None:
    meta = _paint_cabinet(tmp_path / "cabinet.png")
    hits = _hits(meta["path"])
    assert len(hits) == 3
    upper = [hit for hit in hits if hit.y < 300]
    assert len(upper) == 2
    tb1, blank = upper
    assert len(tb1.blocks) == meta["tb1_blocks"]
    assert tb1.blocks[-1].pe is True
    assert sum(1 for block in tb1.blocks if block.pe) == 1
    assert tb1.end_plate_left and tb1.end_plate_right
    assert tb1.jumpers == [(0, 2)]
    assert [block.x for block in tb1.blocks] == sorted(block.x for block in tb1.blocks)
    assert len(blank.blocks) == meta["blank_blocks"]
    assert blank.end_stop_left is True
    assert blank.confidence < 0.85
    lower = next(hit for hit in hits if hit.y > 300)
    assert len(lower.blocks) == meta["tb2_blocks"]
    assert lower.y > tb1.y + 40
    assert not lower.jumpers


def test_detection_reads_tags_when_tesseract_is_installed(tmp_path: Path) -> None:
    _need_tesseract()
    meta = _paint_cabinet(tmp_path / "cabinet.png")
    hits = _hits(meta["path"])
    named = {hit.name: hit for hit in hits}
    assert set(named) >= {"TB1", "TB2"}
    tb1 = named["TB1"]
    assert [block.marker for block in tb1.blocks[:4]] == ["1", "2", "3", "4"]
    assert [block.panel_wire for block in tb1.blocks] == ["11", "12", "13", "14", "15"]
    assert [block.field_wire for block in tb1.blocks[:4]] == ["101", "102", "103", "104"]
    assert tb1.blocks[4].field_wire in {"105", "?"}
    assert tb1.confidence >= 0.85
    blank = next(hit for hit in hits if hit.name == "?")
    assert all(block.marker == "?" for block in blank.blocks)
    assert all(block.panel_wire == "?" and block.field_wire == "?" for block in blank.blocks)
    tb2 = named["TB2"]
    assert [block.panel_wire for block in tb2.blocks] == ["21", "22", "23", "24"]
    assert [block.field_wire for block in tb2.blocks] == ["201", "202", "203", "204"]


def test_photo_populates_strip_model_without_invented_sizes(tmp_path: Path) -> None:
    _need_tesseract()
    meta = _paint_cabinet(tmp_path / "cabinet.png")
    session, _photo_id = _session(meta["path"])
    # A readable name used to install the sheet by itself. Accept is what writes it.
    assert terminal_strip_rows(session.project, session.catalog) == []
    assert not any(
        sheet.kind == "terminal" and str((sheet.properties or {}).get("photo_terminal_sheet") or "") == "yes"
        for sheet in session.project.sheets
    )
    suggestions = [
        item
        for item in session.project.reconstruct.items
        if str(item.label_text).startswith("terminal strip")
    ]
    assert {item.terminal for item in suggestions} >= {"TB1", "TB2", "?"}
    assert all(float(item.confidence or 0) < 0.99 for item in suggestions)
    tb1 = next(item for item in suggestions if item.terminal == "TB1")
    accepted = session.reconstruct_accept_terminal_strip(tb1.id)
    assert accepted["item"]["tag"] == "TB1"
    tb2_block = next(
        item
        for item in session.project.reconstruct.items
        if item.label_text == "terminal block" and item.tag == "TB2"
    )
    session.reconstruct_accept_terminal_strip(tb2_block.id)
    rows = terminal_strip_rows(session.project, session.catalog)
    assert rows
    assert list(rows[0]) == list(_TERMINAL_ROW_KEYS)
    by_strip: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_strip.setdefault(row["strip"], []).append(row)
    assert "TB1" in by_strip and "TB2" in by_strip
    tb1 = by_strip["TB1"]
    assert [row["terminal"] for row in tb1] == ["1", "2", "3", "4", "5"]
    assert all(row["library_id"] == "iec.terminals.feed_through" for row in rows)
    assert all(row["catalog"] == "" for row in rows)
    pe = tb1[-1]
    comp = session.project.sheet(pe["sheet"]).component(pe["ref"])
    assert is_pe_terminal(comp, session.catalog.get(comp.library_id))
    assert session.catalog.get(comp.library_id).panel.width_mm == 0
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if str(comp.properties.get("photo_terminal") or "") != "yes":
                continue
            assert "width_mm" not in comp.properties
            assert "height_mm" not in comp.properties
            assert "depth_mm" not in comp.properties
    jumped = [row for row in tb1 if row["jumper"]]
    assert [row["terminal"] for row in jumped] == ["1", "2", "3"]
    assert len({row["jumper"] for row in jumped}) == 1
    assert tb1[3]["jumper"] == ""
    pairs = [(row["internal"], row["external"]) for row in tb1]
    assert pairs[:4] == [("11", "101"), ("12", "102"), ("13", "103"), ("14", "104")]
    assert pairs[4][0] == "15"
    assert pairs[4][1] in {"105", "?"}
    assert [(row["internal"], row["external"]) for row in by_strip["TB2"]] == [
        ("21", "201"),
        ("22", "202"),
        ("23", "203"),
        ("24", "204"),
    ]
    assert "?" not in by_strip
    suggestions = [
        item
        for item in session.project.reconstruct.items
        if str(item.label_text).startswith("terminal strip") and float(item.confidence or 0) < 0.85
    ]
    assert suggestions
    assert all(item.terminal in {"", "?"} for item in suggestions)
    plates = [
        comp
        for sheet in session.project.sheets
        for comp in sheet.components
        if comp.properties.get("accessory") == "end_plate" and comp.properties.get("strip") == "TB1"
    ]
    assert {comp.properties.get("end") for comp in plates} == {"left", "right"}
    assert instance_rows(session.project, session.catalog) == []
    wire_rows = wire_list_rows(session.project)
    assert wire_rows
    assert list(wire_rows[0]) == list(_WIRE_LIST_KEYS)
    listed = {row["strip"] for row in session.list_terminal_strips()}
    assert listed == {"TB1", "TB2"}


def test_overlay_and_site_exit_flag_unreadable_strips(tmp_path: Path) -> None:
    meta = _paint_cabinet(tmp_path / "cabinet.png")
    session, photo_id = _session(meta["path"])
    before = tmp_path / "overlay-before.png"
    render_annotated_cabinet_photo(session, photo_id=photo_id, include_banner=False).save(str(before), "PNG")
    # A second pass draws the same boxes; the saved pair is the bare photo and the boxed photo.
    bare = QImage(str(meta["path"]))
    bare_path = tmp_path / "overlay-bare.png"
    assert bare.save(str(bare_path), "PNG")

    report = session.reconstruct_photo_feedback(photo_id=photo_id)
    strips = [box for box in report["boxes"] if str(box["label_text"]).startswith("terminal strip")]
    blocks = [box for box in report["boxes"] if box["label_text"] == "terminal block"]
    assert strips
    assert blocks
    assert all(box["outline_only"] for box in blocks)
    assert report["missing_info_count"] == sum(
        1 for box in strips if box["status_category"] == "missing_info" and not box["closeup_photo_id"]
    )
    assert report["missing_info_count"] <= len(strips)
    assert report["missing_info_count"] < len(blocks)
    assert all(box["status_category"] != "confirmed" or box["confidence"] >= 0.85 for box in strips)
    blank = next(box for box in strips if box["confidence"] < 0.85)
    assert blank["status_category"] == "missing_info"
    assert any("?" in reason for reason in blank["missing_reasons"])
    assert blank["needs_closeup"] is True
    store = session.project.reconstruct
    store.item(blank["item_id"]).closeup_photo_id = "closeup"
    linked = session.reconstruct_photo_feedback(photo_id=photo_id)
    linked_blank = next(box for box in linked["boxes"] if box["item_id"] == blank["item_id"])
    assert linked_blank["needs_closeup"] is False
    assert linked["missing_info_count"] == report["missing_info_count"] - 1
    check = session.reconstruct_site_exit_check(photo_id=photo_id)
    assert check["safe_to_leave_site"] is False
    assert "DO NOT LEAVE SITE YET" in check["site_exit_banner"]
    assert "terminal strip" in check["site_exit_banner"]
    assert any("close-up" in line and "terminal strip" in line for line in check["on_site_shot_list"])
    strip_lines = [line for line in check["on_site_shot_list"] if "terminal strip" in line]
    assert len(strip_lines) <= len(strips)
    assert not any("terminal block" in line for line in check["on_site_shot_list"])
    after = tmp_path / "overlay-after.png"
    boxed = render_annotated_cabinet_photo(session, photo_id=photo_id)
    assert boxed.save(str(after), "PNG")
    assert after.stat().st_size > bare_path.stat().st_size
    artifact_dir = Path("/opt/cursor/artifacts")
    if artifact_dir.is_dir():
        bare.save(str(artifact_dir / "terminal-strip-overlay-before.png"), "PNG")
        boxed.save(str(artifact_dir / "terminal-strip-overlay-after.png"), "PNG")


def test_drawn_box_adds_or_adjusts_a_missed_strip(tmp_path: Path) -> None:
    _app()
    image = QImage(640, 360, QImage.Format.Format_RGB32)
    image.fill(QColor("#3a4048"))
    painter = QPainter(image)
    y, w, h, gap = 80, 24, 96, 4
    x = 40
    rects = []
    for _index in range(4):
        painter.fillRect(x, y, w, h, QColor("#f4f1e8"))
        rects.append({"x": x, "y": y, "w": w, "h": h})
        x += w + gap
    for rect in rects:
        _screw(painter, rect)
    painter.end()
    path = tmp_path / "quiet.png"
    assert image.save(str(path), "PNG")
    session = Session.create("Missed strip")
    added = session.reconstruct_add_photos([path], role="backpanel")
    photo_id = added["added"][0]["id"]

    with pytest.raises(ValueError, match="how many blocks"):
        session.reconstruct_add_missed_strip(photo_id, {"x": 400, "y": 30, "w": 180, "h": 120})
    assert terminal_strip_rows(session.project, session.catalog) == []
    assert not any(str(item.label_text).startswith("terminal strip") for item in session.project.reconstruct.items)

    typed = session.reconstruct_add_missed_strip(
        photo_id,
        {"x": 400, "y": 30, "w": 180, "h": 120},
        block_count=4,
    )
    item = typed["item"]
    assert len(typed["blocks"]) == 4
    assert terminal_strip_rows(session.project, session.catalog)
    assert typed["feedback_box"]["needs_closeup"] is True
    assert typed["feedback"]["missing_info_count"] == 1

    moved = session.reconstruct_update_device_box(item["id"], box={"x": 20, "y": 50, "w": 200, "h": 140})
    found = moved["item"]
    assert found["region"]["x"] < 80
    rows = terminal_strip_rows(session.project, session.catalog)
    assert len(rows) == 4
    assert [row["terminal"] for row in rows] == ["1", "2", "3", "4"]
    assert all(row["internal"] == "?" and row["external"] == "?" for row in rows)
    session.reconstruct_identify(photo_id=photo_id, vision=StubVision())
    assert len(terminal_strip_rows(session.project, session.catalog)) == 4


def test_blurry_grey_strip_counts_from_pitch_and_one_closeup(tmp_path: Path) -> None:
    """Grey push-in blocks with marker cards stay countable after a blur.

    A relay part number is not a strip tag. When the pitch cannot be read,
    Accept waits for a block count instead of writing an empty strip, and the
    site-exit banner counts that strip once.
    """

    import cv2

    _app()
    image = QImage(640, 280, QImage.Format.Format_RGB32)
    image.fill(QColor("#6e747a"))
    painter = QPainter(image)
    y, bw, bh, gap, count = 70, 20, 90, 3, 8
    x = 40
    for _index in range(count):
        painter.fillRect(x, y, bw, bh, QColor("#7c848c"))
        painter.fillRect(x + 3, y + 18, bw - 6, 16, QColor("#d5cbb8"))
        painter.fillRect(x + bw, y + 8, gap, bh - 16, QColor("#5c636a"))
        x += bw + gap
    painter.end()
    path = tmp_path / "grey.png"
    assert image.save(str(path), "PNG")
    blurred = cv2.GaussianBlur(cv2.imread(str(path)), (0, 0), 2.4)
    cv2.imwrite(str(path), blurred)
    assert detect_terminal_strips_at(path) == []

    region = {"x": 30, "y": 55, "w": 210, "h": 120}
    session = Session.create("Grey strip")
    photo_id = session.reconstruct_add_photos([path], role="backpanel")["added"][0]["id"]
    refused = session.reconstruct_add_missed_strip(photo_id, region, tag="RM85")
    assert refused["item"]["terminal"] not in {"RM85"}
    assert "RM85" not in str(refused["item"]["tag"])
    assert len(refused["blocks"]) == count
    rows = terminal_strip_rows(session.project, session.catalog)
    assert len(rows) == count
    assert {row["strip"] for row in rows} != {"RM85"}
    report = session.reconstruct_photo_feedback(photo_id=photo_id)
    strips = [box for box in report["boxes"] if str(box["label_text"]).startswith("terminal strip")]
    assert len(strips) == 1
    assert strips[0]["needs_closeup"] is True
    assert report["missing_info_count"] == 1
    check = session.reconstruct_site_exit_check(photo_id=photo_id)
    strip_lines = [line for line in check["on_site_shot_list"] if "terminal strip" in line]
    assert len(strip_lines) == 1
    assert "1 terminal strip(s) need a close-up" in check["site_exit_banner"]

    named = Session.create("Named grey strip")
    named_photo = named.reconstruct_add_photos([path], role="backpanel")["added"][0]["id"]
    xt9 = named.reconstruct_add_missed_strip(named_photo, region, tag="XT9")
    assert xt9["item"]["terminal"] == "XT9"
    assert len(xt9["blocks"]) == count

    blank = QImage(320, 180, QImage.Format.Format_RGB32)
    blank.fill(QColor("#6e747a"))
    blank_path = tmp_path / "flat.png"
    assert blank.save(str(blank_path), "PNG")
    flat = Session.create("Flat strip")
    flat_photo = flat.reconstruct_add_photos([blank_path], role="backpanel")["added"][0]["id"]
    flat_box = {"x": 20, "y": 30, "w": 200, "h": 100}
    with pytest.raises(ValueError, match="how many blocks"):
        flat.reconstruct_add_missed_strip(flat_photo, flat_box, tag="TB1")
    assert terminal_strip_rows(flat.project, flat.catalog) == []
    counted = flat.reconstruct_add_missed_strip(flat_photo, flat_box, tag="TB1", block_count=5)
    assert counted["item"]["terminal"] == "TB1"
    assert len(counted["blocks"]) == 5
    counted_rows = terminal_strip_rows(flat.project, flat.catalog)
    assert [row["terminal"] for row in counted_rows] == ["1", "2", "3", "4", "5"]
    assert all(row["internal"] == "?" and row["external"] == "?" for row in counted_rows)
    flat_report = flat.reconstruct_photo_feedback(photo_id=flat_photo)
    assert flat_report["missing_info_count"] == 1
    flat_check = flat.reconstruct_site_exit_check(photo_id=flat_photo)
    assert len([line for line in flat_check["on_site_shot_list"] if "terminal strip" in line]) == 1


def test_cabinet_devices_without_terminal_evidence_are_not_strips(tmp_path: Path) -> None:
    """PLC modules, relays, breakers, and plain labels are not terminal strips."""

    _app()
    image = QImage(980, 640, QImage.Format.Format_RGB32)
    image.fill(QColor("#4a515a"))
    painter = QPainter(image)
    painter.setPen(QPen(QColor("#111111"), 2))
    # DIN rail under a row of wide PLC-style modules.
    painter.fillRect(40, 210, 700, 14, QColor("#c5c8cc"))
    modules = (("#1b8f38", "PLC"), ("#111111", "KV"), ("#d8dde6", "I/O"), ("#5b2a86", "NET"))
    x = 50
    for color, word in modules:
        painter.fillRect(x, 80, 78, 120, QColor(color))
        painter.setPen(QColor("#f8fafc"))
        painter.setFont(QFont("DejaVu Sans", 12, QFont.Weight.Bold))
        painter.drawText(x + 8, 140, word)
        x += 96
    # Relay and breaker.
    painter.setPen(QPen(QColor("#111111"), 2))
    painter.setBrush(QColor("#f59e0b"))
    painter.drawRoundedRect(70, 340, 70, 70, 6, 6)
    painter.setBrush(QColor("#1e293b"))
    painter.drawRect(180, 320, 54, 96)
    painter.setBrush(QColor("#e2e8f0"))
    painter.drawRect(196, 332, 22, 28)
    # Stickers. Light, green, and yellow by themselves are not blocks.
    painter.fillRect(420, 360, 90, 28, QColor("#f2de22"))
    painter.fillRect(530, 360, 70, 36, QColor("#1b8f38"))
    painter.setPen(QColor("#111111"))
    painter.drawText(428, 378, "DATA")
    # A neat row of plain housings with no screw or spring point.
    x = 40
    for _index in range(6):
        painter.fillRect(x, 480, 22, 90, QColor("#f4f1e8"))
        x += 26
    # And a green row, also with no fastener.
    x = 280
    for _index in range(5):
        painter.fillRect(x, 480, 22, 90, QColor("#1b8f38"))
        x += 26
    painter.end()
    path = tmp_path / "devices.png"
    assert image.save(str(path), "PNG")
    assert detect_terminal_strips_at(path) == []


def test_garbled_label_does_not_become_a_strip_name(tmp_path: Path) -> None:
    _app()
    image = QImage(420, 260, QImage.Format.Format_RGB32)
    image.fill(QColor("#3a4048"))
    painter = QPainter(image)
    y, w, h, gap = 80, 24, 96, 4
    x = 30
    rects = []
    for _index in range(4):
        painter.fillRect(x, y, w, h, QColor("#f4f1e8"))
        rects.append({"x": x, "y": y, "w": w, "h": h})
        x += w + gap
    for rect in rects:
        _screw(painter, rect)
    painter.setPen(QColor("#111111"))
    painter.setFont(QFont("DejaVu Sans", 14, QFont.Weight.Bold))
    painter.fillRect(30, 28, 140, 26, QColor("#fff8dc"))
    painter.drawText(30, 30, 140, 24, Qt.AlignmentFlag.AlignCenter, "PROFINET")
    painter.end()
    path = tmp_path / "word.png"
    assert image.save(str(path), "PNG")
    hits = detect_terminal_strips_at(path)
    assert len(hits) == 1
    assert hits[0].name == "?"
    assert hits[0].confidence < 0.85


def test_drawn_box_named_xt9_replaces_suggestion_without_dimensions(tmp_path: Path) -> None:
    """A named box replaces the overlapping '?' suggestion and writes the sheet."""

    _app()
    image = QImage(640, 360, QImage.Format.Format_RGB32)
    image.fill(QColor("#3a4048"))
    painter = QPainter(image)
    y, w, h, gap = 80, 24, 96, 4
    x = 40
    rects = []
    for _index in range(4):
        painter.fillRect(x, y, w, h, QColor("#f4f1e8"))
        rects.append({"x": x, "y": y, "w": w, "h": h})
        x += w + gap
    for rect in rects:
        _screw(painter, rect)
    painter.end()
    path = tmp_path / "suggest.png"
    assert image.save(str(path), "PNG")
    session = Session.create("Accept strip")
    photo_id = session.reconstruct_add_photos([path], role="backpanel")["added"][0]["id"]
    session.reconstruct_identify(photo_id=photo_id, vision=StubVision())
    before = [
        item
        for item in session.project.reconstruct.items
        if str(item.label_text).startswith("terminal strip")
    ]
    assert len(before) == 1
    assert before[0].terminal in {"", "?"}
    assert terminal_strip_rows(session.project, session.catalog) == []
    old_id = before[0].id
    try:
        added = session.reconstruct_add_missed_strip(
            photo_id,
            {"x": 20, "y": 50, "w": 200, "h": 150},
            tag="XT9",
        )
    except LayoutNotReadyError as exc:  # pragma: no cover - the failure this test locks out
        raise AssertionError(f"accept asked for dimensions: {exc}") from exc
    assert added["item"]["terminal"] == "XT9"
    remaining = [
        item
        for item in session.project.reconstruct.items
        if str(item.label_text).startswith("terminal strip")
    ]
    assert len(remaining) == 1
    assert remaining[0].terminal == "XT9"
    assert remaining[0].id == added["item"]["id"]
    assert not any(
        item.id == old_id and str(item.terminal or "") in {"", "?"}
        for item in session.project.reconstruct.items
        if str(item.label_text).startswith("terminal strip")
    )
    rows = terminal_strip_rows(session.project, session.catalog)
    assert [row["strip"] for row in rows] == ["XT9", "XT9", "XT9", "XT9"]
    assert [row["terminal"] for row in rows] == ["1", "2", "3", "4"]
    sheet = session.project.sheet(rows[0]["sheet"])
    assert sheet.kind == "terminal"
    assert sheet.strip == "XT9"
    for comp in sheet.components:
        if str(comp.properties.get("photo_terminal") or "") != "yes":
            continue
        assert "width_mm" not in comp.properties
        assert "height_mm" not in comp.properties
        assert "depth_mm" not in comp.properties


def test_tight_box_counts_blocks_the_colour_scan_misses(tmp_path: Path) -> None:
    """Salmon housings with no screw are not auto-detected. A tight box still counts them."""

    _app()
    image = QImage(520, 280, QImage.Format.Format_RGB32)
    image.fill(QColor("#5c656e"))
    painter = QPainter(image)
    y, w, h, gap = 70, 22, 110, 3
    x = 36
    for _index in range(6):
        painter.fillRect(x, y, w, h, QColor("#e7a090"))
        painter.fillRect(x + 4, y + 46, w - 8, 8, QColor("#c48478"))
        x += w + gap
    painter.end()
    path = tmp_path / "salmon.png"
    assert image.save(str(path), "PNG")
    assert detect_terminal_strips_at(path) == []
    region = PhotoRegion(x=38, y=74, w=6 * (w + gap) - gap - 4, h=h - 8)
    assert strips_in_box(path, region) == []
    session = Session.create("Tight strip")
    photo_id = session.reconstruct_add_photos([path], role="backpanel")["added"][0]["id"]
    added = session.reconstruct_add_missed_strip(
        photo_id,
        {"x": region.x, "y": region.y, "w": region.w, "h": region.h},
        tag="XT9",
    )
    assert len(added["blocks"]) == 6
    rows = terminal_strip_rows(session.project, session.catalog)
    assert [row["terminal"] for row in rows] == ["1", "2", "3", "4", "5", "6"]
    assert {row["strip"] for row in rows} == {"XT9"}
    assert added["feedback_box"]["status_category"] != "confirmed"


def _post_json(url: str, payload: dict) -> urllib.error.HTTPError:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as exc:
        return exc
    raise AssertionError(f"POST {url} should have failed")


def test_missed_box_post_rejects_bad_numbers(tmp_path: Path) -> None:
    from panelcad.reconstruct.capture_web import CaptureWebServer

    _app()
    server = CaptureWebServer(tmp_path / "cap", port=0)
    server.start()
    try:
        bad_payloads = (
            {"x": "nope", "y": 10, "w": 40, "h": 40},
            {"x": True, "y": 10, "w": 40, "h": 40},
            {"region": "10,10,40,40", "x": 10, "y": 10, "w": 40, "h": 40},
            {"x": 0, "y": 0, "w": 90000, "h": 40},
            {"x": 1, "y": 1, "w": 40, "h": 40},
        )
        for route in ("missed-strip", "missed-device"):
            for payload in bad_payloads:
                caught = _post_json(f"{server.base_url}/api/sessions/missing/{route}", payload)
                assert caught.code == 400, payload

        image = QImage(120, 80, QImage.Format.Format_RGB32)
        image.fill(QColor("#3a4048"))
        photo = tmp_path / "tiny.png"
        assert image.save(str(photo), "PNG")
        summary = server.workspace.create_or_get_session("Box check", session_id="boxcheck")
        sid = summary["session_id"]
        server.workspace.add_uploaded_photo(
            sid,
            filename="tiny.png",
            image_bytes=photo.read_bytes(),
            analyze=False,
        )
        for route in ("missed-strip", "missed-device"):
            for payload in (
                {"x": 200, "y": 10, "w": 20, "h": 20},
                {"x": 0, "y": 0, "w": 400, "h": 40},
                {"x": -4, "y": 2, "w": 30, "h": 30},
            ):
                caught = _post_json(f"{server.base_url}/api/sessions/{sid}/{route}", payload)
                assert caught.code == 400, payload
        plain = {"x": 8, "y": 8, "w": 90, "h": 60}
        missed = _post_json(f"{server.base_url}/api/sessions/{sid}/missed-strip", plain)
        assert missed.code == 400
        assert "how many blocks" in missed.read().decode().lower()
        boolean = _post_json(
            f"{server.base_url}/api/sessions/{sid}/missed-strip",
            {**plain, "block_count": True},
        )
        assert boolean.code == 400
        request = urllib.request.Request(
            f"{server.base_url}/api/sessions/{sid}/missed-strip",
            data=json.dumps({**plain, "block_count": 4, "tag": "XT1"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
    finally:
        server.stop()


def test_blank_photo_detects_no_strip(tmp_path: Path) -> None:
    _app()
    image = QImage(400, 240, QImage.Format.Format_RGB32)
    image.fill(QColor("#3a4048"))
    path = tmp_path / "empty.png"
    assert image.save(str(path), "PNG")
    assert detect_terminal_strips_at(path) == []
