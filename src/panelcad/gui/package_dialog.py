"""File → Print Package… — choose sections, paper, and the output PDF."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from panelcad.render.print_package import DEFAULT_SECTIONS, SECTIONS

_SECTION_LABELS = {
    "cover": "Cover sheet",
    "index": "Drawing index",
    "revisions": "Revision history (when the title block has revisions)",
    "nameplate": "Nameplate at real size, with the full-load working",
    "drawings": "Drawings (schematic, panel, door, terminal, I/O, network)",
    "cutouts": "Cutout and drill drawings (optional; one sheet per surface)",
    "bom": "Bill of materials (grouped)",
    "wires": "Wire list (from / to)",
    "from_to": "Wire list (from-to): one row per conductor",
    "xref": "Device index / cross-reference (page-zone, terminals, wires)",
    "plc_io": "PLC I/O list (address, type, tag, device, spares)",
    "terminals": "Terminal strip lists",
    "interconnect": "Inter-panel cable schedule (when the project has one)",
    "checks": "Design checks (open findings; suppressed noted)",
    "power": "Control power budget (one page per supply)",
    "thermal": "Enclosure thermal (watt loss, rise, cooling)",
}

_PAPER_ITEMS = (
    ("mixed", "Mixed — US Letter reports, each drawing at its sheet size"),
    ("letter", "US Letter (all pages)"),
    ("tabloid", "Tabloid / ANSI B (all pages)"),
    ("a4", "A4 (all pages)"),
    ("a3", "A3 (all pages)"),
)


class PrintPackageDialog(QDialog):
    """Options for the one-click print package PDF."""

    def __init__(self, parent: QWidget | None = None, suggested: str | Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Print Package")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        title = QLabel("Build one PDF for the customer and the shop floor")
        font = title.font()
        font.setPointSize(11)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        note = QLabel(
            "Cover, index, revision history, every drawing at its real sheet size "
            "(with the title block), then BOM, wire lists, PLC I/O list, terminal strips, "
            "inter-panel cables, and open design checks. Two or more panels group the "
            "drawings and split the bill by panel. Vector PDF — text stays selectable."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self.checks: dict[str, QCheckBox] = {}
        for key in SECTIONS:
            box = QCheckBox(_SECTION_LABELS[key])
            box.setChecked(key in DEFAULT_SECTIONS)
            self.checks[key] = box
            layout.addWidget(box)

        form = QFormLayout()
        self.paper = QComboBox()
        for value, label in _PAPER_ITEMS:
            self.paper.addItem(label, value)
        form.addRow("Paper", self.paper)

        path_row = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setText(str(suggested or "print_package.pdf"))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path, 1)
        path_row.addWidget(browse)
        form.addRow("PDF", path_row)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Print package PDF", self.path.text(), "PDF (*.pdf)")
        if path:
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
            self.path.setText(path)

    def values(self) -> dict[str, object]:
        sections = [key for key, box in self.checks.items() if box.isChecked()]
        paper = str(self.paper.currentData() or "mixed")
        path = self.path.text().strip() or "print_package.pdf"
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        return {"path": path, "paper": paper, "sections": sections}
