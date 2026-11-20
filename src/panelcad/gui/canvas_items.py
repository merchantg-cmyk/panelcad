from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from panelcad.gui.canvas import SchematicView

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QStyleOptionGraphicsItem,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.core.layout import clip_offset, rail_height
from panelcad.library.ports import effective_ports, network_node_caption
from panelcad.render.io_ladder import io_card_size, io_pin_layout
from panelcad.render.network import node_size, port_layout
from panelcad.core.models import Component, Device, Enclosure, Rail
from panelcad.core.primitives import Primitive
from panelcad.render.painter import (
    _draw_column_selector,
    _draw_on_delay_coil,
    _primitive_is_lower_pole,
    draw_control_coil,
)
from panelcad.render.qt_painter import QtPainter, draw_world_text as _default_draw_world_text


def draw_world_text(*args, **kwargs):
    """Delegate through ``panelcad.gui.canvas.draw_world_text`` if monkeypatched."""
    import sys

    canvas_mod = sys.modules.get("panelcad.gui.canvas")
    fn = getattr(canvas_mod, "draw_world_text", None) if canvas_mod is not None else None
    if fn is not None and fn is not draw_world_text:
        return fn(*args, **kwargs)
    return _default_draw_world_text(*args, **kwargs)
from panelcad.render.titleblock import (
    draw_sheet_chrome,
    draw_title_block_only,
    title_block_rect,
)


_SCHEMATIC_VIEW_CLS: type | None = None


def _register_schematic_view_cls(cls: type) -> None:
    global _SCHEMATIC_VIEW_CLS
    _SCHEMATIC_VIEW_CLS = cls


def _is_schematic_view(obj: object) -> bool:
    return _SCHEMATIC_VIEW_CLS is not None and isinstance(obj, _SCHEMATIC_VIEW_CLS)

SNAP = 2.5
PANEL_SNAP = 1.0
# Half-width of a wire click. The old stroker was 6 mm (±3 mm). A fixed pixel
# band is tighter than that at every zoom a sheet is actually viewed at, so
# the hit is whichever is larger.
WIRE_HIT_MM = 3.0
WIRE_HIT_PX = 7.0
LIBRARY_MIME = "application/x-panelcad-library-id"


def _qc(role: str) -> QColor:
    """On-screen drawing token. Print and export do not use this."""

    from panelcad.gui.theme import canvas_qcolor

    return canvas_qcolor(role)


def _screen_hex(color: str) -> str:
    """Print hex adapted to the open sheet. Paper canvas leaves it alone."""

    from panelcad.theme import adapt_print_color

    return adapt_print_color(color)


def _voltage_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"(\d+(?:\.\d+)?)", str(value))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _voltage_in(props: object) -> float | None:
    if not isinstance(props, dict):
        return None
    for key, val in props.items():
        if "volt" not in str(key).lower():
            continue
        number = _voltage_number(val)
        if number:
            return number
    return None


def _sheet_voltage(session: Session) -> float | None:
    """Panel or supply voltage from project properties. Sheet text is not a voltage."""

    found = _voltage_in(getattr(session.project, "properties", None))
    if found:
        return found
    sheet = session.sheet()
    for comp in sheet.components:
        found = _voltage_in(comp.properties)
        if found:
            return found
    for other in session.project.sheets:
        if other is sheet:
            continue
        for comp in other.components:
            library = str(getattr(comp, "library_id", "") or "").lower()
            if "supply" not in library and "transformer" not in library:
                continue
            found = _voltage_in(comp.properties)
            if found:
                return found
    return None


def _problems_tab_current(view: SchematicView) -> bool:
    """True when the Problems dock is the tab the user is looking at."""

    window = view.window()
    dock = getattr(window, "problems_wrap", None)
    if dock is None or not dock.toggleViewAction().isChecked():
        return False
    from PySide6.QtWidgets import QTabBar

    title = dock.windowTitle().replace("&", "")
    saw_tab = False
    for bar in window.findChildren(QTabBar):
        if bar.isHidden() or bar.count() < 1:
            continue
        texts = [bar.tabText(index).replace("&", "") for index in range(bar.count())]
        if title not in texts and not any(text.startswith("Problems") for text in texts):
            continue
        saw_tab = True
        current = bar.tabText(bar.currentIndex()).replace("&", "")
        if current == title or current.startswith("Problems"):
            return True
    if saw_tab:
        return False
    return not dock.isHidden()


def _wire_screen_pen(wire, session: Session, *, ladder: bool) -> tuple[QColor, float, QColor | None]:
    """Phase colour for the canvas. Export painters keep their own stroke."""

    from panelcad.theme import phase_color_name, phase_colors_enabled, screen_conductor

    width = 0.55 if ladder else 0.45
    plain = (_qc("ink" if ladder else "wire"), width, None)
    if not phase_colors_enabled():
        return plain
    standard = str(getattr(session.project.title_block, "standard", "") or "")
    name = phase_color_name(
        str(getattr(wire, "color", "") or ""),
        str(getattr(wire, "net", "") or ""),
        standard=standard,
        voltage=_sheet_voltage(session),
    )
    if not name:
        return plain
    core, outline = screen_conductor(name)
    return (QColor(core), width, QColor(outline) if outline else None)
BOM_MIME = "application/x-panelcad-bom"


def snap(value: float) -> float:
    return round(value / SNAP) * SNAP


def _pixels_per_scene(view) -> float:
    """View scale: screen pixels per sheet millimetre. 1 when the view is not up yet."""

    if view is None:
        return 1.0
    return max(abs(float(view.transform().m11())), 1e-6)


def wire_hit_radius_scene(view) -> float:
    """Click half-width in sheet millimetres: max(3 mm, ~7 screen pixels)."""

    scale = _pixels_per_scene(view)
    return max(WIRE_HIT_MM, WIRE_HIT_PX / scale)


def wire_hit_width_scene(view) -> float:
    """Full stroker width. ``boundingRect`` must cover this, or the index drops the click."""

    return wire_hit_radius_scene(view) * 2.0


def _segment_distance(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx = bx - ax
    dy = by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _path_distance(path: QPainterPath, x: float, y: float) -> float:
    """Distance from a point to a wire polyline, including hop curves."""

    best = 1e9
    cx = cy = None
    index = 0
    count = path.elementCount()
    while index < count:
        element = path.elementAt(index)
        kind = element.type
        if kind == QPainterPath.ElementType.MoveToElement:
            cx, cy = element.x, element.y
            index += 1
            continue
        if kind == QPainterPath.ElementType.LineToElement and cx is not None:
            best = min(best, _segment_distance(x, y, cx, cy, element.x, element.y))
            cx, cy = element.x, element.y
            index += 1
            continue
        if kind == QPainterPath.ElementType.CurveToElement and cx is not None and index + 2 < count:
            c1 = element
            c2 = path.elementAt(index + 1)
            end = path.elementAt(index + 2)
            for step in range(1, 9):
                t = step / 8.0
                u = 1.0 - t
                sx = (u * u * u * cx) + (3 * u * u * t * c1.x) + (3 * u * t * t * c2.x) + (t * t * t * end.x)
                sy = (u * u * u * cy) + (3 * u * u * t * c1.y) + (3 * u * t * t * c2.y) + (t * t * t * end.y)
                best = min(best, math.hypot(x - sx, y - sy))
            cx, cy = end.x, end.y
            index += 3
            continue
        index += 1
    return best


class PinItem(QGraphicsItem):
    """Connection terminal. Locked: click to wire. Unlocked: drag to slide on the symbol."""

    HIT = 4.2

    def __init__(
        self, component_id: str, pin_id: str, lx: float, ly: float, parent: QGraphicsItem | None = None,
        label: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.component_id = component_id
        self.pin_id = pin_id
        self.display_label = label or pin_id
        self._lx = lx
        self._ly = ly
        self._hot = False
        self._locked = True
        self.setPos(lx, ly)
        self.setZValue(25)
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.set_locked(True)

    def ref(self) -> str:
        return f"{self.component_id}.{self.pin_id}"

    def set_locked(self, locked: bool) -> None:
        self._locked = bool(locked)
        if self._locked:
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.setToolTip(
                f"Terminal {self.display_label} ({self.pin_id})\nLocked — click or drag to another terminal to add a wire.\n"
                "Right-click the device → Unlock connection points to slide this pin."
            )
        else:
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            self.setToolTip(
                f"Terminal {self.display_label} ({self.pin_id})\nUnlocked — drag to slide onto the connection point.\n"
                "Click another terminal to wire. Lock the device when it looks right."
            )
        self.update()

    def boundingRect(self) -> QRectF:
        hit = self.HIT + (1.2 if not self._locked else 0.0)
        return QRectF(-hit - 1, -hit - 5, hit * 2 + 16, hit * 2 + 10)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        hit = self.HIT + (1.2 if not self._locked else 0.0)
        path.addEllipse(QRectF(-hit, -hit, hit * 2, hit * 2))
        return path

    def _view(self) -> SchematicView | None:
        scene = self.scene()
        if scene is None or not scene.views():
            return None
        view = scene.views()[0]
        return view if _is_schematic_view(view) else None

    def _connected_color(self) -> QColor | None:
        view = self._view()
        if view is None:
            return None
        ref = self.ref()
        for wire in view.session.sheet().wires:
            if ref not in (wire.frm, wire.to):
                continue
            color, _width, _outline = _wire_screen_pen(wire, view.session, ladder=False)
            return color
        return None

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        view = self._view()
        wiring = view is not None and (bool(view.wire_from) or view.mode == "wire")
        problems = bool(getattr(view, "_problems_marks", False)) if view is not None else False
        connected = self._connected_color()
        open_ring = self._hot or wiring or not self._locked or (connected is None and problems)
        if connected is not None and not open_ring:
            painter.setBrush(QBrush(connected))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(0, 0), 1.05, 1.05)
            return
        if open_ring and self._locked:
            r = 2.05 if self._hot or wiring else 1.6
            painter.setBrush(QBrush(_qc("pin_hot") if self._hot else _qc("pin_fill")))
            painter.setPen(QPen(_qc("pin"), 0.45))
            painter.drawEllipse(QPointF(0, 0), r, r)
        elif not self._locked:
            r = 2.05 if self._hot else 1.6
            fill = _qc("pin_unlocked_hot") if self._hot else _qc("pin_unlocked")
            painter.setBrush(QBrush(fill))
            painter.setPen(QPen(_qc("pin_unlocked_ring"), 0.45))
            painter.drawEllipse(QPointF(0, 0), r, r)
        else:
            r = 1.6
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(_qc("pin_muted"), 0.35))
            painter.drawEllipse(QPointF(0, 0), r, r)
        if self._hot or wiring or not self._locked:
            draw_world_text(painter, r + 1.3, -0.3, self.display_label, 2.3, align="start")

    def hoverEnterEvent(self, event) -> None:  # noqa: N802
        self._hot = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: N802
        self._hot = False
        self.update()
        super().hoverLeaveEvent(event)


