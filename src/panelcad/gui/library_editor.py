"""Symbol Builder. Edit a symbol block, then save it into the library.

The block is the geometry, the wire connections, and the attributes (TAG1, TAG2,
DESC, MFG, CAT) that fill in when the symbol is inserted. Builtin YAML is never
overwritten.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
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
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.core.block import PALETTE, infer_block_kind, missing_attributes
from panelcad.core.models import Device, PinDef
from panelcad.gui.symbol_canvas import TOOLS, SymbolCanvas
from panelcad.library.catalog import device_to_raw
from panelcad.library.paths import is_builtin_path

SIDES = ["left", "right", "top", "bottom"]
PIN_COLS = ["id", "x", "y", "side"]
TOOL_LABELS = {
    "select": "Select",
    "line": "Line",
    "box": "Box",
    "circle": "Circle",
    "arc": "Arc",
    "poly": "Points",
    "attr": "Attribute",
    "pin": "Connection",
}


class LibraryEditor(QDialog):
    def __init__(
        self,
        session: Session,
        device: Device | None = None,
        *,
        duplicate: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.source = device
        self.duplicate = duplicate or (
            device is not None and is_builtin_path(session.catalog.source_path(device.library_id))
        )
        self._syncing = False
        title = "Symbol Builder"
        if device is not None:
            title = "Symbol Builder — duplicate" if self.duplicate else "Symbol Builder — edit"
        self.setWindowTitle(title)
        self._view = "schematic"
        raw = device_to_raw(device) if device is not None else {}
        schematic = raw.get("schematic") or {}
        panel_raw = raw.get("panel") or {}
        self._fp_model_items = list(panel_raw.get("draw") or [])
        self._fp_attrs = [dict(a) for a in panel_raw.get("attributes") or []]
        self._sch_model_items = list(schematic.get("draw") or [])
        self._sch_attrs = [dict(a) for a in schematic.get("attributes") or []]
        self.resize(980, 720)
        layout = QVBoxLayout(self)

        if self.duplicate and device is not None:
            layout.addWidget(
                QLabel(
                    f"Builtin {device.library_id} is read-only. Saving writes local.<id> "
                    "under this project's library/ folder."
                )
            )

        form = QFormLayout()
        default_id = ""
        if device is not None:
            default_id = device.id + ("_2" if self.duplicate else "")
        self.part_id = QLineEdit(default_id)
        self.name = QLineEdit(device.name if device else "")
        self.category = QLineEdit(device.category if device else "misc")
        self.ref_prefix = QLineEdit(device.ref_prefix if device else "A")
        self.width = QDoubleSpinBox()
        self.width.setRange(4, 400)
        self.width.setValue(device.schematic_width if device else 28)
        self.height = QDoubleSpinBox()
        self.height.setRange(4, 400)
        self.height.setValue(device.schematic_height if device else 16)
        self.kind = QComboBox()
        for label, value in (
            ("Parent (coil, breaker, drive)", "parent"),
            ("Child (contact)", "child"),
            ("Terminal", "terminal"),
            ("Panel footprint", "footprint"),
        ):
            self.kind.addItem(label, value)
        if device is not None:
            self.kind.setCurrentIndex(max(0, self.kind.findData(infer_block_kind(device))))
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.view = QComboBox()
        self.view.addItem("Schematic block", "schematic")
        self.view.addItem("Footprint block", "footprint")
        self.view.currentIndexChanged.connect(self._view_changed)
        self.values = QCheckBox("Inserted values")
        self.values.toggled.connect(self._toggle_values)
        form.addRow("Block", self.kind)
        form.addRow("Id", self.part_id)
        form.addRow("Name", self.name)
        form.addRow("Category", self.category)
        form.addRow("Ref prefix", self.ref_prefix)
        form.addRow("Editing", self.view)
        form.addRow("", self.values)
        form.addRow("Symbol width mm", self.width)
        form.addRow("Symbol height mm", self.height)
        self.width.valueChanged.connect(self._size_changed)
        self.height.valueChanged.connect(self._size_changed)
        layout.addLayout(form)

        self.attr_row = QHBoxLayout()
        self.attr_row.addWidget(QLabel("Place attribute"))
        self._attr_buttons: list[QPushButton] = []
        layout.addLayout(self.attr_row)
        self._rebuild_attribute_buttons()

        layout.addWidget(QLabel("Connections — id, millimetres, side (left/right = inline on a rung)"))
        self.pins = QTableWidget(0, len(PIN_COLS))
        self.pins.setHorizontalHeaderLabels(PIN_COLS)
        self.pins.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.pins)
        pin_btns = QHBoxLayout()
        add_pin = QPushButton("Add pin")
        add_pin.clicked.connect(self._add_pin)
        del_pin = QPushButton("Remove pin")
        del_pin.clicked.connect(self._remove_pin)
        pin_btns.addWidget(add_pin)
        pin_btns.addWidget(del_pin)
        pin_btns.addStretch()
        layout.addLayout(pin_btns)

        layout.addWidget(
            QLabel(
                "Draw the block on the millimetre grid. Line, Box, and Circle drag. "
                "Arc: click the start, a point on the curve, then the end. The curve stays an arc. "
                "Points: click corners, Enter or right-click to finish. "
                "Attribute: pick a name above, then click where it sits on the block. "
                "Connection: click an edge. Select drags geometry, attributes, and connections. "
                "Arrows nudge 1 mm (Shift 5 mm). Delete removes the selection. "
                "Switch Editing to draw the panel footprint as its own block."
            )
        )
        self.canvas = SymbolCanvas()
        self.canvas.setMinimumSize(420, 260)
        self.canvas.set_symbol_size(self.width.value(), self.height.value())
        if device is not None:
            self.canvas.set_draw(schematic.get("draw") or [])
        else:
            self._sch_attrs = [{"tag": "TAG1", "x": 2.0, "y": -3.0, "h": 2.4}]
        self.canvas.set_attributes(self._sch_attrs)
        self.canvas.value_for = self._sample_value
        self.canvas.pins_changed.connect(self._pins_from_canvas)
        self.canvas.attributes_changed.connect(self._attributes_from_canvas)
        tools = QHBoxLayout()
        self._tool_group = QButtonGroup(self)
        self._tool_group.setExclusive(True)
        for i, name in enumerate(TOOLS):
            btn = QToolButton()
            btn.setText(TOOL_LABELS[name])
            btn.setCheckable(True)
            btn.setChecked(name == "line")
            self._tool_group.addButton(btn, i)
            tools.addWidget(btn)
        self._tool_group.idClicked.connect(self._set_tool)
        del_stroke = QPushButton("Delete")
        del_stroke.clicked.connect(self._delete_selected)
        tools.addWidget(del_stroke)
        tools.addStretch()
        layout.addLayout(tools)
        layout.addWidget(self.canvas, 1)

        if device is not None:
            for pin in device.pins:
                self._add_pin(pin)
        else:
            self._add_pin(PinDef(id="1", x=0, y=8, side="left"))
            self._add_pin(PinDef(id="2", x=28, y=8, side="right"))
        self.canvas.set_pins(self._pin_rows())
        self.pins.itemChanged.connect(self._pins_to_canvas)

        foot = QHBoxLayout()
        self.pw = QDoubleSpinBox()
        self.pw.setRange(0, 2000)
        self.ph = QDoubleSpinBox()
        self.ph.setRange(0, 2000)
        self.pd = QDoubleSpinBox()
        self.pd.setRange(0, 2000)
        panel = device.panel if device is not None else None
        self.pw.setValue(panel.width_mm if panel else 18)
        self.ph.setValue(panel.height_mm if panel else 80)
        self.pd.setValue(panel.depth_mm if panel else 60)
        self.din = QCheckBox("DIN rail")
        self.din.setChecked(True if panel is None else panel.din_rail)
        self.in_panel = QCheckBox("in panel")
        self.in_panel.setChecked(True if panel is None else panel.in_panel)
        foot.addWidget(QLabel("Footprint W×H×D"))
        foot.addWidget(self.pw)
        foot.addWidget(self.ph)
        foot.addWidget(self.pd)
        foot.addWidget(self.din)
        foot.addWidget(self.in_panel)
        self.pw.valueChanged.connect(self._size_changed)
        self.ph.valueChanged.connect(self._size_changed)
        layout.addLayout(foot)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _set_tool(self, index: int) -> None:
        self.canvas.set_tool(TOOLS[index])
        self.canvas.setFocus()

    def _delete_selected(self) -> None:
        if self.canvas.selected_attr is not None:
            del self.canvas.attributes[self.canvas.selected_attr]
            self.canvas.selected_attr = None
            self.canvas.attributes_changed.emit()
            self.canvas.update()
            self.canvas.setFocus()
            return
        if self.canvas.selected is None:
            return
        self.canvas.model.delete(self.canvas.selected)
        self.canvas.selected = None
        self.canvas.draw_changed.emit()
        self.canvas.update()
        self.canvas.setFocus()

    def _add_pin(self, pin: PinDef | None = None) -> None:
        row = self.pins.rowCount()
        self.pins.insertRow(row)
        pin = pin if isinstance(pin, PinDef) else PinDef(id=f"P{row + 1}", x=0, y=8, side="left")
        self.pins.setItem(row, 0, QTableWidgetItem(pin.id))
        self.pins.setItem(row, 1, QTableWidgetItem(str(pin.x)))
        self.pins.setItem(row, 2, QTableWidgetItem(str(pin.y)))
        side = QComboBox()
        side.addItems(SIDES)
        side.setCurrentText(pin.side)
        side.currentTextChanged.connect(lambda *_: self._pins_to_canvas())
        self.pins.setCellWidget(row, 3, side)
        self._pins_to_canvas()

    def _remove_pin(self) -> None:
        row = self.pins.currentRow()
        if row >= 0:
            self.pins.removeRow(row)
            self._pins_to_canvas()

    def _pin_rows(self) -> list[dict]:
        pins: list[dict] = []
        for row in range(self.pins.rowCount()):
            pid = self.pins.item(row, 0)
            xs = self.pins.item(row, 1)
            ys = self.pins.item(row, 2)
            side_w = self.pins.cellWidget(row, 3)
            if pid is None or not pid.text().strip():
                continue
            pins.append(
                {
                    "id": pid.text().strip(),
                    "x": float(xs.text() if xs else 0),
                    "y": float(ys.text() if ys else 0),
                    "side": side_w.currentText() if isinstance(side_w, QComboBox) else "left",
                }
            )
        return pins

    def _pins_to_canvas(self) -> None:
        if self._syncing or not hasattr(self, "canvas"):
            return
        self.canvas.set_pins(self._pin_rows())

    def _pins_from_canvas(self) -> None:
        if self._view != "schematic":
            return
        self._syncing = True
        self.pins.setRowCount(0)
        for pin in self.canvas.pins:
            self._add_pin(
                PinDef(
                    id=str(pin.get("id") or "P"),
                    x=float(pin.get("x") or 0),
                    y=float(pin.get("y") or 0),
                    side=str(pin.get("side") or "left"),
                )
            )
        self._syncing = False
        self.canvas.set_pins(self._pin_rows())

    def _spec(self) -> dict:
        short = self.part_id.text().strip()
        if not short:
            raise ValueError("Id is required")
        self._capture_view()
        spec = {
            "id": short,
            "name": self.name.text().strip() or short,
            "category": self.category.text().strip() or "misc",
            "ref_prefix": self.ref_prefix.text().strip() or "A",
            "block": str(self.kind.currentData() or "parent"),
            "pins": self._pin_rows(),
            "width": self.width.value(),
            "height": self.height.value(),
            "draw": self._stored_draw("schematic"),
            "attributes": self._stored_attrs("schematic"),
            "footprint_draw": self._stored_draw("footprint"),
            "footprint_attributes": self._stored_attrs("footprint"),
            "panel": {
                "w": self.pw.value(),
                "h": self.ph.value(),
                "d": self.pd.value(),
                "din": self.din.isChecked(),
                "in_panel": self.in_panel.isChecked(),
                "mount": "din_rail" if self.din.isChecked() else "none",
                "dims": "generic",
            },
        }
        if self.source is not None:
            spec["library_id"] = self.source.library_id
        return spec

    def _save(self) -> None:
        try:
            spec = self._spec()
            missing = missing_attributes(spec["block"], spec["attributes"], spec["footprint_attributes"])
            started = spec["attributes"] or spec["footprint_attributes"]
            if started and missing:
                raise ValueError("Place the required attribute: " + ", ".join(missing))
            result = self.session.save_library_device(spec)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Symbol Builder", str(exc))
            return
        QMessageBox.information(
            self,
            "Symbol Builder",
            f"Saved block {result['library_id']}\n{result['path']}",
        )
        self.accept()

    def _kind_changed(self) -> None:
        if not hasattr(self, "attr_row"):
            return
        self._rebuild_attribute_buttons()

    def _rebuild_attribute_buttons(self) -> None:
        while self.attr_row.count() > 1:
            item = self.attr_row.takeAt(1)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._attr_buttons.clear()
        kind = str(self.kind.currentData() or "parent")
        for tag in PALETTE.get(kind, PALETTE["parent"]):
            btn = QPushButton(tag)
            btn.clicked.connect(lambda _checked=False, name=tag: self._arm_attribute(name))
            self.attr_row.addWidget(btn)
            self._attr_buttons.append(btn)
        self.attr_row.addStretch()

    def _arm_attribute(self, tag: str) -> None:
        self.canvas.pending_attr = tag
        index = TOOLS.index("attr")
        self._tool_group.button(index).setChecked(True)
        self.canvas.set_tool("attr")
        self.canvas.setFocus()

    def _toggle_values(self, checked: bool) -> None:
        self.canvas.show_values = bool(checked)
        self.canvas.update()

    def _sample_value(self, tag: str) -> str:
        samples = {
            "TAG1": "K1",
            "TAG2": "K1",
            "P_TAG1": "K1",
            "TAGSTRIP": "X1",
            "TERM": "1",
            "DESC1": self.name.text().strip() or "Description",
            "DESC2": "",
            "FAMILY": self.category.text().strip().split(".")[-1] or "family",
            "MFG": "ABB",
            "CAT": "100-C09",
            "RATING1": "24VDC",
            "XREF": "2:K1",
            "LOC": "PANEL",
            "INST": "1",
            "ITEM": "4",
            "WIRE": "12",
        }
        return samples.get(tag.upper(), "")

    def _capture_view(self) -> None:
        if self._view == "schematic":
            self._sch_model_items = self.canvas.model.to_draw()
            self._sch_attrs = [dict(a) for a in self.canvas.attributes]
        else:
            self._fp_model_items = self.canvas.model.to_draw()
            self._fp_attrs = [dict(a) for a in self.canvas.attributes]

    def _stored_draw(self, view: str) -> list:
        if view == self._view:
            return self.canvas.model.to_draw()
        if view == "schematic":
            return list(self._sch_model_items)
        return list(self._fp_model_items)

    def _stored_attrs(self, view: str) -> list:
        if view == self._view:
            return [dict(a) for a in self.canvas.attributes]
        if view == "footprint":
            return [dict(a) for a in self._fp_attrs]
        return [dict(a) for a in self._sch_attrs]

    def _view_changed(self) -> None:
        nxt = str(self.view.currentData() or "schematic")
        if nxt == self._view:
            return
        self._capture_view()
        self._view = nxt
        if nxt == "footprint":
            self.canvas.set_draw(self._fp_model_items)
            self.canvas.set_attributes(self._fp_attrs)
            self.canvas.set_symbol_size(self.pw.value(), max(self.ph.value(), 8))
        else:
            self.canvas.set_draw(self._sch_model_items)
            self.canvas.set_attributes(self._sch_attrs)
            self.canvas.set_symbol_size(self.width.value(), self.height.value())
        self.canvas.set_pins(self._pin_rows() if nxt == "schematic" else [])
        self.canvas.update()

    def _attributes_from_canvas(self) -> None:
        self._capture_view()

    def _size_changed(self) -> None:
        if self._view == "footprint":
            self.canvas.set_symbol_size(self.pw.value(), max(self.ph.value(), 8))
        else:
            self.canvas.set_symbol_size(self.width.value(), self.height.value())


SymbolBuilder = LibraryEditor
