"""Design-check warnings for a project that was built from a print."""

from __future__ import annotations

from typing import Any

from panelcad.core.checks import Finding
from panelcad.importer.models import LOW_CONFIDENCE


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _print_wire_findings(items: list[Any]) -> list[Finding]:
    """One Problems row per unconnected wire number.

    The import summary counts unique numbers. A number printed twice is one
    row that says how many places it appeared, and the first place keeps the
    crop and the zoom point. A provisional landing says to review it. A number
    that never landed says to connect it by hand. Anything else keeps the
    grouped "was not connected" row.
    """

    from panelcad.importer.models import CONVENTIONAL_WIRES

    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for item in items:
        if not isinstance(item, dict) or str(item.get("kind") or "") != "wire":
            continue
        reason = str(item.get("reason") or "")
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        # A rail name is not a wire to connect by hand.
        if text.upper() in CONVENTIONAL_WIRES:
            continue
        if item.get("confirmed") or reason in _OWN_ROW_REASONS:
            continue
        if text not in groups:
            order.append(text)
            groups[text] = []
        groups[text].append(item)
    findings: list[Finding] = []
    for text in order:
        group = groups[text]
        first = group[0]
        label = text or "unreadable"
        places = len(group)
        reasons = {str(item.get("reason") or "") for item in group}
        if "provisional" in reasons:
            message = f"wire number {label} landed on an unmatched symbol — review"
        elif "connect_by_hand" in reasons:
            message = f"wire number {label} — connect by hand"
        else:
            message = f"wire number {label} was not connected"
        if places > 1:
            message += f" ({places} places)"
        extra: dict[str, str] = {}
        crop = str(first.get("crop") or "").strip()
        if crop:
            extra["crop"] = crop
        findings.append(
            Finding(
                "print_wire",
                "warning",
                message,
                sheet=str(first.get("sheet") or ""),
                ref=text,
                target=str(first.get("reason") or ""),
                x=_number_or_none(first.get("x")),
                y=_number_or_none(first.get("y")),
                extra=extra,
            )
        )
    return findings


# Agent-import reasons that keep their own Problems row (findings_from_payload).
_OWN_ROW_REASONS = frozenset({"contact_state", "needs_review", "not_in_evidence", "conflict", "outside_page"})


def findings_from_payload(payload: dict[str, Any]) -> list[Finding]:
    items = list(payload.get("items") or [])
    findings: list[Finding] = _print_wire_findings(items)
    for item in items:
        reason = str(item.get("reason") or "")
        text = str(item.get("text") or "")
        sheet = str(item.get("sheet") or "")
        kind = str(item.get("kind") or "item")
        confidence = float(item.get("confidence") or 0)
        if item.get("confirmed"):
            continue
        if reason == "contact_state":
            detail = str(item.get("detail") or "NO/NC disagrees with the symbol")
            findings.append(
                Finding(
                    "print_contact_state",
                    "warning",
                    f"needs-info: {text or 'contact'} — {detail}",
                    sheet=sheet,
                    ref=text,
                    target="contact",
                )
            )
            continue
        if reason == "needs_review":
            detail = str(item.get("detail") or "needs review")
            findings.append(
                Finding(
                    "print_needs_review",
                    "warning",
                    f"{text or kind} needs review — {detail}",
                    sheet=sheet,
                    ref=text,
                    target=kind,
                )
            )
            continue
        if reason == "not_in_evidence":
            findings.append(
                Finding(
                    "print_evidence",
                    "warning",
                    f"{text or kind} is not in the extracted text on {sheet or 'the print'}",
                    sheet=sheet,
                    ref=text,
                    target=kind,
                )
            )
            continue
        if reason == "conflict":
            findings.append(
                Finding(
                    "print_conflict",
                    "warning",
                    f"{kind} {text or 'item'} conflicts with another reading and was not used",
                    sheet=sheet,
                    ref=text,
                    target=kind,
                )
            )
            continue
        if reason == "outside_page":
            findings.append(
                Finding(
                    "print_evidence",
                    "warning",
                    f"{text or kind} is outside the page",
                    sheet=sheet,
                    ref=text,
                    target=kind,
                )
            )
            continue
        if kind == "wire" and (reason or str(text).strip()):
            continue
        if reason == "sheet_number" or str(item.get("field") or "") == "sheet_number":
            findings.append(
                Finding(
                    "print_sheet_number",
                    "warning",
                    f"sheet number on {sheet or 'import'} needs info — left blank",
                    sheet=sheet,
                    ref="",
                    target="sheet_number",
                )
            )
            continue
        if reason == "needs_info" and not text:
            findings.append(
                Finding(
                    "print_unreadable",
                    "warning",
                    f"needs info: {kind} on {sheet or 'import'} was not read — left blank",
                    sheet=sheet,
                    ref=text,
                    target=kind,
                )
            )
            continue
        if reason == "unreadable" or (not text and reason != "symbol_guess"):
            findings.append(
                Finding(
                    "print_unreadable",
                    "warning",
                    f"unreadable {kind} on {sheet or 'import'} — left blank",
                    sheet=sheet,
                    ref=text,
                    target=kind,
                )
            )
            continue
        if reason == "no_symbol":
            findings.append(
                Finding(
                    "print_unplaced",
                    "warning",
                    f"{text or 'device'} was read but no library symbol matched",
                    sheet=sheet,
                    ref=text,
                )
            )
            continue
        if reason in {"unconfirmed_symbol", "needs_info"}:
            findings.append(
                Finding(
                    "print_unplaced",
                    "warning",
                    f"NOT FOR BUILD: {text or 'device'} does not match a tag this sheet supports. "
                    "No millimetres. Kept off the BOM, the quote, and cutouts.",
                    sheet=sheet,
                    ref=text,
                )
            )
            continue
        if reason in {"no_pins", "unresolved"}:
            findings.append(
                Finding(
                    "print_wire",
                    "warning",
                    f"wire number {text or 'unreadable'} was not connected",
                    sheet=sheet,
                    ref=text,
                    target=reason,
                )
            )
            continue
        if reason == "symbol_guess" or confidence < LOW_CONFIDENCE:
            findings.append(
                Finding(
                    "print_low_confidence",
                    "warning",
                    f"{kind} {text} confidence {confidence:.2f}"
                    + (" — symbol inferred from the tag prefix" if reason == "symbol_guess" else ""),
                    sheet=sheet,
                    ref=text,
                    target=kind,
                )
            )
            continue
        if reason == "unknown":
            findings.append(
                Finding(
                    "print_unknown_part",
                    "warning",
                    f"catalog {text} is not in the part library — kept as read",
                    sheet=sheet,
                    ref=str(item.get("tag") or ""),
                    target=text,
                )
            )
    for xref in payload.get("unresolved_xrefs") or []:
        target = str(xref.get("target") or xref.get("text") or "")
        findings.append(
            Finding(
                "print_unresolved_xref",
                "warning",
                f"cross-reference {target or 'unreadable'} has no matching device",
                sheet=str(xref.get("sheet") or ""),
                ref=str(xref.get("tag") or ""),
                target=target,
            )
        )
    return findings
