"""File > Open Recent, offscreen. The list is the shared settings file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QLabel, QMessageBox

from panelcad.io.winpath import (
    display_name,
    extended_windows_path,
    io_path,
    project_presence,
)
from panelcad.theme import (
    RECENT_LIMIT,
    clear_recent_projects,
    forget_project,
    recent_projects,
    remember_project,
    set_theme,
)


def test_google_drive_and_long_windows_paths_round_trip() -> None:
    short = r"G:\My Drive\PanelCAD\Projects\riverside pump.pcad.yaml"
    assert display_name(short) == "riverside pump.pcad.yaml"
    assert extended_windows_path(short) == short
    assert not extended_windows_path(short).startswith("\\\\?\\")

    parts = ["G:\\My Drive", *["customer drawings"] * 16, "pump-station.pcad.yaml"]
    full = "\\".join(parts)
    assert len(full) > 260
    assert display_name(full) == "pump-station.pcad.yaml"
    extended = extended_windows_path(full)
    assert extended.startswith("\\\\?\\G:\\My Drive\\")
    assert extended.endswith("\\pump-station.pcad.yaml")
    assert "\\\\?\\UNC\\" not in extended

    unc = "\\\\server\\share\\" + ("drawings\\" * 30) + "job.pcad.yaml"
    assert len(unc) > 260
    assert extended_windows_path(unc).startswith("\\\\?\\UNC\\server\\share\\")
    assert display_name(unc) == "job.pcad.yaml"

    remember_project("\\\\?\\" + full)
    remember_project("G:/My Drive/PanelCAD/Projects/riverside pump.pcad.yaml")
    remember_project(short)
    assert recent_projects() == [short, full]
    assert project_presence(short) == "missing"
    assert project_presence(full) == "missing"
    assert project_presence("\0") != "file"


def test_recent_load_drops_non_strings() -> None:
    from panelcad.settings import settings_path, update

    update(
        {
            "recent": [
                1,
                None,
                {"path": r"G:\My Drive\not-a-string.pcad.yaml"},
                "  ",
                "",
                True,
                2.5,
                r"G:\My Drive\keep.pcad.yaml",
            ]
        }
    )
    assert recent_projects() == [r"G:\My Drive\keep.pcad.yaml"]
    saved = json.loads(settings_path().read_text(encoding="utf-8"))
    assert 1 in saved["recent"]
    assert None in saved["recent"]


def test_recent_list_is_newest_first_and_capped() -> None:
    for index in range(RECENT_LIMIT + 2):
        remember_project(rf"G:\My Drive\job-{index}.pcad.yaml")
    names = [display_name(path) for path in recent_projects()]
    assert names == [f"job-{index}.pcad.yaml" for index in range(RECENT_LIMIT + 1, 1, -1)]
    forget_project("G:/My Drive/job-11.pcad.yaml")
    assert "job-11.pcad.yaml" not in [display_name(path) for path in recent_projects()]
    clear_recent_projects()
    assert recent_projects() == []


def test_recent_list_is_the_settings_file_not_auth_json() -> None:
    from panelcad.settings import settings_path

    set_theme("dark")
    drive = r"G:\My Drive\PanelCAD\Projects\riverside pump.pcad.yaml"
    remember_project(drive)
    path = settings_path()
    assert path.name == "settings.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["theme"] == "dark"
    assert saved["recent"] == [drive]
    text = path.read_text(encoding="utf-8")
    assert "auth.json" not in text
    assert ".grok" not in text
    names = {item.name for item in path.parent.iterdir()}
    assert "auth.json" not in names
    assert ".panelcad" not in names


def test_local_file_presence_and_io_path(tmp_path: Path) -> None:
    target = tmp_path / "job.pcad.yaml"
    target.write_text("name: Job\n", encoding="utf-8")
    assert io_path(target) == str(target)
    assert project_presence(target) == "file"
    assert project_presence(tmp_path / "gone.pcad.yaml") == "missing"
    assert display_name(target) == "job.pcad.yaml"


def _file_menu(window):
    for action in window.menuBar().actions():
        if action.text().replace("&", "") == "File":
            return action.menu()
    raise AssertionError("File menu")


def _recent_entries(menu):
    return [action for action in menu.actions() if action.property("recentPath")]


def _click_box(label: str, seen: list[QMessageBox]) -> None:
    def run() -> None:
        app = QApplication.instance()
        for widget in list(app.topLevelWidgets()):
            if isinstance(widget, QMessageBox) and widget.isVisible():
                seen.append(widget)
                assert "Traceback" not in widget.text()
                assert "Traceback" not in widget.informativeText()
                for button in widget.buttons():
                    if label in button.text():
                        button.click()
                        return

    QTimer.singleShot(0, run)


def test_open_recent_menu_lists_opens_and_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    older = tmp_path / "older.pcad.yaml"
    newer = tmp_path / "newer.pcad.yaml"
    copied = tmp_path / "copied.pcad.yaml"
    Session.create("Older").save(older)
    Session.create("Newer").save(newer)
    ghost = r"G:\My Drive\PanelCAD\Projects\gone.pcad.yaml"

    window = MainWindow(Session.create("Untitled"))
    window.show()
    app.processEvents()
    labels = [action.text().replace("&", "") for action in _file_menu(window).actions()]
    assert labels[1] == "Open…"
    assert labels[2] == "Open Recent"
    assert "&Recent" in _file_menu(window).actions()[2].text()

    window.open_path(str(older))
    window.open_path(str(newer))
    remember_project(ghost)
    window._fill_recent_menu()
    entries = _recent_entries(window._recent_menu)
    assert [action.property("recentPath") for action in entries] == [ghost, str(newer), str(older)]
    assert [action.text().replace("&", "") for action in entries] == [
        "gone.pcad.yaml",
        "newer.pcad.yaml",
        "older.pcad.yaml",
    ]
    for action, path in zip(entries, (ghost, str(newer), str(older)), strict=True):
        assert action.toolTip() == path
        assert action.statusTip() == path
        assert action.isEnabled()
    assert entries[0].property("recentMissing") is True
    assert entries[1].property("recentMissing") is False

    entries[1].hover()
    app.processEvents()
    assert window.statusBar().currentMessage() == str(newer)

    assert window.session.project.name == "Newer"
    window._recent_menu.popup(window.mapToGlobal(window.rect().center()))
    app.processEvents()
    window._recent_menu.setActiveAction(None)
    # Down, Down, Down lands on the oldest entry. Enter opens it.
    for _ in range(3):
        window._recent_menu.keyPressEvent(
            QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier)
        )
    window._recent_menu.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
    )
    app.processEvents()
    assert window.session.project.name == "Older"
    window._recent_menu.close()
    window._fill_recent_menu()
    entries = _recent_entries(window._recent_menu)
    missing = next(action for action in entries if action.property("recentPath") == ghost)

    seen: list[QMessageBox] = []
    _click_box("Close", seen)
    missing.trigger()
    app.processEvents()
    assert seen
    assert seen[0].text() == "Can't find gone.pcad.yaml."
    assert seen[0].informativeText() == ghost
    assert ghost in recent_projects()

    seen.clear()
    _click_box("Remove", seen)
    missing.trigger()
    app.processEvents()
    assert ghost not in recent_projects()
    assert "Removed" in window.statusBar().currentMessage()

    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args, **_kwargs: (str(copied), ""))
    assert window.save_project_as() is True
    assert recent_projects()[0] == str(copied)
    window._fill_recent_menu()
    assert _recent_entries(window._recent_menu)[0].property("recentPath") == str(copied)

    clear = next(
        action for action in window._recent_menu.actions() if action.text() == "Clear Recent"
    )
    assert clear.isEnabled()
    clear.trigger()
    assert recent_projects() == []
    assert window.statusBar().currentMessage() == "Recent projects cleared"
    window._fill_recent_menu()
    assert _recent_entries(window._recent_menu) == []
    empty = next(
        action for action in window._recent_menu.actions() if "No recent" in action.text()
    )
    assert empty.isEnabled() is False
    clear = next(
        action for action in window._recent_menu.actions() if action.text() == "Clear Recent"
    )
    assert clear.isEnabled() is False
    window.close()


def test_new_save_is_remembered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    dest = tmp_path / "fresh.pcad.yaml"
    window = MainWindow(Session.create("Fresh"))
    window.show()
    app.processEvents()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args, **_kwargs: (str(dest), ""))
    assert window.save_project() is True
    assert recent_projects()[0] == str(dest)
    window._fill_recent_menu()
    assert _recent_entries(window._recent_menu)[0].property("recentPath") == str(dest)
    window.close()


def test_start_page_recent_is_keyboard_accessible(tmp_path: Path) -> None:
    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow
    from panelcad.gui.start_page import _ElideLabel, _RecentRow

    app = QApplication.instance() or QApplication([])
    job = tmp_path / "mcc-2.pcad.yaml"
    Session.create("MCC").save(job)
    ghost = r"G:\My Drive\PanelCAD\Projects\gone.pcad.yaml"
    remember_project(str(job))
    remember_project(ghost)
    window = MainWindow(None, show_start=True)
    window.resize(1366, 768)
    window.show()
    app.processEvents()
    rows = window._start.findChildren(_RecentRow)
    assert [row._path for row in rows] == [ghost, str(job)]
    ghost_row, job_row = rows
    assert ghost_row.toolTip() == ghost
    assert ghost_row.statusTip() == ghost
    assert ghost_row.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert ghost_row.findChildren(_ElideLabel)[0].objectName() == "muted"
    assert job_row.findChildren(_ElideLabel)[0].objectName() != "muted"

    window._recent_menu.popup(window.mapToGlobal(window.rect().center()))
    app.processEvents()
    missing = [
        label
        for label in window._recent_menu.findChildren(QLabel)
        if label.objectName() == "recentMissing"
    ]
    assert missing
    assert missing[0].text() == "gone.pcad.yaml"
    window._recent_menu.close()

    ghost_row.setFocus()
    app.processEvents()
    assert ghost in window.statusBar().currentMessage()
    seen: list[QMessageBox] = []
    _click_box("Close", seen)
    QApplication.sendEvent(
        ghost_row,
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier),
    )
    app.processEvents()
    assert seen
    assert ghost in recent_projects()
    assert window.session.project.name != "MCC"

    job_row.setFocus()
    QApplication.sendEvent(
        job_row,
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier),
    )
    app.processEvents()
    assert window.session.project.name == "MCC"
    assert window.session.path == job
    window.close()
