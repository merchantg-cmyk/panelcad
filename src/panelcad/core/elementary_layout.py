"""Draw an elementary from facts. Positions on the way in are ignored.

Vision, OCR, and reconstruct may name devices, tags, wire numbers, and
connections. They do not choose where a symbol sits. This module is the
issued ladder: vertical rails, one straight rung per coil, a seal-in as a
short parallel under the start button, other auxiliaries on their own
rungs, and spare terminals in a column clear of the title block.
"""

from __future__ import annotations

from typing import Any

from panelcad.api.session import Session
from panelcad.core.models import Component, Sheet
from panelcad.library.catalog import Catalog

# A seal-in drop is this tall. A control wire longer than this is a wander.
_DROP_MM = 28.0
_PIN_TOL = 1.6
_GAP = 4.0
_TITLE_CLEAR = 4.0
_WIRE_CLEAR = 2.0


def _repair_conductors(session: Session, sheet: Sheet) -> None:
    """Land each wire on the pin that faces the other end. Drop a wire through one body.

    A reader that ties a device to itself, or to the far pin, draws the conductor
    across the symbol. The devices stay. The pin that faces the other end is the
    landing. No new rail tie is added.
    """

    from panelcad.core.ladder import endpoint_is_rail, rail_names
    from panelcad.render.sheet import parse_pin_ref
    from panelcad.render.wiring import clear_route_cache

    hot, ret = rail_names(sheet, session.project)
    by_id = {comp.id: comp for comp in sheet.components}
    kept = []
    for wire in sheet.wires:
        try:
            a_ref, a_pin = parse_pin_ref(wire.frm)
        except ValueError:
            a_ref, a_pin = str(wire.frm).split(".")[0], ""
        try:
            b_ref, b_pin = parse_pin_ref(wire.to)
        except ValueError:
            b_ref, b_pin = str(wire.to).split(".")[0], ""
        if a_ref and a_ref == b_ref and a_ref in by_id:
            # A wire across a device's own pins is a short. Keep it so the
            # checker can refuse the sheet. Do not retarget it into a bypass.
            kept.append(wire)
            continue
        a_rail = endpoint_is_rail(sheet, wire.frm, session.project)
        b_rail = endpoint_is_rail(sheet, wire.to, session.project)
        taken = _taken_ends(sheet.wires, wire.id)
        # A fuse or a transformer pin is not a control-rung landing. Moving the
        # rail wire onto the pin that faces L2 puts it on the primary and the
        # next wire is judged to tie that primary to the return.
        if a_rail and not b_rail and b_ref in by_id and not _power_device(by_id[b_ref]):
            faced = _facing_pin(session.catalog, by_id[b_ref], "left" if a_ref == hot else "right")
            landed = f"{b_ref}.{faced}" if faced else ""
            # The pin that faces the rail may already hold the primary. Moving
            # the rail wire onto it ties that primary to the return.
            if faced and faced != b_pin and landed not in taken:
                wire.to = landed
        elif b_rail and not a_rail and a_ref in by_id and not _power_device(by_id[a_ref]):
            faced = _facing_pin(session.catalog, by_id[a_ref], "left" if b_ref == hot else "right")
            landed = f"{a_ref}.{faced}" if faced else ""
            if faced and faced != a_pin and landed not in taken:
                wire.frm = landed
        wire.points = []
        kept.append(wire)
    sheet.wires = kept
    clear_route_cache()


def _separate_crossings(session: Session, sheet: Sheet) -> None:
    """A wire into the far pin of a device on the left is drawn through that body.

    Land it on the near pin instead. A wire that already uses the near pin stays.
    """

    from panelcad.render.sheet import parse_pin_ref, pin_world
    from panelcad.render.wiring import clear_route_cache

    by_id = {comp.id: comp for comp in sheet.components}
    for wire in sheet.wires:
        try:
            a_ref, a_pin = parse_pin_ref(wire.frm)
            b_ref, b_pin = parse_pin_ref(wire.to)
        except ValueError:
            continue
        if a_ref not in by_id or b_ref not in by_id or a_ref == b_ref:
            continue
        for ref, pin_id, other in ((b_ref, b_pin, a_ref), (a_ref, a_pin, b_ref)):
            comp = by_id[ref]
            device = _device(session.catalog, comp)
            other_comp = by_id[other]
            if device is None:
                continue
            landed = next((pin for pin in device.pins if str(pin.id) == pin_id), None)
            if landed is None or str(landed.side) != "right":
                continue
            px, py = pin_world(comp, landed)
            if other_comp.x > px:
                continue
            # A branch that leaves the rung vertically is not a wire through the body.
            other_dev = _device(session.catalog, other_comp)
            if other_dev is None:
                continue
            _ox, oy = pin_world(other_comp, _pin_named(other_dev, "", "left"))
            if abs(oy - py) > 8.0:
                continue
            near = _facing_pin(session.catalog, comp, "left")
            if not near or near == pin_id:
                continue
            if ref == b_ref:
                wire.to = f"{b_ref}.{near}"
            else:
                wire.frm = f"{a_ref}.{near}"
            wire.points = []
    clear_route_cache()


def _taken_ends(wires: list[Any], skip: str) -> set[str]:
    taken: set[str] = set()
    for other in wires:
        if str(getattr(other, "id", "") or "") == skip:
            continue
        taken.add(str(other.frm))
        taken.add(str(other.to))
    return taken


def _power_device(comp: Component) -> bool:
    lib = str(comp.library_id or "").lower()
    return "fuse" in lib or "transformer" in lib


def _facing_pin(catalog: Catalog, comp: Component, side: str) -> str:
    device = _device(catalog, comp)
    if device is None:
        return ""
    pins = [pin for pin in device.pins if str(pin.side) == side]
    return str(pins[0].id) if pins else ""


def layout_elementary(session: Session, sheet: Sheet) -> int:
    """Replace symbol positions from the wire list. Wires and tags stay."""

    _repair_conductors(session, sheet)
    moved = _layout(session, sheet, snap=True)
    if elementary_layout_issues(session, sheet):
        moved = _layout(session, sheet, snap=False)
    _separate_crossings(session, sheet)
    return moved


def _layout(session: Session, sheet: Sheet, *, snap: bool) -> int:
    catalog = session.catalog
    comps = [comp for comp in sheet.components if _device(catalog, comp) is not None]
    if len(comps) < 2:
        return 0
    by_id = {comp.id: comp for comp in comps}
    devices = {comp.id: _device(catalog, comp) for comp in comps}
    adj = _graph(sheet, by_id)
    for wire in sheet.wires:
        wire.points = []
    placed: set[str] = set()
    moved = _place_power(sheet, session, by_id, devices, placed)
    primary_return = _route_primary_return(session, sheet, by_id, devices)
    floor = _prev_bottom(by_id, devices, placed)
    if primary_return is not None:
        floor = max(floor, primary_return + 3.0)
    rungs = _rungs(sheet, by_id, devices, adj, placed)
    for series, parallel in rungs:
        y, floor = _rung_y(session, sheet, by_id, devices, series, floor, snap=snap)
        moved += _place_row(sheet, by_id, devices, series, y)
        for aux_id, under_id in parallel:
            drop = _drop(devices[under_id], devices[aux_id])
            moved += _place_on(by_id[aux_id], devices[aux_id], by_id[under_id].x, y + drop)
            placed.add(aux_id)
            floor = max(floor, _bottom(by_id[aux_id], devices[aux_id]))
        placed.update(series)
        floor = max(floor, _prev_bottom(by_id, devices, set(series)))
    moved += _place_terminals(sheet, session, by_id, devices, adj, placed, floor)
    _arch_blocked_wires(session, sheet, by_id)
    return moved


def scrub_illegal_wire_numbers(session: Session) -> int:
    """A mark OCR did not read as a sheet/line number stays ``?``.

    Two digits such as ``07`` are not a ladder number. They are not guessed
    into a nearby legal number.
    """

    from panelcad.core.ansi_drafting import _wire_number_ok

    changed = 0
    for sheet in session.project.sheets:
        if getattr(sheet, "kind", "") != "schematic":
            continue
        for wire in sheet.wires:
            number = str(wire.number or "").strip()
            if number and not _wire_number_ok(number):
                wire.number = "?"
                if str(wire.label or "") == number:
                    wire.label = "?"
                changed += 1
    return changed


