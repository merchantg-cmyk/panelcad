"""Panel and door placement for Circuit Builder inserts.

New in-cabinet devices go onto DIN rails (power on top, control below,
terminals at the bottom) with wire duct between rows. A second circuit uses
free rail space, then a new rail, and reports when the enclosure is too small.
Catalog footprints and accessory SKUs are used only when the verified parts
library already defines them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from panelcad.core.layout import (
    DOOR_HEADING_MM,
    DOOR_MARGIN_MM,
    DOOR_PITCH_Y_MM,
    door_row_pitch,
    MODULE_GAP_MM,
    RAIL_CLEAR_MM,
    RAIL_HEIGHT_MM,
    footprint_size,
    heading_rect,
    legend_plate_rect,
    nearest_rail,
    pack_field,
    place_on_rail,
    rail_height,
    rects_overlap,
    tray_aabb,
)
from panelcad.core.models import Component, Rail, WireTray
from panelcad.library.panel import effective_footprint, is_incabinet

DUCT_MM = 22.0
DUCT_GAP_MM = 16.0

CONTACTOR_ROLES = frozenset({"fwd", "rev", "main", "star", "delta", "contactor", "bypass"})
POWER_RANK = {
    "disconnect": 0,
    "breaker": 0,
    "contactor": 10,
    "fwd": 10,
    "main": 10,
    "bypass": 40,
    "rev": 30,
    "delta": 30,
    "star": 50,
    "overload": 80,
    "softstarter": 90,
    "drive": 90,
}
CONTROL_RANK = {
    "timer": 0,
    "relay": 10,
    "transformer": 20,
    "psu": 21,
    "plc": 30,
}
DOOR_ORDER = {
    "estop": 0,
    "stop": 10,
    "start": 20,
    "fwd_pb": 30,
    "rev_pb": 40,
    "hoa": 50,
    "selector": 60,
    "pilot": 70,
    "pilot_run": 71,
    "pilot_fault": 72,
    "pilot_fwd": 73,
    "pilot_rev": 74,
}
DOOR_LEGEND = {
    "estop": "E-STOP",
    "stop": "STOP",
    "start": "START",
    "fwd_pb": "FORWARD",
    "rev_pb": "REVERSE",
    "hoa": "HAND-OFF-AUTO",
    "selector": "RUN",
    "pilot": "PILOT",
    "pilot_run": "RUN",
    "pilot_fault": "FAULT",
    "pilot_fwd": "FORWARD",
    "pilot_rev": "REVERSE",
}


@dataclass
class _Piece:
    ref: str
    library_id: str
    props: dict[str, Any]
    band: str
    group: str
    order: float
    din: bool = True
    accessory: str = ""
    scale: float = 1.0


@dataclass
class _Chunk:
    pieces: list[_Piece] = field(default_factory=list)
    band: str = "power"
    order: int = 0

    @property
    def group(self) -> str:
        return self.pieces[0].group if self.pieces else ""


def pack_circuit_layouts(
    session: Any,
    parts: list[Any],
    *,
    kind: str,
    place_layout: bool = True,
) -> dict[str, Any]:
    """Add this circuit to the panel and door. Existing layout parts stay where they are."""

    from panelcad.core.circuit_builder import DOOR_ROLES, EXTRA_KINDS

    if not place_layout:
        return {
            "sheet": None,
            "door": None,
            "din": 0,
            "field": 0,
            "door_parts": 0,
            "warnings": [],
            "placed": False,
            "accessories": [],
        }
    warnings: list[str] = []
    panel = _append_panel(session, parts, warnings, kind=kind, extra_kind=kind in EXTRA_KINDS)
    door = _append_door(session, parts, warnings, door_roles=DOOR_ROLES, circuit_group=_circuit_group(parts))
    result = {
        "sheet": panel["sheet"],
        "door": door.get("sheet"),
        "din": panel["din"],
        "field": panel["field"],
        "door_parts": door.get("added", 0),
        "warnings": warnings,
        "placed": True,
        "accessories": panel.get("accessories", []),
        "preexisting": bool(panel.get("preexisting")),
    }
    if warnings:
        result["resize"] = "recommend_enclosure"
    return result


def _append_panel(
    session: Any,
    parts: list[Any],
    warnings: list[str],
    *,
    kind: str,
    extra_kind: bool,
) -> dict[str, Any]:
    from panelcad.core.circuit_builder import _ensure_kind_sheet, _layout_ghost
    from panelcad.core.layout import default_enclosure_and_rails

    dest_id = _ensure_kind_sheet(session, "panel_layout", "enclosure", "Enclosure")
    dest = session.sheet(dest_id)
    preexisting = bool(dest.components)
    if dest.enclosure is None or not dest.rails:
        enc, rails = default_enclosure_and_rails(dest.page)
        if dest.enclosure is None:
            dest.enclosure = enc
        if not dest.rails:
            dest.rails = rails

    already = {comp.id for comp in dest.components}
    occupied = _occupied(session, dest)
    pieces = _panel_pieces(session, parts, already, _layout_ghost, sheet_id=dest_id, warnings=warnings)
    if extra_kind:
        pieces.extend(_terminal_pieces(session, parts, already, kind))
    _stamp_catalog_accessories(session, parts, pieces)
    # A sheet that already has a draw scale keeps new parts at that scale.
    # Scale 1 on a 1:2 enclosure draws catalog millimetres and walks off the page.
    sheet_scale = _sheet_scale(dest)
    for piece in pieces:
        piece.scale = sheet_scale

    din_chunks = _chunks([p for p in pieces if p.din and p.band in {"power", "control", "terminal"}])
    field_pieces = [p for p in pieces if not p.din]
    din = 0
    field = 0
    accessories: list[str] = []
    power_bottom = _content_bottom(dest, occupied)
    min_y = float(dest.enclosure.y) if dest.enclosure is not None else 0.0

    for chunk in din_chunks:
        if chunk.band == "control":
            floor = max(min_y, power_bottom)
        elif chunk.band == "terminal":
            floor = max(min_y, _content_bottom(dest, occupied))
        else:
            floor = min_y
        placed = _place_chunk(
            session,
            dest,
            chunk,
            occupied,
            warnings,
            min_rail_y=floor,
            keep_inside=preexisting,
        )
        if chunk.band == "power":
            power_bottom = max(power_bottom, _content_bottom(dest, occupied))
        din += placed
        for piece in chunk.pieces:
            if piece.accessory:
                accessories.append(piece.ref)

    if field_pieces:
        field += _place_field(
            session,
            dest,
            field_pieces,
            occupied,
            warnings,
            keep_inside=preexisting,
        )

    return {
        "sheet": dest_id,
        "din": din,
        "field": field,
        "accessories": accessories,
        "preexisting": preexisting,
    }


def _append_door(
    session: Any,
    parts: list[Any],
    warnings: list[str],
    *,
    door_roles: frozenset[str],
    circuit_group: str,
) -> dict[str, Any]:
    from panelcad.core.circuit_builder import _ensure_kind_sheet, _layout_ghost

    from panelcad.library.panel import is_layout_ready, layout_ready_error, occupies_panel_layout

    ops = [part for part in parts if part.role in door_roles and part.ref and not part.skip_layout]
    if not ops:
        return {"sheet": None, "added": 0}
    kept: list[Any] = []
    for part in ops:
        device = session.catalog.get(part.library_id)
        ghost = _layout_ghost(session, part)
        if occupies_panel_layout(device, ghost) and not is_layout_ready(device, ghost):
            warnings.append(layout_ready_error(part.ref, device, ghost))
            continue
        kept.append(part)
    ops = kept
    if not ops:
        return {"sheet": None, "added": 0}
    dest_id = _ensure_kind_sheet(session, "door_layout", "door", "Door")
    ops.sort(key=lambda part: (0 if part.role == "estop" else 1, DOOR_ORDER.get(part.role, 80), part.ref))
    dest = session.sheet(dest_id)
    _mirror_panel_door(session, dest)
    already = {comp.id for comp in dest.components}
    keep_inside = bool(already)
    occupied = _door_occupied(session, dest)
    box = _door_box(dest)
    sheet_scale = _sheet_scale(dest)
    existing_inserts = []
    for comp in dest.components:
        try:
            existing_inserts.append(int(comp.properties.get("layout_insert") or 0))
        except (TypeError, ValueError):
            continue
    insert_n = (max(existing_inserts) if existing_inserts else 0) + 1
    slots = _door_slots(session, ops, circuit_group, ghost_for=_layout_ghost, insert_n=insert_n)
    if abs(sheet_scale - 1.0) > 1e-6:
        slots = [
            (part, x * sheet_scale, y * sheet_scale, w * sheet_scale, h * sheet_scale, props)
            for part, x, y, w, h, props in slots
        ]
    slots = _seat_door_slots(slots, occupied, box, warnings)
    added = 0
    for part, x, y, w, h, props in slots:
        if part.ref in already:
            continue
        plate = legend_plate_rect(x, y, w, h)
        on_door = _inside(x, y, w, h, box) and _inside(plate[0], plate[1], plate[2], plate[3], box)
        if not on_door:
            warnings.append(f"{part.ref} does not fit in the free backplate on the door")
            if keep_inside:
                continue
        dest.components.append(
            Component(
                id=part.ref,
                library_id=part.library_id,
                x=x,
                y=y,
                scale=sheet_scale,
                properties=props,
            )
        )
        occupied.append((x, y, w, h))
        already.add(part.ref)
        added += 1
    return {"sheet": dest_id, "added": added}


def _mirror_panel_door(session: Any, dest: Any) -> None:
    """Give an empty door the panel's box, drawn to fit the page.

    Operators seated after this are in that outline, and cutout checks read
    true millimetres. A door that already has parts is left alone: copying
    the box would keep those parts at their old sheet coordinates, divide
    them by the new scale, and hide the missing-outline warning.
    """

    enc = getattr(dest, "enclosure", None)
    if enc is not None and float(enc.width_mm) > 40.0 and float(enc.height_mm) > 40.0:
        return
    if getattr(dest, "components", None):
        return
    panel = next(
        (
            sheet
            for sheet in session.project.sheets
            if sheet.kind == "panel_layout" and sheet.enclosure is not None
        ),
        None,
    )
    if panel is None:
        return
    src = panel.enclosure
    if float(src.width_mm) <= 40.0 or float(src.height_mm) <= 40.0:
        return
    from panelcad.core.layout import fit_enclosure_scale
    from panelcad.core.models import Enclosure
    from panelcad.render.titleblock import working_rect

    fx, fy, fw, fh = working_rect(dest)
    scale = fit_enclosure_scale(float(src.width_mm), float(src.height_mm), fw, fh)
    drawn_w = float(src.width_mm) * scale
    drawn_h = float(src.height_mm) * scale
    dest.enclosure = Enclosure(
        x=fx + max(0.0, (fw - drawn_w) / 2.0),
        y=fy + max(0.0, (fh - drawn_h) / 2.0),
        width_mm=float(src.width_mm),
        height_mm=float(src.height_mm),
        depth_mm=float(src.depth_mm or 0.0),
        draw_scale=scale,
        name=src.name,
        nema_type=src.nema_type,
        material=src.material,
        catalog=src.catalog,
        manufacturer=src.manufacturer,
        mount=src.mount,
    )


def _door_box(dest: Any) -> tuple[float, float, float, float]:
    """Sheet rectangle the operators must stay inside.

    A sized door stores true millimetres on the enclosure and a draw scale.
    The old box used those true millimetres, so a second insert was centered
    in a box taller than the page.
    """

    from panelcad.render.titleblock import working_rect

    fx, fy, fw, fh = working_rect(dest)
    frame = (fx, fy, fx + fw, fy + fh)
    enc = dest.enclosure
    if enc is not None and float(enc.width_mm) > 40 and float(enc.height_mm) > 40:
        return _intersect_box(_plate_box(dest) or frame, frame)
    page_w, page_h = dest.page.dimensions()
    page = (12.0, 12.0, page_w - 12.0, page_h - 60.0)
    return _intersect_box(page, frame)


def _door_slots(
    session: Any,
    ops: list[Any],
    circuit_group: str,
    *,
    ghost_for: Any,
    insert_n: int = 1,
) -> list[tuple[Any, float, float, float, float, dict[str, Any]]]:
    """Local positions: shared E-stop on its own row, then one row per motor."""

    groups: list[tuple[str, list[Any]]] = []
    estop = [part for part in ops if part.role == "estop"]
    rest = [part for part in ops if part.role != "estop"]
    if estop:
        groups.append(("estop", estop))
    if rest:
        groups.append((circuit_group, rest))
    y = DOOR_MARGIN_MM
    slots: list[tuple[Any, float, float, float, float, dict[str, Any]]] = []
    order = 0
    for key, members in groups:
        sized: list[tuple[Any, float, float, Any]] = []
        for part in members:
            device = session.catalog.get(part.library_id)
            ghost = ghost_for(session, part)
            from panelcad.library.panel import is_layout_ready, occupies_panel_layout

            if occupies_panel_layout(device, ghost) and not is_layout_ready(device, ghost):
                continue
            w, h = footprint_size(device, ghost)
            if w <= 0 or h <= 0:
                continue
            sized.append((part, w, h, ghost))
        if not sized:
            continue
        pitch_x = door_row_pitch([(w, h, _legend_for(part)) for part, w, h, _ghost in sized])
        if key != "estop":
            y += DOOR_HEADING_MM
        for index, (part, w, h, ghost) in enumerate(sized):
            col = index % 4
            row = index // 4
            x = DOOR_MARGIN_MM + col * pitch_x
            yy = y + row * DOOR_PITCH_Y_MM
            legend = _legend_for(part)
            props = dict(ghost.properties)
            props["role"] = part.role
            props["legend"] = legend
            props["function"] = legend
            props["layout_order"] = order
            props["layout_insert"] = insert_n
            props["layout_band"] = "door"
            props["layout_group"] = key
            if key != "estop" and index == 0:
                props["layout_heading"] = f"Motor {key}"
            slots.append((part, x, yy, w, h, props))
            order += 1
        rows = (len(sized) + 3) // 4
        y += max(rows, 1) * DOOR_PITCH_Y_MM
    return slots


def _slot_ink(slots: list[tuple[Any, float, float, float, float, dict[str, Any]]]) -> list[tuple[float, float, float, float]]:
    ink: list[tuple[float, float, float, float]] = []
    for _part, x, y, w, h, props in slots:
        ink.append((x, y, w, h))
        ink.append(legend_plate_rect(x, y, w, h, str(props.get("legend") or "")))
        heading = str(props.get("layout_heading") or "")
        if heading:
            ink.append(heading_rect(x, y, w, h, heading))
    return ink


def _seat_door_slots(
    slots: list[tuple[Any, float, float, float, float, dict[str, Any]]],
    occupied: list[tuple[float, float, float, float]],
    box: tuple[float, float, float, float],
    warnings: list[str],
) -> list[tuple[Any, float, float, float, float, dict[str, Any]]]:
    if not slots:
        return slots
    if not occupied:
        return _center_slots(slots, box)
    # Center in the drawn door first. A y-only shift of the local coordinates
    # leaves the row at x≈0, outside a door that sits further across the sheet.
    base = _center_slots(slots, box)
    left, top, right, bottom = box
    min_y = min(y for _p, _x, y, _w, _h, _props in base)
    margin = min(DOOR_MARGIN_MM, max(4.0, (bottom - top) * 0.08))
    for extra in range(0, 900, 4):
        delta = (top + margin + extra) - min_y
        trial = [(part, x, y + delta, w, h, props) for part, x, y, w, h, props in base]
        ink = _slot_ink(trial)
        if any(not _inside(x, y, w, h, (left, top, right, bottom)) for x, y, w, h in ink):
            continue
        if _hits_any(ink, occupied):
            continue
        return trial
    warnings.append("door operators do not fit inside the door outline")
    return []


def _door_occupied(session: Any, dest: Any) -> list[tuple[float, float, float, float]]:
    rects = _occupied(session, dest)
    for comp in dest.components:
        device = session.catalog.get(comp.library_id)
        w, h = footprint_size(device, comp)
        if w <= 0 or h <= 0:
            continue
        legend = str(comp.properties.get("legend") or "").strip()
        if legend:
            rects.append(legend_plate_rect(comp.x, comp.y, w, h, legend))
        heading = str(comp.properties.get("layout_heading") or "").strip()
        if heading:
            rects.append(heading_rect(comp.x, comp.y, w, h, heading))
    return rects


def _center_slots(
    slots: list[tuple[Any, float, float, float, float, dict[str, Any]]],
    box: tuple[float, float, float, float],
) -> list[tuple[Any, float, float, float, float, dict[str, Any]]]:
    if not slots:
        return slots
    left, top, right, bottom = box
    min_x = min_y = 1e9
    max_x = max_y = -1e9
    for _part, x, y, w, h, props in slots:
        min_x = min(min_x, x)
        min_y = min(min_y, y - (DOOR_HEADING_MM if props.get("layout_heading") else 0.0))
        max_x = max(max_x, x + w)
        max_y = max(max_y, y + h)
        px, py, pw, ph = legend_plate_rect(x, y, w, h, str(props.get("legend") or ""))
        min_x = min(min_x, px)
        max_x = max(max_x, px + pw)
        max_y = max(max_y, py + ph)
        if props.get("layout_heading"):
            hx, hy, hw, hh = heading_rect(x, y, w, h, str(props["layout_heading"]))
            min_x = min(min_x, hx)
            min_y = min(min_y, hy)
            max_x = max(max_x, hx + hw)
            max_y = max(max_y, hy + hh)
    span_w = max_x - min_x
    span_h = max_y - min_y
    width = right - left
    height = bottom - top
    ox = left + (width - span_w) / 2.0 - min_x
    oy = top + (height - span_h) / 2.0 - min_y
    margin = DOOR_MARGIN_MM
    if min_x + ox < left + margin:
        ox = left + margin - min_x
    if min_y + oy < top + margin:
        oy = top + margin - min_y
    if max_x + ox > right - margin and span_w + 2 * margin <= width:
        ox = right - margin - max_x
    if max_y + oy > bottom - margin and span_h + 2 * margin <= height:
        oy = bottom - margin - max_y
    return [(part, x + ox, y + oy, w, h, props) for part, x, y, w, h, props in slots]


def _legend_for(part: Any) -> str:
    props = part.properties or {}
    explicit = str(props.get("legend") or props.get("function") or "").strip()
    if explicit and explicit.lower() not in {"on-delay", "no", "nc"}:
        return explicit
    return DOOR_LEGEND.get(part.role, part.role.replace("_", " ").upper())


def _circuit_group(parts: list[Any]) -> str:
    """One layout group per motor so the overload sits with that motor's contactors."""

    motor = next((part for part in parts if part.role == "motor" and part.ref), None)
    if motor is not None:
        return str(motor.ref)
    named = next(
        (
            part
            for part in parts
            if part.ref and not part.family and part.role not in {"psu", "transformer", "estop"}
        ),
        None,
    )
    return str(named.ref) if named is not None else "circuit"


