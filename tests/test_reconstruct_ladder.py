"""End-to-end tests for clean as-built NEMA ladder synthesis from panel photos."""

from __future__ import annotations

from pathlib import Path

from panelcad.api.session import Session
from panelcad.importer.ladder_synth import check_converted_sheet_layout
from panelcad.reconstruct.placeholder import project_has_needs_info
from panelcad.reconstruct.service import (
    apply_to_print,
    record_wire_landing,
    synthesize_reconstruct_schematic,
)
from panelcad.reconstruct.vision import FakeVision
from panelcad.reconstruct.fixtures import write_backpanel_photo


def _build_photo_reconstruct_session(tmp_path: Path) -> Session:
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    photo_path = tmp_path / "front_open.png"
    meta = write_backpanel_photo(photo_path)
    session = Session.create("AsBuiltStarter")
    added = session.reconstruct_add_photos([photo_path], role="backpanel")
    photo_id = added["added"][0]["id"]
    corners = meta["corners"]
    session.reconstruct_set_corners(photo_id, corners)
    session.reconstruct_set_scale(photo_id, "face", meta["corners"][0], meta["corners"][1], known_mm=meta.get("enclosure_width_mm") or 400, known_height_mm=meta.get("enclosure_height_mm") or 500)

    recordings = {
        "p1": {
            "devices": [
                {
                    "tag": "CB1",
                    "part_number": "5SY4332-7",
                    "manufacturer": "Siemens",
                    "category": "breaker",
                    "confidence": 0.96,
                    "x": 200, "y": 140, "w": 90, "h": 140,
                },
                {
                    "tag": "M1",
                    "part_number": "MC-9b",
                    "manufacturer": "LS Electric",
                    "category": "contactor",
                    "confidence": 0.96,
                    "x": 340, "y": 140, "w": 90, "h": 140,
                },
                {
                    "tag": "OL1",
                    "part_number": "MT-32",
                    "manufacturer": "LS Electric",
                    "category": "overload",
                    "confidence": 0.95,
                    "x": 470, "y": 140, "w": 90, "h": 140,
                },
                {
                    "tag": "S1",
                    "part_number": "XB4-BA42",
                    "manufacturer": "Schneider",
                    "category": "operator",
                    "confidence": 0.93,
                    "x": 620, "y": 140, "w": 60, "h": 60,
                },
                {
                    "tag": "S2",
                    "part_number": "XB4-BA31",
                    "manufacturer": "Schneider",
                    "category": "operator",
                    "confidence": 0.93,
                    "x": 700, "y": 140, "w": 60, "h": 60,
                },
                {
                    "tag": "PL1",
                    "part_number": "XB4-BVB3",
                    "manufacturer": "Schneider",
                    "category": "pilot_light",
                    "confidence": 0.92,
                    "x": 780, "y": 140, "w": 60, "h": 60,
                },
            ],
            "terminal_strips": [],
            "texts": [],
        }
    }
    session.reconstruct_identify(vision=FakeVision(recordings))
    items_by_tag = {
        row["tag"]: row["id"]
        for row in session.reconstruct_status()["items"]
        if row["role"] == "part" and row.get("tag")
    }
    session.reconstruct_confirm_item(
        items_by_tag["CB1"],
        envelope_w_mm=54.0,
        envelope_h_mm=80.0,
        envelope_d_mm=70.0,
        mount="din_rail",
        tag="CB1",
        library_id="iec.breakers.mcb_3p_32a",
    )
    session.reconstruct_confirm_item(items_by_tag["M1"], catalog="MC-9b", tag="M1")
    session.reconstruct_confirm_item(
        items_by_tag["OL1"],
        envelope_w_mm=45.0,
        envelope_h_mm=75.0,
        envelope_d_mm=80.0,
        mount="din_rail",
        tag="OL1",
        library_id="iec.protection.overload_9a",
    )
    session.reconstruct_confirm_item(
        items_by_tag["S1"],
        envelope_w_mm=30.0,
        envelope_h_mm=40.0,
        envelope_d_mm=55.0,
        mount="door",
        tag="S1",
        library_id="nema.operators.pb_nc",
    )
    session.reconstruct_confirm_item(
        items_by_tag["S2"],
        envelope_w_mm=30.0,
        envelope_h_mm=40.0,
        envelope_d_mm=55.0,
        mount="door",
        tag="S2",
        library_id="nema.operators.pb_no",
    )
    session.reconstruct_confirm_item(
        items_by_tag["PL1"],
        envelope_w_mm=30.0,
        envelope_h_mm=40.0,
        envelope_d_mm=55.0,
        mount="door",
        tag="PL1",
        library_id="nema.operators.pilot_light",
    )
    return session


