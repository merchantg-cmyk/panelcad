"""Guidance: why a control is off, import fix-it, empty states, sample, hints, next steps."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from panelcad.guidance import (
    apply_import_fix,
    bom_empty_copy,
    classify_import,
    dismiss_hint,
    empty_sheet_copy,
    hint_available,
    hints_enabled,
    next_steps_open,
    placeholder_size_issues,
    problems_empty_copy,
    project_checklist,
    set_hints_enabled,
    set_next_steps_open,
)
from panelcad.settings import invalidate

KEYENCE = """Part Number,Description,Qty,Manufacturer
IV3-500CA,Vision sensor head,2,Keyence
IV3-G500CA,Vision amplifier,2,Keyence
"""


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    invalidate()
    yield
    invalidate()


def _app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _write(folder: Path, name: str, text: str) -> Path:
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def test_explain_disabled_sets_reason_status_and_fix() -> None:
    from PySide6.QtGui import QAction

    from panelcad.gui.guidance import explain_disabled, run_disabled_fix

    _app()
    action = QAction("Send")
    ran: list[str] = []
    explain_disabled(
        action,
        "Send is off: no Slack address configured. Add one in Settings.",
        lambda: ran.append("fixed"),
    )
    assert action.isEnabled() is False
    assert "no Slack address" in action.toolTip()
    assert action.statusTip() == action.toolTip()
    assert action.property("guidance_reason") == action.toolTip()
    assert run_disabled_fix(action) is True
    assert ran == ["fixed"]


def test_every_disabled_action_has_a_reason() -> None:
    from PySide6.QtWidgets import QMenu

    from panelcad.api.session import Session
    from panelcad.gui.guidance import disabled_actions_missing_reason
    from panelcad.gui.main_window import MainWindow

    _app()
    window = MainWindow(Session.create("Reasons"))
    window._refresh_closed_menu()
    menu = QMenu()
    window.sheet_bar._fill_reopen_menu(menu)
    missing = disabled_actions_missing_reason(window)
    assert missing == []
    assert window._undo_action.isEnabled() is False
    assert "nothing to undo" in window._undo_action.toolTip()
    window.close()


def test_vendor_csv_is_a_parts_list_with_fix_buttons(tmp_path: Path) -> None:
    from panelcad.gui.guidance import ImportFixDialog

    _app()
    path = _write(tmp_path, "parts.csv", KEYENCE)
    advice = classify_import(path)
    assert advice.route == "bom"
    assert advice.primary_label == "Import as parts list"
    assert advice.secondary_label == "Open BOM import"
    assert advice.help_label == "What files can I import?"
    dialog = ImportFixDialog(advice, "do not know how to import parts.csv")
    assert "looks like a parts list" in dialog.message.text()
    dialog.primary.click()
    assert dialog.choice == "import"


def test_preamble_csv_fix_routes_to_parts_and_stays_needs_info(tmp_path: Path) -> None:
    from panelcad import Session
    from panelcad.io.bom import instance_rows
    from panelcad.library.panel import effective_footprint, is_layout_ready
    from panelcad.reconstruct.placeholder import is_needs_info_component, project_has_needs_info

    path = _write(tmp_path, "order.csv", "Keyence order sheet\n" + KEYENCE)
    advice = classify_import(path)
    assert advice.route == "bom"
    assert advice.prepared_text is not None
    assert "Part Number" in advice.prepared_text.splitlines()[0]
    session = Session.create("Fix it")
    result = apply_import_fix(session, path, advice)
    assert result["kind"] == "bom"
    assert result["errors"] == []
    created = [session.sheet().component(ref) for ref in result["created"]]
    assert created
    for comp in created:
        assert comp.library_id == "reconstruct.needs_info"
        assert is_needs_info_component(comp)
        assert comp.properties.get("bom") == "no"
        for key in ("width_mm", "height_mm", "depth_mm", "mount", "dim_source"):
            assert key not in comp.properties
        device = session.catalog.get(comp.library_id)
        footprint = effective_footprint(device, comp)
        assert footprint.width_mm == 0
        assert is_layout_ready(device, comp) is False
    assert project_has_needs_info(session.project)
    assert all(row["library_id"] != "reconstruct.needs_info" for row in instance_rows(session.project, session.catalog))


def test_gui_fix_button_imports_a_preamble_parts_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QDialog, QMessageBox

    from panelcad.api.session import Session
    from panelcad.gui.guidance import ImportFixDialog
    from panelcad.gui.main_window import MainWindow
    from panelcad.gui.needs_info_dialog import NeedsInfoImportDialog
    from panelcad.reconstruct.placeholder import project_has_needs_info

    _app()
    guides: list[NeedsInfoImportDialog] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Ok),
    )
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Ok),
    )

    def choose_primary(self):
        self.primary.click()
        return QDialog.DialogCode.Accepted

    def show_guide(self):
        guides.append(self)
        self.reject()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(ImportFixDialog, "exec", choose_primary)
    monkeypatch.setattr(NeedsInfoImportDialog, "exec", show_guide)
    window = MainWindow(Session.create("Fix GUI"))
    path = _write(tmp_path, "order.csv", "Keyence order sheet\n" + KEYENCE)
    monkeypatch.setattr(
        "panelcad.gui.main_window.QFileDialog.getOpenFileName",
        staticmethod(lambda *args, **kwargs: (str(path), "")),
    )
    window.import_any()
    assert project_has_needs_info(window.session.project)
    assert guides
    body = guides[0].message.text()
    assert "NOT FOR BUILD" in body
    assert "IV3-500CA" in body
    assert guides[0].show_bom.text() == "Show in BOM"
    assert guides[0].match.text() == "Match to catalog part…"
    created = [
        comp
        for sheet in window.session.project.sheets
        for comp in sheet.components
        if comp.library_id == "reconstruct.needs_info"
    ]
    assert created
    for comp in created:
        for key in ("width_mm", "height_mm", "depth_mm"):
            assert key not in comp.properties
    window.close()


def test_wire_pdf_and_unknown_files_get_their_own_routes(tmp_path: Path) -> None:
    wires = _write(tmp_path, "wires.csv", "notes\nfrom,to\nS1.13,K1.A1\n")
    advice = classify_import(wires)
    assert advice.route == "wires"
    assert advice.primary_label == "Import as wires"
    pdf = _write(tmp_path, "drawing.pdf", "%PDF-1.4\n")
    pdf_advice = classify_import(pdf)
    assert pdf_advice.route == "print"
    assert pdf_advice.primary_label == "Import print"
    unknown = _write(tmp_path, "notes.csv", "Hello,World\nalpha,beta\n")
    unknown_advice = classify_import(unknown)
    assert unknown_advice.route == "unknown"
    assert unknown_advice.primary_label == "What files can I import?"


def test_import_help_names_print_photos_and_svg() -> None:
    from panelcad.guidance import help_topic

    body = help_topic("import")["body"]
    assert (
        "A print (PDF, photo or scan as JPG/PNG/TIFF, DXF, or SVG) uses File → Import print. "
        "SVG lines and text are read as vectors, not as a photo. "
        "Photos of a real panel use File → Reconstruct from photos."
    ) in body
    assert "does not turn an SVG" not in body
    assert "isn't supported yet" not in body


def test_scan_files_use_the_same_import_print_route_as_a_pdf(tmp_path: Path) -> None:
    from panelcad.api.session import Session
    from panelcad.io.exchange import SCAN_SUFFIXES, sniff

    session = Session.create("Scan route")
    pdf = _write(tmp_path, "drawing.pdf", "%PDF-1.4\n")
    assert sniff(pdf) == "pdf"
    for suffix in sorted(SCAN_SUFFIXES):
        path = tmp_path / f"panel{suffix}"
        path.write_bytes(b"not-a-project")
        assert sniff(path) == "pdf"
        advice = classify_import(path)
        assert advice.route == "print"
        assert advice.primary_label == "Import print"
        assert "print" in advice.message.lower()
        with pytest.raises(ValueError, match="print import"):
            session.import_any(path)
    svg = _write(tmp_path, "drawing.svg", "<svg/>")
    svg_advice = classify_import(svg)
    assert svg_advice.route == "print"
    assert "SVG" in svg_advice.message
    with pytest.raises(ValueError, match="print import") as raised:
        session.import_any(svg)
    assert "not supported" not in str(raised.value).lower()


def test_file_import_sends_a_scan_to_import_print(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QDialog

    from panelcad.api.session import Session
    from panelcad.gui.guidance import ImportFixDialog
    from panelcad.gui.main_window import MainWindow

    _app()
    path = tmp_path / "panel.webp"
    path.write_bytes(b"RIFF")
    opened: list[str] = []

    def choose_primary(self):
        self.primary.click()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ImportFixDialog, "exec", choose_primary)
    window = MainWindow(Session.create("Scan GUI"))
    monkeypatch.setattr(window, "import_print", lambda: opened.append("print"))
    monkeypatch.setattr(
        "panelcad.gui.main_window.QFileDialog.getOpenFileName",
        staticmethod(lambda *args, **kwargs: (str(path), "")),
    )
    window.import_any()
    assert opened == ["print"]
    window.close()


def test_import_print_is_the_wizard_and_lists_bmp_and_webp() -> None:
    import inspect

    from panelcad.gui.import_wizard import PRINT_FILE_FILTER
    from panelcad.gui.main_window import MainWindow

    assert "*.bmp" in PRINT_FILE_FILTER
    assert "*.webp" in PRINT_FILE_FILTER
    assert "*.dxf" in PRINT_FILE_FILTER
    source = inspect.getsource(MainWindow)
    assert source.count("def import_print(") == 1
    method = inspect.getsource(MainWindow.import_print)
    assert "ImportWizard" in method
    assert "PDF (*.pdf)" not in method
    assert "from-print" not in method


def test_empty_bom_layout_sheet_and_problems() -> None:
    from panelcad.api.session import Session
    from panelcad.gui.bom_dock import BomDock
    from panelcad.gui.problems import ProblemsDock

    app = _app()
    calls: list[str] = []
    bom = BomDock(on_import=lambda: calls.append("import"))
    bom.set_session(Session.create("Empty BOM"))
    bom.show()
    app.processEvents()
    assert bom.empty.isVisible()
    assert "Import a vendor parts list" in bom.empty.message.text()
    button = bom.empty.button("Import a vendor parts list (CSV)")
    assert button is not None
    button.click()
    assert calls == ["import"]
    assert bom_empty_copy(0, 3).startswith("No parts on the BOM. NOT FOR BUILD: 3")
    session = Session.create("Placed")
    session.place("iec.breakers.mcb_3p_32a", ref="Q1", x=10, y=10)
    bom.set_session(session)
    assert bom.empty.isVisible() is False

    problems = ProblemsDock()
    problems.show()
    app.processEvents()
    assert problems.empty.isVisible()
    assert problems_empty_copy(0) == "No problems: ready to print."
    assert "ready to print" in problems.empty.message.text()
    problems.set_rows(
        [{"severity": "warning", "check": "x", "message": "look", "sheet": "power", "ref": "Q1"}]
    )
    assert problems.empty.isVisible() is False

    message, buttons = empty_sheet_copy("schematic", components=0, wires=0)
    assert "empty" in message
    assert "Import a vendor parts list (CSV)" in {label for label, _ident in buttons}
    _layout_message, layout_buttons = empty_sheet_copy("panel_layout", components=0, wires=0)
    assert "Build layout from schematic" in {label for label, _ident in layout_buttons}
    assert empty_sheet_copy("schematic", components=1, wires=0) == ("", ())


def test_sample_project_loads_without_placeholder_sizes() -> None:
    from panelcad import Session
    from panelcad.examples.sample_catalog import build_sample, sample_yaml_path
    from panelcad.gui.start_page import StartPage

    _app()
    session = build_sample()
    assert placeholder_size_issues(session) == []
    layout = next(sheet for sheet in session.project.sheets if sheet.kind == "panel_layout")
    assert layout.enclosure is not None
    assert str(layout.enclosure.catalog or "").strip()
    loaded = Session.open(sample_yaml_path())
    assert loaded.project.name == "Sample catalog starter"
    assert placeholder_size_issues(loaded) == []
    from PySide6.QtWidgets import QPushButton

    page = StartPage()
    button = page.findChild(QPushButton, "openSample")
    assert button is not None
    assert "sample" in button.text().lower()


def test_help_topic_and_action_learn_more() -> None:
    from panelcad.api.session import Session
    from panelcad.gui.guidance import HelpDialog
    from panelcad.gui.main_window import MainWindow

    _app()
    dialog = HelpDialog()
    dialog.show_topic("import")
    body = dialog.body.toPlainText()
    assert "parts list" in body.lower()
    assert "NOT FOR BUILD" in body
    window = MainWindow(Session.create("Tips"))
    action = window._find_action("Import…")
    assert action is not None
    assert "Learn more" in action.toolTip()
    assert action.property("guidance_help") == "import"
    assert "Learn more" in action.statusTip()
    window.close()


def test_hint_suppression_setting() -> None:
    from panelcad.gui.guidance import HintBanner
    from panelcad.gui.settings_dialog import SettingsDialog

    _app()
    assert hints_enabled() is True
    assert hint_available("wire") is True
    dialog = SettingsDialog()
    assert dialog.hints.isChecked() is True
    dialog.hints.setChecked(False)
    assert hints_enabled() is False
    assert hint_available("wire") is False
    banner = HintBanner()
    assert banner.offer("wire") is False
    assert banner.isHidden()
    set_hints_enabled(True)
    assert banner.offer("library") is True
    dismiss_hint("library")
    assert hint_available("library") is False
    assert banner.offer("library") is False


def test_checklist_tracks_project_and_do_it_wires() -> None:
    from panelcad.api.session import Session
    from panelcad.examples.sample_catalog import build_sample
    from panelcad.gui.main_window import MainWindow

    _app()
    blank = Session.create("Blank")
    blank_steps = {step.id: step.done for step in project_checklist(blank)}
    assert blank_steps == {step: False for step in blank_steps}
    blank.add_sheet("enclosure", title="Enclosure", kind="panel_layout")
    blank.set_active_sheet("enclosure")
    blank.set_enclosure(width_mm=508, height_mm=305, depth_mm=203, name="box", catalog="A201208LP")
    steps = {step.id: step for step in project_checklist(blank)}
    assert steps["enclosure"].done is True
    assert steps["devices"].done is False

    sample = build_sample()
    sample_steps = {step.id: step for step in project_checklist(sample)}
    assert sample_steps["parts"].done is True
    assert "NOT FOR BUILD" not in sample_steps["parts"].detail
    assert sample_steps["print"].done is True

    window = MainWindow(sample)
    window.show()
    _app().processEvents()
    window.next_steps.recompute_now()
    assert "Ready to print" in window.next_steps._details["print"].text()
    window.next_steps.button("wires").click()
    assert window.canvas.mode == "wire"
    window.next_steps.button("devices").click()
    _app().processEvents()
    assert window.lib_wrap.isVisible()
    window.close()


def test_next_steps_persist_and_tab_switch_does_not_recompute() -> None:
    from PySide6.QtWidgets import QPushButton

    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow

    _app()
    session = Session.create("Tabs")
    session.add_sheet("two", title="Two", kind="schematic")
    window = MainWindow(session)
    window.show()
    _app().processEvents()
    dismiss = window.next_steps.findChild(QPushButton, "nextStepsDismiss")
    assert dismiss is not None
    dismiss.click()
    assert next_steps_open() is False
    assert window.next_steps_wrap.isHidden()
    window.close()

    again = MainWindow(Session.create("Remembered"))
    assert again.next_steps_wrap.isHidden()
    set_next_steps_open(True)
    again.close()

    fresh = Session.create("Switch")
    fresh.add_sheet("other", title="Other", kind="schematic")
    switched = MainWindow(fresh)
    switched.show()
    _app().processEvents()
    switched.next_steps._timer.stop()
    switched.next_steps._dirty = False
    schedules = switched.next_steps.schedules
    recomputes = switched.next_steps.recomputes
    started = time.perf_counter()
    for _ in range(30):
        switched.next_sheet()
        switched.prev_sheet()
    _app().processEvents()
    elapsed = time.perf_counter() - started
    assert switched.next_steps.schedules == schedules
    assert switched.next_steps.recomputes == recomputes
    assert switched.next_steps._timer.isActive() is False
    assert elapsed < 3.0
    switched.close()


def test_welcome_tour_and_tasks_persist_in_settings_not_the_project(tmp_path: Path) -> None:
    from panelcad import Session
    from panelcad.guidance import (
        search_help,
        search_tasks,
        set_task_dismissed,
        set_task_step,
        set_tour_state,
        set_welcome_status,
        task_record,
        tour_state,
        welcome_pending,
        welcome_status,
    )
    from panelcad.settings import load

    assert welcome_pending() is True
    assert search_help("")[0] == "start"
    assert "csv" in search_help("vendor csv")
    assert "csv-import" in search_tasks("part number")
    assert search_tasks("no-such-checklist-xyz") == []
    set_welcome_status("dismissed")
    set_tour_state(status="paused", index=3)
    set_task_step("csv-import", "header", True)
    set_task_step("csv-import", "not-a-step", True)
    set_task_dismissed("missing-task", True)
    invalidate()
    assert welcome_pending() is False
    assert welcome_status() == "dismissed"
    assert tour_state() == {"status": "paused", "index": 3}
    record = task_record("csv-import")
    assert record["checked"] == ["header"]
    assert record["dismissed"] is False
    assert task_record("missing-task")["dismissed"] is False
    stored = load()["guidance"]
    assert stored["welcome"] == "dismissed"
    assert stored["tasks"]["csv-import"]["checked"] == ["header"]
    session = Session.create("Guidance stays out")
    dest = tmp_path / "job.pcad.yaml"
    session.save(dest)
    text = dest.read_text(encoding="utf-8")
    assert "hints_seen" not in text
    assert "csv-import" not in text
    assert "notes_dismissed" not in text


def test_help_panel_searches_and_checklists_tick() -> None:
    from panelcad.gui.guidance import HelpPanel
    from panelcad.guidance import task_record

    _app()
    panel = HelpPanel()
    panel.show_topic("csv")
    assert "Part Number" in panel.body.toPlainText()
    assert "NOT FOR BUILD" in panel.body.toPlainText()
    panel.search.setText("troubleshoot lockout")
    assert panel.current_topic() == "troubleshoot"
    assert "NFPA 70E" in panel.body.toPlainText()
    panel.search.setText("zzz-no-howto")
    assert panel.body.toPlainText().startswith("Nothing in Help")
    panel.show_tasks("wire")
    from PySide6.QtWidgets import QCheckBox

    box = panel.findChild(QCheckBox, "task_wire_mode")
    assert box is not None
    box.setChecked(True)
    assert "mode" in task_record("wire")["checked"]
    panel.task_hide.click()
    assert task_record("wire")["dismissed"] is True
    assert panel.task_hidden.isHidden() is False
    panel.task_show.click()
    assert task_record("wire")["dismissed"] is False
    assert panel.findChild(QCheckBox, "task_wire_mode") is not None
    panel.close()


def test_csv_dialog_explains_the_header_and_imports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QDialog, QPushButton

    from panelcad.api.session import Session
    from panelcad.gui.guidance import CsvImportDialog
    from panelcad.gui.import_wizard import ImportWizard
    from panelcad.gui.main_window import MainWindow
    from panelcad.guidance import dismiss_note, note_dismissed
    from panelcad.reconstruct.placeholder import project_has_needs_info

    _app()
    dialog = CsvImportDialog()
    assert dialog.note.isHidden() is False
    assert "Part Number" in dialog.summary.text()
    assert "NOT FOR BUILD" in dialog.summary.text()
    assert "From and To" in dialog.summary.text()
    dismiss = dialog.note.findChild(QPushButton, "inlineGuidanceDismiss")
    assert dismiss is not None
    dismiss.click()
    assert note_dismissed("csv-import") is True
    again = CsvImportDialog()
    assert again.note.isHidden()
    assert "Part Number" in again.summary.text()
    path = _write(tmp_path, "parts.csv", KEYENCE)
    window = MainWindow(Session.create("CSV guide"))

    def accept_path(self):
        self.path.setText(str(path))
        self.accept()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(CsvImportDialog, "exec", accept_path)
    window.import_csv()
    assert project_has_needs_info(window.session.project)
    blob = " ".join(
        str(comp.properties)
        for sheet in window.session.project.sheets
        for comp in sheet.components
    )
    assert "IV3-500CA" in blob
    window.close()

    wizard = ImportWizard()
    assert wizard.guide.isHidden() is False
    assert "vision backend" in wizard.guide.body.text()
    dismiss_note("print-import")
    quiet = ImportWizard()
    assert quiet.guide.isHidden()


def test_tour_whats_this_and_help_reopen(monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QPushButton

    from panelcad.api.session import Session
    from panelcad.guidance import set_welcome_status, tour_state, welcome_pending, welcome_status
    from panelcad.gui.guidance import WelcomeDialog
    from panelcad.gui.main_window import MainWindow
    from panelcad.learn import library_search_empty, quote_empty_copy

    _app()
    app = _app()
    assert quote_empty_copy(0).startswith("No parts on the quote yet")
    assert quote_empty_copy(2) == ""
    assert "Catalog parts" in library_search_empty("zzzz-no-symbol", 0)
    assert library_search_empty("", 0) == ""

    window = MainWindow(Session.create("Tour"))
    window.show()
    app.processEvents()
    assert welcome_pending() is True
    assert window.findChild(type(window), "welcomeDialog") is None
    helps = [
        action
        for action in window.findChildren(type(window._find_action("About")))
        if action.text().replace("&", "") == "Help" and action.shortcut().toString() == "F1"
    ]
    assert helps
    help_action = helps[0]
    shortcuts = window._find_action("Keyboard shortcuts")
    assert shortcuts is not None
    assert shortcuts.shortcut().toString() == ""
    help_action.trigger()
    app.processEvents()
    assert window.help_wrap.isVisible()
    assert "how-tos" in window.help_panel.body.toPlainText().lower() or window.help_panel.heading.text()
    window.help_panel.show_topic("import")
    assert "NOT FOR BUILD" in window.help_panel.body.toPlainText()

    window.start_tour(True)
    app.processEvents()
    assert window.tour_card.isVisible()
    assert window.tour_card.heading.text() == "Library"
    assert tour_state()["status"] == "active"
    window.tour_card.next.click()
    assert tour_state()["index"] == 1
    assert window.tour_card.heading.text() == "Sheet"
    window.tour_card.hide_button.click()
    assert tour_state()["status"] == "paused"
    assert window.tour_card.isHidden()
    window.resume_tour()
    assert window.tour_card.heading.text() == "Sheet"
    window.tour_card.skip_button.click()
    assert tour_state()["status"] == "dismissed"
    window.close()
    app.processEvents()

    reopened = MainWindow(Session.create("Resume"))
    reopened.show()
    app.processEvents()
    assert reopened.tour_card.isHidden()
    assert welcome_pending() is False
    reopened.resume_tour()
    assert reopened.tour_card.isVisible()
    assert tour_state()["index"] == 1
    reopened.close()

    fresh = MainWindow(Session.create("Welcome"))
    set_welcome_status("pending")

    def start_drawing(self):
        self.start_button.click()
        return self.DialogCode.Accepted

    monkeypatch.setattr(WelcomeDialog, "exec", start_drawing)
    fresh.offer_welcome()
    assert welcome_status() == "dismissed"
    fresh.offer_welcome()
    assert welcome_status() == "dismissed"
    fresh.enter_whats_this()
    assert fresh.whats_this.active is True
    fresh.whats_this.show_for(fresh.lib_wrap)
    assert fresh.whats_this.active is False
    assert "Library" in fresh.whats_this.popup.heading.text()
    assert "Drag a symbol" in fresh.whats_this.popup.body.text()
    fresh.enter_whats_this()
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    fresh.whats_this.eventFilter(
        fresh,
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier),
    )
    assert fresh.whats_this.active is False
    assert "No parts on the quote yet" in fresh.quote.empty_hint.text()
    fresh.library_dock.search.setText("zzzz-no-symbol")
    assert fresh.library_dock.empty_hint.isHidden() is False
    assert "Next:" in fresh.library_dock.empty_hint.text()
    fresh.show_checklists()
    app.processEvents()
    fresh.help_panel.show_tasks("wire")
    do_wire = fresh.help_panel.findChild(QPushButton, "taskDo_wire_click")
    assert do_wire is not None
    do_wire.click()
    assert fresh.canvas.mode == "wire"
    fresh.close()


def test_empty_states_name_the_next_step() -> None:
    from panelcad.guidance import empty_sheet_copy, troubleshoot_empty_copy
    from panelcad.learn import quote_empty_copy

    message, buttons = empty_sheet_copy("schematic", components=0, wires=0)
    assert message.startswith("This sheet is empty. Next:")
    assert "Import a vendor parts list (CSV)" in {label for label, _ident in buttons}
    layout_message, _layout_buttons = empty_sheet_copy("panel_layout", components=0, wires=0)
    assert layout_message.startswith("This layout is empty. Next:")
    trouble = troubleshoot_empty_copy(devices=0)
    assert trouble.startswith("No device tags")
    assert "Next:" in trouble
    assert quote_empty_copy(0)
    assert quote_empty_copy(1) == ""


def test_help_points_at_the_settings_tabs_and_the_ai_dock() -> None:
    from panelcad.guidance import HELP_TOPICS, help_topic
    from panelcad.learn import WHATS_THIS, task_button_label

    ai = WHATS_THIS["aiDock"]["text"]
    assert "datasheet" not in ai.lower()
    assert "driving the drawing" in ai
    assert "the page" in ai
    assert "live operations" in ai
    assert "prompt box" in ai
    assert "your own connected AI" in ai
    assert "Edit → Settings… (Ctrl+,), on the AI tab" in help_topic("print-import")["body"]
    assert "Edit → Settings… (Ctrl+,), on the AI tab" in help_topic("troubleshoot")["body"]
    assert "Edit → Settings… (Ctrl+,), on the Appearance tab" in help_topic("hints")["body"]
    blob = "\n".join(str(topic["body"]) for topic in HELP_TOPICS.values())
    assert "Settings → AI" not in blob
    assert "Settings → Appearance" not in blob
    assert "Settings > AI" not in blob
    assert task_button_label("print-help") == "Read more"
    assert task_button_label("wire") == "Do this"


def test_checklist_rows_keep_the_button_readable() -> None:
    from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton

    from panelcad.gui.guidance import HelpPanel

    app = _app()
    panel = HelpPanel()
    panel.resize(480, 160)
    panel.show()
    panel.show_tasks("csv-import")
    app.processEvents()
    do = panel.findChild(QPushButton, "taskDo_csv-import_open")
    read = panel.findChild(QPushButton, "taskDo_csv-import_header")
    assert do is not None and do.text() == "Do this"
    assert read is not None and read.text() == "Read more"
    assert do.height() >= do.sizeHint().height()
    assert do.width() >= do.sizeHint().width()
    assert read.height() >= read.sizeHint().height()
    assert read.width() >= read.sizeHint().width()
    box = panel.findChild(QCheckBox, "task_csv-import_open")
    detail = panel.findChild(QLabel, "taskDetail_csv-import_open")
    assert box is not None and detail is not None
    assert detail.y() > box.y()
    row = detail.parentWidget()
    assert row is not None
    assert row.minimumHeight() >= do.height()
    panel.show_tasks("wire")
    app.processEvents()
    wire = panel.findChild(QPushButton, "taskDo_wire_click")
    assert wire is not None and wire.text() == "Do this"
    panel.close()


def test_tour_card_stays_inside_the_window() -> None:
    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow

    app = _app()
    window = MainWindow(Session.create("Tour fit"))
    try:
        window.resize(1024, 700)
        window.show()
        # Windows does not activate a window that a background test opened, and
        # a Qt.Tool card is hidden until the owner is active. Raise it so the
        # card is measured in the same state a person sees.
        window.raise_()
        window.activateWindow()
        app.processEvents()
        _assert_tour_cards_inside(window, app, "1024x700")
        window.showMaximized()
        app.processEvents()
        _assert_tour_cards_inside(window, app, "maximized")
    finally:
        window.close()
        app.processEvents()


def _assert_tour_cards_inside(window, app, label: str) -> None:
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtWidgets import QWidget

    from panelcad.learn import TOUR_STEPS

    def global_rect(widget) -> QRect:
        return QRect(widget.mapToGlobal(QPoint(0, 0)), widget.size())

    for index, step in enumerate(TOUR_STEPS):
        window._show_tour_step(index)
        app.processEvents()
        card = window.tour_card
        assert card.isVisible(), step["id"]
        # A top-level card's frame is already global. A child card reports
        # frameGeometry in the parent, so map that same rectangle on screen.
        frame = card.frameGeometry()
        if card.isWindow():
            card_rect = frame
        else:
            card_rect = QRect(card.mapToGlobal(QPoint(0, 0)), frame.size())
        win_rect = window.frameGeometry()
        screen = window.screen().availableGeometry()
        assert win_rect.contains(card_rect), (label, step["id"], card_rect, win_rect)
        assert screen.contains(card_rect), (label, step["id"], card_rect, screen)
        anchor = window.findChild(QWidget, step["target"])
        assert anchor is not None, step["target"]
        anchor_rect = global_rect(anchor)
        if step["target"] == "sheetCanvas":
            overlap = anchor_rect.intersected(card_rect)
            assert overlap.isValid(), (label, step["id"], card_rect, anchor_rect)
            if anchor_rect.width() >= card_rect.width() and anchor_rect.height() >= card_rect.height():
                assert anchor_rect.contains(card_rect), (label, step["id"], card_rect, anchor_rect)
        elif step["target"] == "mainToolbar":
            assert card_rect.top() > anchor_rect.bottom(), (label, card_rect, anchor_rect)
        elif step["target"] in {"libraryDock", "inspectorDock"}:
            overlap = anchor_rect.intersected(card_rect)
            assert not overlap.isValid() or overlap.width() < 2, (label, step["id"], card_rect, anchor_rect)
            beside = card_rect.right() < anchor_rect.left() or card_rect.left() > anchor_rect.right()
            assert beside, (label, step["id"], card_rect, anchor_rect)
        else:
            assert card_rect.bottom() < anchor_rect.top(), (label, step["id"], card_rect, anchor_rect)


def test_tour_card_narrows_beside_the_dock_when_the_screen_is_narrow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A screen narrower than the window must not pull the card onto the dock."""

    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtWidgets import QWidget

    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow

    app = _app()
    window = MainWindow(Session.create("Narrow screen"))
    try:
        window.resize(1024, 700)
        window.move(0, 0)
        window.show()
        window.move(0, 0)
        app.processEvents()
        anchor = window.findChild(QWidget, "libraryDock")
        assert anchor is not None
        dock = QRect(anchor.mapToGlobal(QPoint(0, 0)), anchor.size())
        # Wider than the 240 px minimum, narrower than the 360 px card.
        free = 280
        screen_rect = QRect(0, 0, dock.right() + 1 + free, 900)

        class NarrowScreen:
            def availableGeometry(self) -> QRect:
                return screen_rect

            def geometry(self) -> QRect:
                return screen_rect

        monkeypatch.setattr(window, "screen", lambda: NarrowScreen())
        window._show_tour_step(0)
        app.processEvents()
        card = window.tour_card
        frame = card.frameGeometry()
        if card.isWindow():
            card_rect = frame
        else:
            card_rect = QRect(card.mapToGlobal(QPoint(0, 0)), frame.size())
        anchor_rect = QRect(anchor.mapToGlobal(QPoint(0, 0)), anchor.size())
        assert screen_rect.contains(card_rect)
        assert window.frameGeometry().contains(card_rect)
        assert card_rect.left() > anchor_rect.right()
        assert not card_rect.intersects(anchor_rect)
        assert card.width() >= 240
        assert card.width() < 360
    finally:
        window.close()
        app.processEvents()


