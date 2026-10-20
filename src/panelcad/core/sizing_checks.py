"""Design checks for motor wire, branch-device, and overload-range sizing.

The findings are:

* ``wire_undersized`` — a motor power conductor is below the 125% full-load
  requirement on NEC Table 310.16 or UL 508A Table 28.1 at the termination
  temperature (or the gauge is not in those tables).
* ``protection_above_430_52`` — a breaker or fuse above the Table 430.52
  permitted maximum.
* ``overload_range_misses_fla`` — a real overload adjustment range does not
  include the current it must be set to, or no published range includes it.
  A zero-width range is not a range. A catalog part that covers the setpoint
  is named on the device instead of raising this check.
* ``overload_needs_nameplate`` — the overload is still the table full-load
  placeholder. NEC 430.6(A)(2) and 430.32 require the nameplate current.
* ``breaker_frame_below_rating`` — the sized trip is above the placed breaker
  symbol's frame. The message names the next builtin frame, or says none
  exists, and a catalog breaker when one covers the trip.
* ``contactor_undersized`` — the contactor frame is below the motor full-load
  current. The message names a catalog contactor when one covers that current.

Control-circuit wires (coil and auxiliary pins, or a 16 AWG control gauge on
a circuit that was sized) are not compared with the motor branch ampacity.
"""

from __future__ import annotations

import re
from typing import Any

from panelcad.core.models import Component, Device, Project, Sheet, Wire
from panelcad.core.motor_sizing import (
    UL_UNVERIFIED,
    ampacity_of,
    branch_device_max,
    fmt_amps,
    motor_flc,
    size_motor_circuit,
)
from panelcad.library.catalog import Catalog

SIZING_CHECKS = (
    "wire_undersized",
    "protection_above_430_52",
    "overload_range_misses_fla",
    "overload_needs_nameplate",
    "breaker_frame_below_rating",
    "contactor_undersized",
)

# A published adjustable range has two different ends. 7.6-7.6 A is not one.
_MIN_SPAN = 0.05
_FRAME_SUFFIX = re.compile(r"(\d+(?:\.\d+)?)a$", re.IGNORECASE)

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


def _device(catalog: Catalog, comp: Component) -> Device | None:
    try:
        return catalog.get(comp.library_id)
    except KeyError:
        return None


def _is_power_device(device: Device | None, comp: Component) -> bool:
    if device is None:
        return False
    cat = (device.category or "").lower()
    lid = (comp.library_id or "").lower()
    if "motor" in lid or cat.startswith("load.motor"):
        return True
    if cat.startswith("protection.") and "ol_contact" not in lid:
        return True
    if cat.startswith("switching.contactor") and not lid.endswith(".coil") and "aux_" not in lid:
        return True
    if "drive" in lid or "softstarter" in lid or cat.startswith("drive"):
        return True
    if "disconnect" in lid:
        return True
    return False


def _is_breaker(device: Device | None, comp: Component) -> bool:
    if device is None:
        return False
    cat = (device.category or "").lower()
    lid = (comp.library_id or "").lower()
    if "fuse" in lid or cat.startswith("protection.fuse") or "ol_contact" in lid:
        return False
    return cat.startswith("protection.breaker") or "breaker" in lid or "mcb" in lid or "mccb" in lid


def _is_breaker_or_fuse(device: Device | None, comp: Component) -> bool:
    if device is None:
        return False
    cat = (device.category or "").lower()
    lid = (comp.library_id or "").lower()
    if "ol_contact" in lid or lid.endswith(".coil"):
        return False
    return (
        cat.startswith("protection.breaker")
        or cat.startswith("protection.fuse")
        or "breaker" in lid
        or "fuse" in lid
        or "mcb" in lid
        or "mccb" in lid
    )


def _is_contactor(device: Device | None, comp: Component) -> bool:
    if device is None:
        return False
    lid = (comp.library_id or "").lower()
    if lid.endswith(".coil") or "aux_" in lid:
        return False
    cat = (device.category or "").lower()
    return cat.startswith("switching.contactor")


