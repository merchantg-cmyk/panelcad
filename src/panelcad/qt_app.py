"""Headless-safe Qt application bootstrap shared by render, electrical, and api layers.

Keeping this helper at the package root breaks the reverse dependency from
``panelcad.render`` up to ``panelcad.api`` and avoids duplicating the
offscreen ``QApplication`` startup across exporters.
"""

from __future__ import annotations

import os

_filter_installed = False


def install_headless_qt_filter() -> None:
    """Drop the PySide font-directory warning once per process.

    Install once: wrapping the handler repeatedly overflows the stack when a
    message handler paints and emits another message.
    """

    global _filter_installed
    if _filter_installed:
        return
    from PySide6.QtCore import qInstallMessageHandler

    previous = qInstallMessageHandler(None)
    depth = 0

    def _quiet(mode, context, message) -> None:  # noqa: ANN001
        nonlocal depth
        text = str(message or "")
        # Only this warning. Dropping every QFontDatabase message during
        # QApplication startup left later Windows tests with a doubled font.
        if "Cannot find font directory" in text:
            return
        if previous is None or depth:
            return
        depth += 1
        try:
            previous(mode, context, message)
        finally:
            depth -= 1

    qInstallMessageHandler(_quiet)
    _filter_installed = True


def prepare_qt_fonts() -> None:
    """Drop the font-directory warning without replacing the system fonts."""

    try:
        install_headless_qt_filter()
    except Exception:  # noqa: BLE001 — a headless import still runs without Qt
        return


def ensure_qt_app() -> None:
    """Ensure a ``QApplication`` instance exists without disturbing a live GUI."""

    prepare_qt_fonts()
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        # Headless CLI/PDF only. Never force offscreen while a GUI is already running.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        install_headless_qt_filter()
        QApplication([])
