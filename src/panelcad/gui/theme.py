"""Qt side of the theme: stylesheet, icons, dialogs, and live apply.

Hex colors live in ``panelcad.theme``. This module only looks them up.
Icons are Lucide (ISC). See ``gui/icons/LICENSE``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QFileSystemWatcher, QObject, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QIconEngine, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QApplication, QDialog, QScrollArea, QSizePolicy, QWidget

from panelcad.theme import (
    canvas_hex,
    get_canvas,
    get_theme,
    resolved_theme_name,
    subscribe,
    ui_hex,
)

_ICONS = Path(__file__).resolve().parent / "icons"
_installed = False
_fusion_applied = False
_scheme_busy = False
_refreshing = False
_hooks: _ThemeHooks | None = None

# Windows 10 1903+ immersive dark caption. Qt names this DWMWA_USE_IMMERSIVE_DARK_MODE.
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20


def ui_qcolor(role: str) -> QColor:
    return QColor(ui_hex(role))


def canvas_qcolor(role: str) -> QColor:
    return QColor(canvas_hex(role))


def ui_font() -> QFont:
    """10 pt UI font. A fresh face, so a leaked application font does not grow the table."""

    family = "Segoe UI" if sys.platform == "win32" else QFont().defaultFamily()
    font = QFont(family)
    font.setBold(False)
    font.setWeight(QFont.Weight.Normal)
    font.setPixelSize(-1)
    font.setPointSize(10)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return font


def _qss_url(name: str, role: str) -> str:
    path = _cached_svg(name, ui_hex(role))
    if not path:
        return "none"
    return f'url("{path}")'


def stylesheet() -> str:
    """Application stylesheet for the resolved light or dark palette."""

    g = ui_hex
    check = _qss_url("check", "on_accent")
    up = _qss_url("chevron-up", "text")
    down = _qss_url("chevron-down", "text")
    close = _qss_url("x", "dock_title_text")
    float_icon = _qss_url("copy", "dock_title_text")
    tab_close = _qss_url("x", "text")
    return f"""
    QMainWindow, QDialog, QWizard, QMessageBox {{
        background: {g("window")};
        color: {g("text")};
    }}
    QWidget {{
        color: {g("text")};
    }}
    QMenuBar {{
        background: {g("panel")};
        color: {g("text")};
        padding: 2px;
    }}
    QMenuBar::item {{
        background: transparent;
        padding: 4px 10px;
    }}
    QMenuBar::item:selected {{
        background: {g("menu_hover")};
    }}
    QMenu {{
        background: {g("panel")};
        color: {g("text")};
        border: 1px solid {g("border")};
        padding: 4px;
    }}
    QMenu::item {{
        padding: 4px 18px;
    }}
    QMenu::item:selected {{
        background: {g("menu_hover")};
        color: {g("text")};
    }}
    QToolBar {{
        background: {g("panel")};
        border: none;
        border-bottom: 1px solid {g("border")};
        spacing: 4px;
        padding: 4px;
    }}
    QToolBar::separator {{
        background: {g("border")};
        width: 1px;
        margin: 4px 6px;
    }}
    QToolButton {{
        background: transparent;
        color: {g("text")};
        border: none;
        padding: 4px;
        border-radius: 3px;
    }}
    QToolButton:hover {{
        background: {g("button_hover")};
    }}
    QStatusBar {{
        background: {g("status")};
        color: {g("status_text")};
        padding: 2px 8px;
    }}
    QStatusBar QLabel {{
        background: transparent;
        color: {g("status_text")};
        padding: 0 8px;
    }}
    QDockWidget {{
        background: {g("panel")};
        color: {g("dock_title_text")};
        titlebar-close-icon: {close};
        titlebar-normal-icon: {float_icon};
    }}
    QDockWidget::title {{
        background: {g("dock_title")};
        color: {g("dock_title_text")};
        padding: 6px 8px;
        text-align: left;
    }}
    QDockWidget::close-button, QDockWidget::float-button {{
        background: transparent;
        border: none;
        padding: 0px;
    }}
    QDockWidget::close-button:hover, QDockWidget::float-button:hover {{
        background: {g("button_hover")};
    }}
    QDockWidget > QWidget {{
        background: {g("panel")};
        color: {g("text")};
    }}
    QMainWindow::separator {{
        background: {g("border")};
        width: 8px;
        height: 8px;
    }}
    QMainWindow::separator:hover {{
        background: {g("tab_accent")};
    }}
    QTabBar::tab {{
        background: {g("tab")};
        color: {g("text")};
        padding: 6px 12px;
        border: none;
    }}
    QTabBar::tab:selected {{
        background: {g("tab_selected")};
        color: {g("text")};
        border-bottom: 2px solid {g("tab_accent")};
    }}
    QTabBar::close-button {{
        image: {tab_close};
        subcontrol-position: right;
    }}
    QTabBar::close-button:hover {{
        background: {g("button_hover")};
        border-radius: 2px;
    }}
    QPushButton {{
        background: {g("button")};
        color: {g("text")};
        border: 1px solid {g("border")};
        padding: 4px 12px;
        border-radius: 3px;
    }}
    QPushButton:hover {{
        background: {g("button_hover")};
    }}
    QPushButton:disabled {{
        color: {g("muted")};
    }}
    QLineEdit, QComboBox, QPlainTextEdit, QTextEdit,
    QListWidget, QTreeWidget, QTableWidget, QTableView {{
        background: {g("field")};
        color: {g("text")};
        border: 1px solid {g("border")};
        padding: 3px 6px;
        selection-background-color: {g("selection")};
        selection-color: {g("selection_text")};
        alternate-background-color: {g("alt_row")};
    }}
    QSpinBox, QDoubleSpinBox {{
        background: {g("field")};
        color: {g("text")};
        border: 1px solid {g("border")};
        padding: 3px 6px;
        padding-right: 18px;
        selection-background-color: {g("selection")};
        selection-color: {g("selection_text")};
    }}
    QSpinBox::up-button, QDoubleSpinBox::up-button {{
        subcontrol-origin: border;
        subcontrol-position: top right;
        width: 16px;
        background: {g("button")};
        border-left: 1px solid {g("border")};
        border-bottom: 1px solid {g("border")};
    }}
    QSpinBox::down-button, QDoubleSpinBox::down-button {{
        subcontrol-origin: border;
        subcontrol-position: bottom right;
        width: 16px;
        background: {g("button")};
        border-left: 1px solid {g("border")};
    }}
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
        background: {g("button_hover")};
    }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
        image: {up};
        width: 12px;
        height: 12px;
    }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
        image: {down};
        width: 12px;
        height: 12px;
    }}
    QCheckBox, QRadioButton {{
        color: {g("text")};
        background: transparent;
        spacing: 6px;
    }}
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 16px;
        height: 16px;
        background: {g("field")};
        border: 1px solid {g("border")};
    }}
    QCheckBox::indicator {{
        border-radius: 3px;
    }}
    QRadioButton::indicator {{
        border-radius: 8px;
    }}
    QCheckBox::indicator:checked {{
        background: {g("accent")};
        border: 1px solid {g("accent")};
        image: {check};
    }}
    QRadioButton::indicator:checked {{
        background: {g("accent")};
        border: 1px solid {g("accent")};
    }}
    QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
        background: {g("panel")};
        border: 1px solid {g("muted")};
    }}
    QHeaderView {{
        background: {g("header")};
    }}
    QHeaderView::section {{
        background: {g("header")};
        color: {g("text")};
        padding: 4px 6px;
        border: none;
        border-right: 1px solid {g("border")};
        border-bottom: 1px solid {g("border")};
    }}
    QTableCornerButton::section {{
        background: {g("header")};
        border: none;
        border-right: 1px solid {g("border")};
        border-bottom: 1px solid {g("border")};
    }}
    QAbstractScrollArea::viewport {{
        background: {g("window")};
    }}
    QScrollArea, QScrollArea > QWidget > QWidget {{
        background: {g("window")};
        color: {g("text")};
    }}
    QLabel#muted {{
        color: {g("muted")};
    }}
    QToolTip {{
        background: {g("panel")};
        color: {g("text")};
        border: 1px solid {g("border")};
    }}
    QScrollBar:vertical {{
        background: {g("window")};
        width: 12px;
        margin: 0;
    }}
    QScrollBar:horizontal {{
        background: {g("window")};
        height: 12px;
        margin: 0;
    }}
    QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
        background: {g("border")};
        min-height: 24px;
        min-width: 24px;
        border-radius: 4px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{
        height: 0;
        width: 0;
    }}
    """


def table_stylesheet() -> str:
    g = ui_hex
    return (
        f"QTableView {{ background: {g('field')}; color: {g('text')}; "
        f"alternate-background-color: {g('alt_row')}; gridline-color: {g('gridline')}; "
        f"selection-background-color: {g('selection')}; selection-color: {g('selection_text')}; }}"
        "QTableView::item { padding: 2px; border: 0px; }"
        f"QTableView::item:selected {{ background: {g('selection')}; color: {g('selection_text')}; }}"
        "QTableView::item:focus { border: 0px; outline: none; }"
        f"QHeaderView {{ background: {g('header')}; }}"
        f"QHeaderView::section {{ background: {g('header')}; color: {g('text')}; "
        f"padding: 4px 6px; border: none; border-right: 1px solid {g('border')}; "
        f"border-bottom: 1px solid {g('border')}; }}"
        f"QTableCornerButton::section {{ background: {g('header')}; border: none; }}"
    )


def banner_stylesheet(status: str) -> str:
    from panelcad.theme import banner_roles

    fg, bg = banner_roles(status)
    return (
        f"background: {ui_hex(bg)}; color: {ui_hex(fg)}; padding: 8px; "
        "border-radius: 4px; font-weight: 600;"
    )


def build_palette() -> QPalette:
    """Active, inactive, and disabled colours from the current tokens."""

    palette = QPalette()
    window = ui_qcolor("window")
    text = ui_qcolor("text")
    field = ui_qcolor("field")
    muted = ui_qcolor("muted")
    button = ui_qcolor("button")
    panel = ui_qcolor("panel")
    alt = ui_qcolor("alt_row")
    selection = ui_qcolor("selection")
    selection_text = ui_qcolor("selection_text")
    border = ui_qcolor("border")
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        palette.setColor(group, QPalette.ColorRole.Window, window)
        palette.setColor(group, QPalette.ColorRole.WindowText, text)
        palette.setColor(group, QPalette.ColorRole.Base, field)
        palette.setColor(group, QPalette.ColorRole.AlternateBase, alt)
        palette.setColor(group, QPalette.ColorRole.Text, text)
        palette.setColor(group, QPalette.ColorRole.Button, button)
        palette.setColor(group, QPalette.ColorRole.ButtonText, text)
        palette.setColor(group, QPalette.ColorRole.Highlight, selection)
        palette.setColor(group, QPalette.ColorRole.HighlightedText, selection_text)
        palette.setColor(group, QPalette.ColorRole.ToolTipBase, panel)
        palette.setColor(group, QPalette.ColorRole.ToolTipText, text)
        palette.setColor(group, QPalette.ColorRole.PlaceholderText, muted)
        palette.setColor(group, QPalette.ColorRole.Light, window)
        palette.setColor(group, QPalette.ColorRole.Midlight, panel)
        palette.setColor(group, QPalette.ColorRole.Mid, border)
        palette.setColor(group, QPalette.ColorRole.Dark, border)
        palette.setColor(group, QPalette.ColorRole.Shadow, border)
    disabled = QPalette.ColorGroup.Disabled
    palette.setColor(disabled, QPalette.ColorRole.Window, window)
    palette.setColor(disabled, QPalette.ColorRole.WindowText, muted)
    palette.setColor(disabled, QPalette.ColorRole.Base, field)
    palette.setColor(disabled, QPalette.ColorRole.AlternateBase, alt)
    palette.setColor(disabled, QPalette.ColorRole.Text, muted)
    palette.setColor(disabled, QPalette.ColorRole.Button, button)
    palette.setColor(disabled, QPalette.ColorRole.ButtonText, muted)
    palette.setColor(disabled, QPalette.ColorRole.Highlight, selection)
    palette.setColor(disabled, QPalette.ColorRole.HighlightedText, muted)
    palette.setColor(disabled, QPalette.ColorRole.ToolTipBase, panel)
    palette.setColor(disabled, QPalette.ColorRole.ToolTipText, muted)
    palette.setColor(disabled, QPalette.ColorRole.PlaceholderText, muted)
    return palette


def apply_palette(app: QApplication | None = None) -> None:
    target = app or QApplication.instance()
    if target is None:
        return
    target.setPalette(build_palette())


def apply_qt_color_scheme(app: QApplication | None = None) -> None:
    """Tell Qt the window is light or dark so native captions follow.

    Match Windows clears the override. An explicit Light or Dark choice sets it.
    """

    global _scheme_busy
    target = app or QApplication.instance()
    if target is None:
        return
    hints = target.styleHints()
    choice = get_theme()
    _scheme_busy = True
    try:
        if choice == "system":
            unset = getattr(hints, "unsetColorScheme", None)
            if callable(unset):
                unset()
            return
        setter = getattr(hints, "setColorScheme", None)
        if not callable(setter):
            return
        scheme = Qt.ColorScheme.Dark if resolved_theme_name() == "dark" else Qt.ColorScheme.Light
        setter(scheme)
    finally:
        _scheme_busy = False


def apply_titlebar(widget: QWidget) -> None:
    """Dark or light caption via DwmSetWindowAttribute. No-op off Windows.

    Qt 6.8 and newer set the caption from ``setColorScheme``. Calling DWM
    attribute 20 as well makes Qt log that it could not set a light border.
    """

    if sys.platform != "win32" or not widget.isWindow():
        return
    app = QApplication.instance()
    if app is not None and callable(getattr(app.styleHints(), "setColorScheme", None)):
        return
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001
        return
    dark = resolved_theme_name() == "dark"
    value = ctypes.c_int(1 if dark else 0)
    hwnd = wintypes.HWND(int(widget.winId()))
    ctypes.windll.dwmapi.DwmSetWindowAttribute(
        hwnd,
        _DWMWA_USE_IMMERSIVE_DARK_MODE,
        ctypes.byref(value),
        ctypes.sizeof(value),
    )


def apply_stylesheet(app: QApplication | None = None) -> None:
    target = app or QApplication.instance()
    if target is None:
        return
    apply_qt_color_scheme(target)
    apply_palette(target)
    target.setStyleSheet(stylesheet())
    for widget in target.topLevelWidgets():
        apply_titlebar(widget)


def install(app: QApplication | None = None) -> None:
    """Fusion, font, palette, stylesheet, dialog clamp, and OS scheme changes. Idempotent."""

    global _installed, _hooks, _fusion_applied
    target = app or QApplication.instance()
    if target is None:
        return
    # setStyleSheet wraps the style and clears objectName. Calling setStyle
    # again sends StyleChange through every main window that is still alive.
    if not _fusion_applied:
        style = target.style()
        name = style.objectName().lower() if style is not None else ""
        if name != "fusion":
            target.setStyle("Fusion")
        _fusion_applied = True
    target.setFont(ui_font())
    apply_stylesheet(target)
    if _installed:
        return
    subscribe(_on_settings_changed)
    _hooks = _ThemeHooks(target)
    target.installEventFilter(_hooks)
    hints = target.styleHints()
    if hasattr(hints, "colorSchemeChanged"):
        hints.colorSchemeChanged.connect(lambda *_args: _on_system_scheme())
    _installed = True


def system_is_dark(app: QApplication | None = None) -> bool | None:
    target = app or QApplication.instance()
    if target is None:
        return None
    scheme = target.styleHints().colorScheme()
    if scheme == Qt.ColorScheme.Dark:
        return True
    if scheme == Qt.ColorScheme.Light:
        return False
    return None


def clamp_rect(x: int, y: int, w: int, h: int, ax: int, ay: int, aw: int, ah: int) -> tuple[int, int, int, int]:
    """Fit a window rectangle inside the available screen rectangle."""

    width = max(1, min(int(w), int(aw)))
    height = max(1, min(int(h), int(ah)))
    left = min(max(int(x), int(ax)), int(ax) + int(aw) - width)
    top = min(max(int(y), int(ay)), int(ay) + int(ah) - height)
    return (left, top, width, height)


def clamp_widget_to_screen(widget: QWidget) -> None:
    screen = widget.screen()
    if screen is None:
        app = QApplication.instance()
        screen = app.primaryScreen() if app is not None else None
    if screen is None:
        return
    avail = screen.availableGeometry()
    _fit_dialog_to_box(widget, avail)
    frame = widget.frameGeometry()
    left, top, width, height = clamp_rect(
        frame.x(),
        frame.y(),
        frame.width(),
        frame.height(),
        avail.x(),
        avail.y(),
        avail.width(),
        avail.height(),
    )
    if width != widget.width() or height != widget.height():
        widget.resize(max(1, width - (frame.width() - widget.width())), max(1, height - (frame.height() - widget.height())))
        frame = widget.frameGeometry()
        left, top, width, height = clamp_rect(
            frame.x(),
            frame.y(),
            frame.width(),
            frame.height(),
            avail.x(),
            avail.y(),
            avail.width(),
            avail.height(),
        )
    if frame.x() != left or frame.y() != top:
        widget.move(left, top)


def fit_widget_to_box(widget: QWidget, x: int, y: int, width: int, height: int) -> None:
    """Lower the minimum and scroll the body so the widget fits this rectangle."""

    _fit_dialog_to_box(widget, QRect(int(x), int(y), int(width), int(height)))


class _BodyScroll(QScrollArea):
    """Dialog body scroller. Its own minimum stays small so the dialog can shrink."""

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(320, 200)

    def sizeHint(self) -> QSize:  # noqa: N802
        inner = self.widget()
        if inner is None:
            return QSize(640, 480)
        hint = inner.sizeHint()
        return QSize(min(max(hint.width() + 24, 360), 960), min(max(hint.height() + 24, 240), 640))


def _fit_dialog_to_box(widget: QWidget, avail: QRect) -> None:
    if not isinstance(widget, QDialog):
        return
    max_w = max(320, int(avail.width()) - 16)
    max_h = max(200, int(avail.height()) - 16)
    # A large sizeHint is not a reason to wrap. Bug-report dialogs size
    # themselves, then shrink; wrapping them pins the height at the scroller cap.
    too_big = (
        widget.minimumWidth() > max_w
        or widget.minimumHeight() > max_h
        or widget.width() > max_w
        or widget.height() > max_h
    )
    if too_big:
        _ensure_dialog_scroll(widget)
    widget.setMinimumSize(min(widget.minimumWidth(), max_w), min(widget.minimumHeight(), max_h))


def _ensure_dialog_scroll(dialog: QDialog) -> None:
    if dialog.property("_pc_scrolled"):
        return
    layout = dialog.layout()
    if layout is None:
        return
    from PySide6.QtWidgets import QVBoxLayout

    content = QWidget()
    inner = QVBoxLayout(content)
    margins = layout.contentsMargins()
    inner.setContentsMargins(margins)
    inner.setSpacing(layout.spacing())
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        child = item.layout()
        spacer = item.spacerItem()
        if widget is not None:
            inner.addWidget(widget)
        elif child is not None:
            inner.addLayout(child)
        elif spacer is not None:
            inner.addSpacerItem(spacer)
    scroll = _BodyScroll()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    scroll.setWidget(content)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(scroll)
    dialog.setProperty("_pc_scrolled", True)
    dialog.setMinimumSize(320, 200)


class _ThemeHooks(QObject):
    """Dialog clamp, native captions, and settings.json from another process."""

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._busy: set[int] = set()
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_settings_file)
        self._watcher.directoryChanged.connect(self._on_settings_file)
        self.arm()

    def arm(self) -> None:
        from panelcad.settings import observe_file
        from panelcad.theme import settings_path

        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        directory = str(path.parent)
        if directory not in self._watcher.directories():
            self._watcher.addPath(directory)
        target = str(path)
        if path.is_file() and target not in self._watcher.files():
            self._watcher.addPath(target)
        observe_file()

    def _on_settings_file(self, _path: str) -> None:
        from panelcad.settings import consume_external_change, external_appearance_changed

        if consume_external_change() and external_appearance_changed():
            from panelcad.theme import refresh_appearance

            refresh_appearance()
        QTimer.singleShot(0, self.arm)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if not isinstance(watched, QWidget):
            return False
        kind = event.type()
        if kind == QEvent.Type.Show and watched.isWindow():
            apply_titlebar(watched)
        if isinstance(watched, QDialog) and kind in {QEvent.Type.Show, QEvent.Type.Resize}:
            if watched.property("_pc_own_fit"):
                return False
            key = id(watched)
            if key not in self._busy:
                self._busy.add(key)
                try:
                    clamp_widget_to_screen(watched)
                finally:
                    self._busy.discard(key)
        return False


class SvgIconEngine(QIconEngine):
    """Lucide SVG recolored with the current text token. Crisp at any device pixel ratio."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    def clone(self) -> QIconEngine:
        return SvgIconEngine(self._name)

    def paint(self, painter: QPainter, rect, mode, state) -> None:  # noqa: ANN001
        del state
        color = ui_hex("muted" if mode == QIcon.Mode.Disabled else "text")
        renderer = _renderer(self._name, color)
        if renderer is None or not renderer.isValid():
            return
        painter.save()
        renderer.render(painter, QRectF(rect))
        painter.restore()

    def pixmap(self, size, mode, state):  # noqa: ANN001
        pm = QPixmap(size)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        self.paint(painter, pm.rect(), mode, state)
        painter.end()
        return pm


