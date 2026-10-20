"""Structural checks for the PanelCAD Phoenix / WAGO strip XML files."""

from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

PHOENIX_NS = "https://panelcad.dev/ns/phoenix-clipx"
WAGO_NS = "https://panelcad.dev/ns/wago-smartdesigner"
PHOENIX_ROOT = "PhoenixContactProject"
WAGO_ROOT = "WagoSmartDesignerProject"

_ITEM_KIDS = (
    "Kind",
    "Article",
    "Designation",
    "Tag",
    "TerminalNumber",
    "InternalWire",
    "ExternalWire",
    "Jumper",
    "Accessory",
    "Spare",
    "LibraryId",
    "Mapped",
)


def _local(tag: str) -> str:
    if tag.startswith("{") and "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _child(parent: ET.Element, name: str, ns: str) -> ET.Element | None:
    return parent.find(f"{{{ns}}}{name}")


def validate_vendor_xml(path: Path | str, *, vendor: str) -> list[str]:
    """Return human-readable problems. Empty list means the file is structurally ok."""

    dest = Path(path)
    if vendor == "phoenix":
        ns, root_name = PHOENIX_NS, PHOENIX_ROOT
    elif vendor == "wago":
        ns, root_name = WAGO_NS, WAGO_ROOT
    else:
        return [f"no XML schema for vendor '{vendor}'"]
    errors: list[str] = []
    try:
        tree = ET.parse(dest)
    except ET.ParseError as exc:
        return [f"not well-formed XML: {exc}"]
    root = tree.getroot()
    if _local(root.tag) != root_name:
        errors.append(f"root is '{_local(root.tag)}', expected '{root_name}'")
    if root.tag != f"{{{ns}}}{root_name}":
        errors.append(f"root namespace is not {ns}")
    header = _child(root, "Header", ns)
    if header is None:
        errors.append("missing Header")
    elif _child(header, "Strip", ns) is None:
        errors.append("Header missing Strip")
    strip = _child(root, "TerminalStrip", ns)
    if strip is None:
        errors.append("missing TerminalStrip")
        return errors
    items = list(strip.findall(f"{{{ns}}}Item"))
    if not items:
        errors.append("TerminalStrip has no Item elements")
    for index, item in enumerate(items, start=1):
        pos = item.get("position")
        if not pos:
            errors.append(f"Item {index} missing position attribute")
        elif not pos.isdigit():
            errors.append(f"Item {index} position '{pos}' is not an integer")
        kids = {_local(child.tag) for child in item}
        for name in _ITEM_KIDS:
            if name not in kids:
                errors.append(f"Item {index} missing {name}")
        article = _child(item, "Article", ns)
        if article is not None and not (article.text or "").strip():
            errors.append(f"Item {index} has empty Article")
        tag = _child(item, "Tag", ns)
        if tag is not None and not (tag.text or "").strip():
            errors.append(f"Item {index} has empty Tag")
    return errors


def validate_phoenix_xml(path: Path | str) -> list[str]:
    return validate_vendor_xml(path, vendor="phoenix")


def validate_wago_xml(path: Path | str) -> list[str]:
    return validate_vendor_xml(path, vendor="wago")
