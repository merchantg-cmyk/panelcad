"""Whether a schematic sheet is a 3-phase power feeder.

Kept out of the conversion package so opening a project does not import the
importer, OCR, or the circuit builder.
"""

from __future__ import annotations

from typing import Any

_POWER_LIB_TOKENS = (
    "motors.",
    "loads.motor",
    "breakers.mcb_3p",
    "breakers.mccb",
    "contactors.ac3",
    "contactors.three_pole",
    "contactors.power",
    "protection.overload_3p",
    "protection.overload_9a",
    "protection.overload",
    "protection.thermal_ol",
    "drives.",
    "starters.",
    "disconnect",
)
_POWER_ROLES = frozenset(
    {
        "motor",
        "contactor",
        "overload",
        "breaker",
        "drive",
        "softstarter",
        "star",
        "delta",
        "main_contactor",
        "fwd",
        "rev",
        "bypass",
    }
)


def is_power_sheet(sheet: Any, parts: list[Any] | None = None) -> bool:
    """True when a schematic sheet contains 3-phase power feeder components."""

    roles = {part.role for part in (parts or []) if getattr(part, "role", None)}
    if roles & _POWER_ROLES:
        return True
    for comp in getattr(sheet, "components", []) or []:
        role = str(comp.properties.get("role") or "").strip().lower()
        if role in _POWER_ROLES:
            return True
        lid = str(getattr(comp, "library_id", "") or "").lower()
        if lid.endswith((".ol_contact", ".aux_no", ".aux_nc", ".coil")):
            continue
        if any(tok in lid for tok in _POWER_LIB_TOKENS):
            return True
    return False
