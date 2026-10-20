"""Troubleshooting engine: circuit paths, checks, causes, and the open AI export.

Read-only. Nothing here writes the ``.pcad.yaml`` project. Readings live in a
sidecar next to the project. Expected voltages come from the drawing (a net
name, a rail name, or a supply output property on that net). When the drawing
does not state a value, the result is ``needs-info``.

Wire numbers and terminal ids in every answer are taken from the drawing.
Safety devices, overloads, and PLC points are never given a jumper, bypass,
or force instruction. Each flow starts with lockout and arc-flash text, and
each check is ``LIVE`` or ``DE-ENERGIZED``.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from panelcad.core.checks import _hint_voltage, parse_volts
from panelcad.core.electrical import (
    SCHEMATIC_KINDS,
    _io_type,
    contact_state,
    element_kind,
    family_key,
    io_rows,
    symbol_role,
    terminal_identity,
)
from panelcad.core.ladder import rail_names
from panelcad.core.models import Component, Device, Project, Sheet, Wire
from panelcad.core.xref import component_location, is_offpage_component, xref_families
from panelcad.core.xref_index import QueryError
from panelcad.library.catalog import Catalog
from panelcad.version import __version__

FORMAT = "panelcad-troubleshoot"
SCHEMA = "1.0"
LOG_FORMAT = "panelcad-troubleshoot-log"
LOG_SCHEMA = "1.0"
LOG_SUFFIX = ".troubleshoot-log.json"

LIVE = "LIVE"
DEENERGIZED = "DE-ENERGIZED"

LOTO_TEXT = (
    "Qualified persons only. Follow site lockout/tagout before any work, "
    "and prove zero energy before a DE-ENERGIZED check."
)
ARC_FLASH_TEXT = (
    "LIVE checks are energized work. Follow NFPA 70E and the site arc-flash PPE requirements."
)
DRAWING_TEXT = (
    "Verify readings against the panel. Values below come from the drawing and may be out of date."
)
SAFETY_OPEN_TEXT = "Find why the safety circuit is open; reset per the machine's procedure."
OVERLOAD_OPEN_TEXT = "Find why the overload opened; reset per the machine's procedure."
PLC_LEAVE_TEXT = "Confirm the program and leave the point in its normal state."

_BANNED = re.compile(
    r"\b(?:jumper\w*|bypass\w*|force\w*|defeat\w*|override\w*)\b",
    re.IGNORECASE,
)
_WIRE_CITE = re.compile(r"\bwire\s+([A-Za-z0-9._+-]+)", re.IGNORECASE)
_TERMINAL_ONE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_-]*)\s+terminal\s+([A-Za-z0-9_]+)",
    re.IGNORECASE,
)
_TERMINAL_PAIR = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_-]*)\s+terminals\s+([A-Za-z0-9_]+)\s+and\s+([A-Za-z0-9_]+)",
    re.IGNORECASE,
)

_PROSE_KEYS = {
    "text",
    "instruction",
    "meter_leads",
    "detail",
    "title",
    "loto",
    "arc_flash",
    "drawing_disclaimer",
    "when",
    "direction",
    "summary",
    "message",
}

SYMPTOMS: dict[str, dict[str, str]] = {
    "motor_wont_start": {"id": "motor_wont_start", "title": "Motor won't start"},
    "motor_trips": {"id": "motor_trips", "title": "Motor trips"},
    "wont_stop": {"id": "wont_stop", "title": "Won't stop"},
    "no_control_power": {"id": "no_control_power", "title": "No control power"},
    "plc_input_not_seen": {"id": "plc_input_not_seen", "title": "PLC input not seen"},
    "plc_output_on_load_off": {"id": "plc_output_on_load_off", "title": "PLC output on, load off"},
    "vfd_faulted": {"id": "vfd_faulted", "title": "VFD faulted"},
}

_SYMPTOM_ALIASES = {
    "motor won't start": "motor_wont_start",
    "motor wont start": "motor_wont_start",
    "wont start": "motor_wont_start",
    "won't start": "motor_wont_start",
    "not running": "motor_wont_start",
    "motor trips": "motor_trips",
    "trips": "motor_trips",
    "won't stop": "wont_stop",
    "wont stop": "wont_stop",
    "will not stop": "wont_stop",
    "no control power": "no_control_power",
    "plc input not seen": "plc_input_not_seen",
    "input not seen": "plc_input_not_seen",
    "plc output on, load off": "plc_output_on_load_off",
    "plc output on load off": "plc_output_on_load_off",
    "output on load off": "plc_output_on_load_off",
    "vfd faulted": "vfd_faulted",
    "vfd fault": "vfd_faulted",
    "drive faulted": "vfd_faulted",
}

_WEIGHT = {
    "estop": 90,
    "safety": 88,
    "ol_nc": 86,
    "heater": 84,
    "fuse": 80,
    "breaker": 78,
    "supply": 76,
    "plc_output": 74,
    "plc_input": 72,
    "vfd": 70,
    "stop": 60,
    "coil": 55,
    "seal_in": 50,
    "terminal": 48,
    "wire": 45,
    "contactor_pole": 44,
    "start": 40,
    "interlock": 38,
    "aux_no": 36,
    "motor": 30,
    "lamp": 22,
    "rail": 20,
    "device": 15,
    "pole": 15,
}

_BOOST: dict[str, dict[str, float]] = {
    "motor_wont_start": {
        "estop": 1.4,
        "safety": 1.4,
        "ol_nc": 1.3,
        "heater": 1.2,
        "stop": 1.2,
        "coil": 1.2,
        "plc_output": 1.3,
        "supply": 1.1,
        "seal_in": 1.1,
        "breaker": 1.1,
        "contactor_pole": 1.1,
    },
    "motor_trips": {"heater": 1.8, "ol_nc": 1.6, "breaker": 1.5, "motor": 1.4, "vfd": 1.3},
    "wont_stop": {"seal_in": 1.8, "plc_output": 1.6, "stop": 1.3, "aux_no": 1.2},
    "no_control_power": {"supply": 1.8, "fuse": 1.7, "breaker": 1.3, "rail": 1.2},
    "plc_input_not_seen": {"plc_input": 1.8, "wire": 1.4, "terminal": 1.3, "estop": 1.2, "stop": 1.2},
    "plc_output_on_load_off": {"plc_output": 1.6, "coil": 1.4, "wire": 1.3, "lamp": 1.2, "contactor_pole": 1.2},
    "vfd_faulted": {"vfd": 1.8, "breaker": 1.2, "supply": 1.1},
}

_SWITCH_KINDS = {
    "estop",
    "safety",
    "stop",
    "start",
    "seal_in",
    "aux_no",
    "interlock",
    "ol_nc",
    "fuse",
    "breaker",
    "heater",
    "contactor_pole",
    "pole",
    "vfd",
    "coil",
    "lamp",
}

_CANDIDATE_PAIRS = (
    ("L1", "T1"),
    ("L2", "T2"),
    ("L3", "T3"),
    ("L1", "U"),
    ("L2", "V"),
    ("L3", "W"),
    ("1", "2"),
    ("3", "4"),
    ("5", "6"),
    ("11", "12"),
    ("21", "22"),
    ("31", "32"),
    ("41", "42"),
    ("13", "14"),
    ("23", "24"),
    ("33", "34"),
    ("43", "44"),
    ("11", "14"),
    ("21", "24"),
    ("31", "34"),
    ("41", "44"),
    ("95", "96"),
    ("97", "98"),
    ("15", "16"),
    ("17", "18"),
    ("A1", "A2"),
    ("X1", "X2"),
)

_POWER_PAIRS = {
    frozenset(pair)
    for pair in (
        ("L1", "T1"),
        ("L2", "T2"),
        ("L3", "T3"),
        ("L1", "U"),
        ("L2", "V"),
        ("L3", "W"),
    )
}
_OL_AUX = {frozenset(("95", "96")), frozenset(("97", "98"))}
_SUPPLY_OUT = {"plus", "l+", "lplus", "v+", "dc+"}
_LOAD_MATES = {"A1": "A2", "A2": "A1", "X1": "X2", "X2": "X1"}
_MOTOR_PINS = {"U", "V", "W", "U1", "V1", "W1", "T1", "T2", "T3"}

OPS: tuple[dict[str, str], ...] = (
    {"op": "circuit_for", "args": "ref", "read_only": "true"},
    {"op": "checkpoints", "args": "ref, symptom", "read_only": "true"},
    {"op": "upstream", "args": "endpoint (ref.pin)", "read_only": "true"},
    {"op": "downstream", "args": "endpoint (ref.pin)", "read_only": "true"},
    {"op": "plc_point", "args": "address or tag", "read_only": "true"},
    {"op": "likely_causes", "args": "ref, symptom, readings?", "read_only": "true"},
    {"op": "troubleshoot_guide", "args": "ref, symptom, readings?", "read_only": "true"},
)

TROUBLESHOOT_SCHEMAS: dict[str, dict[str, object]] = {
    "circuit_for": {
        "op": "circuit_for",
        "read_only": True,
        "args": {"ref": "Device tag on the drawing (M1, K1, CR1)."},
    },
    "checkpoints": {
        "op": "checkpoints",
        "read_only": True,
        "args": {
            "ref": "Device tag on the drawing.",
            "symptom": (
                "motor_wont_start, motor_trips, wont_stop, no_control_power, "
                "plc_input_not_seen, plc_output_on_load_off, or vfd_faulted."
            ),
        },
    },
    "upstream": {
        "op": "upstream",
        "read_only": True,
        "args": {"endpoint": "Drawing terminal as ref.pin (K1.A1)."},
    },
    "downstream": {
        "op": "downstream",
        "read_only": True,
        "args": {"endpoint": "Drawing terminal as ref.pin (K1.A1)."},
    },
    "plc_point": {
        "op": "plc_point",
        "read_only": True,
        "args": {"address": "PLC address, point label, or tag that is on the drawing."},
    },
    "likely_causes": {
        "op": "likely_causes",
        "read_only": True,
        "args": {
            "ref": "Device tag on the drawing.",
            "symptom": "Same symptom ids as checkpoints.",
            "readings": "Optional list of {id|device+terminal|wire, result: ok|wrong|skipped, measured?}.",
        },
    },
    "troubleshoot_guide": {
        "op": "troubleshoot_guide",
        "read_only": True,
        "args": {
            "ref": "Device tag on the drawing.",
            "symptom": "Same symptom ids as checkpoints.",
            "readings": "Optional list of {id|device+terminal|wire, result: ok|wrong|skipped, measured?}.",
        },
    },
}


def safety_block() -> dict[str, Any]:
    """Lockout and arc-flash text that opens every troubleshooting flow."""

    return {
        "qualified_persons_only": True,
        "loto": LOTO_TEXT,
        "arc_flash": ARC_FLASH_TEXT,
        "drawing_disclaimer": DRAWING_TEXT,
        "prohibited": [
            "Leave E-stops, safety relays, guards, interlocks, and overloads in the circuit.",
            "Leave PLC points in their normal state.",
            SAFETY_OPEN_TEXT,
            OVERLOAD_OPEN_TEXT,
        ],
    }


def prose_violations(payload: Any) -> list[str]:
    """Banned instruction words in user-facing text. Empty means the payload is clean."""

    found: list[str] = []
    for text in _prose(payload):
        for match in _BANNED.finditer(text):
            found.append(match.group(0))
    return found


def normalize_symptom(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise QueryError(_symptom_error(""))
    key = raw.casefold().replace("_", " ")
    if raw.casefold() in SYMPTOMS:
        return raw.casefold()
    compact = key.replace("-", " ")
    if compact in _SYMPTOM_ALIASES:
        return _SYMPTOM_ALIASES[compact]
    ident = re.sub(r"[^a-z0-9]+", "_", raw.casefold()).strip("_")
    if ident in SYMPTOMS:
        return ident
    raise QueryError(_symptom_error(raw))


def circuit_for(project: Project, catalog: Catalog, ref: str) -> dict[str, Any]:
    """Control and power path for *ref*, built from the drawing netlist."""

    graph = build_graph(project, catalog)
    return _circuit(graph, ref)


def checkpoints(project: Project, catalog: Catalog, ref: str, symptom: str) -> dict[str, Any]:
    """Ordered checks for *symptom*. Each check is LIVE or DE-ENERGIZED."""

    symptom_id = normalize_symptom(symptom)
    graph = build_graph(project, catalog)
    circuit = _circuit(graph, ref)
    checks = _checks_for(graph, circuit, symptom_id)
    return {
        "op": "checkpoints",
        "read_only": True,
        "safety": safety_block(),
        "ref": circuit["ref"],
        "symptom": symptom_id,
        "symptom_title": SYMPTOMS[symptom_id]["title"],
        "lockout": LOTO_TEXT,
        "phases": [
            {
                "id": "de-energized",
                "energized": DEENERGIZED,
                "when": "Lock out and tag out. Prove zero energy before these continuity checks.",
            },
            {
                "id": "live",
                "energized": LIVE,
                "when": "Energized work. Qualified persons only. Follow site lockout rules and NFPA 70E arc-flash PPE.",
            },
        ],
        "checks": checks,
        "needs_info": list(circuit["needs_info"]),
        "count": len(checks),
    }


def upstream(project: Project, catalog: Catalog, endpoint: str) -> dict[str, Any]:
    """Devices and wires from *endpoint* toward the supply."""

    return _walk(project, catalog, endpoint, toward="upstream")


def downstream(project: Project, catalog: Catalog, endpoint: str) -> dict[str, Any]:
    """Devices and wires from *endpoint* toward the load or return."""

    return _walk(project, catalog, endpoint, toward="downstream")


def plc_point(project: Project, catalog: Catalog, address: str) -> dict[str, Any]:
    """One PLC I/O point by address, point label, or tag. Drawing rows only."""

    query = str(address or "").strip()
    if not query:
        raise QueryError("PLC point is required")
    rows = _plc_rows(project, catalog)
    hits = [
        row
        for row in rows
        if query.casefold() in {row["address"].casefold(), row["tag"].casefold(), row["point"].casefold()}
    ]
    if not hits:
        raise QueryError(f"PLC point {query} is not on the drawing")
    row = hits[0]
    return {
        "op": "plc_point",
        "read_only": True,
        "safety": safety_block(),
        "query": query,
        "point": row,
        "needs_info": [] if row["wire"] or row["net"] else ["No conductor for this point is on the drawing."],
    }


def likely_causes(
    project: Project,
    catalog: Catalog,
    ref: str,
    symptom: str,
    readings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ranked causes. A first wrong reading narrows the fault to that segment."""

    symptom_id = normalize_symptom(symptom)
    graph = build_graph(project, catalog)
    circuit = _circuit(graph, ref)
    checks = _checks_for(graph, circuit, symptom_id)
    parsed = _parse_readings(checks, readings)
    causes, window = _rank_causes(graph, circuit, checks, symptom_id, parsed)
    last_ok, first_wrong = _fault_marks(checks, parsed)
    return {
        "op": "likely_causes",
        "read_only": True,
        "safety": safety_block(),
        "ref": circuit["ref"],
        "symptom": symptom_id,
        "symptom_title": SYMPTOMS[symptom_id]["title"],
        "fault_between": {"last_ok": last_ok, "first_wrong": first_wrong},
        "causes": causes,
        "needs_info": list(circuit["needs_info"]),
        "count": len(causes),
        "window": window,
    }


