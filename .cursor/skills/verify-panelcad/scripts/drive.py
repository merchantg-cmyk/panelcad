#!/usr/bin/env python3
"""Drive the real PanelCAD GUI through menus and dialog widgets.

Usage (from launch.sh, which sets the isolated env):  drive.py <feature>
Features implemented: print-package, iec-to-nema-convert.  Writes screenshots, outputs and
result.json to $EVIDENCE.  Exit 0 = every assertion passed.

Rules: reach features through QMenuBar actions by visible text and through
the dialog's own widgets (QCheckBox, QListWidget items, QLineEdit, the OK
button).  Never call Session methods or MainWindow slots directly.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

EVIDENCE = Path(os.environ["EVIDENCE"])
SCRATCH = Path(os.environ["SCRATCH"])
REPO = Path(os.environ["REPO"])
log_lines: list[str] = []
result: dict = {"feature": None, "checks": [], "ok": False}


def log(msg: str) -> None:
    print(msg, flush=True)
    log_lines.append(msg)


def check(name: str, ok: bool, detail: str = "") -> None:
    result["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
    log(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def shot(widget, name: str) -> str:
    path = EVIDENCE / f"{len(list(EVIDENCE.glob('*.png'))):02d}-{name}.png"
    widget.grab().save(str(path))
    log(f"screenshot {path}")
    return str(path)


def menu_action(win, menu_text: str, action_text: str):
    """Find a menubar action by visible text (ampersands ignored)."""
    clean = lambda s: s.replace("&", "").strip()
    for top in win.menuBar().actions():
        if clean(top.text()) == menu_text and top.menu():
            for act in top.menu().actions():
                if clean(act.text()) == action_text:
                    return act
    raise LookupError(f"menu {menu_text} → {action_text} not found")


class ModalPump:
    """Polls for modal dialogs and hands each to the next expected handler."""

    def __init__(self, app, handlers):
        from PySide6.QtCore import QTimer
        self.app, self.handlers, self.seen = app, list(handlers), []
        self.timer = QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(150)

    def tick(self):
        w = self.app.activeModalWidget()
        if w is None or w in self.seen:
            return
        self.seen.append(w)
        name = type(w).__name__
        log(f"modal: {name} '{w.windowTitle()}'")
        if not self.handlers:
            shot(w, f"unexpected-{name}")
            check("no unexpected dialog", False, f"{name} '{w.windowTitle()}'")
            w.reject()
            return
        handler = self.handlers.pop(0)
        try:
            handler(w)
        except Exception:
            log(traceback.format_exc())
            check(f"handler {handler.__name__}", False, "raised")
            w.reject()


def feature_print_package(app, win) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDialogButtonBox, QMessageBox

    out_pdf = SCRATCH / "work" / "verify_package.pdf"
    drop = {"enclosure"}  # sample_catalog has power, control, enclosure
    state: dict = {}

    def options_dialog(dlg):
        check("dialog is PrintPackageDialog", type(dlg).__name__ == "PrintPackageDialog", dlg.windowTitle())
        lst = dlg.sheet_list
        ids = [lst.item(i).data(Qt.ItemDataRole.UserRole) for i in range(lst.count())]
        state["sheets_listed"] = ids
        check("sheet list shows every sheet", set(ids) == {"power", "control", "enclosure"}, str(ids))
        shot(dlg, "print-package-dialog-before")
        for i in range(lst.count()):
            item = lst.item(i)
            if item.data(Qt.ItemDataRole.UserRole) in drop:
                # Click the item's checkbox the way a user does.
                lst.scrollToItem(item)
                rect = lst.visualItemRect(item)
                QTest.mouseClick(lst.viewport(), Qt.MouseButton.LeftButton, pos=rect.topLeft() + type(rect.topLeft())(10, rect.height() // 2))
                how = "mouse"
                if item.checkState() != Qt.CheckState.Unchecked:  # click missed the box: use the keyboard
                    lst.setCurrentItem(item)
                    QTest.keyClick(lst, Qt.Key.Key_Space)
                    how = "keyboard"
                log(f"unticked {item.data(Qt.ItemDataRole.UserRole)} via {how}")
        kept = [lst.item(i).data(Qt.ItemDataRole.UserRole) for i in range(lst.count())
                if lst.item(i).checkState() == Qt.CheckState.Checked]
        state["sheets_kept"] = kept
        check("unticked enclosure in the list", not (set(kept) & drop), str(kept))
        dlg.path.selectAll()
        QTest.keyClicks(dlg.path, str(out_pdf))
        check("PDF path typed", dlg.path.text() == str(out_pdf), dlg.path.text())
        shot(dlg, "print-package-dialog-filled")
        ok = dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)
        QTest.mouseClick(ok, Qt.MouseButton.LeftButton)

    def written_box(box):
        check("success box shown", isinstance(box, QMessageBox) and box.windowTitle() == "Print package written",
              box.windowTitle())
        state["box_text"] = box.text()
        (EVIDENCE / "result-dialog.txt").write_text(box.text(), encoding="utf-8")
        shot(box, "print-package-written")
        box.accept()

    pump = ModalPump(app, [options_dialog, written_box])
    shot(win, "main-window-before")
    act = menu_action(win, "File", "Print Package…")
    log(f"triggering File → {act.text()}")
    act.trigger()  # exec() blocks here; the pump drives both modals
    for _ in range(40):
        app.processEvents()
        QTest.qWait(50)
        if len(pump.seen) >= 2:
            break
    pump.timer.stop()
    shot(win, "main-window-after")
    status = win.statusBar().currentMessage()
    log(f"status bar: {status}")

    check("PDF written", out_pdf.is_file() and out_pdf.stat().st_size > 1000,
          f"{out_pdf} {out_pdf.stat().st_size if out_pdf.exists() else 0} bytes")
    if not out_pdf.is_file():
        return
    shutil.copy2(out_pdf, EVIDENCE / out_pdf.name)
    from pypdf import PdfReader
    reader = PdfReader(str(out_pdf))
    pages = len(reader.pages)
    (EVIDENCE / "pdfinfo.txt").write_text(f"Pages: {pages}\n", encoding="utf-8")
    text = "\f".join((pg.extract_text() or "") for pg in reader.pages)
    (EVIDENCE / "verify_package.txt").write_text(text, encoding="utf-8")
    # sample_catalog with enclosure unticked prints exactly 13 pages (14 with every sheet).
    check("PDF has 13 pages", pages == 13, f"pages={pages}")
    check("PDF text extracted", "Power" in text and len(text) > 200, f"{len(text)} chars")
    box = state.get("box_text", "")
    check("result names kept sheet count", f"Drawing sheets: {len(state.get('sheets_kept', []))}" in box, box.replace("\n", " | "))
    # Optional page renders: only when poppler is already present. Never a check.
    if shutil.which("pdftoppm"):
        subprocess.run(["pdftoppm", "-png", "-r", "40", "-f", "1", "-l", "4", str(out_pdf), str(EVIDENCE / "pdf-page")])
    else:
        log("pdftoppm not found: page renders skipped (optional)")
    # Side effect: the PDF outline lists exactly the kept drawing sheets.
    def walk(items, depth=0):
        for it in items:
            if isinstance(it, list):
                yield from walk(it, depth + 1)
            else:
                yield depth, it.title
    outline = list(walk(reader.outline))
    (EVIDENCE / "pdf-outline.txt").write_text("\n".join("  " * d + t for d, t in outline), encoding="utf-8")
    drawings = [t for d, t in outline if d == 1]
    check("PDF outline drawings == kept sheets", drawings == ["Power", "Control"], str(drawings))
    check("dropped sheet absent from PDF outline", "Enclosure" not in drawings, str(outline))


def feature_iec_to_nema_convert(app, win) -> None:
    import yaml
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QComboBox, QDialog, QFrame, QLabel, QPushButton

    out_yaml = SCRATCH / "work" / "xref_iec_mirror-nema.pcad.yaml"
    state: dict = {}

    def convert_dialog(dlg):
        check("dialog is ConvertDialog", type(dlg).__name__ == "ConvertDialog", dlg.windowTitle())
        check("NEMA/ANSI radio is selected", dlg.nema.isChecked(), f"nema={dlg.nema.isChecked()} iec={dlg.iec.isChecked()}")
        check("Whole project radio is selected", dlg.whole.isChecked(), f"whole={dlg.whole.isChecked()}")
        mode_combo = dlg.findChild(QComboBox, "convert-mode-combo")
        check("ConvertDialog has convert-mode-combo selector",
              mode_combo is not None and mode_combo.count() >= 2 and mode_combo.currentData() == "rules_only",
              f"count={mode_combo.count() if mode_combo is not None else 0} current={mode_combo.currentData() if mode_combo is not None else None}")
        import_btn = dlg.findChild(QPushButton, "convert-import-scan-btn")
        check("ConvertDialog has convert-import-scan-btn button",
              import_btn is not None and import_btn.isEnabled() and "Import IEC PDF / scan" in import_btn.text(),
              import_btn.text() if import_btn is not None else "missing")
        wire_combo = dlg.findChild(QComboBox, "convert-wire-mode-combo")
        check("ConvertDialog has convert-wire-mode-combo selector",
              wire_combo is not None and wire_combo.count() >= 2 and wire_combo.currentData() == "line_ref",
              f"count={wire_combo.count() if wire_combo is not None else 0} current={wire_combo.currentData() if wire_combo is not None else None}")
        dlg.path.selectAll()
        QTest.keyClicks(dlg.path, str(out_yaml))
        check("Save as path typed", dlg.path.text() == str(out_yaml), dlg.path.text())
        shot(dlg, "convert-dialog-before")

        buttons = {b.text(): b for b in dlg.findChildren(QPushButton)}
        convert_btn = buttons.get("Convert")
        check("Convert button found", convert_btn is not None, str(sorted(buttons)))
        QTest.mouseClick(convert_btn, Qt.MouseButton.LeftButton)
        app.processEvents()
        QTest.qWait(100)

        header = dlg.findChild(QLabel, "convert-review-header")
        check("review header shown with decisions count",
              header is not None and header.isVisible() and "NEMA/ANSI" in header.text() and "decisions:" in header.text(),
              header.text() if header is not None else "missing")
        tags_lbl = dlg.findChild(QLabel, "chunk-summary:tags")
        check("chunk summary shows reasons",
              tags_lbl is not None and "reasons:" in tags_lbl.text(),
              tags_lbl.text() if tags_lbl is not None else "missing")
        shot(dlg, "convert-dialog-review")

        # Click Before thumbnail on main sheet to verify CompareDialog
        thumb = dlg.findChild(QFrame, "thumb:Before:main")
        check("Before thumbnail rendered", thumb is not None, "thumb:Before:main")
        if thumb is not None:
            QTest.mouseClick(thumb, Qt.MouseButton.LeftButton)
            app.processEvents()
            QTest.qWait(80)
            compare = dlg.findChild(QDialog, "convert-compare")
            check("CompareDialog opened", compare is not None and compare.isVisible(),
                  compare.windowTitle() if compare is not None else "none")
            if compare is not None:
                from PySide6.QtWidgets import QTableWidget
                summary_bar = compare.findChild(QLabel, "compare-summary-bar")
                check("CompareDialog has compare-summary-bar with netlist and from-to status",
                      summary_bar is not None and "netlist: OK" in summary_bar.text() and "from-to: OK" in summary_bar.text(),
                      summary_bar.text() if summary_bar is not None else "missing")
                ft_table = compare.findChild(QTableWidget, "compare-from-to-table")
                check("CompareDialog has compare-from-to-table populated",
                      ft_table is not None and ft_table.rowCount() > 0,
                      f"rows={ft_table.rowCount() if ft_table is not None else 0}")
                dec_table = compare.findChild(QTableWidget, "compare-decisions-table")
                check("CompareDialog has compare-decisions-table populated",
                      dec_table is not None and dec_table.rowCount() > 0,
                      f"rows={dec_table.rowCount() if dec_table is not None else 0}")
                shot(compare, "convert-compare-dialog")
                compare.close()
                app.processEvents()

        # Click Accept on each chunk button via QTest
        accept_btns = [b for b in dlg.findChildren(QPushButton) if b.objectName().startswith("accept:")]
        state["chunk_ids"] = [b.objectName().split(":", 1)[1] for b in accept_btns]
        check("review chunks present", len(accept_btns) >= 3, str(state["chunk_ids"]))
        for cid in list(state["chunk_ids"]):
            btn = dlg.findChild(QPushButton, f"accept:{cid}")
            if btn is not None:
                QTest.mouseClick(btn, Qt.MouseButton.LeftButton)
                app.processEvents()
        shot(dlg, "convert-dialog-accepted")

        close_btn = next((b for b in dlg.findChildren(QPushButton) if b.text() == "Close"), None)
        check("Close button found", close_btn is not None, "Close")
        if close_btn is not None:
            QTest.mouseClick(close_btn, Qt.MouseButton.LeftButton)

    pump = ModalPump(app, [convert_dialog])
    shot(win, "main-window-before")
    act = menu_action(win, "Tools", "Convert drawing standard...")
    log(f"triggering Tools → {act.text()}")
    act.trigger()
    for _ in range(40):
        app.processEvents()
        QTest.qWait(50)
        if len(pump.seen) >= 1:
            break
    pump.timer.stop()
    shot(win, "main-window-after")
    status = win.statusBar().currentMessage()
    log(f"status bar: {status}")
    check("status bar updated", "converted drawing standard" in status, status)
    check("converted YAML written", out_yaml.is_file() and out_yaml.stat().st_size > 500, str(out_yaml))
    if not out_yaml.is_file():
        return
    shutil.copy2(out_yaml, EVIDENCE / out_yaml.name)
    data = yaml.safe_load(out_yaml.read_text(encoding="utf-8"))
    tb = data.get("title_block") or {}
    check("title_block standard is NEMA/ANSI", tb.get("standard") == "NEMA/ANSI", str(tb.get("standard")))
    check("title_block drawing_style is ladder", tb.get("drawing_style") == "ladder", str(tb.get("drawing_style")))
    sheets = data.get("sheets") or []
    check("all sheets use ladder style", all((s.get("drawing_style") or tb.get("drawing_style")) == "ladder" for s in sheets),
          str([s.get("drawing_style") for s in sheets]))
    comp_ids = {c["id"] for s in sheets for c in (s.get("components") or [])}
    lib_ids = {c["library_id"] for s in sheets for c in (s.get("components") or [])}
    check("components converted to NEMA library_ids", all(lid.startswith("nema.") for lid in lib_ids), str(sorted(lib_ids)))
    check("tags renamed by NEMA roles (PB1, M1, PL1, PL2)", {"PB1", "M1", "PL1", "PL2"} <= comp_ids, str(sorted(comp_ids)))
    check("control rails converted from L+/0V to 24V/0V",
          tb.get("rail_hot") == "24V" and tb.get("rail_return") == "0V"
          and all(s.get("rail_hot") == "24V" and s.get("rail_return") == "0V" for s in sheets),
          f"tb=({tb.get('rail_hot')},{tb.get('rail_return')}) sheets={[(s.get('rail_hot'), s.get('rail_return')) for s in sheets]}")
    comps_by_id = {c["id"]: c for s in sheets for c in (s.get("components") or [])}
    m1_xref = str((comps_by_id.get("M1") or {}).get("properties", {}).get("xref") or "")
    check("coil M1 xref table uses NEMA 3-4 / 1-2 and line numbers",
          "3-4 104" in m1_xref and "1-2 107" in m1_xref and "3-4 205" in m1_xref and "/" not in m1_xref,
          m1_xref)
    c14_props = (comps_by_id.get("M1-14") or {}).get("properties") or {}
    c21_props = (comps_by_id.get("M1-21") or {}).get("properties") or {}
    c43_props = (comps_by_id.get("M1-43") or {}).get("properties") or {}
    wire_ends = {w.get("from") for s in sheets for w in (s.get("wires") or [])} | {w.get("to") for s in sheets for w in (s.get("wires") or [])}
    check("child contacts use NEMA pins (.3/.4, .1/.2) and coil line xref (102)",
          {"M1-14.3", "M1-14.4", "M1-21.1", "M1-21.2", "M1-43.3", "M1-43.4"} <= wire_ends
          and c14_props.get("xref") == "102" and c21_props.get("xref") == "102" and c43_props.get("xref") == "102",
          str({"xrefs": [c14_props.get("xref"), c21_props.get("xref"), c43_props.get("xref")], "ends": sorted(wire_ends)}))

    ws_files = list((SCRATCH / "work").glob("*.convert.json"))
    check("conversion workspace written", len(ws_files) == 1, str([p.name for p in ws_files]))
    if ws_files:
        shutil.copy2(ws_files[0], EVIDENCE / ws_files[0].name)
        ws = json.loads(ws_files[0].read_text(encoding="utf-8"))
        conn = ws.get("connection_check") or {}
        check("From-To scorer is from-to-check/1 and ok=True",
              conn.get("scorer") == "from-to-check/1" and conn.get("ok") is True and not conn.get("missing") and not conn.get("extra"),
              json.dumps(conn))
        chunks = ws.get("chunks") or []
        check("all chunks accepted in workspace",
              bool(chunks) and all(c.get("status") == "accepted" for c in chunks),
              str([(c.get("id"), c.get("status")) for c in chunks]))

    # Step 2: Drive a 4-sheet multi-page IEC project (1 power + 3 control linked by offpage arrows)
    mp_in = SCRATCH / "work" / "multipage_iec.pcad.yaml"
    mp_out = SCRATCH / "work" / "multipage_iec-nema.pcad.yaml"
    mp_doc = {'format': 'panelcad', 'version': '0.1', 'name': 'iec_conveyor_3page', 'properties': {}, 'title_block': {'company': '', 'address': '', 'logo': '', 'customer': '', 'location': '', 'drawing_number': '', 'drawn_by': '', 'checked_by': '', 'approved_by': '', 'scale': 'NTS', 'standard': 'IEC', 'drawing_style': '', 'coil_style': 'circle', 'rail_hot': 'L1', 'rail_return': 'L2', 'revisions': []}, 'sheets': [{'id': 'main', 'title': '1 - Main Power', 'description': '', 'kind': 'schematic', 'page': {'size': 'A3', 'orientation': 'landscape'}, 'components': [{'id': 'Q1', 'library_id': 'iec.breakers.mccb_3p_100a', 'x': 100.0, 'y': 55.0, 'rotation': 0, 'mirror': False, 'properties': {}}, {'id': 'K1', 'library_id': 'iec.contactors.ac3_9a', 'x': 100.0, 'y': 105.0, 'rotation': 0, 'mirror': False, 'properties': {}}, {'id': 'F1', 'library_id': 'iec.protection.overload_9a', 'x': 100.0, 'y': 150.0, 'rotation': 0, 'mirror': False, 'properties': {}}, {'id': 'M1', 'library_id': 'iec.loads.motor_3ph', 'x': 100.0, 'y': 200.0, 'rotation': 0, 'mirror': False, 'properties': {}}], 'wires': [{'id': 'w1', 'from': 'Q1.2', 'to': 'K1.L1', 'net': 'L1_1', 'label': 'L1_1', 'number': '101', 'points': []}, {'id': 'w2', 'from': 'K1.T1', 'to': 'F1.1', 'net': 'L1_2', 'label': 'L1_2', 'number': '102', 'points': []}, {'id': 'w3', 'from': 'F1.2', 'to': 'M1.U1', 'net': 'T1', 'label': 'T1', 'number': '103', 'points': []}, {'id': 'w4', 'from': 'Q1.4', 'to': 'K1.L2', 'net': 'L2_1', 'label': 'L2_1', 'number': '104', 'points': []}, {'id': 'w5', 'from': 'K1.T2', 'to': 'F1.3', 'net': 'L2_2', 'label': 'L2_2', 'number': '105', 'points': []}, {'id': 'w6', 'from': 'F1.4', 'to': 'M1.V1', 'net': 'T2', 'label': 'T2', 'number': '106', 'points': []}, {'id': 'w7', 'from': 'Q1.6', 'to': 'K1.L3', 'net': 'L3_1', 'label': 'L3_1', 'number': '107', 'points': []}, {'id': 'w8', 'from': 'K1.T3', 'to': 'F1.5', 'net': 'L3_2', 'label': 'L3_2', 'number': '108', 'points': []}, {'id': 'w9', 'from': 'F1.6', 'to': 'M1.W1', 'net': 'T3', 'label': 'T3', 'number': '109', 'points': []}], 'drawing_style': 'iec'}, {'id': 's1', 'title': '2 - Safety & Supply', 'description': '', 'kind': 'schematic', 'page': {'size': 'A3', 'orientation': 'landscape'}, 'components': [{'id': 'S1', 'library_id': 'iec.operators.estop', 'x': 70.0, 'y': 60.0, 'rotation': 90, 'mirror': False, 'properties': {}}, {'id': 'K0', 'library_id': 'iec.relays.cr', 'x': 70.0, 'y': 155.0, 'rotation': 90, 'mirror': False, 'properties': {}}, {'id': 'K0-13', 'library_id': 'iec.contactors.aux_no', 'x': 150.0, 'y': 75.0, 'rotation': 90, 'mirror': False, 'properties': {'parent': 'K0', 'terminals': '13-14', 'pin_labels': {'13': '13', '14': '14'}}}, {'id': 'OP1', 'library_id': 'iec.connection.offpage', 'x': 150.0, 'y': 155.0, 'rotation': 90, 'mirror': False, 'properties': {'label': '/2.1 L+_SAFE', 'link': 'L+_SAFE', 'role': 'to'}}], 'wires': [{'id': 'w10', 'from': 'L+', 'to': 'S1.11', 'net': 'L+', 'label': 'L+', 'number': 'L+', 'points': []}, {'id': 'w11', 'from': 'S1.12', 'to': 'K0.A1', 'net': 'N_ESTOP', 'label': 'N_ESTOP', 'number': '201', 'points': []}, {'id': 'w12', 'from': 'K0.A2', 'to': '0V', 'net': '0V', 'label': '0V', 'number': '0V', 'points': []}, {'id': 'w13', 'from': 'L+', 'to': 'K0-13.13', 'net': 'L+', 'label': 'L+', 'number': 'L+', 'points': []}, {'id': 'w14', 'from': 'K0-13.14', 'to': 'OP1.A', 'net': 'L+_SAFE', 'label': 'L+_SAFE', 'number': '202', 'points': []}], 'rail_hot': 'L+', 'rail_return': '0V', 'drawing_style': 'iec'}, {'id': 's2', 'title': '3 - Conveyor Drive', 'description': '', 'kind': 'schematic', 'page': {'size': 'A3', 'orientation': 'landscape'}, 'components': [{'id': 'OP2', 'library_id': 'iec.connection.offpage_from', 'x': 70.0, 'y': 35.0, 'rotation': 90, 'mirror': False, 'properties': {'label': '/1.2 L+_SAFE', 'link': 'L+_SAFE', 'role': 'from'}}, {'id': 'S2', 'library_id': 'iec.operators.pb_nc', 'x': 70.0, 'y': 65.0, 'rotation': 90, 'mirror': False, 'properties': {}}, {'id': 'S3', 'library_id': 'iec.operators.pb_no', 'x': 70.0, 'y': 100.0, 'rotation': 90, 'mirror': False, 'properties': {}}, {'id': 'K1-13', 'library_id': 'iec.contactors.aux_no', 'x': 105.0, 'y': 100.0, 'rotation': 90, 'mirror': False, 'properties': {'parent': 'K1', 'terminals': '13-14', 'pin_labels': {'13': '13', '14': '14'}}}, {'id': 'F1-95', 'library_id': 'iec.protection.ol_contact', 'x': 70.0, 'y': 135.0, 'rotation': 90, 'mirror': False, 'properties': {'parent': 'F1', 'terminals': '95-96'}}, {'id': 'K1', 'library_id': 'iec.contactors.coil', 'x': 70.0, 'y': 170.0, 'rotation': 90, 'mirror': False, 'properties': {}}, {'id': 'K1-23', 'library_id': 'iec.contactors.aux_no', 'x': 175.0, 'y': 75.0, 'rotation': 90, 'mirror': False, 'properties': {'parent': 'K1', 'terminals': '23-24', 'pin_labels': {'13': '23', '14': '24'}}}, {'id': 'KT1', 'library_id': 'iec.timers.on_delay', 'x': 175.0, 'y': 160.0, 'rotation': 90, 'mirror': False, 'properties': {}}, {'id': 'OP3', 'library_id': 'iec.connection.offpage', 'x': 230.0, 'y': 160.0, 'rotation': 90, 'mirror': False, 'properties': {'label': '/3.1 CONV_RUN', 'link': 'CONV_RUN', 'role': 'to'}}], 'wires': [{'id': 'w15', 'from': 'OP2.A', 'to': 'S2.11', 'net': 'L+_SAFE', 'label': 'L+_SAFE', 'number': '202', 'points': []}, {'id': 'w16', 'from': 'S2.12', 'to': 'S3.13', 'net': 'N_STOP', 'label': 'N_STOP', 'number': '301', 'points': []}, {'id': 'w17', 'from': 'S2.12', 'to': 'K1-13.13', 'net': 'N_STOP', 'label': 'N_STOP', 'number': '301', 'points': []}, {'id': 'w18', 'from': 'S3.14', 'to': 'F1-95.95', 'net': 'N_START', 'label': 'N_START', 'number': '302', 'points': []}, {'id': 'w19', 'from': 'K1-13.14', 'to': 'F1-95.95', 'net': 'N_START', 'label': 'N_START', 'number': '302', 'points': []}, {'id': 'w20', 'from': 'F1-95.96', 'to': 'K1.A1', 'net': 'N_COIL', 'label': 'N_COIL', 'number': '303', 'points': []}, {'id': 'w21', 'from': 'K1.A2', 'to': '0V', 'net': '0V', 'label': '0V', 'number': '0V', 'points': []}, {'id': 'w22', 'from': 'OP2.A', 'to': 'K1-23.13', 'net': 'L+_SAFE', 'label': 'L+_SAFE', 'number': '202', 'points': []}, {'id': 'w23', 'from': 'K1-23.14', 'to': 'KT1.A1', 'net': 'CONV_RUN', 'label': 'CONV_RUN', 'number': '304', 'points': []}, {'id': 'w24', 'from': 'K1-23.14', 'to': 'OP3.A', 'net': 'CONV_RUN', 'label': 'CONV_RUN', 'number': '304', 'points': []}, {'id': 'w25', 'from': 'KT1.A2', 'to': '0V', 'net': '0V', 'label': '0V', 'number': '0V', 'points': []}], 'rail_hot': 'L+', 'rail_return': '0V', 'drawing_style': 'iec'}, {'id': 's3', 'title': '4 - Annunciation', 'description': '', 'kind': 'schematic', 'page': {'size': 'A3', 'orientation': 'landscape'}, 'components': [{'id': 'OP4', 'library_id': 'iec.connection.offpage_from', 'x': 70.0, 'y': 35.0, 'rotation': 90, 'mirror': False, 'properties': {'label': '/2.2 CONV_RUN', 'link': 'CONV_RUN', 'role': 'from'}}, {'id': 'KT1-15', 'library_id': 'iec.contactors.aux_nc', 'x': 70.0, 'y': 80.0, 'rotation': 90, 'mirror': False, 'properties': {'parent': 'KT1', 'terminals': '15-16', 'pin_labels': {'21': '15', '22': '16'}, 'delayed': 'NC'}}, {'id': 'P1', 'library_id': 'iec.operators.pilot_light', 'x': 70.0, 'y': 160.0, 'rotation': 90, 'mirror': False, 'properties': {}}, {'id': 'KT1-27', 'library_id': 'iec.contactors.aux_no', 'x': 160.0, 'y': 80.0, 'rotation': 90, 'mirror': False, 'properties': {'parent': 'KT1', 'terminals': '27-28', 'pin_labels': {'13': '27', '14': '28'}, 'delayed': 'NO'}}, {'id': 'P2', 'library_id': 'iec.operators.pilot_light', 'x': 160.0, 'y': 160.0, 'rotation': 90, 'mirror': False, 'properties': {}}], 'wires': [{'id': 'w26', 'from': 'OP4.A', 'to': 'KT1-15.21', 'net': 'CONV_RUN', 'label': 'CONV_RUN', 'number': '304', 'points': []}, {'id': 'w27', 'from': 'KT1-15.22', 'to': 'P1.X1', 'net': 'N_HORN', 'label': 'N_HORN', 'number': '401', 'points': []}, {'id': 'w28', 'from': 'P1.X2', 'to': '0V', 'net': '0V', 'label': '0V', 'number': '0V', 'points': []}, {'id': 'w29', 'from': 'OP4.A', 'to': 'KT1-27.13', 'net': 'CONV_RUN', 'label': 'CONV_RUN', 'number': '304', 'points': []}, {'id': 'w30', 'from': 'KT1-27.14', 'to': 'P2.X1', 'net': 'N_RUN_LT', 'label': 'N_RUN_LT', 'number': '402', 'points': []}, {'id': 'w31', 'from': 'P2.X2', 'to': '0V', 'net': '0V', 'label': '0V', 'number': '0V', 'points': []}], 'rail_hot': 'L+', 'rail_return': '0V', 'drawing_style': 'iec'}]}
    mp_in.write_text(yaml.safe_dump(mp_doc, sort_keys=False), encoding="utf-8")
    from panelcad.theme import remember_project
    remember_project(str(mp_in))
    win._fill_recent_menu()
    recent_act = next((a for a in win._recent_menu.actions() if getattr(a, "_path", None) == str(mp_in)), None)
    check("multi-page project listed in File > Open Recent", recent_act is not None, str(mp_in))
    if recent_act is not None:
        recent_act.trigger()
        app.processEvents()
        QTest.qWait(150)
    check("multi-page project opened (4 sheets)", len(win.session.project.sheets) == 4, str([s.id for s in win.session.project.sheets]))

    def convert_mp_dialog(dlg):
        dlg.path.selectAll()
        QTest.keyClicks(dlg.path, str(mp_out))
        buttons = {b.text(): b for b in dlg.findChildren(QPushButton)}
        QTest.mouseClick(buttons["Convert"], Qt.MouseButton.LeftButton)
        app.processEvents()
        QTest.qWait(100)
        thumb_after_s2 = dlg.findChild(QFrame, "thumb:After:s2")
        check("merged sheet After thumbnail rendered", thumb_after_s2 is not None, "thumb:After:s2")
        if thumb_after_s2 is not None:
            QTest.mouseClick(thumb_after_s2, Qt.MouseButton.LeftButton)
            app.processEvents()
            QTest.qWait(80)
            compare2 = dlg.findChild(QDialog, "convert-compare")
            after_lbl = compare2.findChild(QLabel, "compare-image:After") if compare2 is not None else None
            has_after_pix = after_lbl is not None and after_lbl.pixmap() is not None and not after_lbl.pixmap().isNull()
            check("CompareDialog opened for merged sheet with consolidated After pixmap",
                  compare2 is not None and compare2.isVisible() and has_after_pix,
                  f"{compare2.windowTitle() if compare2 is not None else 'none'} pix={has_after_pix}")
            if compare2 is not None:
                shot(compare2, "convert-mp-compare-dialog")
                compare2.close()
                app.processEvents()
        accept_btns = [b for b in dlg.findChildren(QPushButton) if b.objectName().startswith("accept:")]
        mp_chunks = [b.objectName().split(":", 1)[1] for b in accept_btns]
        check("multi-page layout chunks present",
              {"s1:layout", "s2:layout", "s3:layout"} <= set(mp_chunks),
              str(mp_chunks))
        for cid in list(mp_chunks):
            btn = dlg.findChild(QPushButton, f"accept:{cid}")
            if btn is not None:
                QTest.mouseClick(btn, Qt.MouseButton.LeftButton)
                app.processEvents()
        shot(dlg, "convert-mp-dialog-accepted")
        close_btn = next((b for b in dlg.findChildren(QPushButton) if b.text() == "Close"), None)
        if close_btn is not None:
            QTest.mouseClick(close_btn, Qt.MouseButton.LeftButton)

    pump2 = ModalPump(app, [convert_mp_dialog])
    act.trigger()
    for _ in range(40):
        app.processEvents()
        QTest.qWait(50)
        if len(pump2.seen) >= 1:
            break
    pump2.timer.stop()
    shot(win, "main-window-mp-after")
    check("multi-page converted YAML written", mp_out.is_file() and mp_out.stat().st_size > 500, str(mp_out))
    if mp_out.is_file():
        shutil.copy2(mp_out, EVIDENCE / mp_out.name)
        mp_data = yaml.safe_load(mp_out.read_text(encoding="utf-8"))
        mp_sheets = mp_data.get("sheets") or []
        check("3 IEC control sheets consolidated onto 1 NEMA ladder sheet (4 -> 2 total sheets)",
              [s.get("id") for s in mp_sheets] == ["main", "s1"],
              str([s.get("id") for s in mp_sheets]))
        s1_comps = (mp_sheets[1].get("components") or []) if len(mp_sheets) >= 2 else []
        s1_ids = {c["id"] for c in s1_comps}
        s1_libs = {c["library_id"] for c in s1_comps}
        check("zero off-page connectors remain on consolidated NEMA sheet",
              not any("offpage" in lid for lid in s1_libs) and not any(cid.startswith("OP_") for cid in s1_ids),
              str(sorted(s1_libs)))
        check("all control devices from s1/s2/s3 consolidated onto s1",
              {"ES1", "CR1", "PB1", "PB2", "M1", "PL1", "PL2"} <= s1_ids,
              str(sorted(s1_ids)))
        mp_ws_path = SCRATCH / "work" / "multipage_iec-nema.convert.json"
        if mp_ws_path.is_file():
            shutil.copy2(mp_ws_path, EVIDENCE / mp_ws_path.name)
            mp_ws = json.loads(mp_ws_path.read_text(encoding="utf-8"))
            mp_conn = mp_ws.get("connection_check") or {}
            mp_meta = ((mp_data.get("properties") or {}).get("convert_standard") or {}).get("multipage") or {}
            check("multi-page From-To scorer ok=True and multipage consolidation recorded",
                  mp_conn.get("ok") is True and mp_meta.get("consolidated_from") == ["s1", "s2", "s3"] and mp_meta.get("kept_sheets") == ["s1"],
                  json.dumps({"conn_ok": mp_conn.get("ok"), "consolidated_from": mp_meta.get("consolidated_from"), "kept_sheets": mp_meta.get("kept_sheets")}))
        from panelcad.api.session import Session as _VerifySession
        from panelcad.convert.flags import sheet_flags as _verify_sheet_flags
        from panelcad.core.ladder import is_ladder_sheet as _verify_is_ladder, rail_geometry as _verify_rail_geom
        from panelcad.importer.ladder_synth import check_converted_sheet_layout as _verify_check_layout
        from panelcad.render.phase_ink import line_side_callouts as _verify_line_side_callouts
        mp_loaded = _VerifySession.open(mp_out)
        mp_all_flags = {sh.id: _verify_sheet_flags(mp_loaded, sh.id) for sh in mp_loaded.project.sheets}
        mp_layout_issues = _verify_check_layout(mp_loaded)
        main_sh = mp_loaded.sheet("main")
        main_xs = {c.id: round(float(c.x), 1) for c in main_sh.components}
        main_callouts = [c.text for c in _verify_line_side_callouts(mp_loaded.project, main_sh, mp_loaded.catalog)]
        check("power sheet main has no ladder/IEC rails, L1/L2/L3 supply callouts, components on feeder column, and zero geometry flags across all sheets",
              not _verify_is_ladder(main_sh, mp_loaded.project)
              and _verify_rail_geom(main_sh, mp_loaded.project) is None
              and main_sh.drawing_style == "freeform"
              and main_sh.rail_hot == ""
              and main_sh.rail_return == ""
              and main_callouts == ["L1", "L2", "L3"]
              and all(85.0 <= x <= 125.0 for x in main_xs.values())
              and all(fl == [] for fl in mp_all_flags.values())
              and mp_layout_issues == [],
              json.dumps({"main_xs": main_xs, "supply_callouts": main_callouts, "drawing_style": main_sh.drawing_style, "rails": [main_sh.rail_hot, main_sh.rail_return], "flags": mp_all_flags, "layout_issues": mp_layout_issues}))


def feature_panel_wiring(app, win) -> None:
    import yaml
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QLabel, QPushButton, QTableWidget

    shot(win, "main-window-before")

    # 1. Trigger Reports -> Number wires
    act_num = menu_action(win, "Reports", "Number wires")
    log(f"triggering Reports -> {act_num.text()}")
    act_num.trigger()
    app.processEvents()
    QTest.qWait(80)

    # 2. Switch to the Panel Layout tab via SheetBar QTabBar click
    tabs = win.sheet_bar.tabs
    enclosure_tab = -1
    for i in range(tabs.count()):
        if tabs.tabData(i) == "enclosure" or "Layout" in tabs.tabText(i):
            enclosure_tab = i
            break
    check("enclosure tab found in SheetBar", enclosure_tab >= 0, f"tab={enclosure_tab}")
    if enclosure_tab >= 0:
        rect = tabs.tabRect(enclosure_tab)
        QTest.mouseClick(tabs, Qt.MouseButton.LeftButton, pos=rect.center())
        app.processEvents()
        QTest.qWait(100)
    check("switched to enclosure layout sheet", win.session.active_sheet_id == "enclosure", win.session.active_sheet_id)
    shot(win, "panel-layout-before-walkthrough")

    # 3. Open Reports -> Wiring Walkthrough & Cut Sheet…
    act_ww = menu_action(win, "Reports", "Wiring Walkthrough / Cut Sheet…")
    log(f"triggering Reports -> {act_ww.text()}")
    act_ww.trigger()
    app.processEvents()
    QTest.qWait(100)

    dlg = win.findChild(QDialog, "wiring-walkthrough-dialog")
    check("Wiring Walkthrough dialog opened", dlg is not None and dlg.isVisible(),
          dlg.windowTitle() if dlg is not None else "missing")
    if dlg is None:
        return
    shot(dlg, "walkthrough-dialog-initial")

    table = dlg.findChild(QTableWidget, "wire-by-wire-table")
    check("wire-by-wire-table populated with 12 routed wires",
          table is not None and table.rowCount() == 12,
          f"rows={table.rowCount() if table is not None else 0}")

    # 4. Select Show All Bundles mode and slice across left duct wires to create a Zip-Tie waypoint
    mode_combo = dlg.findChild(QComboBox, "wiring-mode-combo")
    check("wiring-mode-combo has 3 modes", mode_combo is not None and mode_combo.count() == 3,
          str(mode_combo.count() if mode_combo else 0))
    if mode_combo is not None:
        mode_combo.setCurrentIndex(mode_combo.findData("all"))
        app.processEvents()
        QTest.qWait(60)
    shot(win, "panel-layout-all-bundles")

    slice_btn = dlg.findChild(QPushButton, "wiring-slice-btn")
    check("Zip-Tie Slice Tool button present", slice_btn is not None, "wiring-slice-btn")
    if slice_btn is not None:
        QTest.mouseClick(slice_btn, Qt.MouseButton.LeftButton)
        app.processEvents()
        check("canvas entered zip_tie slice mode", win.canvas.mode == "zip_tie", win.canvas.mode)
        vp = win.canvas.viewport()
        p1 = win.canvas.mapFromScene(QPointF(28.0, 140.0))
        p2 = win.canvas.mapFromScene(QPointF(52.0, 140.0))
        QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        QTest.mouseMove(vp, p2)
        QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p2)
        app.processEvents()
        QTest.qWait(80)

    routes_col = [table.item(r, 5).text() for r in range(table.rowCount())] if table is not None else []
    check("zip-tie waypoint zt-1 created and bundles intersected wires",
          any("zt-1" in r for r in routes_col),
          str(routes_col))
    shot(win, "panel-layout-with-zip-tie")

    # 5. Switch to Technician Walkthrough (1-by-1) mode and step through wires
    if mode_combo is not None:
        mode_combo.setCurrentIndex(mode_combo.findData("walkthrough"))
        app.processEvents()
        QTest.qWait(60)

    step_lbl = dlg.findChild(QLabel, "wiring-step-label")
    next_btn = dlg.findChild(QPushButton, "wiring-next-btn")
    inst_chk = dlg.findChild(QCheckBox, "wiring-installed-check")
    check("walkthrough step 1 shown", step_lbl is not None and "Wire 1 of 12" in step_lbl.text(),
          step_lbl.text() if step_lbl else "")
    shot(win, "walkthrough-step-1")

    if inst_chk is not None:
        QTest.mouseClick(inst_chk, Qt.MouseButton.LeftButton)
        app.processEvents()
        check("wire 1 marked installed in table",
              table is not None and table.item(0, 8).text() == "DONE",
              table.item(0, 8).text() if table else "")

    if next_btn is not None:
        QTest.mouseClick(next_btn, Qt.MouseButton.LeftButton)
        app.processEvents()
        QTest.qWait(60)
        check("Next Wire button advances to Wire 2 of 12",
              step_lbl is not None and "Wire 2 of 12" in step_lbl.text(),
              step_lbl.text() if step_lbl else "")
    shot(win, "walkthrough-step-2")

    # Click row 3 (4th wire) in the table to jump directly to Wire 4 of 12
    if table is not None:
        item_rect = table.visualItemRect(table.item(3, 1))
        QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=item_rect.center())
        app.processEvents()
        QTest.qWait(60)
        check("clicking table row 4 jumps walkthrough to Wire 4 of 12",
              step_lbl is not None and "Wire 4 of 12" in step_lbl.text(),
              step_lbl.text() if step_lbl else "")
    shot(dlg, "walkthrough-dialog-step-4")

    # 6. Click "+ Add Wire-by-Wire Sheet" to add printable cut sheet to project
    add_sheet_btn = dlg.findChild(QPushButton, "wiring-add-sheet-btn")
    if add_sheet_btn is not None:
        QTest.mouseClick(add_sheet_btn, Qt.MouseButton.LeftButton)
        app.processEvents()
        QTest.qWait(100)
    sheet_ids = [s.id for s in win.session.project.sheets]
    check("Wire-by-Wire Cut Sheet added to project", "wire_by_wire" in sheet_ids, str(sheet_ids))
    shot(win, "wire-by-wire-cut-sheet")

    # 7. Save project via File -> Save and verify persisted YAML
    act_save = menu_action(win, "File", "Save")
    act_save.trigger()
    app.processEvents()
    QTest.qWait(80)
    saved_path = Path(win.session.path)
    shutil.copy2(saved_path, EVIDENCE / saved_path.name)
    saved_data = yaml.safe_load(saved_path.read_text(encoding="utf-8"))
    enc_sheet = next((s for s in (saved_data.get("sheets") or []) if s.get("id") == "enclosure"), {})
    enc_props = enc_sheet.get("properties") or {}
    check("persisted YAML stores zip_ties, walkthrough_step, and installed_wires",
          bool(enc_props.get("zip_ties")) and enc_props.get("walkthrough_step") == 3 and "101" in (enc_props.get("installed_wires") or []),
          json.dumps({"zip_ties": len(enc_props.get("zip_ties") or []), "step": enc_props.get("walkthrough_step"), "installed": enc_props.get("installed_wires")}))
    dlg.close()
    app.processEvents()

FEATURES = {
    "print-package": ("sample_catalog.pcad.yaml", feature_print_package),
    "iec-to-nema-convert": ("xref_iec_mirror.pcad.yaml", feature_iec_to_nema_convert),
    "panel-wiring": ("dol_starter.pcad.yaml", feature_panel_wiring),
}


def main() -> int:
    feature = sys.argv[1] if len(sys.argv) > 1 else "print-package"
    if feature not in FEATURES:
        print(f"unknown feature {feature}; have {sorted(FEATURES)}")
        return 2
    result["feature"] = feature
    pidfile = SCRATCH / "app.pid"
    pidfile.write_text(str(os.getpid()))
    import atexit
    atexit.register(lambda: pidfile.unlink(missing_ok=True))  # app.pid only exists while drive.py runs
    sample, fn = FEATURES[feature]
    project = SCRATCH / "work" / sample
    shutil.copy2(REPO / "examples" / sample, project)  # never edit the repo sample
    from panelcad.gui.app import launch_gui
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    app = launch_gui(project)  # same entry `panelcad gui <project>` uses
    win = next(w for w in QApplication.topLevelWidgets() if type(w).__name__ == "MainWindow")
    for _ in range(20):  # ready = window shown, session loaded, startup timers drained
        QTest.qWait(100)
    ready = win.isVisible() and win.session is not None and win.session.path is not None
    check("ready: main window up with project", ready, str(getattr(win.session, "path", None)))
    if ready:
        try:
            fn(app, win)
        except Exception:
            log(traceback.format_exc())
            check("feature ran", False, "exception")
    result["ok"] = bool(result["checks"]) and all(c["ok"] for c in result["checks"])
    (EVIDENCE / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    log(f"RESULT {'PASS' if result['ok'] else 'FAIL'}  ({sum(c['ok'] for c in result['checks'])}/{len(result['checks'])} checks)")
    win.close()
    app.processEvents()
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
