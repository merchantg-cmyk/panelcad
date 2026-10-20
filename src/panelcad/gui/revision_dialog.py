"""Issue, history, and compare dialogs for customer drawing revisions."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from panelcad.api.session import Session
from panelcad.core.revisions import list_revisions, preview_next_rev


def _table(columns: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    header = table.horizontalHeader()
    header.setStretchLastSection(True)
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    return table


class IssueRevisionDialog(QDialog):
    """Snapshot the project, bump the rev, and fill the title-block revision table."""

    def __init__(self, session: Session, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self.result_info: dict | None = None
        self.setWindowTitle("Issue revision")
        self.resize(460, 420)

        info = list_revisions(session.project)
        current = info["current"] or "none"
        self.banner = QLabel(f"Current revision  {current}")
        self.next_label = QLabel("")
        self.scheme = QComboBox()
        self.scheme.addItem("Letters (A, B, C…)", "letter")
        self.scheme.addItem("Numbers (0, 1, 2…)", "number")
        if info["scheme"] == "number":
            self.scheme.setCurrentIndex(1)
        self.rev = QLineEdit()
        self.date = QLineEdit(self._today())
        self.description = QLineEdit()
        self.description.setPlaceholderText("What changed for the customer")
        self.drawn = QLineEdit()
        self.checked = QLineEdit()
        self.approved = QLineEdit()
        for box in (self.drawn, self.checked, self.approved):
            box.setMaxLength(8)
            box.setPlaceholderText("initials")
        self.auto_clouds = QCheckBox("Draw clouds around changes")
        self.auto_clouds.setChecked(True)
        self.clear_clouds = QCheckBox("Clear clouds from earlier revisions")
        self.clear_clouds.setChecked(bool(info["clear_clouds_on_issue"]))
        note = QLabel("The snapshot is stored in the project file. No git repository is required.")
        note.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Scheme", self.scheme)
        form.addRow("Rev", self.rev)
        form.addRow("Date", self.date)
        form.addRow("Description", self.description)
        form.addRow("Drawn", self.drawn)
        form.addRow("Checked", self.checked)
        form.addRow("Approved", self.approved)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.scheme.currentIndexChanged.connect(self._refresh_preview)
        self._refresh_preview()

        layout = QVBoxLayout(self)
        layout.addWidget(self.banner)
        layout.addWidget(self.next_label)
        layout.addLayout(form)
        layout.addWidget(self.auto_clouds)
        layout.addWidget(self.clear_clouds)
        layout.addWidget(note)
        layout.addWidget(buttons)

    @staticmethod
    def _today() -> str:
        from datetime import date

        return date.today().isoformat()

    def _refresh_preview(self) -> None:
        scheme = str(self.scheme.currentData() or "letter")
        try:
            preview = preview_next_rev(self.session.project, scheme)
        except ValueError:
            preview = ""
        self.rev.setPlaceholderText(preview)
        self.next_label.setText(f"Next revision  {preview}" if preview else "Next revision")

    def values(self) -> dict:
        typed = self.rev.text().strip()
        return {
            "description": self.description.text().strip(),
            "rev": typed or None,
            "scheme": str(self.scheme.currentData() or "letter"),
            "date": self.date.text().strip() or None,
            "drawn": self.drawn.text().strip(),
            "checked": self.checked.text().strip(),
            "approved": self.approved.text().strip(),
            "clear_clouds": self.clear_clouds.isChecked(),
            "auto_clouds": self.auto_clouds.isChecked(),
        }

    def accept(self) -> None:  # noqa: A003
        try:
            self.result_info = self.session.issue_revision(**self.values())
        except Exception as exc:  # noqa: BLE001 — show the reason and keep the dialog open
            QMessageBox.warning(self, "Issue revision", str(exc))
            return
        super().accept()


class RevisionHistoryDialog(QDialog):
    """Issued revisions, oldest first in the model and newest first in the list."""

    def __init__(self, session: Session, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("Revision history")
        self.resize(760, 360)
        info = list_revisions(session.project)
        self.table = _table(["Rev", "Date", "Description", "Drawn", "Checked", "Approved"])
        rows = list(reversed(info["revisions"]))
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            values = [row["rev"], row["date"], row["description"], row["drawn"], row["checked"], row["approved"]]
            for c, value in enumerate(values):
                self.table.setItem(r, c, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()
        current = QLabel(f"Current revision on every sheet  {info['current'] or '—'}")
        if not rows:
            current.setText("No revisions issued yet. The title block updates when you issue one.")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.addWidget(current)
        layout.addWidget(self.table, 1)
        layout.addWidget(buttons)


class CompareRevisionsDialog(QDialog):
    """Structured diff of two revisions, with CSV and PDF export."""

    def __init__(self, session: Session, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self.report: dict | None = None
        self.setWindowTitle("Compare revisions")
        self.resize(920, 520)
        info = list_revisions(session.project)
        choices = [row["rev"] for row in info["revisions"]] + ["working"]
        self.left = QComboBox()
        self.right = QComboBox()
        self.left.addItems(choices)
        self.right.addItems(choices)
        issued = info["revisions"]
        if len(issued) >= 2:
            self.left.setCurrentText(issued[-2]["rev"])
            self.right.setCurrentText(issued[-1]["rev"])
        elif issued:
            self.left.setCurrentText(issued[-1]["rev"])
            self.right.setCurrentText("working")
        self.summary = QLabel("Choose two revisions and compare.")
        self.summary.setWordWrap(True)
        self.table = _table(["Change", "Sheet", "Item", "Before", "After"])
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        buttons_row = QHBoxLayout()
        self.compare_button = QPushButton("Compare")
        self.csv_button = QPushButton("Export CSV…")
        self.pdf_button = QPushButton("Export PDF…")
        self.compare_button.clicked.connect(self.run_compare)
        self.csv_button.clicked.connect(self.export_csv)
        self.pdf_button.clicked.connect(self.export_pdf)
        buttons_row.addWidget(QLabel("From"))
        buttons_row.addWidget(self.left)
        buttons_row.addWidget(QLabel("to"))
        buttons_row.addWidget(self.right)
        buttons_row.addWidget(self.compare_button)
        buttons_row.addStretch(1)
        buttons_row.addWidget(self.csv_button)
        buttons_row.addWidget(self.pdf_button)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        close.accepted.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.addLayout(buttons_row)
        layout.addWidget(self.summary)
        layout.addWidget(self.table, 1)
        layout.addWidget(close)
        if len(choices) >= 2:
            self.run_compare()

    def run_compare(self) -> None:
        try:
            self.report = self.session.compare_revisions(self.left.currentText(), self.right.currentText())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Compare revisions", str(exc))
            return
        from panelcad.render.revision_report import display_cells

        rows = list(self.report.get("rows") or [])
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(display_cells(row)):
                self.table.setItem(r, c, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()
        width = 36 + sum(self.table.columnWidth(c) for c in range(self.table.columnCount()))
        self.resize(min(max(width + 48, 820), 1280), 700)
        counts = self.report.get("counts") or {}
        if rows:
            self.summary.setText(
                f"{self.report['left']}  →  {self.report['right']}    "
                f"devices {counts.get('devices_added', 0)} added, "
                f"{counts.get('devices_removed', 0)} removed, "
                f"{counts.get('devices_changed', 0)} changed    "
                f"wires {counts.get('wires_added', 0)} added, "
                f"{counts.get('wires_removed', 0)} removed, "
                f"{counts.get('wires_renumbered', 0)} renumbered    "
                f"terminals {counts.get('terminals_changed', 0)} changed    "
                f"BOM {counts.get('bom', 0)}    "
                f"layout {counts.get('layout', 0)}"
            )
        else:
            self.summary.setText(f"{self.report['left']}  →  {self.report['right']}    No differences")

    def export_csv(self) -> None:
        if self.report is None:
            self.run_compare()
        if self.report is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Revision compare CSV", "revision-compare.csv", "CSV (*.csv)")
        if not path:
            return
        from panelcad.render.revision_report import write_compare_csv

        dest = write_compare_csv(path, self.report)
        self.summary.setText(f"CSV  {dest}")

    def export_pdf(self) -> None:
        if self.report is None:
            self.run_compare()
        if self.report is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Revision compare PDF", "revision-compare.pdf", "PDF (*.pdf)")
        if not path:
            return
        from panelcad.render.revision_report import write_compare_pdf

        dest = write_compare_pdf(path, self.report, self.session.project)
        self.summary.setText(f"PDF  {dest}")


def suggested_compare_path(session: Session, suffix: str) -> str:
    stem = "revision-compare"
    if session.path is not None:
        stem = f"{Path(session.path).stem}-compare"
    return f"{stem}.{suffix.lstrip('.')}"
