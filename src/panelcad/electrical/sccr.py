"""UL 508A Supplement SB short-circuit current rating for one panel.

The panel rating is the lowest power-circuit result after Table SB4.1
defaults, branch-device let-through, and the transformer rule. Numbers come
from ``sccr_tables``. A cell those reprints do not settle stays empty and is
flagged for the user.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from panelcad.core.models import Component, Device, Project
from panelcad.electrical.sccr_tables import (
    CURRENT_LIMITING_FUSE_CLASSES,
    FUSE_COLUMNS_KA,
    FUSE_IP_CONFLICTS,
    FUSE_IP_KA,
    MOTOR_CONTROLLER_BANDS,
    SB41_KA,
    CITATIONS,
)
from panelcad.render.sheet import parse_pin_ref

_KA_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(kA|A)?", re.I)
_NUM_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)")

_SHEET_RANK = {
    "schematic": 0,
    "io": 1,
    "network": 2,
    "terminal": 3,
    "panel_layout": 4,
    "door_layout": 5,
}

_POWER_CIRCUITS = {"power", "feeder", "branch", "load", "secondary", "primary"}
_POWER_ROLES = {
    "breaker",
    "disconnect",
    "contactor",
    "overload",
    "fuse",
    "bus",
    "transformer",
    "fwd",
    "rev",
    "delta",
    "main",
    "drive",
    "mpcb",
    "softstarter",
    "starter",
    "bypass",
}
_SHOW_EXEMPT_ROLES = {"motor", "star"}

_OCPD_CATEGORIES = (
    "protection.breaker",
    "protection.fuse",
    "protection.mpcb",
    "protection.supplementary",
)


def format_ka(value: float | None) -> str:
    if value is None:
        return ""
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def parse_ka(value: Any) -> float | None:
    """Read a kiloampere figure. A bare number is already kA. ``10000A`` is 10 kA."""

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _KA_RE.search(str(value).replace(",", ""))
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "").upper()
    if unit == "A":
        return number / 1000.0
    return number


def _plain_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUM_RE.search(str(value).replace(",", ""))
    if not match:
        return None
    return float(match.group(1))


def _first(props: dict[str, Any], *keys: str, ka: bool = False) -> float | None:
    for key in keys:
        if key not in props:
            continue
        raw = props.get(key)
        if raw is None or raw == "":
            continue
        parsed = parse_ka(raw) if ka else _plain_float(raw)
        if parsed is not None:
            return parsed
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def normalize_fuse_class(value: Any) -> str:
    text = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    if text.startswith("CLASS"):
        text = text[5:]
    if "300" in text and text.startswith("T"):
        return "T300"
    if "600" in text and text.startswith("T"):
        return "T600"
    return text


def column_index_for(level_ka: float) -> int | None:
    """Table SB4.2 column. Levels above 200 kA are not in the reprinted table."""

    if level_ka <= 50:
        return 0
    if level_ka <= 100:
        return 1
    if level_ka <= 200:
        return 2
    return None


def _row_amps(fuse_class: str, amps: float) -> int | None:
    rows = sorted(rating for (cls, rating) in FUSE_IP_KA if cls == fuse_class)
    if not rows:
        return None
    for rating in rows:
        if amps <= rating + 1e-9:
            return rating
    return None


def peak_let_through(
    fuse_class: str,
    amps: float,
    claim_ka: float,
) -> tuple[float | None, str]:
    """Ip (kA) from Table SB4.2 for one claim level. ``None`` when the cell is blank."""

    cls = normalize_fuse_class(fuse_class)
    note = ""
    classes = [cls]
    if cls == "T":
        classes = ["T300", "J"]
        note = "Class T voltage was not entered; the higher Ip of 300 V T and 600 V T/J is used."
    elif cls == "T600":
        classes = ["J"]
    index = column_index_for(claim_ka)
    if index is None:
        return None, "Table SB4.2 stops at 200 kA."
    ips: list[float] = []
    conflict = ""
    for name in classes:
        row = _row_amps(name, amps)
        if row is None:
            continue
        triple = FUSE_IP_KA.get((name, row))
        if triple is None or triple[index] is None:
            continue
        ips.append(float(triple[index]))
        conflict = conflict or FUSE_IP_CONFLICTS.get((name, row, index), "")
    if not ips:
        return None, note
    ip = max(ips)
    extra = " ".join(part for part in (note, conflict) if part)
    return ip, extra


def motor_controller_rating(
    hp: float | None,
    kw: float | None,
) -> tuple[float | None, str, list[str]]:
    """Table SB4.1 motor-controller band. Gaps use the lower printed band."""

    flags: list[str] = []
    if hp is not None:
        if hp > 1500:
            return None, "", ["Horsepower is above 1500 hp. Enter sccr_ka. The table ends at 901–1500 hp."]
        for lo, hi, _kw_lo, _kw_hi, ka, label in MOTOR_CONTROLLER_BANDS:
            if lo - 1e-9 <= hp <= hi + 1e-9:
                return ka, f"Table SB4.1 motor controller {label}", flags
        lower = [band for band in MOTOR_CONTROLLER_BANDS if band[1] < hp]
        if lower:
            _lo, _hi, _a, _b, ka, label = lower[-1]
            flags.append(
                f"Horsepower {hp:g} sits between printed bands. The lower band {label} is used."
            )
            return ka, f"Table SB4.1 motor controller {label}", flags
    if kw is not None:
        if kw > 1193:
            return None, "", ["kW is above the 672–1193 kW band. Enter sccr_ka."]
        for _lo, _hi, kw_lo, kw_hi, ka, label in MOTOR_CONTROLLER_BANDS:
            if kw_lo - 1e-9 <= kw <= kw_hi + 1e-9:
                return ka, f"Table SB4.1 motor controller {label}", flags
        lower = [band for band in MOTOR_CONTROLLER_BANDS if band[3] < kw]
        if lower:
            _lo, _hi, _a, _b, ka, label = lower[-1]
            flags.append(f"kW {kw:g} sits between printed bands. The lower band {label} is used.")
            return ka, f"Table SB4.1 motor controller {label}", flags
    return None, "", flags


def transformer_secondary_ka(
    *,
    kva: float | None,
    secondary_voltage: float | None,
    phases: int | None,
    impedance_pct: float | None,
    isc_ka: float | None = None,
    voltage_ceiling: float | None = None,
) -> tuple[float | None, str, list[str], str]:
    """Return (threshold kA, source, flags, mode).

    ``mode`` is ``user``, ``formula``, ``screen``, or ``missing``.
    """

    flags = [
        "Tables SB4.3 and SB4.4 are not encoded. The SB4.3.1 formula or a screen is used."
    ]
    if isc_ka is not None:
        return isc_ka, "user transformer_isc_ka", flags, "user"
    if kva is None:
        flags.append("Transformer kVA is not entered. SB4.3.1 was not applied.")
        return None, "", flags, "missing"
    if secondary_voltage is not None and phases in (1, 3):
        z = 2.1 if impedance_pct is None or impedance_pct < 2.1 else impedance_pct
        if impedance_pct is None or impedance_pct < 2.1:
            flags.append("Transformer impedance is missing or below 2.1%. 2.1% is used.")
        if phases == 1:
            ifl = kva * 1000.0 / secondary_voltage
        else:
            ifl = kva * 1000.0 / (secondary_voltage * math.sqrt(3.0))
        isc = ifl * 100.0 / z / 1000.0
        return isc, f"SB4.3.1 ISC {format_ka(isc)} kA (Z={z:g}%)", flags, "formula"
    if secondary_voltage is not None and phases not in (1, 3):
        flags.append("Transformer phases are not 1 or 3. Enter phases or transformer_isc_ka.")
    if kva <= 10 and secondary_voltage is None:
        ceiling = voltage_ceiling
        if kva <= 5 and ceiling is not None and ceiling <= 120:
            return (
                2.0,
                "SB4.3.1 screen: ≤5 kVA and ≤120 V, secondary devices at least 2 kA",
                flags,
                "screen",
            )
        return (
            5.0,
            "SB4.3.1 screen: ≤10 kVA, secondary devices at least 5 kA",
            flags,
            "screen",
        )
    if kva > 10 and secondary_voltage is None:
        flags.append("Secondary voltage is missing and kVA is above 10. Enter secondary_voltage or transformer_isc_ka.")
    return None, "", flags, "missing"


@dataclass
class RatedPart:
    ref: str
    sheet: str
    library_id: str
    name: str
    category: str
    role: str
    circuit: str
    x: float
    y: float
    group: int
    order: int
    base_ka: float | None
    base_source: str
    effective_ka: float | None
    effective_source: str
    limits: bool
    exempt: bool
    flags: list[str]
    kind: str
    fuse_class: str = ""
    fuse_amps: float | None = None
    ir_ka: float | None = None
    let_through_ka: float | None = None
    let_through_at_ka: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "sheet": self.sheet,
            "library_id": self.library_id,
            "name": self.name,
            "category": self.category,
            "role": self.role,
            "circuit": self.circuit,
            "x": self.x,
            "y": self.y,
            "group": self.group,
            "order": self.order,
            "base_ka": self.base_ka,
            "base_source": self.base_source,
            "effective_ka": self.effective_ka,
            "effective_source": self.effective_source,
            "limits": self.limits,
            "exempt": self.exempt,
            "flags": list(self.flags),
            "kind": self.kind,
            "fuse_class": self.fuse_class,
            "fuse_amps": self.fuse_amps,
            "ir_ka": self.ir_ka,
            "let_through_ka": self.let_through_ka,
            "let_through_at_ka": self.let_through_at_ka,
        }


@dataclass
class SccrResult:
    voltage: float | None
    voltage_source: str
    panel_sccr_ka: float | None
    known_minimum_ka: float | None
    limiting_refs: list[str]
    incomplete: bool
    flags: list[str]
    available_fault_ka: float | None
    available_fault_voltage: float | None
    below_available: bool
    components: list[RatedPart]
    citations: list[dict[str, str]] = field(default_factory=list)

    @property
    def panel_text(self) -> str:
        if self.panel_sccr_ka is None:
            return "Panel SCCR incomplete"
        volts = "" if self.voltage is None else f" at {format_ka(self.voltage)} V"
        return f"{format_ka(self.panel_sccr_ka)} kA{volts}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "voltage": self.voltage,
            "voltage_source": self.voltage_source,
            "panel_sccr_ka": self.panel_sccr_ka,
            "panel_text": self.panel_text,
            "known_minimum_ka": self.known_minimum_ka,
            "limiting_refs": list(self.limiting_refs),
            "incomplete": self.incomplete,
            "flags": list(self.flags),
            "available_fault_ka": self.available_fault_ka,
            "available_fault_voltage": self.available_fault_voltage,
            "below_available": self.below_available,
            "components": [part.to_dict() for part in self.components],
            "citations": list(self.citations),
        }


@dataclass
class _Node:
    ref: str
    sheet: str
    sheet_kind: str
    library_id: str
    name: str
    category: str
    role: str
    x: float
    y: float
    props: dict[str, Any]
    device: Device | None
    power_pins: set[str]
    kind: str
    exempt: bool = False
    group: int = 0
    order: int = 0
    circuit: str = ""
    base_ka: float | None = None
    base_source: str = ""
    effective_ka: float | None = None
    effective_source: str = ""
    flags: list[str] = field(default_factory=list)
    fuse_class: str = ""
    fuse_amps: float | None = None
    ir_ka: float | None = None
    let_through_ka: float | None = None
    let_through_at_ka: float | None = None
    high_fault_ka: float | None = None
    high_fault_class: str = ""
    high_fault_max_amps: float | None = None
    limits: bool = False


def _power_pins(device: Device | None) -> set[str]:
    if device is None:
        return set()
    return {pin.id for pin in device.pins if str(pin.electrical) == "power"}


def _ul_type(props: dict[str, Any]) -> str:
    return str(props.get("ul_type") or props.get("sb41") or "").strip().lower().replace(" ", "_")


def _is_ocpd_category(category: str, role: str, props: dict[str, Any]) -> bool:
    ul = _ul_type(props)
    if ul in {"supplementary", "supplementary_protector"}:
        return True
    if role in {"breaker", "fuse", "mpcb"}:
        return True
    return any(category.startswith(prefix) for prefix in _OCPD_CATEGORIES)


def _skip_device(role: str, category: str, power: set[str], props: dict[str, Any]) -> bool:
    # An earth symbol has a power pin and no Table SB4.1 row. Counting it
    # drops the whole panel rating.
    if role == "pe_bar" or category.startswith("connection.ground"):
        return True
    circuit = str(props.get("circuit") or "").strip().lower()
    if circuit in _POWER_CIRCUITS:
        return False
    if role in _SHOW_EXEMPT_ROLES or role in _POWER_ROLES:
        return False
    if category.startswith("power.transformer") or role == "transformer":
        return False
    if category.startswith("connection.bus") or "bus" in category:
        return False
    if category.startswith("power.receptacle") or category.startswith("connection.receptacle"):
        return False
    if category.startswith("drive."):
        return False
    if _ul_type(props):
        return False
    if not power:
        return True
    if category.startswith(
        ("operator.", "timing.", "control.", "switching.aux", "switching.relay", "connection.terminal")
    ):
        return True
    if category.startswith("power.supply") or role == "psu":
        return True
    if category.startswith("load.") or category.startswith("power.load"):
        return False
    return False


def _classify(role: str, category: str, power: set[str], props: dict[str, Any]) -> tuple[str, bool]:
    ul = _ul_type(props)
    if role == "star":
        return "exempt", True
    if role == "motor" or category.startswith("load.motor") or category.startswith("load.heater"):
        return "exempt", True
    if category.startswith("power.load"):
        return "exempt", True
    if category.startswith("power.transformer") or role == "transformer":
        return "transformer", True
    if category.startswith("power.supply") or role == "psu":
        return "exempt", True
    if ul in {"supplementary", "supplementary_protector"} or _is_ocpd_category(category, role, props):
        if category.startswith("protection.fuse_holder"):
            return "component", False
        return "ocpd", False
    if not power and role not in _POWER_ROLES and not ul:
        return "skip", True
    return "component", False


def _mercury_ka(props: dict[str, Any]) -> tuple[float | None, str, list[str]]:
    volts = _first(props, "volts", "voltage", "V")
    amps = _first(props, "amps", "In")
    kva = _first(props, "kva", "kVA")
    if volts is None or amps is None or kva is None:
        return None, "", ["Mercury-tube switch needs volts, amperes, and kVA. Enter sccr_ka."]
    if volts > 250 or amps > 60:
        return SB41_KA["mercury_over_60a_or_250v"], "Table SB4.1 mercury tube switch over 60 A or 250 V", []
    if kva <= 2:
        return SB41_KA["mercury_250v_2kva_or_less"], "Table SB4.1 mercury tube switch ≤250 V and ≤2 kVA", []
    return (
        SB41_KA["mercury_250v_60a_over_2kva"],
        "Table SB4.1 mercury tube switch ≤250 V, ≤60 A, over 2 kVA",
        [],
    )


def _sb41(node: _Node, hp: float | None, kw: float | None) -> tuple[float | None, str, list[str]]:
    category = node.category
    role = node.role
    props = node.props
    ul = _ul_type(props)
    flags: list[str] = []
    if ul in {"supplementary", "supplementary_protector"} or category.startswith("protection.supplementary"):
        return SB41_KA["supplementary_protector"], "Table SB4.1 supplementary protector", flags
    if ul == "mercury" or "mercury" in category:
        return _mercury_ka(props)
    if ul in {"meter", "current_meter"} or "ammeter" in category:
        return None, "", ["A directly connected current meter needs a marked SCCR. Enter sccr_ka."]
    if ul == "meter_socket" or "meter_socket" in category:
        return SB41_KA["meter_socket"], "Table SB4.1 meter socket base", flags
    if ul == "current_shunt" or "shunt" in category:
        return SB41_KA["current_shunt"], "Table SB4.1 current shunt", flags
    if ul == "switch_unit" or "switch_unit" in category:
        return SB41_KA["switch_unit"], "Table SB4.1 switch unit", flags
    if category.startswith("protection.breaker") or category.startswith("protection.mpcb") or role in {
        "breaker",
        "mpcb",
    }:
        standard = ""
        if node.device is not None:
            standard = str(node.device.properties.get("standard") or node.device.standard or "")
        if node.library_id.startswith("iec.") or "60898" in standard or "60947" in standard:
            flags.append(
                "IEC Icu on the symbol is not a UL interrupting rating. "
                "Table SB4.1 circuit breaker 5 kA is used. A supplementary protector would be 0.2 kA."
            )
        return SB41_KA["circuit_breaker"], "Table SB4.1 circuit breaker", flags
    if category.startswith("protection.fuse_holder") or role == "fuse_holder":
        return SB41_KA["fuse_holder"], "Table SB4.1 fuse holder", flags
    if category.startswith("protection.fuse") or role == "fuse" or ul == "miniature_fuse":
        miniature = ul in {"miniature", "miniature_fuse"} or "miniature" in category
        fuse_class = normalize_fuse_class(props.get("fuse_class"))
        if miniature or fuse_class in {"MINI", "MIDGET", "MINIATURE"}:
            flags.append("Table SB4.1 miniature or miscellaneous fuse is 10 kA and is limited to 125 V circuits.")
            return SB41_KA["miniature_fuse"], "Table SB4.1 miniature or miscellaneous fuse", flags
        return None, "", ["Fuse interrupting rating is not in Table SB4.1. Enter ir_ka."]
    if category.startswith("protection.overload") or role == "overload":
        return SB41_KA["overload_relay"], "Table SB4.1 overload relay", flags
    if category.startswith("protection.disconnect") or role == "disconnect" or ul == "switch":
        return SB41_KA["switch"], "Table SB4.1 switch", flags
    if category.startswith("switching.contactor") or role in {"contactor", "fwd", "rev", "delta", "main", "bypass"}:
        ka, source, extra = motor_controller_rating(hp, kw)
        if ka is None and not extra:
            extra = ["Horsepower is not on the device. The 0–50 hp band (5 kA) is used."]
            ka = 5.0
            source = "Table SB4.1 motor controller 0–50 hp (0–37.3 kW)"
        return ka, source, flags + extra
    if category.startswith("connection.bus") or ul in {"bus", "bus_bars"} or role == "bus":
        return SB41_KA["bus_bars"], "Table SB4.1 bus bars", flags
    if (
        category.startswith("connection.terminal")
        or category.startswith("connection.distribution")
        or ul in {"terminal", "pdb", "terminal_block"}
        or "pdb" in category
    ):
        return SB41_KA["terminal_or_pdb"], "Table SB4.1 terminal block or power distribution block", flags
    if "receptacle" in category or ul in {"receptacle", "gfci", "gfci_receptacle"}:
        gfci = ul in {"gfci", "gfci_receptacle"} or _truthy(props.get("gfci")) or "gfci" in category
        if gfci:
            return SB41_KA["receptacle_gfci"], "Table SB4.1 receptacle and GFCI", flags
        return SB41_KA["receptacle"], "Table SB4.1 receptacle other than GFCI", flags
    if category.startswith("drive.") or role in {"drive", "softstarter"}:
        return None, "", ["No Table SB4.1 row for this drive. Enter sccr_ka."]
    return None, "", ["No Table SB4.1 row for this component. Enter sccr_ka."]


def _catalog_part(props: dict[str, Any], parts: Any) -> Any:
    if parts is None:
        return None
    catalog = str(props.get("part_number") or props.get("catalog") or "").strip()
    manufacturer = str(props.get("manufacturer") or "").strip()
    if not catalog:
        key = str(props.get("catalog_key") or "")
        if "|" in key:
            manufacturer, catalog = key.split("|", 1)
    if not catalog:
        return None
    try:
        return parts.get(catalog, manufacturer or None)
    except (KeyError, TypeError):
        return None


def _explicit_sccr(props: dict[str, Any], catalog_part: Any) -> tuple[float | None, str, bool]:
    """Instance value, then catalog. The bool is true when the figure is a bare high-fault string."""

    entered = _first(props, "sccr_ka", ka=True)
    if entered is not None:
        return entered, "entered sccr_ka", False
    text = props.get("sccr")
    parsed = parse_ka(text) if text not in (None, "") else None
    if parsed is not None:
        return parsed, "entered sccr", True
    if catalog_part is not None and getattr(catalog_part, "sccr_ka", None) is not None:
        return float(catalog_part.sccr_ka), "catalog sccr_ka", False
    if catalog_part is not None and getattr(catalog_part, "sccr", ""):
        parsed = parse_ka(catalog_part.sccr)
        if parsed is not None:
            return parsed, f"catalog SCCR {catalog_part.sccr}", True
    return None, "", False


def _ocpd_interrupt(node: _Node, catalog_part: Any) -> tuple[float | None, str, list[str]]:
    flags: list[str] = []
    props = node.props
    for key, label in (
        ("ir_ka", "entered ir_ka"),
        ("sccr_ka", "entered sccr_ka"),
    ):
        value = _first(props, key, ka=True)
        if value is not None:
            return value, label, flags
    if props.get("sccr") not in (None, ""):
        parsed = parse_ka(props.get("sccr"))
        if parsed is not None:
            flags.append("SB4.2.3: confirm this interrupting rating is the marked value for the installed device.")
            return parsed, "entered sccr", flags
    for key in ("ka", "Icu"):
        if key in props and props.get(key) not in (None, ""):
            parsed = parse_ka(props.get(key))
            if parsed is not None:
                flags.append(
                    "Instance ka/Icu is used as the interrupting rating. Confirm it is a UL rating, not an IEC Icu."
                )
                return parsed, f"instance {key}", flags
    if catalog_part is not None:
        if getattr(catalog_part, "sccr_ka", None) is not None:
            return float(catalog_part.sccr_ka), "catalog sccr_ka", flags
        if getattr(catalog_part, "sccr", ""):
            parsed = parse_ka(catalog_part.sccr)
            if parsed is not None:
                flags.append("SB4.2.3: catalog SCCR is used only with the specified protective device. Confirm it.")
                return parsed, f"catalog SCCR {catalog_part.sccr}", flags
        if getattr(catalog_part, "ka", None) is not None:
            flags.append(
                "Catalog ka is used as the interrupting rating. Confirm it is a UL rating, not an IEC Icu."
            )
            return float(catalog_part.ka), "catalog ka", flags
    return None, "", flags


def _horsepower(node: _Node, nodes: list[_Node]) -> tuple[float | None, float | None]:
    hp = _first(node.props, "hp", "HP")
    kw = _first(node.props, "kW", "kw")
    if hp is not None or kw is not None:
        return hp, kw
    for other in nodes:
        if other.group != node.group:
            continue
        if other.role == "motor" or other.category.startswith("load.motor"):
            hp = _first(other.props, "hp", "HP")
            kw = _first(other.props, "kW", "kw")
            if hp is not None or kw is not None:
                return hp, kw
    return None, None


def _fuse_fields(node: _Node) -> None:
    if node.kind != "ocpd":
        return
    if not (node.category.startswith("protection.fuse") or node.role == "fuse"):
        return
    node.fuse_class = normalize_fuse_class(node.props.get("fuse_class"))
    node.fuse_amps = _first(node.props, "fuse_amps", "amps", ka=False)
    if node.fuse_amps is None:
        node.fuse_amps = _plain_float(node.props.get("In"))
    entered_ir = _first(node.props, "ir_ka", ka=True)
    if entered_ir is not None:
        node.ir_ka = entered_ir
    elif node.ir_ka is None and node.base_ka is not None and str(node.base_source).startswith("entered"):
        node.ir_ka = node.base_ka
    node.let_through_ka = _first(node.props, "let_through_ka", ka=True)
    node.let_through_at_ka = _first(node.props, "let_through_at_ka", ka=True)


def _rate_node(node: _Node, nodes: list[_Node], parts: Any) -> None:
    catalog_part = _catalog_part(node.props, parts)
    explicit, explicit_source, from_string = _explicit_sccr(node.props, catalog_part)
    combo_class = str(node.props.get("sccr_with_class") or "").strip()
    combo_amps = _first(node.props, "sccr_with_max_amps")
    if explicit is not None and combo_class and combo_amps is not None:
        node.high_fault_ka = explicit
        node.high_fault_class = normalize_fuse_class(combo_class)
        node.high_fault_max_amps = combo_amps
        explicit = None
        from_string = False
    flags: list[str] = []
    if node.kind == "ocpd":
        value, source, flags = _ocpd_interrupt(node, catalog_part)
        if value is None:
            hp, kw = _horsepower(node, nodes)
            value, source, extra = _sb41(node, hp, kw)
            flags.extend(extra)
        node.base_ka = value
        node.base_source = source
        node.flags.extend(flags)
        node.ir_ka = value
        node.let_through_ka = _first(node.props, "let_through_ka", ka=True)
        node.let_through_at_ka = _first(node.props, "let_through_at_ka", ka=True)
        _fuse_fields(node)
        if node.category.startswith("protection.fuse") or node.role == "fuse":
            if node.ir_ka is None and node.base_ka is None:
                node.flags.append("Enter ir_ka. A fuse interrupting rating is not assumed.")
            elif node.base_source.startswith("Table SB4.1 miniature"):
                node.ir_ka = node.base_ka
        elif _truthy(node.props.get("current_limiting")) and node.let_through_ka is None:
            node.flags.append(
                "Listed current-limiting breaker needs published let-through "
                "(let_through_ka at let_through_at_ka). No boost was applied."
            )
    elif node.exempt and node.kind != "transformer":
        node.base_source = "exempt from the SCCR minimum"
        if node.role == "star":
            node.flags.append("Wye-delta S contactor is omitted from the SCCR minimum.")
        elif node.role == "motor" or node.category.startswith("load."):
            node.flags.append("Field load is omitted from the SCCR minimum.")
    else:
        if explicit is not None:
            node.base_ka = explicit
            node.base_source = explicit_source
            if from_string:
                node.flags.append(
                    "SB4.2.3: a high-fault SCCR applies only with the specified fuse or breaker. Confirm the combination."
                )
        else:
            hp, kw = _horsepower(node, nodes)
            value, source, extra = _sb41(node, hp, kw)
            node.base_ka = value
            node.base_source = source
            node.flags.extend(extra)
    node.effective_ka = node.base_ka
    node.effective_source = node.base_source


def _collect(project: Project, catalog: Any) -> list[_Node]:
    found: dict[str, _Node] = {}
    for sheet in project.sheets:
        rank = _SHEET_RANK.get(sheet.kind, 9)
        for comp in sheet.components:
            device = _device(catalog, comp)
            power = _power_pins(device)
            role = str(comp.properties.get("role") or "")
            category = device.category if device is not None else ""
            if _skip_device(role, category, power, comp.properties):
                continue
            kind, exempt = _classify(role, category, power, comp.properties)
            if kind == "skip":
                continue
            node = _Node(
                ref=comp.id,
                sheet=sheet.id,
                sheet_kind=sheet.kind,
                library_id=comp.library_id,
                name=device.name if device is not None else comp.library_id,
                category=category,
                role=role,
                x=float(comp.x),
                y=float(comp.y),
                props=dict(comp.properties),
                device=device,
                power_pins=power,
                kind=kind,
                exempt=exempt,
            )
            current = found.get(comp.id)
            if current is None or rank < _SHEET_RANK.get(current.sheet_kind, 9):
                found[comp.id] = node
    return list(found.values())


def _device(catalog: Any, comp: Component) -> Device | None:
    if catalog is None:
        return None
    try:
        return catalog.get(comp.library_id)
    except KeyError:
        return None


def _order_key(node: _Node) -> tuple[float, float, str]:
    return (node.y, node.x, node.ref)


def _connect(project: Project, nodes: list[_Node]) -> dict[str, set[str]]:
    by_ref = {node.ref: node for node in nodes}
    adj: dict[str, set[str]] = {node.ref: set() for node in nodes}
    groups: dict[str, str] = {}
    for node in nodes:
        group = str(node.props.get("sccr_group") or "").strip()
        if group:
            groups.setdefault(group, node.ref)
            adj[node.ref].add(groups[group])
            adj[groups[group]].add(node.ref)
    for sheet in project.sheets:
        for wire in sheet.wires:
            try:
                left_ref, left_pin = parse_pin_ref(wire.frm)
                right_ref, right_pin = parse_pin_ref(wire.to)
            except ValueError:
                continue
            left = by_ref.get(left_ref)
            right = by_ref.get(right_ref)
            if left is None or right is None or left.ref == right.ref:
                continue
            if left_pin not in left.power_pins or right_pin not in right.power_pins:
                continue
            adj[left.ref].add(right.ref)
            adj[right.ref].add(left.ref)
    return adj


def _groups(nodes: list[_Node], adj: dict[str, set[str]]) -> None:
    seen: set[str] = set()
    index = 0
    by_ref = {node.ref: node for node in nodes}
    for node in nodes:
        if node.ref in seen:
            continue
        stack = [node.ref]
        members: list[_Node] = []
        seen.add(node.ref)
        while stack:
            current = stack.pop()
            members.append(by_ref[current])
            for nxt in adj[current]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        members.sort(key=_order_key)
        for order, member in enumerate(members):
            member.group = index
            member.order = order
        index += 1


def _downstream(nodes: list[_Node], adj: dict[str, set[str]]) -> dict[str, set[str]]:
    by_ref = {node.ref: node for node in nodes}
    out: dict[str, set[str]] = {}
    for node in nodes:
        seen: set[str] = set()
        stack = [node.ref]
        while stack:
            current = stack.pop()
            for nxt in adj[current]:
                if nxt in seen or nxt == node.ref:
                    continue
                if _order_key(by_ref[nxt]) <= _order_key(by_ref[current]):
                    continue
                seen.add(nxt)
                stack.append(nxt)
        out[node.ref] = seen
    return out


def _label_circuits(nodes: list[_Node], down: dict[str, set[str]]) -> set[str]:
    ocpd = {node.ref for node in nodes if node.kind == "ocpd"}
    branch = {ref for ref in ocpd if not (down[ref] & ocpd)}
    for node in nodes:
        explicit = str(node.props.get("circuit") or "").strip().lower()
        if explicit in _POWER_CIRCUITS:
            node.circuit = explicit
            continue
        if node.ref in branch or any(node.ref in down[ref] for ref in branch):
            node.circuit = "branch"
        elif down[node.ref] & branch:
            node.circuit = "feeder"
        elif node.kind == "transformer":
            node.circuit = "transformer"
        else:
            node.circuit = "power"
    return branch


def _fuse_claim(node: _Node, base_ka: float) -> tuple[float | None, str, list[str]]:
    flags: list[str] = []
    if node.fuse_class in {"R"}:
        flags.append("Class R is not a current-limiting class for SB4.3 unless it is RK1 or RK5.")
        return None, "", flags
    if node.fuse_class and node.fuse_class not in CURRENT_LIMITING_FUSE_CLASSES | {"T300", "T600"}:
        if node.let_through_ka is None:
            flags.append(
                f"Fuse class {node.fuse_class} is not in Table SB4.2. Enter let_through_ka and let_through_at_ka."
            )
    ir = node.ir_ka
    if ir is None:
        return None, "", flags
    if ir > 200:
        flags.append("Fuse interrupting rating is above 200 kA. Table SB4.2 has no column above 200 kA.")
    if node.fuse_amps is None or not node.fuse_class:
        flags.append("Enter fuse_class and fuse_amps to use Table SB4.2.")
        return None, "", flags
    levels: list[float] = [float(level) for level in FUSE_COLUMNS_KA if level <= ir + 1e-9]
    if all(abs(ir - float(level)) > 1e-6 for level in FUSE_COLUMNS_KA) and ir <= 200:
        levels.append(float(ir))
    best: tuple[float, str] | None = None
    notes: list[str] = []
    for level in levels:
        ip, note = peak_let_through(node.fuse_class, node.fuse_amps, level)
        if note:
            notes.append(note)
        if ip is None:
            continue
        if ip <= base_ka + 1e-9 and (best is None or level > best[0]):
            best = (
                level,
                f"Table SB4.2 {node.fuse_class} {format_ka(node.fuse_amps)} A Ip {format_ka(ip)} kA "
                f"at {format_ka(level)} kA",
            )
    if best is None:
        return None, "", flags + notes
    return best[0], best[1], flags + notes


def _published_claim(node: _Node, base_ka: float) -> tuple[float | None, str]:
    if node.let_through_ka is None or node.let_through_at_ka is None:
        return None, ""
    if node.let_through_ka > base_ka + 1e-9:
        return None, ""
    claim = float(node.let_through_at_ka)
    if node.ir_ka is not None:
        claim = min(claim, float(node.ir_ka))
    return claim, (
        f"published let-through {format_ka(node.let_through_ka)} kA "
        f"at {format_ka(node.let_through_at_ka)} kA"
    )


def _apply_let_through(nodes: list[_Node], down: dict[str, set[str]]) -> None:
    by_ref = {node.ref: node for node in nodes}
    for target in nodes:
        if target.exempt or target.kind in {"ocpd", "transformer"} or target.base_ka is None:
            continue
        best_ka = target.effective_ka
        best_source = target.effective_source
        for ref, source in by_ref.items():
            if target.ref not in down.get(ref, ()):
                continue
            if source.kind != "ocpd":
                continue
            claims: list[tuple[float, str]] = []
            if source.fuse_class or source.category.startswith("protection.fuse") or source.role == "fuse":
                claim, text, notes = _fuse_claim(source, target.base_ka)
                target.flags.extend(note for note in notes if note not in target.flags)
                if claim is not None and text:
                    claims.append((claim, text))
            published, published_text = _published_claim(source, target.base_ka)
            if published is not None:
                claims.append((published, published_text))
            for claim, text in claims:
                if best_ka is None or claim > best_ka + 1e-9:
                    best_ka = claim
                    best_source = text
        target.effective_ka = best_ka
        target.effective_source = best_source


def _apply_high_fault(nodes: list[_Node], down: dict[str, set[str]]) -> None:
    by_ref = {node.ref: node for node in nodes}
    for node in nodes:
        if node.high_fault_ka is None:
            continue
        matched = False
        for ref in down:
            if node.ref not in down[ref]:
                continue
            fuse = by_ref[ref]
            if not fuse.fuse_class:
                continue
            amps = fuse.fuse_amps if fuse.fuse_amps is not None else math.inf
            if fuse.fuse_class == node.high_fault_class and amps <= (node.high_fault_max_amps or 0) + 1e-9:
                matched = True
                break
        if matched:
            node.base_ka = node.high_fault_ka
            node.base_source = (
                f"high-fault SCCR with {node.high_fault_class} up to {format_ka(node.high_fault_max_amps)} A"
            )
            node.effective_ka = max(node.effective_ka or 0, node.high_fault_ka)
            node.effective_source = node.base_source
        else:
            node.flags.append(
                "High-fault SCCR applies only with "
                f"{node.high_fault_class} at or below {format_ka(node.high_fault_max_amps)} A. "
                "That device was not found upstream. The standard-fault rating is kept."
            )


def _phases_of(node: _Node) -> int | None:
    phases = _first(node.props, "phases")
    if phases in (1, 3) or (phases is not None and int(phases) in (1, 3)):
        return int(phases)
    if node.device is None:
        return None
    pins = {pin.id.upper() for pin in node.device.pins}
    if {"H1", "H2", "X1", "X2"} <= pins and "X3" not in pins and "H3" not in pins:
        return 1
    return None


def _apply_transformers(nodes: list[_Node], down: dict[str, set[str]]) -> None:
    by_ref = {node.ref: node for node in nodes}
    for node in nodes:
        if node.kind != "transformer":
            continue
        kva = _first(node.props, "kva", "kVA")
        if kva is None and node.props.get("VA") not in (None, ""):
            va = _plain_float(node.props.get("VA"))
            kva = None if va is None else va / 1000.0
        voltage = _first(node.props, "secondary_voltage", "secondary_v")
        ceiling = _first(node.props, "secondary_voltage_ceiling")
        impedance = _first(node.props, "impedance_pct", "z_pct", "Z")
        isc_user = _first(node.props, "transformer_isc_ka", ka=True)
        threshold, source, flags, mode = transformer_secondary_ka(
            kva=kva,
            secondary_voltage=voltage,
            phases=_phases_of(node),
            impedance_pct=impedance,
            isc_ka=isc_user,
            voltage_ceiling=ceiling,
        )
        node.flags.extend(flags)
        node.base_source = source or "transformer body exempt"
        if threshold is None or mode == "missing":
            node.flags.append("Enter kVA, secondary_voltage, and phases, or transformer_isc_ka.")
            continue
        secondary = [
            by_ref[ref]
            for ref in down[node.ref]
            if not by_ref[ref].exempt and by_ref[ref].kind != "transformer"
        ]
        if any(part.effective_ka is None or part.effective_ka + 1e-9 < threshold for part in secondary):
            node.flags.append(
                f"A secondary component is below {format_ka(threshold)} kA ({source}). "
                "Secondary ratings stay in the minimum."
            )
            continue
        primary = [
            other
            for other in nodes
            if other.kind == "ocpd" and node.ref in down.get(other.ref, ())
        ]
        primary.sort(key=_order_key)
        if not primary:
            node.flags.append("No primary overcurrent device was found. SB4.3.1 was not applied.")
            continue
        primary_ir = primary[-1].effective_ka
        if primary_ir is None:
            node.flags.append("Primary overcurrent device has no interrupting rating. SB4.3.1 was not applied.")
            continue
        for part in secondary:
            if part.effective_ka is None or primary_ir > part.effective_ka + 1e-9:
                part.effective_ka = primary_ir
                part.effective_source = f"SB4.3.1 transformer ({source})"
                part.flags.append(
                    "SB4.3.1: secondary rating does not reduce this circuit below the primary device."
                )


def _voltage(project: Project, nodes: list[_Node]) -> tuple[float | None, str, list[str]]:
    entered = _first(project.properties, "sccr_voltage")
    if entered is not None:
        return entered, "project sccr_voltage", []
    for node in nodes:
        if node.role == "motor" or node.category.startswith("load.motor"):
            volts = _first(node.props, "voltage", "volts")
            if volts is not None:
                return volts, "motor nameplate voltage", []
    return 480.0, "assumed 480 V", ["Panel voltage was not on the drawing. 480 V is a stand-in. Enter sccr_voltage."]


def compute_sccr(project: Project, catalog: Any = None, parts: Any = None) -> SccrResult:
    """Rate the power circuit. Does not write the project."""

    nodes = _collect(project, catalog)
    adj = _connect(project, nodes)
    _groups(nodes, adj)
    for node in nodes:
        _rate_node(node, nodes, parts)
    down = _downstream(nodes, adj)
    _label_circuits(nodes, down)
    _apply_high_fault(nodes, down)
    _apply_let_through(nodes, down)
    _apply_transformers(nodes, down)

    voltage, voltage_source, voltage_flags = _voltage(project, nodes)
    required = [node for node in nodes if not node.exempt]
    missing = [node for node in required if node.effective_ka is None]
    known = [node.effective_ka for node in required if node.effective_ka is not None]
    known_min = min(known) if known else None
    incomplete = bool(missing)
    panel = None if incomplete or not required else known_min
    if panel is not None:
        for node in required:
            node.limits = node.effective_ka is not None and abs(node.effective_ka - panel) < 1e-6
    else:
        for node in missing:
            node.limits = True

    available = _first(project.properties, "available_fault_ka", ka=True)
    available_v = _first(project.properties, "available_fault_voltage")
    below = False
    flags = list(voltage_flags)
    if available is not None and (panel is None or panel + 1e-9 < available):
        below = True
    for node in nodes:
        for note in node.flags:
            if note not in flags:
                flags.append(note)
    limiting = [node.ref for node in nodes if node.limits]
    rated = [
        RatedPart(
            ref=node.ref,
            sheet=node.sheet,
            library_id=node.library_id,
            name=node.name,
            category=node.category,
            role=node.role,
            circuit=node.circuit,
            x=node.x,
            y=node.y,
            group=node.group,
            order=node.order,
            base_ka=node.base_ka,
            base_source=node.base_source,
            effective_ka=node.effective_ka,
            effective_source=node.effective_source,
            limits=node.limits,
            exempt=node.exempt,
            flags=list(node.flags),
            kind=node.kind,
            fuse_class=node.fuse_class,
            fuse_amps=node.fuse_amps,
            ir_ka=node.ir_ka,
            let_through_ka=node.let_through_ka,
            let_through_at_ka=node.let_through_at_ka,
        )
        for node in sorted(nodes, key=lambda item: (item.group, item.order, item.ref))
    ]
    citations = [
        {"id": item.id, "section": item.section, "url": item.url, "used_for": item.used_for}
        for item in CITATIONS
    ]
    return SccrResult(
        voltage=voltage,
        voltage_source=voltage_source,
        panel_sccr_ka=panel,
        known_minimum_ka=known_min,
        limiting_refs=limiting,
        incomplete=incomplete,
        flags=flags,
        available_fault_ka=available,
        available_fault_voltage=available_v,
        below_available=below,
        components=rated,
        citations=citations,
    )


def nameplate_text(project: Project, catalog: Any = None, parts: Any = None) -> str:
    """SCCR line for the title block. Empty until the panel has a numeric rating."""

    result = compute_sccr(project, catalog, parts)
    if result.panel_sccr_ka is None or result.voltage is None:
        return ""
    return f"SCCR {format_ka(result.panel_sccr_ka)} kA @ {format_ka(result.voltage)} V"


def sccr_findings(project: Project, catalog: Any = None, parts: Any = None) -> list[Any]:
    """Warn when a user-entered available fault current is above the panel SCCR."""

    from panelcad.core.checks import Finding

    result = compute_sccr(project, catalog, parts)
    if result.available_fault_ka is None or not result.below_available:
        return []
    if result.panel_sccr_ka is None:
        message = (
            "Panel SCCR is incomplete, so it cannot be shown to meet the available fault "
            f"current of {format_ka(result.available_fault_ka)} kA (NEC 409.22)."
        )
    else:
        message = (
            f"Panel SCCR {format_ka(result.panel_sccr_ka)} kA at {format_ka(result.voltage)} V "
            f"is below the available fault current of {format_ka(result.available_fault_ka)} kA "
            "(UL 508A SB4.4, NEC 409.22)."
        )
    if (
        result.available_fault_voltage is not None
        and result.voltage is not None
        and abs(result.available_fault_voltage - result.voltage) > 1e-6
    ):
        message += (
            f" Available fault is entered at {format_ka(result.available_fault_voltage)} V."
        )
    refs = ", ".join(result.limiting_refs)
    return [
        Finding(
            "sccr_below_available",
            "warning",
            message,
            ref=refs,
        )
    ]
