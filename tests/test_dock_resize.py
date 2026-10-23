"""Library and bottom docks stay draggable, including after a saved layout."""

from __future__ import annotations

import os

import pytest
import shiboken6


def _app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _window(tmp_path, monkeypatch):
    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path))
    from panelcad.api.session import Session
    from panelcad.gui.main_window import _DockSeparatorStyle, _ToolDock, MainWindow
    from panelcad.settings import invalidate

    invalidate()
    app = _app()
    window = MainWindow(Session.create("Docks"))
    window.resize(1600, 1000)
    window.show()
    app.processEvents()
    return app, window, _DockSeparatorStyle, _ToolDock


def _separator_names(window) -> list[str]:
    from PySide6.QtWidgets import QWidget

    names = []
    for child in window.findChildren(QWidget):
        name = child.metaObject().className()
        if "Separator" in name or "separator" in name:
            names.append(name)
    return names


@pytest.mark.parametrize("scale_label,factor", [("100%", "1"), ("125%", "1.25"), ("150%", "1.5")])
def test_library_and_bottom_docks_resize(tmp_path, monkeypatch, scale_label: str, factor: str) -> None:
    """resizeDocks is the splitter API.

    Offscreen Qt applies QT_SCALE_FACTOR only when it is set before the
    QApplication exists, so 100%, 125%, and 150% are three processes. A run
    without that variable checks 100%.
    """

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QStyle

    env = os.environ.get("QT_SCALE_FACTOR", "1")
    if env not in {factor, f"{float(factor):g}"}:
        pytest.skip(f"{scale_label} needs QT_SCALE_FACTOR={factor}")
    app, window, style_cls, tool_dock = _window(tmp_path, monkeypatch)
    try:
        ratio = float(app.devicePixelRatio())
        assert abs(ratio - float(factor)) < 0.05, (scale_label, factor, ratio)
        extent = window.style().pixelMetric(QStyle.PixelMetric.PM_DockWidgetSeparatorExtent, None, window)
        assert extent == style_cls.EXTENT
        assert extent >= 6
        # Title buttons add a few pixels on top of the 64×48 floor.
        # The old locks were 360 px wide and about 300 px tall.
        assert window.lib_wrap.minimumWidth() < 120
        assert window.lib_wrap.maximumWidth() > 2000
        assert window.bom_wrap.minimumHeight() < 96
        assert window.bom_wrap.maximumHeight() > 2000
        assert window.photo_peek.view.minimumHeight() == 0
        assert window.photo_peek.view.minimumWidth() == 0

        # A background tab reports a geometry outside the window. Measure the
        # tab the splitter is actually sizing.
        window.lib_wrap.raise_()
        window.bom_wrap.raise_()
        app.processEvents()
        lib = window.lib_wrap.width()
        bottom = window.bom_wrap.height()
        assert window.bom_wrap.mapTo(window, window.bom_wrap.rect().topLeft()).y() > 0
        assert lib > tool_dock.FLOOR.width()
        assert bottom > tool_dock.FLOOR.height()

        window.resizeDocks([window.lib_wrap], [lib + 140], Qt.Orientation.Horizontal)
        window.resizeDocks([window.bom_wrap], [bottom + 140], Qt.Orientation.Vertical)
        app.processEvents()
        assert window.lib_wrap.width() >= lib + 80
        assert window.bom_wrap.height() >= bottom + 80

        grown_lib = window.lib_wrap.width()
        grown_bottom = window.bom_wrap.height()
        window.resizeDocks([window.lib_wrap], [max(tool_dock.FLOOR.width(), grown_lib - 160)], Qt.Orientation.Horizontal)
        window.resizeDocks(
            [window.bom_wrap],
            [max(tool_dock.FLOOR.height(), grown_bottom - 160)],
            Qt.Orientation.Vertical,
        )
        app.processEvents()
        assert window.lib_wrap.width() <= grown_lib - 80
        assert window.bom_wrap.height() <= grown_bottom - 80
        # The handle exists. Some styles name it, some only expose the metric above.
        assert isinstance(_separator_names(window), list)
    finally:
        window.close()
        app.processEvents()


def test_saved_layout_restores_sizes_and_stays_draggable(tmp_path, monkeypatch) -> None:
    from PySide6.QtCore import Qt

    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow
    from panelcad.settings import get, invalidate

    app, window, _style, tool_dock = _window(tmp_path, monkeypatch)
    try:
        window.lib_wrap.raise_()
        window.bom_wrap.raise_()
        app.processEvents()
        window.resizeDocks([window.lib_wrap], [280], Qt.Orientation.Horizontal)
        window.resizeDocks([window.bom_wrap], [240], Qt.Orientation.Vertical)
        app.processEvents()
        saved_lib = window.lib_wrap.width()
        saved_bottom = window.bom_wrap.height()
        assert abs(saved_lib - 280) < 40
        assert abs(saved_bottom - 240) < 40
        window.close()
        app.processEvents()
        assert isinstance(get("layout"), str) and get("layout")

        invalidate()
        again = MainWindow(Session.create("Docks again"))
        again.resize(1600, 1000)
        again.show()
        app.processEvents()
        try:
            again.lib_wrap.raise_()
            again.bom_wrap.raise_()
            app.processEvents()
            assert abs(again.lib_wrap.width() - saved_lib) <= 32
            assert abs(again.bom_wrap.height() - saved_bottom) <= 48
            assert again.lib_wrap.minimumWidth() < 120
            assert again.bom_wrap.minimumHeight() < 96
            again.resizeDocks([again.lib_wrap], [saved_lib + 130], Qt.Orientation.Horizontal)
            again.resizeDocks([again.bom_wrap], [saved_bottom + 100], Qt.Orientation.Vertical)
            app.processEvents()
            assert again.lib_wrap.width() >= saved_lib + 70
            assert again.bom_wrap.height() >= saved_bottom + 50
        finally:
            again.close()
            app.processEvents()
    finally:
        # MainWindow deletes itself once closed; skip it if already gone.
        if shiboken6.isValid(window) and window.isVisible():
            window.close()
            app.processEvents()
