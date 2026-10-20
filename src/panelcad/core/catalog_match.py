"""Catalog SKU versus the device on the drawing.

Assignment still accepts a part in the same category. These checks tell the
floor when that part does not match the device: pole count, current, coil
voltage, pilot-light voltage, or a transformer secondary versus the control
circuit. Wording names the tag and the catalog number. It does not name
library ids.
"""

from __future__ import annotations

import re
from typing import Any

from panelcad.core.electrical import is_coil_only_device, is_power_pole_device
from panelcad.core.models import Component, Device, Project, Sheet
from panelcad.library.catalog import Catalog

CATALOG_MATCH_CHECKS = (
    "catalog_poles",
    "catalog_current",
    "catalog_coil_voltage",
    "catalog_pilot_voltage",
    "catalog_secondary_voltage",
)

_SECONDARY_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*V(?:AC|DC)?\s+secondary",
    re.I,
)
# Longer units first so VAC/VDC is not read as VAC alone.
_VOLT_TOKEN_RE = re.compile(
    r"(?P<lo>\d+(?:\.\d+)?)"
    r"(?:\s*[-–]\s*(?P<hi>\d+(?:\.\d+)?))?"
    r"\s*"
    r"(?P<unit>"
    r"V\s*AC\s*/\s*V\s*DC"
    r"|V\s*DC\s*/\s*V\s*AC"
    r"|V\s*AC\s*/\s*DC"
    r"|V\s*DC\s*/\s*AC"
    r"|V\s*AC"
    r"|V\s*DC"
    r"|V"
    r")",
    re.I,
)
_HZ_RE = re.compile(r"\s*\d+(?:\s*/\s*\d+)?\s*Hz", re.I)
_POLE_CATEGORIES = (
    "protection.breaker",
    "protection.fuse",
    "protection.mpcb",
    "protection.disconnect",
    "switching.contactor",
)
_CURRENT_CATEGORIES = _POLE_CATEGORIES + ("protection.overload",)


def remember_device_rating(comp: Component, _device: Device | None, _part: Any) -> None:
    """Keep a sized trip before a SKU overwrites In/Ie.

    The amps in the symbol name are not a sizing. An In/Ie already written by
    a catalog assignment is not one either.
    """

    if comp.properties.get("device_amps") not in (None, ""):
        return
    props = comp.properties
    if str(props.get("catalog_key") or "").strip() or str(props.get("part_number") or "").strip():
        return
    from panelcad.core.checks import parse_amps

    prior = parse_amps(props.get("In"))
    if prior is None:
        prior = parse_amps(props.get("Ie"))
    if prior is None:
        prior = parse_amps(props.get("ocpd_max_a"))
    if prior is None or prior <= 0:
        return
    comp.properties["device_amps"] = prior


def collect_catalog_mismatches(
    project: Project, catalog: Catalog, parts: Any = None
) -> list[dict[str, Any]]:
    from panelcad.library.panel import resolve_assigned_part

    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for sheet in project.sheets:
        if sheet.kind in {"panel_layout", "door_layout", "terminal"}:
            continue
        for comp in sheet.components:
            device = _device(catalog, comp)
            if device is None:
                continue
            part = resolve_assigned_part(comp.properties or {}, parts)
            if part is None:
                continue
            for item in _mismatches(project, catalog, sheet, comp, device, part):
                key = (str(item["check"]), str(item["ref"]))
                if key in seen:
                    continue
                seen.add(key)
                issues.append(item)
    return issues


