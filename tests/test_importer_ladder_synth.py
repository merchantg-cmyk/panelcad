from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from panelcad.api.session import Session
from panelcad.convert import (
    ImportDeviceEntry,
    ImportFromToRow,
    ImportFromToSchedule,
    apply_import_from_to_edits,
    check_converted_sheet_layout,
    extract_import_from_to,
    synthesize_imported_ladder,
    verify_imported_from_to,
)
from panelcad.importer import (
    ImportJob,
    Workspace,
    build_session,
)


def _make_degraded_scan_workspace(tmp_path: Path) -> Workspace:
    """Create a synthetic degraded scan workspace where raw coordinate snapping drops wires."""
    ws = Workspace(tmp_path / "degraded-scan.import")
    # Devices placed at jittered scan coordinates where raw pin snapping fails (0 raw wires),
    # plus a 3-phase breaker + contactor + motor on the same page.
    devices = [
        {
            "id": "dev-cb1",
            "tag": "CB1",
            "kind": "breaker_3p",
            "symbol_id": "breaker_3p",
            "page_id": "page-001",
            "tile_id": "tile-1",
            "bbox": {"x": 220, "y": 180, "w": 90, "h": 70},
            "confidence": 0.91,
        },
        {
            "id": "dev-m1-main",
            "tag": "M1_MAIN",
            "kind": "contactor_3p",
            "symbol_id": "contactor_3p",
            "page_id": "page-001",
            "tile_id": "tile-1",
            "bbox": {"x": 225, "y": 420, "w": 90, "h": 70},
            "confidence": 0.89,
        },
        {
            "id": "dev-mtr1",
            "tag": "MTR1",
            "kind": "motor_3ph",
            "symbol_id": "motor_3ph",
            "page_id": "page-001",
            "tile_id": "tile-1",
            "bbox": {"x": 228, "y": 720, "w": 95, "h": 95},
            "confidence": 0.93,
        },
        {
            "id": "dev-stop",
            "tag": "PB_STOP",
            "kind": "pushbutton_nc",
            "symbol_id": "pushbutton_nc",
            "page_id": "page-001",
            "tile_id": "tile-2",
            "bbox": {"x": 713, "y": 317, "w": 72, "h": 44},
            "confidence": 0.84,
        },
        {
            "id": "dev-start",
            "tag": "PB_START",
            "kind": "pushbutton_no",
            "symbol_id": "pushbutton_no",
            "page_id": "page-001",
            "tile_id": "tile-2",
            "bbox": {"x": 1041, "y": 323, "w": 74, "h": 44},
            "confidence": 0.76,
        },
        {
            "id": "dev-coil",
            "tag": "M1",
            "kind": "coil",
            "symbol_id": "coil",
            "page_id": "page-001",
            "tile_id": "tile-2",
            "bbox": {"x": 1589, "y": 319, "w": 68, "h": 52},
            "confidence": 0.92,
        },
    ]
    # Broken/unattached wire fragments with huge endpoint gaps so raw build_session snaps 0 wires
    wires = [
        {
            "id": "wire-frag-1",
            "page_id": "page-001",
            "tile_id": "tile-2",
            "points": [[400.0, 120.0], [410.0, 120.0]],
            "label": "101",
            "confidence": 0.55,
        }
    ]
    ws.write_json(
        {
            "pages": [
                {
                    "page_id": "page-001",
                    "index": 0,
                    "width": 2200,
                    "height": 1700,
                    "dpi": 200,
                    "kind": "schematic",
                    "title": "DEGRADED SCAN MOTOR STARTER",
                    "devices": devices,
                    "wires": wires,
                }
            ],
            "devices": devices,
            "wires": wires,
            "texts": [],
            "terminals": [],
            "xrefs": [],
            "notes": [],
        },
        "facts.json",
    )
    return ws