def _panel_pieces(
    session: Any,
    parts: list[Any],
    already: set[str],
    ghost_for: Any,
    sheet_id: str = "",
    warnings: list[str] | None = None,
) -> list[_Piece]:
    group = _circuit_group(parts)
    pieces: list[_Piece] = []
    from panelcad.core.circuit_builder import _schematic_component

    for part in parts:
        if not part.ref or part.ref in already:
            continue
        # Schematic contacts (aux, OL 95-96, soft-starter relay outputs) are not parts.
        if part.skip_layout:
            continue
        device = session.catalog.get(part.library_id)
        ghost = ghost_for(session, part)
        _apply_draw_scale(session, ghost)
        from panelcad.library.panel import is_child_contact

        if is_child_contact(device, ghost) or not is_incabinet(device, ghost):
            continue
        from panelcad.library.panel import is_layout_ready, layout_ready_error, occupies_panel_layout

        if occupies_panel_layout(device, ghost) and not is_layout_ready(device, ghost):
            if warnings is not None:
                warnings.append(layout_ready_error(part.ref, device, ghost))
            continue
        w, h = footprint_size(device, ghost)
        if w <= 0 or h <= 0:
            continue
        fp = effective_footprint(device, ghost)
        band = _band_for(part.role)
        order = POWER_RANK.get(part.role, CONTROL_RANK.get(part.role, 50))
        group_name = group if band != "terminal" else part.ref
        props = dict(ghost.properties)
        props.setdefault("role", part.role)
        # The enclosure pack reads these off the schematic when the part never
        # lands on the panel. Without them a later resize sorts the new motor
        # by tag and puts the overload on the wrong side of its contactor.
        schematic = _schematic_component(session, part.ref)
        if schematic is not None:
            schematic.properties.setdefault("layout_band", band)
            schematic.properties.setdefault("layout_group", group_name)
            schematic.properties.setdefault("layout_order", order)
        pieces.append(
            _Piece(
                ref=part.ref,
                library_id=part.library_id,
                props=props,
                band=band,
                group=group_name,
                order=order,
                din=bool(fp.din_rail),
            )
        )
    _attach_aux_and_interlock(session, parts, pieces, already, ghost_for, group)
    return pieces


