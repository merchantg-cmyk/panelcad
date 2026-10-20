"""Needs-info parts, local OCR, plain file errors, DXF sheets, and pushbutton re-import."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panelcad.importer.pipeline import ImportJob
from panelcad.importer.vision_config import VisionSettings


def test_needs_info_page_keeps_parts_and_does_not_say_read_nothing(tmp_path: Path) -> None:
    from panelcad.importer.build import build_session
    from panelcad.importer.pipeline import _empty_import_warning
    from panelcad.io.bom import grouped_rows
    from panelcad.library.catalog import default_catalog
    from panelcad.library.panel import effective_footprint
    from panelcad.quote.build import build_quote
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    catalog = default_catalog()
    session, report = build_session(
        {
            "standard": "NEMA/ANSI",
            "title_block": {"title": "GE 9C", "project": "Read parts"},
            "sheets": [
                {
                    "id": "power",
                    "kind": "schematic",
                    "title": "Power",
                    "width_mm": 420,
                    "height_mm": 297,
                    "devices": [
                        {"tag": "A1", "x": 40, "y": 40, "source": "vector"},
                        {"tag": "R1", "x": 90, "y": 40, "source": "vector"},
                        {"tag": "12T", "x": 140, "y": 40, "source": "vector"},
                    ],
                    "wires": [],
                }
            ],
        },
        catalog=catalog,
    )
    parts = [comp for sheet in session.project.sheets for comp in sheet.components]
    assert {comp.id for comp in parts} == {"A1", "R1", "12T"}
    assert all(comp.library_id == NEEDS_INFO_LIBRARY_ID for comp in parts)
    assert session.project.properties["print_import"]["not_for_build"] == "yes"
    assert all(row["reason"] == "needs_info" for row in report["skipped"])
    for comp in parts:
        assert "width_mm" not in (comp.properties or {})
        footprint = effective_footprint(catalog.get(comp.library_id), comp)
        assert footprint.width_mm == 0 and footprint.height_mm == 0 and footprint.depth_mm == 0
    refs = " ".join(str(row.get("ref") or "") for row in grouped_rows(session.project, catalog))
    assert "A1" not in refs.split()
    quote = build_quote(session.project, catalog, session.parts)
    quoted = " ".join(str(row.get("refs") or "") for row in quote["parts"])
    assert "A1" not in quoted.split()
    assert quote["totals"]["parts_cost"] == 0

    found = _empty_import_warning(
        [{"vector": True, "merged": {"devices": [{"tag": "A1"}, {"tag": "R1"}]}}],
        {"skipped": [{"tag": "A1", "reason": "layout_not_ready"}], "wires": []},
    )
    assert "read nothing" not in found
    assert "Open review" in found
    blank = _empty_import_warning([{"vector": True, "merged": {"devices": []}}], {"skipped": [], "wires": []})
    assert "read nothing" in blank


def test_margin_pushbutton_survives_reexport(tmp_path: Path) -> None:
    from panelcad.examples.xref_demo import build_nema_seal_in
    from panelcad.importer.read import label_tokens

    tags = [tok["text"] for tok in label_tokens(
        [
            {"text": "PB1", "x": 70.0, "y": 12.6, "w": 6.0, "h": 3.5},
            {"text": "01", "x": 10.0, "y": 12.0, "w": 4.0, "h": 2.4},
            {"text": "M1", "x": 160.0, "y": 40.0, "w": 6.0, "h": 3.5},
        ],
        420.0,
        297.0,
    )["tags"]]
    assert "PB1" in tags
    assert "01" not in tags

    session = build_nema_seal_in()
    first_pdf = tmp_path / "nema.pdf"
    session.export_pdf(first_pdf)
    built = ImportJob.start([first_pdf], tmp_path / "ws1", backend="external").build()
    first = {comp.id for sheet in built.project.sheets for comp in sheet.components}
    assert {"PB1", "PB2"} <= first
    second_pdf = tmp_path / "again.pdf"
    built.export_pdf(second_pdf)
    again = ImportJob.start([second_pdf], tmp_path / "ws2", backend="external").build()
    second = {comp.id for sheet in again.project.sheets for comp in sheet.components}
    assert {"PB1", "PB2"} <= second


def test_local_tesseract_path_and_scan_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from panelcad.importer.raster import TESSERACT_MESSAGE, find_tesseract

    binary = tmp_path / "tesseract"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("TESSERACT_CMD", str(binary))
    assert find_tesseract() == str(binary)
    assert "Scans need OCR or vision turned on" in TESSERACT_MESSAGE


def test_image_pdf_is_a_scan_for_local_ocr(tmp_path: Path) -> None:
    import pymupdf
    from PIL import Image

    from panelcad.importer.ingest import ingest_paths, pdf_page_is_scan
    from panelcad.importer.workspace import Workspace

    image = tmp_path / "page.png"
    Image.new("RGB", (320, 200), (255, 255, 255)).save(image)
    doc = pymupdf.open()
    page = doc.new_page(width=320, height=200)
    page.insert_image(page.rect, filename=str(image), keep_proportion=False)
    page.insert_text((40, 40), "H 4 17 LH")
    assert pdf_page_is_scan(page) is True
    dest = tmp_path / "scan.pdf"
    doc.save(dest)
    doc.close()
    pages = ingest_paths([dest], Workspace(tmp_path / "ws"))
    assert pages[0]["vector"] is False
    assert pages[0]["text"] == ""


def test_logo_on_a_vector_page_keeps_the_text_layer(tmp_path: Path) -> None:
    import json

    import pymupdf
    from PIL import Image

    from panelcad.importer.ingest import ingest_paths, pdf_page_is_scan
    from panelcad.importer.workspace import Workspace

    logo = tmp_path / "logo.png"
    Image.new("RGB", (40, 20), (20, 40, 180)).save(logo)
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    shape = page.new_shape()
    for index in range(149):
        y = 40 + (index % 40)
        shape.draw_line((30, y), (220, y + 1))
    shape.finish(color=(0, 0, 0), width=0.4)
    shape.commit()
    words = ["G1", "H1", "S0", "S1", "S2", "HMI"] + [f"W{number}" for number in range(20)]
    for index, word in enumerate(words):
        page.insert_text((80 + (index % 8) * 40, 80 + (index // 8) * 14), word, fontsize=9)
    page.insert_image(pymupdf.Rect(760, 540, 800, 560), filename=str(logo))
    assert pdf_page_is_scan(page) is False
    dest = tmp_path / "logo.pdf"
    doc.save(dest)
    doc.close()
    pages = ingest_paths([dest], Workspace(tmp_path / "ws"))
    assert pages[0]["vector"] is True
    saved = json.loads((tmp_path / "ws" / "pages" / "page_001" / "words.json").read_text(encoding="utf-8"))
    texts = {item["text"] for item in saved}
    assert {"G1", "S0", "S1", "S2", "H1"} <= texts


def test_layout_titles_win_over_phase_labels() -> None:
    from panelcad.importer.read import classify_text, read_title_block

    rails = "L1 L2 L3 600 400"
    panel = (
        "PANEL LAYOUT 1",
        "PANEL LAYOUT - MCC-1",
        "BACKPLATE LAYOUT",
        "ENCLOSURE LAYOUT - SECTION A",
        "SUBPANEL LAYOUT",
        "Panel Layout",
        "Enclosure",
        "Backplate",
        "CABINET LAYOUT",
        "MOUNTING PLATE LAYOUT",
        "COMPONENT LAYOUT",
        "EQUIPMENT LAYOUT",
        "DEVICE LAYOUT",
        "MCC LAYOUT",
        "LAYOUT",
        "BACKPANEL LAYOUT",
        "BACK PANEL LAYOUT",
        "BACKPLATE WIRING LAYOUT",
        "ENCLOSURE DETAIL",
        "PANEL ARRANGEMENT",
        "GENERAL ARRANGEMENT",
        "PANEL LAYOUT SHEET 2",
        "CONTROL PANEL LAYOUT",
        "PANEL LAYOUT DRAWING",
        "PANEL LAYOUT REV B",
        "PANEL LAYOUT AND BOM",
        "BACKPANEL",
        "BACK PANEL",
        "MOUNTING PLATE",
    )
    for title in panel:
        assert classify_text(title, rails) == "panel_layout", title
    assert classify_text("DOOR LAYOUT (FRONT)", rails) == "door_layout"
    assert classify_text("Door Layout - Front", rails) == "door_layout"
    assert classify_text("INNER DOOR LAYOUT", rails) == "door_layout"
    assert classify_text("DOOR LAYOUT 2", rails) == "door_layout"
    assert classify_text("DWG E-001 REV A panel_layout", rails) == "panel_layout"
    assert classify_text("panel_layout", rails) == "panel_layout"
    assert classify_text("door_layout", rails) == "door_layout"
    labelled = {
        "PANEL LAYOUT FOR PUMP STATION 2": "panel_layout",
        "ENCLOSURE LAYOUT FOR VFD-1": "panel_layout",
        "CONTROL PANEL LAYOUT FOR LIFT STATION": "panel_layout",
        "PANEL LAYOUT SHT 2": "panel_layout",
        "PANEL LAYOUT (SHT 1 OF 2)": "panel_layout",
        "PANEL LAYOUT - REVISED": "panel_layout",
        "PANEL LAYOUT AND NOTES": "panel_layout",
        "REVISED PANEL LAYOUT": "panel_layout",
        "PANEL LAYOUT ON SHEET 5": "panel_layout",
        "DOOR LAYOUT SHT 4": "door_layout",
        "FOR PANEL LAYOUT": "panel_layout",
        "DEVICES ON DOOR": "door_layout",
        "ENCLOSURE ELEVATION": "panel_layout",
        "DOOR ELEVATION": "door_layout",
        "FRONT ELEVATION": "panel_layout",
        "DOOR CUTOUT DETAIL": "door_layout",
        "PANEL CUTOUT": "panel_layout",
        "FRONT VIEW": "panel_layout",
        "PLC CABINET LAYOUT": "panel_layout",
        "PLC RACK LAYOUT": "panel_layout",
    }
    for title, kind in labelled.items():
        assert classify_text(title, rails) == kind, title
    specific = {
        "TERMINAL LAYOUT": "terminal_plan",
        "TERMINAL STRIP LAYOUT": "terminal_plan",
        "TERMINAL ARRANGEMENT": "terminal_plan",
        "PLC I/O LAYOUT": "plc_io",
        "NETWORK LAYOUT": "network",
        "ETHERNET NETWORK ARRANGEMENT": "network",
    }
    for title, kind in specific.items():
        assert classify_text(title, rails) == kind, title
    for title in (
        "SEE DOOR LAYOUT",
        "SEE PANEL LAYOUT",
        "REFER TO PANEL LAYOUT E-002",
    ):
        assert classify_text(title, rails) not in {"panel_layout", "door_layout"}, title
    assert classify_text("SUBPANEL", rails) == "other"
    assert classify_text("Door Interlock", rails) == "other"
    assert classify_text("Control", rails) == "schematic_control"
    assert classify_text("Motor Starter", rails) == "schematic_power"
    assert classify_text("Power Distribution", rails) == "schematic_power"
    rung = "PB1 101 CR1\nL1 L2 L3 600 400"
    assert classify_text("DOOR INTERLOCK SCHEMATIC", rung) not in {"panel_layout", "door_layout"}
    assert classify_text("DOOR SWITCH CIRCUIT", rung) not in {"panel_layout", "door_layout"}
    assert classify_text("Door", rung) == "other"
    assert classify_text("CONTROL SCHEMATIC", "NOTE: BOND ALL GROUNDS TO BACKPLATE") == "schematic_control"
    assert classify_text("", "SEE DOOR LAYOUT") == "other"
    assert classify_text("", "BOND ALL GROUNDS TO BACKPLATE") == "other"
    assert classify_text("Door", rails) == "door_layout"

    height = 420.0
    band_y = 400.0
    titled = read_title_block(
        [
            {"text": "DWG E-001 REV A panel_layout", "x": 20, "y": band_y, "w": 70, "h": 2.4},
            {"text": "Panel Layout 1", "x": 20, "y": band_y - 8, "w": 40, "h": 3.0},
        ],
        height,
    )
    assert titled["title"] == "Panel Layout 1"
    assert classify_text(titled["title"], rails) == "panel_layout"
    kind_only = read_title_block(
        [{"text": "DWG E-001 REV A panel_layout", "x": 20, "y": band_y, "w": 70, "h": 2.4}],
        height,
    )
    assert "panel_layout" not in kind_only["title"].lower()
    assert kind_only["layout_kind"] == "panel_layout"
    from panelcad.importer.read import page_kind_from_title

    assert page_kind_from_title(kind_only, rails) == "panel_layout"
    noted = read_title_block(
        [{"text": "NOTE: BOND ALL GROUNDS TO BACKPLATE", "x": 20, "y": band_y, "w": 80, "h": 2.4}],
        height,
    )
    assert noted["title"] != "NOTE: BOND ALL GROUNDS TO BACKPLATE"
    assert classify_text(noted["title"], "SEE DOOR LAYOUT") != "door_layout"


def test_labelled_title_keeps_words_a_stand_in_rejects() -> None:
    """A TITLE field may say for, sht, or revised. A stand-in line may not."""

    from panelcad.importer.read import classify_text, read_title_block

    height = 420.0

    def block(*lines: str) -> dict:
        tokens = [
            {"text": line, "x": 24, "y": 392 + index * 6, "w": 70, "h": 3}
            for index, line in enumerate(lines)
        ]
        return read_title_block(tokens, height)

    def under(label: str, value: str) -> dict:
        return read_title_block(
            [
                {"text": label, "x": 24, "y": 390, "w": 40, "h": 2.4},
                {"text": value, "x": 24, "y": 398, "w": 80, "h": 3},
            ],
            height,
        )

    rails = "L1 L2 L3 600 400"
    for label in ("TITLE", "DRAWING TITLE", "SHEET TITLE", "DWG TITLE", "DESCRIPTION"):
        titled = under(label, "PUMP PANEL LAYOUT")
        assert titled["title"] == "PUMP PANEL LAYOUT", label
        assert classify_text(titled["title"], rails) == "panel_layout"
    for value in (
        "VFD PANEL LAYOUT",
        "LIFT STATION CONTROL PANEL LAYOUT",
        "MOTOR CONTROL CENTER LAYOUT",
        "JUNCTION BOX LAYOUT",
        "PANEL LAYOUT FOR PUMP STATION 2",
        "PANEL LAYOUT SHT 2",
        "PANEL LAYOUT - REVISED",
    ):
        titled = under("DRAWING TITLE", value)
        assert titled["title"] == value
        assert classify_text(titled["title"], rails) == "panel_layout", value
    assert under("DRAWING TITLE", "DOOR ELEVATION")["title"] == "DOOR ELEVATION"
    assert classify_text("DOOR ELEVATION", rails) == "door_layout"
    assert under("TITLE", "DEVICES ON DOOR")["title"] == "DEVICES ON DOOR"
    assert classify_text(under("TITLE", "DEVICES ON DOOR")["title"], rails) == "door_layout"
    assert classify_text(under("TITLE", "TERMINAL LAYOUT")["title"], rails) == "terminal_plan"
    assert classify_text(under("TITLE", "PLC I/O LAYOUT")["title"], rails) == "plc_io"
    assert classify_text("REMOTE I/O PANEL LAYOUT", rails) == "panel_layout"
    assert classify_text("I/O PANEL LAYOUT", rails) == "panel_layout"
    assert classify_text("NETWORK PANEL LAYOUT", rails) == "panel_layout"
    assert classify_text("DOOR CUTOUTS", rails) == "door_layout"
    assert classify_text("DOOR MOUNTED DEVICES", rails) == "door_layout"
    assert classify_text("PILOT DEVICES ON DOOR", rails) == "door_layout"
    rung = "PB1 101 CR1\nL1 L2 L3 600 400"
    assert classify_text("DOOR SWITCH ON SAFETY CIRCUIT", rung) not in {"panel_layout", "door_layout"}
    assert classify_text("LIGHTING ON DOOR OPEN", rung) not in {"panel_layout", "door_layout"}
    assert classify_text("FLOOR PLAN AND ELEVATION", rails) not in {"panel_layout", "door_layout"}
    assert classify_text("PANEL NAMEPLATE DETAIL", rails) not in {"panel_layout", "door_layout"}
    assert classify_text("FRONT ELEVATION", rails) == "panel_layout"
    for title in (
        "DEVICES ON DOOR (SHT 2)",
        "DEVICES ON DOOR SHT 2",
        "PILOT DEVICES ON DOOR - REVISED",
        "DEVICES ON DOOR AND NOTES",
        "DEVICES ON DOOR 1",
        "COMPONENTS MOUNTED ON DOOR (SHT 1 OF 2)",
        "DEVICES ON ENCLOSURE DOOR",
        "DEVICES ON DOOR E-101",
    ):
        assert classify_text(title, rails) == "door_layout", title
    assert classify_text("PLC I/O LAYOUT FOR RACK 2", rails) == "plc_io"
    assert classify_text("PLC I/O LAYOUT - PANEL CP-1", rails) == "plc_io"
    assert classify_text("TERMINAL LAYOUT FOR JUNCTION BOX JB-1", rails) == "terminal_plan"
    assert classify_text("TERMINAL STRIP LAYOUT - CONTROL PANEL", rails) == "terminal_plan"
    assert classify_text("JUNCTION BOX TERMINAL LAYOUT", rails) == "terminal_plan"
    assert classify_text("NETWORK LAYOUT - CONTROL PANEL CP-1", rails) == "network"
    assert classify_text("ETHERNET NETWORK ARRANGEMENT IN CABINET", rails) == "network"
    assert classify_text("I/O RACK LAYOUT", rails) == "panel_layout"
    assert classify_text("REMOTE I/O CABINET ELEVATION", rails) == "panel_layout"
    # The noun in front of LAYOUT is box, so this is the box layout, not a terminal plan.
    assert classify_text("TERMINAL BOX LAYOUT", rails) == "panel_layout"
    assert classify_text("FLOOR STANDING ENCLOSURE ELEVATION", rails) == "panel_layout"
    assert classify_text("FLOOR MOUNTED PANEL ELEVATION", rails) == "panel_layout"
    assert classify_text("DOOR MTD DEVICES", rails) == "door_layout"
    # A nameplate detail is the nameplate, even when the word door is in the title.
    assert classify_text("DOOR NAMEPLATE DETAIL", rails) not in {"panel_layout", "door_layout"}
    for title in (
        "DEVICES ON THE DOOR",
        "DEVICES ON FRONT DOOR",
        "ON INNER DOOR",
        "ON REAR DOOR",
        "ON LEFT DOOR",
        "ON MCC DOOR",
        "ON CP-1 DOOR",
        "DEVICES ON DOOR - CP-1",
        "DEVICES ON DOOR FOR CP-1",
        "PUSHBUTTONS ON DOOR - PUMP 1",
        "DEVICES ON DOOR REVISION 2",
        "DEVICES ON DOOR (CONT.)",
        "DEVICES ON DOOR - CONTINUED",
        "DEVICES ON DOOR (TYP)",
        "DEVICES ON DOOR (AS-BUILT)",
        "DEVICES ON DOOR - AS BUILT",
        "DEVICES ON DOOR, EXISTING",
        "PILOT LIGHTS ON THE INNER DOOR",
    ):
        assert classify_text(title, rails) == "door_layout", title
    assert classify_text("ON DOOR OPEN", rails) not in {"panel_layout", "door_layout"}
    assert classify_text("ON DOOR SWITCH", rails) not in {"panel_layout", "door_layout"}
    assert classify_text("TERMINAL STRIP LAYOUT - PANEL ELEVATION", rails) == "terminal_plan"
    assert classify_text("NETWORK LAYOUT - PANEL ELEVATION", rails) == "network"
    assert classify_text("FLOOR PLAN AND ENCLOSURE ELEVATION", rails) == "panel_layout"
    for title in (
        "PUMP 1 - PUSHBUTTONS ON DOOR",
        "CP-1, DEVICES ON DOOR",
        "SHEET 2 - DEVICES ON DOOR",
        "MCC-2 - DEVICES ON THE DOOR",
        "(CP-1) DEVICES ON DOOR",
        "DEVICES ON DOORS 1 AND 2",
        "DEVICES ON ENCLOSURE DOORS",
        "PUSHBUTTONS ON THE DOORS",
        "DEVICES ON PANEL DOORS",
        "DEVICES MOUNTED IN THE DOOR",
        "DEVICES IN DOOR",
        "PILOT LIGHTS IN FRONT DOOR",
        "DEVICES INSIDE DOOR",
        "DOOR DEVICES",
        "DOOR COMPONENTS",
        "DOOR DEVICES - SHEET 3 OF 4",
        "DEVICES ON DOOR AND BACKPANEL",
        "DEVICES ON DOOR AND SIDE PANEL",
        "DEVICES ON DOOR AS BUILT",
        "DEVICES ON DOOR AS-BUILT",
        "DEVICES ON DOOR EXISTING",
        "DEVICES ON DOOR TYP",
        "DEVICES ON DOOR CONTINUED",
        "DEVICES ON DOOR NO. 2",
        "DEVICES ON DOOR: CP-1",
        "PUSHBUTTONS ON DOOR / CP-1",
        "AREA 4: PILOT LIGHTS ON INNER DOOR",
    ):
        assert classify_text(title, rails) == "door_layout", title
    assert classify_text("CP-1 - PANEL LAYOUT FOR TERMINALS", rails) == "panel_layout"
    assert classify_text("JB-1 - ENCLOSURE LAYOUT WITH TERMINALS", rails) == "panel_layout"
    assert classify_text("RACK 2 - PANEL LAYOUT, I/O MODULES", rails) == "panel_layout"
    assert classify_text("CP-1 - BACKPANEL LAYOUT (NETWORK SWITCH)", rails) == "panel_layout"
    assert classify_text("DOOR WIRING", rails) not in {"panel_layout", "door_layout"}
    assert classify_text("DOOR INTERLOCK", rails) not in {"panel_layout", "door_layout"}
    assert classify_text("DOOR SWITCH", rails) not in {"panel_layout", "door_layout"}
    assert classify_text("FAN RUNS ON OPEN DOOR", rails) not in {"panel_layout", "door_layout"}
    assert classify_text("LIGHT ON WHEN DOOR", rails) not in {"panel_layout", "door_layout"}
    assert classify_text("SAFETY RELAY CIRCUIT ON DOOR", rails) not in {"panel_layout", "door_layout"}
    for title in (
        "CONTROL PANEL - DEVICES ON DOOR",
        "PUMP CONTROL PANEL - DEVICES ON DOOR",
        "LIFT STATION CONTROL PANEL - DEVICES ON DOOR",
        "CONTROL PANEL, DEVICES ON THE DOOR",
        "CONTROL PANEL: DOOR DEVICES",
        "MAIN CONTROL PANEL - DOOR DEVICES",
        "PUMP PANEL - PUSHBUTTONS ON DOOR",
        "ENCLOSURE - DEVICES ON DOOR",
        "PANEL - DEVICES ON DOOR",
        "AREA PANEL - PILOT LIGHTS IN THE DOOR",
    ):
        assert classify_text(title, rails) == "door_layout", title
    assert classify_text("CONTROL PANEL", rails) == "schematic_control"
    assert classify_text("CONTROL PANEL - SHEET 2", rails) == "schematic_control"
    assert classify_text("CONTROL PANEL - SUBPANEL LAYOUT", rails) == "panel_layout"
    assert classify_text("MAIN CONTROL PANEL - PANEL LAYOUT", rails) == "panel_layout"
    assert classify_text("CONTROL PANEL - TERMINAL LAYOUT", rails) == "terminal_plan"
    assert classify_text("PLC PANEL - I/O MODULE LAYOUT", rails) == "plc_io"
    assert classify_text("MCC PANEL - NETWORK LAYOUT", rails) == "network"
    for title in (
        "DEVICES ON DOOR AND SUBPANEL",
        "DEVICES ON DOOR AND MOUNTING PLATE",
        "DEVICES ON DOOR & BACKPANEL",
        "DEVICES ON DOOR + BACKPLATE",
        "DEVICES ON DOOR AND CABINET",
        "DEVICES ON DOOR AND RACK",
        "DEVICES ON DOOR AND CONSOLE",
        "DEVICES ON DOOR AND CABINET INTERIOR",
        "DEVICES ON DOOR AND SUBPANELS",
        "DEVICES ON DOOR AND BACKPANELS",
        "DEVICES ON DOOR AND BACKPLATES",
        "DEVICES ON DOOR AND SIDE PANELS",
        "DEVICES ON DOORS AND SUBPANELS",
        "DEVICES ON DOORS AND BACKPLATES",
        "DEVICES ON DOOR AND BACK PLATE",
        "DEVICES ON DOOR AND MOUNTING PLATES",
        "DOORS LAYOUT",
        "DOORS AND SUBPANELS LAYOUT",
        "DOORS AND SUBPANEL LAYOUT",
        "DOORS AND BACKPLATES LAYOUT",
        "DOORS AND BACKPLATE LAYOUT",
        "DOORS AND CABINETS LAYOUT",
        "DOORS AND PANELS LAYOUT",
        "DOORS & BACKPANELS LAYOUT",
        "DOOR AND SUBPANEL DEVICES",
        "DOOR & SUBPANEL DEVICES",
        "DOOR AND CABINET DEVICES",
        "DOOR & CABINET DEVICES",
        "DOOR + CONSOLE DEVICES",
        "DOOR AND ENCLOSURE DEVICES",
        "DOOR AND MOUNTING PLATE DEVICES",
        "DOOR AND SUBPANEL MOUNTED DEVICES",
        "DOOR AND CABINET MOUNTED DEVICES",
        "DOOR & BACKPLATE MOUNTED COMPONENTS",
        "DOOR AND SUBPANEL MTD DEVICES",
        "DOOR AND MOUNTING PLATE MOUNTED DEVICES",
        "DOORS AND SUBPANELS MOUNTED DEVICES",
        "DOOR, SUBPANEL AND SIDE PANEL DEVICES",
        "DOOR AND SUBPANEL AND SIDE PANEL DEVICES",
        "DOOR, SUBPANEL DEVICES",
        "DEVICES ON DOOR AND PEDESTAL",
        "DEVICES ON DOOR AND CUBICLES",
    ):
        assert classify_text(title, rails) == "door_layout", title
    assert classify_text("DOOR LIGHT CIRCUIT", rails) not in {"panel_layout", "door_layout"}
    assert classify_text("DOOR PUSHBUTTON WIRING", rails) not in {"panel_layout", "door_layout"}

    both = read_title_block(
        [
            {"text": "PROJECT TITLE", "x": 20, "y": 390, "w": 36, "h": 2.4},
            {"text": "LIFT STATION 3 UPGRADE", "x": 20, "y": 398, "w": 50, "h": 3},
            {"text": "DRAWING TITLE", "x": 110, "y": 390, "w": 36, "h": 2.4},
            {"text": "Power", "x": 110, "y": 398, "w": 16, "h": 3},
        ],
        height,
    )
    assert both["title"] == "Power"
    job_only = under("PROJECT TITLE", "DOL Motor Starter")
    assert job_only["title"] != "DOL Motor Starter"

    for line in (
        "REVISED PANEL LAYOUT",
        "REFER TO PANEL LAYOUT E-002",
        "PANEL LAYOUT ON SHEET 5",
        "DOOR LAYOUT SHT 4",
        "FOR PANEL LAYOUT",
        "SEE PANEL LAYOUT",
        "PANEL LAYOUT DWG E-102",
        "AS BUILT PANEL LAYOUT",
        "UPDATED PANEL LAYOUT",
    ):
        assert "LAYOUT" not in block(line)["title"].upper(), line
        assert classify_text(block(line)["title"], rails) != "panel_layout"

    named = block("PUMP PANEL LAYOUT", "REFER TO PANEL LAYOUT E-002")
    assert named["title"] == "PUMP PANEL LAYOUT"
    assert classify_text(named["title"], rails) == "panel_layout"

    def words_on_row(phrase: str, *, y: float = 400.0, x: float = 30.0) -> list[dict]:
        """One PDF-style token per word. ``x`` is the center, as ``get_text('words')`` stores it."""

        tokens = []
        cursor = x
        for word in phrase.split():
            width = max(4.0, len(word) * 2.2)
            tokens.append({"text": word, "x": cursor + width / 2.0, "y": y, "w": width, "h": 2.6})
            cursor += width + 1.2
        return tokens

    split_cross = read_title_block(
        words_on_row("REVISED PANEL LAYOUT", y=400)
        + words_on_row("DOL MOTOR STARTER", y=408),
        height,
    )
    assert split_cross["title"] == "MOTOR STARTER"
    assert "LAYOUT" not in split_cross["title"].upper()
    pump = read_title_block(words_on_row("PUMP PANEL LAYOUT"), height)
    assert pump["title"] == "PUMP PANEL LAYOUT"
    split_labels = read_title_block(
        words_on_row("PROJECT TITLE", y=370)
        + words_on_row("PUMP CONTROL SCHEMATIC", y=378)
        + words_on_row("DRAWING TITLE", y=390)
        + words_on_row("PANEL LAYOUT", y=398),
        height,
    )
    assert split_labels["title"] == "PANEL LAYOUT"


def test_pdf_title_band_uses_whole_lines(tmp_path: Path) -> None:
    """A PDF stores one word per token. The title check still sees the line."""

    import pymupdf

    from panelcad.importer.read import classify_text, read_title_block, tokens_from_pdf
    from panelcad.importer.vision_config import VisionSettings

    def written(name: str, rows: list[tuple[float, str]]) -> Path:
        doc = pymupdf.open()
        page = doc.new_page(width=792, height=612)
        for y, text in rows:
            page.insert_text((36, y), text, fontsize=9)
        path = tmp_path / name
        doc.save(path)
        doc.close()
        return path

    def block_of(path: Path) -> dict:
        tokens, meta = tokens_from_pdf(path, 0)
        return read_title_block(tokens, float(meta["height_mm"]))

    rails = "L1 L2 L3"
    for name, phrase in (
        ("revised.pdf", "REVISED PANEL LAYOUT"),
        ("see.pdf", "SEE PANEL LAYOUT E-002"),
        ("onsheet.pdf", "PANEL LAYOUT ON SHEET 5"),
        ("asbuilt.pdf", "AS BUILT PANEL LAYOUT"),
    ):
        path = written(name, [(500, phrase), (530, "MOTOR STARTER"), (200, rails)])
        block = block_of(path)
        assert block["title"] == "MOTOR STARTER", name
        assert classify_text(block["title"], rails) == "schematic_power"
        job = ImportJob.start([path], tmp_path / f"ws-{path.stem}", vision=VisionSettings(preset="none"))
        page = job.status()["pages"][0]
        assert page["title"] == "MOTOR STARTER", name
        assert page["kind"] == "schematic_power"

    pump = written("pump.pdf", [(520, "PUMP PANEL LAYOUT"), (200, rails)])
    assert block_of(pump)["title"] == "PUMP PANEL LAYOUT"
    job = ImportJob.start([pump], tmp_path / "ws-pump", vision=VisionSettings(preset="none"))
    assert job.status()["pages"][0]["title"] == "PUMP PANEL LAYOUT"
    assert job.status()["pages"][0]["kind"] == "panel_layout"

    drawing = written(
        "drawing-title.pdf",
        [(500, "DRAWING TITLE"), (522, "LIFT STATION CONTROL PANEL LAYOUT"), (200, rails)],
    )
    assert block_of(drawing)["title"] == "LIFT STATION CONTROL PANEL LAYOUT"
    short = written("mcc.pdf", [(500, "DRAWING TITLE"), (522, "MCC LAYOUT")])
    assert block_of(short)["title"] == "MCC LAYOUT"
    vfd = written("vfd.pdf", [(500, "DRAWING TITLE"), (522, "VFD PANEL LAYOUT")])
    assert block_of(vfd)["title"] == "VFD PANEL LAYOUT"
    jb = written("jb.pdf", [(500, "DRAWING TITLE"), (522, "JB-1 LAYOUT")])
    assert block_of(jb)["title"] == "JB-1 LAYOUT"

    project = written(
        "project-title.pdf",
        [
            (470, "PROJECT TITLE"),
            (488, "PUMP CONTROL SCHEMATIC"),
            (510, "DRAWING TITLE"),
            (528, "PANEL LAYOUT"),
            (200, "Q1 K1 F1 G1 M1 OL " + rails),
        ],
    )
    assert block_of(project)["title"] == "PANEL LAYOUT"
    job = ImportJob.start([project], tmp_path / "ws-project", vision=VisionSettings(preset="none"))
    assert job.status()["pages"][0]["title"] == "PANEL LAYOUT"
    assert job.status()["pages"][0]["kind"] == "panel_layout"

    job_title = written(
        "job-title.pdf",
        [
            (470, "JOB TITLE"),
            (488, "PUMP CONTROL SCHEMATIC"),
            (510, "SHEET TITLE"),
            (528, "PANEL LAYOUT"),
        ],
    )
    assert block_of(job_title)["title"] == "PANEL LAYOUT"

    door = written(
        "door-sht.pdf",
        [(510, "TITLE"), (528, "DEVICES ON DOOR SHT 2"), (200, "Q1 K1 F1 G1 M1 OL " + rails)],
    )
    assert block_of(door)["title"] == "DEVICES ON DOOR SHT 2"
    assert classify_text(block_of(door)["title"], rails) == "door_layout"
    door_job = ImportJob.start([door], tmp_path / "ws-door", vision=VisionSettings(preset="none"))
    assert door_job.status()["pages"][0]["kind"] == "door_layout"

    the_door = written(
        "on-the-door.pdf",
        [(510, "TITLE"), (528, "DEVICES ON THE DOOR"), (200, "Q1 K1 F1 G1 M1 OL " + rails)],
    )
    assert block_of(the_door)["title"] == "DEVICES ON THE DOOR"
    assert classify_text(block_of(the_door)["title"], rails) == "door_layout"
    as_built = written(
        "door-as-built.pdf",
        [(510, "TITLE"), (528, "DEVICES ON DOOR - AS BUILT"), (200, "Q1 K1 F1 G1 M1 OL " + rails)],
    )
    assert classify_text(block_of(as_built)["title"], rails) == "door_layout"
    terminal = written(
        "terminal-elevation.pdf",
        [(510, "TITLE"), (528, "TERMINAL STRIP LAYOUT - PANEL ELEVATION"), (200, "Q1 K1 F1 G1 M1 OL " + rails)],
    )
    assert block_of(terminal)["title"] == "TERMINAL STRIP LAYOUT - PANEL ELEVATION"
    assert classify_text(block_of(terminal)["title"], rails) == "terminal_plan"
    prefixed = written(
        "pump-prefix.pdf",
        [(510, "TITLE"), (528, "PUMP 1 - PUSHBUTTONS ON DOOR"), (200, "Q1 K1 F1 G1 M1 OL " + rails)],
    )
    assert block_of(prefixed)["title"] == "PUMP 1 - PUSHBUTTONS ON DOOR"
    assert classify_text(block_of(prefixed)["title"], rails) == "door_layout"
    prefixed_job = ImportJob.start([prefixed], tmp_path / "ws-prefix", vision=VisionSettings(preset="none"))
    assert prefixed_job.status()["pages"][0]["kind"] == "door_layout"
    tagged = written(
        "cp1-layout.pdf",
        [(510, "TITLE"), (528, "CP-1 - PANEL LAYOUT FOR TERMINALS"), (200, "Q1 K1 F1 G1 M1 OL " + rails)],
    )
    assert classify_text(block_of(tagged)["title"], rails) == "panel_layout"
    indoors = written(
        "in-door.pdf",
        [(510, "TITLE"), (528, "DEVICES MOUNTED IN THE DOOR"), (200, "Q1 K1 F1 G1 M1 OL " + rails)],
    )
    assert classify_text(block_of(indoors)["title"], rails) == "door_layout"
    panel_door = written(
        "panel-door.pdf",
        [(510, "TITLE"), (528, "CONTROL PANEL - DEVICES ON DOOR"), (200, "Q1 K1 F1 G1 M1 OL " + rails)],
    )
    assert classify_text(block_of(panel_door)["title"], rails) == "door_layout"
    panel_term = written(
        "panel-terminal.pdf",
        [(510, "TITLE"), (528, "CONTROL PANEL - TERMINAL LAYOUT"), (200, "Q1 K1 F1 G1 M1 OL " + rails)],
    )
    assert classify_text(block_of(panel_term)["title"], rails) == "terminal_plan"


def test_tall_rail_labels_do_not_drop_device_tags() -> None:
    """Two oversized rail names must not set the height cutoff for the page."""

    from panelcad.importer.read import label_tokens

    tokens = [
        {"text": "X1", "x": 30.0, "y": 40.0, "w": 8.0, "h": 6.5},
        {"text": "X2", "x": 360.0, "y": 40.0, "w": 8.0, "h": 6.5},
        {"text": "TITLE", "x": 80.0, "y": 250.0, "w": 16.0, "h": 2.2},
    ]
    for index, tag in enumerate(["SS1", "LS1", "CR1", "PB1", "M1", "OL1", "K1", "F1", "Q1", "S1"]):
        tokens.append({"text": tag, "x": 40.0 + index * 28.0, "y": 90.0, "w": 8.0, "h": 4.6})
    tags = {tok["text"] for tok in label_tokens(tokens, 420.0, 297.0)["tags"]}
    assert {"SS1", "LS1", "CR1", "PB1", "M1", "OL1", "K1", "F1", "Q1", "S1"} <= tags
    assert "TITLE" not in tags

    # Many short point labels must not pull the gate down onto themselves.
    io_page = [
        {"text": "U1", "x": 40.0, "y": 80.0, "w": 8.0, "h": 3.5},
        {"text": "A2", "x": 70.0, "y": 80.0, "w": 8.0, "h": 3.3},
        {"text": "A3", "x": 100.0, "y": 80.0, "w": 8.0, "h": 3.3},
        {"text": "A4", "x": 130.0, "y": 80.0, "w": 8.0, "h": 3.3},
    ]
    for index, tag in enumerate(["X0", "X1", "X2", "X3", "Y0", "Y1", "Y2", "Y3", "X4", "X5"]):
        io_page.append({"text": tag, "x": 40.0 + (index % 5) * 20.0, "y": 120.0 + (index // 5) * 14.0, "w": 6.0, "h": 2.3})
    io_tags = {tok["text"] for tok in label_tokens(io_page, 420.0, 297.0)["tags"]}
    assert {"U1", "A2", "A3", "A4"} <= io_tags
    assert not io_tags & {"X0", "X1", "Y0", "Y3"}


def test_lowercase_rail_id_is_not_a_needs_info_part() -> None:
    from panelcad.importer.build import build_session
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    session, _report = build_session(
        {
            "standard": "NEMA/ANSI",
            "title_block": {"title": "Enclosure"},
            "sheets": [
                {
                    "id": "enclosure",
                    "kind": "panel_layout",
                    "page_kind": "panel_layout",
                    "title": "Enclosure",
                    "width_mm": 420,
                    "height_mm": 297,
                    "devices": [
                        {"tag": "r1", "x": 40, "y": 80, "source": "vector"},
                        {"tag": "r2", "x": 40, "y": 140, "source": "vector"},
                        {"tag": "wt-top", "x": 30, "y": 20, "source": "vector"},
                    ],
                    "wires": [],
                }
            ],
        }
    )
    ids = {comp.id for sheet in session.project.sheets for comp in sheet.components}
    assert "r1" not in ids and "r2" not in ids and "wt-top" not in ids
    assert not any(comp.library_id == NEEDS_INFO_LIBRARY_ID for sheet in session.project.sheets for comp in sheet.components)
    labels = {row["tag"] for row in _report["skipped"] if row["reason"] == "layout_label"}
    assert labels == {"r1", "r2", "wt-top"}

    schematic, schematic_report = build_session(
        {
            "standard": "NEMA/ANSI",
            "title_block": {"title": "Control"},
            "sheets": [
                {
                    "id": "control",
                    "kind": "schematic",
                    "page_kind": "schematic_control",
                    "title": "Control",
                    "width_mm": 420,
                    "height_mm": 297,
                    "devices": [
                        {"tag": "r1", "x": 40, "y": 80, "source": "vector"},
                        {"tag": "wt-a", "x": 80, "y": 80, "source": "vector"},
                    ],
                    "wires": [],
                }
            ],
        }
    )
    skipped = {(row["tag"], row["reason"]) for row in schematic_report["skipped"]}
    assert ("r1", "needs_info") in skipped
    assert ("wt-a", "needs_info") in skipped
    assert not any(reason == "layout_label" for _tag, reason in skipped)
    marks = [comp for sheet in schematic.project.sheets for comp in sheet.components]
    assert {comp.id for comp in marks} >= {"r1", "wt-a"}
    assert all(comp.library_id == NEEDS_INFO_LIBRARY_ID for comp in marks)


def test_pdf_layout_title_with_phase_labels_stays_a_layout(tmp_path: Path) -> None:
    import pymupdf

    from panelcad.examples.dol import build_dol
    from panelcad.importer.vision_config import VisionSettings

    session = build_dol()
    pdf = tmp_path / "dol.pdf"
    session.export_pdf(pdf)
    doc = pymupdf.open(pdf)
    for page in doc:
        if "TITLE" not in page.get_text("text") or not page.search_for("Enclosure"):
            continue
        for rect in page.search_for("Enclosure"):
            page.add_redact_annot(rect, text="Panel Layout 1", fontsize=7, fill=(1, 1, 1))
        page.apply_redactions()
    renamed = tmp_path / "panel-layout.pdf"
    doc.save(renamed)
    doc.close()
    job = ImportJob.start([renamed], tmp_path / "ws", vision=VisionSettings(preset="none"))
    titled = [row for row in job.status()["pages"] if row.get("title") == "Panel Layout 1"]
    assert titled and titled[0]["kind"] == "panel_layout"
    built = job.build()
    sheet = next(item for item in built.project.sheets if item.title == "Panel Layout 1")
    assert sheet.kind == "panel_layout"


def test_revised_panel_layout_does_not_retitle_dol_power(tmp_path: Path) -> None:
    import ezdxf

    from panelcad.importer.read import read_title_block
    from panelcad.importer.vision_config import VisionSettings

    height = 420.0
    band_y = 400.0
    block = read_title_block(
        [
            {"text": "REVISED PANEL LAYOUT", "x": 30, "y": band_y, "w": 50, "h": 3},
            {"text": "REFER TO PANEL LAYOUT E-002", "x": 30, "y": band_y - 6, "w": 60, "h": 3},
            {"text": "DOOR LAYOUT SHT 4", "x": 30, "y": band_y - 12, "w": 40, "h": 3},
            {"text": "DOL Motor Starter", "x": 30, "y": band_y - 20, "w": 40, "h": 3},
        ],
        height,
    )
    assert "LAYOUT" not in block["title"].upper()
    assert block["title"] == "MOTOR STARTER"

    doc = ezdxf.readfile("examples/dol_power.dxf")
    for entity in doc.modelspace():
        if entity.dxftype() != "TEXT":
            continue
        text = str(entity.dxf.text or "").strip()
        if text == "TITLE":
            entity.dxf.text = ""
        elif text == "Initial issue":
            entity.dxf.text = "REVISED PANEL LAYOUT"
    path = tmp_path / "revised-power.dxf"
    doc.saveas(path)
    job = ImportJob.start([path], tmp_path / "ws", vision=VisionSettings(preset="none"))
    page = job.status()["pages"][0]
    assert page["kind"] == "schematic_power"
    assert page["title"] == "MOTOR STARTER"
    built = job.build()
    sheet = built.project.sheets[0]
    assert sheet.kind == "schematic"
    assert {comp.id for comp in sheet.components} >= {"Q1", "K1", "F1", "M1"}
    ends = {frozenset((wire.frm, wire.to)) for wire in sheet.wires}
    assert ends == {
        frozenset(("Q1.2", "K1.L1")),
        frozenset(("Q1.4", "K1.L2")),
        frozenset(("Q1.6", "K1.L3")),
        frozenset(("K1.T1", "F1.1")),
        frozenset(("K1.T2", "F1.3")),
        frozenset(("K1.T3", "F1.5")),
        frozenset(("F1.2", "M1.U1")),
        frozenset(("F1.4", "M1.V1")),
        frozenset(("F1.6", "M1.W1")),
    }


def test_dxf_layout_titles_and_kind_token(tmp_path: Path) -> None:
    import ezdxf

    from panelcad.importer.vision_config import VisionSettings

    def rewritten(name: str, *, title: str | None, drop_kind: bool) -> Path:
        doc = ezdxf.readfile("examples/dol_enclosure.dxf")
        for entity in doc.modelspace():
            if entity.dxftype() != "TEXT":
                continue
            text = str(entity.dxf.text or "")
            if title is not None and text.strip() == "Enclosure":
                entity.dxf.text = title
            if drop_kind and "panel_layout" in text:
                entity.dxf.text = text.replace("panel_layout", "SHEET")
        dest = tmp_path / name
        doc.saveas(dest)
        return dest

    def kind_of(path: Path) -> str:
        job = ImportJob.start([path], tmp_path / path.stem, vision=VisionSettings(preset="none"))
        return str(job.status()["pages"][0]["kind"])

    for name, title in (
        ("panel-layout-1.dxf", "Panel Layout 1"),
        ("backplate-layout.dxf", "Backplate Layout"),
        ("enclosure-section.dxf", "Enclosure Layout - Section A"),
    ):
        assert kind_of(rewritten(name, title=title, drop_kind=True)) == "panel_layout"
    assert kind_of(rewritten("kind-token.dxf", title="", drop_kind=False)) == "panel_layout"
    assert kind_of(Path("examples/dol_enclosure.dxf")) == "panel_layout"
    assert kind_of(Path("examples/dol_door.dxf")) == "door_layout"


def test_a_note_does_not_turn_a_schematic_into_a_layout(tmp_path: Path) -> None:
    import pymupdf

    from panelcad.examples.dol import build_dol
    from panelcad.importer.read import classify_text
    from panelcad.importer.vision_config import VisionSettings

    assert classify_text("CONTROL SCHEMATIC", "NOTE: BOND ALL GROUNDS TO BACKPLATE") == "schematic_control"
    assert classify_text("", "SEE DOOR LAYOUT") == "other"
    assert classify_text("Enclosure", "DWG E-001 REV A panel_layout") == "panel_layout"

    session = build_dol()
    pdf = tmp_path / "dol.pdf"
    session.export_pdf(pdf)
    doc = pymupdf.open(pdf)
    # Power is page 0. Control is the next sheet.
    doc[1].insert_text((80, 180), "NOTE: BOND ALL GROUNDS TO BACKPLATE", fontsize=8)
    noted = tmp_path / "noted.pdf"
    doc.save(noted)
    doc.close()
    job = ImportJob.start(
        [noted],
        tmp_path / "ws",
        backend="raster",
        vision=VisionSettings(preset="none"),
    )
    control = next(row for row in job.status()["pages"] if row.get("title") == "Control" or "control" in str(row.get("kind")))
    assert control["kind"] in {"schematic_control", "schematic_ladder", "schematic"}
    assert "panel_layout" not in str(control["kind"])
    built = job.build()
    sheet = next(item for item in built.project.sheets if item.title == "Control" or item.id == "control")
    tags = {comp.id for comp in sheet.components}
    assert {"G1", "H1", "S0", "S1", "S2"} <= tags
    assert sheet.kind == "schematic"


def test_needs_info_summary_counts_every_saved_mark(tmp_path: Path) -> None:
    from panelcad.importer.build import build_session
    from panelcad.library.catalog import default_catalog
    from panelcad.reconstruct.placeholder import is_needs_info_component

    catalog = default_catalog()
    device = {"tag": "A1", "x": 40, "y": 40, "source": "vector"}
    session, report = build_session(
        {
            "standard": "NEMA/ANSI",
            "sheets": [
                {"id": "one", "kind": "schematic", "title": "One", "width_mm": 420, "height_mm": 297, "devices": [dict(device)], "wires": []},
                {"id": "two", "kind": "schematic", "title": "Two", "width_mm": 420, "height_mm": 297, "devices": [dict(device)], "wires": []},
            ],
        },
        catalog=catalog,
    )
    marks = [
        comp
        for sheet in session.project.sheets
        for comp in sheet.components
        if is_needs_info_component(comp)
    ]
    assert len(marks) == 2
    unique_skipped = {row["tag"] for row in report["skipped"] if row["reason"] == "needs_info"}
    assert unique_skipped == {"A1"}
    job = ImportJob(tmp_path / "count")
    job.manifest = {"pages": []}
    job._remember_result(session, report)
    assert job.manifest["result"]["needs_info"] == 2
    assert job.manifest["result"]["devices"] == 2


def test_broken_files_use_plain_language(tmp_path: Path) -> None:
    import pymupdf
    from typer.testing import CliRunner

    from panelcad.cli.main import app
    from panelcad.importer.ingest import plain_print_error

    svg = tmp_path / "dol_starter.svg"
    svg.write_text("<svg", encoding="utf-8")
    empty = tmp_path / "zero.pdf"
    empty.write_bytes(b"")
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"not a pdf")
    truncated = tmp_path / "truncated.pdf"
    truncated.write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\n")
    locked = tmp_path / "password.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(locked, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="secret")
    doc.close()

    assert plain_print_error(ValueError("not a print"), svg) == "This file could not be read as an SVG."
    runner = CliRunner()
    expected = {
        svg: "This file could not be read as an SVG.",
        empty: "The file is empty or not a PDF.",
        fake: "This file could not be read as a PDF.",
        truncated: "The file is empty or not a PDF.",
        locked: "This PDF is password protected.",
    }
    for path, message in expected.items():
        result = runner.invoke(
            app,
            ["import", str(path), "--vision-backend", "none", "--workspace", str(tmp_path / path.stem), "--out", str(tmp_path / f"{path.stem}.pcad.yaml")],
        )
        text = (result.output or "") + (getattr(result, "stderr", None) or "")
        assert result.exit_code == 1, text
        assert "Traceback" not in text
        assert "ValueError" not in text
        assert message in text


def test_dxf_layouts_and_enclosure_kind(tmp_path: Path) -> None:
    import ezdxf

    doc = ezdxf.new("R2010", setup=True)
    model = doc.modelspace()
    model.add_text("Q1", height=3.0).set_placement((40, 40))
    model.add_text("Power", height=4.0).set_placement((40, 80))
    second = doc.layouts.new("Sheet2")
    second.add_text("K1", height=3.0).set_placement((50, 40))
    second.add_text("Control", height=4.0).set_placement((50, 80))
    path = tmp_path / "two.dxf"
    doc.saveas(path)
    job = ImportJob.start([path], tmp_path / "dxf-ws", backend="external")
    pages = job.status()["pages"]
    assert len(pages) == 2
    texts = []
    for page in pages:
        words = json.loads((tmp_path / "dxf-ws" / "pages" / page["id"] / "words.json").read_text(encoding="utf-8"))
        texts.extend(item["text"] for item in words)
    assert "Q1" in texts and "K1" in texts

    enclosure = Path("examples/dol_enclosure.dxf")
    enc = ImportJob.start([enclosure], tmp_path / "enc-ws", backend="external")
    assert enc.status()["pages"][0]["kind"] == "panel_layout"
    built = enc.build()
    assert built.project.sheets[0].kind == "panel_layout"
    door = ImportJob.start([Path("examples/dol_door.dxf")], tmp_path / "door-ws", backend="external")
    assert door.status()["pages"][0]["kind"] == "door_layout"
    opened = door.build()
    marks = [comp for sheet in opened.project.sheets for comp in sheet.components]
    assert marks
    assert all(comp.library_id == "reconstruct.needs_info" or not str(comp.library_id).startswith("iec.breakers") for comp in marks)


def test_wizard_does_not_say_built_without_wires(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.import_wizard import ImportWizard, describe_import_result

    text = describe_import_result(
        name="Sheet",
        pages="page_001 schematic",
        stage="built",
        fallbacks=0,
        devices=4,
        reasons=[],
        preset="none",
        project="out.pcad.yaml",
        wires=0,
    )
    assert not text.startswith("Built")
    assert "No wires were built." in text
    wired = describe_import_result(
        name="Sheet",
        pages="page_001",
        stage="built",
        fallbacks=0,
        devices=4,
        reasons=[],
        preset="none",
        project="out.pcad.yaml",
        wires=3,
    )
    assert wired.startswith("Built Sheet")

    QApplication.instance() or QApplication([])
    wizard = ImportWizard()

    class _Job:
        def summary(self):
            return {}

        def review_items(self):
            return [{"id": "page_001-dev-0", "kind": "device", "text": "A1"}]

    wizard.job = _Job()
    wizard._import_failed("Read 1 part. They are listed in the review. Open review to see them.")
    assert wizard.review_btn.isEnabled()
    wizard.review_btn.setEnabled(False)

    class _Empty:
        def summary(self):
            return {}

        def review_items(self):
            return []

    wizard.job = _Empty()
    wizard._import_failed("This PDF is password protected.")
    assert not wizard.review_btn.isEnabled()
    wizard.job = None
    wizard.close()
