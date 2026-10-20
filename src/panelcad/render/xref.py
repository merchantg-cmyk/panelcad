"""Place and draw derived coil/contact/off-page references.

Uses the same obstacle boxes as wire-number labels so xref text stays off
symbols, tags, wires, wire numbers, and ladder rails.
"""

from __future__ import annotations

from math import floor

from panelcad.core.models import Project, Sheet
from panelcad.core.xref import XrefAnnotation, xref_annotations
from panelcad.library.catalog import Catalog
from panelcad.render.painter import Painter
from panelcad.render.titleblock import title_block_rect, working_rect
from panelcad.render.wire_labels import (
    MARK_SIZE,
    boxes_overlap,
    mark_box,
    place_wire_marks,
)
from panelcad.render.wiring import route_sheet_wires

XREF_SIZE = 2.0
LINE_H = 2.6
# Narrow enough to sit on the open side of a 26 mm header cell, clear of the
# coil's 0V drop and of the next column's drop. Row pitch clears a 3-row
# table between the coil and the return rail.
MIRROR_ROW_H = 6.4
MIRROR_W = 20.0
CLEAR_TRY = (
    (0.0, 0.0),
    (0.0, 3.6),
    (0.0, 7.2),
    (8.0, 0.0),
    (12.0, 0.0),
    (16.0, 2.0),
    (20.0, 0.0),
    (24.0, 3.0),
    (32.0, 0.0),
    (40.0, 0.0),
    (0.0, 12.0),
    (0.0, 20.0),
    (0.0, 28.0),
    (-8.0, 0.0),
    (-12.0, 2.4),
    (-16.0, 0.0),
    (0.0, -4.0),
    (8.0, 6.0),
    (14.0, 8.0),
    (6.0, -6.0),
    (-6.0, 6.0),
)
# Coil tables stay under the coil. The 0V drop runs through the coil center,
# so the table steps sideways just far enough to clear it. A ±160 mm search
# was dropping K1's table into the bottom-left corner. -26 mm is the highest
# step that still keeps the table top within 28 mm of the anchor.
_NEAR_DX = tuple(float(value) for value in range(-80, 84, 4))
_NEAR_DY = tuple(float(value) for value in range(0, 144, 4)) + tuple(
    float(value) for value in range(-4, -28, -4)
) + (-26.0,)
NEAR_TRY = tuple(
    sorted(
        ((dx, dy) for dy in _NEAR_DY for dx in _NEAR_DX),
        key=lambda item: (0 if item[1] >= -1.0 else 1, abs(item[0]) + abs(item[1]), abs(item[0])),
    )
)
MODEST_TRY = tuple(
    (float(dx), dy)
    for dx in range(-40, 48, 8)
    for dy in (0.0, 8.0, 16.0, 28.0, 40.0, -12.0, -24.0, -40.0)
)


def _label_box(x: float, y: float, lines: list[str], *, align: str, size: float) -> tuple[float, float, float, float]:
    text = max(lines, key=len) if lines else ""
    w = max(2.2, len(text) * size * 0.64)
    h = max(LINE_H, len(lines) * LINE_H)
    if align == "end":
        x0 = x - w
    elif align == "middle":
        x0 = x - w / 2.0
    else:
        x0 = x
    y0 = y - size * 0.9
    return (x0 - 0.3, y0 - 0.2, x0 + w + 0.3, y0 + h + 0.4)


def mirror_size(ann: XrefAnnotation) -> tuple[float, float]:
    rows = list(ann.mirror_rows or [])
    n = max(1, len(rows))
    return (MIRROR_W, 2.2 + n * MIRROR_ROW_H)


def annotation_box(ann: XrefAnnotation, x: float, y: float, align: str) -> tuple[float, float, float, float]:
    if ann.kind == "mirror" and ann.mirror_rows:
        w, h = mirror_size(ann)
        x0 = x - w / 2.0
        return (x0 - 0.4, y - 1.0, x0 + w + 0.4, y + h + 0.4)
    return _label_box(x, y, ann.lines, align=align, size=ann.size)


def _seat_metrics(ann: XrefAnnotation) -> tuple[bool, float, float, float]:
    """Width and height shared by every seat of this annotation."""

    if ann.kind == "mirror" and ann.mirror_rows:
        w, h = mirror_size(ann)
        return True, w, h, 0.0
    text = max(ann.lines, key=len) if ann.lines else ""
    size = float(ann.size)
    w = max(2.2, len(text) * size * 0.64)
    h = max(LINE_H, len(ann.lines) * LINE_H)
    return False, w, h, size


