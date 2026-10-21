"""Shop ladder layout for Circuit Builder control sheets.

Power sheets stay schematic. Motor control (and PLC I/O) sheets are
elementary diagrams: hot rail left, return rail right, horizontal rungs.
"""

from __future__ import annotations

import math
from typing import Any

from panelcad.core.circuit_builder import (
    _Part,
    _by_role,
    _device,
    _one,
    _pin_y,
    _size,
)
from panelcad.core.ladder import rail_xs
from panelcad.render.titleblock import working_rect

RUNG_PITCH = 36.0
CONTACT_GAP = 12.0
RAIL_PAD = 14.0
BODY_GAP = 3.0
NEXT_HEAD = 28.0
RETURN_NETS = frozenset({"0V", "0VDC", "L2", "X2", "N", "NEUTRAL"})


def _rail_hint(extra: dict[str, Any] | None) -> Any:
    """A number used only to pick rail names. A guess is not a stated voltage."""

    if not extra:
        return None
    if extra.get("control_voltage") not in (None, ""):
        return extra.get("control_voltage")
    return extra.get("rail_voltage_hint")


def rail_pair(extra: dict[str, Any]) -> tuple[str, str]:
    ctrl = _rail_hint(extra)
    if ctrl is not None and float(ctrl) >= 100:
        return "X1", "X2"
    return "24V", "0V"


def store_control_voltage(session: Any, sheet: Any, extra: dict[str, Any] | None) -> None:
    """Remember a stated control voltage. Rails named L1/L2 do not carry one."""

    raw = (extra or {}).get("control_voltage")
    if raw in (None, ""):
        return
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return
    if not math.isfinite(value) or value <= 0:
        return
    sheet.control_voltage = value
    project = getattr(session, "project", None)
    if project is not None:
        project.control_voltage = value


def prepare_ladder_sheet(session: Any, sheet_id: str, extra: dict[str, Any]) -> tuple[str, str]:
    hot, ret = rail_pair(extra)
    sheet = session.sheet(sheet_id)
    sheet.drawing_style = "ladder"
    sheet.rail_hot = hot
    sheet.rail_return = ret
    store_control_voltage(session, sheet, extra)
    return hot, ret


def supply_above_rails(session: Any, sheet_id: str, parts: list[_Part]) -> None:
    """Park the PSU / CPT on the top of the sheet as the rail source."""

    supply = next((p for p in parts if p.role in {"psu", "transformer"}), None)
    if supply is None:
        return
    wx, wy, ww, _wh = working_rect(session.sheet(sheet_id))
    width, _h = _size(_device(session, supply.library_id))
    left, right = rail_xs(session.sheet(sheet_id))
    # Sit on the rail header, centered, so rungs start below it and do not
    # share its X with the left-side contacts.
    supply.rotation = 0
    supply.scale = 1.0
    supply.x = wx + (ww - width) / 2.0
    # Room above the body so +24V can hop over the symbol to the left rail.
    supply.y = wy + 16.0
    # Keep it between the rails so plus/minus drops are short.
    supply.x = min(max(supply.x, left + 8.0), right - width - 8.0)


def _body(session: Any, part: _Part) -> tuple[float, float]:
    return _size(_device(session, part.library_id))


def _part_pin_x(session: Any, part: _Part, pin_id: str) -> float:
    device = _device(session, part.library_id)
    return float(part.x) + float(device.pin(pin_id).x)


def _contact_origins(
    session: Any,
    fresh: list[_Part],
    cursor: float,
    right: float,
    output: _Part | None,
    placed: set[int],
    free_span: bool,
) -> list[float]:
    """Pack a rung, then share leftover width so contacts are not bunched at the rail."""

    if not fresh:
        return []
    if not free_span or len(fresh) < 2:
        origins: list[float] = []
        x = cursor
        for part in fresh:
            origins.append(x)
            x += _body(session, part)[0] + CONTACT_GAP
        return origins
    from panelcad.core.spacing import MIN_H_MM, spread_row

    widths = [_body(session, part)[0] for part in fresh]
    gap = max(CONTACT_GAP, MIN_H_MM)
    reserve = RAIL_PAD
    if output is not None and id(output) not in placed:
        reserve = _body(session, output)[0] + RAIL_PAD + gap
    limit = right - reserve
    return spread_row(cursor, limit, widths, gap)


def layout_rungs(
    session: Any,
    sheet_id: str,
    rungs: list[dict[str, Any]],
    parts: list[_Part] | None = None,
) -> None:
    """Place contacts left-to-right and the coil/lamp on the right rail.

    Each rung dict: contacts: list[_Part], output: _Part | None,
    output_pin: str, seal: _Part | None, seal_under: _Part | None.
    """

    sheet = session.sheet(sheet_id)
    left, right = rail_xs(sheet)
    wx, wy, _ww, wh = working_rect(sheet)
    max_y = wy + wh
    pin_y = wy + 42.0
    # A second circuit must start under parts already on the sheet. The contacts
    # this call is about to move are still sitting at their old coordinates, and
    # counting those would shove the next rung into the seal.
    moving = set()
    for rung in rungs:
        for part in rung.get("contacts") or []:
            if getattr(part, "ref", ""):
                moving.add(part.ref)
        for key in ("output", "seal", "seal_under", "tap", "join"):
            part = rung.get(key)
            if getattr(part, "ref", ""):
                moving.add(part.ref)
    for comp in sheet.components:
        if comp.id in moving:
            continue
        try:
            _width, height = _size(_device(session, comp.library_id))
        except (KeyError, AttributeError):
            height = 16.0
        pin_y = max(pin_y, float(comp.y) + height + 10.0)
    for part in parts or []:
        if part.role in {"psu", "transformer"}:
            _w, height = _body(session, part)
            # First rung must clear the PSU body — later contacts sit under it.
            pin_y = max(pin_y, part.y + height + 24.0)

    def _fresh(rung: dict[str, Any], seen: set[int]) -> list[_Part]:
        return [
            p
            for p in rung.get("contacts", [])
            if p.role not in {"psu", "transformer"} and id(p) not in seen
        ]

    def _headroom(items: list[_Part]) -> float:
        heads = []
        for part in items:
            device = _device(session, part.library_id)
            pin = part.align_pin or next((p.id for p in device.pins if p.side == "left"), device.pins[0].id)
            heads.append(_pin_y(device, pin))
        return max(heads) if heads else 12.0

    placed: set[int] = set()
    placed_parts: list[_Part] = []
    for idx, rung in enumerate(rungs):
        contacts: list[_Part] = [p for p in rung.get("contacts", []) if p.role not in {"psu", "transformer"}]
        output: _Part | None = rung.get("output")
        out_pin = str(rung.get("output_pin") or "A1")
        reused = [p for p in contacts if id(p) in placed]
        fresh = [p for p in contacts if id(p) not in placed]
        cursor = left + RAIL_PAD
        span_lo = span_hi = None
        tap = rung.get("tap")
        join = rung.get("join")
        tap_out = rung.get("tap_out")
        if (
            tap_out
            and tap is not None
            and join is not None
            and id(tap) in placed
            and id(join) in placed
            and fresh
        ):
            # Auto contact sits on the lower branch between the A drop and the coil tee.
            span_lo = _part_pin_x(session, tap, str(tap_out))
            jdev = _device(session, join.library_id)
            jout = next((p.id for p in jdev.pins if p.side == "right"), jdev.pins[-1].id)
            span_hi = _part_pin_x(session, join, jout)
            cursor = span_lo + 2.0
        else:
            if reused:
                cursor = max(p.x + _body(session, p)[0] for p in reused) + CONTACT_GAP
            for other in placed_parts:
                if "seal" not in (other.role or ""):
                    continue
                ow, _oh = _body(session, other)
                if other.x - 2.0 <= cursor <= other.x + ow + 2.0:
                    cursor = other.x + ow + CONTACT_GAP
        rung_bottom = pin_y
        origins = _contact_origins(session, fresh, cursor, right, output, placed, span_lo is None)
        for index, part in enumerate(fresh):
            device = _device(session, part.library_id)
            width, height = _size(device)
            pin = part.align_pin or next((p.id for p in device.pins if p.side == "left"), device.pins[0].id)
            part.align_pin = pin
            # A column contact arrives rotated 90°. The ladder origin is the unrotated body.
            part.rotation = 0
            part.scale = 1.0
            part.x = origins[index] if index < len(origins) else cursor
            if span_lo is not None and span_hi is not None:
                part.x = min(max(span_lo + 2.0, part.x), max(span_lo + 1.0, span_hi - 2.0 - width))
            part.y = pin_y - _pin_y(device, pin)
            part.page = part.page or "control"
            cursor = part.x + width + CONTACT_GAP
            placed.add(id(part))
            placed_parts.append(part)
            rung_bottom = max(rung_bottom, part.y + height)
        if output is not None and id(output) not in placed:
            device = _device(session, output.library_id)
            width, height = _size(device)
            pin_id = out_pin if any(p.id == out_pin for p in device.pins) else (
                next((p.id for p in device.pins if p.side == "left"), device.pins[0].id)
            )
            output.rotation = 0
            output.scale = 1.0
            output.x = right - RAIL_PAD - width
            output.y = pin_y - _pin_y(device, pin_id)
            if output.x < cursor + 4.0:
                output.x = min(cursor + 4.0, right - 8.0 - width)
            for other in placed_parts:
                ow, oh = _body(session, other)
                if output.x + width <= other.x or other.x + ow <= output.x:
                    continue
                need = other.y + oh + BODY_GAP
                if output.y < need and output.y + height > other.y:
                    output.y = need
            placed.add(id(output))
            placed_parts.append(output)
            rung_bottom = max(rung_bottom, output.y + height)
        seal: _Part | None = rung.get("seal")
        under: _Part | None = rung.get("seal_under")
        if seal is not None and under is not None:
            sdev = _device(session, seal.library_id)
            spin = seal.align_pin or next((p.id for p in sdev.pins if p.side == "left"), sdev.pins[0].id)
            seal.align_pin = spin
            _sw, sh = _size(sdev)
            _uw, uh = _body(session, under)
            udev = _device(session, under.library_id)
            u_left = next((p for p in udev.pins if p.side == "left"), udev.pins[0])
            u_right = next((p for p in udev.pins if p.side == "right"), udev.pins[-1])
            s_left = next((p for p in sdev.pins if p.side == "left"), sdev.pins[0])
            s_right = next((p for p in sdev.pins if p.side == "right"), sdev.pins[-1])
            span_u = float(u_right.x) - float(u_left.x)
            span_s = float(s_right.x) - float(s_left.x)
            scale = span_u / span_s if span_s > 0.5 else 1.0
            # Both seal pins sit on the button verticals, so neither lead runs past the tee.
            seal.rotation = 0
            seal.scale = scale
            seal.x = under.x + float(u_left.x) - float(s_left.x) * scale
            seal.y = under.y + uh + BODY_GAP
            placed.add(id(seal))
            placed_parts.append(seal)
            rung_bottom = max(rung_bottom, seal.y + sh)
        upcoming = []
        if idx + 1 < len(rungs):
            upcoming = _fresh(rungs[idx + 1], placed)
            out_n = rungs[idx + 1].get("output")
            if out_n is not None and id(out_n) not in placed:
                upcoming.append(out_n)
        head = _headroom(upcoming)
        pilots_next = bool(
            upcoming
            and all("pilot" in (p.role or "") or p.role in {"pilot_run", "pilot_fault", "pilot_fwd", "pilot_rev"} for p in upcoming)
        )
        # LT sits at the bottom of the lamp; keep a full rung so it misses the next tag.
        step = 48.0 if pilots_next else RUNG_PITCH
        nxt = max(pin_y + step, rung_bottom + head + BODY_GAP)
        if nxt + 20.0 > max_y:
            floor = 46.0 if pilots_next else 26.0
            nxt = min(nxt, max(pin_y + floor, max_y - 22.0))
        pin_y = nxt


