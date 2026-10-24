"""Look up the symbol, role, terminal, and wire-number tables in ``standard_map``."""

from __future__ import annotations

import re
from typing import Any

from panelcad.convert.standard_map import (
    ENTRIES,
    NEMA_RAIL_ALIASES,
    RAILS,
    ROLE_TAG_TABLE,
    TERMINAL_TAG_TABLE,
    WIRE_NUMBER_FORMAT_TABLE,
)
from panelcad.library.catalog import normalize_standard

_BY_LIB: dict[str, dict[str, Any]] = {}
for _row in ENTRIES:
    _BY_LIB[str(_row["nema"])] = _row
    _BY_LIB[str(_row["iec"])] = _row

_BY_ROLE_NAME: dict[str, dict[str, Any]] = {}
_BY_ROLE_TOKEN: dict[str, dict[str, Any]] = {}
_BY_CATEGORY: dict[str, dict[str, Any]] = {}
for _role_row in ROLE_TAG_TABLE:
    _BY_ROLE_NAME[str(_role_row["role"]).lower()] = _role_row
    for _tok in _role_row.get("role_tokens") or ():
        _BY_ROLE_TOKEN.setdefault(str(_tok).lower(), _role_row)
    for _cat in _role_row.get("categories") or ():
        _BY_CATEGORY.setdefault(str(_cat).lower(), _role_row)

_TAG_SPLIT_RE = re.compile(
    r"^(?:=[A-Za-z0-9_.-]+)?(?:\+[A-Za-z0-9_.-]+)?-?([A-Za-z]+)(\d+)(.*)$"
)
_TERMINAL_RE = re.compile(
    r"^(?:=[A-Za-z0-9_.-]+)?(?:\+[A-Za-z0-9_.-]+)?(-?)([A-Za-z]+)(\d+)([:-])([A-Za-z0-9]+)$"
)
_IEC_ADDR_WIRE_RE = re.compile(r"^(?:(\d+)[./:])?(\d+)(?:\.(\d+))?([A-Za-z]?)$")
_NEMA_RUNG_WIRE_RE = re.compile(r"^(\d{3,4})([A-Za-z]?)$")


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


def parse_device_tag(raw_tag: str) -> tuple[str, int, str] | None:
    """Split an IEC or NEMA tag into ``(letters, number, suffix)``.

    Strips optional IEC aspect/location prefixes (``=A1+P1-K1`` -> ``('K', 1, '')``)
    and leading ``-`` so ``-K1``, ``-KT1``, and ``-X1:3`` group with ``K1``,
    ``KT1``, and ``X1:3``.
    """

    text = str(raw_tag or "").strip()
    match = _TAG_SPLIT_RE.match(text)
    if not match:
        return None
    return match.group(1), int(match.group(2)), match.group(3)


def role_entry_for(
    *,
    role: str = "",
    category: str = "",
    library_id: str = "",
    source_prefix: str = "",
) -> dict[str, Any] | None:
    """Look up a ``ROLE_TAG_TABLE`` row by role token, category, library id, or unambiguous prefix."""

    role_key = str(role or "").strip().lower()
    if role_key:
        if role_key in _BY_ROLE_NAME:
            return _BY_ROLE_NAME[role_key]
        if role_key in _BY_ROLE_TOKEN:
            return _BY_ROLE_TOKEN[role_key]
    lib_key = str(library_id or "").strip().lower()
    if lib_key:
        for row in ROLE_TAG_TABLE:
            for token in row.get("library_tokens") or ():
                if str(token).lower() in lib_key:
                    return row
    cat_key = str(category or "").strip().lower()
    if cat_key and cat_key in _BY_CATEGORY:
        return _BY_CATEGORY[cat_key]
    pfx = str(source_prefix or "").strip().upper().lstrip("-")
    if pfx:
        matches = [
            row
            for row in ROLE_TAG_TABLE
            if pfx in {str(a).upper() for a in (row.get("iec_aliases") or ())}
            or pfx in {str(a).upper() for a in (row.get("nema_aliases") or ())}
        ]
        if len(matches) == 1:
            return matches[0]
    return None


def role_prefix_for(
    target: str,
    *,
    role: str = "",
    category: str = "",
    library_id: str = "",
    source_prefix: str = "",
) -> str | None:
    """Return the target standard prefix from ``ROLE_TAG_TABLE``, or None if ambiguous/unmatched."""

    std = target_standard(target)
    row = role_entry_for(
        role=role,
        category=category,
        library_id=library_id,
        source_prefix=source_prefix,
    )
    if row is None:
        return None
    if std == "IEC":
        src = str(source_prefix or "").strip().upper().lstrip("-")
        if src == "KT" and "KT" in (row.get("iec_aliases") or ()):
            return "KT"
        return str(row["iec_prefix"])
    return str(row["nema_prefix"])


def rename_tag_by_role(
    raw_tag: str,
    target: str,
    *,
    role: str = "",
    category: str = "",
    library_id: str = "",
) -> str:
    """Rename a single device or terminal tag using ``TERMINAL_TAG_TABLE`` and ``ROLE_TAG_TABLE``."""

    std = target_standard(target)
    term = convert_terminal_tag(raw_tag, std)
    if term is not None:
        return term
    parsed = parse_device_tag(raw_tag)
    if parsed is None:
        return str(raw_tag or "").strip()
    letters, number, suffix = parsed
    prefix = role_prefix_for(
        std,
        role=role,
        category=category,
        library_id=library_id,
        source_prefix=letters,
    )
    if not prefix:
        return f"{letters}{number}{suffix}"
    return f"{prefix}{number}{suffix}"