def _mismatches(project, catalog, sheet, comp, device, part) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    name = _part_name(part)
    poles = _pole_issue(comp, device, part, name)
    if poles is not None:
        out.append(
            _issue("catalog_poles", poles, sheet, comp, str(getattr(part, "poles", "") or ""))
        )
    current = _current_issue(project, sheet, comp, device, part, name)
    if current is not None:
        out.append(
            _issue(
                "catalog_current",
                current,
                sheet,
                comp,
                _amps_text(getattr(part, "amps", None)),
            )
        )
    if _wants_coil_voltage(device) and not _coil_voltage_elsewhere(project, catalog, comp):
        text = str(getattr(part, "coil_voltage", "") or "").strip()
        options = parse_voltage_options(text)
        circuit = _device_circuit_voltage(project, catalog, sheet, comp, device, ("A1", "A2"))
        if options and circuit is not None and not _part_fits_circuit(options, circuit):
            out.append(
                _issue(
                    "catalog_coil_voltage",
                    (
                        f"{comp.id} coil is on a {_fmt_circuit(circuit)} circuit. "
                        f"{name} is a {_fmt_options(options)} coil."
                    ),
                    sheet,
                    comp,
                    text,
                )
            )
    if (device.category or "") == "operator.light" or "pilot" in device.library_id.lower():
        text = str(getattr(part, "coil_voltage", "") or "").strip()
        options = parse_voltage_options(text) or parse_voltage_options(
            getattr(part, "description", "")
        )
        circuit = _device_circuit_voltage(project, catalog, sheet, comp, device, ("X1", "X2"))
        if options and circuit is not None and not _part_fits_circuit(options, circuit):
            out.append(
                _issue(
                    "catalog_pilot_voltage",
                    (
                        f"{comp.id} is on a {_fmt_circuit(circuit)} circuit. "
                        f"{name} is a {_fmt_options(options)} pilot light."
                    ),
                    sheet,
                    comp,
                    text or _fmt_options(options),
                )
            )
    category = (device.category or "").lower()
    library_id = device.library_id.lower()
    transformer = category.startswith("power.transformer") or "transformer" in library_id
    if transformer:
        secondary = secondary_voltage(part)
        circuit = _transformer_control_voltage(project, catalog, sheet, comp)
        if (
            secondary is not None
            and circuit is not None
            and not _part_fits_circuit([(secondary[0], secondary[0], secondary[1])], circuit)
        ):
            out.append(
                _issue(
                    "catalog_secondary_voltage",
                    (
                        f"{comp.id} secondary is {_fmt_voltage(*secondary)} ({name}). "
                        f"The control circuit is {_fmt_circuit(circuit)}."
                    ),
                    sheet,
                    comp,
                    _fmt_voltage(*secondary),
                )
            )
    return out


def _pole_issue(comp: Component, device: Device, part: Any, name: str) -> str | None:
    have = getattr(part, "poles", None)
    if have is None or is_coil_only_device(device):
        return None
    need = symbol_poles(device)
    if need is None or int(have) == int(need):
        return None
    noun = _noun(device)
    return f"{comp.id} is a {int(need)}-pole {noun}. {name} is {int(have)}-pole."


def _current_issue(
    project: Project, sheet: Sheet, comp: Component, device: Device, part: Any, name: str
) -> str | None:
    if is_coil_only_device(device):
        return None
    cat = (device.category or "").lower()
    if not any(cat.startswith(prefix) for prefix in _CURRENT_CATEGORIES):
        return None
    required = _required_amps(project, sheet, comp, device)
    if required is None or current_covers(part, required):
        return None
    rated = _part_amps_text(part)
    if not rated:
        return None
    return f"{comp.id} is sized {_amps_text(required)} A. {name} is only {rated}."


def _catalog_assigned(props: dict[str, Any]) -> bool:
    key = str(props.get("catalog_key") or "").strip()
    number = str(props.get("part_number") or "").strip()
    return bool(key or number)


def _required_amps(project: Project, sheet: Sheet, comp: Component, device: Device) -> float | None:
    """Sized current only. The symbol frame and the library-id suffix are not a sizing."""

    from panelcad.core.checks import parse_amps

    props = comp.properties or {}
    stored = parse_amps(props.get("device_amps"))
    if stored is not None and stored > 0:
        return stored
    category = (device.category or "").lower()
    if category.startswith("protection.overload"):
        for key in ("ol_fla", "setting"):
            value = parse_amps(props.get(key))
            if value is not None and value > 0:
                return value
    keys = ["ocpd_rating_a", "ocpd_max_a"]
    if not _catalog_assigned(props):
        keys = ["In", "Ie", *keys]
    for key in keys:
        value = parse_amps(props.get(key))
        if value is not None and value > 0:
            return value
    if category.startswith("switching.contactor"):
        return _motor_fla(project, sheet, comp)
    return None