def _in_out(session: Any, part: _Part, pins: Any) -> tuple[str, str]:
    role = part.role
    if role in {"estop"}:
        return pins.estop_in, pins.estop_out
    if role in {"stop"}:
        return pins.stop_in, pins.stop_out
    if role in {"start", "fwd_pb", "rev_pb"}:
        return pins.start_in, pins.start_out
    if "ol" in role or role.endswith("fault"):
        return pins.ol_nc_in, pins.ol_nc_out
    if role == "tr_nc":
        return pins.aux_nc_in, pins.aux_nc_out
    if role == "tr_no":
        return pins.aux_no_in, pins.aux_no_out
    if role == "cr_no":
        return pins.aux_no_in, pins.aux_no_out
    if role == "hoa_auto":
        return pins.selector_com, pins.selector_auto
    if "ilock" in role or role.endswith("_nc") or role.endswith("nc"):
        return pins.aux_nc_in, pins.aux_nc_out
    if role in {"hoa"}:
        return pins.selector_com, pins.selector_hand
    if role in {"selector"}:
        return pins.selector_com, pins.selector_hand
    if role.startswith("pilot") or role in {"pilot_run", "pilot_fault", "pilot_fwd", "pilot_rev"}:
        return pins.pilot_in, pins.pilot_out
    if "seal" in role or "aux" in role or "fb" in role or role.endswith("_no"):
        return pins.aux_no_in, pins.aux_no_out
    device = _device(session, part.library_id)
    left = next((p.id for p in device.pins if p.side == "left"), device.pins[0].id)
    right = next((p.id for p in device.pins if p.side == "right"), device.pins[-1].id)
    return left, right


def _has_pin(session: Any, part: _Part, pin_id: str) -> bool:
    return any(p.id == pin_id for p in _device(session, part.library_id).pins)


def _seal_start(series: list[_Part], seal_under: _Part | None) -> _Part | None:
    if seal_under is not None:
        return seal_under
    for role in ("start", "fwd_pb", "rev_pb"):
        for part in series:
            if part.role == role:
                return part
    if len(series) >= 2:
        return series[-2]
    return None


def wire_rung(
    connect: Any,
    end: Any,
    session: Any,
    parts: list[_Part],
    pins: Any,
    *,
    sheet: str,
    hot: str,
    ret: str,
    contacts: list[_Part],
    output: _Part | None,
    out_in: str = "A1",
    out_ret: str = "A2",
    seal: _Part | None = None,
    seal_under: _Part | None = None,
    feed: str | None = None,
    tap: _Part | None = None,
    tap_out: str | None = None,
    join: _Part | None = None,
    join_pin: str | None = None,
    out_net: str = "RUN",
    sink: str | None = None,
    sink_sheet: str | None = None,
) -> None:
    """Series path: rail (or feed) → contacts → output, output return → rail."""

    series = [p for p in contacts if p.role not in {"psu", "transformer"}]
    if tap is not None and tap_out:
        feed = end(tap, tap_out)
        series = [p for p in series if p is not tap]
    elif tap is not None and tap in series:
        series = series[series.index(tap) + 1 :]
        _tin, tout = _in_out(session, tap, pins)
        feed = end(tap, tout)
    if join is not None:
        _jin, jout = _in_out(session, join, pins)
        dest = end(join, join_pin or jout)
        dest_sheet = sheet
    elif output is not None:
        dest = end(output, out_in)
        dest_sheet = sheet
    else:
        dest = sink
        dest_sheet = sink_sheet or sheet
    if dest is None:
        return
    hop = 0

    def hop_net() -> str:
        nonlocal hop
        hop += 1
        return f"{out_net}:{hop}"

    if not series:
        connect(feed or hot, dest, hop_net() if feed else hot, sheet_id=sheet, to_sheet=dest_sheet)
        if join is None:
            _return_to_rail(session, connect, end, output, out_ret, ret, sheet)
        return

    in_net: dict[int, str] = {}
    out_hop: dict[int, str] = {}
    first_in, _ = _in_out(session, series[0], pins)
    first_net = hot if feed is None else hop_net()
    # A reused E-stop may live on another sheet. Leaving sheet_id unset lets
    # the caller open a cross-sheet wire; a same-sheet tap stays put.
    if tap is not None and getattr(tap, "reuse", False):
        connect(feed or hot, end(series[0], first_in), first_net)
    else:
        connect(feed or hot, end(series[0], first_in), first_net, sheet_id=sheet)
    in_net[id(series[0])] = first_net
    if tap is not None and tap_out:
        _route_drop_across(session, sheet, tap, tap_out, series[0], first_in)
    for left, right in zip(series, series[1:], strict=False):
        _lin, lout = _in_out(session, left, pins)
        rin, _rout = _in_out(session, right, pins)
        name = hop_net()
        connect(end(left, lout), end(right, rin), name, sheet_id=sheet)
        out_hop[id(left)] = name
        in_net[id(right)] = name
    last = series[-1]
    _lin, lout = _in_out(session, last, pins)
    last_net = hop_net()
    connect(end(last, lout), dest, last_net, sheet_id=sheet, to_sheet=dest_sheet)
    out_hop[id(last)] = last_net
    if join is not None:
        _jin, jout = _in_out(session, join, pins)
        _route_join_tee(session, sheet, last, lout, join, join_pin or jout)
    if join is None:
        _return_to_rail(session, connect, end, output, out_ret, ret, sheet)
    if seal is None:
        return
    under = _seal_start(series, seal_under)
    if under is None:
        return
    sin, sout = _in_out(session, seal, pins)
    uin, uout = _in_out(session, under, pins)
    connect(end(under, uin), end(seal, sin), in_net.get(id(under), first_net), sheet_id=sheet)
    connect(end(seal, sout), end(under, uout), out_hop.get(id(under), last_net), sheet_id=sheet)
    _route_seal_u(session, sheet, under, seal, uin, uout, sin, sout)