def test_reconstruct_end_to_end_ladder_and_power_separation(tmp_path: Path):
    session = _build_photo_reconstruct_session(tmp_path)

    # Record control ladder wire landings (Rung 1: Stop/Start/Seal-in -> M1 coil -> OL1 NC -> L2; Rung 2: aux -> PL1 -> L2)
    control_landings = [
        ("101", "L1", "", "S1", "1"),
        ("102", "S1", "2", "S2", "3"),
        ("103", "S2", "4", "M1", "A1"),
        ("102A", "M1-13", "13", "S2", "3"),
        ("103A", "M1-13", "14", "M1", "A1"),
        ("104", "M1", "A2", "OL1", "95"),
        ("105", "OL1", "96", "L2", ""),
        ("201", "L1", "", "M1-23", "13"),
        ("202", "M1-23", "14", "PL1", "X1"),
        ("203", "PL1", "X2", "L2", ""),
    ]
    for wnum, f_tag, f_pin, t_tag, t_pin in control_landings:
        record_wire_landing(
            session,
            wire_number=wnum,
            from_tag=f_tag,
            from_pin=f_pin,
            to_tag=t_tag,
            to_pin=t_pin,
        )

    # Record 3-phase power wires (CB1 -> M1 -> OL1 -> MTR1)
    power_landings = [
        ("L1", "CB1", "2", "M1", "1"),
        ("L2", "CB1", "4", "M1", "3"),
        ("L3", "CB1", "6", "M1", "5"),
        ("T1", "M1", "2", "OL1", "L1"),
        ("T2", "M1", "4", "OL1", "L2"),
        ("T3", "M1", "6", "OL1", "L3"),
        ("M1_T1", "OL1", "T1", "MTR1", "T1"),
        ("M1_T2", "OL1", "T2", "MTR1", "T2"),
        ("M1_T3", "OL1", "T3", "MTR1", "T3"),
    ]
    for wnum, f_tag, f_pin, t_tag, t_pin in power_landings:
        record_wire_landing(
            session,
            wire_number=wnum,
            from_tag=f_tag,
            from_pin=f_pin,
            to_tag=t_tag,
            to_pin=t_pin,
        )

    result = apply_to_print(session)
    assert result["schematic_sheet"] == "asbuilt"
    assert result["power_sheet"] == "asbuilt_power"
    assert result["rungs"] >= 2

    asbuilt = session.project.sheet("asbuilt")
    assert asbuilt.drawing_style == "ladder"
    assert asbuilt.rail_hot == "L1"
    assert asbuilt.rail_return == "L2"

    # Distinct rung y coordinates (not flat y = 40.0)
    comp_map = {c.id: c for c in asbuilt.components}
    assert not all(abs(c.y - 40.0) < 1e-6 for c in asbuilt.components)
    assert abs(comp_map["S1"].y - comp_map["M1-23"].y) >= 35.0
    assert abs(comp_map["M1"].y - comp_map["PL1"].y) >= 35.0
    assert abs(comp_map["M1-13"].y - comp_map["S2"].y) >= 15.0

    # Coils/lights placed to the right of contacts on their rung
    assert comp_map["S1"].x < comp_map["S2"].x < comp_map["M1"].x < comp_map["OL1"].x
    assert comp_map["M1-23"].x < comp_map["PL1"].x

    # 100% From-To netlist preservation across control and power sheets
    all_recorded = {
        (w.number, w.frm, w.to)
        for s in (session.project.sheet("asbuilt"), session.project.sheet("asbuilt_power"))
        for w in s.wires
    }
    expected_control = {
        ("101", "L1", "S1.1"),
        ("102", "S1.2", "S2.3"),
        ("103", "S2.4", "M1.A1"),
        ("102A", "M1-13.13", "S2.3"),
        ("103A", "M1-13.14", "M1.A1"),
        ("104", "M1.A2", "OL1.95"),
        ("105", "OL1.96", "L2"),
        ("201", "L1", "M1-23.13"),
        ("202", "M1-23.14", "PL1.X1"),
        ("203", "PL1.X2", "L2"),
    }
    expected_power = {
        ("L1", "CB1.2", "M1.1"),
        ("L2", "CB1.4", "M1.3"),
        ("L3", "CB1.6", "M1.5"),
        ("T1", "M1.2", "OL1.L1"),
        ("T2", "M1.4", "OL1.L2"),
        ("T3", "M1.6", "OL1.L3"),
        ("M1_T1", "OL1.T1", "MTR1.T1"),
        ("M1_T2", "OL1.T2", "MTR1.T2"),
        ("M1_T3", "OL1.T3", "MTR1.T3"),
    }
    assert expected_control <= all_recorded
    assert expected_power <= all_recorded

    # Zero validation errors and zero layout collisions
    assert session.validate() == []
    assert check_converted_sheet_layout(session) == []


