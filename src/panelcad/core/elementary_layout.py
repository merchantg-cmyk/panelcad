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
    from panelcad.core.pin_geometry import parse_pin_ref
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
    """A wire into the far pin of a fuse/device is drawn through that body.

    Land it on the near pin instead. Covers both directions: neighbor on the
    left into a right pin, and neighbor on the right into a left pin (T1→FU.1).
    A wire that already uses the near pin stays.
    """

    from panelcad.core.pin_geometry import parse_pin_ref, pin_world
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
        for ref, pin_id, other, other_pin_id in (
            (b_ref, b_pin, a_ref, a_pin),
            (a_ref, a_pin, b_ref, b_pin),
        ):
            comp = by_id[ref]
            device = _device(session.catalog, comp)
            other_comp = by_id[other]
            if device is None or not _power_device(comp):
                continue
            # An off-page/source arrow is the feed itself — never retarget the fuse pin.
            if "offpage" in str(other_comp.library_id or "").lower():
                continue
            landed = next((pin for pin in device.pins if str(pin.id) == pin_id), None)
            if landed is None:
                continue
            side = str(landed.side)
            if side not in {"left", "right"}:
                continue
            px, py = pin_world(comp, landed)
            # Neighbor must sit on the far side of this pin (wire would cross the body).
            if side == "right" and other_comp.x > px:
                continue
            if side == "left" and other_comp.x < px:
                continue
            # Compare Y on the actual other pin of this wire (not an arbitrary side pin).
            other_dev = _device(session.catalog, other_comp)
            if other_dev is None:
                continue
            other_pin = next((pin for pin in other_dev.pins if str(pin.id) == other_pin_id), None)
            if other_pin is None:
                other_pin = _pin_named(other_dev, "", "left")
            if other_pin is None:
                continue
            _ox, oy = pin_world(other_comp, other_pin)
            if abs(oy - py) > 12.0:
                continue
            near_side = "left" if side == "right" else "right"
            near = _facing_pin(session.catalog, comp, near_side)
            if not near or near == pin_id:
                continue
            near_end = f"{ref}.{near}"
            far_end = f"{ref}.{pin_id}"
            # If the facing pin is already used, swap with that wire so the
            # neighbor on this side keeps the near pin (T1↔FU2.2, rail↔FU2.1).
            occupant = next(
                (
                    other_wire
                    for other_wire in sheet.wires
                    if str(getattr(other_wire, "id", "") or "") != str(getattr(wire, "id", "") or "")
                    and near_end in {str(other_wire.frm), str(other_wire.to)}
                ),
                None,
            )
            if occupant is not None:
                if str(occupant.frm) == near_end:
                    occupant.frm = far_end
                elif str(occupant.to) == near_end:
                    occupant.to = far_end
                else:
                    continue
                occupant.points = []
            if ref == b_ref:
                wire.to = near_end
            else:
                wire.frm = near_end
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
    snapped_issues = elementary_layout_issues(session, sheet)
    if snapped_issues:
        # Keep a snapshot: snap=False must not trade title-block clearance for
        # a different routing defect (it used to shove CR1/M1-23 onto the title).
        saved = [(comp, comp.x, comp.y, comp.rotation) for comp in sheet.components]
        wires = [wire.model_copy(deep=True) for wire in sheet.wires]
        moved_free = _layout(session, sheet, snap=False)
        free_issues = elementary_layout_issues(session, sheet)
        snapped_title = sum(1 for text in snapped_issues if "title block" in text)
        free_title = sum(1 for text in free_issues if "title block" in text)
        if free_issues and (
            (not snapped_issues)
            or free_title > snapped_title
            or (len(free_issues) >= len(snapped_issues) and free_title >= snapped_title)
        ):
            for comp, x, y, rotation in saved:
                comp.x, comp.y, comp.rotation = x, y, rotation
            sheet.wires = wires
        else:
            moved = moved_free
    _separate_crossings(session, sheet)
    _park_primary_offpage_feeds(session, sheet)
    return moved