def issue_project(session: Session) -> int:
    """Redraw every messy control ladder on this project. Clean sheets stay put."""

    issued = 0
    for sheet in session.project.sheets:
        if getattr(sheet, "kind", "") == "schematic" and issue_elementary(session, sheet):
            issued += 1
    return issued


def issue_elementary(session: Session, sheet: Sheet, *, force: bool = False) -> bool:
    """Redraw a control ladder. Keep the sheet when the redraw is not clean.

    Cleanup only redraws a sheet that already fails the gate. Scan-to-print
    passes force=True so the engine draws the sheet even when the synthesizer
    had already made a straight rung.
    """

    if not _is_control_elementary(sheet):
        return False
    if not force and not elementary_layout_issues(session, sheet):
        return False
    saved = [(comp, comp.x, comp.y, comp.rotation) for comp in sheet.components]
    wires = [wire.model_copy(deep=True) for wire in sheet.wires]
    layout_elementary(session, sheet)
    if elementary_layout_issues(session, sheet):
        for comp, x, y, rotation in saved:
            comp.x, comp.y, comp.rotation = x, y, rotation
        sheet.wires = wires
        return False
    return True


def elementary_layout_issues(session: Session, sheet: Sheet) -> list[str]:
    """Defects a drafter would reject on a ladder elementary.

    An empty list means the sheet is a straight-rung ladder. The check does
    not look at panel or terminal sheets.
    """

    from panelcad.core.ladder import is_ladder_sheet
    from panelcad.render.wiring import clear_route_cache

    if not is_ladder_sheet(sheet, session.project):
        return []
    clear_route_cache()
    catalog = session.catalog
    issues: list[str] = []
    issues.extend(_off_rung(sheet, catalog))
    issues.extend(_bad_routes(session, sheet, catalog))
    issues.extend(_stacked_notes(session, sheet))
    issues.extend(_body_problems(session, sheet, catalog))
    issues.extend(_terminal_gate(session, sheet, catalog))
    issues.extend(_loads_on_the_right(session, sheet, catalog))
    issues.extend(_own_pin_wires(sheet))
    issues.extend(_contacts_only_rungs(sheet))
    issues.extend(_fuse_across_rails(sheet))
    issues.extend(_rail_without_a_load(sheet))
    issues.extend(_rail_gaps(session, sheet, catalog))
    issues.extend(_shorted_numbers(session, sheet))
    return issues


def _is_control_elementary(sheet: Sheet) -> bool:
    control = power = 0
    for comp in sheet.components:
        lib = comp.library_id.lower()
        if "motor" in lib or "ac3_" in lib or ("overload" in lib and "ol_contact" not in lib):
            power += 1
        if any(token in lib for token in ("pb_", "estop", "coil", "pilot", "aux_")):
            control += 1
    return control > 0 and power == 0


def _place_power(sheet, session, by_id, devices, placed) -> int:
    transformer = next((comp for comp in by_id.values() if _role(comp.library_id) == "transformer"), None)
    if transformer is None:
        return 0
    device = devices[transformer.id]
    from panelcad.core.zones import rung_y

    y = rung_y(sheet, 1, project=session.project)
    anchor = _left_x(sheet) + 78.0
    fuses = [comp for comp in by_id.values() if comp.id not in placed and "fuse" in comp.library_id.lower()]
    fuses.sort(key=lambda comp: comp.id)
    if any(not _pin_side_wired(sheet, comp.id, devices[comp.id], "left") for comp in fuses):
        from panelcad.core.ladder import rail_xs

        left, _right = rail_xs(sheet)
        fuse_w = max(float(devices[comp.id].schematic_width) for comp in fuses)
        anchor = max(anchor, left + 56.0 + fuse_w + 14.0)
    moved = _place_pin(transformer, device, _pin_named(device, "H1", "left"), anchor, y)
    placed.add(transformer.id)
    h1 = _pin_named(device, "H1", "left")
    h4 = _pin_named(device, "H4", "left")
    lower = y + float(h4.y) - float(h1.y)
    if fuses:
        moved += _place_left_of(fuses[0], devices[fuses[0].id], transformer, y)
        placed.add(fuses[0].id)
    if len(fuses) > 1:
        moved += _place_left_of(fuses[1], devices[fuses[1].id], transformer, lower)
        placed.add(fuses[1].id)
    return moved


def _rungs(sheet, by_id, devices, adj, placed) -> list[tuple[list[str], list[tuple[str, str]]]]:
    outputs = [
        comp.id
        for comp in by_id.values()
        if comp.id not in placed and _role(comp.library_id) == "output"
    ]
    claimed: set[str] = set(placed)
    groups: list[tuple[str, set[str]]] = []
    for output in outputs:
        nodes = _reachable(output, by_id, adj, claimed)
        groups.append((output, nodes))
        claimed.update(nodes)
    for comp in by_id.values():
        if comp.id in claimed or _role(comp.library_id) in {"terminal", "transformer"}:
            continue
        nodes = _reachable(comp.id, by_id, adj, claimed)
        groups.append((comp.id, nodes))
        claimed.update(nodes)
    rungs: list[tuple[list[str], list[tuple[str, str]]]] = []
    for output, nodes in groups:
        series, parallel = _split_parallel(sheet, output, nodes, by_id, adj)
        rungs.append((series, parallel))
    rungs.sort(key=lambda item: _rung_key(item[0], by_id))
    return _park_unwired_holding_contacts(by_id, adj, rungs)


def _reachable(origin: str, by_id, adj, claimed: set[str]) -> set[str]:
    seen = {origin}
    stack = [origin]
    while stack:
        node = stack.pop()
        for nbr in adj.get(node, ()):
            if nbr in seen or nbr in claimed:
                continue
            if _role(by_id[nbr].library_id) in {"terminal", "transformer", "output"} and nbr != origin:
                continue
            seen.add(nbr)
            stack.append(nbr)
    return seen


def _split_parallel(sheet, output: str, nodes: set[str], by_id, adj) -> tuple[list[str], list[tuple[str, str]]]:
    series_nodes = set(nodes)
    parallel: list[tuple[str, str]] = []
    for ref in list(nodes):
        if _role(by_id[ref].library_id) != "aux":
            continue
        nbrs = [nbr for nbr in adj.get(ref, ()) if nbr in nodes and nbr != ref]
        if len(nbrs) < 2:
            continue
        if _connected(nbrs[0], nbrs[1], nodes - {ref}, adj):
            under = _under_contact(nbrs, by_id, nodes - {ref}, adj)
            parallel.append((ref, under))
            series_nodes.discard(ref)
    series = _path_order(series_nodes, adj, by_id, output, sheet=sheet)
    return series, parallel


def _connected(start: str, goal: str, nodes: set[str], adj) -> bool:
    if start == goal:
        return True
    seen = {start}
    stack = [start]
    while stack:
        node = stack.pop()
        for nbr in adj.get(node, ()):
            if nbr not in nodes or nbr in seen:
                continue
            if nbr == goal:
                return True
            seen.add(nbr)
            stack.append(nbr)
    return False


def _path_order(nodes: set[str], adj, by_id, output: str, sheet: Sheet | None = None) -> list[str]:
    if not nodes:
        return [output] if output else []
    node_set = set(nodes)

    def nbrs(ref: str) -> list[str]:
        return [nbr for nbr in adj.get(ref, ()) if nbr in node_set]

    ends = [ref for ref in nodes if len(nbrs(ref)) <= 1]
    ranked = sorted(ends or list(nodes), key=lambda ref: (_contact_rank(by_id[ref].library_id), ref))
    hot = str(getattr(sheet, "rail_hot", "") or "L1") if sheet is not None else "L1"
    facing = [ref for ref in ranked if _touches_rail(sheet, ref, hot)]
    start = facing[0] if facing else ranked[0]
    ordered = [start]
    prev: str | None = None
    cur = start
    while len(ordered) < len(nodes):
        nxts = [nbr for nbr in nbrs(cur) if nbr != prev and nbr not in ordered]
        if not nxts:
            break
        nxt = nxts[0]
        ordered.append(nxt)
        prev, cur = cur, nxt
    for ref in sorted(nodes, key=lambda ref: ref):
        if ref not in ordered:
            ordered.append(ref)
    return ordered


