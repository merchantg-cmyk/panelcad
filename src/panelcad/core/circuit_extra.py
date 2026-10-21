"""Layouts and wiring for reversing, star-delta, soft-starter, and PLC motor inserts."""

from __future__ import annotations

from typing import Any

from panelcad.core.circuit_builder import (
    CLEAR,
    GAP,
    _CONTACTOR_IN,
    _CONTACTOR_OUT,
    _LINE_NETS,
    _LOAD_NETS,
    _Part,
    _by_role,
    _device,
    _one,
    _pin_x,
    _pin_y,
    _size,
    _wire_dol,
    _wire_vfd,
)
from panelcad.core.circuit_fla import delta_ol_setpoint

# Overload in the windings (after KM, before the motor starts). Set to 0.58 × FLA.
STAR_DELTA_OL_NOTE = (
    "Overload is in the motor windings (IEC / common wye-delta practice), "
    "not in the line. Set the overload to 0.58 × nameplate FLA (winding current). "
    "Line-side overload at 1.0 × FLA is the usual NEMA alternative."
)


def layout_extra(
    session: Any,
    symbols: Any,
    *,
    kind: str,
    spec: dict[str, Any] | None,
    phases: int,
    branch: str,
    line_contactor: bool,
    control: str,
    rating_kw: float,
    x: float,
    y: float,
    extra: dict[str, Any],
) -> list[_Part]:
    if kind == "reversing":
        return _layout_reversing(session, symbols, branch, rating_kw, x, y, control, extra)
    if kind == "star_delta":
        return _layout_star_delta(session, symbols, branch, rating_kw, x, y, control, extra)
    if kind == "soft_starter":
        return _layout_soft_starter(session, symbols, branch, rating_kw, x, y, control, extra)
    if kind == "plc_motor":
        return _layout_plc_motor(
            session,
            symbols,
            spec=spec,
            phases=phases,
            branch=branch,
            line_contactor=line_contactor,
            control=control,
            rating_kw=rating_kw,
            x=x,
            y=y,
            extra=extra,
        )
    raise ValueError(f"unknown kind '{kind}'")


def wire_extra(
    session: Any,
    sheet: str,
    parts: list[_Part],
    symbols: Any,
    *,
    kind: str,
    spec: dict[str, Any] | None,
    phases: int,
    branch: str,
    connect: Any,
    end: Any,
    control_sheet: str | None,
    extra: dict[str, Any],
) -> None:
    extra["_power_sheet"] = sheet
    if kind == "reversing":
        _wire_reversing(parts, branch, symbols, connect, end)
        _wire_control_rungs(session, parts, symbols, connect, end, control_sheet, extra)
        return
    if kind == "star_delta":
        _wire_star_delta(parts, branch, symbols, connect, end, extra)
        _wire_control_rungs(session, parts, symbols, connect, end, control_sheet, extra)
        return
    if kind == "soft_starter":
        _wire_soft_starter(parts, branch, symbols, connect, end)
        _wire_control_rungs(session, parts, symbols, connect, end, control_sheet, extra)
        return
    if kind == "plc_motor":
        starter = extra.get("starter") or "dol"
        if starter == "vfd":
            assert spec is not None
            _wire_vfd(parts, spec, phases, branch, symbols.pins, connect, end)
        else:
            _wire_dol(parts, branch, symbols.pins, connect, end)
        extra = dict(extra)
        extra["spec"] = spec
        _wire_control_rungs(session, parts, symbols, connect, end, control_sheet, extra)
        _wire_plc_io(session, parts, symbols, extra, connect, end, control_sheet, sheet)
        return
    raise ValueError(f"unknown kind '{kind}'")


def _branch(
    session: Any, symbols: Any, branch: str, rating_kw: float, x: float, y: float
) -> tuple[list[_Part], float, float]:
    pins = symbols.pins
    library_id = symbols.branch(3, branch, rating_kw)
    device = _device(session, library_id)
    _w, height = _size(device)
    align = symbols.branch_align(branch)
    role = "disconnect" if branch == "disconnect" else "breaker"
    part = _Part(library_id, role, x, y, align_pin=align)
    anchor = x + _pin_x(device, align)
    return [part], anchor, y + height + GAP


def _place_aligned(
    session: Any,
    library_id: str,
    role: str,
    align_pin: str,
    anchor_x: float,
    y: float,
    **kwargs: Any,
) -> tuple[_Part, float]:
    device = _device(session, library_id)
    part = _Part(
        library_id,
        role,
        anchor_x - _pin_x(device, align_pin),
        y,
        align_pin=align_pin,
        **kwargs,
    )
    return part, y + _size(device)[1] + GAP


def _beside(session: Any, left: _Part, library_id: str, role: str, align_pin: str, **kwargs: Any) -> _Part:
    device = _device(session, library_id)
    width, _h = _size(_device(session, left.library_id))
    return _Part(
        library_id,
        role,
        left.x + width + GAP,
        left.y,
        align_pin=align_pin,
        **kwargs,
    )


def dol_control_parts(symbols: Any, extra: dict[str, Any]) -> list[_Part]:
    """3-wire DOL ladder: supply, E-stop, stop, start with seal, OL NC, coil.

    Same sheet machinery as star-delta. The OL contact is after the start/seal
    pair so the seal cannot hold the coil in with the overload open.
    """

    pins = symbols.pins
    supply, supply_role, hot, _ret = _supply_spec(symbols, extra)
    psu = _ctrl(supply, supply_role, hot, properties=_primary_props(supply_role, extra))
    estop = _ctrl(symbols.estop, "estop", pins.estop_in)
    stop = _ctrl(symbols.pb_nc, "stop", pins.stop_in)
    start = _ctrl(symbols.pb_no, "start", pins.start_in)
    olc = _ctrl(
        symbols.ol_contact,
        "ol_contact",
        pins.ol_nc_in,
        family="overload",
        suffix="-95",
        skip_layout=True,
    )
    coil = _ctrl(symbols.coil, "coil", "A1", family="contactor", suffix="", skip_layout=True)
    seal = _ctrl(
        symbols.aux_no,
        "seal",
        pins.aux_no_in,
        family="contactor",
        suffix="-13",
        skip_layout=True,
    )
    extra["_rungs"] = [
        {
            "contacts": [estop, stop, start, olc],
            "output": coil,
            "seal": seal,
            "seal_under": start,
            "out_net": "RUN",
        }
    ]
    return [psu, estop, stop, start, olc, coil, seal]


def _supply_spec(symbols: Any, extra: dict[str, Any]) -> tuple[str, str, str, str]:
    ctrl = extra.get("control_voltage")
    if ctrl is not None and float(ctrl) >= 100:
        return symbols.transformer, "transformer", symbols.pins.cpt_hot, symbols.pins.cpt_ret
    return symbols.psu, "psu", symbols.pins.psu_plus, symbols.pins.psu_minus


def _primary_props(role: str, extra: dict[str, Any]) -> dict[str, Any]:
    """A control transformer is fed at line voltage; record it so the primary jumpers can be drawn."""
    volts = extra.get("voltage")
    if role != "transformer" or volts is None:
        return {}
    return {"primary_voltage": f"{float(volts):g} V"}


def _ctrl(
    library_id: str,
    role: str,
    pin: str,
    page: str = "control",
    **kwargs: Any,
) -> _Part:
    return _Part(library_id, role, 40.0, 22.0, align_pin=pin, page=page, **kwargs)


def arrange_control_ladder(
    session: Any,
    sheet_id: str,
    parts: list[_Part],
    extra: dict[str, Any],
    key: str = "_rungs",
) -> None:
    from panelcad.core.ladder import is_iec_column_sheet

    if is_iec_column_sheet(session.sheet(sheet_id)):
        from panelcad.core.circuit_iec import layout_iec_control

        layout_iec_control(session, sheet_id, parts, extra, key)
        return
    from panelcad.core.circuit_ladder import layout_plc_io, layout_rungs, supply_above_rails

    if key == "_io_rungs":
        extra["_relay"] = next((p for p in parts if p.role == "relay"), None)
        extra["_plc"] = next((p for p in parts if p.role == "plc"), extra.get("_plc"))
        layout_plc_io(session, sheet_id, extra)
        return
    supply_above_rails(session, sheet_id, parts)
    layout_rungs(session, sheet_id, extra.get(key) or [], parts)