def _park_primary_offpage_feeds(session: Session, sheet: Sheet) -> None:
    """Keep off-page primary-feed arrows on the fuse pin after ladder layout.

    Layout repositions fuses; an arrow left at the pre-layout coordinate would
    sit on the far side of the body and look like a through-body hop.
    """

    from panelcad.core.pin_geometry import parse_pin_ref, pin_world
    from panelcad.render.wiring import clear_route_cache

    by_id = {comp.id: comp for comp in sheet.components}
    for wire in sheet.wires:
        ends: list[tuple[str, str]] = []
        for text in (wire.frm, wire.to):
            try:
                ref, pin = parse_pin_ref(str(text))
            except ValueError:
                continue
            if ref and pin:
                ends.append((ref, pin))
        if len(ends) != 2:
            continue
        for (a_ref, a_pin), (b_ref, b_pin) in (ends, list(reversed(ends))):
            arrow = by_id.get(a_ref)
            host = by_id.get(b_ref)
            if arrow is None or host is None:
                continue
            if "offpage" not in str(arrow.library_id or "").lower():
                continue
            host_lib = str(host.library_id or "").lower()
            if "fuse" not in host_lib and "transformer" not in host_lib:
                continue
            device = _device(session.catalog, host)
            if device is None:
                continue
            pin_obj = next((p for p in device.pins if str(p.id) == b_pin), None)
            if pin_obj is None:
                continue
            try:
                arrow_dev = session.catalog.get(arrow.library_id)
                ap = arrow_dev.pin("A")
            except (KeyError, AttributeError):
                continue
            px, py = pin_world(host, pin_obj)
            side = str(getattr(pin_obj, "side", "") or "left").lower()
            # Sit just outside the pin so the stub does not cross the body.
            gap = 1.2
            if side == "right":
                arrow.x = float(px) + gap - float(ap.x)
            else:
                arrow.x = float(px) - gap - float(ap.x)
            arrow.y = float(py) - float(ap.y)
            wire.points = []
    clear_route_cache()


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
    power_floor = floor
    rungs = _rungs(sheet, by_id, devices, adj, placed)
    control_ids: list[str] = []
    for series, parallel in rungs:
        y, floor = _rung_y(session, sheet, by_id, devices, series, floor, snap=snap)
        moved += _place_row(sheet, by_id, devices, series, y)
        for aux_id, under_id in parallel:
            drop = _drop(devices[under_id], devices[aux_id])
            moved += _place_on(by_id[aux_id], devices[aux_id], by_id[under_id].x, y + drop)
            placed.add(aux_id)
            # Seal-in under the start owns the band through its body bottom
            # and its below-body tag. The next series (CR1 as 109, pilot) must
            # clear that tag so wire 109 does not run through "M1-13".
            floor = max(floor, _tag_floor(by_id[aux_id], devices[aux_id]))
            control_ids.append(aux_id)
        placed.update(series)
        control_ids.extend(series)
        floor = max(floor, _prev_bottom(by_id, devices, set(series) | {aux for aux, _under in parallel}))
    moved += _squeeze_control_rows(session, sheet, by_id, devices, power_floor, control_ids)
    moved += _place_terminals(
        sheet, session, by_id, devices, adj, placed, floor, power_floor, control_ids
    )
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
    issues.extend(_wires_cross_device_labels(session, sheet, catalog))
    issues.extend(_terminal_gate(session, sheet, catalog))
    issues.extend(_loads_on_the_right(session, sheet, catalog))
    issues.extend(_own_pin_wires(sheet))
    issues.extend(_contacts_only_rungs(sheet))
    issues.extend(_fuse_across_rails(sheet))
    issues.extend(_fuse_dangling_pins(sheet, catalog))
    issues.extend(_rail_without_a_load(sheet))
    issues.extend(_rail_gaps(session, sheet, catalog))
    issues.extend(_shorted_numbers(session, sheet))
    issues.extend(_transformer_control_rules(session, sheet))
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
    from panelcad.core.pin_geometry import parse_pin_ref

    y = rung_y(sheet, 1, project=session.project)
    # Tag sits above the body (baseline = body.y - TAG_DY). Keep the whole
    # label inside the working-rect top margin — rung 1 alone can park T1 so
    # the "T1" mark clips the sheet border.
    from panelcad.core.ink import TAG_DY, TAG_SIZE, TEXT_ASCENT
    from panelcad.render.titleblock import working_rect as _working_rect

    _wx, wy, _ww, _wh = _working_rect(sheet)
    h1_probe = _pin_named(device, "H1", "left")
    if h1_probe is not None:
        min_body_y = float(wy) + float(TAG_DY) + float(TAG_SIZE) * float(TEXT_ASCENT) + 2.5
        min_pin_y = min_body_y + float(h1_probe.y)
        if y < min_pin_y:
            y = min_pin_y
    anchor = _left_x(sheet) + 78.0
    fuses = [comp for comp in by_id.values() if comp.id not in placed and "fuse" in comp.library_id.lower()]
    fuses.sort(key=lambda comp: comp.id)

    def fuse_on_transformer_pin(pin_name: str):
        """Fuse already wired to this transformer pin (H1/H4 primary or X1 secondary)."""

        want = f"{transformer.id}.{pin_name}".upper()
        for fuse in fuses:
            for wire in sheet.wires:
                ends = []
                for text in (wire.frm, wire.to):
                    try:
                        ref, pin = parse_pin_ref(text)
                    except ValueError:
                        continue
                    ends.append(f"{ref}.{pin}".upper())
                if want in ends and any(str(fuse.id).upper() == e.split(".")[0] for e in ends):
                    return fuse
        return None

    # Primary = wired to H*; secondary = wired to X* or marked role=secondary_fuse.
    upper_fuse = fuse_on_transformer_pin("H1")
    lower_fuse = fuse_on_transformer_pin("H4")
    secondary_fuse = fuse_on_transformer_pin("X1") or fuse_on_transformer_pin("X2")
    if secondary_fuse is None:
        secondary_fuse = next(
            (
                fuse
                for fuse in fuses
                if str((fuse.properties or {}).get("role") or "") == "secondary_fuse"
            ),
            None,
        )
    used = {fuse.id for fuse in (upper_fuse, lower_fuse, secondary_fuse) if fuse is not None}
    remaining_primary = [fuse for fuse in fuses if fuse.id not in used]
    if upper_fuse is None and lower_fuse is None:
        if remaining_primary:
            upper_fuse = remaining_primary.pop(0)
        if remaining_primary:
            lower_fuse = remaining_primary.pop(0)
    else:
        if upper_fuse is None and remaining_primary:
            upper_fuse = remaining_primary.pop(0)
        if lower_fuse is None and remaining_primary:
            lower_fuse = remaining_primary.pop(0)
    primary_placed = [fuse for fuse in (upper_fuse, lower_fuse) if fuse is not None]
    if primary_placed and any(
        not _pin_side_wired(sheet, comp.id, devices[comp.id], "left") for comp in primary_placed
    ):
        from panelcad.core.ladder import rail_xs

        left, _right = rail_xs(sheet)
        fuse_w = max(float(devices[comp.id].schematic_width) for comp in primary_placed)
        anchor = max(anchor, left + 56.0 + fuse_w + 14.0)
    moved = _place_pin(transformer, device, _pin_named(device, "H1", "left"), anchor, y)
    placed.add(transformer.id)
    h1 = _pin_named(device, "H1", "left")
    h4 = _pin_named(device, "H4", "left")
    lower = y + float(h4.y) - float(h1.y)
    # Prefer the fuse already on H1 / H4 so the hop lands on the facing pin row
    # (FU2 alone on H4 must not sit on the H1 row — that keeps a through-body Y).
    if upper_fuse is not None:
        moved += _place_left_of(upper_fuse, devices[upper_fuse.id], transformer, y)
        placed.add(upper_fuse.id)
    if lower_fuse is not None and lower_fuse.id not in placed:
        moved += _place_left_of(lower_fuse, devices[lower_fuse.id], transformer, lower)
        placed.add(lower_fuse.id)
    # Secondary fuse on the X1 row to the right of T1 (never in the primary
    # H-fuse stack). Facing: T1.X1→FU.1, FU.2→L1 routes around outside the body.
    if secondary_fuse is not None and secondary_fuse.id not in placed:
        x1 = _pin_named(device, "X1", "right")
        sec_y = y
        if x1 is not None and h1 is not None:
            sec_y = y + float(x1.y) - float(h1.y)
        moved += _place_right_of(
            secondary_fuse, devices[secondary_fuse.id], transformer, device, sec_y
        )
        placed.add(secondary_fuse.id)
    # Keep the control transformer in the left power block. A free-layout
    # pass that parked T1 next to L2 stretched 100/101/103 across the sheet
    # and stacked FU3 on the primary junction.
    from panelcad.core.ladder import rail_xs as _rail_xs

    left_rail, _right_rail = _rail_xs(sheet)
    max_t1_x = left_rail + 140.0
    if float(transformer.x) > max_t1_x:
        moved += _place_pin(transformer, device, _pin_named(device, "H1", "left"), anchor, y)
        if upper_fuse is not None and upper_fuse.id in placed:
            moved += _place_left_of(upper_fuse, devices[upper_fuse.id], transformer, y)
        if lower_fuse is not None and lower_fuse.id in placed:
            moved += _place_left_of(lower_fuse, devices[lower_fuse.id], transformer, lower)
        if secondary_fuse is not None and secondary_fuse.id in placed:
            x1 = _pin_named(device, "X1", "right")
            sec_y = y
            if x1 is not None and h1 is not None:
                sec_y = y + float(x1.y) - float(h1.y)
            moved += _place_right_of(
                secondary_fuse, devices[secondary_fuse.id], transformer, device, sec_y
            )
    return moved


