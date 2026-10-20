"""Start page: recent jobs, a new drawing, or an imported print."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from panelcad.io.winpath import display_name, project_presence
from panelcad.theme import MARGIN_PX, SPACING_PX, forget_project, recent_projects
from panelcad.version import __version__


class StartPage(QWidget):
    new_blank = Signal()
    new_template = Signal()
    circuit_builder = Signal()
    open_project = Signal()
    import_print = Signal()
    reconstruct_photos = Signal()
    open_sample = Signal()
    tour_requested = Signal()
    recent_opened = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("startPage")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        scroll.setWidget(host)
        outer.addWidget(scroll)
        layout = QVBoxLayout(host)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(SPACING_PX)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        column = QWidget()
        column.setMaximumWidth(960)
        column_layout = QVBoxLayout(column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(SPACING_PX)
        layout.addWidget(column, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)

        title = QLabel("PanelCAD")
        font = title.font()
        font.setPointSize(22)
        font.setBold(True)
        title.setFont(font)
        column_layout.addWidget(title)
        version = QLabel(f"Version {__version__}")
        version.setObjectName("muted")
        column_layout.addWidget(version)
        hint = QLabel("Open a recent project, start a drawing, photograph a physical panel, or attach an old print.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        column_layout.addWidget(hint)

        cards = QHBoxLayout()
        cards.setSpacing(SPACING_PX)
        for text, icon, signal in (
            ("New blank", "file-plus", self.new_blank),
            ("From template", "layout-dashboard", self.new_template),
            ("Circuit Builder", "workflow", self.circuit_builder),
            ("Open", "folder-open", self.open_project),
            ("Import print", "import", self.import_print),
            ("From photos", "layout-dashboard", self.reconstruct_photos),
        ):
            cards.addWidget(_card(text, icon, signal.emit))
        column_layout.addLayout(cards)

        sample = QPushButton("Open sample: catalog DOL starter")
        sample.setObjectName("openSample")
        sample.setToolTip(
            "Open a copy of a starter made only of catalog parts with published sizes. "
            "Learn more: Sample project."
        )
        sample.clicked.connect(self.open_sample.emit)
        column_layout.addWidget(sample)
        tour = QPushButton("Take a short tour")
        tour.setObjectName("takeTour")
        tour.setToolTip("Walk the Library, the sheet, Wire, the BOM, print, and Troubleshoot.")
        tour.clicked.connect(self.tour_requested.emit)
        column_layout.addWidget(tour)

        column_layout.addSpacing(MARGIN_PX)
        recent_label = QLabel("Recent projects")
        recent_font = recent_label.font()
        recent_font.setBold(True)
        recent_label.setFont(recent_font)
        column_layout.addWidget(recent_label)
        self.recent_host = QVBoxLayout()
        self.recent_host.setSpacing(SPACING_PX)
        column_layout.addLayout(self.recent_host)
        self.empty = QLabel("Start here. Create a drawing, or open one you already have.")
        self.empty.setObjectName("muted")
        self.empty.setWordWrap(True)
        column_layout.addWidget(self.empty)
        self.refresh()

    def refresh(self) -> None:
        while self.recent_host.count():
            item = self.recent_host.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        paths = recent_projects()
        self.empty.setVisible(not paths)
        for path in paths:
            self.recent_host.addWidget(_RecentRow(path, self.recent_opened.emit, self.refresh))


def _recent_parts(path: str) -> tuple[str, str]:
    """Name and folder. Backslashes count so a Windows path reads the same on Linux."""

    text = path.replace("\\", "/").rstrip("/")
    file = Path(text)
    name = Path(display_name(path)).stem or display_name(path) or text
    folder = str(file.parent)
    if folder in {"", "."}:
        folder = ""
    return name, folder


class _ElideLabel(QLabel):
    """One line, clipped to the width the layout actually gives it."""

    def __init__(self, text: str, *, bold: bool = False) -> None:
        super().__init__(text)
        self._full = text
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        if bold:
            font = self.font()
            font.setBold(True)
            self.setFont(font)

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setFont(self.font())
        color = self.palette().color(self.foregroundRole())
        if self.objectName() == "muted":
            from panelcad.gui.theme import ui_qcolor

            color = ui_qcolor("muted")
        painter.setPen(color)
        shown = QFontMetrics(self.font()).elidedText(
            self._full, Qt.TextElideMode.ElideMiddle, max(0, self.width() - 2)
        )
        painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), shown)
        painter.end()


def _card(text: str, icon: str, slot) -> QToolButton:
    from panelcad.gui.theme import themed_icon

    button = QToolButton()
    button.setText(text)
    button.setIcon(themed_icon(icon))
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
    button.setIconSize(QSize(28, 28))
    button.setMinimumSize(140, 88)
    button.clicked.connect(lambda _checked=False, fn=slot: fn())
    return button


class _RecentRow(QFrame):
    def __init__(self, path: str, open_path, refreshed) -> None:
        super().__init__()
        self._path = path
        self._open = open_path
        self._refreshed = refreshed
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(path)
        self.setStatusTip(path)
        try:
            missing = project_presence(path) == "missing"
        except OSError:
            missing = False
        self._missing = missing
        row = QHBoxLayout(self)
        row.setContentsMargins(MARGIN_PX, SPACING_PX, MARGIN_PX, SPACING_PX)
        thumb = QLabel()
        thumb.setFixedSize(72, 48)
        from panelcad.theme import ui_hex

        thumb.setStyleSheet(
            f"background: {ui_hex('panel')}; color: {ui_hex('text')};"
            f" border: 1px solid {ui_hex('border')};"
        )
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setText("DWG")
        row.addWidget(thumb)
        text = QVBoxLayout()
        shown, folder_text = _recent_parts(path)
        self.setAccessibleName(shown or path)
        self.setAccessibleDescription(path)
        name = _ElideLabel(shown, bold=True)
        if self._missing:
            name.setObjectName("muted")
            color = ui_hex("muted")
            name.setStyleSheet(f"color: {color};")
        folder = _ElideLabel(folder_text or " ")
        folder.setObjectName("muted")
        file = Path(path.replace("\\", "/"))
        when = "Not on this computer"
        if not self._missing:
            try:
                if file.is_file():
                    stamp = datetime.fromtimestamp(file.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                    when = f"Modified {stamp}"
            except OSError:
                when = "Not on this computer"
        modified = _ElideLabel(when)
        modified.setObjectName("muted")
        text.addWidget(name)
        text.addWidget(folder)
        text.addWidget(modified)
        row.addLayout(text, 1)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._open(self._path)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            self._open(self._path)
            event.accept()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802
        from PySide6.QtGui import QStatusTipEvent
        from PySide6.QtWidgets import QApplication

        QApplication.sendEvent(self.window(), QStatusTipEvent(self._path))
        super().focusInEvent(event)

    def _menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction("Open", lambda: self._open(self._path))
        remove = QAction("Remove from recent", self)
        remove.triggered.connect(self._remove)
        reveal = QAction("Reveal in folder", self)
        reveal.triggered.connect(self._reveal)
        menu.addAction(remove)
        menu.addAction(reveal)
        menu.exec(self.mapToGlobal(pos))

    def _remove(self) -> None:
        forget_project(self._path)
        self._refreshed()

    def _reveal(self) -> None:
        folder = str(Path(self._path.replace("\\", "/")).parent)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
