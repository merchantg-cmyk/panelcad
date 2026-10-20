"""Keep a dialog inside the screen work area (QScreen.availableGeometry)."""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QDialog, QScrollArea, QWidget


def available_work_area(widget: QWidget) -> QRect:
    """Work area of the screen the widget is on, excluding the taskbar."""

    screen = widget.screen()
    if screen is None:
        app = QGuiApplication.instance()
        screen = app.primaryScreen() if app is not None else None
    if screen is None:
        return QRect(0, 0, 1280, 800)
    return screen.availableGeometry()


class OptionScroll(QScrollArea):
    """Scrolls a tall form. The dialog minimum stays small enough for the buttons.

    sizeHint follows the form so a tall screen shows every option. fit_dialog
    caps that hint to the work area; the minimum stays short so the buttons
    are never pushed off a short screen.
    """

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(240, 120)

    def sizeHint(self) -> QSize:  # noqa: N802
        widget = self.widget()
        if widget is None:
            return QSize(460, 480)
        hint = widget.sizeHint()
        return QSize(max(320, hint.width()), max(160, hint.height()))


def fit_dialog_to_work_area(dialog: QDialog) -> None:
    """Center the dialog in the work area and keep the whole frame inside it.

    A title bar is often still unreported during showEvent, so a short reserve
    is left when the frame and the client are the same size.
    """

    if getattr(dialog, "_fitting_work_area", False):
        return
    dialog._fitting_work_area = True
    try:
        avail = available_work_area(dialog)
        if avail.width() < 200 or avail.height() < 200:
            return
        frame = dialog.frameGeometry()
        client = dialog.geometry()
        chrome_w = max(0, frame.width() - client.width())
        chrome_h = max(0, frame.height() - client.height())
        if chrome_h < 24:
            chrome_h = 40
        if chrome_w < 8:
            chrome_w = 16
        margin = 8
        max_w = max(120, avail.width() - chrome_w - 2 * margin)
        max_h = max(160, avail.height() - chrome_h - 2 * margin)
        hint = dialog.sizeHint()
        # Prefer the layout's hint (a tall form) and any explicit resize.
        # The cap below is what keeps Insert/Apply on the work area.
        width = max(dialog.width(), hint.width(), dialog.minimumWidth())
        height = max(dialog.height(), hint.height(), dialog.minimumHeight())
        width = min(width, max_w)
        height = min(height, max_h)
        dialog.resize(int(width), int(height))
        frame = dialog.frameGeometry()
        frame_w = max(dialog.width(), frame.width())
        frame_h = dialog.height() + chrome_h
        x = avail.x() + max(margin, (avail.width() - frame_w) // 2)
        y = avail.y() + max(margin, (avail.height() - frame_h) // 2)
        x = max(avail.x(), min(x, avail.x() + avail.width() - frame_w))
        y = max(avail.y(), min(y, avail.y() + avail.height() - frame_h))
        dx = frame.x() - dialog.geometry().x()
        dy = frame.y() - dialog.geometry().y()
        dialog.move(int(x - dx), int(y - dy))
    finally:
        dialog._fitting_work_area = False


def fit_when_shown(dialog: QDialog) -> None:
    """Clamp now, and once more after the platform places the window."""

    fit_dialog_to_work_area(dialog)

    def _again() -> None:
        if dialog.isVisible():
            fit_dialog_to_work_area(dialog)

    QTimer.singleShot(0, _again)
