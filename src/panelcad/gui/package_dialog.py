"""File → Print Package… / Export package… — sections, sheets, paper, output.

The last choice is remembered per project (keyed by the project file path, or
the project name for an unsaved project) in QSettings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

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
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from PySide6.QtCore import QSettings, Qt

from panelcad.render.print_package import DEFAULT_SECTIONS, SECTIONS

_SETTINGS_GROUP = "print_package"

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




def selection_key(session: Any) -> str:
    path = getattr(session, "path", None)
    if path is not None:
        return str(Path(path).resolve()).replace("\\", "/").lower()
    return "unsaved:" + str(getattr(session.project, "name", "") or "")


def _settings(settings: QSettings | None) -> QSettings:
    return settings if settings is not None else QSettings("PanelCAD", "PanelCAD")


def load_selection(key: str, settings: QSettings | None = None) -> dict[str, Any] | None:
    """Last saved {sections, sheet_ids, paper} for this project, or None."""

    raw = _settings(settings).value(f"{_SETTINGS_GROUP}/{_safe(key)}")
    if not raw:
        return None
    try:
        data = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_selection(key: str, values: dict[str, Any], settings: QSettings | None = None) -> None:
    payload = {
        "sections": list(values.get("sections") or []),
        "sheet_ids": values.get("sheet_ids"),
        "paper": values.get("paper") or "mixed",
        "known_sheet_ids": list(values.get("known_sheet_ids") or []),
    }
    store = _settings(settings)
    store.setValue(f"{_SETTINGS_GROUP}/{_safe(key)}", json.dumps(payload))
    store.sync()


def _safe(key: str) -> str:
    # QSettings treats "/" as a group separator.
    return "".join(ch if ch.isalnum() else "_" for ch in key)[-200:] + f"_{abs(hash_key(key))}"


def hash_key(key: str) -> int:
    import zlib

    return zlib.crc32(key.encode("utf-8"))


def describe_result(result: dict[str, Any], requested: Sequence[str] | None) -> str:
    """Human summary shown when the export finishes."""

    resolved = list(result.get("sections") or [])
    lines = [
        f"Pages: {result.get('pages', 0)}",
        "Requested: " + (", ".join(requested) if requested else "(defaults)"),
        "Printed: " + (", ".join(resolved) if resolved else "(none)"),
    ]
    skipped = [key for key in (requested or []) if key not in resolved]
    if skipped:
        lines.append("Skipped (nothing to print): " + ", ".join(skipped))
    sheets = result.get("sheets") or []
    if "drawings" in resolved:
        lines.append(f"Drawing sheets: {len(sheets)}")
    return "\n".join(lines)


class PrintPackageDialog(QDialog):
    """Options for the one-click print package PDF or the package folder."""

    def __init__(
        self,
        parent: QWidget | None = None,
        suggested: str | Path | None = None,
        *,
        sheets: Sequence[tuple[str, str]] | None = None,
        initial: dict[str, Any] | None = None,
        folder: bool = False,
    ) -> None:
        super().__init__(parent)
        self._folder = folder
        self.setWindowTitle("Export Package" if folder else "Print Package")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        title = QLabel(
            "Export the package folder (data files, drawings, print.pdf)"
            if folder
            else "Build one PDF for the customer and the shop floor"
        )
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

        initial = initial or {}
        start_sections = initial.get("sections")
        wanted = set(DEFAULT_SECTIONS if start_sections is None else start_sections)
        self.checks: dict[str, QCheckBox] = {}
        self.sheet_list = QListWidget()
        for key in SECTIONS:
            box = QCheckBox(_SECTION_LABELS[key])
            box.setChecked(key in wanted)
            self.checks[key] = box
            layout.addWidget(box)
            if key == "drawings":
                layout.addWidget(self.sheet_list)

        self._all_sheet_ids = [sid for sid, _ in (sheets or [])]
        start_sheets = initial.get("sheet_ids")
        keep = None if start_sheets is None else set(start_sheets)
        # Sheets added since the selection was saved start ticked.
        known_then = set(initial.get("known_sheet_ids") or [])
        for sid, label in sheets or []:
            item = QListWidgetItem(f"{sid} — {label}" if label and label != sid else sid)
            item.setData(Qt.ItemDataRole.UserRole, sid)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = keep is None or sid in keep or (bool(known_then) and sid not in known_then)
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self.sheet_list.addItem(item)
        self.sheet_list.setVisible(bool(sheets))
        self.sheet_list.setMaximumHeight(140)
        self.checks["drawings"].toggled.connect(self.sheet_list.setEnabled)
        self.sheet_list.setEnabled(self.checks["drawings"].isChecked())

        form = QFormLayout()
        self.paper = QComboBox()
        for value, label in _PAPER_ITEMS:
            self.paper.addItem(label, value)
        start_paper = initial.get("paper")
        if start_paper:
            found = self.paper.findData(start_paper)
            if found >= 0:
                self.paper.setCurrentIndex(found)
        form.addRow("Paper", self.paper)

        path_row = QHBoxLayout()
        self.path = QLineEdit()
        default = "package" if folder else "print_package.pdf"
        self.path.setText(str(suggested or default))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path, 1)
        path_row.addWidget(browse)
        form.addRow("Folder" if folder else "PDF", path_row)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        if self._folder:
            path = QFileDialog.getExistingDirectory(self, "Export package folder", self.path.text())
            if path:
                self.path.setText(path)
            return
        path, _ = QFileDialog.getSaveFileName(self, "Print package PDF", self.path.text(), "PDF (*.pdf)")
        if path:
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
            self.path.setText(path)

    def selected_sheet_ids(self) -> list[str] | None:
        """None when every sheet is ticked (so new sheets print by default)."""

        ids = [
            str(self.sheet_list.item(i).data(Qt.ItemDataRole.UserRole))
            for i in range(self.sheet_list.count())
            if self.sheet_list.item(i).checkState() == Qt.CheckState.Checked
        ]
        if len(ids) == len(self._all_sheet_ids):
            return None
        return ids

    def values(self) -> dict[str, object]:
        sections = [key for key, box in self.checks.items() if box.isChecked()]
        sheet_ids = self.selected_sheet_ids()
        if sheet_ids == [] and "drawings" in sections:
            # Drawings ticked but no sheet ticked: nothing to draw.
            sections.remove("drawings")
        paper = str(self.paper.currentData() or "mixed")
        if self._folder:
            path = self.path.text().strip() or "package"
        else:
            path = self.path.text().strip() or "print_package.pdf"
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
        return {
            "path": path,
            "paper": paper,
            "sections": sections,
            "sheet_ids": sheet_ids,
            "known_sheet_ids": list(self._all_sheet_ids),
        }
