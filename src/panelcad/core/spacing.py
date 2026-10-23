"""Schematic spacing grid and page capacity.

Builders and cleanup place devices on this grid. How many devices and rungs
fit on a page comes from the usable sheet area, not from a fixed count.
"""

from __future__ import annotations

from typing import Any

from panelcad.core.models import Component, Device, Sheet
from panelcad.library.catalog import Catalog
from panelcad.render.sheet import pin_world
from panelcad.render.titleblock import SCHEMATIC_SNAP_MM, working_rect

# Body-to-body room for a wire number (horizontal) and a tag (vertical).
MIN_H_MM = 18.0
MIN_V_MM = 14.0
GRID_MM = SCHEMATIC_SNAP_MM
# A typical contact plus the clearance. Capacity uses these cells, not a head count.
CELL_W_MM = 24.0 + MIN_H_MM
# A rung slot is a contact plus the vertical clearance. A3 holds about nine.
CELL_H_MM = 24.0
RUNG_PITCH_MM = CELL_H_MM
RAIL_PAD_MM = 14.0

_DENSITY_KINDS = frozenset({"schematic", "io"})
_POWER_ROLES = frozenset(
    {
        "breaker",
        "disconnect",
        "contactor",
        "overload",
        "drive",
        "motor",
        "fwd",
        "rev",
        "main",
        "star",
        "delta",
        "softstarter",
        "bypass",
    }
)


def snap(value: float, step: float = GRID_MM) -> float:
    if step <= 0:
        return float(value)
    return round(float(value) / step) * step


def spread_row(left: float, right_limit: float, widths: list[float], min_gap: float) -> list[float]:
    """Origins so the bodies sit in ``[left, right_limit]`` with at least ``min_gap``.

    Extra room is shared as equal gaps. When the row does not fit, pack from
    ``left`` at ``min_gap`` and let the caller wrap or split.
    """

    count = len(widths)
    if count == 0:
        return []
    if count == 1:
        return [float(left)]
    total = sum(widths)
    min_span = total + min_gap * (count - 1)
    span = float(right_limit) - float(left)
    if min_span >= span - 0.5:
        cursor = float(left)
        packed: list[float] = []
        for width in widths:
            packed.append(cursor)
            cursor += width + min_gap
        return packed
    extra = (span - min_span) / (count - 1)
    cursor = float(left)
    spread: list[float] = []
    for index, width in enumerate(widths):
        spread.append(cursor)
        if index + 1 < count:
            cursor += width + min_gap + extra
    return spread