def _band_for(role: str) -> str:
    if role in POWER_RANK or role in CONTACTOR_ROLES:
        return "power"
    if role in CONTROL_RANK:
        return "control"
    return "power"


def _attach_aux_and_interlock(
    session: Any,
    parts: list[Any],
    pieces: list[_Piece],
    already: set[str],
    ghost_for: Any,
    group: str,
) -> None:
    _append_catalog_addons(session, parts, pieces, already)
    roles = {str(p.props.get("role") or "") for p in pieces}
    if "fwd" in roles and "rev" in roles:
        _insert_interlock(session, pieces, group, left_role="fwd", right_role="rev", already=already)
    if "star" in roles and "delta" in roles:
        _insert_interlock(session, pieces, group, left_role="delta", right_role="star", already=already)
    _ = ghost_for


def _append_catalog_addons(
    session: Any,
    parts: list[Any],
    pieces: list[_Piece],
    already: set[str],
) -> None:
    """Mount a real catalog aux block on its contactor. Schematic contacts are not blocks."""

    from panelcad.core.circuit_builder import _next_project_ref, _used_refs

    used = _used_refs(session)
    used.update(already)
    used.update(piece.ref for piece in pieces)
    for part in parts:
        if part.family or not part.ref or part.role not in CONTACTOR_ROLES | {"relay"}:
            continue
        parent = next((piece for piece in pieces if piece.ref == part.ref), None)
        if parent is None:
            continue
        for index, accessory in enumerate(_aux_accessories(session, part), start=1):
            library_id = str(accessory.library_id or "")
            if not library_id:
                continue
            try:
                session.catalog.get(library_id)
            except KeyError:
                continue
            ref = _next_project_ref(session, library_id, used)
            props = dict(accessory.assignment_properties())
            props["addon"] = True
            props["parent"] = part.ref
            props["role"] = "aux"
            props["accessory"] = "aux"
            pieces.append(
                _Piece(
                    ref=ref,
                    library_id=library_id,
                    props=props,
                    band=parent.band,
                    group=parent.group,
                    order=parent.order + index * 0.01,
                    din=True,
                    accessory="aux",
                )
            )


