"""Circuit Builder — pick a motor circuit and insert it."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from panelcad.gui.screen_fit import OptionScroll, fit_when_shown


class CircuitBuilderDialog(QDialog):
    """Options for inserting a motor circuit onto the open project."""

    def __init__(self, parent: QWidget | None = None, standard: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Circuit Builder")
        self.setMinimumWidth(460)
        # fit_when_shown owns the work area. The theme clamp must not reparent Insert.
        self.setProperty("_pc_own_fit", True)
        layout = QVBoxLayout(self)
        title = QLabel("Insert a motor circuit")
        font = title.font()
        font.setPointSize(11)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        shown = str(standard or "").strip() or "NEMA/ANSI"
        note = QLabel(
            "DOL is a branch device, contactor, overload, and motor. "
            "A VFD is the branch, an optional line contactor, the drive, and the motor "
            "(no overload on the motor leads). "
            "Reversing is FWD/REV contactors with mechanical and electrical interlock. "
            "Star-delta (wye-delta) uses main, star, and delta contactors; pick the timer "
            "setting and whether the overload sits in the windings (0.58 × FLA) or the line "
            "(1.0 × FLA). "
            "A soft starter is the branch, SS, optional bypass, and 2-wire or 3-wire control. "
            "PLC motor is a DOL (or VFD) starter with an interposing CR, optional HOA "
            "(Hand = local 3-wire, Off = neither, Auto = PLC), a PLC model, and I/O addresses. "
            "Power lands on the open schematic (or a new Motor power sheet); "
            "new-kind control lands on Motor control. "
            "New in-cabinet devices go into free space on the existing panel; "
            "operators go on the door. "
            f"Symbols follow the drawing standard ({shown}; default NEMA/ANSI). "
            "Pick IEC to draw reversing, star-delta, soft starter, and PLC motor "
            "control as vertical columns."
        )
        note.setWordWrap(True)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.addWidget(note)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.standard = QComboBox()
        self.standard.addItem("IEC", "IEC")
        self.standard.addItem("NEMA/ANSI", "NEMA/ANSI")
        token = "".join(ch.lower() for ch in shown if ch.isalnum())
        if token == "iec":
            self.standard.setCurrentIndex(0)
        else:
            self.standard.setCurrentIndex(1)
        self.standard.setToolTip("IEC columns for the new motor circuits, or a NEMA ladder.")
        form.addRow("Drawing standard", self.standard)
        self.kind = QComboBox()
        self.kind.addItem("DOL starter", "dol")
        self.kind.addItem("VFD", "vfd")
        self.kind.addItem("Reversing starter", "reversing")
        self.kind.addItem("Star-delta (wye-delta)", "star_delta")
        self.kind.addItem("Soft starter", "soft_starter")
        self.kind.addItem("PLC-controlled motor", "plc_motor")
        form.addRow("Circuit", self.kind)

        self.shared_supply = QCheckBox("Share the panel control supply")
        self.shared_supply.setObjectName("sharedSupply")
        self.shared_supply.setChecked(True)
        self.shared_supply.setToolTip(
            "A second circuit uses the control supply already on the panel. Uncheck to add a supply for this motor."
        )
        form.addRow("", self.shared_supply)

        self.shared_estop = QCheckBox("One E-stop for the panel")
        self.shared_estop.setObjectName("sharedEstop")
        self.shared_estop.setChecked(True)
        self.shared_estop.setToolTip(
            "One E-stop wired into every motor circuit. Uncheck for an E-stop on this motor."
        )
        form.addRow("", self.shared_estop)

        self.manufacturer = QComboBox()
        self.manufacturer.addItem("Generic symbols", "")
        from panelcad.library.parts import default_parts_catalog

        for name in default_parts_catalog().manufacturers():
            self.manufacturer.addItem(name, name)
        self.manufacturer.setToolTip(
            "Assign this manufacturer's catalog numbers onto the symbols after insert."
        )
        form.addRow("Preferred manufacturer", self.manufacturer)

        self.secondary_manufacturer = QComboBox()
        self.secondary_manufacturer.addItem("Default (project vendor, else AutomationDirect)", "")
        self.secondary_manufacturer.addItem("None", "none")
        for name in default_parts_catalog().manufacturers():
            self.secondary_manufacturer.addItem(name, name)
        self.secondary_manufacturer.setToolTip(
            "Fill categories the preferred manufacturer does not publish. "
            "Default uses the enclosure maker when that name is a parts brand, "
            "otherwise AutomationDirect. None leaves those symbols generic. "
            "Filled rows are marked as a catalog fallback on the BOM."
        )
        form.addRow("Secondary manufacturer", self.secondary_manufacturer)

        self.drive = QComboBox()
        self.drive.addItem("Generic 1.5 kW, 3-phase", "generic_1k5")
        self.drive.addItem("DURApulse GS20 0.5 hp, 1-phase", "gs20_0p5")
        self.drive.addItem("IronHorse ES1 0.75 hp, 1-phase", "es1_0p75")
        form.addRow("Drive", self.drive)

        self.starter = QComboBox()
        self.starter.addItem("DOL starter", "dol")
        self.starter.addItem("VFD", "vfd")
        form.addRow("PLC starter", self.starter)

        self.branch = QComboBox()
        self.branch.addItem("Circuit breaker", "breaker")
        self.branch.addItem("Disconnect", "disconnect")
        form.addRow("Branch", self.branch)

        self.line_contactor = QCheckBox("Line contactor ahead of the drive")
        form.addRow("", self.line_contactor)

        self.bypass = QCheckBox("Bypass contactor (soft starter)")
        form.addRow("", self.bypass)

        self.timer_seconds = QDoubleSpinBox()
        self.timer_seconds.setRange(0.5, 60.0)
        self.timer_seconds.setDecimals(1)
        self.timer_seconds.setSingleStep(1.0)
        self.timer_seconds.setValue(6.0)
        self.timer_seconds.setSuffix(" s")
        self.timer_seconds.setToolTip("Star-delta on-delay. Typical shop setting is 6 s (2–15 s).")
        form.addRow("Star-delta timer", self.timer_seconds)

        self.overload_placement = QComboBox()
        self.overload_placement.addItem("Windings (0.58 × FLA)", "windings")
        self.overload_placement.addItem("Line (1.0 × FLA)", "line")
        self.overload_placement.setToolTip(
            "Windings is IEC / common wye-delta practice. Line is the usual NEMA alternative."
        )
        form.addRow("Overload placement", self.overload_placement)

        self.hoa = QCheckBox("Hand-Off-Auto selector")
        self.hoa.setChecked(True)
        self.hoa.setToolTip("Hand = local 3-wire. Off = neither. Auto = PLC via interposing CR.")
        form.addRow("", self.hoa)

        self.plc_model = QComboBox()
        self.plc_model.addItem("Project default (compact 16DI/16DO)", "")
        self.plc_model.addItem("NEMA compact 16DI/16DO", "nema.plc.compact_16dio")
        self.plc_model.addItem("IEC compact 16DI/16DO", "iec.plc.compact_16dio")
        form.addRow("PLC model", self.plc_model)

        self.io_run = QLineEdit("I0_0")
        self.io_run.setPlaceholderText("I0_0")
        self.io_run.setToolTip("PLC input for run feedback (M aux).")
        form.addRow("PLC run input", self.io_run)

        self.io_ol = QLineEdit("I0_1")
        self.io_ol.setPlaceholderText("I0_1")
        self.io_ol.setToolTip("PLC input for overload trip.")
        form.addRow("PLC OL input", self.io_ol)

        self.io_auto = QLineEdit("I0_2")
        self.io_auto.setPlaceholderText("I0_2")
        self.io_auto.setToolTip("PLC input for HOA Auto.")
        form.addRow("PLC Auto input", self.io_auto)

        self.io_out = QLineEdit("Q0_0")
        self.io_out.setPlaceholderText("Q0_0")
        self.io_out.setToolTip("PLC output to the interposing CR coil.")
        form.addRow("PLC run output", self.io_out)

        self.control = QComboBox()
        self.control.addItem("Power only", "none")
        self.control.addItem("Local start-stop", "local")
        self.control.setToolTip(
            "Power only draws the power circuit and no start/stop rung. "
            "Choose Local start-stop to add that rung. "
            "You can also draw the control circuit by hand with Wire after the insert."
        )
        form.addRow("Control", self.control)

        self.control_wiring = QComboBox()
        self.control_wiring.addItem("3-wire (start / stop)", "3wire")
        self.control_wiring.addItem("2-wire (maintained)", "2wire")
        form.addRow("Control wiring", self.control_wiring)

        self.pilot_lights = QCheckBox("Pilot lights")
        form.addRow("", self.pilot_lights)

        self.kw = QDoubleSpinBox()
        self.kw.setRange(0.0, 500.0)
        self.kw.setDecimals(2)
        self.kw.setSingleStep(0.5)
        self.kw.setSpecialValueText("Default")
        self.kw.setValue(0.0)
        self.kw.setSuffix(" kW")
        self.kw.setToolTip(
            "Default uses 1.5 kW for a DOL starter, or the selected drive's rating. "
            "Above 4 kW the contactor is 32 A. Above 15 kW the breaker is a 100 A MCCB."
        )
        form.addRow("Motor kW", self.kw)

        self.hp = QDoubleSpinBox()
        self.hp.setRange(0.0, 500.0)
        self.hp.setDecimals(2)
        self.hp.setSingleStep(0.5)
        self.hp.setSpecialValueText("Default")
        self.hp.setValue(0.0)
        self.hp.setSuffix(" hp")
        form.addRow("Motor hp", self.hp)

        self.voltage = QDoubleSpinBox()
        self.voltage.setRange(0.0, 1000.0)
        self.voltage.setDecimals(0)
        self.voltage.setSingleStep(10.0)
        self.voltage.setSpecialValueText("Default")
        self.voltage.setValue(0.0)
        self.voltage.setSuffix(" V")
        self.voltage.setToolTip("Default 480 V NEMA / 400 V IEC. Used with NEC Table 430.250 for FLA.")
        form.addRow("Line voltage", self.voltage)

        self.control_voltage = QDoubleSpinBox()
        self.control_voltage.setRange(0.0, 480.0)
        self.control_voltage.setDecimals(0)
        self.control_voltage.setSingleStep(24.0)
        self.control_voltage.setSpecialValueText("Default")
        self.control_voltage.setValue(0.0)
        self.control_voltage.setSuffix(" V")
        self.control_voltage.setToolTip("24 V uses the PSU. 120 V uses the control transformer.")
        form.addRow("Control voltage", self.control_voltage)

        self.fla = QDoubleSpinBox()
        self.fla.setRange(0.0, 2000.0)
        self.fla.setDecimals(1)
        self.fla.setSingleStep(0.5)
        self.fla.setSpecialValueText("NEC 430.250")
        self.fla.setValue(0.0)
        self.fla.setSuffix(" A")
        form.addRow("Motor FLA", self.fla)

        self.place_layout = QCheckBox("Place parts on the panel and door")
        self.place_layout.setChecked(True)
        self.place_layout.setToolTip(
            "DIN-rail devices go on the backplate. Pushbuttons, selectors, pilots, and the e-stop go on the door."
        )
        form.addRow("", self.place_layout)

        self.separate_control_rail = QCheckBox("Separate control rail")
        self.separate_control_rail.setChecked(False)
        self.separate_control_rail.setToolTip(
            "Timer, relays, the PSU, and the PLC get their own rail. "
            "Leave unchecked to put them on the right of the power rail when they fit."
        )
        form.addRow("", self.separate_control_rail)

        self.new_sheet = QCheckBox("New Motor power sheet")
        form.addRow("", self.new_sheet)
        body_layout.addLayout(form)
        scroll = OptionScroll()
        scroll.setObjectName("circuitBuilderOptions")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Insert")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(buttons)
        self.buttons = buttons

        self.kind.currentIndexChanged.connect(self._sync_enabled)
        self.starter.currentIndexChanged.connect(self._sync_enabled)
        self._sync_enabled()

    def showEvent(self, event) -> None:  # noqa: ANN001, N802
        super().showEvent(event)
        fit_when_shown(self)

    def _kind(self) -> str:
        return str(self.kind.currentData() or "dol")

    def _sync_enabled(self) -> None:
        kind = self._kind()
        vfd = kind == "vfd" or (kind == "plc_motor" and str(self.starter.currentData()) == "vfd")
        star = kind == "star_delta"
        plc = kind == "plc_motor"
        self.drive.setEnabled(vfd)
        self.line_contactor.setEnabled(vfd)
        self.starter.setEnabled(plc)
        self.bypass.setEnabled(kind == "soft_starter")
        self.control_wiring.setEnabled(kind == "soft_starter")
        self.timer_seconds.setEnabled(star)
        self.overload_placement.setEnabled(star)
        self.hoa.setEnabled(plc)
        self.plc_model.setEnabled(plc)
        self.io_run.setEnabled(plc)
        self.io_ol.setEnabled(plc)
        self.io_auto.setEnabled(plc)
        self.io_out.setEnabled(plc)
        extra = kind in {"reversing", "star_delta", "soft_starter", "plc_motor"}
        self.pilot_lights.setEnabled(extra)
        if not vfd:
            self.line_contactor.setChecked(False)
        if kind != "soft_starter":
            self.bypass.setChecked(False)
        if extra and self.control.currentData() == "none":
            idx = self.control.findData("local")
            if idx >= 0:
                self.control.setCurrentIndex(idx)

    def values(self) -> dict[str, object]:
        kind = self._kind()
        kw = float(self.kw.value())
        hp = float(self.hp.value())
        voltage = float(self.voltage.value())
        control_voltage = float(self.control_voltage.value())
        fla = float(self.fla.value())
        vfd = kind == "vfd" or (kind == "plc_motor" and str(self.starter.currentData()) == "vfd")
        plc_model = str(self.plc_model.currentData() or "").strip()
        return {
            "kind": kind,
            "manufacturer": str(self.manufacturer.currentData() or "").strip() or None,
            "secondary_manufacturer": str(self.secondary_manufacturer.currentData() or "").strip() or None,
            "drive": None if not vfd else str(self.drive.currentData()),
            "branch": str(self.branch.currentData()),
            "line_contactor": bool(self.line_contactor.isChecked()) and vfd,
            "control": str(self.control.currentData()),
            "kw": None if kw <= 0.0 else kw,
            "hp": None if hp <= 0.0 else hp,
            "voltage": None if voltage <= 0.0 else voltage,
            "control_voltage": None if control_voltage <= 0.0 else control_voltage,
            "fla": None if fla <= 0.0 else fla,
            "pilot_lights": bool(self.pilot_lights.isChecked())
            and kind in {"reversing", "star_delta", "soft_starter", "plc_motor"},
            "bypass": bool(self.bypass.isChecked()) and kind == "soft_starter",
            "control_wiring": str(self.control_wiring.currentData() or "3wire"),
            "starter": str(self.starter.currentData() or "dol") if kind == "plc_motor" else "dol",
            "timer_seconds": float(self.timer_seconds.value()) if kind == "star_delta" else None,
            "overload_placement": str(self.overload_placement.currentData() or "windings"),
            "hoa": bool(self.hoa.isChecked()) if kind == "plc_motor" else True,
            "plc_model": plc_model or None,
            "io_run": str(self.io_run.text() or "I0_0").strip() or "I0_0",
            "io_ol": str(self.io_ol.text() or "I0_1").strip() or "I0_1",
            "io_auto": str(self.io_auto.text() or "I0_2").strip() or "I0_2",
            "io_out": str(self.io_out.text() or "Q0_0").strip() or "Q0_0",
            "new_sheet": self.new_sheet.isChecked(),
            "place_layout": self.place_layout.isChecked(),
            "shared_supply": self.shared_supply.isChecked(),
            "shared_estop": self.shared_estop.isChecked(),
            "separate_control_rail": self.separate_control_rail.isChecked(),
            "standard": str(self.standard.currentData() or "NEMA/ANSI"),
        }
