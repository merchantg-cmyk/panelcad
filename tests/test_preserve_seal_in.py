"""Preserve layout must close seal-in tips the same way ladder synth does."""

from __future__ import annotations

from panelcad.library.catalog import default_catalog
from panelcad.importer.build import build_session
from panelcad.importer.cleanup import _completion_issues, _open_stub_pins, remaining_gaps


def _starter_stitch() -> dict:
    """OCR left M1-13.4 as an open stub; PB1.4→M1.A1 already carries 107."""

    return {
        "title_block": {"standard": "NEMA/ANSI", "drawing_style": "ladder"},
        "standard": "NEMA/ANSI",
        "drawing_style": "ladder",
        "sheets": [
            {
                "id": "elementary",
                "kind": "schematic",
                "width_mm": 431.8,
                "height_mm": 279.4,
                "devices": [
                    {
                        "tag": "ES1",
                        "library_id": "nema.operators.estop",
                        "type": "estop",
                        "x": 40.0,
                        "y": 100.0,
                        "confidence": 0.9,
                        "pins": ["1", "2"],
                    },
                    {
                        "tag": "PB2",
                        "library_id": "nema.operators.pb_nc",
                        "type": "pushbutton",
                        "x": 80.0,
                        "y": 100.0,
                        "confidence": 0.9,
                        "pins": ["1", "2"],
                    },
                    {
                        "tag": "PB1",
                        "library_id": "nema.operators.pb_no",
                        "type": "pushbutton",
                        "x": 120.0,
                        "y": 100.0,
                        "confidence": 0.9,
                        "pins": ["3", "4"],
                    },
                    {
                        "tag": "M1",
                        "library_id": "nema.contactors.coil",
                        "type": "coil",
                        "x": 200.0,
                        "y": 100.0,
                        "confidence": 0.9,
                        "pins": ["A1", "A2"],
                    },
                    {
                        "tag": "M1-13",
                        "library_id": "nema.contactors.aux_no",
                        "type": "contact",
                        "x": 120.0,
                        "y": 136.0,
                        "confidence": 0.85,
                        "pins": ["3", "4"],
                        "open_stubs": ["4"],
                    },
                    {
                        "tag": "OL1",
                        "library_id": "nema.protection.ol_contact",
                        "type": "ol",
                        "x": 260.0,
                        "y": 100.0,
                        "confidence": 0.9,
                        "pins": ["95", "96"],
                    },
                ],
                "wires": [
                    {
                        "number": "105",
                        "from_tag": "ES1",
                        "from_pin": "2",
                        "to_tag": "PB2",
                        "to_pin": "1",
                        "confidence": 0.9,
                        "source": "ocr",
                    },
                    {
                        "number": "106",
                        "from_tag": "PB2",
                        "from_pin": "2",
                        "to_tag": "PB1",
                        "to_pin": "3",
                        "confidence": 0.9,
                        "source": "ocr",
                    },
                    {
                        "number": "106",
                        "from_tag": "PB2",
                        "from_pin": "2",
                        "to_tag": "M1-13",
                        "to_pin": "3",
                        "confidence": 0.9,
                        "source": "ocr",
                    },
                    {
                        "number": "107",
                        "from_tag": "PB1",
                        "from_pin": "4",
                        "to_tag": "M1",
                        "to_pin": "A1",
                        "confidence": 0.9,
                        "source": "ocr",
                    },
                    {
                        "number": "108",
                        "from_tag": "M1",
                        "from_pin": "A2",
                        "to_tag": "OL1",
                        "to_pin": "95",
                        "confidence": 0.9,
                        "source": "ocr",
                    },
                    {
                        "number": "104",
                        "from_tag": "OL1",
                        "from_pin": "96",
                        "to_tag": "L2",
                        "to_pin": "",
                        "confidence": 0.9,
                        "source": "ocr",
                    },
                ],
            }
        ],
    }


def test_preserve_builds_seal_in_tip_107_like_ladder() -> None:
    stitch = _starter_stitch()
    preserve, _ = build_session(stitch, catalog=default_catalog(), layout_mode="preserve")
    ladder, _ = build_session(stitch, catalog=default_catalog(), layout_mode="ladder")

    def tip_edges(session):
        found = []
        for sheet in session.project.sheets:
            for wire in sheet.wires:
                if {"M1-13.4", "PB1.4"} == {str(wire.frm), str(wire.to)}:
                    found.append(str(wire.number or ""))
        return found

    assert tip_edges(ladder) == ["107"], tip_edges(ladder)
    assert tip_edges(preserve) == ["107"], tip_edges(preserve)
    m13 = next(c for c in preserve.project.sheets[0].components if c.id == "M1-13")
    assert _open_stub_pins(m13) == set()
    assert not any("M1-13" in item for item in _completion_issues(preserve))


def test_reader_open_stub_on_contact_still_asks_or_blocks_without_tip() -> None:
    """If the seal-in tip cannot be built, pin 4 must not silently issue."""

    stitch = _starter_stitch()
    # Drop the start→coil hop so preserve has no evidence for 107.
    sheet = stitch["sheets"][0]
    sheet["wires"] = [w for w in sheet["wires"] if not (
        w.get("from_tag") == "PB1" and w.get("to_tag") == "M1"
    )]
    session, _ = build_session(stitch, catalog=default_catalog(), layout_mode="preserve")
    tips = [
        w
        for sheet in session.project.sheets
        for w in sheet.wires
        if {"M1-13.4", "PB1.4"} == {str(w.frm), str(w.to)}
    ]
    assert tips == []
    open_pins = [
        g for g in remaining_gaps(session) if g.get("kind") == "open_pin" and g.get("tag") == "M1-13"
    ]
    completion = _completion_issues(session)
    assert open_pins or any("M1-13" in item for item in completion), (open_pins, completion)
