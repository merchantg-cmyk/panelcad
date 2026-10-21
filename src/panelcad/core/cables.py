"""Field cables that leave the panel.

A cable groups the wires from one panel end to one field device (motor,
sensor, valve, instrument, or remote pushbutton station). The panel end is a
terminal strip, or a drive's output terminals when nothing sits between the
drive and the motor. Conductor count includes a configurable spare. Gauge
comes from the wire, then from a circuit wire-size or motor FLA, and
otherwise stays blank for the user.

Descriptions are standard names (``3C+G #14 TC-ER``, ``3C+G #12 VFD shielded``).
The shield is not part of that copper count. Manufacturer catalog numbers are
never invented: ``part_number`` is blank unless someone typed it or the part
library has that description with an http(s) source URL. Gland size stays
blank unless someone enters one; a library OD is shown, not converted to NPT.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from panelcad.core.models import Cable, CableConductor, Project, Wire

# Same conservative chassis ampacity core.checks uses when a gauge is already set.
# Motor power uses NEC 430.22 (125% of full-load current) against that table,
# and will not go below #14 / 2.5 mm².
_AWG_ORDER = (18, 16, 14, 12, 10, 8, 6, 4)
_MM2_ORDER = (0.5, 0.75, 1.0, 1.5, 2.5, 4.0, 6.0, 10.0, 16.0, 25.0)
_CONTROL_COUNTS = (2, 3, 4, 5, 7, 9, 12, 15, 19, 25)
_PAIR_COUNTS = (1, 2, 4, 6, 8, 12)
_VFD_OUTPUT_PINS = {"U", "V", "W", "T1", "T2", "T3"}

# Spare conductors are not landed on an invented device pin.
SPARE_END = "spare, coiled and taped"
FIELD_CUT_BACK = "cut back and insulated"
GLAND_FLAG = "size per cable OD"

# PowerFlex 520-UM001, Shield Termination - SHLD: the motor-cable shield on a
# drive safety-ground terminal (EMI clamp) is also connected to the motor frame.
VFD_SHIELD_SOURCE = (
    "https://literature.rockwellautomation.com/idc/groups/literature/documents/um/520-um001_-en-e.pdf"
)
VFD_SHIELD_NOTE = (
    "Rockwell PowerFlex 520-UM001: bond the motor-cable shield at a drive safety-ground "
    "terminal with an EMI clamp, and at the motor frame."
)
# 1756-IN063 (Belden 8761): drain to the chassis ground at the analog module;
# the other end is cut back and shrink-wrapped. Same manual, not a Belden
# "ground both ends" note.
INSTRUMENT_SHIELD_SOURCE = (
    "https://literature.rockwellautomation.com/idc/groups/literature/documents/in/1756-in063_-en-p.pdf"
)
INSTRUMENT_SHIELD_NOTE = (
    "Rockwell 1756-IN063: ground the foil and drain at the panel chassis (analog module end); "
    "cut the field end back to the jacket and insulate it."
)
SIGNAL_SHIELD_NOTE = (
    "Rockwell PowerFlex 520-UM001: ground control and signal shields at the source end only."
)

_GROUND_PINS = {"PE", "G", "GND", "E", "GRD"}
_URL_RE = re.compile(r"^https?://", re.I)
_OD_RE = re.compile(r"\bOD\s*([0-9]+(?:\.[0-9]+)?)\s*(mm|in)\b", re.I)
_COLOR_SPEC_RE = re.compile(r"\bcolors?\s*[:=]\s*([^\n]+)", re.I)
_COLOR_PAIR_RE = re.compile(r"([A-Za-z0-9]+)\s*=\s*([^,;\n]+)")
COLOR_UNSET = "per cable"
_UNGROUNDED_RE = re.compile(
    r"\b(cut\s*back|insulated|spare|floating|open|ungrounded|not\s+grounded)\b",
    re.I,
)
_GROUNDED_RE = re.compile(r"\b(frame|pe|chassis|emc|ground|gnd|earth)\b", re.I)


@dataclass
class _Leg:
    sheet_id: str
    sheet_index: int
    wire: Wire
    terminal_ref: str
    terminal_pin: str
    strip: str
    level: str
    y: float
    x: float
    field_ref: str
    field_pin: str
    kind: str
    ground: bool = False
    panel_device: str = ""


@dataclass
class _Spec:
    conductors: int
    pairs: int = 0
    ground: bool = False
    shield: bool = False
    drain: bool = False
    signal_slots: int = 0
    cable_type_body: str = ""
    phrase: str = "tcer"  # tcer, vfd, instrument


@dataclass
class _Built:
    kind: str
    spec: _Spec
    gauge: str
    from_strip: str
    from_device: str = ""
    from_terminals: list[str] = field(default_factory=list)
    assignments: list[CableConductor] = field(default_factory=list)
    vfd: bool = False
    shield_bond: str = ""
    shield_source: str = ""
    shield_note: str = ""


def pull_length(cable: Cable) -> float | None:
    """Installed length plus the optional slack percent."""

    if cable.length is None:
        return None
    slack = float(cable.slack_pct or 0.0)
    return round(float(cable.length) * (1.0 + slack / 100.0), 2)


def cable_dict(cable: Cable) -> dict[str, Any]:
    data = cable.model_dump(mode="json")
    data["pull_length"] = pull_length(cable)
    return data


def legacy_cable_rows(project: Project) -> list[dict[str, str]]:
    """Wires that share a ``wire.cable`` marker. Unchanged for older files."""

    groups: dict[str, list[tuple[Any, Wire]]] = {}
    for sheet in project.sheets:
        for wire in sheet.wires:
            cable = str(wire.cable or "").strip()
            if not cable:
                continue
            groups.setdefault(cable, []).append((sheet, wire))
    rows: list[dict[str, str]] = []
    for cable, items in groups.items():
        first = items[0][1]
        rows.append(
            {
                "cable": cable,
                "count": str(len(items)),
                "wire_numbers": ",".join(str(wire.number or wire.id) for _s, wire in items),
                "from": ";".join(wire.frm for _s, wire in items),
                "to": ";".join(wire.to for _s, wire in items),
                "color": first.color,
                "gauge": first.gauge,
            }
        )
    return rows


def color_text(land: CableConductor) -> str:
    """Schedule color. Blank means the jacket chart is still unknown.

    A typed color, a color already on the wire, or a color named by one
    library cable part with an http source is printed as given. Anything
    else prints ``per cable``. The ground conductor is not given a jacket
    color; its role prints as ``GND``.
    """

    entered = str(land.color or "").strip()
    if entered:
        return entered
    return COLOR_UNSET


def role_text(land: CableConductor) -> str:
    if land.role == "ground":
        return "GND"
    return land.role


def gland_text(cable: Cable) -> str:
    """What the schedule prints for the cord grip.

    A typed gland wins. Otherwise a library OD that came with an http source
    is printed as an OD, not as a guessed NPT or metric trade size. With
    neither, the cell tells the shop to size the grip from the cable OD.
    """

    entered = str(cable.gland or "").strip()
    if entered:
        return entered
    od = str(cable.od or "").strip()
    if od and _URL_RE.match(str(cable.od_source or "").strip()):
        return f"OD {od}"
    return GLAND_FLAG


def shield_label(cable: Cable) -> str:
    """Schedule shield column: both, panel, field, or open."""

    if not cable.shield and not cable.drain:
        return ""
    bond = str(cable.shield_bond or "")
    if bond == "none":
        return "open"
    if bond in {"both", "panel", "field"}:
        return bond
    drains = [item for item in cable.assignments if item.shield or item.id == "SH"]
    panel = any(shield_end_grounded(item.panel_terminal) for item in drains)
    field_end = any(shield_end_grounded(item.field_terminal) for item in drains)
    if panel and field_end:
        return "both"
    if panel:
        return "panel"
    if field_end:
        return "field"
    return "open"


def shield_end_grounded(text: str) -> bool:
    """True when this shield landing is a bond to earth, a frame, or a PE terminal."""

    raw = str(text or "").strip()
    if not raw:
        return False
    if raw.casefold() in {SPARE_END.casefold(), FIELD_CUT_BACK.casefold()}:
        return False
    if _UNGROUNDED_RE.search(raw):
        return False
    if _GROUNDED_RE.search(raw):
        return True
    return _terminal_level_is_ground(raw)


def schedule_row(cable: Cable) -> dict[str, str]:
    lands = [item for item in cable.assignments if item.wire_id and not item.spare]
    numbers = [item.wire_number for item in lands if item.wire_number]
    colors = [item.color for item in cable.assignments if str(item.color or "").strip() and not item.spare]
    pull = pull_length(cable)
    return {
        "cable": cable.tag,
        "count": str(len(lands)),
        "wire_numbers": ",".join(numbers),
        "from": ";".join(item.panel_terminal for item in cable.assignments if item.panel_terminal),
        "to": ";".join(item.field_terminal for item in cable.assignments if item.field_terminal),
        "color": colors[0] if colors else COLOR_UNSET,
        "gauge": cable.gauge,
        "tag": cable.tag,
        "from_strip": cable.from_strip,
        "from_device": cable.from_device,
        "from_terminals": ",".join(cable.from_terminals),
        "to_device": cable.to_device,
        "to_kind": cable.to_kind,
        "cable_type": cable.cable_type,
        "conductors": str(cable.conductors or 0),
        "shield": shield_label(cable),
        "shield_bond": cable.shield_bond,
        "drain": "yes" if cable.drain else "",
        "length": _num(cable.length),
        "length_unit": cable.length_unit if cable.length is not None else "",
        "slack_pct": _num(cable.slack_pct),
        "pull_length": _num(pull),
        "gland": gland_text(cable),
        "route": cable.route,
        "part_number": cable.part_number,
        "part_source": cable.part_source,
    }


SCHEDULE_COLUMNS: tuple[str, ...] = (
    "cable",
    "count",
    "wire_numbers",
    "from",
    "to",
    "color",
    "gauge",
    "tag",
    "from_strip",
    "from_device",
    "from_terminals",
    "to_device",
    "to_kind",
    "cable_type",
    "conductors",
    "shield",
    "shield_bond",
    "drain",
    "length",
    "length_unit",
    "slack_pct",
    "pull_length",
    "gland",
    "route",
    "part_number",
    "part_source",
)
PULL_COLUMNS: tuple[str, ...] = (
    "cable_type",
    "gauge",
    "cables",
    "unit",
    "run_length",
    "pull_length",
    "missing_length",
)
TERMINATION_COLUMNS: tuple[str, ...] = (
    "cable",
    "conductor",
    "color",
    "role",
    "wire_number",
    "panel_strip",
    "panel_terminal",
    "field_device",
    "field_terminal",
    "spare",
    "shield",
)


def write_rows_csv(path: Path | str, rows: list[dict[str, str]], columns: tuple[str, ...]) -> Path:
    """Write a cable CSV with a header even when there are no rows.

    ``newline=""`` plus ``lineterminator="\\r\\n"`` is one CRLF on Windows and Linux.
    """

    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    names = list(rows[0].keys()) if rows else list(columns)
    with dest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=names, extrasaction="ignore", lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)
    return dest


def cable_rows(project: Project) -> list[dict[str, str]]:
    """Schedule rows when the project has cables, else the marker grouping."""

    if not project.cables:
        return legacy_cable_rows(project)
    rows = [schedule_row(cable) for cable in project.cables]
    covered = {cable.tag for cable in project.cables}
    keys = list(rows[0].keys())
    for legacy in legacy_cable_rows(project):
        if legacy["cable"] in covered:
            continue
        padded = {key: "" for key in keys}
        padded["cable"] = legacy["cable"]
        padded["tag"] = legacy["cable"]
        padded["count"] = legacy["count"]
        padded["wire_numbers"] = legacy["wire_numbers"]
        padded["from"] = legacy["from"]
        padded["to"] = legacy["to"]
        padded["color"] = legacy["color"]
        padded["gauge"] = legacy["gauge"]
        rows.append(padded)
    return rows


def pull_rows(project: Project) -> list[dict[str, str]]:
    """Total pull length per cable type. Length is the user value plus slack."""

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for cable in project.cables:
        key = (cable.cable_type or cable.tag, cable.length_unit or "ft")
        bucket = groups.setdefault(
            key,
            {
                "cable_type": cable.cable_type or cable.tag,
                "gauge": cable.gauge,
                "unit": cable.length_unit or "ft",
                "cables": 0,
                "run": 0.0,
                "pull": 0.0,
                "missing": 0,
            },
        )
        bucket["cables"] += 1
        if cable.gauge and not bucket["gauge"]:
            bucket["gauge"] = cable.gauge
        pull = pull_length(cable)
        if cable.length is None or pull is None:
            bucket["missing"] += 1
            continue
        bucket["run"] += float(cable.length)
        bucket["pull"] += float(pull)
    rows: list[dict[str, str]] = []
    for (_type, _unit), bucket in groups.items():
        rows.append(
            {
                "cable_type": bucket["cable_type"],
                "gauge": bucket["gauge"],
                "cables": str(bucket["cables"]),
                "unit": bucket["unit"],
                "run_length": _num(bucket["run"]) if bucket["cables"] != bucket["missing"] else "",
                "pull_length": _num(bucket["pull"]) if bucket["cables"] != bucket["missing"] else "",
                "missing_length": str(bucket["missing"]) if bucket["missing"] else "",
            }
        )
    return rows


def termination_rows(project: Project) -> list[dict[str, str]]:
    """Conductor to the panel terminal and the field terminal."""

    by_tag = {cable.tag: cable for cable in project.cables}
    rows: list[dict[str, str]] = []
    for cable in project.cables:
        for land in cable.assignments:
            rows.append(
                {
                    "cable": cable.tag,
                    "conductor": land.id,
                    "color": color_text(land),
                    "role": role_text(land),
                    "wire_number": land.wire_number,
                    "panel_strip": cable.from_strip,
                    "panel_terminal": land.panel_terminal,
                    "field_device": by_tag[cable.tag].to_device,
                    "field_terminal": land.field_terminal,
                    "spare": "yes" if land.spare else "",
                    "shield": "yes" if land.shield else "",
                }
            )
    return rows


def propose_cables(
    project: Project,
    catalog: Any,
    parts: Any = None,
    *,
    spares: int | None = None,
    replace: bool = False,
    show_tags: bool = True,
    route: str | None = None,
) -> list[Cable]:
    """Group each field device's terminal wires into one cable.

    ``spares`` is extra conductors. ``None`` uses a spare pair on instrument
    cables, one spare on other control cables, and no extra phase conductor
    on a motor (ground and a VFD shield are part of the construction).
    """

    if spares is not None and int(spares) < 0:
        raise ValueError("spares cannot be negative")
    spare_n = None if spares is None else int(spares)
    old_tags = {cable.tag for cable in project.cables}
    if replace:
        project.cables.clear()
        sync_wire_markers(project, managed_tags=old_tags)
        skip: set[str] = set()
    else:
        skip = {cable.to_device for cable in project.cables if cable.to_device}
    legs = _collect_legs(project, catalog)
    groups: dict[str, list[_Leg]] = {}
    for leg in legs:
        if leg.field_ref in skip:
            continue
        groups.setdefault(leg.field_ref, []).append(leg)
    order = sorted(groups, key=lambda ref: _group_key(groups[ref]))
    tray = route if route is not None else _only_tray(project)
    standard = _standard(project)
    created: list[Cable] = []
    for ref in order:
        built = _build_group(project, catalog, _boundary_legs(groups[ref]), spare_n, standard)
        tag = next_cable_tag(project)
        cable_type = _describe(built.spec, built.gauge)
        part_number, part_source = library_part_for(parts, cable_type)
        od, od_source = library_od_for(parts, cable_type)
        _apply_library_colors(built.assignments, library_colors_for(parts, cable_type))
        cable = Cable(
            tag=tag,
            from_strip=built.from_strip,
            from_device=built.from_device,
            from_terminals=built.from_terminals,
            to_device=ref,
            to_kind=built.kind,
            cable_type=cable_type,
            conductors=built.spec.conductors,
            gauge=built.gauge,
            shield=built.spec.shield,
            drain=built.spec.drain,
            shield_bond=built.shield_bond or "",
            shield_source=built.shield_source,
            shield_note=built.shield_note,
            length_unit="m" if standard == "IEC" else "ft",
            route=tray or "",
            part_number=part_number,
            part_source=part_source,
            od=od,
            od_source=od_source,
            show_tag=bool(show_tags),
            pairs=built.spec.pairs,
            assignments=built.assignments,
        )
        project.cables.append(cable)
        created.append(cable)
    sync_wire_markers(project, managed_tags=old_tags)
    return created


def add_cable(
    project: Project,
    parts: Any = None,
    **fields: Any,
) -> Cable:
    fields = dict(fields)
    tag = str(fields.pop("tag", "") or "").strip() or next_cable_tag(project)
    if any(cable.tag == tag for cable in project.cables):
        raise ValueError(f"cable {tag} already exists")
    standard = _standard(project)
    cable = Cable(tag=tag, length_unit="m" if standard == "IEC" else "ft")
    project.cables.append(cable)
    if fields:
        cable = update_cable(project, tag, **fields)
    if cable.cable_type:
        if not cable.part_number:
            number, source = library_part_for(parts, cable.cable_type)
            if number:
                cable.part_number = number
                cable.part_source = source
        if not cable.od:
            od, od_source = library_od_for(parts, cable.cable_type)
            if od:
                cable.od = od
                cable.od_source = od_source
        _apply_library_colors(cable.assignments, library_colors_for(parts, cable.cable_type))
        sync_wire_markers(project)
    return cable


def update_cable(project: Project, tag: str, /, **fields: Any) -> Cable:
    cable = _need(project, tag)
    new_tag = fields.pop("tag", None)
    old_bond = cable.shield_bond
    if new_tag is not None:
        renamed = str(new_tag).strip()
        if renamed and renamed != cable.tag:
            if any(other.tag == renamed for other in project.cables):
                raise ValueError(f"cable {renamed} already exists")
            cable.tag = renamed
    keep_source = "shield_source" in fields
    _apply_fields(cable, fields)
    if "shield_bond" in fields and cable.shield_bond != old_bond:
        _rewrite_shield(cable, keep_source=keep_source)
    if cable.part_source:
        _require_url(cable.part_source)
    sync_wire_markers(project, managed_tags={tag, cable.tag})
    return cable


def delete_cable(project: Project, tag: str) -> None:
    key = str(tag).strip()
    kept = [cable for cable in project.cables if cable.tag != key]
    if len(kept) == len(project.cables):
        raise KeyError(f"no cable '{key}'")
    project.cables = kept
    sync_wire_markers(project, managed_tags={key})


def assign_conductors(project: Project, tag: str, rows: list[dict[str, Any]]) -> Cable:
    cable = _need(project, tag)
    lands: list[CableConductor] = []
    for raw in rows:
        if not str(raw.get("id") or "").strip():
            raise ValueError("each conductor needs an id")
        lands.append(CableConductor.model_validate(raw))
    cable.assignments = lands
    sync_wire_markers(project)
    return cable


def remap_cable(
    project: Project,
    catalog: Any,
    parts: Any = None,
    tag: str = "",
    *,
    spares: int | None = None,
) -> Cable:
    """Rebuild one cable's conductor map from the field wires. Keeps length and route."""

    cable = _need(project, tag)
    if not cable.to_device:
        raise ValueError(f"{cable.tag} has no field device to map")
    legs = _boundary_legs(
        [leg for leg in _collect_legs(project, catalog) if leg.field_ref == cable.to_device]
    )
    if not legs:
        raise ValueError(f"no field wires from a panel terminal to {cable.to_device}")
    standard = _standard(project)
    built = _build_group(project, catalog, legs, spares, standard)
    cable_type = _describe(built.spec, built.gauge)
    cable.from_strip = built.from_strip
    cable.from_device = built.from_device
    cable.from_terminals = built.from_terminals
    cable.to_kind = built.kind
    cable.cable_type = cable_type
    cable.conductors = built.spec.conductors
    cable.gauge = built.gauge
    cable.shield = built.spec.shield
    cable.drain = built.spec.drain
    cable.shield_bond = built.shield_bond or ""
    cable.shield_source = built.shield_source
    cable.shield_note = built.shield_note
    cable.pairs = built.spec.pairs
    cable.assignments = built.assignments
    if not cable.part_number:
        number, source = library_part_for(parts, cable_type)
        cable.part_number = number
        cable.part_source = source
    if not cable.od:
        od, od_source = library_od_for(parts, cable_type)
        cable.od = od
        cable.od_source = od_source
    _apply_library_colors(cable.assignments, library_colors_for(parts, cable_type))
    sync_wire_markers(project)
    return cable


