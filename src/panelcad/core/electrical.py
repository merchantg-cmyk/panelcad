"""Electrical intelligence: wire numbers, tags, cross-reference, terminals, I/O, audit.

These are the drawing tools a panel shop expects from an electrical CAD package.
They read and write the project YAML. Automatic wire numbers live in
``panelcad.core.wire_numbering``; ``number_wires`` here is the full renumber.
Opening a file never assigns numbers.
"""

from __future__ import annotations

import csv
import re
from io import StringIO
from pathlib import Path
from typing import Any

from panelcad.core.models import Component, Device, Project, Sheet, Wire
from panelcad.core.refs import resolve_ref_prefix, sheet_drawing_standard
from panelcad.library.catalog import Catalog
from panelcad.library.panel import effective_footprint, is_child_contact, is_incabinet

SCHEMATIC_KINDS = {"schematic", "io"}
LAYOUT_KINDS = {"panel_layout", "door_layout"}
_SKIP_CATEGORIES = ("connection.notes", "connection.offpage", "connection.node", "connection.ground")
_CHILD_RE = re.compile(r"^([A-Za-z]+\d+)[-.](\d+)$")
_CHILD_NAME_RE = re.compile(r"^([A-Za-z]+\d+)[-.]([A-Za-z][A-Za-z0-9]*)$")
_TERM_RE = re.compile(r"^([A-Za-z]+[A-Za-z0-9]*?)[-.](\d+)$")
# Coil / contact / parent stay the historic family. Breakers, disconnects,
# drives, operators, and PLC modules join so multi-page elements share a tag.
FAMILY_ROLES = frozenset(
    {"coil", "contact", "parent", "breaker", "disconnect", "drive", "operator", "plc"}
)
ANCHOR_ROLES = frozenset({"coil", "parent", "breaker", "disconnect", "drive", "operator", "plc"})


def _device(catalog: Catalog, comp: Component) -> Device | None:
    try:
        return catalog.get(comp.library_id)
    except KeyError:
        return None


_POWER_POLE_PINS = frozenset({"L1", "L2", "L3", "T1", "T2", "T3"})


def is_power_pole_device(device: Device | None) -> bool:
    """A contactor symbol that draws the power poles (L1–L3 / T1–T3).

    The coil-only symbol shares the switching category and A1/A2, but it has
    no power poles. A split device uses both under one tag. An overload also
    has L/T pins; it is not this contactor.
    """

    if device is None or symbol_role(device) not in {"coil", "parent"}:
        return False
    category = (device.category or "").lower()
    library_id = device.library_id.lower()
    if "contactor" not in category and "contactor" not in library_id:
        return False
    pins = {str(pin.id).upper() for pin in device.pins}
    return bool(pins & _POWER_POLE_PINS)


def is_coil_only_device(device: Device | None) -> bool:
    """Coil symbol with no power poles. The ladder coil of a split contactor."""

    return symbol_role(device) == "coil" and not is_power_pole_device(device)


def symbol_role(device: Device | None) -> str:
    """coil, contact, parent (contactor with a coil), terminal, plc, or other."""
    if device is None:
        return "other"
    lid = device.library_id.lower()
    cat = device.category or ""
    if cat == "connection.terminal" or ".terminals." in lid:
        return "terminal"
    if cat.startswith("control.plc") or ".plc." in lid:
        return "plc"
    if cat == "switching.aux" or "aux_no" in lid or "aux_nc" in lid or "ol_contact" in lid:
        return "contact"
    tags = {str(tag).lower() for tag in device.tags}
    if "contact" in tags and ("aux" in tags or "overload" in tags or cat.startswith("protection")):
        return "contact"
    if lid.endswith(".coil"):
        return "coil"
    if "coil" in tags and (cat.startswith("switching") or cat.startswith("timing")):
        return "coil"
    pins = {p.id.upper() for p in device.pins}
    if cat.startswith("protection.overload") and "ol_contact" not in lid:
        return "parent"
    if cat.startswith("timing.") and "A1" in pins and "A2" in pins:
        return "parent"
    if cat.startswith("switching.") and "A1" in pins and "A2" in pins:
        return "parent"
    if cat.startswith("protection.breaker") or cat.startswith("protection.mpcb"):
        return "breaker"
    if cat.startswith("protection.disconnect"):
        return "disconnect"
    if cat.startswith("drive."):
        return "drive"
    if cat.startswith(("operator.pushbutton", "operator.selector", "operator.estop", "operator.limit")):
        return "operator"
    return "other"


def child_family_tag(comp_id: str) -> str | None:
    """Parent tag encoded in a child id (M1-13, CB1-AUX, VFD1-FAULT)."""

    match = _CHILD_RE.match(comp_id) or _CHILD_NAME_RE.match(comp_id)
    return match.group(1) if match else None


def element_kind(comp: Component, device: Device | None) -> str:
    """Fine-grained element type for the device index (coil, heater, trip_alarm, …)."""

    role = symbol_role(device)
    if device is None:
        return role
    lid = device.library_id.lower()
    cat = device.category or ""
    props_role = str(comp.properties.get("role") or "").strip().lower()
    if props_role in {"trip", "trip_alarm", "alarm"}:
        return "trip_alarm"
    if props_role in {"ol_contact", "ol_sense"} or "ol_contact" in lid:
        return "ol_contact"
    if props_role in {"fault", "run"}:
        return props_role
    child = _CHILD_NAME_RE.match(comp.id)
    if child and child.group(2).lower() in {"fault", "run"}:
        return child.group(2).lower()
    if role == "contact":
        return "contact"
    if cat.startswith("protection.overload"):
        return "heater"
    if cat.startswith("timing."):
        return "timer"
    if cat.startswith("switching.safety"):
        return "safety"
    if cat.startswith("switching.contactor"):
        return "contactor"
    if cat.startswith("switching.relay"):
        return "relay"
    if role == "coil":
        return "coil"
    if cat.startswith("operator.light"):
        return "lamp"
    if role in FAMILY_ROLES or role in {"terminal"}:
        return role
    return role or "other"


