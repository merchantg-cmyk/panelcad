from __future__ import annotations

from abc import ABC, abstractmethod

from panelcad.core.geometry import rotate_point
from panelcad.core.models import Component, Device, Wire
from panelcad.core.primitives import Primitive


class Painter(ABC):
    @abstractmethod
    def set_stroke(self, color: str, width: float) -> None: ...

    @abstractmethod
    def set_fill(self, color: str | None) -> None: ...

    @abstractmethod
    def rect(self, x: float, y: float, w: float, h: float) -> None: ...

    @abstractmethod
    def line(self, x1: float, y1: float, x2: float, y2: float) -> None: ...

    @abstractmethod
    def circle(self, x: float, y: float, r: float) -> None: ...

    @abstractmethod
    def polyline(self, points: list[tuple[float, float]]) -> None: ...

    def polygon(self, points: list[tuple[float, float]]) -> None:
        """Closed filled outline. Default: stroke only via polyline."""
        if points:
            self.polyline(list(points) + [points[0]])

    @abstractmethod
    def text(
        self,
        x: float,
        y: float,
        text: str,
        size: float,
        align: str = "middle",
        rotate: float = 0,
    ) -> None: ...

    def image(self, path: str, x: float, y: float, w: float, h: float) -> None:
        """Draw an image fitted into the rect. Painters that cannot skip."""
        return

    def set_layer(self, name: str) -> None:
        """CAD layer for the next primitives. SVG/PDF painters ignore this."""
        return

    def layout_is_true_scale(self) -> bool:
        """True when layout geometry is written 1:1 (DXF), not page-scaled."""
        return False


def draw_primitive(
    painter: Painter, prim: Primitive, ox: float, oy: float, rotation: int = 0, scale: float = 1.0
) -> None:
    s = scale if scale > 0 else 1.0

    def xf(x: float, y: float) -> tuple[float, float]:
        rx, ry = rotate_point(x * s, y * s, rotation)
        return (ox + rx, oy + ry)

    if prim.type == "rect":
        pts = [
            xf(prim.x, prim.y),
            xf(prim.x + prim.w, prim.y),
            xf(prim.x + prim.w, prim.y + prim.h),
            xf(prim.x, prim.y + prim.h),
        ]
        painter.set_layer("DEVICES")
        painter.set_fill("#222222" if prim.fill else "#f7f4ea")
        painter.set_stroke("#111111", 0.35)
        painter.polygon(pts)
        painter.set_fill(None)
    elif prim.type == "line":
        a = xf(prim.x, prim.y)
        b = xf(prim.x2, prim.y2)
        painter.line(a[0], a[1], b[0], b[1])
    elif prim.type == "circle":
        c = xf(prim.x, prim.y)
        # A junction declares fill. Other circles stay the paper colour.
        painter.set_fill("#222222" if prim.fill else "#f7f4ea")
        painter.circle(c[0], c[1], prim.r * s)
        painter.set_fill(None)
    elif prim.type == "polyline":
        painter.polyline([xf(x, y) for x, y in prim.points])
    elif prim.type == "text":
        p = xf(prim.x, prim.y)
        painter.set_layer("TEXT")
        painter.text(p[0], p[1], prim.text, prim.size * s)
        painter.set_layer("DEVICES")


def note_lines(raw: str, width: float, size: float = 2.6) -> list[str]:
    """Wrap note text so each line fits the box. Advance matches schematic_ink text metrics."""

    usable = max(8.0, float(width) - 8.0)
    advance = max(float(size) * 0.62, 0.8)
    lines: list[str] = []
    for para in str(raw or "").replace("\r", "").split("\n"):
        words = para.split()
        if not words:
            lines.append("")
            continue
        line = ""
        for word in words:
            trial = word if not line else f"{line} {word}"
            if len(trial) * advance <= usable or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines or [""]


