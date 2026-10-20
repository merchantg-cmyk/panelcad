"""File → Export → PLC Tags… — pick a target and how tag names are built."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QSizePolicy,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

_TARGETS = (
    ("studio5000", "Rockwell Studio 5000 (CSV tag import)"),
    ("tia", "Siemens TIA Portal (Excel tag table)"),
    ("tia_csv", "Siemens TIA Portal (generic CSV)"),
    ("domore", "AutomationDirect Do-more (element documentation)"),
    ("keyence", "Keyence KV STUDIO (generic CSV)"),
    ("sysmac", "Omron Sysmac Studio (generic CSV)"),
    ("cxprogrammer", "Omron CX-Programmer (symbol table)"),
)

_DEFAULT_PATHS = {
    "studio5000": "studio5000-tags.csv",
    "tia": "tia-tags.xlsx",
    "tia_csv": "tia-tags.csv",
    "domore": "domore-tags.csv",
    "keyence": "keyence-tags.csv",
    "sysmac": "sysmac-tags.csv",
    "cxprogrammer": "cxprogrammer-symbols.txt",
}

_NAMES = (
    ("device", "Connected device tag"),
    ("description", "I/O description"),
)

_STYLES = (
    ("alias", "Alias to Local:x:I.Pt00.Data"),
    ("base", "Base tag (BOOL / REAL)"),
)


class _Note(QLabel):
    """Word-wrapped note that stays as tall as its text."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 — Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 — Qt override
        flags = int(Qt.TextFlag.TextWordWrap)
        rect = QFontMetrics(self.font()).boundingRect(0, 0, max(width, 1), 4000, flags, self.text())
        return rect.height() + 10

    def sizeHint(self):  # noqa: N802 — Qt override
        from PySide6.QtCore import QSize

        width = self.width() if self.width() > 80 else 520
        return QSize(520, self.heightForWidth(width))


