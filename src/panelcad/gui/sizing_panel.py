"""Motor sizing dock: the arithmetic for one branch, from the sheet or from typed values."""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from panelcad.core.motor_sizing import size_motor_circuit


class SizingPanel(QWidget):
    """Shows NEC 430 / UL 508A steps. The Size button does not write the project."""

    def __init__(self) -> None:
        super().__init__()
        self.hp = QLineEdit()
        self.hp.setPlaceholderText("5")
        self.kw = QLineEdit()
        self.kw.setPlaceholderText("optional")
        self.volts = QLineEdit()
        self.volts.setPlaceholderText("460")
        self.phases = QComboBox()
        self.phases.addItem("3-phase", 3)
        self.phases.addItem("1-phase", 1)
        self.nameplate = QLineEdit()
        self.nameplate.setPlaceholderText("nameplate FLA, optional")
        self.device = QComboBox()
        for label, key in (
            ("Inverse-time breaker", "inverse_time_breaker"),
            ("Dual-element fuse", "dual_element_fuse"),
            ("Nontime-delay fuse", "nontime_delay_fuse"),
            ("Instantaneous trip", "instantaneous_trip"),
        ):
            self.device.addItem(label, key)
        self.wire_table = QComboBox()
        self.wire_table.addItem("Automatic — UL 508A 29.6, not verified", "auto")
        self.wire_table.addItem("Terminations marked 75°C", "marked-75")
        self.wire_table.addItem("Terminations marked 60°C", "marked-60")
        self.standard = QComboBox()
        self.standard.addItem("NEMA/ANSI", "NEMA/ANSI")
        self.standard.addItem("IEC", "IEC")

        form = QFormLayout()
        form.addRow("hp", self.hp)
        form.addRow("kW", self.kw)
        form.addRow("Volts", self.volts)
        form.addRow("Phases", self.phases)
        form.addRow("Nameplate A", self.nameplate)
        form.addRow("Branch device", self.device)
        form.addRow("Terminations", self.wire_table)
        form.addRow("Standard", self.standard)

        button = QPushButton("Size")
        button.clicked.connect(self.calculate)
        row = QHBoxLayout()
        row.addWidget(button)
        row.addStretch(1)

        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setPlaceholderText("Motor circuit sizing")
        self.report.setMinimumHeight(80)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(row)
        layout.addWidget(self.report, 1)
        # The form's natural hint is about 500 px. Ignored lets this share the
        # BOM tab height instead of pushing the schematic canvas down.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored)
        self.setMinimumHeight(100)

    def calculate(self) -> dict[str, Any]:
        result = size_motor_circuit(
            hp=_blank_float(self.hp.text()),
            kw=_blank_float(self.kw.text()),
            volts=_blank_float(self.volts.text()),
            phases=int(self.phases.currentData() or 3),
            fla=_blank_float(self.nameplate.text()),
            standard=str(self.standard.currentData() or "NEMA/ANSI"),
            device=str(self.device.currentData() or "inverse_time_breaker"),
            wire_table=str(self.wire_table.currentData() or "auto"),
        )
        self.report.setPlainText(str(result.get("report") or result.get("error") or ""))
        return result

    def show_report(self, text: str) -> None:
        self.report.setPlainText(text)

    def load_project(self, session: Any) -> None:
        """Fill the form from the first schematic motor and show its stored sizing."""

        motor = _first_motor(session)
        if motor is None:
            return
        props = motor.properties
        self._set(self.hp, props.get("hp"))
        self._set(self.kw, props.get("kW"))
        self._set(self.volts, props.get("voltage"))
        self._set(self.nameplate, props.get("nameplate_fla"))
        phases = props.get("phases") or 3
        index = self.phases.findData(int(phases))
        if index >= 0:
            self.phases.setCurrentIndex(index)
        rule = str(props.get("termination_rule") or "")
        if "marked 75" in rule:
            term_key = "marked-75"
        elif "marked 60" in rule:
            term_key = "marked-60"
        else:
            term_key = "auto"
        term_index = self.wire_table.findData(term_key)
        if term_index >= 0:
            self.wire_table.setCurrentIndex(term_index)
        code = str(props.get("sizing_code") or "")
        std_index = self.standard.findData("IEC" if code == "IEC" else "NEMA/ANSI")
        if std_index >= 0:
            self.standard.setCurrentIndex(std_index)
        summary = str(props.get("sizing_summary") or "").strip()
        if summary:
            self.report.setPlainText(summary)

    def _set(self, box: QLineEdit, value: Any) -> None:
        if value in (None, ""):
            box.clear()
            return
        box.setText(str(value))


def _blank_float(text: str) -> float | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    return float(raw)


def _first_motor(session: Any) -> Any | None:
    for sheet in session.project.sheets:
        if sheet.kind in {"panel_layout", "door_layout", "terminal"}:
            continue
        for comp in sheet.components:
            role = str(comp.properties.get("role") or "")
            if role == "motor" or "motor" in str(comp.library_id or "").lower():
                if role == "motor" or "loads.motor" in str(comp.library_id or "").lower():
                    return comp
    return None