def _aux_accessories(session: Any, part: Any) -> list[Any]:
    return [
        accessory
        for accessory in _linked_accessories(session, part)
        if str(getattr(accessory, "category", "") or "").startswith("switching.aux")
    ]


def mount_catalog_aux(session: Any, ref: str) -> list[str]:
    """After a catalog assignment, mount resolved aux blocks beside that contactor."""

    from panelcad.core.circuit_builder import _ensure_kind_sheet, _next_project_ref, _used_refs
    from panelcad.core.models import Component

    parent = None
    for sheet in session.project.sheets:
        if sheet.kind not in {"schematic", "io"}:
            continue
        try:
            parent = sheet.component(ref)
            break
        except KeyError:
            continue
    if parent is None:
        return []
    standin = type("P", (), {"ref": ref, "family": "", "role": "contactor"})()
    # _linked_accessories reads the schematic properties.
    linked = [
        accessory
        for accessory in _linked_accessories(session, standin)
        if str(getattr(accessory, "category", "") or "").startswith("switching.aux")
    ]
    if not linked:
        return []
    panel_id = next((sheet.id for sheet in session.project.sheets if sheet.kind == "panel_layout"), None)
    if panel_id is None:
        panel_id = _ensure_kind_sheet(session, "panel_layout", "enclosure", "Enclosure")
    panel = session.sheet(panel_id)
    try:
        host = panel.component(ref)
    except KeyError:
        return []
    device = session.catalog.get(host.library_id)
    host_w, _host_h = footprint_size(device, host)
    used = _used_refs(session)
    pending: list[tuple[Any, str, dict[str, Any], float]] = []
    extra = 0.0
    for accessory in linked:
        catalog_no = str(accessory.catalog or "")
        if any(
            comp.properties.get("addon")
            and str(comp.properties.get("parent") or "") == ref
            and str(comp.properties.get("catalog") or "") == catalog_no
            for comp in panel.components
        ):
            continue
        library_id = str(accessory.library_id or "")
        try:
            block = session.catalog.get(library_id)
        except KeyError:
            continue
        tag = _next_project_ref(session, library_id, used)
        props = dict(accessory.assignment_properties())
        props.update({"addon": True, "parent": ref, "role": "aux", "accessory": "aux"})
        props["layout_group"] = str(host.properties.get("layout_group") or ref)
        props["layout_band"] = str(host.properties.get("layout_band") or "power")
        try:
            props["layout_order"] = float(host.properties.get("layout_order") or 0) + 0.01 * (len(pending) + 1)
        except (TypeError, ValueError):
            props["layout_order"] = 0.01
        ghost = Component(id=tag, library_id=library_id, x=0, y=0, properties=props)
        w, _h = footprint_size(block, ghost)
        pending.append((block, tag, props, w))
        extra += max(w, 1.0) + MODULE_GAP_MM
    if extra > 0:
        from panelcad.core.layout import clip_offset, nearest_rail

        host_rail = nearest_rail(panel, host.y + clip_offset(device, host))
        for comp in panel.components:
            if comp.id == ref:
                continue
            other = session.catalog.get(comp.library_id)
            if host_rail is None:
                if comp.x + 0.5 < host.x + host_w:
                    continue
                if abs(comp.y - host.y) > 30.0:
                    continue
            else:
                if not effective_footprint(other, comp).din_rail:
                    continue
                rail = nearest_rail(panel, comp.y + clip_offset(other, comp))
                if rail is None or rail.id != host_rail.id or comp.x + 0.5 < host.x + host_w:
                    continue
            comp.x += extra
    added: list[str] = []
    cursor = host.x + host_w + MODULE_GAP_MM
    for block, tag, props, w in pending:
        panel.components.append(
            Component(
                id=tag,
                library_id=block.library_id,
                x=cursor,
                y=host.y,
                properties=props,
            )
        )
        cursor += max(w, 1.0) + MODULE_GAP_MM
        added.append(tag)
    return added


