"""Schematic ink bounds: symbol body plus tags, pin dots, and terminal labels.

Matches ``draw_device`` / ``draw_sheet_body`` so a fit that stays inside
``working_rect`` also keeps the printed tag and line stubs out of the zone
strip and title block. Metrics are conservative Arial/DejaVu estimates — no Qt.
"""

from __future__ import annotations

from typing import Any

from panelcad.core.geometry import rotate_point
from panelcad.core.models import Component, Device
from panelcad.core.pins import extra_captions, pin_caption
from panelcad.core.route import Rect, component_aabb

# contents.py pin dots; painter.py tag at (x, y - 2.5) size 3.0.
PIN_DOT_R = 0.7
TAG_DY = 2.5
TAG_SIZE = 3.0
# Ascent / descent / advance as multiples of font size (mm).
TEXT_ASCENT = 0.90
TEXT_DESCENT = 0.25
TEXT_ADVANCE = 0.62


def text_box(x: float, y: float, text: str, size: float, align: str = "start") -> Rect | None:
    """Axis-aligned box of a baseline-at-``y`` label."""

    if not text:
        return None
    size = max(0.9, float(size))
    width = size * TEXT_ADVANCE * max(1, len(text))
    top = y - size * TEXT_ASCENT
    bottom = y + size * TEXT_DESCENT
    if align == "middle":
        left, right = x - width * 0.5, x + width * 0.5
    elif align == "end":
        left, right = x - width, x
    else:
        left, right = x, x + width
    return (left, top, right, bottom)


def stacked_power_tuck(
    side: str,
    wx: float,
    wy: float,
    pins: list[tuple[float, float]],
) -> bool:
    """True when the next terminal on this drop is only a few millimetres away.

    Q1's load numbers and K1's line numbers share a 2 mm gap. Each caption
    then has to sit on its own device, or 2/4/6 read as K1's upper terminals.
    """

    if side == "top":
        return any(abs(px - wx) <= 1.6 and 0.4 < wy - py <= 7.2 for px, py in pins)
    if side == "bottom":
        return any(abs(px - wx) <= 1.6 and 0.4 < py - wy <= 7.2 for px, py in pins)
    return False


def pin_label_anchor(
    wx: float,
    wy: float,
    side: str,
    *,
    ladder: bool = False,
    column: bool = False,
    caption: str = "",
    tuck: bool = False,
) -> tuple[float, float, str]:
    """Baseline (x, y, align) for a pin caption. Matches the sheet painter.

    Power sheets keep the caption off the wire. Where two symbols share a
    drop, ``tuck`` puts the upper device's load number above its pin and the
    lower device's line number below its pin. Column numbers sit off the rung.
    Ladder sheets keep the coordinates they already draw.
    """

    # Where two symbols share a drop, one number sits to the right of the
    # wire and the other sits to the left. Both on one side print on top of each other.
    if column and side == "top":
        return (wx + 1.8, wy - 1.5, "start")
    if column and side == "bottom":
        # Below the rung. A baseline 1.6 mm under the pin still covers the wire.
        return (wx - 2.2, wy + 2.5, "end")
    if side == "left":
        if ladder:
            return (wx - 2.2, wy + 0.8, "end")
        if column:
            return (wx - 0.3, wy - 2.2, "end")
        return (wx + 0.4, wy - 2.4, "start")
    if side == "right":
        if ladder:
            return (wx + 7.0, wy - 1.4, "start")
        if column:
            return (wx + 0.4, wy - 2.2, "start")
        # 95–98 sit on the overload blades when the caption is left of the pin.
        # Other right-hand numbers stay above the pin: a skirt runs past 13/14.
        if caption in {"95", "96", "97", "98"}:
            return (wx + 1.2, wy - 2.2, "start")
        return (wx - 0.4, wy - 2.4, "end")
    if side == "top":
        if ladder or column:
            return (wx, wy - 2.6, "middle")
        # Motor leads arrive on these pins. A caption above the pin sits on
        # that feeder and on the load numbers of the device stacked above.
        if caption in {"U1", "V1", "W1", "T1", "T2", "T3"}:
            return (wx + 2.4, wy + 4.2, "start")
        # Right of the drop. A stacked device above this pin: the line number
        # belongs below the terminal, on this symbol, not in the shared gap.
        if tuck:
            return (wx + 1.5, wy + 2.45, "start")
        return (wx + 1.6, wy - 1.1, "start")
    if ladder or column:
        return (wx, wy + 3.6, "middle")
    # Beside the pin, not under the load wire. A baseline 1.6 mm below the
    # pin put T1/2/4/6 under the hop and the junction dot. A stacked device
    # below this pin: the load number belongs above the terminal.
    if tuck:
        return (wx - 1.5, wy - 1.85, "end")
    return (wx - 2.6, wy + 0.4, "end")