class ResizeHandle(QGraphicsRectItem):
    """Bottom-right handle. Drag to scale the parent device."""

    def __init__(self, parent: ComponentItem) -> None:
        super().__init__(-1.8, -1.8, 3.6, 3.6, parent)
        self.setBrush(QBrush(_qc("highlight")))
        self.setPen(QPen(_qc("highlight_edge"), 0.3))
        self.setZValue(40)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setToolTip("Drag to resize")
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.reposition()

    def reposition(self) -> None:
        parent = self.parentItem()
        if isinstance(parent, ComponentItem):
            self.setPos(parent._w, parent._h)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        parent = self.parentItem()
        if isinstance(parent, ComponentItem):
            parent.resize_to_local(self.mapToParent(event.pos()))
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        parent = self.parentItem()
        if isinstance(parent, ComponentItem):
            parent.commit_scale()
        event.accept()


class ComponentItem(QGraphicsItem):
    def __init__(
        self,
        component: Component,
        device: Device,
        layout: bool = False,
        network: bool = False,
        io_card: bool = False,
        *,
        sheet_id: str = "",
    ) -> None:
        super().__init__()
        self.component_id = component.id
        # The tag is not an instance. M1 on the power page and M1 on the
        # control page are two placements, and each keeps its own position.
        self.sheet_id = str(sheet_id or "")
        self.device = device
        self._library_id = component.library_id
        from panelcad.library.panel import is_info_needed_placeholder

        self._needs_info = is_info_needed_placeholder(component, device)
        self._marker_label = str(
            (component.properties or {}).get("marker_label")
            or (component.properties or {}).get("part_number")
            or (component.properties or {}).get("label_text")
            or ""
        ).strip()
        self.layout = layout
        self.network = network
        self.io_card = io_card and not bool(component.properties.get("ladder_card"))
        self.ladder_card = bool(component.properties.get("ladder_card"))
        self._addr = network_node_caption(component, effective_ports(device, component))
        self._catalog = str(component.properties.get("catalog") or component.properties.get("tag") or device.name)
        self._snap = PANEL_SNAP if layout else SNAP
        self._scale = component.visual_scale()
        self.setPos(component.x, component.y)
        # Model coordinates this item was built from. A later sync treats the
        # scene as authoritative only after the item moves away from here.
        self._model_xy = (float(component.x), float(component.y))
        self.setRotation(0 if layout else component.rotation)
        self.setZValue(10)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setToolTip(f"{component.id}  —  right-click for commands")
        self._hide_tag = bool(component.properties.get("hide_tag"))
        self._hidden_pins = {str(pin_id) for pin_id in (component.properties.get("hidden_pins") or [])}
        self._tag_layout: tuple[float, float, str, float] | None = None
        self._painted_symbol = ""
        from panelcad.core.pins import pin_caption

        self._pin_draw: list[tuple[float, float, str, bool]] = []
        if layout:
            from panelcad.library.panel import display_footprint_size
            from panelcad.reconstruct.placeholder import PLACEHOLDER_MARK_H, PLACEHOLDER_MARK_W

            if self._needs_info:
                fw, fh = PLACEHOLDER_MARK_W, PLACEHOLDER_MARK_H
            else:
                fw, fh = display_footprint_size(device, component)
            s0 = component.visual_scale()
            self._base_w = max(1.0, fw / s0 if s0 else fw)
            self._base_h = max(1.0, fh / s0 if s0 else fh)
        elif io_card:
            self._base_w, self._base_h = io_card_size(device)
            laid = io_pin_layout(device)
            for pin in device.pins:
                lx, ly = component.pin_xy_id(pin.id, laid.get(pin.id, (self._base_w / 2.0, 18.0)))
                cap = pin_caption(pin, component)
                item = PinItem(component.id, pin.id, lx, ly, self, label=cap)
                item.setPos(lx * self._scale, ly * self._scale)
                item.set_locked(component.pins_locked)
                item.setToolTip(f"{cap}  ({pin.id})")
                self._pin_draw.append((lx, ly, cap, lx < self._base_w * 0.5))
        elif network:
            ports = effective_ports(device, component)
            self._base_w, self._base_h = node_size(len(ports))
            laid = port_layout(ports, self._base_w, self._base_h)
            for p in ports:
                lx, ly = component.pin_xy_id(p.id, laid[p.id])
                cap = str(getattr(p, "label", "") or p.id)
                item = PinItem(component.id, p.id, lx, ly, self, label=cap)
                item.setPos(lx * self._scale, ly * self._scale)
                item.set_locked(component.pins_locked)
                item.setToolTip(f"{cap}  {p.kind}  {p.medium} {p.protocol}".strip())
                self._pin_draw.append((lx, ly, cap, p.side == "left"))
        else:
            if (device.category or "").lower().startswith("connection.notes"):
                from panelcad.render.painter import notes_box

                self._base_w, self._base_h = notes_box(component, 1.0)
            else:
                self._base_w = device.schematic_width
                self._base_h = device.schematic_height
            coil_lib = f"{device.id} {device.library_id}".lower()
            skip_bottom = "coil" in coil_lib and "aux" not in coil_lib
            for pin in device.pins:
                if skip_bottom and pin.side == "bottom":
                    continue
                if str(pin.id) in self._hidden_pins:
                    continue
                lx, ly = component.pin_xy(pin)
                cap = pin_caption(pin, component)
                item = PinItem(component.id, pin.id, lx, ly, self, label=cap)
                item.setPos(lx * self._scale, ly * self._scale)
                item.set_locked(component.pins_locked)
        if self.ladder_card:
            self._base_w = float(component.properties.get("card_w") or self._base_w)
            self._base_h = float(component.properties.get("card_h") or self._base_h)
        self._w = self._base_w * self._scale
        self._h = self._base_h * self._scale
        self._handle = ResizeHandle(self)
        self._handle.setVisible(False)

    def set_tag_layout(self, layout: tuple[float, float, str, float] | None) -> None:
        """World tag anchor from ``place_device_tags``, stored in item space.

        Subtracting ``pos()`` leaves a 90° symbol's tag in the wrong place:
        the item transform then rotates it. ``mapFromScene`` applies that
        rotation, and ``paint`` draws the glyphs back upright.
        """

        self.prepareGeometryChange()
        if layout is None:
            self._tag_layout = None
        else:
            local = self.mapFromScene(QPointF(float(layout[0]), float(layout[1])))
            self._tag_layout = (local.x(), local.y(), str(layout[2]), float(layout[3]))
        self.update()

    def _sheet_and_component(self):
        view = self.scene().views()[0] if self.scene() and self.scene().views() else None
        if not _is_schematic_view(view) or self.layout:
            return None, None
        try:
            sheet = view.session.sheet()
            return view.session.project, sheet.component(self.component_id)
        except Exception:  # noqa: BLE001
            return None, None

    def _shown_tag(self) -> str:
        project, comp = self._sheet_and_component()
        if project is None or comp is None:
            return self.component_id
        view = self.scene().views()[0]
        from panelcad.core.panels import schematic_tag_text

        return schematic_tag_text(project, view.session.sheet(), comp)

    def _panel_location_caption(self) -> str:
        project, comp = self._sheet_and_component()
        if project is None or comp is None:
            return ""
        view = self.scene().views()[0]
        from panelcad.core.panels import schematic_location_caption

        return schematic_location_caption(project, view.session.sheet(), comp)

    def boundingRect(self) -> QRectF:
        rect = QRectF(-28, -16, self._w + 56, self._h + 24)
        if self._tag_layout is not None:
            x, y, _align, size = self._tag_layout
            # The glyphs are counter-rotated, so the item-space box is a square.
            span = max(14.0, size * 8.0)
            rect = rect.united(QRectF(x - span, y - span, span * 2.0, span * 2.0))
        return rect

    def apply_scale(self, scale: float) -> None:
        """Update the on-screen symbol only. Routes stay until ``commit_scale``.

        Dropping wire routes here used to mutate the model while the resize
        handle was still moving, with no undo step. Undo then could not put
        the old path back.
        """

        self.prepareGeometryChange()
        self._scale = max(0.15, min(8.0, float(scale)))
        self._w = self._base_w * self._scale
        self._h = self._base_h * self._scale
        for child in self.childItems():
            if isinstance(child, PinItem):
                child.setPos(child._lx * self._scale, child._ly * self._scale)
            elif isinstance(child, ResizeHandle):
                child.reposition()
        self.update()
        scene = self.scene()
        view = scene.views()[0] if scene and scene.views() else None
        if _is_schematic_view(view):
            view._redraw_wires()

    def resize_to_local(self, local: QPointF) -> None:
        sx = abs(local.x()) / max(self._base_w, 1.0)
        sy = abs(local.y()) / max(self._base_h, 1.0)
        self.apply_scale(max(sx, sy))

    def commit_scale(self) -> None:
        scene = self.scene()
        view = scene.views()[0] if scene and scene.views() else None
        if _is_schematic_view(view):
            # One undo step for the scale and the routes that follow it.
            opened = not view.session.command_open()
            if opened:
                view.session.begin_edit("scale")
            try:
                view.session.set_scale(self.component_id, self._scale)
                view.session.drop_wire_routes({self.component_id})
            finally:
                if opened:
                    view.session.end_edit()
            view._redraw_wires()
            view.project_changed.emit()
            view.status.emit(f"{self.component_id} scale {self._scale:.2f} — drag the orange handle to resize")

    def _layout_picture_path(self):
        from panelcad.render.titleblock import resolve_logo

        scene = self.scene()
        view = scene.views()[0] if scene and scene.views() else None
        if not _is_schematic_view(view):
            return None
        try:
            pic = view.session.sheet().component(self.component_id).picture
        except KeyError:
            return None
        base = view.session.path.parent if view.session.path else None
        return resolve_logo(pic, base)

    def _draw_device_tag(self, painter: QPainter) -> None:
        """Device tag, in item coordinates. Matches the PDF seater when the sheet is open."""

        scene = self.scene()
        view = scene.views()[0] if scene and scene.views() else None
        if _is_schematic_view(view):
            try:
                from panelcad.core.ladder import is_ladder_sheet
                from panelcad.render.sheet_labels import place_device_tags

                sheet = view.session.sheet()
                ladder = is_ladder_sheet(sheet, view.session.project)
                sheet_no = view.session.sheet_index() + 1
                key = (
                    sheet.id,
                    ladder,
                    sheet_no,
                    tuple(
                        (
                            c.id,
                            c.x,
                            c.y,
                            c.library_id,
                            str(c.properties.get("hp") or ""),
                            str(c.properties.get("FLA") or ""),
                            str(c.properties.get("In") or ""),
                            str(c.properties.get("setting") or ""),
                        )
                        for c in sheet.components
                    ),
                    tuple((w.id, w.number, w.frm, w.to) for w in sheet.wires),
                )
                cached = getattr(view, "_device_tag_cache", None)
                if cached is None or cached[0] != key:
                    cached = (
                        key,
                        place_device_tags(
                            sheet,
                            view.session.catalog,
                            ladder=ladder,
                            project=view.session.project,
                            sheet_no=sheet_no,
                        ),
                    )
                    view._device_tag_cache = cached
                layout = cached[1].get(self.component_id)
                comp = sheet.component(self.component_id)
            except (KeyError, ValueError, IndexError, AttributeError):
                layout = None
                comp = None
            if layout is not None and comp is not None:
                rx, ry, align, size = layout
                draw_world_text(painter, rx - comp.x, ry - comp.y, self.component_id, size, align=align)
                return
        draw_world_text(painter, 0.0, -2.5 * self._scale, self.component_id, 3.0, align="start")

    def _paint_column_symbol(self, painter: QPainter, component: Component, coil: str) -> bool:
        """Draw the same coil, timer, or selector print draws, in scene millimetres.

        The item is already rotated. Drawing the library box in item space is
        what put a TON rectangle on the screen where print shows the delay coil.
        """

        cat = (self.device.category or "").lower()
        rotation = int(component.rotation or 0)
        kind = ""

        def draw(target: QtPainter) -> None:
            nonlocal kind
            if draw_control_coil(target, self.device, component, coil):
                kind = "coil"
                return
            if "timer" in cat and rotation in {90, 270} and _draw_on_delay_coil(target, self.device, component):
                kind = "on_delay"
                return
            if "selector" in cat and rotation in {90, 270} and _draw_column_selector(target, self.device, component):
                kind = "selector"

        painter.save()
        inverted, ok = self.sceneTransform().inverted()
        if ok:
            painter.setTransform(inverted, True)
        draw(QtPainter(painter))
        painter.restore()
        if kind:
            self._painted_symbol = kind
        return bool(kind)

    def _sheet_component(self) -> Component | None:
        scene = self.scene()
        view = scene.views()[0] if scene and scene.views() else None
        if not _is_schematic_view(view):
            return None
        try:
            return view.session.sheet().component(self.component_id)
        except KeyError:
            return None

    def _paint_ladder_card(self, painter: QPainter) -> None:
        """Point map for an on-screen PLC ladder card.

        Matches ``render.painter._draw_ladder_card``: header, SEE cross-reference,
        terminal address, and point description. Fills and ink are canvas tokens.
        Print, PDF, and SVG keep the paper palette in that function.
        """

        from panelcad.core.pins import pin_caption
        from panelcad.theme import canvas_hex

        scale = self._scale
        ink = str(canvas_hex("ink"))
        painter.setPen(QPen(_qc("ink"), 0.45))
        painter.setBrush(QBrush(_qc("ladder_fill")))
        painter.drawRect(QRectF(0, 0, self._w, self._h))
        painter.setBrush(QBrush(_qc("ladder_header")))
        painter.drawRect(QRectF(0, 0, self._w, 11.0 * scale))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        comp = self._sheet_component()
        title = self.component_id
        xref = ""
        io_map: dict = {}
        limit = False
        mapped: set[str] = set()
        if comp is not None:
            title = str(comp.properties.get("shown_tag") or comp.id)
            xref = str(comp.properties.get("xref_rack") or "").strip()
            raw = comp.properties.get("io")
            io_map = raw if isinstance(raw, dict) else {}
            limit = bool(comp.properties.get("io_pin_limit"))
            mapped = set(comp.pin_overrides)
        draw_world_text(painter, self._w / 2.0, 7.2 * scale, title, 2.8, align="middle", color=ink)
        if xref:
            draw_world_text(
                painter, self._w / 2.0, 14.6 * scale, f"SEE {xref}", 1.8, align="middle", color=ink
            )
        for pin in self.device.pins:
            if limit and pin.id not in mapped:
                continue
            if not limit and mapped and pin.id not in mapped:
                continue
            if comp is None:
                lx, ly = float(pin.x), float(pin.y)
                side = pin.side
                caption = pin_caption(pin, None)
            else:
                lx, ly = comp.pin_xy(pin)
                side = comp.pin_side(pin) or pin.side
                caption = pin_caption(pin, comp)
            wx, wy = lx * scale, ly * scale
            meta = io_map.get(pin.id) if isinstance(io_map.get(pin.id), dict) else {}
            desc = str(meta.get("description") or "").strip()
            if desc.lower() == "spare":
                desc = ""
            desc = desc[:22]
            painter.setPen(QPen(_qc("ink"), 0.35))
            if side == "left":
                painter.drawLine(QPointF(wx, wy), QPointF(wx + 6.0 * scale, wy))
                draw_world_text(
                    painter,
                    wx + 7.4 * scale,
                    wy - 0.2 * scale,
                    caption,
                    2.1,
                    align="start",
                    color=ink,
                )
                if desc:
                    draw_world_text(
                        painter,
                        wx + 7.4 * scale,
                        wy + 2.6 * scale,
                        desc,
                        1.7,
                        align="start",
                        color=ink,
                    )
            elif side == "right":
                painter.drawLine(QPointF(wx - 6.0 * scale, wy), QPointF(wx, wy))
                draw_world_text(
                    painter,
                    wx - 7.4 * scale,
                    wy - 0.2 * scale,
                    caption,
                    2.1,
                    align="end",
                    color=ink,
                )
                if desc:
                    draw_world_text(
                        painter,
                        wx - 7.4 * scale,
                        wy + 2.6 * scale,
                        desc,
                        1.7,
                        align="end",
                        color=ink,
                    )
            elif side == "top":
                painter.drawLine(QPointF(wx, wy), QPointF(wx, wy + 6.0 * scale))
                draw_world_text(
                    painter,
                    wx + 2.4 * scale,
                    wy + 3.2 * scale,
                    caption,
                    2.0,
                    align="start",
                    color=ink,
                )
            else:
                painter.drawLine(QPointF(wx, wy - 6.0 * scale), QPointF(wx, wy))
                draw_world_text(
                    painter,
                    wx + 2.4 * scale,
                    wy - 3.0 * scale,
                    caption,
                    2.0,
                    align="start",
                    color=ink,
                )

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.isSelected():
            painter.setPen(QPen(_qc("highlight"), 0.6, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(-2, -2, self._w + 4, self._h + 4))
        if self.layout:
            if self._needs_info:
                painter.setPen(QPen(_qc("needs_info"), 0.7, Qt.PenStyle.DashLine))
                painter.setBrush(QBrush(_qc("needs_info_fill")))
                painter.drawRect(QRectF(0, 0, self._w, self._h))
                painter.setPen(QPen(_qc("needs_info"), 0.28))
                step = max(3.0, min(self._w, self._h) / 5.0)
                offset = 0.0
                while offset < self._w + self._h:
                    x1 = max(0.0, offset - self._h)
                    y1 = min(self._h, offset)
                    x2 = min(self._w, offset)
                    y2 = max(0.0, offset - self._w)
                    painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
                    offset += step
                painter.setPen(QPen(_qc("needs_info"), 0.35))
                headline = self._marker_label or self.component_id
                draw_world_text(painter, self._w / 2.0, self._h * 0.38, headline, 2.6, align="middle")
                draw_world_text(painter, self._w / 2.0, self._h * 0.64, "INFO NEEDED", 2.2, align="middle")
                return
            painter.setPen(QPen(_qc("ink"), 0.4))
            painter.setBrush(QBrush(_qc("layout_fill")))
            painter.drawRect(QRectF(0, 0, self._w, self._h))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            pic_path = self._layout_picture_path()
            if pic_path is not None:
                QtPainter(painter).image(str(pic_path), 0.5, 0.5, self._w - 1.0, self._h - 1.0)
            elif self.device.panel.din_rail and self._h >= 12:
                painter.setPen(QPen(_qc("layout_tick"), 0.25))
                tick = max(1.2, min(3.0, self._w * 0.18))
                n = max(2, int(self._h / 8.0))
                for i in range(n):
                    ty = 3.0 + i * ((self._h - 6.0) / max(n - 1, 1))
                    painter.drawLine(QPointF(0.8, ty), QPointF(tick, ty))
                    painter.drawLine(QPointF(self._w - tick, ty), QPointF(self._w - 0.8, ty))
            if self.device.panel.din_rail:
                cy = clip_offset(self.device) * self._scale
                painter.setPen(QPen(_qc("layout_clip"), 0.35))
                painter.drawLine(QPointF(1, cy), QPointF(self._w - 1, cy))
            painter.setPen(QPen(_qc("ink"), 0.35))
            draw_world_text(painter, self._w / 2.0, min(7.0, self._h * 0.32), self.component_id, 2.4, align="middle")
            cat = str(self._catalog or "")
            if pic_path is None and self._h >= 22 and cat:
                draw_world_text(painter, self._w / 2.0, self._h * 0.55, cat[:16], 1.8, align="middle")
            return
        if self.ladder_card:
            self._paint_ladder_card(painter)
            return
        if self.io_card:
            painter.setPen(QPen(_qc("ink"), 0.45))
            painter.setBrush(QBrush(_qc("io_fill")))
            painter.drawRect(QRectF(0, 0, self._w, self._h))
            painter.setBrush(QBrush(_qc("io_header")))
            painter.drawRect(QRectF(0, 0, self._w, 14.0 * self._scale))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            draw_world_text(painter, self._w / 2.0, 5.0 * self._scale, self.component_id, 2.8, align="middle")
            draw_world_text(painter, self._w / 2.0, 10.5 * self._scale, self._catalog[:18], 1.9, align="middle")
            painter.setPen(QPen(_qc("io_rule"), 0.25))
            for lx, ly, cap, left in self._pin_draw:
                py = ly * self._scale
                painter.drawLine(QPointF(0, py), QPointF(self._w, py))
            painter.setPen(QPen(_qc("ink"), 0.35))
            for lx, ly, cap, left in self._pin_draw:
                px = lx * self._scale
                py = ly * self._scale
                tx = px + (3.5 if left else -3.5)
                draw_world_text(painter, tx, py - 0.6, cap, 2.0, align="start" if left else "end")
            return
        if self.network:
            painter.setPen(QPen(_qc("network_stroke"), 0.5))
            painter.setBrush(QBrush(_qc("network_fill")))
            painter.drawRoundedRect(QRectF(0, 0, self._w, self._h), 3.0, 3.0)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            draw_world_text(painter, self._w / 2.0, 8.0, self.component_id, 3.2, align="middle")
            draw_world_text(painter, self._w / 2.0, 16.0, self.device.name[:18], 2.1, align="middle")
            draw_world_text(painter, self._w / 2.0, 25.0, self._addr or "TBD", 2.2, align="middle")
            for lx, ly, cap, left in self._pin_draw:
                tx = lx * self._scale + (4.0 if not left else -4.0)
                draw_world_text(
                    painter,
                    tx,
                    ly * self._scale - 0.4,
                    cap,
                    1.9,
                    align="start" if not left else "end",
                )
            return
        coil = "circle"
        scene = self.scene()
        view = scene.views()[0] if scene and scene.views() else None
        if (self.device.category or "").lower().startswith("connection.notes"):
            from panelcad.render.painter import _draw_notes

            painter.setPen(QPen(_qc("notes_stroke"), 0.25))
            painter.setBrush(QBrush(_qc("notes_fill")))
            painter.drawRect(QRectF(0, 0, self._w, self._h))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if _is_schematic_view(view):
                try:
                    _draw_notes(
                        QtPainter(painter),
                        view.session.sheet().component(self.component_id),
                        self._scale,
                        0.0,
                        0.0,
                    )
                except KeyError:
                    pass
            return
        if _is_schematic_view(view):
            coil = getattr(view.session.project.title_block, "coil_style", "circle") or "circle"
        if coil == "circle" and self.device.category.lower().startswith("load.solenoid"):
            painter.setPen(QPen(_qc("ink"), 0.45))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            cx, cy = self._w * 0.5, self._h * 0.5
            r = min(self._w, self._h) * 0.32
            painter.drawEllipse(QPointF(cx, cy), r, r)
            painter.drawLine(QPointF(0, cy), QPointF(cx - r, cy))
            painter.drawLine(QPointF(cx + r, cy), QPointF(self._w, cy))
            draw_world_text(painter, cx, cy + r + 2.2, "Y", 2.2, align="middle")
            painter.setPen(QPen(_qc("ink"), 0.35))
            self._draw_device_tag(painter)
            return
        comp = None
        if _is_schematic_view(view):
            try:
                comp = view.session.sheet().component(self.component_id)
            except KeyError:
                comp = None
        column_drawn = comp is not None and self._paint_column_symbol(painter, comp, coil)
        if not column_drawn:
            self._painted_symbol = "primitives"
            painter.save()
            painter.scale(self._scale, self._scale)
            painter.setPen(QPen(_qc("ink"), 0.35))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            from panelcad.core.pins import shadowed_terminal_texts

            shadowed = shadowed_terminal_texts(self.device, comp)
            for index, prim in enumerate(self.device.primitives):
                if index in shadowed:
                    continue
                if not isinstance(prim, Primitive):
                    prim = Primitive.model_validate(prim)
                if self._hidden_pins and _primitive_is_lower_pole(prim):
                    continue
                _paint_primitive(painter, prim)
            from panelcad.core.transformer_jumpers import jumper_primitives

            for prim in jumper_primitives(self.device, comp):
                _paint_primitive(painter, prim)
            if comp is not None:
                # Same leads the print draws: a wire never ends beside an inset body.
                from panelcad.core.pin_ink import pin_lead

                for pin in self.device.pins:
                    if pin.id in (self._hidden_pins or ()):
                        continue
                    end = pin_lead(self.device, comp, pin)
                    if end is not None:
                        px, py = comp.pin_xy(pin)
                        _paint_primitive(painter, Primitive(type="line", x=float(px), y=float(py), x2=end[0], y2=end[1]))
            painter.restore()
        painter.setPen(QPen(_qc("ink"), 0.35))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        from panelcad.core.ladder import is_aux_library

        shown = self._shown_tag()
        if self._hide_tag or (column_drawn and self._tag_layout is None):
            pass
        elif self._tag_layout is not None:
            lx, ly, align, size = self._tag_layout
            draw_world_text(
                painter,
                lx,
                ly,
                shown,
                size,
                align=align,
                rotate=-float(self.rotation() or 0),
            )
        elif is_aux_library(self.device.library_id):
            draw_world_text(
                painter,
                self._w * 0.5,
                self._h + 3.2 * self._scale,
                shown,
                2.6,
                align="middle",
            )
        else:
            ladder_ref = False
            if _is_schematic_view(view):
                ladder_ref = str(getattr(view.session.project.title_block, "drawing_style", "") or "") == "ladder"
            if ladder_ref:
                draw_world_text(
                    painter,
                    self._w * 0.5,
                    -2.5 * self._scale,
                    shown,
                    3.0,
                    align="middle",
                )
            else:
                draw_world_text(painter, 0.0, -2.5 * self._scale, shown, 3.0, align="start")
        loc = self._panel_location_caption()
        if loc and not self.layout:
            draw_world_text(painter, 0.0, -8.2 * self._scale, loc, 2.0, align="start")
        if column_drawn:
            return
        try:
            from panelcad.core.pins import extra_captions, pin_caption

            view = self.scene().views()[0] if self.scene() and self.scene().views() else None
            comp = None
            if _is_schematic_view(view):
                try:
                    comp = view.session.sheet().component(self.component_id)
                except KeyError:
                    comp = None
            y_cap = -5.6 * self._scale
            io_field = _is_schematic_view(view) and (not self.io_card) and view.session.sheet().kind == "io"
            ladder = _is_schematic_view(view) and str(
                getattr(view.session.project.title_block, "drawing_style", "") or ""
            ) == "ladder"
            if comp is not None and not io_field and not ladder:
                for line in extra_captions(comp):
                    draw_world_text(painter, 0.0, y_cap, line, 2.2, align="start")
                    y_cap -= 3.1 * self._scale
                from panelcad.core.pins import rating_captions

                y_rate = self._h * 0.42
                for line in rating_captions(comp):
                    draw_world_text(painter, -1.4 * self._scale, y_rate, line, 2.2, align="end")
                    y_rate += 3.1 * self._scale
                for pin in self.device.pins:
                    if str(pin.id) in self._hidden_pins:
                        continue
                    cap = pin_caption(pin, comp)
                    if not cap or cap == pin.id:
                        continue
                    lx, ly = comp.pin_xy(pin)
                    px, py = lx * self._scale, ly * self._scale
                    side = comp.pin_side(pin)
                    if side == "left":
                        draw_world_text(painter, px - 2.2, py + 0.8, cap, 2.0, align="end")
                    elif side == "right":
                        draw_world_text(painter, px + 2.2, py + 0.8, cap, 2.0, align="start")
                    elif side == "top":
                        draw_world_text(painter, px, py - 2.6, cap, 2.0, align="middle")
                    else:
                        draw_world_text(painter, px, py + 3.6, cap, 2.0, align="middle")
        except (KeyError, AttributeError, TypeError):
            pass

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value):
        scene = self.scene()
        view = scene.views()[0] if scene and scene.views() else None
        applying = _is_schematic_view(view) and view._applying_model_pos
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and not applying:
            pt: QPointF = value
            step = self._snap
            return QPointF(round(pt.x() / step) * step, round(pt.y() / step) * step)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged and not applying:
            if _is_schematic_view(view) and view._gesture_edit and view.session.command_open():
                view._moved_since_press = True
                if getattr(self, "component_id", ""):
                    view._dragged_ids.add(str(self.component_id))
                view._wires_follow_component(self)
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            if hasattr(self, "_handle"):
                self._handle.setVisible(bool(value))
        return super().itemChange(change, value)