def test_reconstruct_unconfirmed_parts_keep_not_for_build_and_wizard_rebuild(tmp_path: Path):
    from PySide6.QtWidgets import QApplication
    from panelcad.gui.reconstruct_wizard import ReconstructWizard

    _app = QApplication.instance() or QApplication([])
    photo_path = tmp_path / "front_open.png"
    meta = write_backpanel_photo(photo_path)
    session = Session.create("NeedsInfoHold")
    added = session.reconstruct_add_photos([photo_path], role="backpanel")
    photo_id = added["added"][0]["id"]
    session.reconstruct_set_corners(photo_id, meta["corners"])
    session.reconstruct_set_scale(photo_id, "face", meta["corners"][0], meta["corners"][1], known_mm=meta.get("enclosure_width_mm") or 400, known_height_mm=meta.get("enclosure_height_mm") or 500)

    recordings = {
        "p1": {
            "devices": [
                {
                    "tag": "M1",
                    "part_number": "MC-9b",
                    "manufacturer": "LS Electric",
                    "category": "contactor",
                    "confidence": 0.95,
                    "x": 240, "y": 140, "w": 90, "h": 140,
                },
                {
                    "tag": "U1",
                    "part_number": "UNKNOWN-BOX-99",
                    "manufacturer": "",
                    "category": "plc",
                    "confidence": 0.40,
                    "x": 450, "y": 140, "w": 110, "h": 140,
                },
            ],
            "terminal_strips": [],
            "texts": [],
        }
    }
    session.reconstruct_identify(vision=FakeVision(recordings))
    items_by_tag = {
        row["tag"]: row["id"]
        for row in session.reconstruct_status()["items"]
        if row["role"] == "part" and row.get("tag")
    }
    session.reconstruct_confirm_item(items_by_tag["M1"], catalog="MC-9b", tag="M1")
    # U1 remains unconfirmed -> must keep not_for_build safety hold
    session.reconstruct_record_wire_landing("101", from_tag="L1", to_tag="S1", to_pin="1")
    session.reconstruct_record_wire_landing("102", from_tag="S1", from_pin="2", to_tag="M1", to_pin="A1")
    session.reconstruct_record_wire_landing("103", from_tag="M1", from_pin="A2", to_tag="L2")

    out = apply_to_print(session)
    assert out["not_for_build"] is True
    assert project_has_needs_info(session.project) is True

    wiz = ReconstructWizard(session)
    wiz._refresh_steps()
    assert "3 traced" in wiz.wire_schedule_summary.text()
    assert "S1.2 -> M1.A1" in wiz.wire_schedule_summary.text()
    rebuilt = wiz._rebuild_schematic()
    assert rebuilt["rungs"] == 1
    assert check_converted_sheet_layout(session) == []