def _insert_interlock(
    session: Any,
    pieces: list[_Piece],
    group: str,
    *,
    left_role: str,
    right_role: str,
    already: set[str],
) -> None:
    device = _interlock_device(session, pieces)
    if device is None:
        return
    left = next((p for p in pieces if p.props.get("role") == left_role and p.group == group), None)
    right = next((p for p in pieces if p.props.get("role") == right_role and p.group == group), None)
    if left is None or right is None:
        return
    from panelcad.core.circuit_builder import _next_project_ref, _used_refs

    used = _used_refs(session)
    used.update(p.ref for p in pieces)
    used.update(already)
    ref = _next_project_ref(session, device.library_id, used)
    if ref in already:
        return
    ghost = Component(id=ref, library_id=device.library_id, x=0.0, y=0.0, properties={"role": "interlock"})
    _apply_draw_scale(session, ghost)
    w, h = footprint_size(device, ghost)
    if w <= 0 or h <= 0:
        return
    order = (left.order + right.order) / 2.0
    if left.order == right.order:
        order = left.order + 0.5
    pieces.append(
        _Piece(
            ref=ref,
            library_id=device.library_id,
            props={
                "role": "interlock",
                "accessory": "mechanical_interlock",
                "function": "mechanical interlock",
                "addon": True,
                "layout_furniture": True,
                "parent": left.ref,
            },
            band="power",
            group=group,
            order=order,
            din=True,
            accessory="mechanical_interlock",
        )
    )


def _interlock_device(session: Any, pieces: list[_Piece]) -> Any | None:
    prefix = "iec."
    for piece in pieces:
        if piece.library_id.startswith("nema."):
            prefix = "nema."
            break
        if piece.library_id.startswith("iec."):
            prefix = "iec."
    found = []
    for device in session.catalog.devices.values():
        accessory = str(device.properties.get("accessory") or "")
        if accessory == "mechanical_interlock" or device.category == "switching.interlock":
            found.append(device)
    if not found:
        return None
    for device in found:
        if device.library_id.startswith(prefix):
            return device
    return found[0]


def _terminal_pieces(session: Any, parts: list[Any], already: set[str], kind: str) -> list[_Piece]:
    prefix = "iec."
    for part in parts:
        if str(part.library_id).startswith("nema."):
            prefix = "nema."
            break
    feed = _first_device(session, [f"{prefix}terminals.x_2p5", "iec.terminals.x_2p5"])
    earth = _first_device(
        session,
        [f"{prefix}terminals.pe_4", f"{prefix}terminals.pe", "iec.terminals.pe_4", "nema.terminals.pe"],
    )
    stop = _first_device(session, [f"{prefix}accessories.end_stop", "iec.accessories.end_stop"])
    if feed is None and earth is None:
        return []
    from panelcad.core.circuit_builder import _next_project_ref, _used_refs

    used = _used_refs(session)
    used.update(already)
    group = f"terminals-{kind}-{len(used)}"
    specs: list[tuple[Any, str]] = []
    if stop is not None:
        specs.append((stop, "end_stop"))
    for _ in range(3):
        if feed is not None:
            specs.append((feed, "terminal"))
    if earth is not None:
        specs.append((earth, "terminal"))
    if stop is not None:
        specs.append((stop, "end_stop"))
    pieces: list[_Piece] = []
    for index, (device, accessory) in enumerate(specs):
        ref = _next_project_ref(session, device.library_id, used)
        pieces.append(
            _Piece(
                ref=ref,
                library_id=device.library_id,
                props={
                    "role": accessory,
                    "accessory": accessory,
                    "layout_circuit": kind,
                    "addon": True,
                    "layout_furniture": True,
                },
                band="terminal",
                group=group,
                order=index,
                din=True,
                accessory=accessory,
            )
        )
    return pieces


def _first_device(session: Any, library_ids: list[str]) -> Any | None:
    for library_id in library_ids:
        try:
            return session.catalog.get(library_id)
        except KeyError:
            continue
    return None


def _stamp_catalog_accessories(session: Any, parts: list[Any], pieces: list[_Piece]) -> None:
    """Copy a verified accessory SKU onto a placed accessory when the parent lists it.

    Numbers that are not in the parts library are left off. Nothing is invented.
    """

    parents = [part for part in parts if part.ref and not part.family]
    for part in parents:
        linked = _linked_accessories(session, part)
        if not linked:
            continue
        children = [
            piece
            for piece in pieces
            if piece.accessory and str(piece.props.get("parent") or "") == part.ref
        ]
        interlocks = [piece for piece in pieces if piece.accessory == "mechanical_interlock"]
        pool = list(linked)
        for piece in children + interlocks:
            match = _take_match(pool, piece)
            if match is None:
                continue
            piece.props.update(match.assignment_properties())


def _linked_accessories(session: Any, part: Any) -> list[Any]:
    props = {}
    schematic = None
    from panelcad.core.circuit_builder import _schematic_component

    if part.ref:
        schematic = _schematic_component(session, part.ref)
    if schematic is not None:
        props = schematic.properties
    catalog_no = str(props.get("catalog") or props.get("part_number") or "").strip()
    if not catalog_no or not hasattr(session, "parts"):
        return []
    try:
        parent = session.parts.get(catalog_no, props.get("manufacturer") or None)
    except KeyError:
        return []
    return list(session.parts.resolved_accessories(parent))


def _take_match(pool: list[Any], piece: _Piece) -> Any | None:
    for index, part in enumerate(pool):
        category = str(part.category or "")
        if piece.accessory == "mechanical_interlock" and (
            category == "switching.interlock" or "interlock" in category
        ):
            return pool.pop(index)
        if piece.accessory == "aux" and category.startswith("switching.aux"):
            return pool.pop(index)
        if piece.accessory in {"end_stop", "terminal"} and (
            category.startswith("connection.accessory") or category.startswith("connection.terminal")
        ):
            return pool.pop(index)
    return None


