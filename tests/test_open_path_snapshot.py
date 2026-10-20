"""Opening a saved drawing must not look dirty to install and rollback."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from panelcad.theme import recent_projects, remember_project
from tests.test_update import _app


def _left_release(widget) -> None:
    local = QPointF(8, 8)
    event = QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        local,
        widget.mapToGlobal(local),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    widget.mouseReleaseEvent(event)


def _confirm_without_a_box(window) -> None:
    from PySide6.QtCore import QTimer

    appeared: list[QMessageBox] = []

    def watch() -> None:
        boxes = [
            widget
            for widget in QApplication.topLevelWidgets()
            if isinstance(widget, QMessageBox) and widget.isVisible()
        ]
        appeared.extend(boxes)
        for box in boxes:
            box.close()

    QTimer.singleShot(0, watch)
    assert window._confirm_unsaved() is True
    QApplication.instance().processEvents()
    assert appeared == []


def test_open_path_remembers_the_saved_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow
    from panelcad.gui.start_page import _RecentRow

    app = _app()
    job = tmp_path / "job.pcad.yaml"
    other = tmp_path / "other.pcad.yaml"
    copied = tmp_path / "copy.pcad.yaml"
    Session.create("A").save(job)
    Session.create("B").save(other)

    window = MainWindow(Session.create("Untitled"), show_start=True)
    window.show()
    app.processEvents()
    window.open_path(str(job))
    app.processEvents()
    assert window.session.project.name == "A"
    assert window._project_dirty() is False
    _confirm_without_a_box(window)

    window.session.set_title_block(company="Acme")
    assert window._project_dirty() is True
    assert window.save_project() is True
    assert window._project_dirty() is False
    assert str(job) in recent_projects()

    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *_args, **_kwargs: (str(copied), "")
    )
    assert window.save_project_as() is True
    assert window._project_dirty() is False
    assert str(copied) in recent_projects()
    assert str(copied) in window.windowTitle()

    window.session.set_title_block(company="Dirty")
    assert window._project_dirty() is True
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", lambda *_args, **_kwargs: (str(other), "")
    )
    window.open_project()
    app.processEvents()
    assert window.session.project.name == "B"
    assert window._project_dirty() is False
    _confirm_without_a_box(window)
    assert str(other) in recent_projects()
    window.close()
    app.processEvents()

    remember_project(str(job))
    welcome = MainWindow(Session.create("Untitled"), show_start=True)
    welcome.show()
    app.processEvents()
    rows = [
        row
        for row in welcome._start.findChildren(_RecentRow)
        if row._path == str(job)
    ]
    assert rows
    _left_release(rows[0])
    app.processEvents()
    assert welcome.session.project.name == "A"
    assert welcome._project_dirty() is False
    _confirm_without_a_box(welcome)
    welcome.close()
