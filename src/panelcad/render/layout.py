"""Draw panel_layout / door_layout sheets: enclosure, DIN rails, footprints."""

from __future__ import annotations

from pathlib import Path

from dataclasses import dataclass

from panelcad.core.layout import (
    NAME_STRIP_MM,
    RAIL_HEIGHT_MM,
    clip_offset,
    enclosure_drawn_size,
    footprint_size,
    format_draw_ratio,
    heading_rect,
    legend_plate_rect,
    legend_text_size,
    rail_aabb,
    rail_height,
    rects_overlap,
    tray_aabb,
)
from panelcad.library.panel import display_footprint_size, effective_footprint
from panelcad.core.models import Component, Device, Enclosure, Rail, Sheet, WireTray
from panelcad.library.catalog import Catalog
from panelcad.render.painter import Painter
from panelcad.render.titleblock import resolve_logo

_FILL = {
    "protection": "#f3d6d6",
    "switching": "#d6e4f4",
    "control": "#d9ead3",
    "power": "#fce5cd",
    "operator": "#fff2cc",
    "hmi": "#d0e2f3",
    "plc": "#d9ead3",
    "drive": "#ead1dc",
    "terminal": "#e0e0e0",
    "load": "#eeeeee",
}


def _fill_for(device: Device) -> str:
    cat = (device.category or "").lower()
    for key, color in _FILL.items():
        if key in cat:
            return color
    return "#e8eef4"


def _on_screen(painter: Painter) -> bool:
    return getattr(painter, "qp", None) is not None and not getattr(painter, "_print_paper", True)


def _screen_scale(painter: Painter) -> float | None:
    """Pixels per millimetre, or None on a print painter."""

    if not _on_screen(painter):
        return None
    try:
        scale = abs(float(painter.qp.transform().m11()))
    except (AttributeError, TypeError, ValueError):
        return None
    return scale or None


def draw_enclosure(painter: Painter, enc: Enclosure) -> None:
    dw, dh, scale = enclosure_drawn_size(enc)
    painter.set_layer("OUTLINE")
    label_color = None
    if _on_screen(painter):
        from panelcad.theme import canvas_hex, get_canvas

        mode = get_canvas()
        painter.set_fill(canvas_hex("enclosure_backplate", mode))
        painter.set_stroke(canvas_hex("enclosure_wall", mode), 0.7)
        label_color = canvas_hex("ink", mode)
    else:
        painter.set_fill("#f3f1ea")
        painter.set_stroke("#5c5c5c", 0.7)
    painter.rect(enc.x, enc.y, dw, dh)
    panel_w = float(getattr(enc, "panel_width_mm", 0.0) or 0.0) * scale
    panel_h = float(getattr(enc, "panel_height_mm", 0.0) or 0.0) * scale
    if panel_w > 1.0 and panel_h > 1.0 and (panel_w < dw - 0.5 or panel_h < dh - 0.5):
        # Subpanel at true size, centered the way it mounts in the box.
        painter.set_fill(None)
        painter.set_stroke("#2a2a2a", 0.55)
        painter.rect(enc.x + (dw - panel_w) / 2.0, enc.y + (dh - panel_h) / 2.0, panel_w, panel_h)
    painter.set_fill(None)
    painter.set_layer("TEXT")
    label = enc.name or f"{enc.width_mm:.0f} × {enc.height_mm:.0f} × {enc.depth_mm:.0f} mm"
    if scale < 0.999 and not painter.layout_is_true_scale():
        label = f"{label}  (drawn {format_draw_ratio(scale)})"
    strip = NAME_STRIP_MM * scale
    extras = {"color": label_color} if label_color and hasattr(painter, "qp") else {}
    painter.text(
        enc.x + 3,
        enc.y + min(6.0, max(2.4, strip * 0.6)),
        label,
        min(3.2, max(2.0, strip * 0.4)),
        align="start",
        **extras,
    )


def _tray_label_mm(painter: Painter, nominal: float) -> float:
    """Keep a duct label at least ~10 px on screen. Print stays at the millimetre size."""

    scale = _screen_scale(painter)
    if scale is None:
        return nominal
    return max(nominal, min(6.0, 10.0 / scale))


def _elide_mm(text: str, size: float, limit: float) -> str:
    """Shorten ``text`` so it fits in ``limit`` millimetres."""

    if limit <= 0:
        return ""
    if caption_width(text, size) <= limit:
        return text
    kept = text
    while kept and caption_width(kept + "…", size) > limit:
        kept = kept[:-1]
    return (kept + "…") if kept else ""


def draw_tray(
    painter: Painter,
    tray: WireTray,
    mm_scale: float = 1.0,
    avoid: list[tuple[float, float, float, float]] | None = None,
) -> None:
    """Slotted wiring duct. The caption sits inside the duct, along its length."""
    painter.set_layer("DUCT")
    on_screen = getattr(painter, "qp", None) is not None and not getattr(painter, "_print_paper", True)
    label_color = None
    if on_screen:
        from panelcad.theme import canvas_hex, get_canvas

        mode = get_canvas()
        painter.set_fill(canvas_hex("enclosure_duct", mode))
        painter.set_stroke(canvas_hex("enclosure_duct_edge", mode), 0.45)
        if mode == "dark":
            label_color = canvas_hex("ink", "dark")
    else:
        painter.set_fill("#d8d2c6")
        painter.set_stroke("#6a6458", 0.45)
    if tray.orientation == "v":
        painter.rect(tray.x, tray.y, tray.width_mm, tray.length_mm)
        painter.set_fill(None)
        y = tray.y + 4.0
        while y < tray.y + tray.length_mm - 3.0:
            painter.line(tray.x + 2.0, y, tray.x + tray.width_mm - 2.0, y)
            y += 8.0
    else:
        painter.rect(tray.x, tray.y, tray.length_mm, tray.width_mm)
        painter.set_fill(None)
        x = tray.x + 4.0
        while x < tray.x + tray.length_mm - 3.0:
            painter.line(x, tray.y + 2.0, x, tray.y + tray.width_mm - 2.0)
            x += 8.0
    label = plan_tray_label(tray, mm_scale, avoid or [])
    painter.set_layer("TEXT")
    size = _tray_label_mm(painter, label.size) if on_screen else label.size
    painter.text(
        label.x,
        label.y,
        label.text,
        size,
        align=label.align,
        rotate=label.rotate,
        **({"color": label_color} if label_color else {}),
    )