def _contactor_amps(device: Device | None, comp: Component, parse_amps) -> float | None:
    """Assigned catalog rating when a part number is on the symbol, otherwise the library frame."""

    assigned = str(comp.properties.get("part_number") or comp.properties.get("catalog") or "").strip()
    if assigned:
        for key in ("Ie", "amps"):
            value = parse_amps(comp.properties.get(key))
            if value is not None and value > 0:
                return value
    return library_frame_amps(device, comp.library_id)


def _is_overload(device: Device | None, comp: Component) -> bool:
    if device is None:
        return False
    lid = (comp.library_id or "").lower()
    if "ol_contact" in lid:
        return False
    cat = (device.category or "").lower()
    return cat.startswith("protection.overload") or ("overload" in lid and "contact" not in lid)


def _is_motor(device: Device | None, comp: Component) -> bool:
    lid = (comp.library_id or "").lower()
    if "motor" in lid:
        return True
    if device is None:
        return str(comp.properties.get("role") or "") == "motor"
    return (device.category or "").startswith("load.motor") or str(comp.properties.get("role") or "") == "motor"


def library_frame_amps(device: Device | None, library_id: str = "") -> float | None:
    """Frame printed on the library symbol. Ignores a trip stamped onto the component."""

    from panelcad.core.checks import parse_amps

    props = device.properties if device is not None else {}
    for key in ("In", "Ie"):
        value = parse_amps((props or {}).get(key))
        if value is not None and value > 0:
            return value
    text = library_id or (device.library_id if device is not None else "")
    match = _FRAME_SUFFIX.search(text.rsplit(".", 1)[-1])
    if match:
        return float(match.group(1))
    return None


def real_amp_span(props: dict[str, Any]) -> tuple[float, float] | None:
    """An adjustable range with two different ends. A single point is not a range."""

    from panelcad.core.checks import parse_amp_range

    rng = parse_amp_range(props.get("range"))
    if rng is None:
        low, high = props.get("amps_min"), props.get("amps_max")
        if low not in (None, "") and high not in (None, ""):
            try:
                rng = (float(low), float(high))
            except (TypeError, ValueError):
                rng = None
    if rng is None:
        return None
    lo, hi = float(rng[0]), float(rng[1])
    if hi < lo:
        lo, hi = hi, lo
    if hi - lo < _MIN_SPAN:
        return None
    return (lo, hi)


def _poles(device: Device | None) -> int | None:
    if device is None:
        return None
    raw = (device.properties or {}).get("poles")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def next_breaker_symbol(catalog: Catalog, library_id: str, rating_a: float) -> tuple[str, float] | None:
    """Smallest same-standard breaker frame that covers ``rating_a`` and is larger than the placed frame."""

    try:
        current = catalog.get(library_id)
    except KeyError:
        current = None
    placed = library_frame_amps(current, library_id) or 0.0
    poles = _poles(current)
    prefix = library_id.split(".", 1)[0] + "." if "." in library_id else ""
    best_id: str | None = None
    best_frame: float | None = None
    for lid, device in catalog.devices.items():
        if prefix and not lid.startswith(prefix):
            continue
        if not (device.category or "").lower().startswith("protection.breaker"):
            continue
        if "mpcb" in lid.lower():
            continue
        frame = library_frame_amps(device, lid)
        if frame is None or frame + 1e-6 < float(rating_a) or frame <= placed + 1e-6:
            continue
        other_poles = _poles(device)
        if poles is not None and other_poles is not None and other_poles != poles:
            continue
        if best_frame is None or frame < best_frame - 1e-9 or (abs(frame - best_frame) < 1e-9 and lid < (best_id or "")):
            best_id, best_frame = lid, frame
    if best_id is None or best_frame is None:
        return None
    return best_id, best_frame


