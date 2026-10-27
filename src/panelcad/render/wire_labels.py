"""Place wire-number text off the conductor, clear of dots and other labels.

A two-point route used to take ``pts[len//2]`` — the far endpoint — so 104/105/106
sat on the motor terminals. Placement uses the midpoint of a clear segment and
offsets it perpendicular to the run.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from contextlib import contextmanager
from contextvars import ContextVar
from math import floor, hypot

from panelcad.core.geometry import Point
from panelcad.core.models import Sheet
from panelcad.library.catalog import Catalog

MARK_SIZE = 2.3
# Gauge notes print at the wire-number size. A smaller second line disappears at print scale.
SPEC_SIZE = MARK_SIZE
BUNDLE_GAP = 1.2
CLEAR = 2.8
END_KEEP = 3.6
DOT_R = 1.55
WIRE_PAD = 0.55
# Symbol leads on a 10 mm phase pitch cannot take the route pad on both sides
# of a gauge as wide as "14 AWG". This is half a drawn stroke, so the glyph
# still misses the ink.
_STEM_PAD = 0.20
# Neighbouring wire-number boxes keep this much paper between them.
MARK_GAP = 0.5
MIN_HOST = 1.15
# Larger than a body hit, a placed label, and a wide-note wire hit stacked together.
# A slot with another conductor between the label and its own wire must lose.
_CROSS_CONDUCTOR = 20000.0
# A jog can offer a slot that is CLEAR from one vertical and inside CLEAR of the next.
_CLOSE_OWN = 2500.0
# Overlaid routes (the star-delta bus drawn on one line) are one column, not parallel conductors.
COINCIDENT_COLUMN = 1.0
# Outside the drawable area loses to every legal slot, including a symbol-body hit.
_OFF_SHEET = 1.0e12


def _text_w(text: str, size: float) -> float:
    return max(2.2, len(text) * size * 0.64)


def _text_h(size: float) -> float:
    return size * 1.2


def bundle_note(spec: str) -> bool:
    """True for a shared phase note such as ``3 x 14 AWG``."""

    return " x " in str(spec or "")


def spec_origin(
    x: float,
    y: float,
    mark: str,
    spec: str,
    align: str,
    *,
    size: float = MARK_SIZE,
) -> tuple[float, float, str, float]:
    """Baseline of the gauge note.

    A bundle note shares the wire-number baseline and size, sitting beside the
    number, so it stays readable in the short gap between stacked symbols.
    Any other gauge stays under the number, at the same text height.
    """

    if bundle_note(spec):
        if align == "end":
            return (x - _text_w(mark, size) - BUNDLE_GAP, y, "end", MARK_SIZE)
        if align == "start":
            return (x + _text_w(mark, size) + BUNDLE_GAP, y, "start", MARK_SIZE)
        return (x, y - MARK_SIZE - 0.6, "middle", MARK_SIZE)
    return (x, y + MARK_SIZE + 0.2, align, SPEC_SIZE)


def _glyph_box(
    x: float,
    y: float,
    text: str,
    size: float,
    align: str,
) -> tuple[float, float, float, float]:
    w = _text_w(text, size)
    top = y - size * 0.90
    bottom = y + size * 0.25
    if align == "end":
        left = x - w
    elif align == "middle":
        left = x - w / 2.0
    else:
        left = x
    return (left, top, left + w, bottom)


def mark_box(
    x: float,
    y: float,
    mark: str,
    *,
    align: str = "start",
    spec: str = "",
    size: float = MARK_SIZE,
) -> tuple[float, float, float, float]:
    """Axis-aligned box around the drawn number and its gauge note."""

    box = _glyph_box(x, y, mark, size, align)
    if not spec:
        return box
    sx, sy, salign, ssize = spec_origin(x, y, mark, spec, align, size=size)
    spec_box = _glyph_box(sx, sy, spec, ssize, salign)
    return (
        min(box[0], spec_box[0]),
        min(box[1], spec_box[1]),
        max(box[2], spec_box[2]),
        max(box[3], spec_box[3]),
    )


def boxes_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    *,
    pad: float = 0.0,
) -> bool:
    return not (
        a[2] + pad <= b[0] or b[2] + pad <= a[0] or a[3] + pad <= b[1] or b[3] + pad <= a[1]
    )


def box_gap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    """Clear space between two boxes. Negative when they overlap.

    Separation on either axis counts. A label beside a conductor is clear by
    the horizontal gap even when their vertical spans overlap.
    """

    gx = max(a[0] - b[2], b[0] - a[2])
    gy = max(a[1] - b[3], b[1] - a[3])
    return max(gx, gy)


def drawn_mark_box(
    x: float,
    y: float,
    mark: str,
    align: str,
    spec: str = "",
    *,
    size: float = MARK_SIZE,
) -> tuple[float, float, float, float]:
    """Box matching ``sheet_text_boxes`` for one wire number and its gauge note."""

    if spec:
        return mark_box(x, y, mark, align=align, spec=spec, size=size)
    from panelcad.core.ink import text_box

    box = text_box(x, y, mark, size, align)
    if box is None:
        return (x, y, x, y)
    return box


def _seg_box(a: Point, b: Point, pad: float) -> tuple[float, float, float, float]:
    return (
        min(a.x, b.x) - pad,
        min(a.y, b.y) - pad,
        max(a.x, b.x) + pad,
        max(a.y, b.y) + pad,
    )


def _dot_box(x: float, y: float, r: float = DOT_R) -> tuple[float, float, float, float]:
    return (x - r, y - r, x + r, y + r)


def _tag_bands(sheet: Sheet) -> list[tuple[float, float, float, float]]:
    """Band above an unrotated symbol that Circuit Builder treats as the tag.

    Kept out of the nudge pass. A seated phase number can touch the band's
    edge, and a 0.05 mm nudge pad used to slide the locked 5 hp 101 mark.
    """

    bands: list[tuple[float, float, float, float]] = []
    for comp in sheet.components:
        if int(comp.rotation or 0):
            continue
        scale = float(comp.visual_scale() or 1.0)
        tag_w = max(8.0, len(str(comp.id)) * 2.0) * scale
        bands.append(
            (
                comp.x - 0.8 * scale,
                comp.y - 5.6 * scale,
                comp.x + tag_w,
                comp.y - 0.3 * scale,
            )
        )
    return bands


def _ref_obstacles(
    sheet: Sheet,
    catalog: Catalog,
    *,
    ladder: bool = False,
) -> list[tuple[float, float, float, float]]:
    """Device tags (M1, F1, …) drawn next to the symbol — 104 used to land on M1."""

    from panelcad.core.ink import device_ref_layout
    from panelcad.core.layout import is_io_sheet, is_network_sheet
    from panelcad.core.pins import extra_captions

    boxes: list[tuple[float, float, float, float]] = []
    if is_io_sheet(sheet) or is_network_sheet(sheet):
        return boxes
    align = "middle" if ladder else "start"
    for comp in sheet.components:
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        cat = (device.category or "").lower()
        if cat.startswith("connection.") or cat.startswith("connection.notes"):
            continue
        layout = device_ref_layout(device, comp, ref_align=align)
        if layout is not None:
            x, y, talign, size = layout
            boxes.append(mark_box(x, y, comp.id, align=talign, size=size))
        s = comp.visual_scale()
        # Keep the band above the symbol clear. Aux and tag-below refs are
        # drawn under the block, and that layout box alone lets a number sit
        # where Circuit Builder still treats the tag as living.
        boxes.append(mark_box(comp.x, comp.y - 2.5 * s, comp.id, align="start", size=3.0))
        y_cap = comp.y - 5.6 * s
        for line in extra_captions(comp):
            boxes.append(mark_box(comp.x, y_cap, line, align="start", size=2.2))
            y_cap -= 3.1 * s
        from panelcad.core.ink import text_box
        from panelcad.core.pins import rating_caption_positions

        for cx, cy, line in rating_caption_positions(
            comp, device.schematic_height, s, device.schematic_width
        ):
            # draw_device prints these start-aligned, running out to the right.
            box = text_box(cx, cy, line, 2.2, "start")
            if box is not None:
                boxes.append(box)
    return boxes


def _body_obstacles(sheet: Sheet, catalog: Catalog) -> list[tuple[float, float, float, float]]:
    """Symbol rectangles. Labels in a 4 mm inter-device gap have to step outside these."""

    boxes: list[tuple[float, float, float, float]] = []
    for comp in sheet.components:
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        cat = (device.category or "").lower()
        if cat.startswith("connection."):
            continue
        scale = comp.visual_scale()
        boxes.append(
            (
                comp.x,
                comp.y,
                comp.x + float(device.schematic_width) * scale,
                comp.y + float(device.schematic_height) * scale,
            )
        )
    return boxes


def _pin_obstacles(sheet: Sheet, catalog: Catalog) -> list[tuple[float, float, float, float]]:
    from panelcad.core.layout import is_io_sheet, is_network_sheet
    from panelcad.render.sheet import pin_world

    boxes: list[tuple[float, float, float, float]] = []
    network = is_network_sheet(sheet)
    io = is_io_sheet(sheet)
    for comp in sheet.components:
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        if network:
            from panelcad.library.ports import effective_ports
            from panelcad.render.network import port_world

            ports = effective_ports(device, comp)
            for port in ports:
                wx, wy = port_world(comp, port, ports)
                boxes.append(_dot_box(wx, wy))
            continue
        if io:
            from panelcad.render.io_ladder import io_pin_layout, is_io_module

            if is_io_module(device):
                laid = io_pin_layout(device)
                s = comp.visual_scale()
                for pid, (lx, ly) in laid.items():
                    boxes.append(_dot_box(comp.x + lx * s, comp.y + ly * s))
                continue
        for pin in device.pins:
            wx, wy = pin_world(comp, pin)
            boxes.append(_dot_box(wx, wy))
    return boxes


def _caption_obstacles(
    sheet: Sheet,
    catalog: Catalog,
) -> list[tuple[float, float, float, float]]:
    """Pin captions as the sheet paints them.

    The merge kept terminal dots and dropped the drawn-glyph boxes. An
    approximate box under the pin does not cover F1:4, so 104 sat on that digit.
    """

    from panelcad.render.sheet_labels import pin_caption_boxes

    return [box for _name, box in pin_caption_boxes(sheet, catalog)]


def _route_parts(
    routes: dict[str, list[Point]],
) -> dict[str, tuple[list[tuple[Point, Point]], list[tuple[float, float]]]]:
    parts: dict[str, tuple[list[tuple[Point, Point]], list[tuple[float, float]]]] = {}
    for wid, pts in routes.items():
        segs = [(a, b) for a, b in zip(pts, pts[1:]) if hypot(b.x - a.x, b.y - a.y) >= 0.2]
        ends = [(pts[0].x, pts[0].y), (pts[-1].x, pts[-1].y)] if pts else []
        parts[wid] = (segs, ends)
    return parts


def _drawn_lead_segments(sheet: Sheet, catalog: Catalog) -> list[tuple[Point, Point]]:
    """Symbol strokes that touch a pin. The router stops at the pin, so these are not route segments.

    A mark seated on the pin's x covers this stem. The gauge line is part of the
    mark box the scorer already builds; this is the ink that box has to miss.
    """

    from panelcad.core.geometry import rotate_point

    segs: list[tuple[Point, Point]] = []
    for comp in sheet.components:
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        pins = [(float(pin.x), float(pin.y)) for pin in device.pins]
        if not pins:
            continue
        hidden = {str(pin_id) for pin_id in (comp.properties.get("hidden_pins") or [])}
        scale = float(comp.visual_scale() or 1.0)
        rotation = int(comp.rotation or 0)
        mirror = bool(comp.mirror)

        def _world(lx: float, ly: float) -> Point:
            rx, ry = rotate_point(lx * scale, ly * scale, rotation)
            if mirror:
                rx = -rx
            return Point(x=comp.x + rx, y=comp.y + ry)

        for prim in device.primitives:
            kind = getattr(prim, "type", "")
            if kind == "line":
                local = [(float(prim.x), float(prim.y)), (float(prim.x2), float(prim.y2))]
            elif kind == "polyline":
                local = [(float(x), float(y)) for x, y in (getattr(prim, "points", None) or [])]
            else:
                continue
            if len(local) < 2:
                continue
            if hidden and min(point[1] for point in local) >= 33.0:
                continue
            if not any(
                hypot(local[0][0] - px, local[0][1] - py) <= 0.9
                or hypot(local[-1][0] - px, local[-1][1] - py) <= 0.9
                for px, py in pins
            ):
                continue
            world = [_world(x, y) for x, y in local]
            for a, b in zip(world, world[1:]):
                if hypot(b.x - a.x, b.y - a.y) >= 0.2:
                    segs.append((a, b))
    return segs


def _vertical_pref(pts: list[Point]) -> bool:
    if len(pts) < 2:
        return False
    return abs(pts[-1].y - pts[0].y) >= abs(pts[-1].x - pts[0].x) * 0.55


def _host_segments(pts: list[Point]) -> list[tuple[Point, Point, float, bool]]:
    """Real segments plus merged vertical/horizontal spines. *bool* is vertical."""

    raw: list[tuple[Point, Point, float, bool]] = []
    cols: dict[float, tuple[float, float]] = {}
    rows: dict[float, tuple[float, float]] = {}
    for a, b in zip(pts, pts[1:]):
        length = hypot(b.x - a.x, b.y - a.y)
        if length < 0.35:
            continue
        vertical = abs(b.x - a.x) <= 0.4
        raw.append((a, b, length, vertical))
        if vertical:
            x = round((a.x + b.x) / 2.0, 2)
            lo, hi = cols.get(x, (min(a.y, b.y), max(a.y, b.y)))
            cols[x] = (min(lo, a.y, b.y), max(hi, a.y, b.y))
        elif abs(b.y - a.y) <= 0.4:
            y = round((a.y + b.y) / 2.0, 2)
            lo, hi = rows.get(y, (min(a.x, b.x), max(a.x, b.x)))
            rows[y] = (min(lo, a.x, b.x), max(hi, a.x, b.x))
    extra: list[tuple[Point, Point, float, bool]] = []

    def _merge(bins: dict[float, tuple[float, float]], *, vertical: bool) -> None:
        items = sorted(bins.items())
        used = [False] * len(items)
        for i, (key, (lo, hi)) in enumerate(items):
            if used[i]:
                continue
            keys = [key]
            spans = {key: hi - lo}
            span_lo, span_hi = lo, hi
            for j in range(i + 1, len(items)):
                other, (olo, ohi) = items[j]
                if other - key > 0.85:
                    break
                used[j] = True
                keys.append(other)
                spans[other] = ohi - olo
                span_lo, span_hi = min(span_lo, olo), max(span_hi, ohi)
            # Sit on the longest column/row of a jog, not the average (keeps CLEAR).
            host = max(keys, key=lambda k: (spans[k], -k if vertical else k))
            span = span_hi - span_lo
            if span >= MIN_HOST:
                if vertical:
                    extra.append((Point(x=host, y=span_lo), Point(x=host, y=span_hi), span, True))
                else:
                    extra.append((Point(x=span_lo, y=host), Point(x=span_hi, y=host), span, False))

    _merge(cols, vertical=True)
    _merge(rows, vertical=False)
    return raw + extra


def _offsets(
    mx: float,
    my: float,
    vertical: bool,
    ux: float,
    uy: float,
    *,
    left: float | None = None,
    right: float | None = None,
    close: bool = False,
    tight: bool = False,
) -> list[tuple[float, float, str]]:
    found: list[tuple[float, float, str]] = []
    left_x = mx if left is None else left
    right_x = mx if right is None else right
    # IEC column sheets and generated I/O use the nearer slots from main.
    # A NEMA power sheet keeps CLEAR as the closest slot so locked seats stay.
    distances = (CLEAR, CLEAR + 1.7, CLEAR + 3.3, CLEAR + 5.4, CLEAR + 7.6, CLEAR + 10.0, CLEAR + 16.0, CLEAR + 24.0, CLEAR + 34.0)
    if close:
        distances = (1.05, 1.7, *distances)
    elif tight:
        # IEC power stubs are about 10 mm apart. CLEAR leaves a 3-digit number
        # 0.02 mm closer to the next phase. Just inside CLEAR still clears
        # _CLOSE_OWN (2.6 mm) and the endpoint rule.
        distances = (2.65, *distances)
    for dist in distances:
        if vertical:
            found.append((right_x + dist, my, "start"))
            found.append((left_x - dist, my, "end"))
        elif abs(uy) <= 0.25:
            found.append((mx, my - dist, "middle"))
            found.append((mx, my + dist + MARK_SIZE, "middle"))
        else:
            found.append((mx + uy * dist, my - ux * dist, "start"))
            found.append((mx - uy * dist, my + ux * dist, "end"))
    return found


def _foreign_crosses_vertical(
    a: Point,
    b: Point,
    foreign: list[list[Point]] | None,
) -> bool:
    """True when another wire's horizontal runs through this vertical drop."""

    if not foreign or abs(a.x - b.x) > 0.5:
        return False
    x = (a.x + b.x) / 2.0
    y0, y1 = (a.y, b.y) if a.y <= b.y else (b.y, a.y)
    for pts in foreign:
        for c, d in zip(pts, pts[1:]):
            if abs(c.y - d.y) > 0.5:
                continue
            yy = (c.y + d.y) / 2.0
            if not (y0 + 0.4 < yy < y1 - 0.4):
                continue
            if min(c.x, d.x) - 0.4 <= x <= max(c.x, d.x) + 0.4:
                return True
    return False


