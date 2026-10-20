"""Pick a preferred manufacturer's catalog SKUs onto a Circuit Builder insert.

The schematic symbol stays generic. Only published fields are used: amp (or
adjustable range), coil text, voltage words in the description, and category.
"""

from __future__ import annotations

import re
from typing import Any

from panelcad.library.parts import CatalogPart, normalize_manufacturer

_HV = re.compile(r"\b(380|400|415|440|460|480|575|600)\b")
_LV = re.compile(r"\b(200|208|220|230|240)\b")

_CONTACTOR_ROLES = frozenset(
    {"contactor", "fwd", "rev", "main", "star", "delta", "bypass", "line"}
)
_NO_ROLES = frozenset({"start", "fwd_pb", "rev_pb"})
_NC_ROLES = frozenset({"stop"})
_PILOT_ROLES = frozenset({"pilot", "pilot_run", "pilot_fault", "pilot_fwd", "pilot_rev"})


_SECONDARY_OFF = frozenset({"", "none", "off"})
_DEFAULT_SECONDARY = "AutomationDirect"


def apply_preferred_parts(
    session: Any,
    result: dict[str, Any],
    manufacturer: str,
    *,
    voltage: float | None = None,
    control_voltage: float | None = None,
    fla: float | None = None,
    secondary_manufacturer: str | None = None,
) -> list[dict[str, Any]]:
    """Assign catalog SKUs for *manufacturer* onto the circuit that was just built.

    Categories the primary brand does not publish are filled from
    *secondary_manufacturer*. None uses the project enclosure maker when that
    name is a parts brand, otherwise AutomationDirect. ``none`` / ``off`` /
    ``""`` leave those symbols generic. Called while the insert undo lock is
    held, so one undo removes the circuit and the SKUs. Unknown manufacturers
    raise ValueError.
    """

    name = normalize_manufacturer(manufacturer)
    if not name:
        raise ValueError("manufacturer is empty")
    pool = session.parts.search(manufacturer=name)
    if not pool:
        raise ValueError(f"unknown manufacturer '{manufacturer}'")
    secondary = _resolve_secondary(session, name, secondary_manufacturer)
    pool2 = session.parts.search(manufacturer=secondary) if secondary else []

    kind = str(result.get("kind") or "")
    placement = str(result.get("overload_placement") or "")
    fla_v = _float_or_none(fla if fla is not None else result.get("fla"))
    volt = _float_or_none(voltage)
    if volt is None:
        volt = 400.0 if str(result.get("standard") or "") == "IEC" else 480.0
    ctrl = _float_or_none(control_voltage)
    components = list(result.get("components") or [])
    if ctrl is None and any(str(row.get("role")) == "psu" for row in components):
        ctrl = 24.0
    ol_amps = fla_v
    if fla_v is not None and kind == "star_delta" and placement != "line":
        ol_amps = fla_v * 0.58

    assigned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in components:
        ref = str(row.get("id") or "")
        role = str(row.get("role") or "")
        library_id = str(row.get("library_id") or "")
        if not ref or ref in seen:
            continue
        if role in {"motor", "transformer"} or role.endswith("_fb") or role.endswith("_sense"):
            continue
        spec = _role_spec(role)
        if spec is None:
            continue
        seen.add(ref)
        part = _choose(
            pool,
            category=spec[0],
            mode=spec[1],
            role=role,
            library_id=library_id,
            fla=fla_v,
            ol_amps=ol_amps,
            voltage=volt,
            control_voltage=ctrl,
        )
        if part is None and pool2:
            part = _choose(
                pool2,
                category=spec[0],
                mode=spec[1],
                role=role,
                library_id=library_id,
                fla=fla_v,
                ol_amps=ol_amps,
                voltage=volt,
                control_voltage=ctrl,
            )
            if part is not None:
                written = session.assign_part(ref, part.catalog, manufacturer=part.manufacturer)
                _mark_fallback(session, ref, primary=name)
                assigned.append(
                    {
                        "id": ref,
                        "role": role,
                        "manufacturer": written["manufacturer"],
                        "catalog": written["catalog"],
                        "verified": written["verified"],
                        "source": written["source"],
                        "library_id": written["library_id"],
                        "catalog_fallback": "yes",
                        "catalog_primary": name,
                    }
                )
                continue
        if part is None:
            assigned.append(
                {
                    "id": ref,
                    "role": role,
                    "manufacturer": name,
                    "catalog": "",
                    "reason": f"no {name} {spec[0]} matches this circuit",
                }
            )
            continue
        written = session.assign_part(ref, part.catalog, manufacturer=part.manufacturer)
        assigned.append(
            {
                "id": ref,
                "role": role,
                "manufacturer": written["manufacturer"],
                "catalog": written["catalog"],
                "verified": written["verified"],
                "source": written["source"],
                "library_id": written["library_id"],
            }
        )
    return assigned