def _touches_rail(sheet: Sheet | None, ref: str, rail: str) -> bool:
    if sheet is None:
        return False
    from panelcad.render.sheet import parse_pin_ref

    def end_ref(text: str) -> str:
        try:
            parsed, _pin = parse_pin_ref(text)
        except ValueError:
            parsed = str(text).split(".")[0]
        return parsed

    for wire in sheet.wires:
        ends = {end_ref(wire.frm), end_ref(wire.to)}
        if ref in ends and rail in ends:
            return True
    return False


def _park_unwired_holding_contacts(by_id, adj, rungs):
    """An unread holding contact sits under the start. It is not its own rung.

    The contact was read. No wire is added. A contact that already has a wire
    stays on the rung that wire defines.
    """

    wired = {ref for ref, nbrs in adj.items() if nbrs}
    starts = [
        ref
        for ref, comp in by_id.items()
        if "pb_no" in comp.library_id.lower() and "aux" not in comp.library_id.lower()
    ]
    if len(starts) != 1:
        return rungs
    start = starts[0]
    holding = [
        ref
        for ref, comp in by_id.items()
        if ref not in wired and _role(comp.library_id) == "aux" and str(ref).upper().endswith("-13")
    ]
    if not holding:
        return rungs
    held = set(holding)
    cleaned: list[tuple[list[str], list[tuple[str, str]]]] = []
    for series, parallel in rungs:
        series = [ref for ref in series if ref not in held]
        parallel = [(aux, under) for aux, under in parallel if aux not in held]
        if series:
            cleaned.append((series, parallel))
    attached = False
    parked: list[tuple[list[str], list[tuple[str, str]]]] = []
    for series, parallel in cleaned:
        if not attached and (start in series or any(under == start for _aux, under in parallel)):
            parallel = list(parallel) + [(ref, start) for ref in holding]
            attached = True
        parked.append((series, parallel))
    if not attached:
        parked.append(([start], [(ref, start) for ref in holding]))
    return parked


def _under_contact(nbrs: list[str], by_id, nodes: set[str] | None = None, adj=None) -> str:
    """The button a parallel contact sits under.

    A seal-in that lands on the coil pin, not on the start button, still
    belongs under that button when the button is the device between the
    two landings.
    """

    for ref in nbrs:
        lib = by_id[ref].library_id.lower()
        if "pb_no" in lib and "aux" not in lib:
            return ref
    if nodes is not None and adj is not None and len(nbrs) >= 2:
        for ref in _path_between(nbrs[0], nbrs[1], nodes, adj):
            if ref not in by_id:
                continue
            lib = by_id[ref].library_id.lower()
            if "pb_no" in lib and "aux" not in lib:
                return ref
    return nbrs[0]


def _path_between(start: str, goal: str, nodes: set[str], adj) -> list[str]:
    parent: dict[str, str | None] = {start: None}
    queue = [start]
    while queue:
        node = queue.pop(0)
        if node == goal:
            break
        for nbr in adj.get(node, ()):
            if nbr not in nodes or nbr in parent:
                continue
            parent[nbr] = node
            queue.append(nbr)
    if goal not in parent:
        return []
    path: list[str] = []
    cur: str | None = goal
    while cur is not None:
        path.append(cur)
        cur = parent.get(cur)
    return path


def _rung_key(series: list[str], by_id) -> tuple:
    libs = " ".join(by_id[ref].library_id.lower() for ref in series)
    power = 0 if ("fuse" in libs or "timer" in libs or "transformer" in libs) else 1
    only_aux = bool(series) and all("aux" in by_id[ref].library_id.lower() for ref in series)
    field = 0 if only_aux else 1
    starter = 0 if ("estop" in libs or "pb_no" in libs or "pb_nc" in libs) else 1
    output = next((ref for ref in series if _role(by_id[ref].library_id) == "output"), series[-1] if series else "")
    kind = 0 if _role(by_id[output].library_id) == "output" else 1
    return (field, power, starter, kind, _output_rank(by_id[output].library_id), output)


def _rung_y(session, sheet, by_id, devices, series, floor: float, *, snap: bool) -> tuple[float, float]:
    above = 0.0
    below = 0.0
    for ref in series:
        pin = _pin_named(devices[ref], "", "left")
        above = max(above, float(pin.y))
        below = max(below, float(devices[ref].schematic_height) - float(pin.y))
    minimum = floor + _GAP + above
    y = _fit_pin_y(session, sheet, minimum, below) if snap else minimum
    return y, floor


def _column_pin_x(sheet: Sheet, width: float = 16.0) -> float:
    from panelcad.core.ladder import rail_xs

    _left, right = rail_xs(sheet)
    return right - 8.0 - width


def _place_terminals(sheet, session, by_id, devices, adj, placed, floor: float) -> int:
    """Terminals sit on a real rung, in tag order, each with a straight drop to L2.

    Spares share the rung of the terminal a contact feeds. The tie between
    them is that rung, not a loop around the blocks.
    """

    loose = [comp for comp in by_id.values() if comp.id not in placed and _role(comp.library_id) == "terminal"]
    loose.sort(key=lambda comp: comp.id)
    if not loose:
        return 0
    from panelcad.render.sheet import pin_world
    from panelcad.render.titleblock import working_rect

    sample = devices[loose[0].id]
    width = float(sample.schematic_width)
    half = float(sample.schematic_height) / 2.0
    from panelcad.core.ladder import rail_names, rail_xs

    _left_rail, right_rail = rail_xs(sheet)
    x_right = _column_pin_x(sheet, width)
    # Inset from the rail unless this terminal's wire actually lands on it.
    # A pin id that happens to sit on the right is not that landing: the next
    # redraw would retarget the pin and shift the whole column.
    _hot, ret = rail_names(sheet, session.project)
    if loose and not _lands_on_rail(sheet, loose[-1].id, ret):
        x_right = min(x_right, right_rail - 56.0 - width)
    _wx, wy, _ww, wh = working_rect(sheet)
    limit = wy + wh - _TITLE_CLEAR - half
    hosted_y = None
    for comp in loose:
        host = next((nbr for nbr in adj.get(comp.id, ()) if nbr in placed), None)
        if host is None:
            continue
        _hx, hosted_y = pin_world(by_id[host], _pin_named(devices[host], "", "left"))
        break
    pin_y = hosted_y if hosted_y is not None else min(floor + _GAP + half, limit)
    pin_y = min(pin_y, limit)
    pitch = width + 14.0
    x = x_right - pitch * (len(loose) - 1)
    moved = 0
    for comp in loose:
        moved += _place_on(comp, devices[comp.id], x, pin_y)
        comp.properties["ref_side"] = "left"
        placed.add(comp.id)
        x += pitch
    return moved


def _spare_row_y(rung_ys: list[float], half: float, limit: float) -> float:
    """The gap wide enough for a terminal body between two rung wires."""

    best = rung_ys[-1] + half + 8.0 if rung_ys else half + 40.0
    best_room = -1.0
    for left, right in zip(rung_ys, rung_ys[1:], strict=False):
        room = right - left
        if room <= best_room:
            continue
        mid = (left + right) / 2.0
        if mid - half < left + 4.0 or mid + half > right - 4.0 or mid > limit:
            continue
        best, best_room = mid, room
    return min(best, limit)


def _dress_terminal_wires(session, sheet, by_id, devices) -> None:
    """One jumper bar above a spare row, and one bus below it to L2."""

    from panelcad.core.models import Point
    from panelcad.render.sheet import parse_pin_ref
    from panelcad.render.wiring import wire_ends

    terminals = [comp for comp in by_id.values() if _role(comp.library_id) == "terminal"]
    if len(terminals) < 2:
        return
    rows: dict[float, list[Component]] = {}
    for comp in terminals:
        key = round(_pin_y(comp, devices[comp.id]), 1)
        rows.setdefault(key, []).append(comp)
    for group in rows.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda comp: comp.x)
        top = min(float(comp.y) for comp in group)
        bot = max(float(comp.y) + float(devices[comp.id].schematic_height) for comp in group)
        bar = top - 4.0
        bus = bot + 4.0
        rightmost = group[-1].id
        pin_x = {}
        for comp in group:
            pin = _pin_named(devices[comp.id], "A", "left")
            from panelcad.render.sheet import pin_world

            pin_x[comp.id] = pin_world(comp, pin)[0]
        for wire in sheet.wires:
            try:
                a_ref, _pa = parse_pin_ref(wire.frm)
            except ValueError:
                a_ref = str(wire.frm).split(".")[0]
            try:
                b_ref, _pb = parse_pin_ref(wire.to)
            except ValueError:
                b_ref = str(wire.to).split(".")[0]
            ends = {a_ref, b_ref}
            if not ends & {comp.id for comp in group}:
                continue
            ax, ay, _aside, bx, by, _bside = wire_ends(sheet, session.catalog, wire, session.project)
            both = a_ref in pin_x and b_ref in pin_x
            if both and abs(pin_x[a_ref] - pin_x[b_ref]) > float(devices[a_ref].schematic_width) + 14.0:
                wire.points = [Point(x=ax, y=bar), Point(x=bx, y=bar)]
                continue
            to_rail = a_ref not in pin_x or b_ref not in pin_x
            device = a_ref if a_ref in pin_x else b_ref
            if to_rail and device != rightmost and device in pin_x:
                device_x = ax if a_ref == device else bx
                rail_x = bx if a_ref == device else ax
                wire.points = [Point(x=device_x, y=bus), Point(x=rail_x, y=bus)]