def _place_right_of(comp, device, anchor_comp, anchor_device, y: float) -> int:
    """Park ``comp`` just past the right edge of ``anchor_comp`` at pin row ``y``."""

    width = float(anchor_device.schematic_width)
    return _place_on(comp, device, float(anchor_comp.x) + width + 14.0, y)


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
        # Off-page arrows and fuses are parked with the power block — never their
        # own control rungs (that shoved CR1/M1-23 onto the title).
        if comp.id in claimed or _role(comp.library_id) in {"terminal", "transformer", "fuse", "offpage"}:
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
    from panelcad.core.pin_geometry import parse_pin_ref

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
        if pin is None:
            continue
        above = max(above, float(pin.y))
        below = max(below, float(devices[ref].schematic_height) - float(pin.y))
    minimum = floor + _GAP + above
    if snap:
        y = _fit_pin_y(session, sheet, minimum, below)
    else:
        # Free Y (no grid snap) still must clear the title block. The snap=False
        # retry used to park rungs on the title when the floor had grown past it.
        from panelcad.render.titleblock import working_rect

        _wx, wy, _ww, wh = working_rect(sheet)
        limit = wy + wh - _TITLE_CLEAR
        y = minimum
        if y + below > limit + 0.2:
            y = max(wy + below + _GAP, limit - below)
    return y, floor