def themed_icon(name: str) -> QIcon:
    return QIcon(SvgIconEngine(name))


def render_svg_icon(name: str, px: int, dpr: float = 1.0) -> QPixmap:
    """Rasterize one toolbar icon at ``px`` logical pixels and ``dpr`` (1.25, 1.5, …)."""

    ratio = dpr if dpr and dpr > 0 else 1.0
    pm = QPixmap(max(1, int(round(px * ratio))), max(1, int(round(px * ratio))))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.GlobalColor.transparent)
    renderer = _renderer(name, ui_hex("text"))
    if renderer is None or not renderer.isValid():
        return pm
    painter = QPainter(pm)
    renderer.render(painter, QRectF(0, 0, px, px))
    painter.end()
    return pm


def icon_names() -> list[str]:
    if not _ICONS.is_dir():
        return []
    return sorted(path.stem for path in _ICONS.glob("*.svg"))


def _svg_bytes(name: str, color: str) -> bytes | None:
    path = _ICONS / f"{name}.svg"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    return text.replace("currentColor", color).encode("utf-8")


def _icon_cache_dir() -> Path:
    """Outside the settings folder, so icon writes do not wake the settings watcher."""

    import os
    import tempfile

    override = os.environ.get("PANELCAD_CACHE", "").strip()
    root = Path(override) if override else Path(tempfile.gettempdir()) / "panelcad-cache"
    return root / "icon-cache"


def _cached_svg(name: str, color: str) -> str:
    data = _svg_bytes(name, color)
    if data is None:
        return ""
    folder = _icon_cache_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}-{color.lstrip('#').lower()}.svg"
    if not path.is_file() or path.read_bytes() != data:
        path.write_bytes(data)
    return path.resolve().as_posix()


def _renderer(name: str, color: str):
    data = _svg_bytes(name, color)
    if data is None:
        return None
    from PySide6.QtSvg import QSvgRenderer

    return QSvgRenderer(data)


def _on_settings_changed() -> None:
    apply_stylesheet()
    app = QApplication.instance()
    if app is not None:
        app.setFont(ui_font())


def _on_system_scheme() -> None:
    global _refreshing
    if _scheme_busy or _refreshing:
        return
    if get_theme() != "system":
        return
    from panelcad.theme import refresh_appearance

    _refreshing = True
    try:
        refresh_appearance()
    finally:
        _refreshing = False


def appearance() -> dict[str, str]:
    return {
        "theme": get_theme(),
        "canvas": get_canvas(),
        "resolved_theme": resolved_theme_name(),
    }
