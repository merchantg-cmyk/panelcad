"""Draw PLC I/O schematic sheets from verified module terminal maps.

One module per sheet, or two modules when each has eight points or fewer.
A module with more than eight points continues on the next A2 landscape sheet
(eight points each) with CONT. ON / CONT. FROM references. The sheet kind stays
``io`` and ``drawing_style`` is ``ladder`` so the normal ladder painter draws it.
The 14 mm I/O packer is left for older io sheets.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

from panelcad.core.models import PinOverride
from panelcad.library.io_modules import (
    FIELD_OK,
    IoModule,
    IoPoint,
    find_module,
    point_address,
    require_verified,
)

PITCH = 36.0
CARD_W = 88.0
CARD_Y = 42.0
PIN0 = 26.0
COLUMNS = 8
# Series symbols sit with their left pin 96 mm left of the card. The 24 V bus is
# 16 mm further left so a vertical tie does not enter the next rung's symbol.
HOT_FROM_CARD = 112.0

# field key → (library id suffix, wiring)
# series: left pin from the hot side, right pin toward the card or the load
_SERIES = {
    "pushbutton": "operators.pb_no",
    "limit_switch": "operators.limit_no",
    "relay_coil": "contactors.coil",
    "solenoid": "loads.solenoid",
    "pilot_light": "operators.pilot_light",
}

IO_ADD_MODULE_SCHEMA: dict[str, Any] = {
    "name": "io_add_module",
    "description": (
        "Place one verified PLC I/O module on an I/O sheet. "
        "Unverified modules are refused with the library message. "
        "Does not draw field devices; call io_assign_point and io_generate_sheets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "module": {"type": "string", "description": "Module id, such as brx.bx_08nd3 or 5069-IB16."},
            "ref": {"type": "string", "description": "Sheet tag for the module."},
            "slot": {"type": "integer", "description": "Logix or Micro800 slot. Default 1."},
            "position": {"type": "integer", "description": "CLICK module position. 1 is the first expansion module."},
            "polarity": {"type": "string", "description": "sink or source, when the module allows both."},
            "address_base": {"type": "integer", "description": "TIA process-image byte, or Do-more element base."},
            "fuses": {"type": "boolean", "description": "Fuse the hot feed. Otherwise the project fuse setting is used."},
        },
        "required": ["module"],
    },
}

IO_ASSIGN_POINT_SCHEMA: dict[str, Any] = {
    "name": "io_assign_point",
    "description": "Set the tag, description, and field device for one module point. Updates the I/O list and the sheet.",
    "parameters": {
        "type": "object",
        "properties": {
            "module": {"type": "string", "description": "Module tag on the sheet."},
            "pin": {"type": "string", "description": "Pin id (the I/O list address column)."},
            "tag": {"type": "string"},
            "description": {"type": "string"},
            "field": {
                "type": "string",
                "description": "pushbutton, limit_switch, prox_3wire, relay_coil, solenoid, pilot_light, transmitter_2wire, transmitter_4wire, or spare.",
            },
        },
        "required": ["module", "pin"],
    },
}

IO_GENERATE_SHEETS_SCHEMA: dict[str, Any] = {
    "name": "io_generate_sheets",
    "description": (
        "Draw field devices, terminals, commons, and wire numbers for I/O modules already added. "
        "Regenerating keeps a user-changed field device and reports conflicts. One undo."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "modules": {"type": "array", "items": {"type": "string"}, "description": "Module tags. Omit for every added module."},
            "fuses": {"type": "boolean"},
        },
    },
}


def _slug(text: str) -> str:
    from panelcad.api.session import _slug as slug

    return slug(text)


def _standard(session: Any) -> str:
    try:
        return str(session.drawing_standard() or "")
    except Exception:  # noqa: BLE001
        return str(getattr(session.project.title_block, "standard", "") or "")


def _lib(session: Any, suffix: str) -> str:
    standard = _standard(session).upper()
    if standard.startswith("NEMA"):
        nema = f"nema.{suffix}"
        if nema in session.catalog.devices:
            return nema
    iec = f"iec.{suffix}"
    if iec in session.catalog.devices:
        return iec
    nema = f"nema.{suffix}"
    if nema in session.catalog.devices:
        return nema
    raise KeyError(suffix)


def _field_on_left(module: IoModule, polarity: str) -> bool:
    kind = module.point_type
    if kind in {"do_source", "do_relay", "ao", "ao_voltage", "ao_current"}:
        return False
    return True


def _rail_pair(session: Any, module: IoModule, polarity: str) -> tuple[str, str]:
    """Left rail, right rail. Sourcing inputs put 0 V on the left so the field stays left of the card."""

    if module.point_type == "do_relay":
        hot = str(getattr(session.project.title_block, "rail_hot", "") or "").strip()
        ret = str(getattr(session.project.title_block, "rail_return", "") or "").strip()
        if hot and ret and hot.upper() not in {"24V", "24VDC", "+24", "24VDC+"}:
            return hot, ret
        return "24VDC", "0V"
    if polarity == "source" and module.point_type in {"di_source", "di_sink_source"}:
        return "0V", "24VDC"
    return "24VDC", "0V"


def _polarity(module: IoModule, requested: str) -> str:
    ask = str(requested or "").strip().lower()
    kind = module.point_type
    if kind == "di_sink":
        if ask and ask not in {"sink", "sinking"}:
            raise ValueError(f"{module.catalog or module.id} is sinking only")
        return "sink"
    if kind in {"do_source", "do_sink"}:
        fixed = "source" if kind == "do_source" else "sink"
        if ask and ask not in {fixed, fixed + "ing"}:
            raise ValueError(f"{module.catalog or module.id} is {fixed} only")
        return fixed
    if kind == "do_relay":
        return "relay"
    if ask in {"source", "sourcing"}:
        return "source"
    return "sink"


def _uses_fuses(session: Any, explicit: bool | None) -> bool:
    if explicit is not None:
        return bool(explicit)
    flag = session.project.properties.get("io_fuses")
    if str(flag).strip().lower() in {"1", "true", "yes", "y"}:
        return True
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if "fuse" in str(comp.library_id).lower():
                return True
    return False


def _modules_on(session: Any, sheet: Any) -> list[Any]:
    return [comp for comp in sheet.components if comp.properties.get("io_module")]


def _is_io_ladder(sheet: Any) -> bool:
    return getattr(sheet, "kind", "") == "io" and getattr(sheet, "drawing_style", "") == "ladder"


def _has_side_note(module: IoModule) -> bool:
    """A maker note needs the open column beside the card. It cannot share a sheet."""

    if str(module.return_note or "").strip():
        return True
    return any(item.role in {"nc", ""} for item in module.unwired)


def _fresh_io_sheet(session: Any, module: IoModule, rails: tuple[str, str], *, active: bool) -> Any:
    title = module.catalog or module.id
    base = _slug(f"io-{title}")
    sheet_id = base
    taken = {page.id for page in session.project.sheets}
    n = 2
    while sheet_id in taken:
        sheet_id = f"{base}-{n}"
        n += 1
    sheet = session.add_sheet(sheet_id, title=f"I/O {title}", kind="io", make_active=active)
    sheet.page.size = "A2"
    sheet.page.orientation = "landscape"
    sheet.drawing_style = "ladder"
    sheet.rail_hot, sheet.rail_return = rails
    return sheet


def _host_sheet(session: Any, module: IoModule, rails: tuple[str, str]):
    small = len(module.points) <= COLUMNS and not _has_side_note(module)
    if small:
        for sheet in session.project.sheets:
            if not _is_io_ladder(sheet):
                continue
            if _is_continuation(sheet):
                continue
            placed = _modules_on(session, sheet)
            if len(placed) != 1:
                continue
            other = placed[0]
            if int(other.properties.get("io_point_count") or 99) > COLUMNS:
                continue
            other_module = find_module(
                session.catalog, str(other.properties.get("io_module") or "")
            )
            if _has_side_note(other_module):
                continue
            if (str(sheet.rail_hot), str(sheet.rail_return)) != rails:
                continue
            return sheet
    return _fresh_io_sheet(session, module, rails, active=True)


def _detach_noted_partners(session: Any) -> dict[str, _Redraw]:
    """Move a second card off a sheet whose note would cover that card's field wiring.

    The redraw memory is taken before the old wiring is deleted.
    """

    pending: dict[str, _Redraw] = {}
    for sheet in list(session.project.sheets):
        if not _is_io_ladder(sheet) or _is_continuation(sheet):
            continue
        comps = [comp for comp in sheet.components if comp.properties.get("io_module")]
        if len(comps) < 2:
            continue
        modules = [
            (comp, find_module(session.catalog, str(comp.properties.get("io_module") or "")))
            for comp in comps
        ]
        if not any(_has_side_note(module) for _comp, module in modules):
            continue
        for comp, module in modules[1:]:
            if not (_has_side_note(modules[0][1]) or _has_side_note(module)):
                continue
            pending[comp.id] = _capture_redraw(session, comp.id)
            for page in session.project.sheets:
                _clear_owned(session, page, comp.id)
            sheet.components = [item for item in sheet.components if item is not comp]
            fresh = _fresh_io_sheet(
                session,
                module,
                (str(sheet.rail_hot or ""), str(sheet.rail_return or "")),
                active=False,
            )
            comp.x = 80.0
            comp.y = CARD_Y
            fresh.components.append(comp)
    return pending


def _spare_tag(module_ref: str, point: IoPoint) -> str:
    terminal = re.sub(r"[^A-Za-z0-9]", "_", str(point.terminal or point.pin))
    return f"SPARE_{module_ref}_{terminal}"


def _blank_io(module: IoModule, point: IoPoint, address: str, module_ref: str) -> dict[str, Any]:
    return {
        "tag": _spare_tag(module_ref, point),
        "description": "spare",
        "type": module.io_kind(),
        "field": "spare",
        "field_library": "",
        "field_ref": "",
        "terminal": point.terminal,
        "terminal_ref": "",
        "address": address,
        "user_edited": False,
    }


def _next_module_ref(session: Any) -> str:
    """IO1, IO2, … unique across the project. GUI, CLI, and the agent share this."""

    used = {comp.id for sheet in session.project.sheets for comp in sheet.components}
    n = 1
    while f"IO{n}" in used:
        n += 1
    return f"IO{n}"


def _allocate_base(session: Any, module: IoModule, explicit: int | None) -> int:
    if explicit is not None:
        return int(explicit)
    if module.address_style != "domore":
        return int(module.address_base)
    kind = module.io_kind()
    step = 8 if kind in {"DI", "DO"} else 1
    taken: set[int] = set()
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if not comp.properties.get("io_module"):
                continue
            try:
                other = find_module(session.catalog, str(comp.properties.get("io_module") or ""))
            except ValueError:
                continue
            if other.address_style != "domore" or other.io_kind() != kind:
                continue
            base = int(comp.properties.get("address_base") or 0)
            count = int(comp.properties.get("io_point_count") or len(other.points))
            taken.update(range(base, base + count))
    base = 0
    count = len(module.points)
    while any((base + index) in taken for index in range(count)):
        base += step
    return base


def _placeholder(session: Any, module: IoModule) -> bool:
    device = session.catalog.get(module.library_id)
    return str(getattr(device.panel, "dim_source", "") or "") != "catalog"


def add_module(
    session: Any,
    module_id: str,
    *,
    ref: str | None = None,
    slot: int = 1,
    position: int = 1,
    polarity: str = "",
    address_base: int | None = None,
) -> dict[str, Any]:
    module = find_module(session.catalog, module_id)
    require_verified(module)
    chosen = _polarity(module, polarity)
    base = _allocate_base(session, module, address_base)
    rails = _rail_pair(session, module, chosen)
    sheet = _host_sheet(session, module, rails)
    device = session.catalog.get(module.library_id)
    tag = str(ref or "").strip() or _next_module_ref(session)
    if any(comp.id == tag for page in session.project.sheets for comp in page.components):
        raise ValueError(f"{tag} is already used")
    labels: dict[str, str] = {}
    terminals: dict[str, str] = {}
    names: dict[str, str] = {}
    io_map: dict[str, Any] = {}
    for point in module.points:
        address = point_address(
            module, point, slot=int(slot), position=int(position), address_base=base
        )
        labels[point.pin] = address
        terminals[point.pin] = point.terminal
        names[point.pin] = point.name or point.terminal
        io_map[point.pin] = _blank_io(module, point, address, tag)
    for item in module.power:
        terminals[item.pin] = item.terminal
        names[item.pin] = item.pin
    for item in module.power:
        labels[item.pin] = item.terminal
    comp = session.place(
        module.library_id,
        ref=tag,
        x=80.0,
        y=CARD_Y,
        sheet_id=sheet.id,
        snap=False,
        properties={
            "io_module": module.id,
            "io_point_count": len(module.points),
            "io": io_map,
            "pin_labels": labels,
            "slot": str(int(slot)),
            "position": str(int(position)),
            "polarity": chosen,
            "address_base": base,
            "address_style": module.address_style,
            "catalog": module.catalog,
            "manufacturer": module.manufacturer,
            "part_number": module.catalog,
            "platform": module.platform,
            "io_terminals": terminals,
            "io_names": names,
            "bom_accessories": list(module.accessories),
            "placeholder_footprint": _placeholder(session, module),
            "ladder_card": True,
            "card_w": CARD_W,
            "verified": True,
            "source": module.source,
        },
    )
    # Keep the library symbol from drawing until generate sets the real card height.
    comp.properties["card_h"] = max(device.schematic_height, 40.0)
    return {
        "ref": comp.id,
        "sheet": sheet.id,
        "module": module.id,
        "catalog": module.catalog,
        "library_id": module.library_id,
        "polarity": chosen,
        "slot": int(slot),
        "position": int(position),
        "address_base": base,
        "points": [
            {"pin": point.pin, "terminal": point.terminal, "address": labels[point.pin], "type": module.io_kind()}
            for point in module.points
        ],
        "message": "",
    }


def _find_module_comp(session: Any, ref: str):
    token = str(ref or "").strip()
    for sheet in session.project.sheets:
        try:
            comp = sheet.component(token)
        except KeyError:
            continue
        if comp.properties.get("io_module"):
            return sheet, comp
    raise KeyError(f"no I/O module {token}")


def resolve_point_id(comp: Any, catalog: Any, token: str) -> str:
    """Pin id, maker terminal, or the platform address printed on the sheet."""

    module = find_module(catalog, str(comp.properties.get("io_module") or ""))
    text = str(token or "").strip()
    try:
        return module.point(text).pin
    except KeyError:
        pass
    labels = comp.properties.get("pin_labels") if isinstance(comp.properties.get("pin_labels"), dict) else {}
    io_map = comp.properties.get("io") if isinstance(comp.properties.get("io"), dict) else {}
    for point in module.points:
        meta = io_map.get(point.pin) if isinstance(io_map.get(point.pin), dict) else {}
        if text in {point.terminal, str(labels.get(point.pin) or ""), str(meta.get("address") or "")}:
            return point.pin
    raise ValueError(f"{comp.id} has no point {text}")


def assign_point(
    session: Any,
    module_ref: str,
    pin: str,
    *,
    tag: str | None = None,
    description: str | None = None,
    field: str | None = None,
) -> dict[str, Any]:
    sheet, comp = _find_module_comp(session, module_ref)
    module = find_module(session.catalog, str(comp.properties.get("io_module") or ""))
    require_verified(module)
    try:
        pin_id = resolve_point_id(comp, session.catalog, str(pin or ""))
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    point = module.point(pin_id)
    io_map = dict(comp.properties.get("io") or {})
    meta = dict(io_map.get(pin_id) or {})
    if tag is not None:
        text = str(tag).strip()
        if "." in text or not text:
            raise ValueError("point tag cannot be empty or contain '.'")
        meta["tag"] = text
        meta["user_edited"] = True
    if description is not None:
        meta["description"] = str(description).strip()
        meta["user_edited"] = True
    if field is not None:
        kind = str(field).strip().lower()
        allowed = FIELD_OK.get(module.point_type, {"spare"})
        if kind not in allowed and kind not in {"spare"}:
            meta["field_conflict"] = kind
        meta["field"] = kind or "spare"
        meta["user_edited"] = True
        meta["field_library"] = ""
        meta["user_removed"] = False
    if str(meta.get("field") or "") == "spare":
        meta["description"] = meta.get("description") or "spare"
        if not str(meta.get("tag") or "").strip() or str(meta.get("tag")).startswith("SPARE_"):
            meta["tag"] = _spare_tag(comp.id, point)
    io_map[pin_id] = meta
    comp.properties["io"] = io_map
    _push_point(session, sheet, comp, pin_id)
    return {"module": comp.id, "pin": pin_id, "sheet": sheet.id, **{k: meta.get(k, "") for k in ("tag", "description", "field", "address")}}


def _push_point(session: Any, sheet: Any, module_comp: Any, pin: str) -> None:
    """Rename the field device and refresh its description from the I/O map."""

    from panelcad.core.electrical import rename_component

    meta = (module_comp.properties.get("io") or {}).get(pin) or {}
    field_ref = str(meta.get("field_ref") or "")
    if not field_ref:
        return
    try:
        field = sheet.component(field_ref)
    except KeyError:
        return
    want = str(meta.get("tag") or "").strip()
    if want and want != field.id and "." not in want:
        if not any(other.id == want for other in sheet.components):
            rename_component(session.project, sheet, field.id, want)
            meta = dict(meta)
            meta["field_ref"] = want
            io_map = dict(module_comp.properties.get("io") or {})
            io_map[pin] = meta
            module_comp.properties["io"] = io_map
            field = sheet.component(want)
    desc = str(meta.get("description") or "").strip()
    if desc:
        field.properties["io_description"] = desc


def pull_io_sheet_edits(project: Any, catalog: Any) -> None:
    """Copy a renamed field tag or an edited description back onto the module I/O map."""

    del catalog
    for sheet in project.sheets:
        for comp in sheet.components:
            link = comp.properties.get("io_point")
            if not isinstance(link, dict):
                continue
            if str(link.get("role") or "") == "terminal":
                continue
            module_ref = str(link.get("module") or "")
            pin = str(link.get("pin") or "")
            module_comp = None
            for page in project.sheets:
                try:
                    found = page.component(module_ref)
                except KeyError:
                    continue
                if found.properties.get("io_module"):
                    module_comp = found
                    break
            if module_comp is None:
                continue
            io_map = module_comp.properties.get("io")
            if not isinstance(io_map, dict) or not isinstance(io_map.get(pin), dict):
                continue
            meta = dict(io_map[pin])
            if comp.id != str(meta.get("field_ref") or "") and comp.id != module_ref:
                meta["tag"] = comp.id
                meta["field_ref"] = comp.id
                meta["user_edited"] = True
            edited = str(comp.properties.get("io_description") or "").strip()
            current = str(meta.get("description") or "").strip()
            if edited and edited != current:
                # A generated spare note always stores "spare". That is not an
                # edit, and it must not wipe a description already assigned.
                spare_note = "notes.spare" in str(comp.library_id or "")
                placeholder = (
                    spare_note
                    and edited.lower() == "spare"
                    and bool(meta.get("user_edited"))
                    and current.lower() not in {"", "spare"}
                )
                if not placeholder:
                    meta["description"] = edited
                    meta["user_edited"] = True
            lib = str(comp.library_id or "")
            mapped = str(meta.get("field_library") or "")
            # An import clears field_library when it sets a new field. Do not copy the
            # old symbol back over that. A real symbol edit still has the previous library.
            if lib and mapped and lib != mapped and "notes.spare" not in lib:
                meta["field_library"] = lib
                meta["user_edited"] = True
            io_map[pin] = meta
            module_comp.properties["io"] = io_map


def apply_imported_io(session: Any) -> None:
    """After a CSV import, push tags and descriptions onto field devices."""

    for sheet in session.project.sheets:
        for comp in list(sheet.components):
            if not comp.properties.get("io_module"):
                continue
            for pin in list((comp.properties.get("io") or {}).keys()):
                _push_point(session, sheet, comp, str(pin))


def _clear_owned(session: Any, sheet: Any, module_ref: str) -> None:
    owned = [comp.id for comp in sheet.components if str(comp.properties.get("io_owner") or "") == module_ref]
    if not owned:
        return
    drop = set(owned)
    sheet.components = [comp for comp in sheet.components if comp.id not in drop]
    kept = []
    for wire in sheet.wires:
        ends = (str(wire.frm).split(".", 1)[0], str(wire.to).split(".", 1)[0])
        if ends[0] in drop or ends[1] in drop:
            continue
        kept.append(wire)
    sheet.wires = kept


def _unique_ref(session: Any, prefix: str) -> str:
    used = {comp.id for sheet in session.project.sheets for comp in sheet.components}
    n = 1
    while f"{prefix}{n}" in used:
        n += 1
    return f"{prefix}{n}"


class _Redraw:
    """Refs and wire numbers from the previous draw of one module."""

    def __init__(self) -> None:
        self.refs: dict[tuple[str, str, str], str] = {}
        self.numbers: dict[tuple, str] = {}


def _redraw(session: Any) -> _Redraw | None:
    memory = getattr(session, "_io_redraw", None)
    return memory if isinstance(memory, _Redraw) else None


def _saved_ref(session: Any, owner: str, role: str, point: str, prefix: str) -> str:
    memory = _redraw(session)
    saved = memory.refs.get((owner, role, point)) if memory is not None else None
    if saved and not any(comp.id == saved for sheet in session.project.sheets for comp in sheet.components):
        return saved
    return _unique_ref(session, prefix)


def _role(owner: str, role: str, point: str = "", **extra: Any) -> dict[str, Any]:
    props: dict[str, Any] = {"io_owner": owner, "io_role": role, "io_role_point": point}
    props.update(extra)
    return props


def _end_token(sheet: Any, ref: str) -> tuple:
    text = str(ref)
    if "." not in text:
        return ("rail", text)
    cid, pin = text.split(".", 1)
    try:
        comp = sheet.component(cid)
    except KeyError:
        return ("id", cid, pin)
    if comp.properties.get("io_module") or comp.properties.get("io_continuation"):
        tag = str(comp.properties.get("shown_tag") or comp.id)
        return ("card", tag, pin)
    role = str(comp.properties.get("io_role") or "")
    owner = str(comp.properties.get("io_owner") or "")
    point = str(comp.properties.get("io_role_point") or "")
    raw = comp.properties.get("io_point")
    if isinstance(raw, dict):
        point = point or str(raw.get("pin") or "")
        role = role or str(raw.get("role") or "field")
    if role or owner:
        return ("part", owner, role or str(comp.library_id), point, pin)
    return ("id", comp.id, pin)


def _wire_key(sheet: Any, frm: str, to: str) -> tuple:
    return tuple(sorted((_end_token(sheet, frm), _end_token(sheet, to))))


def _touches_owner(sheet: Any, ends: tuple[str, str], owner: str) -> bool:
    for end in ends:
        if "." not in str(end):
            continue
        cid = str(end).split(".", 1)[0]
        try:
            comp = sheet.component(cid)
        except KeyError:
            continue
        if str(comp.properties.get("io_owner") or "") == owner:
            return True
        if comp.id == owner or str(comp.properties.get("io_continuation") or "") == owner:
            return True
    return False


def _capture_redraw(session: Any, owner: str) -> _Redraw:
    memory = _Redraw()
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if str(comp.properties.get("io_owner") or "") != owner:
                continue
            role = str(comp.properties.get("io_role") or "")
            point = str(comp.properties.get("io_role_point") or "")
            raw = comp.properties.get("io_point")
            if not role and isinstance(raw, dict):
                role = str(raw.get("role") or "field")
                point = point or str(raw.get("pin") or "")
            if role:
                memory.refs[(owner, role, point)] = comp.id
        for wire in sheet.wires:
            ends = (str(wire.frm), str(wire.to))
            if not _touches_owner(sheet, ends, owner):
                continue
            number = str(wire.number or "").strip()
            if number:
                memory.numbers[_wire_key(sheet, wire.frm, wire.to)] = number
    return memory


def _is_continuation(sheet: Any) -> bool:
    if str(getattr(sheet, "io_cont", "") or "").strip():
        return True
    return str(getattr(sheet, "description", "") or "").startswith("io-cont:")


def _place(
    session: Any,
    sheet: Any,
    library_id: str,
    x: float,
    y: float,
    *,
    ref: str | None = None,
    properties: dict[str, Any] | None = None,
):
    props = {"bom": "no", **(properties or {})}
    # Junctions, corners, notes, and schematic earth symbols are not panel parts.
    dressing = (
        "notes" in library_id
        or "junction" in library_id
        or "waypoint" in library_id
        or library_id.endswith("connection.ground")
    )
    if dressing:
        props["bom"] = "no"
    else:
        props.pop("bom", None)
    return session.place(library_id, ref=ref, x=x, y=y, sheet_id=sheet.id, snap=False, properties=props)


def _wire(session: Any, sheet: Any, frm: str, to: str, number: str | None = None) -> Any:
    memory = _redraw(session)
    saved = memory.numbers.get(_wire_key(sheet, frm, to)) if memory is not None else None
    return session.wire(frm, to, sheet_id=sheet.id, number=saved or number or None)


def _pin(device: Any, side: str):
    for pin in device.pins:
        if pin.side == side:
            return pin
    raise KeyError(side)


def _junction(session: Any, sheet: Any, cx: float, cy: float, owner: str) -> str:
    """Filled dot. Only call this where three or more wires meet."""

    point = f"{cx:.1f},{cy:.1f}"
    ref = _saved_ref(session, owner, "junction", point, "J")
    _place(
        session,
        sheet,
        _lib(session, "connection.junction"),
        cx - 3.0,
        cy - 3.0,
        ref=ref,
        properties=_role(owner, "junction", point, bom="no", hide_tag=True),
    )
    return ref


def _waypoint(session: Any, sheet: Any, cx: float, cy: float, owner: str) -> str:
    """Corner with no dot. Ladder routing elbows through it."""

    point = f"{cx:.1f},{cy:.1f}"
    ref = _saved_ref(session, owner, "waypoint", point, "W")
    library_id = "iec.connection.waypoint"
    if library_id not in session.catalog.devices:
        library_id = _lib(session, "connection.waypoint")
    _place(
        session,
        sheet,
        library_id,
        cx - 1.0,
        cy - 1.0,
        ref=ref,
        properties=_role(owner, "waypoint", point, bom="no", hide_tag=True),
    )
    return ref


def _coil_voltage(sheet: Any) -> str:
    """Coil rating written on a generated relay. Matches the sheet rails."""

    for name in (getattr(sheet, "rail_hot", ""), getattr(sheet, "rail_return", "")):
        text = str(name or "").upper().replace(" ", "")
        if "24" in text and "DC" in text:
            return "24VDC"
        if text in {"24V", "+24"}:
            return "24VDC"
    hot = str(getattr(sheet, "rail_hot", "") or "").strip()
    return hot or "24VDC"


def _positive(name: str) -> bool:
    token = str(name or "").upper().replace(" ", "")
    if token in {"", "0V", "0VDC", "COM", "M"}:
        return False
    return "24" in token


def _hot_bus_x(card_x: float) -> float:
    return card_x - HOT_FROM_CARD


def _margin_x(sheet: Any) -> float:
    """0 V column beside the left rail, clear of the fuse and the terminal leads."""

    from panelcad.core.ladder import rail_xs

    left, _right = rail_xs(sheet)
    return left + 11.0


def _card_x(left: float, columns: int, column: int, field_left: bool) -> float:
    if field_left:
        offsets = [156.0, 378.0] if columns == 2 else [200.0]
    else:
        offsets = [58.0, 286.0] if columns == 2 else [78.0]
    return left + offsets[column]


def _layout_card(comp: Any, module: IoModule, *, x: float, rows: list[list[IoPoint]], field_left: bool) -> dict[str, tuple[float, float]]:
    """Pin overrides and world Y of each signal pin. Returns pin → world (x is unused)."""

    height_rows = max(len(rows[0]) if rows else 1, 1)
    card_h = PIN0 + max(height_rows - 1, 0) * PITCH + 24.0
    comp.x = x
    comp.y = CARD_Y
    comp.properties["ladder_card"] = True
    comp.properties["io_pin_limit"] = True
    comp.properties["card_w"] = CARD_W
    comp.properties["card_h"] = card_h
    comp.pin_overrides = {}
    world: dict[str, float] = {}
    for column, points in enumerate(rows):
        edge = 0.0 if field_left else CARD_W
        side = "left" if field_left else "right"
        # Two columns are two cards, so each card has one column of overrides.
        del column
        labels = comp.properties.get("pin_labels") if isinstance(comp.properties.get("pin_labels"), dict) else {}
        for row, point in enumerate(points):
            local_y = PIN0 + row * PITCH
            address = str(labels.get(point.pin) or "")
            caption = f"{point.terminal}  {address}" if address and address != point.terminal else (address or point.terminal)
            comp.pin_overrides[point.pin] = PinOverride(x=edge, y=local_y, side=side, label=caption)
            world[point.pin] = CARD_Y + local_y
    group_y: dict[str, float] = {}
    for group in module.groups:
        for pin in group.points:
            if pin in world and group.common not in group_y:
                group_y[group.common] = world[pin] - CARD_Y
    for item in module.power:
        # Maker terminal name. An internal id such as LAplus is not printed.
        # A maker common (1M, COM0, FE) stays beside its screw number.
        caption = item.terminal
        pin_name = str(item.pin)
        if pin_name != caption and pin_name.upper() == pin_name and pin_name.isalnum():
            caption = f"{pin_name}  {caption}"
        if item.role == "common":
            local_y = group_y.get(item.pin, PIN0)
            if item.pin not in {point.pin for column in rows for point in column}:
                if not any(item.pin == group.common and any(pin in world for pin in group.points) for group in module.groups):
                    continue
            if field_left:
                comp.pin_overrides[item.pin] = PinOverride(x=CARD_W, y=local_y, side="right", label=caption)
            else:
                comp.pin_overrides[item.pin] = PinOverride(x=0.0, y=local_y, side="left", label=caption)
        elif item.role == "supply_plus":
            # Top edge, so the feed can run above the card instead of across the rungs.
            comp.pin_overrides[item.pin] = PinOverride(x=18.0, y=0.0, side="top", label=caption)
        elif item.role == "supply_minus":
            comp.pin_overrides[item.pin] = PinOverride(x=CARD_W / 2.0, y=card_h, side="bottom", label=caption)
        elif item.role == "earth":
            # Below the 11 mm card header and above the first input (PIN0).
            # The lead runs above the commons bus; the symbol sits to the right of that bus.
            comp.pin_overrides[item.pin] = PinOverride(x=CARD_W, y=16.0, side="right", label=caption)
        elif item.role == "shield":
            continue
        elif item.role == "signal_minus":
            point = module.point(item.point) if item.point else None
            if point is None or point.pin not in world:
                continue
            # The field terminal is 24 mm tall, so n− sits 18 mm under n+ and the
            # wire runs clear of the plus terminal. The transmitter's own SIG0
            # pin is still 12 mm under SIG (io_field.yaml); a short jog joins them.
            local_y = world[point.pin] - CARD_Y + 18.0
            if field_left:
                comp.pin_overrides[item.pin] = PinOverride(x=0.0, y=local_y, side="left", label=caption)
            else:
                comp.pin_overrides[item.pin] = PinOverride(x=CARD_W, y=local_y, side="right", label=caption)
    return world


def _note(session: Any, sheet: Any, x: float, y: float, text: str, owner: str, width: float = 150.0) -> None:
    _place(
        session,
        sheet,
        _lib(session, "notes.spare"),
        x,
        y,
        properties={"io_owner": owner, "bom": "no", "text": text, "width": width, "tag": ""},
    )


def _terminal(
    session: Any,
    sheet: Any,
    x_left: float,
    rung_y: float,
    owner: str,
    module_ref: str,
    pin: str,
    number: str,
) -> str:
    """One numbered field terminal on the module's strip. The maker number is the caption."""

    strip = f"TB{module_ref}"
    memory = _redraw(session)
    saved = memory.refs.get((owner, "terminal", pin)) if memory is not None else None
    used = {comp.id for page in session.project.sheets for comp in page.components}
    if saved and saved not in used:
        ref = saved
        n = 1
    else:
        n = 1
        while True:
            ref = f"X{module_ref}-{n}"
            if ref not in used:
                break
            n += 1
    _place(
        session,
        sheet,
        "iec.terminals.x_2p5",
        x_left,
        rung_y - 12.0,
        ref=ref,
        properties=_role(
            owner,
            "terminal",
            pin,
            io_point={"module": module_ref, "pin": pin, "role": "terminal"},
            strip=strip,
            terminal=str(number),
            level=str(n),
            hide_tag=True,
        ),
    )
    return ref


