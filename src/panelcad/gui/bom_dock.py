from __future__ import annotations

import json
from collections.abc import Callable

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QStyleFactory,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.gui.canvas import BOM_MIME
from panelcad.io.bom import instance_rows

ROW_KIND = Qt.ItemDataRole.UserRole + 32

EDITABLE = {"manufacturer", "part_number", "catalog", "notes", "incabinet"}
COLUMNS = [
    "item",
    "qty",
    "ref",
    "description",
    "manufacturer",
    "part_number",
    "catalog",
    "source",
    "catalog_fallback",
    "catalog_primary",
    "width_mm",
    "height_mm",
    "depth_mm",
    "din_rail",
    "incabinet",
    "mount",
    "notes",
    "library_id",
    "sheet",
]
HEADERS = {
    "item": "Item",
    "qty": "Qty",
    "ref": "Ref",
    "description": "Description",
    "manufacturer": "Manufacturer",
    "part_number": "Part number",
    "catalog": "Catalog",
    "source": "Source",
    "catalog_fallback": "Catalog fallback",
    "catalog_primary": "Catalog primary",
    "width_mm": "Width mm",
    "height_mm": "Height mm",
    "depth_mm": "Depth mm",
    "din_rail": "DIN rail",
    "incabinet": "In cabinet",
    "mount": "Mount",
    "notes": "Notes",
    "library_id": "Library",
    "sheet": "Sheet",
}


class BomTable(QTableWidget):
    """Drag a BOM row onto a sheet. Library part is not copied — only placed."""

    def startDrag(self, supportedActions) -> None:  # noqa: N802
        rows = self.selectionModel().selectedRows() if self.selectionModel() else []
        if not rows:
            return
        row = rows[0].row()
        lib_item = self.item(row, COLUMNS.index("library_id"))
        ref_item = self.item(row, COLUMNS.index("ref"))
        if lib_item is None or lib_item.data(ROW_KIND) not in (None, "part"):
            return
        payload = json.dumps(
            {"library_id": lib_item.text(), "ref": ref_item.text() if ref_item else ""}
        ).encode("utf-8")
        mime = QMimeData()
        mime.setData(BOM_MIME, payload)
        mime.setText(lib_item.text())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


