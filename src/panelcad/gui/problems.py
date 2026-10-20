"""Problems dock and a debounced background runner for live design checks."""

from __future__ import annotations

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.core.checks import LIVE_DEBOUNCE_MS
from panelcad.core.models import Project
from panelcad.gui.theme import ui_font

COLUMNS = ("sev", "check", "message", "sheet", "ref")
_SEV_ROLE = {
    "error": "sev_error",
    "warning": "sev_warning",
    "info": "sev_info",
}


class _CheckWorker(QThread):
    finished_rows = Signal(list)

    def __init__(
        self,
        payload: dict,
        catalog,
        include_suppressed: bool,
        parts=None,
        project_path=None,
    ) -> None:
        super().__init__()
        self._payload = payload
        self._catalog = catalog
        self._include = include_suppressed
        self._parts = parts
        self._project_path = project_path

    def run(self) -> None:
        from panelcad.core.checks import run_checks

        project = Project.model_validate(self._payload)
        rows = [
            item.to_row()
            for item in run_checks(
                project,
                self._catalog,
                include_suppressed=self._include,
                parts=self._parts,
                project_path=self._project_path,
            )
        ]
        self.finished_rows.emit(rows)


class LiveCheckRunner(QObject):
    """Run ``design_checks`` on a snapshot after the drawing settles."""

    rows_ready = Signal(list)

    def __init__(self, session: Session, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.include_suppressed = True
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(LIVE_DEBOUNCE_MS)
        self._timer.timeout.connect(self._kick)
        self._worker: _CheckWorker | None = None
        self._busy = False
        self._again = False
        self._generation = 0

    def schedule(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        """Stop the debounce timer and wait out an in-flight check thread."""

        self._timer.stop()
        self._again = False
        self._generation += 1
        worker = self._worker
        self._worker = None
        if worker is not None and worker.isRunning():
            worker.wait(5000)
        if worker is not None:
            worker.deleteLater()
        self._busy = False

    def run_now(self) -> None:
        self._timer.stop()
        self._kick()

    def _kick(self) -> None:
        if self._busy:
            self._again = True
            return
        self._busy = True
        self._generation += 1
        gen = self._generation
        payload = self.session.project.model_dump(mode="python")
        worker = _CheckWorker(
            payload,
            self.session.catalog,
            self.include_suppressed,
            self.session.parts,
            self.session.path,
        )
        self._worker = worker

        def _done(rows: list) -> None:
            if gen != self._generation:
                return
            self._busy = False
            worker.deleteLater()
            if self._worker is worker:
                self._worker = None
            self.rows_ready.emit(rows)
            if self._again:
                self._again = False
                self.schedule()

        worker.finished_rows.connect(_done)
        worker.finished.connect(worker.deleteLater)
        worker.start()


# The check id stays on the row. The Check column shows words.
_CHECK_LABELS = {
    "format": "File format",
    "duplicate_sheet": "Duplicate sheet",
    "duplicate_rail": "Duplicate rail",
    "rail_length": "Rail length",
    "duplicate_tag": "Duplicate tag",
    "duplicate_device_tag": "Duplicate device tag",
    "unknown_library": "Unknown symbol",
    "footprint_overlap": "Parts overlap",
    "wire_same_pin": "Wire lands twice on one pin",
    "dangling_wire": "Wire end is open",
    "unconnected_device": "Device not wired",
    "wire_number_conflict": "Wire number used twice",
    "missing_panel": "No panel layout",
    "missing_door": "No door layout",
    "missing_schematic": "No schematic",
    "terminal_missing": "Terminal missing",
    "terminal_not_on_strip": "Terminal not on a strip",
    "missing_part_number": "No part number",
    "unverified_part": "Part not verified",
    "catalog_missing_dimensions": "Catalog part has no size",
    "protection_over_downstream": "Protection larger than the load",
    "overload_outside_fla": "Overload range misses the motor current",
    "coil_voltage_mismatch": "Coil voltage does not match",
    "outside_enclosure": "Part outside the enclosure",
    "heat_rise_over_limit": "Enclosure too hot",
    "above_max_ambient": "Above the part's max temperature",
    "nema4_filter_fan": "Filter fan on a sealed box",
    "missing_footprint": "No size for layout",
    "contact_without_coil": "Contact has no coil",
    "unwired_coil": "Coil not wired",
    "contact_count_exceeded": "Too many contacts",
    "duplicate_contact_number": "Duplicate contact number",
    "broken_offpage": "Off-page arrow is broken",
    "control_power_margin": "Control power margin",
    "control_power_inrush": "Control power inrush",
    "control_power_protection": "Control power protection",
    "control_power_method": "Control power method",
    "sccr_below_available": "Short-circuit rating too low",
    "shield_ungrounded": "Shield not grounded",
    "instrument_shield_both_ends": "Instrument shield grounded at both ends",
    "wire_undersized": "Wire too small",
    "protection_above_430_52": "Protection above the motor table",
    "overload_range_misses_fla": "Overload range misses the motor current",
    "overload_needs_nameplate": "Overload needs the nameplate",
    "breaker_frame_below_rating": "Breaker frame too small",
    "contactor_undersized": "Contactor too small",
    "duplicate_address": "Duplicate address",
    "placeholder_footprint": "Placeholder size",
    "layout_not_ready": "Not ready for layout",
    "symbol_on_title_block": "Symbol on the title block",
    "print_sheet_number": "Sheet number",
    "print_contact_state": "Contact state",
    "print_needs_review": "Needs review",
    "print_evidence": "Not on the print",
    "print_conflict": "Conflicting reading",
    "print_unknown_part": "Unknown part on the print",
    "print_unplaced": "Not placed from the print",
    "print_unreadable": "Print text not read",
    "print_unresolved_xref": "Cross-reference not resolved",
    "print_low_confidence": "Low confidence read",
    "print_wire": "Wire on the print",
    "reconstruct_photo_missing": "Photo file missing",
    "soft_starter_overload": "Soft starter overload",
    "cutout_unknown": "Unknown cutout",
    "cutout_unresolved": "Cutout not resolved",
    "cutout_edge": "Cutout too close to the edge",
    "cutout_flange": "Cutout hits the flange",
    "cutout_overlap": "Cutouts overlap",
    "cutout_clearance": "Cutout clearance",
    "reconstruct_unconfirmed": "Not ready for layout",
    "reconstruct_unscaled": "Needs a scale",
    "reconstruct_scale_perspective": "Scale",
    "reconstruct_inferred_wire": "Wire guessed from a photo",
    "reconstruct_generic_layout": "Not ready for layout",
    "reconstruct_print_mismatch": "Photo and print disagree",
    "reconstruct_print_unextracted": "Print not read",
    "reconstruct_print_missing_on_photos": "On the print, not in the photos",
    "reconstruct_print_missing_on_print": "In the photos, not on the print",
    "reconstruct_print_catalog_mismatch": "Photo and print part numbers disagree",
    "reconstruct_print_wire_missing": "Wire missing between photo and print",
    "reconstruct_unreadable": "Label not read",
    "reconstruct_needs_info": "Not ready for layout",
    "nameplate_missing_field": "Nameplate field missing",
    "nameplate_sccr_below_available": "Nameplate short-circuit rating too low",
    "nameplate_fla_above_ocpd": "Nameplate current above the breaker",
    "nameplate_flc_approximate": "Nameplate current is an estimate",
    "nameplate_unconfirmed": "Nameplate not confirmed",
    "power_columns_overflow": "Power columns do not fit",
}


def check_label(check: str) -> str:
    key = str(check or "")
    return _CHECK_LABELS.get(key, key)


class _CheckIdDelegate(QStyledItemDelegate):
    """Keep both ends of a long check id when the column is narrower than the text."""

    def paint(self, painter, option, index) -> None:  # noqa: N802
        option.textElideMode = Qt.TextElideMode.ElideMiddle
        super().paint(painter, option, index)


class _MessageDelegate(QStyledItemDelegate):
    """Show the whole finding. A long line wraps instead of ending in an ellipsis."""

    def paint(self, painter, option, index) -> None:  # noqa: N802
        option.textElideMode = Qt.TextElideMode.ElideNone
        super().paint(painter, option, index)


class ProblemsDock(QWidget):
    jump_requested = Signal(dict)
    fix_requested = Signal(dict)
    suppress_requested = Signal(str)
    unsuppress_requested = Signal(str)
    export_requested = Signal()
    print_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, str]] = []
        from panelcad.theme import MARGIN_PX, SPACING_PX

        layout = QVBoxLayout(self)
        layout.setContentsMargins(MARGIN_PX, MARGIN_PX, MARGIN_PX, MARGIN_PX)
        layout.setSpacing(SPACING_PX)
        head = QHBoxLayout()
        self.caption = QLabel("Problems — live design checks")
        head.addWidget(self.caption)
        head.addStretch()
        self.filter = QComboBox()
        self.filter.addItem("All", "all")
        self.filter.addItem("Errors", "error")
        self.filter.addItem("Warnings", "warning")
        self.filter.addItem("Info", "info")
        self.filter.currentIndexChanged.connect(self._rebuild)
        head.addWidget(self.filter)
        self.show_ignored = QCheckBox("Show ignored")
        self.show_ignored.setChecked(False)
        self.show_ignored.toggled.connect(self._rebuild)
        head.addWidget(self.show_ignored)
        layout.addLayout(head)
        btns = QHBoxLayout()
        jump = QPushButton("Jump")
        jump.clicked.connect(self._jump_current)
        self.fix = QPushButton("Fix")
        self.fix.setToolTip("Assign a catalog part or confirm an envelope")
        self.fix.clicked.connect(self._fix_current)
        ignore = QPushButton("Ignore")
        ignore.clicked.connect(self._ignore_current)
        restore = QPushButton("Restore")
        restore.clicked.connect(self._restore_current)
        export = QPushButton("Export…")
        export.clicked.connect(self.export_requested.emit)
        btns.addWidget(jump)
        btns.addWidget(self.fix)
        btns.addWidget(ignore)
        btns.addWidget(restore)
        btns.addWidget(export)
        btns.addStretch()
        layout.addLayout(btns)
        from panelcad.gui.guidance import EmptyState

        self.empty = EmptyState()
        self.empty.clicked.connect(self._empty_clicked)
        layout.addWidget(self.empty)
        self.table = QTableWidget(0, len(COLUMNS))
        # The message column is compared to this font. A leaked application
        # face must not make the advance twice the column width.
        self.setFont(ui_font())
        self.table.setFont(ui_font())
        self.table.setHorizontalHeaderLabels(["Sev", "Check", "Message", "Sheet", "Ref"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.cellDoubleClicked.connect(lambda *_a: self._jump_current())
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.setItemDelegateForColumn(1, _CheckIdDelegate(self.table))
        self.table.setItemDelegateForColumn(2, _MessageDelegate(self.table))
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._fitting = False
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(48)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)
        self._show_empty(0)

    def set_rows(self, rows: list[dict[str, str]]) -> None:
        self._rows = list(rows)
        self._rebuild()

    def visible_rows(self) -> list[dict[str, str]]:
        want = str(self.filter.currentData() or "all")
        show_ignored = self.show_ignored.isChecked()
        out: list[dict[str, str]] = []
        for row in self._rows:
            if row.get("suppressed") == "yes" and not show_ignored:
                continue
            if want != "all" and row.get("severity") != want:
                continue
            out.append(row)
        return out

    def counts(self) -> dict[str, int]:
        counts = {"error": 0, "warning": 0, "info": 0, "suppressed": 0}
        for row in self._rows:
            if row.get("suppressed") == "yes":
                counts["suppressed"] += 1
                continue
            sev = str(row.get("severity") or "")
            if sev in counts:
                counts[sev] += 1
        return counts

    def apply_theme(self) -> None:
        from panelcad.gui.theme import table_stylesheet

        self.table.setStyleSheet(table_stylesheet())
        self._rebuild()

    def _rebuild(self) -> None:
        self.setFont(ui_font())
        self.table.setFont(ui_font())
        rows = self.visible_rows()
        counts = self.counts()
        n = counts["error"] + counts["warning"] + counts["info"]
        self.caption.setText(
            f"Problems — {n} live  ({counts['error']} error, {counts['warning']} warn, "
            f"{counts['info']} info, {counts['suppressed']} ignored)"
        )
        table = self.table
        header = table.horizontalHeader()
        vheader = table.verticalHeader()
        sorting = table.isSortingEnabled()
        wrapping = table.wordWrap()
        hmodes = [header.sectionResizeMode(col) for col in range(table.columnCount())]
        # Resize-to-contents plus word wrap re-lays the whole table on every
        # setItem. On a thousand findings that is quadratic. Fill with layout
        # suspended, then size the columns and rows once.
        table.setUpdatesEnabled(False)
        table.setSortingEnabled(False)
        table.setWordWrap(False)
        vheader.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        for col in range(table.columnCount()):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
        try:
            table.setRowCount(len(rows))
            bold = QFont()
            bold.setBold(True)
            from panelcad.gui.theme import ui_qcolor

            colors = {sev: ui_qcolor(role) for sev, role in _SEV_ROLE.items()}
            text_color = ui_qcolor("text")
            for i, row in enumerate(rows):
                sev = str(row.get("severity") or "")
                values = (
                    sev,
                    check_label(str(row.get("check") or "")),
                    row.get("message") or "",
                    row.get("sheet") or "",
                    row.get("ref") or "",
                )
                color = colors.get(sev, text_color)
                for col, text in enumerate(values):
                    item = QTableWidgetItem(str(text))
                    item.setData(Qt.ItemDataRole.UserRole, row)
                    item.setForeground(color)
                    item.setFont(ui_font())
                    if row.get("suppressed") == "yes":
                        font = item.font()
                        font.setStrikeOut(True)
                        item.setFont(font)
                    elif sev == "error" and col == 0:
                        item.setFont(bold)
                    item.setToolTip(str(text))
                    table.setItem(i, col, item)
        finally:
            for col, mode in enumerate(hmodes):
                header.setSectionResizeMode(col, mode)
            vheader.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            table.setWordWrap(wrapping)
            try:
                self._fit_check_column()
                self._refit_rows()
            finally:
                table.setSortingEnabled(sorting)
                table.setUpdatesEnabled(True)
        self._show_empty(len(rows))

    def _show_empty(self, visible_count: int) -> None:
        from panelcad.guidance import problems_empty_copy

        if self._rows and not visible_count:
            self.empty.set_state("", ())
            return
        message = problems_empty_copy(visible_count if not self._rows else len(self._rows))
        if message:
            self.empty.set_state(message, (("Print package", "print"),))
        else:
            self.empty.set_state("", ())

    def _empty_clicked(self, ident: str) -> None:
        if ident == "print":
            self.print_requested.emit()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self._refit_rows()

    def _refit_rows(self) -> None:
        """Grow a row when the message is wider than its column so the line wraps."""

        if self._fitting:
            return
        self._fitting = True
        try:
            self.table.resizeRowsToContents()
        finally:
            self._fitting = False

    def _fit_check_column(self) -> None:
        """Size the check id to its text. The message column keeps the leftover width."""

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.resizeColumnToContents(1)
        width = self.table.columnWidth(1)
        fm = self.table.fontMetrics()
        needed = fm.horizontalAdvance("Check") + 28
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            if item is not None:
                needed = max(needed, fm.horizontalAdvance(item.text()) + 28)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(1, max(width, needed))

    def current_row(self) -> dict[str, str] | None:
        items = self.table.selectedItems()
        if not items:
            return None
        data = items[0].data(Qt.ItemDataRole.UserRole)
        return data if isinstance(data, dict) else None

    def _jump_current(self) -> None:
        row = self.current_row()
        if row:
            self.jump_requested.emit(row)

    def _fix_current(self) -> None:
        row = self.current_row()
        if row:
            self.fix_requested.emit(row)

    def _ignore_current(self) -> None:
        row = self.current_row()
        if row and row.get("id"):
            self.suppress_requested.emit(str(row["id"]))

    def _restore_current(self) -> None:
        row = self.current_row()
        if row and row.get("id"):
            self.unsuppress_requested.emit(str(row["id"]))

    def _menu(self, pos) -> None:
        row = self.current_row()
        menu = QMenu(self)
        menu.addAction("Jump to", self._jump_current)
        if row and row.get("check") in {"layout_not_ready", "placeholder_footprint", "catalog_missing_dimensions"}:
            menu.addAction("Fix…", self._fix_current)
        if row and row.get("suppressed") == "yes":
            menu.addAction("Restore", self._restore_current)
        else:
            menu.addAction("Ignore", self._ignore_current)
        menu.exec(self.table.viewport().mapToGlobal(pos))


def jump_payload(row: dict[str, str]) -> dict[str, str]:
    """Normalize a table/marker click so the window can switch sheet and select."""

    return {
        "id": str(row.get("id") or ""),
        "check": str(row.get("check") or ""),
        "sheet": str(row.get("sheet") or ""),
        "ref": str(row.get("ref") or ""),
        "target": str(row.get("target") or ""),
        "x": str(row.get("x") or ""),
        "y": str(row.get("y") or ""),
        "message": str(row.get("message") or ""),
        "severity": str(row.get("severity") or ""),
    }