def _layout_reversing(
    session: Any,
    symbols: Any,
    branch: str,
    rating_kw: float,
    x: float,
    y: float,
    control: str,
    extra: dict[str, Any],
) -> list[_Part]:
    parts, anchor, cursor = _branch(session, symbols, branch, rating_kw, x, y)
    coil = symbols.contactor(rating_kw)
    interlock = {"interlock": "mechanical+electrical"}
    fwd, cursor = _place_aligned(
        session, coil, "fwd", "L1", anchor, cursor, properties=dict(interlock)
    )
    parts.append(fwd)
    rev = _beside(session, fwd, coil, "rev", "L1", properties=dict(interlock))
    # Room for a readable L1/L3 crossover on the line side of M2.
    rev.x += 52.0
    # Each tee hops the other poles. Three lanes at one grid, two millimetres
    # clear of each device, need a taller bay than the pin dots alone leave.
    # The breaker moves down so the supply tail and 1/3/5 stay in the frame.
    # The two bays give that headroom back so the motor does not drop.
    line_gap = 7.6
    load_gap = 7.6
    motor_gap = 0.9
    coil_h = _size(_device(session, coil))[1]
    coil_w = _size(_device(session, coil))[0]
    breaker = parts[0]
    breaker_h = _size(_device(session, breaker.library_id))[1]
    try:
        from panelcad.render.titleblock import working_rect

        _wx, wy, _ww, _wh = working_rect(session.sheet())
        breaker.y = wy + 3.8
    except (KeyError, AttributeError, ValueError):
        breaker.y = 18.8
    fwd.y = breaker.y + breaker_h + line_gap
    rev.y = fwd.y
    parts.append(rev)
    ol_h = _size(_device(session, symbols.overload))[1]
    cursor = fwd.y + coil_h + load_gap
    ol, cursor = _place_aligned(session, symbols.overload, "overload", symbols.pins.ol_align, anchor, cursor)
    parts.append(ol)
    motor_h = _size(_device(session, symbols.motor))[1]
    cursor = ol.y + ol_h + motor_gap
    motor, _cursor = _place_aligned(session, symbols.motor, "motor", symbols.pins.motor_align, anchor, cursor)
    parts.append(motor)

    def _park(part: _Part, dx: float, height: float) -> None:
        part.properties = {**part.properties, "ref_dx": dx, "ref_dy": height * 0.45 + 2.5}

    _park(breaker, -14.0, breaker_h)
    _park(fwd, -14.0, coil_h)
    _park(rev, coil_w + 2.0, coil_h)
    _park(ol, -14.0, ol_h)
    _park(motor, -16.0, motor_h)
    if control != "none":
        parts.extend(_reversing_control(session, symbols, extra))
    return parts


def _reversing_control(session: Any, symbols: Any, extra: dict[str, Any]) -> list[_Part]:
    pins = symbols.pins
    supply, supply_role, hot, _ret = _supply_spec(symbols, extra)
    psu = _ctrl(supply, supply_role, hot, properties=_primary_props(supply_role, extra))
    estop = _ctrl(symbols.estop, "estop", pins.estop_in)
    stop = _ctrl(symbols.pb_nc, "stop", pins.stop_in)
    olc = _ctrl(symbols.ol_contact, "ol_contact", pins.ol_nc_in, family="overload", suffix="-95", skip_layout=True)
    fwd_pb = _ctrl(symbols.pb_no, "fwd_pb", pins.start_in)
    rev_ilock = _ctrl(symbols.aux_nc, "rev_ilock", pins.aux_nc_in, family="rev", suffix="-21", skip_layout=True)
    fwd_coil = _ctrl(symbols.coil, "fwd_coil", "A1", family="fwd", suffix="", skip_layout=True)
    fwd_seal = _ctrl(symbols.aux_no, "fwd_seal", pins.aux_no_in, family="fwd", suffix="-13", skip_layout=True)
    rev_pb = _ctrl(symbols.pb_no, "rev_pb", pins.start_in)
    fwd_ilock = _ctrl(symbols.aux_nc, "fwd_ilock", pins.aux_nc_in, family="fwd", suffix="-21", skip_layout=True)
    rev_coil = _ctrl(symbols.coil, "rev_coil", "A1", family="rev", suffix="", skip_layout=True)
    rev_seal = _ctrl(symbols.aux_no, "rev_seal", pins.aux_no_in, family="rev", suffix="-13", skip_layout=True)
    parts = [psu, estop, stop, olc, fwd_pb, rev_ilock, fwd_coil, fwd_seal, rev_pb, fwd_ilock, rev_coil, rev_seal]
    extra["_rungs"] = [
        {
            "contacts": [estop, stop, olc, fwd_pb, rev_ilock],
            "output": fwd_coil,
            "seal": fwd_seal,
            "seal_under": fwd_pb,
            "out_net": "FWD",
        },
        {
            "contacts": [estop, stop, olc, rev_pb, fwd_ilock],
            "output": rev_coil,
            "seal": rev_seal,
            "seal_under": rev_pb,
            "tap": olc,
            "out_net": "REV",
        },
    ]
    if extra.get("pilot_lights"):
        run_props: dict[str, Any] = {}
        run_suffix = "-14"
        if symbols.standard == "IEC":
            run_suffix = "-23"
            run_props = {"terminals": "23-24", "pin_labels": {"13": "23", "14": "24"}}
        fwd_run = _ctrl(
            symbols.aux_no,
            "fwd_run_aux",
            pins.aux_no_in,
            family="fwd",
            suffix=run_suffix,
            skip_layout=True,
            properties=run_props,
        )
        rev_run = _ctrl(
            symbols.aux_no,
            "rev_run_aux",
            pins.aux_no_in,
            family="rev",
            suffix=run_suffix,
            skip_layout=True,
            properties=dict(run_props),
        )
        pl_f = _ctrl(symbols.pilot, "pilot_fwd", pins.pilot_in)
        # H2's tag would otherwise sit in the gap, against H1's lamp.
        pl_r = _ctrl(symbols.pilot, "pilot_rev", pins.pilot_in, properties={"ref_side": "right"})
        parts.extend([fwd_run, rev_run, pl_f, pl_r])
        extra["_rungs"].extend(
            [
                {"contacts": [fwd_run], "output": pl_f, "output_pin": pins.pilot_in, "out_net": "LT_FWD"},
                {"contacts": [rev_run], "output": pl_r, "output_pin": pins.pilot_in, "out_net": "LT_REV"},
            ]
        )
    return parts


def _layout_star_delta(
    session: Any,
    symbols: Any,
    branch: str,
    rating_kw: float,
    x: float,
    y: float,
    control: str,
    extra: dict[str, Any],
) -> list[_Part]:
    parts, anchor, cursor = _branch(session, symbols, branch, rating_kw, x, y)
    coil = symbols.contactor(rating_kw)
    fla = extra.get("fla")
    placement = extra.get("overload_placement") or "windings"
    if placement == "line":
        ol_set = float(fla) if fla is not None else None
        ol_props = {
            "placement": "line",
            "setting_note": (
                "Overload is in the line (NEMA alternative). Set to 1.0 × nameplate FLA."
            ),
        }
        if ol_set is not None:
            ol_props["ol_setpoint"] = ol_set
            ol_props["ol_factor"] = 1.0
        ol, cursor = _place_aligned(
            session, symbols.overload, "overload", symbols.pins.ol_align, anchor, cursor, properties=ol_props
        )
        parts.append(ol)
        main, cursor = _place_aligned(
            session, coil, "main", "L1", anchor, cursor, properties={"interlock": "star-delta"}
        )
        parts.append(main)
        ahead = main
    else:
        ol_set = delta_ol_setpoint(float(fla)) if fla is not None else None
        ol_props = {
            "placement": "delta_windings",
            "setting_note": STAR_DELTA_OL_NOTE,
        }
        if ol_set is not None:
            ol_props["ol_setpoint"] = ol_set
            ol_props["ol_factor"] = 0.58
        main, cursor = _place_aligned(
            session, coil, "main", "L1", anchor, cursor, properties={"interlock": "star-delta"}
        )
        parts.append(main)
        ol, cursor = _place_aligned(
            session, symbols.overload, "overload", symbols.pins.ol_align, anchor, cursor, properties=ol_props
        )
        parts.append(ol)
        ahead = ol
    # Textbook column: Q1, K1, F1, motor centered under that column.
    # K2 (delta) and K3 (star) sit side by side at F1's height, to its right,
    # so the winding ends step across a short shelf into their line pins.
    from panelcad.render.titleblock import working_rect

    host = ahead
    motor_dev = _device(session, symbols.motor_6lead)
    motor_w, motor_h = _size(motor_dev)
    coil_w, coil_h = _size(_device(session, coil))
    try:
        _wx, wy, _ww, wh = working_rect(session.sheet())
        y_lo = wy + 3.8
        y_hi = wy + wh - 0.8
    except (KeyError, AttributeError, ValueError):
        y_lo, y_hi = 18.8, 248.2
    column = [part for part in parts if part.role in {"breaker", "disconnect", "main", "overload"}]
    column.sort(key=lambda part: (part.y, part.x))
    # Park the breaker tag beside the body so the column can start at the
    # drawing edge. The motor crack is where each delta tie hops the phase
    # feed and needs a full grid between lanes, so it stays tall.
    for part in parts:
        if part.role in {"breaker", "disconnect", "overload"}:
            height = _size(_device(session, part.library_id))[1]
            # Left of the body. The default corner tag sits on K1's load
            # numbers where the column steps into the overload.
            part.properties = {
                **part.properties,
                "ref_dx": -14.0,
                "ref_dy": height * 0.42 + 2.5,
            }
    gaps = [2.4 for _ in range(len(column) - 1)]
    motor_gap = 8.2

    def _bottom(gaps_now: list[float], motor_gap_now: float) -> float:
        cursor_y = y_lo
        for index, part in enumerate(column):
            cursor_y += _size(_device(session, part.library_id))[1]
            if index < len(gaps_now):
                cursor_y += gaps_now[index]
        return cursor_y + motor_gap_now + motor_h

    for _ in range(28):
        if _bottom(gaps, motor_gap) <= y_hi:
            break
        shrunk = False
        for index, gap in enumerate(gaps):
            if gap > 1.6:
                gaps[index] = round(gap - 0.2, 2)
                shrunk = True
                break
        if shrunk:
            continue
        if motor_gap > 8.0:
            motor_gap = round(motor_gap - 0.2, 2)
            continue
        break
    cursor_y = y_lo
    for index, part in enumerate(column):
        part.y = cursor_y
        cursor_y += _size(_device(session, part.library_id))[1]
        if index < len(gaps):
            cursor_y += gaps[index]
    motor = _Part(
        symbols.motor_6lead,
        "motor",
        anchor - _pin_x(motor_dev, symbols.pins.motor_align),
        cursor_y + motor_gap,
        align_pin=symbols.pins.motor_align,
        properties={"ref_dx": -16.0, "ref_dy": 22.0},
    )
    parts.append(motor)
    host_w = _size(_device(session, host.library_id))[0]
    # Bay wide enough for the tie rises, the motor-lead ducks, and one
    # contact table beside F1. The table is wider than the old 24 mm crack,
    # and the column below K1 is already the overload and the motor.
    pair_x = max(host.x + host_w, motor.x + motor_w) + 40.0
    delta = _Part(
        coil,
        "delta",
        pair_x,
        host.y,
        align_pin="L1",
        properties={"interlock": "mechanical+electrical"},
    )
    parts.append(delta)
    star = _Part(
        coil,
        "star",
        pair_x + coil_w + 16.0,
        host.y,
        align_pin="L1",
        properties={"interlock": "mechanical+electrical"},
    )
    if star.y + coil_h > y_hi:
        star.y = max(host.y, y_hi - coil_h)
        delta.y = star.y
    parts.append(star)
    if control != "none":
        parts.extend(_star_delta_control(session, symbols, extra))
    return parts


