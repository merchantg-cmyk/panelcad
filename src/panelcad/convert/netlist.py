"""Pin-to-pin netlist.

Ordinary wire numbers stay put. A number that is only the control-rail name
(24V, L+, 0V, M) follows that rail and is mapped back when the netlists are
compared.
"""

from __future__ import annotations

from typing import Any

from panelcad.core.models import Project


def _end(text: str) -> str:
    return str(text or "").strip()


def sheet_edges(project: Project) -> list[tuple[str, str, str, str]]:
    """Sorted (sheet, end_a, end_b, wire_number). Ends are ordered so direction does not matter."""

    rows: list[tuple[str, str, str, str]] = []
    for sheet in project.sheets:
        for wire in sheet.wires:
            a, b = _end(wire.frm), _end(wire.to)
            if a > b:
                a, b = b, a
            rows.append((sheet.id, a, b, str(wire.number or "")))
    rows.sort()
    return rows


def _invert_map(mapping: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for old, new in mapping.items():
        if old != new:
            out[str(new)] = str(old)
    return out


def normalize_edges(
    edges: list[tuple[str, str, str, str]],
    *,
    tag_map: dict[str, str],
    pin_rewrites: list[dict[str, str]],
    rail_map: dict[str, dict[str, str]],
) -> list[tuple[str, str, str, str]]:
    """Map a converted netlist back onto the pre-conversion tags, pins, and rail names."""

    tag_back = _invert_map(tag_map)
    pin_back: dict[tuple[str, str], str] = {}
    for row in pin_rewrites:
        pin_back[(str(row["ref"]), str(row["pin"]))] = str(row["was"])
    rail_back: dict[str, dict[str, str]] = {}
    for sheet_id, pair in rail_map.items():
        rail_back[str(sheet_id)] = {
            str(pair.get("to_hot") or ""): str(pair.get("from_hot") or ""),
            str(pair.get("to_ret") or ""): str(pair.get("from_ret") or ""),
        }

    def _token(sheet_id: str, end: str) -> str:
        rails = rail_back.get(sheet_id) or {}
        if end in rails and rails[end]:
            return rails[end]
        if "." not in end:
            return end
        ref, pin = end.split(".", 1)
        old_ref = tag_back.get(ref, ref)
        old_pin = pin_back.get((ref, pin), pin)
        return f"{old_ref}.{old_pin}"

    def _number(sheet_id: str, number: str) -> str:
        rails = rail_back.get(sheet_id) or {}
        mapped = rails.get(number)
        if mapped:
            return mapped
        return number

    rows: list[tuple[str, str, str, str]] = []
    for sheet_id, a, b, number in edges:
        left, right = _token(sheet_id, a), _token(sheet_id, b)
        if left > right:
            left, right = right, left
        rows.append((sheet_id, left, right, _number(sheet_id, number)))
    rows.sort()
    return rows


def same_netlist(
    before: list[tuple[str, str, str, str]],
    after: list[tuple[str, str, str, str]],
    *,
    tag_map: dict[str, str],
    pin_rewrites: list[dict[str, str]],
    rail_map: dict[str, dict[str, str]],
) -> bool:
    return normalize_edges(after, tag_map=tag_map, pin_rewrites=pin_rewrites, rail_map=rail_map) == list(before)


def dump_rewrites(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