def contact_state(device: Device | None) -> str:
    if device is None:
        return ""
    lid = device.library_id.lower()
    if "aux_nc" in lid or "ol_contact" in lid:
        return "NC"
    if "aux_no" in lid:
        return "NO"
    raw = str(device.properties.get("contact") or "").upper()
    if raw in {"NO", "NC"}:
        return raw
    return ""


# Library `location` values that mean the symbol is ink, not a part you buy.
# A plant name such as "MCC-1" is not in this set, so a field motor stays.
_DRAWING_ONLY_LOCATIONS = frozenset(
    {
        "schematic-only",
        "layout-only",
        "drawing-only",
        "reconstruct-only",
    }
)


def is_drawing_only_mark(device: Device | None, component: Component | None = None) -> bool:
    """True when the symbol is drawing ink, not a purchasable part.

    Off-page arrows, wire nodes, notes, photos, and the earth symbol match.
    A motor, a terminal, or other field or panel equipment does not. The
    decision uses the symbol category and its location. It does not read or
    invent a width, height, or depth.
    """

    library_id = ""
    if component is not None and component.library_id:
        library_id = component.library_id
    elif device is not None:
        library_id = device.library_id or ""
    if "offpage" in library_id.lower():
        return True
    if device is None:
        return False
    category = (device.category or "").lower()
    if category.startswith(_SKIP_CATEGORIES) or "offpage" in (device.library_id or "").lower():
        return True
    device_location = str((device.properties or {}).get("location") or "").strip().lower()
    if device_location in _DRAWING_ONLY_LOCATIONS:
        return True
    if component is None:
        return False
    instance_location = str((component.properties or {}).get("location") or "").strip().lower()
    return instance_location in _DRAWING_ONLY_LOCATIONS


def _skip_device(device: Device | None) -> bool:
    if device is None:
        return True
    return is_drawing_only_mark(device)


def family_key(comp: Component, device: Device | None) -> str | None:
    """Tag shared by a device and its elements. None for parts that are not a family."""
    role = symbol_role(device)
    if role not in FAMILY_ROLES:
        return None
    parent = str(comp.properties.get("parent") or "").strip()
    if parent:
        return parent
    home = str(comp.properties.get("io_continuation") or "").strip()
    if home:
        return home
    child = child_family_tag(comp.id)
    if child and (role == "contact" or child != comp.id):
        return child
    return comp.id


def _swap_end(end: str, old: str, new: str) -> str:
    text = str(end)
    if text == old:
        return new
    prefix = old + "."
    if text.startswith(prefix):
        return new + text[len(old) :]
    return text


def rename_component(project: Project, sheet: Sheet, old: str, new: str) -> None:
    """Rename one device and the wires on its sheet that land on it."""
    if old == new:
        return
    comp = sheet.component(old)
    if any(other.id == new for other in sheet.components):
        raise ValueError(f"{sheet.id} already has {new}")
    for wire in sheet.wires:
        wire.frm = _swap_end(wire.frm, old, new)
        wire.to = _swap_end(wire.to, old, new)
    for page in project.sheets:
        for other in page.components:
            if str(other.properties.get("dest_sheet") or "") == sheet.id and str(other.properties.get("dest_ref") or "") == old:
                other.properties["dest_ref"] = new
    if str(comp.properties.get("parent") or "") == old:
        comp.properties["parent"] = new
    comp.id = new


def _next_tag(prefix: str, used: set[str]) -> str:
    from panelcad.core.refs import next_tag

    return next_tag(prefix, used)


def _used_ids(project: Project) -> set[str]:
    return {comp.id for sheet in project.sheets for comp in sheet.components}


_TAG_PREFIX_RE = re.compile(r"^([A-Za-z]+)")


def _existing_tag_prefix(comp_id: str, fallback: str) -> str:
    match = _TAG_PREFIX_RE.match(comp_id)
    return match.group(1) if match else fallback


def _device_tag_prefix(
    project: Project,
    catalog: Catalog,
    sheet: Sheet,
    comp: Component,
    device: Device,
    *,
    apply_prefixes: bool,
) -> str:
    fallback = device.ref_prefix or "A"
    if not apply_prefixes:
        return _existing_tag_prefix(comp.id, fallback)
    return resolve_ref_prefix(
        device,
        standard=sheet_drawing_standard(project, sheet),
        maps=getattr(catalog, "ref_prefix_maps", None),
        overrides=getattr(project, "ref_prefixes", None),
    )


def retag(
    project: Project,
    catalog: Catalog,
    *,
    mode: str = "duplicates",
    apply_prefixes: bool = False,
) -> list[dict[str, str]]:
    """Renumber device tags.

    ``duplicates`` (default) renames a later schematic copy of the same tag when
    it is a second coil, breaker, or other device. A contact that shares its
    coil's tag is left alone, and so is the panel footprint of that tag.

    ``family`` renumbers every schematic device in each existing tag prefix,
    top to bottom, and moves a panel or door footprint when that tag was unique.

    ``standard`` (or ``apply_prefixes``) remaps tags to the sheet's drawing
    standard — JIC/NFPA 79 prefixes on a NEMA/ANSI project. Loading a file
    never does this.
    """
    token = (mode or "duplicates").strip().lower()
    apply = bool(apply_prefixes) or token in {"standard", "prefixes", "nema"}
    if token in {"duplicate", "duplicates", "unique"}:
        return _retag_duplicates(project, catalog, apply_prefixes=apply)
    if token in {"family", "all", "project", "standard", "prefixes", "nema"}:
        return _retag_family(project, catalog, apply_prefixes=apply)
    raise ValueError("retag mode must be 'duplicates', 'family', or 'standard'")


