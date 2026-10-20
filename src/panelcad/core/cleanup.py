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
    # The nearest device is the run this symbol belongs to. A long tap to
    # another branch must not flip a vertical fuse flat, or the other way.
    nearest = min(range(len(xs)), key=lambda index: (xs[index] * xs[index] + ys[index] * ys[index], index))
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
    if not _path_reaches_rail(sheet, path, hot, hot, ret):
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


def cleanup_sheet(session: Session, sheet: Sheet) -> dict[str, Any]:
    catalog = session.catalog
    scaled = aligned = unstacked = 0
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
            else:
                arrange_schematic(sheet, catalog)
                unstacked = unstack(sheet, catalog)
                unstacked += spread(sheet, catalog)
    from panelcad.core.power_layout import layout_left_alone, unfitted_columns

    clamped = clamp_working(sheet, catalog, skip=unfitted_columns(sheet.id))
    if not layout_left_alone(sheet.id):
        _clear_routes(sheet)
    return {
        "sheet": sheet.id,
        "scaled": scaled,
        "aligned": aligned,
        "unstacked": unstacked,
        "clamped": clamped,
    }


def cleanup_project(session: Session, sheet_id: str | None = None) -> dict[str, Any]:
    if sheet_id:
        sheets = [session.sheet(sheet_id)]
    else:
        sheets = list(session.project.sheets)
    results = [cleanup_sheet(session, sh) for sh in sheets]
    return {"sheets": results, "count": len(results)}
