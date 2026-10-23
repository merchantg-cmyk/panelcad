"""Cleanup sweep: unstack overlapping symbols, fit I/O field devices to rungs.

AIs should run Session.cleanup() after placing / layout_io so prints are readable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from panelcad.core.layout import footprint_size, is_generated_io_sheet, is_io_sheet, is_layout_sheet
from panelcad.library.panel import effective_footprint
from panelcad.core.models import Component, Sheet
from panelcad.core.route import Rect, component_aabb
from panelcad.render.sheet import parse_pin_ref, pin_world
from panelcad.render.titleblock import working_rect

if TYPE_CHECKING:
    from panelcad.api.session import Session
    from panelcad.library.catalog import Catalog

IO_FIELD_MAX_H = 12.0
GAP = 2.0
SCHEMATIC_GAP = 18.0
# Visible wire lead between the last output on a rung and the return rail.
# Outputs sit close to the rail in one right-aligned column, never against
# it. Same margin the circuit builder and arrange_ladder use (rail - 14 mm).
OUTPUT_RAIL_GAP = 14.0
# Widest gap kept between outputs in series at the rail (coil + OL): the
# builder's symbol spacing, so built rungs are not re-spaced.
OUTPUT_SERIES_GAP = SCHEMATIC_GAP
OUTPUT_SEAT_BENDS = 2


def is_offpage(library_id: str) -> bool:
    return "connection.offpage" in library_id


def sheet_number(sheets: list[Sheet], sheet_id: str) -> int:
    for i, sh in enumerate(sheets, 1):
        if sh.id == sheet_id:
            return i
    return 0


def field_wired_pin(sheet: Sheet, ref: str) -> str | None:
    for w in sheet.wires:
        for end in (w.frm, w.to):
            try:
                cid, pid = parse_pin_ref(end)
            except ValueError:
                continue
            if cid == ref:
                return pid
    return None


def _aabb(sheet: Sheet, catalog: Catalog, comp: Component) -> Rect:
    from panelcad.render.wiring import _symbol_size

    device = catalog.get(comp.library_id)
    if is_layout_sheet(sheet):
        w, h = footprint_size(device, comp)
        return component_aabb(comp.x, comp.y, w, h, 0, False)
    w, h = _symbol_size(sheet, catalog, comp, device)
    s = comp.visual_scale()
    rot = 0 if is_layout_sheet(sheet) else comp.rotation
    return component_aabb(comp.x, comp.y, w * s, h * s, rot, comp.mirror)


def _overlap(a: Rect, b: Rect, pad: float = 0.4) -> bool:
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0] or a[3] + pad <= b[1] or b[3] + pad <= a[1])


def _clear_routes(sheet: Sheet) -> None:
    for w in sheet.wires:
        w.points = []


def arrange_io(sheet: Sheet, catalog: Catalog) -> dict[str, Any]:
    """Put I/O cards in ANSI columns and park field devices on their rungs, not on the card."""
    from panelcad.render.io_ladder import (
        LEFT_X,
        RIGHT_X,
        TOP_Y,
        io_card_column,
        io_card_size,
        io_endpoint,
        io_field_is_left,
        io_module_kind,
        io_pin_is_input,
        io_pin_layout,
        is_io_module,
    )

    modules = [c for c in sheet.components if is_io_module(catalog.get(c.library_id))]
    others = [c for c in sheet.components if c not in modules]
    col_y = {"input": TOP_Y, "output": TOP_Y, "combo": TOP_Y}
    for m in modules:
        device = catalog.get(m.library_id)
        kind = io_module_kind(device)
        _w, h = io_card_size(device, m)
        m.x, m.y = io_card_column(device), col_y[kind]
        col_y[kind] += h + 12.0

    pin_y: dict[str, float] = {}
    pin_in: dict[str, bool] = {}
    for m in modules:
        device = catalog.get(m.library_id)
        laid = io_pin_layout(device)
        s = m.visual_scale()
        for pid, (_lx, ly) in laid.items():
            pin_y[f"{m.id}.{pid}"] = m.y + ly * s
            flag = io_pin_is_input(device, pid)
            if flag is not None:
                pin_in[f"{m.id}.{pid}"] = flag

    aligned: dict[str, tuple[float, bool]] = {}
    for w in sheet.wires:
        for ref, other in ((w.frm, w.to.split(".", 1)[0]), (w.to, w.frm.split(".", 1)[0])):
            if ref in pin_y and other not in {m.id for m in modules} and ref in pin_in:
                aligned[other] = (pin_y[ref], pin_in[ref])

    scaled = 0
    left_y, right_y = TOP_Y, TOP_Y
    for o in others:
        device = catalog.get(o.library_id)
        h = max(float(device.schematic_height), 1.0)
        if not is_offpage(o.library_id) and h > IO_FIELD_MAX_H:
            o.scale = max(0.15, min(1.0, IO_FIELD_MAX_H / h))
            scaled += 1
        if is_offpage(o.library_id):
            o.x = LEFT_X if "offpage_from" in o.library_id else RIGHT_X
        elif o.id in aligned:
            _py, to_in = aligned[o.id]
            o.x = LEFT_X if io_field_is_left(device, to_in) else RIGHT_X
        elif io_field_is_left(device):
            o.x, o.y = LEFT_X, left_y
            left_y += IO_FIELD_MAX_H + GAP
        else:
            o.x, o.y = RIGHT_X, right_y
            right_y += IO_FIELD_MAX_H + GAP

        pid = field_wired_pin(sheet, o.id)
        if pid and o.id in aligned:
            try:
                pin = device.pin(pid)
            except KeyError:
                continue
            _wx, wy = pin_world(o, pin)
            o.y += aligned[o.id][0] - wy
    # Keep field bodies off the card face.
    for o in others:
        oa = _aabb(sheet, catalog, o)
        for m in modules:
            ma = _aabb(sheet, catalog, m)
            if not _overlap(oa, ma, pad=1.0):
                continue
            gap = 6.0
            if oa[2] > ma[0] and oa[0] < ma[0] + 8:
                o.x -= (oa[2] - ma[0]) + gap
            elif oa[0] < ma[2] and oa[2] > ma[2] - 8:
                o.x += (ma[2] - oa[0]) + gap
            oa = _aabb(sheet, catalog, o)

    _clear_routes(sheet)
    return {"modules": len(modules), "field": len(others), "scaled": scaled}


def fit_io_fields(sheet: Sheet, catalog: Catalog) -> dict[str, Any]:
    """Back-compat wrapper — full I/O pack lives in arrange_io."""
    return arrange_io(sheet, catalog)



def unstack(sheet: Sheet, catalog: Catalog, *, skip_modules: bool = False) -> int:
    from panelcad.render.io_ladder import is_io_module

    movable = []
    for c in sheet.components:
        if skip_modules and is_io_module(catalog.get(c.library_id)):
            continue
        if is_layout_sheet(sheet) and effective_footprint(catalog.get(c.library_id), c).din_rail:
            continue
        movable.append(c)
    moved = 0
    for _ in range(40):
        progressed = False
        boxes = [(c, _aabb(sheet, catalog, c)) for c in movable]
        for i, (a, ra) in enumerate(boxes):
            for b, rb in boxes[i + 1 :]:
                if not _overlap(ra, rb):
                    continue
                overlap_y = min(ra[3], rb[3]) - max(ra[1], rb[1])
                lower = b if rb[1] >= ra[1] else a
                lower.y += max(overlap_y, GAP) + GAP
                progressed = True
                moved += 1
        if not progressed:
            break
    return moved

_POWER_POLES = {"L1", "L2", "L3", "T1", "T2", "T3"}
_OL_POWER = {"1", "2", "3", "4", "5", "6"}
_OL_CTRL = {"95", "96", "97", "98"}
_RUNG_PITCH = 56.0


def _used_pins(sheet: Sheet, ref: str) -> set[str]:
    pins: set[str] = set()
    for wire in sheet.wires:
        for end in (wire.frm, wire.to):
            try:
                cid, pid = parse_pin_ref(end)
            except ValueError:
                continue
            if cid == ref:
                pins.add(pid)
    return pins


def _prefix(library_id: str) -> str:
    return "nema" if str(library_id).startswith("nema.") else "iec"


def _swap_library(comp: Component, catalog: Catalog, sheet: Sheet, new_id: str) -> bool:
    if new_id not in catalog.devices:
        return False
    keep = {p.id for p in catalog.get(new_id).pins}
    used = _used_pins(sheet, comp.id)
    if used - keep:
        return False
    if comp.library_id == new_id:
        return False
    comp.library_id = new_id
    return True


def demote_power_to_control(sheet: Sheet, catalog: Catalog) -> int:
    """A 3-pole device with only coil/control pins is a control symbol."""

    n = 0
    for comp in sheet.components:
        lib = comp.library_id.lower()
        used = _used_pins(sheet, comp.id)
        if not used:
            continue
        fam = _prefix(comp.library_id)
        if "contactors.ac3" in lib and not ({p.upper() for p in used} & _POWER_POLES):
            if _swap_library(comp, catalog, sheet, f"{fam}.contactors.coil"):
                n += 1
        elif "protection.overload" in lib and "ol_contact" not in lib:
            up = set(used)
            if up and up <= _OL_CTRL:
                if _swap_library(comp, catalog, sheet, f"{fam}.protection.ol_contact"):
                    n += 1
        elif "breakers.mcb_3p" in lib and used <= {"1", "2"}:
            if _swap_library(comp, catalog, sheet, f"{fam}.breakers.mcb_1p_16a"):
                n += 1
    return n


def _wire_graph(sheet: Sheet) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {c.id: set() for c in sheet.components}
    for wire in sheet.wires:
        try:
            a, _ = parse_pin_ref(wire.frm)
            b, _ = parse_pin_ref(wire.to)
        except ValueError:
            continue
        if a in adj and b in adj and a != b:
            adj[a].add(b)
            adj[b].add(a)
    return adj


def _longest_path(adj: dict[str, set[str]]) -> list[str]:
    best: list[str] = []

    def dfs(node: str, seen: set[str], path: list[str]) -> None:
        nonlocal best
        if len(path) > len(best):
            best = list(path)
        for nxt in sorted(adj.get(node, ())):
            if nxt not in seen:
                dfs(nxt, seen | {nxt}, path + [nxt])

    for start in adj:
        dfs(start, {start}, [start])
    return best


def _rung_rank(library_id: str) -> int:
    lib = library_id.lower()
    if "fuse" in lib or "breaker" in lib or "disconnect" in lib:
        return 0
    if "pb_nc" in lib or "estop" in lib:
        return 1
    if "pb_no" in lib or "selector" in lib or "limit" in lib:
        return 2
    if "ol_contact" in lib or "overload" in lib:
        return 7
    if "terminals.pe" in lib or lib.endswith(".pe") or ".pe_" in lib:
        return 8
    if "coil" in lib or "contactor" in lib or "relay" in lib or "timer" in lib:
        return 6
    if "motor" in lib:
        return 9
    return 5


def orient_two_pin_flow(sheet: Sheet, catalog: Catalog, ref: str) -> bool:
    """Fuse / OL contact: 0° in a horizontal run, 90° in a vertical drop (JIC H vs V)."""

    from panelcad.core.ladder import endpoint_is_rail, is_iec_column_sheet
    from panelcad.render.sheet import parse_pin_ref

    # Column sheets already stand the contact in the current path. Re-orienting
    # from component origins turns the PLC I/O overload flat and into its neighbor.
    if is_iec_column_sheet(sheet):
        return False
    try:
        comp = sheet.component(ref)
    except KeyError:
        return False
    device = catalog.get(comp.library_id)
    lib = comp.library_id.lower()
    if "fuse" not in lib and "ol_contact" not in lib:
        return False
    if len(device.pins) != 2:
        return False
    xs: list[float] = []
    ys: list[float] = []
    for wire in sheet.wires:
        for end, other in ((wire.frm, wire.to), (wire.to, wire.frm)):
            try:
                cid, _pid = parse_pin_ref(end)
            except ValueError:
                continue
            if cid != ref:
                continue
            if endpoint_is_rail(sheet, other):
                continue
            try:
                oid, _ = parse_pin_ref(other)
                other_c = sheet.component(oid)
            except (ValueError, KeyError):
                continue
            xs.append(other_c.x - comp.x)
            ys.append(other_c.y - comp.y)
    if not xs:
        return False
    # The nearest device is the run this symbol belongs to. A nearby horizontal
    # partner wins over a slightly closer branch below, so spreading a rung
    # does not stand the contact up. A long tap still must not flip it.
    ranked = sorted(range(len(xs)), key=lambda index: (xs[index] * xs[index] + ys[index] * ys[index], index))
    nearest = ranked[0]
    horizontal = [index for index in ranked if abs(xs[index]) + 4.0 >= abs(ys[index])]
    if horizontal:
        partner = horizontal[0]
        near_d = xs[nearest] * xs[nearest] + ys[nearest] * ys[nearest]
        part_d = xs[partner] * xs[partner] + ys[partner] * ys[partner]
        if part_d <= near_d * 2.25:
            nearest = partner
    dx = abs(xs[nearest])
    dy = abs(ys[nearest])
    want = 90 if dy > dx + 4.0 else 0
    if int(comp.rotation) == want:
        return False
    comp.rotation = want  # type: ignore[assignment]
    return True


def _is_power_load(library_id: str) -> bool:
    lib = library_id.lower()
    return "motor" in lib or "loads.solenoid" in lib


def _is_supply(library_id: str) -> bool:
    lib = library_id.lower()
    return "psu" in lib or "control_transformer" in lib


def _is_rung_output(library_id: str) -> bool:
    """Coil / OL / timer — the THEN, parked at the return rail. Aux is an input."""

    from panelcad.core.ladder import is_lamp_library

    lib = str(library_id or "").lower()
    if "aux" in lib:
        return False
    if is_lamp_library(library_id):
        return True
    if "ol_contact" in lib:
        return True
    if "coil" in lib or "relays." in lib or "timer" in lib:
        return True
    if "contactors." in lib:
        return True
    return False


def _rung_pin_y_offset(catalog: Catalog, comp: Component) -> float:
    device = catalog.get(comp.library_id)
    pins = [p for p in device.pins if p.side in ("left", "right")]
    if not pins:
        return float(device.schematic_height) / 2.0
    return float(pins[0].y)


def arrange_ladder(sheet: Sheet, catalog: Catalog) -> int:
    """Contacts from L1, coils/lamps/OL at L2. Hold-in aux under Start when no lamp."""

    from panelcad.core.ladder import HOLD_DROP, is_aux_library, is_lamp_library, rail_xs
    from panelcad.render.wiring import _symbol_size

    left, right = rail_xs(sheet)
    _wx, wy, _ww, _wh = working_rect(sheet)
    contacts: list[Component] = []
    outputs: list[Component] = []
    auxes: list[Component] = []
    lamps: list[Component] = []
    loads: list[Component] = []
    for comp in sheet.components:
        if is_offpage(comp.library_id):
            continue
        if _is_power_load(comp.library_id) or _is_supply(comp.library_id):
            # A power supply already sits on its own rung above the ladder.
            # Pulling it into the contact row puts the motor wire through it.
            if _is_supply(comp.library_id):
                continue
            loads.append(comp)
        elif is_aux_library(comp.library_id):
            auxes.append(comp)
        elif is_lamp_library(comp.library_id):
            lamps.append(comp)
        elif _is_rung_output(comp.library_id):
            outputs.append(comp)
        else:
            contacts.append(comp)
    if not (contacts or outputs or auxes or lamps or loads):
        return 0
    contacts.sort(key=lambda c: (_rung_rank(c.library_id), c.id))
    # Coil and overload follow the wire, so the device that feeds the return
    # rail is the right-hand one. Rank alone parked the overload past the coil.
    path_at = {ref: index for index, ref in enumerate(_longest_path(_wire_graph(sheet)))}
    outputs.sort(key=lambda c: (path_at.get(c.id, 1000 + _rung_rank(c.library_id)), c.id))
    auxes.sort(key=lambda c: c.id)
    lamps.sort(key=lambda c: c.id)
    loads.sort(key=lambda c: c.id)
    rung_y = wy + 42.0
    gap = 32.0
    inner_left = left + 14.0
    inner_right = right - 14.0
    moved = 0

    def _width(comp: Component) -> float:
        device = catalog.get(comp.library_id)
        w, _h = _symbol_size(sheet, catalog, comp, device)
        # Library size, not the current scale. A second Cleanup used to place
        # a shrunk symbol, then the pack pass shrunk it again.
        return max(w, 24.0)

    def _drop(comp: Component, x: float, pin_y: float) -> None:
        nonlocal moved
        lib = comp.library_id.lower()
        if ("fuse" in lib or "ol_contact" in lib) and int(comp.rotation) != 0:
            moved += 1
            comp.rotation = 0  # type: ignore[assignment]
        ny = pin_y - _rung_pin_y_offset(catalog, comp)
        if abs(comp.x - x) > 0.5 or abs(comp.y - ny) > 0.5:
            moved += 1
        comp.x, comp.y = x, ny

    def _place_left(items: list[Component], pin_y: float, x0: float) -> float:
        x = x0
        for comp in items:
            _drop(comp, x, pin_y)
            x += _width(comp) + gap
            if x > inner_right:
                x = x0
                pin_y += 36.0
        return x

    def _place_right(items: list[Component], pin_y: float, x_right: float, x_min: float) -> None:
        if not items:
            return
        total = sum(_width(c) for c in items) + gap * (len(items) - 1)
        x = x_right - total
        if x < x_min:
            x = x_min
        for comp in items:
            _drop(comp, x, pin_y)
            x += _width(comp) + gap

    free = _place_left(contacts, rung_y, inner_left)
    _place_right(outputs, rung_y, inner_right, free)
    anchor = next((c for c in contacts if "pb_no" in c.library_id.lower()), None)
    if anchor is None:
        anchor = next((c for c in contacts if _rung_rank(c.library_id) == 2), None)
    if anchor is None and contacts:
        anchor = contacts[min(1, len(contacts) - 1)]
    if lamps:
        free_p = _place_left(auxes, rung_y + HOLD_DROP, inner_left)
        _place_right(lamps, rung_y + HOLD_DROP, inner_right, free_p)
    else:
        for i, aux in enumerate(auxes):
            pin_y = rung_y + HOLD_DROP + i * 22.0
            nx = anchor.x if anchor is not None else inner_left
            _drop(aux, nx, pin_y)
    if loads:
        _place_right(loads, rung_y + 64.0, inner_right, inner_left + 24.0)
    return moved


def complete_ladder_rungs(session: "Session", sheet: Sheet) -> int:
    """Attach an existing series path to both rails. Do not invent device-to-device wires."""

    from panelcad.core.ladder import is_aux_library, is_lamp_library, is_rail_ref, rail_names

    hot, ret = rail_names(sheet, session.project)
    n = 0
    adj = _wire_graph(sheet)
    for ref, comp in {c.id: c for c in sheet.components}.items():
        if is_aux_library(comp.library_id) or is_lamp_library(comp.library_id) or _is_power_load(comp.library_id):
            adj.pop(ref, None)
            for nbrs in adj.values():
                nbrs.discard(ref)
    control = [r for r in _longest_path(adj) if r in adj]
    n += _attach_path_to_rails(session, sheet, hot, ret, control)

    padj = _wire_graph(sheet)
    keep = {
        c.id
        for c in sheet.components
        if is_aux_library(c.library_id) or is_lamp_library(c.library_id)
    }
    for ref in list(padj):
        if ref not in keep:
            padj.pop(ref, None)
    for nbrs in padj.values():
        nbrs.intersection_update(keep)
    parallel = [r for r in _longest_path(padj) if r in padj]
    n += _attach_path_to_rails(session, sheet, hot, ret, parallel)
    return n


def _series_reaches_both_rails(session: "Session", sheet: Sheet) -> bool:
    """True when the series path already lands on the hot rail and the return.

    Cleanup must not rebuild that ladder. Picking the rail ends by x position
    wired the overload contact to the return and shorted the coil.
    """

    from panelcad.core.ladder import is_aux_library, is_lamp_library, rail_names

    hot, ret = rail_names(sheet, session.project)
    if not hot or not ret:
        return False
    adj = _wire_graph(sheet)
    for ref, comp in {c.id: c for c in sheet.components}.items():
        if is_aux_library(comp.library_id) or is_lamp_library(comp.library_id) or _is_power_load(comp.library_id):
            adj.pop(ref, None)
            for nbrs in adj.values():
                nbrs.discard(ref)
    path = [ref for ref in _longest_path(adj) if ref in adj]
    if len(path) < 2:
        return False
    return _path_reaches_rail(sheet, path, hot, hot, ret) and _path_reaches_rail(sheet, path, ret, hot, ret)


def _path_reaches_rail(sheet: Sheet, path: list[str], rail: str, hot: str, ret: str) -> bool:
    from panelcad.core.ladder import is_rail_ref

    names = {ref.upper() for ref in path}
    for wire in sheet.wires:
        devices = []
        rails = []
        for end in (wire.frm, wire.to):
            if is_rail_ref(end, hot, ret):
                rails.append(str(end).split(".", 1)[0])
            else:
                devices.append(end.split(".", 1)[0].upper())
        if any(str(item).upper() == str(rail).upper() for item in rails) and any(dev in names for dev in devices):
            return True
    return False


def _fed_offpage(sheet: Sheet, start: str) -> bool:
    """True when the hot end of a rung (*start*) is fed by an off-page connector.

    Only the rung's start counts. An arrow tapped off the middle or the load
    end of a rung carries that rung onward to another sheet; the rung itself
    still needs its hot rail wire.
    """

    pages = {c.id for c in sheet.components if is_offpage(c.library_id)}
    if start in pages:
        return True
    for wire in sheet.wires:
        try:
            a, _ = parse_pin_ref(wire.frm)
            b, _ = parse_pin_ref(wire.to)
        except ValueError:
            continue
        if (a == start and b in pages) or (b == start and a in pages):
            return True
    return False


def _attach_path_to_rails(session: "Session", sheet: Sheet, hot: str, ret: str, path: list[str]) -> int:
    if len(path) < 2:
        return 0
    # The path ends are the circuit ends. x position is not the circuit order:
    # a contact placed to the right of the coil is not the return end.
    start, end = path[0], path[-1]

    def _on_rail(ref: str, rail: str) -> bool:
        return _path_reaches_rail(sheet, [ref], rail, hot, ret)

    if _on_rail(end, hot) and not _on_rail(start, hot):
        start, end = end, start
    elif _on_rail(start, ret) and not _on_rail(end, ret):
        start, end = end, start
    elif _on_rail(end, ret) and not _on_rail(start, ret):
        pass
    elif not _on_rail(start, hot) and not _on_rail(end, hot):
        if _rung_rank(sheet.component(start).library_id) > _rung_rank(sheet.component(end).library_id):
            start, end = end, start
    n = 0

    def _left_pin(ref: str) -> str:
        device = session.catalog.get(sheet.component(ref).library_id)
        pins = [p.id for p in device.pins if p.side == "left"] or [p.id for p in device.pins]
        return f"{ref}.{pins[0]}"

    def _right_pin(ref: str) -> str:
        device = session.catalog.get(sheet.component(ref).library_id)
        pins = [p.id for p in device.pins if p.side == "right"] or [p.id for p in device.pins]
        return f"{ref}.{pins[-1]}"

    # A device whose other side already reaches a rail through this path does
    # not get a second rail wire. That second wire is what shorted the coil.
    # A rung continued from another sheet is fed through its off-page
    # connector. A hot rail wire here would tie that number to L1 and relabel
    # every L1 wire in the project.
    if not _path_reaches_rail(sheet, path, hot, hot, ret) and not _fed_offpage(sheet, start):
        session.wire(hot, _left_pin(start), net=hot, sheet_id=sheet.id)
        n += 1
    if not _path_reaches_rail(sheet, path, ret, hot, ret):
        session.wire(_right_pin(end), ret, net=ret, sheet_id=sheet.id)
        n += 1
    return n


def arrange_schematic(sheet: Sheet, catalog: Catalog) -> int:
    """Put a wired chain on one left-to-right control/power rung."""

    from panelcad.render.wiring import _symbol_size

    if len(sheet.components) < 2:
        return 0
    adj = _wire_graph(sheet)
    path = _longest_path(adj)
    if len(path) < 2:
        return 0
    by_id = {c.id: c for c in sheet.components}
    if _rung_rank(by_id[path[0]].library_id) > _rung_rank(by_id[path[-1]].library_id):
        path = list(reversed(path))
    wx, wy, _ww, _wh = working_rect(sheet)
    x = wx + 18.0
    y = wy + 40.0
    moved = 0
    placed = set()
    for ref in path:
        comp = by_id[ref]
        device = catalog.get(comp.library_id)
        w, h = _symbol_size(sheet, catalog, comp, device)
        s = comp.visual_scale()
        if abs(comp.x - x) > 0.5 or abs(comp.y - y) > 0.5:
            moved += 1
        comp.x, comp.y = x, y
        x += max(w * s, 24.0) + 28.0
        placed.add(ref)
    extra_y = y + 48.0
    extra_x = wx + 18.0
    for comp in sheet.components:
        if comp.id in placed or is_offpage(comp.library_id):
            continue
        nbr = next((n for n in adj[comp.id] if n in placed), None)
        if nbr:
            comp.x = by_id[nbr].x
            comp.y = extra_y
        else:
            comp.x, comp.y = extra_x, extra_y
            extra_x += 48.0
        moved += 1
    return moved


def spread(sheet: Sheet, catalog: Catalog) -> int:
    """Push schematic symbols apart so wires and names can be read."""

    movable = [c for c in sheet.components if not is_offpage(c.library_id)]
    moved = 0
    for _ in range(48):
        progressed = False
        boxes = [(c, _aabb(sheet, catalog, c)) for c in movable]
        boxes.sort(key=lambda item: (item[1][0], item[1][1], item[0].id))
        for i, (a, ra) in enumerate(boxes):
            for b, rb in boxes[i + 1 :]:
                y_over = min(ra[3], rb[3]) - max(ra[1], rb[1])
                x_over = min(ra[2], rb[2]) - max(ra[0], rb[0])
                if y_over > 0.5:
                    left, right, left_box, right_box = (
                        (a, b, ra, rb) if ra[0] <= rb[0] else (b, a, rb, ra)
                    )
                    gap = right_box[0] - left_box[2]
                    if gap < SCHEMATIC_GAP:
                        right.x += SCHEMATIC_GAP - gap
                        progressed = True
                        moved += 1
                        break
                elif x_over > 0.5:
                    upper, lower, up_box, low_box = (
                        (a, b, ra, rb) if ra[1] <= rb[1] else (b, a, rb, ra)
                    )
                    gap = low_box[1] - up_box[3]
                    if gap < SCHEMATIC_GAP:
                        lower.y += SCHEMATIC_GAP - gap
                        progressed = True
                        moved += 1
                        break
            if progressed:
                break
        if not progressed:
            break
    return moved


def clamp_working(sheet: Sheet, catalog: Catalog, skip: set[str] | None = None) -> int:
    wx, wy, ww, wh = working_rect(sheet)
    x1, y1 = wx + ww, wy + wh
    n = 0
    leave = skip or set()
    for comp in sheet.components:
        if comp.id in leave:
            continue
        x0, y0, xa, ya = _aabb(sheet, catalog, comp)
        dx = dy = 0.0
        if xa - x0 <= ww:
            if x0 < wx:
                dx = wx - x0
            elif xa > x1:
                dx = x1 - xa
        if ya - y0 <= wh:
            if y0 < wy:
                dy = wy - y0
            elif ya > y1:
                dy = y1 - ya
        if dx or dy:
            comp.x += dx
            comp.y += dy
            n += 1
    return n


def _rail_wired(sheet: Sheet, ref: str, rail: str) -> bool:
    from panelcad.core.ladder import _head

    for wire in sheet.wires:
        ends = (str(wire.frm or ""), str(wire.to or ""))
        heads = {_head(end).upper() for end in ends}
        if ref.upper() in heads and rail.upper() in {end.strip().upper() for end in ends}:
            return True
    return False


def _output_runs(session: Session, sheet: Sheet) -> list[list[Component]]:
    """Per rung, the outputs at its right end that land on the return rail.

    A rung is the set of symbols sharing one pin line. Its trailing outputs
    (coil, relay, timer, pilot light, OL contact; see _is_rung_output) are the
    run, in x order, provided the last one is wired to the return rail. A rung
    that ends in anything else (a terminal, a PLC card) has no run.
    """

    from panelcad.core.ladder import rail_names
    from panelcad.core.power_layout import _cluster_rungs, _movable

    _hot, ret = rail_names(sheet, session.project)
    devices, comps = _movable(sheet, session.catalog)
    runs: list[list[Component]] = []
    for group in _cluster_rungs(comps, devices):
        ordered = sorted(group, key=lambda comp: (comp.x, comp.id))
        run: list[Component] = []
        for comp in reversed(ordered):
            if not _is_rung_output(comp.library_id) or int(comp.rotation or 0) % 180:
                break
            run.insert(0, comp)
        if run and _rail_wired(sheet, run[-1].id, ret):
            runs.append(run)
    return runs


def seat_outputs_at_rail(session: Session, sheet: Sheet, *, gap: float = OUTPUT_RAIL_GAP) -> int:
    """Slide each rung's outputs right so the last one sits *gap* from the return rail.

    Outputs only ever move right, along their own rung; the rung's left part
    stays put, so the wire to them just gets longer. Each rung is kept only
    when sheet_quality is no worse (overlaps and wires through symbols first,
    then crossings). Returns the number of symbols moved.
    """

    from panelcad.core.ladder import rail_xs
    from panelcad.core.power_layout import _body
    from panelcad.core.sheet_quality import sheet_quality
    from panelcad.render.wiring import clear_route_cache

    catalog = session.catalog
    _left, right = rail_xs(sheet)
    target = right - gap
    moved = 0
    quality = None
    for run in _output_runs(session, sheet):
        boxes = [_body(comp, catalog.get(comp.library_id)) for comp in run]
        # Close series gaps first, then shift the packed run to the rail.
        new_x: list[float] = []
        edge = target
        for comp, box in reversed(list(zip(run, boxes))):
            width = box[2] - box[0]
            left_edge = edge - width
            new_x.insert(0, comp.x + (left_edge - box[0]))
            index = run.index(comp)
            if index > 0:
                spacing = box[0] - boxes[index - 1][2]
                edge = left_edge - max(2.0, min(spacing, OUTPUT_SERIES_GAP))
        if all(abs(nx - comp.x) < 0.5 for nx, comp in zip(new_x, run)):
            continue
        inside_gap = boxes[-1][2] > target + 0.5
        if not inside_gap and any(nx < comp.x - 0.01 for nx, comp in zip(new_x, run)):
            continue  # only pull back an output that crowds the rail
        if quality is None:
            quality = sheet_quality(sheet, catalog, session.project)
        saved = [(comp, comp.x) for comp in run]
        points = [(wire, list(wire.points)) for wire in sheet.wires]
        for comp, nx in zip(run, new_x):
            comp.x = nx
        for wire in sheet.wires:
            wire.points = []
        clear_route_cache()
        after = sheet_quality(sheet, catalog, session.project)
        if _seat_ok(after, quality):
            quality = after
            moved += len(run)
        else:
            for comp, x in saved:
                comp.x = x
            for wire, pts in points:
                wire.points = pts
            clear_route_cache()
    return moved


def _seat_ok(after: dict[str, int], before: dict[str, int]) -> bool:
    """Overlaps and wires through symbols first, then crossings: none may rise.

    A rung whose pins already sit on two lines keeps its jog; moving the
    output re-draws that jog, so up to OUTPUT_SEAT_BENDS extra bends are allowed.
    """

    if any(after[key] > before[key] for key in ("overlaps", "through", "crossings")):
        return False
    return after["bends"] <= before["bends"] + OUTPUT_SEAT_BENDS


def _seats_outputs(session: Session, sheet: Sheet) -> bool:
    from panelcad.core.ladder import is_iec_column_sheet, sheet_drawing_style
    from panelcad.core.power_layout import has_rotated_phase_stacks, layout_left_alone

    if sheet.kind != "schematic" or is_io_sheet(sheet) or is_layout_sheet(sheet):
        return False
    # Freeform may be chosen per sheet (SHEET_OWN_STYLES) as well as in the title block.
    style = str(getattr(session.project.title_block, "drawing_style", "") or "")
    if "freeform" in {style, sheet_drawing_style(sheet, session.project)} or is_iec_column_sheet(sheet, session.project):
        return False
    return not layout_left_alone(sheet.id) and not has_rotated_phase_stacks(sheet, session.catalog)


def _keeps_rungs(session: Session, sheet: Sheet) -> bool:
    from panelcad.core.power_layout import count_rung_rows

    return _series_reaches_both_rails(session, sheet) or count_rung_rows(sheet, session.catalog) >= 2


def _reflow_rungs(sheet: Sheet, catalog: Catalog) -> int | None:
    """Reflow built rungs; None (page untouched) when the reflow cannot read the page."""

    from panelcad.core.power_layout import reflow_existing_rungs

    saved = [(comp, comp.x, comp.y) for comp in sheet.components]
    wires = [wire.model_copy(deep=True) for wire in sheet.wires]
    try:
        moved = reflow_existing_rungs(sheet, catalog)
    except Exception:  # noqa: BLE001
        # A symbol with no pins (notes, a title strip) breaks the reflow. Undo
        # any partial move and let the older arrange path handle the page.
        for comp, x, y in saved:
            comp.x, comp.y = x, y
        sheet.wires = wires
        return None
    return moved


def cleanup_sheet(session: Session, sheet: Sheet) -> dict[str, Any]:
    catalog = session.catalog
    scaled = aligned = unstacked = 0
    powered = 0
    if sheet.kind == "terminal":
        return {"sheet": sheet.id, "scaled": 0, "aligned": 0, "unstacked": 0, "clamped": 0}
    if is_generated_io_sheet(sheet):
        pass
    elif is_io_sheet(sheet):
        fit = arrange_io(sheet, catalog)
        scaled = int(fit.get("scaled", 0))
        aligned = int(fit.get("field", 0))
        unstacked = 0
    elif is_layout_sheet(sheet):
        unstacked = unstack(sheet, catalog)
    else:
        demote_power_to_control(sheet, catalog)
        style = str(getattr(session.project.title_block, "drawing_style", "") or "")
        from panelcad.core.power_layout import layout_left_alone, layout_power_sheet, separate_symbol_overlaps

        from panelcad.core.power_layout import power_column_overflow

        powered = 0 if style == "freeform" else layout_power_sheet(sheet, catalog)
        notes = {
            str(key): str(value)
            for key, value in (session.project.properties.get("power_column_overflow") or {}).items()
            if str(value).strip()
        }
        fresh = power_column_overflow()
        if sheet.id in fresh:
            notes[sheet.id] = fresh[sheet.id]
        else:
            notes.pop(sheet.id, None)
        if notes:
            session.project.properties["power_column_overflow"] = notes
        else:
            session.project.properties.pop("power_column_overflow", None)
        if style == "freeform":
            unstacked = unstack(sheet, catalog)
        elif style == "ladder":
            from panelcad.core.ladder import sync_sheet_rails
            from panelcad.core.power_layout import count_rung_rows, reflow_existing_rungs

            sync_sheet_rails(session.project)
            from panelcad.core.power_layout import has_rotated_phase_stacks

            from panelcad.core.power_layout import layout_left_alone

            if powered:
                unstacked = powered + separate_symbol_overlaps(sheet, catalog, rung=False)
            elif layout_left_alone(sheet.id):
                # A rejoining starter was left as drawn. Building a ladder
                # here would add rail wires and loop the power leads.
                unstacked = 0
            elif has_rotated_phase_stacks(sheet, catalog):
                # A sideways starter row is already a power chain. Reflow was
                # turning each phase into a step and shrinking it every Cleanup.
                unstacked = powered
            elif _series_reaches_both_rails(session, sheet):
                # Both rails are already on the series path. arrange_ladder
                # would rebuild it by x and complete_ladder_rungs would short
                # the coil. A sheet that already fits is left alone.
                unstacked = reflow_existing_rungs(sheet, catalog)
            elif count_rung_rows(sheet, catalog) >= 2:
                # Two rows already share a pin line. arrange_ladder would fold
                # them into one coil row and shove the right-hand symbols past L2.
                unstacked = reflow_existing_rungs(sheet, catalog)
            else:
                arrange_ladder(sheet, catalog)
                unstacked = separate_symbol_overlaps(sheet, catalog, rung=True)
                complete_ladder_rungs(session, sheet)
        else:
            if powered:
                unstacked = powered + separate_symbol_overlaps(sheet, catalog, rung=False)
            elif layout_left_alone(sheet.id):
                unstacked = 0
            elif _keeps_rungs(session, sheet) and (reflowed := _reflow_rungs(sheet, catalog)) is not None:
                # The circuit builder already drew rungs. arrange_schematic
                # would string the longest path across one row and pile the
                # rest through it. Pack the rows that are there instead.
                unstacked = reflowed
            else:
                arrange_schematic(sheet, catalog)
                unstacked = unstack(sheet, catalog)
                unstacked += spread(sheet, catalog)
    from panelcad.core.power_layout import layout_left_alone, unfitted_columns

    clamped = clamp_working(sheet, catalog, skip=unfitted_columns(sheet.id))
    spaced = 0
    from panelcad.core.power_layout import has_rotated_phase_stacks

    # Power columns already share phase pins. Spreading that row pulls a
    # breaker off its contactor. Ladder and plain schematic rungs still open up.
    if (
        not layout_left_alone(sheet.id)
        and not powered
        and not has_rotated_phase_stacks(sheet, catalog)
        and sheet.kind in {"schematic", "io"}
        and not is_generated_io_sheet(sheet)
    ):
        from panelcad.core.spacing import apply_spacing_grid

        spaced = apply_spacing_grid(sheet, catalog)
    seated = seat_outputs_at_rail(session, sheet) if not powered and _seats_outputs(session, sheet) else 0
    if not layout_left_alone(sheet.id):
        _clear_routes(sheet)
    from panelcad.core.spacing import page_fullness

    density = page_fullness(sheet, catalog)
    return {
        "sheet": sheet.id,
        "scaled": scaled,
        "aligned": aligned,
        "unstacked": unstacked,
        "clamped": clamped,
        "spaced": spaced,
        "seated": seated,
        "density": density,
    }


def _measured(sheet: Sheet) -> bool:
    return sheet.kind == "schematic" and not is_io_sheet(sheet) and not is_layout_sheet(sheet)


def _restore_project(session: Session, saved: Any) -> None:
    project = session.project
    for name in type(project).model_fields:
        setattr(project, name, getattr(saved, name))


def _split_over_dense(session: Session, sheet_id: str) -> list[str]:
    """A page over the density limit is split before it is tidied.

    Tidying a full page first packed it tighter, hid the over-full flag, and
    put wires through symbols. The continuation pages are then cleaned one
    at a time. Only a page that already has built rungs is split.
    """

    from panelcad.core.spacing import page_fullness

    sheet = session.sheet(sheet_id)
    if not _measured(sheet) or not page_fullness(sheet, session.catalog).get("over_dense"):
        return [sheet_id]
    if not _keeps_rungs(session, sheet):
        # A rough placement is not a built page. Cleanup arranges it and
        # pulls parts back inside the border instead.
        return [sheet_id]
    saved = session.project.model_copy(deep=True)
    try:
        result = session.split_sheet(sheet_id, apply=True)
    except Exception:  # noqa: BLE001
        # A page the splitter cannot handle (no ladder rails to feed, for
        # example) is tidied as one page. Inside Cleanup's undo step a failed
        # split is not rolled back for us, so put the project back here.
        _restore_project(session, saved)
        return [sheet_id]
    pages = [str(page) for page in (result.get("pages") or []) if page]
    if len(pages) > 1:
        from panelcad.core.sheet_quality import CROSSING_ALLOWANCE, PRIMARY, sheet_quality

        before = sheet_quality(saved.sheet(sheet_id), session.catalog, saved)
        after = {key: 0 for key in PRIMARY}
        for page in pages:
            q = sheet_quality(session.sheet(page), session.catalog, session.project)
            for key in PRIMARY:
                after[key] += q[key]
        cleared = after["overlaps"] < before["overlaps"]
        allowance = CROSSING_ALLOWANCE * (len(pages) - 1) if cleared else 0
        if (
            after["overlaps"] > before["overlaps"]
            or after["through"] > before["through"]
            or after["crossings"] > before["crossings"] + allowance
        ):
            # Same rule as a tidy: the split may not add overlaps or wires
            # through symbols, and adds crossings (feed arrows) only within
            # the allowance and only when it clears an overlap. Otherwise
            # keep the page whole.
            _restore_project(session, saved)
            return [sheet_id]
    return pages or [sheet_id]


def open_rung_gaps(sheet: Sheet, catalog: Catalog, *, gap: float = 2.0) -> int:
    """Open the pitch where one rung's symbol sits on the rung above.

    After a split the continuation page keeps the tight pitch of the full
    page, so a Start button's operator lands on the hold-in contact drawn
    under the rung above (PB6 on M2-13). Everything below the clash moves
    down together, so every rung keeps its shape and its devices. Nothing
    moves when the page has no room below.
    """

    if not _measured(sheet):
        return 0
    from panelcad.render.titleblock import working_rect

    _wx, wy, _ww, wh = working_rect(sheet)
    floor = wy + wh - 1.0
    moved = 0
    for _ in range(40):
        boxes = {}
        for comp in sheet.components:
            try:
                boxes[comp.id] = (comp, _aabb(sheet, catalog, comp))
            except KeyError:
                continue
        hit = None
        items = sorted(boxes.values(), key=lambda item: (item[1][1], item[0].id))
        for i, (ca, ra) in enumerate(items):
            for cb, rb in items[i + 1 :]:
                if not _overlap(ra, rb, pad=-0.5):
                    continue
                upper, lower = ((ca, ra), (cb, rb))
                if (ra[1] + ra[3]) > (rb[1] + rb[3]):
                    upper, lower = lower, upper
                dy = upper[1][3] - lower[1][1] + gap
                if dy > 0:
                    hit = (lower[1][1] - 0.01, upper[1][3], upper[0].id, dy)
                    break
            if hit:
                break
        if hit is None:
            break
        cut, wire_cut, keep, dy = hit
        below = [item for item in boxes.values() if item[1][1] > cut and item[0].id != keep]
        lowest = max((item[1][3] for item in below), default=cut)
        if lowest + dy > floor:
            break
        for comp, _rect in below:
            comp.y += dy
        for wire in sheet.wires:
            for point in wire.points:
                if point.y > wire_cut:
                    point.y += dy
            for end in (wire.from_at, wire.to_at):
                if end is not None and end.y > wire_cut:
                    end.y += dy
        moved += len(below)
    return moved


def open_column_gaps(sheet: Sheet, catalog: Catalog, *, gap: float = 2.0) -> int:
    """Open a rung sideways where two symbols on it touch.

    Everything right of the clash moves right together, short of the return
    rail, so the rung keeps its order. Nothing moves when there is no room.
    """

    if not _measured(sheet):
        return 0
    from panelcad.core.ladder import rail_xs

    _left_rail, right_rail = rail_xs(sheet)
    limit = right_rail - 1.0
    moved = 0
    for _ in range(40):
        boxes = {}
        for comp in sheet.components:
            try:
                boxes[comp.id] = (comp, _aabb(sheet, catalog, comp))
            except KeyError:
                continue
        hit = None
        items = sorted(boxes.values(), key=lambda item: (item[1][0], item[0].id))
        for i, (ca, ra) in enumerate(items):
            for cb, rb in items[i + 1 :]:
                if not _overlap(ra, rb, pad=-0.5):
                    continue
                left, right = ((ca, ra), (cb, rb))
                if (ra[0] + ra[2]) > (rb[0] + rb[2]):
                    left, right = right, left
                dx = left[1][2] - right[1][0] + gap
                if dx > 0:
                    hit = (right[1][0] - 0.01, left[1][2], left[0].id, dx)
                    break
            if hit:
                break
        if hit is None:
            break
        cut, wire_cut, keep, dx = hit
        beyond = [item for item in boxes.values() if item[1][0] > cut and item[0].id != keep]
        furthest = max((item[1][2] for item in beyond), default=cut)
        if furthest + dx > limit:
            break
        for comp, _rect in beyond:
            comp.x += dx
        for wire in sheet.wires:
            for point in wire.points:
                if wire_cut < point.x < right_rail - 0.5:
                    point.x += dx
            for end in (wire.from_at, wire.to_at):
                if end is not None and wire_cut < end.x < right_rail - 0.5:
                    end.x += dx
        moved += len(beyond)
    return moved


def nudge_overlaps(session: Session, sheet: Sheet, *, gap: float = 2.0) -> int:
    """Move one symbol of each clashing pair to the nearest free spot.

    The smaller symbol moves up, down, left or right just far enough to
    clear; the side that reads best (fewest overlaps, wires through symbols
    and crossings) wins. Wires follow their pins, so the rest of the page
    does not move.
    """

    from panelcad.core.sheet_quality import sheet_quality
    from panelcad.render.titleblock import working_rect

    catalog = session.catalog
    wx, wy, ww, wh = working_rect(sheet)
    moved = 0
    tried: set[tuple[str, str]] = set()
    for _ in range(20):
        boxes = {}
        for comp in sheet.components:
            try:
                boxes[comp.id] = (comp, _aabb(sheet, catalog, comp))
            except KeyError:
                continue
        pair = None
        items = sorted(boxes.values(), key=lambda item: (item[1][1], item[1][0], item[0].id))
        for i, (ca, ra) in enumerate(items):
            for cb, rb in items[i + 1 :]:
                if _overlap(ra, rb, pad=-0.5) and (ca.id, cb.id) not in tried:
                    pair = ((ca, ra), (cb, rb))
                    break
            if pair:
                break
        if pair is None:
            break
        tried.add((pair[0][0].id, pair[1][0].id))
        area = lambda r: (r[2] - r[0]) * (r[3] - r[1])
        (still, rs), (mover, rm) = sorted(pair, key=lambda item: (area(item[1]), item[0].id), reverse=True)
        here = (mover.x, mover.y)
        base = sheet_quality(sheet, catalog, session.project)
        best = None
        for dx, dy in (
            (0.0, rs[3] - rm[1] + gap),
            (0.0, rs[1] - rm[3] - gap),
            (rs[2] - rm[0] + gap, 0.0),
            (rs[0] - rm[2] - gap, 0.0),
        ):
            box = (rm[0] + dx, rm[1] + dy, rm[2] + dx, rm[3] + dy)
            if box[0] < wx or box[1] < wy or box[2] > wx + ww or box[3] > wy + wh:
                continue
            if any(_overlap(box, r, pad=-0.5) for cid, (_c, r) in boxes.items() if cid != mover.id):
                continue
            mover.x, mover.y = here[0] + dx, here[1] + dy
            q = sheet_quality(sheet, catalog, session.project)
            mover.x, mover.y = here
            if q["overlaps"] < base["overlaps"] and (best is None or _rank(q) < best[0]):
                best = (_rank(q), dx, dy)
        if best is not None:
            mover.x, mover.y = here[0] + best[1], here[1] + best[2]
            moved += 1
    return moved


def _rank(q: dict[str, int]) -> tuple[int, ...]:
    from panelcad.core.sheet_quality import PRIMARY

    return tuple(q[key] for key in PRIMARY) + (q["bends"],)


def _guarded_cleanup(session: Session, sheet_id: str) -> dict[str, Any]:
    """Clean one page and keep it only when no readability number got worse.

    Tidies are tried from the drawing as built: the full Cleanup, and gap
    openers that only move whole rows down or a rung's right-hand part
    right, and a nudge that moves one clashing symbol clear. The best one
    that passes sheet_quality.no_worse wins (the full Cleanup on a tie):
    overlaps and wires through symbols never rise, and crossings rise by at
    most CROSSING_ALLOWANCE only to clear an overlap. When none passes, the
    page stays as built.

    A rough placement with no built rungs is not guarded: there Cleanup is
    what draws the ladder, as before.
    """

    from panelcad.core.sheet_quality import no_worse, sheet_quality
    from panelcad.core.spacing import page_fullness

    sheet = session.sheet(sheet_id)
    if not _measured(sheet) or not _keeps_rungs(session, sheet):
        return cleanup_sheet(session, sheet)
    before = sheet_quality(sheet, session.catalog, session.project)
    idle = {"sheet": sheet_id, "scaled": 0, "aligned": 0, "unstacked": 0, "clamped": 0, "spaced": 0}
    saved = session.project.model_copy(deep=True)
    best: tuple[tuple[int, ...], Any, dict[str, Any], dict[str, int]] | None = None
    overflow: dict[str, Any] | None = None
    for name in ("cleanup", "rung_gaps", "column_gaps", "both_gaps", "nudge"):
        _restore_project(session, saved.model_copy(deep=True))
        page = session.sheet(sheet_id)
        try:
            if name == "cleanup":
                result = cleanup_sheet(session, page)
                overflow = dict(session.project.properties.get("power_column_overflow") or {})
            else:
                moved = 0
                if name in ("rung_gaps", "both_gaps"):
                    moved += open_rung_gaps(page, session.catalog)
                if name in ("column_gaps", "both_gaps"):
                    moved += open_column_gaps(page, session.catalog)
                if name == "nudge":
                    moved += nudge_overlaps(session, page)
                seated = seat_outputs_at_rail(session, page) if _seats_outputs(session, page) else 0
                result = {**idle, "unstacked": moved, "seated": seated}
        except Exception:  # noqa: BLE001
            # A page this tidy cannot read (a symbol with no pins, a rung the
            # reflow does not recognise) just loses that candidate.
            continue
        after = sheet_quality(session.sheet(sheet_id), session.catalog, session.project)
        seated = int(result.get("seated") or 0)
        relaxed = {**before, "bends": before["bends"] + OUTPUT_SEAT_BENDS * seated}
        if (no_worse(after, before) or (seated and no_worse(after, relaxed))) and (
            best is None or _rank(after) < best[0]
        ):
            best = (_rank(after), session.project.model_copy(deep=True), {**result, "method": name}, after)
    def _keep_overflow_note() -> None:
        # Cleanup records branches that do not fit the power columns. That
        # note is about the page, not about which tidy won, so keep it.
        if overflow is None:
            return
        notes = dict(session.project.properties.get("power_column_overflow") or {})
        if sheet_id in overflow:
            notes[sheet_id] = overflow[sheet_id]
        else:
            notes.pop(sheet_id, None)
        if notes:
            session.project.properties["power_column_overflow"] = notes
        else:
            session.project.properties.pop("power_column_overflow", None)

    if best is None:
        # Every tidy made the page harder to read. Seating the outputs alone
        # is still offered: it is guarded rung by rung against the page as built.
        _restore_project(session, saved.model_copy(deep=True))
        page = session.sheet(sheet_id)
        seated = seat_outputs_at_rail(session, page) if _seats_outputs(session, page) else 0
        if seated:
            # Each rung was already checked against the page as built.
            after = sheet_quality(page, session.catalog, session.project)
            result = {**idle, "seated": seated, "method": "seat_outputs"}
            best = (_rank(after), session.project.model_copy(deep=True), result, after)
    if best is not None:
        _restore_project(session, best[1])
        _keep_overflow_note()
        result = best[2]
        result.update({"before": before, "after": best[3], "kept": True})
        result.setdefault("density", page_fullness(session.sheet(sheet_id), session.catalog))
        return result
    # Every tidy made this page harder to read. The drawing as built wins.
    _restore_project(session, saved)
    _keep_overflow_note()
    return {**idle, "before": before, "after": before, "kept": False,
            "density": page_fullness(session.sheet(sheet_id), session.catalog)}


def cleanup_project(session: Session, sheet_id: str | None = None) -> dict[str, Any]:
    wanted = [session.sheet(sheet_id).id] if sheet_id else [sh.id for sh in session.project.sheets]
    order: list[str] = []
    split: list[dict[str, Any]] = []
    for sid in wanted:
        pages = _split_over_dense(session, sid)
        if len(pages) > 1:
            split.append({"sheet": sid, "pages": pages})
        order.extend(page for page in pages if page not in order)
    results = [_guarded_cleanup(session, sid) for sid in order]
    out: dict[str, Any] = {"sheets": results, "count": len(results)}
    if split:
        out["split"] = split
    return out
