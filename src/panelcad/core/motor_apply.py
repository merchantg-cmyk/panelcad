"""Apply a motor-sizing result to a Circuit Builder insert.

Library symbol ids stay on the existing kW rule (9 A / 32 A contactor, 32 A
MCB / 100 A MCCB). The sized breaker trip is stored as ``In``. When that trip
is above the symbol frame, the device records the next builtin frame and a
catalog part when one exists; the symbol is not swapped. The overload stores
the setpoint as ``setting``. A real adjustable range already on the device is
kept. A zero-width range is not written. Power conductors get the branch gauge;
other wires in the insert get 16 AWG control wire.
"""

from __future__ import annotations

from typing import Any

from panelcad.core.motor_sizing import (
    HP_TO_KW,
    feeder_ampacity,
    fmt_amps,
    is_iec_standard,
    size_motor_circuit,
)

POWER_ROLES = frozenset(
    {
        "breaker",
        "disconnect",
        "contactor",
        "overload",
        "drive",
        "motor",
        "fwd",
        "rev",
        "main",
        "star",
        "delta",
        "softstarter",
        "bypass",
    }
)

_CONTROL_PINS = {
    "A1",
    "A2",
    "13",
    "14",
    "21",
    "22",
    "31",
    "32",
    "95",
    "96",
    "97",
    "98",
    "X1",
    "X2",
    "COM",
    "DCM",
}


def prepare_sizing(
    *,
    standard: str,
    phases: int,
    hp: float | None,
    kw: float | None,
    volts: float | None,
    nameplate: float | None,
    overload_factor: float = 1.0,
    device: str = "inverse_time_breaker",
    termination_c: int | None = None,
) -> dict[str, Any]:
    """Circuit Builder entry. kW that is not a table row may use the nearest published hp."""

    return size_motor_circuit(
        hp=hp,
        kw=kw,
        volts=volts,
        phases=phases,
        fla=nameplate,
        standard=standard or ("IEC" if is_iec_standard(standard) else "NEMA/ANSI"),
        device=device,
        allow_nearest_hp=True,
        overload_factor=overload_factor,
        termination_c=termination_c,
    )


def layout_kw_for(rating_kw: float, sizing: dict[str, Any] | None) -> float:
    """Bump a small kW rating onto the 32 A contactor when the table current exceeds 9 A.

    4.01 kW stays under the 15 kW MCCB threshold, so the breaker frame does not change.
    """

    if not sizing or not sizing.get("ok") or sizing.get("code") != "NEC":
        return rating_kw
    flc = sizing.get("contactor_min_a")
    if flc is None:
        return rating_kw
    if float(flc) > 9.0 and float(rating_kw) <= 4.0:
        return 4.01
    return rating_kw


def reserve_rating_gutter(session: Any, sheet_id: str, parts: list[Any], lines_for) -> None:
    """Shift a power column right when a left-side rating caption would leave the sheet."""

    from panelcad.render.titleblock import working_rect

    if not parts:
        return
    sheet = session.sheet(sheet_id)
    wx, _wy, _ww, _wh = working_rect(sheet)
    overflow = 0.0
    for part in parts:
        for line in lines_for(part):
            width = 2.2 * 0.62 * max(1, len(line))
            left = float(part.x) - 1.4 - width
            overflow = max(overflow, wx - left)
    if overflow <= 0.2:
        return
    for part in parts:
        part.x += overflow + 0.4


def planned_rating_lines(part: Any, motor_props: dict[str, Any], sizing: dict[str, Any] | None) -> list[str]:
    """Caption text the painter will draw, used to keep that text on the sheet."""

    role = str(getattr(part, "role", "") or "")
    if role == "motor":
        return _motor_lines(motor_props)
    if not sizing or not sizing.get("ok") or sizing.get("code") != "NEC":
        return []
    if role == "breaker":
        rating = (sizing.get("ocpd") or {}).get("rating_a")
        if rating is not None:
            return [f"{fmt_amps(float(rating))} A"]
    if role == "overload":
        current = (sizing.get("overload") or {}).get("setting_a")
        if current is not None:
            return [f"{fmt_amps(float(current))} A"]
    return []


