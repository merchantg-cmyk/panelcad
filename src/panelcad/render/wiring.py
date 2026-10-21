"""Sheet-aware wire endpoints and obstacle-avoiding routes."""

from __future__ import annotations

from panelcad.core.geometry import Point, Side
from panelcad.core.layout import is_generated_io_sheet, is_io_sheet, is_layout_sheet, is_network_sheet
from panelcad.core.models import Component, Device, Project, Sheet, Wire
from panelcad.core.route import (
    LANE_GAP,
    Rect,
    component_aabb,
    present_route,
    expand_rect,
    path_as_obstacles,
    path_hits_obstacles,
    needs_separation,
    pull_path,
    route_orthogonal,
    runs_along_lanes,
)
from panelcad.library.catalog import Catalog
from panelcad.memory import CountLRU, sheet_cache_limit
from panelcad.render.sheet import parse_pin_ref, pin_side_world, pin_world, resolve_pin
from panelcad.render.titleblock import title_block_rect, working_rect


def _symbol_size(sheet: Sheet, catalog: Catalog, comp: Component, device: Device) -> tuple[float, float]:
    """Unscaled local symbol size (then multiplied by visual_scale)."""
    if comp.properties.get("ladder_card"):
        return (
            float(comp.properties.get("card_w") or device.schematic_width),
            float(comp.properties.get("card_h") or device.schematic_height),
        )
    if is_layout_sheet(sheet):
        return (max(8.0, device.panel.width_mm), max(8.0, device.panel.height_mm))
    if is_io_sheet(sheet) and not is_generated_io_sheet(sheet):
        from panelcad.render.io_ladder import io_card_size, is_io_module

        if is_io_module(device):
            return io_card_size(device, None)
    if (device.category or "").lower().startswith("connection.notes"):
        from panelcad.render.painter import notes_box

        return notes_box(comp, 1.0)
    if is_network_sheet(sheet):
        from panelcad.library.ports import effective_ports
        from panelcad.render.network import node_size

        return node_size(len(effective_ports(device, comp)))
    return (device.schematic_width, device.schematic_height)


def _supply_interiors(sheet: Sheet, catalog: Catalog) -> list[Rect]:
    """Supply and transformer bodies a straight rung must not cross.

    Phase lanes and contact-to-contact rungs stay on the ladder route. A
    supply drop that would run through the PSU is sent around the body.
    """

    boxes: list[Rect] = []
    for comp in sheet.components:
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        if not _is_supply_category(device.category):
            continue
        boxes.append(_interior_box(component_obstacle(sheet, catalog, comp, pad=0.0)))
    return boxes


def _is_supply_category(category: str) -> bool:
    """Power supply and control transformer, by library category, not by id text."""

    cat = str(category or "").lower()
    return cat.startswith("power.supply") or cat.startswith("power.transformer")


def _interior_box(rect: Rect, inset: float = 1.4) -> Rect:
    """Shrink a symbol so a wire on the pin edge is not 'through' the body."""

    x0, y0, x1, y1 = rect
    if x1 - x0 <= inset * 2.2 or y1 - y0 <= inset * 2.2:
        return rect
    return (x0 + inset, y0 + inset, x1 - inset, y1 - inset)


def component_obstacle(sheet: Sheet, catalog: Catalog, comp: Component, *, pad: float = 0.8) -> Rect:
    device = catalog.get(comp.library_id)
    w, h = _symbol_size(sheet, catalog, comp, device)
    s = comp.visual_scale()
    rot = 0 if is_layout_sheet(sheet) else comp.rotation
    box = component_aabb(comp.x, comp.y, w * s, h * s, rot, comp.mirror)
    return expand_rect(box, pad)


def sheet_obstacles(sheet: Sheet, catalog: Catalog, skip: set[str] | None = None) -> list[Rect]:
    ignore = skip or set()
    rects: list[Rect] = []
    for comp in sheet.components:
        if comp.id in ignore:
            continue
        device = catalog.get(comp.library_id)
        if (device.category or "").lower().startswith("connection.notes"):
            continue
        rects.append(component_obstacle(sheet, catalog, comp))
    tx, ty, tw, th = title_block_rect(sheet)
    rects.append((tx, ty, tx + tw, ty + th))
    return rects


def _side_from_port(sheet: Sheet, catalog: Catalog, ref: str) -> Side | None:
    cid, pid = parse_pin_ref(ref)
    comp = sheet.component(cid)
    device = catalog.get(comp.library_id)
    if is_network_sheet(sheet):
        from panelcad.library.ports import effective_ports

        for p in effective_ports(device, comp):
            if p.id == pid:
                return p.side  # type: ignore[return-value]
    if is_io_sheet(sheet) and not is_generated_io_sheet(sheet):
        from panelcad.render.io_ladder import io_pin_layout, is_io_module

        if is_io_module(device):
            laid = io_pin_layout(device)
            lx, _ly = laid.get(pid, (0.0, 0.0))
            return "left" if lx < 1.0 else "right"
        try:
            return pin_side_world(comp, resolve_pin(device, comp, pid))  # type: ignore[return-value]
        except KeyError:
            return None
    try:
        return pin_side_world(comp, resolve_pin(device, comp, pid))  # type: ignore[return-value]
    except KeyError:
        return None