def _term_no(module_comp: Any, pin: str) -> str:
    raw = module_comp.properties.get("io_terminals")
    if isinstance(raw, dict) and pin in raw:
        return str(raw[pin])
    return pin


def _field_ref(session: Any, sheet: Any, wanted: str) -> str:
    text = str(wanted or "").strip()
    if text and not any(comp.id == text for sheet_i in session.project.sheets for comp in sheet_i.components):
        return text
    return _unique_ref(session, "IO")


def _remember_field(meta: dict[str, Any], comp: Any) -> None:
    meta["field_ref"] = comp.id
    meta["field_library"] = comp.library_id


def _series_device(session: Any, field_key: str) -> tuple[str, Any, Any, Any]:
    suffix = _SERIES[field_key]
    library_id = _lib(session, suffix)
    device = session.catalog.get(library_id)
    return library_id, device, _pin(device, "left"), _pin(device, "right")


def _draw_series(
    session: Any,
    sheet: Any,
    *,
    module_comp: Any,
    pin: str,
    meta: dict[str, Any],
    field_key: str,
    rung_y: float,
    card_x: float,
    hot: str,
    owner: str,
    bus: dict[str, Any],
    toward_card: bool,
    module_ref: str,
) -> None:
    library_id = str(meta.get("field_library") or "")
    if not library_id or "notes.spare" in library_id:
        library_id, device, left, right = _series_device(session, field_key)
    else:
        device = session.catalog.get(library_id)
        try:
            left, right = _pin(device, "left"), _pin(device, "right")
        except KeyError:
            library_id, device, left, right = _series_device(session, field_key)
    if toward_card:
        left_x = card_x - 96.0
        comp_x = left_x - float(left.x)
        comp_y = rung_y - float(left.y)
        term_x = card_x - 20.0
    else:
        # Load sits to the right of the card, then the return rail.
        left_x = card_x + CARD_W + 28.0
        comp_x = left_x - float(left.x)
        comp_y = rung_y - float(left.y)
        term_x = 0.0
    ref = _field_ref(session, sheet, str(meta.get("tag") or ""))
    props = _role(
        owner,
        "field",
        pin,
        io_point={"module": module_ref, "pin": pin, "role": "field"},
        io_description=str(meta.get("description") or ""),
    )
    if field_key == "relay_coil" or str(library_id).endswith("contactors.coil"):
        props["coil"] = _coil_voltage(sheet)
    comp = _place(
        session,
        sheet,
        library_id,
        comp_x,
        comp_y,
        ref=ref,
        properties=props,
    )
    _remember_field(meta, comp)
    if toward_card:
        bus.setdefault("hot", []).append((rung_y, f"{comp.id}.{left.id}"))
        term = _terminal(session, sheet, term_x, rung_y, owner, module_ref, pin, _term_no(module_comp, pin))
        meta["terminal_ref"] = term
        _wire(session, sheet, f"{comp.id}.{right.id}", f"{term}.A")
        _wire(session, sheet, f"{term}.B", f"{module_comp.id}.{pin}")
    else:
        term = _terminal(session, sheet, card_x + CARD_W + 6.0, rung_y, owner, module_ref, pin, _term_no(module_comp, pin))
        meta["terminal_ref"] = term
        _wire(session, sheet, f"{module_comp.id}.{pin}", f"{term}.A")
        _wire(session, sheet, f"{term}.B", f"{comp.id}.{left.id}")
        _wire(session, sheet, f"{comp.id}.{right.id}", sheet.rail_return or "0V")
    del hot


