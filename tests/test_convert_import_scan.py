"""Tests for Patch 6: Import -> From-To Review -> IEC-to-NEMA Convert pipeline."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from panelcad.api.session import Session
from panelcad.convert import (
    DECISION_REASON_CODES,
    ImportedConversionResult,
    ImportedDeviceRecord,
    ImportedFromToRow,
    ImportedIECPrintDraft,
    RecordedImportVisionBackend,
    apply_from_to_corrections,
    build_iec_project_from_imported_from_to,
    convert_imported_iec_print,
    import_iec_print_to_from_to,
)
from panelcad.core.checks import check_project
import sys
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_convert_multipage import build_multipage_conveyor_iec


def _sample_scan_vision_payload(*, typo_on_202: bool = True) -> dict[str, Any]:
    """Simulated vision extraction payload for an IEC 3-phase starter + 24VDC control scan."""

    return {
        "title": "IEC Conveyor Scan",
        "devices": [
            {"tag": "-Q1", "role": "breaker_3p", "pins": ["1", "2", "3", "4", "5", "6"], "confidence": 0.96},
            {"tag": "-K1", "role": "contactor_3p", "pins": ["1", "2", "3", "4", "5", "6", "A1", "A2", "13", "14"], "confidence": 0.95},
            {"tag": "-F1", "role": "overload_3p", "pins": ["1", "2", "3", "4", "5", "6", "95", "96"], "confidence": 0.94},
            {"tag": "-M1", "role": "motor_3p", "pins": ["U1", "V1", "W1"], "confidence": 0.97},
            {"tag": "-S1", "role": "pushbutton_nc", "pins": ["11", "12"], "confidence": 0.92},
            {"tag": "-S2", "role": "pushbutton_no", "pins": ["13", "14"], "confidence": 0.91},
        ],
        "from_to": [
            {"wire_number": "101", "from": "-Q1:2", "to": "-K1:1", "confidence": 0.98},
            {"wire_number": "102", "from": "-Q1:4", "to": "-K1:3", "confidence": 0.98},
            {"wire_number": "103", "from": "-Q1:6", "to": "-K1:5", "confidence": 0.98},
            {"wire_number": "104", "from": "-K1:2", "to": "-F1:1", "confidence": 0.97},
            {"wire_number": "105", "from": "-K1:4", "to": "-F1:3", "confidence": 0.97},
            {"wire_number": "106", "from": "-K1:6", "to": "-F1:5", "confidence": 0.97},
            {"wire_number": "107", "from": "-F1:2", "to": "-M1:U1", "confidence": 0.96},
            {"wire_number": "108", "from": "-F1:4", "to": "-M1:V1", "confidence": 0.96},
            {"wire_number": "109", "from": "-F1:6", "to": "-M1:W1", "confidence": 0.96},
            {"wire_number": "201", "from": "-S1:12", "to": "-S2:13", "confidence": 0.93},
            {
                "wire_number": "202",
                "from": "-K1:13",
                "to": "-S2:14" if typo_on_202 else "-S1:12",
                "confidence": 0.52 if typo_on_202 else 0.95,
            },
            {"wire_number": "203", "from": "-S2:14", "to": "-K1:A1", "confidence": 0.94},
            {"wire_number": "204", "from": "-K1:14", "to": "-K1:A1", "confidence": 0.92},
            {"wire_number": "205", "from": "-K1:A2", "to": "-F1:95", "confidence": 0.95},
        ],
        "notes": [
            {"text": "PROTECTIVE EARTH PE TO MAIN EARTH BAR", "confidence": 0.95},
        ],
    }


def test_decision_reason_codes_include_imported_low_confidence() -> None:
    """`imported_low_confidence` is registered in `DECISION_REASON_CODES`."""

    assert "imported_low_confidence" in DECISION_REASON_CODES


def test_multipage_iec_pdf_import_to_from_to_and_convert_to_nema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-page IEC PDF exported by PanelCAD imports to From-To and converts to NEMA with 0 sheet_flags."""

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])

    iec_sess = build_multipage_conveyor_iec(include_power_sheet=True)
    pdf_path = tmp_path / "conveyor_iec_multipage.pdf"
    iec_sess.export_pdf(pdf_path)
    assert pdf_path.is_file() and pdf_path.stat().st_size > 0

    draft = import_iec_print_to_from_to(pdf_path)
    assert isinstance(draft, ImportedIECPrintDraft)
    assert len(draft.devices) >= 15
    assert len(draft.from_to_rows) >= 20
    assert draft.unresolved_rows() == []

    out_path = tmp_path / "conveyor_nema_from_pdf.pcad.yaml"
    res = convert_imported_iec_print(draft, target="NEMA", mode="rules_only", out=out_path)
    assert isinstance(res, ImportedConversionResult)
    assert res.verification.same_netlist is True
    assert res.verification.sheet_flags == []
    assert out_path.is_file()


