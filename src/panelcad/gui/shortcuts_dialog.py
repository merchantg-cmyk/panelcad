"""Searchable shortcut list, grouped by menu."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from panelcad.theme import MARGIN_PX, SPACING_PX


class ShortcutsDialog(QDialog):
    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self.setWindowTitle("Keyboard shortcuts")
        self.setObjectName("shortcutsDialog")
        self.resize(640, 480)
        self.setMinimumSize(480, 320)
        self._rows = _shortcut_rows(window)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(MARGIN_PX, MARGIN_PX, MARGIN_PX, MARGIN_PX)
        layout.setSpacing(SPACING_PX)
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Find"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Shortcut or command")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._fill)
        search_row.addWidget(self.search, 1)
        layout.addLayout(search_row)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Menu", "Command", "Shortcut"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self._fill()

    def _fill(self) -> None:
        needle = self.search.text().strip().lower()
        rows = [
            row
            for row in self._rows
            if not needle or needle in row[0].lower() or needle in row[1].lower() or needle in row[2].lower()
        ]
        self.table.setRowCount(len(rows))
        for index, (group, label, shortcut) in enumerate(rows):
            self.table.setItem(index, 0, QTableWidgetItem(group))
            self.table.setItem(index, 1, QTableWidgetItem(label))
            self.table.setItem(index, 2, QTableWidgetItem(shortcut))
        self.table.resizeColumnsToContents()


def _shortcut_rows(window: QWidget) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    bar = window.menuBar() if hasattr(window, "menuBar") else None
    menus = bar.actions() if bar is not None else []
    for action in menus:
        menu = action.menu()
        if menu is None:
            continue
        _walk(menu, _clean(action.text()) or "Menu", rows, seen)
    rows.extend(
        [
            ("Mouse", "Zoom", "Wheel"),
            ("Mouse", "Select (short click keeps Place or Wire)", "Drag empty"),
            ("Mouse", "Delete the object under the cursor", "Middle-click"),
            ("Mouse", "Nudge the selection", "Arrows"),
        ]
    )
    return rows


def _walk(menu, group: str, rows: list[tuple[str, str, str]], seen: set[tuple[str, str]]) -> None:
    for action in menu.actions():
        child = action.menu()
        if child is not None:
            _walk(child, _clean(action.text()) or group, rows, seen)
            continue
        label = _clean(action.text())
        shortcut = action.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
        if not label or not shortcut:
            continue
        key = (label, shortcut)
        if key in seen:
            continue
        seen.add(key)
        rows.append((group, label, shortcut))


def _clean(text: str) -> str:
    return str(text or "").replace("&", "").strip()