def _squeeze_control_rows(
    session: Session,
    sheet: Sheet,
    by_id: dict[str, Component],
    devices: dict[str, Any],
    power_floor: float,
    control_ids: list[str],
) -> int:
    """Nudge same-column rungs apart when title clamping collapsed them.

    Only moves the lower of a colliding pair — never pulls upper rungs into the
    power block.
    """

    from panelcad.render.titleblock import working_rect
    from panelcad.render.wiring import clear_route_cache

    ids = [ref for ref in dict.fromkeys(control_ids) if ref in by_id and ref in devices]
    if len(ids) < 2:
        return 0
    _wx, wy, _ww, wh = working_rect(sheet)
    limit = wy + wh - _TITLE_CLEAR
    # Wire-connected cohorts: move a whole rung together, never Y-proximity
    # (title-clamped neighbors sit 2 mm apart and are NOT the same rung).
    neighbors: dict[str, set[str]] = {ref: set() for ref in ids}
    for wire in sheet.wires:
        ends = []
        for text in (str(wire.frm), str(wire.to)):
            ends.append(text.split(".", 1)[0])
        a, b = ends[0], ends[1] if len(ends) > 1 else ""
        if a in neighbors and b in neighbors:
            neighbors[a].add(b)
            neighbors[b].add(a)

    def cohort(ref: str) -> list[str]:
        seen = {ref}
        stack = [ref]
        while stack:
            node = stack.pop()
            for nbr in neighbors.get(node, ()):
                if nbr in seen:
                    continue
                seen.add(nbr)
                stack.append(nbr)
        return [item for item in ids if item in seen]

    def shift(refs: list[str], dy: float) -> int:
        count = 0
        for ref in refs:
            by_id[ref].y = float(by_id[ref].y) + dy
            count += 1
        return count

    def device_pin_y(ref: str) -> float:
        """Representative pin world-Y so series mates match despite body offsets."""

        from panelcad.core.pin_geometry import pin_world

        comp = by_id[ref]
        device = devices[ref]
        pins = list(getattr(device, "pins", None) or [])
        if not pins:
            return float(comp.y)
        return float(pin_world(comp, pins[0])[1])

    def rung_mates(ref: str) -> list[str]:
        """Wire cohort members that share this device's pin Y (one rung line).

        Component Y differs across a series (estop vs coil body offsets), so
        mates are keyed by pin world-Y. Excludes a cohort member already on a
        lower rung (M1-23 under CR1-13).
        """

        y0 = device_pin_y(ref)
        return [
            mate
            for mate in cohort(ref)
            if mate in by_id and abs(device_pin_y(mate) - y0) <= _PIN_TOL + 0.5
        ]

    moved = 0
    # Stable order by Y so we can push the lower symbol down.
    ordered = sorted(ids, key=lambda ref: (float(by_id[ref].y), ref))
    for i, upper in enumerate(ordered):
        for lower in ordered[i + 1 :]:
            if abs(float(by_id[upper].x) - float(by_id[lower].x)) > 22.0:
                continue
            need = max(float(devices[upper].schematic_height), float(devices[lower].schematic_height)) + _GAP
            gap = float(by_id[lower].y) - float(by_id[upper].y)
            if gap >= need - 0.2:
                continue
            # Same rung (wire cohort on one Y) — leave pin-aligned series alone.
            if set(rung_mates(lower)) & set(rung_mates(upper)):
                continue
            dy = need - gap
            # ``control_ids`` sometimes omits a mate; never shift an empty list.
            lower_mates = rung_mates(lower) or [lower]
            # Title room is gated by the colliding column only — a right-hand
            # coil on the same rung (CR1 with M1-23) must not block separating
            # M1-23 from CR1-13 on the left.
            lower_col = [
                ref
                for ref in lower_mates
                if abs(float(by_id[ref].x) - float(by_id[lower].x)) <= 22.0
            ] or [lower]
            new_bottom = max(_bottom(by_id[ref], devices[ref]) for ref in lower_col) + dy
            if new_bottom <= limit + 0.2:
                moved += shift(lower_mates, dy)
                continue
            above = None
            for ref in ordered:
                if ref == upper:
                    break
                if abs(float(by_id[ref].x) - float(by_id[upper].x)) <= 22.0:
                    above = ref
            upper_mates = rung_mates(upper) or [upper]
            max_up = min(float(by_id[ref].y) for ref in upper_mates) - (power_floor + _GAP)
            if above is not None:
                need_above = (
                    max(
                        float(devices[above].schematic_height),
                        float(devices[upper].schematic_height),
                    )
                    + _GAP
                )
                max_up = min(
                    max_up,
                    float(by_id[upper].y) - (float(by_id[above].y) + need_above),
                )
            # Keep this rung's pin-Y conductor out of aux tags the horizontal
            # span will cross (preserve 109 vs the below-body "M1-13" tag).
            span_lo = min(float(by_id[ref].x) for ref in upper_mates)
            span_hi = max(
                float(by_id[ref].x) + float(devices[ref].schematic_width)
                for ref in upper_mates
            )
            pin0 = device_pin_y(upper)
            for other in ids:
                if other in upper_mates or other not in by_id:
                    continue
                if _role(by_id[other].library_id) != "aux":
                    continue
                ox = float(by_id[other].x)
                ow = float(devices[other].schematic_width)
                if ox + ow < span_lo - 1.0 or ox > span_hi + 1.0:
                    continue
                if float(by_id[other].y) >= pin0 - 0.5:
                    continue
                # ``_tag_floor`` includes a trailing gap; ink ends ``_GAP`` sooner.
                text_bottom = _tag_floor(by_id[other], devices[other]) - _GAP
                max_up = min(max_up, max(0.0, pin0 - text_bottom - _WIRE_CLEAR))
            up = min(dy, max(0.0, max_up))
            if up <= 0.05:
                continue
            moved += shift(upper_mates, -up)
            rem = dy - up
            if rem > 0.05:
                bottom2 = max(_bottom(by_id[ref], devices[ref]) for ref in lower_col) + rem
                if bottom2 <= limit + 0.2:
                    moved += shift(lower_mates, rem)
    # Title overflow: pull offenders up without crossing power_floor.
    # Use pin-Y rung mates (not the full wire cohort) so lifting CR1 does not
    # drag M1-23 back into CR1-13 after a column separation.
    ordered = sorted(ids, key=lambda ref: (float(by_id[ref].y), ref))
    for ref in list(ordered):
        bottom = _bottom(by_id[ref], devices[ref])
        if bottom <= limit + 0.2:
            continue
        band = rung_mates(ref) or [ref]
        up = bottom - limit
        max_up = max(0.0, min(float(by_id[item].y) for item in band) - (power_floor + _GAP))
        # Never pull a band up through a seal-in under the start (different
        # column). That pack made M1-13 look like it sat on CR1-13→PL1.
        band_top = min(float(by_id[item].y) for item in band)
        band_pin = min(device_pin_y(item) for item in band)
        span_lo = min(float(by_id[item].x) for item in band)
        span_hi = max(
            float(by_id[item].x) + float(devices[item].schematic_width) for item in band
        )
        for other in ids:
            if other in band or other not in by_id:
                continue
            if _role(by_id[other].library_id) != "aux":
                continue
            if set(rung_mates(other)) & set(band):
                continue
            # Seal-in under a start button: same column, drop band, and wired
            # to that start (cohort). Do not treat an aux→lamp series as seal-in.
            under_start = False
            other_pin = device_pin_y(other)
            other_mates = set(cohort(other))
            for start in ids:
                lib = by_id[start].library_id.lower()
                if "pb_no" not in lib or "aux" in lib:
                    continue
                if start not in other_mates:
                    continue
                if abs(float(by_id[start].x) - float(by_id[other].x)) > 22.0:
                    continue
                if 0.5 < other_pin - device_pin_y(start) <= _DROP_MM + _GAP + 2.0:
                    under_start = True
                    break
            ox = float(by_id[other].x)
            ow = float(devices[other].schematic_width)
            crosses_span = not (ox + ow < span_lo - 1.0 or ox > span_hi + 1.0)
            if not under_start and not crosses_span:
                continue
            # Clear the below-body tag ink, not just the symbol body — otherwise
            # squeeze pulls CR1/109 up through the "M1-13" text.
            other_clear = _tag_floor(by_id[other], devices[other])
            if float(by_id[other].y) < band_top + 0.2:
                max_up = min(max_up, band_top - other_clear)
            if crosses_span and float(by_id[other].y) < band_pin - 0.5:
                text_bottom = other_clear - _GAP
                max_up = min(max_up, max(0.0, band_pin - text_bottom - _WIRE_CLEAR))
        up = min(up, max_up)
        if up > 0.05:
            moved += shift(band, -up)
    if moved:
        clear_route_cache()
        _route_primary_return(session, sheet, by_id, devices)
    return moved


def _column_pin_x(sheet: Sheet, width: float = 16.0) -> float:
    from panelcad.core.ladder import rail_xs

    _left, right = rail_xs(sheet)
    return right - 8.0 - width


