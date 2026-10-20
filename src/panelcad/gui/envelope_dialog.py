"""Confirm a measured panel envelope from the Inspector or Problems dock."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.library.panel import (
    LAYOUT_READY_FIX,
    MIN_ENVELOPE_MM,
    effective_footprint,
    is_layout_ready,
    max_confirmed_envelope_mm,
)

MOUNTS = ("din_rail", "backplate", "door", "field", "none")


class EnvelopeDialog(QDialog):
    def __init__(self, session: Session, ref: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.ref = ref
        self.setWindowTitle(f"Confirm envelope — {ref}")
        layout = QVBoxLayout(self)
        note = QLabel(
            "Enter the real body size for this part. Confirming stores those "
            "millimetres on the component. Do not guess. "
            + LAYOUT_READY_FIX
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.width = QDoubleSpinBox()
        self.height = QDoubleSpinBox()
        self.depth = QDoubleSpinBox()
        limit = max(1.0, max_confirmed_envelope_mm())
        for spin in (self.width, self.height, self.depth):
            spin.setRange(0.0, limit)
            spin.setDecimals(2)
            spin.setSingleStep(1.0)
            spin.setSuffix(" mm")
            spin.setValue(0.0)
            spin.setSpecialValueText("enter mm")
            # 0 is the empty prompt. Confirm rejects anything under 1 mm.
        self.mount = QComboBox()
        for name in MOUNTS:
            self.mount.addItem(name, name)
        self.who = QLineEdit()
        form.addRow("Width", self.width)
        form.addRow("Height", self.height)
        form.addRow("Depth", self.depth)
        form.addRow("Mounting", self.mount)
        form.addRow("Confirmed by", self.who)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Confirm envelope")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._load()

    def _load(self) -> None:
        comp = None
        for sheet in self.session.project.sheets:
            try:
                comp = sheet.component(self.ref)
                break
            except KeyError:
                continue
        if comp is None:
            return
        device = self.session.catalog.get(comp.library_id)
        # Never pre-fill a generic table size. Only show catalog or already-confirmed mm.
        if is_layout_ready(device, comp):
            ready = effective_footprint(device, comp)
            if ready.width_mm > 0:
                self.width.setValue(float(ready.width_mm))
            if ready.height_mm > 0:
                self.height.setValue(float(ready.height_mm))
            if ready.depth_mm > 0:
                self.depth.setValue(float(ready.depth_mm))
            idx = self.mount.findData(ready.mount)
            if idx >= 0:
                self.mount.setCurrentIndex(idx)
        who = str(comp.properties.get("envelope_confirmed_by") or "")
        if not who:
            who = str(self.session.project.title_block.drawn_by or "")
        self.who.setText(who)

    def apply(self) -> dict:
        from panelcad.library.panel import require_explicit_envelope

        width, height, depth, mount = require_explicit_envelope(
            self.width.value(),
            self.height.value(),
            self.depth.value(),
            self.mount.currentData() or "din_rail",
        )
        if width < MIN_ENVELOPE_MM or height < MIN_ENVELOPE_MM or depth < MIN_ENVELOPE_MM:
            raise ValueError("confirmed envelope needs width_mm, height_mm, and depth_mm of at least 1 mm")
        return self.session.confirm_envelope(
            self.ref,
            width_mm=width,
            height_mm=height,
            depth_mm=depth,
            mount=mount,
            confirmed_by=self.who.text().strip() or "user",
        )


def confirm_envelope_dialog(parent: QWidget, session: Session, ref: str) -> dict | None:
    dialog = EnvelopeDialog(session, ref, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.apply()