def _retag_duplicates(
    project: Project,
    catalog: Catalog,
    *,
    apply_prefixes: bool = False,
) -> list[dict[str, str]]:
    groups: dict[str, list[tuple[Sheet, Component, Device]]] = {}
    for sheet in project.sheets:
        if sheet.kind not in SCHEMATIC_KINDS:
            continue
        for comp in sheet.components:
            device = _device(catalog, comp)
            if _skip_device(device) or device is None:
                continue
            groups.setdefault(comp.id, []).append((sheet, comp, device))
    used = _used_ids(project)
    renamed: list[dict[str, str]] = []
    for _ref, items in groups.items():
        parents = [item for item in items if symbol_role(item[2]) in {"coil", "parent"}]
        contacts = [item for item in items if symbol_role(item[2]) == "contact"]
        plain = [item for item in items if symbol_role(item[2]) not in {"coil", "parent", "contact"}]
        extras = parents[1:] + plain[1:]
        if not parents:
            extras += contacts[1:]
        for sheet, comp, device in extras:
            prefix = _device_tag_prefix(
                project, catalog, sheet, comp, device, apply_prefixes=apply_prefixes
            )
            tag = _next_tag(prefix, used)
            old = comp.id
            rename_component(project, sheet, old, tag)
            renamed.append({"sheet": sheet.id, "from": old, "to": tag})
    return renamed


def _retag_family(
    project: Project,
    catalog: Catalog,
    *,
    apply_prefixes: bool = False,
) -> list[dict[str, str]]:
    buckets: dict[str, list[tuple[int, Sheet, Component, Device]]] = {}
    for index, sheet in enumerate(project.sheets):
        if sheet.kind not in SCHEMATIC_KINDS:
            continue
        for comp in sheet.components:
            device = _device(catalog, comp)
            if _skip_device(device) or device is None:
                continue
            prefix = _device_tag_prefix(
                project, catalog, sheet, comp, device, apply_prefixes=apply_prefixes
            )
            buckets.setdefault(prefix, []).append((index, sheet, comp, device))
    plan: list[tuple[Sheet, str, str]] = []
    taken: set[str] = set()
    for prefix, items in buckets.items():
        items.sort(key=lambda row: (row[0], row[2].y, row[2].x, row[2].id))
        n = 1
        for _index, sheet, comp, _dev in items:
            while f"{prefix}{n}" in taken:
                n += 1
            new = f"{prefix}{n}"
            taken.add(new)
            n += 1
            if comp.id != new:
                plan.append((sheet, comp.id, new))
    # A panel footprint keeps the tag when exactly one schematic device had it.
    schematic_ids = [old for _sheet, old, _new in plan]
    for sheet, old, new in list(plan):
        if schematic_ids.count(old) != 1:
            continue
        for page in project.sheets:
            if page.kind not in LAYOUT_KINDS:
                continue
            if any(comp.id == old for comp in page.components):
                plan.append((page, old, new))
    return _apply_rename_plan(project, plan)


def _apply_rename_plan(project: Project, plan: list[tuple[Sheet, str, str]]) -> list[dict[str, str]]:
    renamed: list[dict[str, str]] = []
    temps: list[tuple[Sheet, str, str, str]] = []
    for i, (sheet, old, new) in enumerate(plan):
        if old == new:
            continue
        tmp = f"__retag{i}"
        rename_component(project, sheet, old, tmp)
        temps.append((sheet, old, tmp, new))
    for sheet, old, tmp, new in temps:
        rename_component(project, sheet, tmp, new)
        renamed.append({"sheet": sheet.id, "from": old, "to": new})
    return renamed


def cross_reference_rows(project: Project, catalog: Catalog) -> list[dict[str, str]]:
    """One row per coil or contact. Derived — does not write the project."""

    from panelcad.core.xref import cross_reference_rows as _rows

    return _rows(project, catalog)


def update_cross_references(project: Project, catalog: Catalog) -> list[dict[str, str]]:
    """Write ``properties.xref`` so a save keeps the last computed references."""

    from panelcad.core.xref import update_cross_references as _update

    return _update(project, catalog)


def number_wires(
    project: Project,
    *,
    start: int = 1,
    overwrite: bool = False,
    scheme: str | None = None,
    catalog: Catalog | None = None,
) -> dict[str, Any]:
    """Full project renumber. Locked and conventional numbers are never overwritten.

    Incremental assignment (as wires are drawn) lives in
    ``panelcad.core.wire_numbering.assign_wire_numbers``.
    """
    from panelcad.core.wire_numbering import number_wires as _number

    return _number(project, catalog, start=start, overwrite=overwrite, scheme=scheme)


def wire_number_conflicts(project: Project, catalog: Any | None = None) -> list[str]:
    """Same wire number on two different electrical networks."""
    from panelcad.core.wire_numbering import wire_networks

    owners: dict[str, set[int]] = {}
    for index, items in enumerate(wire_networks(project, catalog)):
        for _sheet, wire in items:
            number = str(wire.number or "").strip()
            if not number:
                continue
            owners.setdefault(number, set()).add(index)
    errors: list[str] = []
    for number, found in sorted(owners.items(), key=lambda item: item[0]):
        if len(found) > 1:
            errors.append(f"wire number {number} is used on {len(found)} different nets")
    return errors


def terminal_identity(comp: Component, device: Device | None) -> tuple[str, str] | None:
    if symbol_role(device) != "terminal":
        return None
    strip = str(comp.properties.get("strip") or "").strip()
    terminal = str(comp.properties.get("terminal") or "").strip()
    match = _TERM_RE.match(comp.id)
    if not strip and match:
        strip = match.group(1)
    if not terminal and match:
        terminal = match.group(2)
    if not strip:
        strip = comp.id
    if not terminal:
        terminal = "1"
    return strip, terminal