def _place_terminals(
    sheet,
    session,
    by_id,
    devices,
    adj,
    placed,
    floor: float,
    power_floor: float,
    control_ids: list[str] | None = None,
) -> int:
    """Terminals sit on a real rung, in tag order, each with a straight drop to L2.

    Spares share the rung of the terminal a contact feeds. The tie between
    them is that rung, not a loop around the blocks.
    """

    loose = [comp for comp in by_id.values() if comp.id not in placed and _role(comp.library_id) == "terminal"]
    loose.sort(key=lambda comp: comp.id)
    if not loose:
        return 0
    from panelcad.core.pin_geometry import pin_world
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
    host_comp = None
    for comp in loose:
        host = next((nbr for nbr in adj.get(comp.id, ()) if nbr in placed), None)
        if host is None:
            continue
        host_pin = _pin_named(devices[host], "", "left")
        if host_pin is None:
            continue
        _hx, hosted_y = pin_world(by_id[host], host_pin)
        host_comp = by_id[host]
        break
    pin_y = hosted_y if hosted_y is not None else min(floor + _GAP + half, limit)
    # A terminal strip must not share the body band of a right-hand load (OL/coil).
    # Hosting on OL1.95 used to park X1-* on top of OL1 and route through the body.
    if host_comp is not None and _is_rung_load(host_comp.library_id):
        host_dev = devices[host_comp.id]
        host_bottom = float(host_comp.y) + float(host_dev.schematic_height)
        pin_y = max(pin_y, host_bottom + half + _GAP, floor + _GAP + half)
    # Device-hosted terminals (seal-in → X1-1) stay on that rung. Only a spare
    # strip with no device host is pushed under every body — and only then do
    # we lift/compress the control stack for title room.
    host_role = _role(host_comp.library_id) if host_comp is not None else ""
    device_hosted = host_comp is not None and host_role not in {"terminal", "transformer", "fuse", ""}
    strip_pad = half + 2.0
    clear_y = pin_y
    if not device_hosted:
        for ref in placed:
            comp = by_id.get(ref)
            if comp is None or _role(comp.library_id) == "terminal":
                continue
            if ref not in devices:
                continue
            clear_y = max(clear_y, _bottom(comp, devices[ref]) + strip_pad)
    if (not device_hosted) and clear_y > limit + 0.2:
        allowed = {
            ref
            for ref in (control_ids or ())
            if ref in placed and ref in devices and by_id.get(ref) is not None
        }
        candidates = [
            ref
            for ref in placed
            if ref in devices
            and by_id.get(ref) is not None
            and _role(by_id[ref].library_id) not in {"terminal", "transformer", "fuse"}
            and (
                ref in allowed
                if allowed
                else float(by_id[ref].y) >= float(power_floor) + 8.0
            )
        ]
        # Fixed floor under the lift: power symbols plus any control already
        # packed onto them (seal-in under the starter). Those stay put.
        fixed_bottom = float(power_floor)
        for ref in placed:
            if ref not in devices or _role(by_id[ref].library_id) == "terminal":
                continue
            if ref in candidates:
                continue
            fixed_bottom = max(fixed_bottom, _bottom(by_id[ref], devices[ref]))
        control_refs = [
            ref
            for ref in candidates
            if float(by_id[ref].y) > fixed_bottom + _GAP + 0.5
        ]
        for ref in candidates:
            if ref not in control_refs:
                fixed_bottom = max(fixed_bottom, _bottom(by_id[ref], devices[ref]))
        if control_refs:
            from panelcad.render.wiring import clear_route_cache

            def _recompute_clear() -> float:
                value = pin_y
                for ref in placed:
                    comp = by_id.get(ref)
                    if comp is None or _role(comp.library_id) == "terminal":
                        continue
                    if ref not in devices:
                        continue
                    value = max(value, _bottom(comp, devices[ref]) + strip_pad)
                return value

            need = clear_y - limit
            top_y = min(float(by_id[ref].y) for ref in control_refs)
            room = max(0.0, top_y - (fixed_bottom + _GAP))
            up = min(need, room)
            if up > 0.05:
                for ref in control_refs:
                    by_id[ref].y = float(by_id[ref].y) - up
                clear_route_cache()
                clear_y = _recompute_clear()
            if clear_y > limit + 0.2:
                from panelcad.core.pin_geometry import pin_world as _pin_world

                def _band_key(ref: str) -> float:
                    pins = list(getattr(devices[ref], "pins", None) or [])
                    if not pins:
                        return round(float(by_id[ref].y), 1)
                    return round(float(_pin_world(by_id[ref], pins[0])[1]), 1)

                for _ in range(len(control_refs) + 2):
                    if clear_y <= limit + 0.2:
                        break
                    bands: dict[float, list[str]] = {}
                    for ref in control_refs:
                        bands.setdefault(_band_key(ref), []).append(ref)
                    ordered_keys = sorted(bands)
                    pulled = False
                    for i in range(1, len(ordered_keys)):
                        upper_refs = bands[ordered_keys[i - 1]]
                        lower_refs = bands[ordered_keys[i]]
                        # Column-gate: M1 on the starter row must not block
                        # pulling PL1/CR1 on the right-hand load column.
                        slacks: list[float] = []
                        for low in lower_refs:
                            lx = float(by_id[low].x)
                            col = [
                                up
                                for up in upper_refs
                                if abs(float(by_id[up].x) - lx) <= 22.0
                            ]
                            if not col:
                                continue
                            upper_bot = max(_bottom(by_id[up], devices[up]) for up in col)
                            slacks.append(float(by_id[low].y) - (upper_bot + _GAP))
                        if not slacks:
                            # No shared column — vertical overlap is fine.
                            slack = clear_y - limit
                        else:
                            slack = min(slacks)
                        if slack <= 0.05:
                            continue
                        pull = min(slack, clear_y - limit)
                        for ref in lower_refs:
                            by_id[ref].y = float(by_id[ref].y) - pull
                        pulled = True
                        clear_route_cache()
                        clear_y = _recompute_clear()
                        break
                    if not pulled:
                        break
    pin_y = min(clear_y, limit)
    pitch = width + 14.0
    x = x_right - pitch * (len(loose) - 1)
    if host_comp is not None and _is_rung_load(host_comp.library_id):
        host_right = float(host_comp.x) + float(devices[host_comp.id].schematic_width)
        if x < host_right + 6.0:
            x = host_right + 6.0
    # Right-hand loads that still share the strip's AABB step left.
    strip_left = x
    strip_right = x + pitch * max(len(loose) - 1, 0) + width
    strip_top = pin_y - half
    strip_bot = pin_y + half
    for ref in list(placed):
        comp = by_id.get(ref)
        if comp is None or ref not in devices:
            continue
        if not _is_rung_load(comp.library_id):
            continue
        left = float(comp.x)
        right = left + float(devices[ref].schematic_width)
        top = float(comp.y)
        bot = _bottom(comp, devices[ref])
        if bot <= strip_top + 0.5 or top >= strip_bot - 0.5:
            continue
        if right <= strip_left + 0.5 or left >= strip_right - 0.5:
            continue
        comp.x = strip_left - float(devices[ref].schematic_width) - 6.0
    moved = 0
    for comp in loose:
        moved += _place_on(comp, devices[comp.id], x, pin_y)
        comp.properties["ref_side"] = "left"
        placed.add(comp.id)
        x += pitch
    return moved