def _seat_box(
    x: float,
    y: float,
    align: str,
    w: float,
    h: float,
    size: float,
    mirror: bool,
) -> tuple[float, float, float, float]:
    """Same rectangle as ``annotation_box`` for a known width and height."""

    if mirror:
        x0 = x - w / 2.0
        return (x0 - 0.4, y - 1.0, x0 + w + 0.4, y + h + 0.4)
    if align == "end":
        x0 = x - w
    elif align == "middle":
        x0 = x - w / 2.0
    else:
        x0 = x
    y0 = y - size * 0.9
    return (x0 - 0.3, y0 - 0.2, x0 + w + 0.3, y0 + h + 0.4)


def _symbol_boxes(
    sheet: Sheet,
    catalog: Catalog,
    *,
    ladder: bool = False,
    project: Project | None = None,
) -> list[tuple[float, float, float, float]]:
    from panelcad.core.ink import device_ref_layout, text_box
    from panelcad.render.sheet_labels import place_device_tags

    boxes: list[tuple[float, float, float, float]] = []
    align = "middle" if ladder else "start"
    # Tags avoid cross-references, so this pass must not ask for them again.
    seated = place_device_tags(
        sheet, catalog, ladder=ladder, project=project, include_xref=False
    )
    for comp in sheet.components:
        device = catalog.devices.get(comp.library_id)
        if device is None:
            continue
        s = comp.visual_scale()
        w = device.schematic_width * s
        h = device.schematic_height * s
        from panelcad.core.route import component_aabb

        box = component_aabb(comp.x, comp.y, w, h, int(comp.rotation or 0), bool(comp.mirror))
        boxes.append((box[0] - 0.6, box[1] - 0.6, box[2] + 0.6, box[3] + 0.6))
        layout = device_ref_layout(device, comp, ref_align=align)
        if layout is not None:
            x, y, talign, size = layout
            boxes.append(mark_box(x, y, comp.id, align=talign, size=size))
        sat = seated.get(comp.id)
        if sat is not None and sat != layout:
            moved = text_box(sat[0], sat[1], comp.id, sat[3], sat[2])
            if moved is not None:
                boxes.append(moved)
    return boxes


def _ink_text_boxes(
    sheet: Sheet,
    catalog: Catalog,
    *,
    ladder: bool = False,
) -> list[tuple[float, float, float, float]]:
    """Pin numbers, nameplates, extra captions, and tags — not symbol AABBs.

    IEC contact-mirror placement must not see these boxes (K1/K3 pixel lock).
    Location lists do, so /2.6 cannot sit on a kW nameplate or a pin caption.
    """

    from panelcad.core.ink import device_ref_layout, text_box
    from panelcad.core.pins import rating_caption_positions
    from panelcad.render.sheet_labels import caption_layouts, pin_caption_boxes

    boxes: list[tuple[float, float, float, float]] = []
    for _name, box in pin_caption_boxes(sheet, catalog):
        boxes.append(box)
    align = "middle" if ladder else "start"
    for comp in sheet.components:
        device = catalog.devices.get(comp.library_id)
        if device is None:
            continue
        layout = device_ref_layout(device, comp, ref_align=align)
        if layout is not None:
            tag = text_box(layout[0], layout[1], comp.id, layout[3], layout[2])
            if tag is not None:
                boxes.append(tag)
        for cx, cy, calign, csize, line in caption_layouts(comp, layout):
            cap = text_box(cx, cy, line, csize, calign)
            if cap is not None:
                boxes.append(cap)
        scale = comp.visual_scale()
        for cx, cy, line in rating_caption_positions(
            comp, device.schematic_height, scale, device.schematic_width
        ):
            rating = text_box(cx, cy, line, 2.2, "start")
            if rating is not None:
                boxes.append(rating)
    return boxes


