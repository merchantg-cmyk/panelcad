"""Automatic wire numbers and from/to wire-list rows.

NEMA shops get sheet/line numbers (101, 102, …) by default. IEC jobs can
switch to ``1-1`` style. Electrically connected segments share one number
through junctions and off-page arrows; a number stops at a device pin.

Opening a project never assigns numbers. ``assign_wire_numbers`` runs when
wires are drawn, connected, or deleted (incremental), or when the operator
asks for a full ``number_wires`` pass.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Literal

from panelcad.core.models import Component, Project, Sheet, Wire, WireNumbering
from panelcad.library.catalog import Catalog

SCHEMES = ("sheet_line", "sequential", "iec")
WireScheme = Literal["sheet_line", "sequential", "iec"]

# Pass-through: the same wire network continues through these symbols.
_PASSTHROUGH_CATS = frozenset({"connection.node", "connection.offpage"})
_PASSTHROUGH_HINTS = ("connection.node", "offpage", "connection.dot", "connection.junction", "waypoint")

# Conventional / power / common names a shop never wants overwritten.
_CONVENTIONAL = frozenset(
    {
        "L1",
        "L2",
        "L3",
        "L",
        "N",
        "PE",
        "GND",
        "GROUND",
        "EARTH",
        "24V",
        "24VDC",
        "+24V",
        "+24VDC",
        "0V",
        "0VDC",
        "L+",
        "M",
        "COM",
        "COMMON",
        "DCM",
        "120",
        "120V",
        "120VAC",
        "240V",
        "240VAC",
        "480V",
        "480VAC",
        "H",
        "HOT",
        "NEUTRAL",
    }
)
_CONVENTIONAL_RE = re.compile(
    r"^(?:L[1-3]|24\s*V(?:DC)?|\+?24\s*V(?:DC)?|0\s*V(?:DC)?)$",
    re.IGNORECASE,
)

# Comms hops are cables, not a conductor tee. A shared ETH/COM port must not
# merge two links onto one wire number (A2→switch and A1→switch are not one net).
_COMMS_PINS = frozenset(
    {
        "ETH",
        "LAN",
        "WAN",
        "RJ45",
        "USB",
        "TX",
        "RX",
        "TXD",
        "RXD",
        "PROFINET",
        "PN",
    }
)
_COMMS_PIN_RE = re.compile(
    r"^(?:ETH(?:ERNET)?\d*|LAN\d*|WAN\d*|COM\d+|RJ45\d*|TXD?|RXD?|USB\d*|PN\d*|PROFINET)$",
    re.IGNORECASE,
)
_COMMS_NET_RE = re.compile(
    r"^(?:LAN|ETH|ETHERNET|PROFINET|MODBUS(?:[-_ ]?TCP|[-_ ]?RTU)?|MB[-_]?\d*|HMI|SERIAL|RS[-_]?485|RS[-_]?232)$",
    re.IGNORECASE,
)


def normalize_scheme(value: str | None, *, default: str = "sheet_line") -> str:
    token = "".join(ch.lower() for ch in str(value or "") if ch.isalnum())
    if token in {"sheetline", "nema", "ansi", "jic", "nfpa", "lineref", "reference"}:
        return "sheet_line"
    if token in {"sequential", "seq", "number", "numeric"}:
        return "sequential"
    if token in {"iec", "iecstyle", "sheetseq"}:
        return "iec"
    return default if default in SCHEMES else "sheet_line"


def resolve_settings(project: Project) -> WireNumbering:
    raw = project.wire_numbering
    if raw is None:
        return WireNumbering()
    return WireNumbering(
        scheme=normalize_scheme(raw.scheme, default="sheet_line"),
        enabled=bool(raw.enabled),
        start=max(1, int(raw.start or 1)),
        split_feedthrough=bool(getattr(raw, "split_feedthrough", False)),
    )


def set_settings(
    project: Project,
    *,
    scheme: str | None = None,
    enabled: bool | None = None,
    start: int | None = None,
    split_feedthrough: bool | None = None,
) -> WireNumbering:
    current = resolve_settings(project)
    if scheme is not None:
        current.scheme = normalize_scheme(scheme)  # type: ignore[assignment]
    if enabled is not None:
        current.enabled = bool(enabled)
    if start is not None:
        current.start = max(1, int(start))
    if split_feedthrough is not None:
        current.split_feedthrough = bool(split_feedthrough)
    project.wire_numbering = current
    return current


def settings_payload(project: Project) -> dict[str, Any]:
    settings = resolve_settings(project)
    return {
        "scheme": settings.scheme,
        "enabled": settings.enabled,
        "start": settings.start,
        "split_feedthrough": settings.split_feedthrough,
        "schemes": list(SCHEMES),
        "default_scheme": "sheet_line",
    }


def _norm(text: str | None) -> str:
    return str(text or "").strip()


def conventional_name(text: str | None, extras: Iterable[str] = ()) -> str:
    """Return the conventional spelling if *text* is a reserved power/common name."""

    raw = _norm(text)
    if not raw:
        return ""
    compact = re.sub(r"\s+", "", raw)
    upper = compact.upper()
    extra = {re.sub(r"\s+", "", str(item)).upper() for item in extras if str(item).strip()}
    if upper in _CONVENTIONAL or upper in extra or _CONVENTIONAL_RE.match(compact):
        return raw
    return ""


def is_conventional(text: str | None, extras: Iterable[str] = ()) -> bool:
    return bool(conventional_name(text, extras))


def is_locked(wire: Wire) -> bool:
    return bool(getattr(wire, "number_locked", False))


def is_protected(wire: Wire, extras: Iterable[str] = ()) -> bool:
    if is_locked(wire):
        return True
    if conventional_name(wire.number, extras):
        return True
    if conventional_name(wire.net, extras) and not _norm(wire.number):
        return True
    return False


def _sheet_no(project: Project, sheet_id: str) -> int:
    for index, sheet in enumerate(project.sheets, start=1):
        if sheet.id == sheet_id:
            return index
    return 1


def _is_rail(sheet: Sheet, project: Project, end: str) -> bool:
    from panelcad.core.ladder import endpoint_is_rail

    return endpoint_is_rail(sheet, end, project)


def split_end(end: str) -> tuple[str, str]:
    """Device id and terminal id. A rail name has an empty terminal."""

    raw = _norm(end)
    if "." not in raw:
        return raw, ""
    ref, pin = raw.split(".", 1)
    return ref, pin


def _device_of(catalog: Catalog | None, comp: Component):
    if catalog is None:
        return None
    try:
        return catalog.get(comp.library_id)
    except KeyError:
        return None


_SPLIT_TERMINAL = ("pe", "ground", "earth", "disconnect", "fuse", "multi-level", "multilevel", "multi level")


def is_feedthrough_terminal(comp: Component, catalog: Catalog | None = None) -> bool:
    """Ordinary two-pin terminal: one conductor through both sides.

    PE, ground, fuse, disconnect, multi-level, and a terminal marked split
    keep a number on each side.
    """

    device = _device_of(catalog, comp)
    if device is None:
        return False
    if len(device.pins) != 2:
        return False
    lib = str(device.library_id or "").lower()
    cat = str(device.category or "").lower()
    props = {**(device.properties or {}), **(comp.properties or {})}
    kind = str(props.get("type") or "").lower().replace("_", " ").replace("-", " ")
    blob = f"{lib} {cat} {kind}"
    if any(hint in blob for hint in _SPLIT_TERMINAL):
        return False
    if str(props.get("split") or "").lower() in {"1", "true", "yes"}:
        return False
    passthrough = str(props.get("passthrough") or "").lower()
    if passthrough in {"0", "false", "no"}:
        return False
    if cat == "connection.terminal" or ".terminals." in lib or passthrough in {"1", "true", "yes"} or "feed" in blob:
        return True
    return False


def release_feedthrough_numbers(project: Project, catalog: Catalog | None = None) -> int:
    """Drop unlocked numbers on wires that land on a feed-through terminal.

    The next numbering pass assigns them again under the current split setting.
    Locked numbers stay.
    """

    cleared = 0
    for sheet in project.sheets:
        if sheet.kind in {"panel_layout", "door_layout"}:
            continue
        for wire in sheet.wires:
            if is_locked(wire):
                continue
            if not any(_end_is_feedthrough(sheet, end, catalog) for end in (wire.frm, wire.to)):
                continue
            if not _norm(wire.number):
                continue
            wire.number = ""
            cleared += 1
    return cleared


def _end_is_feedthrough(sheet: Sheet, end: str, catalog: Catalog | None) -> bool:
    ref, pin = split_end(end)
    if not ref or not pin:
        return False
    try:
        comp = sheet.component(ref)
    except KeyError:
        return False
    return is_feedthrough_terminal(comp, catalog)


def feedthrough_joins(comp: Component, catalog: Catalog | None = None, project: Project | None = None) -> bool:
    """One net through a feed-through terminal, unless the shop asked for split numbers."""

    if project is not None and resolve_settings(project).split_feedthrough:
        return False
    return is_feedthrough_terminal(comp, catalog)


def is_passthrough(comp: Component, catalog: Catalog | None = None, project: Project | None = None) -> bool:
    device = _device_of(catalog, comp)
    if device is not None:
        cat = device.category or ""
        lid = device.library_id.lower()
        if cat in _PASSTHROUGH_CATS or any(hint in lid for hint in _PASSTHROUGH_HINTS):
            return True
    lid = (comp.library_id or "").lower()
    return any(hint in lid for hint in _PASSTHROUGH_HINTS)


def is_comms_pin(pin_id: str) -> bool:
    token = re.sub(r"[\s_\-./]", "", str(pin_id or "")).upper()
    if token in _COMMS_PINS:
        return True
    return bool(_COMMS_PIN_RE.match(str(pin_id or "").strip()))


def end_is_comms(sheet: Sheet, end: str, catalog: Catalog | None = None) -> bool:
    """True for a network-page end or a catalog comms port (ETH, COM1, …)."""

    if sheet.kind == "network":
        return True
    ref, pin = split_end(end)
    if not pin:
        return False
    if is_comms_pin(pin):
        return True
    if catalog is None:
        return False
    try:
        comp = sheet.component(ref)
    except KeyError:
        return False
    device = _device_of(catalog, comp)
    if device is None:
        return False
    from panelcad.library.ports import effective_ports

    return any(p.id.upper() == pin.upper() for p in effective_ports(device, comp))


def is_comms_wire(sheet: Sheet, wire: Wire, catalog: Catalog | None = None) -> bool:
    if sheet.kind == "network":
        return True
    if _COMMS_NET_RE.match(_norm(wire.net)):
        return True
    return end_is_comms(sheet, wire.frm, catalog) or end_is_comms(sheet, wire.to, catalog)


def _end_key(sheet: Sheet, project: Project, end: str) -> tuple:
    raw = _norm(end)
    if _is_rail(sheet, project, raw):
        # Same named rail is one conductor on every sheet (L2 is L2).
        return ("rail", split_end(raw)[0].upper())
    ref, pin = split_end(raw)
    if not pin:
        return ("raw", sheet.id, raw.upper())
    return ("pin", sheet.id, ref.upper(), pin.upper())


class _UF:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def add(self, item: int) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: int) -> int:
        parent = self.parent.setdefault(item, item)
        if parent != item:
            self.parent[item] = self.find(parent)
        return self.parent[item]

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _wire_pin_x(sheet: Sheet, catalog: Catalog | None, wire: Wire) -> float:
    """World X of the first landed pin. Phase conductors then sort left to right."""

    for end in (wire.frm, wire.to):
        ref, pin = split_end(end)
        if not ref:
            continue
        try:
            comp = sheet.component(ref)
        except KeyError:
            continue
        if catalog is None or not pin:
            return float(comp.x)
        try:
            device = catalog.get(comp.library_id)
            pdef = device.pin(pin)
        except KeyError:
            return float(comp.x)
        from panelcad.render.sheet import pin_world

        return float(pin_world(comp, pdef)[0])
    return 0.0


def _wire_anchor(sheet: Sheet, project: Project, wire: Wire) -> tuple[float, float]:
    for end in (wire.frm, wire.to):
        if _is_rail(sheet, project, end):
            continue
        ref, _pin = split_end(end)
        try:
            comp = sheet.component(ref)
            return (float(comp.y), float(comp.x))
        except KeyError:
            continue
    return (0.0, 0.0)


def wire_networks(
    project: Project,
    catalog: Catalog | None = None,
) -> list[list[tuple[Sheet, Wire]]]:
    """Group wires that share a pin, a junction, an off-page link, or a net name.

    Comms ports (ETH, COM1, network sheets) are not junctions. Each Ethernet or
    serial hop keeps its own number even when two cables land on the same switch
    port. Same-net power conductors still share a number.
    """

    indexed: list[tuple[int, Sheet, Wire]] = []
    for sheet in project.sheets:
        if sheet.kind in {"panel_layout", "door_layout"}:
            continue
        for wire in sheet.wires:
            indexed.append((len(indexed), sheet, wire))
    if not indexed:
        return []

    uf = _UF()
    pins: dict[tuple, list[int]] = {}
    nets: dict[str, list[int]] = {}
    for idx, sheet, wire in indexed:
        uf.add(idx)
        comms = is_comms_wire(sheet, wire, catalog)
        for end in (wire.frm, wire.to):
            if comms or end_is_comms(sheet, end, catalog):
                continue
            pins.setdefault(_end_key(sheet, project, end), []).append(idx)
        net = _norm(wire.net)
        if net and not comms:
            nets.setdefault(net.upper(), []).append(idx)

    for members in pins.values():
        head = members[0]
        for other in members[1:]:
            uf.union(head, other)
    for members in nets.values():
        head = members[0]
        for other in members[1:]:
            uf.union(head, other)

    # Junction / off-page symbols: every pin on the symbol is the same network.
    links: dict[str, list[tuple]] = {}
    for sheet in project.sheets:
        if sheet.kind in {"panel_layout", "door_layout"}:
            continue
        for comp in sheet.components:
            if not (is_passthrough(comp, catalog, project) or feedthrough_joins(comp, catalog, project)):
                continue
            keys = [key for key in pins if key[0] == "pin" and key[1] == sheet.id and key[2] == comp.id.upper()]
            if keys:
                head = keys[0]
                for other in keys[1:]:
                    a = pins[head][0]
                    b = pins[other][0]
                    uf.union(a, b)
            link = _norm(comp.properties.get("link"))
            if link:
                for key in keys:
                    links.setdefault(link, []).append(key)

    for keys in links.values():
        if len(keys) < 2:
            continue
        head = pins[keys[0]][0]
        for key in keys[1:]:
            uf.union(head, pins[key][0])

    groups: dict[int, list[tuple[Sheet, Wire]]] = {}
    for idx, sheet, wire in indexed:
        groups.setdefault(uf.find(idx), []).append((sheet, wire))

    def _item_key(row: tuple[Sheet, Wire]) -> tuple:
        sheet, wire = row
        y, x = _wire_anchor(sheet, project, wire)
        return (_sheet_no(project, sheet.id), y, x, _wire_pin_x(sheet, catalog, wire), wire.id)

    def sort_key(items: list[tuple[Sheet, Wire]]) -> tuple:
        sheet, wire = min(items, key=_item_key)
        y, x = _wire_anchor(sheet, project, wire)
        # Same device, top to bottom: the left-hand conductor takes the lower number.
        return (_sheet_no(project, sheet.id), y, x, _wire_pin_x(sheet, catalog, wire), wire.id)

    return [groups[key] for key in sorted(groups, key=lambda k: sort_key(groups[k]))]


def _network_extras(project: Project) -> tuple[str, ...]:
    """Rail names stay as the wire number, the way L1, N, L+, and 0V do.

    Sheet rails are included so an IEC AC control pair named X1/X2 is numbered
    X1/X2. Those names are not supply phases, and they are not added to the
    global conventional set: a pilot pin called X1 keeps a sheet/line number
    unless its net is the rail.
    """

    tb = project.title_block
    names = [getattr(tb, "rail_hot", ""), getattr(tb, "rail_return", "")]
    for sheet in project.sheets:
        names.append(getattr(sheet, "rail_hot", ""))
        names.append(getattr(sheet, "rail_return", ""))
    seen: list[str] = []
    folded: set[str] = set()
    for part in names:
        token = _norm(part)
        if not token or token.upper() in folded:
            continue
        folded.add(token.upper())
        seen.append(token)
    return tuple(seen)


def _pick_existing(wires: list[Wire], extras: Iterable[str], *, overwrite: bool) -> str:
    locked = [_norm(w.number) for w in wires if is_locked(w) and _norm(w.number)]
    if locked:
        return locked[0]
    conv_numbers = [conventional_name(w.number, extras) for w in wires]
    conv_numbers = [name for name in conv_numbers if name]
    if conv_numbers:
        return conv_numbers[0]
    conv_nets = [conventional_name(w.net, extras) for w in wires]
    conv_nets = [name for name in conv_nets if name]
    if conv_nets:
        return conv_nets[0]
    if overwrite:
        return ""
    existing = [_norm(w.number) for w in wires if _norm(w.number)]
    return existing[0] if existing else ""


def _next_number(scheme: str, sheet_no: int, start: int, used: set[str]) -> str:
    if scheme == "iec":
        n = start
        while f"{sheet_no}-{n}" in used:
            n += 1
        value = f"{sheet_no}-{n}"
        used.add(value)
        return value
    if scheme == "sequential":
        n = max(1, start)
        while str(n) in used:
            n += 1
        value = str(n)
        used.add(value)
        return value
    n = sheet_no * 100 + max(1, start)
    while str(n) in used:
        n += 1
    value = str(n)
    used.add(value)
    return value


def assign_wire_numbers(
    project: Project,
    catalog: Catalog | None = None,
    *,
    mode: Literal["incremental", "full"] = "incremental",
    start: int | None = None,
    overwrite: bool = False,
    scheme: str | None = None,
) -> dict[str, Any]:
    """Assign numbers to wire networks.

    ``incremental`` fills blanks and unifies unlocked members of a network.
    It is a no-op when auto-numbering is disabled. ``full`` is ``number_wires``:
    same rules, plus ``overwrite`` reassigns unprotected numbers.
    """

    settings = resolve_settings(project)
    if mode == "incremental" and not settings.enabled:
        networks = wire_networks(project, catalog)
        return {
            "assigned": 0,
            "nets": len(networks),
            "scheme": settings.scheme,
            "start": settings.start,
            "mode": mode,
        }

    use_scheme = normalize_scheme(scheme, default=settings.scheme)
    use_start = max(1, int(start if start is not None else settings.start))
    extras = _network_extras(project)
    networks = wire_networks(project, catalog)
    used: set[str] = set()
    for items in networks:
        for _sheet, wire in items:
            number = _norm(wire.number)
            if not number:
                continue
            if mode == "full" and overwrite and not is_protected(wire, extras):
                continue
            used.add(number)

    assigned = 0
    for items in networks:
        wires = [wire for _sheet, wire in items]
        # A locked blank stays blank. The reference arrows on a PSU primary
        # clear their number on purpose; the next incremental pass must not
        # put 210 back on that 2 mm stub.
        if all(is_locked(wire) for wire in wires):
            continue
        sheet = min(items, key=lambda row: (_sheet_no(project, row[0].id), row[1].id))[0]
        chosen = _pick_existing(wires, extras, overwrite=(mode == "full" and overwrite))
        if not chosen:
            for wire in wires:
                conv = conventional_name(wire.net, extras)
                if conv:
                    chosen = conv
                    break
        if not chosen:
            chosen = _next_number(use_scheme, _sheet_no(project, sheet.id), use_start, used)
        elif chosen not in used:
            used.add(chosen)
        for wire in wires:
            if is_locked(wire):
                continue
            current = _norm(wire.number)
            if current == chosen:
                continue
            if current and is_protected(wire, extras):
                continue
            if mode == "full" and overwrite and not is_protected(wire, extras):
                wire.number = chosen
                assigned += 1
                continue
            if not current:
                wire.number = chosen
                assigned += 1
                continue
            if mode == "incremental" and not is_locked(wire) and not conventional_name(current, extras):
                wire.number = chosen
                assigned += 1
    return {
        "assigned": assigned,
        "nets": len(networks),
        "scheme": use_scheme,
        "start": use_start,
        "mode": mode,
    }


def number_wires(
    project: Project,
    catalog: Catalog | None = None,
    *,
    start: int = 1,
    overwrite: bool = False,
    scheme: str | None = None,
) -> dict[str, Any]:
    """Full renumber. Locked and conventional numbers are never overwritten."""

    return assign_wire_numbers(
        project,
        catalog,
        mode="full",
        start=start,
        overwrite=overwrite,
        scheme=scheme,
    )


def wire_list_rows(project: Project) -> list[dict[str, str]]:
    """From/to report: device and terminal at each end, number, color, gauge."""

    rows: list[dict[str, str]] = []
    for sheet in project.sheets:
        if sheet.kind in {"panel_layout", "door_layout"}:
            continue
        for wire in sheet.wires:
            frm_dev, frm_pin = split_end(wire.frm)
            to_dev, to_pin = split_end(wire.to)
            rows.append(
                {
                    "sheet": sheet.id,
                    "wire": wire.id,
                    "number": wire.number,
                    "from": wire.frm,
                    "to": wire.to,
                    "from_device": frm_dev,
                    "from_terminal": frm_pin,
                    "to_device": to_dev,
                    "to_terminal": to_pin,
                    "net": wire.net or "",
                    "label": wire.label,
                    "color": wire.color,
                    "gauge": wire.gauge,
                    "cable": wire.cable,
                }
            )
    return rows