def notes_box(component: Component, scale: float = 1.0) -> tuple[float, float]:
    """Fitted notes rectangle from the text, not the 372×196 library default."""
    s = scale if scale > 0 else 1.0
    raw = str(component.properties.get("text") or component.properties.get("tag") or "")
    width = float(component.properties.get("width") or 360.0) * s
    size = 2.6 * s
    if not raw.strip():
        return (width, 16.0 * s)
    n = max(1, len(note_lines(raw, width, size)))
    h = (10.0 + n * (2.6 + 1.8) + 4.0) * s
    return (width, h)


def is_control_coil(device: Device, library_id: str = "") -> bool:
    cat = (device.category or "").lower()
    token = f"{getattr(device, 'id', '')} {library_id}".lower()
    if "aux" in token:
        return False
    return "switching" in cat and "coil" in token


def draw_control_coil(
    painter: Painter,
    device: Device,
    component: Component,
    coil_style: str = "circle",
) -> bool:
    """Circle or square coil on the rung. Not a 3-phase motor. Returns True if drawn."""

    if not is_control_coil(device, component.library_id):
        return False
    if int(component.rotation or 0) in {90, 270}:
        return _draw_vertical_coil(painter, device, component)
    s = component.visual_scale()
    w = device.schematic_width * s
    pin_y = 14.0 * s
    for pin in device.pins:
        if str(pin.id).upper() == "A1":
            pin_y = pin.y * s
            break
    cx = component.x + w * 0.5
    cy = component.y + pin_y
    half = min(w, device.schematic_height * s) * 0.32
    painter.set_layer("DEVICES")
    painter.set_stroke("#111111", 0.5)
    painter.set_fill("#f7f4ea")
    if coil_style == "square":
        painter.rect(cx - half, cy - half, half * 2.0, half * 2.0)
    else:
        painter.circle(cx, cy, half)
    painter.set_layer("TEXT")
    painter.set_fill("#111111")
    painter.text(cx, cy + 1.1 * s, component.id, 3.2, align="middle")
    painter.set_fill(None)
    painter.line(component.x, cy, cx - half, cy)
    painter.line(cx + half, cy, component.x + w, cy)
    return True


def _draw_ladder_card(painter: Painter, device: Device, component: Component) -> None:
    """PLC box: pins and names on the edge. No conductor is drawn across the body."""

    from panelcad.core.pins import pin_caption
    from panelcad.render.sheet import pin_world

    s = component.visual_scale()
    w = float(component.properties.get("card_w") or device.schematic_width) * s
    h = float(component.properties.get("card_h") or device.schematic_height) * s
    x, y = component.x, component.y
    painter.set_layer("DEVICES")
    painter.set_stroke("#1b1b1b", 0.5)
    painter.set_fill("#fffdf8")
    painter.rect(x, y, w, h)
    painter.set_fill("#efe6c8")
    painter.rect(x, y, w, 11.0 * s)
    painter.set_fill(None)
    painter.set_layer("TEXT")
    painter.set_fill("#111111")
    title = str(component.properties.get("shown_tag") or component.id)
    painter.text(x + w / 2.0, y + 7.2 * s, title, 2.8, align="middle")
    xref = str(component.properties.get("xref_rack") or "").strip()
    if xref:
        painter.text(x + w / 2.0, y + 14.6 * s, f"SEE {xref}", 1.8, align="middle")
    painter.set_fill(None)
    painter.set_stroke("#1b1b1b", 0.35)
    io_map = component.properties.get("io") if isinstance(component.properties.get("io"), dict) else {}
    limit = bool(component.properties.get("io_pin_limit"))
    mapped = set(component.pin_overrides)
    for pin in device.pins:
        if limit and pin.id not in mapped:
            continue
        if not limit and mapped and pin.id not in mapped:
            continue
        wx, wy = pin_world(component, pin)
        side = component.pin_side(pin) or pin.side
        cap = pin_caption(pin, component)
        meta = io_map.get(pin.id) if isinstance(io_map.get(pin.id), dict) else {}
        desc = str(meta.get("description") or "").strip()
        if desc.lower() == "spare":
            desc = ""
        desc = desc[:22]
        painter.set_layer("DEVICES")
        if side == "left":
            painter.line(wx, wy, wx + 6.0 * s, wy)
            painter.set_layer("TEXT")
            painter.set_fill("#111111")
            painter.text(wx + 7.4 * s, wy - 0.2 * s, cap, 2.1, align="start")
            if desc:
                painter.text(wx + 7.4 * s, wy + 2.6 * s, desc, 1.7, align="start")
        elif side == "right":
            painter.line(wx - 6.0 * s, wy, wx, wy)
            painter.set_layer("TEXT")
            painter.set_fill("#111111")
            painter.text(wx - 7.4 * s, wy - 0.2 * s, cap, 2.1, align="end")
            if desc:
                painter.text(wx - 7.4 * s, wy + 2.6 * s, desc, 1.7, align="end")
        elif side == "top":
            painter.line(wx, wy, wx, wy + 6.0 * s)
            painter.set_layer("TEXT")
            painter.set_fill("#111111")
            # Beside the stub, so the caption is not drawn on the feed.
            painter.text(wx + 2.4 * s, wy + 3.2 * s, cap, 2.0, align="start")
        else:
            painter.line(wx, wy - 6.0 * s, wx, wy)
            painter.set_layer("TEXT")
            painter.set_fill("#111111")
            painter.text(wx + 2.4 * s, wy - 3.0 * s, cap, 2.0, align="start")
        painter.set_fill(None)