def _library_card_on_narrow_screen(monkeypatch: pytest.MonkeyPatch, free: int):
    """Show the library stop with ``free`` px of screen to the right of the dock."""

    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtWidgets import QWidget

    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow

    app = _app()
    window = MainWindow(Session.create("Narrow screen"))
    window.resize(1024, 700)
    window.move(0, 0)
    window.show()
    window.move(0, 0)
    app.processEvents()
    anchor = window.findChild(QWidget, "libraryDock")
    assert anchor is not None
    dock = QRect(anchor.mapToGlobal(QPoint(0, 0)), anchor.size())
    screen_rect = QRect(0, 0, dock.right() + 1 + free, 900)

    class NarrowScreen:
        def availableGeometry(self) -> QRect:
            return screen_rect

        def geometry(self) -> QRect:
            return screen_rect

    monkeypatch.setattr(window, "screen", lambda: NarrowScreen())
    window._show_tour_step(0)
    app.processEvents()
    card = window.tour_card
    frame = card.frameGeometry()
    if card.isWindow():
        card_rect = frame
    else:
        card_rect = QRect(card.mapToGlobal(QPoint(0, 0)), frame.size())
    anchor_rect = QRect(anchor.mapToGlobal(QPoint(0, 0)), anchor.size())
    return app, window, card, card_rect, anchor_rect, screen_rect


