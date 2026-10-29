"""Show a panel photo (or a crop), jump from Problems / a layout part, and inspect photo feedback overlays."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session


def _photo_caption(photo: dict, item: dict) -> str:
    name = str(photo.get("original_name") or "").strip()
    role = str(photo.get("role") or "").replace("_", " ")
    label = item.get("part_number") or item.get("label_text") or item.get("tag") or item.get("id") or ""
    head = name or str(photo.get("id") or "")
    return f"{head}  {role}  {label}".strip()


class PhotoView(QLabel):
    """Photo display widget supporting single-region highlight, full feedback overlay, click-to-select, and drag-box for missed devices."""

    box_clicked = Signal(str)
    region_drawn = Signal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pix = QPixmap()
        self._source_size: tuple[int, int] = (0, 0)
        self._banner_top_offset: int = 0
        self._region: tuple[float, float, float, float] | None = None
        self._feedback_boxes: list[dict[str, Any]] = []
        self._selected_item_id: str | None = None
        self._draw_missed_mode: bool = False
        self._drag_start_px: QPointF | None = None
        self._drag_current_px: QPointF | None = None
        self._first_corner_px: QPointF | None = None
        self._pending_missed_region: dict[str, float] | None = None
        # The dock copy must be free to shrink with the bottom splitter.
        # The dialog sets its own floor after it creates this view.
        self.setMinimumSize(0, 0)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_photo(self, path: Path, region: dict | None = None) -> None:
        self._pix = QPixmap(str(path))
        self._source_size = (self._pix.width(), self._pix.height())
        self._banner_top_offset = 0
        self._feedback_boxes = []
        if region and float(region.get("w") or 0) > 0:
            self._region = (
                float(region.get("x") or 0),
                float(region.get("y") or 0),
                float(region.get("w") or 0),
                float(region.get("h") or 0),
            )
        else:
            self._region = None
        self.update()

    def set_annotated_feedback(
        self,
        image: QImage,
        *,
        source_width: int,
        source_height: int,
        boxes: list[dict[str, Any]],
        selected_item_id: str | None = None,
        banner_top_offset: int = 54,
    ) -> None:
        self._pix = QPixmap.fromImage(image)
        self._source_size = (max(1, int(source_width)), max(1, int(source_height)))
        self._banner_top_offset = max(0, int(banner_top_offset))
        self._region = None
        self._feedback_boxes = list(boxes)
        self._selected_item_id = selected_item_id
        self.update()

    def set_draw_missed_box_mode(self, enabled: bool) -> None:
        self._draw_missed_mode = bool(enabled)
        self._drag_start_px = None
        self._drag_current_px = None
        self._first_corner_px = None
        self.setMouseTracking(bool(enabled))
        self.setCursor(
            Qt.CursorShape.CrossCursor if self._draw_missed_mode else Qt.CursorShape.ArrowCursor
        )
        self.update()

    def draw_missed_box_mode(self) -> bool:
        return self._draw_missed_mode

    def pending_missed_region(self) -> dict[str, float] | None:
        return dict(self._pending_missed_region) if self._pending_missed_region else None

    def clear_pending_missed_region(self) -> None:
        self._pending_missed_region = None
        self.update()

    def _widget_to_photo_px(self, pos: QPointF) -> tuple[float, float] | None:
        if self._pix.isNull() or self._pix.width() <= 0 or self._pix.height() <= 0:
            return None
        target = self.contentsRect().adjusted(4, 4, -4, -4)
        scaled = self._pix.scaled(
            target.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        if scaled.width() <= 0 or scaled.height() <= 0:
            return None
        ox = target.x() + (target.width() - scaled.width()) / 2.0
        oy = target.y() + (target.height() - scaled.height()) / 2.0
        ix = (pos.x() - ox) * (self._pix.width() / scaled.width())
        iy = (pos.y() - oy) * (self._pix.height() / scaled.height()) - self._banner_top_offset
        sw, sh = self._source_size
        if sw <= 0 or sh <= 0:
            sw, sh = self._pix.width(), max(1, self._pix.height() - self._banner_top_offset)
        px = max(0.0, min(float(sw), float(ix)))
        py = max(0.0, min(float(sh), float(iy)))
        return (px, py)

    def simulate_click_photo_px(self, px: float, py: float) -> str | None:
        """Programmatic click helper at photo pixel coordinates (px, py) for tests and automation."""
        hit = self._hit_test_box(px, py)
        if hit:
            self._selected_item_id = hit
            self.box_clicked.emit(hit)
            self.update()
        return hit

    def simulate_drag_photo_region(self, x: float, y: float, w: float, h: float) -> dict[str, float]:
        """Programmatic drag-box helper in photo pixel coordinates (x, y, w, h)."""
        sw, sh = self._source_size
        sw = max(1, sw or self._pix.width() or 1)
        sh = max(1, sh or (self._pix.height() - self._banner_top_offset) or 1)
        rx = max(0.0, min(float(sw - 1), float(x)))
        ry = max(0.0, min(float(sh - 1), float(y)))
        rw = max(4.0, min(float(sw) - rx, float(w)))
        rh = max(4.0, min(float(sh) - ry, float(h)))
        region = {
            "x": round(rx, 2),
            "y": round(ry, 2),
            "w": round(rw, 2),
            "h": round(rh, 2),
            "nx": round(rx / sw, 4),
            "ny": round(ry / sh, 4),
            "nw": round(rw / sw, 4),
            "nh": round(rh / sh, 4),
        }
        self._pending_missed_region = region
        self.region_drawn.emit(dict(region))
        self.update()
        return region

    def _hit_test_box(self, px: float, py: float) -> str | None:
        for b in reversed(self._feedback_boxes):
            reg = b.get("region") or {}
            bx = float(reg.get("x") or 0.0)
            by = float(reg.get("y") or 0.0)
            bw = float(reg.get("w") or 0.0)
            bh = float(reg.get("h") or 0.0)
            if bx <= px <= bx + bw and by <= py <= by + bh:
                return str(b.get("item_id") or "")
        return None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            pt = self._widget_to_photo_px(event.position())
            if pt is not None:
                if self._draw_missed_mode:
                    self._drag_start_px = QPointF(pt[0], pt[1])
                    self._drag_current_px = QPointF(pt[0], pt[1])
                    self.update()
                    return
                hit = self._hit_test_box(pt[0], pt[1])
                if hit:
                    self._selected_item_id = hit
                    self.box_clicked.emit(hit)
                    self.update()
                    return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._draw_missed_mode and (self._drag_start_px is not None or self._first_corner_px is not None):
            pt = self._widget_to_photo_px(event.position())
            if pt is not None:
                self._drag_current_px = QPointF(pt[0], pt[1])
                self.update()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if (
            self._draw_missed_mode
            and event.button() == Qt.MouseButton.LeftButton
            and self._drag_start_px is not None
        ):
            pt = self._widget_to_photo_px(event.position())
            end_pt = QPointF(pt[0], pt[1]) if pt is not None else self._drag_current_px or self._drag_start_px
            dx = abs(end_pt.x() - self._drag_start_px.x())
            dy = abs(end_pt.y() - self._drag_start_px.y())
            if dx >= 12.0 and dy >= 12.0:
                x0 = min(self._drag_start_px.x(), end_pt.x())
                y0 = min(self._drag_start_px.y(), end_pt.y())
                self._drag_start_px = None
                self._drag_current_px = None
                self._first_corner_px = None
                self.simulate_drag_photo_region(x0, y0, dx, dy)
                return
            # Support Two-Click Corner Mode: 1st click sets Corner 1, 2nd click sets Corner 2
            if self._first_corner_px is None:
                self._first_corner_px = QPointF(self._drag_start_px.x(), self._drag_start_px.y())
                self._drag_start_px = None
                self._drag_current_px = QPointF(self._first_corner_px.x(), self._first_corner_px.y())
                self.update()
                return
            else:
                x0 = min(self._first_corner_px.x(), end_pt.x())
                y0 = min(self._first_corner_px.y(), end_pt.y())
                w = max(12.0, abs(end_pt.x() - self._first_corner_px.x()))
                h = max(12.0, abs(end_pt.y() - self._first_corner_px.y()))
                self._drag_start_px = None
                self._drag_current_px = None
                self._first_corner_px = None
                self.simulate_drag_photo_region(x0, y0, w, h)
                return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self._pix.isNull():
            return
        painter = QPainter(self)
        target = self.contentsRect().adjusted(4, 4, -4, -4)
        scaled = self._pix.scaled(
            target.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = target.x() + (target.width() - scaled.width()) / 2
        y = target.y() + (target.height() - scaled.height()) / 2
        painter.drawPixmap(int(x), int(y), scaled)
        if self._region and self._pix.width() and self._pix.height():
            sx = scaled.width() / self._pix.width()
            sy = scaled.height() / self._pix.height()
            rx, ry, rw, rh = self._region
            box = QRectF(x + rx * sx, y + ry * sy, rw * sx, rh * sy)
            painter.setPen(QPen(QColor("#e6a817"), 2))
            painter.drawRect(box)

        # Draw active rubber-band or pending missed-device box if present
        active_drag_rect: tuple[float, float, float, float] | None = None
        if self._drag_start_px is not None and self._drag_current_px is not None:
            active_drag_rect = (
                min(self._drag_start_px.x(), self._drag_current_px.x()),
                min(self._drag_start_px.y(), self._drag_current_px.y()),
                max(4.0, abs(self._drag_current_px.x() - self._drag_start_px.x())),
                max(4.0, abs(self._drag_current_px.y() - self._drag_start_px.y())),
            )
        elif self._pending_missed_region:
            active_drag_rect = (
                float(self._pending_missed_region.get("x") or 0.0),
                float(self._pending_missed_region.get("y") or 0.0),
                float(self._pending_missed_region.get("w") or 0.0),
                float(self._pending_missed_region.get("h") or 0.0),
            )
        if active_drag_rect and self._pix.width() and self._pix.height():
            sx = scaled.width() / self._pix.width()
            sy = scaled.height() / self._pix.height()
            rx, ry, rw, rh = active_drag_rect
            box = QRectF(
                x + rx * sx,
                y + (ry + self._banner_top_offset) * sy,
                rw * sx,
                rh * sy,
            )
            pen = QPen(QColor("#22d3ee"), 3, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(QColor(34, 211, 238, 45))
            painter.drawRect(box)
        painter.end()


class PhotoViewerDialog(QDialog):
    def __init__(self, session: Session, payload: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("Panel photo")
        self.resize(720, 560)
        layout = QVBoxLayout(self)
        self.caption = QLabel()
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)
        self.view = PhotoView()
        self.view.setMinimumSize(320, 240)
        layout.addWidget(self.view, 1)
        row = QHBoxLayout()
        self.feedback_btn = QPushButton("Open Full Cabinet Photo Feedback Overlay…")
        self.feedback_btn.clicked.connect(self._open_feedback_overlay)
        row.addWidget(self.feedback_btn)
        row.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)
        self._current_photo_id: str | None = None
        self._current_item_id: str | None = None
        self.show_payload(payload)

    def show_payload(self, payload: dict) -> None:
        photo = payload.get("photo") or {}
        item = payload.get("item") or {}
        self._current_photo_id = str(photo.get("id") or "") or None
        self._current_item_id = str(item.get("id") or "") or None
        path = Path(str(payload.get("path") or ""))
        self.caption.setText(_photo_caption(photo, item))
        if path.is_file():
            self.view.set_photo(path, payload.get("region") or item.get("region"))

    def _open_feedback_overlay(self) -> None:
        dlg = PhotoFeedbackDialog(
            self.session,
            photo_id=self._current_photo_id,
            selected_item_id=self._current_item_id,
            parent=self,
        )
        dlg.exec()


class PhotoPeekDock(QWidget):
    """Small crop next to Problems. Click a reconstruct-linked part to fill it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        self.caption = QLabel("Select a part placed from a panel photo")
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)
        self.view = PhotoView()
        layout.addWidget(self.view, 1)

    def show_payload(self, payload: dict | None) -> None:
        if not payload:
            self.caption.setText("Select a part placed from a panel photo")
            return
        photo = payload.get("photo") or {}
        item = payload.get("item") or {}
        self.caption.setText(_photo_caption(photo, item))
        path = Path(str(payload.get("path") or ""))
        if path.is_file():
            self.view.set_photo(path, payload.get("region") or item.get("region"))