def _candidates(
    pts: list[Point],
    mark: str,
    spec: str,
    *,
    close: bool = False,
    tight: bool = False,
    foreign: list[list[Point]] | None = None,
) -> list[tuple[float, float, str, float]]:
    """(x, y, align, segment_length) — scored later for overlaps."""

    if len(pts) < 2:
        return []
    ends = (pts[0], pts[-1])
    prefer_vert = _vertical_pref(pts)
    vert_xs = []
    for a, b in zip(pts, pts[1:]):
        if hypot(b.x - a.x, b.y - a.y) < 0.35:
            continue
        if abs(b.x - a.x) <= 0.4:
            vert_xs.append((a.x + b.x) / 2.0)
    found: list[tuple[float, float, str, float]] = []
    # Wide single gauges (350 kcmil) cannot sit a full CLEAR off the bridge:
    # the note reaches the next riser. These closer seats are inside the
    # 2 mm endpoint rule, so they are filtered on the dot only.
    beside: list[tuple[float, float, str, float]] = []
    for a, b, length, vertical in _host_segments(pts):
        if length < MIN_HOST:
            continue
        if prefer_vert and not vertical and length < 10.0:
            continue
        if not prefer_vert and vertical and length < 8.0:
            continue
        dx, dy = b.x - a.x, b.y - a.y
        ux, uy = dx / length, dy / length
        ts = [0.5]
        if length >= 12.0:
            ts = [0.5, 0.38, 0.62, 0.28, 0.72]
        elif length >= 6.0:
            ts = [0.5, 0.4, 0.6]
        elif vertical:
            # A short straight drop beside the next phase's terminal: the middle
            # seats can all touch that dot by a fraction. A seat nearer the far
            # end keeps the mark beside its own wire instead of past the next one.
            ts = [0.5, 0.35, 0.65, 0.75, 0.25]
        # A short drop with a phase swap through it has no clear middle.
        # Sample the ends only then, so an uncrossed NEMA drop keeps its seat.
        if vertical and length < 14.0 and _foreign_crosses_vertical(a, b, foreign):
            for extra in (0.05, 0.2, 0.8, 0.15, 0.85, 0.95):
                if extra not in ts:
                    ts.append(extra)
        for t in ts:
            mx = a.x + ux * length * t
            my = a.y + uy * length * t
            near_end = min(hypot(mx - ends[0].x, my - ends[0].y), hypot(mx - ends[1].x, my - ends[1].y))
            if near_end < END_KEEP and length > END_KEEP * 2.4:
                continue
            left = right = None
            if vertical or abs(ux) <= 0.25:
                near = [x for x in vert_xs if abs(x - mx) <= 0.9]
                if near:
                    left, right = min(near), max(near)
            for x, y, align in _offsets(
                mx, my, vertical or abs(ux) <= 0.25, ux, uy,                 left=left, right=right, close=close, tight=tight
            ):
                found.append((x, y, align, length))
            if not vertical and abs(uy) <= 0.25:
                # A note centered on a bridge is drawn across the leads. Offer
                # the open side of those leads instead.
                leads = [
                    vx
                    for vx in vert_xs
                    if min(a.x, b.x) - 1.0 <= vx <= max(a.x, b.x) + 1.0
                ]
                if len(leads) >= 2:
                    left_lead, right_lead = min(leads), max(leads)
                    # CLEAR - 0.6 is inside the anchor-close penalty, so a free
                    # CLEAR slot still wins. A gauge as wide as 350 kcmil needs
                    # that tighter seat: CLEAR itself reaches the next riser.
                    # The 1.15 / 1.45 mm seats are only for that wide note, and
                    # only when they stay off the terminal dot.
                    dists = (CLEAR - 0.6, CLEAR, CLEAR + 1.6, CLEAR + 3.4)
                    wide = bool(spec) and not bundle_note(spec) and _text_w(spec, MARK_SIZE) >= 12.0
                    if wide:
                        dists = (1.15, 1.45, *dists)
                    for dist in dists:
                        slots = (
                            (left_lead - dist, my, "end", length),
                            (right_lead + dist, my, "start", length),
                        )
                        if dist < 2.0:
                            beside.extend(slots)
                        else:
                            found.extend(slots)
            if vertical or abs(ux) <= 0.25:
                # CLEAR from the far side of a 2 mm jog lands on the near side.
                # Offer the gap just outside that near column as well.
                pad = WIRE_PAD + 0.25
                for vx in vert_xs:
                    if vx < mx - 0.4 and mx - vx <= 4.0:
                        found.append((vx - pad, my, "end", length))
                    elif vx > mx + 0.4 and vx - mx <= 4.0:
                        found.append((vx + pad, my, "start", length))
    found.extend(_bundle_pin_lifts(pts, foreign))
    if found or beside:
        kept = []
        for item in found:
            x, y, align, _length = item
            if min(hypot(x - ends[0].x, y - ends[0].y), hypot(x - ends[1].x, y - ends[1].y)) < 2.0:
                continue
            glyph = mark_box(x, y, mark, align=align)
            if any(
                boxes_overlap(glyph, (end.x - 1.4, end.y - 1.4, end.x + 1.4, end.y + 1.4))
                for end in ends
            ):
                continue
            kept.append(item)
        for item in beside:
            x, y, align, _length = item
            glyph = mark_box(x, y, mark, align=align)
            if any(
                boxes_overlap(glyph, (end.x - 1.4, end.y - 1.4, end.x + 1.4, end.y + 1.4))
                for end in ends
            ):
                continue
            kept.append(item)
        if kept:
            return kept
        return found or beside
    mid = pts[0]
    span = 0.0
    if len(pts) == 2:
        mid = Point(x=(pts[0].x + pts[1].x) / 2.0, y=(pts[0].y + pts[1].y) / 2.0)
        span = hypot(pts[1].x - pts[0].x, pts[1].y - pts[0].y)
    else:
        a, b = max(zip(pts, pts[1:]), key=lambda pair: hypot(pair[1].x - pair[0].x, pair[1].y - pair[0].y))
        mid = Point(x=(a.x + b.x) / 2.0, y=(a.y + b.y) / 2.0)
        span = hypot(b.x - a.x, b.y - a.y)
    # CLEAR alone put a stub shorter than the glyph halfway to the next phase
    # (104 on F1.2). Step that stub up off the terminal pin.
    if span < 4.0:
        slots: list[tuple[float, float, str, float]] = []
        for dist in (1.05, 1.7, CLEAR):
            for dy in (0.0, -4.2, -6.0):
                slots.append((mid.x + dist, mid.y + dy, "start", max(span, 1.0)))
                slots.append((mid.x - dist, mid.y + dy, "end", max(span, 1.0)))
        return slots
    return [(mid.x + CLEAR, mid.y, "start", 1.0), (mid.x - CLEAR, mid.y, "end", 1.0)]