def _route_primary_return(session, sheet, by_id, devices) -> float | None:
    """One horizontal under the transformer, from the primary fuse out to L2."""

    from panelcad.core.models import Point
    from panelcad.render.sheet import parse_pin_ref
    from panelcad.render.wiring import wire_ends

    transformer = next((comp for comp in by_id.values() if _role(comp.library_id) == "transformer"), None)
    if transformer is None:
        return None
    under = float(transformer.y) + float(devices[transformer.id].schematic_height) + 6.0
    for wire in sheet.wires:
        try:
            a_ref, _pa = parse_pin_ref(wire.frm)
        except ValueError:
            a_ref = str(wire.frm).split(".")[0]
        try:
            b_ref, _pb = parse_pin_ref(wire.to)
        except ValueError:
            b_ref = str(wire.to).split(".")[0]
        ends = {a_ref, b_ref}
        fuse = next((ref for ref in ends if ref in by_id and _role(by_id[ref].library_id) == "fuse"), None)
        rail = next((ref for ref in ends if ref not in by_id), None)
        if fuse is None or rail is None:
            continue
        ax, ay, a_side, bx, by, b_side = wire_ends(sheet, session.catalog, wire, session.project)
        device_x, rail_x = (ax, bx) if a_ref == fuse else (bx, ax)
        if rail_x <= device_x:
            continue
        pin_y = ay if a_ref == fuse else by
        pin_side = a_side if a_ref == fuse else b_side
        # A pin that faces the rail drops straight to the channel. A pin on the
        # far side leaves outward first, so the drop does not enter the body.
        rail_side = "right" if rail_x > device_x else "left"
        if pin_side == rail_side:
            wire.points = [Point(x=device_x, y=under), Point(x=rail_x, y=under)]
            continue
        sign = -1.0 if pin_side == "left" else 1.0
        exit_x = device_x + sign * 6.0
        wire.points = [
            Point(x=exit_x, y=pin_y),
            Point(x=exit_x, y=under),
            Point(x=rail_x, y=under),
        ]
    return under


def _arch_blocked_wires(session, sheet, by_id) -> None:
    """Lift a straight wire over a symbol it would otherwise cut."""

    from panelcad.core.models import Point
    from panelcad.core.route import path_hits_obstacles
    from panelcad.render.sheet import parse_pin_ref
    from panelcad.render.wiring import component_obstacle, wire_ends

    boxes = [(comp.id, component_obstacle(sheet, session.catalog, comp)) for comp in by_id.values()]
    for wire in sheet.wires:
        if wire.points:
            continue
        try:
            a_ref, _pa = parse_pin_ref(wire.frm)
        except ValueError:
            a_ref = str(wire.frm).split(".")[0]
        try:
            b_ref, _pb = parse_pin_ref(wire.to)
        except ValueError:
            b_ref = str(wire.to).split(".")[0]
        ends = {a_ref, b_ref}
        if any(ref in by_id and _role(by_id[ref].library_id) == "terminal" for ref in ends):
            continue
        ax, ay, _aside, bx, by, _bside = wire_ends(sheet, session.catalog, wire, session.project)
        straight = [Point(x=ax, y=ay), Point(x=bx, y=by)]
        blocked = [box for ref, box in boxes if ref not in ends and path_hits_obstacles(straight, [box], skip_ends=True)]
        if not blocked:
            continue
        above = min(box[1] for box in blocked) - 4.0
        below = max(box[3] for box in blocked) + 4.0
        for y in (above, below):
            arch = [Point(x=ax, y=ay), Point(x=ax, y=y), Point(x=bx, y=y), Point(x=bx, y=by)]
            if any(path_hits_obstacles(arch, [box], skip_ends=True) for _ref, box in boxes if _ref not in ends):
                continue
            wire.points = [Point(x=ax, y=y), Point(x=bx, y=y)]
            break


def _is_rung_load(library_id: str) -> bool:
    """Coil, relay, timer, pilot, or the overload that sits with the coil at L2."""

    lib = library_id.lower()
    return _role(lib) == "output" or "ol_contact" in lib


def _lands_on_rail(sheet: Sheet, ref: str, rail: str) -> bool:
    """True when a wire from ``ref`` ends on the named rail, not merely on a right-hand pin."""

    want = str(rail or "").strip().upper()
    if not want:
        return False
    for wire in sheet.wires:
        ends = [str(wire.frm or ""), str(wire.to or "")]
        refs = {end.split(".")[0].upper() for end in ends}
        if ref.upper() in refs and want in refs:
            return True
    return False


def _pin_side_wired(sheet: Sheet, ref: str, device, side: str) -> bool:
    from panelcad.render.sheet import parse_pin_ref

    pins = {str(pin.id): str(pin.side) for pin in device.pins}
    for wire in sheet.wires:
        for text in (wire.frm, wire.to):
            try:
                other, pin = parse_pin_ref(text)
            except ValueError:
                continue
            if other == ref and pins.get(pin) == side:
                return True
    return False


def _wired_to_right_pin(sheet: Sheet, ref: str, by_id, devices) -> bool:
    """True when ``ref`` is tied to another load's right-hand port."""

    from panelcad.render.sheet import parse_pin_ref

    for wire in sheet.wires:
        try:
            a_ref, a_pin = parse_pin_ref(wire.frm)
            b_ref, b_pin = parse_pin_ref(wire.to)
        except ValueError:
            continue
        if ref not in {a_ref, b_ref}:
            continue
        other, pin = (b_ref, b_pin) if a_ref == ref else (a_ref, a_pin)
        if other not in by_id or not _is_rung_load(by_id[other].library_id):
            continue
        device = devices.get(other)
        if device is None:
            continue
        for item in device.pins:
            if str(item.id) == pin and str(item.side) == "right":
                return True
    return False


def _place_row(sheet, by_id, devices, refs: list[str], y: float) -> int:
    """Contacts from the left rail. A load with a return wire sits against L2.

    A device with no wire on the rail side is kept clear of that rail. The
    gap is not drawn as a landing the scan did not read.
    """

    from panelcad.core.ladder import rail_xs

    left, right = rail_xs(sheet)
    loads = [ref for ref in refs if _is_rung_load(by_id[ref].library_id)]
    contacts = [ref for ref in refs if ref not in loads]
    at_rail = [ref for ref in loads if _pin_side_wired(sheet, ref, devices[ref], "right")]
    # An overload wired to the coil's right pin sits between that coil and L2.
    # No rail wire is added. The confirmed edge is what places it.
    return_side = [ref for ref in loads if ref not in at_rail and _wired_to_right_pin(sheet, ref, by_id, devices)]
    held = [ref for ref in loads if ref not in at_rail and ref not in return_side]
    edge = right - 14.0
    moved = 0
    for ref in return_side:
        device = devices[ref]
        width = float(device.schematic_width)
        edge -= width
        moved += _place_on(by_id[ref], device, edge, y)
        edge -= 6.0
    for ref in reversed(at_rail):
        device = devices[ref]
        width = float(device.schematic_width)
        edge -= width
        moved += _place_on(by_id[ref], device, edge, y)
        edge -= 10.0
    row = [*contacts, *held]
    origin = left + 18.0
    if row and not _pin_side_wired(sheet, row[0], devices[row[0]], "left"):
        origin = left + 56.0
    x = origin
    limit = edge - 8.0 if at_rail else right - 56.0
    for ref in row:
        device = devices[ref]
        width = float(device.schematic_width)
        if x + width > limit:
            x = origin
        moved += _place_on(by_id[ref], device, x, y)
        x += width + 12.0
    return moved


