"""Catalog part picker — search verified manufacturer SKUs and assign one."""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import QUrl

from panelcad.api.session import Session
from panelcad.library.parts import CatalogPart


COLUMNS = (
    "Manufacturer",
    "Catalog",
    "Verified",
    "Ratings",
    "Description",
    "Source",
)


class PartPickerDialog(QDialog):
    """Search manufacturer / catalog / rating; filter by category."""

    def __init__(
        self,
        session: Session,
        parent: QWidget | None = None,
        *,
        query: str = "",
        manufacturer: str = "",
        category: str = "",
        library_id: str = "",
    ) -> None:
        super().__init__(parent)
        self.session = session
        self._library_id = library_id
        self._hits: list[dict[str, Any]] = []
        self.setWindowTitle("Catalog part")
        self.resize(980, 560)
        layout = QVBoxLayout(self)
        hint = QLabel(
            "Verified manufacturer SKUs only. Partial entries have a source but "
            "are missing a published field — they still flag as unverified_part."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("manufacturer, catalog number, or rating…")
        self.search.setText(query)
        self.search.textChanged.connect(self._rebuild)
        filters.addWidget(self.search, 2)

        self.mfr = QComboBox()
        self.mfr.addItem("All manufacturers", "")
        for name in session.parts.manufacturers():
            self.mfr.addItem(name, name)
        if manufacturer:
            idx = self.mfr.findData(manufacturer)
            if idx < 0:
                want = manufacturer.casefold()
                for i in range(self.mfr.count()):
                    if str(self.mfr.itemData(i) or "").casefold() == want:
                        idx = i
                        break
            if idx >= 0:
                self.mfr.setCurrentIndex(idx)
        self.mfr.currentIndexChanged.connect(self._rebuild)
        filters.addWidget(self.mfr)

        self.cat = QComboBox()
        self.cat.addItem("All categories", "")
        for name in session.parts.categories():
            self.cat.addItem(name, name)
        if category:
            idx = self.cat.findData(category)
            if idx >= 0:
                self.cat.setCurrentIndex(idx)
        self.cat.currentIndexChanged.connect(self._rebuild)
        filters.addWidget(self.cat)

        self.amps_min = QLineEdit()
        self.amps_min.setPlaceholderText("amps min")
        self.amps_min.setFixedWidth(80)
        self.amps_min.setToolTip("Keep parts whose published amp or range overlaps this minimum")
        self.amps_min.textChanged.connect(self._rebuild)
        filters.addWidget(self.amps_min)

        self.amps_max = QLineEdit()
        self.amps_max.setPlaceholderText("amps max")
        self.amps_max.setFixedWidth(80)
        self.amps_max.setToolTip("Keep parts whose published amp or range overlaps this maximum")
        self.amps_max.textChanged.connect(self._rebuild)
        filters.addWidget(self.amps_max)
        layout.addLayout(filters)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(self._accept_if_row)
        self.table.cellClicked.connect(self._maybe_open_source)
        layout.addWidget(self.table, 1)

        self.status = QLabel("")
        layout.addWidget(self.status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_row)
        buttons.rejected.connect(self.reject)
        open_btn = QPushButton("Open source")
        open_btn.clicked.connect(self._open_selected_source)
        buttons.addButton(open_btn, QDialogButtonBox.ButtonRole.ActionRole)
        layout.addWidget(buttons)

        self._rebuild()
        if self.table.rowCount():
            self.table.selectRow(0)

    def _rebuild(self) -> None:
        manufacturer = str(self.mfr.currentData() or "") or None
        category = str(self.cat.currentData() or "") or None
        self._hits = self.session.search_parts(
            self.search.text(),
            manufacturer=manufacturer,
            category=category,
            amps_min=_optional_amps(self.amps_min.text()),
            amps_max=_optional_amps(self.amps_max.text()),
        )
        self.table.setRowCount(len(self._hits))
        for row, part in enumerate(self._hits):
            values = [
                str(part.get("manufacturer") or ""),
                str(part.get("catalog") or ""),
                str(part.get("verified") or ""),
                str(part.get("ratings") or ""),
                str(part.get("description") or ""),
                str(part.get("source") or ""),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col == 2:
                    from panelcad.gui.theme import ui_qcolor

                    verified = text.lower()
                    if verified == "full":
                        item.setForeground(ui_qcolor("part_ok"))
                    elif verified == "partial":
                        item.setForeground(ui_qcolor("part_warn"))
                if col == 5:
                    from panelcad.gui.theme import ui_qcolor

                    item.setForeground(ui_qcolor("part_link"))
                    item.setToolTip("Official manufacturer page or datasheet")
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()
        self.status.setText(f"{len(self._hits)} verified catalog part(s)")

    def selected_row(self) -> dict[str, Any] | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        idx = rows[0].row()
        if idx < 0 or idx >= len(self._hits):
            return None
        return self._hits[idx]

    def selected_part(self) -> CatalogPart | None:
        row = self.selected_row()
        if row is None:
            return None
        return self.session.parts.get(str(row["catalog"]), str(row.get("manufacturer") or "") or None)

    def _accept_if_row(self) -> None:
        if self.selected_part() is None:
            return
        self.accept()

    def _maybe_open_source(self, row: int, column: int) -> None:
        if column != 5:  # Source
            return
        url = str(self._hits[row].get("source") or "")
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _open_selected_source(self) -> None:
        row = self.selected_row()
        if row and row.get("source"):
            QDesktopServices.openUrl(QUrl(str(row["source"])))


def _optional_amps(text: str) -> float | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def pick_catalog_part(
    parent: QWidget | None,
    session: Session,
    *,
    query: str = "",
    manufacturer: str = "",
    category: str = "",
    library_id: str = "",
) -> CatalogPart | None:
    dialog = PartPickerDialog(
        session,
        parent,
        query=query,
        manufacturer=manufacturer,
        category=category,
        library_id=library_id,
    )
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.selected_part()