def suggest_catalog_part(parts: Any, category: str, required_a: float, *, kind: str) -> Any | None:
    """Smallest frame at or above ``required_a``, or the tightest published range that contains it.

    ``kind`` is ``frame`` or ``range``. Ties break on manufacturer, then catalog number.
    Nothing here is invented: the row has to be in the parts catalog.
    """

    if parts is None:
        from panelcad.library.parts import default_parts_catalog

        parts = default_parts_catalog()
    search = getattr(parts, "search", None)
    if search is None:
        return None
    items = list(search(category=category))
    if kind == "range":
        hits: list[tuple[float, float, str, str, Any]] = []
        for part in items:
            lo, hi = getattr(part, "amps_min", None), getattr(part, "amps_max", None)
            if lo is None or hi is None:
                continue
            span = float(hi) - float(lo)
            if span < _MIN_SPAN:
                continue
            if float(lo) - 1e-6 <= float(required_a) <= float(hi) + 1e-6:
                hits.append((span, float(hi), str(part.manufacturer), str(part.catalog), part))
        if not hits:
            return None
        hits.sort()
        return hits[0][-1]
    hits_frame: list[tuple[float, str, str, Any]] = []
    for part in items:
        amps = getattr(part, "amps", None)
        if amps is None:
            continue
        if float(amps) + 1e-6 >= float(required_a):
            hits_frame.append((float(amps), str(part.manufacturer), str(part.catalog), part))
    if not hits_frame:
        return None
    hits_frame.sort()
    return hits_frame[0][-1]


def _family_is_list_or_range(family: str) -> bool:
    """True when the published fit text names more than one frame."""

    lowered = family.lower()
    if " to " in lowered or " or " in lowered:
        return True
    return any(mark in family for mark in (",", "/", "…", "..."))


def catalog_fit_sentence(part: Any) -> str:
    """Contactor family this overload mounts on, taken from its description.

    A published list or range is copied as written. ``only`` is reserved for
    a single frame name. It is not added to a range the description abbreviated.
    """

    description = str(getattr(part, "description", "") or "")
    lowered = description.lower()
    for marker in ("mounts on ", "for "):
        at = lowered.rfind(marker)
        if at < 0:
            continue
        family = description[at + len(marker):].strip(" .")
        if not family:
            continue
        if _family_is_list_or_range(family):
            return f"Fits {family}."
        return f"Fits {family} only."
    return ""


def catalog_range_conflict(part: Any) -> str:
    """Product-page versus PDF range, when the part notes record both."""

    notes = str(getattr(part, "notes", "") or "")
    marker = "RANGE CONFLICT:"
    at = notes.find(marker)
    if at < 0:
        return ""
    rest = notes[at + len(marker):].strip()
    sentence = rest.split(". ")[0].strip().rstrip(".")
    if not sentence:
        return ""
    return sentence + "."


def overload_suggestion(part: Any) -> str:
    """Catalog overload, the family it fits, and any recorded range conflict."""

    text = f"Suggested part: {part_label(part)}."
    fit = catalog_fit_sentence(part)
    if fit:
        text += " " + fit
    conflict = catalog_range_conflict(part)
    if conflict:
        text += " " + conflict
    return text


def part_label(part: Any) -> str:
    """``Manufacturer catalog (range or amps)`` for a check message."""

    text = f"{part.manufacturer} {part.catalog}"
    lo, hi = getattr(part, "amps_min", None), getattr(part, "amps_max", None)
    if lo is not None and hi is not None and float(hi) - float(lo) >= _MIN_SPAN:
        return f"{text} ({fmt_amps(float(lo))}–{fmt_amps(float(hi))} A)"
    amps = getattr(part, "amps", None)
    if amps is not None:
        return f"{text} ({fmt_amps(float(amps))} A)"
    return text


