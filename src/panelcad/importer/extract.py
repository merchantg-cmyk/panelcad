"""Turn tokens, or one submitted crop, into devices, wires, and cross-references."""

from __future__ import annotations

import re
from typing import Any

from panelcad.importer.read import label_tokens

XREF_RE = re.compile(r"^(TO|FROM)\b", re.I)


def facts_from_tokens(
    tokens: list[dict[str, Any]],
    width_mm: float,
    height_mm: float,
    catalog: Any = None,
) -> dict[str, Any]:
    labeled = label_tokens(tokens, width_mm, height_mm, catalog=catalog)
    devices = [
        {
            "tag": tok["text"],
            "type": "",
            "symbol": "",
            "pins": [],
            "x": round(float(tok["x"]), 2),
            "y": round(float(tok["y"]), 2),
            "w": round(float(tok.get("w") or 0), 2),
            "h": round(float(tok.get("h") or 0), 2),
            "confidence": float(tok.get("confidence") or 0.9),
            "unreadable": False,
            "guess": True,
            "library_id": "",
        }
        for tok in labeled["tags"]
    ]
    wires = [
        {
            "number": tok["text"],
            "x": round(float(tok["x"]), 2),
            "y": round(float(tok["y"]), 2),
            "confidence": float(tok.get("confidence") or 0.9),
            "unreadable": False,
        }
        for tok in labeled["wires"]
    ]
    texts = []
    for tok in tokens:
        if not tok.get("unreadable"):
            continue
        texts.append(
            {
                "text": "",
                "x": round(float(tok.get("x") or 0), 2),
                "y": round(float(tok.get("y") or 0), 2),
                "w": round(float(tok.get("w") or 0), 2),
                "h": round(float(tok.get("h") or 0), 2),
                "unreadable": True,
                "confidence": 0.0,
                "reason": str(tok.get("reason") or "needs_info"),
                "detail": str(tok.get("detail") or ""),
            }
        )
    return {"devices": devices, "wires": wires, "xrefs": _xrefs(tokens), "texts": texts}


def slice_facts(facts: dict[str, Any], tile: dict[str, Any]) -> dict[str, Any]:
    return {
        "devices": [item for item in facts.get("devices") or [] if _inside(item, tile)],
        "wires": [item for item in facts.get("wires") or [] if _inside(item, tile)],
        "xrefs": [item for item in facts.get("xrefs") or [] if _inside(item, tile) or "x" not in item],
        "texts": [item for item in facts.get("texts") or [] if _inside(item, tile)],
    }


def normalize_submission(data: dict[str, Any], tile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Accept one crop's JSON. Unreadable text stays empty. Tile-local coordinates are lifted."""

    devices = [_clean_device(item) for item in data.get("devices") or [] if isinstance(item, dict)]
    wires = [_clean_wire(item) for item in data.get("wires") or [] if isinstance(item, dict)]
    xrefs = [_clean_xref(item) for item in data.get("xrefs") or [] if isinstance(item, dict)]
    texts = [_clean_text(item) for item in data.get("texts") or [] if isinstance(item, dict)]
    payload = {"devices": devices, "wires": wires, "xrefs": xrefs, "texts": texts}
    if tile is not None:
        payload = _lift_tile_local(payload, tile)
    return payload


def _unreadable_field_names(value: Any) -> list[str]:
    """A bool, one name, or a list of names. ``True`` blanks every field.

    A model that returns ``true`` or ``false`` must not be iterated. A string
    is that one field. Anything else is ignored.
    """

    if value is True:
        return ["*"]
    if value is False or value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple)):
        names: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                names.append(item.strip())
        return names
    return []