def _lands(sheet: Sheet, ref: str, pin: str) -> list[Wire]:
    token = f"{ref}.{pin}".upper()
    found: list[Wire] = []
    for wire in sheet.wires:
        if str(wire.frm).upper() == token or str(wire.to).upper() == token:
            found.append(wire)
    return found


def _wire_text(wires: list[Wire]) -> str:
    parts: list[str] = []
    for wire in wires:
        text = str(wire.number or wire.net or wire.label or wire.id).strip()
        if text and text not in parts:
            parts.append(text)
    return ",".join(parts)


def _is_spare(comp: Component, internal: list[Wire], external: list[Wire]) -> str:
    flag = str(comp.properties.get("spare") or "").strip().lower()
    if flag in {"1", "true", "yes", "y", "spare"}:
        return "yes"
    if not internal and not external:
        return "yes"
    return ""


def terminal_strip_rows(project: Project, catalog: Catalog) -> list[dict[str, str]]:
    """Tabular terminal strip: level, internal wire, external wire, jumper, spare.

    Pin A (left) is the internal side. Pin B (right) is the field side.
    Order is schematic reading order (sheet, then top to bottom, then left to right).
    Panel footprints of the same tag are not a second level.
    """
    pending: list[tuple[tuple, dict[str, str]]] = []
    for index, sheet in enumerate(project.sheets):
        if sheet.kind in LAYOUT_KINDS:
            continue
        for comp in sheet.components:
            device = _device(catalog, comp)
            ident = terminal_identity(comp, device)
            if ident is None or device is None:
                continue
            strip, terminal = ident
            internal = _lands(sheet, comp.id, "A")
            external = _lands(sheet, comp.id, "B")
            props = {**device.properties, **comp.properties}
            row = {
                "strip": strip,
                "terminal": terminal,
                "ref": comp.id,
                "sheet": sheet.id,
                "spare": _is_spare(comp, internal, external),
                "jumper": str(comp.properties.get("jumper") or ""),
                "internal": _wire_text(internal),
                "external": _wire_text(external),
                "catalog": str(props.get("catalog") or props.get("part_number") or ""),
                "library_id": comp.library_id,
            }
            pending.append(((strip, index, comp.y, comp.x, comp.id), row))
    pending.sort(key=lambda item: item[0])
    return [row for _key, row in pending]


def sequence_terminal_strip(
    project: Project,
    catalog: Catalog,
    strip: str,
    *,
    rename: bool = False,
) -> list[dict[str, str]]:
    """Resequence one strip from schematic reading order (sheet, then top to bottom).

    Schematic position is the order. This writes levels 1..n from that position.
    ``rename`` also retags to ``STRIP-n`` in that same order, including a panel
    footprint that shares the tag. Reordering on the strip drawing moves the
    symbols into the new order and retags, so a later sequence is a no-op.
    """
    name = strip.strip()
    if not name:
        raise ValueError("strip name is required")
    found: list[tuple[int, Sheet, Component]] = []
    for index, sheet in enumerate(project.sheets):
        if sheet.kind in LAYOUT_KINDS:
            continue
        for comp in sheet.components:
            device = _device(catalog, comp)
            ident = terminal_identity(comp, device)
            if ident is None or ident[0] != name:
                continue
            found.append((index, sheet, comp))
    found.sort(key=lambda row: (row[0], row[2].y, row[2].x, row[2].id))
    for level, (_index, _sheet, comp) in enumerate(found, start=1):
        comp.properties["strip"] = name
        comp.properties["terminal"] = str(level)
    if rename:
        plan: list[tuple[Sheet, str, str]] = []
        for level, (_index, sheet, comp) in enumerate(found, start=1):
            new = f"{name}-{level}"
            if comp.id != new:
                plan.append((sheet, comp.id, new))
            for page in project.sheets:
                if page.kind not in LAYOUT_KINDS:
                    continue
                if any(other.id == comp.id for other in page.components):
                    plan.append((page, comp.id, new))
        plan = [(sheet, old, new) for sheet, old, new in plan if old != new]
        _apply_rename_plan(project, plan)
    return [row for row in terminal_strip_rows(project, catalog) if row["strip"] == name]


def jumper_terminals(
    project: Project,
    refs: list[str],
    group: str | None = None,
    catalog: Catalog | None = None,
) -> dict[str, Any]:
    """Mark a consecutive span as jumpered. ``refs`` may be the two ends.

    ``properties.jumper`` stays the stored mark so old files still load. The
    catalog pick (family, pitch, poles) is derived when the BOM or strip
    export asks for it.
    """
    from panelcad.core.jumper import apply_jumper

    return apply_jumper(project, catalog, refs, group=group)


def _expect_layout(device: Device, comp: Component) -> str | None:
    """panel, door, or None when the part stays in the field."""
    if _skip_device(device) or is_child_contact(device, comp):
        return None
    # Field devices, terminals, and fuses drawn on an I/O sheet are schematic
    # dressing. The module itself is the part that belongs on the rack.
    if comp.properties.get("io_owner") and not comp.properties.get("io_module"):
        return None
    if comp.properties.get("io_continuation"):
        return None
    footprint = effective_footprint(device, comp)
    if footprint.mount == "door" or device.category.startswith("operator.") or device.category.startswith("control.hmi"):
        return "door"
    if footprint.mount == "none" or not is_incabinet(device, comp):
        return None
    return "panel"