def breaker_frame_message(
    ref: str,
    trip_a: float,
    frame_a: float,
    library_id: str,
    nxt: tuple[str, float] | None,
    part: Any | None,
) -> str:
    text = (
        f"{ref} trip {fmt_amps(trip_a)} A exceeds the {fmt_amps(frame_a)} A frame of {library_id}."
    )
    if nxt is not None:
        text += f" Next builtin frame: {nxt[0]} ({fmt_amps(nxt[1])} A)."
    else:
        text += f" No larger builtin breaker frame covers {fmt_amps(trip_a)} A."
    if part is not None:
        text += f" Suggested part: {part_label(part)}."
    else:
        text += f" No catalog breaker covers {fmt_amps(trip_a)} A."
    return text


def contactor_frame_message(
    ref: str,
    frame_a: float,
    library_id: str,
    required_a: float,
    part: Any | None,
) -> str:
    text = (
        f"{ref} frame {fmt_amps(frame_a)} A ({library_id}) is below the "
        f"{fmt_amps(required_a)} A full-load current."
    )
    if part is not None:
        text += f" Suggested part: {part_label(part)}."
    else:
        text += f" No catalog contactor covers {fmt_amps(required_a)} A."
    return text


def collect_sizing_issues(project: Project, catalog: Catalog, parts: Any = None) -> list[dict[str, Any]]:
    """Plain dicts. ``checks.run_checks`` wraps them as findings."""

    from panelcad.core.checks import _merged_props, parse_amps
    from panelcad.render.sheet import parse_pin_ref

    issues: list[dict[str, Any]] = []
    for sheet in project.sheets:
        if sheet.kind in {"panel_layout", "door_layout", "terminal"}:
            continue
        motors = [comp for comp in sheet.components if _is_motor(_device(catalog, comp), comp)]
        for motor in motors:
            issues.extend(
                _motor_issues(
                    sheet,
                    catalog,
                    motor,
                    parse_amps,
                    _merged_props,
                    parse_pin_ref,
                    parts,
                )
            )
    return issues


