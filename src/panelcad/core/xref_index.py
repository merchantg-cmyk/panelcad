"""Machine-readable cross-reference index for floor troubleshooting.

Answers three questions without opening the Qt GUI:

* Where are all elements of device X? (page, zone, element, terminals, wires)
* What device/element is at page-zone Y?
* Trace wire N (from/to, pages).

Rows are derived. Opening a file never writes them.
"""

from __future__ import annotations

import re
from typing import Any

from panelcad.core.electrical import SCHEMATIC_KINDS, element_kind, family_key, io_rows
from panelcad.core.from_to import from_to_rows
from panelcad.core.models import Component, Device, Project, Sheet
from panelcad.core.xref import (
    XrefFamily,
    XrefMember,
    component_location,
    contact_terminals,
    is_offpage_component,
    location_of,
    xref_families,
    xref_style,
)
from panelcad.core.zones import sheet_number, zone_at, zone_grid
from panelcad.library.catalog import Catalog


class QueryError(ValueError):
    """Bad technician query. Same sentence on Session, agent, CLI, and drawing tools.

    Not a crash — do not write a crash JSON or append crash_id.
    """


_ZONE_PAGE = re.compile(r"^(\d+)\s*[-:/]\s*([A-Za-z])(\d+)$")
_SHEET_ZONE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)[:/]([A-Za-z])(\d+)$")
_IEC_LOC = re.compile(r"^/(\d+)\.(\d+)$")
_ZONE_ONLY = re.compile(r"^([A-Za-z])(\d+)$")
_LINE_ONLY = re.compile(r"^(\d{3,})$")

_POWER_PINS = frozenset(
    {"L1", "L2", "L3", "T1", "T2", "T3", "T4", "T5", "T6", "U", "V", "W", "U1", "V1", "W1", "1", "3", "5", "2", "4", "6"}
)
_COIL_PINS = frozenset({"A1", "A2"})
_HEATER_PINS = frozenset({"95", "96", "97", "98"})
_DRIVE_IO_PINS = frozenset(
    {
        "DI1",
        "DI2",
        "DI3",
        "DI4",
        "ST",
        "STF",
        "STR",
        "S1",
        "S2",
        "FW",
        "RV",
        "COM",
        "DCM",
        "AI1",
        "AO1",
        "START",
        "STOP",
    }
)
_DRIVE_RELAY_PINS = frozenset(
    {"MA", "MB", "MC", "RA", "RB", "RC", "FAULT", "RUN", "13", "14", "95", "96", "97", "98"}
)
_SAFETY_IN_PINS = frozenset({"S11", "S12", "S21", "S22", "T11", "T12"})
_SAFETY_OUT_PINS = frozenset({"13", "14", "23", "24", "33", "34", "37", "38"})
_TIMER_PINS = frozenset({"15", "16", "17", "18"})

DEVICE_INDEX_SCHEMA: dict[str, object] = {
    "op": "device_index",
    "description": (
        "Where every element of a multi-page device lives. "
        "page, zone, element type, terminals, and wire numbers. "
        "Omit ref to list every family. Derived; does not write YAML."
    ),
    "args": {"ref": "Device tag (M1, CB1, VFD1). Omit for the full index."},
}

LOOKUP_ZONE_SCHEMA: dict[str, object] = {
    "op": "lookup_zone",
    "description": (
        "What device or element sits at a printed page-zone. "
        "Accepts 5-A2, A2, /1.3, a NEMA line (105), or sheet:A2."
    ),
    "args": {"zone": "Page-zone or line.", "sheet": "Optional sheet id when the zone has no page."},
}

TRACE_WIRE_SCHEMA: dict[str, object] = {
    "op": "trace_wire",
    "description": (
        "Follow wire number N across sheets: from/to tags, pins, pages, and zones. "
        "Uses the from-to report plus every schematic segment with that number."
    ),
    "args": {"number": "Wire number as drawn (101, L1, 24V)."},
}


def _device(catalog: Catalog, comp: Component) -> Device | None:
    return catalog.devices.get(comp.library_id)


