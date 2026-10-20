"""The splash must be gone before any startup dialog is on screen.

Startup dialogs:

* QMessageBox "Open failed" — missing or damaged project on the command line
* RecoveryDialog — unclean exit / autosave copy
* WelcomeDialog — first run
* UpdateNoticeDialog — relaunch after update, rollback, or failure
* UpdateAvailableDialog — newer release on the feed
* CrashDialog — exception after startup diagnostics are installed

There is no AI-key dialog at launch. The key is Settings → AI.
Project-locked and sync-conflict boxes are File → Open, not launch.
A QMessageBox or any other QDialog shown while the splash is up is covered
by the same guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow, QMessageBox


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _show_card():
    from panelcad.gui import splash as splash_mod

    splash_mod.reset_splash_state()
    card = splash_mod.show_splash(force=True, message="Loading parts library...")
    assert card is not None
    assert card.isVisible()
    assert card.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    return splash_mod, card


def _topmost_and_visible(card) -> bool:
    try:
        return bool(card.isVisible() and (card.windowFlags() & Qt.WindowType.WindowStaysOnTopHint))
    except RuntimeError:
        return False


def _while_dialog(dialog, splash_mod, card) -> dict:
    """Run ``dialog.exec`` and record the splash while that dialog is visible."""

    app = _app()
    seen: dict = {}

    def check() -> None:
        seen["dialog"] = dialog.isVisible()
        seen["splash"] = splash_mod.splash_is_visible()
        seen["topmost"] = _topmost_and_visible(card)
        seen["parent"] = dialog.parentWidget()
        seen["title"] = dialog.windowTitle()
        screen = dialog.screen() or app.primaryScreen()
        if screen is not None and dialog.isVisible():
            center = dialog.frameGeometry().center()
            seen["on_screen"] = screen.availableGeometry().contains(center)
        else:
            seen["on_screen"] = False
        dialog.reject()

    QTimer.singleShot(30, check)
    dialog.exec()
    app.processEvents()
    return seen


def _assert_splash_yielded(seen: dict) -> None:
    assert seen["dialog"] is True
    assert seen["splash"] is False
    assert seen["topmost"] is False
    assert seen["on_screen"] is True


def _release(app: QApplication, *widgets) -> None:
    """Close windows and stop their timers so a later splash is left alone."""

    from PySide6.QtCore import QTimer

    from panelcad.gui import splash as splash_mod
    from panelcad.gui.main_window import MainWindow

    closing = list(widgets)
    for widget in app.topLevelWidgets():
        if isinstance(widget, MainWindow) and widget not in closing:
            closing.append(widget)
    for widget in closing:
        if widget is None:
            continue
        try:
            widget._closing = True
            widget._defer_startup_updates = False
        except Exception:  # noqa: BLE001 — a plain dialog has neither flag
            pass
        try:
            for timer in widget.findChildren(QTimer):
                timer.stop()
        except RuntimeError:
            pass
        try:
            widget.close()
            widget.deleteLater()
        except RuntimeError:
            pass
    splash_mod.reset_splash_state()
    app.processEvents()


def test_any_dialog_and_message_box_hide_the_splash(monkeypatch: pytest.MonkeyPatch):
    """Stay-on-top cannot cover a modal. The guard is not limited to one caller."""

    monkeypatch.setenv("PANELCAD_FORCE_SPLASH", "1")
    app = _app()
    host = QMainWindow()
    dialog = None
    box = None
    host.resize(960, 640)
    host.show()
    app.processEvents()
    try:
        splash_mod, card = _show_card()
        dialog = QDialog(host)
        dialog.setWindowTitle("PanelCAD")
        dialog.resize(420, 180)
        seen = _while_dialog(dialog, splash_mod, card)
        _assert_splash_yielded(seen)
        assert seen["parent"] is host

        splash_mod, card = _show_card()
        box = QMessageBox(host)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("PanelCAD")
        box.setText("Something went wrong while opening the project.")
        seen = _while_dialog(box, splash_mod, card)
        _assert_splash_yielded(seen)
        assert seen["parent"] is host
    finally:
        _release(app, host, dialog, box)


def test_parentless_message_box_is_parented_centered_and_raised(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("PANELCAD_FORCE_SPLASH", "1")
    app = _app()
    host = QMainWindow()
    box = None
    host.resize(960, 640)
    host.show()
    app.processEvents()
    try:
        splash_mod, card = _show_card()
        box = QMessageBox()
        box.setWindowTitle("Open failed")
        box.setText("Can't find job.pcad.yaml.")
        assert box.parentWidget() is None
        seen = _while_dialog(box, splash_mod, card)
        _assert_splash_yielded(seen)
        assert seen["parent"] is host
        assert box.isWindow()
        assert not (box.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
    finally:
        _release(app, host, box)


def test_startup_dialogs_hide_the_splash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Each dialog the launch path can open hides the splash before it is shown."""

    monkeypatch.setenv("PANELCAD_FORCE_SPLASH", "1")
    monkeypatch.setenv("PANELCAD_NO_WELCOME", "1")
    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path))
    monkeypatch.setenv("PANELCAD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PANELCAD_CONFIG_DIR", str(tmp_path / "updates"))
    monkeypatch.setattr(
        "panelcad.update.settings.startup_check_due",
        lambda *args, **kwargs: False,
    )
    from panelcad.api.session import Session
    from panelcad.diag.crash import UncleanExit, _present_crash
    from panelcad.gui import splash as splash_mod
    from panelcad.gui.bug_report_dialog import offer_unclean_exit
    from panelcad.gui.main_window import MainWindow
    from panelcad.guidance import set_welcome_status
    from panelcad.settings import invalidate

    app = _app()
    invalidate()
    splash_mod.reset_splash_state()
    window = None
    try:
        splash_mod.show_splash(force=True, message="Loading parts library...")
        window = MainWindow(Session.create("Startup dialogs"))
        window.resize(1100, 720)
        window.show()
        app.processEvents()
        assert window._defer_startup_updates is True

        def run(title_kind: str, show) -> None:
            splash_mod.reset_splash_state()
            card = splash_mod.show_splash(force=True, message="Loading parts library...")
            assert card is not None and card.isVisible()
            seen: dict = {}

            def check() -> None:
                match = None
                for widget in app.topLevelWidgets():
                    try:
                        visible = widget.isVisible()
                    except RuntimeError:
                        continue
                    if visible and widget.__class__.__name__ == title_kind:
                        match = widget
                        break
                if match is None:
                    seen["dialog"] = False
                    for widget in app.topLevelWidgets():
                        if isinstance(widget, QDialog):
                            try:
                                widget.reject()
                            except RuntimeError:
                                pass
                    return
                seen["dialog"] = True
                seen["splash"] = splash_mod.splash_is_visible()
                seen["topmost"] = _topmost_and_visible(card)
                seen["parent"] = match.parentWidget()
                screen = match.screen() or app.primaryScreen()
                center = match.frameGeometry().center()
                seen["on_screen"] = bool(screen and screen.availableGeometry().contains(center))
                match.reject()

            QTimer.singleShot(30, check)
            show()
            app.processEvents()
            _assert_splash_yielded(seen)
            assert seen["parent"] is window

        run(
            "UpdateNoticeDialog",
            lambda: window._show_update_notice("0.5.9", kind="update"),
        )
        run(
            "UpdateAvailableDialog",
            lambda: window._present_update_report(
                {
                    "enabled": True,
                    "update_available": True,
                    "skipped": False,
                    "version": "0.9.0",
                    "current": "0.5.9",
                    "installable": False,
                    "notes": "Shop PC",
                },
                automatic=True,
            ),
        )
        run(
            "RecoveryDialog",
            lambda: offer_unclean_exit(window, UncleanExit(reasons=["startup"])),
        )
        set_welcome_status("pending")
        run("WelcomeDialog", window.show_welcome)
        run(
            "CrashDialog",
            lambda: _present_crash(
                {
                    "exception": "startup failed",
                    "traceback": "RuntimeError: startup failed\n",
                    "path": "",
                }
            ),
        )
        from panelcad.gui.app import _show_open_error

        run(
            "QMessageBox",
            lambda: _show_open_error(window, (tmp_path / "missing.pcad.yaml", FileNotFoundError())),
        )
    finally:
        _release(app, window)
        invalidate()