def _motor_fla(project: Project, sheet: Sheet, comp: Component) -> float | None:
    """Full-load current written on the motor this contactor feeds. Not a library default."""

    from panelcad.core.checks import parse_amps
    from panelcad.render.sheet import parse_pin_ref

    group = str(comp.properties.get("sizing_group") or "").strip()

    def _stated(other: Component) -> float | None:
        for key in ("nameplate_fla", "FLA", "fla", "flc_a", "table_flc"):
            value = parse_amps(other.properties.get(key))
            if value is not None and value > 0:
                return value
        return None

    if group:
        for page in project.sheets:
            for other in page.components:
                if other.id == group:
                    stated = _stated(other)
                    if stated is not None:
                        return stated
    connected: set[str] = {comp.id}
    changed = True
    while changed:
        changed = False
        for wire in sheet.wires:
            ends: list[str] = []
            for end in (wire.frm, wire.to):
                try:
                    ends.append(parse_pin_ref(str(end))[0])
                except ValueError:
                    continue
            if len(ends) == 2 and (ends[0] in connected or ends[1] in connected):
                for ref in ends:
                    if ref not in connected:
                        connected.add(ref)
                        changed = True
    found: list[float] = []
    for other in sheet.components:
        if other.id not in connected or "motor" not in other.library_id.lower():
            continue
        stated = _stated(other)
        if stated is not None:
            found.append(stated)
    if len(found) == 1:
        return found[0]
    return None


def current_covers(part: Any, required: float) -> bool:
    """True when the SKU's published amps or range is at least ``required``."""

    lo = getattr(part, "amps_min", None)
    hi = getattr(part, "amps_max", None)
    if lo is not None and hi is not None and float(hi) - float(lo) >= 0.05:
        return float(required) <= float(hi) + 0.05
    amps = getattr(part, "amps", None)
    if amps is None:
        return True
    return float(amps) + 0.05 >= float(required)


def symbol_poles(device: Device | None) -> int | None:
    """Poles drawn on the symbol. A coil-only symbol has none."""

    if device is None or is_coil_only_device(device):
        return None
    raw = (device.properties or {}).get("poles")
    if raw not in (None, ""):
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    cat = (device.category or "").lower()
    if not any(cat.startswith(prefix) for prefix in _POLE_CATEGORIES):
        return None
    pins = {str(pin.id).upper() for pin in device.pins}
    phases = sum(1 for n in (1, 2, 3) if f"L{n}" in pins or f"T{n}" in pins)
    if phases:
        return phases
    nums: list[int] = []
    for pin in device.pins:
        if str(getattr(pin, "electrical", "") or "").lower() != "power":
            continue
        token = str(pin.id).strip()
        if token.isdigit():
            nums.append(int(token))
    if not nums:
        return None
    odds = {n if n % 2 else n - 1 for n in nums}
    return len(odds) or None


def parse_voltage_options(text: str) -> list[tuple[float, float, str]]:
    """Voltages named in a catalog coil or lamp string.

    Each item is ``(low, high, kind)``. A single voltage has ``low == high``.
    ``24-60`` is a range. ``VAC/VDC``, ``VAC/DC``, and ``AC/DC`` are both kinds.
    ``kind`` is ``ac``, ``dc``, or ``""`` when the text does not say.
    """

    raw = str(text or "")
    options: list[tuple[float, float, str]] = []
    for match in _VOLT_TOKEN_RE.finditer(raw):
        low = float(match.group("lo"))
        high = float(match.group("hi")) if match.group("hi") else low
        if high < low:
            low, high = high, low
        kinds = _voltage_kinds(match.group("unit"))
        if kinds == [""] and _HZ_RE.match(raw[match.end() :]):
            kinds = ["ac"]
        for kind in kinds:
            options.append((low, high, kind))
    return options


