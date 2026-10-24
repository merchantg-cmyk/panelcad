"""Convert drawing standard: target, scope, save-as, and a chunk review."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.core.models import Project
from panelcad.core.refs import sheet_drawing_standard

THUMB_W = 520
FULL_W = 1280


def _default_out(session: Session, target: str) -> Path:
    token = "iec" if target == "iec" else "nema"
    source = session.path
    if source is not None:
        text = source.name
        if text.endswith(".pcad.yaml"):
            stem = text[: -len(".pcad.yaml")]
            return source.with_name(f"{stem}-{token}.pcad.yaml")
        return source.with_name(f"{source.stem}-{token}{source.suffix or '.yaml'}")
    slug = "".join(ch if ch.isalnum() else "-" for ch in session.project.name).strip("-") or "project"
    return Path.cwd() / f"{slug}-{token}.pcad.yaml"


def sheet_thumbnail(project: Project, sheet_id: str, catalog: object, width: int = THUMB_W) -> QPixmap | None:
    """Before/after preview. Returns None when the sheet or the painter is unavailable."""

    try:
        sheet = project.sheet(sheet_id)
    except KeyError:
        cs_block = dict(project.properties.get("convert_standard") or {})
        mp_block = dict(cs_block.get("multipage") or {})
        consol = dict(mp_block.get("target_for_sheet") or dict(cs_block.get("consolidated") or {}).get("target_for_sheet") or {})
        mapped = str(consol.get(sheet_id) or "")
        if not mapped and sheet_id in (mp_block.get("removed_sheets") or []):
            kept = list(mp_block.get("kept_sheets") or [])
            mapped = str(kept[0]) if kept else ""
        if not mapped:
            return None
        try:
            sheet = project.sheet(mapped)
        except KeyError:
            return None
    try:
        from panelcad.render.page import paint_sheet

        pw, ph = sheet.page.dimensions()
        scale = width / max(pw, 1.0)
        image = QImage(
            max(1, int(pw * scale)),
            max(1, int(ph * scale)),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        image.fill(QColor("#ffffff"))
        painter = QPainter(image)
        painter.scale(scale, scale)
        index = next(i for i, page in enumerate(project.sheets) if page.id == sheet.id)
        paint_sheet(
            painter,
            project,
            sheet,
            catalog,  # type: ignore[arg-type]
            sheet_index=index,
            sheet_count=len(project.sheets),
        )
        painter.end()
        return QPixmap.fromImage(image)
    except Exception:  # noqa: BLE001
        return None


class CompareDialog(QDialog):
    """Full-size before and after for one sheet. Not modal, so the review stays up."""

    def __init__(
        self,
        session: Session,
        before: Project | None,
        after: Project,
        sheet_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("convert-compare")
        self.setModal(False)
        self.setWindowTitle(f"Before and after — {sheet_id}")
        self.resize(1680, 960)
        row = QHBoxLayout(self)
        row.addWidget(self._pane("Before", before, sheet_id, session))
        row.addWidget(self._pane("After", after, sheet_id, session))

    def _pane(
        self,
        caption: str,
        project: Project | None,
        sheet_id: str,
        session: Session,
    ) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        title = QLabel(caption)
        title.setObjectName(f"compare:{caption}")
        layout.addWidget(title)
        scroll = QScrollArea()
        scroll.setWidgetResizable(False)
        image = QLabel("no preview")
        image.setObjectName(f"compare-image:{caption}")
        image.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        if project is not None:
            pix = sheet_thumbnail(project, sheet_id, session.catalog, width=FULL_W)
            if pix is not None:
                image.setPixmap(pix)
                image.setText("")
        scroll.setWidget(image)
        layout.addWidget(scroll, 1)
        return wrap


class _SheetThumb(QFrame):
    """Click opens the full-size before/after view."""

    def __init__(
        self,
        owner: "ConvertDialog",
        caption: str,
        project: Project | None,
        sheet_id: str,
    ) -> None:
        super().__init__()
        self._owner = owner
        self._sheet_id = sheet_id
        self.setObjectName(f"thumb:{caption}:{sheet_id}")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to open both sheets full size.")
        layout = QVBoxLayout(self)
        caption_label = QLabel(caption)
        image = QLabel("no preview")
        image.setMinimumSize(THUMB_W, 280)
        if project is not None:
            pix = sheet_thumbnail(project, sheet_id, owner.session.catalog)
            if pix is not None:
                image.setPixmap(pix)
                image.setText("")
        for child in (caption_label, image):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(caption_label)
        layout.addWidget(image)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._sheet_id:
            self._owner.open_compare(self._sheet_id)
        super().mouseReleaseEvent(event)


class ConvertDialog(QDialog):
    """Menu command 'Convert drawing standard...'."""

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("Convert drawing standard")
        self.resize(1480, 900)
        self._workspace = ""
        self._source: Project | None = None
        self._compare: CompareDialog | None = None

        current = sheet_drawing_standard(session.project, session.sheet() if session.project.sheets else None)
        self.iec = QRadioButton("IEC")
        self.nema = QRadioButton("NEMA/ANSI")
        (self.nema if current == "IEC" else self.iec).setChecked(True)
        standards = QButtonGroup(self)
        standards.addButton(self.iec)
        standards.addButton(self.nema)
        self.iec.toggled.connect(self._refresh_path)
        self.nema.toggled.connect(self._refresh_path)

        self.whole = QRadioButton("Whole project")
        self.picked = QRadioButton("Selected sheets")
        self.whole.setChecked(True)
        scope = QButtonGroup(self)
        scope.addButton(self.whole)
        scope.addButton(self.picked)
        self.keep_numbers = QCheckBox("Keep existing numbers")
        self.keep_numbers.setObjectName("keep-existing-numbers")
        self.keep_numbers.setToolTip(
            "Leave each device's number as it is. "
            "Unchecked, each letter family is numbered 1, 2, 3 in sheet order."
        )
        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("convert-mode-combo")
        self.mode_combo.addItem("Standard (rules only)", "rules_only")
        self.mode_combo.addItem("AI-assisted", "ai_assisted")
        self.mode_combo.setToolTip(
            "Standard uses deterministic rules only. "
            "AI-assisted adds validated advisory suggestions for ambiguous roles, unmapped symbols, notes, and layout."
        )

        self.sheets = QListWidget()
        active = session.active_sheet_id
        for sheet in session.project.sheets:
            item = QListWidgetItem(f"{sheet.id}   {sheet.title}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if sheet.id == active else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, sheet.id)
            self.sheets.addItem(item)

        self.path = QLineEdit()
        self._refresh_path()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)

        convert = QPushButton("Convert")
        convert.clicked.connect(self._on_convert)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)

        form = QVBoxLayout()
        form.addWidget(QLabel("Target standard"))
        row = QHBoxLayout()
        row.addWidget(self.iec)
        row.addWidget(self.nema)
        row.addStretch(1)
        form.addLayout(row)
        form.addWidget(QLabel("Scope"))
        row = QHBoxLayout()
        row.addWidget(self.whole)
        row.addWidget(self.picked)
        row.addStretch(1)
        form.addLayout(row)
        form.addWidget(self.keep_numbers)
        form.addWidget(QLabel("Conversion type"))
        form.addWidget(self.mode_combo)
        form.addWidget(self.sheets)
        form.addWidget(QLabel("Save as"))
        row = QHBoxLayout()
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        form.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(convert)
        row.addWidget(close)
        row.addStretch(1)
        form.addLayout(row)

        self.review_host = QWidget()
        self.review_layout = QVBoxLayout(self.review_host)
        self.review_layout.setContentsMargins(8, 8, 8, 8)
        self.review_layout.addWidget(QLabel("Review appears after Convert."))
        self.review_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.review_host)
        scroll.setObjectName("convert-review")

        root = QHBoxLayout(self)
        left = QWidget()
        left.setMinimumWidth(320)
        left.setLayout(form)
        root.addWidget(left, 0)
        root.addWidget(scroll, 1)

    def _target(self) -> str:
        return "iec" if self.iec.isChecked() else "nema"

    def _refresh_path(self) -> None:
        if self.path.text().strip() and getattr(self, "_path_edited", False):
            return
        self.path.setText(str(_default_out(self.session, self._target())))

    def _browse(self) -> None:
        name, _filter = QFileDialog.getSaveFileName(
            self,
            "Save converted project",
            self.path.text(),
            "PanelCAD (*.pcad.yaml *.yaml)",
        )
        if name:
            self._path_edited = True
            self.path.setText(name)

    def chosen_sheets(self) -> list[str] | None:
        if self.whole.isChecked():
            return None
        picked: list[str] = []
        for index in range(self.sheets.count()):
            item = self.sheets.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                picked.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return picked

    def _on_convert(self) -> None:
        try:
            self.perform()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Convert drawing standard", str(exc))

    def conversion_mode(self) -> str:
        return str(self.mode_combo.currentData() or "rules_only")

    def perform(self) -> dict:
        """Run the conversion and fill the review. Tests call this without exec."""

        result = self.session.convert_standard(
            self._target(),
            sheets=self.chosen_sheets(),
            out=self.path.text().strip() or None,
            keep_numbers=self.keep_numbers.isChecked(),
            mode=self.conversion_mode(),
        )
        self.show_review(result)
        return result

    def show_review(self, result: dict) -> None:
        self._workspace = str(result.get("workspace") or self._workspace)
        self._source = _load_source(self._workspace)
        while self.review_layout.count():
            item = self.review_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # deleteLater leaves the old label on screen until the event
                # loop runs, so it painted on top of the new header.
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        decisions = list(result.get("decisions") or [])
        header = QLabel(
            f"Target {result.get('target')}    pending {len(result.get('pending') or [])}    decisions: {len(decisions)}"
        )
        header.setObjectName("convert-review-header")
        self.review_layout.addWidget(header)
        for chunk in result.get("chunks") or []:
            self.review_layout.addWidget(self._chunk_row(chunk))
        self.review_layout.addStretch(1)

    def _chunk_row(self, chunk: dict) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame.setObjectName(f"chunk:{chunk['id']}")
        layout = QVBoxLayout(frame)
        flags = ", ".join(issue.get("flag", "") for issue in (chunk.get("issues") or [])) or "none"
        reasons = ", ".join(str(r) for r in (chunk.get("reason_codes") or [])) or flags
        summary_lbl = QLabel(
            f"{chunk['id']}    {chunk['status']}    flags: {flags}    reasons: {reasons}"
        )
        summary_lbl.setObjectName(f"chunk-summary:{chunk['id']}")
        layout.addWidget(summary_lbl)
        chunk_decs = list(chunk.get("decisions") or [])
        if chunk_decs:
            dec_lines = [
                f"• [{d.get('reason_code') or d.get('flag')}] {d.get('message') or d.get('detail')}"
                for d in chunk_decs
            ]
            dec_lbl = QLabel("\n".join(dec_lines))
            dec_lbl.setObjectName(f"chunk-decisions:{chunk['id']}")
            dec_lbl.setWordWrap(True)
            layout.addWidget(dec_lbl)
        sheet_id = str(chunk.get("sheet") or "")
        if sheet_id:
            pics = QHBoxLayout()
            pics.addWidget(self._thumb("Before", self._source, sheet_id))
            pics.addWidget(self._thumb("After", self.session.project, sheet_id))
            layout.addLayout(pics)
            hint = QLabel("Click a picture to open both sheets full size.")
            layout.addWidget(hint)
        suggestion = str(chunk.get("suggestion") or "")
        if suggestion:
            note = QLabel(suggestion)
            note.setWordWrap(True)
            layout.addWidget(note)
        buttons = QHBoxLayout()
        accept = QPushButton("Accept")
        accept.setObjectName(f"accept:{chunk['id']}")
        rerun = QPushButton("Re-run")
        rerun.setObjectName(f"rerun:{chunk['id']}")
        revert = QPushButton("Revert")
        revert.setObjectName(f"revert:{chunk['id']}")
        cid = str(chunk["id"])
        accept.clicked.connect(lambda _checked=False, name=cid: self._accept(name))
        rerun.clicked.connect(lambda _checked=False, name=cid: self._rerun(name))
        revert.clicked.connect(lambda _checked=False, name=cid: self._revert(name))
        buttons.addWidget(accept)
        buttons.addWidget(rerun)
        buttons.addWidget(revert)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return frame

    def _thumb(self, caption: str, project: Project | None, sheet_id: str) -> QWidget:
        return _SheetThumb(self, caption, project, sheet_id)

    def open_compare(self, sheet_id: str) -> CompareDialog:
        previous = self._compare
        if previous is not None:
            previous.close()
        view = CompareDialog(self.session, self._source, self.session.project, sheet_id, self)
        view.show()
        self._compare = view
        return view

    def _accept(self, chunk_id: str) -> None:
        self.show_review(self.session.convert_standard_accept(chunk_id, self._workspace or None))

    def _rerun(self, chunk_id: str) -> None:
        try:
            result = self.session.convert_standard_rerun(chunk_id, self._workspace or None)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Convert drawing standard", str(exc))
            return
        self.show_review(result)
        self._reload_parent()

    def _revert(self, chunk_id: str) -> None:
        try:
            result = self.session.convert_standard_revert(chunk_id, self._workspace or None)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Convert drawing standard", str(exc))
            return
        self.show_review(result)
        self._reload_parent()

    def _reload_parent(self) -> None:
        parent = self.parent()
        canvas = getattr(parent, "canvas", None)
        if canvas is not None:
            canvas.reload()


def _load_source(workspace: str) -> Project | None:
    if not workspace:
        return None
    path = Path(workspace)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Project.model_validate(data["source_project"])
    except Exception:  # noqa: BLE001
        return None