def test_update_notice_waits_until_the_window_is_on_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The 400 ms update timer must not map a dialog during the off-screen paint."""

    monkeypatch.setenv("PANELCAD_FORCE_SPLASH", "1")
    monkeypatch.setenv("PANELCAD_CONFIG_DIR", str(tmp_path / "updates"))
    monkeypatch.setattr(
        "panelcad.update.settings.startup_check_due",
        lambda *args, **kwargs: False,
    )
    from panelcad.api.session import Session
    from panelcad.gui import splash as splash_mod
    from panelcad.gui.main_window import MainWindow
    from panelcad.update.settings import write_notice

    app = _app()
    write_notice("0.5.9", tmp_path / "updates", kind="update")
    splash_mod.reset_splash_state()
    card = splash_mod.show_splash(force=True)
    assert card is not None and card.isVisible()
    window = MainWindow(Session.create("Notice"))
    stop = False
    try:
        window.resize(1000, 700)
        window.show()
        app.processEvents()
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window._defer_startup_updates = False
        window._startup_updates()
        app.processEvents()
        assert splash_mod.splash_is_visible()
        assert _topmost_and_visible(card)
        assert not any(
            widget.__class__.__name__ == "UpdateNoticeDialog" and widget.isVisible()
            for widget in app.topLevelWidgets()
        )

        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, False)
        seen: dict = {}
        loop = QEventLoop()

        def dismiss() -> None:
            if stop:
                return
            for widget in app.topLevelWidgets():
                if widget.__class__.__name__ != "UpdateNoticeDialog" or not widget.isVisible():
                    continue
                seen["splash"] = splash_mod.splash_is_visible()
                seen["topmost"] = _topmost_and_visible(card)
                seen["parent"] = widget.parentWidget()
                widget.accept()
                loop.quit()
                return
            QTimer.singleShot(20, dismiss)

        QTimer.singleShot(20, dismiss)
        QTimer.singleShot(800, loop.quit)
        loop.exec()
        assert seen.get("splash") is False
        assert seen.get("topmost") is False
        assert seen.get("parent") is window
    finally:
        stop = True
        _release(app, window)


def test_missing_project_dialog_hides_the_splash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PANELCAD_FORCE_SPLASH", "1")
    monkeypatch.setenv("PANELCAD_NO_WELCOME", "1")
    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path))
    monkeypatch.setenv("PANELCAD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PANELCAD_CONFIG_DIR", str(tmp_path / "updates"))
    from panelcad.gui import splash as splash_mod
    from panelcad.gui.app import launch_gui
    from panelcad.gui.main_window import MainWindow
    from panelcad.settings import invalidate

    app = _app()
    invalidate()
    splash_mod.reset_splash_state()
    missing = tmp_path / "gone.pcad.yaml"
    seen: dict = {}

    def grab() -> None:
        for widget in app.topLevelWidgets():
            if widget.__class__.__name__ != "QMessageBox" or not widget.isVisible():
                continue
            seen["text"] = widget.text()
            seen["splash"] = splash_mod.splash_is_visible()
            seen["parent_type"] = type(widget.parentWidget()).__name__
            widget.accept()
            return

    try:
        gui = launch_gui(missing)
        assert gui is app or gui is QApplication.instance()
        QTimer.singleShot(30, grab)
        app.processEvents()
        assert seen["text"] == "Can't find gone.pcad.yaml."
        assert seen["splash"] is False
        assert seen["parent_type"] == "MainWindow"
        windows = [
            widget
            for widget in app.topLevelWidgets()
            if isinstance(widget, MainWindow) and widget.isVisible()
        ]
        assert len(windows) == 1
    finally:
        _release(app)
        invalidate()


def test_closed_window_does_not_close_a_later_splash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A finished MainWindow must not drop stay-on-top on the next splash."""

    monkeypatch.setenv("PANELCAD_FORCE_SPLASH", "1")
    monkeypatch.setenv("PANELCAD_NO_WELCOME", "1")
    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path))
    monkeypatch.setenv("PANELCAD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PANELCAD_CONFIG_DIR", str(tmp_path / "updates"))
    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow, _STARTUP_UPDATE_POLLS
    from panelcad.settings import invalidate

    app = _app()
    invalidate()
    window = MainWindow(Session.create("Closed"))
    try:
        window.resize(800, 600)
        window.show()
        app.processEvents()
        window._closing = True
        window.close()
        app.processEvents()
        splash_mod, card = _show_card()
        window._startup_updates()
        app.processEvents()
        assert card.isVisible()
        assert splash_mod.splash_is_visible()
        assert _topmost_and_visible(card)

        splash_mod.reset_splash_state()
        later = MainWindow(Session.create("Still open"))
        later.resize(800, 600)
        later.show()
        app.processEvents()
        splash_mod, card = _show_card()
        later._defer_startup_updates = True
        later._startup_update_waits = _STARTUP_UPDATE_POLLS
        scheduled: list[int] = []
        real = QTimer.singleShot

        def record(*args, **kwargs):
            scheduled.append(1)
            return real(*args, **kwargs)

        monkeypatch.setattr(QTimer, "singleShot", record)
        later._startup_updates()
        assert scheduled == []
        assert card.isVisible()
        assert splash_mod.splash_is_visible()
    finally:
        _release(app, window)
        invalidate()
