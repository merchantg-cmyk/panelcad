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


def layout_elementary(session: Session, sheet: Sheet) -> int:
    """Replace symbol positions from the wire list. Wires and tags stay."""

    moved = _layout(session, sheet, snap=True)
    if elementary_layout_issues(session, sheet):
        moved = _layout(session, sheet, snap=False)
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
    wires = [(wire, list(wire.points)) for wire in sheet.wires]
    layout_elementary(session, sheet)
    if elementary_layout_issues(session, sheet):
        for comp, x, y, rotation in saved:
            comp.x, comp.y, comp.rotation = x, y, rotation
        for wire, points in wires:
            wire.points = points
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
    moved = _place_pin(transformer, device, _pin_named(device, "H1", "left"), _left_x(sheet) + 78.0, y)
    placed.add(transformer.id)
    h1 = _pin_named(device, "H1", "left")
    h4 = _pin_named(device, "H4", "left")
    lower = y + float(h4.y) - float(h1.y)
    fuses = [comp for comp in by_id.values() if comp.id not in placed and "fuse" in comp.library_id.lower()]
    fuses.sort(key=lambda comp: comp.id)
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
    return rungs


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
            under = _under_contact(nbrs, by_id)
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


def _under_contact(nbrs: list[str], by_id) -> str:
    for ref in nbrs:
        if "pb_no" in by_id[ref].library_id.lower():
            return ref
    return nbrs[0]


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


def _feeds_only_a_terminal(ref: str, by_id, adj) -> bool:
    if _role(by_id[ref].library_id) == "terminal":
        return False
    neighbors = [nbr for nbr in adj.get(ref, ()) if nbr in by_id]
    return bool(neighbors) and all(_role(by_id[nbr].library_id) == "terminal" for nbr in neighbors)


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
    x_right = _column_pin_x(sheet, width)
    _wx, wy, _ww, wh = working_rect(sheet)
    limit = wy + wh - _TITLE_CLEAR - half
    hosted_y = None
    for comp in loose:
        host = next(
            (nbr for nbr in adj.get(comp.id, ()) if nbr in placed and _feeds_only_a_terminal(nbr, by_id, adj)),
            None,
        )
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
        ax, ay, _aside, bx, by, _bside = wire_ends(sheet, session.catalog, wire, session.project)
        device_x, rail_x = (ax, bx) if a_ref == fuse else (bx, ax)
        wire.points = [Point(x=device_x, y=under), Point(x=rail_x, y=under)]
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
    """Coil, pilot, or the overload that sits with the coil at L2."""

    lib = library_id.lower()
    return _role(lib) == "output" or "ol_contact" in lib


def _place_row(sheet, by_id, devices, refs: list[str], y: float) -> int:
    """Contacts from the left rail. The coil, light, and overload sit against L2."""

    from panelcad.core.ladder import rail_xs

    left, right = rail_xs(sheet)
    loads = [ref for ref in refs if _is_rung_load(by_id[ref].library_id)]
    contacts = [ref for ref in refs if ref not in loads]
    edge = right - 14.0
    moved = 0
    for ref in reversed(loads):
        device = devices[ref]
        width = float(device.schematic_width)
        edge -= width
        moved += _place_on(by_id[ref], device, edge, y)
        edge -= 10.0
    x = left + 18.0
    limit = edge - 8.0 if loads else right - 36.0
    for ref in contacts:
        device = devices[ref]
        width = float(device.schematic_width)
        if x + width > limit:
            x = left + 18.0
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
    if "coil" in lib or "pilot" in lib or "lamp" in lib:
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
        if kind == "power" and len(pts) > 4:
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
        if not path_hits_obstacles(pts, walls, skip_ends=True):
            continue
        if _straight_terminal_jumper(sheet, catalog, pts):
            continue
        issues.append(f"{sheet.id}: {wire.id} runs through a symbol")
    return issues


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