def test_tour_card_narrows_for_a_free_side_under_240(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """200 to 239 px beside the dock still keeps the card off the dock."""

    app, window, card, card_rect, anchor_rect, screen_rect = _library_card_on_narrow_screen(
        monkeypatch, 220
    )
    try:
        assert screen_rect.contains(card_rect)
        assert window.frameGeometry().contains(card_rect)
        assert card_rect.left() > anchor_rect.right()
        assert not card_rect.intersects(anchor_rect)
        assert 180 <= card.width() < 240
    finally:
        window.close()
        app.processEvents()


def test_tour_card_leaves_the_dock_when_nothing_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under the minimum width, the card must not sit on top of its target."""

    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtWidgets import QWidget

    app, window, card, card_rect, anchor_rect, screen_rect = _library_card_on_narrow_screen(
        monkeypatch, 80
    )
    try:
        assert screen_rect.contains(card_rect)
        assert window.frameGeometry().contains(card_rect)
        overlap = card_rect.intersected(anchor_rect)
        overlap_area = 0 if not overlap.isValid() else overlap.width() * overlap.height()
        card_area = card_rect.width() * card_rect.height()
        assert overlap_area < card_area
        covered = QRect(anchor_rect.left(), anchor_rect.top(), card_rect.width(), card_rect.height())
        covered_hit = covered.intersected(anchor_rect)
        covered_area = 0 if not covered_hit.isValid() else covered_hit.width() * covered_hit.height()
        assert overlap_area < covered_area
        sheet = window.findChild(QWidget, "sheetCanvas")
        assert sheet is not None
        sheet_rect = QRect(sheet.mapToGlobal(QPoint(0, 0)), sheet.size())
        assert card_rect.intersects(sheet_rect.intersected(screen_rect))
    finally:
        window.close()
        app.processEvents()


def test_welcome_follows_recovery_and_honors_no_welcome(monkeypatch: pytest.MonkeyPatch) -> None:
    from panelcad.api.session import Session
    from panelcad.guidance import set_welcome_status, welcome_pending
    from panelcad.gui import app as gui_app
    from panelcad.gui.guidance import WelcomeDialog
    from panelcad.gui.main_window import MainWindow

    order: list[str] = []

    def recovery(window, info) -> None:
        order.append("recovery")
        assert window is sentinel
        assert info == "crash"

    monkeypatch.setattr("panelcad.gui.bug_report_dialog.offer_unclean_exit", recovery)

    class Win:
        def offer_welcome(self) -> None:
            order.append("welcome")

    sentinel = Win()
    gui_app.present_startup_prompts(sentinel, "crash")
    assert order == ["recovery", "welcome"]

    order.clear()

    def broken(window, info) -> None:
        order.append("recovery")
        raise RuntimeError("dialog failed")

    monkeypatch.setattr("panelcad.gui.bug_report_dialog.offer_unclean_exit", broken)
    with pytest.raises(RuntimeError, match="dialog failed"):
        gui_app.present_startup_prompts(sentinel, "crash")
    assert order == ["recovery", "welcome"]

    order.clear()
    gui_app.present_startup_prompts(sentinel, None)
    assert order == ["welcome"]

    monkeypatch.setenv("PANELCAD_NO_WELCOME", "1")
    set_welcome_status("pending")
    app = _app()
    window = MainWindow(Session.create("No welcome"))

    def refuse(self) -> int:
        raise AssertionError("welcome opened")

    monkeypatch.setattr(WelcomeDialog, "exec", refuse)
    window.offer_welcome()
    assert welcome_pending() is True
    window.close()
    app.processEvents()


def test_help_and_menus_use_floor_wording() -> None:
    from panelcad.api.session import Session
    from panelcad.guidance import help_topic, search_help
    from panelcad.gui.import_wizard import ImportWizard
    from panelcad.gui.main_window import MainWindow

    _app()
    for topic_id in ("import", "parts", "csv"):
        body = help_topic(topic_id)["body"]
        assert "stay off the BOM" in body
        assert "BOM lists" not in body
    assert "troubleshoot_guide" not in help_topic("troubleshoot")["body"]
    assert "wire number" in help_topic("wires")["keywords"]
    assert "rail" not in help_topic("reconstruct")["body"].lower()
    assert "duct" not in help_topic("reconstruct")["body"].lower()
    assert "4 corners" in help_topic("reconstruct")["body"]
    assert "ai-account" in search_help("ai key")
    assert "Build layout from schematic" in help_topic("layout")["body"]
    assert "Pack from schematic" not in help_topic("layout")["body"]
    assert "undo" in search_help("undo")
    assert "dark-mode" in search_help("dark mode")
    assert "reconstruct" in search_help("reconstruct")
    assert "updates" in search_help("check for updates")
    assert "report-bug" in search_help("report a bug")
    assert "wires" in search_help("wire number")

    window = MainWindow(Session.create("Wording"))
    names = [action.text().replace("&", "") for action in window.menuBar().actions()]
    assert "Settings" not in names
    help_menu = next(action for action in window.menuBar().actions() if action.text().replace("&", "") == "Help")
    help_labels = [action.text().replace("&", "") for action in help_menu.menu().actions()]
    assert "Updates…" in help_labels

    echoes: list[str] = []

    def walk(menu) -> None:
        for action in menu.actions():
            if action.isSeparator():
                continue
            label = action.text().replace("&", "").strip()
            if not label or label == "(none closed)":
                continue
            tip = action.toolTip().split("Learn more:")[0].strip()
            if tip.casefold() == label.casefold():
                echoes.append(label)
            if action.menu() is not None:
                walk(action.menu())

    for action in window.menuBar().actions():
        if action.menu() is not None:
            walk(action.menu())
        label = action.text().replace("&", "").strip()
        tip = action.toolTip().split("Learn more:")[0].strip()
        if label and tip.casefold() == label.casefold():
            echoes.append(label)
    assert echoes == []
    insert = next(action for action in window.menuBar().actions() if action.text().replace("&", "") == "Insert")
    insert_labels = [action.text().replace("&", "") for action in insert.menu().actions()]
    assert "Build layout from schematic" in insert_labels
    assert "Pack from schematic" not in insert_labels
    window.close()

    wizard = ImportWizard()
    assert wizard.workers.isVisibleTo(wizard) is False
    assert wizard.progress.isVisibleTo(wizard) is False
    settings = wizard.vision_settings()
    assert settings.workers == wizard.workers.value()
    assert settings.num_ctx == wizard.num_ctx.value()
    wizard.advanced_toggle.setChecked(True)
    wizard.workers.setValue(5)
    wizard.advanced_toggle.setChecked(False)
    assert wizard.workers.isVisibleTo(wizard) is False
    assert wizard.vision_settings().workers == 5


def test_wire_help_matches_the_rail_tool() -> None:
    import panelcad.core.ladder as ladder

    from panelcad.guidance import ACTION_TIPS, HINTS, help_topic

    assert hasattr(ladder, "hit_rail")
    body = help_topic("wires")["body"]
    assert "L1 and L2" in body
    assert "L+ and 0V" in body
    assert "junction is drawn where you clicked" in body
    assert "A wire number is the number printed on that wire." in body
    assert "Reports → Number wires" in body
    assert ACTION_TIPS["Wire"][0] == "Click a terminal or a ladder rail, then the next one, to draw a wire."
    assert HINTS["wire"]["text"] == (
        "Tip: click a terminal or a ladder rail, then click the next one. Drag the line to route it."
    )


def test_help_topic_ids_are_not_repeated() -> None:
    import ast
    from pathlib import Path

    from panelcad.guidance import HELP_TOPIC_ORDER
    from panelcad.learn import EXTRA_TOPIC_ORDER

    assert len(HELP_TOPIC_ORDER) == len(set(HELP_TOPIC_ORDER))
    assert len(EXTRA_TOPIC_ORDER) == len(set(EXTRA_TOPIC_ORDER))
    tree = ast.parse(Path("src/panelcad/learn.py").read_text(encoding="utf-8"))
    keys: list[str] = []
    for node in tree.body:
        target = None
        value = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
            value = node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
            value = node.value
        if target != "EXTRA_TOPICS" or not isinstance(value, ast.Dict):
            continue
        for key in value.keys:
            assert isinstance(key, ast.Constant) and isinstance(key.value, str)
            keys.append(key.value)
    assert keys
    assert len(keys) == len(set(keys))
    assert "file-lock" not in keys
    assert "ai-account" in keys


def test_every_finding_id_has_a_label() -> None:
    import ast
    from pathlib import Path

    from panelcad.gui.problems import _CHECK_LABELS

    missing: list[str] = []
    for path in Path("src/panelcad").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name != "Finding":
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value not in _CHECK_LABELS:
                missing.append(f"{path}:{node.lineno}:{arg.value}")
    assert missing == []
    assert _CHECK_LABELS["reconstruct_needs_info"] == "Not ready for layout"
    assert _CHECK_LABELS["reconstruct_scale_perspective"] == "Scale"
    assert _CHECK_LABELS["print_unknown_part"] == "Unknown part on the print"
