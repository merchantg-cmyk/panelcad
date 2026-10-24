from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QLabel, QTableWidget

from panelcad.api.session import Session
from panelcad.convert.engine import ConversionEngine, convert_standard, start_conversion
from panelcad.convert.from_to_check import build_from_to_diff_rows
from panelcad.convert.netlist import same_netlist, sheet_edges
from panelcad.convert.plan import (
    ConversionDecision,
    resolve_conversion_decision,
    sort_decisions_by_confidence,
    valid_decision_options,
)
from panelcad.core.checks import run_checks
from panelcad.gui.convert_dialog import CompareDialog, ConvertDialog

def load_project(path: Path):
    return Session.open(path).project

def save_project(proj, path: Path) -> None:
    sess = Session(proj, path=path)
    sess.save(path)


REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _make_iec_wire_project(tmp_path: Path) -> Path:
    """Build a small IEC ladder project with IEC wire numbers ('1.1', '1.2', '1.3') and an ambiguous device."""
    src = REPO / "examples" / "xref_iec_mirror.pcad.yaml"
    proj = load_project(src)
    main_sheet = next(s for s in proj.sheets if s.id == "main")
    # Set explicit IEC wire numbers on main sheet wires so wire_mode='line_ref' vs 'keep' is distinct
    iec_nums = ["1.1", "1.2", "1.3", "1.1", "1.2", "1.1", "1.4", "1.3"]
    for idx, wire in enumerate(main_sheet.wires):
        wire.number = iec_nums[idx % len(iec_nums)]
    out_path = tmp_path / "iec_wires.pcad.yaml"
    save_project(proj, out_path)
    return out_path


def test_convert_dialog_selectors_and_wire_mode_keep_vs_line_ref(qapp, tmp_path: Path):
    iec_path = _make_iec_wire_project(tmp_path)
    orig_bytes = iec_path.read_bytes()

    # 1. Convert with wire_mode="line_ref" via ConvertDialog
    sess_line = Session.open(iec_path)
    dlg_line = ConvertDialog(sess_line)

    mode_combo = dlg_line.findChild(QComboBox, "convert-mode-combo")
    tag_combo = dlg_line.findChild(QComboBox, "convert-tag-mode-combo")
    wire_combo = dlg_line.findChild(QComboBox, "convert-wire-mode-combo")
    keep_cb = dlg_line.findChild(QCheckBox, "keep-existing-numbers")

    assert mode_combo is not None
    assert [mode_combo.itemData(i) for i in range(mode_combo.count())] == ["rules_only", "ai_assisted"]
    assert tag_combo is not None
    assert [tag_combo.itemData(i) for i in range(tag_combo.count())] == ["sequential", "keep_numbers"]
    assert wire_combo is not None
    assert [wire_combo.itemData(i) for i in range(wire_combo.count())] == ["line_ref", "keep"]
    assert keep_cb is not None

    # Verify two-way sync between tag_combo and keep_existing_numbers checkbox
    dlg_line.set_tag_mode("keep_numbers")
    assert keep_cb.isChecked() is True
    assert dlg_line.tag_mode() == "keep_numbers"
    keep_cb.setChecked(False)
    assert dlg_line.tag_mode() == "sequential"

    out_line = tmp_path / "out_line_ref.pcad.yaml"
    dlg_line.path.setText(str(out_line))
    dlg_line.set_wire_mode("line_ref")
    res_line = dlg_line.perform()

    assert res_line["wire_mode"] == "line_ref"
    assert res_line["same_netlist"] is True
    assert res_line["from_to_check"]["ok"] is True
    proj_line = load_project(out_line)
    main_line = next(s for s in proj_line.sheets if s.id == "main")
    line_wire_nums = [w.number for w in main_line.wires]
    # In line_ref mode, '1.1', '1.2', etc. convert to NEMA rung numbers ('101', '102', ...)
    assert all(not n.startswith("1.") for n in line_wire_nums)
    assert any(n.startswith("10") for n in line_wire_nums)

    # 2. Convert with wire_mode="keep" via ConvertDialog
    sess_keep = Session.open(iec_path)
    dlg_keep = ConvertDialog(sess_keep)
    out_keep = tmp_path / "out_keep.pcad.yaml"
    dlg_keep.path.setText(str(out_keep))
    dlg_keep.set_wire_mode("keep")
    res_keep = dlg_keep.perform()

    assert res_keep["wire_mode"] == "keep"
    assert res_keep["same_netlist"] is True
    assert res_keep["from_to_check"]["ok"] is True
    proj_keep = load_project(out_keep)
    main_keep = next(s for s in proj_keep.sheets if s.id == "main")
    keep_wire_nums = [w.number for w in main_keep.wires]
    # In keep mode, original IEC wire numbers ('1.1', '1.2', ...) are preserved while tags/symbols convert
    assert keep_wire_nums == ["1.1", "1.2", "1.3", "1.1", "1.2", "1.1", "1.4", "1.3"]
    assert {c.id for c in main_keep.components} == {"PB1", "M1", "M1-14", "M1-21", "PL1"}
    # Source project file is never modified
    assert iec_path.read_bytes() == orig_bytes


