"""Project setting for 3-phase power colors."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)


class PhaseColorDialog(QDialog):
    """Edit the palettes drawn on 3-phase power sheets."""

    def __init__(self, settings: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Phase colors")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        note = QLabel(
            "Power conductors take the color of the phase. Black, red, and blue are a "
            "US shop convention for 208/240 V. Brown, orange, and yellow are a US shop "
            "convention for 480 V. Brown, black, and grey are a permitted IEC default, "
            "not a required phase order. IEC neutral: blue (default light blue #5eb0e8). "
            "IEC 60445 §6.2.2 is unverified from an official source. The NEMA neutral "
            "is white/grey. Ground is green, with a yellow edge on IEC sheets. Control "
            "wiring is unchanged. Grey is a solid mid-grey and yellow is a dark gold, "
            "both the same width as any other power wire."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.enabled = QCheckBox("Color 3-phase power conductors")
        self.enabled.setChecked(bool(settings.get("enabled", True)))
        form.addRow("", self.enabled)

        voltage = settings.get("system_voltage")
        self.voltage = QLineEdit("" if voltage is None else f"{float(voltage):g}")
        self.voltage.setPlaceholderText("follow the drawing")
        self.voltage.setToolTip(
            "Blank follows the motor voltage on the drawing. "
            "Below the cutoff, NEMA uses the 208/240 V palette."
        )
        form.addRow("System voltage", self.voltage)

        self.high_from = QDoubleSpinBox()
        self.high_from.setRange(50.0, 1000.0)
        self.high_from.setDecimals(0)
        self.high_from.setSuffix(" V")
        self.high_from.setValue(float(settings.get("nema_high_voltage") or 300))
        self.high_from.setToolTip("NEMA uses the 480 V palette at or above this voltage.")
        form.addRow("480 V from", self.high_from)

        self.nema_low = self._triple(form, "NEMA 208/240", settings.get("nema_low") or {})
        self.nema_high = self._triple(form, "NEMA 480", settings.get("nema_high") or {})
        self.iec = self._triple(form, "IEC", settings.get("iec") or {})

        self.neutral = QLineEdit(str(settings.get("neutral") or ""))
        self.neutral.setPlaceholderText("white")
        self.neutral.setToolTip("NEMA neutral. White/grey is the US shop convention.")
        form.addRow("NEMA neutral", self.neutral)
        self.iec_neutral = QLineEdit(str(settings.get("iec_neutral") or ""))
        self.iec_neutral.setPlaceholderText("blue (default light blue)")
        self.iec_neutral.setToolTip(
            "IEC neutral: blue (default light blue #5eb0e8). "
            "IEC 60445 §6.2.2 is unverified from an official source."
        )
        form.addRow("IEC neutral", self.iec_neutral)
        self.ground = QLineEdit(str(settings.get("ground") or ""))
        self.ground.setPlaceholderText("green")
        form.addRow("Ground", self.ground)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _triple(self, form: QFormLayout, title: str, palette: dict) -> list[QLineEdit]:
        row = QWidget()
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        edits: list[QLineEdit] = []
        for key, label in (("l1", "L1"), ("l2", "L2"), ("l3", "L3")):
            edit = QLineEdit(str(palette.get(key) or ""))
            edit.setPlaceholderText(label)
            edit.setToolTip(f"{title} {label}. A color name or #rrggbb.")
            box.addWidget(QLabel(label))
            box.addWidget(edit, 1)
            edits.append(edit)
        form.addRow(title, row)
        return edits

    def values(self) -> dict[str, object]:
        """Fields for Session.set_phase_colors. A blank voltage follows the drawing."""

        voltage = self.voltage.text().strip()

        def pack(edits: list[QLineEdit]) -> str:
            return ",".join(edit.text().strip() for edit in edits)

        return {
            "enabled": self.enabled.isChecked(),
            "system_voltage": voltage or "auto",
            "nema_high_voltage": float(self.high_from.value()),
            "nema_low": pack(self.nema_low),
            "nema_high": pack(self.nema_high),
            "iec": pack(self.iec),
            "neutral": self.neutral.text().strip(),
            "iec_neutral": self.iec_neutral.text().strip(),
            "ground": self.ground.text().strip(),
        }
