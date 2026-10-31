"""One Photos → drawings run. Existing reconstruct steps stay on their own."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.reconstruct.review_queue import FlaggedItem, queue_line


class PhotosToDrawingsResult:
    """What the run did. The main window uses this to switch sheets."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.summary = str(payload.get("summary") or "")
        self.sheet_id = str(payload.get("sheet_id") or "")
        self.drawings_generated = bool(payload.get("drawings_generated"))
        self.stopped = bool(payload.get("stopped"))
        self.queue = list(payload.get("queue") or [])
        self.placed = list(payload.get("placed") or [])


class PhotosToDrawingsDialog(QDialog):
    """From imported photos to drawings, stopping only on the review queue."""

    def __init__(
        self,
        session: Session,
        parent: QWidget | None = None,
        *,
        vision: Any = None,
        unboxed_callouts: Any = None,
        extra_flags: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.vision = vision
        self.unboxed_callouts = unboxed_callouts
        self.extra_flags = list(extra_flags or [])
        self.result_info: PhotosToDrawingsResult | None = None
        self._accepted: set[str] = set()
        self._dismissed: set[str] = set()
        self._last: dict[str, Any] | None = None
        self.setObjectName("photosToDrawings")
        self.setWindowTitle("Photos → drawings")
        self.resize(960, 700)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "One run from imported photos to drawings. "
            "It stops only for flagged items: missing info, low confidence, "
            "or a possible missed device. Everything else passes through. "
            "Photo intake, the cabinet photo overlay, device review, and drawing "
            "generation still open on their own."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        layout.addWidget(QLabel("Photos"))
        self.photo_list = QListWidget()
        self.photo_list.setObjectName("photosToDrawingsPhotos")
        self.photo_list.setMaximumHeight(110)
        layout.addWidget(self.photo_list)
        actions = QHBoxLayout()
        add = QPushButton("Add photos…")
        add.setObjectName("photosToDrawingsAdd")
        add.clicked.connect(self._add_photos)
        self.run_btn = QPushButton("Run Photos → drawings")
        self.run_btn.setObjectName("photosToDrawingsRun")
        self.run_btn.clicked.connect(self.run_guided)
        actions.addWidget(add)
        actions.addWidget(self.run_btn)
        actions.addStretch()
        layout.addLayout(actions)
        self.status = QLabel(
            "Add photos, then run. Drawings are written when nothing is flagged."
        )
        self.status.setObjectName("photosToDrawingsStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addWidget(QLabel("Review queue"))
        self.queue = QListWidget()
        self.queue.setObjectName("photosToDrawingsQueue")
        self.queue.currentItemChanged.connect(lambda *_a: self._show_detail())
        layout.addWidget(self.queue, 1)
        self.detail = QLabel("Flagged items show here. Unflagged items are not listed.")
        self.detail.setObjectName("photosToDrawingsDetail")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
        row = QHBoxLayout()
        self.steps_btn = QPushButton("Open reconstruct steps…")
        self.steps_btn.setObjectName("photosToDrawingsSteps")
        self.steps_btn.clicked.connect(self._open_steps)
        self.feedback_btn = QPushButton("Open photo feedback…")
        self.feedback_btn.setObjectName("photosToDrawingsFeedback")
        self.feedback_btn.clicked.connect(self._open_feedback)
        self.accept_btn = QPushButton("Accept read")
        self.accept_btn.setObjectName("photosToDrawingsAccept")
        self.accept_btn.clicked.connect(self._accept_selected)
        self.dismiss_btn = QPushButton("Not a missed device")
        self.dismiss_btn.setObjectName("photosToDrawingsDismiss")
        self.dismiss_btn.clicked.connect(self._dismiss_selected)
        self.continue_btn = QPushButton("Continue")
        self.continue_btn.setObjectName("photosToDrawingsContinue")
        self.continue_btn.clicked.connect(self.run_guided)
        for button in (
            self.steps_btn,
            self.feedback_btn,
            self.accept_btn,
            self.dismiss_btn,
            self.continue_btn,
        ):
            row.addWidget(button)
        layout.addLayout(row)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        self._placeholder_queue("Flagged items will be listed here.")
        self._refresh_photos()
        self._sync_actions()

    def flagged_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index in range(self.queue.count()):
            data = self.queue.item(index).data(Qt.ItemDataRole.UserRole)
            if isinstance(data, dict):
                rows.append(data)
        return rows

    def run_guided(self) -> dict[str, Any]:
        try:
            result = self.session.reconstruct_photos_to_drawings(
                vision=self.vision,
                unboxed_callouts=self.unboxed_callouts,
                extra_flags=list(self.extra_flags),
                accepted_ids=sorted(self._accepted),
                dismissed_ids=sorted(self._dismissed),
            )
        except ValueError as exc:
            self.status.setText(str(exc))
            self._last = None
            self.result_info = None
            self._sync_actions()
            return {}
        self._last = result
        self.result_info = PhotosToDrawingsResult(result)
        self._show_result(result)
        return result

    def _add_photos(self) -> None:
        paths, _selected = QFileDialog.getOpenFileNames(
            self,
            "Panel photos",
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)",
        )
        if not paths:
            return
        try:
            self.session.reconstruct_add_photos(paths, role="backpanel")
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
            return
        self._refresh_photos()

    def _open_steps(self) -> None:
        from panelcad.gui.reconstruct_wizard import ReconstructWizard

        wizard = ReconstructWizard(self.session, self, vision=self.vision)
        wizard.exec()
        self._refresh_photos()

    def _open_feedback(self) -> None:
        from panelcad.gui.photo_viewer import PhotoFeedbackDialog

        flag = self._selected()
        photo_id = str(flag.get("photo_id") or "") if flag else ""
        source = str(flag.get("source_id") or "") if flag else ""
        dialog = PhotoFeedbackDialog(
            self.session,
            photo_id=photo_id or None,
            selected_item_id=source or None,
            parent=self,
        )
        dialog.exec()

    def _accept_selected(self) -> None:
        flag = self._selected()
        if flag is None or str(flag.get("reason") or "") != "low_confidence":
            return
        source = str(flag.get("source_id") or "")
        flag_id = str(flag.get("id") or "")
        if source:
            self._accepted.add(source)
        if flag_id:
            self._accepted.add(flag_id)
        self.run_guided()

    def _dismiss_selected(self) -> None:
        flag = self._selected()
        if flag is None or str(flag.get("reason") or "") != "possible_missed_device":
            return
        self._dismissed.add(str(flag.get("id") or ""))
        source = str(flag.get("source_id") or "")
        if source:
            self._dismissed.add(source)
        self.run_guided()

    def _show_result(self, result: dict[str, Any]) -> None:
        self.status.setText(str(result.get("summary") or ""))
        rows = list(result.get("queue") or [])
        self.queue.clear()
        if not rows:
            self._placeholder_queue("No flagged items.")
            self.detail.setText("Unflagged items passed through.")
        else:
            for row in rows:
                label = queue_line(FlaggedItem.model_validate(row))
                widget = QListWidgetItem(label)
                widget.setData(Qt.ItemDataRole.UserRole, row)
                self.queue.addItem(widget)
            self.queue.setCurrentRow(0)
        self._sync_actions()

    def _show_detail(self) -> None:
        flag = self._selected()
        if flag is None:
            return
        text = str(flag.get("detail") or "").strip() or str(flag.get("title") or "")
        self.detail.setText(text)
        self._sync_actions()

    def _sync_actions(self) -> None:
        flag = self._selected()
        reason = str(flag.get("reason") or "") if flag else ""
        self.accept_btn.setEnabled(reason == "low_confidence")
        self.dismiss_btn.setEnabled(reason == "possible_missed_device")
        store = self.session.project.reconstruct
        self.feedback_btn.setEnabled(bool(store and store.photos))
        self.continue_btn.setEnabled(bool(self._last and self._last.get("stopped")))

    def _selected(self) -> dict[str, Any] | None:
        item = self.queue.currentItem()
        if item is None:
            return None
        data = item.data(Qt.ItemDataRole.UserRole)
        return data if isinstance(data, dict) else None

    def _item(self, item_id: str):
        store = self.session.project.reconstruct
        if store is None or not item_id:
            return None
        try:
            return store.item(item_id)
        except KeyError:
            return None

    def _refresh_photos(self) -> None:
        self.photo_list.clear()
        store = self.session.project.reconstruct
        if store is None:
            return
        for photo in store.photos:
            row = QListWidgetItem(f"{photo.id}   {str(photo.role).replace('_', ' ')}")
            row.setData(Qt.ItemDataRole.UserRole, photo.id)
            self.photo_list.addItem(row)

    def _placeholder_queue(self, text: str) -> None:
        self.queue.clear()
        note = QListWidgetItem(text)
        note.setFlags(Qt.ItemFlag.NoItemFlags)
        self.queue.addItem(note)
