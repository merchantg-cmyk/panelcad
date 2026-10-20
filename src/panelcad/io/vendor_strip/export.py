"""Dispatch vendor strip files: documented CSV plus interchange XML."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from panelcad.core.models import Component, Project
from panelcad.core.terminal_strip import layout_terminal_strip
from panelcad.library.catalog import Catalog
from panelcad.io.vendor_strip.mapping import ArticleTable, load_article_table, map_article
from panelcad.io.vendor_strip.phoenix import CLIPX_CONFIDENCE, CLIPX_NOTE, write_phoenix_xml
from panelcad.io.vendor_strip.wago import SMARTDESIGNER_CONFIDENCE, SMARTDESIGNER_NOTE, write_wago_xml
from panelcad.io.vendor_strip.weidmueller import WMC_CONFIDENCE, WMC_NOTE

_PHOENIX_HEADER = ["Strip", "Position", "Tag", "Article", "Internal", "External", "Jumper", "Accessory"]

_PHOENIX_CSV_NOTE = (
    "CSV for PROJECT complete data exchange (File → Data Exchange). "
    "Column layout is PanelCAD rail order (strip, position, tag, article, "
    "internal, external, jumper, accessory). Phoenix does not publish a "
    "column spec. CLIP PROJECT / clipx ENGINEER XML is not this file."
)

_WAGO_CSV_NOTE = (
    "Rail-order CSV for the smartSCRIPT / spreadsheet path. smartDESIGNER "
    "does not publish a generic rail-assembly CSV; its documented rail import "
    "is an EPLAN, WSCAD, or E³ CAE file."
)


@dataclass(frozen=True)
class VendorAlias:
    vendor: str
    default_format: str
    product: str


_ALIASES: dict[str, VendorAlias] = {
    "phoenix": VendorAlias("phoenix", "csv", "Phoenix Contact PROJECT complete"),
    "phoenixcontact": VendorAlias("phoenix", "csv", "Phoenix Contact PROJECT complete"),
    "projectcomplete": VendorAlias("phoenix", "csv", "Phoenix Contact PROJECT complete"),
    "project": VendorAlias("phoenix", "csv", "Phoenix Contact PROJECT complete"),
    "clipproject": VendorAlias("phoenix", "xml", "Phoenix Contact clipx ENGINEER"),
    "clipx": VendorAlias("phoenix", "xml", "Phoenix Contact clipx ENGINEER"),
    "clipxengineer": VendorAlias("phoenix", "xml", "Phoenix Contact clipx ENGINEER"),
    "phoenixxml": VendorAlias("phoenix", "xml", "Phoenix Contact clipx ENGINEER"),
    "wago": VendorAlias("wago", "csv", "WAGO smartDESIGNER"),
    "smartdesigner": VendorAlias("wago", "csv", "WAGO smartDESIGNER"),
    "smartscript": VendorAlias("wago", "csv", "WAGO smartDESIGNER"),
    "wagoxml": VendorAlias("wago", "xml", "WAGO smartDESIGNER"),
    "smartdesignerxml": VendorAlias("wago", "xml", "WAGO smartDESIGNER"),
    "weidmueller": VendorAlias("weidmueller", "csv", "Weidmüller WMC"),
    "weidmuller": VendorAlias("weidmueller", "csv", "Weidmüller WMC"),
    "weidmüller": VendorAlias("weidmueller", "csv", "Weidmüller WMC"),
    "wmc": VendorAlias("weidmueller", "csv", "Weidmüller WMC"),
}


def _token(value: str) -> str:
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def _lookup(vendor: str) -> VendorAlias:
    found = _ALIASES.get(_token(vendor))
    if found is None:
        raise ValueError(f"unknown strip vendor '{vendor}'. Use phoenix, wago, weidmueller")
    return found


def normalize_vendor(vendor: str) -> str:
    return _lookup(vendor).vendor


def _normalize_format(value: str | None) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    token = _token(str(value))
    if token in {"csv", "text"}:
        return "csv"
    if token in {"xml", "clipx", "native"}:
        return "xml"
    raise ValueError(f"unknown strip file format '{value}'. Use csv or xml")


def resolve_file_format(vendor: str, file_format: str | None = None, path: Path | str | None = None) -> str:
    alias = _lookup(vendor)
    chosen = _normalize_format(file_format)
    if chosen is None and path is not None:
        suffix = Path(path).suffix.lower()
        if suffix == ".xml":
            chosen = "xml"
        elif suffix in {".csv", ".txt"}:
            chosen = "csv"
    if chosen is None:
        chosen = alias.default_format
    if alias.vendor == "weidmueller" and chosen == "xml":
        raise ValueError(
            "Weidmüller WMC has no published XML/CAEDEX schema. Use the semicolon CSV."
        )
    return chosen


def vendor_format(
    vendor: str,
    file_format: str | None = None,
    path: Path | str | None = None,
) -> dict[str, str]:
    alias = _lookup(vendor)
    chosen = resolve_file_format(vendor, file_format, path)
    if alias.vendor == "weidmueller":
        return {
            "vendor": alias.vendor,
            "product": alias.product,
            "format": "csv",
            "delimiter": ";",
            "header": "no",
            "confidence": WMC_CONFIDENCE,
            "note": WMC_NOTE,
        }
    if alias.vendor == "wago":
        if chosen == "xml":
            return {
                "vendor": alias.vendor,
                "product": alias.product,
                "format": "xml",
                "delimiter": "",
                "header": "n/a",
                "confidence": SMARTDESIGNER_CONFIDENCE,
                "note": SMARTDESIGNER_NOTE,
            }
        return {
            "vendor": alias.vendor,
            "product": alias.product,
            "format": "csv",
            "delimiter": ",",
            "header": "yes",
            "confidence": "low",
            "note": _WAGO_CSV_NOTE,
        }
    if chosen == "xml":
        return {
            "vendor": "phoenix",
            "product": "Phoenix Contact clipx ENGINEER",
            "format": "xml",
            "delimiter": "",
            "header": "n/a",
            "confidence": CLIPX_CONFIDENCE,
            "note": CLIPX_NOTE,
        }
    return {
        "vendor": "phoenix",
        "product": "Phoenix Contact PROJECT complete",
        "format": "csv",
        "delimiter": ",",
        "header": "yes",
        "confidence": "medium",
        "note": _PHOENIX_CSV_NOTE,
    }


def _find(project: Project, ref: str) -> Component | None:
    if not ref:
        return None
    for sheet in project.sheets:
        for comp in sheet.components:
            if comp.id == ref:
                return comp
    return None


def _marker(row: dict[str, str]) -> str:
    bits = [row.get("level") or "", row.get("internal") or "", row.get("external") or "", row.get("jumper") or ""]
    text = " ".join(bit for bit in bits if bit).strip()
    return text or row["tag"]


def export_rows(project: Project, catalog: Catalog, strip: str, vendor: str = "phoenix") -> list[dict[str, str]]:
    name = normalize_vendor(vendor)
    layout = layout_terminal_strip(project, catalog, strip)
    table: ArticleTable = load_article_table(catalog)
    rows: list[dict[str, str]] = []
    for position, item in enumerate(layout["items"], start=1):
        ref = str(item.get("ref") or "")
        comp = _find(project, ref)
        library_id = str(item.get("library_id") or (comp.library_id if comp is not None else ""))
        mapped = map_article(catalog, comp, name, library_id=library_id, table=table)
        kind = str(item.get("kind") or "")
        rows.append(
            {
                "strip": str(layout["strip"]),
                "position": str(position),
                "tag": ref,
                "article": mapped.article,
                "designation": mapped.designation,
                "internal": str(item.get("internal") or ""),
                "external": str(item.get("external") or ""),
                "jumper": str(item.get("jumper") or ""),
                "accessory": "" if kind == "terminal" else kind,
                "kind": kind or "terminal",
                "library_id": library_id,
                "level": str(item.get("level") or ""),
                "spare": "true" if item.get("spare") else "false",
                "mapped": "true" if mapped.mapped else "false",
                "source": mapped.source,
                "warning": mapped.warning,
            }
        )
    seen: set[tuple[str, str]] = set()
    for jumper in layout.get("jumpers") or []:
        tag = str(jumper.get("id") or "")
        library_id = str(jumper.get("library_id") or "")
        # Catalog number is the orderable article (3030161, not "FBS 2-5").
        article = str(jumper.get("catalog") or jumper.get("part_number") or library_id or "")
        key = (tag, article)
        if not article or key in seen:
            continue
        seen.add(key)
        mapped = map_article(
            catalog,
            None,
            name,
            library_id=library_id,
            table=table,
        )
        if mapped.mapped and mapped.source == "library.vendor_articles":
            article = mapped.article
            designation = mapped.designation or str(jumper.get("part_number") or "")
            warning = ""
            source = mapped.source
            mapped_flag = "true"
        else:
            designation = str(jumper.get("part_number") or jumper.get("description") or "")
            warning = ""
            source = "jumper.catalog"
            mapped_flag = "true" if jumper.get("catalog") or jumper.get("part_number") else "false"
        rows.append(
            {
                "strip": str(layout["strip"]),
                "position": str(len(rows) + 1),
                "tag": tag,
                "article": article,
                "designation": designation,
                "internal": "",
                "external": "",
                "jumper": tag,
                "accessory": "jumper",
                "kind": "jumper",
                "library_id": library_id,
                "level": "",
                "spare": "false",
                "mapped": mapped_flag,
                "source": source,
                "warning": warning,
            }
        )
    return rows


def _warnings(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in rows:
        if row.get("mapped") == "true" or not row.get("warning"):
            continue
        out.append(
            {
                "ref": row["tag"],
                "library_id": row.get("library_id") or "",
                "reason": "unmapped_article",
                "article": row["article"],
                "message": row["warning"],
            }
        )
    return out


def _write_csv(dest: Path, info: dict[str, str], rows: list[dict[str, str]]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    delimiter = info["delimiter"]
    with dest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter=delimiter, lineterminator="\r\n")
        if info["vendor"] == "weidmueller":
            for row in rows:
                writer.writerow([row["article"], row["tag"], _marker(row)])
            return
        writer.writerow(_PHOENIX_HEADER)
        for row in rows:
            writer.writerow(
                [
                    row["strip"],
                    row["position"],
                    row["tag"],
                    row["article"],
                    row["internal"],
                    row["external"],
                    row["jumper"],
                    row["accessory"],
                ]
            )


def write_strip_export(
    path: Path | str,
    project: Project,
    catalog: Catalog,
    strip: str,
    vendor: str,
    file_format: str | None = None,
) -> dict[str, Any]:
    info = vendor_format(vendor, file_format, path)
    rows = export_rows(project, catalog, strip, vendor=info["vendor"])
    dest = Path(path)
    warnings = _warnings(rows)
    if info["format"] == "xml":
        if info["vendor"] == "phoenix":
            write_phoenix_xml(
                dest,
                project_name=str(project.name or ""),
                strip=str(strip).strip(),
                rows=rows,
                warnings=warnings,
            )
        else:
            write_wago_xml(
                dest,
                project_name=str(project.name or ""),
                strip=str(strip).strip(),
                rows=rows,
                warnings=warnings,
            )
    else:
        _write_csv(dest, info, rows)
    mapped = sum(1 for row in rows if row.get("mapped") == "true")
    return {
        **info,
        "path": str(dest),
        "rows": len(rows),
        "strip": str(strip).strip(),
        "mapped": mapped,
        "unmapped": len(warnings),
        "warnings": warnings,
    }