def draw_rail(
    painter: Painter,
    rail: Rail,
    mm_scale: float = 1.0,
    avoid: list[tuple[float, float, float, float]] | None = None,
) -> None:
    rh = rail_height(rail)
    half = rh / 2.0
    hat = max(1.0, rh * (5.0 / RAIL_HEIGHT_MM))
    painter.set_layer("RAIL")
    painter.set_stroke("#8a8f96", 0.25)
    painter.set_fill(None)
    painter.rect(rail.x, rail.y - half, rail.length_mm, rh)
    painter.set_fill("#c5c9ce")
    painter.set_stroke("#4a4e54", 0.45)
    painter.rect(rail.x, rail.y - hat / 2.0, rail.length_mm, hat)
    painter.set_fill(None)
    painter.set_layer("TEXT")
    label = plan_rail_label(rail, mm_scale, avoid or [])
    painter.text(label.x, label.y, label.text, label.size, align=label.align, rotate=label.rotate)


@dataclass(frozen=True)
class FurnitureLabel:
    """A rail or duct caption and the rectangle it occupies on the sheet."""

    x: float
    y: float
    text: str
    size: float
    align: str
    rotate: float
    rect: tuple[float, float, float, float]


def _caption_rect(x: float, baseline: float, width: float, size: float) -> tuple[float, float, float, float]:
    """Axis-aligned box for start-aligned text whose baseline is `baseline`."""

    return (x, baseline - size * 0.92, width, size * 1.2)


def rail_label_text(rail: Rail, mm_scale: float) -> str:
    scale = mm_scale if mm_scale > 1e-9 else 1.0
    return f"{rail.id}  TS35  {rail.length_mm / scale:.0f} mm"


def plan_rail_label(
    rail: Rail,
    mm_scale: float,
    avoid: list[tuple[float, float, float, float]] | None = None,
) -> FurnitureLabel:
    """Rail name at the open right end, above the rail and off parts and ducts."""

    text = rail_label_text(rail, mm_scale)
    size = 2.6
    x = _rail_label_x(rail, text, size, avoid or [])
    baseline = rail.y - rail_height(rail) / 2.0 - 1.5
    rect = _caption_rect(x, baseline, caption_width(text, size), size)
    return FurnitureLabel(x, baseline, text, size, "start", 0.0, rect)


def plan_tray_label(
    tray: WireTray,
    mm_scale: float,
    avoid: list[tuple[float, float, float, float]] | None = None,
) -> FurnitureLabel:
    """Duct name inside the duct, running along its length."""

    blocked = avoid or []
    scale = mm_scale if mm_scale > 1e-9 else 1.0
    true_w = tray.width_mm / scale
    bx, by, bw, bh = tray_aabb(tray)
    if tray.orientation == "v":
        text = f"{tray.id}  {true_w:.0f} mm"
        size = 2.0
        while size > 1.25 and caption_width(text, size) > bh - 3.0:
            size -= 0.15
        width = caption_width(text, size)
        thick = size * 1.2
        anchor_x = bx + bw / 2.0
        y_lo = by + 0.8 + width
        y_hi = by + bh - 0.8

        def rect_at(anchor_y: float) -> tuple[float, float, float, float]:
            # rotate=-90 sends the caption up the duct. Thickness stays across the duct.
            return (anchor_x - thick / 2.0, anchor_y - width, thick, width)

        def clear(anchor_y: float) -> bool:
            rect = rect_at(anchor_y)
            inside = (
                rect[0] >= bx + 0.25
                and rect[1] >= by + 0.25
                and rect[0] + rect[2] <= bx + bw - 0.25
                and rect[1] + rect[3] <= by + bh - 0.25
            )
            if not inside:
                return False
            return not any(rects_overlap(*rect, *other) for other in blocked)

        anchor_y = (y_lo + y_hi) / 2.0 if y_lo <= y_hi else by + bh - 0.8
        if not clear(anchor_y) and y_lo <= y_hi:
            trial = y_lo
            while trial <= y_hi + 0.01:
                if clear(trial):
                    anchor_y = trial
                    break
                trial += 4.0
        return FurnitureLabel(anchor_x, anchor_y, text, size, "start", -90.0, rect_at(anchor_y))
    text = f"{tray.id}  {true_w:.0f} mm duct"
    size = 2.2
    while size > 1.25 and caption_width(text, size) > bw - 3.0:
        size -= 0.15
    width = caption_width(text, size)
    baseline = by + bh / 2.0 + 0.32 * size

    def hrect(x: float) -> tuple[float, float, float, float]:
        return _caption_rect(x, baseline, width, size)

    def hclear(x: float) -> bool:
        rect = hrect(x)
        inside = (
            rect[0] >= bx + 0.25
            and rect[1] >= by + 0.15
            and rect[0] + rect[2] <= bx + bw - 0.25
            and rect[1] + rect[3] <= by + bh - 0.15
        )
        if not inside:
            return False
        return not any(rects_overlap(*rect, *other) for other in blocked)

    x = bx + max(1.0, (bw - width) / 2.0)
    if not hclear(x):
        trial = bx + 1.5
        while trial + width <= bx + bw - 0.3:
            if hclear(trial):
                x = trial
                break
            trial += 4.0
    return FurnitureLabel(x, baseline, text, size, "start", 0.0, hrect(x))