def _draw_prox(
    session: Any,
    sheet: Any,
    *,
    module_comp: Any,
    pin: str,
    meta: dict[str, Any],
    rung_y: float,
    card_x: float,
    owner: str,
    bus: dict[str, Any],
    module_ref: str,
) -> None:
    library_id = str(meta.get("field_library") or "") or _lib(session, "operators.prox_3wire")
    device = session.catalog.get(library_id)
    bk = device.pin("BK")
    bn = device.pin("BN")
    bu = device.pin("BU")
    # BK (right pin) lands just left of the terminal.
    bk_x = card_x - 42.0
    comp_x = bk_x - float(bk.x)
    comp_y = rung_y - float(bk.y)
    ref = _field_ref(session, sheet, str(meta.get("tag") or ""))
    comp = _place(
        session,
        sheet,
        library_id,
        comp_x,
        comp_y,
        ref=ref,
        properties={
            "io_owner": owner,
            "io_point": {"module": module_ref, "pin": pin},
            "io_description": str(meta.get("description") or ""),
        },
    )
    _remember_field(meta, comp)
    bn_x, bn_y = comp_x + float(bn.x), comp_y + float(bn.y)
    del bn_x
    _bu_x, bu_y = comp_x + float(bu.x), comp_y + float(bu.y)
    # Brown is +24 V and blue is 0 V on both sinking and sourcing inputs.
    # A sourcing sheet puts 0 V on the left rail, so blue uses that bus and
    # brown runs to the +24 V rail on the right. They do not swap colours.
    if _positive(sheet.rail_hot or "24VDC"):
        bus.setdefault("hot", []).append((bn_y, f"{comp.id}.BN"))
        bus.setdefault("returns", []).append((f"{comp.id}.BU", bu_y))
    else:
        bus.setdefault("hot", []).append((bu_y, f"{comp.id}.BU"))
        bus.setdefault("returns", []).append((f"{comp.id}.BN", bn_y))
    term = _terminal(session, sheet, card_x - 20.0, rung_y, owner, module_ref, pin, _term_no(module_comp, pin))
    meta["terminal_ref"] = term
    _wire(session, sheet, f"{comp.id}.BK", f"{term}.A")
    _wire(session, sheet, f"{term}.B", f"{module_comp.id}.{pin}")


