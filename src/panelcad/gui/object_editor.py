"""Human object editor: identity, comms ports, instance properties."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
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
    QWidget,
)

from panelcad.api.session import Session

PORT_KINDS = ["ethernet", "serial", "usb", "can", "fieldbus", "other"]
SIDES = ["left", "right", "top", "bottom"]
COLS = ["id", "kind", "medium", "protocol", "connector", "side"]


class ObjectEditor(QDialog):
    """Edit one placed device: ports (COM/ETH) and free-form properties."""

    def __init__(self, session: Session, ref: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.ref = ref
        comp = session.sheet().component(ref)
        device = session.catalog.get(comp.library_id)
        self.setWindowTitle(f"Object editor — {ref}")
        self.resize(640, 480)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"{ref}  ·  {device.name}  ·  {device.library_id}"))

        form = QFormLayout()
        self._panel_combo: QComboBox | None = None
        if session.project.panels:
            from panelcad.core.panels import panel_tag

            self._panel_combo = QComboBox()
            self._panel_combo.addItem("(unassigned)", "")
            current = 0
            for index, panel in enumerate(session.project.panels, start=1):
                label = panel_tag(panel)
                if panel.name:
                    label = f"{label}  {panel.name}"
                self._panel_combo.addItem(label, panel.id)
                if panel.id == comp.panel:
                    current = index
            self._panel_combo.setCurrentIndex(current)
            form.addRow("Panel", self._panel_combo)
        self.notes = QLineEdit(str(comp.properties.get("notes", "") or device.properties.get("notes", "") or ""))
        self.ip = QLineEdit(str(comp.properties.get("ip") or comp.properties.get("address") or ""))
        self.station = QLineEdit(str(comp.properties.get("station") or comp.properties.get("node") or ""))
        form.addRow("IP / address", self.ip)
        form.addRow("Station / node", self.station)
        form.addRow("Notes", self.notes)
        pic_row = QWidget()
        pic_box = QHBoxLayout(pic_row)
        pic_box.setContentsMargins(0, 0, 0, 0)
        self.picture = QLineEdit(str(comp.picture or ""))
        self.picture.setPlaceholderText("panel / door layout only")
        pic_box.addWidget(self.picture, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_picture)
        pic_box.addWidget(browse)
        clear = QPushButton("Clear")
        clear.clicked.connect(lambda: self.picture.setText(""))
        pic_box.addWidget(clear)
        form.addRow("Picture", pic_row)
        layout.addLayout(form)

        layout.addWidget(QLabel("Communications ports — add COM/Ethernet ports for this instance"))
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        add = QPushButton("Add port")
        add.clicked.connect(self._add_row)
        rem = QPushButton("Remove selected")
        rem.clicked.connect(self._remove_row)
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch()
        layout.addLayout(row)

        for port in session.list_ports(ref):
            self._append(port)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _append(self, port: dict) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(str(port.get("id") or "")))
        kind = QComboBox()
        kind.addItems(PORT_KINDS)
        k = str(port.get("kind") or "serial")
        kind.setCurrentText(k if k in PORT_KINDS else "other")
        self.table.setCellWidget(r, 1, kind)
        self.table.setItem(r, 2, QTableWidgetItem(str(port.get("medium") or "")))
        self.table.setItem(r, 3, QTableWidgetItem(str(port.get("protocol") or "")))
        self.table.setItem(r, 4, QTableWidgetItem(str(port.get("connector") or "")))
        side = QComboBox()
        side.addItems(SIDES)
        s = str(port.get("side") or "right")
        side.setCurrentText(s if s in SIDES else "right")
        self.table.setCellWidget(r, 5, side)

    def _browse_picture(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Device picture (panel / door layout)",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp *.svg)",
        )
        if path:
            self.picture.setText(path)

    def _add_row(self) -> None:
        n = self.table.rowCount() + 1
        self._append({"id": f"COM{n}", "kind": "serial", "medium": "RS-485", "side": "left"})

    def _remove_row(self) -> None:
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)

    def _cell(self, r: int, c: int) -> str:
        item = self.table.item(r, c)
        return item.text().strip() if item else ""

    def _save(self) -> None:
        try:
            with self.session.command("object editor"):
                self._write()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Object editor", str(exc))
            return
        self.accept()

    def _write(self) -> None:
        existing = {p["id"] for p in self.session.list_ports(self.ref)}
        keep: set[str] = set()
        for r in range(self.table.rowCount()):
            pid = self._cell(r, 0)
            if not pid:
                continue
            kind_w = self.table.cellWidget(r, 1)
            side_w = self.table.cellWidget(r, 5)
            kind = kind_w.currentText() if isinstance(kind_w, QComboBox) else "serial"
            side = side_w.currentText() if isinstance(side_w, QComboBox) else "right"
            fields = {
                "kind": kind,
                "medium": self._cell(r, 2),
                "protocol": self._cell(r, 3),
                "connector": self._cell(r, 4),
                "side": side,
            }
            if pid in existing:
                self.session.set_port(self.ref, pid, **fields)
            else:
                self.session.add_port(self.ref, pid, **fields)
            keep.add(pid)
        for pid in list(existing):
            if pid not in keep:
                self.session.delete_port(self.ref, pid)
        self.session.set_address(self.ref, ip=self.ip.text(), node=self.station.text())
        if self._panel_combo is not None:
            chosen = str(self._panel_combo.currentData() or "")
            current = str(self.session.sheet().component(self.ref).panel or "")
            if chosen != current:
                if chosen:
                    self.session.assign_device(self.ref, chosen)
                else:
                    for sheet in self.session.project.sheets:
                        for comp in sheet.components:
                            if comp.id == self.ref:
                                comp.panel = ""
        if self.notes.text().strip():
            self.session.set_component_properties(self.ref, {"notes": self.notes.text().strip()})
        pic = self.picture.text().strip()
        if pic:
            self.session.set_picture(self.ref, pic)
        else:
            self.session.clear_picture(self.ref)
