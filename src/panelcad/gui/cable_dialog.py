"""Field cable editor: create, edit, auto-propose, and assign conductors."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.core.cables import field_kind

_CONDUCTOR_COLUMNS = ("Cond", "Color", "Panel terminal", "Field terminal", "Wire", "Role", "Spare")


class CableEditorDialog(QDialog):
    """Edit the cables that leave the panel. Changes are one undo each."""

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self._editing = ""
        self.setWindowTitle("Field cables")
        self.resize(960, 640)
        self.setMinimumSize(640, 400)

        layout = QVBoxLayout(self)
        title = QLabel("Field cables")
        font = title.font()
        font.setPointSize(14)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        note = QLabel(
            "Auto-propose groups the wires from a terminal strip, or from a drive's output terminals, "
            "to one field device (motor, sensor, valve, instrument, or remote station) into one cable. "
            "Conductor count includes spares. Gauge follows the circuit wire size when it is known. "
            "Cable descriptions are standard names. Part numbers stay blank unless you enter one "
            "or it comes from the part library with a source URL."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QHBoxLayout()
        self.spare_count = QSpinBox()
        self.spare_count.setRange(-1, 24)
        self.spare_count.setSpecialValueText("default")
        self.spare_count.setFixedWidth(128)
        self.spare_count.setValue(-1)
        self.spare_count.setToolTip("Extra conductors. Default is a spare pair on instrument cable, one spare on control, and none on a motor.")
        buttons.addWidget(QLabel("Spares"))
        buttons.addWidget(self.spare_count)
        propose = QPushButton("Auto-propose")
        propose.clicked.connect(self.propose)
        buttons.addWidget(propose)
        add = QPushButton("Add cable")
        add.clicked.connect(self.add_cable)
        buttons.addWidget(add)
        remove = QPushButton("Delete")
        remove.clicked.connect(self.delete_selected)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        self.status = QLabel("")
        buttons.addWidget(self.status)
        layout.addLayout(buttons)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Tag", "From", "To", "Cable type", "Gauge", "Length", "Route"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._load_selected)
        self.table.setMinimumHeight(180)

        form_host = QWidget()
        form_row = QHBoxLayout(form_host)
        form_row.setContentsMargins(0, 0, 0, 0)
        left = QFormLayout()
        right = QFormLayout()
        self.tag = QLineEdit()
        self.from_strip = QLineEdit()
        self.from_device = QLineEdit()
        self.from_device.setPlaceholderText("VFD1 when the cable lands on the drive")
        self.terminals = QLineEdit()
        self.terminals.setPlaceholderText("TB1-1, TB1-2 or VFD1.U, VFD1.V, VFD1.W")
        self.device = QComboBox()
        self.device.setEditable(True)
        self.kind = QComboBox()
        self.kind.addItems(["", "motor", "instrument", "sensor", "valve", "pushbutton", "field"])
        self.cable_type = QLineEdit()
        self.conductors = QSpinBox()
        self.conductors.setRange(0, 64)
        self.gauge = QLineEdit()
        self.shield = QCheckBox("Shield")
        self.drain = QCheckBox("Drain")
        self.shield_bond = QComboBox()
        self.shield_bond.addItems(["", "both", "panel", "field", "none"])
        self.length = QDoubleSpinBox()
        self.length.setRange(0, 100000)
        self.length.setDecimals(2)
        self.length.setSpecialValueText("not set")
        self.unit = QComboBox()
        self.unit.addItems(["ft", "m"])
        self.slack = QDoubleSpinBox()
        self.slack.setRange(0, 200)
        self.slack.setDecimals(1)
        self.slack.setSuffix(" %")
        self.gland = QLineEdit()
        self.gland.setPlaceholderText("blank — size per cable OD")
        self.route = QLineEdit()
        self.part_number = QLineEdit()
        self.part_number.setPlaceholderText("blank unless entered or from the library")
        self.part_source = QLineEdit()
        self.part_source.setPlaceholderText("https://… when the part number is from the library")
        self.show_tag = QCheckBox("Show cable tag on the field wires")
        self.show_tag.setChecked(True)
        left.addRow("Tag", self.tag)
        left.addRow("From strip", self.from_strip)
        left.addRow("Panel device", self.from_device)
        left.addRow("Terminals", self.terminals)
        left.addRow("Field device", self.device)
        left.addRow("Kind", self.kind)
        left.addRow("Cable type", self.cable_type)
        left.addRow("Conductors", self.conductors)
        left.addRow("Gauge", self.gauge)
        right.addRow("", self.shield)
        right.addRow("", self.drain)
        right.addRow("Shield bond", self.shield_bond)
        right.addRow("Length", self.length)
        right.addRow("Unit", self.unit)
        right.addRow("Slack", self.slack)
        right.addRow("Gland / cord grip", self.gland)
        right.addRow("Route / tray", self.route)
        right.addRow("Part number", self.part_number)
        right.addRow("Part source", self.part_source)
        right.addRow("", self.show_tag)
        form_row.addLayout(left, 1)
        form_row.addLayout(right, 1)

        self.conductors_table = QTableWidget(0, len(_CONDUCTOR_COLUMNS))
        self.conductors_table.setHorizontalHeaderLabels(list(_CONDUCTOR_COLUMNS))
        self.conductors_table.verticalHeader().setVisible(False)
        self.conductors_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.conductors_table.setMinimumHeight(180)
        conductor_buttons = QHBoxLayout()
        assign = QPushButton("Assign from wires")
        assign.clicked.connect(self.assign_from_wires)
        conductor_buttons.addWidget(assign)
        apply_lands = QPushButton("Apply conductor table")
        apply_lands.clicked.connect(self.apply_conductors)
        conductor_buttons.addWidget(apply_lands)
        conductor_buttons.addStretch(1)
        conductor_host = QWidget()
        conductor_layout = QVBoxLayout(conductor_host)
        conductor_layout.setContentsMargins(0, 0, 0, 0)
        conductor_layout.addWidget(QLabel("Conductors"))
        conductor_layout.addWidget(self.conductors_table)
        conductor_layout.addLayout(conductor_buttons)

        lower = QWidget()
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 8, 0, 0)
        lower_layout.addWidget(form_host)
        lower_layout.addWidget(conductor_host, 1)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(lower)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)

        box = QDialogButtonBox()
        apply = box.addButton("Apply", QDialogButtonBox.ButtonRole.ApplyRole)
        apply.clicked.connect(self.apply_form)
        close = box.addButton(QDialogButtonBox.StandardButton.Close)
        close.clicked.connect(self.accept)
        layout.addWidget(box)

        self.refresh()

    def spares_value(self) -> int | None:
        value = int(self.spare_count.value())
        if value < 0:
            return None
        return value

    def propose(self) -> None:
        result = self.session.propose_cables(spares=self.spares_value(), replace=False, show_tags=True)
        self.refresh()
        self.status.setText(f"Proposed {result['count']} cable(s)")

    def add_cable(self) -> None:
        device = str(self.device.currentText() or "").strip()
        result = self.session.add_cable(to_device=device, show_tag=self.show_tag.isChecked())
        self.refresh()
        self.select_tag(str(result["tag"]))
        self.status.setText(f"Added {result['tag']}")

    def delete_selected(self) -> None:
        tag = self._selected_tag()
        if not tag:
            return
        self.session.delete_cable(tag)
        self._editing = ""
        self.refresh()
        self.status.setText(f"Deleted {tag}")

    def apply_form(self) -> None:
        tag = self._editing or self._selected_tag()
        if not tag:
            self.add_cable()
            tag = self._editing
        if not tag:
            return
        fields = self.form_values()
        result = self.session.update_cable(tag, **fields)
        self.refresh()
        self.select_tag(str(result["tag"]))
        self.status.setText(f"Saved {result['tag']}")

    def assign_from_wires(self) -> None:
        tag = self._editing or self._selected_tag()
        if not tag:
            return
        result = self.session.remap_cable(tag, spares=self.spares_value())
        self.refresh()
        self.select_tag(str(result["tag"]))
        self.status.setText(f"Assigned conductors on {result['tag']}")

    def apply_conductors(self) -> None:
        tag = self._editing or self._selected_tag()
        if not tag:
            return
        existing = {
            str(land.get("id")): dict(land)
            for land in (next((item.get("assignments") or [] for item in self.session.list_cables() if item["tag"] == tag), []))
        }
        rows: list[dict[str, Any]] = []
        for edited in self.conductor_rows():
            base = dict(existing.get(edited["id"]) or {})
            role = str(edited.get("role") or "")
            if role.strip().upper() in {"GND", "GROUND"}:
                edited["role"] = "ground"
            base.update(edited)
            rows.append(base)
        result = self.session.assign_cable_conductors(tag, rows)
        self.refresh()
        self.select_tag(str(result["tag"]))
        self.status.setText(f"Conductors saved on {result['tag']}")

    def form_values(self) -> dict[str, Any]:
        length = float(self.length.value())
        return {
            "tag": self.tag.text().strip(),
            "from_strip": self.from_strip.text().strip(),
            "from_device": self.from_device.text().strip(),
            "from_terminals": self.terminals.text().strip(),
            "to_device": str(self.device.currentText() or "").strip(),
            "to_kind": str(self.kind.currentText() or "").strip(),
            "cable_type": self.cable_type.text().strip(),
            "conductors": int(self.conductors.value()),
            "gauge": self.gauge.text().strip(),
            "shield": self.shield.isChecked(),
            "drain": self.drain.isChecked(),
            "shield_bond": str(self.shield_bond.currentText() or "").strip(),
            "length": None if length <= 0 else length,
            "length_unit": str(self.unit.currentText() or "ft"),
            "slack_pct": float(self.slack.value()),
            "gland": self.gland.text().strip(),
            "route": self.route.text().strip(),
            "part_number": self.part_number.text().strip(),
            "part_source": self.part_source.text().strip(),
            "show_tag": self.show_tag.isChecked(),
        }

    def conductor_rows(self) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for row in range(self.conductors_table.rowCount()):
            values = [self._cell(row, col) for col in range(self.conductors_table.columnCount())]
            if not any(values):
                continue
            rows.append(
                {
                    "id": values[0],
                    "color": values[1],
                    "panel_terminal": values[2],
                    "field_terminal": values[3],
                    "wire_number": values[4],
                    "role": values[5],
                    "spare": values[6].strip().lower() in {"yes", "y", "true", "1", "spare"},
                }
            )
        return rows

    def select_tag(self, tag: str) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.text() == tag:
                self.table.selectRow(row)
                self._load_tag(tag)
                return

    def set_length(self, value: float) -> None:
        self.length.setValue(float(value))

    def set_slack(self, value: float) -> None:
        self.slack.setValue(float(value))

    def set_conductor_color(self, row: int, color: str) -> None:
        self.conductors_table.setItem(row, 1, QTableWidgetItem(color))

    def refresh(self) -> None:
        self._fill_devices()
        cables = self.session.list_cables()
        self.table.setRowCount(len(cables))
        for row, cable in enumerate(cables):
            length = ""
            if cable.get("length") is not None:
                length = f"{cable['length']:g} {cable.get('length_unit') or 'ft'}"
            values = [
                cable["tag"],
                cable.get("from_strip") or cable.get("from_device") or "",
                cable.get("to_device") or "",
                cable.get("cable_type") or "",
                cable.get("gauge") or "",
                length,
                cable.get("route") or "",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        if cables and not self._selected_tag():
            self.table.selectRow(0)
        self.status.setText(f"{len(cables)} field cable(s)")

    def _fill_devices(self) -> None:
        current = str(self.device.currentText() or "")
        names: list[str] = []
        for sheet in self.session.project.sheets:
            if sheet.kind in {"panel_layout", "door_layout"}:
                continue
            for comp in sheet.components:
                try:
                    device = self.session.catalog.get(comp.library_id)
                except KeyError:
                    device = None
                if field_kind(device, comp) and comp.id not in names:
                    names.append(comp.id)
        for cable in self.session.project.cables:
            if cable.to_device and cable.to_device not in names:
                names.append(cable.to_device)
        self.device.blockSignals(True)
        self.device.clear()
        self.device.addItem("")
        for name in names:
            self.device.addItem(name)
        if current:
            self.device.setCurrentText(current)
        self.device.blockSignals(False)

    def _selected_tag(self) -> str:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.text() if item else ""

    def _load_selected(self) -> None:
        tag = self._selected_tag()
        if tag:
            self._load_tag(tag)

    def _load_tag(self, tag: str) -> None:
        cable = next((item for item in self.session.list_cables() if item["tag"] == tag), None)
        if cable is None:
            return
        self._editing = tag
        self.tag.setText(cable["tag"])
        self.from_strip.setText(cable.get("from_strip") or "")
        self.from_device.setText(cable.get("from_device") or "")
        self.terminals.setText(", ".join(cable.get("from_terminals") or []))
        self.device.setCurrentText(cable.get("to_device") or "")
        kind = cable.get("to_kind") or ""
        index = self.kind.findText(kind)
        self.kind.setCurrentIndex(index if index >= 0 else 0)
        self.cable_type.setText(cable.get("cable_type") or "")
        self.conductors.setValue(int(cable.get("conductors") or 0))
        self.gauge.setText(cable.get("gauge") or "")
        self.shield.setChecked(bool(cable.get("shield")))
        self.drain.setChecked(bool(cable.get("drain")))
        bond = cable.get("shield_bond") or ""
        bond_index = self.shield_bond.findText(bond)
        self.shield_bond.setCurrentIndex(bond_index if bond_index >= 0 else 0)
        length = cable.get("length")
        self.length.setValue(float(length) if length is not None else 0.0)
        unit = cable.get("length_unit") or "ft"
        self.unit.setCurrentText(unit if unit in {"ft", "m"} else "ft")
        slack = cable.get("slack_pct")
        self.slack.setValue(float(slack) if slack is not None else 0.0)
        self.gland.setText(cable.get("gland") or "")
        self.route.setText(cable.get("route") or "")
        self.part_number.setText(cable.get("part_number") or "")
        self.part_source.setText(cable.get("part_source") or "")
        self.show_tag.setChecked(bool(cable.get("show_tag", True)))
        assignments = cable.get("assignments") or []
        self.conductors_table.setRowCount(len(assignments))
        for row, land in enumerate(assignments):
            values = [
                land.get("id") or "",
                land.get("color") or "",
                land.get("panel_terminal") or "",
                land.get("field_terminal") or "",
                land.get("wire_number") or land.get("wire_id") or "",
                "GND" if land.get("role") == "ground" else (land.get("role") or ""),
                "yes" if land.get("spare") else "",
            ]
            for col, value in enumerate(values):
                self.conductors_table.setItem(row, col, QTableWidgetItem(str(value)))
        self.conductors_table.resizeColumnsToContents()
        self.conductors_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

    def _cell(self, row: int, col: int) -> str:
        item = self.conductors_table.item(row, col)
        return item.text().strip() if item else ""