class WireItem(QGraphicsPathItem):
    """Schematic / comms link. Right-click to label or delete."""

    def __init__(
        self,
        path: QPainterPath,
        wire_id: str = "",
        label: str = "",
        label_xy: tuple[float, float] | None = None,
        label_align: str = "start",
    ) -> None:
        super().__init__(path)
        self.wire_id = wire_id
        self._label = label
        self._label_xy = label_xy
        self._label_align = label_align
        self._idle_width = 0.45
        self._core = _qc("wire")
        self._outline: QColor | None = None
        self._base_pen = QPen(self._core, self._idle_width)
        self._hovered = False
        # Must accept the click so it is not treated as empty paper / a device under the stroke.
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setZValue(18)

    def _view(self) -> SchematicView | None:
        scene = self.scene()
        if scene is None or not scene.views():
            return None
        view = scene.views()[0]
        return view if _is_schematic_view(view) else None

    def hit_width_scene(self) -> float:
        return wire_hit_width_scene(self._view())

    def _label_rect(self) -> QRectF | None:
        if not self._label:
            return None
        x, y = self._label_pos()
        return QRectF(x - 2, y - 8, max(24.0, len(self._label) * 2.2), 12)

    def shape(self) -> QPainterPath:
        # Full width is twice the half-width. The half-width is 3 mm or 7 px,
        # whichever reaches farther, so fit-page stays at least as easy as before.
        stroker = QPainterPathStroker()
        stroker.setWidth(self.hit_width_scene())
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        path = stroker.createStroke(self.path())
        label = self._label_rect()
        if label is not None:
            path.addRect(label)
        return path

    def boundingRect(self) -> QRectF:
        # The scene index uses this box, not shape(). A pen-only box drops
        # clicks that are a few pixels off the hairline.
        rect = self.path().boundingRect()
        pad = self.hit_width_scene() * 0.5 + 0.5
        rect.adjust(-pad, -pad, pad, pad)
        label = self._label_rect()
        if label is not None:
            rect = rect.united(label)
        return rect

    def distance_to_scene(self, x: float, y: float) -> float:
        local = self.mapFromScene(QPointF(x, y))
        label = self._label_rect()
        if label is not None and label.contains(local):
            return 0.0
        return _path_distance(self.path(), local.x(), local.y())

    @property
    def hovered(self) -> bool:
        return self._hovered

    def set_hovered(self, hovered: bool) -> None:
        hovered = bool(hovered)
        if self._hovered == hovered:
            return
        self._hovered = hovered
        self._sync_pen()
        if self.isSelected():
            self.setZValue(20)
        else:
            self.setZValue(19 if hovered else 18)
        self.update()

    def _label_pos(self) -> tuple[float, float]:
        if self._label_xy is not None:
            return self._label_xy
        mid = self.path().pointAtPercent(0.5)
        return (mid.x() + 2.6, mid.y())

    def set_screen_pen(self, core: QColor, width: float, outline: QColor | None = None) -> None:
        """Core stroke plus an optional contrast halo. The halo is on-screen only."""

        self._idle_width = width
        self._core = QColor(core)
        self._outline = outline
        pen = QPen(self._core, width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        self._base_pen = pen
        self._sync_pen()

    def _sync_pen(self) -> None:
        if self.isSelected() or self._hovered:
            pen = QPen(_qc("highlight"), 1.8 if self._hovered and not self.isSelected() else 0.9)
            if self._hovered and not self.isSelected():
                # Screen pixels, so the hover cue stays visible when zoomed out.
                pen.setCosmetic(True)
                pen.setWidthF(2.5)
        else:
            pen = QPen(self._base_pen)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        self.setPen(pen)

    def paint(self, painter, option, widget=None) -> None:
        if self._outline is not None and not self.isSelected():
            painter.save()
            halo = QPen(self._outline, self._idle_width + 0.9)
            halo.setCapStyle(Qt.PenCapStyle.RoundCap)
            halo.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(halo)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(self.path())
            painter.restore()
        super().paint(painter, option, widget)
        if not self._label:
            return
        x, y = self._label_pos()
        draw_world_text(painter, x, y, self._label, 2.4, align=self._label_align)

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self._sync_pen()
            if value:
                self.setZValue(20)
            else:
                self.setZValue(19 if self._hovered else 18)
        return super().itemChange(change, value)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # Consume so Qt does not fall through to rubber-band or a device under the wire.
        event.accept()


class WireHandleItem(QGraphicsItem):
    """Draggable endpoint or interior corner handle for a selected wire."""

    HIT = 2.8

    def __init__(
        self,
        wire_id: str,
        x: float,
        y: float,
        *,
        kind: str,
        which: str = "",
        corner_index: int = -1,
    ) -> None:
        super().__init__()
        self.wire_id = wire_id
        self.kind = kind
        self.which = which
        self.corner_index = corner_index
        self._hovered = False
        self.setPos(float(x), float(y))
        self.setZValue(34 if kind == "end" else 32)
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        if kind == "end":
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.setToolTip(f"Drag {which} end of {wire_id} to a terminal or rail")
        else:
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            self.setToolTip(f"Drag corner of {wire_id} to reroute")

    def _view(self) -> SchematicView | None:
        scene = self.scene()
        if scene is None or not scene.views():
            return None
        view = scene.views()[0]
        return view if _is_schematic_view(view) else None

    def hit_radius_scene(self) -> float:
        return max(self.HIT, wire_hit_radius_scene(self._view()) * 0.75)

    def boundingRect(self) -> QRectF:
        pad = self.hit_radius_scene() + 0.5
        return QRectF(-pad, -pad, pad * 2.0, pad * 2.0)

    def shape(self) -> QPainterPath:
        r = self.hit_radius_scene()
        path = QPainterPath()
        path.addEllipse(QRectF(-r, -r, r * 2.0, r * 2.0))
        return path

    def paint(
        self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None
    ) -> None:
        del option, widget
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.kind == "end":
            r = 1.9 if self._hovered else 1.55
            fill = _qc("pin_hot") if self._hovered else _qc("highlight")
            painter.setBrush(QBrush(fill))
            painter.setPen(QPen(_qc("highlight_edge"), 0.35))
            painter.drawEllipse(QPointF(0.0, 0.0), r, r)
        else:
            r = 1.65 if self._hovered else 1.35
            fill = _qc("pin_unlocked_hot") if self._hovered else _qc("pin_unlocked")
            painter.setBrush(QBrush(fill))
            painter.setPen(QPen(_qc("highlight_edge"), 0.35))
            painter.drawRect(QRectF(-r, -r, r * 2.0, r * 2.0))

    def hoverEnterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        event.accept()


class PhaseCalloutItem(QGraphicsItem):
    """Incoming 3-phase supply callout (L1, L2, L3) above an unwired line-side lead."""

    def __init__(
        self,
        text: str,
        x: float,
        y: float,
        size: float,
        align: str,
        stroke: str,
        *,
        outline: str | None = None,
    ) -> None:
        super().__init__()
        self._text = text
        self._x = x
        self._y = y
        self._size = size
        self._align = align
        self._stroke = stroke
        self._outline = outline
        self.setZValue(17)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)

    def boundingRect(self) -> QRectF:
        w = max(4.0, len(self._text) * self._size * 0.8)
        h = self._size * 1.4
        return QRectF(self._x - w, self._y - h, w * 2, h * 1.6)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        color = _qc("ink") if self._outline else QColor(_screen_hex(self._stroke))
        painter.setPen(QPen(color, 0.35))
        draw_world_text(painter, self._x, self._y, self._text, self._size, align=self._align)


