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
    captions: bool = True,
    peek: bool = False,
) -> ResolvedLabels:
    """Tag, wire-number, and cross-reference seats after the collision pass.

    *peek* returns the stored result or None, never running the placers.

    *captions* False is the hand-drawn I/O page, which draws the tag alone.
    Reserving room for captions it never draws left no seat for the tag.
    """

    global _COMPUTES
    cache = _CACHE.get()
    key = drawing_key(project, catalog, sheet, ladder=ladder, sheet_no=sheet_no)
    if not captions:
        key = (*key, "no-captions")
    if peek:
        hit = cache.get(key) if cache is not None else None
        return hit if hit is not None else _STORED.get(key)
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
    _avoid(sheet, catalog, project, tags, marks, xrefs, routes, ladder=ladder, sheet_no=sheet_no, captions=captions)
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
    captions: bool = True,
) -> None:
    from panelcad.render.sheet_labels import _line_number_boxes

    specs = bundle_spec_plan(sheet, routes, ladder=ladder)
    named_bodies = sheet_symbol_bodies(sheet, catalog)
    # Off-page arrows and junction nodes are not "bodies" for routing, but
    # their ink still covers text. A wire number keeps the right to sit on the
    # link its own wire ends on.
    links = _link_bodies(sheet, catalog)
    # I/O cards are solid bodies: text may not touch the card border.
    cards = [(f"card:{cid}", box) for cid, box in _hand_io_cards(sheet, catalog).items()]
    named_bodies = [*named_bodies, *cards, *links]
    bodies = [box for _name, box in named_bodies]
    body_of = {name.split(":", 1)[1]: box for name, box in named_bodies}
    # A wire number may overlap the frame of the device its wire lands on, not
    # the arrow of an off-page link: that arrow is drawn right through it.
    end_body_of = {name.split(":", 1)[1]: box for name, box in named_bodies if name.startswith("body:")}
    wx, wy, ww, wh = working_rect(sheet)
    bounds = (wx, wy, wx + ww, wy + wh)
    tx, ty, tw, th = title_block_rect(sheet)
    fixed = [box for _name, box in pin_caption_boxes(sheet, catalog)]
    fixed.extend(box for _name, box in _line_number_boxes(project, sheet, catalog, sheet_no=sheet_no))
    from panelcad.render.phase_ink import line_side_callouts

    for callout in line_side_callouts(project, sheet, catalog):
        cbox = text_box(callout.x, callout.y, callout.text, callout.size, callout.align)
        if cbox is not None:
            fixed.append(cbox)
    fixed.append((tx, ty, tx + tw, ty + th))
    fixed.extend(_rating_boxes(sheet, catalog))
    by_id = {wire.id: wire for wire in sheet.wires}

    def tag_boxes(cid: str, layout: tuple[float, float, str, float]) -> list[tuple[float, float, float, float]]:
        x, y, align, size = layout
        box = text_box(x, y, cid, size, align)
        boxes = [box] if box is not None else []
        comp = _comp(sheet, cid) if captions else None
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
    # Every drawn conductor is an obstacle for text. Only long horizontal buses
    # were before, so a tag or wire number could sit on a lead or a riser.
    # Pin leads are ink the painter adds at the pin. Text may not sit on any
    # of them, the lead of its own wire included: the lead runs at right
    # angles into the body, through a number seated beside the pin.
    lead_routes = _lead_routes(sheet, catalog)
    wire_boxes = _wire_boxes({**routes, **lead_routes})
    all_wires = [box for boxes in wire_boxes.values() for box in boxes]
    # What makes a label move: a conductor or link arrow drawn through its
    # text. Air under WIRE_CLEAR is only a preference while searching, so a
    # label that already reads cleanly stays where it was drawn.
    crossing = _wire_boxes({**routes, **lead_routes}, clear=-TOUCH_DEPTH)
    link_names = {name for name, _box in links}
    plain_bodies = [box for name, box in named_bodies if name not in link_names]
    link_ink = [_shrink(box, 1.0 + TOUCH_DEPTH) for _name, box in links]
    for _ in range(6):
        changed = False
        for cid, layout in list(tags.items()):
            own = tag_boxes(cid, layout)
            blocked_by = _others(
                own, plain_bodies, fixed, _all_tags(tags, tag_boxes, skip=cid), _all_marks(marks, mark_boxes), xref_boxes()
            )
            blocked_by.extend(conductors)
            blocked_by.extend(box for boxes in crossing.values() for box in boxes)
            blocked_by.extend(link_ink)
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
                captions=captions,
                wires=all_wires,
                rivals=[box for name, box in named_bodies if name.split(":", 1)[1] != cid],
                ink=[*(box for boxes in crossing.values() for box in boxes), *link_ink],
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
            mine = {wid}
            others.extend(box for other, boxes in wire_boxes.items() if other not in mine for box in boxes)
            ignore = _end_bodies(by_id.get(wid), end_body_of)
            trigger = _others(
                own, plain_bodies, fixed, _all_tags(tags, tag_boxes), _all_marks(marks, mark_boxes, skip=wid), xref_boxes()
            )
            trigger.extend(box for other, boxes in crossing.items() if other not in mine for box in boxes)
            trigger.extend(link_ink)
            # A number may graze its own terminals. Sitting on the symbol body
            # is still a collision. Keep the body as an obstacle on every pass
            # so a mark that just left the outline does not snap back onto it.
            on_end_body = _overlap_area(own[0], ignore) > 0.2
            if not far and not _hits(own, _without(trigger, ignore)) and not on_end_body:
                continue
            stub = False
            if pts:
                span_x = max(p.x for p in pts) - min(p.x for p in pts)
                span_y = max(p.y for p in pts) - min(p.y for p in pts)
                stub = span_x < 8.0 and span_y < 8.0
            # Far column seats are only for millimetre-scale jumpers. A long
            # rung that grazes a body stays within ON_WIRE of the conductor.
            beside = stub and (on_end_body or far)
            found = _search_on_wire(
                wid,
                at,
                pts,
                by_id,
                specs,
                ladder,
                others,
                bounds,
                [],
                route_limit=42.0 if beside else ON_WIRE,
                beside_stub=beside,
            )
            if found is not None and found != at:
                marks[wid] = found
                changed = True
        if not changed:
            break