def test_compare_dialog_summary_bar_highlights_from_to_and_decisions_sorting(qapp, tmp_path: Path):
    src = REPO / "examples" / "xref_iec_mirror.pcad.yaml"
    work_in = tmp_path / "in.pcad.yaml"
    shutil.copy2(src, work_in)

    # Inject an ambiguous component and a sheet note so AI-assisted conversion produces multiple confidence levels
    proj = load_project(work_in)
    main_sh = next(s for s in proj.sheets if s.id == "main")
    main_sh.description = "All wiring per IEC 60204-1 colour code"
    save_project(proj, work_in)

    sess = Session.open(work_in)
    dlg = ConvertDialog(sess)
    out_path = tmp_path / "out_nema.pcad.yaml"
    dlg.path.setText(str(out_path))
    dlg.set_conversion_mode("ai_assisted")
    res = dlg.perform()

    # Add a low-confidence unresolved role decision and a medium-confidence note decision to workspace
    ws_path = Path(res["workspace"])
    ws_data = json.loads(ws_path.read_text(encoding="utf-8"))
    low_dec = ConversionDecision(
        reason_code="ambiguous_role",
        component_id="PB1",
        sheet_id="main",
        before={"id": "S1", "tag": "S1", "prefix": "S"},
        after={"id": "PB1", "tag": "PB1", "prefix": "PB"},
        proposal={"role_prefix": "SS", "id": "SS1", "valid": True, "accepted": False},
        confidence=0.45,
        needs_review=True,
        message="Ambiguous switch S1: selector switch SS vs pushbutton PB",
        chunk_id="tags",
    ).to_dict()
    med_dec = ConversionDecision(
        reason_code="note_reworded",
        component_id="",
        sheet_id="main",
        before={"note_index": 0, "text": "All wiring per IEC 60204-1 colour code"},
        after={"note_index": 0, "text": "All wiring per IEC 60204-1 colour code"},
        proposal={
            "note_index": 0,
            "original_text": "All wiring per IEC 60204-1 colour code",
            "reworded_text": "All wiring per NFPA 79 color code (verify against project spec)",
            "valid": True,
            "accepted": False,
        },
        confidence=0.72,
        needs_review=True,
        message="Standard-specific note references IEC 60204-1",
        chunk_id="main:symbols",
    ).to_dict()
    ws_data.setdefault("decisions", []).extend([med_dec, low_dec])
    ws_path.write_text(json.dumps(ws_data, indent=2), encoding="utf-8")

    after_proj = load_project(out_path)
    before_proj = load_project(work_in)
    compare = CompareDialog(
        sess,
        before_proj,
        after_proj,
        "main",
        dlg,
        workspace=str(ws_path),
        result=res,
    )

    # Summary bar check
    summary_lbl = compare.findChild(QLabel, "compare-summary-bar")
    assert summary_lbl is not None
    txt = summary_lbl.text()
    assert "swapped" in txt
    assert "renamed" in txt
    assert "needs review" in txt
    assert "netlist: OK" in txt
    assert "from-to: OK" in txt

    # Highlight overlay check
    hl_cb = compare.findChild(QCheckBox, "compare-highlight-toggle")
    assert hl_cb is not None and hl_cb.isChecked() is True
    assert {"S1", "K1", "H1"}.issubset(compare.highlighted_before_ids)
    assert {"PB1", "M1", "PL1"}.issubset(compare.highlighted_after_ids)

    # From-To diff table check
    ft_table = compare.findChild(QTableWidget, "compare-from-to-table")
    assert ft_table is not None
    assert ft_table.rowCount() >= 8
    headers = [ft_table.horizontalHeaderItem(c).text() for c in range(ft_table.columnCount())]
    assert headers == ["Old Wire #", "Old From -> To", "New Wire #", "New From -> To", "Status"]
    statuses = {ft_table.item(r, 4).text() for r in range(ft_table.rowCount())}
    assert statuses.issubset({"MATCH", "RENAMED", "LOCKED", "MISMATCH"})
    assert "MISMATCH" not in statuses

    # Decisions table sorted by confidence ascending (lowest confidence / needs_review first)
    dec_table = compare.findChild(QTableWidget, "compare-decisions-table")
    assert dec_table is not None
    assert dec_table.rowCount() >= 2
    confidences = [float(dec_table.item(r, 3).text()) for r in range(dec_table.rowCount())]
    assert confidences == sorted(confidences)
    assert confidences[0] == pytest.approx(0.45)
    assert confidences[1] == pytest.approx(0.72)


