"""Review needs-info parts. Proposals stay proposals until Accept."""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.needs_info import (
    REQUIRED_FIELDS,
    PROPOSALS_KEY,
    UNITS_UNSPECIFIED,
    accept_lookup,
    accept_manual,
    find_component,
    import_guidance_text,
    lookup_all,
    lookup_part,
    lookup_status,
    needs_info_components,
    resolve_with_catalog,
    set_lookup_note,
    update_draft,
)

_LABELS = {
    "width_mm": "Width mm",
    "height_mm": "Height mm",
    "depth_mm": "Depth mm",
    "mount": "Mounting",
    "category": "Category",
}


class NeedsInfoImportDialog(QDialog):
    """After a vendor import, name the parts that still need info."""

    def __init__(self, session: Session, result: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.choice = ""
        self.ref = ""
        self.setObjectName("needsInfoImport")
        self.setWindowTitle("Parts that need info")
        self.resize(720, 480)
        layout = QVBoxLayout(self)
        self.message = QLabel(import_guidance_text(session, result))
        self.message.setObjectName("needsInfoImportMessage")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.part = QComboBox()
        self.part.setObjectName("needsInfoPart")
        for comp in needs_info_components(session.project):
            props = comp.properties or {}
            label = " ".join(
                bit
                for bit in (
                    comp.id,
                    str(props.get("manufacturer") or ""),
                    str(props.get("part_number") or ""),
                )
                if bit
            )
            self.part.addItem(label, comp.id)
        if self.part.count():
            layout.addWidget(self.part)
        row = QHBoxLayout()
        self.show_bom = _button(row, "Show in BOM", "showInBom", self._choose("bom"))
        self.open_problems = _button(row, "Open Problems", "openProblems", self._choose("problems"))
        self.match = _button(row, "Match to catalog part…", "matchCatalog", self._choose("match"))
        layout.addLayout(row)
        row2 = QHBoxLayout()
        self.lookup_all = _button(row2, "Look up all", "lookupAll", self._choose("lookup_all"))
        self.manual = _button(
            row2,
            "Enter dimensions from the datasheet…",
            "enterDatasheet",
            self._choose("manual"),
        )
        self.close_btn = _button(row2, "Close", "closeNeedsInfo", self.reject)
        layout.addLayout(row2)

    def _choose(self, choice: str):
        def pick() -> None:
            self.choice = choice
            data = self.part.currentData() if self.part.count() else ""
            self.ref = str(data or "")
            self.accept()

        return pick


class LookupUnavailableDialog(QDialog):
    """AI or web access is off. Offer the datasheet form instead of a guess."""

    def __init__(self, reason: str, *, can_enter: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.choice = ""
        self.setWindowTitle("AI lookup")
        self.setObjectName("lookupUnavailable")
        layout = QVBoxLayout(self)
        self.reason = QLabel(reason)
        self.reason.setObjectName("lookupOffReason")
        self.reason.setWordWrap(True)
        layout.addWidget(self.reason)
        buttons = QHBoxLayout()
        self.enter = QPushButton("Enter dimensions from the datasheet")
        self.enter.setObjectName("enterDatasheet")
        self.enter.setEnabled(can_enter)
        if not can_enter:
            self.enter.setToolTip("Select a needs-info row first.")
        self.enter.clicked.connect(self._enter)
        buttons.addWidget(self.enter)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def _enter(self) -> None:
        self.choice = "manual"
        self.accept()


class LookupReviewDialog(QDialog):
    """Show one part's proposal. Accept is the only way those values are stored."""

    def __init__(
        self,
        session: Session,
        ref: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.ref = ref
        self.choice = ""
        self.confirmations: dict[str, bool] = {}
        self.setWindowTitle(f"Datasheet proposal — {ref}")
        self.setObjectName("lookupReview")
        self.resize(860, 380)
        layout = QVBoxLayout(self)
        _sheet, comp = find_component(session, ref)
        proposals = []
        if comp is not None:
            proposals = [item for item in list(comp.properties.get(PROPOSALS_KEY) or []) if isinstance(item, dict)]
        self._by_field = {str(item.get("field") or ""): item for item in proposals}
        note = QLabel(
            "These are proposals. A field is cited only when its datasheet URL "
            "returns that value. Tick I checked the datasheet to accept a value "
            "PanelCAD could not verify. A dimension with no unit stays unaccepted "
            "until you confirm the units. Nothing is written until you click Accept."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = QTableWidget(len(REQUIRED_FIELDS), 6)
        self.table.setHorizontalHeaderLabels(
            ["Field", "Value", "Source", "Page or table", "Confidence", "Checked"]
        )
        self._checks: dict[str, QCheckBox] = {}
        for row, field in enumerate(REQUIRED_FIELDS):
            item = self._by_field.get(field) or {}
            value = str(item.get("value") or "")
            source = str(item.get("source") or "")
            status = str(item.get("status") or "")
            reason = str(item.get("reason") or "")
            unitless = str(item.get("units") or "") == UNITS_UNSPECIFIED
            if not value or not source:
                value = value or "not found — left empty"
            elif unitless:
                value = f"{value} — units not stated"
            confidence = str(item.get("confidence") or "")
            notes: list[str] = []
            if status == "unverified" and reason:
                notes.append(f"unverified: {reason}")
            elif status == "cited":
                notes.append("cited")
            if unitless:
                notes.append("confirm the units")
            if notes:
                confidence = f"{confidence} — {'; '.join(notes)}".strip(" —")
            cells = [
                _LABELS.get(field, field),
                value,
                source,
                str(item.get("page") or ""),
                confidence,
            ]
            for col, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                self.table.setItem(row, col, cell)
            box = QCheckBox("I checked the datasheet")
            box.setObjectName(f"checkedDatasheet_{field}")
            if unitless and status == "cited" and source:
                box.setText("Confirm mm")
                self._checks[field] = box
                box.toggled.connect(self._refresh_accept)
            elif status == "cited" and value and source and not unitless:
                box.setEnabled(False)
                box.setToolTip("This value was found on the datasheet page.")
            elif str(item.get("value") or "") and source:
                self._checks[field] = box
                box.toggled.connect(self._refresh_accept)
            else:
                box.setEnabled(False)
            self.table.setCellWidget(row, 5, box)
        self.table.resizeColumnsToContents()
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        self.accept_btn = QPushButton("Accept")
        self.accept_btn.setObjectName("acceptLookup")
        self.accept_btn.clicked.connect(self._accept)
        self.manual = QPushButton("Enter dimensions from the datasheet")
        self.manual.setObjectName("enterDatasheet")
        self.manual.clicked.connect(self._manual)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(self.accept_btn)
        buttons.addWidget(self.manual)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._refresh_accept()

    def _refresh_accept(self, *_args: object) -> None:
        ready = self._ready()
        self.accept_btn.setEnabled(ready)
        if ready:
            self.accept_btn.setToolTip("")
        else:
            self.accept_btn.setToolTip(
                "Accept needs a cited value, or tick I checked the datasheet for each "
                "unverified field. Confirm the units on a dimension that does not "
                "state them. Empty fields stay empty."
            )

    def _ready(self) -> bool:
        for field in REQUIRED_FIELDS:
            item = self._by_field.get(field) or {}
            value = str(item.get("value") or "")
            source = str(item.get("source") or "")
            if not value or not source:
                return False
            unitless = str(item.get("units") or "") == UNITS_UNSPECIFIED
            if str(item.get("status") or "") == "cited" and not unitless:
                continue
            box = self._checks.get(field)
            if box is None or not box.isChecked():
                return False
        return True

    def _accept(self) -> None:
        if not self._ready():
            return
        self.confirmations = {field: box.isChecked() for field, box in self._checks.items()}
        self.choice = "accept"
        self.accept()

    def _manual(self) -> None:
        self.choice = "manual"
        self.accept()


class LookupAllDialog(QDialog):
    """Review every stored proposal. Each Accept applies that part only."""

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.applied: list[str] = []
        self.setWindowTitle("Datasheet proposals")
        self.setObjectName("lookupAllReview")
        self.resize(760, 480)
        layout = QVBoxLayout(self)
        note = QLabel(
            "AI lookup only proposes. Click Accept on a part to make it a library part. "
            "A part with an empty field stays needs-info."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self._rows = QVBoxLayout()
        layout.addLayout(self._rows)
        self._rebuild()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close)

    def _rebuild(self) -> None:
        while self._rows.count():
            item = self._rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
        for comp in needs_info_components(self.session.project):
            props = comp.properties or {}
            proposals = [item for item in list(props.get(PROPOSALS_KEY) or []) if isinstance(item, dict)]
            by_field = {str(item.get("field") or ""): item for item in proposals}
            bits = []
            complete = True
            for field in REQUIRED_FIELDS:
                item = by_field.get(field)
                if not item or not str(item.get("value") or "") or not str(item.get("source") or ""):
                    complete = False
                    bits.append(f"{_LABELS[field]}: empty")
                    continue
                if str(item.get("units") or "") == UNITS_UNSPECIFIED:
                    complete = False
                    extra = ""
                    if str(item.get("status") or "") != "cited":
                        extra = f"; unverified ({item.get('reason') or 'unverified'})"
                    bits.append(f"{_LABELS[field]}: {item['value']} — confirm the units{extra}")
                    continue
                if str(item.get("status") or "") != "cited":
                    complete = False
                    reason = str(item.get("reason") or "unverified")
                    bits.append(f"{_LABELS[field]}: unverified ({reason})")
                    continue
                page = str(item.get("page") or "")
                where = f" ({page})" if page else ""
                bits.append(
                    f"{_LABELS[field]}: {item['value']}{where} "
                    f"[{item.get('confidence') or 'unspecified'}] {item['source']}"
                )
            title = " ".join(
                bit
                for bit in (
                    comp.id,
                    str(props.get("manufacturer") or ""),
                    str(props.get("part_number") or ""),
                )
                if bit
            )
            label = QLabel(title + "\n" + "\n".join(bits))
            label.setWordWrap(True)
            self._rows.addWidget(label)
            button = QPushButton(f"Accept {comp.id}")
            button.setEnabled(complete)
            if not complete:
                button.setToolTip(
                    "Still needs info. Open the part and tick I checked the datasheet "
                    "for each unverified value. Confirm the units when a dimension "
                    "has none. Empty fields were not guessed."
                )
            button.clicked.connect(lambda _checked=False, ref=comp.id: self._accept_one(ref))
            self._rows.addWidget(button)

    def _accept_one(self, ref: str) -> None:
        result = accept_lookup(self.session, ref)
        if result.get("applied"):
            self.applied.append(ref)
        self._rebuild()


class DatasheetEntryDialog(QDialog):
    """Type datasheet values. Empty spins are not stored as 0 mm."""

    def __init__(self, session: Session, ref: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.ref = ref
        self.setWindowTitle(f"Datasheet dimensions — {ref}")
        self.setObjectName("datasheetEntry")
        layout = QVBoxLayout(self)
        note = QLabel(
            "Enter the size printed on the datasheet. Leave a box empty when you do not "
            "have that number. An empty field is not filled in."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.width = _spin()
        self.height = _spin()
        self.depth = _spin()
        self.mount = QComboBox()
        self.mount.addItem("choose", "")
        self.mount.addItem("DIN rail", "din_rail")
        self.mount.addItem("Panel", "panel")
        self.category = QLineEdit()
        self.source = QLineEdit()
        self.source.setPlaceholderText("https:// datasheet URL, if you have it")
        self.note = QPlainTextEdit()
        self.note.setPlaceholderText("What's missing / where I looked")
        self.note.setFixedHeight(72)
        form.addRow("Width", self.width)
        form.addRow("Height", self.height)
        form.addRow("Depth", self.depth)
        form.addRow("Mounting", self.mount)
        form.addRow("Category", self.category)
        form.addRow("Datasheet URL", self.source)
        form.addRow("What's missing / where I looked", self.note)
        layout.addLayout(form)
        buttons = QDialogButtonBox()
        self.save_partial = buttons.addButton("Save partial", QDialogButtonBox.ButtonRole.ActionRole)
        self.accept_btn = buttons.addButton("Accept", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.save_partial.clicked.connect(self._save_partial)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._load()

    def _load(self) -> None:
        _sheet, comp = find_component(self.session, self.ref)
        if comp is None:
            return
        draft = dict((comp.properties or {}).get("needs_info_draft") or {})
        _set_spin(self.width, draft.get("width_mm"))
        _set_spin(self.height, draft.get("height_mm"))
        _set_spin(self.depth, draft.get("depth_mm"))
        mount = str(draft.get("mount") or "")
        index = self.mount.findData(mount)
        if index >= 0:
            self.mount.setCurrentIndex(index)
        self.category.setText(str(draft.get("category") or ""))
        self.source.setText(str(draft.get("source") or ""))
        self.note.setPlainText(str((comp.properties or {}).get("lookup_note") or ""))

    def _fields(self) -> dict[str, str]:
        return {
            "width_mm": _spin_text(self.width),
            "height_mm": _spin_text(self.height),
            "depth_mm": _spin_text(self.depth),
            "mount": str(self.mount.currentData() or ""),
            "category": self.category.text().strip(),
            "source": self.source.text().strip(),
        }

    def _save_partial(self) -> None:
        _sheet, comp = find_component(self.session, self.ref)
        if comp is None:
            self.reject()
            return
        with self.session.command("needs info"):
            update_draft(comp, self._fields(), note=self.note.toPlainText())
        self.accept()

    def _accept(self) -> None:
        _sheet, comp = find_component(self.session, self.ref)
        if comp is None:
            self.reject()
            return
        with self.session.command("needs info"):
            update_draft(comp, self._fields(), note=self.note.toPlainText())
            result = accept_manual(self.session, self.ref)
        if not result.get("applied"):
            missing = ", ".join(result.get("missing") or [])
            self.note.setPlaceholderText(
                "Still needs info"
                + (f" ({missing})" if missing else "")
                + ". Empty fields were not filled."
            )
            return
        self.accept()


class LookupNoteDialog(QDialog):
    def __init__(self, session: Session, ref: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.ref = ref
        self.setWindowTitle(f"What's missing — {ref}")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("What's missing / where I looked"))
        self.note = QPlainTextEdit()
        _sheet, comp = find_component(session, ref)
        if comp is not None:
            self.note.setPlainText(str((comp.properties or {}).get("lookup_note") or ""))
        layout.addWidget(self.note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self) -> None:
        _sheet, comp = find_component(self.session, self.ref)
        if comp is not None:
            with self.session.command("lookup note"):
                set_lookup_note(comp, self.note.toPlainText())
        self.accept()


def run_needs_action(parent: QWidget, session: Session, action: str, ref: str = "") -> str:
    """One resolve step from the BOM or the import dialog. Returns a status line."""

    if action == "lookup_all":
        return _lookup_all(parent, session)
    if action == "lookup":
        return _lookup_one(parent, session, ref)
    if action == "manual":
        return _manual(parent, session, ref)
    if action == "match":
        return _match(parent, session, ref)
    if action == "note":
        dialog = LookupNoteDialog(session, ref, parent)
        dialog.exec()
        return "note saved"
    return ""


def _lookup_all(parent: QWidget, session: Session) -> str:
    status = lookup_status()
    if not status.get("available"):
        return _offer_manual(parent, session, "", str(status.get("reason") or ""))
    client = _live_client()
    if client is None:
        from panelcad.ai.config import CONNECT_REASON

        return _offer_manual(parent, session, "", CONNECT_REASON)
    lookup_all(session, client)
    dialog = LookupAllDialog(session, parent)
    dialog.exec()
    if dialog.applied:
        return f"accepted {', '.join(dialog.applied)}"
    return "datasheet proposals stored — nothing applied yet"


def _lookup_one(parent: QWidget, session: Session, ref: str) -> str:
    status = lookup_status()
    if not status.get("available"):
        return _offer_manual(parent, session, ref, str(status.get("reason") or ""))
    client = _live_client()
    if client is None:
        from panelcad.ai.config import CONNECT_REASON

        return _offer_manual(parent, session, ref, CONNECT_REASON)
    result = lookup_part(session, ref, client)
    if not result.get("ok"):
        return _offer_manual(parent, session, ref, str(result.get("reason") or "Lookup failed."))
    dialog = LookupReviewDialog(session, ref, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return "proposal stored — not applied"
    if dialog.choice == "manual":
        return _manual(parent, session, ref)
    accepted = accept_lookup(session, ref, dialog.confirmations)
    if accepted.get("applied"):
        return f"accepted {ref}"
    return str(accepted.get("reason") or "not applied")


def _offer_manual(parent: QWidget, session: Session, ref: str, reason: str) -> str:
    dialog = LookupUnavailableDialog(reason, can_enter=bool(ref), parent=parent)
    if dialog.exec() != QDialog.DialogCode.Accepted or dialog.choice != "manual":
        return reason
    if not ref:
        return reason
    return _manual(parent, session, ref)


def _manual(parent: QWidget, session: Session, ref: str) -> str:
    if not ref:
        return "Select a needs-info row to enter dimensions."
    dialog = DatasheetEntryDialog(session, ref, parent)
    dialog.exec()
    _sheet, comp = find_component(session, ref)
    if comp is None:
        return f"{ref} resolved"
    from panelcad.reconstruct.placeholder import is_needs_info_component

    if is_needs_info_component(comp):
        return f"{ref} still needs info"
    return f"accepted {ref}"


def _match(parent: QWidget, session: Session, ref: str) -> str:
    if not ref:
        return "Select a needs-info row to match."
    from panelcad.gui.part_picker import pick_catalog_part

    _sheet, comp = find_component(session, ref)
    props = (comp.properties or {}) if comp is not None else {}
    part = pick_catalog_part(
        parent,
        session,
        query=str(props.get("part_number") or ""),
        manufacturer=str(props.get("manufacturer") or ""),
    )
    if part is None:
        return "match cancelled"
    result = resolve_with_catalog(session, ref, part)
    if result.get("applied"):
        return f"matched {ref} to {part.catalog}"
    return str(result.get("reason") or "not matched")


def _live_client() -> Any | None:
    from panelcad.needs_info import default_lookup_client

    return default_lookup_client()


def _button(row: QHBoxLayout, text: str, name: str, slot) -> QPushButton:
    button = QPushButton(text)
    button.setObjectName(name)
    button.clicked.connect(slot)
    row.addWidget(button)
    return button


def _spin() -> QDoubleSpinBox:
    from panelcad.library.panel import max_confirmed_envelope_mm

    spin = QDoubleSpinBox()
    spin.setRange(0.0, max(1.0, float(max_confirmed_envelope_mm())))
    spin.setDecimals(2)
    spin.setSingleStep(1.0)
    spin.setSuffix(" mm")
    spin.setValue(0.0)
    spin.setSpecialValueText("enter mm")
    return spin


def _spin_text(spin: QDoubleSpinBox) -> str:
    if spin.value() <= 0:
        return ""
    text = f"{spin.value():.2f}".rstrip("0").rstrip(".")
    return text


def _set_spin(spin: QDoubleSpinBox, value: Any) -> None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        spin.setValue(0.0)
        return
    if number <= 0:
        spin.setValue(0.0)
        return
    spin.setValue(number)
