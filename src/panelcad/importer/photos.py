"""Panel photos propose a backplate or door layout. They never move a schematic."""

from __future__ import annotations

from typing import Any


def proposals_from_photo(photo: dict[str, Any], extraction: dict[str, Any]) -> dict[str, Any]:
    devices = []
    for item in extraction.get("devices") or []:
        tag = str(item.get("tag") or "").strip()
        unreadable = bool(item.get("unreadable")) or not tag
        devices.append(
            {
                "tag": "" if unreadable else tag,
                "unreadable": unreadable,
                "nx": _frac(item.get("nx", item.get("x"))),
                "ny": _frac(item.get("ny", item.get("y"))),
                "confidence": float(item.get("confidence") or (0.0 if unreadable else 0.6)),
                "photo": photo.get("id") or "",
            }
        )
    return {
        "photo": photo.get("id") or "",
        "image": photo.get("image") or "",
        "devices": devices,
        "rails": list(extraction.get("rails") or []),
        "ducts": list(extraction.get("ducts") or []),
        "strips": list(extraction.get("strips") or []),
    }


def layout_targets(
    proposals: list[dict[str, Any]],
    placed_tags_by_kind: dict[str, set[str]],
) -> list[dict[str, Any]]:
    """Tags the schematic already named, and the print's layout sheet did not place."""

    schematic = set()
    for kind, tags in placed_tags_by_kind.items():
        if kind not in {"panel_layout", "door_layout"}:
            schematic.update(tags)
    laid_out = set(placed_tags_by_kind.get("panel_layout") or set()) | set(
        placed_tags_by_kind.get("door_layout") or set()
    )
    out = []
    seen: set[str] = set()
    for proposal in proposals:
        for device in proposal.get("devices") or []:
            tag = str(device.get("tag") or "")
            if not tag or device.get("unreadable") or tag in seen:
                continue
            if tag not in schematic or tag in laid_out:
                continue
            seen.add(tag)
            out.append(device)
    return out


def _frac(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number > 1.5:
        # Millimetres sneaked in. Caller should have passed a fraction.
        return max(0.0, min(1.0, number / 400.0))
    return max(0.0, min(1.0, number))
