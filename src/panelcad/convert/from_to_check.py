"""Wire-by-wire (From-To) check for a standard conversion.

The From-To list is built before and after the conversion. Every end of the
converted list is mapped back through the tag map, the pin rewrites and the
rail renames. The nets (sets of ends joined by conductors) must be the same,
end for end. A lost, added or merged connection is a hard failure.

The same pass returns the wire-number and terminal mapping, one row per net,
so the review shows what each wire is called before and after.
"""

from __future__ import annotations

from typing import Any

from panelcad.core.models import Project

SCORER_VERSION = "from-to-check/1"


class ConnectionLost(ValueError):
    """The converted drawing does not carry every From-To connection."""

    def __init__(self, report: dict[str, Any], *, source_project: dict[str, Any] | None = None) -> None:
        self.report = report
        self.source_project = source_project
        lost = len(report.get("missing") or [])
        added = len(report.get("extra") or [])
        super().__init__(
            f"conversion changed the wiring: {lost} connection(s) lost, {added} added. "
            "The converted copy was not saved."
        )


class RevertRefused(ConnectionLost):
    """Reverting one chunk would break the wiring; the revert was not done."""

    def __init__(self, report: dict[str, Any], *, chunk_id: str) -> None:
        super().__init__(report, source_project=None)
        self.chunk_id = chunk_id
        lost = len(report.get("missing") or [])
        added = len(report.get("extra") or [])
        self.args = (
            f"revert of {chunk_id} refused: it would change the wiring "
            f"({lost} connection(s) lost, {added} added). The conversion is unchanged.",
        )


def _invert(mapping: dict[str, str]) -> dict[str, str]:
    return {str(new): str(old) for old, new in mapping.items() if str(old) != str(new)}


def _mappers(
    tag_map: dict[str, str],
    pin_rewrites: list[dict[str, str]],
    rail_map: dict[str, dict[str, str]],
) -> tuple[Any, Any]:
    tag_back = _invert(tag_map)
    pin_back: dict[tuple[str, str], str] = {}
    for row in pin_rewrites:
        pin_back[(str(row.get("ref") or ""), str(row.get("pin") or ""))] = str(row.get("was") or "")
    rail_back: dict[str, dict[str, str]] = {}
    for sheet_id, pair in (rail_map or {}).items():
        back: dict[str, str] = {}
        for to_key, from_key in (("to_hot", "from_hot"), ("to_ret", "from_ret")):
            new = str(pair.get(to_key) or "")
            old = str(pair.get(from_key) or "")
            if new and old:
                back[new] = old
        rail_back[str(sheet_id)] = back

    def end_back(sheet_id: str, kind: str, raw: str) -> str:
        if kind == "rail":
            name = (rail_back.get(sheet_id) or {}).get(raw, raw)
            return f"rail:{sheet_id}:{name}"
        if "." not in raw:
            return tag_back.get(raw, raw)
        ref, pin = raw.rsplit(".", 1)
        return f"{tag_back.get(ref, ref)}.{pin_back.get((ref, pin), pin)}"

    def number_back(sheet_id: str, number: str) -> str:
        return (rail_back.get(sheet_id) or {}).get(number, number)

    return end_back, number_back


def _identity(sheet_id: str, kind: str, raw: str) -> str:
    if kind == "rail":
        return f"rail:{sheet_id}:{raw}"
    return raw


def _nets(
    project: Project, catalog: Any, end_key: Any, number_key: Any
) -> tuple[dict[frozenset, set[str]], list[tuple[str, str, str]]]:
    from panelcad.core.from_to import conductor_records

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pairs: list[tuple[str, str, str]] = []
    numbers: dict[str, set[str]] = {}
    for row in conductor_records(project, catalog):
        a = end_key(row.frm.sheet_id, row.frm.kind, row.frm.raw)
        b = end_key(row.to.sheet_id, row.to.kind, row.to.raw)
        parent[find(a)] = find(b)
        left, right = sorted((a, b))
        pairs.append((left, right, str(row.number or "")))
        for end in (a, b):
            numbers.setdefault(end, set())
            if row.number:
                numbers[end].add(number_key(row.frm.sheet_id, str(row.number)))
    groups: dict[str, set[str]] = {}
    for end in list(parent):
        groups.setdefault(find(end), set()).add(end)
    nets: dict[frozenset, set[str]] = {}
    for members in groups.values():
        found: set[str] = set()
        for end in members:
            found |= numbers.get(end, set())
        nets[frozenset(members)] = found
    pairs.sort()
    return nets, pairs


def check_connections(
    before: Project,
    after: Project,
    catalog: Any,
    *,
    tag_map: dict[str, str],
    pin_rewrites: list[dict[str, str]],
    rail_map: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Compare the From-To nets. ``ok`` is False when any connection is lost or added."""

    end_back, number_back = _mappers(tag_map, pin_rewrites, rail_map)
    old_nets, old_pairs = _nets(before, catalog, _identity, lambda _s, n: n)
    new_raw, _ = _nets(after, catalog, _identity, lambda _s, n: n)
    new_nets, new_pairs = _nets(after, catalog, end_back, number_back)

    missing = sorted(sorted(net) for net in old_nets if net not in new_nets)
    extra = sorted(sorted(net) for net in new_nets if net not in old_nets)

    # Wire-number / terminal mapping, one row per surviving net.
    forward_tag = {str(k): str(v) for k, v in tag_map.items()}
    number_rows: list[dict[str, Any]] = []
    new_by_old_net: dict[frozenset, set[str]] = {}
    for raw_net, raw_numbers in new_raw.items():
        mapped = frozenset(end_back(*_split_identity(end)) for end in raw_net)
        new_by_old_net[mapped] = raw_numbers
    for net, old_numbers in sorted(old_nets.items(), key=lambda item: sorted(item[0])):
        if net not in new_by_old_net:
            continue
        new_numbers = new_by_old_net[net]
        if old_numbers or new_numbers:
            number_rows.append(
                {
                    "ends": sorted(net),
                    "was": sorted(old_numbers),
                    "now": sorted(new_numbers),
                    "changed": sorted(old_numbers) != sorted(new_numbers),
                }
            )
    old_set = {(a, b) for a, b, _n in old_pairs}
    new_set = {(a, b) for a, b, _n in new_pairs}
    repaired = sorted(new_set - old_set)
    return {
        "scorer": SCORER_VERSION,
        "ok": not missing and not extra,
        "nets_before": len(old_nets),
        "nets_after": len(new_nets),
        "conductors_before": len(old_pairs),
        "conductors_after": len(new_pairs),
        "missing": missing,
        "extra": extra,
        "repaired": [list(pair) for pair in repaired],
        "wire_numbers": number_rows,
        "tag_map": forward_tag,
    }


def _split_identity(end: str) -> tuple[str, str, str]:
    if end.startswith("rail:"):
        _tag, sheet_id, name = end.split(":", 2)
        return sheet_id, "rail", name
    return "", "device", end
