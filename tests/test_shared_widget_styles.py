"""Styles handed to ``setStyle`` must outlive the widgets that use them.

``QWidget.setStyle`` does not take ownership, and with an application style
sheet Qt keeps a raw pointer to the style. A style parented to the window was
deleted before the window's children, which then called through freed memory
(0xC0000005 in Qt6Widgets.dll on Windows).
"""

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication


def _deliver_deletes(app) -> None:
    QCoreApplication.sendPostedEvents(None, int(QEvent.Type.DeferredDelete))
    app.processEvents()


def test_dock_style_is_shared_and_outlives_the_window() -> None:
    from shiboken6 import isValid

    from panelcad.gui.main_window import MainWindow, _shared_dock_style

    app = QApplication.instance() or QApplication([])
    style = _shared_dock_style()
    assert style.parent() is app
    first = MainWindow()
    second = MainWindow()
    assert style not in first.children()
    assert style not in second.children()
    first.show()
    first.close()
    first.deleteLater()
    _deliver_deletes(app)
    assert isValid(style)
    assert _shared_dock_style() is style
    second.close()
    second.deleteLater()
    _deliver_deletes(app)
    assert isValid(style)


def test_bom_fusion_style_is_shared_and_owned_by_the_app() -> None:
    from shiboken6 import isValid

    from panelcad.gui.bom_dock import _shared_fusion_style

    app = QApplication.instance() or QApplication([])
    style = _shared_fusion_style()
    assert style is not None
    assert style.parent() is app
    assert _shared_fusion_style() is style
    assert isValid(style)