def _bundle_pin_lifts(
    pts: list[Point],
    foreign: list[list[Point]] | None,
) -> list[tuple[float, float, str, float]]:
    """Seats above a device pin when a gauge cannot fit between bundled phases.

    Reversing load swaps sit one grid apart. A full-size gauge under the
    number is taller than that gap, so the middle phase sits just above the
    pin its stub leaves instead of between the runs.
    """

    if not foreign or len(pts) < 2:
        return []
    horizontals: list[tuple[Point, Point]] = []
    for a, b in zip(pts, pts[1:]):
        if abs(a.y - b.y) <= 0.4 and hypot(b.x - a.x, b.y - a.y) >= 20.0:
            horizontals.append((a, b))
    if not horizontals:
        return []
    tight = False
    for a, b in horizontals:
        y = (a.y + b.y) / 2.0
        x0, x1 = (a.x, b.x) if a.x <= b.x else (b.x, a.x)
        for route in foreign:
            for c, d in zip(route, route[1:]):
                if abs(c.y - d.y) > 0.4 or hypot(d.x - c.x, d.y - c.y) < 8.0:
                    continue
                if abs((c.y + d.y) / 2.0 - y) > 4.0:
                    continue
                ox0, ox1 = (c.x, d.x) if c.x <= d.x else (d.x, c.x)
                if min(x1, ox1) - max(x0, ox0) >= 8.0:
                    tight = True
                    break
            if tight:
                break
        if tight:
            break
    if not tight:
        return []
    # The gauge is drawn under the number. This lift puts that ink above the pin.
    spec_drop = MARK_SIZE + 0.2 + MARK_SIZE * 0.25
    ends = { (round(pts[0].x, 2), round(pts[0].y, 2)), (round(pts[-1].x, 2), round(pts[-1].y, 2)) }
    lifts: list[tuple[float, float, str, float]] = []
    for a, b in zip(pts, pts[1:]):
        length = hypot(b.x - a.x, b.y - a.y)
        if abs(a.x - b.x) > 0.4 or length < 2.0 or length > 14.0:
            continue
        pin = a if a.y <= b.y else b
        if (round(pin.x, 2), round(pin.y, 2)) not in ends:
            continue
        y = pin.y - spec_drop - 1.55
        # pin.x is the drawn stem. On a 10 mm pitch a gauge such as 14 AWG
        # fills the bay, so the seat can only move a stroke-width off this
        # stem and still stay nearer this wire than the next.
        for dist in (_STEM_PAD + 0.05, CLEAR):
            lifts.append((pin.x - dist, y, "end", length))
    return lifts


def fallback_anchor(pts: list[Point]) -> tuple[float, float, str]:
    """Midpoint of the longest segment, offset to the side. Never an endpoint."""

    if not pts:
        return (0.0, 0.0, "start")
    if len(pts) == 1:
        return (pts[0].x + CLEAR, pts[0].y, "start")
    best = max(zip(pts, pts[1:]), key=lambda pair: hypot(pair[1].x - pair[0].x, pair[1].y - pair[0].y))
    a, b = best
    mx, my = (a.x + b.x) / 2.0, (a.y + b.y) / 2.0
    if abs(b.x - a.x) <= abs(b.y - a.y):
        return (mx + CLEAR, my, "start")
    return (mx, my - CLEAR, "middle")


def _foreign_cover_cost(
    box: tuple[float, float, float, float],
    own_id: str,
    parts: dict[str, tuple[list[tuple[Point, Point]], list[tuple[float, float]]]],
    near: set[str] | None,
    strokes: dict[str, set[tuple[int, int]]] | None = None,
) -> float:
    """Cost of covering foreign dots and strokes.

    The first overlap on a wire pays ``_CROSS_CONDUCTOR``. A later segment of
    that same wire pays the cheap extra. ``near`` skips wires that cannot
    overlap; ``None`` reads every wire. ``strokes`` is that same overlap set
    grouped by wire, so the scan does not retest every segment.
    """

    score = 0.0
    wide = (box[2] - box[0]) >= 8.0
    if strokes is not None:
        if not strokes:
            return 0.0
        for wid, got in strokes.items():
            if wid == own_id or not got:
                continue
            segs, ends = parts.get(wid, ((), ()))
            covered_this = False
            for i in range(len(ends)):
                if (0, i) not in got:
                    continue
                score += _CROSS_CONDUCTOR
                covered_this = True
            for i in range(len(segs)):
                if (1, i) not in got:
                    continue
                score += _CROSS_CONDUCTOR if not covered_this else (2400.0 if wide else 90.0)
                covered_this = True
        return score
    for wid, (segs, ends) in parts.items():
        if wid == own_id:
            continue
        if near is not None and wid not in near:
            continue
        # A same-numbered companion shares this mark, so it is not "the next
        # phase". Its strokes are still conductors: the glyph may not cover them.
        covered_this = False
        for x, y in ends:
            if boxes_overlap(box, _dot_box(x, y)):
                # Covering another terminal is as bad as jumping past that wire.
                score += _CROSS_CONDUCTOR
                covered_this = True
        for a, b in segs:
            if boxes_overlap(box, _seg_box(a, b, WIRE_PAD)):
                # A gauge note wider than the phase gap must leave the bundle.
                # Sitting on the next conductor used to beat a crossing because
                # the per-segment hit was only 90 or 2400. Each conductor pays
                # that once; a second segment of the same wire stays cheap.
                score += _CROSS_CONDUCTOR if not covered_this else (2400.0 if wide else 90.0)
                covered_this = True
    return score


def _other_conductor_between(
    box: tuple[float, float, float, float],
    own_id: str,
    parts: dict[str, tuple[list[tuple[Point, Point]], list[tuple[float, float]]]],
) -> bool:
    """True when another wire's vertical run lies between this label and its own wire.

    Jogged power conductors (100 A breaker into a 32 A contactor) used to
    place L2 past L3, and 102 past 103, because the far slot did not overlap
    a box. The conductor in the gap is the fault, not the overlap.
    """

    y0, y1 = box[1], box[3]

    def _verticals(wid: str) -> list[float]:
        found: list[float] = []
        for a, b in parts.get(wid, ([], []))[0]:
            if abs(a.x - b.x) > 0.6:
                continue
            if min(max(a.y, b.y), y1) - max(min(a.y, b.y), y0) < 0.4:
                continue
            found.append((a.x + b.x) / 2.0)
        return found

    own_xs = _verticals(own_id)
    if not own_xs:
        return False
    center = (box[0] + box[2]) / 2.0
    own_x = min(own_xs, key=lambda value: abs(value - center))
    if own_x < box[0]:
        gap_lo, gap_hi = own_x, box[0]
    elif own_x > box[2]:
        gap_lo, gap_hi = box[2], own_x
    else:
        return False
    for wid in parts:
        if wid == own_id:
            continue
        for other_x in _verticals(wid):
            if gap_lo + 0.35 < other_x < gap_hi - 0.35:
                return True
    return False


def _inside_working(
    box: tuple[float, float, float, float],
    rect: tuple[float, float, float, float] | None,
    *,
    tol: float = 0.05,
) -> bool:
    """True when the whole mark box sits in ``working_rect`` (x, y, w, h)."""

    if rect is None:
        return True
    x, y, w, h = rect
    return box[0] >= x - tol and box[1] >= y - tol and box[2] <= x + w + tol and box[3] <= y + h + tol


def _gap_to_strokes(
    box: tuple[float, float, float, float],
    segs: list[tuple[Point, Point]],
) -> float | None:
    """Smallest clearance from a mark box to its own strokes. None when there are none."""

    if not segs:
        return None
    best = 1.0e9
    for a, b in segs:
        stroke = (min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y))
        best = min(best, box_gap(box, stroke))
    return best


def _split_column_penalty(
    box: tuple[float, float, float, float],
    own_segs: list[tuple[Point, Point]],
    placed: list[tuple[float, float, float, float]],
) -> float:
    """Prefer the side of this conductor that already holds a neighbour.

    104/105/106 share one drawn vertical. An equal-scoring slot on the other
    side left 106 beside the motor while 105 sat on the open side.
    """

    if not placed or not own_segs:
        return 0.0
    xs: list[float] = []
    for a, b in own_segs:
        if abs(a.x - b.x) > 0.4:
            continue
        if min(max(a.y, b.y), box[3]) - max(min(a.y, b.y), box[1]) < 0.4:
            continue
        xs.append((a.x + b.x) / 2.0)
    if not xs:
        return 0.0
    center = (box[0] + box[2]) / 2.0
    own_x = min(xs, key=lambda value: abs(value - center))
    if box[2] < own_x - 0.2:
        side = -1
    elif box[0] > own_x + 0.2:
        side = 1
    else:
        return 0.0
    for other in placed:
        if other[2] < own_x - 0.3:
            other_side = -1
        elif other[0] > own_x + 0.3:
            other_side = 1
        else:
            continue
        near = min(abs(other[0] - own_x), abs(other[2] - own_x))
        if near > CLEAR + 2.0:
            continue
        if min(other[3], box[3] + 14.0) - max(other[1], box[1] - 14.0) < 0.0:
            continue
        if other_side != side:
            return 40.0
    return 0.0


class _Seg:
    __slots__ = ("wid", "a", "b", "sid", "x0", "y0", "x1", "y1", "axis")

    def __init__(self, wid: str, a: Point, b: Point, sid: int) -> None:
        self.wid = wid
        self.a = a
        self.b = b
        self.sid = sid
        self.x0 = a.x if a.x <= b.x else b.x
        self.x1 = b.x if a.x <= b.x else a.x
        self.y0 = a.y if a.y <= b.y else b.y
        self.y1 = b.y if a.y <= b.y else a.y
        self.axis = abs(a.x - b.x) < 1e-9 or abs(a.y - b.y) < 1e-9


