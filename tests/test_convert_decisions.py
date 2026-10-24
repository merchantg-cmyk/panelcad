"""Tests for IEC <-> NEMA conversion decision records and Problems panel integration (Patch 4)."""

from __future__ import annotations

from pathlib import Path

from panelcad.api.session import Session
from panelcad.convert import (
    ALL_REASON_CODES,
    DECISION_REASON_CODES,
    SHEET_FLAG_REASON_CODES,
    ConversionChunk,
    ConversionDecision,
    ConversionPlan,
    accept_chunk,
    apply_conversion,
    conversion_plan,
    convert_standard,
    revert_chunk,
)
from panelcad.core.checks import CHECK_IDS, check_project, run_checks


def test_reason_code_catalog_and_models_roundtrip() -> None:
    expected_decisions = {
        "ambiguous_role",
        "multifunction_block",
        "unmapped_symbol",
        "unmapped_pin",
        "off_grid_relayout",
        "tag_collision",
        "coil_without_contacts",
        "contact_without_coil",
        "multipage_consolidation",
    }
    expected_flags = {
        "component_overlap",
        "rail_collision",
        "wire_overlap",
        "title_block_overlap",
    }
    assert expected_decisions <= DECISION_REASON_CODES
    assert expected_flags <= SHEET_FLAG_REASON_CODES
    assert expected_decisions | expected_flags <= ALL_REASON_CODES
    assert "convert.needs_review" in CHECK_IDS
    assert "convert.sheet_flag" in CHECK_IDS

    dec = ConversionDecision(
        reason_code="unmapped_symbol",
        component_id="U99",
        sheet_id="main",
        before={"library_id": "iec.vendor.custom_drive"},
        after={"library_id": "iec.vendor.custom_drive"},
        needs_review=True,
        message="U99 has no counterpart symbol.",
        chunk_id="main:symbols",
    )
    roundtripped = ConversionDecision.from_dict(dec.to_dict())
    assert roundtripped.reason_code == "unmapped_symbol"
    assert roundtripped.component_id == "U99"
    assert roundtripped.sheet_id == "main"
    assert roundtripped.flag == "unmapped_symbol"
    assert roundtripped.ref == "U99"
    assert roundtripped["reason_code"] == "unmapped_symbol"

    chunk = ConversionChunk(
        id="main:symbols",
        kind="symbols",
        sheet="main",
        status="done",
        decisions=[roundtripped],
    )
    assert chunk.reason_codes == ["unmapped_symbol"]
    assert chunk.needs_review is True
    c_rt = ConversionChunk.from_dict(chunk.to_dict())
    assert c_rt.reason_codes == ["unmapped_symbol"]

    plan = ConversionPlan.from_dict(
        {
            "workspace": "/tmp/test.convert.json",
            "out_path": "/tmp/test-nema.pcad.yaml",
            "target": "NEMA/ANSI",
            "source_standard": "IEC",
            "chunks": [c_rt.to_dict()],
        }
    )
    assert plan.reason_codes == ["unmapped_symbol"]
    assert len(plan.needs_review_decisions()) == 1


def test_unmapped_symbol_tag_collision_and_problems_integration(tmp_path: Path) -> None:
    s = Session.create("decision_test")
    s.project.title_block.standard = "IEC"
    # PB1 already exists on the sheet; S1 will want to become PB1 when keep_numbers=True -> tag_collision
    s.place("nema.operators.pb_no", "PB1", x=60, y=50)
    s.place("iec.operators.pb_no", "S1", x=100, y=50)
    # Mechanical interlock has no NEMA counterpart in standard_map -> unmapped_symbol
    s.place("iec.contactors.mech_interlock", "MI1", x=150, y=50)
    # Ambiguous generic K device and multifunction block
    s.place("iec.contactors.coil", "K9", x=100, y=100)
    s.sheet("main").component("K9").properties["multifunction"] = True
    s.sheet("main").component("K9").properties["flag_orphan_coil"] = True
    # Contact whose parent coil K99 does not exist -> contact_without_coil
    s.place("iec.contactors.aux_no", "K99-13", x=140, y=100)
    s.sheet("main").component("K99-13").properties["parent"] = "K99"

    src_file = tmp_path / "src.pcad.yaml"
    out_file = tmp_path / "src-nema.pcad.yaml"
    s.save(src_file)

    res = convert_standard(s, "NEMA/ANSI", out=out_file, keep_numbers=True)
    plan = ConversionPlan.from_dict(res)
    codes = set(plan.reason_codes)
    assert "tag_collision" in codes
    assert "unmapped_symbol" in codes
    assert "multifunction_block" in codes
    assert "coil_without_contacts" in codes
    assert "contact_without_coil" in codes

    # Project metadata has last_conversion_decisions populated while chunks are unaccepted
    assert s.project.metadata.get("last_conversion_decisions")
    findings = check_project(s.project, s.catalog, label_cover=False)
    review_findings = [f for f in findings if f.code == "convert.needs_review"]
    assert len(review_findings) >= 4
    finding_reasons = {f.reason_code for f in review_findings}
    assert {"tag_collision", "unmapped_symbol", "multifunction_block", "contact_without_coil"} <= finding_reasons

    # Accepting all chunks resolves and clears the needs_review findings
    for chunk in res["chunks"]:
        accept_chunk(s, chunk["id"], res["workspace"])
    assert not s.project.metadata.get("last_conversion_decisions")
    findings_after = check_project(s.project, s.catalog, label_cover=False)
    assert not [f for f in findings_after if f.code in {"convert.needs_review", "convert.sheet_flag"}]


