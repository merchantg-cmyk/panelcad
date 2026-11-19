"""Quarantined preserve-mode scan policy.

Ladder is the default and only issuable layout mode for schematic scan rebuilds.
Preserve mode is retained only for panel/door/terminal/IO/network layout scans
and for vector/PDF/external/vision imports; preserve-mode schematic scans stay
blocked from issuing.
"""

from __future__ import annotations

from typing import Any

# Sheet kinds that keep scan placement (not a ladder rebuild).
PRESERVE_SCAN_KINDS: frozenset[str] = frozenset(
    {"panel_layout", "door_layout", "terminal", "network", "io"}
)

# Page-kind tokens from OCR that map onto preserve-placement sheets.
PRESERVE_SCAN_PAGE_KINDS: frozenset[str] = frozenset(
    {"panel_layout", "door_layout", "terminal_plan", "plc_io", "network"}
)

LADDER_MODE_ALIASES: frozenset[str] = frozenset({"ladder", "clean_ladder", "clean"})


def is_ladder_mode(mode: str | None) -> bool:
    return str(mode or "").strip().lower() in LADDER_MODE_ALIASES


def is_preserve_mode(mode: str | None) -> bool:
    token = str(mode or "").strip().lower()
    return token == "preserve"


def default_scan_layout_mode(stitched: dict[str, Any]) -> str:
    """Return ``ladder`` for schematic scans; ``preserve`` only for layout-only jobs.

    Electrical OCR + ladder rebuild is the schematic path. A backplate or a
    terminal strip has no rungs to redraw — keep the pixels' own placement.
    """

    sheets = list(stitched.get("sheets") or [])
    if not sheets:
        return "ladder"
    for sheet in sheets:
        page_kind = str(sheet.get("page_kind") or "").strip().lower()
        kind = str(sheet.get("kind") or "").strip().lower()
        if page_kind in PRESERVE_SCAN_PAGE_KINDS or kind in PRESERVE_SCAN_KINDS:
            continue
        return "ladder"
    return "preserve"


def resolve_effective_layout_mode(
    *,
    requested: str,
    stamped: str,
    scan_reader: bool,
    stitched: dict[str, Any],
) -> str:
    """Resolve effective layout mode, keeping ladder as the schematic scan default."""

    req = str(requested or "").strip().lower()
    st = str(stamped or "").strip().lower()
    if is_ladder_mode(req):
        return "ladder"
    if is_preserve_mode(req):
        return "preserve"
    if is_ladder_mode(st):
        return "ladder"
    if is_preserve_mode(st):
        return "preserve"
    if scan_reader:
        return default_scan_layout_mode(stitched)
    return "preserve"


def preserve_scan_issue_blocked(project: Any) -> bool:
    """True when ``project`` is a preserve-mode scan schematic that must not issue."""

    props = getattr(project, "properties", None) or {}
    pi = props.get("print_import") if isinstance(props, dict) else None
    if not isinstance(pi, dict):
        return False
    if str(pi.get("from_scan") or "").strip().lower() != "yes":
        return False
    if not is_preserve_mode(pi.get("layout_mode")):
        return False
    sheets = getattr(project, "sheets", None) or []
    return any(str(getattr(sheet, "kind", "") or "").strip().lower() == "schematic" for sheet in sheets)
