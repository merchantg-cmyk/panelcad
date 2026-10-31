"""Photos → drawings: one run from imported photos to generated drawings.

Photo intake, the cabinet photo overlay, device review, and drawing generation
stay separate steps. This run calls those steps and stops only for a short
review queue. Unflagged items are confirmed and placed without a review stop.

The queue is not limited to devices. Pass ``extra_flags`` or call
``ReviewQueue.accept`` with any ``kind`` to stop the run for a later detector.
"""

from __future__ import annotations

from typing import Any

from panelcad.reconstruct.geometry import mm_per_px
from panelcad.reconstruct.match import LIKELY_CONFIDENCE, compact
from panelcad.reconstruct.models import (
    CatalogMatch,
    ObservedItem,
    Photo,
    catalog_match_has_footprint,
    ensure_reconstruction,
    item_is_layout_ready,
)
from panelcad.reconstruct.review_queue import REVIEW_CONFIDENCE_FLOOR, ReviewQueue
from panelcad.reconstruct.service import apply_to_print, confirm_item, identify, place_confirmed
from panelcad.reconstruct.vision import NO_AI_MESSAGE

# A second hit at or above this is another real candidate.
# Brand-only (0.2) and description (0.45) glances are not.
_PLAUSIBLE_MATCH = 0.5

_CALLOUT_OWN = {"id", "photo_id", "kind", "label", "title", "detail", "confidence", "reason"}