def _wire_boxes(
    sheet: Sheet,
    catalog: Catalog,
    ladder: bool,
    project: Project,
) -> list[tuple[float, float, float, float]]:
    try:
        routes = route_sheet_wires(sheet, catalog, project=project)
    except (KeyError, ValueError, IndexError):
        return [], []
    from panelcad.render.wire_labels import bundle_spec_plan

    marks = place_wire_marks(sheet, catalog, routes, ladder=ladder, project=project)
    specs = bundle_spec_plan(sheet, routes, ladder=ladder)
    boxes: list[tuple[float, float, float, float]] = []
    strokes: list[tuple[float, float, float, float]] = []
    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        for a, b in zip(pts, pts[1:]):
            stroke = (
                min(a.x, b.x) - 0.45,
                min(a.y, b.y) - 0.45,
                max(a.x, b.x) + 0.45,
                max(a.y, b.y) + 0.45,
            )
            strokes.append(stroke)
            boxes.append(stroke)
        at = marks.get(wire.id)
        mark = wire.drawn_mark(ladder=ladder)
        if at and mark:
            boxes.append(
                mark_box(at[0], at[1], mark, align=at[2], spec=specs.get(wire.id, ""), size=MARK_SIZE)
            )
    return boxes, strokes


def _rail_boxes(sheet: Sheet, *, ladder: bool) -> list[tuple[float, float, float, float]]:
    from panelcad.core.ladder import RAIL_INSET, iec_rail_ys

    if str(getattr(sheet, "drawing_style", "") or "") == "iec":
        wx, _wy, ww, _wh = working_rect(sheet)
        top, bot = iec_rail_ys(sheet)
        # The frame already stops 2.6 mm above the return rail. A pad that
        # wide makes every in-frame table read as sitting on the rail, so the
        # search climbs onto the coil.
        pad = 1.0
        return [
            (wx, top - pad, wx + ww, top + pad),
            (wx, bot - pad, wx + ww, bot + pad),
        ]
    if not ladder:
        return []

    wx, wy, ww, wh = working_rect(sheet)
    left = wx + RAIL_INSET
    right = wx + ww - RAIL_INSET
    top = wy + 8.0
    bot = wy + wh - 6.0
    pad = 2.6
    return [
        (left - pad, top, left + pad, bot),
        (right - pad, top, right + pad, bot),
    ]


def _frame_box(sheet: Sheet) -> tuple[float, float, float, float]:
    wx, wy, ww, wh = working_rect(sheet)
    bottom = wy + wh - 1.0
    if str(getattr(sheet, "drawing_style", "") or "") == "iec":
        from panelcad.core.ladder import iec_rail_ys

        _top, rail = iec_rail_ys(sheet)
        # The return rail is the last line above the border. A table whose
        # last row sits on that line reads as crossing the frame.
        bottom = min(bottom, rail - 2.6)
    return (wx + 1.0, wy + 1.0, wx + ww - 1.0, bottom)


def _title_obstacle(sheet: Sheet) -> tuple[float, float, float, float]:
    """Title strip as x0, y0, x1, y1. title_block_rect is x, y, width, height."""

    x, y, w, h = title_block_rect(sheet)
    return (x, y, x + w, y + h)


def _tuck_dxs(ann: XrefAnnotation, sheet: Sheet) -> list[float]:
    """Horizontal offsets that keep the table centre in the coil's header cell."""

    from panelcad.core.zones import column_from_header, header_columns

    w, _h = mirror_size(ann)
    half = w / 2.0 + 0.4
    gap = 0.75 + half
    col = column_from_header(sheet, ann.anchor_x)
    cells = header_columns(sheet)
    if not cells:
        return []
    _index, x0, x1, _center = cells[col - 1]
    lo, hi = x0 + 0.2, x1 - 0.05
    dxs: list[float] = []
    for center in (ann.anchor_x - gap, ann.anchor_x + gap):
        if lo <= center < hi:
            dxs.append(center - ann.anchor_x)
    return dxs


# Set while place_xref_labels is choosing seats, so a wire-number pocket
# can ask for the real tables without calling placement again.
_PLACING = False


