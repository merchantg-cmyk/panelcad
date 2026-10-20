"""WAGO smartDESIGNER writers."""

from __future__ import annotations

from pathlib import Path

from panelcad.io.vendor_strip.xmlcheck import WAGO_NS, WAGO_ROOT
from panelcad.io.vendor_strip.xmlwrite import write_rail_xml

# WAGO documents CAE import (EPLAN, WSCAD, Zuken E³). AutoCAD Electrical 2027
# claims an XML for smartDESIGNER; the schema is not published. Older ProServe
# XML is not compatible with current smartDESIGNER (Autodesk forum).
SMARTDESIGNER_NOTE = (
    "PanelCAD interchange XML with rail order, article numbers, markers, and "
    "accessories. WAGO smartDESIGNER's documented import is a CAE file "
    "(EPLAN / WSCAD / E³). AutoCAD Electrical 2027 claims a vendor XML; that "
    "schema is not public. Older ProServe XML does not import. Import of this "
    "XML is unconfirmed. Use the comma CSV fallback."
)

SMARTDESIGNER_CONFIDENCE = "unconfirmed"


def write_wago_xml(
    path: Path,
    *,
    project_name: str,
    strip: str,
    rows: list[dict[str, str]],
    warnings: list[dict[str, str]],
) -> None:
    write_rail_xml(
        path,
        vendor="wago",
        root_name=WAGO_ROOT,
        namespace=WAGO_NS,
        product="WAGO smartDESIGNER",
        confidence=SMARTDESIGNER_CONFIDENCE,
        note=SMARTDESIGNER_NOTE,
        project_name=project_name,
        strip=strip,
        rows=rows,
        warnings=warnings,
    )