class PhaseLeadItem(QGraphicsItem):
    """Colored breaker lead. Not a wire, so it cannot be selected or dragged."""

    def __init__(
        self,
        points: list[tuple[float, float]],
        stroke: str,
        width: float,
        *,
        outline: str | None = None,
        outline_width: float = 1.15,
    ) -> None:
        super().__init__()
        self._points = points
        self._stroke = stroke
        self._width = width
        self._outline = outline
        self._outline_width = outline_width
        self.setZValue(17)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)

    def boundingRect(self) -> QRectF:
        xs = [p[0] for p in self._points]
        ys = [p[1] for p in self._points]
        pad = 1.4
        return QRectF(min(xs) - pad, min(ys) - pad, max(xs) - min(xs) + pad * 2, max(ys) - min(ys) + pad * 2)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        if len(self._points) < 2:
            return
        path = QPainterPath()
        path.moveTo(self._points[0][0], self._points[0][1])
        for x, y in self._points[1:]:
            path.lineTo(x, y)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # Cream matches the canvas paper and covers the black symbol lead.
        under = QPen(_qc("paper"), 0.75)
        under.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(under)
        painter.drawPath(path)
        if self._outline:
            edge = QPen(QColor(_screen_hex(self._outline)), self._outline_width)
            edge.setCapStyle(Qt.PenCapStyle.FlatCap)
            painter.setPen(edge)
            painter.drawPath(path)
        pen = QPen(QColor(_screen_hex(self._stroke)), self._width)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(pen)
        painter.drawPath(path)