def _rail_end(
    sheet: Sheet,
    ref: str,
    along: float,
    hot: str,
    ret: str,
    stored,
    *,
    iec: bool,
) -> tuple[float, float, Side]:
    """Rail landing. A stored click overrides the free axis (Y on a ladder, X on IEC)."""

    from panelcad.core.ladder import iec_rail_point, rail_point

    if iec:
        x, y, side = iec_rail_point(sheet, ref, along, hot, ret)
        if stored is not None:
            x = float(stored.x)
        return x, y, side
    x, y, side = rail_point(sheet, ref, along, hot, ret)
    if stored is not None:
        y = float(stored.y)
    return x, y, side


def wire_ends(
    sheet: Sheet,
    catalog: Catalog,
    wire: Wire,
    project: Project | None = None,
) -> tuple[float, float, Side, float, float, Side]:
    from panelcad.core.ladder import endpoint_is_rail, is_iec_column_sheet, rail_names

    hot, ret = rail_names(sheet, project)
    a_rail = endpoint_is_rail(sheet, wire.frm, project)
    b_rail = endpoint_is_rail(sheet, wire.to, project)
    if (a_rail or b_rail) and is_iec_column_sheet(sheet, project):
        if a_rail and b_rail:
            ax, ay, a_side = _rail_end(sheet, wire.frm, 40.0, hot, ret, wire.from_at, iec=True)
            bx, by, b_side = _rail_end(sheet, wire.to, 80.0, hot, ret, wire.to_at, iec=True)
            return ax, ay, a_side, bx, by, b_side
        device_ref = wire.to if a_rail else wire.frm
        cid, pid = parse_pin_ref(device_ref)
        comp = sheet.component(cid)
        dev = catalog.get(comp.library_id)
        px, py = pin_world(comp, resolve_pin(dev, comp, pid))
        side = _side_from_port(sheet, catalog, device_ref) or "top"
        rail_x = px
        if wire.points:
            # A stored riser lands the rail on its own X so plus and minus
            # are not the same vertical (that shorted L+ to 0V).
            rail_x = wire.points[0].x if a_rail else wire.points[-1].x
        if a_rail:
            ax, ay, a_side = _rail_end(sheet, wire.frm, rail_x, hot, ret, wire.from_at, iec=True)
            return ax, ay, a_side, px, py, side
        bx, by, b_side = _rail_end(sheet, wire.to, rail_x, hot, ret, wire.to_at, iec=True)
        return px, py, side, bx, by, b_side
    if a_rail or b_rail:
        if a_rail and b_rail:
            ax, ay, a_side = _rail_end(sheet, wire.frm, 40.0, hot, ret, wire.from_at, iec=False)
            bx, by, b_side = _rail_end(sheet, wire.to, 40.0, hot, ret, wire.to_at, iec=False)
            return ax, ay, a_side, bx, by, b_side
        device_ref = wire.to if a_rail else wire.frm
        cid, pid = parse_pin_ref(device_ref)
        comp = sheet.component(cid)
        dev = catalog.get(comp.library_id)
        px, py = pin_world(comp, resolve_pin(dev, comp, pid))
        side = _side_from_port(sheet, catalog, device_ref) or "left"
        if a_rail:
            ax, ay, a_side = _rail_end(sheet, wire.frm, py, hot, ret, wire.from_at, iec=False)
            return ax, ay, a_side, px, py, side
        bx, by, b_side = _rail_end(sheet, wire.to, py, hot, ret, wire.to_at, iec=False)
        return px, py, side, bx, by, b_side
    try:
        cid_a, _ = parse_pin_ref(wire.frm)
        cid_b, _ = parse_pin_ref(wire.to)
    except ValueError:
        # A saved end that is not a pin and not a rail name still has to draw.
        y = 40.0
        ax, ay, a_side = _rail_end(sheet, wire.frm, y, hot, ret, wire.from_at, iec=False)
        bx, by, b_side = _rail_end(sheet, wire.to, y, hot, ret, wire.to_at, iec=False)
        return ax, ay, a_side, bx, by, b_side
    if is_network_sheet(sheet):
        from panelcad.render.network import network_endpoint

        ax, ay = network_endpoint(sheet, catalog, wire.frm)
        bx, by = network_endpoint(sheet, catalog, wire.to)
    elif is_io_sheet(sheet) and not is_generated_io_sheet(sheet):
        from panelcad.render.io_ladder import io_endpoint

        ax, ay = io_endpoint(sheet, catalog, wire.frm)
        bx, by = io_endpoint(sheet, catalog, wire.to)
    else:
        a_comp = sheet.component(cid_a)
        b_comp = sheet.component(cid_b)
        a_dev = catalog.get(a_comp.library_id)
        b_dev = catalog.get(b_comp.library_id)
        a_pin = resolve_pin(a_dev, a_comp, parse_pin_ref(wire.frm)[1])
        b_pin = resolve_pin(b_dev, b_comp, parse_pin_ref(wire.to)[1])
        ax, ay = pin_world(a_comp, a_pin)
        bx, by = pin_world(b_comp, b_pin)
    a_side = _side_from_port(sheet, catalog, wire.frm) or ("left" if ax <= bx else "right")
    b_side = _side_from_port(sheet, catalog, wire.to) or ("right" if ax <= bx else "left")
    return ax, ay, a_side, bx, by, b_side


