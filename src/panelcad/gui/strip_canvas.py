"""Selectable terminal-strip blocks for the schematic canvas.

The picture itself is ``draw_physical_scene`` — the same renderer the print,
PDF, and SVG paths use. These items only hit-test and show a selection.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem, QStyleOptionGraphicsItem, QWidget

def _c(role: str):
    from panelcad.gui.theme import canvas_qcolor

    return canvas_qcolor(role)


class StripSceneItem(QGraphicsItem):
    """The physical strip picture. Not a component."""

    def __init__(self, scene: dict) -> None:
        super().__init__()
        self.scene = scene
        self.setZValue(1)
        bounds = scene.get("bounds") or (0.0, 0.0, 40.0, 40.0)
        self._rect = QRectF(float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3]))

    def boundingRect(self) -> QRectF:  # noqa: N802
        return self._rect

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        del option, widget
        from panelcad.render.qt_painter import QtPainter
        from panelcad.render.terminal_strip import draw_physical_scene

        draw_physical_scene(QtPainter(painter), self.scene)


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
        return QRectF(-0.4, -0.4, self.w + 0.8, self.h + 0.8)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        del option, widget
        if not self.isSelected():
            return
        painter.setPen(QPen(_c("strip_selected"), 0.8))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(0.0, 0.0, self.w, self.h))


class StripJumperItem(QGraphicsItem):
    """Hover target for a jumper comb. The picture is drawn with the sheet."""

    def __init__(self, jumper: dict) -> None:
        super().__init__()
        self.jumper = jumper
        self.setZValue(20)
        self.setAcceptHoverEvents(True)
        x1 = float(jumper.get("x1") or 0.0)
        x2 = float(jumper.get("x2") or 0.0)
        y = float(jumper.get("y") or 0.0)
        lx = float(jumper["label_x"]) if jumper.get("label_x") is not None else (x1 + x2) / 2.0
        ly = float(jumper["label_y"]) if jumper.get("label_y") is not None else y - 4.0
        size = float(jumper.get("label_size") or 2.0)
        top = min(y - 2.0, ly - size - 1.0)
        bottom = y + float(jumper.get("tick") or 4.0) + 2.0
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
        del painter, option, widget
