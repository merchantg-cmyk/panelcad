"""Pick or name a terminal strip and open its drawing."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QVBoxLayout, QWidget


class TerminalStripDialog(QDialog):
    def __init__(self, names: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Terminal strip")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        title = QLabel("Open a terminal strip")
        font = title.font()
        font.setPointSize(11)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        note = QLabel(
            "The strip is drawn on a DIN rail: blocks in level order, jumpers, "
            "end plates, and accessories. Internal wires sit above the block and "
            "field wires sit below. Adding, reordering, or retagging updates the "
            "schematic, the project file, the strip report, and the BOM."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.name = QComboBox()
        self.name.setEditable(True)
        for strip in names:
            self.name.addItem(strip)
        if names:
            self.name.setCurrentIndex(0)
        else:
            self.name.setCurrentText("X1")
        self.name.setToolTip("Existing strip, or type a new name such as X1.")
        form.addRow("Strip", self.name)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Open")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def strip_name(self) -> str:
        return str(self.name.currentText() or "").strip()