def _route_primary_return(session, sheet, by_id, devices) -> float | None:
    """Channel under the transformer for fuse↔rail hops (primary L2 or secondary L1).

    Primary fuse left of T1 often feeds L2 to the right. Secondary fuse right of
    T1 feeds L1 to the left. A far-pin landing exits outward first so the drop
    does not cut through the fuse body.
    """

    from panelcad.core.models import Point
    from panelcad.core.pin_geometry import parse_pin_ref
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
    from panelcad.core.pin_geometry import parse_pin_ref
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
    from panelcad.core.pin_geometry import parse_pin_ref

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

    from panelcad.core.pin_geometry import parse_pin_ref

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
    if pin is None:
        moved = int(abs(comp.x - x) > 0.5 or abs(comp.y - y) > 0.5)
        comp.x, comp.y = x, y
        return moved
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
    """A named pin, else the first pin on ``side``, else any pin.

    Pinless placeholders (``reconstruct.needs_info``) return ``None``. Callers
    must skip those devices — they block issue as an unread gap, not a crash.
    """

    pins = list(getattr(device, "pins", None) or [])
    if name:
        try:
            return device.pin(name)
        except KeyError:
            pass
    side_pins = [pin for pin in pins if str(pin.side) == side]
    if side_pins:
        return side_pins[0]
    if pins:
        return pins[0]
    return None


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
    if minimum + below <= limit + 0.2:
        return minimum
    # Last resort: park just above the title instead of sitting on it.
    return max(wy + below + _GAP, limit - below)


def _snap(session: Session, sheet: Sheet, y: float) -> float:
    from panelcad.core.zones import rung_index_at, rung_y

    index = max(1, rung_index_at(sheet, y, project=session.project))
    snapped = rung_y(sheet, index, project=session.project)
    if snapped < y - 0.4:
        snapped = rung_y(sheet, index + 1, project=session.project)
    return snapped


def _bottom(comp: Component, device: Any) -> float:
    return float(comp.y) + float(device.schematic_height)


def _tag_floor(comp: Component, device: Any) -> float:
    """Lowest Y occupied by the symbol body and its printed tag."""

    from panelcad.core.ink import TEXT_DESCENT, device_ref_layout, text_box
    from panelcad.core.ladder import is_aux_library

    body = _bottom(comp, device)
    if is_aux_library(comp.library_id) or comp.properties.get("tag_below"):
        # Aux tags sit below the body (baseline = bottom + 3.2, size 2.6).
        return body + 3.2 + 2.6 * float(TEXT_DESCENT) + _GAP
    layout = device_ref_layout(device, comp)
    if layout is None:
        return body
    box = text_box(layout[0], layout[1], comp.id, layout[3], layout[2])
    if box is None:
        return body
    return max(body, float(box[3]) + _GAP)


def _pin_y(comp: Component, device: Any) -> float:
    from panelcad.core.pin_geometry import pin_world

    pin = _pin_named(device, "", "left")
    if pin is None:
        return float(comp.y) + float(device.schematic_height) / 2.0
    return pin_world(comp, pin)[1]


def _prev_bottom(by_id, devices, refs: set[str]) -> float:
    if not refs:
        return 36.0
    return max(_bottom(by_id[ref], devices[ref]) for ref in refs)


def _drop(under, aux) -> float:
    under_pin = _pin_named(under, "", "left")
    aux_pin = _pin_named(aux, "", "left")
    if under_pin is None or aux_pin is None:
        return float(under.schematic_height) + _GAP
    below = float(under.schematic_height) - float(under_pin.y)
    return below + _GAP + float(aux_pin.y)


def _graph(sheet: Sheet, by_id: dict[str, Component]) -> dict[str, set[str]]:
    from panelcad.core.pin_geometry import parse_pin_ref

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
    """Sort control rungs: coil/relay, then pilot, then other loads.

    ``nema.relays.cr`` has no ``coil`` token but is the same kind of load as a
    contactor coil. Ranking it after a pilot packed CR1-13→PL1 under the M1
    seal-in and left M1-13 looking like it sat on the pilot rung.
    """

    lib = library_id.lower()
    if "coil" in lib or "relays." in lib or ".timers." in lib:
        return 1
    if "pilot" in lib or "lamp" in lib:
        return 2
    return 3