def _voltage_kinds(unit: str) -> list[str]:
    token = re.sub(r"\s+", "", str(unit or "")).upper()
    has_ac = "AC" in token
    has_dc = "DC" in token
    if has_ac and has_dc:
        return ["ac", "dc"]
    if has_ac:
        return ["ac"]
    if has_dc:
        return ["dc"]
    return [""]


def secondary_voltage(part: Any) -> tuple[float, str] | None:
    """Published secondary, from the part text. Nothing is invented."""

    explicit = getattr(part, "secondary_voltage", None) or getattr(part, "secondary", None)
    if explicit not in (None, ""):
        from panelcad.core.checks import parse_volts

        parsed = parse_volts(explicit)
        if parsed and parsed[0] > 0:
            return parsed
    text = f"{getattr(part, 'description', '')} {getattr(part, 'notes', '')}"
    match = _SECONDARY_RE.search(text)
    if not match:
        return None
    return (float(match.group(1)), "")


def voltage_fits(options: list[tuple[float, float, str]], circuit: tuple[float, str]) -> bool:
    """True when ``circuit`` sits in a published voltage or range, kind included."""

    if not options:
        return True
    volts, kind = circuit
    for low, high, option_kind in options:
        if not _voltage_in_span(volts, low, high):
            continue
        if not option_kind or not kind or option_kind == kind:
            return True
    return False


def _voltage_in_span(volts: float, low: float, high: float) -> bool:
    if low - 0.6 <= volts <= high + 0.6:
        return True
    return _same_band(volts, low) or _same_band(volts, high)


def _same_band(left: float, right: float) -> bool:
    if abs(left - right) <= 0.6:
        return True
    if 100 <= left <= 130 and 100 <= right <= 130:
        return True
    if 200 <= left <= 250 and 200 <= right <= 250:
        return True
    return left <= 30 and right <= 30 and abs(left - right) <= 1.0


def _fmt_voltage(volts: float, kind: str) -> str:
    label = {"ac": " AC", "dc": " DC"}.get(kind, "")
    return f"{volts:g} V{label}"


def _fmt_options(options: list[tuple[float, float, str]]) -> str:
    grouped: dict[tuple[float, float], set[str]] = {}
    order: list[tuple[float, float]] = []
    for low, high, kind in options:
        key = (low, high)
        if key not in grouped:
            order.append(key)
            grouped[key] = set()
        if kind:
            grouped[key].add(kind)
    bits: list[str] = []
    for low, high in order:
        kinds = grouped[(low, high)]
        span = f"{low:g}–{high:g}" if abs(high - low) > 0.05 else f"{low:g}"
        if kinds >= {"ac", "dc"}:
            bits.append(f"{span} V AC/DC")
        elif "ac" in kinds:
            bits.append(f"{span} V AC")
        elif "dc" in kinds:
            bits.append(f"{span} V DC")
        else:
            bits.append(f"{span} V")
    return " / ".join(bits)


def _amps_text(value: float | None) -> str:
    if value is None:
        return ""
    number = float(value)
    if abs(number - round(number)) < 1e-6:
        return str(int(round(number)))
    return f"{number:g}"


def _part_amps_text(part: Any) -> str:
    lo = getattr(part, "amps_min", None)
    hi = getattr(part, "amps_max", None)
    if lo is not None and hi is not None and float(hi) - float(lo) >= 0.05:
        return f"{_amps_text(float(hi))} A"
    amps = getattr(part, "amps", None)
    if amps is None:
        return ""
    return f"{_amps_text(float(amps))} A"


def _part_name(part: Any) -> str:
    manufacturer = str(getattr(part, "manufacturer", "") or "").strip()
    catalog = str(getattr(part, "catalog", "") or "").strip()
    return " ".join(bit for bit in (manufacturer, catalog) if bit)