def _route_seal_u(
    session: Any,
    sheet_id: str,
    start: _Part,
    seal: _Part,
    start_in: str,
    start_out: str,
    seal_in: str,
    seal_out: str,
) -> None:
    """One vertical at each start-button pin: 203 down to the seal, 204 back up."""

    from panelcad.render.sheet import pin_world

    page = session.sheet(sheet_id)
    try:
        start_comp = page.component(start.ref)
        seal_comp = page.component(seal.ref)
    except KeyError:
        return
    sdev = session.catalog.get(start_comp.library_id)
    adev = session.catalog.get(seal_comp.library_id)
    in_x, _in_y = pin_world(start_comp, sdev.pin(start_in))
    out_x, _out_y = pin_world(start_comp, sdev.pin(start_out))
    _sl_x, sl_y = pin_world(seal_comp, adev.pin(seal_in))
    _sr_x, sr_y = pin_world(seal_comp, adev.pin(seal_out))
    left = {f"{start.ref}.{start_in}", f"{seal.ref}.{seal_in}"}
    right = {f"{start.ref}.{start_out}", f"{seal.ref}.{seal_out}"}
    for wire in page.wires:
        ends = {wire.frm, wire.to}
        if ends == left:
            session.set_wire_route(wire.id, [(in_x, sl_y)], sheet_id=sheet_id)
        elif ends == right:
            session.set_wire_route(wire.id, [(out_x, sr_y)], sheet_id=sheet_id)


def _route_drop_across(
    session: Any,
    sheet_id: str,
    src: _Part,
    src_pin: str,
    dst: _Part,
    dst_pin: str,
) -> None:
    """Drop from the selector output, then run across into the Auto contact."""

    from panelcad.render.sheet import pin_world

    page = session.sheet(sheet_id)
    try:
        src_comp = page.component(src.ref)
        dst_comp = page.component(dst.ref)
    except KeyError:
        return
    sdev = session.catalog.get(src_comp.library_id)
    ddev = session.catalog.get(dst_comp.library_id)
    sx, _sy = pin_world(src_comp, sdev.pin(src_pin))
    _dx, dy = pin_world(dst_comp, ddev.pin(dst_pin))
    want = {f"{src.ref}.{src_pin}", f"{dst.ref}.{dst_pin}"}
    for wire in page.wires:
        if {wire.frm, wire.to} == want:
            session.set_wire_route(wire.id, [(sx, dy)], sheet_id=sheet_id)
            return


def _route_join_tee(
    session: Any,
    sheet_id: str,
    src: _Part,
    src_pin: str,
    join: _Part,
    join_pin: str,
) -> None:
    """Rise to the join pin on one vertical so Auto does not redraw the coil feed."""

    from panelcad.render.sheet import pin_world

    page = session.sheet(sheet_id)
    try:
        src_comp = page.component(src.ref)
        join_comp = page.component(join.ref)
    except KeyError:
        return
    sdev = session.catalog.get(src_comp.library_id)
    jdev = session.catalog.get(join_comp.library_id)
    _sx, sy = pin_world(src_comp, sdev.pin(src_pin))
    jx, _jy = pin_world(join_comp, jdev.pin(join_pin))
    want = {f"{src.ref}.{src_pin}", f"{join.ref}.{join_pin}"}
    for wire in page.wires:
        if {wire.frm, wire.to} == want:
            session.set_wire_route(wire.id, [(jx, sy)], sheet_id=sheet_id)
            return


def _already_wired(session: Any, sheet_id: str, frm: str, to: str) -> bool:
    page = session.sheet(sheet_id)
    want = {frm, to}
    return any({wire.frm, wire.to} == want for wire in page.wires)


def _return_to_rail(
    session: Any,
    connect: Any,
    end: Any,
    output: _Part | None,
    out_ret: str,
    ret: str,
    sheet: str,
) -> None:
    """One A2/X2 stub per coil. A second 0V wire on the same pin double-labels it."""

    if output is None or not _has_pin(session, output, out_ret):
        return
    frm = end(output, out_ret)
    if _already_wired(session, sheet, frm, ret):
        return
    connect(frm, ret, ret, sheet_id=sheet)


def _route_supply_clear_of_body(session: Any, sheet_id: str, supply: _Part, plus_pin: str, hot: str) -> None:
    """+24V leaves the right-hand output and hops *over* the PSU to the left rail."""

    from panelcad.core.ladder import rail_xs
    from panelcad.render.sheet import pin_world
    from panelcad.render.titleblock import working_rect

    page = session.sheet(sheet_id)
    try:
        comp = page.component(supply.ref)
    except KeyError:
        return
    device = session.catalog.get(comp.library_id)
    px, py = pin_world(comp, device.pin(plus_pin))
    left, _right = rail_xs(page)
    wx, wy, _ww, _wh = working_rect(page)
    # Corridor between the rail header and the top of the PSU body.
    over_y = max(wy + 3.0, min(comp.y - 4.0, py - 8.0))
    stub_x = px + 6.0
    plus = f"{supply.ref}.{plus_pin}"
    for wire in page.wires:
        if plus in {wire.frm, wire.to} and hot in {wire.frm, wire.to}:
            session.set_wire_route(
                wire.id,
                [(stub_x, py), (stub_x, over_y), (left + 1.0, over_y)],
                sheet_id=sheet_id,
            )
            return


def _rail_already_fed(session: Any, sheet: str, hot: str) -> bool:
    page = session.sheet(sheet)
    for wire in page.wires:
        if hot in {wire.frm, wire.to} or str(wire.net or "") == hot:
            return True
    return False


def _route_iec_supply(
    session: Any,
    sheet_id: str,
    supply: _Part,
    plus_pin: str,
    minus_pin: str,
    hot: str,
    ret: str,
) -> None:
    """Plus rises to L+ on one X. Minus drops to 0V on another. Never the same line."""

    from panelcad.core.geometry import Point
    from panelcad.render.sheet import pin_world
    from panelcad.render.wiring import clear_route_cache

    page = session.sheet(sheet_id)
    try:
        comp = page.component(supply.ref)
    except KeyError:
        return
    device = session.catalog.get(comp.library_id)
    plus = pin_world(comp, device.pin(plus_pin))
    minus = pin_world(comp, device.pin(minus_pin))
    right = comp.x + float(device.schematic_width) * comp.visual_scale()
    plus_x = right + 8.0
    minus_x = right + 18.0

    def _land(pin: str, rail: str, riser: float, pin_y: float) -> None:
        token = f"{supply.ref}.{pin}"
        for wire in page.wires:
            if token in {wire.frm, wire.to} and rail in {wire.frm, wire.to}:
                wire.points = [Point(x=riser, y=pin_y)]
                return

    _land(plus_pin, hot, plus_x, plus[1])
    _land(minus_pin, ret, minus_x, minus[1])
    clear_route_cache()


