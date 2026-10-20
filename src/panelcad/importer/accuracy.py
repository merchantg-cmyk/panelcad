"""Round-trip score: tags, wire numbers, drawn symbol class, and the netlist."""

from __future__ import annotations

from collections import Counter
from typing import Any

from panelcad.core.models import Project
from panelcad.importer.symbols import shape_class

_LAYOUT = {"panel_layout", "door_layout"}


def score_projects(truth: Project, built: Project, *, catalog: Any = None) -> dict[str, Any]:
    truth_tags = _tags(truth)
    built_tags = _tags(built)
    truth_wires = _wire_numbers(truth)
    built_wires = _wire_numbers(built)
    symbols = _class_score(truth, built, catalog)
    edges = _edge_score(truth, built)
    return {
        "devices": _recall(truth_tags, built_tags),
        "tags": _recall(truth_tags, built_tags),
        "wire_numbers": _recall(truth_wires, built_wires),
        "symbol_class": symbols["recall"],
        "connectivity_precision": edges["precision"],
        "connectivity_recall": edges["recall"],
        "truth_devices": len(truth_tags),
        "recovered_devices": len(truth_tags & built_tags),
        "truth_wires": len(truth_wires),
        "recovered_wires": len(truth_wires & built_wires),
        "truth_symbols": symbols["truth"],
        "matched_symbols": symbols["matched"],
        "truth_edges": edges["truth"],
        "built_edges": edges["built"],
        "matched_edges": edges["matched"],
        "missing_tags": sorted(truth_tags - built_tags),
        "missing_wires": sorted(truth_wires - built_wires),
        "extra_tags": sorted(built_tags - truth_tags),
        "extra_wires": sorted(built_wires - truth_wires),
        "missing_edges": edges["missing"],
        "extra_edges": edges["extra"],
        "netlist_match": edges["precision"] == 1.0 and edges["recall"] == 1.0 and edges["truth"] == edges["built"],
    }


def score_sheet(truth: Project, built: Project, sheet_id: str, *, catalog: Any = None) -> dict[str, Any]:
    """Score one sheet. Sheet ids are titles when the import rebuilt them."""

    truth_sheet = _sheet(truth, sheet_id)
    built_sheet = _sheet(built, sheet_id)
    if truth_sheet is None or built_sheet is None:
        return score_projects(truth, built, catalog=catalog)
    return score_sheet_pair(truth_sheet, built_sheet, catalog=catalog, sheet=sheet_id)


def score_sheet_pair(truth_sheet: Any, built_sheet: Any, catalog: Any = None, *, sheet: str = "") -> dict[str, Any]:
    """Score two sheets that are the same drawing, whatever they were named."""

    truth_tags = _tags_on(truth_sheet)
    built_tags = _tags_on(built_sheet)
    truth_wires = {str(wire.number) for wire in truth_sheet.wires if str(wire.number or "").strip()}
    built_wires = {str(wire.number) for wire in built_sheet.wires if str(wire.number or "").strip()}
    symbols = _class_score_sheets(truth_sheet, built_sheet, catalog)
    edges = _edge_score_sheets(truth_sheet, built_sheet)
    return {
        "sheet": sheet or getattr(truth_sheet, "id", ""),
        "devices": _recall(truth_tags, built_tags),
        "tags": _recall(truth_tags, built_tags),
        "wire_numbers": _recall(truth_wires, built_wires),
        "symbol_class": symbols["recall"],
        "connectivity_precision": edges["precision"],
        "connectivity_recall": edges["recall"],
        "missing_tags": sorted(truth_tags - built_tags),
        "missing_wires": sorted(truth_wires - built_wires),
        "extra_tags": sorted(built_tags - truth_tags),
        "extra_wires": sorted(built_wires - truth_wires),
        "missing_edges": edges["missing"],
        "extra_edges": edges["extra"],
        "netlist_match": edges["precision"] == 1.0 and edges["recall"] == 1.0,
    }


def _tags(project: Project) -> set[str]:
    found: set[str] = set()
    for sheet in project.sheets:
        found.update(_tags_on(sheet))
    return found