def _off_rung(sheet: Sheet, catalog: Catalog) -> list[str]:
    from panelcad.core.pin_geometry import parse_pin_ref, pin_world

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
    from panelcad.core.pin_geometry import parse_pin_ref
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
            if _straight_terminal_jumper(sheet, catalog, pts):
                continue
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
    """A wire through a fuse/transformer body, or a far-pin rail wire into the body.

    Device–device hops (T1.H4→FU2.1) used to skip the fuse in ``sheet_obstacles``
    and never hit this check because neither end was a rail. When the other end
    sits on the far side of the landed pin, a path through the body must fail.
    """

    from panelcad.core.ladder import endpoint_is_rail, rail_names
    from panelcad.core.route import path_hits_obstacles
    from panelcad.core.pin_geometry import parse_pin_ref, pin_world
    from panelcad.render.wiring import component_obstacle

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
        # Control-transformer secondary (X*) feeds L1/L2 from the right of the
        # block; the run around to the rail column is not a through-body hop.
        if (
            comp is not None
            and "transformer" in str(comp.library_id or "").lower()
            and str(pin_id).upper().startswith("X")
        ):
            return False
        want = "left" if rail_ref == hot else "right" if rail_ref == ret else ""
        if not want:
            return False
        for pin in device.pins:
            if str(pin.id) == pin_id:
                return str(pin.side) != want
        return False

    def power_far_from_neighbor(device_ref: str, pin_id: str, other_ref: str) -> bool:
        """True when other sits beyond the landed pin so a straight hop crosses the body."""

        comp = by_id.get(device_ref)
        other = by_id.get(other_ref)
        if comp is None or other is None or not _power_device(comp):
            return False
        if "offpage" in str(other.library_id or "").lower():
            return False
        device = _device(catalog, comp)
        if device is None or not pin_id:
            return False
        landed = next((pin for pin in device.pins if str(pin.id) == pin_id), None)
        if landed is None:
            return False
        px, _py = pin_world(comp, landed)
        side = str(landed.side)
        if side == "left" and other.x > px:
            return True
        if side == "right" and other.x < px:
            return True
        return False

    def hits_body(device_ref: str) -> bool:
        comp = by_id.get(device_ref)
        if comp is None or not pts:
            return True
        return path_hits_obstacles(pts, [component_obstacle(sheet, catalog, comp)], skip_ends=True)

    # Fuse/transformer ↔ neighbor on the far side of the landed pin.
    if power_far_from_neighbor(a_ref, a_pin, b_ref) and hits_body(a_ref):
        return True
    if power_far_from_neighbor(b_ref, b_pin, a_ref) and hits_body(b_ref):
        return True

    # Far-pin rail landings: fuse/transformer/contact bodies. Terminal A→L2
    # jumpers land on the left pin by design and must not fail here.
    device_ref = ""
    rail_ref = ""
    if endpoint_is_rail(sheet, wire.frm, None) and far_pin(a_ref, b_ref, b_pin):
        device_ref = b_ref
        rail_ref = a_ref
    elif endpoint_is_rail(sheet, wire.to, None) and far_pin(b_ref, a_ref, a_pin):
        device_ref = a_ref
        rail_ref = b_ref
    if not device_ref:
        return False
    device_comp = by_id.get(device_ref)
    if device_comp is None or _role(device_comp.library_id) == "terminal":
        return False
    # The pin faces the other rail. That is through the body only when the
    # drawn path enters the body. A jog that stays outside is a clean landing.
    # Secondary fuse → L1 must still fail when the path cuts through the fuse
    # body — the earlier exemption hid those through-body wires.
    return hits_body(device_ref)


def _straight_terminal_jumper(sheet: Sheet, catalog: Catalog, pts) -> bool:
    """A jumper that stays on the terminal pin line is the rung, not a loop."""

    from panelcad.core.route import path_hits_obstacles
    from panelcad.core.pin_geometry import pin_world
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
        pin = _pin_named(device, "", "left")
        if pin is None:
            continue
        _px, py = pin_world(comp, pin)
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


def _wires_cross_device_labels(session: Session, sheet: Sheet, catalog: Catalog) -> list[str]:
    """A conductor must not run through a device tag (e.g. 109 through M1-13)."""

    from panelcad.core.ink import text_box
    from panelcad.core.ladder import is_ladder_sheet
    from panelcad.render.sheet_labels import place_device_tags
    from panelcad.render.wiring import route_sheet_wires

    try:
        layouts = place_device_tags(
            sheet,
            catalog,
            ladder=is_ladder_sheet(sheet, session.project),
            project=session.project,
            sheet_no=1,
        )
    except Exception:  # noqa: BLE001
        return []
    try:
        routes = route_sheet_wires(sheet, catalog, project=session.project)
    except Exception:  # noqa: BLE001
        return []
    issues: list[str] = []
    for comp in sheet.components:
        layout = layouts.get(comp.id)
        if layout is None:
            continue
        box = text_box(layout[0], layout[1], comp.id, layout[3], layout[2])
        if box is None:
            continue
        left, top, right, bottom = box
        for wire in sheet.wires:
            # A wire that lands on this device may touch its tag near the pin.
            ends = {str(wire.frm).split(".")[0], str(wire.to).split(".")[0]}
            if comp.id in ends:
                continue
            pts = routes.get(wire.id) or []
            for a, b in zip(pts, pts[1:], strict=False):
                if _segment_hits_box(
                    float(a.x), float(a.y), float(b.x), float(b.y), left, top, right, bottom
                ):
                    mark = str(wire.number or wire.id)
                    issues.append(
                        f"{sheet.id}: wire {mark} runs through device label {comp.id}"
                    )
                    break
            if issues and issues[-1].endswith(f"label {comp.id}"):
                break
        if len(issues) >= 3:
            break
    return issues


def _segment_hits_box(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    left: float,
    top: float,
    right: float,
    bottom: float,
    *,
    pad: float = 0.35,
) -> bool:
    """True when the segment crosses the interior of the axis-aligned box."""

    left -= pad
    top -= pad
    right += pad
    bottom += pad
    # Reject endpoints that merely graze a corner outside the box.
    min_x, max_x = (x0, x1) if x0 <= x1 else (x1, x0)
    min_y, max_y = (y0, y1) if y0 <= y1 else (y1, y0)
    if max_x < left or min_x > right or max_y < top or min_y > bottom:
        return False
    # Horizontal / vertical rung segments (the common ladder case).
    if abs(y0 - y1) <= 0.8:
        return top <= y0 <= bottom and not (max_x < left or min_x > right)
    if abs(x0 - x1) <= 0.8:
        return left <= x0 <= right and not (max_y < top or min_y > bottom)
    # Generic: sample the segment.
    steps = max(4, int(max(abs(x1 - x0), abs(y1 - y0)) / 1.5))
    for i in range(steps + 1):
        t = i / steps
        x = x0 + (x1 - x0) * t
        y = y0 + (y1 - y0) * t
        if left <= x <= right and top <= y <= bottom:
            return True
    return False