class RailJunctionItem(QGraphicsEllipseItem):
    """Filled dot where a wire meets a ladder or IEC rail."""

    def __init__(self, x: float, y: float) -> None:
        radius = 0.8
        super().__init__(x - radius, y - radius, radius * 2, radius * 2)
        self.setBrush(QBrush(_qc("ink")))
        self.setPen(QPen(Qt.PenStyle.NoPen))
        self.setZValue(21)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)


class EnclosureItem(QGraphicsItem):
    def __init__(self, enc: Enclosure) -> None:
        super().__init__()
        self.enc = enc
        self.setZValue(-6)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)

    def boundingRect(self) -> QRectF:
        from panelcad.core.layout import enclosure_drawn_size

        dw, dh, _s = enclosure_drawn_size(self.enc)
        return QRectF(self.enc.x, self.enc.y, dw, dh)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:

        from panelcad.render.layout import draw_enclosure
        from panelcad.render.qt_painter import QtPainter

        draw_enclosure(QtPainter(painter), self.enc)


class TrayItem(QGraphicsItem):
    def __init__(self, tray, mm_scale: float = 1.0) -> None:
        super().__init__()
        self.tray = tray
        self.mm_scale = mm_scale
        self.setZValue(-2)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)

    def boundingRect(self) -> QRectF:
        t = self.tray
        if t.orientation == "v":
            return QRectF(t.x, t.y, t.width_mm, t.length_mm)
        return QRectF(t.x, t.y, t.length_mm, t.width_mm)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        from panelcad.render.layout import draw_tray
        from panelcad.render.qt_painter import QtPainter

        draw_tray(QtPainter(painter), self.tray, mm_scale=self.mm_scale)


