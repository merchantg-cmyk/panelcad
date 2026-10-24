"""Patch 8: Conversion scorecard harness, 18-print varied IEC corpus, and golden NEMA From-To CSVs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import panelcad.convert.scorecard as scorecard_mod
from panelcad.convert import (
    CORPUS_IDS,
    GOLDEN_NEMA_FROM_TO_FILES,
    SCORER_VERSION,
    STATIC_CORPUS_GROUND_TRUTH,
    build_golden_nema_from_to_session,
    build_iec_conversion_corpus,
    compare_scorecard_modes,
    export_golden_nema_from_to_csv,
    run_conversion_scorecard,
    score_conversion_item,
    verify_golden_nema_from_to_csvs,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = ROOT / "tests" / "golden" / "wire_list"


@pytest.fixture(scope="module")
def corpus():
    return build_iec_conversion_corpus()


def test_static_ground_truth_never_calls_conversion_engine_or_ai_backend(monkeypatch):
    def _forbidden_engine(*args, **kwargs):
        raise AssertionError("build_iec_conversion_corpus must NEVER call ConversionEngine")

    def _forbidden_ai_backend(*args, **kwargs):
        raise AssertionError("build_iec_conversion_corpus must NEVER call ConfiguredConversionAIBackend")

    monkeypatch.setattr(scorecard_mod, "ConversionEngine", _forbidden_engine)
    monkeypatch.setattr(scorecard_mod, "ConfiguredConversionAIBackend", _forbidden_ai_backend)

    assert set(STATIC_CORPUS_GROUND_TRUTH.keys()) == set(CORPUS_IDS)
    built = build_iec_conversion_corpus()
    assert len(built) == 18
    for item in built:
        assert item.ground_truth.expected_tags
        assert item.ground_truth.expected_from_to
        assert item.ground_truth.expected_iec_from_to


def test_corpus_has_18_varied_synthetic_prints_and_no_forbidden_terms(corpus):
    assert len(corpus) == 18
    ids = tuple(item.item_id for item in corpus)
    assert ids == CORPUS_IDS
    categories = {item.category for item in corpus}
    assert categories >= {
        "motor_starter",
        "drive_system",
        "plc_io",
        "safety_circuit",
        "power_distribution",
        "multi_page",
        "numbering_convention",
        "ai_ambiguity_and_notes",
        "imported_scan",
    }

    # Verify neither scorecard.py nor this test file contains any banned token
    scorecard_src = Path(scorecard_mod.__file__).read_text(encoding="utf-8")
    test_src = Path(__file__).read_text(encoding="utf-8")
    assert not scorecard_mod.has_forbidden_corpus_terms(scorecard_src)
    assert not scorecard_mod.has_forbidden_corpus_terms(test_src)

    for item in corpus:
        assert not scorecard_mod.has_forbidden_corpus_terms(item.item_id)
        assert not scorecard_mod.has_forbidden_corpus_terms(item.description)
        session = item.build_session()
        assert not scorecard_mod.has_forbidden_corpus_terms(session.project.name)
        for sheet in session.project.sheets:
            assert not scorecard_mod.has_forbidden_corpus_terms(sheet.title or "")


def test_rules_only_and_ai_recorded_scorecard_modes_and_comparison(corpus, tmp_path: Path):
    cmp_path = tmp_path / "scorecard_comparison.json"
    comparison = compare_scorecard_modes(
        corpus,
        modes=("rules_only", "ai_recorded"),
        save_path=cmp_path,
        resumable=False,
    )
    assert cmp_path.is_file()
    assert comparison["scorer_version"] == SCORER_VERSION
    assert comparison["corpus_size"] == 18

    rules_rep = comparison["modes"]["rules_only"]
    ai_rep = comparison["modes"]["ai_recorded"]

    assert rules_rep["scorer_version"] == SCORER_VERSION
    assert ai_rep["scorer_version"] == SCORER_VERSION
    assert rules_rep["completed_count"] == 18
    assert ai_rep["completed_count"] == 18

    # Both modes must achieve 100% contact number accuracy, 100% round-trip From-To match,
    # 100% sheet flags clear rate, and 0.0% wrong-unflagged rate.
    for rep in (rules_rep, ai_rep):
        assert rep["contact_number_accuracy"] == 1.0
        assert rep["round_trip_from_to_ok"] is True
        assert rep["round_trip_from_to_match"] is True
        assert rep["round_trip_pass_rate"] == 1.0
        assert rep["sheet_flags_clear_rate"] == 1.0
        assert rep["wrong_unflagged_rate"] == 0.0
        assert rep["total_wrong_unflagged_count"] == 0

    # AI-recorded mode achieves 100% From-To connection F1 across all 18 prints,
    # and improves on rules-only where item 17 has custom symbols left unrenamed in rules-only.
    assert ai_rep["connection_f1"] == 1.0
    assert rules_rep["connection_f1"] >= 0.94

    # AI-recorded mode resolves the ambiguous roles, custom symbols, and German notes on item 17
    deltas = comparison["deltas"]["ai_recorded_vs_rules_only"]
    assert deltas["device_f1_delta"] > 0.0
    assert deltas["tag_accuracy_delta"] > 0.0
    assert deltas["connection_f1_delta"] > 0.0
    assert deltas["note_accuracy_delta"] > 0.0
    assert deltas["flagged_rate_delta"] < 0.0
    assert deltas["wrong_unflagged_rate_rules_only"] == 0.0
    assert deltas["wrong_unflagged_rate_ai_recorded"] == 0.0

    assert ai_rep["tag_accuracy"] == 1.0
    assert ai_rep["xref_accuracy"] == 1.0
    assert ai_rep["note_accuracy"] == 1.0

    # Ensure no sidecar .pcad.ai.jsonl files pollute the repository root
    assert list(ROOT.glob("*.pcad.ai.jsonl")) == []


def test_incremental_saving_and_resumable_progress(corpus, tmp_path: Path):
    subset = corpus[:3]
    jsonl_path = tmp_path / "progress.jsonl"

    # Run first 2 items and save incremental JSONL progress
    rep_partial = run_conversion_scorecard(
        subset[:2],
        mode="rules_only",
        save_path=jsonl_path,
        resumable=True,
    )
    assert rep_partial.completed_count == 2
    lines_after_2 = [ln for ln in jsonl_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines_after_2) == 2

    # Resume over all 3 items: first 2 should be loaded from jsonl_path, only 3rd appended
    rep_resumed = run_conversion_scorecard(
        subset,
        mode="rules_only",
        save_path=jsonl_path,
        resumable=True,
    )
    assert rep_resumed.completed_count == 3
    lines_after_3 = [ln for ln in jsonl_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines_after_3) == 3

    # Also verify JSON file resumability
    json_path = tmp_path / "progress.json"
    run_conversion_scorecard(subset[:2], mode="rules_only", save_path=json_path, resumable=True)
    saved_json = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved_json["completed_count"] == 2

    rep_json_resumed = run_conversion_scorecard(subset, mode="rules_only", save_path=json_path, resumable=True)
    assert rep_json_resumed.completed_count == 3


def test_golden_nema_from_to_csvs_match():
    for kind, filename in GOLDEN_NEMA_FROM_TO_FILES.items():
        path = GOLDEN_DIR / filename
        assert path.is_file(), f"Missing golden NEMA From-To CSV for {kind}: {path}"
        raw_bytes = path.read_bytes()
        assert b"\r\n" not in raw_bytes, f"Golden CSV {filename} must use LF line endings"
        content = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(content) > 5, f"Golden CSV {filename} looks empty"
        assert content[0].startswith("wire_number,from,to,")

    results = verify_golden_nema_from_to_csvs(GOLDEN_DIR)
    assert results == {"dol": True, "reversing": True, "plc": True}