def _wires_on(sheet: Sheet, ref: str, pins: set[str] | None = None) -> list[str]:
    found: list[str] = []
    token = ref.upper()
    prefix = token + "."
    want = {item.upper() for item in pins} if pins else None
    for wire in sheet.wires:
        number = str(wire.number or "").strip()
        if not number:
            continue
        for end in (wire.frm, wire.to):
            raw = str(end).upper()
            if raw == token or raw.startswith(prefix):
                pin = raw.split(".", 1)[1] if "." in raw else ""
                if want is not None and pin not in want:
                    continue
                if number not in found:
                    found.append(number)
    return found


def _pin_ids(device: Device | None) -> set[str]:
    if device is None:
        return set()
    return {str(pin.id).upper() for pin in device.pins}


def _pin_terminals(device: Device | None, pins: set[str]) -> str:
    if device is None:
        return ""
    have = [str(pin.id) for pin in device.pins if str(pin.id).upper() in pins]
    return "-".join(have)


def _row(
    *,
    device: str,
    ref: str,
    element: str,
    role: str,
    sheet: Sheet,
    loc,
    style: str,
    terminals: str = "",
    wires: str = "",
    state: str = "",
    contact_no: str = "",
    library_id: str = "",
    x: float = 0.0,
    y: float = 0.0,
) -> dict[str, str]:
    return {
        "device": device,
        "ref": ref,
        "element": element,
        "role": role,
        "state": state,
        "contact_no": contact_no,
        "sheet": sheet.id,
        "sheet_no": str(loc.sheet_no),
        "page": str(loc.sheet_no),
        "zone": loc.zone(),
        "page_zone": f"{loc.sheet_no}-{loc.zone()}",
        "location": loc.display(style),
        "location_iec": loc.iec(),
        "line": str(loc.line),
        "terminals": terminals,
        "wires": wires,
        "library_id": library_id,
        "x": f"{x:g}",
        "y": f"{y:g}",
    }


def _member_row(member: XrefMember, style: str) -> dict[str, str]:
    terminals = ""
    if member.role == "contact":
        terminals = contact_terminals(member.comp, member.device)
    elif member.element in {"coil", "contactor", "relay", "timer", "safety"}:
        pins = _pin_ids(member.device)
        terminals = _pin_terminals(member.device, pins & _COIL_PINS) or contact_terminals(
            member.comp, member.device
        )
    else:
        terminals = contact_terminals(member.comp, member.device)
    wires = ",".join(_wires_on(member.sheet, member.comp.id))
    return _row(
        device=member.family,
        ref=member.comp.id,
        element=member.element or member.role,
        role=member.role,
        sheet=member.sheet,
        loc=member.location,
        style=style,
        terminals=terminals,
        wires=wires,
        state=member.state,
        contact_no=member.contact_no,
        library_id=member.comp.library_id,
        x=member.comp.x,
        y=member.comp.y,
    )