def normalize_classification(data: dict[str, Any]) -> dict[str, Any]:
    from panelcad.importer.models import PAGE_KINDS

    kind = str(data.get("kind") or "other").strip()
    if kind not in PAGE_KINDS:
        kind = "other"
    unreadable = _unreadable_field_names(data.get("unreadable_fields"))
    blank = "*" in unreadable

    def field(name: str) -> str:
        if blank or name in unreadable or data.get(name) in (None, ""):
            return ""
        return str(data.get(name) or "").strip()

    return {
        "kind": "other" if blank or "kind" in unreadable else kind,
        "title": field("title"),
        "project": field("project"),
        "description": field("description"),
        "company": field("company"),
        "address": field("address"),
        "customer": field("customer"),
        "location": field("location"),
        "drawn_by": field("drawn_by"),
        "scale": field("scale"),
        "date": field("date"),
        "rev_description": field("rev_description"),
        "drawing_number": field("drawing_number"),
        "rev": field("rev"),
        "sheet_number": field("sheet_number"),
        "sheet_count": field("sheet_count"),
        "standard": field("standard"),
        "confidence": float(data.get("confidence") or (0.0 if unreadable else 0.7)),
        "unreadable_fields": unreadable,
    }


def _inside(item: dict[str, Any], tile: dict[str, Any]) -> bool:
    if "x" not in item or item.get("x") is None:
        return False
    x0, y0 = float(tile["x"]), float(tile["y"])
    return x0 <= float(item["x"]) <= x0 + float(tile["w"]) and y0 <= float(item["y"]) <= y0 + float(tile["h"])


def _lift_tile_local(payload: dict[str, Any], tile: dict[str, Any]) -> dict[str, Any]:
    points = [
        item
        for key in ("devices", "wires", "texts")
        for item in payload.get(key) or []
        if item.get("x") is not None
    ]
    if not points:
        return payload
    origin_x = float(tile["x"])
    origin_y = float(tile["y"])
    if origin_x <= 5 and origin_y <= 5:
        return payload
    width = float(tile["w"])
    height = float(tile["h"])
    # Crop-local numbers sit in the tile's own box and at least one of them is
    # left or above the tile on the page. A point already inside the page window
    # is a page coordinate and must not be shifted again.
    in_local = all(-1.0 <= float(item["x"]) <= width + 2.0 and -1.0 <= float(item["y"]) <= height + 2.0 for item in points)
    left_of_tile = any(
        (origin_x > 5 and float(item["x"]) < origin_x - 1.0) or (origin_y > 5 and float(item["y"]) < origin_y - 1.0)
        for item in points
    )
    if in_local and left_of_tile:
        for item in points:
            item["x"] = round(float(item["x"]) + origin_x, 2)
            item["y"] = round(float(item["y"]) + origin_y, 2)
    return payload


def _clean_device(item: dict[str, Any]) -> dict[str, Any]:
    from panelcad.importer.symbols import PREFIX_CONFIDENCE

    unreadable = bool(item.get("unreadable"))
    tag = "" if unreadable else str(item.get("tag") or "").strip()
    if not tag:
        unreadable = True
    type_text = "" if unreadable else str(item.get("type") or "").strip()
    symbol = "" if unreadable else str(item.get("symbol") or type_text).strip()
    library_id = "" if unreadable else str(item.get("library_id") or "").strip()
    guess = bool(item.get("guess"))
    confidence = 0.0 if unreadable else _conf(item.get("confidence"), 0.65)
    # No drawn class and no library row: the tag letters are not a symbol.
    if not unreadable and not library_id and not type_text and not str(item.get("symbol") or "").strip():
        guess = True
        confidence = min(confidence, PREFIX_CONFIDENCE)
    return {
        "tag": tag,
        "type": type_text,
        "symbol": symbol,
        "pins": [] if unreadable else [str(pin) for pin in item.get("pins") or [] if str(pin).strip()],
        "x": _num(item.get("x")),
        "y": _num(item.get("y")),
        "confidence": confidence,
        "unreadable": unreadable,
        "library_id": library_id,
        "guess": guess,
        "pin_frame": "" if unreadable else str(item.get("pin_frame") or ""),
        "scale": 1.0 if unreadable else float(item.get("scale") or 1.0),
        "source": _source(item),
        "model_assisted": bool(item.get("model_assisted")),
    }