def iec_mirror_pockets(project: Project, catalog: Catalog, sheet: Sheet) -> list[tuple[float, float, float, float]]:
    """Where an IEC contact table will sit, so a wire number does not land there first."""

    if str(getattr(sheet, "drawing_style", "") or "") != "iec":
        return []
    if not _PLACING:
        placed = place_xref_labels(project, catalog, sheet, ladder=True)
        return [
            annotation_box(ann, x, y, align)
            for ann, x, y, align in placed
            if ann.kind == "mirror" and ann.mirror_rows
        ]
    from panelcad.render.wiring import route_sheet_wires

    frame = _frame_box(sheet)
    symbols = _symbol_boxes(sheet, catalog, ladder=True)
    try:
        routes = route_sheet_wires(sheet, catalog)
    except (KeyError, ValueError, IndexError):
        routes = {}
    wires: list[tuple[float, float, float, float]] = []
    for pts in routes.values():
        for a, b in zip(pts, pts[1:]):
            wires.append(
                (
                    min(a.x, b.x) - 0.45,
                    min(a.y, b.y) - 0.45,
                    max(a.x, b.x) + 0.45,
                    max(a.y, b.y) + 0.45,
                )
            )
    pockets: list[tuple[float, float, float, float]] = []
    for ann in xref_annotations(project, catalog, sheet):
        if ann.kind != "mirror" or not ann.mirror_rows:
            continue
        _w, h = mirror_size(ann)
        y_fit = min(ann.anchor_y, frame[3] - 0.05 - h - 0.4)
        for dx in _tuck_dxs(ann, sheet):
            box = annotation_box(ann, ann.anchor_x + dx, y_fit, "middle")
            if not _inside(box, frame):
                continue
            if any(boxes_overlap(box, other, pad=0.15) for other in symbols):
                continue
            if any(boxes_overlap(box, other, pad=0.15) for other in wires):
                continue
            pockets.append(box)
            break
    return pockets


def _iec_mirror_tries(
    ann: XrefAnnotation,
    sheet: Sheet,
    frame: tuple[float, float, float, float],
) -> tuple[tuple[float, float], ...]:
    """Seats under an IEC coil: beside the drop when that stays in the column."""

    _w, h = mirror_size(ann)
    dxs = _tuck_dxs(ann, sheet)
    # box bottom is y + h + 0.4. The highest seat that still fits in the frame
    # is the one a 3-row table needs when the coil sits on the return rail.
    # Half-millimetre steps under the coil catch the gap between this coil and
    # the next one. A 2 mm grid skipped that gap and stacked K4 under K3.
    y_top = frame[3] - 0.05 - h - 0.4
    dys = [index * 0.5 for index in range(0, 25)]
    dys.extend(float(value) for value in range(-16, 0, 2))
    dys.extend(float(value) for value in range(14, 80, 2))
    dys.append(y_top - ann.anchor_y)
    dys.append(0.0)
    seen: set[tuple[float, float]] = set()
    items: list[tuple[float, float]] = []
    for dx, dy in list(NEAR_TRY) + [(dx, dy) for dx in dxs for dy in dys]:
        key = (round(dx, 2), round(dy, 2))
        if key in seen:
            continue
        seen.add(key)
        items.append((float(dx), float(dy)))
    items.sort(key=lambda item: (0 if item[1] >= -1.0 else 1, abs(item[0]) + abs(item[1]), abs(item[0])))
    return tuple(items)


def _stacked_on_mirror(
    box: tuple[float, float, float, float],
    others: list[tuple[float, float, float, float]],
) -> bool:
    """True when this table shares an x range with another table.

    A vertical gap does not separate them: K4 sat 7 mm under K3 and still
    read as one stack. Tables under different coils, in different header
    columns, stay separate when a clear gap remains between their x ranges.
    """

    for other in others:
        if box[2] <= other[0] + 0.05 or other[2] <= box[0] + 0.05:
            continue
        return True
    return False


def _inside(box: tuple[float, float, float, float], frame: tuple[float, float, float, float]) -> bool:
    return box[0] >= frame[0] and box[1] >= frame[1] and box[2] <= frame[2] and box[3] <= frame[3]


def _clamp_into(
    ann: XrefAnnotation,
    x: float,
    y: float,
    align: str,
    frame: tuple[float, float, float, float],
) -> tuple[float, float]:
    """Slide a table back inside the drawing frame, above the title block."""

    box = annotation_box(ann, x, y, align)
    dx = 0.0
    dy = 0.0
    if box[0] < frame[0]:
        dx += frame[0] - box[0]
    if box[2] + dx > frame[2]:
        dx -= box[2] + dx - frame[2]
    if box[1] < frame[1]:
        dy += frame[1] - box[1]
    if box[3] + dy > frame[3]:
        dy -= box[3] + dy - frame[3]
    return x + dx, y + dy