# One lane per phase beside a feeder. Closer than this and L1/L2/L3 read as one wire.
_PHASE_LANE = 4.0
# A smaller gap is a pin-pitch jog inside one column, not a hop to another branch.
_PHASE_HOP = 16.0


def _beside_phase_route(
    sheet: Sheet,
    catalog: Catalog,
    wire: Wire,
    sx: float,
    sy: float,
    dx: float,
    dy: float,
    index: int,
    forward: bool,
) -> list[Point] | None:
    """One lane per phase across the gap when the load is beside the source."""

    lane_y = sy + _PHASE_LANE * (index + 1)
    gutter = _gutter_x(sheet, catalog, wire, sx, dx, index)
    above = dy - 2.0 - index * _PHASE_LANE
    raw = [(sx, sy), (sx, lane_y), (gutter, lane_y), (gutter, above), (dx, above), (dx, dy)]
    if not forward:
        raw = list(reversed(raw))
    points = [Point(x=raw[0][0], y=raw[0][1])]
    for x, y in raw[1:]:
        if abs(x - points[-1].x) < 0.05 and abs(y - points[-1].y) < 0.05:
            continue
        points.append(Point(x=x, y=y))
    if _phase_path_blocked(sheet, catalog, wire, points):
        return None
    return points


def _phase_link_route(
    sheet: Sheet,
    catalog: Catalog,
    wire: Wire,
    ax: float,
    ay: float,
    a_side: str,
    bx: float,
    by: float,
    b_side: str,
) -> list[Point] | None:
    """Route a branch hop so the phases do not share one line or climb back over the feeder.

    A motor lead a couple of millimetres off the overload stays on the normal
    router. A feeder that supplies another column drops each phase into its own
    lane under the load pins, rises in the gutter beside the feeder, and enters
    the branch from above. If that lane would cross some other device, the
    normal router draws the hop instead.
    """

    ends = _phase_ends(sheet, catalog, wire)
    if ends is None:
        return None
    a_index, b_index = ends
    if abs(ax - bx) < _PHASE_HOP:
        return None
    if a_side == "bottom" and b_side == "top":
        sx, sy, dx, dy, index, forward = ax, ay, bx, by, a_index, True
    elif b_side == "bottom" and a_side == "top":
        sx, sy, dx, dy, index, forward = bx, by, ax, ay, b_index, False
    else:
        return None
    # The downward gutter is a hop to the next column. A device beside the
    # load pin keeps each phase on its own lane in the gap, so the leads do
    # not share one vertical along the symbol.
    if dy < sy + 8.0:
        return _beside_phase_route(sheet, catalog, wire, sx, sy, dx, dy, index, forward)
    # Leave the load pin downward, cross in that phase's lane, rise in the
    # gutter beside the feeder (not up its phases), then enter the branch
    # from above its line pin. Each phase keeps its own x and its own y.
    lane = sy + _PHASE_LANE + index * _PHASE_LANE
    gutter = _gutter_x(sheet, catalog, wire, sx, dx, index)
    above = dy - 2.0 - (2 - min(index, 2)) * _PHASE_LANE
    if above >= dy - 1.0:
        above = dy - 2.0 - index * _PHASE_LANE
    raw = [(sx, sy), (sx, lane), (gutter, lane), (gutter, above), (dx, above), (dx, dy)]
    if not forward:
        raw = list(reversed(raw))
    points = [Point(x=raw[0][0], y=raw[0][1])]
    for x, y in raw[1:]:
        if abs(x - points[-1].x) < 0.05 and abs(y - points[-1].y) < 0.05:
            continue
        points.append(Point(x=x, y=y))
    if _phase_path_blocked(sheet, catalog, wire, points):
        return None
    return points


