"""Needs-info parts: vendor rows, datasheet proposals, and save/reopen.

A needs-info part keeps the vendor text, a free-text note, any partial entry,
and any datasheet proposal that has not been accepted. Save and autosave write
those fields with the project. Nothing becomes a millimetre until the user
accepts a complete cited proposal or types the datasheet values.

AI lookup only proposes. A value without an http(s) datasheet URL is dropped.
A dimension with no unit is kept and flagged; it is not applied until the user
confirms the units. A partial proposal stays on the part and the part stays
needs-info.
"""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any

from panelcad.core.models import Device, PanelFootprint
from panelcad.core.primitives import Primitive
from panelcad.library.catalog import _device_from_raw, device_to_raw
from panelcad.library.panel import apply_confirmed_envelope, require_explicit_envelope
from panelcad.reconstruct.placeholder import is_needs_info_component

DRAFT_KEY = "needs_info_draft"
PROPOSALS_KEY = "lookup_proposals"
LOOKUP_NOTE_KEY = "lookup_note"
RESOLVED_KEY = "resolved_library"
DATASHEET_KEY = "datasheet"

REQUIRED_FIELDS = ("width_mm", "height_mm", "depth_mm", "mount", "category")
_MM_FIELDS = ("width_mm", "height_mm", "depth_mm")
UNITS_UNSPECIFIED = "unspecified"
_MM_SUFFIXES = ("millimetres", "millimeters", "millimetre", "millimeter", "mm")
_UNAPPLIED_KEYS = ("width_mm", "height_mm", "depth_mm", "mount", "dim_source", "din_rail")
_PLACEHOLDER_KEYS = (
    "reconstruct_needs_info",
    "bom",
    "in_panel",
    "incabinet",
    "needs_missing",
    DRAFT_KEY,
    PROPOSALS_KEY,
)
_FIELD_ALIASES = {
    "width": "width_mm",
    "w": "width_mm",
    "width_mm": "width_mm",
    "height": "height_mm",
    "h": "height_mm",
    "height_mm": "height_mm",
    "depth": "depth_mm",
    "d": "depth_mm",
    "depth_mm": "depth_mm",
    "mount": "mount",
    "mounting": "mount",
    "category": "category",
    "family": "category",
    "library_family": "category",
}
_MANUAL_SOURCE = "entered from datasheet"


def needs_info_components(project: Any) -> list[Any]:
    found: list[Any] = []
    for sheet in getattr(project, "sheets", []) or []:
        for comp in getattr(sheet, "components", []) or []:
            if is_needs_info_component(comp):
                found.append(comp)
    return found


def save_status_note(project: Any) -> str:
    """Quiet save note. Empty when every part is resolved."""

    count = len(needs_info_components(project))
    if count <= 0:
        return ""
    if count == 1:
        return "1 part still needs info"
    return f"{count} parts still need info"


def lookup_status(client: Any = None) -> dict[str, Any]:
    """Whether datasheet lookup may run. A missing account is off, not a guess."""

    from panelcad.ai.config import TURNED_OFF_REASON, configuration_reason

    flag = os.environ.get("PANELCAD_AI_LOOKUP", "").strip().lower()
    web = os.environ.get("PANELCAD_WEB_ACCESS", "").strip().lower()
    if flag in {"0", "off", "false", "no"}:
        return _off(TURNED_OFF_REASON)
    if web in {"0", "off", "false", "no"}:
        return _off(
            "Web access is off, so a datasheet cannot be looked up. "
            "Enter dimensions from the datasheet."
        )
    if client is not None:
        return {"available": True, "reason": "", "offer": ""}
    reason = configuration_reason()
    if reason:
        return _off(reason)
    return {"available": True, "reason": "", "offer": ""}


def default_lookup_client() -> Any | None:
    """The user's configured provider. No key is built into PanelCAD."""

    if not lookup_status().get("available"):
        return None
    try:
        from panelcad.ai.config import load_ai_lookup
        from panelcad.ai.lookup import DatasheetLookupClient

        return DatasheetLookupClient(load_ai_lookup())
    except Exception:  # noqa: BLE001 — a missing account is the off state
        return None


def find_component(session: Any, ref: str) -> tuple[Any, Any]:
    want = str(ref or "").strip()
    project = getattr(session, "project", None)
    for sheet in getattr(project, "sheets", []) or []:
        for comp in sheet.components:
            if comp.id == want:
                return sheet, comp
    return None, None


def set_lookup_note(comp: Any, text: str) -> None:
    """Free-text 'what's missing / where I looked'. Empty clears it."""

    note = str(text or "").strip()
    if note:
        comp.properties[LOOKUP_NOTE_KEY] = note
    else:
        comp.properties.pop(LOOKUP_NOTE_KEY, None)