def page_capacity(sheet: Sheet) -> dict[str, Any]:
    """How many devices and rungs the usable area can hold."""

    _x, _y, width, height = working_rect(sheet)
    # The working rect is already inside the border. Do not shrink it again.
    rung_pitch = max(CELL_H_MM, RUNG_PITCH_MM)
    columns = max(1, int(width // CELL_W_MM))
    rungs = max(1, int(height // rung_pitch))
    return {
        "usable_width_mm": round(width, 2),
        "usable_height_mm": round(height, 2),
        "min_h_mm": MIN_H_MM,
        "min_v_mm": MIN_V_MM,
        "grid_mm": GRID_MM,
        "cell_w_mm": CELL_W_MM,
        "cell_h_mm": CELL_H_MM,
        "rung_pitch_mm": rung_pitch,
        "max_per_rung": columns,
        "max_rungs": rungs,
        "max_devices": columns * rungs,
    }


def _is_sketch(library_id: str) -> bool:
    lib = str(library_id or "").lower()
    return "connection.offpage" in lib or "connection.notes" in lib


def _notes_size(comp: Component, device: Device) -> tuple[float, float] | None:
    from panelcad.core.layout import is_notes_device

    if not is_notes_device(device):
        return None
    from panelcad.render.painter import notes_box

    return notes_box(comp, comp.visual_scale())


def _body_width(comp: Component, device: Device) -> float:
    fitted = _notes_size(comp, device)
    if fitted is not None:
        return fitted[0]
    scale = comp.visual_scale()
    width = float(device.schematic_width) * scale
    height = float(device.schematic_height) * scale
    if int(comp.rotation or 0) % 180 == 90:
        return height
    return width


def _rung_y(comp: Component, device: Device) -> float:
    pins = [pin for pin in device.pins if pin.side in {"left", "right"}]
    if not pins:
        return float(comp.y)
    return sum(pin_world(comp, pin)[1] for pin in pins) / len(pins)


def _clusters(comps: list[Component], devices: dict[str, Device]) -> list[list[Component]]:
    groups: list[list[Component]] = []
    used: set[str] = set()
    ordered = sorted(comps, key=lambda comp: (_rung_y(comp, devices[comp.id]), comp.x, comp.id))
    for comp in ordered:
        if comp.id in used:
            continue
        y0 = _rung_y(comp, devices[comp.id])
        group = [other for other in comps if abs(_rung_y(other, devices[other.id]) - y0) <= 6.0]
        for other in group:
            used.add(other.id)
        groups.append(group)
    return groups


def _row_gap(comp: Component, device: Device) -> float:
    """Horizontal clearance that also keeps a short tag off the next symbol."""

    body = max(_body_width(comp, device), 1.0)
    tag = max(body, 1.9 * len(comp.id) + 2.0)
    return max(MIN_H_MM, tag - body + 2.0)


def _body_height(comp: Component, device: Device) -> float:
    fitted = _notes_size(comp, device)
    if fitted is not None:
        return fitted[1]
    scale = comp.visual_scale()
    width = float(device.schematic_width) * scale
    height = float(device.schematic_height) * scale
    if int(comp.rotation or 0) % 180 == 90:
        return width
    return height


def _body_box(comp: Component, device: Device, x: float | None = None, y: float | None = None) -> tuple[float, float, float, float]:
    origin_x = float(comp.x if x is None else x)
    origin_y = float(comp.y if y is None else y)
    return (
        origin_x,
        origin_y,
        origin_x + _body_width(comp, device),
        origin_y + _body_height(comp, device),
    )


def _boxes_hit(a: tuple[float, float, float, float], b: tuple[float, float, float, float], pad: float = 0.6) -> bool:
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0] or a[3] + pad <= b[1] or b[3] + pad <= a[1])


def _hits_any(
    box: tuple[float, float, float, float],
    comps: list[Component],
    devices: dict[str, Device],
    ignore: set[str],
) -> bool:
    for other in comps:
        if other.id in ignore:
            continue
        if _boxes_hit(box, _body_box(other, devices[other.id])):
            return True
    return False


def _outside_usable(sheet: Sheet, comp: Component, device: Device) -> bool:
    """True when the symbol body leaves the drawing area or covers the title block."""

    return _leaves_drawing(sheet, comp, device) or _covers_title_block(sheet, comp, device)


def _leaves_drawing(sheet: Sheet, comp: Component, device: Device) -> bool:
    """True when the symbol body crosses the drawable border."""

    wx, wy, ww, wh = working_rect(sheet)
    box = _body_box(comp, device)
    return box[0] < wx - 0.6 or box[1] < wy - 0.6 or box[2] > wx + ww + 0.6 or box[3] > wy + wh + 0.6


def _covers_title_block(sheet: Sheet, comp: Component, device: Device) -> bool:
    from panelcad.render.titleblock import body_hits_title_block

    box = _body_box(comp, device)
    return body_hits_title_block(sheet, box[0], box[1], box[2] - box[0], box[3] - box[1])


def _is_power_device(comp: Component, device: Device) -> bool:
    role = str(comp.properties.get("role") or "")
    if role in _POWER_ROLES:
        return True
    tops = [pin for pin in device.pins if pin.side == "top"]
    bots = [pin for pin in device.pins if pin.side == "bottom"]
    return len(tops) >= 2 and (len(bots) >= 2 or "motor" in (device.category or "").lower())


def _power_columns(comps: list[Component], devices: dict[str, Device]) -> list[list[Component]]:
    """Vertical builder stacks. Devices that share a sizing group, or an x, are one column."""

    stacks = [comp for comp in comps if _is_power_device(comp, devices[comp.id])]
    grouped: dict[str, list[Component]] = {}
    loose: list[Component] = []
    for comp in stacks:
        key = str(comp.properties.get("sizing_group") or "").strip()
        if key:
            grouped.setdefault(key, []).append(comp)
        else:
            loose.append(comp)
    columns = list(grouped.values())
    for comp in sorted(loose, key=lambda item: (item.x, item.y, item.id)):
        placed = False
        for column in columns:
            if abs(float(comp.x) - min(float(other.x) for other in column)) <= 12.0:
                column.append(comp)
                placed = True
                break
        if not placed:
            columns.append([comp])
    columns.sort(key=lambda column: (min(float(comp.x) for comp in column), column[0].id))
    return columns


def _wired_groups(sheet: Sheet, comps: list[Component]) -> list[list[Component]]:
    """Devices joined by a wire, ignoring rails. A rung stays one group."""

    by_id = {comp.id: comp for comp in comps}
    parent = {ident: ident for ident in by_id}

    def find(ident: str) -> str:
        while parent[ident] != ident:
            parent[ident] = parent[parent[ident]]
            ident = parent[ident]
        return ident

    def union(left: str, right: str) -> None:
        root_l, root_r = find(left), find(right)
        if root_l != root_r:
            parent[root_r] = root_l

    for wire in sheet.wires:
        left = str(wire.frm).split(".", 1)[0]
        right = str(wire.to).split(".", 1)[0]
        if left in by_id and right in by_id and left != right:
            union(left, right)
    buckets: dict[str, list[Component]] = {}
    for ident, comp in by_id.items():
        buckets.setdefault(find(ident), []).append(comp)
    groups = list(buckets.values())
    groups.sort(key=lambda group: (min(float(comp.y) for comp in group), min(float(comp.x) for comp in group), group[0].id))
    return groups


def page_fullness(sheet: Sheet, catalog: Catalog) -> dict[str, Any]:
    """Devices and rungs on this page against the usable-area limit."""

    capacity = page_capacity(sheet)
    if sheet.kind not in _DENSITY_KINDS:
        return {
            "sheet": sheet.id,
            "kind": sheet.kind,
            "applies": False,
            "devices": 0,
            "rungs": 0,
            "columns": 0,
            "busiest_rung": 0,
            "outside": [],
            "severity": "",
            "fullness": 0.0,
            "over_dense": False,
            "recommend_split": False,
            "capacity": capacity,
            "message": "",
        }
    devices: dict[str, Device] = {}
    comps: list[Component] = []
    for comp in sheet.components:
        if _is_sketch(comp.library_id):
            continue
        try:
            devices[comp.id] = catalog.get(comp.library_id)
        except KeyError:
            continue
        comps.append(comp)
    groups = _clusters(comps, devices) if comps else []
    ladder_rows = [group for group in groups if len(group) >= 2]
    columns = _power_columns(comps, devices)
    column_count = len(columns)
    # A power column is one horizontal slot and one vertical run. One-device
    # pin groups used to hide a row of columns from the limit.
    rung_count = max(len(ladder_rows), column_count)
    busiest = max((len(group) for group in ladder_rows), default=0)
    busiest = max(busiest, column_count)
    # A symbol already reported by symbol_on_title_block is not listed again.
    outside = [
        comp.id
        for comp in comps
        if _leaves_drawing(sheet, comp, devices[comp.id])
        and not _covers_title_block(sheet, comp, devices[comp.id])
    ]
    device_count = len(comps)
    device_ratio = device_count / capacity["max_devices"]
    rung_ratio = rung_count / capacity["max_rungs"]
    row_ratio = busiest / capacity["max_per_rung"]
    fullness = max(device_ratio, rung_ratio, row_ratio, 1.0 if outside else 0.0)
    count_over = (
        device_count > capacity["max_devices"]
        or rung_count > capacity["max_rungs"]
        or busiest > capacity["max_per_rung"]
    )
    over = count_over or bool(outside)
    severity = "warning" if over else ""
    message = ""
    if outside:
        shown = ", ".join(outside[:8])
        extra = f" (+{len(outside) - 8} more)" if len(outside) > 8 else ""
        message = (
            f"Sheet {sheet.id} has {len(outside)} device(s) outside the drawing area "
            f"({shown}{extra}). "
            "Split onto a continuation sheet (split_sheet apply=true)."
        )
    elif over:
        message = (
            f"Sheet {sheet.id} is over the page limit "
            f"({device_count} devices, {rung_count} rungs, {column_count} power columns, "
            f"busiest rung {busiest}; usable area allows {capacity['max_devices']} devices and "
            f"{capacity['max_rungs']} rungs, {capacity['max_per_rung']} per rung). "
            "Split onto a continuation sheet (split_sheet apply=true)."
        )
    return {
        "sheet": sheet.id,
        "kind": sheet.kind,
        "applies": True,
        "devices": device_count,
        "rungs": rung_count,
        "columns": column_count,
        "busiest_rung": busiest,
        "outside": outside,
        "severity": severity,
        "fullness": round(fullness, 3),
        "over_dense": over,
        "recommend_split": over,
        "capacity": capacity,
        "message": message,
    }


def project_density(project: Any, catalog: Catalog, sheet_id: str | None = None) -> dict[str, Any]:
    sheets = [project.sheet(sheet_id)] if sheet_id else list(project.sheets)
    rows = [page_fullness(sheet, catalog) for sheet in sheets]
    return {
        "sheets": rows,
        "over_dense": [row["sheet"] for row in rows if row["over_dense"]],
        "limits": {
            "min_h_mm": MIN_H_MM,
            "min_v_mm": MIN_V_MM,
            "grid_mm": GRID_MM,
            "note": (
                "max_devices and max_rungs come from the usable sheet area. "
                "Call split_sheet with apply=true to continue on the next page "
                "with off-page references."
            ),
        },
    }


def _part_width(session: Any, part: Any) -> float:
    try:
        device = session.catalog.get(part.library_id)
    except KeyError:
        return 24.0
    scale = float(getattr(part, "scale", 1.0) or 1.0)
    return max(8.0, float(device.schematic_width) * scale)


def _column_span_parts(session: Any, parts: list[Any]) -> tuple[float, float]:
    left = min(float(part.x) for part in parts)
    right = max(float(part.x) + _part_width(session, part) for part in parts)
    return left, max(8.0, right - left)


def _column_span_comps(catalog: Catalog, column: list[Component]) -> tuple[float, float]:
    left = min(float(comp.x) for comp in column)
    right = left
    for comp in column:
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            right = max(right, float(comp.x) + 24.0)
            continue
        right = max(right, float(comp.x) + _body_width(comp, device))
    return left, max(8.0, right - left)


def _even_origins(left: float, right_edge: float, widths: list[float], min_gap: float) -> list[float]:
    """Left edges. One column is centered. Several share the usable width."""

    if len(widths) == 1:
        span = right_edge - left
        return [left + max(0.0, (span - widths[0]) / 2.0)]
    return spread_row(left, right_edge, widths, min_gap)


def _continuation_power_sheet(session: Any, sheet: Sheet) -> Any:
    base = f"{sheet.id}-power"
    sid = base
    n = 2
    existing = {other.id for other in session.project.sheets}
    while sid in existing:
        sid = f"{base}-{n}"
        n += 1
    dest = session.add_sheet(sid, title=f"{sheet.title or sheet.id} (cont.)", kind=sheet.kind, make_active=False)
    dest.drawing_style = sheet.drawing_style
    dest.standard = getattr(sheet, "standard", "") or ""
    dest.description = f"Continuation of {sheet.id}"
    return dest


def _note_column_overflow(session: Any, sheet_id: str, message: str) -> None:
    notes = session.project.properties.get("power_column_overflow")
    if not isinstance(notes, dict):
        notes = {}
    notes[str(sheet_id)] = message
    session.project.properties["power_column_overflow"] = notes


def _spill_column(session: Any, sheet: Sheet, parts: list[Any], new_left: float, new_width: float) -> list[str]:
    dest = _continuation_power_sheet(session, sheet)
    dwx, _dwy, dww, _dwh = working_rect(dest)
    target = dwx + max(0.0, (dww - new_width) / 2.0)
    dx = target - new_left
    for part in parts:
        part.x = float(part.x) + dx
        part.sheet_override = dest.id
    message = (
        f"Power columns do not fit on {sheet.id} at the minimum clearance. "
        f"Continued on {dest.id}."
    )
    _note_column_overflow(session, sheet.id, message)
    return [message]


def distribute_power_columns(session: Any, sheet_id: str, parts: list[Any]) -> list[str]:
    """Space every builder power column across the usable width.

    Adding a column re-spaces the columns already on the sheet. When the row
    does not fit at the minimum clearance, the new column goes to a continuation
    power sheet instead of wrapping onto the title block.
    """

    if not parts:
        return []
    xs = [float(part.x) for part in parts]
    if max(xs) - min(xs) > 14.0:
        return []
    sheet = session.sheet(sheet_id)
    placed: list[Component] = []
    placed_devices: dict[str, Device] = {}
    for comp in sheet.components:
        try:
            placed_devices[comp.id] = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        placed.append(comp)
    existing = _power_columns(placed, placed_devices)
    # Drop a column that is this insert (refs already assigned, not placed yet).
    new_refs = {str(getattr(part, "ref", "") or "") for part in parts}
    existing = [
        [comp for comp in column if comp.id not in new_refs]
        for column in existing
    ]
    existing = [column for column in existing if column]
    new_left, new_width = _column_span_parts(session, parts)
    wx, _wy, ww, _wh = working_rect(sheet)
    # Ink (tag, rating) hangs past the body. Keep that inside the border.
    left_limit = wx + 12.0
    right_edge = wx + ww - 36.0
    if right_edge <= left_limit + new_width:
        return []
    # A star-delta or reversing insert is several stacks side by side. Pulling
    # those apart to "re-space columns" is not a fit. Park the new column in
    # the free width. Simple DOL columns re-space, and only spill when even a
    # 2 mm gap puts a body outside the drawing area.
    simple = all(
        max(float(comp.x) for comp in column) - min(float(comp.x) for comp in column) <= 14.0
        for column in existing
    )
    if existing and not simple:
        content_right = left_limit
        for comp in placed:
            content_right = max(content_right, _body_box(comp, placed_devices[comp.id])[2])
        gap = min(MIN_H_MM, 8.0)
        target = content_right + gap
        if target + new_width <= right_edge + 0.5:
            dx = target - new_left
            if abs(dx) > 0.4:
                for part in parts:
                    part.x = float(part.x) + dx
            return []
        return _spill_column(session, sheet, parts, new_left, new_width)
    widths = [_column_span_comps(session.catalog, column)[1] for column in existing]
    widths.append(new_width)
    usable = right_edge - left_limit
    if len(widths) == 1:
        origins = _even_origins(left_limit, right_edge, widths, MIN_H_MM)
    else:
        gap = (usable - sum(widths)) / (len(widths) - 1)
        if gap < 2.0:
            return _spill_column(session, sheet, parts, new_left, new_width)
        origins = spread_row(left_limit, right_edge, widths, gap)
    moved_ids: set[str] = set()
    for column, origin in zip(existing, origins[:-1], strict=False):
        current, _width = _column_span_comps(session.catalog, column)
        dx = origin - current
        if abs(dx) <= 0.4:
            continue
        for comp in column:
            comp.x = float(comp.x) + dx
            moved_ids.add(comp.id)
    dx = origins[-1] - new_left
    if abs(dx) > 0.4:
        for part in parts:
            part.x = float(part.x) + dx
    if moved_ids:
        for wire in sheet.wires:
            ends = {str(wire.frm).split(".", 1)[0], str(wire.to).split(".", 1)[0]}
            if ends & moved_ids:
                wire.points = []
    return []


def apply_spacing_grid(sheet: Sheet, catalog: Catalog) -> int:
    """Open crowded rungs across the rails. A second pass does not move them.

    A device sitting under another (a hold-in contact under Start) keeps that
    x when the device above moves. Single-device rows, including a power
    column, stay put.
    """

    from panelcad.core.ladder import rail_xs
    from panelcad.core.layout import is_layout_sheet

    if is_layout_sheet(sheet) or sheet.kind not in _DENSITY_KINDS:
        return 0
    devices: dict[str, Device] = {}
    comps: list[Component] = []
    for comp in sheet.components:
        if _is_sketch(comp.library_id):
            continue
        try:
            devices[comp.id] = catalog.get(comp.library_id)
        except KeyError:
            continue
        comps.append(comp)
    if len(comps) < 2:
        return 0
    try:
        left, right = rail_xs(sheet)
    except (AttributeError, ValueError):
        wx, _wy, ww, _wh = working_rect(sheet)
        left, right = wx + 8.0, wx + ww - 8.0
    inner_left = left + RAIL_PAD_MM
    inner_right = right - RAIL_PAD_MM
    old_x = {comp.id: float(comp.x) for comp in comps}
    by_id = {comp.id: comp for comp in comps}
    moved = 0
    shifts: dict[str, float] = {}
    for group in _clusters(comps, devices):
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda comp: (comp.x, comp.id))
        widths = [_body_width(comp, devices[comp.id]) for comp in ordered]
        pinned: Component | None = None
        last_right = float(ordered[-1].x) + widths[-1]
        if last_right >= inner_right - 6.0:
            pinned = ordered[-1]
            ordered = ordered[:-1]
            widths = widths[:-1]
        if len(ordered) < 2:
            continue
        gap = max(_row_gap(comp, devices[comp.id]) for comp in ordered)
        limit = float(pinned.x) - gap if pinned is not None else inner_right
        # Do not leapfrog a symbol that shares this band but is not in the row
        # (a coil whose pin line is a few millimetres off the contacts).
        band_top = min(float(comp.y) for comp in ordered)
        band_bot = max(float(comp.y) + 16.0 for comp in ordered)
        row_ids = {comp.id for comp in ordered}
        if pinned is not None:
            row_ids.add(pinned.id)
        for other in comps:
            if other.id in row_ids:
                continue
            height = float(devices[other.id].schematic_height) * other.visual_scale()
            if float(other.y) + height < band_top - 4.0 or float(other.y) > band_bot + 4.0:
                continue
            if float(other.x) <= min(float(comp.x) for comp in ordered) + 1.0:
                continue
            limit = min(limit, float(other.x) - gap)
        if limit <= inner_left + widths[0]:
            continue
        origins = spread_row(inner_left, limit, widths, gap)
        for comp, origin in zip(ordered, origins, strict=True):
            dx = origin - float(comp.x)
            if abs(dx) <= 0.4:
                continue
            proposed = _body_box(comp, devices[comp.id], x=origin)
            # Every symbol on the sheet, not only this pin row. A contact
            # must not land inside a supply that sits on a different pin line.
            if _hits_any(proposed, comps, devices, {comp.id}):
                continue
            comp.x = origin
            shifts[comp.id] = dx
            moved += 1
    for comp in comps:
        if comp.id in shifts:
            continue
        best: tuple[float, float] | None = None
        for parent_id, dx in shifts.items():
            parent = by_id[parent_id]
            if abs(old_x[comp.id] - old_x[parent_id]) > 8.0:
                continue
            if comp.y <= parent.y - 2.0 or comp.y > parent.y + 56.0:
                continue
            dist = abs(old_x[comp.id] - old_x[parent_id])
            if best is None or dist < best[0]:
                best = (dist, dx)
        if best is None or abs(best[1]) <= 0.4:
            continue
        proposed = _body_box(comp, devices[comp.id], x=float(comp.x) + best[1])
        if _hits_any(proposed, comps, devices, {comp.id}):
            continue
        comp.x = float(comp.x) + best[1]
        moved += 1
    return moved


def _parent_tag(comp: Component) -> str | None:
    """Coil tag a child contact belongs to. M1-13 belongs with M1."""

    import re

    parent = str(comp.properties.get("parent") or "").strip()
    if parent and parent != comp.id:
        return parent
    match = re.match(r"^([A-Za-z]+\d+)[-.](\d+)$", comp.id)
    if match:
        return match.group(1)
    return None


def _is_coil_comp(comp: Component, device: Device) -> bool:
    role = str(comp.properties.get("role") or "").lower()
    if "coil" in role:
        return True
    blob = f"{device.category} {comp.library_id}".lower()
    return "coil" in blob and "contact" not in role


def _is_supply_comp(comp: Component, device: Device) -> bool:
    role = str(comp.properties.get("role") or "").lower()
    if role in {"psu", "transformer"}:
        return True
    cat = str(device.category or "").lower()
    return cat.startswith("power.supply") or cat.startswith("power.transformer")


def _rung_groups(sheet: Sheet, comps: list[Component], devices: dict[str, Device]) -> tuple[list[Component], list[list[Component]]]:
    """Supply (shared) plus one group per coil rung.

    A seal-in contact is not its own rung. It belongs with the coil of the
    same tag. A shared E-stop, which feeds every motor, stays with the supply
    instead of gluing the rungs into one group.
    """

    by_id = {comp.id: comp for comp in comps}
    on_sheet = set(by_id)
    children: dict[str, list[Component]] = {}
    supplies: list[Component] = []
    series: list[Component] = []
    for comp in comps:
        parent = _parent_tag(comp)
        if parent and parent in on_sheet and parent != comp.id:
            children.setdefault(parent, []).append(comp)
            continue
        if _is_supply_comp(comp, devices[comp.id]):
            supplies.append(comp)
            continue
        series.append(comp)
    graph: dict[str, set[str]] = {comp.id: set() for comp in series}
    for wire in sheet.wires:
        left = str(wire.frm).split(".", 1)[0]
        right = str(wire.to).split(".", 1)[0]
        if left in graph and right in graph and left != right:
            graph[left].add(right)
            graph[right].add(left)
    coils = [comp for comp in series if _is_coil_comp(comp, devices[comp.id])]
    coils.sort(key=lambda comp: (float(comp.y), float(comp.x), comp.id))
    if not coils:
        # No coil: each wired cluster is a rung, supplies stay shared.
        groups = _wired_groups(sheet, series)
        return supplies, groups
    from collections import deque

    dist: dict[str, dict[str, int]] = {}
    for coil in coils:
        distance = {coil.id: 0}
        queue: deque[str] = deque([coil.id])
        while queue:
            node = queue.popleft()
            for nxt in graph.get(node, ()):
                if nxt in distance:
                    continue
                distance[nxt] = distance[node] + 1
                queue.append(nxt)
        dist[coil.id] = distance
    rungs: dict[str, list[Component]] = {coil.id: [by_id[coil.id]] for coil in coils}
    claimed: set[str] = set()
    for comp in series:
        if comp.id in {coil.id for coil in coils}:
            continue
        best: list[tuple[int, str]] = []
        for coil in coils:
            if comp.id in dist[coil.id]:
                best.append((dist[coil.id][comp.id], coil.id))
        if not best:
            continue
        best.sort()
        if len(best) == 1 or best[0][0] < best[1][0]:
            rungs[best[0][1]].append(comp)
            claimed.add(comp.id)
    # Tag children follow their coil even when the wire graph did not.
    for parent, kids in children.items():
        for coil_id, group in rungs.items():
            if parent == coil_id or any(comp.id == parent for comp in group):
                group.extend(kids)
                break
    ordered = [rungs[coil.id] for coil in coils if rungs[coil.id]]
    # Devices that never joined a coil (and are not the shared E-stop) are their own rungs.
    loose = [comp for comp in series if comp.id not in claimed and comp.id not in {c.id for c in coils}]
    # Shared nodes (equal distance to two coils) stay with the supply so the
    # rungs can split. An E-stop is that node.
    for comp in loose:
        nearer = []
        for coil in coils:
            if comp.id in dist.get(coil.id, {}):
                nearer.append(dist[coil.id][comp.id])
        if len(nearer) >= 2 and len(set(nearer)) == 1:
            supplies.append(comp)
        else:
            ordered.append([comp])
    ordered.sort(key=lambda group: (min(float(comp.y) for comp in group), min(float(comp.x) for comp in group), group[0].id))
    return supplies, ordered


def split_plan(sheet: Sheet, catalog: Catalog) -> dict[str, Any]:
    """Split near the middle of the rung order. A coil and its contacts stay together."""

    report = page_fullness(sheet, catalog)
    devices: dict[str, Device] = {}
    comps: list[Component] = []
    for comp in sheet.components:
        if _is_sketch(comp.library_id):
            continue
        try:
            devices[comp.id] = catalog.get(comp.library_id)
        except KeyError:
            continue
        comps.append(comp)
    supplies, rungs = _rung_groups(sheet, comps, devices)
    keep: list[str] = [comp.id for comp in supplies]
    move: list[str] = []
    if len(rungs) >= 2:
        mid = max(1, len(rungs) // 2)
        # A page that is only slightly over still splits near the middle, not
        # after the supply alone.
        if len(rungs) - mid < 1:
            mid = len(rungs) - 1
        for group in rungs[:mid]:
            keep.extend(comp.id for comp in group)
        for group in rungs[mid:]:
            move.extend(comp.id for comp in group)
    else:
        keep.extend(comp.id for group in rungs for comp in group)
    report["keep"] = keep
    report["move"] = move
    report["rung_count_split"] = len(rungs)
    return report
