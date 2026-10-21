"""Millimetre draw list for the library symbol editor. Same YAML as catalog draw:."""

from __future__ import annotations

from typing import Any

from panelcad.core.primitives import arc_polyline, arc_through


def _snap(value: float, step: float) -> float:
    if step <= 0:
        return float(value)
    return round(float(value) / step) * step


def _as_xy(pt: Any) -> tuple[float, float]:
    if isinstance(pt, (list, tuple)) and len(pt) >= 2:
        return float(pt[0]), float(pt[1])
    return 0.0, 0.0


class DrawModel:
    def __init__(self, items: list[dict[str, Any]] | None = None, *, snap: float = 1.0) -> None:
        self.snap = float(snap)
        self.items: list[dict[str, Any]] = [dict(item) for item in (items or [])]

    @classmethod
    def from_draw(cls, items: list[Any] | None, *, snap: float = 1.0) -> DrawModel:
        return cls(list(items or []), snap=snap)

    def to_draw(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.items]

    def _s(self, value: float) -> float:
        return _snap(value, self.snap)

    def add_line(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self.items.append({"line": [self._s(x1), self._s(y1), self._s(x2), self._s(y2)]})

    def add_rect(self, x: float, y: float, w: float, h: float) -> None:
        x, y, w, h = self._s(x), self._s(y), self._s(w), self._s(h)
        if w < 0:
            x += w
            w = -w
        if h < 0:
            y += h
            h = -h
        self.items.append({"rect": [x, y, max(w, self.snap or 1), max(h, self.snap or 1)]})

    def add_circle(self, cx: float, cy: float, r: float) -> None:
        self.items.append({"circle": [self._s(cx), self._s(cy), max(self._s(abs(r)), self.snap or 1)]})

    def add_arc(self, cx: float, cy: float, r: float, start_deg: float, sweep_deg: float) -> None:
        """Store a true arc. The curve is not flattened onto the 1 mm grid.

        Snapping every chord to 1 mm is what made coils and heaters stair-step.
        Center and radius stay at the solved values. A full turn is a circle.
        """

        radius = abs(float(r))
        sweep = float(sweep_deg)
        if radius < 0.2 or abs(sweep) < 1.0:
            return
        if abs(abs(sweep) - 360.0) < 1.0 or abs(sweep) > 359.0:
            self.add_circle(cx, cy, radius)
            return
        self.items.append(
            {"arc": [float(cx), float(cy), radius, float(start_deg), sweep]}
        )

    def add_arc_through(
        self,
        p1: tuple[float, float],
        p2: tuple[float, float],
        p3: tuple[float, float],
    ) -> bool:
        solved = arc_through(p1, p2, p3)
        if solved is None:
            self.add_line(p1[0], p1[1], p3[0], p3[1])
            return False
        cx, cy, radius, start, sweep = solved
        self.add_arc(cx, cy, radius, start, sweep)
        return True

    def add_polyline(self, points: list[tuple[float, float]]) -> None:
        pts = [[self._s(x), self._s(y)] for x, y in (_as_xy(p) for p in points)]
        if len(pts) >= 2:
            self.items.append({"polyline": pts})

    def add_text(self, x: float, y: float, text: str, size: float = 2.5) -> None:
        label = str(text or "").strip()
        if not label:
            return
        self.items.append({"text": [self._s(x), self._s(y), label, float(size)]})

    def delete(self, index: int) -> None:
        if 0 <= index < len(self.items):
            del self.items[index]

    def move(self, index: int, dx: float, dy: float) -> None:
        if 0 <= index < len(self.items):
            self.items[index] = _translated(self.items[index], self._s(dx), self._s(dy))

    def replace_translated(self, index: int, origin: dict[str, Any], dx: float, dy: float) -> None:
        if 0 <= index < len(self.items):
            self.items[index] = _translated(origin, self._s(dx), self._s(dy))

    def hit_test(self, x: float, y: float, slop: float = 2.0) -> int | None:
        for i in range(len(self.items) - 1, -1, -1):
            if _item_hits(self.items[i], x, y, slop):
                return i
        return None


def _translated(item: dict[str, Any], dx: float, dy: float) -> dict[str, Any]:
    out = dict(item)
    if "line" in item:
        x1, y1, x2, y2 = item["line"]
        out["line"] = [x1 + dx, y1 + dy, x2 + dx, y2 + dy]
    elif "rect" in item:
        x, y, w, h = item["rect"]
        out["rect"] = [x + dx, y + dy, w, h]
    elif "circle" in item:
        cx, cy, r = item["circle"]
        out["circle"] = [cx + dx, cy + dy, r]
    elif "arc" in item:
        cx, cy, r, start, sweep = item["arc"]
        out["arc"] = [cx + dx, cy + dy, r, start, sweep]
    elif "polyline" in item:
        out["polyline"] = [[px + dx, py + dy] for px, py in item["polyline"]]
    elif "text" in item:
        raw = list(item["text"])
        raw[0] = float(raw[0]) + dx
        raw[1] = float(raw[1]) + dy
        out["text"] = raw
    return out


def _item_hits(item: dict[str, Any], x: float, y: float, slop: float) -> bool:
    if "line" in item:
        x1, y1, x2, y2 = item["line"]
        return _near_segment(x, y, x1, y1, x2, y2, slop)
    if "rect" in item:
        rx, ry, rw, rh = item["rect"]
        return rx - slop <= x <= rx + rw + slop and ry - slop <= y <= ry + rh + slop
    if "circle" in item:
        cx, cy, r = item["circle"]
        return abs(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 - r) <= slop or (
            (x - cx) ** 2 + (y - cy) ** 2
        ) ** 0.5 <= r + slop
    if "arc" in item:
        cx, cy, r, start, sweep = item["arc"]
        pts = arc_polyline(cx, cy, r, start, sweep)
        for a, b in zip(pts, pts[1:]):
            if _near_segment(x, y, a[0], a[1], b[0], b[1], slop):
                return True
        return False
    if "polyline" in item:
        pts = item["polyline"]
        for a, b in zip(pts, pts[1:]):
            if _near_segment(x, y, a[0], a[1], b[0], b[1], slop):
                return True
        return False
    if "text" in item:
        tx, ty = item["text"][0], item["text"][1]
        return abs(x - tx) <= 6 and abs(y - ty) <= 4
    return False


def _near_segment(x: float, y: float, x1: float, y1: float, x2: float, y2: float, slop: float) -> bool:
    dx, dy = x2 - x1, y2 - y1
    length = (dx * dx + dy * dy) ** 0.5
    if length < 1e-6:
        return ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5 <= slop
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (length * length)))
    px, py = x1 + t * dx, y1 + t * dy
    return ((x - px) ** 2 + (y - py) ** 2) ** 0.5 <= slop