def _draw_transmitter(
    session: Any,
    sheet: Any,
    *,
    module_comp: Any,
    module: IoModule,
    pin: str,
    meta: dict[str, Any],
    rung_y: float,
    card_x: float,
    owner: str,
    bus: dict[str, Any],
    four_wire: bool,
    module_ref: str,
) -> None:
    suffix = "loads.transmitter_4wire" if four_wire else "loads.transmitter_2wire"
    library_id = str(meta.get("field_library") or "") or _lib(session, suffix)
    device = session.catalog.get(library_id)
    sig = device.pin("SIG")
    # 4-wire needs room for a second terminal beside the plus terminal.
    # 2-wire stays closer to the card. Both stay to the right of the hot bus.
    sig_x = card_x - (60.0 if four_wire else 42.0)
    comp_x = sig_x - float(sig.x)
    comp_y = rung_y - float(sig.y)
    ref = _field_ref(session, sheet, str(meta.get("tag") or ""))
    comp = _place(
        session,
        sheet,
        library_id,
        comp_x,
        comp_y,
        ref=ref,
        properties={
            "io_owner": owner,
            "io_point": {"module": module_ref, "pin": pin},
            "io_description": str(meta.get("description") or ""),
        },
    )
    _remember_field(meta, comp)
    if four_wire:
        # 5069-IN010E-EN-P August 2023 p.9: Signal − lands on Input n−. The
        # transmitter supply is separate from SA and is not the sheet 24 V rail.
        # n− is not bonded to 0 V. "Terminal inputs 0− … 7− are internally connected."
        pwr = device.pin("PWR")
        pwr0 = device.pin("PWR0")
        pwr_y = comp_y + float(pwr.y)
        pwr0_y = comp_y + float(pwr0.y)
        # Open stubs stop left of the hot bus. The leads cross that bus in open
        # space; they do not land on it.
        stub_x = card_x - HOT_FROM_CARD - 18.0
        ext_p = _waypoint(session, sheet, stub_x, pwr_y, owner)
        ext_n = _waypoint(session, sheet, stub_x, pwr0_y, owner)
        _wire(session, sheet, f"{ext_p}.E", f"{comp.id}.PWR")
        _wire(session, sheet, f"{ext_n}.E", f"{comp.id}.PWR0")
        if not bus.get("ext_supply_note"):
            _note(session, sheet, stub_x - 44.0, pwr_y - 2.0, "EXT SUPPLY", owner, width=40.0)
            bus["ext_supply_note"] = True
        sig0 = device.pin("SIG0")
        sig0_x = comp_x + float(sig0.x)
        sig0_y = comp_y + float(sig0.y)
        # Plus terminal is 24 mm tall and ends 12 mm under the SIG rung.
        # The minus terminal sits one column left, on a wire 18 mm under SIG.
        sig0_wire_y = rung_y + 18.0
        pair = ""
        for item in module.power:
            if item.role == "signal_minus" and item.point == pin:
                pair = item.pin
                break
        sig0_term = _terminal(
            session, sheet, card_x - 40.0, sig0_wire_y, owner, module_ref, pair or pin, _term_no(module_comp, pair or pin)
        )
        meta["signal_minus_terminal"] = sig0_term
        jog_x = sig0_x + 4.0
        jog = _waypoint(session, sheet, jog_x, sig0_y, owner)
        down = _waypoint(session, sheet, jog_x, sig0_wire_y, owner)
        _wire(session, sheet, f"{comp.id}.SIG0", f"{jog}.W")
        _wire(session, sheet, f"{jog}.S", f"{down}.N")
        _wire(session, sheet, f"{down}.E", f"{sig0_term}.A")
        if pair:
            _wire(session, sheet, f"{sig0_term}.B", f"{module_comp.id}.{pair}")
    else:
        # 5069-IN010E-EN-P August 2023 p.9: a 2-wire transmitter's supply is not SA.
        # Supply + feeds the transmitter. Supply − lands on Input n−.
        plus = device.pin("PLUS")
        plus_y = comp_y + float(plus.y)
        stub_x = card_x - HOT_FROM_CARD - 18.0
        ext_p = _waypoint(session, sheet, stub_x, plus_y, owner)
        _wire(session, sheet, f"{ext_p}.E", f"{comp.id}.PLUS")
        pair = ""
        for item in module.power:
            if item.role == "signal_minus" and item.point == pin:
                pair = item.pin
                break
        ret_y = rung_y + 18.0
        ext_n = _waypoint(session, sheet, stub_x, ret_y, owner)
        if pair:
            ret_term = _terminal(
                session, sheet, card_x - 40.0, ret_y, owner, module_ref, pair, _term_no(module_comp, pair)
            )
            meta["signal_minus_terminal"] = ret_term
            _wire(session, sheet, f"{ext_n}.E", f"{ret_term}.A")
            _wire(session, sheet, f"{ret_term}.B", f"{module_comp.id}.{pair}")
        if not bus.get("ext_supply_note"):
            _note(session, sheet, stub_x - 44.0, plus_y - 8.0, "EXT SUPPLY", owner, width=40.0)
            bus["ext_supply_note"] = True
    term = _terminal(session, sheet, card_x - 20.0, rung_y, owner, module_ref, pin, _term_no(module_comp, pin))
    meta["terminal_ref"] = term
    _wire(session, sheet, f"{comp.id}.SIG", f"{term}.A")
    _wire(session, sheet, f"{term}.B", f"{module_comp.id}.{pin}")


def _draw_spare(
    session: Any,
    sheet: Any,
    *,
    module_comp: Any,
    pin: str,
    meta: dict[str, Any],
    rung_y: float,
    card_x: float,
    owner: str,
    field_left: bool,
    module_ref: str,
) -> None:
    if field_left:
        note_x = card_x - 78.0
        term_x = card_x - 20.0
    else:
        note_x = card_x + CARD_W + 20.0
        term_x = card_x + CARD_W + 4.0
    ref = _field_ref(session, sheet, str(meta.get("tag") or f"SPARE_{pin}"))
    comp = _place(
        session,
        sheet,
        _lib(session, "notes.spare"),
        note_x,
        rung_y - 5.0,
        ref=ref,
        properties={
            "io_owner": owner,
            "bom": "no",
            "text": "SPARE",
            "width": 28,
            "io_point": {"module": module_ref, "pin": pin},
            "io_description": "spare",
        },
    )
    meta["field_ref"] = comp.id
    meta["field"] = "spare"
    term = _terminal(session, sheet, term_x, rung_y, owner, module_ref, pin, _term_no(module_comp, pin))
    meta["terminal_ref"] = term
    if field_left:
        _wire(session, sheet, f"{term}.B", f"{module_comp.id}.{pin}")
    else:
        _wire(session, sheet, f"{module_comp.id}.{pin}", f"{term}.A")


def _build_hot_bus(session: Any, sheet: Any, taps: list[tuple[float, str]], owner: str, x: float) -> str | None:
    """Vertical +24 V (or 0 V) bus. A dot is drawn only where three wires meet."""

    if not taps:
        return None
    ordered = sorted(taps, key=lambda item: item[0])
    count = len(ordered)
    nodes: list[str] = []
    for index, (y, dest) in enumerate(ordered):
        # Top of a multi-rung bus gains the feed on W, so it is a tee.
        # The bottom rung has only the vertical from above and the tap. One rung is a straight feed.
        tee = count > 1 and index < count - 1
        node = _junction(session, sheet, x, y, owner) if tee else _waypoint(session, sheet, x, y, owner)
        _wire(session, sheet, f"{node}.E", dest)
        nodes.append(node)
    for upper, lower in zip(nodes, nodes[1:]):
        _wire(session, sheet, f"{upper}.S", f"{lower}.N")
    return nodes[0]