def layout_label_avoid(
    sheet: Sheet,
    catalog: Catalog,
    kind: str,
) -> list[tuple[float, float, float, float]]:
    """Rectangles a rail or duct caption must miss. Duct captions may sit in a duct."""

    parts: list[tuple[float, float, float, float]] = []
    for comp in sheet.components:
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        width, height = display_footprint_size(device, comp)
        if width > 0.0 and height > 0.0:
            parts.append((comp.x, comp.y, width, height))
    rails = [rail_aabb(rail) for rail in sheet.rails]
    ducts = [tray_aabb(tray) for tray in sheet.trays]
    terminals = [label.rect for label in plan_terminal_labels(sheet, catalog).values()]
    if kind == "tray":
        return parts + rails + terminals
    return parts + rails + ducts + terminals


def _rail_label_x(
    rail: Rail,
    label: str,
    size: float,
    avoid: list[tuple[float, float, float, float]],
) -> float:
    """Park the rail name at the right end, then slide left until it is clear."""

    rh = rail_height(rail)
    baseline = rail.y - rh / 2.0 - 1.5
    width = caption_width(label, size)
    rail_box = rail_aabb(rail)

    def hits(x: float) -> bool:
        rect = _caption_rect(x, baseline, width, size)
        if rects_overlap(*rect, *rail_box):
            return True
        return any(rects_overlap(*rect, *other) for other in avoid)

    left = rail.x + 2.0
    right = rail.x + rail.length_mm - 2.0 - width
    x = right if right >= left else left
    guard = 0
    while x >= left - 0.01 and guard < 80:
        if not hits(x):
            return x
        x -= max(3.0, width * 0.2)
        guard += 1
    # A short terminal rail is narrower than its caption. Park the name in the
    # open field just past the modules so it does not cross the numbers.
    past = rail.x + rail.length_mm + 1.6
    if not hits(past):
        return past
    if not hits(left):
        return left
    return right if right >= left else left


# Conservative Arial advance (em) so captions stay inside a footprint in AutoCAD.
_CHAR_EM = 0.62


def caption_width(text: str, size: float) -> float:
    return max(0.0, len(text or "") * float(size) * _CHAR_EM)


def _clip_line(text: str, width: float, size: float) -> str:
    """Keep one caption line inside ``width``. A long word is ellipsized, not spilled."""

    raw = " ".join((text or "").split())
    if not raw or width <= 0:
        return ""
    if caption_width(raw, size) <= width + 0.05:
        return raw
    kept = raw
    while kept and caption_width(kept + "…", size) > width + 0.05:
        kept = kept[:-1]
    return (kept + "…") if kept else ""


def _wrap_caption(text: str, width: float, size: float) -> list[str]:
    raw = " ".join((text or "").split())
    if not raw:
        return []
    if caption_width(raw, size) <= width:
        return [raw]
    lines: list[str] = []
    current = ""
    for word in raw.split():
        piece = word
        if caption_width(piece, size) > width:
            if current:
                lines.append(_clip_line(current, width, size))
                current = ""
            lines.append(_clip_line(piece, width, size))
            continue
        trial = (current + " " + word).strip()
        if caption_width(trial, size) <= width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(_clip_line(current, width, size))
    return [line for line in lines if line]


def fit_footprint_caption(
    text: str,
    width: float,
    max_height: float,
    *,
    size: float = 1.8,
    min_size: float = 1.2,
    max_lines: int = 2,
) -> tuple[list[str], float]:
    """Wrap and shrink caption text so it stays inside width × max_height."""
    raw = " ".join((text or "").split())
    if not raw or width < 2.0 or max_height < min_size:
        return [], size
    s = float(size)
    while s >= min_size - 1e-9:
        lines = _wrap_caption(raw, width, s)
        too_wide = any(caption_width(line, s) > width + 0.05 for line in lines)
        too_tall = len(lines) * s * 1.2 > max_height + 0.05
        too_many = len(lines) > max_lines
        if not too_wide and not too_tall and not too_many:
            return lines, s
        s -= 0.1
    clipped = _clip_line(raw, width, min_size)
    return ([clipped], min_size) if clipped else ([], min_size)


def _fit_size(text: str, width: float, size: float, min_size: float = 1.15) -> float:
    if not text or width <= 0:
        return min_size
    if caption_width(text, size) <= width:
        return size
    fitted = width / max(len(text) * _CHAR_EM, 0.5)
    return max(0.7, min(float(size), fitted))


def should_draw_din_clip(device: Device, component: Component, width: float, height: float) -> bool:
    """TS35 clip across a rail module. Plug-on parts have no clip of their own."""

    from panelcad.core.enclosure import is_plug_on_overload

    if is_plug_on_overload(device, component):
        return False
    mounting = str(component.properties.get("mounting") or "").strip().lower().replace("-", "_").replace(" ", "_")
    if mounting in {"plug_on", "plugon"}:
        return False
    return bool(device.panel.din_rail) and height >= 12 and width >= 8


def operator_tag_origin(x: float, y: float, width: float, height: float) -> tuple[float, float]:
    """Device tag baseline, outside the operator circle and clear of the legend plate."""

    return (x + width + 1.6, y + max(2.2, min(height * 0.38, height - 1.2)))


def _is_operator_face(device: Device) -> bool:
    cat = (device.category or "").lower()
    return cat.startswith("operator.") or cat.startswith("control.hmi")


def _ink_hits_clip(baseline: float, size: float, clip_y: float | None) -> bool:
    if clip_y is None:
        return False
    top = baseline - size * 0.92
    bot = baseline + size * 0.25
    return not (bot < clip_y - 1.05 or top > clip_y + 1.05)


