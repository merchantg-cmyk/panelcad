from __future__ import annotations

import math
import re
from collections import OrderedDict
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QCursor,
    QFont,
    QMouseEvent,
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
    QGraphicsView,
    QMenu,
    QStyleOptionGraphicsItem,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.core.layout import clip_offset, is_io_sheet, is_layout_sheet, is_network_sheet, rail_height
from panelcad.library.ports import effective_ports, network_node_caption
from panelcad.render.io_ladder import io_card_size, io_pin_layout, is_io_module
from panelcad.render.network import node_size, port_layout
from panelcad.core.models import Component, Device, Enclosure, PinOverride, Rail, infer_pin_side
from panelcad.core.primitives import Primitive
from panelcad.render.painter import (
    _draw_column_selector,
    _draw_on_delay_coil,
    _primitive_is_lower_pole,
    draw_control_coil,
)
from panelcad.render.qt_painter import QtPainter, draw_world_text
from panelcad.core.geometry import Point
from panelcad.core.route import nearest_run, set_run_channel
from panelcad.render.sheet import iter_wire_points
from panelcad.render.titleblock import (
    company_logo_rect,
    draw_sheet_chrome,
    draw_title_block_only,
    title_block_rect,
    working_rect,
)

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
        return view if isinstance(view, SchematicView) else None

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
        if not isinstance(view, SchematicView) or self.layout:
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
        if isinstance(view, SchematicView):
            view.session.drop_wire_routes({self.component_id})
            view._redraw_wires()

    def resize_to_local(self, local: QPointF) -> None:
        sx = abs(local.x()) / max(self._base_w, 1.0)
        sy = abs(local.y()) / max(self._base_h, 1.0)
        self.apply_scale(max(sx, sy))

    def commit_scale(self) -> None:
        scene = self.scene()
        view = scene.views()[0] if scene and scene.views() else None
        if isinstance(view, SchematicView):
            view.session.set_scale(self.component_id, self._scale)
            view.project_changed.emit()
            view.status.emit(f"{self.component_id} scale {self._scale:.2f} — drag the orange handle to resize")

    def _layout_picture_path(self):
        from panelcad.render.titleblock import resolve_logo

        scene = self.scene()
        view = scene.views()[0] if scene and scene.views() else None
        if not isinstance(view, SchematicView):
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
        if isinstance(view, SchematicView):
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
        if not isinstance(view, SchematicView):
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
            if isinstance(view, SchematicView):
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
        if isinstance(view, SchematicView):
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
        if isinstance(view, SchematicView):
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
            if isinstance(view, SchematicView):
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
            if isinstance(view, SchematicView):
                try:
                    comp = view.session.sheet().component(self.component_id)
                except KeyError:
                    comp = None
            y_cap = -5.6 * self._scale
            io_field = isinstance(view, SchematicView) and (not self.io_card) and view.session.sheet().kind == "io"
            ladder = isinstance(view, SchematicView) and str(
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
        applying = isinstance(view, SchematicView) and view._applying_model_pos
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and not applying:
            pt: QPointF = value
            step = self._snap
            return QPointF(round(pt.x() / step) * step, round(pt.y() / step) * step)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged and not applying:
            if isinstance(view, SchematicView) and view._gesture_edit and view.session.command_open():
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
        return view if isinstance(view, SchematicView) else None

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
        from panelcad.core.layout import enclosure_drawn_size

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
        if isinstance(view, SchematicView):
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
        if isinstance(view, SchematicView):
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
        if isinstance(view, SchematicView):
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


class SchematicView(QGraphicsView):
    status = Signal(str)
    pin_clicked = Signal(str)
    selection_changed = Signal()
    logo_clicked = Signal()
    project_changed = Signal()
    finding_clicked = Signal(dict)
    object_edit_requested = Signal(str)
    rename_requested = Signal(str)
    address_edit_requested = Signal(str)
    wire_label_requested = Signal(str)
    wire_number_requested = Signal(str)
    wire_spec_requested = Signal(str)
    delete_sheet_requested = Signal()
    close_sheet_requested = Signal()
    new_sheet_requested = Signal()
    rename_sheet_requested = Signal()
    pack_requested = Signal()
    view_changed = Signal()

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.mode = "select"  # select | place | wire | cloud
        self.place_library_id: str | None = None
        self.wire_from: str | None = None
        self._wire_from_at: QPointF | None = None
        self._wire_drag = False
        self._wire_preview: QGraphicsPathItem | None = None
        self._cloud_origin: QPointF | None = None
        self._cloud_preview: QGraphicsPathItem | None = None
        self._slide_pin: PinItem | None = None
        self._slide_moved = False
        self._pull_wire: WireItem | None = None
        self._pull_pts: list[Point] | None = None
        self._pull_seg = -1
        self._pull_kind = ""
        self._pull_moved = False
        # PySide destroys QGraphicsItems when the last Python wrapper is GC'd.
        # Scene.addItem is not enough — keep every Python-created item here.
        self._items: list[QGraphicsItem] = []
        self._panning = False
        self._pan_start = QPointF()
        self._middle_press_pos: QPointF | None = None
        self._middle_hit_device: ComponentItem | None = None
        self._middle_hit_wire: WireItem | None = None
        self._left_press_pos: QPointF | None = None
        self._left_empty_press = False
        self._left_select_drag = False
        self._hover_wire: WireItem | None = None
        self._wire_hit_scale = -1.0
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        # Do not re-center on dock/inspector resize — that was scrolling the title block off-screen.
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self._moved_since_press = False
        self._dragged_ids: set[str] = set()
        self._gesture_edit = False
        self._closing_gesture = False
        self._applying_model_pos = False
        self._shown_sheet_id = ""
        self._sheet_scenes: OrderedDict[str, _SheetScene] = OrderedDict()
        self._spare_scenes: list[tuple[QGraphicsScene, list[QGraphicsItem]]] = []
        self._strip_caption = ""
        self._findings: list[dict[str, str]] = []
        self._trace_highlight = None
        self.setBackgroundBrush(QBrush(_qc("paper")))
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.scale(3.0, 3.0)
        self._user_zoomed = False
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_if_automatic)
        self.scene.selectionChanged.connect(self.selection_changed)
        self.reload()

    def _add(self, item: QGraphicsItem) -> QGraphicsItem:
        """Add *item* to the scene and hold a Python reference so PySide cannot GC it."""
        self._items.append(item)
        self.scene.addItem(item)
        return item

    def _device_tag_layouts(self) -> dict[str, tuple[float, float, str, float]]:
        """Same tag seats the sheet painter uses, so the screen matches the print."""

        from panelcad.core.ladder import sheet_drawing_style
        from panelcad.render.label_avoid import resolve_labels

        sheet = self.session.sheet()
        style = sheet_drawing_style(sheet, self.session.project)
        elementary = style in {"ladder", "iec"} and sheet.kind == "schematic"
        try:
            return resolve_labels(
                self.session.project,
                self.session.catalog,
                sheet,
                ladder=elementary,
            ).tags
        except (KeyError, ValueError, IndexError):
            return {}

    def reload(self) -> None:
        """Redraw the active sheet. A recent sheet is shown again without rebuilding.

        Only a few scenes stay in memory. The rest are released. A scene is
        reused only when the project, the library symbols, and the theme still
        match the picture that was built.
        """

        from panelcad.memory import scene_cache_slots
        from panelcad.render.label_avoid import share_resolved_labels
        from panelcad.render.wire_labels import share_wire_marks

        self._discard_preview()
        self._drop_deleted_sheets()
        sheet = self.session.sheet()
        stamp = self._scene_stamp()
        self._drop_stale_sheet_scenes(stamp)
        cached = self._sheet_scenes.get(sheet.id)
        if (
            cached is not None
            and cached.stamp == stamp
            and cached.scene is not None
            and self._cached_picture_matches(cached)
        ):
            self._mount_sheet_scene(cached)
            self._sheet_scenes.move_to_end(sheet.id)
            self._draw_finding_markers()
            self._draw_trace_highlight()
            self.status.emit(self._status_text())
            self.project_changed.emit()
            return
        self._prepare_build_scene(scene_cache_slots())
        with share_wire_marks(), share_resolved_labels():
            self._reload_sheet()
        self._remember_sheet_scene(sheet.id, stamp)
        self._evict_sheet_scenes(scene_cache_slots())

    def _scene_stamp(self) -> str:
        """Project stamp, reused while the drawing and the theme stay put.

        Hashing the whole project on every sheet change made a walk slower
        than clearing the one scene. The hash is taken again when an edit
        bumps the generation, the project object is replaced, or a symbol changes.
        """

        from panelcad.render.label_avoid import _digest, _library_key, project_drawing_stamp
        from panelcad.theme import get_canvas, get_theme

        project = self.session.project
        catalog = self.session.catalog
        theme = (get_theme(), get_canvas())
        key = (
            int(getattr(self.session, "_drawing_generation", 0)),
            id(project),
            id(catalog),
            _digest(_library_key(project, catalog)),
            theme,
        )
        if key == getattr(self, "_scene_stamp_key", None):
            return self._scene_stamp_value
        stamp = f"{project_drawing_stamp(project, catalog)}|{theme[0]}|{theme[1]}"
        self._scene_stamp_key = key
        self._scene_stamp_value = stamp
        return stamp

    def _discard_preview(self) -> None:
        """Drop a rubber-band so a parked sheet does not keep it."""

        self._clear_wire_hover()
        self._wire_hit_scale = -1.0
        for attr in ("_wire_preview", "_cloud_preview"):
            item = getattr(self, attr, None)
            if item is None:
                continue
            if item in self._items:
                self._items.remove(item)
            scene = item.scene()
            if scene is not None:
                scene.removeItem(item)
            setattr(self, attr, None)
        self._cloud_origin = None

    def _scene_is_cached(self, scene: QGraphicsScene | None) -> bool:
        return any(slot.scene is scene for slot in self._sheet_scenes.values())

    def _prepare_build_scene(self, cap: int) -> None:
        """Keep a cached picture, and draw the next sheet on a scene we already own.

        Allocating a new scene for every sheet made switching slower than
        clearing the one scene. The pool stays at the cache cap.
        """

        if not self._scene_is_cached(self.scene) or cap <= 1:
            return
        if self._spare_scenes:
            scene, items = self._spare_scenes.pop()
            self.scene = scene
            self._items = items
            self.setScene(scene)
            return
        if len(self._sheet_scenes) >= cap:
            for sid, slot in self._sheet_scenes.items():
                if slot.scene is self.scene:
                    continue
                self._sheet_scenes.pop(sid)
                self.scene = slot.scene
                self._items = slot.items
                self.setScene(slot.scene)
                return
        scene = QGraphicsScene(self)
        scene.selectionChanged.connect(self.selection_changed)
        self.scene = scene
        self._items = []
        self.setScene(scene)

    def _mount_sheet_scene(self, slot: _SheetScene) -> None:
        self._items = slot.items
        self._shown_sheet_id = slot.sheet_id
        if self.scene is not slot.scene:
            self.scene = slot.scene
            self.setScene(slot.scene)
        # A full rebuild used to clear the scene, which dropped the selection.
        slot.scene.blockSignals(True)
        try:
            slot.scene.clearSelection()
        finally:
            slot.scene.blockSignals(False)
        # The parked picture can still sit where a drag left it. The model wins.
        self._resync_items_from_model()

    def _release_mouse_grab(self) -> None:
        scene = self.scene
        if scene is None:
            return
        grabber = scene.mouseGrabberItem()
        if grabber is not None:
            grabber.ungrabMouse()

    def _resync_items_from_model(self) -> None:
        """Move the devices on the open scene to the coordinates in the model."""

        if self.scene is None:
            return
        try:
            sheet = self.session.sheet()
        except KeyError:
            return
        by_id = {comp.id: comp for comp in sheet.components}
        self._applying_model_pos = True
        try:
            for item in self.scene.items():
                if not isinstance(item, ComponentItem):
                    continue
                if item.sheet_id and item.sheet_id != sheet.id:
                    continue
                comp = by_id.get(item.component_id)
                if comp is None:
                    continue
                item.setPos(float(comp.x), float(comp.y))
                item._model_xy = (float(comp.x), float(comp.y))
        finally:
            self._applying_model_pos = False

    def settle_before_sheet_change(self) -> None:
        """Drop a half-finished drag before its scene is parked.

        The mouse button may still be down. Ungrabbing stops the later release
        from sliding the device again after the model has stayed put.
        """

        self._release_mouse_grab()
        self._resync_items_from_model()
        self._moved_since_press = False
        self._dragged_ids.clear()

    def _remember_sheet_scene(self, sheet_id: str, stamp: str) -> None:
        # Reusing the live scene means it no longer belongs to the sheet that
        # was on it. Drop that slot or a one-slot cache keeps both names.
        for sid, slot in list(self._sheet_scenes.items()):
            if slot.scene is self.scene:
                self._sheet_scenes.pop(sid, None)
        previous = self._sheet_scenes.pop(sheet_id, None)
        if previous is not None and previous.scene is not self.scene:
            self._release_sheet_scene(previous)
        self._sheet_scenes[sheet_id] = _SheetScene(sheet_id, stamp, self.scene, self._items)
        self._sheet_scenes.move_to_end(sheet_id)

    def _cached_picture_matches(self, slot: _SheetScene) -> bool:
        """A parked scene is reused only when its devices still sit on the model.

        A drag, a wizard cancel, or a direct edit can move the model without
        rebuilding the picture. The numbers on the model win.
        """

        sheet = self.session.sheet()
        if slot.sheet_id != sheet.id:
            return False
        by_id = {comp.id: comp for comp in sheet.components}
        devices = [item for item in slot.items if isinstance(item, ComponentItem)]
        if {item.component_id for item in devices} != set(by_id):
            return False
        for item in devices:
            comp = by_id[item.component_id]
            if abs(float(item.pos().x()) - float(comp.x)) > 0.05:
                return False
            if abs(float(item.pos().y()) - float(comp.y)) > 0.05:
                return False
        return True

    def _drop_deleted_sheets(self) -> None:
        """A deleted page does not keep an empty scene."""

        live = {sheet.id for sheet in self.session.project.sheets}
        removed = False
        for sid in [sid for sid in self._sheet_scenes if sid not in live]:
            slot = self._sheet_scenes.pop(sid)
            self._destroy_sheet_scene(slot)
            removed = True
        if removed:
            self._trim_spare_scenes()

    def _destroy_sheet_scene(self, slot: _SheetScene) -> None:
        scene = slot.scene
        items = slot.items
        slot.items = []
        slot.scene = None  # type: ignore[assignment]
        items.clear()
        if scene is None:
            return
        self._spare_scenes = [(kept, bag) for kept, bag in self._spare_scenes if kept is not scene]
        if scene is self.scene:
            return
        scene.clear()
        scene.deleteLater()

    def _trim_spare_scenes(self) -> None:
        while self._spare_scenes:
            scene, items = self._spare_scenes.pop()
            items.clear()
            if scene is self.scene:
                continue
            scene.clear()
            scene.deleteLater()

    def _drop_stale_sheet_scenes(self, stamp: str) -> None:
        stale = [sid for sid, slot in self._sheet_scenes.items() if slot.stamp != stamp]
        for sid in stale:
            slot = self._sheet_scenes.pop(sid)
            if slot.scene is self.scene:
                continue
            self._release_sheet_scene(slot)

    def _evict_sheet_scenes(self, cap: int) -> None:
        cap = max(1, int(cap))
        guard = len(self._sheet_scenes) + 1
        while len(self._sheet_scenes) > cap and guard:
            guard -= 1
            sid, slot = self._sheet_scenes.popitem(last=False)
            if slot.scene is self.scene:
                self._sheet_scenes[sid] = slot
                self._sheet_scenes.move_to_end(sid)
                if len(self._sheet_scenes) == 1:
                    return
                continue
            self._release_sheet_scene(slot)

    def release_scenes(self) -> None:
        """Drop every Python reference to scene items, then clear the scenes.

        Called when the window closes, before Qt frees the canvas. Same order
        as ``_release_sheet_scene``: lists first, then ``scene.clear()``, so
        no item wrapper outlives the C++ item it points at.
        """

        scenes: list[QGraphicsScene] = []

        def keep(scene) -> None:
            if scene is not None and all(scene is not known for known in scenes):
                scenes.append(scene)

        keep(self.scene)
        for slot in list(self._sheet_scenes.values()):
            keep(slot.scene)
            slot.items.clear()
            slot.scene = None  # type: ignore[assignment]
        self._sheet_scenes.clear()
        for scene, items in self._spare_scenes:
            keep(scene)
            items.clear()
        self._spare_scenes = []
        self._items.clear()
        self._wire_preview = None
        self._cloud_preview = None
        self._cloud_origin = None
        for scene in scenes:
            scene.blockSignals(True)
            scene.clear()

    def _release_sheet_scene(self, slot: _SheetScene) -> None:
        scene = slot.scene
        items = slot.items
        slot.items = []
        slot.scene = None  # type: ignore[assignment]
        if scene is None or scene is self.scene:
            items.clear()
            return
        items.clear()
        scene.clear()
        self._spare_scenes.append((scene, items))

    def _reload_sheet(self) -> None:
        self._clear_wire_hover()
        self._wire_hit_scale = -1.0
        self.setBackgroundBrush(QBrush(_qc("paper")))
        self._items.clear()
        self._wire_preview = None
        self._cloud_preview = None
        self._cloud_origin = None
        self.scene.clear()
        sheet = self.session.sheet()
        self._shown_sheet_id = sheet.id
        w, h = sheet.page.dimensions()
        self.scene.setSceneRect(0, 0, w, h)
        self._draw_grid(sheet)
        self._add(SheetChromeItem(self.session))
        self._add(TitleBlockItem(self.session))
        self._add(LogoSlotItem(company_logo_rect(sheet), self.logo_clicked.emit))
        if sheet.kind == "terminal":
            self._draw_terminal_strip(sheet)
            self._draw_revision_clouds()
            self._draw_trace_highlight()
            self.status.emit(self._status_text())
            self.project_changed.emit()
            return
        layout = is_layout_sheet(sheet)
        network = is_network_sheet(sheet)
        io_page = is_io_sheet(sheet)
        mm_scale = 1.0
        if sheet.enclosure is not None:
            self._add(EnclosureItem(sheet.enclosure))
            if sheet.enclosure.draw_scale and sheet.enclosure.draw_scale > 0:
                mm_scale = float(sheet.enclosure.draw_scale)
        for tray in sheet.trays:
            self._add(TrayItem(tray, mm_scale=mm_scale))
        for rail in sheet.rails:
            self._add(RailItem(rail, mm_scale=mm_scale))
        tags = self._device_tag_layouts() if sheet.kind == "schematic" and not layout and not network else {}
        for comp in sheet.components:
            device = self.session.catalog.get(comp.library_id)
            item = ComponentItem(
                comp,
                device,
                layout=layout,
                network=network,
                io_card=io_page and is_io_module(device) and not comp.properties.get("ladder_card"),
                sheet_id=sheet.id,
            )
            if comp.id in tags:
                item.set_tag_layout(tags[comp.id])
            self._add(item)
        if not layout:
            self._redraw_wires()
        self._apply_pin_mode()
        self._draw_finding_markers()
        self._draw_revision_clouds()
        self._draw_trace_highlight()
        self.status.emit(self._status_text())
        self.project_changed.emit()

    def _status_text(self) -> str:
        sheet = self.session.sheet()
        extra = ""
        if self.mode == "place" and self.place_library_id:
            extra = f"  ·  PLACE {self.place_library_id} (click empty sheet or drop from library)"
        elif self.mode == "cloud":
            extra = "  ·  CLOUD  drag a rectangle (Esc cancels)"
        elif self.wire_from:
            extra = f"  ·  wiring from {self.wire_from} — click or release on the other terminal or rail (Esc cancels)"
        elif self._pull_wire is not None:
            extra = "  ·  dragging wire — release to keep, Esc to cancel"
        if is_layout_sheet(sheet):
            return (
                f"{sheet.title}  ·  panel layout  ·  {len(sheet.components)} devices  ·  "
                f"{len(sheet.rails)} rails  ·  select a part, drag the orange handle to resize{extra}"
            )
        if is_network_sheet(sheet):
            medium = sheet.network.medium if sheet.network else "mixed"
            return (
                f"{sheet.title}  ·  comms {medium}  ·  {len(sheet.components)} nodes  ·  "
                f"{len(sheet.wires)} links  ·  double-click a node to edit ports{extra}"
            )
        if is_io_sheet(sheet):
            return (
                f"{sheet.title}  ·  I/O ladder  ·  {len(sheet.components)} devices  ·  "
                f"{len(sheet.wires)} rungs  ·  right-click a wire to label it{extra}"
            )
        if sheet.kind == "terminal":
            note = self._strip_caption or "terminal strip"
            return f"{sheet.title}  ·  {note}{extra}"
        return (
            f"{sheet.title}  ·  {len(sheet.components)} devices  ·  "
            f"{len(sheet.wires)} wires  ·  drag a wire run to pull it  ·  "
            f"hover a terminal and click or drag to the next one{extra}"
        )

    def _draw_grid(self, sheet) -> None:
        wx, wy, ww, wh = working_rect(sheet)
        minor = QPen(_qc("grid"), 0.12)
        major = QPen(_qc("grid_major"), 0.22)
        x = wx
        index = 0
        while x <= wx + ww + 0.01:
            line = self.scene.addLine(x, wy, x, wy + wh, major if index % 5 == 0 else minor)
            _ignore_mouse(line)
            self._items.append(line)
            x += 10
            index += 1
        y = wy
        index = 0
        while y <= wy + wh + 0.01:
            line = self.scene.addLine(wx, y, wx + ww, y, major if index % 5 == 0 else minor)
            _ignore_mouse(line)
            self._items.append(line)
            y += 10
            index += 1

    def refresh_frame(self) -> None:
        found_block = False
        found_chrome = False
        for item in list(self.scene.items()):
            if isinstance(item, TitleBlockItem):
                item.update()
                found_block = True
            elif isinstance(item, SheetChromeItem):
                item.update()
                found_chrome = True
        if not found_chrome:
            self._add(SheetChromeItem(self.session))
        if not found_block:
            self._add(TitleBlockItem(self.session))

    def _redraw_wires(self) -> None:
        from panelcad.render.wire_labels import share_wire_marks

        with share_wire_marks():
            self._redraw_wire_items()

    def _redraw_wire_items(self) -> None:
        self._clear_wire_hover()
        self._wire_hit_scale = -1.0
        kept: list[QGraphicsItem] = []
        for item in self._items:
            if isinstance(item, (WireItem, PhaseLeadItem, RailJunctionItem)):
                if item.scene() is self.scene:
                    self.scene.removeItem(item)
            else:
                kept.append(item)
        self._items = kept
        sheet = self.session.sheet()
        created: list[WireItem] = []
        from panelcad.core.ladder import sheet_drawing_style

        style = sheet_drawing_style(sheet, self.session.project)
        ladder = style == "ladder" and sheet.kind == "schematic"
        column = style == "iec" and sheet.kind == "schematic"
        elementary = ladder or column
        from panelcad.core.phase_color import wire_phase_pens
        from panelcad.render.crossings import hop_routes
        from panelcad.render.phase_ink import line_side_runs, pole_runs
        from panelcad.render.label_avoid import resolve_labels
        from panelcad.render.wiring import route_sheet_wires

        try:
            resolved = resolve_labels(
                self.session.project,
                self.session.catalog,
                sheet,
                ladder=elementary,
            )
            routes = resolved.routes
            marks = resolved.marks
        except (KeyError, ValueError, IndexError):
            resolved = None
            try:
                routes = route_sheet_wires(sheet, self.session.catalog, project=self.session.project)
            except (KeyError, ValueError, IndexError):
                routes = {}
            from panelcad.render.wire_labels import place_wire_marks

            marks = place_wire_marks(
                sheet, self.session.catalog, routes, ladder=elementary, project=self.session.project
            )
        pens = wire_phase_pens(self.session.project, sheet, self.session.catalog)
        try:
            leads = [
                *line_side_runs(self.session.project, sheet, self.session.catalog),
                *pole_runs(self.session.project, sheet, self.session.catalog),
            ]
        except (KeyError, ValueError, IndexError):
            leads = []
        for run in leads:
            self._add(
                PhaseLeadItem(
                    run.points,
                    run.pen.stroke,
                    run.pen.width,
                    outline=run.pen.outline,
                    outline_width=run.pen.outline_width,
                )
            )
        hopped = hop_routes(routes)
        for wire in sheet.wires:
            raw = routes.get(wire.id)
            if raw is None:
                try:
                    raw = iter_wire_points(
                        sheet, self.session.catalog, wire, project=self.session.project
                    )
                except (KeyError, ValueError, IndexError):
                    continue
            pts_xy = hopped.get(wire.id) or [(p.x, p.y) for p in raw]
            if not pts_xy:
                continue
            path = QPainterPath()
            path.moveTo(pts_xy[0][0], pts_xy[0][1])
            for x, y in pts_xy[1:]:
                path.lineTo(x, y)
            at = marks.get(wire.id)
            # Print draws nothing when the mark has no seat. A midpoint
            # fallback was painting 107/108/109 that the sheet does not show.
            # Print draws the seated mark and nothing when the mark has no seat.
            # Blanking every elementary caption hid 201–209 on the control sheet.
            caption = "" if at is None else wire.drawn_mark(ladder=elementary)
            pen = pens.get(wire.id)
            item = WireItem(
                path,
                wire.id,
                caption,
                label_xy=(at[0], at[1]) if at else None,
                label_align=at[2] if at else "start",
            )
            if pen is not None:
                from panelcad.theme import adapt_print_color

                outline = QColor(adapt_print_color(pen.outline)) if pen.outline else None
                item.set_screen_pen(QColor(adapt_print_color(pen.stroke)), pen.width, outline)
            else:
                item.set_screen_pen(*_wire_screen_pen(wire, self.session, ladder=elementary))
            if getattr(wire, "inferred", False):
                dashed = QPen(item.pen())
                dashed.setStyle(Qt.PenStyle.DashLine)
                item.setPen(dashed)
                item._base_pen = dashed
            tip = f"Wire {wire.id}  {wire.frm} → {wire.to}"
            if getattr(wire, "inferred", False):
                tip = f"INFERRED (not confirmed)  {tip}"
            if caption:
                tip += f"  [{caption}]"
            tip += "\nDrag a run to pull it. Right-click to label, reset route, or delete."
            item.setToolTip(tip)
            created.append(item)
        for item in created:
            self._add(item)
        from panelcad.core.ladder import rail_landing_points

        for jx, jy in rail_landing_points(sheet, routes, self.session.project):
            self._add(RailJunctionItem(jx, jy))
        self._redraw_xref(ladder=elementary)

    def _redraw_xref(self, *, ladder: bool = False) -> None:
        kept: list[QGraphicsItem] = []
        for item in self._items:
            if isinstance(item, XrefLabelItem):
                if item.scene() is self.scene:
                    self.scene.removeItem(item)
            else:
                kept.append(item)
        self._items = kept
        sheet = self.session.sheet()
        if sheet.kind not in {"schematic", "io"}:
            return
        from panelcad.render.label_avoid import resolve_labels

        try:
            placed = resolve_labels(
                self.session.project,
                self.session.catalog,
                sheet,
                ladder=ladder,
            ).xrefs
        except (KeyError, ValueError, IndexError):
            return
        for ann, x, y, align in placed:
            self._add(XrefLabelItem(ann, x, y, align))

    def _apply_pin_mode(self) -> None:
        layout = is_layout_sheet(self.session.sheet())
        buttons = Qt.MouseButton.NoButton if layout else Qt.MouseButton.LeftButton
        sheet = self.session.sheet()
        for item in self.scene.items():
            if isinstance(item, PinItem):
                item.setAcceptedMouseButtons(buttons)
                try:
                    item.set_locked(sheet.component(item.component_id).pins_locked)
                except KeyError:
                    item.set_locked(True)

    def _pin_item(self, ref: str) -> PinItem | None:
        for item in self.scene.items():
            if isinstance(item, PinItem) and item.ref() == ref:
                return item
        return None

    def _clear_wire_preview(self) -> None:
        if self._wire_preview is not None:
            self.scene.removeItem(self._wire_preview)
            self._wire_preview = None

    def _update_wire_preview(
        self,
        scene_pos: QPointF,
        snap_pin: PinItem | None = None,
        snap_at: QPointF | None = None,
    ) -> None:
        if not self.wire_from:
            self._clear_wire_preview()
            return
        if self._wire_from_at is not None:
            ax, ay = self._wire_from_at.x(), self._wire_from_at.y()
        else:
            start = self._pin_item(self.wire_from)
            if start is None:
                self._clear_wire_preview()
                return
            ax, ay = start.scenePos().x(), start.scenePos().y()
        if snap_pin is not None:
            bx, by = snap_pin.scenePos().x(), snap_pin.scenePos().y()
        elif snap_at is not None:
            bx, by = snap_at.x(), snap_at.y()
        else:
            bx, by = scene_pos.x(), scene_pos.y()
        path = QPainterPath()
        path.moveTo(ax, ay)
        mid = (ax + bx) / 2.0
        path.lineTo(mid, ay)
        path.lineTo(mid, by)
        path.lineTo(bx, by)
        if self._wire_preview is None:
            self._wire_preview = QGraphicsPathItem()
            self._wire_preview.setPen(QPen(_qc("wire_preview"), 0.55, Qt.PenStyle.DashLine))
            self._wire_preview.setZValue(19)
            self._wire_preview.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.scene.addItem(self._wire_preview)
        self._wire_preview.setPath(path)

    def _drop_gesture_flags(self) -> None:
        """Forget a drag without touching the undo stack."""

        self._gesture_edit = False
        self._slide_pin = None
        self._slide_moved = False
        self._pull_wire = None
        self._pull_pts = None
        self._pull_seg = -1
        self._pull_kind = ""
        self._pull_moved = False
        self._moved_since_press = False
        self._dragged_ids.clear()
        self.unsetCursor()

    def drop_open_gesture(self) -> None:
        """Undo or Redo is about to run. Do not record the drag still under the cursor."""

        self._drop_gesture_flags()

    def _finish_gesture(self, *, moved: bool = True) -> None:
        if not self._gesture_edit:
            return
        self._gesture_edit = False
        self.session.end_edit()
        if not moved:
            self.session.forget_noop_checkpoint()

    def _close_open_gesture(self) -> None:
        """End a drag that never received a mouse release.

        A second press, or the canvas losing focus or the mouse grab, used to
        leave the step open. Later edits then nested inside it and pushed nothing.
        """

        if self._closing_gesture or not self._gesture_edit:
            return
        self._closing_gesture = True
        try:
            self._close_open_gesture_body()
        finally:
            self._closing_gesture = False

    def _close_open_gesture_body(self) -> None:
        if not self.session.command_open():
            self._drop_gesture_flags()
            return
        if self._pull_wire is not None:
            moved = self._pull_moved
            self._commit_wire_pull()
            self._finish_gesture(moved=moved)
            return
        if self._slide_pin is not None:
            pin = self._slide_pin
            moved = self._slide_moved
            self._slide_pin = None
            self._slide_moved = False
            if moved:
                self._commit_slide(pin)
            self._finish_gesture(moved=moved)
            return
        user_moved = bool(self._moved_since_press and self._dragged_ids)
        moved = self._commit_symbol_motion(lift=False)
        self._finish_gesture(moved=bool(moved or user_moved))
        self._moved_since_press = False
        self._dragged_ids.clear()

    def _begin_gesture(self, name: str) -> None:
        self._close_open_gesture()
        self.session.begin_edit(name)
        self._gesture_edit = True

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._close_open_gesture()
        super().focusOutEvent(event)

    def viewportEvent(self, event) -> bool:  # noqa: N802
        if event.type() in (QEvent.Type.UngrabMouse, QEvent.Type.FocusOut):
            self._close_open_gesture()
        return super().viewportEvent(event)

    def cancel_wire(self) -> None:
        self.wire_from = None
        self._wire_from_at = None
        self._wire_drag = False
        self._slide_pin = None
        self._slide_moved = False
        pulling = self._pull_wire is not None
        self._pull_wire = None
        self._pull_pts = None
        self._pull_seg = -1
        self._pull_kind = ""
        self._pull_moved = False
        self._clear_wire_preview()
        if pulling:
            self._redraw_wires()
        self.status.emit(self._status_text())

    def _sheet_for_item(self, item: ComponentItem):
        """Sheet this symbol was placed on. The active tab is not that identity."""

        sid = str(getattr(item, "sheet_id", "") or getattr(self, "_shown_sheet_id", "") or "")
        if not sid:
            return self.session.sheet()
        try:
            return self.session.project.sheet(sid)
        except KeyError:
            return None

    def sync_positions(self) -> set[str]:
        """Copy a dragged symbol onto the instance it was built for.

        The same tag on another page is a different placement. A scene item
        writes only that placement, never the active sheet's copy of the tag.

        The scene wins only while the pointer is dragging that symbol. An edit
        that arrived in the model — a ``panelcad send`` move, undo, or another
        page — refreshes the item. Writing the stale scene back is what put an
        on-screen device back where it started.
        The drawing grid applies only to symbols the pointer has dragged.
        Rounding every item pulled an imported, off-grid layout onto the grid
        the first time someone clicked a device.
        """

        snap_ids = set(self._dragged_ids) if self._moved_since_press else set()
        moved: set[str] = set()
        active_id = self.session.active_sheet_id
        for item in self.scene.items():
            if not isinstance(item, ComponentItem):
                continue
            sheet = self._sheet_for_item(item)
            if sheet is None:
                continue
            try:
                comp = sheet.component(item.component_id)
            except KeyError:
                continue
            scene_x = float(item.pos().x())
            scene_y = float(item.pos().y())
            built = getattr(item, "_model_xy", None)
            if not built:
                built = (scene_x, scene_y)
            built_x, built_y = float(built[0]), float(built[1])
            model_x, model_y = float(comp.x), float(comp.y)
            scene_moved = abs(scene_x - built_x) > 1e-6 or abs(scene_y - built_y) > 1e-6
            model_moved = abs(model_x - built_x) > 1e-6 or abs(model_y - built_y) > 1e-6
            # Only a drag owns the scene. Anything else, including a command
            # that moved the model while this picture was still on screen,
            # keeps the model and the picture catches up.
            if scene_moved and item.component_id in snap_ids:
                nx, ny = scene_x, scene_y
                step = item._snap
                nx = round(nx / step) * step
                ny = round(ny / step) * step
                if abs(model_x - nx) > 1e-6 or abs(model_y - ny) > 1e-6:
                    comp.x = nx
                    comp.y = ny
                    if sheet.id == active_id:
                        moved.add(item.component_id)
                item._model_xy = (float(comp.x), float(comp.y))
                if abs(scene_x - comp.x) > 1e-6 or abs(scene_y - comp.y) > 1e-6:
                    self._applying_model_pos = True
                    try:
                        item.setPos(comp.x, comp.y)
                    finally:
                        self._applying_model_pos = False
            elif scene_moved or model_moved:
                self._applying_model_pos = True
                try:
                    item.setPos(model_x, model_y)
                finally:
                    self._applying_model_pos = False
                item._model_xy = (model_x, model_y)
        return moved

    def _sync_scene_from_model(self, ids: set[str]) -> None:
        if not ids:
            return
        sheet = self.session.sheet()
        by_id = {c.id: c for c in sheet.components}
        self._applying_model_pos = True
        try:
            for item in self.scene.items():
                if not isinstance(item, ComponentItem):
                    continue
                if str(getattr(item, "sheet_id", "") or "") not in {"", sheet.id}:
                    continue
                if item.component_id not in ids or item.component_id not in by_id:
                    continue
                comp = by_id[item.component_id]
                item.setPos(comp.x, comp.y)
                item._model_xy = (float(comp.x), float(comp.y))
        finally:
            self._applying_model_pos = False

    def _commit_symbol_motion(self, *, lift: bool) -> set[str]:
        """Store a drag. A click that never moved returns nothing and writes nothing."""

        dragged = set(self._dragged_ids)
        if not self._moved_since_press or not dragged:
            return set()
        moved = self.sync_positions()
        if lift and self._gesture_edit:
            lifted = set(self.session.lift_symbols_off_title_block(dragged))
            if lifted:
                self._sync_scene_from_model(lifted)
                moved.update(lifted)
        if is_layout_sheet(self.session.sheet()):
            self.session.snap_layout_positions(refs=dragged)
            self._sync_scene_from_model(dragged)
        elif self._moved_since_press:
            # The move handler already wrote the snapped position, so
            # sync_positions often returns nothing. Stored waypoints still
            # have to drop or the wires keep the old trace.
            affected = set(moved) | dragged
            if affected:
                self.session.drop_wire_routes(affected)
            self._redraw_wires()
            return affected
        return moved

    def _wires_follow_component(self, item: ComponentItem) -> None:
        sheet = self._sheet_for_item(item)
        if sheet is None or is_layout_sheet(sheet):
            return
        if sheet.id != self.session.sheet().id:
            return
        try:
            comp = sheet.component(item.component_id)
        except KeyError:
            return
        step = item._snap
        nx = round(item.pos().x() / step) * step
        ny = round(item.pos().y() / step) * step
        if abs(comp.x - nx) < 1e-6 and abs(comp.y - ny) < 1e-6:
            return
        comp.x, comp.y = nx, ny
        # Full routing waits for mouse release. A straight segment keeps the
        # wire on the pin until then. Release drops stored waypoints and reroutes.
        self._preview_drag_wires(item.component_id)

    def _preview_drag_wires(self, comp_id: str) -> None:
        """Straight stand-in while a device moves. Release replaces it with the route.

        The ends are the same points the router will use: a pin, a rung on a
        ladder rail, a clicked landing, or the horizontal IEC rail. The middle
        of a vertical rail is not a landing.
        """

        from panelcad.render.wiring import wire_ends

        sheet = self.session.sheet()
        catalog = self.session.catalog
        project = self.session.project
        by_id = {wire.id: wire for wire in sheet.wires}
        for item in self._items:
            if not isinstance(item, WireItem) or not item.wire_id:
                continue
            wire = by_id.get(item.wire_id)
            if wire is None:
                continue
            ends = (str(wire.frm).split(".", 1)[0], str(wire.to).split(".", 1)[0])
            if comp_id not in ends:
                continue
            # Release drops stored waypoints and keeps a clicked rail landing.
            # The stand-in uses that same pair of ends, so an old riser does
            # not hold the rail off the pin's column.
            saved_points = list(wire.points)
            try:
                if saved_points:
                    wire.points = []
                ax, ay, _a_side, bx, by, _b_side = wire_ends(sheet, catalog, wire, project)
            except (KeyError, ValueError, IndexError):
                continue
            finally:
                if saved_points:
                    wire.points = saved_points
            path = QPainterPath()
            path.moveTo(ax, ay)
            path.lineTo(bx, by)
            item.setPath(path)

    def place_at(self, library_id: str, x: float, y: float) -> None:
        step = PANEL_SNAP if is_layout_sheet(self.session.sheet()) else SNAP
        self.session.begin_edit("place")
        try:
            self.sync_positions()
            try:
                comp = self.session.place(library_id, x=round(x / step) * step, y=round(y / step) * step)
            except ValueError as exc:
                self.status.emit(str(exc))
                return
            lifted = comp.id in self.session.lift_symbols_off_title_block([comp.id])
        finally:
            self.session.end_edit()
        if lifted:
            comp = self.session.sheet().component(comp.id)
        device = self.session.catalog.get(comp.library_id)
        item = ComponentItem(
            comp,
            device,
            layout=is_layout_sheet(self.session.sheet()),
            network=is_network_sheet(self.session.sheet()),
            io_card=is_io_sheet(self.session.sheet()) and is_io_module(device) and not comp.properties.get("ladder_card"),
            sheet_id=self.session.sheet().id,
        )
        if self.session.sheet().kind == "schematic" and not item.layout and not item.network:
            laid = self._device_tag_layouts().get(comp.id)
            if laid is not None:
                item.set_tag_layout(laid)
        self._add(item)
        self.scene.clearSelection()
        item.setSelected(True)
        self._apply_pin_mode()
        note = " above the title block" if lifted else ""
        self.status.emit(f"placed {comp.id}{note}  —  drag to move, Delete to remove")
        self.project_changed.emit()

    def place_from_bom_at(self, library_id: str, ref: str | None, x: float, y: float) -> None:
        step = PANEL_SNAP if is_layout_sheet(self.session.sheet()) else SNAP
        self.session.begin_edit("place")
        try:
            self.sync_positions()
            try:
                result = self.session.place_from_bom(
                    library_id,
                    ref=ref,
                    x=round(x / step) * step,
                    y=round(y / step) * step,
                )
            except ValueError as exc:
                self.status.emit(str(exc))
                return
            if result.get("placed"):
                self.session.lift_symbols_off_title_block([str(result["id"])])
        finally:
            self.session.end_edit()
        if not result.get("placed"):
            self.session.forget_noop_checkpoint()
        if not result.get("placed") and result.get("reason") == "not_layout_ready":
            self.status.emit(str(result.get("message") or result.get("reason")))
            return
        self.reload()
        self.select_ref(result["id"])
        if result.get("placed"):
            self.status.emit(
                f"BOM {result['id']} on {result['kind']}  ({result['library_id']})"
            )
        else:
            self.status.emit(f"{result['id']} already on this {result['kind']} sheet")
        self.project_changed.emit()

    def _to_point(self, view_pos) -> QPoint:
        if isinstance(view_pos, QPoint):
            return view_pos
        if hasattr(view_pos, "toPoint"):
            return view_pos.toPoint()
        return QPoint(int(view_pos.x()), int(view_pos.y()))

    def _device_at(self, view_pos) -> ComponentItem | None:
        for item in self.items(self._to_point(view_pos)):
            if isinstance(item, ComponentItem) or getattr(item, "strip_block", False):
                return item
            parent = item.parentItem()
            if isinstance(parent, ComponentItem) or getattr(parent, "strip_block", False):
                return parent
        return None

    def _pin_at(self, view_pos) -> PinItem | None:
        for item in self.items(self._to_point(view_pos)):
            if isinstance(item, PinItem):
                return item
        return None

    def _wire_at(self, view_pos) -> WireItem | None:
        # Walk the conductors themselves. A sheet frame, grid line, or rung
        # symbol can sit on the same pixel; the closest wire within the
        # screen-pixel hit still wins.
        scene_pos = self.mapToScene(self._to_point(view_pos))
        return self.wire_at_scene(scene_pos)

    def _refresh_wire_hit_geometry(self) -> None:
        scale = _pixels_per_scene(self)
        if abs(scale - self._wire_hit_scale) < 1e-9:
            return
        self._wire_hit_scale = scale
        for item in self._items:
            if isinstance(item, WireItem):
                item.prepareGeometryChange()
                item.update()

    def wire_at_scene(self, scene_pos: QPointF) -> WireItem | None:
        """Closest wire whose screen hit contains ``scene_pos``, or None."""

        self._refresh_wire_hit_geometry()
        limit = wire_hit_radius_scene(self)
        best: WireItem | None = None
        best_d = limit
        px, py = float(scene_pos.x()), float(scene_pos.y())
        point = QPointF(px, py)
        # scene.items() is topmost-first. Equal distances keep that wire.
        # _items is insertion order, which picked the bottom wire on a shared bus.
        for item in self.scene.items():
            if not isinstance(item, WireItem) or not item.isVisible():
                continue
            local = item.mapFromScene(point)
            # boundingRect already covers the hit pad and the label. A tight
            # path box dropped clicks that still land inside that pad at zoom 1.
            if not item.boundingRect().adjusted(-0.5, -0.5, 0.5, 0.5).contains(local):
                continue
            distance = item.distance_to_scene(px, py)
            if distance > limit + 1e-6:
                continue
            closer = best is None or distance < best_d - 1e-6
            tie = best is not None and abs(distance - best_d) <= 1e-6 and item.zValue() > best.zValue()
            if closer or tie:
                best = item
                best_d = distance
        return best

    def _hover_wire_at(self, view_pos) -> WireItem | None:
        if self._pin_at(view_pos) is not None:
            return None
        scene_pos = self.mapToScene(self._to_point(view_pos))
        if self._in_title_block(scene_pos):
            return None
        wiring = self.mode == "wire" or self.wire_from is not None
        if wiring and not is_layout_sheet(self.session.sheet()) and self._rail_at(scene_pos) is not None:
            return None
        return self.wire_at_scene(scene_pos)

    def _clear_wire_hover(self) -> None:
        item = self._hover_wire
        self._hover_wire = None
        if item is None:
            return
        try:
            if item.scene() is not None:
                item.set_hovered(False)
        except RuntimeError:
            return

    def _set_wire_hover(self, item: WireItem | None) -> None:
        if item is self._hover_wire:
            return
        self._clear_wire_hover()
        self._hover_wire = item
        if item is not None:
            item.set_hovered(True)

    def _wire_pts(self, wire_id: str) -> list[Point]:
        wire = self.session.find_wire(wire_id=wire_id)
        return list(
            iter_wire_points(
                self.session.sheet(), self.session.catalog, wire, project=self.session.project
            )
        )

    def _set_wire_item_path(self, item: WireItem, pts: list[Point]) -> None:
        path = QPainterPath()
        path.moveTo(pts[0].x, pts[0].y)
        for p in pts[1:]:
            path.lineTo(p.x, p.y)
        item.setPath(path)

    def _begin_wire_pull(self, item: WireItem, scene_pos: QPointF) -> bool:
        if not item.wire_id or is_layout_sheet(self.session.sheet()):
            return False
        try:
            pts = self._wire_pts(item.wire_id)
        except (KeyError, ValueError, IndexError):
            return False
        hit = nearest_run(pts, scene_pos.x(), scene_pos.y())
        if hit is None:
            return False
        self._begin_gesture("pull wire")
        self._pull_wire = item
        self._pull_pts = pts
        self._pull_seg, self._pull_kind = hit
        self._pull_moved = False
        item.setSelected(True)
        if self._pull_kind == "h":
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        return True

    def _update_wire_pull(self, scene_pos: QPointF) -> None:
        if self._pull_wire is None or not self._pull_pts:
            return
        channel = snap(scene_pos.y() if self._pull_kind == "h" else scene_pos.x())
        pts = set_run_channel(self._pull_pts, self._pull_seg, self._pull_kind, channel)
        self._pull_moved = True
        self._set_wire_item_path(self._pull_wire, pts)
        self._pull_wire._live_pts = pts  # type: ignore[attr-defined]

    def _commit_wire_pull(self) -> None:
        item = self._pull_wire
        pts = getattr(item, "_live_pts", None) if item is not None else None
        self._pull_wire = None
        self._pull_pts = None
        self._pull_seg = -1
        self._pull_kind = ""
        moved = self._pull_moved
        self._pull_moved = False
        self.unsetCursor()
        if item is None or not item.wire_id:
            return
        if not moved or not pts or len(pts) < 2:
            self.status.emit(self._status_text())
            return
        interior = pts[1:-1]
        self.session.set_wire_route(item.wire_id, interior)
        self._redraw_wires()
        self.project_changed.emit()
        self.status.emit(f"pulled wire {item.wire_id}  —  right-click to reset auto-route")

    def _in_title_block(self, scene_pos: QPointF) -> bool:
        x, y, w, h = title_block_rect(self.session.sheet())
        return QRectF(x, y, w, h).contains(scene_pos)

    def _begin_empty_select_drag(self, event) -> None:
        """Hold-drag on empty paper: leave place/wire and rubber-band select."""
        self.set_select_mode()
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        origin = self._left_press_pos if self._left_press_pos is not None else event.position()
        press = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            origin,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            event.modifiers(),
        )
        super().mousePressEvent(press)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._moved_since_press = False
        self._dragged_ids.clear()
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = False
            self._pan_start = event.position()
            self._middle_press_pos = event.position()
            self._middle_hit_device = None
            self._middle_hit_wire = None
            scene_pos = self.mapToScene(event.position().toPoint())
            if not self._in_title_block(scene_pos):
                pin = None if is_layout_sheet(self.session.sheet()) else self._pin_at(event.position())
                wire = None if pin is not None else self._wire_at(event.position())
                device = None if pin is not None or wire is not None else self._device_at(event.position())
                if pin is not None:
                    parent = pin.parentItem()
                    device = parent if isinstance(parent, ComponentItem) else self._device_at(event.position())
                    wire = None
                self._middle_hit_device = device
                self._middle_hit_wire = wire
            event.accept()
            return

        if event.button() == Qt.MouseButton.RightButton:
            # Do not start a drag — contextMenuEvent builds the command menu.
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        self._left_press_pos = event.position()
        self._left_empty_press = False
        self._left_select_drag = False

        hit = self.itemAt(event.position().toPoint())
        if isinstance(hit, LogoSlotItem):
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            super().mousePressEvent(event)
            return

        scene_pos = self.mapToScene(event.position().toPoint())
        if self._in_title_block(scene_pos):
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            event.accept()
            return

        if self.mode == "cloud":
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self._cloud_origin = scene_pos
            self._update_cloud_preview(scene_pos, scene_pos)
            event.accept()
            return

        pin = None if is_layout_sheet(self.session.sheet()) else self._pin_at(event.position())
        # Rails are wire ends only while the Wire tool is on, or a wire is
        # already started. Select and Place keep the click they had before.
        wiring = self.mode == "wire" or self.wire_from is not None
        rail = self._rail_at(scene_pos) if wiring and pin is None else None
        # A wire on top of a device's padded bounding box is still a wire click.
        wire = None if pin is not None or rail is not None else self._wire_at(event.position())
        device = None if pin is not None or rail is not None or wire is not None else self._device_at(event.position())

        if pin is not None:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            locked = True
            try:
                locked = self.session.sheet().component(pin.component_id).pins_locked
            except KeyError:
                locked = True
            if (not locked) and self.wire_from is None:
                self._begin_gesture("slide pin")
                self._slide_pin = pin
                self._slide_moved = False
                event.accept()
                return
            self._wire_drag = True
            self._handle_pin(pin.ref())
            self._update_wire_preview(scene_pos, None)
            event.accept()
            return

        if rail is not None:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self._wire_drag = True
            self._handle_wire_end(rail.name, QPointF(rail.x, rail.y))
            self._update_wire_preview(scene_pos, None)
            event.accept()
            return

        # Existing device: always select/drag. Never place-on-top, never clear the scene.
        if device is not None:
            self._begin_gesture("move")
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            super().mousePressEvent(event)
            return

        if wire is not None:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.scene.clearSelection()
            wire.setSelected(True)
            if self._begin_wire_pull(wire, scene_pos):
                cap = f"  [{wire._label}]" if wire._label else ""
                self.status.emit(f"wire {wire.wire_id}{cap}  —  drag to pull, right-click to label or reset")
            else:
                cap = f"  [{wire._label}]" if wire._label else ""
                self.status.emit(f"wire {wire.wire_id}{cap}  —  right-click to label or delete")
            event.accept()
            return

        # Empty workspace: short click keeps the current tool; hold-drag enters select.
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self._left_empty_press = True
        event.accept()
        return

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not event.buttons():
            self._set_wire_hover(self._hover_wire_at(event.position()))
        if event.buttons() & Qt.MouseButton.MiddleButton:
            if self._middle_press_pos is not None and not self._panning:
                if (event.position() - self._middle_press_pos).manhattanLength() >= 8:
                    self._panning = True
                    self._pan_start = event.position()
                    self.setCursor(Qt.CursorShape.ClosedHandCursor)
            if self._panning:
                delta = event.position() - self._pan_start
                self._pan_start = event.position()
                self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(delta.x()))
                self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(delta.y()))
            event.accept()
            return
        if self._cloud_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            end = self.mapToScene(event.position().toPoint())
            self._update_cloud_preview(self._cloud_origin, end)
            event.accept()
            return
        if self._slide_pin is not None:
            self._slide_pin_to(event)
            event.accept()
            return
        if self._pull_wire is not None:
            self._update_wire_pull(self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        if (
            event.buttons() & Qt.MouseButton.LeftButton
            and self._left_press_pos is not None
            and not self._wire_drag
        ):
            if not self._left_select_drag:
                if (event.position() - self._left_press_pos).manhattanLength() >= 8:
                    self._left_select_drag = True
                    if self._left_empty_press:
                        self._begin_empty_select_drag(event)
                        event.accept()
                        return
                    self.set_select_mode()
            elif self._left_empty_press:
                super().mouseMoveEvent(event)
                event.accept()
                return
        if self.wire_from:
            scene_pos = self.mapToScene(event.position().toPoint())
            pin = self._pin_at(event.position())
            if pin is not None and self._ends_match(pin.ref(), self.wire_from):
                pin = None
            rail = None if pin is not None else self._rail_at(scene_pos)
            snap_at = None
            if rail is not None and not self._ends_match(rail.name, self.wire_from):
                snap_at = QPointF(rail.x, rail.y)
            self._update_wire_preview(scene_pos, pin, snap_at)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._gesture_edit
            and not self.session.command_open()
        ):
            # Undo or Redo already cancelled this drag. Do not push it back.
            self._drop_gesture_flags()
            super().mouseReleaseEvent(event)
            self._left_press_pos = None
            self._left_select_drag = False
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self._resync_items_from_model()
            event.accept()
            return
        if event.button() == Qt.MouseButton.MiddleButton:
            panned = self._panning
            self._panning = False
            self.unsetCursor()
            if not panned:
                self._delete_middle_hit()
            self._middle_press_pos = None
            self._middle_hit_device = None
            self._middle_hit_wire = None
            event.accept()
            return
        if self._cloud_origin is not None and event.button() == Qt.MouseButton.LeftButton:
            origin = self._cloud_origin
            end = self.mapToScene(event.position().toPoint())
            self._cancel_cloud_drag()
            x = min(origin.x(), end.x())
            y = min(origin.y(), end.y())
            w = abs(end.x() - origin.x())
            h = abs(end.y() - origin.y())
            if w >= 3.0 and h >= 3.0:
                try:
                    self.commit_cloud(x, y, w, h)
                except ValueError as exc:
                    self.status.emit(str(exc))
            else:
                self.status.emit("drag a revision cloud at least 3 mm")
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._left_empty_press:
            dragged = self._left_select_drag
            press_pos = self._left_press_pos or event.position()
            self._left_empty_press = False
            self._left_select_drag = False
            self._left_press_pos = None
            if dragged:
                super().mouseReleaseEvent(event)
                self.setDragMode(QGraphicsView.DragMode.NoDrag)
                event.accept()
                return
            if self.mode == "place" and self.place_library_id:
                scene_pos = self.mapToScene(press_pos.toPoint())
                self.place_at(self.place_library_id, scene_pos.x(), scene_pos.y())
                event.accept()
                return
            self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
            press = QMouseEvent(
                QMouseEvent.Type.MouseButtonPress,
                press_pos,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                event.modifiers(),
            )
            super().mousePressEvent(press)
            super().mouseReleaseEvent(event)
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            event.accept()
            return
        if self._pull_wire is not None and event.button() == Qt.MouseButton.LeftButton:
            moved = self._pull_moved
            self._commit_wire_pull()
            self._finish_gesture(moved=moved)
            event.accept()
            return
        if self._slide_pin is not None and event.button() == Qt.MouseButton.LeftButton:
            pin = self._slide_pin
            moved = self._slide_moved
            self._slide_pin = None
            self._slide_moved = False
            if moved:
                self._commit_slide(pin)
                self._finish_gesture(moved=True)
            else:
                self._finish_gesture(moved=False)
                self._wire_drag = True
                self._handle_pin(pin.ref())
                self._update_wire_preview(self.mapToScene(event.position().toPoint()), None)
            event.accept()
            return
        if self._wire_drag and event.button() == Qt.MouseButton.LeftButton:
            scene_pos = self.mapToScene(event.position().toPoint())
            pin = self._pin_at(event.position())
            if pin is not None and self.wire_from and not self._ends_match(pin.ref(), self.wire_from):
                self._handle_pin(pin.ref())
            else:
                rail = None if pin is not None else self._rail_at(scene_pos)
                if rail is not None and self.wire_from and not self._ends_match(rail.name, self.wire_from):
                    self._handle_wire_end(rail.name, QPointF(rail.x, rail.y))
            self._wire_drag = False
            event.accept()
            return
        super().mouseReleaseEvent(event)
        self._left_press_pos = None
        self._left_select_drag = False
        user_moved = bool(self._moved_since_press and self._dragged_ids)
        moved = self._commit_symbol_motion(lift=True)
        self._dragged_ids.clear()
        self._finish_gesture(moved=bool(moved or user_moved))
        self._moved_since_press = False
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        # A cached scene can still be mid-drag when the button comes up.
        # The commit wrote the model; the picture has to follow it.
        self._resync_items_from_model()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._clear_wire_hover()
        super().leaveEvent(event)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if _library_id_from_mime(event.mimeData()) or _bom_from_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if _library_id_from_mime(event.mimeData()) or _bom_from_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        scene_pos = self.mapToScene(event.position().toPoint())
        bom = _bom_from_mime(event.mimeData())
        if bom and bom.get("library_id"):
            self.place_from_bom_at(
                str(bom["library_id"]),
                str(bom.get("ref") or "") or None,
                scene_pos.x(),
                scene_pos.y(),
            )
            event.acceptProposedAction()
            return
        lib_id = _library_id_from_mime(event.mimeData())
        if not lib_id:
            event.ignore()
            return
        self.set_place_mode(lib_id)
        self.place_at(lib_id, scene_pos.x(), scene_pos.y())
        event.acceptProposedAction()

    def _slide_pin_to(self, event) -> None:
        pin = self._slide_pin
        if pin is None:
            return
        parent = pin.parentItem()
        if not isinstance(parent, ComponentItem):
            return
        scene_pos = self.mapToScene(event.position().toPoint())
        local = parent.mapFromScene(scene_pos)
        scale = max(parent._scale, 0.01)
        x = round((local.x() / scale) * 2.0) / 2.0
        y = round((local.y() / scale) * 2.0) / 2.0
        x = max(-4.0, min(parent._base_w + 4.0, x))
        y = max(-4.0, min(parent._base_h + 4.0, y))
        new_pos = QPointF(x * scale, y * scale)
        if (new_pos - pin.pos()).manhattanLength() > 0.6:
            self._slide_moved = True
        pin._lx = x
        pin._ly = y
        pin.setPos(new_pos)
        try:
            comp = self.session.sheet().component(pin.component_id)
        except KeyError:
            return
        side = infer_pin_side(x, y, parent._base_w, parent._base_h)
        comp.pin_overrides[pin.pin_id] = PinOverride(x=x, y=y, side=side)  # type: ignore[arg-type]
        self._redraw_wires()

    def _commit_slide(self, pin: PinItem) -> None:
        try:
            ov = self.session.set_pin(pin.component_id, pin.pin_id, pin._lx, pin._ly)
        except (KeyError, ValueError) as exc:
            self.status.emit(str(exc))
            return
        self.session.drop_wire_routes({pin.component_id})
        self._redraw_wires()
        self.project_changed.emit()
        self.status.emit(f"{pin.ref()}  {ov.x:g},{ov.y:g}  {ov.side}  — lock the device when finished")

    def set_selected_pins_locked(self, locked: bool) -> None:
        refs = [i.component_id for i in self.scene.selectedItems() if isinstance(i, ComponentItem)]
        if not refs:
            self.status.emit("select a device first, then lock or unlock its connection points")
            return
        last = None
        self.session.begin_edit("lock pins" if locked else "unlock pins")
        self.sync_positions()
        try:
            for ref in refs:
                last = self.session.set_pins_locked(ref, locked)
        finally:
            self.session.end_edit()
        self._apply_pin_mode()
        word = "locked" if locked else "unlocked — drag a terminal onto the connection point"
        self.status.emit(f"{last.id if last else ''} connection points {word}")
        self.project_changed.emit()

    def reset_selected_pins(self) -> None:
        refs = [i.component_id for i in self.scene.selectedItems() if isinstance(i, ComponentItem)]
        if not refs:
            self.status.emit("select a device first to reset its connection points")
            return
        self.session.begin_edit("reset pins")
        self.sync_positions()
        try:
            for ref in refs:
                self.session.reset_pins(ref)
        finally:
            self.session.end_edit()
        self.reload()
        if refs:
            self.select_ref(refs[-1])
        self.status.emit("connection points restored to library positions")

    def _rail_at(self, scene_pos: QPointF):
        if is_layout_sheet(self.session.sheet()) or self.session.sheet().kind == "terminal":
            return None
        from panelcad.core.ladder import hit_rail

        return hit_rail(
            self.session.sheet(), scene_pos.x(), scene_pos.y(), self.session.project
        )

    def _ends_match(self, left: str, right: str) -> bool:
        return str(left).strip().upper() == str(right).strip().upper()

    def _handle_pin(self, ref: str) -> None:
        self._handle_wire_end(ref, None)

    def _handle_wire_end(self, ref: str, at: QPointF | None) -> None:
        if self.wire_from is None:
            self.wire_from = ref
            self._wire_from_at = QPointF(at) if at is not None else None
            self.status.emit(f"wire from {ref} — click the other pin or rail (Esc cancels)")
            return
        if self._ends_match(self.wire_from, ref):
            self.wire_from = None
            self._wire_from_at = None
            self._wire_drag = False
            self._clear_wire_preview()
            self.status.emit("wire cancelled")
            return
        from panelcad.core.ladder import rail_short_message

        short = rail_short_message(
            self.session.sheet(), self.wire_from, ref, self.session.project
        )
        if short:
            self.status.emit(short)
            return
        kwargs: dict = {}
        if self._wire_from_at is not None:
            kwargs["from_at"] = (self._wire_from_at.x(), self._wire_from_at.y())
        if at is not None:
            kwargs["to_at"] = (at.x(), at.y())
        self.session.begin_edit("wire")
        self.sync_positions()
        try:
            self.session.wire(self.wire_from, ref, **kwargs)
        finally:
            self.session.end_edit()
        self.wire_from = None
        self._wire_from_at = None
        self._wire_drag = False
        self._clear_wire_preview()
        self._redraw_wires()
        self.project_changed.emit()
        self.status.emit(self._status_text())

    def wheelEvent(self, event) -> None:  # noqa: N802
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.zoom_by(factor)

    def zoom_by(self, factor: float) -> None:
        self._user_zoomed = True
        self.scale(factor, factor)
        self._wire_hit_scale = -1.0
        self._refresh_wire_hit_geometry()
        self.view_changed.emit()

    def zoom_percent(self) -> int:
        """100% is 1 mm on the sheet equals 1 mm on the screen."""

        dpi = float(self.logicalDpiX() or 96)
        true = float(self.transform().m11()) * (25.4 / dpi)
        return max(1, int(round(true * 100)))

    def paintEvent(self, event) -> None:  # noqa: N802
        from panelcad.theme import canvas_hex, push_screen_ink, reset_screen_ink

        self._problems_marks = _problems_tab_current(self)
        token = push_screen_ink(canvas_hex("ink"))
        try:
            super().paintEvent(event)
        finally:
            reset_screen_ink(token)

    def set_place_mode(self, library_id: str) -> None:
        self.mode = "place"
        self.place_library_id = library_id
        self.wire_from = None
        self._wire_from_at = None
        self._apply_pin_mode()
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.status.emit(f"place {library_id} — drag from library or click empty sheet (not on a part)")

    def _draw_terminal_strip(self, sheet) -> None:
        from panelcad.core.terminal_strip import layout_terminal_strip
        from panelcad.gui.strip_canvas import StripBlockItem, StripChromeItem, StripJumperItem

        name = str(sheet.strip or "").strip()
        if not name:
            self._strip_caption = "no strip assigned — Insert → Terminal strip…"
            return
        layout = layout_terminal_strip(self.session.project, self.session.catalog, name)
        part = int(getattr(sheet, "strip_part", 1) or 1)
        layout = {
            **layout,
            "part": part,
            "items": [item for item in layout["items"] if int(item.get("part") or 1) == part],
            "jumpers": [jumper for jumper in layout["jumpers"] if int(jumper.get("part") or 1) == part],
        }
        self._add(StripChromeItem(layout))
        for block in layout["items"]:
            self._add(StripBlockItem(block))
        for jumper in layout["jumpers"]:
            self._add(StripJumperItem(jumper))
        n = sum(1 for block in layout["items"] if block["kind"] == "terminal")
        continued = f"  ·  sheet {part}" if int(layout.get("parts") or 1) > 1 else ""
        self._strip_caption = f"terminal strip {name}  ·  {n} terminals  ·  catalog scale{continued}"

    def select_ref(self, ref: str) -> None:
        self.scene.clearSelection()
        for item in self.scene.items():
            if getattr(item, "component_id", None) != ref:
                continue
            if isinstance(item, ComponentItem) or getattr(item, "strip_block", False):
                item.setSelected(True)
                self.centerOn(item)
                return
        for item in self.scene.items():
            if isinstance(item, WireItem) and item.wire_id == ref:
                item.setSelected(True)
                self.centerOn(item)
                return

    def set_findings(self, rows: list[dict[str, str]]) -> None:
        self._findings = [row for row in rows if row.get("suppressed") != "yes"]
        self._draw_finding_markers()

    def _draw_finding_markers(self) -> None:
        for item in list(self._items):
            if isinstance(item, FindingMarker):
                self.scene.removeItem(item)
                self._items.remove(item)
        sheet_id = self.session.sheet().id
        visible = [row for row in self._findings if str(row.get("sheet") or "") in {"", sheet_id}]
        components = {item.component_id: item for item in self._items if isinstance(item, ComponentItem)}
        wires = {item.wire_id: item for item in self._items if isinstance(item, WireItem) and item.wire_id}
        hosts: dict[str, str] = {}
        for wire in self.session.sheet().wires:
            ends = [_host_component(str(end), set(components)) for end in (wire.frm, wire.to)]
            live = [end for end in ends if end]
            if len(live) == 1:
                hosts[wire.id] = live[0]
        obstacles = [device_drawn_scene_bounds(item) for item in components.values()]
        for kind, ident, rows in group_finding_rows(visible, set(components), set(wires), hosts=hosts):
            worst = rows[0]
            marker = FindingMarker(worst, count=len(rows), rows=rows, anchor_ref=ident)
            br = marker.boundingRect()
            tip: QPointF | None = None
            if kind == "comp" and ident in components:
                box = device_drawn_scene_bounds(components[ident])
                pos = marker_pos_off_top_right(box, br)
                tip = QPointF(box.right(), box.top())
            elif kind == "wire" and ident in wires:
                box = wire_drawn_scene_bounds(wires[ident])
                pos = marker_pos_off_top_right(box, br)
                tip = QPointF(box.right(), box.top())
            else:
                try:
                    raw = QPointF(float(worst.get("x") or 0), float(worst.get("y") or 0))
                except (TypeError, ValueError):
                    continue
                box = QRectF(raw.x() - 1.0, raw.y() - 1.0, 2.0, 2.0)
                pos = marker_pos_off_top_right(box, br)
                tip = raw
            pos = place_clear_of_devices(pos, br, obstacles)
            marker.setPos(pos)
            self._add(marker)
            if tip is not None:
                marker.set_leader(tip)

    def highlight_trace(
        self,
        devices: list[str],
        wires: list[str],
        *,
        focus: str = "",
        focus_wire: str = "",
        sheet_id: str = "",
    ) -> None:
        """Mark the troubleshooting path. A reload keeps the same marks."""

        self._trace_highlight = {
            "devices": [str(item) for item in devices if item],
            "wires": [str(item) for item in wires if item],
            "focus": str(focus or ""),
            "focus_wire": str(focus_wire or ""),
            "sheet": str(sheet_id or ""),
        }
        self._draw_trace_highlight()
        if focus:
            self.select_ref(focus)

    def clear_trace_highlight(self) -> None:
        self._trace_highlight = None
        self._remove_trace_marks()

    def _remove_trace_marks(self) -> None:
        for item in list(self._items):
            if isinstance(item, (TraceHalo, TraceWireMark)):
                if item.scene() is not None:
                    self.scene.removeItem(item)
                self._items.remove(item)

    def _draw_trace_highlight(self) -> None:
        self._remove_trace_marks()
        spec = self._trace_highlight
        if not spec:
            return
        sheet = self.session.sheet()
        wanted_sheet = str(spec.get("sheet") or "")
        if wanted_sheet and wanted_sheet != sheet.id:
            return
        devices = {name.casefold() for name in spec.get("devices") or []}
        focus = str(spec.get("focus") or "").casefold()
        numbers = {str(item) for item in spec.get("wires") or [] if item}
        focus_wire = str(spec.get("focus_wire") or "")
        by_id = {wire.id: str(wire.number or "").strip() for wire in sheet.wires}
        for item in list(self._items):
            if isinstance(item, ComponentItem) and item.component_id.casefold() in devices:
                rect = device_drawn_scene_bounds(item)
                if rect.isValid() and rect.width() > 0 and rect.height() > 0:
                    self._add(TraceHalo(rect, focus=item.component_id.casefold() == focus))
            elif isinstance(item, WireItem) and item.wire_id:
                number = by_id.get(item.wire_id, "")
                if number and number in numbers:
                    self._add(TraceWireMark(item.path(), focus=number == focus_wire))

    def jump_to_finding(self, row: dict[str, str]) -> None:
        ref = str(row.get("ref") or "")
        if ref:
            self.select_ref(ref)
            if not self.scene.selectedItems() and str(row.get("target") or ""):
                self.select_ref(str(row["target"]))
        try:
            x = float(row.get("x") or "")
            y = float(row.get("y") or "")
        except (TypeError, ValueError):
            return
        self.centerOn(x, y)

    def fit_page(self, *, user: bool = True) -> None:
        """Show the whole sheet, including the bottom title block."""
        self._user_zoomed = user
        self.resetTransform()
        rect = self.scene.sceneRect()
        if rect.isEmpty():
            return
        self.fitInView(rect.adjusted(-4, -4, 4, 4), Qt.AspectRatioMode.KeepAspectRatio)
        self._wire_hit_scale = -1.0
        self._refresh_wire_hit_geometry()
        self.view_changed.emit()

    def fit_drawing(self) -> None:
        """Fit the devices and wires, not the empty page around them."""

        rect = QRectF()
        for item in self.scene.items():
            if isinstance(item, (ComponentItem, WireItem, EnclosureItem, TrayItem, RailItem)):
                rect = rect.united(item.sceneBoundingRect())
        if rect.isEmpty() or rect.width() < 1 or rect.height() < 1:
            self.fit_page(user=False)
            return
        self._user_zoomed = False
        self.resetTransform()
        self.fitInView(rect.adjusted(-8, -8, 8, 8), Qt.AspectRatioMode.KeepAspectRatio)
        self._wire_hit_scale = -1.0
        self._refresh_wire_hit_geometry()
        self.view_changed.emit()

    def _fit_if_automatic(self) -> None:
        if self._user_zoomed:
            return
        self.fit_drawing()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if getattr(self, "_user_zoomed", True):
            return
        timer = getattr(self, "_fit_timer", None)
        if timer is not None:
            timer.start(150)

    def selected_refs(self) -> list[str]:
        refs: list[str] = []
        for item in self.scene.selectedItems():
            if isinstance(item, ComponentItem):
                refs.append(item.component_id)
            elif isinstance(item, WireItem) and item.wire_id:
                refs.append(item.wire_id)
        return refs

    def restore_selection(self, refs: list[str]) -> None:
        wanted = {ref for ref in refs if ref}
        if not wanted:
            return
        self.scene.blockSignals(True)
        try:
            for item in self.scene.items():
                if isinstance(item, ComponentItem) and item.component_id in wanted:
                    item.setSelected(True)
                elif isinstance(item, WireItem) and item.wire_id in wanted:
                    item.setSelected(True)
        finally:
            self.scene.blockSignals(False)
        self.selection_changed.emit()

    def set_select_mode(self) -> None:
        self.mode = "select"
        self.place_library_id = None
        self.cancel_wire()
        self._cancel_cloud_drag()
        self._apply_pin_mode()
        self.unsetCursor()
        self.status.emit(self._status_text())

    def set_cloud_mode(self) -> None:
        self.mode = "cloud"
        self.place_library_id = None
        self.cancel_wire()
        self._apply_pin_mode()
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.status.emit("drag a rectangle to draw a revision cloud")

    def commit_cloud(self, x: float, y: float, w: float, h: float, rev: str = "") -> dict:
        """Store a manual revision cloud on the active sheet and redraw."""

        info = self.session.add_revision_cloud(float(x), float(y), float(w), float(h), rev=rev)
        self.reload()
        self.status.emit(f"revision cloud {info.get('id', '')}")
        return info

    def _draw_revision_clouds(self) -> None:
        store = self.session.project.revision_control
        if store is None:
            return
        sheet = self.session.sheet()
        for cloud in store.clouds:
            if cloud.sheet == sheet.id and cloud.w > 0 and cloud.h > 0:
                self._add(RevisionCloudItem(cloud, sheet, self.session.catalog))

    def _cancel_cloud_drag(self) -> None:
        self._cloud_origin = None
        item = self._cloud_preview
        self._cloud_preview = None
        if item is None:
            return
        if item.scene() is not None:
            self.scene.removeItem(item)
        try:
            self._items.remove(item)
        except ValueError:
            pass

    def _update_cloud_preview(self, origin: QPointF, end: QPointF) -> None:
        from panelcad.render.clouds import cloud_polyline

        x = min(origin.x(), end.x())
        y = min(origin.y(), end.y())
        w = max(abs(end.x() - origin.x()), 0.4)
        h = max(abs(end.y() - origin.y()), 0.4)
        pts = cloud_polyline(x, y, w, h)
        path = QPainterPath()
        path.moveTo(pts[0][0], pts[0][1])
        for px, py in pts[1:]:
            path.lineTo(px, py)
        from panelcad.render.clouds import CLOUD
        from panelcad.theme import adapt_print_color

        pen = QPen(QColor(adapt_print_color(CLOUD)), 0.6, Qt.PenStyle.DashLine)
        if self._cloud_preview is None:
            item = QGraphicsPathItem()
            item.setPen(pen)
            item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            item.setZValue(66)
            item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
            self._add(item)
            self._cloud_preview = item
        self._cloud_preview.setPen(pen)
        self._cloud_preview.setPath(path)

    def set_wire_mode(self) -> None:
        if is_layout_sheet(self.session.sheet()):
            self.set_select_mode()
            self.status.emit("wires are schematic-only — use a schematic or comms tab")
            return
        self.mode = "wire"
        self.place_library_id = None
        self._apply_pin_mode()
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.status.emit("hover a terminal or a rail, then click or drag to the next one")

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self._left_empty_press = False
            self._left_select_drag = False
            self._left_press_pos = None
            self.cancel_wire()
            self.set_select_mode()
            event.accept()
            return
        step = {
            Qt.Key.Key_Left: (-1, 0),
            Qt.Key.Key_Right: (1, 0),
            Qt.Key.Key_Up: (0, -1),
            Qt.Key.Key_Down: (0, 1),
        }.get(event.key())
        if step is not None and self._nudge_selected(step[0], step[1]):
            event.accept()
            return
        super().keyPressEvent(event)

    def _nudge_selected(self, sx: int, sy: int) -> bool:
        """Move selected devices one snap step. One undo for the whole set."""
        items = [i for i in self.scene.selectedItems() if isinstance(i, ComponentItem)]
        if not items:
            return False
        self.session.begin_edit("move")
        self.sync_positions()
        moved: set[str] = set()
        try:
            for item in items:
                nx = item.pos().x() + sx * item._snap
                ny = item.pos().y() + sy * item._snap
                self.session.move(item.component_id, nx, ny)
                comp = self.session.sheet().component(item.component_id)
                item.setPos(comp.x, comp.y)
                moved.add(item.component_id)
            if moved:
                self.session.drop_wire_routes(moved)
        finally:
            self.session.end_edit()
        if moved:
            self._redraw_wires()
            self.project_changed.emit()
            refs = ", ".join(sorted(moved))
            self.status.emit(f"moved {refs}")
        return True

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        pin = None if is_layout_sheet(self.session.sheet()) else self._pin_at(event.position())
        wire = None if pin is not None else self._wire_at(event.position())
        device = None if pin is not None or wire is not None else self._device_at(event.position())
        if wire is not None and wire.wire_id:
            self.wire_label_requested.emit(wire.wire_id)
            event.accept()
            return
        if device is not None:
            self.object_edit_requested.emit(device.component_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        self.fill_context_menu(menu, event.pos())
        if menu.actions():
            menu.exec(event.globalPos())
        event.accept()

    def fill_context_menu(self, menu: QMenu, view_pos) -> None:
        """Populate *menu* for a right-click at view coordinates. Used by tests."""
        layout = is_layout_sheet(self.session.sheet())
        pin = None if layout else self._pin_at(view_pos)
        wire = None if pin is not None else self._wire_at(view_pos)
        device = None if pin is not None or wire is not None else self._device_at(view_pos)
        if device is None and pin is not None:
            device = self._device_at(view_pos)

        if device is not None:
            self.scene.clearSelection()
            device.setSelected(True)
            ref = device.component_id
            locked = True
            try:
                locked = self.session.sheet().component(ref).pins_locked
            except KeyError:
                locked = True
            if pin is not None:
                menu.addAction("Start wire here", lambda r=pin.ref(): self._start_wire_from(r))
                menu.addSeparator()
            menu.addAction("Object editor…", lambda r=ref: self.object_edit_requested.emit(r))
            menu.addAction("Rename…", lambda r=ref: self.rename_requested.emit(r))
            menu.addAction("Set address…", lambda r=ref: self.address_edit_requested.emit(r))
            menu.addAction("Duplicate", self.duplicate_selected)
            if not device.layout:
                menu.addAction("Rotate 90°", self.rotate_selected)
            menu.addSeparator()
            if locked:
                menu.addAction("Unlock connection points", lambda: self.set_selected_pins_locked(False))
            else:
                menu.addAction("Lock connection points", lambda: self.set_selected_pins_locked(True))
                menu.addAction("Reset connection points", self.reset_selected_pins)
            menu.addSeparator()
            menu.addAction("Bigger  ]", lambda: self.nudge_scale(1.1))
            menu.addAction("Smaller  [", lambda: self.nudge_scale(1 / 1.1))
            menu.addAction("Delete", self._delete_selected)
            return

        if wire is not None:
            wid = wire.wire_id
            menu.addAction("Set label…", lambda w=wid: self.wire_label_requested.emit(w))
            menu.addAction("Set wire number…", lambda w=wid: self.wire_number_requested.emit(w))
            menu.addAction("Set size, color, type…", lambda w=wid: self.wire_spec_requested.emit(w))
            found = None
            try:
                found = self.session.find_wire(wire_id=wid) if wid else None
            except (KeyError, ValueError):
                found = None
            if found is not None:
                if found.number_locked:
                    menu.addAction(
                        "Unlock wire number",
                        lambda w=wid: self._set_wire_locked(w, False),
                    )
                elif found.number:
                    menu.addAction(
                        "Lock wire number",
                        lambda w=wid: self._set_wire_locked(w, True),
                    )
            menu.addAction("Reset auto-route", lambda w=wid: self._reset_wire_route(w))
            menu.addAction(
                f"Delete wire {wid}" if wid else "Delete wire",
                lambda w=wid: self._delete_wire(w),
            )
            menu.addSeparator()

        if not layout:
            menu.addAction("Wire mode", self.set_wire_mode)
        menu.addAction("Revision cloud", self.set_cloud_mode)
        menu.addAction("Select", self.set_select_mode)
        menu.addAction("Fit page", self.fit_page)
        if layout:
            layout_action = menu.addAction("Build layout from schematic", self.pack_requested.emit)
            layout_action.setToolTip("Place catalog-sized devices onto the panel layout.")
        menu.addSeparator()
        menu.addAction("New page…", self.new_sheet_requested.emit)
        menu.addAction("Rename page…", self.rename_sheet_requested.emit)
        menu.addAction("Close this page", self.close_sheet_requested.emit)
        menu.addAction("Delete this page…", self.delete_sheet_requested.emit)

    def _start_wire_from(self, ref: str) -> None:
        if is_layout_sheet(self.session.sheet()):
            return
        self.set_wire_mode()
        self.wire_from = ref
        self._wire_from_at = None
        self.status.emit(f"wire from {ref} — click the other pin or rail (Esc cancels)")

    def _set_wire_locked(self, wire_id: str, locked: bool) -> None:
        if not wire_id:
            return
        try:
            self.session.set_wire_spec(wire_id, locked=locked)
        except KeyError:
            return
        self._redraw_wires()
        self.project_changed.emit()
        self.status.emit(f"{'lock' if locked else 'unlock'} {wire_id}")

    def _reset_wire_route(self, wire_id: str) -> None:
        if not wire_id:
            return
        try:
            self.session.clear_wire_route(wire_id)
        except KeyError:
            return
        self._redraw_wires()
        self.project_changed.emit()
        self.status.emit(f"auto-route {wire_id}")

    def _delete_wire(self, wire_id: str) -> None:
        if not wire_id:
            return
        try:
            self.session.delete_wire(wire_id)
        except KeyError:
            return
        self._redraw_wires()
        self.project_changed.emit()
        self.status.emit(f"deleted wire {wire_id}")

    def _delete_middle_hit(self) -> None:
        """Middle-click (no drag) removes the device or wire under the cursor."""

        wire = self._middle_hit_wire
        device = self._middle_hit_device
        if wire is not None and wire.wire_id:
            self._delete_wire(wire.wire_id)
            return
        if device is None:
            return
        ref = device.component_id
        if self.wire_from and self.wire_from.startswith(ref + "."):
            self.cancel_wire()
        self.session.begin_edit("delete")
        removed = False
        try:
            self.sync_positions()
            try:
                if getattr(device, "strip_block", False):
                    self.session.delete_strip_component(ref)
                else:
                    self.session.delete_component(ref)
            except KeyError:
                return
            removed = True
        finally:
            self.session.end_edit()
        if not removed:
            return
        self.reload()
        self.status.emit(f"deleted {ref}")
        self.project_changed.emit()

    def _delete_selected(self) -> None:
        selected = list(self.scene.selectedItems())
        strip_refs = [i.component_id for i in selected if getattr(i, "strip_block", False) and i.component_id]
        refs = [i.component_id for i in selected if isinstance(i, ComponentItem)]
        wires = [i.wire_id for i in selected if isinstance(i, WireItem) and i.wire_id]
        if not refs and not wires and not strip_refs:
            return
        self.session.begin_edit("delete")
        try:
            self.sync_positions()
            seen: set[str] = set()
            for ref in strip_refs:
                if ref in seen:
                    continue
                seen.add(ref)
                try:
                    from panelcad.core.terminal_strip import remove_strip_component

                    remove_strip_component(self.session, ref)
                except KeyError:
                    continue
            for ref in refs:
                if ref in seen:
                    continue
                self.session.delete_component(ref)
            for wid in wires:
                try:
                    self.session.delete_wire(wid)
                except KeyError:
                    continue
        finally:
            self.session.end_edit()
        if refs or strip_refs:
            self.reload()
            count = len(set(refs) | set(strip_refs))
            noun = "object" if count == 1 else "objects"
            self.status.emit(f"deleted {count} {noun}")
        elif wires:
            self._redraw_wires()
            self.project_changed.emit()
            self.status.emit(f"deleted {len(wires)} wire(s)")

    def duplicate_selected(self) -> None:
        refs = [i.component_id for i in self.scene.selectedItems() if isinstance(i, ComponentItem)]
        created = None
        self.session.begin_edit("duplicate")
        self.sync_positions()
        try:
            for ref in refs:
                created = self.session.duplicate_component(ref)
        finally:
            self.session.end_edit()
        if created is not None:
            self.reload()
            self.select_ref(created.id)
            self.status.emit(f"duplicated → {created.id}")

    def rotate_selected(self) -> None:
        refs = [
            i.component_id
            for i in self.scene.selectedItems()
            if isinstance(i, ComponentItem) and not i.layout
        ]
        last = None
        self.session.begin_edit("rotate")
        self.sync_positions()
        try:
            for ref in refs:
                last = self.session.rotate_component(ref, 90)
            if last is not None:
                self.session.drop_wire_routes(set(refs))
        finally:
            self.session.end_edit()
        if last is not None:
            self.reload()
            self.select_ref(last.id)
            self.status.emit(f"{last.id} rotation {last.rotation}°")

    def nudge_scale(self, factor: float) -> None:
        items = [item for item in self.scene.selectedItems() if isinstance(item, ComponentItem)]
        if not items:
            return
        self.session.begin_edit("scale")
        try:
            for item in items:
                item.apply_scale(item._scale * factor)
                item.commit_scale()
        finally:
            self.session.end_edit()


def _ignore_mouse(item: QGraphicsItem) -> None:
    item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
    item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
    item.setZValue(-10)
    if isinstance(item, (QGraphicsLineItem, QGraphicsRectItem)):
        item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
