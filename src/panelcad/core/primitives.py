"""Normalized drawing primitives used by both library symbols and renderers."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Primitive(BaseModel):
    type: Literal["rect", "line", "circle", "polyline", "text"]
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    r: float = 0.0
    points: list[tuple[float, float]] = Field(default_factory=list)
    text: str = ""
    size: float = 3.0
    fill: bool = False


def expand_draw_item(item: dict[str, Any]) -> Primitive:
    if "rect" in item:
        x, y, w, h = item["rect"]
        return Primitive(type="rect", x=float(x), y=float(y), w=float(w), h=float(h), fill=bool(item.get("fill", False)))
    if "line" in item:
        x1, y1, x2, y2 = item["line"]
        return Primitive(type="line", x=float(x1), y=float(y1), x2=float(x2), y2=float(y2))
    if "circle" in item:
        cx, cy, r = item["circle"]
        return Primitive(type="circle", x=float(cx), y=float(cy), r=float(r), fill=bool(item.get("fill", False)))
    if "polyline" in item:
        pts = [(float(p[0]), float(p[1])) for p in item["polyline"]]
        return Primitive(type="polyline", points=pts)
    if "text" in item:
        raw = item["text"]
        x, y, text = raw[0], raw[1], raw[2]
        size = float(raw[3]) if len(raw) > 3 else float(item.get("size", 3.0))
        return Primitive(type="text", x=float(x), y=float(y), text=str(text), size=size)
    if "type" in item:
        return Primitive.model_validate(item)
    raise ValueError(f"unknown draw item: {item!r}")
