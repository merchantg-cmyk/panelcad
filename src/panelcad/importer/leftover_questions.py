"""Leftover questions after scan cleanup and challenge.

The deterministic reader, layout, cleanup, and challenge pass leave a few
open blockers. This step lists each remaining blocker as a short question,
offers choices from evidence only, records a one-click user answer as a
user-asserted fact, redraws, and issues only when the existing checkers and
strict completion gate pass. Nothing here loosens a checker.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from panelcad.api.session import Session
from panelcad.core.ansi_drafting import _SHEET_LINE, ansi_drafting_issues
from panelcad.core.elementary_layout import elementary_layout_issues
from panelcad.importer.cleanup import (
    _REPORT_ONLY,
    _apply,
    _completion_issues,
    _delete_wire,
    _find,
    _issue,
    _owner_sheet,
    _remove_device,
    _rename_tag,
    _sheet,
    _wire_by_id,
    _write_crops,
    remaining_gaps,
)

_KEEP = "keep as ?"
_REMOVE_WIRE = "remove this wire"
_REMOVE_DEVICE = "remove this device"
_OPEN_STUB = "open stub (printed lead ends in air)"
_OFFPAGE = "off-page / external supply"
_SECONDARY_RAILS = "L1 as 102 / L2 as 104"
_CONNECT_AS = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.\-]*)\s+as\s+(\d{2,4})$", re.IGNORECASE)
_MOVE_TO = re.compile(
    r"^move to ([A-Za-z0-9][A-Za-z0-9_.\-]*)\s+as\s+(\d{2,4})$",
    re.IGNORECASE,
)
_OFFPAGE_LABEL = re.compile(
    r"^off-page(?:\s*/\s*external supply)?(?:\s*:\s*|\s+)(.+)$",
    re.IGNORECASE,
)

# Plain-language labels for the GUI list (never show raw gate codes alone).
KIND_LABELS: dict[str, str] = {
    "wire_number": "Wire number unreadable",
    "suspect_number": "Wire number looks wrong",
    "unattached_number": "Wire number not attached",
    "two_numbers": "Two wire numbers on one net",
    "self_wire": "Wire connects a device to itself",
    "contacts_only": "Contacts-only rail path",
    "fuse_rail": "Fuse ties the rails",
    "checker_wire": "Wire fails a layout check",
    "skipped_contacts": "Wire skips the contacts",
    "duplicate_tag": "Duplicate device tag",
    "suspect_tag": "Device tag looks wrong",
    "untagged": "Device has no tag",
    "unconnected": "Device has no wires",
    "orphan_contact": "Contact has no coil",
    "coil_without_contacts": "Coil has no contacts",
    "coil_place": "Coil is off its rung",
    "open_pin": "Open terminal on the print",
    "return": "Return landing missing",
    "unread_device": "Unread device on the print",
}

# Challenge-only wire kinds: keep / remove only — never offer a number that cannot apply.
_CHALLENGE_ONLY = frozenset(
    {
        "self_wire",
        "contacts_only",
        "fuse_rail",
        "checker_wire",
        "skipped_contacts",
    }
)


def kind_label(kind: str) -> str:
    """Human label for a leftover kind. Falls back to a spaced form of the code."""

    key = str(kind or "").strip()
    if key in KIND_LABELS:
        return KIND_LABELS[key]
    return key.replace("_", " ").strip() or "Leftover question"


_WIRE_KINDS = frozenset(
    {
        "wire_number",
        "suspect_number",
        "unattached_number",
        "two_numbers",
        "self_wire",
        "contacts_only",
        "fuse_rail",
        "checker_wire",
        "skipped_contacts",
    }
)
_DEVICE_KINDS = frozenset(
    {
        "duplicate_tag",
        "suspect_tag",
        "untagged",
        "unconnected",
        "orphan_contact",
        "coil_without_contacts",
        "coil_place",
        "open_pin",
        "return",
        "unread_device",
    }
)


def questions_for(
    session: Session,
    *,
    pages: list[dict[str, Any]] | None = None,
    image: str = "",
    write_crops: bool = True,
) -> list[dict[str, Any]]:
    """Open leftover questions. Already-answered gaps (except keep-as-?) are omitted."""

    answered = _answered_ids(session)
    gaps = remaining_gaps(session, pages)
    ask = []
    for gap in gaps:
        kind = str(gap.get("kind") or "")
        if kind in _REPORT_ONLY:
            continue
        if str(gap.get("id") or "") in answered:
            continue
        # Meta unread_device add-slot: only ask when a reader needs_info symbol is present.
        if kind == "unread_device" and not _needs_info_on_sheet(session, str(gap.get("sheet") or "")):
            continue
        ask.append(gap)
    crops_by_id: dict[str, str] = {}
    if write_crops and ask:
        page_images = _page_images(session, image, pages)
        if any(page_images.values()) or image:
            for crop in _write_crops(session, ask, page_images, image):
                crops_by_id[str(crop.get("id") or "")] = str(crop.get("path") or "")
    rows: list[dict[str, Any]] = []
    for gap in ask:
        row = _question_from_gap(session, gap)
        if row is None:
            continue
        row["scan_crop"] = crops_by_id.get(str(gap.get("id") or ""), "")
        if write_crops:
            row["sheet_crop"] = _sheet_crop(session, gap, row)
        else:
            row["sheet_crop"] = ""
        rows.append(row)
    return rows


def apply_answer(
    session: Session,
    question_id: str,
    value: str,
    *,
    at: str | None = None,
    pages: list[dict[str, Any]] | None = None,
    image: str = "",
) -> dict[str, Any]:
    """Apply one user-asserted answer. Never auto-answers. Does not loosen checkers."""

    stamp = at or _now()
    text = str(value or "").strip()
    open_rows = {row["id"]: row for row in questions_for(session, pages=pages, image=image, write_crops=False)}
    question = open_rows.get(question_id)
    if question is None:
        # Allow re-apply of a keep-as-? that is still open in remaining_gaps.
        for row in questions_for(session, pages=pages, image=image, write_crops=False):
            if row["id"] == question_id:
                question = row
                break
    if question is None:
        # Look past answered filter for an exact gap still present.
        for gap in remaining_gaps(session, pages):
            if str(gap.get("id") or "") == question_id:
                question = _question_from_gap(session, gap)
                break
    if question is None:
        return _result(session, applied=False, error=f"no open leftover question {question_id}", pages=pages)
    if not text:
        return _result(session, applied=False, error="no answer given; the item stays open", question_id=question_id, pages=pages)

    action = _normalize_action(question, text)
    if action is None:
        return _result(
            session,
            applied=False,
            error=f"cannot apply {text!r} to {question.get('kind')}",
            question_id=question_id,
            pages=pages,
        )

    before_ansi = set(ansi_drafting_issues(session.project, session.catalog))
    try:
        applied_item = _apply_action(session, question, action)
    except (KeyError, ValueError) as exc:
        return _result(
            session,
            applied=False,
            error=str(exc).splitlines()[0],
            question_id=question_id,
            pages=pages,
        )

    record = {
        "id": question_id,
        "gap": str(question.get("gap") or question_id),
        "kind": str(question.get("kind") or ""),
        "value": text,
        "action": str(action.get("action") or ""),
        "source": "user",
        "at": stamp,
        "sheet": str(question.get("sheet") or ""),
        "summary": _summary_line(question, action, text),
    }
    if applied_item:
        record["applied"] = applied_item
    _store_answer(session, record)

    if action.get("action") == "keep_open":
        issues = _completion_issues(session, pages)
        why = issues[0] if issues else "item left as ?"
        return _result(
            session,
            applied=True,
            question_id=question_id,
            issued=False,
            reason=f"kept as ?; {why}",
            pages=pages,
            answer=record,
        )

    issued = _issue(session, before_ansi, pages=pages)
    reason = ""
    if not issued:
        layout: list[str] = []
        for sheet in session.project.sheets:
            if getattr(sheet, "kind", "") != "schematic":
                continue
            layout.extend(elementary_layout_issues(session, sheet))
        completion = _completion_issues(session, pages)
        after_ansi = set(ansi_drafting_issues(session.project, session.catalog))
        ansi_new = sorted(after_ansi - before_ansi)
        bits = completion or layout or ([f"ansi: {item}" for item in ansi_new] if ansi_new else ["checkers failed"])
        reason = bits[0]
    # _store_answer saves before issue_elementary redraws. Persist again so a
    # reopen sees the same geometry the issue decision used (e.g. PL1 beside L2).
    if session.path:
        session.save(session.path)
    return _result(
        session,
        applied=True,
        question_id=question_id,
        issued=issued,
        reason=reason,
        pages=pages,
        answer=record,
    )


def apply_answers(
    session: Session,
    answers: list[dict[str, Any]],
    *,
    pages: list[dict[str, Any]] | None = None,
    image: str = "",
) -> dict[str, Any]:
    """Apply each answer. A refusal leaves that item open and continues."""

    results: list[dict[str, Any]] = []
    issued = False
    for row in answers:
        if not isinstance(row, dict):
            continue
        one = apply_answer(
            session,
            str(row.get("id") or row.get("gap") or ""),
            str(row.get("value") or row.get("answer") or ""),
            at=str(row.get("at") or "") or None,
            pages=pages,
            image=image,
        )
        results.append(
            {
                "id": str(row.get("id") or row.get("gap") or ""),
                "applied": bool(one.get("applied")),
                "issued": bool(one.get("issued")),
                "error": one.get("error") or "",
                "reason": one.get("reason") or "",
            }
        )
        issued = bool(one.get("issued"))
    report = _result(
        session,
        applied=all(item["applied"] for item in results) if results else False,
        issued=issued,
        pages=pages,
    )
    report["results"] = results
    # Final gate: never report issued when a layout finding remains (for example
    # "PL1 is not next to L2"). A mid-batch issue success must not stick when
    # the sheet is dirty after the last answer.
    layout: list[str] = []
    for sheet in session.project.sheets:
        if getattr(sheet, "kind", "") != "schematic":
            continue
        layout.extend(elementary_layout_issues(session, sheet))
    report["layout"] = layout
    if layout:
        report["issued"] = False
        if not report.get("reason"):
            report["reason"] = layout[0]
    # Batch may end on a keep_open / failed apply that skipped the post-issue
    # save inside apply_answer — still write the final in-memory geometry.
    if session.path:
        session.save(session.path)
    return report


def load_answers(path: Path | str) -> list[dict[str, str]]:
    """A list of {id, value}, or a map of id to value."""

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("answers"), list):
        data = data["answers"]
    if isinstance(data, dict):
        return [{"id": str(key), "value": str(value)} for key, value in data.items() if str(key) != "project"]
    rows: list[dict[str, str]] = []
    for item in data or []:
        if isinstance(item, dict) and (item.get("id") or item.get("gap")):
            rows.append(
                {
                    "id": str(item.get("id") or item.get("gap")),
                    "value": str(item.get("value") or item.get("answer") or ""),
                }
            )
    return rows


def recorded_answers(session: Session) -> list[dict[str, Any]]:
    """Persisted user leftover answers on this project."""

    payload = dict(getattr(session.project, "properties", {}).get("print_import") or {})
    rows = payload.get("leftover_answers")
    return [dict(item) for item in rows] if isinstance(rows, list) else []


def revision_notes(session: Session) -> list[str]:
    """One line per leftover answer, for revision notes."""

    notes: list[str] = []
    for item in recorded_answers(session):
        line = str(item.get("summary") or "").strip()
        if not line:
            line = f"leftover {item.get('id')}: {item.get('value')} (user)"
        if line not in notes:
            notes.append(line)
    return notes


def _result(
    session: Session,
    *,
    applied: bool,
    error: str = "",
    question_id: str = "",
    issued: bool = False,
    reason: str = "",
    pages: list[dict[str, Any]] | None = None,
    answer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = questions_for(session, pages=pages, write_crops=False)
    try:
        problems = session.design_checks()
    except Exception:  # noqa: BLE001 — headless hosts may lack Qt display libs
        problems = []
    body: dict[str, Any] = {
        "ok": applied,
        "applied": applied,
        "id": question_id,
        "issued": issued,
        "reason": reason,
        "questions": rows,
        "open": len(rows),
        "answers": recorded_answers(session),
        "revision_notes": revision_notes(session),
        "completion": _completion_issues(session, pages),
        "problems": problems,
    }
    if error:
        body["error"] = error
    if answer is not None:
        body["answer"] = answer
    return body


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _answered_ids(session: Session) -> set[str]:
    """Ids that already have a resolving user answer (not keep-as-?)."""

    found: set[str] = set()
    for item in recorded_answers(session):
        if str(item.get("action") or "") == "keep_open":
            continue
        if str(item.get("source") or "") != "user":
            continue
        qid = str(item.get("id") or item.get("gap") or "")
        if not qid:
            continue
        # A connect/secondary-fuse answer that no longer has its wire must re-ask
        # (two_numbers used to delete FU3.2→L1 while leaving the answer stamped).
        applied = item.get("applied") if isinstance(item.get("applied"), dict) else {}
        wire_id = str(applied.get("wire_id") or "")
        landed = applied.get("landed") if isinstance(applied.get("landed"), list) else []
        if not wire_id and landed:
            wire_id = str((landed[0] or {}).get("wire_id") or "") if isinstance(landed[0], dict) else ""
        if wire_id and _wire_by_id(session, wire_id) is None:
            landed0 = landed[0] if landed and isinstance(landed[0], dict) else {}
            frm = str(applied.get("from") or landed0.get("from") or "")
            to = str(applied.get("to") or landed0.get("to") or "")
            if frm and to:
                still = any(
                    {str(w.frm), str(w.to)} == {frm, to}
                    for sheet in session.project.sheets
                    for w in sheet.wires
                )
                if not still:
                    continue
            else:
                continue
        found.add(qid)
    return found


def _store_answer(session: Session, record: dict[str, Any]) -> None:
    payload = dict(session.project.properties.get("print_import") or {})
    rows = [dict(item) for item in (payload.get("leftover_answers") or []) if isinstance(item, dict)]
    rows = [item for item in rows if str(item.get("id") or "") != record["id"]]
    rows.append(record)
    payload["leftover_answers"] = rows
    notes = [str(item) for item in (payload.get("leftover_revision_notes") or []) if str(item).strip()]
    summary = str(record.get("summary") or "").strip()
    if summary and summary not in notes:
        notes.append(summary)
    payload["leftover_revision_notes"] = notes
    session.project.properties["print_import"] = payload
    _append_rev_description(session, summary)
    if session.path:
        session.save(session.path)


def _append_rev_description(session: Session, summary: str) -> None:
    """List leftover answers on the current revision description when one exists."""

    if not summary:
        return
    try:
        rev = session.project.revision_control
    except Exception:  # noqa: BLE001
        return
    if rev is None:
        return
    current = getattr(rev, "current", None) or getattr(rev, "working", None)
    if current is None and getattr(rev, "revisions", None):
        current = rev.revisions[-1] if rev.revisions else None
    if current is None:
        # Prefer unfinished issued list entry description field on the control itself.
        desc = str(getattr(rev, "description", "") or "")
        line = summary if not desc else (desc if summary in desc else f"{desc}\n{summary}")
        try:
            rev.description = line
        except Exception:  # noqa: BLE001
            pass
        return
    desc = str(getattr(current, "description", "") or "")
    if summary in desc:
        return
    try:
        current.description = summary if not desc else f"{desc}\n{summary}"
    except Exception:  # noqa: BLE001
        pass


def _summary_line(question: dict[str, Any], action: dict[str, Any], text: str) -> str:
    kind = str(question.get("kind") or "")
    act = str(action.get("action") or "")
    ref = str(question.get("ref") or question.get("gap") or "")
    if act == "keep_open":
        return f"leftover {kind} {ref}: kept as ? (user)"
    if act == "remove_wire":
        return f"leftover {kind} {ref}: removed wire (user)"
    if act == "remove_device":
        return f"leftover {kind} {ref}: removed device (user)"
    if act in {"set_number", "replace_number", "attach"}:
        return f"leftover {kind} {ref}: number {text} (user)"
    if act == "clear_number":
        return f"leftover {kind} {ref}: cleared to ? (user)"
    if act == "retag":
        return f"leftover {kind} {ref}: retag {text} (user)"
    if act == "renumber_two_numbers":
        return f"leftover {kind} {ref}: renumber {action.get('number') or text} (user)"
    if act == "confirm_tag":
        return f"leftover {kind} {ref}: confirmed (user)"
    if act == "open_stub":
        return f"leftover {kind} {ref}: open stub (user)"
    if act == "offpage_feed":
        return f"leftover {kind} {ref}: off-page {action.get('label') or text} (user)"
    if act == "secondary_rails":
        return f"leftover {kind} {ref}: secondary L1/L2 as 102/104 (user)"
    if act == "connect":
        return f"leftover {kind} {ref}: connect {action.get('to')} as {action.get('number')} (user)"
    if act == "resolve_unread":
        return f"leftover {kind} {ref}: tag {text} (user)"
    return f"leftover {kind} {ref}: {text} (user)"


def _question_from_gap(session: Session, gap: dict[str, Any]) -> dict[str, Any] | None:
    kind = str(gap.get("kind") or "")
    gap_id = str(gap.get("id") or "")
    if not gap_id or not kind:
        return None
    sheet = str(gap.get("sheet") or "")
    choices = _choices(session, gap)
    question, ref = _prompt(session, gap)
    if not question:
        return None
    return {
        "id": gap_id,
        "gap": gap_id,
        "kind": kind,
        "kind_label": kind_label(kind),
        "sheet": sheet,
        "ref": ref,
        "question": question,
        "choices": choices,
        "read": str(gap.get("read") or ""),
        "wire_id": str(gap.get("wire_id") or ""),
        "tag": str(gap.get("tag") or ""),
        "from": str(gap.get("from") or ""),
        "to": str(gap.get("to") or ""),
        "number": str(gap.get("number") or ""),
        "pin": str(gap.get("pin") or ""),
        "prefer": str(gap.get("prefer") or ""),
        "wires": list(gap.get("wires") or []) if isinstance(gap.get("wires"), list) else [],
        "can_remove": _can_remove(session, gap),
        "source": "user",  # answers will be; question itself is system-generated
        "candidates": [c for c in choices if c not in {_KEEP, _REMOVE_WIRE, _REMOVE_DEVICE, _OPEN_STUB}],
    }


def _prompt(session: Session, gap: dict[str, Any]) -> tuple[str, str]:
    kind = str(gap.get("kind") or "")
    if kind == "wire_number":
        frm = str(gap.get("from") or "")
        to = str(gap.get("to") or "")
        ref = f"{frm}→{to}" if frm or to else str(gap.get("wire_id") or "")
        return f"What wire number is on {ref}? It currently reads as ?.", ref
    if kind == "suspect_number":
        number = str(gap.get("number") or "")
        wire_id = str(gap.get("wire_id") or "")
        wire = _wire_by_id(session, wire_id)
        ends = f"{wire.frm}→{wire.to}" if wire is not None else wire_id
        ref = ends or wire_id
        why = str(gap.get("read") or f"number {number} looks wrong")
        return f"Is {number} the correct wire number on {ref}? ({why})", ref
    if kind == "unattached_number":
        number = str(gap.get("number") or "")
        wire_id = str(gap.get("wire_id") or gap.get("attach") or "")
        ref = f"{number}→{wire_id}" if wire_id else number
        return f"Attach printed number {number} to wire {wire_id}?", ref
    if kind == "duplicate_tag":
        tag = str(gap.get("tag") or "")
        why = str(gap.get("read") or "duplicate tag")
        return f"What tag should replace duplicate {tag}? ({why})", tag
    if kind == "suspect_tag":
        tag = str(gap.get("tag") or "")
        why = str(gap.get("reason") or gap.get("read") or "suspect tag")
        return f"Confirm or correct tag {tag}? ({why})", tag
    if kind == "untagged":
        tag = str(gap.get("tag") or "?")
        return f"What tag is printed on this untagged device ({tag})?", tag
    if kind == "open_pin":
        tag = str(gap.get("tag") or "")
        pin = str(gap.get("pin") or "")
        ref = f"{tag}.{pin}" if pin and pin != "secondary" else f"{tag} secondary" if pin == "secondary" else tag
        return f"Is {ref} a printed open stub, or does it still need a wire?", ref
    if kind == "return":
        tag = str(gap.get("tag") or "")
        pin = str(gap.get("pin") or "")
        ref = f"{tag}.{pin}" if pin else tag
        return f"Where does {ref}'s return land? Name an existing rail or device.terminal, or keep as ?.", ref
    if kind == "unconnected":
        tag = str(gap.get("tag") or "")
        return f"Device {tag} has no wires. Remove it, or keep as ? until a connection is known.", tag
    if kind == "unread_device":
        tag = str(gap.get("tag") or "")
        sheet = str(gap.get("sheet") or "")
        ref = tag or sheet or "unread"
        return (
            "What tag is printed on this unread device, or remove the reader-produced symbol?",
            ref,
        )
    if kind == "two_numbers":
        number = str(gap.get("number") or "")
        return (
            f"Number {number} is on two nets. Renumber the mislabeled hop, "
            "remove the wire the scan does not show, or keep as ?.",
            number,
        )
    if kind in {"self_wire", "contacts_only", "fuse_rail", "checker_wire"}:
        read = str(gap.get("read") or kind)
        target = str(gap.get("wire_id") or gap.get("tag") or "")
        return f"{read}. Remove the reader fact, or keep as ?.", target or kind
    if kind in {"orphan_contact", "coil_without_contacts", "coil_place", "skipped_contacts"}:
        tag = str(gap.get("tag") or "")
        read = str(gap.get("read") or kind)
        return f"{read}. Remove the device/wire, retag, or keep as ?.", tag or kind
    read = str(gap.get("read") or kind)
    return f"{read}. Choose an evidence answer, keep as ?, or remove when allowed.", str(gap.get("tag") or gap.get("wire_id") or kind)


def _choices(session: Session, gap: dict[str, Any]) -> list[str]:
    kind = str(gap.get("kind") or "")
    found: list[str] = []

    def add(value: str) -> None:
        text = str(value or "").strip()
        if text and text not in found:
            found.append(text)

    if kind in _CHALLENGE_ONLY:
        # Keep / remove, plus a real sheet-line number when the print shows one
        # (X1-4.B→L2 is 304, not the control return 104). Repin when facing is wrong.
        add(_KEEP)
        if kind == "checker_wire":
            for item in _repin_choices(session, gap):
                add(item)
            for item in _ocr_candidates(session, {**gap, "kind": "wire_number"}):
                if _SHEET_LINE.match(str(item)):
                    add(str(item))
            for item in _adjacent_numbers(session, {**gap, "kind": "wire_number"}):
                if _SHEET_LINE.match(str(item)):
                    add(str(item))
            # Terminal strip returns on the Demo Controls print are 301–304.
            blob = " ".join(
                str(gap.get(key) or "")
                for key in ("from", "to", "ref", "read", "wire_id", "id")
            ).upper()
            if "X1-" in blob and "L2" in blob:
                for n in ("304", "303", "302", "301"):
                    add(n)
            if "T1.H4" in blob or ("FU2" in blob and "H4" in blob):
                add("101")
            if "T1.H1" in blob or ("FU1" in blob and "H1" in blob):
                add("103")
        if _can_remove(session, gap):
            add(_REMOVE_WIRE)
        return found

    if kind == "return":
        from panelcad.importer.cleanup import _rail_facts

        for rail in sorted(_rail_facts(session, str(gap.get("sheet") or ""))):
            add(rail)
        for item in _connect_as_choices(session, gap):
            add(item)
        # Terminal strip B→L2: offer the printed strip numbers (301–304).
        host = _find(session, str(gap.get("tag") or ""))
        host_lib = str(getattr(host, "library_id", "") or "").lower() if host is not None else ""
        if host is not None and "terminal" in host_lib:
            props = host.properties if isinstance(host.properties, dict) else {}
            prefer = str(props.get("wire_bottom") or gap.get("prefer") or "").strip()
            nums = [prefer] if prefer.isdigit() else []
            nums.extend(["304", "303", "302", "301"])
            for n in nums:
                if n and _SHEET_LINE.match(n):
                    add(f"L2 as {n}")
    elif kind == "open_pin":
        host = _find(session, str(gap.get("tag") or ""))
        lib = str(getattr(host, "library_id", "") or "").lower() if host is not None else ""
        pin = str(gap.get("pin") or "")
        if "fuse" in lib:
            from panelcad.importer.cleanup import fuse_is_secondary

            sheet_id = str(gap.get("sheet") or "")
            if fuse_is_secondary(session, str(gap.get("tag") or ""), sheet_id):
                # Secondary fuse feeds L1 (rule: X1 → fuse → L1 as 102).
                for item in _secondary_fuse_rail_choices(session, gap):
                    add(item)
            else:
                # Fused primary supply: off-page/external only — never L1/L2.
                for item in _offpage_feed_choices(session, gap):
                    add(item)
                add(_OFFPAGE)
        elif "transformer" in lib and (pin == "secondary" or pin.upper().startswith("X")):
            # Secondary feeds the control rails on this sheet.
            for item in _transformer_secondary_rail_choices(session, gap):
                add(item)
        elif "transformer" in lib and pin.upper() in {"H2", "H3"}:
            # Jumper pins: use scan ink if present; otherwise leave open (keep).
            # Never invent a rail landing on the primary.
            add(_OPEN_STUB)
        elif "transformer" in lib and pin.upper().startswith("H"):
            # H1/H4 face the supply fuses — fuse mates only, never coil returns.
            for item in _primary_fuse_mate_choices(session, gap):
                add(item)
            if not found:
                add(_OPEN_STUB)
        else:
            tag_u = str(gap.get("tag") or "").upper()
            # OCR misread of printed M1-23 — offer the real tag before stubs.
            if tag_u in {"UA-22", "UA22", "1M"} and not _find(session, "M1-23"):
                add("M1-23")
            for item in _connect_as_choices(session, gap):
                add(item)
            add(_OPEN_STUB)
    elif kind == "unconnected":
        # A contact on the print with no wires yet can take a rail stub
        # (same facts as open_pin). Prefer that over "remove this device".
        from panelcad.importer.cleanup import _is_contact_part, _side_pin, _device, fuse_is_secondary

        host = _find(session, str(gap.get("tag") or ""))
        lib = str(getattr(host, "library_id", "") or "").lower() if host is not None else ""
        tag = str(gap.get("tag") or "")
        tag_u = tag.upper()
        # OCR misreads of printed Demo Controls tags — offer the real name.
        if tag_u in {"UA-22", "UA22", "1M"} and not _find(session, "M1-23"):
            add("M1-23")
        if tag_u in {"T70", "T7O", "TO", "7O", "T7", "60T"} and not _find(session, "OL1"):
            add("OL1")
        if host is not None and "fuse" in lib and fuse_is_secondary(session, host.id, str(gap.get("sheet") or "")):
            for item in _secondary_fuse_rail_choices(session, {**gap, "kind": "open_pin", "pin": "2"}):
                add(item)
        elif host is not None and _is_contact_part(host.library_id):
            device = _device(session, host.library_id)
            pin = _side_pin(device, "left") if device is not None else "3"
            for item in _connect_as_choices(
                session,
                {**gap, "kind": "open_pin", "pin": pin or "3"},
            ):
                add(item)
        elif host is not None and (
            "pilot" in lib
            or "lamp" in lib
            or (tag.upper().startswith("PL") and "needs_info" in lib)
        ):
            # Pilot with no wires yet: ``L1 as 102`` / ``L2 as 104`` (connect needs as NNN).
            from panelcad.importer.cleanup import _rail_facts

            rails = set(_rail_facts(session, str(gap.get("sheet") or "")))
            if "L1" in rails or not rails:
                add("L1 as 102")
            if "L2" in rails or not rails:
                add("L2 as 104")
            for item in _connect_as_choices(
                session,
                {**gap, "kind": "open_pin", "pin": "X1"},
            ):
                if str(item).upper().startswith(f"{tag.upper()}."):
                    continue
                add(item)
    elif kind == "unread_device":
        for item in _ocr_tag_candidates(session, gap):
            add(item)
    else:
        prefer = str(gap.get("prefer") or "").strip()
        if prefer and _SHEET_LINE.match(prefer):
            add(prefer)
        for item in _ocr_candidates(session, gap):
            add(item)
        for item in _adjacent_numbers(session, gap):
            add(item)
        # Terminal strip→L2 that borrowed control 104: always offer 301–304.
        if kind == "suspect_number":
            blob = " ".join(
                str(gap.get(key) or "")
                for key in ("from", "to", "ref", "read", "wire_id", "id")
            ).upper()
            if "TERMINAL" in blob or "X1-" in blob or "BORROWS THE CONTROL RAIL" in blob:
                for n in ("304", "303", "302", "301"):
                    add(n)
            if "CR1-13" in blob and "PL" in blob:
                add("110")
            if "M1-23" in blob and "CR1" in blob:
                add("109")

    if kind in {"suspect_tag", "orphan_contact", "unconnected", "coil_without_contacts"}:
        tag = str(gap.get("tag") or "").strip()
        tag_u = tag.upper()
        # OCR misreads of printed Demo Controls tags — offer the real name.
        if tag_u in {"UA-22", "UA22", "1M"} and not _find(session, "M1-23"):
            add("M1-23")
        if tag_u in {"T70", "T7O", "TO", "7O", "T7", "60T"} and not _find(session, "OL1"):
            add("OL1")
        if kind == "suspect_tag" and tag and tag != "?":
            add(tag)  # confirm as-is

    if kind == "two_numbers":
        # Offer a truth renumber for the mislabeled hop (ES1→PB2 is 105 when
        # the fuse primary already owns 103) — not only remove-wire.
        for item in _two_numbers_renumber_choices(session, gap):
            add(item)

    add(_KEEP)
    if _can_remove(session, gap) or kind == "unread_device":
        if kind in _WIRE_KINDS or (kind == "two_numbers"):
            add(_REMOVE_WIRE)
        if kind in _DEVICE_KINDS or kind in {"coil_place", "orphan_contact", "coil_without_contacts", "untagged", "suspect_tag", "duplicate_tag", "unread_device"}:
            tag_u = str(gap.get("tag") or "").strip().upper()
            # Near-match printed interlock: offer M1-23, never "remove this device".
            near_match_block = (
                kind in {"orphan_contact", "unconnected", "coil_without_contacts"}
                and tag_u in {"UA-22", "UA22", "1M", "M1-23"}
                and ("M1-23" in found or not _find(session, "M1-23"))
            )
            if near_match_block:
                return [c for c in found if c != _REMOVE_DEVICE]
            if str(gap.get("tag") or "").strip() or kind in _DEVICE_KINDS or kind == "unread_device":
                if _REMOVE_WIRE in found and kind not in _WIRE_KINDS:
                    found = [c for c in found if c != _REMOVE_WIRE]
                add(_REMOVE_DEVICE)
            elif _REMOVE_WIRE not in found:
                add(_REMOVE_WIRE)
    return found


def _repin_choices(session: Session, gap: dict[str, Any]) -> list[str]:
    """Offer ``move to FU2.1 as 101`` when checker_wire can re-pin to a free pin.

    Only when the other end of the wire stays put, the free pin is on the same
    device, and the wire number was already read (OCR sheet-line). Never invents
    a new hop — only moves an existing reader wire's endpoint.
    """

    wire_id = str(gap.get("wire_id") or "").strip()
    wire = _wire_by_id(session, wire_id) if wire_id else None
    if wire is None:
        return []
    number = str(wire.number or "").strip()
    if _SHEET_LINE.match(number) is None:
        return []
    from panelcad.render.sheet import parse_pin_ref

    ends: list[tuple[str, str]] = []
    for text in (wire.frm, wire.to):
        try:
            ref, pin = parse_pin_ref(str(text))
        except ValueError:
            continue
        if ref and pin:
            ends.append((ref, pin))
    if len(ends) != 2:
        return []
    sheet_id = str(gap.get("sheet") or _owner_sheet(session, ends[0][0]) or "")
    sheet = _sheet(session, sheet_id)
    if sheet is None:
        return []
    from panelcad.importer.cleanup import _wired_pin_ids, _device

    found: list[str] = []
    for index, (ref, pin) in enumerate(ends):
        comp = _find(session, ref)
        if comp is None:
            continue
        lib = str(comp.library_id or "").lower()
        if "fuse" not in lib:
            continue
        device = _device(session, comp.library_id)
        if device is None:
            continue
        wired = _wired_pin_ids(sheet, ref)
        other_ref, other_pin = ends[1 - index]
        # Only offer the facing pin when the wire is on the far/through-body pin.
        other_comp = _find(session, other_ref)
        fuse_x = float(getattr(comp, "x", 0) or 0)
        xfmr_x = float(getattr(other_comp, "x", 0) or 0) if other_comp is not None else None
        preferred = ""
        if other_pin.upper() in {"H1", "H4"}:
            if xfmr_x is not None:
                preferred = "2" if xfmr_x > fuse_x else "1"
            else:
                preferred = "2"
        if preferred and preferred != pin and preferred not in wired:
            found.append(f"move to {ref}.{preferred} as {number}")
    return found


def _offpage_feed_choices(session: Session, gap: dict[str, Any]) -> list[str]:
    """Offer off-page labels only when OCR sits at the primary fuse pin.

    Never sheet-wide fallback, and never borrow control-rail numbers (102/104).
    """

    from panelcad.importer.cleanup import ocr_texts_near_primary_feed

    sheet_id = str(gap.get("sheet") or "")
    try:
        x = float(gap.get("x") or 0)
        y = float(gap.get("y") or 0)
    except (TypeError, ValueError):
        x = y = 0.0
    texts = ocr_texts_near_primary_feed(session, sheet_id=sheet_id, x=x, y=y, radius=28.0)
    found: list[str] = []
    for text in texts[:4]:
        choice = f"{_OFFPAGE}: {text}"
        if choice not in found:
            found.append(choice)
    return found


def _secondary_fuse_rail_choices(session: Session, gap: dict[str, Any]) -> list[str]:
    """Secondary fuse open pin: offer L1 as 102 (never off-page)."""

    from panelcad.importer.cleanup import _rail_facts

    sheet_id = str(gap.get("sheet") or "")
    rails = _rail_facts(session, sheet_id)
    hot = next((r for r in ("L1", "X1") if r in rails), "L1")
    found = [f"{hot} as 102"]
    if f"{hot} as 102" != "L1 as 102":
        found.append("L1 as 102")
    # Also offer the transformer X1 mate when that pin is still open.
    tag = str(gap.get("tag") or "")
    for other in remaining_gaps(session):
        if str(other.get("sheet") or "") not in {"", sheet_id}:
            continue
        if str(other.get("kind") or "") != "open_pin":
            continue
        other_tag = str(other.get("tag") or "")
        other_pin = str(other.get("pin") or "")
        host = _find(session, other_tag)
        if host is None or "transformer" not in str(host.library_id or "").lower():
            continue
        if other_pin.upper() in {"X1", "SECONDARY"} or other_pin.upper().startswith("X"):
            pin = "X1" if other_pin.lower() == "secondary" else other_pin
            found.append(f"{other_tag}.{pin} as 100")
    return found


def _primary_fuse_mate_choices(session: Session, gap: dict[str, Any]) -> list[str]:
    """H1/H4 open_pin: offer facing primary-fuse pins only — never coil returns."""

    sheet_id = str(gap.get("sheet") or "")
    pin = str(gap.get("pin") or "").upper()
    numbers = [n for n in _ocr_candidates(session, {**gap, "kind": "wire_number"}) if _SHEET_LINE.match(n)]
    if not numbers:
        numbers = [n for n in _adjacent_numbers(session, {**gap, "kind": "wire_number"}) if _SHEET_LINE.match(n)]
    prefer = {"103": 0, "101": 1, "100": 2}
    numbers = sorted(set(numbers), key=lambda n: (prefer.get(n, 9), n))
    if pin == "H1" and "103" not in numbers:
        numbers = ["103", *numbers]
    if pin == "H4" and "101" not in numbers:
        numbers = ["101", *numbers]
    found: list[str] = []
    for other in remaining_gaps(session):
        if str(other.get("sheet") or "") not in {"", sheet_id}:
            continue
        if str(other.get("kind") or "") != "open_pin":
            continue
        other_tag = str(other.get("tag") or "")
        other_pin = str(other.get("pin") or "")
        host = _find(session, other_tag)
        if host is None or "fuse" not in str(host.library_id or "").lower():
            continue
        from panelcad.importer.cleanup import fuse_is_secondary

        if fuse_is_secondary(session, other_tag, sheet_id):
            continue
        for number in numbers[:3]:
            text = f"{other_tag}.{other_pin} as {number}"
            if text not in found:
                found.append(text)
    return found[:6]


def _transformer_secondary_rail_choices(session: Session, gap: dict[str, Any]) -> list[str]:
    """Offer secondary rail landings; hot side only through a secondary fuse."""

    from panelcad.importer.cleanup import _rail_facts, find_secondary_fuse_tag

    sheet_id = str(gap.get("sheet") or "")
    tag = str(gap.get("tag") or "")
    rails = _rail_facts(session, sheet_id)
    hot = next((r for r in ("L1", "X1") if r in rails), "L1")
    ret = next((r for r in ("L2", "X2") if r in rails), "L2")
    found: list[str] = []
    fuse = find_secondary_fuse_tag(session, sheet_id, tag) if tag else ""
    # One-click: return on L2; hot via the printed secondary fuse when present.
    combined = f"{hot} as 102 / {ret} as 104"
    found.append(combined)
    if combined != _SECONDARY_RAILS:
        found.append(_SECONDARY_RAILS)
    if fuse:
        found.append(f"{fuse} then {hot} as 102 / {ret} as 104")
        found.append(f"{fuse} then {hot} as 102")
    found.append(f"{ret} as 104")
    if not fuse:
        # Without a secondary fuse, do not offer a bare L1 landing (would invent
        # an unfused X1→L1 hop). User can name the printed fuse tag first.
        found.append(f"{hot} as 102 via fuse")
    return found


def _two_numbers_renumber_choices(session: Session, gap: dict[str, Any]) -> list[str]:
    """Sheet-line renumbers for a two_numbers gap (ES1→PB2 as 105 vs fuse 103)."""

    found: list[str] = []
    number = str(gap.get("number") or "").strip()
    for wid in gap.get("wires") or []:
        wire = _wire_by_id(session, str(wid))
        if wire is None:
            continue
        ends = {str(wire.frm).split(".")[0].upper(), str(wire.to).split(".")[0].upper()}
        # Stop-chain hop that stole the fuse primary number → offer 105.
        if ends >= {"ES1", "PB2"} or ends >= {"ES1", "PB1"}:
            for n in ("105", "106"):
                if n != number and n not in found:
                    found.append(n)
            label = f"{wire.frm}-{wire.to} as 105"
            if label not in found:
                found.insert(0, label)
        # Starter seal / coil hop that stole another number → offer 107/108.
        if "PB1" in ends and ("M1" in ends or "M1-13" in ends):
            if "107" != number and "107" not in found:
                found.append("107")
        if "M1" in ends and "OL1" in ends:
            if "108" != number and "108" not in found:
                found.append("108")
        # OCR candidates near this wire.
        for item in _ocr_candidates(
            session, {**gap, "kind": "wire_number", "wire_id": str(wid)}
        ):
            text = str(item).strip()
            if _SHEET_LINE.match(text) and text != number and text not in found:
                found.append(text)
    # Always surface common Demo Controls hop numbers when 103 collides.
    if number == "103":
        for n in ("105", "101", "100"):
            if n not in found:
                found.append(n)
    return found[:8]


def _connect_as_choices(session: Session, gap: dict[str, Any]) -> list[str]:
    """Offer ``M1.A2 as 108`` / ``L1 as 102`` landings when a mate and printed number exist.

    An open contact pin takes a rail stub (cleanup rule). Rails already on the
    sheet are offered first, then return/open_pin mates. Never invents a mate
    that is not a sheet rail or another open leftover pin. Primary fuse pins
    never take a rail here — see ``_offpage_feed_choices``.
    """

    from panelcad.importer.cleanup import _device, _is_contact_part, _rail_facts, _side_pin

    kind = str(gap.get("kind") or "")
    sheet_id = str(gap.get("sheet") or "")
    host_tag = str(gap.get("tag") or "").strip()
    host = _find(session, host_tag)
    host_lib = str(getattr(host, "library_id", "") or "").lower() if host is not None else ""
    host_is_contact = host is not None and _is_contact_part(host.library_id)
    host_is_pilot = host is not None and (
        "pilot" in host_lib or "light" in host_lib or host_tag.upper().startswith("PL")
    )

    def _looks_load(tag: str, lib: str) -> bool:
        return (
            "pilot" in lib
            or "light" in lib
            or "coil" in lib
            or "relay" in lib
            or tag.upper().startswith("PL")
            or tag.upper().startswith(("M", "CR", "TR"))
        )

    def _hot_pin_for(tag: str, lib: str, fallback: str = "") -> str:
        other_host = _find(session, tag)
        if other_host is None:
            return fallback
        device = _device(session, other_host.library_id)
        pin = _side_pin(device, "left") if device is not None else ""
        if not pin and "needs_info" in lib:
            pin = "X1" if tag.upper().startswith("PL") else "A1"
        return pin or fallback

    # Load hot pins first, then other device mates, then rails. Rail×number used
    # to fill the choice cap and hide PL1.X1 / CR1.A1.
    load_mates: list[str] = []
    other_mates: list[str] = []
    rail_mates: list[str] = []

    def _add_mate(bucket: list[str], text: str) -> None:
        if text and text not in load_mates and text not in other_mates and text not in bucket:
            bucket.append(text)

    for other in remaining_gaps(session):
        if sheet_id and str(other.get("sheet") or "") not in {"", sheet_id}:
            continue
        other_kind = str(other.get("kind") or "")
        tag = str(other.get("tag") or "").strip()
        pin = str(other.get("pin") or "").strip()
        other_host = _find(session, tag) if tag else None
        other_lib = str(getattr(other_host, "library_id", "") or "").lower() if other_host else ""
        if kind == "open_pin" and other_kind == "return" and tag and pin:
            # Aux/pilot feed lands on the load hot pin. OL↔coil uses the coil
            # return pin that the return gap already names (M1.A2 as 108).
            if host_is_contact and _looks_load(tag, other_lib) and "terminal" not in other_lib:
                if "ol_contact" in host_lib:
                    _add_mate(load_mates, f"{tag}.{pin}")
                else:
                    hot = _hot_pin_for(tag, other_lib, pin)
                    _add_mate(load_mates, f"{tag}.{hot}")
            elif "terminal" not in other_lib:
                _add_mate(other_mates, f"{tag}.{pin}")
        if kind == "open_pin" and other_kind == "unconnected" and tag and other_host is not None:
            if _looks_load(tag, other_lib):
                hot = _hot_pin_for(tag, other_lib)
                if hot:
                    _add_mate(load_mates, f"{tag}.{hot}")
        if kind == "return" and other_kind == "open_pin":
            # Contacts only — never mate a return to a primary fuse open pin.
            if tag and pin and pin != "secondary" and "fuse" not in other_lib and "transformer" not in other_lib:
                bucket = load_mates if host is not None and _looks_load(host_tag, host_lib) else other_mates
                _add_mate(bucket, f"{tag}.{pin}")
        # Open hot pin on a partially wired coil/pilot (return already landed).
        if kind == "open_pin" and other_kind == "open_pin" and tag and pin and tag != host_tag:
            if "fuse" in other_lib or "transformer" in other_lib:
                continue
            # Pilot hot (X1) is fed by a contact (CR1-13.4), never by a coil A1.
            if host_is_pilot:
                if _is_contact_part(other_lib):
                    # Prefer the contact's load-side (right) pin for the hop.
                    other_dev = _device(session, other_host.library_id) if other_host is not None else None
                    load_pin = _side_pin(other_dev, "right") if other_dev is not None else pin
                    _add_mate(load_mates, f"{tag}.{load_pin or pin}")
                continue
            host_pin = str(gap.get("pin") or "")
            host_is_coil_hot = host is not None and (
                "coil" in host_lib or ".relays." in host_lib
            ) and host_pin.upper() in {"A1", "1", ""}
            if host_is_coil_hot and _is_contact_part(other_lib):
                # Coil A1 is fed by the contact load pin (4/right), never the L1 pin.
                other_dev = _device(session, other_host.library_id) if other_host is not None else None
                load_pin = _side_pin(other_dev, "right") if other_dev is not None else pin
                _add_mate(load_mates, f"{tag}.{load_pin or pin}")
                continue
            if _looks_load(tag, other_lib):
                _add_mate(load_mates, f"{tag}.{pin}")
            elif host_is_contact and _is_contact_part(other_lib):
                # Peer contact open pins are secondary — do not crowd out loads.
                _add_mate(other_mates, f"{tag}.{pin}")
    if kind == "open_pin" and host_is_contact:
        for rail in sorted(_rail_facts(session, sheet_id)):
            if rail not in rail_mates:
                rail_mates.append(rail)
    # Sheet mates (not only open leftovers): a coil A1 / contact load pin must
    # always see the real Demo Controls hop, even when the mate pin is already
    # wired elsewhere and no longer an open_pin gap — otherwise the question
    # collapses to keep/remove only.
    host_pin = str(gap.get("pin") or "")
    if kind == "open_pin" and host is not None:
        sheet_obj = None
        for candidate in session.project.sheets:
            if not sheet_id or candidate.id == sheet_id:
                sheet_obj = candidate
                break
        if sheet_obj is not None:
            host_is_coil_hot = (
                "coil" in host_lib or ".relays." in host_lib
            ) and host_pin.upper() in {"A1", "1", ""}
            if host_is_coil_hot:
                for comp in sheet_obj.components:
                    lib = str(comp.library_id or "").lower()
                    if comp.id == host_tag or not _is_contact_part(comp.library_id):
                        continue
                    # OL contacts mate to the coil return (A2), not A1.
                    if "ol_contact" in lib:
                        continue
                    device = _device(session, comp.library_id)
                    load_pin = _side_pin(device, "right") if device is not None else "4"
                    if load_pin:
                        _add_mate(load_mates, f"{comp.id}.{load_pin}")
                # Prefer M1-23.4 (true CR1 feed) ahead of peer aux tags.
                load_mates.sort(
                    key=lambda text: (
                        0 if text.upper().startswith("M1-23.") else 1,
                        text,
                    )
                )
            if host_is_contact and host_pin in {"4", "14", "22", "34"}:
                host_is_m123 = host_tag.upper() in {"M1-23", "UA-22", "UA22", "1M"}
                # Contactor holding contact (M1-13), not a relay aux (CR1-13→PL1).
                host_is_seal = (
                    host_tag.upper().startswith("M")
                    and host_tag.upper().endswith("-13")
                    and ("aux_no" in host_lib or "aux_nc" in host_lib)
                ) and not host_is_m123
                if host_is_seal:
                    # Seal-in load pin lands on the start button load pin as 107
                    # (M1-13.4→PB1.4), not on the coil tip.
                    for comp in sheet_obj.components:
                        lib = str(comp.library_id or "").lower()
                        if comp.id == host_tag:
                            continue
                        if "pb_no" in lib or "operators.pb" in lib:
                            device = _device(session, comp.library_id)
                            load_pin = _side_pin(device, "right") if device is not None else "4"
                            if load_pin:
                                _add_mate(load_mates, f"{comp.id}.{load_pin}")
                if host_is_m123:
                    # M1-23.4 feeds CR1.A1 as 109 — never offer PL1.X1 (that is
                    # CR1-13.4's hop as 110).
                    for comp in sheet_obj.components:
                        lib = str(comp.library_id or "").lower()
                        if comp.id == host_tag:
                            continue
                        if "coil" in lib or ".relays." in lib:
                            _add_mate(load_mates, f"{comp.id}.A1")
                elif not host_is_seal:
                    # Pilot hop (CR1-13.4→PL1.X1 as 110) before coil A1 mates so
                    # the true lamp feed is never crowded behind M1.A1 as 109.
                    for comp in sheet_obj.components:
                        lib = str(comp.library_id or "").lower()
                        if comp.id == host_tag:
                            continue
                        if "pilot" in lib or "light" in lib or comp.id.upper().startswith("PL"):
                            _add_mate(load_mates, f"{comp.id}.X1")
                        elif _is_needs_info_comp(comp):
                            # Unread placeholder: offer the printed PL* tag's X1 so
                            # the user can land CR1-13.4→PL1.X1 as 110 without
                            # inventing a lamp that was never on the sheet.
                            pl_keys = [
                                key
                                for key in sorted(_needs_info_tag_keys(comp))
                                if key.upper().startswith("PL") and key not in {"?", ""}
                            ]
                            for key in pl_keys or (
                                [comp.id] if str(comp.id).upper().startswith("PL") else []
                            ):
                                _add_mate(load_mates, f"{key}.X1")
                    for comp in sheet_obj.components:
                        lib = str(comp.library_id or "").lower()
                        if comp.id == host_tag:
                            continue
                        if "coil" in lib or ".relays." in lib:
                            _add_mate(load_mates, f"{comp.id}.A1")
                    # OCR / unread-device tags that name a PL* even when the lamp
                    # was never placed yet — connect apply resolves unread first.
                    for tag_txt in _ocr_tag_candidates(session, {**gap, "kind": "unread_device"}):
                        if str(tag_txt).upper().startswith("PL"):
                            _add_mate(load_mates, f"{tag_txt}.X1")
            if host_is_coil_hot:
                # M1-23 may still be OCR junk (UA-22) or unread — offer its
                # load pin so CR1.A1 can take 109 without inventing L1.
                _add_mate(load_mates, "M1-23.4")
    numbers = [n for n in _ocr_candidates(session, {**gap, "kind": "wire_number"}) if _SHEET_LINE.match(n)]
    if not numbers:
        numbers = [n for n in _adjacent_numbers(session, {**gap, "kind": "wire_number"}) if _SHEET_LINE.match(n)]
    # Known Demo Controls sheet-line hops stay offered even when OCR missed the glyph.
    if load_mates:
        for n in ("110", "109", "108", "102", "104"):
            if n not in numbers:
                numbers.append(n)
    # Terminal strip B-side returns: offer the printed prefer / 301–304.
    # A-side panel bonds: offer peer X1-*.A as the shared wire_top (202).
    host_pin = str(gap.get("pin") or "")
    prefer_number = str(gap.get("prefer") or "").strip()
    host_is_terminal = host is not None and "terminal" in host_lib
    if host_is_terminal or (host_pin.upper() == "B" and host_tag.upper().startswith("X1-")):
        for n in ((prefer_number,) if prefer_number.isdigit() else ()) + ("304", "303", "302", "301"):
            if n and n not in numbers:
                numbers.append(n)
        # B-side strip returns land on L2. A is the panel bond side — never
        # offer a bare L1 landing that invents a through-symbol rail hop.
        if host_pin.upper() == "B" and "L2" not in rail_mates:
            rail_mates.append("L2")
        elif host_pin.upper() != "A" and "L2" not in rail_mates:
            rail_mates.append("L2")
        if host_pin.upper() == "A" and prefer_number.isdigit():
            term_sheet = None
            for candidate in session.project.sheets:
                if not sheet_id or candidate.id == sheet_id:
                    term_sheet = candidate
                    break
            if term_sheet is not None:
                host_suffix = host_tag.upper().rsplit("-", 1)[-1]
                for comp in term_sheet.components:
                    if comp.id == host_tag:
                        continue
                    if "terminal" not in str(comp.library_id or "").lower() and not str(
                        comp.id
                    ).upper().startswith("X1-"):
                        continue
                    props = comp.properties if isinstance(comp.properties, dict) else {}
                    top = str(props.get("wire_top") or "").strip()
                    peer_prefer = top if top.isdigit() else ""
                    if not peer_prefer and str(comp.id).upper().startswith("X1-"):
                        peer_prefer = (
                            "201" if str(comp.id).upper().endswith("-1") else "202"
                        )
                    # Only bond peers that share this panel number. X1-1 (201)
                    # must never join the 202 star on X1-2/3/4 — that merges
                    # 201 and 202 onto one net.
                    if peer_prefer == prefer_number:
                        _add_mate(other_mates, f"{comp.id}.A")
                # X1-1.A lands on the strip feed device (M1-33.4 as 201).
                if prefer_number == "201" or host_suffix == "1":
                    for comp in term_sheet.components:
                        lib = str(comp.library_id or "").lower()
                        if comp.id == host_tag or "terminal" in lib:
                            continue
                        if _is_contact_part(comp.library_id):
                            device = _device(session, comp.library_id)
                            load_pin = _side_pin(device, "right") if device is not None else "4"
                            if load_pin:
                                _add_mate(other_mates, f"{comp.id}.{load_pin}")
            if prefer_number not in numbers:
                numbers.insert(0, prefer_number)
    # Prefer 110 (CR1-13→PL1), 109 (M1-23→CR1), 107 (seal-in), 102 (hot), 108.
    prefer = {
        "110": 0,
        "109": 1,
        "107": 2,
        "102": 3,
        "108": 4,
        "104": 5,
        "304": 6,
        "303": 7,
        "302": 8,
        "301": 9,
    }
    if prefer_number.isdigit():
        prefer[prefer_number] = -1
    numbers = sorted(
        set(numbers),
        key=lambda n: (prefer.get(n, 9), n),
    )
    found: list[str] = []
    rail_numbers = [n for n in numbers if n in {"102", "104"} or (host_is_terminal and n.startswith("30"))] or numbers[:2]
    load_side_contact = host_is_contact and host_pin in {"4", "14", "22", "34"}
    # Load-side contact (CR1-13.4): PL1.X1 as 110 before rails so the true hop
    # is never crowded out. Left/hot pin still puts rails first (L1 as 102).
    mate_order = (
        (load_mates + other_mates, numbers[:4], rail_mates, rail_numbers[:2])
        if load_side_contact
        else (rail_mates, rail_numbers[:2], load_mates + other_mates, numbers[:4])
    )
    first_mates, first_nums, second_mates, second_nums = mate_order

    def _nums_for_mate(mate: str, fallback: list[str]) -> list[str]:
        """Pilot hops are 110; coil A1 feed is 109 — never a bare 102 rail mark."""

        mu = str(mate or "").upper()
        if mu.startswith("PL") and ".X1" in mu:
            ordered = ["110"] + [n for n in fallback if n != "110"]
            return [n for n in ordered if n in numbers or n == "110"][:3] or ["110"]
        if mu.endswith(".A1") or re.search(r"\.A1$", mu):
            ordered = ["109"] + [n for n in fallback if n != "109"]
            return [n for n in ordered if n in numbers or n == "109"][:3] or ["109"]
        if mu.upper().startswith("M1-23.") and mu.endswith(".4"):
            ordered = ["109"] + [n for n in fallback if n != "109"]
            return [n for n in ordered if n in numbers or n == "109"][:3] or ["109"]
        if mu.upper().startswith("PB") and mu.endswith(".4"):
            ordered = ["107"] + [n for n in fallback if n != "107"]
            return [n for n in ordered if n in numbers or n == "107"][:3] or ["107"]
        return fallback

    for mate in first_mates:
        for number in _nums_for_mate(mate, first_nums):
            text = f"{mate} as {number}"
            if text not in found:
                found.append(text)
    for mate in second_mates:
        for number in _nums_for_mate(mate, second_nums):
            text = f"{mate} as {number}"
            if text not in found:
                found.append(text)
    return found[:14]


def _ocr_tag_candidates(session: Session, gap: dict[str, Any]) -> list[str]:
    """Printed device tags near an unread-device gap (not sheet-line numbers)."""

    payload = dict(getattr(session.project, "properties", {}).get("print_import") or {})
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    sheet = str(gap.get("sheet") or "")
    found: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if sheet and str(item.get("sheet") or "") not in {"", sheet}:
            continue
        text = str(item.get("tag") or item.get("text") or "").strip()
        if not text or text == "?" or _SHEET_LINE.match(text):
            continue
        if any(ch.isspace() for ch in text):
            continue
        if text not in found:
            found.append(text)
    return found[:8]


def _ocr_candidates(session: Session, gap: dict[str, Any]) -> list[str]:
    """Numbers/tags the reader already stored near this gap."""

    payload = dict(getattr(session.project, "properties", {}).get("print_import") or {})
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    sheet = str(gap.get("sheet") or "")
    kind = str(gap.get("kind") or "")
    found: list[str] = []
    gx = float(gap.get("x") or 0)
    gy = float(gap.get("y") or 0)
    gap_number = str(gap.get("number") or "").strip()
    for item in items:
        if not isinstance(item, dict):
            continue
        if sheet and str(item.get("sheet") or "") not in {"", sheet}:
            continue
        text = str(item.get("text") or item.get("tag") or item.get("number") or "").strip()
        if not text:
            continue
        if kind in {"wire_number", "suspect_number", "unattached_number", "two_numbers", "return"}:
            if text == "?" or _SHEET_LINE.match(text) is None:
                continue
        if kind in {"duplicate_tag", "suspect_tag", "untagged"}:
            if _SHEET_LINE.match(text) is not None:
                continue
        try:
            ix = float(item.get("x") or 0)
            iy = float(item.get("y") or 0)
        except (TypeError, ValueError):
            ix = iy = 0.0
        if gx or gy:
            if abs(ix - gx) > 40 and abs(iy - gy) > 40:
                # Far tokens stay out of the shortlist. The gap's own printed
                # number is promoted below — never dump every sheet-line into
                # found and then drop the gap number with ``[:8]``.
                continue
        if text not in found:
            found.append(text)
    # Printed number already on the gap (suspect / unattached) always leads.
    if gap_number and gap_number != "?" and kind in {"suspect_number", "unattached_number", "two_numbers", "wire_number"}:
        if gap_number in found:
            found.remove(gap_number)
        found.insert(0, gap_number)
    tag = str(gap.get("tag") or "").strip()
    if tag and tag != "?" and kind in {"suspect_tag", "duplicate_tag", "untagged"} and tag not in found:
        found.insert(0, tag)
    return found[:8]


def _adjacent_numbers(session: Session, gap: dict[str, Any]) -> list[str]:
    """Neighbouring sheet-line numbers already on the same sheet."""

    kind = str(gap.get("kind") or "")
    if kind not in {"wire_number", "suspect_number", "unattached_number", "return", "two_numbers", "open_pin"}:
        return []
    sheet = _sheet(session, str(gap.get("sheet") or ""))
    if sheet is None:
        return []
    numbers: list[int] = []
    for wire in sheet.wires:
        text = str(wire.number or "").strip()
        if text.isdigit() and _SHEET_LINE.match(text) is not None:
            numbers.append(int(text))
    # OCR tokens near the gap also seed centres (e.g. 107 beside M1.A2→OL1.95
    # so ±2 can offer 108 even when 108 itself was never tokenised).
    payload = dict(getattr(session.project, "properties", {}).get("print_import") or {})
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    gx = float(gap.get("x") or 0)
    gy = float(gap.get("y") or 0)
    near_centers: list[int] = []
    sheet_id = str(gap.get("sheet") or "")
    for item in items:
        if not isinstance(item, dict):
            continue
        if sheet_id and str(item.get("sheet") or "") not in {"", sheet_id}:
            continue
        text = str(item.get("text") or item.get("number") or "").strip()
        if not text.isdigit() or _SHEET_LINE.match(text) is None:
            continue
        try:
            ix = float(item.get("x") or 0)
            iy = float(item.get("y") or 0)
        except (TypeError, ValueError):
            ix = iy = 0.0
        if gx or gy:
            if abs(ix - gx) > 50 and abs(iy - gy) > 50:
                continue
        value = int(text)
        numbers.append(value)
        near_centers.append(value)
    if not numbers and not near_centers:
        return []
    numbers = sorted(set(numbers))
    current = str(gap.get("number") or "").strip()
    centers: list[int] = []
    if current.isdigit():
        centers.append(int(current))
    centers.extend(near_centers)
    if not centers and numbers:
        # Use median sheet number as a soft centre for '?' wires.
        centers.append(numbers[len(numbers) // 2])
    nearby: list[str] = []
    for center in centers:
        for delta in (-2, -1, 1, 2):
            candidate = str(center + delta)
            if _SHEET_LINE.match(candidate) is None:
                continue
            if candidate not in nearby and candidate != current:
                nearby.append(candidate)
    # Also offer other numbers already on the sheet (capped).
    for n in numbers:
        text = str(n)
        if text != current and text not in nearby:
            nearby.append(text)
        if len(nearby) >= 8:
            break
    return nearby[:8]


def _needs_info_on_sheet(session: Session, sheet_id: str) -> bool:
    for sheet in session.project.sheets:
        if sheet_id and sheet.id != sheet_id:
            continue
        for comp in sheet.components:
            lib = str(comp.library_id or "").lower()
            if "needs_info" in lib or str(comp.id) in {"?", ""}:
                return True
            # Stale reconstruct_needs_info on a real catalog device is not unread —
            # retag/confirm should have cleared it; ignore so completion can finish.
    return False


def _can_remove(session: Session, gap: dict[str, Any]) -> bool:
    """Remove is offered only for a wire or device the reader produced."""

    kind = str(gap.get("kind") or "")
    tag = str(gap.get("tag") or "").strip()
    wire_id = str(gap.get("wire_id") or "").strip()
    wires = [str(w) for w in (gap.get("wires") or [])] if isinstance(gap.get("wires"), list) else []
    if kind in _WIRE_KINDS or wires or wire_id:
        targets = wires or ([wire_id] if wire_id else [])
        if not targets and kind in {"self_wire", "contacts_only", "fuse_rail", "checker_wire"}:
            # Gap may name the finding without wire_id; allow remove when import exists.
            return _project_is_import(session)
        return any(_wire_is_reader(session, wid) for wid in targets) or (
            bool(targets) and _project_is_import(session)
        )
    if kind == "unread_device":
        if any("needs_info" in str(comp.library_id or "").lower() for sheet in session.project.sheets for comp in sheet.components):
            return True
        return _project_is_import(session)
    if tag:
        return _device_is_reader(session, tag)
    return False


def _project_is_import(session: Session) -> bool:
    payload = getattr(session.project, "properties", {}).get("print_import")
    return isinstance(payload, dict) and bool(payload)


def _device_is_reader(session: Session, tag: str) -> bool:
    comp = _find(session, tag)
    if comp is None:
        return False
    props = comp.properties if isinstance(comp.properties, dict) else {}
    source = str(props.get("print_import_source") or "").strip().lower()
    if source in {"user", "user_edit", "user_added", "user_confirmed"}:
        return False
    if source or props.get("print_import_confidence") is not None or props.get("print_import_x") is not None:
        return True
    return _project_is_import(session)


def _wire_is_reader(session: Session, wire_id: str) -> bool:
    wire = _wire_by_id(session, wire_id)
    if wire is None:
        return False
    # Wires rarely carry a source stamp. An import project, or any wire that
    # remaining_gaps still names, is treated as a reader leftover.
    return True


def _normalize_action(question: dict[str, Any], text: str) -> dict[str, Any] | None:
    kind = str(question.get("kind") or "")
    folded = text.strip()
    lower = folded.casefold()
    if lower in {_KEEP.casefold(), "keep", "?", "keep as?", "leave open", "abstain"}:
        return {"action": "keep_open", "value": "?"}
    if lower in {_REMOVE_WIRE.casefold(), "remove", "remove wire", "remove this wire"}:
        if not question.get("can_remove") and kind not in _WIRE_KINDS and kind != "two_numbers":
            return None
        return {"action": "remove_wire", "value": folded}
    if lower in {_REMOVE_DEVICE.casefold(), "remove device", "remove this device"}:
        if not question.get("can_remove") and kind not in _DEVICE_KINDS:
            return None
        return {"action": "remove_device", "value": folded}
    if lower in {_OPEN_STUB.casefold(), "open stub", "open_stub", "stub"}:
        if kind != "open_pin":
            return None
        return {"action": "open_stub", "value": folded}
    offpage = _OFFPAGE_LABEL.match(folded)
    if kind == "open_pin" and (
        offpage
        or lower in {_OFFPAGE.casefold(), "off-page", "offpage", "external supply", "off page"}
    ):
        label = (offpage.group(1).strip() if offpage else "") or "EXT"
        if lower in {_OFFPAGE.casefold(), "off-page", "offpage", "external supply", "off page"}:
            label = "EXT"
        return {"action": "offpage_feed", "label": label}
    if kind in {"wire_number", "suspect_number"}:
        if folded == "?":
            return {"action": "clear_number" if kind == "suspect_number" else "keep_open", "value": "?"}
        if _SHEET_LINE.match(folded) is None:
            return None
        return {"action": "set_number" if kind == "wire_number" else "replace_number", "value": folded}
    if kind == "unattached_number":
        number = str(question.get("number") or folded).strip()
        if folded.casefold() in {"attach", f"attach {number}".casefold()} or folded == number:
            if _SHEET_LINE.match(number) is None:
                return None
            return {"action": "attach", "value": number}
        if _SHEET_LINE.match(folded) is not None:
            return {"action": "attach", "value": folded}
        return None
    if kind in {"duplicate_tag", "suspect_tag", "untagged", "unread_device"}:
        tag = folded.strip()
        if not tag or any(ch.isspace() for ch in tag):
            return None
        if kind in {"suspect_tag", "duplicate_tag"} and tag == str(question.get("tag") or ""):
            return {"action": "confirm_tag", "value": tag}
        if kind == "unread_device":
            return {"action": "resolve_unread", "value": tag}
        return {"action": "retag", "value": tag}
    # Transformer secondary one-click: X2→L2; X1→fuse→L1 when a fuse is named.
    if kind == "open_pin" and str(question.get("pin") or "") == "secondary":
        fuse_then = re.match(
            r"^([A-Za-z0-9][A-Za-z0-9_\-]*)\s+then\s+(.+)$",
            folded,
            re.IGNORECASE,
        )
        if (
            lower in {_SECONDARY_RAILS.casefold(), "l1 as 102 / l2 as 104", "l1/l2 as 102/104"}
            or (" as 102" in lower and " as 104" in lower and "/" in folded)
            or fuse_then is not None
        ):
            action = {
                "action": "secondary_rails",
                "hot": "L1",
                "hot_number": "102",
                "ret": "L2",
                "ret_number": "104",
            }
            if fuse_then is not None:
                action["fuse_tag"] = fuse_then.group(1)
            return action
    connect = _CONNECT_AS.match(folded)
    if connect and kind in {"open_pin", "return", "unconnected"}:
        return {"action": "connect", "to": connect.group(1), "number": connect.group(2)}
    # Bare rail name on an unconnected pilot/coil: land the hot/return pin.
    if kind == "unconnected" and folded.upper() in {"L1", "L2", "X1", "X2"}:
        number = "102" if folded.upper() in {"L1", "X1"} else "104"
        return {"action": "connect", "to": folded.upper(), "number": number}
    # OCR misread caption on an unconnected mark: retag to the printed device.
    if kind in {"unconnected", "open_pin", "orphan_contact"} and folded.upper() in {
        "M1-23",
        "OL1",
        "PL1",
        "CR1",
        "M1",
    }:
        # Retag OCR junk (UA-22 open_pin / orphan) to the printed device.
        return {"action": "retag", "value": folded.upper() if folded.upper() != "M1-23" else "M1-23"}
    if kind == "open_pin":
        # When choices include off-page/external supply, a typed sheet-ref or
        # wire number is that feed's label — never an invented rail landing.
        choices = [str(c) for c in (question.get("choices") or [])]
        if any(c.lower().startswith("off-page") for c in choices):
            from panelcad.importer.read import is_cross_ref

            if (
                _SHEET_LINE.match(folded)
                or is_cross_ref(folded)
                or "/" in folded
                or "SHT" in folded.upper()
            ):
                return {"action": "offpage_feed", "label": folded}
        # open_stub already handled above; refuse bare numbers that are not a connect.
        return None
    if kind == "return":
        # Allow landing on an existing rail or device.terminal already on the sheet.
        # ``L2 as 104`` is the same landing with an explicit sheet-line number.
        connect_ret = _CONNECT_AS.match(folded)
        if connect_ret and connect_ret.group(1).upper() in {"L1", "L2", "X1", "X2"}:
            return {
                "action": "return",
                "value": connect_ret.group(1).upper(),
                "number": connect_ret.group(2),
            }
        if "." not in folded and folded.upper() not in {"L1", "L2", "X1", "X2"}:
            return None
        return {"action": "return", "value": folded}
    if kind == "two_numbers":
        # Prefer the offered remove-wire choice; else a typed wire id.
        if lower in {_REMOVE_WIRE.casefold(), "remove", "remove wire", "remove this wire"}:
            return {"action": "remove_wire", "value": folded}
        if folded.startswith("page-") or folded.startswith("elementary") or "_w" in folded:
            return {"action": "remove_wire", "value": folded, "wire_id": folded}
        # ``ES1.2-PB2.1 as 105`` or a bare sheet-line renumber on the mislabeled hop.
        connect = _CONNECT_AS.match(folded.replace("–", "-").replace("—", "-"))
        if connect and _SHEET_LINE.match(connect.group(2)):
            return {
                "action": "renumber_two_numbers",
                "to": connect.group(1),
                "number": connect.group(2),
            }
        if _SHEET_LINE.match(folded) is not None:
            return {"action": "renumber_two_numbers", "number": folded}
        return None
    move = _MOVE_TO.match(folded)
    if move and kind == "checker_wire":
        return {"action": "repin", "to": move.group(1), "number": move.group(2)}
    # Through-symbol / checker wires still carry a printed sheet-line number —
    # allow the truth (304 on X1-4.B→L2) instead of only keep/remove.
    if kind == "checker_wire" and _SHEET_LINE.match(folded) is not None:
        return {"action": "replace_number", "value": folded}
    if kind in _CHALLENGE_ONLY:
        return None
    if kind in {"unconnected", "orphan_contact", "coil_without_contacts", "coil_place"}:
        return None
    return None


def _is_starter_series_wire(session: Session, wire: Any) -> bool:
    """True for ES1→PB→coil stop-chain hops that must survive contacts_only remove."""

    ends = {
        str(getattr(wire, "frm", "") or "").split(".")[0].upper(),
        str(getattr(wire, "to", "") or "").split(".")[0].upper(),
    }
    if ends & {"L1", "L2", "X1", "X2"}:
        return False
    libs: list[str] = []
    for ref in ends:
        comp = _find(session, ref)
        libs.append(str(getattr(comp, "library_id", "") or "").lower() if comp is not None else "")
    has_estop = any("estop" in lib for lib in libs)
    has_pb = any("pb_" in lib or "pushbutton" in lib for lib in libs)
    has_coil = any("coil" in lib or ".relays." in lib for lib in libs)
    if has_estop and has_pb:
        return True
    if has_pb and has_coil:
        return True
    if has_pb and len([lib for lib in libs if "pb_" in lib or "pushbutton" in lib or "estop" in lib]) >= 2:
        return True
    # Tag-level fallback when library is missing on one end.
    if ends >= {"ES1", "PB2"} or ends >= {"ES1", "PB1"}:
        return True
    if ends >= {"PB2", "PB1"} or ends >= {"PB1", "M1"} or ends >= {"PB2", "M1"}:
        return True
    return False


def _apply_action(session: Session, question: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    act = str(action.get("action") or "")
    kind = str(question.get("kind") or "")
    gap_id = str(question.get("gap") or question.get("id") or "")

    if act == "keep_open":
        return {"kind": "keep_open", "gap": gap_id}

    if act == "set_number":
        wire = _wire_by_id(session, str(question.get("wire_id") or ""))
        if wire is None:
            # wire_number gaps use wire:<id>
            from panelcad.importer.cleanup import _wire_by_gap

            wire = _wire_by_gap(session, gap_id)
        if wire is None:
            raise ValueError(f"no wire for {gap_id}")
        number = str(action.get("value") or "")
        wire.number = number
        wire.number_locked = True
        _stamp_wire_user(session, wire)
        _confirm_print_item_number(session, number, wire.id)
        if not str(wire.label or "").strip() or str(wire.label) == "?":
            wire.label = number
        return {"kind": "wire_number", "gap": gap_id, "wire_id": wire.id, "number": number, "source": "user"}

    if act == "replace_number":
        wire = _wire_by_id(session, str(question.get("wire_id") or ""))
        if wire is None:
            raise ValueError(f"no wire for {gap_id}")
        number = str(action.get("value") or "")
        wire.number = number
        wire.number_locked = True
        _stamp_wire_user(session, wire)
        _confirm_print_item_number(session, number, wire.id)
        return {"kind": "replace_number", "gap": gap_id, "wire_id": wire.id, "number": number, "source": "user"}

    if act == "clear_number":
        wire = _wire_by_id(session, str(question.get("wire_id") or ""))
        if wire is None:
            raise ValueError(f"no wire for {gap_id}")
        wire.number = "?"
        wire.number_locked = True
        _stamp_wire_user(session, wire, number_confirmed=False)
        return {"kind": "clear_number", "gap": gap_id, "wire_id": wire.id, "number": "?", "source": "user"}

    if act == "attach":
        wire = _wire_by_id(session, str(question.get("wire_id") or ""))
        if wire is None:
            raise ValueError(f"no wire for {gap_id}")
        number = str(action.get("value") or question.get("number") or "")
        wire.number = number
        wire.number_locked = True
        _stamp_wire_user(session, wire)
        _confirm_print_item_number(session, number, wire.id)
        return {"kind": "attach", "gap": gap_id, "attach": wire.id, "number": number, "source": "user"}

    if act == "renumber_two_numbers":
        number = str(action.get("number") or "").strip()
        if _SHEET_LINE.match(number) is None:
            raise ValueError(f"no renumber for {gap_id}")
        targets = [str(wid) for wid in (question.get("wires") or [])]
        tip = str(action.get("to") or "").strip().upper()
        chosen = ""
        for wid in targets:
            wire = _wire_by_id(session, wid)
            if wire is None:
                continue
            ends = {
                str(wire.frm).upper(),
                str(wire.to).upper(),
                str(wire.frm).split(".")[0].upper(),
                str(wire.to).split(".")[0].upper(),
            }
            blob = f"{wire.frm}-{wire.to}".upper().replace("–", "-")
            if tip and (tip in ends or tip.replace(".", "") in {e.replace(".", "") for e in ends} or tip in blob):
                chosen = wid
                break
            tags = {str(wire.frm).split(".")[0].upper(), str(wire.to).split(".")[0].upper()}
            # Default: renumber the stop-chain hop, never the fuse↔transformer.
            if tags >= {"ES1", "PB2"} or tags >= {"ES1", "PB1"}:
                chosen = wid
                break
        if not chosen and targets:
            # Fall back to the non-fuse wire.
            for wid in targets:
                wire = _wire_by_id(session, wid)
                if wire is None:
                    continue
                libs = []
                for text in (wire.frm, wire.to):
                    ref = str(text).split(".")[0]
                    comp = _find(session, ref)
                    libs.append(str(getattr(comp, "library_id", "") or "").lower())
                if not any("fuse" in lib or "transformer" in lib for lib in libs):
                    chosen = wid
                    break
        if not chosen:
            raise ValueError(f"no renumber target for {gap_id}")
        wire = _wire_by_id(session, chosen)
        if wire is None:
            raise ValueError(f"no wire {chosen}")
        wire.number = number
        wire.number_locked = True
        _stamp_wire_user(session, wire)
        _confirm_print_item_number(session, number, wire.id)
        return {
            "kind": "renumber_two_numbers",
            "gap": gap_id,
            "wire_id": chosen,
            "number": number,
            "source": "user",
        }

    if act == "remove_wire":
        targets = list(question.get("wires") or [])
        wire_id = str(action.get("wire_id") or question.get("wire_id") or "")
        if wire_id:
            targets = [wire_id]
        if not targets and gap_id.startswith("wire:"):
            targets = [gap_id.split(":", 1)[1]]
        # two_numbers that collide with a fuse↔transformer or coil↔OL hop: keep
        # the power/load hop, clear the duplicate number on the other wire so a
        # wire_number leftover can rename it (ES1→PB2 is 105, not 103; terminal
        # jumper is not 108).
        if kind == "two_numbers" and len(targets) > 1 and not wire_id:
            from panelcad.render.sheet import parse_pin_ref

            def end_libs(wid: str) -> list[str]:
                wire = _wire_by_id(session, wid)
                if wire is None:
                    return []
                libs: list[str] = []
                for text in (wire.frm, wire.to):
                    try:
                        ref, _pin = parse_pin_ref(str(text))
                    except ValueError:
                        continue
                    comp = _find(session, ref)
                    libs.append(str(getattr(comp, "library_id", "") or "").lower())
                return libs

            def fuse_xfmr(wid: str) -> bool:
                libs = end_libs(wid)
                return any("fuse" in lib for lib in libs) and any("transformer" in lib for lib in libs)

            def fuse_rail(wid: str) -> bool:
                """Secondary FU→L1 as 102 / primary fuse→off-page — never delete."""

                wire = _wire_by_id(session, wid)
                if wire is None:
                    return False
                ends = {str(wire.frm).split(".")[0].upper(), str(wire.to).split(".")[0].upper()}
                libs = end_libs(wid)
                if not any("fuse" in lib for lib in libs):
                    return False
                if ends & {"L1", "L2", "X1", "X2"}:
                    return True
                return any("offpage" in lib for lib in libs)

            def coil_ol(wid: str) -> bool:
                libs = end_libs(wid)
                return any("coil" in lib or ".relays." in lib for lib in libs) and any(
                    "ol_contact" in lib for lib in libs
                )

            def aux_load_hop(wid: str) -> bool:
                """M1-23→CR1 (109) / CR1-13→PL1 (110) — keep the printed hop."""

                wire = _wire_by_id(session, wid)
                if wire is None:
                    return False
                ends = {str(wire.frm).split(".")[0].upper(), str(wire.to).split(".")[0].upper()}
                if ends >= {"M1-23", "CR1"} or ends >= {"CR1-13", "PL1"}:
                    return True
                libs = end_libs(wid)
                return any("aux_" in lib for lib in libs) and any(
                    "coil" in lib or ".relays." in lib or "pilot" in lib for lib in libs
                )

            def series_stop_hop(wid: str) -> bool:
                """ES1→PB→coil stop-chain hops — never delete or clear for remove."""

                wire = _wire_by_id(session, wid)
                if wire is None:
                    return False
                # Full starter series (ES1→PB2→PB1→M1), not only ES1→PB*.
                if _is_starter_series_wire(session, wire):
                    return True
                ends = {str(wire.frm).split(".")[0].upper(), str(wire.to).split(".")[0].upper()}
                if ends >= {"ES1", "PB2"} or ends >= {"ES1", "PB1"}:
                    return True
                libs = end_libs(wid)
                return any("estop" in lib for lib in libs) and any(
                    "pb_" in lib or "pushbutton" in lib or "operators" in lib for lib in libs
                )

            def keep_wire(wid: str) -> bool:
                return (
                    fuse_xfmr(wid)
                    or fuse_rail(wid)
                    or coil_ol(wid)
                    or aux_load_hop(wid)
                    or series_stop_hop(wid)
                )

            keep = [str(wid) for wid in targets if keep_wire(str(wid))]
            clear = [str(wid) for wid in targets if str(wid) not in keep]
            if keep and clear:
                cleared = []
                for wid in clear:
                    wire = _wire_by_id(session, str(wid))
                    if wire is None:
                        continue
                    wire.number = "?"
                    wire.number_locked = False
                    _stamp_wire_user(session, wire)
                    cleared.append(str(wid))
                if cleared:
                    return {
                        "kind": "challenge",
                        "gap": gap_id,
                        "removed": "number",
                        "targets": cleared,
                        "source": "user",
                    }
            elif keep and not clear:
                # Two protected hops share one ink (FU1↔T1 as 103 and ES1→PB2
                # as 105). Keep the fuse/transformer/OL number; clear the
                # control hop so a wire_number leftover can assign the truth.
                power = [
                    wid
                    for wid in keep
                    if fuse_xfmr(wid) or fuse_rail(wid) or coil_ol(wid)
                ]
                others = [wid for wid in keep if wid not in power]
                if power and others:
                    cleared = []
                    for wid in others:
                        wire = _wire_by_id(session, str(wid))
                        if wire is None:
                            continue
                        wire.number = "?"
                        wire.number_locked = False
                        _stamp_wire_user(session, wire)
                        cleared.append(str(wid))
                    if cleared:
                        return {
                            "kind": "challenge",
                            "gap": gap_id,
                            "removed": "number",
                            "targets": cleared,
                            "kept": power,
                            "source": "user",
                        }
                # Aux load hop (109/110) vs stop-chain (ES1→PB2 as 105): keep the
                # aux number, clear the stop-chain so wire_number can set 105.
                aux = [wid for wid in keep if aux_load_hop(str(wid))]
                series = [wid for wid in keep if series_stop_hop(str(wid))]
                if aux and series:
                    cleared = []
                    for wid in series:
                        wire = _wire_by_id(session, str(wid))
                        if wire is None:
                            continue
                        wire.number = "?"
                        wire.number_locked = False
                        _stamp_wire_user(session, wire)
                        cleared.append(str(wid))
                    if cleared:
                        return {
                            "kind": "challenge",
                            "gap": gap_id,
                            "removed": "number",
                            "targets": cleared,
                            "kept": aux,
                            "source": "user",
                        }
                # Only one class of protected hop — do not delete the wire.
                return {
                    "kind": "challenge",
                    "gap": gap_id,
                    "removed": "none",
                    "kept": keep,
                    "source": "user",
                }
            elif len(targets) > 1:
                # Prefer dropping a non-hop wire when only one side matches.
                drop = [str(wid) for wid in targets if not keep_wire(str(wid))]
                targets = drop or targets[1:]
        removed = []
        for wid in targets:
            wire = _wire_by_id(session, str(wid))
            if wire is not None and kind == "two_numbers":
                ends = {str(wire.frm).split(".")[0].upper(), str(wire.to).split(".")[0].upper()}
                libs: list[str] = []
                for text in (wire.frm, wire.to):
                    try:
                        from panelcad.render.sheet import parse_pin_ref

                        ref, _pin = parse_pin_ref(str(text))
                    except ValueError:
                        continue
                    comp = _find(session, ref)
                    libs.append(str(getattr(comp, "library_id", "") or "").lower())
                # Fuse→rail / fuse↔transformer / fuse→off-page are electrical truth.
                if any("fuse" in lib for lib in libs) and (
                    ends & {"L1", "L2", "X1", "X2"}
                    or any("transformer" in lib or "offpage" in lib for lib in libs)
                ):
                    continue
            # Never delete the starter series (ES1→PB→coil). contacts_only /
            # two_numbers / checker_wire remove left ES1 on L1 with no load path.
            if wire is not None and kind in {
                "contacts_only",
                "self_wire",
                "skipped_contacts",
                "two_numbers",
                "checker_wire",
            }:
                if _is_starter_series_wire(session, wire):
                    continue
            if _delete_wire(session, str(wid)):
                removed.append(str(wid))
        if not removed:
            raise ValueError(f"could not remove wire for {gap_id}")
        return {"kind": "challenge", "gap": gap_id, "removed": "wire", "targets": removed, "source": "user"}

    if act == "remove_device":
        tag = str(question.get("tag") or question.get("ref") or "")
        if kind == "unread_device" and (not tag or tag in {"?", "unread"} or tag == str(question.get("sheet") or "")):
            removed = _remove_needs_info(session, str(question.get("sheet") or ""))
            if not removed:
                raise ValueError(f"no reader-produced unread device for {gap_id}")
            return {"kind": "challenge", "gap": gap_id, "removed": "device", "targets": removed, "source": "user"}
        if not tag:
            raise ValueError(f"no device for {gap_id}")
        # Facts-only: OCR near-match of a printed interlock (UA-22→M1-23) must
        # be retagged, not deleted. Removing it drops 109/102 and still used to
        # issue a sheet missing the device the scan clearly shows.
        tag_u = tag.upper()
        if kind in {"orphan_contact", "unconnected", "coil_without_contacts"} and tag_u in {
            "UA-22",
            "UA22",
            "1M",
            "M1-23",
        }:
            if not _find(session, "M1-23") or tag_u == "M1-23":
                raise ValueError(
                    f"retag {tag} to M1-23; removing the printed series interlock leaves the rung incomplete"
                )
        if not _remove_device(session, tag) and not _rename_tag(session, tag, "?", ""):
            raise ValueError(f"could not remove device {tag}")
        # Record when a near-match orphan was dropped so completion can still
        # block if the printed M1-23 never lands.
        if tag_u in {"UA-22", "UA22", "1M"}:
            props = session.project.properties if isinstance(session.project.properties, dict) else {}
            block = props.setdefault("print_import", {})
            if isinstance(block, dict):
                dropped = list(block.get("dropped_near_match") or [])
                dropped.append({"tag": tag, "want": "M1-23"})
                block["dropped_near_match"] = dropped
                session.project.properties = props
        return {"kind": "challenge", "gap": gap_id, "removed": "device", "target": tag, "source": "user"}

    if act == "retag":
        old = str(question.get("tag") or "")
        new = str(action.get("value") or "").strip()
        if new.upper() == "M1-23":
            new = "M1-23"
        else:
            new = new.strip().upper()
        if not old or not new:
            raise ValueError(f"no retag for {gap_id}")
        # Recover library when OCR caption was junk but the symbol is known.
        library = ""
        if new == "M1-23":
            library = "nema.contactors.aux_no"
        elif new == "OL1":
            library = "nema.protection.ol_contact"
        elif new.upper().startswith("PL"):
            library = "nema.operators.pilot_light"
        if not _rename_tag(session, old, new, library):
            raise ValueError(f"could not retag {old} to {new}")
        return {"kind": "retag", "gap": gap_id, "old": old, "tag": new, "source": "user"}

    if act == "confirm_tag":
        tag = str(question.get("tag") or action.get("value") or "")
        comp = _find(session, tag)
        if comp is None:
            raise ValueError(f"no device {tag}")
        props = comp.properties if isinstance(comp.properties, dict) else {}
        props.pop("reconstruct_needs_info", None)
        props.pop("print_import_needs_info", None)
        props.pop("needs_missing", None)
        props["print_import_confirmed"] = "yes"
        props["print_import_source"] = "user"
        props["print_import_answered_at"] = _now()
        comp.properties = props
        return {"kind": "confirm", "tag": tag, "source": "user"}

    if act == "open_stub":
        tag = str(question.get("tag") or "")
        pin = str(question.get("pin") or "")
        comp = _find(session, tag)
        if comp is None:
            raise ValueError(f"no device {tag}")
        sheet = _sheet(session, str(question.get("sheet") or _owner_sheet(session, tag)))
        from panelcad.importer.cleanup import _wired_pin_ids, _device

        wired = _wired_pin_ids(sheet, tag) if sheet is not None else set()
        pins: list[str]
        if pin == "secondary":
            device = _device(session, comp.library_id)
            pins = [str(item.id) for item in device.pins if str(item.id).upper().startswith("X")] if device is not None else []
            pins = [item for item in pins if item not in wired]
        else:
            pins = [pin] if pin and pin not in wired else []
        if not pins:
            raise ValueError(f"no open pin on {tag}")
        props = comp.properties if isinstance(comp.properties, dict) else {}
        existing = {part.strip() for part in str(props.get("print_open_stubs") or "").split(",") if part.strip()}
        for item in pins:
            existing.add(item)
        kept = sorted(p for p in existing if p not in wired)
        props["print_open_stubs"] = ",".join(kept)
        props["print_open_stub_confirmed"] = "yes"
        props["print_import_source"] = "user"
        props["print_import_answered_at"] = _now()
        comp.properties = props
        return {"kind": "open_stub", "gap": gap_id, "tag": tag, "pins": kept, "source": "user"}

    if act == "secondary_rails":
        tag = str(question.get("tag") or "")
        sheet_id = str(question.get("sheet") or _owner_sheet(session, tag) or "")
        from panelcad.importer.cleanup import land_transformer_secondary_rails

        landed = land_transformer_secondary_rails(
            session,
            sheet_id,
            tag,
            hot=str(action.get("hot") or "L1"),
            hot_number=str(action.get("hot_number") or "102"),
            ret=str(action.get("ret") or "L2"),
            ret_number=str(action.get("ret_number") or "104"),
            fuse_tag=str(action.get("fuse_tag") or ""),
        )
        return {
            "kind": "secondary_rails",
            "gap": gap_id,
            "tag": tag,
            "landed": landed,
            "source": "user",
        }

    if act == "offpage_feed":
        tag = str(question.get("tag") or "")
        pin = str(question.get("pin") or "")
        label = str(action.get("label") or "").strip() or "EXT"
        sheet_id = str(question.get("sheet") or _owner_sheet(session, tag) or "")
        from panelcad.importer.cleanup import land_primary_offpage_feed

        if pin == "secondary" or str(pin).upper().startswith("X"):
            raise ValueError("transformer secondary feeds L1/L2, not an off-page primary")
        landed = land_primary_offpage_feed(session, sheet_id, tag, pin, label)
        landed["gap"] = gap_id
        landed["source"] = "user"
        return landed

    if act == "return":
        tag = str(question.get("tag") or "")
        pin = str(question.get("pin") or "")
        other = str(action.get("value") or "").strip()
        number = str(action.get("number") or "").strip()
        if not tag or not pin or not other:
            raise ValueError(f"no return for {gap_id}")
        frm = f"{tag}.{pin}"
        to = other if "." in other else other  # rail name ok
        if not number and to.upper() in {"L2", "X2"}:
            number = "104"
        elif not number and to.upper() in {"L1", "X1"}:
            number = "102"
        accepted = [{"kind": "return", "gap": gap_id, "from": frm, "to": to, "number": number}]
        added = _apply(session, accepted)
        if not added:
            raise ValueError(f"could not land return for {gap_id}")
        added[0]["source"] = "user"
        return added[0]

    if act == "connect":
        tag = str(question.get("tag") or "")
        pin = str(question.get("pin") or "")
        other = str(action.get("to") or "").strip()
        number = str(action.get("number") or "").strip()
        if not pin and kind == "unconnected":
            from panelcad.importer.cleanup import _device, _side_pin

            host = _find(session, tag)
            device = _device(session, host.library_id) if host is not None else None
            pin = _side_pin(device, "left") if device is not None else "3"
            pin = pin or "3"
        # Secondary fuse answered "L1 as 102": land X1→fuse→L1 (never a bare fuse→L1 alone).
        host = _find(session, tag)
        if (
            host is not None
            and "fuse" in str(host.library_id or "").lower()
            and other.upper() in {"L1", "X1"}
            and number == "102"
        ):
            from panelcad.importer.cleanup import (
                find_secondary_fuse_tag,
                fuse_is_secondary,
                land_transformer_secondary_hot_via_fuse,
            )

            sheet_id = str(question.get("sheet") or _owner_sheet(session, tag) or "")
            if fuse_is_secondary(session, tag, sheet_id):
                xfmr = ""
                for comp in (_sheet(session, sheet_id).components if _sheet(session, sheet_id) else []):
                    if "transformer" in str(comp.library_id or "").lower():
                        if find_secondary_fuse_tag(session, sheet_id, comp.id) == tag or not xfmr:
                            xfmr = comp.id
                if xfmr:
                    hot = other.upper() if other.upper() in {"L1", "X1"} else "L1"
                    landed = land_transformer_secondary_hot_via_fuse(
                        session, sheet_id, xfmr, tag, hot=hot, hot_number="102"
                    )
                    # Never report applied when no FU→L1 wire exists on the sheet.
                    sheet = _sheet(session, sheet_id)
                    hot_u = hot.upper()
                    landed_hot = False
                    if sheet is not None:
                        for wire in sheet.wires:
                            ends = {str(wire.frm), str(wire.to)}
                            ends_u = {e.upper() for e in ends}
                            if not any(e.startswith(f"{tag}.") for e in ends):
                                continue
                            if hot_u in ends_u or hot in ends:
                                wire.number = "102"
                                wire.number_locked = True
                                landed_hot = True
                                break
                    if not landed_hot:
                        raise ValueError(f"secondary fuse {tag} did not land on L1 as 102")
                    from panelcad.core.elementary_layout import layout_elementary

                    if sheet is not None:
                        layout_elementary(session, sheet)
                    return {
                        "kind": "secondary_fuse_rail",
                        "gap": gap_id,
                        "tag": tag,
                        "landed": landed,
                        "source": "user",
                    }
        # Transformer X2 → L2 as 104 (X1 already through the fuse).
        if (
            host is not None
            and "transformer" in str(host.library_id or "").lower()
            and pin.upper() == "X2"
            and other.upper() in {"L2", "X2"}
            and (not number or number == "104")
        ):
            from panelcad.importer.cleanup import land_transformer_secondary_return

            sheet_id = str(question.get("sheet") or _owner_sheet(session, tag) or "")
            from panelcad.importer.cleanup import _wired_pin_ids

            landed = land_transformer_secondary_return(
                session, sheet_id, tag, ret=other.upper() if other.upper() in {"L2", "X2"} else "L2", ret_number="104"
            )
            sheet = _sheet(session, sheet_id)
            if sheet is None or "X2" not in _wired_pin_ids(sheet, tag):
                raise ValueError(f"{tag}.X2 did not land on L2 as 104")
            return {
                "kind": "secondary_return",
                "gap": gap_id,
                "tag": tag,
                "landed": landed,
                "source": "user",
            }
        # Secondary gap answered with a single rail: map L1→X1, L2→X2.
        if pin == "secondary":
            from panelcad.importer.cleanup import _device, land_transformer_secondary_rails

            host = _find(session, tag)
            device = _device(session, host.library_id) if host is not None else None
            sec = [str(p.id) for p in device.pins if str(p.id).upper().startswith("X")] if device else []
            other_u = other.upper()
            if other_u in {"L1", "X1"} and number == "102" and len(sec) >= 1:
                pin = sec[0]
            elif other_u in {"L2", "X2"} and number == "104" and len(sec) >= 2:
                pin = sec[1]
            elif other_u in {"L1", "X1"} and len(sec) >= 1:
                pin = sec[0]
            elif other_u in {"L2", "X2"} and len(sec) >= 2:
                pin = sec[1]
            else:
                # One-click both rails when the answer names either rail with 102/104 pair intent.
                sheet_id = str(question.get("sheet") or _owner_sheet(session, tag) or "")
                landed = land_transformer_secondary_rails(session, sheet_id, tag)
                return {"kind": "secondary_rails", "gap": gap_id, "tag": tag, "landed": landed, "source": "user"}
        if not tag or not pin or pin == "secondary" or not other or _SHEET_LINE.match(number) is None:
            raise ValueError(f"no connect for {gap_id}")
        sheet_id = str(question.get("sheet") or _owner_sheet(session, tag) or "")
        # Upgrade / place a needs_info pilot/OL before wiring so .X1 / .95 pins
        # exist. Also resolve when the mate tag is only an OCR unread name
        # (PL1.X1 offered before the lamp was upgraded onto the sheet).
        for end in (tag, other.split(".", 1)[0] if "." in other else ""):
            if not end:
                continue
            host_end = _find(session, end)
            needs = host_end is not None and (
                "needs_info" in str(host_end.library_id or "").lower()
                or _is_needs_info_comp(host_end)
            )
            missing_mate = host_end is None and (
                end.upper().startswith(("PL", "OL", "LT", "HL"))
                or end.upper() in {"M1-23"}
            )
            if not needs and not missing_mate:
                continue
            resolved = _resolve_unread_tag(session, sheet_id, end)
            if resolved is None:
                tip = pin if end == tag else other.split(".", 1)[-1]
                raise ValueError(
                    f"unread device '{end}' has no placeable mark"
                    if missing_mate
                    else f"needs_info has no pin or port '{tip}'"
                )
        frm = f"{tag}.{pin}"
        try:
            wire = session.wire(frm, other, number=number, sheet_id=sheet_id or None)
        except (ValueError, KeyError, AssertionError) as exc:
            raise ValueError(str(exc).splitlines()[0]) from exc
        _stamp_wire_user(session, wire)
        _confirm_print_item_number(session, number, wire.id)
        # Rail feed replaces a reader open_stub guess on this pin.
        host = _find(session, tag)
        if host is not None and pin:
            props = host.properties if isinstance(host.properties, dict) else {}
            existing = {
                part.strip()
                for part in str(props.get("print_open_stubs") or "").split(",")
                if part.strip() and part.strip() != pin
            }
            if existing:
                props["print_open_stubs"] = ",".join(sorted(existing))
            elif "print_open_stubs" in props:
                props.pop("print_open_stubs", None)
            host.properties = props
        return {
            "kind": "connect",
            "gap": gap_id,
            "from": frm,
            "to": other,
            "number": number,
            "wire_id": wire.id,
            "source": "user",
        }

    if act == "repin":
        wire_id = str(question.get("wire_id") or "")
        wire = _wire_by_id(session, wire_id)
        new_end = str(action.get("to") or "").strip()
        number = str(action.get("number") or "").strip()
        if wire is None or not new_end or "." not in new_end or _SHEET_LINE.match(number) is None:
            raise ValueError(f"no repin for {gap_id}")
        from panelcad.render.sheet import parse_pin_ref

        try:
            new_ref, new_pin = parse_pin_ref(new_end)
        except ValueError as exc:
            raise ValueError(str(exc).splitlines()[0]) from exc
        # Move the endpoint that shares the device tag; keep the other end.
        old_frm, old_to = str(wire.frm), str(wire.to)
        if old_frm.startswith(f"{new_ref}."):
            wire.frm = new_end
        elif old_to.startswith(f"{new_ref}."):
            wire.to = new_end
        else:
            raise ValueError(f"repin {new_end} does not match wire ends {old_frm}/{old_to}")
        if number and str(wire.number or "").strip() in {"", "?"}:
            wire.number = number
        _stamp_wire_user(session, wire)
        return {
            "kind": "repin",
            "gap": gap_id,
            "wire_id": wire.id,
            "from": str(wire.frm),
            "to": str(wire.to),
            "number": str(wire.number or number),
            "source": "user",
        }

    if act == "resolve_unread":
        tag = str(action.get("value") or "").strip()
        sheet_id = str(question.get("sheet") or "")
        if not tag:
            raise ValueError(f"no tag for {gap_id}")
        resolved = _resolve_unread_tag(session, sheet_id, tag)
        if not resolved:
            raise ValueError(f"could not resolve unread device to {tag}")
        return {"kind": "unread_device", "gap": gap_id, "tag": tag, "resolved": resolved, "source": "user"}

    raise ValueError(f"cannot apply action {act} for {kind}")


def _remove_needs_info(session: Session, sheet_id: str) -> list[str]:
    """Remove reader-produced needs_info / pinless placeholders on the sheet."""

    removed: list[str] = []
    for sheet in session.project.sheets:
        if sheet_id and sheet.id != sheet_id:
            continue
        for comp in list(sheet.components):
            lib = str(comp.library_id or "").lower()
            # Only true placeholders — never a retagged catalog device that still
            # carries a stale reconstruct_needs_info flag.
            if "needs_info" not in lib and str(comp.id) not in {"?", ""}:
                continue
            if _remove_device(session, comp.id):
                removed.append(comp.id)
    return removed


def _is_needs_info_comp(comp: Any) -> bool:
    lib = str(getattr(comp, "library_id", "") or "").lower()
    if "needs_info" in lib or str(getattr(comp, "id", "") or "") in {"?", ""}:
        return True
    props = getattr(comp, "properties", None)
    if isinstance(props, dict) and str(props.get("reconstruct_needs_info") or "").lower() in {
        "1",
        "true",
        "yes",
    }:
        return True
    return False


def _needs_info_tag_keys(comp: Any) -> set[str]:
    """Ids / printed captions that identify this placeholder for a resolve."""

    keys = {str(getattr(comp, "id", "") or "").strip().upper()}
    props = getattr(comp, "properties", None)
    if isinstance(props, dict):
        for key in ("print_tag", "label_text", "ocr_tag", "tag"):
            text = str(props.get(key) or "").strip().upper()
            if text:
                keys.add(text)
    return {k for k in keys if k}


def _resolve_unread_tag(session: Session, sheet_id: str, tag: str) -> dict[str, Any] | None:
    """Apply a printed tag to an unread reader symbol.

    Prefer replacing a needs_info placeholder with a catalog device named by the
    OCR item. Otherwise retag an untagged / needs_info symbol to the printed tag.
    """

    payload = dict(session.project.properties.get("print_import") or {})
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    library_id = ""
    for item in items:
        if not isinstance(item, dict):
            continue
        if sheet_id and str(item.get("sheet") or "") not in {"", sheet_id}:
            continue
        text = str(item.get("tag") or item.get("text") or "").strip()
        if text.upper() != tag.upper():
            continue
        library_id = str(item.get("library_id") or "").strip()
        # A needs_info / placeholder library is not a real symbol — fall through
        # to the PL*/OL1 catalog defaults so CR1-13.4→PL1.X1 can land.
        if library_id and "needs_info" in library_id.lower():
            library_id = ""
            continue
        if library_id:
            break
    want = str(tag or "").strip().upper()
    matched: list[Any] = []
    anonymous: list[Any] = []
    for sheet in session.project.sheets:
        if sheet_id and sheet.id != sheet_id:
            continue
        for comp in sheet.components:
            if not _is_needs_info_comp(comp):
                continue
            # Already-catalog devices that only carry a stale needs_info flag
            # (e.g. UA-22→M1-23 retag) are not unread placeholders.
            lib = str(comp.library_id or "").lower()
            if "needs_info" not in lib and str(comp.id) not in {"?", ""}:
                if want and want in _needs_info_tag_keys(comp):
                    # Same printed tag answered again: clear the stale flag.
                    props = comp.properties if isinstance(comp.properties, dict) else {}
                    props.pop("reconstruct_needs_info", None)
                    props.pop("print_import_needs_info", None)
                    props.pop("needs_missing", None)
                    props["print_import_source"] = "user"
                    props["print_import_confirmed"] = "yes"
                    props["print_import_answered_at"] = _now()
                    comp.properties = props
                    return {"cleared": comp.id, "tag": tag}
                continue
            if want and want in _needs_info_tag_keys(comp):
                matched.append(comp)
            elif str(comp.id) in {"?", ""} or "needs_info" in lib:
                anonymous.append(comp)
    target = matched[0] if matched else (anonymous[0] if anonymous else None)
    if target is None and tag.upper() == "M1-23":
        # OCR caption UA-22 / 1M is the printed M1-23 aux — retag it when the
        # user lands M1-23.4 as 109 before a separate suspect_tag answer.
        for alias in ("UA-22", "UA22", "1M"):
            alias_comp = _find(session, alias)
            if alias_comp is not None:
                target = alias_comp
                break
    if target is None:
        # Tag already exists as a real device — nothing to resolve.
        existing = _find(session, tag)
        if existing is not None and "needs_info" not in str(existing.library_id or "").lower():
            props = existing.properties if isinstance(existing.properties, dict) else {}
            props.pop("reconstruct_needs_info", None)
            props.pop("print_import_needs_info", None)
            props["print_import_source"] = "user"
            props["print_import_confirmed"] = "yes"
            props["print_import_answered_at"] = _now()
            existing.properties = props
            return {"confirmed": existing.id, "tag": tag}
        return None
    old = target.id
    # A PL* answer on a needs_info mark is a pilot light even when OCR never
    # confirmed the symbol — otherwise completion stays blocked forever.
    if not library_id and tag.upper().startswith("PL"):
        for cand in ("nema.operators.pilot_light", "iec.operators.pilot_light"):
            if cand in session.catalog.devices:
                library_id = cand
                break
    if not library_id and tag.upper() == "OL1":
        for cand in ("nema.protection.ol_contact", "iec.protection.ol_contact"):
            if cand in session.catalog.devices:
                library_id = cand
                break
    if not library_id and tag.upper() == "M1-23":
        for cand in ("nema.contactors.aux_no", "iec.contactors.aux_no"):
            if cand in session.catalog.devices:
                library_id = cand
                break
    if library_id and "needs_info" in str(target.library_id or "").lower():
        # Same tag: upgrade the library in place so existing leftover wires stay.
        if old == tag:
            target.library_id = library_id
            props = target.properties if isinstance(target.properties, dict) else {}
            props.pop("reconstruct_needs_info", None)
            props.pop("print_import_needs_info", None)
            props.pop("needs_missing", None)
            props["print_import_source"] = "user"
            props["print_import_confirmed"] = "yes"
            props["print_import_answered_at"] = _now()
            target.properties = props
            return {"upgraded": old, "tag": tag, "library_id": library_id}
        x, y = float(target.x), float(target.y)
        # Preserve conductors that already named this placeholder.
        owner = None
        for sheet in session.project.sheets:
            if any(c is target for c in sheet.components):
                owner = sheet
                break
        saved = []
        if owner is not None:
            for wire in list(owner.wires):
                ends = (str(wire.frm), str(wire.to))
                if any(end == old or end.startswith(old + ".") for end in ends):
                    saved.append((str(wire.frm), str(wire.to), str(wire.number or "")))
        _remove_device(session, old)
        placed = session.place(library_id, ref=tag, x=x, y=y, sheet_id=sheet_id or None, snap=False)
        props = placed.properties if isinstance(placed.properties, dict) else {}
        props["print_import_source"] = "user"
        props["print_import_confirmed"] = "yes"
        props["print_import_answered_at"] = _now()
        placed.properties = props
        for frm, to, number in saved:
            new_frm = f"{tag}{frm[len(old):]}" if frm == old or frm.startswith(old + ".") else frm
            new_to = f"{tag}{to[len(old):]}" if to == old or to.startswith(old + ".") else to
            try:
                session.wire(new_frm, new_to, number=number or None, sheet_id=sheet_id or None)
            except Exception:  # noqa: BLE001
                continue
        return {"replaced": old, "tag": tag, "library_id": library_id}
    if not _rename_tag(session, old, tag, library_id or ""):
        return None
    comp = _find(session, tag)
    if comp is not None:
        props = comp.properties if isinstance(comp.properties, dict) else {}
        props.pop("reconstruct_needs_info", None)
        props.pop("print_import_needs_info", None)
        props.pop("needs_missing", None)
        props["print_import_source"] = "user"
        props["print_import_confirmed"] = "yes"
        props["print_import_answered_at"] = _now()
        comp.properties = props
    return {"retag": old, "tag": tag}


def _stamp_wire_user(session: Session, wire: Any, *, number_confirmed: bool = True) -> None:
    """Record a user-asserted wire number on the project (wires have no properties)."""

    payload = dict(session.project.properties.get("print_import") or {})
    if number_confirmed:
        confirmed = dict(payload.get("confirmed_wire_numbers") or {})
        confirmed[str(wire.id)] = str(wire.number or "").strip()
        payload["confirmed_wire_numbers"] = confirmed
    payload.setdefault("leftover_wire_sources", {})
    if isinstance(payload["leftover_wire_sources"], dict):
        payload["leftover_wire_sources"][str(wire.id)] = {
            "source": "user",
            "at": _now(),
            "number": str(wire.number or "").strip(),
        }
    session.project.properties["print_import"] = payload


def _confirm_print_item_number(session: Session, number: str, wire_id: str = "") -> None:
    """Close the stored OCR token so unattached_number does not re-ask."""

    if not number:
        return
    payload = dict(session.project.properties.get("print_import") or {})
    items = list(payload.get("items") or [])
    changed = False
    for item in items:
        if not isinstance(item, dict) or str(item.get("kind") or "") != "wire":
            continue
        text = str(item.get("text") or item.get("number") or "").strip()
        if text != number:
            continue
        item["confirmed"] = True
        if wire_id:
            item["attached_wire"] = wire_id
        changed = True
    if changed:
        payload["items"] = items
        session.project.properties["print_import"] = payload


def _page_images(session: Session, image: str, pages: list[dict[str, Any]] | None) -> dict[str, str]:
    from panelcad.importer.cleanup import _page_images as cleanup_page_images

    return cleanup_page_images(session, image, pages)


def _sheet_crop(session: Session, gap: dict[str, Any], row: dict[str, Any]) -> str:
    """Crop of the rebuilt sheet around the wire/device. Empty when Qt is unavailable."""

    try:
        from panelcad.importer.preview import attach_rebuilt_previews
    except Exception:  # noqa: BLE001
        return ""
    payload = dict(getattr(session.project, "properties", {}).get("print_import") or {})
    dest_root = Path(str(payload.get("prep_dir") or ""))
    if session.path:
        project = Path(session.path)
        dest_root = project.parent / f"{project.stem}.leftover-questions"
    elif not dest_root:
        dest_root = Path("/tmp/panelcad-leftover-questions")
    dest_root.mkdir(parents=True, exist_ok=True)
    kind = "wire" if str(gap.get("kind") or "") in _WIRE_KINDS or gap.get("wire_id") else "device"
    text = str(gap.get("tag") or gap.get("number") or gap.get("wire_id") or "")
    item = {
        "id": str(gap.get("id") or "").replace(":", "_"),
        "kind": kind,
        "text": text if kind == "device" else "",
        "page_id": str(gap.get("sheet") or ""),
        "payload": {"x": gap.get("x"), "y": gap.get("y")},
    }
    if kind == "wire":
        wire = _wire_by_id(session, str(gap.get("wire_id") or ""))
        if wire is None and str(gap.get("id") or "").startswith("wire:"):
            wire = _wire_by_id(session, str(gap.get("id") or "").split(":", 1)[1])
        if wire is not None:
            # Anchor mid-wire using endpoint component positions when possible.
            xs: list[float] = []
            ys: list[float] = []
            for end in (wire.frm, wire.to):
                ref = str(end).split(".", 1)[0]
                comp = _find(session, ref)
                if comp is not None:
                    xs.append(float(comp.x))
                    ys.append(float(comp.y))
            if xs and ys:
                item["payload"] = {"x": sum(xs) / len(xs), "y": sum(ys) / len(ys)}
                item["kind"] = "device"
                item["text"] = ""
    try:
        attach_rebuilt_previews(session, [item], dest_root)
    except Exception:  # noqa: BLE001
        return ""
    return str(item.get("rebuilt") or "")
