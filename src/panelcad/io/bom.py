"""Bill of materials — instance and grouped CSV, Excel-friendly, round-trippable."""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any, Iterable

from panelcad.core.electrical import is_drawing_only_mark
from panelcad.core.jumper import is_jumper_device, jumper_bom_rows
from panelcad.core.models import Component, Project
from panelcad.library.catalog import Catalog
from panelcad.library.panel import (
    ENVELOPE_AT_KEY,
    ENVELOPE_BY_KEY,
    ENVELOPE_CONFIRMED_KEY,
    ENVELOPE_SNAPSHOT_KEYS,
    NEEDS_INFO_KEY,
    NEEDS_INFO_LIBRARY_ID,
    assigned_catalog_ready,
    effective_footprint,
    envelope_is_confirmed,
    export_dimension_mm,
    is_child_contact,
    is_incabinet,
    strip_placeholder_guesses,
    _finite_num,
    _num,
    _truthy,
)
from panelcad.render.titleblock import working_rect

CORE_FIELDS = [
    "item",
    "qty",
    "sheet",
    "ref",
    "library_id",
    "description",
    "manufacturer",
    "part_number",
    "catalog",
    "source",
    "catalog_fallback",
    "catalog_primary",
    "category",
    "width_mm",
    "height_mm",
    "depth_mm",
    "din_rail",
    "incabinet",
    "in_panel",
    "mount",
    "dim_source",
    "notes",
    "x",
    "y",
]

PROP_FIELDS = ("manufacturer", "part_number", "catalog", "notes")

_ALIASES = {
    "quantity": "qty",
    "qty.": "qty",
    "count": "qty",
    "quantity required": "qty",
    "qty req": "qty",
    "reference": "ref",
    "tag": "ref",
    "tags": "ref",
    "refs": "ref",
    "designation": "ref",
    "library": "library_id",
    "type": "library_id",
    "symbol": "library_id",
    "component": "library_id",
    "name": "description",
    "desc": "description",
    "mfr": "manufacturer",
    "mfg": "manufacturer",
    "vendor": "manufacturer",
    "brand": "manufacturer",
    "make": "manufacturer",
    "part no": "part_number",
    "part_no": "part_number",
    "part no.": "part_number",
    "part number": "part_number",
    "part": "part_number",
    "item number": "part_number",
    "model": "part_number",
    "model number": "part_number",
    "manufacturer part number": "part_number",
    "mpn": "part_number",
    "pn": "part_number",
    "catalogue": "catalog",
    "catalog number": "catalog",
    "cat no": "catalog",
    "order_no": "catalog",
    "order number": "catalog",
    "sku": "catalog",
    "url": "source",
    "catalog source": "source",
    "source url": "source",
    "datasheet": "source",
    "width": "width_mm",
    "w": "width_mm",
    "height": "height_mm",
    "h": "height_mm",
    "depth": "depth_mm",
    "d": "depth_mm",
    "din": "din_rail",
    "din rail": "din_rail",
    "din-rail": "din_rail",
    "panel": "incabinet",
    "in panel": "incabinet",
    "in_panel": "incabinet",
    "in-cabinet": "incabinet",
    "in cabinet": "incabinet",
    "cabinet": "incabinet",
    "dims": "dim_source",
    "remark": "notes",
    "remarks": "notes",
    "comment": "notes",
    "line": "item",
    "line_no": "item",
    "item no": "item",
    "page": "sheet",
}


def _with_placeholder(notes: str, props: dict[str, Any], fp: Any) -> str:
    generic = str(getattr(fp, "dim_source", "") or "") == "generic" and (
        props.get("io_module") or props.get("io_module_ref") or props.get("placeholder_footprint")
    )
    marked = str(props.get("placeholder_footprint") or "").lower() in {"1", "true", "yes"}
    if not generic and not marked:
        return notes
    text = notes.strip()
    if "PLACEHOLDER" in text:
        return text
    return (text + " PLACEHOLDER footprint").strip()


def _accessory_rows(project: Project) -> list[dict[str, str]]:
    """5069-RTB18 and other cited accessories. No invented width, height, or depth."""

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for sheet in project.sheets:
        for comp in sheet.components:
            if not comp.properties.get("io_module") or comp.id in seen:
                continue
            raw = comp.properties.get("bom_accessories")
            if not isinstance(raw, list):
                continue
            seen.add(comp.id)
            for item in raw:
                if not isinstance(item, dict):
                    continue
                catalog = str(item.get("catalog") or "").strip()
                if not catalog:
                    continue
                rows.append(
                    {
                        "qty": "1",
                        "sheet": sheet.id,
                        "ref": comp.id,
                        "library_id": "accessory",
                        "description": str(item.get("description") or catalog),
                        "manufacturer": str(comp.properties.get("manufacturer") or ""),
                        "part_number": catalog,
                        "catalog": catalog,
                        "source": "",
                        "catalog_fallback": "",
                        "catalog_primary": "",
                        "category": "connection.terminal",
                        "width_mm": "",
                        "height_mm": "",
                        "depth_mm": "",
                        "din_rail": "",
                        "incabinet": "",
                        "in_panel": "",
                        "mount": "",
                        "dim_source": "",
                        "notes": str(item.get("note") or ""),
                        "x": "",
                        "y": "",
                    }
                )
    return rows


