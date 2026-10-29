"""Wizard: intake real panel photos, set scale, confirm parts, apply to the print."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.reconstruct.geometry import mm_per_px
from panelcad.reconstruct.layout import target_sheet_kind
from panelcad.reconstruct.models import PHOTO_ROLES, ObservedItem, item_is_layout_ready
from panelcad.reconstruct.store import resolve_path
from panelcad.reconstruct.vision import NO_AI_MESSAGE

_STEP_HEADERS = (
    "Step 1 of 5: Add photos. Next only changes pages.",
    "Step 2 of 5: Set scale. Required before parts can be placed.",
    "Step 3 of 5: Read labels. Confirm a catalog part or a measured envelope.",
    "Step 4 of 5: Floor checklist. Next does not write parts to the drawing.",
    "Step 5 of 5: Review. Apply to print writes confirmed parts to the drawing.",
)

_NO_AI = NO_AI_MESSAGE

_IDENTIFY_INTRO = (
    "No parts yet. Read labels on photos. "
    "If no AI is connected, labels can't be read automatically. "
    "Connect AI in Settings, or add parts by hand from the library onto the sheet. "
    "Next only changes pages."
)

_SKIP_TEXT = {
    "unconfirmed": "not confirmed — pick a catalog part or confirm a measured envelope",
    "no_footprint": "catalog match has no footprint — confirm a measured envelope",
    "unscaled": "the photo has no scale — set scale before this part can be placed",
    "unplaced": "the photo has no scale — set scale before this part can be placed",
}


class ScalePhotoView(QWidget):
    """The scale photo. Clicks are photo pixels, not typed coordinates."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pix = QPixmap()
        self._marks: list[tuple[float, float]] = []
        self._on_click = None
        self.setMinimumSize(360, 240)
        self.setAutoFillBackground(True)

    def set_photo(self, path: str) -> None:
        self._pix = QPixmap(path) if path else QPixmap()
        self.update()

    def set_marks(self, marks: list[tuple[float, float]]) -> None:
        self._marks = list(marks)
        self.update()

    def mark_photo(self, x: float, y: float) -> None:
        if self._on_click is not None:
            self._on_click(float(x), float(y))

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        mapped = self._from_widget(float(event.position().x()), float(event.position().y()))
        if mapped is not None:
            self.mark_photo(*mapped)
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        if self._pix.isNull():
            painter.end()
            return
        target, scaled, origin = self._fitted()
        _ = target
        painter.drawPixmap(int(origin[0]), int(origin[1]), scaled)
        if self._pix.width() and self._pix.height():
            sx = scaled.width() / self._pix.width()
            sy = scaled.height() / self._pix.height()
            painter.setPen(QPen(QColor("#e6a817"), 2))
            for x, y in self._marks:
                px = origin[0] + x * sx
                py = origin[1] + y * sy
                painter.drawLine(int(px - 6), int(py), int(px + 6), int(py))
                painter.drawLine(int(px), int(py - 6), int(px), int(py + 6))
        painter.end()

    def _fitted(self):
        target = self.rect().adjusted(4, 4, -4, -4)
        scaled = self._pix.scaled(
            target.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        origin = (
            target.x() + (target.width() - scaled.width()) / 2,
            target.y() + (target.height() - scaled.height()) / 2,
        )
        return target, scaled, origin

    def _from_widget(self, x: float, y: float) -> tuple[float, float] | None:
        if self._pix.isNull() or self._pix.width() <= 0 or self._pix.height() <= 0:
            return None
        _target, scaled, origin = self._fitted()
        if scaled.width() <= 0 or scaled.height() <= 0:
            return None
        local_x = x - origin[0]
        local_y = y - origin[1]
        if local_x < 0 or local_y < 0 or local_x > scaled.width() or local_y > scaled.height():
            return None
        return (local_x / scaled.width() * self._pix.width(), local_y / scaled.height() * self._pix.height())


class ReconstructApplyResult:
    """What Apply to print did. The main window uses this to switch sheets."""

    def __init__(
        self,
        *,
        summary: str,
        sheets: list[str],
        placed: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        needs_info: list[str],
        applied: bool,
    ) -> None:
        self.summary = summary
        self.sheets = list(sheets)
        self.placed = placed
        self.skipped = skipped
        self.needs_info = needs_info
        self.applied = applied

    @property
    def sheet_id(self) -> str:
        return self.sheets[0] if self.sheets else ""


class ReconstructWizard(QDialog):
    def __init__(self, session: Session, parent=None, *, vision: Any = None) -> None:
        super().__init__(parent)
        self.session = session
        self.vision = vision
        self.apply_result: ReconstructApplyResult | None = None
        self._last_identify: dict[str, Any] | None = None
        self._accepting = False
        self._in_reject = False
        self.setWindowTitle("Reconstruct from photos")
        self.resize(820, 620)
        self._page = 0
        self._snap_project = session.project.model_dump(mode="json")
        self._snap_undo = [dict(item) for item in session._undo]
        self._snap_redo = [dict(item) for item in session._redo]
        layout = QVBoxLayout(self)
        hint = QLabel(
            "Primary input is phone photos of the physical panel. "
            "An old drawing is optional and only used to flag mismatches. "
            "Add photos → Set scale → Read labels → Confirm → Apply to print "
            "is what writes to the drawing. Next only changes pages."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.step_header = QLabel()
        self.step_header.setWordWrap(True)
        layout.addWidget(self.step_header)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._photos_page())
        self.stack.addWidget(self._scale_page())
        self.stack.addWidget(self._identify_page())
        self.stack.addWidget(self._checklist_page())
        self.stack.addWidget(self._review_page())
        layout.addWidget(self.stack, 1)
        nav = QHBoxLayout()
        self.back = QPushButton("Back")
        self.cancel = QPushButton("Cancel")
        self.next = QPushButton("Next")
        self.back.clicked.connect(self._back)
        self.cancel.clicked.connect(self._cancel)
        self.next.clicked.connect(self._next)
        nav.addWidget(self.back)
        nav.addWidget(self.cancel)
        nav.addStretch()
        nav.addWidget(self.next)
        layout.addLayout(nav)
        self._refresh_photos()
        self._sync_nav()

    def _photos_page(self) -> QWidget:
        from panelcad.gui.photo_viewer import PhotoView

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Panel photos (Capture Phase — check site-exit readiness before leaving cabinet)"))
        self.photo_list = QListWidget()
        self.photo_list.setMaximumHeight(115)
        self.photo_list.currentRowChanged.connect(lambda *_a: self.refresh_capture_feedback())
        layout.addWidget(self.photo_list)

        row = QHBoxLayout()
        add = QPushButton("Add photos…")
        add.clicked.connect(self._add_photos)
        self.role = QComboBox()
        for role in PHOTO_ROLES:
            self.role.addItem(role.replace("_", " "), role)
        tag = QPushButton("Tag selected")
        tag.clicked.connect(self._tag_selected)
        print_btn = QPushButton("Old drawing (optional)…")
        print_btn.clicked.connect(self._attach_print)
        self.capture_check_btn = QPushButton("Check Site-Exit Readiness (Analyze Capture)")
        self.capture_check_btn.clicked.connect(self.run_capture_site_exit_check)
        self.phone_capture_btn = QPushButton("📱 Phone Capture Feed…")
        self.phone_capture_btn.clicked.connect(self.open_mobile_capture_server_dialog)
        row.addWidget(add)
        row.addWidget(self.role)
        row.addWidget(tag)
        row.addWidget(print_btn)
        row.addWidget(self.capture_check_btn)
        row.addWidget(self.phone_capture_btn)
        layout.addLayout(row)

        self.site_exit_banner = QLabel("Add a cabinet photo and click 'Check Site-Exit Readiness' before leaving the site.")
        self.site_exit_banner.setWordWrap(True)
        self.site_exit_banner.setStyleSheet(
            "font-weight: 700; font-size: 12px; padding: 6px 10px; border-radius: 4px; background: #1e293b; color: #f8fafc;"
        )
        layout.addWidget(self.site_exit_banner)

        cap_split = QHBoxLayout()
        self.capture_feedback_view = PhotoView()
        self.capture_feedback_view.setMinimumSize(420, 260)
        self.capture_feedback_view.region_drawn.connect(self._on_capture_region_drawn)
        cap_split.addWidget(self.capture_feedback_view, 3)

        right_col = QVBoxLayout()
        right_col.addWidget(QLabel("Before You Leave Site — Required Close-Ups & Context:"))
        self.shot_list = QListWidget()
        right_col.addWidget(self.shot_list, 1)

        cap_btns = QHBoxLayout()
        self.capture_draw_missed_btn = QPushButton("＋ Draw Box on Missed Device")
        self.capture_draw_missed_btn.setCheckable(True)
        self.capture_draw_missed_btn.toggled.connect(
            lambda checked: self.capture_feedback_view.set_draw_missed_box_mode(bool(checked))
        )
        self.capture_snap_boxes_btn = QPushButton("✨ Auto-Snap Boxes")
        self.capture_snap_boxes_btn.clicked.connect(self._on_capture_snap_all_boxes)
        self.capture_link_closeup_btn = QPushButton("Attach Close-Up…")
        self.capture_link_closeup_btn.clicked.connect(self._on_capture_attach_closeup)
        self.capture_overlay_btn = QPushButton("Open Full Feedback Overlay…")
        self.capture_overlay_btn.clicked.connect(lambda: self.open_photo_feedback_overlay())
        cap_btns.addWidget(self.capture_draw_missed_btn)
        cap_btns.addWidget(self.capture_snap_boxes_btn)
        cap_btns.addWidget(self.capture_link_closeup_btn)
        cap_btns.addWidget(self.capture_overlay_btn)
        right_col.addLayout(cap_btns)

        cap_split.addLayout(right_col, 2)
        layout.addLayout(cap_split, 1)
        return page

    def _scale_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            QLabel(
                "Easiest: click the 4 corners of a part you know the size of. "
                "Or click a 35 mm DIN rail and a second length (a duct, or the other side of the part). "
                "Click the photo — you do not type pixel coordinates."
            )
        )
        self.scale_photo = QComboBox()
        self.scale_photo.currentIndexChanged.connect(lambda *_a: self._show_scale_photo())
        layout.addWidget(self.scale_photo)
        self.scale_view = ScalePhotoView()
        self.scale_view._on_click = self._scale_clicked
        self._scale_mode = "corners"
        layout.addWidget(self.scale_view, 1)
        modes = QHBoxLayout()
        self.scale_mode_corners = QPushButton("4 corners")
        self.scale_mode_len1 = QPushButton("Length 1")
        self.scale_mode_len2 = QPushButton("Length 2")
        self.scale_mode_corners.clicked.connect(lambda: self._set_scale_mode("corners"))
        self.scale_mode_len1.clicked.connect(lambda: self._set_scale_mode("len1"))
        self.scale_mode_len2.clicked.connect(lambda: self._set_scale_mode("len2"))
        modes.addWidget(self.scale_mode_corners)
        modes.addWidget(self.scale_mode_len1)
        modes.addWidget(self.scale_mode_len2)
        layout.addLayout(modes)
        form = QFormLayout()
        self.scale_kind = QComboBox()
        for kind, label in (
            ("face", "Easiest: 4 corners of a part you know (width × height)"),
            ("backpanel", "These corners are the backpanel"),
            ("din_rail", "35 mm DIN rail (one length)"),
            ("catalog_part", "Catalog part"),
            ("wire_duct", "Wire duct width"),
            ("measured", "Measured length (duct, enclosure inside, …)"),
        ):
            self.scale_kind.addItem(label, kind)
        self.corners = QLineEdit()
        self.corners.setPlaceholderText("filled when you click the 4 corners")
        self.corners_are_backpanel = QCheckBox("These corners are the backpanel")
        self.corners_are_backpanel.setToolTip(
            "Check this only when the four clicks are the enclosure face. "
            "A part's corners set the scale and do not resize the enclosure."
        )
        self.p1 = QLineEdit()
        self.p2 = QLineEdit()
        self.p1.setPlaceholderText("click length 1 start")
        self.p2.setPlaceholderText("click length 1 end")
        self.p3 = QLineEdit()
        self.p4 = QLineEdit()
        self.p3.setPlaceholderText("click length 2 start")
        self.p4.setPlaceholderText("click length 2 end")
        form.addRow("Length 1 start", self.p1)
        form.addRow("Length 1 end", self.p2)
        form.addRow("Length 2 start", self.p3)
        form.addRow("Length 2 end", self.p4)
        self.known = QDoubleSpinBox()
        self.known.setRange(0, 2000)
        self.known.setSuffix(" mm")
        self.known.setSpecialValueText("unset")
        self.known_height = QDoubleSpinBox()
        self.known_height.setRange(0, 2000)
        self.known_height.setSuffix(" mm")
        self.known_height.setSpecialValueText("unset")
        self.catalog = QLineEdit()
        form.addRow("Reference", self.scale_kind)
        form.addRow("Corners", self.corners)
        form.addRow("", self.corners_are_backpanel)
        form.addRow("Length 1", self.known)
        form.addRow("Length 2 / height", self.known_height)
        form.addRow("Catalog (if used)", self.catalog)
        layout.addLayout(form)
        apply_row = QHBoxLayout()
        apply = QPushButton("Set scale")
        apply.clicked.connect(self._apply_scale)
        clear = QPushButton("Clear scale")
        clear.clicked.connect(self._clear_scale)
        apply_row.addWidget(apply)
        apply_row.addWidget(clear)
        layout.addLayout(apply_row)
        self.scale_status = QLabel()
        self.scale_status.setWordWrap(True)
        layout.addWidget(self.scale_status)
        return page

    def _identify_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Part identification — unconfirmed items stay in Problems."))
        self.identify_note = QLabel(_IDENTIFY_INTRO)
        self.identify_note.setWordWrap(True)
        layout.addWidget(self.identify_note)
        run = QPushButton("Read labels on photos")
        run.clicked.connect(self._identify)
        layout.addWidget(run)
        self.item_list = QListWidget()
        self.item_list.currentItemChanged.connect(lambda *_a: self._fill_identify_fields())
        layout.addWidget(self.item_list, 1)
        form = QFormLayout()
        self.confirm_catalog = QLineEdit()
        self.confirm_tag = QLineEdit()
        self.env_w = QDoubleSpinBox()
        self.env_h = QDoubleSpinBox()
        self.env_d = QDoubleSpinBox()
        from panelcad.library.panel import max_confirmed_envelope_mm

        envelope_cap = max(2000.0, max_confirmed_envelope_mm())
        for box in (self.env_w, self.env_h, self.env_d):
            box.setRange(0, envelope_cap)
            box.setSuffix(" mm")
            box.setSpecialValueText("")
            box.setValue(0)
        self.env_mount = QComboBox()
        for mount in ("din_rail", "backplate", "door", "field"):
            self.env_mount.addItem(mount.replace("_", " "), mount)
        self.env_lib = QLineEdit()
        self.env_lib.setPlaceholderText("symbol for this type, if you know it")
        form.addRow("Catalog", self.confirm_catalog)
        form.addRow("Tag", self.confirm_tag)
        form.addRow("Size (mm) from the part or datasheet: W, H, D", QLabel(""))
        form.addRow("W", self.env_w)
        form.addRow("H", self.env_h)
        form.addRow("D", self.env_d)
        form.addRow("Mount", self.env_mount)
        form.addRow("Symbol", self.env_lib)
        layout.addLayout(form)
        row = QHBoxLayout()
        cat = QPushButton("Confirm catalog")
        env = QPushButton("Confirm envelope")
        place = QPushButton("Place confirmed")
        cat.clicked.connect(self._confirm_catalog)
        env.clicked.connect(self._confirm_envelope)
        place.clicked.connect(self._place)
        row.addWidget(cat)
        row.addWidget(env)
        row.addWidget(place)
        layout.addLayout(row)
        feedback_row = QHBoxLayout()
        self.photo_feedback_btn = QPushButton("Photo Feedback Overlay / Add Missed Device…")
        self.export_feedback_btn = QPushButton("Export Annotated Photo…")
        self.photo_feedback_btn.clicked.connect(lambda: self.open_photo_feedback_overlay())
        self.export_feedback_btn.clicked.connect(self._export_photo_feedback_dialog)
        feedback_row.addWidget(self.photo_feedback_btn)
        feedback_row.addWidget(self.export_feedback_btn)
        layout.addLayout(feedback_row)
        self.place_note = QLabel(
            "Place confirmed writes layout-ready parts. "
            "Each skipped part is listed here with the reason."
        )
        self.place_note.setWordWrap(True)
        layout.addWidget(self.place_note)
        return page

    def _checklist_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Floor checklist"))
        self.step_list = QListWidget()
        layout.addWidget(self.step_list, 1)
        self.answer = QLineEdit()
        self.answer.setPlaceholderText("Type where the wire lands, like K1:A1")
        layout.addWidget(self.answer)
        row = QHBoxLayout()
        send = QPushButton("Record answer")
        send.clicked.connect(self._answer)
        cmp = QPushButton("Compare attached print")
        cmp.clicked.connect(self._compare)
        self.rebuild_schematic_btn = QPushButton("Rebuild Clean Schematic")
        self.rebuild_schematic_btn.clicked.connect(self._rebuild_schematic)
        row.addWidget(send)
        row.addWidget(cmp)
        row.addWidget(self.rebuild_schematic_btn)
        layout.addLayout(row)
        self.wire_schedule_summary = QLabel("From-To Wire Landings: 0 traced")
        self.wire_schedule_summary.setWordWrap(True)
        layout.addWidget(self.wire_schedule_summary)
        return page

    def _review_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.review_scroll = QScrollArea()
        self.review_scroll.setWidgetResizable(True)
        self.review_summary = QLabel("Review what will be written to the drawing.")
        self.review_summary.setWordWrap(True)
        self.review_summary.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.review_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.review_scroll.setWidget(self.review_summary)
        layout.addWidget(self.review_scroll, 1)
        return page

    def _add_photos(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Panel photos",
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)",
        )
        if not paths:
            return
        try:
            self.session.reconstruct_add_photos(paths, role=str(self.role.currentData() or "other"))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Photos", str(exc))
            return
        self._refresh_photos()

    def _tag_selected(self) -> None:
        item = self.photo_list.currentItem()
        if item is None:
            return
        try:
            self.session.reconstruct_tag_photo(item.data(Qt.ItemDataRole.UserRole), str(self.role.currentData() or "other"))
        except (KeyError, ValueError) as exc:
            QMessageBox.warning(self, "Tag photo", str(exc))
            return
        self._refresh_photos()

    def _attach_print(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Secondary print",
            "",
            "Prints (*.pcad.yaml *.yaml *.yml *.pdf *.png *.jpg)",
        )
        if not path:
            return
        try:
            self.session.reconstruct_attach_print(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Print", str(exc))

    def _apply_scale(self) -> None:
        photo_id = str(self.scale_photo.currentData() or "")
        if not photo_id:
            return
        try:
            if self.corners.text().strip():
                pts = []
                for token in self.corners.text().replace(";", " ").split():
                    pts.append(_xy(token))
                self.session.reconstruct_set_corners(photo_id, pts)
            kind = str(self.scale_kind.currentData() or "measured")
            height = float(self.known_height.value() or 0)
            known = float(self.known.value() or 0)
            have_first = bool(self.p1.text().strip() and self.p2.text().strip())
            have_second = bool(self.p3.text().strip() and self.p4.text().strip())
            if kind in {"face", "backpanel"}:
                if known <= 0 or height <= 0:
                    raise ValueError("enclosure / face scale needs both width and height")
                dummy = (0.0, 0.0)
                result = self.session.reconstruct_set_scale(
                    photo_id,
                    "backpanel" if kind == "backpanel" or self.corners_are_backpanel.isChecked() else "face",
                    _xy(self.p1.text()) if self.p1.text().strip() else dummy,
                    _xy(self.p2.text()) if self.p2.text().strip() else dummy,
                    known_mm=known,
                    known_height_mm=height,
                    catalog=self.catalog.text().strip(),
                    backpanel=kind == "backpanel" or self.corners_are_backpanel.isChecked(),
                )
            else:
                if not have_first:
                    raise ValueError(
                        "Mark a length you know on the photo first (rail, duct, or a part's corners)."
                    )
                if kind != "din_rail" and known <= 0:
                    raise ValueError("enter a known length; untouched fields do not write a scale")
                result = self.session.reconstruct_set_scale(
                    photo_id,
                    kind,
                    _xy(self.p1.text()),
                    _xy(self.p2.text()),
                    known_mm=known if known > 0 else None,
                    catalog=self.catalog.text().strip(),
                )
                if have_second and height > 0:
                    result = self.session.reconstruct_set_scale(
                        photo_id,
                        "measured" if kind == "din_rail" else kind,
                        _xy(self.p3.text()),
                        _xy(self.p4.text()),
                        known_mm=height,
                    )
        except (KeyError, ValueError) as exc:
            QMessageBox.warning(self, "Scale", str(exc))
            return
        if result.get("scaled"):
            warning = str(result.get("message") or "").strip()
            if warning:
                self.scale_status.setText(warning)
            else:
                ratio = result.get("mm_per_px")
                self.scale_status.setText(f"Scale set ({ratio} mm/px).")
        else:
            self.scale_status.setText(
                str(result.get("message") or "Scale not set: mark two lengths, or the 4 corners of a known part.")
            )

    def _clear_scale(self) -> None:
        photo_id = str(self.scale_photo.currentData() or "")
        if not photo_id:
            return
        try:
            result = self.session.reconstruct_clear_scale(photo_id)
        except (KeyError, ValueError) as exc:
            QMessageBox.warning(self, "Scale", str(exc))
            return
        self.scale_status.setText(str(result.get("message") or "scale cleared"))

    def _identify(self) -> None:
        try:
            kwargs: dict[str, Any] = {}
            if self.vision is not None:
                kwargs["vision"] = self.vision
            result = self.session.reconstruct_identify(**kwargs)
        except ValueError as exc:
            QMessageBox.warning(self, "Identify", str(exc))
            return
        self._last_identify = result
        self._refresh_items()
        self._show_identify_feedback(result)

    def _show_identify_feedback(self, result: dict[str, Any]) -> None:
        text = _identify_feedback(result, self._part_items(), self._unreadable_lines())
        self.identify_note.setText(text)
        self.identify_note.setVisible(bool(text.strip()))

    def _selected_item_id(self) -> str:
        item = self.item_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""

    def open_photo_feedback_overlay(
        self,
        photo_id: str | None = None,
        *,
        selected_item_id: str | None = None,
        modal: bool = True,
    ) -> Any:
        from panelcad.gui.photo_viewer import PhotoFeedbackDialog

        chosen_item_id = selected_item_id or self._selected_item_id() or None
        dlg = PhotoFeedbackDialog(
            self.session,
            photo_id=photo_id,
            selected_item_id=chosen_item_id,
            parent=self,
        )
        if modal:
            dlg.exec()
            self._refresh_items()
            self._refresh_steps()
            self._refresh_review()
            self.refresh_capture_feedback()
        return dlg

    def open_mobile_capture_server_dialog(self, *, modal: bool = True) -> Any:
        from panelcad.gui.photo_viewer import MobileCaptureServerDialog

        dlg = MobileCaptureServerDialog(
            self.session,
            on_session_updated=self._on_mobile_session_synced,
            parent=self,
        )
        if modal:
            dlg.exec()
            self._on_mobile_session_synced()
        return dlg

    def _on_mobile_session_synced(self) -> None:
        self._refresh_photos()
        self._refresh_items()
        self._refresh_steps()
        self._refresh_review()
        self.refresh_capture_feedback()

    def _export_photo_feedback_dialog(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            "Export Annotated Cabinet Photo Feedback",
            "cabinet-photo-feedback.png",
            "PNG Images (*.png)",
        )
        if not chosen:
            return
        try:
            res = self.session.reconstruct_export_photo_feedback(
                output_path=chosen,
                selected_item_id=self._selected_item_id() or None,
            )
            self.place_note.setText(f"Exported annotated photo feedback to {res['output_path']}")
        except (KeyError, ValueError, OSError) as exc:
            QMessageBox.warning(self, "Export Photo Feedback", str(exc))

    def _confirm_catalog(self) -> None:
        item_id = self._selected_item_id()
        if not item_id:
            return
        try:
            self.session.reconstruct_confirm_item(
                item_id,
                catalog=self.confirm_catalog.text().strip(),
                tag=self.confirm_tag.text().strip(),
            )
        except (KeyError, ValueError) as exc:
            QMessageBox.warning(self, "Confirm", str(exc))
            return
        self._identify()

    def _confirm_envelope(self) -> None:
        item_id = self._selected_item_id()
        if not item_id:
            return
        try:
            self.session.reconstruct_confirm_item(
                item_id,
                envelope_w_mm=self.env_w.value(),
                envelope_h_mm=self.env_h.value(),
                envelope_d_mm=self.env_d.value(),
                mount=str(self.env_mount.currentData() or ""),
                tag=self.confirm_tag.text().strip(),
                library_id=self.env_lib.text().strip(),
            )
        except (KeyError, ValueError) as exc:
            QMessageBox.warning(self, "Envelope", str(exc))
            return
        self._identify()

    def _place(self) -> None:
        try:
            result = self.session.reconstruct_place_confirmed()
        except ValueError as exc:
            QMessageBox.warning(self, "Place", str(exc))
            return
        text = _place_text(result, parts=self._part_items(), no_ai=self._no_ai())
        self.place_note.setText(text)
        QMessageBox.information(self, "Place", text)
        self._refresh_items()

    def _answer(self) -> None:
        item = self.step_list.currentItem()
        if item is None:
            return
        try:
            self.session.reconstruct_answer(str(item.data(Qt.ItemDataRole.UserRole) or ""), self.answer.text())
        except (KeyError, ValueError) as exc:
            QMessageBox.warning(self, "Answer", str(exc))
            return
        self._refresh_steps()

    def _compare(self) -> None:
        try:
            result = self.session.reconstruct_compare_print()
        except ValueError as exc:
            QMessageBox.warning(self, "Compare", str(exc))
            return
        QMessageBox.information(self, "Compare", f"{len(result.get('mismatches') or [])} mismatches")
        self._refresh_steps()

    def _refresh_photos(self) -> None:
        self.photo_list.clear()
        self.scale_photo.blockSignals(True)
        self.scale_photo.clear()
        for photo in self.session.reconstruct_list_photos().get("photos") or []:
            name = str(photo.get("original_name") or "").strip() or Path(str(photo.get("path") or "")).name
            role = str(photo.get("role") or "").replace("_", " ")
            row = QListWidgetItem(f"{name}  {role}")
            row.setData(Qt.ItemDataRole.UserRole, photo.get("id"))
            path = resolve_path(str(photo.get("path") or ""), self.session.path)
            if path.is_file():
                pix = QPixmap(str(path))
                if not pix.isNull():
                    row.setIcon(QIcon(pix.scaled(48, 36, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)))
            self.photo_list.addItem(row)
            self.scale_photo.addItem(name, photo.get("id"))
        self.scale_photo.blockSignals(False)
        if self.photo_list.count() > 0 and self.photo_list.currentRow() < 0:
            self.photo_list.setCurrentRow(0)
        self._show_scale_photo()
        self.refresh_capture_feedback()

    def _selected_capture_photo_id(self) -> str | None:
        cur = self.photo_list.currentItem()
        if cur is not None:
            pid = str(cur.data(Qt.ItemDataRole.UserRole) or "")
            if pid:
                return pid
        photos = self.session.reconstruct_list_photos().get("photos") or []
        for p_entry in photos:
            if p_entry.get("role") == "backpanel":
                return str(p_entry.get("id") or "")
        return str(photos[0].get("id") or "") if photos else None

    def run_capture_site_exit_check(self) -> dict[str, Any]:
        """Run label/device detection if needed and refresh Step 1 Site-Exit Readiness."""
        pid = self._selected_capture_photo_id()
        if not pid:
            return {}
        store_items = self.session.reconstruct_status().get("items") or []
        if not any(i.get("photo_id") == pid for i in store_items):
            try:
                kwargs: dict[str, Any] = {"photo_id": pid}
                if self.vision is not None:
                    kwargs["vision"] = self.vision
                self.session.reconstruct_identify(**kwargs)
            except Exception:
                pass
        return self.refresh_capture_feedback(photo_id=pid)

    def refresh_capture_feedback(self, photo_id: str | None = None) -> dict[str, Any]:
        from panelcad.reconstruct.feedback import build_photo_feedback_report, render_annotated_cabinet_photo

        pid = photo_id or self._selected_capture_photo_id()
        if not hasattr(self, "site_exit_banner"):
            return {}
        if not pid:
            self.site_exit_banner.setText(
                "Add a cabinet photo and click 'Check Site-Exit Readiness' before leaving the site."
            )
            self.site_exit_banner.setStyleSheet(
                "font-weight: 700; font-size: 12px; padding: 6px 10px; border-radius: 4px; background: #1e293b; color: #f8fafc;"
            )
            self.shot_list.clear()
            return {}
        try:
            report = build_photo_feedback_report(self.session, photo_id=pid)
        except Exception:
            return {}

        banner = str(report.get("site_exit_banner") or "")
        safe = bool(report.get("safe_to_leave_site"))
        self.site_exit_banner.setText(banner)
        if safe:
            self.site_exit_banner.setStyleSheet(
                "font-weight: 700; font-size: 12px; padding: 6px 10px; border-radius: 4px; background: #064e3b; color: #ecfdf5;"
            )
        else:
            self.site_exit_banner.setStyleSheet(
                "font-weight: 700; font-size: 12px; padding: 6px 10px; border-radius: 4px; background: #7f1d1d; color: #fffbeb;"
            )

        self.shot_list.clear()
        shots = report.get("on_site_shot_list") or []
        if shots:
            for s in shots:
                item = QListWidgetItem(f"⚠ {s}")
                item.setForeground(QColor("#fde047"))
                self.shot_list.addItem(item)
        else:
            item = QListWidgetItem("✓ All detected devices have legible context — safe to leave site.")
            item.setForeground(QColor("#86efac"))
            self.shot_list.addItem(item)

        img = render_annotated_cabinet_photo(self.session, photo_id=pid, include_banner=True)
        pw = int(report.get("width_px") or img.width())
        ui_scale = max(1.0, min(3.2, pw / 1280.0))
        top_off = int(round(58 * ui_scale))
        self.capture_feedback_view.set_annotated_feedback(
            img,
            source_width=pw,
            source_height=int(report.get("height_px") or max(1, img.height() - top_off)),
            boxes=report.get("boxes") or [],
            banner_top_offset=top_off,
        )
        return report

    def _on_capture_region_drawn(self, region: dict[str, float]) -> None:
        pid = self._selected_capture_photo_id()
        if not pid:
            return
        self.capture_draw_missed_btn.setChecked(False)
        self.session.reconstruct_add_missed_device(
            pid,
            region,
            note="Missed device boxed during site capture",
            snap_to_edges=True,
        )
        self._refresh_items()
        self.refresh_capture_feedback(photo_id=pid)

    def _on_capture_snap_all_boxes(self) -> None:
        pid = self._selected_capture_photo_id()
        if not pid:
            return
        items = self.session.reconstruct_status().get("items") or []
        for it in items:
            if it.get("photo_id") == pid and it.get("role") == "part":
                try:
                    self.session.reconstruct_update_device_box(str(it.get("id") or ""), snap_to_edges=True)
                except Exception:
                    pass
        self._refresh_items()
        self.refresh_capture_feedback(photo_id=pid)

    def _on_capture_attach_closeup(self) -> None:
        from panelcad.reconstruct.feedback import build_photo_feedback_report

        pid = self._selected_capture_photo_id()
        target_item_id: str | None = None
        if pid:
            try:
                rep = build_photo_feedback_report(self.session, photo_id=pid)
                boxes = rep.get("boxes") or []
                missing_boxes = [b for b in boxes if b.get("status_category") == "missing_info"]
                if missing_boxes:
                    target_item_id = str(missing_boxes[0].get("item_id") or "") or None
                elif boxes:
                    target_item_id = str(boxes[0].get("item_id") or "") or None
            except Exception:
                target_item_id = None
        dlg = self.open_photo_feedback_overlay(
            photo_id=pid,
            selected_item_id=target_item_id,
            modal=False,
        )
        dlg._on_attach_closeup_clicked()
        self._refresh_items()
        self._refresh_steps()
        self._refresh_review()
        self.refresh_capture_feedback(photo_id=pid)

    def _show_scale_photo(self) -> None:
        photo_id = str(self.scale_photo.currentData() or "")
        if not photo_id or not hasattr(self, "scale_view"):
            return
        try:
            payload = self.session.reconstruct_photo_region(photo_id=photo_id)
        except (KeyError, ValueError):
            self.scale_view.set_photo("")
            return
        self.scale_view.set_photo(str(payload.get("path") or ""))
        self._sync_scale_marks()

    def _set_scale_mode(self, mode: str) -> None:
        self._scale_mode = mode
        self._sync_scale_marks()

    def _scale_clicked(self, x: float, y: float) -> None:
        text = f"{x:.1f},{y:.1f}"
        if self._scale_mode == "len1":
            if self.p1.text().strip() and not self.p2.text().strip():
                self.p2.setText(text)
            else:
                self.p1.setText(text)
                self.p2.clear()
        elif self._scale_mode == "len2":
            if self.p3.text().strip() and not self.p4.text().strip():
                self.p4.setText(text)
            else:
                self.p3.setText(text)
                self.p4.clear()
        else:
            points = self._clicked_corners()
            if len(points) >= 4:
                points = []
            points.append((x, y))
            self.corners.setText(" ".join(f"{px:.1f},{py:.1f}" for px, py in points))
        self._sync_scale_marks()

    def _clicked_corners(self) -> list[tuple[float, float]]:
        points: list[tuple[float, float]] = []
        for token in self.corners.text().replace(";", " ").split():
            try:
                points.append(_xy(token))
            except ValueError:
                return []
        return points

    def _sync_scale_marks(self) -> None:
        if not hasattr(self, "scale_view"):
            return
        marks: list[tuple[float, float]] = []
        if self._scale_mode == "corners":
            marks.extend(self._clicked_corners())
        elif self._scale_mode == "len1":
            for box in (self.p1, self.p2):
                if box.text().strip():
                    try:
                        marks.append(_xy(box.text()))
                    except ValueError:
                        pass
        else:
            for box in (self.p3, self.p4):
                if box.text().strip():
                    try:
                        marks.append(_xy(box.text()))
                    except ValueError:
                        pass
        self.scale_view.set_marks(marks)

    def _fill_identify_fields(self) -> None:
        item_id = self._selected_item_id()
        store = self.session.project.reconstruct
        if store is None or not item_id:
            return
        try:
            item = store.item(item_id)
        except KeyError:
            return
        library_id = ""
        if item.matches and item.matches[0].library_id:
            library_id = item.matches[0].library_id
            if not self.confirm_catalog.text().strip():
                self.confirm_catalog.setText(item.matches[0].catalog)
        elif item.library_id and item.library_id not in {
            "reconstruct.needs_info",
            "reconstruct.measured_envelope",
        }:
            library_id = item.library_id
        self.env_lib.setText(library_id)
        if item.tag and not self.confirm_tag.text().strip():
            self.confirm_tag.setText(item.tag)

    def _refresh_items(self) -> None:
        selected = self._selected_item_id()
        self.item_list.clear()
        store = self.session.project.reconstruct
        if store is None:
            return
        restore = -1
        for item in store.items:
            name = item.part_number or item.label_text or item.tag or item.terminal or item.wire_number or ""
            label = f"{item.id}  {name}  {self._row_hint(item)}".rstrip()
            row = QListWidgetItem(label)
            row.setData(Qt.ItemDataRole.UserRole, item.id)
            self.item_list.addItem(row)
            if item.id == selected:
                restore = self.item_list.count() - 1
        if restore >= 0:
            self.item_list.setCurrentRow(restore)

    def _refresh_steps(self) -> None:
        self.step_list.clear()
        for step in self.session.reconstruct_checklist().get("checklist") or []:
            row = QListWidgetItem(f"{step.get('id')}  [{step.get('status')}]  {step.get('prompt')}")
            row.setData(Qt.ItemDataRole.UserRole, step.get("id"))
            self.step_list.addItem(row)
        self._refresh_wire_schedule()

    def _traced_wires(self) -> list[Any]:
        wires: list[Any] = []
        store = self.session.project.reconstruct
        target_ids = {"asbuilt", "asbuilt_power", "asbuilt-power"}
        if store is not None and store.schematic_sheet:
            target_ids.add(store.schematic_sheet)
        for sheet in self.session.project.sheets:
            if sheet.kind == "schematic" and (sheet.id in target_ids or sheet.wires):
                wires.extend(sheet.wires)
        return wires

    def _refresh_wire_schedule(self) -> None:
        if not hasattr(self, "wire_schedule_summary"):
            return
        wires = self._traced_wires()
        if not wires:
            self.wire_schedule_summary.setText("From-To Wire Landings: 0 traced")
            return
        preview = ", ".join(
            f"{(w.number + ': ') if w.number else ''}{w.frm} -> {w.to}" for w in wires[:6]
        )
        if len(wires) > 6:
            preview += f" (+{len(wires) - 6} more)"
        self.wire_schedule_summary.setText(f"From-To Wire Landings ({len(wires)} traced): {preview}")

    def _rebuild_schematic(self) -> dict[str, Any]:
        from panelcad.reconstruct.service import synthesize_reconstruct_schematic

        result = synthesize_reconstruct_schematic(self.session)
        self._refresh_wire_schedule()
        return result

    def _set_review(self, text: str) -> None:
        self.review_summary.setText(text)
        width = 280
        if hasattr(self, "review_scroll"):
            width = max(200, self.review_scroll.viewport().width() - 12)
        self.review_summary.setMinimumWidth(width)
        height = self.review_summary.heightForWidth(width)
        if height > 0:
            self.review_summary.setMinimumHeight(height)

    def _refresh_review(self) -> None:
        placed, pending, blocked, needs = self._classify()
        summary = _preview_summary(
            self,
            placed=placed,
            pending=pending,
            blocked=blocked,
            needs=needs,
        )
        self._set_review(_review_body(self, summary, placed, pending, blocked, needs))

    def _back(self) -> None:
        if self._page > 0:
            self._page -= 1
            self.stack.setCurrentIndex(self._page)
        self._sync_nav()

    def _next(self) -> None:
        if self._page >= self.stack.count() - 1:
            self._apply_to_print()
            return
        self._page += 1
        self.stack.setCurrentIndex(self._page)
        if self._page == 2:
            self._refresh_items()
        if self._page == 3:
            self._refresh_steps()
        self._sync_nav()

    def _apply_to_print(self) -> None:
        _placed, pending, _blocked, _needs = self._classify()
        if pending:
            try:
                self.session.reconstruct_place_confirmed()
            except ValueError as exc:
                QMessageBox.warning(self, "Apply to print", str(exc))
                self._refresh_review()
                return
        if any(
            s.kind == "schematic" and (s.id in {"asbuilt", "asbuilt_power", "asbuilt-power"} or s.wires)
            for s in self.session.project.sheets
        ):
            from panelcad.reconstruct.service import synthesize_reconstruct_schematic

            synthesize_reconstruct_schematic(self.session)
        placed, pending, blocked, needs = self._classify()
        if placed:
            summary = _added_summary(self, placed, needs)
        else:
            summary = _nothing_summary(self, blocked, needs)
        self._set_review(_review_body(self, summary, placed, pending, blocked, needs))
        sheets: list[str] = []
        for item in placed:
            if item.sheet and item.sheet not in sheets:
                sheets.append(item.sheet)
        skipped = [
            {"id": item.id, "reason": reason, "text": _skip_text(reason)} for item, reason in blocked
        ]
        self.apply_result = ReconstructApplyResult(
            summary=summary,
            sheets=sheets,
            placed=[
                {"id": item.id, "ref": item.placed_ref, "sheet": item.sheet} for item in placed
            ],
            skipped=skipped,
            needs_info=[item.id for item in needs],
            applied=bool(placed),
        )
        self._accepting = True
        self.accept()

    def _sync_nav(self) -> None:
        self.back.setEnabled(self._page > 0)
        header = _STEP_HEADERS[self._page] if self._page < len(_STEP_HEADERS) else ""
        self.step_header.setText(header)
        if self._page >= self.stack.count() - 1:
            self._refresh_review()
            placed, pending, _blocked, _needs = self._classify()
            if pending or placed:
                self.next.setText("Apply to print")
            else:
                self.next.setText("Close without changes")
        else:
            self.next.setText("Next")

    def _cancel(self) -> None:
        if not self._confirm_discard():
            return
        self.reject()

    def _confirm_discard(self) -> bool:
        if not self._dirty():
            return True
        answer = QMessageBox.question(
            self,
            "Discard changes?",
            "Discard changes made in this wizard?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _dirty(self) -> bool:
        current = self.session.project.model_dump(mode="json")
        if current != self._snap_project:
            return True
        if len(self.session._undo) != len(self._snap_undo):
            return True
        return len(self.session._redo) != len(self._snap_redo)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and not event.isAutoRepeat():
            self._cancel()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._accepting or int(self.result()) == int(QDialog.DialogCode.Accepted):
            event.accept()
            return
        if self._in_reject:
            event.accept()
            return
        if not self._confirm_discard():
            event.ignore()
            return
        self._in_reject = True
        try:
            self.reject()
        finally:
            self._in_reject = False
        event.accept()

    def reject(self) -> None:  # noqa: N802
        from panelcad.core.models import Project

        restored = Project.model_validate(self._snap_project)
        project = self.session.project
        for name in type(project).model_fields:
            setattr(project, name, getattr(restored, name))
        self.session._undo = [dict(item) for item in self._snap_undo]
        self.session._redo = [dict(item) for item in self._snap_redo]
        # The lists are the steps. The cached tips are the pictures those steps
        # restore. Leaving the tips from the discarded edits undoes back into them.
        self.session._sync_history_tip(self.session._undo, "_undo_tip")
        self.session._sync_history_tip(self.session._redo, "_redo_tip")
        self.session._touch_drawing()
        super().reject()

    def _part_items(self) -> list[ObservedItem]:
        store = self.session.project.reconstruct
        if store is None:
            return []
        return [item for item in store.items if item.role == "part"]

    def _row_hint(self, item: ObservedItem) -> str:
        if item.role != "part":
            return str(item.role).replace("_", " ")
        if item_is_layout_ready(item):
            photo = _photo(self, item.photo_id)
            if photo is None or mm_per_px(photo) is None:
                return "needs scale"
            return "ready"
        if item.status == "catalog":
            return "needs envelope"
        return "needs catalog"

    def _classify(
        self,
    ) -> tuple[list[ObservedItem], list[ObservedItem], list[tuple[ObservedItem, str]], list[ObservedItem]]:
        placed: list[ObservedItem] = []
        pending: list[ObservedItem] = []
        blocked: list[tuple[ObservedItem, str]] = []
        needs: list[ObservedItem] = []
        store = self.session.project.reconstruct
        if store is None:
            return placed, pending, blocked, needs
        for item in self._part_items():
            if item_is_layout_ready(item) and item.placed_ref:
                placed.append(item)
                continue
            if item_is_layout_ready(item):
                photo = _photo(self, item.photo_id)
                if photo is None or mm_per_px(photo) is None:
                    blocked.append((item, "unscaled"))
                else:
                    pending.append(item)
                continue
            reason = "no_footprint" if item.status == "catalog" else "unconfirmed"
            blocked.append((item, reason))
            needs.append(item)
        return placed, pending, blocked, needs

    def _no_ai(self) -> bool:
        last = self._last_identify
        if not last:
            return False
        if str(last.get("backend") or "") == "stub":
            return True
        return bool(str(last.get("warning") or "").strip())

    def _unreadable_lines(self) -> list[str]:
        store = self.session.project.reconstruct
        if store is None:
            return []
        lines: list[str] = []
        for photo in store.photos:
            if not (photo.unreadable or str(photo.vision_note or "").strip()):
                continue
            note = str(photo.vision_note or "").strip() or "unreadable"
            if self._no_ai() and "reshoot" in note:
                lines.append(
                    "No AI connected, so labels aren't read automatically. "
                    "Type the part number from each label."
                )
                return lines
            lines.append(f"Photo {photo.id} could not be read: {note}")
        return lines

    def _sheet_title(self, sheet_id: str) -> str:
        if not sheet_id:
            return ""
        try:
            sheet = self.session.project.sheet(sheet_id)
        except KeyError:
            return sheet_id
        return str(sheet.title or sheet.id)

    def _future_title(self, item: ObservedItem) -> str:
        store = self.session.project.reconstruct
        if store is None:
            return "Panel"
        try:
            photo = store.photo(item.photo_id)
        except KeyError:
            return "Panel"
        kind = target_sheet_kind(photo.role)
        current = store.panel_sheet if kind == "panel_layout" else store.door_sheet
        if current:
            try:
                return self._sheet_title(current)
            except KeyError:
                pass
        for sheet in self.session.project.sheets:
            if sheet.kind == kind:
                return str(sheet.title or sheet.id)
        return "Panel" if kind == "panel_layout" else "Door"


def _photo(wizard: ReconstructWizard, photo_id: str) -> Any:
    store = wizard.session.project.reconstruct
    if store is None or not photo_id:
        return None
    try:
        return store.photo(photo_id)
    except KeyError:
        return None


def _skip_text(reason: str) -> str:
    token = str(reason or "").strip()
    return _SKIP_TEXT.get(token, token or "not ready to place")


def _identify_feedback(result: dict[str, Any], parts: list[ObservedItem], unread: list[str]) -> str:
    backend = str(result.get("backend") or "")
    warning = str(result.get("warning") or "").strip()
    lines: list[str] = []
    if not parts or backend == "stub" or warning:
        offline = backend == "stub" or "unavailable" in warning.lower() or "no ai" in warning.lower()
        if offline:
            lines.append(_NO_AI)
        elif not parts:
            lines.append(
                "No parts were found on these photos. "
                "Read the labels again, connect AI in Settings, or add parts by hand: "
                "place parts from the library onto the sheet."
            )
        if warning and all(warning not in line for line in lines):
            lines.append(warning)
    lines.extend(unread)
    if not parts and not lines:
        lines.append("No parts were found. Read the labels again, or add parts by hand from the library.")
    return "\n".join(lines)


def _place_text(result: dict[str, Any], *, parts: list[ObservedItem], no_ai: bool) -> str:
    placed = list(result.get("placed") or [])
    skipped = list(result.get("skipped") or [])
    lines: list[str] = []
    if placed:
        bits = [f"{row.get('ref')} on sheet {row.get('sheet')}" for row in placed]
        lines.append(f"Placed {len(placed)}: " + "; ".join(bits) + ".")
    else:
        lines.append("Placed 0.")
    if skipped:
        lines.append(f"Skipped {len(skipped)}:")
        for row in skipped:
            lines.append(f"{row.get('id')} skipped: {_skip_text(str(row.get('reason') or ''))}.")
    else:
        extra = ""
        if not placed and not parts:
            extra = " No parts were found, so there was nothing to place."
            if no_ai:
                extra += " " + NO_AI_MESSAGE
        lines.append("Skipped 0." + extra)
    return "\n".join(lines)


def _count_phrase(wizard: ReconstructWizard, verb: str, items: list[ObservedItem], *, future: bool) -> str:
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for item in items:
        if future:
            title = wizard._future_title(item)
            label = item.tag or item.part_number or item.id
        else:
            title = wizard._sheet_title(item.sheet)
            label = item.placed_ref or item.tag or item.id
        if title not in groups:
            order.append(title)
            groups[title] = []
        groups[title].append(str(label))
    bits: list[str] = []
    for title in order:
        refs = groups[title]
        word = "part" if len(refs) == 1 else "parts"
        bits.append(f"{len(refs)} {word} to sheet {title}")
    return verb + " " + ", ".join(bits)


def _added_summary(wizard: ReconstructWizard, placed: list[ObservedItem], needs: list[ObservedItem]) -> str:
    text = _count_phrase(wizard, "Added", placed, future=False)
    if needs:
        text += f"; {len(needs)} need info, see Problems"
    return text


def _nothing_summary(
    wizard: ReconstructWizard,
    blocked: list[tuple[ObservedItem, str]],
    needs: list[ObservedItem],
) -> str:
    sentences = ["Nothing was applied."]
    if not wizard._part_items():
        if wizard._no_ai():
            sentences.append(NO_AI_MESSAGE)
            sentences.append(
                "Connect AI in Settings, or add parts by hand from the library onto the sheet."
            )
        else:
            sentences.append("No parts were found.")
            sentences.append(
                "Read labels again, or add parts by hand from the library onto the sheet."
            )
    else:
        need_ids = {item.id for item in needs}
        for item, reason in blocked:
            if item.id in need_ids:
                continue
            sentences.append(f"{_review_label(item)} skipped: {_skip_text(reason)}.")
        if not blocked:
            sentences.append("No layout-ready parts with a scale are waiting to be placed.")
    if needs:
        sentences.append(f"{len(needs)} need info, see Problems.")
    sentences.extend(line + "." for line in wizard._unreadable_lines())
    return " ".join(sentences)


def _preview_summary(
    wizard: ReconstructWizard,
    *,
    placed: list[ObservedItem],
    pending: list[ObservedItem],
    blocked: list[tuple[ObservedItem, str]],
    needs: list[ObservedItem],
) -> str:
    if pending:
        text = _count_phrase(wizard, "Will add", pending, future=True)
        if placed:
            text += ". " + _count_phrase(wizard, "Already added", placed, future=False)
        if needs:
            text += f". {len(needs)} need info, see Problems"
        return text
    if placed:
        return _added_summary(wizard, placed, needs)
    return _nothing_summary(wizard, blocked, needs)


def _review_label(item: ObservedItem) -> str:
    tag = item.placed_ref or item.tag or ""
    name = item.part_number or item.label_text or ""
    if tag and name:
        return f"{tag} {name}"
    return tag or name or "part"


def _review_body(
    wizard: ReconstructWizard,
    summary: str,
    placed: list[ObservedItem],
    pending: list[ObservedItem],
    blocked: list[tuple[ObservedItem, str]],
    needs: list[ObservedItem],
) -> str:
    lines = [summary, ""]
    if pending:
        lines.append("Will be added when you choose Apply to print:")
        for item in pending:
            lines.append(f"  ✔ {_review_label(item)} on sheet {wizard._future_title(item)}")
    if placed:
        lines.append("On the drawing:")
        for item in placed:
            lines.append(f"  ✔ {_review_label(item)} placed")
    traced = wizard._traced_wires() if hasattr(wizard, "_traced_wires") else []
    if traced:
        lines.append(f"From-To Wire Landings ({len(traced)} traced):")
        for wire in traced[:8]:
            num = f"[{wire.number}] " if wire.number else ""
            lines.append(f"  • {num}{wire.frm} -> {wire.to}")
    if needs:
        lines.append("Needs size:")
        for item in needs:
            lines.append(f"  ? {_review_label(item)} needs size")
    skipped = [(item, reason) for item, reason in blocked if item not in needs]
    if skipped:
        lines.append("Skipped:")
        for item, reason in skipped:
            lines.append(f"  {_review_label(item)} skipped: {_skip_text(reason)}.")
    if not placed and not pending:
        lines.append("Nothing will be written to the drawing as a sized part.")
    lines.append("Next only changes pages. Apply to print is what writes confirmed parts.")
    return "\n".join(lines)


def _xy(text: str) -> tuple[float, float]:
    parts = [part.strip() for part in str(text).replace(" ", "").split(",") if part.strip()]
    if len(parts) != 2:
        raise ValueError("point must be x,y in photo pixels")
    return (float(parts[0]), float(parts[1]))


# Used by tests that construct the dialog without showing it.
QDialogButtonBox  # noqa: B018