def wire_supply_to_rails(
    connect: Any,
    end: Any,
    parts: list[_Part],
    symbols: Any,
    *,
    sheet: str,
    hot: str,
    ret: str,
    session: Any = None,
    external: _Part | None = None,
) -> None:
    pins = symbols.pins
    if session is not None and _rail_already_fed(session, sheet, hot):
        return
    if _by_role(parts, "transformer"):
        supply = _one(parts, "transformer")
        connect(end(supply, pins.cpt_hot), hot, hot, sheet_id=sheet)
        connect(end(supply, pins.cpt_ret), ret, ret, sheet_id=sheet)
        if session is not None and _iec_columns(session, sheet):
            _route_iec_supply(session, sheet, supply, pins.cpt_hot, pins.cpt_ret, hot, ret)
        elif session is not None:
            _route_supply_clear_of_body(session, sheet, supply, pins.cpt_hot, hot)
        return
    if _by_role(parts, "psu"):
        supply = _one(parts, "psu")
        connect(end(supply, pins.psu_plus), hot, hot, sheet_id=sheet)
        connect(end(supply, pins.psu_minus), ret, ret, sheet_id=sheet)
        if session is not None and _iec_columns(session, sheet):
            _route_iec_supply(session, sheet, supply, pins.psu_plus, pins.psu_minus, hot, ret)
        elif session is not None:
            _route_supply_clear_of_body(session, sheet, supply, pins.psu_plus, hot)
        return
    if external is None:
        return
    plus = pins.cpt_hot if external.role == "transformer" else pins.psu_plus
    minus = pins.cpt_ret if external.role == "transformer" else pins.psu_minus
    if getattr(external, "reuse", False):
        # The supply already sits on an earlier control sheet. A rail name has
        # no component home, so connect() would otherwise land the wire on the
        # power sheet. Same sheet: wire there. A new ladder or IEC sheet gets
        # an off-page feed from that supply pin to this sheet's rail.
        _feed_reused_supply(session, connect, end, external, plus, sheet, hot, hot)
        _feed_reused_supply(session, connect, end, external, minus, sheet, ret, ret)
    else:
        connect(end(external, plus), hot, hot, sheet_id=sheet)
        connect(end(external, minus), ret, ret, sheet_id=sheet)


def _feed_reused_supply(
    session: Any,
    connect: Any,
    end: Any,
    supply: _Part,
    pin: str,
    sheet: str,
    rail: str,
    net: str,
) -> None:
    """Land a shared supply pin on this control sheet's rail."""

    from panelcad.core.circuit_builder import _schematic_home

    pin_ref = end(supply, pin)
    home = _schematic_home(session, pin_ref) if session is not None else None
    if home is None or home == sheet:
        connect(pin_ref, rail, net, sheet_id=home or sheet)
        return
    _offpage_to_rail(session, home, pin_ref, sheet, rail, net)


def _offpage_to_rail(
    session: Any,
    home: str,
    pin_ref: str,
    dest_sheet: str,
    rail: str,
    net: str,
    rung_y: float | None = None,
) -> None:
    """Off-page link from a supply pin to a rail on another control sheet."""

    from panelcad.core.cleanup import sheet_number
    from panelcad.core.ladder import is_iec_column_sheet
    from panelcad.render.sheet import parse_pin_ref, pin_world

    link = f"{home}:{pin_ref}|{dest_sheet}:{rail}"
    for page in session.project.sheets:
        if any(str(comp.properties.get("link") or "") == link for comp in page.components):
            return
    src = session.sheet(home)
    dest = session.sheet(dest_sheet)
    comp_id, pin_id = parse_pin_ref(pin_ref)
    comp = src.component(comp_id)
    device = session.catalog.get(comp.library_id)
    px, py = pin_world(comp, device.pin(pin_id))
    src_no = sheet_number(session.project.sheets, home) or 1
    dst_no = sheet_number(session.project.sheets, dest_sheet) or 1
    rung = float(py if rung_y is None else rung_y)
    hidden = {
        "hide_tag": True,
        "in_panel": False,
        "incabinet": False,
        "mount": "none",
        "din_rail": False,
        "link": link,
        "role": "supply_feed",
    }
    hot = str(dest.rail_hot or "")
    side = "left" if rail == hot else "right"
    if is_iec_column_sheet(dest) or is_iec_column_sheet(src):
        side = "top" if rail == hot else "bottom"
    to_ref = session.next_ref("iec.connection.offpage", home)
    seat_offpage_arrow(
        session,
        home,
        "iec.connection.offpage",
        to_ref,
        rung_y=rung,
        along=px,
        side=side,
        properties={
            **hidden,
            "tag": net,
            "callout": f"TO {dst_no}",
            "dest_sheet": dest_sheet,
            "dest_ref": rail,
            "dest_pin": "",
        },
    )
    session.wire(pin_ref, f"{to_ref}.A", net=net, sheet_id=home)

    from_ref = session.next_ref("iec.connection.offpage_from", dest_sheet)
    seat_offpage_arrow(
        session,
        dest_sheet,
        "iec.connection.offpage_from",
        from_ref,
        rung_y=rung,
        along=px,
        side=side,
        properties={
            **hidden,
            "tag": net,
            "callout": f"FROM {src_no}",
            "dest_sheet": home,
            "dest_ref": comp_id,
            "dest_pin": pin_id,
        },
    )
    session.wire(f"{from_ref}.A", rail, net=net, sheet_id=dest_sheet)


def link_split_feeds(
    session: Any,
    source: Any,
    dest: Any,
    crossing: list[tuple[str, str, str, str]],
) -> list[dict[str, Any]]:
    """One off-page pair per rail, and one pair for a contact that feeds several rungs.

    A pair sits in the free row under the devices, at the rail the net uses.
    Feed wires stay in that row or on a device's own pin stub. They do not
    run across a rung that is already drawn.
    """

    from panelcad.core.ladder import is_iec_column_sheet

    if is_iec_column_sheet(source) or is_iec_column_sheet(dest):
        return _link_each_crossing(session, source, dest, crossing)
    return _link_ladder_feeds(session, source, dest, crossing)


def _link_each_crossing(
    session: Any,
    source: Any,
    dest: Any,
    crossing: list[tuple[str, str, str, str]],
) -> list[dict[str, Any]]:
    """IEC columns keep one arrow per crossing. Ladder sheets share a rail pair."""

    links: list[dict[str, Any]] = []
    shared: dict[str, int] = {}
    for keep, move, _net, kind in crossing:
        if kind == "device":
            shared[keep] = shared.get(keep, 0) + 1
    for left, right, net, kind in crossing:
        if kind == "rail":
            _offpage_to_rail(
                session, dest.id, left, source.id, right, net or right, rung_y=_ref_pin_y(session, dest, left)
            )
            links.append({"from": left, "to": right, "to_sheet": source.id, "net": net, "kind": "rail"})
            continue
        stay_y = _ref_pin_y(session, source, left)
        move_y = _ref_pin_y(session, dest, right)
        src_y = move_y if shared.get(left, 0) > 1 and move_y is not None else stay_y
        links.append(
            session.wire_offpage(
                left,
                right,
                net=net or None,
                from_sheet=source.id,
                to_sheet=dest.id,
                label=net or None,
                src_rung_y=src_y,
                dst_rung_y=move_y,
            )
        )
    return links


def _link_ladder_feeds(
    session: Any,
    source: Any,
    dest: Any,
    crossing: list[tuple[str, str, str, str]],
) -> list[dict[str, Any]]:
    from collections import defaultdict

    rails: dict[str, list[str]] = defaultdict(list)
    fans: dict[str, list[tuple[str, str]]] = defaultdict(list)
    singles: list[tuple[str, str, str]] = []
    for keep, move, net, kind in crossing:
        if kind == "rail":
            rails[move].append(keep)
            continue
        fans[keep].append((move, net))
    for keep, moves in list(fans.items()):
        if len(moves) == 1:
            move, net = moves[0]
            singles.append((keep, move, net))
            del fans[keep]

    links: list[dict[str, Any]] = []
    hot = str(dest.rail_hot or source.rail_hot or "")
    ret = str(dest.rail_return or source.rail_return or "")
    for rail, pins in rails.items():
        for pin in pins:
            session.wire(pin, rail, net=rail or None, sheet_id=dest.id)
        links.append(_link_rail_pair(session, source, dest, rail, hot))
    for keep, moves in fans.items():
        side_rail = _fan_rail(session, source, keep, hot, ret)
        for move, _net in moves:
            session.wire(move, side_rail, net=side_rail or None, sheet_id=dest.id)
        links.append(_link_fan_pair(session, source, dest, keep, side_rail, moves[0][1]))
    for keep, move, net in singles:
        stay_y = _ref_pin_y(session, source, keep)
        move_y = _ref_pin_y(session, dest, move)
        links.append(
            session.wire_offpage(
                keep,
                move,
                net=net or None,
                from_sheet=source.id,
                to_sheet=dest.id,
                label=net or None,
                src_rung_y=stay_y,
                dst_rung_y=move_y,
            )
        )
    return links


