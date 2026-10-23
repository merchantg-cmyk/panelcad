from __future__ import annotations

import sys
from pathlib import Path


def _open_main_window(session, *, show_start: bool):
    from panelcad.gui.main_window import MainWindow

    return MainWindow(session, show_start=show_start)


def launch_gui(project: Path | str | None = None):
    """Build the main window. The splash, when enabled, is already up or shown here.

    Returns the ``QApplication``. Does not enter the event loop.
    """

    from PySide6.QtCore import QTimer

    from panelcad.gui.splash import (
        application_icon,
        close_splash,
        ensure_application,
        finish_splash,
        mark_startup,
        normalize_gui_platform,
        prepare_early_splash,
        reveal_main_window,
        splash_is_visible,
        splash_message,
    )

    normalize_gui_platform()
    try:
        prepare_early_splash("Loading parts library...")
        from panelcad.api.session import Session
        from panelcad.diag.crash import mark_clean_exit
        from panelcad.diag.runtime import install_process_diagnostics
        unclean = install_process_diagnostics(gui=True)
        app = ensure_application()
        icon = application_icon()
        if not icon.isNull():
            app.setWindowIcon(icon)
        # The window applies the stylesheet once, after its docks exist.
        splash_message("Opening project...")
        open_error = None
        session = None
        if project:
            try:
                session = Session.open(project)
            except Exception as exc:  # noqa: BLE001 — shown after the splash closes
                open_error = (project, exc)
        win = _open_main_window(session, show_start=session is None)
        # Hold the update check until the open / recovery / welcome dialogs
        # have closed. The 400 ms timer is armed inside the window constructor,
        # and the off-screen first paint calls processEvents.
        win._defer_startup_updates = splash_is_visible()
        if not icon.isNull():
            win.setWindowIcon(icon)
        reveal_main_window(win, app)
        finish_splash(win)
        mark_startup("main_window")

        def _prompts() -> None:
            try:
                present_startup_prompts(win, unclean, open_error)
            except BaseException:
                close_splash()
                raise

        # One timer, so the welcome cannot open inside the recovery dialog's loop.
        QTimer.singleShot(0, win, _prompts)
        app.aboutToQuit.connect(lambda: mark_clean_exit(force=True))
        return app
    except BaseException:
        close_splash()
        raise


def run_gui(project: Path | str | None = None) -> None:
    from panelcad.gui.splash import boot_only

    app = launch_gui(project)
    if boot_only():
        return
    sys.exit(app.exec())


def present_startup_prompts(window, unclean, open_error=None) -> None:
    """Show a failed open, then recovery, then the welcome.

    The splash is closed before the first dialog. Each dialog runs only after
    the previous one has closed. ``open_error`` is ``(path, exc)`` when the
    project passed on the command line could not be read.

    Dialogs that can appear during startup, all after the splash is gone:

    * ``QMessageBox`` "Open failed" — missing or damaged project file
    * ``RecoveryDialog`` — previous unclean exit and its autosave copy
    * ``WelcomeDialog`` — first run
    * ``UpdateNoticeDialog`` — relaunch after an update, rollback, or failure
    * ``UpdateAvailableDialog`` — a newer release on the feed
    * ``CrashDialog`` — an exception after the window starts
    """

    from panelcad.gui.splash import close_splash

    close_splash()
    try:
        if open_error is not None:
            _show_open_error(window, open_error)
        if unclean is not None:
            from panelcad.gui.bug_report_dialog import offer_unclean_exit

            try:
                offer_unclean_exit(window, unclean)
            finally:
                window.offer_welcome()
            return
        window.offer_welcome()
    finally:
        close_splash()
        if hasattr(window, "_defer_startup_updates"):
            window._defer_startup_updates = False
            _schedule_startup_updates(window)


def _schedule_startup_updates(window) -> None:
    """Run the startup update check once the open / recovery / welcome pass is over."""

    from PySide6.QtCore import QTimer

    from panelcad.gui.main_window import _qt_alive

    start = getattr(window, "_startup_updates", None)
    if start is None or not _qt_alive(window):
        return
    try:
        if not window.isVisible() or getattr(window, "_closing", False):
            return
    except RuntimeError:
        return
    QTimer.singleShot(0, window, start)


def _show_open_error(window, open_error) -> None:
    from PySide6.QtWidgets import QMessageBox

    from panelcad.io.yaml_format import describe_open_error

    path, exc = open_error
    QMessageBox.critical(window, "Open failed", describe_open_error(exc, path))
