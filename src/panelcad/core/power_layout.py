"""NEMA power columns and symbol clearance for schematic sheets.

A power sheet is any schematic with two or more phase stacks (a symbol
with at least two top pins and two bottom pins: disconnect, breaker, fuse
block, contactor, overload, drive, motor). Each wired chain flows top to
bottom. Phase pins of a chain share one set of X positions. Terminal
strips sit in their own column. A transformer and its single-pole fuses
sit beside the device that feeds them.

Control ladders are not restacked. Overlapping bodies on a ladder are
pushed apart, and a whole rung moves together so the rung stays straight.
"""

from __future__ import annotations

from panelcad.core.models import Component, Device, Sheet
from panelcad.core.route import component_aabb
from panelcad.library.catalog import Catalog
from panelcad.render.sheet import parse_pin_ref, pin_side_world, pin_world
from panelcad.render.titleblock import working_rect

# Room for a tag and a wire number between stacked power symbols.
_STACK_GAP = 14.0
_MIN_GAP = 8.0
_COLUMN_GAP = 26.0
_SIDE_GAP = 16.0
_TERMINAL_GAP = 7.0
_TOP_PAD = 14.0
_BOTTOM_PAD = 6.0
_RATING_GUTTER = 22.0
# A second Cleanup must not multiply this. The fit is an absolute scale.
_MIN_SCALE = 0.62
# Three phase risers, 4 mm apart, in the gap after a feeder column.
_BUS_CHANNEL = 16.0
_MIN_COLUMN_GAP = 6.0
_OVERFLOW: dict[str, str] = {}
_UNFITTED: dict[str, set[str]] = {}
_LEFT_ALONE: set[str] = set()


def power_column_overflow() -> dict[str, str]:
    """Sheets whose columns still do not fit at the minimum scale."""

    return dict(_OVERFLOW)


def unfitted_columns(sheet_id: str) -> set[str]:
    """Devices left where they were because their column does not fit."""

    return set(_UNFITTED.get(sheet_id, ()))


def layout_left_alone(sheet_id: str) -> bool:
    """True when this sheet's power shape was recognised and not redrawn."""

    return sheet_id in _LEFT_ALONE


def has_rotated_phase_stacks(sheet: Sheet, catalog: Catalog) -> bool:
    """True when a multi-pole power symbol is drawn on its side."""

    for comp in sheet.components:
        if int(comp.rotation or 0) % 180 == 0:
            continue
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        tops = [pin for pin in device.pins if pin.side == "top"]
        bots = [pin for pin in device.pins if pin.side == "bottom"]
        blob = f"{device.category} {comp.library_id} {comp.properties.get('role') or ''}".lower()
        if len(tops) >= 2 and (len(bots) >= 2 or "motor" in blob):
            return True
    return False


def tidy_schematic_sheet(sheet: Sheet, catalog: Catalog) -> dict[str, int]:
    """Apply power-column rules, then separate any bodies that still intersect."""

    from panelcad.core.ladder import is_ladder_sheet

    powered = layout_power_sheet(sheet, catalog)
    separated = separate_symbol_overlaps(sheet, catalog, rung=not powered and is_ladder_sheet(sheet))
    _drop_routes(sheet)
    return {"power": powered, "separated": separated}