# Vendor lists route to the BOM when any of these survive header normalization.
BOM_CSV_ROUTE_FIELDS = frozenset(
    {"part_number", "catalog", "manufacturer", "description", "qty", "library_id", "ref"}
)
_EXPECTED_BOM_COLUMNS = (
    "Tag/Ref, Part Number, Manufacturer, Description, Qty (library_id optional)"
)
_LAYOUT_SHEET_KINDS = frozenset({"panel_layout", "door_layout"})


def _norm_header(raw: str) -> str:
    key = re.sub(r"\s+", " ", str(raw or "").strip().lower().replace("_", " "))
    key = key.replace("#", "").replace(".", "")
    key = re.sub(r"\s+", " ", key).strip()
    key = re.sub(r"[^a-z0-9 ]+$", "", key).strip()
    mapped = _ALIASES.get(key, key.replace(" ", "_"))
    return mapped


def unrecognized_csv_message(found: Iterable[str]) -> str:
    """Name the columns we saw and the parts-list columns we accept."""

    names = [str(name).strip() for name in found if str(name or "").strip()]
    shown = ", ".join(names) if names else "(none)"
    return (
        "Unrecognized CSV. For a parts list use File → Import BOM… or BOM → Import CSV…. "
        f"Expected columns: {_EXPECTED_BOM_COLUMNS}. "
        f"Found columns: {shown}."
    )


def csv_header_is_bom(headers: Iterable[str]) -> bool:
    """True when a CSV header is a parts list rather than wires or I/O."""

    return any(_norm_header(name) in BOM_CSV_ROUTE_FIELDS for name in headers)


def _bom_off(value: Any) -> bool:
    """True when this part is marked off the counted bill.

    YAML ``no`` loads as a boolean, and a library symbol can store that flag
    as ``False``. Both mean the same thing as the string ``no``.
    """

    if value is None or value == "":
        return False
    if isinstance(value, bool):
        return value is False
    return str(value).strip().lower() in {"no", "false", "off", "0", "n"}


def _component_bom_off(comp: Component, device: Any) -> bool:
    """Instance ``bom`` wins. A blank instance inherits the library symbol."""

    raw = (comp.properties or {}).get("bom", None)
    if raw in (None, "") and device is not None:
        raw = (getattr(device, "properties", None) or {}).get("bom", None)
    return _bom_off(raw)


def unresolved_needs_info_rows(project: Project) -> list[dict[str, str]]:
    """Reconstruct INFO NEEDED markers. Unpriced. Never mixed into the build BOM."""

    rows: list[dict[str, str]] = []
    for sheet in project.sheets:
        for comp in sheet.components:
            props = comp.properties or {}
            if str(props.get("reconstruct_needs_info") or "").lower() not in {"1", "true", "yes"} and str(
                comp.library_id or ""
            ) != "reconstruct.needs_info":
                continue
            # A print-import mark stays off the quote. A vendor needs-info row still lists.
            if str(props.get("quote") or "").lower() == "no":
                continue
            rows.append(
                {
                    "ref": comp.id,
                    "sheet": sheet.id,
                    "description": "INFO NEEDED",
                    "notes": str(props.get("needs_missing") or "unresolved"),
                    "price": "",
                    "qty": "1",
                }
            )
    return rows


def _ref_panel_tag(project: Project, ref: str) -> str:
    if not project.panels or not ref:
        return ""
    for sheet in project.sheets:
        for comp in sheet.components:
            if comp.id == ref:
                tag = _component_panel_tag(project, sheet, comp)
                if tag:
                    return tag
    return ""


def _component_panel_tag(project: Project, sheet: Any, comp: Component) -> str:
    if not project.panels:
        return ""
    from panelcad.core.panels import component_panel_tag

    return component_panel_tag(project, sheet, comp)