def schematic_ink_rect(
    device: Device,
    x: float,
    y: float,
    ref: str = "",
    *,
    component: Component | None = None,
    rotation: int = 0,
    mirror: bool = False,
    scale: float = 1.0,
) -> Rect:
    """World AABB of everything the schematic painter inks for one symbol."""

    if component is not None:
        x = float(component.x)
        y = float(component.y)
        ref = component.id
        rotation = int(component.rotation or 0)
        mirror = bool(component.mirror)
        scale = component.visual_scale()
    s = scale if scale > 0 else 1.0
    if component is not None and (device.category or "").lower().startswith("connection.notes"):
        # Notes draw a fitted box and no device tag. The library 28×10 plus the tag
        # band would both miss the real text and stick into the sheet border.
        from panelcad.render.painter import notes_box

        width, height = notes_box(component, s)
        return (x, y, x + width, y + height)
    width = float(device.schematic_width) * s
    height = float(device.schematic_height) * s
    if component is not None and component.properties.get("card_h"):
        height = max(height, float(component.properties["card_h"]) * s)
    if component is not None and component.properties.get("card_w"):
        width = max(width, float(component.properties["card_w"]) * s)

    xs: list[float] = []
    ys: list[float] = []

    def include(rect: Rect | None) -> None:
        if rect is None:
            return
        xs.extend((rect[0], rect[2]))
        ys.extend((rect[1], rect[3]))

    include(component_aabb(x, y, width, height, rotation, mirror))
    _include_tag(device, x, y, ref, s, rotation, component, include)
    if component is not None:
        y_cap = y - 5.6 * s
        for line in extra_captions(component):
            include(text_box(x, y_cap, line, 2.2, "start"))
            y_cap -= 3.1 * s
        from panelcad.core.pins import rating_caption_positions

        for cx, cy, line in rating_caption_positions(component, height / s if s else height, s):
            include(text_box(cx, cy, line, 2.2, "end"))
    _include_pins(device, x, y, s, rotation, mirror, component, include)
    if not xs:
        return (x, y, x + width, y + height)
    return (min(xs), min(ys), max(xs), max(ys))


def device_ref_layout(
    device: Device,
    component: Component,
    *,
    ref_align: str = "start",
) -> tuple[float, float, str, float] | None:
    """External device tag (x, y, align, size). None when the ref is in-coil or a block attribute."""

    from panelcad.core.block import has_tag_attribute
    from panelcad.core.ladder import is_aux_library

    if component.properties.get("hide_tag"):
        return None
    if has_tag_attribute(device):
        return None
    cat = (device.category or "").lower()
    token = f"{getattr(device, 'id', '')} {component.library_id}".lower()
    if "switching" in cat and "coil" in token and "aux" not in token:
        return None
    if "timer" in cat and int(component.rotation or 0) in {90, 270}:
        return None
    s = component.visual_scale()
    if int(component.rotation or 0):
        from panelcad.core.route import component_aabb

        w = float(device.schematic_width) * s
        h = float(device.schematic_height) * s
        box = component_aabb(component.x, component.y, w, h, int(component.rotation), bool(component.mirror))
        raw_dx = component.properties.get("ref_dx")
        raw_dy = component.properties.get("ref_dy")
        dx = float(raw_dx) if raw_dx not in (None, "") else 0.0
        dy = float(raw_dy) if raw_dy not in (None, "") else 0.0
        side = str(component.properties.get("ref_side") or "")
        # A seal's default tag sits on the button it parallels. A second lamp's
        # tag sits on the lamp to its left. Callers pick the clear side.
        if side == "right":
            return (box[2] + 1.4 + dx, (box[1] + box[3]) * 0.5 + dy, "start", 2.2)
        if side == "below":
            return ((box[0] + box[2]) * 0.5 + dx, box[3] + 3.4 + dy, "middle", 2.2)
        # Left of the symbol. Above sits on the line drop; the right side sits on
        # the next column's return. The rung enters on the right of a 90° contact.
        return (box[0] - 1.2 + dx, (box[1] + box[3]) * 0.5 + dy, "end", 2.2)
    if is_aux_library(component.library_id):
        return (
            component.x + float(device.schematic_width) * s * 0.5,
            component.y + float(device.schematic_height) * s + 3.2 * s,
            "middle",
            2.6,
        )
    rx = component.x
    align = ref_align if ref_align in {"start", "middle", "end"} else "start"
    if align == "middle":
        rx = component.x + float(device.schematic_width) * s * 0.5
    elif align == "end":
        rx = component.x + float(device.schematic_width) * s
    dx = dy = 0.0
    raw_dx = component.properties.get("ref_dx")
    raw_dy = component.properties.get("ref_dy")
    if raw_dx not in (None, ""):
        dx = float(raw_dx)
    if raw_dy not in (None, ""):
        dy = float(raw_dy)
    return (rx + dx, component.y - TAG_DY * s + dy, align, TAG_SIZE)


