"""IEC column layout for Circuit Builder control and PLC I/O sheets.

Power sheets stay schematic. Control sheets are vertical current paths:
L+ (or X1) on the top rail, 0V (or X2) on the bottom, IEC 60617 symbols
rotated so each contact stands in a column. The PLC I/O sheet uses that
same pair so the card common is the control return. X1/X2 are not the
supply phases L1/L2/L3.
"""

from __future__ import annotations

from typing import Any

from panelcad.core.circuit_builder import _Part, _device
from panelcad.core.ladder import iec_rail_ys
from panelcad.render.titleblock import working_rect

GAP_Y = 5.0
BODY_GAP = 3.0
FIRST_DROP = 8.0


def iec_rail_pair(extra: dict[str, Any], *, io: bool = False) -> tuple[str, str]:
    """24 V control is L+/0V. AC control (100 V and up) is X1/X2.

    The I/O sheet uses the same pair. A separate L+/M rail never joined the
    control common, and on an AC job those rails had no source. The PLC pin
    is still M; the wire lands on this return. X1/X2 match the NEMA control
    rails so a 230 V secondary is not the same net as supply phase L1.
    """

    del io
    from panelcad.core.circuit_ladder import _rail_hint

    ctrl = _rail_hint(extra)
    if ctrl is not None and float(ctrl) >= 100:
        return "X1", "X2"
    return "L+", "0V"


def prepare_iec_sheet(
    session: Any,
    sheet_id: str,
    extra: dict[str, Any],
    *,
    io: bool = False,
) -> tuple[str, str]:
    hot, ret = iec_rail_pair(extra, io=io)
    sheet = session.sheet(sheet_id)
    sheet.drawing_style = "iec"
    sheet.standard = "IEC"
    sheet.rail_hot = hot
    sheet.rail_return = ret
    from panelcad.core.circuit_ladder import store_control_voltage

    store_control_voltage(session, sheet, extra)
    return hot, ret


def layout_iec_control(
    session: Any,
    sheet_id: str,
    parts: list[_Part],
    extra: dict[str, Any],
    key: str = "_rungs",
) -> None:
    if key == "_io_rungs":
        extra["_relay"] = next((p for p in parts if p.role == "relay"), None)
        extra["_plc"] = next((p for p in parts if p.role == "plc"), extra.get("_plc"))
        layout_plc_io_iec(session, sheet_id, extra)
        return
    for part in parts:
        if part.role == "estop":
            part.properties = {**part.properties, "hidden_pins": ["21", "22"]}
    _place_supply(session, sheet_id, parts)
    _layout_columns(session, sheet_id, extra.get(key) or [], parts)


def _place_supply(session: Any, sheet_id: str, parts: list[_Part]) -> None:
    supply = next((p for p in parts if p.role in {"psu", "transformer"}), None)
    if supply is None:
        return
    wx, wy, _ww, _wh = working_rect(session.sheet(sheet_id))
    supply.rotation = 0
    supply.scale = 1.0
    device = _dev(session, supply)
    # L/N/PE take a short off-page arrow on the left. The old wx+8 left no room.
    has_primary = any(pin.id in {"L", "N", "PE"} for pin in device.pins)
    supply.x = wx + (30.0 if has_primary else 8.0)
    # Clear of the L+ rail. The tag sits above the body.
    supply.y = wy + 26.0


def _dev(session: Any, part: _Part):
    return _device(session, part.library_id)


def _in_out_pins(session: Any, part: _Part):
    device = _dev(session, part)
    align = part.align_pin if any(p.id == part.align_pin for p in device.pins) else ""
    incoming = device.pin(align) if align else next(
        (p for p in device.pins if p.side == "left"), device.pins[0]
    )
    outgoing = next(
        (p for p in device.pins if p.side == "right" and p.id != incoming.id),
        None,
    )
    if outgoing is None:
        outgoing = next((p for p in device.pins if p.id != incoming.id), incoming)
    return device, incoming, outgoing


def _place_pin(part: _Part, pin: Any, column_x: float, pin_y: float) -> None:
    """Rotation 90: the named pin sits at (column_x, pin_y)."""

    scale = float(part.scale or 1.0)
    part.rotation = 90
    part.x = float(column_x) + float(pin.y) * scale
    part.y = float(pin_y) - float(pin.x) * scale