def layout_power_sheet(sheet: Sheet, catalog: Catalog) -> int:
    """Place phase chains, terminal strips, and transformer fuses. 0 if this is not a power sheet."""

    if sheet.kind != "schematic":
        return 0
    _UNFITTED.pop(sheet.id, None)
    _LEFT_ALONE.discard(sheet.id)
    devices: dict[str, Device] = {}
    for comp in sheet.components:
        try:
            devices[comp.id] = catalog.get(comp.library_id)
        except KeyError:
            continue
    stacks = [comp for comp in sheet.components if comp.id in devices and _is_phase_stack(devices[comp.id], comp)]
    if len(stacks) < 2:
        return 0
    terminals = [
        comp
        for comp in sheet.components
        if comp.id in devices and _is_terminal(devices[comp.id], comp) and comp not in stacks
    ]
    stack_ids = {comp.id for comp in stacks}
    terminal_ids = {comp.id for comp in terminals}
    sides = [
        comp
        for comp in sheet.components
        if comp.id in devices
        and comp.id not in stack_ids
        and comp.id not in terminal_ids
        and not _skip(devices[comp.id])
    ]
    from panelcad.core.ladder import rail_xs

    wx, wy, ww, wh = working_rect(sheet)
    room = wh - _TOP_PAD - _BOTTOM_PAD
    if _power_shape_left_alone(sheet, stacks, devices):
        # A rejoining starter, or a shape that is not a plain column or feeder.
        # Splitting it loops the leads. Leave the drawing as it is.
        _LEFT_ALONE.add(sheet.id)
        return 0
    chains = _chains(sheet, stacks, devices)
    chains.sort(key=lambda group: (min(sheet.component(cid).x for cid in group), min(group)))
    ordered = [_order_chain(group, sheet, devices) for group in chains]
    _place_terminals(terminals, devices, wx, wy, room)
    _left_rail, right_rail = rail_xs(sheet)
    cursor = _content_right(terminals, devices, wx + 8.0) + 8.0
    available = right_rail - 2.0 - cursor
    bus_gaps = sum(
        1 for chain in ordered if _chain_feeds_branches(chain, ordered, sheet, devices)
    )
    scales, col_gap, overflow = _fit_columns(
        ordered,
        devices,
        room,
        available,
        _side_widths(sheet, ordered, sides, devices),
        bus_gaps,
    )
    if overflow:
        _OVERFLOW[sheet.id] = overflow
    else:
        _OVERFLOW.pop(sheet.id, None)
    placed: set[str] = set(terminal_ids)
    held: set[str] = set()
    for index, chain in enumerate(ordered):
        scale = scales[index]
        span = max(float(devices[comp.id].schematic_width) for comp in chain) * scale
        # A column that does not fit stays where the drawing put it. Parking
        # the extras piled them on the last branch and moved them again next time.
        if index > 0 and cursor + span > right_rail + 0.5:
            held.update(comp.id for comp in chain)
            continue
        _set_scale(chain, scale)
        gap = _STACK_GAP if scale > 0.98 else _MIN_GAP
        _place_column(chain, devices, cursor + _phase_inset(chain, devices), wy + _TOP_PAD, gap)
        placed.update(comp.id for comp in chain)
        _place_attached_sides(sheet, sides, devices, placed, chain, wy + wh - _BOTTOM_PAD)
        cursor = _content_right(
            [sheet.component(cid) for cid in placed if cid not in terminal_ids],
            devices,
            cursor,
        ) + col_gap
        if _chain_feeds_branches(chain, ordered, sheet, devices):
            # The phase risers stand in this gap. Without it they climb back
            # up the feeder and the fuses read as bypassed.
            cursor += _BUS_CHANNEL
    if held:
        _UNFITTED[sheet.id] = held
        _OVERFLOW.setdefault(
            sheet.id,
            "The power columns do not fit between the rails at the minimum scale. "
            "Split the feeder onto another sheet.",
        )
    else:
        _UNFITTED.pop(sheet.id, None)
    _drop_routes(sheet)
    return len(placed)


def _set_scale(chain: list[Component], scale: float) -> None:
    for comp in chain:
        comp.scale = scale


def _power_shape_left_alone(sheet: Sheet, stacks: list[Component], devices: dict[str, Device]) -> bool:
    """True when column layout would misread this power drawing."""

    if _has_rejoining_split(sheet, stacks, devices):
        return True
    if _side_device_between_stacks(sheet, stacks, devices):
        return True
    known = {comp.id for comp in stacks}
    for wire in sheet.wires:
        try:
            left, left_pin = parse_pin_ref(wire.frm)
            right, right_pin = parse_pin_ref(wire.to)
        except ValueError:
            continue
        if left not in known or right not in known or left == right:
            continue
        if _directed_link(left, left_pin, right, right_pin, devices) is None:
            return True
    return False


def _side_device_between_stacks(
    sheet: Sheet, stacks: list[Component], devices: dict[str, Device]
) -> bool:
    """True when a single-pole device sits on the wire path between two stacks.

    A fuse row between a disconnect and a drive is the power circuit. Gathering
    those fuses as side parts piles them on one point. A transformer that only
    hangs off one stack is not on a path to another stack, so it still sits beside
    the device that feeds it.
    """

    stack_ids = {comp.id for comp in stacks}
    terminal_ids = {
        comp.id
        for comp in sheet.components
        if comp.id in devices and _is_terminal(devices[comp.id], comp)
    }
    graph: dict[str, set[str]] = {}
    for wire in sheet.wires:
        try:
            left, _left_pin = parse_pin_ref(wire.frm)
            right, _right_pin = parse_pin_ref(wire.to)
        except ValueError:
            continue
        if left == right:
            continue
        graph.setdefault(left, set()).add(right)
        graph.setdefault(right, set()).add(left)
    for start in stack_ids:
        # node, a side device is already on this walk, stack the walk left
        queue: list[tuple[str, bool, str]] = [(start, False, start)]
        seen = {(start, False)}
        while queue:
            node, via_side, anchor = queue.pop()
            for nxt in graph.get(node, ()):
                if nxt in stack_ids:
                    if via_side and nxt != anchor:
                        return True
                    state = (nxt, False)
                    if not via_side and state not in seen:
                        seen.add(state)
                        queue.append((nxt, False, nxt))
                    continue
                device = devices.get(nxt)
                is_side = (
                    device is not None and nxt not in terminal_ids and not _skip(device)
                )
                state = (nxt, via_side or is_side)
                if state not in seen:
                    seen.add(state)
                    queue.append((nxt, via_side or is_side, anchor))
    return False