def _star_delta_control(session: Any, symbols: Any, extra: dict[str, Any]) -> list[_Part]:
    pins = symbols.pins
    supply, supply_role, hot, _ret = _supply_spec(symbols, extra)
    psu = _ctrl(supply, supply_role, hot, properties=_primary_props(supply_role, extra))
    estop = _ctrl(symbols.estop, "estop", pins.estop_in)
    stop = _ctrl(symbols.pb_nc, "stop", pins.stop_in)
    olc = _ctrl(symbols.ol_contact, "ol_contact", pins.ol_nc_in, family="overload", suffix="-95", skip_layout=True)
    start = _ctrl(symbols.pb_no, "start", pins.start_in)
    main_coil = _ctrl(symbols.coil, "main_coil", "A1", family="main", suffix="", skip_layout=True)
    main_seal = _ctrl(symbols.aux_no, "main_seal", pins.aux_no_in, family="main", suffix="-13", skip_layout=True)
    # The seal already prints 13-14. The timer aux is the next pair, as on the soft starter.
    tr_props: dict[str, Any] = {}
    tr_suffix = "-14"
    if symbols.standard == "IEC":
        tr_suffix = "-23"
        tr_props = {"terminals": "23-24", "pin_labels": {"13": "23", "14": "24"}}
    main_tr = _ctrl(
        symbols.aux_no,
        "main_aux_tr",
        pins.aux_no_in,
        family="main",
        suffix=tr_suffix,
        skip_layout=True,
        properties=tr_props,
    )
    timer_s = extra.get("timer_seconds")
    if timer_s is None:
        timer_s = 6.0
    timer_s = float(timer_s)
    timer = _ctrl(
        symbols.timer,
        "timer",
        "A1",
        properties={
            "function": "on-delay",
            "timer_seconds": timer_s,
            "setting": f"{timer_s:g} s",
            "notes": f"Star run, {timer_s:g} s on-delay",
            # Timed contacts are their own symbols. The coil pins would print
            # a second 15 and 18 beside A2.
            "hidden_pins": ["15", "18"],
        },
    )
    nc_suffix = "-21"
    no_suffix = "-15"
    nc_props: dict[str, Any] = {}
    no_props: dict[str, Any] = {}
    if symbols.standard == "IEC":
        # On-delay timed contacts: 15-16 NC (star) and 15-18 NO (delta).
        nc_suffix = "-16"
        no_suffix = "-18"
        nc_props = {
            "terminals": "15-16",
            "pin_labels": {"21": "15", "22": "16"},
            "delayed": "NC",
        }
        no_props = {
            "terminals": "15-18",
            "pin_labels": {"13": "15", "14": "18"},
            "delayed": "NO",
        }
    tr_nc = _ctrl(
        symbols.aux_nc,
        "tr_nc",
        pins.aux_nc_in,
        family="timer",
        suffix=nc_suffix,
        skip_layout=True,
        properties=nc_props,
    )
    tr_no = _ctrl(
        symbols.aux_no,
        "tr_no",
        pins.aux_no_in,
        family="timer",
        suffix=no_suffix,
        skip_layout=True,
        properties=no_props,
    )
    delta_ilock = _ctrl(symbols.aux_nc, "delta_ilock", pins.aux_nc_in, family="delta", suffix="-21", skip_layout=True)
    star_ilock = _ctrl(symbols.aux_nc, "star_ilock", pins.aux_nc_in, family="star", suffix="-21", skip_layout=True)
    star_coil = _ctrl(symbols.coil, "star_coil", "A1", family="star", suffix="", skip_layout=True)
    delta_coil = _ctrl(symbols.coil, "delta_coil", "A1", family="delta", suffix="", skip_layout=True)
    parts = [
        psu, estop, stop, olc, start, main_coil, main_seal, main_tr, timer,
        tr_nc, tr_no, delta_ilock, star_ilock, star_coil, delta_coil,
    ]
    extra["_rungs"] = [
        {
            "contacts": [estop, stop, olc, start],
            "output": main_coil,
            "seal": main_seal,
            "seal_under": start,
            "out_net": "RUN",
        },
        {"contacts": [main_tr], "output": timer, "out_net": "TR"},
        {
            "contacts": [main_tr, tr_nc, delta_ilock],
            "output": star_coil,
            "tap": main_tr,
            "out_net": "STAR",
        },
        {
            "contacts": [main_tr, tr_no, star_ilock],
            "output": delta_coil,
            "tap": main_tr,
            "out_net": "DELTA",
        },
    ]
    if extra.get("pilot_lights"):
        pl = _ctrl(symbols.pilot, "pilot_run", pins.pilot_in)
        run_aux = _ctrl(symbols.aux_no, "main_run_aux", pins.aux_no_in, family="main", suffix="-15", skip_layout=True)
        parts.extend([run_aux, pl])
        extra["_rungs"].append(
            {"contacts": [run_aux], "output": pl, "output_pin": pins.pilot_in, "out_net": "LT_RUN"}
        )
    return parts


def _layout_soft_starter(
    session: Any,
    symbols: Any,
    branch: str,
    rating_kw: float,
    x: float,
    y: float,
    control: str,
    extra: dict[str, Any],
) -> list[_Part]:
    parts, anchor, cursor = _branch(session, symbols, branch, rating_kw, x, y)
    ss, cursor = _place_aligned(session, symbols.softstarter, "softstarter", "L1", anchor, cursor)
    parts.append(ss)
    if extra.get("bypass"):
        bypass = _beside(
            session,
            ss,
            symbols.contactor(rating_kw),
            "bypass",
            "L1",
            properties={"function_label": "Bypass", "notes": "Bypass contactor across the soft starter"},
        )
        parts.append(bypass)
        cursor = max(cursor, bypass.y + _size(_device(session, bypass.library_id))[1] + GAP)
    motor, _c = _place_aligned(
        session,
        symbols.motor,
        "motor",
        symbols.pins.motor_align,
        anchor,
        cursor,
        properties={"description": "Motor, overload: soft starter internal"},
    )
    parts.append(motor)
    if control != "none":
        parts.extend(_soft_starter_control(session, symbols, extra))
    return parts