def _pin_group_rows(family: XrefFamily, project: Project) -> list[dict[str, str]]:
    """Extra index rows for pin groups that live on the parent symbol."""

    covered = {member.element for member in family.members}
    rows: list[dict[str, str]] = []
    for parent in family.parents:
        pins = _pin_ids(parent.device)
        style = xref_style(project, parent.sheet)
        groups: list[tuple[str, set[str]]] = []
        if pins & _COIL_PINS and parent.element not in {"coil"} and "coil" not in covered:
            groups.append(("coil", pins & _COIL_PINS))
        if pins & _POWER_PINS and "main_poles" not in covered and parent.element not in {"breaker", "disconnect"}:
            groups.append(("main_poles", pins & _POWER_PINS))
        if parent.element in {"breaker", "disconnect"} and pins & _POWER_PINS:
            groups.append(("main_poles", pins & _POWER_PINS))
        if parent.element == "heater" and pins & _HEATER_PINS and "ol_contact" not in covered:
            groups.append(("ol_contact", pins & _HEATER_PINS))
        if parent.element == "drive":
            if pins & _POWER_PINS:
                groups.append(("drive_power", pins & _POWER_PINS))
            if pins & _DRIVE_IO_PINS:
                groups.append(("drive_control", pins & _DRIVE_IO_PINS))
            if pins & _DRIVE_RELAY_PINS:
                groups.append(("drive_relay", pins & _DRIVE_RELAY_PINS))
        if parent.element == "safety":
            if pins & _SAFETY_IN_PINS:
                groups.append(("safety_input", pins & _SAFETY_IN_PINS))
            if pins & _SAFETY_OUT_PINS:
                groups.append(("safety_output", pins & _SAFETY_OUT_PINS))
        if parent.element == "timer" and pins & _TIMER_PINS:
            groups.append(("timed_contact", pins & _TIMER_PINS))
        seen: set[str] = set()
        for element, group in groups:
            if element in seen:
                continue
            seen.add(element)
            wires = ",".join(_wires_on(parent.sheet, parent.comp.id, group))
            rows.append(
                _row(
                    device=family.key,
                    ref=parent.comp.id,
                    element=element,
                    role=parent.role,
                    sheet=parent.sheet,
                    loc=parent.location,
                    style=style,
                    terminals=_pin_terminals(parent.device, group),
                    wires=wires,
                    library_id=parent.comp.library_id,
                    x=parent.comp.x,
                    y=parent.comp.y,
                )
            )
    return rows