def audit_schematic_panel(project: Project, catalog: Catalog) -> list[dict[str, str]]:
    """Schematic devices missing a footprint, and footprints missing a schematic tag.

    Same tag on the schematic and on the panel is a match. Notes and off-page
    arrows are ignored. Door operators are expected on a door sheet.
    """
    schematic: dict[str, list[tuple[Sheet, Component, Device]]] = {}
    panel: dict[str, list[str]] = {}
    door: dict[str, list[str]] = {}
    for sheet in project.sheets:
        for comp in sheet.components:
            device = _device(catalog, comp)
            if device is None or _skip_device(device):
                continue
            if sheet.kind in SCHEMATIC_KINDS:
                schematic.setdefault(comp.id, []).append((sheet, comp, device))
            elif sheet.kind == "panel_layout":
                panel.setdefault(comp.id, []).append(sheet.id)
            elif sheet.kind == "door_layout":
                door.setdefault(comp.id, []).append(sheet.id)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for ref, items in schematic.items():
        _sheet, comp, device = items[0]
        expect = _expect_layout(device, comp)
        sheets = ",".join(sheet.id for sheet, _c, _d in items)
        if expect == "panel" and ref not in panel:
            rows.append(
                {
                    "ref": ref,
                    "library_id": comp.library_id,
                    "issue": "missing_panel",
                    "sheets": sheets,
                }
            )
        elif expect == "door" and ref not in door:
            rows.append(
                {
                    "ref": ref,
                    "library_id": comp.library_id,
                    "issue": "missing_door",
                    "sheets": sheets,
                }
            )
        seen.add(ref)
    for ref, sheets in panel.items():
        if ref in seen:
            continue
        device = None
        library_id = ""
        for sheet in project.sheets:
            if sheet.kind != "panel_layout":
                continue
            try:
                comp = sheet.component(ref)
            except KeyError:
                continue
            device = _device(catalog, comp)
            library_id = comp.library_id
            break
        if device is None or _skip_device(device) or device.category in {"connection.accessory", "connection.jumper"}:
            continue
        # A catalog aux block mounts on its contactor. It is not its own schematic symbol.
        if comp.properties.get("addon"):
            continue
        rows.append(
            {
                "ref": ref,
                "library_id": library_id,
                "issue": "missing_schematic",
                "sheets": ",".join(sheets),
            }
        )
    for ref, sheets in door.items():
        if ref in schematic:
            continue
        library_id = ""
        for sheet in project.sheets:
            if sheet.kind != "door_layout":
                continue
            try:
                library_id = sheet.component(ref).library_id
            except KeyError:
                continue
            break
        rows.append(
            {
                "ref": ref,
                "library_id": library_id,
                "issue": "missing_schematic",
                "sheets": ",".join(sheets),
            }
        )
    return rows


def _io_type(pin_id: str, electrical: str) -> str | None:
    pid = pin_id.upper()
    if electrical == "power" or pid in {"LPLUS", "M", "PE", "L1", "L2", "L3", "N", "A1", "A2"}:
        return None
    # Word/analog prefixes before the single-letter discrete prefixes (WX before X, IW before I).
    if pid.startswith(("AI", "IW", "WX")):
        return "AI"
    if pid.startswith(("AO", "QW", "WY")):
        return "AO"
    if pid.startswith(("DI", "IN", "I", "X")):
        return "DI"
    if pid.startswith(("DO", "OUT", "Q", "Y")):
        return "DO"
    if electrical == "input":
        return "DI"
    if electrical == "output":
        return "DO"
    return None


def _pin_end(ref: str, pin_id: str) -> str:
    return f"{ref}.{pin_id}".upper()


_ADDRESS_LABEL = re.compile(r"^[A-Za-z]+\d+(?:[._]\d+)?$")


def _point_label(pin_id: str, labels: dict[str, Any]) -> str:
    """Address as drawn (I0.0). Pin ids stay I0_0 so a spreadsheet can round-trip."""

    label = str(labels.get(pin_id) or "").strip()
    if label and _ADDRESS_LABEL.match(label):
        return label.replace("_", ".")
    if "_" in pin_id and _ADDRESS_LABEL.match(pin_id):
        return pin_id.replace("_", ".")
    return pin_id


def _comp_on(project: Project, sheet: Sheet, ref: str) -> tuple[Sheet, Component] | None:
    try:
        return sheet, sheet.component(ref)
    except KeyError:
        pass
    fallback: tuple[Sheet, Component] | None = None
    for page in project.sheets:
        try:
            comp = page.component(ref)
        except KeyError:
            continue
        if page.kind not in LAYOUT_KINDS:
            return page, comp
        fallback = (page, comp)
    return fallback


def _is_offpage(device: Device | None) -> bool:
    if device is None:
        return False
    cat = device.category or ""
    return cat.startswith("connection.offpage") or "offpage" in device.library_id


def _lands_project(project: Project, ref: str, pin: str) -> list[tuple[Sheet, Wire]]:
    token = f"{ref}.{pin}".upper()
    found: list[tuple[Sheet, Wire]] = []
    for sheet in project.sheets:
        if sheet.kind in LAYOUT_KINDS:
            continue
        for wire in sheet.wires:
            if str(wire.frm).upper() == token or str(wire.to).upper() == token:
                found.append((sheet, wire))
    return found


def _follow_offpage(
    project: Project,
    catalog: Catalog,
    wire: Wire,
    skip: set[str],
) -> tuple[Sheet, Component, Device | None] | None:
    """The field device on the other side of an off-page arrow, matched by wire number."""

    number = str(wire.number or "").strip()
    net = str(wire.net or "").strip()
    if not number and not net:
        return None
    for sheet in project.sheets:
        if sheet.kind in LAYOUT_KINDS:
            continue
        for other in sheet.wires:
            if other is wire:
                continue
            same_number = bool(number) and str(other.number or "").strip() == number
            same_net = bool(net) and not number and str(other.net or "").strip() == net
            if not same_number and not same_net:
                continue
            for end in (other.frm, other.to):
                ref = str(end).split(".", 1)[0]
                if not ref or ref in skip:
                    continue
                found = _comp_on(project, sheet, ref)
                if found is None:
                    continue
                page, comp = found
                device = _device(catalog, comp)
                if _is_offpage(device):
                    continue
                return page, comp, device
    return None