def run_photos_to_drawings(
    session: Any,
    *,
    vision: Any = None,
    recordings: dict[str, Any] | None = None,
    unboxed_callouts: Any = None,
    extra_flags: list[dict[str, Any]] | None = None,
    accepted_ids: list[str] | None = None,
    dismissed_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Read imported photos, pass unflagged items, and generate drawings.

    When the review queue is not empty the run stops before drawing generation.
    Items that are not flagged are still confirmed and placed.
    """

    store = ensure_reconstruction(session.project)
    if not store.photos:
        raise ValueError("Add photos before Photos → drawings.")
    identified = identify(session, vision=vision, recordings=recordings)
    accepted = {str(item) for item in (accepted_ids or []) if str(item).strip()}
    dismissed = {str(item) for item in (dismissed_ids or []) if str(item).strip()}
    offline = _offline(identified)
    created_ids = {str(row.get("id") or "") for row in (identified.get("items") or [])}
    _auto_confirm(session, accepted, created_ids)
    queue = _collect(
        session,
        accepted=accepted,
        dismissed=dismissed,
        offline=offline,
        created_ids=created_ids,
        unboxed_callouts=unboxed_callouts,
        extra_flags=extra_flags,
    )
    store = ensure_reconstruction(session.project)
    flagged_sources = {row.source_id for row in queue.items if row.source_id}
    flagged_item_ids = {row.id for row in queue.items}
    passed = [
        item.id
        for item in store.items
        if item.id not in flagged_sources and f"item:{item.id}" not in flagged_item_ids
    ]
    drawings: dict[str, Any] | None = None
    if queue.items:
        # Pass unflagged parts onto the layout. Drawing generation waits.
        place_confirmed(session)
        stopped = True
        generated = False
    else:
        drawings = apply_to_print(session)
        stopped = False
        generated = True
    # Identify may already have dropped a marker that confirm then upgraded.
    # Count layout-ready parts on a sheet, not only this call's new rows.
    placed = _ready_placed(session)
    summary = _summary(stopped, len(queue.items), len(passed), len(placed), drawings)
    return {
        "stopped": stopped,
        "drawings_generated": generated,
        "summary": summary,
        "queue": queue.public(),
        "passed": passed,
        "placed": placed,
        "sheet_id": _sheet_id(placed, drawings),
        "drawings": drawings,
        "identify": {
            "backend": identified.get("backend"),
            "warning": identified.get("warning") or "",
            "count": len(identified.get("items") or []),
        },
    }


def _offline(identified: dict[str, Any]) -> bool:
    if str(identified.get("backend") or "") == "stub":
        return True
    warning = str(identified.get("warning") or "").lower()
    return "no ai" in warning or "aren't read automatically" in warning


def _accepted(item: ObservedItem, accepted: set[str]) -> bool:
    return item.id in accepted or f"item:{item.id}" in accepted


def _footprint(item: ObservedItem) -> bool:
    return bool(item.matches) and catalog_match_has_footprint(item.matches[0])


def _identity(item: ObservedItem) -> bool:
    for value in (
        item.tag,
        item.part_number,
        item.label_text,
        item.terminal,
        item.wire_number,
        item.brand,
    ):
        if str(value or "").strip():
            return True
    return False


def _read_ok(item: ObservedItem, accepted: set[str]) -> bool:
    if float(item.confidence or 0) >= REVIEW_CONFIDENCE_FLOOR:
        return True
    return _accepted(item, accepted)


def _label_number(item: ObservedItem) -> str:
    return str(item.label_part_number or item.part_number or "").strip()


def _exact_catalog(item: ObservedItem, match: CatalogMatch) -> bool:
    label = compact(_label_number(item))
    return bool(label) and label == compact(match.catalog)


def _plausible_matches(item: ObservedItem) -> list[CatalogMatch]:
    seen: set[str] = set()
    found: list[CatalogMatch] = []
    for match in item.matches:
        token = compact(match.catalog)
        if not token or token in seen:
            continue
        if float(match.confidence or 0) < _PLAUSIBLE_MATCH:
            continue
        seen.add(token)
        found.append(match)
    return found


def _match_is_sure(item: ObservedItem) -> bool:
    """Top hit is likely or an exact catalog number, and nothing else is plausible."""

    if not _footprint(item):
        return False
    top = item.matches[0]
    sure = float(top.confidence or 0) >= LIKELY_CONFIDENCE or _exact_catalog(item, top)
    if not sure:
        return False
    top_token = compact(top.catalog)
    others = [match for match in _plausible_matches(item) if compact(match.catalog) != top_token]
    return not others


def _auto_confirm(session: Any, accepted: set[str], created_ids: set[str]) -> None:
    """Confirm one sure catalog match. Leave needs-info and fuzzy matches alone."""

    store = ensure_reconstruction(session.project)
    for item in list(store.items):
        if item.role != "part" or item_is_layout_ready(item) or item.unreadable:
            continue
        human = _accepted(item, accepted)
        if item.status == "needs_info" and item.id not in created_ids and not human:
            continue
        if not human and not _read_ok(item, accepted):
            continue
        if not _footprint(item):
            continue
        if not human and not _match_is_sure(item):
            continue
        saved = [match.model_copy(deep=True) for match in item.matches]
        top = saved[0]
        try:
            confirm_item(
                session,
                item.id,
                catalog=top.catalog,
                manufacturer=top.manufacturer,
                tag=item.tag,
            )
        except ValueError:
            continue
        # confirm_item looks the catalog number up again and stores that score.
        # The photo label's score is the one the queue and the drawing must keep.
        item.matches = saved


def _block_reason(
    item: ObservedItem,
    photo: Photo,
    accepted: set[str],
    created_ids: set[str],
) -> str | None:
    if _accepted(item, accepted):
        return None
    read_ok = _read_ok(item, accepted)
    if item.role == "part":
        if item_is_layout_ready(item):
            if mm_per_px(photo) is None and not item.placed_ref:
                return "missing_info"
            return None
        if item.unreadable:
            return "missing_info"
        if not read_ok:
            return "low_confidence"
        if item.status == "needs_info" and item.id not in created_ids:
            return "missing_info"
        if not _footprint(item):
            return "missing_info"
        if not _match_is_sure(item):
            return "low_confidence"
        return "missing_info"
    if item.unreadable or not _identity(item):
        return "missing_info"
    if not read_ok:
        return "low_confidence"
    return None


def _collect(
    session: Any,
    *,
    accepted: set[str],
    dismissed: set[str],
    offline: bool,
    created_ids: set[str],
    unboxed_callouts: Any,
    extra_flags: list[dict[str, Any]] | None,
) -> ReviewQueue:
    store = ensure_reconstruction(session.project)
    queue = ReviewQueue()
    items_by_photo: dict[str, list[ObservedItem]] = {}
    for item in store.items:
        items_by_photo.setdefault(item.photo_id, []).append(item)
        if item.id in dismissed or f"item:{item.id}" in dismissed:
            continue
        try:
            photo = store.photo(item.photo_id)
        except KeyError:
            queue.accept(
                id=f"item:{item.id}",
                kind=item.role or "item",
                reason="missing_info",
                title=_title(item),
                detail="This item is not tied to a photo.",
                source_id=item.id,
                confidence=float(item.confidence or 0),
            )
            continue
        reason = _block_reason(item, photo, accepted, created_ids)
        if reason is None:
            continue
        queue.accept(
            id=f"item:{item.id}",
            kind=str(item.role or "item"),
            reason=reason,
            title=_title(item),
            detail=_item_detail(item, photo, reason),
            photo_id=photo.id,
            source_id=item.id,
            confidence=float(item.confidence or 0),
        )
    for photo in store.photos:
        if items_by_photo.get(photo.id):
            continue
        if photo.id in dismissed or f"photo:{photo.id}" in dismissed:
            continue
        reason = "missing_info" if (offline or photo.unreadable) else "possible_missed_device"
        queue.accept(
            id=f"photo:{photo.id}",
            kind="photo",
            reason=reason,
            title=photo.original_name or photo.id,
            detail=_photo_detail(photo, offline),
            photo_id=photo.id,
            source_id=photo.id,
        )
    for photo_id, callout, index in _iter_callouts(unboxed_callouts):
        _add_callout(queue, callout, photo_id, index, dismissed)
    for raw in extra_flags or []:
        _enqueue_extra(queue, raw, dismissed)
    return queue


def _enqueue_extra(queue: ReviewQueue, raw: Any, dismissed: set[str]) -> None:
    """A detector flag with no reason still stops the run. It does not raise."""

    if not isinstance(raw, dict):
        return
    fields = dict(raw)
    flag_id = str(fields.get("id") or "")
    source = str(fields.get("source_id") or "")
    if flag_id in dismissed or source in dismissed:
        return
    if not str(fields.get("reason") or "").strip():
        fields["reason"] = "missing_info"
    try:
        queue.accept(**fields)
    except (TypeError, ValueError):
        fields.pop("confidence", None)
        try:
            queue.accept(**fields)
        except (TypeError, ValueError):
            return


def _title(item: ObservedItem) -> str:
    if item.role == "part":
        return str(item.tag or item.part_number or item.label_text or "Device")
    for value in (item.tag, item.terminal, item.wire_number, item.label_text, item.part_number):
        if str(value or "").strip():
            return str(value).strip()
    return str(item.role or "item").replace("_", " ")


def _item_detail(item: ObservedItem, photo: Photo, reason: str) -> str:
    if reason == "low_confidence":
        conf = float(item.confidence or 0)
        if conf < REVIEW_CONFIDENCE_FLOOR:
            return (
                f"Read confidence {conf:.2f} is below {REVIEW_CONFIDENCE_FLOOR:.2f}. "
                "Confirm the label before this item is placed."
            )
        plausible = _plausible_matches(item)
        if len(plausible) > 1:
            names = ", ".join(match.catalog for match in plausible[:3])
            return f"More than one catalog part fits this label ({names}). Confirm which one."
        if item.matches:
            top = item.matches[0]
            return (
                f"Catalog match {top.catalog} is {float(top.confidence or 0):.2f}, "
                f"below {LIKELY_CONFIDENCE:.2f}. Confirm the part before it is placed."
            )
        return "The read is not confident enough to place."
    if item.unreadable:
        return "The label could not be read."
    if item.role == "part" and not item_is_layout_ready(item) and not _footprint(item):
        return "Missing a catalog part or a measured width, height, depth, and mounting."
    if item.role == "part" and mm_per_px(photo) is None:
        return "The photo has no scale, so this item cannot be placed on the drawing."
    if item.role == "part":
        return "Missing a catalog part or a measured width, height, depth, and mounting."
    return "Missing the text read from the photo."


def _photo_detail(photo: Photo, offline: bool) -> str:
    if offline:
        return NO_AI_MESSAGE
    if photo.unreadable:
        note = str(photo.vision_note or "").strip()
        return note or "This photo could not be read."
    return (
        "Nothing was detected on this photo. "
        "Open the photo feedback overlay if a device was missed."
    )


def _iter_callouts(unboxed: Any):
    if not unboxed:
        return
    if isinstance(unboxed, dict):
        for photo_id, rows in unboxed.items():
            for index, row in enumerate(rows or []):
                if isinstance(row, dict):
                    yield str(photo_id), row, index
        return
    for index, row in enumerate(unboxed):
        if isinstance(row, dict):
            yield str(row.get("photo_id") or ""), row, index


def _add_callout(
    queue: ReviewQueue,
    callout: dict[str, Any],
    photo_id: str,
    index: int,
    dismissed: set[str],
) -> None:
    token = str(callout.get("id") or f"{photo_id}:{index}")
    flag_id = f"missed:{token}"
    if token in dismissed or flag_id in dismissed:
        return
    kind = str(callout.get("kind") or "device").strip() or "device"
    reason = str(callout.get("reason") or "possible_missed_device").strip()
    reason = reason or "possible_missed_device"
    title = str(callout.get("title") or callout.get("label") or "Possible missed device").strip()
    detail = str(callout.get("detail") or "").strip() or (
        "Nothing was detected in this region. "
        "Open the photo feedback overlay to mark it or clear it."
    )
    payload = {key: value for key, value in callout.items() if key not in _CALLOUT_OWN}
    confidence = callout.get("confidence", None)
    queue.accept(
        id=flag_id,
        kind=kind,
        reason=reason,
        title=title,
        detail=detail,
        photo_id=str(callout.get("photo_id") or photo_id),
        source_id=token,
        confidence=confidence,
        payload=payload,
    )


def _summary(
    stopped: bool,
    flagged: int,
    passed: int,
    placed: int,
    drawings: dict[str, Any] | None,
) -> str:
    if stopped:
        noun = "item" if flagged == 1 else "items"
        return f"Stopped for {flagged} flagged {noun}. {passed} passed through without review."
    sheet = str((drawings or {}).get("schematic_sheet") or "")
    drawn = f" Schematic {sheet}." if sheet else ""
    return f"Drawings generated. {placed} placed.{drawn}"


def _ready_placed(session: Any) -> list[dict[str, Any]]:
    store = ensure_reconstruction(session.project)
    rows: list[dict[str, Any]] = []
    for item in store.items:
        if item.role != "part" or not item.placed_ref or not item_is_layout_ready(item):
            continue
        rows.append(
            {
                "id": item.id,
                "ref": item.placed_ref,
                "sheet": item.sheet,
                "x": item.x_mm,
                "y": item.y_mm,
            }
        )
    return rows


def _sheet_id(placed: list[dict[str, Any]], drawings: dict[str, Any] | None) -> str:
    for row in placed:
        sheet = str(row.get("sheet") or "")
        if sheet:
            return sheet
    if drawings:
        return str(drawings.get("schematic_sheet") or "")
    return ""