def test_sheet_flag_decisions_and_revert_clears_findings(tmp_path: Path) -> None:
    s = Session.create("flag_test")
    s.project.title_block.standard = "IEC"
    s.place("iec.operators.pb_no", "S1", x=80, y=60)
    # Inject a synthetic sheet flag decision + ambiguous_role decision via metadata
    s.project.metadata["last_conversion_decisions"] = [
        ConversionDecision(
            reason_code="component_overlap",
            component_id="S1",
            sheet_id="main",
            needs_review=True,
            message="S1 overlaps PB2 on sheet main.",
            chunk_id="main:layout",
        ).to_dict(),
        ConversionDecision(
            reason_code="ambiguous_role",
            component_id="K1",
            sheet_id="main",
            needs_review=True,
            message="K1 uses shared prefix K without explicit role.",
            chunk_id="tags",
        ).to_dict(),
        ConversionDecision(
            reason_code="off_grid_relayout",
            component_id="S1",
            sheet_id="main",
            needs_review=True,
            message="S1 was not placed on the ladder grid.",
            chunk_id="main:layout",
        ).to_dict(),
    ]
    findings = run_checks(s.project, s.catalog, label_cover=False)
    by_code = {f.reason_code: f for f in findings if f.code in {"convert.needs_review", "convert.sheet_flag"}}
    assert by_code["component_overlap"].code == "convert.sheet_flag"
    assert by_code["component_overlap"].sheet_id == "main"
    assert by_code["component_overlap"].component_id == "S1"
    assert by_code["ambiguous_role"].code == "convert.needs_review"
    assert by_code["off_grid_relayout"].code == "convert.needs_review"


def test_multipage_consolidation_decision_and_convert_dialog(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication, QLabel
    from panelcad.gui.convert_dialog import ConvertDialog

    _app = QApplication.instance() or QApplication([])

    s = Session.open("examples/xref_iec_mirror.pcad.yaml")
    copy_in = tmp_path / "xref_iec_mirror.pcad.yaml"
    copy_out = tmp_path / "xref_iec_mirror-nema.pcad.yaml"
    s.save(copy_in)

    dlg = ConvertDialog(s)
    dlg.path.setText(str(copy_out))
    res = dlg.perform()

    header = dlg.findChild(QLabel, "convert-review-header")
    assert header is not None
    assert "decisions:" in header.text()

    tags_summary = dlg.findChild(QLabel, "chunk-summary:tags")
    assert tags_summary is not None
    assert "reasons:" in tags_summary.text()
    dlg.close()


def test_unmapped_pin_and_revert_chunk_updates_decisions(tmp_path: Path) -> None:
    s = Session.create("pin_and_revert")
    s.project.title_block.standard = "IEC"
    s.place("iec.operators.pb_no", "S1", x=80, y=60)
    s.place("iec.contactors.coil", "K1", x=120, y=60)
    # Add a wire attached to a custom pin 99 on S1 that has no NEMA pin map entry
    from panelcad.core.models import Wire

    s.sheet("main").wires.append(
        Wire.model_validate({"id": "W_CUSTOM", "from": "S1.99", "to": "K1.A1"})
    )
    out_file = tmp_path / "pin-nema.pcad.yaml"
    res = convert_standard(s, "NEMA/ANSI", out=out_file)
    plan = ConversionPlan.from_dict(res)
    assert "unmapped_pin" in plan.reason_codes

    findings = check_project(s.project, s.catalog, label_cover=False)
    assert any(f.code == "convert.needs_review" and f.reason_code == "unmapped_pin" for f in findings)

    # Revert the symbols chunk -> unmapped_pin decision should disappear from active decisions
    revert_chunk(s, "main:symbols", res["workspace"])
    findings_after = check_project(s.project, s.catalog, label_cover=False)
    assert not any(f.code == "convert.needs_review" and f.reason_code == "unmapped_pin" for f in findings_after)


def test_multipage_consolidation_emits_decision_record(tmp_path: Path) -> None:
    from tests.test_convert_multipage import build_multipage_conveyor_iec
    s = build_multipage_conveyor_iec()
    src_file = tmp_path / "conveyor-iec.pcad.yaml"
    s.save(src_file)
    out_file = tmp_path / "conveyor-nema.pcad.yaml"
    res = convert_standard(s, "NEMA/ANSI", out=out_file)
    plan = ConversionPlan.from_dict(res)
    assert "multipage_consolidation" in plan.reason_codes
    consol_decs = [d for d in plan.decisions if d.reason_code == "multipage_consolidation"]
    assert consol_decs
    assert consol_decs[0].needs_review is False
