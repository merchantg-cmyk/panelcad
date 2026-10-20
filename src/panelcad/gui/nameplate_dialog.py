"""Reports → Nameplate. Form, live plate, and real-size export."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.electrical.nameplate import (
    CAUTION_TEMPLATE,
    PRESETS,
    SHOP_CONFIRM_PLACEHOLDER,
    _CONFIRM_BASES,
    _flag_label,
    build_nameplate,
)
from panelcad.electrical.nameplate_page import paint_marks
from panelcad.render.qt_painter import QtPainter


def _quiet_button(text: str) -> QPushButton:
    """A push button Enter will not activate.

    Inside a QDialog the first QPushButton is auto-default, so Return in a
    line edit commits the field and then clicks Export PDF.
    """

    button = QPushButton(text)
    button.setAutoDefault(False)
    button.setDefault(False)
    return button


class NameplatePreview(QWidget):
    """The plate at its real proportion, scaled to the widget."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._plate = None
        self.setMinimumSize(420, 280)
        self.setAutoFillBackground(True)

    def set_plate(self, plate) -> None:
        self._plate = plate
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#d9d3c5"))
        plate = self._plate
        if plate is None:
            painter.end()
            return
        margin = 28
        avail_w = max(10, self.width() - 2 * margin)
        avail_h = max(10, self.height() - 2 * margin)
        scale = min(avail_w / plate.width_mm, avail_h / plate.height_mm)
        origin_x = (self.width() - plate.width_mm * scale) / 2.0
        origin_y = (self.height() - plate.height_mm * scale) / 2.0
        painter.setClipping(False)
        painter.translate(origin_x, origin_y)
        painter.scale(scale, scale)
        paint_marks(QtPainter(painter, print_paper=True), plate.marks, plate.width_mm, plate.height_mm)
        painter.end()