def troubleshoot_document(project: Project, catalog: Catalog) -> dict[str, Any]:
    """Open ``troubleshoot.json`` document. Any AI can read it without PanelCAD."""

    graph = build_graph(project, catalog)
    devices = [_device_record(graph, placed) for placed in _schematic_devices(graph)]
    devices.sort(key=lambda row: (row["tag"].casefold(), row["sheet"]))
    nets = [_net_record(graph, net) for net in graph.nets.values()]
    nets.sort(key=lambda row: (row["wire_no"], row["name"], row["id"]))
    needs: list[dict[str, str]] = []
    for net in nets:
        if net["voltage"] is None:
            label = net["wire_no"] or net["name"] or net["id"]
            needs.append(
                {
                    "topic": "voltage",
                    "net": label,
                    "detail": f"No voltage is stored for net {label}.",
                }
            )
    needs.append(
        {
            "topic": "plc_program",
            "net": "",
            "detail": "No PLC program is included. Logic references are needs-info until a program file is linked.",
        }
    )
    safety_tags = sorted({row["tag"] for row in devices if row["safety"]})
    return {
        "format": FORMAT,
        "schema": SCHEMA,
        "generator": f"PanelCAD {__version__}",
        "project": project.name,
        "drawing_number": str(project.title_block.drawing_number or ""),
        "read_only": True,
        "safety": safety_block(),
        "rules": {
            "expected_values": (
                "Voltages come only from a net name, a rail name, or a supply output property "
                "on that net. When none of those is present the value is needs-info."
            ),
            "citations": "Cite only wire numbers and terminal ids present in this file.",
            "safety": (
                "Leave E-stops, safety relays, guards, interlocks, overloads, and PLC points "
                "in the circuit. " + SAFETY_OPEN_TEXT
            ),
            "drawing": DRAWING_TEXT,
            "log": (
                "Troubleshooting readings are a separate file next to the project "
                f"(*{LOG_SUFFIX}), not a field in the project file."
            ),
        },
        "symptoms": list(SYMPTOMS.values()),
        "ops": [dict(row) for row in OPS],
        "devices": devices,
        "nets": nets,
        "cross_refs": _cross_refs(project, catalog),
        "plc_io": _plc_rows(project, catalog),
        "safety_devices": safety_tags,
        "plc_code_refs": [],
        "needs_info": needs,
    }


def write_troubleshoot(project: Project, catalog: Catalog, path: Path | str) -> Path:
    """Write the open troubleshoot document. Does not modify the project file."""

    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    document = troubleshoot_document(project, catalog)
    dest.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return dest


def log_path_for(project_path: Path | str) -> Path:
    """Sidecar path next to the project. Not the project file itself.

    ``pump.pcad.yaml`` writes ``pump.troubleshoot-log.json``.
    """

    src = Path(project_path)
    name = src.name
    for suffix in (".pcad.yaml", ".pcad.yml", ".yaml", ".yml"):
        if name.endswith(suffix):
            stem = name[: -len(suffix)]
            break
    else:
        stem = src.stem
    return src.with_name(stem + LOG_SUFFIX)


def append_troubleshoot_log(
    project: Project,
    catalog: Catalog,
    project_path: Path | str,
    *,
    ref: str,
    symptom: str,
    readings: list[dict[str, Any]] | None = None,
    who: str = "",
    note: str = "",
    when: str = "",
) -> dict[str, Any]:
    """Append one readings entry beside the project. The project file is not opened for write."""

    src = Path(project_path)
    if not str(ref or "").strip():
        raise QueryError("device is required")
    symptom_id = normalize_symptom(symptom)
    checks = checkpoints(project, catalog, ref, symptom_id)["checks"]
    parsed = _parse_readings(checks, readings)
    dest = log_path_for(src)
    if dest.resolve() == src.resolve():
        raise QueryError("troubleshooting log cannot replace the project file")
    document = _read_log(dest, src.name)
    entry = {
        "id": uuid.uuid4().hex,
        "when": when.strip() or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "who": str(who or "").strip(),
        "ref": str(ref).strip(),
        "symptom": symptom_id,
        "readings": parsed,
        "note": str(note or "").strip(),
    }
    document["entries"].append(entry)
    dest.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return {"path": str(dest), "format": LOG_FORMAT, "schema": LOG_SCHEMA, "entry": entry}


# --- graph -----------------------------------------------------------------


@dataclass
class Endpoint:
    sheet_id: str
    ref: str
    pin: str
    kind: str  # pin | rail

    def token(self) -> str:
        if self.kind == "rail":
            return self.ref
        return f"{self.ref}.{self.pin}"


@dataclass
class Placed:
    sheet: Sheet
    comp: Component
    device: Device | None


@dataclass
class Net:
    key: str
    wires: list[tuple[str, Wire]] = field(default_factory=list)
    endpoints: list[Endpoint] = field(default_factory=list)
    voltage: dict[str, Any] | None = None
    voltage_note: str = ""


@dataclass
class Edge:
    net_a: str
    net_b: str
    sheet_id: str
    ref: str
    pin_a: str
    pin_b: str
    domain: str
    kind: str
    safety: bool

    def other(self, net: str) -> str:
        return self.net_b if net == self.net_a else self.net_a

    def touches(self, net: str) -> bool:
        return net == self.net_a or net == self.net_b

    def pin_on(self, net: str) -> str:
        return self.pin_a if net == self.net_a else self.pin_b


@dataclass
class Hop:
    edge: Edge
    src: str
    dst: str

    @property
    def pin_src(self) -> str:
        return self.edge.pin_on(self.src)

    @property
    def pin_dst(self) -> str:
        return self.edge.pin_on(self.dst)


@dataclass
class Graph:
    project: Project
    catalog: Catalog
    devices: dict[tuple[str, str], Placed]
    nets: dict[str, Net]
    edges: list[Edge]
    pin_net: dict[tuple[str, str, str], str]
    io: list[dict[str, str]]


class _UF:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def add(self, item: int) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: int) -> int:
        self.add(item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        root_l, root_r = self.find(left), self.find(right)
        if root_l != root_r:
            self.parent[root_r] = root_l


def build_graph(project: Project, catalog: Catalog) -> Graph:
    devices: dict[tuple[str, str], Placed] = {}
    sheets: dict[str, Sheet] = {}
    for sheet in project.sheets:
        if sheet.kind not in SCHEMATIC_KINDS:
            continue
        sheets[sheet.id] = sheet
        for comp in sheet.components:
            device = _device(catalog, comp)
            if device is not None and is_offpage_component(comp, device):
                devices[(sheet.id, comp.id.casefold())] = Placed(sheet, comp, device)
                continue
            devices[(sheet.id, comp.id.casefold())] = Placed(sheet, comp, device)

    wires: list[tuple[Sheet, Wire]] = []
    for sheet in project.sheets:
        if sheet.kind not in SCHEMATIC_KINDS:
            continue
        for wire in sheet.wires:
            wires.append((sheet, wire))

    uf = _UF()
    for index, _pair in enumerate(wires):
        uf.add(index)
    by_end: dict[str, list[int]] = defaultdict(list)
    for index, (sheet, wire) in enumerate(wires):
        for raw in (wire.frm, wire.to):
            ep = _endpoint(sheet, raw)
            if ep is None:
                continue
            by_end[_end_key(ep)].append(index)
    for group in by_end.values():
        for other in group[1:]:
            uf.union(group[0], other)
    _union_label(uf, wires, lambda wire: str(wire.number or "").strip().casefold())
    _union_label(
        uf,
        wires,
        lambda wire: str(wire.net or "").strip().casefold() if not str(wire.number or "").strip() else "",
    )
    _union_offpage(uf, wires, devices)

    groups: dict[int, list[int]] = defaultdict(list)
    for index, _pair in enumerate(wires):
        groups[uf.find(index)].append(index)

    nets: dict[str, Net] = {}
    pin_net: dict[tuple[str, str, str], str] = {}
    for root, members in groups.items():
        net = Net(key=f"n{root}")
        seen_ep: set[str] = set()
        for index in members:
            sheet, wire = wires[index]
            net.wires.append((sheet.id, wire))
            for raw in (wire.frm, wire.to):
                ep = _endpoint(sheet, raw)
                if ep is None:
                    continue
                token = _end_key(ep)
                if token not in seen_ep:
                    seen_ep.add(token)
                    net.endpoints.append(ep)
                if ep.kind == "pin":
                    pin_net[(ep.sheet_id, ep.ref.casefold(), ep.pin.casefold())] = net.key
        voltage, note = _net_voltage(project, devices, net)
        net.voltage = voltage
        net.voltage_note = note
        nets[net.key] = net

    graph = Graph(
        project=project,
        catalog=catalog,
        devices=devices,
        nets=nets,
        edges=[],
        pin_net=pin_net,
        io=[],
    )
    graph.edges = _edges(graph)
    graph.io = _plc_rows(project, catalog)
    return graph


def _union_label(uf: _UF, wires: list[tuple[Sheet, Wire]], label) -> None:
    groups: dict[str, list[int]] = defaultdict(list)
    for index, (_sheet, wire) in enumerate(wires):
        token = label(wire)
        if token:
            groups[token].append(index)
    for group in groups.values():
        for other in group[1:]:
            uf.union(group[0], other)


def _union_offpage(
    uf: _UF,
    wires: list[tuple[Sheet, Wire]],
    devices: dict[tuple[str, str], Placed],
) -> None:
    by_pin: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for index, (sheet, wire) in enumerate(wires):
        for raw in (wire.frm, wire.to):
            ep = _endpoint(sheet, raw)
            if ep is None or ep.kind != "pin":
                continue
            by_pin[(ep.sheet_id, ep.ref.casefold(), ep.pin.casefold())].append(index)
    for (sheet_id, ref_cf, _pin), indexes in list(by_pin.items()):
        placed = devices.get((sheet_id, ref_cf))
        if placed is None or placed.device is None or not is_offpage_component(placed.comp, placed.device):
            continue
        props = placed.comp.properties or {}
        dest_sheet = str(props.get("dest_sheet") or "").strip()
        dest_ref = str(props.get("dest_ref") or "").strip()
        dest_pin = str(props.get("dest_pin") or "").strip()
        if not (dest_sheet and dest_ref and dest_pin):
            continue
        others = by_pin.get((dest_sheet, dest_ref.casefold(), dest_pin.casefold()), [])
        if others and indexes:
            uf.union(indexes[0], others[0])


def _edges(graph: Graph) -> list[Edge]:
    found: list[Edge] = []
    seen: set[tuple[str, str, str, str]] = set()
    for placed in graph.devices.values():
        device = placed.device
        if device is None or is_offpage_component(placed.comp, device):
            continue
        for pin_a, pin_b in _pairs(device):
            domain = _domain(device, pin_a, pin_b)
            if domain == "load":
                continue
            net_a = graph.pin_net.get((placed.sheet.id, placed.comp.id.casefold(), pin_a.casefold()))
            net_b = graph.pin_net.get((placed.sheet.id, placed.comp.id.casefold(), pin_b.casefold()))
            if not net_a or not net_b or net_a == net_b:
                continue
            kind = _edge_kind(device, placed.comp, pin_a, pin_b, domain)
            key = (placed.comp.id.casefold(), pin_a.casefold(), pin_b.casefold(), domain)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                Edge(
                    net_a=net_a,
                    net_b=net_b,
                    sheet_id=placed.sheet.id,
                    ref=placed.comp.id,
                    pin_a=pin_a,
                    pin_b=pin_b,
                    domain=domain,
                    kind=kind,
                    safety=_is_safety_device(device, placed.comp),
                )
            )
    return found