def _has_rejoining_split(sheet: Sheet, stacks: list[Component], devices: dict[str, Device]) -> bool:
    """True when a load feeds two branches that meet again downstream.

    A breaker that feeds the forward and reverse contactors, which then share
    one overload, is one starter. It is not a feeder with a column per branch.
    """

    links = _load_to_line_links(sheet, stacks, devices)
    children: dict[str, set[str]] = {}
    feeders: dict[str, set[str]] = {}
    for src, dst in links:
        children.setdefault(src, set()).add(dst)
        feeders.setdefault(src, set()).add(dst)

    def reachable(start: str) -> set[str]:
        seen: set[str] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            for nxt in children.get(node, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    for src, dsts in feeders.items():
        if len(dsts) < 2:
            continue
        reaches = [reachable(dst) for dst in dsts]
        if set.intersection(*reaches):
            return True
    return False


def _phase_inset(chain: list[Component], devices: dict[str, Device]) -> float:
    """How far the first phase pin sits in from the symbol's left edge."""

    device = devices[chain[0].id]
    pins = sorted((pin for pin in device.pins if pin.side == "top"), key=lambda pin: pin.x)
    if not pins:
        return 0.0
    return float(pins[0].x) * chain[0].visual_scale()


def _column_width(chain: list[Component], devices: dict[str, Device]) -> float:
    return max(float(devices[comp.id].schematic_width) * comp.visual_scale() for comp in chain)


def _chain_load_y(chain: list[Component], devices: dict[str, Device]) -> float:
    lowest = 0.0
    for comp in chain:
        device = devices[comp.id]
        bots = [pin for pin in device.pins if pin.side == "bottom"]
        scale = comp.visual_scale()
        if bots:
            lowest = max(lowest, comp.y + max(float(pin.y) for pin in bots) * scale)
        else:
            lowest = max(lowest, comp.y + float(device.schematic_height) * scale)
    return lowest


def _side_widths(
    sheet: Sheet,
    ordered: list[list[Component]],
    sides: list[Component],
    devices: dict[str, Device],
) -> list[float]:
    """Width of the transformer and fuses that sit beside each chain."""

    widths = []
    claimed: set[str] = set()
    for chain in ordered:
        anchor = {comp.id for comp in chain}
        extra = 0.0
        for _ in range(len(sides) + 1):
            added = False
            for comp in sides:
                if comp.id in claimed:
                    continue
                if not any(other in anchor for other in _neighbors(sheet, comp.id)):
                    continue
                extra += float(devices[comp.id].schematic_width) + _SIDE_GAP
                anchor.add(comp.id)
                claimed.add(comp.id)
                added = True
            if not added:
                break
        widths.append(extra)
    return widths


def _chain_feeds_branches(
    chain: list[Component],
    ordered: list[list[Component]],
    sheet: Sheet,
    devices: dict[str, Device],
) -> bool:
    """True when this column's load side feeds two or more other columns."""

    members = {comp.id for comp in chain}
    others = {comp.id for group in ordered for comp in group if comp.id not in members}
    if len(others) < 2:
        return False
    stacks = [comp for group in ordered for comp in group]
    fed = {
        dst
        for src, dst in _load_to_line_links(sheet, stacks, devices)
        if src in members and dst in others
    }
    return len(fed) >= 2


def _fit_columns(
    ordered: list[list[Component]],
    devices: dict[str, Device],
    room: float,
    available: float,
    side_extra: list[float] | None = None,
    bus_gaps: int = 0,
) -> tuple[list[float], float, str]:
    """Scale and gap so every column sits in the rail span. Absolute, so Cleanup repeats."""

    if not ordered:
        return [], _MIN_COLUMN_GAP, ""
    vertical: list[float] = []
    for chain in ordered:
        scale, _gap = _fit(chain, devices, room)
        vertical.append(scale)
    extra = side_extra or [0.0] * len(ordered)
    unscaled = [
        max(float(devices[comp.id].schematic_width) for comp in chain) + extra[index]
        for index, chain in enumerate(ordered)
    ]

    def total(scales: list[float], gap: float) -> float:
        return (
            sum(width * scale for width, scale in zip(unscaled, scales))
            + gap * max(0, len(scales) - 1)
            + _BUS_CHANNEL * bus_gaps
        )

    scales = list(vertical)
    gap = _COLUMN_GAP + _RATING_GUTTER
    overflow = ""
    if total(scales, gap) > available:
        # Shrink the pitch before the symbols. Then share one lower scale.
        # The bus channel is part of the width, or the scale stays at the
        # vertical fit and the extra columns are reported without shrinking.
        gap = _MIN_COLUMN_GAP
        if total(scales, gap) > available:
            used = gap * max(0, len(scales) - 1) + _BUS_CHANNEL * bus_gaps
            factor = (available - used) / max(sum(unscaled), 1.0)
            factor = max(_MIN_SCALE, factor)
            scales = [min(scale, factor) for scale in scales]
            if total(scales, gap) > available + 0.5:
                overflow = (
                    "The power columns do not fit between the rails at the minimum scale. "
                    "Split the feeder onto another sheet."
                )
                gap = _MIN_COLUMN_GAP
    return scales, gap, overflow


def separate_symbol_overlaps(sheet: Sheet, catalog: Catalog, *, rung: bool = False) -> int:
    """Push intersecting symbol bodies apart. On a ladder, rungs stay straight."""

    if sheet.kind != "schematic":
        return 0
    devices: dict[str, Device] = {}
    for comp in sheet.components:
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        if _skip(device):
            continue
        devices[comp.id] = device
    if len(devices) < 2:
        return 0
    comps = [comp for comp in sheet.components if comp.id in devices]
    if rung:
        return _reflow_rungs(sheet, comps, devices)
    _wx, wy, _ww, wh = working_rect(sheet)
    floor = wy + wh - 1.0
    moved = 0
    for _ in range(40):
        pair = _first_hit(comps, devices)
        if pair is None:
            break
        lower, dy = pair
        partner = _overlap_partner(lower, comps, devices)
        if partner is not None and _is_phase_stack(devices[partner.id], partner) and not _is_phase_stack(
            devices[lower.id], lower
        ):
            stay = _body(partner, devices[partner.id])
            box = _body(lower, devices[lower.id])
            lower.x += stay[2] + _SIDE_GAP - box[0]
            moved += 1
            continue
        if partner is not None and _is_phase_stack(devices[lower.id], lower) and not _is_phase_stack(
            devices[partner.id], partner
        ):
            stay = _body(lower, devices[lower.id])
            box = _body(partner, devices[partner.id])
            partner.x += stay[2] + _SIDE_GAP - box[0]
            moved += 1
            continue
        group = [lower]
        if rung:
            y0 = _rung_y(lower, devices[lower.id])
            for other in comps:
                if other is lower:
                    continue
                if abs(_rung_y(other, devices[other.id]) - y0) <= 4.0:
                    group.append(other)
        bottoms = [_body(comp, devices[comp.id])[3] + dy for comp in group]
        if max(bottoms) > floor:
            tops = [_body(comp, devices[comp.id])[1] for comp in comps]
            slack = min(tops) - (wy + 2.0)
            if slack > 1.0:
                lift = min(slack, max(bottoms) - floor)
                for comp in comps:
                    comp.y -= lift
                moved += 1
                continue
            dy = max(0.0, floor - _body(lower, devices[lower.id])[3])
            if dy < 0.4:
                break
        for comp in group:
            comp.y += dy
        moved += 1
    if moved:
        _drop_routes(sheet)
    return moved


def _is_phase_stack(device: Device, comp: Component) -> bool:
    """A multi-pole power symbol, or a polyphase load that ends the column.

    A motor has phase pins on the top and no load-side pins. It still belongs
    in the column under the overload.
    """

    if int(comp.rotation or 0) % 180 != 0:
        return False
    tops = [pin for pin in device.pins if pin.side == "top"]
    bots = [pin for pin in device.pins if pin.side == "bottom"]
    if len(tops) >= 2 and len(bots) >= 2:
        return True
    blob = f"{device.category} {comp.library_id} {comp.properties.get('role') or ''}".lower()
    return len(tops) >= 2 and "motor" in blob


def _is_terminal(device: Device, comp: Component) -> bool:
    cat = (device.category or "").lower()
    lib = comp.library_id.lower()
    if cat.startswith("connection.terminal") or ".terminals." in lib:
        return True
    if comp.properties.get("strip"):
        return True
    return False


def _skip(device: Device) -> bool:
    cat = (device.category or "").lower()
    return cat.startswith("connection.notes") or "offpage" in cat or "offpage" in (device.id or "")


def _rank(device: Device, comp: Component) -> int:
    blob = f"{device.category} {comp.library_id} {comp.properties.get('role') or ''}".lower()
    for key, rank in (
        ("disconnect", 0),
        ("breaker", 10),
        ("mpcb", 12),
        ("fuse", 20),
        ("contactor", 30),
        ("soft", 40),
        ("drive", 42),
        ("overload", 50),
        ("motor", 70),
    ):
        if key in blob:
            return rank
    return 60


def _chains(sheet: Sheet, stacks: list[Component], devices: dict[str, Device]) -> list[list[str]]:
    """One column per branch. Only a 1-to-1 load-to-line link joins a chain.

    A stack whose load side feeds two or more other stacks is a feeder. It
    stays in its own column (with a 1-to-1 upstream device, such as the
    disconnect ahead of the fuses). Each branch starts a new column.
    """

    ids = [comp.id for comp in stacks]
    parent = {cid: cid for cid in ids}
    links = _load_to_line_links(sheet, stacks, devices)
    fed: dict[str, set[str]] = {}
    feeders: dict[str, set[str]] = {}
    for src, dst in links:
        fed.setdefault(dst, set()).add(src)
        feeders.setdefault(src, set()).add(dst)

    def find(cid: str) -> str:
        while parent[cid] != cid:
            parent[cid] = parent[parent[cid]]
            cid = parent[cid]
        return cid

    def union(left: str, right: str) -> None:
        ra, rb = find(left), find(right)
        if ra != rb:
            parent[rb] = ra

    for src, dst in links:
        if len(feeders.get(src, ())) != 1:
            continue
        if len(fed.get(dst, ())) != 1:
            continue
        union(src, dst)
    groups: dict[str, list[str]] = {}
    for cid in ids:
        groups.setdefault(find(cid), []).append(cid)
    return list(groups.values())


def _load_to_line_links(
    sheet: Sheet,
    stacks: list[Component],
    devices: dict[str, Device],
) -> list[tuple[str, str]]:
    """(upstream, downstream) where a bottom pin lands on another stack's top pin."""

    known = {comp.id for comp in stacks}
    links: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for wire in sheet.wires:
        try:
            left, lp = parse_pin_ref(wire.frm)
            right, rp = parse_pin_ref(wire.to)
        except ValueError:
            continue
        if left not in known or right not in known or left == right:
            continue
        pair = _directed_link(left, lp, right, rp, devices)
        if pair is None or pair in seen:
            continue
        seen.add(pair)
        links.append(pair)
    return links


def _directed_link(
    left_id: str,
    left_pin: str,
    right_id: str,
    right_pin: str,
    devices: dict[str, Device],
) -> tuple[str, str] | None:
    ls = _pin_side(left_id, left_pin, devices)
    rs = _pin_side(right_id, right_pin, devices)
    if ls == "bottom" and rs == "top":
        return (left_id, right_id)
    if rs == "bottom" and ls == "top":
        return (right_id, left_id)
    return None


def _pin_side(comp_id: str, pin_id: str, devices: dict[str, Device]) -> str:
    device = devices.get(comp_id)
    if device is None:
        return ""
    try:
        return str(device.pin(pin_id).side or "")
    except KeyError:
        return ""


def _order_chain(members: list[str], sheet: Sheet, devices: dict[str, Device]) -> list[Component]:
    by_id = {comp.id: comp for comp in sheet.components}
    incoming = {cid: 0 for cid in members}
    edges: dict[str, list[str]] = {cid: [] for cid in members}
    known = set(members)
    for wire in sheet.wires:
        try:
            left, lp = parse_pin_ref(wire.frm)
            right, rp = parse_pin_ref(wire.to)
        except ValueError:
            continue
        if left not in known or right not in known or left == right:
            continue
        src, dst = _flow(by_id[left], devices[left], lp, by_id[right], devices[right], rp)
        if dst not in edges[src]:
            edges[src].append(dst)
            incoming[dst] += 1
    ready = [cid for cid in members if incoming[cid] == 0]
    ready.sort(key=lambda cid: (_rank(devices[cid], by_id[cid]), by_id[cid].y, cid))
    ordered: list[str] = []
    while ready:
        cur = ready.pop(0)
        ordered.append(cur)
        for nxt in edges[cur]:
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                ready.append(nxt)
        ready.sort(key=lambda cid: (_rank(devices[cid], by_id[cid]), by_id[cid].y, cid))
    for cid in sorted(members, key=lambda item: (_rank(devices[item], by_id[item]), by_id[item].y, item)):
        if cid not in ordered:
            ordered.append(cid)
    return [by_id[cid] for cid in ordered]


def _flow(
    left: Component,
    left_dev: Device,
    left_pin: str,
    right: Component,
    right_dev: Device,
    right_pin: str,
) -> tuple[str, str]:
    try:
        ls = pin_side_world(left, left_dev.pin(left_pin))
        rs = pin_side_world(right, right_dev.pin(right_pin))
    except KeyError:
        ls = rs = ""
    if ls == "bottom" and rs == "top":
        return left.id, right.id
    if rs == "bottom" and ls == "top":
        return right.id, left.id
    if _rank(left_dev, left) <= _rank(right_dev, right):
        return left.id, right.id
    return right.id, left.id


def _fit(chain: list[Component], devices: dict[str, Device], room: float) -> tuple[float, float]:
    """Absolute scale from the library height. A later Cleanup computes the same number."""

    heights = [float(devices[comp.id].schematic_height) for comp in chain]
    gaps = max(0, len(chain) - 1)
    for gap in (_STACK_GAP, 12.0, 10.0, _MIN_GAP):
        if sum(heights) + gap * gaps <= room:
            return 1.0, gap
    scale = (room - _MIN_GAP * gaps) / max(sum(heights), 1.0)
    return max(_MIN_SCALE, scale), _MIN_GAP


def _place_column(
    chain: list[Component],
    devices: dict[str, Device],
    phase_x: float,
    top: float,
    gap: float,
) -> None:
    y = top
    ref_mid: float | None = None
    ref_count = 0
    for comp in chain:
        device = devices[comp.id]
        scale = comp.visual_scale()
        pins = sorted((pin for pin in device.pins if pin.side == "top"), key=lambda pin: pin.x)
        if not pins:
            comp.x = phase_x
        elif ref_mid is None or len(pins) != ref_count:
            comp.x = phase_x - float(pins[0].x) * scale
            mid = pins[len(pins) // 2]
            ref_mid = comp.x + float(mid.x) * scale
            ref_count = len(pins)
        else:
            # The motor's 12 mm pitch and the overload's 10 mm pitch share the
            # middle phase, so only the outer leads jog, and only by a millimetre.
            mid = pins[len(pins) // 2]
            comp.x = ref_mid - float(mid.x) * scale
        comp.y = y
        y += float(device.schematic_height) * scale + gap


def _place_terminals(
    terminals: list[Component],
    devices: dict[str, Device],
    wx: float,
    wy: float,
    room: float,
) -> None:
    groups: dict[str, list[Component]] = {}
    for comp in terminals:
        groups.setdefault(_strip_name(comp), []).append(comp)
    x = wx + 10.0
    for name in sorted(groups):
        group = groups[name]
        group.sort(key=lambda comp: (_terminal_index(comp), comp.y, comp.id))
        scale, gap = _fit(group, devices, room)
        y = wy + _TOP_PAD
        width = 0.0
        for comp in group:
            comp.scale = scale
            comp.x = x
            comp.y = y
            device = devices[comp.id]
            width = max(width, float(device.schematic_width) * comp.visual_scale())
            y += float(device.schematic_height) * comp.visual_scale() + gap
        x += width + _COLUMN_GAP


def _place_attached_sides(
    sheet: Sheet,
    sides: list[Component],
    devices: dict[str, Device],
    placed: set[str],
    chain: list[Component],
    floor: float,
) -> None:
    anchor_ids = {comp.id for comp in chain}
    pending = [comp for comp in sides if comp.id not in placed]
    for _ in range(len(pending) + 1):
        progress = False
        for comp in list(pending):
            neighbors = [other for other in _neighbors(sheet, comp.id) if other in anchor_ids]
            if not neighbors:
                continue
            _place_beside(sheet, comp, devices, neighbors[0], floor, placed)
            placed.add(comp.id)
            anchor_ids.add(comp.id)
            pending.remove(comp)
            progress = True
        if not progress:
            break


def _place_beside(
    sheet: Sheet,
    comp: Component,
    devices: dict[str, Device],
    partner_id: str,
    floor: float,
    placed: set[str],
) -> None:
    partner = sheet.component(partner_id)
    device = devices[comp.id]
    pdevice = devices[partner_id]
    comp_pin, partner_pin = _link_pins(sheet, comp.id, partner_id, device, pdevice)
    scale = comp.visual_scale()
    pscale = partner.visual_scale()
    try:
        px, py = pin_world(partner, pdevice.pin(partner_pin))
        side = pin_side_world(partner, pdevice.pin(partner_pin))
        local = device.pin(comp_pin)
    except KeyError:
        comp.x = partner.x + float(pdevice.schematic_width) * pscale + _SIDE_GAP
        comp.y = partner.y
        return
    width = float(device.schematic_width) * scale
    height = float(device.schematic_height) * scale
    pwidth = float(pdevice.schematic_width) * pscale
    if side == "bottom" and py + 12.0 + height <= floor:
        comp.y = py + 12.0 - float(local.y) * scale
        comp.x = partner.x + pwidth + _SIDE_GAP - float(local.x) * scale
    elif side == "left":
        comp.y = py - float(local.y) * scale
        comp.x = partner.x - _SIDE_GAP - width
    else:
        comp.y = py - float(local.y) * scale
        comp.x = px + _SIDE_GAP - float(local.x) * scale
    obstacles = [_body(sheet.component(cid), devices[cid]) for cid in placed if cid in devices]
    for _ in range(16):
        box = _body(comp, device)
        hit = next((other for other in obstacles if _intersects(box, other, 1.0)), None)
        if hit is None and box[3] <= floor:
            break
        if hit is not None:
            comp.x += hit[2] + _SIDE_GAP - box[0]
        elif box[3] > floor:
            comp.y -= box[3] - floor


def _link_pins(
    sheet: Sheet,
    comp_id: str,
    partner_id: str,
    device: Device,
    pdevice: Device,
) -> tuple[str, str]:
    for wire in sheet.wires:
        try:
            left, lp = parse_pin_ref(wire.frm)
            right, rp = parse_pin_ref(wire.to)
        except ValueError:
            continue
        if {left, right} == {comp_id, partner_id}:
            if left == comp_id:
                return lp, rp
            return rp, lp
    return device.pins[0].id, pdevice.pins[0].id


def _neighbors(sheet: Sheet, comp_id: str) -> list[str]:
    found: list[str] = []
    for wire in sheet.wires:
        try:
            left, _lp = parse_pin_ref(wire.frm)
            right, _rp = parse_pin_ref(wire.to)
        except ValueError:
            continue
        if left == comp_id and right != comp_id:
            found.append(right)
        elif right == comp_id and left != comp_id:
            found.append(left)
    return found


def _strip_name(comp: Component) -> str:
    named = str(comp.properties.get("strip") or "").strip()
    if named:
        return named
    head, sep, tail = comp.id.rpartition("-")
    if sep and tail.isdigit() and head:
        return head
    return comp.id


def _terminal_index(comp: Component) -> int:
    raw = comp.properties.get("terminal")
    if raw not in (None, ""):
        try:
            return int(str(raw).strip())
        except ValueError:
            pass
    tail = comp.id.rpartition("-")[2]
    if tail.isdigit():
        return int(tail)
    return 10**6


def _content_right(comps: list[Component], devices: dict[str, Device], default: float) -> float:
    right = default
    for comp in comps:
        device = devices.get(comp.id)
        if device is None:
            continue
        right = max(right, _body(comp, device)[2])
    return right


def _body(comp: Component, device: Device) -> tuple[float, float, float, float]:
    scale = comp.visual_scale()
    return component_aabb(
        comp.x,
        comp.y,
        float(device.schematic_width) * scale,
        float(device.schematic_height) * scale,
        int(comp.rotation or 0),
        bool(comp.mirror),
    )


def _intersects(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    pad: float = 0.0,
) -> bool:
    ox = min(a[2], b[2]) - max(a[0], b[0])
    oy = min(a[3], b[3]) - max(a[1], b[1])
    return ox > pad and oy > pad


def _overlap_partner(
    comp: Component,
    comps: list[Component],
    devices: dict[str, Device],
) -> Component | None:
    box = _body(comp, devices[comp.id])
    for other in comps:
        if other is comp:
            continue
        if _intersects(box, _body(other, devices[other.id]), 0.4):
            return other
    return None


def _first_hit(
    comps: list[Component],
    devices: dict[str, Device],
) -> tuple[Component, float] | None:
    boxes = [(comp, _body(comp, devices[comp.id])) for comp in comps]
    for i, (left, la) in enumerate(boxes):
        for right, rb in boxes[i + 1 :]:
            if not _intersects(la, rb, 0.4):
                continue
            if rb[1] >= la[1]:
                lower, low, high = right, rb, la
            else:
                lower, low, high = left, la, rb
            overlap = min(high[3], low[3]) - max(high[1], low[1])
            return lower, max(overlap, 0.0) + 2.0
    return None


def count_rung_rows(sheet: Sheet, catalog: Catalog) -> int:
    """Rows that already hold two devices on one pin line.

    A rough placement (a coil and a button at unrelated heights) is not a
    rung, so Cleanup can still build the ladder. A sheet that already has
    two such rows keeps those rows.
    """

    devices, comps = _movable(sheet, catalog)
    if not comps:
        return 0
    return sum(1 for group in _cluster_rungs(comps, devices) if len(group) >= 2)


def reflow_existing_rungs(sheet: Sheet, catalog: Catalog) -> int:
    """Pack the rows already on the sheet. Does not invent a new ladder."""

    devices, comps = _movable(sheet, catalog)
    if len(comps) < 2:
        return 0
    return _pack_rungs(sheet, comps, devices)


def _reflow_rungs(sheet: Sheet, comps: list[Component], devices: dict[str, Device]) -> int:
    """Used after arrange_ladder. A sheet that already fits is left alone."""

    return _pack_rungs(sheet, comps, devices)


def _movable(sheet: Sheet, catalog: Catalog) -> tuple[dict[str, Device], list[Component]]:
    devices: dict[str, Device] = {}
    for comp in sheet.components:
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        if _skip(device):
            continue
        devices[comp.id] = device
    comps = [comp for comp in sheet.components if comp.id in devices]
    return devices, comps


def _cluster_rungs(comps: list[Component], devices: dict[str, Device]) -> list[list[Component]]:
    rungs: list[list[Component]] = []
    used: set[str] = set()
    ordered = sorted(comps, key=lambda comp: (_rung_y(comp, devices[comp.id]), comp.x, comp.id))
    for comp in ordered:
        if comp.id in used:
            continue
        y0 = _rung_y(comp, devices[comp.id])
        group = [other for other in comps if abs(_rung_y(other, devices[other.id]) - y0) <= 6.0]
        for other in group:
            used.add(other.id)
        rungs.append(group)
    return rungs


def _pack_rungs(sheet: Sheet, comps: list[Component], devices: dict[str, Device]) -> int:
    """Put each existing row between the rails. Scale is absolute, from library size."""

    from panelcad.core.ladder import rail_xs

    rungs = _cluster_rungs(comps, devices)
    if not rungs:
        return 0
    _wx, wy, _ww, wh = working_rect(sheet)
    top_pad = 8.0
    room = wh - top_pad - _BOTTOM_PAD
    left_rail, right_rail = rail_xs(sheet)
    inner_left = left_rail + 1.5
    inner_right = right_rail - 1.5
    spans = [_rung_span_at(group, devices, 1.0) for group in rungs]
    scale, gap = _fit_numbers(spans, room)
    rail_span = max(8.0, inner_right - inner_left)
    for group in rungs:
        widths = [float(devices[comp.id].schematic_width) for comp in group]
        gaps = max(0, len(group) - 1)
        need = sum(widths) + 4.0 * gaps
        if need > rail_span:
            scale = min(scale, max(_MIN_SCALE, (rail_span - 4.0 * gaps) / max(sum(widths), 1.0)))
    if _ladder_stable(sheet, comps, devices, scale, inner_left, inner_right):
        return 0
    for comp in comps:
        comp.scale = scale
    y = wy + top_pad
    index = 0
    while index < len(rungs):
        group = rungs[index]
        line = y + _ascent_at(group, devices, scale)
        for comp in group:
            comp.y = line - _pin_local_y(comp, devices[comp.id]) * scale
        wrapped = _fit_rung_inside(group, devices, inner_left, inner_right)
        boxes = [_body(comp, devices[comp.id]) for comp in group]
        if boxes:
            y = max(box[3] for box in boxes) + gap
        if wrapped:
            rungs.insert(index + 1, wrapped)
        index += 1
    _drop_routes(sheet)
    return len(comps)


def _ladder_stable(
    sheet: Sheet,
    comps: list[Component],
    devices: dict[str, Device],
    scale: float,
    inner_left: float,
    inner_right: float,
) -> bool:
    del scale
    if _first_hit(comps, devices) is not None:
        return False
    _wx, wy, _ww, wh = working_rect(sheet)
    for comp in comps:
        box = _body(comp, devices[comp.id])
        if box[0] < inner_left - 0.4 or box[2] > inner_right + 0.4:
            return False
        if box[1] < wy - 0.4 or box[3] > wy + wh + 0.4:
            return False
    return True


def _fit_numbers(heights: list[float], room: float) -> tuple[float, float]:
    gaps = max(0, len(heights) - 1)
    for gap in (_STACK_GAP, 8.0, 4.0, 2.0):
        if sum(heights) + gap * gaps <= room:
            return 1.0, gap
    for gap in (2.0, 1.0):
        scale = (room - gap * gaps) / max(sum(heights), 1.0)
        if scale >= _MIN_SCALE:
            return scale, gap
    return _MIN_SCALE, 1.0


def _rung_span_at(group: list[Component], devices: dict[str, Device], scale: float) -> float:
    top = 0.0
    bottom = 0.0
    for comp in group:
        device = devices[comp.id]
        pin_y = _pin_local_y(comp, device) * scale
        height = float(device.schematic_height) * scale
        if int(comp.rotation or 0) % 180 == 90:
            height = float(device.schematic_width) * scale
        top = min(top, -pin_y)
        bottom = max(bottom, height - pin_y)
    return bottom - top


def _ascent_at(group: list[Component], devices: dict[str, Device], scale: float) -> float:
    ascent = 0.0
    for comp in group:
        ascent = max(ascent, _pin_local_y(comp, devices[comp.id]) * scale)
    return ascent


def _fit_rung_inside(
    group: list[Component],
    devices: dict[str, Device],
    inner_left: float,
    inner_right: float,
) -> list[Component]:
    """Keep this row between the rails. Devices that no longer fit drop to the next row."""

    ordered = sorted(group, key=lambda comp: (comp.x, comp.id))
    kept: list[Component] = []
    wrapped: list[Component] = []
    for comp in ordered:
        _clamp_between(comp, devices[comp.id], inner_left, inner_right)
        box = _body(comp, devices[comp.id])
        nudged = False
        for other in kept:
            other_box = _body(other, devices[other.id])
            if not _intersects(box, other_box, 0.4):
                continue
            comp.x += other_box[2] + 3.0 - box[0]
            _clamp_between(comp, devices[comp.id], inner_left, inner_right)
            box = _body(comp, devices[comp.id])
            nudged = True
            if _intersects(box, other_box, 0.4):
                wrapped.append(comp)
                nudged = False
                break
        if nudged or comp not in wrapped:
            if comp not in wrapped:
                kept.append(comp)
    group[:] = kept
    return wrapped


def _clamp_between(
    comp: Component,
    device: Device,
    inner_left: float,
    inner_right: float,
) -> None:
    box = _body(comp, device)
    width = box[2] - box[0]
    if width >= inner_right - inner_left:
        comp.x += inner_left - box[0]
        return
    if box[0] < inner_left:
        comp.x += inner_left - box[0]
        box = _body(comp, device)
    if box[2] > inner_right:
        comp.x -= box[2] - inner_right


def _horiz_pin(device: Device):
    pins = [pin for pin in device.pins if pin.side in {"left", "right"}]
    return pins[0] if pins else device.pins[0]


def _pin_local_y(comp: Component, device: Device) -> float:
    return float(_horiz_pin(device).y)


def _rung_y(comp: Component, device: Device) -> float:
    pins = [pin for pin in device.pins if pin.side in {"left", "right"}]
    if not pins:
        return comp.y
    return sum(pin_world(comp, pin)[1] for pin in pins) / len(pins)


def _drop_routes(sheet: Sheet) -> None:
    from panelcad.render.wiring import clear_route_cache

    for wire in sheet.wires:
        wire.points = []
    clear_route_cache()