def convert_terminal_tag(raw_tag: str, target: str) -> str | None:
    """Convert a terminal designation between IEC ``-X1:3`` / ``X1:3`` and NEMA ``TB1-3``.

    Returns ``None`` when ``raw_tag`` is not a terminal strip:pin tag.
    """

    std = target_standard(target)
    match = _TERMINAL_RE.match(str(raw_tag or "").strip())
    if not match:
        return None
    _dash, prefix, strip_num, _sep, pin = match.groups()
    pfx_upper = prefix.upper()
    iec_aliases = {str(a).upper() for a in TERMINAL_TAG_TABLE["IEC"]["strip_aliases"]}
    nema_aliases = {str(a).upper() for a in TERMINAL_TAG_TABLE["NEMA/ANSI"]["strip_aliases"]}
    if pfx_upper not in (iec_aliases | nema_aliases):
        return None
    rule = TERMINAL_TAG_TABLE[std]
    return f"{rule['strip_prefix']}{ int(strip_num) }{rule['pin_separator']}{pin}"


def convert_terminal_suffix(suffix: str, target: str) -> str:
    """Convert a terminal pin suffix ``:3`` <-> ``-3`` using ``TERMINAL_TAG_TABLE``."""

    text = str(suffix or "")
    if len(text) < 2 or text[0] not in {":", "-"} or not text[1:].isalnum():
        return text
    std = target_standard(target)
    sep = str(TERMINAL_TAG_TABLE[std]["pin_separator"])
    return f"{sep}{text[1:]}"


def format_wire_number(
    raw_number: str,
    target: str,
    *,
    rung: int | None = None,
    branch: int | str | None = None,
    sheet: int = 1,
    col: int = 1,
    seq: int = 1,
    keep: bool = False,
) -> str:
    """Format or convert a wire number using ``WIRE_NUMBER_FORMAT_TABLE``.

    - When ``keep=True``, the existing wire number is kept as-is.
    - Supply/return rail tokens in ``rail_aliases`` map to the target standard's wording.
    - Converting to ``NEMA/ANSI`` produces ladder line-reference numbers (e.g. ``101``, ``101A``)
      from ``rung`` (or from an IEC address wire number ``1.1`` -> rung 101).
    - Converting to ``IEC`` produces potential/address style numbers from ``sheet``/``col``/``seq``
      (or from a NEMA rung wire number ``101`` -> ``11.1``).
    """

    text = str(raw_number or "").strip()
    if keep:
        return text
    std = target_standard(target)
    rule = WIRE_NUMBER_FORMAT_TABLE[std]
    aliases = rule.get("rail_aliases") or {}
    if text in aliases:
        return str(aliases[text])
    upper = text.upper()
    if upper in aliases:
        return str(aliases[upper])

    branch_letter = ""
    if isinstance(branch, int):
        if branch > 0:
            branch_letter = chr(ord("A") + ((branch - 1) % 26))
    elif isinstance(branch, str) and branch.strip():
        branch_letter = branch.strip().upper()

    if std == "NEMA/ANSI":
        rung_num = rung
        inferred_branch = branch_letter
        if rung_num is None and text:
            m_nema = _NEMA_RUNG_WIRE_RE.match(text)
            if m_nema:
                rung_num = int(m_nema.group(1))
                inferred_branch = inferred_branch or m_nema.group(2).upper()
            else:
                m_iec = _IEC_ADDR_WIRE_RE.match(text)
                if m_iec:
                    p_sheet = int(m_iec.group(1) or sheet or 1)
                    p_line = int(m_iec.group(2))
                    p_sub = int(m_iec.group(3) or 0)
                    rung_num = p_sheet * 100 + p_line if p_line < 100 else p_line
                    if not inferred_branch:
                        if m_iec.group(4):
                            inferred_branch = m_iec.group(4).upper()
                        elif p_sub > 1:
                            inferred_branch = chr(ord("A") + ((p_sub - 2) % 26))
        if rung_num is None:
            return text
        if inferred_branch:
            return str(rule["rung_branch_format"]).format(rung=int(rung_num), branch=inferred_branch)
        return str(rule["rung_base_format"]).format(rung=int(rung_num))

    # Target is IEC potential/address style
    if rung is not None:
        s_num = rung // 100 if rung >= 100 else sheet
        c_num = rung % 100 if rung >= 100 else rung
        q_num = (ord(branch_letter[0]) - ord("A") + 2) if branch_letter else seq
        return str(rule["seq_format"]).format(sheet=s_num, col=c_num, seq=q_num)
    if text:
        m_nema = _NEMA_RUNG_WIRE_RE.match(text)
        if m_nema:
            r_val = int(m_nema.group(1))
            b_val = m_nema.group(2).upper()
            s_num = r_val // 100 if r_val >= 100 else sheet
            c_num = r_val % 100 if r_val >= 100 else r_val
            q_num = (ord(b_val[0]) - ord("A") + 2) if b_val else 1
            return str(rule["seq_format"]).format(sheet=s_num, col=c_num, seq=q_num)
    return text