def _ref_pin_y(session: Any, sheet: Any, pin_ref: str) -> float | None:
    if "." not in str(pin_ref):
        return None
    from panelcad.render.sheet import parse_pin_ref, pin_world

    try:
        cid, pid = parse_pin_ref(pin_ref)
        comp = sheet.component(cid)
        _px, py = pin_world(comp, session.catalog.get(comp.library_id).pin(pid))
    except (KeyError, ValueError):
        return None
    return float(py)


def _fan_rail(session: Any, sheet: Any, pin_ref: str, hot: str, ret: str) -> str:
    """The rail on the same side of the sheet as this shared contact."""

    from panelcad.render.sheet import parse_pin_ref

    wx, _wy, ww, _wh = working_rect(sheet)
    try:
        cid, _pid = parse_pin_ref(pin_ref)
        comp = sheet.component(cid)
    except (KeyError, ValueError):
        return hot or ret
    if float(comp.x) <= wx + ww / 2.0:
        return hot or ret
    return ret or hot


def _link_rail_pair(session: Any, source: Any, dest: Any, rail: str, hot: str) -> dict[str, Any]:
    """Tie one rail across the two sheets with a single arrow at each end."""

    from panelcad.core.cleanup import sheet_number

    side = "left" if rail == hot else "right"
    src_no = sheet_number(session.project.sheets, source.id) or 1
    dst_no = sheet_number(session.project.sheets, dest.id) or 1
    hidden = _feed_props(f"{source.id}:rail:{rail}|{dest.id}:rail:{rail}")
    # TO on the sheet that already has the rail, FROM on the continuation.
    _place_rail_arrow(
        session,
        source,
        "iec.connection.offpage",
        side=side,
        rail=rail,
        callout=f"TO {dst_no}",
        props={**hidden, "tag": rail, "dest_sheet": dest.id, "dest_ref": rail, "dest_pin": ""},
        role="supply_feed",
    )
    _place_rail_arrow(
        session,
        dest,
        "iec.connection.offpage_from",
        side=side,
        rail=rail,
        callout=f"FROM {src_no}",
        props={**hidden, "tag": rail, "dest_sheet": source.id, "dest_ref": rail, "dest_pin": ""},
        role="supply_feed",
    )
    return {"from": rail, "to": rail, "to_sheet": dest.id, "net": rail, "kind": "rail"}


def _link_fan_pair(
    session: Any,
    source: Any,
    dest: Any,
    keep: str,
    rail: str,
    net: str,
) -> dict[str, Any]:
    """One arrow for a contact that used to feed every moved rung."""

    from panelcad.core.cleanup import sheet_number
    from panelcad.core.geometry import Point
    from panelcad.render.sheet import parse_pin_ref, pin_world

    src_no = sheet_number(session.project.sheets, source.id) or 1
    dst_no = sheet_number(session.project.sheets, dest.id) or 1
    cid, pid = parse_pin_ref(keep)
    comp = source.component(cid)
    device = session.catalog.get(comp.library_id)
    px, py = pin_world(comp, device.pin(pid))
    hidden = _feed_props(f"{source.id}:{keep}|{dest.id}:rail:{rail}")
    origin_y = _feed_origin_y(source, session.catalog)
    channel = _clear_drop_x(source, session.catalog, float(px), float(py), origin_y + 6.0)
    to_ref = session.next_ref("iec.connection.offpage", source.id)
    session.place(
        "iec.connection.offpage",
        ref=to_ref,
        x=channel + 4.0,
        y=origin_y,
        sheet_id=source.id,
        snap=False,
        properties={
            **hidden,
            "tag": net or rail,
            "callout": f"TO {dst_no} / {rail}",
            "dest_sheet": dest.id,
            "dest_ref": rail,
            "dest_pin": "",
            "role": "to",
        },
    )
    arrow = source.component(to_ref)
    pin_x, pin_y = pin_world(arrow, session.catalog.get(arrow.library_id).pin("A"))
    wire = session.wire(keep, f"{to_ref}.A", net=net or rail, sheet_id=source.id)
    wire.points = [Point(x=channel, y=float(py)), Point(x=channel, y=float(pin_y))]
    _place_rail_arrow(
        session,
        dest,
        "iec.connection.offpage_from",
        side="left" if rail == str(dest.rail_hot or "") else "right",
        rail=rail,
        callout=f"FROM {src_no} / {rail}",
        props={
            **hidden,
            "tag": rail,
            "dest_sheet": source.id,
            "dest_ref": cid,
            "dest_pin": pid,
        },
        role="from",
    )
    return {"from": keep, "to": rail, "to_sheet": dest.id, "net": net or rail, "kind": "fan"}


def _feed_props(link: str) -> dict[str, Any]:
    return {
        "hide_tag": True,
        "in_panel": False,
        "incabinet": False,
        "mount": "none",
        "din_rail": False,
        "link": link,
    }


def _place_rail_arrow(
    session: Any,
    sheet: Any,
    library_id: str,
    *,
    side: str,
    rail: str,
    callout: str,
    props: dict[str, Any],
    role: str,
) -> None:
    """Park an arrow on the rail in the free row and tie its pin to that rail."""

    from panelcad.core.geometry import Point
    from panelcad.core.ladder import rail_xs
    from panelcad.render.sheet import pin_world

    symbol = session.catalog.get(library_id)
    width = float(symbol.schematic_width or 32.0)
    pin = symbol.pin("A")
    origin_y = _feed_origin_y(sheet, session.catalog)
    wx, wy, ww, _wh = working_rect(sheet)
    left, right = rail_xs(sheet)
    if side == "right":
        # Pin on the right edge of a FROM arrow lands on the return rail.
        # A TO arrow's pin is on the left, so its body ends at the rail and the
        # stub jogs through the clear band above the arrow.
        if float(pin.x) > width / 2.0:
            x = right - width
        else:
            x = min(right, wx + ww) - width
    else:
        x = max(left, wx) - (0.0 if float(pin.x) < width / 2.0 else float(pin.x))
        if x < wx + 0.4:
            x = wx + 0.4
    ref = session.next_ref(library_id, sheet.id)
    session.place(
        library_id,
        ref=ref,
        x=x,
        y=origin_y,
        sheet_id=sheet.id,
        snap=False,
        properties={**props, "callout": callout, "role": role},
    )
    comp = sheet.component(ref)
    pin_x, pin_y = pin_world(comp, session.catalog.get(comp.library_id).pin("A"))
    rail_x = right if side == "right" else left
    wire = session.wire(f"{ref}.A", rail, net=rail or None, sheet_id=sheet.id)
    if abs(float(pin_x) - rail_x) <= 0.8:
        return
    # Leave the pin away from the arrow body, then cross to the rail above it.
    band_y = max(origin_y - 4.0, wy + 1.0)
    away = -3.0 if float(pin.x) < width / 2.0 else 3.0
    out_x = float(pin_x) + away
    wire.points = [Point(x=out_x, y=float(pin_y)), Point(x=out_x, y=band_y)]
    wire.to_at = Point(x=rail_x, y=band_y)


def _feed_origin_y(sheet: Any, catalog: Any) -> float:
    """Top of an arrow in the open band under the last device."""

    from panelcad.core.spacing import _body_box

    _wx, wy, _ww, wh = working_rect(sheet)
    height = 12.0
    bottom = wy + 12.0
    for comp in sheet.components:
        if "offpage" in (comp.library_id or ""):
            continue
        try:
            box = _body_box(comp, catalog.get(comp.library_id))
        except KeyError:
            continue
        bottom = max(bottom, box[3])
    origin = bottom + 8.0
    if origin + height > wy + wh - 0.6:
        origin = wy + wh - height - 0.6
    return origin


