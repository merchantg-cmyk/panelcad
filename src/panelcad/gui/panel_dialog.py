"""Add a control panel. The enclosure is a catalog row, not a typed size."""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QVBoxLayout, QWidget

from panelcad.api.session import Session
from panelcad.library.enclosures import load_enclosures


class NewPanelDialog(QDialog):
    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("New panel")
        self.resize(480, 220)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.tag = QLineEdit()
        self.tag.setPlaceholderText("CP1")
        self.name = QLineEdit()
        self.name.setPlaceholderText("Main control panel")
        self.system = QLineEdit()
        self.system.setPlaceholderText("IEC =system, optional")
        self.enclosure = QComboBox()
        project_dir = session.path.parent if session.path else None
        self._boxes = load_enclosures(project_dir).boxes
        self.enclosure.addItem("(choose a catalog enclosure)", "")
        for box in self._boxes:
            self.enclosure.addItem(box.display_name(), box.id)
        form.addRow("Tag", self.tag)
        form.addRow("Name", self.name)
        form.addRow("System", self.system)
        form.addRow("Enclosure", self.enclosure)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        if not self.tag.text().strip():
            self.tag.setFocus()
            return
        if not str(self.enclosure.currentData() or "").strip():
            self.enclosure.setFocus()
            return
        self.accept()

    def values(self) -> dict[str, str]:
        return {
            "tag": self.tag.text().strip(),
            "name": self.name.text().strip(),
            "system": self.system.text().strip(),
            "enclosure_id": str(self.enclosure.currentData() or "").strip(),
        }