def next_cable_tag(project: Project) -> str:
    """NEMA ``W101``, IEC ``-W1``, or ``ref_prefixes.cable`` when set."""

    used = {cable.tag for cable in project.cables}
    for sheet in project.sheets:
        for wire in sheet.wires:
            mark = str(wire.cable or "").strip()
            if mark:
                used.add(mark)
    style, prefix = _tag_style(project)
    if style == "seq":
        number = 1
        while f"{prefix}{number}" in used:
            number += 1
        return f"{prefix}{number}"
    number = 101
    while f"{prefix}{number}" in used:
        number += 1
    return f"{prefix}{number}"


def sync_wire_markers(project: Project, *, managed_tags: set[str] | None = None) -> None:
    """Optional schematic cable tag: the wire's ``cable`` marker, already drawn."""

    tags = {cable.tag for cable in project.cables}
    if managed_tags:
        tags |= {str(item) for item in managed_tags if item}
    owned: dict[tuple[str, str], str] = {}
    for cable in project.cables:
        if not cable.show_tag:
            continue
        for land in cable.assignments:
            if not land.wire_id or land.spare:
                continue
            owned[(land.sheet, land.wire_id)] = cable.tag
    for sheet in project.sheets:
        for wire in sheet.wires:
            key = (sheet.id, wire.id)
            mark = str(wire.cable or "").strip()
            if key in owned:
                wire.cable = owned[key]
            elif mark in tags:
                wire.cable = ""