def _noun(device: Device) -> str:
    cat = (device.category or "").lower()
    lid = device.library_id.lower()
    if "fuse" in cat or "fuse" in lid:
        return "fuse"
    if "disconnect" in cat or "disconnect" in lid:
        return "disconnect"
    if "contactor" in cat or "contactor" in lid:
        return "contactor"
    if "overload" in cat:
        return "overload"
    if "breaker" in cat or "mpcb" in cat or "mcb" in lid or "mccb" in lid:
        return "breaker"
    return "device"


def _wants_coil_voltage(device: Device) -> bool:
    if is_coil_only_device(device) or is_power_pole_device(device):
        return True
    cat = (device.category or "").lower()
    return (
        cat.startswith("switching.contactor")
        or cat.startswith("switching.relay")
        or cat.startswith("timing.")
    )


def _coil_voltage_elsewhere(project: Project, catalog: Catalog, comp: Component) -> bool:
    """Power poles defer to the coil symbol that shares the tag."""

    if not is_power_pole_device(_device(catalog, comp)):
        return False
    for sheet in project.sheets:
        if sheet.kind in {"panel_layout", "door_layout", "terminal"}:
            continue
        for other in sheet.components:
            if other.id != comp.id or other is comp:
                continue
            if is_coil_only_device(_device(catalog, other)):
                return True
    return False


def _device_circuit_voltage(
    project: Project,
    catalog: Catalog,
    sheet: Sheet,
    comp: Component,
    device: Device,
    pins: tuple[str, ...],
) -> list[tuple[float, float, str]] | None:
    found = _as_spans(_voltage_on_pins(project, sheet, comp, pins))
    if found is not None:
        return found
    rail = _as_spans(_sheet_rail_voltage(project, sheet))
    if rail is not None:
        return rail
    stated = _as_spans(_explicit_circuit_voltage(project, sheet, comp))
    if stated is not None:
        return stated
    del device
    return _agreed_catalog_voltage(project, catalog, [sheet], skip_id=comp.id)


def _transformer_control_voltage(
    project: Project, catalog: Catalog, sheet: Sheet, comp: Component
) -> list[tuple[float, float, str]] | None:
    found = _as_spans(_voltage_on_pins(project, sheet, comp, ("X1", "X2")))
    if found is not None:
        return found
    rail = _as_spans(_sheet_rail_voltage(project, sheet))
    if rail is not None:
        return rail
    stated = _as_spans(_explicit_circuit_voltage(project, sheet, comp))
    if stated is not None:
        return stated
    fed = _fed_sheets(project, sheet, comp)
    agreed = _agreed_catalog_voltage(project, catalog, fed, skip_id=comp.id)
    if agreed is not None:
        return agreed
    # X1/X2 never named another sheet. One control supply feeds the whole job.
    if len(fed) == 1 and _sole_control_supply(project, catalog, comp):
        pages = [item for item in project.sheets if item.kind in {"schematic", "io"}]
        return _agreed_catalog_voltage(project, catalog, pages, skip_id=comp.id)
    return None


def _as_spans(point: tuple[float, str] | None) -> list[tuple[float, float, str]] | None:
    if point is None:
        return None
    return [(point[0], point[0], point[1])]


def _part_fits_circuit(
    options: list[tuple[float, float, str]],
    circuit: list[tuple[float, float, str]],
) -> bool:
    """True when the part's published voltage sits on the circuit.

    Two published points in the same nominal band match: 110/115/120 V, and
    220/230/240 V. A range still has to cover the other voltage; the band does
    not stretch its ends.
    """

    if not circuit:
        return True
    for low, high, kind in options:
        for clo, chi, ckind in circuit:
            if kind and ckind and kind != ckind:
                continue
            part_point = _is_point(low, high)
            circuit_point = _is_point(clo, chi)
            if part_point and circuit_point and _same_band(low, clo):
                return True
            if max(low, clo) - 0.6 <= min(high, chi) + 0.6:
                return True
    return False


def _fmt_circuit(circuit: list[tuple[float, float, str]]) -> str:
    return _fmt_options(circuit)