def instance_rows(project: Project, catalog: Catalog) -> list[dict[str, str]]:
    """One row per physical part. The same ref on another sheet is not a second part.

    Off-page arrows and other drawing marks are not parts, so they are omitted.
    """

    rows: list[dict[str, str]] = []
    seen: dict[str, dict[str, str]] = {}
    item = 1
    extra_keys: list[str] = []
    from panelcad.library.panel import export_dimension_mm, is_info_needed_placeholder

    for sheet in project.sheets:
        for comp in sheet.components:
            device = catalog.get(comp.library_id)
            if is_jumper_device(device) or is_child_contact(device, comp):
                continue
            if _component_bom_off(comp, device) or is_info_needed_placeholder(comp, device):
                continue
            if is_drawing_only_mark(device, comp):
                continue
            if comp.id in seen:
                existing = seen[comp.id]
                sheets = [part for part in str(existing.get("sheet") or "").split(",") if part]
                if sheet.id not in sheets:
                    existing["sheet"] = ",".join([*sheets, sheet.id])
                if project.panels and not str(existing.get("panel") or "").strip():
                    tag = _component_panel_tag(project, sheet, comp)
                    if tag:
                        existing["panel"] = tag
                continue
            props = {**device.properties, **comp.properties}
            fp = effective_footprint(device, comp)
            width_s, height_s, depth_s = export_dimension_mm(device, comp)
            stored_item = str(props.get("item") or "").strip()
            row = {
                "item": stored_item or str(item),
                "qty": "1",
                "sheet": sheet.id,
                "ref": comp.id,
                "library_id": comp.library_id,
                "description": str(props.get("description") or device.name),
                "manufacturer": str(props.get("manufacturer", "") or ""),
                "part_number": str(props.get("part_number", "") or ""),
                "catalog": str(props.get("catalog", "") or ""),
                "source": str(props.get("catalog_source") or props.get("source") or ""),
                "catalog_fallback": str(props.get("catalog_fallback") or ""),
                "catalog_primary": str(props.get("catalog_primary") or ""),
                "category": device.category,
                "width_mm": width_s,
                "height_mm": height_s,
                "depth_mm": depth_s,
                "din_rail": "yes" if fp.din_rail else "no",
                "incabinet": "yes" if is_incabinet(device, comp) else "no",
                "in_panel": "yes" if is_incabinet(device, comp) else "no",
                "mount": fp.mount,
                "dim_source": fp.dim_source,
                "notes": _with_placeholder(str(props.get("notes", "") or ""), props, fp),
                "x": f"{comp.x:g}",
                "y": f"{comp.y:g}",
            }
            if project.panels:
                row["panel"] = _component_panel_tag(project, sheet, comp)
                if "panel" not in extra_keys:
                    extra_keys.append("panel")
            for k, v in props.items():
                if k in PROP_FIELDS or k in row or k in {"catalog_source", "source", "io", "bom_accessories", "pin_labels"}:
                    continue
                row[str(k)] = str(v)
                if str(k) not in extra_keys:
                    extra_keys.append(str(k))
            seen[comp.id] = row
            rows.append(row)
            item += 1
    for row in _accessory_rows(project):
        row = dict(row)
        row["item"] = str(item)
        for key in row:
            if key not in CORE_FIELDS and key not in extra_keys:
                extra_keys.append(key)
        rows.append(row)
        item += 1
    for row in jumper_bom_rows(project, catalog):
        row = dict(row)
        row["item"] = str(item)
        for key in row:
            if key not in CORE_FIELDS and key not in extra_keys:
                extra_keys.append(key)
        rows.append(row)
        item += 1
    return _align_columns(rows, extra_keys)


