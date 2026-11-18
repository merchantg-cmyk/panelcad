"""Print review: the source crop beside the rebuilt tag, with accept, fix, or reject."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
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
    from_to_edited = Signal(list)
    rebuild_clean_ladder_requested = Signal(str)
    leftover_answer_requested = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict] = []
        self._index = -1
        self._leftovers: list[dict] = []
        self._leftover_index = -1
        self._leftover_choice_buttons: list[QPushButton] = []

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

        self.layout_mode_combo = QComboBox()
        self.layout_mode_combo.setObjectName("importLayoutModeCombo")
        # Ladder is the scan default; preserve stays selectable but cannot issue.
        self.layout_mode_combo.addItem("Clean NEMA Ladder", "ladder")
        self.layout_mode_combo.addItem("Preserve Scan Positions", "preserve")
        self.layout_mode_combo.setCurrentIndex(0)
        self.rebuild_clean_ladder_button = QPushButton("Rebuild Clean Ladder")
        self.rebuild_clean_ladder_button.setObjectName("rebuildCleanLadderButton")
        self.rebuild_clean_ladder_button.clicked.connect(self._on_rebuild_clean_ladder_clicked)

        header_bar = QWidget()
        header_lay = QHBoxLayout(header_bar)
        header_lay.setContentsMargins(0, 0, 0, 0)
        header_lay.setSpacing(6)
        header_lay.addWidget(self.caption, 1)
        header_lay.addWidget(QLabel("Layout Mode:"))
        header_lay.addWidget(self.layout_mode_combo)
        header_lay.addWidget(self.rebuild_clean_ladder_button)

        self.from_to_table = QTableWidget(0, 5)
        self.from_to_table.setObjectName("fromToScheduleTable")
        self.from_to_table.setHorizontalHeaderLabels(
            ["Wire #", "From Device.Pin", "To Device.Pin", "Sheet/Rung", "Confidence"]
        )
        hdr = self.from_to_table.horizontalHeader()
        if hdr is not None:
            hdr.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.from_to_table.itemChanged.connect(self._on_from_to_item_changed)
        self._updating_from_to = False

        self.add_from_to_row_button = QPushButton("Add Row")
        self.add_from_to_row_button.setObjectName("addFromToRowButton")
        self.add_from_to_row_button.clicked.connect(self._on_add_from_to_row)
        self.remove_from_to_row_button = QPushButton("Remove Row")
        self.remove_from_to_row_button.setObjectName("removeFromToRowButton")
        self.remove_from_to_row_button.clicked.connect(self.remove_selected_from_to_row)
        self.apply_from_to_edits_button = QPushButton("Apply From-To Edits")
        self.apply_from_to_edits_button.setObjectName("applyFromToEditsButton")
        self.apply_from_to_edits_button.clicked.connect(self._emit_from_to_edits)

        ft_btn_row = QWidget()
        ft_btn_lay = QHBoxLayout(ft_btn_row)
        ft_btn_lay.setContentsMargins(0, 0, 0, 0)
        ft_btn_lay.addWidget(self.add_from_to_row_button)
        ft_btn_lay.addWidget(self.remove_from_to_row_button)
        ft_btn_lay.addWidget(self.apply_from_to_edits_button)
        ft_btn_lay.addStretch(1)

        self.from_to_tab = QWidget()
        ft_lay = QVBoxLayout(self.from_to_tab)
        ft_lay.setContentsMargins(0, 0, 0, 0)
        ft_lay.addWidget(self.from_to_table, 1)
        ft_lay.addWidget(ft_btn_row, 0)

        self.leftover_list = QListWidget()
        self.leftover_list.setObjectName("leftoverQuestionsList")
        self.leftover_list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.leftover_list.setWordWrap(True)
        self.leftover_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.leftover_list.setMinimumHeight(56)
        self.leftover_list.setMaximumHeight(84)
        self.leftover_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.leftover_list.currentRowChanged.connect(self._show_leftover)
        self.leftover_list.itemDoubleClicked.connect(lambda *_: self._open_leftover_dialog())
        self.leftover_kind = QLabel("")
        self.leftover_kind.setObjectName("leftoverQuestionKind")
        self.leftover_kind.setWordWrap(True)
        self.leftover_detail = QLabel("No leftover questions. Rebuild a scan to list blockers.")
        self.leftover_detail.setObjectName("leftoverQuestionDetail")
        self.leftover_detail.setWordWrap(True)
        self.leftover_detail.setMinimumWidth(280)
        self.leftover_status = QLabel("")
        self.leftover_status.setObjectName("leftoverQuestionStatus")
        self.leftover_status.setWordWrap(True)
        self.leftover_choices = QWidget()
        self.leftover_choices.setObjectName("leftoverChoiceButtons")
        self.leftover_choices_lay = QGridLayout(self.leftover_choices)
        self.leftover_choices_lay.setContentsMargins(0, 0, 0, 0)
        self.leftover_choices_lay.setHorizontalSpacing(8)
        self.leftover_choices_lay.setVerticalSpacing(8)
        self.leftover_edit = QLineEdit()
        self.leftover_edit.setObjectName("leftoverTypedAnswer")
        self.leftover_edit.setPlaceholderText("Typed value (fallback)")
        self.leftover_edit.setMinimumHeight(32)
        self.leftover_apply = QPushButton("Apply typed answer")
        self.leftover_apply.setObjectName("leftoverApplyTyped")
        self.leftover_apply.setMinimumHeight(36)
        self.leftover_apply.clicked.connect(self._apply_leftover_typed)
        self.leftover_open = QPushButton("Answer in dialog…")
        self.leftover_open.setObjectName("leftoverOpenDialog")
        self.leftover_open.setMinimumHeight(36)
        self.leftover_open.clicked.connect(self._open_leftover_dialog)
        typed_row = QHBoxLayout()
        typed_row.addWidget(self.leftover_edit, 1)
        typed_row.addWidget(self.leftover_apply)
        typed_row.addWidget(self.leftover_open)
        self.leftover_scan = QLabel("No scan crop")
        self.leftover_scan.setObjectName("leftoverScanCrop")
        self.leftover_scan.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.leftover_scan.setMinimumSize(220, 120)
        self.leftover_scan.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.leftover_sheet = QLabel("No sheet crop")
        self.leftover_sheet.setObjectName("leftoverSheetCrop")
        self.leftover_sheet.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.leftover_sheet.setMinimumSize(220, 120)
        self.leftover_sheet.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        leftover_pics = QHBoxLayout()
        scan_col = QWidget()
        scan_lay = QVBoxLayout(scan_col)
        scan_lay.setContentsMargins(0, 0, 0, 0)
        scan_lay.addWidget(QLabel("Original scan crop"))
        scan_lay.addWidget(self.leftover_scan, stretch=1)
        sheet_col = QWidget()
        sheet_lay = QVBoxLayout(sheet_col)
        sheet_lay.setContentsMargins(0, 0, 0, 0)
        sheet_lay.addWidget(QLabel("Rebuilt sheet highlight"))
        sheet_lay.addWidget(self.leftover_sheet, stretch=1)
        leftover_pics.addWidget(scan_col, stretch=1)
        leftover_pics.addWidget(sheet_col, stretch=1)
        detail_body = QWidget()
        detail_lay = QVBoxLayout(detail_body)
        detail_lay.setContentsMargins(4, 4, 4, 4)
        detail_lay.setSpacing(8)
        detail_lay.addWidget(self.leftover_kind)
        detail_lay.addWidget(self.leftover_detail)
        # Crops before the choice grid so evidence stays above the fold.
        detail_lay.addLayout(leftover_pics, stretch=1)
        detail_lay.addWidget(self.leftover_choices)
        detail_lay.addLayout(typed_row)
        detail_lay.addWidget(self.leftover_status)
        leftover_scroll = QScrollArea()
        leftover_scroll.setObjectName("leftoverQuestionsScroll")
        leftover_scroll.setWidgetResizable(True)
        leftover_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        leftover_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        leftover_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        leftover_scroll.setWidget(detail_body)
        leftover_scroll.setMinimumHeight(160)
        self.leftover_tab = QWidget()
        self.leftover_tab.setObjectName("leftoverQuestionsTab")
        leftover_tab_lay = QVBoxLayout(self.leftover_tab)
        leftover_tab_lay.setContentsMargins(4, 4, 4, 4)
        leftover_tab_lay.setSpacing(4)
        leftover_tab_lay.addWidget(self.leftover_list, 0)
        leftover_tab_lay.addWidget(leftover_scroll, 1)

        self.tabs = QTabWidget()
        self.tabs.addTab(review, "Review")
        self.tabs.addTab(self.from_to_tab, "From-To Wire Schedule")
        self.tabs.addTab(self.leftover_tab, "Leftover questions (0)")
        self.tabs.addTab(diagnostics, "Diagnostics")
        self._leftover_tab_index = 2
        self._diag: dict = {}
        self._diag_items: list[dict] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 2, 6, 2)
        lay.setSpacing(2)
        lay.addWidget(header_bar, 0)
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
        self.leftover_scan.setStyleSheet(
            f"background: {panel}; color: {text}; border: 1px solid {border};"
        )
        self.leftover_sheet.setStyleSheet(
            f"background: {field}; color: {text}; border: 1px solid {border};"
        )
        self._leftover_btn_style = (
            f"QPushButton {{ color: {text}; background: {field}; border: 1px solid {border}; "
            f"padding: 8px 14px; min-height: 36px; }}"
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

    def layout_mode(self) -> str:
        data = self.layout_mode_combo.currentData()
        return str(data or "ladder")

    def set_layout_mode(self, mode: str) -> None:
        norm = "ladder" if str(mode or "").strip().lower() in {"ladder", "clean_ladder", "clean"} else "preserve"
        idx = self.layout_mode_combo.findData(norm)
        if idx >= 0:
            self.layout_mode_combo.setCurrentIndex(idx)

    def set_from_to_schedule(self, schedule_or_rows: object) -> None:
        rows_list: list[dict] = []
        if hasattr(schedule_or_rows, "to_dict"):
            d = schedule_or_rows.to_dict()
            rows_list = list(d.get("from_to_rows") or [])
        elif isinstance(schedule_or_rows, dict):
            rows_list = list(schedule_or_rows.get("from_to_rows") or [])
        elif isinstance(schedule_or_rows, list):
            for item in schedule_or_rows:
                if hasattr(item, "to_dict"):
                    rows_list.append(item.to_dict())
                elif isinstance(item, dict):
                    rows_list.append(dict(item))
        self._updating_from_to = True
        try:
            self.from_to_table.setRowCount(0)
            for r in rows_list:
                w_num = str(r.get("wire_number") or r.get("number") or "")
                f_ep = str(r.get("from") or "")
                if not f_ep:
                    ft = str(r.get("from_tag") or "")
                    fp = str(r.get("from_pin") or "")
                    f_ep = f"{ft}.{fp}" if (ft and fp) else ft
                t_ep = str(r.get("to") or "")
                if not t_ep:
                    tt = str(r.get("to_tag") or "")
                    tp = str(r.get("to_pin") or "")
                    t_ep = f"{tt}.{tp}" if (tt and tp) else tt
                sh = str(r.get("sheet_id") or r.get("page_id") or "page-001")
                rung = int(r.get("rung") or 1)
                conf = float(r.get("confidence", 0.90))
                needs_rev = bool(r.get("needs_review", conf < 0.80))
                self._insert_from_to_table_row(
                    w_num,
                    f_ep,
                    t_ep,
                    f"{sh}:{rung}",
                    conf,
                    needs_review=needs_rev,
                    source=str(r.get("source") or "extracted"),
                )
        finally:
            self._updating_from_to = False

    def _insert_from_to_table_row(
        self,
        wire_number: str,
        from_ep: str,
        to_ep: str,
        sheet_rung: str,
        confidence: float,
        *,
        needs_review: bool = False,
        source: str = "extracted",
    ) -> int:
        row_idx = self.from_to_table.rowCount()
        self.from_to_table.insertRow(row_idx)
        conf_label = f"{confidence:.2f}" + (" (needs review)" if needs_review else "")
        cells = [wire_number, from_ep, to_ep, sheet_rung, conf_label]
        for col_idx, val in enumerate(cells):
            item = QTableWidgetItem(val)
            if col_idx == 4:
                item.setData(Qt.ItemDataRole.UserRole, float(confidence))
                item.setData(Qt.ItemDataRole.UserRole + 1, str(source))
                item.setData(Qt.ItemDataRole.UserRole + 2, bool(needs_review))
            if needs_review:
                item.setBackground(QColor(255, 243, 205))
            self.from_to_table.setItem(row_idx, col_idx, item)
        return row_idx

    def add_from_to_row(
        self,
        wire_number: str = "",
        from_ep: str = "L1",
        to_ep: str = "",
        sheet_rung: str = "page-001:1",
        confidence: float = 1.0,
    ) -> int:
        self._updating_from_to = True
        try:
            w_num = wire_number or f"W{self.from_to_table.rowCount() + 1}"
            idx = self._insert_from_to_table_row(
                w_num,
                from_ep,
                to_ep,
                sheet_rung,
                confidence,
                needs_review=False,
                source="user_added",
            )
        finally:
            self._updating_from_to = False
        return idx

    def _on_add_from_to_row(self) -> None:
        self.add_from_to_row()
        self._emit_from_to_edits()

    def remove_selected_from_to_row(self) -> None:
        sel = sorted({idx.row() for idx in self.from_to_table.selectedIndexes()}, reverse=True)
        if not sel and self.from_to_table.currentRow() >= 0:
            sel = [self.from_to_table.currentRow()]
        elif not sel and self.from_to_table.rowCount() > 0:
            sel = [self.from_to_table.rowCount() - 1]
        self._updating_from_to = True
        try:
            for r in sel:
                if 0 <= r < self.from_to_table.rowCount():
                    self.from_to_table.removeRow(r)
        finally:
            self._updating_from_to = False
        self._emit_from_to_edits()

    def _on_from_to_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_from_to:
            return
        row_idx = item.row()
        conf_item = self.from_to_table.item(row_idx, 4)
        if conf_item is not None and item.column() != 4:
            self._updating_from_to = True
            try:
                conf_item.setText("1.00")
                conf_item.setData(Qt.ItemDataRole.UserRole, 1.0)
                conf_item.setData(Qt.ItemDataRole.UserRole + 1, "user_edit")
                conf_item.setData(Qt.ItemDataRole.UserRole + 2, False)
            finally:
                self._updating_from_to = False

    def from_to_rows(self) -> list[dict]:
        rows: list[dict] = []
        for r in range(self.from_to_table.rowCount()):
            w_item = self.from_to_table.item(r, 0)
            f_item = self.from_to_table.item(r, 1)
            t_item = self.from_to_table.item(r, 2)
            s_item = self.from_to_table.item(r, 3)
            c_item = self.from_to_table.item(r, 4)
            w_num = (w_item.text() if w_item else "").strip() or f"W{r + 1}"
            f_ep = (f_item.text() if f_item else "").strip()
            t_ep = (t_item.text() if t_item else "").strip()
            if not f_ep or not t_ep:
                continue
            sr_text = (s_item.text() if s_item else "page-001:1").strip()
            if ":" in sr_text:
                sh_part, rg_part = sr_text.rsplit(":", 1)
                sheet_id = sh_part.strip() or "page-001"
                try:
                    rung = int(rg_part.strip())
                except ValueError:
                    rung = r + 1
            else:
                sheet_id = sr_text or "page-001"
                rung = r + 1
            conf = 1.0
            source = "user_edit"
            needs_review = False
            if c_item is not None:
                raw_c = c_item.data(Qt.ItemDataRole.UserRole)
                if isinstance(raw_c, (int, float)):
                    conf = float(raw_c)
                else:
                    try:
                        conf = float(c_item.text().split()[0])
                    except (ValueError, IndexError):
                        conf = 1.0
                raw_s = c_item.data(Qt.ItemDataRole.UserRole + 1)
                if raw_s:
                    source = str(raw_s)
                raw_nr = c_item.data(Qt.ItemDataRole.UserRole + 2)
                if isinstance(raw_nr, bool):
                    needs_review = raw_nr
                else:
                    needs_review = conf < 0.80
            f_tag, f_pin = (f_ep.rsplit(".", 1) if "." in f_ep else (f_ep, ""))
            t_tag, t_pin = (t_ep.rsplit(".", 1) if "." in t_ep else (t_ep, ""))
            rows.append(
                {
                    "wire_number": w_num,
                    "from": f_ep,
                    "from_tag": f_tag.strip(),
                    "from_pin": f_pin.strip(),
                    "to": t_ep,
                    "to_tag": t_tag.strip(),
                    "to_pin": t_pin.strip(),
                    "sheet_id": sheet_id,
                    "rung": rung,
                    "confidence": conf,
                    "source": source,
                    "needs_review": needs_review,
                }
            )
        return rows

    def _emit_from_to_edits(self) -> None:
        self.from_to_edited.emit(self.from_to_rows())

    def _on_rebuild_clean_ladder_clicked(self) -> None:
        self.set_layout_mode("ladder")
        rows = self.from_to_rows()
        if rows:
            self.from_to_edited.emit(rows)
        self.rebuild_clean_ladder_requested.emit("ladder")

    def set_leftover_questions(self, rows: list[dict] | None, *, status: str = "") -> None:
        """Populate the leftover-questions tab after cleanup / challenge."""

        from panelcad.importer.leftover_questions import kind_label

        self._leftovers = [dict(row) for row in (rows or []) if isinstance(row, dict)]
        self.leftover_list.clear()
        for row in self._leftovers:
            label_kind = str(row.get("kind_label") or kind_label(str(row.get("kind") or "")))
            ref = str(row.get("ref") or row.get("tag") or row.get("wire_id") or "").strip()
            question = str(row.get("question") or "").strip()
            title = f"{label_kind}" + (f" — {ref}" if ref else "")
            item = QListWidgetItem(title)
            item.setToolTip(question or title)
            self.leftover_list.addItem(item)
        title = f"Leftover questions ({len(self._leftovers)})"
        self.tabs.setTabText(self._leftover_tab_index, title)
        if status:
            self.leftover_status.setText(status)
        elif not self._leftovers:
            self.leftover_status.setText("No open leftover questions.")
            self.leftover_kind.setText("")
            self.leftover_detail.setText("No leftover questions. Rebuild a scan to list blockers.")
            self.leftover_scan.setPixmap(QPixmap())
            self.leftover_scan.setText("No scan crop")
            self.leftover_sheet.setPixmap(QPixmap())
            self.leftover_sheet.setText("No sheet crop")
            self._clear_leftover_choices()
        if self._leftovers:
            self.leftover_list.setCurrentRow(0)
            self.tabs.setCurrentIndex(self._leftover_tab_index)

    def leftover_questions(self) -> list[dict]:
        return list(self._leftovers)

    def _show_leftover(self, row: int) -> None:
        from panelcad.importer.leftover_questions import kind_label

        self._leftover_index = row
        self._clear_leftover_choices()
        self.leftover_edit.clear()
        if row < 0 or row >= len(self._leftovers):
            self.leftover_kind.setText("")
            return
        item = self._leftovers[row]
        label_kind = str(item.get("kind_label") or kind_label(str(item.get("kind") or "")))
        self.leftover_kind.setText(label_kind)
        self.leftover_detail.setText(str(item.get("question") or ""))
        self._set_crop_label(self.leftover_scan, str(item.get("scan_crop") or ""), "No scan crop")
        self._set_crop_label(self.leftover_sheet, str(item.get("sheet_crop") or ""), "No sheet crop")
        self._fill_leftover_choice_grid(item.get("choices") or [], self.leftover_choices_lay)

    def _fill_leftover_choice_grid(self, choices: list, layout: QGridLayout) -> None:
        columns = 3
        for index, choice in enumerate(choices):
            text = str(choice)
            button = QPushButton(text)
            button.setMinimumHeight(40)
            button.setMinimumWidth(max(96, 10 * len(text) + 24))
            button.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)
            button.setObjectName("leftoverChoice")
            if getattr(self, "_leftover_btn_style", ""):
                button.setStyleSheet(self._leftover_btn_style)
            button.clicked.connect(lambda _checked=False, value=text: self._emit_leftover(value))
            layout.addWidget(button, index // columns, index % columns)
            self._leftover_choice_buttons.append(button)

    def _clear_leftover_choices(self) -> None:
        while self.leftover_choices_lay.count():
            taken = self.leftover_choices_lay.takeAt(0)
            widget = taken.widget()
            if widget is not None:
                widget.deleteLater()
        self._leftover_choice_buttons = []

    def _set_crop_label(self, label: QLabel, path: str, empty: str) -> None:
        if path and Path(path).is_file():
            pix = QPixmap(path)
            if not pix.isNull():
                target_w = max(220, label.width() or 280)
                target_h = max(120, label.height() or 160)
                label.setPixmap(
                    pix.scaled(
                        target_w,
                        target_h,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                label.setText("")
                return
        label.setPixmap(QPixmap())
        label.setText(empty)

    def _emit_leftover(self, value: str) -> None:
        if self._leftover_index < 0 or self._leftover_index >= len(self._leftovers):
            return
        question_id = str(self._leftovers[self._leftover_index].get("id") or "")
        if question_id and value:
            self.leftover_answer_requested.emit(question_id, value)

    def _apply_leftover_typed(self) -> None:
        text = self.leftover_edit.text().strip()
        if text:
            self._emit_leftover(text)

    def _open_leftover_dialog(self) -> None:
        """Full-size answer dialog when the Print review dock is too short."""

        from panelcad.importer.leftover_questions import kind_label

        if self._leftover_index < 0 or self._leftover_index >= len(self._leftovers):
            return
        item = self._leftovers[self._leftover_index]
        dialog = QDialog(self)
        dialog.setObjectName("leftoverAnswerDialog")
        dialog.setWindowTitle("Leftover question")
        dialog.resize(720, 520)
        root = QVBoxLayout(dialog)
        kind = QLabel(str(item.get("kind_label") or kind_label(str(item.get("kind") or ""))))
        kind.setWordWrap(True)
        question = QLabel(str(item.get("question") or ""))
        question.setObjectName("leftoverDialogQuestion")
        question.setWordWrap(True)
        root.addWidget(kind)
        root.addWidget(question)
        choices_host = QWidget()
        choices_lay = QGridLayout(choices_host)
        choices_lay.setHorizontalSpacing(8)
        choices_lay.setVerticalSpacing(8)
        chosen: list[str] = []

        def pick(value: str) -> None:
            chosen.append(value)
            dialog.accept()

        columns = 3
        for index, choice in enumerate(item.get("choices") or []):
            text = str(choice)
            button = QPushButton(text)
            button.setObjectName("leftoverChoice")
            button.setMinimumHeight(40)
            button.setMinimumWidth(max(96, 10 * len(text) + 24))
            if getattr(self, "_leftover_btn_style", ""):
                button.setStyleSheet(self._leftover_btn_style)
            button.clicked.connect(lambda _checked=False, value=text: pick(value))
            choices_lay.addWidget(button, index // columns, index % columns)
        root.addWidget(choices_host)
        typed = QLineEdit()
        typed.setObjectName("leftoverTypedAnswer")
        typed.setPlaceholderText("Typed value (fallback)")
        typed.setMinimumHeight(32)
        root.addWidget(typed)
        pics = QHBoxLayout()
        scan = QLabel("No scan crop")
        scan.setObjectName("leftoverScanCrop")
        scan.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scan.setMinimumSize(280, 160)
        sheet = QLabel("No sheet crop")
        sheet.setObjectName("leftoverSheetCrop")
        sheet.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sheet.setMinimumSize(280, 160)
        if getattr(self, "_leftover_btn_style", ""):
            # Reuse crop border colours from the panel theme.
            scan.setStyleSheet(self.leftover_scan.styleSheet())
            sheet.setStyleSheet(self.leftover_sheet.styleSheet())
        self._set_crop_label(scan, str(item.get("scan_crop") or ""), "No scan crop")
        self._set_crop_label(sheet, str(item.get("sheet_crop") or ""), "No sheet crop")
        scan_box = QVBoxLayout()
        scan_box.addWidget(QLabel("Original scan crop"))
        scan_box.addWidget(scan, stretch=1)
        sheet_box = QVBoxLayout()
        sheet_box.addWidget(QLabel("Rebuilt sheet highlight"))
        sheet_box.addWidget(sheet, stretch=1)
        pics.addLayout(scan_box, 1)
        pics.addLayout(sheet_box, 1)
        root.addLayout(pics, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        root.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            value = chosen[0] if chosen else typed.text().strip()
            if value:
                self._emit_leftover(value)
