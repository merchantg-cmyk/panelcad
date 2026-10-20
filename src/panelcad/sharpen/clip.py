"""Shrink agent replies so a 4B context stays usable."""

from __future__ import annotations

import json
from typing import Any

MAX_CHARS = 800


def clip_reply(
    reply: dict[str, Any],
    *,
    max_chars: int = MAX_CHARS,
    allowed: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Copy a reply and drop bulky inspect fields."""

    clipped = json.loads(json.dumps(reply, default=str))
    result = clipped.get("result")
    if isinstance(result, dict):
        clipped["result"] = _clip_result(result)
        if allowed is not None:
            nxt = clipped["result"].get("next")
            if nxt and str(nxt) not in set(allowed) | {"done"}:
                clipped["result"].pop("next", None)
                clipped["result"].pop("style_needed", None)
    text = json.dumps(clipped, default=str)
    if len(text) <= max_chars:
        return clipped
    clipped["_clipped"] = True
    clipped["_chars"] = len(text)
    if isinstance(clipped.get("result"), dict):
        clipped["result"] = _hard_trim(clipped["result"], max_chars - 80)
    text = json.dumps(clipped, default=str)
    if len(text) > max_chars:
        clipped["result"] = {"_truncated": text[: max_chars - 60] + "…"}
    return clipped


def _clip_result(result: dict[str, Any]) -> dict[str, Any]:
    out = dict(result)
    sheets = out.get("sheets")
    if isinstance(sheets, list):
        out["sheets"] = [_clip_sheet(s) if isinstance(s, dict) else s for s in sheets]
    comps = out.get("components")
    if isinstance(comps, list):
        out["components"] = [_clip_component(c) if isinstance(c, dict) else c for c in comps]
    devices = out.get("devices")
    if isinstance(devices, list) and len(devices) > 12:
        out["devices"] = devices[:12]
        out["device_count"] = len(devices)
    pins = out.get("pins")
    if isinstance(pins, list):
        out["pins"] = [_clip_pin(p) if isinstance(p, dict) else p for p in pins]
    return out


def _clip_sheet(sheet: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "id": sheet.get("id"),
        "title": sheet.get("title"),
        "kind": sheet.get("kind"),
        "index": sheet.get("index"),
        "count": sheet.get("count"),
        "active": sheet.get("active"),
        "closed": sheet.get("closed"),
    }
    comps = sheet.get("components")
    if isinstance(comps, list):
        keep["components"] = [_clip_component(c) if isinstance(c, dict) else c for c in comps]
        keep["component_count"] = len(comps)
    wires = sheet.get("wires")
    if isinstance(wires, list):
        keep["wire_count"] = len(wires)
        keep["wires"] = [
            {"id": w.get("id"), "from": w.get("from"), "to": w.get("to"), "net": w.get("net")}
            if isinstance(w, dict)
            else w
            for w in wires[:20]
        ]
    return {k: v for k, v in keep.items() if v is not None}


def _clip_component(comp: dict[str, Any]) -> dict[str, Any]:
    pins = comp.get("pins")
    pin_ids: list[str] = []
    if isinstance(pins, dict):
        pin_ids = list(pins.keys())
    elif isinstance(pins, list):
        pin_ids = [p["id"] if isinstance(p, dict) else str(p) for p in pins]
    return {
        "id": comp.get("id"),
        "library_id": comp.get("library_id"),
        "x": comp.get("x"),
        "y": comp.get("y"),
        "pins": pin_ids[:16],
    }


def _clip_pin(pin: dict[str, Any]) -> dict[str, Any]:
    return {"id": pin.get("id"), "side": pin.get("side"), "label": pin.get("label")}


def _hard_trim(result: dict[str, Any], budget: int) -> dict[str, Any]:
    keys = ("name", "active", "active_sheet", "sheet_count", "errors", "ok", "sheets", "id")
    slim = {k: result[k] for k in keys if k in result}
    if "sheets" in slim and isinstance(slim["sheets"], list):
        slim["sheets"] = [
            {"id": s.get("id"), "title": s.get("title"), "kind": s.get("kind")}
            if isinstance(s, dict)
            else s
            for s in slim["sheets"]
        ]
    text = json.dumps(slim, default=str)
    if len(text) > budget:
        slim.pop("sheets", None)
        slim["_truncated"] = True
    return slim