_ROLE_TOKEN = {
    "run_fb": ("RUN_FB", "Run feedback"),
    "run_seal": ("RUN_SEAL", "Run seal"),
    "fwd_seal": ("FWD_SEAL", "Forward seal"),
    "rev_seal": ("REV_SEAL", "Reverse seal"),
    "hoa_auto_fb": ("AUTO", "Auto"),
    "hoa_hand_fb": ("HAND", "Hand"),
    "hoa_off_fb": ("OFF", "Off"),
}


def _tag_piece(text: str) -> str:
    piece = re.sub(r"[^A-Za-z0-9_]", "_", text)
    piece = re.sub(r"_+", "_", piece).strip("_")
    return piece


def _parent_ref(comp: Component, device: Device | None) -> str:
    """Coil or device that owns this contact. Empty when the symbol is the device."""

    parent = str(comp.properties.get("parent") or "").strip()
    if parent and parent != comp.id:
        return parent
    if symbol_role(device) == "contact" or contact_state(device):
        child = child_family_tag(comp.id)
        if child and child != comp.id:
            return child
    return ""


def _hoa_token(comp: Component, role: str, pin_id: str) -> str:
    """AUTO, HAND, or OFF from the contact role or the HOA pin label on the drawing."""

    labels = comp.properties.get("pin_labels") if isinstance(comp.properties.get("pin_labels"), dict) else {}
    label = str(labels.get(pin_id) or "").strip().upper()
    if label in {"A", "AUTO"}:
        return "AUTO"
    if label in {"H", "HAND"}:
        return "HAND"
    if label in {"O", "OFF"}:
        return "OFF"
    low = role.lower()
    if "auto" in low:
        return "AUTO"
    if "hand" in low:
        return "HAND"
    if re.search(r"(^|_)off($|_)", low):
        return "OFF"
    return ""


def _child_function(comp: Component, device: Device | None, pin_id: str) -> tuple[str, str]:
    """Tag token and function label for a contact of a parent device."""

    role = str(comp.properties.get("role") or "").strip().lower()
    state = contact_state(device)
    library = (device.library_id if device is not None else "").lower()
    if role in {"ol_sense", "ol_contact"} or "ol_contact" in library:
        if state == "NO":
            return "TRIP", "Overload trip"
        return "OK", "Overload OK"
    mapped = _ROLE_TOKEN.get(role)
    if mapped is not None:
        return mapped
    hoa = _hoa_token(comp, role, pin_id)
    if hoa:
        label = {"AUTO": "Auto", "HAND": "Hand", "OFF": "Off"}[hoa]
        return hoa, label
    if role and role not in {"contact", "aux"}:
        token = _tag_piece(role).upper()
        if token:
            return token, role.replace("_", " ")
    if state == "NC":
        return "NC", "NC contact"
    if state == "NO":
        return "NO", "NO contact"
    return "", ""


def _connected_identity(
    comp: Component,
    device: Device | None,
    pin_id: str,
) -> tuple[str, str, str]:
    """Device column, function label, and default tag for the landed symbol.

    A contact such as M1-14 keeps its parent (M1) in the device column. The tag
    is the parent plus the contact role or HOA position (M1_RUN_FB, OL1_OK, SEL1_AUTO).
    A pushbutton, coil, or terminal that is the device keeps its own tag.
    """

    explicit = str(comp.properties.get("function") or "").strip()
    parent = _parent_ref(comp, device)
    if not parent:
        function = explicit or (device.name if device is not None else "")
        hoa = _hoa_token(comp, str(comp.properties.get("role") or ""), pin_id)
        if hoa:
            if not explicit:
                function = {"AUTO": "Auto", "HAND": "Hand", "OFF": "Off"}[hoa]
            piece = _tag_piece(comp.id) or comp.id
            return comp.id, function, f"{piece}_{hoa}"
        return comp.id, function, comp.id
    token, label = _child_function(comp, device, pin_id)
    if explicit:
        label = explicit
    if not token and explicit:
        token = _tag_piece(explicit).upper()
    device_id = parent
    piece = _tag_piece(parent) or parent
    tag = f"{piece}_{token}" if token else piece
    function = label or (device.name if device is not None else "")
    return device_id, function, tag


def _module_start_address(comp: Component, device: Device | None) -> str:
    """Process-image byte where this module's analog words begin, if one is set."""

    bags: list[dict[str, Any]] = [comp.properties]
    if device is not None and isinstance(device.properties, dict):
        bags.append(device.properties)
    for bag in bags:
        raw = bag.get("start_address")
        if raw is None or str(raw).strip() == "":
            continue
        return str(raw).strip()
    return ""


def _unique_io_tag(tag: str, used: set[str]) -> str:
    candidate = tag or "Tag"
    n = 1
    while candidate.casefold() in used:
        n += 1
        candidate = f"{tag}_{n}"
    used.add(candidate.casefold())
    return candidate


def _continuation_ids(project: Project) -> dict[str, list[str]]:
    """Continuation-card ids keyed by the home module they draw again."""

    found: dict[str, list[str]] = {}
    for sheet in project.sheets:
        if sheet.kind in LAYOUT_KINDS:
            continue
        for comp in sheet.components:
            home = str(comp.properties.get("io_continuation") or "").strip()
            if home:
                found.setdefault(home, []).append(comp.id)
    return found