def stamp_inserted_circuit(
    session: Any,
    sizing: dict[str, Any] | None,
    motor_ref: str,
    wire_rows: list[dict[str, Any]] | None,
    parts: list[Any],
) -> None:
    """Write sizing onto the motor, breaker, overload, and the wires just created."""

    if not motor_ref or not sizing:
        return
    found = _find(session, motor_ref)
    if found is None:
        return
    _sheet, motor = found
    motor.properties["sizing_summary"] = str(sizing.get("report") or "")
    motor.properties["sizing_code"] = str(sizing.get("code") or "")
    if not sizing.get("ok") or sizing.get("code") != "NEC":
        return
    _stamp_motor(session, motor, sizing)
    by_ref: dict[str, Any] = {}
    for part in parts:
        ref = str(getattr(part, "ref", "") or "")
        if not ref:
            continue
        # A coil or aux reuses the contactor tag. Keep the power body for L/T pins.
        current = by_ref.get(ref)
        if current is None or (
            str(getattr(part, "role", "") or "") in POWER_ROLES
            and str(getattr(current, "role", "") or "") not in POWER_ROLES
        ):
            by_ref[ref] = part
    for part in parts:
        if str(getattr(part, "ref", "") or "") == motor_ref:
            continue
        other = _find(session, str(part.ref))
        if other is None:
            continue
        _stamp_device(session, other[1], part.role, sizing)
    _stamp_wires(session, wire_rows or [], by_ref, sizing)


def apply_sizing_to_motor(session: Any, motor_ref: str, sizing: dict[str, Any]) -> dict[str, Any]:
    """Re-apply a sizing result to a motor already tagged with ``sizing_group``."""

    found = _find(session, motor_ref)
    if found is None:
        raise ValueError(f"no component '{motor_ref}'")
    if not sizing.get("ok") or sizing.get("code") != "NEC":
        raise ValueError(str(sizing.get("error") or "sizing did not produce an NEC result"))
    sheet, motor = found
    group = str(motor.properties.get("sizing_group") or motor.id)
    members = []
    for page in session.project.sheets:
        for comp in page.components:
            if comp.id == motor.id or str(comp.properties.get("sizing_group") or "") == group:
                members.append(comp)
    _stamp_motor(session, motor, sizing)
    motor.properties["sizing_summary"] = str(sizing.get("report") or "")
    motor.properties["sizing_code"] = "NEC"
    for comp in members:
        if comp.id == motor.id:
            continue
        role = str(comp.properties.get("role") or "")
        _stamp_device(session, comp, role, sizing)
    power_gauge = str((sizing.get("wire") or {}).get("gauge") or "")
    control_gauge = str((sizing.get("control") or {}).get("gauge") or "16 AWG")
    member_ids = {comp.id for comp in members}
    stamped = 0
    for page in session.project.sheets:
        for wire in page.wires:
            kind = _wire_kind(wire.frm, wire.to, member_ids, members)
            if kind is None:
                continue
            gauge = power_gauge if kind == "power" else control_gauge
            if not gauge:
                continue
            _set_gauge(wire, gauge)
            stamped += 1
    return {"ref": motor_ref, "sheet": sheet.id, "wires": stamped}


def feeder_for_project(project: Any, catalog: Any) -> dict[str, Any] | None:
    """NEC 430.24 when two or more schematic motors carry a table full-load current."""

    from panelcad.core.checks import _merged_props

    currents: list[float] = []
    for sheet in project.sheets:
        if sheet.kind in {"panel_layout", "door_layout", "terminal"}:
            continue
        for comp in sheet.components:
            if not _looks_like_motor(comp):
                continue
            try:
                device = catalog.get(comp.library_id)
            except KeyError:
                device = None
            props = _merged_props(device, comp)
            raw = props.get("table_flc", props.get("flc_a"))
            if raw in (None, ""):
                continue
            try:
                currents.append(float(raw))
            except (TypeError, ValueError):
                continue
    if len(currents) < 2:
        return None
    return feeder_ampacity(currents)