class _RouteIndex:
    """Grid of route segments and terminal dots.

    ``_score`` asks for the wires that can touch one mark, and for the nearest
    foreign stroke. Both answers match a full scan.
    """

    def __init__(
        self,
        parts: dict[str, tuple[list[tuple[Point, Point]], list[tuple[float, float]]]],
        *,
        cell: float = 16.0,
    ) -> None:
        self.cell = cell
        self.grid: dict[tuple[int, int], list[_Seg]] = {}
        self._ends: dict[tuple[int, int], list[str]] = {}
        self._segs: list[_Seg] = []
        self._wire_ids: set[str] = set()
        self._bounds: dict[str, tuple[float, float, float, float]] = {}
        self._verts: dict[str, list[tuple[float, float, float]]] = {}
        vert_rows: list[tuple[float, float, float, str]] = []
        sid = 0
        for wid, (segs, ends) in parts.items():
            self._wire_ids.add(wid)
            xs: list[float] = []
            ys: list[float] = []
            for a, b in segs:
                xs.extend((a.x, b.x))
                ys.extend((a.y, b.y))
            for x, y in ends:
                xs.append(x)
                ys.append(y)
                key = (self._idx(x), self._idx(y))
                end_bucket = self._ends.get(key)
                if end_bucket is None:
                    self._ends[key] = [wid]
                else:
                    end_bucket.append(wid)
            if xs:
                self._bounds[wid] = (min(xs), min(ys), max(xs), max(ys))
            own_verts: list[tuple[float, float, float]] = []
            for a, b in segs:
                seg = _Seg(wid, a, b, sid)
                sid += 1
                self._segs.append(seg)
                self._cover(seg)
                if abs(a.x - b.x) <= 0.6:
                    vx = (a.x + b.x) * 0.5
                    vy0, vy1 = (a.y, b.y) if a.y <= b.y else (b.y, a.y)
                    own_verts.append((vx, vy0, vy1))
                    vert_rows.append((vx, vy0, vy1, wid))
            if own_verts:
                self._verts[wid] = own_verts
        vert_rows.sort(key=lambda item: item[0])
        self._vert_rows = vert_rows
        self._vert_xs = [item[0] for item in vert_rows]
        horiz: list[_Seg] = []
        verts: list[_Seg] = []
        diags: list[_Seg] = []
        for seg in self._segs:
            if abs(seg.a.y - seg.b.y) < 1e-9:
                horiz.append(seg)
            elif abs(seg.a.x - seg.b.x) < 1e-9:
                verts.append(seg)
            else:
                diags.append(seg)
        horiz.sort(key=lambda seg: seg.y0)
        verts.sort(key=lambda seg: seg.x0)
        self._hsegs = horiz
        self._hys = [seg.y0 for seg in horiz]
        self._vsegs = verts
        self._vxs = [seg.x0 for seg in verts]
        self._diags = diags
        self._build_strokes(parts)

    def _build_strokes(self, parts) -> None:
        """Boxes the foreign-cover test used to rebuild for every candidate."""

        from panelcad.render.xref import _BoxIndex

        meta: list[tuple[str, int, int]] = []
        boxes: list[tuple[float, float, float, float]] = []
        own_dots: dict[str, list[tuple[float, float, float, float]]] = {}
        own_pads: dict[str, list[tuple[float, float, float, float]]] = {}
        own_strokes: dict[str, list[tuple[float, float, float, float]]] = {}
        for wid, (segs, ends) in parts.items():
            dots_w: list[tuple[float, float, float, float]] = []
            pads_w: list[tuple[float, float, float, float]] = []
            strokes_w: list[tuple[float, float, float, float]] = []
            for i, (x, y) in enumerate(ends):
                dbox = _dot_box(x, y)
                dots_w.append(dbox)
                boxes.append(dbox)
                meta.append((wid, 0, i))
            for i, (a, b) in enumerate(segs):
                pbox = _seg_box(a, b, WIRE_PAD)
                pads_w.append(pbox)
                strokes_w.append((min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y)))
                boxes.append(pbox)
                meta.append((wid, 1, i))
            own_dots[wid] = dots_w
            own_pads[wid] = pads_w
            own_strokes[wid] = strokes_w
        self._own_dots = own_dots
        self._own_pads = own_pads
        self._own_strokes = own_strokes
        self._stroke_meta = meta
        self._stroke_index = _BoxIndex(boxes)

    def stroke_hits(
        self, box: tuple[float, float, float, float]
    ) -> dict[str, set[tuple[int, int]]]:
        """``(kind, index)`` hits. Kind 0 is a terminal dot, kind 1 a segment."""

        found: dict[str, set[tuple[int, int]]] = {}
        meta = self._stroke_meta
        for idx in self._stroke_index.overlap_ids(box, 0.0):
            wid, kind, ordinal = meta[idx]
            bucket = found.get(wid)
            if bucket is None:
                found[wid] = {(kind, ordinal)}
            else:
                bucket.add((kind, ordinal))
        return found

    def _idx(self, value: float) -> int:
        return floor(value / self.cell)

    def _cover(self, seg: _Seg) -> None:
        x0, x1 = (seg.a.x, seg.b.x) if seg.a.x <= seg.b.x else (seg.b.x, seg.a.x)
        y0, y1 = (seg.a.y, seg.b.y) if seg.a.y <= seg.b.y else (seg.b.y, seg.a.y)
        i0, i1 = self._idx(x0), self._idx(x1)
        j0, j1 = self._idx(y0), self._idx(y1)
        grid = self.grid
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                bucket = grid.get((i, j))
                if bucket is None:
                    grid[(i, j)] = [seg]
                else:
                    bucket.append(seg)

    def near_wire_ids(
        self,
        box: tuple[float, float, float, float],
        *,
        seg_pad: float,
        dot_r: float,
    ) -> set[str]:
        """Wires whose stroke or terminal dot can overlap *box*.

        A segment is stored in every cell its centerline covers. The query
        covers *box* grown by the same pad the overlap test uses, so a real
        hit is always in the set. Extra ids are wires that share a cell and
        do not change the score.
        """

        found: set[str] = set()
        self._collect(self.grid, box, seg_pad, found, segs=True)
        self._collect(self._ends, box, dot_r, found, segs=False)
        return found

    def _collect(
        self,
        grid: dict,
        box: tuple[float, float, float, float],
        pad: float,
        found: set[str],
        *,
        segs: bool,
    ) -> None:
        i0 = self._idx(box[0] - pad)
        i1 = self._idx(box[2] + pad)
        j0 = self._idx(box[1] - pad)
        j1 = self._idx(box[3] + pad)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                bucket = grid.get((i, j))
                if not bucket:
                    continue
                if segs:
                    for seg in bucket:
                        found.add(seg.wid)
                else:
                    found.update(bucket)

    def conductor_between(self, box: tuple[float, float, float, float], own_id: str) -> bool:
        """Same answer as ``_other_conductor_between`` for this index."""

        y0, y1 = box[1], box[3]
        own_xs: list[float] = []
        for x, ya, yb in self._verts.get(own_id, ()):
            if min(yb, y1) - max(ya, y0) < 0.4:
                continue
            own_xs.append(x)
        if not own_xs:
            return False
        center = (box[0] + box[2]) / 2.0
        own_x = min(own_xs, key=lambda value: abs(value - center))
        if own_x < box[0]:
            gap_lo, gap_hi = own_x, box[0]
        elif own_x > box[2]:
            gap_lo, gap_hi = box[2], own_x
        else:
            return False
        lo = gap_lo + 0.35
        hi = gap_hi - 0.35
        rows = self._vert_rows
        i = bisect_right(self._vert_xs, lo)
        n = len(rows)
        while i < n:
            x, ya, yb, wid = rows[i]
            if x >= hi:
                return False
            if wid != own_id and min(yb, y1) - max(ya, y0) >= 0.4:
                return True
            i += 1
        return False

    def nearest_box(self, box: tuple[float, float, float, float], exclude: set[str]) -> float:
        ax0, ay0, ax1, ay1 = box

        def measure(seg: _Seg) -> float:
            return _axis_seg_box_distance(seg, box)

        best = self._walk_axis(
            self._vsegs,
            self._vxs,
            ax0,
            ax1,
            exclude,
            best=1.0e9,
            measure=measure,
            gap_left=lambda seg: ax0 - seg.x1,
            gap_right=lambda seg: seg.x0 - ax1,
        )
        if best == 0.0:
            return 0.0
        best = self._walk_axis(
            self._hsegs,
            self._hys,
            ay0,
            ay1,
            exclude,
            best=best,
            measure=measure,
            gap_left=lambda seg: ay0 - seg.y1,
            gap_right=lambda seg: seg.y0 - ay1,
        )
        if best == 0.0:
            return 0.0
        for seg in self._diags:
            if seg.wid in exclude:
                continue
            dist = box_route_distance(box, [seg.a, seg.b])
            if dist < best:
                best = dist
                if best == 0.0:
                    return 0.0
        return best

    def nearest_point(self, x: float, y: float, exclude: set[str]) -> float:
        def measure(seg: _Seg) -> float:
            return _point_segment_distance(x, y, seg.a, seg.b)

        best = self._walk_axis(
            self._vsegs,
            self._vxs,
            x,
            x,
            exclude,
            best=1.0e9,
            measure=measure,
            gap_left=lambda seg: x - seg.x1,
            gap_right=lambda seg: seg.x0 - x,
        )
        if best == 0.0:
            return 0.0
        best = self._walk_axis(
            self._hsegs,
            self._hys,
            y,
            y,
            exclude,
            best=best,
            measure=measure,
            gap_left=lambda seg: y - seg.y1,
            gap_right=lambda seg: seg.y0 - y,
        )
        if best == 0.0:
            return 0.0
        for seg in self._diags:
            if seg.wid in exclude:
                continue
            dist = measure(seg)
            if dist < best:
                best = dist
                if best == 0.0:
                    return 0.0
        return best

    def _walk_axis(self, segs, keys, lo, hi, exclude, *, best, measure, gap_left, gap_right):
        """Nearest segment in a list sorted by its fixed coordinate.

        Walking outward stops once the axis gap is already at least *best*.
        That gap is the distance to the segment's bounding interval, which is
        never greater than the real distance for an axis-aligned stroke.
        """

        n = len(segs)
        if n == 0:
            return best
        i = bisect_left(keys, (lo + hi) * 0.5)
        j = i
        while j < n:
            seg = segs[j]
            if keys[j] > hi and gap_right(seg) >= best:
                break
            if seg.wid not in exclude:
                dist = measure(seg)
                if dist < best:
                    best = dist
                    if best == 0.0:
                        return 0.0
            j += 1
        j = i - 1
        while j >= 0:
            seg = segs[j]
            if keys[j] < lo and gap_left(seg) >= best:
                break
            if seg.wid not in exclude:
                dist = measure(seg)
                if dist < best:
                    best = dist
                    if best == 0.0:
                        return 0.0
            j -= 1
        return best


def _score(
    box: tuple[float, float, float, float],
    pts: list[Point],
    own_id: str,
    parts: dict[str, tuple[list[tuple[Point, Point]], list[tuple[float, float]]]],
    pin_boxes: list[tuple[float, float, float, float]],
    placed: list[tuple[float, float, float, float]],
    length: float,
    vertical_pref: bool,
    align: str,
    anchor: tuple[float, float] | None = None,
    bounds: tuple[float, float, float, float] | None = None,
    bundle: bool = False,
    lead_boxes: list[tuple[float, float, float, float]] | None = None,
    channel_boxes: list[tuple[float, float, float, float]] | None = None,
    outline_boxes: list[tuple[float, float, float, float]] | None = None,
    schematic_boxes: list[tuple[float, float, float, float]] | None = None,
    io_boxes: list[tuple[float, float, float, float]] | None = None,
    ignore_ids: set[str] | None = None,
    tag_bands: list[tuple[float, float, float, float]] | None = None,
    caption_boxes: list[tuple[float, float, float, float]] | None = None,
    stem_segs: list[tuple[Point, Point]] | None = None,
    route_index: _RouteIndex | None = None,
    obstacles: _MarkFields | None = None,
    ceiling: float | None = None,
    pts_span: float | None = None,
    pts_bounds: tuple[float, float, float, float] | None = None,
) -> float:
    if bounds is not None and not _inside_working(box, bounds):
        return _OFF_SHEET
    score = 0.0
    own_segs, own_ends = parts.get(own_id, ([], []))
    index = route_index
    if pts and index is None:
        index = _RouteIndex(parts)
    if index is not None:
        for dbox in index._own_dots.get(own_id, ()):
            if boxes_overlap(box, dbox):
                score += 1200.0
        for pbox in index._own_pads.get(own_id, ()):
            if boxes_overlap(box, pbox):
                score += _CROSS_CONDUCTOR
    else:
        for x, y in own_ends:
            if boxes_overlap(box, _dot_box(x, y)):
                score += 1200.0
        for a, b in own_segs:
            if boxes_overlap(box, _seg_box(a, b, WIRE_PAD)):
                # The glyph may sit beside its wire. It may not cover the stroke.
                score += _CROSS_CONDUCTOR
    # The router ends at the pin. The symbol still draws a lead through that
    # point, and the gauge line hangs under the number. Covering that stem is
    # the same fault as covering the route.
    if obstacles is not None:
        if obstacles.stems.overlap_ids(box, 0.0):
            score += _CROSS_CONDUCTOR
    else:
        for a, b in stem_segs or []:
            if boxes_overlap(box, _seg_box(a, b, _STEM_PAD)):
                score += _CROSS_CONDUCTOR
                break
    if index is not None:
        own_strokes = index._own_strokes.get(own_id, ())
        own_gap = min((box_gap(box, s) for s in own_strokes), default=None)
    else:
        own_gap = _gap_to_strokes(box, own_segs)
    if own_gap is not None:
        # A free slot next to this conductor beats an equally clear slot
        # several millimetres off the line (the 104/105/106 row).
        score += max(0.0, own_gap - (CLEAR + 0.6)) * 90.0
    skip = ignore_ids or set()
    # Companions in *skip* still pay for covering a stroke. They are not the
    # next phase, so they stay out of the nearest-foreign distance.
    # Later terms subtract at most the bundle bonus and the length bonus.
    # A seat that is already behind by that much cannot win.
    if ceiling is not None and score - (25.0 + 40.0 * 0.35) >= ceiling:
        return score
    if index is not None:
        score += _foreign_cover_cost(
            box, own_id, parts, None, strokes=index.stroke_hits(box)
        )
    else:
        score += _foreign_cover_cost(box, own_id, parts, None)
    # A bundle note belongs on the left of its phase. A plain gauge does not:
    # that bonus parked 2/0 AWG on the motor side of a shared conductor while
    # 105 and 106 stayed on the open side.
    if bundle and (box[2] - box[0]) >= 8.0 and align == "end":
        score -= 25.0
    if ceiling is not None and score - (40.0 * 0.35) >= ceiling:
        return score
    if obstacles is not None:
        pin_hits = obstacles.pins.overlap_ids(box, 0.0)
        for i in sorted(pin_hits):
            pin = pin_boxes[i]
            # Symbol bodies are stored with a sentinel width so a label can leave
            # the 4 mm gap between stacked Circuit Builder symbols.
            score += 3200.0 if (pin[2] - pin[0]) > 8.0 and (pin[3] - pin[1]) > 8.0 else 600.0
    else:
        for pin in pin_boxes:
            if boxes_overlap(box, pin):
                # Symbol bodies are stored with a sentinel width so a label can leave
                # the 4 mm gap between stacked Circuit Builder symbols.
                score += 3200.0 if (pin[2] - pin[0]) > 8.0 and (pin[3] - pin[1]) > 8.0 else 600.0
    span = (
        pts_span
        if pts_span is not None
        else (sum(hypot(b.x - a.x, b.y - a.y) for a, b in zip(pts, pts[1:])) if pts else 0.0)
    )
    # Only a stub shorter than the glyph. 104 beside F1:4 was a fraction of a
    # millimetre from the next pole. The same penalty on a long run (star-delta
    # 105 on K2:T2) made the only nearer-own seat lose to one nearer 106.
    if 0.0 < span < 4.0:
        if obstacles is not None:
            for _i in obstacles.captions.overlap_ids(box, 1.0):
                score += 3600.0
        else:
            for caption in caption_boxes or []:
                if boxes_overlap(box, caption, pad=1.0):
                    score += 3600.0
    if obstacles is not None:
        for _i in obstacles.tags.overlap_ids(box, 0.0):
            # Circuit Builder treats this band as the device tag. It has to
            # lose to a clear slot along the lead, and still lose to a real
            # conductor crossing.
            score += 4500.0
        for _i in obstacles.placed.overlap_ids(box, MARK_GAP):
            # Must beat conductor/dot hits. A 4 mm Circuit Builder gap used to
            # pick the cheap left slot on 102 even when it sat on 101.
            score += 2800.0
    else:
        for band in tag_bands or []:
            if boxes_overlap(box, band):
                # Circuit Builder treats this band as the device tag. It has to
                # lose to a clear slot along the lead, and still lose to a real
                # conductor crossing.
                score += 4500.0
        for other in placed:
            if boxes_overlap(box, other, pad=MARK_GAP):
                # Must beat conductor/dot hits. A 4 mm Circuit Builder gap used to
                # pick the cheap left slot on 102 even when it sat on 101.
                score += 2800.0
    score += _split_column_penalty(box, own_segs, placed)
    if pts:
        if pts_bounds is not None:
            x0, y0, x1, y1 = pts_bounds
        else:
            y0, y1 = min(p.y for p in pts), max(p.y for p in pts)
            x0, x1 = min(p.x for p in pts), max(p.x for p in pts)
        cy = (box[1] + box[3]) / 2.0
        cx = (box[0] + box[2]) / 2.0
        if vertical_pref and (cy < y0 - 1.2 or cy > y1 + 1.2):
            score += 400.0
        if not vertical_pref and (cx < x0 - 1.2 or cx > x1 + 1.2):
            score += 200.0
        if vertical_pref and align == "middle":
            score += 80.0
    score -= min(length, 40.0) * 0.35
    if ceiling is not None and score >= ceiling:
        return score
    if index is not None:
        crossed = index.conductor_between(box, own_id)
    else:
        crossed = _other_conductor_between(box, own_id, parts)
    if crossed:
        score += _CROSS_CONDUCTOR
    if anchor is not None and _anchor_close_to_own_vertical(anchor, own_id, parts):
        # A wide single gauge (350 kcmil) has to park its right edge inside
        # CLEAR. The full penalty let that seat lose to one nearer the next
        # phase. A narrow number still pays the full amount, so its CLEAR
        # seat stays put.
        if align == "end" and (box[2] - box[0]) >= 12.0:
            score += _CLOSE_OWN - 800.0
        else:
            score += _CLOSE_OWN
    # IEC / 6-lead / I/O penalties from main. A slot that already clears the
    # symbol body is unchanged; these only reject ink on the drawn outline,
    # a motor lead strip, a winding channel, or a generated I/O body.
    if obstacles is not None:
        if obstacles.outlines.overlap_ids(box, 0.15):
            score += 8000.0
    elif any(boxes_overlap(box, outline, pad=0.15) for outline in (outline_boxes or [])):
        score += 8000.0
    if schematic_boxes:
        cx = (box[0] + box[2]) * 0.5
        cy = (box[1] + box[3]) * 0.5
        if obstacles is not None:
            bodies = obstacles.schematic.boxes_at(cx, cy)
        else:
            bodies = schematic_boxes
        for body in bodies:
            if body[0] <= cx <= body[2] and body[1] <= cy <= body[3]:
                score += 5000.0
                break
    if obstacles is not None:
        for _i in obstacles.io.overlap_ids(box, 0.0):
            # A graze of a generated I/O body has to lose to a slot a few
            # millimetres further along the lead.
            score += 9000.0
        for _i in obstacles.leads.overlap_ids(box, 0.0):
            score += 450.0
        for i in sorted(obstacles.channels.overlap_ids(box, 0.0)):
            channel = (channel_boxes or [])[i]
            cx = (box[0] + box[2]) / 2.0
            cy = (box[1] + box[3]) / 2.0
            if not pts or _dist_to_route(cx, cy, pts) > 3.5:
                score += 120.0
    else:
        for body in io_boxes or []:
            if boxes_overlap(box, body):
                # A graze of a generated I/O body has to lose to a slot a few
                # millimetres further along the lead.
                score += 9000.0
        for lead in lead_boxes or []:
            if boxes_overlap(box, lead):
                score += 450.0
        for channel in channel_boxes or []:
            if not boxes_overlap(box, channel):
                continue
            cx = (box[0] + box[2]) / 2.0
            cy = (box[1] + box[3]) / 2.0
            if not pts or _dist_to_route(cx, cy, pts) > 3.5:
                score += 120.0
    # Nearest-foreign penalties only add. A seat that already lost can skip
    # that search; the winner is still scored in the same order as before.
    if ceiling is not None and score >= ceiling:
        return score
    nearest_foreign = 1.0e9
    if pts and index is not None:
        nearest_foreign = index.nearest_box(box, {own_id, *skip})
    # A slot equidistant from the next phase (102 sitting on 103) loses to one
    # that is actually nearer this wire. A real crossing is still _CROSS_CONDUCTOR.
    if pts and nearest_foreign < 1e8:
        own_box = box_route_distance(box, pts)
        if nearest_foreign <= own_box + 0.15:
            score += 160.0 + (own_box + 0.15 - nearest_foreign) * 200.0
        # The shop check uses the box center. A wide gauge can clear both
        # strokes and still sit closer to the next phase.
        cx = (box[0] + box[2]) * 0.5
        cy = (box[1] + box[3]) * 0.5
        own_c = _dist_to_route(cx, cy, pts)
        foreign_c = (
            index.nearest_point(cx, cy, {own_id, *skip}) if index is not None else 1.0e9
        )
        if foreign_c + 0.35 < own_c:
            score += 8000.0
    return score


