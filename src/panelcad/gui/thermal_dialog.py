"""Tools → Panel → Thermal… — watt table, rise, and cooling. Does not resize the box."""

from __future__ import annotations

from typing import Any

from html import escape

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


_MOUNTS = (
    ("", "As drawn"),
    ("wall", "Wall"),
    ("free", "Free-standing"),
    ("row_end_free", "Row end, free"),
    ("row_end_wall", "Row end, wall"),
    ("row_mid_free", "Row middle, free"),
    ("row_mid_wall", "Row middle, wall"),
    ("row_mid_wall_covered", "Row middle, covered"),
)
_MATERIALS = (
    ("", "As drawn"),
    ("steel", "Painted steel"),
    ("stainless", "Stainless"),
    ("aluminum", "Aluminium"),
    ("aluminum_double", "Aluminium double wall"),
    ("plastic", "Plastic"),
)
_NEMAS = ("", "1", "12", "3R", "4", "4X", "6", "6P")


class ThermalDialog(QDialog):
    """Human path for the enclosure thermal report."""

    def __init__(self, session: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.report: dict[str, Any] = {}
        self._edited: dict[str, float] = {}
        self.setWindowTitle("Enclosure Thermal")
        self.setMinimumSize(640, 400)
        self.resize(960, 640)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.findings = QLabel()
        self.findings.setWordWrap(True)

        form = QFormLayout()
        self.ambient = self._spin(40.0, -40.0, 80.0, 1)
        self.tmax = self._spin(50.0, -20.0, 90.0, 1)
        self.tmin = self._spin(0.0, -40.0, 40.0, 1)
        self.tmin.setSpecialValueText("not set")
        self.tmin.setMinimum(-40.0)
        self.cold = self._spin(0.0, -50.0, 40.0, 1)
        self.cold.setSpecialValueText("same as ambient")
        self.cold.setMinimum(-50.0)
        self.component_max = self._spin(40.0, 0.0, 80.0, 1)
        self.altitude = self._spin(0.0, 0.0, 5000.0, 0)
        self.outdoor = QCheckBox("Outdoor")
        self.outdoor.setToolTip("Doubles the unpowered heater, per the Rittal outdoor factor.")
        self.mount = QComboBox()
        for value, label in _MOUNTS:
            self.mount.addItem(label, value)
        self.material = QComboBox()
        for value, label in _MATERIALS:
            self.material.addItem(label, value)
        self.nema = QComboBox()
        self.nema.addItem("As drawn", "")
        for value in _NEMAS[1:]:
            self.nema.addItem(f"NEMA {value}", value)
        form.addRow("Ambient °C", self.ambient)
        form.addRow("Max internal °C", self.tmax)
        form.addRow("Min internal °C", self.tmin)
        form.addRow("Cold ambient °C", self.cold)
        form.addRow("Part max if unrated °C", self.component_max)
        form.addRow("Altitude m", self.altitude)
        form.addRow("Mount", self.mount)
        form.addRow("Material", self.material)
        form.addRow("NEMA", self.nema)
        form.addRow("", self.outdoor)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Ref", "Catalog", "W", "Basis", "Max °C", "Margin", "Note"])
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(240)
        self.table.cellChanged.connect(self._on_cell)

        self.assumptions = QPlainTextEdit()
        self.assumptions.setReadOnly(True)
        self.assumptions.setMaximumHeight(120)

        buttons = QDialogButtonBox()
        self.recalc_btn = buttons.addButton("Recalculate", QDialogButtonBox.ButtonRole.ActionRole)
        self.save_btn = buttons.addButton("Save settings", QDialogButtonBox.ButtonRole.ActionRole)
        self.pdf_btn = buttons.addButton("Save PDF…", QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        self.recalc_btn.clicked.connect(self.recalculate)
        self.save_btn.clicked.connect(self.save_settings)
        self.pdf_btn.clicked.connect(self.save_pdf)
        buttons.rejected.connect(self.reject)

        left = QWidget()
        left.setLayout(form)
        left.setMaximumWidth(340)
        body = QHBoxLayout()
        body.addWidget(left)
        right = QVBoxLayout()
        right.addWidget(self.table, 1)
        right.addWidget(QLabel("Assumptions"))
        right.addWidget(self.assumptions)
        body.addLayout(right, 1)

        root = QVBoxLayout(self)
        root.addWidget(self.summary)
        root.addWidget(self.findings)
        root.addLayout(body, 1)
        root.addWidget(buttons)
        self._load_settings()
        self.recalculate()

    def _spin(self, value: float, low: float, high: float, decimals: int) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(low, high)
        box.setDecimals(decimals)
        box.setValue(value)
        return box

    def _load_settings(self) -> None:
        from panelcad.core.thermal import THERMAL_KEY, settings_from

        stored = self.session.project.properties.get(THERMAL_KEY) or {}
        settings = settings_from(self.session.project)
        self.ambient.setValue(settings.ambient_c)
        self.tmax.setValue(settings.max_internal_c)
        if isinstance(stored, dict) and stored.get("min_internal_c") not in (None, ""):
            self.tmin.setValue(float(stored["min_internal_c"]))
        else:
            self.tmin.setValue(self.tmin.minimum())
        if isinstance(stored, dict) and stored.get("min_ambient_c") not in (None, ""):
            self.cold.setValue(float(stored["min_ambient_c"]))
        else:
            self.cold.setValue(self.cold.minimum())
        self.component_max.setValue(settings.component_max_c)
        self.altitude.setValue(settings.altitude_m)
        self.outdoor.setChecked(bool(settings.outdoor))
        self._select(self.mount, settings.mount)
        self._select(self.material, settings.material)
        self._select(self.nema, settings.nema)

    @staticmethod
    def _select(box: QComboBox, value: str) -> None:
        index = box.findData(value or "")
        box.setCurrentIndex(0 if index < 0 else index)

    def overrides(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ambient_c": float(self.ambient.value()),
            "max_internal_c": float(self.tmax.value()),
            "component_max_c": float(self.component_max.value()),
            "altitude_m": float(self.altitude.value()),
            "outdoor": self.outdoor.isChecked(),
            "mount": str(self.mount.currentData() or ""),
            "material": str(self.material.currentData() or ""),
            "nema": str(self.nema.currentData() or ""),
        }
        if self.tmin.value() <= self.tmin.minimum() + 1e-9:
            data["min_internal_c"] = None
        else:
            data["min_internal_c"] = float(self.tmin.value())
        if self.cold.value() <= self.cold.minimum() + 1e-9:
            data["min_ambient_c"] = None
        else:
            data["min_ambient_c"] = float(self.cold.value())
        if self._edited:
            data["estimates"] = dict(self._edited)
        return data

    def recalculate(self) -> None:
        self.report = self.session.compute_heat_load(**self.overrides())
        self._fill()

    def _fill(self) -> None:
        report = self.report
        rec = report.get("recommendation") or {}
        enc = report.get("enclosure") or {}
        box = "No enclosure is on the panel sheet yet."
        if enc:
            box = (
                f"{enc.get('width_mm'):g} × {enc.get('height_mm'):g} × {enc.get('depth_mm'):g} mm, "
                f"{enc.get('material_label')}, mount {enc.get('mount')}, NEMA {enc.get('nema') or '—'}."
            )
        self.summary.setText(_summary_html(report, box, str(rec.get("summary") or "")))
        found = report.get("findings") or []
        if found:
            self.findings.setText("\n".join(str(item.get("message") or "") for item in found))
        else:
            self.findings.setText("No thermal design checks.")
        from panelcad.core.thermal import format_margin

        rows = list(report.get("devices") or [])
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            limit = row.get("max_ambient_c")
            limit_s = "" if limit is None else f"{float(limit):g}"
            if row.get("max_ambient_basis") == "user" and limit_s:
                limit_s += " user"
            margin = row.get("margin_k")
            values = [
                str(row.get("ref") or ""),
                str(row.get("catalog") or ""),
                f"{float(row.get('watts') or 0):.2f}",
                str(row.get("source_label") or ""),
                limit_s,
                "" if margin is None else format_margin(float(margin)),
                str(row.get("note") or ""),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col != 2 or row.get("basis") != "estimate":
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                else:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                if col == 5 and margin is not None and float(margin) < 0:
                    from panelcad.gui.theme import ui_qcolor

                    item.setForeground(ui_qcolor("alert_text"))
                item.setToolTip(str(row.get("note") or ""))
                self.table.setItem(index, col, item)
        for index, width in enumerate((52, 110, 64, 80, 64, 64)):
            self.table.setColumnWidth(index, width)
        self.table.resizeRowsToContents()
        self.table.blockSignals(False)
        self.assumptions.setPlainText("\n".join(report.get("assumptions") or []))

    def _on_cell(self, row: int, col: int) -> None:
        if col != 2:
            return
        item = self.table.item(row, 0)
        watts = self.table.item(row, 2)
        basis = self.table.item(row, 3)
        if item is None or watts is None or basis is None:
            return
        if basis.text() != "ESTIMATE":
            return
        try:
            self._edited[item.text()] = float(watts.text())
        except ValueError:
            return

    def save_settings(self) -> None:
        fields = self.overrides()
        estimates = fields.pop("estimates", None)
        with self.session.command("thermal settings"):
            self.session.set_ambient(**fields)
            if estimates:
                for ref, watts in estimates.items():
                    self.session.set_watt_estimate(ref, watts)
                self._edited.clear()
        self.recalculate()

    def save_pdf(self) -> None:
        dest, _ = QFileDialog.getSaveFileName(self, "Thermal report", "thermal.pdf", "PDF (*.pdf)")
        if not dest:
            return
        self.session.export_thermal_pdf(dest, **self.overrides())


def _summary_html(report: dict[str, Any], box: str, recommendation: str) -> str:
    """Two-column label/value block, with the recommendation on its own bold line."""

    def cell(text: object) -> str:
        return escape("" if text is None else str(text))

    def num(value: object, digits: int, suffix: str) -> str:
        if value is None:
            return "—"
        return f"{float(value):.{digits}f}{suffix}"

    pairs = (
        ("Total", num(report.get("watts_total"), 2, " W"), "Ambient", num(report.get("ambient_c"), 1, " °C")),
        ("Published", num(report.get("watts_published"), 2, " W"), "Max internal", num(report.get("max_internal_c"), 1, " °C")),
        ("Estimated", num(report.get("watts_estimate"), 2, " W"), "Rise", num(report.get("rise_k"), 1, " K")),
        ("Area", num(report.get("surface_m2"), 3, " m²"), "Uncontrolled", num(report.get("internal_c"), 1, " °C")),
        ("k", num(report.get("k_w_m2k"), 1, " W/m²K"), "Design", num(report.get("design_c"), 1, " °C")),
    )
    rows = []
    for left, left_v, right, right_v in pairs:
        rows.append(
            "<tr>"
            f"<td>{cell(left)}</td><td>&nbsp;&nbsp;{cell(left_v)}</td>"
            "<td>&nbsp;&nbsp;&nbsp;&nbsp;</td>"
            f"<td>{cell(right)}</td><td>&nbsp;&nbsp;{cell(right_v)}</td>"
            "</tr>"
        )
    return (
        f"<div>{cell(box)}<br>"
        f"<table cellspacing='0' cellpadding='1'>{''.join(rows)}</table>"
        f"<p><b>{cell(recommendation)}</b></p></div>"
    )