def _stamp_motor(session: Any, motor: Any, sizing: dict[str, Any]) -> None:
    wire = sizing.get("wire") or {}
    ocpd = sizing.get("ocpd") or {}
    thermal = sizing.get("overload") or {}
    flc = sizing.get("flc_a")
    nameplate = sizing.get("nameplate_fla_a")
    ol_current = sizing.get("overload_current_a")
    motor.properties.update(
        {
            "table_flc": flc,
            "flc_a": flc,
            "conductor_min_a": sizing.get("conductor_min_a"),
            "wire_gauge": wire.get("gauge") or "",
            "wire_table": wire.get("table") or "nec_310_16+ul_508a_28_1",
            "wire_temp_c": wire.get("temp_c") or 60,
            "wire_governed_by": wire.get("governed_by") or "",
            "termination_c": wire.get("temp_c") or 60,
            "termination_rule": wire.get("termination_rule") or "",
            "ocpd_max_a": ocpd.get("permitted_max_a"),
            "ocpd_device": ocpd.get("device") or "",
            "ocpd_rating_a": ocpd.get("rating_a"),
            "sizing_summary": str(sizing.get("report") or ""),
            "sizing_code": "NEC",
            "phases": sizing.get("phases") or motor.properties.get("phases") or 3,
        }
    )
    if nameplate is not None:
        motor.properties["nameplate_fla"] = nameplate
        motor.properties["FLA"] = nameplate
        motor.properties["fla_source"] = "nameplate"
    elif flc is not None:
        motor.properties["FLA"] = flc
        motor.properties["fla_source"] = "nec-table"
    if ol_current is not None:
        motor.properties["ol_fla"] = ol_current
    if thermal:
        motor.properties["ol_setting_a"] = thermal.get("setting_a")
        motor.properties["ol_trip_max_a"] = thermal.get("trip_max_a")
    if sizing.get("contactor_adequate") is False:
        motor.properties["contactor_note"] = _contactor_short_note(session, sizing)
    else:
        motor.properties.pop("contactor_note", None)


_CONTACTOR_ROLES = frozenset({"contactor", "fwd", "rev", "main", "star", "delta", "bypass"})


def _stamp_device(session: Any, comp: Any, role: str, sizing: dict[str, Any]) -> None:
    if role == "breaker":
        rating = (sizing.get("ocpd") or {}).get("rating_a")
        permitted = (sizing.get("ocpd") or {}).get("permitted_max_a")
        if rating is None:
            return
        comp.properties["In"] = f"{fmt_amps(float(rating))}A"
        comp.properties["sized_by"] = "nec-430"
        comp.properties["ocpd_device"] = (sizing.get("ocpd") or {}).get("device") or ""
        comp.properties["ocpd_max_a"] = permitted
        _stamp_breaker_frame(session, comp, float(rating))
        return
    if role in _CONTACTOR_ROLES:
        _stamp_contactor(session, comp, sizing)
        return
    if role != "overload":
        return
    thermal = sizing.get("overload") or {}
    current = thermal.get("setting_a")
    if current is None:
        return
    amps = float(current)
    comp.properties["setting"] = f"{fmt_amps(amps)}A"
    comp.properties["ol_fla"] = amps
    comp.properties["sized_by"] = "nec-430"
    comp.properties["overload_rule"] = thermal.get("rule") or ""
    _stamp_overload_range(session, comp, amps)


def _stamp_breaker_frame(session: Any, comp: Any, trip_a: float) -> None:
    from panelcad.core.sizing_checks import (
        breaker_frame_message,
        library_frame_amps,
        next_breaker_symbol,
        suggest_catalog_part,
    )

    try:
        device = session.catalog.get(comp.library_id)
    except KeyError:
        device = None
    frame = library_frame_amps(device, comp.library_id)
    if frame is None:
        comp.properties.pop("frame_a", None)
        comp.properties.pop("frame_note", None)
        comp.properties.pop("suggested_library_id", None)
        comp.properties.pop("suggested_part", None)
        return
    comp.properties["frame_a"] = frame
    if trip_a <= float(frame) + 0.05:
        comp.properties.pop("frame_note", None)
        comp.properties.pop("suggested_library_id", None)
        comp.properties.pop("suggested_part", None)
        return
    nxt = next_breaker_symbol(session.catalog, comp.library_id, trip_a)
    part = suggest_catalog_part(getattr(session, "parts", None), "protection.breaker", trip_a, kind="frame")
    comp.properties["frame_note"] = breaker_frame_message(comp.id, trip_a, float(frame), comp.library_id, nxt, part)
    if nxt is not None:
        comp.properties["suggested_library_id"] = nxt[0]
    else:
        comp.properties.pop("suggested_library_id", None)
    if part is not None:
        comp.properties["suggested_part"] = f"{part.manufacturer} {part.catalog}"
    else:
        comp.properties.pop("suggested_part", None)