def _place_on(comp: Component, device: Any, x: float, y: float) -> int:
    pin = _pin_named(device, "", "left")
    return _place_pin(comp, device, pin, x + float(pin.x), y)


def _place_left_of(comp, device, anchor, y: float) -> int:
    width = float(device.schematic_width)
    return _place_on(comp, device, anchor.x - width - 14.0, y)


def _place_pin(comp: Component, device: Any, pin: Any, x: float, y: float) -> int:
    comp.rotation = 0  # type: ignore[assignment]
    nx = x - float(pin.x)
    ny = y - float(pin.y)
    moved = int(abs(comp.x - nx) > 0.5 or abs(comp.y - ny) > 0.5)
    comp.x, comp.y = nx, ny
    return moved


def _pin_named(device: Any, name: str, side: str):
    if name:
        try:
            return device.pin(name)
        except KeyError:
            pass
    pins = [pin for pin in device.pins if str(pin.side) == side]
    if pins:
        return pins[0]
    return device.pins[0]


def _left_x(sheet: Sheet) -> float:
    from panelcad.core.ladder import rail_xs

    return rail_xs(sheet)[0]


def _fit_pin_y(session: Session, sheet: Sheet, minimum: float, below: float) -> float:
    """Use the next rung line when the symbol still clears the title block."""

    from panelcad.render.titleblock import working_rect

    _wx, wy, _ww, wh = working_rect(sheet)
    limit = wy + wh - _TITLE_CLEAR
    snapped = _snap(session, sheet, minimum)
    if snapped + below <= limit + 0.2:
        return snapped
    return minimum


def _snap(session: Session, sheet: Sheet, y: float) -> float:
    from panelcad.core.zones import rung_index_at, rung_y

    index = max(1, rung_index_at(sheet, y, project=session.project))
    snapped = rung_y(sheet, index, project=session.project)
    if snapped < y - 0.4:
        snapped = rung_y(sheet, index + 1, project=session.project)
    return snapped


def _bottom(comp: Component, device: Any) -> float:
    return float(comp.y) + float(device.schematic_height)


def _pin_y(comp: Component, device: Any) -> float:
    from panelcad.render.sheet import pin_world

    return pin_world(comp, _pin_named(device, "", "left"))[1]


def _prev_bottom(by_id, devices, refs: set[str]) -> float:
    if not refs:
        return 36.0
    return max(_bottom(by_id[ref], devices[ref]) for ref in refs)


def _drop(under, aux) -> float:
    under_pin = _pin_named(under, "", "left")
    aux_pin = _pin_named(aux, "", "left")
    below = float(under.schematic_height) - float(under_pin.y)
    return below + _GAP + float(aux_pin.y)


def _graph(sheet: Sheet, by_id: dict[str, Component]) -> dict[str, set[str]]:
    from panelcad.render.sheet import parse_pin_ref

    adj: dict[str, set[str]] = {ref: set() for ref in by_id}
    for wire in sheet.wires:
        try:
            a, _pa = parse_pin_ref(wire.frm)
            b, _pb = parse_pin_ref(wire.to)
        except ValueError:
            continue
        if a in adj and b in adj and a != b:
            adj[a].add(b)
            adj[b].add(a)
    return adj


def _device(catalog: Catalog, comp: Component):
    try:
        return catalog.get(comp.library_id)
    except KeyError:
        return None


def _role(library_id: str) -> str:
    lib = library_id.lower()
    if "control_transformer" in lib or (".transformer" in lib) or lib.endswith("transformer"):
        return "transformer"
    if "fuse" in lib:
        return "fuse"
    if "terminal" in lib:
        return "terminal"
    if "offpage" in lib:
        return "offpage"
    if "aux_" in lib or ".aux" in lib:
        return "aux"
    if "ol_contact" in lib:
        return "contact"
    # A timer contact stays on the left. The timer coil (on-delay, off-delay)
    # and a control-relay circle are loads, same as a contactor coil.
    if "contact" in lib and "coil" not in lib:
        return "contact"
    if "coil" in lib or "pilot" in lib or "lamp" in lib or "relays." in lib or ".timers." in lib:
        return "output"
    return "contact"


def _contact_rank(library_id: str) -> int:
    lib = library_id.lower()
    if "estop" in lib:
        return 0
    if "pb_nc" in lib:
        return 1
    if "pb_no" in lib:
        return 2
    if "offpage" in lib:
        return 0
    if "ol_contact" in lib:
        return 4
    if "aux" in lib:
        return 3
    return 5


def _output_rank(library_id: str) -> int:
    lib = library_id.lower()
    if "coil" in lib:
        return 1
    if "pilot" in lib or "lamp" in lib:
        return 2
    return 3


def _off_rung(sheet: Sheet, catalog: Catalog) -> list[str]:
    from panelcad.render.sheet import parse_pin_ref, pin_world

    issues: list[str] = []
    by_id = {comp.id: comp for comp in sheet.components}
    for wire in sheet.wires:
        try:
            a_ref, a_pin = parse_pin_ref(wire.frm)
            b_ref, b_pin = parse_pin_ref(wire.to)
        except ValueError:
            continue
        if a_ref not in by_id or b_ref not in by_id:
            continue
        a_role = _role(by_id[a_ref].library_id)
        b_role = _role(by_id[b_ref].library_id)
        if "transformer" in {a_role, b_role} or "fuse" in {a_role, b_role}:
            continue
        if "terminal" in {a_role, b_role}:
            continue
        a_dev = _device(catalog, by_id[a_ref])
        b_dev = _device(catalog, by_id[b_ref])
        if a_dev is None or b_dev is None:
            continue
        try:
            _ax, ay = pin_world(by_id[a_ref], a_dev.pin(a_pin))
            _bx, by = pin_world(by_id[b_ref], b_dev.pin(b_pin))
        except KeyError:
            continue
        if abs(ay - by) <= _PIN_TOL:
            continue
        aux = a_role == "aux" or b_role == "aux"
        if aux and abs(ay - by) <= _DROP_MM + _GAP:
            continue
        issues.append(f"{sheet.id}: {a_ref} and {b_ref} are not on one rung line")
    return issues


def _bad_routes(session: Session, sheet: Sheet, catalog: Catalog) -> list[str]:
    from panelcad.render.sheet import parse_pin_ref
    from panelcad.render.wiring import route_sheet_wires

    issues: list[str] = []
    try:
        routes = route_sheet_wires(sheet, catalog, project=session.project)
    except Exception:  # noqa: BLE001
        return [f"{sheet.id}: ladder wires could not be routed"]
    roles: dict[str, str] = {}
    def _end_ref(text: str) -> str:
        try:
            ref, _pin = parse_pin_ref(text)
        except ValueError:
            ref = str(text).split(".")[0]
        return ref

    for wire in sheet.wires:
        ends = set()
        for ref in (_end_ref(wire.frm), _end_ref(wire.to)):
            comp = next((item for item in sheet.components if item.id == ref), None)
            if comp is not None:
                ends.add(_role(comp.library_id))
        roles[wire.id] = "power" if ends & {"transformer", "fuse"} else "terminal" if "terminal" in ends else "control"
    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        kind = roles.get(wire.id, "control")
        for a, b in zip(pts, pts[1:], strict=False):
            dx = abs(float(a.x) - float(b.x))
            dy = abs(float(a.y) - float(b.y))
            if dx > 0.8 and dy > 0.8:
                issues.append(f"{sheet.id}: {wire.id} is diagonal")
            if kind == "control" and dy > _DROP_MM + 8.0 and dx <= 0.8:
                issues.append(f"{sheet.id}: {wire.id} wanders {dy:.0f} mm off the rung")
        if len(pts) > 3 and kind == "control":
            ys = [float(p.y) for p in pts]
            if max(ys) - min(ys) > _DROP_MM + _GAP:
                issues.append(f"{sheet.id}: {wire.id} jogs through the sheet")
        if kind == "terminal":
            long_levels = {
                round(float(a.y), 1)
                for a, b in zip(pts, pts[1:], strict=False)
                if abs(float(a.y) - float(b.y)) <= 0.8 and abs(float(a.x) - float(b.x)) > 18.0
            }
            if len(long_levels) >= 2:
                issues.append(f"{sheet.id}: {wire.id} arches over the terminals")
            if any(
                abs(float(a.x) - float(b.x)) <= 0.8 and abs(float(a.y) - float(b.y)) > 2.0
                for a, b in zip(pts, pts[1:], strict=False)
            ):
                issues.append(f"{sheet.id}: {wire.id} loops around the terminals")
        if kind == "power" and _power_jog(pts):
            issues.append(f"{sheet.id}: {wire.id} jogs through the power block")
    from panelcad.core.route import path_hits_obstacles
    from panelcad.render.sheet import parse_pin_ref
    from panelcad.render.wiring import sheet_obstacles

    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        if not pts:
            continue
        skip: set[str] = set()
        for end in (wire.frm, wire.to):
            try:
                ref, _pin = parse_pin_ref(end)
            except ValueError:
                continue
            skip.add(ref)
        walls = sheet_obstacles(sheet, catalog, skip=skip)
        if path_hits_obstacles(pts, walls, skip_ends=True):
            if not _straight_terminal_jumper(sheet, catalog, pts):
                issues.append(f"{sheet.id}: {wire.id} runs through a symbol")
                continue
        if _crosses_own_body(sheet, catalog, wire, pts):
            issues.append(f"{sheet.id}: {wire.id} runs through a symbol")
    return issues


