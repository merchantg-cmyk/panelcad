"""Turn messy weak-model text into one PanelCAD agent op."""

from __future__ import annotations

import json
import re
from typing import Any

from panelcad.cli.agent import OPS
from panelcad.sharpen.cards import STARTER_OPS

HARNESS_OPS = {"done"}
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

_OP_ALIASES = {
    "cmd": "op",
    "command": "op",
    "operation": "op",
    "action": "op",
}
_FIELD_ALIASES = {
    "lib": "library_id",
    "library": "library_id",
    "device": "library_id",
    "sheet_id": "sheet",
    "page": "sheet",
    "from_pin": "from",
    "to_pin": "to",
    "file": "path",
    "filepath": "path",
}


class ParseError(ValueError):
    """The model did not emit a usable JSON op."""


def parse_op(text: str, *, allowed: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Extract one JSON object, apply aliases, require a known op."""

    raw = (text or "").strip()
    if not raw:
        raise ParseError("empty reply — send one JSON object, e.g. {\"op\":\"list_sheets\"}")
    if _looks_like_python(raw):
        raise ParseError(
            "do not write Python. Send a JSON op, e.g. "
            '{"op":"place","library_id":"iec.terminals.x_2p5","ref":"X20","x":50,"y":180,"sheet":"io"}'
        )
    if _looks_like_shell(raw) and "{" not in raw:
        raise ParseError(
            "do not type a shell command. PanelCAD agent wants JSON. "
            'Example: {"op":"open","path":"<job>"} or {"op":"list_sheets"}'
        )
    obj = _first_object(raw)
    for nest in ("args", "arguments", "params", "parameters"):
        extra = obj.get(nest)
        if isinstance(extra, dict):
            obj.pop(nest, None)
            for key, value in extra.items():
                obj.setdefault(key, value)
    for src, dest in _OP_ALIASES.items():
        if dest not in obj and src in obj:
            obj[dest] = obj.pop(src)
    op_guess = str(obj.get("op") or "").strip()
    for src, dest in _FIELD_ALIASES.items():
        if dest not in obj and src in obj:
            # add_sheet's sheet_id is the new page id, not "which sheet to edit"
            if src == "sheet_id" and dest == "sheet" and op_guess == "add_sheet":
                continue
            obj[dest] = obj.pop(src)
    if op_guess == "add_sheet" and "sheet_id" not in obj and obj.get("sheet"):
        obj["sheet_id"] = obj.pop("sheet")
    op = obj.get("op")
    if not isinstance(op, str) or not op.strip():
        raise ParseError('JSON needs "op". Example: {"op":"list_sheets"}')
    op = op.strip()
    obj["op"] = op
    allowed_set = set(allowed or STARTER_OPS) | HARNESS_OPS | OPS
    if op not in allowed_set:
        names = ", ".join(sorted(allowed or STARTER_OPS))
        raise ParseError(f"unknown op '{op}'. Allowed: {names}, done")
    if allowed is not None and op not in set(allowed) | HARNESS_OPS:
        names = ", ".join(allowed)
        raise ParseError(f"op '{op}' is not in this task's allow-list: {names}")
    return obj


def _first_object(text: str) -> dict[str, Any]:
    fenced = _FENCE.search(text)
    blob = fenced.group(1) if fenced else text
    match = _OBJECT.search(blob)
    if not match:
        raise ParseError(
            "no JSON object found. Reply with only: {\"op\":\"list_sheets\"}"
        )
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ParseError(f"invalid JSON ({exc.msg}). Send one object, no trailing text.") from exc
    if not isinstance(data, dict):
        raise ParseError("JSON must be an object, not a list")
    return data


def _looks_like_python(text: str) -> bool:
    head = text.lstrip()
    return (
        head.startswith("def ")
        or head.startswith("from ")
        or head.startswith("import ")
        or "tool_code" in head[:80]
        or "Session(" in head
    )


def _looks_like_shell(text: str) -> bool:
    head = text.lstrip()
    return head.startswith("panelcad ") or head.startswith("python ")
