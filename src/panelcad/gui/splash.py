"""Startup splash, application icon, and the first paint of the main window.

Headless CLI commands and offscreen tests do not show the splash. Set
``PANELCAD_FORCE_SPLASH=1`` to show it anyway (timing and tests). Set
``PANELCAD_NO_SPLASH=1`` to keep it off. ``PANELCAD_KEEP_OFFSCREEN=1`` or a
running pytest session leaves ``QT_QPA_PLATFORM=offscreen`` in place.

The splash may use stay-on-top while the parts library loads. It drops that
flag and closes before any modal dialog is mapped, so an update prompt, the
recovery dialog, the first-run welcome, a crash dialog, or a message box
cannot open underneath it.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_ASSETS = Path(__file__).resolve().parents[1] / "assets"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
_TRUE = {"1", "true", "yes", "on"}

_splash = None
_icon = None
_messages: list[str] = []
_shown = False
_message_color = None
_dialog_guard = None
_dialog_cls = None
# Qt subclasses are built once per process and never freed. A class statement
# inside a function made a new Shiboken type, with its own dynamic QMetaObject,
# on every call. Python types always sit in reference cycles, so each stale
# type was freed later by a cyclic gc.collect(), and on Windows that free hit
# heap corruption (0xC0000374) right after the splash tests.
_splash_cls = None
_dialog_guard_cls = None

_LIGHT = {
    "bg": "#F3F6FA",
    "band": "#E4EDF6",
    "title": "#143A66",
    "muted": "#4E6074",
}
_DARK = {
    "bg": "#1B1E24",
    "band": "#12151A",
    "title": "#F3F1EA",
    "muted": "#C8C4B8",
}


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def keep_offscreen() -> bool:
    """True when a GUI launch must not clear a requested offscreen platform."""

    if _flag("PANELCAD_KEEP_OFFSCREEN"):
        return True
    return "pytest" in sys.modules


def normalize_gui_platform() -> None:
    """Drop a leftover offscreen platform so an interactive window can open."""

    if os.environ.get("QT_QPA_PLATFORM") == "offscreen" and not keep_offscreen():
        os.environ.pop("QT_QPA_PLATFORM", None)


def splash_enabled() -> bool:
    """False for CLI tests, offscreen Qt, and a Linux session with no display."""

    if _flag("PANELCAD_NO_SPLASH"):
        return False
    if _flag("PANELCAD_FORCE_SPLASH"):
        return True
    if os.environ.get("QT_QPA_PLATFORM", "").strip().lower() == "offscreen":
        return False
    if "pytest" in sys.modules:
        return False
    if sys.platform.startswith("linux"):
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            return False
    return True


def command_is_gui(argv: list[str] | None = None) -> bool:
    """True when this process was started as ``panelcad gui``."""

    args = list(sys.argv if argv is None else argv)[1:]
    if any(arg in {"-h", "--help"} for arg in args):
        return False
    positional = [arg for arg in args if not arg.startswith("-")]
    return bool(positional) and positional[0] == "gui"


def should_show_splash(argv: list[str] | None = None) -> bool:
    return command_is_gui(argv) and splash_enabled()


def boot_only() -> bool:
    return _flag("PANELCAD_BOOT_ONLY")


def messages() -> list[str]:
    return list(_messages)


def splash_was_shown() -> bool:
    return _shown


def splash_is_visible() -> bool:
    splash = _splash
    if splash is None:
        return False
    try:
        return bool(splash.isVisible())
    except RuntimeError:
        return False


def reset_splash_state() -> None:
    """Close the splash and forget messages. Tests call this between cases."""

    global _shown, _messages
    close_splash()
    _shown = False
    _messages = []


def mark_startup(name: str) -> None:
    """Print ``PANELCAD_TIMING <name> <seconds>`` when timing is enabled."""

    if not _flag("PANELCAD_STARTUP_TIMING"):
        return
    raw = os.environ.get("PANELCAD_TIMING_ZERO", "").strip()
    if not raw:
        return
    try:
        zero = float(raw)
    except ValueError:
        return
    sys.stderr.write(f"PANELCAD_TIMING {name} {time.perf_counter() - zero:.4f}\n")
    sys.stderr.flush()


def _windows_app_id() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PanelCAD.Application")
    except Exception:  # noqa: BLE001 — the window icon still applies
        return


def application_icon():
    """Multi-size PanelCAD icon. Empty if the asset files are missing."""

    global _icon
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QIcon

    if _icon is not None:
        return _icon
    icon = QIcon()
    ico = _ASSETS / "panelcad.ico"
    if ico.is_file():
        icon.addFile(str(ico))
    for size in ICON_SIZES:
        png = _ASSETS / f"panelcad-{size}.png"
        if png.is_file():
            icon.addFile(str(png), QSize(size, size))
    _icon = icon
    return icon


def ensure_application():
    """Create ``QApplication`` once, with the PanelCAD icon and name."""

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    _windows_app_id()
    if QApplication.instance() is None:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
        app = QApplication(sys.argv)
    else:
        app = QApplication.instance()
    app.setApplicationName("PanelCAD")
    from panelcad.gui import gc_control

    gc_control.install(app)
    icon = application_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
    return app


def _theme_is_dark(app) -> bool:
    theme = "light"
    try:
        from panelcad.settings import peek

        theme = str(peek().get("theme") or "light").strip().lower()
    except Exception:  # noqa: BLE001 — splash still paints with the light card
        theme = "light"
    if theme == "dark":
        return True
    if theme == "light":
        return False
    from PySide6.QtCore import Qt

    try:
        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except Exception:  # noqa: BLE001
        return False


def _splash_pixmap(app, icon):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont, QPainter, QPixmap

    from panelcad.version import __version__

    colors = _DARK if _theme_is_dark(app) else _LIGHT
    width, height = 480, 300
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor(colors["bg"]))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.fillRect(0, 244, width, height - 244, QColor(colors["band"]))
    mark = icon.pixmap(96, 96)
    if not mark.isNull():
        painter.drawPixmap((width - mark.width()) // 2, 28, mark)
    title_font = QFont()
    title_font.setFamilies(["Segoe UI", "sans-serif"])
    title_font.setPointSize(22)
    title_font.setWeight(QFont.Weight.DemiBold)
    painter.setFont(title_font)
    painter.setPen(QColor(colors["title"]))
    painter.drawText(
        24,
        140,
        width - 48,
        40,
        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
        "PanelCAD",
    )
    version_font = QFont()
    version_font.setFamilies(["Segoe UI", "sans-serif"])
    version_font.setPointSize(11)
    painter.setFont(version_font)
    painter.setPen(QColor(colors["muted"]))
    painter.drawText(
        24,
        182,
        width - 48,
        28,
        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
        __version__,
    )
    painter.end()
    return pixmap, QColor(colors["muted"])


def _splash_screen_type():
    """The QSplashScreen subclass. Built on first use, then reused."""

    global _splash_cls
    if _splash_cls is not None:
        return _splash_cls
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QSplashScreen, QWidget

    class _SplashScreen(QSplashScreen):
        """Skip Qt's one-second map wait on the offscreen plugin.

        ``QSplashScreen`` waits on Show until the native window reports itself
        visible, up to one second. A real display returns as soon as the window
        is mapped. Offscreen never reports that. ``finish()`` is unchanged.
        """

        def event(self, event):  # noqa: N802
            offscreen = os.environ.get("QT_QPA_PLATFORM", "").strip().lower() == "offscreen"
            if event.type() == QEvent.Type.Show and offscreen:
                return QWidget.event(self, event)
            return super().event(event)

    _splash_cls = _SplashScreen
    return _SplashScreen


def _show_new_splash(app, message: str):
    global _splash, _shown, _message_color
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont

    _SplashScreen = _splash_screen_type()
    icon = application_icon()
    pixmap, color = _splash_pixmap(app, icon)
    # Stay above other programs while the library loads. A startup dialog
    # clears this and closes the splash before it maps (see _DialogGuard).
    splash = _SplashScreen(pixmap, Qt.WindowType.WindowStaysOnTopHint)
    splash.setWindowTitle("PanelCAD")
    if not icon.isNull():
        splash.setWindowIcon(icon)
    font = QFont()
    font.setFamilies(["Segoe UI", "sans-serif"])
    font.setPointSize(11)
    splash.setFont(font)
    _splash = splash
    _message_color = color
    _shown = True
    _install_dialog_guard(app)
    splash.show()
    splash_message(message)
    app.processEvents()
    return splash


def show_splash(*, force: bool = False, message: str = "Loading parts library..."):
    """Show the splash, or return the one already on screen."""

    if _splash is not None:
        splash_message(message)
        return _splash
    if not force and not splash_enabled():
        return None
    app = ensure_application()
    return _show_new_splash(app, message)


def prepare_early_splash(message: str = "Loading parts library...") -> bool:
    """Paint the splash before heavy imports. No-op when the splash is disabled."""

    if _splash is not None:
        return True
    if not splash_enabled():
        return False
    show_splash(message=message)
    mark_startup("splash")
    return _splash is not None


def splash_message(text: str) -> None:
    """Update the status line and let Qt paint it before the next blocking step."""

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QApplication

    _messages.append(text)
    splash = _splash
    if splash is None:
        return
    color = _message_color or QColor("#143A66")
    splash.showMessage(
        text,
        Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
        color,
    )
    app = QApplication.instance()
    if app is not None:
        app.processEvents()


def _drop_stays_on_top(splash) -> None:
    """Clear stay-on-top. Qt hides a visible window when its flags change."""

    from PySide6.QtCore import Qt

    try:
        if splash.windowFlags() & Qt.WindowType.WindowStaysOnTopHint:
            splash.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
    except RuntimeError:
        return


def _dispose_splash(splash) -> None:
    """Let Qt delete a closed splash card on the next event-loop turn.

    The card is parentless and owned by its Python wrapper, so without this
    its C++ object was destroyed whenever Python dropped the last reference:
    from a cyclic ``gc.collect()`` if a closure or traceback held it. A closed
    card is never shown again (``show_splash`` makes a new one), so deleting
    it here only makes that destruction happen at a known point.
    """

    try:
        splash.deleteLater()
    except RuntimeError:  # the C++ object is already gone
        return


def finish_splash(window) -> None:
    """Close the splash once ``window`` is on screen. ``QSplashScreen.finish``."""

    global _splash
    splash = _splash
    if splash is None:
        return
    from PySide6.QtWidgets import QApplication

    _drop_stays_on_top(splash)
    try:
        splash.finish(window)
    except Exception as exc:  # noqa: BLE001 — still close the card on the way out
        del exc
    try:
        if splash.isVisible():
            splash.close()
    except RuntimeError:
        pass
    _dispose_splash(splash)
    app = QApplication.instance()
    if app is not None:
        app.processEvents()
    if _splash is splash:
        _splash = None


def close_splash(*, flush: bool = True) -> None:
    """Hide the splash and drop stay-on-top.

    Startup errors call this so the card cannot stay up. ``flush`` is false
    from the dialog guard: processing events there would nest another dialog
    under the one that is already opening.
    """

    global _splash
    splash = _splash
    _splash = None
    if splash is None:
        return
    from PySide6.QtWidgets import QApplication

    _drop_stays_on_top(splash)
    try:
        if splash.isVisible():
            splash.hide()
        splash.close()
    except RuntimeError:
        return
    _dispose_splash(splash)
    if not flush:
        return
    app = QApplication.instance()
    if app is not None:
        try:
            app.processEvents()
        except Exception:  # noqa: BLE001
            return


def _is_dialog(widget) -> bool:
    global _dialog_cls
    if _dialog_cls is None:
        from PySide6.QtWidgets import QDialog

        _dialog_cls = QDialog
    return isinstance(widget, _dialog_cls)


def _startup_main_window(dialog):
    """The main window a parentless startup dialog should use."""

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMainWindow

    app = QApplication.instance()
    if app is None:
        return None
    fallback = None
    for widget in app.topLevelWidgets():
        if widget is dialog or not isinstance(widget, QMainWindow):
            continue
        if fallback is None:
            fallback = widget
        try:
            on_screen = widget.isVisible() and not widget.testAttribute(
                Qt.WidgetAttribute.WA_DontShowOnScreen
            )
        except RuntimeError:
            continue
        if on_screen:
            return widget
    return fallback


def _center_dialog(dialog) -> None:
    parent = dialog.parentWidget()
    if parent is not None:
        try:
            dialog.adjustSize()
            frame = dialog.frameGeometry()
            frame.moveCenter(parent.window().frameGeometry().center())
            dialog.move(frame.topLeft())
        except RuntimeError:
            return
    try:
        from panelcad.gui.screen_fit import fit_dialog_to_work_area

        fit_dialog_to_work_area(dialog)
    except Exception:  # noqa: BLE001 — the dialog still has to open
        return


def _adopt_startup_dialog(dialog) -> None:
    """Give a parentless dialog the main window before it is shown.

    ``setParent`` on a widget that is already visible hides it. Call this from
    ``Polish``, while ``isVisible()`` is still false, so ``exec`` is not cancelled.
    """

    try:
        if dialog.parentWidget() is not None or dialog.isVisible():
            return
        parent = _startup_main_window(dialog)
        if parent is None:
            return
        dialog.setParent(parent, dialog.windowFlags())
    except RuntimeError:
        return


def _place_startup_dialog(dialog) -> None:
    """Center the dialog and raise it. The splash is already closed."""

    from PySide6.QtCore import QTimer

    try:
        _center_dialog(dialog)
        dialog.raise_()
        dialog.activateWindow()
    except RuntimeError:
        return

    def _again() -> None:
        try:
            if dialog.isVisible():
                dialog.raise_()
                dialog.activateWindow()
        except RuntimeError:
            return

    # Bound to the dialog: Qt drops the call if the dialog is deleted first,
    # and the closure is released with the dialog, not by the cyclic GC.
    QTimer.singleShot(0, dialog, _again)


def _dialog_guard_type():
    """QObject subclass. Built on first use so importing this module stays light.

    Cached like the splash class, so a second QApplication does not make a new type.
    """

    global _dialog_guard_cls
    if _dialog_guard_cls is not None:
        return _dialog_guard_cls
    from PySide6.QtCore import QEvent, QObject

    class _DialogGuard(QObject):
        """Close the splash before a modal dialog is mapped.

        Installed on ``QApplication``, so it sees every ``Show``.
        ``QMessageBox``, ``QFileDialog``, and ``QInputDialog`` are dialogs,
        which covers a startup path that does not call ``close_splash`` itself.
        """

        def __init__(self, parent) -> None:
            super().__init__(parent)
            self._depth = 0

        def eventFilter(self, watched, event) -> bool:  # noqa: N802
            if _splash is None or self._depth:
                return False
            if not _is_dialog(watched):
                return False
            kind = event.type()
            if kind == QEvent.Type.Polish:
                self._depth += 1
                try:
                    _adopt_startup_dialog(watched)
                finally:
                    self._depth -= 1
                return False
            if kind != QEvent.Type.Show:
                return False
            self._depth += 1
            try:
                close_splash(flush=False)
                _place_startup_dialog(watched)
            finally:
                self._depth -= 1
            return False

    _dialog_guard_cls = _DialogGuard
    return _DialogGuard


def _install_dialog_guard(app) -> None:
    global _dialog_guard
    if app is None:
        return
    if _dialog_guard is not None:
        try:
            if _dialog_guard.parent() is app:
                return
        except RuntimeError:
            _dialog_guard = None
    guard = _dialog_guard_type()(app)
    app.installEventFilter(guard)
    _dialog_guard = guard


def reveal_main_window(window, app=None) -> None:
    """Show ``window`` after its first paint so the frame is not black.

    The off-screen pass fills the backing store with the theme window color
    (light or dark). The widget is mapped only after that paint.
    """

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QApplication

    from panelcad.theme import ui_hex

    target = app or QApplication.instance()
    color = QColor(ui_hex("window"))
    window.setAutoFillBackground(True)
    palette = window.palette()
    palette.setColor(QPalette.ColorRole.Window, color)
    palette.setColor(QPalette.ColorRole.Base, color)
    window.setPalette(palette)

    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    if target is not None:
        target.processEvents()
    window.repaint()
    window.grab()
    if target is not None:
        target.processEvents()
    window.hide()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, False)
    window.show()
    if target is not None:
        target.processEvents()
