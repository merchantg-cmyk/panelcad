"""Short review queue for a Photos → drawings run.

The queue holds only items the run must stop on. Anything that is not flagged
passes through and is not listed. ``kind`` is an open string, not an enum, so
a later detector can enqueue its own rows (a terminal strip, a duct, a rail)
without a change here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# A read at or above this floor is not a low-confidence stop.
# The same floor the print importer uses, kept local so this queue does not
# import the importer.
REVIEW_CONFIDENCE_FLOOR = 0.6

STOP_REASONS = ("missing_info", "low_confidence", "possible_missed_device")

REASON_LABEL = {
    "missing_info": "Missing info",
    "low_confidence": "Low confidence",
    "possible_missed_device": "Possible missed device",
}

_OWN_FIELDS = (
    "id",
    "kind",
    "reason",
    "title",
    "detail",
    "photo_id",
    "source_id",
    "confidence",
    "payload",
)


class FlaggedItem(BaseModel):
    """One row. ``kind`` is whatever the detector called the thing."""

    id: str
    kind: str = "item"
    reason: str
    title: str
    detail: str = ""
    photo_id: str = ""
    source_id: str = ""
    confidence: float | None = None
    # Fields a detector needs later. The queue does not interpret them.
    payload: dict[str, Any] = Field(default_factory=dict)


class ReviewQueue(BaseModel):
    items: list[FlaggedItem] = Field(default_factory=list)

    def accept(self, **fields: Any) -> FlaggedItem:
        """Enqueue any flagged item.

        Unknown keys are stored on ``payload``. ``kind`` may be any string.
        """

        raw = dict(fields)
        payload = dict(raw.pop("payload", None) or {})
        extra = {key: raw.pop(key) for key in list(raw) if key not in _OWN_FIELDS}
        payload.update(extra)
        reason = str(raw.get("reason") or "").strip() or "missing_info"
        kind = str(raw.get("kind") or "item").strip() or "item"
        title = str(raw.get("title") or "").strip() or kind.replace("_", " ")
        item_id = str(raw.get("id") or "").strip() or f"flag-{len(self.items) + 1}"
        confidence = raw.get("confidence", None)
        if confidence in ("", None):
            confidence_value: float | None = None
        else:
            try:
                confidence_value = float(confidence)
            except (TypeError, ValueError):
                confidence_value = None
        item = FlaggedItem(
            id=item_id,
            kind=kind,
            reason=reason,
            title=title,
            detail=str(raw.get("detail") or ""),
            photo_id=str(raw.get("photo_id") or ""),
            source_id=str(raw.get("source_id") or ""),
            confidence=confidence_value,
            payload=payload,
        )
        self.items = [row for row in self.items if row.id != item.id]
        self.items.append(item)
        return item

    def dismiss(self, item_id: str) -> None:
        before = len(self.items)
        self.items = [row for row in self.items if row.id != item_id]
        if len(self.items) == before:
            raise KeyError(item_id)

    def pending(self) -> list[FlaggedItem]:
        return list(self.items)

    def empty(self) -> bool:
        return not self.items

    def by_kind(self, kind: str) -> list[FlaggedItem]:
        return [row for row in self.items if row.kind == kind]

    def public(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self.items]


def queue_line(item: FlaggedItem) -> str:
    """One short line for the review list."""

    reason = REASON_LABEL.get(item.reason, item.reason.replace("_", " ").capitalize())
    if item.kind in {"part", "device", "photo", "item"}:
        return f"{reason} — {item.title}"
    return f"{reason} — {item.kind.replace('_', ' ')}: {item.title}"