def library_part_for(parts: Any, cable_type: str) -> tuple[str, str]:
    """Return (catalog, source URL) only for one exact library match."""

    hits = _cable_part_hits(parts, cable_type)
    if len(hits) != 1:
        return "", ""
    return str(hits[0].catalog), str(hits[0].source)


def library_colors_for(parts: Any, cable_type: str) -> dict[str, str]:
    """Conductor id to color, from one sourced library cable part.

    Notes or the description may say ``colors: 1=BLK, 2=RED, G=GRN``.
    A comma list with no ids fills conductors 1, 2, 3 in that order.
    Zero or several matching parts stay empty.
    """

    hits = _cable_part_hits(parts, cable_type)
    if len(hits) != 1:
        return {}
    part = hits[0]
    blob = f"{getattr(part, 'description', '')}\n{getattr(part, 'notes', '')}"
    match = _COLOR_SPEC_RE.search(blob)
    if not match:
        return {}
    body = match.group(1).strip().rstrip(".")
    named = _COLOR_PAIR_RE.findall(body)
    if named:
        return {cid.upper(): color.strip() for cid, color in named if color.strip()}
    tokens = [part.strip() for part in re.split(r"[,/|]", body) if part.strip()]
    return {str(index + 1): color for index, color in enumerate(tokens)}