def _feed_hot(session: Any, sheet: Any, top: str, owner: str, use_fuses: bool) -> None:
    hot = sheet.rail_hot or "24VDC"
    # Fuse the +24 V feed. A sourcing input keeps 0 V on the left rail; that wire stays unfused.
    if use_fuses and not _positive(hot):
        use_fuses = False
    from panelcad.render.sheet import pin_world

    junction = sheet.component(top)
    device = session.catalog.get(junction.library_id)
    center_x, rung_y = pin_world(junction, device.pin("W"))
    # A second module sits to the right. Its hot feed uses a trunk above the cards
    # so the wire does not cut the first module.
    if center_x > 180.0:
        _feed_hot_overhead(session, sheet, top, owner, use_fuses, center_x)
        return
    if not use_fuses:
        _wire(session, sheet, hot, f"{top}.W")
        return
    try:
        library_id = _lib(session, "protection.fuse")
    except KeyError:
        library_id = "iec.protection.fuse"
    device = session.catalog.get(library_id)
    pin_y = 7.0
    # Sit the fuse on the feed rung so the wire stays horizontal.
    fuse_x = center_x - 36.0
    # The sheet id keeps a continuation fuse from taking the home sheet's ref.
    ref = _saved_ref(session, owner, "fuse-hot", sheet.id, "FU")
    fuse = _place(
        session,
        sheet,
        library_id,
        fuse_x,
        rung_y - pin_y,
        ref=ref,
        properties=_role(owner, "fuse-hot", sheet.id),
    )
    left = _pin(device, "left")
    right = _pin(device, "right")
    _wire(session, sheet, hot, f"{fuse.id}.{left.id}")
    _wire(session, sheet, f"{fuse.id}.{right.id}", f"{top}.W")
    fuse.rotation = 0


def _feed_hot_overhead(session: Any, sheet: Any, top: str, owner: str, use_fuses: bool, center_x: float) -> None:
    """24 V trunk above the cards, then a drop onto this module's hot bus."""

    hot = sheet.rail_hot or "24VDC"
    if use_fuses and not _positive(hot):
        use_fuses = False
    # Above the cards and above the field-device captions. The +V trunk is at
    # y=33 and jogs under this fuse. A corner: the feed comes in on W and drops on S.
    trunk_y = 28.0
    riser = _waypoint(session, sheet, center_x, trunk_y, owner)
    _wire(session, sheet, f"{riser}.S", f"{top}.N")
    if not use_fuses:
        _wire(session, sheet, hot, f"{riser}.W")
        return
    try:
        library_id = _lib(session, "protection.fuse")
    except KeyError:
        library_id = "iec.protection.fuse"
    device = session.catalog.get(library_id)
    from panelcad.core.ladder import rail_xs

    rail_left, _rail_right = rail_xs(sheet)
    ref = _saved_ref(session, owner, "fuse-overhead", sheet.id, "FU")
    fuse = _place(
        session,
        sheet,
        library_id,
        rail_left + 18.0,
        trunk_y - 7.0,
        ref=ref,
        properties=_role(owner, "fuse-overhead", sheet.id),
    )
    left = _pin(device, "left")
    right = _pin(device, "right")
    _wire(session, sheet, hot, f"{fuse.id}.{left.id}")
    _wire(session, sheet, f"{fuse.id}.{right.id}", f"{riser}.W")
    fuse.rotation = 0


def _rail_bottom(sheet: Any) -> float:
    from panelcad.render.titleblock import working_rect

    _wx, wy, _ww, wh = working_rect(sheet)
    return wy + wh - 2.0


def _under_y(module_comp: Any, sheet: Any) -> float:
    """Return-bar Y. A card further right sits lower, and the bar stays on the rail."""

    extra = 8.0 if float(module_comp.x) > 300.0 else 0.0
    y = float(module_comp.y) + float(module_comp.properties.get("card_h") or 40) + 12.0 + extra
    return min(y, _rail_bottom(sheet) - 1.0)


def _commons(
    session: Any,
    sheet: Any,
    module_comp: Any,
    module: IoModule,
    ret: str,
    *,
    visible: set[str],
    use_fuses: bool,
    tie_returns: bool = False,
) -> str | None:
    """Tie commons on a bus just outside the card, then one run under the card to the return rail.

    Signal-minus pins are not commons. 5069-IN010E p.9 connects 0−…7− to each other
    inside the module and does not bond them to the 24 V return.
    """

    from panelcad.render.sheet import pin_world

    pins = []
    for item in module.power:
        if item.role != "common":
            continue
        group_points = []
        for group in module.groups:
            if group.common == item.pin:
                group_points = list(group.points)
        if group_points and not any(point in visible for point in group_points):
            continue
        if item.pin not in {pin.id for pin in session.catalog.get(module_comp.library_id).pins}:
            continue
        pins.append(item)
    if not pins:
        return None
    device = session.catalog.get(module_comp.library_id)
    located = []
    for item in pins:
        px, py = pin_world(module_comp, device.pin(item.pin))
        located.append((item, px, py))
    card_left = float(module_comp.x)
    card_right = card_left + float(module_comp.properties.get("card_w") or 88.0)
    on_right = located[0][1] >= (card_left + card_right) / 2.0
    bus_x = card_right + 10.0 if on_right else card_left - 10.0
    owner = str(module_comp.id)
    rung: list[tuple[float, str]] = []
    toward = "W" if on_right else "E"
    located.sort(key=lambda item: item[2])
    for index, (item, _px, py) in enumerate(located):
        # The top common is a corner. Each lower common also ties the bus, so it is a tee.
        node = _junction if index > 0 else _waypoint
        junction = node(session, sheet, bus_x, py, owner)
        _wire(session, sheet, f"{module_comp.id}.{item.pin}", f"{junction}.{toward}")
        rung.append((py, junction))
    for (_y0, upper), (_y1, lower) in zip(rung, rung[1:]):
        _wire(session, sheet, f"{upper}.S", f"{lower}.N")
    under_y = _under_y(module_comp, sheet)
    # The return column lands on this corner. That makes three wires, so it is a dot.
    under_node = _junction if tie_returns else _waypoint
    under = under_node(session, sheet, bus_x, under_y, owner)
    _wire(session, sheet, f"{rung[-1][1]}.S", f"{under}.N")
    rail = ret or sheet.rail_return or "0V"
    _to_rail(
        session,
        sheet,
        f"{under}.E",
        rail,
        owner,
        use_fuses and _positive(rail),
        under_y,
        bus_x + 12.0,
        rail_on_right=True,
        role="fuse-return",
    )
    return under


def _to_rail(
    session: Any,
    sheet: Any,
    frm: str,
    rail: str,
    owner: str,
    fuse_it: bool,
    y: float,
    fuse_x: float,
    *,
    rail_on_right: bool = True,
    role: str = "fuse-rail",
) -> None:
    """Horizontal run to a rail. A fuse sits on +24 V only, with no corner dot."""

    if not fuse_it:
        _wire(session, sheet, frm, rail)
        return
    try:
        library_id = _lib(session, "protection.fuse")
    except KeyError:
        library_id = "iec.protection.fuse"
    device = session.catalog.get(library_id)
    left = _pin(device, "left")
    right = _pin(device, "right")
    ref = _saved_ref(session, owner, role, sheet.id, "FU")
    fuse = _place(
        session,
        sheet,
        library_id,
        fuse_x,
        y - float(left.y),
        ref=ref,
        properties=_role(owner, role, sheet.id),
    )
    if rail_on_right:
        _wire(session, sheet, frm, f"{fuse.id}.{left.id}")
        _wire(session, sheet, f"{fuse.id}.{right.id}", rail)
    else:
        _wire(session, sheet, rail, f"{fuse.id}.{left.id}")
        _wire(session, sheet, f"{fuse.id}.{right.id}", frm)
    fuse.rotation = 0


def _supply(session: Any, sheet: Any, module_comp: Any, module: IoModule, owner: str, use_fuses: bool, bus: dict[str, Any]) -> None:
    from panelcad.render.sheet import pin_world

    device = session.catalog.get(module_comp.library_id)
    plus = next((item for item in module.power if item.role == "supply_plus"), None)
    minus = next((item for item in module.power if item.role == "supply_minus"), None)
    hot = sheet.rail_hot or "24VDC"
    if plus is not None and _positive(hot):
        from panelcad.core.ladder import rail_xs

        px, _py = pin_world(module_comp, device.pin(plus.pin))
        rail_left, _rail_right = rail_xs(sheet)
        # A pushbutton on the first rung is drawn at y=44. Its tag and
        # description occupy about y=36–42. Keep this trunk above that text.
        trunk_y = 33.0
        drop = _waypoint(session, sheet, px, trunk_y, owner)
        _wire(session, sheet, f"{module_comp.id}.{plus.pin}", f"{drop}.S")
        # Beside the card, not over the first field device.
        fuse_x = max(rail_left + 48.0, px - 42.0)
        # The fuse symbol is 28 mm wide. Keep its right lead off the card.
        fuse_x = min(fuse_x, float(module_comp.x) - 32.0)
        fuse_x = max(fuse_x, rail_left + 16.0)
        source = hot
        overhead = next(
            (
                comp
                for comp in sheet.components
                if comp.properties.get("io_owner") == owner and comp.properties.get("io_role") == "fuse-overhead"
            ),
            None,
        )
        if overhead is not None:
            body = session.catalog.get(overhead.library_id)
            top = float(overhead.y)
            bottom = top + float(body.schematic_height)
            # That fuse sits on the y=28 load trunk and fills y 21–35, so a
            # straight +V run crosses the symbol. Pass under it, then rise.
            if top - 1.0 <= trunk_y <= bottom + 1.0:
                under_y = bottom + 1.8
                pass_x = float(overhead.x) + float(body.schematic_width) + 6.0
                corner = _waypoint(session, sheet, pass_x, under_y, owner)
                rise = _waypoint(session, sheet, pass_x, trunk_y, owner)
                _wire(session, sheet, hot, f"{corner}.W")
                _wire(session, sheet, f"{corner}.N", f"{rise}.S")
                source = f"{rise}.E"
        _to_rail(
            session,
            sheet,
            f"{drop}.W",
            source,
            owner,
            use_fuses and _positive(hot),
            trunk_y,
            fuse_x,
            rail_on_right=False,
            role="fuse-supply",
        )
    elif plus is not None:
        _wire(session, sheet, hot, f"{module_comp.id}.{plus.pin}")
    if minus is not None:
        _px, py = pin_world(module_comp, device.pin(minus.pin))
        under_y = _under_y(module_comp, sheet)
        drop = _waypoint(session, sheet, _px, under_y, owner)
        _wire(session, sheet, f"{module_comp.id}.{minus.pin}", f"{drop}.N")
        _wire(session, sheet, f"{drop}.E", sheet.rail_return or "0V")
    del bus



def _return_drops(
    session: Any,
    sheet: Any,
    module_comp: Any,
    returns: list[tuple[str, float]],
    common_under: str | None,
) -> None:
    """Collect prox returns between the sensor and the field terminal.

    Brown stays on the +24 V bus and blue on the 0 V spine, or the reverse when
    the sheet is sourcing. Each colour leaves its own side of that column, so
    the two power leads do not cross. A signal lead that meets the spine is
    drawn with a hop.
    """

    if not returns:
        return
    from panelcad.render.sheet import parse_pin_ref, pin_world

    owner = str(module_comp.id)
    card_x = float(module_comp.x)
    # The sensor's right pin is 42 mm left of the card. The terminal body starts
    # 30 mm left of the card. This column is the gap between them.
    spine_x = card_x - 36.0
    under_y = _under_y(module_comp, sheet)
    located: list[tuple[str, float, float]] = []
    for src, fallback_y in returns:
        sx, sy = spine_x - 20.0, float(fallback_y)
        try:
            cid, pid = parse_pin_ref(src)
            comp = sheet.component(cid)
            device = session.catalog.get(comp.library_id)
            sx, sy = pin_world(comp, device.pin(pid))
        except (KeyError, ValueError):
            pass
        located.append((src, float(sx), float(sy)))
    ordered = sorted(located, key=lambda item: item[2])
    nodes: list[str] = []
    for index, (src, _sx, sy) in enumerate(ordered):
        # The top drop is a corner. Each lower drop is a tee (up, tap, down).
        node = (
            _junction(session, sheet, spine_x, sy, owner)
            if index
            else _waypoint(session, sheet, spine_x, sy, owner)
        )
        _wire(session, sheet, src, f"{node}.W")
        nodes.append(node)
    for upper, lower in zip(nodes, nodes[1:]):
        _wire(session, sheet, f"{upper}.S", f"{lower}.N")
    under = _waypoint(session, sheet, spine_x, under_y, owner)
    _wire(session, sheet, f"{nodes[-1]}.S", f"{under}.N")
    if common_under:
        _wire(session, sheet, f"{under}.E", f"{common_under}.W")
    else:
        _wire(session, sheet, f"{under}.E", sheet.rail_return or "0V")



