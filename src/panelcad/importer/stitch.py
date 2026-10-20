"""Cross-page device list, wire numbers, and cross-references. Images stay on disk."""

from __future__ import annotations

import re
from typing import Any

from panelcad.importer.models import DRAWING_KINDS, LOW_CONFIDENCE
from panelcad.library.catalog import normalize_standard

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def stitch_pages(
    pages: list[dict[str, Any]],
    *,
    standard_override: str = "",
) -> dict[str, Any]:
    from panelcad.importer.read import detect_drawing_standard

    detected: list[str] = []
    for page in pages:
        info = dict(page.get("classification") or {})
        merged = page.get("merged") or {}
        page_standard = detect_drawing_standard(
            title_standard=str(info.get("standard") or ""),
            devices=list(merged.get("devices") or []),
            wires=list(merged.get("wires") or []),
            texts=[str(info.get("title") or ""), str(info.get("text") or ""), str(page.get("text") or "")],
        )
        info["standard"] = page_standard
        page["classification"] = info
        detected.append(page_standard)
    if standard_override:
        standard = normalize_standard(standard_override, default="") or str(standard_override)
        if standard not in {"NEMA/ANSI", "IEC"}:
            standard = ""
    else:
        standard = _agreed_standard(detected)
    title_block = _title_block(pages)
    sheets = []
    devices_global: dict[str, dict[str, Any]] = {}
    wire_numbers: set[str] = set()
    xrefs: list[dict[str, Any]] = []
    for page in pages:
        kind = str((page.get("classification") or {}).get("kind") or "other")
        if kind in {"bom", "cover", "index"}:
            continue
        merged = page.get("merged") or {}
        devices = [item for item in merged.get("devices") or [] if item.get("tag") and not item.get("unreadable")]
        info = page.get("classification") or {}
        unreadable = _unreadable(merged)
        if info.get("sheet_number_needs_info"):
            unreadable.append(
                {
                    "kind": "text",
                    "text": "",
                    "field": "sheet_number",
                    "unreadable": True,
                    "needs_info": True,
                }
            )
        if kind == "other" and not devices and not unreadable:
            continue
        sheet_kind = DRAWING_KINDS.get(kind, "schematic")
        title = str((page.get("classification") or {}).get("title") or page["id"])
        sheet_id = _unique_slug(title if title and title.lower() != page["id"] else page["id"], [s["id"] for s in sheets])
        style = "ladder" if kind == "schematic_ladder" or (standard == "NEMA/ANSI" and sheet_kind == "schematic" and kind != "schematic_control") else ""
        if kind in {"schematic_power", "schematic_control"} and standard == "IEC":
            style = "schematic"
        for device in devices:
            tag = str(device["tag"])
            devices_global.setdefault(tag, {"tag": tag, "sheets": [], "type": device.get("type") or ""})
            devices_global[tag]["sheets"].append(sheet_id)
            if device.get("type") and not devices_global[tag].get("type"):
                devices_global[tag]["type"] = device["type"]
            if device.get("library_id") and not device.get("guess"):
                # The clearest reading of this tag is the one a tag-only mark inherits.
                # A later contact must not replace a coil that matched more strongly.
                incoming = float(device.get("confidence") or 0)
                held = float(devices_global[tag].get("confidence") or 0)
                if not devices_global[tag].get("library_id") or incoming > held:
                    devices_global[tag]["library_id"] = device["library_id"]
                    devices_global[tag]["symbol"] = device.get("symbol") or device.get("type") or ""
                    devices_global[tag]["confidence"] = incoming
        for wire in merged.get("wires") or []:
            if _sheet_wire(wire) and wire.get("number"):
                wire_numbers.add(str(wire["number"]))
        for xref in merged.get("xrefs") or []:
            item = dict(xref)
            item["sheet"] = sheet_id
            xrefs.append(item)
        sheets.append(
            {
                "id": sheet_id,
                "title": title,
                "description": str(info.get("description") or ""),
                "sheet_number": str(info.get("sheet_number") or ""),
                "sheet_number_needs_info": bool(info.get("sheet_number_needs_info")),
                "kind": sheet_kind,
                "page_kind": kind,
                "standard": standard if standard_override else str((page.get("classification") or {}).get("standard") or ""),
                "drawing_style": style,
                "page_id": page["id"],
                "width_mm": page.get("width_mm"),
                "height_mm": page.get("height_mm"),
                "devices": devices,
                "wires": [wire for wire in merged.get("wires") or [] if _sheet_wire(wire)],
                "xrefs": [xref for xref in merged.get("xrefs") or []],
                "unreadable": unreadable,
            }
        )
    _share_symbols(sheets, devices_global)
    from panelcad.importer.tile import collapse_devices_across_sheets

    collapse_devices_across_sheets(sheets)
    sheets.sort(key=lambda sheet: (int(sheet["sheet_number"]) if str(sheet.get("sheet_number") or "").isdigit() else 999, sheet["id"]))
    known = set(devices_global)
    unresolved = []
    for xref in xrefs:
        target = str(xref.get("target") or "").strip()
        if xref.get("unreadable"):
            unresolved.append(xref)
            continue
        if target and target not in known and not _target_known(target, known):
            unresolved.append({**xref, "reason": "missing_target"})
    return {
        "standard": standard,
        "drawing_style": "ladder" if standard == "NEMA/ANSI" else "schematic",
        "title_block": title_block,
        "sheets": sheets,
        "devices": list(devices_global.values()),
        "wire_numbers": sorted(wire_numbers),
        "xrefs": xrefs,
        "unresolved_xrefs": unresolved,
        "low_confidence": _low(sheets),
    }