def _chunks(pieces: list[_Piece]) -> list[_Chunk]:
    band_rank = {"power": 0, "control": 1, "terminal": 2}
    ordered = sorted(pieces, key=lambda p: (band_rank.get(p.band, 9), p.group, p.order, p.ref))
    chunks: list[_Chunk] = []
    for piece in ordered:
        if (
            chunks
            and chunks[-1].group
            and chunks[-1].group == piece.group
            and chunks[-1].band == piece.band
        ):
            chunks[-1].pieces.append(piece)
            continue
        chunks.append(_Chunk(pieces=[piece], band=piece.band, order=int(piece.order)))
    for chunk in chunks:
        chunk.pieces.sort(key=lambda p: (p.order, p.ref))
    return chunks


def _rails_for(session: Any, dest: Any, chunk: _Chunk, min_rail_y: float) -> list[Rail]:
    rails = []
    for rail in dest.rails:
        bands = _rail_bands(session, dest, rail)
        if bands and chunk.band not in bands:
            continue
        # A rail that already carries this band stays usable. A later power
        # row can sit below the terminal rail; new terminals still join it.
        if float(rail.y) + 1e-6 < min_rail_y and chunk.band not in bands:
            continue
        rails.append(rail)
    rails.sort(key=lambda rail: (rail.y, rail.x))
    return rails


def _module_gap(pieces: list[_Piece]) -> float:
    """Sheet millimetres between modules. Catalog gap is true millimetres."""

    scales = [piece.scale for piece in pieces if piece.scale > 1e-6]
    scale = min(scales) if scales else 1.0
    return MODULE_GAP_MM * scale


def _prefix_len(session: Any, rail: Rail, pieces: list[_Piece]) -> int:
    """How many modules from the front of the group fit on this rail."""

    limit = rail.length_mm
    gap = _module_gap(pieces)
    total = 0.0
    count = 0
    for piece in pieces:
        w, _h = _piece_size(session, piece)
        need = w if count == 0 else w + gap
        if count and total + need > limit + 1e-6:
            break
        if w > limit + 1e-6:
            break
        total += need
        count += 1
    return count


def _place_chunk(
    session: Any,
    dest: Any,
    chunk: _Chunk,
    occupied: list[tuple[float, float, float, float]],
    warnings: list[str],
    *,
    min_rail_y: float,
    keep_inside: bool = False,
) -> int:
    if not chunk.pieces:
        return 0
    pending = list(chunk.pieces)
    placed = 0
    guard = 0
    while pending and guard < 16:
        guard += 1
        rails = _rails_for(session, dest, chunk, min_rail_y)
        fitted = False
        for count in range(len(pending), 0, -1):
            sub = _Chunk(pieces=pending[:count], band=chunk.band, order=chunk.order)
            for rail in rails:
                if _try_on_rail(
                    session,
                    dest,
                    rail,
                    sub,
                    occupied,
                    warnings,
                    allow_overflow=False,
                    keep_inside=keep_inside,
                ):
                    pending = pending[count:]
                    placed += count
                    fitted = True
                    break
            if fitted:
                break
        if fitted:
            continue
        rails_before = list(dest.rails)
        trays_before = list(dest.trays)
        rail = _add_rail(dest, occupied, warnings)
        if keep_inside and not _furniture_fits(dest, rail, trays_before):
            dest.rails[:] = rails_before
            dest.trays[:] = trays_before
            warnings.append(_fit_warning(pending))
            break
        count = _prefix_len(session, rail, pending) or 1
        count = min(count, len(pending))
        sub = _Chunk(pieces=pending[:count], band=chunk.band, order=chunk.order)
        if not _try_on_rail(
            session,
            dest,
            rail,
            sub,
            occupied,
            warnings,
            allow_overflow=False,
            keep_inside=keep_inside,
        ):
            if keep_inside and not _force_on_rail(
                session,
                dest,
                rail,
                sub,
                occupied,
                warnings,
                keep_inside=True,
            ):
                dest.rails[:] = rails_before
                dest.trays[:] = trays_before
                warnings.append(_fit_warning(pending[:count]))
                break
            if not keep_inside:
                _force_on_rail(session, dest, rail, sub, occupied, warnings)
        pending = pending[count:]
        placed += count
    return placed


def _try_on_rail(
    session: Any,
    dest: Any,
    rail: Rail,
    chunk: _Chunk,
    occupied: list[tuple[float, float, float, float]],
    warnings: list[str],
    *,
    allow_overflow: bool,
    keep_inside: bool = False,
) -> bool:
    _match_rail_to_existing_clips(session, dest, rail)
    widths = [_piece_size(session, piece) for piece in chunk.pieces]
    gap = _module_gap(chunk.pieces)
    total = sum(w for w, _h in widths) + gap * max(0, len(widths) - 1)
    if total <= 0:
        return False
    limit = rail.x + rail.length_mm
    starts = _candidate_starts(rail, occupied)
    if allow_overflow and total > rail.length_mm + 1e-6:
        starts = [rail.x]
    for start in starts:
        if not allow_overflow and start + total > limit + 1e-6:
            continue
        rects = _rects_at(session, rail, chunk, start)
        if rects is None:
            continue
        if _hits_any(rects, occupied) or _hits_trays(rects, dest):
            continue
        if not allow_overflow and any(x + w > limit + 1e-6 for x, _y, w, _h in rects):
            continue
        if keep_inside and not _rects_inside(dest, rects):
            continue
        _commit(session, dest, chunk, rects, occupied, warnings)
        return True
    return False


def _force_on_rail(
    session: Any,
    dest: Any,
    rail: Rail,
    chunk: _Chunk,
    occupied: list[tuple[float, float, float, float]],
    warnings: list[str],
    *,
    keep_inside: bool = False,
) -> bool:
    _match_rail_to_existing_clips(session, dest, rail)
    rects = _rects_at(session, rail, chunk, rail.x) or []
    if not rects:
        return False
    # Slide right until the chunk clears parts already on this rail, then
    # keep every module inside the rail.
    guard = 0
    limit = rail.x + rail.length_mm
    while guard < 40 and (_hits_any(rects, occupied) or _hits_trays(rects, dest)):
        guard += 1
        if any(x + w + 4.0 > limit + 1e-6 for x, _y, w, _h in rects):
            break
        rects = [(x + 4.0, y, w, h) for x, y, w, h in rects]
    clamped: list[tuple[float, float, float, float]] = []
    for x, y, w, h in rects:
        if x + w > limit + 1e-6:
            x = max(rail.x, limit - w)
        if x < rail.x:
            x = rail.x
        clamped.append((x, y, w, h))
    if keep_inside and (
        not _rects_inside(dest, clamped) or _hits_any(clamped, occupied) or _hits_trays(clamped, dest)
    ):
        return False
    _commit(session, dest, chunk, clamped, occupied, warnings)
    return True