def _soft_starter_control(session: Any, symbols: Any, extra: dict[str, Any]) -> list[_Part]:
    pins = symbols.pins
    supply, supply_role, hot, _ret = _supply_spec(symbols, extra)
    wiring = extra.get("control_wiring") or "3wire"
    psu = _ctrl(supply, supply_role, hot, properties=_primary_props(supply_role, extra))
    estop = _ctrl(symbols.estop, "estop", pins.estop_in)
    coil = _ctrl(symbols.coil, "ss_coil", "A1", family="softstarter", suffix="", skip_layout=True)
    parts = [psu, estop, coil]
    extra["_rungs"] = []
    if wiring == "2wire":
        sel = _ctrl(
            symbols.selector,
            "selector",
            pins.selector_com,
            properties={"notes": "2-wire maintained run command"},
        )
        parts.append(sel)
        extra["_rungs"].append({"contacts": [estop, sel], "output": coil})
    else:
        stop = _ctrl(symbols.pb_nc, "stop", pins.stop_in)
        start = _ctrl(symbols.pb_no, "start", pins.start_in)
        seal = _ctrl(symbols.aux_no, "ss_seal", pins.aux_no_in, family="softstarter", suffix="-13", skip_layout=True)
        parts.extend([stop, start, seal])
        extra["_rungs"].append(
            {"contacts": [estop, stop, start], "output": coil, "seal": seal, "seal_under": start}
        )
    if extra.get("bypass"):
        run_props: dict[str, Any] = {}
        if symbols.standard == "IEC":
            # The seal already uses 13-14. The run aux is the next pair.
            run_props = {"terminals": "23-24", "pin_labels": {"13": "23", "14": "24"}}
        run_aux = _ctrl(
            symbols.aux_no,
            "ss_run_aux",
            pins.aux_no_in,
            family="softstarter",
            suffix="-23" if symbols.standard == "IEC" else "-14",
            skip_layout=True,
            properties=run_props,
        )
        byp = _ctrl(
            symbols.coil,
            "bypass_coil",
            "A1",
            family="bypass",
            suffix="",
            skip_layout=True,
            properties={"function_label": "Bypass", "notes": "Bypass contactor coil"},
        )
        parts.extend([run_aux, byp])
        extra["_rungs"].append({"contacts": [run_aux], "output": byp})
    if extra.get("pilot_lights"):
        pl_run = _ctrl(symbols.pilot, "pilot_run", pins.pilot_in)
        pl_flt = _ctrl(symbols.pilot, "pilot_fault", pins.pilot_in)
        run_lt = _ctrl(symbols.aux_no, "ss_run_lt", pins.aux_no_in, family="softstarter", suffix="-15", skip_layout=True)
        flt = _ctrl(symbols.ol_contact, "ss_fault", pins.ol_nc_in, family="softstarter", suffix="-95", skip_layout=True)
        parts.extend([run_lt, flt, pl_run, pl_flt])
        extra["_rungs"].extend(
            [
                {"contacts": [run_lt], "output": pl_run, "output_pin": pins.pilot_in, "out_net": "LT_RUN"},
                {"contacts": [flt], "output": pl_flt, "output_pin": pins.pilot_in, "out_net": "LT_FLT"},
            ]
        )
    return parts


def _layout_plc_motor(
    session: Any,
    symbols: Any,
    *,
    spec: dict[str, Any] | None,
    phases: int,
    branch: str,
    line_contactor: bool,
    control: str,
    rating_kw: float,
    x: float,
    y: float,
    extra: dict[str, Any],
) -> list[_Part]:
    starter = extra.get("starter") or "dol"
    parts, anchor, cursor = _branch(session, symbols, branch, rating_kw, x, y)
    if starter == "vfd":
        assert spec is not None
        if line_contactor:
            cont, cursor = _place_aligned(session, symbols.contactor(rating_kw), "contactor", "L1", anchor, cursor)
            parts.append(cont)
        drive, cursor = _place_aligned(session, str(spec["library_id"]), "drive", str(spec["line"][0]), anchor, cursor)
        parts.append(drive)
        motor, _c = _place_aligned(session, symbols.motor, "motor", symbols.pins.motor_align, anchor, cursor)
        parts.append(motor)
    else:
        cont, cursor = _place_aligned(session, symbols.contactor(rating_kw), "contactor", "L1", anchor, cursor)
        parts.append(cont)
        ol, cursor = _place_aligned(session, symbols.overload, "overload", symbols.pins.ol_align, anchor, cursor)
        parts.append(ol)
        motor, _c = _place_aligned(session, symbols.motor, "motor", symbols.pins.motor_align, anchor, cursor)
        parts.append(motor)
    if control != "none":
        parts.extend(_plc_control(session, symbols, extra, starter=starter))
    return parts


def _plc_control(session: Any, symbols: Any, extra: dict[str, Any], *, starter: str) -> list[_Part]:
    pins = symbols.pins
    supply, supply_role, hot, _ret = _supply_spec(symbols, extra)
    dol = starter != "vfd"
    hoa_on = extra.get("hoa", True)
    io_run = extra.get("io_run") or "I0_0"
    io_ol = extra.get("io_ol") or "I0_1"
    io_auto = extra.get("io_auto") or "I0_2"
    psu = _ctrl(supply, supply_role, hot, properties=_primary_props(supply_role, extra))
    estop = _ctrl(symbols.estop, "estop", pins.estop_in)
    stop = _ctrl(symbols.pb_nc, "stop", pins.stop_in)
    hoa = None
    hoa_auto_fb = None
    if hoa_on:
        hoa = _ctrl(
            symbols.selector,
            "hoa",
            pins.selector_com,
            properties={"pin_labels": {pins.selector_hand: "H", pins.selector_auto: "A"}},
        )
        hoa_auto_fb = _ctrl(
            symbols.aux_no,
            "hoa_auto_fb",
            pins.aux_no_in,
            family="hoa",
            suffix="-14",
            skip_layout=True,
            page="io",
        )
    start = _ctrl(symbols.pb_no, "start", pins.start_in)
    cr_no = _ctrl(symbols.aux_no, "cr_no", pins.aux_no_in, family="relay", suffix="-13", skip_layout=True)
    parts = [psu, estop, stop, start, cr_no]
    if hoa is not None:
        parts.insert(3, hoa)
    extra["_rungs"] = []
    coil = None
    seal = None
    if dol:
        olc = _ctrl(symbols.ol_contact, "ol_contact", pins.ol_nc_in, family="overload", suffix="-95", skip_layout=True)
        coil = _ctrl(symbols.coil, "starter_coil", "A1", family="contactor", suffix="", skip_layout=True)
        seal = _ctrl(symbols.aux_no, "run_seal", pins.aux_no_in, family="contactor", suffix="-13", skip_layout=True)
        parts.extend([olc, coil, seal])
        hand = [estop, stop, olc]
        if hoa is not None:
            hand.append(hoa)
        hand.append(start)
        extra["_rungs"].append(
            {
                "contacts": hand,
                "output": coil,
                "seal": seal,
                "seal_under": start,
                "out_net": "HAND",
            }
        )
        auto_contacts = [cr_no]
        auto_tap = None
        auto_pin = None
        if hoa is not None:
            auto_contacts = [hoa, cr_no]
            auto_tap = hoa
            auto_pin = pins.selector_auto
        extra["_rungs"].append(
            {
                "contacts": auto_contacts,
                "output": coil,
                "tap": auto_tap,
                "tap_out": auto_pin,
                "join": start,
                "out_net": "AUTO",
            }
        )
    else:
        hand = [estop, stop]
        if hoa is not None:
            hand.append(hoa)
        hand.append(start)
        extra["_rungs"].append(
            {
                "contacts": hand,
                "out_net": "HAND",
                "sink_role": "drive",
            }
        )
        auto_contacts = [cr_no]
        auto_tap = None
        auto_pin = None
        if hoa is not None:
            auto_contacts = [hoa, cr_no]
            auto_tap = hoa
            auto_pin = pins.selector_auto
        extra["_rungs"].append(
            {
                "contacts": auto_contacts,
                "tap": auto_tap,
                "tap_out": auto_pin,
                "out_net": "AUTO",
                "sink_role": "drive",
            }
        )
    plc_lib = extra.get("plc_model") or symbols.plc
    plc = _ctrl(str(plc_lib), "plc", "Lplus", page="io")
    relay = _ctrl(symbols.relay, "relay", "A1", page="io")
    run_fb = _ctrl(
        symbols.aux_no, "run_fb", pins.aux_no_in, family="contactor", suffix="-14", skip_layout=True, page="io"
    )
    ol_sense = _ctrl(
        symbols.ol_contact, "ol_sense", pins.ol_nc_in, family="overload", suffix="-96", skip_layout=True, page="io"
    )
    parts.extend([plc, relay, run_fb, ol_sense])
    extra["_plc"] = plc
    extra["_io_rungs"] = [
        {"contacts": [run_fb], "output": relay, "output_pin": "A1"},
        {"contacts": [ol_sense], "output": relay, "output_pin": "A1"},
    ]
    extra["_io_inputs"] = [
        (run_fb, io_run),
        (ol_sense, io_ol),
    ]
    if hoa_auto_fb is not None:
        parts.append(hoa_auto_fb)
        extra["_io_rungs"].append({"contacts": [hoa_auto_fb], "output": relay, "output_pin": "A1"})
        extra["_io_inputs"].append((hoa_auto_fb, io_auto))
    if extra.get("pilot_lights") and coil is not None and seal is not None:
        pl = _ctrl(symbols.pilot, "pilot_run", pins.pilot_in)
        parts.append(pl)
        extra["_rungs"].append(
            {"contacts": [seal], "output": pl, "output_pin": pins.pilot_in, "out_net": "LT_RUN"}
        )
    return parts


def _hot_ret(parts: list[_Part], symbols: Any) -> tuple[_Part, str, str]:
    pins = symbols.pins
    if _by_role(parts, "transformer"):
        return _one(parts, "transformer"), pins.cpt_hot, pins.cpt_ret
    return _one(parts, "psu"), pins.psu_plus, pins.psu_minus