def test_degraded_scan_from_to_extraction_and_ladder_synthesis(tmp_path: Path) -> None:
    ws = _make_degraded_scan_workspace(tmp_path)

    # Preserve mode has 0 snapped wires because scan coordinates are jittered/fragmented
    preserved, _rep_prev = build_session(ws, layout_mode="preserve")
    raw_wire_count = sum(len(sh.wires) for sh in preserved.project.sheets)
    assert raw_wire_count == 0

    # Universal From-To extraction infers both 3-phase power connections and control rung connections
    schedule = extract_import_from_to(ws)
    assert isinstance(schedule, ImportFromToSchedule)
    assert len(schedule.devices) == 6
    assert len(schedule.from_to_rows) >= 6

    # Low-confidence inferred rows are flagged for review
    assert any(r.needs_review for r in schedule.from_to_rows)

    # Synthesize clean NEMA ladder via build_session(..., layout_mode="ladder")
    synth, rep_synth = build_session(ws, layout_mode="ladder")
    synth_wire_count = sum(len(sh.wires) for sh in synth.project.sheets)
    assert synth_wire_count >= len(schedule.from_to_rows)
    assert len(rep_synth["wires"]) == synth_wire_count

    # Zero validate() errors, zero design_checks() errors, zero layout audit flags
    assert synth.validate() == []
    dc_errors = [d for d in synth.design_checks() if (d.get("severity") if isinstance(d, dict) else getattr(d, "severity", "")) == "error"]
    assert dc_errors == []

    for sh in synth.project.sheets:
        assert check_converted_sheet_layout(sh) == []

    # Verify netlist round-trip against schedule
    ver = verify_imported_from_to(synth, schedule)
    assert ver["matched"] is True
    assert ver["missing_count"] == 0
    assert ver["dangling_wire_count"] == 0

    # Power devices should be separated onto a dedicated power sheet without control L1/L2 ladder rails,
    # while control devices sit on a control ladder sheet with L1/L2 rails and coil on the right.
    power_sheets = [
        sh
        for sh in synth.project.sheets
        if any(comp.id in {"CB1", "M1_MAIN", "MTR1"} for comp in sh.components)
    ]
    control_sheets = [
        sh
        for sh in synth.project.sheets
        if any(comp.id in {"PB_STOP", "PB_START", "M1"} for comp in sh.components)
    ]
    assert len(power_sheets) == 1
    assert len(control_sheets) == 1
    assert power_sheets[0].id != control_sheets[0].id

    ctrl_sh = control_sheets[0]
    by_tag = {comp.id: comp for comp in ctrl_sh.components}
    assert by_tag["PB_STOP"].x < by_tag["PB_START"].x < by_tag["M1"].x


def test_apply_import_from_to_edits_persistence_and_round_trip(tmp_path: Path) -> None:
    ws = _make_degraded_scan_workspace(tmp_path)
    schedule = extract_import_from_to(ws)

    # User edits the From-To schedule to add a pilot light LT1 rung.
    edited_rows = [r.to_dict() for r in schedule.from_to_rows]
    edited_rows.append(
        {
            "wire_number": "W90",
            "from": "L1",
            "to": "LT1.X1",
            "sheet_id": "page-001",
            "rung": 2,
            "confidence": 1.0,
            "source": "user_added",
            "needs_review": False,
        }
    )
    edited_rows.append(
        {
            "wire_number": "W91",
            "from": "LT1.X2",
            "to": "L2",
            "sheet_id": "page-001",
            "rung": 2,
            "confidence": 1.0,
            "source": "user_added",
            "needs_review": False,
        }
    )

    updated = apply_import_from_to_edits(ws, from_to_rows=edited_rows)
    assert any(r.to_tag == "LT1" for r in updated.from_to_rows)
    assert any(d.tag == "LT1" for d in updated.devices)

    # Re-reading from workspace preserves the user-edited schedule
    reloaded = extract_import_from_to(ws)
    assert len(reloaded.from_to_rows) == len(updated.from_to_rows)
    assert any(r.wire_number == "W90" and r.to_tag == "LT1" for r in reloaded.from_to_rows)

    # Synthesizing from the edited schedule places LT1 and wires all rows cleanly
    synth = synthesize_imported_ladder(ws, schedule=reloaded)
    assert synth.validate() == []
    assert [d for d in synth.design_checks() if (d.get("severity") if isinstance(d, dict) else getattr(d, "severity", "")) == "error"] == []
    for sh in synth.project.sheets:
        assert check_converted_sheet_layout(sh) == []
    ver = verify_imported_from_to(synth, reloaded)
    assert ver["matched"] is True


