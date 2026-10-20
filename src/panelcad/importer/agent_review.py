"""Cross-checks for an outside agent's print JSON.

The agent labels the sheet. These checks only use the extracted text and the
traced lines, so they stay the same for every print. A claim that fails is
needs-review. It is not rewritten into a different tag, contact, or part.
"""

from __future__ import annotations

import math
from typing import Any

# A wire number is "on" a conductor when its text centre is this close to a stroke.
NEAR_MM = 8.0
# A device touches that conductor when the stroke meets its bbox, plus this pad.
TOUCH_MM = 4.0
# Strokes this close belong to the same conductor.
JOIN_MM = 1.5


def covering_token(tag: str, tokens: list[dict[str, Any]]) -> str:
    """A longer extracted word that ends with ``tag`` and adds one or two letters.

    That is the usual shape of a tag whose first letter is covered by a symbol.
    The short reading is not renamed.
    """

    norm = _norm(tag)
    if len(norm) < 2:
        return ""
    for token in tokens:
        text = _norm(str(token.get("text") or ""))
        extra = len(text) - len(norm)
        if extra not in {1, 2} or not text.endswith(norm):
            continue
        prefix = text[:extra]
        if prefix.isalpha():
            return str(token.get("text") or "")
    return ""


def coil_and_contact(winner: dict[str, Any], other: dict[str, Any]) -> bool:
    """A coil and its contact are one device, not two competing readings."""

    symbols = (str(winner.get("symbol") or ""), str(other.get("symbol") or ""))
    return any(_is_coil_symbol(symbol) for symbol in symbols) and any(
        _is_contact_symbol(symbol) for symbol in symbols
    )