def _rotated_bus_route(
    sheet: Sheet,
    catalog: Catalog,
    wire: Wire,
    ax: float,
    ay: float,
    a_side: str,
    bx: float,
    by: float,
    b_side: str,
) -> list[Point] | None:
    """Give each phase of a rotated starter bus its own vertical lane.

    Several sideways contactors tapping one off-page column used to elbow on
    the same x, so L1, L2 and L3 read as one conductor.
    """

    del a_side, b_side
    a_index = _rotated_phase_index(sheet, catalog, wire.frm)
    b_index = _rotated_phase_index(sheet, catalog, wire.to)
    if a_index is None and b_index is None:
        return None
    a_off = _is_offpage_end(sheet, wire.frm)
    b_off = _is_offpage_end(sheet, wire.to)
    if a_index is not None and (b_index is None or b_off):
        sx, sy, dx, dy, index, forward = ax, ay, bx, by, a_index, True
    elif b_index is not None and (a_index is None or a_off):
        sx, sy, dx, dy, index, forward = bx, by, ax, ay, b_index, False
    elif a_index is not None and b_index is not None:
        if ax <= bx:
            sx, sy, dx, dy, index, forward = ax, ay, bx, by, a_index, True
        else:
            sx, sy, dx, dy, index, forward = bx, by, ax, ay, b_index, False
    else:
        return None
    # A lead along one starter row is already on its own y. Only the drop
    # from row to row, or out to the off-page column, needs a lane.
    if abs(sy - dy) < 8.0 or abs(sx - dx) < 8.0:
        return None
    # The marker may sit on either side of the starter. Try the gap beside
    # its body first, then the other side, each phase 4 mm from the next.
    # Falling back to one shared elbow is what tied 101, 103 and 105 together.
    bus_left, bus_right = _body_x_span(sheet, catalog, wire, dx)
    toward = 1.0 if sx >= dx else -1.0
    lanes: list[float] = []
    for direction in (toward, -toward):
        edge = bus_right if direction > 0 else bus_left
        for step in range(0, 5):
            lanes.append(edge + direction * _PHASE_LANE * (index + 1 + step))
    seen: set[int] = set()
    ordered: list[float] = []
    for lane_x in lanes:
        key = round(lane_x * 10)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(lane_x)
    fallback: list[Point] | None = None
    for lane_x in ordered:
        raw = [(sx, sy), (lane_x, sy), (lane_x, dy), (dx, dy)]
        if not forward:
            raw = list(reversed(raw))
        points = [Point(x=raw[0][0], y=raw[0][1])]
        for x, y in raw[1:]:
            if abs(x - points[-1].x) < 0.05 and abs(y - points[-1].y) < 0.05:
                continue
            points.append(Point(x=x, y=y))
        if fallback is None:
            fallback = points
        if not _phase_path_blocked(sheet, catalog, wire, points):
            return points
    return fallback


def _body_x_span(sheet: Sheet, catalog: Catalog, wire: Wire, pin_x: float) -> tuple[float, float]:
    """Left and right of the symbol the bus pin belongs to."""

    from panelcad.core.route import component_aabb

    for ref in (wire.frm, wire.to):
        try:
            comp_id, _pin = parse_pin_ref(ref)
            comp = sheet.component(comp_id)
            device = catalog.get(comp.library_id)
        except (ValueError, KeyError):
            continue
        scale = comp.visual_scale()
        box = component_aabb(
            comp.x,
            comp.y,
            float(device.schematic_width) * scale,
            float(device.schematic_height) * scale,
            int(comp.rotation or 0),
            bool(comp.mirror),
        )
        if box[0] - 2.0 <= pin_x <= box[2] + 2.0:
            return box[0], box[2]
    return pin_x, pin_x


def _rotated_phase_index(sheet: Sheet, catalog: Catalog, ref: str) -> int | None:
    try:
        comp_id, pin_id = parse_pin_ref(ref)
        comp = sheet.component(comp_id)
        device = catalog.get(comp.library_id)
    except (ValueError, KeyError):
        return None
    if int(comp.rotation or 0) % 180 == 0:
        return None
    tops = [pin for pin in device.pins if pin.side == "top"]
    bots = [pin for pin in device.pins if pin.side == "bottom"]
    if not (len(tops) >= 2 and len(bots) >= 2):
        blob = f"{device.category} {comp.library_id}".lower()
        if not (len(tops) >= 2 and "motor" in blob):
            return None
    return _phase_index(device, pin_id)


def _is_offpage_end(sheet: Sheet, ref: str) -> bool:
    try:
        comp_id, _pin = parse_pin_ref(ref)
        comp = sheet.component(comp_id)
    except (ValueError, KeyError):
        return False
    return "offpage" in comp.library_id


def _phase_path_blocked(
    sheet: Sheet,
    catalog: Catalog,
    wire: Wire,
    points: list[Point],
) -> bool:
    """True when the lane crosses a device that is not one of the wire's ends."""

    from panelcad.core.route import path_hits_obstacles

    try:
        skip = {parse_pin_ref(wire.frm)[0], parse_pin_ref(wire.to)[0]}
    except ValueError:
        skip = set()
    return path_hits_obstacles(points, sheet_obstacles(sheet, catalog, skip=skip), skip_ends=False)