def _tags_on(sheet: Any) -> set[str]:
    found = set()
    for comp in sheet.components:
        lib = str(comp.library_id or "")
        if "offpage" in lib or lib.endswith("notes.block"):
            continue
        found.add(comp.id)
    return found


def _wire_numbers(project: Project) -> set[str]:
    found = set()
    for sheet in project.sheets:
        for wire in sheet.wires:
            number = str(wire.number or "").strip()
            if number:
                found.add(number)
    return found


def _recall(truth: set[str], built: set[str]) -> float:
    if not truth:
        return 1.0
    return len(truth & built) / len(truth)


def _sheet(project: Project, sheet_id: str) -> Any:
    try:
        return project.sheet(sheet_id)
    except KeyError:
        for sheet in project.sheets:
            if sheet.title == sheet_id or sheet.id == sheet_id:
                return sheet
    return None


def _class_of(component: Any, catalog: Any) -> str:
    if catalog is None:
        return ""
    try:
        device = catalog.get(component.library_id)
    except KeyError:
        return ""
    return shape_class(device)


def _class_score(truth: Project, built: Project, catalog: Any) -> dict[str, Any]:
    truth_counts = _class_counts(truth, catalog)
    built_counts = _class_counts(built, catalog)
    matched = sum(min(truth_counts[key], built_counts[key]) for key in truth_counts)
    total = sum(truth_counts.values())
    return {"recall": 1.0 if not total else matched / total, "truth": total, "matched": matched}


def _class_score_sheets(truth: Any, built: Any, catalog: Any) -> dict[str, Any]:
    truth_counts = _class_counts_on(truth, catalog)
    built_counts = _class_counts_on(built, catalog)
    matched = sum(min(truth_counts[key], built_counts[key]) for key in truth_counts)
    total = sum(truth_counts.values())
    return {"recall": 1.0 if not total else matched / total, "truth": total, "matched": matched}


def _class_counts(project: Project, catalog: Any) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    for sheet in project.sheets:
        counts.update(_class_counts_on(sheet, catalog))
    return counts


def _class_counts_on(sheet: Any, catalog: Any) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    for comp in sheet.components:
        lib = str(comp.library_id or "")
        if "offpage" in lib or lib.endswith("notes.block"):
            continue
        counts[(comp.id, _class_of(comp, catalog))] += 1
    return counts


def _edge_score(truth: Project, built: Project) -> dict[str, Any]:
    truth_edges = _edges(truth)
    built_edges = _edges(built)
    return _edge_numbers(truth_edges, built_edges)


def _edge_score_sheets(truth: Any, built: Any) -> dict[str, Any]:
    return _edge_numbers(_edges_on(truth), _edges_on(built))


def _edge_numbers(truth_edges: set[tuple[str, str]], built_edges: set[tuple[str, str]]) -> dict[str, Any]:
    matched = truth_edges & built_edges
    precision = 1.0 if not built_edges else len(matched) / len(built_edges)
    recall = 1.0 if not truth_edges else len(matched) / len(truth_edges)
    return {
        "precision": precision,
        "recall": recall,
        "truth": len(truth_edges),
        "built": len(built_edges),
        "matched": len(matched),
        "missing": sorted(f"{a} {b}" for a, b in truth_edges - built_edges),
        "extra": sorted(f"{a} {b}" for a, b in built_edges - truth_edges),
    }


def _edges(project: Project) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for sheet in project.sheets:
        if sheet.kind in _LAYOUT:
            continue
        found.update(_edges_on(sheet))
    return found


def _edges_on(sheet: Any) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    if getattr(sheet, "kind", "") in _LAYOUT:
        return found
    for wire in sheet.wires:
        pair = _pair(str(wire.frm), str(wire.to))
        if pair is not None:
            found.add(pair)
    return found


def _pair(left: str, right: str) -> tuple[str, str] | None:
    a = left.strip().upper()
    b = right.strip().upper()
    if not a or not b or "." not in a or "." not in b:
        return None
    return tuple(sorted((a, b)))  # type: ignore[return-value]
