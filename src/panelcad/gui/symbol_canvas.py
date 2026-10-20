"""Click-draw millimetre canvas for library symbols."""

from __future__ import annotations

import copy

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QKeyEvent
from PySide6.QtWidgets import QInputDialog, QWidget

from panelcad.gui.symbol_draw import DrawModel

TOOLS = ("select", "line", "box", "circle", "poly", "attr", "pin")


def _c(role: str) -> QColor:
    from panelcad.gui.theme import canvas_qcolor

    return canvas_qcolor(role)


class SymbolCanvas(QWidget):
    pins_changed = Signal()
    draw_changed = Signal()
    attributes_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = DrawModel()
        self.pins: list[dict] = []
        self.attributes: list[dict] = []
        self.pending_attr = "TAG1"
        self.show_values = False
        self.value_for = None
        self.tool = "line"
        self.scale_px = 8.0
        self.sym_w = 28.0
        self.sym_h = 16.0
        self._press: tuple[float, float] | None = None
        self._cur: tuple[float, float] | None = None
        self._poly: list[tuple[float, float]] = []
        self.selected: int | None = None
        self.selected_pin: int | None = None
        self.selected_attr: int | None = None
        self._move_from: dict | None = None
        self._move_pin_from: dict | None = None
        self._move_attr_from: dict | None = None
        self.setMinimumSize(280, 180)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._fit()

    def set_symbol_size(self, width: float, height: float) -> None:
        self.sym_w = max(8.0, float(width))
        self.sym_h = max(8.0, float(height))
        self._fit()
        self.update()

    def set_draw(self, items: list) -> None:
        self.model = DrawModel.from_draw(items)
        self.selected = None
        self.update()

    def set_pins(self, pins: list[dict]) -> None:
        self.pins = [dict(p) for p in pins]
        self.update()

    def set_attributes(self, attributes: list[dict]) -> None:
        self.attributes = [dict(a) for a in attributes if str(a.get("tag") or "").strip()]
        self.update()

    def to_draw(self) -> list:
        return self.model.to_draw()

    def set_tool(self, tool: str) -> None:
        self.tool = tool if tool in TOOLS else "select"
        self._press = None
        self._poly = []
        self._move_from = None
        self._move_pin_from = None
        self._move_attr_from = None
        if self.tool == "select":
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)
        self.update()

    def _fit(self) -> None:
        w = max(self.width(), 40)
        h = max(self.height(), 40)
        self.scale_px = max(4.0, min((w - 24) / max(self.sym_w, 1), (h - 24) / max(self.sym_h, 1)))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit()

    def _origin(self) -> tuple[float, float]:
        return (
            (self.width() - self.sym_w * self.scale_px) / 2.0,
            (self.height() - self.sym_h * self.scale_px) / 2.0,
        )

    def to_mm(self, pos: QPointF) -> tuple[float, float]:
        ox, oy = self._origin()
        x = (pos.x() - ox) / self.scale_px
        y = (pos.y() - oy) / self.scale_px
        return (self.model._s(x), self.model._s(y))

    def paintEvent(self, event) -> None:  # noqa: N802
        from panelcad.theme import canvas_hex, push_screen_ink, reset_screen_ink

        token = push_screen_ink(canvas_hex("ink"))
        p = QPainter(self)
        try:
            self._paint(p)
        finally:
            p.end()
            reset_screen_ink(token)

    def _paint(self, p: QPainter) -> None:
        p.fillRect(self.rect(), _c("paper"))
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        ox, oy = self._origin()
        p.translate(ox, oy)
        p.scale(self.scale_px, self.scale_px)
        pen = QPen(_c("grid"))
        pen.setWidthF(0.08)
        p.setPen(pen)
        x = 0.0
        while x <= self.sym_w + 0.01:
            p.drawLine(QPointF(x, 0), QPointF(x, self.sym_h))
            x += 2.0
        y = 0.0
        while y <= self.sym_h + 0.01:
            p.drawLine(QPointF(0, y), QPointF(self.sym_w, y))
            y += 2.0
        box = QPen(_c("symbol_box"))
        box.setWidthF(0.15)
        p.setPen(box)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(0, 0, self.sym_w, self.sym_h))
        ink = QPen(_c("ink"))
        ink.setWidthF(0.35)
        p.setPen(ink)
        for i, item in enumerate(self.model.items):
            if self.selected == i:
                hi = QPen(_c("review"))
                hi.setWidthF(0.6)
                p.setPen(hi)
            else:
                p.setPen(ink)
            self._paint_item(p, item)
        pin_pen = QPen(_c("pin"))
        pin_pen.setWidthF(0.4)
        p.setPen(pin_pen)
        p.setBrush(_c("pin"))
        for i, pin in enumerate(self.pins):
            px, py = float(pin.get("x", 0)), float(pin.get("y", 0))
            r = 1.3 if self.selected_pin == i else 0.9
            if self.selected_pin == i:
                hi = QPen(_c("review"))
                hi.setWidthF(0.5)
                p.setPen(hi)
                p.setBrush(_c("review"))
            else:
                p.setPen(pin_pen)
                p.setBrush(_c("pin"))
            p.drawEllipse(QPointF(px, py), r, r)
            p.drawText(QPointF(px + 1.2, py - 1.0), str(pin.get("id") or ""))
        attr_pen = QPen(_c("attr"))
        attr_pen.setWidthF(0.25)
        for i, attr in enumerate(self.attributes):
            if self.selected_attr == i:
                hi = QPen(_c("review"))
                hi.setWidthF(0.45)
                p.setPen(hi)
            else:
                p.setPen(attr_pen)
            p.drawText(QPointF(float(attr.get("x", 0)), float(attr.get("y", 0))), self._attr_label(attr))
        if self._press and self._cur and self.tool in {"line", "box", "circle"}:
            ghost = QPen(_c("review"))
            ghost.setStyle(Qt.PenStyle.DashLine)
            ghost.setWidthF(0.3)
            p.setPen(ghost)
            p.setBrush(Qt.BrushStyle.NoBrush)
            ax, ay = self._press
            bx, by = self._cur
            if self.tool == "line":
                p.drawLine(QPointF(ax, ay), QPointF(bx, by))
            elif self.tool == "box":
                p.drawRect(QRectF(QPointF(ax, ay), QPointF(bx, by)).normalized())
            else:
                r = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
                p.drawEllipse(QPointF(ax, ay), r, r)
        if self._poly:
            ghost = QPen(_c("review"))
            ghost.setWidthF(0.3)
            p.setPen(ghost)
            for a, b in zip(self._poly, self._poly[1:]):
                p.drawLine(QPointF(*a), QPointF(*b))
            if self._cur:
                p.drawLine(QPointF(*self._poly[-1]), QPointF(*self._cur))

    def _paint_item(self, p: QPainter, item: dict) -> None:
        p.setBrush(Qt.BrushStyle.NoBrush)
        if "line" in item:
            x1, y1, x2, y2 = item["line"]
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        elif "rect" in item:
            x, y, w, h = item["rect"]
            p.drawRect(QRectF(x, y, w, h))
        elif "circle" in item:
            cx, cy, r = item["circle"]
            p.drawEllipse(QPointF(cx, cy), r, r)
        elif "polyline" in item:
            pts = item["polyline"]
            for a, b in zip(pts, pts[1:]):
                p.drawLine(QPointF(a[0], a[1]), QPointF(b[0], b[1]))
        elif "text" in item:
            raw = item["text"]
            p.drawText(QPointF(float(raw[0]), float(raw[1])), str(raw[2]))

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.setFocus()
        mm = self.to_mm(event.position())
        if event.button() == Qt.MouseButton.RightButton:
            if self.tool == "poly" and self._poly:
                self.model.add_polyline(self._poly)
                self._poly = []
                self.draw_changed.emit()
            self.tool = "select"
            self.update()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self.tool == "select":
            attr_i = self._hit_attr(*mm)
            if attr_i is not None:
                self.selected_attr = attr_i
                self.selected = None
                self.selected_pin = None
                self._move_attr_from = dict(self.attributes[attr_i])
                self._move_from = None
                self._move_pin_from = None
                self._press = mm
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.update()
                return
            pin_i = self._hit_pin(*mm)
            if pin_i is not None:
                self.selected_pin = pin_i
                self.selected = None
                self.selected_attr = None
                self._move_pin_from = dict(self.pins[pin_i])
                self._move_from = None
                self._press = mm
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.update()
                return
            hit = self.model.hit_test(*mm)
            self.selected = hit
            self.selected_pin = None
            self.selected_attr = None
            self._move_pin_from = None
            self._move_attr_from = None
            self._move_from = copy.deepcopy(self.model.items[hit]) if hit is not None else None
            self._press = mm if hit is not None else None
            if hit is not None:
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.update()
            return
        if self.tool == "poly":
            self._poly.append(mm)
            self.update()
            return
        if self.tool == "label":
            text, ok = QInputDialog.getText(self, "Label", "Text:")
            if ok and text.strip():
                self.model.add_text(mm[0], mm[1], text.strip())
                self.draw_changed.emit()
            self.update()
            return
        if self.tool == "attr":
            self._place_attribute(*mm)
            return
        if self.tool == "pin":
            side = self._edge_side(*mm)
            x, y = mm
            if side == "left":
                x = 0.0
            elif side == "right":
                x = self.sym_w
            elif side == "top":
                y = 0.0
            else:
                y = self.sym_h
            pid = str(len(self.pins) + 1)
            self.pins.append({"id": pid, "x": x, "y": y, "side": side})
            self.pins_changed.emit()
            self.update()
            return
        self._press = mm
        self._cur = mm
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._cur = self.to_mm(event.position())
        if self.tool == "select" and self._press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            dx = self._cur[0] - self._press[0]
            dy = self._cur[1] - self._press[1]
            if self._move_from is not None and self.selected is not None:
                self.model.replace_translated(self.selected, self._move_from, dx, dy)
            elif self._move_pin_from is not None and self.selected_pin is not None:
                self._drag_pin(dx, dy)
            elif self._move_attr_from is not None and self.selected_attr is not None:
                self._drag_attr(dx, dy)
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._press is None:
            return
        a, b = self._press, self.to_mm(event.position())
        moved = (b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2 >= 0.36
        if self.tool == "select":
            if moved and self._move_from is not None:
                self.draw_changed.emit()
            elif moved and self._move_pin_from is not None:
                self.pins_changed.emit()
            elif moved and self._move_attr_from is not None:
                self.attributes_changed.emit()
            self._press = None
            self._move_from = None
            self._move_pin_from = None
            self._move_attr_from = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self.update()
            return
        self._press = None
        dx, dy = b[0] - a[0], b[1] - a[1]
        if not moved:
            self.update()
            return
        if self.tool == "line":
            self.model.add_line(*a, *b)
        elif self.tool == "box":
            self.model.add_rect(a[0], a[1], dx, dy)
        elif self.tool == "circle":
            self.model.add_circle(a[0], a[1], (dx * dx + dy * dy) ** 0.5)
        self.draw_changed.emit()
        self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.tool == "poly" and self._poly:
            self.model.add_polyline(self._poly)
            self._poly = []
            self.draw_changed.emit()
            self.update()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.selected_attr is not None:
            del self.attributes[self.selected_attr]
            self.selected_attr = None
            self.attributes_changed.emit()
            self.update()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.selected is not None:
            self.model.delete(self.selected)
            self.selected = None
            self.draw_changed.emit()
            self.update()
            return
        nudge = {
            Qt.Key.Key_Left: (-1.0, 0.0),
            Qt.Key.Key_Right: (1.0, 0.0),
            Qt.Key.Key_Up: (0.0, -1.0),
            Qt.Key.Key_Down: (0.0, 1.0),
        }.get(event.key())
        if nudge is not None:
            step = 5.0 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1.0
            dx, dy = nudge[0] * step, nudge[1] * step
            if self.selected is not None:
                self.model.move(self.selected, dx, dy)
                self.draw_changed.emit()
                self.update()
                return
            if self.selected_attr is not None:
                attr = dict(self.attributes[self.selected_attr])
                attr["x"] = self.model._s(float(attr.get("x", 0)) + dx)
                attr["y"] = self.model._s(float(attr.get("y", 0)) + dy)
                self.attributes[self.selected_attr] = attr
                self.attributes_changed.emit()
                self.update()
                return
            if self.selected_pin is not None:
                self._nudge_pin(dx, dy)
                self.pins_changed.emit()
                self.update()
                return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._poly:
            self.model.add_polyline(self._poly)
            self._poly = []
            self.draw_changed.emit()
            self.update()
            return
        if event.key() == Qt.Key.Key_Escape:
            self._poly = []
            self._press = None
            self._move_from = None
            self._move_pin_from = None
            self._move_attr_from = None
            self.update()
            return
        super().keyPressEvent(event)

    def _hit_pin(self, x: float, y: float, slop: float = 2.0) -> int | None:
        best: int | None = None
        best_d = slop
        for i, pin in enumerate(self.pins):
            px, py = float(pin.get("x", 0)), float(pin.get("y", 0))
            d = ((x - px) ** 2 + (y - py) ** 2) ** 0.5
            if d <= best_d:
                best_d = d
                best = i
        return best

    def _drag_pin(self, dx: float, dy: float) -> None:
        if self.selected_pin is None or self._move_pin_from is None:
            return
        src = self._move_pin_from
        side = str(src.get("side") or "left")
        x = float(src.get("x", 0)) + dx
        y = float(src.get("y", 0)) + dy
        if side in {"left", "right"}:
            x = 0.0 if side == "left" else self.sym_w
            y = max(0.0, min(self.sym_h, self.model._s(y)))
        else:
            y = 0.0 if side == "top" else self.sym_h
            x = max(0.0, min(self.sym_w, self.model._s(x)))
        pin = dict(src)
        pin["x"] = x
        pin["y"] = y
        self.pins[self.selected_pin] = pin

    def _nudge_pin(self, dx: float, dy: float) -> None:
        if self.selected_pin is None:
            return
        self._move_pin_from = dict(self.pins[self.selected_pin])
        self._drag_pin(dx, dy)
        self._move_pin_from = None

    def _attr_label(self, attr: dict) -> str:
        tag = str(attr.get("tag") or "TAG").upper()
        if self.show_values and self.value_for is not None:
            shown = str(self.value_for(tag) or "").strip()
            if shown:
                return shown
        return f"[{tag}]"

    def _place_attribute(self, x: float, y: float) -> None:
        tag = str(self.pending_attr or "TAG1").upper()
        for i, attr in enumerate(self.attributes):
            if str(attr.get("tag") or "").upper() == tag:
                moved = dict(attr)
                moved["x"] = x
                moved["y"] = y
                self.attributes[i] = moved
                self.selected_attr = i
                self.attributes_changed.emit()
                self.update()
                return
        self.attributes.append({"tag": tag, "x": x, "y": y, "h": 2.4})
        self.selected_attr = len(self.attributes) - 1
        self.attributes_changed.emit()
        self.update()

    def _hit_attr(self, x: float, y: float) -> int | None:
        best: int | None = None
        best_d = 4.0
        for i, attr in enumerate(self.attributes):
            ax, ay = float(attr.get("x", 0)), float(attr.get("y", 0))
            d = ((x - ax) ** 2 + (y - ay) ** 2) ** 0.5
            if d <= best_d:
                best_d = d
                best = i
        return best

    def _drag_attr(self, dx: float, dy: float) -> None:
        if self.selected_attr is None or self._move_attr_from is None:
            return
        src = self._move_attr_from
        attr = dict(src)
        attr["x"] = self.model._s(float(src.get("x", 0)) + dx)
        attr["y"] = self.model._s(float(src.get("y", 0)) + dy)
        self.attributes[self.selected_attr] = attr

    def _edge_side(self, x: float, y: float) -> str:
        dists = {
            "left": abs(x - 0),
            "right": abs(self.sym_w - x),
            "top": abs(y - 0),
            "bottom": abs(self.sym_h - y),
        }
        return min(dists, key=dists.get)