def library_od_for(parts: Any, cable_type: str) -> tuple[str, str]:
    """Return (``11.4 mm``, source URL) when that one part states an OD.

    The OD is not a cord-grip trade size. Zero or several matches stay blank.
    """

    hits = _cable_part_hits(parts, cable_type)
    if len(hits) != 1:
        return "", ""
    part = hits[0]
    blob = f"{getattr(part, 'description', '')} {getattr(part, 'notes', '')}"
    match = _OD_RE.search(blob)
    if not match:
        return "", ""
    number, unit = match.group(1), match.group(2).lower()
    return f"{number} {unit}", str(part.source)


def _cable_part_hits(parts: Any, cable_type: str) -> list[Any]:
    want = _norm_desc(cable_type)
    if parts is None or not want or not hasattr(parts, "parts"):
        return []
    hits = []
    for part in parts.parts.values():
        if not _is_sourced_cable_part(part):
            continue
        desc = _norm_desc(getattr(part, "description", ""))
        catalog = _norm_desc(getattr(part, "catalog", ""))
        if want == desc or want == catalog:
            hits.append(part)
    return hits


def _is_sourced_cable_part(part: Any) -> bool:
    category = str(getattr(part, "category", "") or "").lower()
    if not category.startswith("cable"):
        return False
    source = str(getattr(part, "source", "") or "").strip()
    return bool(_URL_RE.match(source))


def field_kind(device: Any, comp: Any) -> str | None:
    """motor, valve, sensor, instrument, pushbutton, or None when it stays in the panel."""

    if device is None or comp is None:
        return None
    from panelcad.core.electrical import symbol_role

    if symbol_role(device) == "terminal":
        return None
    props: dict[str, Any] = {}
    props.update(getattr(device, "properties", None) or {})
    props.update(getattr(comp, "properties", None) or {})
    cat = str(getattr(device, "category", "") or "").lower()
    lid = str(getattr(device, "library_id", "") or "").lower()
    signal = str(props.get("signal") or props.get("cable_class") or "").strip().lower()
    loc = str(props.get("location") or "").strip().lower()
    flag = str(props.get("field") or "").strip().lower()
    station = str(props.get("station") or "").strip()
    mount = ""
    try:
        from panelcad.library.panel import effective_footprint

        mount = str(effective_footprint(device, comp).mount or "")
    except Exception:  # noqa: BLE001
        mount = ""
    explicit = loc == "field" or flag in {"1", "true", "yes", "field"} or mount == "field" or bool(station)
    if signal in {"analog", "4-20", "4-20ma", "ma", "instrument"} or cat.startswith("instrument"):
        return "instrument"
    if cat == "load.motor" or ".motor" in lid:
        return "motor"
    if cat == "load.solenoid" or "valve" in cat or "solenoid" in cat:
        return "valve"
    if cat == "operator.limit" or cat.startswith("sensor"):
        return "sensor"
    if explicit and (cat.startswith("operator.") or station):
        return "pushbutton"
    if explicit:
        return "field"
    return None


def gauge_label(text: str, standard: str = "NEMA/ANSI") -> str:
    """``#12`` or ``2.5 mm²``. Leaves an unrecognized size as typed."""

    raw = str(text or "").strip()
    if not raw:
        return ""
    if re.search(r"mm", raw, re.I):
        mm = _parse_mm2(raw)
        return f"{mm:g} mm²" if mm is not None else raw
    if raw.startswith("#") or re.search(r"awg", raw, re.I):
        awg = _parse_awg(raw, explicit=True)
        return f"#{awg}" if awg is not None else raw
    if re.fullmatch(r"\d+\.\d+", raw):
        return f"{float(raw):g} mm²"
    if re.fullmatch(r"\d{1,2}", raw):
        size = int(raw)
        in_mm = float(size) in _mm2_table()
        in_awg = size in _awg_table()
        if standard == "IEC" and in_mm:
            return f"{size:g} mm²"
        if in_awg:
            return f"#{size}"
        if in_mm:
            return f"{size:g} mm²"
    return raw


def _boundary_legs(legs: list[_Leg]) -> list[_Leg]:
    """A terminal strip is the field boundary. Drive pins are used only with no strip."""

    stripped = [leg for leg in legs if leg.strip]
    return stripped or list(legs)


def _spare_land(cid: str, color: str = "") -> CableConductor:
    return CableConductor(
        id=cid,
        color=color,
        panel_terminal=SPARE_END,
        field_terminal=SPARE_END,
        role="spare",
        spare=True,
    )


def _apply_library_colors(assignments: list[CableConductor], colors: dict[str, str]) -> None:
    """Fill blank conductor colors from a library chart. Typed colors stay."""

    if not colors:
        return
    for land in assignments:
        if str(land.color or "").strip():
            continue
        color = colors.get(land.id.upper()) or colors.get(land.id)
        if color:
            land.color = color


def _real_terminal(ref: str) -> bool:
    text = str(ref or "").strip()
    if not text or " " in text:
        return False
    if text.casefold() in {SPARE_END.casefold(), FIELD_CUT_BACK.casefold()}:
        return False
    return True


