"""Short-circuit current rating dialog for the open project."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.electrical.sccr import format_ka

_COLUMNS = (
    "Ref",
    "Device",
    "Circuit",
    "Base kA",
    "Effective kA",
    "Limits",
    "SCCR kA",
    "Fuse class",
    "Fuse A",
    "IR kA",
)
_EDITABLE = {6, 7, 8, 9}


class SccrDialog(QDialog):
    """Show the SB4 result, edit a part rating, and export the report page."""

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("Short-circuit current rating (UL 508A SB)")
        self.resize(1100, 720)
        self.setMinimumSize(640, 400)
        self.resize(1000, 640)
        layout = QVBoxLayout(self)
        self.summary = QLabel("Panel SCCR")
        font = self.summary.font()
        font.setPointSize(14)
        font.setBold(True)
        self.summary.setFont(font)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        form = QFormLayout()
        self.voltage = QDoubleSpinBox()
        self.voltage.setRange(0, 1000)
        self.voltage.setDecimals(0)
        self.voltage.setSuffix(" V")
        self.voltage.setSpecialValueText("from drawing")
        self.available = QDoubleSpinBox()
        self.available.setRange(0, 300)
        self.available.setDecimals(1)
        self.available.setSuffix(" kA")
        self.available.setSpecialValueText("not entered")
        form.addRow("Panel voltage", self.voltage)
        form.addRow("Available fault current", self.available)
        layout.addLayout(form)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(list(_COLUMNS))
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, stretch=1)

        self.notes = QTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMaximumHeight(140)
        layout.addWidget(self.notes)

        buttons = QHBoxLayout()
        recalc = QPushButton("Recalculate")
        recalc.clicked.connect(self.recalculate)
        export = QPushButton("Export PDF")
        export.clicked.connect(self.export_pdf)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(recalc)
        buttons.addWidget(export)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._load()

    def _load(self) -> None:
        result = self.session.compute_sccr()
        volts = result.get("voltage")
        self.voltage.blockSignals(True)
        self.available.blockSignals(True)
        props = self.session.project.properties
        if props.get("sccr_voltage") not in (None, ""):
            self.voltage.setValue(float(props["sccr_voltage"]))
        elif volts:
            self.voltage.setValue(float(volts))
        else:
            self.voltage.setValue(0)
        available = result.get("available_fault_ka")
        self.available.setValue(0 if available is None else float(available))
        self.voltage.blockSignals(False)
        self.available.blockSignals(False)
        self._show(result)

    def _show(self, result: dict) -> None:
        text = str(result.get("panel_text") or "Panel SCCR")
        limiting = ", ".join(result.get("limiting_refs") or []) or "none"
        extra = ""
        if result.get("below_available"):
            extra = "  —  below the available fault current"
        self.summary.setText(f"Panel SCCR  {text}   Limited by {limiting}{extra}")
        rows = result.get("components") or []
        self.table.setRowCount(len(rows))
        for row_index, part in enumerate(rows):
            values = [
                part.get("ref") or "",
                part.get("name") or "",
                part.get("circuit") or "",
                format_ka(part.get("base_ka")),
                "exempt" if part.get("exempt") else format_ka(part.get("effective_ka")),
                "YES" if part.get("limits") else "",
                "" if part.get("base_ka") is None else format_ka(part.get("base_ka")),
                part.get("fuse_class") or "",
                "" if part.get("fuse_amps") is None else format_ka(part.get("fuse_amps")),
                "" if part.get("ir_ka") is None else format_ka(part.get("ir_ka")),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column not in _EDITABLE:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if part.get("limits"):
                    from panelcad.gui.theme import ui_qcolor

                    item.setBackground(ui_qcolor("row_alert"))
                self.table.setItem(row_index, column, item)
        self.table.resizeColumnsToContents()
        notes = list(result.get("flags") or [])
        if result.get("incomplete"):
            known = result.get("known_minimum_ka")
            if known is not None:
                notes.insert(0, f"Incomplete. Lowest known value is {format_ka(known)} kA.")
        self.notes.setPlainText("\n".join(notes) if notes else "No extra flags.")

    def recalculate(self) -> None:
        """Write voltage, available fault, and edited part ratings, then rate again."""

        voltage = self.voltage.value()
        available = self.available.value()
        sccr_voltage = None if voltage <= 0 else float(voltage)
        self.session.begin_edit("sccr")
        try:
            if available > 0 or sccr_voltage is not None:
                self.session.set_available_fault(
                    None if available <= 0 else float(available),
                    sccr_voltage=sccr_voltage,
                )
            for row in range(self.table.rowCount()):
                ref_item = self.table.item(row, 0)
                if ref_item is None:
                    continue
                ref = ref_item.text().strip()
                if not ref:
                    continue
                sccr_text = _cell(self.table, row, 6)
                fuse_class = _cell(self.table, row, 7)
                fuse_amps = _optional_float(_cell(self.table, row, 8))
                ir_ka = _optional_float(_cell(self.table, row, 9))
                fields: dict = {}
                if sccr_text not in {"", "exempt"}:
                    fields["sccr_ka"] = float(sccr_text)
                if fuse_class:
                    fields["fuse_class"] = fuse_class
                if fuse_amps is not None:
                    fields["fuse_amps"] = fuse_amps
                if ir_ka is not None:
                    fields["ir_ka"] = ir_ka
                if fields:
                    self.session.set_part_sccr(ref, **fields)
        finally:
            self.session.end_edit()
        self._show(self.session.compute_sccr())

    def export_pdf(self) -> None:
        path, _selected = QFileDialog.getSaveFileName(self, "SCCR report", "sccr.pdf", "PDF (*.pdf)")
        if not path:
            return
        self.session.export_sccr_pdf(path)


def _cell(table: QTableWidget, row: int, column: int) -> str:
    item = table.item(row, column)
    return "" if item is None else item.text().strip()


def _optional_float(text: str) -> float | None:
    if not text or text == "—":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def grab_dialog(session: Session, path: Path | str) -> Path:
    """Show the dialog offscreen and save a screenshot. Used by the report artifacts."""

    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        QApplication([])
    dialog = SccrDialog(session)
    dialog.show()
    QApplication.processEvents()
    image = dialog.grab()
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(dest))
    dialog.close()
    return dest