class PhotoFeedbackDialog(QDialog):
    """Interactive Cabinet Photo Feedback Overlay.

    - Draws color-coded bounding boxes around every detected device on the cabinet photo:
      * Red/Amber box + explicit 'MISSING: ...' callout badges for devices missing catalog/envelope/nameplate/wire info.
      * Emerald Green box + '✓ TAG · Part #' badge for confirmed devices so any unboxed physical component immediately stands out as missed by the bot/software.
    - Supports filtering ('All Detected (Spot Missed Devices)', 'Missing Info Only', 'Confirmed Only').
    - Lets the human click a boxed device to fill in its missing info right alongside the photo.
    - Lets the human drag a box on the cabinet photo (or enter a box) to add a device that automated detection completely missed.
    - Exports the annotated cabinet photo as a high-contrast PNG for field review or Slack.
    """

    def __init__(
        self,
        session: Session,
        *,
        photo_id: str | None = None,
        selected_item_id: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self._photo_id = photo_id
        self._selected_item_id = selected_item_id
        self._last_report: dict[str, Any] = {}
        self._last_exported_path: Path | None = None

        self.setWindowTitle("Cabinet Photo Feedback Overlay — Missing Info & Missed Device Check")
        self.resize(1180, 780)

        root = QVBoxLayout(self)

        # Top control bar
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Photo:"))
        self.photo_combo = QComboBox()
        self.photo_combo.currentIndexChanged.connect(self._on_photo_changed)
        top_row.addWidget(self.photo_combo)

        top_row.addWidget(QLabel("Show Boxes:"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItem("All Detected (Spot Missed Devices)", "all")
        self.filter_combo.addItem("Missing Info Only (Action Required)", "missing_only")
        self.filter_combo.addItem("Confirmed Only", "confirmed_only")
        self.filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        top_row.addWidget(self.filter_combo)

        self.draw_missed_btn = QPushButton("＋ Draw Box on Missed Device")
        self.draw_missed_btn.setCheckable(True)
        self.draw_missed_btn.toggled.connect(self._on_draw_missed_toggled)
        top_row.addWidget(self.draw_missed_btn)

        self.snap_selected_btn = QPushButton("✨ Auto-Snap Selected Box to Edges")
        self.snap_selected_btn.clicked.connect(self._on_snap_selected_box)
        top_row.addWidget(self.snap_selected_btn)

        self.redraw_selected_btn = QPushButton("📐 Adjust / Redraw Selected Box")
        self.redraw_selected_btn.setCheckable(True)
        self.redraw_selected_btn.toggled.connect(self._on_redraw_selected_toggled)
        top_row.addWidget(self.redraw_selected_btn)

        self.export_btn = QPushButton("Export Annotated Photo…")
        self.export_btn.clicked.connect(self._on_export_clicked)
        top_row.addWidget(self.export_btn)
        top_row.addStretch()
        root.addLayout(top_row)

        # Site-Exit Readiness banner + Summary banner label
        self.site_exit_banner_label = QLabel()
        self.site_exit_banner_label.setWordWrap(True)
        self.site_exit_banner_label.setStyleSheet(
            "font-weight: 700; font-size: 13px; padding: 7px 12px; border-radius: 4px; background: #7f1d1d; color: #fffbeb;"
        )
        root.addWidget(self.site_exit_banner_label)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet(
            "font-weight: 600; padding: 5px 10px; border-radius: 4px; background: #1e293b; color: #f8fafc;"
        )
        root.addWidget(self.summary_label)

        # Main horizontal splitter: annotated photo on left, device feedback & missed-device inspector on right
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.view = PhotoView()
        self.view.setMinimumSize(560, 460)
        self.view.box_clicked.connect(self._on_photo_box_clicked)
        self.view.region_drawn.connect(self._on_photo_region_drawn)
        splitter.addWidget(self.view)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(6, 0, 0, 0)

        right_layout.addWidget(QLabel("Before You Leave Site — Required Close-Ups & Context:"))
        self.shot_list_widget = QListWidget()
        self.shot_list_widget.setMaximumHeight(110)
        right_layout.addWidget(self.shot_list_widget)

        right_layout.addWidget(QLabel("Boxed Devices on This Photo (click a box or row):"))
        self.device_list = QListWidget()
        self.device_list.currentRowChanged.connect(self._on_device_row_changed)
        right_layout.addWidget(self.device_list, 1)

        # Group 1: Selected Device Missing-Info Inspector & Quick Fill-In
        self.inspect_group = QGroupBox("Selected Device — What PanelCAD Is Missing")
        inspect_v = QVBoxLayout(self.inspect_group)
        self.missing_details_label = QLabel("Select a boxed device on the photo to see what is missing.")
        self.missing_details_label.setWordWrap(True)
        inspect_v.addWidget(self.missing_details_label)

        form = QFormLayout()
        self.tag_edit = QLineEdit()
        self.tag_edit.setPlaceholderText("e.g. K1, CB1, PSU1")
        form.addRow("Device Tag:", self.tag_edit)

        self.part_edit = QLineEdit()
        self.part_edit.setPlaceholderText("Catalog / Part # (e.g. QOU220, 2903152)")
        form.addRow("Part # / SKU:", self.part_edit)

        self.brand_edit = QLineEdit()
        self.brand_edit.setPlaceholderText("Brand (e.g. Square D, Phoenix Contact)")
        form.addRow("Brand:", self.brand_edit)

        dim_row = QHBoxLayout()
        self.w_spin = QDoubleSpinBox()
        self.w_spin.setRange(0.0, 2000.0)
        self.w_spin.setSuffix(" mm W")
        self.h_spin = QDoubleSpinBox()
        self.h_spin.setRange(0.0, 2000.0)
        self.h_spin.setSuffix(" mm H")
        self.d_spin = QDoubleSpinBox()
        self.d_spin.setRange(0.0, 2000.0)
        self.d_spin.setSuffix(" mm D")
        dim_row.addWidget(self.w_spin)
        dim_row.addWidget(self.h_spin)
        dim_row.addWidget(self.d_spin)
        form.addRow("Dimensions:", dim_row)

        self.mount_combo = QComboBox()
        self.mount_combo.addItem("(Keep / Unset)", "")
        self.mount_combo.addItem("DIN Rail", "din_rail")
        self.mount_combo.addItem("Backplate", "backplate")
        self.mount_combo.addItem("Door", "door")
        form.addRow("Mounting:", self.mount_combo)
        inspect_v.addLayout(form)

        btn_row = QHBoxLayout()
        self.save_device_btn = QPushButton("Save Missing Info & Confirm Selected Device")
        self.save_device_btn.clicked.connect(self._on_save_selected_device)
        self.attach_closeup_btn = QPushButton("Attach Close-Up Photo…")
        self.attach_closeup_btn.clicked.connect(self._on_attach_closeup_clicked)
        btn_row.addWidget(self.save_device_btn)
        btn_row.addWidget(self.attach_closeup_btn)
        inspect_v.addLayout(btn_row)
        right_layout.addWidget(self.inspect_group)

        # Group 2: Add Missed Device (when Bot / Vision missed a physical component)
        self.missed_group = QGroupBox("Spot an Unboxed Component? Add Missed Device")
        missed_form = QFormLayout(self.missed_group)
        self.missed_region_edit = QLineEdit()
        self.missed_region_edit.setPlaceholderText("Drag on photo or enter x,y,w,h (px)")
        missed_form.addRow("Photo Box (x,y,w,h):", self.missed_region_edit)

        self.missed_tag_edit = QLineEdit()
        self.missed_tag_edit.setPlaceholderText("Optional Tag (e.g. CR3, FU2)")
        missed_form.addRow("Tag:", self.missed_tag_edit)

        self.missed_part_edit = QLineEdit()
        self.missed_part_edit.setPlaceholderText("Optional Part # (leave blank if needs info)")
        missed_form.addRow("Part # / SKU:", self.missed_part_edit)

        self.missed_brand_edit = QLineEdit()
        self.missed_brand_edit.setPlaceholderText("Optional Brand")
        missed_form.addRow("Brand:", self.missed_brand_edit)

        self.missed_note_edit = QLineEdit()
        self.missed_note_edit.setPlaceholderText("e.g. Missed relay next to K1 on middle DIN rail")
        missed_form.addRow("What's Missing / Note:", self.missed_note_edit)

        missed_btn_row = QHBoxLayout()
        self.snap_missed_btn = QPushButton("✨ Snap Box to Device Edges")
        self.snap_missed_btn.clicked.connect(self._on_snap_missed_box)
        self.add_missed_btn = QPushButton("＋ Add Missed Device Box to Photo")
        self.add_missed_btn.clicked.connect(self._on_add_missed_device)
        missed_btn_row.addWidget(self.snap_missed_btn)
        missed_btn_row.addWidget(self.add_missed_btn)
        missed_form.addRow(missed_btn_row)
        right_layout.addWidget(self.missed_group)

        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)

        # Bottom close bar
        bottom = QHBoxLayout()
        self.status_note = QLabel("")
        bottom.addWidget(self.status_note, 1)
        close_btn = QPushButton("Done")
        close_btn.clicked.connect(self.accept)
        bottom.addWidget(close_btn)
        root.addLayout(bottom)

        self._populate_photos()
        self.refresh_feedback()

    def _populate_photos(self) -> None:
        self.photo_combo.blockSignals(True)
        self.photo_combo.clear()
        st = self.session.reconstruct_status()
        photos = st.get("photos") or []
        selected_idx = 0
        for idx, p in enumerate(photos):
            pid = str(p.get("id") or "")
            name = str(p.get("original_name") or pid)
            role = str(p.get("role") or "")
            self.photo_combo.addItem(f"{pid} · {name} ({role})", pid)
            if self._photo_id and pid == self._photo_id:
                selected_idx = idx
        if photos:
            self.photo_combo.setCurrentIndex(selected_idx)
            self._photo_id = str(self.photo_combo.currentData() or "")
        self.photo_combo.blockSignals(False)

    def current_filter_mode(self) -> str:
        return str(self.filter_combo.currentData() or "all")

    def set_filter_mode(self, mode: str) -> None:
        for i in range(self.filter_combo.count()):
            if self.filter_combo.itemData(i) == mode:
                self.filter_combo.setCurrentIndex(i)
                return

    def refresh_feedback(self) -> dict[str, Any]:
        from panelcad.reconstruct.feedback import render_annotated_cabinet_photo

        st = self.session.reconstruct_status()
        photos = st.get("photos") or []
        if not photos:
            self.summary_label.setText(
                "No cabinet photos attached yet. Add a cabinet photo in Reconstruct from Photos first."
            )
            self.device_list.clear()
            self._last_report = {}
            return {}

        if not self._photo_id:
            self._photo_id = str(photos[0].get("id") or "")

        mode = self.current_filter_mode()
        report = self.session.reconstruct_photo_feedback(
            photo_id=self._photo_id,
            filter_mode=mode,
        )
        self._last_report = report

        banner_txt = str(report.get("site_exit_banner") or "")
        safe = bool(report.get("safe_to_leave_site"))
        self.site_exit_banner_label.setText(banner_txt)
        if safe:
            self.site_exit_banner_label.setStyleSheet(
                "font-weight: 700; font-size: 13px; padding: 7px 12px; border-radius: 4px; background: #064e3b; color: #ecfdf5;"
            )
        else:
            self.site_exit_banner_label.setStyleSheet(
                "font-weight: 700; font-size: 13px; padding: 7px 12px; border-radius: 4px; background: #7f1d1d; color: #fffbeb;"
            )
        self.summary_label.setText(str(report.get("summary_text") or ""))

        self.shot_list_widget.clear()
        shots = report.get("on_site_shot_list") or []
        if shots:
            for s in shots:
                it = QListWidgetItem(f"⚠ {s}")
                it.setForeground(QColor("#fde047"))
                self.shot_list_widget.addItem(it)
        else:
            it = QListWidgetItem("✓ All detected devices have legible context — safe to leave site.")
            it.setForeground(QColor("#86efac"))
            self.shot_list_widget.addItem(it)

        img = render_annotated_cabinet_photo(
            self.session,
            photo_id=self._photo_id,
            filter_mode=mode,  # type: ignore[arg-type]
            selected_item_id=self._selected_item_id,
            include_banner=True,
        )
        pw = int(report.get("width_px") or img.width())
        ui_scale = max(1.0, min(3.2, pw / 1280.0))
        top_off = int(round(58 * ui_scale))
        self.view.set_annotated_feedback(
            img,
            source_width=pw,
            source_height=int(report.get("height_px") or max(1, img.height() - top_off)),
            boxes=report.get("boxes") or [],
            selected_item_id=self._selected_item_id,
            banner_top_offset=top_off,
        )

        self.device_list.blockSignals(True)
        self.device_list.clear()
        selected_row = -1
        for idx, b in enumerate(report.get("boxes") or []):
            iid = str(b.get("item_id") or "")
            tag = str(b.get("tag") or iid)
            if b.get("status_category") == "missing_info":
                reasons = "; ".join(b.get("missing_reasons") or ["Needs Info"])
                text = f"[MISSING INFO] {iid} ({tag}) — {reasons}"
                row_item = QListWidgetItem(text)
                row_item.setForeground(QColor("#fca5a5"))
            else:
                part = " ".join(
                    x for x in (str(b.get("brand") or "").strip(), str(b.get("part_number") or "").strip()) if x
                ) or "Confirmed Envelope"
                text = f"[✓ CONFIRMED] {iid} ({tag}) — {part}"
                row_item = QListWidgetItem(text)
                row_item.setForeground(QColor("#86efac"))
            row_item.setData(Qt.ItemDataRole.UserRole, b)
            self.device_list.addItem(row_item)
            if self._selected_item_id and iid == self._selected_item_id:
                selected_row = idx
        if selected_row < 0 and self.device_list.count() > 0:
            selected_row = 0
        if selected_row >= 0:
            self.device_list.setCurrentRow(selected_row)
            self._populate_inspector_from_row(selected_row)
        else:
            self.missing_details_label.setText("No boxed devices match the current filter.")
        self.device_list.blockSignals(False)
        return report

    def _populate_inspector_from_row(self, row: int) -> None:
        item = self.device_list.item(row)
        if item is None:
            return
        b = item.data(Qt.ItemDataRole.UserRole) or {}
        self._selected_item_id = str(b.get("item_id") or "") or None
        if b.get("status_category") == "missing_info":
            reasons = b.get("missing_reasons") or []
            prompts = b.get("checklist_prompts") or []
            lines = [f"• {r}" for r in reasons]
            for p in prompts:
                if p and all(p not in r for r in reasons):
                    lines.append(f"• Question: {p}")
            self.missing_details_label.setText(
                f"Device {b.get('item_id')} ({b.get('tag') or 'untagged'}) needs:\n" + "\n".join(lines)
            )
        else:
            info = b.get("confirmed_summary") or []
            self.missing_details_label.setText(
                f"Device {b.get('item_id')} ({b.get('tag')}) is confirmed:\n"
                + "\n".join(f"• {line}" for line in info)
            )
        self.tag_edit.setText(str(b.get("tag") or ""))
        self.part_edit.setText(str(b.get("part_number") or ""))
        self.brand_edit.setText(str(b.get("brand") or ""))
        self.w_spin.setValue(float(b.get("envelope_w_mm") or 0.0))
        self.h_spin.setValue(float(b.get("envelope_h_mm") or 0.0))
        self.d_spin.setValue(float(b.get("envelope_d_mm") or 0.0))
        mount = str(b.get("mount") or "")
        m_idx = max(0, self.mount_combo.findData(mount))
        self.mount_combo.setCurrentIndex(m_idx)

    def _on_photo_changed(self, _index: int) -> None:
        self._photo_id = str(self.photo_combo.currentData() or "") or None
        self.refresh_feedback()

    def _on_filter_changed(self, _index: int) -> None:
        self.refresh_feedback()

    def _on_draw_missed_toggled(self, checked: bool) -> None:
        if checked and getattr(self, "redraw_selected_btn", None) and self.redraw_selected_btn.isChecked():
            self.redraw_selected_btn.blockSignals(True)
            self.redraw_selected_btn.setChecked(False)
            self.redraw_selected_btn.blockSignals(False)
        self._redraw_mode_for_selected = False
        self.view.set_draw_missed_box_mode(checked)
        if checked:
            self.status_note.setText(
                "🎯 DRAW MODE: Drag a box around the missed device on the photo (or click 2 opposite corners)."
            )
        else:
            self.status_note.setText("")

    def _on_redraw_selected_toggled(self, checked: bool) -> None:
        if checked and self.draw_missed_btn.isChecked():
            self.draw_missed_btn.blockSignals(True)
            self.draw_missed_btn.setChecked(False)
            self.draw_missed_btn.blockSignals(False)
        self._redraw_mode_for_selected = bool(checked)
        self.view.set_draw_missed_box_mode(checked)
        if checked:
            self.status_note.setText(
                f"🎯 ADJUST BOX MODE ({self._selected_item_id or 'selected'}): Drag a new box around the device (or click 2 opposite corners)."
            )
        else:
            self.status_note.setText("")

    def _on_snap_selected_box(self) -> None:
        if not self._selected_item_id:
            return
        try:
            res = self.session.reconstruct_update_device_box(
                self._selected_item_id,
                snap_to_edges=True,
            )
            reg = res.get("region") or {}
            self.status_note.setText(
                f"✨ Snapped {self._selected_item_id} box to edges ({reg.get('x', 0):.0f},{reg.get('y', 0):.0f},{reg.get('w', 0):.0f}×{reg.get('h', 0):.0f} px)."
            )
            self.refresh_feedback()
        except (KeyError, ValueError) as exc:
            self.status_note.setText(str(exc))

    def _on_snap_missed_box(self) -> None:
        if not self._photo_id:
            return
        try:
            reg = self._parse_missed_region()
            refined = self.session.reconstruct_refine_device_box(self._photo_id, reg)
            self.missed_region_edit.setText(
                f"{refined['x']:.0f},{refined['y']:.0f},{refined['w']:.0f},{refined['h']:.0f}"
            )
            self.view.simulate_drag_photo_region(
                refined["x"], refined["y"], refined["w"], refined["h"]
            )
            self.status_note.setText(
                f"✨ Snapped missed-device box to edges ({refined['x']:.0f}, {refined['y']:.0f}, {refined['w']:.0f}×{refined['h']:.0f} px)."
            )
        except (KeyError, ValueError) as exc:
            self.status_note.setText(str(exc))

    def _on_photo_box_clicked(self, item_id: str) -> None:
        self._selected_item_id = item_id
        for row in range(self.device_list.count()):
            it = self.device_list.item(row)
            b = (it.data(Qt.ItemDataRole.UserRole) or {}) if it else {}
            if str(b.get("item_id") or "") == item_id:
                self.device_list.setCurrentRow(row)
                self._populate_inspector_from_row(row)
                break
        self.refresh_feedback()

    def _on_photo_region_drawn(self, region: dict) -> None:
        x = float(region.get("x") or 0.0)
        y = float(region.get("y") or 0.0)
        w = float(region.get("w") or 0.0)
        h = float(region.get("h") or 0.0)
        if getattr(self, "_redraw_mode_for_selected", False) and self._selected_item_id:
            try:
                res = self.session.reconstruct_update_device_box(
                    self._selected_item_id,
                    box={"x": x, "y": y, "w": w, "h": h},
                    snap_to_edges=True,
                )
                reg = res.get("region") or {"x": x, "y": y, "w": w, "h": h}
                self.view.clear_pending_missed_region()
                self.redraw_selected_btn.setChecked(False)
                self.status_note.setText(
                    f"✓ Updated & edge-snapped box for {self._selected_item_id} ({reg.get('x', 0):.0f}, {reg.get('y', 0):.0f}, {reg.get('w', 0):.0f}×{reg.get('h', 0):.0f} px)."
                )
                self.refresh_feedback()
                return
            except (KeyError, ValueError) as exc:
                self.status_note.setText(str(exc))
                return
        self.missed_region_edit.setText(f"{x:.0f},{y:.0f},{w:.0f},{h:.0f}")
        self.status_note.setText(
            f"Selected missed-device box ({x:.0f}, {y:.0f}, {w:.0f}×{h:.0f} px) — click '＋ Add Missed Device Box to Photo' or '✨ Snap Box to Device Edges'."
        )

    def _on_device_row_changed(self, row: int) -> None:
        if row < 0:
            return
        self._populate_inspector_from_row(row)
        from panelcad.reconstruct.feedback import render_annotated_cabinet_photo

        if self._photo_id:
            mode = self.current_filter_mode()
            img = render_annotated_cabinet_photo(
                self.session,
                photo_id=self._photo_id,
                filter_mode=mode,  # type: ignore[arg-type]
                selected_item_id=self._selected_item_id,
                include_banner=True,
            )
            pw = int(self._last_report.get("width_px") or img.width())
            ui_scale = max(1.0, min(3.2, pw / 1280.0))
            top_off = int(round(58 * ui_scale))
            self.view.set_annotated_feedback(
                img,
                source_width=pw,
                source_height=int(self._last_report.get("height_px") or max(1, img.height() - top_off)),
                boxes=self._last_report.get("boxes") or [],
                selected_item_id=self._selected_item_id,
                banner_top_offset=top_off,
            )

    def _on_save_selected_device(self) -> None:
        if not self._selected_item_id:
            return
        part = self.part_edit.text().strip()
        brand = self.brand_edit.text().strip()
        tag = self.tag_edit.text().strip()
        w = self.w_spin.value() or None
        h = self.h_spin.value() or None
        d = self.d_spin.value() or None
        mount = str(self.mount_combo.currentData() or "") or "din_rail"
        from panelcad.reconstruct.models import ensure_reconstruction, item_is_layout_ready

        store = ensure_reconstruction(self.session.project)
        item = store.item(self._selected_item_id)
        if part:
            item.label_part_number = part
            item.part_number = part
        if brand:
            item.label_brand = brand
            item.brand = brand
        if tag:
            item.tag = tag
        confirmed = False
        if part:
            try:
                self.session.reconstruct_confirm_item(
                    self._selected_item_id,
                    catalog=part,
                    manufacturer=brand,
                    tag=tag,
                )
                confirmed = item_is_layout_ready(item)
            except (KeyError, ValueError):
                confirmed = False
        if not confirmed and w and h and d:
            try:
                self.session.reconstruct_confirm_item(
                    self._selected_item_id,
                    envelope_w_mm=w,
                    envelope_h_mm=h,
                    envelope_d_mm=d,
                    tag=tag,
                    mount=mount,
                )
                if part:
                    item.part_number = part
                    item.label_part_number = part
                if brand:
                    item.brand = brand
                    item.label_brand = brand
                confirmed = item_is_layout_ready(item)
            except (KeyError, ValueError) as exc:
                self.status_note.setText(str(exc))
                return
        self.status_note.setText(
            f"Updated {self._selected_item_id} ({'confirmed' if confirmed else 'needs_info'})."
        )
        self.refresh_feedback()

    def _parse_missed_region(self) -> dict[str, float]:
        pending = self.view.pending_missed_region()
        txt = self.missed_region_edit.text().strip()
        if txt:
            parts = [float(p.strip()) for p in txt.split(",") if p.strip()]
            if len(parts) == 4:
                return {"x": parts[0], "y": parts[1], "w": parts[2], "h": parts[3]}
        if pending:
            return pending
        raise ValueError("Drag a box on the cabinet photo or enter x,y,w,h first.")

    def _on_add_missed_device(self) -> None:
        if not self._photo_id:
            return
        try:
            region = self._parse_missed_region()
        except ValueError as exc:
            self.status_note.setText(str(exc))
            return
        res = self.session.reconstruct_add_missed_device(
            self._photo_id,
            region,
            tag=self.missed_tag_edit.text().strip(),
            part_number=self.missed_part_edit.text().strip(),
            brand=self.missed_brand_edit.text().strip(),
            note=self.missed_note_edit.text().strip(),
        )
        new_item = res.get("item") or {}
        self._selected_item_id = str(new_item.get("id") or "") or None
        self.view.clear_pending_missed_region()
        self.draw_missed_btn.setChecked(False)
        self.missed_region_edit.clear()
        self.missed_tag_edit.clear()
        self.missed_part_edit.clear()
        self.missed_brand_edit.clear()
        self.missed_note_edit.clear()
        self.status_note.setText(
            f"Added missed device {self._selected_item_id} to {self._photo_id}."
        )
        self.refresh_feedback()

    def attach_closeup_to_selected(
        self,
        closeup_path: Path | str | None = None,
        *,
        closeup_photo_id: str = "",
        tag: str | None = None,
        part_number: str | None = None,
        brand: str | None = None,
        envelope_w_mm: float | None = None,
        envelope_h_mm: float | None = None,
        envelope_d_mm: float | None = None,
        mount: str | None = None,
    ) -> dict[str, Any]:
        if not self._selected_item_id:
            raise ValueError("Select a boxed device on the photo first.")
        res = self.session.reconstruct_link_closeup(
            self._selected_item_id,
            closeup_photo_path=closeup_path,
            closeup_photo_id=closeup_photo_id,
            tag=tag if tag is not None else self.tag_edit.text().strip(),
            part_number=part_number if part_number is not None else self.part_edit.text().strip(),
            brand=brand if brand is not None else self.brand_edit.text().strip(),
            envelope_w_mm=envelope_w_mm if envelope_w_mm is not None else (self.w_spin.value() or None),
            envelope_h_mm=envelope_h_mm if envelope_h_mm is not None else (self.h_spin.value() or None),
            envelope_d_mm=envelope_d_mm if envelope_d_mm is not None else (self.d_spin.value() or None),
            mount=mount if mount is not None else (str(self.mount_combo.currentData() or "") or "din_rail"),
        )
        self.status_note.setText(
            f"Linked close-up {res.get('closeup_photo_id') or ''} to {self._selected_item_id}."
        )
        self._populate_photos()
        self.refresh_feedback()
        return res

    def _on_attach_closeup_clicked(self) -> None:
        if not self._selected_item_id:
            return
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Attach Close-Up / Nameplate Photo for Selected Device",
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)",
        )
        if not chosen:
            return
        self.attach_closeup_to_selected(chosen)

    def export_annotated_to_path(self, output_path: Path | str) -> dict[str, Any]:
        res = self.session.reconstruct_export_photo_feedback(
            output_path=output_path,
            photo_id=self._photo_id,
            filter_mode=self.current_filter_mode(),
            selected_item_id=self._selected_item_id,
        )
        self._last_exported_path = Path(res["output_path"])
        self.status_note.setText(f"Exported annotated photo feedback to {self._last_exported_path}")
        return res

    def _on_export_clicked(self) -> None:
        default_name = f"cabinet-feedback-{self._photo_id or 'photo'}.png"
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            "Export Annotated Cabinet Photo Feedback",
            default_name,
            "PNG Images (*.png)",
        )
        if not chosen:
            return
        self.export_annotated_to_path(chosen)