def _power_jog(pts) -> bool:
    """A power return may use one channel under the block. Two long runs weave."""

    if len(pts) <= 4:
        return False
    longs = 0
    for a, b in zip(pts, pts[1:], strict=False):
        if abs(float(a.y) - float(b.y)) <= 0.8 and abs(float(a.x) - float(b.x)) > 18.0:
            longs += 1
    return longs >= 2


def _crosses_own_body(sheet: Sheet, catalog: Catalog, wire, pts) -> bool:
    """A wire between a device's own pins, or a far-pin rail wire whose path enters the body."""

    from panelcad.core.ladder import endpoint_is_rail, rail_names
    from panelcad.render.sheet import parse_pin_ref

    def parsed(text: str) -> tuple[str, str]:
        try:
            ref, pin = parse_pin_ref(text)
        except ValueError:
            ref, pin = str(text).split(".")[0], ""
        return ref, pin

    a_ref, a_pin = parsed(wire.frm)
    b_ref, b_pin = parsed(wire.to)
    if a_ref and a_ref == b_ref:
        return True
    hot, ret = rail_names(sheet, None)
    by_id = {comp.id: comp for comp in sheet.components}

    def far_pin(rail_ref: str, device_ref: str, pin_id: str) -> bool:
        comp = by_id.get(device_ref)
        device = _device(catalog, comp) if comp is not None else None
        if device is None or not pin_id:
            return False
        want = "left" if rail_ref == hot else "right" if rail_ref == ret else ""
        if not want:
            return False
        for pin in device.pins:
            if str(pin.id) == pin_id:
                return str(pin.side) != want
        return False

    device_ref = ""
    if endpoint_is_rail(sheet, wire.frm, None) and far_pin(a_ref, b_ref, b_pin):
        device_ref = b_ref
    elif endpoint_is_rail(sheet, wire.to, None) and far_pin(b_ref, a_ref, a_pin):
        device_ref = a_ref
    if not device_ref:
        return False
    # The pin faces the other rail. That is through the body only when the
    # drawn path enters the body. A jog that stays outside is a clean landing.
    comp = by_id.get(device_ref)
    if comp is None or not pts:
        return True
    from panelcad.core.route import path_hits_obstacles
    from panelcad.render.wiring import component_obstacle

    return path_hits_obstacles(pts, [component_obstacle(sheet, catalog, comp)], skip_ends=True)


def _straight_terminal_jumper(sheet: Sheet, catalog: Catalog, pts) -> bool:
    """A jumper that stays on the terminal pin line is the rung, not a loop."""

    from panelcad.core.route import path_hits_obstacles
    from panelcad.render.sheet import pin_world
    from panelcad.render.wiring import component_obstacle

    ys = [float(point.y) for point in pts]
    if not ys or max(ys) - min(ys) > _PIN_TOL:
        return False
    y = sum(ys) / len(ys)
    for comp in sheet.components:
        if _role(comp.library_id) != "terminal":
            if path_hits_obstacles(pts, [component_obstacle(sheet, catalog, comp)], skip_ends=True):
                return False
            continue
        device = _device(catalog, comp)
        if device is None:
            continue
        _px, py = pin_world(comp, _pin_named(device, "", "left"))
        if abs(py - y) > _PIN_TOL and path_hits_obstacles(pts, [component_obstacle(sheet, catalog, comp)], skip_ends=True):
            return False
    return True


def _stacked_notes(session: Session, sheet: Sheet) -> list[str]:
    from panelcad.core.ladder import is_ladder_sheet
    from panelcad.render.sheet_labels import sheet_text_boxes

    try:
        boxes = sheet_text_boxes(
            session.project,
            session.catalog,
            sheet,
            ladder=is_ladder_sheet(sheet, session.project),
            sheet_no=1,
        )
    except Exception:  # noqa: BLE001
        return []
    notes = [(name, box) for name, box in boxes if name.startswith("wire:") or name.startswith("xref:")]
    for i, (a_name, a) in enumerate(notes):
        for b_name, b in notes[i + 1 :]:
            if a[0] < b[2] - 0.4 and a[2] > b[0] + 0.4 and a[1] < b[3] - 0.4 and a[3] > b[1] + 0.4:
                return [f"{sheet.id}: {a_name} overlaps {b_name}"]
    return []


def _body_problems(session: Session, sheet: Sheet, catalog: Catalog) -> list[str]:
    from panelcad.core.route import component_aabb
    from panelcad.render.sheet import pin_world
    from panelcad.render.titleblock import working_rect

    issues: list[str] = []
    boxes: list[tuple[str, tuple[float, float, float, float], float]] = []
    outputs: list[tuple[str, float]] = []
    for comp in sheet.components:
        device = _device(catalog, comp)
        if device is None or "offpage" in comp.library_id.lower():
            continue
        box = component_aabb(
            comp.x,
            comp.y,
            float(device.schematic_width),
            float(device.schematic_height),
            int(comp.rotation or 0),
        )
        pin = _pin_named(device, "", "left")
        _px, py = pin_world(comp, pin)
        boxes.append((comp.id, box, py))
        if _role(comp.library_id) == "output":
            outputs.append((comp.id, py))
    _wx, wy, _ww, wh = working_rect(sheet)
    limit = wy + wh - _TITLE_CLEAR
    for name, box, _py in boxes:
        if box[3] > limit:
            issues.append(f"{sheet.id}: {name} sits on the title block")
    for i, (a, ab, _py) in enumerate(boxes):
        for b, bb, _qy in boxes[i + 1 :]:
            if ab[0] < bb[2] - 0.4 and ab[2] > ab[0] and ab[0] < bb[2] and ab[1] < bb[3] - 0.4 and ab[3] > bb[1] + 0.4 and ab[2] > bb[0] + 0.4:
                issues.append(f"{sheet.id}: {a} overlaps {b}")
                return issues
    for i, (a, ay) in enumerate(outputs):
        for b, by in outputs[i + 1 :]:
            if abs(ay - by) <= _PIN_TOL:
                issues.append(f"{sheet.id}: {a} and {b} share one rung")
    rung_ys = [py for name, _box, py in boxes if _role(next(c.library_id for c in sheet.components if c.id == name)) != "terminal"]
    terminals = [(name, box, py) for name, box, py in boxes if _role(next(c.library_id for c in sheet.components if c.id == name)) == "terminal"]
    for name, box, py in terminals:
        for rung_y in rung_ys:
            if abs(rung_y - py) <= _PIN_TOL:
                continue
            if box[1] - _WIRE_CLEAR < rung_y < box[3] + _WIRE_CLEAR:
                issues.append(f"{sheet.id}: {name} collides with a rung wire")
                return issues
    for i, (a, ab, _py) in enumerate(terminals):
        for b, bb, _qy in terminals[i + 1 :]:
            gap_x = max(0.0, max(ab[0], bb[0]) - min(ab[2], bb[2]))
            gap_y = max(0.0, max(ab[1], bb[1]) - min(ab[3], bb[3]))
            if gap_x < 6.0 and gap_y < 6.0:
                issues.append(f"{sheet.id}: {a} and {b} terminals are cramped")
                return issues
    wired: dict[str, set[str]] = {}
    from panelcad.render.sheet import parse_pin_ref as _parse

    for wire in sheet.wires:
        try:
            a_ref, _pa = _parse(wire.frm)
            b_ref, _pb = _parse(wire.to)
        except ValueError:
            continue
        wired.setdefault(a_ref, set()).add(b_ref)
        wired.setdefault(b_ref, set()).add(a_ref)
    terminal_ids = {name for name, _box, _py in terminals}
    by_py = {name: py for name, _box, py in boxes}
    for name, _box, py in boxes:
        comp = next(item for item in sheet.components if item.id == name)
        if _role(comp.library_id) not in {"aux", "contact"}:
            continue
        sharing = [term for term in terminal_ids if abs(by_py[term] - py) <= _PIN_TOL]
        allowed = wired.get(name, set()) & terminal_ids
        aux_box = next(box for box_name, box, _py in boxes if box_name == name)
        for term in sharing:
            if term in allowed:
                continue
            term_box = next(box for box_name, box, _py in boxes if box_name == term)
            gap = max(0.0, max(aux_box[0], term_box[0]) - min(aux_box[2], term_box[2]))
            if gap < 40.0:
                issues.append(f"{sheet.id}: {name} shares the terminal row")
                return issues
    return issues


