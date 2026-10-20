"""Reports → Wire List (From-To)… and File → Export → Wire Markers…."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from panelcad.io.wire_markers import (
    BRADY_NOTE,
    PHOENIX_DOC_URLS,
    PHOENIX_NOTE,
    WEIDMUELLER_NOTE,
)

_TARGETS = (
    ("generic", "Generic CSV — one row per wire end"),
    ("phoenix", "Phoenix Contact — single column, no header"),
    ("weidmueller", "Weidmüller M-Print PRO — generic CSV (no published header)"),
    ("brady", "Brady Workstation — generic CSV (no published header)"),
)

_STYLES = (
    ("heat-shrink", "Heat-shrink"),
    ("clip-on", "Clip-on"),
)

_SORTS = (
    ("number", "Wire number"),
    ("device", "Device (from tag, then pin)"),
)

_SUMMARY = (
    "Phoenix Contact: one column, no header (documented)\n"
    "Weidmüller, Brady: generic CSV, header unverified\n"
    "Generic CSV: one row per wire end"
)


def markers_doc_path() -> Path | None:
    """Repo copy of docs/WIRE_MARKERS.md, when this file sits in the source tree."""

    path = Path(__file__).resolve().parents[3] / "docs" / "WIRE_MARKERS.md"
    return path if path.is_file() else None


def _path_row(parent: QWidget, edit: QLineEdit, title: str, file_filter: str) -> QHBoxLayout:
    row = QHBoxLayout()
    browse = QPushButton("Browse…")

    def pick() -> None:
        path, _ = QFileDialog.getSaveFileName(parent, title, edit.text(), file_filter)
        if path:
            if not path.lower().endswith(".csv"):
                path += ".csv"
            edit.setText(path)

    browse.clicked.connect(pick)
    row.addWidget(edit, 1)
    row.addWidget(browse)
    return row


class FromToDialog(QDialog):
    """Sort and path for the from-to CSV."""

    def __init__(self, parent: QWidget | None = None, suggested: str | Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Wire List (From-To)")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        note = QLabel(
            "One row per conductor from the netlist. A contact's two sides are "
            "separate rows with separate wire numbers. From and to are tag:pin "
            "(M1:A1), or strip:terminal where the wire lands."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.sort = QComboBox()
        for value, label in _SORTS:
            self.sort.addItem(label, value)
        form.addRow("Sort", self.sort)
        self.path = QLineEdit(str(suggested or "from_to.csv"))
        form.addRow("CSV", _path_row(self, self.path, "From-to wire list", "CSV (*.csv)"))
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict[str, str]:
        path = self.path.text().strip() or "from_to.csv"
        if not path.lower().endswith(".csv"):
            path += ".csv"
        return {"path": path, "sort": str(self.sort.currentData() or "number")}


class WireMarkerDialog(QDialog):
    """Target, style, and path for a wire-marker file."""

    def __init__(self, parent: QWidget | None = None, suggested: str | Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export Wire Markers")
        self.setMinimumWidth(640)
        layout = QVBoxLayout(self)
        title = QLabel("One marker per wire end")
        font = title.font()
        font.setPointSize(11)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        self.summary = QLabel(_SUMMARY)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.details = QLabel('<a href="wire-markers">Details…</a>')
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.details.setToolTip("Open docs/WIRE_MARKERS.md for the vendor citations.")
        self.details.linkActivated.connect(self._open_details)
        layout.addWidget(self.details)
        form = QFormLayout()
        self.target = QComboBox()
        for value, label in _TARGETS:
            self.target.addItem(label, value)
        form.addRow("Target", self.target)
        self.style = QComboBox()
        for value, label in _STYLES:
            self.style.addItem(label, value)
        form.addRow("Marker", self.style)
        self.sort = QComboBox()
        for value, label in _SORTS:
            self.sort.addItem(label, value)
        form.addRow("Sort", self.sort)
        self.path = QLineEdit(str(suggested or "wire_markers.csv"))
        form.addRow("File", _path_row(self, self.path, "Wire markers", "CSV (*.csv)"))
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _open_details(self, _link: str = "") -> None:
        path = markers_doc_path()
        if path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            return
        QMessageBox.information(
            self,
            "Wire marker formats",
            "\n\n".join((PHOENIX_NOTE, WEIDMUELLER_NOTE, BRADY_NOTE, PHOENIX_DOC_URLS[0])),
        )

    def values(self) -> dict[str, str]:
        path = self.path.text().strip() or "wire_markers.csv"
        if not path.lower().endswith(".csv"):
            path += ".csv"
        return {
            "path": path,
            "target": str(self.target.currentData() or "generic"),
            "style": str(self.style.currentData() or "heat-shrink"),
            "sort": str(self.sort.currentData() or "number"),
        }


class WireSpecDialog(QDialog):
    """Size, color, and type on one wire. Blank leaves that field unchanged when clearing is off."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        caption: str = "",
        size: str = "",
        color: str = "",
        wire_type: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Wire size, color, type")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        if caption:
            label = QLabel(caption)
            label.setWordWrap(True)
            layout.addWidget(label)
        form = QFormLayout()
        self.size = QLineEdit(size)
        self.color = QLineEdit(color)
        self.wire_type = QLineEdit(wire_type)
        form.addRow("Size", self.size)
        form.addRow("Color", self.color)
        form.addRow("Type", self.wire_type)
        layout.addLayout(form)
        hint = QLabel("Empty clears that field on this wire. A blank field inherits the net default.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict[str, str]:
        return {
            "size": self.size.text().strip(),
            "color": self.color.text().strip(),
            "type": self.wire_type.text().strip(),
        }


class NetWireDialog(QDialog):
    """Default size, color, and type for one net. ``*`` applies when the net has no entry."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        nets: list[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Net wire defaults")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        note = QLabel(
            "Wires with a blank size, color, or type inherit this. "
            "* is the fallback for every net that has no entry of its own. "
            "all is the same key."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.net = QComboBox()
        self.net.setEditable(True)
        self.net.addItem("*")
        for name in nets or []:
            if name and name != "*":
                self.net.addItem(name)
        form.addRow("Net", self.net)
        self.size = QLineEdit()
        self.color = QLineEdit()
        self.wire_type = QLineEdit()
        form.addRow("Size", self.size)
        form.addRow("Color", self.color)
        form.addRow("Type", self.wire_type)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict[str, str]:
        return {
            "net": self.net.currentText().strip() or "*",
            "size": self.size.text().strip(),
            "color": self.color.text().strip(),
            "type": self.wire_type.text().strip(),
        }