def caption_baseline(
    baseline: float,
    size: float,
    *,
    clip_y: float | None,
    body_top: float,
    body_bot: float,
) -> float | None:
    """A baseline whose ink stays in the body and off the DIN clip. None if it cannot."""

    def fits(y: float) -> bool:
        if y - size * 0.92 < body_top - 0.05 or y > body_bot + 0.05:
            return False
        return not _ink_hits_clip(y, size, clip_y)

    if fits(baseline):
        return baseline
    if clip_y is not None:
        below = clip_y + 1.15 + size * 0.92
        if fits(below):
            return below
        above = clip_y - 1.15
        if fits(above):
            return above
    clamped = min(max(baseline, body_top + size * 0.92), body_bot)
    if fits(clamped):
        return clamped
    return None


def _draw_item_balloon(painter: Painter, component: Component, width: float, *, outside: bool = False) -> None:
    item = str(component.properties.get("item") or "").strip()
    if not item:
        return
    if outside:
        bx, by = operator_tag_origin(component.x, component.y, width, 8.0)
        bx += 6.0
    else:
        bx = component.x + width - 2.4
        by = component.y + 2.6
    painter.set_stroke("#1b1b1b", 0.25)
    painter.set_fill(None)
    painter.circle(bx, by, 2.0)
    painter.text(bx, by + 0.6, item, 1.6, align="middle")


def footprint_wire_note(project, ref: str) -> str:
    """Wire number, color, and gauge for conductors that land on this tag."""
    notes: list[str] = []
    for sheet in project.sheets:
        if sheet.kind in {"panel_layout", "door_layout"}:
            continue
        for wire in sheet.wires:
            ends = (str(wire.frm).split(".", 1)[0], str(wire.to).split(".", 1)[0])
            if ref not in ends:
                continue
            text = " ".join(part for part in (wire.number, wire.color, wire.gauge) if str(part).strip())
            if text and text not in notes:
                notes.append(text)
    return ", ".join(notes[:3])


def _draw_operator_body(
    painter: Painter,
    device: Device,
    component: Component,
    width: float,
    height: float,
) -> bool:
    """22 mm operators as a true-scale circle. E-stop is a 40 mm red mushroom on yellow."""

    cat = (device.category or "").lower()
    role = str(component.properties.get("role") or "")
    if not cat.startswith("operator.") or min(width, height) < 4.0:
        return False
    cx = component.x + width / 2.0
    cy = component.y + height / 2.0
    if cat.startswith("operator.estop") or role == "estop":
        painter.set_fill("#f6d326")
        painter.set_stroke("#1b1b1b", 0.45)
        painter.rect(component.x, component.y, width, height)
        painter.set_fill("#d21f1f")
        painter.circle(cx, cy, min(width, height) / 2.0)
        painter.set_fill(None)
        return True
    painter.set_fill(_fill_for(device))
    painter.circle(cx, cy, min(width, height) / 2.0)
    painter.set_fill(None)
    return True


def draw_panel_device(
    painter: Painter,
    device: Device,
    component: Component,
    *,
    base_dir: Path | None = None,
    wire_note: str = "",
    strip_label: StripLabel | None = None,
) -> None:
    w, h = display_footprint_size(device, component)
    if w <= 0 or h <= 0:
        w, h = footprint_size(device, component)
    from panelcad.library.panel import is_info_needed_placeholder

    if is_info_needed_placeholder(component, device):
        from panelcad.reconstruct.placeholder import PLACEHOLDER_MARK_H, PLACEHOLDER_MARK_W

        _draw_needs_info(painter, component, PLACEHOLDER_MARK_W, PLACEHOLDER_MARK_H)
        return
    if w <= 0 or h <= 0:
        return
    if painter.layout_is_true_scale():
        from panelcad.library.panel import is_layout_ready, occupies_panel_layout

        if occupies_panel_layout(device, component) and not is_layout_ready(device, component):
            return
    painter.set_layer("DEVICES")
    painter.set_stroke("#1b1b1b", 0.4)
    poles = 0
    try:
        poles = int(component.properties.get("pole_count") or 0)
    except (TypeError, ValueError):
        poles = 0
    if poles > 1:
        painter.set_fill(_fill_for(device))
        try:
            gap = float(component.properties.get("pole_gap_mm") or 0) * component.visual_scale()
        except (TypeError, ValueError):
            gap = 0.0
        span = w - gap * max(0, poles - 1)
        pw = span / poles if poles else w
        label = str(component.properties.get("pole_label") or "").strip()
        x = component.x
        for _pole in range(poles):
            painter.rect(x, component.y, max(pw, 0.1), h)
            if label and pw >= 8.0 and h >= 12.0:
                painter.set_layer("TEXT")
                painter.text(
                    x + pw / 2.0,
                    component.y + h / 2.0,
                    label,
                    _fit_size(label, max(1.0, pw - 1.4), 1.8),
                    align="middle",
                )
                painter.set_layer("DEVICES")
                painter.set_fill(_fill_for(device))
            x += pw + gap
        painter.set_fill(None)
        painter.set_stroke("#1b1b1b", 0.35)
        _draw_footprint_captions(painter, device, component, w, h, wire_note=wire_note)
        _draw_item_balloon(painter, component, w, outside=_is_operator_face(device))
        return
    if not _draw_operator_body(painter, device, component, w, h):
        painter.set_fill(_fill_for(device))
        painter.rect(component.x, component.y, w, h)
        painter.set_fill(None)
    for prim in device.footprint_primitives:
        from panelcad.core.primitives import Primitive
        from panelcad.render.painter import draw_primitive

        if not isinstance(prim, Primitive):
            prim = Primitive.model_validate(prim)
        draw_primitive(painter, prim, component.x, component.y, 0, scale=1.0)
    if device.footprint_attributes:
        from panelcad.core.block import attribute_value

        for attr in device.footprint_attributes:
            text = attribute_value(attr.tag, device, component)
            if text:
                painter.set_layer("TEXT")
                painter.text(component.x + attr.x, component.y + attr.y, text, max(1.6, attr.h))
                painter.set_layer("DEVICES")
    pic = resolve_logo(component.picture, base_dir)
    if pic is not None:
        inset = 1.2 if min(w, h) > 8 else 0.4
        painter.image(str(pic), component.x + inset, component.y + inset, w - 2 * inset, h - 2 * inset)
        painter.set_stroke("#1b1b1b", 0.35)
        _draw_panel_mount(painter, device, component, w, h)
        _draw_footprint_captions(
            painter, device, component, w, h, wire_note=wire_note, strip_label=strip_label
        )
        _draw_item_balloon(painter, component, w, outside=_is_operator_face(device))
        _draw_legend_plate(painter, component, w, h)
        _draw_motor_heading(painter, component, w, h)
        return
    clip_y = None
    if should_draw_din_clip(device, component, w, h):
        if str(component.properties.get("din_clip") or "") == "center":
            clip_y = component.y + h / 2.0
        else:
            clip_y = component.y + clip_offset(device, component)
        painter.set_stroke("#4a4e54", 0.3)
        painter.line(component.x + 1, clip_y, component.x + w - 1, clip_y)
        painter.set_stroke("#7a7568", 0.25)
        tick = max(1.2, min(3.0, w * 0.18))
        n = max(2, int(h / 8.0))
        for i in range(n):
            ty = component.y + 3.0 + i * ((h - 6.0) / max(n - 1, 1))
            if clip_y is not None and abs(ty - clip_y) < 1.3:
                continue
            painter.line(component.x + 0.8, ty, component.x + tick, ty)
            painter.line(component.x + w - tick, ty, component.x + w - 0.8, ty)
    painter.set_stroke("#1b1b1b", 0.35)
    _draw_panel_mount(painter, device, component, w, h)
    _draw_footprint_captions(
        painter, device, component, w, h, wire_note=wire_note, strip_label=strip_label, clip_y=clip_y
    )
    _draw_item_balloon(painter, component, w, outside=_is_operator_face(device))
    _draw_legend_plate(painter, component, w, h)
    _draw_motor_heading(painter, component, w, h)