def _terminal_level_is_ground(ref: str) -> bool:
    tail = str(ref or "").split(".")[-1].split("-")[-1].upper()
    return tail in _GROUND_PINS


def _default_shield(
    phrase: str,
    device_ref: str,
    panel_device: str,
    from_strip: str,
    claimed: str,
) -> tuple[str, str, str, str, str]:
    """Panel land, field land, bond, source URL, note. Plain shields stay blank."""

    if phrase == "vfd":
        if from_strip and claimed:
            panel = claimed
        elif panel_device:
            panel = f"{panel_device} PE / EMC shield clamp"
        else:
            panel = "panel PE / EMC shield clamp"
        return panel, f"{device_ref} frame", "both", VFD_SHIELD_SOURCE, VFD_SHIELD_NOTE
    if phrase == "instrument":
        panel = claimed or "panel chassis ground"
        return panel, FIELD_CUT_BACK, "panel", INSTRUMENT_SHIELD_SOURCE, INSTRUMENT_SHIELD_NOTE
    return "", "", "", "", ""


def _ground_ref_on_cable(cable: Cable) -> str:
    refs = list(cable.from_terminals) + [item.panel_terminal for item in cable.assignments]
    for ref in refs:
        if _real_terminal(ref) and _terminal_level_is_ground(ref):
            return ref
    return ""


def _rewrite_shield(cable: Cable, *, keep_source: bool) -> None:
    """Reland only the drain when the bond actually changes."""

    bond = cable.shield_bond
    if not bond:
        return
    device = cable.to_device or "field"
    claimed = _ground_ref_on_cable(cable)
    if bond == "both":
        if cable.from_strip and claimed:
            panel = claimed
        elif cable.from_device:
            panel = f"{cable.from_device} PE / EMC shield clamp"
        elif claimed:
            panel = claimed
        else:
            panel = "panel PE / EMC shield clamp"
        field_end = f"{device} frame"
        source, note = VFD_SHIELD_SOURCE, VFD_SHIELD_NOTE
    elif bond == "panel":
        panel = claimed or "panel chassis ground"
        field_end = FIELD_CUT_BACK
        if (cable.to_kind or "").lower() == "instrument":
            source, note = INSTRUMENT_SHIELD_SOURCE, INSTRUMENT_SHIELD_NOTE
        else:
            source, note = VFD_SHIELD_SOURCE, SIGNAL_SHIELD_NOTE
    elif bond == "field":
        panel = FIELD_CUT_BACK
        field_end = f"{device} frame"
        source, note = "", ""
    else:
        panel = FIELD_CUT_BACK
        field_end = FIELD_CUT_BACK
        source, note = "", ""
    if not keep_source:
        cable.shield_source = source
        cable.shield_note = note
    if bond in {"both", "panel", "field"}:
        cable.shield = True
        cable.drain = True
    shield = next((item for item in cable.assignments if item.shield or item.id == "SH"), None)
    if shield is None:
        cable.assignments.append(
            CableConductor(
                id="SH",
                color="",
                panel_terminal=panel,
                field_terminal=field_end,
                role="shield",
                shield=True,
            )
        )
    else:
        shield.panel_terminal = panel
        shield.field_terminal = field_end
        shield.role = "shield"
        shield.shield = True
        shield.spare = False
    if _real_terminal(panel) and panel not in cable.from_terminals:
        cable.from_terminals.append(panel)


def _as_drive_leg(
    sheet_id: str,
    sheet_index: int,
    wire: Wire,
    a_ref: str,
    a_pin: str,
    a: tuple[Any, Any, Any],
    b_ref: str,
    b_pin: str,
    b: tuple[Any, Any, Any],
) -> _Leg | None:
    """A VFD output pin (U/V/W or T1/T2/T3) landed on a field motor, with no strip."""

    pairs = (
        (a_ref, a_pin, a, b_ref, b_pin, b),
        (b_ref, b_pin, b, a_ref, a_pin, a),
    )
    for dev_ref, dev_pin, dev, field_ref, field_pin, field_item in pairs:
        _sheet, comp, device = dev
        if not _device_is_vfd(device):
            continue
        pin = dev_pin.upper()
        if pin not in _VFD_OUTPUT_PINS and pin not in _GROUND_PINS:
            continue
        _field_sheet, field_comp, field_device = field_item
        if field_kind(field_device, field_comp) != "motor":
            continue
        ground = pin in _GROUND_PINS or field_pin.upper() in _GROUND_PINS
        return _Leg(
            sheet_id=sheet_id,
            sheet_index=sheet_index,
            wire=wire,
            terminal_ref=f"{dev_ref}.{dev_pin}",
            terminal_pin=dev_pin,
            strip="",
            level=dev_pin,
            y=float(comp.y),
            x=float(comp.x),
            field_ref=field_ref,
            field_pin=field_pin,
            kind="motor",
            ground=ground,
            panel_device=dev_ref,
        )
    return None


def _build_group(
    project: Project,
    catalog: Any,
    legs: list[_Leg],
    spares: int | None,
    standard: str,
) -> _Built:
    kind = legs[0].kind
    signal = [leg for leg in legs if not leg.ground]
    grounds = [leg for leg in legs if leg.ground]
    signal.sort(key=lambda leg: (leg.sheet_index, leg.y, leg.x, leg.terminal_ref))
    vfd = kind == "motor" and (any(leg.panel_device for leg in legs) or _group_is_vfd(project, catalog, legs))
    extra = _extra_conductors(kind, spares)
    gauge = _pick_gauge(project, catalog, signal or legs, kind, standard)
    spec = _spec_for(kind, vfd, len(signal), extra)
    iec = standard == "IEC"
    named: dict[str, int] = {}
    for leg in legs:
        if leg.strip:
            named[leg.strip] = named.get(leg.strip, 0) + 1
    from_strip = max(named, key=lambda name: (named[name], name)) if named else ""
    devices = {leg.panel_device for leg in legs if leg.panel_device}
    from_device = next(iter(devices)) if len(devices) == 1 and not from_strip else ""
    assignments, terminals, shield = _assign(
        signal,
        grounds,
        spec,
        project,
        catalog,
        iec,
        legs[0].field_ref,
        panel_device=from_device,
        from_strip=from_strip,
    )
    return _Built(
        kind=kind,
        spec=spec,
        gauge=gauge,
        from_strip=from_strip,
        from_device=from_device,
        from_terminals=terminals,
        assignments=assignments,
        vfd=vfd,
        shield_bond=shield[0],
        shield_source=shield[1],
        shield_note=shield[2],
    )