def grouped_rows(
    project: Project,
    catalog: Catalog,
    *,
    by_panel: bool | None = None,
) -> list[dict[str, str]]:
    """Grouped bill. With two or more panels, the same part on two panels is two lines.

    ``by_panel=False`` sums those lines into one project total. A file with
    no panels list keeps the original columns.
    """

    buckets: dict[tuple[str, ...], dict[str, Any]] = {}
    order: list[tuple[str, ...]] = []
    counted: set[str] = set()
    from panelcad.library.panel import export_dimension_mm, is_info_needed_placeholder

    split = bool(project.panels) if by_panel is None else bool(by_panel)
    show_panel = bool(project.panels) and (split or by_panel is False)

    for sheet in project.sheets:
        for comp in sheet.components:
            device = catalog.get(comp.library_id)
            if is_jumper_device(device) or is_child_contact(device, comp):
                continue
            if _component_bom_off(comp, device) or is_info_needed_placeholder(comp, device):
                continue
            if is_drawing_only_mark(device, comp):
                continue
            props = {**device.properties, **comp.properties}
            fp = effective_footprint(device, comp)
            description = str(props.get("description") or device.name)
            panel_name = _component_panel_tag(project, sheet, comp) if project.panels else ""
            key = (
                comp.library_id,
                str(props.get("manufacturer", "") or ""),
                str(props.get("part_number", "") or ""),
                str(props.get("catalog", "") or ""),
                str(props.get("catalog_fallback") or ""),
                description,
                panel_name if split else "",
            )
            if key not in buckets:
                width_s, height_s, depth_s = export_dimension_mm(device, comp)
                buckets[key] = {
                    "qty": 0,
                    "sheet": sheet.id,
                    "refs": [],
                    "library_id": comp.library_id,
                    "description": description,
                    "manufacturer": key[1],
                    "part_number": key[2],
                    "catalog": key[3],
                    "source": str(props.get("catalog_source") or props.get("source") or ""),
                    "catalog_fallback": key[4],
                    "catalog_primary": str(props.get("catalog_primary") or ""),
                    "category": device.category,
                    "width_mm": width_s,
                    "height_mm": height_s,
                    "depth_mm": depth_s,
                    "din_rail": "yes" if fp.din_rail else "no",
                    "incabinet": "yes" if is_incabinet(device, comp) else "no",
                    "in_panel": "yes" if is_incabinet(device, comp) else "no",
                    "mount": fp.mount,
                    "dim_source": fp.dim_source,
                    "notes": _with_placeholder(str(props.get("notes", "") or ""), props, fp),
                    "panels": set(),
                }
                order.append(key)
            if show_panel and panel_name:
                buckets[key]["panels"].add(panel_name)
            if comp.id in counted:
                if sheet.id not in str(buckets[key]["sheet"]).split(","):
                    buckets[key]["sheet"] = f"{buckets[key]['sheet']},{sheet.id}"
                continue
            counted.add(comp.id)
            buckets[key]["qty"] += 1
            buckets[key]["refs"].append(comp.id)
            if sheet.id not in str(buckets[key]["sheet"]).split(","):
                buckets[key]["sheet"] = f"{buckets[key]['sheet']},{sheet.id}"
    for row in _accessory_rows(project):
        ref = str(row.get("ref") or "")
        accessory_panel = _ref_panel_tag(project, ref) if split else ""
        key = (
            str(row.get("library_id") or "accessory"),
            str(row.get("manufacturer") or ""),
            str(row.get("part_number") or ""),
            str(row.get("catalog") or ""),
            "",
            accessory_panel,
        )
        if key not in buckets:
            buckets[key] = {
                "qty": 0,
                "sheet": str(row.get("sheet") or ""),
                "refs": [],
                "library_id": key[0],
                "description": str(row.get("description") or ""),
                "manufacturer": key[1],
                "part_number": key[2],
                "catalog": key[3],
                "source": "",
                "catalog_fallback": "",
                "catalog_primary": "",
                "category": "connection.terminal",
                "width_mm": "",
                "height_mm": "",
                "depth_mm": "",
                "din_rail": "",
                "incabinet": "",
                "in_panel": "",
                "mount": "",
                "dim_source": "",
                "notes": str(row.get("notes") or ""),
                "panels": set(),
            }
            order.append(key)
        buckets[key]["qty"] += 1
        if ref and ref not in buckets[key]["refs"]:
            buckets[key]["refs"].append(ref)
        if show_panel and accessory_panel:
            buckets[key]["panels"].add(accessory_panel)
    for row in jumper_bom_rows(project, catalog):
        ref = str(row.get("ref") or "")
        jumper_panel = _ref_panel_tag(project, ref) if split else ""
        key = (
            str(row.get("library_id") or ""),
            str(row.get("manufacturer") or ""),
            str(row.get("part_number") or ""),
            str(row.get("catalog") or ""),
            str(row.get("description") or ""),
            jumper_panel,
        )
        try:
            qty = int(float(row.get("qty") or 1))
        except ValueError:
            qty = 1
        if key not in buckets:
            buckets[key] = {
                "qty": 0,
                "sheet": str(row.get("sheet") or ""),
                "refs": [],
                "library_id": key[0],
                "description": str(row.get("description") or ""),
                "manufacturer": key[1],
                "part_number": key[2],
                "catalog": key[3],
                "source": str(row.get("source") or row.get("catalog_source") or ""),
                "category": str(row.get("category") or "connection.jumper"),
                "width_mm": str(row.get("width_mm") or ""),
                "height_mm": str(row.get("height_mm") or ""),
                "depth_mm": str(row.get("depth_mm") or ""),
                "din_rail": str(row.get("din_rail") or "no"),
                "incabinet": str(row.get("incabinet") or "yes"),
                "in_panel": str(row.get("in_panel") or "yes"),
                "mount": str(row.get("mount") or "none"),
                "dim_source": str(row.get("dim_source") or ""),
                "notes": str(row.get("notes") or ""),
                "panels": set(),
            }
            order.append(key)
        buckets[key]["qty"] += max(1, qty)
        if ref and ref not in buckets[key]["refs"]:
            buckets[key]["refs"].append(ref)
        if show_panel and jumper_panel:
            buckets[key]["panels"].add(jumper_panel)
        sheet_id = str(row.get("sheet") or "")
        if sheet_id and sheet_id not in str(buckets[key]["sheet"]).split(","):
            buckets[key]["sheet"] = (
                f"{buckets[key]['sheet']},{sheet_id}" if buckets[key]["sheet"] else sheet_id
            )
    rows: list[dict[str, str]] = []
    live = [key for key in order if int(buckets[key]["qty"]) > 0]
    for i, key in enumerate(live, start=1):
        b = buckets[key]
        item = {
            "item": str(i),
            "qty": str(b["qty"]),
            "sheet": b["sheet"],
            "ref": ", ".join(b["refs"]),
            "library_id": b["library_id"],
            "description": b["description"],
            "manufacturer": b["manufacturer"],
            "part_number": b["part_number"],
            "catalog": b["catalog"],
            "source": b.get("source", ""),
            "catalog_fallback": b.get("catalog_fallback", ""),
            "catalog_primary": b.get("catalog_primary", ""),
            "category": b["category"],
            "width_mm": b["width_mm"],
            "height_mm": b["height_mm"],
            "depth_mm": b["depth_mm"],
            "din_rail": b["din_rail"],
            "incabinet": b.get("incabinet", b["in_panel"]),
            "in_panel": b["in_panel"],
            "mount": b["mount"],
            "dim_source": b["dim_source"],
            "notes": b["notes"],
            "x": "",
            "y": "",
        }
        if show_panel:
            names = sorted(name for name in (b.get("panels") or set()) if name)
            item["panel"] = ", ".join(names)
        rows.append(item)
    return rows


def bom_for_panel(project: Project, catalog: Catalog, panel: str) -> list[dict[str, str]]:
    """Grouped lines whose panel tag or id is ``panel``."""

    from panelcad.core.panels import find_panel, panel_tag

    found = find_panel(project, panel)
    if found is None:
        raise KeyError(f"no panel '{panel}'")
    want = {found.id.casefold(), panel_tag(found).casefold()}
    return [
        row
        for row in grouped_rows(project, catalog, by_panel=True)
        if str(row.get("panel") or "").casefold() in want
    ]


def _align_columns(rows: list[dict[str, str]], extra_keys: list[str]) -> list[dict[str, str]]:
    fields = CORE_FIELDS + [k for k in extra_keys if k not in CORE_FIELDS]
    aligned: list[dict[str, str]] = []
    for row in rows:
        aligned.append({k: row.get(k, "") for k in fields})
    return aligned


_NEEDS_INFO_HEADER = "needs info (not counted)"


