"""Selectable terminal-strip blocks for the schematic canvas."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem, QStyleOptionGraphicsItem, QWidget

from panelcad.core.terminal_strip import CALLOUT
from panelcad.render.qt_painter import draw_world_text

def _c(role: str) -> QColor:
    from panelcad.gui.theme import canvas_qcolor

    return canvas_qcolor(role)


_FILL_ROLE = {
    "terminal": "strip_terminal",
    "end_plate": "strip_end_plate",
    "end_stop": "strip_end_stop",
    "partition": "strip_partition",
}
_WORD = {"end_plate": "END", "end_stop": "STOP", "partition": "PART"}


class StripChromeItem(QGraphicsItem):
    """Title, side labels, and one TS35 rail per row. Not a component."""

    def __init__(self, layout: dict) -> None:
        super().__init__()
        self.layout = layout
        self.setZValue(1)
        items = layout.get("items") or []
        if items:
            x0 = min(item["x"] for item in items) - 28.0
            x1 = max(item["x"] + item["w"] for item in items) + 8.0
            y0 = min(item["y"] for item in items) - CALLOUT - 16.0
            y1 = max(item["y"] + item["h"] for item in items) + CALLOUT + 22.0
            self._rect = QRectF(x0, y0, max(40.0, x1 - x0), max(40.0, y1 - y0))
        else:
            self._rect = QRectF(20.0, 18.0, 180.0, 40.0)

    def boundingRect(self) -> QRectF:  # noqa: N802
        return self._rect

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        del option, widget
        items = self.layout.get("items") or []
        name = str(self.layout.get("strip") or "")
        part = int(self.layout.get("part") or 1)
        title = f"Terminal strip {name}" if part <= 1 else f"Terminal strip {name} (continued {part})"
        draw_world_text(painter, 36.0, 28.0, title, 4.2, align="start")
        if not items:
            draw_world_text(painter, 36.0, 42.0, "No terminals on this sheet.", 2.6, align="start")
            return
        rows: dict[int, list] = {}
        for item in items:
            rows.setdefault(int(item.get("row") or 0), []).append(item)
        first = min(items, key=lambda item: (float(item["y"]), float(item["x"])))
        draw_world_text(painter, float(first["x"]) - 1.0, float(first["y"]) - 3.0, "Internal", 2.2, align="end")
        draw_world_text(
            painter,
            float(first["x"]) - 1.0,
            float(first["y"]) + float(first["h"]) + CALLOUT - 2.0,
            "Field",
            2.2,
            align="end",
        )
        for row_items in rows.values():
            x0 = min(float(item["x"]) for item in row_items) - 1.0
            x1 = max(float(item["x"]) + float(item["w"]) for item in row_items) + 1.0
            bottom = max(float(item["y"]) + float(item["h"]) for item in row_items)
            rail_y = bottom + CALLOUT
            painter.setPen(QPen(_c("strip_rail_stroke"), 0.45))
            painter.setBrush(_c("strip_rail"))
            painter.drawRect(QRectF(x0, rail_y, x1 - x0, 7.0))
            painter.drawRect(QRectF(x0, rail_y - 1.6, x1 - x0, 1.6))
            painter.drawRect(QRectF(x0, rail_y + 7.0, x1 - x0, 1.6))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            draw_world_text(painter, (x0 + x1) / 2.0, rail_y + 12.0, "TS35", 2.4, align="middle")


class StripBlockItem(QGraphicsItem):
    """One block on the strip. Selecting it selects the real component."""

    def __init__(self, block: dict) -> None:
        super().__init__()
        self.block = block
        self.component_id = str(block.get("ref") or "")
        self.strip_kind = str(block.get("kind") or "")
        self.strip_block = True
        self.w = float(block["w"])
        self.h = float(block["h"])
        self.setPos(float(block["x"]), float(block["y"]))
        self.setZValue(10)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setAcceptHoverEvents(True)
        bits = [self.component_id, self.strip_kind.replace("_", " ")]
        if block.get("internal"):
            bits.append(f"internal {block['internal']}")
        if block.get("external"):
            bits.append(f"field {block['external']}")
        if block.get("jumper"):
            bits.append(f"jumper {block['jumper']}")
        self.setToolTip("  ·  ".join(bit for bit in bits if bit))

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(-3.0, -CALLOUT, self.w + 6.0, self.h + CALLOUT * 2.0)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        del option, widget
        block = self.block
        role = "strip_pe" if block.get("pe") else _FILL_ROLE.get(self.strip_kind, "strip_terminal")
        fill = _c(role)
        pen = QPen(_c("strip_selected") if self.isSelected() else _c("strip_ink"), 0.7 if self.isSelected() else 0.4)
        painter.setPen(pen)
        painter.setBrush(fill)
        painter.drawRect(QRectF(0.0, 0.0, self.w, self.h))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.strip_kind == "end_stop":
            painter.setPen(QPen(_c("strip_stop"), 0.7))
            painter.drawLine(self.w * 0.35, 4.0, self.w * 0.35, self.h - 4.0)
            painter.drawLine(self.w * 0.65, 4.0, self.w * 0.65, self.h - 4.0)
        elif block.get("pe"):
            painter.setPen(QPen(_c("strip_pe_mark"), 0.45))
            mid = self.w / 2.0
            painter.drawLine(mid - min(3.2, self.w * 0.35), self.h - 14.0, mid + min(3.2, self.w * 0.35), self.h - 14.0)
            painter.drawLine(mid - min(2.2, self.w * 0.25), self.h - 11.5, mid + min(2.2, self.w * 0.25), self.h - 11.5)
            painter.drawLine(mid - min(1.2, self.w * 0.15), self.h - 9.0, mid + min(1.2, self.w * 0.15), self.h - 9.0)
        if self.strip_kind == "terminal":
            radius = min(1.05, self.w * 0.16)
            painter.setPen(QPen(_c("strip_screw"), 0.3))
            painter.setBrush(_c("strip_screw"))
            cy = min(8.0, self.h * 0.18)
            painter.drawEllipse(QRectF(self.w / 2.0 - radius, cy - radius, radius * 2, radius * 2))
            painter.drawEllipse(QRectF(self.w / 2.0 - radius, self.h - cy - radius, radius * 2, radius * 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
        size = max(1.2, min(2.2, self.w * 0.62))
        if self.strip_kind == "terminal":
            draw_world_text(painter, self.w / 2.0, 0.0, str(block.get("internal") or ""), 2.1, align="start", rotate=-90)
            draw_world_text(painter, self.w / 2.0, self.h, str(block.get("external") or ""), 2.1, align="start", rotate=90)
            draw_world_text(painter, self.w / 2.0, self.h / 2.0, self.component_id, size, align="middle", rotate=-90)
        else:
            draw_world_text(painter, self.w / 2.0, 0.0, self.component_id, size, align="start", rotate=-90)
            draw_world_text(
                painter,
                self.w / 2.0,
                self.h / 2.0,
                _WORD.get(self.strip_kind, ""),
                size,
                align="middle",
                rotate=-90,
            )


class StripJumperItem(QGraphicsItem):
    def __init__(self, jumper: dict) -> None:
        super().__init__()
        self.jumper = jumper
        self.setZValue(20)
        x1 = float(jumper["x1"])
        x2 = float(jumper["x2"])
        y = float(jumper["y"])
        lx = float(jumper["label_x"]) if jumper.get("label_x") is not None else (x1 + x2) / 2.0
        ly = float(jumper["label_y"]) if jumper.get("label_y") is not None else y - 4.0
        size = float(jumper.get("label_size") or 2.0)
        top = min(y - 2.0, ly - size - 1.0)
        bottom = y + 6.0
        left = min(x1, x2, lx) - 4.0
        right = max(x1, x2, lx) + 4.0
        self._rect = QRectF(left, top, max(8.0, right - left), max(10.0, bottom - top))
        bits = [str(jumper.get("id") or "jumper")]
        if jumper.get("part_number"):
            bits.append(str(jumper["part_number"]))
        if jumper.get("catalog"):
            bits.append(str(jumper["catalog"]))
        if jumper.get("refs"):
            bits.append("–".join(str(ref) for ref in jumper["refs"]))
        self.setToolTip("  ·  ".join(bit for bit in bits if bit))

    def boundingRect(self) -> QRectF:  # noqa: N802
        return self._rect

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        del option, widget
        y = float(self.jumper["y"])
        x1 = float(self.jumper["x1"])
        x2 = float(self.jumper["x2"])
        painter.setPen(QPen(_c("strip_jumper"), 0.9))
        painter.drawLine(x1, y, x2, y)
        for tick in self.jumper.get("ticks") or []:
            painter.drawLine(float(tick), y, float(tick), y + 4.0)
        label = str(self.jumper.get("label") or self.jumper.get("id") or "")
        lx = float(self.jumper["label_x"]) if self.jumper.get("label_x") is not None else (x1 + x2) / 2.0
        ly = float(self.jumper["label_y"]) if self.jumper.get("label_y") is not None else y - 4.0
        size = float(self.jumper.get("label_size") or 2.0)
        draw_world_text(painter, lx, ly, label, size, align="middle")