class _BoxIndex:
    """Overlap counts that match ``boxes_overlap`` including duplicate boxes."""

    def __init__(
        self,
        boxes: list[tuple[float, float, float, float]] | None = None,
        *,
        cell: float = 12.0,
    ) -> None:
        self.cell = cell
        self.boxes: list[tuple[float, float, float, float]] = []
        self.grid: dict[tuple[int, int], list[int]] = {}
        self._stamp: list[int] = []
        self._tick = 0
        for box in boxes or []:
            self.add(box)

    def add(self, box: tuple[float, float, float, float]) -> None:
        idx = len(self.boxes)
        self.boxes.append(box)
        self._stamp.append(0)
        cell = self.cell
        i0 = floor(box[0] / cell)
        i1 = floor(box[2] / cell)
        j0 = floor(box[1] / cell)
        j1 = floor(box[3] / cell)
        grid = self.grid
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                bucket = grid.get((i, j))
                if bucket is None:
                    grid[(i, j)] = [idx]
                else:
                    bucket.append(idx)

    def count(
        self,
        box: tuple[float, float, float, float],
        pad: float,
        limit: int | None = None,
    ) -> int:
        """How many stored boxes overlap *box*.

        Stops once the count passes *limit*. The returned value is exact when
        it is less than or equal to *limit*.
        """

        boxes = self.boxes
        if not boxes:
            return 0
        self._tick += 1
        if self._tick == 2**30:
            self._stamp = [0] * len(boxes)
            self._tick = 1
        tick = self._tick
        stamp = self._stamp
        cell = self.cell
        x0 = box[0] - pad
        y0 = box[1] - pad
        x1 = box[2] + pad
        y1 = box[3] + pad
        i0 = floor(x0 / cell)
        i1 = floor(x1 / cell)
        j0 = floor(y0 / cell)
        j1 = floor(y1 / cell)
        # boxes_overlap is true when each gap is strictly under pad.
        ax0, ay0, ax1, ay1 = box
        n = 0
        grid = self.grid
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                bucket = grid.get((i, j))
                if not bucket:
                    continue
                for idx in bucket:
                    if stamp[idx] == tick:
                        continue
                    stamp[idx] = tick
                    bx0, by0, bx1, by1 = boxes[idx]
                    if ax1 + pad <= bx0 or bx1 + pad <= ax0 or ay1 + pad <= by0 or by1 + pad <= ay0:
                        continue
                    n += 1
                    if limit is not None and n > limit:
                        return n
        return n

    def overlap_ids(
        self,
        box: tuple[float, float, float, float],
        pad: float = 0.0,
    ) -> set[int]:
        """Indexes of stored boxes that overlap *box*, same test as ``count``."""

        boxes = self.boxes
        if not boxes:
            return set()
        self._tick += 1
        if self._tick == 2**30:
            self._stamp = [0] * len(boxes)
            self._tick = 1
        tick = self._tick
        stamp = self._stamp
        cell = self.cell
        i0 = floor((box[0] - pad) / cell)
        i1 = floor((box[2] + pad) / cell)
        j0 = floor((box[1] - pad) / cell)
        j1 = floor((box[3] + pad) / cell)
        ax0, ay0, ax1, ay1 = box
        found: set[int] = set()
        grid = self.grid
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                bucket = grid.get((i, j))
                if not bucket:
                    continue
                for idx in bucket:
                    if stamp[idx] == tick:
                        continue
                    stamp[idx] = tick
                    bx0, by0, bx1, by1 = boxes[idx]
                    if ax1 + pad <= bx0 or bx1 + pad <= ax0 or ay1 + pad <= by0 or by1 + pad <= ay0:
                        continue
                    found.add(idx)
        return found

    def boxes_at(self, x: float, y: float) -> list[tuple[float, float, float, float]]:
        """Stored boxes whose cells contain the point. The caller tests geometry."""

        bucket = self.grid.get((floor(x / self.cell), floor(y / self.cell)))
        if not bucket:
            return []
        boxes = self.boxes
        return [boxes[idx] for idx in bucket]


def _count_sum(
    primary: _BoxIndex,
    extra: _BoxIndex,
    box: tuple[float, float, float, float],
    pad: float,
    limit: int | None,
) -> int:
    """Sum of two indexes. Exact when the result is within *limit*."""

    if limit is None:
        return primary.count(box, pad) + extra.count(box, pad)
    n = primary.count(box, pad, limit=limit)
    if n > limit:
        return n
    return n + extra.count(box, pad, limit=limit - n)


