"""Free-form NEMA/JIC print import.

GE sheet 9C stays on the existing gate
``test_sheet_9c_tile04_grok_reply_builds_27_connections`` (27 connections).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from PIL import Image

_TESTS = str(Path(__file__).resolve().parent)
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)
from test_importer import _font

_FREEFORM = (
    "FU1-7",
    "FU-1A",
    "FU1A1",
    "CR410",
    "DRIVE1",
    "MOT1",
    "DISC1",
    "PWS",
    "HMI",
    "S-204",
    "LS-300",
    "TB2-24",
    "PLT001",
    "PBLT001",
)
_PLC = ("I0.0", "I0.X", "I17.3", "Q0.1", "Q0.X", "Q40.2L", "Q40.XL")
_STILL_DEVICES = ("10M-G", "10OL-G", "10G", "1M", "2CR", "K1", "Q1")
# Printed prefix -> catalog ref_prefix. HMI is matched by name; its catalog prefix is P.
_PREFIX = {
    "FU1-7": "FU",
    "CR410": "CR",
    "DRIVE1": "VFD",
    "MOT1": "MTR",
    "DISC1": "DISC",
    "PWS": "PS",
    "S-204": "S",
    "LS-300": "LS",
    "TB2-24": "TB",
    "PLT001": "PL",
    "PBLT001": "PB",
}


def test_freeform_nema_tags_parse_and_old_tags_stay():
    from panelcad.importer.read import (
        TAG_RE,
        component_ref,
        is_device_tag,
        is_nema_line_ref,
        is_plc_address,
        is_wire_label,
        normalize_device_tag,
    )
    from panelcad.importer.symbols import catalog_prefix, print_tag_placeable, supported_tag_family, tag_prefix

    for tag in _FREEFORM + _PLC + _STILL_DEVICES:
        assert is_device_tag(tag), tag
        assert not is_wire_label(tag), tag
    for tag in _FREEFORM + _STILL_DEVICES:
        assert TAG_RE.match(tag), tag
    for tag in _PLC:
        assert is_plc_address(tag), tag
        assert TAG_RE.match(tag), tag
    assert normalize_device_tag("Q 40.2 L") == "Q40.2L"
    assert normalize_device_tag("i0.x") == "I0.X"
    assert is_device_tag("i0.x")
    assert is_device_tag("Q 40.2 L")
    assert component_ref("I0.0") == "I0_0"
    assert component_ref("Q40.2L") == "Q40_2L"
    assert component_ref("FU1-7") == "FU1-7"
    assert component_ref("Q 40.2 L") == "Q40_2L"

    assert is_wire_label("10T1") and not is_device_tag("10T1")
    assert is_wire_label("9B16B") and not is_device_tag("9B16B")
    assert is_wire_label("L1") and is_wire_label("410")
    assert is_nema_line_ref("6-410") and is_nema_line_ref("(410)")
    assert not is_device_tag("6-410") and not is_device_tag("(410)")
    assert not is_wire_label("6-410")

    assert tag_prefix("FU1-7") == "FU"
    assert tag_prefix("FU-1A") == "FU"
    assert tag_prefix("S-204") == "S"
    assert tag_prefix("LS-300") == "LS"
    assert tag_prefix("TB2-24") == "TB"
    assert tag_prefix("10M-G") == "M"
    assert tag_prefix("10OL-G") == "OL"
    assert tag_prefix("Q40.2L") == ""
    assert tag_prefix("Q1") == "Q"
    assert catalog_prefix("DRIVE1") == "VFD"
    assert catalog_prefix("MOT1") == "MTR"
    assert catalog_prefix("PWS") == "PS"
    assert catalog_prefix("PLT001") == "PL"
    assert catalog_prefix("PBLT001") == "PB"
    assert supported_tag_family("DRIVE1") == "drive"
    assert supported_tag_family("HMI") == "hmi"
    assert supported_tag_family("Q1") == "breaker"
    assert supported_tag_family("Q40.2L") == ""
    assert supported_tag_family("I0.0") == ""
    for bare in ("A1", "R1", "12T", "10C"):
        assert is_device_tag(bare)
        assert supported_tag_family(bare) == ""
        assert print_tag_placeable({"tag": bare, "source": "model"}) is False


def test_freeform_tags_are_not_rejected_as_unparsed():
    from panelcad.importer.vision import detections_to_facts, filter_vision_detections
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    tile = {"x": 0, "y": 0, "w": 200, "h": 160, "id": "tile_00"}

    def row(tag: str, *, symbol: str = "", x: float = 10) -> dict:
        return {
            "tag": tag,
            "wire_number": "",
            "symbol_class": symbol,
            "bbox": [x, 12, 18, 10],
            "confidence": 0.9,
        }

    rows = [row(tag, x=12 + index * 4) for index, tag in enumerate(_FREEFORM + ("I0.0", "Q40.2L", "Q 40.2 L"))]
    rows.append(row("6-410", x=20))
    rows.append(row("(410)", x=40))
    kept = filter_vision_detections(rows, catalog, tile)
    by_tag = {str(item.get("tag") or ""): item for item in kept}
    for tag in _FREEFORM:
        assert tag in by_tag, tag
        assert not by_tag[tag].get("tag_unparsed"), tag
    assert by_tag["I0.0"].get("tag_unparsed") is not True
    assert by_tag["Q40.2L"].get("tag_unparsed") is not True
    assert "Q 40.2 L" not in by_tag
    line_refs = {item.get("line_ref") for item in kept}
    assert line_refs >= {"6-410", "(410)"}
    facts = detections_to_facts(kept, tile, catalog, standard="NEMA/ANSI")
    placed_tags = {device["tag"] for device in facts["devices"]}
    assert set(_FREEFORM) <= placed_tags
    assert "6-410" not in placed_tags and "(410)" not in placed_tags
    assert {item["text"] for item in facts["xrefs"]} >= {"6-410", "(410)"}
    assert all(item["kind"] == "nema_line" for item in facts["xrefs"])
    libraries = {device["tag"]: device["library_id"] for device in facts["devices"]}
    # S and TB have no NEMA row with those ref letters; the family still lands on nema.*.
    # HMI has no NEMA symbol at all.
    _FAMILY_ONLY = {"S-204", "TB2-24"}
    for tag, prefix in _PREFIX.items():
        assert str(libraries[tag]).startswith("nema."), (tag, libraries[tag])
        if tag not in _FAMILY_ONLY:
            assert catalog.get(libraries[tag]).ref_prefix.upper() == prefix, (tag, libraries[tag])
    assert libraries["HMI"] == ""


def _sheet(devices: list[dict], wires: list[dict], *, sheet_id: str = "power") -> dict:
    return {
        "standard": "NEMA/ANSI",
        "sheets": [
            {
                "id": sheet_id,
                "kind": "schematic",
                "width_mm": 431.8,
                "height_mm": 279.4,
                "devices": devices,
                "wires": wires,
            }
        ],
    }


def _device(tag: str, x: float, **extra) -> dict:
    device = {
        "tag": tag,
        "type": extra.pop("type", ""),
        "symbol": extra.pop("symbol", ""),
        "library_id": extra.pop("library_id", ""),
        "x": x,
        "y": 40.0,
        "w": 28,
        "h": 18,
        "confidence": 0.93,
        "source": "model",
    }
    device.update(extra)
    return device


def _wire(number: str, frm: str, from_pin: str, to: str, to_pin: str) -> dict:
    return {
        "number": number,
        "from_tag": frm,
        "from_pin": from_pin,
        "to_tag": to,
        "to_pin": to_pin,
        "confidence": 0.9,
    }


def test_needs_info_device_keeps_its_wire():
    from panelcad.importer.build import build_session
    from panelcad.io.bom import grouped_rows
    from panelcad.library.catalog import default_catalog
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    catalog = default_catalog()
    session, report = build_session(
        _sheet(
            [
                _device("K1", 40, library_id="iec.contactors.ac3_9a", symbol="contactor", type="contactor"),
                _device("A1", 120, symbol="contactor", type="contactor"),
                _device("I0.0", 200, symbol="plc", type="plc"),
            ],
            [
                _wire("101", "K1", "A1", "A1", "13"),
                _wire("102", "K1", "A2", "I0.0", "0"),
            ],
        ),
        catalog=catalog,
    )
    sheet = session.project.sheet("power")
    ids = {comp.id for comp in sheet.components}
    assert {"K1", "A1", "I0_0"} <= ids
    anchor = sheet.component("A1")
    plc = sheet.component("I0_0")
    assert anchor.library_id == NEEDS_INFO_LIBRARY_ID
    assert plc.library_id == NEEDS_INFO_LIBRARY_ID
    assert anchor.properties.get("bom") == "no"
    assert "13" in str(anchor.properties.get("print_import_pins"))
    assert "0" in str(plc.properties.get("print_import_pins"))
    pairs = {frozenset({str(wire.frm).upper(), str(wire.to).upper()}) for wire in sheet.wires}
    assert sheet.component("K1").library_id.startswith("nema.")
    assert frozenset({"K1.A1", "A1.13"}) in pairs
    assert frozenset({"K1.A2", "I0_0.0"}) in pairs
    assert any(row["tag"] == "A1" and row["reason"] == "needs_info" for row in report["skipped"])
    refs = " ".join(str(row.get("ref") or "") for row in grouped_rows(session.project, catalog))
    assert "A1" not in refs.split()
    assert "I0_0" not in refs

    both, _both_report = build_session(
        _sheet(
            [
                _device("A1", 40, symbol="relay"),
                _device("R1", 100, symbol="relay"),
            ],
            [_wire("201", "A1", "13", "R1", "14")],
        ),
        catalog=catalog,
    )
    linked = both.project.sheet("power")
    assert {comp.id for comp in linked.components} == {"A1", "R1"}
    assert all(comp.library_id == NEEDS_INFO_LIBRARY_ID for comp in linked.components)
    linked_pairs = {frozenset({str(wire.frm).upper(), str(wire.to).upper()}) for wire in linked.wires}
    assert frozenset({"A1.13", "R1.14"}) in linked_pairs

    bare, bare_report = build_session(
        _sheet([_device("A1", 40), _device("12T", 90), _device("R1", 140)], []),
        catalog=catalog,
    )
    bare_parts = [comp for sheet in bare.project.sheets for comp in sheet.components]
    assert {comp.id for comp in bare_parts} == {"A1", "12T", "R1"}
    assert all(comp.library_id == NEEDS_INFO_LIBRARY_ID for comp in bare_parts)
    assert all("width_mm" not in (comp.properties or {}) for comp in bare_parts)
    assert all(str((comp.properties or {}).get("bom") or "") == "no" for comp in bare_parts)
    from panelcad.library.panel import effective_footprint

    for comp in bare_parts:
        footprint = effective_footprint(bare.catalog.get(comp.library_id), comp)
        assert footprint.width_mm == 0 and footprint.height_mm == 0 and footprint.depth_mm == 0
    bare_refs = " ".join(str(row.get("ref") or "") for row in grouped_rows(bare.project, catalog))
    assert "A1" not in bare_refs.split() and "12T" not in bare_refs.split() and "R1" not in bare_refs.split()
    skipped = {row["tag"] for row in bare_report["skipped"]}
    assert {"A1", "12T", "R1"} <= skipped


def test_freeform_symbols_place_and_connect():
    from panelcad.importer.build import build_session
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    tags = [*_PREFIX, "HMI"]
    devices = [_device(tag, 30 + index * 24) for index, tag in enumerate(tags)]
    session, report = build_session(
        _sheet(devices, [_wire("301", "FU1-7", "2", "CR410", "A1")]),
        catalog=catalog,
    )
    sheet = session.project.sheet("power")
    placed = {comp.id: comp.library_id for comp in sheet.components}
    for tag, prefix in _PREFIX.items():
        assert tag in placed, (tag, report["skipped"])
        assert placed[tag].startswith("nema."), (tag, placed[tag])
        if tag not in {"S-204", "TB2-24"}:
            assert catalog.get(placed[tag]).ref_prefix.upper() == prefix, (tag, placed[tag])
    assert placed["HMI"] == "reconstruct.needs_info"
    assert "width_mm" not in (sheet.component("HMI").properties or {})
    assert any(row["tag"] == "HMI" and row["reason"] == "needs_info" for row in report["skipped"])
    assert all(not lib.startswith("iec.") for lib in placed.values())
    pairs = {frozenset({str(wire.frm).upper(), str(wire.to).upper()}) for wire in sheet.wires}
    assert frozenset({"FU1-7.2", "CR410.A1"}) in pairs


def test_photo_72_dpi_uses_a_real_sheet(tmp_path: Path):
    from panelcad.importer.ingest import _STANDARD_SHEETS_MM, ingest_paths, resolve_sheet_size_mm
    from panelcad.importer.workspace import Workspace

    photo = tmp_path / "phone.jpg"
    Image.new("RGB", (4000, 3000), (255, 255, 255)).save(photo, dpi=(72, 72), quality=85)
    image = Image.open(photo)
    size, note = resolve_sheet_size_mm(image)
    allowed = set()
    for _name, short, long in _STANDARD_SHEETS_MM:
        allowed.add((round(short, 3), round(long, 3)))
        allowed.add((round(long, 3), round(short, 3)))
    assert size in allowed
    assert max(size) <= 1120
    assert "72 DPI" in note
    explicit, explicit_note = resolve_sheet_size_mm(image, dpi=72)
    assert explicit_note == ""
    assert abs(explicit[0] - 4000 / 72 * 25.4) < 0.2
    assert abs(explicit[1] - 3000 / 72 * 25.4) < 0.2

    letter = tmp_path / "letter.png"
    Image.new("RGB", (612, 792), (255, 255, 255)).save(letter, dpi=(72, 72))
    letter_size, letter_note = resolve_sheet_size_mm(Image.open(letter))
    assert letter_note == ""
    assert abs(letter_size[0] - 215.9) < 0.2
    assert abs(letter_size[1] - 279.4) < 0.2

    tabloid = tmp_path / "tabloid.png"
    Image.new("RGB", (1100, 1700), (255, 255, 255)).save(tabloid, dpi=(100, 100))
    tabloid_size, tabloid_note = resolve_sheet_size_mm(Image.open(tabloid))
    assert tabloid_note == ""
    assert abs(tabloid_size[0] - 279.4) < 0.2
    assert abs(tabloid_size[1] - 431.8) < 0.2

    pages = ingest_paths([photo], Workspace(tmp_path / "dpi-ws"))
    page = pages[0]
    assert page.get("dpi_tag_ignored") is True
    assert "72 DPI" in str(page.get("dpi_note") or "")
    assert max(float(page["width_mm"]), float(page["height_mm"])) <= 1120


def _band(words: list[str], y: float) -> list[dict]:
    return [{"text": text, "x": 24 + index * 14, "y": y, "h": 3.0, "w": 12} for index, text in enumerate(words)]


def test_title_block_sheet_numbers():
    from panelcad.importer.read import read_title_block

    height = 431.8
    of_sheet = read_title_block(_band("6 OF 6".split(), 400), height)
    assert of_sheet["sheet_number"] == "6"
    assert of_sheet["sheet_count"] == "6"
    named = read_title_block(_band("SHEET 6 OF 9".split(), 400), height)
    assert named["sheet_number"] == "6"
    assert named["sheet_count"] == "9"
    legacy = read_title_block(
        _band("ELEMENTARY DIAGRAM JOB NO. 380L3503 SH.NO. 9C".split(), 400),
        height,
    )
    assert legacy["sheet_number"] == "9C"
    assert legacy["drawing_number"] == "380L3503"
    note = _band("SEE 2 OF 3 NOTES".split(), 40) + _band("SH.NO. 9C".split(), 400)
    assert read_title_block(note, height)["sheet_number"] == "9C"
    outside = read_title_block(_band("2 OF 3".split(), 40), height)
    assert outside["sheet_number"] == ""
    rotated = read_title_block(_band("SHEET NO 7".split(), 80), height)
    assert rotated["sheet_number"] == "7"
    assert rotated["sheet_number_needs_info"] is False


def test_sheet_numbers_cover_common_title_blocks_and_do_not_guess():
    """Formats from different title blocks. A date, a scale, or a second number is not a guess."""

    from panelcad.importer.read import read_title_block, sheet_fields_from_text

    height = 431.8
    assert sheet_fields_from_text("SHEET 3 OF 9", in_title_block=True) == {
        "sheet_number": "3",
        "sheet_count": "9",
        "sheet_number_needs_info": False,
    }
    assert sheet_fields_from_text("DWG E-1042 SH 3", in_title_block=True)["sheet_number"] == "3"
    assert sheet_fields_from_text("DWG 4512-A SH6", in_title_block=True)["sheet_number"] == "6"
    assert sheet_fields_from_text("PAGE 4 OF 12", in_title_block=True) == {
        "sheet_number": "4",
        "sheet_count": "12",
        "sheet_number_needs_info": False,
    }
    assert sheet_fields_from_text("SHT 7 OF 10", in_title_block=True)["sheet_number"] == "7"
    assert sheet_fields_from_text("PAGE 4 OF 12", in_title_block=False)["sheet_number"] == ""
    assert sheet_fields_from_text("PAGE 4 B-6", in_title_block=True)["sheet_number"] == ""
    dated = sheet_fields_from_text("DATE 8/3/92 SH.NO. 9C", in_title_block=True)
    assert dated["sheet_number"] == "9C"
    assert dated["sheet_count"] == ""
    scaled = sheet_fields_from_text("SCALE 1/2 SHEET 6 OF 9", in_title_block=True)
    assert scaled["sheet_number"] == "6"
    assert scaled["sheet_count"] == "9"
    crossed = sheet_fields_from_text("SEE SHEET 2  CONT ON SHEET 4  SH.NO. 9C", in_title_block=True)
    assert crossed["sheet_number"] == "9C"
    conflict = sheet_fields_from_text("SHEET 3 OF 9 SHEET 5 OF 9", in_title_block=True)
    assert conflict["sheet_number"] == ""
    assert conflict["sheet_number_needs_info"] is True
    unsure = sheet_fields_from_text("SHEET 3 OF 9", in_title_block=True, confidence=0.2)
    assert unsure["sheet_number"] == ""
    assert unsure["sheet_number_needs_info"] is True
    under = read_title_block(
        _band(["SHEET"], 390) + _band(["3", "OF", "9"], 396),
        height,
    )
    assert under["sheet_number"] == "3"
    assert under["sheet_count"] == "9"
    low = read_title_block(
        [
            {"text": "SHEET", "x": 20, "y": 400, "h": 3, "w": 12, "confidence": 0.9},
            {"text": "3", "x": 40, "y": 400, "h": 3, "w": 4, "confidence": 0.1},
            {"text": "OF", "x": 50, "y": 400, "h": 3, "w": 6, "confidence": 0.9},
            {"text": "9", "x": 60, "y": 400, "h": 3, "w": 4, "confidence": 0.8},
        ],
        height,
    )
    assert low["sheet_number"] == ""
    assert low["sheet_number_needs_info"] is True


def test_title_region_ocr_reads_varied_sheet_numbers():
    """Low-resolution photos, scans, corner blocks, and a side title. Not one shop's layout."""

    import shutil

    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    if shutil.which("tesseract") is None:
        pytest.skip("tesseract is not installed")
    from panelcad.importer.raster import ocr_title_sheet, read_pixels
    from panelcad.library.catalog import default_catalog

    def face(size: int, *, heavy: bool = False):
        return _font(size, bold=heavy)

    def page(width_mm: float, height_mm: float, dpi: int, paint, *, margin: int = 0, bg=(255, 255, 255)):
        def px(mm: float) -> int:
            return int(round(mm / 25.4 * dpi))

        image = Image.new("RGB", (px(width_mm) + margin * 2, px(height_mm) + margin * 2), bg)
        paint(ImageDraw.Draw(image), px, margin, image)
        return image

    def read(image, dpi: int, *, blur: float = 0, jpeg: int = 0):
        if blur:
            image = image.filter(ImageFilter.GaussianBlur(radius=blur))
        if jpeg:
            import io

            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=jpeg)
            buf.seek(0)
            image = Image.open(buf).convert("RGB")
        ppm = dpi / 25.4
        found = ocr_title_sheet(np.asarray(image), ppm, image.width / ppm, image.height / ppm)
        return found

    def bottom_line(width_mm, height_mm, dpi, text, *, size, heavy=True, rules=False):
        def paint(draw, px, margin, image):
            y = margin + px(height_mm - 18)
            if rules:
                draw.rectangle((margin + px(8), y - px(4), image.width - margin - px(6), image.height - margin - px(4)), outline=(0, 0, 0), width=2)
            draw.text((margin + px(14), y), text, fill=(0, 0, 0), font=face(size, heavy=heavy))

        return page(width_mm, height_mm, dpi, paint)

    sheet_of = read(bottom_line(215.9, 279.4, 72, "SHEET 3 OF 9", size=16), 72)
    assert sheet_of["sheet_number"] == "3" and sheet_of["sheet_count"] == "9"
    assert sheet_of["sheet_number_needs_info"] is False

    page_of = read(bottom_line(215.9, 279.4, 72, "CONTROL SCHEMATIC    PAGE 4 OF 12", size=15), 72)
    assert page_of["sheet_number"] == "4" and page_of["sheet_count"] == "12"

    legacy = read(bottom_line(279.4, 431.8, 150, "JOB NO. 380L3503   SH.NO. 9C", size=18, heavy=False, rules=True), 150)
    assert legacy["sheet_number"] == "9C"

    short = read(bottom_line(215.9, 279.4, 300, "MOTOR STARTERS   SHT 7 OF 10", size=22, heavy=False), 300)
    assert short["sheet_number"] == "7" and short["sheet_count"] == "10"

    def corner(draw, px, margin, image):
        x = margin + px(279.4 - 90)
        y = margin + px(215.9 - 36)
        draw.rectangle((x, y, image.width - 8, image.height - 6), outline=(0, 0, 0), width=2)
        draw.line((x, y + px(12), image.width - 8, y + px(12)), fill=(0, 0, 0), width=1)
        draw.text((x + 8, y + 4), "DWG E-1042", fill=(0, 0, 0), font=face(16, heavy=True))
        draw.text((x + 8, y + px(16)), "SH 3", fill=(0, 0, 0), font=face(18, heavy=True))

    assert read(page(279.4, 215.9, 96, corner), 96)["sheet_number"] == "3"

    def photo(draw, px, margin, image):
        y = margin + px(279.4 - 22)
        draw.text((margin + px(18), y), "SHEET 2 OF 8", fill=(30, 30, 30), font=face(20, heavy=True))

    gray = read(page(215.9, 279.4, 100, photo, bg=(206, 202, 194)), 100, blur=0.6, jpeg=55)
    assert gray["sheet_number"] == "2" and gray["sheet_count"] == "8"

    def margined(draw, px, margin, image):
        draw.rectangle((margin + 8, margin + 8, image.width - margin - 8, image.height - margin - 8), outline=(0, 0, 0), width=2)
        draw.text((margin + px(16), image.height - margin - px(18)), "SHEET 5 OF 6", fill=(0, 0, 0), font=face(16, heavy=True))

    assert read(page(215.9, 279.4, 72, margined, margin=36), 72)["sheet_number"] == "5"

    def clash(draw, px, margin, image):
        y = margin + px(279.4 - 24)
        draw.text((margin + px(16), y), "SHEET 3 OF 9", fill=(0, 0, 0), font=face(18, heavy=True))
        draw.text((margin + px(130), y), "SHEET 5 OF 9", fill=(0, 0, 0), font=face(18, heavy=True))

    clash_read = read(page(215.9, 279.4, 120, clash), 120)
    assert clash_read["sheet_number"] == ""
    assert clash_read["sheet_number_needs_info"] is True

    def reference(draw, px, margin, image):
        draw.text((margin + px(30), margin + px(40)), "SEE SHEET 2", fill=(0, 0, 0), font=face(16))
        draw.text((margin + px(30), margin + px(55)), "PAGE 4 B-6", fill=(0, 0, 0), font=face(16))
        draw.text((margin + px(16), margin + px(279.4 - 16)), "SHEET 5 OF 6", fill=(0, 0, 0), font=face(18, heavy=True))

    assert read(page(215.9, 279.4, 110, reference), 110)["sheet_number"] == "5"

    def plain(draw, px, margin, image):
        draw.rectangle((px(8), px(8), image.width - px(8), image.height - px(8)), outline=(0, 0, 0), width=2)
        draw.text((px(16), image.height - px(20)), "ELEMENTARY DIAGRAM", fill=(0, 0, 0), font=face(16))

    plain_read = read(page(215.9, 279.4, 100, plain), 100)
    assert plain_read["sheet_number"] == ""
    assert plain_read["sheet_number_needs_info"] is False

    side = Image.new("RGB", (int(215.9 / 25.4 * 100), int(279.4 / 25.4 * 100)), (255, 255, 255))
    strip = Image.new("RGB", (side.height - 40, 40), (255, 255, 255))
    ImageDraw.Draw(strip).text((20, 8), "SHEET 8 OF 11", fill=(0, 0, 0), font=face(18, heavy=True))
    turned = strip.rotate(90, expand=True)
    side.paste(turned, (side.width - turned.width - 6, 20))
    assert read(side, 100)["sheet_number"] == "8"

    def low(draw, px, margin, image):
        draw.rectangle((px(6), px(6), image.width - px(6), image.height - px(6)), outline=(0, 0, 0), width=2)
        y = px(279.4 - 24)
        draw.rectangle((px(8), y, image.width - px(8), image.height - px(6)), outline=(0, 0, 0), width=2)
        draw.text((px(14), y + px(4)), "DWG 4512-A   SH 6", fill=(0, 0, 0), font=face(13, heavy=True))

    low_image = page(215.9, 279.4, 72, low)
    assert read(low_image, 72)["sheet_number"] == "6"
    facts = read_pixels(low_image, 215.9, 279.4, default_catalog())
    assert facts["title"]["sheet_number"] == "6"
    assert facts["title"]["sheet_number_needs_info"] is False