def _match_rail_to_existing_clips(session: Any, dest: Any, rail: Rail) -> None:
    """Move the rail onto the clip line of modules already seated on it.

    A short terminal's clip is not 15 mm from the body top, so the rail drawn
    from the arrangement sits a couple of millimetres off that group. A later
    insert would otherwise clip to the rail and miss the first group's line.
    """

    from panelcad.core.layout import clip_offset, is_rail_module

    clips: list[float] = []
    for comp in dest.components:
        try:
            device = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        if not is_rail_module(device, comp):
            continue
        clip = comp.y + clip_offset(device, comp)
        if abs(clip - rail.y) <= 6.0:
            clips.append(clip)
    if not clips:
        return
    clips.sort()
    rail.y = clips[len(clips) // 2]


def _rects_at(session: Any, rail: Rail, chunk: _Chunk, start: float) -> list[tuple[float, float, float, float]] | None:
    rects: list[tuple[float, float, float, float]] = []
    cursor = start
    for piece in chunk.pieces:
        device = session.catalog.get(piece.library_id)
        ghost = _ghost(piece)
        w, h = footprint_size(device, ghost)
        if w <= 0 or h <= 0:
            return None
        clamped, y = place_on_rail(device, rail, cursor, component=ghost)
        # Keep the requested x so a group can extend past a short rail without
        # the rail clamp stacking the next module on top of the previous one.
        x = cursor if cursor + 1e-6 >= rail.x else clamped
        rects.append((x, y, w, h))
        cursor = x + w + _module_gap(chunk.pieces)
    return rects


def _commit(
    session: Any,
    dest: Any,
    chunk: _Chunk,
    rects: list[tuple[float, float, float, float]],
    occupied: list[tuple[float, float, float, float]],
    warnings: list[str],
) -> None:
    for piece, (x, y, w, h) in zip(chunk.pieces, rects, strict=True):
        props = dict(piece.props)
        props["layout_band"] = piece.band
        props["layout_group"] = piece.group
        props["layout_order"] = piece.order
        if piece.accessory and "accessory" not in props:
            props["accessory"] = piece.accessory
        dest.components.append(
            Component(
                id=piece.ref,
                library_id=piece.library_id,
                x=x,
                y=y,
                scale=_ghost(piece).scale,
                properties=props,
            )
        )
        occupied.append((x, y, w, h))
        if not _part_inside(dest, x, y, w, h):
            warnings.append(_fit_warning([piece]))


def _rail_bands(session: Any, dest: Any, rail: Rail) -> set[str]:
    from panelcad.core.layout import clip_offset

    bands: set[str] = set()
    for comp in dest.components:
        band = str(comp.properties.get("layout_band") or "")
        if band not in {"power", "control", "terminal"}:
            continue
        device = session.catalog.get(comp.library_id)
        clip_y = comp.y + clip_offset(device, comp)
        nearest = nearest_rail(dest, clip_y)
        if nearest is not None and nearest.id == rail.id:
            bands.add(band)
    return bands


def _candidate_starts(rail: Rail, occupied: list[tuple[float, float, float, float]]) -> list[float]:
    starts = {float(rail.x)}
    for ox, oy, ow, oh in occupied:
        if oy + oh < rail.y - 80.0 or oy > rail.y + 40.0:
            continue
        starts.add(ox + ow + MODULE_GAP_MM)
    ordered = sorted(x for x in starts if x >= rail.x - 1e-6)
    return ordered or [float(rail.x)]


def _add_rail(
    dest: Any,
    occupied: list[tuple[float, float, float, float]],
    warnings: list[str],
) -> Rail:
    last = dest.rails[-1]
    rh = rail_height(last)
    scale = _sheet_scale(dest)
    duct_mm = DUCT_MM * scale
    duct_gap = DUCT_GAP_MM * scale
    bottom = _stack_bottom(dest, occupied, rh)
    floor = _lower_floor(dest)
    duct_y = bottom + duct_gap
    rail_y = duct_y + duct_mm + duct_gap + rh / 2.0
    if floor is not None and rail_y + rh / 2.0 > floor - 0.4:
        # No room for another duct under the row. Sit the rail in the gap
        # above the bottom duct instead of running off the backplate.
        rail_y = bottom + max(duct_gap, RAIL_CLEAR_MM * scale) + rh / 2.0
        duct_y = None
    if duct_y is not None:
        duct = WireTray(
            id=_next_id(dest.trays, "wt"),
            x=last.x,
            y=duct_y,
            length_mm=last.length_mm,
            width_mm=duct_mm,
            orientation="h",
        )
        dx, dy, dw, dh = tray_aabb(duct)
        duct_rect = (dx, dy, dw, dh)
        if not _hits_any([duct_rect], occupied) and not _hits_trays([duct_rect], dest):
            if floor is None or dy + dh <= floor - 0.4:
                dest.trays.append(duct)
            else:
                clear = RAIL_CLEAR_MM * scale
                rail_y = bottom + clear + rh / 2.0
        else:
            clear = RAIL_CLEAR_MM * scale
            rail_y = max(last.y + rh + clear, bottom + clear + rh / 2.0)
            warnings.append("wire duct does not fit between rails")
    rail = Rail(
        id=_next_rail_id(dest),
        x=last.x,
        y=rail_y,
        length_mm=last.length_mm,
        height_mm=rh,
        type=last.type,
    )
    dest.rails.append(rail)
    return rail


def _place_field(
    session: Any,
    dest: Any,
    pieces: list[_Piece],
    occupied: list[tuple[float, float, float, float]],
    warnings: list[str],
    *,
    keep_inside: bool = False,
) -> int:
    enc = dest.enclosure
    if enc is None:
        return 0
    plate = _plate_box(dest)
    if plate is None:
        return 0
    blocked = list(occupied)
    blocked.extend(tray_aabb(tray) for tray in dest.trays)
    items = []
    for piece in pieces:
        device = session.catalog.get(piece.library_id)
        ghost = _ghost(piece)
        items.append((piece.ref, device, ghost))
    spots = pack_field(items, enc, blocked=blocked, bounds=plate)
    placed = 0
    by_ref = {piece.ref: piece for piece in pieces}
    for ref, x, y, scale in spots:
        piece = by_ref[ref]
        device = session.catalog.get(piece.library_id)
        ghost = _ghost(piece)
        w, h = footprint_size(device, ghost)
        ww, hh = w * scale, h * scale
        outside = scale < 0.99 or not _part_inside(dest, x, y, ww, hh)
        if outside and keep_inside:
            warnings.append(_fit_warning([piece]))
            continue
        props = dict(piece.props)
        props["layout_band"] = "power"
        props["layout_group"] = piece.group
        props["layout_order"] = piece.order
        dest.components.append(
            Component(
                id=ref,
                library_id=piece.library_id,
                x=x,
                y=y,
                scale=ghost.scale * scale,
                properties=props,
            )
        )
        occupied.append((x, y, ww, hh))
        if outside:
            warnings.append(_fit_warning([piece]))
        placed += 1
    return placed


def _ghost(piece: _Piece) -> Component:
    return Component(
        id=piece.ref,
        library_id=piece.library_id,
        x=0.0,
        y=0.0,
        scale=piece.scale if piece.scale > 1e-6 else 1.0,
        properties=dict(piece.props),
    )


def _piece_size(session: Any, piece: _Piece) -> tuple[float, float]:
    device = session.catalog.get(piece.library_id)
    return footprint_size(device, _ghost(piece))


def _apply_draw_scale(session: Any, ghost: Component) -> None:
    """Stamp the panel draw scale onto a sizing ghost. Placement uses piece.scale."""

    panel = next((sheet for sheet in session.project.sheets if sheet.kind == "panel_layout"), None)
    ghost.scale = _sheet_scale(panel) if panel is not None else 1.0


def _sheet_scale(dest: Any) -> float:
    enc = getattr(dest, "enclosure", None)
    if enc is None:
        return 1.0
    from panelcad.core.layout import _draw_scale

    return _draw_scale(enc)


def _plate_box(dest: Any) -> tuple[float, float, float, float] | None:
    enc = getattr(dest, "enclosure", None)
    if enc is None:
        return None
    from panelcad.core.layout import backplate_sheet_bounds

    return backplate_sheet_bounds(enc)


def _intersect_box(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[2], b[2])
    bottom = min(a[3], b[3])
    if right - left < 16.0 or bottom - top < 16.0:
        return a
    return (left, top, right, bottom)


def _frame_box(dest: Any) -> tuple[float, float, float, float]:
    from panelcad.render.titleblock import working_rect

    fx, fy, fw, fh = working_rect(dest)
    return (fx, fy, fx + fw, fy + fh)


def _part_inside(dest: Any, x: float, y: float, w: float, h: float) -> bool:
    """True when the footprint is on the drawn backplate and inside the frame."""

    from panelcad.render.titleblock import title_block_rect

    if w <= 0 or h <= 0:
        return True
    if not _inside(x, y, w, h, _frame_box(dest)):
        return False
    tx, ty, tw, th = title_block_rect(dest)
    if rects_overlap(x, y, w, h, tx, ty, tw, th):
        return False
    plate = _plate_box(dest)
    if plate is not None and not _inside(x, y, w, h, plate):
        return False
    return True


def _rects_inside(dest: Any, rects: list[tuple[float, float, float, float]]) -> bool:
    return all(_part_inside(dest, x, y, w, h) for x, y, w, h in rects)


def _furniture_fits(dest: Any, rail: Rail, trays_before: list[Any]) -> bool:
    from panelcad.core.layout import rail_aabb

    if not _part_inside(dest, *rail_aabb(rail)):
        return False
    for tray in dest.trays[len(trays_before) :]:
        if not _part_inside(dest, *tray_aabb(tray)):
            return False
    return True


def _fit_warning(pieces: list[Any]) -> str:
    names = ", ".join(str(getattr(piece, "ref", piece)) for piece in pieces)
    verb = "does" if len(pieces) == 1 else "do"
    return (
        f"{names} {verb} not fit in the free backplate; "
        "recommend_enclosure can size a larger box"
    )


def _occupied(session: Any, dest: Any) -> list[tuple[float, float, float, float]]:
    rects: list[tuple[float, float, float, float]] = []
    for comp in dest.components:
        device = session.catalog.get(comp.library_id)
        w, h = footprint_size(device, comp)
        if w > 0 and h > 0:
            rects.append((comp.x, comp.y, w, h))
    return rects


def _stack_bottom(dest: Any, occupied: list[tuple[float, float, float, float]], rail_h: float) -> float:
    """Bottom of devices and intermediate ducts. The lower perimeter duct is a floor."""

    bottom = float(dest.enclosure.y) if dest.enclosure is not None else 0.0
    for _x, y, _w, h in occupied:
        bottom = max(bottom, y + h)
    for rail in dest.rails:
        bottom = max(bottom, rail.y + rail_h / 2.0)
    plate = _plate_box(dest)
    mid = (plate[1] + plate[3]) / 2.0 if plate is not None else None
    for tray in dest.trays:
        # Side ducts run the full height. Only a horizontal duct between rows
        # is something the next rail has to clear.
        if tray.orientation != "h":
            continue
        _tx, ty, _tw, th = tray_aabb(tray)
        if mid is not None and ty >= mid:
            continue
        bottom = max(bottom, ty + th)
    return bottom


def _lower_floor(dest: Any) -> float | None:
    """Top of the bottom perimeter duct, or the backplate edge when there is none."""

    plate = _plate_box(dest)
    if plate is None:
        return None
    floor = plate[3]
    mid = (plate[1] + plate[3]) / 2.0
    for tray in dest.trays:
        if tray.orientation != "h":
            continue
        _tx, ty, _tw, _th = tray_aabb(tray)
        if ty >= mid:
            floor = min(floor, ty)
    return floor


def _content_bottom(dest: Any, occupied: list[tuple[float, float, float, float]]) -> float:
    bottom = 0.0
    if dest.enclosure is not None:
        bottom = float(dest.enclosure.y)
    for _x, y, _w, h in occupied:
        bottom = max(bottom, y + h)
    for rail in dest.rails:
        bottom = max(bottom, rail.y + rail_height(rail) / 2.0)
    for tray in dest.trays:
        _tx, ty, _tw, th = tray_aabb(tray)
        bottom = max(bottom, ty + th)
    return bottom


def _hits_any(
    rects: list[tuple[float, float, float, float]],
    occupied: list[tuple[float, float, float, float]],
) -> bool:
    for x, y, w, h in rects:
        for ox, oy, ow, oh in occupied:
            if rects_overlap(x, y, w, h, ox, oy, ow, oh):
                return True
    return False


def _hits_trays(rects: list[tuple[float, float, float, float]], dest: Any) -> bool:
    boxes = [tray_aabb(tray) for tray in dest.trays]
    return _hits_any(rects, boxes)


def _inside(x: float, y: float, w: float, h: float, box: tuple[float, float, float, float], pad: float = 0.5) -> bool:
    left, top, right, bottom = box
    return x >= left - pad and y >= top - pad and x + w <= right + pad and y + h <= bottom + pad


def _next_rail_id(dest: Any) -> str:
    used = {rail.id for rail in dest.rails}
    n = len(dest.rails) + 1
    while f"r{n}" in used:
        n += 1
    return f"r{n}"


def _next_id(items: list[Any], prefix: str) -> str:
    used = {item.id for item in items}
    n = len(items) + 1
    while f"{prefix}{n}" in used:
        n += 1
    return f"{prefix}{n}"