def place_xref_labels(
    project: Project,
    catalog: Catalog,
    sheet: Sheet,
    *,
    ladder: bool = False,
) -> list[tuple[XrefAnnotation, float, float, str]]:
    """Annotations on *sheet* with a clear (x, y, align)."""

    global _PLACING
    outer = not _PLACING
    _PLACING = True
    try:
        from panelcad.render.wire_labels import share_wire_marks

        with share_wire_marks():
            return _place_xref_labels(project, catalog, sheet, ladder=ladder)
    finally:
        if outer:
            _PLACING = False


def _place_xref_labels(
    project: Project,
    catalog: Catalog,
    sheet: Sheet,
    *,
    ladder: bool = False,
) -> list[tuple[XrefAnnotation, float, float, str]]:
    anns = [item for item in xref_annotations(project, catalog, sheet) if item.lines or item.mirror_rows]
    # Mirrors claim the coil column before a contact reference does. A contact
    # ref that lands on that seat makes the table step down onto the drop the
    # router already jogged around the original seat.
    anns = [item for item in anns if item.kind == "mirror"] + [item for item in anns if item.kind != "mirror"]
    frame = _frame_box(sheet)
    title = _title_obstacle(sheet)
    symbols = _symbol_boxes(sheet, catalog, ladder=ladder, project=project)
    ink_text = _ink_text_boxes(sheet, catalog, ladder=ladder)
    wire_boxes, _stroke_boxes = _wire_boxes(sheet, catalog, ladder, project)
    obstacles = symbols + wire_boxes + _rail_boxes(sheet, ladder=ladder) + [title]
    placed: list[tuple[XrefAnnotation, float, float, str]] = []
    placed_boxes: list[tuple[float, float, float, float]] = []
    placed_mirrors: list[tuple[float, float, float, float]] = []
    placed_index = _BoxIndex()
    symbol_index = _BoxIndex(symbols)
    ink_index = _BoxIndex(ink_text)
    obstacle_index = _BoxIndex(obstacles)
    iec_sheet = str(getattr(sheet, "drawing_style", "") or "") == "iec"
    column_from_header = None
    if iec_sheet:
        from panelcad.core.zones import column_from_header
    for ann in anns:
        align = "middle" if ann.prefer == "below" or ann.kind == "mirror" else "start"
        if ann.prefer == "left":
            align = "end"
        iec_mirror = ann.kind == "mirror" and str(getattr(sheet, "drawing_style", "") or "") == "iec"
        if iec_mirror:
            tries = _iec_mirror_tries(ann, sheet, frame)
        elif ann.kind == "mirror" or ann.prefer == "below":
            tries = NEAR_TRY
        else:
            tries = CLEAR_TRY + MODEST_TRY
            if ann.prefer == "right":
                tries = tuple(item for item in tries if item[0] >= 0) + tries
            elif ann.prefer == "left":
                tries = tuple(item for item in tries if item[0] <= 0) + tries
        chosen = (ann.anchor_x, ann.anchor_y, align)
        best_key = (10**6, 10**6, 10**6, 10**6, 10**6, 10**6, 10**6)
        mirror_seat, seat_w, seat_h, seat_size = _seat_metrics(ann)
        fx0, fy0, fx1, fy1 = frame
        title_box = title
        # Clamped tries land on the same edge box many times. The count for
        # one box and one early-abort limit does not change during this search.
        # A later try of that box only matters when its offset is shorter.
        seen: dict[tuple, int] = {}
        best_dist: dict[tuple[float, float, float, float], float] = {}
        for dx, dy in tries:
            x = ann.anchor_x + dx
            y = ann.anchor_y + dy
            box = _seat_box(x, y, align, seat_w, seat_h, seat_size, mirror_seat)
            cdx = 0.0
            cdy = 0.0
            if box[0] < fx0:
                cdx = fx0 - box[0]
            if box[2] + cdx > fx1:
                cdx -= box[2] + cdx - fx1
            if box[1] < fy0:
                cdy = fy0 - box[1]
            if box[3] + cdy > fy1:
                cdy -= box[3] + cdy - fy1
            if cdx != 0.0 or cdy != 0.0:
                x += cdx
                y += cdy
                box = _seat_box(x, y, align, seat_w, seat_h, seat_size, mirror_seat)
            if not _inside(box, frame) or boxes_overlap(box, title_box, pad=0.4):
                continue
            if ann.kind == "mirror" and box[1] < ann.anchor_y - 28.0:
                continue
            # A table on the coil loses to one that only crosses a wire number.
            # Prefer a clear spot under the coil, then any clear spot.
            below = 0 if y + 1.0 >= ann.anchor_y - 2.0 else 1
            dist = abs(dx) + abs(dy)
            previous = best_dist.get(box)
            if previous is not None and dist >= previous:
                continue
            best_dist[box] = dist
            ann_key = (box, 0, -1)
            ann_hits = seen.get(ann_key)
            if ann_hits is None:
                ann_hits = placed_index.count(box, 0.3)
                seen[ann_key] = ann_hits
            # Do not rank the straight coil drop ahead of the header column.
            # route_sheet_wires jogs that drop around the table after this
            # search. Ranking the drop first moved the table, and the jog then
            # missed it. Contact refs use the same order so one of them does
            # not occupy the coil seat and push the table onto the drop.
            # Counts stop once they already lose to the best seat. A stored
            # key only keeps a count that was exact.
            if iec_mirror:
                stacked = 1 if _stacked_on_mirror(box, placed_mirrors) else 0
                column_miss = 0
                if column_from_header is not None:
                    cx = (box[0] + box[2]) * 0.5
                    if column_from_header(sheet, cx) != column_from_header(sheet, ann.anchor_x):
                        column_miss = 1
                if (ann_hits, stacked) > best_key[:2]:
                    continue
                sym_limit = None if (ann_hits, stacked) < best_key[:2] else best_key[2]
                sym_key = (box, 1, -1 if sym_limit is None else sym_limit)
                symbol_hits = seen.get(sym_key)
                if symbol_hits is None:
                    symbol_hits = symbol_index.count(box, 0.15, limit=sym_limit)
                    seen[sym_key] = symbol_hits
                if (ann_hits, stacked, symbol_hits) > best_key[:3]:
                    continue
                if (ann_hits, stacked, symbol_hits, column_miss) > best_key[:4]:
                    continue
                prefix_better = (ann_hits, stacked, symbol_hits, column_miss) < best_key[:4]
                if (
                    not prefix_better
                    and best_key[4] == 0
                    and (below, dist) >= (best_key[5], best_key[6])
                ):
                    continue
                hit_limit = None if prefix_better else best_key[4]
                hit_key = (box, 2, -1 if hit_limit is None else hit_limit)
                hits = seen.get(hit_key)
                if hits is None:
                    hits = obstacle_index.count(box, 0.15, limit=hit_limit)
                    seen[hit_key] = hits
                key = (ann_hits, stacked, symbol_hits, column_miss, hits, below, dist)
            else:
                if ann_hits > best_key[1]:
                    continue
                sym_limit = None if ann_hits < best_key[1] else best_key[2]
                sym_key = (box, 3, -1 if sym_limit is None else sym_limit)
                symbol_hits = seen.get(sym_key)
                if symbol_hits is None:
                    symbol_hits = _count_sum(symbol_index, ink_index, box, 0.15, sym_limit)
                    seen[sym_key] = symbol_hits
                if (0, ann_hits, symbol_hits) > best_key[:3]:
                    continue
                prefix_better = (0, ann_hits, symbol_hits) < best_key[:3]
                if (
                    not prefix_better
                    and best_key[3] == 0
                    and (below, dist) >= (best_key[4], best_key[5])
                ):
                    continue
                hit_limit = None if prefix_better else best_key[3]
                hit_key = (box, 4, -1 if hit_limit is None else hit_limit)
                hits = seen.get(hit_key)
                if hits is None:
                    hits = _count_sum(obstacle_index, ink_index, box, 0.15, hit_limit)
                    seen[hit_key] = hits
                column_miss = 0
                key = (column_miss, ann_hits, symbol_hits, hits, below, dist)
            if key < best_key:
                chosen = (x, y, align)
                best_key = key
                if iec_mirror:
                    perfect = (
                        ann_hits == 0
                        and stacked == 0
                        and symbol_hits == 0
                        and hits == 0
                        and column_miss == 0
                        and below == 0
                    )
                else:
                    perfect = (
                        column_miss == 0
                        and ann_hits == 0
                        and symbol_hits == 0
                        and hits == 0
                        and below == 0
                    )
                if perfect:
                    break
        x, y = _clamp_into(ann, chosen[0], chosen[1], chosen[2], frame)
        placed.append((ann, x, y, chosen[2]))
        box = annotation_box(ann, x, y, chosen[2])
        placed_boxes.append(box)
        placed_index.add(box)
        obstacle_index.add(box)
        if ann.kind == "mirror":
            placed_mirrors.append(box)
        obstacles.append(box)
    return placed