def _clean_wire(item: dict[str, Any]) -> dict[str, Any]:
    unreadable = bool(item.get("unreadable"))
    number = "" if unreadable else str(item.get("number") or "").strip()
    from_pin = "" if unreadable else str(item.get("from_pin") or "").strip()
    to_pin = "" if unreadable else str(item.get("to_pin") or "").strip()
    from_tag = "" if unreadable else str(item.get("from_tag") or "").strip()
    to_tag = "" if unreadable else str(item.get("to_tag") or "").strip()
    # A jumper that already names both pins is a connection even with no printed number.
    pinned = bool(from_pin and to_pin and (from_tag or to_tag))
    if not number and not pinned:
        unreadable = True
        from_pin = ""
        to_pin = ""
        from_tag = ""
        to_tag = ""
    # A vision stub or an attached number is kept even when the pins are still open.
    held = (bool(item.get("stub")) or bool(item.get("attached"))) and not bool(item.get("ambiguous"))
    ambiguous = False if held else (bool(item.get("ambiguous")) or not (from_pin and to_pin))
    confidence = 0.0 if unreadable else _conf(item.get("confidence"), 0.65)
    if ambiguous and not unreadable:
        # An end that could be two pins is not assigned. The review shows it as unsure.
        from_pin = ""
        to_pin = ""
        confidence = min(confidence, 0.45)
    stub = bool(item.get("stub")) and not ambiguous
    attached = bool(item.get("attached")) and not ambiguous
    cleaned: dict[str, Any] = {
        "number": number,
        "x": _num(item.get("x")),
        "y": _num(item.get("y")),
        "from_tag": "" if ambiguous else from_tag,
        "to_tag": "" if ambiguous else to_tag,
        "from_pin": from_pin,
        "to_pin": to_pin,
        "confidence": confidence,
        "unreadable": unreadable,
        "ambiguous": ambiguous,
        "stub": stub,
        "attached": attached,
        "needs_review": bool(item.get("needs_review")) or stub or (attached and not (from_pin and to_pin)),
        "source": _source(item),
        "model_assisted": bool(item.get("model_assisted")),
    }
    for key in ("crop_box", "crop_span", "tile_x", "tile_y", "tile_w", "tile_h"):
        if item.get(key) not in (None, "", []):
            cleaned[key] = item[key]
    return cleaned


def _clean_xref(item: dict[str, Any]) -> dict[str, Any]:
    unreadable = bool(item.get("unreadable"))
    text = "" if unreadable else str(item.get("text") or "").strip()
    return {
        "kind": str(item.get("kind") or "offpage"),
        "text": text,
        "tag": "" if unreadable else str(item.get("tag") or "").strip(),
        "target": "" if unreadable else str(item.get("target") or "").strip(),
        "x": _num(item.get("x")),
        "y": _num(item.get("y")),
        "confidence": 0.0 if unreadable else _conf(item.get("confidence"), 0.6),
        "unreadable": unreadable or not (text or item.get("target")),
    }


def _clean_text(item: dict[str, Any]) -> dict[str, Any]:
    unreadable = bool(item.get("unreadable"))
    text = "" if unreadable else str(item.get("text") or "").strip()
    return {
        "text": text,
        "role": str(item.get("role") or ""),
        "x": _num(item.get("x")),
        "y": _num(item.get("y")),
        "unreadable": unreadable or not text,
        "confidence": 0.0 if unreadable or not text else _conf(item.get("confidence"), 0.6),
    }


def _xrefs(tokens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = []
    for tok in tokens:
        if not XREF_RE.match(tok["text"]):
            continue
        found.append(
            {
                "kind": "offpage",
                "text": tok["text"],
                "tag": "",
                "target": "",
                "x": float(tok["x"]),
                "y": float(tok["y"]),
                "confidence": 0.7,
                "unreadable": False,
            }
        )
    return found


def _source(item: dict[str, Any]) -> str:
    token = str(item.get("source") or "").strip().lower()
    if token in {"vector", "ocr", "symbol", "model"}:
        return token
    return ""


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _conf(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))
