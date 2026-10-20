"""Shared rail-order XML writer for Phoenix and WAGO interchange files."""

from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

from panelcad.io.vendor_strip.xmlcheck import validate_vendor_xml
from panelcad.version import __version__



def _el(parent: ET.Element, name: str, text: str = "") -> ET.Element:
    node = ET.SubElement(parent, name)
    node.text = text
    return node


def write_rail_xml(
    path: Path,
    *,
    vendor: str,
    root_name: str,
    namespace: str,
    product: str,
    confidence: str,
    note: str,
    project_name: str,
    strip: str,
    rows: list[dict[str, str]],
    warnings: list[dict[str, str]],
) -> None:
    ET.register_namespace("", namespace)
    root = ET.Element(
        f"{{{namespace}}}{root_name}",
        {
            "generator": "PanelCAD",
            "version": __version__,
            "product": product,
            "confidence": confidence,
        },
    )
    header = ET.SubElement(root, f"{{{namespace}}}Header")
    _el(header, f"{{{namespace}}}ProjectName", project_name)
    _el(header, f"{{{namespace}}}Strip", strip)
    _el(header, f"{{{namespace}}}Note", note)
    strip_el = ET.SubElement(
        root,
        f"{{{namespace}}}TerminalStrip",
        {"name": strip, "sequence": "rail"},
    )
    for row in rows:
        item = ET.SubElement(
            strip_el,
            f"{{{namespace}}}Item",
            {"position": row["position"]},
        )
        _el(item, f"{{{namespace}}}Kind", row.get("kind") or "terminal")
        _el(item, f"{{{namespace}}}Article", row["article"])
        _el(item, f"{{{namespace}}}Designation", row.get("designation") or "")
        _el(item, f"{{{namespace}}}Tag", row["tag"])
        _el(item, f"{{{namespace}}}TerminalNumber", row.get("level") or "")
        _el(item, f"{{{namespace}}}InternalWire", row.get("internal") or "")
        _el(item, f"{{{namespace}}}ExternalWire", row.get("external") or "")
        _el(item, f"{{{namespace}}}Jumper", row.get("jumper") or "")
        _el(item, f"{{{namespace}}}Accessory", row.get("accessory") or "")
        _el(item, f"{{{namespace}}}Spare", row.get("spare") or "false")
        _el(item, f"{{{namespace}}}LibraryId", row.get("library_id") or "")
        _el(item, f"{{{namespace}}}Mapped", "true" if row.get("mapped") == "true" else "false")
    warn_el = ET.SubElement(root, f"{{{namespace}}}Warnings")
    for warning in warnings:
        node = ET.SubElement(
            warn_el,
            f"{{{namespace}}}Warning",
            {
                "ref": warning.get("ref") or "",
                "library_id": warning.get("library_id") or "",
                "reason": warning.get("reason") or "unmapped_article",
            },
        )
        node.text = warning.get("message") or ""
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tree.write(dest, encoding="utf-8", xml_declaration=True)
    problems = validate_vendor_xml(dest, vendor=vendor)
    if problems:
        raise ValueError(f"{vendor} XML failed structural checks: " + "; ".join(problems))