@dataclass(frozen=True)
class StripLabel:
    """Rotated terminal number, clear of the block and of its neighbours."""

    x: float
    y: float
    size: float
    rotate: float
    rect: tuple[float, float, float, float]


def _rail_stop_id(component: Component) -> bool:
    """Generated rail stops. The block is the mark; the long id is not a shop tag."""

    return str(component.id).startswith("ES-") and str(component.properties.get("accessory") or "") == "end_stop"


def _is_strip_block(device: Device, component: Component) -> bool:
    """Terminal blocks and end stops. Other narrow parts (interlocks, aux) stay put."""

    if effective_footprint(device, component).width_mm > 12.0:
        return False
    return (device.category or "").startswith("connection.")


def plan_terminal_labels(sheet: Sheet, catalog: Catalog) -> dict[str, StripLabel]:
    """One legible number per terminal, alternating above and below the strip.

    A 5 mm block is narrower than a digit once the enclosure is scaled onto the
    sheet, so a horizontal tag overprints its neighbour. Even blocks are labeled
    above the strip and odd blocks below, in the duct gap, so the numbers do not
    share ink.
    """

    blocks: list[tuple[Component, float, float]] = []
    for comp in sheet.components:
        device = catalog.get(comp.library_id)
        if _rail_stop_id(comp):
            continue
        if not _is_strip_block(device, comp):
            continue
        width, height = display_footprint_size(device, comp)
        if width <= 0 or height <= 0:
            continue
        blocks.append((comp, width, height))
    if not blocks:
        return {}
    blocks.sort(key=lambda item: (round(item[0].y, 1), item[0].x))
    rows: list[list[tuple[Component, float, float]]] = []
    for item in blocks:
        if rows and abs(item[0].y - rows[-1][0][0].y) <= 3.0:
            rows[-1].append(item)
        else:
            rows.append([item])
    planned: dict[str, StripLabel] = {}
    for row in rows:
        row.sort(key=lambda item: item[0].x)
        planned.update(_plan_strip_row(sheet, row))
    return planned


# Clearance from the terminal body to the start of a rotated tag.
_STRIP_GAP_MM = 1.6


def _plan_strip_row(sheet: Sheet, row: list[tuple[Component, float, float]]) -> dict[str, StripLabel]:
    top = min(comp.y for comp, _w, _h in row)
    bot = max(comp.y + height for comp, _w, height in row)
    left = min(comp.x for comp, _w, _h in row) - 1.0
    right = max(comp.x + width for comp, width, _h in row) + 1.0
    ceiling = top - 36.0
    floor = bot + 36.0
    if sheet.enclosure is not None:
        _drawn_w, drawn_h, _scale = enclosure_drawn_size(sheet.enclosure)
        ceiling = float(sheet.enclosure.y)
        floor = float(sheet.enclosure.y) + drawn_h
    for tray in sheet.trays:
        x, y, width, height = tray_aabb(tray)
        if x + width < left or x > right:
            continue
        if y + height <= top + 0.4:
            ceiling = max(ceiling, y + height)
        if y >= bot - 0.4:
            floor = min(floor, y)
    gap_above = max(0.0, top - ceiling)
    gap_below = max(0.0, floor - bot)
    # The tag starts _STRIP_GAP_MM off the body. The rest of the gap has to
    # hold the rotated ink, or the number runs into the duct.
    room_above = gap_above - _STRIP_GAP_MM - 0.45
    room_below = gap_below - _STRIP_GAP_MM - 0.45
    above = [item for index, item in enumerate(row) if index % 2 == 0]
    below = [item for index, item in enumerate(row) if index % 2 == 1]
    if room_above >= 1.6 and room_below >= 1.6 and below:
        size = min(
            _strip_size([comp.id for comp, _w, _h in above], room_above, _pitch(above)),
            _strip_size([comp.id for comp, _w, _h in below], room_below, _pitch(below)),
        )
        planned = {
            comp.id: _strip_label(comp, width, top, bot, size, above=index % 2 == 0)
            for index, (comp, width, _height) in enumerate(row)
        }
        return _pull_labels_off_trays(sheet, planned)
    side_above = room_above >= room_below
    room = room_above if side_above else room_below
    tags = [comp.id for comp, _w, _h in row]
    size = _strip_size(tags, max(1.2, room / 2.0), _pitch(above if side_above else below or above))
    planned: dict[str, StripLabel] = {}
    for index, (comp, width, _height) in enumerate(row):
        lane = index % 2
        adv = _ink_advance(comp.id, size)
        extra = lane * (adv + 0.35)
        planned[comp.id] = _strip_label(comp, width, top, bot, size, above=side_above, extra=extra)
    return _pull_labels_off_trays(sheet, planned)