def _motor_issues(sheet, catalog, motor, parse_amps, merged, parse_pin_ref, parts=None) -> list[dict[str, Any]]:
    mdev = _device(catalog, motor)
    props = merged(mdev, motor)
    requirement = _requirements(props)
    if requirement is None:
        return []
    connected = _connected_refs(sheet, motor.id, parse_pin_ref)
    issues: list[dict[str, Any]] = []
    if _needs_nameplate(props):
        placeholder = props.get("table_flc", props.get("flc_a", props.get("FLA")))
        shown = fmt_amps(float(placeholder)) if placeholder not in (None, "") else "the table"
        issues.append(
            _issue(
                "overload_needs_nameplate",
                (
                    f"{motor.id} has no nameplate current. The overload placeholder is {shown} A "
                    "from the NEC full-load table. NEC 430.6(A)(2) and 430.32 require the nameplate."
                ),
                sheet,
                motor.id,
                "nameplate",
                motor,
            )
        )
    required_a = float(requirement["conductor_min_a"])
    temp_c = _marked_termination(sheet, connected, int(requirement["wire_temp_c"]))
    for wire in sheet.wires:
        if not _is_branch_wire(sheet, catalog, wire, connected, parse_pin_ref):
            continue
        gauge = str(wire.gauge or "").strip()
        if not gauge:
            continue
        amp, cite = _limiting_ampacity(gauge, temp_c)
        if amp is None:
            issues.append(
                _issue(
                    "wire_undersized",
                    (
                        f"{wire.id} gauge {gauge} has no ampacity in NEC Table 310.16 "
                        f"or UL 508A Table 28.1 at {temp_c}°C. {UL_UNVERIFIED}"
                    ),
                    sheet,
                    motor.id,
                    wire.id,
                    wire,
                )
            )
        elif amp + 1e-6 < required_a:
            issues.append(
                _issue(
                    "wire_undersized",
                    (
                        f"{wire.id} is {gauge} ({fmt_amps(amp)} A, {cite}) below "
                        f"{fmt_amps(required_a)} A required for {motor.id}"
                    ),
                    sheet,
                    motor.id,
                    wire.id,
                    wire,
                )
            )
    permitted = requirement.get("ocpd_max_a")
    for comp in sheet.components:
        if comp.id not in connected:
            continue
        device = _device(catalog, comp)
        if not _is_breaker_or_fuse(device, comp):
            continue
        rating = parse_amps(merged(device, comp).get("In"))
        if rating is None:
            continue
        if permitted is not None and float(rating) > float(permitted) + 0.05:
            issues.append(
                _issue(
                    "protection_above_430_52",
                    (
                        f"{comp.id} is {fmt_amps(float(rating))} A, above the {fmt_amps(float(permitted))} A "
                        f"maximum for {motor.id} ({requirement['ocpd_citation']})"
                    ),
                    sheet,
                    comp.id,
                    motor.id,
                    comp,
                )
            )
        frame = library_frame_amps(device, comp.library_id)
        if _is_breaker(device, comp) and frame is not None and float(rating) > float(frame) + 0.05:
            nxt = next_breaker_symbol(catalog, comp.library_id, float(rating))
            offered = suggest_catalog_part(parts, "protection.breaker", float(rating), kind="frame")
            issues.append(
                _issue(
                    "breaker_frame_below_rating",
                    breaker_frame_message(comp.id, float(rating), float(frame), comp.library_id, nxt, offered),
                    sheet,
                    comp.id,
                    motor.id,
                    comp,
                    severity="error",
                )
            )
    flc = requirement.get("flc_a")
    if flc is not None:
        for comp in sheet.components:
            if comp.id not in connected:
                continue
            device = _device(catalog, comp)
            if not _is_contactor(device, comp):
                continue
            frame = _contactor_amps(device, comp, parse_amps)
            if frame is None or float(flc) <= float(frame) + 0.05:
                continue
            offered = suggest_catalog_part(parts, "switching.contactor", float(flc), kind="frame")
            issues.append(
                _issue(
                    "contactor_undersized",
                    contactor_frame_message(comp.id, float(frame), comp.library_id, float(flc), offered),
                    sheet,
                    comp.id,
                    motor.id,
                    comp,
                    severity="error",
                )
            )
    ol_current = requirement.get("overload_current_a")
    if ol_current is not None:
        current = float(ol_current)
        for comp in sheet.components:
            if comp.id not in connected:
                continue
            device = _device(catalog, comp)
            if not _is_overload(device, comp):
                continue
            span = real_amp_span(merged(device, comp))
            if span is None:
                offered = suggest_catalog_part(parts, "protection.overload", current, kind="range")
                if offered is None:
                    issues.append(
                        _issue(
                            "overload_range_misses_fla",
                            (
                                f"{comp.id} has no adjustable range that includes "
                                f"{motor.id} current {fmt_amps(current)} A. "
                                f"No catalog overload range includes {fmt_amps(current)} A."
                            ),
                            sheet,
                            comp.id,
                            motor.id,
                            comp,
                        )
                    )
                continue
            lo, hi = span
            if current + 1e-6 < lo or current - 1e-6 > hi:
                offered = suggest_catalog_part(parts, "protection.overload", current, kind="range")
                message = (
                    f"{comp.id} range {fmt_amps(lo)}–{fmt_amps(hi)} A does not include "
                    f"{motor.id} current {fmt_amps(current)} A."
                )
                if offered is not None:
                    message += f" {overload_suggestion(offered)}"
                else:
                    message += f" No catalog overload range includes {fmt_amps(current)} A."
                issues.append(
                    _issue(
                        "overload_range_misses_fla",
                        message,
                        sheet,
                        comp.id,
                        motor.id,
                        comp,
                    )
                )
    return issues