def _terminal_gate(session: Session, sheet: Sheet, catalog: Catalog) -> list[str]:
    """Reject a jumper nest and terminals parked in the gap between rungs."""

    from panelcad.render.sheet import pin_world
    from panelcad.render.sheet_labels import place_device_tags

    issues: list[str] = []
    terminals: list[tuple[str, float, float, float]] = []
    device_ys: list[float] = []
    for comp in sheet.components:
        device = _device(catalog, comp)
        if device is None:
            continue
        _px, py = pin_world(comp, _pin_named(device, "", "left"))
        if _role(comp.library_id) == "terminal":
            terminals.append((comp.id, py, float(comp.y), float(comp.y) + float(device.schematic_height)))
        else:
            device_ys.append(py)
    for name, py, top, _bot in terminals:
        between = [y for y in device_ys if y < py - 6.0] and [y for y in device_ys if y > py + 6.0]
        on_rung = any(abs(py - y) <= _PIN_TOL for y in device_ys)
        if between and not on_rung:
            issues.append(f"{sheet.id}: {name} floats between rungs")
    try:
        tags = place_device_tags(sheet, catalog, ladder=True, project=session.project, sheet_no=1)
    except Exception:  # noqa: BLE001
        tags = {}
    for name, _py, top, bot in terminals:
        layout = tags.get(name)
        if layout is None:
            continue
        _tx, ty, _align, _size = layout
        if ty < top - 6.0 or ty > bot + 6.0:
            issues.append(f"{sheet.id}: {name} tag sits on another rung")
    return issues


def _own_pin_wires(sheet: Sheet) -> list[str]:
    """A wire between a device's own pins bypasses that device. It must not be issued."""

    from panelcad.core.ladder import rail_names
    from panelcad.render.sheet import parse_pin_ref

    hot, ret = rail_names(sheet, None)
    rails = {hot, ret, "L1", "L2", "L3", "N", "X1", "X2"}
    issues: list[str] = []
    for wire in sheet.wires:
        try:
            a_ref, _a_pin = parse_pin_ref(wire.frm)
            b_ref, _b_pin = parse_pin_ref(wire.to)
        except ValueError:
            a_ref, b_ref = str(wire.frm).split(".")[0], str(wire.to).split(".")[0]
        if not a_ref or a_ref != b_ref or a_ref in rails:
            continue
        issues.append(f"{sheet.id}: {wire.id} connects {a_ref}'s own pins")
    return issues


def _fuse_across_rails(sheet: Sheet) -> list[str]:
    """A fuse whose only path runs from one rail to the other is not a feeder."""

    from panelcad.render.sheet import parse_pin_ref

    hot = str(getattr(sheet, "rail_hot", "") or "L1")
    ret = str(getattr(sheet, "rail_return", "") or "L2")
    fuses = {comp.id for comp in sheet.components if "fuse" in comp.library_id.lower()}
    if not fuses:
        return []
    adj: dict[str, set[str]] = {}

    def add(left: str, right: str) -> None:
        if not left or not right or left == right:
            return
        adj.setdefault(left, set()).add(right)
        adj.setdefault(right, set()).add(left)

    def end_ref(text: str) -> str:
        try:
            ref, _pin = parse_pin_ref(text)
        except ValueError:
            ref = str(text).split(".")[0]
        return ref

    for wire in sheet.wires:
        add(end_ref(wire.frm), end_ref(wire.to))
    issues: list[str] = []
    for tag in sorted(fuses):
        # Walk every simple path. The fuse is rail-to-rail when one path's
        # only device is that fuse.
        stack: list[tuple[str, list[str]]] = [(hot, [hot])]
        seen_paths: set[tuple[str, ...]] = set()
        while stack:
            node, path = stack.pop()
            if len(path) > 6:
                continue
            for nbr in sorted(adj.get(node, ())):
                if nbr in path:
                    continue
                nxt = path + [nbr]
                if nbr == ret:
                    devices = tuple(ref for ref in nxt if ref not in {hot, ret})
                    if devices == (tag,) and devices not in seen_paths:
                        seen_paths.add(devices)
                        issues.append(f"{sheet.id}: {tag} is a fuse from {hot} to {ret}")
                    continue
                stack.append((nbr, nxt))
    return issues


def _rail_without_a_load(sheet: Sheet) -> list[str]:
    """A device landed on a rail, with no path from it through a load.

    The drawing is a straight rail tie. A fuse, contact, or button may touch
    a rail only when the rung reaches a coil, a lamp, a transformer, or the
    same kind of load. The checker still owns that. Nothing is redrawn away.
    """

    from panelcad.render.sheet import parse_pin_ref

    hot = str(getattr(sheet, "rail_hot", "") or "L1")
    ret = str(getattr(sheet, "rail_return", "") or "L2")
    rails = {hot, ret}
    adj: dict[str, set[str]] = {}

    def add(left: str, right: str) -> None:
        if not left or not right or left == right:
            return
        adj.setdefault(left, set()).add(right)
        adj.setdefault(right, set()).add(left)

    def end_ref(text: str) -> str:
        try:
            ref, _pin = parse_pin_ref(text)
        except ValueError:
            ref = str(text).split(".")[0]
        return ref

    for wire in sheet.wires:
        add(end_ref(wire.frm), end_ref(wire.to))
    by_id = {comp.id: comp for comp in sheet.components}

    def is_load(ref: str) -> bool:
        comp = by_id.get(ref)
        if comp is None:
            return False
        lib = comp.library_id.lower()
        role = _role(lib)
        # An off-page is the rest of the rung. It is not a bare rail tie.
        return role in {"output", "terminal", "transformer", "offpage"} or "ol_contact" in lib

    def reaches_load(start: str) -> bool:
        seen = {start}
        stack = [start]
        while stack:
            node = stack.pop()
            if node not in rails and is_load(node):
                return True
            for nbr in adj.get(node, ()):
                if nbr in seen or nbr in rails:
                    continue
                seen.add(nbr)
                stack.append(nbr)
        return False

    issues: list[str] = []
    seen: set[tuple[str, str]] = set()
    for wire in sheet.wires:
        ends = [end_ref(wire.frm), end_ref(wire.to)]
        for device, rail in (ends, list(reversed(ends))):
            if rail not in rails or device in rails or device not in by_id:
                continue
            if is_load(device) or reaches_load(device):
                continue
            key = (device, rail)
            if key in seen:
                continue
            seen.add(key)
            issues.append(f"{sheet.id}: {device} is wired to {rail} with no path through a load")
    return issues


