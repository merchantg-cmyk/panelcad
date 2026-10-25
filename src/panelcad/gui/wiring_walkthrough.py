"""Technician Wiring Walkthrough & Wire-by-Wire Cut Sheet panel for PanelCAD."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.core.panel_wiring import (
    WireByWireSchedule,
    add_wire_by_wire_sheet,
    build_wire_by_wire_schedule,
    export_wire_by_wire_csv,
    get_panel_wiring_mode,
    get_walkthrough_step,
    list_zip_tie_waypoints,
    mark_wire_installed,
    remove_zip_tie_waypoint,
    set_panel_wiring_mode,
    set_walkthrough_step,
    step_walkthrough,
)


class WiringWalkthroughPanel(QWidget):
    """Interactive dock/panel for Backpanel Wire Routing, Zip-Ties, and Technician Walkthrough."""

    changed = Signal()
    slice_tool_requested = Signal()
    status_message = Signal(str)
    sheet_added = Signal(str)

    def __init__(self, session: Session | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("wiring-walkthrough-panel")
        self.session: Session | None = session
        self._syncing = False
        self._schedule: WireByWireSchedule | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # Top control bar
        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)

        top_bar.addWidget(QLabel("Wiring View:"))
        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("wiring-mode-combo")
        self.mode_combo.addItem("Off", "off")
        self.mode_combo.addItem("Show All Bundles", "all")
        self.mode_combo.addItem("Technician Walkthrough (1-by-1)", "walkthrough")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_combo_changed)
        top_bar.addWidget(self.mode_combo)

        self.prev_btn = QPushButton("◀ Previous Wire")
        self.prev_btn.setObjectName("wiring-prev-btn")
        self.prev_btn.clicked.connect(self.previous_wire)
        top_bar.addWidget(self.prev_btn)

        self.next_btn = QPushButton("Next Wire ▶")
        self.next_btn.setObjectName("wiring-next-btn")
        self.next_btn.clicked.connect(self.next_wire)
        top_bar.addWidget(self.next_btn)

        self.step_label = QLabel("Wire 0 of 0")
        self.step_label.setObjectName("wiring-step-label")
        self.step_label.setStyleSheet("font-weight: 600;")
        top_bar.addWidget(self.step_label, 1)

        self.installed_check = QCheckBox("Mark Wire Installed")
        self.installed_check.setObjectName("wiring-installed-check")
        self.installed_check.toggled.connect(self._on_installed_toggled)
        top_bar.addWidget(self.installed_check)

        self.slice_btn = QPushButton("✂ Zip-Tie Slice Tool")
        self.slice_btn.setObjectName("wiring-slice-btn")
        self.slice_btn.setToolTip("Drag across panel wires on the layout sheet to bundle them into a Zip-Tie waypoint")
        self.slice_btn.clicked.connect(self.slice_tool_requested.emit)
        top_bar.addWidget(self.slice_btn)

        self.clear_ties_btn = QPushButton("Clear Zip-Ties")
        self.clear_ties_btn.setObjectName("wiring-clear-ties-btn")
        self.clear_ties_btn.clicked.connect(self.clear_zip_ties)
        top_bar.addWidget(self.clear_ties_btn)

        self.add_sheet_btn = QPushButton("+ Add Wire-by-Wire Sheet")
        self.add_sheet_btn.setObjectName("wiring-add-sheet-btn")
        self.add_sheet_btn.clicked.connect(self.create_wire_by_wire_sheet)
        top_bar.addWidget(self.add_sheet_btn)

        self.export_csv_btn = QPushButton("Export CSV…")
        self.export_csv_btn.setObjectName("wiring-export-csv-btn")
        self.export_csv_btn.clicked.connect(self.export_csv_dialog)
        top_bar.addWidget(self.export_csv_btn)

        root.addLayout(top_bar)

        # Wire-by-Wire Cut Sheet Table
        self.wire_by_wire_table = QTableWidget(0, 9)
        self.wire_by_wire_table.setObjectName("wire-by-wire-table")
        self.wire_by_wire_table.setHorizontalHeaderLabels(
            [
                "Step",
                "Wire #",
                "From",
                "To",
                "Spec",
                "Route / Zip-Ties",
                "Cut (in)",
                "Cut (mm)",
                "Installed",
            ]
        )
        self.wire_by_wire_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.wire_by_wire_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.wire_by_wire_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hdr = self.wire_by_wire_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.wire_by_wire_table.cellClicked.connect(self._on_table_row_clicked)
        root.addWidget(self.wire_by_wire_table, 1)

        if self.session is not None:
            self.refresh()

    @property
    def table(self) -> QTableWidget:
        return self.wire_by_wire_table

    def set_session(self, session: Session) -> None:
        self.session = session
        self.refresh()

    def refresh(self) -> None:
        if self.session is None:
            return
        self._syncing = True
        try:
            try:
                sched = build_wire_by_wire_schedule(self.session)
                mode = get_panel_wiring_mode(self.session)
                step = get_walkthrough_step(self.session)
            except Exception:
                self._schedule = None
                self.wire_by_wire_table.setRowCount(0)
                self.step_label.setText("No panel layout sheet")
                return

            self._schedule = sched
            idx = self.mode_combo.findData(mode)
            if idx >= 0:
                self.mode_combo.setCurrentIndex(idx)

            rows = sched.rows
            self.wire_by_wire_table.setRowCount(len(rows))
            for r_idx, row in enumerate(rows):
                via_str = " -> ".join(row.via) if row.via else "direct"
                cells = [
                    str(row.step_index + 1),
                    row.wire_number,
                    f"{row.from_tag}:{row.from_pin}",
                    f"{row.to_tag}:{row.to_pin}",
                    f"{row.gauge} {row.color}".strip(),
                    via_str,
                    f'{row.cut_length_in:.1f}"',
                    f"{row.cut_length_mm:.0f} mm",
                    "DONE" if row.installed else "TODO",
                ]
                for c_idx, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, row.step_index)
                    self.wire_by_wire_table.setItem(r_idx, c_idx, item)

            if rows:
                active_idx = max(0, min(step, len(rows) - 1))
                active = rows[active_idx]
                self.wire_by_wire_table.selectRow(active_idx)
                self.step_label.setText(
                    f"Wire {active_idx + 1} of {len(rows)} — #{active.wire_number} "
                    f"({active.gauge} {active.color}) — {active.from_tag}:{active.from_pin} → "
                    f"{active.to_tag}:{active.to_pin} — Cut: {active.cut_length_in:.1f}\" ({active.cut_length_mm:.0f} mm)"
                )
                self.installed_check.setEnabled(True)
                self.installed_check.setChecked(bool(active.installed))
            else:
                self.step_label.setText("Wire 0 of 0")
                self.installed_check.setEnabled(False)
                self.installed_check.setChecked(False)
        finally:
            self._syncing = False

    def set_mode(self, mode: str) -> None:
        if self.session is None:
            return
        set_panel_wiring_mode(self.session, mode)
        self.refresh()
        self.changed.emit()

    def _on_mode_combo_changed(self, index: int) -> None:
        if self._syncing or self.session is None:
            return
        mode = str(self.mode_combo.itemData(index) or "off")
        set_panel_wiring_mode(self.session, mode)
        self.refresh()
        self.changed.emit()

    def previous_wire(self) -> dict[str, Any]:
        if self.session is None:
            return {}
        res = step_walkthrough(self.session, delta=-1)
        self.refresh()
        self.changed.emit()
        return res

    def next_wire(self) -> dict[str, Any]:
        if self.session is None:
            return {}
        res = step_walkthrough(self.session, delta=1)
        self.refresh()
        self.changed.emit()
        return res

    def select_wire_step(self, step_index: int) -> dict[str, Any]:
        if self.session is None:
            return {}
        res = set_walkthrough_step(self.session, step_index)
        self.refresh()
        self.changed.emit()
        return res

    def _on_table_row_clicked(self, row: int, _col: int) -> None:
        if self._syncing or self.session is None:
            return
        self.select_wire_step(row)

    def _on_installed_toggled(self, checked: bool) -> None:
        if self._syncing or self.session is None:
            return
        step = get_walkthrough_step(self.session)
        mark_wire_installed(self.session, step, installed=bool(checked))
        self.refresh()
        self.changed.emit()

    def clear_zip_ties(self) -> None:
        if self.session is None:
            return
        ties = list_zip_tie_waypoints(self.session)
        for tie in ties:
            remove_zip_tie_waypoint(self.session, None, tie.id)
        self.refresh()
        self.changed.emit()
        self.status_message.emit("Cleared all zip-tie waypoints")

    def create_wire_by_wire_sheet(self) -> str | None:
        if self.session is None:
            return None
        sheet = add_wire_by_wire_sheet(self.session)
        self.refresh()
        self.sheet_added.emit(sheet.id)
        self.changed.emit()
        self.status_message.emit(f"Added Wire-by-Wire Cut Sheet ({sheet.id})")
        return sheet.id

    def export_csv_dialog(self) -> Path | None:
        if self.session is None:
            return None
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export Wire-by-Wire Cut Sheet CSV",
            "wire_by_wire_cut_sheet.csv",
            "CSV Files (*.csv)",
        )
        if not path_str:
            return None
        out = export_wire_by_wire_csv(self.session, path_str)
        self.status_message.emit(f"Exported Wire-by-Wire CSV to {out}")
        return out