def test_title_on_dark_background_survives_tilt_and_a_left_edge():
    """A page on a dark surface, a few degrees off, still yields the printed sheet number.

    The block may sit at the bottom or along the left edge. An unreadable block
    and a bare scale stay blank. Nothing here is one shop's sheet.
    """

    import shutil

    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    if shutil.which("tesseract") is None:
        pytest.skip("tesseract is not installed")
    from panelcad.importer.raster import ocr_title_sheet

    def face(size: int, *, heavy: bool = False):
        return _font(size, bold=heavy)

    def textured(width: int, height: int, seed: int) -> Image.Image:
        rng = np.random.default_rng(seed)
        canvas = rng.integers(18, 52, size=(height, width, 3), dtype=np.uint8)
        for _ in range(36):
            cy = int(rng.integers(0, height))
            cx = int(rng.integers(0, width))
            rad = int(rng.integers(10, 36))
            shade = int(rng.integers(8, 70))
            yy, xx = np.ogrid[:height, :width]
            blob = (yy - cy) ** 2 + (xx - cx) ** 2 <= rad * rad
            canvas[blob] = (shade, max(0, shade - 6), min(255, shade + 4))
        return Image.fromarray(canvas, "RGB")

    def photograph(sheet: Image.Image, *, angle: float, seed: int) -> Image.Image:
        margin = 96
        canvas = textured(sheet.width + margin * 2, sheet.height + margin * 2, seed)
        canvas.paste(sheet, (margin, margin))
        return canvas.rotate(
            angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(24, 22, 20)
        )

    def read(image: Image.Image, dpi: int):
        ppm = dpi / 25.4
        return ocr_title_sheet(np.asarray(image), ppm, image.width / ppm, image.height / ppm)

    dpi = 110

    def px(mm: float) -> int:
        return int(round(mm / 25.4 * dpi))

    def sheet(width_mm: float, height_mm: float) -> Image.Image:
        image = Image.new("RGB", (px(width_mm), px(height_mm)), (246, 244, 238))
        draw = ImageDraw.Draw(image)
        draw.rectangle((8, 8, image.width - 9, image.height - 9), outline=(20, 20, 20), width=3)
        return image

    upright = sheet(240.0, 170.0)
    draw = ImageDraw.Draw(upright)
    draw.text(
        (upright.width - 250, upright.height - 52),
        "SHEET 3 OF 7",
        fill=(12, 12, 12),
        font=face(26, heavy=True),
    )
    for angle, seed in ((6.0, 3), (-4.5, 11)):
        found = read(photograph(upright, angle=angle, seed=seed), dpi)
        assert found["sheet_number"] == "3" and found["sheet_count"] == "7", (angle, found)
        assert found["sheet_number_needs_info"] is False

    turned = sheet(250.0, 180.0)
    label = Image.new("RGB", (260, 40), (246, 244, 238))
    ImageDraw.Draw(label).text((8, 6), "SHEET 3 OF 7", fill=(12, 12, 12), font=face(24, heavy=True))
    vertical = label.rotate(90, expand=True)
    turned.paste(vertical, (14, 36))
    left = read(photograph(turned, angle=5.0, seed=19), dpi)
    assert left["sheet_number"] == "3" and left["sheet_count"] == "7", left
    assert left["sheet_number_needs_info"] is False

    blurred = photograph(upright, angle=-5.5, seed=23).filter(ImageFilter.GaussianBlur(radius=11))
    unread = read(blurred, dpi)
    assert unread["sheet_number"] == ""

    scale_only = sheet(230.0, 160.0)
    ImageDraw.Draw(scale_only).text(
        (scale_only.width - 180, scale_only.height - 48),
        "SCALE 3/7",
        fill=(12, 12, 12),
        font=face(26, heavy=True),
    )
    bare_scale = read(photograph(scale_only, angle=4.0, seed=29), dpi)
    assert bare_scale["sheet_number"] == ""
    assert bare_scale["sheet_number_needs_info"] is False


