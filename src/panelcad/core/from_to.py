"""From-to wire list. One row per conductor, taken from the netlist.

Wire numbers come from the wires already on the project. This module does not
assign or change them. A contact is not a junction, so the conductor on each
side of a contact is its own row and its own number.

``from`` / ``to`` are ``tag:pin`` (``M1:A1``) or ``strip:terminal`` (``X1:4``).
A rail is not an end: the devices that land on it are daisy-chained
(``PS1:minus`` → ``M1:A2`` → ``M2:A2``), through a strip terminal when one is
on that net. A drawing junction or off-page arrow is not an end either; the
row connects the device or terminal pins on either side.

``crossing`` is the ``strip:terminal`` a conductor lands on. A row with no
strip terminal leaves it blank. The cell is filled whether or not the wire
number changes at the block.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from panelcad.core.electrical import symbol_role, terminal_identity
from panelcad.core.ladder import endpoint_is_rail, rail_names, rail_side
from panelcad.core.models import Component, Device, Project, Sheet, Wire
from panelcad.core.wire_numbering import is_passthrough, split_end, wire_networks
from panelcad.core.xref import location_of, xref_style
from panelcad.library.catalog import Catalog
from panelcad.render.titleblock import working_rect

FROM_TO_COLUMNS: tuple[str, ...] = (
    "wire_number",
    "from",
    "to",
    "from_sheet",
    "from_line",
    "from_column",
    "to_sheet",
    "to_line",
    "to_column",
    "size",
    "color",
    "type",
    "crossing",
)

WIRE_LIST_SCHEMA: dict[str, object] = {
    "op": "wire_list",
    "description": (
        "From-to report. One row per conductor from the netlist (wire_networks). "
        "A contact's two sides are separate rows with separate wire numbers. "
        "Uses the numbers already on the wires; it does not renumber. "
        "from/to are tag:pin (M1:A1) or strip:terminal (X1:4). "
        "A rail is daisy-chained between the device and terminal pins that land on it. "
        "A junction dot is not an end. "
        "Each end has sheet, NEMA line, and IEC column. size/color/type come from "
        "the wire, else that net, else the control or power entry, else *. "
        "An unknown gauge is TBD. The report does not substitute a default size. "
        "Color and type still fall back (control BU MTW, power BK MTW). "
        "crossing is the strip terminal a conductor lands on, or blank when neither end is a terminal. "
        "Pass from_to=true (the drawing-agent tool always does) so result.rows is this report. "
        "Without from_to, result.rows stays the legacy device/terminal list and result.from_to is this report."
    ),
    "columns": list(FROM_TO_COLUMNS),
    "sort": ["number", "device"],
    "args": {
        "sort": "number (default) or device (from-tag, then pin, then wire number).",
        "path": "Optional CSV path.",
        "from_to": "true writes and returns this report. Omit to keep the legacy rows.",
    },
}


@dataclass
class _End:
    display: str
    device: str
    pin: str
    sheet_id: str
    line: str
    column: str
    loc: str
    kind: str
    passthrough: bool
    identity: str
    x: float | None
    y: float | None
    raw: str
    link: str


@dataclass
class _Conductor:
    wire_id: str
    number: str
    frm: _End
    to: _End
    size: str
    color: str
    wire_type: str
    crossing: str

    def public(self) -> dict[str, str]:
        row = {
            "wire_number": self.number,
            "from": self.frm.display,
            "to": self.to.display,
            "from_sheet": self.frm.sheet_id,
            "from_line": self.frm.line,
            "from_column": self.frm.column,
            "to_sheet": self.to.sheet_id,
            "to_line": self.to.line,
            "to_column": self.to.column,
            "size": self.size,
            "color": self.color,
            "type": self.wire_type,
            "crossing": self.crossing,
            "from_loc": self.frm.loc,
            "to_loc": self.to.loc,
        }
        return row


def normalize_sort(value: str | None) -> str:
    token = "".join(ch.lower() for ch in str(value or "number") if ch.isalnum())
    if token in {"number", "wirenumber", "wire"}:
        return "number"
    if token in {"device", "tag", "from", "component"}:
        return "device"
    raise ValueError(f"unknown from-to sort '{value}' (number or device)")


def _number_key(text: str) -> tuple:
    raw = str(text or "").strip()
    if not raw:
        return (3, 0, 0, "")
    if raw.isdigit():
        return (0, int(raw), 0, "")
    match = re.fullmatch(r"(\d+)-(\d+)", raw)
    if match:
        return (1, int(match.group(1)), int(match.group(2)), "")
    return (2, 0, 0, raw.upper())


def _tag_pin(display: str) -> tuple[str, tuple]:
    if ":" in display:
        tag, pin = display.split(":", 1)
    else:
        tag, pin = display, ""
    if pin.isdigit():
        pin_key: tuple = (0, int(pin), "")
    else:
        pin_key = (1, 0, pin.upper())
    return (tag.upper(), pin_key)


def _device(catalog: Catalog | None, comp: Component) -> Device | None:
    if catalog is None:
        return None
    try:
        return catalog.get(comp.library_id)
    except KeyError:
        return None


def _pin_def(device: Device | None, pin_id: str):
    if device is None or not pin_id:
        return None
    want = pin_id.upper()
    for pin in device.pins:
        if pin.id.upper() == want:
            return pin
    return None


# Color and type used only when the wire, the net, a control/power entry, and
# ``*`` all leave that field blank. Control is the shop 24 V circuit; power is a
# line or motor lead (L1/T1/U1 and the same net names). Size is not in this
# table: an unknown gauge is ``TBD`` on every project.
_UNKNOWN_GAUGE = "TBD"
_BUILTIN_COLOR_TYPE = {
    "control": ("BU", "MTW"),
    "power": ("BK", "MTW"),
}
_POWER_NETS = frozenset({"L1", "L2", "L3", "T1", "T2", "T3", "U1", "V1", "W1"})
_POWER_PINS = frozenset(
    {
        "L1",
        "L2",
        "L3",
        "T1",
        "T2",
        "T3",
        "T4",
        "T5",
        "T6",
        "U",
        "V",
        "W",
        "U1",
        "U2",
        "V1",
        "V2",
        "W1",
        "W2",
    }
)


def _named_spec(project: Project, name: str):
    specs = getattr(project, "net_wire_specs", None) or {}
    if name in specs:
        return specs[name]
    upper = name.upper()
    for key, spec in specs.items():
        if str(key).upper() == upper:
            return spec
    return None


def _conductor_class(number: str, net: str, ends: list[_End]) -> str:
    token = str(number or net or "").strip().upper()
    if token in _POWER_NETS:
        return "power"
    for end in ends:
        if end.kind == "terminal":
            continue
        if str(end.pin or "").upper() in _POWER_PINS:
            return "power"
    return "control"


def _fill_spec(
    project: Project,
    size: str,
    color: str,
    wire_type: str,
    *,
    net: str,
    number: str,
    ends: list[_End],
) -> tuple[str, str, str]:
    """Wire field, then that net, then control/power, then *.

    Size stops there. A blank gauge is ``TBD``. Color and type still use the
    report color and type when every spec leaves them blank.
    """

    kind = _conductor_class(number, net, ends)
    exact = _named_spec(project, net) if str(net or "").strip() else None
    if exact is None and str(number or "").strip() and str(number).strip().upper() != str(net or "").strip().upper():
        exact = _named_spec(project, number)
    starred = _named_spec(project, "*")
    class_spec = _named_spec(project, kind)
    color_fallback = _builtin_color(project, kind, number=number, net=net, ends=ends)
    type_fallback = _BUILTIN_COLOR_TYPE[kind][1]

    def pick(current: str, attr: str, fallback: str) -> str:
        if str(current or "").strip():
            return str(current).strip()
        for spec in (exact, class_spec, starred):
            if spec is None:
                continue
            found = str(getattr(spec, attr) or "").strip()
            if found:
                return found
        return fallback

    return (
        pick(size, "size", _UNKNOWN_GAUGE),
        pick(color, "color", color_fallback),
        pick(wire_type, "wire_type", type_fallback),
    )


_AWG_SUFFIX = re.compile(r"\s+AWG$", re.I)


def _report_size(size: str) -> str:
    """Wire-list size is the bare gauge. A schematic ``14 AWG`` is ``14`` here."""

    return _AWG_SUFFIX.sub("", str(size or "").strip()).strip()


def _merged_spec(project: Project, wires: list[Wire], number: str, ends: list[_End]) -> tuple[str, str, str]:
    size = color = wire_type = net = ""
    for wire in wires:
        size = size or _report_size(str(wire.gauge or ""))
        color = color or str(wire.color or "").strip()
        wire_type = wire_type or str(getattr(wire, "wire_type", "") or "").strip()
        net = net or str(wire.net or "").strip()
    return _fill_spec(project, size, color, wire_type, net=net, number=number, ends=ends)


def _locate(project: Project, sheet: Sheet, x: float, y: float) -> tuple[str, str, str]:
    loc = location_of(project, sheet, x, y)
    line = str(loc.line)
    column = str(loc.col)
    if xref_style(project, sheet) == "iec":
        shown = f"{sheet.id} {loc.iec()}"
    else:
        shown = f"{sheet.id} {line}"
    return line, column, shown


def _describe(
    project: Project,
    sheet: Sheet,
    catalog: Catalog | None,
    raw: str,
    *,
    sibling_y: float | None,
) -> _End:
    hot, ret = rail_names(sheet, project)
    text = str(raw or "").strip()
    if endpoint_is_rail(sheet, text, project):
        wx, wy, ww, _wh = working_rect(sheet)
        x = wx + 6.0 if rail_side(text, hot, ret) == "left" else wx + ww - 6.0
        y = float(sibling_y) if sibling_y is not None else wy + 30.0
        line, column, shown = _locate(project, sheet, x, y)
        name = split_end(text)[0] or text
        return _End(
            display=name,
            device=name,
            pin="",
            sheet_id=sheet.id,
            line=line,
            column=column,
            loc=shown,
            kind="rail",
            passthrough=False,
            identity=name.upper(),
            x=x,
            y=y,
            raw=text,
            link="",
        )

    ref, pin = split_end(text)
    comp: Component | None = None
    if ref:
        try:
            comp = sheet.component(ref)
        except KeyError:
            comp = None
    device = _device(catalog, comp) if comp is not None else None
    kind = "device"
    passthrough = False
    display = f"{ref}:{pin}" if pin else ref
    device_name = ref
    pin_name = pin
    identity = f"{ref.upper()}.{pin.upper()}" if pin else ref.upper()
    if comp is not None and is_passthrough(comp, catalog, project):
        kind = "passthrough"
        passthrough = True
    elif comp is not None and device is not None and symbol_role(device) == "terminal":
        ident = terminal_identity(comp, device)
        if ident is not None:
            strip, terminal = ident
            display = f"{strip}:{terminal}"
            device_name = strip
            pin_name = terminal
            kind = "terminal"

    x, y = (0.0, 0.0)
    if comp is not None:
        pin_def = _pin_def(device, pin)
        if pin_def is not None:
            from panelcad.render.sheet import pin_world

            x, y = pin_world(comp, pin_def)
        else:
            x, y = float(comp.x), float(comp.y)
    line, column, shown = _locate(project, sheet, x, y)
    link = ""
    if comp is not None and passthrough:
        link = str(comp.properties.get("link") or "").strip()
    return _End(
        display=display or text,
        device=device_name,
        pin=pin_name,
        sheet_id=sheet.id,
        line=line,
        column=column,
        loc=shown,
        kind=kind,
        passthrough=passthrough,
        identity=identity or text.upper(),
        x=x,
        y=y,
        raw=text,
        link=link,
    )


def _sheet_index(project: Project, sheet_id: str) -> int:
    for index, sheet in enumerate(project.sheets):
        if sheet.id == sheet_id:
            return index
    return 0


def _pos_key(project: Project, end: _End) -> tuple:
    return (
        _sheet_index(project, end.sheet_id),
        end.y if end.y is not None else 0.0,
        end.x if end.x is not None else 0.0,
        end.display.upper(),
    )


def _is_real(end: _End) -> bool:
    return end.kind in {"device", "terminal"} and bool(end.device) and bool(end.pin)


@dataclass
class _Seg:
    sheet: Sheet
    wire: Wire
    left: _End
    right: _End
    number: str


def _node_key(end: _End) -> tuple[str, str]:
    return (end.sheet_id, end.identity)


def _pair(project: Project, ends: list[_End]) -> list[tuple[_End, _End]]:
    ordered = sorted(ends, key=lambda end: _pos_key(project, end))
    return list(zip(ordered, ordered[1:]))


def default_power_color(
    project: Project,
    *,
    number: str = "",
    net: str = "",
    pins: tuple[str, ...] = (),
) -> str:
    """Color used when a power conductor has no color of its own.

    Today every power conductor falls back to black. Phase colors belong in a
    later change: NEMA BK/RD/BU, 480 V BR/OR/YE, IEC BN/BK/GY. ``number``,
    ``net``, and ``pins`` (L1/L2/L3 and the motor leads) are already passed so
    that change can read the phase here without a new call site.
    """

    del project, number, net, pins
    return "BK"


def _builtin_color(project: Project, kind: str, *, number: str, net: str, ends: list[_End]) -> str:
    if kind == "power":
        pins = tuple(end.pin for end in ends if end.pin)
        return default_power_color(project, number=number, net=net, pins=pins)
    return _BUILTIN_COLOR_TYPE[kind][0]


def _crossing_for(end: _End) -> str:
    """Strip terminal this end lands on. Blank for a device pin."""

    if end.kind != "terminal":
        return ""
    return end.display


def _make_row(
    project: Project,
    *,
    wire_id: str,
    number: str,
    frm: _End,
    to: _End,
    wires: list[Wire],
) -> _Conductor:
    size, color, wire_type = _merged_spec(project, wires, number, [frm, to])
    marks: list[str] = []
    for end in (frm, to):
        mark = _crossing_for(end)
        if mark and mark not in marks:
            marks.append(mark)
    return _Conductor(
        wire_id=wire_id,
        number=number,
        frm=frm,
        to=to,
        size=size,
        color=color,
        wire_type=wire_type,
        crossing=", ".join(marks),
    )


def _rows_for_segments(
    project: Project,
    segs: list[_Seg],
) -> list[_Conductor]:
    """Real pin-to-pin rows. Rails are daisy-chained. Junctions are skipped."""

    if not segs:
        return []
    nodes: dict[tuple[str, str], _End] = {}
    edges: list[tuple[tuple[str, str], tuple[str, str], _Seg]] = []
    adj: dict[tuple[str, str], list[tuple[str, str]]] = {}

    def touch(end: _End) -> tuple[str, str]:
        key = _node_key(end)
        nodes.setdefault(key, end)
        adj.setdefault(key, [])
        return key

    def link(a: tuple[str, str], b: tuple[str, str]) -> None:
        if a == b or b in adj[a]:
            return
        adj[a].append(b)
        adj[b].append(a)

    for seg in segs:
        left = touch(seg.left)
        right = touch(seg.right)
        edges.append((left, right, seg))
        link(left, right)

    by_link: dict[str, list[tuple[str, str]]] = {}
    by_body: dict[tuple[str, str], list[tuple[str, str]]] = {}
    by_rail: dict[str, list[tuple[str, str]]] = {}
    for key, end in nodes.items():
        if end.passthrough and end.link:
            by_link.setdefault(end.link, []).append(key)
        if end.passthrough and end.device:
            by_body.setdefault((end.sheet_id, end.device.upper()), []).append(key)
        if end.kind == "rail":
            by_rail.setdefault(end.identity, []).append(key)
    for keys in by_link.values():
        head = keys[0]
        for other in keys[1:]:
            link(head, other)
    for keys in by_body.values():
        head = keys[0]
        for other in keys[1:]:
            link(head, other)
    for keys in by_rail.values():
        head = keys[0]
        for other in keys[1:]:
            link(head, other)

    seen: set[tuple[str, str]] = set()
    components: list[set[tuple[str, str]]] = []
    for start in nodes:
        if start in seen:
            continue
        stack = [start]
        comp: set[tuple[str, str]] = set()
        while stack:
            key = stack.pop()
            if key in seen:
                continue
            seen.add(key)
            comp.add(key)
            stack.extend(adj.get(key, []))
        components.append(comp)

    rows: list[_Conductor] = []
    for comp in components:
        comp_edges = [edge for edge in edges if edge[0] in comp and edge[1] in comp]
        direct = [
            edge
            for edge in comp_edges
            if _is_real(nodes[edge[0]]) and _is_real(nodes[edge[1]])
        ]
        for left_key, right_key, seg in direct:
            rows.append(
                _make_row(
                    project,
                    wire_id=seg.wire.id,
                    number=seg.number,
                    frm=nodes[left_key],
                    to=nodes[right_key],
                    wires=[seg.wire],
                )
            )
        bus_seen: set[tuple[str, str]] = set()
        rail_keys = {key for key in comp if nodes[key].kind == "rail"}
        if rail_keys:
            bus = rail_keys | {key for key in comp if nodes[key].passthrough}
            landings: list[_End] = []
            taken: set[tuple[str, str]] = set()
            stack = list(rail_keys)
            while stack:
                key = stack.pop()
                if key in bus_seen or key not in bus:
                    continue
                bus_seen.add(key)
                for neighbor in adj.get(key, []):
                    if neighbor not in comp:
                        continue
                    end = nodes[neighbor]
                    if _is_real(end):
                        if neighbor not in taken:
                            taken.add(neighbor)
                            landings.append(end)
                    elif neighbor in bus and neighbor not in bus_seen:
                        stack.append(neighbor)
            rail_edges = [edge for edge in comp_edges if edge[0] in bus or edge[1] in bus]
            wires = [edge[2].wire for edge in rail_edges]
            number = next((edge[2].number for edge in rail_edges if edge[2].number), "")
            direct_pairs = {frozenset((edge[0], edge[1])) for edge in direct}
            for frm, to in _pair(project, landings):
                if frozenset((_node_key(frm), _node_key(to))) in direct_pairs:
                    continue
                rows.append(
                    _make_row(
                        project,
                        wire_id="+".join(wire.id for wire in wires),
                        number=number,
                        frm=frm,
                        to=to,
                        wires=wires,
                    )
                )
        pass_keys = {key for key in comp if nodes[key].passthrough and key not in bus_seen}
        if not pass_keys:
            continue
        walked: set[tuple[str, str]] = set()
        for start in pass_keys:
            if start in walked:
                continue
            stack = [start]
            cluster: set[tuple[str, str]] = set()
            while stack:
                key = stack.pop()
                if key in walked or key not in pass_keys:
                    continue
                walked.add(key)
                cluster.add(key)
                for neighbor in adj.get(key, []):
                    if neighbor in pass_keys and neighbor not in walked:
                        stack.append(neighbor)
            found: list[_End] = []
            taken_real: set[tuple[str, str]] = set()
            for key in cluster:
                for neighbor in adj.get(key, []):
                    if neighbor not in comp or neighbor in cluster or neighbor in taken_real:
                        continue
                    end = nodes.get(neighbor)
                    if end is None or not _is_real(end):
                        continue
                    taken_real.add(neighbor)
                    found.append(end)
            cluster_edges = [
                edge for edge in comp_edges if edge[0] in cluster or edge[1] in cluster
            ]
            wires = [edge[2].wire for edge in cluster_edges]
            number = next((edge[2].number for edge in cluster_edges if edge[2].number), "")
            wire_ids = [wire.id for wire in wires]
            direct_pairs = {frozenset((edge[0], edge[1])) for edge in direct}
            for frm, to in _pair(project, found):
                if frozenset((_node_key(frm), _node_key(to))) in direct_pairs:
                    continue
                rows.append(
                    _make_row(
                        project,
                        wire_id="+".join(wire_ids),
                        number=number,
                        frm=frm,
                        to=to,
                        wires=wires,
                    )
                )
    return rows


def _conductors(project: Project, catalog: Catalog | None) -> list[_Conductor]:
    rows: list[_Conductor] = []
    for group in wire_networks(project, catalog):
        segs: list[_Seg] = []
        for sheet, wire in group:
            left = _describe(project, sheet, catalog, wire.frm, sibling_y=None)
            right = _describe(project, sheet, catalog, wire.to, sibling_y=None)
            if left.kind == "rail" and right.y is not None:
                left = _describe(project, sheet, catalog, wire.frm, sibling_y=right.y)
            if right.kind == "rail" and left.y is not None:
                right = _describe(project, sheet, catalog, wire.to, sibling_y=left.y)
            segs.append(
                _Seg(
                    sheet=sheet,
                    wire=wire,
                    left=left,
                    right=right,
                    number=str(wire.number or "").strip(),
                )
            )
        rows.extend(_rows_for_segments(project, segs))
    return rows


def _sorted(rows: list[_Conductor], sort: str) -> list[_Conductor]:
    if sort == "device":
        return sorted(
            rows,
            key=lambda row: (
                _tag_pin(row.frm.display),
                _tag_pin(row.to.display),
                _number_key(row.number),
                row.wire_id,
            ),
        )
    return sorted(
        rows,
        key=lambda row: (
            _number_key(row.number),
            _tag_pin(row.frm.display),
            _tag_pin(row.to.display),
            row.wire_id,
        ),
    )


def conductor_records(project: Project, catalog: Catalog | None = None, *, sort: str = "number") -> list[_Conductor]:
    """Sorted conductors. Markers use the same records so both lists agree."""

    return _sorted(_conductors(project, catalog), normalize_sort(sort))


def from_to_rows(
    project: Project,
    catalog: Catalog | None = None,
    *,
    sort: str = "number",
) -> list[dict[str, str]]:
    """One dict per conductor. Extra ``from_loc`` / ``to_loc`` feed the print page."""

    return [row.public() for row in conductor_records(project, catalog, sort=sort)]


def write_table(path: Path | str, rows: list[dict[str, str]], columns: tuple[str, ...] | list[str]) -> Path:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), lineterminator="\r\n", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: str(row.get(key, "") or "") for key in columns})
    return dest


def export_from_to(
    project: Project,
    catalog: Catalog | None,
    path: Path | str,
    *,
    sort: str = "number",
) -> Path:
    rows = [{key: row.get(key, "") for key in FROM_TO_COLUMNS} for row in from_to_rows(project, catalog, sort=sort)]
    return write_table(path, rows, FROM_TO_COLUMNS)
