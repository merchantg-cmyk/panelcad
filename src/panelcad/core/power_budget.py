"""Control power budget.

Trace loads back to a 24 V DC supply or a control-transformer secondary
through wire endpoints, rail names, and closed contact / fuse poles.
Coils, PLC power pins, lamps, and transformer windings stay open so the
two rails are not shorted together.

Published consumption comes from the part library. Anything else is an
estimate the user can replace. Estimates are labeled. They are not findings.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from panelcad.core.models import Component, Device, Project, Sheet
from panelcad.library.catalog import Catalog

# Schneider 9070 Type T, stable supply, 90% secondary voltage, 20% power factor.
# 200 VA is lower than 150 VA on the published chart. Keep the published number.
SCHNEIDER_9070_90_PF20: dict[int, float] = {
    50: 266,
    75: 396,
    100: 499,
    150: 893,
    200: 815,
    250: 1910,
    300: 2184,
    350: 2592,
    500: 4104,
    750: 5515,
    1000: 9079,
    1500: 23983,
    2000: 38777,
    3000: 39934,
    5000: 85265,
}

METHOD_SCHNEIDER = "schneider_9070_type_t"
MARGIN_KEY = "control_power_margin"
DEFAULT_MARGIN = 0.80

# PanelCAD defaults. Not manufacturer ratings.
_ESTIMATE_W = {
    "coil": 5.0,
    "relay": 1.2,
    "plc_cpu": 6.0,
    "plc_io": 2.0,
    "pilot": 1.0,
    "hmi": 15.0,
    "sensor": 0.5,
    "fan": 8.0,
}
_ESTIMATE_AC_VA = 10.0
_ESTIMATE_AC_W = 3.0

_CONTACT_PAIRS = (
    ("13", "14"),
    ("23", "24"),
    ("33", "34"),
    ("43", "44"),
    ("53", "54"),
    ("11", "12"),
    ("21", "22"),
    ("31", "32"),
    ("41", "42"),
    ("11", "14"),
    ("21", "24"),
    ("31", "34"),
    ("41", "44"),
    ("1", "2"),
    ("3", "4"),
    ("5", "6"),
    ("7", "8"),
    ("95", "96"),
    ("97", "98"),
    ("15", "18"),
    ("15", "16"),
)
_POLE_PAIRS = (("1", "2"), ("3", "4"), ("5", "6"))

_VOLT_RE = re.compile(r"([0-9]+(?:[.][0-9]+)?)\s*V\s*(AC|DC)?", re.I)
_AMP_RE = re.compile(r"([0-9]+(?:[.][0-9]+)?)\s*A\b", re.I)
_HTTP_RE = re.compile(r"^https?://", re.I)

_SCHEMATIC_KINDS = frozenset({"schematic", "io"})
_COIL_KINDS = frozenset({"coil", "relay"})


@dataclass
class LoadLine:
    ref: str
    sheet: str
    kind: str
    steady_w: float
    steady_va: float
    inrush_va: float
    current_a: float
    estimate: bool
    source: str = ""
    catalog: str = ""
    note: str = ""
    inrush_published: bool = False
    inrush_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "sheet": self.sheet,
            "kind": self.kind,
            "steady_w": _round(self.steady_w),
            "steady_va": _round(self.steady_va),
            "inrush_va": _round(self.inrush_va),
            "inrush_published": self.inrush_published,
            "inrush_note": self.inrush_note,
            "current_a": _round(self.current_a),
            "estimate": self.estimate,
            "source": self.source,
            "catalog": self.catalog,
            "note": self.note,
        }


@dataclass
class PowerFinding:
    check: str
    severity: str
    message: str
    sheet: str
    ref: str
    x: float | None = None
    y: float | None = None
    target: str = ""


@dataclass
class SupplyBudget:
    ref: str
    sheet: str
    kind: str
    voltage: float
    rating_w: float | None
    rating_a: float | None
    rating_va: float | None
    steady_w: float
    steady_va: float
    steady_a: float
    inrush_va: float
    margin: float
    limit_w: float | None
    limit_va: float | None
    inrush_limit_va: float | None
    method: str
    method_note: str
    ok_margin: bool
    ok_inrush: bool
    ok_protection: bool
    method_missing: bool
    catalog: str
    source: str
    loads: list[LoadLine] = field(default_factory=list)
    protection: list[dict[str, Any]] = field(default_factory=list)
    protection_note: str = ""
    boost_note: str = ""
    suggestion: dict[str, Any] | None = None
    findings: list[PowerFinding] = field(default_factory=list)
    x: float | None = None
    y: float | None = None

    def to_dict(self) -> dict[str, Any]:
        warnings = [item.message for item in self.findings]
        return {
            "ref": self.ref,
            "sheet": self.sheet,
            "kind": self.kind,
            "voltage": _round(self.voltage),
            "rating_w": _none_round(self.rating_w),
            "rating_a": _none_round(self.rating_a),
            "rating_va": _none_round(self.rating_va),
            "steady_w": _round(self.steady_w),
            "steady_va": _round(self.steady_va),
            "steady_a": _round(self.steady_a),
            "inrush_va": _round(self.inrush_va),
            "margin": self.margin,
            "limit_w": _none_round(self.limit_w),
            "limit_va": _none_round(self.limit_va),
            "inrush_limit_va": _none_round(self.inrush_limit_va),
            "loading": _loading(self),
            "method": self.method,
            "method_note": self.method_note,
            "ok_margin": self.ok_margin,
            "ok_inrush": self.ok_inrush,
            "ok_protection": self.ok_protection,
            "method_missing": self.method_missing,
            "catalog": self.catalog,
            "source": self.source,
            "utilization": utilization_text(self),
            "loads": [row.to_dict() for row in self.loads],
            "protection": self.protection,
            "protection_note": self.protection_note,
            "boost_note": self.boost_note,
            "suggestion": self.suggestion,
            "warnings": warnings,
            "x": self.x,
            "y": self.y,
        }


def margin_of(project: Project, override: float | None = None) -> float:
    """Fraction of nameplate allowed for steady load. 0.80 means 80%."""

    if override is None:
        raw = project.properties.get(MARGIN_KEY, DEFAULT_MARGIN)
    else:
        raw = override
    try:
        number = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("control power margin must be a number") from exc
    if number > 1.5:
        number = number / 100.0
    if number <= 0 or number > 1:
        raise ValueError("control power margin must be between 0 and 1 (or 1 and 100 percent)")
    return number


def report_dict(
    project: Project,
    catalog: Catalog,
    parts: Any = None,
    *,
    supply: str | None = None,
    margin: float | None = None,
) -> dict[str, Any]:
    rows = analyze(project, catalog, parts, supply=supply, margin=margin)
    failed = any((not row.ok_margin) or (not row.ok_inrush) or (not row.ok_protection) for row in rows)
    warnings = [item.message for row in rows for item in row.findings]
    return {
        "margin": margin_of(project, margin),
        "supplies": [row.to_dict() for row in rows],
        "warnings": warnings,
        "failed": failed,
        "estimate_loads": [
            {"supply": row.ref, "ref": load.ref, "sheet": load.sheet, "kind": load.kind, "steady_w": load.steady_w}
            for row in rows
            for load in row.loads
            if load.estimate
        ],
    }


def control_power_findings(
    project: Project,
    catalog: Catalog,
    parts: Any = None,
) -> list[PowerFinding]:
    found: list[PowerFinding] = []
    for row in analyze(project, catalog, parts):
        found.extend(row.findings)
    return found


def csv_rows(budgets: Iterable[SupplyBudget]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for supply in budgets:
        if not supply.loads:
            rows.append(_csv_supply(supply, None))
        for load in supply.loads:
            rows.append(_csv_supply(supply, load))
    return rows


class _UF:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, node: str) -> str:
        self.parent.setdefault(node, node)
        return node

    def find(self, node: str) -> str:
        self.add(node)
        while self.parent[node] != node:
            self.parent[node] = self.parent[self.parent[node]]
            node = self.parent[node]
        return node

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[b] = a


@dataclass
class _Pin:
    sheet: str
    ref: str
    pin: str
    node: str
    wired: bool


@dataclass
class _SupplyNode:
    ref: str
    sheet: str
    kind: str
    voltage: float
    hot: str
    ret: str
    comp: Component
    device: Device
    part: Any
    loads: list[LoadLine] = field(default_factory=list)


@dataclass
class _LoadNode:
    ref: str
    sheet: str
    kind: str
    hot: str
    ret: str
    comp: Component
    device: Device
    part: Any
    supply: _SupplyNode | None = None


@dataclass
class _Edge:
    left: str
    right: str
    kind: str
    device: tuple[str, str] | None = None


class _Graph:
    def __init__(self) -> None:
        self.uf = _UF()
        self.edges: list[_Edge] = []

    @classmethod
    def build(cls, project: Project, catalog: Catalog) -> _Graph:
        graph = cls()
        for sheet in project.sheets:
            if sheet.kind not in _SCHEMATIC_KINDS:
                continue
            for wire in sheet.wires:
                graph.uf.union(_end_node(sheet, wire.frm), _end_node(sheet, wire.to))
            for comp in sheet.components:
                device = catalog.devices.get(comp.library_id)
                if device is None:
                    continue
                _add_passthrough(graph, sheet, comp, device)
                for left, right, kind in _conducting_pairs(device):
                    graph.edges.append(
                        _Edge(
                            _pin_node(sheet, comp.id, left),
                            _pin_node(sheet, comp.id, right),
                            kind,
                            (sheet.id, comp.id) if kind == "protection" else None,
                        )
                    )
                _add_plc_source(graph, sheet, comp, device)
        return graph

    def root(self, node: str) -> str:
        return self.uf.find(node)

    def distances(self, start: str, *, blocked: tuple[str, str] | None = None) -> dict[str, int]:
        adj: dict[str, list[str]] = defaultdict(list)
        for edge in self.edges:
            if blocked is not None and edge.kind == "protection" and edge.device == blocked:
                continue
            left, right = self.root(edge.left), self.root(edge.right)
            if left == right:
                continue
            adj[left].append(right)
            adj[right].append(left)
        origin = self.root(start)
        dist = {origin: 0}
        queue: deque[str] = deque([origin])
        while queue:
            node = queue.popleft()
            for nxt in adj[node]:
                if nxt in dist:
                    continue
                dist[nxt] = dist[node] + 1
                queue.append(nxt)
        return dist


def _supplies(project: Project, catalog: Catalog, parts: Any, graph: _Graph) -> list[_SupplyNode]:
    found: list[_SupplyNode] = []
    for sheet in project.sheets:
        if sheet.kind not in _SCHEMATIC_KINDS:
            continue
        for comp in sheet.components:
            device = catalog.devices.get(comp.library_id)
            if device is None:
                continue
            spec = _supply_spec(device)
            if spec is None:
                continue
            kind, hot_pin, ret_pin = spec
            if not (_pin_wired(sheet, comp.id, hot_pin) or _pin_wired(sheet, comp.id, ret_pin)):
                continue
            part = _assigned_part(comp, parts)
            props = _merged(device, comp)
            voltage, tagged = _supply_voltage(device, props, part, kind)
            if kind == "dc" and tagged == "AC":
                kind = "ac"
            if kind == "ac" and tagged == "DC" and device.category.startswith("power.supply"):
                kind = "dc"
            found.append(
                _SupplyNode(
                    ref=comp.id,
                    sheet=sheet.id,
                    kind=kind,
                    voltage=voltage,
                    hot=graph.root(_pin_node(sheet, comp.id, hot_pin)),
                    ret=graph.root(_pin_node(sheet, comp.id, ret_pin)),
                    comp=comp,
                    device=device,
                    part=part,
                )
            )
    return found


def _loads(project: Project, catalog: Catalog, parts: Any, graph: _Graph) -> list[_LoadNode]:
    found: list[_LoadNode] = []
    for sheet in project.sheets:
        if sheet.kind not in _SCHEMATIC_KINDS:
            continue
        for comp in sheet.components:
            device = catalog.devices.get(comp.library_id)
            if device is None:
                continue
            spec = _load_spec(device)
            if spec is None:
                continue
            kind, hot_pin, ret_pin = spec
            if not (_pin_wired(sheet, comp.id, hot_pin) and _pin_wired(sheet, comp.id, ret_pin)):
                continue
            found.append(
                _LoadNode(
                    ref=comp.id,
                    sheet=sheet.id,
                    kind=kind,
                    hot=graph.root(_pin_node(sheet, comp.id, hot_pin)),
                    ret=graph.root(_pin_node(sheet, comp.id, ret_pin)),
                    comp=comp,
                    device=device,
                    part=_assigned_part(comp, parts),
                )
            )
    return found


def _findings(budget: SupplyBudget) -> list[PowerFinding]:
    found: list[PowerFinding] = []
    pct = int(round(budget.margin * 100))
    if not budget.ok_margin:
        if budget.kind == "dc":
            rating = _fmt(budget.rating_w, "W")
            limit = _fmt(budget.limit_w, "W")
            load = _fmt(budget.steady_w, "W")
        else:
            rating = _fmt(budget.rating_va, "VA")
            limit = _fmt(budget.limit_va, "VA")
            load = _fmt(budget.steady_va, "VA")
        next_bit = _suggestion_text(budget.suggestion)
        found.append(
            _finding(
                budget,
                "control_power_margin",
                f"{budget.ref} steady load {load} exceeds {pct}% of {rating} (limit {limit}).{next_bit}",
            )
        )
    if not budget.ok_inrush:
        next_bit = _suggestion_text(budget.suggestion)
        found.append(
            _finding(
                budget,
                "control_power_inrush",
                (
                    f"{budget.ref} simultaneous inrush {_fmt(budget.inrush_va, 'VA')} exceeds "
                    f"{budget.method_note or 'the transformer inrush rating'} "
                    f"({_fmt(budget.inrush_limit_va, 'VA')}).{next_bit}"
                ),
                target="inrush",
            )
        )
    if budget.method_missing:
        if budget.method:
            detail = budget.method_note or "the published chart has no row for this VA"
            text = f"{budget.ref} inrush method does not cover this rating ({detail})."
        else:
            text = (
                f"{budget.ref} has {_fmt(budget.inrush_va, 'VA')} of AC coil inrush and no published "
                "transformer inrush method. Nameplate VA is not an inrush rating."
            )
        found.append(_finding(budget, "control_power_method", text, target="method"))
    for row in budget.protection:
        if row.get("ok"):
            continue
        found.append(
            _finding(
                budget,
                "control_power_protection",
                str(row.get("message") or ""),
                target=str(row.get("ref") or ""),
            )
        )
    return found


def _finding(budget: SupplyBudget, check: str, message: str, target: str = "") -> PowerFinding:
    return PowerFinding(
        check=check,
        severity="warning",
        message=message,
        sheet=budget.sheet,
        ref=budget.ref,
        x=budget.x,
        y=budget.y,
        target=target,
    )


def _consumption(load: _LoadNode, supply: _SupplyNode) -> LoadLine:
    props = _merged(load.device, load.comp)
    origin = str(props.get("power_origin") or "").strip().casefold()
    part = load.part
    voltage = supply.voltage or 24.0
    estimate = False
    source = ""
    note = ""
    catalog_no = ""
    power_w = _num(props.get("power_w"))
    current = _num(props.get("current_a"))
    sealed = _num(props.get("coil_va_sealed"))
    inrush = _num(props.get("coil_va_inrush"))
    if part is not None:
        catalog_no = part.catalog
        if origin != "estimate":
            if power_w is None:
                power_w = part.power_w
            if current is None:
                current = part.current_a
            if sealed is None:
                sealed = part.coil_va_sealed
            if inrush is None:
                inrush = part.coil_va_inrush
            if origin == "catalog" or _part_has_consumption(part):
                origin = "catalog"
                source = str(part.power_source or part.source or props.get("power_source") or "")
    if origin == "estimate":
        estimate = True
        note = "estimate"
        source = ""
    elif origin == "catalog" and (power_w is not None or current is not None or sealed is not None):
        estimate = False
        source = source or str(props.get("power_source") or "")
    else:
        device_source = str(load.device.properties.get("power_source") or "")
        if _HTTP_RE.match(device_source) and (power_w is not None or current is not None or sealed is not None):
            estimate = False
            source = device_source
            origin = "catalog"
        else:
            estimate = True
            note = "estimate"
            power_w = power_w if origin == "estimate" else None
            current = current if origin == "estimate" else None
            sealed = None
            inrush = None
            if str(props.get("power_origin") or "").strip().casefold() == "estimate":
                power_w = _num(props.get("power_w"))
                current = _num(props.get("current_a"))
                sealed = _num(props.get("coil_va_sealed"))
                inrush = _num(props.get("coil_va_inrush"))

    if not estimate and supply.kind == "dc" and power_w is None and current is None:
        estimate = True
        source = ""
        note = "estimate"
        sealed = None
        inrush = None

    if estimate:
        note = "estimate"
        power_w, current, sealed, inrush = _fill_estimate(
            load.kind, supply.kind, power_w, current, sealed, inrush, voltage
        )
    else:
        if supply.kind == "dc":
            sealed = 0.0
            inrush = 0.0
            if power_w is None and current is not None:
                power_w = current * voltage
            if current is None and power_w is not None and voltage:
                current = power_w / voltage
        else:
            if load.kind in _COIL_KINDS:
                if sealed is None and power_w is not None:
                    sealed = power_w
                if current is None and sealed is not None and voltage:
                    current = sealed / voltage
                if power_w is None and sealed is not None:
                    power_w = sealed
            else:
                if power_w is None and current is not None:
                    power_w = current * voltage
                if current is None and power_w is not None and voltage:
                    current = power_w / voltage
                sealed = power_w or 0.0
                inrush = 0.0
    published = inrush is not None and supply.kind == "ac" and load.kind in _COIL_KINDS
    if supply.kind == "dc":
        steady_va = power_w or 0.0
        inrush_va = 0.0
        published = False
    else:
        steady_va = sealed or power_w or 0.0
        inrush_va = float(inrush) if published else 0.0
    return LoadLine(
        ref=load.ref,
        sheet=load.sheet,
        kind=load.kind,
        steady_w=float(power_w or 0.0),
        steady_va=float(steady_va or 0.0),
        inrush_va=float(inrush_va or 0.0),
        current_a=float(current or 0.0),
        estimate=estimate,
        source=source,
        catalog=catalog_no,
        note=note,
        inrush_published=published,
        inrush_note="" if published else "not published",
    )


def _fill_estimate(
    kind: str,
    supply_kind: str,
    power_w: float | None,
    current: float | None,
    sealed: float | None,
    inrush: float | None,
    voltage: float,
) -> tuple[float, float, float, float | None]:
    if kind in _COIL_KINDS and supply_kind == "ac":
        sealed_v = sealed if sealed is not None else _ESTIMATE_AC_VA
        watts = power_w if power_w is not None else (_ESTIMATE_AC_W if kind == "coil" else _ESTIMATE_W["relay"])
        amps = current if current is not None else (sealed_v / voltage if voltage else 0.0)
        return watts, amps, sealed_v, inrush
    if power_w is None and current is not None and voltage:
        power_w = current * voltage
    if power_w is None:
        power_w = _ESTIMATE_W.get(kind, 1.0)
    if current is None and voltage:
        current = power_w / voltage
    if supply_kind == "ac":
        return power_w, current or 0.0, power_w, 0.0
    return power_w, current or 0.0, 0.0, 0.0


def _supply_rating(supply: _SupplyNode) -> tuple[float | None, float | None, float | None, str, str, str]:
    props = _merged(supply.device, supply.comp)
    part = supply.part
    power_w = _num(props.get("power_w"))
    current = _num(props.get("current_a"))
    if current is None:
        current = _num(props.get("amps"))
    va = _num(props.get("va"))
    if va is None:
        va = _num(props.get("VA"))
    method = str(props.get("inrush_method") or "").strip()
    source = str(props.get("power_source") or props.get("catalog_source") or "")
    catalog_no = str(props.get("catalog") or props.get("part_number") or "")
    if part is not None:
        if power_w is None:
            power_w = part.power_w
        if current is None:
            current = part.current_a if part.current_a is not None else part.amps
        if va is None:
            va = part.va
        if not method:
            method = str(part.inrush_method or "")
        if not source:
            source = str(part.power_source or part.source or "")
        if not catalog_no:
            catalog_no = part.catalog
    if current is None:
        current = _first_amp(props.get("Iout"), props.get("In"), props.get("amps"))
    if va is None:
        va = _num(supply.device.properties.get("VA"))
    voltage = supply.voltage or 0.0
    if supply.kind == "dc":
        if power_w is None and current is not None and voltage:
            power_w = current * voltage
        if current is None and power_w is not None and voltage:
            current = power_w / voltage
        return power_w, current, None, "", source, catalog_no
    if va is None and power_w is not None:
        va = power_w
    return None, current, va, method, source, catalog_no


def _inrush_limit(method: str, rating_va: float | None) -> tuple[float | None, str]:
    if method != METHOD_SCHNEIDER or rating_va is None:
        return None, ""
    key = int(round(rating_va))
    limit = SCHNEIDER_9070_90_PF20.get(key)
    note = "Schneider 9070 Type T, 90% secondary, 20% power factor"
    if limit is None:
        return None, note + f" (no chart row for {key} VA)"
    return float(limit), note


def _suggest(
    supply: _SupplyNode,
    parts: Any,
    steady_w: float,
    steady_va: float,
    steady_a: float,
    inrush: float,
    rating_a: float | None,
    rating_va: float | None,
) -> dict[str, Any] | None:
    if parts is None:
        return None
    if supply.kind == "dc":
        return _suggest_psu(parts, supply.voltage, rating_a, steady_w, steady_a)
    return _suggest_transformer(parts, rating_va, steady_va, inrush)


def _suggest_psu(
    parts: Any,
    voltage: float,
    rating_a: float | None,
    steady_w: float,
    steady_a: float,
) -> dict[str, Any] | None:
    best = None
    best_amps = None
    for part in parts.parts.values():
        if not str(part.category or "").startswith("power.supply"):
            continue
        volts, _kind = _parse_volts(part.description)
        if volts is None or abs(volts - voltage) > 6:
            continue
        amps = part.current_a if part.current_a is not None else part.amps
        watts = part.power_w
        if amps is None and watts is not None and voltage:
            amps = watts / voltage
        if amps is None:
            continue
        if rating_a is not None and amps <= rating_a + 1e-9:
            continue
        if watts is not None and watts + 1e-9 < steady_w:
            continue
        if amps + 1e-9 < steady_a:
            continue
        if best_amps is None or amps < best_amps:
            best = part
            best_amps = amps
    if best is None:
        return None
    return {
        "manufacturer": best.manufacturer,
        "catalog": best.catalog,
        "power_w": best.power_w,
        "current_a": best.current_a if best.current_a is not None else best.amps,
        "source": best.source,
        "reason": "next supply in the library above this rating",
    }


def _suggest_transformer(
    parts: Any,
    rating_va: float | None,
    steady_va: float,
    inrush: float,
) -> dict[str, Any] | None:
    cataloged = []
    for part in parts.parts.values():
        if part.inrush_method != METHOD_SCHNEIDER or part.va is None:
            continue
        limit = SCHNEIDER_9070_90_PF20.get(int(round(part.va)))
        cataloged.append((float(part.va), limit, part))
    cataloged.sort(key=lambda row: row[0])
    current = rating_va or 0.0
    for va, limit, part in cataloged:
        if va <= current + 1e-9:
            continue
        if limit is None or limit + 1e-9 < inrush:
            continue
        if va + 1e-9 < steady_va:
            continue
        return {
            "manufacturer": part.manufacturer,
            "catalog": part.catalog,
            "va": part.va,
            "inrush_va": limit,
            "source": part.source,
            "reason": "next Type T transformer whose chart covers this inrush",
        }
    for va in sorted(SCHNEIDER_9070_90_PF20):
        limit = SCHNEIDER_9070_90_PF20[va]
        if va <= current + 1e-9:
            continue
        if limit + 1e-9 < inrush or va + 1e-9 < steady_va:
            continue
        return {
            "manufacturer": "",
            "catalog": "",
            "va": va,
            "inrush_va": limit,
            "source": "",
            "reason": f"next 9070 Type T chart step is {va} VA ({limit:g} VA inrush); no matching SKU in the library",
        }
    return None


def _suggestion_text(suggestion: dict[str, Any] | None) -> str:
    if not suggestion:
        return ""
    catalog = str(suggestion.get("catalog") or "").strip()
    maker = str(suggestion.get("manufacturer") or "").strip()
    if catalog:
        bits = f" Next: {maker} {catalog}".rstrip()
        if suggestion.get("power_w"):
            bits += f" ({_fmt(float(suggestion['power_w']), 'W')}"
            if suggestion.get("current_a"):
                bits += f", {_fmt(float(suggestion['current_a']), 'A')}"
            bits += ")"
        elif suggestion.get("va"):
            bits += f" ({_fmt(float(suggestion['va']), 'VA')}"
            if suggestion.get("inrush_va"):
                bits += f", chart {_fmt(float(suggestion['inrush_va']), 'VA')}"
            bits += ")"
        return bits + "."
    reason = str(suggestion.get("reason") or "").strip()
    return f" {reason}." if reason else ""


def _supply_spec(device: Device) -> tuple[str, str, str] | None:
    ids = {pin.id for pin in device.pins}
    cat = device.category or ""
    lib = device.library_id.lower()
    if cat.startswith("power.transformer") or "transformer" in lib:
        hot = "X1" if "X1" in ids else ("21" if "21" in ids else "")
        ret = "X2" if "X2" in ids else ("22" if "22" in ids else "")
        if hot and ret:
            return "ac", hot, ret
        return None
    if cat.startswith("power.supply") or "psu" in lib:
        hot = _first_id(ids, ("plus", "+", "L+", "Lplus", "24V"))
        ret = _first_id(ids, ("minus", "M", "0V", "-"))
        if hot and ret:
            return "dc", hot, ret
    return None


def _load_spec(device: Device) -> tuple[str, str, str] | None:
    ids = {pin.id for pin in device.pins}
    cat = (device.category or "").lower()
    lib = device.library_id.lower()
    tags = {str(tag).lower() for tag in device.tags}
    if cat.startswith("power.") or cat.startswith("drive.") or cat.startswith("load.motor"):
        return None
    if "A1" in ids and "A2" in ids and (
        cat.startswith("switching.contactor")
        or cat.startswith("switching.relay")
        or cat.startswith("switching.safety")
        or cat.startswith("timing.")
        or "coil" in lib
        or "coil" in tags
    ):
        kind = "relay" if ("relay" in cat or "relay" in lib or "relay" in tags) else "coil"
        return kind, "A1", "A2"
    hot = _first_id(ids, ("Lplus", "L+", "+", "24V"))
    ret = _first_id(ids, ("M", "0V", "minus", "-"))
    if hot and ret and cat.startswith("control.plc"):
        cpu = "cpu" in tags or "cpu" in lib or "compact" in lib
        io = any(tag in tags for tag in ("input", "output")) or any(
            token in lib for token in (".di", ".do", "di16", "do16", "do12")
        )
        if cpu and not (io and "cpu" not in tags and "cpu" not in lib):
            return "plc_cpu", hot, ret
        return "plc_io", hot, ret
    if hot and ret and cat.startswith("control.hmi"):
        return "hmi", hot, ret
    if hot and ret and cat.startswith("sensor."):
        return "sensor", hot, ret
    if hot and ret and (cat.startswith("load.fan") or cat.startswith("power.fan") or "fan" in tags):
        return "fan", hot, ret
    if "X1" in ids and "X2" in ids and (cat.startswith("operator.light") or "pilot" in lib or "light" in tags):
        return "pilot", "X1", "X2"
    return None


def _conducting_pairs(device: Device) -> list[tuple[str, str, str]]:
    ids = {pin.id for pin in device.pins}
    cat = (device.category or "").lower()
    lib = device.library_id.lower()
    if cat.startswith("power.") or cat.startswith("drive.") or "transformer" in lib or "psu" in lib:
        return []
    if cat.startswith("operator.light") or cat.startswith("control.") or cat.startswith("sensor.") or cat.startswith("load."):
        return []
    if "selector" in cat or "selector" in lib:
        return [(left, right, "contact") for left, right in _selector_pairs(device)]
    if cat.startswith("protection.fuse") or cat.startswith("protection.breaker") or ".fuse" in lib:
        rows = []
        for left, right in _POLE_PAIRS:
            if left in ids and right in ids:
                rows.append((left, right, "protection"))
        if not rows and len(device.pins) == 2:
            a, b = device.pins
            rows.append((a.id, b.id, "protection"))
        return rows
    contact = (
        cat.startswith("operator.")
        or cat.startswith("switching.aux")
        or cat.startswith("switching.relay")
        or cat.startswith("timing.")
        or cat.startswith("protection.overload")
        or "aux" in lib
        or "contact" in lib
    )
    if not contact:
        return []
    rows = []
    for left, right in _CONTACT_PAIRS:
        if left in ids and right in ids:
            rows.append((left, right, "contact"))
    if cat.startswith("timing.") and "1" in ids and "3" in ids:
        rows.append(("1", "3", "contact"))
    return rows


def _selector_pairs(device: Device) -> list[tuple[str, str]]:
    pins = list(device.pins)
    left = [pin for pin in pins if pin.side == "left"]
    if len(left) == 1:
        common = left[0].id
    elif any(pin.id == "11" for pin in pins):
        common = "11"
    elif any(pin.id == "1" for pin in pins):
        common = "1"
    else:
        return []
    return [(common, pin.id) for pin in pins if pin.id != common]


def _add_plc_source(graph: _Graph, sheet: Sheet, comp: Component, device: Device) -> None:
    """A sourcing output delivers supply current. The output pin is fed from L+."""

    if not (device.category or "").startswith("control.plc"):
        return
    ids = {pin.id for pin in device.pins}
    hot = _first_id(ids, ("Lplus", "L+", "+", "24V"))
    if not hot:
        return
    for pin in device.pins:
        if pin.id == hot:
            continue
        if pin.electrical == "output" or _output_pin(pin.id):
            graph.edges.append(
                _Edge(_pin_node(sheet, comp.id, hot), _pin_node(sheet, comp.id, pin.id), "plc_source")
            )


def _output_pin(pin_id: str) -> bool:
    token = pin_id.upper()
    return token.startswith("Q") or token.startswith("Y")


def _add_passthrough(graph: _Graph, sheet: Sheet, comp: Component, device: Device) -> None:
    cat = (device.category or "").lower()
    if not (cat.startswith("connection.terminal") or cat.startswith("terminal.")):
        return
    if len(device.pins) != 2:
        return
    left, right = device.pins
    graph.uf.union(_pin_node(sheet, comp.id, left.id), _pin_node(sheet, comp.id, right.id))


def _end_node(sheet: Sheet, end: str) -> str:
    text = str(end or "").strip()
    if "." not in text:
        return f"name:{text.upper()}"
    ref, pin = text.split(".", 1)
    return _pin_node(sheet, ref, pin)


def _pin_node(sheet: Sheet, ref: str, pin: str) -> str:
    return f"pin:{sheet.id}:{ref}:{pin}"


def _pin_wired(sheet: Sheet, ref: str, pin: str) -> bool:
    token = f"{ref}.{pin}".upper()
    for wire in sheet.wires:
        if str(wire.frm).strip().upper() == token or str(wire.to).strip().upper() == token:
            return True
    return False


def _assigned_part(comp: Component, parts: Any) -> Any:
    if parts is None:
        return None
    catalog_no = str(comp.properties.get("catalog") or comp.properties.get("part_number") or "").strip()
    if not catalog_no:
        return None
    manufacturer = str(comp.properties.get("manufacturer") or "").strip() or None
    try:
        return parts.get(catalog_no, manufacturer)
    except KeyError:
        return None


def _part_has_consumption(part: Any) -> bool:
    return any(
        getattr(part, name, None) is not None
        for name in ("power_w", "current_a", "coil_va_sealed", "coil_va_inrush")
    )


def _merged(device: Device, comp: Component) -> dict[str, Any]:
    props: dict[str, Any] = dict(device.properties or {})
    props.update(comp.properties or {})
    return props


def _supply_voltage(device: Device, props: dict[str, Any], part: Any, kind: str) -> tuple[float, str]:
    texts = [
        props.get("Vout"),
        props.get("secondary"),
        props.get("voltage"),
        props.get("supply"),
        device.properties.get("Vout"),
        device.properties.get("secondary"),
        part.description if part is not None else "",
    ]
    for text in texts:
        volts, tagged = _parse_volts(text)
        if volts is not None:
            return volts, tagged
    return (24.0 if kind == "dc" else 120.0), ""


def _parse_volts(text: Any) -> tuple[float | None, str]:
    match = _VOLT_RE.search(str(text or ""))
    if not match:
        return None, ""
    return float(match.group(1)), (match.group(2) or "").upper()


def _first_amp(*values: Any) -> float | None:
    for value in values:
        if value is None or value == "":
            continue
        if isinstance(value, (int, float)):
            return float(value)
        match = _AMP_RE.search(str(value))
        if match:
            return float(match.group(1))
    return None


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"[0-9]+(?:[.][0-9]+)?", str(value))
    if not match:
        return None
    return float(match.group(0))


def _first_id(ids: set[str], names: tuple[str, ...]) -> str:
    for name in names:
        if name in ids:
            return name
    return ""


def _loading(budget: SupplyBudget) -> float | None:
    if budget.kind == "dc" and budget.rating_w:
        return _round(budget.steady_w / budget.rating_w)
    if budget.kind == "ac" and budget.rating_va:
        return _round(budget.steady_va / budget.rating_va)
    if budget.rating_a:
        return _round(budget.steady_a / budget.rating_a)
    return None


def utilization_text(budget: SupplyBudget) -> str:
    """Steady load as a percent of nameplate, plus the margin limit."""

    limit = int(round(budget.margin * 100))
    fraction = _loading(budget)
    if fraction is None:
        return ""
    pct = int(round(float(fraction) * 100))
    if budget.kind == "dc" and budget.rating_w:
        return f"{_fmt(budget.steady_w, 'W')} of {_fmt(budget.rating_w, 'W')}, {pct}%; limit {limit}%"
    if budget.kind == "ac" and budget.rating_va:
        return f"{_fmt(budget.steady_va, 'VA')} of {_fmt(budget.rating_va, 'VA')}, {pct}%; limit {limit}%"
    if budget.rating_a:
        return f"{_fmt(budget.steady_a, 'A')} of {_fmt(budget.rating_a, 'A')}, {pct}%; limit {limit}%"
    return ""


def _boost_note(loads: list[LoadLine]) -> str:
    coils = [row for row in loads if row.kind in _COIL_KINDS]
    if not coils or all(row.inrush_published for row in coils):
        return ""
    return "Peak/boost capability was not checked for coil pickup; inrush is not published."


def _fmt(number: float | None, unit: str) -> str:
    if number is None:
        return "—"
    if abs(number) >= 100:
        text = f"{number:.0f}"
    elif abs(number) >= 10:
        text = f"{number:.1f}".rstrip("0").rstrip(".")
    else:
        text = f"{number:.3f}".rstrip("0").rstrip(".")
    return f"{text} {unit}"


def _round(number: float) -> float:
    return round(float(number), 4)


def _none_round(number: float | None) -> float | None:
    if number is None:
        return None
    return round(float(number), 4)


def _csv_supply(supply: SupplyBudget, load: LoadLine | None) -> dict[str, str]:
    suggestion = ""
    if supply.suggestion:
        suggestion = str(supply.suggestion.get("catalog") or supply.suggestion.get("reason") or "")
    return {
        "supply": supply.ref,
        "supply_sheet": supply.sheet,
        "supply_kind": supply.kind,
        "voltage": f"{supply.voltage:g}",
        "rating_w": "" if supply.rating_w is None else f"{supply.rating_w:g}",
        "rating_a": "" if supply.rating_a is None else f"{supply.rating_a:g}",
        "rating_va": "" if supply.rating_va is None else f"{supply.rating_va:g}",
        "steady_w": f"{supply.steady_w:g}",
        "steady_va": f"{supply.steady_va:g}",
        "inrush_va": f"{supply.inrush_va:g}",
        "margin": f"{supply.margin:g}",
        "ok_margin": "yes" if supply.ok_margin else "no",
        "ok_inrush": "yes" if supply.ok_inrush else "no",
        "ok_protection": "yes" if supply.ok_protection else "no",
        "suggestion": suggestion,
        "load": "" if load is None else load.ref,
        "load_sheet": "" if load is None else load.sheet,
        "load_kind": "" if load is None else load.kind,
        "load_w": "" if load is None else f"{load.steady_w:g}",
        "load_va": "" if load is None else f"{load.steady_va:g}",
        "load_inrush_va": "" if load is None else f"{load.inrush_va:g}",
        "estimate": "" if load is None else ("yes" if load.estimate else "no"),
        "source": "" if load is None else load.source,
    }


# Protection is filled in _finish, but it needs the graph. Patch _finish by
# keeping graph roots on the load lines. The helper below is the real check;
# _protection above is replaced after the graph is still in scope.
# 1492-SG122Q-EN-P, July 2023, page 10. Bulletin 1489-M is non-100% rated
# (UL 489 para 7.1.4.2) and should be loaded to no more than 80% on a continuous load.
_1489_CONTINUOUS = 0.80
_CURVE_RE = re.compile(r"1489-M\d([A-Z])\d", re.I)
_CURVE_WORD = re.compile(r"\b([A-Z])-curve\b", re.I)
_TRIP_UNPUBLISHED = "breaker tripping capability not published for this supply; verify"


def _breaker_curve(part: Any) -> str:
    catalog = str(getattr(part, "catalog", "") or "")
    match = _CURVE_RE.search(catalog)
    if match:
        return match.group(1).upper()
    desc = str(getattr(part, "description", "") or "")
    word = _CURVE_WORD.search(desc)
    if word:
        return word.group(1).upper()
    return ""


def _continuous_requirement(part: Any, current_a: float) -> tuple[float, str]:
    """Minimum breaker rating for this steady current, and the cited factor."""

    series = str(getattr(part, "series", "") or "").strip()
    if series.casefold() == "1489-m":
        required = current_a / _1489_CONTINUOUS
        note = (
            f"Needs at least {_fmt(required, 'A')}. Steady current is treated as continuous. "
            "1492-SG122 page 10: 1489-M is non-100% rated (UL 489 para 7.1.4.2) and should be "
            f"loaded to no more than 80%, so the rating must be at least 125% of {_fmt(current_a, 'A')}."
        )
        return required, note
    note = (
        f"Needs at least {_fmt(current_a, 'A')}. "
        "No continuous-load factor is applied; one was not published for this device."
    )
    return current_a, note


def _curve_row(rows: list[Any], curve: str) -> Any:
    for row in rows:
        if str(getattr(row, "curve", "") or "").upper() == curve:
            return row
    return None


def _ohm_bit(loop_mohm: float | None, *, approx: bool) -> str:
    if loop_mohm is None:
        return ""
    mark = "~" if approx else ""
    return f" ({mark}{loop_mohm:g} mΩ)"


def _supply_trip(supply_part: Any, curve: str, required: float) -> tuple[str, float | None]:
    """Citation and the downstream trip ceiling. None means do not suggest a size."""

    catalog = str(getattr(supply_part, "catalog", "") or "").strip()
    trip = getattr(supply_part, "output_trip", None) if supply_part is not None else None
    if trip is None or not curve:
        if not curve and trip is not None:
            name = catalog or "This supply"
            return (
                f"{name}: the breaker has no published trip curve, so it was not matched to "
                f"{trip.publication}. {_TRIP_UNPUBLISHED}",
                None,
            )
        return (f"{catalog}: {_TRIP_UNPUBLISHED}" if catalog else _TRIP_UNPUBLISHED), None
    supply_row = _curve_row(trip.supply_end, curve)
    load_row = _curve_row(trip.load_end, curve)
    where = f"{trip.publication} section {trip.section} page {trip.pages}".replace("  ", " ").strip()
    bits = [f"{catalog}: {where}."]
    if supply_row is not None and supply_row.trips and supply_row.max_amps is not None:
        bits.append(
            f"{curve}-curve trips at ≤ {_fmt(float(supply_row.max_amps), 'A')} "
            f"on a short at the supply{_ohm_bit(supply_row.loop_mohm, approx=True)}."
        )
    elif supply_row is not None and not supply_row.trips:
        bits.append(f"{curve}-curve is not documented to trip at the supply ({supply_row.note}).")
    else:
        bits.append(f"Supply-end {curve}-curve test is not in {trip.publication}.")
    ceiling: float | None = None
    if load_row is not None and load_row.trips and load_row.max_amps is not None:
        ceiling = float(load_row.max_amps)
        bits.append(
            f"{curve}-curve trips at ≤ {_fmt(ceiling, 'A')} at the load end"
            f"{_ohm_bit(load_row.loop_mohm, approx=False)}. "
            "A downstream breaker must be at or below that rating."
        )
        if required > ceiling + 1e-9:
            bits.append(
                f"{_fmt(required, 'A')} is above {_fmt(ceiling, 'A')}, so this supply is not "
                "documented to trip a breaker for this load."
            )
    elif load_row is not None and not load_row.trips:
        bits.append(f"{curve}-curve is not documented to trip at the load end ({load_row.note}).")
    else:
        bits.append(f"Downstream {curve}-curve test is not in {trip.publication}. {_TRIP_UNPUBLISHED}")
    return " ".join(bits), ceiling


def _iter_parts(parts: Any) -> list[Any]:
    if parts is None:
        return []
    pool = getattr(parts, "parts", None)
    if isinstance(pool, dict):
        return list(pool.values())
    return list(parts)


def _sourced_larger(parts: Any, breaker: Any) -> list[Any]:
    series = str(getattr(breaker, "series", "") or "").strip()
    poles = getattr(breaker, "poles", None)
    installed = getattr(breaker, "amps", None)
    rows = []
    for item in _iter_parts(parts):
        if series and str(getattr(item, "series", "") or "").strip().casefold() != series.casefold():
            continue
        if poles is not None and getattr(item, "poles", None) != poles:
            continue
        if not str(getattr(item, "source", "") or "").startswith("http"):
            continue
        amps = getattr(item, "amps", None)
        if amps is None:
            continue
        if installed is not None and float(amps) <= float(installed) + 1e-9:
            continue
        rows.append(item)
    rows.sort(key=lambda item: (float(item.amps), str(item.catalog)))
    return rows


def _breaker_fits(part: Any, curve: str, required: float, ceiling: float | None, ampacity: float | None) -> bool:
    if ceiling is None or _breaker_curve(part) != curve:
        return False
    amps = float(part.amps)
    if amps + 1e-9 < required:
        return False
    if amps > ceiling + 1e-9:
        return False
    if ampacity is not None and amps > ampacity + 1e-9:
        return False
    return True


def _library_sentence(
    parts: Any,
    breaker: Any,
    curve: str,
    required: float,
    ceiling: float | None,
    ampacity: float | None,
) -> tuple[str, dict[str, Any] | None]:
    """Suggest the smallest sourced breaker inside the window, or say why not."""

    series = str(getattr(breaker, "series", "") or "").strip() or "breaker"
    poles = getattr(breaker, "poles", None)
    pole_bit = f"{poles}-pole " if poles else ""
    covering = [item for item in _sourced_larger(parts, breaker) if float(item.amps) + 1e-9 >= required]
    fits = [item for item in covering if _breaker_fits(item, curve, required, ceiling, ampacity)]
    if fits:
        best = fits[0]
        return "", {
            "catalog": best.catalog,
            "manufacturer": best.manufacturer,
            "amps": float(best.amps),
            "source": best.source,
            "series": series,
            "reason": f"next {series} size inside the published trip window",
        }
    if not covering:
        return (
            f"No larger {pole_bit}{series} breaker with a source covers {_fmt(required, 'A')}. "
            "Suggestion is limited to library parts with a source."
        ), None
    nxt = covering[0]
    amps = float(nxt.amps)
    reasons: list[str] = []
    if ceiling is None:
        reasons.append("the supply is not documented to trip a breaker for this load")
    elif amps > ceiling + 1e-9:
        reasons.append(
            f"{_fmt(amps, 'A')} is above the published downstream trip current {_fmt(ceiling, 'A')}"
        )
    if ampacity is not None and amps > ampacity + 1e-9:
        reasons.append(f"{_fmt(amps, 'A')} is above the conductor ampacity {_fmt(ampacity, 'A')}")
    if not curve:
        reasons.append("the installed breaker has no published trip curve")
    elif _breaker_curve(nxt) != curve:
        reasons.append(f"its curve is {_breaker_curve(nxt) or 'unpublished'}, not {curve}")
    because = "; ".join(reasons) if reasons else "it is outside the published window"
    return (
        f"The next {pole_bit}{series} with a source is {nxt.manufacturer} {nxt.catalog} "
        f"({_fmt(amps, 'A')}). {because[0].upper() + because[1:]}. "
        "Suggestion is limited to library parts with a source."
    ), None


def _lookup_supply(parts: Any, suggestion: dict[str, Any] | None) -> Any:
    if not suggestion:
        return None
    catalog = str(suggestion.get("catalog") or "").strip()
    if not catalog or parts is None:
        return None
    manufacturer = str(suggestion.get("manufacturer") or "").strip() or None
    try:
        return parts.get(catalog, manufacturer)
    except KeyError:
        return None


def _conductor_limit(graph: _Graph, supply: _SupplyNode, breaker_key: tuple[str, str]) -> tuple[float | None, str]:
    """Minimum ampacity of the conductors the breaker disconnects from the supply."""

    project = getattr(supply, "_project", None)
    if project is None:
        return None, "Wire gauge is not on this circuit, so conductor ampacity was not applied."
    open_hot = graph.distances(supply.hot)
    blocked = graph.distances(supply.hot, blocked=breaker_key)
    gauges: list[str] = []
    for sheet in project.sheets:
        if sheet.kind not in _SCHEMATIC_KINDS:
            continue
        for wire in sheet.wires:
            left = graph.root(_end_node(sheet, wire.frm))
            right = graph.root(_end_node(sheet, wire.to))
            was = left in open_hot and right in open_hot
            still = left in blocked and right in blocked
            if was and not still:
                gauges.append(str(wire.gauge or "").strip())
    if not gauges or any(not gauge for gauge in gauges):
        return None, "Wire gauge is not on every protected conductor, so conductor ampacity was not applied."
    from panelcad.core.checks import gauge_ampacity

    known: list[tuple[str, float]] = []
    unknown: list[str] = []
    for gauge in gauges:
        cap = gauge_ampacity(gauge)
        if cap is None:
            unknown.append(gauge)
        else:
            known.append((gauge, float(cap)))
    if unknown or not known:
        listed = ", ".join(unknown) if unknown else "unset"
        return None, (
            f"Wire gauge {listed} has no ampacity in the control-panel table, "
            "so conductor ampacity was not applied."
        )
    _gauge, cap = min(known, key=lambda item: item[1])
    shown = ", ".join(f"{gauge} ({_fmt(amps, 'A')})" for gauge, amps in known)
    return cap, (
        f"Protected conductor ampacity {_fmt(cap, 'A')} ({shown}). "
        f"Breaker rating must be ≤ {_fmt(cap, 'A')}."
    )


def _amps_number(number: float) -> str:
    return _fmt(number, "A").removesuffix(" A")


def _downstream_limit(supply_part: Any, curve: str) -> tuple[float | None, str]:
    """Load-end trip ceiling and the publication citation. Ceiling is None when unpublished."""

    trip = getattr(supply_part, "output_trip", None) if supply_part is not None else None
    if trip is None:
        return None, ""
    cite = f"{trip.publication} section {trip.section} page {trip.pages}".replace("  ", " ").strip()
    row = _curve_row(trip.load_end, curve) if curve else None
    if row is None or not row.trips or row.max_amps is None:
        return None, cite
    return float(row.max_amps), cite


def _c_curve_breakers(parts: Any) -> list[Any]:
    """Sourced 1-pole 1489-M C-curve breakers, smallest rating first."""

    rows = []
    for item in _iter_parts(parts):
        if str(getattr(item, "series", "") or "").strip().casefold() != "1489-m":
            continue
        if getattr(item, "poles", None) != 1:
            continue
        if _breaker_curve(item) != "C":
            continue
        if not str(getattr(item, "source", "") or "").startswith("http"):
            continue
        if getattr(item, "amps", None) is None:
            continue
        rows.append(item)
    rows.sort(key=lambda item: (float(item.amps), str(item.catalog)))
    return rows


def _smallest_in_range(parts: Any, low: float, high: float) -> Any | None:
    for item in _c_curve_breakers(parts):
        amps = float(item.amps)
        if amps + 1e-9 >= low and amps <= high + 1e-9:
            return item
    return None


def _fewest_branches(
    loads: list[tuple[str, float]],
    capacity: float,
) -> tuple[list[list[tuple[str, float]]], list[tuple[str, float]]]:
    """Fewest groups whose steady current is within *capacity*. Oversized loads are separate."""

    oversized = [item for item in loads if item[1] > capacity + 1e-9]
    rest = sorted(
        [item for item in loads if item[1] <= capacity + 1e-9],
        key=lambda item: (-item[1], item[0]),
    )
    best: list[list[tuple[str, float]]] | None = None

    def search(index: int, bins: list[list[tuple[str, float]]], sums: list[float]) -> None:
        nonlocal best
        if best is not None and len(bins) >= len(best):
            return
        if index == len(rest):
            best = [list(group) for group in bins]
            return
        ref, amps = rest[index]
        for slot, total in enumerate(sums):
            if total + amps <= capacity + 1e-9:
                bins[slot].append((ref, amps))
                sums[slot] += amps
                search(index + 1, bins, sums)
                bins[slot].pop()
                sums[slot] -= amps
        bins.append([(ref, amps)])
        sums.append(amps)
        search(index + 1, bins, sums)
        bins.pop()
        sums.pop()

    if rest and len(rest) <= 10:
        search(0, [], [])
    elif rest:
        bins = []
        sums = []
        for ref, amps in rest:
            placed = False
            for slot, total in enumerate(sums):
                if total + amps <= capacity + 1e-9:
                    bins[slot].append((ref, amps))
                    sums[slot] += amps
                    placed = True
                    break
            if not placed:
                bins.append([(ref, amps)])
                sums.append(amps)
        best = bins
    groups = best or []
    for group in groups:
        group.sort(key=lambda item: item[0])
    groups.sort(key=lambda group: group[0][0])
    oversized.sort(key=lambda item: item[0])
    return groups, oversized


def _branch_lines(
    parts: Any,
    loads: list[tuple[str, float]],
    supply_part: Any,
    curve: str,
    *,
    role: str,
) -> list[str]:
    ceiling, cite = _downstream_limit(supply_part, curve)
    catalog = str(getattr(supply_part, "catalog", "") or "").strip() or "this supply"
    if ceiling is None or not loads:
        return []
    capacity = ceiling * _1489_CONTINUOUS
    groups, oversized = _fewest_branches(loads, capacity)
    lines = [
        f"On {catalog} ({role}, load-end {curve}-curve ≤ {_fmt(ceiling, 'A')}, {cite}):"
    ]
    for index, group in enumerate(groups, start=1):
        current = sum(amps for _ref, amps in group)
        required = current / _1489_CONTINUOUS
        names = ", ".join(ref for ref, _amps in group)
        part = _smallest_in_range(parts, required, ceiling)
        if part is not None:
            maker = str(getattr(part, "manufacturer", "") or "").strip()
            choice = f"{maker} {part.catalog} ({_fmt(float(part.amps), 'A')})".strip()
        else:
            choice = (
                f"needs {_amps_number(required)}–{_amps_number(ceiling)} A; "
                "no library part with a source in that range"
            )
        lines.append(
            f"Branch {index}: {names}, {_fmt(current, 'A')}, 125% {_fmt(required, 'A')}. {choice}."
        )
    for ref, amps in oversized:
        required = amps / _1489_CONTINUOUS
        lines.append(
            f"{ref}, {_fmt(amps, 'A')}, 125% {_fmt(required, 'A')}, cannot be protected by a "
            f"C-curve breaker on this supply per {cite} (downstream ≤ {_fmt(ceiling, 'A')}). "
            "Verify with the supply maker."
        )
    return lines


def _verdict(
    ref: str,
    rating: float,
    current_a: float,
    *,
    suggestion: dict[str, Any] | None,
    host: str,
    either: bool,
    required: float,
    ceiling: float | None,
    split: bool,
    voltage: float,
) -> str:
    opener = f"{ref} ({_fmt(rating, 'A')}) is too small for {_fmt(current_a, 'A')}"
    if suggestion and suggestion.get("catalog"):
        maker = str(suggestion.get("manufacturer") or "").strip()
        name = f"{maker} {suggestion['catalog']}".strip()
        return (
            f"{opener}. Next: {name} ({_fmt(float(suggestion['amps']), 'A')}). "
            f"{host} is documented to trip this rating on a downstream short."
        )
    if ceiling is None:
        return f"{opener}. {_TRIP_UNPUBLISHED}"
    if required > ceiling + 1e-9:
        where = "either supply" if either else host
        text = (
            f"{opener}, and no single C-curve breaker both carries this load and is "
            f"documented to trip on {where}."
        )
    else:
        text = (
            f"{opener}. No library C-curve with a source is in the "
            f"{_amps_number(required)}–{_amps_number(ceiling)} A range documented to trip on {host}."
        )
    if split:
        if abs(voltage - 24.0) <= 0.6:
            text += " Split the 24 V loads into separately protected branches."
        else:
            text += " Split the loads into separately protected branches."
    return text


def _breaker_advice(
    parts: Any,
    breaker: Any,
    current_a: float,
    *,
    breaker_ref: str,
    rating_a: float,
    voltage: float,
    loads: list[tuple[str, float]],
    installed_supply: Any,
    alternate: Any,
    ampacity: float | None,
    ahead: list[str] | None = None,
) -> tuple[list[str], dict[str, Any] | None]:
    """Plain verdict, then a branch split when needed, then the cited detail."""

    required, factor = _continuous_requirement(breaker, current_a)
    curve = _breaker_curve(breaker)
    installed_text, installed_ceiling = _supply_trip(installed_supply, curve, required)
    binding_part = installed_supply
    binding_ceiling = installed_ceiling
    alt_text = ""
    if alternate is not None and getattr(alternate, "catalog", None):
        alt_text, alt_ceiling = _supply_trip(alternate, curve, required)
        binding_part = alternate
        binding_ceiling = alt_ceiling
    host = str(getattr(binding_part, "catalog", "") or "").strip() or "this supply"
    suggestion = None
    library = ""
    ampacity_blocks = ampacity is not None and required > ampacity + 1e-9
    if binding_ceiling is not None and not ampacity_blocks:
        library, suggestion = _library_sentence(
            parts, breaker, curve, required, binding_ceiling, ampacity
        )
    plan: list[str] = []
    split = False
    if suggestion is None and binding_ceiling is not None and curve == "C" and loads and not ampacity_blocks:
        plan = _branch_lines(parts, loads, binding_part, curve, role="recommended" if alternate is not None else "installed")
        installed_catalog = str(getattr(installed_supply, "catalog", "") or "").strip()
        if (
            alternate is not None
            and installed_catalog
            and installed_catalog != str(getattr(alternate, "catalog", "") or "")
            and installed_ceiling is not None
        ):
            plan.extend(_branch_lines(parts, loads, installed_supply, curve, role="installed"))
        branch_count = sum(1 for line in plan if line.startswith("Branch "))
        oversized = sum(1 for line in plan if "cannot be protected" in line)
        split = branch_count > 1 or (branch_count >= 1 and oversized >= 1)
    either = bool(
        alternate is not None
        and installed_ceiling is not None
        and binding_ceiling is not None
        and required > installed_ceiling + 1e-9
        and required > binding_ceiling + 1e-9
    )
    verdict = _verdict(
        breaker_ref,
        rating_a,
        current_a,
        suggestion=suggestion,
        host=host,
        either=either,
        required=required,
        ceiling=binding_ceiling,
        split=split,
        voltage=voltage,
    )
    lines = [verdict, *plan, *(ahead or []), factor, installed_text]
    if alt_text:
        lines.append(alt_text)
    if ampacity_blocks and ampacity is not None:
        lines.append(
            f"{_fmt(required, 'A')} is above the conductor ampacity {_fmt(ampacity, 'A')}, "
            "so no breaker can both carry this load and protect that conductor."
        )
    if library:
        lines.append(library)
    elif suggestion and suggestion.get("catalog"):
        maker = str(suggestion.get("manufacturer") or "").strip()
        name = f"{maker} {suggestion['catalog']}".strip()
        lines.append(
            f"Next: {name} ({_fmt(float(suggestion['amps']), 'A')}). "
            f"{host} is documented to trip this rating on a downstream short."
        )
    return lines, suggestion


def _attach_protection(
    graph: _Graph,
    supply: _SupplyNode,
    parts: Any,
    supply_suggestion: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], bool, str]:
    devices: dict[tuple[str, str], list[_Edge]] = defaultdict(list)
    for edge in graph.edges:
        if edge.kind == "protection" and edge.device is not None:
            devices[edge.device].append(edge)
    rows: list[dict[str, Any]] = []
    ok = True
    # Map load hot/ret. LoadLine doesn't store roots; recover from the private
    # nodes stashed on the supply object.
    load_nodes: list[_LoadNode] = getattr(supply, "_load_nodes", [])
    if not load_nodes:
        return [], True, "No output fuse or breaker on this supply."
    full_hot = graph.distances(supply.hot)
    full_ret = graph.distances(supply.ret)
    alternate = _lookup_supply(parts, supply_suggestion)
    for key in devices:
        sheet_id, ref = key
        blocked_hot = graph.distances(supply.hot, blocked=key)
        blocked_ret = graph.distances(supply.ret, blocked=key)
        downstream: list[_LoadNode] = []
        for load in load_nodes:
            if load.supply is not supply:
                continue
            was = load.hot in full_hot and load.ret in full_ret
            still = load.hot in blocked_hot and load.ret in blocked_ret
            if was and not still:
                downstream.append(load)
        if not downstream:
            continue
        comp, device = _component(supply, sheet_id, ref)
        if comp is None or device is None:
            continue
        part = _assigned_part(comp, parts)
        rating = None
        if part is not None and part.amps is not None:
            rating = float(part.amps)
        if rating is None and comp is not None:
            props = _merged(device, comp)
            rating = _first_amp(props.get("amps"), props.get("In"), props.get("in"), device.properties.get("In"))
        current = 0.0
        loads: list[tuple[str, float]] = []
        for load in downstream:
            line = _line_for(supply, load)
            if line is not None:
                current += line.current_a
                loads.append((line.ref, line.current_a))
        catalog_no = ""
        if part is not None:
            catalog_no = part.catalog
        else:
            catalog_no = str(comp.properties.get("catalog") or comp.properties.get("part_number") or "")
        good = rating is not None and current <= rating + 1e-9
        suggestion = None
        if rating is None:
            message = f"{supply.ref} output protection {ref} has downstream load {_fmt(current, 'A')} and no amp rating."
            good = False
        elif not good:
            name = f"{catalog_no}, " if catalog_no else ""
            head = (
                f"{supply.ref} output protection {ref} ({name}{_fmt(rating, 'A')}) "
                f"sees {_fmt(current, 'A')} downstream."
            )
            if part is None:
                detail = [
                    (
                        f"Needs at least {_fmt(current, 'A')}. "
                        "No continuous-load factor is applied; the breaker has no catalog series."
                    ),
                    _TRIP_UNPUBLISHED,
                ]
            else:
                ampacity, conductor = _conductor_limit(graph, supply, key)
                detail, suggestion = _breaker_advice(
                    parts,
                    part,
                    current,
                    breaker_ref=ref,
                    rating_a=rating,
                    voltage=float(supply.voltage or 0.0),
                    loads=loads,
                    installed_supply=supply.part,
                    alternate=alternate,
                    ampacity=ampacity,
                    ahead=[head, conductor],
                )
            message = "\n".join(detail if part is not None else [head, *detail])
        else:
            name = f"{catalog_no}, " if catalog_no else ""
            message = (
                f"Output protection {ref} ({name}{_fmt(rating, 'A')}) "
                f"carries {_fmt(current, 'A')}."
            )
        if not good:
            ok = False
        rows.append(
            {
                "ref": ref,
                "sheet": sheet_id,
                "rating_a": rating,
                "current_a": _round(current),
                "catalog": catalog_no,
                "ok": good,
                "message": message,
                "suggestion": suggestion,
            }
        )
    if not rows:
        total = 0.0
        for load in load_nodes:
            if load.supply is not supply:
                continue
            line = _line_for(supply, load)
            if line is not None:
                total += line.current_a
        note = (
            f"No output fuse or breaker on this supply, so {_fmt(total, 'A')} "
            "is not checked against one."
        )
        return [], True, note
    return rows, ok, ""


def _component(supply: _SupplyNode, sheet_id: str, ref: str) -> tuple[Component | None, Device | None]:
    project = getattr(supply, "_project", None)
    catalog = getattr(supply, "_catalog", None)
    if project is None or catalog is None:
        return None, None
    try:
        sheet = project.sheet(sheet_id)
        comp = sheet.component(ref)
    except KeyError:
        return None, None
    return comp, catalog.devices.get(comp.library_id)


def _line_for(supply: _SupplyNode, load: _LoadNode) -> LoadLine | None:
    for line in supply.loads:
        if line.ref == load.ref and line.sheet == load.sheet and line.kind == load.kind:
            return line
    return None


def _assign(supplies: list[_SupplyNode], loads: list[_LoadNode], graph: _Graph) -> None:
    reach = [(graph.distances(item.hot), graph.distances(item.ret), item) for item in supplies]
    for item in supplies:
        item._load_nodes = []  # type: ignore[attr-defined]
    for load in loads:
        best: tuple[int, _SupplyNode] | None = None
        for hot_dist, ret_dist, supply in reach:
            if load.hot not in hot_dist or load.ret not in ret_dist:
                continue
            dist = hot_dist[load.hot]
            if best is None or dist < best[0]:
                best = (dist, supply)
        if best is None:
            continue
        supply = best[1]
        supply.loads.append(_consumption(load, supply))
        load.supply = supply
        supply._load_nodes.append(load)  # type: ignore[attr-defined]


def _finish(supply: _SupplyNode, margin: float, parts: Any, graph: _Graph) -> SupplyBudget:
    rating_w, rating_a, rating_va, method, source, catalog_no = _supply_rating(supply)
    steady_w = sum(row.steady_w for row in supply.loads)
    steady_va = sum(row.steady_va for row in supply.loads)
    steady_a = sum(row.current_a for row in supply.loads)
    if supply.kind == "ac":
        published = [row for row in supply.loads if row.kind in _COIL_KINDS and row.inrush_published]
        if published:
            inrush = sum(row.inrush_va for row in published)
            inrush += sum(row.steady_va for row in supply.loads if row.kind not in _COIL_KINDS)
        else:
            inrush = 0.0
    else:
        inrush = 0.0
    limit_w = None if rating_w is None else rating_w * margin
    limit_va = None if rating_va is None else rating_va * margin
    inrush_limit, method_note = _inrush_limit(method, rating_va)
    ok_margin = True
    if supply.kind == "dc":
        if limit_w is not None and steady_w > limit_w + 1e-9:
            ok_margin = False
        if rating_a is not None and steady_a > rating_a * margin + 1e-9:
            ok_margin = False
    elif limit_va is not None and steady_va > limit_va + 1e-9:
        ok_margin = False
    ok_inrush = True
    if supply.kind == "ac" and inrush_limit is not None and inrush > inrush_limit + 1e-9:
        ok_inrush = False
    method_missing = bool(supply.kind == "ac" and inrush > 1e-9 and not method)
    if method and supply.kind == "ac" and inrush > 1e-9 and inrush_limit is None:
        method_missing = True
        method_note = method_note or "published inrush method has no row for this VA"
    suggestion = None
    if not ok_margin or not ok_inrush:
        suggestion = _suggest(supply, parts, steady_w, steady_va, steady_a, inrush, rating_a, rating_va)
    protection, ok_protection, protection_note = _attach_protection(graph, supply, parts, suggestion)
    budget = SupplyBudget(
        ref=supply.ref,
        sheet=supply.sheet,
        kind=supply.kind,
        voltage=supply.voltage,
        rating_w=rating_w,
        rating_a=rating_a,
        rating_va=rating_va,
        steady_w=steady_w,
        steady_va=steady_va,
        steady_a=steady_a,
        inrush_va=inrush,
        margin=margin,
        limit_w=limit_w,
        limit_va=limit_va,
        inrush_limit_va=inrush_limit,
        method=method,
        method_note=method_note,
        ok_margin=ok_margin,
        ok_inrush=ok_inrush,
        ok_protection=ok_protection,
        method_missing=method_missing,
        catalog=catalog_no,
        source=source,
        loads=sorted(supply.loads, key=lambda row: (row.sheet, row.ref, row.kind)),
        protection=protection,
        protection_note=protection_note,
        boost_note=_boost_note(supply.loads),
        suggestion=suggestion,
        x=supply.comp.x,
        y=supply.comp.y,
    )
    budget.findings = _findings(budget)
    return budget


def analyze(
    project: Project,
    catalog: Catalog,
    parts: Any = None,
    *,
    supply: str | None = None,
    margin: float | None = None,
) -> list[SupplyBudget]:
    """One budget per control supply. ``supply`` keeps a single tag."""

    limit = margin_of(project, margin)
    graph = _Graph.build(project, catalog)
    supplies = _supplies(project, catalog, parts, graph)
    loads = _loads(project, catalog, parts, graph)
    for item in supplies:
        item._project = project  # type: ignore[attr-defined]
        item._catalog = catalog  # type: ignore[attr-defined]
    _assign(supplies, loads, graph)
    want = str(supply or "").strip().casefold()
    out: list[SupplyBudget] = []
    for item in supplies:
        if want and item.ref.casefold() != want and f"{item.sheet}:{item.ref}".casefold() != want:
            continue
        out.append(_finish(item, limit, parts, graph))
    return out