def _sole_control_supply(project: Project, catalog: Catalog, comp: Component) -> bool:
    """True when this is the only control transformer or supply on the job."""

    found: list[str] = []
    for sheet in project.sheets:
        if sheet.kind in {"panel_layout", "door_layout", "terminal"}:
            continue
        for other in sheet.components:
            device = _device(catalog, other)
            if device is None:
                continue
            category = (device.category or "").lower()
            library_id = device.library_id.lower()
            supply = category.startswith("power.transformer") or category.startswith("power.supply")
            supply = supply or "transformer" in library_id or "psu" in library_id
            if supply:
                found.append(other.id)
    return found == [comp.id]


def _voltage_on_pins(
    project: Project, sheet: Sheet, comp: Component, pins: tuple[str, ...]
) -> tuple[float, str] | None:
    from panelcad.core.checks import _end_voltage, _lands_on
    from panelcad.render.sheet import parse_pin_ref

    wanted = {pin.upper() for pin in pins}
    for wire in _lands_on(sheet, comp.id):
        ends = (str(wire.frm), str(wire.to))
        touches = False
        for end in ends:
            try:
                cid, pid = parse_pin_ref(end)
            except ValueError:
                cid, pid = "", ""
            if cid == comp.id and pid.upper() in wanted:
                touches = True
        if pins and not touches:
            continue
        for end in ends:
            circuit = _end_voltage(sheet, project, end, wire)
            if circuit and circuit[0] > 0:
                return circuit
    return None


def _sheet_rail_voltage(project: Project, sheet: Sheet) -> tuple[float, str] | None:
    from panelcad.core.checks import _rail_voltage

    return _rail_voltage(sheet, project)


def _explicit_circuit_voltage(
    project: Project, sheet: Sheet, comp: Component
) -> tuple[float, str] | None:
    """A control voltage a person or Circuit Builder wrote down. Not a library default."""

    from panelcad.core.checks import parse_volts

    for raw in (
        getattr(sheet, "control_voltage", None),
        getattr(project, "control_voltage", None),
    ):
        if raw not in (None, ""):
            try:
                number = float(raw)
            except (TypeError, ValueError):
                number = 0.0
            if number > 0:
                return (number, "")
    sources: list[dict[str, Any]] = []
    if isinstance(comp.properties, dict):
        sources.append(comp.properties)
    project_props = getattr(project, "properties", None)
    if isinstance(project_props, dict):
        sources.append(project_props)
    for source in sources:
        for key in ("control_voltage", "circuit_voltage"):
            raw = source.get(key)
            parsed = parse_volts(raw)
            if parsed and parsed[0] > 0:
                return parsed
            try:
                number = float(raw)
            except (TypeError, ValueError):
                continue
            if number > 0:
                return (number, "")
    return None


def _votes_as_control_coil(device: Device | None) -> bool:
    """A coil, contactor, relay, or timer. A pilot light is checked, not evidence."""

    if device is None:
        return False
    if is_coil_only_device(device) or is_power_pole_device(device):
        return True
    category = (device.category or "").lower()
    return category.startswith("switching.relay") or category.startswith("timing.")


def _agreed_catalog_voltage(
    project: Project,
    catalog: Catalog,
    sheets: list[Sheet],
    *,
    skip_id: str,
) -> list[tuple[float, float, str]] | None:
    """Voltage published on assigned coils, when at least two agree.

    Library symbol defaults never count. The device being checked is left out.
    """

    from panelcad.library.panel import resolve_assigned_part

    ballots: list[list[tuple[float, float, str]]] = []
    del project
    for sheet in sheets:
        if sheet.kind not in {"schematic", "io"}:
            continue
        for comp in sheet.components:
            if comp.id == skip_id:
                continue
            device = _device(catalog, comp)
            if not _votes_as_control_coil(device):
                continue
            part = resolve_assigned_part(comp.properties or {}, None)
            if part is None:
                continue
            options = parse_voltage_options(getattr(part, "coil_voltage", "") or "")
            if options:
                ballots.append(options)
    if len(ballots) < 2:
        return None
    for index, ballot in enumerate(ballots):
        for other in ballots[index + 1 :]:
            if not _voltage_ballots_agree(ballot, other):
                return None
    return _ballot_intersection(ballots)


