"""Shared constants for the print importer. State itself is JSON on disk."""

from __future__ import annotations

LOW_CONFIDENCE = 0.6

PAGE_KINDS = (
    "schematic_power",
    "schematic_control",
    "schematic_ladder",
    "plc_io",
    "terminal_plan",
    "panel_layout",
    "door_layout",
    "network",
    "bom",
    "cover",
    "index",
    "other",
)

# Kinds that become a PanelCAD sheet. Cover, index, and a parts list do not.
DRAWING_KINDS = {
    "schematic_power": "schematic",
    "schematic_control": "schematic",
    "schematic_ladder": "schematic",
    "plc_io": "io",
    "terminal_plan": "terminal",
    "panel_layout": "panel_layout",
    "door_layout": "door_layout",
    "network": "network",
    "other": "schematic",
}

LAYOUT_KINDS = {"panel_layout", "door_layout"}

# Names that are wire numbers on a panel print, not device tags.
CONVENTIONAL_WIRES = {
    "L1",
    "L2",
    "L3",
    "N",
    "PE",
    "GND",
    "24V",
    "0V",
    "120V",
    "240V",
    "480V",
    "24VDC",
    "0VDC",
    "24VAC",
    "120VAC",
    "COM",
    "NEUTRAL",
}

# Furniture text that matches a tag shape but is not a device.
FURNITURE = {"TS35", "DIN", "AWG"}

STAGES = (
    "ingest",
    "classify",
    "tile",
    "extract",
    "stitch",
    "parts",
    "photos",
    "review",
    "ready",
    "built",
)