def test_synthetic_text_iec_pdf_with_pypdf_extraction_converts_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-page IEC PDF containing vector text From-To lines extracts via pypdf and converts to NEMA."""

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QFont, QPainter, QPdfWriter
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])

    pdf_path = tmp_path / "synthetic_iec_text.pdf"
    writer = QPdfWriter(str(pdf_path))
    writer.setResolution(72)
    painter = QPainter(writer)
    painter.setFont(QFont("Sans", 10))
    page1_lines = [
        "SHEET 1: 3-PHASE POWER FEEDER",
        "DEVICES: -Q1 -K1 -F1 -M1",
        "WIRE 101: -Q1:2 -> -K1:1",
        "WIRE 102: -Q1:4 -> -K1:3",
        "WIRE 103: -Q1:6 -> -K1:5",
        "WIRE 104: -K1:2 -> -F1:1",
        "WIRE 105: -K1:4 -> -F1:3",
        "WIRE 106: -K1:6 -> -F1:5",
        "WIRE 107: -F1:2 -> -M1:U1",
        "WIRE 108: -F1:4 -> -M1:V1",
        "WIRE 109: -F1:6 -> -M1:W1",
    ]
    for idx, line in enumerate(page1_lines):
        painter.drawText(QPointF(50, 60 + idx * 24), line)

    writer.newPage()
    page2_lines = [
        "SHEET 2: 24VDC CONTROL",
        "DEVICES: -S1 -S2 -K1",
        "WIRE 201: -S1:12 -> -S2:13",
        "WIRE 202: -S2:14 -> -K1:A1",
        "WIRE 203: -K1:13 -> -S1:12",
        "WIRE 204: -K1:14 -> -K1:A1",
        "WIRE 205: -K1:A2 -> -F1:95",
    ]
    for idx, line in enumerate(page2_lines):
        painter.drawText(QPointF(50, 60 + idx * 24), line)
    painter.end()
    del writer

    draft = import_iec_print_to_from_to(pdf_path)
    assert len(draft.from_to_rows) == 14
    assert {d.tag for d in draft.devices} >= {"Q1", "K1", "F1", "M1", "S1", "S2"}

    res = convert_imported_iec_print(draft, target="NEMA", mode="rules_only")
    assert res.verification.same_netlist is True
    assert res.verification.sheet_flags == []


def test_scan_with_low_confidence_typo_corrected_before_conversion(tmp_path: Path) -> None:
    """Simulated IEC scan with a low-confidence OCR/vision typo (-K1:13 -> -S2:14) is corrected before conversion."""

    scan_path = tmp_path / "iec_motor_starter_scan.png"
    Image.new("RGB", (900, 650), "white").save(scan_path)

    vision = RecordedImportVisionBackend({"default": _sample_scan_vision_payload(typo_on_202=True)})
    draft = import_iec_print_to_from_to(scan_path, vision_backend=vision, confidence_threshold=0.80)

    # Wire 202 has confidence 0.52 < 0.80 and is flagged for review
    unresolved = draft.unresolved_rows()
    assert len(unresolved) == 1
    assert unresolved[0].wire_number == "202"
    assert unresolved[0].frm == "K1:13"
    assert unresolved[0].to == "S2:14"
    assert unresolved[0].needs_review is True
    assert unresolved[0].reason_code == "imported_low_confidence"

    # User corrects wire 202 from -K1:13 -> -S2:14 to -K1:13 -> -S1:12, adds a spare wire, and deletes it
    corrected = apply_from_to_corrections(
        draft,
        edits=[
            {
                "wire_number": "202",
                "from_tag": "-K1",
                "from_pin": "13",
                "to_tag": "-S1",
                "to_pin": "12",
            }
        ],
        add_rows=[
            {"row_id": "temp_false_row", "wire_number": "999", "from": "-S1:11", "to": "-S2:14"}
        ],
        delete_rows=["temp_false_row"],
    )
    assert corrected.unresolved_rows() == []
    row_202 = next(r for r in corrected.from_to_rows if r.wire_number == "202")
    assert row_202.to == "S1:12"
    assert row_202.needs_review is False
    assert row_202.confidence == 1.0

    # Reconstruct IEC project and verify From-To equivalence + clean NEMA conversion
    iec_sess = build_iec_project_from_imported_from_to(corrected)
    assert len(iec_sess.project.sheets) == 2

    res = convert_imported_iec_print(corrected, target="NEMA", mode="ai_assisted")
    assert res.verification.same_netlist is True
    assert res.verification.sheet_flags == []

    # No unresolved imported_low_confidence findings remain after user correction
    findings = [
        f
        for f in check_project(res.converted_project)
        if f.check == "convert.needs_review" and f.target == "imported_low_confidence"
    ]
    assert findings == []


def test_uncorrected_low_confidence_vision_item_surfaces_in_decisions_and_checks(tmp_path: Path) -> None:
    """Uncorrected low-confidence vision items propagate into ConversionDecision and check_project()."""

    scan_path = tmp_path / "iec_uncorrected_scan.png"
    Image.new("RGB", (800, 600), "white").save(scan_path)

    vision = RecordedImportVisionBackend({"default": _sample_scan_vision_payload(typo_on_202=True)})
    draft = import_iec_print_to_from_to(scan_path, vision_backend=vision, confidence_threshold=0.80)

    res = convert_imported_iec_print(draft, target="NEMA", mode="rules_only")
    assert res.verification.same_netlist is True
    assert res.verification.sheet_flags == []

    low_conf_decisions = [d for d in res.decisions if d.reason_code == "imported_low_confidence"]
    assert len(low_conf_decisions) == 1
    assert low_conf_decisions[0].needs_review is True
    assert "202" in low_conf_decisions[0].message

    findings = check_project(res.converted_project)
    low_conf_findings = [
        f for f in findings if f.check == "convert.needs_review" and f.target == "imported_low_confidence"
    ]
    assert len(low_conf_findings) == 1
    assert "202" in low_conf_findings[0].message


def test_zero_tesseract_or_poppler_subprocess_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Import -> From-To -> Convert never invokes tesseract, pdftoppm, pdfimages, or external binaries."""

    real_run = subprocess.run
    real_popen = subprocess.Popen
    forbidden = ("tesseract", "pdftoppm", "pdfimages", "pdftotext", "ghostscript", "gs")

    def _guarded_run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        cmd_str = " ".join(str(x) for x in cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        for token in forbidden:
            if token in cmd_str.lower():
                raise AssertionError(f"Forbidden external binary invoked: {cmd_str}")
        return real_run(cmd, *args, **kwargs)

    def _guarded_popen(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        cmd_str = " ".join(str(x) for x in cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        for token in forbidden:
            if token in cmd_str.lower():
                raise AssertionError(f"Forbidden external binary invoked: {cmd_str}")
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _guarded_run)
    monkeypatch.setattr(subprocess, "Popen", _guarded_popen)

    scan_path = tmp_path / "iec_no_tesseract.png"
    Image.new("RGB", (640, 480), "white").save(scan_path)
    vision = RecordedImportVisionBackend({"default": _sample_scan_vision_payload(typo_on_202=False)})

    sess = Session.create("IEC")
    res = sess.import_and_convert_standard(scan_path, target="NEMA", vision_backend=vision)
    assert res.verification.same_netlist is True
    assert res.verification.sheet_flags == []


def test_convert_dialog_import_scan_button_and_editable_from_to_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ConvertDialog exposes `convert-import-scan-btn` and `convert-import-from-to-table` for From-To correction."""

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QPushButton, QTableWidget

    from panelcad.gui.convert_dialog import ConvertDialog

    _app = QApplication.instance() or QApplication([])
    sess = Session.create("IEC")
    dlg = ConvertDialog(sess)

    btn = dlg.findChild(QPushButton, "convert-import-scan-btn")
    table = dlg.findChild(QTableWidget, "convert-import-from-to-table")
    assert btn is not None
    assert table is not None

    scan_path = tmp_path / "dialog_scan.png"
    Image.new("RGB", (640, 480), "white").save(scan_path)
    vision = RecordedImportVisionBackend({"default": _sample_scan_vision_payload(typo_on_202=True)})

    draft = dlg.load_imported_print(scan_path, vision_backend=vision)
    assert table.rowCount() == len(draft.from_to_rows)

    # Locate wire 202 in the table and correct To Tag/To Pin from S2:14 -> S1:12
    row_idx = next(
        i for i in range(table.rowCount()) if table.item(i, 0) and table.item(i, 0).text() == "202"
    )
    table.item(row_idx, 3).setText("S1")
    table.item(row_idx, 4).setText("12")

    out_yaml = tmp_path / "dialog_converted_nema.pcad.yaml"
    dlg.path.setText(str(out_yaml))
    res = dlg.perform()

    assert res["connection_check"]["ok"] is True
    assert out_yaml.is_file()
    low_conf = [d for d in res["decisions"] if d.get("reason_code") == "imported_low_confidence"]
    assert low_conf == []