class MobileCaptureServerDialog(QDialog):
    """Turnkey Self-Serve Phone Pairing & Live Mobile Field Capture Server Dialog.

    Lets any user start a local or LAN/VPN mobile web capture server in 1 click,
    scan a QR code on their phone, configure an optional webhook URL, and sync
    captured cabinet photos, close-ups, and missed-device boxes directly into the
    active PanelCAD session.
    """

    def __init__(
        self,
        session: Session,
        *,
        on_session_updated: Any = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self._on_session_updated = on_session_updated
        self._server: Any = None
        self.setWindowTitle("Mobile Field Capture Server — Connect Phone / Tablet")
        self.resize(620, 560)

        root = QVBoxLayout(self)
        intro = QLabel(
            "Start a local Mobile Field Capture server so you (or a field technician) can open "
            "PanelCAD Field Capture on any phone browser over Wi-Fi, a phone hotspot, or VPN/Tailscale. "
            "Snap cabinet photos, check Site-Exit Readiness, attach nameplate close-ups, and finger-drag "
            "missed device boxes right from the phone."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        cfg_form = QFormLayout()
        self.port_spin = QDoubleSpinBox()
        self.port_spin.setDecimals(0)
        self.port_spin.setRange(0, 65535)
        self.port_spin.setValue(8788)
        self.webhook_edit = QLineEdit()
        self.webhook_edit.setPlaceholderText("Optional: https://your-webhook-or-agent.example/hook")
        cfg_form.addRow("Port (0 = auto):", self.port_spin)
        cfg_form.addRow("Webhook URL (optional):", self.webhook_edit)
        root.addLayout(cfg_form)

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("▶ Start Phone Capture Server")
        self.stop_btn = QPushButton("■ Stop Server")
        self.stop_btn.setEnabled(False)
        self.sync_btn = QPushButton("↻ Refresh Desktop from Phone Captures")
        self.start_btn.clicked.connect(self.start_server)
        self.stop_btn.clicked.connect(self.stop_server)
        self.sync_btn.clicked.connect(self._notify_updated)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(self.sync_btn)
        root.addLayout(btn_row)

        self.status_label = QLabel("Server stopped. Click '▶ Start Phone Capture Server' to generate your phone QR code.")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(
            "font-weight: 700; padding: 8px 10px; border-radius: 5px; background: #1e293b; color: #f8fafc;"
        )
        root.addWidget(self.status_label)

        mid = QHBoxLayout()
        self.qr_label = QLabel("QR Code")
        self.qr_label.setFixedSize(210, 210)
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setStyleSheet("background: #ffffff; color: #0f172a; border: 1px solid #cbd5e1; border-radius: 8px;")
        mid.addWidget(self.qr_label)

        right = QVBoxLayout()
        right.addWidget(QLabel("Reachable Phone URLs (Wi-Fi / Hotspot / VPN):"))
        self.url_list = QListWidget()
        self.url_list.currentRowChanged.connect(self._on_url_selected)
        right.addWidget(self.url_list, 1)

        cli_hint = QLabel("Headless / Always-On VM CLI: panelcad capture-server --host 0.0.0.0 --port 8788 --workspace ./panelcad-captures")
        cli_hint.setStyleSheet("font-family: monospace; font-size: 11px; color: #94a3b8;")
        cli_hint.setWordWrap(True)
        right.addWidget(cli_hint)
        mid.addLayout(right, 1)
        root.addLayout(mid, 1)

        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        root.addLayout(close_row)

    @property
    def server(self) -> Any:
        return self._server

    def start_server(self) -> str:
        from panelcad.reconstruct.capture_web import CaptureWebServer

        if self._server is not None:
            self.stop_server()
        import tempfile

        base_dir = (
            Path(self.session.path).parent / ".panelcad-mobile-capture"
            if self.session.path
            else Path(tempfile.mkdtemp(prefix="panelcad-mobile-capture-"))
        )
        req_port = int(self.port_spin.value())
        try:
            self._server = CaptureWebServer(
                base_dir,
                host="0.0.0.0",
                port=req_port,
                webhook_url=self.webhook_edit.text().strip(),
                live_session=self.session,
            )
            self._server.start(daemon=True)
        except OSError:
            self._server = CaptureWebServer(
                base_dir,
                host="0.0.0.0",
                port=0,
                webhook_url=self.webhook_edit.text().strip(),
                live_session=self.session,
            )
            self._server.start(daemon=True)

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        urls = self._server.reachable_urls()
        self.url_list.clear()
        for u in urls:
            item = QListWidgetItem(f"{u['label']}: {u['url']}")
            item.setData(Qt.ItemDataRole.UserRole, u["url"])
            self.url_list.addItem(item)
        primary = urls[0]["url"] if urls else self._server.base_url
        self.status_label.setText(f"✓ Phone Capture Server running on port {self._server.port} — Scan QR or open {primary}")
        self.status_label.setStyleSheet(
            "font-weight: 700; padding: 8px 10px; border-radius: 5px; background: #064e3b; color: #ecfdf5;"
        )
        if self.url_list.count() > 0:
            self.url_list.setCurrentRow(0)
        else:
            self._render_qr_for_url(primary)
        return primary

    def _on_url_selected(self, row: int) -> None:
        if row < 0:
            return
        item = self.url_list.item(row)
        if item is None:
            return
        url = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if url:
            self._render_qr_for_url(url)

    def _render_qr_for_url(self, url: str) -> None:
        from panelcad.reconstruct.capture_web import render_qr_qimage

        qimg = render_qr_qimage(url, scale=5, border=3)
        pix = QPixmap.fromImage(qimg).scaled(
            194,
            194,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        self.qr_label.setPixmap(pix)

    def _notify_updated(self) -> None:
        if callable(self._on_session_updated):
            self._on_session_updated()

    def stop_server(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_label.setText("Server stopped.")
        self.status_label.setStyleSheet(
            "font-weight: 700; padding: 8px 10px; border-radius: 5px; background: #1e293b; color: #f8fafc;"
        )