def _clear_drop_x(sheet: Any, catalog: Any, pin_x: float, y0: float, y1: float) -> float:
    """A vertical channel beside the pin that misses every symbol."""

    from panelcad.core.spacing import _body_box

    boxes = []
    for comp in sheet.components:
        if "offpage" in (comp.library_id or ""):
            continue
        try:
            boxes.append(_body_box(comp, catalog.get(comp.library_id)))
        except KeyError:
            continue
    lo, hi = (y0, y1) if y0 <= y1 else (y1, y0)

    def clear(x: float) -> bool:
        for box in boxes:
            if x < box[0] - 1.5 or x > box[2] + 1.5:
                continue
            if hi < box[1] - 0.4 or lo > box[3] + 0.4:
                continue
            return False
        return True

    for step in range(1, 24):
        candidate = float(pin_x) + step * 2.0
        if clear(candidate):
            return candidate
    for step in range(1, 12):
        candidate = float(pin_x) - step * 2.0
        if clear(candidate):
            return candidate
    return float(pin_x) + 4.0


def seat_offpage_arrow(
    session: Any,
    sheet_id: str,
    library_id: str,
    ref: str,
    *,
    rung_y: float,
    side: str,
    properties: dict[str, Any],
    along: float | None = None,
) -> Any:
    """Park one off-page arrow on its rung, at the rail, inside the border.

    ``side`` is ``left`` or ``right`` on a ladder, ``top`` or ``bottom`` on an
    IEC column. A second arrow for the same rung steps inward, then devices
    that still cover the arrow move off it.
    """

    from panelcad.core.ladder import iec_rail_ys, rail_xs
    from panelcad.core.spacing import _body_box, _boxes_hit

    sheet = session.sheet(sheet_id)
    symbol = session.catalog.get(library_id)
    width = float(symbol.schematic_width or 32.0)
    height = float(symbol.schematic_height or 12.0)
    pin = symbol.pin("A")
    wx, wy, ww, wh = working_rect(sheet)
    x, y = _rail_arrow_origin(
        sheet,
        width,
        height,
        float(pin.x),
        float(pin.y),
        rung_y,
        side,
        along,
        rail_xs,
        iec_rail_ys,
    )
    x, y = _step_clear_of_arrows(
        sheet, session.catalog, x, y, width, height, side, _body_box, _boxes_hit
    )
    session.place(
        library_id,
        ref=ref,
        x=x,
        y=y,
        sheet_id=sheet_id,
        snap=False,
        properties=properties,
    )
    _open_arrow_slot(sheet, session.catalog, ref, side, _body_box, _boxes_hit)
    return sheet.component(ref)


def _rail_arrow_origin(
    sheet: Any,
    width: float,
    height: float,
    pin_x: float,
    pin_y: float,
    rung_y: float,
    side: str,
    along: float | None,
    rail_xs: Any,
    iec_rail_ys: Any,
) -> tuple[float, float]:
    """Origin that puts the arrow body on the rail and the pin on the rung."""

    wx, wy, ww, wh = working_rect(sheet)
    if side in {"top", "bottom"}:
        hot_y, ret_y = iec_rail_ys(sheet)
        rail_y = hot_y if side == "top" else ret_y
        y = rail_y - pin_y
        x = (float(along) if along is not None else wx + ww / 2.0) - width / 2.0
    else:
        left, right = rail_xs(sheet)
        y = float(rung_y) - pin_y
        if side == "right":
            edge = min(right, wx + ww)
            x = edge - width
        else:
            edge = max(left, wx)
            x = edge - pin_x
    x = min(max(x, wx + 0.4), wx + ww - width - 0.4)
    y = min(max(y, wy + 0.4), wy + wh - height - 0.4)
    return x, y


def _step_clear_of_arrows(
    sheet: Any,
    catalog: Any,
    x: float,
    y: float,
    width: float,
    height: float,
    side: str,
    body_box: Any,
    boxes_hit: Any,
) -> tuple[float, float]:
    """Keep a new arrow off arrows already on this sheet, still at the rail."""

    wx, wy, ww, wh = working_rect(sheet)
    gap = 2.0
    for _ in range(10):
        box = (x, y, x + width, y + height)
        blocked = None
        for comp in sheet.components:
            if "offpage" not in (comp.library_id or ""):
                continue
            try:
                other = body_box(comp, catalog.get(comp.library_id))
            except KeyError:
                continue
            if boxes_hit(box, other, pad=0.4):
                blocked = other
                break
        if blocked is None:
            return x, y
        if side == "right":
            shifted = blocked[0] - width - gap
            if shifted >= wx + 0.4:
                x = shifted
                continue
        elif side == "left":
            shifted = blocked[2] + gap
            if shifted + width <= wx + ww - 0.4:
                x = shifted
                continue
        elif side == "bottom":
            shifted = blocked[1] - height - gap
            if shifted >= wy + 0.4:
                y = shifted
                continue
        else:
            shifted = blocked[3] + gap
            if shifted + height <= wy + wh - 0.4:
                y = shifted
                continue
        nudged = y + height + gap
        if nudged + height <= wy + wh - 0.4:
            y = nudged
        else:
            y = max(wy + 0.4, y - height - gap)
    return x, y


def _open_arrow_slot(
    sheet: Any,
    catalog: Any,
    arrow_id: str,
    side: str,
    body_box: Any,
    boxes_hit: Any,
) -> None:
    """Slide devices off the arrow. The arrow stays on its rung at the rail."""

    wx, wy, ww, wh = working_rect(sheet)
    gap = 1.6
    horizontal = side in {"left", "right"}
    inward = -1.0 if side in {"right", "bottom"} else 1.0
    arrow = sheet.component(arrow_id)
    try:
        symbol = catalog.get(arrow.library_id)
    except KeyError:
        return
    height = float(symbol.schematic_height or 12.0)
    moved: set[str] = set()
    for _ in range(48):
        arrow_box = body_box(arrow, symbol)
        if not _push_off_arrow(
            sheet, catalog, arrow_box, body_box, boxes_hit, horizontal, inward, wx, wy, ww, wh, gap, moved
        ) and not _separate_device_pairs(
            sheet, catalog, body_box, boxes_hit, horizontal, inward, wx, wy, ww, wh, gap, moved
        ):
            if not _arrow_hits_device(sheet, catalog, arrow_box, body_box, boxes_hit):
                return
            # The rung is full. Step this arrow along the rail and try once more.
            if horizontal:
                bumped = arrow.y + height + gap
                if bumped + height > wy + wh - 0.4:
                    bumped = arrow.y - height - gap
                if bumped < wy + 0.4 or bumped + height > wy + wh - 0.4:
                    return
                arrow.y = bumped
            else:
                width = float(symbol.schematic_width or 32.0)
                bumped = arrow.x + width + gap
                if bumped + width > wx + ww - 0.4:
                    bumped = arrow.x - width - gap
                if bumped < wx + 0.4 or bumped + width > wx + ww - 0.4:
                    return
                arrow.x = bumped
            continue


def _arrow_hits_device(
    sheet: Any,
    catalog: Any,
    arrow_box: tuple[float, float, float, float],
    body_box: Any,
    boxes_hit: Any,
) -> bool:
    for comp in sheet.components:
        if "offpage" in (comp.library_id or ""):
            continue
        try:
            box = body_box(comp, catalog.get(comp.library_id))
        except KeyError:
            continue
        if boxes_hit(arrow_box, box, pad=0.2):
            return True
    return False