def _pairs(device: Device) -> list[tuple[str, str]]:
    pins = {pin.id.upper(): pin.id for pin in device.pins}
    found: list[tuple[str, str]] = []
    used: set[frozenset[str]] = set()
    for left, right in _CANDIDATE_PAIRS:
        if left in pins and right in pins:
            pair = frozenset((pins[left], pins[right]))
            if pair in used:
                continue
            used.add(pair)
            found.append((pins[left], pins[right]))
    if not found and len(device.pins) == 2:
        found.append((device.pins[0].id, device.pins[1].id))
    return found


def _domain(device: Device, pin_a: str, pin_b: str) -> str:
    pair = frozenset((pin_a.upper(), pin_b.upper()))
    cat = device.category or ""
    role = symbol_role(device)
    if pair == frozenset(("A1", "A2")):
        return "load"
    if pair == frozenset(("X1", "X2")) and (_is_lamp(device) or cat.startswith("power.transformer")):
        return "load"
    if pair == frozenset(("X1", "X2")) and _is_lamp(device):
        return "load"
    if cat.startswith("drive.") or role == "drive":
        if pair in _POWER_PAIRS or pair <= {"L1", "L2", "L3", "U", "V", "W", "T1", "T2", "T3"}:
            if pair in _POWER_PAIRS or _is_phase_pair(pair):
                return "power"
    if role in {"breaker", "disconnect"} or cat.startswith("protection.breaker") or cat.startswith("protection.mpcb"):
        return "power"
    if cat.startswith("protection.overload") and pair not in _OL_AUX:
        return "power"
    if pair in _POWER_PAIRS and (cat.startswith("switching.") or role in {"parent", "coil"}):
        return "power"
    if _is_phase_pair(pair) and cat.startswith("drive."):
        return "power"
    return "control"


def _is_phase_pair(pair: frozenset[str]) -> bool:
    phases = (
        frozenset(("L1", "U")),
        frozenset(("L2", "V")),
        frozenset(("L3", "W")),
        frozenset(("L1", "T1")),
        frozenset(("L2", "T2")),
        frozenset(("L3", "T3")),
    )
    return pair in phases


def _edge_kind(device: Device, comp: Component, pin_a: str, pin_b: str, domain: str) -> str:
    if domain == "power":
        cat = device.category or ""
        role = symbol_role(device)
        if cat.startswith("drive.") or role == "drive":
            return "vfd"
        if cat.startswith("protection.overload"):
            return "heater"
        if role in {"breaker", "disconnect"} or "breaker" in cat or "disconnect" in cat:
            return "breaker"
        if "fuse" in cat or "fuse" in device.library_id.lower():
            return "fuse"
        if cat.startswith("switching.") or role in {"parent", "coil"}:
            return "contactor_pole"
        return "pole"
    return _control_kind(device, comp, [pin_a, pin_b])


def _control_kind(device: Device, comp: Component, pins: list[str]) -> str:
    lid = device.library_id.lower()
    cat = (device.category or "").lower()
    tags = {str(tag).lower() for tag in device.tags}
    element = element_kind(comp, device)
    state = contact_state(device)
    pinset = {pin.upper() for pin in pins}
    if "estop" in cat or "estop" in lid or "estop" in tags:
        return "estop"
    if element == "safety" or cat.startswith("switching.safety") or "safety" in tags:
        return "safety"
    if element == "ol_contact" or "ol_contact" in lid or pinset in ({"95", "96"}, {"97", "98"}):
        return "ol_nc"
    if "fuse" in cat or ".fuses." in lid:
        return "fuse"
    if symbol_role(device) in {"breaker", "disconnect"}:
        return "breaker"
    if cat.startswith("operator.pushbutton") or ".pb_" in lid:
        if state == "NC" or "pb_nc" in lid:
            return "stop"
        return "start"
    if symbol_role(device) == "contact" or element == "contact":
        if state == "NO":
            return "seal_in"
        return "interlock"
    if symbol_role(device) == "terminal":
        return "terminal"
    if cat.startswith("operator.limit") or cat.startswith("operator.selector"):
        return "interlock"
    if symbol_role(device) == "plc":
        kind = _io_type(pins[0], "") if pins else None
        if kind in {"DO", "AO"}:
            return "plc_output"
        if kind in {"DI", "AI"}:
            return "plc_input"
    return "device"


# --- circuit ---------------------------------------------------------------


def _circuit(graph: Graph, ref: str) -> dict[str, Any]:
    members = _resolve(graph, ref)
    needs: list[str] = []
    control_segments: list[dict[str, Any]] = []
    branches: list[dict[str, Any]] = []
    power_phases: list[dict[str, Any]] = []
    queried = members[0].comp.id

    plc_only = all(_is_plc(item) for item in members)
    if plc_only:
        control_segments = _plc_device_segments(graph, members)
    else:
        coils = _with_load_pins(members)
        motors = [item for item in members if _is_motor(item)]
        if motors and not coils:
            coils = _feeder_coils(graph, motors)
        lamps = [item for item in members if _is_lamp(item.device)]
        drives = [item for item in members if _is_drive(item)]
        targets = coils or lamps
        if targets:
            control_segments, branches, coil_needs = _control_from_targets(graph, targets)
            needs.extend(coil_needs)
        elif drives and not motors:
            control_segments = _wired_pin_segments(graph, drives)
        else:
            switches = [item for item in members if not _is_motor(item)]
            if switches:
                control_segments, branches = _control_through(graph, switches)
        if motors:
            power_phases = _power_phases(graph, motors)
        elif any(_is_drive(item) or _has_power_pins(item) for item in members):
            fed = [item for item in members if _is_drive(item) or _has_power_pins(item)]
            # A contactor's own power poles are part of the motor chain when a motor is downstream.
            downstream_motors = _motors_downstream(graph, fed)
            if downstream_motors:
                power_phases = _power_phases(graph, downstream_motors)
            elif drives:
                power_phases = _power_phases(graph, drives)

    if not control_segments and not power_phases:
        needs.append(f"No wired path for {queried} is on the drawing.")
    for item in _with_load_pins(members) or _feeder_coils(graph, [m for m in members if _is_motor(m)]):
        for pin in ("A1", "A2"):
            if _pin(item.device, pin) and (item.sheet.id, item.comp.id.casefold(), pin.casefold()) not in graph.pin_net:
                note = f"{item.comp.id} terminal {pin} has no wire on the drawing."
                if note not in needs:
                    needs.append(note)

    hot_name, hot_wire, ret_name, ret_wire = _ends(graph, control_segments)
    return {
        "op": "circuit_for",
        "read_only": True,
        "safety": safety_block(),
        "ref": queried,
        "resolved": _unique([item.comp.id for item in members]),
        "control": {
            "present": bool(control_segments),
            "hot": {"name": hot_name, "wire": hot_wire},
            "return": {"name": ret_name, "wire": ret_wire},
            "segments": control_segments,
            "branches": branches,
        },
        "power": {"present": bool(power_phases), "phases": power_phases},
        "needs_info": needs,
    }


