"""Show a panel photo (or a crop) and jump from Problems / a layout part."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from panelcad.api.session import Session


def _photo_caption(photo: dict, item: dict) -> str:
    name = str(photo.get("original_name") or "").strip()
    role = str(photo.get("role") or "").replace("_", " ")
    label = item.get("part_number") or item.get("label_text") or item.get("tag") or item.get("id") or ""
    head = name or str(photo.get("id") or "")
    return f"{head}  {role}  {label}".strip()


class PhotoView(QLabel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pix = QPixmap()
        self._region: tuple[float, float, float, float] | None = None
        # The dock copy must be free to shrink with the bottom splitter.
        # The dialog sets its own floor after it creates this view.
        self.setMinimumSize(0, 0)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_photo(self, path: Path, region: dict | None = None) -> None:
        self._pix = QPixmap(str(path))
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
        row.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)
        self.show_payload(payload)

    def show_payload(self, payload: dict) -> None:
        photo = payload.get("photo") or {}
        item = payload.get("item") or {}
        path = Path(str(payload.get("path") or ""))
        self.caption.setText(_photo_caption(photo, item))
        if path.is_file():
            self.view.set_photo(path, payload.get("region") or item.get("region"))


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