def _gutter_x(sheet: Sheet, catalog: Catalog, wire: Wire, sx: float, dx: float, index: int) -> float:
    """A riser just past the feeder body, one lane per phase, clear of the branch."""

    from panelcad.render.sheet import parse_pin_ref

    left_id, right_id = (wire.frm, wire.to) if sx <= dx else (wire.to, wire.frm)
    try:
        src = sheet.component(parse_pin_ref(left_id)[0])
        dst = sheet.component(parse_pin_ref(right_id)[0])
        src_dev = catalog.get(src.library_id)
        src_right = src.x + float(src_dev.schematic_width) * src.visual_scale()
        dst_left = dst.x
    except (ValueError, KeyError):
        src_right = min(sx, dx)
        dst_left = max(sx, dx)
    lane_x = src_right + _PHASE_LANE + index * _PHASE_LANE
    if lane_x >= dst_left - 2.0:
        span = max(_PHASE_LANE, dst_left - src_right)
        lane_x = src_right + span * (index + 1) / 4.0
    return lane_x


def _phase_ends(sheet: Sheet, catalog: Catalog, wire: Wire) -> tuple[int, int] | None:
    from panelcad.render.sheet import parse_pin_ref

    try:
        a_ref, a_pin = parse_pin_ref(wire.frm)
        b_ref, b_pin = parse_pin_ref(wire.to)
        a_comp = sheet.component(a_ref)
        b_comp = sheet.component(b_ref)
        a_dev = catalog.get(a_comp.library_id)
        b_dev = catalog.get(b_comp.library_id)
    except (ValueError, KeyError):
        return None
    if not _upright_phase_device(a_comp, a_dev) or not _upright_phase_device(b_comp, b_dev):
        return None
    a_index = _phase_index(a_dev, a_pin)
    b_index = _phase_index(b_dev, b_pin)
    if a_index is None or b_index is None:
        return None
    return a_index, b_index


def _upright_phase_device(comp: Component, device: Device) -> bool:
    if int(comp.rotation or 0) % 180 != 0:
        return False
    tops = [pin for pin in device.pins if pin.side == "top"]
    bots = [pin for pin in device.pins if pin.side == "bottom"]
    if len(tops) >= 2 and len(bots) >= 2:
        return True
    blob = f"{device.category} {comp.library_id}".lower()
    return len(tops) >= 2 and "motor" in blob


def _phase_index(device: Device, pin_id: str) -> int | None:
    for side in ("bottom", "top"):
        pins = sorted((pin for pin in device.pins if pin.side == side), key=lambda pin: pin.x)
        for index, pin in enumerate(pins):
            if pin.id == pin_id:
                return index
    return None


def _orthogonal(points: list[Point], tol: float = 0.6) -> bool:
    return all(
        abs(a.x - b.x) <= tol or abs(a.y - b.y) <= tol for a, b in zip(points, points[1:])
    )


def route_wire(
    sheet: Sheet,
    catalog: Catalog,
    wire: Wire,
    extra_obstacles: list[Rect] | None = None,
    project: Project | None = None,
    lanes: list[Rect] | None = None,
) -> list[Point]:
    ax, ay, a_side, bx, by, b_side = wire_ends(sheet, catalog, wire, project)
    # A stored diagonal (the old midpoint fallback) is not a route.
    if wire.points:
        stored = [Point(x=ax, y=ay), *list(wire.points), Point(x=bx, y=by)]
        if _orthogonal(stored):
            return stored
    phased = _phase_link_route(sheet, catalog, wire, ax, ay, a_side, bx, by, b_side)
    if phased is not None and not _crosses_foreign(phased, lanes):
        return present_route(phased)
    bused = _rotated_bus_route(sheet, catalog, wire, ax, ay, a_side, bx, by, b_side)
    if bused is not None and not _crosses_foreign(bused, lanes):
        return present_route(bused)
    if is_io_sheet(sheet) and not is_generated_io_sheet(sheet):
        from panelcad.core.geometry import manhattan_route

        return manhattan_route(ax, ay, a_side, bx, by, b_side, stub=2.0)
    from panelcad.core.ladder import is_iec_column_sheet, is_ladder_sheet, ladder_route

    if is_ladder_sheet(sheet, project) or is_generated_io_sheet(sheet) or is_iec_column_sheet(sheet, project):
        plain = ladder_route(ax, ay, bx, by)
        # A supply or transformer sitting on a rung must not have the conductor
        # through its body. Phase lanes stay on the straight ladder route.
        blocks = _supply_interiors(sheet, catalog)
        if blocks and path_hits_obstacles(plain, blocks, skip_ends=True):
            wx, wy, ww, wh = working_rect(sheet)
            bounds = (wx, wy + 4.0, wx + ww, wy + wh - 2.0)
            walls = sheet_obstacles(sheet, catalog, skip=set())
            return present_route(
                route_orthogonal(ax, ay, a_side, bx, by, b_side, walls, bounds=bounds)
            )
        return present_route(plain)
    # Every symbol is an obstacle, including the two this wire lands on.
    # Stubs out of the pin are ignored (path_hits skip_ends); the body is not.
    obstacles = sheet_obstacles(sheet, catalog, skip=set())
    if extra_obstacles:
        obstacles = [*obstacles, *extra_obstacles]
    wx, wy, ww, wh = working_rect(sheet)
    bounds = (wx, wy + 4.0, wx + ww, wy + wh - 2.0)
    # Keep a route that already clears other conductors. Only a wire that sits
    # on one of them is sent around that lane.
    plain = route_orthogonal(ax, ay, a_side, bx, by, b_side, obstacles, bounds=bounds)
    if lanes and runs_along_lanes(plain, lanes):
        return route_orthogonal(
            ax, ay, a_side, bx, by, b_side, obstacles, bounds=bounds, lanes=lanes
        )
    return plain