class RailItem(QGraphicsItem):
    def __init__(self, rail: Rail, mm_scale: float = 1.0) -> None:
        super().__init__()
        self.rail = rail
        self.mm_scale = mm_scale
        self.setZValue(0)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)

    def boundingRect(self) -> QRectF:
        h = rail_height(self.rail)
        half = h / 2.0
        return QRectF(self.rail.x, self.rail.y - half, self.rail.length_mm, h)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        from panelcad.render.layout import draw_rail
        from panelcad.render.qt_painter import QtPainter

        draw_rail(QtPainter(painter), self.rail, mm_scale=self.mm_scale)


class SheetChromeItem(QGraphicsItem):
    """Page border and zone ticks — behind devices and wires."""

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session
        self.setZValue(-5)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        w, h = session.sheet().page.dimensions()
        self._w, self._h = w, h

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self._w, self._h)

    def shape(self) -> QPainterPath:
        # Paint-only. A page-sized shape covered every wire, so a click on a
        # rung or the border never reached the conductor.
        return QPainterPath()

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        from panelcad.core.ladder import sheet_drawing_style

        sheet = self.session.sheet()
        style = sheet_drawing_style(sheet, self.session.project)
        try:
            index = self.session.project.sheets.index(sheet)
        except ValueError:
            index = 0
        draw_sheet_chrome(
            QtPainter(painter),
            sheet,
            drawing_style=style,
            catalog=self.session.catalog,
            project=self.session.project,
            sheet_index=index,
        )


class RevisionCloudItem(QGraphicsItem):
    """Scalloped revision cloud. Ignores the mouse so devices under it stay selectable."""

    def __init__(self, cloud, sheet, catalog=None) -> None:
        super().__init__()
        self.cloud = cloud
        self._sheet = sheet
        self._catalog = catalog
        from panelcad.render.clouds import drawn_box

        self._box = drawn_box(cloud, sheet)
        self.setZValue(65)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        rev = str(cloud.rev or "").strip() or "revision"
        note = str(getattr(cloud, "note", "") or "").strip()
        self.setToolTip(f"Revision cloud {rev}: {note}" if note else f"Revision cloud {rev}")

    def boundingRect(self) -> QRectF:
        x, y, w, h = self._box
        return QRectF(x - 18.0, y - 18.0, w + 36.0, h + 36.0)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        from panelcad.render.clouds import paint_revision_cloud

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_revision_cloud(QtPainter(painter), self.cloud, self._sheet, self._catalog)


class TitleBlockItem(QGraphicsItem):
    """Bottom info boxes. High z so wires never paint over it."""

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session
        self.setZValue(80)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        x, y, w, h = title_block_rect(session.sheet())
        self._rect = QRectF(x, y, w, h)

    def boundingRect(self) -> QRectF:
        return self._rect

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        sheet = self.session.sheet()
        idx = self.session.project.sheets.index(sheet)
        base = self.session.path.parent if self.session.path else None
        draw_title_block_only(
            QtPainter(painter),
            self.session.project,
            sheet,
            sheet_index=idx,
            sheet_count=len(self.session.project.sheets),
            base_dir=base,
            catalog=self.session.catalog,
        )