def _stamp_bill_flags(row: dict[str, str], *, counted: bool) -> dict[str, str]:
    """Every exported row says whether its qty belongs in the order total."""

    out = dict(row)
    out["counted"] = "yes" if counted else "no"
    out["flag"] = "" if counted else "needs-info"
    return out


def with_needs_info_section(
    rows: list[dict[str, str]],
    project: Project,
    catalog: Catalog | None = None,
) -> list[dict[str, str]]:
    """Counted bill, then a needs-info group. Those rows are not part of the total.

    Dimensions stay blank. Nothing here invents a size. ``counted`` is yes only
    on rows whose qty is part of the order, so a sum of that column matches
    the counted bill.
    """

    from panelcad.needs_info import grouped_needs_info_rows

    extra = grouped_needs_info_rows(project, catalog=catalog)
    if not extra:
        return list(rows)
    fields = list(rows[0].keys()) if rows else list(CORE_FIELDS)
    out = [_stamp_bill_flags(row, counted=True) for row in rows]
    header = {key: "" for key in fields}
    header["item"] = "Needs info (not counted)"
    header["description"] = "Needs info (not counted)"
    header["notes"] = "needs-info"
    header = _stamp_bill_flags(header, counted=False)
    header["qty"] = ""
    out.append(header)
    for item in extra:
        row = {key: "" for key in fields}
        for key in (
            "qty",
            "ref",
            "description",
            "manufacturer",
            "part_number",
            "catalog",
            "library_id",
            "sheet",
        ):
            if key in row or key in ("qty", "ref", "description", "manufacturer", "part_number", "catalog"):
                row[key] = str(item.get(key) or "")
        row["notes"] = "needs-info"
        for key in ("width_mm", "height_mm", "depth_mm", "mount", "dim_source"):
            row[key] = ""
        # The quantity is on the row for the estimator. ``counted=no`` keeps it
        # out of an order total.
        out.append(_stamp_bill_flags(row, counted=False))
    return out


def exported_needs_info_kind(row: dict[str, str]) -> str:
    """``header`` or ``row`` for a needs-info line this program wrote. Else ``""``.

    Re-import must not turn the section heading into a new part.
    """

    description = " ".join(str(row.get("description") or "").split()).casefold()
    if description == _NEEDS_INFO_HEADER:
        return "header"
    flag = str(row.get("flag") or "").strip().casefold().replace("_", "-")
    if flag in {"needs-info", "needs info"}:
        return "row"
    return ""


def write_bom_csv(path: Path | str, rows: list[dict[str, str]]) -> None:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys()) if rows else list(CORE_FIELDS)
    # newline="" so csv's line ending is not translated again (Windows text
    # mode would turn \r\n into \r\r\n). utf-8-sig so Excel keeps the header.
    with dest.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\r\n")
        writer.writeheader()
        if rows:
            writer.writerows(rows)


def _csv_body_from_header(raw: str) -> str:
    """Drop title lines that sit above the parts-list header. Help says those lines are fine."""

    lines = raw.splitlines()
    for index, line in enumerate(lines):
        if not str(line).strip():
            continue
        try:
            cells = next(csv.reader([line]))
        except csv.Error:
            continue
        headers = [_norm_header(cell) for cell in cells]
        if any(key in CORE_FIELDS for key in headers) or csv_header_is_bom(cells):
            return "\n".join(lines[index:])
    return raw


def read_bom_csv(path: Path | str) -> list[dict[str, str]]:
    raw = _csv_body_from_header(Path(path).read_text(encoding="utf-8-sig"))
    reader = csv.DictReader(StringIO(raw))
    if not reader.fieldnames:
        raise ValueError(unrecognized_csv_message([]))
    raw_names = [name or "" for name in reader.fieldnames]
    headers = [_norm_header(name) for name in raw_names]
    if not any(key in CORE_FIELDS for key in headers):
        raise ValueError(unrecognized_csv_message(raw_names))
    rows: list[dict[str, str]] = []
    for src in reader:
        row: dict[str, str] = {}
        for orig, key in zip(reader.fieldnames, headers, strict=True):
            if not key:
                continue
            row[key] = (src.get(orig) or "").strip()
        if any(row.values()):
            rows.append(row)
    return rows


def split_refs(value: str) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in re.split(r"[,;]+", value) if p.strip()]


@dataclass
class BomChange:
    action: str
    ref: str
    detail: str = ""


@dataclass
class BomResult:
    updated: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "updated": self.updated,
            "created": self.created,
            "skipped": self.skipped,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def _components_with_ref(session: Any, ref: str) -> list[Component]:
    found: list[Component] = []
    for sheet in session.project.sheets:
        try:
            found.append(sheet.component(ref))
        except KeyError:
            continue
    return found