_ROUTE_CACHE: CountLRU[tuple, dict[str, list[Point]]] = CountLRU(sheet_cache_limit)
_IN_ROUTE = False


def clear_route_cache() -> None:
    global _IN_ROUTE
    _ROUTE_CACHE.clear()
    _IN_ROUTE = False


def _route_key(sheet: Sheet, project: Project | None, catalog: Catalog | None) -> tuple:
    """Same facts as the label cache, so a moved pin cannot reuse the previous trace."""

    if project is None or catalog is None:
        return (
            "bare",
            id(sheet),
            tuple((comp.id, round(comp.x, 3), round(comp.y, 3), comp.rotation) for comp in sheet.components),
            tuple((wire.id, wire.frm, wire.to, len(wire.points or [])) for wire in sheet.wires),
        )
    from panelcad.render.label_avoid import drawing_key

    return ("route", drawing_key(project, catalog, sheet, ladder=False, sheet_no=0))


# Pin dots are drawn at 0.7 mm radius with a 0.45 mm wire stroke. A pulled run
# that sits on the symbol pad (0.8 mm) lands inside that dot. Keep the trace
# center at least this far from every pin so it does not read as a short.
PIN_KEEP_MM = 2.0


def pin_keepouts(sheet: Sheet, catalog: Catalog) -> list[Rect]:
    """Small boxes around connection dots. Pull must not drag a trace through them."""

    rects: list[Rect] = []
    for comp in sheet.components:
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        for pin in device.pins:
            wx, wy = pin_world(comp, pin)
            rects.append(
                (wx - PIN_KEEP_MM, wy - PIN_KEEP_MM, wx + PIN_KEEP_MM, wy + PIN_KEEP_MM)
            )
    return rects


def _straight_routes(
    sheet: Sheet, catalog: Catalog, project: Project | None = None
) -> dict[str, list[Point]]:
    prior: list[list[Point]] = []
    prior_keys: list[str] = []
    out: dict[str, list[Point]] = {}
    from panelcad.core.ladder import is_iec_column_sheet, is_ladder_sheet

    skip_pull = (
        is_io_sheet(sheet)
        or is_layout_sheet(sheet)
        or is_ladder_sheet(sheet)
        or is_iec_column_sheet(sheet)
    )
    symbols = [] if skip_pull else sheet_obstacles(sheet, catalog, skip=set())
    walls = [] if skip_pull else [*symbols, *pin_keepouts(sheet, catalog)]
    for wire in sheet.wires:
        key = _conductor_key(wire)
        foreign = [
            path
            for path, prior_key in zip(prior, prior_keys)
            if prior_key != key
        ]
        pts = route_wire(sheet, catalog, wire, project=project)
        manual = False
        if wire.points and len(pts) >= 2 and _orthogonal(pts):
            manual = True
        if not skip_pull and prior and not manual and needs_separation(pts, foreign):
            pulled = pull_path(pts, prior, walls, keys=prior_keys, own=key)
            # Moving off a shared lane through a symbol is worse than the share.
            if _cuts_symbol(pulled, symbols) and not _cuts_symbol(pts, symbols):
                pulled = pts
            if needs_separation(pulled, foreign):
                # Detours only, so every earlier conductor can be a lane without
                # a grid search. A path that still sits on one is refused below.
                corridors = _lane_corridors(foreign)
                ax, ay, a_side, bx, by, b_side = wire_ends(sheet, catalog, wire, project)
                wx, wy, ww, wh = working_rect(sheet)
                repaired = route_orthogonal(
                    ax,
                    ay,
                    a_side,
                    bx,
                    by,
                    b_side,
                    symbols,
                    bounds=(wx, wy + 4.0, wx + ww, wy + wh - 2.0),
                    lanes=corridors,
                    allow_astar=False,
                )
                if not _cuts_symbol(repaired, symbols) and not needs_separation(repaired, foreign):
                    pts = repaired
                else:
                    pts = pulled
            else:
                pts = pulled
        out[wire.id] = pts
        prior.append(pts)
        prior_keys.append(key)
    return out


