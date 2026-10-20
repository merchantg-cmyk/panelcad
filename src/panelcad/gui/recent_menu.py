"""File > Open Recent. The list lives in settings, not in a project."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QLabel, QMenu, QWidget, QWidgetAction

from panelcad.io.winpath import display_name, project_presence
from panelcad.theme import RECENT_LIMIT, recent_projects, ui_hex


def fill_recent_menu(
    menu: QMenu,
    *,
    on_open: Callable[[str], None],
    on_clear: Callable[[], None],
    on_hover: Callable[[str], None] | None = None,
) -> None:
    """Rebuild ``menu``. Newest project first. A missing file stays clickable."""

    menu.clear()
    try:
        paths = list(recent_projects())[:RECENT_LIMIT]
    except OSError:
        paths = []
    if not paths:
        empty = menu.addAction("No recent projects")
        empty.setEnabled(False)
    for path in paths:
        action = _RecentAction(path, menu)
        if on_hover is not None:
            action.hovered.connect(lambda _path=path: on_hover(_path))
        action.triggered.connect(lambda _checked=False, _path=path: on_open(_path))
        menu.addAction(action)
    menu.addSeparator()
    clear = QAction("Clear Recent", menu)
    clear.setEnabled(bool(paths))
    clear.triggered.connect(on_clear)
    menu.addAction(clear)


class _RecentAction(QWidgetAction):
    """File name in the menu. The full path is the tooltip and the status tip.

    A missing file is drawn in the muted color and still accepts a click or
    Enter, so the window can explain and offer to remove it.
    """

    def __init__(self, path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._path = path
        try:
            presence = project_presence(path)
        except OSError:
            presence = "unavailable"
        self._missing = presence == "missing"
        name = display_name(path) or path
        self.setText(name.replace("&", "&&"))
        self.setToolTip(path)
        self.setStatusTip(path)
        self.setProperty("recentPath", path)
        self.setProperty("recentMissing", self._missing)

    def createWidget(self, parent: QWidget) -> QWidget:  # noqa: N802
        name = display_name(self._path) or self._path
        label = QLabel(name, parent)
        label.setToolTip(self._path)
        label.setStatusTip(self._path)
        label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        label.setProperty("recentPath", self._path)
        if self._missing:
            label.setObjectName("recentMissing")
            color = ui_hex("muted")
            label.setStyleSheet(f"color: {color}; background: transparent; padding: 2px 8px;")
        else:
            label.setObjectName("recentProject")
            label.setStyleSheet("background: transparent; padding: 2px 8px;")
        return label