# Clear air between text and a conductor it does not belong to (mm).
WIRE_CLEAR = 0.4


def _lead_routes(sheet: Sheet, catalog: Catalog) -> dict[str, list[Point]]:
    """Pin leads as two-point routes keyed "lead:<comp>.<pin>"."""

    from panelcad.core.pin_ink import lead_segments

    out: dict[str, list[Point]] = {}
    cards = _hand_io_cards(sheet, catalog)
    for comp in sheet.components:
        device = catalog.devices.get(comp.library_id)
        if device is None or not device.primitives or comp.id in cards:
            continue
        try:
            segs = lead_segments(device, comp)
        except (KeyError, ValueError):
            continue
        for pid, (x1, y1, x2, y2) in segs:
            out[f"lead:{comp.id}.{pid}"] = [Point(x=x1, y=y1), Point(x=x2, y=y2)]
    return out


# How far ink must reach into a text box before it counts as drawn through it.
TOUCH_DEPTH = 0.15


def _shrink(box, by: float):
    return (box[0] + by, box[1] + by, box[2] - by, box[3] - by)


def _wire_boxes(
    routes: dict[str, list[Point]], clear: float | None = None
) -> dict[str, list[tuple[float, float, float, float]]]:
    """One box per route segment, shrunk so _hits' 1 mm pad leaves WIRE_CLEAR.

    A conductor is a hairline. The full text pad around it pushed labels a
    millimetre further off every lead than they need to be.
    """

    shrink = 1.0 - (WIRE_CLEAR if clear is None else clear)
    out: dict[str, list[tuple[float, float, float, float]]] = {}
    for wid, pts in routes.items():
        boxes = []
        for a, b in zip(pts, pts[1:]):
            if hypot(b.x - a.x, b.y - a.y) < 0.05:
                continue
            boxes.append(
                (
                    min(a.x, b.x) + shrink,
                    min(a.y, b.y) + shrink,
                    max(a.x, b.x) - shrink,
                    max(a.y, b.y) - shrink,
                )
            )
        out[wid] = boxes
    return out


def _hand_io_cards(sheet: Sheet, catalog: Catalog) -> dict[str, tuple[float, float, float, float]]:
    """Drawn extent of each I/O card on a hand-drawn I/O page.

    draw_io_sheet paints the module as a card wider than its library symbol,
    with the pins on the card edge. The symbol box missed the strip between,
    so wire numbers were seated across the card border.
    """

    from panelcad.core.layout import is_generated_io_sheet, is_io_sheet

    if not is_io_sheet(sheet) or is_generated_io_sheet(sheet):
        return {}
    from panelcad.render.io_ladder import io_card_size, is_io_module

    out = {}
    for comp in sheet.components:
        device = catalog.devices.get(comp.library_id)
        if device is None or not is_io_module(device):
            continue
        w, h = io_card_size(device, comp)
        out[comp.id] = (comp.x, comp.y, comp.x + w, comp.y + h)
    return out


