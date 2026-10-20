"""I/O wizard: platform, modules, point assignment, then a sheet preview."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.library.io_modules import format_point_address, list_modules, unverified_message

_FIELDS = (
    ("spare", "Spare"),
    ("pushbutton", "Pushbutton"),
    ("limit_switch", "Limit switch"),
    ("prox_3wire", "3-wire proximity switch"),
    ("relay_coil", "Relay coil"),
    ("solenoid", "Solenoid"),
    ("pilot_light", "Pilot light"),
    ("transmitter_2wire", "2-wire transmitter"),
    ("transmitter_4wire", "4-wire transmitter"),
)

def _io_phrase(module) -> str:
    count = len(module.points)
    prefix = f"{count} " if count else ""
    phrases = {
        "di_sink_source": f"{prefix}inputs, sink/source",
        "di_sink": f"{prefix}inputs, sink",
        "di_source": f"{prefix}inputs, source",
        "do_sink": f"{prefix}outputs, sink",
        "do_source": f"{prefix}outputs, source",
        "do_relay": f"{prefix}relay outputs",
        "ai": f"{prefix}analog inputs",
        "ao": f"{prefix}analog outputs",
    }
    return phrases.get(module.point_type, f"{prefix}points".strip())


def module_list_label(module) -> str:
    """Words for the module list. Library ids stay off this line."""

    name = (module.catalog or "").strip() or (module.manufacturer or "").strip() or "Module"
    if module.verified:
        return f"{name}  ({_io_phrase(module)})"
    raw = unverified_message(module).lower()
    if "old symbol" in raw or "does not match" in raw:
        why = "old symbol, not drawn"
    elif "generic symbol" in raw:
        why = "generic symbol, no terminal map"
    elif "not checked" in raw:
        why = "not checked against the manual"
    else:
        why = "no verified terminal map"
    return f"{name}  — {why}"


def _select_field(widget: QComboBox, raw: str) -> None:
    """Match a CSV field by its stored code or by the words in the list."""

    text = raw.strip()
    index = widget.findData(text)
    if index < 0:
        folded = text.casefold()
        for row in range(widget.count()):
            if widget.itemText(row).casefold() == folded or str(widget.itemData(row) or "").casefold() == folded:
                index = row
                break
    if index >= 0:
        widget.setCurrentIndex(index)


_PLATFORM_LABELS = {
    "brx": "BRX",
    "click": "CLICK",
    "compactlogix": "CompactLogix",
    "micro800": "Micro800",
    "s7-1200": "S7-1200",
    "s7-1500": "S7-1500",
    "productivity": "Productivity",
    "generic": "Generic",
}


class IoWizard(QDialog):
    def __init__(self, session: Session, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self._applied = False
        self._result: dict | None = None
        self.setWindowTitle("PLC I/O sheets")
        self.resize(860, 620)

        self.platform = QComboBox()
        self.platform.addItem("All platforms", "")
        seen: list[str] = []
        for module in list_modules(session.catalog):
            if module.platform and module.platform not in seen:
                seen.append(module.platform)
                label = _PLATFORM_LABELS.get(module.platform, module.platform)
                self.platform.addItem(label, module.platform)
        self.platform.currentIndexChanged.connect(self._fill_modules)

        self.slot = QSpinBox()
        self.slot.setRange(0, 32)
        self.slot.setValue(1)
        self.position = QSpinBox()
        self.position.setRange(0, 16)
        self.position.setValue(1)
        self.polarity = QComboBox()
        self.polarity.addItems(["", "sink", "source"])
        self.address_base = QSpinBox()
        # -1 is "module default". 0 is a real base (TIA %I0.0, Do-more X0).
        self.address_base.setRange(-1, 4096)
        self.address_base.setSpecialValueText("module default")
        self.address_base.setValue(-1)
        self.fuses = QCheckBox("Fuse the hot feed")

        form = QFormLayout()
        form.addRow("Platform", self.platform)
        form.addRow("First slot", self.slot)
        form.addRow("CLICK position", self.position)
        form.addRow("Polarity", self.polarity)
        form.addRow("Address base", self.address_base)
        form.addRow("", self.fuses)
        page_platform = QWidget()
        lay = QVBoxLayout(page_platform)
        lay.addWidget(QLabel("Pick the PLC family, then the modules to draw."))
        lay.addLayout(form)
        lay.addStretch(1)

        self.modules = QListWidget()
        page_modules = QWidget()
        mod_lay = QVBoxLayout(page_modules)
        mod_lay.addWidget(QLabel("Checked modules are drawn. Unverified modules stay listed with the reason they cannot."))
        mod_lay.addWidget(self.modules)

        self.place = QTableWidget(0, 5)
        self.place.setHorizontalHeaderLabels(["Module", "Slot", "Position", "Polarity", "Address base"])
        self.points = QTableWidget(0, 6)
        self.points.setHorizontalHeaderLabels(["Module", "Pin", "Address", "Tag", "Description", "Field"])
        self.points.horizontalHeader().setStretchLastSection(True)
        import_btn = QPushButton("Import CSV…")
        import_btn.clicked.connect(self._import_csv)
        page_points = QWidget()
        pt_lay = QVBoxLayout(page_points)
        pt_lay.addWidget(QLabel("Each module has its own slot, position, polarity, and address base. Then assign tags."))
        pt_lay.addWidget(self.place)
        pt_lay.addWidget(import_btn)
        pt_lay.addWidget(self.points)

        self.preview = QLabel("Generate to preview the first sheet.")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.preview)
        self.scroll.setWidgetResizable(False)
        self.zoom = QSlider(Qt.Orientation.Horizontal)
        self.zoom.setRange(50, 300)
        self.zoom.setValue(100)
        self.zoom.valueChanged.connect(self._apply_zoom)
        self._preview_image: QImage | None = None
        self.open_btn = QPushButton("Open sheet")
        self.open_btn.clicked.connect(self._open_sheet)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        page_preview = QWidget()
        pv_lay = QVBoxLayout(page_preview)
        zoom_row = QHBoxLayout()
        zoom_row.addWidget(QLabel("Zoom"))
        zoom_row.addWidget(self.zoom, 1)
        zoom_row.addWidget(self.open_btn)
        pv_lay.addLayout(zoom_row)
        pv_lay.addWidget(self.scroll, 1)
        pv_lay.addWidget(self.status)

        self.stack = QStackedWidget()
        self.stack.addWidget(page_platform)
        self.stack.addWidget(page_modules)
        self.stack.addWidget(page_points)
        self.stack.addWidget(page_preview)

        self.back_btn = QPushButton("Back")
        self.next_btn = QPushButton("Next")
        self.back_btn.clicked.connect(self._back)
        self.next_btn.clicked.connect(self._next)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.back_btn)
        buttons.addWidget(self.next_btn)

        root = QVBoxLayout(self)
        root.addWidget(self.stack)
        root.addLayout(buttons)
        self._fill_modules()
        self._sync_buttons()

    def _fill_modules(self) -> None:
        self.modules.clear()
        platform = str(self.platform.currentData() or "")
        for module in list_modules(self.session.catalog, platform or None):
            text = module_list_label(module)
            if module.verified:
                device = self.session.catalog.get(module.library_id)
                if str(getattr(device.panel, "dim_source", "") or "") != "catalog":
                    text += "  — placeholder footprint"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, module.id)
            if module.verified:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
            else:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.modules.addItem(item)

    def selected_module_ids(self) -> list[str]:
        chosen: list[str] = []
        for row in range(self.modules.count()):
            item = self.modules.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                chosen.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return chosen

    def _checked_modules(self):
        ids = set(self.selected_module_ids())
        return [module for module in list_modules(self.session.catalog) if module.id in ids]

    def _refs_for(self, modules) -> list[str]:
        used = {comp.id for sheet in self.session.project.sheets for comp in sheet.components}
        refs: list[str] = []
        n = 1
        for _module in modules:
            ref = f"IO{n}"
            while ref in used:
                n += 1
                ref = f"IO{n}"
            used.add(ref)
            refs.append(ref)
            n += 1
        return refs

    def _fill_placement(self) -> None:
        modules = self._checked_modules()
        refs = self._refs_for(modules)
        self.place.setRowCount(len(modules))
        for row, module in enumerate(modules):
            name = QTableWidgetItem(f"{refs[row]}  {module.catalog or module.id}")
            name.setData(Qt.ItemDataRole.UserRole, module.id)
            name.setData(Qt.ItemDataRole.UserRole + 1, refs[row])
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.place.setItem(row, 0, name)
            slot = QSpinBox()
            slot.setRange(0, 32)
            slot.setValue(self.slot.value() + row)
            self.place.setCellWidget(row, 1, slot)
            position = QSpinBox()
            position.setRange(0, 16)
            position.setValue(self.position.value() + row)
            self.place.setCellWidget(row, 2, position)
            polarity = QComboBox()
            polarity.addItems(["", "sink", "source"])
            polarity.setCurrentText(self.polarity.currentText())
            self.place.setCellWidget(row, 3, polarity)
            base = QSpinBox()
            base.setRange(-1, 4096)
            base.setSpecialValueText("module default")
            base.setValue(self.address_base.value())
            self.place.setCellWidget(row, 4, base)

    def specs(self) -> list[dict]:
        modules = self._checked_modules()
        if self.place.rowCount() == len(modules) and modules:
            specs = []
            for row, module in enumerate(modules):
                item = self.place.item(row, 0)
                ref = str(item.data(Qt.ItemDataRole.UserRole + 1) or "") if item else ""
                slot = self.place.cellWidget(row, 1)
                position = self.place.cellWidget(row, 2)
                polarity = self.place.cellWidget(row, 3)
                base = self.place.cellWidget(row, 4)
                base_value = base.value() if isinstance(base, QSpinBox) else -1
                specs.append(
                    {
                        "module": module.id,
                        "ref": ref or None,
                        "slot": slot.value() if isinstance(slot, QSpinBox) else 1,
                        "position": position.value() if isinstance(position, QSpinBox) else 1,
                        "polarity": polarity.currentText() if isinstance(polarity, QComboBox) else "",
                        "address_base": None if base_value < 0 else base_value,
                    }
                )
            return specs
        refs = self._refs_for(modules)
        base = None if self.address_base.value() < 0 else self.address_base.value()
        return [
            {
                "module": module.id,
                "ref": ref,
                "slot": self.slot.value() + index,
                "position": self.position.value() + index,
                "polarity": self.polarity.currentText(),
                "address_base": base,
            }
            for index, (module, ref) in enumerate(zip(modules, refs))
        ]

    def _fill_points(self) -> None:
        self._fill_placement()
        self.points.setRowCount(0)
        for spec in self.specs():
            module = next(item for item in list_modules(self.session.catalog) if item.id == spec["module"])
            for point in module.points:
                address = format_point_address(
                    module,
                    point,
                    slot=int(spec["slot"]),
                    position=int(spec["position"]),
                    address_base=spec["address_base"] if spec["address_base"] is not None else module.address_base,
                )
                row = self.points.rowCount()
                self.points.insertRow(row)
                for col, text in enumerate((spec["ref"], point.pin, address)):
                    item = QTableWidgetItem(text)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self.points.setItem(row, col, item)
                self.points.setItem(row, 3, QTableWidgetItem(""))
                self.points.setItem(row, 4, QTableWidgetItem(""))
                field = QComboBox()
                for code, label in _FIELDS:
                    field.addItem(label, code)
                self.points.setCellWidget(row, 5, field)

    def assignments(self) -> list[dict]:
        rows: list[dict] = []
        for row in range(self.points.rowCount()):
            tag = self.points.item(row, 3).text().strip() if self.points.item(row, 3) else ""
            description = self.points.item(row, 4).text().strip() if self.points.item(row, 4) else ""
            widget = self.points.cellWidget(row, 5)
            field = str(widget.currentData() or "spare") if isinstance(widget, QComboBox) else "spare"
            if not tag and not description and field in {"", "spare"}:
                continue
            rows.append(
                {
                    "module": self.points.item(row, 0).text(),
                    "pin": self.points.item(row, 1).text(),
                    "tag": tag or None,
                    "description": description or None,
                    "field": field or None,
                }
            )
        return rows

    def load_points_csv(self, path: Path | str) -> int:
        import csv

        raw = Path(path).read_text(encoding="utf-8-sig")
        updated = 0
        for record in csv.DictReader(raw.splitlines()):
            if not record:
                continue
            lowered = {(key or "").strip().lower(): (value or "").strip() for key, value in record.items()}
            pin = lowered.get("pin") or lowered.get("address") or ""
            module = lowered.get("module") or ""
            for row in range(self.points.rowCount()):
                if self.points.item(row, 1).text() != pin:
                    continue
                if module and self.points.item(row, 0).text() != module:
                    continue
                if lowered.get("tag"):
                    self.points.item(row, 3).setText(lowered["tag"])
                if lowered.get("description"):
                    self.points.item(row, 4).setText(lowered["description"])
                if lowered.get("field"):
                    widget = self.points.cellWidget(row, 5)
                    if isinstance(widget, QComboBox):
                        _select_field(widget, lowered["field"])
                updated += 1
                break
        return updated

    def _import_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "I/O points", "", "CSV (*.csv)")
        if not path:
            return
        count = self.load_points_csv(path)
        self.status.setText(f"Imported {count} point rows.")

    def generate(self) -> dict:
        if self._applied and self._result is not None:
            return self._result
        specs = self.specs()
        if not specs:
            raise ValueError("check at least one verified module")
        # An unchecked box leaves the project fuse setting alone.
        fuses = True if self.fuses.isChecked() else None
        result = self.session.apply_io_wizard(specs, self.assignments(), fuses=fuses)
        self._applied = True
        self._result = result
        return result

    def _paint_preview(self) -> None:
        result = self._result or {}
        sheets = result.get("sheets") or []
        if not sheets:
            return
        sheet = self.session.project.sheet(sheets[0])
        width, height = sheet.page.dimensions()
        scale = min(780.0 / width, 420.0 / height, 2.0)
        image = QImage(max(1, int(width * scale)), max(1, int(height * scale)), QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.white)
        painter = QPainter(image)
        painter.scale(scale, scale)
        from panelcad.render.page import paint_sheet

        index = self.session.project.sheets.index(sheet)
        paint_sheet(
            painter,
            self.session.project,
            sheet,
            self.session.catalog,
            sheet_index=index,
            sheet_count=len(self.session.project.sheets),
        )
        painter.end()
        self._preview_image = image
        self._apply_zoom()
        warnings = result.get("warnings") or []
        self.status.setText(
            f"Drew {', '.join(sheets)}." + (f" {len(warnings)} warning(s)." if warnings else "")
        )

    def _apply_zoom(self) -> None:
        image = self._preview_image
        if image is None:
            return
        factor = max(0.5, self.zoom.value() / 100.0)
        scaled = image.scaled(
            max(1, int(image.width() * factor)),
            max(1, int(image.height() * factor)),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview.setPixmap(QPixmap.fromImage(scaled))
        self.preview.resize(scaled.size())

    def _open_sheet(self) -> None:
        sheets = (self._result or {}).get("sheets") or []
        if sheets:
            self.session.set_active_sheet(sheets[0])
        self.accept()

    def _sync_buttons(self) -> None:
        index = self.stack.currentIndex()
        self.back_btn.setEnabled(index > 0)
        self.next_btn.setText("Finish" if index == self.stack.count() - 1 else "Next")

    def _back(self) -> None:
        if self.stack.currentIndex() == 3 and self._applied:
            self.session.undo()
            self._applied = False
            self._result = None
        self.stack.setCurrentIndex(max(0, self.stack.currentIndex() - 1))
        self._sync_buttons()

    def _next(self) -> None:
        index = self.stack.currentIndex()
        if index == 1 and not self.selected_module_ids():
            QMessageBox.information(self, "PLC I/O sheets", "Check a verified module.")
            return
        if index == 1:
            self._fill_points()
        if index == 2:
            try:
                self.generate()
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "PLC I/O sheets", str(exc))
                return
            self._paint_preview()
        if index == self.stack.count() - 1:
            self.accept()
            return
        self.stack.setCurrentIndex(index + 1)
        self._sync_buttons()