def _world(part: _Part, pin: Any) -> tuple[float, float]:
    scale = float(part.scale or 1.0)
    if int(part.rotation or 0) == 90:
        return (part.x - float(pin.y) * scale, part.y + float(pin.x) * scale)
    if int(part.rotation or 0) == 270:
        return (part.x + float(pin.y) * scale, part.y - float(pin.x) * scale)
    return (part.x + float(pin.x) * scale, part.y + float(pin.y) * scale)


def _box(session: Any, part: _Part) -> tuple[float, float, float, float]:
    device = _dev(session, part)
    scale = float(part.scale or 1.0)
    width = float(device.schematic_width) * scale
    height = float(device.schematic_height) * scale
    if int(part.rotation or 0) == 90:
        return (part.x - height, part.y, part.x, part.y + width)
    if int(part.rotation or 0) == 270:
        return (part.x, part.y - width, part.x + height, part.y)
    return (part.x, part.y, part.x + width, part.y + height)


def _hits(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    gap: float = BODY_GAP,
) -> bool:
    return not (a[2] + gap <= b[0] or b[2] + gap <= a[0] or a[3] + gap <= b[1] or b[3] + gap <= a[1])


def _shift(parts: list[_Part], dx: float) -> None:
    for part in parts:
        part.x += dx


def _push_clear(
    session: Any,
    group: list[_Part],
    occupied: list[tuple[float, float, float, float]],
    *,
    direction: float = 1.0,
) -> None:
    """Slide a placed group until its bodies clear what is already on the sheet."""

    for _ in range(24):
        dx = 0.0
        for part in group:
            box = _box(session, part)
            for other in occupied:
                if not _hits(box, other):
                    continue
                if direction >= 0:
                    need = other[2] + BODY_GAP - box[0]
                else:
                    need = other[0] - BODY_GAP - box[2]
                if direction >= 0 and need > dx:
                    dx = need
                elif direction < 0 and need < dx:
                    dx = need
        if abs(dx) < 0.2:
            return
        _shift(group, dx)


def _stack(
    session: Any,
    items: list[_Part],
    column_x: float,
    start_y: float,
) -> list[_Part]:
    """Drop contacts down one column. Each in-pin shares X with the previous out-pin."""

    y = float(start_y)
    prev_x = float(column_x)
    placed: list[_Part] = []
    for index, part in enumerate(items):
        device, incoming, outgoing = _in_out_pins(session, part)
        if index:
            y = y + GAP_Y
        _place_pin(part, incoming, prev_x, y)
        ox, oy = _world(part, outgoing)
        prev_x = ox
        y = oy
        placed.append(part)
    return placed


def _place_seal(session: Any, seal: _Part, under: _Part) -> None:
    """Parallel column: in-pins share Y, out-pins share Y, body to the right of the button."""

    _udev, u_in, u_out = _in_out_pins(session, under)
    sdev, s_in, s_out = _in_out_pins(session, seal)
    ux0, uy0 = _world(under, u_in)
    _ux1, uy1 = _world(under, u_out)
    span = uy1 - uy0
    raw = float(s_out.x) - float(s_in.x)
    scale = span / raw if raw > 0.5 and span > 0.5 else 1.0
    seal.scale = max(0.35, min(2.5, scale))
    under_box = _box(session, under)
    left_over = (float(sdev.schematic_height) - float(s_in.y)) * float(seal.scale)
    column = under_box[2] + BODY_GAP + left_over
    _place_pin(seal, s_in, column, uy0)
    # The default tag is left of the seal, which is the operator circle.
    seal.properties = {**seal.properties, "ref_side": "below"}


