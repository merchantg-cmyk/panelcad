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
    from PySide6.QtWidgets import QDialog, QFrame, QLabel, QPushButton

    out_yaml = SCRATCH / "work" / "xref_iec_mirror-nema.pcad.yaml"
    state: dict = {}

    def convert_dialog(dlg):
        check("dialog is ConvertDialog", type(dlg).__name__ == "ConvertDialog", dlg.windowTitle())
        check("NEMA/ANSI radio is selected", dlg.nema.isChecked(), f"nema={dlg.nema.isChecked()} iec={dlg.iec.isChecked()}")
        check("Whole project radio is selected", dlg.whole.isChecked(), f"whole={dlg.whole.isChecked()}")
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
        check("review header shown", header is not None and header.isVisible() and "NEMA/ANSI" in header.text(),
              header.text() if header is not None else "missing")
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


FEATURES = {
    "print-package": ("sample_catalog.pcad.yaml", feature_print_package),
    "iec-to-nema-convert": ("xref_iec_mirror.pcad.yaml", feature_iec_to_nema_convert),
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
