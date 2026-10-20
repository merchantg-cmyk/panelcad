"""Phoenix Contact clipx ENGINEER / PROJECT complete writers."""

from __future__ import annotations

from pathlib import Path

from panelcad.io.vendor_strip.xmlcheck import PHOENIX_NS, PHOENIX_ROOT
from panelcad.io.vendor_strip.xmlwrite import write_rail_xml

# Autodesk documents an XML export for CLIP Project Planning / clipx ENGINEER
# (catalog, terminal number, tag, spare, accessories). Phoenix does not publish
# the element names. This file is a field-complete interchange XML. Import into
# clipx ENGINEER is unconfirmed; PROJECT complete CSV is the fallback.
CLIPX_NOTE = (
    "PanelCAD interchange XML using the field list Autodesk documents for "
    "CLIP Project / clipx ENGINEER (article, terminal number, tag, spare, "
    "accessories, wire markings). Phoenix does not publish the native schema. "
    "clipx ENGINEER's public path is a CAE add-on (EPLAN exchanges AML). "
    "Import of this XML is unconfirmed. Use the PROJECT complete CSV fallback "
    "for File → Data Exchange."
)

CLIPX_CONFIDENCE = "unconfirmed"


def write_phoenix_xml(
    path: Path,
    *,
    project_name: str,
    strip: str,
    rows: list[dict[str, str]],
    warnings: list[dict[str, str]],
) -> None:
    write_rail_xml(
        path,
        vendor="phoenix",
        root_name=PHOENIX_ROOT,
        namespace=PHOENIX_NS,
        product="Phoenix Contact clipx ENGINEER",
        confidence=CLIPX_CONFIDENCE,
        note=CLIPX_NOTE,
        project_name=project_name,
        strip=strip,
        rows=rows,
        warnings=warnings,
    )