def test_resolve_conversion_decision_accept_change_keep_original_and_checks(qapp, tmp_path: Path):
    src = REPO / "examples" / "xref_iec_mirror.pcad.yaml"
    work_in = tmp_path / "in_resolve.pcad.yaml"
    shutil.copy2(src, work_in)
    orig_bytes = work_in.read_bytes()

    proj = load_project(work_in)
    main_sh = next(s for s in proj.sheets if s.id == "main")
    main_sh.description = "All wiring per IEC 60204-1 colour code"
    save_project(proj, work_in)
    orig_with_note_bytes = work_in.read_bytes()

    out_path = tmp_path / "out_resolve.pcad.yaml"
    eng = ConversionEngine(Session.open(work_in), "NEMA/ANSI", out=out_path, mode="ai_assisted")
    res = eng.start()

    ws_path = Path(res["workspace"])
    ws_data = json.loads(ws_path.read_text(encoding="utf-8"))

    # Add 3 reviewable decisions:
    # 1. Role decision on PB1 (originally S1) -> proposal SS1
    # 2. Note decision on main note 0 -> proposal NFPA 79 text
    # 3. Role decision on PL1 (originally H1) -> proposal LT1 (will be set to Keep original -> H1)
    d_role_change = ConversionDecision(
        reason_code="ambiguous_role",
        component_id="PB1",
        sheet_id="main",
        before={"id": "S1", "tag": "S1", "prefix": "S"},
        after={"id": "PB1", "tag": "PB1", "prefix": "PB"},
        proposal={"role_prefix": "SS", "id": "SS1", "valid": True, "accepted": False},
        confidence=0.45,
        needs_review=True,
        message="Ambiguous switch S1",
        chunk_id="tags",
    ).to_dict()
    d_note_accept = ConversionDecision(
        reason_code="note_reworded",
        component_id="",
        sheet_id="main",
        before={"note_index": 0, "text": "All wiring per IEC 60204-1 colour code"},
        after={"note_index": 0, "text": "All wiring per IEC 60204-1 colour code"},
        proposal={
            "note_index": 0,
            "original_text": "All wiring per IEC 60204-1 colour code",
            "reworded_text": "All wiring per NFPA 79 color code",
            "valid": True,
            "accepted": False,
        },
        confidence=0.60,
        needs_review=True,
        message="Standard-specific note",
        chunk_id="main:symbols",
    ).to_dict()
    d_role_keep = ConversionDecision(
        reason_code="ambiguous_role",
        component_id="PL1",
        sheet_id="main",
        before={"id": "H1", "tag": "H1", "prefix": "H"},
        after={"id": "PL1", "tag": "PL1", "prefix": "PL"},
        proposal={"role_prefix": "LT", "id": "LT1", "valid": True, "accepted": False},
        confidence=0.55,
        needs_review=True,
        message="Indicator H1 role review",
        chunk_id="tags",
    ).to_dict()
    d_unresolved = ConversionDecision(
        reason_code="unmapped_symbol",
        component_id="M1",
        sheet_id="main",
        before={"id": "K1", "library_id": "iec.contactors.coil"},
        after={"id": "M1", "library_id": "nema.contactors.coil"},
        proposal={"library_id": "nema.relays.cr", "valid": True, "accepted": False},
        confidence=0.50,
        needs_review=True,
        message="Contactor vs control relay review",
        chunk_id="main:symbols",
    ).to_dict()

    ws_data["decisions"] = [d_role_change, d_note_accept, d_role_keep, d_unresolved]
    ws_data["working_project"]["properties"]["last_conversion_decisions"] = list(ws_data["decisions"])
    ws_path.write_text(json.dumps(ws_data, indent=2), encoding="utf-8")
    save_project(load_project(out_path), out_path)

    # Verify invalid option raises ValueError
    with pytest.raises(ValueError, match="Invalid option"):
        eng.resolve_decision(0, "change", "NOT_A_VALID_ROLE_999")

    # Open CompareDialog and drive interactive resolution on the first 3 rows, leaving d_unresolved alone
    sess = Session.open(work_in)
    dlg = ConvertDialog(sess)
    dlg.path.setText(str(out_path))
    dlg._workspace = str(ws_path)
    after_proj = load_project(out_path)
    after_proj.properties["last_conversion_decisions"] = list(ws_data["decisions"])
    before_proj = load_project(work_in)

    compare = CompareDialog(
        sess,
        before_proj,
        after_proj,
        "main",
        dlg,
        workspace=str(ws_path),
        result=res,
    )

    # Sorted order by confidence ascending:
    # row 0: 0.45 (d_role_change on PB1) -> set action="change", option="SS1"
    # row 1: 0.50 (d_unresolved on M1) -> leave untouched (needs_review=True)
    # row 2: 0.55 (d_role_keep on PL1) -> set action="keep_original" (reverts PL1 -> H1)
    # row 3: 0.60 (d_note_accept on main note 0) -> set action="accept" (applies NFPA 79 note)
    dec_table = compare.decisions_table
    assert dec_table.rowCount() >= 4

    # Row 0: Change PB1 -> SS1 using valid option picker
    act_0 = dec_table.cellWidget(0, 6)
    opt_0 = dec_table.cellWidget(0, 7)
    assert isinstance(act_0, QComboBox) and isinstance(opt_0, QComboBox)
    act_0.setCurrentIndex(act_0.findData("change"))
    ss1_idx = opt_0.findData("SS1")
    assert ss1_idx >= 0
    opt_0.setCurrentIndex(ss1_idx)
    compare._apply_single_decision_row(0)

    # Find the row for PL1 (originally H1) and set Keep original
    for r in range(compare.decisions_table.rowCount()):
        if compare.decisions_table.item(r, 0).text() in {"PL1", "H1"}:
            act_w = compare.decisions_table.cellWidget(r, 6)
            act_w.setCurrentIndex(act_w.findData("keep_original"))
            compare._apply_single_decision_row(r)
            break

    # Find the row for standard_specific_note and set Accept
    for r in range(compare.decisions_table.rowCount()):
        if compare.decisions_table.item(r, 2).text() == "note_reworded":
            act_w = compare.decisions_table.cellWidget(r, 6)
            act_w.setCurrentIndex(act_w.findData("accept"))
            compare._apply_single_decision_row(r)
            break

    # Accept & Save in CompareDialog
    compare._on_accept_save()

    # Source project file is untouched
    assert work_in.read_bytes() == orig_with_note_bytes

    # Saved converted project reflects all 3 resolved decisions
    saved = load_project(out_path)
    saved_main = next(s for s in saved.sheets if s.id == "main")
    comp_ids = {c.id for c in saved_main.components}
    assert "SS1" in comp_ids and "PB1" not in comp_ids
    assert "H1" in comp_ids and "PL1" not in comp_ids
    assert saved_main.description == "All wiring per NFPA 79 color code"

    # Netlist equivalence still holds with updated tag_map
    counts = compare.summary_counts()
    assert counts["netlist_ok"] is True
    assert counts["from_to_ok"] is True

    # Resolved decisions cleared from Problems (run_checks), while the 1 unresolved decision remains
    issues = run_checks(saved, sess.catalog)
    conv_issues = [i for i in issues if i.code.startswith("convert.")]
    assert len(conv_issues) == 1
    assert conv_issues[0].code == "convert.needs_review"
    assert conv_issues[0].reason_code == "unmapped_symbol"
    assert conv_issues[0].component_id == "M1"