def _pull_labels_off_trays(sheet: Sheet, planned: dict[str, StripLabel]) -> dict[str, StripLabel]:
    """Shrink any tag whose ink still meets a duct, from the duct side."""

    ducts = [tray_aabb(tray) for tray in sheet.trays]
    if not ducts:
        return planned
    pulled: dict[str, StripLabel] = {}
    for ident, label in planned.items():
        size = label.size
        current = label
        guard = 0
        while guard < 28:
            hit = next((duct for duct in ducts if rects_overlap(*current.rect, *duct)), None)
            if hit is None or size <= 1.05:
                break
            guard += 1
            size = max(1.05, size - 0.08)
            advance = _ink_advance(ident, size)
            half = size * 0.5
            rect = current.rect
            top = rect[1]
            bottom = rect[1] + rect[3]
            duct_mid = hit[1] + hit[3] / 2.0
            if duct_mid < (top + bottom) / 2.0:
                anchor = bottom
                new_rect = (current.x - half, anchor - advance, half * 2.0, advance)
                current = StripLabel(current.x, anchor, size, current.rotate, new_rect)
            else:
                new_rect = (current.x - half, top, half * 2.0, advance)
                current = StripLabel(current.x, top + advance, size, current.rotate, new_rect)
        pulled[ident] = current
    return pulled


def _pitch(items: list[tuple[Component, float, float]]) -> float:
    centers = sorted(comp.x + width / 2.0 for comp, width, _h in items)
    if len(centers) < 2:
        return 30.0
    return min(right - left for left, right in zip(centers, centers[1:]))


def _ink_advance(text: str, size: float) -> float:
    """Rotated-tag length. The drawn font can be wider than the 0.62 em estimate."""

    estimate = caption_width(text, size)
    try:
        from panelcad.render.qt_painter import text_advance_mm

        drawn = text_advance_mm(text, size)
    except Exception:
        drawn = estimate
    return max(estimate, drawn)


def _strip_size(tags: list[str], room: float, pitch: float) -> float:
    longest = max(tags, key=len) if tags else "X"
    size = min(2.8, max(1.5, pitch * 0.72))
    limit = max(1.2, room - 0.2)
    while size > 1.15 and _ink_advance(longest, size) > limit:
        size -= 0.05
    return size


def _strip_label(
    comp: Component,
    width: float,
    top: float,
    bot: float,
    size: float,
    *,
    above: bool,
    extra: float = 0.0,
) -> StripLabel:
    """Both sides use rotate -90 so every number reads the same way.

    Above the strip the first character sits against the block and the rest
    runs upward. Below the strip the same rotation is used, with the anchor
    dropped by the text length so the ink stays under the block.
    """

    cx = comp.x + width / 2.0
    advance = _ink_advance(comp.id, size)
    # Rotated ink is about one em wide. Keep the box on the block so an end
    # stop hard against the side duct does not paint into the duct.
    half = size * 0.5
    # Clear the DIN-rail edge, which sits just above the terminal body.
    gap = 1.6 + extra
    if above:
        anchor_y = top - gap
        rect = (cx - half, anchor_y - advance, half * 2.0, advance)
        return StripLabel(cx, anchor_y, size, -90.0, rect)
    anchor_y = bot + gap + advance
    rect = (cx - half, bot + gap, half * 2.0, advance)
    return StripLabel(cx, anchor_y, size, -90.0, rect)