def review_devices(devices: list[dict[str, Any]], pages: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag clipped tags, a contact with no coil, and an NO/NC clash. Do not guess."""

    notes: list[dict[str, Any]] = []
    for device in devices:
        page = pages.get(str(device.get("_page") or ""))
        if page is None:
            continue
        tag = str(device.get("tag") or "")
        if device.get("_confirm_tag"):
            near = [str(value).strip() for value in device.get("_near") or [] if str(value).strip()]
            hint = f" OCR read {', '.join(near)}." if near else ""
            note = _device_note(device, page, f"PanelCAD's text did not read this tag.{hint}")
            note["candidates"] = [tag] if tag else []
            notes.append(note)
        else:
            longer = covering_token(tag, page.get("token_rows") or [])
            if longer and _norm(longer) != _norm(tag):
                notes.append(_device_note(device, page, f"tag may be clipped; extracted text has {longer}"))
        contact = str(device.get("contact") or "").upper()
        if device.get("_contact_review"):
            notes.append(_device_note(device, page, str(device["_contact_review"])))
            device["contact"] = ""
        if device.get("_symbol_review"):
            notes.append(_device_note(device, page, str(device["_symbol_review"])))
        elif _contact_like(device) and contact in {"NO", "NC"}:
            seen = _geometry_state(page.get("lines") or [], _item_box(device))
            if seen and seen != contact:
                reason = f"agent read {contact}; the symbol geometry reads {seen}. Left needs-info"
                device["_contact_review"] = reason
                device["contact"] = ""
                notes.append(_device_note(device, page, reason))
        family = _coil_family(devices, tag, ignore=device)
        if _is_contact_symbol(str(device.get("symbol") or "")) and not family:
            notes.append(_device_note(device, page, "contact tag has no matching coil"))
        elif family == "timer" and "timer" not in str(device.get("symbol") or "").casefold():
            notes.append(_device_note(device, page, "contact symbol does not match its timer coil"))
    return notes


def review_wire(
    wire: dict[str, Any],
    devices: dict[tuple[str, str], dict[str, Any]],
    page: dict[str, Any],
) -> str:
    """Empty when the wire number is real text on a conductor that touches both ends."""

    number = str(wire.get("number") or "").strip()
    if not number:
        return ""
    lines = [item for item in (page.get("lines") or []) if isinstance(item, dict)]
    if not lines:
        return "no traced conductor to check against the wire number"
    tokens = [
        token
        for token in page.get("token_rows") or []
        if _norm(str(token.get("text") or "")) == _norm(number)
    ]
    seeds: list[int] = []
    for token in tokens:
        try:
            px, py = float(token["x"]), float(token["y"])
        except (KeyError, TypeError, ValueError):
            continue
        for index, line in enumerate(lines):
            if _point_seg_dist(px, py, line) <= NEAR_MM:
                seeds.append(index)
    if not seeds:
        return "wire number is not near a traced conductor"
    traced = _trace(lines, seeds)
    for endpoint in (str(wire.get("from") or ""), str(wire.get("to") or "")):
        name, _pin = _split(endpoint)
        device = devices.get((str(wire.get("page") or ""), name.upper()))
        if device is None:
            return f"endpoint {endpoint} does not touch the conductor"
        box = _item_box(device)
        if not any(_segment_hits_box(lines[index], box, TOUCH_MM) for index in traced):
            return f"endpoint {endpoint} does not touch the conductor"
    return ""


def _device_note(device: dict[str, Any], page: dict[str, Any], reason: str) -> dict[str, Any]:
    tag = str(device.get("tag") or "")
    page_id = str(device.get("_page") or "")
    contact = "contact_state" if "needs-info" in reason or "NO/NC" in reason or "neither is used" in reason else "needs_review"
    if device.get("_contact_review") and reason == device.get("_contact_review"):
        contact = "contact_state"
    return {
        "review_id": f"device:{page_id}:{tag}:{_norm(reason)[:24]}",
        "kind": "device",
        "id": tag,
        "page": page_id,
        "sheet": page_id,
        "reason": reason,
        "check": contact,
        "bbox": device.get("bbox") if isinstance(device.get("bbox"), dict) else None,
        "image": str(page.get("image") or ""),
    }


def _coil_family(devices: list[dict[str, Any]], tag: str, *, ignore: dict[str, Any]) -> str:
    """``timer`` when a same-tag coil says so, ``coil`` for any other coil, else empty."""

    want = _norm(tag)
    found = ""
    for device in devices:
        if device is ignore or _norm(str(device.get("tag") or "")) != want:
            continue
        symbol = str(device.get("symbol") or "")
        if not _is_coil_symbol(symbol):
            continue
        if "timer" in symbol.casefold():
            return "timer"
        found = "coil"
    return found


def _is_contact_symbol(symbol: str) -> bool:
    text = symbol.casefold()
    return "contact" in text and "coil" not in text


def _is_coil_symbol(symbol: str) -> bool:
    text = symbol.casefold()
    if _is_contact_symbol(symbol):
        return False
    return any(word in text for word in ("coil", "relay", "contactor", "timer", "starter"))


def _contact_like(device: dict[str, Any]) -> bool:
    text = str(device.get("symbol") or "").casefold()
    return _is_contact_symbol(text) or any(word in text for word in ("push", "button", "selector"))


def _geometry_state(lines: list[dict[str, Any]], box: tuple[float, float, float, float]) -> str:
    """NC when a diagonal crosses the symbol. NO when the ink is only axis-aligned.

    Anything else is unknown. Unknown does not disagree with the agent.
    """

    _x, _y, width, height = box
    if width < 1 or height < 1 or width > 60 or height > 60:
        return ""
    diagonal = False
    axis = 0
    for line in lines:
        if not _segment_hits_box(line, box, 0.4):
            continue
        try:
            dx = abs(float(line["x1"]) - float(line["x0"]))
            dy = abs(float(line["y1"]) - float(line["y0"]))
        except (KeyError, TypeError, ValueError):
            continue
        length = math.hypot(dx, dy)
        if length < 1.5:
            continue
        if dx > 1.2 and dy > 1.2 and min(dx, dy) / max(dx, dy) > 0.35:
            diagonal = True
        elif min(dx, dy) < 1.0:
            axis += 1
    if diagonal:
        return "NC"
    if axis >= 2:
        return "NO"
    return ""


def _item_box(device: dict[str, Any]) -> tuple[float, float, float, float]:
    raw = device.get("bbox")
    if isinstance(raw, dict):
        try:
            return (float(raw["x"]), float(raw["y"]), float(raw["w"]), float(raw["h"]))
        except (KeyError, TypeError, ValueError):
            pass
    try:
        x, y = float(device.get("x") or 0), float(device.get("y") or 0)
    except (TypeError, ValueError):
        x, y = 0.0, 0.0
    return (x - 4.0, y - 4.0, 8.0, 8.0)


def _trace(lines: list[dict[str, Any]], seeds: list[int]) -> set[int]:
    seen = {index for index in seeds if 0 <= index < len(lines)}
    stack = list(seen)
    while stack:
        current = stack.pop()
        for index, other in enumerate(lines):
            if index in seen:
                continue
            if _seg_gap(lines[current], other) <= JOIN_MM:
                seen.add(index)
                stack.append(index)
    return seen


def _seg_gap(left: dict[str, Any], right: dict[str, Any]) -> float:
    return min(
        _point_seg_dist(float(left["x0"]), float(left["y0"]), right),
        _point_seg_dist(float(left["x1"]), float(left["y1"]), right),
        _point_seg_dist(float(right["x0"]), float(right["y0"]), left),
        _point_seg_dist(float(right["x1"]), float(right["y1"]), left),
    )


def _point_seg_dist(px: float, py: float, line: dict[str, Any]) -> float:
    x0, y0 = float(line["x0"]), float(line["y0"])
    x1, y1 = float(line["x1"]), float(line["y1"])
    dx, dy = x1 - x0, y1 - y0
    length = dx * dx + dy * dy
    if length <= 1e-9:
        return math.hypot(px - x0, py - y0)
    tee = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / length))
    return math.hypot(px - (x0 + tee * dx), py - (y0 + tee * dy))


def _segment_hits_box(line: dict[str, Any], box: tuple[float, float, float, float], pad: float) -> bool:
    try:
        x0, y0 = float(line["x0"]), float(line["y0"])
        x1, y1 = float(line["x1"]), float(line["y1"])
    except (KeyError, TypeError, ValueError):
        return False
    left, top, width, height = box
    min_x, min_y = left - pad, top - pad
    max_x, max_y = left + width + pad, top + height + pad

    def code(px: float, py: float) -> int:
        flags = 0
        if px < min_x:
            flags |= 1
        elif px > max_x:
            flags |= 2
        if py < min_y:
            flags |= 4
        elif py > max_y:
            flags |= 8
        return flags

    c0, c1 = code(x0, y0), code(x1, y1)
    for _ in range(8):
        if c0 == 0 and c1 == 0:
            return True
        if c0 & c1:
            return False
        side = c0 or c1
        if side & 8:
            px = x0 + (x1 - x0) * (max_y - y0) / ((y1 - y0) or 1e-12)
            py = max_y
        elif side & 4:
            px = x0 + (x1 - x0) * (min_y - y0) / ((y1 - y0) or 1e-12)
            py = min_y
        elif side & 2:
            py = y0 + (y1 - y0) * (max_x - x0) / ((x1 - x0) or 1e-12)
            px = max_x
        else:
            py = y0 + (y1 - y0) * (min_x - x0) / ((x1 - x0) or 1e-12)
            px = min_x
        if side == c0:
            x0, y0, c0 = px, py, code(px, py)
        else:
            x1, y1, c1 = px, py, code(px, py)
    return False


def _split(endpoint: str) -> tuple[str, str]:
    text = str(endpoint or "").strip()
    if "." not in text:
        return text, ""
    device, pin = text.split(".", 1)
    return device.strip(), pin.strip()


def _norm(text: str) -> str:
    return "".join(str(text or "").split()).upper()