def update_draft(comp: Any, fields: dict[str, Any], note: str | None = None) -> dict[str, Any]:
    """Store typed values without writing millimetres onto the part."""

    if not is_needs_info_component(comp):
        return {"ok": False, "applied": False, "reason": "already resolved"}
    draft = dict(comp.properties.get(DRAFT_KEY) or {})
    allowed = set(REQUIRED_FIELDS) | {"source"}
    for key, raw in fields.items():
        name = _FIELD_ALIASES.get(str(key or "").strip().lower(), "")
        if name not in allowed and str(key) not in allowed:
            continue
        field = name or str(key)
        if field not in allowed:
            continue
        text = str(raw or "").strip()
        if not text or text in {"0", "0.0"}:
            draft.pop(field, None)
            continue
        if field in _MM_FIELDS:
            parsed = _parse_mm(text)
            if parsed is None:
                continue
            draft[field] = parsed
            continue
        if field == "mount":
            mount = _parse_mount(text)
            if not mount:
                continue
            draft[field] = mount
            continue
        if field == "source":
            url = _http_url(text)
            if not url:
                continue
            draft[field] = url
            continue
        draft[field] = text[:80]
    if draft:
        comp.properties[DRAFT_KEY] = draft
    else:
        comp.properties.pop(DRAFT_KEY, None)
    if note is not None:
        set_lookup_note(comp, note)
    _strip_unapplied(comp)
    return {"ok": True, "applied": False, "draft": dict(draft)}


def parse_lookup_reply(reply: Any) -> dict[str, Any]:
    """Keep cited datasheet values. Drop everything else. Do not estimate."""

    data = _as_dict(reply)
    cited: dict[str, dict[str, str]] = {}
    rejected: list[dict[str, str]] = []
    for item in _entries(data):
        field = _FIELD_ALIASES.get(str(item.get("field") or "").strip().lower(), "")
        if field not in REQUIRED_FIELDS:
            continue
        source = _http_url(str(item.get("source") or item.get("url") or ""))
        units = ""
        if field in _MM_FIELDS:
            parsed = _proposed_dimension(item.get("value"))
            value = parsed[0] if parsed else ""
            units = parsed[1] if parsed else ""
        elif field == "mount":
            value = _parse_mount(item.get("value"))
        else:
            value = str(item.get("value") or "").strip()[:80]
        if not source or not value:
            rejected.append(
                {
                    "field": field,
                    "reason": "no source" if not source else "no datasheet value",
                }
            )
            continue
        page = _page_text(item)
        confidence = str(item.get("confidence") or "").strip()[:40]
        row = {
            "field": field,
            "value": value,
            "source": source,
            "page": page,
            "confidence": confidence,
        }
        if units == UNITS_UNSPECIFIED:
            row["units"] = UNITS_UNSPECIFIED
        cited[field] = row
    ordered = [cited[name] for name in REQUIRED_FIELDS if name in cited]
    return {"cited": ordered, "rejected": rejected}


