"""Weidmüller Configurator (WMC) CSV — the documented generic import."""

from __future__ import annotations

# Short manual: semicolon CSV, row 1 is the first block, no header.
# Column 1 article number, column 2 product designation, column 3 marker.
# https://assets.dam.weidmueller.com/.../Weidmueller_Configurator_CSV_Interface_shortmanual.pdf
WMC_NOTE = (
    "WMC generic CSV import (official short manual): column 1 article number, "
    "column 2 product designation, column 3 marker labelling. Semicolon "
    "separated. Row 1 is the first block. Map a non-Weidmüller article in "
    "WMC's article mapping. AutoCAD Electrical 2027 also mentions CAEDEX; "
    "that schema is not published, so PanelCAD does not emit it."
)

WMC_CONFIDENCE = "high"