def _draw_vertical_coil(painter: Painter, device: Device, component: Component) -> bool:
    """A1 on top, A2 on the bottom, for a coil standing in an IEC column."""

    from panelcad.render.sheet import pin_world

    s = component.visual_scale()
    pins = {p.id: p for p in device.pins}
    a1 = pins.get("A1") or next((p for p in device.pins if p.side == "left"), device.pins[0])
    a2 = pins.get("A2") or next((p for p in device.pins if p.side == "right"), device.pins[-1])
    x1, y1 = pin_world(component, a1)
    x2, y2 = pin_world(component, a2)
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    span = max(abs(y2 - y1), abs(x2 - x1), 8.0 * s)
    r = min(span, device.schematic_height * s) * 0.28
    r = max(r, 5.0 * s)
    painter.set_layer("DEVICES")
    painter.set_stroke("#111111", 0.5)
    painter.set_fill("#f7f4ea")
    painter.circle(cx, cy, r)
    painter.set_fill(None)
    if y1 <= y2:
        painter.line(x1, y1, cx, cy - r)
        painter.line(cx, cy + r, x2, y2)
    else:
        painter.line(x1, y1, cx, cy + r)
        painter.line(cx, cy - r, x2, y2)
    painter.set_layer("TEXT")
    painter.set_fill("#111111")
    painter.text(cx, cy + 1.0 * s, component.id, 3.0, align="middle")
    label = str(component.properties.get("function_label") or "").strip()
    if label:
        painter.text(cx, cy + r + 3.4 * s, label, 2.0, align="middle")
    painter.set_fill(None)
    return True


def _draw_on_delay_coil(painter: Painter, device: Device, component: Component) -> bool:
    """IEC on-delay coil: circle, tag, and the delayed-operating mark. Not a boxed K."""

    if not _draw_vertical_coil(painter, device, component):
        return False
    from panelcad.render.sheet import pin_world

    s = component.visual_scale()
    pins = {p.id: p for p in device.pins}
    a1 = pins.get("A1") or device.pins[0]
    a2 = pins.get("A2") or device.pins[-1]
    x1, y1 = pin_world(component, a1)
    x2, y2 = pin_world(component, a2)
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    span = max(abs(y2 - y1), 8.0 * s)
    r = max(min(span, device.schematic_height * s) * 0.28, 5.0 * s)
    # IEC delayed operation: a small box with the on-delay diagonal, beside the coil.
    bx, by = cx + r + 1.4 * s, cy - 3.0 * s
    painter.set_layer("DEVICES")
    painter.set_stroke("#111111", 0.35)
    painter.set_fill(None)
    painter.rect(bx, by, 6.2 * s, 6.0 * s)
    painter.line(bx, by + 6.0 * s, bx + 6.2 * s, by)
    return True


