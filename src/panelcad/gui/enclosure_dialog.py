"""Tools → Size Enclosure — recommendation, heat, arrangement preview, Apply."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from panelcad.gui.screen_fit import fit_when_shown


class ArrangementPreview(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict[str, Any]] = []
        self._box: tuple[float, float] = (400.0, 500.0)
        self.setMinimumSize(280, 220)
        self.setAutoFillBackground(True)

    def set_arrangement(self, width_mm: float, height_mm: float, items: list[dict[str, Any]]) -> None:
        self._box = (max(1.0, width_mm), max(1.0, height_mm))
        self._items = list(items)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        from panelcad.gui.theme import canvas_qcolor

        painter.fillRect(self.rect(), canvas_qcolor("enclosure_paper"))
        bw, bh = self._box
        margin = 12.0
        avail_w = max(20.0, self.width() - 2 * margin)
        avail_h = max(20.0, self.height() - 2 * margin)
        scale = min(avail_w / bw, avail_h / bh)
        ox = margin + (avail_w - bw * scale) / 2.0
        oy = margin + (avail_h - bh * scale) / 2.0
        painter.setPen(QPen(canvas_qcolor("enclosure_wall"), 2))
        painter.setBrush(canvas_qcolor("enclosure_wall_fill"))
        painter.drawRect(QRectF(ox, oy, bw * scale, bh * scale))
        colors = {
            "duct": canvas_qcolor("enclosure_duct"),
            "rail": canvas_qcolor("enclosure_rail"),
            "device": canvas_qcolor("enclosure_device"),
            "block": canvas_qcolor("enclosure_block"),
            "door": canvas_qcolor("enclosure_door"),
        }
        font = QFont(self.font())
        font.setPointSize(7)
        painter.setFont(font)
        for item in self._items:
            kind = str(item.get("kind") or "device")
            x = ox + float(item.get("x") or 0) * scale
            y = oy + float(item.get("y") or 0) * scale
            w = max(1.0, float(item.get("w") or 1) * scale)
            h = max(1.0, float(item.get("h") or 1) * scale)
            edge = "enclosure_duct_edge" if kind == "duct" else "enclosure_label"
            painter.setPen(QPen(canvas_qcolor(edge), 1))
            painter.setBrush(colors.get(kind, canvas_qcolor("enclosure_other")))
            painter.drawRect(QRectF(x, y, w, h))
            label = str(item.get("label") or item.get("ref") or "")
            if label and kind in {"device", "block", "door"} and w > 18 and h > 10:
                painter.setPen(canvas_qcolor("enclosure_ink"))
                painter.drawText(QRectF(x + 1, y + 1, w - 2, h - 2), Qt.AlignmentFlag.AlignCenter, label)
        painter.end()


class SizeEnclosureDialog(QDialog):
    def __init__(self, session: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.recommendation: dict[str, Any] = {}
        self.applied = False
        self.setWindowTitle("Size Enclosure")
        self.setObjectName("sizeEnclosureDialog")
        self.resize(860, 640)
        # fit_when_shown owns the work area. The theme clamp must not restack Apply.
        self.setProperty("_pc_own_fit", True)

        layout = QVBoxLayout(self)
        title = QLabel("Size Enclosure")
        font = title.font()
        font.setPointSize(12)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        self.headline = QLabel("Reading parts…")
        self.headline.setWordWrap(True)
        layout.addWidget(self.headline)

        body = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        form = QFormLayout(left)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.nema = QComboBox()
        for value, label in (("12", "NEMA 12 (indoor shop default)"), ("1", "NEMA 1"), ("4", "NEMA 4"), ("4X", "NEMA 4X")):
            self.nema.addItem(label, value)
        form.addRow("Rating", self.nema)
        self.material = QComboBox()
        self.material.addItem("Painted steel", "steel")
        self.material.addItem("Stainless", "stainless")
        form.addRow("Material", self.material)
        self.spare = QDoubleSpinBox()
        self.spare.setRange(0.0, 100.0)
        self.spare.setSingleStep(5.0)
        self.spare.setDecimals(0)
        self.spare.setValue(25)
        self.spare.setSuffix(" %")
        self.spare.setToolTip("Minimum leftover back-panel height, as a percent of the packed height.")
        form.addRow("Spare space", self.spare)
        from panelcad.core.enclosure import FILL_TARGET_MAX, FILL_TARGET_MIN

        self.fill_target = QDoubleSpinBox()
        self.fill_target.setRange(FILL_TARGET_MIN * 100.0, FILL_TARGET_MAX * 100.0)
        self.fill_target.setSingleStep(5.0)
        self.fill_target.setDecimals(0)
        self.fill_target.setValue(65)
        self.fill_target.setSuffix(" %")
        self.fill_target.setToolTip(
            "Aim for this share of the back-panel area (default 60–70%). "
            "A tighter fit steps up to the next real catalog size."
        )
        form.addRow("Fill target", self.fill_target)
        self.duct_face = QDoubleSpinBox()
        self.duct_face.setRange(0.0, 6.0)
        self.duct_face.setSingleStep(0.5)
        self.duct_face.setDecimals(1)
        self.duct_face.setValue(2.0)
        self.duct_face.setSuffix(" in")
        self.duct_face.setToolTip("Wire-duct face on the back panel.")
        form.addRow("Wire duct", self.duct_face)
        self.rail_clear = QDoubleSpinBox()
        self.rail_clear.setRange(0.0, 4.0)
        self.rail_clear.setSingleStep(0.25)
        self.rail_clear.setDecimals(2)
        self.rail_clear.setValue(1.0)
        self.rail_clear.setSuffix(" in")
        self.rail_clear.setToolTip("Clearance from a DIN rail to the wire duct.")
        form.addRow("Rail to duct", self.rail_clear)
        self.ambient = QDoubleSpinBox()
        self.ambient.setRange(-20.0, 80.0)
        self.ambient.setSuffix(" °C")
        self.ambient.setValue(40.0)
        form.addRow("Ambient", self.ambient)
        self.tmax = QDoubleSpinBox()
        self.tmax.setRange(0.0, 120.0)
        self.tmax.setSuffix(" °C")
        self.tmax.setValue(50.0)
        form.addRow("Max internal", self.tmax)
        self.place = QCheckBox("Place parts in the proposed arrangement")
        form.addRow("", self.place)
        self.separate_control = QCheckBox("Separate control rail")
        self.separate_control.setToolTip(
            "Timer, relays, the PSU, and the PLC get their own rail. "
            "Off: they sit on the right of the power rail when they fit."
        )
        form.addRow("", self.separate_control)
        self.reasons = QPlainTextEdit()
        self.reasons.setReadOnly(True)
        self.reasons.setObjectName("sizeEnclosureReasons")
        form.addRow(self.reasons)
        body.addWidget(left)

        right = QWidget()
        right_l = QVBoxLayout(right)
        self.heat = QLabel("Heat")
        self.heat.setWordWrap(True)
        self.heat.setObjectName("sizeEnclosureHeat")
        right_l.addWidget(self.heat)
        self.preview = ArrangementPreview()
        self.preview.setObjectName("sizeEnclosurePreview")
        right_l.addWidget(self.preview, 1)
        body.addWidget(right)
        body.setStretchFactor(0, 1)
        body.setStretchFactor(1, 1)
        layout.addWidget(body, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Close
            | QDialogButtonBox.StandardButton.Reset
        )
        buttons.button(QDialogButtonBox.StandardButton.Reset).setText("Recalculate")
        buttons.button(QDialogButtonBox.StandardButton.Apply).setText("Apply")
        buttons.clicked.connect(self._clicked)
        buttons.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(buttons)
        self._buttons = buttons

        stored = session.project.properties.get("enclosure_sizing") or {}
        if isinstance(stored, dict):
            if stored.get("nema"):
                idx = self.nema.findData(str(stored["nema"]))
                if idx >= 0:
                    self.nema.setCurrentIndex(idx)
            if stored.get("material"):
                idx = self.material.findData(str(stored["material"]))
                if idx >= 0:
                    self.material.setCurrentIndex(idx)
            if stored.get("spare_pct") is not None:
                raw = float(stored["spare_pct"])
                self.spare.setValue(raw * 100.0 if raw <= 1.0 else raw)
            if stored.get("fill_target") is not None:
                raw = float(stored["fill_target"])
                self.fill_target.setValue(raw * 100.0 if raw <= 1.5 else raw)
            if stored.get("duct_face_in") is not None:
                self.duct_face.setValue(float(stored["duct_face_in"]))
            if stored.get("rail_to_duct_in") is not None:
                self.rail_clear.setValue(float(stored["rail_to_duct_in"]))
            if stored.get("ambient_c") is not None:
                self.ambient.setValue(float(stored["ambient_c"]))
            if stored.get("max_internal_c") is not None:
                self.tmax.setValue(float(stored["max_internal_c"]))
            self.separate_control.setChecked(bool(stored.get("separate_control_rail")))
        self.nema.currentIndexChanged.connect(self.recalculate)
        self.separate_control.toggled.connect(self.recalculate)
        self.material.currentIndexChanged.connect(self.recalculate)
        self.spare.valueChanged.connect(self.recalculate)
        self.fill_target.valueChanged.connect(self.recalculate)
        self.duct_face.valueChanged.connect(self.recalculate)
        self.rail_clear.valueChanged.connect(self.recalculate)
        self.ambient.valueChanged.connect(self.recalculate)
        self.tmax.valueChanged.connect(self.recalculate)
        self.recalculate()

    def showEvent(self, event) -> None:  # noqa: ANN001, N802
        super().showEvent(event)
        fit_when_shown(self)

    def options(self) -> dict[str, Any]:
        return {
            "nema": str(self.nema.currentData()),
            "material": str(self.material.currentData()),
            "spare_pct": float(self.spare.value()) / 100.0,
            "fill_target": float(self.fill_target.value()) / 100.0,
            "duct_face_in": float(self.duct_face.value()),
            "rail_to_duct_in": float(self.rail_clear.value()),
            "ambient_c": float(self.ambient.value()),
            "max_internal_c": float(self.tmax.value()),
            "separate_control_rail": self.separate_control.isChecked(),
        }

    def recalculate(self) -> None:
        self.recommendation = self.session.recommend_enclosure(**self.options())
        self._show(self.recommendation)

    def _show(self, rec: dict[str, Any]) -> None:
        box = rec.get("enclosure") or {}
        heat = rec.get("heat") or {}
        fit = "fits" if rec.get("fit") else "does not fit the catalog"
        self.headline.setText(f"{box.get('name') or 'No standard box'} — {fit}")
        lines = list(rec.get("reasons") or [])
        lines.extend(rec.get("warnings") or [])
        self.reasons.setPlainText("\n".join(lines))
        rise = heat.get("rise_k")
        cool = heat.get("cooling") or "none"
        self.heat.setText(
            f"Heat: {heat.get('watts_known', 0)} W known, {heat.get('unknown_count', 0)} unknown. "
            f"ΔT {rise if rise is not None else '—'} K "
            f"(k={heat.get('k_w_m2k')} W/m²·K, A={heat.get('surface_m2')} m²). "
            f"Ambient {heat.get('ambient_c')} °C, limit {heat.get('max_internal_c')} °C, "
            f"{'OK' if heat.get('ok') else 'OVER LIMIT'}. Cooling: {cool.replace('_', ' ')}."
        )
        arr = rec.get("arrangement") or {}
        self.preview.set_arrangement(
            float(arr.get("width_mm") or box.get("width_mm") or 400),
            float(arr.get("height_mm") or box.get("height_mm") or 500),
            list(arr.get("items") or []),
        )

    def apply_recommendation(self) -> dict[str, Any]:
        rec = self.session.apply_enclosure(
            place_parts=self.place.isChecked(),
            recommendation=self.recommendation,
            **self.options(),
        )
        self.recommendation = rec
        self.applied = True
        self._show(rec)
        return rec

    def _clicked(self, button) -> None:  # noqa: ANN001
        role = self._buttons.buttonRole(button)
        if role == QDialogButtonBox.ButtonRole.ApplyRole:
            self.apply_recommendation()
        elif role == QDialogButtonBox.ButtonRole.ResetRole:
            self.recalculate()
        else:
            self.accept() if self.applied else self.reject()