def _link_bodies(sheet: Sheet, catalog: Catalog) -> list[tuple[str, tuple[float, float, float, float]]]:
    from panelcad.render.sheet_labels import symbol_body_box

    out = []
    for comp in sheet.components:
        device = catalog.devices.get(comp.library_id)
        if device is None:
            continue
        cat = (device.category or "").lower()
        if not cat.startswith("connection.") or "terminal" in cat or cat.startswith("connection.notes"):
            continue
        if not device.primitives:
            continue
        out.append((f"link:{comp.id}", symbol_body_box(comp, device)))
    return out


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
    *,
    captions: bool = True,
    wires=None,
    rivals=None,
    ink=None,
):
    x, y, align, size = layout
    comp = _comp(sheet, cid)
    caption_comp = comp if captions else None

    def boxes_at(px, py, palign):
        trial = (px, py, palign, size)
        own = []
        box = text_box(px, py, cid, size, palign)
        if box is not None:
            own.append(box)
        if caption_comp is not None:
            for cx, cy, calign, csize, line in caption_layouts(caption_comp, trial):
                cbox = text_box(cx, cy, line, csize, calign)
                if cbox is not None:
                    own.append(cbox)
        return own

    others = _others([], bodies, fixed, _all_tags(tags, tag_boxes, skip=cid), _all_marks(marks, mark_boxes), xref_boxes())
    others.extend(conductors or [])
    others.extend(wires or [])
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
    if ink is not None:
        # The seat the plain search has always picked stands when no wire or
        # link arrow is drawn through it. Only a crossed seat is searched again
        # with the wires as obstacles, so clean drawings keep their layout.
        plain = [box for box in others if box not in (wires or [])] if wires else others
        first = _search_point(
            x, y, align, boxes_at, plain, bounds, prefer_up=True, owner_body=owner_body, covered_neighbors=covered
        )
        if first is not None and not _hits(boxes_at(*first), ink):
            return (first[0], first[1], first[2], size)
    if owner_body is not None:
        seat = _beside_body(boxes_at, owner_body, size, others, bounds, rivals or [], covered)
        if seat is not None:
            return (seat[0], seat[1], seat[2], size)
    found = _search_point(
        x,
        y,
        align,
        boxes_at,
        others,
        bounds,
        prefer_up=True,
        owner_body=owner_body,
        covered_neighbors=covered,
        rivals=rivals,
    )
    if found is None and (wires or rivals):
        # Nothing clears every conductor and reads as this symbol's tag. Take
        # the seat the plain search finds rather than leave the tag where it
        # was blocked; the covered-label check reports what is still crossed.
        plain = [box for box in others if box not in (wires or [])] if wires else others
        found = _search_point(
            x, y, align, boxes_at, plain, bounds, prefer_up=True, owner_body=owner_body, covered_neighbors=covered
        )
    if found is None:
        return None
    return (found[0], found[1], found[2], size)


# A tag further than this from its own symbol no longer reads as its tag.
TAG_REACH = 6.0


def _box_gap(a, b) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return hypot(dx, dy)


def _reads_as_own(box, owner_body, rivals) -> bool:
    """True when the tag is near its symbol and no other symbol is nearer."""

    own = _box_gap(box, owner_body)
    if own > TAG_REACH:
        return False
    return all(_box_gap(box, other) > own + 0.2 for other in rivals)


def _beside_body(boxes_at, body, size, others, bounds, rivals, covered=None):
    """Seats hugging the owner's body, nearest corners first.

    The generic ring search starts from the default seat. When the default
    sits between two stacked symbols every ring step lands nearer the
    neighbour, or far down the sheet, and the tag ends up beside the wrong
    device.
    """

    from panelcad.core.ink import TEXT_ASCENT, TEXT_DESCENT

    gap = 1.4
    up = size * TEXT_ASCENT
    down = size * TEXT_DESCENT
    x1, y1, x2, y2 = body
    seats = (
        (x1, y1 - gap - down, "start"),
        (x2 + gap, y1 + up, "start"),
        (x1 - gap, y1 + up, "end"),
        (x2 + gap, y2 - down, "start"),
        (x1 - gap, y2 - down, "end"),
        ((x1 + x2) / 2.0, y2 + gap + up, "middle"),
        (x1, y2 + gap + up, "start"),
    )
    blocking = others
    if covered:
        blocking = [box for box in others if tuple(box) not in covered]
    for sx, sy, align in seats:
        boxes = boxes_at(sx, sy, align)
        if not boxes or any(not _inside(box, bounds) for box in boxes):
            continue
        if _hits(boxes, blocking):
            continue
        if not _reads_as_own(boxes[0], body, rivals):
            continue
        return (sx, sy, align)
    return None