def _stamp_contactor(session: Any, comp: Any, sizing: dict[str, Any]) -> None:
    from panelcad.core.sizing_checks import contactor_frame_message, library_frame_amps, suggest_catalog_part

    required = sizing.get("contactor_min_a", sizing.get("flc_a"))
    if required in (None, ""):
        return
    try:
        device = session.catalog.get(comp.library_id)
    except KeyError:
        device = None
    frame = library_frame_amps(device, comp.library_id)
    if frame is None:
        return
    comp.properties["frame_a"] = frame
    if float(required) <= float(frame) + 0.05:
        comp.properties.pop("contactor_note", None)
        comp.properties.pop("suggested_part", None)
        return
    part = suggest_catalog_part(
        getattr(session, "parts", None), "switching.contactor", float(required), kind="frame"
    )
    comp.properties["contactor_note"] = contactor_frame_message(
        comp.id, float(frame), comp.library_id, float(required), part
    )
    if part is not None:
        comp.properties["suggested_part"] = f"{part.manufacturer} {part.catalog}"
    else:
        comp.properties.pop("suggested_part", None)


def _contactor_short_note(session: Any, sizing: dict[str, Any]) -> str:
    from panelcad.core.sizing_checks import part_label, suggest_catalog_part

    required = sizing.get("contactor_min_a", sizing.get("flc_a"))
    note = "Builtin contactor frame is 32 A and does not cover this full-load current."
    part = None
    if required not in (None, ""):
        part = suggest_catalog_part(
            getattr(session, "parts", None), "switching.contactor", float(required), kind="frame"
        )
    if part is not None:
        note += f" Suggested part: {part_label(part)}."
    note += " Design check contactor_undersized."
    return note


def _stamp_overload_range(session: Any, comp: Any, amps: float) -> None:
    """Keep a real covering range. Otherwise store the setpoint only.

    A selected part's range (min below max, setpoint inside) stays. The builtin
    symbol's published range is left on the library device so a merge can show
    it. A point range such as ``7.6-7.6A`` is removed.
    """

    from panelcad.core.sizing_checks import overload_suggestion, real_amp_span, suggest_catalog_part

    span = real_amp_span(comp.properties)
    if span is not None and span[0] - 1e-6 <= amps <= span[1] + 1e-6:
        comp.properties.pop("ol_library_note", None)
        comp.properties.pop("suggested_part", None)
        return
    if span is None:
        _drop_point_range(comp)
    if span is None and _library_covers(session, comp.library_id, amps):
        comp.properties.pop("ol_library_note", None)
        comp.properties.pop("suggested_part", None)
        return
    part = suggest_catalog_part(getattr(session, "parts", None), "protection.overload", amps, kind="range")
    shown = fmt_amps(amps)
    if part is not None:
        comp.properties["suggested_part"] = f"{part.manufacturer} {part.catalog}"
        offer = overload_suggestion(part)
    else:
        comp.properties.pop("suggested_part", None)
        offer = f"No published overload range includes {shown} A."
    if span is None:
        comp.properties["ol_library_note"] = (
            f"No overload range is assigned for the {shown} A setpoint. {offer}"
        )
    else:
        comp.properties["ol_library_note"] = (
            f"Overload range does not include the {shown} A setpoint. {offer}"
        )


def _drop_point_range(comp: Any) -> None:
    from panelcad.core.sizing_checks import real_amp_span

    if real_amp_span(comp.properties) is not None:
        return
    comp.properties.pop("range", None)
    comp.properties.pop("amps_min", None)
    comp.properties.pop("amps_max", None)


def _stamp_wires(session: Any, rows: list[dict[str, Any]], by_ref: dict[str, Any], sizing: dict[str, Any]) -> None:
    power_gauge = str((sizing.get("wire") or {}).get("gauge") or "")
    control_gauge = str((sizing.get("control") or {}).get("gauge") or "16 AWG")
    for row in rows:
        kind = _row_kind(str(row.get("from") or ""), str(row.get("to") or ""), by_ref)
        if kind is None:
            continue
        gauge = power_gauge if kind == "power" else control_gauge
        if not gauge:
            continue
        row["gauge"] = gauge
        wire = _match_wire(session, row)
        if wire is None:
            continue
        _set_gauge(wire, gauge)