def _resolve_secondary(session: Any, primary: str, requested: str | None) -> str | None:
    """Return a parts brand that may fill categories *primary* does not make."""

    if requested is not None:
        token = str(requested).strip()
        if token.casefold() in _SECONDARY_OFF:
            return None
        name = normalize_manufacturer(token)
        if not name or not session.parts.search(manufacturer=name):
            raise ValueError(f"unknown manufacturer '{requested}'")
        if name.casefold() == primary.casefold():
            return None
        return name
    project = _project_parts_vendor(session)
    if project and project.casefold() != primary.casefold():
        return project
    fallback = normalize_manufacturer(_DEFAULT_SECONDARY)
    if not fallback or fallback.casefold() == primary.casefold():
        return None
    if not session.parts.search(manufacturer=fallback):
        return None
    return fallback


def _project_parts_vendor(session: Any) -> str | None:
    """Enclosure maker, when that name is also a parts brand with catalog rows."""

    project = getattr(session, "project", None)
    sheets = getattr(project, "sheets", None) or []
    for sheet in sheets:
        enclosure = getattr(sheet, "enclosure", None)
        raw = str(getattr(enclosure, "manufacturer", "") or "").strip()
        if not raw:
            continue
        name = normalize_manufacturer(raw)
        if name and session.parts.search(manufacturer=name):
            return name
    return None


def _mark_fallback(session: Any, ref: str, *, primary: str) -> None:
    project = getattr(session, "project", None)
    for sheet in getattr(project, "sheets", None) or []:
        try:
            comp = sheet.component(ref)
        except KeyError:
            continue
        comp.properties["catalog_fallback"] = "yes"
        comp.properties["catalog_primary"] = primary


def _role_spec(role: str) -> tuple[str, str] | None:
    if role in _CONTACTOR_ROLES:
        return ("switching.contactor", "contactor")
    if role == "overload":
        return ("protection.overload", "overload")
    if role == "drive":
        return ("drive.vfd", "drive")
    if role == "softstarter":
        return ("drive.softstarter", "drive")
    if role == "breaker":
        return ("protection.breaker", "breaker")
    if role == "plc":
        return ("control.plc", "plc")
    if role == "psu":
        return ("power.supply", "any")
    if role in _NO_ROLES:
        return ("operator.pushbutton", "no")
    if role in _NC_ROLES:
        return ("operator.pushbutton", "nc")
    if role in _PILOT_ROLES:
        return ("operator.light", "any")
    return None


def _choose(
    pool: list[CatalogPart],
    *,
    category: str,
    mode: str,
    role: str,
    library_id: str,
    fla: float | None,
    ol_amps: float | None,
    voltage: float | None,
    control_voltage: float | None,
) -> CatalogPart | None:
    items = [part for part in pool if part.category == category or part.category.startswith(category + ".")]
    if mode == "drive":
        items = [part for part in items if part.category == category]
        want = _voltage_class(voltage)
        if want:
            classed = [part for part in items if want in _text_classes(part.description)]
            if classed:
                items = classed
            else:
                return None
        need = fla
        rated = [part for part in items if part.amps is not None and (need is None or part.amps + 1e-9 >= need)]
        if not rated:
            return None
        items = rated
        return _best(items, library_id, key=lambda part: (float(part.amps or 0), part.catalog))
    if mode == "contactor":
        need = fla
        rated = [part for part in items if part.amps is not None and (need is None or part.amps + 1e-9 >= need)]
        if not rated:
            return None
        items = _coil_prefer(rated, control_voltage)
        return _best(
            items,
            library_id,
            key=lambda part: (
                float(part.amps or 0),
                _aux_rank(part),
                _dc_rank(part, control_voltage),
                part.catalog,
            ),
        )
    if mode == "overload":
        need = ol_amps
        if need is None:
            return _best(items, library_id, key=lambda part: (float(part.amps_max or part.amps or 0), part.catalog))
        covering = [part for part in items if _covers(part, need)]
        if not covering:
            return None
        return _best(
            covering,
            library_id,
            key=lambda part: (
                _span(part),
                float(part.amps_max if part.amps_max is not None else part.amps or 0),
                part.catalog,
            ),
        )
    if mode == "breaker":
        need = fla
        rated = [part for part in items if part.amps is not None and (need is None or part.amps + 1e-9 >= need)]
        if not rated:
            return None
        return _best(rated, library_id, key=lambda part: (float(part.amps or 0), part.catalog))
    if mode == "no":
        items = [part for part in items if _contact_kind(part) == "no"]
        return _best(items, library_id, key=lambda part: (_color_rank(part, "start"), part.catalog))
    if mode == "nc":
        items = [part for part in items if _contact_kind(part) == "nc"]
        return _best(items, library_id, key=lambda part: (_color_rank(part, "stop"), part.catalog))
    if mode == "plc":
        return _best(items, library_id, key=lambda part: (part.catalog,))
    return _best(items, library_id, key=lambda part: (0 if part.has_footprint() else 1, part.catalog))