def _apply_props(session: Any, comp: Component, row: dict[str, str]) -> list[str]:
    skip = {
        "item",
        "qty",
        "sheet",
        "ref",
        "library_id",
        "description",
        "category",
        "x",
        "y",
        "name",
        "width_mm",
        "height_mm",
        "depth_mm",
        "din_rail",
        "incabinet",
        "in_panel",
        "mount",
        "dim_source",
        "source",
        "catalog_key",
        "catalog_verified",
        "catalog_source",
        ENVELOPE_CONFIRMED_KEY,
        ENVELOPE_BY_KEY,
        ENVELOPE_AT_KEY,
        *ENVELOPE_SNAPSHOT_KEYS,
        NEEDS_INFO_KEY,
        "reconstruct_needs_info",
        "flag",
        "counted",
    }
    panel_keys = (
        "width_mm",
        "height_mm",
        "depth_mm",
        "din_rail",
        "incabinet",
        "in_panel",
        "mount",
        "dim_source",
    )
    warnings: list[str] = []
    siblings = _components_with_ref(session, comp.id) or [comp]
    before = {
        key: siblings[0].properties.get(key)
        for key in ("width_mm", "height_mm", "depth_mm", "mount")
    }
    had_confirmation = any(envelope_is_confirmed(item) for item in siblings)
    csv_had_envelope = _truthy(row.get(ENVELOPE_CONFIRMED_KEY)) is True
    source = str(row.get("source") or "").strip()
    for sibling in siblings:
        for key, value in row.items():
            if key in skip or value == "":
                continue
            if key in panel_keys:
                continue
            sibling.properties[key] = value
    sku = str(row.get("catalog") or row.get("part_number") or "").strip()
    key = str(row.get("catalog_key") or "").strip()
    manufacturer = str(row.get("manufacturer") or "").strip() or None
    assigned = None
    if key:
        part = getattr(getattr(session, "parts", None), "parts", {}).get(key)
        if part is not None:
            assigned = session.try_assign_sku(comp.id, part.catalog, part.manufacturer)
    if assigned is None and sku:
        assigned = session.try_assign_sku(comp.id, sku, manufacturer)
    if assigned is not None:
        for sibling in siblings:
            if row.get("manufacturer"):
                sibling.properties["manufacturer"] = row["manufacturer"]
            if row.get("part_number"):
                sibling.properties["part_number"] = row["part_number"]
    else:
        drop = getattr(session, "drop_stale_assignment", None)
        if callable(drop):
            drop(comp.id)
    device = None
    try:
        device = session.catalog.get(siblings[0].library_id)
    except (AttributeError, KeyError):
        device = None
    parts = getattr(session, "parts", None)
    after_assign_ready = assigned_catalog_ready(siblings[0], parts=parts, device=device)
    for sibling in siblings:
        for key, value in row.items():
            if key not in panel_keys or value == "":
                continue
            if key in ("din_rail", "in_panel", "incabinet"):
                flag = value.lower() in ("1", "true", "yes")
                sibling.properties[key] = flag
                if key in ("incabinet", "in_panel"):
                    sibling.properties["incabinet"] = flag
                    sibling.properties["in_panel"] = flag
            elif key in ("width_mm", "height_mm", "depth_mm"):
                number = _num(value)
                if number is not None and not math.isfinite(number):
                    note = f"{comp.id}: ignored non-finite {key}"
                    if note not in warnings:
                        warnings.append(note)
                    continue
                if number is None:
                    continue
                sibling.properties[key] = number
            elif key == "dim_source":
                continue
            else:
                sibling.properties[key] = value
    changed = False
    for key in ("width_mm", "height_mm", "depth_mm"):
        incoming = _finite_num(row.get(key)) if row.get(key) not in ("", None) else None
        if incoming is None:
            continue
        prior = _finite_num(before.get(key))
        if prior is None or abs(incoming - prior) > 1e-6:
            changed = True
    incoming_mount = str(row.get("mount") or "").strip()
    if incoming_mount and incoming_mount != str(before.get("mount") or "").strip():
        changed = True
    if changed:
        from panelcad.reconstruct.layout import drop_envelope_confirmation

        prior_size = (
            before.get("width_mm"),
            before.get("height_mm"),
            before.get("depth_mm"),
        )
        for sibling in siblings:
            drop_envelope_confirmation(sibling, was=prior_size)
    ready = assigned_catalog_ready(siblings[0], parts=parts, device=device)
    for sibling in siblings:
        if str(row.get("dim_source") or "").strip() == "catalog" and ready:
            sibling.properties["dim_source"] = "catalog"
        elif str(sibling.properties.get("dim_source") or "") == "catalog" and not ready:
            if any(row.get(k) not in ("", None) for k in ("width_mm", "height_mm", "depth_mm")):
                sibling.properties["dim_source"] = "measured"
            else:
                sibling.properties.pop("dim_source", None)
        if source and ready:
            sibling.properties["catalog_source"] = source
    now_confirmed = any(envelope_is_confirmed(item) for item in siblings)
    if not ready and (
        (had_confirmation and not now_confirmed) or (csv_had_envelope and not now_confirmed)
    ):
        warnings.append(f"{comp.id}: envelope confirmation not imported; confirm in PanelCAD")
    if after_assign_ready and not ready and changed:
        warnings.append(f"{comp.id}: size changed on a catalog SKU; no longer catalog-ready")
    if str(comp.library_id or "") == NEEDS_INFO_LIBRARY_ID:
        cleaned = strip_placeholder_guesses(comp.library_id, comp.properties)
        for sibling in siblings:
            sibling.properties.clear()
            sibling.properties.update(dict(cleaned))
    else:
        for sibling in siblings:
            sibling.properties.pop(NEEDS_INFO_KEY, None)
    return warnings


