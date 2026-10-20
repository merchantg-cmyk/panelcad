"""Tester C: Problems Fix, save/open errors, small-screen layout, and guidance."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_fix_handler_is_connected_once() -> None:
    from panelcad.gui.main_window import MainWindow

    source = inspect.getsource(MainWindow)
    assert source.count("\n    def _fix_finding(") == 1
    assert source.count("fix_requested.connect(self._fix_finding)") == 1


def test_fix_opens_one_dialog_for_each_row_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow

    _app()
    window = MainWindow(Session.create("Fix"))
    titles: list[str] = []
    notes: list[str] = []
    photos: list[str] = []

    def _exec(self) -> int:
        titles.append(self.windowTitle())
        return 0

    def _info(*args, **kwargs):
        notes.append(str(args[2] if len(args) > 2 else kwargs.get("text", "")))
        return QMessageBox.StandardButton.Ok

    class _Viewer:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def exec(self) -> int:
            photos.append("photo")
            return 0

    monkeypatch.setattr(QMessageBox, "exec", _exec)
    monkeypatch.setattr(QMessageBox, "information", staticmethod(_info))
    monkeypatch.setattr("panelcad.gui.photo_viewer.PhotoViewerDialog", _Viewer)
    monkeypatch.setattr(
        window.session,
        "reconstruct_photo_region",
        lambda **_kwargs: {"id": "p1"},
    )

    window._fix_finding(
        {
            "check": "layout_not_ready",
            "action": "pick_catalog",
            "ref": "Q1",
            "message": "Q1 needs a catalog part",
        }
    )
    window._fix_finding(
        {
            "check": "outside_enclosure",
            "ref": "A1",
            "message": "A1 is outside the enclosure",
        }
    )
    window._fix_finding(
        {
            "check": "missing_footprint",
            "ref": "PLC1-XS-L",
            "message": "PLC1-XS-L has no library width/height for enclosure sizing",
        }
    )
    window._fix_finding(
        {
            "check": "missing_part_number",
            "ref": "PLC1",
            "message": "PLC1 has no part number",
        }
    )
    window._fix_finding(
        {
            "check": "reconstruct_needs_info",
            "action": "width_mm",
            "ref": "U1",
            "item_id": "i2",
            "photo_id": "p1",
            "message": "U1 needs width",
        }
    )
    opened_nameplate: list[str] = []
    monkeypatch.setattr(window, "open_nameplate", lambda: opened_nameplate.append("nameplate"))
    window._fix_finding(
        {
            "check": "nameplate_missing_field",
            "message": "Fill in the nameplate before printing: Reports → Nameplate…",
        }
    )
    window._fix_finding(
        {
            "check": "terminal_not_on_strip",
            "ref": "PLC1",
            "message": "PLC1 is a terminal but is not on a strip",
        }
    )
    window.problems.fix_requested.emit(
        {"check": "reconstruct_print_unextracted", "message": "The old print was not read"}
    )

    assert titles == ["Fix layout part", "Fix layout part", "Fix layout part"]
    assert photos == ["photo"]
    assert opened_nameplate == ["nameplate"]
    assert notes[0].startswith("A1 is outside the enclosure")
    assert "Tools → Panel → Size Enclosure…" in notes[0]
    assert "Strip → Open strip…" in notes[1]
    assert "File → Import print…" in notes[2]
    window.close()


def test_fix_catalog_and_connection_run_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow

    _app()
    window = MainWindow(Session.create("Fix actions"))
    picked: list[str] = []
    confirmed: list[str] = []

    class _Part:
        catalog = "ABC-1"
        manufacturer = "Acme"

    monkeypatch.setattr("panelcad.gui.part_picker.pick_catalog_part", lambda *_a, **_k: _Part())
    monkeypatch.setattr(
        window.session,
        "reconstruct_confirm_item",
        lambda item_id, **kwargs: picked.append(f"{item_id}:{kwargs['catalog']}") or {},
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *_a, **_k: QMessageBox.StandardButton.Yes),
    )
    monkeypatch.setattr(
        window.session,
        "reconstruct_confirm_connection",
        lambda **kwargs: confirmed.append(kwargs["wire_id"]) or {},
    )
    window._fix_finding({"action": "pick_catalog", "item_id": "i1", "ref": "PLC1", "message": "pick"})
    window._fix_finding({"action": "confirm_connection", "ref": "w1", "item_id": "w1", "message": "inferred"})
    assert picked == ["i1:ABC-1"]
    assert confirmed == ["w1"]
    window.close()


def test_save_as_permission_keeps_the_drawing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QFileDialog

    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow, cant_save_message

    _app()
    original = tmp_path / "job.pcad.yaml"
    Session.create("Keep me").save(original)
    window = MainWindow(Session.open(original))
    blocked = tmp_path / "ro" / "x.pcad.yaml"
    shown: list[tuple] = []

    def denied(_project, _path) -> None:
        raise PermissionError(13, "Permission denied", str(_path))

    monkeypatch.setattr("panelcad.api.session.save_project", denied)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_a, **_k: (str(blocked), ""))
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *args, **_kwargs: shown.append(args) or QMessageBox.StandardButton.Ok),
    )
    assert window.save_project_as() is False
    assert window.session.path == original
    assert window.session.project.name == "Keep me"
    assert "Keep me" in original.read_text(encoding="utf-8")
    assert not blocked.exists()
    assert shown
    assert shown[0][1] == "Can't save there"
    assert shown[0][2] == cant_save_message(PermissionError(13, "Permission denied", str(blocked)))
    window.close()


def test_windows_lock_during_save_says_file_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow
    from panelcad.io import share as share_mod

    _app()
    path = tmp_path / "job.pcad.yaml"
    Session.create("Busy").save(path)
    window = MainWindow(Session.open(path))
    original = path.read_text(encoding="utf-8")
    warnings: list[tuple] = []

    def locked(src, dest) -> None:
        err = PermissionError(13, "The process cannot access the file", str(dest))
        err.winerror = 32
        raise err

    monkeypatch.setattr(share_mod, "REPLACE_PAUSES", ())
    monkeypatch.setattr(share_mod.os, "replace", locked)
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *args, **_kwargs: warnings.append(args) or QMessageBox.StandardButton.Ok),
    )
    assert window.save_project() is False
    assert warnings
    assert warnings[0][1] == "File busy"
    assert "busy" in str(warnings[0][2]).lower()
    assert "Can't save there" not in str(warnings[0][2])
    assert window.session.path == path
    assert path.read_text(encoding="utf-8") == original
    window.close()


def test_open_errors_are_plain_language(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow
    from panelcad.io.yaml_format import describe_open_error

    _app()
    empty = tmp_path / "empty.pcad.yaml"
    empty.write_text("", encoding="utf-8")
    bad = tmp_path / "bad.pcad.yaml"
    bad.write_text("name: Job\nsheets: [1, 2\n", encoding="utf-8")
    wrong = tmp_path / "wrong.pcad.yaml"
    wrong.write_text("name: Job\nsheets: 1\n", encoding="utf-8")
    missing = tmp_path / "missing.pcad.yaml"
    window = MainWindow(Session.create("Open"))
    shown: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        staticmethod(lambda *_a, **_k: shown.append(str(_a[2])) or QMessageBox.StandardButton.Ok),
    )
    for path in (empty, bad, wrong, missing):
        window.open_path(str(path))
        assert window.session.project.name == "Open"
    assert len(shown) == 4
    assert shown[3] == "Can't find missing.pcad.yaml."
    for text in shown[:3]:
        assert "damaged" in text
        assert "Help → Report a bug" in text
        assert "Recover" not in text
        lowered = text.lower()
        assert "pydantic" not in lowered
        assert "yaml mapping" not in lowered
        assert "validation error" not in lowered
        assert "traceback" not in lowered
    assert "(line " in shown[1]
    assert describe_open_error(FileNotFoundError(missing), missing) == "Can't find missing.pcad.yaml."
    denied = PermissionError(13, "Permission denied", str(missing))
    assert (
        describe_open_error(denied, missing)
        == "Can't open missing.pcad.yaml: you don't have permission to read it."
    )
    def _denied(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied", "secret.pcad.yaml")

    monkeypatch.setattr("panelcad.api.session.Session.open", _denied)
    window.open_path(str(tmp_path / "secret.pcad.yaml"))
    assert shown[-1] == "Can't open secret.pcad.yaml: you don't have permission to read it."
    assert "damaged" not in shown[-1]
    window.close()


def test_title_line_csv_is_only_a_parts_list(tmp_path: Path) -> None:
    from panelcad.guidance import classify_import
    from panelcad.gui.guidance import ImportFixDialog

    path = tmp_path / "bom.csv"
    path.write_text(
        "Order sheet 44\nPart #,Description,Qty,Brand\nABC-1,Widget,1,Acme\n",
        encoding="utf-8",
    )
    advice = classify_import(path)
    assert advice.route == "bom"
    assert "looks like a parts list" in advice.message
    assert "Unrecognized CSV" not in advice.message
    assert "Order sheet 44" not in advice.message
    assert "BOM dock" not in advice.message
    dialog = ImportFixDialog(advice, "")
    labels = [dialog.primary.text()]
    if dialog.secondary is not None:
        labels.append(dialog.secondary.text())
    if dialog.help_button is not None:
        labels.append(dialog.help_button.text())
    assert labels.count("What files can I import?") <= 1
    assert len(labels) == len(set(labels))


def test_import_fix_help_button_is_not_duplicated(tmp_path: Path) -> None:
    from panelcad.guidance import classify_import
    from panelcad.gui.guidance import ImportFixDialog

    unknown = tmp_path / "notes.csv"
    unknown.write_text("Hello,World\nalpha,beta\n", encoding="utf-8")
    advice = classify_import(unknown)
    dialog = ImportFixDialog(advice, "")
    labels = [dialog.primary.text()]
    if dialog.secondary is not None:
        labels.append(dialog.secondary.text())
    if dialog.help_button is not None:
        labels.append(dialog.help_button.text())
    assert labels.count("What files can I import?") == 1


def test_blank_project_has_one_nameplate_prompt() -> None:
    from panelcad.api.session import Session
    from panelcad.electrical.nameplate import NAMEPLATE_PROMPT
    from panelcad.gui.problems import ProblemsDock

    session = Session.create("Blank")
    rows = session.design_checks()
    nameplate = [row for row in rows if row["check"] == "nameplate_missing_field"]
    assert len(nameplate) == 1
    assert nameplate[0]["severity"] == "info"
    assert nameplate[0]["message"] == NAMEPLATE_PROMPT
    assert nameplate[0]["target"] == "nameplate"
    warnings = [row for row in rows if row["severity"] == "warning" and "nameplate" in row["check"]]
    assert warnings == []
    _app()
    dock = ProblemsDock()
    dock.set_rows(nameplate)
    assert dock.empty.isVisible() is False
    assert "ready to print" not in dock.empty.message.text()
    dock.close()


def test_narrow_window_keeps_a_usable_sheet() -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTabBar

    from panelcad.api.session import Session
    from panelcad.gui.import_wizard import ImportWizard
    from panelcad.gui.main_window import MainWindow

    app = _app()
    window = MainWindow(Session.create("Narrow"))
    window.resize(1024, 700)
    window.show()
    app.processEvents()
    assert window.width() <= 1024
    assert window.canvas.width() >= 600
    assert window.canvas.height() >= 300
    window.problems_wrap.raise_()
    app.processEvents()
    assert window.problems_wrap.height() >= 110
    fix = window.problems.fix
    assert fix.height() >= fix.sizeHint().height() - 1
    fix_bottom = fix.mapTo(window.problems, fix.rect().bottomLeft())
    assert 0 <= fix_bottom.y() <= window.problems.height()
    window.next_steps_wrap.raise_()
    app.processEvents()
    assert window.next_steps.height() >= 120
    window.show_checklists()
    app.processEvents()
    assert window.help_wrap in window.tabifiedDockWidgets(window.prop_wrap)
    viewport = window.help_panel.task_scroll.viewport()
    from PySide6.QtWidgets import QCheckBox

    visible_steps = 0
    for box in window.help_panel.task_body.findChildren(QCheckBox):
        center = box.mapTo(viewport, box.rect().center())
        if viewport.rect().contains(center):
            visible_steps += 1
    assert visible_steps >= 3
    assert window.dockWidgetArea(window.lib_wrap) == Qt.DockWidgetArea.LeftDockWidgetArea
    side = window.tabifiedDockWidgets(window.prop_wrap)
    assert window.ai_wrap in side
    window._watch_dock_tab_bars()
    bars = [bar for bar in window.findChildren(QTabBar) if window._owns_dock_tab_bar(bar) and bar.count()]
    assert bars
    for bar in bars:
        assert bar.elideMode() == Qt.TextElideMode.ElideNone
        assert bar.usesScrollButtons()
    window.close()

    wizard = ImportWizard()
    assert wizard.minimumSizeHint().height() < 700
    wizard.resize(960, 620)
    wizard.show()
    app.processEvents()
    wizard.resize(960, 620)
    app.processEvents()
    bottom = wizard.run_btn.mapTo(wizard, wizard.run_btn.rect().bottomLeft())
    assert wizard.run_btn.isVisible()
    assert bottom.y() < wizard.rect().height()
    wizard.close()


def test_library_buttons_are_not_clipped() -> None:
    from PySide6.QtWidgets import QPushButton

    from panelcad.api.session import Session
    from panelcad.gui.main_window import NARROW_WINDOW_PX, MainWindow

    app = _app()
    window = MainWindow(Session.create("Library buttons"))
    window.show()
    app.processEvents()
    try:
        for width, height in ((1024, 700), (1920, 1000)):
            window.resize(width, height)
            app.processEvents()
            buttons = [
                button
                for button in window.lib_wrap.findChildren(QPushButton)
                if button.isVisible()
            ]
            assert buttons
            for button in buttons:
                assert button.width() >= button.sizeHint().width(), button.text()
            if width < NARROW_WINDOW_PX:
                assert window.lib_wrap.width() <= 220
                assert window.canvas.width() >= 600
                assert window.canvas.height() >= 300
            else:
                assert window.lib_wrap.width() >= 320
    finally:
        window.close()


def test_widening_keeps_closed_side_docks_closed() -> None:
    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow

    app = _app()
    window = MainWindow(Session.create("Closed docks"))
    window.resize(1024, 700)
    window.show()
    app.processEvents()
    window.lib_wrap.close()
    window.ai_wrap.close()
    app.processEvents()
    window.resize(1600, 800)
    app.processEvents()
    assert window.lib_wrap.isVisible() is False
    assert window.ai_wrap.isVisible() is False
    assert window.prop_wrap.isVisible() is True
    window.close()
    app.processEvents()

    wide = MainWindow(Session.create("Closed before narrow"))
    wide.resize(1600, 800)
    wide.show()
    app.processEvents()
    wide.lib_wrap.close()
    wide.prop_wrap.close()
    wide.ai_wrap.close()
    app.processEvents()
    wide.resize(1024, 700)
    app.processEvents()
    wide.resize(1600, 800)
    app.processEvents()
    assert wide.lib_wrap.isVisible() is False
    assert wide.prop_wrap.isVisible() is False
    assert wide.ai_wrap.isVisible() is False
    wide.close()


def test_help_search_matches_whole_words() -> None:
    from panelcad.guidance import search_help

    assert search_help("")[0] == "start"
    found = search_help("lock")
    assert "problems" not in found
    assert "troubleshoot" not in found
    assert "wires" in search_help("wire")


def test_import_any_title_line_omits_the_sniff_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QDialog, QFileDialog, QLabel

    from panelcad.api.session import Session
    from panelcad.gui.guidance import ImportFixDialog
    from panelcad.gui.main_window import MainWindow

    _app()
    path = tmp_path / "bom.csv"
    path.write_text(
        "Order sheet 44\nPart #,Description,Qty,Brand\nABC-1,Widget,1,Acme\n",
        encoding="utf-8",
    )
    texts: list[str] = []

    def show(self):
        texts.append(self.message.text())
        for label in self.findChildren(QLabel):
            texts.append(label.text())
        self.reject()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(ImportFixDialog, "exec", show)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_a, **_k: (str(path), ""))
    window = MainWindow(Session.create("Title line"))
    window.import_any()
    blob = "\n".join(texts)
    assert "looks like a parts list" in blob
    assert "Unrecognized CSV" not in blob
    assert "Order sheet 44" not in blob
    window.close()


def test_choose_another_file_opens_the_import_dialog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QDialog, QFileDialog

    from panelcad.api.session import Session
    from panelcad.gui.guidance import ImportFixDialog
    from panelcad.gui.main_window import MainWindow

    _app()
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    calls: list[str] = []

    def opener(*args, **_kwargs):
        calls.append(str(args[1] if len(args) > 1 else ""))
        if len(calls) == 1:
            return (str(pdf), "")
        return ("", "")

    def show(self):
        assert self.secondary is not None
        assert self.secondary.text() == "Choose another file"
        self.secondary.click()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ImportFixDialog, "exec", show)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", opener)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_a, **_k: "")
    window = MainWindow(Session.create("Browse"))
    helped: list[str] = []
    monkeypatch.setattr(window, "show_help", lambda *args, **_kwargs: helped.append(str(args)))
    window.import_any()
    assert calls[0] == "Import"
    assert len(calls) >= 2
    assert helped == []
    window.close()