def _anchor_close_to_own_vertical(
    anchor: tuple[float, float],
    own_id: str,
    parts: dict[str, tuple[list[tuple[Point, Point]], list[tuple[float, float]]]],
) -> bool:
    """True when the number's anchor is inside the clearance of its own vertical run."""

    ax, ay = anchor
    for a, b in parts.get(own_id, ([], []))[0]:
        if abs(a.x - b.x) > 0.4:
            continue
        if min(a.y, b.y) - 0.5 <= ay <= max(a.y, b.y) + 0.5 and abs(ax - a.x) < CLEAR - 0.2:
            return True
    return False


def _axis_spans(pts: list[Point], *, vertical: bool) -> list[tuple[float, float, float]]:
    """(axis, lo, hi) for segments long enough to host a number."""

    found: list[tuple[float, float, float]] = []
    for a, b in zip(pts, pts[1:]):
        if vertical:
            if abs(a.x - b.x) > 0.6 or abs(a.y - b.y) < 2.0:
                continue
            found.append(((a.x + b.x) / 2.0, min(a.y, b.y), max(a.y, b.y)))
        else:
            if abs(a.y - b.y) > 0.6 or abs(a.x - b.x) < 2.0:
                continue
            found.append(((a.y + b.y) / 2.0, min(a.x, b.x), max(a.x, b.x)))
    return found


def routes_near(a: list[Point], b: list[Point], *, gap: float = 12.0) -> bool:
    """True when two routes sit close enough that two copies of one number read as a pair."""

    if len(a) < 2 or len(b) < 2:
        return False

    def _box(pts: list[Point]) -> tuple[float, float, float, float]:
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    ax0, ay0, ax1, ay1 = _box(a)
    bx0, by0, bx1, by1 = _box(b)
    return not (ax1 + gap < bx0 or bx1 + gap < ax0 or ay1 + gap < by0 or by1 + gap < ay0)


def routes_share_run(a: list[Point], b: list[Point], *, overlap: float = 2.0) -> bool:
    """True when two routes share a collinear stretch (same number twice on one line)."""

    for vertical in (False, True):
        for axis, lo, hi in _axis_spans(a, vertical=vertical):
            for other, olo, ohi in _axis_spans(b, vertical=vertical):
                if abs(axis - other) > 0.85:
                    continue
                if min(hi, ohi) - max(lo, olo) >= overlap:
                    return True
    return False


def _almost(a: float, b: float, tol: float = 0.65) -> bool:
    return abs(a - b) <= tol


def _line_spans(pts: list[Point]) -> list[tuple[str, float, float, float]]:
    """Axis-aligned runs longer than 1 mm: (h|v, fixed, lo, hi)."""

    spans: list[tuple[str, float, float, float]] = []
    for start, end in zip(pts, pts[1:]):
        if _almost(start.y, end.y) and abs(start.x - end.x) > 1.0:
            lo, hi = (start.x, end.x) if start.x <= end.x else (end.x, start.x)
            spans.append(("h", start.y, lo, hi))
        elif _almost(start.x, end.x) and abs(start.y - end.y) > 1.0:
            lo, hi = (start.y, end.y) if start.y <= end.y else (end.y, start.y)
            spans.append(("v", start.x, lo, hi))
    return spans


def _spans_read_as_one(
    left: tuple[str, float, float, float],
    right: tuple[str, float, float, float],
) -> bool:
    """Same line, and close enough that one number stands for both.

    A straight drop on each side of an overload shares an x but the symbol
    sits between them. Each side keeps its number. A jog that only breaks
    the riser for a few millimetres still prints once.
    """

    if left[0] != right[0] or not _almost(left[1], right[1]):
        return False
    gap = max(left[2], right[2]) - min(left[3], right[3])
    return gap <= 8.0


def routes_share_line(pts_a: list[Point], pts_b: list[Point]) -> bool:
    """True when two routes share a rung or riser that reads as one conductor.

    Same-net labels on that run print once. Segments separated by a device
    do not: the number has to show on both sides of the symbol.
    """

    for left in _line_spans(pts_a):
        for right in _line_spans(pts_b):
            if _spans_read_as_one(left, right):
                return True
    return False


def routes_share_collinear_run(pts_a: list[Point], pts_b: list[Point]) -> bool:
    """True when two routes share a horizontal or vertical stretch (a tee's long side)."""

    def axis(a: Point, b: Point) -> str:
        if _almost(a.x, b.x):
            return "v"
        if _almost(a.y, b.y):
            return "h"
        return ""

    def overlap_1d(a0: float, a1: float, b0: float, b1: float) -> bool:
        lo_a, hi_a = (a0, a1) if a0 <= a1 else (a1, a0)
        lo_b, hi_b = (b0, b1) if b0 <= b1 else (b1, b0)
        return min(hi_a, hi_b) - max(lo_a, lo_b) > 1.0

    for a1, a2 in zip(pts_a, pts_a[1:]):
        ax = axis(a1, a2)
        if not ax:
            continue
        for b1, b2 in zip(pts_b, pts_b[1:]):
            if axis(b1, b2) != ax:
                continue
            if ax == "h" and _almost(a1.y, b1.y) and overlap_1d(a1.x, a2.x, b1.x, b2.x):
                return True
            if ax == "v" and _almost(a1.x, b1.x) and overlap_1d(a1.y, a2.y, b1.y, b2.y):
                return True
    return False


def _bundle_column(pts: list[Point]) -> tuple[float, float, float] | None:
    """(x, y0, y1) used to decide which phase wires share one gauge note.

    The host x is the longest vertical. A jog within 2.5 mm of that column
    (the 100 A breaker into a 32 A contactor) is part of the same span, so
    101/102/103 overlap and share ``3 x 4 AWG`` instead of each printing a
    note wide enough to sit on the next conductor. The span is that union:
    a jog of short pieces still counts when the run is at least 6 mm.
    """

    verts: list[tuple[float, float, float, float]] = []
    for a, b in zip(pts, pts[1:]):
        if abs(a.x - b.x) > 0.6:
            continue
        length = abs(a.y - b.y)
        if length < 2.0:
            continue
        x = (a.x + b.x) / 2.0
        lo, hi = min(a.y, b.y), max(a.y, b.y)
        verts.append((length, x, lo, hi))
    if not verts:
        return None
    longest = max(verts, key=lambda item: item[0])
    x0, lo, hi = longest[1], longest[2], longest[3]
    for _length, x, olo, ohi in verts:
        if abs(x - x0) <= 2.5:
            lo = min(lo, olo)
            hi = max(hi, ohi)
    # A jog can be several 2 mm pieces. The column is the union, not one piece,
    # so 101/102/103 still share a note. A stub shorter than 6 mm stays out.
    if hi - lo < 6.0:
        return None
    return (x0, lo, hi)