def test_sheet_confidence_uses_the_sheet_token_only():
    """A one-digit sheet does not inherit the confidence of a longer number."""

    from panelcad.importer.raster import _word_supports_sheet

    assert _word_supports_sheet("3", "3")
    assert _word_supports_sheet("9C", "9C")
    assert _word_supports_sheet("SH3", "3")
    assert _word_supports_sheet("3OF7", "3")
    assert _word_supports_sheet("10", "1") is False
    assert _word_supports_sheet("E-1042", "1") is False
    assert _word_supports_sheet("30", "3") is False
    assert _word_supports_sheet("2/5", "2")
    assert _word_supports_sheet("PAGE 2/5", "2")
    assert _word_supports_sheet("12/12", "12")
    assert _word_supports_sheet("2OF5", "2")
    assert _word_supports_sheet("2/5", "5") is False
    assert _word_supports_sheet("12/12", "1") is False
    assert _word_supports_sheet("25", "2") is False


def test_wide_low_resolution_title_crop_is_enlarged():
    """ppm under 8 still enlarges a crop wider than the old 900 px cutoff."""

    import numpy as np

    from panelcad.importer.raster import _prepare_title_pieces

    crop = np.full((80, 1600), 214, dtype=np.uint8)
    pieces = _prepare_title_pieces(crop, upscale=2.0)
    assert len(pieces) >= 2
    assert min(piece.shape[0] for piece in pieces) >= 80 * 2