def _stroke_mark(painter: Painter, x: float, y: float, text: str, align: str, size: float, mark: str) -> None:
    if not mark or not text:
        return
    w = max(2.0, len(text) * size * 0.64)
    if align == "end":
        x0, x1 = x - w, x
    elif align == "middle":
        x0, x1 = x - w / 2.0, x + w / 2.0
    else:
        x0, x1 = x, x + w
    painter.set_stroke("#111111", 0.22)
    painter.set_fill(None)
    if mark == "underline":
        painter.line(x0, y + 0.45, x1, y + 0.45)
    elif mark == "bar":
        painter.line(x0, y - size * 0.95, x1, y - size * 0.95)


def _contact_glyph(painter: Painter, x: float, y: float, *, nc: bool, width: float = 11.0) -> None:
    """Small NO or NC contact on a horizontal conductor at *y*."""

    h = 3.6
    lead = min(2.4, max(1.2, width * 0.28))
    painter.set_stroke("#111111", 0.35)
    painter.set_fill(None)
    painter.line(x, y, x + lead, y)
    painter.line(x + width - lead, y, x + width, y)
    painter.line(x + lead, y - h / 2, x + lead, y + h / 2)
    painter.line(x + width - lead, y - h / 2, x + width - lead, y + h / 2)
    if nc:
        painter.line(x + lead * 0.8, y + h / 2 + 0.2, x + width - lead * 0.7, y - h / 2 - 0.8)
    else:
        painter.line(x + lead, y, x + width - lead * 1.3, y - h / 2 - 0.8)


