"""DIN-rail packing and millimetre panel geometry."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from panelcad.core.geometry import Page
from panelcad.core.models import Component, Device, Enclosure, Rail, Sheet, WireTray
from panelcad.library.panel import effective_footprint, is_layout_ready, occupies_panel_layout

DIN_CLIP_FROM_TOP = 15.0
RAIL_HEIGHT_MM = 35.0
MODULE_GAP_MM = 1.0
# Generic pack gap. The GA500 sample does not use this for the drive: Figure 7.5
# in TOEPC7106173TA p.36 is 30 mm each side and 100 mm above and below.
BACKPLATE_CLEAR_MM = 50.0
RAIL_CLEAR_MM = 20.0  # free space under a hanging module before the next rail body
WALL_MM = 8.0
GUTTER_X_MM = 40.0
GUTTER_Y_MM = 50.0
DEPTH_WIREWAY_MM = 40.0
TRAY_MM = 40.0
# Clear space from a duct face to the DIN module (top) or to the module side.
# One inch keeps Q1/K1/F1/U1 off the perimeter duct when the box is drawn to fit the sheet.
TRAY_TO_RAIL_MM = 45.0
TRAY_TO_RAIL_X_MM = 40.0
NAME_STRIP_MM = 10.0  # enclosure title sits above the top duct, not in it
# TS35 end stop. A rail trimmed to its devices keeps one of these at each end.
END_STOP_MM = 9.5
# Shop drawing ratios, largest first. 1:3 and 1:3.5 are allowed; a 36×30 box
# on landscape A3 still lands on 1:4 because 1:3.5 is taller than the frame.
STANDARD_DRAW_DENOMINATORS = (1.0, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 8.0, 10.0)
# 22 mm door operators on about 55 mm centres, with a legend under each one.
DOOR_PITCH_X_MM = 55.0
DOOR_PITCH_Y_MM = 58.0
DOOR_MARGIN_MM = 20.0
DOOR_HEADING_MM = 22.0


def is_layout_sheet(sheet: Sheet) -> bool:
    return sheet.kind in ("panel_layout", "door_layout")


def symbol_body_size(sheet: Sheet, device: Device, component: Component | None = None) -> tuple[float, float]:
    """Millimetres of the drawn body. Layout sheets use the footprint."""

    if is_layout_sheet(sheet):
        return footprint_size(device, component)
    scale = component.visual_scale() if component is not None else 1.0
    width = float(device.schematic_width) * scale
    height = float(device.schematic_height) * scale
    if component is not None and int(component.rotation or 0) in {90, 270}:
        width, height = height, width
    return (max(0.0, width), max(0.0, height))


def is_network_sheet(sheet: Sheet) -> bool:
    return sheet.kind == "network"


def is_io_sheet(sheet: Sheet) -> bool:
    return sheet.kind == "io"


def is_generated_io_sheet(sheet: Sheet) -> bool:
    """I/O sheet drawn as a ladder (one module, full-size symbols), not the 14 mm packer."""

    return sheet.kind == "io" and str(getattr(sheet, "drawing_style", "") or "") == "ladder"


def footprint_size(device: Device, component: Component | None = None) -> tuple[float, float]:
    """Catalog / instance millimetres (layout pack). Display overrides are draw-only.

    Callers that *place* or *size* a box must also check ``is_layout_ready``.
    Generic table sizes stay available so an existing project can open and
    show Problems; they must not be used to invent a new layout.
    """
    from panelcad.library.panel import is_info_needed_placeholder

    if is_info_needed_placeholder(component, device):
        return (0.0, 0.0)
    fp = effective_footprint(device, component)
    s = component.visual_scale() if component is not None else 1.0
    return (max(0.0, float(fp.width_mm) * s), max(0.0, float(fp.height_mm) * s))


def place_scale(sheet: Sheet, scale: float = 1.0) -> float:
    """Scale to store when a part is dropped on a sheet.

    Circuit Builder already stamps ``enclosure.draw_scale`` onto a door insert.
    A canvas drop or ``Session.place`` that leaves scale at 1 draws catalog
    millimetres on a fitted sheet, and cutout math then reads that body as
    ``catalog / draw_scale``. An explicit scale other than 1 is kept.
    ``visual_scale`` still clamps the on-screen symbol; this value does not.
    """

    try:
        asked = float(scale)
    except (TypeError, ValueError):
        asked = 1.0
    if asked <= 0:
        asked = 1.0
    if not is_layout_sheet(sheet) or sheet.enclosure is None:
        return asked
    draw = _draw_scale(sheet.enclosure)
    if abs(asked - 1.0) <= 0.05 and draw < 0.98:
        return draw
    return asked


def clip_from_body_top(height_mm: float) -> float:
    """True millimetres from the body top to the DIN clip.

    A body under 60 mm clips at a quarter of its height, and never past the
    15 mm DIN line. ``clip_offset`` is this distance times the draw scale.
    A 47 mm terminal is 11.75 mm, which is 3.25 mm above a rail drawn at 15 mm.
    """

    height = float(height_mm)
    if height <= 0:
        return 0.0
    if height < 60.0:
        return min(DIN_CLIP_FROM_TOP, height * 0.25)
    return DIN_CLIP_FROM_TOP


def clip_offset(device: Device, component: Component | None = None) -> float:
    _w, h = footprint_size(device, component)
    scale = component.visual_scale() if component is not None else 1.0
    if h <= 0 or scale <= 0:
        return 0.0
    return clip_from_body_top(h / scale) * scale


LEGEND_EM = 0.62


def legend_text_size(height: float) -> float:
    """One legend size for STOP, START, RUN, and a long selector.

    The size follows the operator until it would exceed about 2.6 mm on the
    sheet. A longer legend widens the plate; it does not get a smaller size.
    """

    plate_h = height * (10.0 / 22.0)
    if plate_h <= 0:
        return 1.2
    return min(2.6, max(1.15, plate_h * 0.78))


def legend_plate_rect(
    x: float,
    y: float,
    width: float,
    height: float,
    text: str = "",
) -> tuple[float, float, float, float]:
    """Legend plate under a door operator.

    A 22 mm operator gets a plate about 34 × 10 mm. HAND-OFF-AUTO uses the
    same text height as STOP and widens the plate to suit.
    """

    size = legend_text_size(height)
    # Two margins, so HAND-OFF-AUTO does not paint the plate edge to edge.
    text_w = (len(text) * size * LEGEND_EM + max(4.8, size * 2.2)) if text else 0.0
    plate_w = max(width * (34.0 / 22.0), text_w)
    plate_h = height * (10.0 / 22.0)
    gap = height * (1.4 / 22.0)
    return (x + width / 2.0 - plate_w / 2.0, y + height + gap, plate_w, plate_h)


def door_row_pitch(sizes: list[tuple[float, float, str]]) -> float:
    """Column pitch for one door row, wide enough for the longest legend plate.

    Legend type is capped in drawn millimetres, so a long word is measured at
    sheet scale as well as at 1:1 and the true pitch covers both.
    """

    if not sizes:
        return DOOR_PITCH_X_MM
    widest = max(width for width, _height, _text in sizes)
    pitch = DOOR_PITCH_X_MM if widest <= 30.0 else max(DOOR_PITCH_X_MM, widest + 28.0)
    for width, height, text in sizes:
        for probe in (1.0, 0.22):
            plate_w = legend_plate_rect(0.0, 0.0, width * probe, height * probe, text)[2]
            pitch = max(pitch, (plate_w + 5.0) / probe)
    return pitch


def heading_rect(x: float, y: float, width: float, height: float, text: str) -> tuple[float, float, float, float]:
    """Motor heading above the first operator, clear of the operator body.

    A full-size 22 mm operator gets a heading about 6.5 mm tall. On a scaled
    sheet the box stays at least 2.2 mm, and the gap under it stays large
    enough that the heading does not sit on the pushbutton.
    """

    size = max(2.2, height * (6.5 / 22.0))
    tw = max(width * 1.6, len(text or "") * size * 0.62)
    th = size * 1.15
    gap = max(1.6, height * (4.0 / 22.0))
    return (x + width / 2.0 - tw / 2.0, y - th - gap, tw, th)


def snap_mm(value: float, step: float = 1.0) -> float:
    if step <= 0:
        return value
    return round(value / step) * step


def nearest_rail(sheet: Sheet, y: float) -> Rail | None:
    if not sheet.rails:
        return None
    return min(sheet.rails, key=lambda r: abs(r.y - y))


def place_on_rail(device: Device, rail: Rail, x: float, component: Component | None = None) -> tuple[float, float]:
    w, _h = footprint_size(device, component)
    x = snap_mm(x)
    if w > 0:
        x = max(rail.x, min(x, rail.x + rail.length_mm - w))
    else:
        x = max(rail.x, min(x, rail.x + rail.length_mm))
    y = rail.y - clip_offset(device, component)
    return (x, y)


# Edges that merely touch, or that overlap by a rounding hair, are not a clash.
# A VFD end stop can land 1e-14 mm onto the breaker beside it.
OVERLAP_TOL_MM = 0.05


def rects_overlap(
    ax: float,
    ay: float,
    aw: float,
    ah: float,
    bx: float,
    by: float,
    bw: float,
    bh: float,
    tol: float = OVERLAP_TOL_MM,
) -> bool:
    return ax + tol < bx + bw and ax + aw > bx + tol and ay + tol < by + bh and ay + ah > by + tol


def rail_height(rail: Rail) -> float:
    h = float(getattr(rail, "height_mm", 0) or 0)
    return h if h > 0 else RAIL_HEIGHT_MM


def tray_aabb(tray: WireTray) -> tuple[float, float, float, float]:
    if tray.orientation == "v":
        return (tray.x, tray.y, tray.width_mm, tray.length_mm)
    return (tray.x, tray.y, tray.length_mm, tray.width_mm)


def rail_aabb(rail: Rail) -> tuple[float, float, float, float]:
    h = rail_height(rail)
    return (rail.x, rail.y - h / 2.0, rail.length_mm, h)


def inner_backplate(
    enc: Enclosure,
    trays: list[WireTray],
    pad: float = TRAY_TO_RAIL_X_MM,
) -> tuple[float, float, float, float]:
    """Rectangle that does not sit on perimeter wire duct."""
    left = float(enc.x)
    top = float(enc.y)
    right = float(enc.x) + float(enc.width_mm)
    bottom = float(enc.y) + float(enc.height_mm)
    mid_x = enc.x + enc.width_mm / 2.0
    mid_y = enc.y + enc.height_mm / 2.0
    for tray in trays:
        x, y, w, h = tray_aabb(tray)
        if tray.orientation == "v":
            if x + w / 2.0 <= mid_x:
                left = max(left, x + w + pad)
            else:
                right = min(right, x - pad)
        else:
            if y + h / 2.0 <= mid_y:
                top = max(top, y + h + pad)
            else:
                bottom = min(bottom, y - pad)
    if right < left + 16.0:
        right = left + 16.0
    if bottom < top + 16.0:
        bottom = top + 16.0
    return (left, top, right, bottom)


def panel_furniture(enc: Enclosure) -> tuple[list[Rail], list[WireTray]]:
    """Perimeter wire duct plus the first TS35 rail, true millimetres from (0,0)."""
    w, h = float(enc.width_mm), float(enc.height_mm)
    wall = WALL_MM
    tw = TRAY_MM
    gap_h = TRAY_TO_RAIL_X_MM
    top_y = wall + NAME_STRIP_MM
    bot_y = max(top_y + tw, h - wall - tw)
    inner_len = max(80.0, w - 2 * wall)
    side_top = top_y + tw
    side_len = max(40.0, bot_y - side_top)
    trays = [
        WireTray(id="wt-top", x=wall, y=top_y, length_mm=inner_len, width_mm=tw, orientation="h"),
        WireTray(id="wt-bot", x=wall, y=bot_y, length_mm=inner_len, width_mm=tw, orientation="h"),
        WireTray(id="wt-left", x=wall, y=side_top, length_mm=side_len, width_mm=tw, orientation="v"),
        WireTray(id="wt-right", x=w - wall - tw, y=side_top, length_mm=side_len, width_mm=tw, orientation="v"),
    ]
    rail_x = wall + tw + gap_h
    rail_len = max(80.0, w - 2 * (wall + tw + gap_h))
    rail_y = top_y + tw + TRAY_TO_RAIL_MM + RAIL_HEIGHT_MM / 2.0
    rails = [Rail(id="r1", x=rail_x, y=rail_y, length_mm=rail_len, height_mm=RAIL_HEIGHT_MM)]
    return rails, trays


def enclosure_is_sized(enc: Enclosure | None, page: Page | None = None) -> bool:
    """True when a person or a catalog box set the enclosure.

    The automatic page outline (``default_enclosure_and_rails``) is not a size.
    ``set_enclosure`` with width and height, ``apply_enclosure``, and Size
    Enclosure set ``sized``. A catalog, maker, or back-panel size counts too.
    """

    if enc is None:
        return False
    if bool(getattr(enc, "sized", False)):
        return True
    if str(enc.catalog or "").strip() or str(enc.manufacturer or "").strip():
        return True
    if float(enc.panel_width_mm or 0.0) > 1.0 and float(enc.panel_height_mm or 0.0) > 1.0:
        return True
    # An old save invented this name from a generic cabinet snap. No catalog
    # and no maker means that box was never sized by a person.
    if is_invented_cabinet_name(enc.name):
        return False
    if page is None:
        return False
    placeholder, _rails = default_enclosure_and_rails(page)
    if abs(float(enc.width_mm) - float(placeholder.width_mm)) > 0.5:
        return True
    if abs(float(enc.height_mm) - float(placeholder.height_mm)) > 0.5:
        return True
    return False


def default_enclosure_and_rails(page: Page) -> tuple[Enclosure, list[Rail]]:
    """Furniture that fits an A3 sheet, leaving the title-block strip clear.

    This outline is not a sized enclosure. Layout and thermal treat it as unset.
    """
    pw, ph = page.dimensions()
    x, y = 18.0, 18.0
    w = max(80.0, pw - 36.0)
    h = max(80.0, ph - 72.0)
    enc = Enclosure(x=x, y=y, width_mm=w, height_mm=h, name="Enclosure", sized=False)
    inset = 20.0
    rail_len = max(60.0, w - 2 * inset)
    rx = x + inset
    rails: list[Rail] = []
    rails.append(Rail(id="r1", x=rx, y=y + 50.0, length_mm=rail_len))
    return enc, rails


def _din_parts(item: tuple) -> tuple[str, Device, Component | None]:
    ref = item[0]
    device = item[1]
    component = item[2] if len(item) > 2 else None
    return ref, device, component


def pack_din(
    items: list[tuple],
    rails: list[Rail],
    gap: float = MODULE_GAP_MM,
    trays: list[WireTray] | None = None,
) -> tuple[list[tuple[str, float, float]], list[Rail]]:
    """Left-to-right. The next rail is placed below the hanging height of the last one.

    Each item is `(ref, device)` or `(ref, device, component)`.
    """
    if not rails:
        rails = [Rail(id="r1", x=40.0, y=80.0, length_mm=400.0, height_mm=RAIL_HEIGHT_MM)]
    first = rails[0]
    rh = rail_height(first)
    live: list[Rail] = [
        Rail(id="r1", x=first.x, y=first.y, length_mm=first.length_mm, height_mm=rh, type=first.type)
    ]
    out: list[tuple[str, float, float]] = []
    cursor_rail = 0
    cursor_x = live[0].x
    bottoms: list[float] = [first.y]
    tray_boxes = [tray_aabb(tray) for tray in (trays or [])]

    def _clear_of_ducts(x: float, y: float, w: float, h: float) -> tuple[float, float]:
        """Slide a module right until its footprint misses every duct."""

        guard = 0
        while guard < 30 and any(rects_overlap(x, y, w, h, *box) for box in tray_boxes):
            guard += 1
            x += max(gap, 1.0)
        return x, y

    def _new_rail() -> Rail:
        prev = live[-1]
        bottom = bottoms[len(live) - 1]
        # Next rail body sits fully below hanging modules, with wiring space.
        y = max(prev.y + rh + RAIL_CLEAR_MM, bottom + RAIL_CLEAR_MM + rh / 2.0)
        rail = Rail(
            id=f"r{len(live) + 1}",
            x=prev.x,
            y=y,
            length_mm=prev.length_mm,
            height_mm=rh,
            type=prev.type,
        )
        live.append(rail)
        bottoms.append(rail.y)
        return rail

    for item in items:
        ref, device, component = _din_parts(item)
        if occupies_panel_layout(device, component) and not is_layout_ready(device, component):
            continue
        w, h = footprint_size(device, component)
        if w <= 0 or h <= 0:
            continue
        rail = live[cursor_rail]
        if w > rail.length_mm or (cursor_x > rail.x and w > rail.x + rail.length_mm - cursor_x + 1e-6):
            cursor_rail += 1
            if cursor_rail >= len(live):
                rail = _new_rail()
            else:
                rail = live[cursor_rail]
            cursor_x = rail.x
        if w > rail.length_mm:
            x, y = place_on_rail(device, rail, rail.x, component=component)
            x, y = _clear_of_ducts(x, y, w, h)
            if x + w > rail.x + rail.length_mm + 1e-6:
                x = max(rail.x, min(x, rail.x + rail.length_mm - w))
            # A module that still hangs into a duct, including the bottom duct, is not a row.
            if any(rects_overlap(x, y, w, h, *box) for box in tray_boxes):
                continue
            out.append((ref, x, y))
            bottoms[cursor_rail] = max(bottoms[cursor_rail], y + h)
            cursor_rail += 1
            cursor_x = _new_rail().x if cursor_rail >= len(live) else live[cursor_rail].x
            continue
        x, y = place_on_rail(device, rail, cursor_x, component=component)
        x, y = _clear_of_ducts(x, y, w, h)
        if x + w > rail.x + rail.length_mm + 1e-6:
            # Sliding clear of a duct must not leave the module hanging off the rail.
            cursor_rail += 1
            if cursor_rail >= len(live):
                rail = _new_rail()
            else:
                rail = live[cursor_rail]
            cursor_x = rail.x
            x, y = place_on_rail(device, rail, cursor_x, component=component)
            x, y = _clear_of_ducts(x, y, w, h)
            if x + w > rail.x + rail.length_mm + 1e-6:
                x = max(rail.x, min(x, rail.x + rail.length_mm - w))
        if any(rects_overlap(x, y, w, h, *box) for box in tray_boxes):
            continue
        out.append((ref, x, y))
        bottoms[cursor_rail] = max(bottoms[cursor_rail], y + h)
        cursor_x = x + w + gap
    return out, live


def _hits(
    x: float,
    y: float,
    w: float,
    h: float,
    rects: list[tuple[float, float, float, float]],
    pad: float = 2.0,
) -> bool:
    for rx, ry, rw, rh in rects:
        if rects_overlap(x, y, w, h, rx - pad, ry - pad, rw + 2 * pad, rh + 2 * pad):
            return True
    return False


def pack_field(
    items: list[tuple],
    enclosure: Enclosure,
    blocked: list[tuple[float, float, float, float]] | None = None,
    start_y: float | None = None,
    gap: float = 8.0,
    bounds: tuple[float, float, float, float] | None = None,
) -> list[tuple[str, float, float, float]]:
    """Place non-DIN hardware with no overlap. Returns (ref, x, y, scale)."""
    if bounds is not None:
        left, top, right, bottom = bounds
    else:
        left = enclosure.x + 8.0
        top = enclosure.y + 14.0
        right = enclosure.x + enclosure.width_mm - 8.0
        bottom = enclosure.y + enclosure.height_mm - 6.0
    occupied = list(blocked or [])
    din_bottom = top
    for _x, ry, _w, rh in occupied:
        edge = ry + rh
        # A duct that runs to the field boundary is the wall, not the DIN stack.
        if edge >= bottom - 1.0:
            continue
        din_bottom = max(din_bottom, edge)
    # Never start field hardware above the hanging DIN stack.
    origin_y = start_y if start_y is not None else din_bottom + gap
    if origin_y + 16.0 > bottom:
        origin_y = max(top, din_bottom + 4.0)

    def _field_size(item: tuple) -> tuple[float, float]:
        _ref, device, component = _din_parts(item)
        return footprint_size(device, component)

    ranked = sorted(
        items,
        key=lambda it: -(_field_size(it)[0] * _field_size(it)[1]),
    )
    out: list[tuple[str, float, float, float]] = []
    for item in ranked:
        ref, device, component = _din_parts(item)
        if occupies_panel_layout(device, component) and not is_layout_ready(device, component):
            continue
        bw, bh = footprint_size(device, component)
        if bw <= 0 or bh <= 0:
            continue
        found: tuple[float, float, float] | None = None
        for scale in (1.0, 0.85, 0.7, 0.55, 0.4, 0.3):
            w, h = bw * scale, bh * scale
            if w > right - left + 0.5 or h > bottom - top + 0.5:
                continue
            y = origin_y
            while y + h <= bottom + 0.5:
                x = left
                while x + w <= right + 0.5:
                    if not _hits(x, y, w, h, occupied, pad=gap / 2.0):
                        found = (x, y, scale)
                        break
                    x += 5.0
                if found:
                    break
                y += 5.0
            if found:
                break
        if found is None:
            avail_w = max(16.0, right - left)
            avail_h = max(16.0, bottom - origin_y)
            scale = max(0.2, min(1.0, avail_w / bw, avail_h / bh))
            w, h = bw * scale, bh * scale
            y = origin_y
            if occupied:
                y = max(y, max(r[1] + r[3] for r in occupied) + gap)
            y = min(y, bottom - h)
            w, h = bw * scale, bh * scale
            # Do not force a part onto a duct, a rail, or another footprint.
            if y < top - 0.5 or _hits(left, y, w, h, occupied, pad=0.0):
                continue
            found = (left, y, scale)
        x, y, scale = found
        w, h = bw * scale, bh * scale
        out.append((ref, snap_mm(x), snap_mm(y), round(float(scale), 3)))
        occupied.append((x, y, w, h))
    return out


def settle_catalog_footprints(session: Any, sheet_id: str) -> list[str]:
    """Move catalog footprints so nothing shares the backplate.

    Called after preferred-part assignment, which writes real W×H onto devices
    that were packed from the generic symbol. Drive and other backplate
    devices go in their own zone below the DIN stack. Plug-on overloads stay
    on the rail. DIN modules that grew into a neighbor slide along the rail.
    """

    warnings: list[str] = []
    sheet = session.sheet(sheet_id)
    enc = sheet.enclosure
    if enc is None:
        return warnings
    rows: list[tuple[Any, Device, float, float, bool]] = []
    for comp in sheet.components:
        try:
            device = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        fp = effective_footprint(device, comp)
        width, height = footprint_size(device, comp)
        if width <= 0 or height <= 0:
            continue
        backplate = device.category.startswith("drive.") or (
            fp.mount == "backplate" and not fp.din_rail
        )
        rows.append((comp, device, width, height, backplate))
    din = [row for row in rows if not row[4]]
    din.sort(key=lambda row: (round(row[0].y, 1), row[0].x, row[0].id))
    for index, (comp, _device, width, height, _back) in enumerate(din):
        for _ in range(40):
            blocker = None
            for other, _odev, owidth, oheight, _oback in din[:index]:
                if rects_overlap(comp.x, comp.y, width, height, other.x, other.y, owidth, oheight):
                    blocker = other.x + owidth + MODULE_GAP_MM
                    break
            if blocker is None:
                break
            comp.x = blocker
    field = [row for row in rows if row[4]]
    if not field:
        return warnings
    margin = 8.0
    left = enc.x + margin
    right = enc.x + enc.width_mm - margin
    din_bottom = enc.y
    for comp, _device, _width, height, back in rows:
        if not back:
            din_bottom = max(din_bottom, comp.y + height)
    cursor_x = left
    cursor_y = din_bottom + BACKPLATE_CLEAR_MM
    row_h = 0.0
    field.sort(key=lambda row: (-(row[2] * row[3]), row[0].id))
    for comp, _device, width, height, _back in field:
        if cursor_x > left and cursor_x + width > right + 0.5:
            cursor_x = left
            cursor_y += row_h + BACKPLATE_CLEAR_MM
            row_h = 0.0
        comp.x = cursor_x
        comp.y = cursor_y
        comp.properties["mount"] = "backplate"
        comp.properties["din_rail"] = False
        for other in session.project.sheets:
            if other.id == sheet.id:
                continue
            try:
                twin = other.component(comp.id)
            except KeyError:
                continue
            twin.properties["mount"] = "backplate"
            twin.properties["din_rail"] = False
        cursor_x += width + BACKPLATE_CLEAR_MM
        row_h = max(row_h, height)
        floor = enc.y + enc.height_mm - margin
        if comp.y + height > floor + 0.5:
            enc.height_mm += (comp.y + height + margin) - (enc.y + enc.height_mm)
            warnings.append(f"{comp.id} backplate zone extended the enclosure")
    return warnings


def separate_vertically(
    boxes: list[tuple[str, float, float, float, float]],
    gap: float = 6.0,
) -> list[tuple[str, float, float, float, float]]:
    """Push later parts down until no two rectangles overlap."""
    ordered = sorted(boxes, key=lambda b: (b[2], b[1], b[0]))
    placed: list[tuple[str, float, float, float, float]] = []
    for ref, x, y, w, h in ordered:
        guard = 0
        while guard < 80:
            guard += 1
            hit = False
            for _oid, ox, oy, ow, oh in placed:
                if rects_overlap(x, y, w, h, ox, oy, ow, oh):
                    y = oy + oh + gap
                    hit = True
                    break
            if not hit:
                break
        placed.append((ref, snap_mm(x), snap_mm(y), w, h))
    return placed


CAB_W = (300.0, 400.0, 500.0, 600.0, 800.0, 1000.0, 1200.0)
CAB_H = (300.0, 400.0, 500.0, 600.0, 800.0, 1000.0, 1200.0, 1400.0, 1600.0, 1800.0, 2000.0)
CAB_D = (150.0, 200.0, 250.0, 300.0, 400.0, 500.0, 600.0)


def _snap_up(value: float, series: tuple[float, ...]) -> float:
    for step in series:
        if step + 0.5 >= value:
            return float(step)
    last = series[-1]
    gap = series[-1] - series[-2] if len(series) > 1 else 50.0
    n = last
    while n < value:
        n += gap
    return float(n)


def enclosure_drawn_size(enc: Enclosure) -> tuple[float, float, float]:
    """Drawn W×H and the scale used (true millimetres stay on the enclosure)."""
    s = float(enc.draw_scale) if enc.draw_scale and enc.draw_scale > 0 else 1.0
    return (enc.width_mm * s, enc.height_mm * s, s)


def fit_enclosure_scale(width_mm: float, height_mm: float, avail_w: float, avail_h: float) -> float:
    if width_mm <= 0 or height_mm <= 0:
        return 1.0
    return min(1.0, avail_w / width_mm, avail_h / height_mm)


def fit_standard_enclosure_scale(width_mm: float, height_mm: float, avail_w: float, avail_h: float) -> float:
    """Largest allowed shop ratio that fits the drawing frame.

    Allowed denominators are ``STANDARD_DRAW_DENOMINATORS`` (1:1, 1:2, 1:2.5,
    1:3, 1:3.5, 1:4, 1:5, 1:8, 1:10). A box that fits none of them uses the
    continuous fit so the outline still stays inside the frame.
    """

    if width_mm <= 0 or height_mm <= 0:
        return 1.0
    for denom in STANDARD_DRAW_DENOMINATORS:
        ratio = 1.0 / float(denom)
        if width_mm * ratio <= avail_w + 1e-6 and height_mm * ratio <= avail_h + 1e-6:
            return ratio
    return fit_enclosure_scale(width_mm, height_mm, avail_w, avail_h)


def format_draw_ratio(scale: float) -> str:
    """Caption for a draw scale. Standard ratios print as 1:4 or 1:3.5."""

    if scale <= 1e-9:
        return "1:1"
    for denom in STANDARD_DRAW_DENOMINATORS:
        if abs(float(scale) * float(denom) - 1.0) <= 1e-3:
            return f"1:{float(denom):g}"
    return f"1:{1.0 / float(scale):.1f}"


def center_in_frame(
    frame: tuple[float, float, float, float],
    drawn_w: float,
    drawn_h: float,
) -> tuple[float, float]:
    """Top-left of a box centered in the drawing frame above the title block."""

    x, y, width, height = frame
    return (
        x + max(0.0, (width - drawn_w) / 2.0),
        y + max(0.0, (height - drawn_h) / 2.0),
    )


def recommend_cabinet_size(
    din: list[tuple[Device, Component | None]],
    field: list[tuple[Device, Component | None]],
) -> dict[str, float | int | str]:
    """Cabinet W×H×D from in-cabinet BOM footprints (DIN pack + backplate + gutters)."""
    from panelcad.library.panel import effective_footprint

    max_part_w = 80.0
    max_depth = 60.0
    din = [(d, c) for d, c in din if is_layout_ready(d, c) or not occupies_panel_layout(d, c)]
    field = [(d, c) for d, c in field if is_layout_ready(d, c) or not occupies_panel_layout(d, c)]
    for d, c in (*din, *field):
        w, h = footprint_size(d, c)
        max_part_w = max(max_part_w, w)
        max_depth = max(max_depth, float(effective_footprint(d, c).depth_mm or 0))

    rail_len = max(400.0, max_part_w + 20.0)
    cursor = 0.0
    row_h = 0.0
    din_h = 0.0
    din_w = 0.0
    rails = 1
    for d, c in din:
        w, h = footprint_size(d, c)
        if cursor > 0 and cursor + w > rail_len + 1e-6:
            din_h += row_h + RAIL_CLEAR_MM
            din_w = max(din_w, cursor)
            cursor = 0.0
            row_h = 0.0
            rails += 1
        cursor += w + MODULE_GAP_MM
        row_h = max(row_h, h)
    if cursor:
        din_h += row_h
        din_w = max(din_w, cursor)
    if not din:
        din_h, din_w, rails = 0.0, rail_len, 0

    inner_w = max(rail_len, din_w)
    field_h = 0.0
    field_w = 0.0
    for d, c in field:
        w, h = footprint_size(d, c)
        field_w = max(field_w, w)
        field_h += h + 8.0
    packed_w = max(inner_w, field_w)
    packed_h = max(din_h + field_h, 80.0)
    side = WALL_MM + TRAY_MM + TRAY_TO_RAIL_X_MM
    rec_w = _snap_up(packed_w + 2 * side, CAB_W)
    rec_h = _snap_up(
        packed_h + 2 * (WALL_MM + TRAY_MM) + TRAY_TO_RAIL_MM + RAIL_CLEAR_MM + NAME_STRIP_MM,
        CAB_H,
    )
    rec_d = _snap_up(max_depth + DEPTH_WIREWAY_MM, CAB_D)
    return {
        "width_mm": rec_w,
        "height_mm": rec_h,
        "depth_mm": rec_d,
        "packed_w": round(packed_w, 1),
        "packed_h": round(packed_h, 1),
        "packed_d": round(max_depth, 1),
        "din_parts": len(din),
        "field_parts": len(field),
        "din_rails": rails,
        "rail_mm": round(rail_len, 1),
        "gutter_x": side,
        "gutter_y": WALL_MM + TRAY_MM + TRAY_TO_RAIL_MM,
    }


def _draw_scale(enc: Enclosure) -> float:
    scale = float(enc.draw_scale or 0.0)
    return scale if scale > 1e-6 else 1.0


def part_scale_ratio(component_scale: float, old_draw: float) -> float:
    """How component.scale relates to the enclosure draw scale.

    A part placed with the enclosure uses draw_scale, or draw_scale times a
    shrink. A part left at scale 1 on a scaled sheet is drawn at catalog
    millimetres and looks oversized; that snaps back to true size.
    """

    old = old_draw if old_draw > 1e-6 else 1.0
    scale = component_scale if component_scale > 1e-6 else 1.0
    if scale <= old * 1.05:
        return scale / old
    if abs(scale - 1.0) < 0.05 and old < 0.98:
        return 1.0
    return scale / old


def nudge_components_apart(
    components: list[Component],
    device_of: Callable[[Component], Device | None],
    bounds: tuple[float, float, float, float] | None = None,
    gap: float = MODULE_GAP_MM,
) -> list[str]:
    """Slide later footprints off earlier ones.

    A pack that lands an end stop on a fuse is moved to the right, then down
    a row, until the bodies no longer meet. Returns refs that still overlap
    because the enclosure has no free spot.
    """

    boxes: list[list[Any]] = []
    for comp in components:
        device = device_of(comp)
        if device is None:
            continue
        width, height = footprint_size(device, comp)
        if width <= 0 or height <= 0:
            continue
        boxes.append([comp, width, height])
    boxes.sort(key=lambda item: (float(item[0].y), float(item[0].x), str(item[0].id)))
    left = top = right = bottom = None
    if bounds is not None:
        left, top, right, bottom = bounds
    unresolved: list[str] = []
    for index, item in enumerate(boxes):
        comp, width, height = item
        device = device_of(comp)
        on_rail = device is not None and is_rail_module(device, comp)
        origin_x, origin_y = float(comp.x), float(comp.y)
        seated = False
        for _ in range(48):
            blocker = None
            for earlier in boxes[:index]:
                other, other_w, other_h = earlier
                if rects_overlap(comp.x, comp.y, width, height, other.x, other.y, other_w, other_h, tol=0.2):
                    blocker = (other, other_w)
                    break
            if blocker is None:
                seated = True
                break
            other, other_w = blocker
            comp.x = other.x + other_w + gap
            past_right = (
                bounds is not None
                and right is not None
                and left is not None
                and comp.x + width > right + 0.2
            )
            if not past_right:
                continue
            # A DIN module stays on its rail. Wrapping it down leaves the clip.
            if on_rail:
                break
            comp.x = left
            comp.y = comp.y + height + gap
            if bottom is not None and comp.y + height > bottom + 0.2:
                break
        if not seated:
            comp.x, comp.y = origin_x, origin_y
            unresolved.append(str(comp.id))
    return unresolved


_INVENTED_CABINET_NAME = re.compile(
    r"^Enclosure \d+ × \d+ × \d+ mm, \d+ BOM items in cabinet$"
)


def is_invented_cabinet_name(name: str | None) -> bool:
    """True for the old auto-size string ``Enclosure 600 × 300 × 150 mm, N BOM items in cabinet``."""

    return _INVENTED_CABINET_NAME.match(str(name or "").strip()) is not None


def format_mm(value: float) -> str:
    """Whole millimetres when the value is on a millimetre, otherwise one decimal."""

    number = float(value)
    rounded = round(number)
    if abs(number - rounded) < 0.05:
        return str(int(rounded))
    return f"{number:.1f}".rstrip("0").rstrip(".")


def footprint_overlaps_tray(
    x: float,
    y: float,
    width: float,
    height: float,
    trays: list[WireTray],
) -> bool:
    """True when a part body covers a wire duct."""

    return any(rects_overlap(x, y, width, height, *tray_aabb(tray)) for tray in trays)


def sits_fully_on_a_rail(component: Component, device: Device | None, rails: list[Rail]) -> bool:
    """True when a DIN module's body lies on one kept rail.

    Parts that are not DIN modules are left to the other checks.
    """

    if device is None or not is_rail_module(device, component):
        return True
    width, height = footprint_size(device, component)
    if width <= 0 or height <= 0:
        return True
    seat = float(component.y) + clip_offset(device, component)
    for rail in rails:
        if abs(seat - float(rail.y)) > 0.8:
            continue
        if (
            float(component.x) >= float(rail.x) - 0.5
            and float(component.x) + width <= float(rail.x) + float(rail.length_mm) + 0.5
        ):
            return True
    return False


def body_inside(
    x: float,
    y: float,
    width: float,
    height: float,
    bounds: tuple[float, float, float, float],
    tol: float = 0.5,
) -> bool:
    """True when the rectangle sits inside ``bounds`` (left, top, right, bottom)."""

    left, top, right, bottom = bounds
    return (
        x >= left - tol
        and y >= top - tol
        and x + width <= right + tol
        and y + height <= bottom + tol
    )


def backplate_sheet_bounds(enc: Enclosure) -> tuple[float, float, float, float]:
    """Sheet rectangle of the subpanel. The outline is the backplate when no panel is set."""

    scale = _draw_scale(enc)
    drawn_w = float(enc.width_mm) * scale
    drawn_h = float(enc.height_mm) * scale
    panel_w = float(enc.panel_width_mm or 0.0)
    panel_h = float(enc.panel_height_mm or 0.0)
    if panel_w > 1.0 and panel_h > 1.0:
        plate_w = panel_w * scale
        plate_h = panel_h * scale
    else:
        plate_w, plate_h = drawn_w, drawn_h
    left = float(enc.x) + (drawn_w - plate_w) / 2.0
    top = float(enc.y) + (drawn_h - plate_h) / 2.0
    return (left, top, left + plate_w, top + plate_h)


def _clamp_on_plate(
    x: float,
    y: float,
    width: float,
    height: float,
    bounds: tuple[float, float, float, float],
) -> tuple[float, float]:
    left, top, right, bottom = bounds
    span_w = right - left
    span_h = bottom - top
    if width <= span_w:
        x = min(max(x, left), right - width)
    else:
        x = left
    if height <= span_h:
        y = min(max(y, top), bottom - height)
    else:
        y = top
    return (x, y)


def _hits_keepout(
    x: float,
    y: float,
    width: float,
    height: float,
    blocked: list[tuple[float, float, float, float]],
) -> bool:
    inset = 0.15
    if width <= inset * 2.0 or height <= inset * 2.0:
        ax, ay, aw, ah = x, y, width, height
    else:
        ax = x + inset
        ay = y + inset
        aw = width - 2.0 * inset
        ah = height - 2.0 * inset
    for bx, by, bw, bh in blocked:
        if rects_overlap(ax, ay, aw, ah, bx, by, bw, bh):
            return True
    return False


def _slot_on_plate(
    width: float,
    height: float,
    bounds: tuple[float, float, float, float],
    blocked: list[tuple[float, float, float, float]],
    prefer: tuple[float, float] = (0.0, 0.0),
) -> tuple[float, float] | None:
    """Nearest clear spot to prefer, so a part that only overhangs stays nearby."""

    left, top, right, bottom = bounds
    if width > (right - left) + 0.8 or height > (bottom - top) + 0.8:
        return None
    step = 6.0
    best: tuple[float, float, float] | None = None
    prefer_x, prefer_y = prefer
    y = top
    while y + height <= bottom + 0.5:
        x = left
        while x + width <= right + 0.5:
            if not _hits_keepout(x, y, width, height, blocked):
                dist = (x - prefer_x) ** 2 + (y - prefer_y) ** 2
                if best is None or dist < best[0]:
                    best = (dist, x, y)
                    if dist <= step * step:
                        return (x, y)
            x += step
        y += step
    if best is None:
        return None
    return (best[1], best[2])


def rescale_parts_onto_enclosure(
    components: list[Component],
    *,
    old: Enclosure | None,
    new: Enclosure,
    trays: list[WireTray],
    size_of: Callable[[Component], tuple[float, float]],
    blockers: list[tuple[float, float, float, float]] | None = None,
) -> list[str]:
    """Keep catalog millimetres after a box resize, and stay on the backplate.

    Positions are converted with the old draw scale, then drawn at the new one.
    A part that would cross the subpanel edge or a duct is moved into clear space.
    Returns short warnings. Does not shrink a footprint below its true size.
    """

    if not components:
        return []
    new_scale = _draw_scale(new)
    old_scale = _draw_scale(old) if old is not None else new_scale
    old_x = float(old.x) if old is not None else float(new.x)
    old_y = float(old.y) if old is not None else float(new.y)
    for comp in components:
        ratio = 1.0 if old is None else part_scale_ratio(float(comp.scale or 1.0), old_scale)
        true_x = 0.0 if old is None else (float(comp.x) - old_x) / old_scale
        true_y = 0.0 if old is None else (float(comp.y) - old_y) / old_scale
        comp.scale = new_scale * ratio
        comp.x = float(new.x) + true_x * new_scale
        comp.y = float(new.y) + true_y * new_scale

    bounds = backplate_sheet_bounds(new)
    blocked = [tray_aabb(tray) for tray in trays]
    blocked.extend(blockers or [])
    moved: list[str] = []
    stuck: list[str] = []
    loose: list[tuple[Component, float, float, float, float]] = []
    for comp in components:
        width, height = size_of(comp)
        width = max(0.0, float(width))
        height = max(0.0, float(height))
        if width <= 0.0 or height <= 0.0:
            continue
        inside = (
            comp.x >= bounds[0] - 0.4
            and comp.y >= bounds[1] - 0.4
            and comp.x + width <= bounds[2] + 0.4
            and comp.y + height <= bounds[3] + 0.4
        )
        if inside and not _hits_keepout(comp.x, comp.y, width, height, blocked):
            blocked.append((comp.x, comp.y, width, height))
        else:
            loose.append((comp, comp.x, comp.y, width, height))
    loose.sort(key=lambda item: -(item[3] * item[4]))
    for comp, origin_x, origin_y, width, height in loose:
        x, y = _clamp_on_plate(comp.x, comp.y, width, height, bounds)
        if not _hits_keepout(x, y, width, height, blocked) and _slot_fits(x, y, width, height, bounds):
            comp.x, comp.y = x, y
        else:
            spot = _slot_on_plate(width, height, bounds, blocked, prefer=(x, y))
            if spot is None:
                comp.x, comp.y = x, y
                stuck.append(comp.id)
            else:
                comp.x, comp.y = spot
        if abs(comp.x - origin_x) > 0.6 or abs(comp.y - origin_y) > 0.6:
            moved.append(comp.id)
        blocked.append((comp.x, comp.y, width, height))
    notes: list[str] = []
    if moved:
        notes.append("Moved onto the backplate, clear of wire duct: " + ", ".join(moved))
    if stuck:
        notes.append("True-size footprint does not fit inside the backplate: " + ", ".join(stuck))
    return notes


def _slot_fits(
    x: float,
    y: float,
    width: float,
    height: float,
    bounds: tuple[float, float, float, float],
) -> bool:
    left, top, right, bottom = bounds
    if width > (right - left) + 0.8 or height > (bottom - top) + 0.8:
        return False
    return x >= left - 0.4 and y >= top - 0.4 and x + width <= right + 0.4 and y + height <= bottom + 0.4


def is_rail_module(device: Device, component: Component) -> bool:
    """DIN or terminal module that clips to TS35. Plug-on overloads do not."""

    from panelcad.core.enclosure import classify_role, is_plug_on_overload

    if is_plug_on_overload(device, component):
        return False
    return classify_role(device, component) in {"din", "terminal"}


def _side_duct_faces(
    trays: list[WireTray],
    enclosure: Enclosure | None,
) -> tuple[float, float] | None:
    """Inner faces of the left and right ducts, in sheet millimetres."""

    if enclosure is None:
        return None
    scale = _draw_scale(enclosure)
    mid = float(enclosure.x) + float(enclosure.width_mm) * scale / 2.0
    left_edge: float | None = None
    right_edge: float | None = None
    for tray in trays:
        if tray.orientation != "v":
            continue
        x, _y, width, _height = tray_aabb(tray)
        if x + width / 2.0 <= mid:
            edge = x + width
            left_edge = edge if left_edge is None else max(left_edge, edge)
        else:
            right_edge = x if right_edge is None else min(right_edge, x)
    if left_edge is None or right_edge is None or right_edge <= left_edge:
        return None
    return (left_edge, right_edge)


def _rail_gap_beside_ducts(
    left_edge: float,
    right_edge: float,
    row_width: float,
    scale: float,
) -> float:
    """Clearance from a side duct to the rail.

    TRAY_TO_RAIL_X_MM (40 mm) is the shop clearance from the duct face to the
    module. A row that already fills the opening keeps the 0.8 mm stroke gap
    instead of being pushed through the opposite duct.
    """

    full = max(0.8, TRAY_TO_RAIL_X_MM * scale)
    spare = (right_edge - left_edge) - max(0.0, row_width)
    return min(full, max(0.8, spare / 2.0))


def _opening_between_side_ducts(
    trays: list[WireTray],
    enclosure: Enclosure | None,
) -> tuple[float, float] | None:
    """Sheet x and length of the gap between the side ducts, clear of both."""

    faces = _side_duct_faces(trays, enclosure)
    if faces is None or enclosure is None:
        return None
    left_edge, right_edge = faces
    scale = _draw_scale(enclosure)
    # Full clearance. finish_rails shrinks this per rail when the devices
    # already occupy the opening.
    gap = max(0.8, TRAY_TO_RAIL_X_MM * scale)
    start = left_edge + gap
    end = right_edge - gap
    if end - start < max(24.0, 40.0 * scale):
        return None
    return (start, end - start)


def rails_holding_parts(
    rails: list[Rail],
    components: list[Component],
    device_of: Callable[[Component], Device | None],
) -> list[Rail]:
    """Rails that still have a DIN module clipped to them."""

    return [rail for rail in rails if _row_on_rail(rail, components, device_of)]


def trim_rails_out_of_side_ducts(rails: list[Rail], trays: list[WireTray]) -> None:
    """Stop a rail at a side duct instead of running through it.

    A large box already clears the ducts. A very small backplate leaves the
    packed rail long enough to enter the right duct; shorten that end.
    """

    vertical = [tray for tray in trays if tray.orientation == "v"]
    if not vertical:
        return
    for rail in rails:
        for _ in range(4):
            box = rail_aabb(rail)
            hit = next((tray for tray in vertical if rects_overlap(*box, *tray_aabb(tray))), None)
            if hit is None:
                break
            tx, _ty, tw, _th = tray_aabb(hit)
            right = rail.x + rail.length_mm
            if rail.x < tx - 0.05 and right > tx - 0.05:
                rail.length_mm = max(8.0, tx - rail.x - 0.4)
            elif rail.x < tx + tw - 0.05:
                new_x = tx + tw + 0.4
                rail.length_mm = max(8.0, right - new_x)
                rail.x = new_x
            else:
                break


def fit_rails_between_ducts(
    rails: list[Rail],
    trays: list[WireTray],
    enclosure: Enclosure | None,
) -> bool:
    """Stretch every TS35 so it runs from duct to duct and does not enter a duct.

    Returns False when the sheet has no pair of side ducts. The caller then
    trims each rail to the devices on it, plus an end stop at each end.
    """

    opening = _opening_between_side_ducts(trays, enclosure)
    if opening is None or not rails:
        return False
    x, length = opening
    for rail in rails:
        rail.x = x
        rail.length_mm = length
    return True


def _fresh_rail_id(rails: list[Rail]) -> str:
    used = {rail.id for rail in rails}
    n = len(rails) + 1
    while f"r{n}" in used:
        n += 1
    return f"r{n}"


def _clear_span_of_ducts(
    left: float,
    right: float,
    rail: Rail,
    trays: list[WireTray],
) -> tuple[float, float]:
    rh = rail_height(rail)
    top = rail.y - rh / 2.0
    bottom = rail.y + rh / 2.0
    for tray in trays:
        x, y, width, height = tray_aabb(tray)
        if y + height <= top + 0.2 or y >= bottom - 0.2:
            continue
        if x + width <= left + 0.2 or x >= right - 0.2:
            continue
        mid = (left + right) / 2.0
        if x + width / 2.0 <= mid:
            left = max(left, x + width + 0.4)
        else:
            right = min(right, x - 0.4)
    return (left, right)


def _is_end_stop(device: Device, component: Component) -> bool:
    if (device.category or "") == "connection.accessory" or "end_stop" in (device.library_id or ""):
        return True
    return str(component.properties.get("accessory") or "") == "end_stop"


def _row_on_rail(
    rail: Rail,
    components: list[Component],
    device_of: Callable[[Component], Device | None],
) -> list[tuple[Component, float]]:
    """DIN modules on this rail, including the strip's end stops.

    Interlocks and aux blocks in that same row come along too. End stops are
    part of the strip: when the rail moves, they move with the terminals.
    """

    from panelcad.core.enclosure import is_plug_on_overload

    modules: list[tuple[Component, float]] = []
    for comp in components:
        device = device_of(comp)
        if device is None or not is_rail_module(device, comp):
            continue
        if is_plug_on_overload(device, comp):
            continue
        width, _height = footprint_size(device, comp)
        if width <= 0:
            continue
        if abs(comp.y + clip_offset(device, comp) - rail.y) > 0.8:
            continue
        modules.append((comp, width))
    if not modules:
        return []
    band_y = min(comp.y for comp, _width in modules)
    x0 = min(comp.x for comp, _width in modules) - 1.0
    x1 = max(comp.x + width for comp, width in modules) + 1.0
    seen = {comp.id for comp, _width in modules}
    for comp in components:
        if comp.id in seen:
            continue
        device = device_of(comp)
        if device is None or is_plug_on_overload(device, comp):
            continue
        width, _height = footprint_size(device, comp)
        if width <= 0 or abs(comp.y - band_y) > 1.5:
            continue
        if comp.x + width < x0 or comp.x > x1:
            continue
        modules.append((comp, width))
    modules.sort(key=lambda item: (item[0].x, item[0].id))
    return modules


def _extend_trimmed_row(
    rail: Rail,
    components: list[Component],
    device_of: Callable[[Component], Device | None],
    trays: list[WireTray],
    blockers: list[tuple[float, float, float, float]],
) -> None:
    """Shift a trimmed row onto the rail when the duct inset left it short.

    The right end grows by the same amount when the duct and any beside-mounted
    block still leave that much room, so the end stop stays ahead of the devices.
    """

    from panelcad.core.enclosure import is_plug_on_overload

    seated = _row_on_rail(rail, components, device_of)
    modules = [comp for comp, _width in seated]
    if not modules:
        return
    first = min(modules, key=lambda comp: comp.x)
    overhang = rail.x - first.x
    if overhang <= 0.05:
        return
    new_end = rail.x + rail.length_mm + overhang
    _left, cleared = _clear_span_of_ducts(rail.x, new_end, rail, trays)
    new_end = min(new_end, cleared)
    for x, _y, width, _height in blockers:
        if x >= rail.x + rail.length_mm - 0.2:
            new_end = min(new_end, x - 0.4)
    shift = min(overhang, new_end - (rail.x + rail.length_mm))
    if shift <= 0.05:
        return
    moved = {comp.id for comp in modules}
    for comp in modules:
        comp.x += shift
    for comp in components:
        parent = str(comp.properties.get("parent") or "")
        if parent not in moved:
            continue
        device = device_of(comp)
        if device is None or not is_plug_on_overload(device, comp):
            continue
        comp.x += shift
    rail.length_mm += shift


def align_row_to_rail(
    rail: Rail,
    components: list[Component],
    device_of: Callable[[Component], Device | None],
) -> None:
    """Pull a DIN row onto the rail after the rail moves in from the duct.

    The rail keeps TRAY_TO_RAIL_X_MM (40 mm, scaled onto the sheet) off each
    side duct, and at least a 0.8 mm stroke gap. Modules that were packed on
    the duct face then start before the rail and sit on the duct.
    Shift the whole row when the right end still fits. Otherwise close the
    ordinary module gaps until the row fits, and move a plug-on with its host.
    """

    from panelcad.core.enclosure import is_plug_on_overload

    modules = _row_on_rail(rail, components, device_of)
    if not modules:
        return
    modules.sort(key=lambda item: (item[0].x, item[0].id))
    rail_end = rail.x + rail.length_mm
    first = modules[0][0]
    last, last_w = modules[-1]
    if first.x >= rail.x - 0.05 and last.x + last_w <= rail_end + 0.05:
        return

    def shift_plugs(moves: dict[str, float]) -> None:
        if not moves:
            return
        for comp in components:
            parent = str(comp.properties.get("parent") or "")
            if parent not in moves:
                continue
            device = device_of(comp)
            if device is None or not is_plug_on_overload(device, comp):
                continue
            comp.x += moves[parent]

    shift = rail.x - first.x
    if shift > 0.05 and last.x + last_w + shift <= rail_end + 0.05:
        moves = {}
        for comp, _width in modules:
            comp.x += shift
            moves[comp.id] = shift
        shift_plugs(moves)
        return
    widths = [width for _comp, width in modules]
    total = sum(widths)
    span = rail_end - rail.x
    if total > span + 0.05:
        # Shifting a row that is already wider than the rail runs the last
        # module into the far duct. Move only as far as the rail end allows.
        room = rail_end - (last.x + last_w)
        applied = min(shift, room) if shift > 0.05 and room > 0.05 else 0.0
        if applied > 0.05:
            moves = {}
            for comp, _width in modules:
                comp.x += applied
                moves[comp.id] = applied
            shift_plugs(moves)
        return
    gaps: list[float] = []
    for (left_comp, left_w), (right_comp, _right_w) in zip(modules, modules[1:], strict=False):
        gaps.append(max(0.0, right_comp.x - (left_comp.x + left_w)))
    spare = span - total
    if gaps and sum(gaps) > spare + 0.05:
        natural = sum(gaps)
        factor = spare / natural if natural > 1e-6 else 0.0
        gaps = [gap * factor for gap in gaps]
    cursor = rail.x
    moves = {}
    for index, (comp, width) in enumerate(modules):
        moves[comp.id] = cursor - comp.x
        comp.x = cursor
        cursor += width
        if index < len(gaps):
            cursor += gaps[index]
    shift_plugs(moves)


def _opening_hits(
    opening: tuple[float, float],
    boxes: list[tuple[float, float, float, float]],
) -> bool:
    start, length = opening
    end = start + length
    for x, _y, width, _height in boxes:
        if x + width <= start + 0.3 or x >= end - 0.3:
            continue
        return True
    return False


def _seat_modules_on_clip(
    components: list[Component],
    rails: list[Rail],
    device_of: Callable[[Component], Device | None],
) -> None:
    """Drop each module so its clip lies on the rail it already nearly meets.

    The arrangement draws a rail 15 mm under the body top. A 47 mm terminal
    clips at 11.75 mm, so the strip sits 3.25 mm off that line and a later
    horizontal move cannot see it. Seat the body first. A plug-on stays under
    its host.
    """

    if not rails:
        return
    from panelcad.core.enclosure import is_plug_on_overload

    shifts: dict[str, float] = {}
    for comp in components:
        device = device_of(comp)
        if device is None or not is_rail_module(device, comp):
            continue
        clip = comp.y + clip_offset(device, comp)
        rail = min(rails, key=lambda item: abs(item.y - clip))
        if abs(rail.y - clip) > 6.0 or abs(rail.y - clip) <= 0.05:
            continue
        new_y = rail.y - clip_offset(device, comp)
        shifts[comp.id] = new_y - comp.y
        comp.y = new_y
    if not shifts:
        return
    for comp in components:
        parent = str(comp.properties.get("parent") or "")
        if parent not in shifts:
            continue
        device = device_of(comp)
        if device is not None and is_plug_on_overload(device, comp):
            comp.y += shifts[parent]


def _nudge_row(
    components: list[Component],
    row: list[tuple[Component, Device, float]],
    device_of: Callable[[Component], Device | None],
    shift: float,
) -> None:
    """Move a seated row, end stops included, and any plug-on under it."""

    if shift <= 0.05:
        return
    from panelcad.core.enclosure import is_plug_on_overload

    moved = set()
    for comp, _device, _width in row:
        comp.x += shift
        moved.add(comp.id)
    for comp in components:
        parent = str(comp.properties.get("parent") or "")
        if parent not in moved:
            continue
        device = device_of(comp)
        if device is not None and is_plug_on_overload(device, comp):
            comp.x += shift


def finish_rails(
    components: list[Component],
    rails: list[Rail],
    trays: list[WireTray],
    enclosure: Enclosure | None,
    device_of: Callable[[Component], Device | None],
    stop_device: Device | None = None,
) -> list[Component]:
    """Run each rail from duct to duct, or trim it and stop both ends.

    A backplate block in the rail band (a drive beside the row) keeps that
    rail on its modules. The rail then gains an end stop at each end instead
    of running through the block. Side clearance is ``TRAY_TO_RAIL_X_MM`` when
    the opening has spare, including a rail trimmed beside a block. Moving a
    rail carries every module on it, and the strip's end stops move with it.
    """

    if enclosure is None or not rails:
        return []
    from panelcad.core.enclosure import is_plug_on_overload

    scale = _draw_scale(enclosure)
    added: list[Component] = []
    used = {comp.id for comp in components}
    stop_w = END_STOP_MM * scale
    stop_clip = 0.0
    if stop_device is not None:
        ghost = Component(
            id="_end_stop",
            library_id=stop_device.library_id,
            x=0.0,
            y=0.0,
            scale=scale,
            properties={"accessory": "end_stop", "addon": True},
        )
        width, _height = footprint_size(stop_device, ghost)
        if width > 0:
            stop_w = width
        stop_clip = clip_offset(stop_device, ghost)

    def band_blockers(rail: Rail) -> list[tuple[float, float, float, float]]:
        height = rail_height(rail)
        top = rail.y - height / 2.0
        bottom = rail.y + height / 2.0
        boxes: list[tuple[float, float, float, float]] = []
        for comp in components:
            device = device_of(comp)
            if device is None:
                continue
            if is_rail_module(device, comp) or is_plug_on_overload(device, comp):
                continue
            width, body = footprint_size(device, comp)
            if width <= 0 or body <= 0:
                continue
            if comp.y + body <= top + 0.3 or comp.y >= bottom - 0.3:
                continue
            boxes.append((comp.x, comp.y, width, body))
        return boxes

    def seated(rail: Rail) -> list[tuple[Component, Device, float]]:
        found: list[tuple[Component, Device, float]] = []
        for comp in list(components) + added:
            device = device_of(comp)
            if device is None or not is_rail_module(device, comp):
                continue
            width, _height = footprint_size(device, comp)
            if width <= 0:
                continue
            if abs(comp.y + clip_offset(device, comp) - rail.y) > 0.8:
                continue
            found.append((comp, device, width))
        return found

    def make_stop(rail: Rail, x: float, side: str) -> Component | None:
        if stop_device is None:
            return None
        # Snap the hair that made a touching breaker look like an overlap.
        x = round(float(x), 3)
        y = round(float(rail.y - stop_clip), 3)
        for comp, device, _width in seated(rail):
            if _is_end_stop(device, comp) and abs(comp.x - x) <= 1.2:
                return None
        ident = f"ES-{rail.id}-{side}"
        for comp in list(components) + added:
            if comp.id != ident:
                continue
            comp.x = x
            comp.y = y
            comp.scale = scale
            comp.properties["role"] = "end_stop"
            comp.properties["accessory"] = "end_stop"
            comp.properties["addon"] = True
            comp.properties["layout_furniture"] = True
            return None
        n = 2
        while ident in used:
            ident = f"ES-{rail.id}-{side}{n}"
            n += 1
        used.add(ident)
        return Component(
            id=ident,
            library_id=stop_device.library_id,
            x=x,
            y=y,
            scale=scale,
            properties={
                "role": "end_stop",
                "accessory": "end_stop",
                "addon": True,
                "layout_furniture": True,
            },
        )

    _seat_modules_on_clip(components, rails, device_of)
    faces = _side_duct_faces(trays, enclosure)
    for rail in rails:
        span = None if faces is None else (faces[0], faces[1] - faces[0])
        if span is not None and not _opening_hits(span, band_blockers(rail)):
            left_edge, right_edge = faces
            found = seated(rail)
            if found:
                first_x = min(comp.x for comp, _device, _width in found)
                last_x = max(comp.x + width for comp, _device, width in found)
                row_width = last_x - first_x
            else:
                row_width = 0.0
            gap = _rail_gap_beside_ducts(left_edge, right_edge, row_width, scale)
            rail.x = left_edge + gap
            rail.length_mm = right_edge - gap - rail.x
            if rail.length_mm >= 16.0:
                align_row_to_rail(rail, components, device_of)
                continue
        row = seated(rail)
        if not row:
            continue
        left_comp, left_dev, _left_w = min(row, key=lambda item: item[0].x)
        right_comp, right_dev, right_w = max(row, key=lambda item: item[0].x + item[2])
        device_left = left_comp.x
        device_right = right_comp.x + right_w
        # A trimmed rail uses the same duct clearance as a full-span rail.
        # The row stops before a beside-mounted block, so the gap is only as
        # large as the spare in that opening.
        if faces is not None:
            span_left, span_right = faces
            row_mid = (device_left + device_right) / 2.0
            for bx, _by, bw, _bh in band_blockers(rail):
                if bx + bw <= device_left + 0.4:
                    span_left = max(span_left, bx + bw + 0.4)
                elif bx >= device_right - 0.4:
                    span_right = min(span_right, bx - 0.4)
                elif bx + bw / 2.0 >= row_mid:
                    span_right = min(span_right, bx - 0.4)
                else:
                    span_left = max(span_left, bx + bw + 0.4)
            row_width = max(0.0, device_right - device_left)
            if span_right > span_left + 16.0 and row_width > 0.0:
                gap = _rail_gap_beside_ducts(span_left, span_right, row_width, scale)
                target = min(span_left + gap, span_right - row_width)
                shift = target - device_left
                if shift > 0.05:
                    _nudge_row(components, row, device_of, shift)
                    device_left += shift
                    device_right += shift
        pad_left = 0.0 if _is_end_stop(left_dev, left_comp) else stop_w
        pad_right = 0.0 if _is_end_stop(right_dev, right_comp) else stop_w
        left, right = _clear_span_of_ducts(device_left - pad_left, device_right + pad_right, rail, trays)
        # A duct hard against the modules eats the end-stop pad. Do not park the stop on a device.
        if pad_left > 0 and left > device_left - stop_w + 0.4:
            pad_left = 0.0
            left = max(left, device_left)
        if pad_right > 0 and right < device_right + stop_w - 0.4:
            pad_right = 0.0
            right = min(right, device_right)
        if right - left < 16.0:
            continue
        rail.x = left
        rail.length_mm = right - left
        # The duct inset can leave the first module a fraction before the rail.
        # Grow the rail into clear space and move the row with it so the module
        # starts on the rail and the end-stop pad stays in front of the devices.
        _extend_trimmed_row(rail, components, device_of, trays, band_blockers(rail))
        if pad_left > 0:
            stop = make_stop(rail, rail.x, "L")
            if stop is not None:
                added.append(stop)
        if pad_right > 0:
            stop = make_stop(rail, rail.x + rail.length_mm - stop_w, "R")
            if stop is not None:
                added.append(stop)
    return added


def trim_rails_to_devices(
    rails: list[Rail],
    components: list[Component],
    device_of: Callable[[Component], Device | None],
    trays: list[WireTray],
    enclosure: Enclosure | None,
) -> None:
    """When there is no side duct, each rail covers its devices plus an end stop."""

    if _opening_between_side_ducts(trays, enclosure) is not None:
        return
    scale = _draw_scale(enclosure) if enclosure is not None else 1.0
    pad = END_STOP_MM * scale
    for rail in rails:
        seated: list[tuple[float, float]] = []
        for comp in components:
            device = device_of(comp)
            if device is None:
                continue
            width, _height = footprint_size(device, comp)
            if width <= 0:
                continue
            clip_y = comp.y + clip_offset(device, comp)
            if abs(clip_y - rail.y) > 0.8:
                continue
            seated.append((comp.x, width))
        if not seated:
            continue
        left = min(x for x, _width in seated) - pad
        right = max(x + width for x, width in seated) + pad
        left, right = _clear_span_of_ducts(left, right, rail, trays)
        if right - left < 16.0:
            continue
        rail.x = left
        rail.length_mm = right - left


def _blank_rail(
    rails: list[Rail],
    trays: list[WireTray],
    enclosure: Enclosure,
) -> Rail:
    scale = _draw_scale(enclosure)
    opening = _opening_between_side_ducts(trays, enclosure)
    if opening is not None:
        x, length = opening
    else:
        left, _top, right, _bottom = backplate_sheet_bounds(enclosure)
        x = left + 4.0
        length = max(40.0, right - left - 8.0)
    rail = Rail(
        id=_fresh_rail_id(rails),
        x=x,
        y=float(enclosure.y) + (40.0 + RAIL_HEIGHT_MM / 2.0) * scale,
        length_mm=length,
        height_mm=RAIL_HEIGHT_MM * scale,
    )
    rails.append(rail)
    return rail


def _rail_below(
    prev: Rail,
    hanging_bottom: float,
    rails: list[Rail],
    trays: list[WireTray],
    enclosure: Enclosure,
) -> Rail:
    scale = _draw_scale(enclosure)
    rh = rail_height(prev)
    y = max(
        prev.y + rh + RAIL_CLEAR_MM * scale,
        hanging_bottom + RAIL_CLEAR_MM * scale + rh / 2.0,
    )
    rail = Rail(
        id=_fresh_rail_id(rails),
        x=prev.x,
        y=y,
        length_mm=prev.length_mm,
        height_mm=rh,
        type=prev.type,
    )
    for _ in range(6):
        box = rail_aabb(rail)
        hit = next((tray_aabb(tray) for tray in trays if rects_overlap(*box, *tray_aabb(tray))), None)
        if hit is None:
            break
        rail.y = hit[1] + hit[3] + rh / 2.0 + max(0.4, scale)
    rails.append(rail)
    return rail


def _plug_on_host(
    extra: Component,
    modules: list[Component],
    device_of: Callable[[Component], Device | None],
) -> Component | None:
    parent = str(extra.properties.get("parent") or "").strip()
    if parent:
        return next((module for module in modules if module.id == parent), None)
    group = str(extra.properties.get("layout_group") or "")
    contactors: list[Component] = []
    for host in modules:
        device = device_of(host)
        if device is None or not (device.category or "").lower().startswith("switching.contactor"):
            continue
        host_group = str(host.properties.get("layout_group") or "")
        if group and host_group and group != host_group:
            continue
        contactors.append(host)
    if not contactors:
        return None
    try:
        order = float(extra.properties.get("layout_order"))
    except (TypeError, ValueError):
        order = None
    if order is None:
        return contactors[-1]
    before: list[Component] = []
    for host in contactors:
        try:
            host_order = float(host.properties.get("layout_order"))
        except (TypeError, ValueError):
            continue
        if host_order <= order + 1e-9:
            before.append(host)
    if not before:
        return contactors[-1]
    return max(before, key=lambda host: float(host.properties.get("layout_order") or 0.0))


def reseat_din_modules(
    components: list[Component],
    rails: list[Rail],
    trays: list[WireTray],
    enclosure: Enclosure | None,
    device_of: Callable[[Component], Device | None],
    order_x: dict[str, float] | None = None,
) -> list[str]:
    """Put every DIN-mount module's clip on a TS35 segment.

    The row keeps left-to-right order. A module that does not fit the rail
    goes onto the next rail, and a new TS35 is added when the sheet has none
    left. Plug-on overloads stay stacked under their contactor. Parts that
    are not rail modules are moved off the rails and the ducts.
    """

    if enclosure is None or not components:
        return []
    from panelcad.core.enclosure import CONTROL_RAIL_GAP_MM, classify_role, is_plug_on_overload

    scale = _draw_scale(enclosure)
    gap = MODULE_GAP_MM * scale
    control_gap = CONTROL_RAIL_GAP_MM * scale
    order = order_x or {}
    modules: list[Component] = []
    plug_ons: list[Component] = []
    others: list[Component] = []
    for comp in components:
        device = device_of(comp)
        if device is None:
            continue
        width, height = footprint_size(device, comp)
        if width <= 0.0 or height <= 0.0:
            continue
        if is_plug_on_overload(device, comp):
            plug_ons.append(comp)
        elif is_rail_module(device, comp):
            modules.append(comp)
        else:
            others.append(comp)
    modules.sort(key=lambda comp: (float(order.get(comp.id, comp.x)), comp.id))
    hosted: dict[str, list[Component]] = {}
    for extra in plug_ons:
        host = _plug_on_host(extra, modules, device_of)
        if host is None:
            modules.append(extra)
        else:
            hosted.setdefault(host.id, []).append(extra)
    if not modules and not others:
        trim_rails_to_devices(rails, components, device_of, trays, enclosure)
        return []
    if modules and not rails:
        _blank_rail(rails, trays, enclosure)

    def stack_height(host: Component) -> float:
        total = 0.0
        for extra in hosted.get(host.id, ()):
            device = device_of(extra)
            if device is None:
                continue
            _width, height = footprint_size(device, extra)
            total += height
        return total

    notes: list[str] = []
    added: list[str] = []

    def pack(parts: list[Component], pool: list[Rail]) -> None:
        if not parts:
            return
        if not pool:
            pool.append(_blank_rail(rails, trays, enclosure))
        pool.sort(key=lambda rail: (rail.y, rail.id))
        index = 0
        rail = pool[index]
        cursor = rail.x
        row_bottom = rail.y
        seen_power = False
        seen_control = False
        for comp in parts:
            device = device_of(comp)
            if device is None:
                continue
            width, height = footprint_size(device, comp)
            band = str(comp.properties.get("layout_band") or "")
            extra = 0.0
            if cursor > rail.x + 0.05:
                if band == "control" and seen_power and not seen_control:
                    extra = control_gap
                else:
                    extra = gap
            fits = cursor + extra + width <= rail.x + rail.length_mm + 0.4
            if not fits:
                alone = cursor <= rail.x + 0.05 and width > rail.length_mm + 0.6
                if alone:
                    comp.x = rail.x
                    comp.y = rail.y - clip_offset(device, comp)
                    cursor = rail.x + rail.length_mm
                    row_bottom = max(row_bottom, comp.y + height + stack_height(comp))
                    if band == "control":
                        seen_control = True
                    elif band == "power":
                        seen_power = True
                    continue
                if len(added) >= 6:
                    notes.append(f"DIN device does not fit on a rail: {comp.id}")
                    continue
                index += 1
                if index >= len(pool):
                    rail = _rail_below(rail, row_bottom, rails, trays, enclosure)
                    pool.append(rail)
                    added.append(rail.id)
                else:
                    rail = pool[index]
                cursor = rail.x
                extra = 0.0
                row_bottom = rail.y
                seen_power = False
                seen_control = False
            comp.x = cursor + extra
            comp.y = rail.y - clip_offset(device, comp)
            cursor = comp.x + width
            row_bottom = max(row_bottom, comp.y + height + stack_height(comp))
            if band == "control":
                seen_control = True
            elif band == "power":
                seen_power = True

    din_rails = [rail for rail in rails if "terminal" not in rail.id.lower()]
    term_rails = [rail for rail in rails if "terminal" in rail.id.lower()]
    def _role(comp: Component) -> str:
        device = device_of(comp)
        if device is None:
            return ""
        return classify_role(device, comp)

    din_parts = [comp for comp in modules if _role(comp) != "terminal"]
    term_parts = [comp for comp in modules if comp not in din_parts]
    pack(din_parts, din_rails)
    if term_parts:
        if not term_rails:
            prev = max(rails, key=lambda rail: rail.y)
            bottom = prev.y
            for comp in din_parts:
                device = device_of(comp)
                if device is None:
                    continue
                _width, height = footprint_size(device, comp)
                bottom = max(bottom, comp.y + height + stack_height(comp))
            rail = _rail_below(prev, bottom, rails, trays, enclosure)
            term_rails.append(rail)
            added.append(rail.id)
        pack(term_parts, term_rails)
    for host_id, extras in hosted.items():
        host = next((comp for comp in modules if comp.id == host_id), None)
        if host is None:
            continue
        host_device = device_of(host)
        if host_device is None:
            continue
        host_w, host_h = footprint_size(host_device, host)
        y = host.y + host_h
        for extra in extras:
            device = device_of(extra)
            if device is None:
                continue
            width, height = footprint_size(device, extra)
            extra.x = host.x + max(0.0, (host_w - width) / 2.0)
            extra.y = y
            y += height
    bounds = backplate_sheet_bounds(enclosure)
    blocked = [tray_aabb(tray) for tray in trays]
    blocked.extend(rail_aabb(rail) for rail in rails)
    for comp in modules:
        device = device_of(comp)
        if device is None:
            continue
        width, height = footprint_size(device, comp)
        blocked.append((comp.x, comp.y, width, height + stack_height(comp)))
    def _area(comp: Component) -> float:
        device = device_of(comp)
        if device is None:
            return 0.0
        width, height = footprint_size(device, comp)
        return width * height

    others.sort(key=lambda comp: -_area(comp))
    moved: list[str] = []
    for comp in others:
        device = device_of(comp)
        if device is None:
            continue
        width, height = footprint_size(device, comp)
        origin = (comp.x, comp.y)
        x, y = _clamp_on_plate(comp.x, comp.y, width, height, bounds)
        if _slot_fits(x, y, width, height, bounds) and not _hits_keepout(x, y, width, height, blocked):
            comp.x, comp.y = x, y
        else:
            spot = _slot_on_plate(width, height, bounds, blocked, prefer=(x, y))
            if spot is None:
                comp.x, comp.y = x, y
                notes.append(f"True-size footprint does not fit inside the backplate: {comp.id}")
            else:
                comp.x, comp.y = spot
        if abs(comp.x - origin[0]) > 0.6 or abs(comp.y - origin[1]) > 0.6:
            moved.append(comp.id)
        blocked.append((comp.x, comp.y, width, height))
    trim_rails_to_devices(rails, components, device_of, trays, enclosure)
    if added:
        notes.append("Added TS35 " + ", ".join(added) + " so every DIN device is on a rail")
    if moved:
        notes.append("Moved clear of the DIN rail: " + ", ".join(moved))
    return notes