def _draw_column_selector(painter: Painter, device: Device, component: Component) -> bool:
    """Hand and Auto contacts of a 3-position selector standing in a column."""

    from panelcad.render.sheet import pin_world

    pins = {p.id: p for p in device.pins}
    common = pins.get("11") or pins.get("1")
    hand = pins.get("12") or pins.get("2")
    auto = pins.get("14") or pins.get("3")
    if common is None or hand is None:
        return False
    cx, cy = pin_world(component, common)
    hx, hy = pin_world(component, hand)
    painter.set_layer("DEVICES")
    painter.set_stroke("#111111", 0.45)
    painter.set_fill("#f7f4ea")
    painter.circle(cx, cy + 7.0, 3.2)
    painter.set_fill(None)
    painter.line(cx, cy, cx, cy + 4.0)
    mid_y = cy + 10.5
    painter.line(cx, cy + 10.0, cx, mid_y)
    painter.line(cx, mid_y, hx, hy)
    painter.line(hx - 2.2, hy - 2.4, hx + 2.2, hy + 2.4)
    painter.set_layer("TEXT")
    painter.set_fill("#111111")
    painter.text(min(cx, hx) - 1.6, hy - 1.2, "Hand", 2.1, align="end")
    if auto is not None:
        ax, ay = pin_world(component, auto)
        painter.set_stroke("#111111", 0.45)
        painter.set_fill(None)
        painter.line(cx, mid_y, ax, ay)
        painter.line(ax - 2.2, ay + 2.2, ax + 2.2, ay - 2.2)
        painter.set_fill("#111111")
        painter.text(min(cx, ax) - 1.6, ay + 3.4, "Auto", 2.1, align="end")
    painter.set_fill(None)
    return True


def _primitive_is_lower_pole(prim: Primitive) -> bool:
    """The second estop contact sits at local y >= 33. The operator is above that."""

    ys = [float(prim.y)]
    if prim.type == "line":
        ys.append(float(prim.y2))
    if prim.type == "rect":
        ys.append(float(prim.y) + float(prim.h))
    if prim.points:
        ys.extend(float(point[1]) for point in prim.points)
    return min(ys) >= 33.0


def _draw_ladder_coil(painter: Painter, device: Device, component: Component) -> None:
    """A1 on the left, A2 on the right. The return wire stops at A2."""

    from panelcad.render.sheet import pin_world

    s = component.visual_scale()
    pins = {p.id: p for p in device.pins}
    a1 = pins.get("A1") or next((p for p in device.pins if p.side == "left"), device.pins[0])
    a2 = pins.get("A2") or next((p for p in device.pins if p.side == "right"), device.pins[-1])
    x1, y1 = pin_world(component, a1)
    x2, y2 = pin_world(component, a2)
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    r = min(abs(x2 - x1), device.schematic_height * s) * 0.32
    r = max(r, 6.0 * s)
    painter.set_layer("DEVICES")
    painter.set_stroke("#111111", 0.5)
    painter.set_fill("#f7f4ea")
    painter.circle(cx, cy, r)
    painter.set_fill(None)
    vertical = abs(y2 - y1) > abs(x2 - x1) + 0.5
    if vertical:
        if y1 <= y2:
            painter.line(x1, y1, cx, cy - r)
            painter.line(cx, cy + r, x2, y2)
        else:
            painter.line(x1, y1, cx, cy + r)
            painter.line(cx, cy - r, x2, y2)
    else:
        painter.line(x1, y1, cx - r, cy)
        painter.line(cx + r, cy, x2, y2)
    painter.set_layer("TEXT")
    painter.set_fill("#111111")
    painter.text(cx, cy + 1.0 * s, component.id, 3.0, align="middle")
    if vertical:
        # Keep A1/A2 off the conductor that lands on the pin.
        painter.text(x1 + 1.4 * s, y1 - 2.4 * s, a1.id, 2.0, align="start")
        painter.text(x2 + 1.4 * s, y2 + 3.2 * s, a2.id, 2.0, align="start")
    else:
        painter.text(x1 + 1.6 * s, y1 - 1.5 * s, a1.id, 2.0, align="start")
        painter.text(x2 - 1.6 * s, y2 - 1.5 * s, a2.id, 2.0, align="end")
    painter.set_fill(None)