class PlcTagExportDialog(QDialog):
    """Target, prefix, and device-tag vs description naming."""

    def __init__(
        self,
        parent: QWidget | None = None,
        suggested: str | Path | None = None,
        analog_start: int | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export PLC Tags")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        title = QLabel("Export PLC tags from the drawing")
        font = title.font()
        font.setPointSize(11)
        font.setBold(True)
        title.setFont(font)
        title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(title)

        self.note = _Note()
        layout.addWidget(self.note)
        layout.setSpacing(8)

        form = QFormLayout()
        self.target = QComboBox()
        for value, label in _TARGETS:
            self.target.addItem(label, value)
        form.addRow("Target", self.target)

        self.prefix = QLineEdit()
        self.prefix.setPlaceholderText("Optional, for example M1_")
        form.addRow("Prefix", self.prefix)

        self.name_from = QComboBox()
        for value, label in _NAMES:
            self.name_from.addItem(label, value)
        form.addRow("Tag name", self.name_from)

        self.logix_style = QComboBox()
        for value, label in _STYLES:
            self.logix_style.addItem(label, value)
        form.addRow("Logix style", self.logix_style)

        self.logix_slot = QSpinBox()
        self.logix_slot.setRange(0, 99)
        self.logix_slot.setValue(1)
        form.addRow("First slot", self.logix_slot)

        self.analog_start = QSpinBox()
        self.analog_start.setRange(-1, 65534)
        self.analog_start.setSpecialValueText("Automatic")
        self.analog_start.setToolTip(
            "TIA %IW/%QW byte address. Automatic uses the module start address, "
            "or the next even word after the highest digital byte."
        )
        if analog_start is None:
            self.analog_start.setValue(-1)
        else:
            start = int(analog_start)
            if start % 2:
                start += 1
            self.analog_start.setValue(max(0, start))
        form.addRow("Analog start", self.analog_start)

        path_row = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setText(str(suggested or "studio5000-tags.csv"))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path, 1)
        path_row.addWidget(browse)
        form.addRow("File", path_row)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.target.currentIndexChanged.connect(self._sync_target)
        self._sync_target()

    def _sync_target(self) -> None:
        target = str(self.target.currentData() or "studio5000")
        logix = target == "studio5000"
        self.logix_style.setEnabled(logix)
        self.logix_slot.setEnabled(logix)
        if target == "tia":
            self.note.setText(
                "TIA Portal Excel tag table. Sheets PLCTags and TagTableProperties. "
                "English columns: Name, Path, Data Type, Logical Address, Comment, "
                "Hmi Visible, Hmi Accessible, Hmi Writeable, Typeobject ID, Version ID. "
                "Document property TIA_Version is 1.0. Addresses are %I0.0 and %Q0.0. "
                "Analog words (%IW, %QW) start on their own even byte so they do not "
                "cover the digital inputs or outputs."
            )
        elif target == "tia_csv":
            self.note.setText(
                "Labeled generic CSV. The TIA import file is the Excel tag table above. "
                "This text file is not a TIA import. Addresses are %I0.0 and %Q0.0."
            )
        elif target == "domore":
            self.note.setText(
                "Do-more Designer element documentation: element, nickname, "
                "extra info, and description. Discrete points map to X0 and Y0, "
                "analog points to WX0 and WY0."
            )
        elif target == "keyence":
            self.note.setText(
                "Labeled generic CSV. KV STUDIO's device-comment section names "
                "CSV (MBCS) and TXT (Unicode) and does not list the columns. "
                "This file is not a KV STUDIO import. Devices follow the Unit "
                "Editor default: relays from R30000, data memory from DM10000."
            )
        elif target == "sysmac":
            self.note.setText(
                "Labeled generic CSV. Sysmac Studio does not publish a "
                "global-variable import file with AT columns. This file is not "
                "a Sysmac Studio import. Names keep separate underscores "
                "(M1_RUN_FB). cio_address uses CIO 0.00, 100.00, and one "
                "CJ1W-MAD42 unit for a combo analog module."
            )
        elif target == "cxprogrammer":
            self.note.setText(
                "CX-Programmer symbol-table paste. No header. Tab-separated "
                "name, data type, address, and comment. Inputs start at 0.00 "
                "and outputs at 100.00. A combo analog module is one "
                "CJ1W-MAD42 unit: output 1 is n+1, input 1 is n+5."
            )
        else:
            self.note.setText(
                "Studio 5000 CSV tag import. Alias rows point at Local:x:I.Pt00.Data "
                "(outputs Local:x:O.Pt00.Data, analog Ch00.Data). Base tags are BOOL or REAL with the "
                "name from the prefix and the device tag or description."
            )
        current = self.path.text().strip()
        if not current or current in set(_DEFAULT_PATHS.values()):
            self.path.setText(_DEFAULT_PATHS[target])
        self.note.updateGeometry()

    def _browse(self) -> None:
        target = str(self.target.currentData() or "")
        if target == "tia":
            filt, suffix = "Excel (*.xlsx)", ".xlsx"
        elif target == "cxprogrammer":
            filt, suffix = "Text (*.txt)", ".txt"
        else:
            filt, suffix = "CSV (*.csv)", ".csv"
        path, _ = QFileDialog.getSaveFileName(self, "PLC tags", self.path.text(), filt)
        if path:
            if target == "cxprogrammer" and path.lower().endswith(".tsv"):
                suffix = ""
            if suffix and not path.lower().endswith(suffix):
                path += suffix
            self.path.setText(path)

    def values(self) -> dict[str, object]:
        target = str(self.target.currentData() or "studio5000")
        path = self.path.text().strip() or _DEFAULT_PATHS.get(target, "plc-tags.csv")
        if target == "tia":
            suffix = ".xlsx"
        elif target == "cxprogrammer":
            suffix = "" if path.lower().endswith((".txt", ".tsv")) else ".txt"
        else:
            suffix = ".csv"
        if suffix and not path.lower().endswith(suffix):
            path += suffix
        raw_analog = int(self.analog_start.value())
        if raw_analog < 0:
            analog: int | None = None
        else:
            analog = raw_analog if raw_analog % 2 == 0 else raw_analog + 1
        return {
            "path": path,
            "target": str(self.target.currentData() or "studio5000"),
            "prefix": self.prefix.text(),
            "name_from": str(self.name_from.currentData() or "device"),
            "logix_style": str(self.logix_style.currentData() or "alias"),
            "logix_slot": int(self.logix_slot.value()),
            "analog_start": analog,
        }
