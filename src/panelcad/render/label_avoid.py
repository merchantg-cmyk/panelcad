"""Move tags, wire numbers, and cross-references that land on other ink.

The placers pick a first seat. This pass only moves a label that already
intersects another label or a symbol, and it leaves a clear label where it
is. A wire number stays beside its own conductor.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from math import hypot

from panelcad.core.geometry import Point
from panelcad.core.ink import text_box
from panelcad.core.models import Project, Sheet
from panelcad.library.catalog import Catalog
from panelcad.render.sheet_labels import (
    caption_layouts,
    pin_caption_boxes,
    sheet_symbol_bodies,
)
from panelcad.render.titleblock import title_block_rect, working_rect
from panelcad.render.wire_labels import boxes_overlap, bundle_spec_plan, mark_box
from panelcad.memory import CountLRU, sheet_cache_limit
from panelcad.render.wiring import route_sheet_wires
from panelcad.render.xref import annotation_box

# A number this far from every segment of its wire is not on that wire.
ON_WIRE = 12.0

_CACHE: ContextVar[dict | None] = ContextVar("panelcad_resolved_labels", default=None)
# Survives a sheet switch. Keyed on the sheet, the project, and the library
# geometry those labels were built from. An edit, undo, redo, or import drops it.
# The key is a hash so the cache does not keep a second copy of the project.
_STORED: CountLRU[tuple, "ResolvedLabels"] = CountLRU(sheet_cache_limit)
_COMPUTES = 0


def label_cache_stats() -> tuple[int, int]:
    """(full computes, entries kept). A sheet switch that hits does not compute."""

    return _COMPUTES, len(_STORED)


def clear_resolved_labels() -> None:
    """Drop cached routes and marks. The next sheet paints from the model."""

    _STORED.clear()
    try:
        from panelcad.render.wiring import clear_route_cache

        clear_route_cache()
    except Exception:  # noqa: BLE001 — a missing router still forgets the labels
        return


def _blob(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _device_key(device: object) -> str:
    dump = device.model_dump(mode="json") if hasattr(device, "model_dump") else repr(device)
    return _blob(dump)


def _library_key(project: Project, catalog: Catalog) -> tuple:
    ids: set[str] = set()
    for page in project.sheets:
        for comp in page.components:
            ids.add(comp.library_id)
    rows: list[tuple[str, str]] = []
    for lib_id in sorted(ids):
        try:
            device = catalog.get(lib_id)
        except KeyError:
            rows.append((lib_id, "missing"))
            continue
        rows.append((lib_id, _device_key(device)))
    return tuple(rows)


def _project_key(project: Project) -> str:
    """Other pages too: a cross-reference moves when a coil on another sheet moves."""

    title = project.title_block.model_dump(mode="json") if project.title_block is not None else None
    pages = []
    for page in project.sheets:
        pages.append(
            {
                "id": page.id,
                "title": page.title,
                "kind": page.kind,
                "drawing_style": page.drawing_style,
                "standard": getattr(page, "standard", ""),
                "components": [
                    (
                        comp.id,
                        comp.library_id,
                        round(float(comp.x), 3),
                        round(float(comp.y), 3),
                        int(comp.rotation or 0),
                        round(float(comp.visual_scale() or 1.0), 4),
                    )
                    for comp in page.components
                ],
                "wires": [(wire.id, wire.frm, wire.to, wire.number) for wire in page.wires],
            }
        )
    return _blob({"name": project.name, "title": title, "sheets": pages})


def _digest(value: object) -> str:
    """Short stable hash. The cache keeps this instead of the JSON it came from."""

    hasher = hashlib.blake2s(digest_size=16)
    hasher.update(_blob(value).encode())
    return hasher.hexdigest()


def project_drawing_stamp(project: Project, catalog: Catalog) -> str:
    """Changes when the drawing or the symbols on it change.

    Sheet scenes use this so a cached picture is dropped when the model moves,
    including a revision cloud or a library pin that the route key also sees.
    """

    return _digest((project.model_dump(mode="json"), _library_key(project, catalog)))


def drawing_key(
    project: Project,
    catalog: Catalog,
    sheet: Sheet,
    *,
    ladder: bool,
    sheet_no: int,
) -> tuple:
    """Everything that moves a route, a wire number, or a tag on this sheet."""

    payload = (
        sheet.id,
        bool(ladder),
        int(sheet_no),
        sheet.model_dump(mode="json"),
        _project_key(project),
        _library_key(project, catalog),
    )
    return (sheet.id, bool(ladder), int(sheet_no), _digest(payload))


@contextmanager
def share_resolved_labels():
    token = _CACHE.set({})
    try:
        yield
    finally:
        _CACHE.reset(token)


@dataclass
class ResolvedLabels:
    tags: dict[str, tuple[float, float, str, float]]
    marks: dict[str, tuple[float, float, str]]
    xrefs: list
    routes: dict[str, list[Point]]


def resolve_labels(
    project: Project,
    catalog: Catalog,
    sheet: Sheet,
    *,
    ladder: bool = False,
    sheet_no: int = 1,
) -> ResolvedLabels:
    """Tag, wire-number, and cross-reference seats after the collision pass."""

    global _COMPUTES
    cache = _CACHE.get()
    key = drawing_key(project, catalog, sheet, ladder=ladder, sheet_no=sheet_no)
    if cache is not None and key in cache:
        return cache[key]
    stored = _STORED.get(key)
    if stored is not None:
        if cache is not None:
            cache[key] = stored
        return stored
    _COMPUTES += 1
    # Look the placers up on their modules so a test can replace them.
    import panelcad.render.sheet_labels as sheet_labels
    import panelcad.render.wire_labels as wire_labels
    import panelcad.render.xref as xref_mod

    tags = dict(sheet_labels.place_device_tags(sheet, catalog, ladder=ladder, project=project, sheet_no=sheet_no))
    # A missing pin or a rail that was renamed out from under a wire has to
    # raise. Swallowing it drew the sheet with no wires and no error.
    routes = route_sheet_wires(sheet, catalog, project=project)
    marks = dict(wire_labels.place_wire_marks(sheet, catalog, routes, ladder=ladder, project=project))
    try:
        xrefs = list(xref_mod.place_xref_labels(project, catalog, sheet, ladder=ladder))
    except (KeyError, ValueError, IndexError):
        xrefs = []
    _avoid(sheet, catalog, project, tags, marks, xrefs, routes, ladder=ladder, sheet_no=sheet_no)
    resolved = ResolvedLabels(tags=tags, marks=marks, xrefs=xrefs, routes=routes)
    _STORED[key] = resolved
    if cache is not None:
        cache[key] = resolved
    return resolved


def anchor_on_route(x: float, y: float, pts: list[Point], limit: float = ON_WIRE) -> bool:
    """True when (x, y) sits beside a segment of *pts*, not past its ends."""

    return _route_distance(x, y, pts) <= limit


def _avoid(
    sheet: Sheet,
    catalog: Catalog,
    project: Project,
    tags: dict[str, tuple[float, float, str, float]],
    marks: dict[str, tuple[float, float, str]],
    xrefs: list,
    routes: dict[str, list[Point]],
    *,
    ladder: bool,
    sheet_no: int,
) -> None:
    from panelcad.render.sheet_labels import _line_number_boxes

    specs = bundle_spec_plan(sheet, routes, ladder=ladder)
    named_bodies = sheet_symbol_bodies(sheet, catalog)
    bodies = [box for _name, box in named_bodies]
    body_of = {name.split(":", 1)[1]: box for name, box in named_bodies if name.startswith("body:")}
    wx, wy, ww, wh = working_rect(sheet)
    bounds = (wx, wy, wx + ww, wy + wh)
    tx, ty, tw, th = title_block_rect(sheet)
    fixed = [box for _name, box in pin_caption_boxes(sheet, catalog)]
    fixed.extend(box for _name, box in _line_number_boxes(project, sheet, catalog, sheet_no=sheet_no))
    fixed.append((tx, ty, tx + tw, ty + th))
    fixed.extend(_rating_boxes(sheet, catalog))
    by_id = {wire.id: wire for wire in sheet.wires}

    def tag_boxes(cid: str, layout: tuple[float, float, str, float]) -> list[tuple[float, float, float, float]]:
        x, y, align, size = layout
        box = text_box(x, y, cid, size, align)
        boxes = [box] if box is not None else []
        comp = _comp(sheet, cid)
        if comp is None:
            return boxes
        for cx, cy, calign, csize, line in caption_layouts(comp, layout):
            cbox = text_box(cx, cy, line, csize, calign)
            if cbox is not None:
                boxes.append(cbox)
        return boxes

    def mark_boxes(wid: str, at: tuple[float, float, str]) -> list[tuple[float, float, float, float]]:
        wire = by_id.get(wid)
        if wire is None:
            return []
        text = wire.drawn_mark(ladder=ladder)
        if not text:
            return []
        return [mark_box(at[0], at[1], text, align=at[2], spec=specs.get(wid, ""))]

    def xref_boxes() -> list[tuple[float, float, float, float]]:
        return [annotation_box(ann, x, y, align) for ann, x, y, align in xrefs]

    # Long bus lanes are not tag obstacles unless they are named here. A tag
    # parked above a breaker then sits on the phase lanes.
    conductors = _long_conductor_boxes(routes)
    for _ in range(6):
        changed = False
        for cid, layout in list(tags.items()):
            own = tag_boxes(cid, layout)
            blocked_by = _others(
                own, bodies, fixed, _all_tags(tags, tag_boxes, skip=cid), _all_marks(marks, mark_boxes), xref_boxes()
            )
            blocked_by.extend(conductors)
            if not own or not _hits(own, blocked_by):
                continue
            found = _search_tag(
                cid,
                layout,
                sheet,
                bodies,
                fixed,
                tags,
                tag_boxes,
                marks,
                mark_boxes,
                xref_boxes,
                bounds,
                conductors,
                body_of.get(cid),
            )
            if found is not None and found != layout:
                tags[cid] = found
                changed = True
        for index, (ann, x, y, align) in enumerate(list(xrefs)):
            box = annotation_box(ann, x, y, align)
            others = _others([box], bodies, fixed, _all_tags(tags, tag_boxes), _all_marks(marks, mark_boxes), [b for i, b in enumerate(xref_boxes()) if i != index])
            if not _hits([box], others):
                continue
            found = _search_point(
                x,
                y,
                align,
                lambda px, py, palign: [annotation_box(ann, px, py, palign)],
                others,
                bounds,
            )
            if found is not None:
                xrefs[index] = (ann, found[0], found[1], found[2])
                changed = True
        for wid, at in list(marks.items()):
            pts = routes.get(wid) or []
            own = mark_boxes(wid, at)
            far = bool(pts) and not anchor_on_route(at[0], at[1], pts)
            if not own:
                continue
            others = _others(own, bodies, fixed, _all_tags(tags, tag_boxes), _all_marks(marks, mark_boxes, skip=wid), xref_boxes())
            ignore = _end_bodies(by_id.get(wid), body_of)
            if not far and not _hits(own, _without(others, ignore)):
                continue
            found = _search_on_wire(wid, at, pts, by_id, specs, ladder, others, bounds, ignore)
            if found is not None and found != at:
                marks[wid] = found
                changed = True
        if not changed:
            break


def _long_conductor_boxes(routes: dict[str, list[Point]]) -> list[tuple[float, float, float, float]]:
    """Boxes around conductor runs long enough to be a bus, not a pin stub."""

    boxes = []
    half = 0.6
    for pts in routes.values():
        for start, end in zip(pts, pts[1:]):
            # Only a long horizontal bus. A column lead is vertical and must
            # not shove the device tag off the symbol.
            if abs(start.y - end.y) > 0.6 or hypot(end.x - start.x, end.y - start.y) < 18.0:
                continue
            boxes.append(
                (
                    min(start.x, end.x) - half,
                    min(start.y, end.y) - half,
                    max(start.x, end.x) + half,
                    max(start.y, end.y) + half,
                )
            )
    return boxes


def _search_tag(
    cid,
    layout,
    sheet,
    bodies,
    fixed,
    tags,
    tag_boxes,
    marks,
    mark_boxes,
    xref_boxes,
    bounds,
    conductors=None,
    owner_body=None,
):
    x, y, align, size = layout
    comp = _comp(sheet, cid)

    def boxes_at(px, py, palign):
        trial = (px, py, palign, size)
        own = []
        box = text_box(px, py, cid, size, palign)
        if box is not None:
            own.append(box)
        if comp is not None:
            for cx, cy, calign, csize, line in caption_layouts(comp, trial):
                cbox = text_box(cx, cy, line, csize, calign)
                if cbox is not None:
                    own.append(cbox)
        return own

    others = _others([], bodies, fixed, _all_tags(tags, tag_boxes, skip=cid), _all_marks(marks, mark_boxes), xref_boxes())
    others.extend(conductors or [])
    # The phase lanes run in the band above a feeder column, which is also
    # where the tag wants to sit. Only then try a seat beside the symbol.
    # Doing this for every blocked tag pulled F1 off the overload.
    current = boxes_at(x, y, align)
    if comp is not None and current and _hits(current, conductors or []):
        from panelcad.core.ink import TEXT_ADVANCE

        tag_w = size * TEXT_ADVANCE * max(1, len(cid))
        for baseline in (comp.y + 12.0, comp.y + 20.0, comp.y + 28.0):
            trial = (comp.x - tag_w / 2.0 - 1.8, baseline, "middle")
            boxes = boxes_at(*trial)
            if boxes and all(_inside(box, bounds) for box in boxes) and not _hits(boxes, others):
                return (trial[0], trial[1], trial[2], size)
    covered = None
    if owner_body is not None:
        covered = {
            tuple(box)
            for box in bodies
            if tuple(box) != tuple(owner_body) and _rects_touch(owner_body, box)
        }
    found = _search_point(
        x, y, align, boxes_at, others, bounds, prefer_up=True, owner_body=owner_body, covered_neighbors=covered
    )
    if found is None:
        return None
    return (found[0], found[1], found[2], size)


def _search_on_wire(wid, at, pts, by_id, specs, ladder, others, bounds, ignore=None):
    """A seat on this wire. A blocked seat still beats a number left off the run."""

    wire = by_id.get(wid)
    if wire is None or len(pts) < 2:
        return None
    text = wire.drawn_mark(ladder=ladder)
    if not text:
        return None
    spec = specs.get(wid, "")
    obstacles = _without(others, ignore or [])
    seats = _seats(pts) or _short_seats(pts)
    best_clear = None
    best_rank = 10**9
    best_overlap = None
    best_area = 10**9
    best_overlap_rank = 10**9
    for sx, sy, align in seats:
        if not anchor_on_route(sx, sy, pts):
            continue
        box = mark_box(sx, sy, text, align=align, spec=spec)
        rank = abs(sx - at[0]) + abs(sy - at[1]) + (0.0 if _inside(box, bounds) else 30.0)
        if _inside(box, bounds) and not _hits([box], obstacles):
            if rank < best_rank:
                best_clear = (sx, sy, align)
                best_rank = rank
            continue
        area = _overlap_area(box, obstacles)
        if area < best_area - 0.01 or (abs(area - best_area) <= 0.01 and rank < best_overlap_rank):
            best_overlap = (sx, sy, align)
            best_area = area
            best_overlap_rank = rank
    return best_clear if best_clear is not None else best_overlap


def _end_bodies(wire, body_of: dict) -> list[tuple[float, float, float, float]]:
    if wire is None:
        return []
    from panelcad.render.sheet import parse_pin_ref

    found = []
    for end in (wire.frm, wire.to):
        try:
            ref, _pin = parse_pin_ref(end)
        except ValueError:
            continue
        box = body_of.get(ref)
        if box is not None:
            found.append(box)
    return found


def _without(others, ignore):
    if not ignore:
        return others
    kept = []
    for box in others:
        if any(_same_box(box, skip) for skip in ignore):
            continue
        kept.append(box)
    return kept


def _same_box(a, b) -> bool:
    return all(abs(float(a[i]) - float(b[i])) <= 0.05 for i in range(4))


def _overlap_area(box, others) -> float:
    total = 0.0
    for other in others:
        width = min(box[2], other[2]) - max(box[0], other[0])
        height = min(box[3], other[3]) - max(box[1], other[1])
        if width > 0.0 and height > 0.0:
            total += width * height
    return total


def _short_seats(pts: list[Point]) -> list[tuple[float, float, str]]:
    """Midpoint of a lead that is shorter than the normal host."""

    if len(pts) < 2:
        return []
    best = max(zip(pts, pts[1:]), key=lambda pair: hypot(pair[1].x - pair[0].x, pair[1].y - pair[0].y))
    a, b = best
    mx, my = (a.x + b.x) * 0.5, (a.y + b.y) * 0.5
    if abs(a.x - b.x) <= abs(a.y - b.y):
        return [(mx - 3.2, my, "end"), (mx + 2.6, my, "start"), (mx, my, "middle")]
    return [(mx, my - 3.2, "middle"), (mx, my + 2.6, "start"), (mx, my, "middle")]


def _rects_touch(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _faces_away(box: tuple[float, float, float, float], anchor_x: float, body: tuple[float, float, float, float]) -> bool:
    """True when the text sits on the opposite side of the anchor from its symbol."""

    body_x = (body[0] + body[2]) / 2.0
    text_x = (box[0] + box[2]) / 2.0
    return (text_x - anchor_x) * (body_x - anchor_x) < -1.0


def _search_point(
    x,
    y,
    align,
    box_fn,
    others,
    bounds,
    *,
    prefer_up: bool = False,
    owner_body=None,
    covered_neighbors: set[tuple[float, float, float, float]] | None = None,
):
    best = None
    best_rank = 10**9
    offsets = [(0.0, 0.0)]
    for radius in (2.0, 4.0, 6.0, 9.0, 12.0, 16.0, 22.0, 30.0, 40.0):
        offsets.extend(
            (
                (0.0, -radius),
                (-radius, -radius * 0.4),
                (radius, -radius * 0.4),
                (-radius, 0.0),
                (radius, 0.0),
                (0.0, radius),
                (-radius, radius * 0.5),
                (radius, radius * 0.5),
            )
        )
    for dx, dy in offsets:
        for palign in (align, "start", "end", "middle"):
            boxes = box_fn(x + dx, y + dy, palign)
            if not boxes or any(not _inside(box, bounds) for box in boxes):
                continue
            if owner_body is not None and _faces_away(boxes[0], x, owner_body):
                continue
            # A symbol drawn on top of its neighbor (a lamp on a button) makes
            # the neighbor's box cover the lamp's own tag corner. That neighbor
            # is not a reason to flip the tag away from the lamp. The lamp's
            # own body still blocks the tag.
            blocking = others
            if owner_body is not None and covered_neighbors:
                blocking = [box for box in others if tuple(box) not in covered_neighbors]
            if _hits(boxes, blocking):
                continue
            rank = abs(dx) + abs(dy) + (0.0 if palign == align else 1.5)
            if prefer_up and dy > 0:
                rank += 4.0
            if rank < best_rank:
                best = (x + dx, y + dy, palign)
                best_rank = rank
    return best


def _seats(pts: list[Point]) -> list[tuple[float, float, str]]:
    seats: list[tuple[float, float, str]] = []
    for a, b in zip(pts, pts[1:]):
        length = hypot(b.x - a.x, b.y - a.y)
        if length < 2.0:
            continue
        vertical = abs(a.x - b.x) <= abs(a.y - b.y)
        for t in (0.5, 0.35, 0.65, 0.25, 0.75):
            mx = a.x + (b.x - a.x) * t
            my = a.y + (b.y - a.y) * t
            if vertical:
                seats.append((mx - 4.0, my, "end"))
                seats.append((mx + 3.2, my, "start"))
                seats.append((mx - 7.0, my, "end"))
            else:
                seats.append((mx, my - 3.4, "middle"))
                seats.append((mx, my + 3.2, "start"))
                seats.append((mx, my - 6.0, "middle"))
    return seats


def _route_distance(x: float, y: float, pts: list[Point]) -> float:
    best = 10**9
    for a, b in zip(pts, pts[1:]):
        best = min(best, _segment_distance(x, y, a.x, a.y, b.x, b.y))
    return best


def _segment_distance(x: float, y: float, x1: float, y1: float, x2: float, y2: float) -> float:
    dx, dy = x2 - x1, y2 - y1
    length2 = dx * dx + dy * dy
    if length2 <= 1e-6:
        return hypot(x - x1, y - y1)
    t = ((x - x1) * dx + (y - y1) * dy) / length2
    if t < 0.0 or t > 1.0:
        return 10**6
    return hypot(x - (x1 + t * dx), y - (y1 + t * dy))


def _rating_boxes(sheet: Sheet, catalog: Catalog) -> list[tuple[float, float, float, float]]:
    from panelcad.core.pins import rating_caption_positions

    boxes = []
    for comp in sheet.components:
        device = catalog.devices.get(comp.library_id)
        if device is None:
            continue
        scale = comp.visual_scale()
        for cx, cy, line in rating_caption_positions(comp, device.schematic_height, scale, device.schematic_width):
            box = text_box(cx, cy, line, 2.2, "start")
            if box is not None:
                boxes.append(box)
    return boxes


def _all_tags(tags, tag_boxes, *, skip: str = "") -> list[tuple[float, float, float, float]]:
    out = []
    for cid, layout in tags.items():
        if cid == skip:
            continue
        out.extend(tag_boxes(cid, layout))
    return out


def _all_marks(marks, mark_boxes, *, skip: str = "") -> list[tuple[float, float, float, float]]:
    out = []
    for wid, at in marks.items():
        if wid == skip:
            continue
        out.extend(mark_boxes(wid, at))
    return out


def _others(own, bodies, fixed, tags, marks, xrefs) -> list[tuple[float, float, float, float]]:
    return [*bodies, *fixed, *tags, *marks, *xrefs]


def _hits(own, others) -> bool:
    # One millimetre of air. The glyph is a little wider than the advance used
    # for the box, and a tag that only clears by a hair still reads as stacked.
    for box in own:
        for other in others:
            if boxes_overlap(box, other, pad=1.0):
                return True
    return False


def _inside(box, bounds) -> bool:
    return box[0] >= bounds[0] - 0.4 and box[1] >= bounds[1] - 0.4 and box[2] <= bounds[2] + 0.4 and box[3] <= bounds[3] + 0.4


def _comp(sheet: Sheet, cid: str):
    try:
        return sheet.component(cid)
    except KeyError:
        return None