def _push_off_arrow(
    sheet: Any,
    catalog: Any,
    arrow_box: tuple[float, float, float, float],
    body_box: Any,
    boxes_hit: Any,
    horizontal: bool,
    inward: float,
    wx: float,
    wy: float,
    ww: float,
    wh: float,
    gap: float,
    moved: set[str],
) -> bool:
    """Move the first device that covers the arrow. True when something moved."""

    for comp in sheet.components:
        if "offpage" in (comp.library_id or ""):
            continue
        try:
            box = body_box(comp, catalog.get(comp.library_id))
        except KeyError:
            continue
        if not boxes_hit(arrow_box, box, pad=0.2):
            continue
        if horizontal and inward < 0:
            shift = box[2] - (arrow_box[0] - gap)
            if shift > 0 and box[0] - shift >= wx + 0.2:
                comp.x -= shift
                moved.add(comp.id)
                return True
        elif horizontal:
            shift = (arrow_box[2] + gap) - box[0]
            if shift > 0 and box[2] + shift <= wx + ww - 0.2:
                comp.x += shift
                moved.add(comp.id)
                return True
        elif inward < 0:
            shift = box[3] - (arrow_box[1] - gap)
            if shift > 0 and box[1] - shift >= wy + 0.2:
                comp.y -= shift
                moved.add(comp.id)
                return True
        else:
            shift = (arrow_box[3] + gap) - box[1]
            if shift > 0 and box[3] + shift <= wy + wh - 0.2:
                comp.y += shift
                moved.add(comp.id)
                return True
    return False


def _separate_device_pairs(
    sheet: Any,
    catalog: Any,
    body_box: Any,
    boxes_hit: Any,
    horizontal: bool,
    inward: float,
    wx: float,
    wy: float,
    ww: float,
    wh: float,
    gap: float,
    moved: set[str],
) -> bool:
    """Push a neighbor out of a device that was slid off the arrow."""

    devices: list[tuple[Any, tuple[float, float, float, float]]] = []
    for comp in sheet.components:
        if "offpage" in (comp.library_id or ""):
            continue
        try:
            devices.append((comp, body_box(comp, catalog.get(comp.library_id))))
        except KeyError:
            continue
    for index, (comp, box) in enumerate(devices):
        for other, obox in devices[index + 1 :]:
            if comp.id not in moved and other.id not in moved:
                continue
            if not boxes_hit(box, obox, pad=0.2):
                continue
            if horizontal and inward < 0:
                left, left_box, right_box = (comp, box, obox) if box[0] <= obox[0] else (other, obox, box)
                shift = left_box[2] + gap - right_box[0]
                if shift > 0 and left_box[0] - shift >= wx + 0.2:
                    left.x -= shift
                    moved.add(left.id)
                    return True
            elif horizontal:
                right, right_box, left_box = (comp, box, obox) if box[2] >= obox[2] else (other, obox, box)
                shift = left_box[2] + gap - right_box[0]
                if shift > 0 and right_box[2] + shift <= wx + ww - 0.2:
                    right.x += shift
                    moved.add(right.id)
                    return True
            elif inward < 0:
                upper, upper_box, lower_box = (comp, box, obox) if box[1] <= obox[1] else (other, obox, box)
                shift = upper_box[3] + gap - lower_box[1]
                if shift > 0 and upper_box[1] - shift >= wy + 0.2:
                    upper.y -= shift
                    moved.add(upper.id)
                    return True
            else:
                lower, lower_box, upper_box = (comp, box, obox) if box[3] >= obox[3] else (other, obox, box)
                shift = upper_box[3] + gap - lower_box[1]
                if shift > 0 and lower_box[3] + shift <= wy + wh - 0.2:
                    lower.y += shift
                    moved.add(lower.id)
                    return True
    return False


def route_rail_captions(session: Any, sheet_id: str | None) -> None:
    """Offset a long pin-to-rail run so it does not travel along the terminal name.

    Top and bottom captions sit on an IEC column drop. Left and right captions
    sit on a NEMA ladder rung. A run shorter than the pin neighborhood is left
    alone: the wire-label check already ignores that stub. Wires that already
    have a route (the PSU hop, a seal) are left alone.
    """

    from panelcad.core.geometry import Point
    from panelcad.core.ladder import is_iec_column_sheet, rail_names, rail_point
    from panelcad.render.sheet import pin_side_world, pin_world
    from panelcad.render.titleblock import working_rect
    from panelcad.render.wiring import clear_route_cache

    if not sheet_id:
        return
    try:
        page = session.sheet(sheet_id)
    except KeyError:
        return
    hot, ret = rail_names(page)
    if not hot and not ret:
        return
    rails = {name for name in (hot, ret) if name}
    iec = is_iec_column_sheet(page)
    wx, wy, ww, wh = working_rect(page)
    changed = False
    for wire in list(page.wires):
        if wire.points:
            continue
        ends = {wire.frm, wire.to}
        hit = ends & rails
        if len(hit) != 1:
            continue
        rail = next(iter(hit))
        pin_end = wire.to if wire.frm == rail else wire.frm
        if "." not in str(pin_end):
            continue
        ref, pid = str(pin_end).split(".", 1)
        try:
            comp = page.component(ref)
            pin = session.catalog.get(comp.library_id).pin(pid)
        except (KeyError, ValueError):
            continue
        px, py = pin_world(comp, pin)
        side = pin_side_world(comp, pin)
        if iec:
            if side not in {"top", "bottom"}:
                continue
            _rx, rail_y, _side = rail_point_iec(page, rail, px, hot, ret)
            if abs(rail_y - py) <= 9.0:
                continue
            # Step aside just past the caption and stay on that X. Returning to
            # the pin X drops through the tag of the contact beside the column
            # (star-delta K-13). A full +5 mm shift lands in the mirror table.
            dx = 3.5 if px + 3.5 <= wx + ww - 6.0 else -3.5
            past = py + 6.0 if side == "bottom" else py - 6.0
            elbow = [Point(x=px + dx, y=py), Point(x=px + dx, y=past)]
            wire.points = elbow if wire.frm == pin_end else list(reversed(elbow))
            changed = True
            continue
        if side not in {"left", "right"}:
            continue
        rx, _ry, _side = rail_point(page, rail, py, hot, ret)
        if abs(rx - px) <= 9.0:
            continue
        jog = -7.0 if py - 7.0 >= wy + 3.0 else 7.0
        if py + jog > wy + wh - 3.0:
            jog = -7.0
        jy = py + jog
        # Interior points follow frm → to. The rail is not always frm.
        if wire.frm == pin_end:
            wire.points = [Point(x=px, y=jy), Point(x=rx, y=jy)]
        else:
            wire.points = [Point(x=rx, y=jy), Point(x=px, y=jy)]
        changed = True
    # Pin-to-pin runs on one Y (a ladder series contact, or a PLC output
    # into a coil) travel through the side caption. Step off the pin, cross,
    # and come back. Vertical column runs already miss those captions.
    for wire in list(page.wires):
        if wire.points:
            continue
        if "." not in str(wire.frm) or "." not in str(wire.to):
            continue
        try:
            a_ref, a_pid = str(wire.frm).split(".", 1)
            b_ref, b_pid = str(wire.to).split(".", 1)
            a_comp = page.component(a_ref)
            b_comp = page.component(b_ref)
            a_pin = session.catalog.get(a_comp.library_id).pin(a_pid)
            b_pin = session.catalog.get(b_comp.library_id).pin(b_pid)
        except (KeyError, ValueError):
            continue
        ax, ay = pin_world(a_comp, a_pin)
        bx, by = pin_world(b_comp, b_pin)
        if abs(ax - bx) <= 9.0 and abs(ay - by) <= 9.0:
            continue
        a_side = pin_side_world(a_comp, a_pin)
        b_side = pin_side_world(b_comp, b_pin)
        if abs(ay - by) <= 0.6 and abs(ax - bx) > 9.0:
            if a_side not in {"left", "right"} and b_side not in {"left", "right"}:
                continue
            # Split the run so each caption sits on an 8 mm stub. The text check
            # ignores a stub whose midpoint is within 4 mm of its own pin. A jog
            # above the pin runs through the coil tag.
            span = bx - ax
            if abs(span) > 18.0:
                sign = 1.0 if span > 0 else -1.0
                wire.points = [
                    Point(x=ax + 8.0 * sign, y=ay),
                    Point(x=bx - 8.0 * sign, y=by),
                ]
            else:
                # The gap is shorter than two stubs. Step above the captions;
                # they only hang a couple of millimetres off the pin.
                jog = -4.0 if ay - 4.0 >= wy + 3.0 else 4.0
                wire.points = [Point(x=ax, y=ay + jog), Point(x=bx, y=by + jog)]
            changed = True
            continue
        # Elbow onto a side pin: the long horizontal ends in the caption.
        if abs(ay - by) > 0.6 and abs(ax - bx) > 18.0 and b_side in {"left", "right"}:
            sign = 1.0 if bx > ax else -1.0
            wire.points = [Point(x=ax, y=by), Point(x=bx - 8.0 * sign, y=by)]
            changed = True
    if changed:
        clear_route_cache()