def store_lookup_reply(
    comp: Any,
    reply: Any,
    *,
    fetch: Any = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Remember proposals. A value is cited only after its URL is checked.

    Does not write width, height, depth, or mount. A URL that is not 200, or
    whose page does not contain the value, is stored as unverified.
    """

    if not is_needs_info_component(comp):
        return {"ok": False, "applied": False, "reason": "already resolved", "stored": []}
    parsed = parse_lookup_reply(reply)
    cited = parsed["cited"]
    if cited:
        from panelcad.ai.sources import default_source_fetch, verify_citations

        checked = verify_citations(cited, fetch or default_source_fetch, timeout)
        comp.properties[PROPOSALS_KEY] = checked
    else:
        checked = []
    _strip_unapplied(comp)
    missing = [name for name in REQUIRED_FIELDS if name not in {item["field"] for item in checked}]
    return {
        "ok": True,
        "applied": False,
        "stored": [item["field"] for item in checked],
        "rejected": parsed["rejected"],
        "missing": missing,
        "complete": not missing,
    }


def accept_lookup(session: Any, ref: str, confirmations: Any = None) -> dict[str, Any]:
    """Apply a stored proposal when every field is cited or the user checked it.

    ``confirmations`` maps a field name to true when the user ticked
    "I checked the datasheet" for that field, or confirmed the units on a
    dimension that did not state them. Unverified fields and unitless
    dimensions without that tick are refused. The tick is stored on the
    accepted citation.
    """

    _sheet, comp = find_component(session, ref)
    if comp is None:
        return {"ok": False, "applied": False, "reason": "missing part"}
    if not is_needs_info_component(comp):
        return {"ok": False, "applied": False, "reason": "already resolved"}
    with session.command("datasheet answer"):
        return _accept_lookup_body(session, comp, confirmations)


def _accept_lookup_body(session: Any, comp: Any, confirmations: Any) -> dict[str, Any]:
    cited = [
        item
        for item in list(comp.properties.get(PROPOSALS_KEY) or [])
        if isinstance(item, dict) and _http_url(str(item.get("source") or ""))
    ]
    by_field = {str(item.get("field") or ""): item for item in cited}
    missing = [name for name in REQUIRED_FIELDS if name not in by_field]
    if missing:
        _strip_unapplied(comp)
        reason = "no source" if not by_field else "incomplete"
        return {"ok": False, "applied": False, "reason": reason, "missing": missing}
    checked = _confirmation_fields(confirmations)
    unverified = [
        name
        for name in REQUIRED_FIELDS
        if str(by_field[name].get("status") or "") != "cited" and name not in checked
    ]
    unconfirmed_units = [
        name
        for name in _MM_FIELDS
        if str(by_field[name].get("units") or "") == UNITS_UNSPECIFIED and name not in checked
    ]
    if unverified or unconfirmed_units:
        _strip_unapplied(comp)
        if unverified:
            refused: dict[str, Any] = {
                "ok": False,
                "applied": False,
                "reason": "unverified",
                "unverified": unverified,
            }
            if unconfirmed_units:
                refused["unconfirmed_units"] = unconfirmed_units
            return refused
        return {
            "ok": False,
            "applied": False,
            "reason": "confirm units",
            "unconfirmed_units": unconfirmed_units,
        }
    prepared: list[dict[str, str]] = []
    for name in REQUIRED_FIELDS:
        item = dict(by_field[name])
        if name in checked:
            if str(item.get("units") or "") == UNITS_UNSPECIFIED:
                item["units"] = "mm"
                if str(item.get("status") or "") != "cited":
                    item["confirmation"] = "I checked the datasheet; units confirmed"
                else:
                    item["confirmation"] = "units confirmed"
            else:
                item["confirmation"] = "I checked the datasheet"
            by_field[name]["confirmation"] = item["confirmation"]
            if "units" in item:
                by_field[name]["units"] = item["units"]
        prepared.append(item)
    return _apply_resolution(session, comp, prepared, require_url=True)


def accept_manual(session: Any, ref: str) -> dict[str, Any]:
    """Turn a complete typed draft into a library part. Partial drafts stay put."""

    _sheet, comp = find_component(session, ref)
    if comp is None:
        return {"ok": False, "applied": False, "reason": "missing part"}
    if not is_needs_info_component(comp):
        return {"ok": False, "applied": False, "reason": "already resolved"}
    with session.command("needs info"):
        return _accept_manual_body(session, comp)


def _accept_manual_body(session: Any, comp: Any) -> dict[str, Any]:
    draft = dict(comp.properties.get(DRAFT_KEY) or {})
    source = _http_url(str(draft.get("source") or "")) or _MANUAL_SOURCE
    cited: list[dict[str, str]] = []
    missing: list[str] = []
    for name in REQUIRED_FIELDS:
        value = str(draft.get(name) or "").strip()
        if name in _MM_FIELDS:
            value = _parse_mm(value) or ""
        elif name == "mount":
            value = _parse_mount(value) or ""
        if not value:
            missing.append(name)
            continue
        cited.append(
            {
                "field": name,
                "value": value,
                "source": source,
                "page": "",
                "confidence": "entered",
            }
        )
    if missing:
        _strip_unapplied(comp)
        return {"ok": False, "applied": False, "reason": "incomplete", "missing": missing}
    return _apply_resolution(session, comp, cited, require_url=False)


def lookup_part(
    session: Any,
    ref: str,
    client: Any = None,
    *,
    fetch: Any = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Ask for a datasheet proposal and store it. Does not accept it."""

    ready = _client_or_reason(client)
    if isinstance(ready, dict):
        return ready
    _sheet, comp = find_component(session, ref)
    if comp is None or not is_needs_info_component(comp):
        return {"ok": False, "applied": False, "reason": "not a needs-info part"}
    props = comp.properties or {}
    try:
        reply = ready.lookup(
            manufacturer=str(props.get("manufacturer") or ""),
            part_number=str(props.get("part_number") or props.get("catalog") or ""),
            description=str(props.get("description") or ""),
        )
    except Exception:  # noqa: BLE001 — the part stays needs-info
        return {
            "ok": False,
            "applied": False,
            "reason": "Datasheet lookup failed. Enter dimensions from the datasheet.",
            "offer": "manual",
        }
    if isinstance(reply, dict) and reply.get("error") and not reply.get("values") and not reply.get("fields"):
        return {
            "ok": False,
            "applied": False,
            "reason": str(reply.get("error") or "Datasheet lookup failed."),
            "offer": "manual",
        }
    with session.command("datasheet lookup"):
        stored = store_lookup_reply(comp, reply, fetch=fetch, timeout=timeout)
    stored["ref"] = comp.id
    return stored


def lookup_all(
    session: Any,
    client: Any = None,
    *,
    fetch: Any = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Propose datasheet values for every needs-info part. Does not accept them."""

    ready = _client_or_reason(client)
    if isinstance(ready, dict):
        ready["updated"] = []
        return ready
    updated: list[str] = []
    errors: list[str] = []
    with session.command("datasheet lookup"):
        for comp in needs_info_components(session.project):
            result = lookup_part(session, comp.id, ready, fetch=fetch, timeout=timeout)
            if result.get("ok"):
                updated.append(comp.id)
            elif result.get("reason"):
                errors.append(f"{comp.id}: {result['reason']}")
    return {"ok": True, "applied": False, "updated": updated, "errors": errors}


def exact_catalog_match(parts: Any, row: dict[str, Any]) -> Any | None:
    """A parts-library row with the same catalog number and a published footprint."""

    part = _catalog_row(parts, row)
    if part is None or not part.has_footprint():
        return None
    return part


def try_place_exact_catalog(
    session: Any,
    row: dict[str, Any],
    ref: str | None,
    sheet_id: str,
    x: float,
    y: float,
) -> Any | None:
    """Place a real catalog part when the vendor number matches a published footprint."""

    part = exact_catalog_match(getattr(session, "parts", None), row)
    if part is None:
        return None
    chosen = str(ref or "").strip() or None
    comp = session.place(part.library_id, ref=chosen, x=float(x), y=float(y), sheet_id=sheet_id)
    try:
        session.assign_part(comp.id, part.catalog, part.manufacturer)
    except (KeyError, ValueError):
        sheet = session.project.sheet(sheet_id)
        sheet.components = [item for item in sheet.components if item.id != comp.id]
        return None
    return comp


def resolve_with_catalog(session: Any, ref: str, part: Any) -> dict[str, Any]:
    """Replace one needs-info part with a catalog SKU that has published dimensions."""

    if part is None or not getattr(part, "has_footprint", lambda: False)():
        return {
            "ok": False,
            "applied": False,
            "reason": "catalog part has no published dimensions",
        }
    _sheet, comp = find_component(session, ref)
    if comp is None:
        return {"ok": False, "applied": False, "reason": "missing part"}
    if not is_needs_info_component(comp):
        return {"ok": False, "applied": False, "reason": "already resolved"}
    saved_id = comp.library_id
    saved = copy.deepcopy(dict(comp.properties))
    comp.library_id = str(part.library_id)
    _clear_placeholder(comp)
    try:
        session.assign_part(comp.id, part.catalog, part.manufacturer)
    except (KeyError, ValueError) as exc:
        comp.library_id = saved_id
        comp.properties.clear()
        comp.properties.update(saved)
        return {"ok": False, "applied": False, "reason": str(exc)}
    return {"ok": True, "applied": True, "library_id": comp.library_id, "ref": comp.id}


def part_reason(session: Any, comp: Any) -> str:
    props = comp.properties or {}
    base = _catalog_gap(getattr(session, "parts", None), props)
    extra: list[str] = []
    proposals = [item for item in list(props.get(PROPOSALS_KEY) or []) if isinstance(item, dict)]
    if proposals:
        names = ", ".join(str(item.get("field") or "") for item in proposals if item.get("field"))
        extra.append(f"datasheet proposal not accepted ({names})")
    if dict(props.get(DRAFT_KEY) or {}):
        extra.append("partial values entered, not applied")
    if extra:
        return base + "; " + "; ".join(extra)
    return base


def _listed_needs_info(comp: Any, device: Any = None) -> bool:
    """A placeholder kept off the counted bill. Same test the BOM uses."""

    from panelcad.library.panel import is_info_needed_placeholder

    return is_info_needed_placeholder(comp, device)


def grouped_needs_info_rows(project: Any, session: Any = None, catalog: Any = None) -> list[dict[str, str]]:
    """One row per maker and part number. Shown, not counted, and never sized.

    The same ref on another sheet is the same part. Two placements of one
    vendor number share a quantity. Millimetre columns stay empty.
    """

    book = catalog if catalog is not None else getattr(session, "catalog", None)
    buckets: dict[tuple[str, ...], dict[str, Any]] = {}
    order: list[tuple[str, ...]] = []
    by_id: dict[str, tuple[str, ...]] = {}
    for sheet in getattr(project, "sheets", []) or []:
        for comp in getattr(sheet, "components", []) or []:
            device = None
            if book is not None:
                try:
                    device = book.get(comp.library_id)
                except (AttributeError, KeyError):
                    device = None
            if not _listed_needs_info(comp, device):
                continue
            if comp.id in by_id:
                bucket = buckets[by_id[comp.id]]
                if sheet.id not in bucket["sheets"]:
                    bucket["sheets"].append(sheet.id)
                continue
            props = comp.properties or {}
            maker = str(props.get("manufacturer") or "").strip()
            number = str(props.get("part_number") or "").strip()
            catalog = str(props.get("catalog") or "").strip()
            description = str(props.get("description") or "").strip()
            identity = (maker.casefold(), number.casefold(), catalog.casefold(), description.casefold())
            if not any(identity):
                identity = ("ref", str(comp.id).casefold(), "", "")
            if identity not in buckets:
                buckets[identity] = {
                    "refs": [],
                    "sheets": [],
                    "description": description,
                    "manufacturer": maker,
                    "part_number": number,
                    "catalog": catalog,
                    "notes": [],
                    "reasons": [],
                    "library_id": str(getattr(comp, "library_id", "") or ""),
                }
                order.append(identity)
            bucket = buckets[identity]
            by_id[comp.id] = identity
            bucket["refs"].append(comp.id)
            if sheet.id not in bucket["sheets"]:
                bucket["sheets"].append(sheet.id)
            vendor_notes = str(props.get("notes") or "").strip()
            looked = str(props.get(LOOKUP_NOTE_KEY) or "").strip()
            reason = part_reason(session, comp) if session is not None else ""
            for bit in (vendor_notes, reason, looked):
                if bit and bit not in bucket["notes"]:
                    bucket["notes"].append(bit)
            if reason and reason not in bucket["reasons"]:
                bucket["reasons"].append(reason)
    rows: list[dict[str, str]] = []
    for identity in order:
        bucket = buckets[identity]
        reason = " | ".join(bucket["reasons"])
        rows.append(
            {
                "item": "",
                "qty": str(len(bucket["refs"])),
                "ref": ", ".join(bucket["refs"]),
                "description": bucket["description"],
                "manufacturer": bucket["manufacturer"],
                "part_number": bucket["part_number"],
                "catalog": bucket["catalog"],
                "source": "",
                "catalog_fallback": "",
                "catalog_primary": "",
                "width_mm": "",
                "height_mm": "",
                "depth_mm": "",
                "din_rail": "",
                "incabinet": "",
                "mount": "",
                "notes": " | ".join(bucket["notes"]),
                "library_id": bucket["library_id"],
                "sheet": ",".join(bucket["sheets"]),
                "reason": reason,
                "lookup_note": "",
                "flag": "needs-info",
            }
        )
    return rows


def needs_info_display_rows(session: Any) -> list[dict[str, str]]:
    """BOM rows that are shown and not counted. No millimetre columns."""

    return grouped_needs_info_rows(session.project, session)


def import_guidance_text(session: Any, result: dict[str, Any]) -> str:
    """Tell the user which new parts need info, and why."""

    from panelcad.io.bom import format_bom_import_report

    created = set(result.get("created") or [])
    lines = [format_bom_import_report(result), ""]
    needs = [
        comp
        for sheet in session.project.sheets
        for comp in sheet.components
        if comp.id in created and is_needs_info_component(comp)
    ]
    if not needs:
        return lines[0]
    lines.append(
        f"{save_status_note(session.project)}. "
        "The print stays NOT FOR BUILD until each one is a library part. "
        "Nothing here guessed a millimetre."
    )
    lines.append("")
    grouped: dict[tuple[str, str, str], list[str]] = {}
    reasons: dict[tuple[str, str, str], str] = {}
    for comp in needs:
        props = comp.properties or {}
        key = (
            str(props.get("manufacturer") or ""),
            str(props.get("part_number") or ""),
            str(props.get("description") or ""),
        )
        grouped.setdefault(key, []).append(comp.id)
        reasons.setdefault(key, part_reason(session, comp))
    for key, refs in grouped.items():
        maker, number, description = key
        label = " ".join(bit for bit in (maker, number) if bit) or refs[0]
        lines.append(f"{label} ×{len(refs)} — {description} — {reasons[key]} — {', '.join(refs)}")
    status = lookup_status()
    lines.append("")
    if status.get("available"):
        lines.append("AI lookup proposes datasheet values. Nothing is applied until you accept them.")
    else:
        lines.append(str(status.get("reason") or ""))
    return "\n".join(lines).strip()


def unrecognized_prompt(session: Any, raw: str) -> str:
    quoted = " ".join(str(raw or "").split())
    if len(quoted) > 80:
        quoted = quoted[:77] + "..."
    note = save_status_note(getattr(session, "project", None))
    if note:
        return (
            f'Didn\'t recognize "{quoted}". {note} '
            "(no catalog match and no dimensions). "
            'Type "resolve needs-info parts" to list them. Sizes are not guessed.'
        )
    return (
        "couldn't do that. Try: add an ethernet switch to ethernet · "
        "delete H2 · wire A2.Y1 to K2.A1"
    )


def resolve_prompt(session: Any) -> str:
    comps = needs_info_components(session.project)
    if not comps:
        return "No needs-info parts on this drawing."
    lines = [
        f"{save_status_note(session.project)}. "
        "The print stays NOT FOR BUILD until each one is resolved."
    ]
    for row in needs_info_display_rows(session):
        lines.append(
            f"{row['ref']}  {row['manufacturer']} {row['part_number']} — "
            f"{row['description']} — {row['reason']}"
        )
    lines.append(
        "Match a catalog part, enter dimensions from the datasheet, or use AI lookup. "
        "AI lookup only proposes a value that has a datasheet URL. "
        "A dimension with no unit is not applied until you confirm the units. "
        "Nothing is applied until you accept it."
    )
    return "\n".join(lines)


def part_snapshot(session: Any) -> list[dict[str, Any]]:
    """Stable view of every part used to prove save/reopen."""

    rows: list[dict[str, Any]] = []
    for sheet in session.project.sheets:
        for comp in sheet.components:
            props = dict(comp.properties or {})
            panel = None
            try:
                device = session.catalog.get(comp.library_id)
            except KeyError:
                device = None
            if device is not None and not is_needs_info_component(comp):
                panel = {
                    "width_mm": device.panel.width_mm,
                    "height_mm": device.panel.height_mm,
                    "depth_mm": device.panel.depth_mm,
                    "mount": device.panel.mount,
                    "dim_source": device.panel.dim_source,
                    "category": device.category,
                }
            rows.append(
                {
                    "id": comp.id,
                    "library_id": comp.library_id,
                    "sheet": sheet.id,
                    "needs_info": is_needs_info_component(comp),
                    "properties": props,
                    "panel": panel,
                }
            )
    return rows


def install_resolved_devices(session: Any) -> None:
    """Rebuild accepted library parts stored inside an older or recovered file."""

    catalog = getattr(session, "catalog", None)
    project = getattr(session, "project", None)
    if catalog is None or project is None:
        return
    for sheet in project.sheets:
        for comp in sheet.components:
            embed = (comp.properties or {}).get(RESOLVED_KEY)
            if not isinstance(embed, dict):
                continue
            raw = embed.get("device")
            if not isinstance(raw, dict) or not str(raw.get("id") or "").strip():
                continue
            lib = str(embed.get("library") or "project")
            device = _device_from_raw(lib, raw)
            catalog.devices.setdefault(device.library_id, device)


def write_resolved_library(session: Any) -> None:
    """Write accepted datasheet parts beside the project. Skip recovery copies."""

    dest = _library_dest(session)
    catalog = getattr(session, "catalog", None)
    if dest is None or catalog is None:
        return
    seen: set[str] = set()
    for comp in (
        component
        for sheet in session.project.sheets
        for component in sheet.components
    ):
        embed = (comp.properties or {}).get(RESOLVED_KEY)
        if not isinstance(embed, dict):
            continue
        raw = embed.get("device")
        if not isinstance(raw, dict) or not str(raw.get("id") or "").strip():
            continue
        lib = str(embed.get("library") or "project")
        device = _device_from_raw(lib, raw)
        if device.library_id in seen:
            continue
        seen.add(device.library_id)
        catalog.save_device(device, dest)


def _apply_resolution(
    session: Any,
    comp: Any,
    cited: list[dict[str, str]],
    *,
    require_url: bool,
) -> dict[str, Any]:
    by_field = {item["field"]: item for item in cited}
    if any(name not in by_field for name in REQUIRED_FIELDS):
        _strip_unapplied(comp)
        return {"ok": False, "applied": False, "reason": "incomplete"}
    if require_url and any(not _http_url(item.get("source", "")) for item in cited):
        _strip_unapplied(comp)
        return {"ok": False, "applied": False, "reason": "no source"}
    if not require_url and any(not str(item.get("source") or "").strip() for item in cited):
        _strip_unapplied(comp)
        return {"ok": False, "applied": False, "reason": "no source"}
    mount = by_field["mount"]["value"]
    panel_mount = "din_rail" if mount == "din_rail" else "backplate"
    try:
        width, height, depth, kind = require_explicit_envelope(
            by_field["width_mm"]["value"],
            by_field["height_mm"]["value"],
            by_field["depth_mm"]["value"],
            panel_mount,
        )
    except ValueError as exc:
        _strip_unapplied(comp)
        return {"ok": False, "applied": False, "reason": str(exc)}
    category = by_field["category"]["value"]
    saved_id = comp.library_id
    saved = copy.deepcopy(dict(comp.properties))
    try:
        device = _build_device(session, comp, cited, width, height, depth, kind, category)
        dest = _library_dest(session)
        if dest is not None:
            session.catalog.save_device(device, dest)
        else:
            session.catalog.devices[device.library_id] = device
        comp.library_id = device.library_id
        _clear_placeholder(comp)
        note = str(saved.get(LOOKUP_NOTE_KEY) or "").strip()
        vendor_notes = str(saved.get("notes") or "").strip()
        if vendor_notes:
            comp.properties["notes"] = vendor_notes
        if note:
            comp.properties[LOOKUP_NOTE_KEY] = note
        comp.properties[DATASHEET_KEY] = [dict(item) for item in cited]
        comp.properties[RESOLVED_KEY] = {"library": "project", "device": device_to_raw(device)}
        apply_confirmed_envelope(
            comp,
            width_mm=width,
            height_mm=height,
            depth_mm=depth,
            mount=kind,
            confirmed_by="datasheet review",
        )
    except (KeyError, ValueError, OSError) as exc:
        comp.library_id = saved_id
        comp.properties.clear()
        comp.properties.update(saved)
        _strip_unapplied(comp)
        return {"ok": False, "applied": False, "reason": str(exc)}
    return {
        "ok": True,
        "applied": True,
        "ref": comp.id,
        "library_id": comp.library_id,
        "datasheet": list(comp.properties.get(DATASHEET_KEY) or []),
    }


def _build_device(
    session: Any,
    comp: Any,
    cited: list[dict[str, str]],
    width: float,
    height: float,
    depth: float,
    panel_mount: str,
    category: str,
) -> Device:
    props = comp.properties or {}
    manufacturer = str(props.get("manufacturer") or "").strip()
    part_number = str(props.get("part_number") or props.get("catalog") or comp.id).strip()
    description = str(props.get("description") or part_number).strip()
    lib, device_id = _device_identity(session, manufacturer, part_number, width, height, depth, panel_mount)
    device = Device(
        library_id=f"{lib}.{device_id}",
        id=device_id,
        name=" ".join(bit for bit in (manufacturer, part_number) if bit) or part_number,
        category=category,
        ref_prefix="A",
        tags=["datasheet", "project"],
        schematic_width=28.0,
        schematic_height=18.0,
        primitives=[Primitive(type="rect", x=1, y=1, w=26, h=16)],
        panel=PanelFootprint(
            width_mm=width,
            height_mm=height,
            depth_mm=depth,
            din_rail=panel_mount == "din_rail",
            in_panel=True,
            mount=panel_mount,  # type: ignore[arg-type]
            dim_source="measured",
            depth_from_generic=False,
        ),
        properties={
            "manufacturer": manufacturer,
            "part_number": part_number,
            "description": description,
            DATASHEET_KEY: [dict(item) for item in cited],
        },
        standard="OEM",
    )
    return device


def _device_identity(
    session: Any,
    manufacturer: str,
    part_number: str,
    width: float,
    height: float,
    depth: float,
    panel_mount: str,
) -> tuple[str, str]:
    base = "_".join(bit for bit in (_token(manufacturer), _token(part_number)) if bit) or "part"
    candidate = base
    number = 2
    while f"project.{candidate}" in session.catalog.devices:
        existing = session.catalog.devices[f"project.{candidate}"]
        props = existing.properties or {}
        same_part = (
            str(props.get("part_number") or "") == part_number
            and str(props.get("manufacturer") or "") == manufacturer
        )
        panel = existing.panel
        same_size = (
            abs(float(panel.width_mm) - width) < 1e-6
            and abs(float(panel.height_mm) - height) < 1e-6
            and abs(float(panel.depth_mm) - depth) < 1e-6
            and str(panel.mount) == panel_mount
        )
        if same_part and same_size:
            return "project", candidate
        candidate = f"{base}_{number}"
        number += 1
        if number > 50:
            break
    return "project", candidate


def _catalog_row(parts: Any, row: dict[str, Any]) -> Any | None:
    if parts is None:
        return None
    number = str(row.get("part_number") or row.get("catalog") or "").strip()
    if not number:
        return None
    maker = str(row.get("manufacturer") or "").strip()
    try:
        part = parts.get(number, maker) if maker else parts.get(number)
    except KeyError:
        return None
    if maker:
        from panelcad.library.parts import normalize_manufacturer

        if (
            normalize_manufacturer(maker).casefold()
            != normalize_manufacturer(str(part.manufacturer or "")).casefold()
        ):
            return None
    return part


def _catalog_gap(parts: Any, props: dict[str, Any]) -> str:
    part = _catalog_row(parts, props)
    if part is None:
        return "no catalog match and no dimensions"
    if not part.has_footprint():
        return "catalog part has no published dimensions"
    return f"catalog part {part.catalog} has published dimensions and can be matched"


def _clear_placeholder(comp: Any) -> None:
    for key in _PLACEHOLDER_KEYS:
        comp.properties.pop(key, None)


def _strip_unapplied(comp: Any) -> None:
    if not is_needs_info_component(comp):
        return
    for key in _UNAPPLIED_KEYS:
        comp.properties.pop(key, None)


def _client_or_reason(client: Any) -> Any:
    if client is not None:
        return client
    status = lookup_status()
    if not status.get("available"):
        return {
            "ok": False,
            "applied": False,
            "reason": status["reason"],
            "offer": "manual",
        }
    live = default_lookup_client()
    if live is None:
        from panelcad.ai.config import CONNECT_REASON

        return {
            "ok": False,
            "applied": False,
            "reason": CONNECT_REASON,
            "offer": "manual",
        }
    return live


def _off(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason, "offer": "manual"}


def _confirmation_fields(confirmations: Any) -> set[str]:
    if not confirmations:
        return set()
    if isinstance(confirmations, dict):
        return {str(key) for key, value in confirmations.items() if value}
    return {str(key) for key in confirmations}


def _library_dest(session: Any) -> Path | None:
    path = getattr(session, "path", None)
    if path is None:
        return None
    from panelcad.diag.paths import is_under_recovery

    if is_under_recovery(path):
        return None
    return Path(path).parent / "library" / "needs_info.yaml"


def _as_dict(reply: Any) -> dict[str, Any]:
    if isinstance(reply, dict):
        return reply
    if isinstance(reply, str):
        return _json_object(reply)
    return {}


def _json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(raw[start : end + 1])
        except ValueError:
            return {}
        if isinstance(data, dict):
            return data
    return {}


def _entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    values = data.get("values")
    if isinstance(values, list):
        return [item for item in values if isinstance(item, dict)]
    fields = data.get("fields")
    rows: list[dict[str, Any]] = []
    if isinstance(fields, dict):
        for key, item in fields.items():
            if isinstance(item, dict):
                row = dict(item)
                row.setdefault("field", key)
                rows.append(row)
            elif item not in (None, ""):
                rows.append({"field": key, "value": item})
    return rows


def _page_text(item: dict[str, Any]) -> str:
    bits: list[str] = []
    for key in ("page", "table", "page_or_table"):
        text = str(item.get(key) or "").strip()
        if text and text not in bits:
            bits.append(text)
    return ", ".join(bits)[:200]


def _http_url(value: str) -> str:
    text = str(value or "").strip()
    lower = text.lower()
    if not (lower.startswith("https://") or lower.startswith("http://")):
        return ""
    if len(text) > 500 or any(ch.isspace() for ch in text):
        return ""
    return text


def _without_mm_suffix(text: str) -> tuple[str, bool]:
    """Drop a trailing millimetre unit. Longer spellings are checked first."""

    for suffix in _MM_SUFFIXES:
        if len(text) > len(suffix) and text.endswith(suffix):
            return text[: -len(suffix)], True
    return text, False


def _proposed_dimension(value: Any) -> tuple[str, str] | None:
    """AI size as ``(number, units)``.

    ``units`` is ``mm`` when the proposal says so, and ``unspecified`` when the
    number has no unit. Inches and other units are not converted.
    """

    number = _parse_mm(value)
    if number is None:
        return None
    compact = str(value or "").strip().lower().replace(" ", "")
    stated = _without_mm_suffix(compact)[1]
    return number, "mm" if stated else UNITS_UNSPECIFIED


def _parse_mm(value: Any) -> str | None:
    text = str(value or "").strip().lower().replace(" ", "")
    text, _stated = _without_mm_suffix(text)
    text = text.replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", text):
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number < 1:
        return None
    try:
        from panelcad.library.panel import max_confirmed_envelope_mm

        if number > float(max_confirmed_envelope_mm()):
            return None
    except Exception:  # noqa: BLE001
        return None
    return text


def _parse_mount(value: Any) -> str:
    token = re.sub(r"[^a-z]", "", str(value or "").lower())
    if token in {"din", "dinrail", "ts35"}:
        return "din_rail"
    if token in {"panel", "backplate", "panelmount", "panelmounted"}:
        return "panel"
    return ""


def _token(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").casefold()).strip("_")[:48]