def _layout_columns(
    session: Any,
    sheet_id: str,
    rungs: list[dict[str, Any]],
    parts: list[_Part],
) -> None:
    sheet = session.sheet(sheet_id)
    wx, _wy, ww, _wh = working_rect(sheet)
    top_rail, bot_rail = iec_rail_ys(sheet)
    right_limit = wx + ww - 14.0
    occupied: list[tuple[float, float, float, float]] = []
    for part in parts:
        if part.role in {"psu", "transformer"}:
            occupied.append(_box(session, part))
    placed: set[int] = set()
    # Leave a gutter so an Auto contact can sit left of the first column.
    cursor = wx + 118.0

    def _remember(group: list[_Part]) -> None:
        for part in group:
            occupied.append(_box(session, part))
            placed.add(id(part))

    for rung in rungs:
        contacts = [p for p in (rung.get("contacts") or []) if p.role not in {"psu", "transformer"}]
        fresh = [p for p in contacts if id(p) not in placed]
        output = rung.get("output")
        seal = rung.get("seal")
        under = rung.get("seal_under")
        if rung.get("join") is not None and rung.get("tap_out") and fresh:
            group = _place_auto_branch(session, rung, fresh, top_rail)
            _push_clear(session, group, occupied, direction=-1.0)
            _keep_on_sheet(session, group, wx + 4.0, right_limit)
            _remember(group)
            continue
        reused = [p for p in contacts if id(p) in placed]
        column_items: list[_Part] = []
        if reused and fresh:
            tap = rung.get("tap") if rung.get("tap") is not None else reused[-1]
            _tdev, _tin, tout = _in_out_pins(session, tap)
            pin = tout
            if rung.get("tap_out"):
                pin = _dev(session, tap).pin(str(rung["tap_out"]))
            _tx, ty = _world(tap, pin)
            column_items = list(fresh)
            if output is not None and id(output) not in placed:
                column_items.append(output)
            group = _stack(session, column_items, cursor, ty)
        else:
            column_items = list(fresh)
            if output is not None and id(output) not in placed:
                column_items.append(output)
            if not column_items:
                continue
            group = _stack(session, column_items, cursor, top_rail + FIRST_DROP)
        if seal is not None and under is not None and id(seal) not in placed:
            _place_seal(session, seal, under)
            group = [*group, seal]
        _push_clear(session, group, occupied, direction=1.0)
        _keep_on_sheet(session, group, wx + 4.0, right_limit)
        if occupied or group:
            edges = [_box(session, part)[2] for part in group]
            cursor = max(edges) + 6.0 if edges else cursor
        _remember(group)


def _place_auto_branch(
    session: Any,
    rung: dict[str, Any],
    fresh: list[_Part],
    top_rail: float,
) -> list[_Part]:
    """CR (and any extra contacts) hang off the selector Auto pin, level with the start out pin."""

    tap = rung.get("tap")
    join = rung.get("join")
    group: list[_Part] = []
    if tap is None or join is None:
        return _stack(session, fresh, top_rail, top_rail + FIRST_DROP)
    tap_dev = _dev(session, tap)
    tap_pin = tap_dev.pin(str(rung.get("tap_out")))
    _jdev, _jin, jout = _in_out_pins(session, join)
    tx, ty = _world(tap, tap_pin)
    _jx, jy = _world(join, jout)
    span = jy - ty
    if span < 8.0:
        span = 28.0
    tap_box = _box(session, tap)
    for part in fresh:
        device, incoming, outgoing = _in_out_pins(session, part)
        raw = float(outgoing.x) - float(incoming.x)
        scale = span / raw if raw > 0.5 else 1.0
        part.scale = max(0.35, min(2.5, scale))
        right_over = float(incoming.y) * float(part.scale)
        column = min(tx - 16.0, tap_box[0] - BODY_GAP - right_over)
        _place_pin(part, incoming, column, ty)
        group.append(part)
    return group


def _keep_on_sheet(
    session: Any,
    group: list[_Part],
    left_limit: float,
    right_limit: float,
) -> None:
    if not group:
        return
    boxes = [_box(session, part) for part in group]
    left = min(box[0] for box in boxes)
    right = max(box[2] for box in boxes)
    dx = 0.0
    if right > right_limit:
        dx -= right - right_limit
    if left + dx < left_limit:
        dx += left_limit - (left + dx)
    if abs(dx) > 0.2:
        _shift(group, dx)