def draw_contact_mirror(painter: Painter, ann: XrefAnnotation, x: float, y: float) -> None:
    """IEC coil contact table: glyph + terminals + /sheet.column, one row each."""

    rows = list(ann.mirror_rows or [])
    if not rows:
        return
    w, h = mirror_size(ann)
    x0 = x - w / 2.0
    painter.set_stroke("#111111", 0.22)
    painter.set_fill(None)
    painter.rect(x0, y - 0.6, w, h)
    painter.set_fill("#1a1a1a")
    for i, row in enumerate(rows):
        yy = y + 2.4 + i * MIRROR_ROW_H
        cy = yy + 2.4
        left, right = (row.get("terminals") or "—").split("-", 1) if "-" in (row.get("terminals") or "") else (
            row.get("terminals") or "",
            "",
        )
        painter.set_fill("#1a1a1a")
        painter.text(x0 + 1.6, cy + 0.6, left, 1.7, align="middle")
        painter.set_fill(None)
        _contact_glyph(painter, x0 + 3.2, cy, nc=row.get("state") == "NC", width=6.6)
        painter.set_fill("#1a1a1a")
        painter.text(x0 + 11.5, cy + 0.6, right, 1.7, align="middle")
        painter.text(x0 + w - 3.0, cy + 0.6, row.get("location") or "", 1.6, align="middle")
        painter.set_fill(None)


def draw_annotation(painter: Painter, ann: XrefAnnotation, x: float, y: float, align: str) -> None:
    if ann.kind == "mirror" and ann.mirror_rows:
        draw_contact_mirror(painter, ann, x, y)
        return
    painter.set_fill("#1a1a1a")
    for i, line in enumerate(ann.lines):
        yy = y + i * LINE_H
        painter.text(x, yy, line, ann.size, align=align)
        mark = ann.marks[i] if i < len(ann.marks) else ""
        _stroke_mark(painter, x, yy, line, align, ann.size, mark)
    painter.set_fill(None)


def draw_cross_references(
    painter: Painter,
    project: Project,
    sheet: Sheet,
    catalog: Catalog,
    *,
    ladder: bool = False,
) -> list[tuple[XrefAnnotation, float, float, str]]:
    placed = place_xref_labels(project, catalog, sheet, ladder=ladder)
    painter.set_layer("XREF")
    painter.set_stroke("#111111", 0.25)
    for ann, x, y, align in placed:
        draw_annotation(painter, ann, x, y, align)
    return placed