class PanelWiringOverlayItem(QGraphicsItem):
    """Draws backpanel wire routing, zip-tie bundles, and technician walkthrough overlays."""

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session
        self.setZValue(65)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        w, h = session.sheet().page.dimensions()
        self._rect = QRectF(0, 0, w, h)

    def boundingRect(self) -> QRectF:
        return self._rect

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        from panelcad.render.panel_wiring_render import draw_panel_wiring_overlay

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        sheet = self.session.sheet()
        draw_panel_wiring_overlay(
            QtPainter(painter),
            sheet,
            self.session.catalog,
            self.session.project,
        )


class WireByWireSheetItem(QGraphicsItem):
    """Draws the dedicated Wire-by-Wire Cut Sheet table on canvas."""

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session
        self.setZValue(30)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        w, h = session.sheet().page.dimensions()
        self._rect = QRectF(0, 0, w, h)

    def boundingRect(self) -> QRectF:
        return self._rect

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        from panelcad.render.panel_wiring_render import draw_wire_by_wire_sheet

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        sheet = self.session.sheet()
        draw_wire_by_wire_sheet(
            QtPainter(painter),
            sheet,
            self.session.catalog,
            self.session.project,
        )


class LogoSlotItem(QGraphicsRectItem):
    """Click the LOGO cell to pick an image."""

    def __init__(self, rect: tuple[float, float, float, float], on_click) -> None:
        super().__init__(QRectF(*rect))
        self._on_click = on_click
        self.setPen(QPen(Qt.PenStyle.NoPen))
        self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.setZValue(90)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to insert company logo")
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_click()
            event.accept()
            return
        super().mousePressEvent(event)


def _paint_primitive(painter: QPainter, prim: Primitive) -> None:
    if prim.type == "rect":
        painter.setBrush(QBrush(_qc("paper")))
        painter.drawRect(QRectF(prim.x, prim.y, prim.w, prim.h))
        painter.setBrush(Qt.BrushStyle.NoBrush)
    elif prim.type == "line":
        painter.drawLine(QPointF(prim.x, prim.y), QPointF(prim.x2, prim.y2))
    elif prim.type == "circle":
        painter.setBrush(QBrush(_qc("paper")))
        painter.drawEllipse(QPointF(prim.x, prim.y), prim.r, prim.r)
        painter.setBrush(Qt.BrushStyle.NoBrush)
    elif prim.type == "arc":
        rect = QRectF(prim.x - prim.r, prim.y - prim.r, prim.r * 2.0, prim.r * 2.0)
        path = QPainterPath()
        path.arcMoveTo(rect, prim.start)
        path.arcTo(rect, prim.start, prim.sweep)
        painter.strokePath(path, painter.pen())
    elif prim.type == "polyline":
        path = QPainterPath()
        if not prim.points:
            return
        path.moveTo(*prim.points[0])
        for x, y in prim.points[1:]:
            path.lineTo(x, y)
        painter.drawPath(path)
    elif prim.type == "text":
        draw_world_text(painter, prim.x, prim.y, prim.text, max(2.2, prim.size), align="middle")


def _library_id_from_mime(mime) -> str | None:
    if mime.hasFormat(LIBRARY_MIME):
        return bytes(mime.data(LIBRARY_MIME)).decode("utf-8")
    return None