def rail_point_iec(page: Any, rail: str, x: float, hot: str, ret: str) -> tuple[float, float, str]:
    from panelcad.core.ladder import iec_rail_point

    return iec_rail_point(page, rail, x, hot, ret)


def _iec_columns(session: Any, sheet_id: str) -> bool:
    from panelcad.core.ladder import is_iec_column_sheet

    try:
        return is_iec_column_sheet(session.sheet(sheet_id))
    except KeyError:
        return False


_IO_POWER_Y = 16.0
_IO_FIRST_Y = 72.0
_IO_PITCH = 36.0


def _io_pin_label(pin_id: str) -> str:
    key = pin_id.strip().lower()
    if key in {"lplus", "l+"}:
        return "L+"
    if key == "m":
        return "M"
    if "_" in pin_id:
        return pin_id.replace("_", ".")
    return pin_id


def _io_pin_kind(pin: Any) -> str:
    elec = str(getattr(pin, "electrical", "") or "").lower()
    pid = str(pin.id).lower()
    if elec == "power" or pid in {"lplus", "l+", "m", "com", "24v", "0v"}:
        return "power"
    if elec == "output" or pid.startswith(("q", "do", "y")):
        return "output"
    if elec == "input" or pid.startswith(("i", "di", "x")):
        return "input"
    return "other"


def layout_plc_io(session: Any, sheet_id: str, extra: dict[str, Any]) -> None:
    """One rung per PLC input. Power and the coil leave on the box edge, not through it."""

    plc: _Part | None = extra.get("_plc")
    relay: _Part | None = extra.get("_relay")
    if plc is None:
        return
    left, right = rail_xs(session.sheet(sheet_id))
    _wx, wy, _ww, _wh = working_rect(session.sheet(sheet_id))
    pdev = _device(session, plc.library_id)
    pw, _ph = _size(pdev)
    by_id = {p.id: p for p in pdev.pins}
    power = [p for p in pdev.pins if _io_pin_kind(p) == "power"]
    in_pins = [p for p in pdev.pins if _io_pin_kind(p) == "input"]
    out_pins = [p for p in pdev.pins if _io_pin_kind(p) == "output"]
    wired_in = [str(pin_id) for _part, pin_id in (extra.get("_io_inputs") or [])]
    wired_out = str(extra.get("io_out") or "Q0_0")
    ordered_in: list[Any] = []
    for pid in wired_in:
        pin = by_id.get(pid)
        if pin is not None and pin not in ordered_in:
            ordered_in.append(pin)
    for pin in in_pins:
        if pin not in ordered_in:
            ordered_in.append(pin)
    ordered_out: list[Any] = []
    if wired_out in by_id:
        ordered_out.append(by_id[wired_out])
    for pin in out_pins:
        if pin not in ordered_out:
            ordered_out.append(pin)
    rows = max(len(ordered_in), len(ordered_out), 1)
    row_y = [_IO_FIRST_Y + i * _IO_PITCH for i in range(rows)]
    left_power = [p for p in power if str(p.id).lower() not in {"m"}]
    right_power = [p for p in power if str(p.id).lower() in {"m"}]
    if power and not left_power:
        left_power = power[:1]
        right_power = power[1:]
    power_bottom = _IO_POWER_Y + 8.0 * max(len(left_power), len(right_power), 1)
    if row_y[0] < power_bottom + 20.0:
        shift = power_bottom + 20.0 - row_y[0]
        row_y = [y + shift for y in row_y]
    pin_map: dict[str, tuple[float, float, str]] = {}
    for i, pin in enumerate(left_power):
        pin_map[pin.id] = (0.0, _IO_POWER_Y + i * 10.0, "left")
    for i, pin in enumerate(right_power):
        pin_map[pin.id] = (pw, _IO_POWER_Y + i * 10.0, "right")
    for i, pin in enumerate(ordered_in):
        pin_map[pin.id] = (0.0, row_y[i], "left")
    for i, pin in enumerate(ordered_out):
        pin_map[pin.id] = (pw, row_y[min(i, len(row_y) - 1)], "right")
    card_h = max(row_y[-1] + 22.0, max((spec[1] for spec in pin_map.values()), default=0.0) + 16.0)
    for pin in pdev.pins:
        if pin.id not in pin_map:
            pin_map[pin.id] = (pw * 0.5, card_h - 4.0, "bottom")
    plc.rotation = 0
    plc.scale = 1.0
    plc.x = left + 128.0
    plc.y = wy + 14.0
    plc.properties = {
        **plc.properties,
        "ladder_card": True,
        "card_h": card_h,
        "pin_labels": {pid: _io_pin_label(pid) for pid in pin_map},
    }
    for part, pin_id in extra.get("_io_inputs") or []:
        local = pin_map.get(str(pin_id))
        if local is None:
            continue
        cdev = _device(session, part.library_id)
        right_pin = next((p for p in cdev.pins if p.side == "right"), cdev.pins[-1])
        part.align_pin = right_pin.id
        part.rotation = 0
        part.scale = 1.0
        cw, _ch = _size(cdev)
        part.x = left + RAIL_PAD
        if part.x + cw > plc.x - 18.0:
            part.x = plc.x - cw - 20.0
        part.y = plc.y + local[1] - float(right_pin.y)
        part.properties = {**part.properties, "tag_below": True}
    extra["_plc_pin_map"] = pin_map
    if relay is not None:
        rdev = _device(session, relay.library_id)
        rw, rh = _size(rdev)
        q_local = pin_map.get(wired_out) or (pw, row_y[0], "right")
        relay.rotation = 0
        relay.scale = 1.0
        relay.properties = {**relay.properties, "ladder_coil": True}
        relay.y = plc.y + q_local[1] - rh / 2.0
        relay.x = plc.x + pw + 28.0
        if relay.x + rw > right - 40.0:
            relay.x = right - 40.0 - rw
        extra["_relay_coil_pins"] = {
            "A1": (0.0, rh / 2.0, "left"),
            "A2": (float(rw), rh / 2.0, "right"),
        }


def apply_plc_io_pins(session: Any, sheet_id: str, extra: dict[str, Any]) -> None:
    """Move PLC and coil pins onto the box edge so each rung is its own wire."""

    from panelcad.core.models import PinOverride

    page = session.sheet(sheet_id)
    plc: _Part | None = extra.get("_plc")
    if plc is not None and plc.ref:
        try:
            comp = page.component(plc.ref)
        except KeyError:
            comp = None
        if comp is not None:
            known = {p.id for p in session.catalog.get(comp.library_id).pins}
            for pid, spec in (extra.get("_plc_pin_map") or {}).items():
                if pid not in known:
                    continue
                x, y, side = spec
                comp.pin_overrides[pid] = PinOverride(x=float(x), y=float(y), side=side)
    relay: _Part | None = extra.get("_relay")
    if relay is None or not relay.ref:
        return
    try:
        rcomp = page.component(relay.ref)
    except KeyError:
        return
    known = {p.id for p in session.catalog.get(rcomp.library_id).pins}
    for pid, spec in (extra.get("_relay_coil_pins") or {}).items():
        if pid not in known:
            continue
        x, y, side = spec
        rcomp.pin_overrides[pid] = PinOverride(x=float(x), y=float(y), side=side)


def coil_a1_nets(session: Any, sheet_id: str) -> list[tuple[str, str]]:
    """(coil_ref, net on A1) for every coil-like symbol on the sheet."""

    sheet = session.sheet(sheet_id)
    found: list[tuple[str, str]] = []
    for comp in sheet.components:
        lib = comp.library_id.lower()
        if not any(token in lib for token in ("coil", "contactors.coil", "relays.", "timers.", "pilot")):
            if "coil" not in lib:
                continue
        for wire in sheet.wires:
            for end in (wire.frm, wire.to):
                if end == f"{comp.id}.A1" or end == f"{comp.id}.X1":
                    found.append((comp.id, str(wire.net or "")))
    return found
