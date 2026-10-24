"""Look up the symbol, role, terminal, and wire-number tables in ``standard_map``."""

from __future__ import annotations

import re
from typing import Any

from panelcad.convert.standard_map import (
    CONTACT_PIN_MAP_TABLE,
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


_CONTACT_BY_IEC_TERMS: dict[str, dict[str, Any]] = {
    str(row["iec_terminals"]): row for row in CONTACT_PIN_MAP_TABLE
}
_IEC_NO_AUX_PAIRS: set[str] = {
    "13-14", "23-24", "33-34", "43-44", "53-54", "63-64", "73-74", "83-84",
}
_IEC_NC_AUX_PAIRS: set[str] = {
    "11-12", "21-22", "31-32", "41-42", "51-52", "61-62", "71-72", "81-82",
}
_IEC_TIMER_NO_PAIRS: set[str] = {"15-18", "25-28"}
_IEC_TIMER_NC_PAIRS: set[str] = {"15-16", "25-26"}
_IEC_PIN_VALUE_TO_NEMA_NO: dict[str, str] = {
    "13": "3", "14": "4",
    "23": "3", "24": "4",
    "33": "3", "34": "4",
    "43": "3", "44": "4",
    "53": "3", "54": "4",
    "63": "3", "64": "4",
    "73": "3", "74": "4",
    "83": "3", "84": "4",
    "15": "3", "18": "4",
    "25": "3", "28": "4",
}
_IEC_PIN_VALUE_TO_NEMA_NC: dict[str, str] = {
    "11": "1", "12": "2",
    "21": "1", "22": "2",
    "31": "1", "32": "2",
    "41": "1", "42": "2",
    "51": "1", "52": "2",
    "61": "1", "62": "2",
    "71": "1", "72": "2",
    "81": "1", "82": "2",
    "15": "1", "16": "2",
    "25": "1", "26": "2",
}


def contact_pin_map_for(
    terminals: str = "",
    *,
    state: str = "",
    delayed: str = "",
    kind: str = "",
    slot: int = 1,
) -> dict[str, Any] | None:
    """Return the contact pin-map row matching ``terminals`` or ``(kind, state, delayed, slot)``."""
    clean = str(terminals or "").strip().replace("/", "-")
    if clean in _CONTACT_BY_IEC_TERMS:
        return _CONTACT_BY_IEC_TERMS[clean]
    d_up = str(delayed or "").strip().upper()
    s_up = str(state or d_up or "").strip().upper()
    k_lo = str(kind or "").strip().lower()
    if not k_lo and clean.lower() in {"aux_no", "aux_nc", "operator_nc", "timer_nc", "timer_no", "ol_nc", "ol_no"}:
        k_lo = clean.lower()
    if k_lo == "ol_nc":
        return _CONTACT_BY_IEC_TERMS["95-96"]
    if k_lo == "ol_no":
        return _CONTACT_BY_IEC_TERMS["97-98"]
    if k_lo == "aux_nc" and not s_up:
        s_up = "NC"
    if k_lo == "aux_no" and not s_up:
        s_up = "NO"
    slot_num = max(1, int(slot or 1))
    if clean in ("95-96", "97-98"):
        return _CONTACT_BY_IEC_TERMS.get(clean)
    if d_up == "NC" or k_lo == "timer_nc":
        for row in CONTACT_PIN_MAP_TABLE:
            if row.get("kind") == "timer_nc" and int(row.get("slot", 1)) == slot_num:
                return row
        return _CONTACT_BY_IEC_TERMS["15-16"]
    if d_up == "NO" or k_lo == "timer_no":
        for row in CONTACT_PIN_MAP_TABLE:
            if row.get("kind") == "timer_no" and int(row.get("slot", 1)) == slot_num:
                return row
        return _CONTACT_BY_IEC_TERMS["15-18"]
    if k_lo == "operator_nc":
        return _CONTACT_BY_IEC_TERMS["11-12"]
    if s_up == "NC" or clean == "1-2":
        for row in CONTACT_PIN_MAP_TABLE:
            if row.get("kind") == "aux_nc" and int(row.get("slot", 1)) == slot_num:
                return row
        return _CONTACT_BY_IEC_TERMS["21-22"]
    if s_up == "NO" or clean == "3-4":
        for row in CONTACT_PIN_MAP_TABLE:
            if row.get("kind") == "aux_no" and int(row.get("slot", 1)) == slot_num:
                return row
        return _CONTACT_BY_IEC_TERMS["13-14"]
    return None


def convert_contact_terminals(
    terminals: str,
    target: str,
    *,
    state: str = "",
    delayed: str = "",
    kind: str = "",
    slot: int = 1,
) -> str:
    """Convert a contact terminal pair (e.g. ``23-24`` or ``15-16``) to ``target`` standard."""
    norm = normalize_standard(target)
    clean = str(terminals or "").strip().replace("/", "-")
    if clean in ("95-96", "97-98"):
        return clean
    row = contact_pin_map_for(clean, state=state, delayed=delayed, kind=kind, slot=slot)
    if norm == "NEMA/ANSI":
        if row is not None:
            return str(row["nema_terminals"])
        if clean in _IEC_NO_AUX_PAIRS or clean in _IEC_TIMER_NO_PAIRS:
            return "3-4"
        if clean in _IEC_NC_AUX_PAIRS or clean in _IEC_TIMER_NC_PAIRS:
            return "1-2"
        return clean
    if row is not None:
        return str(row["iec_terminals"])
    return clean


def convert_contact_pin_labels(
    pin_labels: dict[str, Any],
    pin_map: dict[str, str],
    target: str,
    *,
    state: str = "",
    delayed: str = "",
    slot: int = 1,
) -> dict[str, str]:
    """Remap ``pin_labels`` keys and contact-number values across standards."""
    norm = normalize_standard(target)
    d_up = str(delayed or "").strip().upper()
    s_up = str(state or d_up or "").strip().upper()
    is_nc = s_up == "NC" or "11" in pin_map or "21" in pin_map or "1" in pin_map
    out: dict[str, str] = {}
    default_to_nema = {"11": "1", "12": "2", "21": "1", "22": "2", "13": "3", "14": "4"}
    default_to_iec = {"1": "21", "2": "22", "3": "13", "4": "14"}
    for k, v in (pin_labels or {}).items():
        ks = str(k)
        if ks in pin_map:
            new_k = pin_map[ks]
        elif not pin_map and norm == "NEMA/ANSI" and ks in default_to_nema:
            new_k = default_to_nema[ks]
        elif not pin_map and norm == "IEC" and ks in default_to_iec:
            new_k = default_to_iec[ks]
        else:
            new_k = ks
        val = str(v)
        if norm == "NEMA/ANSI":
            table = _IEC_PIN_VALUE_TO_NEMA_NC if is_nc else _IEC_PIN_VALUE_TO_NEMA_NO
            new_v = table.get(val, val)
        else:
            row = contact_pin_map_for(state="NC" if is_nc else "NO", delayed=d_up, slot=slot)
            if row is not None and val in row["pins_nema_to_iec"]:
                new_v = str(row["pins_nema_to_iec"][val])
            else:
                new_v = val
        out[new_k] = new_v
    return out


def convert_contact_number(
    contact_no: str,
    target: str,
    *,
    state: str = "",
    delayed: str = "",
    slot: int = 1,
) -> str:
    """Convert a single contact number (``13``, ``23``, ``11``, ``21``, ``15``, ``18``, ``95``, ``97``) to ``target``."""
    norm = normalize_standard(target)
    clean = str(contact_no or "").strip()
    if not clean:
        return clean
    if clean in ("95", "96", "97", "98"):
        return clean
    d_up = str(delayed or "").strip().upper()
    s_up = str(state or d_up or "").strip().upper()
    if norm == "NEMA/ANSI":
        if clean in _IEC_PIN_VALUE_TO_NEMA_NC and (s_up == "NC" or clean in {"11", "12", "21", "22", "31", "32", "41", "42", "16", "26"}):
            return _IEC_PIN_VALUE_TO_NEMA_NC[clean]
        if clean in _IEC_PIN_VALUE_TO_NEMA_NO:
            return _IEC_PIN_VALUE_TO_NEMA_NO[clean]
        return clean
    row = contact_pin_map_for(state=s_up or ("NC" if clean in {"1", "2"} else "NO"), delayed=d_up, slot=slot)
    if row is not None:
        if clean in row["pins_nema_to_iec"]:
            return str(row["pins_nema_to_iec"][clean])
        return str(row["iec_contact_no"])
    return clean