class BomDock(QWidget):
    select_requested = Signal(str)
    edit_requested = Signal(str)
    address_requested = Signal(str)
    delete_requested = Signal(str)
    oem_requested = Signal(list)
    oem_all_requested = Signal()
    place_on_sheet_requested = Signal(str, str)
    catalog_requested = Signal()
    resolve_requested = Signal(str, str)
    lookup_all_requested = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        on_export: Callable[[], None] | None = None,
        on_export_grouped: Callable[[], None] | None = None,
        on_import: Callable[[], None] | None = None,
        on_select_ref: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._session: Session | None = None
        self._on_export = on_export
        self._on_export_grouped = on_export_grouped
        self._on_import = on_import
        self._on_select_ref = on_select_ref
        self._rows: list[dict[str, str]] = []
        self._needs_rows: list[dict[str, str]] = []
        from panelcad.theme import MARGIN_PX, SPACING_PX

        layout = QVBoxLayout(self)
        layout.setContentsMargins(MARGIN_PX, MARGIN_PX, MARGIN_PX, MARGIN_PX)
        layout.setSpacing(SPACING_PX)
        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("Bill of materials"))
        title_row.addStretch()
        layout.addLayout(title_row)
        buttons = QGridLayout()
        buttons.setHorizontalSpacing(SPACING_PX)
        buttons.setVerticalSpacing(SPACING_PX)
        btn_imp = QPushButton("Import CSV…")
        btn_imp.setObjectName("importCsvButton")
        from panelcad.gui.guidance import annotate

        annotate(btn_imp, "Import CSV…")
        btn_imp.setToolTip(
            "Import a parts-list CSV. Accepted columns: Tag/Ref, Part Number, "
            "Manufacturer, Description, Qty, and library_id (optional). "
            "Also catalog, notes, sheet, and sizes when you already have them. "
            "Aliases: part #, part, item number, model, model number, catalog number, "
            "cat no, manufacturer part number, brand, make, mfr, count, quantity required, "
            "qty req. A row with a part number or description and no tag or library id "
            "is kept as needs-info: no millimetres, off the BOM, quote, and cutouts "
            "until a library part is chosen. The print is marked NOT FOR BUILD."
        )
        btn_exp = QPushButton("Export CSV…")
        btn_grp = QPushButton("Export grouped…")
        btn_place = QPushButton("Place on sheet")
        btn_oem = QPushButton("Update OEM…")
        btn_all = QPushButton("Update all OEM…")
        btn_imp.clicked.connect(lambda: self._on_import() if self._on_import else None)
        btn_exp.clicked.connect(lambda: self._on_export() if self._on_export else None)
        btn_grp.clicked.connect(lambda: self._on_export_grouped() if self._on_export_grouped else None)
        btn_place.setToolTip(
            "Drop the selected BOM part onto the current sheet. "
            "One library object; schematic / ladder / Ethernet / panel each draw it differently."
        )
        btn_place.clicked.connect(self._emit_place)
        btn_oem.setToolTip("Ask the AI to pull catalog size, connection points, and a photo for the selected row(s).")
        btn_all.setToolTip("Ask the AI to pull OEM data for every part on the BOM.")
        self.lookup_all = QPushButton("Look up all")
        self.lookup_all.setObjectName("lookupAllNeedsInfo")
        self.lookup_all.setToolTip(
            "Ask the in-app AI for a datasheet proposal for every needs-info part. "
            "Nothing is applied until you accept a cited value."
        )
        btn_oem.clicked.connect(self._emit_oem)
        btn_all.clicked.connect(self.oem_all_requested.emit)
        self.lookup_all.clicked.connect(self.lookup_all_requested.emit)
        for index, button in enumerate(
            (btn_place, btn_imp, btn_exp, btn_grp, btn_oem, btn_all, self.lookup_all)
        ):
            buttons.addWidget(button, index // 3, index % 3)
        layout.addLayout(buttons)
        self.notice = QLabel("")
        self.notice.setObjectName("bomNeedsInfoNote")
        self.notice.setWordWrap(True)
        self.notice.hide()
        layout.addWidget(self.notice)
        from panelcad.gui.guidance import EmptyState

        self.empty = EmptyState()
        self.empty.clicked.connect(self._empty_clicked)
        layout.addWidget(self.empty)
        self.table = BomTable(0, len(COLUMNS))
        fusion = QStyleFactory.create("Fusion")
        if fusion is not None:
            fusion.setParent(self.table)
            self.table.setStyle(fusion)
        self.table.setHorizontalHeaderLabels([HEADERS[key] for key in COLUMNS])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setDragEnabled(True)
        self.table.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.table.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(True)
        self.table.viewport().setAutoFillBackground(True)
        self.apply_theme()
        self.table.viewport().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.viewport().customContextMenuRequested.connect(self._table_menu)
        self.table.itemSelectionChanged.connect(self._picked)
        self.table.itemChanged.connect(self._edited)
        layout.addWidget(self.table)

    def apply_theme(self) -> None:
        from panelcad.gui.theme import table_stylesheet

        self.table.setStyleSheet(table_stylesheet())

    def set_session(self, session: Session) -> None:
        self._session = session
        self.refresh()

    def refresh(self) -> None:
        if self._session is None:
            return
        from panelcad.needs_info import needs_info_display_rows

        selected = self._selected_refs()
        self._rows = instance_rows(self._session.project, self._session.catalog)
        self._needs_rows = needs_info_display_rows(self._session)
        display: list[tuple[str, dict[str, str]]] = [("part", row) for row in self._rows]
        if self._needs_rows:
            display.append(("header", {"description": "Needs info (not counted)"}))
            for row in self._needs_rows:
                display.append(("needs", row))
        sel_model = self.table.selectionModel()
        self.table.blockSignals(True)
        if sel_model is not None:
            sel_model.blockSignals(True)
        current = self.table.currentItem()
        if current is not None:
            self.table.closePersistentEditor(current)
        self.table.clearSpans()
        self.table.clearSelection()
        self.table.clearContents()
        self.table.setRowCount(0)
        self.table.setRowCount(len(display))
        for r, (kind, row) in enumerate(display):
            for c, key in enumerate(COLUMNS):
                text = "Needs info (not counted)" if kind == "header" and c == 0 else row.get(key, "")
                if kind == "header" and c != 0:
                    text = ""
                item = QTableWidgetItem(text)
                item.setData(ROW_KIND, kind)
                if kind != "part" or key not in EDITABLE:
                    flags = Qt.ItemFlag.ItemIsEnabled
                    if kind == "needs":
                        flags |= Qt.ItemFlag.ItemIsSelectable
                    item.setFlags(flags)
                self.table.setItem(r, c, item)
            if kind == "header":
                self.table.setSpan(r, 0, 1, len(COLUMNS))
        self.table.resizeColumnsToContents()
        if sel_model is not None:
            sel_model.blockSignals(False)
        self.table.blockSignals(False)
        if selected:
            for r in range(self.table.rowCount()):
                ref = self._ref_at_row(r)
                if ref in selected:
                    self.table.selectRow(r)
                    break
        self.table.viewport().update()
        self._show_empty()

    def _show_empty(self) -> None:
        from panelcad.guidance import bom_empty_copy

        if self._needs_rows:
            count = len(self._needs_rows)
            noun = "part" if count == 1 else "parts"
            self.notice.setText(
                f"NOT FOR BUILD: {count} {noun} still need info. "
                "Listed below and not counted in totals, the quote, cutouts, or exports."
            )
            self.notice.setVisible(True)
            self.empty.set_state("", ())
            return
        self.notice.setVisible(False)
        message = bom_empty_copy(len(self._rows), 0)
        if message:
            self.empty.set_state(
                message,
                (
                    ("Import a vendor parts list (CSV)", "import"),
                    ("Add from catalog", "catalog"),
                ),
            )
        else:
            self.empty.set_state("", ())

    def _empty_clicked(self, ident: str) -> None:
        if ident == "import" and self._on_import is not None:
            self._on_import()
        elif ident == "catalog":
            self.catalog_requested.emit()

    def _picked(self) -> None:
        refs = self._selected_refs()
        if not refs:
            return
        self.select_requested.emit(refs[0])

    def _ref_at_row(self, row: int) -> str | None:
        if row < 0:
            return None
        item = self.table.item(row, COLUMNS.index("ref"))
        if item is None:
            return None
        text = item.text().strip()
        if not text:
            return None
        return text.split(",")[0].strip()

    def _row_kind(self, row: int) -> str:
        if row < 0:
            return ""
        item = self.table.item(row, 0)
        if item is None:
            return ""
        return str(item.data(ROW_KIND) or "")

    def _table_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        kind = self._row_kind(row)
        if kind == "header":
            return
        ref = self._ref_at_row(row)
        if not ref:
            return
        self.table.selectRow(row)
        if kind == "needs":
            menu = QMenu(self)
            menu.addAction("Match to catalog part…", lambda r=ref: self.resolve_requested.emit("match", r))
            menu.addAction(
                "Enter dimensions from the datasheet…",
                lambda r=ref: self.resolve_requested.emit("manual", r),
            )
            menu.addAction("AI lookup…", lambda r=ref: self.resolve_requested.emit("lookup", r))
            menu.addAction(
                "What's missing / where I looked…",
                lambda r=ref: self.resolve_requested.emit("note", r),
            )
            menu.exec(self.table.viewport().mapToGlobal(pos))
            return
        menu = QMenu(self)
        lib = ""
        lib_item = self.table.item(row, COLUMNS.index("library_id"))
        if lib_item is not None:
            lib = lib_item.text()
        menu.addAction(
            "Place on this sheet",
            lambda r=ref, lid=lib: self.place_on_sheet_requested.emit(lid, r),
        )
        menu.addAction("Select on sheet", lambda r=ref: self.select_requested.emit(r))
        menu.addAction("Object editor…", lambda r=ref: self.edit_requested.emit(r))
        menu.addAction("Set address…", lambda r=ref: self.address_requested.emit(r))
        menu.addSeparator()
        menu.addAction("Update OEM…", self._emit_oem)
        menu.addAction("Update all OEM…", self.oem_all_requested.emit)
        menu.addSeparator()
        menu.addAction("Delete", lambda r=ref: self.delete_requested.emit(r))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _selected_refs(self) -> list[str]:
        refs: list[str] = []
        for idx in self.table.selectionModel().selectedRows():
            ref = self._ref_at_row(idx.row())
            if ref:
                refs.append(ref)
        return refs

    def _emit_place(self) -> None:
        refs = self._selected_refs()
        if not refs:
            return
        row = None
        for idx in self.table.selectionModel().selectedRows():
            row = idx.row()
            break
        if row is None:
            return
        lib_item = self.table.item(row, COLUMNS.index("library_id"))
        if lib_item is None or lib_item.data(ROW_KIND) not in (None, "part"):
            return
        self.place_on_sheet_requested.emit(lib_item.text(), refs[0])

    def _emit_oem(self) -> None:
        refs = self._selected_refs()
        if refs:
            self.oem_requested.emit(refs)

    def _edited(self, item: QTableWidgetItem) -> None:
        if self._session is None:
            return
        if item.data(ROW_KIND) not in (None, "part"):
            return
        key = COLUMNS[item.column()]
        if key not in EDITABLE:
            return
        ref_item = self.table.item(item.row(), COLUMNS.index("ref"))
        if ref_item is None:
            return
        ref = ref_item.text()
        for sheet in self._session.project.sheets:
            for comp in sheet.components:
                if comp.id == ref:
                    text = item.text()
                    if key == "incabinet":
                        flag = text.strip().lower() in ("1", "true", "yes", "y")
                        self._session.set_component_properties(
                            ref, {"incabinet": flag, "in_panel": flag}
                        )
                    elif key in {"catalog", "part_number"}:
                        manufacturer = str(comp.properties.get("manufacturer") or "").strip() or None
                        assigned = self._session.try_assign_sku(ref, text, manufacturer)
                        if assigned is None:
                            self._session.set_component_properties(ref, {key: text})
                        self.refresh()
                    elif key == "manufacturer":
                        sku = str(
                            comp.properties.get("catalog") or comp.properties.get("part_number") or ""
                        ).strip()
                        assigned = self._session.try_assign_sku(ref, sku, text) if sku else None
                        if assigned is None:
                            self._session.set_component_properties(ref, {key: text})
                        self.refresh()
                    else:
                        self._session.set_component_properties(ref, {key: text})
                    return