def layout_plc_io_iec(session: Any, sheet_id: str, extra: dict[str, Any]) -> None:
    """Each PLC input is its own column. L+ drops from the top rail; M returns at the bottom."""

    from panelcad.core.circuit_ladder import _io_pin_label

    plc: _Part | None = extra.get("_plc")
    relay: _Part | None = extra.get("_relay")
    if plc is None:
        return
    sheet = session.sheet(sheet_id)
    wx, _wy, ww, _wh = working_rect(sheet)
    top_rail, bot_rail = iec_rail_ys(sheet)
    pdev = _dev(session, plc)
    pw, ph = float(pdev.schematic_width), float(pdev.schematic_height)
    by_id = {p.id: p for p in pdev.pins}
    wired = [str(pin_id) for _part, pin_id in (extra.get("_io_inputs") or [])]
    ordered = [by_id[pid] for pid in wired if pid in by_id]
    if not ordered:
        ordered = [p for p in pdev.pins if str(p.id).upper().startswith("I")]
    pitch = 28.0
    origin = 32.0
    pin_map: dict[str, tuple[float, float, str]] = {}
    if "Lplus" in by_id:
        pin_map["Lplus"] = (8.0, 0.0, "top")
    for index, pin in enumerate(ordered):
        pin_map[pin.id] = (origin + index * pitch, 0.0, "top")
    card_h = ph
    if "M" in by_id:
        pin_map["M"] = (8.0, card_h, "bottom")
    wired_out = str(extra.get("io_out") or "Q0_0")
    if wired_out in by_id:
        pin_map[wired_out] = (pw, card_h * 0.42, "right")
    plc.rotation = 0
    plc.scale = 1.0
    plc.x = wx + min(150.0, max(90.0, ww * 0.32))
    plc.y = top_rail + 46.0
    if plc.y + card_h > bot_rail - 20.0:
        plc.y = max(top_rail + 36.0, bot_rail - 20.0 - card_h)
    plc.properties = {
        **plc.properties,
        "ladder_card": True,
        "card_h": card_h,
        "pin_labels": {pid: _io_pin_label(pid) for pid in pin_map},
    }
    extra["_plc_pin_map"] = pin_map
    for part, pin_id in extra.get("_io_inputs") or []:
        local = pin_map.get(str(pin_id))
        if local is None:
            continue
        device, incoming, outgoing = _in_out_pins(session, part)
        span = float(outgoing.x) - float(incoming.x)
        part.scale = 1.0
        column = plc.x + float(local[0])
        out_y = plc.y - 3.0
        in_y = out_y - max(span, 8.0)
        if in_y < top_rail + 6.0:
            in_y = top_rail + 6.0
            out_y = in_y + max(span, 8.0)
        _place_pin(part, incoming, column, in_y)
    if relay is None:
        return
    rdev = _dev(session, relay)
    rw, rh = float(rdev.schematic_width), float(rdev.schematic_height)
    q_local = pin_map.get(wired_out) or (pw, card_h * 0.42, "right")
    relay.rotation = 0
    relay.scale = 1.0
    relay.properties = {**relay.properties, "ladder_coil": True}
    relay.x = plc.x + float(q_local[0]) + 16.0
    relay.y = plc.y + float(q_local[1])
    if relay.x + rw > wx + ww - 8.0:
        relay.x = wx + ww - 8.0 - rw
    if relay.y + rh > bot_rail - 10.0:
        relay.y = bot_rail - 10.0 - rh
    extra["_relay_coil_pins"] = {
        "A1": (rw * 0.5, 0.0, "top"),
        "A2": (rw * 0.5, rh, "bottom"),
    }


def attach_primary_references(session: Any, control_sheet_id: str) -> None:
    """Short off-page arrows on a PSU's L, N, and PE.

    The control sheet does not draw the primary circuit. L and N point back
    at the incoming supply's sheet and column. PE points at the PE terminal.
    """

    page = session.sheet(control_sheet_id)
    if str(getattr(page, "drawing_style", "") or "") != "iec":
        return
    supply = next((comp for comp in page.components if ".psu_" in comp.library_id or comp.library_id.endswith(".psu_24v_5a")), None)
    found = _incoming_supply(session, control_sheet_id)
    if found is None:
        return
    power_sheet, line_x, breaker = found
    pe = _ensure_pe_terminal(session, power_sheet, breaker)
    if supply is None:
        # Shared supply stays on the earlier sheet. The IEC power sheet still
        # gets an IEC earth; there is no local PSU to hang L/N/PE arrows on.
        return
    if any(str(comp.properties.get("role") or "") == "supply_ref" for comp in page.components):
        return
    try:
        device = session.catalog.get(supply.library_id)
    except KeyError:
        return
    pins = {pin.id: pin for pin in device.pins}
    if not {"L", "N", "PE"} <= set(pins):
        return
    from panelcad.core.zones import column_from_header, sheet_number
    from panelcad.render.sheet import pin_world

    supply_no = sheet_number(session.project.sheets, power_sheet.id) or 1
    supply_col = column_from_header(power_sheet, line_x)
    pe_x, _pe_y = pin_world(pe, session.catalog.get(pe.library_id).pin("A"))
    pe_no = sheet_number(session.project.sheets, power_sheet.id) or 1
    pe_col = column_from_header(power_sheet, pe_x)
    prefix = supply.library_id.split(".", 1)[0]
    arrow_lib = f"{prefix}.connection.offpage_ref"
    try:
        arrow = session.catalog.get(arrow_lib)
    except KeyError:
        return
    calls = (
        ("L", f"/{supply_no}.{supply_col}"),
        ("N", f"/{supply_no}.{supply_col}"),
        ("PE", f"PE /{pe_no}.{pe_col}"),
    )
    for pin_id, callout in calls:
        px, py = pin_world(supply, pins[pin_id])
        pin = arrow.pin("A")
        ref = session.next_ref(arrow_lib, page.id)
        session.place(
            arrow_lib,
            ref=ref,
            x=px - 1.8 - float(pin.x),
            y=py - float(pin.y),
            sheet_id=page.id,
            snap=False,
            properties={
                "role": "supply_ref",
                "hide_tag": True,
                "hidden_pins": ["A"],
                "callout": callout,
            },
        )
        item = session.wire(f"{ref}.A", f"{supply.id}.{pin_id}", sheet_id=page.id)
        if hasattr(item, "number"):
            item.number = ""
            item.label = ""
            item.net = None
            item.number_locked = True