def draw_device(
    painter: Painter,
    device: Device,
    component: Component,
    *,
    coil_style: str = "circle",
    captions: bool = True,
    ref_align: str = "start",
    tag_at: tuple[float, float, str, float] | None = None,
    tag_text: str | None = None,
    location_text: str = "",
) -> None:
    painter.set_stroke("#111111", 0.35)
    painter.set_fill(None)
    painter.set_layer("DEVICES")
    s = component.visual_scale()
    cat = device.category.lower()
    if component.properties.get("ladder_card"):
        _draw_ladder_card(painter, device, component)
        return
    if component.properties.get("ladder_coil"):
        _draw_ladder_coil(painter, device, component)
        return
    if cat.startswith("connection.notes"):
        w, h = notes_box(component, s)
        painter.set_stroke("#8a8678", 0.25)
        painter.set_fill("#fbfaf3")
        painter.rect(component.x, component.y, w, h)
        painter.set_fill(None)
        _draw_notes(painter, component, s)
        return
    if device.block_kind:
        for prim in device.primitives:
            if not isinstance(prim, Primitive):
                prim = Primitive.model_validate(prim)
            draw_primitive(painter, prim, component.x, component.y, component.rotation, scale=s)
    elif draw_control_coil(painter, device, component, coil_style):
        from panelcad.core.pins import extra_captions

        if not captions:
            return
        y_cap = component.y - 5.6 * s
        for line in extra_captions(component):
            painter.text(component.x, y_cap, line, 2.2)
            y_cap -= 3.1 * s
        return
    elif "timer" in cat and int(component.rotation or 0) in {90, 270} and _draw_on_delay_coil(
        painter, device, component
    ):
        pass
    elif "selector" in cat and int(component.rotation or 0) in {90, 270} and _draw_column_selector(
        painter, device, component
    ):
        pass
    else:
        hidden = {str(pin_id) for pin_id in (component.properties.get("hidden_pins") or [])}
        lamp = "light" in cat or "pilot" in (device.id or "").lower()
        for prim in device.primitives:
            if not isinstance(prim, Primitive):
                prim = Primitive.model_validate(prim)
            if hidden and _primitive_is_lower_pole(prim):
                continue
            if lamp and component.rotation and prim.type == "text" and str(prim.text).strip() == "H":
                continue
            draw_primitive(painter, prim, component.x, component.y, component.rotation, scale=s)
        _draw_rating_lines(painter, device, component, s)
    from panelcad.core.block import has_tag_attribute
    from panelcad.core.ink import device_ref_layout
    from panelcad.core.ladder import is_aux_library

    painter.set_layer("TEXT")
    shown = tag_text or component.id
    tag_layout: tuple[float, float, str, float] | None = None
    hide_tag = bool(component.properties.get("hide_tag")) or cat.startswith("connection.node")
    if hide_tag:
        tag_layout = None
        from panelcad.core.pins import io_terminal_caption

        caption = io_terminal_caption(component, device.schematic_width, device.schematic_height, s)
        if caption is not None:
            cx, cy, calign, csize, line = caption
            painter.set_fill("#111111")
            painter.text(cx, cy, line, csize, align=calign)
            painter.set_fill(None)
    elif has_tag_attribute(device):
        _draw_block_attributes(painter, device, component, s)
    elif component.rotation:
        layout = device_ref_layout(device, component, ref_align=ref_align)
        if layout is not None:
            rx, ry, align, size = layout
            painter.text(rx, ry, shown, size, align=align)
    elif is_aux_library(component.library_id) or component.properties.get("tag_below"):
        tag_layout = (
            component.x + device.schematic_width * s * 0.5,
            component.y + device.schematic_height * s + 3.2 * s,
            "middle",
            2.6,
        )
        painter.text(tag_layout[0], tag_layout[1], shown, tag_layout[3], align="middle")
    else:
        tag_layout = tag_at if tag_at is not None else device_ref_layout(device, component, ref_align=ref_align)
        if tag_layout is not None:
            rx, ry, align, size = tag_layout
            painter.text(rx, ry, shown, size, align=align)
    if location_text:
        painter.set_fill("#111111")
        painter.text(component.x, component.y - 8.0 * s, location_text, 2.0)
        painter.set_fill(None)
    if not captions:
        return
    label = str(component.properties.get("function_label") or "").strip()
    if label and not component.rotation and not component.properties.get("ladder_coil"):
        painter.set_fill("#111111")
        painter.text(component.x, component.y - 9.0 * s, label, 2.1)
        painter.set_fill(None)
    from panelcad.render.sheet_labels import caption_layouts

    for cx, cy, calign, csize, line in caption_layouts(component, tag_layout):
        painter.text(cx, cy, line, csize, align=calign)
    from panelcad.core.pins import rating_caption_positions

    for cx, cy, line in rating_caption_positions(
        component, device.schematic_height, s, device.schematic_width
    ):
        painter.text(cx, cy, line, 2.2, align="start")