def bundle_spec_plan(
    sheet: Sheet,
    routes: dict[str, list[Point]],
    *,
    ladder: bool = False,
) -> dict[str, str]:
    """Spec drawn under each wire number.

    Ladder sheets keep the wire number only. On a schematic, parallel vertical
    conductors of the same gauge within one phase pitch share one note,
    ``3 x 14 AWG``, on the leftmost wire. The others draw no gauge.
    """

    plan = {wire.id: "" if ladder else wire.spec_text() for wire in sheet.wires}
    if ladder:
        return plan
    spans: dict[str, tuple[float, float, float]] = {}
    for wire in sheet.wires:
        if not plan.get(wire.id):
            continue
        column = _bundle_column(routes.get(wire.id) or [])
        if column is not None:
            spans[wire.id] = column
    ids = [wire.id for wire in sheet.wires if wire.id in spans]
    ids.sort(key=lambda wid: spans[wid][0])
    used: set[str] = set()
    for wid in ids:
        if wid in used:
            continue
        group = [wid]
        used.add(wid)
        _x0, lo0, hi0 = spans[wid]
        spec = plan[wid]
        for other in ids:
            if other in used or plan.get(other) != spec:
                continue
            ox, olo, ohi = spans[other]
            if ox < spans[group[-1]][0] - 0.05:
                continue
            overlaps = min(hi0, ohi) - max(lo0, olo) >= 6.0
            # Same drawn column (star-delta 104/105/106 stacked by the router).
            # A line-side L1 at this x is a different run from load-side 101, so
            # same x only counts when the vertical spans actually overlap.
            # Do not grow the span: a 6 mm link on that line is not another conductor.
            if any(abs(ox - spans[item][0]) <= COINCIDENT_COLUMN for item in group):
                if overlaps:
                    used.add(other)
                    # Same drawn stroke. One gauge on the host, not a copy on each number.
                    plan[other] = ""
                continue
            if ox - spans[group[-1]][0] > 14.0:
                break
            if ox - spans[group[0]][0] > 36.0:
                break
            if not overlaps:
                continue
            group.append(other)
            used.add(other)
            lo0, hi0 = min(lo0, olo), max(hi0, ohi)
        if len(group) < 2:
            continue
        host = group[0]
        plan[host] = f"{len(group)} x {spec}"
        for other in group[1:]:
            plan[other] = ""
    # A stub shorter than the gauge glyph cannot carry "4 AWG" without
    # covering the next conductor. The number still prints. A bundle note
    # on a real column is left alone.
    for wid, spec in list(plan.items()):
        if not spec or bundle_note(spec):
            continue
        pts = routes.get(wid) or []
        longest = max(
            (hypot(b.x - a.x, b.y - a.y) for a, b in zip(pts, pts[1:])),
            default=0.0,
        )
        # The note is the number plus the gauge. A run shorter than that
        # glyph covers the symbols the lead lands on. The number still prints.
        box = mark_box(0.0, 0.0, "00", align="start", spec=spec)
        if longest < (box[3] - box[1]):
            plan[wid] = ""
    return plan


_mark_cache: ContextVar[dict | None] = ContextVar("panelcad_wire_mark_cache", default=None)
_PLACE_CALLS = 0
_PLACE_COMPUTES = 0


def reset_wire_mark_stats() -> None:
    """Zero the call counters used by the reload regression."""

    global _PLACE_CALLS, _PLACE_COMPUTES
    _PLACE_CALLS = 0
    _PLACE_COMPUTES = 0


def wire_mark_stats() -> tuple[int, int]:
    """(calls, computes) since the last reset.

    A sheet reload calls placement several times and computes it once.
    """

    return _PLACE_CALLS, _PLACE_COMPUTES


@contextmanager
def share_wire_marks():
    """Share one ``place_wire_marks`` result for each sheet during a reload."""

    if _mark_cache.get() is not None:
        yield
        return
    token = _mark_cache.set({})
    try:
        yield
    finally:
        _mark_cache.reset(token)


def _sheet_token(sheet: Sheet) -> tuple:
    wires = tuple(
        (
            wire.id,
            str(getattr(wire, "frm", "") or ""),
            str(getattr(wire, "to", "") or ""),
            str(getattr(wire, "number", "") or ""),
            str(getattr(wire, "cable", "") or ""),
            str(getattr(wire, "spec", "") or ""),
        )
        for wire in sheet.wires
    )
    comps = tuple(
        (
            comp.id,
            comp.library_id,
            float(comp.x),
            float(comp.y),
            int(comp.rotation or 0),
            bool(comp.mirror),
            float(comp.visual_scale() or 1.0),
        )
        for comp in sheet.components
    )
    return (
        sheet.id,
        sheet.kind,
        str(getattr(sheet, "drawing_style", "") or ""),
        str(getattr(sheet, "standard", "") or ""),
        wires,
        comps,
    )


def _routes_token(routes: dict[str, list[Point]]) -> tuple:
    return tuple(
        (wid, tuple((pt.x, pt.y) for pt in routes[wid]))
        for wid in sorted(routes)
    )


def _wire_mark_key(
    sheet: Sheet,
    catalog: Catalog,
    routes: dict[str, list[Point]],
    ladder: bool,
    project: object,
) -> tuple:
    iec_placing = False
    if project is not None and str(getattr(sheet, "drawing_style", "") or "") == "iec":
        from panelcad.render import xref

        iec_placing = bool(xref._PLACING)
    return (
        bool(ladder),
        iec_placing,
        id(catalog),
        id(project) if project is not None else 0,
        _sheet_token(sheet),
        _routes_token(routes),
    )


class _MarkFields:
    """Overlap indexes for the boxes ``_score`` used to scan one by one."""

    def __init__(
        self,
        pin_boxes: list[tuple[float, float, float, float]],
        stem_segs: list[tuple[Point, Point]],
        caption_boxes: list[tuple[float, float, float, float]],
        tag_bands: list[tuple[float, float, float, float]],
        outline_boxes: list[tuple[float, float, float, float]],
        schematic_boxes: list[tuple[float, float, float, float]],
        io_boxes: list[tuple[float, float, float, float]],
        lead_boxes: list[tuple[float, float, float, float]],
        channel_boxes: list[tuple[float, float, float, float]],
    ) -> None:
        from panelcad.render.xref import _BoxIndex

        self.pins = _BoxIndex(pin_boxes)
        self.stems = _BoxIndex([_seg_box(a, b, _STEM_PAD) for a, b in stem_segs])
        self.captions = _BoxIndex(caption_boxes)
        self.tags = _BoxIndex(tag_bands)
        self.outlines = _BoxIndex(outline_boxes)
        self.schematic = _BoxIndex(schematic_boxes)
        self.io = _BoxIndex(io_boxes)
        self.leads = _BoxIndex(lead_boxes)
        self.channels = _BoxIndex(channel_boxes)
        self.placed = _BoxIndex()

    def add_placed(self, box: tuple[float, float, float, float]) -> None:
        self.placed.add(box)


def place_wire_marks(
    sheet: Sheet,
    catalog: Catalog,
    routes: dict[str, list[Point]],
    *,
    ladder: bool = False,
    project: Project | None = None,
) -> dict[str, tuple[float, float, str]]:
    """wire id → (x, y, align) for the number. Empty when the wire has no mark."""

    global _PLACE_CALLS, _PLACE_COMPUTES
    _PLACE_CALLS += 1
    cache = _mark_cache.get()
    key = None
    if cache is not None:
        key = _wire_mark_key(sheet, catalog, routes, ladder, project)
        if key in cache:
            return dict(cache[key])
    _PLACE_COMPUTES += 1

    from panelcad.render.titleblock import working_rect

    pin_boxes = (
        _pin_obstacles(sheet, catalog)
        + _ref_obstacles(sheet, catalog, ladder=ladder)
        + _body_obstacles(sheet, catalog)
    )
    iec = str(getattr(sheet, "drawing_style", "") or "") == "iec"
    if project is not None and iec:
        from panelcad.render.xref import iec_mirror_pockets

        pin_boxes = pin_boxes + iec_mirror_pockets(project, catalog, sheet)
    lead_boxes = _motor_lead_obstacles(sheet, catalog)
    channel_boxes = _winding_channel(sheet, catalog)
    outline_boxes = device_outline_boxes(sheet, catalog)
    schematic_boxes = _schematic_bodies(sheet, catalog)
    io_boxes = _io_body_obstacles(sheet, catalog)
    tag_bands = _tag_bands(sheet)
    caption_boxes = _caption_obstacles(sheet, catalog)
    # Nearer than CLEAR only on IEC columns and generated I/O. NEMA power
    # seats (the locked 5 hp coordinates) stay on the CLEAR grid.
    close = iec or bool(io_boxes)
    tight = (not close) and str(getattr(sheet, "standard", "") or "").upper().startswith("IEC")
    bounds = working_rect(sheet)
    parts = _route_parts(routes)
    route_index = _RouteIndex(parts)
    stem_segs = _drawn_lead_segments(sheet, catalog)
    obstacles = _MarkFields(
        pin_boxes,
        stem_segs,
        caption_boxes,
        tag_bands,
        outline_boxes,
        schematic_boxes,
        io_boxes,
        lead_boxes,
        channel_boxes,
    )
    placed: list[tuple[float, float, float, float]] = []
    out: dict[str, tuple[float, float, str]] = {}
    by_id = {wire.id: wire for wire in sheet.wires}
    specs = bundle_spec_plan(sheet, routes, ladder=ladder)
    # Rail names stay on every drop. Same-X dedupe would collapse every 24V
    # hot onto the left rail. On a power sheet L1 and L2 are phase conductors,
    # so one mark is enough. Ladder and IEC columns still repeat L1/L2 on each drop.
    rail_marks = {"24V", "0V", "X1", "X2", "L+", "M", "N"}
    if ladder:
        rail_marks.update({"L1", "L2"})
    wires = list(sheet.wires)

    def _span(item: object) -> float:
        pts = routes.get(getattr(item, "id", "")) or []
        return sum(hypot(b.x - a.x, b.y - a.y) for a, b in zip(pts, pts[1:]))

    # The long run keeps a repeated number. A 2 mm jumper used to print the
    # mark and the conductor that actually carries it stayed blank.
    wires.sort(key=_span, reverse=True)
    suppressed: set[str] = set()
    grouped: dict[str, list] = {}
    for wire in wires:
        token = wire.drawn_mark(ladder=ladder)
        if not token or token in rail_marks:
            continue
        grouped.setdefault(token, []).append(wire)
    for group in grouped.values():
        if len(group) < 2:
            continue
        parent = list(range(len(group)))

        def _find(idx: int, parent: list[int] = parent) -> int:
            while parent[idx] != idx:
                parent[idx] = parent[parent[idx]]
                idx = parent[idx]
            return idx

        for left_i, left in enumerate(group):
            left_pts = routes.get(left.id) or []
            for right_i in range(left_i + 1, len(group)):
                right = group[right_i]
                right_pts = routes.get(right.id) or []
                if routes_share_collinear_run(left_pts, right_pts) or routes_share_line(left_pts, right_pts):
                    parent[_find(right_i)] = _find(left_i)
        clusters: dict[int, list] = {}
        for idx, wire in enumerate(group):
            clusters.setdefault(_find(idx), []).append(wire)
        for cluster in clusters.values():
            if len(cluster) < 2:
                continue
            keeper = max(cluster, key=_span)
            for wire in cluster:
                if wire.id != keeper.id:
                    suppressed.add(wire.id)
    for wire in wires:
        if wire.id in suppressed:
            continue
        mark = wire.drawn_mark(ladder=ladder)
        if not mark:
            continue
        pts = routes.get(wire.id) or []
        if not ladder and _jumper_repeats_terminal(sheet, catalog, wire, mark, pts):
            continue
        if mark not in rail_marks:
            # One number per power node. The longest run was sorted first, so
            # a second L1 beside the reversing contactor does not print again.
            if not ladder and any(
                (other := by_id.get(other_id)) is not None and other.drawn_mark(ladder=ladder) == mark
                for other_id in out
            ):
                continue
            already = False
            for other_id in out:
                other = by_id.get(other_id)
                if other is None or other.drawn_mark(ladder=ladder) != mark:
                    continue
                cable = str(wire.cable or "").strip()
                if cable and str(other.cable or "").strip() != cable:
                    continue
                other_pts = routes.get(other_id) or []
                if (
                    routes_share_line(pts, other_pts)
                    or routes_share_collinear_run(pts, other_pts)
                    or routes_share_run(pts, other_pts)
                    or routes_near(pts, other_pts)
                ):
                    already = True
                    break
            if already:
                continue
        spec = specs.get(wire.id, "")
        prefer_vert = _vertical_pref(pts)
        companions = {
            other.id
            for other in sheet.wires
            if other.id != wire.id
            and other.drawn_mark(ladder=ladder) == mark
            and (
                routes_share_line(pts, routes.get(other.id) or [])
                or routes_share_collinear_run(pts, routes.get(other.id) or [])
                or routes_near(pts, routes.get(other.id) or [])
            )
        }
        best: tuple[float, float, str] | None = None
        best_score = 10**12
        pts_span = sum(hypot(b.x - a.x, b.y - a.y) for a, b in zip(pts, pts[1:])) if pts else 0.0
        pts_bounds = (
            (min(p.x for p in pts), min(p.y for p in pts), max(p.x for p in pts), max(p.y for p in pts))
            if pts
            else None
        )
        foreign = [routes.get(other.id) or [] for other in sheet.wires if other.id != wire.id]
        for x, y, align, length in _candidates(
            pts, mark, spec, close=close, tight=tight, foreign=foreign
        ):
            box = mark_box(x, y, mark, align=align, spec=spec)
            score = _score(
                box,
                pts,
                wire.id,
                parts,
                pin_boxes,
                placed,
                length,
                prefer_vert,
                align,
                anchor=(x, y),
                bounds=bounds,
                bundle=bundle_note(spec),
                lead_boxes=lead_boxes,
                channel_boxes=channel_boxes,
                outline_boxes=outline_boxes,
                schematic_boxes=schematic_boxes,
                io_boxes=io_boxes,
                ignore_ids=companions,
                tag_bands=tag_bands,
                caption_boxes=caption_boxes,
                stem_segs=stem_segs,
                route_index=route_index,
                obstacles=obstacles,
                ceiling=best_score,
                pts_span=pts_span,
                pts_bounds=pts_bounds,
            )
            if score < best_score:
                best = (x, y, align)
                best_score = score
        if best is None:
            best = fallback_anchor(pts)
        out[wire.id] = best
        seated = mark_box(best[0], best[1], mark, align=best[2], spec=spec)
        placed.append(seated)
        obstacles.add_placed(seated)
    _separate_marks(out, sheet, routes, ladder=ladder, specs=specs, bounds=bounds)
    _seat_phase_marks(out, sheet, catalog, routes, ladder=ladder, specs=specs, bounds=bounds)
    _nudge_off_pins(out, sheet, pin_boxes + io_boxes, routes, ladder=ladder, specs=specs)
    if io_boxes:
        _collapse_close_numbers(out, sheet, routes)
        _drop_body_marks(out, sheet, io_boxes, ladder=ladder)
    if cache is not None and key is not None:
        cache[key] = dict(out)
    return out