def _incoming_supply(session: Any, control_sheet_id: str):
    """(sheet, line-pin x, breaker) for the power-sheet incoming device.

    An IEC control sheet prefers an IEC breaker. The first device in sheet
    order may be a NEMA breaker left from an earlier circuit.
    """

    from panelcad.render.sheet import pin_world

    page = session.sheet(control_sheet_id)
    style = str(getattr(page, "drawing_style", "") or "")
    standard = str(getattr(page, "standard", "") or "").upper()
    want = "iec." if style == "iec" or standard == "IEC" else ""
    found: list[tuple[Any, float, Any]] = []
    for sheet in session.project.sheets:
        if sheet.id == control_sheet_id or sheet.kind != "schematic":
            continue
        for comp in sheet.components:
            role = str(comp.properties.get("role") or "")
            lib = comp.library_id.lower()
            if role not in {"breaker", "disconnect"} and "breaker" not in lib and "disconnect" not in lib:
                continue
            try:
                device = session.catalog.get(comp.library_id)
            except KeyError:
                continue
            tops = [pin for pin in device.pins if (pin.side or "") == "top"]
            pin = tops[0] if tops else (device.pins[0] if device.pins else None)
            if pin is None:
                continue
            found.append((sheet, pin_world(comp, pin)[0], comp))
    if want:
        for item in found:
            if item[2].library_id.lower().startswith(want):
                return item
    return found[0] if found else None


def _ensure_pe_terminal(session: Any, sheet: Any, breaker: Any):
    """Earth symbol on the power sheet, left of the incoming device."""

    for comp in sheet.components:
        if str(comp.properties.get("role") or "") == "pe_bar" or comp.library_id.endswith(".connection.ground"):
            return comp
    library_id = _ground_library_id(session, sheet, breaker)
    device = session.catalog.get(library_id)
    from panelcad.render.titleblock import working_rect

    wx, _wy, _ww, _wh = working_rect(sheet)
    used = {comp.id for page in session.project.sheets for comp in page.components}
    if "PE" in used:
        ref = session.next_ref(library_id, sheet.id)
    else:
        ref = "PE"
    return session.place(
        library_id,
        ref=ref,
        x=max(wx + 1.5, float(breaker.x) - float(device.schematic_width) - 6.0),
        y=float(breaker.y) + 8.0,
        sheet_id=sheet.id,
        snap=False,
        properties={"role": "pe_bar", "hidden_pins": ["A"]},
    )


def _ground_library_id(session: Any, sheet: Any, breaker: Any) -> str:
    """Earth symbol for this drawing. A NEMA breaker has no ground part."""

    standard = str(getattr(sheet, "standard", "") or "").upper()
    style = str(getattr(sheet, "drawing_style", "") or "")
    prefix = str(getattr(breaker, "library_id", "") or "iec").split(".", 1)[0]
    ordered: list[str] = []
    if standard == "IEC" or style == "iec":
        ordered.append("iec.connection.ground")
    if prefix:
        ordered.append(f"{prefix}.connection.ground")
    ordered.append("iec.connection.ground")
    seen: set[str] = set()
    for library_id in ordered:
        if library_id in seen:
            continue
        seen.add(library_id)
        try:
            session.catalog.get(library_id)
        except KeyError:
            continue
        return library_id
    raise KeyError("unknown library_id 'connection.ground'")
