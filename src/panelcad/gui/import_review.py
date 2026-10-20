"""Print review: the source crop beside the rebuilt tag, with accept, fix, or reject."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from panelcad.importer.models import LOW_CONFIDENCE

_SOURCE_LABELS = {
    "vector": "vector text",
    "ocr": "OCR",
    "symbol": "symbol match",
    "model": "model-assisted",
}


def _clip_crop(pix: QPixmap, item: dict) -> QPixmap:
    """Model and pixel rows show the detection box. A fallback note shows the whole tile."""

    if str(item.get("kind") or "") == "note":
        return pix
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    box = item.get("crop_box") or payload.get("crop_box")
    span = item.get("crop_span") or payload.get("crop_span")
    if not (
        isinstance(box, (list, tuple))
        and isinstance(span, (list, tuple))
        and len(box) == 4
        and len(span) == 2
    ):
        return pix
    try:
        span_w = float(span[0])
        span_h = float(span[1])
        x, y, width, height = (float(value) for value in box)
    except (TypeError, ValueError):
        return pix
    too_small = span_w <= 0 or span_h <= 0 or width <= 0 or height <= 0
    if too_small or pix.width() < 1 or pix.height() < 1:
        return pix
    pad = 2.0
    x -= pad
    y -= pad
    width += pad * 2
    height += pad * 2
    scale_x = pix.width() / span_w
    scale_y = pix.height() / span_h
    x0 = max(0, int(x * scale_x))
    y0 = max(0, int(y * scale_y))
    x1 = min(pix.width(), int((x + width) * scale_x + 0.999))
    y1 = min(pix.height(), int((y + height) * scale_y + 0.999))
    if x1 - x0 < 1 or y1 - y0 < 1:
        return pix
    clipped = pix.copy(x0, y0, x1 - x0, y1 - y0)
    return pix if clipped.isNull() else clipped


def _source_label(item: dict) -> str:
    payload = item.get("payload") or {}
    if item.get("model_assisted") or payload.get("model_assisted") or str(item.get("source") or payload.get("source") or "") == "model":
        label = "model-assisted"
    elif payload.get("inherited"):
        label = "inherited"
    else:
        source = str(item.get("source") or payload.get("source") or "")
        label = _SOURCE_LABELS.get(source, source or "—")
    if item.get("vision_fallback") or payload.get("vision_fallback"):
        return f"{label} · vision fallback"
    return label


class ImportReviewPanel(QWidget):
    """One extracted item at a time. The crop is the evidence; the text is what was read."""

    decision_made = Signal(str, str, str)
    rerun_requested = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict] = []
        self._index = -1

        self.caption = QLabel("Print review")
        self.caption.setStyleSheet("font-weight: 600;")
        self.caption.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.caption.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.list = QListWidget()
        self.list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.list.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setMinimumWidth(460)
        self.list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.list.currentRowChanged.connect(self._show_row)

        self.crop = QLabel("No crop")
        self.crop.setObjectName("printReviewCrop")
        self.crop.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.crop.setMinimumSize(160, 72)
        self.rebuilt = QLabel("Rebuilt item appears after the project is built.")
        self.rebuilt.setObjectName("printReviewRebuilt")
        self.rebuilt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rebuilt.setMinimumSize(160, 72)
        crop_col = QWidget()
        crop_lay = QVBoxLayout(crop_col)
        crop_lay.setContentsMargins(0, 0, 0, 0)
        crop_lay.addWidget(QLabel("Original crop"))
        crop_lay.addWidget(self.crop, stretch=1)
        built_col = QWidget()
        built_lay = QVBoxLayout(built_col)
        built_lay.setContentsMargins(0, 0, 0, 0)
        built_lay.addWidget(QLabel("Rebuilt"))
        built_lay.addWidget(self.rebuilt, stretch=1)
        pictures = QHBoxLayout()
        pictures.addWidget(crop_col, stretch=1)
        pictures.addWidget(built_col, stretch=1)

        self.detail = QLabel("Import a print to review what was read.")
        self.detail.setWordWrap(True)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Corrected tag or wire number")

        self.accept_btn = QPushButton("Accept")
        self.fix_btn = QPushButton("Fix")
        self.reject_btn = QPushButton("Reject")
        self.rerun_tile_btn = QPushButton("Re-run tile")
        self.rerun_page_btn = QPushButton("Re-run page")
        self.accept_btn.clicked.connect(lambda: self._emit("accept"))
        self.fix_btn.clicked.connect(lambda: self._emit("fix"))
        self.reject_btn.clicked.connect(lambda: self._emit("reject"))
        self.rerun_tile_btn.clicked.connect(self._rerun_tile)
        self.rerun_page_btn.clicked.connect(self._rerun_page)

        buttons = QHBoxLayout()
        for button in (self.accept_btn, self.fix_btn, self.reject_btn, self.rerun_tile_btn, self.rerun_page_btn):
            buttons.addWidget(button)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.addLayout(pictures, stretch=1)
        right_lay.addWidget(self.detail)
        right_lay.addWidget(self.edit)
        right_lay.addLayout(buttons)

        split = QSplitter()
        split.addWidget(self.list)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([560, 640])
        split.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        review = QWidget()
        review_lay = QVBoxLayout(review)
        review_lay.setContentsMargins(0, 0, 0, 0)
        review_lay.addWidget(split, 1)

        self.diag_counts = QLabel("No diagnostics for this import.")
        self.diag_counts.setObjectName("diagnosticsCounts")
        self.diag_counts.setWordWrap(True)
        self.diag_list = QListWidget()
        self.diag_list.setObjectName("diagnosticsList")
        self.diag_list.currentRowChanged.connect(self._show_diag)
        self.diag_crop = QLabel("No tile")
        self.diag_crop.setObjectName("diagnosticsCrop")
        self.diag_crop.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.diag_crop.setMinimumSize(160, 72)
        self.diag_reply = QPlainTextEdit()
        self.diag_reply.setObjectName("diagnosticsReply")
        self.diag_reply.setReadOnly(True)
        self.diag_reply.setPlaceholderText("Raw model reply")
        diag_side = QWidget()
        diag_side_lay = QVBoxLayout(diag_side)
        diag_side_lay.setContentsMargins(0, 0, 0, 0)
        diag_side_lay.addWidget(QLabel("Tile crop"))
        diag_side_lay.addWidget(self.diag_crop, stretch=1)
        diag_side_lay.addWidget(QLabel("Raw reply"))
        diag_side_lay.addWidget(self.diag_reply, stretch=1)
        diag_split = QSplitter()
        diag_split.addWidget(self.diag_list)
        diag_split.addWidget(diag_side)
        diag_split.setStretchFactor(0, 1)
        diag_split.setStretchFactor(1, 1)
        self.truth_path = QLineEdit()
        self.truth_path.setObjectName("diagnosticsTruth")
        self.truth_path.setPlaceholderText("Ground truth file (yaml or json)")
        self.truth_path.returnPressed.connect(self._apply_truth_path)
        self.truth_browse = QPushButton("Truth…")
        self.truth_browse.setObjectName("diagnosticsTruthBrowse")
        self.truth_browse.clicked.connect(self._browse_truth)
        truth_row = QWidget()
        truth_lay = QHBoxLayout(truth_row)
        truth_lay.setContentsMargins(0, 0, 0, 0)
        truth_lay.addWidget(self.truth_path, 1)
        truth_lay.addWidget(self.truth_browse)
        diagnostics = QWidget()
        diagnostics_lay = QVBoxLayout(diagnostics)
        diagnostics_lay.setContentsMargins(0, 0, 0, 0)
        diagnostics_lay.addWidget(truth_row)
        diagnostics_lay.addWidget(self.diag_counts)
        diagnostics_lay.addWidget(diag_split, 1)

        self.tabs = QTabWidget()
        self.tabs.addTab(review, "Review")
        self.tabs.addTab(diagnostics, "Diagnostics")
        self._diag: dict = {}
        self._diag_items: list[dict] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 2, 6, 2)
        lay.setSpacing(2)
        lay.addWidget(self.caption, 0)
        lay.addWidget(self.tabs, 1)
        self.apply_theme()

    def apply_theme(self) -> None:
        from panelcad.theme import ui_hex

        border = ui_hex("border")
        text = ui_hex("text")
        panel = ui_hex("panel")
        field = ui_hex("field")
        self.crop.setStyleSheet(
            f"background: {panel}; color: {text}; border: 1px solid {border};"
        )
        self.rebuilt.setStyleSheet(
            f"background: {field}; color: {text}; border: 1px solid {border};"
        )
        self.diag_crop.setStyleSheet(
            f"background: {panel}; color: {text}; border: 1px solid {border};"
        )

    def set_items(self, items: list[dict]) -> None:
        self._items = list(items)
        self.list.clear()
        low = 0
        for item in self._items:
            mark = item.get("status") or "pending"
            text = item.get("text") or ("unreadable" if item.get("unreadable") else "")
            if item.get("kind") == "note" and "(" in text:
                text = text.split("(", 1)[0].strip()
            payload = item.get("payload") or {}
            if payload.get("suggested_tag"):
                note = f"  suggest {payload.get('suggested_tag')}"
            elif payload.get("wrong_field"):
                note = "  wrong field"
            elif payload.get("stub"):
                note = "  wire stub"
            elif payload.get("ambiguous"):
                note = "  ambiguous ends"
            elif payload.get("attached") and payload.get("needs_review"):
                note = "  attached"
            else:
                note = ""
            confidence = float(item.get("confidence") or 0)
            flagged = item.get("kind") != "note" and (
                confidence < LOW_CONFIDENCE
                or item.get("unreadable")
                or payload.get("needs_review")
                or payload.get("stub")
            )
            flag = "  low-confidence" if flagged else ""
            label = (
                f"{item.get('kind')}  {text or '—'}  {confidence:.2f}"
                f"  {_source_label(item)}{flag}{note}  {mark}"
            )
            row = QListWidgetItem(label)
            row.setToolTip(label)
            self.list.addItem(row)
            if flagged:
                low += 1
        hint = self.list.sizeHintForColumn(0) + 36
        self.list.setMinimumWidth(min(720, max(460, hint)))
        self.caption.setText(f"Print review — {len(self._items)} item(s), {low} low-confidence")
        if self._items:
            self.list.setCurrentRow(0)
        else:
            self.detail.setText("Nothing extracted yet.")
            self.crop.setText("No crop")
            self.crop.setPixmap(QPixmap())
            self.rebuilt.setText("Rebuilt item appears after the project is built.")
            self.rebuilt.setPixmap(QPixmap())

    def set_diagnostics(self, report: dict | None) -> None:
        """Stage counts, lost items, and a click-through to the tile crop and raw reply."""

        self._diag = dict(report or {})
        counts = self._diag.get("counts") or {}
        losses = self._diag.get("loss_counts") or {}
        if not self._diag:
            self.diag_counts.setText("No diagnostics for this import.")
            self.diag_list.clear()
            self._diag_items = []
            self.diag_reply.setPlainText("")
            self.diag_crop.setPixmap(QPixmap())
            self.diag_crop.setText("No tile")
            return
        version = self._diag.get("prompt_version") or ""
        self.diag_counts.setText(
            "prompt "
            + str(version)
            + "   reported {reported}   parsed {parsed}   accepted {accepted}   merged {merged}   built {built}\n"
            "model_miss {model_miss}   parse_fail {parse_fail}   code_reject {code_reject}   "
            "wrong_field {wrong_field}   merge_loss {merge_loss}   build_fail {build_fail}   "
            "wrong_connection {wrong_connection}".format(
                reported=int(counts.get("reported") or 0),
                parsed=int(counts.get("parsed") or 0),
                accepted=int(counts.get("accepted") or 0),
                merged=int(counts.get("merged") or 0),
                built=int(counts.get("built") or 0),
                model_miss=int(losses.get("model_miss") or 0),
                parse_fail=int(losses.get("parse_fail") or 0),
                code_reject=int(losses.get("code_reject") or 0),
                wrong_field=int(losses.get("wrong_field") or 0),
                merge_loss=int(losses.get("merge_loss") or 0),
                build_fail=int(losses.get("build_fail") or 0),
                wrong_connection=int(losses.get("wrong_connection") or 0),
            )
        )
        self._diag_items = [item for item in (self._diag.get("items") or []) if item.get("loss")]
        self.diag_list.clear()
        for item in self._diag_items:
            rule = f"  {item.get('rule')}" if item.get("rule") else ""
            label = f"{item.get('loss')}{rule}  {item.get('kind')}  {item.get('text') or '—'}"
            row = QListWidgetItem(label)
            row.setToolTip(label)
            self.diag_list.addItem(row)
        shown = str(self._diag.get("truth_path") or "")
        if shown and not self.truth_path.text().strip():
            self.truth_path.setText(shown)
        if self._diag_items:
            self.diag_list.setCurrentRow(0)
        elif self._losses_look_clear():
            self.diag_reply.setPlainText("Nothing was lost.")
            self.diag_crop.setText("No tile")
        else:
            self.diag_reply.setPlainText("Devices are still missing or low-confidence.")
            self.diag_crop.setText("No tile")

    def _losses_look_clear(self) -> bool:
        """Empty loss list is not a clean import while devices are missing or uncertain."""

        losses = self._diag.get("loss_counts") or {}
        if any(int(losses.get(name) or 0) for name in losses):
            return False
        for item in self._items:
            if item.get("kind") == "note":
                continue
            payload = item.get("payload") or {}
            confidence = float(item.get("confidence") or 0)
            if (
                confidence < LOW_CONFIDENCE
                or item.get("unreadable")
                or payload.get("needs_review")
                or payload.get("stub")
            ):
                return False
        return True

    def _browse_truth(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self,
            "Ground truth",
            self.truth_path.text().strip(),
            "Truth (*.yaml *.yml *.json)",
        )
        if not path:
            return
        self.truth_path.setText(path)
        self._apply_truth_path()

    def _apply_truth_path(self) -> None:
        path = self.truth_path.text().strip()
        root = str(self._diag.get("path") or "")
        if not path or not root:
            self.diag_reply.setPlainText("Open an import, then choose a ground-truth file.")
            return
        from panelcad.importer.diagnostics import load_run

        try:
            report = load_run(root, truth=path)
        except (OSError, ValueError) as exc:
            self.diag_reply.setPlainText(str(exc).splitlines()[0])
            return
        self.set_diagnostics(report)

    def _show_diag(self, row: int) -> None:
        if row < 0 or row >= len(self._diag_items):
            return
        item = self._diag_items[row]
        root = Path(str(self._diag.get("path") or ""))
        rel = str(item.get("trace") or "")
        file = root / rel if rel else Path()
        reply = "The model did not report this item."
        image = ""
        box = item.get("bbox")
        span = None
        if file.is_file():
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
            payload = data.get("reply")
            if isinstance(payload, str):
                reply = payload
            elif payload is not None:
                reply = json.dumps(payload, indent=2, default=str)
            cut = data.get("truncated_reply")
            if isinstance(cut, str) and cut.strip():
                reply = "First reply (cut off):\n" + cut + "\n\n" + reply
            request = data.get("request") if isinstance(data.get("request"), dict) else {}
            image = str(request.get("image") or "")
            bbox = data.get("bbox") if isinstance(data.get("bbox"), dict) else {}
            if bbox:
                span = [bbox.get("w") or 0, bbox.get("h") or 0]
        self.diag_reply.setPlainText(reply[:20000])
        self._show_diag_crop(image, box if isinstance(box, list) else [], span)

    def _show_diag_crop(self, path: str, box: list, span: list | None) -> None:
        file = Path(path)
        if not path or not file.is_file():
            self.diag_crop.setPixmap(QPixmap())
            self.diag_crop.setText("Crop not on disk" if path else "No tile")
            return
        pix = QPixmap(str(file))
        if pix.isNull():
            self.diag_crop.setText("Crop could not be read")
            return
        if span:
            pix = _clip_crop(pix, {"crop_box": box, "crop_span": span, "kind": "device"})
        self._fit(self.diag_crop, pix)

    def current_item(self) -> dict | None:
        if self._index < 0 or self._index >= len(self._items):
            return None
        return self._items[self._index]

    def _show_row(self, row: int) -> None:
        self._index = row
        item = self.current_item()
        if item is None:
            return
        text = item.get("text") or ("unreadable" if item.get("unreadable") else "")
        self.edit.setText("" if item.get("unreadable") else str(item.get("text") or ""))
        kind = item.get("kind") or "item"
        conf = float(item.get("confidence") or 0)
        page = str(item.get("page_id") or "—")
        tile = str(item.get("tile_id") or "—")
        self.detail.setText(
            f"{kind} {text or 'unreadable'}\n"
            f"confidence {conf:.2f}   source {_source_label(item)}   status {item.get('status') or 'pending'}\n"
            f"{page}   {tile}"
        )
        self._load_crop(item)
        self._load_rebuilt(str(item.get("rebuilt") or ""))

    def _load_crop(self, item: dict) -> None:
        path = str(item.get("crop") or "")
        file = Path(path)
        if not path or not file.is_file():
            self.crop.setPixmap(QPixmap())
            self.crop.setText("Crop not on disk")
            return
        pix = QPixmap(str(file))
        if pix.isNull():
            self.crop.setText("Crop could not be read")
            return
        pix = _clip_crop(pix, item)
        if pix.isNull():
            self.crop.setText("Crop could not be read")
            return
        self._fit(self.crop, pix)

    def _load_rebuilt(self, path: str) -> None:
        file = Path(path)
        if not path or not file.is_file():
            self.rebuilt.setPixmap(QPixmap())
            self.rebuilt.setText("Rebuilt item appears after the project is built.")
            return
        pix = QPixmap(str(file))
        if pix.isNull():
            self.rebuilt.setText("Rebuilt crop could not be read")
            return
        self._fit(self.rebuilt, pix)

    def _fit(self, label: QLabel, pix: QPixmap) -> None:
        label.setText("")
        target = label.size()
        if target.width() < 40 or target.height() < 40:
            target = label.minimumSize()
        fitted = pix.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        label.setPixmap(fitted)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        item = self.current_item()
        if item is not None:
            self._load_crop(item)
            self._load_rebuilt(str(item.get("rebuilt") or ""))

    def _emit(self, action: str) -> None:
        item = self.current_item()
        if item is None:
            return
        text = self.edit.text().strip()
        if action == "fix" and not text:
            self.detail.setText("Type the corrected text, then Fix. Empty text is not a guess.")
            return
        self.decision_made.emit(str(item["id"]), action, text)

    def _rerun_tile(self) -> None:
        item = self.current_item()
        if item is None:
            return
        self.rerun_requested.emit(str(item.get("tile_id") or ""), "")

    def _rerun_page(self) -> None:
        item = self.current_item()
        if item is None:
            return
        self.rerun_requested.emit("", str(item.get("page_id") or ""))