def _vertical_column(pts: list[Point]) -> tuple[float, float, float] | None:
    """(x, y0, y1) of the longest vertical run on this route."""

    bins: dict[float, list[float]] = {}
    for a, b in zip(pts, pts[1:]):
        if abs(a.x - b.x) > 0.5 or abs(a.y - b.y) < 1.0:
            continue
        key = round((a.x + b.x) / 2.0, 2)
        lo, hi = min(a.y, b.y), max(a.y, b.y)
        span = bins.setdefault(key, [lo, hi, 0.0])
        span[0] = min(span[0], lo)
        span[1] = max(span[1], hi)
        span[2] += hi - lo
    if not bins:
        return None
    x, (lo, hi, _length) = max(bins.items(), key=lambda item: item[1][2])
    if hi - lo < 2.0:
        return None
    return (x, lo, hi)


def _seat_phase_marks(
    out: dict[str, tuple[float, float, str]],
    sheet: Sheet,
    catalog: Catalog,
    routes: dict[str, list[Point]],
    *,
    ladder: bool,
    specs: dict[str, str],
    bounds: tuple[float, float, float, float] | None = None,
) -> None:
    """Put each phase number on its own conductor, left to right.

    The scorer used to park every number in the right-hand gutter, so L2/L3
    and 102/103 read as one clump and the clump was not in phase order.
    """

    if ladder:
        return
    by_id = {wire.id: wire for wire in sheet.wires}
    columns: dict[str, tuple[float, float, float]] = {}
    for wid in out:
        column = _vertical_column(routes.get(wid) or [])
        if column is not None:
            columns[wid] = column
    ordered = sorted(columns, key=lambda wid: (columns[wid][0], wid))
    used: set[str] = set()
    bodies = _body_obstacles(sheet, catalog) + _ref_obstacles(sheet, catalog, ladder=False)
    # The circuit-builder tag check uses this band, which is taller than the glyph.
    bodies.extend(
        (
            comp.x - 0.8,
            comp.y - 5.6,
            comp.x + max(8.0, len(comp.id) * 2.0),
            comp.y - 0.3,
        )
        for comp in sheet.components
        if not int(comp.rotation or 0)
    )
    wires = _route_parts(routes)
    for wid in ordered:
        if wid in used:
            continue
        group = [wid]
        used.add(wid)
        x0, lo, hi = columns[wid]
        for other in ordered:
            if other in used:
                continue
            ox, olo, ohi = columns[other]
            if ox < columns[group[-1]][0] - 0.1:
                continue
            if ox - columns[group[-1]][0] > 14.0:
                break
            if ox - x0 > 36.0:
                break
            if min(hi, ohi) - max(lo, olo) < 2.0:
                continue
            group.append(other)
            used.add(other)
            lo, hi = min(lo, olo), max(hi, ohi)
        if len(group) < 2:
            continue
        if not any(bundle_note(specs.get(item, "")) for item in group):
            continue
        taken = [
            mark_box(
                out[other][0],
                out[other][1],
                by_id[other].drawn_mark(ladder=False),
                align=out[other][2],
                spec=specs.get(other, ""),
            )
            for other in out
            if other not in group and other in by_id
        ]
        for item in group:
            wire = by_id.get(item)
            if wire is None:
                continue
            mark = wire.drawn_mark(ladder=False)
            spec = specs.get(item, "")
            seated = _seat_beside(
                columns[item],
                mark,
                spec,
                bodies,
                wires,
                taken,
                bounds=bounds,
            )
            if seated is None:
                continue
            x, y, align, box = seated
            out[item] = (x, y, align)
            taken.append(box)


def _seat_beside(
    column: tuple[float, float, float],
    mark: str,
    spec: str,
    bodies: list[tuple[float, float, float, float]],
    wires: dict[str, tuple[list[tuple[Point, Point]], list[tuple[float, float]]]],
    taken: list[tuple[float, float, float, float]],
    bounds: tuple[float, float, float, float] | None = None,
) -> tuple[float, float, str, tuple[float, float, float, float]] | None:
    """Number to the left of this conductor, in the open gap when there is one."""

    wire_x, lo, hi = column
    span = max(hi - lo, 0.4)
    steps = max(12, int(span / 0.25))
    mid = (lo + hi) / 2.0
    best: tuple[float, float, str, tuple[float, float, float, float]] | None = None
    best_rank = 10**9
    # The device tag sits just left of the symbol, so the left-hand conductor
    # sometimes has to step a little further left. Middle and right stay in their bay.
    for pad in (CLEAR, CLEAR + 1.2, CLEAR + 2.4, CLEAR + 4.0, CLEAR + 6.0, CLEAR + 8.2):
        anchor = wire_x - pad
        for i in range(steps + 1):
            y = lo + span * i / steps
            box = mark_box(anchor, y, mark, align="end", spec=spec)
            if not _inside_working(box, bounds):
                continue
            if any(boxes_overlap(box, body) for body in bodies):
                continue
            if any(boxes_overlap(box, prior, pad=MARK_GAP) for prior in taken):
                continue
            if _box_hits_wires(box, wires):
                continue
            cy = (box[1] + box[3]) / 2.0
            if cy < lo - 1.2 or cy > hi + 1.2:
                continue
            rank = abs(y - mid) + pad
            if rank < best_rank:
                best = (anchor, y, "end", box)
                best_rank = rank
        if best is not None and best_rank <= CLEAR + 0.2:
            break
    return best


def _box_hits_route(
    box: tuple[float, float, float, float],
    routes: dict[str, list[Point]],
) -> bool:
    """True when the drawn number, including its gauge note, covers a stroke."""

    for pts in routes.values():
        for a, b in zip(pts, pts[1:]):
            if boxes_overlap(box, _seg_box(a, b, 0.45)):
                return True
    return False


def _box_hits_wires(
    box: tuple[float, float, float, float],
    wires: dict[str, tuple[list[tuple[Point, Point]], list[tuple[float, float]]]],
) -> bool:
    for segs, ends in wires.values():
        for x, y in ends:
            if boxes_overlap(box, _dot_box(x, y, 1.2)):
                return True
        for a, b in segs:
            # The sweep pads segments by 0.45 mm. 0.35 let reversing L2/L3 graze the bus.
            if boxes_overlap(box, _seg_box(a, b, 0.50)):
                return True
    return False


def _clear_of_route(x: float, y: float, pts: list[Point]) -> bool:
    if not pts:
        return True
    if min(hypot(x - pts[0].x, y - pts[0].y), hypot(x - pts[-1].x, y - pts[-1].y)) < 2.0:
        return False
    for a, b in zip(pts, pts[1:]):
        if abs(b.x - a.x) <= 0.4 and min(a.y, b.y) - 0.5 <= y <= max(a.y, b.y) + 0.5:
            if abs(x - a.x) < CLEAR - 0.2:
                return False
        seg = _seg_box(a, b, 0.45)
        probe = (x - 0.2, y - 0.2, x + 0.2, y + 0.2)
        if boxes_overlap(probe, seg):
            return False
    return True


def _separate_marks(
    out: dict[str, tuple[float, float, str]],
    sheet: Sheet,
    routes: dict[str, list[Point]],
    *,
    ladder: bool,
    specs: dict[str, str] | None = None,
    bounds: tuple[float, float, float, float] | None = None,
) -> None:
    """Last pass: if two numbers still overlap, nudge the later one off."""

    by_id = {wire.id: wire for wire in sheet.wires}
    drawn = specs if specs is not None else {}

    def _box(wid: str) -> tuple[float, float, float, float]:
        x, y, align = out[wid]
        wire = by_id[wid]
        return mark_box(
            x,
            y,
            wire.drawn_mark(ladder=ladder),
            align=align,
            spec=drawn.get(wid, "" if specs is not None else wire.spec_text()),
        )

    ids = [wid for wid in out if by_id.get(wid) is not None]
    boxes = {wid: _box(wid) for wid in ids}
    for _ in range(8):
        moved = False
        for i, a in enumerate(ids):
            box_a = boxes[a]
            for b in ids[i + 1 :]:
                box_b = boxes[b]
                if box_gap(box_a, box_b) >= MARK_GAP:
                    continue
                bx, by, balign = out[b]
                pts = routes.get(b) or []
                height = box_b[3] - box_b[1]
                width = box_b[2] - box_b[0]
                for dx, dy in (
                    (0.0, height + 0.8),
                    (0.0, -(height + 0.8)),
                    (width + 0.8, 0.0),
                    (-(width + 0.8), 0.0),
                    (0.0, height + 2.4),
                    (0.0, -(height + 2.4)),
                ):
                    trial = (bx + dx, by + dy, balign)
                    out[b] = trial
                    moved_box = _box(b)
                    if (
                        not _inside_working(moved_box, bounds)
                        or box_gap(box_a, moved_box) < MARK_GAP
                    ):
                        out[b] = (bx, by, balign)
                        continue
                    if _box_hits_route(moved_box, routes):
                        out[b] = (bx, by, balign)
                        continue
                    if _clear_of_route(trial[0], trial[1], pts):
                        boxes[b] = moved_box
                        moved = True
                        break
                    out[b] = (bx, by, balign)
                else:
                    out[b] = (bx, by, balign)
        if not moved:
            break


def _motor_lead_obstacles(sheet: Sheet, catalog: Catalog) -> list[tuple[float, float, float, float]]:
    """The strip where the phase feeders land on a motor. Numbers there pile on the tag."""

    from panelcad.core.route import component_aabb

    boxes: list[tuple[float, float, float, float]] = []
    for comp in sheet.components:
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        if not str(getattr(device, "category", "") or "").startswith("load.motor"):
            continue
        scale = float(comp.visual_scale() or 1.0)
        x0, y0, x1, y1 = component_aabb(
            comp.x,
            comp.y,
            float(device.schematic_width) * scale,
            float(device.schematic_height) * scale,
            int(comp.rotation or 0),
            bool(comp.mirror),
        )
        boxes.append((x0 - 1.0, y0 - 8.0, x0 + 8.0, y0 + 5.0))
        boxes.append((x0 + 2.0, y0 - 0.6, x1 + 1.0, y0 + 16.0))
    return boxes


def _winding_channel(sheet: Sheet, catalog: Catalog) -> list[tuple[float, float, float, float]]:
    """Open band beside a 6-lead motor, where end-wires run. Numbers there read as loose."""

    from panelcad.core.route import component_aabb

    boxes: list[tuple[float, float, float, float]] = []
    for comp in sheet.components:
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        if not str(getattr(device, "category", "") or "").startswith("load.motor"):
            continue
        if not any(pin.id in {"W2", "U2", "V2", "T4", "T5", "T6"} for pin in device.pins):
            continue
        scale = float(comp.visual_scale() or 1.0)
        x0, y0, x1, y1 = component_aabb(
            comp.x,
            comp.y,
            float(device.schematic_width) * scale,
            float(device.schematic_height) * scale,
            int(comp.rotation or 0),
            bool(comp.mirror),
        )
        boxes.append((x1 - 4.0, y0 + 20.0, x1 + 150.0, y1 + 8.0))
    return boxes