def _connected_end(
    project: Project,
    catalog: Catalog,
    plc_ref: str,
    pin_id: str,
) -> tuple[str, str, str, str, str, str]:
    """Device, function, wire, sheet id, NEMA line, and default tag.

    The line is the rung through the symbol, the same Y cross-reference uses,
    not the symbol origin. Returns empty strings when the point is spare.
    ``wire`` is still filled when a wire lands but the other end cannot be named.
    """

    from panelcad.core.xref import component_rung_y
    from panelcad.core.zones import nema_line_at, sheet_number

    landed = _lands_project(project, plc_ref, pin_id)
    token = _pin_end(plc_ref, pin_id)
    sheets = list(project.sheets)
    chosen: tuple[Sheet, Wire, Sheet, Component, Device | None, str] | None = None
    wire_no = ""
    for wire_sheet, wire in landed:
        if not wire_no:
            wire_no = str(wire.number or wire.net or "")
        far = wire.to if str(wire.frm).upper() == token else wire.frm
        ref, _, far_pin = str(far).partition(".")
        if not ref or ref == plc_ref:
            continue
        found = _comp_on(project, wire_sheet, ref)
        if found is None:
            continue
        dev_sheet, comp = found
        device = _device(catalog, comp)
        if _is_offpage(device):
            followed = _follow_offpage(project, catalog, wire, {plc_ref, ref})
            if followed is None:
                continue
            dev_sheet, comp, device = followed
            far_pin = ""
        chosen = (wire_sheet, wire, dev_sheet, comp, device, far_pin)
        wire_no = str(wire.number or wire.net or "") or wire_no
        break
    if chosen is None:
        return "", "", wire_no, "", "", ""
    _wire_sheet, _wire, dev_sheet, comp, device, far_pin = chosen
    device_id, function, tag = _connected_identity(comp, device, far_pin)
    rung_y = component_rung_y(comp, device)
    line = str(nema_line_at(dev_sheet, rung_y, sheet_number(sheets, dev_sheet.id), project=project))
    return device_id, function, wire_no, dev_sheet.id, line, tag


def io_rows(project: Project, catalog: Catalog) -> list[dict[str, str]]:
    """PLC and I/O-module points: address, type, tag, description, device, and spares.

    ``address`` is the pin id (I0_0) so a spreadsheet import still matches the symbol.
    ``point`` is the address as drawn (I0.0). ``description`` is the connected device:
    tag, function, wire number, sheet, and line. A contact reports its parent device
    (M1, not M1-14) and a tag built from that parent plus the contact role.
    Unlanded points are spares. A continuation card is the same module drawn
    again for the next eight points, so its pins stay on the home module.
    """

    from panelcad.core.io_sheets import pull_io_sheet_edits

    pull_io_sheet_edits(project, catalog)
    rows: list[dict[str, str]] = []
    used_tags: set[str] = set()
    continuations = _continuation_ids(project)
    for sheet in project.sheets:
        if sheet.kind in LAYOUT_KINDS:
            continue
        for comp in sheet.components:
            device = _device(catalog, comp)
            if device is None or symbol_role(device) != "plc":
                continue
            # Points 8–15 already have the home module's slot and address.
            if str(comp.properties.get("io_continuation") or "").strip():
                continue
            io_map = comp.properties.get("io") if isinstance(comp.properties.get("io"), dict) else {}
            labels = comp.properties.get("pin_labels") if isinstance(comp.properties.get("pin_labels"), dict) else {}
            slot = str(comp.properties.get("slot") or "").strip()
            for pin in device.pins:
                kind = _io_type(pin.id, pin.electrical)
                if kind is None:
                    continue
                meta = io_map.get(pin.id) if isinstance(io_map.get(pin.id), dict) else {}
                if str(meta.get("type") or "").upper() in {"DI", "DO", "AI", "AO"}:
                    kind = str(meta.get("type")).upper()
                note = str(meta.get("description") or "").strip()
                other, function, wire_no, dev_sheet, line, connected_tag = _connected_end(
                    project, catalog, comp.id, pin.id
                )
                # Points 8–15 are drawn on the continuation card (IO1_2.I08),
                # so the wire does not land on the home id.
                if not other and not wire_no:
                    for cont_id in continuations.get(comp.id, ()):
                        other, function, wire_no, dev_sheet, line, connected_tag = _connected_end(
                            project, catalog, cont_id, pin.id
                        )
                        if other or wire_no:
                            break
                if not wire_no:
                    wire_no = str(meta.get("wire") or "")
                spare = "no" if other or wire_no else "yes"
                if str(meta.get("field") or "").lower() == "spare":
                    spare = "yes"
                explicit_tag = str(meta.get("tag") or "").strip()
                if explicit_tag:
                    tag = explicit_tag
                elif other:
                    tag = connected_tag or other
                else:
                    tag = f"Spare_{pin.id}"
                stored = str(meta.get("address") or "").strip()
                sheet_address = ""
                address_style = ""
                if comp.properties.get("io_module"):
                    sheet_address, address_style = _module_sheet_address(catalog, comp, pin.id)
                    if sheet_address:
                        stored = sheet_address
                point = stored or _point_label(pin.id, labels)
                tag = _unique_io_tag(tag, used_tags)
                if spare == "yes":
                    description = note or "spare"
                    function = ""
                    line = ""
                    dev_sheet = ""
                    other = ""
                elif comp.properties.get("io_module"):
                    field_ref = str(meta.get("field_ref") or "")
                    other = field_ref
                    function = ""
                    line = ""
                    dev_sheet = sheet.id if field_ref else ""
                    description = note or field_ref or tag
                else:
                    parts = [other or tag]
                    if function:
                        parts.append(function)
                    if note and note != function:
                        parts.append(note)
                    if wire_no:
                        parts.append(f"wire {wire_no}")
                    if dev_sheet and line:
                        parts.append(f"sheet {dev_sheet} line {line}")
                    description = ", ".join(parts)
                rows.append(
                    {
                        "sheet": sheet.id,
                        "module": comp.id,
                        "address": pin.id,
                        "type": kind,
                        "description": description,
                        "wire": wire_no,
                        "device": other,
                        "library_id": comp.library_id,
                        "point": point,
                        "tag": tag,
                        "function": function,
                        "line": line,
                        "spare": spare,
                        "device_sheet": dev_sheet,
                        "slot": slot,
                        "start_address": _module_start_address(comp, device),
                        "field": str(meta.get("field") or ""),
                        "sheet_address": sheet_address,
                        "address_style": address_style,
                        "platform": str(comp.properties.get("platform") or ""),
                    }
                )
    return rows