def _token(text, score, left, top, width, height):
    from panelcad.importer.raster import _OcrToken

    return _OcrToken(text, score, left, top, width, height)


def test_bare_of_count_needs_a_number_on_the_of_line():
    """A digit from linework is not N. A clear OF M with no number is needs-info."""

    from panelcad.importer.raster import _merge_sheet_reads, _sheet_from_piece
    from panelcad.importer.read import sheet_fields_from_text

    of_word = _token("OF", 0.96, 200, 10, 30, 20)
    count = _token("12", 0.96, 236, 10, 28, 20)

    noise = _token("2", 0.55, 168, 12, 14, 18)
    missed = _sheet_from_piece([noise, of_word, count], sheet_fields_from_text)
    assert missed["sheet_number"] == ""
    assert missed["sheet_count"] == "12"
    assert missed["sheet_number_needs_info"] is True

    short = _token("2", 0.92, 176, 22, 8, 6)
    tick = _sheet_from_piece([short, of_word, count], sheet_fields_from_text)
    assert tick["sheet_number"] == ""
    assert tick["sheet_number_needs_info"] is True

    far = _token("2", 0.95, 20, 10, 16, 20)
    distant = _sheet_from_piece([far, of_word, count], sheet_fields_from_text)
    assert distant["sheet_number"] == ""
    assert distant["sheet_number_needs_info"] is True

    seven = _token("7", 0.93, 162, 10, 18, 20)
    read = _sheet_from_piece([seven, of_word, count], sheet_fields_from_text)
    assert read["sheet_number"] == "7"
    assert read["sheet_count"] == "12"
    assert read["sheet_number_needs_info"] is False

    # The digit and OF live in different tiles. They must not become one phrase.
    left = _sheet_from_piece([_token("2", 0.95, 10, 10, 16, 20)], sheet_fields_from_text)
    right = _sheet_from_piece([of_word, count], sheet_fields_from_text)
    merged = _merge_sheet_reads([left, right])
    assert merged["sheet_number"] == ""
    assert merged["sheet_number_needs_info"] is True

    from panelcad.importer.read import SheetGlyph, read_title_block, sheet_from_glyphs

    glued = sheet_from_glyphs(
        [SheetGlyph("OF12", 0.96, 200, 10, 58, 20)],
        in_title_block=True,
    )
    assert glued["sheet_number"] == ""
    assert glued["sheet_count"] == "12"
    assert glued["sheet_number_needs_info"] is True

    height = 279.4
    far_tokens = read_title_block(
        [
            {"text": "2", "x": 20, "y": 260, "w": 4, "h": 4, "confidence": 0.95},
            {"text": "OF", "x": 120, "y": 260, "w": 8, "h": 4, "confidence": 0.96},
            {"text": "12", "x": 130, "y": 260, "w": 8, "h": 4, "confidence": 0.96},
        ],
        height,
    )
    assert far_tokens["sheet_number"] == ""
    assert far_tokens["sheet_number_needs_info"] is True
    labeled = read_title_block(
        [
            {"text": "SHEET", "x": 20, "y": 260, "w": 18, "h": 4, "confidence": 0.9},
            {"text": "NO.", "x": 42, "y": 260, "w": 10, "h": 4, "confidence": 0.9},
            {"text": "7", "x": 56, "y": 260, "w": 4, "h": 4, "confidence": 0.93},
            {"text": "OF", "x": 160, "y": 260, "w": 8, "h": 4, "confidence": 0.96},
            {"text": "12", "x": 176, "y": 260, "w": 8, "h": 4, "confidence": 0.96},
        ],
        height,
    )
    assert labeled["sheet_number"] == "7"
    assert labeled["sheet_count"] == "12"
    assert labeled["sheet_number_needs_info"] is False

    from panelcad.importer.read import SheetGlyph, sheet_from_glyphs

    glued = sheet_from_glyphs(
        [SheetGlyph("SHT1", 0.0, 10, 10, 40, 20)],
        in_title_block=True,
    )
    assert glued["sheet_number"] == ""
    assert glued["sheet_number_needs_info"] is True

    faint = sheet_from_glyphs(
        [
            SheetGlyph("2", 0.6, 20, 10, 16, 20),
            SheetGlyph("OF", 0.6, 200, 10, 30, 20),
            SheetGlyph("12", 0.96, 236, 10, 28, 20),
        ],
        in_title_block=True,
    )
    assert faint["sheet_number"] == ""
    assert faint["sheet_number_needs_info"] is True
    low = sheet_from_glyphs(
        [
            SheetGlyph("2", 0.55, 20, 10, 16, 20),
            SheetGlyph("OF", 0.55, 200, 10, 30, 20),
            SheetGlyph("12", 0.55, 236, 10, 28, 20),
        ],
        in_title_block=True,
    )
    assert low["sheet_number"] == ""
    assert low["sheet_number_needs_info"] is True


