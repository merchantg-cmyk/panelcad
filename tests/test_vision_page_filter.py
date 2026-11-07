"""A vision row is kept only when the page OCR supports it."""

from __future__ import annotations

import json

from panelcad.importer.vision import (
    DETECTION_SCHEMA,
    SHORT_TILE_PROMPT,
    TILE_PROMPT,
    drop_unsupported_vision,
    prompt_example_tags,
)


def test_the_prompt_has_no_copyable_device_tags():
    blob = "\n".join((TILE_PROMPT, SHORT_TILE_PROMPT, json.dumps(DETECTION_SCHEMA)))
    for leak in ("CR410", "FU1-7", "MOT1", "DRIVE1", "DISC1", "S-204", "TB2-24", "PBLT001"):
        assert leak not in blob
    assert "CR410" not in prompt_example_tags(blob)


def test_a_tag_the_page_did_not_print_is_dropped_and_a_wire_becomes_unread():
    rows = drop_unsupported_vision(
        [
            {"tag": "CR410", "symbol_class": "relay", "bbox": [1, 2, 10, 8], "confidence": 0.9},
            {"tag": "PB1", "symbol_class": "pushbutton", "bbox": [4, 6, 10, 8], "confidence": 0.8},
            {
                "tag": "",
                "wire_number": "410",
                "from_tag": "MOT1",
                "to_tag": "PB1",
                "bbox": [8, 9, 4, 2],
                "confidence": 0.7,
            },
            {
                "tag": "FU1",
                "symbol_class": "fuse",
                "bbox": [12, 4, 8, 6],
                "confidence": 0.6,
            },
        ],
        [
            {"text": "PB1", "x": 10, "y": 20},
            {"text": "FU", "x": 30, "y": 20},
            {"text": "1", "x": 36, "y": 20},
        ],
    )
    tags = [str(row.get("tag") or "") for row in rows]
    assert "CR410" not in tags
    assert "PB1" in tags
    assert "FU1" in tags
    numbers = [str(row.get("wire_number") or "") for row in rows]
    assert "410" not in numbers
    assert "?" in numbers
    assert all(str(row.get("from_tag") or "") != "MOT1" for row in rows)
