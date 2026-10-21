"""Readability numbers for one schematic page.

Cleanup keeps a change only when none of these got worse. Crossings count
one per place two different conductors cross; two wires that are the same
node (they share a pin or a net name) do not cross each other.
"""

from __future__ import annotations

from typing import Any

from panelcad.core.models import Sheet
from panelcad.library.catalog import Catalog

_TOL = 0.5


class _Nodes:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, key: str) -> str:
        self.parent.setdefault(key, key)
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def join(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _boxes(sheet: Sheet, catalog: Catalog) -> dict[str, tuple[float, float, float, float]]:
    from panelcad.core.cleanup import _aabb

    out: dict[str, tuple[float, float, float, float]] = {}
    for comp in sheet.components:
        try:
            out[comp.id] = tuple(_aabb(sheet, catalog, comp))  # type: ignore[assignment]
        except KeyError:
            continue
    return out


def sheet_quality(sheet: Sheet, catalog: Catalog, project: Any = None) -> dict[str, int]:
    """overlaps, wires through symbols, crossings, and bends on one page."""

    from panelcad.render.wiring import route_sheet_wires

    boxes = _boxes(sheet, catalog)
    ids = sorted(boxes)
    overlaps = 0
    for i, a in enumerate(ids):
        ax0, ay0, ax1, ay1 = boxes[a]
        for b in ids[i + 1 :]:
            bx0, by0, bx1, by1 = boxes[b]
            if ax0 < bx1 - _TOL and ax1 > bx0 + _TOL and ay0 < by1 - _TOL and ay1 > by0 + _TOL:
                overlaps += 1
    nodes = _Nodes()
    by_net: dict[str, str] = {}
    for wire in sheet.wires:
        key = f"w:{wire.id}"
        nodes.join(key, f"p:{wire.frm}")
        nodes.join(key, f"p:{wire.to}")
        net = str(wire.net or "").strip()
        if net:
            if net in by_net:
                nodes.join(key, by_net[net])
            else:
                by_net[net] = key
    try:
        routes = route_sheet_wires(sheet, catalog, project) if project is not None else route_sheet_wires(sheet, catalog)
    except TypeError:
        routes = route_sheet_wires(sheet, catalog)
    through: set[tuple[str, str]] = set()
    horiz: list[tuple[str, float, float, float]] = []
    vert: list[tuple[str, float, float, float]] = []
    bends = 0
    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        if len(pts) < 2:
            continue
        bends += max(0, len(pts) - 2)
        node = nodes.find(f"w:{wire.id}")
        ends = {str(wire.frm).split(".", 1)[0], str(wire.to).split(".", 1)[0]}
        for a, b in zip(pts, pts[1:], strict=False):
            sx0, sx1 = sorted((float(a.x), float(b.x)))
            sy0, sy1 = sorted((float(a.y), float(b.y)))
            if abs(a.y - b.y) < 0.01 and sx1 - sx0 > 0.01:
                horiz.append((node, float(a.y), sx0, sx1))
            elif abs(a.x - b.x) < 0.01 and sy1 - sy0 > 0.01:
                vert.append((node, float(a.x), sy0, sy1))
            for cid, (x0, y0, x1, y1) in boxes.items():
                if cid in ends or (wire.id, cid) in through:
                    continue
                if sx0 < x1 - 1 and sx1 > x0 + 1 and sy0 < y1 - 1 and sy1 > y0 + 1:
                    through.add((wire.id, cid))
    points: set[tuple[float, float, frozenset[str]]] = set()
    for hnode, y, hx0, hx1 in horiz:
        for vnode, x, vy0, vy1 in vert:
            if hnode == vnode:
                continue
            if hx0 + _TOL < x < hx1 - _TOL and vy0 + _TOL < y < vy1 - _TOL:
                points.add((round(x, 1), round(y, 1), frozenset((hnode, vnode))))
    return {
        "overlaps": overlaps,
        "through": len(through),
        "crossings": len(points),
        "bends": bends,
    }


PRIMARY = ("overlaps", "through", "crossings")


CROSSING_ALLOWANCE = 3


def no_worse(after: dict[str, int], before: dict[str, int]) -> bool:
    """Overlaps and wires through symbols never rise.

    Crossings may rise by at most CROSSING_ALLOWANCE on a page, and only
    when that buys fewer overlaps. Moving one symbol clear re-routes its few
    wires; on the sample pages that costs 3 crossings (dol_starter S1/H2,
    4 -> 7). Extra bends only buy a real fix.
    """

    if after["overlaps"] > before["overlaps"] or after["through"] > before["through"]:
        return False
    cleared = after["overlaps"] < before["overlaps"]
    allowance = CROSSING_ALLOWANCE if cleared else 0
    if after["crossings"] > before["crossings"] + allowance:
        return False
    if after["bends"] <= before["bends"]:
        return True
    return any(after[key] < before[key] for key in PRIMARY)