def _contacts_only_rungs(sheet: Sheet) -> list[str]:
    """An L1-to-L2 path of only contacts is not a rung. It must not be issued."""

    from panelcad.render.sheet import parse_pin_ref

    hot = str(getattr(sheet, "rail_hot", "") or "L1")
    ret = str(getattr(sheet, "rail_return", "") or "L2")
    adj: dict[str, set[str]] = {}

    def add(left: str, right: str) -> None:
        if not left or not right or left == right:
            return
        adj.setdefault(left, set()).add(right)
        adj.setdefault(right, set()).add(left)

    def end_ref(text: str) -> str:
        try:
            ref, _pin = parse_pin_ref(text)
        except ValueError:
            ref = str(text).split(".")[0]
        return ref

    for wire in sheet.wires:
        add(end_ref(wire.frm), end_ref(wire.to))
    if hot not in adj or ret not in adj:
        return []
    by_id = {comp.id: comp for comp in sheet.components}

    def carries_a_load(ref: str) -> bool:
        if ref in {hot, ret}:
            return False
        comp = by_id.get(ref)
        if comp is None:
            return True
        lib = comp.library_id.lower()
        role = _role(lib)
        return role in {"output", "terminal", "transformer", "fuse"} or "ol_contact" in lib

    found: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    stack: list[tuple[str, list[str]]] = [(hot, [hot])]
    while stack:
        node, path = stack.pop()
        if len(path) > len(by_id) + 2:
            continue
        for nbr in sorted(adj.get(node, ())):
            if nbr in path:
                continue
            nxt = path + [nbr]
            if nbr == ret:
                devices = tuple(ref for ref in nxt if ref not in {hot, ret})
                if devices and not any(carries_a_load(ref) for ref in devices) and devices not in seen:
                    seen.add(devices)
                    found.append(devices)
                continue
            stack.append((nbr, nxt))
    return [
        f"{sheet.id}: {', '.join(devices)} runs from {hot} to {ret} with no load" for devices in found
    ]


def _shorted_numbers(session: Session, sheet: Sheet) -> list[str]:
    """One node with two printed numbers, or a primary net landed on the return.

    Rails do not join rungs. A fuse or a transformer primary (H1–H4) that
    also reaches the return rail is a short, and so is a tee that puts 101
    and 104 on the same pin.
    """

    from panelcad.core.ansi_drafting import _SHEET_LINE
    from panelcad.core.ladder import endpoint_is_rail, rail_names
    from panelcad.render.sheet import parse_pin_ref

    _hot, ret = rail_names(sheet, session.project)
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        ra, rb = find(left), find(right)
        if ra != rb:
            parent[rb] = ra

    grouped: dict[str, list[tuple[Any, list[tuple[str, str]]]]] = {}
    for wire in sheet.wires:
        ends: list[tuple[str, str]] = []
        pins: list[str] = []
        for text in (wire.frm, wire.to):
            if endpoint_is_rail(sheet, text, session.project):
                ends.append(("rail", str(text).split(".")[0].upper()))
                continue
            try:
                ref, pin = parse_pin_ref(text)
            except ValueError:
                ref, pin = str(text).split(".")[0], ""
            token = f"{ref}.{pin}" if pin else ref
            ends.append(("pin", token))
            pins.append(token)
        if len(pins) >= 2:
            for pin in pins[1:]:
                union(pins[0], pin)
        key = find(pins[0]) if pins else f"wire:{wire.id}"
        grouped.setdefault(key, []).append((wire, ends))
    # Re-key after every union, so wires that only meet through a third wire share a net.
    nets: dict[str, list[tuple[Any, list[tuple[str, str]]]]] = {}
    for items in grouped.values():
        for wire, ends in items:
            pins = [name for kind, name in ends if kind == "pin"]
            key = find(pins[0]) if pins else f"wire:{wire.id}"
            nets.setdefault(key, []).append((wire, ends))
    issues: list[str] = []
    for items in nets.values():
        numbers = sorted(
            {
                str(wire.number or "").strip()
                for wire, _ends in items
                if _SHEET_LINE.match(str(wire.number or "").strip() or "")
            }
        )
        if len(numbers) >= 2:
            issues.append(f"{sheet.id}: {', '.join(numbers)} are two wire numbers on one net")
        primary = False
        to_return = False
        for _wire, ends in items:
            for kind, name in ends:
                if kind == "rail" and name == ret.upper():
                    to_return = True
                if kind == "pin" and _pin_is_primary(sheet, name):
                    primary = True
        if primary and to_return:
            label = numbers[0] if numbers else items[0][0].id
            issues.append(f"{sheet.id}: {label} ties the primary to {ret}")
    return issues


def _pin_is_primary(sheet: Sheet, token: str) -> bool:
    ref, _, pin = token.partition(".")
    comp = next((item for item in sheet.components if item.id == ref), None)
    if comp is None:
        return False
    lib = comp.library_id.lower()
    # The load side of a control fuse reaches the coil and the return on
    # purpose. Only the transformer primary is a short when it does.
    return "transformer" in lib and pin.upper().startswith("H")


def _rail_gaps(session: Session, sheet: Sheet, catalog: Catalog) -> list[str]:
    """A pin drawn in the rail column with no wire to that rail is not a landing."""

    from panelcad.core.ladder import endpoint_is_rail, rail_names, rail_xs
    from panelcad.render.sheet import parse_pin_ref, pin_world

    hot, ret = rail_names(sheet, session.project)
    left, right = rail_xs(sheet)
    near = 42.0
    wired_left: set[str] = set()
    wired_right: set[str] = set()

    def end_pin(text: str) -> tuple[str, str]:
        try:
            ref, pin = parse_pin_ref(text)
        except ValueError:
            ref, pin = str(text).split(".")[0], ""
        return ref, pin

    by_id = {comp.id: comp for comp in sheet.components}
    for wire in sheet.wires:
        for text in (wire.frm, wire.to):
            if endpoint_is_rail(sheet, text, session.project):
                continue
            ref, pin_id = end_pin(text)
            comp = by_id.get(ref)
            device = _device(catalog, comp) if comp is not None else None
            if device is None:
                continue
            side = ""
            for pin in device.pins:
                if str(pin.id) == pin_id:
                    side = str(pin.side)
                    break
            if side == "left":
                wired_left.add(ref)
            elif side == "right":
                wired_right.add(ref)
    issues: list[str] = []
    for comp in sheet.components:
        device = _device(catalog, comp)
        if device is None:
            continue
        role = _role(comp.library_id)
        if role in {"transformer", "offpage"}:
            continue
        left_pins = [pin for pin in device.pins if str(pin.side) == "left"]
        right_pins = [pin for pin in device.pins if str(pin.side) == "right"]
        if left_pins and comp.id not in wired_left:
            px, _py = pin_world(comp, left_pins[0])
            if 0.0 < px - left <= near:
                issues.append(f"{sheet.id}: {comp.id} does not land on {hot}")
                continue
        if right_pins and comp.id not in wired_right and (
            role in {"output", "terminal", "fuse"} or "ol_contact" in comp.library_id.lower()
        ):
            px, _py = pin_world(comp, right_pins[0])
            if 0.0 < right - px <= near:
                issues.append(f"{sheet.id}: {comp.id} does not land on {ret}")
    return issues


def _loads_on_the_right(session: Session, sheet: Sheet, catalog: Catalog) -> list[str]:
    """A coil, overload, or pilot belongs just left of L2, not mid-sheet."""

    from panelcad.core.ladder import rail_xs
    from panelcad.render.sheet import pin_world

    _left, right = rail_xs(sheet)
    issues: list[str] = []
    by_y: dict[float, list[tuple[str, float, bool]]] = {}
    for comp in sheet.components:
        device = _device(catalog, comp)
        if device is None:
            continue
        load = _is_rung_load(comp.library_id)
        contact = _role(comp.library_id) in {"contact", "aux", "offpage"} and not load
        if load and not _pin_side_wired(sheet, comp.id, device, "right"):
            load = False
        if not load and not contact:
            continue
        pins = [pin for pin in device.pins if str(pin.side) == "right"] or list(device.pins)
        px, py = pin_world(comp, pins[-1] if load else _pin_named(device, "", "left"))
        key = round(py, 1)
        by_y.setdefault(key, []).append((comp.id, px, load))
    for group in by_y.values():
        loads = [item for item in group if item[2]]
        if not loads:
            continue
        rightmost = max(loads, key=lambda item: item[1])
        if right - rightmost[1] > 36.0:
            issues.append(f"{sheet.id}: {rightmost[0]} is not next to L2")
        for name, px, is_load in group:
            if is_load:
                continue
            if px > rightmost[1] + 2.0:
                issues.append(f"{sheet.id}: {name} sits to the right of the coil")
    return issues
