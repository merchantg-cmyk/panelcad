"""Geometry checks already used by the router, applied to a rebuilt sheet."""

from __future__ import annotations

from typing import Any

from panelcad.core.checks import structural_findings
from panelcad.core.geometry import Point
from panelcad.core.route import path_hits_obstacles
from panelcad.render.titleblock import title_block_rect
from panelcad.render.wire_labels import boxes_overlap, mark_box, place_wire_marks
from panelcad.render.wiring import component_obstacle, route_sheet_wires

_LAYOUT = {"panel_layout", "door_layout"}


def geometry_problems(project: Any, catalog: Any) -> list[dict[str, str]]:
    """Wires through bodies, wires in the title block or off the page, dangling ends, text on a wire, orphan notes."""

    problems: list[dict[str, str]] = []
    for sheet in project.sheets:
        if sheet.kind in _LAYOUT:
            continue
        problems.extend(_sheet_problems(project, sheet, catalog))
    return problems


def _sheet_problems(project: Any, sheet: Any, catalog: Any) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    width, height = sheet.page.dimensions()
    tx, ty, tw, th = title_block_rect(sheet)
    title = (tx, ty, tx + tw, ty + th)
    bodies = []
    for comp in sheet.components:
        lib = str(comp.library_id or "")
        try:
            device = catalog.get(lib)
        except KeyError:
            found.append({"sheet": sheet.id, "kind": "orphan", "ref": comp.id})
            continue
        props = comp.properties or {}
        needs_info = str(props.get("reconstruct_needs_info") or "").lower() in {"1", "true", "yes"}
        if not needs_info and ("notes" in lib or (device.category or "").lower().startswith("connection.notes")):
            found.append({"sheet": sheet.id, "kind": "orphan", "ref": comp.id})
        if comp.x < -1 or comp.y < -1 or comp.x > width + 1 or comp.y > height + 1:
            found.append({"sheet": sheet.id, "kind": "frame", "ref": comp.id})
        if tx <= comp.x <= tx + tw and ty <= comp.y <= ty + th:
            found.append({"sheet": sheet.id, "kind": "title_block", "ref": comp.id})
        try:
            bodies.append(component_obstacle(sheet, catalog, comp))
        except (KeyError, ValueError):
            continue
    try:
        routes = route_sheet_wires(sheet, catalog)
    except (KeyError, ValueError):
        routes = {}
        found.append({"sheet": sheet.id, "kind": "route", "ref": ""})
    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        if len(pts) >= 2 and path_hits_obstacles(pts, bodies, skip_ends=True):
            found.append({"sheet": sheet.id, "kind": "body", "ref": wire.id})
        if len(pts) >= 2 and path_hits_obstacles(pts, [title], skip_ends=False):
            found.append({"sheet": sheet.id, "kind": "title_block", "ref": wire.id})
        for point in pts:
            if point.x < -0.8 or point.y < -0.8 or point.x > width + 0.8 or point.y > height + 0.8:
                found.append({"sheet": sheet.id, "kind": "frame", "ref": wire.id})
                break
    for item in structural_findings(project, catalog):
        if item.check == "dangling_wire" and item.sheet == sheet.id:
            found.append({"sheet": sheet.id, "kind": "dangling", "ref": item.ref})
    found.extend(_text_on_wire(sheet, catalog, routes))
    return found


def _text_on_wire(sheet: Any, catalog: Any, routes: dict[str, list[Point]]) -> list[dict[str, str]]:
    if not routes:
        return []
    try:
        marks = place_wire_marks(sheet, catalog, routes, ladder=sheet.drawing_style == "ladder")
    except (KeyError, ValueError):
        return [{"sheet": sheet.id, "kind": "wire_over_text", "ref": ""}]
    found = []
    by_id = {wire.id: wire for wire in sheet.wires}
    for wire_id, placed in marks.items():
        wire = by_id.get(wire_id)
        if wire is None:
            continue
        mark = wire.drawn_mark(ladder=sheet.drawing_style == "ladder")
        box = mark_box(placed[0], placed[1], mark, align=placed[2], spec=wire.spec_text())
        pts = routes.get(wire_id) or []
        for start, end in zip(pts, pts[1:], strict=False):
            if abs(start.x - end.x) + abs(start.y - end.y) < 2.0:
                continue
            segment = (
                min(start.x, end.x) - 0.15,
                min(start.y, end.y) - 0.15,
                max(start.x, end.x) + 0.15,
                max(start.y, end.y) + 0.15,
            )
            if boxes_overlap(box, segment):
                found.append({"sheet": sheet.id, "kind": "wire_over_text", "ref": wire_id})
                break
    return found
