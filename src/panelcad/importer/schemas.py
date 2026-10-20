"""JSON schemas handed to an external agent with one crop. Keep them small."""

from __future__ import annotations

from typing import Any

TILE_INSTRUCTIONS = """
Read only this crop of one drawing page. Return JSON matching the schema.
Coordinates are millimetres on the full page, not pixels. The crop covers
page_mm. Copy tags, wire numbers, and pin text exactly as printed.
If a character is unclear, set unreadable true and leave the text empty.
Do not guess a tag, a wire number, or a catalog number.
Devices need a tag and the symbol class that is drawn in the crop (breaker,
mpcb, contactor, overload, motor, terminal, pushbutton, estop, pilot, plc,
hmi, power_supply, drive, relay, coil, contact, fuse, disconnect). The tag
prefix is not the symbol. Set guess true and a low confidence when the shape
is not actually drawn.
Wires need the printed wire number and the from/to device pins the conductor
ends on. If either end could be two pins, set ambiguous true and leave the
pins empty. Cross-references are off-page arrows and coil-to-
contact notes. Omit anything outside the crop.
""".strip()

CLASSIFY_INSTRUCTIONS = """
Classify this single page. Return JSON matching the schema.
kind is one of: schematic_power, schematic_control, schematic_ladder, plc_io,
terminal_plan, panel_layout, door_layout, network, bom, cover, index, other.
Read the title block: company, project, title, description, customer,
location, drawing number, revision, sheet number, sheet count, standard
(IEC or NEMA), scale, drawn by, date. Copy text exactly. Unreadable fields
stay empty with unreadable true. Do not invent a drawing number or revision.
""".strip()

PHOTO_INSTRUCTIONS = """
This is one photo of a real panel, not a schematic. Identify devices, DIN
rails, wire ducts, and terminal strips. Match a device to a schematic tag
only when a label or nameplate is readable. Positions are fractions of the
photo (0-1, origin top-left). Unreadable labels stay unreadable.
Do not invent tags. This photo proposes a layout only.
""".strip()

BOM_INSTRUCTIONS = """
Read this parts-list crop. Each row is qty, catalog number, description, and
tag when printed. Copy the catalog number exactly. If a cell is unreadable,
set unreadable true and leave it empty. Do not complete a partial number.
""".strip()


def _device() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "tag": {"type": "string"},
            "type": {"type": "string"},
            "symbol": {"type": "string"},
            "library_id": {"type": "string"},
            "guess": {"type": "boolean"},
            "pins": {"type": "array", "items": {"type": "string"}},
            "x": {"type": "number"},
            "y": {"type": "number"},
            "confidence": {"type": "number"},
            "unreadable": {"type": "boolean"},
        },
        "required": ["tag", "x", "y"],
    }


def tile_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "devices": {"type": "array", "items": _device()},
            "wires": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "number": {"type": "string"},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "from_tag": {"type": "string"},
                        "to_tag": {"type": "string"},
                        "from_pin": {"type": "string"},
                        "to_pin": {"type": "string"},
                        "ambiguous": {"type": "boolean"},
                        "confidence": {"type": "number"},
                        "unreadable": {"type": "boolean"},
                    },
                    "required": ["number"],
                },
            },
            "xrefs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string"},
                        "text": {"type": "string"},
                        "tag": {"type": "string"},
                        "target": {"type": "string"},
                        "confidence": {"type": "number"},
                        "unreadable": {"type": "boolean"},
                    },
                },
            },
            "texts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "role": {"type": "string"},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "unreadable": {"type": "boolean"},
                        "confidence": {"type": "number"},
                    },
                },
            },
        },
        "required": ["devices", "wires"],
    }


def classify_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string"},
            "title": {"type": "string"},
            "project": {"type": "string"},
            "description": {"type": "string"},
            "company": {"type": "string"},
            "address": {"type": "string"},
            "customer": {"type": "string"},
            "location": {"type": "string"},
            "drawn_by": {"type": "string"},
            "scale": {"type": "string"},
            "date": {"type": "string"},
            "rev_description": {"type": "string"},
            "drawing_number": {"type": "string"},
            "rev": {"type": "string"},
            "sheet_number": {"type": "string"},
            "sheet_count": {"type": "string"},
            "standard": {"type": "string"},
            "confidence": {"type": "number"},
            "unreadable_fields": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["kind"],
    }


def photo_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "devices": {"type": "array"},
            "rails": {"type": "array"},
            "ducts": {"type": "array"},
            "strips": {"type": "array"},
        },
        "required": ["devices"],
    }


def bom_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "qty": {"type": "string"},
                        "catalog": {"type": "string"},
                        "description": {"type": "string"},
                        "tag": {"type": "string"},
                        "unreadable": {"type": "boolean"},
                        "confidence": {"type": "number"},
                    },
                },
            }
        },
        "required": ["rows"],
    }


def instructions_for(kind: str) -> str:
    if kind == "classify":
        return CLASSIFY_INSTRUCTIONS
    if kind == "photo":
        return PHOTO_INSTRUCTIONS
    if kind == "bom":
        return BOM_INSTRUCTIONS
    return TILE_INSTRUCTIONS


def schema_for(kind: str) -> dict[str, Any]:
    if kind == "classify":
        return classify_schema()
    if kind == "photo":
        return photo_schema()
    if kind == "bom":
        return bom_schema()
    return tile_schema()