def _set_gauge(wire: Any, gauge: str) -> None:
    wire.gauge = gauge
    label = str(wire.label or "").strip()
    net = str(wire.net or "").strip()
    if not label or label == net:
        wire.label = gauge


def _row_kind(frm: str, to: str, by_ref: dict[str, Any]) -> str | None:
    a_ref, a_pin = _split(frm)
    b_ref, b_pin = _split(to)
    a = by_ref.get(a_ref)
    b = by_ref.get(b_ref)
    if a is None and b is None:
        return None
    if a is not None and b is not None:
        if _powerish(a, a_pin) and _powerish(b, b_pin):
            return "power"
        return "control"
    known = a if a is not None else b
    pin = a_pin if a is not None else b_pin
    if known is None:
        return None
    if _powerish(known, pin):
        return "power"
    return "control"


def _wire_kind(frm: str, to: str, member_ids: set[str], members: list[Any]) -> str | None:
    roles: dict[str, str] = {}
    for comp in members:
        role = str(comp.properties.get("role") or "")
        prev = roles.get(comp.id)
        if prev is None or (role in POWER_ROLES and prev not in POWER_ROLES):
            roles[comp.id] = role
    a_ref, a_pin = _split(frm)
    b_ref, b_pin = _split(to)
    a_in = a_ref in member_ids
    b_in = b_ref in member_ids
    if not a_in and not b_in:
        return None

    def powerish(ref: str, pin: str) -> bool:
        return roles.get(ref, "") in POWER_ROLES and pin.upper() not in _CONTROL_PINS

    if a_in and b_in:
        if powerish(a_ref, a_pin) and powerish(b_ref, b_pin):
            return "power"
        return "control"
    ref = a_ref if a_in else b_ref
    pin = a_pin if a_in else b_pin
    if powerish(ref, pin):
        return "power"
    return "control"


def _powerish(part: Any, pin: str) -> bool:
    return str(getattr(part, "role", "") or "") in POWER_ROLES and pin.upper() not in _CONTROL_PINS


def _split(pin_ref: str) -> tuple[str, str]:
    text = str(pin_ref or "")
    if "." not in text:
        return text, ""
    ref, pin = text.split(".", 1)
    return ref, pin


def _match_wire(session: Any, row: dict[str, Any]) -> Any | None:
    frm = str(row.get("from") or "")
    to = str(row.get("to") or "")
    sheet_id = str(row.get("sheet") or "")
    sheets = []
    if sheet_id:
        try:
            sheets.append(session.sheet(sheet_id))
        except KeyError:
            sheets = []
    if not sheets:
        sheets = list(session.project.sheets)
    wanted = {frm, to}
    for sheet in sheets:
        for wire in sheet.wires:
            if {wire.frm, wire.to} == wanted:
                return wire
    return None


def _find(session: Any, ref: str) -> tuple[Any, Any] | None:
    for sheet in session.project.sheets:
        try:
            return sheet, sheet.component(ref)
        except KeyError:
            continue
    return None


def _library_covers(session: Any, library_id: str, current: float) -> bool:
    from panelcad.core.sizing_checks import real_amp_span

    try:
        device = session.catalog.get(library_id)
    except KeyError:
        return False
    span = real_amp_span(device.properties or {})
    if span is None:
        return False
    low, high = span
    return low - 1e-6 <= float(current) <= high + 1e-6


def _looks_like_motor(comp: Any) -> bool:
    lid = str(comp.library_id or "").lower()
    role = str(comp.properties.get("role") or "")
    return role == "motor" or "motor" in lid


def _motor_lines(props: dict[str, Any]) -> list[str]:
    bits: list[str] = []
    if props.get("hp") not in (None, ""):
        bits.append(f"{_num(props.get('hp'))} hp")
    elif props.get("kW") not in (None, ""):
        bits.append(f"{_num(props.get('kW'))} kW")
    if props.get("voltage") not in (None, ""):
        bits.append(f"{_num(props.get('voltage'))} V")
    lines: list[str] = []
    if bits:
        lines.append(" ".join(bits))
    fla = props.get("FLA", props.get("fla"))
    if fla not in (None, ""):
        lines.append(f"FLA {_num(fla)} A")
    return lines


def _num(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:.2f}".rstrip("0").rstrip(".")


def hp_from_kw(kw: float) -> float:
    return float(kw) / HP_TO_KW