def _agreed_standard(detected: list[str]) -> str:
    """One standard when every detected page agrees. Otherwise mixed or unknown."""

    named = [item for item in detected if item in {"NEMA/ANSI", "IEC"}]
    if any(item == "mixed" for item in detected) or ("NEMA/ANSI" in named and "IEC" in named):
        return "mixed"
    if named and all(item == named[0] for item in named):
        return named[0]
    return ""


def _title_block(pages: list[dict[str, Any]]) -> dict[str, str]:
    fields = (
        "title",
        "project",
        "company",
        "address",
        "customer",
        "location",
        "drawn_by",
        "scale",
        "date",
        "rev_description",
        "drawing_number",
        "rev",
        "sheet_count",
        "standard",
    )
    block = {name: "" for name in fields}
    for page in pages:
        info = page.get("classification") or {}
        for name in fields:
            block[name] = block[name] or str(info.get(name) or "")
    return block


def _share_symbols(sheets: list[dict[str, Any]], devices_global: dict[str, dict[str, Any]]) -> None:
    """A footprint or node that only shows the tag keeps the symbol identified on the schematic."""

    for sheet in sheets:
        for device in sheet["devices"]:
            if device.get("library_id") and not device.get("guess"):
                continue
            known = devices_global.get(str(device.get("tag") or ""))
            if not known or not known.get("library_id"):
                continue
            device["library_id"] = known["library_id"]
            device["symbol"] = known.get("symbol") or device.get("symbol") or ""
            device["type"] = device.get("type") or known.get("symbol") or ""
            device["guess"] = False
            device["inherited"] = True
            device["confidence"] = max(float(device.get("confidence") or 0), float(known.get("confidence") or 0.8))


def _sheet_wire(wire: dict[str, Any]) -> bool:
    """A numbered conductor, or an unnumbered jumper that already names both pins."""

    if wire.get("unreadable") or wire.get("rejected"):
        return False
    if str(wire.get("number") or "").strip():
        return True
    return bool(
        wire.get("from_pin")
        and wire.get("to_pin")
        and wire.get("from_tag")
        and wire.get("to_tag")
        and not wire.get("ambiguous")
    )


def _unreadable(merged: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key in ("devices", "wires", "texts", "xrefs"):
        for item in merged.get(key) or []:
            if item.get("unreadable"):
                rows.append({"kind": key, **item})
    return rows


def _low(sheets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for sheet in sheets:
        for device in sheet["devices"]:
            if float(device.get("confidence") or 0) < LOW_CONFIDENCE:
                rows.append({"sheet": sheet["id"], "kind": "device", "text": device.get("tag"), "confidence": device.get("confidence")})
        for wire in sheet["wires"]:
            if float(wire.get("confidence") or 0) < LOW_CONFIDENCE:
                rows.append({"sheet": sheet["id"], "kind": "wire", "text": wire.get("number"), "confidence": wire.get("confidence")})
    return rows


def _target_known(target: str, known: set[str]) -> bool:
    head = target.split("/")[-1].strip().upper()
    return head in {tag.upper() for tag in known}


def _unique_slug(text: str, used: list[str]) -> str:
    base = _SLUG_RE.sub("-", text.lower()).strip("-") or "sheet"
    if base not in used:
        return base
    n = 2
    while f"{base}-{n}" in used:
        n += 1
    return f"{base}-{n}"