def _warn_field(module: IoModule, pin: str, field_key: str, library_id: str) -> str | None:
    allowed = FIELD_OK.get(module.point_type, {"spare"})
    if field_key in allowed or field_key in {"", "spare"}:
        return None
    return (
        f"{module.catalog or module.id} point {pin} keeps field device {library_id or field_key}. "
        f"That device is not the usual {module.point_type} symbol."
    )


def _draw_module_column(
    session: Any,
    sheet: Any,
    module_comp: Any,
    module: IoModule,
    points: list[IoPoint],
    *,
    column: int,
    columns: int,
    use_fuses: bool,
    warnings: list[str],
    field_left: bool,
    logical: Any | None = None,
    notes: bool = True,
    rack: bool = True,
    landings: bool = True,
    cont_on: str = "",
    cont_from: str = "",
) -> None:
    from panelcad.core.ladder import rail_xs

    store = logical if logical is not None else module_comp
    module_ref = str(store.id)
    left, _right = rail_xs(sheet)
    rows = [points]
    x = _card_x(left, columns, column, field_left)
    world = _layout_card(module_comp, module, x=x, rows=rows, field_left=field_left)
    if not landings:
        for item in module.power:
            if item.role in {"earth", "shield", "supply_plus", "supply_minus"}:
                module_comp.pin_overrides.pop(item.pin, None)
    owner = module_ref
    io_map = dict(store.properties.get("io") or {})
    bus: dict[str, Any] = {}
    for point in points:
        meta = dict(io_map.get(point.pin) or {})
        if meta.get("user_removed"):
            field_key = "spare"
            meta["field"] = "spare"
            meta["field_library"] = ""
            meta["field_ref"] = ""
            meta["tag"] = _spare_tag(module_ref, point)
        else:
            field_key = str(meta.get("field") or "spare").lower()
        library_id = str(meta.get("field_library") or "")
        warning = _warn_field(module, point.pin, field_key, library_id)
        if warning and library_id:
            warnings.append(warning)
            field_key = "user"
        elif warning:
            warnings.append(
                f"{module.catalog or module.id} point {point.pin} field '{field_key}' is not drawn; labelled SPARE."
            )
            field_key = "spare"
        rung_y = world[point.pin]
        if field_key == "user" and library_id:
            try:
                device = session.catalog.get(library_id)
                _pin(device, "left")
                _pin(device, "right")
            except (KeyError, StopIteration):
                field_key = "spare"
            else:
                meta["field_library"] = library_id
                _draw_series(
                    session,
                    sheet,
                    module_comp=module_comp,
                    pin=point.pin,
                    meta=meta,
                    field_key="pushbutton",
                    rung_y=rung_y,
                    card_x=x,
                    hot=sheet.rail_hot,
                    owner=owner,
                    bus=bus,
                    toward_card=field_left,
                    module_ref=module_ref,
                )
                io_map[point.pin] = meta
                continue
        if field_key == "prox_3wire":
            _draw_prox(
                session,
                sheet,
                module_comp=module_comp,
                pin=point.pin,
                meta=meta,
                rung_y=rung_y,
                card_x=x,
                owner=owner,
                bus=bus,
                module_ref=module_ref,
            )
        elif field_key in {"transmitter_2wire", "transmitter_4wire"}:
            # 2-wire is not a series load. 5069-IN010E p.9 feeds it from a
            # separate supply and lands supply − on Input n−.
            _draw_transmitter(
                session,
                sheet,
                module_comp=module_comp,
                module=module,
                pin=point.pin,
                meta=meta,
                rung_y=rung_y,
                card_x=x,
                owner=owner,
                bus=bus,
                four_wire=field_key == "transmitter_4wire",
                module_ref=module_ref,
            )
        elif field_key in _SERIES and field_key != "spare":
            _draw_series(
                session,
                sheet,
                module_comp=module_comp,
                pin=point.pin,
                meta=meta,
                field_key=field_key,
                rung_y=rung_y,
                card_x=x,
                hot=sheet.rail_hot,
                owner=owner,
                bus=bus,
                toward_card=field_left,
                module_ref=module_ref,
            )
        else:
            if field_key not in {"spare", ""}:
                warnings.append(
                    f"{module.catalog or module.id} point {point.pin} field '{field_key}' is not drawn; labelled SPARE."
                )
            meta["field"] = "spare"
            _draw_spare(
                session,
                sheet,
                module_comp=module_comp,
                pin=point.pin,
                meta=meta,
                rung_y=rung_y,
                card_x=x,
                owner=owner,
                field_left=field_left,
                module_ref=module_ref,
            )
        io_map[point.pin] = meta
    store.properties["io"] = io_map
    top = _build_hot_bus(session, sheet, list(bus.get("hot") or []), owner, _hot_bus_x(x))
    # Field loads still need the +24 V bus when the module also has a +V/LA+ pin.
    # That pin is the module supply, not the load supply.
    if top:
        _feed_hot(session, sheet, top, owner, use_fuses)
    visible = {point.pin for point in points}
    common_under = _commons(
        session,
        sheet,
        module_comp,
        module,
        sheet.rail_return or "0V",
        visible=visible,
        use_fuses=use_fuses,
        tie_returns=bool(bus.get("returns")),
    )
    if landings:
        _supply(session, sheet, module_comp, module, owner, use_fuses, bus)
        _earth(session, sheet, module_comp, module, owner)
        _shields(session, sheet, module_comp, module, owner)
    _return_drops(
        session,
        sheet,
        module_comp,
        list(bus.get("returns") or []),
        common_under,
    )
    if notes:
        _finish_notes(session, sheet, module_comp, module, owner)
    _cont_note(session, sheet, owner, cont_on=cont_on, cont_from=cont_from)
    if rack:
        _rack(session, store, sheet)
    store.properties["io_drawn"] = True
    for fuse in sheet.components:
        if fuse.properties.get("io_owner") == owner and "fuse" in fuse.library_id:
            fuse.rotation = 0


def _finish_notes(session: Any, sheet: Any, module_comp: Any, module: IoModule, owner: str) -> None:
    """Full maker note beside the card. Never clipped, and never drawn on the card body."""

    lines: list[str] = []
    if module.return_note:
        lines.append(module.return_note)
    nc = [item.terminal for item in module.unwired if item.role in {"nc", ""}]
    if nc:
        lines.append("Not wired: " + ", ".join(nc) + ".")
    if not lines:
        return
    text = " ".join(lines)
    from panelcad.core.ladder import rail_xs

    _rail_left, rail_right = rail_xs(sheet)
    card_w = float(module_comp.properties.get("card_w") or CARD_W)
    # Earth (FE) hangs to the right of the commons bus. Start the note below and
    # clear of that symbol (16 mm wide, placed bus_x + 8).
    note_y = float(module_comp.y)
    note_x = float(module_comp.x) + card_w + 28.0
    if any(item.role == "earth" for item in module.power):
        note_y = float(module_comp.y) + 52.0
        note_x = float(module_comp.x) + card_w + 44.0
    later = [
        float(comp.x)
        for comp in sheet.components
        if (comp.properties.get("io_module") or comp.properties.get("io_continuation"))
        and comp.id != module_comp.id
        and float(comp.x) > float(module_comp.x) + 20.0
    ]
    if later:
        width = min(later) - note_x - 6.0
    else:
        width = rail_right - note_x - 6.0
    width = max(36.0, min(200.0, width))
    _note(session, sheet, note_x, note_y, text, owner, width=width)


def _cont_note(session: Any, sheet: Any, owner: str, *, cont_on: str = "", cont_from: str = "") -> None:
    lines = []
    if cont_from:
        lines.append(f"CONT. FROM SHEET {cont_from}")
    if cont_on:
        lines.append(f"CONT. ON SHEET {cont_on}")
    if not lines:
        return
    from panelcad.core.ladder import rail_xs

    _rail_left, rail_right = rail_xs(sheet)
    # Inside the working area, above the module note that starts at the card.
    _note(session, sheet, rail_right - 120.0, 20.0, "\n".join(lines), owner, width=100.0)


def _earth(session: Any, sheet: Any, module_comp: Any, module: IoModule, owner: str) -> None:
    """SM 1221 FE (X11:1) lands on an earth symbol. It is not an open stub.

    The commons bus is 10 mm outside the card and starts at the first input.
    The FE pin is above that bus. The symbol is to the right of the bus so the
    lead does not cross it, and the symbol body does not cover the bus.
    """

    item = next((row for row in module.power if row.role == "earth"), None)
    if item is None or item.pin not in module_comp.pin_overrides:
        return
    from panelcad.render.sheet import pin_world

    device = session.catalog.get(module_comp.library_id)
    try:
        px, py = pin_world(module_comp, device.pin(item.pin))
    except KeyError:
        return
    card_right = float(module_comp.x) + float(module_comp.properties.get("card_w") or CARD_W)
    # Same x as _commons when the common pins are on the right.
    bus_x = card_right + 10.0
    # iec.connection.ground pin A is local (8, 0), the top of the 16 mm symbol.
    place_x = max(px + 4.0, bus_x + 8.0)
    ref = _saved_ref(session, owner, "earth", item.pin, "PE")
    _place(
        session,
        sheet,
        "iec.connection.ground",
        place_x,
        py,
        ref=ref,
        properties=_role(owner, "earth", item.pin, bom="no", hide_tag=True),
    )
    _wire(session, sheet, f"{module_comp.id}.{item.pin}", f"{ref}.A")


def _shields(session: Any, sheet: Any, module_comp: Any, module: IoModule, owner: str) -> None:
    """5069-IF8 terminals 16 and 17 are Shield (5069-IN010E-EN-P August 2023 p.9).

    They land on one earth symbol. They are not tied to the 24 V 0 V rail.
    """

    shields = [row for row in module.power if row.role == "shield"]
    if not shields:
        return
    card_h = float(module_comp.properties.get("card_h") or 40.0)
    card_w = float(module_comp.properties.get("card_w") or CARD_W)
    count = len(shields)
    for index, item in enumerate(shields):
        local_x = card_w * (index + 1) / (count + 1)
        module_comp.pin_overrides[item.pin] = PinOverride(
            x=local_x, y=card_h, side="bottom", label=str(item.terminal)
        )
    from panelcad.render.sheet import pin_world

    device = session.catalog.get(module_comp.library_id)
    located: list[tuple[Any, float, float]] = []
    for item in shields:
        try:
            px, py = pin_world(module_comp, device.pin(item.pin))
        except KeyError:
            continue
        located.append((item, px, py))
    if not located:
        return
    bus_y = located[0][2] + 8.0
    # The ground symbol hangs below the bus. A wire along the symbol's top edge
    # reads as a connection through the earth mark.
    drops: list[tuple[float, str]] = []
    for item, px, _py in located:
        drop = _waypoint(session, sheet, px, bus_y, owner)
        _wire(session, sheet, f"{module_comp.id}.{item.pin}", f"{drop}.N")
        drops.append((px, drop))
    if len(drops) == 1:
        gather = drops[0][1]
        gather_x = drops[0][0]
    else:
        gather_x = sum(px for px, _drop in drops) / len(drops)
        gather = _junction(session, sheet, gather_x, bus_y, owner)
        for px, drop in drops:
            if px < gather_x - 0.5:
                _wire(session, sheet, f"{drop}.E", f"{gather}.W")
            elif px > gather_x + 0.5:
                _wire(session, sheet, f"{drop}.W", f"{gather}.E")
    ref = _saved_ref(session, owner, "shield-earth", "shield", "PE")
    ground_y = bus_y + 4.0
    _place(
        session,
        sheet,
        "iec.connection.ground",
        gather_x - 8.0,
        ground_y,
        ref=ref,
        properties=_role(owner, "shield-earth", "shield", bom="no", hide_tag=True),
    )
    _wire(session, sheet, f"{gather}.S", f"{ref}.A")