def _c(
    connect: Any,
    end: Any,
    a: _Part,
    pa: str,
    b: _Part,
    pb: str,
    net: str,
    sheet: str | None,
    to_sheet: str | None = None,
) -> None:
    connect(end(a, pa), end(b, pb), net, sheet_id=sheet, to_sheet=to_sheet)


def _wire_reversing(parts: list[_Part], branch: str, symbols: Any, connect: Any, end: Any) -> None:
    pins = symbols.pins
    branch_part = _one(parts, "disconnect" if branch == "disconnect" else "breaker")
    fwd = _one(parts, "fwd")
    rev = _one(parts, "rev")
    overload = _one(parts, "overload")
    motor = _one(parts, "motor")
    outs = pins.disc_out if branch == "disconnect" else pins.branch_out
    for src, dst, net in zip(outs, _CONTACTOR_IN, _LINE_NETS, strict=True):
        connect(end(branch_part, src), end(fwd, dst), net)
    # REV swaps L1/L3 on the line side so the crossover is drawn above M2.
    rev_in = (_CONTACTOR_IN[2], _CONTACTOR_IN[1], _CONTACTOR_IN[0])
    for src, dst, net in zip(outs, rev_in, _LINE_NETS, strict=True):
        connect(end(branch_part, src), end(rev, dst), net)
    for src, dst, net in zip(_CONTACTOR_OUT, pins.ol_in, _LOAD_NETS, strict=True):
        connect(end(fwd, src), end(overload, dst), net)
        connect(end(rev, src), end(overload, dst), net)
    for src, dst, net in zip(pins.ol_out, pins.motor, pins.motor_nets, strict=True):
        connect(end(overload, src), end(motor, dst), net)


def _wire_control_rungs(
    session: Any,
    parts: list[_Part],
    symbols: Any,
    connect: Any,
    end: Any,
    sheet: str | None,
    extra: dict[str, Any],
    key: str = "_rungs",
) -> None:
    from panelcad.core.circuit_ladder import rail_pair, wire_rung, wire_supply_to_rails

    if not sheet:
        return
    page = session.sheet(sheet)
    hot = str(getattr(page, "rail_hot", "") or "").strip()
    ret = str(getattr(page, "rail_return", "") or "").strip()
    if not hot or not ret:
        hot, ret = rail_pair(extra)
    wire_supply_to_rails(
        connect,
        end,
        parts,
        symbols,
        sheet=sheet,
        hot=hot,
        ret=ret,
        session=session,
        external=extra.get("supply_part"),
    )
    pins = symbols.pins
    for i, rung in enumerate(extra.get(key) or []):
        output = rung.get("output")
        sink = None
        sink_sheet = None
        if output is None and rung.get("sink_role") and _by_role(parts, str(rung["sink_role"])):
            drive = _one(parts, str(rung["sink_role"]))
            spec = extra.get("spec") or {}
            sink_pin = str(rung.get("sink_pin") or spec.get("run") or "DI1")
            sink = end(drive, sink_pin)
            sink_sheet = extra.get("_power_sheet")
        if output is None and sink is None:
            continue
        out_pin = str(rung.get("output_pin") or "A1")
        out_ret = pins.pilot_out if out_pin == pins.pilot_in else "A2"
        if out_pin.startswith("I0"):
            out_ret = "M"
        wire_rung(
            connect,
            end,
            session,
            parts,
            pins,
            sheet=sheet,
            hot=hot,
            ret=ret,
            contacts=rung.get("contacts") or [],
            output=output,
            out_in=out_pin,
            out_ret=out_ret,
            seal=rung.get("seal"),
            seal_under=rung.get("seal_under"),
            tap=rung.get("tap"),
            tap_out=rung.get("tap_out"),
            join=rung.get("join"),
            join_pin=rung.get("join_pin"),
            out_net=str(rung.get("out_net") or f"R{i}"),
            sink=sink,
            sink_sheet=sink_sheet,
        )
    if key == "_rungs":
        from panelcad.core.circuit_iec import attach_primary_references

        attach_primary_references(session, sheet)


def _wire_star_delta(
    parts: list[_Part], branch: str, symbols: Any, connect: Any, end: Any, extra: dict[str, Any]
) -> None:
    pins = symbols.pins
    branch_part = _one(parts, "disconnect" if branch == "disconnect" else "breaker")
    main = _one(parts, "main")
    star = _one(parts, "star")
    delta = _one(parts, "delta")
    overload = _one(parts, "overload")
    motor = _one(parts, "motor")
    outs = pins.disc_out if branch == "disconnect" else pins.branch_out
    if extra.get("overload_placement") == "line":
        for src, dst, net in zip(outs, pins.ol_in, _LINE_NETS, strict=True):
            connect(end(branch_part, src), end(overload, dst), net)
        for src, dst, net in zip(pins.ol_out, _CONTACTOR_IN, _LINE_NETS, strict=True):
            connect(end(overload, src), end(main, dst), net)
        for src, dst, net in zip(_CONTACTOR_OUT, pins.motor, pins.motor_nets, strict=True):
            connect(end(main, src), end(motor, dst), net)
    else:
        for src, dst, net in zip(outs, _CONTACTOR_IN, _LINE_NETS, strict=True):
            connect(end(branch_part, src), end(main, dst), net)
        for src, dst, net in zip(_CONTACTOR_OUT, pins.ol_in, _LOAD_NETS, strict=True):
            connect(end(main, src), end(overload, dst), net)
        for src, dst, net in zip(pins.ol_out, pins.motor, pins.motor_nets, strict=True):
            connect(end(overload, src), end(motor, dst), net)
    for src, dst, net in zip(pins.motor_end, _CONTACTOR_IN, pins.motor_end_nets, strict=True):
        connect(end(motor, src), end(star, dst), net)
        connect(end(motor, src), end(delta, dst), f"{net}_D")
    # Star point: short the star-contactor load side.
    connect(end(star, "T1"), end(star, "T2"), "STAR")
    connect(end(star, "T2"), end(star, "T3"), "STAR")
    # Delta: W2–U1 / U2–V1 / V2–W1 (NEMA T6–T1 / T4–T2 / T5–T3).
    for src, dst, net in zip(_CONTACTOR_OUT, pins.motor, pins.motor_nets, strict=True):
        connect(end(delta, src), end(motor, dst), f"D_{net}")


def _wire_soft_starter(parts: list[_Part], branch: str, symbols: Any, connect: Any, end: Any) -> None:
    pins = symbols.pins
    branch_part = _one(parts, "disconnect" if branch == "disconnect" else "breaker")
    ss = _one(parts, "softstarter")
    motor = _one(parts, "motor")
    outs = pins.disc_out if branch == "disconnect" else pins.branch_out
    for src, dst, net in zip(outs, ("L1", "L2", "L3"), _LINE_NETS, strict=True):
        connect(end(branch_part, src), end(ss, dst), net)
    for src, dst, net in zip(("T1", "T2", "T3"), pins.motor, ("T1", "T2", "T3"), strict=True):
        connect(end(ss, src), end(motor, dst), net)
    if _by_role(parts, "bypass"):
        bypass = _one(parts, "bypass")
        for src, dst, net in zip(outs, _CONTACTOR_IN, _LINE_NETS, strict=True):
            connect(end(branch_part, src), end(bypass, dst), net)
        for src, dst, net in zip(_CONTACTOR_OUT, pins.motor, _LOAD_NETS, strict=True):
            connect(end(bypass, src), end(motor, dst), net)


def _wire_plc_io(
    session: Any,
    parts: list[_Part],
    symbols: Any,
    extra: dict[str, Any],
    connect: Any,
    end: Any,
    control_sheet: str | None,
    power_sheet: str | None,
) -> None:
    """PLC card: I0_0 run FB, I0_1 OL healthy, I0_2 HOA Auto; Q0_0 drives CR."""

    from panelcad.core.circuit_ladder import rail_pair

    if not _by_role(parts, "plc"):
        return
    pins = symbols.pins
    hot, ret = rail_pair(extra)
    plc = _one(parts, "plc")
    relay = _one(parts, "relay")
    io_sheet = next((p.sheet_override for p in parts if p.page == "io" and p.sheet_override), None)
    dest = io_sheet or control_sheet
    if dest:
        page = session.sheet(dest)
        sheet_hot = str(getattr(page, "rail_hot", "") or "").strip()
        sheet_ret = str(getattr(page, "rail_return", "") or "").strip()
        if sheet_hot and sheet_ret:
            hot, ret = sheet_hot, sheet_ret
    if dest:
        from panelcad.core.circuit_ladder import apply_plc_io_pins

        apply_plc_io_pins(session, dest, extra)
    io_run = extra.get("io_run") or "I0_0"
    io_ol = extra.get("io_ol") or "I0_1"
    io_auto = extra.get("io_auto") or "I0_2"
    io_out = extra.get("io_out") or "Q0_0"
    connect(hot, end(plc, "Lplus"), hot, sheet_id=dest)
    connect(end(plc, "M"), ret, ret, sheet_id=dest)
    connect(end(plc, io_out), end(relay, "A1"), io_out, sheet_id=dest)
    connect(end(relay, "A2"), ret, ret, sheet_id=dest)
    for role, pin, net in (
        ("run_fb", io_run, "RUN_FB"),
        ("ol_sense", io_ol, "OL_OK"),
        ("hoa_auto_fb", io_auto, "AUTO"),
    ):
        if not _by_role(parts, role):
            continue
        part = _one(parts, role)
        inn, out = pins.aux_no_in, pins.aux_no_out
        if "ol" in role:
            inn, out = pins.ol_nc_in, pins.ol_nc_out
        connect(hot, end(part, inn), hot, sheet_id=dest)
        connect(end(part, out), end(plc, pin), net, sheet_id=dest)
    extra["_power_sheet"] = power_sheet