def _requirements(props: dict[str, Any]) -> dict[str, Any] | None:
    """Stored sizing wins. Otherwise recompute from hp, voltage, and phase count."""

    conductor = props.get("conductor_min_a")
    ocpd_max = props.get("ocpd_max_a")
    if conductor not in (None, ""):
        try:
            conductor_a = float(conductor)
        except (TypeError, ValueError):
            conductor_a = None
        else:
            ol = props.get("ol_fla", props.get("FLA", props.get("fla")))
            try:
                ol_a = None if ol in (None, "") else float(str(ol).lower().replace("a", "").strip())
            except (TypeError, ValueError):
                ol_a = None
            try:
                max_a = None if ocpd_max in (None, "") else float(ocpd_max)
            except (TypeError, ValueError):
                max_a = None
            try:
                temp_c = int(props.get("wire_temp_c") or props.get("termination_c") or 60)
            except (TypeError, ValueError):
                temp_c = 60
            if temp_c not in {60, 75}:
                temp_c = 60
            flc_a = _optional_float(props.get("table_flc"))
            if flc_a is None:
                flc_a = _optional_float(props.get("flc_a"))
            return {
                "conductor_min_a": conductor_a,
                "ocpd_max_a": max_a,
                "overload_current_a": ol_a,
                "flc_a": flc_a,
                "wire_table": "nec_310_16+ul_508a_28_1",
                "wire_temp_c": temp_c,
                "wire_citation": (
                    f"NEC Table 310.16 and UL 508A Table 28.1 {temp_c}°C. {UL_UNVERIFIED}"
                ),
                "ocpd_citation": "NEC Table 430.52(C)(1)",
            }
    hp = props.get("hp")
    voltage = props.get("voltage")
    if hp in (None, "") or voltage in (None, ""):
        return None
    try:
        phases = int(props.get("phases") or 3)
    except (TypeError, ValueError):
        phases = 3
    sized = size_motor_circuit(
        hp=hp,
        volts=voltage,
        phases=phases,
        fla=_optional_float(props.get("nameplate_fla")),
        device=str(props.get("ocpd_device") or "inverse_time_breaker"),
        wire_table=str(props.get("wire_table") or "auto"),
        temp_c=_optional_int(props.get("termination_c", props.get("wire_temp_c"))),
    )
    if not sized.get("ok") or sized.get("code") != "NEC":
        return None
    return {
        "conductor_min_a": sized["conductor_min_a"],
        "ocpd_max_a": sized["ocpd"]["permitted_max_a"],
        "overload_current_a": sized["overload_current_a"],
        "flc_a": sized.get("flc_a"),
        "wire_table": sized["wire"]["table"],
        "wire_temp_c": sized["wire"]["temp_c"],
        "wire_citation": sized["wire"]["citation"],
        "ocpd_citation": sized["ocpd"]["citation"],
    }


def _needs_nameplate(props: dict[str, Any]) -> bool:
    """True when a NEC sizing used the table current because no nameplate was entered."""

    if props.get("nameplate_fla") not in (None, ""):
        return False
    if str(props.get("fla_source") or "") == "nameplate":
        return False
    if str(props.get("sizing_code") or "") == "IEC":
        return False
    return str(props.get("sizing_code") or "") == "NEC" or str(props.get("fla_source") or "") == "nec-table"


def _marked_termination(sheet: Sheet, connected: set[str], stored: int) -> int:
    """Motor ``termination_c`` overrides the temperature stored at insert."""

    for comp in sheet.components:
        if comp.id not in connected:
            continue
        role = str(comp.properties.get("role") or "")
        if role != "motor" and "motor" not in str(comp.library_id).lower():
            continue
        raw = comp.properties.get("termination_c")
        if raw in (None, ""):
            continue
        try:
            marked = int(raw)
        except (TypeError, ValueError):
            continue
        if marked in {60, 75}:
            return marked
    return stored


