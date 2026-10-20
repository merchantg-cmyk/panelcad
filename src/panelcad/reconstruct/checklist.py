"""Floor checklist: trace wires, confirm parts, compare an old print."""

from __future__ import annotations

import re
from typing import Any

from panelcad.reconstruct.models import ChecklistItem, Reconstruction

_PIN_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9_-]*)\s*[:.]\s*([A-Za-z0-9_-]+)\s*$"
)


def rebuild_checklist(store: Reconstruction) -> list[ChecklistItem]:
    """Keep answered steps; add missing open questions. Photos stay primary."""

    kept = [step for step in store.checklist if step.status in {"answered", "confirmed", "skipped"}]
    existing = {(step.kind, step.item_id, step.wire_number, step.from_ref) for step in kept}
    next_id = _next_prefix("q", [step.id for step in kept])
    added: list[ChecklistItem] = []

    from panelcad.reconstruct.geometry import mm_per_px
    from panelcad.reconstruct.placeholder import missing_fields

    if store.photos and not any(mm_per_px(photo) is not None for photo in store.photos):
        key = ("confirm_scale", "", "", "")
        if key not in existing:
            added.append(
                ChecklistItem(
                    id=next_id(),
                    kind="confirm_scale",
                    prompt=(
                        "Mark two independent known lengths on a backpanel photo, "
                        "or the enclosure inside width and height, so millimetres can be recovered."
                    ),
                    photo_id=store.photos[0].id,
                )
            )
            existing.add(key)

    for item in store.items:
        if item.role == "part" and item.status == "needs_info":
            key = ("needs_info", item.id, "", "")
            if key not in existing:
                missing = ", ".join(field.replace("_", " ") for field in missing_fields(item))
                known = item.label_text or item.tag or item.id
                added.append(
                    ChecklistItem(
                        id=next_id(),
                        kind="needs_info",
                        prompt=(
                            f"{known} is INFO NEEDED. Supply: {missing or 'catalog SKU or W×H×D plus mount'}. "
                            "A catalog match or confirmed envelope converts the placeholder in place."
                        ),
                        item_id=item.id,
                        photo_id=item.photo_id,
                    )
                )
                existing.add(key)
        if item.role == "part" and item.status == "unconfirmed" and not item.unreadable:
            key = ("identify_part", item.id, "", "")
            if key in existing:
                continue
            label = item.part_number or item.label_text or item.tag or item.id
            added.append(
                ChecklistItem(
                    id=next_id(),
                    kind="identify_part",
                    prompt=f"Identify {label} from photo {item.photo_id}: pick a catalog SKU or confirm a measured envelope.",
                    item_id=item.id,
                    photo_id=item.photo_id,
                )
            )
            existing.add(key)
        if item.wire_number:
            origin = item.tag or item.placed_ref
            pin = item.terminal or _nearest_terminal(store, item)
            from_ref = f"{origin}:{pin}" if origin and pin else origin
            key = ("trace_wire", item.id, item.wire_number, from_ref)
            if key in existing:
                continue
            who = f"{origin}:{pin}" if origin and pin else (origin or "the device in the photo")
            added.append(
                ChecklistItem(
                    id=next_id(),
                    kind="trace_wire",
                    prompt=(
                        f"Trace wire {item.wire_number} from {who}. "
                        "Type where the wire lands, like K1:A1."
                    ),
                    item_id=item.id,
                    from_ref=from_ref,
                    wire_number=item.wire_number,
                    photo_id=item.photo_id,
                )
            )
            existing.add(key)

    for step in kept:
        if (
            step.kind == "trace_wire"
            and step.status == "answered"
            and step.to_ref
            and step.wire_id
        ):
            key = ("confirm_connection", step.item_id, step.wire_number, step.from_ref)
            if key not in existing:
                added.append(
                    ChecklistItem(
                        id=next_id(),
                        kind="confirm_connection",
                        prompt=f"Confirm inferred wire {step.wire_number or step.wire_id}: {step.from_ref} → {step.to_ref}",
                        item_id=step.item_id,
                        from_ref=step.from_ref,
                        to_ref=step.to_ref,
                        wire_number=step.wire_number,
                        wire_id=step.wire_id,
                        inferred=True,
                    )
                )
                existing.add(key)

    for mismatch in store.mismatches:
        key = ("compare", mismatch.item_id, mismatch.check, mismatch.print_tag)
        if key in existing:
            continue
        added.append(
            ChecklistItem(
                id=next_id(),
                kind="compare",
                prompt=mismatch.message,
                item_id=mismatch.item_id,
                photo_id=mismatch.photo_id,
            )
        )
        existing.add(key)

    store.checklist = kept + added
    return store.checklist


def _nearest_terminal(store: Reconstruction, item: Any) -> str:
    """Terminal on the same strip, nearest the wire number. Empty if none was read."""

    tag = str(item.tag or "").strip()
    if not tag:
        return ""
    candidates = [
        other
        for other in store.items
        if other.role == "terminal"
        and other.photo_id == item.photo_id
        and other.tag == tag
        and str(other.terminal or "").strip()
    ]
    if not candidates:
        return ""
    if len(candidates) == 1:
        return str(candidates[0].terminal)
    cx = float(item.region.x) + float(item.region.w) / 2.0
    cy = float(item.region.y) + float(item.region.h) / 2.0

    def _distance(other: Any) -> float:
        ox = float(other.region.x) + float(other.region.w) / 2.0
        oy = float(other.region.y) + float(other.region.h) / 2.0
        return (ox - cx) ** 2 + (oy - cy) ** 2

    return str(min(candidates, key=_distance).terminal)


def next_open(store: Reconstruction) -> ChecklistItem | None:
    for step in store.checklist:
        if step.status == "open":
            return step
    return None


def parse_landing(answer: str) -> str:
    """Accept K1:A1, K1.A1, or X1-1 as a landing. Empty if it is free text."""

    text = str(answer or "").strip()
    if not text:
        return ""
    match = _PIN_RE.match(text)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    dashed = re.match(r"^\s*([A-Za-z][A-Za-z0-9_]*)-(\d+)\s*$", text)
    if dashed:
        return f"{dashed.group(1)}-{dashed.group(2)}.A"
    return ""


def public_step(step: ChecklistItem) -> dict[str, Any]:
    return step.model_dump(mode="json")


def _next_prefix(prefix: str, used: list[str]):
    seen = set(used)
    index = 1

    def _next() -> str:
        nonlocal index
        while f"{prefix}{index}" in seen:
            index += 1
        token = f"{prefix}{index}"
        seen.add(token)
        index += 1
        return token

    return _next