def _best(items: list[CatalogPart], library_id: str, *, key) -> CatalogPart | None:
    if not items:
        return None
    want = _suffix(library_id)
    matched = [part for part in items if _suffix(part.library_id) == want]
    pool = matched or items
    return sorted(pool, key=key)[0]


def _coil_prefer(items: list[CatalogPart], control_voltage: float | None) -> list[CatalogPart]:
    if control_voltage is None:
        return items
    strict = [part for part in items if part.coil_voltage and _coil_matches(part.coil_voltage, control_voltage)]
    if strict:
        return strict
    # A published coil that is the wrong voltage is not a match. An empty
    # result lets the secondary brand supply a coil for this control voltage.
    return [
        part
        for part in items
        if not part.coil_voltage or _coil_matches(part.coil_voltage, control_voltage)
    ]


def _coil_matches(coil: str, control_voltage: float) -> bool:
    text = coil.upper().replace(" ", "")
    if control_voltage <= 30:
        return "24" in text and "120" not in text and "240" not in text
    if 100 <= control_voltage <= 130:
        return any(token in text for token in ("110", "115", "120"))
    if 200 <= control_voltage <= 250:
        return any(token in text for token in ("200", "208", "220", "230", "240"))
    return True


def _covers(part: CatalogPart, amps: float) -> bool:
    if part.amps_min is not None and part.amps_max is not None:
        return part.amps_min - 1e-9 <= amps <= part.amps_max + 1e-9
    if part.amps is not None:
        return abs(part.amps - amps) < 1e-6 or part.amps + 1e-9 >= amps
    return False


def _span(part: CatalogPart) -> float:
    if part.amps_min is not None and part.amps_max is not None:
        return float(part.amps_max - part.amps_min)
    return 0.0


def _aux_rank(part: CatalogPart) -> int:
    kind = _contact_kind(part)
    if kind == "nc":
        return 1
    return 0


def _dc_rank(part: CatalogPart, control_voltage: float | None) -> int:
    if control_voltage is None or control_voltage > 30:
        return 0
    return 0 if "DC" in part.coil_voltage.upper() else 1


def _contact_kind(part: CatalogPart) -> str:
    text = part.contact_config.upper().replace(" ", "").replace(".", "")
    has_no = "NO" in text
    has_nc = "NC" in text
    if has_no and not has_nc:
        return "no"
    if has_nc and not has_no:
        return "nc"
    if has_no and has_nc:
        return "both"
    return ""


def _color_rank(part: CatalogPart, want: str) -> int:
    desc = part.description.lower()
    if want == "start":
        if "green" in desc:
            return 0
        if "black" in desc:
            return 1
    if want == "stop" and "red" in desc:
        return 0
    return 2


def _voltage_class(voltage: float | None) -> str | None:
    if voltage is None:
        return None
    if voltage >= 360:
        return "480"
    if voltage >= 180:
        return "230"
    return None


def _text_classes(text: str) -> set[str]:
    found: set[str] = set()
    if _HV.search(text):
        found.add("480")
    if _LV.search(text):
        found.add("230")
    return found


def _suffix(library_id: str) -> str:
    head, _, tail = library_id.lower().partition(".")
    if head in {"iec", "nema", "adc"} and tail:
        return tail
    return library_id.lower()


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
