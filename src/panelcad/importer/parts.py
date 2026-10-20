"""Match a printed catalog number to the part library. Exact text only."""

from __future__ import annotations

from typing import Any

from panelcad.library.parts import PartsCatalog


def match_rows(rows: list[dict[str, Any]], parts: PartsCatalog) -> list[dict[str, Any]]:
    matched = []
    for row in rows:
        matched.append(match_one(row, parts))
    return matched


def match_one(row: dict[str, Any], parts: PartsCatalog) -> dict[str, Any]:
    catalog = str(row.get("catalog") or "").strip()
    item = {
        "qty": str(row.get("qty") or ""),
        "catalog": catalog,
        "description": str(row.get("description") or ""),
        "tag": str(row.get("tag") or ""),
        "unreadable": bool(row.get("unreadable")),
        "confidence": float(row.get("confidence") or (0.0 if row.get("unreadable") else 0.8)),
        "matched": False,
        "library_id": "",
        "manufacturer": "",
        "flag": "",
    }
    if item["unreadable"] or not catalog:
        item["flag"] = "unreadable" if item["unreadable"] else "blank"
        item["catalog"] = "" if item["unreadable"] else catalog
        return item
    try:
        part = parts.get(catalog, row.get("manufacturer"))
    except KeyError:
        item["flag"] = "unknown"
        return item
    item["matched"] = True
    item["catalog"] = part.catalog
    item["library_id"] = part.library_id
    item["manufacturer"] = part.manufacturer
    item["flag"] = ""
    return item