def _plc_point_rows(project: Project, catalog: Catalog, ref: str | None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in io_rows(project, catalog):
        module = str(item.get("module") or "")
        if ref and module.casefold() != ref.casefold() and str(item.get("device") or "").casefold() != ref.casefold():
            continue
        sheet_id = str(item.get("sheet") or "")
        try:
            sheet = project.sheet(sheet_id)
            comp = sheet.component(module)
        except KeyError:
            continue
        device = _device(catalog, comp)
        address = str(item.get("address") or item.get("point") or "")
        loc = component_location(project, sheet, comp, device)
        pin = next((item_pin for item_pin in (device.pins if device else []) if str(item_pin.id) == address), None)
        if pin is not None:
            from panelcad.render.sheet import pin_world

            px, py = pin_world(comp, pin)
            loc = location_of(project, sheet, px, py)
        style = xref_style(project, sheet)
        field = str(item.get("device") or "")
        rows.append(
            _row(
                device=module,
                ref=f"{module}.{address}",
                element="plc_io",
                role="plc",
                sheet=sheet,
                loc=loc,
                style=style,
                terminals=address,
                wires=str(item.get("wire") or ""),
                library_id=str(item.get("library_id") or comp.library_id),
                x=loc.x,
                y=loc.y,
            )
        )
        rows[-1]["address"] = address
        if field:
            rows[-1]["field_device"] = field
        see = str(comp.properties.get("xref_rack") or comp.properties.get("xref_io") or "").strip()
        if see:
            rows[-1]["see"] = see
    return rows


def _offpage_rows(project: Project, catalog: Catalog) -> list[dict[str, str]]:
    from panelcad.core.xref import offpage_callout

    rows: list[dict[str, str]] = []
    for sheet in project.sheets:
        if sheet.kind not in SCHEMATIC_KINDS:
            continue
        style = xref_style(project, sheet)
        for comp in sheet.components:
            device = _device(catalog, comp)
            if not is_offpage_component(comp, device):
                continue
            loc = component_location(project, sheet, comp, device)
            wires = ",".join(_wires_on(sheet, comp.id))
            callout = offpage_callout(project, catalog, sheet, comp)
            row = _row(
                device=comp.id,
                ref=comp.id,
                element="offpage",
                role="offpage",
                sheet=sheet,
                loc=loc,
                style=style,
                terminals=callout,
                wires=wires,
                library_id=comp.library_id,
                x=comp.x,
                y=comp.y,
            )
            row["dest_sheet"] = str(comp.properties.get("dest_sheet") or "")
            row["dest_ref"] = str(comp.properties.get("dest_ref") or "")
            rows.append(row)
    return rows


def _standalone_rows(project: Project, catalog: Catalog, ref: str | None) -> list[dict[str, str]]:
    """Lamps and other tagged parts that are not a coil/contact family."""

    want = (ref or "").strip()
    rows: list[dict[str, str]] = []
    for sheet in project.sheets:
        if sheet.kind not in SCHEMATIC_KINDS:
            continue
        style = xref_style(project, sheet)
        for comp in sheet.components:
            if want and comp.id.casefold() != want.casefold():
                continue
            device = _device(catalog, comp)
            if device is None or is_offpage_component(comp, device):
                continue
            if family_key(comp, device) is not None:
                continue
            kind = element_kind(comp, device)
            if not want and kind not in {"lamp"}:
                continue
            loc = component_location(project, sheet, comp, device)
            pins = [str(pin.id) for pin in device.pins]
            rows.append(
                _row(
                    device=comp.id,
                    ref=comp.id,
                    element=kind or "other",
                    role="device",
                    sheet=sheet,
                    loc=loc,
                    style=style,
                    terminals="-".join(pins[:2]) if len(pins) >= 2 else (pins[0] if pins else ""),
                    wires=",".join(_wires_on(sheet, comp.id)),
                    library_id=comp.library_id,
                    x=comp.x,
                    y=comp.y,
                )
            )
    return rows


def device_index_rows(project: Project, catalog: Catalog, ref: str | None = None) -> list[dict[str, str]]:
    """Flat index: one row per device element (and pin group / PLC point / off-page)."""

    want = (ref or "").strip()
    rows: list[dict[str, str]] = []
    for family in xref_families(project, catalog).values():
        if want and family.key.casefold() != want.casefold():
            continue
        for member in family.members:
            style = xref_style(project, member.sheet)
            rows.append(_member_row(member, style))
        rows.extend(_pin_group_rows(family, project))
    rows.extend(_standalone_rows(project, catalog, want or None))
    rows.extend(_plc_point_rows(project, catalog, want or None))
    if not want:
        rows.extend(_offpage_rows(project, catalog))
    else:
        rows.extend(
            row
            for row in _offpage_rows(project, catalog)
            if row["device"].casefold() == want.casefold()
        )
    rows.sort(key=lambda row: (row["device"], int(row["sheet_no"] or 0), row["line"], row["ref"], row["element"]))
    return rows


def device_index(project: Project, catalog: Catalog, ref: str | None = None) -> dict[str, Any]:
    """Where every element of device *ref* lives. Omit *ref* for the full index."""

    rows = device_index_rows(project, catalog, ref)
    if ref:
        return {"device": ref, "elements": rows, "count": len(rows)}
    devices: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        devices.setdefault(row["device"], []).append(row)
    return {
        "devices": [{"device": key, "elements": items, "count": len(items)} for key, items in devices.items()],
        "rows": rows,
        "count": len(rows),
    }


def _sheet_by_number(project: Project, number: int) -> Sheet | None:
    for sheet in project.sheets:
        if sheet_number(project.sheets, sheet.id) == number:
            return sheet
    return None


def parse_zone_query(raw: str) -> dict[str, str]:
    """Split a technician query into sheet / zone / line / column tokens."""

    text = str(raw or "").strip()
    parsed = {"query": text, "sheet": "", "sheet_no": "", "row": "", "col": "", "zone": "", "line": ""}
    if not text:
        return parsed
    match = _ZONE_PAGE.match(text)
    if match:
        parsed["sheet_no"] = match.group(1)
        parsed["row"] = match.group(2).upper()
        parsed["col"] = match.group(3)
        parsed["zone"] = f"{parsed['row']}{parsed['col']}"
        return parsed
    match = _SHEET_ZONE.match(text)
    if match:
        parsed["sheet"] = match.group(1)
        parsed["row"] = match.group(2).upper()
        parsed["col"] = match.group(3)
        parsed["zone"] = f"{parsed['row']}{parsed['col']}"
        return parsed
    match = _IEC_LOC.match(text)
    if match:
        parsed["sheet_no"] = match.group(1)
        parsed["col"] = match.group(2)
        return parsed
    match = _ZONE_ONLY.match(text)
    if match:
        parsed["row"] = match.group(1).upper()
        parsed["col"] = match.group(2)
        parsed["zone"] = f"{parsed['row']}{parsed['col']}"
        return parsed
    match = _LINE_ONLY.match(text)
    if match:
        parsed["line"] = match.group(1)
        return parsed
    return parsed


def _zone_on_grid(sheet: Sheet, row: str, col: str) -> bool:
    grid = zone_grid(sheet)
    if row:
        index = ord(row.upper()) - ord("A") + 1
        if index < 1 or index > grid.rows:
            return False
    if col:
        try:
            number = int(col)
        except ValueError:
            return False
        if number < 1 or number > grid.cols:
            return False
    return True


def _validate_zone_query(project: Project, zone: str, parsed: dict[str, str]) -> None:
    text = str(zone or "").strip()
    if not text:
        raise QueryError("zone is required")
    if not any((parsed["sheet"], parsed["sheet_no"], parsed["zone"], parsed["col"], parsed["line"])):
        raise QueryError(f"cannot parse zone {text!r}")
    pages: list[Sheet] = []
    if parsed["sheet"]:
        try:
            pages = [project.sheet(parsed["sheet"])]
        except KeyError as exc:
            raise QueryError(f"unknown sheet {parsed['sheet']!r}") from exc
    elif parsed["sheet_no"]:
        try:
            number = int(parsed["sheet_no"])
        except ValueError as exc:
            raise QueryError(f"unknown sheet number {parsed['sheet_no']}") from exc
        page = _sheet_by_number(project, number)
        if page is None:
            raise QueryError(f"unknown sheet number {parsed['sheet_no']}")
        pages = [page]
    else:
        pages = [item for item in project.sheets if item.kind in SCHEMATIC_KINDS] or list(project.sheets)
    if (parsed["row"] or parsed["col"]) and pages:
        if not any(_zone_on_grid(page, parsed["row"], parsed["col"]) for page in pages):
            raise QueryError(f"zone {text!r} is off the sheet grid")


def lookup_zone(
    project: Project,
    catalog: Catalog,
    zone: str,
    *,
    sheet: str | None = None,
) -> dict[str, Any]:
    """What device/element is at page-zone *zone*."""

    parsed = parse_zone_query(zone)
    if sheet:
        parsed["sheet"] = sheet
    _validate_zone_query(project, zone, parsed)
    matches: list[dict[str, str]] = []
    for row in device_index_rows(project, catalog):
        if parsed["sheet"] and row["sheet"] != parsed["sheet"]:
            continue
        if parsed["sheet_no"] and row["sheet_no"] != parsed["sheet_no"]:
            continue
        if parsed["zone"] and row["zone"].upper() != parsed["zone"].upper():
            continue
        if parsed["col"] and not parsed["zone"]:
            # IEC /sheet.column — match the printed column.
            loc = row.get("location_iec") or ""
            if not loc.endswith(f".{parsed['col']}"):
                continue
        if parsed["line"] and row["line"] != parsed["line"]:
            continue
        if not any((parsed["sheet"], parsed["sheet_no"], parsed["zone"], parsed["col"], parsed["line"])):
            continue
        matches.append(row)
    return {"query": zone, "parsed": parsed, "matches": matches, "count": len(matches)}


def trace_wire(project: Project, catalog: Catalog, number: str) -> dict[str, Any]:
    """From/to, pages, and devices for wire *number*."""

    want = str(number or "").strip()
    if not want:
        raise QueryError("wire number is required")
    conductors = [row for row in from_to_rows(project, catalog, sort="number") if str(row.get("wire_number") or "") == want]
    plc_io_pins = _plc_io_pin_keys(project, catalog)
    segments: list[dict[str, str]] = []
    pages: list[str] = []
    devices: list[str] = []
    for sheet in project.sheets:
        if sheet.kind not in SCHEMATIC_KINDS:
            continue
        style = xref_style(project, sheet)
        for wire in sheet.wires:
            if str(wire.number or "").strip() != want:
                continue
            frm_ref, _, frm_pin = str(wire.frm).partition(".")
            to_ref, _, to_pin = str(wire.to).partition(".")
            frm_loc = _end_location(
                project, catalog, sheet, frm_ref, style, frm_pin, plc_io_pins=plc_io_pins
            )
            to_loc = _end_location(
                project, catalog, sheet, to_ref, style, to_pin, plc_io_pins=plc_io_pins
            )
            segments.append(
                {
                    "wire": wire.id,
                    "number": want,
                    "from": wire.frm,
                    "to": wire.to,
                    "from_device": frm_ref,
                    "to_device": to_ref,
                    "from_pin": frm_pin,
                    "to_pin": to_pin,
                    "sheet": sheet.id,
                    "sheet_no": str(sheet_number(project.sheets, sheet.id) or ""),
                    "from_zone": frm_loc.get("zone", ""),
                    "to_zone": to_loc.get("zone", ""),
                    "from_location": frm_loc.get("location", ""),
                    "to_location": to_loc.get("location", ""),
                    "from_page_zone": frm_loc.get("page_zone", ""),
                    "to_page_zone": to_loc.get("page_zone", ""),
                }
            )
            if sheet.id not in pages:
                pages.append(sheet.id)
            for tag in (frm_ref, to_ref):
                if tag and tag not in devices:
                    devices.append(tag)
    if not conductors and not segments:
        raise QueryError(f"unknown wire number {want!r}")
    return {
        "number": want,
        "from_to": conductors,
        "segments": segments,
        "pages": pages,
        "devices": devices,
        "count": len(segments),
    }


def _plc_io_pin_keys(project: Project, catalog: Catalog) -> frozenset[tuple[str, str]]:
    """(module, exact pin id) pairs whose device-index row is ``plc_io``.

    Pin ids stay exact (``Q0_1`` does not match ``Q0_10``). Module tags
    follow the same case-insensitive match ``_plc_point_rows`` already uses.
    """

    return frozenset(
        (str(row.get("device") or "").casefold(), str(row.get("address") or ""))
        for row in _plc_point_rows(project, catalog, None)
        if row.get("device") and row.get("address")
    )


def _end_location(
    project: Project,
    catalog: Catalog,
    sheet: Sheet,
    ref: str,
    style: str,
    pin: str = "",
    plc_io_pins: frozenset[tuple[str, str]] | None = None,
) -> dict[str, str]:
    if not ref:
        return {}
    try:
        comp = sheet.component(ref)
    except KeyError:
        for page in project.sheets:
            try:
                comp = page.component(ref)
                sheet = page
                break
            except KeyError:
                continue
        else:
            return {}
    device = _device(catalog, comp)
    loc = component_location(project, sheet, comp, device)
    token = str(pin or "").strip()
    # Index rows store the symbol cell except plc_io, which is pin-located.
    # Prefer the pin cell only for that plc_io row (exact pin id).
    keys = plc_io_pins if plc_io_pins is not None else _plc_io_pin_keys(project, catalog)
    if token and device is not None and (ref.casefold(), token) in keys:
        found = next((item for item in device.pins if str(item.id) == token), None)
        if found is not None:
            from panelcad.render.sheet import pin_world

            px, py = pin_world(comp, found)
            loc = location_of(project, sheet, px, py)
    return {
        "zone": loc.zone(),
        "location": loc.display(style),
        "page_zone": f"{loc.sheet_no}-{loc.zone()}",
        "sheet": sheet.id,
    }


def zone_of_point(project: Project, sheet: Sheet, x: float, y: float) -> dict[str, str]:
    loc = location_of(project, sheet, x, y)
    row, col, _index = zone_at(sheet, x, y)
    return {
        "sheet": sheet.id,
        "sheet_no": str(loc.sheet_no),
        "zone": f"{row}{col}",
        "page_zone": f"{loc.sheet_no}-{row}{col}",
        "location": loc.display(xref_style(project, sheet)),
        "line": str(loc.line),
    }
