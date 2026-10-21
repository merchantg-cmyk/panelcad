"""Compact undo snapshots.

A step stores a full project only every few edits. The steps between store the
fields that actually changed. Restoring replays those steps onto the last full
copy. The drawing that comes back is the same dict a full copy would have kept.
"""

from __future__ import annotations

from typing import Any

# A full copy this often caps how far a restore has to replay.
FULL_EVERY = 16

_NO_CHANGE = object()


def diff_value(old: Any, new: Any) -> Any:
    """Patch that turns ``old`` into ``new``. ``None`` when they already match."""

    if old == new:
        return None
    if isinstance(old, dict) and isinstance(new, dict):
        patch: dict[Any, Any] = {}
        for key in old.keys() - new.keys():
            patch[key] = ("del",)
        for key, value in new.items():
            if key not in old:
                patch[key] = ("set", value)
                continue
            child = diff_value(old[key], value)
            if child is not None:
                patch[key] = ("patch", child)
        if not patch:
            return None
        return ("dict", patch)
    if isinstance(old, list) and isinstance(new, list):
        listed = _diff_list(old, new)
        if listed is not _NO_CHANGE:
            return listed
    return ("set", new)


def apply_value(old: Any, patch: Any) -> Any:
    """Copy ``old`` with ``patch`` applied. Unchanged nested values are shared."""

    if not isinstance(patch, tuple) or not patch:
        return old
    kind = patch[0]
    if kind == "set":
        return patch[1]
    if kind == "dict":
        base = dict(old) if isinstance(old, dict) else {}
        for key, child in patch[1].items():
            if child[0] == "del":
                base.pop(key, None)
            elif child[0] == "set":
                base[key] = child[1]
            else:
                base[key] = apply_value(base.get(key), child[1])
        return base
    if kind == "ids":
        return _apply_ids(old if isinstance(old, list) else [], patch[1])
    if kind == "idx":
        base = list(old) if isinstance(old, list) else []
        for index, child in patch[1]:
            if 0 <= index < len(base):
                base[index] = apply_value(base[index], child)
        return base
    return old


def diff_snap(old: dict[str, Any] | None, new: dict[str, Any]) -> Any:
    if old is None:
        return ("set", new)
    return diff_value(old, new)


def apply_snap(old: dict[str, Any], patch: Any) -> dict[str, Any]:
    applied = apply_value(old, patch)
    if isinstance(applied, dict):
        return applied
    return old


def _identities(items: list[Any]) -> dict[str, Any] | None:
    found: dict[str, Any] = {}
    for item in items:
        if not isinstance(item, dict):
            return None
        ident = item.get("id")
        if not isinstance(ident, str) or not ident or ident in found:
            return None
        found[ident] = item
    return found


def _diff_list(old: list[Any], new: list[Any]) -> Any:
    old_ids = _identities(old)
    new_ids = _identities(new)
    if old_ids is not None and new_ids is not None:
        ops: list[Any] = []
        for item in new:
            ident = item["id"]
            prev = old_ids.get(ident)
            if prev is None:
                ops.append(("add", ident, item))
                continue
            child = diff_value(prev, item)
            if child is None:
                ops.append(("keep", ident))
            else:
                ops.append(("patch", ident, child))
        return ("ids", ops)
    if len(old) == len(new):
        changes = []
        for index, (left, right) in enumerate(zip(old, new, strict=True)):
            child = diff_value(left, right)
            if child is not None:
                changes.append((index, child))
        if not changes:
            return None
        return ("idx", changes)
    return _NO_CHANGE


def _apply_ids(old: list[Any], ops: list[Any]) -> list[Any]:
    by_id = _identities(old) or {}
    out: list[Any] = []
    for op in ops:
        kind = op[0]
        if kind == "keep":
            if op[1] in by_id:
                out.append(by_id[op[1]])
        elif kind == "add":
            out.append(op[2])
        elif kind == "patch":
            base = by_id.get(op[1])
            out.append(apply_value(base, op[2]) if base is not None else op[2])
    return out