def _limiting_ampacity(gauge: str, temp_c: int) -> tuple[float | None, str]:
    """Smaller published ampacity of the two tables. That column governs the check."""

    def _cite(text: str) -> str:
        if "UL 508A" in text:
            return f"{text}. {UL_UNVERIFIED}"
        return text

    nec = ampacity_of(gauge, table="nec_310_16", temp_c=temp_c)
    ul = ampacity_of(gauge, table="ul_508a_28_1", temp_c=temp_c)
    if nec is None and ul is None:
        return None, f"{temp_c}°C"
    if nec is None:
        return ul, _cite(f"UL 508A Table 28.1 {temp_c}°C")
    if ul is None:
        return nec, f"NEC Table 310.16 {temp_c}°C"
    if ul + 1e-9 < nec:
        return ul, _cite(
            f"UL 508A Table 28.1 {temp_c}°C; NEC Table 310.16 {temp_c}°C is {fmt_amps(nec)} A"
        )
    if nec + 1e-9 < ul:
        return nec, _cite(
            f"NEC Table 310.16 {temp_c}°C; UL 508A Table 28.1 {temp_c}°C is {fmt_amps(ul)} A"
        )
    return nec, _cite(f"NEC Table 310.16 and UL 508A Table 28.1 {temp_c}°C")


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).lower().replace("a", "").strip())
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _connected_refs(sheet: Sheet, start: str, parse_pin_ref) -> set[str]:
    """Devices reached through wires, crossing from one pin of a device to its others.

    A motor, overload, contactor, and breaker are one branch even though the
    wire lands on a single pin of each symbol.
    """

    graph: dict[str, list[str]] = {}
    for wire in sheet.wires:
        try:
            a_ref, a_pin = parse_pin_ref(wire.frm)
            b_ref, b_pin = parse_pin_ref(wire.to)
        except ValueError:
            continue
        graph.setdefault(f"{a_ref}.{a_pin}", []).append(f"{b_ref}.{b_pin}")
        graph.setdefault(f"{b_ref}.{b_pin}", []).append(f"{a_ref}.{a_pin}")
    pins_of: dict[str, list[str]] = {}
    for key in graph:
        try:
            ref, _pin = parse_pin_ref(key)
        except ValueError:
            continue
        pins_of.setdefault(ref, []).append(key)
    seen: set[str] = set()
    refs = {start}
    queue = list(pins_of.get(start, []))
    while queue:
        token = queue.pop()
        if token in seen:
            continue
        seen.add(token)
        try:
            ref, _pin = parse_pin_ref(token)
        except ValueError:
            continue
        if ref not in refs:
            refs.add(ref)
            for pin_key in pins_of.get(ref, []):
                if pin_key not in seen:
                    queue.append(pin_key)
        for nxt in graph.get(token, []):
            if nxt not in seen:
                queue.append(nxt)
    return refs


def _is_branch_wire(sheet, catalog, wire: Wire, connected: set[str], parse_pin_ref) -> bool:
    try:
        a_ref, a_pin = parse_pin_ref(wire.frm)
        b_ref, b_pin = parse_pin_ref(wire.to)
    except ValueError:
        return False
    if a_pin.upper() in _CONTROL_PINS or b_pin.upper() in _CONTROL_PINS:
        return False
    if a_ref not in connected or b_ref not in connected:
        return False
    try:
        a_comp = sheet.component(a_ref)
        b_comp = sheet.component(b_ref)
    except KeyError:
        return False
    return _is_power_device(_device(catalog, a_comp), a_comp) and _is_power_device(_device(catalog, b_comp), b_comp)


def _issue(
    check: str,
    message: str,
    sheet,
    ref: str,
    target: str,
    anchor,
    severity: str = "warning",
) -> dict[str, Any]:
    x = getattr(anchor, "x", None)
    y = getattr(anchor, "y", None)
    return {
        "check": check,
        "severity": severity,
        "message": message,
        "sheet": sheet.id,
        "ref": ref,
        "target": target,
        "x": x,
        "y": y,
    }


def recompute_branch_max(flc: float, device: str, motor_type: str) -> float | None:
    """Helper for tests that want the permitted maximum without the full report."""

    return branch_device_max(flc, device=device, motor_type=motor_type)["permitted_max_a"]


def table_flc_or_none(hp: float, voltage: float, phases: int = 3) -> float | None:
    looked = motor_flc(hp=hp, voltage=voltage, phases=phases)
    if looked is None:
        return None
    return looked[2]