def _zone_label(session: Any, sheet: Any, x: float, y: float) -> str:
    from panelcad.core.zones import sheet_number, zone_at

    number = sheet_number(session.project.sheets, sheet.id) or 1
    row, col, _index = zone_at(sheet, x, y)
    return f"{number}-{row}{col}"


def _foot_w(session: Any, comp: Any) -> float:
    from panelcad.core.layout import footprint_size
    from panelcad.library.panel import is_layout_ready, occupies_panel_layout

    device = session.catalog.get(comp.library_id)
    if occupies_panel_layout(device, comp) and not is_layout_ready(device, comp):
        return 0.0
    width, _height = footprint_size(device, comp)
    if width <= 0:
        return 0.0
    return width


def _rack(session: Any, module_comp: Any, io_sheet: Any) -> None:
    """Footprint on the rack. The layout copy does not carry io_module, so a later generate cannot draw a ladder on it."""

    props = {
        "catalog": str(module_comp.properties.get("catalog") or ""),
        "manufacturer": str(module_comp.properties.get("manufacturer") or ""),
        "part_number": str(module_comp.properties.get("part_number") or ""),
        "io_module_ref": str(module_comp.properties.get("io_module") or ""),
        "placeholder_footprint": bool(module_comp.properties.get("placeholder_footprint")),
    }
    panels = [sheet for sheet in session.project.sheets if sheet.kind == "panel_layout"]
    # A previous generate may have wrapped this module onto rack-2. place() only
    # rejects a duplicate on the sheet it is given, so search every rack sheet.
    existing = None
    panel = None
    for candidate in panels:
        existing = next((comp for comp in candidate.components if comp.id == module_comp.id), None)
        if existing is not None:
            panel = candidate
            break
    if existing is None:
        from panelcad.core.models import Component
        from panelcad.library.panel import is_layout_ready, occupies_panel_layout

        device = session.catalog.get(module_comp.library_id)
        ghost = Component(
            id=module_comp.id,
            library_id=module_comp.library_id,
            x=0.0,
            y=0.0,
            properties=dict(props),
        )
        if occupies_panel_layout(device, ghost) and not is_layout_ready(device, ghost):
            return
        panel = panels[0] if panels else None
        if panel is None:
            panel = session.add_sheet("rack", title="Rack", kind="panel_layout", make_active=False)
            panels = [panel]
        x = 40.0
        for other in panel.components:
            if not (other.properties.get("io_module_ref") or other.properties.get("io_module")):
                continue
            other_w = _foot_w(session, other)
            if other_w <= 0:
                continue
            x = max(x, float(other.x) + other_w + 12.0)
        existing = session.place(
            module_comp.library_id,
            ref=module_comp.id,
            x=x,
            y=40.0,
            sheet_id=panel.id,
            snap=False,
            properties=dict(props),
        )
    existing.properties.pop("io_module", None)
    for key, value in props.items():
        existing.properties[key] = value
    cx = float(module_comp.x) + float(module_comp.properties.get("card_w") or CARD_W) / 2.0
    cy = float(module_comp.y) + 20.0
    existing.properties["xref_io"] = _zone_label(session, io_sheet, cx, cy)
    module_comp.properties["xref_rack"] = _zone_label(
        session, panel, float(existing.x) + _foot_w(session, existing) / 2.0, float(existing.y) + 10.0
    )


def _reflow_racks(session: Any) -> None:
    """Wrap rack modules onto rows, and onto another rack sheet, so each one stays in the enclosure."""

    from panelcad.core.layout import footprint_size

    racks = [sheet for sheet in session.project.sheets if sheet.kind == "panel_layout"]
    if not racks:
        return
    modules: list[Any] = []
    for rack in racks:
        for comp in rack.components:
            if comp.properties.get("io_module_ref") or comp.properties.get("io_module"):
                modules.append(comp)
    if not modules:
        return

    def _home(comp: Any) -> int:
        for index, sheet in enumerate(session.project.sheets):
            if any(item.id == comp.id and item.properties.get("io_module") for item in sheet.components):
                return index
        return 999

    modules.sort(key=lambda comp: (_home(comp), comp.id))
    kept: list[Any] = []
    seen: set[str] = set()
    dropped: list[Any] = []
    for comp in modules:
        if comp.id in seen:
            dropped.append(comp)
            continue
        seen.add(comp.id)
        kept.append(comp)
    if dropped:
        drop_ids = {id(comp) for comp in dropped}
        for rack in racks:
            rack.components = [item for item in rack.components if id(item) not in drop_ids]
    modules = kept
    primary = racks[0]
    enc = primary.enclosure
    if enc is None:
        left, top, right, bottom = 28.0, 28.0, 270.0, 360.0
    else:
        left = float(enc.x) + 6.0
        top = float(enc.y) + 8.0
        right = float(enc.x) + float(enc.width_mm) - 4.0
        bottom = float(enc.y) + float(enc.height_mm) - 4.0
    pages: list[list[tuple[Any, float, float, float, float]]] = []
    page: list[tuple[Any, float, float, float, float]] = []
    x = left
    y = top
    row_h = 0.0
    for comp in modules:
        device = session.catalog.get(comp.library_id)
        from panelcad.library.panel import is_layout_ready, occupies_panel_layout

        if occupies_panel_layout(device, comp) and not is_layout_ready(device, comp):
            continue
        width, height = footprint_size(device, comp)
        if width <= 0 or height <= 0:
            continue
        if page and x + width > right:
            x = left
            y += row_h + 10.0
            row_h = 0.0
        if page and y + height > bottom:
            pages.append(page)
            page = []
            x = left
            y = top
            row_h = 0.0
        page.append((comp, x, y, width, height))
        x += width + 8.0
        row_h = max(row_h, height)
    if page:
        pages.append(page)
    sheets = [primary]
    extras = [sheet for sheet in racks if sheet is not primary]
    while len(sheets) < len(pages):
        if extras:
            sheets.append(extras.pop(0))
            continue
        number = len(sheets) + 1
        sid = f"rack-{number}"
        taken = {page.id for page in session.project.sheets}
        while sid in taken:
            number += 1
            sid = f"rack-{number}"
        sheets.append(session.add_sheet(sid, title=f"Rack {number}", kind="panel_layout", make_active=False))
    homes: dict[int, tuple[Any, Any]] = {}
    for sheet, group in zip(sheets, pages):
        for comp, px, py, width, height in group:
            for other in session.project.sheets:
                if comp in other.components and other is not sheet:
                    other.components = [item for item in other.components if item is not comp]
            if comp not in sheet.components:
                sheet.components.append(comp)
            comp.x = float(px)
            comp.y = float(py)
            for candidate in session.project.sheets:
                if candidate.kind != "io":
                    continue
                try:
                    card = candidate.component(comp.id)
                except KeyError:
                    continue
                if card.properties.get("io_module"):
                    homes[id(comp)] = (sheet, card, px, py, width, height)
                    break
    for comp_id, (sheet, card, px, py, width, height) in homes.items():
        del comp_id
        card.properties["xref_rack"] = _zone_label(session, sheet, px + width / 2.0, py + min(height, 30.0) / 2.0)


def _prepare_page(sheet: Any) -> None:
    """A2 landscape. A module past eight points continues on the next sheet instead of a custom page."""

    sheet.page.size = "A2"
    sheet.page.orientation = "landscape"
    sheet.page.width_mm = None
    sheet.page.height_mm = None
    sheet.drawing_style = "ladder"


def _chunks(points: list[IoPoint]) -> list[list[IoPoint]]:
    if not points:
        return [[]]
    return [points[index : index + COLUMNS] for index in range(0, len(points), COLUMNS)]


def _sheet_no(session: Any, sheet: Any) -> int:
    from panelcad.core.zones import sheet_number

    return sheet_number(session.project.sheets, sheet.id) or 1


def _continuation_sheet(
    session: Any, parent: Any, module: IoModule, home: Any, part: int, point_lo: int, point_hi: int
):
    marker = f"{parent.id}:{part}"
    legacy = f"io-cont:{marker}"
    title = module.catalog or module.id
    human = f"{parent.id} {title} points {point_lo}–{point_hi} (cont.)"
    for sheet in session.project.sheets:
        if str(getattr(sheet, "io_cont", "") or "") != marker and str(getattr(sheet, "description", "") or "") != legacy:
            continue
        sheet.components = []
        sheet.wires = []
        sheet.io_cont = marker
        sheet.description = human
        _prepare_page(sheet)
        sheet.rail_hot = home.rail_hot
        sheet.rail_return = home.rail_return
        return sheet
    base = _slug(f"io-{title}-p{part}")
    sheet_id = base
    taken = {page.id for page in session.project.sheets}
    n = 2
    while sheet_id in taken:
        sheet_id = f"{base}-{n}"
        n += 1
    sheet = session.add_sheet(sheet_id, title=f"I/O {title} ({part})", kind="io", make_active=False)
    sheet.io_cont = marker
    sheet.description = human
    _prepare_page(sheet)
    sheet.rail_hot = home.rail_hot
    sheet.rail_return = home.rail_return
    return sheet


def _place_continuation(session: Any, sheet: Any, parent: Any, module: IoModule, part: int):
    """Visual card only. It is not an io_module, so generate will not treat it as a second PLC."""

    del module
    ref = f"{parent.id}_{part}"
    if any(comp.id == ref for page in session.project.sheets for comp in page.components):
        ref = _unique_ref(session, f"{parent.id}{part}")
    return session.place(
        parent.library_id,
        ref=ref,
        x=80.0,
        y=CARD_Y,
        sheet_id=sheet.id,
        snap=False,
        properties={
            "io_continuation": parent.id,
            "shown_tag": parent.id,
            "bom": "no",
            "pin_labels": dict(parent.properties.get("pin_labels") or {}),
            "io_terminals": dict(parent.properties.get("io_terminals") or {}),
            "io_names": dict(parent.properties.get("io_names") or {}),
            "catalog": parent.properties.get("catalog") or "",
            "manufacturer": parent.properties.get("manufacturer") or "",
            "part_number": parent.properties.get("part_number") or "",
            "ladder_card": True,
            "verified": True,
            "source": parent.properties.get("source") or "",
        },
    )


def _end_xy(session: Any, sheet: Any, ref: str, near_y: float) -> tuple[float, float]:
    if "." not in str(ref):
        from panelcad.core.ladder import rail_point

        x, y, _side = rail_point(sheet, ref, near_y, sheet.rail_hot or "", sheet.rail_return or "")
        return float(x), float(y)
    from panelcad.render.sheet import parse_pin_ref, pin_world

    cid, pid = parse_pin_ref(ref)
    comp = sheet.component(cid)
    x, y = pin_world(comp, session.catalog.get(comp.library_id).pin(pid))
    return float(x), float(y)