def test_empty_of_field_and_a_far_digit_are_not_a_sheet_number():
    """An empty SHEET NO field, and a digit far from OF, never become the sheet."""

    import shutil

    import numpy as np
    from PIL import Image, ImageDraw

    if shutil.which("tesseract") is None:
        pytest.skip("tesseract is not installed")
    from panelcad.importer.raster import ocr_title_sheet, prepare_opencv, read_pixels
    from panelcad.library.catalog import default_catalog

    bold = _font(28, bold=True)

    def read(image, dpi=120):
        ppm = dpi / 25.4
        crop = ocr_title_sheet(np.asarray(image), ppm, image.width / ppm, image.height / ppm)
        prepared, _angles = prepare_opencv(image)
        facts = read_pixels(
            prepared,
            prepared.width / ppm,
            prepared.height / ppm,
            default_catalog(),
        )
        return crop, facts["title"]

    def assert_blank(crop, title):
        assert crop["sheet_number"] == "" and crop["sheet_number_needs_info"] is True
        assert title["sheet_number"] == "" and title["sheet_number_needs_info"] is True

    def sheet():
        image = Image.new("RGB", (1100, 780), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle((12, 12, 1086, 766), outline=(0, 0, 0), width=3)
        return image, draw

    def clutter(draw):
        for x in range(430, 700, 18):
            draw.line((x, 700, x, 708), fill=(0, 0, 0), width=2)
        draw.text((180, 640), "2", fill=(0, 0, 0), font=_font(14))

    empty, draw = sheet()
    draw.text((40, 690), "SHEET NO", fill=(0, 0, 0), font=bold)
    draw.text((860, 690), "OF 12", fill=(0, 0, 0), font=bold)
    clutter(draw)
    assert_blank(*read(empty))

    filled, draw = sheet()
    draw.text((40, 690), "SHEET NO", fill=(0, 0, 0), font=bold)
    draw.text((780, 690), "7 OF 12", fill=(0, 0, 0), font=bold)
    clutter(draw)
    crop, title = read(filled)
    assert crop["sheet_number"] == "7" and crop["sheet_count"] == "12"
    assert title["sheet_number"] == "7" and title["sheet_count"] == "12"
    assert title["sheet_number_needs_info"] is False

    far, draw = sheet()
    draw.text((40, 690), "2", fill=(0, 0, 0), font=bold)
    draw.text((860, 690), "OF 12", fill=(0, 0, 0), font=bold)
    assert_blank(*read(far))

    labeled, draw = sheet()
    draw.text((40, 690), "SHEET NO. 7", fill=(0, 0, 0), font=bold)
    draw.text((860, 690), "OF 12", fill=(0, 0, 0), font=bold)
    crop, title = read(labeled)
    assert crop["sheet_number"] == "7" and crop["sheet_count"] == "12"
    assert title["sheet_number"] == "7" and title["sheet_count"] == "12"
    assert title["sheet_number_needs_info"] is False

    dpi = 150
    width = int(round(215.9 / 25.4 * dpi))
    height = int(round(279.4 / 25.4 * dpi))
    letter = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(letter)
    of_x = width - 220
    draw.text((of_x - 12 * 28 - 40, height - 70), "3", fill=(0, 0, 0), font=bold)
    draw.text((of_x, height - 70), "OF 12", fill=(0, 0, 0), font=bold)
    assert_blank(*read(letter, dpi))


def test_page_fraction_is_read_from_the_title():
    """PAGE 2/5 and 12/12 are the sheet fraction, through the crop and the page read."""

    import shutil

    import numpy as np
    from PIL import Image, ImageDraw

    if shutil.which("tesseract") is None:
        pytest.skip("tesseract is not installed")
    from panelcad.importer.raster import ocr_title_sheet, prepare_opencv, read_pixels
    from panelcad.library.catalog import default_catalog

    def both(text: str):
        image = Image.new("RGB", (900, 640), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle((8, 8, 890, 630), outline=(0, 0, 0), width=2)
        draw.text((36, 560), text, fill=(0, 0, 0), font=_font(32, bold=True))
        ppm = 120 / 25.4
        crop = ocr_title_sheet(np.asarray(image), ppm, image.width / ppm, image.height / ppm)
        prepared, _angles = prepare_opencv(image)
        facts = read_pixels(
            prepared, prepared.width / ppm, prepared.height / ppm, default_catalog()
        )
        return crop, facts["title"]

    page, title = both("PAGE 2/5")
    assert page["sheet_number"] == "2" and page["sheet_count"] == "5"
    assert title["sheet_number"] == "2" and title["sheet_count"] == "5"
    assert title["sheet_number_needs_info"] is False

    page, title = both("PAGE 12/12")
    assert page["sheet_number"] == "12" and page["sheet_count"] == "12"
    assert title["sheet_number"] == "12" and title["sheet_count"] == "12"
    assert title["sheet_number_needs_info"] is False


def test_uncertain_sheet_number_stays_blank():
    from panelcad.importer.build import build_session
    from panelcad.importer.checks import findings_from_payload

    session, _report = build_session(
        {
            "standard": "NEMA/ANSI",
            "title_block": {"drawing_number": "E-1042"},
            "sheets": [
                {
                    "id": "power",
                    "title": "Power",
                    "kind": "schematic",
                    "sheet_number": "",
                    "sheet_number_needs_info": True,
                    "width_mm": 279.4,
                    "height_mm": 431.8,
                    "devices": [],
                    "wires": [],
                    "unreadable": [
                        {
                            "kind": "text",
                            "text": "",
                            "field": "sheet_number",
                            "unreadable": True,
                            "needs_info": True,
                        }
                    ],
                }
            ],
        }
    )
    info = session.project.properties["print_import"]
    assert info["sheet_numbers"]["power"] == ""
    assert info["sheet_number_needs_info"] == ["power"]
    assert info["not_for_build"] == ""
    findings = findings_from_payload(info)
    assert any(item.check == "print_sheet_number" and "needs info" in item.message for item in findings)


def _vision_settings():
    from panelcad.importer.vision_config import VisionSettings

    return VisionSettings(
        preset="custom",
        base_url="https://example.test/v1",
        model="mock-vision",
        api_key_env="PANELCAD_TEST_VISION_KEY",
        timeout_s=5,
        page_timeout_s=60,
        max_usd=0.5,
        workers=1,
    )


def _costly_transport(calls: dict):
    def transport(method, url, payload, headers, timeout):
        if method == "GET":
            return {"data": [{"id": "mock-vision"}]}
        calls["n"] += 1
        body = {"detections": []}
        return {
            "choices": [{"message": {"content": json.dumps(body)}}],
            "usage": {"cost_in_usd_ticks": 10_000_000_000},
        }

    return transport


def test_cost_guard_warns_and_can_continue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from panelcad.importer.pipeline import ImportJob

    monkeypatch.setenv("PANELCAD_TEST_VISION_KEY", "guard-key")
    image = tmp_path / "page.png"
    Image.new("RGB", (640, 400), (255, 255, 255)).save(image)

    skipped = {"n": 0}
    quiet = ImportJob.start(
        [image],
        tmp_path / "quiet",
        vision=_vision_settings(),
        transport=_costly_transport(skipped),
    )
    tile_count = len(quiet.ws.read_json("pages", "page_001", "tiles.json"))
    assert tile_count > 2
    assert skipped["n"] < tile_count
    warning = str(quiet.summary().get("warning") or "")
    assert "page cost guard" in warning
    assert "Continue to read them" in warning
    assert quiet.summary()["cost_guard"]["continued"] is False
    assert quiet.manifest["cost_guard"]["continued"] is False
    for page_id in quiet.manifest["pages"]:
        assert "cost_guard" not in quiet._page(page_id)

    continued = {"n": 0}

    def allow(_info: dict) -> bool:
        return True

    job = ImportJob.start(
        [image],
        tmp_path / "continue",
        vision=_vision_settings(),
        transport=_costly_transport(continued),
        on_cost_guard=allow,
    )
    assert continued["n"] == len(job.ws.read_json("pages", "page_001", "tiles.json"))
    guard = job.summary()["cost_guard"]
    assert guard["continued"] is True
    assert guard["warned"] is True
    assert "Continuing" in str(job.summary().get("warning") or "")
    assert "page cost guard" not in (job._page("page_001").get("vision_fallback") or "")
    for page_id in job.manifest["pages"]:
        assert "cost_guard" not in job._page(page_id)

    declined_calls = {"n": 0}
    offered: list[dict] = []

    def decline(info: dict) -> bool:
        offered.append(dict(info))
        return False

    declined = ImportJob.start(
        [image],
        tmp_path / "decline",
        vision=_vision_settings(),
        transport=_costly_transport(declined_calls),
        on_cost_guard=decline,
    )
    assert offered and offered[0]["remaining_tiles"] > 0
    assert declined_calls["n"] < tile_count
    assert declined.summary()["cost_guard"]["continued"] is False
    assert "not sent" in str(declined.summary().get("warning") or "")
    assert "cost_guard" not in declined._page("page_001")


def test_old_per_page_cost_guard_loads_as_one_job_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A workspace that stored the limit on every page still opens."""

    from panelcad.api.session import Session
    from panelcad.importer.pipeline import ImportJob

    monkeypatch.setenv("PANELCAD_TEST_VISION_KEY", "guard-key")
    image = tmp_path / "page.png"
    Image.new("RGB", (640, 400), (255, 255, 255)).save(image)
    job = ImportJob.start(
        [image],
        tmp_path / "legacy",
        vision=_vision_settings(),
        transport=_costly_transport({"n": 0}),
    )
    guard = dict(job.manifest["cost_guard"])
    job.manifest.pop("cost_guard", None)
    for page_id in list(job.manifest.get("pages") or []):
        page = job._page(page_id)
        page["cost_guard"] = dict(guard)
        job._save_page(page)
    job._save()
    loaded = ImportJob(job.ws.root)
    assert loaded.manifest["cost_guard"]["continued"] is False
    assert loaded.manifest["cost_guard"]["reason"] == "page cost guard"
    assert loaded.summary()["cost_guard"]["max_usd"] == guard["max_usd"]
    # The old copies may still sit on the pages. They are not required to load.
    assert loaded._page("page_001").get("cost_guard", {}).get("continued") is False

    session = Session.create("Old cost guard")
    session.project.properties["cost_guard"] = dict(guard)
    path = tmp_path / "old.pcad.yaml"
    session.save(path)
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "kind: schematic",
        "kind: schematic\n  cost_guard:\n    warned: true\n    continued: false",
        1,
    )
    path.write_text(text, encoding="utf-8")
    opened = Session.open(path)
    assert opened.project.name == "Old cost guard"
    assert opened.project.properties["cost_guard"]["continued"] is False


def test_cost_guard_clients_resume_after_continue():
    from panelcad.importer.grok_build import GrokBuildVision
    from panelcad.importer.vision import OpenAICompatibleVision
    from panelcad.importer.vision_config import VisionSettings

    chat = OpenAICompatibleVision(
        VisionSettings(
            preset="custom",
            max_usd=0.5,
            base_url="https://example.test/v1",
            model="m",
            api_key_env="PANELCAD_TEST_VISION_KEY",
        )
    )
    chat.spent_usd = 2.0
    assert chat.job_stop() == "page cost guard"
    chat.allow_cost_guard()
    assert chat.job_stop() == ""
    chat.begin_job()
    assert chat.cost_guard_continued is False

    grok = GrokBuildVision(VisionSettings(preset="grok-build", max_usd=0.5))
    grok.spent_usd = 2.0
    assert grok.job_stop() == "page cost guard"
    grok.allow_cost_guard()
    assert grok.job_stop() == ""


def test_cli_cost_guard_skips_a_pipe_and_confirms_a_terminal(monkeypatch: pytest.MonkeyPatch):
    import importlib

    # panelcad.cli re-exports main(), which shadows the submodule on the package.
    cli = importlib.import_module("panelcad.cli.main")

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    info = {"spent_usd": 1.25, "max_usd": 0.5, "remaining_tiles": 41}
    assert cli._confirm_cost_guard(info) is False
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.typer, "confirm", lambda *_args, **_kwargs: True)
    assert cli._confirm_cost_guard(info) is True
    monkeypatch.setattr(cli.typer, "confirm", lambda *_args, **_kwargs: False)
    assert cli._confirm_cost_guard(info) is False


def test_wizard_cost_guard_dialog_offers_continue(monkeypatch: pytest.MonkeyPatch):
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    from panelcad.gui import import_wizard

    QApplication.instance() or QApplication([])
    shown: list[str] = []

    class FakeBox(QMessageBox):
        pick = "continue"

        def setText(self, text: str) -> None:  # noqa: N802
            shown.append(text)
            super().setText(text)

        def exec(self) -> int:  # noqa: A003
            want = (
                QMessageBox.ButtonRole.AcceptRole
                if self.pick == "continue"
                else QMessageBox.ButtonRole.RejectRole
            )
            self._picked = None
            for button in self.buttons():
                if self.buttonRole(button) == want:
                    self._picked = button
                    return 0
            return 0

        def clickedButton(self):  # noqa: N802
            return getattr(self, "_picked", None)

    monkeypatch.setattr(import_wizard, "QMessageBox", FakeBox)
    host = QWidget()
    info = {"spent_usd": 3.5, "max_usd": 1.0, "remaining_tiles": 41}
    assert import_wizard.ImportWizard._confirm_cost_guard(host, info) is True
    assert any("41" in text for text in shown)
    FakeBox.pick = "skip"
    assert import_wizard.ImportWizard._confirm_cost_guard(host, info) is False


def _at(tag: str, x: float, y: float, library_id: str, **extra) -> dict:
    return _device(tag, x, y=y, library_id=library_id, source="model", **extra)


def _number(number: str, x: float, y: float, **extra) -> dict:
    row = {
        "number": number,
        "x": x,
        "y": y,
        "from_tag": "",
        "from_pin": "",
        "to_tag": "",
        "to_pin": "",
        "confidence": 0.9,
    }
    row.update(extra)
    return row


def test_numbered_rungs_connect_only_a_single_open_gap():
    """A wire number in one rung gap joins those two devices. Anything else stays needs-info."""

    from panelcad.importer.build import build_session
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    devices = [
        _at("PB2", 40, 60, "nema.operators.pb_no"),
        _at("CR1", 140, 60, "nema.contactors.coil"),
        _at("LT3", 260, 60, "nema.operators.pilot_light"),
        _at("1CR", 40, 220, "nema.contactors.coil"),
        _at("2PL", 180, 220, "nema.operators.pilot_light"),
        _at("M1", 420, 40, "nema.contactors.ac3_9a", symbol="contactor", type="contactor"),
        _at("M2", 420, 200, "nema.contactors.ac3_9a", symbol="contactor", type="contactor"),
    ]
    wires = [
        _number("77", 100, 60, from_tag="PB2", from_pin="4", to_tag="CR1", to_pin="A1"),
        _number("88", 90, 60),
        _number("89", 110, 60),
        _number("4-12", 210, 60),
        _number("W12", 110, 220),
        _number("501", 420, 150),
        _number("300", 100, 400),
    ]
    session, report = build_session(_sheet(devices, wires), catalog=catalog)
    sheet = session.project.sheet("power")
    pairs = {frozenset({str(wire.frm).upper(), str(wire.to).upper()}) for wire in sheet.wires}
    assert frozenset({"PB2.4", "CR1.A1"}) in pairs
    assert frozenset({"CR1.A2", "LT3.X1"}) in pairs
    assert frozenset({"1CR.A2", "2PL.X1"}) in pairs
    assert not any("M1." in "".join(pair) and "M2." in "".join(pair) for pair in pairs)
    unresolved = {str(row.get("number") or ""): str(row.get("reason") or "") for row in report["unresolved_wires"]}
    assert unresolved.get("88") == "needs_info"
    assert unresolved.get("89") == "needs_info"
    assert unresolved.get("501") == "needs_info"
    assert "300" not in {str(wire.number) for wire in sheet.wires}


def test_duplicate_tags_merge_across_tiles_and_pages():
    from panelcad.importer.build import build_session
    from panelcad.importer.tile import merge_extractions
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    merged = merge_extractions(
        [
            {
                "devices": [
                    _device(
                        "CR-12",
                        20,
                        symbol="contact",
                        type="contact",
                        library_id="iec.relays.k_4co_24vdc",
                        confidence=0.95,
                        tile_id="tile_00",
                    )
                ],
                "wires": [],
            },
            {
                "devices": [
                    _device(
                        "cr-12",
                        260,
                        symbol="relay",
                        type="relay",
                        library_id="nema.relays.k_4co_24vdc",
                        confidence=0.42,
                        tile_id="tile_07",
                    )
                ],
                "wires": [],
            },
        ]
    )
    # Different symbols, and readings farther apart than a tile overlap, stay two occurrences.
    assert len(merged["devices"]) == 2
    assert {device["symbol"] for device in merged["devices"]} == {"contact", "relay"}
    close = merge_extractions(
        [
            {
                "devices": [
                    _device("SS1", 40, symbol="pilot", type="pilot", library_id="nema.operators.pilot_light", confidence=0.4)
                ],
                "wires": [],
            },
            {
                "devices": [
                    _device(
                        "SS1",
                        48,
                        symbol="pilot",
                        type="pilot",
                        library_id="nema.operators.pilot_light",
                        confidence=0.91,
                    )
                ],
                "wires": [],
            },
        ]
    )
    assert len(close["devices"]) == 1
    assert close["devices"][0]["confidence"] == 0.91

    session, report = build_session(
        {
            "standard": "NEMA/ANSI",
            "drawing_style": "ladder",
            "sheets": [
                {
                    "id": "ladder",
                    "kind": "schematic",
                    "width_mm": 431.8,
                    "height_mm": 279.4,
                    "devices": [
                        _at("CR-12", 40, 40, "nema.relays.k_4co_24vdc", symbol="relay", type="relay", confidence=0.9),
                        _at("CR-12", 320, 40, "iec.relays.k_4co_24vdc", symbol="contact", type="contact", confidence=0.99),
                        _at("2PL", 40, 140, "nema.operators.pilot_light", symbol="pilot", confidence=0.88),
                        _at("2PL", 300, 140, "iec.operators.pilot_light", symbol="pilot", confidence=0.2),
                    ],
                    "wires": [],
                },
                {
                    "id": "sheet-b",
                    "kind": "schematic",
                    "width_mm": 431.8,
                    "height_mm": 279.4,
                    "devices": [
                        _at("CR-12", 50, 40, "iec.relays.k_4co_24vdc", symbol="contact", confidence=0.4),
                        _at("PB-3", 90, 40, "nema.operators.pb_no", symbol="pushbutton", type="pushbutton"),
                    ],
                    "wires": [],
                },
            ],
        },
        catalog=catalog,
    )
    found = [(sheet.id, comp.id, comp.library_id) for sheet in session.project.sheets for comp in sheet.components]
    by_tag: dict[str, list[tuple[str, str]]] = {}
    for sheet_id, tag, library_id in found:
        by_tag.setdefault(tag, []).append((sheet_id, library_id))
    assert ("ladder", "nema.relays.k_4co_24vdc") in by_tag["CR-12"]
    assert any(sheet_id == "sheet-b" for sheet_id, _library in by_tag["CR-12"])
    assert by_tag["2PL"] == [("ladder", "nema.operators.pilot_light")]
    assert by_tag["PB-3"][0][1].startswith("nema.")
    assert not any("already exists" in str(row.get("reason") or "").lower() for row in report["skipped"])


def test_nema_print_prefers_nema_symbols_and_leaves_a_miss_off_the_quote():
    from panelcad.importer.build import build_session
    from panelcad.importer.symbols import resolve_symbol
    from panelcad.io.bom import grouped_rows
    from panelcad.library.catalog import default_catalog
    from panelcad.quote.build import build_quote

    catalog = default_catalog()
    for tag, kind in (("K1", "contactor"), ("PB1", "pushbutton"), ("1M", "contactor"), ("PL-2", "pilot"), ("CR7", "")):
        resolved = resolve_symbol(
            catalog,
            tag=tag,
            type_text=kind,
            standard="NEMA/ANSI",
            library_id="iec.contactors.ac3_9a",
        )
        assert str(resolved["library_id"]).startswith("nema."), (tag, kind, resolved)
    missing = resolve_symbol(
        catalog,
        tag="HMI",
        type_text="hmi",
        standard="NEMA/ANSI",
        library_id="iec.hmi.panel_7in",
    )
    assert missing["library_id"] == ""
    iec = resolve_symbol(catalog, tag="K1", type_text="contactor", standard="IEC")
    assert str(iec["library_id"]).startswith("iec.")

    session, report = build_session(
        _sheet(
            [
                _device("K3", 40, type="contactor", symbol="contactor"),
                _device("S1", 140, type="pushbutton", symbol="pushbutton"),
                _device("HMI", 240, type="hmi", symbol="hmi"),
            ],
            [],
        ),
        catalog=catalog,
    )
    placed = {comp.id: comp.library_id for comp in session.project.sheet("power").components}
    assert placed["K3"].startswith("nema.")
    assert placed["S1"].startswith("nema.operators.")
    assert placed["HMI"] == "reconstruct.needs_info"
    assert "width_mm" not in (session.project.sheet("power").component("HMI").properties or {})
    assert any(row["tag"] == "HMI" and row["reason"] == "needs_info" for row in report["skipped"])
    bom_refs = " ".join(str(row.get("ref") or "") for row in grouped_rows(session.project, catalog))
    assert "HMI" not in bom_refs.split(",") and "HMI" not in bom_refs.split()
    quoted = " ".join(str(row.get("refs") or "") for row in build_quote(session.project, catalog)["parts"])
    assert "HMI" not in quoted.split()


def _rung_sheet(middle: dict, *, extra_sheets: list[dict] | None = None, wire_y: float = 60) -> dict:
    rung = {
        "id": "rung",
        "kind": "schematic",
        "standard": "NEMA/ANSI",
        "width_mm": 431.8,
        "height_mm": 279.4,
        "devices": [
            _at("PB1", 40, 60, "nema.operators.pb_no"),
            middle,
            _at("LT1", 260, 60, "nema.operators.pilot_light"),
        ],
        "wires": [_number("12", 100, wire_y)],
    }
    return {
        "standard": "NEMA/ANSI",
        "drawing_style": "ladder",
        "sheets": [*(extra_sheets or []), rung],
    }


def test_second_symbol_does_not_drop_wires_the_contact_already_owns():
    """CR2–M1 and M1–OL2 stay when a motor of tag M1 is also on the sheet.

    The conductor names the contact pin. Placing the motor instead drops both wires.
    """

    from panelcad.importer.build import build_session
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    session, _report = build_session(
        {
            "standard": "NEMA/ANSI",
            "drawing_style": "ladder",
            "sheets": [
                {
                    "id": "rung",
                    "kind": "schematic",
                    "standard": "NEMA/ANSI",
                    "width_mm": 431.8,
                    "height_mm": 279.4,
                    "devices": [
                        _device("CR2", 40, y=60, library_id="nema.relays.cr", symbol="coil", type="coil"),
                        _device(
                            "M1",
                            140,
                            y=60,
                            library_id="nema.contactors.aux_nc",
                            symbol="contactor",
                            type="contactor",
                            confidence=0.88,
                        ),
                        _device(
                            "M1",
                            140,
                            y=160,
                            library_id="iec.loads.motor_3ph",
                            symbol="motor",
                            type="motor",
                            confidence=0.9,
                        ),
                        _device(
                            "OL2",
                            240,
                            y=60,
                            library_id="nema.protection.overload",
                            symbol="overload",
                            type="overload",
                        ),
                    ],
                    "wires": [
                        _number("15", 90, 60),
                        {
                            "number": "16",
                            "x": 190,
                            "y": 60,
                            "from_tag": "M1",
                            "from_pin": "2",
                            "to_tag": "OL2",
                            "to_pin": "T1",
                            "confidence": 0.9,
                        },
                    ],
                }
            ],
        },
        catalog=catalog,
    )
    pairs = {
        (wire.number, frozenset({str(wire.frm).split(".")[0], str(wire.to).split(".")[0]}))
        for wire in session.project.sheet("rung").wires
    }
    assert ("15", frozenset({"CR2", "M1"})) in pairs
    assert ("16", frozenset({"M1", "OL2"})) in pairs


def test_rung_number_does_not_skip_an_unplaced_box():
    """PB1, something, LT1. The number must not become a PB1–LT1 wire."""

    from panelcad.importer.build import build_session
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    contact = _device(
        "CR1",
        140,
        y=60,
        symbol="contact",
        type="contact",
        library_id="nema.contactors.aux_no",
        w=28,
        h=16,
    )
    cases = {
        "other-page": _rung_sheet(
            contact,
            extra_sheets=[
                {
                    "id": "coil",
                    "kind": "schematic",
                    "standard": "NEMA/ANSI",
                    "width_mm": 431.8,
                    "height_mm": 279.4,
                    "devices": [_at("CR1", 40, 40, "nema.contactors.coil", symbol="coil", type="coil")],
                    "wires": [],
                }
            ],
        ),
        "same-page": _rung_sheet(
            {
                **contact,
                "confidence": 0.3,
            }
        ),
        "needs-info": _rung_sheet(_device("HMI", 140, y=60, symbol="hmi", type="hmi", w=40, h=24)),
        "unreadable": _rung_sheet(
            {"tag": "X4", "x": 140, "y": 60, "w": 24, "h": 16, "unreadable": True, "source": "model"}
        ),
    }
    # The same-page case also keeps the coil of that tag on the rung sheet, off the wire.
    cases["same-page"]["sheets"][-1]["devices"].append(
        _at("CR1", 40, 200, "nema.contactors.coil", symbol="coil", type="coil", confidence=0.95)
    )
    for name, stitched in cases.items():
        session, report = build_session(stitched, catalog=catalog)
        pairs = {
            frozenset({str(wire.frm).upper(), str(wire.to).upper()})
            for sheet in session.project.sheets
            for wire in sheet.wires
        }
        assert not any("PB1." in "".join(pair) and "LT1." in "".join(pair) for pair in pairs), name
        reasons = [str(row.get("reason") or "") for row in report["unresolved_wires"] if str(row.get("number") or "") == "12"]
        assert "needs_info" in reasons, (name, report["unresolved_wires"])


def test_self_wire_stays_needs_info_unless_pins_are_jumpered():
    from panelcad.importer.build import build_session
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    session, report = build_session(
        _sheet(
            [_at("CR1", 80, 60, "nema.contactors.coil")],
            [_wire("A", "CR1", "A1", "CR1", "A2")],
        ),
        catalog=catalog,
    )
    assert session.project.sheet("power").wires == []
    assert any(row["number"] == "A" and row["reason"] == "needs_info" for row in report["unresolved_wires"])

    part = catalog.get("nema.contactors.coil")
    saved = [(pin.id, pin.jumper) for pin in part.pins]
    try:
        for pin in part.pins:
            if pin.id in {"A1", "A2"}:
                pin.jumper = "coil"
        jumped, jumped_report = build_session(
            _sheet(
                [_at("CR1", 80, 60, "nema.contactors.coil")],
                [_wire("A", "CR1", "A1", "CR1", "A2")],
            ),
            catalog=catalog,
        )
    finally:
        for pin_id, jumper in saved:
            for pin in part.pins:
                if pin.id == pin_id:
                    pin.jumper = jumper
    pairs = {frozenset({str(wire.frm).upper(), str(wire.to).upper()}) for wire in jumped.project.sheet("power").wires}
    assert frozenset({"CR1.A1", "CR1.A2"}) in pairs
    assert not any(row["number"] == "A" and row["reason"] == "needs_info" for row in jumped_report["unresolved_wires"])


def test_identical_pin_pair_is_not_drawn_twice():
    from panelcad.importer.build import build_session
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    wire = _wire("41", "PB1", "4", "CR1", "A1")
    session, _report = build_session(
        _sheet(
            [
                _at("PB1", 40, 60, "nema.operators.pb_no"),
                _at("CR1", 140, 60, "nema.contactors.coil"),
            ],
            [wire, dict(wire)],
        ),
        catalog=catalog,
    )
    pairs = [frozenset({str(item.frm).upper(), str(item.to).upper()}) for item in session.project.sheet("power").wires]
    assert pairs.count(frozenset({"PB1.4", "CR1.A1"})) == 1


def test_print_standard_uses_page_evidence_and_can_be_overridden():
    from panelcad.importer.read import detect_drawing_standard, read_title_block
    from panelcad.importer.stitch import stitch_pages

    nema = detect_drawing_standard(
        devices=[
            {"tag": "CR12", "symbol": "coil"},
            {"tag": "PB3", "symbol": "pushbutton"},
            {"tag": "LS2", "symbol": "contact"},
            {"tag": "FU1", "symbol": "fuse"},
        ],
        rails=["L1", "L2"],
        wires=[{"number": "104"}, {"number": "105"}],
    )
    assert nema == "NEMA/ANSI"
    iec = detect_drawing_standard(
        devices=[{"tag": "K1", "symbol": "contactor"}, {"tag": "Q1", "symbol": "breaker"}],
        rails=["L+", "M"],
    )
    assert iec == "IEC"
    mixed = detect_drawing_standard(
        devices=[{"tag": "CR1", "symbol": "coil"}, {"tag": "K1", "symbol": "contactor"}],
        rails=["L1", "L+"],
    )
    assert mixed == "mixed"
    assert detect_drawing_standard() == ""
    # The GE elementary title is NEMA even without the word NEMA.
    height = 431.8
    block = read_title_block(
        [{"text": word, "x": 24 + index * 18, "y": 400, "h": 3, "w": 16} for index, word in enumerate(
            "ELEMENTARY DIAGRAM JOB NO. 380L3503 SH.NO. 9C".split()
        )],
        height,
    )
    assert block["standard"] == "NEMA/ANSI"
    assert detect_drawing_standard(title_standard=block["standard"]) == "NEMA/ANSI"

    pages = [
        {
            "id": "page_001",
            "width_mm": 420,
            "height_mm": 297,
            "classification": {"kind": "schematic_ladder", "title": "Ladder", "standard": ""},
            "merged": {
                "devices": [{"tag": "CR1", "symbol": "coil", "x": 40, "y": 40}, {"tag": "PB1", "symbol": "pushbutton", "x": 80, "y": 40}],
                "wires": [{"number": "101", "x": 60, "y": 40}],
                "xrefs": [],
            },
        },
        {
            "id": "page_002",
            "width_mm": 420,
            "height_mm": 297,
            "classification": {"kind": "schematic_control", "title": "Control", "standard": "IEC"},
            "merged": {"devices": [{"tag": "K1", "symbol": "contactor", "x": 40, "y": 40}], "wires": [], "xrefs": []},
        },
    ]
    detected = stitch_pages(pages)
    assert detected["standard"] == "mixed"
    by_id = {sheet["page_id"]: sheet["standard"] for sheet in detected["sheets"]}
    assert by_id["page_001"] == "NEMA/ANSI"
    assert by_id["page_002"] == "IEC"
    forced = stitch_pages(pages, standard_override="IEC")
    assert forced["standard"] == "IEC"
    assert {sheet["standard"] for sheet in forced["sheets"]} == {"IEC"}


def test_shared_rail_names_do_not_make_a_page_mixed():
    from panelcad.importer.read import detect_drawing_standard

    iec_power = [{"tag": tag, "symbol": "contactor"} for tag in ("K1", "Q1", "S1", "F1", "M1")]
    assert detect_drawing_standard(devices=iec_power, rails=["L1", "L2", "L3"]) == "IEC"
    assert detect_drawing_standard(devices=[{"tag": "K1"}, {"tag": "Q1"}, {"tag": "S1"}], rails=["+24V", "0V"]) == "IEC"
    assert (
        detect_drawing_standard(devices=[{"tag": "-K1"}, {"tag": "-Q2"}, {"tag": "-S3"}], rails=["L1"])
        == "IEC"
    )
    nema = detect_drawing_standard(
        devices=[{"tag": "CR1", "symbol": "coil"}, {"tag": "PB2", "symbol": "pushbutton"}, {"tag": "FU1"}],
        rails=["X1", "X2"],
    )
    assert nema == "NEMA/ANSI"
    assert detect_drawing_standard(title_standard="NEMA/ANSI", rails=["L1", "X1"]) == "NEMA/ANSI"


def test_nema_page_replaces_an_iec_id_from_the_model():
    from panelcad.importer.build import build_session
    from panelcad.io.bom import grouped_rows
    from panelcad.library.catalog import default_catalog
    from panelcad.quote.build import build_quote

    catalog = default_catalog()
    session, report = build_session(
        _sheet(
            [
                _device("CR1", 40, library_id="iec.contactors.coil", symbol="coil", type="coil", source="model"),
                _device("HMI", 140, library_id="iec.hmi.panel_7in", symbol="hmi", type="hmi", source="model"),
            ],
            [],
        ),
        catalog=catalog,
    )
    placed = {comp.id: comp.library_id for comp in session.project.sheet("power").components}
    assert placed["CR1"].startswith("nema.")
    assert placed["HMI"] == "reconstruct.needs_info"
    assert any(row["tag"] == "HMI" and row["reason"] == "needs_info" for row in report["skipped"])
    bom = " ".join(str(row.get("ref") or "") for row in grouped_rows(session.project, catalog))
    quote = " ".join(str(row.get("refs") or "") for row in build_quote(session.project, catalog)["parts"])
    assert "HMI" not in bom.split() and "HMI" not in quote.split()

    unknown, unknown_report = build_session(
        {
            "standard": "",
            "sheets": [
                {
                    "id": "power",
                    "kind": "schematic",
                    "standard": "",
                    "width_mm": 431.8,
                    "height_mm": 279.4,
                    "devices": [
                        _device("K1", 40, library_id="iec.contactors.ac3_9a", symbol="contactor", source="model"),
                        _device(
                            "K2",
                            120,
                            library_id="iec.contactors.ac3_9a",
                            symbol="contactor",
                            source="library",
                            catalog_matched=True,
                        ),
                    ],
                    "wires": [],
                }
            ],
        },
        catalog=catalog,
    )
    unknown_placed = {comp.id: comp.library_id for comp in unknown.project.sheet("power").components}
    assert unknown_placed["K1"] == "reconstruct.needs_info"
    assert "width_mm" not in (unknown.project.sheet("power").component("K1").properties or {})
    assert unknown_placed["K2"] == "iec.contactors.ac3_9a"
    assert any(row["tag"] == "K1" and row["reason"] == "needs_info" for row in unknown_report["skipped"])


def test_model_symbol_words_still_join_a_three_device_rung():
    """The model says contact and pilot_light. Those are still one rung."""

    from panelcad.importer.build import build_session
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    session, report = build_session(
        _sheet(
            [
                _at("PB1", 40, 60, "nema.operators.pb_no", symbol="push_button", type="push_button"),
                _at("CR2", 140, 60, "nema.contactors.aux_no", symbol="contact", type="contact"),
                _at("LT1", 260, 60, "nema.operators.pilot_light", symbol="pilot_light", type="pilot_light"),
            ],
            [_number("5", 100, 60), _number("6", 200, 60)],
        ),
        catalog=catalog,
    )
    pairs = {frozenset({str(wire.frm).upper(), str(wire.to).upper()}) for wire in session.project.sheet("power").wires}
    assert frozenset({"PB1.4", "CR2.3"}) in pairs
    assert frozenset({"CR2.4", "LT1.X1"}) in pairs
    assert "5" not in {str(row.get("number") or "") for row in report["unresolved_wires"]}
    assert "6" not in {str(row.get("number") or "") for row in report["unresolved_wires"]}