def _control_from_targets(
    graph: Graph,
    targets: list[Placed],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    needs: list[str] = []
    segments: list[dict[str, Any]] = []
    branches: list[dict[str, Any]] = []
    sources = _source_nets(graph)
    returns = _return_nets(graph)
    adj = _adj(graph, "control")
    for target in targets:
        hot_pin, ret_pin = _orient_load(graph, target, sources)
        if hot_pin is None:
            needs.append(f"{target.comp.id} coil terminals have no wire on the drawing.")
            segments.append(_load_segment(graph, target, "", "", kind=_load_kind(target)))
            continue
        hot_net = graph.pin_net.get((target.sheet.id, target.comp.id.casefold(), hot_pin.casefold()))
        paths = _search(sources, {hot_net} if hot_net else set(), adj, allow_empty=True, must=None)
        spine = _pick_spine(paths)
        extra = _branch_hops(paths, spine)
        built, branch_rows = _assemble_control(graph, target, hot_pin, ret_pin, spine, extra, sources, returns, adj)
        segments.extend(built)
        branches.extend(branch_rows)
    return _dedupe_segments(segments), branches, needs


def _control_through(
    graph: Graph,
    switches: list[Placed],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sources = _source_nets(graph)
    adj = _adj(graph, "control")
    goals = _end_nets(graph)
    must = {item.comp.id.casefold() for item in switches}
    paths = _search(sources, goals, adj, allow_empty=False, must=must)
    spine = _pick_spine(paths)
    extra = _branch_hops(paths, spine)
    if not spine and not extra:
        return _wired_pin_segments(graph, switches), []
    segments, branches = _assemble_switch_path(graph, spine, extra, sources)
    return segments, branches


def _assemble_control(
    graph: Graph,
    target: Placed,
    hot_pin: str,
    ret_pin: str | None,
    spine: list[Hop],
    extra: list[Hop],
    sources: set[str],
    returns: set[str],
    adj: dict[str, list[Edge]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    start = spine[0].src if spine else graph.pin_net.get((target.sheet.id, target.comp.id.casefold(), hot_pin.casefold()), "")
    segments: list[dict[str, Any]] = []
    if start:
        segments.extend(_source_segments(graph, start))
    coil_family = family_key(target.comp, target.device) or target.comp.id
    for hop in spine:
        segments.append(_hop_segment(graph, hop, coil_family))
    branch_rows: list[dict[str, Any]] = []
    placed_branch: set[tuple[str, str, str]] = set()
    for hop in extra:
        row = _hop_segment(graph, hop, coil_family, branch=True)
        segments.append(row)
        branch_rows.append(row)
        placed_branch.add((hop.edge.ref.casefold(), hop.edge.pin_a.casefold(), hop.edge.pin_b.casefold()))
    segments.append(_load_segment(graph, target, hot_pin, ret_pin or "", kind=_load_kind(target)))
    if ret_pin:
        ret_net = graph.pin_net.get((target.sheet.id, target.comp.id.casefold(), ret_pin.casefold()))
        if ret_net:
            back = _search({ret_net}, returns, adj, allow_empty=True, must=None, limit=4)
            ret_spine = _pick_spine(back)
            for hop in ret_spine:
                segments.append(_hop_segment(graph, hop, coil_family))
    return segments, branch_rows


def _assemble_switch_path(
    graph: Graph,
    spine: list[Hop],
    extra: list[Hop],
    sources: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    start = spine[0].src if spine else ""
    segments: list[dict[str, Any]] = []
    if start:
        segments.extend(_source_segments(graph, start))
    for hop in spine:
        segments.append(_hop_segment(graph, hop, ""))
    branches: list[dict[str, Any]] = []
    for hop in extra:
        row = _hop_segment(graph, hop, "", branch=True)
        segments.append(row)
        branches.append(row)
    if spine:
        goal = spine[-1].dst
        segments.extend(_goal_pin_segments(graph, goal, ignore={spine[-1].edge.ref.casefold()}))
    for index, row in enumerate(segments, start=1):
        row["order"] = index
        row["id"] = f"seg-{index}-{row['ref']}-{row['kind']}"
    return segments, branches


def _source_segments(graph: Graph, net_key: str) -> list[dict[str, Any]]:
    net = graph.nets.get(net_key)
    if net is None:
        return []
    rows: list[dict[str, Any]] = []
    rails = [ep for ep in net.endpoints if ep.kind == "rail"]
    for ep in rails:
        sheet = _sheet(graph, ep.sheet_id)
        if sheet is None:
            continue
        hot, _ret = rail_names(sheet, graph.project)
        if ep.ref.casefold() != hot.casefold():
            continue
        rows.append(_rail_segment(graph, net, ep))
    for ep in net.endpoints:
        if ep.kind != "pin":
            continue
        placed = _placed(graph, ep.sheet_id, ep.ref)
        if placed is None or placed.device is None:
            continue
        if _is_supply_output(placed, ep.pin):
            rows.append(_device_segment(graph, placed, [ep.pin], "supply", net))
        elif _is_plc_output_pin(placed, ep.pin):
            rows.append(_plc_segment(graph, placed, ep.pin, "plc_output", net))
    return rows


def _goal_pin_segments(graph: Graph, net_key: str, ignore: set[str]) -> list[dict[str, Any]]:
    net = graph.nets.get(net_key)
    if net is None:
        return []
    rows: list[dict[str, Any]] = []
    for ep in net.endpoints:
        if ep.kind != "pin" or ep.ref.casefold() in ignore:
            continue
        placed = _placed(graph, ep.sheet_id, ep.ref)
        if placed is None or not _is_plc_input_pin(placed, ep.pin):
            continue
        rows.append(_plc_segment(graph, placed, ep.pin, "plc_input", net))
    return rows


def _load_segment(graph: Graph, placed: Placed, hot_pin: str, ret_pin: str, kind: str) -> dict[str, Any]:
    pins = [pin for pin in (hot_pin, ret_pin) if pin]
    net_key = ""
    if hot_pin:
        net_key = graph.pin_net.get((placed.sheet.id, placed.comp.id.casefold(), hot_pin.casefold()), "")
    net = graph.nets.get(net_key) if net_key else None
    row = _device_segment(graph, placed, pins, kind, net)
    row["safety_device"] = _is_safety_device(placed.device, placed.comp) or kind in {"estop", "safety"}
    return row


def _hop_segment(graph: Graph, hop: Hop, coil_family: str, branch: bool = False) -> dict[str, Any]:
    placed = _placed(graph, hop.edge.sheet_id, hop.edge.ref)
    kind = hop.edge.kind
    if kind == "seal_in" and coil_family and placed is not None and placed.device is not None:
        family = family_key(placed.comp, placed.device) or ""
        if family.casefold() != coil_family.casefold():
            kind = "aux_no"
    net = graph.nets.get(hop.dst)
    pins = [hop.pin_src, hop.pin_dst]
    if placed is None:
        row = {
            "id": "",
            "order": 0,
            "kind": kind,
            "ref": hop.edge.ref,
            "terminals": pins,
            "wire": _first_number(net) if net else "",
            "wires": _numbers(net) if net else [],
            "sheet": hop.edge.sheet_id,
            "page": "",
            "zone": "",
            "page_zone": "",
            "safety_device": hop.edge.safety,
            "branch": branch,
            "voltage": net.voltage if net else None,
        }
        return row
    row = _device_segment(graph, placed, pins, kind, net)
    row["branch"] = branch
    row["safety_device"] = hop.edge.safety or kind in {"estop", "safety"}
    return row


def _device_segment(
    graph: Graph,
    placed: Placed,
    pins: list[str],
    kind: str,
    net: Net | None,
) -> dict[str, Any]:
    loc = _location(graph, placed)
    clean = [pin for pin in pins if pin and _pin(placed.device, pin)]
    return {
        "id": "",
        "order": 0,
        "kind": kind,
        "ref": placed.comp.id,
        "terminals": clean,
        "wire": _first_number(net) if net else "",
        "wires": _numbers(net) if net else [],
        "sheet": placed.sheet.id,
        "page": loc["page"],
        "zone": loc["zone"],
        "page_zone": loc["page_zone"],
        "safety_device": _is_safety_device(placed.device, placed.comp) or kind in {"estop", "safety"},
        "overload": kind in {"ol_nc", "heater"},
        "branch": False,
        "voltage": net.voltage if net else None,
        "net": _net_name(net) if net else "",
    }


def _rail_segment(graph: Graph, net: Net, ep: Endpoint) -> dict[str, Any]:
    return {
        "id": "",
        "order": 0,
        "kind": "rail",
        "ref": ep.ref,
        "terminals": [],
        "wire": _first_number(net),
        "wires": _numbers(net),
        "sheet": ep.sheet_id,
        "page": "",
        "zone": "",
        "page_zone": "",
        "safety_device": False,
        "overload": False,
        "branch": False,
        "voltage": net.voltage,
        "net": _net_name(net),
    }


def _plc_segment(graph: Graph, placed: Placed, pin: str, kind: str, net: Net | None) -> dict[str, Any]:
    row = _device_segment(graph, placed, [pin], kind, net)
    info = _io_match(graph, placed.comp.id, pin)
    if info:
        row["address"] = info["address"]
        row["tag"] = info["tag"]
        row["direction"] = info["direction"]
    else:
        row["address"] = pin
        row["tag"] = ""
        row["direction"] = "DO" if kind == "plc_output" else "DI"
    return row


def _dedupe_segments(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for row in rows:
        key = (row["ref"].casefold(), row["kind"], tuple(row.get("terminals") or []))
        if key in seen:
            continue
        seen.add(key)
        kept.append(row)
    for index, row in enumerate(kept, start=1):
        row["order"] = index
        row["id"] = f"seg-{index}-{row['ref']}-{row['kind']}"
    return kept


def _power_phases(graph: Graph, motors: list[Placed]) -> list[dict[str, Any]]:
    phases: list[dict[str, Any]] = []
    for motor in motors:
        for pin in _motor_pins(motor):
            net_key = graph.pin_net.get((motor.sheet.id, motor.comp.id.casefold(), pin.casefold()))
            if not net_key:
                continue
            chain = _power_chain(graph, net_key, motor.comp.id)
            segments = _dedupe_segments(chain)
            if not segments:
                continue
            phases.append({"phase": pin, "motor": motor.comp.id, "segments": segments})
    return phases


def _power_chain(graph: Graph, start: str, motor_ref: str) -> list[dict[str, Any]]:
    seen = {start}
    current = start
    hops: list[Hop] = []
    while True:
        options = [
            edge
            for edge in graph.edges
            if edge.domain == "power" and edge.touches(current) and edge.other(current) not in seen
        ]
        if not options:
            break
        edge = options[0]
        nxt = edge.other(current)
        hops.append(Hop(edge, nxt, current))
        seen.add(nxt)
        current = nxt
    hops.reverse()
    # hops were recorded load→line, then reversed so src is the line side.
    rows: list[dict[str, Any]] = []
    if hops:
        line_net = hops[0].src
    else:
        line_net = start
    rows.extend(_power_sources(graph, line_net, ignore={h.edge.ref.casefold() for h in hops}))
    for hop in hops:
        # After reverse, Hop.src is toward the line and Hop.dst toward the motor.
        rows.append(_hop_segment(graph, hop, ""))
    motor = _find_ref(graph, motor_ref)
    if motor is not None:
        pin = ""
        net = graph.nets.get(start)
        if net is not None:
            for ep in net.endpoints:
                if ep.kind == "pin" and ep.ref.casefold() == motor_ref.casefold():
                    pin = ep.pin
                    break
        rows.append(_device_segment(graph, motor, [pin] if pin else _motor_pins(motor), "motor", net))
    return rows


def _power_sources(graph: Graph, net_key: str, ignore: set[str]) -> list[dict[str, Any]]:
    net = graph.nets.get(net_key)
    if net is None:
        return []
    rows: list[dict[str, Any]] = []
    for ep in net.endpoints:
        if ep.kind != "pin" or ep.ref.casefold() in ignore:
            continue
        placed = _placed(graph, ep.sheet_id, ep.ref)
        if placed is None or placed.device is None:
            continue
        role = symbol_role(placed.device)
        cat = placed.device.category or ""
        if role in {"breaker", "disconnect"} or "fuse" in cat or cat.startswith("protection.breaker"):
            mate = _mate_pin(placed.device, ep.pin)
            pins = ([mate] if mate else []) + [ep.pin]
            kind = "fuse" if "fuse" in cat else "breaker"
            rows.append(_device_segment(graph, placed, pins, kind, net))
    return rows


def _plc_device_segments(graph: Graph, members: list[Placed]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for placed in members:
        if placed.device is None:
            continue
        for pin in placed.device.pins:
            kind = _io_type(pin.id, pin.electrical)
            if kind is None:
                continue
            net_key = graph.pin_net.get((placed.sheet.id, placed.comp.id.casefold(), pin.id.casefold()))
            if not net_key:
                continue
            net = graph.nets.get(net_key)
            seg_kind = "plc_output" if kind in {"DO", "AO"} else "plc_input"
            rows.append(_plc_segment(graph, placed, pin.id, seg_kind, net))
            for ep in net.endpoints if net else []:
                if ep.kind != "pin" or ep.ref.casefold() == placed.comp.id.casefold():
                    continue
                other = _placed(graph, ep.sheet_id, ep.ref)
                if other is None or other.device is None:
                    continue
                if is_offpage_component(other.comp, other.device):
                    continue
                rows.append(_device_segment(graph, other, [ep.pin], _control_kind(other.device, other.comp, [ep.pin]), net))
    return _dedupe_segments(rows)


def _wired_pin_segments(graph: Graph, members: list[Placed]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for placed in members:
        if placed.device is None:
            continue
        for pin in placed.device.pins:
            net_key = graph.pin_net.get((placed.sheet.id, placed.comp.id.casefold(), pin.id.casefold()))
            if not net_key:
                continue
            net = graph.nets.get(net_key)
            kind = _load_kind(placed) if _pin(placed.device, "A1") or _is_lamp(placed.device) else _control_kind(placed.device, placed.comp, [pin.id])
            if _is_drive(placed):
                kind = "vfd"
            rows.append(_device_segment(graph, placed, [pin.id], kind, net))
            if net is None:
                continue
            for ep in net.endpoints:
                if ep.kind != "pin" or ep.ref.casefold() == placed.comp.id.casefold():
                    continue
                other = _placed(graph, ep.sheet_id, ep.ref)
                if other is None or other.device is None or is_offpage_component(other.comp, other.device):
                    continue
                rows.append(_device_segment(graph, other, [ep.pin], _control_kind(other.device, other.comp, [ep.pin]), net))
    return _dedupe_segments(rows)


# --- checks and causes -----------------------------------------------------


def _checks_for(graph: Graph, circuit: dict[str, Any], symptom: str) -> list[dict[str, Any]]:
    segments = _segments_for_symptom(circuit, symptom)
    checks: list[dict[str, Any]] = []
    order = 1
    for segment in segments:
        if segment["kind"] in _SWITCH_KINDS and len(segment.get("terminals") or []) >= 2:
            checks.append(_continuity_check(graph, segment, order))
            order += 1
    for segment in segments:
        if segment["kind"] == "rail" or segment.get("terminals") or segment.get("wire"):
            checks.append(_voltage_check(graph, segment, order, symptom))
            order += 1
    return checks


def _segments_for_symptom(circuit: dict[str, Any], symptom: str) -> list[dict[str, Any]]:
    control = list(circuit["control"]["segments"])
    power: list[dict[str, Any]] = []
    for phase in circuit["power"]["phases"]:
        power.extend(phase["segments"])
    if symptom == "no_control_power":
        chosen = control or power
    elif symptom in {"plc_input_not_seen", "plc_output_on_load_off"}:
        chosen = control or power
    elif symptom == "vfd_faulted":
        chosen = power + control
    elif symptom == "motor_trips":
        chosen = power + [row for row in control if row["kind"] in {"ol_nc", "heater", "breaker", "estop", "safety"}]
    else:
        chosen = control + power
    return chosen


def _continuity_check(graph: Graph, segment: dict[str, Any], order: int) -> dict[str, Any]:
    pins = list(segment.get("terminals") or [])
    ref = segment["ref"]
    state = _expected_continuity(graph, segment)
    text = f"{ref} terminal {pins[0]} to {ref} terminal {pins[1]}"
    return _check(
        segment,
        order=order,
        phase="de-energized",
        energized=DEENERGIZED,
        terminal=pins[0],
        meter_leads=text,
        expected={
            "value": state,
            "volts": None,
            "kind": "continuity",
            "text": state,
            "needs_info": False,
            "source": "symbol contact state",
        },
        instruction=(
            f"Lock out and tag out first. {DEENERGIZED}. Check continuity from {ref} terminal {pins[0]} "
            f"to terminal {pins[1]}.{_safety_suffix(segment)}"
        ),
        graph=graph,
    )


def _safety_suffix(segment: dict[str, Any]) -> str:
    if segment.get("kind") in {"ol_nc", "heater"} or segment.get("overload"):
        return " " + OVERLOAD_OPEN_TEXT
    if segment.get("safety_device") or segment.get("kind") in {"estop", "safety"}:
        return " " + SAFETY_OPEN_TEXT
    return ""


def _measure_pin(segment: dict[str, Any], pins: list[str]) -> str:
    """Pin on the measured net. A hop lists the destination pin last."""

    del segment
    if not pins:
        return ""
    return str(pins[-1])


def _voltage_check(graph: Graph, segment: dict[str, Any], order: int, symptom: str) -> dict[str, Any]:
    pins = list(segment.get("terminals") or [])
    terminal = _measure_pin(segment, pins)
    wire = str(segment.get("wire") or "")
    if wire and wire not in _known_wires(graph.project):
        wire = ""
    return_name = _return_name(graph, str(segment.get("sheet") or ""))
    if wire:
        leads = f"wire {wire} to {return_name}" if return_name else f"wire {wire}"
    elif terminal and segment["ref"]:
        leads = f"{segment['ref']} terminal {terminal} to {return_name}" if return_name else f"{segment['ref']} terminal {terminal}"
    else:
        leads = return_name
    expected = _expected_voltage(segment, symptom, return_name)
    if expected["needs_info"]:
        extra = " The drawing does not state the expected voltage."
    elif expected["text"]:
        extra = f" Expected {expected['text']}."
    else:
        extra = ""
    return _check(
        segment,
        order=order,
        phase="live",
        energized=LIVE,
        terminal=terminal,
        meter_leads=leads,
        expected=expected,
        instruction=f"{LIVE}. {_live_context(symptom)} Measure {leads}.{extra}{_safety_suffix(segment)}",
        graph=graph,
    )


def _check(
    segment: dict[str, Any],
    *,
    order: int,
    phase: str,
    energized: str,
    terminal: str,
    meter_leads: str,
    expected: dict[str, Any],
    instruction: str,
    graph: Graph,
) -> dict[str, Any]:
    plc = None
    if segment.get("address"):
        plc = {
            "module": segment["ref"],
            "address": segment.get("address") or "",
            "tag": segment.get("tag") or "",
            "direction": segment.get("direction") or "",
            "terminal": terminal,
        }
    else:
        plc = _plc_on_segment(graph, segment)
    return {
        "id": f"{phase}-{order}-{segment['ref']}-{terminal or segment['kind']}",
        "order": order,
        "phase": phase,
        "energized": energized,
        "segment_id": segment.get("id") or "",
        "kind": segment.get("kind") or "",
        "sheet": segment.get("sheet") or "",
        "page": segment.get("page") or "",
        "zone": segment.get("zone") or "",
        "page_zone": segment.get("page_zone") or "",
        "device": segment.get("ref") or "",
        "terminal": terminal,
        "terminals": list(segment.get("terminals") or []),
        "wire": segment.get("wire") or "",
        "wires": list(segment.get("wires") or []),
        "meter_leads": meter_leads,
        "expected": expected,
        "plc": plc,
        "instruction": instruction,
        "safety_device": bool(segment.get("safety_device")),
        "branch": bool(segment.get("branch")),
    }


def _expected_continuity(graph: Graph, segment: dict[str, Any]) -> str:
    kind = segment.get("kind") or ""
    if kind in {"start", "seal_in", "aux_no"}:
        return "open"
    placed = _find_ref(graph, segment.get("ref") or "")
    if placed is not None and placed.device is not None:
        state = contact_state(placed.device)
        if state == "NO":
            return "open"
        if state == "NC":
            return "continuity"
    return "continuity"


def _expected_voltage(segment: dict[str, Any], symptom: str, return_name: str) -> dict[str, Any]:
    if symptom == "wont_stop" and segment.get("kind") in {"coil", "lamp", "seal_in"} and return_name:
        return {
            "value": f"0 V to {return_name}",
            "volts": 0.0,
            "kind": "state",
            "text": f"0 V to {return_name} when stop is pressed",
            "needs_info": False,
            "source": "stop open state",
        }
    voltage = segment.get("voltage")
    if isinstance(voltage, dict) and voltage.get("text"):
        return {
            "value": voltage["text"],
            "volts": voltage.get("volts"),
            "kind": "voltage",
            "text": voltage["text"],
            "needs_info": False,
            "source": voltage.get("source") or "",
        }
    return {
        "value": None,
        "volts": None,
        "kind": "voltage",
        "text": None,
        "needs_info": True,
        "source": "",
    }


def _live_context(symptom: str) -> str:
    if symptom == "no_control_power":
        return "With the supply energized,"
    if symptom == "wont_stop":
        return "With stop pressed,"
    if symptom == "plc_output_on_load_off":
        return "With the output commanded,"
    if symptom == "vfd_faulted":
        return "Read the drive fault and, with the drive energized,"
    return "With the circuit energized and start pressed,"


def _rank_causes(
    graph: Graph,
    circuit: dict[str, Any],
    checks: list[dict[str, Any]],
    symptom: str,
    readings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    _last_ok, first_wrong, window = _bisection(checks, readings)
    bad_segment = ""
    if first_wrong is not None:
        match = next((row for row in checks if row["id"] == first_wrong["id"]), None)
        bad_segment = str((match or {}).get("segment_id") or "")
    causes: list[dict[str, Any]] = []
    segments = _segments_for_symptom(circuit, symptom)
    seen_text: set[str] = set()
    for segment in segments:
        seg_id = segment.get("id") or ""
        if window and seg_id not in window:
            continue
        for cause in _causes_for_segment(graph, segment):
            if cause["text"] in seen_text:
                continue
            seen_text.add(cause["text"])
            weight = _WEIGHT.get(segment.get("kind") or "", 10)
            weight *= _BOOST.get(symptom, {}).get(segment.get("kind") or "", 1.0)
            if window and seg_id == bad_segment:
                weight += 300
            elif window and seg_id in window:
                weight += 150
            cause["weight"] = round(weight, 1)
            cause["segment_id"] = seg_id
            causes.append(cause)
    causes.sort(key=lambda row: (-row["weight"], row.get("ref") or "", row["text"]))
    for index, cause in enumerate(causes, start=1):
        cause["rank"] = index
        cause["id"] = f"cause-{index}-{cause['ref']}-{cause['kind']}"
    return causes, sorted(window)


def _causes_for_segment(graph: Graph, segment: dict[str, Any]) -> list[dict[str, Any]]:
    kind = segment.get("kind") or "device"
    ref = segment.get("ref") or ""
    pins = [pin for pin in (segment.get("terminals") or []) if pin]
    wire = segment.get("wire") or ""
    if wire and wire not in _known_wires(graph.project):
        wire = ""
    rows = [_cause_row(graph, segment, kind, _cause_text(kind, ref, pins, wire, segment), wire)]
    if wire and len(pins) >= 1:
        rows.append(
            _cause_row(
                graph,
                segment,
                "wire",
                _wire_cause(segment, wire),
                wire,
            )
        )
    if pins and kind not in {"rail"}:
        rows.append(
            _cause_row(
                graph,
                segment,
                "terminal",
                _terminal_cause(ref, pins),
                "",
            )
        )
    return [row for row in rows if row["text"]]


def _cause_text(kind: str, ref: str, pins: list[str], wire: str, segment: dict[str, Any]) -> str:
    terms = _terms_phrase(ref, pins)
    if kind in {"estop", "safety"} or segment.get("safety_device"):
        where = f"{terms} are open. " if pins else f"{ref} is open. "
        return where + SAFETY_OPEN_TEXT
    if kind in {"ol_nc", "heater"}:
        where = f"Overload {terms} may be open. " if pins else f"Overload {ref} may be open. "
        return where + OVERLOAD_OPEN_TEXT
    if kind == "plc_output":
        address = segment.get("address") or (pins[0] if pins else "")
        tag = segment.get("tag") or ""
        tag_bit = f" ({tag})" if tag else ""
        place = f"{ref} terminal {address}" if address else ref
        return f"PLC output {place}{tag_bit} may not be commanded. {PLC_LEAVE_TEXT}"
    if kind == "plc_input":
        address = segment.get("address") or (pins[0] if pins else "")
        tag = segment.get("tag") or ""
        tag_bit = f" ({tag})" if tag else ""
        return (
            f"PLC input {ref} terminal {address}{tag_bit} is not seeing the field device. "
            "Confirm the field device and the conductor."
        )
    if kind == "stop":
        return f"Stop {terms} may be open."
    if kind == "start":
        return f"Start {terms} may not be closing."
    if kind == "seal_in":
        return f"Seal-in {terms} may be open."
    if kind == "aux_no":
        return f"NO contact {terms} may be open."
    if kind == "interlock":
        return f"Interlock {terms} may be open."
    if kind == "fuse":
        return f"Fuse {terms} may be open."
    if kind == "breaker":
        return f"Breaker {terms} may be open or tripped."
    if kind == "supply":
        return f"Control supply {terms} may be off."
    if kind == "coil":
        return f"Coil {terms} may be open."
    if kind == "lamp":
        return f"Lamp {terms} may be open."
    if kind == "contactor_pole":
        return f"Contactor {terms} may be open."
    if kind == "motor":
        return f"Motor {terms} may be open."
    if kind == "vfd":
        return f"Drive {terms} may be faulted. Read the fault and reset per the machine's procedure."
    if kind == "rail":
        named = f"wire {wire}" if wire else ref
        return f"{named} may not be present at the supply rail."
    if kind == "terminal":
        return _terminal_cause(ref, pins)
    return f"{terms} may be open."


def _wire_cause(segment: dict[str, Any], wire: str) -> str:
    pins = list(segment.get("terminals") or [])
    ref = segment.get("ref") or ""
    pin = pins[-1] if pins else ""
    if ref and pin:
        return f"Wire {wire} at {ref} terminal {pin} may be open."
    return f"Wire {wire} may be open."


def _terminal_cause(ref: str, pins: list[str]) -> str:
    if not ref or not pins:
        return ""
    if len(pins) == 1:
        return f"Terminal {ref} {pins[0]} may be loose."
    return f"Terminal {ref} {pins[0]} or {ref} {pins[1]} may be loose."


def _cause_row(graph: Graph, segment: dict[str, Any], kind: str, text: str, wire: str) -> dict[str, Any]:
    return {
        "rank": 0,
        "id": "",
        "kind": kind,
        "ref": segment.get("ref") or "",
        "terminals": list(segment.get("terminals") or []),
        "wire": wire,
        "sheet": segment.get("sheet") or "",
        "page_zone": segment.get("page_zone") or "",
        "text": text,
        "safety_device": bool(segment.get("safety_device")) or kind in {"estop", "safety"},
        "overload": bool(segment.get("overload")) or kind in {"ol_nc", "heater"},
        "weight": 0,
    }


def _terms_phrase(ref: str, pins: list[str]) -> str:
    if not pins:
        return ref
    if len(pins) == 1:
        return f"{ref} terminal {pins[0]}"
    return f"{ref} terminals {pins[0]} and {pins[1]}"


def _parse_readings(checks: list[dict[str, Any]], readings: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not readings:
        return []
    if not isinstance(readings, list):
        raise QueryError("readings must be a list")
    parsed: list[dict[str, Any]] = []
    for raw in readings:
        if not isinstance(raw, dict):
            raise QueryError("each reading must be an object")
        result = str(raw.get("result") or raw.get("value") or "").strip().casefold()
        aliases = {"pass": "ok", "fail": "wrong", "bad": "wrong", "good": "ok", "skip": "skipped"}
        result = aliases.get(result, result)
        if result not in {"ok", "wrong", "skipped"}:
            raise QueryError("reading result must be ok, wrong, or skipped")
        check = _match_check(checks, raw)
        if check is None:
            raise QueryError("reading does not match a check on the drawing")
        item = {"id": check["id"], "result": result}
        if raw.get("measured") not in (None, ""):
            item["measured"] = str(raw.get("measured"))
        parsed.append(item)
    return parsed


def _match_check(checks: list[dict[str, Any]], raw: dict[str, Any]) -> dict[str, Any] | None:
    ident = str(raw.get("id") or raw.get("check_id") or raw.get("checkpoint") or "").strip()
    if ident:
        for check in checks:
            if check["id"] == ident:
                return check
        return None
    device = str(raw.get("device") or raw.get("ref") or "").strip()
    terminal = str(raw.get("terminal") or raw.get("pin") or "").strip()
    wire = str(raw.get("wire") or "").strip()
    for check in checks:
        if device and check["device"].casefold() == device.casefold():
            if terminal and terminal.casefold() in {pin.casefold() for pin in check.get("terminals") or []}:
                return check
            if not terminal:
                return check
        if wire and wire == check.get("wire"):
            return check
    return None


def _fault_marks(checks: list[dict[str, Any]], readings: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    last_ok, first_wrong, _window = _bisection(checks, readings)
    return last_ok, first_wrong


def _bisection(
    checks: list[dict[str, Any]],
    readings: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, set[str]]:
    """First wrong reading, scoped to that LIVE or DE-ENERGIZED pass.

    Continuity is listed before voltage. A wrong voltage check does not pull
    later continuity rows into the fault window.
    """

    answered = {row["id"]: row["result"] for row in readings}
    ordered = sorted(checks, key=lambda row: row["order"])
    first = next((check for check in ordered if answered.get(check["id"]) == "wrong"), None)
    if first is None:
        return None, None, set()
    phase_rows = [row for row in ordered if row["energized"] == first["energized"]]
    last_ok = None
    for check in phase_rows:
        if check["id"] == first["id"]:
            break
        if answered.get(check["id"]) == "ok":
            last_ok = {
                "id": check["id"],
                "device": check["device"],
                "terminal": check["terminal"],
                "wire": check["wire"],
                "order": check["order"],
            }
    start_order = last_ok["order"] if last_ok is not None else 0
    window: set[str] = set()
    for check in phase_rows:
        if start_order < check["order"] <= first["order"] and check.get("segment_id"):
            window.add(check["segment_id"])
    if not window and first.get("segment_id"):
        window.add(first["segment_id"])
    first_wrong = {
        "id": first["id"],
        "device": first["device"],
        "terminal": first["terminal"],
        "wire": first["wire"],
    }
    if last_ok is not None:
        last_ok = {key: last_ok[key] for key in ("id", "device", "terminal", "wire")}
    return last_ok, first_wrong, window


# --- upstream / downstream -------------------------------------------------


def _walk(project: Project, catalog: Catalog, endpoint: str, *, toward: str) -> dict[str, Any]:
    graph = build_graph(project, catalog)
    sheet_id, ref, pin = _parse_endpoint(graph, endpoint)
    placed = _placed(graph, sheet_id, ref)
    if placed is None or placed.device is None:
        raise QueryError(f"device {ref} is not on the drawing")
    real_pin = _pin(placed.device, pin)
    if real_pin is None:
        raise QueryError(f"{placed.comp.id} has no terminal {pin} on the drawing")
    net_key = graph.pin_net.get((placed.sheet.id, placed.comp.id.casefold(), real_pin.casefold()))
    needs: list[str] = []
    if not net_key:
        needs.append(f"{placed.comp.id} terminal {real_pin} has no wire on the drawing.")
        hops: list[dict[str, Any]] = []
    else:
        hops = _directed_hops(graph, placed, real_pin, net_key, toward)
        if not hops:
            needs.append(f"No {toward} conductor from {placed.comp.id} terminal {real_pin} is on the drawing.")
    direction = "toward the supply" if toward == "upstream" else "toward the load"
    return {
        "op": toward,
        "read_only": True,
        "safety": safety_block(),
        "endpoint": f"{placed.comp.id}.{real_pin}",
        "ref": placed.comp.id,
        "pin": real_pin,
        "direction": direction,
        "hops": hops,
        "needs_info": needs,
    }


def _directed_hops(graph: Graph, placed: Placed, pin: str, net_key: str, toward: str) -> list[dict[str, Any]]:
    domain = _pin_domain(placed, pin)
    if domain == "power":
        return _power_hops(graph, net_key, toward)
    return _control_hops(graph, placed, pin, net_key, toward)


def _control_hops(graph: Graph, placed: Placed, pin: str, net_key: str, toward: str) -> list[dict[str, Any]]:
    sources = _source_nets(graph)
    returns = _return_nets(graph)
    adj = _adj(graph, "control")
    source_dist = _distances(sources, adj)
    hops: list[dict[str, Any]] = []
    mate_name = _LOAD_MATES.get(pin.upper())
    mate = _pin(placed.device, mate_name) if mate_name else None
    mate_net = None
    if mate:
        mate_net = graph.pin_net.get((placed.sheet.id, placed.comp.id.casefold(), mate.casefold()))
    hot_side = _is_hot_side(net_key, mate_net, source_dist, pin)
    if toward == "downstream" and hot_side and mate and mate_net:
        hops.append(_device_hop(graph, placed, pin, mate, "coil" if not _is_lamp(placed.device) else "lamp", ""))
        hops.extend(_step_hops(graph, mate_net, _distances(returns, adj), closer=True, domain="control"))
        return _number_hops(hops)
    if toward == "upstream" and not hot_side and mate and mate_net:
        hops.append(_device_hop(graph, placed, pin, mate, "coil" if not _is_lamp(placed.device) else "lamp", ""))
        hops.extend(_step_hops(graph, mate_net, source_dist, closer=True, domain="control"))
        return _number_hops(hops)
    if toward == "upstream":
        hops.extend(_step_hops(graph, net_key, source_dist, closer=True, domain="control"))
    else:
        hops.extend(_step_hops(graph, net_key, _distances(returns, adj), closer=True, domain="control"))
    return _number_hops(hops)


def _power_hops(graph: Graph, net_key: str, toward: str) -> list[dict[str, Any]]:
    motor_nets = set()
    for placed in graph.devices.values():
        if not _is_motor(placed) and not _is_drive(placed):
            continue
        for pin in _motor_pins(placed) or _drive_out_pins(placed):
            key = graph.pin_net.get((placed.sheet.id, placed.comp.id.casefold(), pin.casefold()))
            if key:
                motor_nets.add(key)
    adj = _adj(graph, "power")
    dist = _distances(motor_nets, adj)
    closer = toward == "downstream"
    return _number_hops(_step_hops(graph, net_key, dist, closer=closer, domain="power"))


def _step_hops(
    graph: Graph,
    start: str,
    dist: dict[str, int],
    *,
    closer: bool,
    domain: str,
) -> list[dict[str, Any]]:
    hops: list[dict[str, Any]] = []
    seen = {start}
    queue = [start]
    while queue:
        current = queue.pop(0)
        here = dist.get(current)
        for edge in _adj(graph, domain).get(current, []):
            other = edge.other(current)
            there = dist.get(other)
            if there is None or here is None:
                continue
            step_ok = there < here if closer else there > here
            if not step_ok:
                continue
            placed = _placed(graph, edge.sheet_id, edge.ref)
            hop = Hop(edge, current, other)
            if placed is not None:
                hops.append(_device_hop(graph, placed, hop.pin_src, hop.pin_dst, edge.kind, _first_number(graph.nets.get(current))))
            if other not in seen:
                seen.add(other)
                queue.append(other)
    return hops


def _device_hop(
    graph: Graph,
    placed: Placed,
    pin_from: str,
    pin_to: str,
    kind: str,
    wire: str,
) -> dict[str, Any]:
    loc = _location(graph, placed)
    pins = [pin for pin in (pin_from, pin_to) if pin]
    return {
        "order": 0,
        "ref": placed.comp.id,
        "terminals": pins,
        "wire": wire if wire in _known_wires(graph.project) else "",
        "kind": kind,
        "sheet": placed.sheet.id,
        "page_zone": loc["page_zone"],
        "safety_device": _is_safety_device(placed.device, placed.comp),
    }


def _number_hops(hops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, tuple[str, ...]]] = set()
    rows: list[dict[str, Any]] = []
    for hop in hops:
        key = (hop["ref"].casefold(), tuple(hop.get("terminals") or []))
        if key in seen:
            continue
        seen.add(key)
        rows.append(hop)
    for index, hop in enumerate(rows, start=1):
        hop["order"] = index
    return rows


# --- voltage, nets, devices ------------------------------------------------


def _net_voltage(project: Project, devices: dict[tuple[str, str], Placed], net: Net) -> tuple[dict[str, Any] | None, str]:
    found: list[tuple[float, str, str]] = []
    for _sheet_id, wire in net.wires:
        for label, source in ((wire.net, "net"), (wire.label, "label")):
            parsed = _voltage_token(label)
            if parsed:
                found.append((parsed[0], parsed[1], f"{source}:{label}"))
        number = str(wire.number or "")
        if "V" in number.upper():
            parsed = _voltage_token(number)
            if parsed:
                found.append((parsed[0], parsed[1], f"wire:{number}"))
    for ep in net.endpoints:
        if ep.kind == "rail":
            parsed = _voltage_token(ep.ref)
            if parsed:
                found.append((parsed[0], parsed[1], f"rail:{ep.ref}"))
            continue
        placed = devices.get((ep.sheet_id, ep.ref.casefold()))
        if placed is None:
            continue
        supply = _supply_voltage(placed, ep.pin)
        if supply:
            found.append(supply)
    if not found:
        return None, ""
    volts = found[0][0]
    if any(abs(item[0] - volts) > 0.6 for item in found):
        return None, "conflicting voltages on this net"
    kinds = {item[1] for item in found if item[1]}
    if len(kinds) > 1:
        return None, "conflicting AC and DC marks on this net"
    kind = next(iter(kinds), "")
    source = found[0][2]
    for item in found:
        if item[1]:
            source = item[2]
            break
    return {
        "volts": volts,
        "kind": kind,
        "text": _voltage_text(volts, kind),
        "source": source,
    }, ""


def _supply_voltage(placed: Placed, pin: str) -> tuple[float, str, str] | None:
    if placed.device is None or not _is_supply_output(placed, pin):
        return None
    props = _props(placed)
    cat = placed.device.category or ""
    keys = ("Vout", "voltage", "secondary") if cat.startswith("power.supply") else ("secondary", "voltage")
    for key in keys:
        parsed = parse_volts(props.get(key))
        if parsed:
            return parsed[0], parsed[1], f"device:{placed.comp.id}.{key}"
    return None


def _voltage_token(value: Any) -> tuple[float, str] | None:
    return _hint_voltage(str(value or ""))


def _voltage_text(volts: float, kind: str) -> str:
    label = {"ac": "AC", "dc": "DC"}.get(kind, "")
    number = f"{volts:g}"
    if label:
        return f"{number} V{label}"
    return f"{number} V"


def _device_record(graph: Graph, placed: Placed) -> dict[str, Any]:
    device = placed.device
    loc = _location(graph, placed)
    props = _props(placed)
    catalog_no = str(props.get("catalog") or props.get("part_number") or "").strip()
    pins = []
    if device is not None:
        for pin in device.pins:
            pins.append({"id": pin.id, "terminal": pin.label or pin.id})
    return {
        "tag": placed.comp.id,
        "type": element_kind(placed.comp, device) if device is not None else "other",
        "name": device.name if device is not None else "",
        "catalog": catalog_no,
        "library_id": placed.comp.library_id,
        "sheet": placed.sheet.id,
        "page": loc["page"],
        "zone": loc["zone"],
        "page_zone": loc["page_zone"],
        "pins": pins,
        "safety": _is_safety_device(device, placed.comp),
    }


def _net_record(graph: Graph, net: Net) -> dict[str, Any]:
    endpoints: list[str] = []
    terminals: list[str] = []
    rails: list[str] = []
    for ep in net.endpoints:
        endpoints.append(ep.token())
        if ep.kind == "rail":
            rails.append(ep.ref)
            continue
        placed = _placed(graph, ep.sheet_id, ep.ref)
        if placed is None or placed.device is None:
            continue
        ident = terminal_identity(placed.comp, placed.device)
        if ident:
            terminals.append(f"{ident[0]}:{ident[1]}")
    numbers = _numbers(net)
    names = _net_names(net)
    return {
        "id": numbers[0] if len(numbers) == 1 else (names[0] if len(names) == 1 else net.key),
        "wire_no": numbers[0] if len(numbers) == 1 else "",
        "wire_numbers": numbers,
        "name": names[0] if len(names) == 1 else "",
        "voltage": net.voltage,
        "rail": rails[0] if len(rails) == 1 else "",
        "endpoints": endpoints,
        "terminals": _unique(terminals),
    }


def _cross_refs(project: Project, catalog: Catalog) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, family in xref_families(project, catalog).items():
        coils = [member.comp.id for member in family.coils]
        contacts = [member.comp.id for member in family.contacts]
        if not coils and not contacts:
            continue
        rows.append({"device": key, "coils": coils, "contacts": contacts})
    rows.sort(key=lambda row: row["device"].casefold())
    return rows


def _plc_rows(project: Project, catalog: Catalog) -> list[dict[str, str]]:
    wires = _known_wires(project)
    rows: list[dict[str, str]] = []
    for row in io_rows(project, catalog):
        raw_wire = str(row.get("wire") or "")
        wire = raw_wire if raw_wire in wires else ""
        net = "" if wire else raw_wire
        rows.append(
            {
                "module": row.get("module") or "",
                "address": row.get("address") or "",
                "point": row.get("point") or "",
                "tag": row.get("tag") or "",
                "direction": row.get("type") or "",
                "device": row.get("device") or "",
                "wire": wire,
                "net": net,
                "sheet": row.get("sheet") or "",
            }
        )
    return rows


def _io_match(graph: Graph, module: str, pin: str) -> dict[str, str] | None:
    for row in graph.io:
        if row["module"].casefold() == module.casefold() and row["address"].casefold() == pin.casefold():
            return row
    return None


def _plc_on_segment(graph: Graph, segment: dict[str, Any]) -> dict[str, str] | None:
    ref = str(segment.get("ref") or "")
    for pin in segment.get("terminals") or []:
        match = _io_match(graph, ref, pin)
        if match:
            return {
                "module": match["module"],
                "address": match["address"],
                "tag": match["tag"],
                "direction": match["direction"],
                "terminal": pin,
            }
    return None


# --- path search -----------------------------------------------------------


def _search(
    starts: set[str],
    goals: set[str],
    adj: dict[str, list[Edge]],
    *,
    allow_empty: bool,
    must: set[str] | None,
    limit: int = 8,
    max_hops: int = 12,
) -> list[list[Hop]]:
    found: list[list[Hop]] = []
    if not goals:
        return found

    def walk(net: str, hops: list[Hop], seen: set[str]) -> None:
        if len(found) >= limit:
            return
        if net in goals and (hops or allow_empty):
            refs = {hop.edge.ref.casefold() for hop in hops}
            if must is None or (must & refs) or (allow_empty and not hops and must is None):
                found.append(list(hops))
            if hops or not allow_empty:
                return
        if len(hops) >= max_hops:
            return
        for edge in adj.get(net, []):
            nxt = edge.other(net)
            if nxt in seen:
                continue
            seen.add(nxt)
            hops.append(Hop(edge, net, nxt))
            walk(nxt, hops, seen)
            hops.pop()
            seen.remove(nxt)

    for start in sorted(starts):
        if start not in goals and start not in adj and not allow_empty:
            continue
        walk(start, [], {start})
        if len(found) >= limit:
            break
    return found


def _pick_spine(paths: list[list[Hop]]) -> list[Hop]:
    if not paths:
        return []

    def score(path: list[Hop]) -> tuple[int, int]:
        kinds = [hop.edge.kind for hop in path]
        rank = len(path)
        if "stop" in kinds:
            rank += 10
        if "estop" in kinds:
            rank += 8
        if "start" in kinds:
            rank += 5
        if "ol_nc" in kinds:
            rank += 3
        return rank, len(path)

    return max(paths, key=score)


def _branch_hops(paths: list[list[Hop]], spine: list[Hop]) -> list[Hop]:
    have = {(hop.edge.ref.casefold(), hop.edge.pin_a.casefold(), hop.edge.pin_b.casefold()) for hop in spine}
    extra: list[Hop] = []
    for path in paths:
        for hop in path:
            key = (hop.edge.ref.casefold(), hop.edge.pin_a.casefold(), hop.edge.pin_b.casefold())
            if key in have:
                continue
            have.add(key)
            extra.append(hop)
    return extra


def _adj(graph: Graph, domain: str) -> dict[str, list[Edge]]:
    adj: dict[str, list[Edge]] = defaultdict(list)
    for edge in graph.edges:
        if edge.domain != domain:
            continue
        adj[edge.net_a].append(edge)
        adj[edge.net_b].append(edge)
    return adj


def _distances(starts: set[str], adj: dict[str, list[Edge]]) -> dict[str, int]:
    dist = {key: 0 for key in starts}
    queue = list(starts)
    while queue:
        current = queue.pop(0)
        for edge in adj.get(current, []):
            other = edge.other(current)
            if other not in dist:
                dist[other] = dist[current] + 1
                queue.append(other)
    return dist


def _source_nets(graph: Graph) -> set[str]:
    found: set[str] = set()
    for net in graph.nets.values():
        for ep in net.endpoints:
            if ep.kind == "rail":
                sheet = _sheet(graph, ep.sheet_id)
                if sheet is None:
                    continue
                hot, _ret = rail_names(sheet, graph.project)
                if ep.ref.casefold() == hot.casefold():
                    found.add(net.key)
                continue
            placed = _placed(graph, ep.sheet_id, ep.ref)
            if placed is None:
                continue
            if _is_supply_output(placed, ep.pin) or _is_plc_output_pin(placed, ep.pin):
                found.add(net.key)
    return found


def _return_nets(graph: Graph) -> set[str]:
    found: set[str] = set()
    for net in graph.nets.values():
        for ep in net.endpoints:
            if ep.kind == "rail":
                sheet = _sheet(graph, ep.sheet_id)
                if sheet is None:
                    continue
                _hot, ret = rail_names(sheet, graph.project)
                if ep.ref.casefold() == ret.casefold():
                    found.add(net.key)
                continue
            placed = _placed(graph, ep.sheet_id, ep.ref)
            if placed is not None and _is_return_pin(placed, ep.pin):
                found.add(net.key)
    return found


def _end_nets(graph: Graph) -> set[str]:
    found: set[str] = set()
    for key, (sheet_id, ref_cf, pin_cf) in ((net, item) for item, net in graph.pin_net.items()):
        del key
        placed = _placed(graph, sheet_id, ref_cf)
        if placed is None or placed.device is None:
            continue
        real = _pin(placed.device, pin_cf) or pin_cf
        if _is_plc_input_pin(placed, real) or _is_load_pin(placed, real):
            found.add(graph.pin_net[(sheet_id, ref_cf, pin_cf)])
    return found


def _orient_load(graph: Graph, placed: Placed, sources: set[str]) -> tuple[str | None, str | None]:
    names = []
    if _pin(placed.device, "A1"):
        names = ["A1", "A2"]
    elif _pin(placed.device, "X1") and _is_lamp(placed.device):
        names = ["X1", "X2"]
    wired: list[tuple[str, str, int]] = []
    dist = _distances(sources, _adj(graph, "control"))
    for name in names:
        real = _pin(placed.device, name)
        if real is None:
            continue
        net = graph.pin_net.get((placed.sheet.id, placed.comp.id.casefold(), real.casefold()))
        if net:
            wired.append((real, net, dist.get(net, 999)))
    if not wired:
        return None, None
    wired.sort(key=lambda item: item[2])
    hot = wired[0][0]
    other = ""
    for name in names:
        real = _pin(placed.device, name)
        if real and real != hot:
            other = real
    return hot, other or None


def _is_hot_side(net_key: str, mate_net: str | None, source_dist: dict[str, int], pin: str) -> bool:
    here = source_dist.get(net_key)
    there = source_dist.get(mate_net) if mate_net else None
    if here is not None and (there is None or here <= there):
        return True
    if there is not None and here is None:
        return False
    return pin.upper() in {"A1", "X1"}


# --- resolve and classify --------------------------------------------------


def _resolve(graph: Graph, ref: str) -> list[Placed]:
    want = str(ref or "").strip()
    if not want:
        raise QueryError("device is required")
    found: list[Placed] = []
    for placed in graph.devices.values():
        if placed.device is not None and is_offpage_component(placed.comp, placed.device):
            continue
        if placed.comp.id.casefold() == want.casefold():
            found.append(placed)
            continue
        if placed.device is None:
            continue
        key = family_key(placed.comp, placed.device)
        if key and key.casefold() == want.casefold():
            found.append(placed)
    if not found:
        raise QueryError(f"device {want} is not on the drawing")
    expanded: list[Placed] = list(found)
    seen = {(item.sheet.id, item.comp.id.casefold()) for item in found}
    for item in found:
        if item.device is None:
            continue
        key = family_key(item.comp, item.device)
        if not key:
            continue
        for other in graph.devices.values():
            if other.device is None or is_offpage_component(other.comp, other.device):
                continue
            other_key = family_key(other.comp, other.device)
            ident = (other.sheet.id, other.comp.id.casefold())
            if other_key and other_key.casefold() == key.casefold() and ident not in seen:
                seen.add(ident)
                expanded.append(other)
    return expanded


def _feeder_coils(graph: Graph, motors: list[Placed]) -> list[Placed]:
    refs: list[str] = []
    for motor in motors:
        for pin in _motor_pins(motor):
            net_key = graph.pin_net.get((motor.sheet.id, motor.comp.id.casefold(), pin.casefold()))
            if not net_key:
                continue
            seen = {net_key}
            current = net_key
            while True:
                options = [
                    edge
                    for edge in graph.edges
                    if edge.domain == "power" and edge.touches(current) and edge.other(current) not in seen
                ]
                if not options:
                    net = graph.nets.get(current)
                    if net is not None:
                        for ep in net.endpoints:
                            if ep.kind == "pin":
                                refs.append(ep.ref)
                    break
                edge = options[0]
                refs.append(edge.ref)
                current = edge.other(current)
                seen.add(current)
    coils: list[Placed] = []
    seen_ids: set[tuple[str, str]] = set()
    for ref in refs:
        for placed in graph.devices.values():
            if placed.comp.id.casefold() != ref.casefold():
                continue
            if not _pin(placed.device, "A1"):
                continue
            key = (placed.sheet.id, placed.comp.id.casefold())
            if key in seen_ids:
                continue
            seen_ids.add(key)
            coils.append(placed)
    return coils


def _motors_downstream(graph: Graph, feeders: list[Placed]) -> list[Placed]:
    motors: list[Placed] = []
    seen: set[tuple[str, str]] = set()
    for feeder in feeders:
        for edge in graph.edges:
            if edge.domain != "power" or edge.ref.casefold() != feeder.comp.id.casefold():
                continue
            for net_key in (edge.net_a, edge.net_b):
                net = graph.nets.get(net_key)
                if net is None:
                    continue
                for ep in net.endpoints:
                    if ep.kind != "pin":
                        continue
                    placed = _placed(graph, ep.sheet_id, ep.ref)
                    if placed is None or not _is_motor(placed):
                        continue
                    key = (placed.sheet.id, placed.comp.id.casefold())
                    if key in seen:
                        continue
                    seen.add(key)
                    motors.append(placed)
    return motors


def _parse_endpoint(graph: Graph, endpoint: str) -> tuple[str, str, str]:
    text = str(endpoint or "").strip()
    if "." not in text:
        raise QueryError("endpoint must be ref.pin")
    ref, pin = text.split(".", 1)
    if not ref or not pin:
        raise QueryError("endpoint must be ref.pin")
    matches = [
        placed
        for (sheet_id, ref_cf), placed in graph.devices.items()
        if ref_cf == ref.casefold() and placed.device is not None and not is_offpage_component(placed.comp, placed.device)
    ]
    if not matches:
        raise QueryError(f"device {ref} is not on the drawing")
    wired = [
        item
        for item in matches
        if (item.sheet.id, item.comp.id.casefold(), pin.casefold()) in graph.pin_net
    ]
    chosen = wired[0] if wired else matches[0]
    return chosen.sheet.id, chosen.comp.id, pin


def _with_load_pins(members: list[Placed]) -> list[Placed]:
    return [item for item in members if _pin(item.device, "A1") and _pin(item.device, "A2") and not _is_motor(item)]


def _load_kind(placed: Placed) -> str:
    if placed.device is None:
        return "coil"
    if _is_safety_device(placed.device, placed.comp) and symbol_role(placed.device) != "operator":
        return "safety"
    if _is_lamp(placed.device):
        return "lamp"
    if _is_drive(placed):
        return "vfd"
    if _is_motor(placed):
        return "motor"
    return "coil"


def _is_safety_device(device: Device | None, comp: Component) -> bool:
    if device is None:
        return False
    cat = (device.category or "").lower()
    lid = device.library_id.lower()
    tags = {str(tag).lower() for tag in device.tags}
    if "estop" in cat or "estop" in lid or "estop" in tags:
        return True
    if cat.startswith("switching.safety") or "safety" in tags or "safety" in lid:
        return True
    role = str(comp.properties.get("role") or "").lower()
    return role in {"safety", "estop", "guard"}


def _is_supply_output(placed: Placed, pin: str) -> bool:
    if placed.device is None:
        return False
    cat = placed.device.category or ""
    token = pin.casefold()
    if cat.startswith("power.supply") and token in _SUPPLY_OUT:
        return True
    if cat.startswith("power.transformer") and token in {"x1", "21"}:
        return True
    return False


def _is_return_pin(placed: Placed, pin: str) -> bool:
    if placed.device is None:
        return False
    token = pin.casefold()
    cat = placed.device.category or ""
    role = symbol_role(placed.device)
    if token in {"minus", "0v", "0vdc"} and cat.startswith("power."):
        return True
    if token in {"m", "com"} and (cat.startswith("power.") or role == "plc" or cat.startswith("drive.")):
        return True
    return False


def _is_plc(placed: Placed) -> bool:
    return placed.device is not None and symbol_role(placed.device) == "plc"


def _is_plc_output_pin(placed: Placed, pin: str) -> bool:
    if not _is_plc(placed) or placed.device is None:
        return False
    real = _pin(placed.device, pin)
    if real is None:
        return False
    try:
        electrical = placed.device.pin(real).electrical
    except KeyError:
        electrical = ""
    return _io_type(real, electrical) in {"DO", "AO"}


def _is_plc_input_pin(placed: Placed, pin: str) -> bool:
    if not _is_plc(placed) or placed.device is None:
        return False
    real = _pin(placed.device, pin)
    if real is None:
        return False
    try:
        electrical = placed.device.pin(real).electrical
    except KeyError:
        electrical = ""
    return _io_type(real, electrical) in {"DI", "AI"}


def _is_load_pin(placed: Placed, pin: str) -> bool:
    token = pin.upper()
    if token in {"A1", "X1"} and (_pin(placed.device, "A1") or _is_lamp(placed.device)):
        return True
    return False


def _is_lamp(device: Device | None) -> bool:
    if device is None:
        return False
    cat = device.category or ""
    lid = device.library_id.lower()
    return cat.startswith("operator.light") or "pilot" in lid


def _is_motor(placed: Placed) -> bool:
    if placed.device is None:
        return False
    cat = placed.device.category or ""
    lid = placed.device.library_id.lower()
    return cat.startswith("load.motor") or ("motor" in lid and "contactor" not in lid)


def _is_drive(placed: Placed) -> bool:
    if placed.device is None:
        return False
    return (placed.device.category or "").startswith("drive.") or symbol_role(placed.device) == "drive"


def _has_power_pins(placed: Placed) -> bool:
    if placed.device is None:
        return False
    pins = {pin.id.upper() for pin in placed.device.pins}
    return bool(pins & {"L1", "T1", "U", "U1"})


def _motor_pins(placed: Placed) -> list[str]:
    if placed.device is None:
        return []
    return [pin.id for pin in placed.device.pins if pin.id.upper() in _MOTOR_PINS or pin.id.upper() in {"U", "V", "W"}]


def _drive_out_pins(placed: Placed) -> list[str]:
    if placed.device is None:
        return []
    return [pin.id for pin in placed.device.pins if pin.id.upper() in {"U", "V", "W", "T1", "T2", "T3"}]


def _pin_domain(placed: Placed, pin: str) -> str:
    token = pin.upper()
    if token in {"A1", "A2", "X1", "X2"}:
        return "control"
    if _is_plc_input_pin(placed, pin) or _is_plc_output_pin(placed, pin):
        return "control"
    if placed.device is not None:
        cat = placed.device.category or ""
        role = symbol_role(placed.device)
        if token in _MOTOR_PINS or token in {"L1", "L2", "L3", "T1", "T2", "T3", "1", "2", "3", "4", "5", "6"}:
            if role in {"breaker", "disconnect", "parent", "drive"} or cat.startswith("protection.") or _is_motor(placed) or _is_drive(placed):
                return "power"
    return "control"


def _mate_pin(device: Device | None, pin: str) -> str:
    if device is None:
        return ""
    pairs = _pairs(device)
    for left, right in pairs:
        if left.casefold() == pin.casefold():
            return right
        if right.casefold() == pin.casefold():
            return left
    return ""


# --- small helpers ---------------------------------------------------------


def _device(catalog: Catalog, comp: Component) -> Device | None:
    try:
        return catalog.get(comp.library_id)
    except KeyError:
        return None


def _endpoint(sheet: Sheet, raw: str) -> Endpoint | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if "." in text:
        ref, pin = text.split(".", 1)
        if ref and pin:
            return Endpoint(sheet.id, ref, pin, "pin")
        return None
    return Endpoint(sheet.id, text, "", "rail")


def _end_key(ep: Endpoint) -> str:
    if ep.kind == "rail":
        return f"{ep.sheet_id}|rail|{ep.ref.casefold()}"
    return f"{ep.sheet_id}|{ep.ref.casefold()}|{ep.pin.casefold()}"


def _placed(graph: Graph, sheet_id: str, ref: str) -> Placed | None:
    return graph.devices.get((sheet_id, ref.casefold()))


def _find_ref(graph: Graph, ref: str) -> Placed | None:
    for placed in graph.devices.values():
        if placed.comp.id.casefold() == ref.casefold() and (
            placed.device is None or not is_offpage_component(placed.comp, placed.device)
        ):
            return placed
    return None


def _sheet(graph: Graph, sheet_id: str) -> Sheet | None:
    for sheet in graph.project.sheets:
        if sheet.id == sheet_id:
            return sheet
    return None


def _pin(device: Device | None, name: str | None) -> str | None:
    if device is None or not name:
        return None
    for pin in device.pins:
        if pin.id.casefold() == name.casefold():
            return pin.id
    return None


def _props(placed: Placed) -> dict[str, Any]:
    props: dict[str, Any] = {}
    if placed.device is not None:
        props.update(placed.device.properties or {})
    props.update(placed.comp.properties or {})
    return props


def _location(graph: Graph, placed: Placed) -> dict[str, str]:
    try:
        loc = component_location(graph.project, placed.sheet, placed.comp, placed.device)
    except Exception:  # noqa: BLE001 — a missing zone still returns the sheet
        return {"page": "", "zone": "", "page_zone": "", "line": ""}
    return {
        "page": str(loc.sheet_no),
        "zone": loc.zone(),
        "page_zone": f"{loc.sheet_no}-{loc.zone()}",
        "line": str(loc.line),
    }


def _numbers(net: Net | None) -> list[str]:
    if net is None:
        return []
    found: list[str] = []
    for _sheet_id, wire in net.wires:
        number = str(wire.number or "").strip()
        if number and number not in found:
            found.append(number)
    return found


def _first_number(net: Net | None) -> str:
    numbers = _numbers(net)
    return numbers[0] if numbers else ""


def _net_names(net: Net) -> list[str]:
    found: list[str] = []
    for _sheet_id, wire in net.wires:
        name = str(wire.net or "").strip()
        if name and name not in found:
            found.append(name)
    return found


def _net_name(net: Net | None) -> str:
    if net is None:
        return ""
    names = _net_names(net)
    if len(names) == 1:
        return names[0]
    numbers = _numbers(net)
    return numbers[0] if len(numbers) == 1 else ""


def _ends(graph: Graph, segments: list[dict[str, Any]]) -> tuple[str, str, str, str]:
    hot_name = ""
    hot_wire = ""
    ret_name = ""
    ret_wire = ""
    for segment in segments:
        if segment.get("kind") == "rail" and not hot_name:
            hot_name = segment.get("ref") or ""
            hot_wire = segment.get("wire") or ""
        if segment.get("kind") == "supply" and not hot_name:
            hot_name = segment.get("net") or ""
            hot_wire = segment.get("wire") or ""
    for segment in reversed(segments):
        sheet_id = str(segment.get("sheet") or "")
        if sheet_id and not ret_name:
            ret_name = _return_name(graph, sheet_id)
            if ret_name:
                ret_wire = _wire_for_name(graph, ret_name)
                break
    return hot_name, hot_wire, ret_name, ret_wire


def _return_name(graph: Graph, sheet_id: str) -> str:
    sheet = _sheet(graph, sheet_id) if sheet_id else None
    if sheet is not None:
        _hot, ret = rail_names(sheet, graph.project)
        for net in graph.nets.values():
            for ep in net.endpoints:
                if ep.kind == "rail" and ep.sheet_id == sheet.id and ep.ref.casefold() == ret.casefold():
                    return ep.ref
    for net in graph.nets.values():
        for name in _net_names(net):
            if name.casefold() in {"0v", "0vdc", "m"}:
                return name
    return ""


def _wire_for_name(graph: Graph, name: str) -> str:
    for net in graph.nets.values():
        if any(item.casefold() == name.casefold() for item in _net_names(net)) or any(
            ep.kind == "rail" and ep.ref.casefold() == name.casefold() for ep in net.endpoints
        ):
            number = _first_number(net)
            if number:
                return number
    return ""


def _schematic_devices(graph: Graph) -> list[Placed]:
    rows: list[Placed] = []
    for placed in graph.devices.values():
        if placed.device is None or is_offpage_component(placed.comp, placed.device):
            continue
        rows.append(placed)
    return rows


def _known_wires(project: Project) -> set[str]:
    found: set[str] = set()
    for sheet in project.sheets:
        for wire in sheet.wires:
            number = str(wire.number or "").strip()
            if number:
                found.add(number)
    return found


def citation_errors(project: Project, payload: Any, catalog: Catalog | None = None) -> list[str]:
    """Wire numbers and terminal ids in *payload* that are not on the drawing."""

    wires = _known_wires(project)
    pins = _pins_from_catalog(project, catalog) if catalog is not None else _pins_from_wires(project)
    errors: list[str] = []
    _walk_citations(payload, wires, pins, errors)
    return errors


def _pins_from_catalog(project: Project, catalog: Catalog) -> dict[str, set[str]]:
    pins: dict[str, set[str]] = defaultdict(set)
    for sheet in project.sheets:
        for comp in sheet.components:
            device = _device(catalog, comp)
            if device is None:
                continue
            for pin in device.pins:
                pins[comp.id.casefold()].add(pin.id.casefold())
    return pins


def _pins_from_wires(project: Project) -> dict[str, set[str]]:
    pins: dict[str, set[str]] = defaultdict(set)
    for sheet in project.sheets:
        for wire in sheet.wires:
            for raw in (wire.frm, wire.to):
                ep = _endpoint(sheet, raw)
                if ep is not None and ep.kind == "pin":
                    pins[ep.ref.casefold()].add(ep.pin.casefold())
    return pins


def _walk_citations(payload: Any, wires: set[str], pins: dict[str, set[str]], errors: list[str]) -> None:
    if isinstance(payload, dict):
        wire = payload.get("wire")
        if isinstance(wire, str) and wire and wire not in wires:
            errors.append(f"wire {wire}")
        for number in payload.get("wires") or []:
            if isinstance(number, str) and number and number not in wires:
                errors.append(f"wire {number}")
        device = str(payload.get("device") or payload.get("ref") or "")
        terminal = payload.get("terminal")
        if isinstance(terminal, str) and terminal and device:
            if terminal.casefold() not in pins.get(device.casefold(), set()):
                errors.append(f"{device} terminal {terminal}")
        for pin in payload.get("terminals") or []:
            if isinstance(pin, str) and pin and device and pin.casefold() not in pins.get(device.casefold(), set()):
                # Rails and bare supply names have no pin table; skip those.
                if device.casefold() in pins:
                    errors.append(f"{device} terminal {pin}")
        for value in payload.values():
            if isinstance(value, str):
                _cite_text(value, wires, pins, errors)
            else:
                _walk_citations(value, wires, pins, errors)
    elif isinstance(payload, list):
        for item in payload:
            _walk_citations(item, wires, pins, errors)


def _cite_text(text: str, wires: set[str], pins: dict[str, set[str]], errors: list[str]) -> None:
    for match in _WIRE_CITE.finditer(text):
        number = match.group(1).rstrip(".,;")
        if number.casefold() in {"number", "numbers"}:
            continue
        if number not in wires:
            errors.append(f"wire {number}")
    for match in _TERMINAL_PAIR.finditer(text):
        _cite_pin(match.group(1), match.group(2), pins, errors)
        _cite_pin(match.group(1), match.group(3), pins, errors)
    for match in _TERMINAL_ONE.finditer(text):
        _cite_pin(match.group(1), match.group(2), pins, errors)


def _cite_pin(ref: str, pin: str, pins: dict[str, set[str]], errors: list[str]) -> None:
    have = pins.get(ref.casefold())
    if have is None:
        return
    if pin.casefold() not in have:
        errors.append(f"{ref} terminal {pin}")


def _prose(payload: Any) -> Iterable[str]:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in _PROSE_KEYS and isinstance(value, str):
                yield value
            elif key == "prohibited" and isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        yield item
            else:
                yield from _prose(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _prose(item)


def _symptom_error(raw: str) -> str:
    names = ", ".join(SYMPTOMS)
    if raw:
        return f"unknown symptom {raw!r}. Expected one of: {names}"
    return f"symptom is required. Expected one of: {names}"


def _read_log(path: Path, project_name: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "format": LOG_FORMAT,
            "schema": LOG_SCHEMA,
            "project_file": project_name,
            "note": "Readings log. Not part of the project file.",
            "entries": [],
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QueryError(f"troubleshooting log {path.name} is not readable") from exc
    if not isinstance(data, dict) or data.get("format") != LOG_FORMAT:
        raise QueryError(f"troubleshooting log {path.name} is not a {LOG_FORMAT} file")
    if not isinstance(data.get("entries"), list):
        data["entries"] = []
    return data


def _unique(items: list[str]) -> list[str]:
    found: list[str] = []
    for item in items:
        if item and item not in found:
            found.append(item)
    return found