class NameplateDialog(QDialog):
    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        from panelcad.electrical.nameplate import note_nameplate_started

        note_nameplate_started(self.session.project)
        self.setWindowTitle("Nameplate")
        self.resize(1180, 780)
        self._edits: dict[str, QLineEdit] = {}
        self._badges: dict[str, QLabel] = {}
        self._loading = False
        self._reload_queued = False

        root = QVBoxLayout(self)
        splitter = QSplitter()
        root.addWidget(splitter, stretch=1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(480)
        form_host = QWidget()
        self.form = QFormLayout(form_host)
        # Long labels sit above the field so the edit is the full form width.
        # A side-by-side label was clipping "Acme Process Controls" to "cme".
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        scroll.setWidget(form_host)
        splitter.addWidget(scroll)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.preview = NameplatePreview()
        right_layout.addWidget(self.preview, stretch=1)
        self.working = QTextEdit()
        self.working.setReadOnly(True)
        self.working.setMinimumHeight(180)
        right_layout.addWidget(self.working)
        splitter.addWidget(right)
        splitter.setSizes([460, 700])

        options = QHBoxLayout()
        self.preset = QComboBox()
        self.preset.addItem("3 × 5 in (5 in wide × 3 in high)", "3x5")
        self.preset.addItem("Custom", "custom")
        self.preset.currentIndexChanged.connect(self._size_changed)
        self.width_in = QDoubleSpinBox()
        self.width_in.setRange(1.0, 24.0)
        self.width_in.setDecimals(2)
        self.width_in.setSuffix(" in wide")
        self.height_in = QDoubleSpinBox()
        self.height_in.setRange(1.0, 24.0)
        self.height_in.setDecimals(2)
        self.height_in.setSuffix(" in high")
        self.width_in.editingFinished.connect(self._size_changed)
        self.height_in.editingFinished.connect(self._size_changed)
        self.service = QComboBox()
        self.service.addItem("Service equipment: not confirmed", "unset")
        self.service.addItem("Not service equipment", "no")
        self.service.addItem("Suitable for use as service equipment", "yes")
        self.service.currentIndexChanged.connect(self._options_changed)
        self.control_only = QComboBox()
        self.control_only.addItem("SCCR required", "unset")
        self.control_only.addItem("Control circuit only — SCCR not required", "yes")
        self.control_only.currentIndexChanged.connect(self._options_changed)
        options.addWidget(self.preset)
        options.addWidget(self.width_in)
        options.addWidget(self.height_in)
        root.addLayout(options)
        flags = QHBoxLayout()
        flags.addWidget(self.service)
        flags.addWidget(self.control_only)
        root.addLayout(flags)

        loc = QHBoxLayout()
        loc.addWidget(QLabel("Disconnect locations"))
        self.locations = QLineEdit()
        self.locations.setPlaceholderText("Required when more than one supply is 50 V or more")
        self.locations.setTextMargins(6, 1, 4, 1)
        self.locations.editingFinished.connect(self._options_changed)
        loc.addWidget(self.locations, stretch=1)
        root.addLayout(loc)

        caution_row = QHBoxLayout()
        caution_row.addWidget(QLabel("Caution template"))
        self.caution = QLineEdit()
        self.caution.setObjectName("nameplate-caution")
        self.caution.setTextMargins(6, 1, 4, 1)
        self.caution.setPlaceholderText(CAUTION_TEMPLATE)
        self.caution.editingFinished.connect(self._options_changed)
        self.caution_ok = QComboBox()
        self.caution_ok.addItem("Verify against UL 508A 55.4 / NEC 409", "no")
        self.caution_ok.addItem("Caution wording confirmed", "yes")
        self.caution_ok.currentIndexChanged.connect(self._options_changed)
        caution_row.addWidget(self.caution, stretch=1)
        caution_row.addWidget(self.caution_ok)
        root.addLayout(caution_row)

        holes = QHBoxLayout()
        self.hole_d = QDoubleSpinBox()
        self.hole_d.setRange(0.0, 1.0)
        self.hole_d.setDecimals(3)
        self.hole_d.setSpecialValueText("no holes")
        self.hole_d.setSuffix(" in hole")
        self.hole_inset = QDoubleSpinBox()
        self.hole_inset.setRange(0.0, 2.0)
        self.hole_inset.setDecimals(3)
        self.hole_inset.setSpecialValueText("no holes")
        self.hole_inset.setSuffix(" in inset")
        self.hole_d.editingFinished.connect(self._options_changed)
        self.hole_inset.editingFinished.connect(self._options_changed)
        holes.addWidget(QLabel("Mounting holes"))
        holes.addWidget(self.hole_d)
        holes.addWidget(self.hole_inset)
        holes.addStretch(1)
        root.addLayout(holes)

        buttons = QHBoxLayout()
        pdf = _quiet_button("Export PDF")
        pdf.clicked.connect(self.export_pdf)
        dxf = _quiet_button("Export DXF")
        dxf.clicked.connect(self.export_dxf)
        close = _quiet_button("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(pdf)
        buttons.addWidget(dxf)
        buttons.addStretch(1)
        buttons.addWidget(close)
        root.addLayout(buttons)
        self.reload()

    def reload(self) -> None:
        plate = build_nameplate(self.session.project, self.session.catalog)
        self._loading = True
        store = self.session.project.properties.get("nameplate") or {}
        preset = str(store.get("preset") or plate.preset or "3x5")
        self.preset.setCurrentIndex(0 if preset == "3x5" else 1)
        width_in, height_in = PRESETS["3x5"]
        if preset != "3x5":
            width_in = plate.width_mm / 25.4
            height_in = plate.height_mm / 25.4
        self.width_in.setValue(width_in)
        self.height_in.setValue(height_in)
        self.width_in.setEnabled(preset != "3x5")
        self.height_in.setEnabled(preset != "3x5")
        service = store.get("service_equipment")
        self.service.setCurrentIndex(0 if service is None else (2 if service else 1))
        self.control_only.setCurrentIndex(1 if store.get("control_circuit_only") else 0)
        self.locations.setText(str(store.get("disconnect_locations") or ""))
        self.caution.setText(str(store.get("caution_text") or CAUTION_TEMPLATE))
        self.caution_ok.setCurrentIndex(1 if store.get("caution_confirmed") else 0)
        self.hole_d.setValue(float(store.get("hole_diameter_in") or 0))
        self.hole_inset.setValue(float(store.get("hole_inset_in") or 0))
        self._fill_form(plate)
        self._show(plate)
        self._loading = False

    def _fill_form(self, plate) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        self._edits.clear()
        self._badges.clear()
        for item in plate.fields:
            if item.hidden and item.key == "service_equipment":
                continue
            edit = QLineEdit(item.value)
            edit.setObjectName(f"nameplate-{item.key}")
            edit.setTextMargins(8, 1, 8, 1)
            edit.setCursorPosition(0)
            shown = item.value or ""
            if shown:
                edit.setMinimumWidth(edit.fontMetrics().horizontalAdvance(shown) + 28)
            note = str(item.note or "").strip()
            if note:
                edit.setToolTip(note)
            base = item.key.split(":", 1)[0]
            if item.overridden and item.computed:
                edit.setPlaceholderText(f"drawing: {item.computed}")
            elif item.computed:
                edit.setPlaceholderText(str(item.computed))
            elif note and base in _CONFIRM_BASES:
                edit.setPlaceholderText(SHOP_CONFIRM_PLACEHOLDER)
            elif note:
                edit.setPlaceholderText(note)
            edit.editingFinished.connect(lambda key=item.key: self._commit_field(key))
            flag = _flag_label(item)
            badge = QLabel({"OVR": "overridden", "CONFIRM": "confirm", "VERIFY": "verify"}.get(flag, ""))
            badge.setObjectName(f"badge-{item.key}")
            reset = _quiet_button("Reset")
            reset.clicked.connect(lambda _checked=False, key=item.key: self._reset_field(key))
            host = QWidget()
            row_host = QHBoxLayout(host)
            row_host.setContentsMargins(0, 0, 0, 0)
            row_host.addWidget(edit, stretch=1)
            row_host.addWidget(badge)
            row_host.addWidget(reset)
            self.form.addRow(item.label, host)
            self._edits[item.key] = edit
            self._badges[item.key] = badge

    def _show(self, plate) -> None:
        self.preview.set_plate(plate)
        chunks = list(plate.to_dict()["working"])
        if plate.engineer_notes:
            chunks.append("")
            chunks.append("Engineer confirm")
            chunks.extend(plate.engineer_notes)
        if plate.issues:
            chunks.append("")
            chunks.append("Checks")
            chunks.extend(issue["message"] for issue in plate.issues)
        self.working.setPlainText("\n".join(chunks))

    def _commit_field(self, key: str) -> None:
        if self._loading:
            return
        edit = self._edits.get(key)
        if edit is None:
            return
        text = edit.text().strip()
        plate = self.preview._plate
        current = plate.field(key) if plate is not None else None
        if current is None:
            return
        shown = str(current.value or "").strip()
        computed = str(current.computed or "").strip()
        if text == shown:
            return
        if text == computed:
            if current.overridden:
                self.session.nameplate_set(clear=[key])
                self._schedule_reload()
            return
        self.session.nameplate_set(**{key: text})
        self._schedule_reload()

    def _schedule_reload(self) -> None:
        """Rebuild after the line edit's own focus handler has returned.

        ``editingFinished`` runs while Qt is still inside that QLineEdit.
        ``reload`` deletes the row, and deleting the edit from its own handler
        segfaults (0xC0000005 / SIGSEGV).
        """

        if self._reload_queued:
            return
        self._reload_queued = True
        QTimer.singleShot(0, self, self._reload_soon)

    def _reload_soon(self) -> None:
        self._reload_queued = False
        self.reload()

    def _reset_field(self, key: str) -> None:
        if self._loading:
            return
        self.session.nameplate_set(clear=[key])
        self._schedule_reload()

    def _size_changed(self) -> None:
        if self._loading:
            return
        preset = self.preset.currentData()
        if preset == "3x5":
            self.session.nameplate_set(preset="3x5")
        else:
            self.session.nameplate_set(
                preset="custom",
                width_in=float(self.width_in.value()),
                height_in=float(self.height_in.value()),
            )
        self.reload()

    def _options_changed(self) -> None:
        if self._loading:
            return
        service = self.service.currentData()
        control = self.control_only.currentData()
        store = self.session.project.properties.get("nameplate") or {}
        updates: dict[str, Any] = {}
        locations = self.locations.text().strip()
        if locations != str(store.get("disconnect_locations") or ""):
            updates["disconnect_locations"] = locations
        service_value = "" if service == "unset" else ("yes" if service == "yes" else "no")
        stored_service = store.get("service_equipment")
        stored_service_token = "" if stored_service is None else ("yes" if stored_service else "no")
        if service_value != stored_service_token:
            updates["service_equipment"] = service_value
        control_value = "" if control == "unset" else "yes"
        stored_control = "yes" if store.get("control_circuit_only") else ""
        if control_value != stored_control:
            updates["control_circuit_only"] = control_value
        caution = self.caution.text().strip()
        stored_caution = str(store.get("caution_text") or "")
        if caution != stored_caution and not (caution == CAUTION_TEMPLATE and not stored_caution):
            updates["caution_text"] = caution
        confirmed = self.caution_ok.currentData() == "yes"
        if confirmed != bool(store.get("caution_confirmed")):
            updates["caution_confirmed"] = "yes" if confirmed else ""
        hole_d = float(self.hole_d.value()) if self.hole_d.value() > 0 and self.hole_inset.value() > 0 else None
        hole_inset = float(self.hole_inset.value()) if hole_d is not None else None
        stored_d = store.get("hole_diameter_in")
        stored_i = store.get("hole_inset_in")
        if hole_d is None:
            if stored_d not in {None, ""} or stored_i not in {None, ""}:
                updates["hole_diameter_in"] = ""
                updates["hole_inset_in"] = ""
        elif float(stored_d or 0) != hole_d or float(stored_i or 0) != hole_inset:
            updates["hole_diameter_in"] = hole_d
            updates["hole_inset_in"] = hole_inset
        if not updates:
            return
        self.session.nameplate_set(**updates)
        self.reload()

    def _confirm_export(self) -> bool:
        plate = self.preview._plate
        if plate is None:
            return True
        rows = [
            issue["message"]
            for issue in plate.issues
            if issue.get("check") in {"nameplate_unconfirmed", "nameplate_flc_approximate"}
        ]
        if not rows:
            return True
        answer = QMessageBox.warning(
            self,
            "Nameplate",
            "These values are not confirmed:\n\n" + "\n".join(rows) + "\n\nExport anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def export_pdf(self) -> None:
        if not self._confirm_export():
            return
        path, _ = QFileDialog.getSaveFileName(self, "Nameplate PDF", "nameplate.pdf", "PDF (*.pdf)")
        if path:
            self.session.nameplate_export(pdf=Path(path))

    def export_dxf(self) -> None:
        if not self._confirm_export():
            return
        path, _ = QFileDialog.getSaveFileName(self, "Nameplate DXF", "nameplate.dxf", "DXF (*.dxf)")
        if path:
            self.session.nameplate_export(dxf=Path(path))
