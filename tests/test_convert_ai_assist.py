"""Tests for Patch 5: src/panelcad/convert/ai_assist.py (advisory AI helper)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from panelcad.api.session import Session
from panelcad.convert import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    HARD_LAYOUT_FLAGS,
    ROLE_PREFIX_TABLE,
    ConversionEngine,
    RecordedJSONBackend,
    default_confidence_threshold,
    rank_layout_candidates,
    resolve_ambiguous_role,
    revert_chunk,
    revert_reworded_notes,
    reword_sheet_notes,
    start_conversion,
    suggest_symbol_counterpart,
)
from panelcad.convert.from_to_check import check_connections
from panelcad.core.models import Project, Wire
from panelcad.convert.netlist import same_netlist, sheet_edges
from panelcad.library.catalog import Catalog, Library


def _make_ambiguous_iec_session() -> Session:
    """Build an IEC schematic with:
    - ambiguous -K1 (generic/custom coil without motor power contacts, wired only to pilot light)
    - unmapped custom symbol 'iec.custom.safety_relay_2p' wired on pins A1, A2
    - German/IEC sheet title & notes ('Hauptstromkreis / Not-Halt nach IEC 60204-1')
    """
    catalog = Library.load_builtin()
    base_coil = catalog.get("iec.relays.cr")
    custom_sym = base_coil.model_copy(
        update={
            "library_id": "iec.custom.safety_relay_2p",
            "name": "IEC Custom Safety Relay Coil",
            "category": "custom",
            "ref_prefix": "K",
        }
    )
    catalog.devices["iec.custom.safety_relay_2p"] = custom_sym

    doc = {
        "format": "panelcad",
        "version": "0.1",
        "name": "ai_assist_demo",
        "properties": {
            "notes": [
                "Not-Halt Kreis prüfen",
                "Motorschutzschalter im Hauptstromkreis",
            ]
        },
        "title_block": {
            "standard": "IEC",
            "drawing_style": "schematic",
            "coil_style": "circle",
            "rail_hot": "L+",
            "rail_return": "0V",
        },
        "sheets": [
            {
                "id": "s1",
                "title": "Steuerspannung 24VDC - Not-Halt",
                "description": "Schütz und Klemmenleiste nach IEC 60204-1",
                "kind": "schematic",
                "drawing_style": "iec",
                "rail_hot": "L+",
                "rail_return": "0V",
                "page": {"size": "A3", "orientation": "landscape"},
                "components": [
                    {
                        "id": "S1",
                        "library_id": "iec.operators.estop",
                        "x": 70.0,
                        "y": 60.0,
                        "rotation": 90,
                        "properties": {"note": "Not-Halt Taster"},
                    },
                    {
                        "id": "K1",
                        "library_id": "iec.custom.safety_relay_2p",
                        "x": 70.0,
                        "y": 150.0,
                        "rotation": 90,
                        "properties": {"description": "Auxiliary control relay for safety circuit"},
                    },
                    {
                        "id": "K1-13",
                        "library_id": "iec.contactors.aux_no",
                        "x": 150.0,
                        "y": 70.0,
                        "rotation": 90,
                        "properties": {"parent": "K1", "terminals": "13-14"},
                    },
                    {
                        "id": "P1",
                        "library_id": "iec.operators.pilot_light",
                        "x": 150.0,
                        "y": 150.0,
                        "rotation": 90,
                        "properties": {"note": "Meldeleuchte Steuerspannung"},
                    },
                ],
                "wires": [
                    {"id": "w1", "from": "L+", "to": "S1.11", "net": "L+", "number": "L+"},
                    {"id": "w2", "from": "S1.12", "to": "K1.A1", "net": "N1", "number": "101"},
                    {"id": "w3", "from": "K1.A2", "to": "0V", "net": "0V", "number": "0V"},
                    {"id": "w4", "from": "L+", "to": "K1-13.13", "net": "L+", "number": "L+"},
                    {"id": "w5", "from": "K1-13.14", "to": "P1.X1", "net": "N2", "number": "102"},
                    {"id": "w6", "from": "P1.X2", "to": "0V", "net": "0V", "number": "0V"},
                ],
            }
        ],
    }
    project = Project.model_validate(doc)
    return Session(project=project, catalog=catalog)


def test_threshold_and_role_table_exports() -> None:
    assert default_confidence_threshold == pytest.approx(0.80)
    assert DEFAULT_CONFIDENCE_THRESHOLD == pytest.approx(0.80)
    assert "control_relay" in ROLE_PREFIX_TABLE
    assert ROLE_PREFIX_TABLE["control_relay"] == ("K", "CR")
    assert ROLE_PREFIX_TABLE["contactor"] == ("K", "M")
    assert ROLE_PREFIX_TABLE["circuit_breaker"] == ("Q", "CB")
    assert ROLE_PREFIX_TABLE["disconnect_switch"] == ("Q", "DISC")
    assert ROLE_PREFIX_TABLE["fuse"] == ("F", "FU")
    assert ROLE_PREFIX_TABLE["overload_relay"] == ("F", "OL")


def test_resolve_ambiguous_role_high_low_and_invalid() -> None:
    # 1. High confidence valid role (-K -> control_relay / CR, -Q -> disconnect_switch / DISC, -F -> fuse / FU)
    backend = RecordedJSONBackend(
        {
            "roles": {
                "K1": {
                    "role": "control_relay",
                    "prefix": "CR",
                    "confidence": 0.93,
                    "reason": "Aux NO contact feeds pilot light only; no motor load.",
                    "model": "recorded-json/v1",
                },
                "Q1": {
                    "role": "disconnect_switch",
                    "prefix": "DISC",
                    "confidence": 0.88,
                    "reason": "Non-automatic main isolator switch.",
                    "model": "recorded-json/v1",
                },
                "F1": {
                    "role": "fuse",
                    "prefix": "FU",
                    "confidence": 0.62,
                    "reason": "Uncertain whether fuse or thermal overload.",
                    "model": "recorded-json/v1",
                },
                "K_BAD": {
                    "role": "non_existent_role",
                    "prefix": "ZZ",
                    "confidence": 0.99,
                    "reason": "Hallucinated role name.",
                    "model": "recorded-json/v1",
                },
            }
        }
    )

    res_k = resolve_ambiguous_role(
        "K1",
        target="nema",
        library_id="iec.relays.cr",
        pins=["A1", "A2"],
        linked_contacts=["K1-13"],
        connected_components=["S1", "P1"],
        nearby_text="Control relay",
        backend=backend,
    )
    role, prefix, conf, reason, model = res_k
    assert (role, prefix, conf, model) == ("control_relay", "CR", 0.93, "recorded-json/v1")
    assert "pilot light" in reason
    assert res_k.valid is True
    assert res_k.accepted is True
    assert res_k.needs_review is False
    assert res_k.decision.reason_code == "ambiguous_role"
    assert res_k.decision.accepted is True
    assert res_k.decision.needs_review is False
    assert res_k.decision.model == "recorded-json/v1"
    assert res_k.decision.confidence == pytest.approx(0.93)
    assert res_k.decision.proposal["prefix"] == "CR"

    # -Q high confidence
    res_q = resolve_ambiguous_role(
        "Q1",
        target="nema",
        library_id="iec.switches.isolator_3p",
        backend=backend,
    )
    assert res_q.accepted is True
    assert res_q.prefix == "DISC"
    assert res_q.role == "disconnect_switch"

    # -F low confidence (< 0.80) -> rejected, needs_review=True
    res_f = resolve_ambiguous_role(
        "F1",
        target="nema",
        library_id="iec.protection.generic",
        backend=backend,
    )
    assert res_f.valid is True
    assert res_f.accepted is False
    assert res_f.needs_review is True
    assert res_f.decision.needs_review is True
    assert res_f.decision.accepted is False
    assert res_f.decision.confidence == pytest.approx(0.62)

    # Hallucinated role -> rejected by deterministic ROLE_PREFIX_TABLE validation
    res_bad = resolve_ambiguous_role(
        "K_BAD",
        target="nema",
        library_id="iec.relays.cr",
        backend=backend,
    )
    assert res_bad.valid is False
    assert res_bad.accepted is False
    assert res_bad.needs_review is True
    assert res_bad.decision.needs_review is True
    assert res_bad.decision.accepted is False


def test_suggest_symbol_counterpart_validation_gates() -> None:
    catalog = Library.load_builtin()

    # 1. Valid NEMA counterpart covering wired pins A1, A2
    backend_ok = RecordedJSONBackend(
        {
            "symbols": {
                "iec.custom.safety_relay_2p": {
                    "suggested_library_id": "nema.relays.cr",
                    "pin_map": {"A1": "A1", "A2": "A2"},
                    "confidence": 0.91,
                    "reason": "2-pin relay coil maps cleanly to nema.relays.cr.",
                    "model": "recorded-json/v1",
                }
            }
        }
    )
    sug_ok = suggest_symbol_counterpart(
        "K1",
        "iec.custom.safety_relay_2p",
        target="nema",
        wired_pins=["A1", "A2"],
        catalog=catalog,
        backend=backend_ok,
    )
    assert sug_ok.valid is True
    assert sug_ok.accepted is True
    assert sug_ok.needs_review is False
    assert sug_ok.suggested_library_id == "nema.relays.cr"
    assert sug_ok.decision.accepted is True

    # 2. Hallucinated non-existent library symbol -> rejected
    backend_hallucinated = RecordedJSONBackend(
        {
            "symbols": {
                "iec.custom.safety_relay_2p": {
                    "suggested_library_id": "nema.relays.non_existent_xyz",
                    "pin_map": {"A1": "A1", "A2": "A2"},
                    "confidence": 0.99,
                    "reason": "Invented symbol id.",
                    "model": "recorded-json/v1",
                }
            }
        }
    )
    sug_bad_lib = suggest_symbol_counterpart(
        "K1",
        "iec.custom.safety_relay_2p",
        target="nema",
        wired_pins=["A1", "A2"],
        catalog=catalog,
        backend=backend_hallucinated,
    )
    assert sug_bad_lib.valid is False
    assert sug_bad_lib.accepted is False
    assert sug_bad_lib.needs_review is True
    assert sug_bad_lib.decision.reason_code == "unmapped_symbol"
    assert sug_bad_lib.decision.needs_review is True

    # 3. Existing symbol in target library, but missing wired pin '98' -> rejected
    sug_missing_pin = suggest_symbol_counterpart(
        "K1",
        "iec.custom.safety_relay_2p",
        target="nema",
        wired_pins=["A1", "A2", "98"],
        catalog=catalog,
        backend=backend_ok,
    )
    assert sug_missing_pin.valid is False
    assert sug_missing_pin.accepted is False
    assert sug_missing_pin.needs_review is True
    assert "missing wired pins" in sug_missing_pin.reason

    # 4. Low confidence (< threshold) -> valid symbol, but not accepted; flagged for review
    backend_low = RecordedJSONBackend(
        {
            "symbols": {
                "iec.custom.safety_relay_2p": {
                    "suggested_library_id": "nema.relays.cr",
                    "pin_map": {"A1": "A1", "A2": "A2"},
                    "confidence": 0.55,
                    "reason": "Low confidence match.",
                    "model": "recorded-json/v1",
                }
            }
        }
    )
    sug_low = suggest_symbol_counterpart(
        "K1",
        "iec.custom.safety_relay_2p",
        target="nema",
        wired_pins=["A1", "A2"],
        catalog=catalog,
        backend=backend_low,
    )
    assert sug_low.valid is True
    assert sug_low.accepted is False
    assert sug_low.needs_review is True


def test_reword_sheet_notes_and_revert() -> None:
    session = _make_ambiguous_iec_session()
    sheet = session.project.sheet("s1")
    orig_title = sheet.title
    orig_desc = sheet.description
    orig_notes = list(session.project.properties["notes"])
    orig_comp_note = sheet.components[0].properties["note"]

    backend = RecordedJSONBackend({})
    decisions = reword_sheet_notes(
        session.project,
        target="nema",
        sheets=["s1"],
        backend=backend,
    )
    assert len(decisions) >= 4
    assert "Emergency Stop" in sheet.title
    assert "Control Voltage" in sheet.title
    assert "Contactor" in sheet.description
    assert "Terminal Block" in sheet.description
    assert "NFPA 79" in sheet.description
    assert any("Emergency Stop" in n for n in session.project.properties["notes"])
    assert any("Motor Circuit Protector" in n for n in session.project.properties["notes"])
    assert "Emergency Stop" in sheet.components[0].properties["note"]

    # Every decision preserves original text in before & proposal
    for dec in decisions:
        assert dec.reason_code == "note_reworded"
        assert dec.model == "recorded-json/v1"
        assert dec.confidence is not None and dec.confidence >= 0.80
        assert dec.before.get("original")
        assert dec.proposal.get("original") == dec.before.get("original")
        assert dec.proposal.get("text") != dec.before.get("original")

    # Revert restores exact original strings
    revert_reworded_notes(session.project, sheets=["s1"])
    assert sheet.title == orig_title
    assert sheet.description == orig_desc
    assert session.project.properties["notes"] == orig_notes
    assert sheet.components[0].properties["note"] == orig_comp_note


def test_rank_layout_candidates_disqualifies_hard_sheet_flags() -> None:
    session = _make_ambiguous_iec_session()
    # Build two candidates:
    # - "crowded": pre-flagged with component_overlap (hard flag)
    # - "clean": zero flags
    candidates = [
        {"id": "crowded", "flags": ["component_overlap"], "rungs": 12},
        {"id": "clean", "flags": [], "rungs": 6},
        {"id": "compact", "flags": [], "rungs": 8},
    ]
    # Even if AI tries to pick the disqualified "crowded" candidate, hard validation rejects it
    backend_bad_pick = RecordedJSONBackend(
        {
            "layouts": {
                "s1": {
                    "chosen_id": "crowded",
                    "confidence": 0.95,
                    "reason": "Prefer crowded layout.",
                    "model": "recorded-json/v1",
                }
            }
        }
    )
    rank_bad = rank_layout_candidates(
        session,
        "s1",
        candidates,
        backend=backend_bad_pick,
    )
    assert "crowded" in rank_bad.disqualified_ids
    assert rank_bad.chosen_id == "clean"
    assert rank_bad.accepted is False

    # When AI picks among valid flag-free candidates ("compact"), it is accepted
    backend_good_pick = RecordedJSONBackend(
        {
            "layouts": {
                "s1": {
                    "chosen_id": "compact",
                    "confidence": 0.91,
                    "reason": "Balanced rung spacing with zero overlaps.",
                    "model": "recorded-json/v1",
                }
            }
        }
    )
    rank_good = rank_layout_candidates(
        session,
        "s1",
        candidates,
        backend=backend_good_pick,
    )
    assert "crowded" in rank_good.disqualified_ids
    assert rank_good.chosen_id == "compact"
    assert rank_good.accepted is True
    assert rank_good.decision.reason_code == "layout_candidate"
    assert rank_good.decision.model == "recorded-json/v1"
    assert rank_good.decision.confidence == pytest.approx(0.91)


def test_end_to_end_ai_assisted_conversion_and_rules_only_parity(tmp_path: Path) -> None:
    # 1. Verify rules_only parity with Patch 4
    s_rules = _make_ambiguous_iec_session()
    s_default = _make_ambiguous_iec_session()

    out_rules = tmp_path / "rules.pcad.yaml"
    ws_rules = tmp_path / "rules.convert.json"
    res_rules = s_rules.convert_standard(
        "nema",
        out=out_rules,
        workspace=ws_rules,
        mode="rules_only",
    )

    out_default = tmp_path / "default.pcad.yaml"
    ws_default = tmp_path / "default.convert.json"
    res_default = s_default.convert_standard(
        "nema",
        out=out_default,
        workspace=ws_default,
    )

    assert res_rules["decisions"] == res_default["decisions"]
    assert res_rules["tag_map"] == res_default["tag_map"]
    assert res_rules["pin_rewrites"] == res_default["pin_rewrites"]
    assert res_rules["connection_check"]["ok"] is True
    # In rules_only mode, K1 (custom symbol) stays K1 and its symbol stays unmapped
    rules_comp_ids = {c.id for c in s_rules.project.sheet("s1").components}
    assert "K1" in rules_comp_ids
    k1_rules = next(c for c in s_rules.project.sheet("s1").components if c.id == "K1")
    assert k1_rules.library_id == "iec.custom.safety_relay_2p"

    # 2. Now run AI-assisted conversion with RecordedJSONBackend
    s_ai = _make_ambiguous_iec_session()
    before_project = copy.deepcopy(s_ai.project)
    before_edges = sheet_edges(before_project)

    ai_backend = RecordedJSONBackend(
        {
            "roles": {
                "K1": {
                    "role": "control_relay",
                    "prefix": "CR",
                    "confidence": 0.94,
                    "reason": "Safety control relay coil driving auxiliary contact to pilot light.",
                    "model": "recorded-json/v1",
                }
            },
            "symbols": {
                "iec.custom.safety_relay_2p": {
                    "suggested_library_id": "nema.relays.cr",
                    "pin_map": {"A1": "A1", "A2": "A2"},
                    "confidence": 0.92,
                    "reason": "Maps 2-pin IEC safety relay coil to NEMA control relay coil.",
                    "model": "recorded-json/v1",
                }
            },
        }
    )

    out_ai = tmp_path / "ai.pcad.yaml"
    ws_ai = tmp_path / "ai.convert.json"
    engine = ConversionEngine(
        s_ai,
        "nema",
        mode="ai_assisted",
        ai_backend=ai_backend,
        confidence_threshold=0.80,
    )
    res_ai = engine.run(out=out_ai, workspace=ws_ai)

    # K1 was resolved to control_relay -> renamed CR1, child K1-13 -> CR1-13
    ai_sheet = s_ai.project.sheet("s1")
    ai_comp_ids = {c.id for c in ai_sheet.components}
    assert "CR1" in ai_comp_ids
    assert "CR1-13" in ai_comp_ids
    cr1 = next(c for c in ai_sheet.components if c.id == "CR1")
    assert cr1.library_id == "nema.relays.cr"

    # Notes and sheet title were reworded to NEMA/NFPA 79 English
    assert "Emergency Stop" in ai_sheet.title
    assert "NFPA 79" in ai_sheet.description

    # Netlist and From-To connection gate must pass 100%
    ws_data = json.loads(ws_ai.read_text(encoding="utf-8"))
    assert ws_data["connection_check"]["ok"] is True
    assert res_ai["connection_check"]["ok"] is True
    after_edges = sheet_edges(s_ai.project)
    assert same_netlist(
        before_edges,
        after_edges,
        tag_map=res_ai["tag_map"],
        pin_rewrites=res_ai["pin_rewrites"],
        rail_map=res_ai["rail_map"],
    )
    conn_report = check_connections(
        before_project,
        s_ai.project,
        s_ai.catalog,
        tag_map=res_ai["tag_map"],
        pin_rewrites=res_ai["pin_rewrites"],
        rail_map=res_ai["rail_map"],
    )
    assert conn_report["ok"] is True

    # Decisions recorded with model and confidence in result, workspace JSON, and project.properties
    ai_decs = [d for d in res_ai["decisions"] if d.get("model") == "recorded-json/v1"]
    assert len(ai_decs) >= 3
    reason_codes = {d["reason_code"] for d in ai_decs}
    assert {"ambiguous_role", "unmapped_symbol", "note_reworded", "layout_candidate"} <= reason_codes
    for d in ai_decs:
        assert d.get("confidence") is not None

    prop_decs = s_ai.project.properties.get("last_conversion_decisions") or []
    assert any(d.get("model") == "recorded-json/v1" for d in prop_decs)

    # Determinism check: running again on a fresh session produces identical decisions & YAML
    s_ai2 = _make_ambiguous_iec_session()
    res_ai2 = start_conversion(
        s_ai2,
        "nema",
        out=tmp_path / "ai2.pcad.yaml",
        workspace=tmp_path / "ai2.convert.json",
        mode="ai_assisted",
        ai_backend=ai_backend,
    )
    assert res_ai["decisions"] == res_ai2["decisions"]

    # Reverting tags chunk restores original German/IEC notes and tag K1
    revert_chunk(s_ai, "tags", ws_ai)
    reverted_sheet = s_ai.project.sheet("s1")
    assert reverted_sheet.title == "Steuerspannung 24VDC - Not-Halt"
    assert reverted_sheet.description == "Schütz und Klemmenleiste nach IEC 60204-1"


def test_convert_dialog_has_mode_combo() -> None:
    from PySide6.QtWidgets import QApplication, QComboBox
    _app = QApplication.instance() or QApplication([])
    from panelcad.gui.convert_dialog import ConvertDialog

    session = _make_ambiguous_iec_session()
    dlg = ConvertDialog(session)
    combo = dlg.findChild(QComboBox, "convert-mode-combo")
    assert combo is not None
    assert combo.count() == 2
    assert combo.itemData(0) == "rules_only"
    assert combo.itemData(1) == "ai_assisted"
    assert dlg.conversion_mode() == "rules_only"
    combo.setCurrentIndex(1)
    assert dlg.conversion_mode() == "ai_assisted"