def _module_sheet_address(catalog: Catalog, comp: Component, pin_id: str) -> tuple[str, str]:
    """Platform address from the module record. Same function the sheet uses."""

    from panelcad.library.io_modules import find_module, point_address

    try:
        module = find_module(catalog, str(comp.properties.get("io_module") or ""))
        point = module.point(pin_id)
    except (KeyError, ValueError):
        return "", ""
    raw_base = comp.properties.get("address_base")
    base = module.address_base if raw_base in (None, "") else int(raw_base)
    text = point_address(
        module,
        point,
        slot=int(comp.properties.get("slot") or 1),
        position=int(comp.properties.get("position") or 1),
        address_base=base,
    )
    return text, module.address_style


def _norm_row(row: dict[str, Any]) -> dict[str, str]:
    return {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}


def import_io_csv(project: Project, catalog: Catalog, path: Path | str) -> dict[str, Any]:
    """Fill PLC pin descriptions from a spreadsheet. Does not create modules."""
    raw = Path(path).read_text(encoding="utf-8-sig")
    reader = csv.DictReader(StringIO(raw))
    updated: list[str] = []
    errors: list[str] = []
    for raw_row in reader:
        if not raw_row:
            continue
        row = _norm_row(raw_row)
        module = row.get("module") or row.get("ref") or row.get("plc") or ""
        address = row.get("address") or row.get("pin") or ""
        if not module or not address:
            errors.append("row needs module and address")
            continue
        sheet_id = row.get("sheet") or ""
        comp: Component | None = None
        sheet: Sheet | None = None
        pages = [project.sheet(sheet_id)] if sheet_id else list(project.sheets)
        for page in pages:
            try:
                comp = page.component(module)
                sheet = page
                break
            except KeyError:
                continue
        if comp is None or sheet is None:
            errors.append(f"no module {module}")
            continue
        device = _device(catalog, comp)
        if device is None or symbol_role(device) != "plc":
            errors.append(f"{module} is not a PLC")
            continue
        pin_id = address
        if io_module_row := bool(comp.properties.get("io_module")):
            from panelcad.core.io_sheets import resolve_point_id

            try:
                pin_id = resolve_point_id(comp, catalog, address)
            except ValueError as exc:
                errors.append(str(exc))
                continue
        elif not any(pin.id == address for pin in device.pins):
            errors.append(f"{module} has no address {address}")
            continue
        address = pin_id
        io_module = io_module_row
        description = row.get("description") or row.get("desc") or ""
        kind = (row.get("type") or "").upper()
        wire_no = row.get("wire") or row.get("wire_number") or row.get("number") or ""
        tag = row.get("tag") or ""
        field = (row.get("field") or "").lower()
        if description and not io_module:
            labels = dict(comp.properties.get("pin_labels") or {})
            labels[address] = description
            comp.properties["pin_labels"] = labels
        io_map = dict(comp.properties.get("io") or {})
        if io_module and isinstance(io_map.get(address), dict):
            meta = dict(io_map[address])
            if description:
                meta["description"] = description
                meta["user_edited"] = True
            if kind:
                meta["type"] = kind
            if wire_no:
                meta["wire"] = wire_no
            if tag:
                meta["tag"] = tag
                meta["user_edited"] = True
            if field:
                meta["field"] = field
                meta["field_library"] = ""
                meta["user_edited"] = True
            io_map[address] = meta
        else:
            io_map[address] = {"description": description, "type": kind, "wire": wire_no}
        comp.properties["io"] = io_map
        if wire_no:
            for wire in _lands(sheet, comp.id, address):
                if not str(wire.number or "").strip():
                    wire.number = wire_no
        updated.append(f"{module}.{address}")
    return {"updated": updated, "errors": errors}


def assign_item_numbers(project: Project) -> int:
    """Store the BOM item number on each device so a panel balloon can show it."""
    item = 1
    for sheet in project.sheets:
        for comp in sheet.components:
            if str(comp.properties.get("bom") or "").lower() == "no":
                continue
            comp.properties["item"] = str(item)
            item += 1
    return item - 1


def cable_rows(project: Project) -> list[dict[str, str]]:
    """Field-cable schedule when the project has cables, else wires that share ``wire.cable``."""

    from panelcad.core.cables import cable_rows as _rows

    return _rows(project)


def panel_wire_annotations(project: Project, catalog: Catalog) -> list[dict[str, str]]:
    """Wire number, color, and gauge beside the panel footprint of each end.

    ``catalog`` is accepted so callers can pass the same pair as the other reports.
    """
    del catalog
    loc: dict[str, tuple[str, float, float]] = {}
    for sheet in project.sheets:
        if sheet.kind not in LAYOUT_KINDS:
            continue
        for comp in sheet.components:
            loc.setdefault(comp.id, (sheet.id, comp.x, comp.y))
    rows: list[dict[str, str]] = []
    for sheet in project.sheets:
        if sheet.kind in LAYOUT_KINDS or sheet.kind == "terminal":
            continue
        for wire in sheet.wires:
            frm = str(wire.frm).split(".", 1)[0]
            to = str(wire.to).split(".", 1)[0]
            src = loc.get(frm)
            dst = loc.get(to)
            rows.append(
                {
                    "sheet": sheet.id,
                    "wire": wire.id,
                    "number": wire.number,
                    "color": wire.color,
                    "gauge": wire.gauge,
                    "cable": wire.cable,
                    "from": wire.frm,
                    "to": wire.to,
                    "from_panel": src[0] if src else "",
                    "from_x": f"{src[1]:g}" if src else "",
                    "from_y": f"{src[2]:g}" if src else "",
                    "to_panel": dst[0] if dst else "",
                    "to_x": f"{dst[1]:g}" if dst else "",
                    "to_y": f"{dst[2]:g}" if dst else "",
                }
            )
    return rows