def _assign(
    signal: list[_Leg],
    grounds: list[_Leg],
    spec: _Spec,
    project: Project,
    catalog: Any,
    iec: bool,
    device_ref: str,
    *,
    panel_device: str,
    from_strip: str,
) -> tuple[list[CableConductor], list[str], tuple[str, str, str]]:
    lands: list[CableConductor] = []
    terminals: list[str] = []

    def _add_terminal(ref: str) -> None:
        if _real_terminal(ref) and ref not in terminals:
            terminals.append(ref)

    if spec.phrase == "instrument":
        slots = [f"{index // 2 + 1}{'A' if index % 2 == 0 else 'B'}" for index in range(spec.pairs * 2)]
        for index, cid in enumerate(slots):
            if index < len(signal):
                leg = signal[index]
                _add_terminal(leg.terminal_ref)
                lands.append(_from_leg(leg, cid, _wire_color(leg), "signal", spare=False))
            else:
                lands.append(_spare_land(cid))
    else:
        for index, leg in enumerate(signal):
            _add_terminal(leg.terminal_ref)
            lands.append(
                _from_leg(leg, str(index + 1), _wire_color(leg), "phase" if spec.ground else "signal", spare=False)
            )
        spare_slots = max(0, spec.signal_slots - len(signal))
        for offset in range(spare_slots):
            lands.append(_spare_land(str(len(signal) + offset + 1)))
    if spec.ground:
        if grounds:
            for index, leg in enumerate(grounds):
                cid = "G" if index == 0 else f"G{index + 1}"
                _add_terminal(leg.terminal_ref)
                lands.append(_from_leg(leg, cid, _wire_color(leg), "ground", spare=False))
        elif spec.phrase == "vfd" and panel_device and not from_strip:
            lands.append(
                CableConductor(
                    id="G",
                    color="",
                    panel_terminal=f"{panel_device} PE",
                    field_terminal=f"{device_ref} frame",
                    role="ground",
                )
            )
        else:
            strips = {leg.strip for leg in signal} or {leg.strip for leg in grounds}
            used = set(terminals)
            claimed = _claim_ground(project, catalog, strips, used)
            panel = claimed or ""
            if panel:
                _add_terminal(panel)
            lands.append(
                CableConductor(
                    id="G",
                    color="",
                    panel_terminal=panel,
                    field_terminal=f"{device_ref}.PE",
                    role="ground",
                )
            )
    bond, source, note = "", "", ""
    if spec.shield or spec.drain:
        strips = {leg.strip for leg in (*signal, *grounds) if leg.strip}
        if from_strip:
            strips.add(from_strip)
        ground_panel = next(
            (
                land.panel_terminal
                for land in lands
                if land.role == "ground" and _real_terminal(land.panel_terminal)
            ),
            "",
        )
        claimed = ground_panel or (_claim_ground(project, catalog, strips, set(terminals)) if strips else "")
        panel, field_end, bond, source, note = _default_shield(
            spec.phrase, device_ref, panel_device, from_strip, claimed
        )
        if _real_terminal(panel):
            _add_terminal(panel)
        lands.append(
            CableConductor(
                id="SH",
                color="",
                panel_terminal=panel,
                field_terminal=field_end,
                role="shield",
                shield=True,
            )
        )
    return lands, terminals, (bond, source, note)


def _wire_color(leg: _Leg) -> str:
    """A color already on the wire. An empty wire is not given a jacket color."""

    return str(leg.wire.color or "").strip()


def _from_leg(leg: _Leg, cid: str, color: str, role: str, *, spare: bool) -> CableConductor:
    return CableConductor(
        id=cid,
        color=color,
        panel_terminal=leg.terminal_ref,
        field_terminal=f"{leg.field_ref}.{leg.field_pin}",
        sheet=leg.sheet_id,
        wire_id=leg.wire.id,
        wire_number=str(leg.wire.number or leg.wire.net or "").strip(),
        role=role,
        spare=spare,
    )