def _cuts_symbol(points: list[Point], walls: list[Rect]) -> bool:
    """True when the stroke runs through a symbol. A short pin stub does not count."""

    from panelcad.core.route import path_hits_obstacles

    return path_hits_obstacles(points, walls, skip_ends=True)


def _lane_corridors(paths: list[list[Point]]) -> list[Rect]:
    """Keep the next centerline a lane away from wires that are already drawn."""

    rects: list[Rect] = []
    for path in paths:
        rects.extend(path_as_obstacles(path, half=LANE_GAP, skip_stubs=True))
    return rects


def _crosses_foreign(points: list[Point], corridors: list[Rect] | None) -> bool:
    if not corridors:
        return False
    from panelcad.core.route import runs_along_lanes

    return runs_along_lanes(points, corridors)


def _conductor_key(wire: Wire) -> str:
    """Same number is one conductor. A blank number falls back to the net name."""

    number = str(wire.number or "").strip()
    if number:
        return number
    net = str(wire.net or wire.label or "").strip()
    return net or str(wire.id)


def _rects_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    pad: float = 0.0,
) -> bool:
    return not (
        a[2] + pad <= b[0] or b[2] + pad <= a[0] or a[3] + pad <= b[1] or b[3] + pad <= a[1]
    )


def _collapse_points(points: list[Point]) -> list[Point]:
    if not points:
        return points
    out = [points[0]]
    for point in points[1:]:
        prev = out[-1]
        if abs(point.x - prev.x) < 0.05 and abs(point.y - prev.y) < 0.05:
            continue
        out.append(point)
    return out


def _mirror_bands(
    x: float,
    y0: float,
    y1: float,
    mirrors: list[tuple[float, float, float, float]],
) -> list[list[tuple[float, float, float, float]]]:
    """Contact tables this vertical run actually crosses, merged when they stack."""

    lo, hi = (y0, y1) if y0 <= y1 else (y1, y0)
    hits = [
        box
        for box in mirrors
        if box[0] - 0.2 <= x <= box[2] + 0.2 and hi > box[1] + 0.05 and lo < box[3] - 0.05
    ]
    hits.sort(key=lambda box: box[1])
    bands: list[list[tuple[float, float, float, float]]] = []
    for box in hits:
        if bands and box[1] <= max(item[3] for item in bands[-1]) + 2.0:
            bands[-1].append(box)
        else:
            bands.append([box])
    return bands


def _detour_clear(
    x: float,
    side: float,
    y_out: float,
    y_back: float,
    obstacles: list[tuple[float, float, float, float]],
) -> bool:
    spans = (
        (min(x, side) - 0.15, y_out - 0.15, max(x, side) + 0.15, y_out + 0.15),
        (side - 0.15, min(y_out, y_back), side + 0.15, max(y_out, y_back)),
        (min(x, side) - 0.15, y_back - 0.15, max(x, side) + 0.15, y_back + 0.15),
    )
    return not any(_rects_overlap(span, obs, 0.2) for span in spans for obs in obstacles)


def _fit_table_detour(
    x: float,
    cursor_y: float,
    end_y: float,
    boxes: list[tuple[float, float, float, float]],
    obstacles: list[tuple[float, float, float, float]],
    *,
    downward: bool,
    x_lo: float,
    x_hi: float,
) -> tuple[float, float, float] | None:
    """Leave the drop, pass the table, and come back. None when no side is open."""

    top = min(box[1] for box in boxes)
    bot = max(box[3] for box in boxes)
    left = min(box[0] for box in boxes)
    right = max(box[2] for box in boxes)
    for gap in (1.0, 1.6, 2.4):
        if downward:
            y_out = top - gap
            y_back = bot + gap
            if y_out < cursor_y + 0.45:
                y_out = cursor_y + 0.55
            if y_back > end_y - 0.45:
                y_back = end_y - 0.55
            if y_back <= y_out + 0.8 or y_out > top - 0.45:
                continue
        else:
            y_out = bot + gap
            y_back = top - gap
            if y_out > cursor_y - 0.45:
                y_out = cursor_y - 0.55
            if y_back < end_y + 0.45:
                y_back = end_y + 0.55
            if y_back >= y_out - 0.8 or y_out < bot + 0.45:
                continue
        found: list[float] = []
        for extra in (1.2, 2.6, 4.5, 7.0):
            for side in (left - extra, right + extra):
                if side <= x_lo or side >= x_hi:
                    continue
                if _detour_clear(x, side, y_out, y_back, obstacles):
                    found.append(side)
            if found:
                side = min(found, key=lambda value: abs(value - x))
                return (y_out, side, y_back)
    return None