def _find_component(project: Project, ref: str, sheet_id: str | None) -> tuple[str, Component] | None:
    sheets = project.sheets
    if sheet_id:
        try:
            sheets = [project.sheet(sheet_id)]
        except KeyError:
            sheets = project.sheets
    for sheet in sheets:
        for comp in sheet.components:
            if comp.id == ref:
                return (sheet.id, comp)
    for sheet in project.sheets:
        for comp in sheet.components:
            if comp.id == ref:
                return (sheet.id, comp)
    return None


class _GridPlacer:
    def __init__(self, sheet) -> None:
        wx, wy, ww, wh = working_rect(sheet)
        self.x = wx + 12
        self.y = wy + 12
        self.x0 = self.x
        self.xmax = wx + ww - 36
        self.dx = 42
        self.dy = 48

    def next_xy(self) -> tuple[float, float]:
        pos = (self.x, self.y)
        self.x += self.dx
        if self.x > self.xmax:
            self.x = self.x0
            self.y += self.dy
        return pos


def format_bom_import_report(result: dict[str, Any]) -> str:
    """Counts plus the reason each row was skipped or rejected."""

    updated = list(result.get("updated") or [])
    created = list(result.get("created") or [])
    skipped = list(result.get("skipped") or [])
    errors = list(result.get("errors") or [])
    warnings = list(result.get("warnings") or [])
    lines = [f"updated {len(updated)}, created {len(created)}, skipped {len(skipped)}"]

    def _extend(title: str, items: list[Any]) -> None:
        if not items:
            return
        lines.append("")
        if title:
            lines.append(title)
        lines.extend(str(item) for item in items[:12])
        extra = len(items) - 12
        if extra > 0:
            lines.append(f"… {extra} more")

    _extend("Skipped:", skipped)
    _extend("", errors)
    _extend("Warnings:", warnings)
    return "\n".join(lines)


def _vendor_identity(row: dict[str, str]) -> str:
    return (
        str(row.get("part_number") or "").strip()
        or str(row.get("catalog") or "").strip()
        or str(row.get("description") or "").strip()
    )


def _sheet_for_needs_info(session: Any, sheet_hint: str) -> str:
    """Keep an unsized vendor part off a backplate or door.

    A layout sheet would otherwise draw a stand-in box. Needs-info parts have
    no millimetres, so they stay on a schematic (or another non-layout sheet)
    until someone assigns a real library part.
    """

    def kind_of(sheet_id: str) -> str | None:
        if not sheet_id:
            return None
        try:
            return str(session.project.sheet(sheet_id).kind)
        except KeyError:
            return None

    hint = sheet_hint
    if kind_of(hint) is None:
        hint = str(getattr(session, "active_sheet_id", "") or "")
    if kind_of(hint) is None and session.project.sheets:
        hint = session.project.sheets[0].id
    hint_kind = kind_of(hint)
    if hint_kind is not None and hint_kind not in _LAYOUT_SHEET_KINDS:
        return hint
    active = str(getattr(session, "active_sheet_id", "") or "")
    active_kind = kind_of(active)
    if active_kind is not None and active_kind not in _LAYOUT_SHEET_KINDS:
        return active
    for sheet in session.project.sheets:
        if sheet.kind not in _LAYOUT_SHEET_KINDS:
            return sheet.id
    return hint


def _needs_info_properties(row: dict[str, str]) -> dict[str, Any]:
    """Text from the vendor row. No width, height, depth, or generic footprint."""

    identity = _vendor_identity(row)
    description = str(row.get("description") or "").strip() or identity or "INFO NEEDED"
    props: dict[str, Any] = {
        "reconstruct_needs_info": "yes",
        "bom": "no",
        "in_panel": "no",
        "incabinet": "no",
        "description": description,
        "needs_missing": f"library_id ({identity})" if identity else "library_id",
        "notes": "vendor parts list — needs a library part before build",
    }
    for key in ("manufacturer", "part_number", "catalog"):
        value = str(row.get(key) or "").strip()
        if value:
            props[key] = value
    source = str(row.get("source") or "").strip()
    if source:
        props["catalog_source"] = source
    notes = str(row.get("notes") or "").strip()
    if notes:
        props["notes"] = notes
    sealed = strip_placeholder_guesses(NEEDS_INFO_LIBRARY_ID, props)
    for key in list(sealed):
        if "mm" in str(key).lower():
            sealed.pop(key, None)
    sealed.pop("mount", None)
    sealed.pop("dim_source", None)
    sealed["reconstruct_needs_info"] = "yes"
    sealed["bom"] = "no"
    sealed["in_panel"] = "no"
    sealed["incabinet"] = "no"
    return {key: value for key, value in sealed.items() if value not in ("", None)}


def _next_needs_info_ref(session: Any, sheet_id: str) -> str:
    from panelcad.core.refs import next_tag

    used = {comp.id for sheet in session.project.sheets for comp in sheet.components}
    try:
        prefix = session.ref_prefix_for(NEEDS_INFO_LIBRARY_ID, sheet_id)
    except (AttributeError, KeyError):
        prefix = "U"
    return next_tag(str(prefix or "U"), used)


def _place_vendor_needs_info(
    session: Any,
    row: dict[str, str],
    ref: str,
    sheet_id: str,
    x: float,
    y: float,
) -> Component:
    """INFO NEEDED marker for a vendor row. Does not invent a library id or a size."""

    session.catalog.get(NEEDS_INFO_LIBRARY_ID)
    sheet = session.project.sheet(sheet_id)
    if any(comp.id == ref for comp in sheet.components):
        raise ValueError(f"component '{ref}' already exists on {sheet.id}")
    comp = Component(
        id=ref,
        library_id=NEEDS_INFO_LIBRARY_ID,
        x=float(x),
        y=float(y),
        properties=_needs_info_properties(row),
    )
    sheet.components.append(comp)
    return comp