def _spec_for(kind: str, vfd: bool, landed: int, extra: int) -> _Spec:
    if kind == "instrument":
        total = max(2, landed + extra)
        pairs_needed = max(1, (total + 1) // 2)
        pairs = _round_up(pairs_needed, _PAIR_COUNTS)
        return _Spec(
            conductors=pairs * 2,
            pairs=pairs,
            shield=True,
            drain=True,
            signal_slots=pairs * 2,
            cable_type_body=f"{pairs}PR",
            phrase="instrument",
        )
    if kind == "motor" and vfd:
        phases = max(3, landed + extra)
        return _Spec(
            conductors=phases + 1,
            ground=True,
            shield=True,
            drain=True,
            signal_slots=phases,
            cable_type_body=f"{phases}C+G",
            phrase="vfd",
        )
    if kind == "motor":
        phases = max(1, landed + extra)
        return _Spec(
            conductors=phases + 1,
            ground=True,
            signal_slots=phases,
            cable_type_body=f"{phases}C+G",
            phrase="tcer",
        )
    count = _round_up(max(2, landed + extra), _CONTROL_COUNTS)
    return _Spec(
        conductors=count,
        signal_slots=count,
        cable_type_body=f"{count}C",
        phrase="tcer",
    )


def _describe(spec: _Spec, gauge: str) -> str:
    body = f"{spec.cable_type_body} {gauge}".strip() if gauge else spec.cable_type_body
    if spec.phrase == "vfd":
        return f"{body} VFD shielded"
    if spec.phrase == "instrument":
        return f"{body} shielded instrument"
    return f"{body} TC-ER"


def _extra_conductors(kind: str, spares: int | None) -> int:
    if spares is not None:
        return int(spares)
    if kind == "instrument":
        return 2
    if kind == "motor":
        return 0
    return 1


def _pick_gauge(project: Project, catalog: Any, legs: list[_Leg], kind: str, standard: str) -> str:
    labels = []
    for leg in legs:
        raw = str(leg.wire.gauge or "").strip()
        if raw:
            labels.append(gauge_label(raw, standard))
    if not labels:
        labels.extend(_gauges_on_device(project, catalog, legs, standard))
    if labels:
        return _largest_gauge(labels)
    if not legs:
        return ""
    device_ref = legs[0].field_ref
    comp, device = _lookup_comp(project, catalog, device_ref)
    if device is None or comp is None:
        return ""
    props: dict[str, Any] = {}
    props.update(device.properties or {})
    props.update(comp.properties or {})
    for key in ("wire_size", "gauge", "awg", "conductor"):
        raw = str(props.get(key) or "").strip()
        if not raw:
            continue
        label = gauge_label(raw, standard)
        if _parse_awg(raw, explicit=True) or _parse_mm2(raw) or label.startswith("#") or "mm" in label:
            return label
    if kind != "motor":
        return ""
    fla = _fla(props)
    if fla is None:
        return ""
    required = fla * 1.25
    if standard == "IEC":
        return _mm2_for_amps(required, minimum=2.5)
    return _awg_for_amps(required, minimum=14)


def _gauges_on_device(project: Project, catalog: Any, legs: list[_Leg], standard: str) -> list[str]:
    """Gauge on the internal side of the same terminal, when the field wire is blank."""

    found: list[str] = []
    for leg in legs:
        other = "A" if leg.terminal_pin.upper() == "B" else "B" if leg.terminal_pin.upper() == "A" else ""
        if not other:
            continue
        sheet = project.sheet(leg.sheet_id)
        token = f"{leg.terminal_ref}.{other}".upper()
        for wire in sheet.wires:
            if token not in {str(wire.frm).upper(), str(wire.to).upper()}:
                continue
            raw = str(wire.gauge or "").strip()
            if raw:
                found.append(gauge_label(raw, standard))
    return found


def _largest_gauge(labels: list[str]) -> str:
    from panelcad.core.checks import gauge_ampacity

    best = ""
    best_amp = -1.0
    for label in labels:
        if not label:
            continue
        amp = gauge_ampacity(label)
        if amp is None:
            amp = gauge_ampacity(label.replace(" mm²", "mm2"))
        score = float(amp or 0.0)
        if not best or score > best_amp:
            best = label
            best_amp = score
    return best


def _awg_for_amps(amps: float, *, minimum: int) -> str:
    table = _awg_table()
    chosen = f"#{minimum}"
    for size in _AWG_ORDER:
        if size > minimum:
            continue
        chosen = f"#{size}"
        if table[size] >= amps:
            return chosen
    return chosen


def _mm2_for_amps(amps: float, *, minimum: float) -> str:
    table = _mm2_table()
    chosen = f"{minimum:g} mm²"
    for size in _MM2_ORDER:
        if size + 1e-9 < minimum:
            continue
        chosen = f"{size:g} mm²"
        if table[size] >= amps:
            return chosen
    return chosen


def _fla(props: dict[str, Any]) -> float | None:
    from panelcad.core.checks import parse_amps

    for key in ("FLA", "fla", "Ie", "Iout"):
        amps = parse_amps(props.get(key))
        if amps:
            return float(amps)
    return None


def _collect_legs(project: Project, catalog: Any) -> list[_Leg]:
    from panelcad.core.electrical import terminal_identity
    from panelcad.render.sheet import parse_pin_ref

    index = _index(project, catalog)
    legs: list[_Leg] = []
    seen: set[tuple[str, str]] = set()
    for sheet_index, sheet in enumerate(project.sheets):
        if sheet.kind not in {"schematic", "io"}:
            continue
        for wire in sheet.wires:
            try:
                a_ref, a_pin = parse_pin_ref(str(wire.frm))
                b_ref, b_pin = parse_pin_ref(str(wire.to))
            except ValueError:
                continue
            a = index.get(a_ref)
            b = index.get(b_ref)
            if a is None or b is None:
                continue
            a_term = terminal_identity(a[1], a[2])
            b_term = terminal_identity(b[1], b[2])
            if a_term and not b_term:
                kind = field_kind(b[2], b[1])
                if not kind:
                    continue
                term_ref, term_pin, term = a_ref, a_pin, a
                field_ref, field_pin = b_ref, b_pin
                strip, level = a_term
            elif b_term and not a_term:
                kind = field_kind(a[2], a[1])
                if not kind:
                    continue
                term_ref, term_pin, term = b_ref, b_pin, b
                field_ref, field_pin = a_ref, a_pin
                strip, level = b_term
            else:
                drive = _as_drive_leg(
                    sheet.id,
                    sheet_index,
                    wire,
                    a_ref,
                    a_pin,
                    a,
                    b_ref,
                    b_pin,
                    b,
                )
                if drive is None:
                    continue
                key = (sheet.id, wire.id)
                if key not in seen:
                    seen.add(key)
                    legs.append(drive)
                continue
            key = (sheet.id, wire.id)
            if key in seen:
                continue
            seen.add(key)
            _sheet, comp, device = term
            ground = _is_ground_terminal(comp, device, level) or field_pin.upper() in _GROUND_PINS
            legs.append(
                _Leg(
                    sheet_id=sheet.id,
                    sheet_index=sheet_index,
                    wire=wire,
                    terminal_ref=term_ref,
                    terminal_pin=term_pin,
                    strip=strip,
                    level=level,
                    y=float(comp.y),
                    x=float(comp.x),
                    field_ref=field_ref,
                    field_pin=field_pin,
                    kind=kind,
                    ground=ground,
                )
            )
    return legs


def _group_is_vfd(project: Project, catalog: Any, legs: list[_Leg]) -> bool:
    index = _index(project, catalog)
    for leg in legs:
        if _upstream_is_vfd(project, index, leg):
            return True
    return False


def _upstream_is_vfd(project: Project, index: dict[str, Any], leg: _Leg) -> bool:
    from panelcad.core.electrical import terminal_identity
    from panelcad.render.sheet import parse_pin_ref

    other = "A" if leg.terminal_pin.upper() == "B" else "B" if leg.terminal_pin.upper() == "A" else ""
    if not other:
        return False
    seen: set[str] = set()
    frontier: list[tuple[str, str]] = [(leg.sheet_id, f"{leg.terminal_ref}.{other}")]
    for _hop in range(4):
        nxt: list[tuple[str, str]] = []
        for sheet_id, token in frontier:
            sheet = project.sheet(sheet_id)
            bare = token.endswith(".*")
            prefix = token[:-2].upper() if bare else ""
            want = token.upper()
            for wire in sheet.wires:
                if wire.id in seen:
                    continue
                ends = (str(wire.frm), str(wire.to))
                upper = [end.upper() for end in ends]
                if bare:
                    if not any(end.split(".", 1)[0] == prefix for end in upper):
                        continue
                elif want not in upper:
                    continue
                seen.add(wire.id)
                for end, end_u in zip(ends, upper):
                    if bare and end_u.split(".", 1)[0] == prefix:
                        continue
                    if not bare and end_u == want:
                        continue
                    try:
                        ref, pin = parse_pin_ref(end)
                    except ValueError:
                        continue
                    found = index.get(ref)
                    if found is None:
                        continue
                    _sheet, comp, device = found
                    if _device_is_vfd(device):
                        return True
                    if field_kind(device, comp):
                        continue
                    ident = terminal_identity(comp, device)
                    if ident:
                        hop = "A" if pin.upper() == "B" else "B" if pin.upper() == "A" else ""
                        if hop:
                            nxt.append((sheet_id, f"{ref}.{hop}"))
                        continue
                    nxt.append((sheet_id, f"{ref}.*"))
        frontier = nxt
        if not frontier:
            break
    return False


def _claim_ground(project: Project, catalog: Any, strips: set[str], used: set[str]) -> str:
    from panelcad.core.electrical import terminal_identity

    found: list[tuple[int, float, float, str]] = []
    for sheet_index, sheet in enumerate(project.sheets):
        if sheet.kind in {"panel_layout", "door_layout", "terminal"}:
            continue
        for comp in sheet.components:
            device = _device(catalog, comp)
            ident = terminal_identity(comp, device)
            if ident is None or ident[0] not in strips or comp.id in used:
                continue
            if not _is_ground_terminal(comp, device, ident[1]):
                continue
            found.append((sheet_index, float(comp.y), float(comp.x), comp.id))
    if not found:
        return ""
    found.sort()
    return found[0][3]


def _is_ground_terminal(comp: Any, device: Any, level: str) -> bool:
    if str(level or "").upper() in _GROUND_PINS:
        return True
    lid = str(getattr(device, "library_id", "") or "").lower()
    tags = {str(tag).lower() for tag in (getattr(device, "tags", None) or [])}
    if tags & {"pe", "earth", "ground"}:
        return True
    if lid.endswith(".pe") or ".pe_" in lid or "pe_" in lid.split(".")[-1]:
        return True
    props: dict[str, Any] = {}
    if device is not None:
        props.update(device.properties or {})
    props.update(getattr(comp, "properties", None) or {})
    flag = str(props.get("pe") or props.get("ground") or "").strip().lower()
    return flag in {"1", "true", "yes", "pe"}


def _device_is_vfd(device: Any) -> bool:
    if device is None:
        return False
    cat = str(device.category or "").lower()
    lid = str(device.library_id or "").lower()
    return cat.startswith("drive.vfd") or ".vfd" in lid


def _index(project: Project, catalog: Any) -> dict[str, tuple[Any, Any, Any]]:
    found: dict[str, tuple[Any, Any, Any]] = {}
    for sheet in project.sheets:
        if sheet.kind in {"panel_layout", "door_layout"}:
            continue
        for comp in sheet.components:
            found.setdefault(comp.id, (sheet, comp, _device(catalog, comp)))
    return found


def _lookup_comp(project: Project, catalog: Any, ref: str) -> tuple[Any, Any]:
    item = _index(project, catalog).get(ref)
    if item is None:
        return None, None
    return item[1], item[2]


def _device(catalog: Any, comp: Any) -> Any:
    try:
        return catalog.get(comp.library_id)
    except KeyError:
        return None


def _group_key(legs: list[_Leg]) -> tuple:
    leg = min(legs, key=lambda item: (item.sheet_index, item.y, item.x, item.terminal_ref))
    return (leg.sheet_index, leg.y, leg.x, leg.field_ref)


def _only_tray(project: Project) -> str:
    """Use the route when the job has one field tray.

    Panel wire ducts (``wt``, ``wt-top``, ``wt1``) are shop furniture from the
    layout. They are not a field-cable route, so a panel that also has ``TRAY-A``
    still fills that route.
    """

    ids: list[str] = []
    for sheet in project.sheets:
        for tray in sheet.trays:
            if not tray.id or _panel_duct_id(tray.id) or tray.id in ids:
                continue
            ids.append(tray.id)
    if len(ids) == 1:
        return ids[0]
    return ""


def _panel_duct_id(tray_id: str) -> bool:
    text = str(tray_id).strip().lower()
    if text == "wt" or text.startswith("wt-"):
        return True
    return text.startswith("wt") and text[2:].isdigit()


def _standard(project: Project) -> str:
    from panelcad.core.refs import sheet_drawing_standard

    return sheet_drawing_standard(project, None)


def _tag_style(project: Project) -> tuple[str, str]:
    override = str(project.ref_prefixes.get("cable") or "").strip()
    iec = _standard(project) == "IEC"
    if override:
        if override.startswith("-"):
            return "seq", override
        if iec:
            return "seq", override
        return "sheet", override
    if iec:
        return "seq", "-W"
    return "sheet", "W"


def _apply_fields(cable: Cable, fields: dict[str, Any]) -> None:
    for key, value in fields.items():
        if key in {"op", "id", "tag"}:
            continue
        if key == "assignments":
            cable.assignments = [CableConductor.model_validate(item) for item in (value or [])]
            continue
        if key == "length":
            cable.length = _optional_float(value)
            continue
        if key == "slack_pct":
            cable.slack_pct = _optional_float(value)
            if cable.slack_pct is not None and cable.slack_pct < 0:
                raise ValueError("slack percent cannot be negative")
            continue
        if key == "conductors":
            cable.conductors = int(value or 0)
            if cable.conductors < 0:
                raise ValueError("conductor count cannot be negative")
            continue
        if key == "pairs":
            cable.pairs = int(value or 0)
            continue
        if key == "shield_bond":
            bond = str(value or "").strip().lower()
            if bond not in {"", "both", "panel", "field", "none"}:
                raise ValueError("shield_bond must be both, panel, field, or none")
            cable.shield_bond = bond  # type: ignore[assignment]
            continue
        if key in {"shield", "drain", "show_tag"}:
            setattr(cable, key, _as_bool(value))
            continue
        if key == "length_unit":
            unit = str(value or "ft").strip().lower()
            if unit in {"ft", "feet", "foot"}:
                cable.length_unit = "ft"
            elif unit in {"m", "meter", "metre", "meters", "metres"}:
                cable.length_unit = "m"
            else:
                raise ValueError("length unit must be ft or m")
            continue
        if key == "from_terminals":
            if isinstance(value, str):
                cable.from_terminals = [part.strip() for part in value.split(",") if part.strip()]
            else:
                cable.from_terminals = [str(part).strip() for part in (value or []) if str(part).strip()]
            continue
        if key == "to_terminals":
            if isinstance(value, str):
                cable.to_terminals = [part.strip() for part in value.split(",") if part.strip()]
            else:
                cable.to_terminals = [str(part).strip() for part in (value or []) if str(part).strip()]
            continue
        if key == "kind":
            kind = str(value or "field").strip().lower().replace("-", "").replace("_", "")
            if kind in {"", "field"}:
                cable.kind = "field"
            elif kind == "interpanel":
                cable.kind = "interpanel"
            else:
                raise ValueError("cable kind must be field or interpanel")
            continue
        if not hasattr(cable, key):
            raise ValueError(f"unknown cable field '{key}'")
        if value is None:
            setattr(cable, key, "")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            setattr(cable, key, value)
        else:
            setattr(cable, key, str(value).strip())
    if cable.length is not None and cable.length < 0:
        raise ValueError("length cannot be negative")


def _need(project: Project, tag: str) -> Cable:
    key = str(tag).strip()
    for cable in project.cables:
        if cable.tag == key:
            return cable
    raise KeyError(f"no cable '{key}'")


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no"}
    return bool(value)


def _round_up(count: int, choices: tuple[int, ...]) -> int:
    for size in choices:
        if size >= count:
            return size
    return count


def _num(value: float | None) -> str:
    if value is None:
        return ""
    rounded = round(float(value), 2)
    if abs(rounded - int(rounded)) < 1e-9:
        return str(int(round(rounded)))
    text = f"{rounded:.2f}".rstrip("0").rstrip(".")
    return text


def _norm_desc(text: str) -> str:
    return " ".join(str(text or "").replace("²", "2").replace("mm2", "mm2").split()).casefold()


def _awg_table() -> dict[int, float]:
    from panelcad.core.checks import _AWG_AMPACITY

    return _AWG_AMPACITY


def _mm2_table() -> dict[float, float]:
    from panelcad.core.checks import _MM2_AMPACITY

    return _MM2_AMPACITY


def _parse_awg(text: str, *, explicit: bool = False) -> int | None:
    raw = str(text or "").strip()
    match = re.fullmatch(r"(?:#\s*|AWG\s*)?(\d{1,2})(?:\s*AWG)?", raw, flags=re.I)
    if not match:
        return None
    size = int(match.group(1))
    marked = explicit or raw.startswith("#") or "awg" in raw.lower()
    if size in _awg_table():
        return size
    if marked and 0 <= size <= 40:
        return size
    return None


def _parse_mm2(text: str) -> float | None:
    raw = str(text or "").strip().replace("²", "2").replace("^2", "2")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:MM2)?", raw, flags=re.I)
    if not match:
        return None
    if "mm" not in raw.lower() and not re.fullmatch(r"\d+\.\d+", raw.strip()):
        return None
    return float(match.group(1))


def _require_url(source: str) -> None:
    if not _URL_RE.match(str(source or "").strip()):
        raise ValueError("part source must be an http(s) URL")