@pytest.mark.parametrize("example_name", ["dol_starter", "xref_nema_seal_in", "xref_iec_mirror"])
def test_example_projects_ladder_synth_zero_errors_and_zero_layout_flags(
    example_name: str, tmp_path: Path
) -> None:
    examples_dir = Path(__file__).resolve().parents[1] / "examples"
    src_session = Session.open(examples_dir / f"{example_name}.pcad.yaml")

    sched = extract_import_from_to(src_session)
    assert len(sched.from_to_rows) > 0

    synth = synthesize_imported_ladder(src_session, schedule=sched)
    assert synth.validate() == []
    dc_errors = [d for d in synth.design_checks() if (d.get("severity") if isinstance(d, dict) else getattr(d, "severity", "")) == "error"]
    assert dc_errors == []

    for sh in synth.project.sheets:
        assert check_converted_sheet_layout(sh) == []

    ver = verify_imported_from_to(synth, sched)
    assert ver["matched"] is True
    assert ver["missing_count"] == 0


def test_import_review_panel_from_to_and_rebuild_clean_ladder(tmp_path: Path) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.import_review import ImportReviewPanel
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    ws = _make_degraded_scan_workspace(tmp_path)
    job = ImportJob.open(ws)

    panel = ImportReviewPanel()
    sched = job.from_to_schedule()
    panel.set_from_to_schedule(sched)

    initial_rows = panel.from_to_rows()
    assert len(initial_rows) == len(sched.from_to_rows)
    assert panel.layout_mode() == "preserve"

    # Switch to the From-To Wire Schedule tab and add a new pilot light rung via GUI controls
    panel.tabs.setCurrentWidget(panel.from_to_tab)
    r1 = panel.add_from_to_row(
        wire_number="W200",
        from_ep="L1",
        to_ep="LT_RUN.X1",
        sheet_rung="page-001:2",
        confidence=1.0,
    )
    r2 = panel.add_from_to_row(
        wire_number="W201",
        from_ep="LT_RUN.X2",
        to_ep="L2",
        sheet_rung="page-001:2",
        confidence=1.0,
    )
    assert r2 == r1 + 1
    assert len(panel.from_to_rows()) == len(initial_rows) + 2

    # Edit a cell in the From-To table and confirm confidence updates to 1.00 (user_edit)
    item = panel.from_to_table.item(0, 0)
    assert item is not None
    item.setText("W100_EDITED")
    updated_rows = panel.from_to_rows()
    assert updated_rows[0]["wire_number"] == "W100_EDITED"
    assert updated_rows[0]["confidence"] == 1.0
    assert updated_rows[0]["source"] == "user_edit"

    # Wire up MainWindow with this ImportJob and click Rebuild Clean Ladder via QTest
    win = MainWindow()
    win._import_job = job
    win._show_import_review(job)
    win.import_review.add_from_to_row(
        wire_number="W300",
        from_ep="L1",
        to_ep="LT_OK.X1",
        sheet_rung="page-001:2",
        confidence=1.0,
    )
    win.import_review.add_from_to_row(
        wire_number="W301",
        from_ep="LT_OK.X2",
        to_ep="L2",
        sheet_rung="page-001:2",
        confidence=1.0,
    )

    rebuild_signals: list[str] = []
    win.import_review.rebuild_clean_ladder_requested.connect(rebuild_signals.append)

    QTest.mouseClick(win.import_review.rebuild_clean_ladder_button, Qt.MouseButton.LeftButton)
    assert rebuild_signals == ["ladder"]
    assert win.import_review.layout_mode() == "ladder"

    # MainWindow session should now be rebuilt in clean ladder mode with LT_OK placed and wired
    assert win.session.validate() == []
    assert [d for d in win.session.design_checks() if (d.get("severity") if isinstance(d, dict) else getattr(d, "severity", "")) == "error"] == []
    all_tags = {comp.id for sh in win.session.project.sheets for comp in sh.components}
    assert "LT_OK" in all_tags
    for sh in win.session.project.sheets:
        assert check_converted_sheet_layout(sh) == []
    win.close()