def _divert_vertical(
    start: Point,
    end: Point,
    mirrors: list[tuple[float, float, float, float]],
    obstacles: list[tuple[float, float, float, float]],
    *,
    x_lo: float,
    x_hi: float,
) -> list[Point]:
    bands = _mirror_bands(start.x, start.y, end.y, mirrors)
    if not bands:
        return [end]
    downward = end.y >= start.y
    if not downward:
        bands = list(reversed(bands))
    cursor_y = start.y
    points: list[Point] = []
    for boxes in bands:
        fitted = _fit_table_detour(
            start.x,
            cursor_y,
            end.y,
            boxes,
            obstacles,
            downward=downward,
            x_lo=x_lo,
            x_hi=x_hi,
        )
        if fitted is None:
            continue
        y_out, side, y_back = fitted
        if abs(y_out - cursor_y) > 0.05:
            points.append(Point(x=start.x, y=y_out))
        points.append(Point(x=side, y=y_out))
        points.append(Point(x=side, y=y_back))
        points.append(Point(x=start.x, y=y_back))
        cursor_y = y_back
    points.append(end)
    return points


def _divert_polyline(
    points: list[Point],
    mirrors: list[tuple[float, float, float, float]],
    obstacles: list[tuple[float, float, float, float]],
    *,
    x_lo: float,
    x_hi: float,
) -> list[Point]:
    if len(points) < 2:
        return points
    built = [points[0]]
    for nxt in points[1:]:
        prev = built[-1]
        if abs(prev.x - nxt.x) > 0.6:
            built.append(nxt)
            continue
        built.extend(
            _divert_vertical(prev, nxt, mirrors, obstacles, x_lo=x_lo, x_hi=x_hi)
        )
    return _collapse_points(built)


def _jog_around_mirrors(
    sheet: Sheet,
    catalog: Catalog,
    project: Project,
    routes: dict[str, list[Point]],
) -> dict[str, list[Point]]:
    """Step an IEC drop around a contact table that has to sit on that drop.

    The table stays in the coil's header column. A 20 mm table cannot also
    clear the drop and the next column's table, so the drop leaves the column
    centre for the height of the table and comes back.
    """

    from panelcad.render.xref import annotation_box, place_xref_labels

    try:
        placed = place_xref_labels(project, catalog, sheet, ladder=True)
    except (KeyError, ValueError, IndexError):
        return routes
    mirrors: list[tuple[float, float, float, float]] = []
    obstacles = [*sheet_obstacles(sheet, catalog), *pin_keepouts(sheet, catalog)]
    tx, ty, tw, th = title_block_rect(sheet)
    obstacles.append((tx, ty, tx + tw, ty + th))
    for ann, x, y, align in placed:
        box = annotation_box(ann, x, y, align)
        obstacles.append(box)
        if ann.kind == "mirror" and ann.mirror_rows:
            mirrors.append(box)
    if not mirrors:
        return routes
    wx, _wy, ww, _wh = working_rect(sheet)
    manual: set[str] = set()
    for wire in sheet.wires:
        pts = routes.get(wire.id)
        if wire.points and pts and len(pts) >= 2 and _orthogonal(pts):
            manual.add(wire.id)
    out: dict[str, list[Point]] = {}
    for wire_id, pts in routes.items():
        if wire_id in manual or len(pts) < 2:
            out[wire_id] = pts
            continue
        out[wire_id] = _divert_polyline(
            pts, mirrors, obstacles, x_lo=wx + 1.5, x_hi=wx + ww - 1.5
        )
    return out


def route_sheet_wires(
    sheet: Sheet,
    catalog: Catalog,
    project: Project | None = None,
) -> dict[str, list[Point]]:
    """Route around symbols, then pull stacked runs off earlier traces. Crossings stay.

    On an IEC column sheet, *project* lets a coil's return drop step around
    that coil's contact table. The placement call sees the straight drop, so
    a re-entry here returns those straight runs and does not jog again.
    """

    global _IN_ROUTE
    from panelcad.core.ladder import is_iec_column_sheet

    if _IN_ROUTE:
        return _straight_routes(sheet, catalog, project)
    key = _route_key(sheet, project, catalog)
    cached = _ROUTE_CACHE.get(key)
    if cached is not None:
        return cached
    straight = _straight_routes(sheet, catalog, project)
    routed = straight
    if project is not None and is_iec_column_sheet(sheet):
        _IN_ROUTE = True
        try:
            routed = _jog_around_mirrors(sheet, catalog, project, straight)
        finally:
            _IN_ROUTE = False
    _ROUTE_CACHE[key] = routed
    return routed