def _voltage_ballots_agree(
    left: list[tuple[float, float, str]],
    right: list[tuple[float, float, str]],
) -> bool:
    for low_a, high_a, kind_a in left:
        for low_b, high_b, kind_b in right:
            if kind_a and kind_b and kind_a != kind_b:
                continue
            overlap = low_a <= high_b + 0.6 and low_b <= high_a + 0.6
            band = any(
                _same_band(a, b)
                for a in (low_a, high_a)
                for b in (low_b, high_b)
            )
            if overlap or band:
                return True
    return False


def _is_point(low: float, high: float) -> bool:
    return abs(high - low) <= 0.05


def _option_covers_point(
    option: tuple[float, float, str], volts: float, kind: str
) -> bool:
    low, high, option_kind = option
    if option_kind and kind and option_kind != kind:
        return False
    if _is_point(low, high):
        return abs(low - volts) <= 0.6 or _same_band(low, volts)
    return low - 0.6 <= volts <= high + 0.6


def _ballot_intersection(
    ballots: list[list[tuple[float, float, str]]],
) -> list[tuple[float, float, str]] | None:
    """The span every coil accepts. A range does not become its top number.

    A published point (120 V, 230 V) sets the circuit when every other coil
    accepts it. Ranges only narrow that point, or stand as the circuit when
    no coil published a single voltage.
    """

    points: list[tuple[float, float, str]] = []
    seen: set[tuple[float, str]] = set()
    for ballot in ballots:
        for low, high, kind in ballot:
            if not _is_point(low, high):
                continue
            key = (round(low, 3), kind)
            if key in seen:
                continue
            if all(any(_option_covers_point(opt, low, kind) for opt in other) for other in ballots):
                seen.add(key)
                points.append((low, low, kind))
    if points:
        return points
    spans = list(ballots[0])
    for other in ballots[1:]:
        narrowed: list[tuple[float, float, str]] = []
        for low, high, kind in spans:
            for other_low, other_high, other_kind in other:
                if kind and other_kind and kind != other_kind:
                    continue
                start = max(low, other_low)
                end = min(high, other_high)
                if start <= end + 0.6:
                    narrowed.append((start, max(start, end), kind or other_kind))
        spans = narrowed
        if not spans:
            return None
    return spans


def _fed_sheets(project: Project, sheet: Sheet, comp: Component) -> list[Sheet]:
    """This sheet, plus a sheet an X1/X2 wire names through an off-page marker."""

    from panelcad.render.sheet import parse_pin_ref

    found: dict[str, Sheet] = {sheet.id: sheet}
    wanted = {"X1", "X2"}
    for wire in sheet.wires:
        ends: list[tuple[str, str]] = []
        for end in (wire.frm, wire.to):
            try:
                ends.append(parse_pin_ref(str(end)))
            except ValueError:
                continue
        if not any(cid == comp.id and pin.upper() in wanted for cid, pin in ends):
            continue
        for cid, _pin in ends:
            if cid == comp.id:
                continue
            try:
                other = sheet.component(cid)
            except KeyError:
                continue
            dest = str(other.properties.get("dest_sheet") or "").strip()
            if not dest:
                continue
            try:
                found[dest] = project.sheet(dest)
            except KeyError:
                continue
    return list(found.values())


def _device(catalog: Catalog, comp: Component) -> Device | None:
    try:
        return catalog.get(comp.library_id)
    except KeyError:
        return None


def _issue(check: str, message: str, sheet: Sheet, comp: Component, target: str) -> dict[str, Any]:
    return {
        "check": check,
        "severity": "warning",
        "message": message,
        "sheet": sheet.id,
        "ref": comp.id,
        "target": target,
        "x": comp.x,
        "y": comp.y,
    }