def _body_problems(session: Session, sheet: Sheet, catalog: Catalog) -> list[str]:
    from panelcad.core.route import component_aabb
    from panelcad.core.pin_geometry import pin_world
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
        if pin is None:
            continue
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
    from panelcad.core.pin_geometry import parse_pin_ref as _parse

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

    from panelcad.core.pin_geometry import pin_world
    from panelcad.render.sheet_labels import place_device_tags

    issues: list[str] = []
    terminals: list[tuple[str, float, float, float]] = []
    device_ys: list[float] = []
    for comp in sheet.components:
        device = _device(catalog, comp)
        if device is None:
            continue
        pin = _pin_named(device, "", "left")
        if pin is None:
            continue
        _px, py = pin_world(comp, pin)
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
    from panelcad.core.pin_geometry import parse_pin_ref

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


def _fuse_dangling_pins(sheet: Sheet, catalog: Catalog) -> list[str]:
    """A fuse with only one pin wired leaves the other end as a dangling stub.

    The line-side pin is terminated by a wire to an off-page/source arrow (never
    by inventing a hop to this sheet's control rails). ``_rail_gaps`` misses an
    empty pin when layout parks the fuse outside the rail column.
    """

    from panelcad.core.pin_geometry import parse_pin_ref

    issues: list[str] = []
    for comp in sheet.components:
        if "fuse" not in str(comp.library_id or "").lower():
            continue
        device = _device(catalog, comp)
        if device is None or not device.pins:
            continue
        wired: set[str] = set()
        for wire in sheet.wires:
            for text in (wire.frm, wire.to):
                try:
                    ref, pin = parse_pin_ref(text)
                except ValueError:
                    continue
                if ref == comp.id and pin:
                    wired.add(str(pin))
        if not wired:
            continue
        for pin in device.pins:
            pid = str(pin.id)
            if pid not in wired:
                issues.append(f"{sheet.id}: {comp.id} pin {pid} is a dangling stub")
    return issues


def _fuse_across_rails(sheet: Sheet) -> list[str]:
    """A fuse whose only path runs from one rail to the other is not a feeder."""

    from panelcad.core.pin_geometry import parse_pin_ref

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

    from panelcad.core.pin_geometry import parse_pin_ref

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
            # A printed open stub on the other pin is intentional — not a bare rail tie.
            comp = by_id.get(device)
            props = comp.properties if comp is not None and isinstance(comp.properties, dict) else {}
            stubs = {
                part.strip()
                for part in str(props.get("print_open_stubs") or "").split(",")
                if part.strip()
            }
            if stubs:
                continue
            key = (device, rail)
            if key in seen:
                continue
            seen.add(key)
            issues.append(f"{sheet.id}: {device} is wired to {rail} with no path through a load")
    return issues


def _contacts_only_rungs(sheet: Sheet) -> list[str]:
    """An L1-to-L2 path of only contacts is not a rung. It must not be issued."""

    from panelcad.core.pin_geometry import parse_pin_ref

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
    from panelcad.core.pin_geometry import parse_pin_ref

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
    from panelcad.core.pin_geometry import parse_pin_ref, pin_world

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


def _transformer_control_rules(session: Session, sheet: Sheet) -> list[str]:
    """Control transformer primary never lands on L1/L2; X1 feeds L1 through a secondary fuse."""

    from panelcad.core.ladder import rail_names
    from panelcad.importer.electrical_rules import validate_transformer_control_rules

    hot, ret = rail_names(sheet, session.project)
    violations = validate_transformer_control_rules(sheet, hot_rail=hot, return_rail=ret)
    return [f"{sheet.id}: {item.message}" for item in violations]


def _loads_on_the_right(session: Session, sheet: Sheet, catalog: Catalog) -> list[str]:
    """A coil, overload, or pilot belongs just left of L2, not mid-sheet."""

    from panelcad.core.ladder import rail_xs
    from panelcad.core.pin_geometry import pin_world

    _left, right = rail_xs(sheet)
    issues: list[str] = []
    # (comp.id, left_x, right_x, is_load)
    by_y: dict[float, list[tuple[str, float, float, bool]]] = {}
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
        if load:
            if not pins:
                continue
            px, py = pin_world(comp, pins[-1])
            left_pin = _pin_named(device, "", "left")
            lx = pin_world(comp, left_pin)[0] if left_pin is not None else float(comp.x)
            by_y.setdefault(round(py, 1), []).append((comp.id, min(float(comp.x), lx), px, True))
        else:
            left_pin = _pin_named(device, "", "left")
            if left_pin is None:
                continue
            px, py = pin_world(comp, left_pin)
            by_y.setdefault(round(py, 1), []).append((comp.id, px, px, False))
    for group in by_y.values():
        loads = sorted((item for item in group if item[3]), key=lambda item: item[2])
        if not loads:
            continue
        rightmost = loads[-1]
        if right - rightmost[2] > 36.0:
            issues.append(f"{sheet.id}: {rightmost[0]} is not next to L2")
        # Every load in the right-hand cluster (e.g. coil M1 to the left of OL1)
        # must sit beside its right neighbor, never parked mid-sheet.
        for idx in range(len(loads) - 2, -1, -1):
            cur = loads[idx]
            nxt = loads[idx + 1]
            if nxt[1] - cur[2] > 36.0:
                issues.append(f"{sheet.id}: {cur[0]} is not next to L2")
        for name, lx, _rx, is_load in group:
            if is_load:
                continue
            if lx > rightmost[2] + 2.0:
                issues.append(f"{sheet.id}: {name} sits to the right of the coil")
    return issues