def _search_on_wire(
    wid,
    at,
    pts,
    by_id,
    specs,
    ladder,
    others,
    bounds,
    ignore=None,
    route_limit: float = ON_WIRE,
    beside_stub: bool = False,
):
    """A seat on this wire. A blocked seat still beats a number left off the run."""

    wire = by_id.get(wid)
    if wire is None or len(pts) < 2:
        return None
    text = wire.drawn_mark(ladder=ladder)
    if not text:
        return None
    spec = specs.get(wid, "")
    obstacles = _without(others, ignore or [])
    seats = list(_seats(pts) or _short_seats(pts))
    seats.append((at[0], at[1], at[2]))
    if beside_stub:
        # A 1 mm stub between two fat symbols has no on-wire seat that
        # clears the bodies. Park the number beside the column.
        seats.extend(_body_clear_seats(pts))
    best_clear = None
    best_rank = 10**9
    best_overlap = None
    best_area = 10**9
    best_overlap_rank = 10**9
    for sx, sy, align in seats:
        on_route = (
            _clamped_route_distance(sx, sy, pts) <= route_limit
            if beside_stub
            else anchor_on_route(sx, sy, pts, limit=route_limit)
        )
        # Keep a millimetre-scale jumper's current seat even when it sits
        # beside the bodies. A long rung's current seat still has to lie
        # on the conductor; otherwise a far mark stays parked off the wire.
        is_current = beside_stub and abs(sx - at[0]) < 0.05 and abs(sy - at[1]) < 0.05
        if not is_current and not on_route:
            continue
        box = mark_box(sx, sy, text, align=align, spec=spec)
        rank = abs(sx - at[0]) + abs(sy - at[1]) + (0.0 if _inside(box, bounds) else 30.0)
        if _inside(box, bounds) and not _hits([box], obstacles):
            if rank < best_rank:
                best_clear = (sx, sy, align)
                best_rank = rank
            continue
        # Crossing a conductor counts like a square millimetre of body. The
        # shrunk wire boxes have no area of their own.
        area = _overlap_area(box, obstacles) + sum(
            1.0 for other in obstacles if (other[0] > other[2] or other[1] > other[3]) and _hits([box], [other])
        )
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
    return _offset_seats(mx, my, abs(a.x - b.x) <= abs(a.y - b.y))


def _body_clear_seats(pts: list[Point]) -> list[tuple[float, float, str]]:
    """Seats far enough beside a fat symbol that a stub's number can leave the body."""

    if len(pts) < 2:
        return []
    span_x = max(p.x for p in pts) - min(p.x for p in pts)
    span_y = max(p.y for p in pts) - min(p.y for p in pts)
    vertical = span_y >= span_x
    # A millimetre-scale jumper between stacked devices may jog either way.
    # Offer both axes so L2 at the column centre can leave a 40 mm breaker.
    stub = span_x < 8.0 and span_y < 8.0
    anchors = [((a.x + b.x) * 0.5, (a.y + b.y) * 0.5) for a, b in zip(pts, pts[1:])]
    anchors.append((sum(p.x for p in pts) / len(pts), sum(p.y for p in pts) / len(pts)))
    seats: list[tuple[float, float, str]] = []
    for ax, ay in anchors:
        for d in (8.0, 12.0, 16.0, 22.0, 28.0, 34.0, 40.0):
            if vertical or stub:
                seats.append((ax - d, ay, "end"))
                seats.append((ax + d, ay, "start"))
            if (not vertical) or stub:
                seats.append((ax, ay - d, "middle"))
                seats.append((ax, ay + d, "start"))
    return seats


def _offset_seats(mx: float, my: float, vertical: bool) -> list[tuple[float, float, str]]:
    seats: list[tuple[float, float, str]] = []
    for d in (3.2, 5.5, 7.5, 9.5, 11.4):
        if vertical:
            seats.append((mx - d, my, "end"))
            seats.append((mx + d * 0.85, my, "start"))
        else:
            seats.append((mx, my - d, "middle"))
            seats.append((mx, my + d * 0.85, "start"))
    seats.append((mx, my, "middle"))
    return seats


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
    rivals=None,
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
            if owner_body is not None and rivals is not None and not _reads_as_own(boxes[0], owner_body, rivals):
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
            seats.extend(_offset_seats(mx, my, vertical))
    return seats


