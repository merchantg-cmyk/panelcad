"""Review items: confidence, source crop, accept / fix / reject."""

from __future__ import annotations

from typing import Any


def build_review(pages: list[dict[str, Any]], parts: list[dict[str, Any]], photos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in pages:
        merged = page.get("merged") or {}
        page_id = page["id"]
        crop_fallback = str(page.get("image") or "")
        for index, device in enumerate(merged.get("devices") or []):
            items.append(_item("device", f"{page_id}-dev-{index}", page_id, device, device.get("tag") or "", crop_fallback))
        for index, wire in enumerate(merged.get("wires") or []):
            items.append(_item("wire", f"{page_id}-wire-{index}", page_id, wire, wire.get("number") or "", crop_fallback))
        for index, xref in enumerate(merged.get("xrefs") or []):
            items.append(_item("xref", f"{page_id}-xref-{index}", page_id, xref, xref.get("text") or xref.get("target") or "", crop_fallback))
        for index, text in enumerate(merged.get("texts") or []):
            if text.get("role") == "vision_fallback" or text.get("vision_fallback"):
                items.append(
                    _item(
                        "note",
                        f"{page_id}-vision-{index}",
                        page_id,
                        text,
                        str(text.get("text") or ""),
                        "",
                    )
                )
                continue
            if text.get("unreadable"):
                items.append(_item("text", f"{page_id}-text-{index}", page_id, text, "", crop_fallback))
    for index, row in enumerate(parts):
        items.append(
            _item(
                "part",
                f"part-{index}",
                "",
                row,
                row.get("catalog") or row.get("tag") or "",
                str(row.get("crop") or ""),
            )
        )
    for photo in photos:
        for index, device in enumerate(photo.get("devices") or []):
            items.append(
                _item(
                    "photo",
                    f"{photo.get('photo') or 'photo'}-dev-{index}",
                    "",
                    device,
                    device.get("tag") or "",
                    str(photo.get("image") or ""),
                )
            )
    return items


def apply_decision(items: list[dict[str, Any]], item_id: str, action: str, text: str | None = None) -> dict[str, Any]:
    action = action.strip().lower()
    if action not in {"accept", "accepted", "fix", "fixed", "reject", "rejected"}:
        raise ValueError("action must be accept, fix, or reject")
    status = {"accept": "accepted", "accepted": "accepted", "fix": "fixed", "fixed": "fixed", "reject": "rejected", "rejected": "rejected"}[action]
    for item in items:
        if item["id"] != item_id:
            continue
        item["status"] = status
        if status == "fixed":
            if text is None:
                raise ValueError("fix needs the replacement text")
            item["text"] = text
            payload = item.setdefault("payload", {})
            if item["kind"] in {"device", "photo"}:
                payload["tag"] = text
                payload["unreadable"] = False
            elif item["kind"] == "wire":
                payload["number"] = text
                payload["unreadable"] = False
            elif item["kind"] == "part":
                payload["catalog"] = text
                payload["unreadable"] = False
            else:
                payload["text"] = text
                payload["unreadable"] = False
        return item
    raise KeyError(item_id)


def pages_with_decisions(pages: list[dict[str, Any]], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy pages so a rejected or corrected review item is what the build sees."""

    by_page: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item.get("kind") not in {"device", "wire", "xref", "text"}:
            continue
        by_page.setdefault(str(item.get("page_id") or ""), []).append(item)
    out = []
    for page in pages:
        copy = dict(page)
        grouped = by_page.get(page["id"])
        if not grouped:
            out.append(copy)
            continue
        merged = {"devices": [], "wires": [], "xrefs": [], "texts": []}
        key = {"device": "devices", "wire": "wires", "xref": "xrefs", "text": "texts"}
        for item in grouped:
            if item.get("status") == "rejected":
                continue
            payload = dict(item.get("payload") or {})
            if item.get("status") == "fixed":
                if item["kind"] == "device":
                    payload["tag"] = item.get("text") or payload.get("tag")
                elif item["kind"] == "wire":
                    payload["number"] = item.get("text") or payload.get("number")
            payload["crop"] = item.get("crop") or payload.get("crop") or ""
            merged[key[item["kind"]]].append(payload)
        copy["merged"] = merged
        out.append(copy)
    return out


def _item(kind: str, item_id: str, page_id: str, payload: dict[str, Any], text: str, crop_fallback: str) -> dict[str, Any]:
    unreadable = bool(payload.get("unreadable"))
    return {
        "id": item_id,
        "kind": kind,
        "text": "" if unreadable else text,
        "confidence": 0.0 if unreadable else float(payload.get("confidence") or 0.0),
        "status": "pending",
        "crop": str(payload.get("crop") or crop_fallback),
        "page_id": page_id,
        "tile_id": str(payload.get("tile_id") or ""),
        "unreadable": unreadable,
        "source": str(payload.get("source") or ""),
        "model_assisted": bool(payload.get("model_assisted")),
        "vision_fallback": str(payload.get("vision_fallback") or ""),
        "crop_box": list(payload["crop_box"]) if payload.get("crop_box") else None,
        "crop_span": list(payload["crop_span"]) if payload.get("crop_span") else None,
        "payload": payload,
    }


def stamp_review_crop(item: dict[str, Any], tile: dict[str, Any], crop: str = "") -> None:
    """Point a row at its tile image and the millimetre box to clip, when it has one."""

    item["tile_id"] = str(tile.get("id") or item.get("tile_id") or "")
    if tile.get("page_id"):
        item["page_id"] = str(tile["page_id"])
    if crop:
        item["crop"] = str(crop)
    span_w = float(tile.get("w") or 0)
    span_h = float(tile.get("h") or 0)
    if span_w > 0 and span_h > 0 and not item.get("crop_span"):
        item["crop_span"] = [round(span_w, 2), round(span_h, 2)]
    if item.get("crop_box"):
        return
    source = str(item.get("source") or "")
    if source not in {"ocr", "symbol", "model"} and not item.get("model_assisted"):
        return
    box = _review_box(item, span_w, span_h, float(tile.get("x") or 0), float(tile.get("y") or 0))
    if box:
        item["crop_box"] = box


def _review_box(
    item: dict[str, Any],
    span_w: float,
    span_h: float,
    origin_x: float,
    origin_y: float,
) -> list[float] | None:
    """Tile-relative millimetres. A wire number is a point; a device origin is the top-left."""

    if item.get("x") is None or item.get("y") is None or span_w <= 0 or span_h <= 0:
        return None
    x = float(item["x"]) - origin_x
    y = float(item["y"]) - origin_y
    width = float(item.get("w") or 0)
    height = float(item.get("h") or 0)
    point = bool(str(item.get("number") or "")) and not str(item.get("tag") or "")
    if point:
        width = width or 16.0
        height = height or 8.0
        x -= width / 2.0
        y -= height / 2.0
    else:
        width = width or 32.0
        height = height or 24.0
    x0 = max(0.0, x)
    y0 = max(0.0, y)
    x1 = min(span_w, x + width)
    y1 = min(span_h, y + height)
    if x1 - x0 < 0.8 or y1 - y0 < 0.8:
        return None
    return [round(x0, 2), round(y0, 2), round(x1 - x0, 2), round(y1 - y0, 2)]