# Parallel power lanes stay further apart than the 0.75 mm different-number join test.
_LANE = 1.85


def _route_star_delta_power(
    session: Any,
    sheet_id: str,
    parts: list[_Part],
    _xy: Any,
    _ends: Any,
    _lay: Any,
) -> None:
    """Short orthogonal grid under the column.

    The motor sits under K1/F1. K2 and K3 sit beside F1. Each delta tie
    drops under its own load pin, rises in the bay, and crosses the other
    phase feeds on a hop lane just above the motor. The left phase is the
    highest lane and the rightmost rise, so the ties do not cross each
    other. Winding ends run below those shelves, one grid apart. Delta
    rises in the K2–K3 gap and star rises on K3's right skirt. A run that
    has to cross a riser does it one grid above the foot, on a riser long
    enough for the hop to land in the end window. Nothing runs along the
    sheet edge.
    """

    from panelcad.core.route import component_aabb
    from panelcad.render.titleblock import working_rect

    sheet = session.sheet(sheet_id)
    boxes: dict[str, tuple[float, float, float, float]] = {}
    for comp in sheet.components:
        try:
            device = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        scale = comp.visual_scale()
        boxes[comp.id] = component_aabb(
            comp.x,
            comp.y,
            float(device.schematic_width) * scale,
            float(device.schematic_height) * scale,
            int(comp.rotation or 0),
            bool(comp.mirror),
        )
    try:
        wx, wy, ww, wh = working_rect(sheet)
        y_lo, y_hi = wy + 2.0, wy + wh - 1.2
        x_hi = wx + ww - 18.0
    except (KeyError, AttributeError, ValueError):
        y_lo, y_hi = 17.0, 247.8
        x_hi = 387.0

    star = _one(parts, "star") if _by_role(parts, "star") else None
    delta = _one(parts, "delta") if _by_role(parts, "delta") else None
    motor = _one(parts, "motor")
    mb = boxes.get(motor.ref)
    if mb is None:
        return
    mid_y = (mb[1] + mb[3]) / 2.0
    db = boxes.get(delta.ref) if delta is not None else None
    sb = boxes.get(star.ref) if star is not None else None

    def _vertical(wire: Any, ax: float, ay: float, bx: float, by: float) -> None:
        upper = (ax, ay) if ay <= by else (bx, by)
        lower = (bx, by) if upper[1] == ay else (ax, ay)
        # Two elbows. One midpoint sits on the average and the router drops it.
        _lay(wire, [(upper[0], upper[1] + 0.35), (lower[0], lower[1] - 0.35)], upper)

    for wire in sheet.wires:
        parsed = _ends(wire)
        if parsed is None:
            continue
        a_ref, a_pin, b_ref, b_pin = parsed
        if motor.ref in {a_ref, b_ref}:
            continue
        if star is not None and a_ref == b_ref == star.ref and {a_pin, b_pin} <= {"T1", "T2", "T3"}:
            continue
        ax, ay = _xy(a_ref, a_pin)
        bx, by = _xy(b_ref, b_pin)
        if abs(ax - bx) <= 1.6:
            _vertical(wire, ax, ay, bx, by)

    if star is not None:
        bus_y: float | None = None
        bridges: list[tuple[Any, float, float, float]] = []
        for wire in sheet.wires:
            parsed = _ends(wire)
            if parsed is None:
                continue
            a_ref, a_pin, b_ref, b_pin = parsed
            if not (a_ref == b_ref == star.ref and {a_pin, b_pin} <= {"T1", "T2", "T3"}):
                continue
            ax, ay = _xy(a_ref, a_pin)
            bx, _by = _xy(b_ref, b_pin)
            bus_y = max(ay, _by) if bus_y is None else max(bus_y, ay, _by)
            bridges.append((wire, ax, ay, bx))
        if bus_y is not None and sb is not None:
            bus_y = min(y_hi - 1.0, max(bus_y + 2.2, sb[3] + 2.2))
            for wire, ax, ay, bx in bridges:
                _lay(wire, [(ax, bus_y), (bx, bus_y)], (ax, ay))

    tops: list[tuple[Any, float, float, float, float, str]] = []
    ends: list[tuple[Any, float, float, float, float, str]] = []
    for wire in sheet.wires:
        parsed = _ends(wire)
        if parsed is None:
            continue
        a_ref, _a_pin, b_ref, _b_pin = parsed
        if motor.ref not in {a_ref, b_ref}:
            continue
        ax, ay = _xy(a_ref, _a_pin)
        bx, by = _xy(b_ref, _b_pin)
        if a_ref == motor.ref:
            mx, my, ox, oy, o_ref = ax, ay, bx, by, b_ref
        else:
            mx, my, ox, oy, o_ref = bx, by, ax, ay, a_ref
        # Line terminals are on the top edge. Winding ends are beside them,
        # on the right side, so a mid-height test would call W2 a line pin.
        if my <= mb[1] + 1.2:
            tops.append((wire, mx, my, ox, oy, o_ref))
        else:
            ends.append((wire, mx, my, ox, oy, o_ref))

    PITCH = 2.6

    def _dedupe(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        kept: list[tuple[float, float]] = []
        for point in points:
            if kept and abs(point[0] - kept[-1][0]) < 1e-6 and abs(point[1] - kept[-1][1]) < 1e-6:
                continue
            kept.append(point)
        return kept

    feeders: list[tuple[float, float, float]] = []
    for wire, mx, my, ox, oy, o_ref in tops:
        if delta is not None and o_ref == delta.ref:
            continue
        if abs(mx - ox) <= 1.6:
            upper = min(my, oy)
            # One stub keeps the feed a single hoppable segment down to the motor.
            _lay(wire, [(mx, upper + 0.2)], (mx, upper))
            feeders.append((mx, upper, max(my, oy)))
        else:
            jog = min(my - 1.2, max(oy, my) - 1.4)
            _lay(wire, [(ox, jog), (mx, jog)], (ox, oy))

    ties = [row for row in tops if delta is not None and row[5] == delta.ref]
    ties.sort(key=lambda item: item[1])
    by_pin: dict[tuple[float, float], list] = {}
    for row in ends:
        by_pin.setdefault((round(row[1], 1), round(row[2], 1)), []).append(row)
    pins = sorted(by_pin, key=lambda item: (item[1], item[0]))

    if ties and db is not None:
        # Leftmost feeder (index 0) takes the highest hop lane and the
        # rightmost bay rise, so its horizontal clears the shorter rises.
        # Each lower shelf stops at its own rise and never meets a taller one.
        n_ties = len(ties)
        if feeders:
            feeder_top = min(item[1] for item in feeders)
            feeder_bot = max(item[2] for item in feeders)
        else:
            feeder_top = min(item[2] for item in ties) - 7.0
            feeder_bot = min(item[2] for item in ties)
        lane0 = feeder_top + 2.25
        lanes = [lane0 + index * PITCH for index in range(n_ties)]
        # The last phase does not cross a feeder, so it may sit nearer the pin.
        if lanes[-1] > feeder_bot - 0.55:
            lanes[-1] = feeder_bot - 0.55
        bay_left = mb[2] + 1.2 + n_ties * PITCH
        bay_right = db[0] - 2.4
        if bay_right < bay_left:
            bay_right = bay_left
        # Index 0 is the rightmost rise. Lower phases stand to its left.
        if n_ties == 1:
            rises = [bay_right]
        else:
            span = bay_right - bay_left
            step = PITCH if span >= (n_ties - 1) * PITCH else span / (n_ties - 1)
            rises = [bay_right - index * step for index in range(n_ties)]
        pin_y = min(item[4] for item in ties)
        shelf0 = pin_y + 3.6
        shelves = [min(y_hi - 6.0, shelf0 + index * PITCH) for index in range(n_ties)]
        # The winding runs sit on the pin. The lowest tie shelf has to stay
        # a grid above that pin or the two read as one conductor.
        if pins:
            room = pins[0][1] - 2.55 - shelves[-1]
            if room < 0:
                shelves = [shelf + room for shelf in shelves]
        for index, (wire, mx, my, ox, oy, _ref) in enumerate(ties):
            rise = rises[index]
            lane = lanes[index]
            shelf = shelves[index]
            _lay(wire, _dedupe([(ox, shelf), (rise, shelf), (rise, lane), (mx, lane)]), (ox, oy))

    if pins and (db is not None or sb is not None):
        # One run per winding end, a grid below the tie shelves. The top end
        # (index 0) takes the highest run and the rightmost gap rise, but the
        # leftmost drop beside the motor. The lower shelves start further
        # right, so a short stub never runs beside the next phase. The top
        # run still spans the shorter rises, one grid above their feet. Each
        # rise is long enough that those hops fall inside the end window, so
        # the geometry cap still sees only the three tie hops.
        # Star leaves the same run and climbs K3's right skirt. The top
        # skirt column is the leftmost, so the lane hops sit in that same
        # end window instead of crossing in the middle of the sheet.
        n_pins = len(pins)
        duck_left = mb[2] + 2.4
        duck_right = duck_left + (n_pins - 1) * PITCH
        # Bottom end (last index) takes the rightmost drop. Its run then
        # starts to the right of the shorter stubs, so those stubs are not
        # a parallel run sitting one fraction of a grid off the shelf.
        ducks = [duck_left + index * PITCH for index in range(n_pins)]
        # The run is the pin Y, so the symbol stub and the wire match.
        # Shelves are a pin-span apart (6 mm on this motor, not two grids).
        # The riser has to be long enough that a hop that far from the end
        # still falls inside the 4% window the geometry cap ignores.
        pin_span = pins[-1][1] - pins[0][1] if len(pins) >= 2 else 0.0
        rise_len = max(2.0 * PITCH, pin_span + 0.5) / 0.039

        def _pitched(left_edge: float, right_edge: float, *, rightmost_first: bool) -> list[float]:
            if right_edge < left_edge:
                right_edge = left_edge
            if n_pins == 1:
                return [right_edge if rightmost_first else left_edge]
            span = (n_pins - 1) * PITCH
            room = right_edge - left_edge
            if room + 1e-6 < span:
                step = room / (n_pins - 1)
                left, right = left_edge, right_edge
            else:
                step = PITCH
                pad = (room - span) / 2.0
                left = left_edge + pad
                right = right_edge - pad
            if rightmost_first:
                return [right - index * step for index in range(n_pins)]
            return [left + index * step for index in range(n_pins)]

        if db is not None and sb is not None:
            gap_left, gap_right = db[2] + 1.6, sb[0] - 1.6
        elif db is not None:
            gap_left = db[2] + 1.6
            gap_right = gap_left + (n_pins - 1) * PITCH
        else:
            gap_right = sb[0] - 1.6
            gap_left = gap_right - (n_pins - 1) * PITCH
        # Top end is the rightmost gap rise, so a lower lane ends before it.
        k2_rises = _pitched(gap_left, gap_right, rightmost_first=True)
        if sb is not None:
            skirt_left = sb[2] + 1.6
            skirt_right = min(sb[2] + 8.0, x_hi)
        else:
            skirt_left = k2_rises[0] + PITCH
            skirt_right = skirt_left + (n_pins - 1) * PITCH
        # Top end is the leftmost skirt rise. Lower lanes hop the top of it.
        k3_rises = _pitched(skirt_left, skirt_right, rightmost_first=False)
        for index, key in enumerate(pins):
            rows = by_pin[key]
            mx, my = rows[0][1], rows[0][2]
            duck = ducks[index]
            # The stub is drawn at the pin. A shelf a fraction of a millimetre
            # off reads as a step (2 px, 2 px, 5 px at sheet scale).
            run = my
            lane = run - rise_len
            k2_x = k2_rises[index]
            k3_x = k3_rises[index]
            trunk = [(duck, my), (duck, run), (k2_x, run)]
            for row in rows:
                wire, _mx, _my, ox, oy, o_ref = row
                to_star = star is not None and o_ref == star.ref
                if to_star and sb is not None:
                    points = [*trunk, (k3_x, run), (k3_x, lane), (ox, lane)]
                else:
                    points = [*trunk, (k2_x, lane), (ox, lane)]
                _lay(wire, _dedupe(points), (mx, my))


def finish_power_routes(session: Any, sheet_id: str, parts: list[_Part], kind: str) -> None:
    """Keep power conductors off the motor body and show the reversing crossover."""

    from panelcad.render.sheet import parse_pin_ref, pin_world
    from panelcad.render.wiring import clear_route_cache

    sheet = session.sheet(sheet_id)
    catalog = session.catalog

    def _xy(ref: str, pin: str) -> tuple[float, float]:
        comp = sheet.component(ref)
        return pin_world(comp, catalog.get(comp.library_id).pin(pin))

    def _ends(wire: Any) -> tuple[str, str, str, str] | None:
        try:
            a_ref, a_pin = parse_pin_ref(wire.frm)
            b_ref, b_pin = parse_pin_ref(wire.to)
        except ValueError:
            return None
        return a_ref, a_pin, b_ref, b_pin

    def _lay(wire: Any, points: list[tuple[float, float]], head: tuple[float, float]) -> None:
        """Store *points* running from *head* (one pin) toward the other pin."""

        parsed = _ends(wire)
        if parsed is None:
            return
        ax, ay = _xy(parsed[0], parsed[1])
        if abs(ax - head[0]) > 1.2 or abs(ay - head[1]) > 1.2:
            points = list(reversed(points))
        session.set_wire_route(wire.id, points, sheet_id=sheet_id)

    if kind == "star_delta" and _by_role(parts, "motor"):
        _route_star_delta_power(session, sheet_id, parts, _xy, _ends, _lay)

    if kind == "reversing" and _by_role(parts, "rev"):
        rev = _one(parts, "rev")
        fwd = _one(parts, "fwd") if _by_role(parts, "fwd") else None
        # Each supply phase keeps its own horizontal in the bay under the breaker.
        # Sharing one Y put L1 on top of L3 between M1 and M2.
        line_legs: list[tuple[Any, str, float, float, float, float]] = []
        for wire in list(sheet.wires):
            parsed = _ends(wire)
            if parsed is None:
                continue
            a_ref, a_pin, b_ref, b_pin = parsed
            if rev.ref not in {a_ref, b_ref}:
                continue
            rev_pin = a_pin if a_ref == rev.ref else b_pin
            o_ref, o_pin = (b_ref, b_pin) if a_ref == rev.ref else (a_ref, a_pin)
            if o_ref == rev.ref or rev_pin not in {"L1", "L2", "L3"}:
                continue
            ox, oy = _xy(o_ref, o_pin)
            rx, ry = _xy(rev.ref, rev_pin)
            line_legs.append((wire, str(wire.net or rev_pin), ox, oy, rx, ry))
        if line_legs:
            from panelcad.core.route import component_aabb

            def _aabb(ref: str) -> tuple[float, float, float, float]:
                comp = sheet.component(ref)
                device = catalog.get(comp.library_id)
                scale = comp.visual_scale()
                return component_aabb(
                    comp.x,
                    comp.y,
                    float(device.schematic_width) * scale,
                    float(device.schematic_height) * scale,
                    int(comp.rotation or 0),
                    bool(comp.mirror),
                )

            # Leftmost supply phase takes the highest lane in the breaker gap
            # and the leftmost channel, so each hop clears the feeder end.
            # Travel lanes stay a grid apart beside M2; the drop is on the pin.
            line_legs.sort(key=lambda item: item[2])
            count = len(line_legs)
            rev_box = _aabb(rev.ref)
            fwd_box = _aabb(fwd.ref) if fwd is not None else rev_box
            pitch = 2.55
            gap_lo = fwd_box[2] + 4.0
            gap_hi = rev_box[0] - 4.0
            if count > 1 and gap_lo + (count - 1) * pitch > gap_hi:
                gap_lo = rev_box[2] + 4.0
                gap_hi = gap_lo + (count - 1) * pitch
            pin_y = min(item[3] for item in line_legs)
            body_top = min(fwd_box[1], rev_box[1])
            travel_pitch = 7.0
            travel0 = pin_y - 2.0 - (count - 1) * travel_pitch
            # The last hop used to be pulled up to the coil and land 2.45 mm
            # from the one above it. Share the bay so every gap stays a grid.
            exit_ys = [pin_y + 2.2 + index * pitch for index in range(count)]
            exit_limit = body_top - 0.4
            if count > 1 and exit_ys[-1] > exit_limit:
                step = (exit_limit - exit_ys[0]) / (count - 1)
                step = max(step, 2.5)
                exit_ys = [exit_limit - (count - 1 - index) * step for index in range(count)]
            for index, (wire, _net, ox, oy, rx, ry) in enumerate(line_legs):
                channel = gap_lo + index * pitch
                exit_y = exit_ys[index]
                travel = travel0 + index * travel_pitch
                _lay(
                    wire,
                    [(ox, exit_y), (channel, exit_y), (channel, travel), (rx, travel)],
                    (ox, oy),
                )
        if fwd is not None:
            for wire in list(sheet.wires):
                parsed = _ends(wire)
                if parsed is None:
                    continue
                a_ref, a_pin, b_ref, b_pin = parsed
                refs = {a_ref, b_ref}
                if fwd.ref not in refs or rev.ref in refs:
                    continue
                if a_pin not in {"L1", "L2", "L3"} and b_pin not in {"L1", "L2", "L3"}:
                    continue
                ax, ay = _xy(a_ref, a_pin)
                bx, by = _xy(b_ref, b_pin)
                if abs(ax - bx) <= 1.6:
                    upper = (ax, ay) if ay <= by else (bx, by)
                    _lay(wire, [(upper[0], upper[1] + 0.15)], upper)
        if _by_role(parts, "fwd") and _by_role(parts, "overload"):
            ol = _one(parts, "overload")
            legs: list[tuple[Any, float, float, float, float]] = []
            for wire in list(sheet.wires):
                parsed = _ends(wire)
                if parsed is None:
                    continue
                a_ref, a_pin, b_ref, b_pin = parsed
                refs = {a_ref, b_ref}
                if ol.ref not in refs or (fwd.ref not in refs and rev.ref not in refs):
                    continue
                ax, ay = _xy(a_ref, a_pin)
                bx, by = _xy(b_ref, b_pin)
                if abs(ax - bx) <= 1.5:
                    upper = (ax, ay) if ay <= by else (bx, by)
                    _lay(wire, [(upper[0], upper[1] + 0.15)], upper)
                    continue
                if ay <= by:
                    sx, sy, dx, dy = ax, ay, bx, by
                else:
                    sx, sy, dx, dy = bx, by, ax, ay
                legs.append((wire, sx, sy, dx, dy))
            # Leftmost overload phase is the highest lane. The run ends on
            # that phase's vertical so the other poles are hopped, not joined.
            legs.sort(key=lambda item: (item[3], item[1]))
            if legs:
                pitch = 2.55
                top = min(item[2] for item in legs)
                bot = min(item[4] for item in legs)
                bus_ys = [top + 2.2 + index * pitch for index in range(len(legs))]
                bus_limit = bot - 0.4
                if len(legs) > 1 and bus_ys[-1] > bus_limit:
                    step = (bus_limit - bus_ys[0]) / (len(legs) - 1)
                    step = max(step, 2.5)
                    bus_ys = [bus_limit - (len(legs) - 1 - index) * step for index in range(len(legs))]
                for index, (wire, sx, sy, dx, dy) in enumerate(legs):
                    bus_y = bus_ys[index]
                    _lay(wire, [(sx, bus_y), (dx, bus_y)], (sx, sy))
        if _by_role(parts, "motor") and _by_role(parts, "overload"):
            motor = _one(parts, "motor")
            ol = _one(parts, "overload")
            drops: list[tuple[Any, float, float, float, float]] = []
            for wire in list(sheet.wires):
                parsed = _ends(wire)
                if parsed is None:
                    continue
                a_ref, a_pin, b_ref, b_pin = parsed
                if {a_ref, b_ref} != {motor.ref, ol.ref}:
                    continue
                ax, ay = _xy(a_ref, a_pin)
                bx, by = _xy(b_ref, b_pin)
                if ay <= by:
                    sx, sy, dx, dy = ax, ay, bx, by
                else:
                    sx, sy, dx, dy = bx, by, ax, ay
                drops.append((wire, sx, sy, dx, dy))
            drops.sort(key=lambda item: item[1])
            for index, (wire, sx, sy, dx, dy) in enumerate(drops):
                if abs(sx - dx) <= 0.8:
                    span = dy - sy
                    if span <= 3.2:
                        # One waypoint is the average of a short drop, and the
                        # router throws it away. Two elbows stay a straight lead.
                        _lay(wire, [(sx, sy + span * 0.25), (sx, sy + span * 0.75)], (sx, sy))
                    else:
                        _lay(wire, [(sx, sy + 0.15)], (sx, sy))
                    continue
                # Stay under the motor's top-pin caption (it hangs to about pin_y - 0.6).
                shelf = min(dy - 0.35, sy + 2.6 + index * 2.6)
                _lay(wire, [(sx, shelf), (dx, shelf)], (sx, sy))

    if kind == "soft_starter" and _by_role(parts, "motor") and _by_role(parts, "bypass"):
        motor = _one(parts, "motor")
        starter = _one(parts, "softstarter")
        bypass = _one(parts, "bypass")
        starter_legs: list[tuple[Any, float, float, float, float]] = []
        bypass_legs: list[tuple[Any, float, float, float, float]] = []
        for wire in list(sheet.wires):
            parsed = _ends(wire)
            if parsed is None:
                continue
            a_ref, a_pin, b_ref, b_pin = parsed
            refs = {a_ref, b_ref}
            if motor.ref not in refs:
                continue
            ax, ay = _xy(a_ref, a_pin)
            bx, by = _xy(b_ref, b_pin)
            if ay <= by:
                sx, sy, dx, dy = ax, ay, bx, by
            else:
                sx, sy, dx, dy = bx, by, ax, ay
            if starter.ref in refs:
                starter_legs.append((wire, sx, sy, dx, dy))
            elif bypass.ref in refs:
                bypass_legs.append((wire, sx, sy, dx, dy))
        starter_legs.sort(key=lambda item: item[3])
        for index, (wire, sx, sy, dx, dy) in enumerate(starter_legs):
            if abs(sx - dx) <= 0.8:
                # NEMA motor pins sit on the soft-starter pins, so this lead is
                # vertical. Leaving it empty lets the autorouter put a corner on
                # the bypass shelf of another phase (a drawn short). Two elbows
                # above that shelf stay a straight lead. One midpoint is thrown
                # away and the autorouter runs again.
                top = sy + 0.9
                shelf = sy + 1.3 + index * 0.4
                # Bypass lanes occupy the last 2.8 mm under the motor pin.
                limit = dy - 3.6
                if shelf > limit:
                    shelf = limit
                if shelf < top + 0.5:
                    shelf = top + 0.5
                _lay(wire, [(sx, top), (dx, shelf)], (sx, sy))
                continue
            shelf = sy + 1.3 + index * 0.4
            _lay(wire, [(sx, shelf), (dx, shelf)], (sx, sy))
        bypass_legs.sort(key=lambda item: item[3])
        for index, (wire, sx, sy, dx, dy) in enumerate(bypass_legs):
            shelf = dy - 2.8 + index * 1.2
            approach = dx + 5.0
            _lay(wire, [(sx, shelf), (approach, shelf), (approach, dy)], (sx, sy))

    if kind == "vfd" and _by_role(parts, "drive"):
        # The run lead shares the drive's right-hand stub with the common.
        # A NEMA estop is short enough that the autorouter drops the run under
        # the buttons and that stub reads as a tee. An IEC estop blocks that
        # gap, so the run already goes above the row. Store that upper lane.
        drive = _one(parts, "drive")
        from panelcad.core.geometry import Point
        from panelcad.core.route import component_aabb, path_hits_obstacles
        from panelcad.render.wiring import sheet_obstacles

        def _box(ref: str) -> tuple[float, float, float, float]:
            comp = sheet.component(ref)
            device = catalog.get(comp.library_id)
            scale = comp.visual_scale()
            return component_aabb(
                comp.x,
                comp.y,
                float(device.schematic_width) * scale,
                float(device.schematic_height) * scale,
                int(comp.rotation or 0),
                bool(comp.mirror),
            )

        def _side(ref: str, pin: str) -> str:
            comp = sheet.component(ref)
            return str(catalog.get(comp.library_id).pin(pin).side)

        def _stub(x: float, y: float, side: str) -> tuple[float, float] | None:
            if side == "right":
                return (x + 6.0, y)
            if side == "left":
                return (x - 6.0, y)
            return None

        power_pins = {"L1", "L2", "L3", "U", "V", "W", "T1", "T2", "T3", "L", "N"}
        runs: list[tuple[Any, float, float, float, float, float, float]] = []
        for wire in list(sheet.wires):
            parsed = _ends(wire)
            if parsed is None:
                continue
            a_ref, a_pin, b_ref, b_pin = parsed
            if a_ref == drive.ref and a_pin not in power_pins:
                d_pin, o_ref, o_pin = a_pin, b_ref, b_pin
            elif b_ref == drive.ref and b_pin not in power_pins:
                d_pin, o_ref, o_pin = b_pin, a_ref, a_pin
            else:
                continue
            dx, dy = _xy(drive.ref, d_pin)
            ox, oy = _xy(o_ref, o_pin)
            if abs(ox - dx) < 16.0:
                continue
            d_stub = _stub(dx, dy, _side(drive.ref, d_pin))
            o_stub = _stub(ox, oy, _side(o_ref, o_pin))
            if d_stub is None or o_stub is None:
                continue
            runs.append((wire, d_stub[0], dy, o_stub[0], oy, ox, oy))
        tops = [
            _box(part.ref)[1]
            for part in parts
            if part.role in {"estop", "stop", "start", "selector"}
        ]
        if runs and tops:
            obstacles = sheet_obstacles(sheet, catalog, skip=set())
            lane = min(tops) - 2.8
            for index, (wire, stub_x, dy, out_x, pin_y, ox, oy) in enumerate(runs):
                this_lane = lane - index * 2.5
                points = [
                    Point(x=ox, y=oy),
                    Point(x=out_x, y=pin_y),
                    Point(x=out_x, y=this_lane),
                    Point(x=stub_x, y=this_lane),
                    Point(x=stub_x, y=dy),
                ]
                if path_hits_obstacles(points, obstacles):
                    continue
                _lay(
                    wire,
                    [(out_x, pin_y), (out_x, this_lane), (stub_x, this_lane), (stub_x, dy)],
                    (ox, oy),
                )
    clear_route_cache()
