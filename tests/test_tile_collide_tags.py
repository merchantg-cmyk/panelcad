from __future__ import annotations

from panelcad.importer.tile import merge_extractions


def test_merge_keeps_different_tags_on_identical_box():
    """M1-23 and CR1-13 on the same xy must both survive; weaker is needs_info."""

    merged = merge_extractions(
        [
            {
                "devices": [
                    {
                        "tag": "M1-23",
                        "library_id": "nema.contactors.aux_no",
                        "type": "contactor",
                        "symbol": "contactor",
                        "x": 40.98,
                        "y": 171.69,
                        "confidence": 0.85,
                        "tile_id": "tile_04",
                        "page_id": "page_001",
                    },
                    {
                        "tag": "CR1-13",
                        "library_id": "nema.contactors.aux_no",
                        "type": "contactor",
                        "symbol": "contactor",
                        "x": 40.98,
                        "y": 171.69,
                        "confidence": 0.80,
                        "tile_id": "tile_04",
                        "page_id": "page_001",
                    },
                ],
                "wires": [],
            }
        ]
    )
    tags = {str(d.get("tag")) for d in merged["devices"]}
    assert tags == {"M1-23", "CR1-13"}
    by_tag = {str(d["tag"]): d for d in merged["devices"]}
    assert (by_tag["M1-23"]["x"], by_tag["M1-23"]["y"]) != (
        by_tag["CR1-13"]["x"],
        by_tag["CR1-13"]["y"],
    )
    weak = by_tag["CR1-13"]
    assert weak.get("needs_info") is True
    assert weak.get("reconstruct_needs_info") is True
