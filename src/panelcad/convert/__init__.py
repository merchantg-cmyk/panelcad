"""IEC <-> NEMA drawing conversion.

Submodules load on first use. Importing this package must not pull in the
print importer or OCR; opening a drawing only needs ``is_power_sheet``.
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {}


def _from(module: str, names: tuple[str, ...], *, attr: str | None = None) -> None:
    for name in names:
        _EXPORTS[name] = (module, attr or name)


_from(
    "panelcad.convert.ai_assist",
    (
        "DEFAULT_CONFIDENCE_THRESHOLD",
        "HARD_LAYOUT_FLAGS",
        "ROLE_PREFIX_TABLE",
        "ConfiguredConversionAIBackend",
        "ConversionAIBackend",
        "FakeConversionAIBackend",
        "LayoutRankingResult",
        "NoOpAIBackend",
        "NoteRewordResult",
        "RecordedJSONBackend",
        "RoleResolution",
        "SymbolSuggestion",
        "default_confidence_threshold",
        "rank_layout_candidates",
        "resolve_ai_backend",
        "resolve_ambiguous_role",
        "revert_reworded_notes",
        "reword_note_text",
        "reword_sheet_notes",
        "suggest_symbol_counterpart",
    ),
)
_from(
    "panelcad.convert.engine",
    (
        "ConversionEngine",
        "accept_chunk",
        "apply_conversion",
        "conversion_plan",
        "convert_standard",
        "next_chunk",
        "review",
        "rerun_chunk",
        "revert_chunk",
        "start_conversion",
    ),
)
_from(
    "panelcad.convert.import_convert",
    (
        "DEFAULT_IMPORT_CONFIDENCE_THRESHOLD",
        "ImportDeviceEntry",
        "ImportFromToRow",
        "ImportFromToSchedule",
        "ImportedConversionResult",
        "ImportedDeviceRecord",
        "ImportedFromToRow",
        "ImportedIECPrintDraft",
        "RecordedImportVisionBackend",
        "apply_from_to_corrections",
        "apply_import_from_to_edits",
        "build_iec_project_from_imported_from_to",
        "check_converted_sheet_layout",
        "clean_iec_tag",
        "convert_imported_iec_print",
        "extract_import_from_to",
        "import_iec_print_to_from_to",
        "split_endpoint_token",
        "synthesize_imported_ladder",
        "verify_imported_from_to",
    ),
)
_from(
    "panelcad.convert.from_to_check",
    (
        "ConnectionLost",
        "RevertRefused",
        "build_from_to_diff_rows",
        "check_connections",
        "verify_converted_from_to",
    ),
)
_EXPORTS["FROM_TO_SCORER_VERSION"] = ("panelcad.convert.from_to_check", "SCORER_VERSION")
_from(
    "panelcad.convert.layout_convert",
    (
        "consolidate_control_sheets",
        "consolidate_multipage_iec",
        "is_power_sheet",
    ),
)
_from(
    "panelcad.convert.plan",
    (
        "ALL_REASON_CODES",
        "DECISION_REASON_CODES",
        "SHEET_FLAG_REASON_CODES",
        "ConversionChunk",
        "ConversionDecision",
        "ConversionPlan",
        "decision_original_value",
        "decision_proposal_value",
        "resolve_conversion_decision",
        "sort_decisions_by_confidence",
        "valid_decision_options",
    ),
)
_from(
    "panelcad.convert.scorecard",
    (
        "AMBIGUOUS_ROLES_RECORDINGS",
        "CORPUS_IDS",
        "GOLDEN_NEMA_FROM_TO_FILES",
        "SCORER_VERSION",
        "STATIC_CORPUS_GROUND_TRUTH",
        "CorpusGroundTruth",
        "CorpusItem",
        "CorpusPrintItem",
        "ItemScorecardResult",
        "ScorecardReport",
        "build_golden_nema_from_to_session",
        "build_iec_conversion_corpus",
        "compare_scorecard_modes",
        "export_golden_nema_from_to_csv",
        "run_conversion_scorecard",
        "score_conversion_item",
        "verify_golden_nema_from_to_csvs",
    ),
)
_from(
    "panelcad.convert.table",
    (
        "convert_terminal_tag",
        "format_wire_number",
        "rename_tag_by_role",
        "role_entry_for",
        "role_prefix_for",
    ),
)

__all__ = [
    "ALL_REASON_CODES",
    "ConfiguredConversionAIBackend",
    "ConversionAIBackend",
    "ConversionChunk",
    "ConversionDecision",
    "ConversionEngine",
    "ConversionPlan",
    "DECISION_REASON_CODES",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "FakeConversionAIBackend",
    "HARD_LAYOUT_FLAGS",
    "LayoutRankingResult",
    "NoOpAIBackend",
    "NoteRewordResult",
    "ROLE_PREFIX_TABLE",
    "RecordedJSONBackend",
    "RoleResolution",
    "SHEET_FLAG_REASON_CODES",
    "SymbolSuggestion",
    "accept_chunk",
    "apply_conversion",
    "conversion_plan",
    "convert_standard",
    "convert_terminal_tag",
    "default_confidence_threshold",
    "format_wire_number",
    "next_chunk",
    "rank_layout_candidates",
    "rename_tag_by_role",
    "resolve_ai_backend",
    "resolve_ambiguous_role",
    "revert_reworded_notes",
    "review",
    "rerun_chunk",
    "revert_chunk",
    "reword_note_text",
    "reword_sheet_notes",
    "role_entry_for",
    "role_prefix_for",
    "start_conversion",
    "DEFAULT_IMPORT_CONFIDENCE_THRESHOLD",
    "ImportedConversionResult",
    "ImportedDeviceRecord",
    "ImportedFromToRow",
    "ImportedIECPrintDraft",
    "RecordedImportVisionBackend",
    "apply_from_to_corrections",
    "build_iec_project_from_imported_from_to",
    "clean_iec_tag",
    "convert_imported_iec_print",
    "import_iec_print_to_from_to",
    "ConnectionLost",
    "FROM_TO_SCORER_VERSION",
    "RevertRefused",
    "build_from_to_diff_rows",
    "check_connections",
    "consolidate_control_sheets",
    "consolidate_multipage_iec",
    "is_power_sheet",
    "verify_converted_from_to",
    "decision_original_value",
    "decision_proposal_value",
    "resolve_conversion_decision",
    "sort_decisions_by_confidence",
    "split_endpoint_token",
    "suggest_symbol_counterpart",
    "valid_decision_options",
    "AMBIGUOUS_ROLES_RECORDINGS",
    "CORPUS_IDS",
    "GOLDEN_NEMA_FROM_TO_FILES",
    "SCORER_VERSION",
    "STATIC_CORPUS_GROUND_TRUTH",
    "CorpusGroundTruth",
    "CorpusItem",
    "CorpusPrintItem",
    "ItemScorecardResult",
    "ScorecardReport",
    "build_golden_nema_from_to_session",
    "build_iec_conversion_corpus",
    "compare_scorecard_modes",
    "export_golden_nema_from_to_csv",
    "run_conversion_scorecard",
    "score_conversion_item",
    "verify_golden_nema_from_to_csvs",
    "ImportDeviceEntry",
    "ImportFromToRow",
    "ImportFromToSchedule",
    "apply_import_from_to_edits",
    "check_converted_sheet_layout",
    "extract_import_from_to",
    "synthesize_imported_ladder",
    "verify_imported_from_to",
]


def __getattr__(name: str) -> Any:
    spec = _EXPORTS.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(spec[0])
    value = getattr(module, spec[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
