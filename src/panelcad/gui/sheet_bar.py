from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QHBoxLayout, QMenu, QPushButton, QTabBar, QWidget

from panelcad.api.session import Session


class SheetBar(QWidget):
    """Tab strip for open sheets. X closes a tab; the page stays in the project."""

    sheet_selected = Signal(str)
    add_requested = Signal()
    close_requested = Signal(str)
    delete_requested = Signal(str)
    rename_requested = Signal(str)
    reopen_requested = Signal(str)
    move_requested = Signal(str, str)
    order_changed = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)
        self.tabs = QTabBar()
        self.tabs.setDocumentMode(True)
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.tabMoved.connect(self._tab_moved)
        self.tabs.customContextMenuRequested.connect(self._tab_menu)
        self._applying_order = False
        layout.addWidget(self.tabs, 1)
        add = QPushButton("+ Sheet")
        add.setFixedHeight(22)
        add.setToolTip("Add a sheet (schematic, panel, communications, door, terminal)")
        add.clicked.connect(self.add_requested.emit)
        layout.addWidget(add)
        self.closed_btn = QPushButton("Closed")
        self.closed_btn.setFixedHeight(22)
        self.closed_btn.setToolTip("Reopen pages closed with the tab X")
        self.closed_btn.clicked.connect(self._closed_menu)
        layout.addWidget(self.closed_btn)
        self._session: Session | None = None
        self._on_switch: Callable[[str], None] | None = None

    def set_session(self, session: Session) -> None:
        self._session = session
        self.refresh()

    def refresh(self) -> None:
        if self._session is None:
            return
        self._applying_order = True
        self.tabs.blockSignals(True)
        while self.tabs.count():
            self.tabs.removeTab(0)
        current = 0
        visible_i = 0
        for i, sheet in enumerate(self._session.project.sheets):
            if sheet.closed:
                continue
            label = f"{i + 1}  {sheet.title or sheet.id}"
            idx = self.tabs.addTab(label)
            self.tabs.setTabData(idx, sheet.id)
            self.tabs.setTabToolTip(
                idx,
                f"{sheet.kind} · {sheet.id}  —  drag to reorder. Right-click to move, rename, or delete.",
            )
            if sheet.id == self._session.active_sheet_id:
                current = visible_i
            visible_i += 1
        self.tabs.setCurrentIndex(current)
        self.tabs.blockSignals(False)
        self._applying_order = False
        n_closed = len(self._session.closed_sheets())
        self.closed_btn.setEnabled(n_closed > 0)
        self.closed_btn.setText(f"Closed ({n_closed})" if n_closed else "Closed")
        if n_closed:
            self.closed_btn.setToolTip("Reopen pages closed with the tab X")
        else:
            self.closed_btn.setToolTip(
                "Reopen is off: no pages are closed. Close a tab with X, then reopen it here."
            )

    def _sheet_id(self, index: int) -> str | None:
        if index < 0:
            return None
        sid = self.tabs.tabData(index)
        return str(sid) if sid else None

    def _tab_moved(self, _from: int, _to: int) -> None:
        if self._applying_order or self._session is None:
            return
        ids = [str(self.tabs.tabData(i)) for i in range(self.tabs.count()) if self.tabs.tabData(i)]
        if ids:
            self.order_changed.emit(ids)

    def _tab_changed(self, index: int) -> None:
        sid = self._sheet_id(index)
        if sid:
            self.sheet_selected.emit(sid)

    def _close_tab(self, index: int) -> None:
        sid = self._sheet_id(index)
        if sid:
            self.close_requested.emit(sid)

    def _fill_reopen_menu(self, menu: QMenu) -> None:
        if self._session is None:
            return
        closed = self._session.closed_sheets()
        if not closed:
            from panelcad.gui.guidance import explain_disabled

            empty = QAction("(none closed)", self)
            explain_disabled(
                empty,
                "Reopen is off: no pages are closed. Close a tab with X to park a page, then reopen it here.",
            )
            menu.addAction(empty)
            return
        for sheet in closed:
            i = self._session.project.sheets.index(sheet) + 1
            act = QAction(f"{i}  {sheet.title or sheet.id}  ({sheet.kind})", self)
            act.triggered.connect(lambda _=False, sid=sheet.id: self.reopen_requested.emit(sid))
            menu.addAction(act)

    def _closed_menu(self) -> None:
        menu = QMenu(self)
        self._fill_reopen_menu(menu)
        menu.exec(self.closed_btn.mapToGlobal(self.closed_btn.rect().bottomLeft()))

    def _tab_menu(self, pos) -> None:
        index = self.tabs.tabAt(pos)
        sid = self._sheet_id(index)
        menu = QMenu(self)
        if sid:
            rename = QAction("Rename page…", self)
            rename.triggered.connect(lambda: self.rename_requested.emit(sid))
            close = QAction("Close page", self)
            close.triggered.connect(lambda: self.close_requested.emit(sid))
            delete = QAction("Delete page…", self)
            delete.triggered.connect(lambda: self.delete_requested.emit(sid))
            earlier = QAction("Move earlier", self)
            earlier.triggered.connect(lambda: self.move_requested.emit(sid, "earlier"))
            later = QAction("Move later", self)
            later.triggered.connect(lambda: self.move_requested.emit(sid, "later"))
            menu.addAction(earlier)
            menu.addAction(later)
            menu.addAction(rename)
            menu.addAction(close)
            menu.addAction(delete)
            menu.addSeparator()
        add = QAction("New page…", self)
        add.triggered.connect(self.add_requested.emit)
        menu.addAction(add)
        reopen = menu.addMenu("Reopen closed page")
        self._fill_reopen_menu(reopen)
        menu.exec(self.tabs.mapToGlobal(pos))