def _include_tag(
    device: Device,
    x: float,
    y: float,
    ref: str,
    scale: float,
    rotation: int,
    component: Component | None,
    include: Any,
) -> None:
    from panelcad.core.block import attribute_value, has_tag_attribute
    from panelcad.core.ladder import is_aux_library

    if has_tag_attribute(device):
        for attr in device.attributes:
            text = attribute_value(attr.tag, device, component)
            if not text:
                continue
            rx, ry = rotate_point(attr.x * scale, attr.y * scale, rotation)
            include(text_box(x + rx, y + ry, text, max(1.6, attr.h * scale), "start"))
        return
    if component is not None and (
        component.properties.get("ladder_card") or component.properties.get("ladder_coil")
    ):
        return
    if component is not None and component.properties.get("tag_below"):
        include(
            text_box(
                x + float(device.schematic_width) * scale * 0.5,
                y + float(device.schematic_height) * scale + 3.2 * scale,
                component.id,
                2.6,
                "middle",
            )
        )
        return
    if component is not None:
        layout = device_ref_layout(device, component)
        if layout is None:
            return
        tx, ty, align, size = layout
        include(text_box(tx, ty, component.id, size, align))
        return
    label = ref or "MTR1"
    below = is_aux_library(device.library_id) or (
        component is not None and component.properties.get("tag_below")
    )
    if below:
        include(
            text_box(
                x + float(device.schematic_width) * scale * 0.5,
                y + float(device.schematic_height) * scale + 3.2 * scale,
                label,
                2.6,
                "middle",
            )
        )
        return
    include(text_box(x, y - TAG_DY * scale, label, TAG_SIZE, "start"))


def _include_pins(
    device: Device,
    x: float,
    y: float,
    scale: float,
    rotation: int,
    mirror: bool,
    component: Component | None,
    include: Any,
) -> None:
    for pin in device.pins:
        if component is not None:
            from panelcad.render.sheet import pin_side_world, pin_world

            wx, wy = pin_world(component, pin)
            side = pin_side_world(component, pin)
        else:
            px, py = pin.x * scale, pin.y * scale
            if mirror:
                px = -px
            rx, ry = rotate_point(px, py, rotation)
            wx, wy = x + rx, y + ry
            side = pin.side
        include((wx - PIN_DOT_R, wy - PIN_DOT_R, wx + PIN_DOT_R, wy + PIN_DOT_R))
        cap = pin_caption(pin, component)
        if not cap or cap == pin.id:
            continue
        inside = component is not None and (
            component.properties.get("ladder_card") or component.properties.get("ladder_coil")
        )
        if component is not None and component.rotation and side in {"top", "bottom"}:
            include(text_box(wx - 2.8, wy + 0.7, cap, 1.9, "end"))
            continue
        if inside and side == "left":
            include(text_box(wx + 7.5, wy + 0.7, cap, 2.1, "start"))
            continue
        if inside and side == "right":
            include(text_box(wx - 7.5, wy + 0.7, cap, 2.1, "end"))
            continue
        if side == "left":
            include(text_box(wx - 2.2, wy + 0.8, cap, 2.0, "end"))
        elif side == "right":
            include(text_box(wx + 2.2, wy + 0.8, cap, 2.0, "start"))
        elif side == "top":
            include(text_box(wx, wy - 2.6, cap, 2.0, "middle"))
        else:
            include(text_box(wx, wy + 3.6, cap, 2.0, "middle"))