def device_outline_boxes(
    sheet: Sheet,
    catalog: Catalog,
) -> list[tuple[float, float, float, float]]:
    """World boxes of the drawn symbol outlines (rect primitives), not the lead span."""

    from panelcad.core.geometry import rotate_point

    boxes: list[tuple[float, float, float, float]] = []
    for comp in sheet.components:
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        scale = float(comp.visual_scale() or 1.0)
        rotation = int(comp.rotation or 0)
        mirror = bool(comp.mirror)
        for prim in device.primitives:
            if prim.type != "rect":
                continue
            corners = (
                (prim.x, prim.y),
                (prim.x + prim.w, prim.y),
                (prim.x + prim.w, prim.y + prim.h),
                (prim.x, prim.y + prim.h),
            )
            xs: list[float] = []
            ys: list[float] = []
            for x, y in corners:
                px, py = x * scale, y * scale
                if mirror:
                    px = -px
                rx, ry = rotate_point(px, py, rotation)
                xs.append(comp.x + rx)
                ys.append(comp.y + ry)
            boxes.append((min(xs), min(ys), max(xs), max(ys)))
    return boxes


def _schematic_bodies(sheet: Sheet, catalog: Catalog) -> list[tuple[float, float, float, float]]:
    """Placement rectangles. The center of a number must not sit inside one."""

    from panelcad.core.route import component_aabb

    boxes: list[tuple[float, float, float, float]] = []
    for comp in sheet.components:
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        if (device.category or "").lower().startswith("connection."):
            continue
        scale = float(comp.visual_scale() or 1.0)
        boxes.append(
            component_aabb(
                comp.x,
                comp.y,
                float(device.schematic_width) * scale,
                float(device.schematic_height) * scale,
                int(comp.rotation or 0),
                bool(comp.mirror),
            )
        )
    return boxes


def _axis_seg_box_distance(
    seg: _Seg,
    box: tuple[float, float, float, float],
) -> float:
    """Same distance ``nearest_box`` used to measure on one axis-aligned segment."""

    if box[2] < seg.x0:
        gx = seg.x0 - box[2]
    elif box[0] > seg.x1:
        gx = box[0] - seg.x1
    else:
        gx = 0.0
    if box[3] < seg.y0:
        gy = seg.y0 - box[3]
    elif box[1] > seg.y1:
        gy = box[1] - seg.y1
    else:
        gy = 0.0
    if gx == 0.0 and gy == 0.0:
        return 0.0
    if gx == 0.0:
        return gy
    if gy == 0.0:
        return gx
    return hypot(gx, gy)


def _axis_box_distance(
    box: tuple[float, float, float, float],
    a: Point,
    b: Point,
) -> float:
    """Euclidean distance from *box* to an axis-aligned segment."""

    sx0, sx1 = (a.x, b.x) if a.x <= b.x else (b.x, a.x)
    sy0, sy1 = (a.y, b.y) if a.y <= b.y else (b.y, a.y)
    if box[2] < sx0:
        gx = sx0 - box[2]
    elif box[0] > sx1:
        gx = box[0] - sx1
    else:
        gx = 0.0
    if box[3] < sy0:
        gy = sy0 - box[3]
    elif box[1] > sy1:
        gy = box[1] - sy1
    else:
        gy = 0.0
    if gx == 0.0 and gy == 0.0:
        return 0.0
    if gx == 0.0:
        return gy
    if gy == 0.0:
        return gx
    return hypot(gx, gy)


def _box_segment_distance(
    box: tuple[float, float, float, float],
    a: Point,
    b: Point,
) -> float:
    """Same value as ``box_route_distance`` for one segment."""

    if abs(a.x - b.x) < 1e-9 or abs(a.y - b.y) < 1e-9:
        return _axis_box_distance(box, a, b)
    return box_route_distance(box, [a, b])


def box_route_distance(
    box: tuple[float, float, float, float],
    pts: list[Point],
) -> float:
    """Minimum distance from a text box to a route. 0 when they meet."""

    if len(pts) < 2:
        return 1e9
    best = 1e9
    corners = (
        (box[0], box[1]),
        (box[2], box[1]),
        (box[0], box[3]),
        (box[2], box[3]),
    )
    for a, b in zip(pts, pts[1:]):
        if _segment_meets_box(a, b, box):
            return 0.0
        best = min(best, _point_box_distance(a.x, a.y, box), _point_box_distance(b.x, b.y, box))
        for x, y in corners:
            best = min(best, _point_segment_distance(x, y, a, b))
    return best


def _point_box_distance(x: float, y: float, box: tuple[float, float, float, float]) -> float:
    dx = 0.0 if box[0] <= x <= box[2] else (box[0] - x if x < box[0] else x - box[2])
    dy = 0.0 if box[1] <= y <= box[3] else (box[1] - y if y < box[1] else y - box[3])
    return hypot(dx, dy)


def _point_segment_distance(x: float, y: float, a: Point, b: Point) -> float:
    dx, dy = b.x - a.x, b.y - a.y
    span = dx * dx + dy * dy
    if span < 1e-9:
        return hypot(x - a.x, y - a.y)
    t = max(0.0, min(1.0, ((x - a.x) * dx + (y - a.y) * dy) / span))
    return hypot(x - (a.x + t * dx), y - (a.y + t * dy))


def _segment_meets_box(a: Point, b: Point, box: tuple[float, float, float, float]) -> bool:
    # The stroke lies inside its bounds, so a miss there is a miss.
    if not boxes_overlap((min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y)), box):
        return False
    # Axis-aligned overlap of the bounds is not enough for a diagonal.
    # Power routes are orthogonal, so a bounds hit is a real hit.
    if abs(a.x - b.x) <= 0.4 or abs(a.y - b.y) <= 0.4:
        return True
    steps = 8
    for i in range(steps + 1):
        t = i / steps
        x = a.x + (b.x - a.x) * t
        y = a.y + (b.y - a.y) * t
        if box[0] <= x <= box[2] and box[1] <= y <= box[3]:
            return True
    return False


def _dist_to_route(x: float, y: float, pts: list[Point]) -> float:
    """Distance from a point to the nearest segment of a route."""

    best = 1e9
    for a, b in zip(pts, pts[1:]):
        dx, dy = b.x - a.x, b.y - a.y
        span = dx * dx + dy * dy
        if span < 1e-9:
            dist = hypot(x - a.x, y - a.y)
        else:
            t = max(0.0, min(1.0, ((x - a.x) * dx + (y - a.y) * dy) / span))
            dist = hypot(x - (a.x + t * dx), y - (a.y + t * dy))
        if dist < best:
            best = dist
    return best


def _jumper_repeats_terminal(
    sheet: Sheet,
    catalog: Catalog,
    wire: object,
    mark: str,
    pts: list[Point],
) -> bool:
    """A short jumper whose wire number is already the terminal caption."""

    if not mark or len(pts) < 2:
        return False
    span = sum(hypot(b.x - a.x, b.y - a.y) for a, b in zip(pts, pts[1:]))
    if span > 12.0:
        return False
    from panelcad.core.pins import pin_caption
    from panelcad.render.sheet import parse_pin_ref

    iec = str(getattr(sheet, "standard", "") or "") == "IEC" or str(
        getattr(sheet, "drawing_style", "") or ""
    ) == "iec"
    for end in (getattr(wire, "frm", ""), getattr(wire, "to", "")):
        try:
            ref, pin_id = parse_pin_ref(str(end))
            comp = sheet.component(ref)
            device = catalog.get(comp.library_id)
            pin = device.pin(pin_id)
        except (KeyError, ValueError, AttributeError):
            continue
        cap = pin_caption(pin, comp)
        if cap != mark:
            continue
        drawn = {str(prim.text).strip() for prim in device.primitives if prim.type == "text" and prim.text}
        if cap != pin.id or (iec and cap not in drawn):
            return True
    return False


def _nudge_off_pins(
    out: dict[str, tuple[float, float, str]],
    sheet: Sheet,
    pin_boxes: list[tuple[float, float, float, float]],
    routes: dict[str, list[Point]],
    *,
    ladder: bool,
    specs: dict[str, str] | None = None,
) -> None:
    """Slide a number off a terminal caption when every candidate still hit one."""

    by_id = {wire.id: wire for wire in sheet.wires}
    drawn = specs if specs is not None else {}

    def _box(wid: str) -> tuple[float, float, float, float]:
        x, y, align = out[wid]
        wire = by_id[wid]
        spec = drawn.get(wid, "" if specs is not None else wire.spec_text())
        return mark_box(x, y, wire.drawn_mark(ladder=ladder), align=align, spec=spec)

    offsets = (
        (2.4, 0.0),
        (-2.4, 0.0),
        (0.0, 2.6),
        (0.0, -2.6),
        (4.2, 0.0),
        (-4.2, 0.0),
        (0.0, 4.4),
        (0.0, -4.4),
    )
    from panelcad.render.xref import _BoxIndex

    pins = _BoxIndex(pin_boxes)
    for wid in list(out):
        if by_id.get(wid) is None:
            continue
        for _attempt in range(6):
            box = _box(wid)
            if pins.count(box, 0.05, limit=0) == 0:
                break
            x, y, align = out[wid]
            placed = False
            for dx, dy in offsets:
                out[wid] = (x + dx, y + dy, align)
                trial = _box(wid)
                if pins.count(trial, 0.05, limit=0) > 0:
                    continue
                if any(boxes_overlap(trial, _box(other), pad=MARK_GAP) for other in out if other != wid):
                    continue
                if _box_hits_route(trial, routes):
                    continue
                placed = True
                break
            if not placed:
                out[wid] = (x, y, align)
                break


def _drop_body_marks(
    out: dict[str, tuple[float, float, str]],
    sheet: Sheet,
    bodies: list[tuple[float, float, float, float]],
    *,
    ladder: bool,
) -> None:
    """Drop a number that sits on a symbol when the same number is clear elsewhere."""

    if not bodies:
        return
    by_id = {wire.id: wire for wire in sheet.wires}

    def _box(wid: str) -> tuple[float, float, float, float]:
        x, y, align = out[wid]
        wire = by_id[wid]
        return mark_box(x, y, wire.drawn_mark(ladder=ladder), align=align, spec=wire.spec_text())

    def _hits(wid: str) -> bool:
        box = _box(wid)
        return any(boxes_overlap(box, body) for body in bodies)

    groups: dict[str, list[str]] = {}
    for wid in list(out):
        wire = by_id.get(wid)
        if wire is None:
            continue
        groups.setdefault(wire.drawn_mark(ladder=ladder), []).append(wid)
    for wids in groups.values():
        if not any(_hits(wid) for wid in wids):
            continue
        if not any(not _hits(wid) for wid in wids):
            continue
        for wid in wids:
            if _hits(wid):
                out.pop(wid, None)


def _route_length(pts: list[Point]) -> float:
    return sum(hypot(b.x - a.x, b.y - a.y) for a, b in zip(pts, pts[1:]))


def _collapse_close_numbers(
    out: dict[str, tuple[float, float, str]],
    sheet: Sheet,
    routes: dict[str, list[Point]],
) -> None:
    """One number on a short jog. Keep the longest leg when marks sit together."""

    by_id = {wire.id: wire for wire in sheet.wires}
    groups: dict[str, list[str]] = {}
    for wid in list(out):
        wire = by_id.get(wid)
        if wire is None:
            continue
        mark = wire.drawn_mark(ladder=True)
        if not mark or mark in {"24V", "0V", "X1", "X2", "L1", "L2"}:
            continue
        groups.setdefault(mark, []).append(wid)
    for wids in groups.values():
        ranked = sorted(wids, key=lambda wid: _route_length(routes.get(wid) or []), reverse=True)
        kept: list[tuple[float, float]] = []
        for wid in ranked:
            x, y, _align = out[wid]
            if any(hypot(x - kx, y - ky) <= 18.0 for kx, ky in kept):
                out.pop(wid, None)
                continue
            kept.append((x, y))


def _io_body_obstacles(sheet: Sheet, catalog: Catalog) -> list[tuple[float, float, float, float]]:
    """Symbol bodies and terminal numbers on a generated I/O ladder sheet."""

    from panelcad.core.ink import text_box
    from panelcad.core.layout import is_generated_io_sheet
    from panelcad.core.pins import io_terminal_caption
    from panelcad.render.wiring import component_obstacle

    if not is_generated_io_sheet(sheet):
        return []
    boxes: list[tuple[float, float, float, float]] = []
    for comp in sheet.components:
        lid = comp.library_id.lower()
        if "waypoint" in lid or "junction" in lid:
            continue
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        boxes.append(component_obstacle(sheet, catalog, comp, pad=0.3))
        caption = io_terminal_caption(comp, device.schematic_width, device.schematic_height, comp.visual_scale())
        if caption is None:
            continue
        cx, cy, align, size, text = caption
        box = text_box(cx, cy, text, size, align)
        if box is not None:
            boxes.append(box)
    return boxes