def _route_distance(x: float, y: float, pts: list[Point]) -> float:
    best = 10**9
    for a, b in zip(pts, pts[1:]):
        best = min(best, _segment_distance(x, y, a.x, a.y, b.x, b.y))
    return best


def _clamped_route_distance(x: float, y: float, pts: list[Point]) -> float:
    """Distance to the nearest point of the polyline, including the ends.

    A 1 mm stub between two symbols has almost no interior, so a number
    parked beside it must still count as on that wire.
    """

    best = 10**9
    for a, b in zip(pts, pts[1:]):
        dx, dy = b.x - a.x, b.y - a.y
        length2 = dx * dx + dy * dy
        if length2 <= 1e-6:
            best = min(best, hypot(x - a.x, y - a.y))
            continue
        t = max(0.0, min(1.0, ((x - a.x) * dx + (y - a.y) * dy) / length2))
        best = min(best, hypot(x - (a.x + t * dx), y - (a.y + t * dy)))
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


@dataclass(frozen=True)
class CoveredLabel:
    kind: str  # "tag" or "mark"
    owner: str  # component id or wire id
    text: str
    x: float
    y: float
    by: tuple[str, ...]


def _segment_crosses_box(a: Point, b: Point, box, inset: float = 0.15) -> bool:
    x1, y1, x2, y2 = box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset
    if x1 >= x2 or y1 >= y2:
        return False
    # Routes are orthogonal. A diagonal is checked by its bounding box, which
    # can only over-report.
    return not (max(a.x, b.x) < x1 or min(a.x, b.x) > x2 or max(a.y, b.y) < y1 or min(a.y, b.y) > y2)


def covered_labels(
    project: Project,
    catalog: Catalog,
    sheet: Sheet,
    *,
    ladder: bool = False,
    sheet_no: int = 1,
    captions: bool = True,
    peek: bool = False,
) -> list[CoveredLabel] | None:
    """Tags and wire numbers that a foreign conductor or symbol body still covers.

    The collision pass moves what it can. What is left here had no clear seat
    and is a drawing problem for a person to fix (move a device, open a gap).
    """

    resolved = resolve_labels(project, catalog, sheet, ladder=ladder, sheet_no=sheet_no, captions=captions, peek=peek)
    if resolved is None:
        return None
    named = [*sheet_symbol_bodies(sheet, catalog), *_link_bodies(sheet, catalog)]
    body_of = {name.split(":", 1)[1]: box for name, box in named}
    by_id = {wire.id: wire for wire in sheet.wires}
    specs = bundle_spec_plan(sheet, resolved.routes, ladder=ladder)
    out: list[CoveredLabel] = []

    leads = _lead_routes(sheet, catalog)

    def crossers(box, *, skip_wire: str = "", skip_bodies: tuple = ()) -> list[str]:
        hits: list[str] = []
        for wid, pts in {**resolved.routes, **leads}.items():
            if wid == skip_wire:
                continue
            if any(_segment_crosses_box(a, b, box) for a, b in zip(pts, pts[1:])):
                hits.append(f"wire {wid}")
        for cid, body in body_of.items():
            if cid in skip_bodies:
                continue
            if boxes_overlap(
                (box[0] + 0.15, box[1] + 0.15, box[2] - 0.15, box[3] - 0.15), body
            ) and not _contains(body, box):
                hits.append(f"symbol {cid}")
        return hits

    for cid, (x, y, align, size) in resolved.tags.items():
        box = text_box(x, y, cid, size, align)
        if box is None:
            continue
        hits = crossers(box, skip_bodies=(cid,))
        if hits:
            out.append(CoveredLabel("tag", cid, cid, x, y, tuple(hits)))
    for wid, (x, y, align) in resolved.marks.items():
        wire = by_id.get(wid)
        if wire is None:
            continue
        text = wire.drawn_mark(ladder=ladder)
        if not text:
            continue
        box = mark_box(x, y, text, align=align, spec=specs.get(wid, ""))
        ends = []
        from panelcad.render.sheet import parse_pin_ref

        for end in (wire.frm, wire.to):
            try:
                ref = parse_pin_ref(end)[0]
            except ValueError:
                continue
            if ref in body_of and not any(name == f"link:{ref}" for name, _box in named):
                ends.append(ref)
        hits = crossers(box, skip_wire=wid, skip_bodies=tuple(ends))
        if hits:
            out.append(CoveredLabel("mark", wid, text, x, y, tuple(hits)))
    return out


def _contains(outer, inner) -> bool:
    """Text wholly inside a body is the body's own legend, not covered by it."""

    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]
