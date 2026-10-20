"""Symbol block: geometry, wire connections, and attributes that fill in on insert.

A catalog record is the saved file. The block is what Symbol Builder edits:
parent, child, terminal, or panel footprint.
"""

from __future__ import annotations

from typing import Any

from panelcad.core.models import BlockAttribute, Component, Device

BLOCK_KINDS = ("parent", "child", "terminal", "footprint")

REQUIRED_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "parent": ("TAG1",),
    "child": ("TAG2",),
    "terminal": ("TAGSTRIP",),
    "footprint": ("P_TAG1",),
}

PALETTE: dict[str, tuple[str, ...]] = {
    "parent": ("TAG1", "DESC1", "DESC2", "FAMILY", "MFG", "CAT", "RATING1", "LOC", "INST"),
    "child": ("TAG2", "DESC1", "FAMILY", "XREF"),
    "terminal": ("TAGSTRIP", "TERM", "DESC1", "WIRE"),
    "footprint": ("P_TAG1", "MFG", "CAT", "ITEM"),
}

TAG_ATTRIBUTES = {"TAG1", "TAG2", "P_TAG1", "TAGSTRIP"}


def normalize_kind(value: str | None) -> str:
    token = str(value or "").strip().lower()
    if token in {"parent", "schematic", "coil", "device"}:
        return "parent"
    if token in {"child", "contact", "aux"}:
        return "child"
    if token in {"terminal", "term", "strip"}:
        return "terminal"
    if token in {"footprint", "panel", "layout"}:
        return "footprint"
    return "parent"


def infer_block_kind(device: Device) -> str:
    if device.block_kind in BLOCK_KINDS:
        return device.block_kind
    cat = device.category or ""
    lid = device.library_id.lower()
    if cat == "switching.aux" or "aux_" in lid:
        return "child"
    if cat == "connection.terminal" or ".terminals." in lid:
        return "terminal"
    return "parent"


def attribute_tags(attrs: list[BlockAttribute] | list[dict[str, Any]]) -> set[str]:
    found: set[str] = set()
    for attr in attrs:
        tag = attr.tag if isinstance(attr, BlockAttribute) else str(attr.get("tag") or "")
        if tag.strip():
            found.add(tag.strip().upper())
    return found


def missing_attributes(
    kind: str,
    attributes: list[BlockAttribute] | list[dict[str, Any]],
    footprint_attributes: list[BlockAttribute] | list[dict[str, Any]] | None = None,
) -> list[str]:
    """Required attribute tags that are not placed on the block."""
    token = normalize_kind(kind)
    source = footprint_attributes if token == "footprint" else attributes
    placed = attribute_tags(source or [])
    return [tag for tag in REQUIRED_ATTRIBUTES[token] if tag not in placed]


def has_tag_attribute(device: Device) -> bool:
    tags = attribute_tags(device.attributes) | attribute_tags(device.footprint_attributes)
    return bool(tags & TAG_ATTRIBUTES)


def attribute_value(tag: str, device: Device, component: Component | None = None) -> str:
    """Text an inserted symbol shows for this attribute."""
    name = str(tag or "").strip().upper()
    props: dict[str, Any] = dict(device.properties)
    if component is not None:
        props.update(component.properties)
    if name in {"TAG1", "TAG2", "P_TAG1"}:
        if component is not None:
            return component.id
        return str(props.get("tag") or "")
    if name == "TAGSTRIP":
        return str(props.get("strip") or (component.id if component is not None else ""))
    if name == "TERM":
        return str(props.get("terminal") or "")
    if name.startswith("DESC"):
        return str(props.get(name.lower()) or props.get("description") or props.get("desc") or "")
    if name == "FAMILY":
        return str(props.get("family") or device.category.split(".")[-1])
    if name == "MFG":
        return str(props.get("manufacturer") or props.get("mfg") or "")
    if name == "CAT":
        return str(props.get("catalog") or props.get("part_number") or "")
    if name == "RATING1":
        return str(props.get("rating") or props.get("rating1") or "")
    if name == "XREF":
        return str(props.get("xref") or "")
    if name == "ITEM":
        return str(props.get("item") or "")
    if name == "LOC":
        return str(props.get("loc") or props.get("location") or "")
    if name == "INST":
        return str(props.get("inst") or props.get("installation") or "")
    if name == "WIRE":
        return str(props.get("wire") or "")
    return str(props.get(name.lower()) or "")


def apply_symbol_spec(device: Device, spec: dict[str, Any]) -> None:
    """Write block kind, attributes, and footprint geometry onto a device."""
    from panelcad.core.primitives import expand_draw_item
    from panelcad.library.panel import resolve_panel

    if spec.get("block") or spec.get("block_kind"):
        device.block_kind = normalize_kind(str(spec.get("block") or spec.get("block_kind")))
    if "attributes" in spec:
        device.attributes = parse_attributes(spec.get("attributes"))
    if "footprint_draw" in spec:
        device.footprint_primitives = [expand_draw_item(item) for item in spec.get("footprint_draw") or []]
    if "footprint_attributes" in spec:
        device.footprint_attributes = parse_attributes(spec.get("footprint_attributes"))
    panel = spec.get("panel")
    if isinstance(panel, dict):
        device.panel = resolve_panel(device.category, panel)


def parse_attributes(raw: Any) -> list[BlockAttribute]:
    if not isinstance(raw, list):
        return []
    out: list[BlockAttribute] = []
    for item in raw:
        if isinstance(item, BlockAttribute):
            out.append(item)
        elif isinstance(item, dict) and str(item.get("tag") or "").strip():
            out.append(BlockAttribute.model_validate(item))
    return out