def _bom_from_mime(mime) -> dict | None:
    if not mime.hasFormat(BOM_MIME):
        return None
    import json

    try:
        return json.loads(bytes(mime.data(BOM_MIME)).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None


_SEV_RANK = {"error": 0, "warning": 1, "info": 2}
PIN_DRAW_R = 3.2
MARKER_STANDOFF = 1.0 * SNAP  # 1–2 grid units off the drawn top-right


def _host_component(text: str, components: set[str]) -> str:
    """``S1`` or ``S1.13`` → ``S1`` when that device is on the sheet."""

    raw = str(text or "").strip()
    if raw in components:
        return raw
    if "." not in raw:
        return ""
    from panelcad.render.sheet import parse_pin_ref

    try:
        cid, _pid = parse_pin_ref(raw)
    except ValueError:
        cid = raw.split(".", 1)[0]
    return cid if cid in components else ""


def _tag_drawn_locally(item: "ComponentItem") -> bool:
    """False when the ref is painted inside the body (coil / layout / card)."""

    if item.layout or item.network or item.io_card or getattr(item, "ladder_card", False):
        return False
    token = f"{getattr(item.device, 'id', '')} {getattr(item.device, 'library_id', '')}".lower()
    if "aux" in token:
        return True
    return "coil" not in token


def _primitive_ink_rect(prim: Primitive, scale: float) -> QRectF:
    """Local (already scaled) bounds of one library draw command."""

    s = float(scale)
    pad = 0.45
    if prim.type == "rect":
        return QRectF(prim.x * s - pad, prim.y * s - pad, prim.w * s + 2 * pad, prim.h * s + 2 * pad)
    if prim.type == "circle" or prim.type == "arc":
        return QRectF(
            (prim.x - prim.r) * s - pad,
            (prim.y - prim.r) * s - pad,
            prim.r * 2.0 * s + 2 * pad,
            prim.r * 2.0 * s + 2 * pad,
        )
    if prim.type == "line":
        x0, x1 = min(prim.x, prim.x2) * s, max(prim.x, prim.x2) * s
        y0, y1 = min(prim.y, prim.y2) * s, max(prim.y, prim.y2) * s
        return QRectF(x0 - pad, y0 - pad, max(0.8, x1 - x0) + 2 * pad, max(0.8, y1 - y0) + 2 * pad)
    if prim.type == "polyline" and prim.points:
        xs = [pt[0] * s for pt in prim.points]
        ys = [pt[1] * s for pt in prim.points]
        return QRectF(min(xs) - pad, min(ys) - pad, max(xs) - min(xs) + 2 * pad, max(ys) - min(ys) + 2 * pad)
    if prim.type == "text" and prim.text:
        tw = max(2.0, len(prim.text) * float(prim.size or 3.0) * 0.7) * s
        th = float(prim.size or 3.0) * 1.2 * s
        return QRectF(prim.x * s - tw * 0.5, prim.y * s - th, tw, th)
    return QRectF()


def _symbol_ink_rect(item: "ComponentItem") -> QRectF:
    """Ink the painter actually puts down — not the empty schematic allocation box."""

    w, h, scale = float(item._w), float(item._h), float(item._scale)
    if item.layout or item.network or item.io_card:
        return QRectF(0.0, 0.0, w, h)
    from panelcad.render.painter import is_control_coil

    if is_control_coil(item.device, item.device.library_id):
        pin_y = 14.0 * scale
        for pin in item.device.pins:
            if str(pin.id).upper() == "A1":
                pin_y = float(pin.y) * scale
                break
        half = min(w, h) * 0.32
        box = QRectF(w * 0.5 - half, pin_y - half, half * 2.0, half * 2.0)
        return box.united(QRectF(0.0, pin_y - 0.4, w, 0.8))
    box = QRectF()
    for raw in item.device.primitives:
        prim = raw if isinstance(raw, Primitive) else Primitive.model_validate(raw)
        piece = _primitive_ink_rect(prim, scale)
        if not piece.isEmpty():
            box = piece if box.isEmpty() else box.united(piece)
    if box.isEmpty():
        return QRectF(0.0, 0.0, w, h)
    return box


def finding_content_rect(item: "ComponentItem") -> QRectF:
    """Tight local rect of the drawn symbol, tag, and pin dots — not hit boxes."""

    box = _symbol_ink_rect(item)
    scale = float(item._scale)
    if _tag_drawn_locally(item):
        tag = str(item.component_id or "")
        tw = max(3.0, len(tag) * 3.0 * 0.72)
        th = 3.0 * 1.2
        box = box.united(QRectF(0.0, -2.5 * scale - th, tw, th + 0.8))
        view = item.scene().views()[0] if item.scene() and item.scene().views() else None
        if _is_schematic_view(view):
            try:
                from panelcad.core.pins import extra_captions

                comp = view.session.sheet().component(item.component_id)
                y_cap = -5.6 * scale
                for line in extra_captions(comp):
                    lw = max(3.0, len(line) * 2.2 * 0.72)
                    box = box.united(QRectF(0.0, y_cap - 2.4, lw, 3.0))
                    y_cap -= 3.1 * scale
            except (KeyError, AttributeError, TypeError):
                pass
    for child in item.childItems():
        if not isinstance(child, PinItem):
            continue
        pin = QRectF(-PIN_DRAW_R, -PIN_DRAW_R, PIN_DRAW_R * 2.0, PIN_DRAW_R * 2.0)
        box = box.united(child.mapRectToParent(pin))
    return box


def device_drawn_scene_bounds(item: "ComponentItem") -> QRectF:
    """Symbol + tag + pins in scene coordinates after the item's transform."""

    return item.mapRectToScene(finding_content_rect(item))


def wire_drawn_scene_bounds(item: "WireItem") -> QRectF:
    """The stroked path only — not the inflated label / hit box."""

    return item.mapRectToScene(item.path().boundingRect())


def marker_pos_off_top_right(bounds: QRectF, marker_br: QRectF, gap: float = MARKER_STANDOFF) -> QPointF:
    """Item pos so the badge sits *gap* off the top-right corner of *bounds*."""

    return QPointF(bounds.right() + gap - marker_br.left(), bounds.top() - gap - marker_br.bottom())


def place_clear_of_devices(pos: QPointF, marker_br: QRectF, obstacles: list[QRectF]) -> QPointF:
    """Nudge right, then up, so the marker rect misses every device box."""

    placed = QPointF(pos)
    for _ in range(32):
        rect = marker_br.translated(placed)
        if not any(rect.intersects(box) for box in obstacles):
            return placed
        right = QPointF(placed.x() + SNAP, placed.y())
        if not any(marker_br.translated(right).intersects(box) for box in obstacles):
            return right
        placed.setY(placed.y() - SNAP)
    return placed


def group_finding_rows(
    rows: list[dict[str, str]],
    components: set[str],
    wires: set[str],
    hosts: dict[str, str] | None = None,
) -> list[tuple[str, str, list[dict[str, str]]]]:
    """One group per device (or wire). Worst severity first inside each group.

    *hosts* maps a wire id (or other ref) onto a live device so a dangling
    wire parks on the end that still exists.
    """

    hosts = hosts or {}
    groups: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in rows:
        ref = str(row.get("ref") or "")
        target = str(row.get("target") or "")
        host = (
            (ref if ref in components else "")
            or (target if target in components else "")
            or _host_component(target, components)
            or _host_component(ref, components)
            or (hosts[ref] if hosts.get(ref) in components else "")
            or (hosts[target] if hosts.get(target) in components else "")
        )
        if host:
            key = ("comp", host)
        elif ref in wires:
            key = ("wire", ref)
        else:
            key = ("loose", ref or str(row.get("id") or ""))
        groups.setdefault(key, []).append(row)
    out: list[tuple[str, str, list[dict[str, str]]]] = []
    for (kind, ident), items in groups.items():
        items = sorted(items, key=lambda row: _SEV_RANK.get(str(row.get("severity") or ""), 9))
        out.append((kind, ident, items))
    return out


class FindingMarker(QGraphicsItem):
    """Canvas badge for one device (or wire). Never sits on the symbol, tag, or pins."""

    def _color(self) -> QColor:
        return {
            "error": _qc("finding_error"),
            "warning": _qc("finding_warning"),
            "info": _qc("finding_info"),
        }.get(self.severity(), _qc("finding_info"))

    def __init__(
        self,
        row: dict[str, str],
        parent: QGraphicsItem | None = None,
        *,
        count: int = 1,
        rows: list[dict[str, str]] | None = None,
        anchor_ref: str = "",
    ) -> None:
        super().__init__(parent)
        self.row = row
        self.count = max(1, int(count))
        self.rows = list(rows or [row])
        self.anchor_ref = anchor_ref or str(row.get("ref") or "")
        self._leader: QGraphicsLineItem | None = None
        self.setZValue(40)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lines = [f"{item.get('severity', '')}: {item.get('message', '')}" for item in self.rows]
        if self.count > 1:
            lines.insert(0, f"{self.count} findings")
        self.setToolTip("\n".join(lines))

    def set_leader(self, scene_tip: QPointF) -> None:
        """Thin line from the badge toward the device's drawn top-right."""

        local = self.mapFromScene(scene_tip)
        br = self.boundingRect()
        if self._leader is None:
            self._leader = QGraphicsLineItem(self)
            self._leader.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            pen = QPen(_qc("finding_leader"), 0.0)
            pen.setCosmetic(True)
            self._leader.setPen(pen)
            self._leader.setZValue(-1)
        self._leader.setLine(br.left() + 0.4, br.bottom() - 0.4, local.x(), local.y())

    def severity(self) -> str:
        return str(self.row.get("severity") or "info")

    def shape_kind(self) -> str:
        """info is a disc; error/warning stay a triangle."""

        return "circle" if self.severity() == "info" else "triangle"

    def boundingRect(self) -> QRectF:
        extra = 6.2 if self.count > 1 else 0.0
        return QRectF(-5.6, -6.4, 11.2 + extra, 12.8)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        del option, widget
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = self._color()
        painter.setBrush(QBrush(color))
        painter.setPen(QPen(_qc("ink"), 0.35))
        if self.shape_kind() == "circle":
            painter.drawEllipse(QPointF(0.0, 0.0), 4.6, 4.6)
            glyph = "i"
        else:
            painter.drawPolygon(QPolygonF([QPointF(0.0, -5.6), QPointF(4.8, 4.8), QPointF(-4.8, 4.8)]))
            glyph = "!"
        painter.setPen(QPen(_qc("finding_glyph"), 0.2))
        font = QFont()
        font.setPointSizeF(3.4)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(-3.6, -3.4, 7.2, 7.0), Qt.AlignmentFlag.AlignCenter, glyph)
        if self.count > 1:
            painter.setPen(QPen(color, 0.2))
            painter.setBrush(QBrush(_qc("finding_badge")))
            painter.drawRoundedRect(QRectF(4.2, -2.4, 6.0, 5.0), 1.1, 1.1)
            painter.setPen(QPen(color, 0.2))
            small = QFont()
            small.setPointSizeF(2.8)
            small.setBold(True)
            painter.setFont(small)
            painter.drawText(QRectF(4.2, -2.4, 6.0, 5.0), Qt.AlignmentFlag.AlignCenter, str(self.count))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        view = self.scene().views()[0] if self.scene() and self.scene().views() else None
        if _is_schematic_view(view):
            view.finding_clicked.emit(self.row)
        event.accept()


class XrefLabelItem(QGraphicsItem):
    """Clickable coil/contact/off-page reference. Jumps like a Problems finding."""

    def __init__(self, ann, x: float, y: float, align: str) -> None:
        super().__init__()
        from panelcad.core.xref import jump_row

        self.ann = ann
        self._x = float(x)
        self._y = float(y)
        self._align = align
        self.row = jump_row(ann)
        lines = list(ann.lines)
        if getattr(ann, "kind", "") == "mirror" and getattr(ann, "mirror_rows", None):
            from panelcad.render.xref import mirror_size

            width, height = mirror_size(ann)
            self._box = QRectF(-width / 2.0 - 0.4, -1.0, width + 0.8, height + 1.2)
        else:
            width = max((len(line) for line in lines), default=1) * ann.size * 0.64
            height = max(2.6, len(lines) * 2.6)
            if align == "end":
                x0 = -width
            elif align == "middle":
                x0 = -width / 2.0
            else:
                x0 = 0.0
            self._box = QRectF(x0 - 0.4, -ann.size, width + 0.8, height + 1.2)
        self.setPos(self._x, self._y)
        self.setZValue(25)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setToolTip(" ".join(lines) or "cross-reference")

    def boundingRect(self) -> QRectF:
        return self._box

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        del option, widget
        from panelcad.render.qt_painter import QtPainter
        from panelcad.render.xref import draw_annotation

        draw_annotation(QtPainter(painter), self.ann, 0.0, 0.0, self._align)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        view = self.scene().views()[0] if self.scene() and self.scene().views() else None
        if _is_schematic_view(view):
            view.finding_clicked.emit(self.row)
        event.accept()


class TraceHalo(QGraphicsRectItem):
    """Dashed box around a device on the troubleshooting path. Not selectable."""

    def __init__(self, rect: QRectF, *, focus: bool) -> None:
        pad = 2.2 if focus else 1.2
        super().__init__(rect.adjusted(-pad, -pad, pad, pad))
        pen = QPen(_qc("highlight"), 0.8 if focus else 0.4)
        if not focus:
            pen.setStyle(Qt.PenStyle.DashLine)
        self.setPen(pen)
        self.setBrush(Qt.BrushStyle.NoBrush)
        self.setZValue(35 if focus else 32)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)


class TraceWireMark(QGraphicsPathItem):
    """Highlight along a wire number that is on the troubleshooting path."""

    def __init__(self, path: QPainterPath, *, focus: bool) -> None:
        super().__init__(path)
        pen = QPen(_qc("highlight"), 1.3 if focus else 0.75)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        if not focus:
            pen.setStyle(Qt.PenStyle.DashLine)
        self.setPen(pen)
        self.setBrush(Qt.BrushStyle.NoBrush)
        self.setZValue(19)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)


@dataclass
class _SheetScene:
    """One built sheet. Inactive slots are released when the cache is over its cap."""

    sheet_id: str
    stamp: str
    scene: QGraphicsScene
    items: list[QGraphicsItem]