def _wire_owner(sheet: Any, wire: Any) -> str:
    for end in (str(wire.frm), str(wire.to)):
        if "." not in end:
            continue
        try:
            comp = sheet.component(end.split(".", 1)[0])
        except KeyError:
            continue
        owner = str(comp.properties.get("io_owner") or "")
        if owner:
            return owner
    return ""


def _junction_at(sheet: Any, cx: float, cy: float) -> str | None:
    for comp in sheet.components:
        if "junction" not in comp.library_id:
            continue
        if abs(float(comp.x) + 3.0 - cx) < 1.2 and abs(float(comp.y) + 3.0 - cy) < 1.2:
            return str(comp.id)
    return None


def _is_rail_ref(ref: str) -> bool:
    return "." not in str(ref)


def _reaches_rail(sheet: Any, start: str, rail: str) -> bool:
    """True when ``start`` can walk to ``rail`` through corners and junctions."""

    seen: set[str] = set()
    stack = [start]
    while stack:
        token = stack.pop()
        if token in seen:
            continue
        seen.add(token)
        if token == rail:
            return True
        cid = token.split(".", 1)[0]
        try:
            comp = sheet.component(cid)
        except KeyError:
            comp = None
        conducts = comp is not None and (
            "junction" in comp.library_id or "waypoint" in comp.library_id
        )
        for wire in sheet.wires:
            ends = (str(wire.frm), str(wire.to))
            if token in ends:
                stack.append(ends[0] if ends[1] == token else ends[1])
            elif conducts and any(end.split(".", 1)[0] == cid for end in ends):
                for end in ends:
                    if end.split(".", 1)[0] == cid:
                        stack.append(end)
    return False


def _drop_parallel_rail(sheet: Any, node: str) -> None:
    """Drop a junction-to-rail stub when another pin of that dot already reaches the rail.

    The left module's return and the next module's common drop are the same
    net. Keeping both rail runs draws a closed loop beside the rail.
    """

    direct = []
    for wire in sheet.wires:
        ends = (str(wire.frm), str(wire.to))
        if not any(end.split(".", 1)[0] == node for end in ends):
            continue
        if _is_rail_ref(ends[0]) or _is_rail_ref(ends[1]):
            direct.append(wire)
    for wire in direct:
        ends = (str(wire.frm), str(wire.to))
        rail = ends[0] if _is_rail_ref(ends[0]) else ends[1]
        pin = ends[1] if _is_rail_ref(ends[0]) else ends[0]
        others = [f"{node}.{side}" for side in "WENS" if f"{node}.{side}" != pin]
        sheet.wires = [item for item in sheet.wires if item is not wire]
        if any(_reaches_rail(sheet, other, rail) for other in others):
            continue
        sheet.wires.append(wire)


def _split_straight(session: Any, sheet: Any, wire: Any, cx: float, cy: float, node: str) -> bool:
    """Replace one straight run with two runs that meet at ``node``."""

    fx, fy = _end_xy(session, sheet, str(wire.frm), cy)
    tx, ty = _end_xy(session, sheet, str(wire.to), cy)
    number = str(wire.number or "").strip() or None
    horizontal = abs(fy - ty) <= 0.6 and abs(fy - cy) <= 0.8
    vertical = abs(fx - tx) <= 0.6 and abs(fx - cx) <= 0.8
    if horizontal == vertical:
        return False
    sheet.wires = [item for item in sheet.wires if item is not wire]
    if horizontal:
        left, right = (str(wire.frm), str(wire.to)) if fx <= tx else (str(wire.to), str(wire.frm))
        _wire(session, sheet, left, f"{node}.W", number=number)
        _wire(session, sheet, f"{node}.E", right, number=number)
    else:
        top, bottom = (str(wire.frm), str(wire.to)) if fy <= ty else (str(wire.to), str(wire.frm))
        _wire(session, sheet, top, f"{node}.N", number=number)
        _wire(session, sheet, f"{node}.S", bottom, number=number)
    return True


def _bond_same_net_crossings(session: Any, sheet: Any) -> None:
    """A return that crosses another run of the same number gets a junction dot.

    The left module's 0 V bar runs to the right rail and crosses the next
    module's common drop. Same-net wires connect; the dot shows that.
    """

    from panelcad.render.hops import _proper_cross, _same_net
    from panelcad.render.wiring import route_sheet_wires

    for _guard in range(8):
        routes = route_sheet_wires(sheet, session.catalog)
        found: tuple[Any, Any, float, float] | None = None
        wires = [wire for wire in sheet.wires if len(routes.get(wire.id) or []) == 2]
        for index, left in enumerate(wires):
            left_pts = routes[left.id]
            for right in wires[index + 1 :]:
                if not _same_net(left, right):
                    continue
                right_pts = routes[right.id]
                cross = _proper_cross(left_pts[0], left_pts[1], right_pts[0], right_pts[1])
                if cross is None:
                    continue
                found = (left, right, float(cross[0]), float(cross[1]))
                break
            if found is not None:
                break
        if found is None:
            return
        left, right, cx, cy = found
        if _junction_at(sheet, cx, cy) is not None:
            return
        owners = [item for item in (_wire_owner(sheet, left), _wire_owner(sheet, right)) if item]
        owner = min(owners) if owners else "IO"
        node = _junction(session, sheet, cx, cy, owner)
        if not _split_straight(session, sheet, left, cx, cy, node):
            return
        if not _split_straight(session, sheet, right, cx, cy, node):
            return
        _drop_parallel_rail(sheet, node)


def generate_sheets(session: Any, module_refs: list[str] | None = None, *, fuses: bool | None = None) -> dict[str, Any]:
    use_fuses = _uses_fuses(session, fuses)
    pending = _detach_noted_partners(session)
    wanted = {str(item).strip() for item in (module_refs or []) if str(item).strip()}
    if wanted and pending:
        wanted |= set(pending)
    targets: list[tuple[Any, Any]] = []
    for sheet in session.project.sheets:
        # Rack footprints use io_module_ref. Only a generated I/O ladder sheet is drawn.
        if not _is_io_ladder(sheet):
            continue
        if _is_continuation(sheet):
            continue
        for comp in sheet.components:
            if not comp.properties.get("io_module") or comp.properties.get("io_continuation"):
                continue
            if wanted and comp.id not in wanted and str(comp.properties.get("io_module")) not in wanted:
                continue
            targets.append((sheet, comp))
    if not targets:
        raise ValueError("no I/O module to draw; call io_add_module first")
    pull_io_sheet_edits(session.project, session.catalog)
    warnings: list[str] = []
    sheets: list[str] = []
    by_sheet: dict[str, list[Any]] = {}
    for sheet, comp in targets:
        by_sheet.setdefault(sheet.id, []).append(comp)
    for sheet_id, comps in by_sheet.items():
        sheet = session.project.sheet(sheet_id)
        point_counts = []
        for comp in comps:
            module = find_module(session.catalog, str(comp.properties.get("io_module") or ""))
            point_counts.append(len(module.points))
        if any(count > COLUMNS for count in point_counts) and len(comps) > 1:
            raise ValueError("a module with more than 8 points stays on its own sheet")
        _prepare_page(sheet)
        memories = {}
        for comp in comps:
            held = pending.pop(comp.id, None)
            memories[comp.id] = held if held is not None else _capture_redraw(session, comp.id)
        for comp in comps:
            _clear_owned(session, sheet, comp.id)
        columns = 1 if any(count > COLUMNS for count in point_counts) else len(comps)
        # Note width is the gap to the next card. Set every card's final x
        # before the first note is written, or the first generate still sees
        # the partner at the add-module default and the second does not.
        from panelcad.core.ladder import rail_xs

        rail_left, _rail_right = rail_xs(sheet)
        for index, comp in enumerate(comps):
            module = find_module(session.catalog, str(comp.properties.get("io_module") or ""))
            polarity = str(comp.properties.get("polarity") or "sink")
            comp.x = _card_x(rail_left, columns, index, _field_on_left(module, polarity))
        extra_ids: list[str] = []
        for index, comp in enumerate(comps):
            session._io_redraw = memories[comp.id]
            try:
                module = find_module(session.catalog, str(comp.properties.get("io_module") or ""))
                polarity = str(comp.properties.get("polarity") or "sink")
                field_left = _field_on_left(module, polarity)
                sheet.rail_hot, sheet.rail_return = _rail_pair(session, module, polarity)
                chunks = _chunks(list(module.points))
                cont_sheets = []
                for part, chunk in enumerate(chunks[1:], start=2):
                    lo = (part - 1) * COLUMNS
                    hi = lo + len(chunk) - 1
                    cont_sheets.append(_continuation_sheet(session, comp, module, sheet, part, lo, hi))
                cont_on = str(_sheet_no(session, cont_sheets[0])) if cont_sheets else ""
                _draw_module_column(
                    session,
                    sheet,
                    comp,
                    module,
                    chunks[0],
                    column=index,
                    columns=columns,
                    use_fuses=use_fuses,
                    warnings=warnings,
                    field_left=field_left,
                    logical=comp,
                    notes=True,
                    rack=True,
                    landings=True,
                    cont_on=cont_on,
                )
                previous = sheet
                for offset, (chunk, cont) in enumerate(zip(chunks[1:], cont_sheets)):
                    part = offset + 2
                    visual = _place_continuation(session, cont, comp, module, part)
                    nxt = cont_sheets[offset + 1] if offset + 1 < len(cont_sheets) else None
                    _draw_module_column(
                        session,
                        cont,
                        visual,
                        module,
                        chunk,
                        column=0,
                        columns=1,
                        use_fuses=use_fuses,
                        warnings=warnings,
                        field_left=field_left,
                        logical=comp,
                        notes=False,
                        rack=False,
                        landings=False,
                        cont_on=str(_sheet_no(session, nxt)) if nxt else "",
                        cont_from=str(_sheet_no(session, previous)),
                    )
                    previous = cont
                    if cont.id not in extra_ids:
                        extra_ids.append(cont.id)
            finally:
                session._io_redraw = None
        merged = _Redraw()
        for memory in memories.values():
            merged.refs.update(memory.refs)
            merged.numbers.update(memory.numbers)
        session._io_redraw = merged
        try:
            _bond_same_net_crossings(session, sheet)
            for extra in extra_ids:
                _bond_same_net_crossings(session, session.project.sheet(extra))
        finally:
            session._io_redraw = None
        if sheet_id not in sheets:
            sheets.append(sheet_id)
        for extra in extra_ids:
            if extra not in sheets:
                sheets.append(extra)
    _reflow_racks(session)
    return {"sheets": sheets, "modules": [comp.id for _sheet, comp in targets], "warnings": warnings, "fuses": use_fuses}


def import_points_csv(session: Any, module_ref: str, path: Path | str) -> dict[str, Any]:
    """Assign points from a CSV with columns pin, tag, description, field."""

    raw = Path(path).read_text(encoding="utf-8-sig")
    reader = csv.DictReader(raw.splitlines())
    updated: list[str] = []
    for row in reader:
        if not row:
            continue
        lowered = {(key or "").strip().lower(): (value or "").strip() for key, value in row.items()}
        pin = lowered.get("pin") or lowered.get("address") or ""
        if not pin:
            continue
        assign_point(
            session,
            module_ref,
            pin,
            tag=lowered.get("tag") or None,
            description=lowered.get("description") or None,
            field=lowered.get("field") or None,
        )
        updated.append(pin)
    return {"updated": updated}