def _draw_block_attributes(painter: Painter, device: Device, component: Component, scale: float) -> None:
    from panelcad.core.block import attribute_value

    s = scale if scale > 0 else 1.0
    rotation = int(component.rotation or 0)

    def xf(x: float, y: float) -> tuple[float, float]:
        rx, ry = rotate_point(x * s, y * s, rotation)
        return (component.x + rx, component.y + ry)

    for attr in device.attributes:
        text = attribute_value(attr.tag, device, component)
        if not text:
            continue
        x, y = xf(attr.x, attr.y)
        painter.text(x, y, text, max(1.6, attr.h * s))


def _draw_rating_lines(painter: Painter, device: Device, component: Component, s: float) -> None:
    """Instance HD/ND text inside a drive symbol. The library draw stays generic."""

    raw = str(component.properties.get("rating_lines") or "").strip()
    if not raw:
        return
    painter.set_layer("TEXT")
    painter.set_fill("#111111")
    cx = component.x + float(device.schematic_width) * s * 0.50
    cy = component.y + float(device.schematic_height) * s * 0.34
    for i, line in enumerate(raw.splitlines()[:3]):
        text = line.strip()
        if not text:
            continue
        painter.text(cx, cy + i * 3.6 * s, text, max(1.6, 2.0 * s), align="middle")
    painter.set_fill(None)


def _draw_notes(painter: Painter, component: Component, s: float, ox: float | None = None, oy: float | None = None) -> None:
    raw = str(component.properties.get("text") or component.properties.get("tag") or "")
    if not raw:
        return
    size = 2.6 * (s if s > 0 else 1.0)
    width = float(component.properties.get("width") or 360) * (s if s > 0 else 1.0)
    x = (component.x if ox is None else ox) + 4 * (s if s > 0 else 1.0)
    y = (component.y if oy is None else oy) + 8 * (s if s > 0 else 1.0)
    y_line = y
    for line in note_lines(raw, width, size):
        painter.text(x, y_line, line, size, align="start")
        y_line += size + 1.8


def draw_wire_caption(
    painter: Painter,
    wire: Wire,
    pts: list,
    *,
    ladder: bool = False,
    at: tuple[float, float, str] | None = None,
    spec: str | None = None,
) -> None:
    """Wire number beside the conductor, color and gauge underneath.

    ``spec`` is the text actually drawn. Pass ``""`` to suppress a gauge that
    a parallel bundle already noted. ``None`` uses ``wire.spec_text()``.
    """
    if not pts:
        return
    from panelcad.render.wire_labels import MARK_SIZE, SPEC_SIZE

    mark = wire.drawn_mark(ladder=ladder)
    shown = wire.spec_text() if spec is None else spec
    painter.set_layer("WIRE_NUMBERS")
    if at is None:
        return
    x, y, align = at
    if mark:
        painter.text(x, y, mark, MARK_SIZE, align=align)
    if shown:
        from panelcad.render.wire_labels import spec_origin

        sx, sy, salign, ssize = spec_origin(x, y, mark, shown, align)
        painter.text(sx, sy, shown, ssize, align=salign)
