"""Print-style picker for schematic sheets (not panel layout or network)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from panelcad.core.models import PRINT_STYLES

THUMB_W, THUMB_H = 220, 132


def _ink() -> QColor:
    from panelcad.gui.theme import canvas_qcolor

    return canvas_qcolor("ink")


def style_pixmap(style_id: str, width: int = THUMB_W, height: int = THUMB_H) -> QPixmap:
    """Tiny example of a logical print in that style."""

    from panelcad.gui.theme import canvas_qcolor

    pix = QPixmap(width, height)
    pix.fill(canvas_qcolor("paper"))
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(_ink(), 1.6))
    p.setBrush(canvas_qcolor("style_fill"))
    if style_id == "ladder":
        _paint_ladder(p, width, height)
    elif style_id == "freeform":
        _paint_freeform(p, width, height)
    else:
        _paint_schematic(p, width, height)
    p.end()
    return pix


def _paint_schematic(p: QPainter, w: int, h: int) -> None:
    y = h // 2
    boxes = [(22, y - 14, 36, 28), (92, y - 18, 40, 36), (166, y - 14, 36, 28)]
    for x, yy, bw, bh in boxes:
        p.drawRect(x, yy, bw, bh)
    p.drawLine(58, y, 92, y)
    p.drawLine(132, y, 166, y)
    p.setFont(QFont("Arial", 7))
    p.drawText(26, y + 4, "Q1")
    p.drawText(102, y + 4, "K1")
    p.drawText(172, y + 4, "M1")


def _paint_ladder(p: QPainter, w: int, h: int) -> None:
    left, right = 22, w - 22
    y = 62
    p.setPen(QPen(_ink(), 2.4))
    p.drawLine(left, 18, left, h - 16)
    p.drawLine(right, 18, right, h - 16)
    p.setFont(QFont("Arial", 7, QFont.Weight.Bold))
    p.drawText(left - 8, 14, "L1")
    p.drawText(right - 10, 14, "L2")
    p.setPen(QPen(_ink(), 1.5))
    p.drawLine(left, y, right, y)
    p.drawLine(70, y, 70, y + 22)
    p.drawLine(70, y + 22, 118, y + 22)
    p.drawLine(118, y + 22, 118, y)
    p.drawLine(52, y - 8, 68, y)
    p.drawLine(88, y, 104, y - 8)
    p.drawEllipse(128, y - 12, 24, 24)
    p.drawLine(164, y - 6, 178, y)
    p.setFont(QFont("Arial", 6))
    p.drawText(48, y + 14, "Stop")
    p.drawText(88, y + 14, "Start")
    p.drawText(132, y + 4, "M")
    p.drawText(164, y + 14, "OL")
    p.drawText(86, y + 34, "M")


def _paint_freeform(p: QPainter, w: int, h: int) -> None:
    p.drawEllipse(24, 22, 34, 34)
    p.drawRect(96, 18, 40, 24)
    p.drawRect(150, 70, 36, 28)
    p.drawRect(40, 78, 32, 26)
    p.drawLine(58, 56, 96, 30)
    p.drawLine(136, 42, 150, 84)
    p.drawLine(58, 91, 150, 84)
    p.setFont(QFont("Arial", 6))
    p.drawText(32, 42, "M")
    p.drawText(106, 34, "CR")
    p.drawText(44, 94, "PB")
    p.drawText(158, 88, "OL")


class _StyleCard(QFrame):
    def __init__(self, style_id: str, title: str, blurb: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.style_id = style_id
        self.setFrameShape(QFrame.Shape.Box)
        self.setLineWidth(1)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        thumb = QLabel()
        thumb.setPixmap(style_pixmap(style_id))
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(thumb)
        name = QLabel(title)
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = name.font()
        font.setBold(True)
        name.setFont(font)
        layout.addWidget(name)
        hint = QLabel(blurb)
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        from panelcad.theme import ui_hex

        hint.setObjectName("muted")
        hint.setStyleSheet(f"color: {ui_hex('muted')};")
        layout.addWidget(hint)
        self._set_selected(False)

    def _set_selected(self, on: bool) -> None:
        from panelcad.theme import ui_hex

        if on:
            self.setStyleSheet(
                f"QFrame {{ border: 2px solid {ui_hex('accent')}; background: {ui_hex('selection')}; }}"
            )
        else:
            self.setStyleSheet(
                f"QFrame {{ border: 1px solid {ui_hex('border')}; background: {ui_hex('panel')}; }}"
            )


class PrintStyleDialog(QDialog):
    """Human picks schematic / ladder / freeform. Network and panel pages ignore this."""

    def __init__(self, parent: QWidget | None = None, *, current: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Print style")
        self.choice = ""
        layout = QVBoxLayout(self)
        title = QLabel("How should schematic sheets look?")
        font = title.font()
        font.setPointSize(11)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        note = QLabel(
            "This applies to logical prints only. Panel layout and network pages are unchanged."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        self._cards: list[_StyleCard] = []
        for style_id, name, blurb in PRINT_STYLES:
            card = _StyleCard(style_id, name, blurb)
            card.mousePressEvent = lambda ev, c=card: self._pick(c)  # type: ignore[method-assign]
            card.mouseDoubleClickEvent = lambda ev, c=card: self._accept(c)  # type: ignore[method-assign]
            self._cards.append(card)
            row.addWidget(card)
        layout.addLayout(row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setEnabled(False)
        layout.addWidget(buttons)
        if current:
            for card in self._cards:
                if card.style_id == current:
                    self._pick(card)
                    break

    def _pick(self, card: _StyleCard) -> None:
        self.choice = card.style_id
        for other in self._cards:
            other._set_selected(other is card)
        self._ok.setEnabled(True)

    def _accept(self, card: _StyleCard) -> None:
        self._pick(card)
        self.accept()


def choose_print_style(parent: QWidget | None = None, *, current: str = "") -> str:
    """Modal picker. Empty string if the operator cancels."""

    dlg = PrintStyleDialog(parent, current=current)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return ""
    return dlg.choice