def _draw_footprint_captions(
    painter: Painter,
    device: Device,
    component: Component,
    width: float,
    height: float,
    *,
    wire_note: str = "",
    strip_label: StripLabel | None = None,
    clip_y: float | None = None,
) -> None:
    """Tag, optional description, and size — inside the body and off the DIN clip."""

    if _rail_stop_id(component):
        return
    painter.set_layer("TEXT")
    inner = max(0.0, width - 1.6)
    cx = component.x + width / 2.0
    tag = component.id
    fp = effective_footprint(device, component)
    if _is_operator_face(device) and strip_label is None:
        tx, ty = operator_tag_origin(component.x, component.y, width, height)
        painter.text(tx, ty, tag, min(2.2, max(1.5, height * 0.16)), align="start")
        return
    if strip_label is not None:
        # rotate -90 on both sides, so a reader turns the sheet once.
        painter.text(
            strip_label.x,
            strip_label.y,
            tag,
            strip_label.size,
            align="start",
            rotate=strip_label.rotate,
        )
        return
    tag_size = _fit_size(tag, inner, 2.4)
    screen = _screen_scale(painter)
    if screen is not None:
        # About 11 px, or skip the caption when that would swallow the footprint.
        floor = 11.0 / screen
        room = height * 0.45
        if floor > room:
            return
        tag_size = min(max(tag_size, floor), room)
    tag_draw = _clip_line(tag, inner, tag_size) or tag
    if caption_width(tag_draw, tag_size) > inner + 0.05:
        tag_size = _fit_size(tag_draw, inner, tag_size, min_size=0.7)
    y_tag = component.y + min(max(tag_size + 1.0, 3.2), height * 0.32)
    y_tag = min(y_tag, component.y + height - 1.4)
    placed = caption_baseline(
        y_tag, tag_size, clip_y=clip_y, body_top=component.y + 0.4, body_bot=component.y + height - 0.6
    )
    if placed is None and tag_size > 1.0:
        tag_size = max(0.9, tag_size * 0.75)
        placed = caption_baseline(
            y_tag, tag_size, clip_y=clip_y, body_top=component.y + 0.4, body_bot=component.y + height - 0.6
        )
    if placed is not None:
        painter.text(cx, placed, tag_draw, tag_size, align="middle")
        y_tag = placed

    size_text = ""
    if height >= 32:
        from panelcad.library.panel import is_layout_ready

        if is_layout_ready(device, component):
            size_text = f"{fp.width_mm:.0f}×{fp.height_mm:.0f}"
    y_size = component.y + height - 2.6
    size_h = 1.7 if size_text else 0.0

    y_cursor = y_tag + tag_size * 0.35
    body_top = component.y + 0.4
    body_bot = component.y + height - 0.6

    def _paint(line: str, size: float, y: float) -> float | None:
        fitted = _clip_line(line, inner, size)
        if not fitted:
            return y
        baseline = caption_baseline(y, size, clip_y=clip_y, body_top=body_top, body_bot=body_bot)
        if baseline is None:
            return None
        painter.text(cx, baseline, fitted, size, align="middle")
        return baseline

    placeholder = bool(component.properties.get("placeholder_footprint")) or (
        (component.properties.get("io_module") or component.properties.get("io_module_ref"))
        and str(getattr(fp, "dim_source", "") or "") != "catalog"
    )
    if placeholder and height >= 14 and inner >= 8:
        ph_size = _fit_size("PLACEHOLDER", inner, 2.2, min_size=1.6)
        y_ph = min(y_tag + tag_size + 1.8, component.y + height - ph_size - 1.2)
        painted = _paint("PLACEHOLDER", ph_size, y_ph)
        if painted is not None:
            y_cursor = max(y_cursor, painted + ph_size * 0.35)

    if wire_note and inner >= 4.0:
        note_lines, note_size = fit_footprint_caption(wire_note, inner, max(2.0, height * 0.2), size=1.5, min_size=1.1)
        for line in note_lines:
            y_cursor += note_size * 1.15
            if y_cursor > y_size - (size_h + 0.4):
                break
            painted = _paint(line, note_size, y_cursor)
            if painted is None:
                break
            y_cursor = painted

    xref = str(component.properties.get("xref_io") or "").strip()
    if xref and height >= 22 and inner >= 8.0:
        see = f"SEE {xref}"
        see_size = _fit_size(see, inner, 1.5)
        y_see = y_cursor + see_size * 1.15
        if y_see < (y_size - size_h - 0.4 if size_text else component.y + height - 1.2):
            painter.text(cx, y_see, see, see_size, align="middle")
            y_cursor = y_see
    cat = str(component.properties.get("catalog") or component.properties.get("tag") or device.name)
    # Terminals (~5 mm) stay tag-only. 22–27 mm operators/I/O wrap or shrink.
    room = (y_size - size_h - 0.6) - y_cursor if size_text else (component.y + height - 1.6) - y_cursor
    if cat and cat != tag and height >= 18 and inner >= 6.0 and room >= 2.0:
        lines, cap_size = fit_footprint_caption(
            cat, inner, room, size=1.8, min_size=1.15, max_lines=3 if height >= 28 else 2
        )
        for line in lines:
            y_cursor += cap_size * 1.2
            if y_cursor > (y_size - size_h - 0.3 if size_text else component.y + height - 1.2):
                break
            painted = _paint(line, cap_size, y_cursor)
            if painted is None:
                break
            y_cursor = painted

    if size_text:
        size_h_draw = _fit_size(size_text, inner, 1.7)
        painted = _paint(size_text, size_h_draw, y_size)
        if painted is None:
            _paint(size_text, size_h_draw, component.y + height - 1.2)


def _draw_panel_mount(
    painter: Painter,
    device: Device,
    component: Component,
    width: float,
    height: float,
) -> None:
    """Corner marks on a backplate device that does not clip to DIN rail."""

    fp = effective_footprint(device, component)
    if fp.din_rail or fp.mount != "backplate" or width < 8 or height < 8:
        return
    inset = min(2.2, width * 0.12, height * 0.08)
    radius = min(0.9, width * 0.06, height * 0.05)
    painter.set_stroke("#1b1b1b", 0.35)
    painter.set_fill(None)
    for cx, cy in (
        (component.x + inset, component.y + inset),
        (component.x + width - inset, component.y + inset),
        (component.x + inset, component.y + height - inset),
        (component.x + width - inset, component.y + height - inset),
    ):
        painter.circle(cx, cy, max(0.35, radius))
    if height >= 16 and width >= 18:
        note = "panel mount"
        painter.set_layer("TEXT")
        painter.text(
            component.x + width / 2.0,
            component.y + height - 5.0,
            note,
            _fit_size(note, max(4.0, width - 2.0), 1.6),
            align="middle",
        )
        painter.set_layer("DEVICES")


def _draw_motor_heading(painter: Painter, component: Component, width: float, height: float) -> None:
    heading = str(component.properties.get("layout_heading") or "").strip()
    if not heading:
        return
    _hx, hy, _hw, hh = heading_rect(component.x, component.y, width, height, heading)
    painter.set_layer("TEXT")
    painter.text(
        component.x + width / 2.0,
        hy + hh * 0.78,
        heading,
        max(1.7, min(2.6, hh * 0.78)),
        align="middle",
    )


