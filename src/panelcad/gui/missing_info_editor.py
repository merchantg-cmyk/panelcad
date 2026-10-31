"""Small editor for one missing-info badge. Saves through the shared feedback rules."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session


class MissingInfoInlineEditor(QFrame):
    """Type a value or pick a published library part, then save it onto the project."""

    saved = Signal(dict)

    def __init__(self, session: Session, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self._item_id = ""
        self._field = ""
        self._library_pick: dict[str, Any] | None = None

        self.setObjectName("missingInfoEditor")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "QFrame#missingInfoEditor { background: #1c1917; border: 1px solid #f59e0b; border-radius: 6px; }"
            "QFrame#missingInfoEditor QLabel { color: #fef3c7; }"
            "QFrame#missingInfoEditor QLineEdit, QFrame#missingInfoEditor QComboBox, "
            "QFrame#missingInfoEditor QDoubleSpinBox, QFrame#missingInfoEditor QListWidget {"
            " background: #fffbeb; color: #1c1917; selection-background-color: #f59e0b; }"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        self.title_label = QLabel("Missing info")
        self.title_label.setStyleSheet("font-weight: 700;")
        self.prompt_label = QLabel("")
        self.prompt_label.setWordWrap(True)
        root.addWidget(self.title_label)
        root.addWidget(self.prompt_label)

        self.value_edit = QLineEdit()
        self.value_edit.setObjectName("missingInfoValue")
        self.value_edit.setPlaceholderText("Value from the device")
        self.value_edit.textEdited.connect(self._on_value_edited)
        root.addWidget(self.value_edit)

        dims = QHBoxLayout()
        self.w_spin = QDoubleSpinBox()
        self.h_spin = QDoubleSpinBox()
        self.d_spin = QDoubleSpinBox()
        for spin, name, suffix in (
            (self.w_spin, "missingInfoWidth", " mm W"),
            (self.h_spin, "missingInfoHeight", " mm H"),
            (self.d_spin, "missingInfoDepth", " mm D"),
        ):
            spin.setObjectName(name)
            spin.setRange(0.0, 2000.0)
            spin.setDecimals(1)
            spin.setSpecialValueText("—")
            spin.setSuffix(suffix)
            spin.valueChanged.connect(self._clear_library_pick)
            dims.addWidget(spin)
        self.dim_row = QWidget()
        self.dim_row.setLayout(dims)
        root.addWidget(self.dim_row)

        form = QFormLayout()
        self.mount_combo = QComboBox()
        self.mount_combo.setObjectName("missingInfoMount")
        self.mount_combo.addItem("Choose mounting", "")
        self.mount_combo.addItem("DIN rail", "din_rail")
        self.mount_combo.addItem("Backplate", "backplate")
        self.mount_combo.addItem("Door", "door")
        self.mount_combo.addItem("Field", "field")
        form.addRow("Mounting:", self.mount_combo)
        self.mount_host = QWidget()
        self.mount_host.setLayout(form)
        root.addWidget(self.mount_host)

        wire_form = QFormLayout()
        self.terminal_edit = QLineEdit()
        self.terminal_edit.setObjectName("missingInfoTerminal")
        self.terminal_edit.setPlaceholderText("Terminal on this device")
        self.wire_edit = QLineEdit()
        self.wire_edit.setObjectName("missingInfoWire")
        self.wire_edit.setPlaceholderText("Wire number")
        self.lands_edit = QLineEdit()
        self.lands_edit.setObjectName("missingInfoLands")
        self.lands_edit.setPlaceholderText("Other end only if you can see it, such as PLC1:O0")
        wire_form.addRow("Terminal:", self.terminal_edit)
        wire_form.addRow("Wire number:", self.wire_edit)
        wire_form.addRow("Lands on:", self.lands_edit)
        self.wire_host = QWidget()
        self.wire_host.setLayout(wire_form)
        root.addWidget(self.wire_host)

        self.library_query = QLineEdit()
        self.library_query.setObjectName("missingInfoLibraryQuery")
        self.library_query.setPlaceholderText("Search the parts library")
        self.library_query.textEdited.connect(self._refresh_library)
        root.addWidget(self.library_query)

        self.library_list = QListWidget()
        self.library_list.setObjectName("missingInfoLibrary")
        self.library_list.setMaximumHeight(120)
        self.library_list.itemClicked.connect(self._on_library_clicked)
        root.addWidget(self.library_list)

        self.error_label = QLabel("")
        self.error_label.setObjectName("missingInfoError")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #fecaca;")
        root.addWidget(self.error_label)

        self.save_btn = QPushButton("Save to project")
        self.save_btn.setObjectName("missingInfoSave")
        self.save_btn.clicked.connect(self.save)
        root.addWidget(self.save_btn)
        self.hide()

    @property
    def field(self) -> str:
        return self._field

    @property
    def item_id(self) -> str:
        return self._item_id

    def open_for(self, box: dict[str, Any], field: str) -> None:
        from panelcad.reconstruct.feedback import canonical_missing_field

        self._item_id = str(box.get("item_id") or "")
        self._field = canonical_missing_field(field)
        self._library_pick = None
        badge = next((b for b in box.get("badges") or [] if b.get("field") == self._field), {})
        tag = str(box.get("tag") or self._item_id)
        self.title_label.setText(f"{tag} — {badge.get('label') or self._field}")
        self.prompt_label.setText(str(badge.get("prompt") or ""))
        self.error_label.clear()
        self.library_list.clear()
        self.library_query.clear()
        self._show_field(self._field)
        if self._field == "part_number":
            self.value_edit.setText(str(box.get("part_number") or ""))
        elif self._field == "ratings":
            self.value_edit.setText(str(box.get("ratings") or badge.get("current") or ""))
        elif self._field == "dimensions":
            self.w_spin.setValue(float(box.get("envelope_w_mm") or 0.0))
            self.h_spin.setValue(float(box.get("envelope_h_mm") or 0.0))
            self.d_spin.setValue(float(box.get("envelope_d_mm") or 0.0))
            mount = str(box.get("mount") or "")
            index = self.mount_combo.findData(mount)
            self.mount_combo.setCurrentIndex(index if index >= 0 else 0)
        else:
            self.terminal_edit.setText(str(box.get("terminal") or ""))
            self.wire_edit.setText(str(box.get("wire_number") or ""))
            self.lands_edit.clear()
        self.setVisible(True)

    def _show_field(self, field: str) -> None:
        is_text = field in {"part_number", "ratings"}
        is_dim = field == "dimensions"
        is_wire = field == "terminals_wires"
        self.value_edit.setVisible(is_text)
        self.dim_row.setVisible(is_dim)
        self.mount_host.setVisible(is_dim)
        self.wire_host.setVisible(is_wire)
        self.library_query.setVisible(field in {"dimensions", "ratings"})
        self.library_list.setVisible(field != "terminals_wires")
        if field == "part_number":
            self.value_edit.setPlaceholderText("Catalog number from the device")
        elif field == "ratings":
            self.value_edit.setPlaceholderText("Rating printed on the device")

    def _clear_library_pick(self, *_args: Any) -> None:
        self._library_pick = None

    def _on_value_edited(self, text: str) -> None:
        if self._library_pick and text.strip() != str(self._library_pick.get("catalog") or ""):
            self._library_pick = None
        if self._field == "part_number":
            self._refresh_library(text)

    def _refresh_library(self, query: str) -> None:
        from panelcad.reconstruct.feedback import library_choices

        self.library_list.clear()
        rows = library_choices(self.session, query, limit=8)
        if not rows and len(str(query or "").strip()) >= 2:
            empty = QListWidgetItem("No published parts match.")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.library_list.addItem(empty)
            return
        for row in rows:
            bits = [str(row.get("manufacturer") or ""), str(row.get("catalog") or "")]
            if row.get("ratings"):
                bits.append(str(row["ratings"]))
            if row.get("width_mm") and row.get("height_mm") and row.get("depth_mm"):
                bits.append(
                    f"{float(row['width_mm']):g}×{float(row['height_mm']):g}×{float(row['depth_mm']):g} mm"
                )
            item = QListWidgetItem("  ".join(bit for bit in bits if bit))
            item.setData(Qt.ItemDataRole.UserRole, row)
            self.library_list.addItem(item)

    def _on_library_clicked(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        if not isinstance(data, dict) or not data.get("catalog"):
            return
        if self._field == "ratings":
            ratings = str(data.get("ratings") or "").strip()
            if not ratings:
                self._library_pick = None
                self.error_label.setText("That library part has no published rating.")
                return
            self.error_label.clear()
            self.value_edit.setText(ratings)
            self._library_pick = None
            return
        if self._field == "dimensions":
            if not (data.get("width_mm") and data.get("height_mm") and data.get("depth_mm")):
                self._library_pick = None
                self.error_label.setText("That library part has no published width, height, and depth.")
                return
            self._library_pick = data
            self.error_label.setText(
                f"Save will use the published part {data.get('manufacturer')} {data.get('catalog')} "
                f"({float(data['width_mm']):g}×{float(data['height_mm']):g}×{float(data['depth_mm']):g} mm)."
            )
            return
        self._library_pick = data
        self.value_edit.blockSignals(True)
        self.value_edit.setText(str(data.get("catalog") or ""))
        self.value_edit.blockSignals(False)
        self.error_label.clear()

    def save(self) -> None:
        if not self._item_id or not self._field:
            return
        field = self._field
        kwargs: dict[str, Any]
        if self._library_pick and field in {"part_number", "dimensions"}:
            field = "part_number"
            kwargs = {
                "catalog": str(self._library_pick.get("catalog") or ""),
                "manufacturer": str(self._library_pick.get("manufacturer") or ""),
                "library_id": str(self._library_pick.get("library_id") or ""),
            }
        elif field == "part_number":
            kwargs = {"value": self.value_edit.text()}
        elif field == "ratings":
            kwargs = {"value": self.value_edit.text()}
        elif field == "dimensions":
            kwargs = {
                "width_mm": self.w_spin.value() or None,
                "height_mm": self.h_spin.value() or None,
                "depth_mm": self.d_spin.value() or None,
                "mount": str(self.mount_combo.currentData() or ""),
            }
        else:
            kwargs = {
                "terminal": self.terminal_edit.text(),
                "wire_number": self.wire_edit.text(),
                "to_ref": self.lands_edit.text(),
            }
        try:
            result = self.session.reconstruct_apply_missing_info(self._item_id, field, **kwargs)
        except (ValueError, KeyError) as exc:
            self.error_label.setText(str(exc))
            return
        self.error_label.clear()
        self.saved.emit(result)