def apply_bom_rows(session: Any, rows: Iterable[dict[str, str]], *, place_missing: bool = True) -> BomResult:
    """Update existing refs; optionally place missing library items. Does not delete."""
    result = BomResult()
    placers: dict[str, _GridPlacer] = {}
    origin_sheet = str(getattr(session, "active_sheet_id", "") or "")

    def placer(sheet_id: str) -> _GridPlacer:
        if sheet_id not in placers:
            placers[sheet_id] = _GridPlacer(session.project.sheet(sheet_id))
        return placers[sheet_id]

    for index, row in enumerate(rows, start=2):
        kind = exported_needs_info_kind(row)
        if kind:
            # Heading and needs-info lines are not new parts. Existing refs stay.
            result.skipped.append(f"row {index}: needs-info, not counted")
            continue
        library_id = row.get("library_id", "").strip()
        sheet_hint = row.get("sheet", "").strip() or origin_sheet
        refs = split_refs(row.get("ref", ""))
        identity = _vendor_identity(row)
        try:
            qty = int(float(row.get("qty") or 1))
        except ValueError:
            qty = 1
        qty = max(1, qty)

        x_raw, y_raw = row.get("x", "").strip(), row.get("y", "").strip()

        def place_vendor(ref: str | None) -> None:
            sid = _sheet_for_needs_info(session, sheet_hint)
            if x_raw and y_raw and sid == sheet_hint:
                try:
                    x, y = float(x_raw), float(y_raw)
                except ValueError:
                    x, y = placer(sid).next_xy()
            else:
                x, y = placer(sid).next_xy()
            label = identity or ref or "(no ref)"
            from panelcad.needs_info import try_place_exact_catalog

            try:
                matched = try_place_exact_catalog(session, row, ref, sid, x, y)
            except (KeyError, ValueError) as exc:
                result.errors.append(f"row {index} ({label}): {exc}")
                return
            if matched is not None:
                session.active_sheet_id = sid
                result.created.append(matched.id)
                return
            chosen = ref or _next_needs_info_ref(session, sid)
            label = identity or chosen
            try:
                comp = _place_vendor_needs_info(session, row, chosen, sid, x, y)
            except (KeyError, ValueError) as exc:
                result.errors.append(f"row {index} ({label}): {exc}")
                return
            session.active_sheet_id = sid
            result.created.append(comp.id)

        def place_one(ref: str | None) -> str | None:
            if not library_id:
                label = ref or identity or "(no ref)"
                result.errors.append(f"row {index} ({label}): cannot place: missing library_id")
                return None
            if library_id not in session.catalog.devices:
                result.errors.append(f"unknown library_id '{library_id}'")
                return None
            placed_device = session.catalog.get(library_id)
            if is_jumper_device(placed_device):
                result.skipped.append(ref or library_id)
                return None
            try:
                session.project.sheet(sheet_hint)
                sid = sheet_hint
            except KeyError:
                sid = session.active_sheet_id
            if x_raw and y_raw:
                x, y = float(x_raw), float(y_raw)
            else:
                x, y = placer(sid).next_xy()
            session.active_sheet_id = sid
            from panelcad.reconstruct.placeholder import reconstruct_mode

            if library_id == NEEDS_INFO_LIBRARY_ID and not reconstruct_mode(session):
                result.errors.append(
                    f"cannot place {ref or library_id}: needs-info placeholders "
                    "can only be created in reconstruct mode"
                )
                return None
            try:
                comp = session.place(library_id, ref=ref, x=x, y=y, sheet_id=sid)
            except ValueError as exc:
                result.errors.append(str(exc))
                return None
            result.warnings.extend(_apply_props(session, comp, row))
            result.created.append(comp.id)
            return comp.id

        if refs:
            for ref in refs:
                found = _find_component(session.project, ref, sheet_hint)
                if found is not None:
                    _sid, comp = found
                    result.warnings.extend(_apply_props(session, comp, row))
                    if x_raw and y_raw:
                        comp.x = float(x_raw)
                        comp.y = float(y_raw)
                    if library_id and library_id != comp.library_id:
                        # keep symbol; record part data only
                        comp.properties["bom_library_id"] = library_id
                    result.updated.append(ref)
                elif place_missing and not library_id and identity:
                    place_vendor(ref)
                elif place_missing:
                    place_one(ref)
                else:
                    result.skipped.append(
                        f"row {index} ({ref}): not on the drawing"
                        + (f" ({identity})" if identity else "")
                    )
            continue

        if not library_id:
            if identity:
                if not place_missing:
                    result.skipped.append(
                        f"row {index} ({identity}): needs Tag/Ref to update an existing part "
                        "or library_id to place one"
                    )
                    continue
                for _ in range(qty):
                    place_vendor(None)
                continue
            result.skipped.append(
                f"row {index}: no Tag/Ref, library_id, part number, or description"
            )
            continue
        if not place_missing:
            result.skipped.append(f"row {index} ({library_id}): not placed")
            continue
        for _ in range(qty):
            place_one(None)
    result.warnings = list(dict.fromkeys(result.warnings))
    result.updated = list(dict.fromkeys(result.updated))
    return result