def _legend_lines(legend: str, plate_w: float, plate_h: float) -> tuple[list[str], float]:
    """Legend copy with a margin inside the plate. Long words shrink before they touch the edge."""

    margin = max(1.3, min(2.2, plate_w * 0.08))
    limit = max(1.0, plate_w - 2.0 * margin)
    size = legend_text_size(plate_h * (22.0 / 10.0))
    parts = [part for part in legend.split("-") if part]
    chunks = [legend]
    if len(parts) >= 2 and plate_h >= size * 2.1:
        chunks = ["-".join(parts[:-1]), parts[-1]] if len(parts) >= 3 else parts
    while size > 1.05 and any(caption_width(line, size) > limit for line in chunks):
        size -= 0.05
    if len(chunks) > 1 and all(caption_width(line, size) <= limit + 0.05 for line in chunks):
        return chunks, size
    line = _clip_line(legend, limit, size) or legend
    return [line], size


def _draw_legend_plate(painter: Painter, component: Component, width: float, height: float) -> None:
    """Legend plate under a door operator, using the device function text."""

    legend = str(component.properties.get("legend") or "").strip()
    if not legend:
        return
    px, py, plate_w, plate_h = legend_plate_rect(component.x, component.y, width, height, legend)
    role = str(component.properties.get("role") or "")
    estop = role == "estop" or legend.upper().replace(" ", "") in {"E-STOP", "ESTOP"}
    painter.set_layer("DEVICES")
    painter.set_fill("#f6d326" if estop else "#f7f4ea")
    painter.set_stroke("#1b1b1b", 0.3)
    painter.rect(px, py, plate_w, plate_h)
    painter.set_fill(None)
    painter.set_layer("TEXT")
    lines, size = _legend_lines(legend, plate_w, plate_h)
    cx = component.x + width / 2.0
    if len(lines) == 1:
        painter.text(cx, py + plate_h * 0.72, lines[0], size, align="middle")
        return
    painter.text(cx, py + plate_h * 0.40, lines[0], size, align="middle")
    painter.text(cx, py + plate_h * 0.82, lines[1], size, align="middle")


def draw_panel_sheet(
    painter: Painter,
    sheet: Sheet,
    catalog: Catalog,
    *,
    base_dir: Path | None = None,
    project=None,
) -> None:
    mm_scale = 1.0
    if sheet.enclosure is not None:
        draw_enclosure(painter, sheet.enclosure)
        if sheet.enclosure.draw_scale and sheet.enclosure.draw_scale > 0:
            mm_scale = float(sheet.enclosure.draw_scale)
    labels = plan_terminal_labels(sheet, catalog)
    rail_avoid = layout_label_avoid(sheet, catalog, "rail")
    tray_avoid = layout_label_avoid(sheet, catalog, "tray")
    for tray in sheet.trays:
        draw_tray(painter, tray, mm_scale=mm_scale, avoid=tray_avoid)
    for rail in sheet.rails:
        draw_rail(painter, rail, mm_scale=mm_scale, avoid=rail_avoid)
    for comp in sheet.components:
        device = catalog.get(comp.library_id)
        note = footprint_wire_note(project, comp.id) if project is not None else ""
        _draw_keepout(painter, device, comp)
        draw_panel_device(
            painter,
            device,
            comp,
            base_dir=base_dir,
            wire_note=note,
            strip_label=labels.get(comp.id),
        )
    from panelcad.render.panel_wiring_render import draw_panel_wiring_overlay

    draw_panel_wiring_overlay(painter, sheet, catalog, project)


def _draw_keepout(painter: Painter, device: Device, component: Component) -> None:
    """Installation clearance around a backplate device. Values are true millimetres."""

    props = component.properties
    try:
        left = float(props.get("keepout_left_mm") or 0)
        right = float(props.get("keepout_right_mm") or 0)
        top = float(props.get("keepout_top_mm") or 0)
        bottom = float(props.get("keepout_bottom_mm") or 0)
    except (TypeError, ValueError):
        return
    if max(left, right, top, bottom) <= 0:
        return
    w, h = display_footprint_size(device, component)
    if w <= 0 or h <= 0:
        return
    scale = component.visual_scale()
    x = component.x - left * scale
    y = component.y - top * scale
    painter.set_layer("OUTLINE")
    painter.set_fill(None)
    painter.set_stroke("#1f6f8b", 0.35)
    painter.rect(x, y, w + (left + right) * scale, h + (top + bottom) * scale)
    note = str(props.get("keepout_note") or "").strip()
    if note:
        painter.set_layer("TEXT")
        painter.text(x + 1.2, y + min(4.0, top * scale * 0.45), note, 2.0, align="start")


def _draw_needs_info(painter: Painter, component: Component, width: float, height: float) -> None:
    """Hatched INFO NEEDED box. Never looks like a catalog part."""

    painter.set_layer("DEVICES")
    painter.set_fill("#ffe3b3")
    painter.set_stroke("#c45c00", 0.7)
    painter.rect(component.x, component.y, width, height)
    painter.set_fill(None)
    painter.set_stroke("#c45c00", 0.28)
    step = max(3.0, min(width, height) / 5.0)
    x0, y0 = component.x, component.y
    offset = 0.0
    while offset < width + height:
        x1 = x0 + max(0.0, offset - height)
        y1 = y0 + min(height, offset)
        x2 = x0 + min(width, offset)
        y2 = y0 + max(0.0, offset - width)
        painter.line(x1, y1, x2, y2)
        offset += step
    painter.set_layer("TEXT")
    painter.set_stroke("#8a3b00", 0.2)
    props = component.properties or {}
    label = str(props.get("marker_label") or props.get("part_number") or props.get("label_text") or "").strip()
    headline = label or str(component.id or "")
    painter.text(component.x + width / 2.0, component.y + height * 0.38, headline, min(3.4, height * 0.22), align="middle")
    painter.text(component.x + width / 2.0, component.y + height * 0.64, "INFO NEEDED", min(3.0, height * 0.2), align="middle")
