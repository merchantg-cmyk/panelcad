"""Look up the symbol table. The table is ``standard_map.ENTRIES``."""

from __future__ import annotations

from typing import Any

from panelcad.convert.standard_map import ENTRIES, NEMA_RAIL_ALIASES, RAILS
from panelcad.library.catalog import normalize_standard

_BY_LIB: dict[str, dict[str, Any]] = {}
for _row in ENTRIES:
    _BY_LIB[str(_row["nema"])] = _row
    _BY_LIB[str(_row["iec"])] = _row


def target_standard(value: str) -> str:
    token = normalize_standard(value, default="")
    if token == "IEC":
        return "IEC"
    if token == "NEMA/ANSI":
        return "NEMA/ANSI"
    raise ValueError("target must be iec or nema")


def entry_for(library_id: str) -> dict[str, Any] | None:
    return _BY_LIB.get(str(library_id or ""))


def counterpart(library_id: str, target: str) -> str | None:
    """Library id on ``target``, or None when this symbol has no row."""

    row = entry_for(library_id)
    if row is None:
        return None
    std = target_standard(target)
    return str(row["iec"] if std == "IEC" else row["nema"])


def pin_map(library_id: str, target: str) -> dict[str, str]:
    """Old pin id -> pin id on the target symbol. Identity pins are omitted."""

    row = entry_for(library_id)
    if row is None:
        return {}
    forward = {str(a): str(b) for a, b in (row.get("pins_nema_to_iec") or {}).items()}
    std = target_standard(target)
    current_is_nema = str(library_id) == str(row["nema"])
    if std == "IEC":
        return forward if current_is_nema else {}
    inverse: dict[str, str] = {}
    for src, dst in forward.items():
        inverse[dst] = src
    return {} if current_is_nema else inverse


def extra_pins(library_id: str, target: str) -> set[str]:
    """Pins on this symbol that the target symbol does not draw."""

    row = entry_for(library_id)
    if row is None:
        return set()
    std = target_standard(target)
    if std == "IEC":
        return {str(pin) for pin in (row.get("nema_only") or [])}
    return {str(pin) for pin in (row.get("iec_only") or [])}


def rail_pair_for(kind: str, target: str, *, nema_alias: str = "") -> tuple[str, str]:
    """Hot, return for the target standard.

    ``kind`` is ``dc``, ``ac``, or ``io``. ``nema_alias`` restores L1/L2 or
    +24V/0V when that is the wording the sheet used.
    """

    std = target_standard(target)
    if std == "NEMA/ANSI" and nema_alias in NEMA_RAIL_ALIASES:
        return NEMA_RAIL_ALIASES[nema_alias]
    table = RAILS[std]
    return table.get(kind) or table["dc"]


def nema_alias_for(hot: str, ret: str) -> str:
    pair = (str(hot or ""), str(ret or ""))
    for name, value in NEMA_RAIL_ALIASES.items():
        if value == pair:
            return name
    return ""
